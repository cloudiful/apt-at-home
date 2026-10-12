"""Validate and stage Kandev's reviewed standard server bundle."""

from __future__ import annotations

import json
import re
import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path

from .deb import finalize_package_stage
from .download import canonical_member
from .manifest import Manifest, Package
from .package_licenses import kandev_source_document, read_vendor_licenses

BUNDLE_TYPE = "kandev-standard"
MAX_ARCHIVE_BYTES = 160 * 1024 * 1024
MAX_EXPANDED_BYTES = 600 * 1024 * 1024
MAX_MEMBERS = 64
MAX_MANIFEST_BYTES = 64 * 1024
_ELF_HEADER_SIZE = 64
_ELF_MACHINES = {"amd64": 62, "arm64": 183}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_DIRS = frozenset({"kandev", "kandev/bin"})
_FILES = {
    "kandev/bin/kandev": "bin/kandev",
    "kandev/bin/agentctl": "bin/agentctl",
    "kandev/remote-helpers.json": "remote-helpers.json",
}
_HELPERS = {
    "linux/amd64": "agentctl-linux-amd64.gz",
    "linux/arm64": "agentctl-linux-arm64.gz",
    "darwin/amd64": "agentctl-darwin-amd64.gz",
    "darwin/arm64": "agentctl-darwin-arm64.gz",
}
_WRAPPER = b'#!/bin/sh\nexec /usr/lib/kandev/bin/kandev "$@"\n'


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
    manifest_bytes: bytes
    commit: str


def _check_elf_header(stream, *, name: str, expected_arch: str) -> None:
    header = stream.read(_ELF_HEADER_SIZE)
    if len(header) != _ELF_HEADER_SIZE or header[:4] != b"\x7fELF":
        raise ValueError(f"Kandev member {name!r} is not a complete ELF64 header")
    if header[4] != 2:
        raise ValueError(f"Kandev member {name!r} is not ELF64")
    if header[5] != 1:
        raise ValueError(f"Kandev member {name!r} is not little-endian ELF")
    if header[6] != 1 or int.from_bytes(header[52:54], "little") != _ELF_HEADER_SIZE:
        raise ValueError(f"Kandev member {name!r} has a malformed ELF64 header")
    machine = int.from_bytes(header[18:20], "little")
    if machine != _ELF_MACHINES[expected_arch]:
        raise ValueError(
            f"Kandev member {name!r} targets e_machine {machine}, expected "
            f"{expected_arch} ({_ELF_MACHINES[expected_arch]})"
        )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"remote-helpers.json repeats key {key!r}")
        result[key] = value
    return result


def parse_remote_helper_manifest(data: bytes, expected_version: str) -> str:
    """Validate exact standard-helper records and return the bundle build commit."""
    if len(data) > MAX_MANIFEST_BYTES:
        raise ValueError("remote-helpers.json exceeds the size limit")
    try:
        raw = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"remote-helpers.json is invalid: {exc}") from None
    keys = {"schema_version", "version", "commit", "variant", "helpers"}
    if not isinstance(raw, dict) or set(raw) != keys:
        raise ValueError("remote-helpers.json has an unexpected schema")
    commit = raw["commit"]
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise ValueError("remote-helpers.json has an unsupported schema_version")
    if raw["version"] != expected_version or raw["variant"] != "standard":
        raise ValueError("remote-helpers.json does not match the pinned standard release")
    if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
        raise ValueError("remote-helpers.json commit must be a full lowercase Git SHA")
    entries = raw["helpers"]
    if not isinstance(entries, list) or len(entries) != len(_HELPERS):
        raise ValueError("remote-helpers.json must list exactly the reviewed helper platforms")
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"platform", "asset", "sha256", "size_bytes"}:
            raise ValueError("remote-helpers.json contains an unexpected helper record")
        platform, asset, digest, size = (
            entry["platform"], entry["asset"], entry["sha256"], entry["size_bytes"]
        )
        if not isinstance(platform, str) or platform not in _HELPERS or platform in seen:
            raise ValueError(f"remote-helpers.json has an unsupported or duplicate platform: {platform!r}")
        if asset != _HELPERS[platform] or not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise ValueError(f"remote-helpers.json has invalid asset metadata for {platform}")
        if type(size) is not int or not 0 < size <= MAX_ARCHIVE_BYTES:
            raise ValueError(f"remote-helpers.json has an invalid size_bytes for {platform}")
        seen.add(platform)
    if seen != set(_HELPERS):
        raise ValueError("remote-helpers.json is missing a reviewed helper platform")
    return commit


