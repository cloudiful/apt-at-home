"""Update overlays retain every reviewed uv command and license source."""

import contextlib
import copy
import io
import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from scripts.manifest import deb_filename
from scripts.release_verify import restore_metadata
from scripts.smoke_package import check_copyright, check_package
from scripts.storage import MAX_DEB_BYTES
from scripts.upstream_assets import license_urls_for
from scripts.update_build import UpdateResult, apply_update
from scripts.update_plan import build_plan
from tests.helpers import FakeS3, make_gpg_home_with_key
from tests.release_fixtures import CATALOG_NAMES, build_release_objects
from tests.test_update import (
    _catalog_candidates,
    baseline_document,
    candidate,
    catalog_document,
)
from tests.test_update_build import Store, _export_private_key, _stub_archive


class MetadataOnlyReportTest(unittest.TestCase):
    """A date-only plan is reported as metadata without claiming a rebuild."""

    def _report_text(self, plan) -> str:
        import contextlib
        import io

        from scripts import update as update_module

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            update_module._report(plan)
        return out.getvalue()

    def test_a_metadata_only_plan_names_metadata_not_a_rebuild(self) -> None:
        from scripts.manifest import default_manifest_path
        from scripts.release_manifest import load_manifest_data
        from scripts.update_plan import build_plan

        baseline = load_manifest_data(default_manifest_path())
        for entry in baseline["packages"]:  # type: ignore[index]
            entry.pop("upstream_published_at", None)
        plan = build_plan(
            baseline_release_id="published",
            baseline_data=baseline,
            catalog_data=catalog_document(),
            candidates=_catalog_candidates(),
        )
        self.assertEqual(plan.changed, ())
        self.assertTrue(plan.metadata_changed)

        text = self._report_text(plan)
        self.assertIn("metadata only: ", text)
        self.assertNotIn("would rebuild:", text)
        self.assertNotIn("no updates", text)

    def test_an_unchanged_plan_still_reports_no_updates(self) -> None:
        from scripts.update_plan import build_plan

        plan = build_plan(
            baseline_release_id="published",
            baseline_data=catalog_document(),
            catalog_data=catalog_document(),
            candidates=_catalog_candidates(),
        )
        self.assertTrue(plan.is_noop)
        text = self._report_text(plan)
        self.assertIn("no updates", text)
        self.assertNotIn("would rebuild:", text)
        self.assertNotIn("metadata only: ", text)


