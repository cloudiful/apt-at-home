"""Discover the latest stable upstream release for the two pinned packages.

The GitHub API is the only discovery source and it must supply the asset
digests: an archive is never accepted on a guessed checksum. License bytes are
compared against the vendored copy, so a licensing change stops the run instead
of being assumed equivalent.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from .upstream_assets import (
    API_BASE,
    ARCHES,
    EXPECTED_LICENSES,
    LICENSE_PATHS,
    asset_name,
    check_official_asset_url,
    license_url,
    parse_asset_digest,
    parse_stable_tag,
    repo_for,
)

USER_AGENT = "apt-at-home/1.0"
JSON_TIMEOUT = 30
BYTES_TIMEOUT = 60
# Generous ceilings: the largest pinned musl tarball is well under this, and a
# response that exceeds it is refused rather than buffered without bound.
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_LICENSE_BYTES = 1024 * 1024


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


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    url: str
    sha256: str


def latest_stable(client: HttpClient, package_name: str) -> tuple[str, str, dict[str, ReleaseAsset]]:
    """Return (repo, tag, per-arch assets) for the latest stable release."""
    repo = repo_for(package_name)
    payload = client.get_json(f"{API_BASE}/repos/{repo}/releases/latest")
    if not isinstance(payload, dict):
        raise ValueError(f"GitHub release payload for {package_name} is not an object")
    if payload.get("draft") or payload.get("prerelease"):
        raise ValueError(f"GitHub returned a draft or prerelease for {package_name}")
    tag = payload.get("tag_name")
    parse_stable_tag(tag)  # exact stable v?N.N.N or fail closed
    raw_assets = payload.get("assets")
    if not isinstance(raw_assets, list):
        raise ValueError(f"GitHub release for {package_name} carries no asset list")

    by_name = {a.get("name"): a for a in raw_assets if isinstance(a, dict)}
    assets: dict[str, ReleaseAsset] = {}
    for arch in ARCHES:
        name = asset_name(package_name, arch)
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


def check_license(client: HttpClient, *, package_name: str, repo: str, tag: str, vendored: bytes) -> str:
    """Compare the tagged license bytes with the vendored copy; return its URL."""
    url = license_url(repo, tag, LICENSE_PATHS[package_name])
    remote = client.get_bytes(url, max_bytes=MAX_LICENSE_BYTES)
    if remote != vendored:
        raise ValueError(
            f"{package_name} license bytes changed in {tag}; "
            f"human review required before republishing ({url})"
        )
    return url


def discover(client: HttpClient, package_name: str, *, vendored_license: bytes) -> Candidate:
    """Discover and fully validate the latest stable release of one package."""
    expected_license = EXPECTED_LICENSES.get(package_name)
    if expected_license is None:
        raise ValueError(f"no expected license for {package_name!r}")
    repo, tag, assets = latest_stable(client, package_name)
    version = parse_stable_tag(tag)
    url = check_license(client, package_name=package_name, repo=repo, tag=tag, vendored=vendored_license)
    targets = {
        arch: {"url": assets[arch].url, "sha256": assets[arch].sha256}
        for arch in ARCHES
    }
    return Candidate(
        name=package_name, repo=repo, tag=tag, version=version,
        license_url=url, targets=targets,
    )


def client_from_env() -> UrllibClient:
    """Build the default client; a token raises the API rate limit when present."""
    return UrllibClient(token=os.environ.get("GITHUB_TOKEN") or None)
