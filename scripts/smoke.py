"""Manifest-driven package checks for the native smoke jobs.

CI must not hard-code how many packages exist or which ones they are: the
manifest is the inventory. This helper therefore derives every expectation from
a manifest document — the committed catalog, or the manifest bound to a release
artifact — so a package added to the catalog needs no workflow edit.

Three modes, all read-only with respect to the repository:

- ``--count`` prints how many ``.deb`` files that manifest declares.
- ``--pool DIR`` checks each declared package in a built pool: control fields,
  the installed binary path, the ELF machine of that binary, and the declared
  dependencies. No upstream binary is executed.
- ``--installed`` runs the already-installed binaries with the read-only flags
  their own CLI documents and requires exit status 0. Only a native runner of
  the matching architecture may use it.
"""

from __future__ import annotations

import argparse
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

from .catalog import binary_name, depends_for, smoke_args
from .common import require_tool
from .manifest import Manifest, Package, deb_filename, load_manifest

# ELF e_machine values for the two published architectures.
ELF_MACHINE = {"amd64": "x86-64", "arm64": "aarch64"}
_EM_X86_64 = 62
_EM_AARCH64 = 183
_MACHINE_CODE = {"amd64": _EM_X86_64, "arm64": _EM_AARCH64}
# One binary is a single executable, so a fixture-sized ceiling is plenty.
MAX_BINARY_BYTES = 512 * 1024 * 1024


class SmokeError(RuntimeError):
    """A built or installed package does not match its manifest declaration."""


def elf_machine(path: Path) -> str:
    """Return the ELF machine name of a binary without executing it."""
    header = path.read_bytes()[:20]
    if len(header) < 20 or header[:4] != b"\x7fELF":
        raise SmokeError(f"{path} is not an ELF binary")
    little_endian = header[5] == 1
    code = struct.unpack("<H" if little_endian else ">H", header[18:20])[0]
    names = {value: name for name, value in (
        ("x86-64", _EM_X86_64), ("aarch64", _EM_AARCH64)
    )}
    if code not in names:
        raise SmokeError(f"{path} targets an unsupported machine: e_machine {code}")
    return names[code]


def control_fields(deb: Path) -> dict[str, str]:
    """Read one `.deb`'s control fields through dpkg-deb, never by unpacking."""
    proc = subprocess.run(
        ["dpkg-deb", "-f", str(deb)], text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if proc.returncode != 0:
        raise SmokeError(f"dpkg-deb -f {deb.name} failed: {proc.stderr.strip()}")
    fields: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if ": " in line:
            key, value = line.split(": ", 1)
            fields.setdefault(key.strip(), value.strip())
    return fields


def _expected_binary(pkg: Package) -> str:
    """The installed file name must agree with the recipe, not only the manifest."""
    declared = {pkg.targets[arch].binary for arch in pkg.targets}
    expected = binary_name(pkg.name)
    if declared != {expected}:
        raise SmokeError(
            f"{pkg.name} installs {sorted(declared)}, the reviewed recipe expects {expected}"
        )
    return expected


def check_package(pkg: Package, arch: str, *, manifest: Manifest, pool: Path) -> list[str]:
    """Check one declared package of one architecture; returns human findings."""
    deb = pool / deb_filename(pkg, arch, manifest)
    if not deb.is_file():
        raise SmokeError(f"pool is missing {deb.name}")
    fields = control_fields(deb)
    expected_fields = {
        "Package": pkg.name,
        "Version": f"{pkg.version}{manifest.debian_revision}",
        "Architecture": arch,
    }
    for key, value in expected_fields.items():
        if fields.get(key) != value:
            raise SmokeError(
                f"{deb.name} declares {key}: {fields.get(key)!r}, expected {value!r}"
            )
    declared_depends = depends_for(pkg.name, arch)
    if fields.get("Depends", "") != declared_depends:
        raise SmokeError(
            f"{deb.name} declares Depends: {fields.get('Depends', '')!r}, "
            f"expected {declared_depends!r}"
        )

    binary = _expected_binary(pkg)
    listing = subprocess.run(
        ["dpkg-deb", "-c", str(deb)], text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if listing.returncode != 0:
        raise SmokeError(f"dpkg-deb -c {deb.name} failed: {listing.stderr.strip()}")
    installed = {
        line.split()[-1]
        for line in listing.stdout.splitlines()
        if line.split() and line.split()[-1].startswith("./usr/bin/")
    }
    if f"./usr/bin/{binary}" not in installed:
        raise SmokeError(
            f"{deb.name} installs {sorted(installed)}, expected /usr/bin/{binary}"
        )
    return [
        f"{deb.name}: {expected_fields['Package']} {expected_fields['Version']} {arch} "
        f"installs /usr/bin/{binary} as {ELF_MACHINE[arch]}"
    ]


def check_elf(pkg: Package, arch: str, binary: Path) -> None:
    """Require a binary to be a real ELF file for the given architecture."""
    if not binary.is_file():
        raise SmokeError(f"{binary} is missing from the installed package")
    if binary.stat().st_size > MAX_BINARY_BYTES:
        raise SmokeError(f"{binary} is implausibly large: {binary.stat().st_size} bytes")
    machine = elf_machine(binary)
    if machine != ELF_MACHINE[arch]:
        raise SmokeError(f"{binary} is {machine}, expected {ELF_MACHINE[arch]}")


def _extract(deb: Path, destination: Path) -> Path:
    """Unpack one package into a scratch directory and return its root."""
    proc = subprocess.run(
        ["dpkg-deb", "-x", str(deb), str(destination)], text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if proc.returncode != 0:
        raise SmokeError(f"dpkg-deb -x {deb.name} failed: {proc.stderr.strip()}")
    return destination


def run_installed(manifest: Manifest, arch: str, prefix: Path = Path("/")) -> list[str]:
    """Run each installed binary with its documented read-only flag.

    The ELF machine is checked first, so a run can never claim an architecture it
    did not actually execute; the argument that follows is the one each project
    documents for a version print, and the client only ever prints its help.
    """
    findings: list[str] = []
    for pkg in sorted(manifest.packages, key=lambda p: p.name):
        binary = prefix / "usr" / "bin" / _expected_binary(pkg)
        check_elf(pkg, arch, binary)
        args = smoke_args(pkg.name)
        proc = subprocess.run(
            [str(binary), *args], text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
        )
        if proc.returncode != 0:
            raise SmokeError(
                f"{binary.name} {' '.join(args)} exited {proc.returncode}: "
                f"{proc.stdout.strip()[:400]}"
            )
        first_line = (proc.stdout.strip().splitlines() or [""])[0]
        findings.append(
            f"{binary} [{arch}] {' '.join(args)} exited 0: {first_line[:120]}"
        )
    return findings


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
                check_elf(pkg, arch, extracted / "usr" / "bin" / _expected_binary(pkg))
                findings.append(f"{pkg.name} {arch}: binary is {ELF_MACHINE[arch]}")
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
    except (SmokeError, ValueError, OSError) as exc:
        print(f"smoke failed: {exc}", file=sys.stderr)
        return 1
    print("choose one of --count, --pool, or --installed", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())