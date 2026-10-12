"""Static bundle checks and the bounded native Kandev launcher probe."""

from __future__ import annotations

import stat
from pathlib import Path

from .catalog import tag_for
from .kandev_bundle import (
    BUNDLE_TYPE,
    MAX_MANIFEST_BYTES,
    parse_remote_helper_manifest,
)
from .manifest import Package
from .package_licenses import kandev_source_document
from .smoke_package import ELF_MACHINE, SmokeError, check_elf

_WRAPPER = b'#!/bin/sh\nexec /usr/lib/kandev/bin/kandev "$@"\n'


def _regular_file(path: Path, mode: int) -> None:
    try:
        entry = path.lstat()
    except OSError as exc:
        raise SmokeError(f"Kandev bundle file is missing: {path}") from exc
    if not stat.S_ISREG(entry.st_mode):
        raise SmokeError(f"Kandev bundle path is not a regular file: {path}")
    if stat.S_IMODE(entry.st_mode) != mode:
        raise SmokeError(f"Kandev bundle file has mode {stat.S_IMODE(entry.st_mode):04o}: {path}")


def check_bundle_payload(pkg: Package, arch: str, package_root: Path) -> list[str]:
    """Verify the installed wrapper, bundle manifest, binaries, and source pointer."""
    target = pkg.targets[arch]
    if pkg.name != "kandev" or target.bundle_type != BUNDLE_TYPE:
        raise SmokeError("Kandev smoke received a package outside the reviewed standard bundle")
    wrapper = package_root / "usr" / "bin" / "kandev"
    _regular_file(wrapper, 0o755)
    if wrapper.read_bytes() != _WRAPPER:
        raise SmokeError("/usr/bin/kandev does not exec the packaged bundle with quoted arguments")

    bundle = package_root / "usr" / "lib" / "kandev"
    manifest_path = bundle / "remote-helpers.json"
    _regular_file(manifest_path, 0o644)
    manifest_bytes = manifest_path.read_bytes()
    if len(manifest_bytes) > MAX_MANIFEST_BYTES:
        raise SmokeError("Kandev remote-helpers.json exceeds its size limit")
    try:
        remote = parse_remote_helper_manifest(manifest_bytes, tag_for(pkg.name, pkg.version))
    except ValueError as exc:
        raise SmokeError(f"Kandev remote-helpers.json is invalid: {exc}") from exc

    for relative in ("bin/kandev", "bin/agentctl"):
        binary = bundle / relative
        _regular_file(binary, 0o755)
        check_elf(pkg, arch, binary)
    if ELF_MACHINE[arch] not in ("x86-64", "aarch64"):
        raise SmokeError(f"unsupported Kandev architecture: {arch}")

    source_doc = package_root / "usr" / "share" / "doc" / "kandev" / "README.source"
    _regular_file(source_doc, 0o644)
    expected = kandev_source_document(pkg.version, remote).encode("utf-8")
    if source_doc.read_bytes() != expected:
        raise SmokeError("Kandev README.source does not match the pinned bundle version and commit")
    return [
        f"kandev {arch}: standard bundle manifest, wrapper, and source reference verified",
        f"kandev {arch}: bundled kandev and agentctl are {ELF_MACHINE[arch]} ELF files",
    ]


def check_version_output(pkg: Package, output: str) -> None:
    """Require the native no-server version command to identify the pinned release."""
    expected = tag_for(pkg.name, pkg.version)
    if output.strip() != expected:
        raise SmokeError(f"kandev --version returned {output.strip()!r}, expected {expected!r}")
