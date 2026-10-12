"""The smoke checker is the only place the upstream binary is ever executed.

These tests cover the properties that make that defensible, without needing the
real 88 MB binary: the ELF architecture must be read and matched *before*
anything runs, `--static-only` must be an unconditional execution disable, and a
run must use a private temporary HOME rather than shared system storage.

The proxy variables are best-effort deterrence for proxy-aware requests. They are
deliberately *not* described here as a network sandbox: a client that ignores
them, or opens a raw socket, is not stopped by this module.
"""

import contextlib
import io
import os
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import bun_candidate_smoke as smoke


def write_elf(path: Path, machine: int, *, payload: bytes = b"") -> None:
    """Write a minimal 64-bit little-endian ELF header with the given machine."""
    header = bytearray(20)
    header[0:4] = b"\x7fELF"
    header[4] = 2  # ELFCLASS64
    header[5] = 1  # ELFDATA2LSB
    struct.pack_into("<H", header, 18, machine)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(header) + payload)
    path.chmod(0o755)


class ElfArchTest(unittest.TestCase):
    def test_reads_class_and_machine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bun"
            write_elf(path, smoke._MACHINE["amd64"])
            self.assertEqual(smoke.read_elf(path), (64, 62))

    def test_non_elf_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bun"
            path.write_bytes(b"not an elf file at all")
            with self.assertRaisesRegex(ValueError, "not an ELF file"):
                smoke.read_elf(path)

    def test_truncated_header_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bun"
            path.write_bytes(b"\x7fELF")
            with self.assertRaisesRegex(ValueError, "not an ELF file"):
                smoke.read_elf(path)

    def test_big_endian_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bun"
            header = bytearray(b"\x7fELF" + bytes(16))
            header[4], header[5] = 2, 2
            path.write_bytes(bytes(header))
            with self.assertRaisesRegex(ValueError, "little-endian"):
                smoke.read_elf(path)

    def test_arch_mismatch_refused_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bun"
            write_elf(path, smoke._MACHINE["arm64"])
            with self.assertRaisesRegex(ValueError, "requires 62"):
                smoke.check_elf_arch(path, "amd64")

    def test_32_bit_elf_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bun"
            header = bytearray(20)
            header[0:4] = b"\x7fELF"
            header[4] = 1  # ELFCLASS32
            header[5] = 1  # ELFDATA2LSB
            path.write_bytes(bytes(header))
            with self.assertRaisesRegex(ValueError, "32-bit"):
                smoke.check_elf_arch(path, "amd64")


class PayloadShapeTest(unittest.TestCase):
    """Static checks on a staged payload: shape, mode, and the owned alias."""

    def _stage(self, root: Path, *, machine: int, alias: str | None = "bun") -> Path:
        bin_dir = root / "usr" / "bin"
        bin_dir.mkdir(parents=True)
        write_elf(bin_dir / "bun", machine)
        if alias is not None:
            (bin_dir / "bunx").symlink_to(alias)
        return bin_dir

    def test_static_checks_pass_on_a_well_formed_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = self._stage(root, machine=smoke._MACHINE["amd64"])
            if smoke.native_arch() != "amd64":
                self.assertEqual(
                    smoke.smoke(bin_dir, "amd64", "1.4.3", require_native=False)[0],
                    "bun: ELF 64-bit amd64, mode 0755",
                )

    def test_wrong_mode_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = self._stage(root, machine=smoke._MACHINE["amd64"])
            (bin_dir / "bun").chmod(0o755)
            os.chmod(bin_dir / "bun", 0o700)
            with self.assertRaisesRegex(ValueError, "expected 755"):
                smoke.smoke(bin_dir, "amd64", "1.4.3", require_native=False)

    def test_missing_alias_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self._stage(Path(tmp), machine=smoke._MACHINE["amd64"], alias=None)
            with self.assertRaisesRegex(ValueError, "not a symlink"):
                smoke.native_checks(bin_dir / "bun", bin_dir / "bunx", "1.4.3", Path(tmp))

    def test_alias_pointing_elsewhere_refused(self) -> None:
        """Only the reviewed `bunx -> bun` link is accepted, never an upstream one."""
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self._stage(Path(tmp), machine=smoke._MACHINE["amd64"], alias="/bin/sh")
            with self.assertRaisesRegex(ValueError, "points at /bin/sh"):
                smoke.native_checks(bin_dir / "bun", bin_dir / "bunx", "1.4.3", Path(tmp))

    def test_foreign_arch_is_skipped_not_executed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            foreign = "arm64" if smoke.native_arch() != "arm64" else "amd64"
            bin_dir = self._stage(root, machine=smoke._MACHINE[foreign])
            results = smoke.smoke(bin_dir, foreign, "1.4.3", require_native=False)
            self.assertTrue(any("native run skipped" in line for line in results))

    def test_require_native_refuses_on_a_foreign_machine(self) -> None:
        foreign = "arm64" if smoke.native_arch() != "arm64" else "amd64"
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self._stage(Path(tmp), machine=smoke._MACHINE[foreign])
            with self.assertRaisesRegex(ValueError, "native smoke needs"):
                smoke.smoke(bin_dir, foreign, "1.4.3", require_native=True)


