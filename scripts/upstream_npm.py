"""Resolve an official npm distribution for one reviewed package.

The npm registry is the only origin an npm release may come from. Everything
this module reads is named by the reviewed recipe: the wrapper package, the
platform package published for each reviewed architecture, and the exact
version those two agree on. No URL, package name, or version ever comes from
a response, so a registry answer can only confirm what was already reviewed.

The trust chain is the registry's own subresource integrity: the archive bytes
are compared with the sha512 digest the registry published for that exact
package version before any weaker digest is computed from them. A missing,
malformed, or mismatching digest fails closed, and there is no weaker digest to
fall back to.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from .upstream_assets import (
    NPM_REGISTRY_BASE,
    check_npm_tarball_url,
    npm_package_endpoint,
    npm_tarball_name,
    npm_version_endpoint,
)

# The npm metadata ceiling. The GitHub and Codeberg JSON reads keep their own,
# smaller one: this bound belongs to the npm registry alone.
MAX_METADATA_BYTES = 8 * 1024 * 1024

# The archive ceiling of a reviewed npm platform package. A recipe may set a
# different one; this is the bound used when it declares none.
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024

# The reviewed stable npm line. A newer major line is not a stable update of
# the reviewed one, so it is refused until it is reviewed in its own right.
REVIEWED_MAJORS = frozenset({2})

# One exact stable version: three numeric parts, no prerelease and no build
# metadata. A range, a prerelease, or a fourth part is not a stable version.
_STABLE_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")

# The npm publication instant: UTC with optional fractional seconds.
_PUBLISHED_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")

# One sha512 subresource integrity digest, in the strict base64 alphabet.
_INTEGRITY = re.compile(r"^sha512-(?P<digest>[A-Za-z0-9+/]+={0,2})$")

# The digest length of sha512, in bytes.
_SHA512_BYTES = 64


class NpmClient(Protocol):
    """The two bounded reads the npm resolution needs."""

    def get_json(self, url: str, *, max_bytes: int) -> object: ...

    def get_bytes(self, url: str, *, max_bytes: int) -> bytes: ...


@dataclass(frozen=True)
class NpmAsset:
    """One architecture's verified npm archive."""

    arch: str
    package: str
    name: str
    url: str
    integrity: str
    sha256: str


@dataclass(frozen=True)
class NpmRelease:
    """One reviewed npm release and the archives that carry it."""

    version: str
    published_at: str
    assets: tuple[NpmAsset, ...]


