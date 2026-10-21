"""Incremental catalog updates from two realistic baselines, proven by file identity.

The six-package release and the five-package release each add the catalog
packages they lack. Each run rebuilds exactly the added packages, keeps every
published .deb byte-identical, and publishes a complete pool. Assertions name
the files, so a count that happens to match never passes on its own.
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
    CATALOG_NAMES,
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


def _added_for(published: tuple[str, ...]) -> tuple[str, ...]:
    """The catalog packages a published baseline does not carry yet, sorted."""
    return tuple(sorted(set(CATALOG_NAMES) - set(published)))


def _debs_for(names: tuple[str, ...]) -> int:
    """The number of debs the named catalog packages contribute."""
    targets = {pkg.name: len(pkg.targets) for pkg in load_manifest().packages}
    return sum(targets[name] for name in names)


def _packages_for(arch: str) -> int:
    """The number of catalog packages the index for one architecture lists."""
    return sum(1 for pkg in load_manifest().packages if arch in pkg.targets)


class SixPublishedSubsetTest(unittest.TestCase):
    """The six-package published baseline plus the catalog packages it lacks."""

    ADDED = _added_for(SIX_PUBLISHED_NAMES)

    def test_builds_exactly_the_added_packages_and_keeps_the_published_debs(self) -> None:
        run = run_incremental(self, names=SIX_PUBLISHED_NAMES, release_id="incremental-6-4")
        self.assertEqual(run.plan.added, self.ADDED)
        self.assertEqual(sorted(p.name for p in run.result.debs), _expected_built(self.ADDED))
        self.assertEqual(len(run.result.debs), _debs_for(self.ADDED))
        self.assertEqual(len(run.pool), _debs_for(CATALOG_NAMES))
        self.assertEqual(len(run.published), _debs_for(SIX_PUBLISHED_NAMES))
        for name, payload in run.published.items():
            self.assertEqual(run.pool[name], payload, msg=name)
        for arch in ARCHES:
            packages = run.output / "releases" / "incremental-6-4" / "dists" / "stable" / "main" / f"binary-{arch}" / "Packages"
            expected = _packages_for(arch)
            self.assertEqual(len(packages.read_text().strip().split("\n\n")), expected, msg=arch)


class FivePublishedSubsetTest(unittest.TestCase):
    """The five-package published baseline plus the catalog packages it lacks."""

    ADDED = _added_for(FIVE_PUBLISHED_NAMES)

    def test_builds_exactly_the_added_packages_and_keeps_the_published_debs(self) -> None:
        run = run_incremental(self, names=FIVE_PUBLISHED_NAMES, release_id="incremental-5-5")
        self.assertEqual(run.plan.added, self.ADDED)
        self.assertEqual(sorted(p.name for p in run.result.debs), _expected_built(self.ADDED))
        self.assertEqual(len(run.result.debs), _debs_for(self.ADDED))
        self.assertEqual(len(run.published), _debs_for(FIVE_PUBLISHED_NAMES))
        self.assertEqual(len(run.pool), _debs_for(CATALOG_NAMES))
        for name, payload in run.published.items():
            self.assertEqual(run.pool[name], payload, msg=name)


class PartialTargetSupersededTest(unittest.TestCase):
    """Cleanup is per target: a kept architecture stays, a rebuilt one drops."""

    def _plan(self, changed: tuple[str, ...], baseline, final):
        return UpdatePlan(
            baseline_release_id="r1",
            baseline_manifest=baseline,
            manifest_data={},
            manifest=final,
            changed=changed,
            versions={},
        )

    def _pool(self, package, manifest) -> Path:
        pool = Path(self.enterContext(tempfile.TemporaryDirectory())) / "pool"
        pool.mkdir()
        for arch in package.targets:
            (pool / deb_filename(package, arch, manifest)).write_bytes(b"published")
        return pool

    def test_a_new_architecture_keeps_the_published_target_object(self) -> None:
        from tests.release_fixtures import historical_cargo_baseline

        manifest = load_manifest()
        baseline = historical_cargo_baseline()
        published_cargo = next(
            p for p in baseline.packages if p.name == "cargo-tauri"
        )
        pool = self._pool(published_cargo, baseline)
        plan = self._plan(("cargo-tauri",), baseline, manifest)
        self.assertEqual(remove_superseded(plan, pool), [])
        self.assertEqual(
            [path.name for path in pool.iterdir()],
            ["cargo-tauri_2.12.3-1_amd64.deb"],
        )

    def test_a_rebuilt_target_drops_its_published_deb(self) -> None:
        from dataclasses import replace

        manifest = load_manifest()
        cargo = next(p for p in manifest.packages if p.name == "cargo-tauri")
        rebuilt = replace(
            cargo,
            targets={
                arch: replace(target, depends=target.depends + ", zlib1g")
                for arch, target in cargo.targets.items()
            },
        )
        final = replace(manifest, packages=(rebuilt,))
        pool = self._pool(cargo, manifest)
        plan = self._plan(("cargo-tauri",), manifest, final)
        self.assertEqual(
            sorted(remove_superseded(plan, pool)),
            ["cargo-tauri_2.12.3-1_amd64.deb", "cargo-tauri_2.12.3-1_arm64.deb"],
        )
        self.assertEqual(list(pool.glob("*.deb")), [])

    def test_an_unchanged_package_keeps_every_published_target(self) -> None:
        manifest = load_manifest()
        cargo = next(p for p in manifest.packages if p.name == "cargo-tauri")
        pool = self._pool(cargo, manifest)
        plan = self._plan((), manifest, manifest)
        self.assertEqual(remove_superseded(plan, pool), [])
        self.assertEqual(len(list(pool.glob("*.deb"))), 2)


if __name__ == "__main__":
    unittest.main()
