"""Native execution checks for installed binaries on a runner of the same architecture.

A check runs only after the binary's ELF machine matches the architecture, so a
run never claims an architecture it did not execute. Each package runs with the
read-only flag its own recipe documents. Neovim is the exception: its check is a
reviewed headless handler that proves the packaged runtime and the bundled
tree-sitter Lua parser load, and it fails closed with an explicit nonzero exit.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from .catalog import smoke_args
from .manifest import Manifest, Package
from .smoke_commands import (
    CommandProbe,
    binaries_for_target,
    check_probe_output,
    extra_command_probes,
)
from .smoke_package import SmokeError, check_elf, expected_binary
from .kandev_smoke import check_bundle_payload, check_version_output

# A static Lua contract: each step must hold, and any failure exits 1 explicitly
# so a silent error can never report success.
NEOVIM_LUA_CHECK = (
    "local ok, err = pcall(function()"
    " assert(vim.env.VIMRUNTIME, 'VIMRUNTIME unset')"
    " local help = io.open(vim.env.VIMRUNTIME .. '/doc/help.txt', 'r')"
    " assert(help, 'doc/help.txt unreadable'); help:close()"
    " assert(require('vim.treesitter'), 'vim.treesitter missing')"
    " assert(vim.treesitter.language.add('lua'), 'lua parser not registered')"
    " local tree = vim.treesitter.get_string_parser('local value = 1', 'lua'):parse()"
    " assert(tree and #tree > 0, 'lua parse produced no tree')"
    " end)"
    " if not ok then io.stderr:write(tostring(err), '\\n'); vim.cmd('cquit 1')"
    " else io.write('neovim runtime and lua parser ok\\n') end"
)
NEOVIM_ARGS = (
    "--headless", "--clean", "-u", "NONE", "-i", "NONE", "--noplugin",
    "-c", f"lua {NEOVIM_LUA_CHECK}", "-c", "qa!",
)
RUN_TIMEOUT_SECONDS = 30
# Runtime probes get isolated user directories; uv commands stay offline.
_ISOLATED_ENV = ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")


def _run(binary: Path, args: tuple[str, ...], *, scratch: Path) -> subprocess.CompletedProcess[str]:
    """Execute one installed binary in an isolated home; capture its output."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "UV_OFFLINE": "1",
    }
    for name in _ISOLATED_ENV:
        env[name] = str(scratch / name.lower())
        (scratch / name.lower()).mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        [str(binary), *args], text=True, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
        timeout=RUN_TIMEOUT_SECONDS,
    )


def _neovim_args() -> tuple[str, ...]:
    return NEOVIM_ARGS


def _generic_args(package_name: str) -> tuple[str, ...]:
    return smoke_args(package_name)


# The reviewed handler table: a package not listed here runs its recipe flag.
HANDLERS: dict[str, Callable[[str], tuple[str, ...]]] = {
    "neovim": lambda _name: _neovim_args(),
}


def arguments_for(package_name: str) -> tuple[str, ...]:
    handler = HANDLERS.get(package_name)
    return handler(package_name) if handler else _generic_args(package_name)


def _run_probe(
    pkg: Package, arch: str, probe: CommandProbe, *, prefix: Path, scratch: Path
) -> str:
    target = pkg.targets[arch]
    if probe.binary not in binaries_for_target(target):
        raise SmokeError(f"{pkg.name} smoke probe names uninstalled command {probe.binary}")
    binary = prefix / "usr" / "bin" / probe.binary
    if target.bundle_type:
        check_bundle_payload(pkg, arch, prefix)
    else:
        check_elf(pkg, arch, binary)
    proc = _run(binary, probe.args, scratch=scratch)
    if proc.returncode != 0:
        raise SmokeError(
            f"{binary.name} {' '.join(probe.args[:2])} exited {proc.returncode}: "
            f"{proc.stdout.strip()[:400]}"
        )
    try:
        check_probe_output(probe, proc.stdout)
    except ValueError as exc:
        raise SmokeError(str(exc)) from exc
    if target.bundle_type:
        check_version_output(pkg, proc.stdout)
    first_line = (proc.stdout.strip().splitlines() or [""])[0]
    return f"{binary} [{arch}] {' '.join(probe.args[:2])} exited 0: {first_line[:120]}"


def run_package(pkg: Package, arch: str, *, prefix: Path, scratch: Path) -> list[str]:
    """Run every reviewed command check for one installed package."""
    probes = (
        CommandProbe(expected_binary(pkg), arguments_for(pkg.name)),
        *extra_command_probes(pkg.name),
    )
    findings = [
        _run_probe(pkg, arch, probe, prefix=prefix, scratch=scratch)
        for probe in probes
    ]
    if pkg.name == "cargo-tauri":
        findings.append(_cargo_tauri_external(pkg, arch, prefix=prefix, scratch=scratch))
    return findings


def _cargo_tauri_external(
    pkg: Package, arch: str, *, prefix: Path, scratch: Path
) -> str:
    """Run `cargo tauri --version` against the installed `cargo-tauri`.

    Cargo resolves an external subcommand by looking for `cargo-tauri` on
    `PATH`, so the installed `/usr/bin` directory is prepended there. The
    output must equal the direct `cargo-tauri --version` line
    (`tauri-cli {version}`), proving the binary works both directly and as a
    Cargo subcommand. A missing `cargo` fails closed because `cargo` is the
    package's own declared dependency.
    """
    binary = prefix / "usr" / "bin" / "cargo-tauri"
    check_elf(pkg, arch, binary)
    cargo = shutil.which("cargo")
    if cargo is None:
        raise SmokeError("cargo tauri --version needs a cargo on PATH")
    # Cargo must resolve `cargo-tauri` to the installed binary, not to
    # whatever else the machine has.
    probe_path = f"{prefix / 'usr' / 'bin'}:{Path(cargo).parent}"
    resolved = shutil.which("cargo-tauri", path=probe_path)
    if resolved is None or Path(resolved).resolve() != binary.resolve():
        raise SmokeError(
            f"cargo would not resolve cargo-tauri to the installed binary "
            f"(found {resolved!r})"
        )
    env = {
        "PATH": probe_path,
        "LANG": "C.UTF-8",
        "UV_OFFLINE": "1",
    }
    for name in _ISOLATED_ENV:
        env[name] = str(scratch / name.lower())
        (scratch / name.lower()).mkdir(parents=True, exist_ok=True)
    try:
        completed = subprocess.run(
            [cargo, "tauri", "--version"], text=True, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
            timeout=RUN_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise SmokeError("cargo tauri --version exceeded the 30s bound") from exc
    if completed.returncode != 0:
        raise SmokeError(
            f"cargo tauri --version exited {completed.returncode}: "
            f"{completed.stdout.strip()[:400]}"
        )
    expected = f"tauri-cli {pkg.version}"
    reported = completed.stdout.strip()
    if reported != expected:
        raise SmokeError(
            f"cargo tauri --version printed {reported!r}, expected {expected!r}"
        )
    return f"cargo tauri --version -> {reported} (cargo external subcommand)"


def run_installed(manifest: Manifest, arch: str, prefix: Path = Path("/")) -> list[str]:
    """Run each installed binary with its reviewed check; stops on the first failure."""
    findings: list[str] = []
    scratch = Path(os.environ.get("TMPDIR", "/tmp")) / "apt-smoke-run"
    scratch.mkdir(parents=True, exist_ok=True)
    for pkg in sorted(manifest.packages, key=lambda p: p.name):
        findings.extend(run_package(pkg, arch, prefix=prefix, scratch=scratch))
    return findings
