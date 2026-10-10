"""Update planning and the dry-run CLI contract.

Plans are compared against a validated baseline, so the tests drive real
manifest documents through the overlay and assert on the resulting versions,
digests, and refusal messages rather than on internal state.
"""

import contextlib
import copy
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.manifest import default_manifest_path
from scripts.metadata import ARCHES
from scripts.release_manifest import load_manifest_data, validated_document
from scripts.update_plan import build_plan
from scripts.upstream import Candidate
from scripts.upstream_assets import (
    LICENSE_PATHS,
    TRUSTED_REPOS,
    asset_name,
    license_url,
    release_download_url,
)


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
    repo = TRUSTED_REPOS[name]
    tag = f"v{version}"
    return Candidate(
        name=name,
        repo=repo,
        tag=tag,
        version=version,
        license_url=license_url(repo, tag, LICENSE_PATHS[name]),
        targets={
            arch: {
                "url": release_download_url(repo, tag, asset_name(name, arch)),
                "sha256": sha or committed_sha(name, arch),
            }
            for arch in ARCHES
        },
    )


def baseline_document() -> dict[str, object]:
    return copy.deepcopy(load_manifest_data(default_manifest_path()))


class PlanTest(unittest.TestCase):
    def test_unchanged_candidates_produce_an_empty_plan(self) -> None:
        data = baseline_document()
        _, committed = validated_document(default_manifest_path())
        versions = {pkg.name: pkg.version for pkg in committed.packages}
        plan = build_plan(
            baseline_release_id="20260102-030405",
            baseline_data=data,
            candidates={name: candidate(name, version) for name, version in versions.items()},
        )
        self.assertTrue(plan.is_noop)
        self.assertFalse(plan.is_bootstrap)
        self.assertEqual(plan.changed, ())

    def test_one_changed_package_moves_only_its_own_fields(self) -> None:
        data = baseline_document()
        plan = build_plan(
            baseline_release_id="20260102-030405",
            baseline_data=data,
            candidates={
                "starship": candidate("starship", "1.27.0", sha="cd" * 32),
                "zellij": candidate("zellij", "0.45.1"),
            },
        )
        self.assertEqual(plan.changed, ("starship",))
        updated = {p.name: p.version for p in plan.manifest.packages}
        self.assertEqual(updated, {"starship": "1.27.0", "zellij": "0.45.1"})
        starship = next(p for p in plan.manifest.packages if p.name == "starship")
        self.assertTrue(starship.license_url.endswith("/v1.27.0/LICENSE"))
        self.assertEqual(
            starship.targets["amd64"].url,
            "https://github.com/starship/starship/releases/download/v1.27.0/"
            "starship-x86_64-unknown-linux-musl.tar.gz",
        )
        # The unchanged package keeps its pinned digests byte for byte.
        zellij = next(p for p in plan.manifest.packages if p.name == "zellij")
        original = {p.name: p for p in validated_document(default_manifest_path())[1].packages}
        self.assertEqual(zellij.targets["arm64"].sha256, original["zellij"].targets["arm64"].sha256)
        self.assertEqual(zellij.targets["arm64"].archive_member, original["zellij"].targets["arm64"].archive_member)

    def test_downgrade_and_missing_candidate_are_refused(self) -> None:
        data = baseline_document()
        with self.assertRaises(ValueError) as ctx:
            build_plan(
                baseline_release_id="r",
                baseline_data=data,
                candidates={
                    "starship": candidate("starship", "1.25.0"),
                    "zellij": candidate("zellij", "0.45.1"),
                },
            )
        self.assertIn("older than the published baseline", str(ctx.exception))
        with self.assertRaises(ValueError):
            build_plan(baseline_release_id="r", baseline_data=data, candidates={})
        with self.assertRaises(ValueError):
            build_plan(
                baseline_release_id="r",
                baseline_data=data,
                candidates={"starship": candidate("starship", "1.27.0")},
            )

    def test_same_version_with_a_changed_digest_is_refused(self) -> None:
        """A republished tag is not an unchanged release: it must stop, not no-op."""
        data = baseline_document()
        _, committed = validated_document(default_manifest_path())
        versions = {p.name: p.version for p in committed.packages}
        with self.assertRaises(ValueError) as ctx:
            build_plan(
                baseline_release_id="20260102-030405",
                baseline_data=data,
                candidates={n: candidate(n, v, sha="cd" * 32) for n, v in versions.items()},
            )
        message = str(ctx.exception)
        self.assertIn("immutable", message)
        self.assertIn("amd64", message)

        # The published digest itself must stay a clean no-op.
        plan = build_plan(
            baseline_release_id="20260102-030405",
            baseline_data=data,
            candidates={n: candidate(n, v) for n, v in versions.items()},
        )
        self.assertTrue(plan.is_noop)

    def test_bootstrap_plan_is_marked_and_keeps_every_package(self) -> None:
        data = baseline_document()
        plan = build_plan(
            baseline_release_id=None,
            baseline_data=data,
            candidates={
                "starship": candidate("starship", "1.26.0"),
                "zellij": candidate("zellij", "0.46.0"),
            },
        )
        self.assertTrue(plan.is_bootstrap)
        self.assertEqual(plan.changed, ("zellij",))

    def test_unexpected_license_in_the_baseline_is_refused(self) -> None:
        data = baseline_document()
        for entry in data["packages"]:  # type: ignore[index]
            if entry["name"] == "starship":
                entry["license"] = "GPL-3.0"
        with self.assertRaises(ValueError) as ctx:
            build_plan(
                baseline_release_id="r",
                baseline_data=data,
                candidates={
                    "starship": candidate("starship", "1.27.0"),
                    "zellij": candidate("zellij", "0.46.0"),
                },
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

    def test_dry_run_is_the_default_and_publishes_nothing(self) -> None:
        from tests.release_fixtures import build_release_objects
        from tests.helpers import make_gpg_home_with_key

        tmpdir, home, key_id = make_gpg_home_with_key()
        try:
            with tempfile.TemporaryDirectory() as work:
                objects = build_release_objects(Path(work), key_home=home, key_id=key_id)
                storage = self._storage(objects)
                _, committed = validated_document(default_manifest_path())
                current = {pkg.name: candidate(pkg.name, pkg.version) for pkg in committed.packages}
                code, output = self._run(
                    ["--output", str(Path(work) / "out")],
                    storage=storage, candidates=current, fingerprint=key_id,
                )
                self.assertEqual(code, 0, msg=output)
                self.assertIn("no updates", output)
                self.assertIn("nothing to publish", output)
                self.assertFalse((Path(work) / "out").exists())
                self.assertEqual([key for key in storage.reads if key.startswith("pool/")], [])
        finally:
            tmpdir.cleanup()

    def test_fresh_storage_bootstraps_from_the_committed_manifest(self) -> None:
        _, committed = validated_document(default_manifest_path())
        current = {pkg.name: candidate(pkg.name, pkg.version) for pkg in committed.packages}
        code, output = self._run([], storage=self._storage({}), candidates=current)
        self.assertEqual(code, 0, msg=output)
        self.assertIn("committed manifest (fresh storage)", output)

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
