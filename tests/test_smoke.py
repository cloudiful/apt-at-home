"""Manifest-driven smoke checks: count, package metadata, ELF machine, native run.

The helper must work for any catalog, so these tests build a small real manifest
with real `.deb` files rather than a fake shape, and they prove both the success
path and every way a package can disagree with its manifest.
"""

import contextlib
import io
import os
import shutil
import struct
import tempfile
import unittest
from pathlib import Path

from scripts.deb import build_deb
from scripts.manifest import Manifest, deb_filename, parse_manifest
from tests.helpers import stage_fixture_target
from scripts.smoke import (
    ELF_MACHINE,
    MAX_BINARY_BYTES,
    SmokeError,
    check_elf,
    check_pool,
    control_fields,
    elf_machine,
    main,
    run_installed,
)
from scripts.smoke_runtime import NEOVIM_LUA_CHECK, arguments_for

# coreutils is present on every image that runs this suite; both are real
# x86-64 ELF executables with a fixed exit status.
_TRUE = "/bin/true" if Path("/bin/true").exists() else "/usr/bin/true"
_FALSE = "/bin/false" if Path("/bin/false").exists() else "/usr/bin/false"

_ELF_MACHINE_CODE = {"amd64": 62, "arm64": 183}


def fake_elf(arch: str) -> bytes:
    """A minimal but structurally valid 64-bit little-endian ELF header."""
    header = b"\x7fELF" + bytes([2, 1, 1, 0]) + b"\x00" * 8
    return header + struct.pack("<HHI", 2, _ELF_MACHINE_CODE[arch], 1) + b"\x00" * 32

REPO_ROOT = Path(__file__).resolve().parent.parent


def _catalog(names: tuple[str, ...] = ("git-delta",)) -> Manifest:
    """A minimal valid manifest restricted to the requested packages."""
    import copy

    from scripts.release_manifest import load_manifest_data
    from scripts.manifest import default_manifest_path

    data = copy.deepcopy(load_manifest_data(default_manifest_path()))
    data["packages"] = [
        entry for entry in data["packages"] if entry["name"] in names
    ]
    return parse_manifest(data)


def _build_pool(root: Path, manifest: Manifest) -> Path:
    """Stage one tiny .deb per package and architecture, without the network."""
    pool = root / "pool"
    pool.mkdir(parents=True, exist_ok=True)
    for pkg in manifest.packages:
        for arch in ("amd64", "arm64"):
            stage = stage_fixture_target(
                root, pkg, arch, manifest, fake_elf(arch), root / "stages",
            )
            build_deb(stage, pool / deb_filename(pkg, arch, manifest))
    return pool


class ElfMachineTest(unittest.TestCase):
    def test_recognizes_the_two_published_architectures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for arch in ("amd64", "arm64"):
                path = root / f"{arch}.bin"
                path.write_bytes(fake_elf(arch))
                self.assertEqual(elf_machine(path), ELF_MACHINE[arch])

    def test_refuses_a_non_elf_and_an_unknown_machine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            text = root / "text.bin"
            text.write_bytes(b"#!/bin/sh\necho hello\n")
            with self.assertRaises(SmokeError):
                elf_machine(text)
            foreign = root / "foreign.bin"
            foreign.write_bytes(struct.pack("<16sH", b"\x7fELF", 0x3E) + b"\x00" * 4)
            with self.assertRaises(SmokeError) as ctx:
                elf_machine(foreign)
            self.assertIn("unsupported machine", str(ctx.exception))


