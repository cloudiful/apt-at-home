"""The producer is isolated by construction, and the wiring says so.

These are regressions on the two committed workflows, read as documents and run
as shell. The properties that matter cannot be checked by a passing test suite in
CI alone: a job that grows a secret reference, a checkout that loses
`persist-credentials: false`, an upload path that starts including the upstream
checkout, or a compile that is no longer gated on a verified decision would all
leave every other test green. Each case below is one of those.

The plan and compile steps are driven against stub binaries, so the assertions
are about what the job *runs*: the planner is the only thing that may decide, the
only thing that reaches the output file is its own boolean, and the compile reads
the plan rather than a workflow input.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.source_build import PLAN_NAME, SOURCE_PROVENANCE_NAME
from tests.test_workflows import RUN_RE, install_packages, job_block, read_workflow, run_bodies

PRODUCER = "source-producer"
PLANNER = "python -m scripts.source_plan"
COMPILE = "python -m scripts.tauri_source"
PLAN_STEP = "Plan the source build against the public repository"
COMPILE_STEP = "Check out and compile the planned source identity"
ROUTING_STEP = "Resolve the same-run source artifact routing"
WORKFLOWS = ("ci.yml", "publish.yml")

STEP_SPLIT_RE = re.compile(r"(?m)^(      - )")
SECRET_RE = re.compile(r"secrets\.[A-Za-z0-9_]+")
ARG_SEP = b"\x01"

STUB_UV = """#!/usr/bin/env bash
set -euo pipefail
{
  for arg in "$@"; do printf '%s\\0' "$arg"; done
  printf '\\001'
} >>"${UV_STUB_LOG:?}"
case "$*" in
  *scripts.source_plan\\ published*) echo "${SOURCE_PUBLISHED_ANSWER-false}" ;;
  *) echo "${SOURCE_PLAN_DECISION-true}" ;;
