"""Incremental catalog updates from two realistic baselines, proven by file identity.

The six-package release adds five catalog packages (6+5). A five-package
release adds six packages (5+6). Each run rebuilds exactly the added packages,
keeps every published .deb byte-identical, and publishes a complete pool. Assertions name the files, so a count that happens to match never
passes on its own.
"""

import copy
import tempfile
import unittest
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType

from scripts.manifest import deb_filename, load_manifest, parse_manifest
from scripts.metadata import ARCHES
from scripts.release_verify import restore_metadata
from scripts.source_artifacts import load_source_artifacts
from scripts.source_build import (
    ArtifactProvenance,
    Lockfile,
    Toolchain,
    check_realized_document,
    realize_document,
)
from scripts.storage import MAX_DEB_BYTES
from scripts.tauri_source import source_policy
from scripts.update_build import UpdateResult, apply_update, remove_superseded
from scripts.update_plan import UpdatePlan, build_plan
from scripts.upstream import Candidate
from tests.helpers import FakeS3, make_gpg_home_with_key
from tests.release_fixtures import (
    FIVE_PUBLISHED_NAMES,
    SIX_PUBLISHED_NAMES,
    build_release_objects,
    published_manifest_data,
)
from tests.test_source_artifacts import artifact_directory, pinned, source_identity
from tests.test_source_build import ASSET_URL, VERSION, source_manifest_document
from tests.test_update import _catalog_candidates, candidate, catalog_document
from tests.test_update_build import Store, _export_private_key, _stub_archive

BUCKET = Store.bucket


def _with_reviewed_extra(document: dict) -> dict:
    """Return a source fixture as a valid production document.

    P1/P2 helpers predate the production recipe and carry no
    `extra_license_urls` and a placeholder `license_url`; production parsing
    requires the reviewed tag-bound dual-license URLs, so tests that exercise
    source-target validation add them here rather than weakening the validator.
    """
    document["packages"][0]["license_url"] = (  # type: ignore[index]
        "https://raw.githubusercontent.com/tauri-apps/tauri/tauri-cli-v2.12.2/LICENSE-MIT"
    )
    document["packages"][0]["extra_license_urls"] = [  # type: ignore[index]
        "https://raw.githubusercontent.com/tauri-apps/tauri/tauri-cli-v2.12.2/LICENSE-APACHE-2.0"
    ]
    return document


def _source_document(source=None) -> dict:
    return _with_reviewed_extra(source_manifest_document(source))


def _fixture_catalog() -> dict:
    """Full catalog with the fixture source identity for hermetic local builds.

    The committed manifest pins the real upstream lock; fixture artifacts carry
    the fixture lock so they stay hermetic. Using the fixture identity for both
    catalog and candidates still proves the eleven-package shape while letting
    the fixture artifact match without network. The real pin stays covered by
    `test_cargo_tauri_recipe`.
    """
    import copy as _copy

    data = _copy.deepcopy(catalog_document())
    from tests.test_source_artifacts import source_identity as _fixture_identity
    from scripts.source_build import document as _document_of

    fixture_doc = _document_of(_fixture_identity())
    for entry in data["packages"]:  # type: ignore[index]
        if entry["name"] == "cargo-tauri":  # type: ignore[index]
            entry["targets"]["arm64"]["source_build"] = fixture_doc  # type: ignore[index]
    return data


