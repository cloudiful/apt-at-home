"""End-to-end incremental update against an in-memory object store.

The published release, the reprepro indices, the GPG signature, and the pointer
ordering are all real; only the upstream archive download is stubbed, so the
test proves the update mechanics rather than GitHub's availability. The
headline case is the one production will run first: a published two-package
release meeting the full catalog, which must add every catalog package it
lacks, build exactly their .deb files (cargo-tauri is AMD-only), keep the
published ones byte-identical, and publish a complete release.
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.manifest import deb_filename, load_manifest
from scripts.metadata import ARCHES
from scripts.release_verify import restore_metadata
from scripts.smoke_package import check_copyright, check_package
from scripts.restore_packages import restore_pool
from scripts.storage import MAX_DEB_BYTES, ObjectNotFound
from scripts.update_build import apply_update
from scripts.update_plan import build_plan
from tests.helpers import FakeS3, archive_stem, make_gpg_home_with_key, write_target_archive
from tests.test_kandev_bundle import write_bundle_archive
from tests.release_fixtures import (
    CATALOG_NAMES,
    PUBLISHED_NAMES,
    RELEASE_ID,
    build_release_objects,
    published_manifest,
    published_manifest_data,
)
from tests.test_update import _catalog_candidates, candidate, catalog_document


def _fixture_catalog() -> dict:
    """The committed catalog as ordinary asset-only documents."""
    import copy as _copy

    return _copy.deepcopy(catalog_document())


def _fixture_candidates() -> dict:
    """Candidates matching the committed catalog's pinned versions."""
    return _catalog_candidates()


def _catalog_arches() -> dict[str, tuple[str, ...]]:
    """The architectures each catalog package is built for."""
    return {pkg.name: tuple(sorted(pkg.targets)) for pkg in load_manifest().packages}


def _debs_for(names: tuple[str, ...]) -> int:
    """The number of debs the named catalog packages contribute."""
    arches = _catalog_arches()
    return sum(len(arches[name]) for name in names)


def _packages_for(arch: str, names: tuple[str, ...]) -> int:
    """The number of the given packages the index for one architecture lists."""
    arches = _catalog_arches()
    return sum(1 for name in names if arch in arches[name])


class Store:
    """The published bucket: restore reads through it, publish writes to it."""

    def __init__(self, client: FakeS3) -> None:
        self.client = client
        self.reads: list[str] = []

    def seed(self, objects: dict[str, bytes]) -> None:
        for key, payload in objects.items():
            self.client.objects[(self.bucket, key)] = payload

    bucket = "test-bucket"

    def get(self, key: str, *, max_bytes: int) -> bytes:
        self.reads.append(key)
        payload = self.client.objects.get((self.bucket, key))
        if payload is None:
            raise ObjectNotFound(key)
        if len(payload) > max_bytes:
            raise ValueError(f"object {key} exceeded the {max_bytes} byte limit")
        return payload


def _stub_archive():
    """Serve any requested archive with the exact member its manifest pins."""

    def fetch(url: str, expected_sha256: str, dest: Path) -> Path:
        stem = archive_stem(dest.name)
        manifest = load_manifest()
        name = next(
            pkg.name
            for pkg in sorted(manifest.packages, key=lambda p: len(p.name), reverse=True)
            if stem.startswith(f"{pkg.name}-")
        )
        pkg = next(p for p in manifest.packages if p.name == name)
        arch = stem.rsplit("-", 1)[1]
        payload = f"fake-{name}-{arch}".encode()
        if name == "kandev":
            return write_bundle_archive(dest, version=f"v{pkg.version}", arch=arch)
        return write_target_archive(dest, pkg, arch, payload)

    return mock.patch("scripts.build.fetch_verified", fetch)


