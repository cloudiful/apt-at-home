"""Structural regressions for production workflows and builder defaults.

These tests pin the deployment-owner and release-safety properties without live
cloud access for the two published entrypoints (``ci.yml`` and ``publish.yml``).
"""

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
WORKFLOW_NAMES = ("ci.yml", "publish.yml")

RUN_RE = re.compile(r"run: \|\n(?P<body>(?: {10,}[^\n]*\n)+)")
SECRET_RE = re.compile(r"secrets\.([A-Z0-9_]+)")


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


def event_block(text: str) -> str:
    """Slice the top-level `on:` trigger block from a workflow document."""
    match = re.search(r"^on:\n(?P<body>(?:[ \t]+[^\n]*\n)+)", text, re.MULTILINE)
    return match.group("body") if match else ""


def event_entries(text: str) -> dict[str, list[str]]:
    """Each event name under `on:` with its own body lines, stripped."""
    entries: dict[str, list[str]] = {}
    current: str | None = None
    for line in event_block(text).splitlines():
        if not line.strip():
            continue
        if re.match(r"^  [A-Za-z0-9_-]+:\s*$", line):
            current = line.strip().rstrip(":")
            entries[current] = []
        elif current is not None:
            entries[current].append(line.strip())
    return entries


def install_packages(job: str) -> list[str]:
    """Package names from the job's apt-get install line, flags stripped."""
    for line in job.splitlines():
        if "apt-get install" not in line:
            continue
        _, _, rest = line.partition("apt-get install")
        return [token for token in rest.split() if not token.startswith("-")]
    return []


def step_order(job: str) -> list[str]:
    """Each top-level step's identifying text, in the order the job runs them."""
    steps: list[str] = []
    for line in job.splitlines():
        if not line.startswith("      - "):
            continue
        body = line.strip()[2:].strip()
        if body.startswith("uses:") or body.startswith("name:"):
            steps.append(body)
    return steps


# Bash array syntax that a POSIX `sh` cannot parse.
BASH_ARRAY = re.compile(r"args=\(|\$\{args\[@\]\}")


def uses_bash_arrays(job: str) -> bool:
    return BASH_ARRAY.search(job) is not None


class BashArrayShellTest(unittest.TestCase):
    """A Bash array under the default `sh` is a syntax error, not a build.

    A container job runs `run` steps with `sh` unless it says otherwise, so an
    array like `args=(...)` fails before the command it was assembling ever
    starts. Every job that uses one must therefore have an effective Bash shell,
    declared on the job or on the step. The ordinary CI build no longer routes
    through a Bash array; only the publish routing steps do.
    """

    def array_jobs(self) -> list[tuple[str, str, str]]:
        found: list[tuple[str, str, str]] = []
        for workflow in ("ci.yml", "publish.yml"):
            text = read_workflow(workflow)
            for name in re.findall(r"\n  ([A-Za-z0-9_-]+):", text):
                job = job_block(text, name)
                if uses_bash_arrays(job):
                    found.append((workflow, name, job))
        return found

    def test_every_array_job_has_an_effective_bash_shell(self) -> None:
        jobs = self.array_jobs()
        self.assertTrue(jobs, "expected at least one array-using job")
        for workflow, name, job in jobs:
            with self.subTest(workflow=workflow, job=name):
                self.assertTrue(
                    re.search(r"defaults:\s*\n\s*run:\s*\n\s*shell:\s*bash", job),
                    "the job needs defaults.run.shell: bash",
                )

    def test_the_array_step_is_not_left_to_the_container_default(self) -> None:
        """The array step itself must be Bash, or inherit a Bash job default."""
        for workflow, name, job in self.array_jobs():
            with self.subTest(workflow=workflow, job=name):
                self.assertIn("shell: bash", job)

    def test_the_ordinary_build_invokes_no_source_routing(self) -> None:
        """The simplified CI build is a plain asset-only invocation."""
        job = job_block(read_workflow("ci.yml"), "python")
        self.assertIn("python -m scripts.repo build --output dist/repo", job)
        self.assertNotIn("--source-artifacts", job)
        self.assertNotIn("--reuse-published-source", job)
        self.assertNotIn("SOURCE_ROUTING", job)


