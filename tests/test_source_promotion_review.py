"""Independent issue-71 regressions at production orchestration seams."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import update
from scripts.source_artifacts import production_reviewed_notices, load_source_artifacts
from scripts.package_licenses import cargo_tauri_source_document
from scripts.manifest import parse_manifest
from scripts.tauri_source import native_binary
from scripts.tauri_candidate_smoke import native_checks
from tests.test_source_artifacts import artifact_directory, pinned
from tests.test_source_build import source_manifest_document
from tests.test_source_workflows import (PRODUCER, PLAN_STEP, COMPILE_STEP,
    STUB_UV, job_block, read_workflow, step_named, run_body)


class PromotionReviewTest(unittest.TestCase):
    def test_updater_discovers_production_source_package(self):
        import base64
        from scripts.tauri_source import git_blob_id
        from tests.test_source_artifacts import LOCK, COMMIT

        class OfficialRelease:
            """Protocol-complete client: canonical CLI selection never reads repo-wide latest."""
            def get_json(self, url):
                if url.endswith("/releases/latest"):
                    raise AssertionError(
                        "canonical CLI discovery must not use repository-wide latest"
                    )
                if "/tags?" in url:
                    return [{"name": "tauri-v2.10.0"}, {"name": "tauri-cli-v2.12.2"}]
                if "/releases/tags/tauri-cli-v2.12.2" in url:
                    name = "cargo-tauri-x86_64-unknown-linux-gnu.tgz"
                    return {"tag_name": "tauri-cli-v2.12.2", "draft": False,
                            "prerelease": False, "assets": [{"name": name,
                            "browser_download_url": "https://github.com/tauri-apps/tauri/releases/download/tauri-cli-v2.12.2/" + name,
                            "digest": "sha256:" + "a" * 64}]}
                if "/git/ref/tags/tauri-cli-v2.12.2" in url:
                    return {"object": {"type": "commit", "sha": COMMIT}}
                if "/contents/Cargo.lock" in url:
                    return {"type": "file", "sha": git_blob_id(LOCK), "size": len(LOCK),
                            "encoding": "base64", "content": base64.b64encode(LOCK).decode()}
                raise AssertionError(url)

            def get_bytes(self, url, *, max_bytes):
                vendor = Path("packaging/licenses/cargo-tauri")
                if url.endswith("/LICENSE-MIT"):
                    return (vendor / "LICENSE.txt").read_bytes()
                if url.endswith("/LICENSE-APACHE-2.0"):
                    return (vendor / "LICENSE-APACHE-2.0").read_bytes()
                raise AssertionError(url)

        # Exercise the real deciding layer, not a patched discover() candidate.
        with mock.patch.object(update, "RECIPES", {"cargo-tauri": object()}):
            result = update._discover(OfficialRelease())
        self.assertIn("arm64", result["cargo-tauri"].sources)
        self.assertEqual(result["cargo-tauri"].version, "2.12.2")
        self.assertEqual(result["cargo-tauri"].sources["arm64"].commit, COMMIT)
        self.assertIn("amd64", result["cargo-tauri"].targets)

    def test_planner_decision_reaches_compile_environment(self):
        for workflow in ("ci.yml", "publish.yml"):
            with self.subTest(workflow=workflow), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                uv = root / "uv"
                uv.write_text(STUB_UV)
                uv.chmod(0o755)
                output, environment = root / "output", root / "environment"
                output.touch()
                environment.touch()
                job = job_block(read_workflow(workflow), PRODUCER)
                env = {"PATH": f"{root}:{os.environ['PATH']}",
                       "GITHUB_OUTPUT": str(output), "GITHUB_ENV": str(environment),
                       "UV_STUB_LOG": str(root / "calls"),
                       "SOURCE_PLAN_DECISION": "true", "PRODUCER_MODE": "manifest",
                       "SOURCE_BASE_URL": "https://apt.example.com/",
                       "SOURCE_TRUST_FINGERPRINT": "94D49782052D6521E0249E4178C3D292443558E2",
                       "ARTIFACTS": "dist/source-artifacts"}
                proc = subprocess.run(["bash", "-c", run_body(step_named(job, PLAN_STEP))],
                                      cwd=root, env=env, capture_output=True, text=True)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("env.BUILD_NEEDED", step_named(job, COMPILE_STEP))
                self.assertIn("BUILD_NEEDED=true", environment.read_text(),
                              "step outputs do not populate the Actions env context")

    def test_relative_producer_checkout_supports_isolated_native_probe(self):
        # Both workflows pass a relative --upstream. No compiler or ARM payload runs.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, identity = artifact_directory(root)
            upstream = Path(os.path.relpath(root / "checkout", Path.cwd()))
            binary = native_binary(upstream, identity)
            binary.parent.mkdir(parents=True)
            binary.write_text("#!/bin/sh\nprintf 'tauri-cli 2.12.2\\n'\n")
            binary.chmod(0o755)
            home = root / "isolated-home"
            home.mkdir()
            self.assertEqual(native_checks(binary, "2.12.2", home, None,
                                           require_cargo=False)[0],
                             "cargo-tauri --version -> tauri-cli 2.12.2")

    def test_old_twenty_pins_and_fixture_debs_match_base(self):
        import json
        import types
        from scripts import build
        from scripts.manifest import load_manifest
        from tests.test_build import _write_build_archive

        base = "6e098cdc320184bab8dab55f5429c2cfc9ec3401"
        def at_base(path):
            return subprocess.check_output(["git", "show", f"{base}:{path}"], text=True)
        old_data = json.loads(at_base("packaging/manifest.json"))
        current_data = json.loads(Path("packaging/manifest.json").read_text())
        current_entries = {p["name"]: p for p in current_data["packages"]}
        for entry in old_data["packages"]:
            self.assertEqual(entry, current_entries[entry["name"]])
        baseline = types.ModuleType("scripts._review_baseline_build")
        exec(at_base("scripts/build.py"), baseline.__dict__)
        manifest = parse_manifest(old_data)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "cache"
            cache.mkdir()
            for pkg in manifest.packages:
                for arch in ("amd64", "arm64"):
                    _write_build_archive(cache / f"{pkg.name}-{pkg.version}-{arch}.tar.gz",
                                         pkg, arch, f"review-{pkg.name}-{arch}".encode())
            with mock.patch.object(build, "fetch_verified", side_effect=lambda u, s, p: p), \
                 mock.patch.object(baseline, "fetch_verified", side_effect=lambda u, s, p: p):
                old = baseline.build_all(manifest, pool_dir=root / "old", cache_dir=cache)
                new = build.build_all(manifest, pool_dir=root / "new", cache_dir=cache)
            self.assertEqual(len(old), 20)
            self.assertEqual({p.name: p.read_bytes() for p in old},
                             {p.name: p.read_bytes() for p in new})

    def test_consumer_rejects_self_asserted_notice_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory, identity = artifact_directory(Path(tmp))
            with self.assertRaisesRegex(ValueError, "reviewed licence text"):
                load_source_artifacts(directory, expected=pinned(identity),
                                      reviewed_notices=production_reviewed_notices())

    def test_readme_tracks_future_version_and_source_identity(self):
        _, identity = artifact_directory_for_document()
        data = source_manifest_document(identity.document())
        pkg = parse_manifest(data).packages[0]
        rendered = cargo_tauri_source_document(pkg)
        self.assertIn(pkg.version, rendered)
        self.assertIn(identity.commit, rendered)
        self.assertIn(pkg.targets["amd64"].sha256, rendered)
        self.assertNotIn("{version}", rendered)


class CanonicalCliDiscoveryReviewTest(unittest.TestCase):
    def test_updater_selects_cli_release_when_repository_latest_is_another_product(self):
        """A multiproduct repository's latest release is not its latest CLI tag."""
        import base64
        from scripts.tauri_source import git_blob_id, latest_stable_version
        from tests.test_source_artifacts import LOCK, COMMIT

        class OfficialMultiProductClient:
            def get_json(self, url):
                if url.endswith("/releases/latest"):
                    return {"tag_name": "tauri-v2.10.0", "draft": False,
                            "prerelease": False, "assets": []}
                if "/tags?" in url:
                    return [{"name": "tauri-v2.10.0"}, {"name": "tauri-cli-v2.12.2"}]
                if "/releases/tags/tauri-cli-v2.12.2" in url:
                    name = "cargo-tauri-x86_64-unknown-linux-gnu.tgz"
                    return {"tag_name": "tauri-cli-v2.12.2", "draft": False,
                            "prerelease": False, "assets": [{"name": name,
                            "browser_download_url": "https://github.com/tauri-apps/tauri/releases/download/tauri-cli-v2.12.2/" + name,
                            "digest": "sha256:" + "a" * 64}]}
                if "/git/ref/tags/tauri-cli-v2.12.2" in url:
                    return {"object": {"type": "commit", "sha": COMMIT}}
                if "/contents/Cargo.lock" in url:
                    return {"type": "file", "sha": git_blob_id(LOCK), "size": len(LOCK),
                            "encoding": "base64", "content": base64.b64encode(LOCK).decode()}
                raise AssertionError(url)

            def get_bytes(self, url, *, max_bytes):
                vendor = Path("packaging/licenses/cargo-tauri")
                if url.endswith("/LICENSE-MIT"):
                    return (vendor / "LICENSE.txt").read_bytes()
                if url.endswith("/LICENSE-APACHE-2.0"):
                    return (vendor / "LICENSE-APACHE-2.0").read_bytes()
                raise AssertionError(url)

        client = OfficialMultiProductClient()
        self.assertEqual(latest_stable_version(client), "2.12.2")
        with mock.patch.object(update, "RECIPES", {"cargo-tauri": object()}):
            result = update._discover(client)
        self.assertEqual(result["cargo-tauri"].version, "2.12.2")
        self.assertEqual(result["cargo-tauri"].sources["arm64"].commit, COMMIT)


