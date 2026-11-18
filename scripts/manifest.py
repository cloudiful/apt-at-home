"""Load and validate the pinned upstream manifest.

Every target pins an official download URL from the package's reviewed source
with its digest and the reviewed archive member inside it. Production is
asset-only: an invented `https://` URL for a compiled binary would be a false
claim about where its bytes came from, so the absence of an asset is expressed
by the absence of that architecture's target, never by a source block. Each
reviewed recipe determines its exact required targets through
`catalog.arches_for`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType

from .archive_tree import top_root
from .catalog import (
    KNOWN_PACKAGES,
    RECIPES,
    additional_binaries_for,
    arches_for,
    archive_member,
    asset_name,
    binary_dir_for,
    binary_name,
    canonical_source_for,
    check_known_names,
    check_relations,
    debian_revision_for,
    extra_license_urls_for,
    legacy_asset_name,
    license_path_for,
    npm_platform_package_for,
    official_deb_for,
    release_asset_name,
    source_for,
    tag_for,
)
from .upstream_assets import (
    license_url,
    npm_tarball_name,
    npm_tarball_url,
    release_download_url,
    version_key,
)

from .download import canonical_member

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ARCHES = ("amd64", "arm64")
_VERSION = re.compile(r"^[0-9][A-Za-z0-9.+~:-]*$")
_BINARY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
# The canonical upstream publication instant: UTC RFC3339 with second precision.
_PUBLISHED_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
# The reviewed release hosts other than GitLab, in their fixed download form.
# Credentials, a port, a query, and a fragment are all outside the shape, and
# the canonical check below binds every URL to its own recipe, release, and
# asset.
_URL = re.compile(
    r"^https://(?:github\.com|codeberg\.org)/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
    r"/releases/download/[^/?#\s]+/[^/?#\s]+$"
)
# GitLab's release download form, including its `/-/releases/.../downloads/`
# path. A foreign host, a credential, a port, a query, and a fragment are all
# outside the shape, and the canonical check binds the project, tag, and asset.
_GITLAB_URL = re.compile(
    r"^https://gitlab\.com/(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+"
    r"/-/releases/[^/?#\s]+/downloads/[^/?#\s]+$"
)
# The npm registry archive form. The host, the package path, and the archive
# name are the only shapes this accepts, and the canonical check below binds
# each of them to one reviewed package, version, and asset.
_NPM_URL = re.compile(
    r"^https://registry\.npmjs\.org/(?:@[A-Za-z0-9._-]+/)?[A-Za-z0-9._-]+"
    r"/-/[^/?#\s]+\.tgz$"
)

# The one reviewed architecture gap in published history: cargo-tauri publishes
# its official arm64 asset only from 2.12.3, so an earlier or equal published
# release of that package declares its amd64 target alone. The allowance is a
# property of the recorded baseline, never a switch a document may request, and
# it names the exact target set an old baseline carries.
_HISTORICAL_ARCH_GAPS: Mapping[str, tuple[str, frozenset[str]]] = MappingProxyType(
    {"cargo-tauri": ("2.12.3", frozenset({"amd64"}))}
)


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
    official_deb: bool = False


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
    upstream_published_at: str = ""
    extra_license_urls: tuple[str, ...] = ()
    conflicts: str = ""
    replaces: str = ""
    debian_revision: str = ""


@dataclass(frozen=True)
class Manifest:
    maintainer: str
    section: str
    priority: str
    debian_revision: str
    packages: tuple[Package, ...]


def default_manifest_path() -> Path:
    return Path(__file__).resolve().parent.parent / "packaging" / "manifest.json"


def normalize_published_at(value: str) -> str:
    """Return the canonical UTC form of an upstream publication instant.

    The canonical form is RFC3339 UTC with second precision. An offset-aware
    ISO8601 instant is converted to UTC, so a source that publishes a local
    time yields the same instant as every other source. A naive or unparseable
    value is refused: an instant without a timezone is not a publication time,
    and guessing one would publish a wrong date.
    """
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(
            f"upstream release date is not an ISO8601 instant: {value!r}"
        ) from None
    if moment.tzinfo is None:
        raise ValueError(f"upstream release date carries no timezone: {value!r}")
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def check_published_at(value: object, *, name: str) -> str:
    """Return the canonical publication instant a document declares.

    A declared value must already be canonical: a nonempty UTC timestamp with
    a real calendar date and time. Anything else is refused, so a malformed or
    impossible date can never reach the published page.
    """
    if not isinstance(value, str) or not value:
        raise ValueError(
            f"upstream_published_at must be a nonempty string for {name}"
        )
    if not _PUBLISHED_AT.match(value):
        raise ValueError(
            f"upstream_published_at for {name} is not a canonical UTC timestamp: {value}"
        )
    try:
        datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(
            f"upstream_published_at for {name} is not a real date and time: {value}"
        ) from None
    return value


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
    """Validate a current manifest; every reviewed target must be declared.

    Only a package entry that exactly matches its recorded pre-official payload
    form is tolerated alongside the current reviewed form. `woodpecker` -1
    entries are such a signed-baseline form; any current entry still has to use
    the reviewed official package and revision.
    """
    return _parse_manifest(
        raw, allow_historical_gap=False, allow_legacy_payload=True
    )


def parse_baseline_manifest(raw: object) -> Manifest:
    """Validate a published baseline, which may predate a reviewed arch asset.

    Only the recorded historical gap is tolerated: a package listed in
    `_HISTORICAL_ARCH_GAPS` may omit one reviewed target when its version is at
    or below the recorded ceiling and its declared targets are exactly the set
    that baseline shipped. A recorded pre-official payload form at or below its
    reviewed `legacy_through` is also accepted unchanged. Every other missing,
    extra, or malformed target is refused, so a generic missing-architecture
    escape does not exist here.
    """
    return _parse_manifest(
        raw, allow_historical_gap=True, allow_legacy_payload=True
    )


def _historical_arch_gap(
    name: str, version: str, required: tuple[str, ...], present: set[str]
) -> bool:
    """Whether this package's published baseline may lack a reviewed target."""
    gap = _HISTORICAL_ARCH_GAPS.get(name)
    if gap is None:
        return False
    ceiling, baseline_targets = gap
    if present != set(baseline_targets):
        return False
    if set(required) <= present:
        return False
    try:
        return version_key(version) <= version_key(ceiling)
    except ValueError:
        return False


