"""Trusted upstream release identity: repositories, tags, assets, and digests.

Everything discovered from GitHub passes through this module before a byte is
downloaded, so an unexpected origin, an asset that does not follow the pinned
musl naming scheme, or a release without an API digest fails closed instead of
guessing. The two pinned packages and their license identifiers are the whole
trusted surface; there is no generic upstream.
"""

from __future__ import annotations

import re

API_BASE = "https://api.github.com"
WEB_BASE = "https://github.com"
RAW_BASE = "https://raw.githubusercontent.com"

# The only upstream repositories this repository will ever talk to.
TRUSTED_REPOS: dict[str, str] = {
    "zellij": "zellij-org/zellij",
    "starship": "starship/starship",
}
# SPDX identifiers carried by the committed manifest; a change is a stop, never
# a licensing assumption.
EXPECTED_LICENSES: dict[str, str] = {"zellij": "MIT", "starship": "ISC"}
# License file inside each upstream repository, used to rebuild license_url.
LICENSE_PATHS: dict[str, str] = {"zellij": "LICENSE.md", "starship": "LICENSE"}
# Rust musl target triples the pinned manifests already build from.
MUSL_TRIPLES: dict[str, str] = {
    "amd64": "x86_64-unknown-linux-musl",
    "arm64": "aarch64-unknown-linux-musl",
}
ARCHES = ("amd64", "arm64")

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
# Latest stable only: an exact v?N.N.N with no prerelease or build metadata.
_STABLE_TAG = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def repo_for(package_name: str) -> str:
    try:
        return TRUSTED_REPOS[package_name]
    except KeyError:
        raise ValueError(f"no trusted upstream repository for {package_name!r}") from None


def triple_for(arch: str) -> str:
    try:
        return MUSL_TRIPLES[arch]
    except KeyError:
        raise ValueError(f"unsupported architecture: {arch!r}") from None


def asset_name(package_name: str, arch: str) -> str:
    """Return the only asset name accepted for this package and architecture."""
    repo_for(package_name)
    return f"{package_name}-{triple_for(arch)}.tar.gz"


def parse_stable_tag(tag: str) -> str:
    """Return the version of an exact stable release tag, or fail closed."""
    if not isinstance(tag, str):
        raise ValueError("release tag must be a string")
    match = _STABLE_TAG.match(tag.strip())
    if match is None:
        raise ValueError(
            f"release tag is not an exact stable v?N.N.N version: {tag!r}"
        )
    return ".".join(match.groups())


def version_key(version: str) -> tuple[int, int, int]:
    """Order two stable versions numerically; prereleases never reach here."""
    match = _STABLE_TAG.match(version if version.startswith("v") else f"v{version}")
    if match is None:
        raise ValueError(f"version is not an exact stable v?N.N.N version: {version!r}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def parse_asset_digest(asset: object) -> str:
    """Return the API-provided sha256 digest; a missing digest is never guessed."""
    if not isinstance(asset, dict):
        raise ValueError("release asset must be an object")
    raw = asset.get("digest")
    if not isinstance(raw, str) or not raw.startswith("sha256:"):
        raise ValueError(
            f"release asset {asset.get('name')!r} carries no GitHub sha256 digest"
        )
    value = raw[len("sha256:"):].strip().lower()
    if not _SHA256.match(value):
        raise ValueError(f"release asset {asset.get('name')!r} has a malformed sha256 digest")
    return value


def release_download_url(repo: str, tag: str, name: str) -> str:
    """Build the canonical download URL for one release asset."""
    return f"{WEB_BASE}/{repo}/releases/download/{tag}/{name}"


def check_official_asset_url(url: object, *, repo: str, tag: str, name: str) -> str:
    """Require the API's download URL to be the canonical official one."""
    expected = release_download_url(repo, tag, name)
    if url != expected:
        raise ValueError(f"release asset url is not the official {repo} url: {url!r}")
    return expected


def license_url(repo: str, tag: str, license_path: str) -> str:
    """Return the raw license URL for one exact upstream tag."""
    return f"{RAW_BASE}/{repo}/{tag}/{license_path}"
