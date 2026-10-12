"""Bounded static and native checks for a built Bun candidate.

Two rules govern this module.

First, an upstream binary is *untrusted code*. It is never run before its
architecture has been read out of the ELF header and matched against the target
we asked for, so a mislabelled arm64 asset can never be executed on amd64. When
the requested architecture is not this machine's, the checks stay static and say
so, rather than quietly skipping. `--static-only` is an unconditional execution
disable: it returns after the static checks no matter what the host is or what
`--require-native` says.

Second, a run is *narrowed*, not sandboxed. It gets a private temporary HOME and
XDG tree, a minimal environment, and a 30 second wall-clock bound. The proxy
variables are set to a closed port, which deters proxy-aware requests — but that
is deterrence, not isolation: a client that ignores the proxy variables or opens
a raw socket is not stopped by this module, and nothing here should be described
as disabling the network. Only `bun --version`, `bunx --version` and one
in-memory JavaScript evaluation run; `bunx` is never allowed to fetch a package,
because that would turn a smoke test into an uncontrolled request.
"""

from __future__ import annotations

import argparse
import os
import platform
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

TIMEOUT_SECONDS = 30

# ELF e_machine values for the two Debian architectures this repository builds.
_MACHINE = {"amd64": 62, "arm64": 183}

_HOST = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}

# One pure-JavaScript evaluation with a single deterministic answer: it proves
# the runtime parses and executes JS without touching the filesystem or network.
SMOKE_JS = "console.log([1, 2, 3].map((n) => n * 2).join('-'))"
SMOKE_JS_OUTPUT = "2-4-6"

# A closed port that nothing listens on: a fetch attempt fails immediately
# instead of hanging, so the 30s bound is a backstop rather than the first guard.
_DEAD_PROXY = "http://127.0.0.1:9"


def native_arch() -> str | None:
    """This machine's Debian architecture name, or None when it is not supported."""
    return _HOST.get(platform.machine())


def read_elf(path: Path) -> tuple[int, int]:
    """Return (class bits, e_machine) from an ELF header, without executing it."""
    with path.open("rb") as handle:
        header = handle.read(20)
    if len(header) < 20 or header[:4] != b"\x7fELF":
        raise ValueError(f"{path} is not an ELF file")
    elf_class = {1: 32, 2: 64}.get(header[4])
    if elf_class is None:
        raise ValueError(f"{path} has an unknown ELF class byte {header[4]}")
    if header[5] != 1:
        raise ValueError(f"{path} is not a little-endian ELF file")
    (machine,) = struct.unpack("<H", header[18:20])
    return elf_class, machine


def check_elf_arch(binary: Path, arch: str) -> None:
    """Refuse to go further if the binary is not the architecture we pinned."""
    elf_class, machine = read_elf(binary)
    if elf_class != 64:
        raise ValueError(f"{binary} is {elf_class}-bit, but the candidate is 64-bit")
    expected = _MACHINE[arch]
    if machine != expected:
        raise ValueError(
            f"{binary} has ELF machine {machine}, but {arch} requires {expected}"
        )


def isolated_env(home: Path) -> dict[str, str]:
    """A minimal environment for a probe, rooted at a private home directory.

    The proxy variables point at a closed port so that a proxy-aware client
    fails fast rather than reaching a real proxy. That is request deterrence
    only; it does not disable networking and is not a sandbox.
    """
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "BUN_INSTALL": str(home / ".bun"),
        "LANG": "C",
        "LC_ALL": "C",
        "TZ": "UTC",
        "HTTP_PROXY": _DEAD_PROXY,
        "HTTPS_PROXY": _DEAD_PROXY,
        "ALL_PROXY": _DEAD_PROXY,
        "http_proxy": _DEAD_PROXY,
        "https_proxy": _DEAD_PROXY,
        "no_proxy": "",
    }


def run_bounded(command: list[str], cwd: Path, home: Path) -> str:
    """Run one bounded command in the given home/cwd and return stdout, or refuse."""
    try:
        completed = subprocess.run(
            command, cwd=cwd, env=isolated_env(home), timeout=TIMEOUT_SECONDS,
            capture_output=True, text=True, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"{command[0]} exceeded the {TIMEOUT_SECONDS}s bound") from exc
    if completed.returncode != 0:
        raise ValueError(
            f"{' '.join(command)} exited {completed.returncode}: {completed.stderr.strip()}"
        )
    return completed.stdout.strip()