def _is_higher_revision(declared: str, reviewed: str) -> bool:
    """Whether a baseline entry declares a revision ahead of the reviewed one.

    A signed baseline can only ever carry a revision at or ahead of the
    reviewed one. An ahead revision stays readable here so the planner can
    refuse the downgrade explicitly instead of reporting a malformed manifest.
    """
    if not declared.startswith("-") or not declared[1:].isdigit():
        return False
    if not reviewed.startswith("-") or not reviewed[1:].isdigit():
        return False
    return int(declared[1:]) > int(reviewed[1:])


def _is_reviewed_legacy_target(
    name: str,
    version: str,
    arch: str,
    *,
    url: str,
    member: str,
    binary: str,
    depends: str,
    install_tree: str,
    bundle_type: str,
    additional: Mapping[str, str],
) -> bool:
    """Whether one target is exactly a recorded pre-official payload form."""
    if name not in KNOWN_PACKAGES or install_tree or bundle_type or additional:
        return False
    expected_member = archive_member(name, version, arch)
    if member != expected_member or binary != binary_name(name):
        return False
    if depends:
        return False
    expected_asset = legacy_asset_name(name, version, arch)
    if not expected_asset:
        return False
    source, repo = canonical_source_for(name, version)
    expected_url = release_download_url(
        repo, tag_for(name, version), expected_asset, source=source
    )
    return url == expected_url


def is_legacy_target(pkg: Package, arch: str, manifest: Manifest) -> bool:
    """Whether one target is exactly the recorded pre-official payload form."""
    target = pkg.targets[arch]
    if effective_debian_revision(pkg, manifest) != "-1":
        return False
    return not target.official_deb and _is_reviewed_legacy_target(
        pkg.name, pkg.version, arch,
        url=target.url,
        member=target.archive_member,
        binary=target.binary,
        depends=target.depends,
        install_tree=target.install_tree,
        bundle_type=target.bundle_type,
        additional=target.additional_binaries,
    )


def is_legacy_package(pkg: Package, manifest: Manifest) -> bool:
    """Whether every target of a package is its recorded legacy payload form."""
    return bool(pkg.targets) and all(
        is_legacy_target(pkg, arch, manifest) for arch in pkg.targets
    )


def installed_binary_dir(pkg: Package, manifest: Manifest) -> str:
    """Return the package-relative command directory for this revision.

    A retained trusted baseline package installed flat `/usr/bin` commands; only
    the reviewed current payload installs the recipe's directory.
    """
    if is_legacy_package(pkg, manifest):
        return "usr/bin"
    return binary_dir_for(pkg.name)


