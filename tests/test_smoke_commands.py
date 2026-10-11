"""Static package inventory and native command probe policy."""

import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from scripts.deb import build_deb
from scripts.manifest import Manifest, deb_filename, load_manifest
from scripts.package_licenses import copyright_text, read_vendor_licenses
from scripts.smoke import check_pool
from scripts.smoke_package import SmokeError
from scripts.smoke_commands import (
    binaries_for_target,
    check_probe_output,
    extra_command_probes,
)
from scripts.smoke_runtime import run_installed
from tests.helpers import stage_fixture_target
from tests.test_smoke import fake_elf


def _uv_pool(root: Path, *, damage: str = "") -> tuple[Manifest, Path]:
    catalog = load_manifest()
    pkg = next(package for package in catalog.packages if package.name == "uv")
    manifest = replace(catalog, packages=(pkg,))
    pool = root / "pool"
    pool.mkdir(parents=True)
    stage = stage_fixture_target(
        root, pkg, "amd64", manifest, fake_elf("amd64"), root / "stages"
    )
    if damage == "missing-uvx":
        (stage / "usr" / "bin" / "uvx").unlink()
    elif damage == "wrong-uvx-elf":
        uvx = stage / "usr" / "bin" / "uvx"
        uvx.write_bytes(fake_elf("arm64"))
        uvx.chmod(0o755)
    elif damage == "missing-apache":
        copyright = stage / "usr" / "share" / "doc" / "uv" / "copyright"
        copyright.write_text(
            copyright_text(pkg, read_vendor_licenses(pkg)[0]), encoding="utf-8"
        )
    build_deb(stage, pool / deb_filename(pkg, "amd64", manifest))
    return manifest, pool


class SmokeCommandsTest(unittest.TestCase):
    def test_uv_inventory_and_native_probe_include_uvx(self) -> None:
        uv = next(pkg for pkg in load_manifest().packages if pkg.name == "uv")
        for arch in ("amd64", "arm64"):
            self.assertEqual(binaries_for_target(uv.targets[arch]), ("uv", "uvx"))
        self.assertEqual(extra_command_probes("uv")[0].binary, "uvx")
        self.assertEqual(extra_command_probes("uv")[0].args, ("--version",))

    def test_pool_smoke_rejects_missing_uvx_wrong_elf_and_missing_license(self) -> None:
        cases = (
            ("missing-uvx", "expected"),
            ("wrong-uvx-elf", "expected x86-64"),
            ("missing-apache", "copyright does not match reviewed license texts"),
        )
        for damage, expected in cases:
            with self.subTest(damage=damage), tempfile.TemporaryDirectory() as tmp:
                manifest, pool = _uv_pool(Path(tmp), damage=damage)
                with self.assertRaisesRegex(SmokeError, expected):
                    check_pool(manifest, pool, ("amd64",))

    def test_zoxide_shell_integration_probe_is_captured_not_executed(self) -> None:
        probe = extra_command_probes("zoxide")[0]
        self.assertEqual((probe.binary, probe.args), ("zoxide", ("init", "bash")))
        check_probe_output(probe, "function __zoxide_z() { :; }\n")
        with self.assertRaisesRegex(ValueError, "lacks '__zoxide_z'"):
            check_probe_output(probe, "no integration output")

    def test_native_runner_executes_both_uv_commands_as_argument_vectors(self) -> None:
        uv = next(pkg for pkg in load_manifest().packages if pkg.name == "uv")
        calls: list[tuple[str, tuple[str, ...]]] = []

        def run(binary: Path, args: tuple[str, ...], *, scratch: Path):
            calls.append((binary.name, args))
            return subprocess.CompletedProcess(
                [str(binary), *args], 0, f"{binary.name} 0.13.0\n", ""
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "usr" / "bin"
            bin_dir.mkdir(parents=True)
            for name in ("uv", "uvx"):
                path = bin_dir / name
                path.write_bytes(fake_elf("amd64"))
                path.chmod(0o755)
            with mock.patch("scripts.smoke_runtime._run", side_effect=run):
                findings = run_installed(
                    Manifest(
                        maintainer="test", section="utils", priority="optional",
                        debian_revision="-1", packages=(uv,),
                    ),
                    "amd64",
                    prefix=root,
                )

        self.assertEqual(calls, [("uv", ("--version",)), ("uvx", ("--version",))])
        self.assertEqual(len(findings), 2)
        self.assertTrue(all("exited 0" in finding for finding in findings))


if __name__ == "__main__":
    unittest.main()
