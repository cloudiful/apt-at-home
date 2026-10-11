"""Incremental catalog updates from two realistic baselines, proven by file identity.

The six-package release adds the three new commands (6+3). A five-package
release adds the four packages it lacks (5+4). Each run rebuilds exactly the
added packages, keeps every published .deb byte-identical, and publishes a
complete pool. Assertions name the files, so a count that happens to match never
passes on its own.
"""

import unittest
from dataclasses import dataclass
from pathlib import Path

from scripts.manifest import deb_filename, load_manifest
from scripts.metadata import ARCHES
from scripts.release_verify import restore_metadata
from scripts.storage import MAX_DEB_BYTES
from scripts.update_build import UpdateResult, apply_update
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
        catalog_data=catalog_document(),
        candidates=_catalog_candidates(),
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
        for arch in ARCHES
    )


class SixPublishedThreeAddedTest(unittest.TestCase):
    """6 published packages + bottom, uv, and zoxide: 6 rebuilt, 12 kept."""

    ADDED = ("bottom", "uv", "zoxide")

    def test_builds_exactly_the_three_additions_and_keeps_the_six_published(self) -> None:
        run = run_incremental(self, names=SIX_PUBLISHED_NAMES, release_id="incremental-6-3")
        self.assertEqual(run.plan.added, self.ADDED)
        self.assertEqual(sorted(p.name for p in run.result.debs), _expected_built(self.ADDED))
        self.assertEqual(len(run.result.debs), 6)
        self.assertEqual(len(run.pool), 18)
        self.assertEqual(len(run.published), 12)
        for name, payload in run.published.items():
            self.assertEqual(run.pool[name], payload, msg=name)
        for arch in ARCHES:
            packages = run.output / "releases" / "incremental-6-3" / "dists" / "stable" / "main" / f"binary-{arch}" / "Packages"
            self.assertEqual(len(packages.read_text().strip().split("\n\n")), 9, msg=arch)


class FivePublishedFourAddedTest(unittest.TestCase):
    """5 published packages + four missing packages: 8 rebuilt, 10 kept."""

    ADDED = ("bottom", "neovim", "uv", "zoxide")

    def test_builds_exactly_the_four_additions_and_keeps_the_ten_published_debs(self) -> None:
        run = run_incremental(self, names=FIVE_PUBLISHED_NAMES, release_id="incremental-5-4")
        self.assertEqual(run.plan.added, self.ADDED)
        self.assertEqual(sorted(p.name for p in run.result.debs), _expected_built(self.ADDED))
        self.assertEqual(len(run.result.debs), 8)
        self.assertEqual(len(run.published), 10)
        self.assertEqual(len(run.pool), 18)
        for name, payload in run.published.items():
            self.assertEqual(run.pool[name], payload, msg=name)


if __name__ == "__main__":
    unittest.main()
