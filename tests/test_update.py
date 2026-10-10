"""Update planning and the dry-run CLI contract.

Plans are compared against a validated baseline plus the committed catalog, so
the tests drive real manifest documents through the merge and overlay and assert
on versions, digests, members, and refusal messages rather than on internal
state.
"""

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.catalog import repo_of, tag_for
from scripts.manifest import default_manifest_path
from scripts.metadata import ARCHES
from scripts.release_manifest import validated_document
from scripts.update_plan import build_plan, merge_catalog
from scripts.upstream import Candidate
from scripts.upstream_assets import asset_name, license_path, license_url, release_download_url
from tests.release_fixtures import published_manifest, published_manifest_data


def committed_sha(package_name: str, arch: str) -> str:
    """The digest the committed manifest pins, so an unchanged candidate is real."""
    _, committed = validated_document(default_manifest_path())
    pkg = next(p for p in committed.packages if p.name == package_name)
    return pkg.targets[arch].sha256


def candidate(name: str, version: str, *, sha: str | None = None) -> Candidate:
    """Build a candidate whose URLs satisfy the same official-origin rules as discovery.

    By default each architecture keeps the digest the committed manifest already
    pins, which is what an unchanged upstream release looks like; pass `sha` to
    model a rebuilt archive.
    """
    repo = repo_of(name)
    tag = tag_for(name, version)
    return Candidate(
        name=name,
        repo=repo,
        tag=tag,
        version=version,
        license_url=license_url(repo, tag, license_path(name)),
        targets={
            arch: {
                "url": release_download_url(repo, tag, asset_name(name, version, arch)),
                "sha256": sha or committed_sha(name, arch),
            }
            for arch in ARCHES
        },
    )


def baseline_document() -> dict[str, object]:
    """The published baseline: the catalog subset the current release carries.

    The committed catalog is wider than what production has published, so the
    planning tests start from the released subset and let the merge add the rest.
    """
    return published_manifest_data()


def catalog_document() -> dict[str, object]:
    return validated_document(default_manifest_path())[0]


def candidate_pairs(data: dict[str, object]) -> dict[str, Candidate]:
    """One unchanged candidate per package in a manifest document."""
    return {
        str(entry["name"]): candidate(str(entry["name"]), str(entry["version"]))
        for entry in data["packages"]  # type: ignore[index]
    }


def _catalog_candidates(**overrides: str) -> dict[str, Candidate]:
    """One candidate per catalog package at its pinned version."""
    versions = {pkg.name: pkg.version for pkg in published_manifest().packages}
    _, committed = validated_document(default_manifest_path())
    versions.update({pkg.name: pkg.version for pkg in committed.packages})
    versions.update(overrides)
    return {name: candidate(name, version) for name, version in sorted(versions.items())}


class MergeCatalogTest(unittest.TestCase):
    def test_only_absent_packages_are_added(self) -> None:
        merged = merge_catalog(baseline_document(), catalog_document())
        self.assertEqual(
            [entry["name"] for entry in merged],
            ["zellij", "starship", "git-delta", "rustfs-cli", "sccache"],
        )

    def test_a_newer_published_version_survives_the_merge(self) -> None:
        baseline = baseline_document()
        for entry in baseline["packages"]:  # type: ignore[index]
            if entry["name"] == "starship":
                entry["version"] = "1.27.0"
        merged = merge_catalog(baseline, catalog_document())
        starship = next(e for e in merged if e["name"] == "starship")
        self.assertEqual(starship["version"], "1.27.0")
        self.assertEqual(len(merged), 5)

    def test_the_committed_catalog_never_drops_a_published_package(self) -> None:
        """A catalog that lost a name cannot delete what is already published."""
        trimmed = catalog_document()
        trimmed["packages"] = [  # type: ignore[index]
            e for e in trimmed["packages"]  # type: ignore[index]
            if e["name"] != "zellij"
        ]
        merged = merge_catalog(baseline_document(), trimmed)
        self.assertEqual(len(merged), 5)
        self.assertIn("zellij", [e["name"] for e in merged])