# The two jobs that run the complete Python suite.
FULL_SUITE_JOBS = (("ci.yml", "python"), ("publish.yml", "publish"))


class CheckoutHistoryTest(unittest.TestCase):
    """A checkout that cannot read git, or cannot read the base, fails the suite.

    `actions/checkout` silently falls back to a REST tarball when git is missing,
    and a shallow clone omits the base commit the suite compares against. Both
    failures look the same from here — a suite step that cannot find the
    repository — so they are asserted structurally instead.
    """

    def test_git_is_installed_before_this_repository_is_checked_out(self) -> None:
        for workflow, job_name in FULL_SUITE_JOBS:
            with self.subTest(workflow=workflow):
                job = job_block(read_workflow(workflow), job_name)
                order = step_order(job)
                checkout = next(
                    (i for i, step in enumerate(order) if "actions/checkout" in step), None
                )
                self.assertIsNotNone(checkout, "the job checks this repository out")
                tools = next(
                    (i for i, step in enumerate(order) if step.startswith("name: Install system tools")),
                    None,
                )
                self.assertIsNotNone(tools, "the job installs its tools explicitly")
                self.assertLess(tools, checkout, "git must exist before checkout runs")
                self.assertIn("git", install_packages(job))

    def test_checkout_never_persists_credentials(self) -> None:
        for workflow, job_name in FULL_SUITE_JOBS:
            with self.subTest(workflow=workflow, job=job_name):
                job = job_block(read_workflow(workflow), job_name)
                self.assertIn("persist-credentials: false", job)

    def test_no_source_producer_job_remains(self) -> None:
        """The source-build producer and its routing are gone, not disabled."""
        for workflow in ("ci.yml", "publish.yml"):
            text = read_workflow(workflow)
            self.assertNotIn("source-producer", text)
            self.assertNotIn("tauri_source", text)
            self.assertNotIn("source-plan", text)
            self.assertNotIn("SOURCE_ROUTING", text)

    def test_shallow_checkout_is_sufficient_without_source_history(self) -> None:
        """Normal shallow checkouts remain; no job requires full history."""
        for workflow, job_name in FULL_SUITE_JOBS:
            with self.subTest(workflow=workflow):
                job = job_block(read_workflow(workflow), job_name)
                self.assertNotRegex(job, r"fetch-depth:\s*0")


class CiTriggerTest(unittest.TestCase):
    """An ordinary branch push must not launch a second full CI run.

    The pull-request event already covers every proposed change, so the push
    trigger is limited to the default branch. A push without a branch filter
    would run the whole suite on every branch again, which is exactly the
    duplicate this pins shut.
    """

    def test_push_runs_on_main_only(self) -> None:
        entries = event_entries(read_workflow("ci.yml"))
        self.assertIn("push", entries)
        branch_lines = [line for line in entries["push"] if line.startswith("branches:")]
        self.assertEqual(len(branch_lines), 1, msg=f"push filters: {entries['push']}")
        self.assertEqual(branch_lines[0], 'branches: ["main"]')

    def test_push_has_no_other_branch_filter(self) -> None:
        push = "\n".join(event_entries(read_workflow("ci.yml"))["push"])
        for forbidden in ("branches-ignore", "paths", "paths-ignore", "tags"):
            self.assertNotIn(forbidden, push)

    def test_pull_request_and_manual_triggers_are_kept(self) -> None:
        entries = event_entries(read_workflow("ci.yml"))
        self.assertIn("pull_request", entries)
        self.assertIn("workflow_dispatch", entries)