def native_checks(bun: Path, bunx: Path, version: str, home: Path) -> list[str]:
    """Run the three bounded checks inside `home` and return their result lines.

    `home` is a private temporary directory used both as the working directory
    and as HOME/XDG/BUN_INSTALL root, so a probe never reads or writes the
    invoking user's real home, the staged `/usr/bin`, or shared system storage.
    """
    if not bunx.is_symlink():
        raise ValueError(f"{bunx} is not a symlink; the owned alias is missing")
    if os.readlink(bunx) != "bun":
        raise ValueError(f"{bunx} points at {os.readlink(bunx)}, expected bun")
    reported = run_bounded([str(bun), "--version"], home, home)
    if reported != version:
        raise ValueError(f"bun --version printed {reported!r}, expected {version!r}")
    aliased = run_bounded([str(bunx), "--version"], home, home)
    if aliased != version:
        raise ValueError(f"bunx --version printed {aliased!r}, expected {version!r}")
    evaluated = run_bounded([str(bun), "-e", SMOKE_JS], home, home)
    if evaluated != SMOKE_JS_OUTPUT:
        raise ValueError(f"bun -e printed {evaluated!r}, expected {SMOKE_JS_OUTPUT!r}")
    return [
        f"bun --version -> {reported}",
        f"bunx --version -> {aliased} (alias bunx -> bun)",
        f"bun -e static JS -> {evaluated}",
    ]


def smoke(
    bin_dir: Path,
    arch: str,
    version: str,
    *,
    require_native: bool = False,
    allow_execution: bool = True,
) -> list[str]:
    """Check a staged or installed payload; returns one result line per check.

    `allow_execution=False` is an unconditional execution disable: the static
    checks still run and report, but no subprocess is started whatever the host
    architecture is. `require_native` only tightens the *skip* case into an
    error; it never re-enables execution that `allow_execution` disabled.
    """
    bun, bunx = bin_dir / "bun", bin_dir / "bunx"
    if not bun.is_file() or bun.is_symlink():
        raise ValueError(f"{bun} is missing or is not a regular file")
    check_elf_arch(bun, arch)
    mode = bun.stat().st_mode & 0o777
    if mode != 0o755:
        raise ValueError(f"{bun} has mode {mode:o}, expected 755")
    results = [f"{bun.name}: ELF 64-bit {arch}, mode 0755"]
    if not allow_execution:
        results.append("execution disabled by --static-only; static checks only")
        return results
    host = native_arch()
    if host != arch:
        if require_native:
            raise ValueError(f"native smoke needs an {arch} machine, this is {host}")
        results.append(f"native run skipped: this machine is {host}, not {arch}")
        return results
    with tempfile.TemporaryDirectory(prefix="apt-bun-smoke-") as private:
        results.extend(native_checks(bun, bunx, version, Path(private)))
    return results


def deb_payload_bin(pool: Path, arch: str) -> Path:
    """Extract the candidate deb for `arch` and return its extracted payload root."""
    matches = sorted(pool.glob(f"bun_*_{arch}.deb"))
    if len(matches) != 1:
        raise ValueError(f"expected exactly one bun {arch} deb in {pool}, found {len(matches)}")
    target = Path(tempfile.mkdtemp(prefix=f"apt-bun-pool-{arch}-"))
    subprocess.run(
        ["dpkg-deb", "-x", str(matches[0]), str(target)], check=True, capture_output=True,
    )
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.bun_candidate_smoke",
        description="Static and bounded native checks for a Bun candidate package.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--pool", type=Path, help="directory holding the built candidate debs")
    source.add_argument("--installed", action="store_true", help="check the installed /usr/bin")
    parser.add_argument("--arch", choices=("amd64", "arm64"), required=True)
    parser.add_argument("--version", default=None, help="expected --version output")
    parser.add_argument("--prefix", type=Path, default=Path("/"),
                        help="install root for --installed; defaults to /")
    parser.add_argument("--static-only", action="store_true",
                        help="never execute the binary, whatever the host architecture")
    parser.add_argument("--require-native", action="store_true",
                        help="fail instead of skipping when this machine is not --arch")
    args = parser.parse_args(argv)
    if args.static_only and args.require_native:
        parser.error("--static-only disables execution and contradicts --require-native")
    version = args.version or _pinned_version()
    try:
        if args.pool is not None:
            root = deb_payload_bin(args.pool, args.arch)
            bin_dir = root / "usr" / "bin"
        else:
            bin_dir = args.prefix / "usr" / "bin"
        for line in smoke(
            bin_dir, args.arch, version,
            require_native=args.require_native, allow_execution=not args.static_only,
        ):
            print(line)
    except (ValueError, subprocess.CalledProcessError) as exc:
        print(f"smoke failed: {exc}", file=sys.stderr)
        return 1
    return 0


def _pinned_version() -> str:
    from .bun_candidate_metadata import load_candidate
    return load_candidate().package.version


if __name__ == "__main__":
    sys.exit(main())