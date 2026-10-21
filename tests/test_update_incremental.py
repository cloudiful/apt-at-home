"""Incremental catalog updates from two realistic baselines, proven by file identity.

The six-package release adds six catalog packages (6+6). A five-package
release adds seven packages (5+7). Each run rebuilds exactly the added packages,
keeps every published .deb byte-identical, and publishes a complete pool. Assertions name the files, so a count that happens to match never
passes on its own.
"""

import copy
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from scripts.manifest import deb_filename, load_manifest
from scripts.metadata import ARCHES
from scripts.release_verify import restore_metadata
from scripts.storage import MAX_DEB_BYTES
from scripts.update_build import UpdateResult, apply_update, remove_superseded
from scripts.update_plan import UpdatePlan, build_plan
from tests.helpers import FakeS3, make_gpg_home_with_key
from tests.release_fixtures import (
    FIVE_PUBLISHED_NAMES,
    SIX_PUBLISHED_NAMES,
    build_release_objects,
)
from tests.test_update import _catalog_candidates, catalog_document
from tests.test_update_build import Store, _export_private_key, _stub_archive

BUCKET = Store.bucket


def _fixture_catalog() -> dict:
    import copy as _copy

    return _copy.deepcopy(catalog_document())


def _fixture_candidates() -> dict:
    return _catalog_candidates()


@dataclass(frozen=True)
class IncrementalRun:
    published: dict[str, bytes]
    pool: dict[str, bytes]
    result: UpdateResult
    plan: UpdatePlan
    output: Path


def run_incremental(
    test: unittest.TestCase, *, names: tuple[str, ...], release_id: str,
) -> IncrementalRun:
    """Seed a `names` baseline, apply the full-catalog update, and return both pools.

    The temporary directory is registered on `test`, so the built files stay on
    disk for the assertions that read them.
    """
    tmpdir, home, key_id = make_gpg_home_with_key()
    test.addCleanup(tmpdir.cleanup)
    root = Path(tmpdir.name)
    store = Store(FakeS3())
    store.seed(build_release_objects(root / "published", key_home=home, key_id=key_id, names=names))
    restored = restore_metadata(store, fingerprint=key_id)
    published = {
        name: store.get(f"pool/{name}", max_bytes=MAX_DEB_BYTES)
        for name in sorted(p.name for p in (root / "published" / "pool").glob("*.deb"))
    }
    plan = build_plan(
        baseline_release_id=restored.release_id,
        baseline_data=restored.manifest_data,
        catalog_data=_fixture_catalog(),
        candidates=_fixture_candidates(),
    )
    key_file = _export_private_key(home, root / "test-private.asc")
    output = root / "out"
    with _stub_archive():
        result = apply_update(
            plan=plan, release=restored, source=store, output_root=output,
            cache_dir=output.parent / "cache" / "upstream", release_id=release_id,
            bucket=BUCKET, fingerprint=key_id, signing_key_file=key_file,
            key_id=key_id, client=store.client,
        )
    pool = {p.name: p.read_bytes() for p in (output / "pool").glob("*.deb")}
    return IncrementalRun(published=published, pool=pool, result=result, plan=plan, output=output)


def _expected_built(names: tuple[str, ...]) -> list[str]:
    catalog = load_manifest()
    return sorted(
        deb_filename(pkg, arch, catalog)
        for pkg in catalog.packages if pkg.name in names
        for arch in sorted(pkg.targets)
    )


class SixPublishedSixAddedTest(unittest.TestCase):
    """Six published packages plus six catalog additions."""

    ADDED = ("bottom", "bun", "cargo-tauri", "kandev", "uv", "zoxide")

    def test_builds_exactly_the_six_additions_and_keeps_the_six_published(self) -> None:
        run = run_incremental(self, names=SIX_PUBLISHED_NAMES, release_id="incremental-6-4")
        self.assertEqual(run.plan.added, self.ADDED)
        self.assertEqual(sorted(p.name for p in run.result.debs), _expected_built(self.ADDED))
        self.assertEqual(len(run.result.debs), 11)
        self.assertEqual(len(run.pool), 23)
        self.assertEqual(len(run.published), 12)
        for name, payload in run.published.items():
            self.assertEqual(run.pool[name], payload, msg=name)
        for arch in ARCHES:
            packages = run.output / "releases" / "incremental-6-4" / "dists" / "stable" / "main" / f"binary-{arch}" / "Packages"
            expected = 12 if arch == "amd64" else 11
            self.assertEqual(len(packages.read_text().strip().split("\n\n")), expected, msg=arch)


class FivePublishedSevenAddedTest(unittest.TestCase):
    """Five published packages plus the seven missing catalog packages."""

    ADDED = ("bottom", "bun", "cargo-tauri", "kandev", "neovim", "uv", "zoxide")

    def test_builds_exactly_the_seven_additions_and_keeps_the_ten_published_debs(self) -> None:
        run = run_incremental(self, names=FIVE_PUBLISHED_NAMES, release_id="incremental-5-5")
        self.assertEqual(run.plan.added, self.ADDED)
        self.assertEqual(sorted(p.name for p in run.result.debs), _expected_built(self.ADDED))
        self.assertEqual(len(run.result.debs), 13)
        self.assertEqual(len(run.published), 10)
        self.assertEqual(len(run.pool), 23)
        for name, payload in run.published.items():
            self.assertEqual(run.pool[name], payload, msg=name)


class PartialArchSupersededTest(unittest.TestCase):
    """An unchanged AMD-only package keeps its pool file; a changed one drops it."""

    def _plan(self, changed: tuple[str, ...]):
        from scripts.manifest import parse_manifest as _parse
        from scripts.release_manifest import load_manifest_data as _load_data
        from scripts.manifest import default_manifest_path as _default_path

        document = _load_data(_default_path())
        manifest = _parse(document)
        package = next(p for p in manifest.packages if p.name == "cargo-tauri")
        pool = Path(self.enterContext(tempfile.TemporaryDirectory())) / "pool"
        pool.mkdir()
        for name in (deb_filename(package, "amd64", manifest),):
            (pool / name).write_bytes(b"published")
        plan = UpdatePlan(
            baseline_release_id="r1",
            baseline_manifest=manifest,
            manifest_data=document,
            manifest=manifest,
            changed=changed,
            versions={"cargo-tauri": package.version},
        )
        return plan, pool

    def test_an_unchanged_amd_only_package_keeps_its_published_deb(self) -> None:
        plan, pool = self._plan(())
        self.assertEqual(remove_superseded(plan, pool), [])
        self.assertEqual(len(list(pool.glob("*.deb"))), 1)

    def test_a_rebuilt_amd_only_package_drops_its_published_deb(self) -> None:
        plan, pool = self._plan(("cargo-tauri",))
        self.assertEqual(
            sorted(remove_superseded(plan, pool)),
            ["cargo-tauri_2.12.2-1_amd64.deb"],
        )
        self.assertEqual(list(pool.glob("*.deb")), [])


if __name__ == "__main__":
    unittest.main()
