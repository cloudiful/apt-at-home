"""Load and validate the pinned upstream manifest."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ARCHES = ("amd64", "arm64")
_VERSION = re.compile(r"^[0-9][A-Za-z0-9.+~:-]*$")
_URL = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/releases/download/[^ ]+$")


@dataclass(frozen=True)
class ArchTarget:
    url: str
    sha256: str
    archive_member: str
    binary: str


@dataclass(frozen=True)
class Package:
    name: str
    version: str
    homepage: str
    description: str
    license: str
    copyright: str
    license_url: str
    targets: dict[str, ArchTarget]


@dataclass(frozen=True)
class Manifest:
    maintainer: str
    section: str
    priority: str
    debian_revision: str
    packages: tuple[Package, ...]


def default_manifest_path() -> Path:
    return Path(__file__).resolve().parent.parent / "packaging" / "manifest.json"


def load_manifest(path: Path | None = None) -> Manifest:
    manifest_path = path or default_manifest_path()
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"manifest not found: {manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"manifest is not valid JSON: {exc}") from exc
    return parse_manifest(raw)


def parse_manifest(raw: object) -> Manifest:
    if not isinstance(raw, dict):
        raise ValueError("manifest root must be an object")
    for key in ("maintainer", "section", "priority", "debian_revision", "packages"):
        if key not in raw:
            raise ValueError(f"manifest missing required key: {key}")
    assert isinstance(raw, dict)
    maintainer = str(raw["maintainer"])
    section = str(raw["section"])
    priority = str(raw["priority"])
    debian_revision = str(raw["debian_revision"])
    if debian_revision != "-1":
        raise ValueError("debian_revision must be '-1'")
    packages_raw = raw["packages"]
    if not isinstance(packages_raw, list) or not packages_raw:
        raise ValueError("manifest packages must be a non-empty list")
    packages: list[Package] = []
    seen: set[tuple[str, str]] = set()
    for entry in packages_raw:
        pkg = parse_package(entry)
        for arch in _ARCHES:
            if arch not in pkg.targets:
                raise ValueError(f"package {pkg.name} missing arch target: {arch}")
            key = (pkg.name, arch)
            if key in seen:
                raise ValueError(f"duplicate package target: {pkg.name} {arch}")
            seen.add(key)
        packages.append(pkg)
    if len(packages) != 2:
        raise ValueError("manifest must pin exactly two packages")
    names = sorted(p.name for p in packages)
    if names != ["starship", "zellij"]:
        raise ValueError(f"manifest must pin starship and zellij, got: {names}")
    return Manifest(
        maintainer=maintainer,
        section=section,
        priority=priority,
        debian_revision=debian_revision,
        packages=tuple(packages),
    )


def parse_package(raw: object) -> Package:
    if not isinstance(raw, dict):
        raise ValueError("package entry must be an object")
    for key in ("name", "version", "homepage", "description", "license", "copyright", "license_url", "targets"):
        if key not in raw:
            raise ValueError(f"package entry missing required key: {key}")
    assert isinstance(raw, dict)
    name = str(raw["name"])
    version = str(raw["version"])
    if not _VERSION.match(version):
        raise ValueError(f"invalid version for {name}: {version}")
    targets_raw = raw["targets"]
    if not isinstance(targets_raw, dict):
        raise ValueError(f"targets must be an object for {name}")
    targets: dict[str, ArchTarget] = {}
    for arch, target_raw in targets_raw.items():
        if arch not in _ARCHES:
            raise ValueError(f"unsupported arch {arch} for {name}")
        if not isinstance(target_raw, dict):
            raise ValueError(f"target must be an object for {name} {arch}")
        url = str(target_raw.get("url", ""))
        sha256 = str(target_raw.get("sha256", "")).lower()
        member = str(target_raw.get("archive_member", ""))
        binary = str(target_raw.get("binary", ""))
        if not _URL.match(url):
            raise ValueError(f"invalid pinned URL for {name} {arch}: {url}")
        if not _SHA256.match(sha256):
            raise ValueError(f"invalid sha256 for {name} {arch}")
        if not member or member.startswith("/") or ".." in member:
            raise ValueError(f"invalid archive_member for {name} {arch}")
        if not binary or "/" in binary or binary.startswith("."):
            raise ValueError(f"invalid binary name for {name} {arch}")
        targets[arch] = ArchTarget(url=url, sha256=sha256, archive_member=member, binary=binary)
    return Package(
        name=name,
        version=version,
        homepage=str(raw["homepage"]),
        description=str(raw["description"]),
        license=str(raw["license"]),
        copyright=str(raw["copyright"]),
        license_url=str(raw["license_url"]),
        targets=targets,
    )


def debian_version(pkg: Package, manifest: Manifest) -> str:
    return f"{pkg.version}{manifest.debian_revision}"


def deb_filename(pkg: Package, arch: str, manifest: Manifest) -> str:
    return f"{pkg.name}_{debian_version(pkg, manifest)}_{arch}.deb"