class SourceVersionBindingReviewTest(unittest.TestCase):
    def test_consumer_rejects_version_that_is_only_a_tag_suffix(self):
        import json
        from scripts.repo import _source_artifacts
        from scripts.source_artifacts import production_reviewed_notices
        from scripts.source_build import SOURCE_PROVENANCE_NAME

        with tempfile.TemporaryDirectory() as tmp:
            directory, identity = artifact_directory(Path(tmp))
            for name, body in production_reviewed_notices().items():
                (directory / name).write_bytes(body)
            record_path = directory / SOURCE_PROVENANCE_NAME
            record = json.loads(record_path.read_text())
            record["version"] = "12.2"  # suffix of tauri-cli-v2.12.2, not its version
            record_path.write_text(json.dumps(record))
            expected_document = source_manifest_document(identity.document())
            self.assertEqual(expected_document["packages"][0]["version"], "2.12.2")
            with self.assertRaises(ValueError):
                _source_artifacts([str(directory)], expected_document)


def artifact_directory_for_document():
    from tests.test_source_artifacts import source_identity
    from dataclasses import replace
    identity = source_identity()
    return None, replace(identity, tag="tauri-cli-v2.13.0", commit="a" * 40)


class SuiteGitTrustReviewTest(unittest.TestCase):
    def test_workflow_trust_reaches_git_only_for_the_suite_workspace(self):
        from tests.test_workflows import FULL_SUITE_JOBS, job_block, read_workflow, run_bodies

        workspace = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir, home = root / "bin", root / "home"
            bin_dir.mkdir()
            home.mkdir()
            uv = bin_dir / "uv"
            uv.write_text(
                '#!/bin/sh\n[ "$*" = "run python -m unittest discover -s tests" ] || exit 91\n'
                '[ "$GIT_CONFIG_COUNT" = 1 ] || exit 92\n'
                '[ "$GIT_CONFIG_KEY_0" = safe.directory ] || exit 93\n'
                '[ "$GIT_CONFIG_VALUE_0" = "$PWD" ] || exit 94\n'
                'git config --get-all safe.directory || exit 95\n'
                # Simulate mismatched ownership without chown or config/ref writes.
                'GIT_TEST_ASSUME_DIFFERENT_OWNER=1 git rev-parse --is-inside-work-tree\n'
            )
            uv.chmod(0o755)
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith("GIT_CONFIG") and key != "GIT_TEST_ASSUME_DIFFERENT_OWNER"}
            env.update(PATH=f"{bin_dir}:{os.environ['PATH']}", HOME=str(home),
                       GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null")
            for workflow, job_name in FULL_SUITE_JOBS:
                with self.subTest(workflow=workflow):
                    bodies = run_bodies(job_block(read_workflow(workflow), job_name))
                    body = next(body for body in bodies if "unittest discover" in body)
                    proc = subprocess.run(
                        ["bash", "-c", body + '\nprintf "after-count=%s\\n" "${GIT_CONFIG_COUNT-unset}"'],
                        cwd=workspace, env=env, text=True, capture_output=True,
                    )
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertEqual(proc.stdout.splitlines(),
                                     [str(workspace), "true", "after-count=unset"])
            # Trusting a different path must not exempt this repository's ownership.
            other = dict(env, GIT_TEST_ASSUME_DIFFERENT_OWNER="1", GIT_CONFIG_COUNT="1",
                         GIT_CONFIG_KEY_0="safe.directory", GIT_CONFIG_VALUE_0=str(root))
            denied = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                                    cwd=workspace, env=other, text=True, capture_output=True)
            self.assertNotEqual(denied.returncode, 0)
            self.assertIn("dubious ownership", denied.stderr)


