"""Load and validate the pinned upstream manifest.

Every target pins an official download URL with its digest and the reviewed
archive member inside it. Production is asset-only: an invented `https://` URL
for a compiled binary would be a false claim about where its bytes came from,
so the absence of an asset is expressed by the absence of that architecture's
target, never by a source block. Each reviewed recipe determines its exact
required targets through `catalog.arches_for`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from .archive_tree import top_root
from .catalog import (
    KNOWN_PACKAGES,
    RECIPES,
    additional_binaries_for,
    arches_for,
    archive_member,
    check_known_names,
    check_relations,
    extra_license_urls_for,
)

from .download import canonical_member

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ARCHES = ("amd64", "arm64")
_VERSION = re.compile(r"^[0-9][A-Za-z0-9.+~:-]*$")
_BINARY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_URL = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/releases/download/[^ ]+$")


@dataclass(frozen=True)
class ArchTarget:
    """One architecture's pinned official asset."""

    url: str
    sha256: str
    archive_member: str
    binary: str
    depends: str = ""
    install_tree: str = ""
    bundle_type: str = ""
    additional_binaries: Mapping[str, str] = MappingProxyType({})


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
    extra_license_urls: tuple[str, ...] = ()
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
        required = arches_for(pkg.name) if pkg.name in KNOWN_PACKAGES else tuple(sorted(pkg.targets))
        missing = sorted(set(required) - set(pkg.targets))
        if missing:
            raise ValueError(f"package {pkg.name} missing arch target: {', '.join(missing)}")
        extra = sorted(set(pkg.targets) - set(required))
        if extra:
            raise ValueError(
                f"package {pkg.name} declares unsupported arch target(s): {', '.join(extra)}"
            )
        for arch in pkg.targets:
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


def _parse_source(
    name: str, version: str, arch: str, target_raw: dict[str, object]
) -> tuple[str, str, str]:
    """Return a reviewed flat member, tree root, and closed bundle type."""
    has_member = "archive_member" in target_raw
    has_tree = "install_tree" in target_raw
    if has_member == has_tree:
        raise ValueError(f"{name} {arch} must declare exactly one of archive_member or install_tree")
    recipe = RECIPES.get(name)
    bundle_type = recipe.bundle_type if recipe is not None else ""
    if has_tree:
        if bundle_type:
            raise ValueError(f"{name} {arch} must use its reviewed {bundle_type} archive member")
        tree = str(target_raw["install_tree"])
        try:
            top_root(tree)
        except ValueError as exc:
            raise ValueError(f"invalid install_tree for {name} {arch}: {exc}") from None
        return "", tree, ""
    try:
        member = canonical_member(str(target_raw["archive_member"]))
    except ValueError as exc:
        raise ValueError(f"invalid archive_member for {name} {arch}: {exc}") from None
    if bundle_type and member != archive_member(name, version, arch):
        raise ValueError(
            f"{name} {arch} archive_member does not match the reviewed {bundle_type} recipe"
        )
    return member, "", bundle_type


def _parse_additional_binaries(
    name: str, version: str, arch: str, target_raw: dict[str, object],
    primary_member: str, primary_binary: str, install_tree: str,
) -> Mapping[str, str]:
    if install_tree:
        if "additional_binaries" in target_raw:
            raise ValueError(f"{name} {arch} tree targets cannot declare additional_binaries")
        return MappingProxyType({})
    raw = target_raw.get("additional_binaries", {})
    if not isinstance(raw, dict):
        raise ValueError(f"additional_binaries must be an object for {name} {arch}")
    parsed: dict[str, str] = {}
    members = {primary_member}
    binaries = {primary_binary}
    for source, binary in raw.items():
        if not isinstance(source, str):
            raise ValueError(f"invalid additional archive member for {name} {arch}")
        try:
            member = canonical_member(source)
        except ValueError as exc:
            raise ValueError(f"invalid additional archive member for {name} {arch}: {exc}") from None
        if member != source or member in members:
            raise ValueError(f"duplicate or non-canonical additional member for {name} {arch}: {source!r}")
        if not isinstance(binary, str) or not _BINARY.fullmatch(binary) or binary in binaries:
            raise ValueError(f"invalid or duplicate additional binary for {name} {arch}: {binary!r}")
        parsed[member] = binary
        members.add(member)
        binaries.add(binary)
    expected = additional_binaries_for(name, version, arch) if name in KNOWN_PACKAGES else {}
    if parsed != expected:
        raise ValueError(f"additional_binaries for {name} {arch} do not match the reviewed recipe")
    return MappingProxyType(parsed)


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
    extra_raw = raw.get("extra_license_urls", [])
    if not isinstance(extra_raw, list) or any(not isinstance(url, str) for url in extra_raw):
        raise ValueError(f"extra_license_urls must be a string list for {name}")
    extra_urls = tuple(extra_raw)
    expected_extra_urls = extra_license_urls_for(name, version) if name in KNOWN_PACKAGES else ()
    if len(set(extra_urls)) != len(extra_urls) or extra_urls != expected_extra_urls:
        raise ValueError(f"extra_license_urls for {name} do not match the reviewed recipe")
    targets_raw = raw["targets"]
    if not isinstance(targets_raw, dict):
        raise ValueError(f"targets must be an object for {name}")
    targets: dict[str, ArchTarget] = {}
    allowed_target_fields = frozenset({
        "url", "sha256", "archive_member", "install_tree", "binary",
        "depends", "additional_binaries",
    })
    for arch, target_raw in targets_raw.items():
        if arch not in _ARCHES:
            raise ValueError(f"unsupported arch {arch} for {name}")
        if not isinstance(target_raw, dict):
            raise ValueError(f"target must be an object for {name} {arch}")
        unknown = sorted(set(target_raw) - allowed_target_fields)
        if unknown:
            raise ValueError(
                f"unknown field(s) for {name} {arch}: {', '.join(unknown)}"
            )
        url = str(target_raw.get("url", ""))
        sha256 = str(target_raw.get("sha256", "")).lower()
        binary = str(target_raw.get("binary", ""))
        member, install_tree, bundle_type = _parse_source(name, version, arch, target_raw)
        if not _URL.match(url):
            raise ValueError(f"invalid pinned URL for {name} {arch}: {url}")
        if not _SHA256.match(sha256):
            raise ValueError(f"invalid sha256 for {name} {arch}")
        additional = _parse_additional_binaries(
            name, version, arch, target_raw, member, binary, install_tree
        )
        if not _BINARY.fullmatch(binary):
            raise ValueError(f"invalid binary name for {name} {arch}")
        depends = check_relations(
            f"depends for {name} {arch}", str(target_raw.get("depends", "") or "")
        )
        targets[arch] = ArchTarget(
            url=url, sha256=sha256, archive_member=member, binary=binary,
            depends=depends, install_tree=install_tree, bundle_type=bundle_type,
            additional_binaries=additional,
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
        extra_license_urls=extra_urls,
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
        for arch in pkg.targets
    }


def expected_debs_for_arch(manifest: Manifest, arch: str) -> set[str]:
    """The pool filenames one architecture's index must list exactly."""
    if arch not in _ARCHES:
        raise ValueError(f"unsupported architecture: {arch!r}")
    return {
        deb_filename(pkg, arch, manifest)
        for pkg in manifest.packages
        if arch in pkg.targets
    }