def _fixture_candidates() -> dict:
    """Candidates matching `_fixture_catalog`, with the fixture source identity."""
    import copy as _copy
    from types import MappingProxyType as _MP

    from tests.test_source_artifacts import source_identity as _fixture_identity

    candidates = _catalog_candidates()
    cargo = candidates["cargo-tauri"]
    candidates["cargo-tauri"] = cargo.__class__(
        name=cargo.name,
        repo=cargo.repo,
        tag=cargo.tag,
        version=cargo.version,
        license_url=cargo.license_url,
        targets=cargo.targets,
        extra_license_urls=cargo.extra_license_urls,
        sources=_MP({"arm64": _fixture_identity()}),
    )
    return candidates


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
    from scripts.source_artifacts import index_source_artifacts, load_source_artifacts as _load_artifacts
    from tests.test_source_artifacts import artifact_directory as _artifact_dir, pinned as _pinned

    _artifacts_dir, _identity = _artifact_dir(root / "source-artifacts")
    _artifacts = index_source_artifacts(
        [_load_artifacts(_artifacts_dir, expected=_pinned(_identity))]
    )
    with _stub_archive():
        result = apply_update(
            plan=plan, release=restored, source=store, output_root=output,
            cache_dir=output.parent / "cache" / "upstream", release_id=release_id,
            bucket=BUCKET, fingerprint=key_id, signing_key_file=key_file,
            key_id=key_id, client=store.client, source_artifacts=_artifacts,
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


class SixPublishedFourAddedTest(unittest.TestCase):
    """Six published packages plus five catalog additions."""

    ADDED = ("bottom", "cargo-tauri", "kandev", "uv", "zoxide")

    def test_builds_exactly_the_four_additions_and_keeps_the_six_published(self) -> None:
        run = run_incremental(self, names=SIX_PUBLISHED_NAMES, release_id="incremental-6-4")
        self.assertEqual(run.plan.added, self.ADDED)
        self.assertEqual(sorted(p.name for p in run.result.debs), _expected_built(self.ADDED))
        self.assertEqual(len(run.result.debs), 10)
        self.assertEqual(len(run.pool), 22)
        self.assertEqual(len(run.published), 12)
        for name, payload in run.published.items():
            self.assertEqual(run.pool[name], payload, msg=name)
        for arch in ARCHES:
            packages = run.output / "releases" / "incremental-6-4" / "dists" / "stable" / "main" / f"binary-{arch}" / "Packages"
            self.assertEqual(len(packages.read_text().strip().split("\n\n")), 11, msg=arch)


class FivePublishedFiveAddedTest(unittest.TestCase):
    """Five published packages plus the six missing catalog packages."""

    ADDED = ("bottom", "cargo-tauri", "kandev", "neovim", "uv", "zoxide")

    def test_builds_exactly_the_five_additions_and_keeps_the_ten_published_debs(self) -> None:
        run = run_incremental(self, names=FIVE_PUBLISHED_NAMES, release_id="incremental-5-5")
        self.assertEqual(run.plan.added, self.ADDED)
        self.assertEqual(sorted(p.name for p in run.result.debs), _expected_built(self.ADDED))
        self.assertEqual(len(run.result.debs), 12)
        self.assertEqual(len(run.published), 10)
        self.assertEqual(len(run.pool), 22)
        for name, payload in run.published.items():
            self.assertEqual(run.pool[name], payload, msg=name)


class SourceBuiltPackagePlanTest(unittest.TestCase):
    """A source-built target is planned, compared, and restored like any other.

    The interesting cases are the ones that keep an unchanged version cheap and a
    moved one fatal: an unchanged source version must need no new compilation, and
    a version whose commit, lockfile, compiler, or command moved must stop the run
    instead of replacing what clients already installed.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.directory, self.identity = artifact_directory(self.root)
        self.artifacts = load_source_artifacts(self.directory, expected=pinned(self.identity))
        self.realized = realize_document(
            _source_document(self.identity.document()),
            {("cargo-tauri", "arm64"): self.artifacts.artifact},
        )["packages"][0]

    def candidates(self, identity=None) -> dict:
        from scripts.upstream_assets import license_urls_for as _license_urls

        identity = identity or self.identity
        tag = identity.tag
        urls = _license_urls("cargo-tauri", tag)
        return {
            "starship": candidate("starship", "1.26.0"),
            "zellij": candidate("zellij", "0.45.1"),
            "cargo-tauri": Candidate(
                name="cargo-tauri",
                repo="tauri-apps/tauri",
                tag=tag,
                version=VERSION,
                license_url=urls[0],
                # The amd64 architecture still comes from the official release
                # asset; only arm64 has no asset to name.
                targets={"amd64": {"url": ASSET_URL, "sha256": "a" * 64}},
                extra_license_urls=urls[1:],
                sources=MappingProxyType({"arm64": identity}),
            ),
        }

    def baseline(self) -> dict[str, object]:
        data = copy.deepcopy(published_manifest_data())
        data["packages"].append(copy.deepcopy(self.realized))  # type: ignore[union-attr]
        return data

    def test_an_unchanged_source_version_needs_no_new_compilation(self) -> None:
        baseline = self.baseline()
        plan = build_plan(
            baseline_release_id="r1",
            baseline_data=baseline,
            catalog_data=copy.deepcopy(baseline),
            candidates=self.candidates(),
        )
        self.assertTrue(plan.is_noop)
        self.assertEqual(plan.changed, ())
        # The realized artifact survives planning untouched, so the published deb
        # is restored rather than rebuilt.
        entry = next(e for e in plan.manifest_data["packages"] if e["name"] == "cargo-tauri")
        self.assertEqual(
            entry["targets"]["arm64"]["source_build"]["artifact"],
            self.realized["targets"]["arm64"]["source_build"]["artifact"],
        )
        check_realized_document(plan.manifest_data)

    def test_a_new_source_version_is_planned_and_marked_changed(self) -> None:
        catalog = copy.deepcopy(published_manifest_data())
        catalog["packages"].append(copy.deepcopy(self.realized))  # type: ignore[union-attr]
        plan = build_plan(
            baseline_release_id="r1",
            baseline_data=published_manifest_data(),
            catalog_data=catalog,
            candidates=self.candidates(),
        )
        self.assertEqual(plan.added, ("cargo-tauri",))
        self.assertIn("cargo-tauri", plan.changed)
        check_realized_document(plan.manifest_data)

    def test_a_same_version_source_mutation_is_refused(self) -> None:
        cases = {
            "commit": {"commit": "a" * 40},
            "lockfile": {"lockfile": Lockfile("Cargo.lock", "b" * 40, 10, "c" * 64)},
            "compiler": {"toolchain": Toolchain("1.98.0")},
            "command": {"build": source_policy(profile="release")},
        }
        for label, change in cases.items():
            with self.subTest(label):
                baseline = self.baseline()
                candidate = self.candidates(replace(self.identity, **change))
                with self.assertRaisesRegex(ValueError, "a published release is immutable"):
                    build_plan(
                        baseline_release_id="r1",
                        baseline_data=baseline,
                        catalog_data=copy.deepcopy(baseline),
                        candidates=candidate,
                    )

    def test_a_version_that_starts_being_a_source_target_is_refused(self) -> None:
        baseline = copy.deepcopy(published_manifest_data())
        entry = copy.deepcopy(_source_document(self.identity.document())["packages"][0])
        entry["targets"]["arm64"] = {  # type: ignore[index]
            "url": ASSET_URL,
            "sha256": "a" * 64,
            "archive_member": "cargo-tauri",
            "binary": "cargo-tauri",
        }
        baseline["packages"].append(entry)  # type: ignore[union-attr]
        # Upstream publishes no arm64 asset, so an asset target for it is
        # refused while parsing — a version cannot start being a source target
        # from an asset one because the asset one was never valid.
        with self.assertRaisesRegex(ValueError, "publishes no arm64 release asset"):
            build_plan(
                baseline_release_id="r1",
                baseline_data=baseline,
                catalog_data=copy.deepcopy(baseline),
                candidates=self.candidates(),
            )

    def test_a_version_with_nothing_discovered_is_refused(self) -> None:
        baseline = self.baseline()
        candidates = self.candidates()
        candidates["cargo-tauri"] = replace(
            candidates["cargo-tauri"], sources=MappingProxyType({})
        )
        with self.assertRaisesRegex(ValueError, "has no discovered upstream release asset"):
            build_plan(
                baseline_release_id="r1",
                baseline_data=baseline,
                catalog_data=copy.deepcopy(baseline),
                candidates=candidates,
            )


class SupersededSourceDebTest(unittest.TestCase):
    """An unchanged source-built package keeps its pool file; a changed one does not."""

    def _plan(self, changed: tuple[str, ...]):
        document = realize_document(
            _source_document(source_identity().document()),
            {("cargo-tauri", "arm64"): ArtifactProvenance("b" * 64, 4096, 183)},
        )
        manifest = parse_manifest(document)
        package = manifest.packages[0]
        pool = Path(self.enterContext(tempfile.TemporaryDirectory())) / "pool"
        pool.mkdir()
        for name in (deb_filename(package, "amd64", manifest),
                     deb_filename(package, "arm64", manifest)):
            (pool / name).write_bytes(b"published")
        plan = UpdatePlan(
            baseline_release_id="r1",
            baseline_manifest=manifest,
            manifest_data=document,
            manifest=manifest,
            changed=changed,
            versions={"cargo-tauri": VERSION},
        )
        return plan, pool

    def test_an_unchanged_source_package_keeps_both_published_debs(self) -> None:
        plan, pool = self._plan(())
        self.assertEqual(remove_superseded(plan, pool), [])
        self.assertEqual(len(list(pool.glob("*.deb"))), 2)

    def test_a_rebuilt_source_package_drops_its_published_debs(self) -> None:
        plan, pool = self._plan(("cargo-tauri",))
        self.assertEqual(
            sorted(remove_superseded(plan, pool)),
            ["cargo-tauri_2.12.2-1_amd64.deb", "cargo-tauri_2.12.2-1_arm64.deb"],
        )
        self.assertEqual(list(pool.glob("*.deb")), [])


if __name__ == "__main__":
    unittest.main()
