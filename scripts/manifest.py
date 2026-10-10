"""Load and validate the pinned upstream manifest."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .archive_tree import top_root
from .catalog import check_known_names, check_relations
from .download import canonical_member

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ARCHES = ("amd64", "arm64")
_VERSION = re.compile(r"^[0-9][A-Za-z0-9.+~:-]*$")
_URL = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/releases/download/[^ ]+$")


@dataclass(frozen=True)
class ArchTarget:
    """One architecture's pinned asset: a flat member or a tree root, never both."""

    url: str
    sha256: str
    archive_member: str
    binary: str
    depends: str = ""
    install_tree: str = ""


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
    conflicts: str = ""
    replaces: str = ""


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
    check_known_names([pkg.name for pkg in packages])
    return Manifest(
        maintainer=maintainer,
        section=section,
        priority=priority,
        debian_revision=debian_revision,
        packages=tuple(packages),
    )


def _parse_source(name: str, arch: str, target_raw: dict[str, object]) -> tuple[str, str]:
    """Return (archive_member, install_tree) with exactly one of the two set."""
    has_member = "archive_member" in target_raw
    has_tree = "install_tree" in target_raw
    if has_member == has_tree:
        raise ValueError(f"{name} {arch} must declare exactly one of archive_member or install_tree")
    if has_tree:
        tree = str(target_raw["install_tree"])
        try:
            top_root(tree)
        except ValueError as exc:
            raise ValueError(f"invalid install_tree for {name} {arch}: {exc}") from None
        return "", tree
    try:
        member = canonical_member(str(target_raw["archive_member"]))
    except ValueError as exc:
        raise ValueError(f"invalid archive_member for {name} {arch}: {exc}") from None
    return member, ""


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
        binary = str(target_raw.get("binary", ""))
        member, install_tree = _parse_source(name, arch, target_raw)
        depends = check_relations(
            f"depends for {name} {arch}", str(target_raw.get("depends", "") or "")
        )
        if not _URL.match(url):
            raise ValueError(f"invalid pinned URL for {name} {arch}: {url}")
        if not _SHA256.match(sha256):
            raise ValueError(f"invalid sha256 for {name} {arch}")
        if not binary or "/" in binary or binary.startswith("."):
            raise ValueError(f"invalid binary name for {name} {arch}")
        targets[arch] = ArchTarget(
            url=url, sha256=sha256, archive_member=member, binary=binary,
            depends=depends, install_tree=install_tree,
        )
    conflicts = check_relations(f"conflicts for {name}", str(raw.get("conflicts", "") or ""))
    replaces = check_relations(f"replaces for {name}", str(raw.get("replaces", "") or ""))
    return Package(
        name=name,
        version=version,
        homepage=str(raw["homepage"]),
        description=str(raw["description"]),
        license=str(raw["license"]),
        copyright=str(raw["copyright"]),
        license_url=str(raw["license_url"]),
        targets=targets,
        conflicts=conflicts,
        replaces=replaces,
    )


def debian_version(pkg: Package, manifest: Manifest) -> str:
    return f"{pkg.version}{manifest.debian_revision}"


def deb_filename(pkg: Package, arch: str, manifest: Manifest) -> str:
    return f"{pkg.name}_{debian_version(pkg, manifest)}_{arch}.deb"


def expected_debs(manifest: Manifest) -> set[str]:
    """Every pool filename a release of this manifest must carry."""
    return {
        deb_filename(pkg, arch, manifest)
        for pkg in manifest.packages
        for arch in _ARCHES
    }


def expected_debs_for_arch(manifest: Manifest, arch: str) -> set[str]:
    """The pool filenames one architecture's index must list exactly."""
    if arch not in _ARCHES:
        raise ValueError(f"unsupported architecture: {arch!r}")
    return {deb_filename(pkg, arch, manifest) for pkg in manifest.packages}
