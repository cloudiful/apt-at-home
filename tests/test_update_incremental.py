"""Incremental catalog updates from two realistic baselines, proven by file identity.

The two fixtures are the ones the Neovim phase depends on. A two-package
published release that meets the full catalog adds four packages (2+4). A
five-package release that predates Neovim adds one package (5+1). Each run
rebuilds exactly the added packages, keeps every published .deb byte-identical,
and publishes a complete pool. Assertions name the files, so a count that happens
to match never passes on its own.
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
    PUBLISHED_NAMES,
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


class TwoPublishedFourAddedTest(unittest.TestCase):
    """2 published (starship, zellij) + 4 catalog additions: 8 rebuilt, 2 kept."""

    ADDED = ("git-delta", "neovim", "rustfs-cli", "sccache")

    def test_builds_exactly_the_four_additions_and_keeps_the_two_published(self) -> None:
        run = run_incremental(self, names=PUBLISHED_NAMES, release_id="incremental-2-4")
        self.assertEqual(run.plan.added, self.ADDED)
        self.assertEqual(sorted(p.name for p in run.result.debs), _expected_built(self.ADDED))
        self.assertEqual(len(run.result.debs), 8)
        self.assertEqual(len(run.pool), 12)
        self.assertEqual(len(run.published), 4)
        for name, payload in run.published.items():
            self.assertEqual(run.pool[name], payload, msg=name)
        for arch in ARCHES:
            packages = run.output / "releases" / "incremental-2-4" / "dists" / "stable" / "main" / f"binary-{arch}" / "Packages"
            self.assertEqual(len(packages.read_text().strip().split("\n\n")), 6, msg=arch)


class FivePublishedOneAddedTest(unittest.TestCase):
    """5 published (no neovim) + 1 catalog addition: 2 rebuilt, 10 kept."""

    def test_builds_exactly_neovim_and_keeps_the_ten_published_debs(self) -> None:
        run = run_incremental(self, names=FIVE_PUBLISHED_NAMES, release_id="incremental-5-1")
        self.assertEqual(run.plan.added, ("neovim",))
        self.assertEqual(
            sorted(p.name for p in run.result.debs),
            ["neovim_0.12.6-1_amd64.deb", "neovim_0.12.6-1_arm64.deb"],
        )
        self.assertEqual(len(run.published), 10)
        self.assertEqual(len(run.pool), 12)
        for name, payload in run.published.items():
            self.assertEqual(run.pool[name], payload, msg=name)


if __name__ == "__main__":
    unittest.main()
