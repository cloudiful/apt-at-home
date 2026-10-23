"""Trusted upstream release identity: tags, official URLs, and digests.

Everything discovered from an upstream host passes through this module before a
byte is downloaded, so an unexpected origin, an asset the reviewed recipe does
not name, or a release without the digest its source provides fails closed
instead of guessing. The API endpoint, the download origin, and the raw license
host are fixed per source here: nothing a manifest or an upstream response can
supply ever builds one. Two sources are reviewed: GitHub, whose API publishes a
sha256 digest for every asset, and Codeberg, whose API publishes none.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from types import MappingProxyType

from .catalog import (
    ARCHES,
    asset_name,
    extra_license_paths_for,
    license_path_for,
    license_source_for,
    recipe_for,
    repo_of,
    tag_for,
)
from .download import NPM_REGISTRY_HOST

API_BASE = "https://api.github.com"
WEB_BASE = "https://github.com"
RAW_BASE = "https://raw.githubusercontent.com"
CODEBERG_API_BASE = "https://codeberg.org/api/v1"
CODEBERG_WEB_BASE = "https://codeberg.org"
# The npm registry is the one origin an npm release may come from, and the
# only origin its metadata and archives are ever read from.
NPM_REGISTRY_BASE = f"https://{NPM_REGISTRY_HOST}"

# The fixed URL form of each reviewed source. Nothing outside this table can
# become a release, download, or raw-license origin.
_RELEASE_API_TEMPLATES = MappingProxyType({
    "github": f"{API_BASE}/repos/{{repo}}/releases/latest",
    "codeberg": f"{CODEBERG_API_BASE}/repos/{{repo}}/releases/latest",
})
_DOWNLOAD_TEMPLATES = MappingProxyType({
    "github": f"{WEB_BASE}/{{repo}}/releases/download/{{tag}}/{{name}}",
    "codeberg": f"{CODEBERG_WEB_BASE}/{{repo}}/releases/download/{{tag}}/{{name}}",
})
_RAW_LICENSE_TEMPLATES = MappingProxyType({
    "github": f"{RAW_BASE}/{{repo}}/{{tag}}/{{path}}",
    "codeberg": f"{CODEBERG_WEB_BASE}/{{repo}}/raw/tag/{{tag}}/{{path}}",
})

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
# Latest stable only: an exact <prefix>N.N.N with no prerelease or build part.
_STABLE_TAG = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def _source_url(templates: Mapping[str, str], source: str, **fields: str) -> str:
    """Return one source's fixed URL form; an unreviewed source fails closed."""
    try:
        template = templates[source]
    except KeyError:
        raise ValueError(f"unsupported release source: {source!r}") from None
    return template.format(**fields)


def releases_api_url(repo: str, *, source: str = "github") -> str:
    """Return the fixed API endpoint for one repository's latest release."""
    return _source_url(_RELEASE_API_TEMPLATES, source, repo=repo)


def release_tag_api_url(repo: str, tag: str) -> str:
    """Return the fixed API endpoint for one repository's release by exact tag.

    A multiproduct repository's `releases/latest` may name another product
    line; the canonical CLI release is fetched by its exact reviewed tag so
    discovery selects the same CLI version. The exact-tag policy is a GitHub
    one, so this endpoint stays on the GitHub API.
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


def release_download_url(repo: str, tag: str, name: str, *, source: str = "github") -> str:
    """Build the canonical download URL for one release asset."""
    return _source_url(_DOWNLOAD_TEMPLATES, source, repo=repo, tag=tag, name=name)


def check_official_asset_url(
    url: object, *, repo: str, tag: str, name: str, source: str = "github"
) -> str:
    """Require the API's download URL to be the canonical official one."""
    expected = release_download_url(repo, tag, name, source=source)
    if url != expected:
        raise ValueError(f"release asset url is not the official {repo} url: {url!r}")
    return expected


def license_url(repo: str, tag: str, path: str, *, source: str = "github") -> str:
    """Return the raw license URL for one exact upstream tag."""
    return _source_url(_RAW_LICENSE_TEMPLATES, source, repo=repo, tag=tag, path=path)


def license_urls_for(package_name: str, tag: str) -> tuple[str, ...]:
    """Return primary and supplemental license URLs in reviewed recipe order."""
    paths = (license_path(package_name), *extra_license_paths_for(package_name))
    repo = repo_of(package_name)
    source = license_source_for(package_name)
    return tuple(license_url(repo, tag, path, source=source) for path in paths)


# One npm package name: an optional scope and a lowercase name. A name outside
# this shape can never name a reviewed package, so no endpoint is built for it.
_NPM_NAME = re.compile(r"^(?:@([a-z0-9][a-z0-9._-]*)/)?([a-z0-9][a-z0-9._-]*)$")


def _split_npm_name(package_name: str) -> tuple[str, str]:
    """Return the scope and the name of one npm package, or fail closed."""
    match = _NPM_NAME.match(package_name)
    if match is None:
        raise ValueError(f"unusable npm package name: {package_name!r}")
    return match.group(1) or "", match.group(2)


def npm_package_endpoint(package_name: str) -> str:
    """Return the canonical registry metadata endpoint for one package.

    A scoped package names its scope and its name in one path segment, so the
    endpoint is built from the reviewed name and never from a response.
    """
    scope, stem = _split_npm_name(package_name)
    if scope:
        return f"{NPM_REGISTRY_BASE}/@{scope}%2F{stem}"
    return f"{NPM_REGISTRY_BASE}/{stem}"


def npm_version_endpoint(package_name: str, version: str) -> str:
    """Return the canonical registry endpoint for one exact package version."""
    if not re.match(r"^[0-9]+\.[0-9]+\.[0-9]+$", version):
        raise ValueError(f"unusable npm version: {version!r}")
    return f"{npm_package_endpoint(package_name)}/{version}"


def npm_tarball_name(package_name: str, version: str) -> str:
    """Return the canonical archive file name of one npm package version."""
    _, stem = _split_npm_name(package_name)
    return f"{stem}-{version}.tgz"


def npm_tarball_url(package_name: str, version: str) -> str:
    """Return the canonical registry archive URL for one package version.

    The URL is constructed from the reviewed scope, name, and version alone,
    so no response can ever name the origin or the path of an archive.
    """
    scope, stem = _split_npm_name(package_name)
    prefix = f"{NPM_REGISTRY_BASE}/@{scope}/{stem}" if scope else (
        f"{NPM_REGISTRY_BASE}/{stem}"
    )
    return f"{prefix}/-/{npm_tarball_name(package_name, version)}"


def check_npm_tarball_url(url: object, package_name: str, version: str) -> str:
    """Require a registry archive URL to be the canonical reviewed one."""
    expected = npm_tarball_url(package_name, version)
    if url != expected:
        raise ValueError(
            f"the npm archive url for {package_name} {version} is not the "
            f"canonical one: {url!r}"
        )
    return expected


__all__ = [
    "ARCHES",
    "API_BASE",
    "CODEBERG_API_BASE",
    "CODEBERG_WEB_BASE",
    "NPM_REGISTRY_BASE",
    "RAW_BASE",
    "WEB_BASE",
    "asset_name",
    "check_npm_tarball_url",
    "check_official_asset_url",
    "license_path",
    "license_url",
    "license_urls_for",
    "npm_package_endpoint",
    "npm_tarball_name",
    "npm_tarball_url",
    "npm_version_endpoint",
    "parse_asset_digest",
    "parse_stable_tag",
    "release_download_url",
    "release_tag_api_url",
    "releases_api_url",
    "tag_for",
    "tags_api_url",
    "version_key",
]