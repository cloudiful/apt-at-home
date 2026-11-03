"""Validate and stage Pi's reviewed standalone bundle."""

from __future__ import annotations

import json
import re
import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path

from .deb import finalize_package_stage
from .manifest import Manifest, Package
from .package_licenses import pi_source_document, read_vendor_licenses

BUNDLE_TYPE = "pi-standard"
MAX_ARCHIVE_BYTES = 160 * 1024 * 1024
MAX_EXPANDED_BYTES = 512 * 1024 * 1024
MAX_MEMBERS = 1024
MAX_BINARY_BYTES = 256 * 1024 * 1024
MAX_RESOURCE_BYTES = 64 * 1024 * 1024
MAX_PACKAGE_JSON_BYTES = 64 * 1024
_ELF_HEADER_SIZE = 64
_ELF_MACHINES = {"amd64": 62, "arm64": 183}
_NATIVE_TRIPLES = {"amd64": "x64", "arm64": "arm64"}
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_PACKAGE_NAME = "@earendil-works/pi-coding-agent"
# Pi's examples carry reviewed dotfiles such as `.gitignore`; a component may
# start with a dot, but `.` and `..` are still refused as path components.
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9._+-]+$")

_ROOT = "pi"
_TREES = ("pi/theme", "pi/assets", "pi/export-html", "pi/docs", "pi/examples")
_REQUIRED_TREES = ("pi/assets", "pi/docs", "pi/examples", "pi/export-html/vendor")
_REQUIRED_FILES = frozenset({
    "pi/pi",
    "pi/photon_rs_bg.wasm",
    "pi/package.json",
    "pi/README.md",
    "pi/CHANGELOG.md",
    "pi/theme/dark.json",
    "pi/theme/light.json",
    "pi/export-html/template.html",
})
_NATIVE_PREFIX = "pi/native/linux/prebuilds"
_LAUNCHER = (
    b"#!/bin/sh\n"
    b"PI_SELF_UPDATE_COMMAND=/usr/lib/pi/apt-update\n"
    b"export PI_SELF_UPDATE_COMMAND\n"
    b'exec /usr/lib/pi/pi "$@"\n'
)
_UPDATE_HELPER = (
    b"#!/bin/sh\n"
    b"printf '%s\\n'"
    b" 'pi is managed by APT and cannot update itself.'"
    b" 'Run: sudo apt upgrade pi'"
    b" 'or:  sudo apt install --only-upgrade pi' >&2\n"
    b"exit 1\n"
)


@dataclass(frozen=True)
class BundleFile:
    source: str
    relative: str
    size: int
    executable: bool


@dataclass(frozen=True)
class BundleInventory:
    bundle_type: str
    expected_version: str
    expected_arch: str
    files: tuple[BundleFile, ...]
    package_json: bytes


def launcher_bytes() -> bytes:
    """Return the packaged /usr/bin/pi launcher this release installs."""
    return _LAUNCHER


def update_helper_bytes() -> bytes:
    """Return the packaged /usr/lib/pi/apt-update helper this release installs."""
    return _UPDATE_HELPER


def native_helper_path(arch: str) -> str:
    """Return the reviewed X11 clipboard helper member for one architecture."""
    try:
        triple = _NATIVE_TRIPLES[arch]
    except KeyError:
        raise ValueError(f"unsupported Pi architecture: {arch!r}") from None
    return f"{_NATIVE_PREFIX}/linux-{triple}/linux-platform-x11.node"


def _safe_member(name: str) -> str:
    """Validate a Pi archive member path, allowing reviewed dotfile names."""
    if not name or "\\" in name or "\0" in name or name.startswith("/"):
        raise ValueError(f"unsafe archive member name: {name!r}")
    if any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise ValueError(f"unsafe archive member name: {name!r}")
    relative = name[2:] if name.startswith("./") else name
    if not relative or relative.endswith("/"):
        raise ValueError(f"unsafe archive member name: {name!r}")
    for component in relative.split("/"):
        if component in (".", "..") or not _SAFE_COMPONENT.fullmatch(component):
            raise ValueError(f"unsafe archive member name: {name!r}")
    return relative


