"""Regressions for the fail-closed CI tier selector."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts import ci_scope

ROOT = Path(__file__).resolve().parent.parent

LIGHT_PATHS = (
    "web/app.ts",
    "worker/index.ts",
    "docs/publishing.md",
    "client/main.rs",
    "README.md",
    "README.zh-CN.md",
    "package.json",
    "package-lock.json",
    "bun.lock",
    "bun.lockb",
    "tsconfig.json",
    "vitest.config.ts",
    "components.json",
    ".gitignore",
)

HEAVY_PATHS = (
    "packaging/manifest.json",
    "scripts/ci_scope.py",
    "tests/test_ci_scope.py",
    ".github/workflows/ci.yml",
    "pyproject.toml",
    "uv.lock",
)


def git_env() -> dict[str, str]:
    return {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }


class TempRepo:
    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name)
        self.env = git_env()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "CI Test")
        self.git("config", "user.email", "ci@example.invalid")

    def close(self) -> None:
        self._tmp.cleanup()

    def git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.path), *args],
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
        return result.stdout.strip()

    def write(self, name: str, text: str = "content\n") -> None:
        target = self.path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-m", message)
        return self.git("rev-parse", "HEAD")


class PathClassificationTest(unittest.TestCase):
    def test_documented_light_paths_are_light(self) -> None:
        for path in LIGHT_PATHS:
            with self.subTest(path=path):
                self.assertFalse(ci_scope.path_is_heavy(path))

    def test_package_relevant_paths_are_heavy(self) -> None:
        for path in HEAVY_PATHS:
            with self.subTest(path=path):
                self.assertTrue(ci_scope.path_is_heavy(path))

    def test_unclassified_paths_are_heavy(self) -> None:
        for path in ("CHANGELOG.md", "webx/app.ts", "src/main.rs", "packaging"):
            with self.subTest(path=path):
                self.assertTrue(ci_scope.path_is_heavy(path))

    def test_markdown_has_no_broad_wildcard(self) -> None:
        self.assertFalse(ci_scope.path_is_heavy("README.md"))
        self.assertTrue(ci_scope.path_is_heavy("CHANGELOG.md"))

    def test_one_heavy_path_makes_the_change_heavy(self) -> None:
        self.assertTrue(ci_scope.classify_paths(["web/app.ts", "packaging/manifest.json"]))
        self.assertFalse(ci_scope.classify_paths(["web/app.ts", "docs/publishing.md"]))


class AcquisitionTest(unittest.TestCase):
    BASE = "a" * 40
    HEAD = "b" * 40

    def run_with(self, *, stdout: bytes = b"", returncode: int = 0, error: BaseException | None = None):
        environ = {"CI_EVENT_NAME": "push", "CI_BASE_SHA": self.BASE, "CI_HEAD_SHA": self.HEAD}
        if error is not None:
            patcher = mock.patch.object(ci_scope.subprocess, "run", side_effect=error)
        else:
            patcher = mock.patch.object(
                ci_scope.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=returncode, stdout=stdout),
            )
        with patcher as runner:
            heavy = ci_scope.is_heavy(environ)
        return heavy, runner

    def test_exact_git_argv_is_used(self) -> None:
        _, runner = self.run_with(stdout=b"web/app.ts\0")
        runner.assert_called_once_with(
            ["git", "diff", "--no-renames", "--name-only", "-z", self.BASE, self.HEAD, "--"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )

    def test_light_diff_stays_light(self) -> None:
        heavy, _ = self.run_with(stdout=b"web/app.ts\0docs/publishing.md\0")
        self.assertFalse(heavy)

    def test_heavy_diff_is_heavy(self) -> None:
        heavy, _ = self.run_with(stdout=b"web/app.ts\0packaging/manifest.json\0")
        self.assertTrue(heavy)

    def test_empty_diff_is_heavy(self) -> None:
        heavy, _ = self.run_with(stdout=b"")
        self.assertTrue(heavy)

    def test_diff_failure_is_heavy(self) -> None:
        heavy, _ = self.run_with(returncode=128)
        self.assertTrue(heavy)

    def test_missing_repository_is_heavy(self) -> None:
        heavy, _ = self.run_with(error=OSError("git missing"))
        self.assertTrue(heavy)

    def test_malformed_bytes_are_heavy(self) -> None:
        heavy, _ = self.run_with(stdout=b"web/\xff.ts\0")
        self.assertTrue(heavy)

    def test_unsafe_paths_are_heavy(self) -> None:
        for raw in (b"/etc/passwd\0", b"web/../packaging/x\0", b"web//x\0", b"web\\x\0"):
            with self.subTest(raw=raw):
                heavy, _ = self.run_with(stdout=raw)
                self.assertTrue(heavy)


class EventDecisionTest(unittest.TestCase):
    SHA = "c" * 40

    def env(self, **overrides: str) -> dict[str, str]:
        environ = {"CI_EVENT_NAME": "push", "CI_BASE_SHA": self.SHA, "CI_HEAD_SHA": self.SHA}
        environ.update(overrides)
        return environ

    def test_manual_dispatch_never_needs_git(self) -> None:
        with mock.patch.object(ci_scope.subprocess, "run", side_effect=AssertionError("git used")):
            self.assertTrue(ci_scope.is_heavy(self.env(CI_EVENT_NAME="workflow_dispatch")))

    def test_unrecognized_or_missing_event_is_heavy(self) -> None:
        for event in ("", "schedule", "pull_request_target"):
            with self.subTest(event=event):
                self.assertTrue(ci_scope.is_heavy(self.env(CI_EVENT_NAME=event)))

    def test_invalid_shas_are_heavy(self) -> None:
        for base in ("", "c" * 39, "C" * 40, "0" * 40, "not-a-sha"):
            with self.subTest(base=base):
                self.assertTrue(ci_scope.is_heavy(self.env(CI_BASE_SHA=base)))
        for head in ("", "c" * 39, "C" * 40, "0" * 40):
            with self.subTest(head=head):
                self.assertTrue(ci_scope.is_heavy(self.env(CI_HEAD_SHA=head)))

    def test_pull_request_uses_base_and_head_env(self) -> None:
        base, head = "d" * 40, "e" * 40
        with mock.patch.object(
            ci_scope.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout=b"worker/index.ts\0"),
        ) as runner:
            self.assertFalse(
                ci_scope.is_heavy(
                    self.env(CI_EVENT_NAME="pull_request", CI_BASE_SHA=base, CI_HEAD_SHA=head)
                )
            )
        runner.assert_called_once_with(
            ["git", "diff", "--no-renames", "--name-only", "-z", base, head, "--"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )


class CliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo()
        self.addCleanup(self.repo.close)

    def cli(self, event: str, base: str = "", head: str = "") -> subprocess.CompletedProcess[str]:
        environ = dict(self.repo.env)
        for key in ("CI_EVENT_NAME", "CI_BASE_SHA", "CI_HEAD_SHA"):
            environ.pop(key, None)
        environ.update(
            {
                "CI_EVENT_NAME": event,
                "CI_BASE_SHA": base,
                "CI_HEAD_SHA": head,
                "PYTHONPATH": str(ROOT),
            }
        )
        return subprocess.run(
            [sys.executable, "-m", "scripts.ci_scope"],
            cwd=self.repo.path,
            env=environ,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def assert_decision(self, event: str, base: str, head: str, expected: str) -> None:
        result = self.cli(event, base, head)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stdout, f"heavy={expected}\n", msg=result.stderr)

    def test_manual_dispatch_prints_heavy(self) -> None:
        self.assert_decision("workflow_dispatch", "", "", "true")

    def test_invalid_input_prints_heavy(self) -> None:
        self.assert_decision("push", "not-a-sha", "e" * 40, "true")

    def test_real_light_and_heavy_commits(self) -> None:
        self.repo.write("web/app.ts")
        self.repo.write("packaging/manifest.json", "{}\n")
        base = self.repo.commit("base")
        self.repo.write("web/feature.ts")
        web = self.repo.commit("web only")
        self.assert_decision("push", base, web, "false")
        self.repo.write("packaging/manifest.json", '{"changed": true}\n')
        package = self.repo.commit("packaging")
        self.assert_decision("push", web, package, "true")

    def test_deleting_a_package_path_stays_heavy(self) -> None:
        self.repo.write("packaging/x.txt")
        base = self.repo.commit("base")
        (self.repo.path / "packaging" / "x.txt").unlink()
        head = self.repo.commit("delete")
        self.assert_decision("push", base, head, "true")

    def test_rename_out_of_and_into_light_paths(self) -> None:
        self.repo.write("packaging/x.txt")
        self.repo.write("web/a.ts")
        base = self.repo.commit("base")
        (self.repo.path / "web").mkdir(exist_ok=True)
        self.repo.git("mv", "packaging/x.txt", "web/x.txt")
        heavy = self.repo.commit("package to web")
        self.assert_decision("push", base, heavy, "true")
        self.repo.git("mv", "web/a.ts", "web/b.ts")
        light = self.repo.commit("web to web")
        self.assert_decision("push", heavy, light, "false")

    def test_pull_request_scopes_base_to_merge_commit(self) -> None:
        self.repo.write("README.md", "base\n")
        base = self.repo.commit("base")

        self.repo.write("packaging/manifest.json", "{}\n")
        main = self.repo.commit("main package change")

        self.repo.git("checkout", "-b", "feature", base)
        self.repo.write("web/feature.ts")
        self.repo.commit("feature web change")
        self.repo.git("checkout", "main")
        self.repo.git("merge", "--no-ff", "feature", "-m", "merge feature")
        merge = self.repo.git("rev-parse", "HEAD")

        self.assert_decision("pull_request", main, merge, "false")


if __name__ == "__main__":
    unittest.main()