def inventory_bundle(
    archive: Path, *, expected_version: str, expected_arch: str,
    bundle_type: str = BUNDLE_TYPE,
) -> BundleInventory:
    """Validate archive members, ELF identity, and helper metadata before staging."""
    if bundle_type != BUNDLE_TYPE:
        raise ValueError(f"unsupported Kandev bundle type: {bundle_type!r}")
    if expected_arch not in _ELF_MACHINES:
        raise ValueError(f"unsupported Kandev architecture: {expected_arch!r}")
    if not _VERSION.fullmatch(expected_version.removeprefix("v")):
        raise ValueError(f"invalid Kandev release version: {expected_version!r}")
    if archive.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError(f"Kandev archive exceeds the {MAX_ARCHIVE_BYTES} byte compressed limit")
    try:
        with tarfile.open(archive, "r:gz") as tar:
            seen: set[str] = set()
            dirs: set[str] = set()
            files: list[BundleFile] = []
            total = 0
            member_count = 0
            manifest_bytes: bytes | None = None
            for item in tar:
                member_count += 1
                if member_count > MAX_MEMBERS:
                    raise ValueError(f"Kandev archive exceeds the {MAX_MEMBERS} member limit")
                if item.issym() or item.islnk() or item.isdev() or item.isfifo():
                    raise ValueError(f"refusing link or special Kandev member: {item.name!r}")
                if item.mode & 0o7022:
                    raise ValueError(f"refusing dangerous Kandev member mode: {item.name!r}")
                name = item.name[:-1] if item.isdir() and item.name.endswith("/") else item.name
                path = canonical_member(name)
                if path != name or path in seen:
                    raise ValueError(f"unsafe or duplicate Kandev member: {item.name!r}")
                seen.add(path)
                if path in _DIRS:
                    if not item.isdir() or item.size != 0:
                        raise ValueError(f"Kandev bundle path must be a directory: {item.name!r}")
                    dirs.add(path)
                    continue
                relative = _FILES.get(path)
                if relative is None:
                    raise ValueError(f"unexpected member in Kandev standard bundle: {item.name!r}")
                if not item.isfile() or not 0 < item.size <= MAX_EXPANDED_BYTES:
                    raise ValueError(f"Kandev member has an invalid type or size: {item.name!r}")
                executable = relative.startswith("bin/")
                if bool(item.mode & 0o100) != executable:
                    raise ValueError(f"Kandev member has an unexpected executable mode: {item.name!r}")
                if executable:
                    if item.size < _ELF_HEADER_SIZE:
                        raise ValueError(f"Kandev member {item.name!r} has a truncated ELF header")
                    stream = tar.extractfile(item)
                    if stream is None:
                        raise ValueError(f"cannot read Kandev ELF member {item.name!r}")
                    _check_elf_header(stream, name=item.name, expected_arch=expected_arch)
                total += item.size
                if total > MAX_EXPANDED_BYTES:
                    raise ValueError(f"Kandev bundle exceeds the {MAX_EXPANDED_BYTES} byte expanded limit")
                files.append(BundleFile(item.name, relative, item.size, executable))
                if relative == "remote-helpers.json":
                    if item.size > MAX_MANIFEST_BYTES:
                        raise ValueError("remote-helpers.json exceeds the size limit")
                    stream = tar.extractfile(item)
                    if stream is None:
                        raise ValueError("cannot read remote-helpers.json")
                    manifest_bytes = stream.read(MAX_MANIFEST_BYTES + 1)
                    if len(manifest_bytes) != item.size:
                        raise ValueError("remote-helpers.json size does not match its tar header")
    except tarfile.TarError as exc:
        raise ValueError(f"invalid Kandev gzip tar archive: {exc}") from None
    if dirs != _DIRS:
        raise ValueError(f"Kandev bundle directories differ from the reviewed layout: {sorted(dirs)}")
    if {item.source for item in files} != set(_FILES) or manifest_bytes is None:
        raise ValueError("Kandev standard bundle is missing a required file or directory")
    commit = parse_remote_helper_manifest(manifest_bytes, expected_version)
    return BundleInventory(
        bundle_type, expected_version, expected_arch,
        tuple(sorted(files, key=lambda item: item.relative)), manifest_bytes, commit,
    )