def _check_elf_header(stream, *, name: str, expected_arch: str) -> None:
    header = stream.read(_ELF_HEADER_SIZE)
    if len(header) != _ELF_HEADER_SIZE or header[:4] != b"\x7fELF":
        raise ValueError(f"Pi member {name!r} is not a complete ELF64 header")
    if header[4] != 2:
        raise ValueError(f"Pi member {name!r} is not ELF64")
    if header[5] != 1:
        raise ValueError(f"Pi member {name!r} is not little-endian ELF")
    if header[6] != 1 or int.from_bytes(header[52:54], "little") != _ELF_HEADER_SIZE:
        raise ValueError(f"Pi member {name!r} has a malformed ELF64 header")
    machine = int.from_bytes(header[18:20], "little")
    if machine != _ELF_MACHINES[expected_arch]:
        raise ValueError(
            f"Pi member {name!r} targets e_machine {machine}, expected "
            f"{expected_arch} ({_ELF_MACHINES[expected_arch]})"
        )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"package.json repeats key {key!r}")
        result[key] = value
    return result


def parse_package_manifest(data: bytes, expected_version: str) -> bytes:
    """Validate the bundled package.json against the pinned release, return its bytes."""
    if len(data) > MAX_PACKAGE_JSON_BYTES:
        raise ValueError("package.json exceeds the size limit")
    try:
        raw = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"package.json is invalid: {exc}") from None
    if not isinstance(raw, dict):
        raise ValueError("package.json is not an object")
    if raw.get("name") != _PACKAGE_NAME:
        raise ValueError(f"package.json names {raw.get('name')!r}, expected {_PACKAGE_NAME!r}")
    if raw.get("version") != expected_version:
        raise ValueError(
            f"package.json version {raw.get('version')!r} does not match {expected_version!r}"
        )
    binaries = raw.get("bin")
    if not isinstance(binaries, dict) or binaries.get("pi") != "dist/bundle/cli.js":
        raise ValueError("package.json does not declare the reviewed pi command")
    return data


def _dir_allowed(path: str, arch: str) -> bool:
    if path == _ROOT:
        return True
    if any(path == tree or path.startswith(tree + "/") for tree in _TREES):
        return True
    native = native_helper_path(arch)
    parents = {native.rsplit("/", index)[0] for index in range(1, native.count("/") + 1)}
    return path in parents


def _file_allowed(path: str, arch: str) -> bool:
    if path == native_helper_path(arch):
        return True
    if path in _REQUIRED_FILES:
        return True
    return any(path.startswith(tree + "/") for tree in _TREES)


def _check_member_mode(item: tarfile.TarInfo, *, name: str, executable: bool) -> None:
    if item.mode & 0o7022:
        raise ValueError(f"refusing dangerous Pi member mode: {name!r}")
    expected = 0o755 if executable else 0o644
    if item.mode != expected:
        raise ValueError(
            f"Pi member {name!r} has mode {item.mode:04o}, expected {expected:04o}"
        )


