"""Discover the latest stable upstream release for a reviewed package.

The GitHub API is the only discovery source and it must supply the asset
digests: an archive is never accepted on a guessed checksum. Asset names, archive
members, binaries, and dependencies come from the reviewed recipe rather than
from the release payload, and the license bytes are compared against the
vendored copy, so a licensing change stops the run instead of being assumed
equivalent.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from .catalog import license_path_for, repo_of
from .license_sources import check_license_sources, vendor_license_sources
from .upstream_assets import (
    ARCHES,
    asset_name,
    check_official_asset_url,
    parse_asset_digest,
    parse_stable_tag,
    releases_api_url,
    tag_for,
)

USER_AGENT = "apt-at-home/1.0"
JSON_TIMEOUT = 30
BYTES_TIMEOUT = 60
# Generous ceilings: the largest pinned musl tarball is well under this, and a
# response that exceeds it is refused rather than buffered without bound.
MAX_JSON_BYTES = 4 * 1024 * 1024


class HttpClient(Protocol):
    """The two bounded reads discovery needs; injectable so tests never hit the network."""

    def get_json(self, url: str) -> object: ...

    def get_bytes(self, url: str, *, max_bytes: int) -> bytes: ...


class HttpError(RuntimeError):
    """A bounded HTTP read failed."""


class UrllibClient:
    """stdlib HTTP with timeouts, response ceilings, and an optional bearer token."""

    def __init__(self, *, token: str | None = None, timeout: int = JSON_TIMEOUT) -> None:
        self._token = token
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
        # The token only ever lives in a request header; nothing logs it.
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _read(self, url: str, *, max_bytes: int) -> bytes:
        request = urllib.request.Request(url, headers=self._headers())
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise HttpError(f"response exceeded {max_bytes} bytes: {url}")
                    chunks.append(chunk)
        except urllib.error.URLError as exc:
            raise HttpError(f"request failed for {url}: {exc.reason}") from exc
        return b"".join(chunks)

    def get_json(self, url: str) -> object:
        return json.loads(self._read(url, max_bytes=MAX_JSON_BYTES).decode("utf-8"))

    def get_bytes(self, url: str, *, max_bytes: int) -> bytes:
        return self._read(url, max_bytes=max_bytes)


@dataclass(frozen=True)
class Candidate:
    """One package's latest stable release, fully validated against upstream."""

    name: str
    repo: str
    tag: str
    version: str
    license_url: str
    targets: dict[str, dict[str, str]]
    extra_license_urls: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    url: str
    sha256: str


def latest_stable(client: HttpClient, package_name: str) -> tuple[str, str, dict[str, ReleaseAsset]]:
    """Return (repo, tag, per-arch assets) for the latest stable release."""
    repo = repo_of(package_name)
    payload = client.get_json(releases_api_url(repo))
    if not isinstance(payload, dict):
        raise ValueError(f"GitHub release payload for {package_name} is not an object")
    if payload.get("draft") or payload.get("prerelease"):
        raise ValueError(f"GitHub returned a draft or prerelease for {package_name}")
    tag = payload.get("tag_name")
    # The tag must carry this project's own prefix and be an exact stable
    # version before any asset name is derived from it.
    version = parse_stable_tag(package_name, tag)
    if tag != tag_for(package_name, version):
        raise ValueError(f"{package_name} release tag {tag!r} is not canonical")
    raw_assets = payload.get("assets")
    if not isinstance(raw_assets, list):
        raise ValueError(f"GitHub release for {package_name} carries no asset list")

    by_name = {a.get("name"): a for a in raw_assets if isinstance(a, dict)}
    assets: dict[str, ReleaseAsset] = {}
    for arch in ARCHES:
        name = asset_name(package_name, version, arch)
        asset = by_name.get(name)
        if asset is None:
            raise ValueError(f"{package_name} {arch} is missing asset {name} from {tag}")
        assets[arch] = ReleaseAsset(
            name=name,
            url=check_official_asset_url(
                asset.get("browser_download_url"), repo=repo, tag=tag, name=name
            ),
            sha256=parse_asset_digest(asset),
        )
    return repo, tag, assets


def check_license(
    client: HttpClient, *, package_name: str, repo: str, tag: str, vendored: bytes
) -> str:
    """Compare every reviewed license source and return the primary URL."""
    if repo != repo_of(package_name):
        raise ValueError(f"{package_name} license repository is not the reviewed upstream")
    sources = vendor_license_sources(package_name)
    sources[license_path_for(package_name)] = vendored
    primary, _ = check_license_sources(client, package_name, tag, vendored=sources)
    return primary


def discover(
    client: HttpClient, package_name: str, *, vendored_license: bytes | None = None
) -> Candidate:
    """Discover and fully validate the latest stable release of one package."""
    repo, tag, assets = latest_stable(client, package_name)
    version = parse_stable_tag(package_name, tag)
    sources = vendor_license_sources(package_name)
    if vendored_license is not None:
        sources[license_path_for(package_name)] = vendored_license
    primary_url, extra_urls = check_license_sources(
        client, package_name, tag, vendored=sources
    )
    targets = {
        arch: {"url": assets[arch].url, "sha256": assets[arch].sha256}
        for arch in ARCHES
    }
    return Candidate(
        name=package_name, repo=repo, tag=tag, version=version,
        license_url=primary_url, targets=targets, extra_license_urls=extra_urls,
    )


def client_from_env() -> UrllibClient:
    """Build the default client; a token raises the API rate limit when present."""
    return UrllibClient(token=os.environ.get("GITHUB_TOKEN") or None)
