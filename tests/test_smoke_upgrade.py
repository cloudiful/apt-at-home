"""Post-upgrade dpkg state: the packaged neovim replaces the distro runtime and nothing else."""

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import smoke_upgrade
from scripts.manifest import load_manifest
from scripts.smoke_package import SmokeError
from scripts.smoke_upgrade import check_upgraded_state


class OwnsFilesTest(unittest.TestCase):
    def test_only_states_that_still_own_files_block_the_upgrade(self) -> None:
        self.assertTrue(smoke_upgrade._owns_files("install ok installed"))  # noqa: SLF001
        self.assertTrue(smoke_upgrade._owns_files("install ok half-configured"))  # noqa: SLF001
        self.assertFalse(smoke_upgrade._owns_files("deinstall ok config-files"))  # noqa: SLF001
        self.assertFalse(smoke_upgrade._owns_files("purge ok not-installed"))  # noqa: SLF001


class UpgradedStateTest(unittest.TestCase):
    """A fake dpkg-query answers from a table, so the decisions are exact."""

    def _run(self, table: dict[str, str]) -> list[str]:
        manifest = load_manifest()
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp)
            script = bin_dir / "dpkg-query"
            lines = ["#!/bin/sh", "for last in \"$@\"; do :; done"]
            for name, status in table.items():
                version = "0.12.6-1" if name == "neovim" else "0.10.4-8"
                lines.append(
                    f"if [ \"$last\" = \"{name}\" ]; then printf '{name} {version} {status}\\n'; exit 0; fi"
                )
            lines.append("exit 1")
            script.write_text("\n".join(lines) + "\n", encoding="utf-8")
            script.chmod(script.stat().st_mode | stat.S_IEXEC)
            with mock.patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}):
                return check_upgraded_state(manifest)

    def test_packaged_neovim_with_the_distro_runtime_removed_passes(self) -> None:
        findings = self._run({
            "neovim": "install ok installed",
            "neovim-runtime": "deinstall ok config-files",
        })
        self.assertIn("neovim 0.12.6-1 is installed", findings[0])

    def test_a_still_installed_distro_runtime_fails_the_upgrade(self) -> None:
        with self.assertRaises(SmokeError) as ctx:
            self._run({
                "neovim": "install ok installed",
                "neovim-runtime": "install ok installed",
            })
        self.assertIn("still owns files", str(ctx.exception))

    def test_a_distro_version_left_in_place_fails_the_version_check(self) -> None:
        manifest = load_manifest()
        with mock.patch.object(smoke_upgrade, "_query", return_value=("0.12.5-1~trixie", "install ok installed")):
            with self.assertRaises(SmokeError) as ctx:
                check_upgraded_state(manifest)
        self.assertIn("manifest expects 0.12.6-1", str(ctx.exception))

    def test_a_package_that_is_not_installed_fails(self) -> None:
        manifest = load_manifest()
        with mock.patch.object(smoke_upgrade, "_query", return_value=None):
            with self.assertRaises(SmokeError) as ctx:
                check_upgraded_state(manifest)
        self.assertIn("not installed", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