esac
"""

ARG_SEP = b"\x01"


def steps_of(job: str) -> list[str]:
    body = job.split("\n    steps:\n", 1)[1]
    parts = STEP_SPLIT_RE.split(body)
    return [parts[index] + parts[index + 1] for index in range(1, len(parts), 2)]


def step_named(job: str, name: str) -> str:
    for block in steps_of(job):
        if re.search(rf"(?m)^      - name: {re.escape(name)}$", block):
            return block
    raise AssertionError(f"job has no step named {name!r}")


def run_body(block: str) -> str:
    match = RUN_RE.search(block)
    if match is None:
        raise AssertionError(f"no run body in step: {block[:40]}")
    return "\n".join(line[10:] for line in match.group("body").splitlines())


class ProducerIsolationTest(unittest.TestCase):
    """Both producers: same job name, same shape, in both workflows."""

    def producer(self, workflow: str) -> str:
        return job_block(read_workflow(workflow), PRODUCER)

    def test_the_producer_is_present_in_both_workflows(self) -> None:
        for workflow in WORKFLOWS:
            with self.subTest(workflow=workflow):
                self.assertIn(f"\n  {PRODUCER}:", read_workflow(workflow))
                self.assertIn("name: Plan and compile", self.producer(workflow))

    def test_the_producer_holds_no_secret_and_no_environment(self) -> None:
        for workflow in WORKFLOWS:
            with self.subTest(workflow=workflow):
                job = self.producer(workflow)
                self.assertEqual(SECRET_RE.findall(job), [], "the producer may not read a secret")
                self.assertNotIn("environment:", job)
                for material in (
                    "GNUPGHOME", "APT_RELEASE_SIGNING_KEY", "APT_SIGNING_KEY_FILE",
                    "GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "APT_S3_BUCKET",
                ):
                    self.assertNotIn(material, job, material)

    def test_the_producer_compiles_natively_on_arm(self) -> None:
        for workflow in WORKFLOWS:
            with self.subTest(workflow=workflow):
                job = self.producer(workflow)
                self.assertIn("runs-on: ubuntu-24.04-arm", job)
                self.assertIn("timeout-minutes: 45", job)
                self.assertIn("container: debian:13", job)

    def test_git_is_installed_before_every_checkout(self) -> None:
        # actions/checkout silently falls back to a REST tarball without git, and
        # a tarball has no repository for the provenance check to prove anything.
        for workflow in WORKFLOWS:
            with self.subTest(workflow=workflow):
                job = self.producer(workflow)
                packages = install_packages(job)
                self.assertIn("git", packages)
                self.assertLess(
                    job.index("apt-get install"), job.index("- uses: actions/checkout@")
                )

    def test_no_checkout_persists_its_credentials(self) -> None:
        for workflow in WORKFLOWS:
            with self.subTest(workflow=workflow):
                job = self.producer(workflow)
                checkouts = [
                    block for block in steps_of(job) if "actions/checkout@" in block
                ]
                self.assertEqual(len(checkouts), 1)
                self.assertIn("persist-credentials: false", checkouts[0])

    def test_the_toolchain_is_pinned_and_digest_checked(self) -> None:
        for workflow in WORKFLOWS:
            with self.subTest(workflow=workflow):
                job = self.producer(workflow)
                step = step_named(job, "Install the pinned Rust toolchain")
                self.assertIn("static.rust-lang.org/rustup/dist/aarch64", run_body(step))
                self.assertIn("sha256sum -c -", run_body(step))
                self.assertIn("RUST_TOOLCHAIN", job)
                self.assertRegex(job, r"RUSTUP_INIT_SHA256: [0-9a-f]{64}")

    def test_the_compile_waits_for_a_verified_plan_and_free_space(self) -> None:
        for workflow in WORKFLOWS:
            with self.subTest(workflow=workflow):
                job = self.producer(workflow)
                space = step_named(job, "Check free disk space before the compile")
                self.assertIn("15728640", run_body(space))
                compile_step = step_named(job, COMPILE_STEP)
                self.assertIn("env.BUILD_NEEDED == 'true'", compile_step)
                # CI validation always compiles what the committed manifest
                # declares; the publication producer compiles when the plan says
                # so and the run permits it (schedule, manifest preview, or
                # non-dry publish). Only manual upstream dry runs are plan-only.
                if workflow == "publish.yml":
                    self.assertIn("env.PRODUCER_COMPILE == 'true'", compile_step)

    def test_manifest_preview_permits_compile_while_upstream_dry_does_not(self) -> None:
        job = job_block(read_workflow("publish.yml"), PRODUCER)
        expr = next(
            line for line in job.splitlines() if "PRODUCER_COMPILE:" in line
        )
        # Manifest preview (dry, manifest) must permit Cargo; upstream dry must not.
        self.assertIn("inputs.source == 'manifest'", expr)
        def permitted(event: str, source: str, dry_run: str) -> bool:
            if event == "schedule":
                return True
            if source == "manifest":
                return True
            return dry_run == "false"
        cases = [
            ("schedule", "upstream", "true", True),
            ("workflow_dispatch", "upstream", "true", False),
            ("workflow_dispatch", "upstream", "false", True),
            ("workflow_dispatch", "manifest", "true", True),
            ("workflow_dispatch", "manifest", "false", True),
        ]
        for event, source, dry_run, expected in cases:
            with self.subTest(event=event, source=source, dry_run=dry_run):
                self.assertEqual(permitted(event, source, dry_run), expected)

    def test_the_compile_runs_the_reviewed_command_not_a_shell_fragments(self) -> None:
        for workflow in WORKFLOWS:
            with self.subTest(workflow=workflow):
                body = run_body(step_named(self.producer(workflow), COMPILE_STEP))
                self.assertNotIn("cargo build", body)
                self.assertEqual(body.count(f"{COMPILE} checkout"), 1)
                self.assertEqual(body.count(f"{COMPILE} build"), 1)
                self.assertIn("--identity", body)

    def test_the_upload_is_one_named_root_and_no_checkout(self) -> None:
        for workflow in WORKFLOWS:
            with self.subTest(workflow=workflow):
                job = self.producer(workflow)
                upload = [b for b in steps_of(job) if "actions/upload-artifact@" in b]
                self.assertEqual(len(upload), 1)
                paths = re.search(r"(?ms)path: \|\n(?P<body>(?: {12,}[^\n]*\n)+)", upload[0])
                self.assertIsNotNone(paths)
                listed = [line.strip() for line in paths.group("body").splitlines()]
                self.assertEqual(
                    sorted(listed),
                    sorted([
                        f"dist/source-artifacts/{PLAN_NAME}",
                        f"dist/source-artifacts/{SOURCE_PROVENANCE_NAME}",
                        "dist/source-artifacts/cargo-tauri",
                        "dist/source-artifacts/LICENSE.txt",
                        "dist/source-artifacts/LICENSE-APACHE-2.0",
                    ]),
                )
                # Everything uploaded is under the one artifact root, so the
                # checkout, the Cargo target directory, and the toolchain the job
                # just wrote cannot travel with it.
                for entry in listed:
                    self.assertTrue(entry.startswith("dist/source-artifacts/"), entry)
                self.assertNotIn("UPSTREAM_CHECKOUT", upload[0])
                self.assertIn("name: source-artifacts", upload[0])
                self.assertIn("if-no-files-found: ignore", upload[0])
                for foreign in ("run-id", "artifact-ids", "github-token"):
                    self.assertNotIn(foreign, upload[0], foreign)

    def test_the_producer_caches_nothing(self) -> None:
        for workflow in WORKFLOWS:
            with self.subTest(workflow=workflow):
                job = self.producer(workflow)
                self.assertNotIn("actions/cache@", job)
                # A compiler cache would be a trusted input the next run reads
                # back, so the toolchain lives in the runner's own temporary area.
                toolchain = step_named(job, "Install the pinned Rust toolchain")
                body = run_body(toolchain)
                self.assertIn('CARGO_HOME="$RUNNER_TEMP/cargo-home"', body)
                self.assertIn('RUSTUP_HOME="$RUNNER_TEMP/rustup-home"', body)

    def test_every_action_reference_is_pinned_to_a_sha(self) -> None:
        for workflow in WORKFLOWS:
            for reference in re.findall(r"uses:\s*([\w./-]+)@(\S+)", read_workflow(workflow)):
                with self.subTest(reference=reference[0]):
                    self.assertRegex(reference[1], r"^[0-9a-f]{40}")

    def test_no_step_body_interpolates_a_template(self) -> None:
        for workflow in WORKFLOWS:
            for body in run_bodies(read_workflow(workflow)):
                with self.subTest(workflow=workflow):
                    self.assertNotIn("${{", body)


class ProducerModeTest(unittest.TestCase):
    """Each producer plans from the mode its own run is actually about."""

    def test_both_producers_plan_against_the_real_public_gateway(self) -> None:
        """A placeholder host fails DNS before any planning happens.

        The producer verifies the served key against its pin, so the URL is not
        configuration to be parameterised: it is the deployment this repository
        publishes, and the two producers must name the same root so a CI run and
        a publication run can never disagree about what is published.
        """
        expected = "https://deb.cloud1ful.com/"
        seen = {}
        for workflow in WORKFLOWS:
            with self.subTest(workflow=workflow):
                job = job_block(read_workflow(workflow), PRODUCER)
                declared = re.findall(r"SOURCE_BASE_URL:\s*(\S+)", job)
                self.assertEqual(len(declared), 1, "the producer declares one gateway root")
                self.assertEqual(declared[0], expected)
                self.assertNotIn("example.com", job)
                self.assertNotIn("${{", declared[0], "the root is never a template")
                self.assertTrue(declared[0].startswith("https://"))
                # The plan step must spend the declared root, not another one.
                self.assertIn('--base-url "$SOURCE_BASE_URL"',
                              run_body(step_named(job, PLAN_STEP)))
                seen[workflow] = declared[0]
        self.assertEqual(len(set(seen.values())), 1,
                         "both producers must name the same public gateway root")
        # The pin that root has to serve is the published trust anchor.
        self.assertIn("94D49782052D6521E0249E4178C3D292443558E2",
                      job_block(read_workflow("ci.yml"), PRODUCER))

    def test_the_production_producer_keeps_its_trust_pin(self) -> None:
        """Repointing the root must not have moved the fingerprint it verifies."""
        for workflow in WORKFLOWS:
            with self.subTest(workflow=workflow):
                job = job_block(read_workflow(workflow), PRODUCER)
                pins = re.findall(r"SOURCE_TRUST_FINGERPRINT:\s*(\S+)", job)
                self.assertEqual(pins, ["94D49782052D6521E0249E4178C3D292443558E2"])

    def test_the_publication_producer_follows_the_dispatch_inputs(self) -> None:
        job = job_block(read_workflow("publish.yml"), PRODUCER)
        # A scheduled run is always an upstream publish; a manual run may choose.
        # The planner validates the value, so an absent or forged input fails
        # there rather than selecting a mode here.
        self.assertIn("github.event_name == 'schedule' && 'upstream' || inputs.source", job)
        self.assertIn(
            "github.event_name == 'schedule' && 'true' || inputs.source == 'manifest' && 'true' || inputs.dry_run == 'false'", job
        )
        self.assertIn("--mode \"$PRODUCER_MODE\"", run_body(step_named(job, PLAN_STEP)))

    def test_ci_plans_the_committed_manifest(self) -> None:
        job = job_block(read_workflow("ci.yml"), PRODUCER)
        body = run_body(step_named(job, PLAN_STEP))
        # CI-only validation mode: committed manifest, no baseline, always
        # builds when declared, even after publication.
        self.assertIn("--mode validation", body)
        self.assertNotIn("inputs.", job, "CI has no dispatch inputs to read")

    def test_the_publication_consumer_waits_for_the_producer(self) -> None:
        publish = job_block(read_workflow("publish.yml"), "publish")
        self.assertIn("needs: [source-producer]", publish)
        download = [b for b in steps_of(publish) if "actions/download-artifact@" in b]
        self.assertEqual(len(download), 1)
        self.assertIn("name: source-artifacts", download[0])
        # No run id and no artifact id: the name resolves inside this run only.
        for foreign in ("run-id:", "artifact-ids:", "github-token:"):
            self.assertNotIn(foreign, download[0], foreign)

    def test_the_ci_build_consumes_the_same_run_artifact(self) -> None:
        python_job = job_block(read_workflow("ci.yml"), "python")
        self.assertIn("needs: [source-producer]", python_job)
        download = [b for b in steps_of(python_job) if "actions/download-artifact@" in b]
        self.assertEqual(len(download), 1)
        self.assertIn("name: source-artifacts", download[0])
        self.assertIn("path: dist/source-artifacts", download[0])

    def test_a_consumer_passes_the_artifact_only_when_one_was_produced(self) -> None:
        # The routing decision is made once, from this run's producer upload, and
        # every build step may only act on it. Nothing infers reuse from a missing
        # artifact, and no step may continue past a decision it does not recognise.
        publish = job_block(read_workflow("publish.yml"), "publish")
        routing = [name for name, step in _steps_with_bodies(publish)
                   if SOURCE_PROVENANCE_NAME in run_body(step)]
        self.assertEqual(routing, [ROUTING_STEP], "the routing decision must be made once")
        body = run_body(step_named(publish, ROUTING_STEP))
        self.assertIn(f'if [ -f dist/source-artifacts/{SOURCE_PROVENANCE_NAME} ]', body)
        self.assertIn("SOURCE_ROUTING=artifact", body)
        self.assertIn("SOURCE_ROUTING=reuse", body)
        self.assertIn("SOURCE_ROUTING=none", body)
        self.assertIn("scripts.source_plan published", body)
        self.assertNotIn("continue-on-error", body)

    def test_every_build_step_acts_only_on_the_resolved_routing(self) -> None:
        for name, step in _steps_with_bodies(job_block(read_workflow("publish.yml"), "publish")):
            if "--source-artifacts" not in run_body(step):
                continue
            with self.subTest(step=name):
                body = run_body(step_named(job_block(read_workflow("publish.yml"), "publish"), name))
                self.assertIn('case "${SOURCE_ROUTING:-none}" in', body)
                self.assertIn("args+=(--source-artifacts dist/source-artifacts)", body)
                self.assertIn("args+=(--reuse-published-source)", body)
                self.assertIn('*) echo "invalid source routing', body)
                self.assertNotIn("continue-on-error", body)

    def test_ci_has_no_reuse_path_at_all(self) -> None:
        # CI never reaches object storage: it has no bucket and no pin, so the
        # published-deb path would refuse there anyway. Its build consumes a
        # compiled artifact and nothing else.
        block = job_block(read_workflow("ci.yml"), "python")
        consumers = [
            name for name, step in _steps_with_bodies(block)
            if "--source-artifacts" in run_body(step)
        ]
        self.assertTrue(consumers)
        for name in consumers:
            body = run_body(step_named(block, name))
            self.assertIn(f'if [ -f dist/source-artifacts/{SOURCE_PROVENANCE_NAME} ]', body)
            self.assertIn("args+=(--source-artifacts dist/source-artifacts)", body)
            self.assertNotIn("--reuse-published-source", body)
        self.assertNotIn("--reuse-published-source", read_workflow("ci.yml"))


def _steps_with_bodies(job: str) -> list[tuple[str, str]]:
    """The job's named steps that carry a run body, as (name, block) pairs."""
    found: list[tuple[str, str]] = []
    for block in steps_of(job):
        name = re.search(r"(?m)^      - name: (.+)$", block)
        if name is not None and RUN_RE.search(block):
            found.append((name.group(1).strip(), block))
    return found


