"""Structural assertions for the Tauri CLI candidate CI jobs.

The candidate is deliberately fenced off from the published pipeline, and CI is
where that fence is easiest to erode by accident: a later edit that reuses the
production artifact, signs the candidate, installs the upstream source, or points
a native job at the wrong architecture would not fail any build. These tests read
`ci.yml` as text and pin the properties that must hold, including the ones that
only matter because upstream publishes no Linux arm64 release asset: the arm64
job has to compile the pinned commit, and nothing may claim an arm64 asset URL.
"""

import os
import re
import unittest
from pathlib import Path

from scripts.tauri_candidate_metadata import REVIEWED_COMMIT, REVIEWED_TOOLCHAIN

ROOT = Path(__file__).resolve().parent.parent
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

BUILD_JOBS = ("tauri-candidate-amd64", "tauri-candidate-arm64")
SMOKE_JOBS = ("tauri-candidate-smoke-amd64", "tauri-candidate-smoke-arm64")
CANDIDATE_JOBS = BUILD_JOBS + SMOKE_JOBS
PRODUCTION_ARTIFACT = "apt-repo-amd64"
DEB = "cargo-tauri_2.12.2-1_{arch}.deb"


def job_block(job: str) -> str:
    """Slice one top-level job (two-space indent) out of the workflow."""
    start = CI.index(f"\n  {job}:")
    rest = CI[start + 1:]
    match = re.search(r"\n  [A-Za-z0-9_-]+:", rest[1:])
    return rest[: match.start() + 1] if match else rest


def step_order(job: str) -> list[str]:
    """The names of a job's steps, in the order the workflow runs them."""
    block = job_block(job)
    return re.findall(r"^      - (?:name: (.+)|uses: ([^\s]+))$", block, re.MULTILINE)


def installed_packages(job: str) -> list[str]:
    """Package names from a job's `apt-get install` lines, flags stripped."""
    packages: list[str] = []
    for line in job_block(job).splitlines():
        if "apt-get install" not in line:
            continue
        for token in line.split("apt-get install", 1)[1].split():
            if token.startswith("-") or token == "&&":
                if token == "&&":
                    break
                continue
            packages.append(token)
    return packages