def _copy_member(source, target: Path, size: int, name: str) -> None:
    written = 0
    with target.open("wb") as out:
        while chunk := source.read(1024 * 1024):
            written += len(chunk)
            if written > size or written > MAX_EXPANDED_BYTES:
                raise ValueError(f"Kandev member {name!r} streamed more bytes than declared")
            out.write(chunk)
    if written != size:
        raise ValueError(f"Kandev member {name!r} size does not match its tar header")


def extract_bundle(archive: Path, inventory: BundleInventory, destination: Path) -> None:
    """Revalidate the archive, then extract only its three reviewed regular files."""
    if destination.exists():
        raise ValueError(f"Kandev bundle destination already exists: {destination}")
    current = inventory_bundle(
        archive, expected_version=inventory.expected_version,
        expected_arch=inventory.expected_arch, bundle_type=inventory.bundle_type,
    )
    if current != inventory:
        raise ValueError("Kandev archive changed after its bundle inventory was validated")
    destination.mkdir(parents=True)
    wanted = {item.source: item for item in inventory.files if item.relative != "remote-helpers.json"}
    with tarfile.open(archive, "r:gz") as tar:
        member_count = 0
        for item in tar:
            member_count += 1
            if member_count > MAX_MEMBERS:
                raise ValueError(f"Kandev archive exceeds the {MAX_MEMBERS} member limit")
            selected = wanted.pop(item.name, None)
            if selected is None:
                continue
            stream = tar.extractfile(item)
            if stream is None:
                raise ValueError(f"cannot read Kandev member {item.name!r}")
            target = destination / selected.relative
            target.parent.mkdir(parents=True, exist_ok=True)
            _copy_member(stream, target, selected.size, item.name)
            target.chmod(0o755)
    if wanted:
        raise ValueError(f"validated Kandev members vanished during extraction: {sorted(wanted)}")
    manifest = destination / "remote-helpers.json"
    manifest.write_bytes(inventory.manifest_bytes)
    manifest.chmod(0o644)


def stage_kandev_package(
    *, pkg: Package, arch: str, manifest: Manifest, archive: Path,
    inventory: BundleInventory, stage_root: Path,
) -> Path:
    """Install the standard server bundle, quoted launcher wrapper, and notices."""
    target = pkg.targets[arch]
    if pkg.name != "kandev" or target.bundle_type != BUNDLE_TYPE:
        raise ValueError("Kandev bundle staging requires the reviewed kandev-standard recipe")
    if (
        inventory.bundle_type != target.bundle_type
        or inventory.expected_version != f"v{pkg.version}"
        or inventory.expected_arch != arch
    ):
        raise ValueError("Kandev inventory does not match the package's pinned bundle")
    stage = stage_root / f"{pkg.name}_{arch}"
    if stage.exists():
        shutil.rmtree(stage)
    usr = stage / "usr"
    extract_bundle(archive, inventory, usr / "lib" / "kandev")
    launcher = usr / "bin" / "kandev"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_bytes(_WRAPPER)
    launcher.chmod(0o755)
    docs = usr / "share" / "doc" / pkg.name
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "README.source").write_text(
        kandev_source_document(pkg.version, inventory.commit), encoding="utf-8"
    )
    finalize_package_stage(
        stage=stage, pkg=pkg, arch=arch, manifest=manifest,
        license_bodies=read_vendor_licenses(pkg), keep_executables=True,
    )
    return stage
