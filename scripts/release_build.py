"""Build one release tree: pool, reprepro indices, bound manifest, signature.

Kept apart from the CLI so the build order that produces a verifiable release
is one testable unit: indices first, then the canonical manifest artifact, then
a Release that binds that artifact, and only then a single signature pass.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Sequence
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
) -> list[Path]:
    """Build one release tree; returns the pool .deb paths in sorted order.

    Order matters for trust: the indices come first, then the canonical
    manifest artifact, then a Release that binds that artifact by digest and
    size, and only then a single signature pass over the finished Release.
    `manifest_document` and `packages` let an incremental update supply the
    overlay it computed and rebuild only what changed.
    """
    source = manifest_path or default_manifest_path()
    pinned = parse_manifest(manifest_document) if manifest_document is not None else load_manifest(source)
    pool_dir = output_root / "pool"
    debs = build_all(
        pinned, pool_dir=pool_dir, cache_dir=cache_dir, packages=packages,
    )
    declared = manifest_document if manifest_document is not None else load_manifest_data(source)

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
        data=declared,
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