class CandidateJobsExistTest(unittest.TestCase):
    def test_the_four_candidate_jobs_are_present(self) -> None:
        for job in CANDIDATE_JOBS:
            self.assertIn(f"\n  {job}:", CI, job)
    def test_each_candidate_job_builds_on_debian_13(self) -> None:
        for job in CANDIDATE_JOBS:
            self.assertIn("container: debian:13", job_block(job), job)

    def test_the_amd64_build_job_uses_the_public_uv_index(self) -> None:
        build = job_block("tauri-candidate-amd64")
        self.assertIn("UV_DEFAULT_INDEX: https://pypi.org/simple", build)
        self.assertIn("uv lock --check", build)
        self.assertIn("uv sync --frozen", build)

    def test_the_amd64_build_job_builds_only_the_official_asset(self) -> None:
        build = job_block("tauri-candidate-amd64")
        self.assertIn(
            "python -m scripts.tauri_candidate --arch amd64 --output dist/tauri-candidate", build,
        )
        self.assertNotIn("--binary", build)
        self.assertNotIn("--provenance", build)

    def test_the_arm64_build_job_compiles_the_pinned_commit_natively(self) -> None:
        build = job_block("tauri-candidate-arm64")
        self.assertIn("runs-on: ubuntu-24.04-arm", build)
        self.assertIn("repository: tauri-apps/tauri", build)
        self.assertIn(REVIEWED_COMMIT, build)
        self.assertIn("cargo build --locked --manifest-path crates/tauri-cli/Cargo.toml", build)
        self.assertIn("--target aarch64-unknown-linux-gnu", build)
        self.assertIn("--profile release-size-optimized --bin cargo-tauri", build)
        self.assertIn("timeout 45m", build)

    def test_the_arm64_build_job_installs_only_the_pinned_toolchain(self) -> None:
        build = job_block("tauri-candidate-arm64")
        self.assertIn(f"RUST_TOOLCHAIN: {REVIEWED_TOOLCHAIN}", build)
        self.assertIn("https://static.rust-lang.org/rustup/dist/", build)
        self.assertIn("sha256sum -c -", build)
        self.assertIn("--no-modify-path", build)
        # The toolchain lives in the runner's own temporary directories; nothing
        # may write into a shared or user-level Cargo or rustup home.
        self.assertIn('CARGO_HOME="$RUNNER_TEMP/cargo-home"', build)
        self.assertIn('RUSTUP_HOME="$RUNNER_TEMP/rustup-home"', build)
        self.assertNotIn("~/.cargo", build)
        self.assertNotIn("sudo ", build)

    def test_the_installer_step_puts_the_tools_on_this_shells_path(self) -> None:
        """`--no-modify-path` and `GITHUB_PATH` leave the installing shell blind."""
        step = job_block("tauri-candidate-arm64").split(
            "- name: Install the pinned Rust toolchain"
        )[1].split("- name:")[0]
        before_probe = step.split("rustc -Vv")[0]
        self.assertRegex(before_probe, r'export PATH="\$CARGO_HOME/bin:\$PATH"')
        self.assertLess(before_probe.index("export PATH="), step.index("rustc -Vv"))

    def test_the_arm64_build_job_checks_free_disk_before_compiling(self) -> None:
        build = job_block("tauri-candidate-arm64")
        self.assertIn("df -Pk /", build)
        self.assertIn("15728640", build)
        self.assertLess(build.index("free disk space"), build.index("cargo build --locked"))

    def test_the_arm64_build_job_records_the_build_it_performed(self) -> None:
        build = job_block("tauri-candidate-arm64")
        self.assertIn("python3 -m scripts.tauri_candidate_provenance emit-source-build", build)
        self.assertIn("--rustc \"$(rustc -Vv)\"", build)
        self.assertIn("--cargo \"$(cargo -V)\"", build)
        self.assertIn(
            "python3 -m scripts.tauri_candidate --arch arm64 --output dist/tauri-candidate \\\n"
            "            --binary \"$ARTIFACT\" --provenance dist/tauri-candidate/build-provenance.json",
            build,
        )

    def test_each_build_job_packages_twice_and_compares_the_bytes(self) -> None:
        for job in BUILD_JOBS:
            block = job_block(job)
            self.assertIn("sha256sum <dist/tauri-candidate/", block, job)
            self.assertIn("dist/tauri-candidate-rebuild", block, job)

    def test_the_arm64_build_job_does_not_install_the_upstream_source(self) -> None:
        """The arm64 package carries a record and licences, never the checkout."""
        block = job_block("tauri-candidate-arm64")
        upload = block.split("actions/upload-artifact@")[1]
        self.assertIn("dist/tauri-candidate/cargo-tauri_2.12.2-1_arm64.deb", upload)
        self.assertIn("dist/tauri-candidate/build-provenance.json", upload)
        self.assertIn("dist/tauri-candidate/licenses/tauri-cli/LICENSE-MIT.txt", upload)
        self.assertNotIn("dist/tauri-candidate/upstream", upload)
        self.assertNotIn("target/", upload)

    def test_the_arm64_upload_stays_under_one_directory(self) -> None:
        """upload-artifact roots multiple paths at their least common ancestor.

        A path outside `dist/tauri-candidate` would root the archive at the
        repository, and the smoke job would then look for the deb one directory
        deeper than the one apt installs.
        """
        block = job_block("tauri-candidate-arm64")
        upload = block.split("actions/upload-artifact@")[1]
        paths = re.search(r"          path: \|\n((?:            .+\n)+)", upload)
        self.assertIsNotNone(paths)
        inputs = [Path(line.strip()) for line in paths.group(1).splitlines()]
        root = Path(os.path.commonpath([str(path.parent) for path in inputs]))
        self.assertEqual(root, Path("dist/tauri-candidate"))
        downloaded = Path("dist/tauri-candidate") / inputs[0].relative_to(root)
        self.assertEqual(downloaded, Path("dist/tauri-candidate/cargo-tauri_2.12.2-1_arm64.deb"))
        self.assertIn("cp packaging/licenses/tauri-cli/", block)