class PlanTest(unittest.TestCase):
    def test_unchanged_candidates_produce_an_empty_plan(self) -> None:
        plan = build_plan(
            baseline_release_id="20260102-030405",
            baseline_data=baseline_document(),
            catalog_data=baseline_document(),
            candidates=candidate_pairs(baseline_document()),
        )
        self.assertTrue(plan.is_noop)
        self.assertFalse(plan.is_bootstrap)
        self.assertEqual(plan.changed, ())

    def test_a_wider_catalog_adds_only_the_absent_packages(self) -> None:
        plan = build_plan(
            baseline_release_id="20260102-030405",
            baseline_data=baseline_document(),
            catalog_data=catalog_document(),
            candidates=_catalog_candidates(),
        )
        # Every package sits at its pinned version, but the three the release
        # does not carry still have to be built.
        self.assertEqual(plan.changed, ("git-delta", "rustfs-cli", "sccache"))
        self.assertEqual(plan.added, ("git-delta", "rustfs-cli", "sccache"))
        self.assertEqual(
            sorted(p.name for p in plan.manifest.packages),
            ["git-delta", "rustfs-cli", "sccache", "starship", "zellij"],
        )
        by_name = {p.name: p for p in plan.manifest.packages}
        # An added package keeps its catalog member and dependency profile.
        self.assertEqual(
            by_name["git-delta"].targets["arm64"].archive_member,
            "delta-0.20.1-aarch64-unknown-linux-gnu/delta",
        )
        self.assertEqual(
            by_name["git-delta"].targets["arm64"].depends,
            "libc6 (>= 2.18), libgcc-s1",
        )
        self.assertEqual(by_name["rustfs-cli"].targets["amd64"].binary, "rc")

    def test_one_changed_package_moves_only_its_own_fields(self) -> None:
        plan = build_plan(
            baseline_release_id="20260102-030405",
            baseline_data=baseline_document(),
            catalog_data=catalog_document(),
            candidates=_catalog_candidates(starship="1.27.0", sccache="0.19.0"),
        )
        self.assertEqual(plan.changed, ("git-delta", "rustfs-cli", "sccache", "starship"))
        self.assertEqual(plan.added, ("git-delta", "rustfs-cli", "sccache"))
        by_name = {p.name: p for p in plan.manifest.packages}
        starship = by_name["starship"]
        self.assertTrue(starship.license_url.endswith("/v1.27.0/LICENSE"))
        self.assertTrue(
            starship.targets["amd64"].url.endswith(
                "/v1.27.0/starship-x86_64-unknown-linux-musl.tar.gz"
            )
        )
        # A version bump recomputes the nested member instead of reusing it.
        self.assertEqual(
            by_name["sccache"].targets["amd64"].archive_member,
            "sccache-v0.19.0-x86_64-unknown-linux-musl/sccache",
        )
        self.assertEqual(
            by_name["sccache"].targets["arm64"].archive_member,
            "sccache-v0.19.0-aarch64-unknown-linux-musl/sccache",
        )
        # The unchanged package keeps its pinned digests byte for byte.
        zellij = by_name["zellij"]
        self.assertEqual(zellij.targets["arm64"].sha256, committed_sha("zellij", "arm64"))

    def test_a_bumped_delta_keeps_its_arm64_dependency(self) -> None:
        plan = build_plan(
            baseline_release_id="20260102-030405",
            baseline_data=catalog_document(),
            catalog_data=catalog_document(),
            candidates=_catalog_candidates(**{"git-delta": "0.21.0"}),
        )
        delta = next(p for p in plan.manifest.packages if p.name == "git-delta")
        self.assertEqual(delta.version, "0.21.0")
        self.assertEqual(
            delta.targets["arm64"].archive_member,
            "delta-0.21.0-aarch64-unknown-linux-gnu/delta",
        )
        self.assertEqual(delta.targets["arm64"].depends, "libc6 (>= 2.18), libgcc-s1")
        self.assertEqual(delta.targets["amd64"].depends, "")

    def test_a_published_version_newer_than_the_catalog_is_never_replaced(self) -> None:
        """The catalog pins starship 1.26.0; a published 1.27.0 must survive."""
        baseline = baseline_document()
        for entry in baseline["packages"]:  # type: ignore[index]
            if entry["name"] == "starship":
                entry["version"] = "1.27.0"
        plan = build_plan(
            baseline_release_id="20260102-030405",
            baseline_data=baseline,
            catalog_data=catalog_document(),
            candidates=_catalog_candidates(starship="1.27.0"),
        )
        starship = next(p for p in plan.manifest.packages if p.name == "starship")
        self.assertEqual(starship.version, "1.27.0")
        self.assertNotIn("starship", plan.changed)
        # Only the catalog additions remain to be built.
        self.assertEqual(plan.changed, ("git-delta", "rustfs-cli", "sccache"))

    def test_an_older_candidate_for_a_newer_published_version_is_refused(self) -> None:
        baseline = baseline_document()
        for entry in baseline["packages"]:  # type: ignore[index]
            if entry["name"] == "starship":
                entry["version"] = "1.27.0"
        with self.assertRaises(ValueError) as ctx:
            build_plan(
                baseline_release_id="20260102-030405",
                baseline_data=baseline,
                catalog_data=catalog_document(),
                candidates=_catalog_candidates(starship="1.26.0"),
            )
        self.assertIn("older than the published baseline", str(ctx.exception))

    def test_downgrade_and_missing_candidate_are_refused(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            build_plan(
                baseline_release_id="r",
                baseline_data=baseline_document(),
                catalog_data=catalog_document(),
                candidates=_catalog_candidates(starship="1.25.0"),
            )
        self.assertIn("older than the published baseline", str(ctx.exception))
        with self.assertRaises(ValueError):
            build_plan(
                baseline_release_id="r",
                baseline_data=baseline_document(),
                catalog_data=catalog_document(),
                candidates={},
            )

    def test_same_version_with_a_changed_digest_is_refused(self) -> None:
        """A republished tag is not an unchanged release: it must stop, not no-op."""
        versions = {pkg.name: pkg.version for pkg in published_manifest().packages}
        bumped = {
            n: candidate(n, v, sha="cd" * 32) for n, v in sorted(versions.items())
        }
        with self.assertRaises(ValueError) as ctx:
            build_plan(
                baseline_release_id="20260102-030405",
                baseline_data=baseline_document(),
                catalog_data=baseline_document(),
                candidates=bumped,
            )
        message = str(ctx.exception)
        self.assertIn("immutable", message)
        self.assertIn("amd64", message)

        # The published digest itself must stay a clean no-op.
        plan = build_plan(
            baseline_release_id="20260102-030405",
            baseline_data=baseline_document(),
            catalog_data=baseline_document(),
            candidates={n: candidate(n, v) for n, v in sorted(versions.items())},
        )
        self.assertTrue(plan.is_noop)

    def test_bootstrap_plan_is_marked_and_keeps_every_package(self) -> None:
        plan = build_plan(
            baseline_release_id=None,
            baseline_data=baseline_document(),
            catalog_data=catalog_document(),
            candidates=_catalog_candidates(starship="1.26.0", zellij="0.46.0"),
        )
        self.assertTrue(plan.is_bootstrap)
        self.assertIn("zellij", plan.changed)

    def test_unexpected_license_in_the_baseline_is_refused(self) -> None:
        data = baseline_document()
        for entry in data["packages"]:  # type: ignore[index]
            if entry["name"] == "starship":
                entry["license"] = "GPL-3.0"
        with self.assertRaises(ValueError) as ctx:
            build_plan(
                baseline_release_id="r",
                baseline_data=data,
                catalog_data=catalog_document(),
                candidates=_catalog_candidates(starship="1.27.0"),
            )
        self.assertIn("license", str(ctx.exception))


class CliTest(unittest.TestCase):
    def _run(self, argv: list[str], *, storage, candidates, fingerprint: str = "A" * 40) -> tuple[int, str]:
        from scripts import update as update_module

        out = io.StringIO()
        with mock.patch.object(update_module, "s3_source", return_value=storage), mock.patch.object(
            update_module, "_discover", return_value=candidates
        ), mock.patch.object(
            update_module, "_fingerprint", return_value=fingerprint
        ), mock.patch.dict(
            os.environ, {"APT_S3_BUCKET": ""}, clear=False
        ), contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            return update_module.main(argv), out.getvalue()

    def _storage(self, objects: dict[str, bytes]):
        from tests.release_fixtures import FakeStorage

        return FakeStorage(objects)

    def _published_storage(self, work: Path):
        """A real signed two-package release in an in-memory object store."""
        from tests.release_fixtures import build_release_objects
        from tests.helpers import make_gpg_home_with_key

        tmpdir, home, key_id = make_gpg_home_with_key()
        self.addCleanup(tmpdir.cleanup)
        storage = self._storage(
            build_release_objects(work / "published", key_home=home, key_id=key_id)
        )
        return storage, key_id

    def test_a_dry_run_plans_the_catalog_addition_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            storage, key_id = self._published_storage(root)
            code, output = self._run(
                ["--output", str(root / "out")],
                storage=storage, candidates=_catalog_candidates(), fingerprint=key_id,
            )
            self.assertEqual(code, 0, msg=output)
            self.assertIn("adding to the catalog: git-delta, rustfs-cli, sccache", output)
            self.assertIn("would rebuild: git-delta, rustfs-cli, sccache", output)
            self.assertIn("dry run: no key imported", output)
            self.assertFalse((root / "out").exists())
            self.assertEqual([key for key in storage.reads if key.startswith("pool/")], [])

    def test_discovery_that_misses_a_catalog_package_stops_the_run(self) -> None:
        """A partial discovery must never quietly drop a catalog package."""
        with tempfile.TemporaryDirectory() as work:
            storage, key_id = self._published_storage(Path(work))
            code, output = self._run(
                ["--output", str(Path(work) / "out")],
                storage=storage,
                candidates={k: v for k, v in _catalog_candidates().items()
                            if k in {"starship", "zellij"}},
                fingerprint=key_id,
            )
            self.assertEqual(code, 1)
            self.assertIn("no stable upstream release discovered for git-delta", output)

    def test_fresh_storage_bootstraps_from_the_committed_manifest(self) -> None:
        code, output = self._run(
            [], storage=self._storage({}), candidates=_catalog_candidates()
        )
        self.assertEqual(code, 0, msg=output)
        self.assertIn("committed manifest (fresh storage)", output)
        self.assertIn("nothing to publish", output)

    def test_publish_without_bucket_is_refused(self) -> None:
        code, output = self._run(["--publish"], storage=self._storage({}), candidates={})
        self.assertEqual(code, 1)
        self.assertIn("requires --bucket", output)

    def test_untrusted_baseline_is_refused(self) -> None:
        from tests.release_fixtures import DeniedStorage

        code, output = self._run([], storage=DeniedStorage({}), candidates={})
        self.assertEqual(code, 1)
        self.assertIn("update refused", output)

    def test_missing_fingerprint_is_refused(self) -> None:
        from scripts import update as update_module
        from tests.release_fixtures import FakeStorage

        with mock.patch.dict(os.environ, {"APT_SIGNING_FINGERPRINT": ""}, clear=False):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                update_module._fingerprint()
        self.assertEqual(self._run([], storage=FakeStorage({}), candidates={})[0], 1)


if __name__ == "__main__":
    unittest.main()