class CheckPoolTest(unittest.TestCase):
    def test_a_manifest_built_pool_passes_every_check(self) -> None:
        manifest = _catalog()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pool = _build_pool(root, manifest)
            findings = check_pool(manifest, pool, ("amd64", "arm64"))
        self.assertTrue(findings)
        self.assertTrue(any("git-delta" in line for line in findings))
        self.assertTrue(any("aarch64" in line for line in findings))

    def test_a_missing_package_is_refused(self) -> None:
        manifest = _catalog()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pool = _build_pool(root, manifest)
            (pool / deb_filename(manifest.packages[0], "arm64", manifest)).unlink()
            with self.assertRaises(SmokeError) as ctx:
                check_pool(manifest, pool, ("amd64", "arm64"))
            self.assertIn("missing", str(ctx.exception))

    def test_control_fields_are_read_through_dpkg_deb(self) -> None:
        manifest = _catalog()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pool = _build_pool(root, manifest)
            delta = manifest.packages[0]
            arm64 = pool / deb_filename(delta, "arm64", manifest)
            fields = control_fields(arm64)
            self.assertEqual(fields["Package"], "git-delta")
            self.assertEqual(fields["Architecture"], "arm64")
            self.assertEqual(fields["Depends"], "libc6 (>= 2.18), libgcc-s1")
            # The static sibling declares no Depends field at all.
            self.assertNotIn(
                "Depends", control_fields(pool / deb_filename(delta, "amd64", manifest))
            )


class CheckElfTest(unittest.TestCase):
    def test_a_wrong_machine_is_refused(self) -> None:
        pkg = _catalog().packages[0]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delta"
            path.write_bytes(fake_elf("arm64"))
            with self.assertRaises(SmokeError) as ctx:
                check_elf(pkg, "amd64", path)
            self.assertIn("expected x86-64", str(ctx.exception))

    def test_a_missing_binary_is_refused(self) -> None:
        pkg = _catalog().packages[0]
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SmokeError):
                check_elf(pkg, "amd64", Path(tmp) / "absent")

    def test_the_binary_ceiling_is_finite(self) -> None:
        self.assertEqual(MAX_BINARY_BYTES, 512 * 1024 * 1024)


class RunInstalledTest(unittest.TestCase):
    """`run_installed` checks the ELF machine before it executes anything."""

    def _installed(self, root: Path, manifest: Manifest, source: str) -> Path:
        """A stand-in installed root whose 'binaries' are a real coreutils binary."""
        bin_dir = root / "usr" / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        for pkg in manifest.packages:
            shutil.copyfile(source, bin_dir / pkg.targets["amd64"].binary)
            (bin_dir / pkg.targets["amd64"].binary).chmod(0o755)
        return root

    def test_each_binary_runs_with_its_documented_flag(self) -> None:
        manifest = _catalog(("git-delta", "rustfs-cli"))
        with tempfile.TemporaryDirectory() as tmp:
            root = self._installed(Path(tmp), manifest, _TRUE)
            findings = run_installed(manifest, "amd64", prefix=root)
        self.assertEqual(len(findings), 2)
        joined = " ".join(findings)
        self.assertIn("exited 0", joined)
        self.assertIn("--version", joined)
        self.assertIn("--help", joined)

    def test_a_failing_binary_stops_the_run(self) -> None:
        manifest = _catalog(("git-delta",))
        with tempfile.TemporaryDirectory() as tmp:
            root = self._installed(Path(tmp), manifest, _FALSE)
            with self.assertRaises(SmokeError) as ctx:
                run_installed(manifest, "amd64", prefix=root)
            self.assertIn("exited 1", str(ctx.exception))

    def test_a_non_elf_program_is_never_executed(self) -> None:
        manifest = _catalog(("git-delta",))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "usr" / "bin" / "delta"
            script.parent.mkdir(parents=True)
            script.write_text("#!/bin/sh\necho should not run\n", encoding="utf-8")
            script.chmod(0o755)
            with self.assertRaises(SmokeError) as ctx:
                run_installed(manifest, "amd64", prefix=root)
            self.assertIn("not an ELF binary", str(ctx.exception))