def _has_legacy_payload(pkg: Package) -> bool:
    """Whether every target of a package exactly matches a legacy form."""
    if pkg.name not in KNOWN_PACKAGES or not pkg.targets:
        return False
    if set(pkg.targets) != set(arches_for(pkg.name)):
        return False
    for arch, target in pkg.targets.items():
        if target.official_deb:
            return False
        if not _is_reviewed_legacy_target(
            pkg.name, pkg.version, arch,
            url=target.url,
            member=target.archive_member,
            binary=target.binary,
            depends=target.depends,
            install_tree=target.install_tree,
            bundle_type=target.bundle_type,
            additional=target.additional_binaries,
        ):
            return False
    return True


def _is_reviewed_legacy_package(
    pkg: Package, manifest_revision: str
) -> bool:
    """Whether a package is a complete recorded legacy -1 entry."""
    if pkg.name not in KNOWN_PACKAGES:
        return False
    if pkg.debian_revision not in ("", "-1"):
        return False
    if (pkg.debian_revision or manifest_revision) != "-1":
        return False
    return _has_legacy_payload(pkg)


def _parse_manifest(
    raw: object, *, allow_historical_gap: bool, allow_legacy_payload: bool
) -> Manifest:
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
        pkg = parse_package(entry, allow_legacy_payload=allow_legacy_payload)
        if pkg.name in KNOWN_PACKAGES:
            expected_revision = debian_revision_for(pkg.name)
            legacy_payload = _has_legacy_payload(pkg)
            legacy_package = (
                allow_legacy_payload
                and _is_reviewed_legacy_package(pkg, debian_revision)
            )
            if pkg.debian_revision != expected_revision and not legacy_package:
                if not (
                    allow_historical_gap
                    and not legacy_payload
                    and _is_higher_revision(pkg.debian_revision, expected_revision)
                ):
                    raise ValueError(
                        f"package {pkg.name} declares revision "
                        f"{pkg.debian_revision or 'the manifest revision'!r}, "
                        f"the reviewed recipe requires {expected_revision or 'none'!r}"
                    )
            if pkg.debian_revision == expected_revision and legacy_payload:
                raise ValueError(
                    f"package {pkg.name} declares the reviewed revision "
                    f"{expected_revision!r} but keeps a pre-official payload; "
                    "the current revision must use its reviewed official package"
                )
        required = arches_for(pkg.name) if pkg.name in KNOWN_PACKAGES else tuple(sorted(pkg.targets))
        missing = sorted(set(required) - set(pkg.targets))
        if missing and not (
            allow_historical_gap
            and _historical_arch_gap(pkg.name, pkg.version, required, set(pkg.targets))
        ):
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


def _check_npm_target_url(name: str, version: str, arch: str, url: str) -> str:
    """Require the pinned URL to be the reviewed npm archive.

    The URL is bound to the reviewed platform package and to the exact
    version, so a generic registry URL, another package, or another version
    can never pass. The recipe's own asset name must name that same archive,
    so a manifest and a recipe can never disagree about which file is taken.
    """
    if not _NPM_URL.match(url):
        raise ValueError(f"invalid pinned URL for {name} {arch}: {url}")
    platform = npm_platform_package_for(name, arch)
    expected = npm_tarball_url(platform, version)
    if url != expected:
        raise ValueError(
            f"pinned URL for {name} {arch} is not the reviewed official asset: {url}"
        )
    reviewed = asset_name(name, version, arch)
    if reviewed != npm_tarball_name(platform, version):
        raise ValueError(
            f"the reviewed asset name for {name} {arch} is not the npm archive "
            f"name: {reviewed}"
        )
    return url


