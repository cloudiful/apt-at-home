from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.manifest import load_manifest
from scripts.pi_bundle import launcher_bytes, update_helper_bytes
from scripts.smoke_package import SmokeError
from scripts.smoke_runtime import run_package
from tests.test_pi_smoke import installed_bundle


class PiLauncherTest(unittest.TestCase):
    def test_launcher_overrides_the_caller_command_and_passes_arguments_through(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            libdir = root / "usr/lib/pi"
            libdir.mkdir(parents=True)
            fake_pi = libdir / "pi"
            fake_pi.write_text(
                '#!/bin/sh\nprintf \'self=%s\\n\' "$PI_SELF_UPDATE_COMMAND"\n'
                'for arg in "$@"; do printf \'arg=%s\\n\' "$arg"; done\n',
                encoding="utf-8",
            )
            fake_pi.chmod(0o755)
            launcher = root / "pi"
            launcher.write_bytes(launcher_bytes().replace(b"/usr/lib/pi", str(libdir).encode()))
            launcher.chmod(0o755)
            env = dict(os.environ)
            env["PI_SELF_UPDATE_COMMAND"] = "/tmp/attacker-updater"
            completed = subprocess.run(
                [str(launcher), "update", "--extensions", "owner/pkg", "--models"],
                env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.splitlines(), [
            f"self={libdir}/apt-update",
            "arg=update",
            "arg=--extensions",
            "arg=owner/pkg",
            "arg=--models",
        ])

    def test_update_helper_is_actionable_nonzero_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            helper = root / "apt-update"
            helper.write_bytes(update_helper_bytes())
            helper.chmod(0o755)
            before = sorted(path.name for path in root.iterdir())
            completed = subprocess.run(
                [str(helper)], text=True, cwd=root,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            after = sorted(path.name for path in root.iterdir())
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("apt upgrade pi", completed.stderr)
        self.assertIn("apt install --only-upgrade pi", completed.stderr)
        self.assertEqual(before, after)
        self.assertEqual(completed.stdout, "")
        for forbidden in (b"apt-get", b"curl", b"wget", b"dpkg -i"):
            self.assertNotIn(forbidden, update_helper_bytes())


class PiRuntimeDispatchTest(unittest.TestCase):
    def test_run_package_dispatches_pi_bundle_and_version_contract(self) -> None:
        package = next(pkg for pkg in load_manifest().packages if pkg.name == "pi")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            installed_bundle(root, "amd64")
            completed = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="1.5.0\n", stderr=""
            )
            with mock.patch("scripts.smoke_runtime._run", return_value=completed):
                findings = run_package(
                    package, "amd64", prefix=root, scratch=root / "scratch"
                )
        self.assertTrue(any("1.5.0" in line for line in findings))

    def test_run_package_rejects_a_different_pi_version(self) -> None:
        package = next(pkg for pkg in load_manifest().packages if pkg.name == "pi")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            installed_bundle(root, "amd64")
            completed = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="1.5.1\n", stderr=""
            )
            with mock.patch("scripts.smoke_runtime._run", return_value=completed):
                with self.assertRaisesRegex(SmokeError, "expected '1.5.0'"):
                    run_package(package, "amd64", prefix=root, scratch=root / "scratch")


if __name__ == "__main__":
    unittest.main()
