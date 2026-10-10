"""Real run-block matrix for the unified ``publish.yml``.

The YAML parsing and stub-execution fixture lives in
``tests.publish_workflow_fixtures``; these tests assert what each effective mode
actually ran: resolve routing, private-key gating, the exact CLI argv per mode,
the container shell, and that a hostile ``release_id`` stays one inert argv
element.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

from tests.publish_workflow_fixtures import (
    CONTAINER_DEFAULT_SHELL,
    IMPORT,
    JOB_DEFAULT_SHELL_RE,
    MANIFEST,
    PREVIEW,
    RUN_BLOCK_RE,
    UPSTREAM,
    PublishWorkflowCase,
    effective_shell,
    job_default_shell,
    run_body,
    step_blocks,
    step_name,
)


class PublishWorkflowRunTest(PublishWorkflowCase):
    # --- resolve routing -------------------------------------------------

    def test_schedule_forces_upstream_publish_without_rotation(self) -> None:
        # A forged dispatch input cannot redirect a scheduled run.
        proc, env = self.resolve("schedule", source="manifest", dry_run="true", rotation="true")
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertEqual(env["EFFECTIVE_SOURCE"], "upstream")
        self.assertEqual(env["EFFECTIVE_PUBLISH"], "true")
        self.assertEqual(env["EFFECTIVE_ROTATION"], "false")

    def test_dispatch_matrix_resolves_each_mode(self) -> None:
        cases = [
            ({"source": "upstream", "dry_run": "true", "rotation": "false"}, "upstream", "false", "false"),
            ({"source": "upstream", "dry_run": "false", "rotation": "false"}, "upstream", "true", "false"),
            ({"source": "manifest", "dry_run": "true", "rotation": "false"}, "manifest", "false", "false"),
            ({"source": "manifest", "dry_run": "false", "rotation": "false"}, "manifest", "true", "false"),
            ({"source": "manifest", "dry_run": "false", "rotation": "true"}, "manifest", "true", "true"),
        ]
        for inputs, source, publish, rotation in cases:
            with self.subTest(inputs=inputs):
                proc, env = self.resolve("workflow_dispatch", **inputs)
                self.assertEqual(proc.returncode, 0, msg=proc.stderr)
                self.assertEqual(env["EFFECTIVE_SOURCE"], source)
                self.assertEqual(env["EFFECTIVE_PUBLISH"], publish)
                self.assertEqual(env["EFFECTIVE_ROTATION"], rotation)

    def test_rejected_modes_fail_closed_without_effective_env(self) -> None:
        dispatch_bad = [
            {"source": "upstream", "dry_run": "false", "rotation": "true"},
            {"source": "manifest", "dry_run": "true", "rotation": "true"},
            {"source": "bogus", "dry_run": "true", "rotation": "false"},
            {"source": "upstream", "dry_run": "maybe", "rotation": "false"},
            {"source": "upstream", "dry_run": "true", "rotation": "maybe"},
        ]
        for inputs in dispatch_bad:
            with self.subTest(inputs=inputs):
                proc, env = self.resolve("workflow_dispatch", **inputs)
                self.assertEqual(proc.returncode, 1, msg=proc.stdout)
                self.assertEqual(env, {}, "a rejected mode must publish no effective env")
        proc, env = self.resolve("push")
        self.assertEqual(proc.returncode, 1, msg=proc.stdout)
        self.assertEqual(env, {})

    # --- route argv matrix ----------------------------------------------

    def test_upstream_routes_plan_or_publish_with_optional_release_id(self) -> None:
        for publish, argv in (("false", "--dry-run"), ("true", "--publish")):
            with self.subTest(publish=publish):
                self.log.write_bytes(b"")
                proc = self.route(UPSTREAM, "upstream", publish, release_id="20260102-030405")
                self.assertEqual(proc.returncode, 0, msg=proc.stderr)
                self.assertEqual(
                    self.uv_calls(),
                    [("run", "python", "-m", "scripts.update", argv, "--release-id", "20260102-030405")],
                )

    def test_manifest_preview_builds_unsigned_and_smokes_the_pool(self) -> None:
        proc = self.route(PREVIEW, "manifest", "false")
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        calls = self.uv_calls()
        self.assertEqual(calls[0], ("run", "python", "-m", "scripts.repo", "build", "--output", "dist/repo"))
        self.assertEqual(
            calls[1],
            ("run", "python", "-m", "scripts.smoke", "--manifest", "packaging/manifest.json", "--pool", "dist/repo/pool"),
        )
        # A preview never verifies (verify needs the signing key).
        self.assertNotIn("verify", [arg for call in calls for arg in call])

    def test_manifest_publish_runs_the_real_verify_publish_guards(self) -> None:
        base = ("run", "python", "-m", "scripts.repo", "publish", "--output", "dist/repo")
        for rotation, tail in (("false", base), ("true", base + ("--allow-key-rotation",))):
            with self.subTest(rotation=rotation):
                self.log.write_bytes(b"")
                proc = self.route(MANIFEST, "manifest", "true", rotation=rotation)
                self.assertEqual(proc.returncode, 0, msg=proc.stderr)
                calls = self.uv_calls()
                self.assertEqual(calls[0][:6], ("run", "python", "-m", "scripts.repo", "build", "--output"))
                self.assertEqual(calls[1], ("run", "python", "-m", "scripts.repo", "verify", "--output", "dist/repo"))
                self.assertEqual(calls[-1], tail)

    def test_hostile_release_id_stays_one_inert_argument(self) -> None:
        canary = self.root / "pwned"
        nasty = f"rel; touch {canary}; $(touch {canary}).sub; `touch {canary}.back` && echo x"
        for name, source, publish in ((UPSTREAM, "upstream", "true"), (PREVIEW, "manifest", "false")):
            with self.subTest(step=name):
                self.log.write_bytes(b"")
                proc = self.route(name, source, publish, release_id=nasty)
                self.assertEqual(proc.returncode, 0, msg=proc.stderr)
                calls = self.uv_calls()
                forwarded = [(call[i], call[i + 1]) for call in calls for i in range(len(call) - 1)]
                self.assertIn(("--release-id", nasty), forwarded,
                              msg="the id must survive quoted as a single argv element")
                self.assertFalse(canary.exists(), "release_id must never reach a shell")
                self.assertFalse(Path(f"{canary}.sub").exists())
                self.assertFalse(Path(f"{canary}.back").exists())

    # --- container shell -------------------------------------------------

    def test_container_shell_is_bash_and_the_default_is_load_bearing(self) -> None:
        # A container job defaults to `sh`, but the routing steps use Bash
        # arrays. The job must declare Bash, and removing that declaration must
        # make the real route bodies unparseable on the container default.
        self.assertEqual(job_default_shell(self.workflow), "bash")
        for block in self.steps:
            if RUN_BLOCK_RE.search(block) is None:
                continue
            with self.subTest(step=step_name(block)):
                proc = subprocess.run(
                    [effective_shell(self.workflow, block), "-n", "-c", run_body(block)],
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                self.assertEqual(proc.returncode, 0, msg=proc.stderr)

        without = JOB_DEFAULT_SHELL_RE.sub("", self.workflow, count=1)
        self.assertEqual(job_default_shell(without), CONTAINER_DEFAULT_SHELL)
        blocks = {step_name(b): b for b in step_blocks(without)}
        for name in (UPSTREAM, PREVIEW, MANIFEST):
            with self.subTest(sh_rejects=name):
                self.assertEqual(effective_shell(without, blocks[name]), CONTAINER_DEFAULT_SHELL)
                proc = subprocess.run(
                    ["sh", "-n", "-c", run_body(blocks[name])],
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                self.assertNotEqual(proc.returncode, 0, msg="sh must reject the Bash arrays")

    # --- private-key gating ---------------------------------------------

    def test_signing_key_import_is_gated_on_publication(self) -> None:
        self.assertIn("if: env.EFFECTIVE_PUBLISH == 'true'", self.block(IMPORT))

    def test_only_the_import_step_reads_the_private_key(self) -> None:
        for block in self.steps:
            name = step_name(block)
            if name == IMPORT or RUN_BLOCK_RE.search(block) is None:
                continue
            body = run_body(block)
            self.assertNotIn("APT_RELEASE_SIGNING_KEY", body, msg=str(name))
            self.assertNotIn("gpg", body, msg=str(name))

    def test_import_step_materializes_mode_600_key_and_exports_paths(self) -> None:
        proc = self.run_block(
            IMPORT,
            {"APT_RELEASE_SIGNING_KEY": "secret-armored-key", "APT_SIGNING_FINGERPRINT": "A" * 40},
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        key_file = self.runner_temp / "release-private.asc"
        self.assertTrue(key_file.exists())
        self.assertEqual(key_file.stat().st_mode & 0o777, 0o600)
        env = self.resolved()
        self.assertEqual(env["APT_SIGNING_KEY_FILE"], str(key_file))
        self.assertEqual(env["APT_SIGNING_KEY_ID"], "A" * 40)

    def test_import_step_fails_without_the_private_key(self) -> None:
        proc = self.run_block(IMPORT, {"APT_RELEASE_SIGNING_KEY": "", "APT_SIGNING_FINGERPRINT": "A" * 40})
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn("APT_SIGNING_KEY_FILE", self.resolved())


if __name__ == "__main__":
    unittest.main()
