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
WORKFLOW_NAMES = ("ci.yml", "release.yml", "update.yml")

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
        self.assertIn("    environment: prod", read_workflow("release.yml"))
        self.assertNotIn("environment:", read_workflow("ci.yml"))

    def test_update_job_runs_in_the_protected_prod_environment(self) -> None:
        # Automatic publication carries the same secrets as a manual release, so
        # it must sit behind the same protected environment and its approvals.
        self.assertIn("    environment: prod", job_block(read_workflow("update.yml"), "update"))
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
        self.assertNotIn("schedule:", text)
        self.assertIn("group: apt-production-release", text)
        self.assertIn("cancel-in-progress: false", text)

    def test_update_is_scheduled_and_dispatched(self) -> None:
        text = read_workflow("update.yml")
        self.assertIn("schedule:", text)
        self.assertIn('cron: "17 */6 * * *"', text)
        self.assertIn("workflow_dispatch:", text)

    def test_update_dispatch_defaults_to_a_dry_run(self) -> None:
        # The scheduled run publishes, but a human pressing the button must get
        # a plan unless they explicitly choose to publish.
        head = read_workflow("update.yml").split("permissions:")[0]
        self.assertIn("dry_run:", head)
        self.assertIn('default: "true"', head)

    def test_manual_update_publishes_only_when_asked(self) -> None:
        update = job_block(read_workflow("update.yml"), "update")
        step = read_workflow("update.yml").split("- name: Update to the latest stable")[1]
        # A scheduled run publishes; dispatch follows the explicit input.
        self.assertIn('"$GITHUB_EVENT_NAME" = "schedule"', step)
        self.assertIn('"$DRY_RUN_INPUT" = "false"', step)
        self.assertIn("scripts.update --publish", step)
        self.assertIn("scripts.update --dry-run", step)

    def test_update_publishes_only_from_the_default_branch(self) -> None:
        # A manual run on any other ref must not publish unreviewed code.
        update = job_block(read_workflow("update.yml"), "update")
        self.assertIn("if: github.ref == 'refs/heads/main'", update)

    def test_production_jobs_share_one_serialized_concurrency_group(self) -> None:
        # Automatic updates and manual releases must never move the pointer at
        # the same time, so both workflows serialize on one group.
        for name in ("release.yml", "update.yml"):
            text = read_workflow(name)
            self.assertIn("group: apt-production-release", text, msg=name)
            self.assertIn("cancel-in-progress: false", text, msg=name)

    def test_workflows_use_least_privilege_contents_read(self) -> None:
        for name in WORKFLOW_NAMES:
            text = read_workflow(name)
            self.assertIn("permissions:\n  contents: read", text, msg=name)
            for privilege in ("contents: write", "pull-requests: write", "id-token: write"):
                self.assertNotIn(privilege, text, msg=f"{name}: {privilege}")

    def test_update_reuses_the_existing_production_secrets_only(self) -> None:
        text = read_workflow("update.yml")
        expected = {
            "APT_S3_BUCKET", "APT_S3_ENDPOINT", "APT_S3_REGION",
            "APT_S3_ACCESS_KEY_ID", "APT_S3_SECRET_ACCESS_KEY",
            "APT_RELEASE_SIGNING_KEY", "APT_SIGNING_FINGERPRINT",
        }
        used = set(re.findall(r"secrets\.([A-Z0-9_]+)", text))
        self.assertEqual(used, expected)
        # No Cloudflare material belongs in the publication path.
        for name in WORKFLOW_NAMES:
            self.assertNotIn("CLOUDFLARE", read_workflow(name), msg=name)

    def test_update_signing_key_is_private_and_always_scrubbed(self) -> None:
        text = read_workflow("update.yml")
        import_step = text.split("Import signing key")[1].split("- name:")[0]
        self.assertIn('chmod 600 "$RUNNER_TEMP/release-private.asc"', import_step)
        self.assertIn("gpg --batch --import", import_step)
        # The key is never an artifact and never echoed.
        self.assertNotIn("upload-artifact", text)
        cleanup = text.split("Scrub isolated key material")[1]
        self.assertIn("if: always()", cleanup)
        self.assertIn("$RUNNER_TEMP/release-private.asc", cleanup)

    def test_update_never_interpolates_upstream_data_into_the_shell(self) -> None:
        # Upstream API values must not reach a shell command or an output file.
        update = job_block(read_workflow("update.yml"), "update")
        self.assertNotIn("GITHUB_OUTPUT", update)
        for body in run_bodies(read_workflow("update.yml")):
            self.assertNotIn("${{", body, msg=body[:60])

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

    def test_build_jobs_install_reprepro(self) -> None:
        # Production `Packages` indices come from reprepro, so every job that
        # runs the build must install it instead of falling back or skipping.
        for name, job in (
            ("ci.yml", "python"),
            ("release.yml", "publish"),
            ("update.yml", "update"),
        ):
            packages = install_packages(job_block(read_workflow(name), job))
            self.assertIn("reprepro", packages, msg=f"{name}:{job}")

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

        update_job = job_block(read_workflow("update.yml"), "update")
        self.assertIn("gnupg", install_packages(update_job))
        self.assertNotIn("gpg", install_packages(update_job))
        self.assertIn("gpg --batch --import", update_job)

    def test_ci_exercises_the_update_cli(self) -> None:
        # The scheduled updater runs the real CLI in production, so CI proves it
        # at least parses and resolves without touching any secret.
        text = read_workflow("ci.yml")
        self.assertIn("python -m scripts.update --help", text)

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
