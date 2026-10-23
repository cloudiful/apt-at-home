"""Structural regressions for production workflows and builder defaults.

These tests pin the deployment-owner and release-safety properties without live
cloud access for the single published entrypoint (``publish.yml``).
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
WORKFLOW_NAMES = ("publish.yml",)

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
    declared on the job or on the step. The publish routing steps are the only
    remaining array users.
    """

    def array_jobs(self) -> list[tuple[str, str, str]]:
        found: list[tuple[str, str, str]] = []
        for workflow in WORKFLOW_NAMES:
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

# The job that runs the complete Python suite.
FULL_SUITE_JOBS = (("publish.yml", "publish"),)


class CheckoutHistoryTest(unittest.TestCase):
    """`actions/checkout` silently falls back to a REST tarball when git is
    missing, so the publish job installs git before checkout and keeps its
    shallow checkout because it never compares base history.
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
        for workflow in WORKFLOW_NAMES:
            text = read_workflow(workflow)
            self.assertNotIn("source-producer", text)
            self.assertNotIn("tauri_source", text)
            self.assertNotIn("source-plan", text)
            self.assertNotIn("SOURCE_ROUTING", text)

    def test_publish_checkout_stays_shallow(self) -> None:
        job = job_block(read_workflow("publish.yml"), "publish")
        self.assertNotIn("fetch-depth: 0", job)


class WorkflowStructureTest(unittest.TestCase):
    def test_only_publish_workflow_exists(self) -> None:
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

    def test_publish_job_is_protected(self) -> None:
        self.assertIn("    environment: prod", read_workflow("publish.yml"))

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
        # Cloudflare Worker Builds owns the deploy; the publish workflow never
        # bundles or uploads the Worker.
        for name in WORKFLOW_NAMES:
            self.assertNotIn("wrangler deploy", read_workflow(name), msg=name)

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

    def test_publish_installs_reprepro(self) -> None:
        # Production `Packages` indices come from reprepro in the publish job.
        job = job_block(read_workflow("publish.yml"), "publish")
        self.assertIn("reprepro", install_packages(job))

    def test_publish_installs_gnupg_for_key_import(self) -> None:
        # A bare `gpg` carries no gpg-agent, so key import fails; the `gnupg`
        # metapackage pulls the agent in as a hard dependency.
        publish_job = job_block(read_workflow("publish.yml"), "publish")
        self.assertIn("gnupg", install_packages(publish_job))
        self.assertNotIn("gpg", install_packages(publish_job))
        self.assertIn("gpg --batch --import", publish_job)

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