class UvUpdateOverlayTest(unittest.TestCase):
    def test_a_uv_upgrade_preserves_uvx_and_both_license_urls(self) -> None:
        manifest = catalog_document()
        candidates = _catalog_candidates(uv="0.14.0")

        plan = build_plan(
            baseline_release_id="published",
            baseline_data=manifest,
            catalog_data=manifest,
            candidates=candidates,
        )

        uv = next(pkg for pkg in plan.manifest.packages if pkg.name == "uv")
        target = uv.targets["arm64"]
        self.assertEqual(plan.changed, ("uv",))
        self.assertEqual(target.archive_member, "uv-aarch64-unknown-linux-gnu/uv")
        self.assertEqual(
            dict(target.additional_binaries),
            {"uv-aarch64-unknown-linux-gnu/uvx": "uvx"},
        )
        self.assertEqual(uv.extra_license_urls, license_urls_for("uv", "0.14.0")[1:])

    def test_uv_upgrade_rebuilds_two_commands_then_plans_a_zero_write_noop(self) -> None:
        tmpdir, home, key_id = make_gpg_home_with_key()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                store = Store(FakeS3())
                # Focused uv overlay: exclude the source-built package so this
                # asset-only upgrade stays hermetic without a producer artifact.
                # Full-catalog source builds are covered by the incremental and
                # update-build suites with fixture artifacts.
                uv_names = tuple(name for name in CATALOG_NAMES if name not in ("kandev", "cargo-tauri"))
                store.seed(build_release_objects(
                    root / "published", key_home=home, key_id=key_id, names=uv_names
                ))
                restored = restore_metadata(store, fingerprint=key_id)
                published_pool = {
                    path.name: store.get(f"pool/{path.name}", max_bytes=MAX_DEB_BYTES)
                    for path in (root / "published" / "pool").glob("*.deb")
                }
                uv_catalog = catalog_document()
                uv_catalog["packages"] = [
                    entry for entry in uv_catalog["packages"] if entry["name"] not in ("kandev", "cargo-tauri")
                ]
                uv_candidates = {
                    name: item for name, item in _catalog_candidates(uv="0.14.0").items()
                    if name not in ("kandev", "cargo-tauri")
                }
                plan = build_plan(
                    baseline_release_id=restored.release_id,
                    baseline_data=restored.manifest_data,
                    catalog_data=uv_catalog,
                    candidates=uv_candidates,
                )
                self.assertEqual(plan.changed, ("uv",))
                key_file = _export_private_key(home, root / "test-private.asc")
                output = root / "out"
                with _stub_archive():
                    result = apply_update(
                        plan=plan, release=restored, source=store, output_root=output,
                        cache_dir=output.parent / "cache" / "upstream",
                        release_id="uv-upgrade", bucket=Store.bucket,
                        fingerprint=key_id, signing_key_file=key_file, key_id=key_id,
                        client=store.client,
                    )

                self.assertEqual(
                    sorted(path.name for path in result.debs),
                    ["uv_0.14.0-1_amd64.deb", "uv_0.14.0-1_arm64.deb"],
                )
                pool = {path.name: path.read_bytes() for path in (output / "pool").glob("*.deb")}
                expected_pool = {
                    deb_filename(pkg, arch, plan.manifest)
                    for pkg in plan.manifest.packages
                    if pkg.name in uv_names
                    for arch in pkg.targets
                }
                self.assertEqual(set(pool), expected_pool)
                self.assertNotIn("uv_0.13.0-1_amd64.deb", pool)
                for name, payload in published_pool.items():
                    if not name.startswith("uv_"):
                        self.assertEqual(pool[name], payload, msg=name)

                uv = next(pkg for pkg in plan.manifest.packages if pkg.name == "uv")
                for arch in ("amd64", "arm64"):
                    check_package(uv, arch, manifest=plan.manifest, pool=output / "pool")
                    extracted_root = root / f"uv-upgraded-{arch}"
                    proc = subprocess.run(
                        ["dpkg-deb", "-x", str(output / "pool" / deb_filename(uv, arch, plan.manifest)), str(extracted_root)],
                        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                    )
                    self.assertEqual(proc.returncode, 0, msg=proc.stderr)
                    check_copyright(uv, extracted_root)

                writes = list(store.client.puts)
                follow_up = restore_metadata(store, fingerprint=key_id)
                noop = build_plan(
                    baseline_release_id=follow_up.release_id,
                    baseline_data=follow_up.manifest_data,
                    catalog_data=uv_catalog,
                    candidates=uv_candidates,
                )
                self.assertTrue(noop.is_noop)
                self.assertEqual(store.client.puts, writes)
        finally:
            tmpdir.cleanup()

    def test_same_version_supplemental_provenance_cannot_be_dropped(self) -> None:
        manifest = catalog_document()
        candidates = _catalog_candidates()
        candidates["uv"] = replace(candidate("uv", "0.13.0"), extra_license_urls=())

        with self.assertRaisesRegex(ValueError, "supplemental license sources changed"):
            build_plan(
                baseline_release_id="published",
                baseline_data=manifest,
                catalog_data=manifest,
                candidates=candidates,
            )


class _Baseline:
    def __init__(self, data: dict[str, object]) -> None:
        self.release_id = "published"
        self.manifest_data = data