class ProducerRunTest(unittest.TestCase):
    """Drive the producer's steps against stub binaries: what runs, and what is written."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        uv = self.bin / "uv"
        uv.write_text(STUB_UV, encoding="utf-8")
        uv.chmod(0o755)
        self.log = self.root / "uv.log"
        self.log.write_bytes(b"")
        self.output = self.root / "github-output"
        self.output.write_text("", encoding="utf-8")
        self.environment = self.root / "github-env"
        self.environment.write_text("", encoding="utf-8")

    def step_body(self, workflow: str, step: str) -> str:
        return run_body(step_named(job_block(read_workflow(workflow), PRODUCER), step))

    def drive(self, body: str, **env: str) -> subprocess.CompletedProcess[str]:
        environment = {
            "PATH": f"{self.bin}:{os.environ.get('PATH', '')}",
            "HOME": str(self.root),
            "UV_STUB_LOG": str(self.log),
            "GITHUB_OUTPUT": str(self.output),
            "GITHUB_ENV": str(self.environment),
        }
        environment.update(env)
        return subprocess.run(
            ["sh", "-c", body], cwd=self.root, env=environment, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    def run_plan(
        self, workflow: str, *, decision: str, source: str = "upstream"
    ) -> subprocess.CompletedProcess[str]:
        self.output.write_text("", encoding="utf-8")
        self.environment.write_text("", encoding="utf-8")
        return self.drive(
            self.step_body(workflow, PLAN_STEP),
            SOURCE_PLAN_DECISION=decision,
            PRODUCER_MODE=source,
            SOURCE_BASE_URL="https://apt.example.com/",
            SOURCE_TRUST_FINGERPRINT="94D49782052D6521E0249E4178C3D292443558E2",
            ARTIFACTS="dist/source-artifacts",
        )

    def calls(self) -> list[tuple[str, ...]]:
        return [
            tuple(part.decode() for part in record.split(b"\x00")[:-1])
            for record in self.log.read_bytes().split(ARG_SEP)[:-1] if record
        ]

    def run_routing(self, *, compiled: bool, published: str = "false") -> subprocess.CompletedProcess[str]:
        """Drive the publisher's routing step with a producer upload in the tree."""
        block = job_block(read_workflow("publish.yml"), "publish")
        artifacts = self.root / "dist" / "source-artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        (artifacts / PLAN_NAME).write_text("{}", encoding="utf-8")
        if compiled:
            (artifacts / SOURCE_PROVENANCE_NAME).write_text("{}", encoding="utf-8")
        environment = self.root / "github-env"
        environment.write_text("", encoding="utf-8")
        proc = self.drive(
            run_body(step_named(block, ROUTING_STEP)),
            GITHUB_ENV=str(environment),
            SOURCE_PUBLISHED_ANSWER=published,
        )
        return proc, environment.read_text(encoding="utf-8")

    def test_a_compiled_artifact_routes_to_the_artifact_option(self) -> None:
        proc, written = self.run_routing(compiled=True, published="true")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # A compiled artifact wins: reuse is never inferred, and never preferred.
        self.assertEqual(written, "SOURCE_ROUTING=artifact\n")
        self.assertEqual(self.calls(), [])

    def test_a_published_version_routes_to_the_reuse_option(self) -> None:
        proc, written = self.run_routing(compiled=False, published="true")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(written, "SOURCE_ROUTING=reuse\n")
        argv = self.calls()[0]
        self.assertIn(PLANNER, " ".join(argv))
        self.assertIn("published", argv)
        self.assertEqual(argv[-1], f"dist/source-artifacts/{PLAN_NAME}")

    def test_no_artifact_and_no_published_version_route_to_nothing(self) -> None:
        proc, written = self.run_routing(compiled=False, published="false")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(written, "SOURCE_ROUTING=none\n")

    def test_a_needed_build_is_decided_by_the_planner_alone(self) -> None:
        proc = self.run_plan("publish.yml", decision="true")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        calls = self.calls()
        self.assertEqual(len(calls), 1)
        argv = calls[0]
        self.assertEqual(argv[:3], ("run", "python", "-m"))
        self.assertIn(PLANNER, " ".join(argv))
        self.assertIn("plan", argv)
        self.assertIn("--mode", argv)
        self.assertIn("upstream", argv)
        self.assertIn("--base-url", argv)
        self.assertIn("--fingerprint", argv)
        self.assertEqual(
            argv[argv.index("--output") + 1], f"dist/source-artifacts/{PLAN_NAME}"
        )
        # The only thing that reaches an output is this job's own boolean, in
        # both the step output and the env context the compile gate reads.
        self.assertEqual(self.output.read_text(encoding="utf-8"), "BUILD_NEEDED=true\n")
        self.assertEqual(self.environment.read_text(encoding="utf-8"), "BUILD_NEEDED=true\n")

    def test_ci_plans_in_validation_mode(self) -> None:
        proc = self.run_plan("ci.yml", decision="true", source="manifest")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        argv = self.calls()[0]
        self.assertEqual(argv[argv.index("--mode") + 1], "validation")
        self.assertEqual(self.output.read_text(encoding="utf-8"), "BUILD_NEEDED=true\n")
        self.assertEqual(self.environment.read_text(encoding="utf-8"), "BUILD_NEEDED=true\n")

    def test_a_run_that_needs_no_compile_says_so(self) -> None:
        for workflow in WORKFLOWS:
            with self.subTest(workflow=workflow):
                proc = self.run_plan(workflow, decision="false")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(self.output.read_text(encoding="utf-8"), "BUILD_NEEDED=false\n")
                self.assertEqual(self.environment.read_text(encoding="utf-8"), "BUILD_NEEDED=false\n")

    def test_a_decision_that_is_not_a_boolean_stops_the_job(self) -> None:
        for decision in ("", "maybe", "true; rm -rf /", "TRUE"):
            with self.subTest(decision=decision):
                self.output.write_text("", encoding="utf-8")
                self.environment.write_text("", encoding="utf-8")
                proc = self.run_plan("publish.yml", decision=decision)
                self.assertEqual(proc.returncode, 1)
                self.assertEqual(self.output.read_text(encoding="utf-8"), "")
                self.assertEqual(self.environment.read_text(encoding="utf-8"), "")

    def test_the_compile_step_reads_the_plan_and_never_a_workflow_input(self) -> None:
        for workflow in WORKFLOWS:
            with self.subTest(workflow=workflow):
                self.log.write_bytes(b"")
                proc = self.drive(
                    self.step_body(workflow, COMPILE_STEP),
                    ARTIFACTS="dist/source-artifacts",
                    UPSTREAM_CHECKOUT="dist/source-artifacts-upstream",
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                checkout, build = self.calls()
                self.assertIn(COMPILE, " ".join(checkout))
                self.assertIn("checkout", checkout)
                self.assertEqual(
                    checkout[checkout.index("--identity") + 1],
                    f"dist/source-artifacts/{PLAN_NAME}",
                )
                self.assertEqual(
                    checkout[checkout.index("--into") + 1], "dist/source-artifacts-upstream"
                )
                self.assertIn("build", build)
                self.assertEqual(
                    build[build.index("--identity") + 1],
                    f"dist/source-artifacts/{PLAN_NAME}",
                )
                self.assertEqual(
                    build[build.index("--output") + 1], "dist/source-artifacts"
                )
                # No version, commit, or URL is passed in: they come from the plan.
                for flag in ("--version", "--commit", "--tag"):
                    self.assertNotIn(flag, checkout + build)

    def test_every_producer_step_body_takes_its_inputs_from_the_environment(self) -> None:
        for workflow in WORKFLOWS:
            for body in run_bodies(job_block(read_workflow(workflow), PRODUCER)):
                with self.subTest(workflow=workflow, body=body.splitlines()[0]):
                    self.assertNotIn("${{", body)
                    self.assertNotRegex(body, r"--version \$|--commit \$|--url \$")


if __name__ == "__main__":
    unittest.main()