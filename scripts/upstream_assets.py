"""Trusted upstream release identity: tags, official URLs, and digests.

Everything discovered from GitHub passes through this module before a byte is
downloaded, so an unexpected origin, an asset the reviewed recipe does not
name, or a release without an API digest fails closed instead of guessing. The
API endpoint, the download origin, and the raw license host are constants here:
nothing a manifest or an upstream response can supply ever builds one.
"""

from __future__ import annotations

import re

from .catalog import (
    ARCHES,
    asset_name,
    extra_license_paths_for,
    license_path_for,
    recipe_for,
    repo_of,
    tag_for,
)

API_BASE = "https://api.github.com"
WEB_BASE = "https://github.com"
RAW_BASE = "https://raw.githubusercontent.com"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
# Latest stable only: an exact <prefix>N.N.N with no prerelease or build part.
_STABLE_TAG = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def releases_api_url(repo: str) -> str:
    """Return the fixed API endpoint for one repository's latest release."""
    return f"{API_BASE}/repos/{repo}/releases/latest"


def release_tag_api_url(repo: str, tag: str) -> str:
    """Return the fixed API endpoint for one repository's release by exact tag.

    A multiproduct repository's `releases/latest` may name another product
    line; the canonical CLI release is fetched by its exact reviewed tag so
    discovery selects the same CLI version.
    """
    return f"{API_BASE}/repos/{repo}/releases/tags/{tag}"


def tags_api_url(repo: str, per_page: int = 100, page: int = 1) -> str:
    """Return the API endpoint listing one repository's tags for one page.

    Pagination is explicit and bounded: the exact-tag lookup walks pages in
    order and stops only on a short or empty completed page. A full page may
    hide matching tags on later pages, so discovery never assumes the first
    page is newest-first or stops at the first matching version.
    """
    base = f"{API_BASE}/repos/{repo}/tags?per_page={per_page}"
    return base if page <= 1 else f"{base}&page={page}"


def license_path(package_name: str) -> str:
    """Return the license file path inside this package's upstream repository."""
    return license_path_for(package_name)


def parse_stable_tag(package_name: str, tag: str) -> str:
    """Return the version of an exact stable release tag, or fail closed."""
    recipe = recipe_for(package_name)
    if not isinstance(tag, str):
        raise ValueError("release tag must be a string")
    match = _STABLE_TAG.match(tag.removeprefix(recipe.tag_prefix))
    if match is None or not tag.startswith(recipe.tag_prefix):
        raise ValueError(
            f"{package_name} release tag {tag!r} is not an exact stable "
            f"{recipe.tag_prefix}N.N.N version"
        )
    return ".".join(match.groups())


def version_key(version: str) -> tuple[int, int, int]:
    """Order two stable versions numerically; prereleases never reach here."""
    match = _STABLE_TAG.match(version.removeprefix("v"))
    if match is None:
        raise ValueError(f"version is not an exact stable N.N.N version: {version!r}")
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


def license_url(repo: str, tag: str, path: str) -> str:
    """Return the raw license URL for one exact upstream tag."""
    return f"{RAW_BASE}/{repo}/{tag}/{path}"


def license_urls_for(package_name: str, tag: str) -> tuple[str, ...]:
    """Return primary and supplemental license URLs in reviewed recipe order."""
    paths = (license_path(package_name), *extra_license_paths_for(package_name))
    repo = repo_of(package_name)
    return tuple(license_url(repo, tag, path) for path in paths)


__all__ = [
    "ARCHES",
    "API_BASE",
    "RAW_BASE",
    "WEB_BASE",
    "asset_name",
    "check_official_asset_url",
    "license_path",
    "license_url",
    "license_urls_for",
    "parse_asset_digest",
    "parse_stable_tag",
    "release_download_url",
    "release_tag_api_url",
    "releases_api_url",
    "tag_for",
    "tags_api_url",
    "version_key",
]