class CandidateArtifactTest(unittest.TestCase):
    def test_each_architecture_gets_its_own_artifact(self) -> None:
        for arch in ("amd64", "arm64"):
            self.assertIn(f"name: tauri-candidate-{arch}", job_block(f"tauri-candidate-{arch}"))

    def test_no_candidate_job_touches_the_production_artifact(self) -> None:
        for job in CANDIDATE_JOBS:
            self.assertNotIn(PRODUCTION_ARTIFACT, job_block(job), job)

    def test_each_smoke_job_downloads_only_its_own_candidate_artifact(self) -> None:
        for arch in ("amd64", "arm64"):
            block = job_block(f"tauri-candidate-smoke-{arch}")
            self.assertIn("actions/download-artifact@", block)
            self.assertIn(f"name: tauri-candidate-{arch}", block)

    def test_the_smoke_jobs_depend_on_their_own_build_job(self) -> None:
        for arch in ("amd64", "arm64"):
            block = job_block(f"tauri-candidate-smoke-{arch}")
            self.assertIn(f"needs: [tauri-candidate-{arch}]", block, arch)


class CandidateNativeTest(unittest.TestCase):
    def test_each_smoke_job_installs_only_its_own_architecture(self) -> None:
        for arch in ("amd64", "arm64"):
            block = job_block(f"tauri-candidate-smoke-{arch}")
            self.assertIn(f"./dist/tauri-candidate/{DEB.format(arch=arch)}", block)
            other = "arm64" if arch == "amd64" else "amd64"
            self.assertNotIn(DEB.format(arch=other), block, arch)

    def test_the_arm64_smoke_job_runs_on_an_arm64_runner(self) -> None:
        self.assertIn("runs-on: ubuntu-24.04-arm", job_block("tauri-candidate-smoke-arm64"))

    def test_each_smoke_job_runs_the_installed_candidate_natively(self) -> None:
        for arch in ("amd64", "arm64"):
            self.assertIn(
                f"python3 -m scripts.tauri_candidate_smoke --installed --arch {arch} "
                "--require-native --require-cargo",
                job_block(f"tauri-candidate-smoke-{arch}"),
            )

    def test_no_candidate_job_runs_a_foreign_binary(self) -> None:
        """QEMU or any other emulator would not be a native check."""
        for job in CANDIDATE_JOBS:
            block = job_block(job)
            for token in ("qemu", "binfmt", "docker run"):
                self.assertNotIn(token, block, f"{job} references {token}")


class CandidateFenceTest(unittest.TestCase):
    """The candidate must not reach into the published pipeline."""

    def test_no_candidate_job_signs_anything(self) -> None:
        for job in CANDIDATE_JOBS:
            block = job_block(job)
            self.assertNotIn("APT_SIGNING_KEY", block, job)
            self.assertNotIn("gpg", block, job)
            self.assertNotIn("--signing-key", block, job)

    def test_no_candidate_job_uses_a_secret_or_publishes(self) -> None:
        forbidden = ("secrets.", "publish.yml", "scripts.publish", "publish-tree",
                     "workflow_run", "repository_dispatch")
        for job in CANDIDATE_JOBS:
            block = job_block(job)
            for token in forbidden:
                self.assertNotIn(token, block, f"{job} references {token}")

    def test_no_candidate_job_builds_the_production_repository(self) -> None:
        for job in CANDIDATE_JOBS:
            self.assertNotIn("scripts.repo build", job_block(job), job)

    def test_no_candidate_job_registers_the_candidate_in_the_catalog(self) -> None:
        for job in CANDIDATE_JOBS:
            block = job_block(job)
            self.assertNotIn("packaging/manifest.json", block, job)
            self.assertNotIn("scripts.recipes", block, job)

    def test_no_candidate_job_claims_an_official_arm64_asset(self) -> None:
        """There is no upstream arm64 asset; naming one would be a false claim."""
        for job in CANDIDATE_JOBS:
            block = job_block(job)
            self.assertNotIn("cargo-tauri-aarch64-unknown-linux-gnu", block, job)
            self.assertNotIn("releases/download/tauri-cli-v2.12.2/cargo-tauri-aarch64", block, job)

    def test_every_action_reference_is_pinned_to_a_sha(self) -> None:
        for match in re.finditer(r"uses: ([^\s@]+)@([^\s#]+)", CI):
            name, ref = match.group(1), match.group(2)
            if name.startswith("./"):
                continue
            self.assertRegex(ref, r"^[0-9a-f]{40}$", f"{name}@{ref} is not pinned")

    def test_no_candidate_job_is_triggered_by_another_workflow(self) -> None:
        self.assertIn("workflow_dispatch", CI)
        for job in CANDIDATE_JOBS:
            self.assertNotIn("needs: [publish", job_block(job), job)