def inventory_bundle(
    archive: Path, *, expected_version: str, expected_arch: str,
    bundle_type: str = BUNDLE_TYPE,
) -> BundleInventory:
    """Validate archive membership, resource presence, ELF identity, and version."""
    if bundle_type != BUNDLE_TYPE:
        raise ValueError(f"unsupported Pi bundle type: {bundle_type!r}")
    if expected_arch not in _ELF_MACHINES:
        raise ValueError(f"unsupported Pi architecture: {expected_arch!r}")
    version = expected_version.removeprefix("v")
    if not _VERSION.fullmatch(version):
        raise ValueError(f"invalid Pi release version: {expected_version!r}")
    if archive.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError(f"Pi archive exceeds the {MAX_ARCHIVE_BYTES} byte compressed limit")
    try:
        with tarfile.open(archive, "r:gz") as tar:
            seen: set[str] = set()
            dirs: set[str] = set()
            files: list[BundleFile] = []
            total = 0
            member_count = 0
            package_json: bytes | None = None
            native = native_helper_path(expected_arch)
            for item in tar:
                member_count += 1
                if member_count > MAX_MEMBERS:
                    raise ValueError(f"Pi archive exceeds the {MAX_MEMBERS} member limit")
                if item.issym() or item.islnk() or item.isdev() or item.isfifo():
                    raise ValueError(f"refusing link or special Pi member: {item.name!r}")
                name = item.name[:-1] if item.isdir() and item.name.endswith("/") else item.name
                path = _safe_member(name)
                if path != name or path in seen:
                    raise ValueError(f"unsafe or duplicate Pi member: {item.name!r}")
                if item.mode & 0o7022:
                    raise ValueError(f"refusing dangerous Pi member mode: {item.name!r}")
                seen.add(path)
                if item.isdir():
                    if not _dir_allowed(path, expected_arch):
                        raise ValueError(f"unexpected Pi directory: {item.name!r}")
                    if item.size != 0:
                        raise ValueError(f"Pi directory entry carries data: {item.name!r}")
                    _check_member_mode(item, name=name, executable=True)
                    dirs.add(path)
                    continue
                if not item.isfile():
                    raise ValueError(f"refusing non-regular Pi member: {item.name!r}")
                if not _file_allowed(path, expected_arch):
                    raise ValueError(f"unexpected member in Pi standard bundle: {item.name!r}")
                if path in ("pi/pi", native):
                    if not item.mode & 0o100:
                        raise ValueError(f"Pi member has a missing executable mode: {item.name!r}")
                    executable = True
                elif path.startswith("pi/examples/"):
                    executable = bool(item.mode & 0o100)
                else:
                    if item.mode & 0o100:
                        raise ValueError(f"Pi member has an unexpected executable mode: {item.name!r}")
                    executable = False
                _check_member_mode(item, name=name, executable=executable)
                limit = MAX_BINARY_BYTES if path == "pi/pi" else MAX_RESOURCE_BYTES
                if not 0 < item.size <= limit:
                    raise ValueError(f"Pi member has an invalid type or size: {item.name!r}")
                stream = tar.extractfile(item)
                if stream is None:
                    raise ValueError(f"cannot read Pi member {item.name!r}")
                if path in ("pi/pi", native):
                    _check_elf_header(stream, name=item.name, expected_arch=expected_arch)
                elif path == "pi/package.json":
                    if item.size > MAX_PACKAGE_JSON_BYTES:
                        raise ValueError("package.json exceeds the size limit")
                    package_json = stream.read(MAX_PACKAGE_JSON_BYTES + 1)
                    if len(package_json) != item.size:
                        raise ValueError("package.json size does not match its tar header")
                total += item.size
                if total > MAX_EXPANDED_BYTES:
                    raise ValueError(f"Pi bundle exceeds the {MAX_EXPANDED_BYTES} byte expanded limit")
                files.append(BundleFile(item.name, path.removeprefix(_ROOT + "/"), item.size, executable))
    except tarfile.TarError as exc:
        raise ValueError(f"invalid Pi gzip tar archive: {exc}") from None
    sources = {item.source for item in files}
    expected_dirs = {
        "/".join(source.split("/")[:index])
        for source in sources
        for index in range(1, source.count("/") + 1)
    }
    if dirs != expected_dirs:
        raise ValueError("Pi bundle directories differ from its member listing")
    if not _REQUIRED_FILES <= sources:
        raise ValueError("Pi standard bundle is missing a required file")
    if native not in sources:
        raise ValueError("Pi standard bundle is missing its native clipboard helper")
    for tree in _REQUIRED_TREES:
        if not any(source.startswith(tree + "/") for source in sources):
            raise ValueError(f"Pi standard bundle is missing the reviewed {tree} resources")
    if package_json is None:
        raise ValueError("Pi standard bundle is missing package.json")
    parse_package_manifest(package_json, version)
    return BundleInventory(
        bundle_type, expected_version, expected_arch,
        tuple(sorted(files, key=lambda item: item.relative)), package_json,
    )


