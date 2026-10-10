"""Structural regressions for production workflows and builder defaults.

These tests pin the deployment-owner and release-safety properties without live
cloud access: job-level environments, env-indirected workflow inputs, retained
signing-key material, dry-run-only Worker bundling, required system tools, and
explicit xz compression.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
WORKFLOW_NAMES = ("ci.yml", "release.yml")

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


def install_packages(job: str) -> list[str]:
    """Package names from the job's apt-get install line, flags stripped."""
    for line in job.splitlines():
        if "apt-get install" not in line:
            continue
        _, _, rest = line.partition("apt-get install")
        return [token for token in rest.split() if not token.startswith("-")]
    return []


class WorkflowStructureTest(unittest.TestCase):
    def test_only_validation_and_release_workflows_exist(self) -> None:
        names = sorted(path.name for path in WORKFLOWS.glob("*.yml"))
        self.assertEqual(names, list(WORKFLOW_NAMES))

    def test_no_top_level_environment(self) -> None:
        for name in WORKFLOW_NAMES:
            head = read_workflow(name).split("jobs:")[0]
            self.assertNotIn("\nenvironment:", head, msg=name)

    def test_release_job_is_the_only_protected_job(self) -> None:
        self.assertIn("    environment: apt-production", read_workflow("release.yml"))
        self.assertNotIn("environment:", read_workflow("ci.yml"))

    def test_no_template_interpolation_inside_shell(self) -> None:
        for name in WORKFLOW_NAMES:
            for body in run_bodies(read_workflow(name)):
                self.assertNotIn("${{", body, msg=f"{name}: {body[:60]}")

    def test_workflows_validate_the_portable_public_lock(self) -> None:
        for name in WORKFLOW_NAMES:
            text = read_workflow(name)
            self.assertIn("UV_DEFAULT_INDEX: https://pypi.org/simple", text, msg=name)
            self.assertIn("uv lock --check", text, msg=name)
            self.assertIn("uv sync --frozen", text, msg=name)

    def test_github_never_deploys_the_worker(self) -> None:
        # Cloudflare Worker Builds owns the deploy; every wrangler invocation
        # that remains in GitHub stays a dry-run bundling check.
        deploy_lines = [
            (name, line.strip())
            for name in WORKFLOW_NAMES
            for line in read_workflow(name).splitlines()
            if "wrangler deploy" in line
        ]
        self.assertTrue(deploy_lines, msg="expected at least one CI dry-run")
        for name, line in deploy_lines:
            self.assertIn("--dry-run", line, msg=f"{name}: {line}")

    def test_ci_dry_runs_production_equivalent_command(self) -> None:
        text = read_workflow("ci.yml")
        self.assertIn("--config worker/wrangler.jsonc", text)
        self.assertIn("--domains apt-ci.example.com", text)
        self.assertIn("--dry-run", text)
        self.assertIn("--secrets-file", text)
        self.assertNotIn("wrangler.deploy.jsonc", text)
        self.assertNotIn("routes:", text)

    def test_dry_run_provisions_synthetic_secret_atomically(self) -> None:
        text = read_workflow("ci.yml")
        deploy = text.split("Dry-run production-equivalent")[1]
        self.assertIn("--secrets-file", deploy)
        self.assertIn("ci-secrets.json", deploy)
        self.assertIn('chmod 600 "$RUNNER_TEMP/ci-secrets.json"', deploy)

    def test_release_inputs_pass_through_env(self) -> None:
        text = read_workflow("release.yml")
        self.assertIn("RELEASE_ID_INPUT: ${{ inputs.release_id }}", text)
        self.assertIn("ALLOW_KEY_ROTATION_INPUT: ${{ inputs.allow_key_rotation }}", text)
        self.assertIn('if [ -n "$RELEASE_ID_INPUT" ]', text)
        self.assertIn('"$RELEASE_ID_INPUT"', text)
        self.assertIn('[ "$ALLOW_KEY_ROTATION_INPUT" = "true" ]', text)

    def test_release_serialized_and_manual(self) -> None:
        text = read_workflow("release.yml")
        self.assertIn("workflow_dispatch:", text)
        self.assertIn("group: apt-release", text)
        self.assertIn("cancel-in-progress: false", text)

    def test_release_signing_key_retained_until_cleanup(self) -> None:
        text = read_workflow("release.yml")
        self.assertIn("APT_SIGNING_KEY_FILE=$RUNNER_TEMP/release-private.asc", text)
        self.assertIn("APT_SIGNING_KEY_ID=$APT_SIGNING_FINGERPRINT", text)
        import_step = text.split("Import release signing key")[1].split("- name:")[0]
        self.assertNotIn("rm -f", import_step)
        cleanup = text.split("Scrub isolated key material")[1]
        self.assertIn("$RUNNER_TEMP/release-private.asc", cleanup)

    def test_required_system_tools_installed(self) -> None:
        for name in WORKFLOW_NAMES:
            text = read_workflow(name)
            for tool in ("apt-utils", "file", "xz-utils"):
                self.assertIn(tool, text, msg=f"{name} missing {tool}")

    def test_gpg_agent_installed_where_keys_are_generated(self) -> None:
        # A bare `gpg` install carries no gpg-agent, so non-interactive key
        # generation and import fail; the `gnupg` metapackage pulls the agent
        # in as a hard dependency.
        python_job = job_block(read_workflow("ci.yml"), "python")
        self.assertIn("gnupg", install_packages(python_job))
        self.assertNotIn("gpg", install_packages(python_job))
        self.assertIn("gpg --batch --generate-key", python_job)

        publish_job = job_block(read_workflow("release.yml"), "publish")
        self.assertIn("gnupg", install_packages(publish_job))
        self.assertNotIn("gpg", install_packages(publish_job))
        self.assertIn("gpg --batch --import", publish_job)

    def test_smoke_jobs_install_no_unused_gpg(self) -> None:
        # Fresh containers per job: smoke jobs never touch GPG, so they must
        # not grow an unused keyring package.
        text = read_workflow("ci.yml")
        for job in ("smoke-amd64", "smoke-arm64"):
            packages = install_packages(job_block(text, job))
            self.assertNotIn("gpg", packages, msg=job)
            self.assertNotIn("gnupg", packages, msg=job)

    def test_smoke_arm64_installs_file_in_same_job(self) -> None:
        # Each job gets a fresh container, so the python job's tool install
        # cannot satisfy the arm64 smoke job's `file` invocations.
        job = job_block(read_workflow("ci.yml"), "smoke-arm64")
        install_lines = [line for line in job.splitlines() if "apt-get install" in line]
        self.assertEqual(len(install_lines), 1, msg="smoke-arm64 installs no tools")
        self.assertIn("file", install_lines[0])
        first_file_use = job.find("file /tmp/")
        self.assertGreater(first_file_use, job.find("apt-get install"))

    def test_builder_uses_explicit_xz(self) -> None:
        deb = (ROOT / "scripts" / "deb.py").read_text(encoding="utf-8")
        self.assertIn('"-Zxz"', deb)


if __name__ == "__main__":
    unittest.main()
