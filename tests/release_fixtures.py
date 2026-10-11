"""Shared signed-release fixtures for the restore, update, and publish tests.

A real throwaway GPG homedir signs a manifest-bound release, and the resulting
objects are handed around as a dict keyed by served path. Every consumer then
exercises the production trust chain without storage credentials or an operator
key; the in-memory doubles keep a missing key distinguishable from a denied one.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

from scripts.deb import build_deb
from scripts.manifest import (
    Manifest,
    default_manifest_path,
    deb_filename,
    load_manifest,
    parse_manifest,
)
from scripts.metadata import ARCHES, generate_release, write_indices
from scripts.release_manifest import (
    MANIFEST_NAME,
    load_manifest_data,
    manifest_binding_fields,
    write_manifest_document,
)
from scripts.reprepro_adapter import generate_packages
from scripts.sign import export_public_key, sign_release
from scripts.storage import ObjectNotFound
from tests.helpers import stage_fixture_target

RELEASE_ID = "20260102-030405"
COMMITTED = load_manifest()
CATALOG_NAMES = tuple(pkg.name for pkg in COMMITTED.packages)

# What production publishes today: the current release predates the newer
# catalog entries, so the fixtures below model that two-package baseline rather
# than assuming the committed catalog was already shipped.
PUBLISHED_NAMES = ("starship", "zellij")
# The five packages carried before neovim joined, and that six-package release.
FIVE_PUBLISHED_NAMES = ("git-delta", "rustfs-cli", "sccache", "starship", "zellij")
SIX_PUBLISHED_NAMES = (*FIVE_PUBLISHED_NAMES, "neovim")


def published_manifest_data(names: tuple[str, ...] = PUBLISHED_NAMES) -> dict[str, object]:
    """The committed catalog restricted to what the published release carries."""
    data = copy.deepcopy(load_manifest_data(default_manifest_path()))
    packages = data.get("packages")
    assert isinstance(packages, list)
    data["packages"] = [
        entry for entry in packages if entry.get("name") in names
    ]
    return data


def published_manifest(names: tuple[str, ...] = PUBLISHED_NAMES) -> Manifest:
    """The validated catalog subset the published release binds."""
    return parse_manifest(published_manifest_data(names))


PUBLISHED = published_manifest()


class FakeStorage:
    """Object store double: a missing key raises, byte ceilings are enforced."""

    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = dict(objects)
        self.reads: list[str] = []

    def get(self, key: str, *, max_bytes: int) -> bytes:
        self.reads.append(key)
        if key not in self.objects:
            raise ObjectNotFound(key)
        payload = self.objects[key]
        if len(payload) > max_bytes:
            raise ValueError(f"object {key} exceeded the {max_bytes} byte limit")
        return payload


class DeniedStorage(FakeStorage):
    """Every read fails the way a permissions error does, never like a 404."""

    def get(self, key: str, *, max_bytes: int) -> bytes:
        raise RuntimeError("An error occurred (403) Forbidden: access denied")


def _build_debs(root: Path, manifest: Manifest) -> None:
    """Stage the published packages as real .deb files under root/pool."""
    pool = root / "pool"
    pool.mkdir(parents=True, exist_ok=True)
    for pkg in manifest.packages:
        for arch in ARCHES:
            stage = stage_fixture_target(
                root, pkg, arch, manifest, f"fake-{pkg.name}-{arch}".encode(), root / "stages",
            )
            build_deb(stage, pool / deb_filename(pkg, arch, manifest))


def build_release_objects(
    root: Path,
    *,
    key_home: Path,
    key_id: str,
    bind: bool = True,
    names: tuple[str, ...] = PUBLISHED_NAMES,
) -> dict[str, bytes]:
    """Build a signed release and return its served objects.

    `bind` stays for the explicit negative case: an unbound release has no
    `X-Manifest-*` fields and no artifact, which the trust chain must reject.
    `names` selects the published package set, so the incremental fixtures can
    model a five-package baseline without changing the two-package default.
    """
    published = published_manifest(names)
    _build_debs(root, published)
    pool = root / "pool"
    dists = root / "releases" / RELEASE_ID / "dists" / "stable"
    packages_files: dict[str, list[Path]] = {}
    inputs: list[tuple[Path, str]] = []
    for arch in ARCHES:
        packages, packages_gz = generate_packages(
            pool, dists / "main" / f"binary-{arch}", sorted(pool.glob(f"*_{arch}.deb"))
        )
        packages_files[arch] = [packages, packages_gz]
        inputs.append((packages, f"main/binary-{arch}/Packages"))
        inputs.append((packages_gz, f"main/binary-{arch}/Packages.gz"))

    extra: list[tuple[str, str]] = []
    if bind:
        artifact = write_manifest_document(
            data=published_manifest_data(names),
            destination=root / "releases" / RELEASE_ID / MANIFEST_NAME,
        )
        extra = manifest_binding_fields(artifact)
    release_file = generate_release(
        output_root=root, release_id=RELEASE_ID, files=inputs, extra_fields=extra
    )
    write_indices(output_root=root, release_id=RELEASE_ID, packages_files=packages_files)

    previous = os.environ.get("GNUPGHOME")
    os.environ["GNUPGHOME"] = str(key_home)
    try:
        sign_release(release_file=release_file, key_id=key_id)
        export_public_key(homedir=key_home, key_id=key_id, dest=root / "key.asc")
    finally:
        if previous is None:
            os.environ.pop("GNUPGHOME", None)
        else:
            os.environ["GNUPGHOME"] = previous

    objects: dict[str, bytes] = {
        "current.json": (json.dumps({"release": RELEASE_ID}) + "\n").encode(),
        "key.asc": (root / "key.asc").read_bytes(),
    }
    base = root / "releases" / RELEASE_ID
    for path in sorted(base.rglob("*")):
        if path.is_file():
            objects[str(path.relative_to(root))] = path.read_bytes()
    for deb in sorted(pool.glob("*.deb")):
        objects[f"pool/{deb.name}"] = deb.read_bytes()
    return objects
