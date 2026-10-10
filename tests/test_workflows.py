"""Structural regressions for production workflows and builder defaults.

These tests pin the P4 repair properties without live cloud access: job-level
environments, env-indirected workflow inputs, retained signing-key material,
base-config deploys with atomic secret provisioning, required system tools,
and explicit xz compression.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"

RUN_RE = re.compile(r"run: \|\n(?P<body>(?: {10,}[^\n]*\n)+)")


def read_workflow(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def run_bodies(text: str) -> list[str]:
    return [match.group("body") for match in RUN_RE.finditer(text)]


def job_block(text: str, job: str) -> str:
    """Slice one top-level job (two-space indent) from a workflow document."""
    start = text.index(f"\n  {job}:")
    rest = text[start + 1:]
    nxt = re.search(r"\n  [A-Za-z0-9_-]+:", rest[1:])
    return rest[: nxt.start() + 1] if nxt else rest


class WorkflowStructureTest(unittest.TestCase):
    def test_no_top_level_environment(self) -> None:
        for name in ("ci.yml", "release.yml", "worker-deploy.yml"):
            text = read_workflow(name)
            head = text.split("jobs:")[0]
            self.assertNotIn("\nenvironment:", head, msg=name)

    def test_protected_jobs_carry_environment(self) -> None:
        for name in ("release.yml", "worker-deploy.yml"):
            text = read_workflow(name)
            self.assertIn("    environment: apt-production", read_workflow(name), msg=name)

    def test_no_template_interpolation_inside_shell(self) -> None:
        for name in ("ci.yml", "release.yml", "worker-deploy.yml"):
            for body in run_bodies(read_workflow(name)):
                self.assertNotIn("${{", body, msg=f"{name}: {body[:60]}")

    def test_release_inputs_pass_through_env(self) -> None:
        text = read_workflow("release.yml")
        self.assertIn("RELEASE_ID_INPUT: ${{ inputs.release_id }}", text)
        self.assertIn("ALLOW_KEY_ROTATION_INPUT: ${{ inputs.allow_key_rotation }}", text)
        self.assertIn('if [ -n "$RELEASE_ID_INPUT" ]', text)
        self.assertIn('"$RELEASE_ID_INPUT"', text)
        self.assertIn('[ "$ALLOW_KEY_ROTATION_INPUT" = "true" ]', text)

    def test_release_signing_key_retained_until_cleanup(self) -> None:
        text = read_workflow("release.yml")
        self.assertIn("APT_SIGNING_KEY_FILE=$RUNNER_TEMP/release-private.asc", text)
        self.assertIn("APT_SIGNING_KEY_ID=$APT_SIGNING_FINGERPRINT", text)
        import_step = text.split("Import release signing key")[1].split("- name:")[0]
        self.assertNotIn("rm -f", import_step)
        cleanup = text.split("Scrub isolated key material")[1]
        self.assertIn("$RUNNER_TEMP/release-private.asc", cleanup)

    def test_required_system_tools_installed(self) -> None:
        for name in ("ci.yml", "release.yml"):
            text = read_workflow(name)
            for tool in ("apt-utils", "file", "xz-utils"):
                self.assertIn(tool, text, msg=f"{name} missing {tool}")

    def test_smoke_arm64_installs_file_in_same_job(self) -> None:
        # Each job gets a fresh container, so the python job's tool install
        # cannot satisfy the arm64 smoke job's `file` invocations.
        job = job_block(read_workflow("ci.yml"), "smoke-arm64")
        install_lines = [line for line in job.splitlines() if "apt-get install" in line]
        self.assertEqual(len(install_lines), 1, msg="smoke-arm64 installs no tools")
        self.assertIn("file", install_lines[0])
        first_file_use = job.find("file /tmp/")
        self.assertGreater(first_file_use, job.find("apt-get install"))

    def test_deploy_uses_base_config_with_domains(self) -> None:
        text = read_workflow("worker-deploy.yml")
        self.assertIn("--config worker/wrangler.jsonc", text)
        self.assertIn('--domains "$APT_WORKER_DOMAIN"', text)
        self.assertNotIn("wrangler.deploy.jsonc", text)
        self.assertNotIn("routes:", text)

    def test_deploy_provisions_secret_atomically(self) -> None:
        text = read_workflow("worker-deploy.yml")
        self.assertIn("--secrets-file", text)
        self.assertIn("worker-secrets.json", text)
        validate = text.split("Validate production domain and credentials")[1].split("- name:")[0]
        self.assertIn("JSON.parse", validate)
        self.assertIn("CREDENTIALS", validate)
        for line in validate.splitlines():
            stripped = line.strip()
            if stripped.startswith("echo ") and "WORKER_CREDENTIALS" in stripped:
                self.fail("secret value must never appear in an echo line")
        cleanup = text.split("Scrub generated secret material")[1]
        self.assertIn("if: always()", cleanup.split("run:")[0])
        self.assertIn("worker-secrets.json", cleanup)

    def test_secret_files_explicitly_chmodded(self) -> None:
        # Node's writeFileSync mode only applies on creation, so both
        # generated secrets files need an explicit chmod for pre-existing paths.
        ci_validate = read_workflow("ci.yml").split("Dry-run production-equivalent")[1].split("- name:")[0]
        self.assertIn('chmod 600 "$RUNNER_TEMP/ci-secrets.json"', ci_validate)
        deploy_validate = read_workflow("worker-deploy.yml").split(
            "Validate production domain and credentials")[1].split("- name:")[0]
        self.assertIn('chmod 600 "$RUNNER_TEMP/worker-secrets.json"', deploy_validate)

    def test_ci_dry_runs_production_equivalent_command(self) -> None:
        text = read_workflow("ci.yml")
        self.assertIn("--config worker/wrangler.jsonc", text)
        self.assertIn("--domains apt-ci.example.com", text)
        self.assertIn("--dry-run", text)
        self.assertIn("--secrets-file", text)

    def test_builder_uses_explicit_xz(self) -> None:
        deb = (ROOT / "scripts" / "deb.py").read_text(encoding="utf-8")
        self.assertIn('"-Zxz"', deb)


if __name__ == "__main__":
    unittest.main()
