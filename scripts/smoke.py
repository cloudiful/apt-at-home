"""Manifest-driven package checks for the native smoke jobs.

CI must not hard-code how many packages exist or which ones they are: the
manifest is the inventory. This CLI derives every expectation from a manifest
document, so a package added to the catalog needs no workflow edit.

Three modes, all read-only with respect to the repository:

- ``--count`` prints how many ``.deb`` files that manifest declares.
- ``--pool DIR`` checks each declared package in a built pool (static checks in
  `smoke_package`). No upstream binary is executed.
- ``--installed`` runs the already-installed binaries of one architecture with
  their reviewed native check (`smoke_runtime`) and requires exit status 0. Only
  a native runner of the matching architecture may use it.
- ``--upgraded`` checks the dpkg state after an APT upgrade (`smoke_upgrade`).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

from .common import require_tool
from .manifest import Manifest, deb_filename, load_manifest
from .smoke_package import (
    ELF_MACHINE,
    MAX_BINARY_BYTES,
    SmokeError,
    check_copyright,
    check_elf,
    check_package,
    control_fields,
    elf_machine,
)
from .smoke_commands import binaries_for_target
from .smoke_runtime import run_installed
from .smoke_tree import check_relations, check_tree_payload
from .smoke_upgrade import check_upgraded_state

__all__ = [
    "ELF_MACHINE",
    "MAX_BINARY_BYTES",
    "SmokeError",
    "check_elf",
    "check_package",
    "check_pool",
    "control_fields",
    "elf_machine",
    "main",
    "run_installed",
]


def _extract(deb: Path, destination: Path) -> Path:
    """Unpack one package into a scratch directory and return its root."""
    proc = subprocess.run(
        ["dpkg-deb", "-x", str(deb), str(destination)], text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if proc.returncode != 0:
        raise SmokeError(f"dpkg-deb -x {deb.name} failed: {proc.stderr.strip()}")
    return destination


def check_pool(manifest: Manifest, pool: Path, arches: tuple[str, ...]) -> list[str]:
    """Check every declared package of every architecture in a built pool."""
    findings: list[str] = []
    with tempfile.TemporaryDirectory(prefix="apt-smoke-") as scratch:
        for arch in arches:
            for pkg in sorted(manifest.packages, key=lambda p: p.name):
                findings.extend(check_package(pkg, arch, manifest=manifest, pool=pool))
                extracted = _extract(
                    pool / deb_filename(pkg, arch, manifest),
                    Path(scratch) / f"{pkg.name}-{arch}",
                )
                check_copyright(pkg, extracted)
                for binary in binaries_for_target(pkg.targets[arch]):
                    check_elf(pkg, arch, extracted / "usr" / "bin" / binary)
                    findings.append(f"{pkg.name} {arch}: /usr/bin/{binary} is {ELF_MACHINE[arch]}")
                if pkg.targets[arch].install_tree:
                    deb = pool / deb_filename(pkg, arch, manifest)
                    check_relations(pkg, control_fields(deb))
                    findings.extend(
                        f"{pkg.name} {arch}: {line}"
                        for line in check_tree_payload(pkg, arch, extracted / "usr")
                    )
    return findings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.smoke",
        description="Manifest-driven package count, metadata, and native smoke checks.",
    )
    parser.add_argument("--manifest", required=True, help="manifest document to check against")
    modes = parser.add_argument_group("modes (choose one)")
    modes.add_argument("--count", action="store_true", help="print the expected .deb count")
    modes.add_argument("--pool", metavar="DIR", help="check the .deb files in this pool")
    modes.add_argument(
        "--installed", action="store_true",
        help="run the installed binaries for this architecture and require exit 0",
    )
    modes.add_argument(
        "--upgraded", action="store_true",
        help="check the APT-upgraded machine state: packaged neovim, distro runtime removed",
    )
    parser.add_argument(
        "--arch", default="amd64", choices=sorted(ELF_MACHINE),
        help="architecture for --installed (default: amd64)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = load_manifest(Path(args.manifest))
        if args.count:
            print(len(manifest.packages) * len(ELF_MACHINE))
            return 0
        if args.pool:
            require_tool("dpkg-deb")
            for line in check_pool(manifest, Path(args.pool), tuple(sorted(ELF_MACHINE))):
                print(f"ok: {line}")
            return 0
        if args.installed:
            for line in run_installed(manifest, args.arch):
                print(f"ok: {line}")
            return 0
        if args.upgraded:
            for line in check_upgraded_state(manifest):
                print(f"ok: {line}")
            return 0
    except (SmokeError, ValueError, OSError) as exc:
        print(f"smoke failed: {exc}", file=sys.stderr)
        return 1
    print("choose one of --count, --pool, or --installed", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