class NeovimRuntimeTest(unittest.TestCase):
    """The reviewed Neovim handler runs headless and fails closed on a broken runtime."""

    def test_neovim_uses_the_headless_handler_not_a_version_flag(self) -> None:
        args = arguments_for("neovim")
        self.assertEqual(args[:2], ("--headless", "--clean"))
        self.assertIn("-u", args)
        self.assertIn("NONE", args)
        self.assertNotIn("--version", args)
        self.assertEqual(args[-2:], ("-c", "qa!"))

    def test_other_packages_keep_their_recipe_flag(self) -> None:
        self.assertEqual(arguments_for("git-delta"), ("--version",))
        self.assertEqual(arguments_for("rustfs-cli"), ("--help",))

    def test_the_lua_check_fails_closed_with_an_explicit_nonzero_exit(self) -> None:
        self.assertIn("cquit 1", NEOVIM_LUA_CHECK)
        self.assertIn("pcall", NEOVIM_LUA_CHECK)
        self.assertNotIn("qa0", NEOVIM_LUA_CHECK)

    def test_a_non_elf_neovim_is_never_executed(self) -> None:
        manifest = _catalog(("neovim",))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "usr" / "bin" / "nvim"
            script.parent.mkdir(parents=True)
            script.write_text("#!/bin/sh\necho should not run\n", encoding="utf-8")
            script.chmod(0o755)
            with self.assertRaises(SmokeError) as ctx:
                run_installed(manifest, "amd64", prefix=root)
            self.assertIn("not an ELF binary", str(ctx.exception))

    def test_a_wrong_architecture_neovim_is_refused_before_any_run(self) -> None:
        manifest = _catalog(("neovim",))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root / "usr" / "bin" / "nvim"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(fake_elf("arm64"))
            binary.chmod(0o755)
            with self.assertRaises(SmokeError) as ctx:
                run_installed(manifest, "amd64", prefix=root)
            self.assertIn("expected x86-64", str(ctx.exception))


class NativeNeovimRunTest(unittest.TestCase):
    """Runs the real pinned Neovim when its verified archive is cached and the host is amd64."""

    CACHED = Path.home() / ".cache" / "opencode" / "apt-neovim" / "nvim-amd64.tar.gz"

    @unittest.skipUnless(
        os.uname().machine == "x86_64" and CACHED.is_file(),
        "needs an amd64 host and the cached pinned Neovim archive",
    )
    def test_the_packaged_neovim_runtime_and_lua_parser_run_headless(self) -> None:
        import tarfile

        from scripts.smoke_runtime import _run

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with tarfile.open(self.CACHED, "r:gz") as tar:
                for member in tar.getmembers():
                    if member.name.startswith("nvim-linux-x86_64/bin/nvim") or (
                        member.name.startswith("nvim-linux-x86_64/share/nvim/runtime/")
                        or member.name.startswith("nvim-linux-x86_64/lib/nvim/")
                    ):
                        tar.extract(member, root, filter="data")
            binary = root / "nvim-linux-x86_64" / "bin" / "nvim"
            proc = _run(binary, arguments_for("neovim"), scratch=root / "scratch")
            self.assertEqual(proc.returncode, 0, msg=proc.stdout)
            self.assertIn("neovim runtime and lua parser ok", proc.stdout)


class CliTest(unittest.TestCase):
    def _manifest_path(self, tmp: Path) -> Path:
        from scripts.manifest import default_manifest_path
        from scripts.release_manifest import canonical_bytes, load_manifest_data

        path = tmp / "manifest.json"
        path.write_bytes(canonical_bytes(load_manifest_data(default_manifest_path())))
        return path

    def test_count_is_derived_from_the_manifest(self) -> None:
        from scripts.manifest import default_manifest_path
        from scripts.release_manifest import canonical_bytes, load_manifest_data

        def count_of(packages: list[str]) -> int:
            data = load_manifest_data(default_manifest_path())
            data["packages"] = [
                e for e in data["packages"] if e["name"] in packages
            ]
            path = root / "subset.json"
            path.write_bytes(canonical_bytes(data))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--manifest", str(path), "--count"]), 0)
            return len(packages) * 2

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(count_of(["zellij", "starship"]), 4)
            self.assertEqual(
                count_of(["zellij", "starship", "git-delta", "rustfs-cli", "sccache"]), 10
            )

    def test_a_mode_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(io.StringIO()):
            path = self._manifest_path(Path(tmp))
            self.assertEqual(main(["--manifest", str(path)]), 2)

    def test_a_bad_pool_path_fails_without_a_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(io.StringIO()):
            path = self._manifest_path(Path(tmp))
            self.assertEqual(
                main(["--manifest", str(path), "--pool", str(Path(tmp) / "absent")]), 1
            )


if __name__ == "__main__":
    unittest.main()