class DiagnosticsCliTest(unittest.TestCase):
    """The CLI flags and the deferred-partial report contract."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.report = self.root / "update-diagnostics.json"

    def _run(
        self,
        argv: list[str],
        *,
        baseline_data: dict[str, object] | None = None,
        candidates: dict,
        blocked: dict[str, str] | None = None,
        apply_result: UpdateResult | None = None,
        apply_error: Exception | None = None,
        published: bool = True,
        load_error: Exception | None = None,
        source_error: Exception | None = None,
        discover_error: Exception | None = None,
    ):
        from scripts import update as update_module

        data = copy.deepcopy(baseline_data if baseline_data is not None else catalog_document())
        restored = _Baseline(data) if published else None
        applied = mock.Mock(
            return_value=apply_result
            if apply_result is not None
            else UpdateResult(release_id="release-1", debs=[], uploaded=[])
        )
        if apply_error is not None:
            applied.side_effect = apply_error
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            if source_error is not None:
                stack.enter_context(
                    mock.patch.object(update_module, "s3_source", side_effect=source_error)
                )
            else:
                stack.enter_context(
                    mock.patch.object(update_module, "s3_source", return_value=object())
                )
            if discover_error is not None:
                stack.enter_context(
                    mock.patch.object(
                        update_module, "_discover", side_effect=discover_error
                    )
                )
            else:
                stack.enter_context(
                    mock.patch.object(
                        update_module, "_discover", return_value=(candidates, blocked or {})
                    )
                )
            stack.enter_context(
                mock.patch.object(update_module, "_fingerprint", return_value="A" * 40)
            )
            if load_error is not None:
                stack.enter_context(
                    mock.patch.object(
                        update_module, "_load_baseline", side_effect=load_error
                    )
                )
            else:
                stack.enter_context(
                    mock.patch.object(
                        update_module,
                        "_load_baseline",
                        return_value=(restored, copy.deepcopy(data)),
                    )
                )
            stack.enter_context(
                mock.patch.object(update_module, "apply_update", applied)
            )
            stack.enter_context(
                mock.patch.dict(os.environ, {"APT_S3_BUCKET": ""}, clear=False)
            )
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(out))
            code = update_module.main(argv)
        return code, out.getvalue(), applied

    def _report(self) -> dict[str, object]:
        return json.loads(self.report.read_text(encoding="utf-8"))

    def test_a_strict_run_writes_the_fatal_report_and_fails(self) -> None:
        candidates = _catalog_candidates()
        del candidates["zellij"]
        code, output, applied = self._run(
            ["--diagnostics", str(self.report)],
            candidates=candidates,
            blocked={"zellij": "license bytes changed"},
        )
        self.assertEqual(code, 1, msg=output)
        applied.assert_not_called()
        report = self._report()
        self.assertIsNotNone(report["fatal"])
        self.assertFalse(report["published"])
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["deferred"], [])
        # The baseline was restored before planning failed: keep its identity.
        self.assertEqual(report["baseline_release_id"], "published")

    def test_a_fatal_discovery_error_keeps_the_restored_baseline_identity(self) -> None:
        from scripts.upstream import HttpError

        code, output, applied = self._run(
            ["--diagnostics", str(self.report)],
            candidates=_catalog_candidates(),
            discover_error=HttpError("network failed"),
        )
        self.assertEqual(code, 1, msg=output)
        applied.assert_not_called()
        report = self._report()
        self.assertEqual(report["baseline_release_id"], "published")
        self.assertIn("network failed", report["fatal"])

    def test_keep_blocked_retains_the_published_package_and_writes_the_report(self) -> None:
        candidates = _catalog_candidates()
        del candidates["zellij"]
        code, output, applied = self._run(
            ["--keep-blocked", "--diagnostics", str(self.report)],
            baseline_data=baseline_document(),
            candidates=candidates,
            blocked={"zellij": "license bytes changed"},
        )
        self.assertEqual(code, 0, msg=output)
        applied.assert_not_called()
        self.assertIn("deferred: zellij (discovery)", output)
        report = self._report()
        self.assertIsNone(report["fatal"])
        self.assertFalse(report["published"])
        self.assertTrue(report["dry_run"])
        self.assertEqual(
            [(item["package"], item["stage"]) for item in report["deferred"]],
            [("zellij", "discovery")],
        )
        self.assertIn("rustfs-cli", report["changed"])
        self.assertNotIn("zellij", report["changed"])

    def test_publish_success_is_the_only_confirmed_publication(self) -> None:
        key = self.root / "key.asc"
        key.write_text("key", encoding="utf-8")
        candidates = _catalog_candidates()
        del candidates["zellij"]
        code, output, applied = self._run(
            [
                "--keep-blocked", "--publish", "--bucket", "test-bucket",
                "--signing-key-file", str(key), "--diagnostics", str(self.report),
            ],
            baseline_data=baseline_document(),
            candidates=candidates,
            blocked={"zellij": "license bytes changed"},
        )
        self.assertEqual(code, 0, msg=output)
        applied.assert_called_once()
        self.assertEqual(
            applied.call_args.kwargs["plan"].deferred[0].name, "zellij"
        )
        report = self._report()
        self.assertTrue(report["published"])
        self.assertFalse(report["dry_run"])
        self.assertIsNotNone(report["planned_release_id"])
        self.assertEqual(report["confirmed_release_id"], "release-1")

    def test_a_failed_publish_never_claims_a_confirmed_release(self) -> None:
        key = self.root / "key.asc"
        key.write_text("key", encoding="utf-8")
        code, output, _ = self._run(
            [
                "--publish", "--bucket", "test-bucket",
                "--signing-key-file", str(key), "--diagnostics", str(self.report),
            ],
            baseline_data=baseline_document(),
            candidates=_catalog_candidates(),
            apply_error=RuntimeError("upload denied"),
        )
        self.assertEqual(code, 1, msg=output)
        report = self._report()
        self.assertFalse(report["published"])
        self.assertIsNone(report["confirmed_release_id"])
        self.assertIsNotNone(report["planned_release_id"])
        self.assertIn("upload denied", report["fatal"])

    def test_only_deferred_packages_never_republish(self) -> None:
        key = self.root / "key.asc"
        key.write_text("key", encoding="utf-8")
        candidates = _catalog_candidates()
        del candidates["zellij"]
        code, output, applied = self._run(
            [
                "--keep-blocked", "--publish", "--bucket", "test-bucket",
                "--signing-key-file", str(key), "--diagnostics", str(self.report),
            ],
            candidates=candidates,
            blocked={"zellij": "license bytes changed"},
        )
        self.assertEqual(code, 0, msg=output)
        applied.assert_not_called()
        report = self._report()
        self.assertFalse(report["published"])
        self.assertEqual(report["changed"], [])
        self.assertEqual(len(report["deferred"]), 1)

    def test_a_fresh_storage_bootstrap_never_defers_a_blocked_package(self) -> None:
        candidates = _catalog_candidates()
        del candidates["zellij"]
        code, output, applied = self._run(
            ["--keep-blocked", "--diagnostics", str(self.report)],
            published=False,
            candidates=candidates,
            blocked={"zellij": "license bytes changed"},
        )
        self.assertEqual(code, 1, msg=output)
        applied.assert_not_called()
        report = self._report()
        self.assertIsNotNone(report["fatal"])
        self.assertEqual(report["deferred"], [])

    def test_help_states_the_diagnostics_write_and_two_step_verdict(self) -> None:
        from scripts import update as update_module

        help_text = " ".join(update_module.build_parser().format_help().split())
        self.assertIn("except the report given explicitly to --diagnostics", help_text)
        self.assertIn("--check-deferred gate", help_text)

    def test_a_credential_url_never_reaches_stderr_or_the_report(self) -> None:
        key = self.root / "key.asc"
        key.write_text("key", encoding="utf-8")
        error = RuntimeError(
            "upload failed for https://user:pass@example.invalid/object"
            "?X-Amz-Signature=example-secret#frag"
        )
        code, output, _ = self._run(
            [
                "--publish", "--bucket", "test-bucket",
                "--signing-key-file", str(key), "--diagnostics", str(self.report),
            ],
            baseline_data=baseline_document(),
            candidates=_catalog_candidates(),
            apply_error=error,
        )
        self.assertEqual(code, 1, msg=output)
        self.assertNotIn("example-secret", output)
        self.assertNotIn("user:pass", output)
        self.assertNotIn("example-secret", self.report.read_text(encoding="utf-8"))

    def test_an_s3_client_error_writes_a_sanitized_fatal_report(self) -> None:
        from botocore.exceptions import ClientError

        error = ClientError(
            {
                "Error": {
                    "Code": "AccessDenied",
                    "Message": (
                        "denied for https://user:pass@example.invalid/o"
                        "?token=example-secret"
                    ),
                }
            },
            "GetObject",
        )
        code, output, applied = self._run(
            ["--diagnostics", str(self.report)],
            candidates=_catalog_candidates(),
            load_error=error,
        )
        self.assertEqual(code, 1, msg=output)
        applied.assert_not_called()
        report = self._report()
        self.assertIsNotNone(report["fatal"])
        self.assertIn("AccessDenied", report["fatal"])
        # The failure happened before any baseline was restored.
        self.assertIsNone(report["baseline_release_id"])
        self.assertNotIn("example-secret", report["fatal"])
        text = self.report.read_text(encoding="utf-8")
        self.assertNotIn("example-secret", text)
        self.assertNotIn("user:pass", text)
        self.assertNotIn("example-secret", output)

    def test_a_missing_s3_credential_writes_a_fatal_report(self) -> None:
        from botocore.exceptions import NoCredentialsError

        code, output, _ = self._run(
            ["--diagnostics", str(self.report)],
            candidates=_catalog_candidates(),
            source_error=NoCredentialsError(),
        )
        self.assertEqual(code, 1, msg=output)
        self.assertIn("Unable to locate credentials", self._report()["fatal"])

    def test_a_missing_fingerprint_still_writes_its_fatal_report(self) -> None:
        from scripts import update as update_module

        out = io.StringIO()
        with mock.patch.object(update_module, "_fingerprint", side_effect=SystemExit(2)), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = update_module.main(["--diagnostics", str(self.report)])
        self.assertEqual(code, 2)
        report = self._report()
        self.assertIsNotNone(report["fatal"])
        self.assertIn("APT_SIGNING_FINGERPRINT", report["fatal"])
        self.assertFalse(report["published"])
        self.assertTrue(report["dry_run"])

    def test_a_report_write_failure_is_nonzero(self) -> None:
        missing = self.root / "missing" / "update-diagnostics.json"
        code, output, _ = self._run(
            ["--diagnostics", str(missing)],
            candidates=_catalog_candidates(),
        )
        self.assertEqual(code, 1, msg=output)
        self.assertIn("could not write update diagnostics", output)


if __name__ == "__main__":
    unittest.main()