def _copy_member(source, target: Path, size: int, name: str) -> None:
    written = 0
    with target.open("wb") as out:
        while chunk := source.read(1024 * 1024):
            written += len(chunk)
            if written > size or written > MAX_BINARY_BYTES:
                raise ValueError(f"Pi member {name!r} streamed more bytes than declared")
            out.write(chunk)
    if written != size:
        raise ValueError(f"Pi member {name!r} size does not match its tar header")


def extract_bundle(archive: Path, inventory: BundleInventory, destination: Path) -> None:
    """Revalidate the archive, then extract its complete reviewed resource tree."""
    if destination.exists():
        raise ValueError(f"Pi bundle destination already exists: {destination}")
    current = inventory_bundle(
        archive, expected_version=inventory.expected_version,
        expected_arch=inventory.expected_arch, bundle_type=inventory.bundle_type,
    )
    if current != inventory:
        raise ValueError("Pi archive changed after its bundle inventory was validated")
    destination.mkdir(parents=True)
    wanted = {item.source: item for item in inventory.files}
    with tarfile.open(archive, "r:gz") as tar:
        member_count = 0
        for item in tar:
            member_count += 1
            if member_count > MAX_MEMBERS:
                raise ValueError(f"Pi archive exceeds the {MAX_MEMBERS} member limit")
            selected = wanted.pop(item.name, None)
            if selected is None:
                continue
            stream = tar.extractfile(item)
            if stream is None:
                raise ValueError(f"cannot read Pi member {item.name!r}")
            target = destination / selected.relative
            target.parent.mkdir(parents=True, exist_ok=True)
            _copy_member(stream, target, selected.size, item.name)
            target.chmod(0o755 if selected.executable else 0o644)
    if wanted:
        raise ValueError(f"validated Pi members vanished during extraction: {sorted(wanted)}")


def stage_pi_package(
    *, pkg: Package, arch: str, manifest: Manifest, archive: Path,
    inventory: BundleInventory, stage_root: Path,
) -> Path:
    """Install the complete bundle, the quoted launcher, and the APT update helper."""
    target = pkg.targets[arch]
    if pkg.name != "pi" or target.bundle_type != BUNDLE_TYPE:
        raise ValueError("Pi bundle staging requires the reviewed pi-standard recipe")
    if (
        inventory.bundle_type != target.bundle_type
        or inventory.expected_version != f"v{pkg.version}"
        or inventory.expected_arch != arch
    ):
        raise ValueError("Pi inventory does not match the package's pinned bundle")
    stage = stage_root / f"{pkg.name}_{arch}"
    if stage.exists():
        shutil.rmtree(stage)
    usr = stage / "usr"
    extract_bundle(archive, inventory, usr / "lib" / "pi")
    launcher = usr / "bin" / "pi"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_bytes(_LAUNCHER)
    launcher.chmod(0o755)
    helper = usr / "lib" / "pi" / "apt-update"
    helper.write_bytes(_UPDATE_HELPER)
    helper.chmod(0o755)
    docs = usr / "share" / "doc" / pkg.name
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "README.source").write_text(
        pi_source_document(pkg.version), encoding="utf-8"
    )
    finalize_package_stage(
        stage=stage, pkg=pkg, arch=arch, manifest=manifest,
        license_bodies=read_vendor_licenses(pkg), keep_executables=True,
    )
    return stage
