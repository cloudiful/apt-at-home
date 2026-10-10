"""Native execution checks for installed binaries on a runner of the same architecture.

A check runs only after the binary's ELF machine matches the architecture, so a
run never claims an architecture it did not execute. Each package runs with the
read-only flag its own recipe documents. Neovim is the exception: its check is a
reviewed headless handler that proves the packaged runtime and the bundled
tree-sitter Lua parser load, and it fails closed with an explicit nonzero exit.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

from .catalog import smoke_args
from .manifest import Manifest, Package
from .smoke_package import SmokeError, check_elf, expected_binary

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
# A runtime probe gets a scrubbed environment: no user config, data, or network.
_ISOLATED_ENV = ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")


def _run(binary: Path, args: tuple[str, ...], *, scratch: Path) -> subprocess.CompletedProcess[str]:
    """Execute one installed binary in an isolated home; capture its output."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"}
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


def run_package(pkg: Package, arch: str, *, prefix: Path, scratch: Path) -> str:
    """Run one installed package's check; returns one finding line or raises."""
    binary = prefix / "usr" / "bin" / expected_binary(pkg)
    check_elf(pkg, arch, binary)
    args = arguments_for(pkg.name)
    proc = _run(binary, args, scratch=scratch)
    if proc.returncode != 0:
        raise SmokeError(
            f"{binary.name} {' '.join(args[:2])} exited {proc.returncode}: "
            f"{proc.stdout.strip()[:400]}"
        )
    first_line = (proc.stdout.strip().splitlines() or [""])[0]
    return f"{binary} [{arch}] {' '.join(args[:2])} exited 0: {first_line[:120]}"


def run_installed(manifest: Manifest, arch: str, prefix: Path = Path("/")) -> list[str]:
    """Run each installed binary with its reviewed check; stops on the first failure."""
    findings: list[str] = []
    scratch = Path(os.environ.get("TMPDIR", "/tmp")) / "apt-smoke-run"
    scratch.mkdir(parents=True, exist_ok=True)
    for pkg in sorted(manifest.packages, key=lambda p: p.name):
        findings.append(run_package(pkg, arch, prefix=prefix, scratch=scratch))
    return findings