class GitAvailabilityTest(unittest.TestCase):
    """Real git has to exist before anything claims to verify a git object.

    Two environments decide this: the gate that runs the test suite, which
    shells out to git, and the ARM job, whose upstream checkout and provenance
    both go through git. Without it, actions/checkout silently falls back to a
    REST tarball, and `git rev-parse` later reports that there is no repository.
    """

    def test_the_python_gate_installs_git(self) -> None:
        packages = installed_packages("python")
        self.assertIn("git", packages, packages)
        self.assertIn("python3", packages, packages)

    def test_the_arm64_job_installs_git_in_its_system_tools(self) -> None:
        step = job_block("tauri-candidate-arm64").split("- name: Install system tools")[1]
        step = step.split("- name:")[0]
        self.assertIn("apt-get install", step)
        self.assertIn(" git ", f" {step} ")

    def test_the_arm64_job_installs_git_before_every_checkout(self) -> None:
        block = job_block("tauri-candidate-arm64")
        install = block.index("- name: Install system tools")
        first_checkout = block.index("      - uses: actions/checkout@")
        upstream = block.index("- name: Check out the pinned upstream source")
        self.assertLess(install, first_checkout)
        self.assertLess(install, upstream)
        self.assertLess(first_checkout, upstream)

    def test_the_upstream_checkout_still_asks_for_a_real_repository(self) -> None:
        block = job_block("tauri-candidate-arm64")
        upstream = block.split("- name: Check out the pinned upstream source")[1]
        upstream = upstream.split("- name:")[0]
        self.assertIn(f"ref: ${{{{ env.UPSTREAM_COMMIT }}}}", upstream)
        self.assertIn("persist-credentials: false", upstream)
        self.assertIn("fetch-depth: 1", upstream)

    def test_the_arm64_job_still_proves_the_commit_and_lockfile_with_git(self) -> None:
        """No fallback that asserts only the pinned commit without checking it."""
        block = job_block("tauri-candidate-arm64")
        self.assertIn("python3 -m scripts.tauri_candidate_provenance emit-source-build", block)
        self.assertIn("--upstream dist/tauri-candidate/upstream", block)
        for token in ("git init", "git commit", "git clone", "--allow-unverified",
                      "--skip-verification", "git config"):
            self.assertNotIn(token, block, token)


class ProductionJobsUntouchedTest(unittest.TestCase):
    """Adding the candidate jobs must not have changed the published ones."""

    def test_the_production_build_job_still_serves_the_manifest(self) -> None:
        python_job = job_block("python")
        self.assertIn("python -m scripts.repo build --output dist/repo", python_job)
        self.assertIn("python -m scripts.repo verify --output dist/repo", python_job)

    def test_the_production_smoke_jobs_still_use_their_own_artifact(self) -> None:
        for job in ("smoke-amd64", "smoke-arm64"):
            self.assertIn(f"name: {PRODUCTION_ARTIFACT}", job_block(job), job)

    def test_the_production_upgrade_jobs_are_still_present(self) -> None:
        for job in ("upgrade-amd64", "upgrade-arm64"):
            self.assertIn(f"\n  {job}:", CI)

    def test_the_bun_candidate_jobs_are_still_present(self) -> None:
        for job in ("bun-candidate", "bun-candidate-smoke-amd64", "bun-candidate-smoke-arm64"):
            self.assertIn(f"\n  {job}:", CI, job)


if __name__ == "__main__":
    unittest.main()
