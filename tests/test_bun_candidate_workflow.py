"""Structural assertions for the Bun candidate CI jobs.

The candidate is deliberately fenced off from the published pipeline, and CI is
where that fence is easiest to erode by accident: a later edit that reuses the
production artifact, signs the candidate, or points a native job at the wrong
architecture would not fail any build. These tests read `ci.yml` as text and pin
the properties that must hold.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

CANDIDATE_JOBS = ("bun-candidate", "bun-candidate-smoke-amd64", "bun-candidate-smoke-arm64")
PRODUCTION_ARTIFACT = "apt-repo-amd64"


def job_block(job: str) -> str:
    """Slice one top-level job (two-space indent) out of the workflow."""
    start = CI.index(f"\n  {job}:")
    rest = CI[start + 1:]
    match = re.search(r"\n  [A-Za-z0-9_-]+:", rest[1:])
    return rest[: match.start() + 1] if match else rest


class CandidateJobsExistTest(unittest.TestCase):
    def test_the_three_candidate_jobs_are_present(self) -> None:
        for job in CANDIDATE_JOBS:
            self.assertIn(f"\n  {job}:", CI, job)

    def test_each_candidate_job_builds_on_debian_13(self) -> None:
        for job in CANDIDATE_JOBS:
            self.assertIn("container: debian:13", job_block(job), job)

    def test_the_build_job_uses_the_public_uv_index(self) -> None:
        build = job_block("bun-candidate")
        self.assertIn("UV_DEFAULT_INDEX: https://pypi.org/simple", build)
        self.assertIn("uv lock --check", build)
        self.assertIn("uv sync --frozen", build)

    def test_the_build_job_invokes_the_candidate_cli(self) -> None:
        build = job_block("bun-candidate")
        self.assertIn("python -m scripts.bun_candidate --output dist/bun-candidate", build)


class CandidateArtifactTest(unittest.TestCase):
    def test_the_candidate_artifact_is_distinct_from_the_production_one(self) -> None:
        build = job_block("bun-candidate")
        self.assertIn("name: bun-candidate", build)
        self.assertNotIn(PRODUCTION_ARTIFACT, build)

    def test_the_build_job_uploads_the_candidate_artifact(self) -> None:
        build = job_block("bun-candidate")
        self.assertIn("actions/upload-artifact@", build)
        self.assertIn("path: dist/bun-candidate", build)

    def test_each_native_job_downloads_only_the_candidate_artifact(self) -> None:
        for job in CANDIDATE_JOBS[1:]:
            block = job_block(job)
            self.assertIn("actions/download-artifact@", block)
            self.assertIn("name: bun-candidate", block)
            self.assertNotIn(PRODUCTION_ARTIFACT, block)


class CandidateNativeTest(unittest.TestCase):
    def test_native_jobs_depend_on_the_candidate_build(self) -> None:
        for job in CANDIDATE_JOBS[1:]:
            self.assertIn("needs: [bun-candidate]", job_block(job), job)

    def test_each_native_job_installs_only_its_own_architecture(self) -> None:
        for arch in ("amd64", "arm64"):
            block = job_block(f"bun-candidate-smoke-{arch}")
            self.assertIn(f"./dist/bun-candidate/bun_1.4.3-1_{arch}.deb", block)
            other = "arm64" if arch == "amd64" else "amd64"
            self.assertNotIn(f"bun_1.4.3-1_{other}.deb", block, arch)

    def test_the_arm64_native_job_runs_on_an_arm64_runner(self) -> None:
        self.assertIn("runs-on: ubuntu-24.04-arm", job_block("bun-candidate-smoke-arm64"))

    def test_each_native_job_requires_a_native_run(self) -> None:
        for arch in ("amd64", "arm64"):
            block = job_block(f"bun-candidate-smoke-{arch}")
            self.assertIn(
                f"python3 -m scripts.bun_candidate_smoke --installed --arch {arch} --require-native",
                block,
            )


class CandidateFenceTest(unittest.TestCase):
    """The candidate must not reach into the published pipeline."""

    def test_no_candidate_job_signs_anything(self) -> None:
        for job in CANDIDATE_JOBS:
            block = job_block(job)
            self.assertNotIn("APT_SIGNING_KEY", block, job)
            self.assertNotIn("gpg", block, job)
            self.assertNotIn("--signing-key", block, job)

    def test_no_candidate_job_uses_a_secret_or_publishes(self) -> None:
        # The job *name* says "not published"; what matters is that no publish
        # step, workflow, or CLI entrypoint is actually invoked.
        forbidden = ("secrets.", "publish.yml", "scripts.publish", "publish-tree",
                     "workflow_run", "repository_dispatch")
        for job in CANDIDATE_JOBS:
            block = job_block(job)
            for token in forbidden:
                self.assertNotIn(token, block, f"{job} references {token}")

    def test_no_candidate_job_builds_the_production_repository(self) -> None:
        for job in CANDIDATE_JOBS:
            self.assertNotIn("scripts.repo build", job_block(job), job)

    def test_every_action_reference_is_pinned_to_a_sha(self) -> None:
        for match in re.finditer(r"uses: ([^\s@]+)@([^\s#]+)", CI):
            name, ref = match.group(1), match.group(2)
            if name.startswith("./"):
                continue
            self.assertRegex(ref, r"^[0-9a-f]{40}$", f"{name}@{ref} is not pinned")


class ProductionJobsUntouchedTest(unittest.TestCase):
    """Adding the candidate must not have changed the published jobs."""

    def test_the_production_build_job_still_serves_the_manifest(self) -> None:
        python_job = job_block("python")
        self.assertIn("python -m scripts.repo build --output dist/repo", python_job)
        self.assertIn("python -m scripts.repo verify --output dist/repo", python_job)

    def test_production_smoke_jobs_still_use_their_own_artifact(self) -> None:
        for job in ("smoke-amd64", "smoke-arm64"):
            self.assertIn(f"name: {PRODUCTION_ARTIFACT}", job_block(job), job)

    def test_the_production_upgrade_jobs_are_still_present(self) -> None:
        for job in ("upgrade-amd64", "upgrade-arm64"):
            self.assertIn(f"\n  {job}:", CI)


if __name__ == "__main__":
    unittest.main()