def check_registry_url(url: str) -> str:
    """Require a registry URL to be the plain HTTPS registry endpoint.

    A credential, a port, a query, a fragment, or another host would make the
    URL name something other than the reviewed registry, so each of them is
    refused before a request is sent.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() != "https":
        raise ValueError(f"the npm registry url is not HTTPS: {url!r}")
    if (parts.hostname or "").lower() != NPM_REGISTRY_BASE.removeprefix("https://"):
        raise ValueError(f"the npm registry url names another host: {url!r}")
    if parts.username or parts.password:
        raise ValueError(f"the npm registry url carries a credential: {url!r}")
    if parts.port:
        raise ValueError(f"the npm registry url carries a port: {url!r}")
    if parts.query or parts.fragment:
        raise ValueError(f"the npm registry url carries a query or fragment: {url!r}")
    return url


def select_version(
    packument: object, *, wrapper: str, reviewed_majors: frozenset[int] = REVIEWED_MAJORS
) -> str:
    """Return the exact stable version the wrapper's latest tag names.

    The tag must be an exact stable version of a reviewed major line: a
    prerelease, a range, a build suffix, or a newer major line is refused
    rather than resolved to something this repository never reviewed.
    """
    if not isinstance(packument, dict):
        raise ValueError(f"the npm metadata for {wrapper} is not an object")
    tags = packument.get("dist-tags")
    if not isinstance(tags, dict):
        raise ValueError(f"the npm metadata for {wrapper} carries no dist-tags")
    latest = tags.get("latest")
    if not isinstance(latest, str) or not _STABLE_VERSION.match(latest):
        raise ValueError(
            f"the npm latest tag for {wrapper} is not an exact stable version: {latest!r}"
        )
    major = int(latest.split(".")[0])
    if major not in reviewed_majors:
        raise ValueError(
            f"the npm latest version {latest} for {wrapper} is not a reviewed "
            "stable line"
        )
    return latest


def wrapper_published_at(packument: object, version: str) -> str:
    """Return the wrapper's publication instant for one version, or ''.

    The instant belongs to the wrapper's publication of that version, never to
    an architecture package's upload. A source that reports none leaves the
    date unknown; a source that reports an unusable one stops discovery,
    because a guessed date would be published as fact.
    """
    if not isinstance(packument, dict):
        raise ValueError("the npm metadata is not an object")
    times = packument.get("time")
    if times is None:
        return ""
    if not isinstance(times, dict):
        raise ValueError("the npm publication times are not an object")
    raw = times.get(version)
    if raw is None or raw == "":
        return ""
    if not isinstance(raw, str) or not _PUBLISHED_AT.match(raw):
        raise ValueError(
            f"the npm publication time for {version} is unusable: {raw!r}"
        )
    return raw


def check_platform_dependency(wrapper_meta: object, platform: str, version: str) -> None:
    """Require the wrapper to pin one platform package at the exact version.

    The wrapper's own optional dependency is what ties the published platform
    archive to the reviewed version, so a range, another version, or a missing
    dependency fails closed.
    """
    if not isinstance(wrapper_meta, dict):
        raise ValueError("the npm wrapper metadata is not an object")
    dependencies = wrapper_meta.get("optionalDependencies")
    if not isinstance(dependencies, dict):
        raise ValueError("the npm wrapper declares no optional dependencies")
    pinned = dependencies.get(platform)
    if not isinstance(pinned, str) or pinned != version:
        raise ValueError(
            f"the npm wrapper does not pin {platform} at the exact version {version}"
        )


def verify_integrity(data: bytes, integrity: object) -> str:
    """Return the SHA256 of archive bytes that match their integrity digest.

    The digest must be a sha512 subresource integrity over the whole archive.
    It is decoded strictly, so a malformed digest never degrades into a
    weaker one, and the archive is compared before any digest is computed from
    it.
    """
    if not isinstance(integrity, str):
        raise ValueError("the npm package carries no integrity digest")
    match = _INTEGRITY.match(integrity)
    if match is None:
        raise ValueError(
            f"the npm integrity digest is not a sha512 digest: {integrity!r}"
        )
    try:
        expected = base64.b64decode(match.group("digest"), validate=True)
    except (binascii.Error, ValueError):
        raise ValueError(
            "the npm integrity digest is not strict base64"
        ) from None
    if len(expected) != _SHA512_BYTES:
        raise ValueError(
            f"the npm integrity digest is {_SHA512_BYTES} bytes, not "
            f"{len(expected)}"
        )
    if hashlib.sha512(data).digest() != expected:
        raise ValueError("the npm archive does not match its integrity digest")
    return hashlib.sha256(data).hexdigest()


def _platform_asset(
    client: NpmClient,
    *,
    arch: str,
    platform: str,
    version: str,
    wrapper_meta: object,
    max_archive_bytes: int,
) -> NpmAsset:
    """Return one architecture's verified npm archive."""
    check_platform_dependency(wrapper_meta, platform, version)
    payload = client.get_json(
        npm_version_endpoint(platform, version), max_bytes=MAX_METADATA_BYTES
    )
    if not isinstance(payload, dict):
        raise ValueError(f"the npm metadata for {platform} {version} is not an object")
    if payload.get("name") != platform:
        raise ValueError(
            f"the npm metadata names {payload.get('name')!r}, not {platform!r}"
        )
    if payload.get("version") != version:
        raise ValueError(
            f"the npm metadata for {platform} carries version "
            f"{payload.get('version')!r}, not {version!r}"
        )
    if payload.get("deprecated"):
        raise ValueError(f"the npm package {platform} {version} is deprecated")
    dist = payload.get("dist")
    if not isinstance(dist, dict):
        raise ValueError(f"the npm metadata for {platform} {version} carries no dist")
    url = check_npm_tarball_url(dist.get("tarball"), platform, version)
    check_registry_url(url)
    data = client.get_bytes(url, max_bytes=max_archive_bytes)
    digest = verify_integrity(data, dist.get("integrity"))
    return NpmAsset(
        arch=arch,
        package=platform,
        name=npm_tarball_name(platform, version),
        url=url,
        integrity=str(dist.get("integrity")),
        sha256=digest,
    )


def discover(
    client: NpmClient,
    *,
    wrapper: str,
    platforms: Mapping[str, str],
    max_archive_bytes: int = MAX_ARCHIVE_BYTES,
    reviewed_majors: frozenset[int] = REVIEWED_MAJORS,
) -> NpmRelease:
    """Resolve the reviewed npm release of one package.

    The wrapper's latest stable tag names the version, the wrapper's own
    optional dependencies tie each reviewed platform package to that exact
    version, and each platform archive is fetched from the canonical registry
    URL and checked against the digest the registry published for it.
    """
    if not platforms:
        raise ValueError(f"the npm package {wrapper} publishes no platform package")
    endpoint = npm_package_endpoint(wrapper)
    check_registry_url(endpoint)
    packument = client.get_json(endpoint, max_bytes=MAX_METADATA_BYTES)
    if not isinstance(packument, dict):
        raise ValueError(f"the npm metadata for {wrapper} is not an object")
    if packument.get("name") != wrapper:
        raise ValueError(
            f"the npm metadata names {packument.get('name')!r}, not {wrapper!r}"
        )
    version = select_version(
        packument, wrapper=wrapper, reviewed_majors=reviewed_majors
    )
    versions = packument.get("versions")
    if not isinstance(versions, dict) or version not in versions:
        raise ValueError(f"the npm metadata for {wrapper} carries no {version}")
    wrapper_meta = versions[version]
    if not isinstance(wrapper_meta, dict):
        raise ValueError(f"the npm metadata for {wrapper} {version} is not an object")
    if wrapper_meta.get("name") != wrapper or wrapper_meta.get("version") != version:
        raise ValueError(
            f"the npm metadata for {wrapper} {version} names another version"
        )
    if wrapper_meta.get("deprecated"):
        raise ValueError(
            f"the npm wrapper {wrapper} {version} is deprecated"
        )
    assets = tuple(
        _platform_asset(
            client,
            arch=arch,
            platform=platform,
            version=version,
            wrapper_meta=wrapper_meta,
            max_archive_bytes=max_archive_bytes,
        )
        for arch, platform in sorted(platforms.items())
    )
    return NpmRelease(
        version=version,
        published_at=wrapper_published_at(packument, version),
        assets=assets,
    )