def _check_target_url(
    name: str, version: str, arch: str, url: str, *, official_deb: bool = False
) -> str:
    """Require the pinned URL to be the reviewed package's official asset.

    The shape check refuses every other host form, a credential, a port, a
    query, and a fragment. The canonical check then binds the URL to the
    source, repository, release tag, and asset that published this exact
    version, so a swapped host, repository, path, or tag can never pass. A
    repacked architecture names upstream's official Debian package; every
    other target keeps the recipe's archive asset.
    """
    if name not in KNOWN_PACKAGES:
        if not _URL.match(url):
            raise ValueError(f"invalid pinned URL for {name} {arch}: {url}")
        return url
    source, repo = canonical_source_for(name, version)
    if source == "npm":
        return _check_npm_target_url(name, version, arch, url)
    if source == "gitlab":
        if not _GITLAB_URL.match(url):
            raise ValueError(f"invalid pinned URL for {name} {arch}: {url}")
    elif not _URL.match(url):
        raise ValueError(f"invalid pinned URL for {name} {arch}: {url}")
    expected = release_download_url(
        repo,
        tag_for(name, version),
        (
            release_asset_name(name, version, arch)
            if official_deb
            else asset_name(name, version, arch)
        ),
        source=source,
    )
    if url != expected:
        raise ValueError(
            f"pinned URL for {name} {arch} is not the reviewed official asset: {url}"
        )
    return url


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
        if name in KNOWN_PACKAGES and source_for(name) == "gitlab":
            raise ValueError(
                f"{name} {arch} is a reviewed flat GitLab package and must use "
                "its archive_member"
            )
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
    if (
        name in KNOWN_PACKAGES
        and source_for(name) == "npm"
        and member != archive_member(name, version, arch)
    ):
        raise ValueError(
            f"{name} {arch} archive_member does not match the reviewed npm recipe"
        )
    if (
        name in KNOWN_PACKAGES
        and source_for(name) == "gitlab"
        and member != archive_member(name, version, arch)
    ):
        raise ValueError(
            f"{name} {arch} archive_member does not match the reviewed GitLab recipe"
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


def _check_license_url(name: str, version: str, url: str) -> str:
    """Require a GitLab package's license URL to be its reviewed raw file.

    The URL is bound to the reviewed project, tag, and license path, so a
    swapped host, project, or tag can never claim to be this release's license.
    """
    if name not in KNOWN_PACKAGES or source_for(name) != "gitlab":
        return url
    _, repo = canonical_source_for(name, version)
    expected = license_url(
        repo,
        tag_for(name, version),
        license_path_for(name),
        source="gitlab",
    )
    if url != expected:
        raise ValueError(
            f"license_url for {name} is not the reviewed official license: {url}"
        )
    return url


def parse_package(raw: object, *, allow_legacy_payload: bool = False) -> Package:
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
    debian_revision = str(raw.get("debian_revision", "") or "")
    if debian_revision and not re.fullmatch(r"-[0-9]+", debian_revision):
        raise ValueError(f"invalid debian_revision for {name}: {debian_revision!r}")
    license_source_url = _check_license_url(name, version, str(raw["license_url"]))
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
        "depends", "additional_binaries", "official_deb",
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
        raw_official = target_raw.get("official_deb", False)
        if not isinstance(raw_official, bool):
            raise ValueError(f"official_deb must be a boolean for {name} {arch}")
        expected_official = official_deb_for(name, arch) if name in KNOWN_PACKAGES else False
        legacy_target = (
            allow_legacy_payload and expected_official and not raw_official
        )
        if raw_official != expected_official and not legacy_target:
            raise ValueError(
                f"official_deb for {name} {arch} is {raw_official}, "
                f"the reviewed recipe requires {expected_official}"
            )
        url = str(target_raw.get("url", ""))
        sha256 = str(target_raw.get("sha256", "")).lower()
        binary = str(target_raw.get("binary", ""))
        member, install_tree, bundle_type = _parse_source(name, version, arch, target_raw)
        _check_target_url(
            name, version, arch, url, official_deb=bool(raw_official)
        )
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
        if legacy_target and not _is_reviewed_legacy_target(
            name, version, arch,
            url=url,
            member=member,
            binary=binary,
            depends=depends,
            install_tree=install_tree,
            bundle_type=bundle_type,
            additional=additional,
        ):
            raise ValueError(
                f"{name} {arch} is not the recorded pre-official release form; "
                "a signed baseline may retain it but a current manifest must "
                "use the reviewed official package"
            )
        targets[arch] = ArchTarget(
            url=url, sha256=sha256, archive_member=member, binary=binary,
            depends=depends, install_tree=install_tree, bundle_type=bundle_type,
            additional_binaries=additional, official_deb=bool(raw_official),
        )
    conflicts = check_relations(f"conflicts for {name}", str(raw.get("conflicts", "") or ""))
    replaces = check_relations(f"replaces for {name}", str(raw.get("replaces", "") or ""))
    published_at = ""
    if "upstream_published_at" in raw:
        published_at = check_published_at(raw["upstream_published_at"], name=name)
    return Package(
        name=name,
        version=version,
        homepage=str(raw["homepage"]),
        description=str(raw["description"]),
        license=str(raw["license"]),
        copyright=str(raw["copyright"]),
        license_url=license_source_url,
        targets=targets,
        upstream_published_at=published_at,
        extra_license_urls=extra_urls,
        conflicts=conflicts,
        replaces=replaces,
        debian_revision=debian_revision,
    )


def effective_debian_revision(pkg: Package, manifest: Manifest) -> str:
    """Return the revision that decides this package's published version."""
    return pkg.debian_revision or manifest.debian_revision


def debian_version(pkg: Package, manifest: Manifest) -> str:
    return f"{pkg.version}{effective_debian_revision(pkg, manifest)}"


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
