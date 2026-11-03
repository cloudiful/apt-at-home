"""Static bundle checks and the bounded native Pi launcher probe."""

from __future__ import annotations

import stat
from pathlib import Path

from .manifest import Package
from .package_licenses import pi_source_document
from .pi_bundle import (
    BUNDLE_TYPE,
    MAX_PACKAGE_JSON_BYTES,
    launcher_bytes,
    native_helper_path,
    parse_package_manifest,
    update_helper_bytes,
)
from .smoke_package import ELF_MACHINE, SmokeError, check_elf

_REQUIRED_FILES = (
    "pi/photon_rs_bg.wasm",
    "pi/package.json",
    "pi/README.md",
    "pi/CHANGELOG.md",
    "pi/theme/dark.json",
    "pi/theme/light.json",
    "pi/export-html/template.html",
)
_REQUIRED_TREES = ("pi/assets", "pi/docs", "pi/examples", "pi/export-html/vendor")


def _regular_file(path: Path, mode: int) -> None:
    try:
        entry = path.lstat()
    except OSError as exc:
        raise SmokeError(f"Pi bundle file is missing: {path}") from exc
    if not stat.S_ISREG(entry.st_mode):
        raise SmokeError(f"Pi bundle path is not a regular file: {path}")
    if stat.S_IMODE(entry.st_mode) != mode:
        raise SmokeError(f"Pi bundle file has mode {stat.S_IMODE(entry.st_mode):04o}: {path}")


def _nonempty_tree(root: Path, relative: str) -> None:
    tree = root / relative
    if not tree.is_dir() or not any(path.is_file() for path in tree.rglob("*")):
        raise SmokeError(f"Pi bundle is missing the {relative} resources")


def check_bundle_payload(pkg: Package, arch: str, package_root: Path) -> list[str]:
    """Verify the installed launcher, APT helper, resources, and ELF identities."""
    target = pkg.targets[arch]
    if pkg.name != "pi" or target.bundle_type != BUNDLE_TYPE:
        raise SmokeError("Pi smoke received a package outside the reviewed standard bundle")
    launcher = package_root / "usr" / "bin" / "pi"
    _regular_file(launcher, 0o755)
    if launcher.read_bytes() != launcher_bytes():
        raise SmokeError(
            "/usr/bin/pi does not force the packaged APT update helper with quoted arguments"
        )

    bundle = package_root / "usr" / "lib" / "pi"
    helper = bundle / "apt-update"
    _regular_file(helper, 0o755)
    if helper.read_bytes() != update_helper_bytes():
        raise SmokeError("/usr/lib/pi/apt-update does not refuse self-update as reviewed")

    for relative in ("pi", "native/" + native_helper_path(arch).removeprefix("pi/native/")):
        binary = bundle / relative
        _regular_file(binary, 0o755)
        check_elf(pkg, arch, binary)

    for relative in _REQUIRED_FILES:
        path = package_root / "usr" / "lib" / relative
        _regular_file(path, 0o644)
    for tree in _REQUIRED_TREES:
        _nonempty_tree(bundle, tree.removeprefix("pi/"))

    package_json = (bundle / "package.json").read_bytes()
    if len(package_json) > MAX_PACKAGE_JSON_BYTES:
        raise SmokeError("Pi package.json exceeds its size limit")
    try:
        parse_package_manifest(package_json, pkg.version)
    except ValueError as exc:
        raise SmokeError(f"Pi package.json is invalid: {exc}") from exc

    source_doc = package_root / "usr" / "share" / "doc" / "pi" / "README.source"
    _regular_file(source_doc, 0o644)
    expected = pi_source_document(pkg.version).encode("utf-8")
    if source_doc.read_bytes() != expected:
        raise SmokeError("Pi README.source does not match the pinned bundle version")
    return [
        f"pi {arch}: launcher, APT update helper, resources, and package metadata verified",
        f"pi {arch}: bundled pi and X11 clipboard helper are {ELF_MACHINE[arch]} ELF files",
    ]


def check_version_output(pkg: Package, output: str) -> None:
    """Require the native version command to identify the pinned release."""
    if output.strip() != pkg.version:
        raise SmokeError(f"pi --version returned {output.strip()!r}, expected {pkg.version!r}")