class WorkflowStructureTest(unittest.TestCase):
    def test_only_ci_and_publish_workflows_exist(self) -> None:
        names = sorted(path.name for path in WORKFLOWS.glob("*.yml"))
        self.assertEqual(names, list(WORKFLOW_NAMES))

    def test_removed_publication_entrypoints_are_gone(self) -> None:
        # The merge replaces the two old facades rather than disabling them.
        for stale in ("release.yml", "update.yml"):
            self.assertFalse((WORKFLOWS / stale).exists(), msg=stale)
        # No document keeps a stale reference, not even a negative historical one.
        for doc in [ROOT / "README.md", ROOT / "README.zh-CN.md", *sorted((ROOT / "docs").glob("*.md"))]:
            text = doc.read_text(encoding="utf-8")
            for stale in ("release.yml", "update.yml"):
                self.assertNotIn(stale, text, msg=f"{doc.name}: {stale}")

    def test_no_top_level_environment(self) -> None:
        for name in WORKFLOW_NAMES:
            head = read_workflow(name).split("jobs:")[0]
            self.assertNotIn("\nenvironment:", head, msg=name)

    def test_publish_job_is_the_only_protected_job(self) -> None:
        self.assertIn("    environment: prod", read_workflow("publish.yml"))
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

    def test_publish_inputs_pass_through_env(self) -> None:
        text = read_workflow("publish.yml")
        self.assertIn("SOURCE_INPUT: ${{ inputs.source }}", text)
        self.assertIn("DRY_RUN_INPUT: ${{ inputs.dry_run }}", text)
        self.assertIn("ROTATION_INPUT: ${{ inputs.allow_key_rotation }}", text)
        self.assertIn("RELEASE_ID_INPUT: ${{ inputs.release_id }}", text)
        self.assertIn('if [ -n "$RELEASE_ID_INPUT" ]', text)
        self.assertIn('args+=(--release-id "$RELEASE_ID_INPUT")', text)

    def test_publish_is_scheduled_and_dispatched(self) -> None:
        text = read_workflow("publish.yml")
        self.assertIn("schedule:", text)
        self.assertIn('cron: "17 */6 * * *"', text)
        self.assertIn("workflow_dispatch:", text)

    def test_publish_dispatch_defaults_to_a_dry_run_upstream_update(self) -> None:
        # A button press must get a plan unless the operator chooses to publish.
        head = read_workflow("publish.yml").split("permissions:")[0]
        self.assertIn("dry_run:", head)
        self.assertIn('default: "true"', head)
        self.assertIn("source:", head)
        self.assertIn('default: "upstream"', head)

    def test_publish_publishes_only_from_the_default_branch(self) -> None:
        # A manual run on any other ref must not publish unreviewed code.
        publish = job_block(read_workflow("publish.yml"), "publish")
        self.assertIn("if: github.ref == 'refs/heads/main'", publish)

    def test_production_job_is_serialized(self) -> None:
        # Automatic updates and manual releases must never move the pointer at
        # the same time, so the single production workflow serializes on one group.
        text = read_workflow("publish.yml")
        self.assertIn("group: apt-production-release", text)
        self.assertIn("cancel-in-progress: false", text)

    def test_workflows_use_least_privilege_contents_read(self) -> None:
        for name in WORKFLOW_NAMES:
            text = read_workflow(name)
            self.assertIn("permissions:\n  contents: read", text, msg=name)
            for privilege in ("contents: write", "pull-requests: write", "id-token: write"):
                self.assertNotIn(privilege, text, msg=f"{name}: {privilege}")

    def test_publish_reuses_the_existing_production_secrets_only(self) -> None:
        text = read_workflow("publish.yml")
        expected = {
            "APT_S3_BUCKET", "APT_S3_ENDPOINT", "APT_S3_REGION",
            "APT_S3_ACCESS_KEY_ID", "APT_S3_SECRET_ACCESS_KEY",
            "APT_RELEASE_SIGNING_KEY", "APT_SIGNING_FINGERPRINT",
        }
        # GITHUB_TOKEN is provided by the repository, not stored as a secret, so
        # it does not widen the production secret set.
        used = set(SECRET_RE.findall(text)) - {"GITHUB_TOKEN"}
        self.assertEqual(used, expected)
        # No Cloudflare material belongs in the publication path.
        for name in WORKFLOW_NAMES:
            self.assertNotIn("CLOUDFLARE", read_workflow(name), msg=name)

    def test_publish_signing_key_is_private_and_always_scrubbed(self) -> None:
        text = read_workflow("publish.yml")
        import_step = text.split("Import signing key")[1].split("- name:")[0]
        self.assertIn('chmod 600 "$RUNNER_TEMP/release-private.asc"', import_step)
        self.assertIn("gpg --batch --import", import_step)
        # The key is never an artifact and never echoed.
        publish = job_block(text, "publish")
        self.assertNotIn("upload-artifact", publish)
        cleanup = text.split("Scrub isolated key material")[1]
        self.assertIn("if: always()", cleanup)
        self.assertIn("$RUNNER_TEMP/release-private.asc", cleanup)

    def test_publish_never_interpolates_upstream_data_into_the_shell(self) -> None:
        # Upstream API values must not reach a shell command or an output file.
        publish = job_block(read_workflow("publish.yml"), "publish")
        self.assertNotIn("GITHUB_OUTPUT", publish)
        for body in run_bodies(read_workflow("publish.yml")):
            self.assertNotIn("${{", body, msg=body[:60])

    def test_publish_signing_key_retained_until_cleanup(self) -> None:
        text = read_workflow("publish.yml")
        self.assertIn("APT_SIGNING_KEY_FILE=$RUNNER_TEMP/release-private.asc", text)
        self.assertIn("APT_SIGNING_KEY_ID=$APT_SIGNING_FINGERPRINT", text)
        import_step = text.split("Import signing key")[1].split("- name:")[0]
        self.assertNotIn("rm -f", import_step)
        cleanup = text.split("Scrub isolated key material")[1]
        self.assertIn("$RUNNER_TEMP/release-private.asc", cleanup)

    def test_required_system_tools_installed(self) -> None:
        for name in WORKFLOW_NAMES:
            text = read_workflow(name)
            for tool in ("file", "xz-utils"):
                self.assertIn(tool, text, msg=f"{name} missing {tool}")
            self.assertNotIn("apt-utils", text, msg=f"{name} installs the removed apt-utils")

    def test_build_jobs_install_reprepro(self) -> None:
        # Production `Packages` indices come from reprepro in every build job.
        for name, job in (("ci.yml", "python"), ("publish.yml", "publish")):
            packages = install_packages(job_block(read_workflow(name), job))
            self.assertIn("reprepro", packages, msg=f"{name}:{job}")

    def test_gpg_agent_installed_where_keys_are_generated(self) -> None:
        # A bare `gpg` carries no gpg-agent, so key generation and import fail;
        # the `gnupg` metapackage pulls the agent in as a hard dependency.
        python_job = job_block(read_workflow("ci.yml"), "python")
        self.assertIn("gnupg", install_packages(python_job))
        self.assertNotIn("gpg", install_packages(python_job))
        self.assertIn("gpg --batch --generate-key", python_job)

        publish_job = job_block(read_workflow("publish.yml"), "publish")
        self.assertIn("gnupg", install_packages(publish_job))
        self.assertNotIn("gpg", install_packages(publish_job))
        self.assertIn("gpg --batch --import", publish_job)

    def test_ci_exercises_the_update_cli(self) -> None:
        # The scheduled updater runs the real CLI, so CI proves it at least
        # parses and resolves without touching any secret.
        text = read_workflow("ci.yml")
        self.assertIn("python -m scripts.update --help", text)

    def test_ci_does_not_import_a_production_secret(self) -> None:
        # CI is independent: it only ever generates an ephemeral key of its own.
        text = read_workflow("ci.yml")
        self.assertNotIn("APT_RELEASE_SIGNING_KEY", text)
        self.assertNotIn("APT_SIGNING_FINGERPRINT", text)

    def test_smoke_jobs_install_the_generated_packages(self) -> None:
        # Each smoke job installs the debs its own architecture built.
        text = read_workflow("ci.yml")
        for job, arch in (("smoke-amd64", "amd64"), ("smoke-arm64", "arm64")):
            block = job_block(text, job)
            self.assertIn(f"apt-get install -y --no-install-recommends ./dist/repo/pool/*_{arch}.deb", block)
            installed = " ".join(
                line for line in block.splitlines() if "apt-get install" in line
            )
            self.assertNotIn("gpg", installed, msg=job)
            self.assertNotIn("gnupg", installed, msg=job)

    def test_each_smoke_job_installs_its_own_python_interpreter(self) -> None:
        """Each fresh smoke container cannot inherit an interpreter.

        Asserted per job, and the install must precede the helper in that job.
        """
        text = read_workflow("ci.yml")
        for job in ("smoke-amd64", "smoke-arm64"):
            with self.subTest(job=job):
                block = job_block(text, job)
                install_lines = [
                    line for line in block.splitlines() if "apt-get install" in line
                ]
                self.assertTrue(install_lines, msg=f"{job} installs nothing")
                self.assertTrue(
                    any("python3" in line for line in install_lines),
                    msg=f"{job} never installs python3",
                )
                self.assertLess(
                    block.index("python3"), block.index("-m scripts.smoke"),
                    msg=f"{job} installs python3 after the helper runs",
                )

    def test_smoke_jobs_check_every_package_from_the_manifest(self) -> None:
        # No workflow may hard-code the package set; the manifest drives it.
        text = read_workflow("ci.yml")
        for job, arch in (("smoke-amd64", "amd64"), ("smoke-arm64", "arm64")):
            block = job_block(text, job)
            self.assertIn(
                "--manifest packaging/manifest.json --count", block, msg=job
            )
            self.assertIn("--pool dist/repo/pool", block, msg=job)
            self.assertIn(f"--installed --arch {arch}", block, msg=job)
            self.assertNotRegex(block, r'=\s*"4"')
            self.assertNotIn("zellij_0.45.1-1", block, msg=job)
            self.assertNotIn("starship_1.26.0-1", block, msg=job)

    def test_apt_upgrade_jobs_install_distro_neovim_then_the_generated_debs(self) -> None:
        # The distro neovim must be installed first, so the upgrade is a real transition.
        text = read_workflow("ci.yml")
        for job, arch in (("upgrade-amd64", "amd64"), ("upgrade-arm64", "arm64")):
            with self.subTest(job=job):
                block = job_block(text, job)
                self.assertIn("needs: [python]", block)
                distro = block.index("apt-get install -y --no-install-recommends python3 neovim")
                generated = block.index(f"./dist/repo/pool/*_{arch}.deb")
                self.assertLess(distro, generated)
                self.assertIn("--upgraded", block)
                self.assertIn(f"--installed --arch {arch}", block)
                self.assertNotIn("dpkg -i", block)
                self.assertNotIn("--force", block)

    def test_publish_counts_the_pool_from_the_manifest(self) -> None:
        block = job_block(read_workflow("publish.yml"), "publish")
        self.assertIn("--manifest packaging/manifest.json --count", block)
        self.assertNotRegex(block, r'=\s*"4"')

    def test_publish_discovery_uses_the_repository_token(self) -> None:
        # The read-only repository token lifts the public API rate limit.
        block = job_block(read_workflow("publish.yml"), "publish")
        self.assertIn("GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}", block)
        self.assertIn("contents: read", read_workflow("publish.yml"))

    def test_builder_uses_explicit_xz(self) -> None:
        deb = (ROOT / "scripts" / "deb.py").read_text(encoding="utf-8")
        self.assertIn('"-Zxz"', deb)


if __name__ == "__main__":
    unittest.main()