class NativeCargoInvocationReviewTest(unittest.TestCase):
    def test_real_proxy_and_toolchain_probes_work_without_global_state(self):
        """Execute only small shell fixtures: no Rust/source compiler or ARM payload."""
        from scripts.tauri_candidate_smoke import native_checks
        from scripts.tauri_source import toolchain_cargo

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli_dir, proxy_dir = root / "cli", root / "proxy"
            pinned_dir, home = root / "toolchain", root / "private-home"
            for directory in (cli_dir, proxy_dir, pinned_dir, home):
                directory.mkdir()
            cli = cli_dir / "cargo-tauri"
            cli.write_text(
                f'#!/bin/sh\n[ "$HOME" = "{home}" ] || exit 91\n'
                "printf 'tauri-cli 2.12.2\\n'\n"
            )
            cli.chmod(0o755)
            dispatch = (
                '#!/bin/sh\ncase "${0##*/}" in cargo) ;; *) exit 92 ;; esac\n'
                '[ "$1" = tauri ] && [ "$2" = --version ] || exit 93\n'
                '[ -z "${RUSTUP_HOME+x}" ] && [ -z "${CARGO_HOME+x}" ] || exit 94\n'
                'exec cargo-tauri --version\n'
            )
            rustup = proxy_dir / "rustup"
            rustup.write_text(dispatch)
            rustup.chmod(0o755)
            proxy = proxy_dir / "cargo"
            proxy.symlink_to(rustup)
            # Real subprocess execution distinguishes cargo argv0 from rustup argv0.
            self.assertEqual(len(native_checks(cli, "2.12.2", home, str(proxy),
                                               require_cargo=True)), 2)
            pinned = pinned_dir / "cargo"
            pinned.write_text(dispatch)
            pinned.chmod(0o755)

            def trusted_lookup(argv, cwd):
                self.assertEqual(argv, [str(rustup.absolute()), "which", "--toolchain",
                                        "1.99.0", "cargo"])
                return subprocess.CompletedProcess(argv, 0, f"{pinned}\n", "")

            with mock.patch("scripts.tauri_source.shutil.which", return_value=str(rustup)), \
                 mock.patch("scripts.tauri_source._run", side_effect=trusted_lookup):
                selected = toolchain_cargo("1.99.0")
            self.assertEqual(selected, str(pinned.absolute()))
            self.assertEqual(len(native_checks(cli, "2.12.2", home, selected,
                                               require_cargo=True)), 2)

    def test_producer_probe_keeps_the_cargo_proxy_name_and_pinned_toolchain(self):
        """A rustup cargo shim must be invoked by name, with the pinned toolchain.

        CI failed because the probe dereferenced `/…/cargo-home/bin/cargo` to the
        real toolchain executable, so `cargo tauri` ran as a differently named
        command. The probe must keep the proxy path (only made absolute) and must
        receive the pinned channel's own cargo, resolved before the private HOME
        exists. No compiler runs here.
        """
        from scripts.tauri_candidate_smoke import native_checks, version_output
        from scripts.tauri_source import toolchain_cargo

        seen: list[tuple] = []

        def run_probe(command, home, *, path=None):
            seen.append((command, path))
            return version_output("2.12.2")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "dist" / "usr" / "bin"
            cargo_home = root / "cargo-home" / "bin"
            toolchain_bin = cargo_home / "toolchains" / "1.99.0" / "bin"
            for directory in (bin_dir, cargo_home, toolchain_bin):
                directory.mkdir(parents=True)
            cli = bin_dir / "cargo-tauri"
            cli.write_text("#!/bin/sh\nprintf 'tauri-cli 2.12.2\\n'\n", encoding="utf-8")
            cli.chmod(0o755)
            # rustup installs cargo as a proxy with a differently named target.
            real = toolchain_bin / "cargo-1.99.0"
            real.write_text("#!/bin/sh\n", encoding="utf-8")
            real.chmod(0o755)
            proxy = cargo_home / "cargo"
            proxy.symlink_to(real)
            home = root / "isolated-home"
            home.mkdir()

            with mock.patch(
                "scripts.tauri_candidate_smoke.run_probe", side_effect=run_probe
            ):
                lines = native_checks(cli, "2.12.2", home, str(proxy), require_cargo=True)

            self.assertEqual(len(lines), 2)
            self.assertTrue(any("external subcommand" in line for line in lines))
            # The direct proof still ran against the candidate binary.
            self.assertEqual(seen[0][0], [str(cli.absolute()), "--version"])
            # The Cargo proof ran the proxy itself, not its dereferenced target.
            invoked = Path(seen[1][0][0])
            self.assertEqual(invoked, proxy.absolute())
            self.assertEqual(invoked.name, "cargo")
            self.assertNotEqual(invoked, real.absolute())
            self.assertEqual(seen[1][0][1:], ["tauri", "--version"])
            # A private HOME carried no rustup state, so the pinned toolchain
            # cargo must come from the trusted lookup, not from that HOME.
            rustup = root / "rustup"
            rustup.write_text("#!/bin/sh\n", encoding="utf-8")
            rustup.chmod(0o755)
            calls: list[list[str]] = []

            def fake_run(argv, cwd):
                calls.append(argv)
                return subprocess.CompletedProcess(argv, 0, f"{real}\n", "")

            with mock.patch("scripts.tauri_source.shutil.which", return_value=str(rustup)), \
                 mock.patch("scripts.tauri_source._run", side_effect=fake_run):
                self.assertEqual(toolchain_cargo("1.99.0"), str(real.absolute()))
            self.assertEqual(calls[0][1:], ["which", "--toolchain", "1.99.0", "cargo"])