class IsolationTest(unittest.TestCase):
    """The probe environment is narrow, rooted in a private home, and bounded.

    Nothing here claims to disable networking: the proxy variables only deter a
    client that honours them, and the test below asserts exactly that much.
    """

    def test_environment_has_a_private_home_and_deterrent_proxies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = smoke.isolated_env(Path(tmp))
            self.assertEqual(env["HOME"], tmp)
            self.assertEqual(env["BUN_INSTALL"], f"{tmp}/.bun")
            self.assertTrue(env["HTTP_PROXY"].startswith("http://127.0.0.1:"))
            self.assertEqual(env["HTTPS_PROXY"], env["HTTP_PROXY"])
            self.assertNotIn("GITHUB_TOKEN", env)

    def test_environment_carries_no_ambient_variables(self) -> None:
        """Nothing from the parent process leaks in, so a run cannot be steered."""
        with tempfile.TemporaryDirectory() as tmp:
            env = smoke.isolated_env(Path(tmp))
            self.assertEqual(set(env), set(smoke.isolated_env(Path("/other"))))

    def test_proxy_aware_fetch_inside_the_environment_is_deterred(self) -> None:
        """Asserts deterrence only: a proxy-honouring fetch fails fast."""
        bun = _local_bun()
        if bun is None:
            self.skipTest("no native bun binary available to probe deterrence")
        js = (
            "try { await fetch('https://example.com'); console.log('REACHED'); } "
            "catch { console.log('BLOCKED'); }"
        )
        out = smoke.run_bounded([str(bun), "-e", js], Path("/tmp"), Path("/tmp"))
        self.assertEqual(out, "BLOCKED")

    def test_timeout_bound_is_enforced(self) -> None:
        if smoke.TIMEOUT_SECONDS > 2:
            original = smoke.TIMEOUT_SECONDS
            smoke.TIMEOUT_SECONDS = 1
            try:
                with self.assertRaisesRegex(ValueError, "exceeded the 1s bound"):
                    smoke.run_bounded(["/bin/sleep", "30"], Path("/tmp"), Path("/tmp"))
            finally:
                smoke.TIMEOUT_SECONDS = original


class ExecutionDisableTest(unittest.TestCase):
    """`--static-only` must stop execution, and must not fight `--require-native`."""

    def _matching_host_stage(self, root: Path) -> Path:
        bin_dir = root / "usr" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "bun").write_bytes(b"\x7fELF")
        (bin_dir / "bun").chmod(0o755)
        (bin_dir / "bunx").symlink_to("bun")
        return bin_dir

    def test_allow_execution_false_never_starts_a_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self._matching_host_stage(Path(tmp))
            with mock.patch.object(smoke, "native_arch", return_value="amd64"), \
                 mock.patch.object(smoke, "check_elf_arch"), \
                 mock.patch.object(smoke, "run_bounded") as run:
                results = smoke.smoke(
                    bin_dir, "amd64", "1.4.3", require_native=True, allow_execution=False
                )
            run.assert_not_called()
            self.assertTrue(any("execution disabled" in line for line in results))

    def test_static_only_contradicting_require_native_is_rejected(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()) as captured:
            with self.assertRaises(SystemExit):
                smoke.main(["--installed", "--arch", "amd64",
                            "--static-only", "--require-native"])
        self.assertIn("contradicts", captured.getvalue())

    def test_probes_use_the_private_home_not_the_staged_directory(self) -> None:
        seen: list[tuple] = []

        def run(command, cwd, home):
            seen.append((cwd, home))
            return "2-4-6" if "-e" in command else "1.4.3"

        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self._matching_host_stage(Path(tmp))
            with mock.patch.object(smoke, "native_arch", return_value="amd64"), \
                 mock.patch.object(smoke, "check_elf_arch"), \
                 mock.patch.object(smoke, "run_bounded", side_effect=run):
                smoke.smoke(bin_dir, "amd64", "1.4.3", require_native=True)
        private = seen[0][1]
        self.assertNotEqual(private, bin_dir)
        self.assertEqual(len(seen), 3)
        self.assertTrue(all(cwd == private for cwd, _ in seen))
        self.assertTrue(all(home == private for _, home in seen))


def _local_bun() -> Path | None:
    """A real bun binary from an already-built candidate pool, if there is one."""
    pool = Path("dist/bun-candidate")
    matches = sorted(pool.glob("bun_*_amd64.deb")) if pool.is_dir() else []
    if not matches or smoke.native_arch() != "amd64":
        return None
    target = Path(tempfile.mkdtemp(prefix="apt-bun-probe-"))
    subprocess.run(["dpkg-deb", "-x", str(matches[0]), str(target)], check=True, capture_output=True)
    binary = target / "usr" / "bin" / "bun"
    return binary if binary.is_file() else None


if __name__ == "__main__":
    unittest.main()