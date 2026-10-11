"""Static checks of built `.deb` payloads: control fields, installed paths, and ELF machine.

Nothing here executes an upstream binary. Every check reads the package through
`dpkg-deb` or the ELF header, so a pool can be verified on any host.
"""

from __future__ import annotations

import struct
import subprocess
from pathlib import Path

from .catalog import binary_name, depends_for
from .manifest import Manifest, Package, deb_filename
from .package_licenses import copyright_text, read_vendor_licenses
from .smoke_commands import binaries_for_target

# ELF e_machine values for the two published architectures.
ELF_MACHINE = {"amd64": "x86-64", "arm64": "aarch64"}
_EM_X86_64 = 62
_EM_AARCH64 = 183
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


def expected_binary(pkg: Package) -> str:
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

    expected_binary(pkg)
    binaries = binaries_for_target(pkg.targets[arch])
    listing = subprocess.run(
        ["dpkg-deb", "-c", str(deb)], text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if listing.returncode != 0:
        raise SmokeError(f"dpkg-deb -c {deb.name} failed: {listing.stderr.strip()}")
    installed = {
        line.split()[-1]
        for line in listing.stdout.splitlines()
        if line.split()
        and line.split()[-1].startswith("./usr/bin/")
        and not line.split()[-1].endswith("/")
    }
    expected_installed = {f"./usr/bin/{binary}" for binary in binaries}
    if installed != expected_installed:
        raise SmokeError(
            f"{deb.name} installs {sorted(installed)}, expected "
            f"{sorted(expected_installed)}"
        )
    return [
        f"{deb.name}: {expected_fields['Package']} {expected_fields['Version']} {arch} "
        f"installs {', '.join('/usr/bin/' + binary for binary in binaries)}"
    ]


def check_copyright(pkg: Package, package_root: Path) -> None:
    """Require the packaged copyright file to carry every reviewed license text."""
    path = package_root / "usr" / "share" / "doc" / pkg.name / "copyright"
    if not path.is_file():
        raise SmokeError(f"{pkg.name} package is missing its Debian copyright file")
    expected = copyright_text(pkg, *read_vendor_licenses(pkg))
    if path.read_text(encoding="utf-8") != expected:
        raise SmokeError(f"{pkg.name} Debian copyright does not match reviewed license texts")


def check_elf(pkg: Package, arch: str, binary: Path) -> None:
    """Require a binary to be a real ELF file for the given architecture."""
    if not binary.is_file():
        raise SmokeError(f"{binary} is missing from the installed package")
    if binary.stat().st_size > MAX_BINARY_BYTES:
        raise SmokeError(f"{binary} is implausibly large: {binary.stat().st_size} bytes")
    machine = elf_machine(binary)
    if machine != ELF_MACHINE[arch]:
        raise SmokeError(f"{binary} is {machine}, expected {ELF_MACHINE[arch]}")
