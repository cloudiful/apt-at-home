"""Build one release tree: pool, reprepro indices, bound manifest, signature.

Kept apart from the CLI so the build order that produces a verifiable release
is one testable unit: indices first, then the canonical manifest artifact, then
a Release that binds that artifact, and only then a single signature pass.

The manifest that gets bound is the *realized* one. A source-built target is
declared as a plan before anything is compiled, and this module merges each
measured artifact into that plan before the artifact bytes are written. A target
that would still be a plan at signing time is refused: the Release binds the
manifest bytes, so a plan reaching the signature would publish a source package
with no provenance for the bytes it was built from.

A target reused from the published release merges the provenance that shipped
with it instead of a fresh measurement, which is the same shape: the manifest a
release signs always states measured facts for every source-built target in it.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .build import build_all
from .manifest import default_manifest_path, load_manifest, parse_manifest
from .metadata import ARCHES, COMPONENT, SUITE, generate_release, write_indices
from .release_manifest import (
    MANIFEST_NAME,
    load_manifest_data,
    manifest_binding_fields,
    write_manifest_document,
)
from .reprepro_adapter import generate_packages
from .sign import IsolatedHomedir, export_public_key, import_key, sign_release
from .source_artifacts import ReusedSource, SourceArtifacts
from .source_build import check_realized_document, realize_document


@contextmanager
def _gnupghome(home: Path) -> Iterator[None]:
    """Point GnuPG at an isolated homedir for the duration of the block."""
    previous = os.environ.get("GNUPGHOME")
    os.environ["GNUPGHOME"] = str(home)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("GNUPGHOME", None)
        else:
            os.environ["GNUPGHOME"] = previous


def build_release(
    *,
    output_root: Path,
    release_id: str,
    cache_dir: Path,
    manifest_path: Path | None = None,
    manifest_document: dict[str, object] | None = None,
    packages: Sequence[str] | None = None,
    signing_key_file: Path | None = None,
    key_id: str | None = None,
    source_artifacts: Mapping[tuple[str, str], SourceArtifacts] | None = None,
    reused_sources: Mapping[tuple[str, str], ReusedSource] | None = None,
) -> list[Path]:
    """Build one release tree; returns the pool .deb paths in sorted order.

    Order matters for trust: the indices come first, then the canonical
    manifest artifact, then a Release that binds that artifact by digest and
    size, and only then a single signature pass over the finished Release.
    `manifest_document` and `packages` let an incremental update supply the
    overlay it computed and rebuild only what changed, `source_artifacts`
    supplies the compilations a source-built target is built from, and
    `reused_sources` supplies the already published deb for one whose version has
    not changed — the bytes and the realized provenance are then the ones clients
    already have, merged below rather than measured again.
    """
    source = manifest_path or default_manifest_path()
    pinned = parse_manifest(manifest_document) if manifest_document is not None else load_manifest(source)
    pool_dir = output_root / "pool"
    debs = build_all(
        pinned, pool_dir=pool_dir, cache_dir=cache_dir, packages=packages,
        source_artifacts=source_artifacts, reused_sources=reused_sources,
    )
    declared = manifest_document if manifest_document is not None else load_manifest_data(source)
    # Measured provenance is merged in before anything is written, so the bound
    # artifact and the signed Release describe the realized build, not the plan.
    # A reused target contributes the provenance that shipped with it: same
    # identity, same measurement, already authenticated by the release it came
    # from, so `realize_document` refuses any disagreement before signing.
    measured = {key: item.artifact for key, item in (source_artifacts or {}).items()}
    measured.update({key: item.artifact for key, item in (reused_sources or {}).items()})
    document = realize_document(declared, measured)
    check_realized_document(document)

    dists = output_root / "releases" / release_id / "dists" / SUITE / COMPONENT
    packages_files: dict[str, list[Path]] = {}
    release_inputs: list[tuple[Path, str]] = []
    for arch in ARCHES:
        arch_debs = sorted(pool_dir.glob(f"*_{arch}.deb"))
        arch_dir = dists / f"binary-{arch}"
        generated, generated_gz = generate_packages(pool_dir, arch_dir, arch_debs)
        packages_files[arch] = [generated, generated_gz]
        release_inputs.append((generated, f"{COMPONENT}/binary-{arch}/Packages"))
        release_inputs.append((generated_gz, f"{COMPONENT}/binary-{arch}/Packages.gz"))

    artifact = write_manifest_document(
        data=document,
        destination=output_root / "releases" / release_id / MANIFEST_NAME,
    )
    release_file = generate_release(
        output_root=output_root,
        release_id=release_id,
        files=release_inputs,
        extra_fields=manifest_binding_fields(artifact),
    )
    write_indices(output_root=output_root, release_id=release_id, packages_files=packages_files)

    if signing_key_file is None:
        print(
            "no signing key provided; Release left unsigned (verify will fail until signed)",
            file=sys.stderr,
        )
    else:
        if not signing_key_file.exists():
            raise ValueError(f"signing key file not found: {signing_key_file}")
        with IsolatedHomedir() as home, _gnupghome(home):
            import_key(home, signing_key_file)
            sign_release(release_file=release_file, key_id=key_id)
            export_public_key(homedir=home, key_id=key_id, dest=output_root / "key.asc")
    (output_root / "current.json").write_text(
        json.dumps({"release": release_id}) + "\n", encoding="utf-8"
    )
    return debs