def _export_private_key(home: Path, dest: Path) -> Path:
    """Export the ephemeral test key so a build can sign exactly like production."""
    proc = subprocess.run(
        ["gpg", "--batch", "--armor", "--export-secret-keys"],
        env={"GNUPGHOME": str(home), "PATH": os.environ["PATH"], "GPG_AGENT_INFO": ""},
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(f"secret key export failed: {proc.stderr}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(proc.stdout, encoding="utf-8")
    dest.chmod(0o600)
    return dest


class UpdateBuildTest(unittest.TestCase):
    def _seeded_store(self, work: Path, key_home: Path, key_id: str) -> Store:
        store = Store(FakeS3())
        store.seed(build_release_objects(work / "published", key_home=key_home, key_id=key_id))
        return store

    def test_the_catalog_addition_builds_every_added_deb_and_keeps_the_published_ones(self) -> None:
        tmpdir, home, key_id = make_gpg_home_with_key()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                store = self._seeded_store(root, home, key_id)
                output = root / "out"
                restored = restore_metadata(store, fingerprint=key_id)
                self.assertEqual(restored.release_id, RELEASE_ID)
                self.assertEqual(
                    sorted(p.name for p in restored.manifest.packages),
                    sorted(PUBLISHED_NAMES),
                )
                published_pool = {
                    path.name: store.get(f"pool/{path.name}", max_bytes=MAX_DEB_BYTES)
                    for path in (root / "published" / "pool").glob("*.deb")
                }
                self.assertEqual(len(published_pool), _debs_for(PUBLISHED_NAMES))

                plan = build_plan(
                    baseline_release_id=restored.release_id,
                    baseline_data=restored.manifest_data,
                    catalog_data=_fixture_catalog(),
                    candidates=_fixture_candidates(),
                )
                additions = tuple(sorted(set(CATALOG_NAMES) - set(PUBLISHED_NAMES)))
                self.assertEqual(plan.changed, additions)

                key_file = _export_private_key(home, root / "test-private.asc")
                with _stub_archive():
                    result = apply_update(
                        plan=plan,
                        release=restored,
                        source=store,
                        output_root=output,
                        cache_dir=output.parent / "cache" / "upstream",
                        release_id="catalog-release",
                        bucket=Store.bucket,
                        fingerprint=key_id,
                        signing_key_file=key_file,
                        key_id=key_id,
                        client=store.client,
                    )
                self.assertEqual(result.release_id, "catalog-release")

                pool = {p.name: p.read_bytes() for p in (output / "pool").glob("*.deb")}
                self.assertEqual(len(pool), _debs_for(CATALOG_NAMES))
                # Exactly the added packages were built for their reviewed
                # architectures: cargo-tauri contributes only its AMD target.
                self.assertEqual(
                    sorted(path.name for path in result.debs),
                    sorted(
                        deb_filename(pkg, arch, plan.manifest)
                        for pkg in plan.manifest.packages
                        if pkg.name in additions
                        for arch in sorted(pkg.targets)
                    ),
                )
                self.assertEqual(len(result.debs), _debs_for(additions))
                # Every published package stays byte-identical.
                for name, payload in published_pool.items():
                    self.assertEqual(pool[name], payload, msg=name)

                for arch in ARCHES:
                    text = (
                        output / "releases" / "catalog-release" / "dists" / "stable"
                        / "main" / f"binary-{arch}" / "Packages"
                    ).read_text()
                    expected_count = _packages_for(arch, CATALOG_NAMES)
                    self.assertEqual(len(text.strip().split("\n\n")), expected_count, msg=arch)
                    self.assertIn("Version: 0.20.1-1", text)
                    # The Debian name and the installed command differ for rc.
                    self.assertIn(f"Package: rustfs-cli\n", text)
                    self.assertIn(f"Filename: pool/rustfs-cli_0.1.36-1_{arch}.deb", text)
                    # The glibc-linked delta arm64 build and every neovim build declare
                    # runtime dependencies; the static delta amd64 build declares none.
                    stanzas = text.strip().split("\n\n")
                    neovim_stanza = next(s for s in stanzas if s.startswith("Package: neovim\n"))
                    self.assertIn("Depends: libc6 (>= 2.34), libgcc-s1", neovim_stanza, msg=arch)
                    delta_stanza = next(
                        s for s in stanzas
                        if s.startswith("Package: git-delta\n") and f"Architecture: {arch}\n" in s
                    )
                    self.assertEqual("Depends:" in delta_stanza, arch == "arm64", msg=arch)
                    kandev_stanza = next(s for s in stanzas if s.startswith("Package: kandev\n"))
                    self.assertIn("Depends: libc6 (>= 2.34)", kandev_stanza, msg=arch)
                    uv = next(pkg for pkg in plan.manifest.packages if pkg.name == "uv")
                    check_package(uv, arch, manifest=plan.manifest, pool=output / "pool")
                    uv_root = root / f"uv-{arch}"
                    extracted = subprocess.run(
                        ["dpkg-deb", "-x", str(output / "pool" / deb_filename(uv, arch, plan.manifest)), str(uv_root)],
                        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                    )
                    self.assertEqual(extracted.returncode, 0, msg=extracted.stderr)
                    check_copyright(uv, uv_root)

                # The new release binds the complete catalog manifest.
                bound = json.loads(
                    (output / "releases" / "catalog-release" / "manifest.json").read_text()
                )
                self.assertEqual(len(bound["packages"]), len(CATALOG_NAMES))
                self.assertIn("releases/catalog-release/manifest.json", store.client.puts)
                # The pointer is the last object written.
                self.assertEqual(store.client.puts[-1], "current.json")
                self.assertEqual(
                    json.loads(store.client.objects[(Store.bucket, "current.json")])["release"],
                    "catalog-release",
                )
        finally:
            tmpdir.cleanup()

    def test_second_run_after_the_addition_finds_nothing(self) -> None:
        tmpdir, home, key_id = make_gpg_home_with_key()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                store = self._seeded_store(root, home, key_id)
                output = root / "out"
                restored = restore_metadata(store, fingerprint=key_id)
                plan = build_plan(
                    baseline_release_id=restored.release_id,
                    baseline_data=restored.manifest_data,
                    catalog_data=_fixture_catalog(),
                    candidates=_fixture_candidates(),
                )
                key_file = _export_private_key(home, root / "test-private.asc")
                with _stub_archive():
                    apply_update(
                        plan=plan, release=restored, source=store, output_root=output,
                        cache_dir=output.parent / "cache" / "upstream",
                        release_id="catalog-release", bucket=Store.bucket,
                        fingerprint=key_id, signing_key_file=key_file, key_id=key_id,
                        client=store.client,
                    )

                uploaded_before = list(store.client.puts)
                follow_up = restore_metadata(store, fingerprint=key_id)
                self.assertEqual(follow_up.release_id, "catalog-release")
                self.assertEqual(len(follow_up.manifest.packages), len(CATALOG_NAMES))
                second = build_plan(
                    baseline_release_id=follow_up.release_id,
                    baseline_data=follow_up.manifest_data,
                    catalog_data=_fixture_catalog(),
                    candidates=_fixture_candidates(),
                )
                self.assertTrue(second.is_noop)
                self.assertEqual(store.client.puts, uploaded_before)
        finally:
            tmpdir.cleanup()

    def test_one_changed_package_rebuilds_two_debs_and_keeps_the_rest(self) -> None:
        tmpdir, home, key_id = make_gpg_home_with_key()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                store = self._seeded_store(root, home, key_id)
                output = root / "out"
                restored = restore_metadata(store, fingerprint=key_id)
                published_pool = {
                    path.name: store.get(f"pool/{path.name}", max_bytes=MAX_DEB_BYTES)
                    for path in (root / "published" / "pool").glob("*.deb")
                }

                plan = build_plan(
                    baseline_release_id=restored.release_id,
                    baseline_data=restored.manifest_data,
                    # The catalog already matches the published release, so the
                    # only work is the single upgraded package.
                    catalog_data=published_manifest_data(),
                    candidates={
                        "starship": candidate("starship", "1.27.0"),
                        "zellij": candidate("zellij", "0.45.1"),
                    },
                )
                self.assertEqual(plan.changed, ("starship",))

                key_file = _export_private_key(home, root / "test-private.asc")
                with _stub_archive():
                    result = apply_update(
                        plan=plan,
                        release=restored,
                        source=store,
                        output_root=output,
                        cache_dir=output.parent / "cache" / "upstream",
                        release_id="p2-release",
                        bucket=Store.bucket,
                        fingerprint=key_id,
                        signing_key_file=key_file,
                        key_id=key_id,
                        client=store.client,
                    )
                self.assertEqual(result.release_id, "p2-release")

                pool = {p.name: p.read_bytes() for p in (output / "pool").glob("*.deb")}
                self.assertEqual(len(pool), _debs_for(PUBLISHED_NAMES))
                self.assertEqual(
                    sorted(path.name for path in result.debs),
                    ["starship_1.27.0-1_amd64.deb", "starship_1.27.0-1_arm64.deb"],
                )
                for name, payload in published_pool.items():
                    if name.startswith("zellij"):
                        self.assertEqual(pool[name], payload, msg=name)
                self.assertNotIn("starship_1.26.0-1_amd64.deb", pool)

                for arch in ARCHES:
                    text = (
                        output / "releases" / "p2-release" / "dists" / "stable"
                        / "main" / f"binary-{arch}" / "Packages"
                    ).read_text()
                    self.assertEqual(
                        len(text.strip().split("\n\n")),
                        _packages_for(arch, PUBLISHED_NAMES),
                    )
                    self.assertIn("Version: 1.27.0-1", text)

                self.assertEqual(store.client.puts[-1], "current.json")
        finally:
            tmpdir.cleanup()

    def test_restore_rejects_a_tampered_pool_object(self) -> None:
        tmpdir, home, key_id = make_gpg_home_with_key()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                store = self._seeded_store(root, home, key_id)
                restored = restore_metadata(store, fingerprint=key_id)
                target = deb_filename(published_manifest().packages[0], "amd64", published_manifest())
                key = (Store.bucket, f"pool/{target}")
                store.client.objects[key] = store.client.objects[key] + b"tampered"
                with self.assertRaises(ValueError):
                    restore_pool(restored, store, root / "out")
        finally:
            tmpdir.cleanup()


if __name__ == "__main__":
    unittest.main()