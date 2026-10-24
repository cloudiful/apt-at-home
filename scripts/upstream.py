"""Discover the latest stable upstream release for a reviewed package.

Each recipe names the source host its release comes from. GitHub's API is the
only source for most packages and must supply the asset digests: an archive is
never accepted on a guessed checksum. Codeberg publishes no release digest, so
the assets of a Codeberg package are pinned by a SHA256 computed here from the
official download. GitLab publishes a release checksums.txt instead of a digest
per asset, so its selected digests are read from that file. Asset names, archive
members, binaries, and dependencies come from the reviewed recipe rather than
from the release payload, and the license bytes are compared against the
vendored copy, so a licensing change stops the run instead of being assumed
equivalent.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from .catalog import (
    arches_for,
    exact_tag_release_for,
    fork_label_for,
    license_path_for,
    max_archive_bytes_for,
    npm_package_for,
    npm_platform_package_for,
    repo_of,
    source_for,
)
from .download import NPM_REGISTRY_HOST
from .license_sources import check_license_sources, vendor_license_sources
from .manifest import normalize_published_at
from .upstream_assets import (
    asset_name,
    check_official_asset_url,
    parse_asset_digest,
    parse_stable_tag,
    release_tag_api_url,
    releases_api_url,
    tag_for,
    tags_api_url,
    version_key,
)
from .upstream_npm import MAX_ARCHIVE_BYTES, discover as discover_npm

USER_AGENT = "apt-at-home/1.0"
JSON_TIMEOUT = 30
BYTES_TIMEOUT = 60
# Generous ceilings: the largest pinned musl tarball is well under this, and a
# response that exceeds it is refused rather than buffered without bound.
MAX_JSON_BYTES = 4 * 1024 * 1024
# A release archive is downloaded whole to be hashed, so it needs its own
# ceiling; the reviewed archives are an order of magnitude below it.
MAX_ASSET_BYTES = 64 * 1024 * 1024
# The only origin a GitHub token is ever sent to, and the path prefix of the
# Codeberg API.
GITHUB_API_HOST = "api.github.com"
CODEBERG_API_PATH = "/api/v1/"
# The most redirects one request chain may follow, each of them inside its
# origin.
MAX_REDIRECTS = 3
# The request attribute that carries the hop budget along one chain.
_HOP_COUNT = "_apt_at_home_redirect_hops"


class HttpClient(Protocol):
    """The two bounded reads discovery needs; injectable so tests never hit the network."""

    def get_json(self, url: str, *, max_bytes: int = MAX_JSON_BYTES) -> object: ...

    def get_bytes(self, url: str, *, max_bytes: int) -> bytes: ...


class HttpError(RuntimeError):
    """A bounded HTTP read failed."""


def _origin(url: str) -> tuple[str, str]:
    """Return the scheme and host:port an URL belongs to."""
    parts = urllib.parse.urlsplit(url)
    return (parts.scheme.lower(), parts.netloc.lower())


def _check_registry_redirect(newurl: str, code: int, headers, fp) -> None:  # type: ignore[no-untyped-def]
    """Refuse a registry redirect that is not the exact canonical identity.

    The registry answers one canonical URL per package, version, and archive,
    so a redirect target that carries a credential, a port, a query, or a
    fragment names a request this repository never reviewed. Every other
    origin keeps its own redirect behavior.
    """
    parts = urllib.parse.urlsplit(newurl)
    if (parts.hostname or "").lower() != NPM_REGISTRY_HOST:
        return
    if parts.netloc.lower() != NPM_REGISTRY_HOST or parts.query or parts.fragment:
        raise urllib.error.HTTPError(
            newurl,
            code,
            "a registry redirect must not carry a credential, port, query, or fragment",
            headers,
            fp,
        )


class SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only inside the origin the request already had.

    A redirect is refused before the next request is sent when it leaves the
    origin (host, port, and scheme) or downgrades to plain HTTP, so a token or
    a cookie scoped to one origin can never reach another one. A registry
    redirect must also stay the exact reviewed request identity: a credential,
    a port, a query, or a fragment on the redirect target is refused, because
    the registry answers only canonical URLs and a response must never select
    another one. A chain follows at most three hops in total, repeated
    destinations included; the budget travels with the request, so every new
    request starts with a full allowance and concurrent chains never share a
    counter.
    """

    max_redirections = MAX_REDIRECTS
    max_repeats = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        if urllib.parse.urlsplit(newurl).scheme.lower() != "https":
            raise urllib.error.HTTPError(
                newurl, code, "a redirect to a non-HTTPS url is refused", headers, fp
            )
        if _origin(newurl) != _origin(req.full_url):
            raise urllib.error.HTTPError(
                newurl, code, "a cross-origin redirect is refused", headers, fp
            )
        _check_registry_redirect(newurl, code, headers, fp)
        hops = getattr(req, _HOP_COUNT, 0)
        if hops >= MAX_REDIRECTS:
            raise urllib.error.HTTPError(
                newurl, code,
                f"the redirect budget of {MAX_REDIRECTS} hops is exhausted",
                headers, fp,
            )
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        setattr(new, _HOP_COUNT, hops + 1)
        return new


class UrllibClient:
    """stdlib HTTP with timeouts, response ceilings, and an optional bearer token."""

    def __init__(self, *, token: str | None = None, timeout: int = JSON_TIMEOUT) -> None:
        self._token = token
        self._timeout = timeout
        self._opener = urllib.request.build_opener(SameOriginRedirectHandler())

    def _headers(self, url: str) -> dict[str, str]:
        headers = {"User-Agent": USER_AGENT}
        parts = urllib.parse.urlsplit(url)
        if parts.scheme.lower() != "https":
            return headers
        if parts.netloc.lower() == GITHUB_API_HOST:
            headers["Accept"] = "application/vnd.github+json"
            # The token only ever lives in a request header to the GitHub API;
            # nothing logs it, and no other host ever receives it.
            if self._token:
                headers["Authorization"] = f"Bearer {self._token}"
        elif parts.netloc.lower() == "codeberg.org" and parts.path.startswith(
            CODEBERG_API_PATH
        ):
            headers["Accept"] = "application/json"
        return headers

    def _read(self, url: str, *, max_bytes: int) -> bytes:
        # The client speaks HTTPS only: a plaintext target is refused here,
        # before any request is created and before the transport is touched.
        if urllib.parse.urlsplit(url).scheme.lower() != "https":
            raise HttpError(f"refusing a non-HTTPS request: {url}")
        request = urllib.request.Request(url, headers=self._headers(url))
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
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
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()
            raise HttpError(f"request failed for {url}: {exc.reason}") from exc
        return b"".join(chunks)

    def get_json(self, url: str, *, max_bytes: int = MAX_JSON_BYTES) -> object:
        return json.loads(self._read(url, max_bytes=max_bytes).decode("utf-8"))

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
    upstream_published_at: str = ""


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    url: str
    sha256: str


@dataclass(frozen=True)
class DiscoveredRelease:
    """One upstream release with the publication instant its API reported."""

    repo: str
    tag: str
    assets: dict[str, ReleaseAsset]
    upstream_published_at: str = ""


def _latest_exact_tag(client: HttpClient, package_name: str, repo: str) -> str:
    """Return the highest stable tag with this package's own prefix.

    Bounded pagination walks the repository's tag pages in order and stops
    only on a short or empty completed page. Matching tags are collected
    across all completed pages and the highest canonical version wins, so a
    first page of unrelated product tags can never hide a CLI release on a
    later page. `releases/latest` is never read here, so another product
    line's latest release can never select the wrong CLI version.
    The bound covers the current ~2000-tag multiproduct listing with room
    for growth; a listing that never completes within the bound fails closed.
    """
    from .catalog import tag_prefix_for

    prefix = tag_prefix_for(package_name)
    per_page = 100
    max_pages = 30
    max_items = per_page * max_pages
    versions: set[str] = set()
    seen = 0
    for page in range(1, max_pages + 1):
        payload = client.get_json(tags_api_url(repo, per_page=per_page, page=page))
        if not isinstance(payload, list):
            raise ValueError(f"GitHub tag listing for {package_name} is not a list")
        if len(payload) > per_page:
            raise ValueError(f"GitHub tag page for {package_name} exceeds {per_page} items")
        seen += len(payload)
        if seen > max_items:
            raise ValueError(f"GitHub tag listing for {package_name} exceeds {max_items} items")
        for entry in payload:
            if not isinstance(entry, dict):
                raise ValueError(f"GitHub tag listing entry for {package_name} is not an object")
            name = entry.get("name")
            if not isinstance(name, str) or not name.startswith(prefix):
                continue
            try:
                version = parse_stable_tag(package_name, name)
            except ValueError:
                continue
            if name != tag_for(package_name, version):
                continue
            versions.add(version)
        if len(payload) < per_page:
            break
    else:
        raise ValueError(
            f"{repo} tag lookup for {package_name} reached {max_pages} pages "
            "without a completed page"
        )
    if not versions:
        label = fork_label_for(package_name)
        expected = (
            f"reviewed {label} fork release"
            if label
            else f"{prefix}N.N.N release"
        )
        raise ValueError(
            f"{repo} tags no {expected} for {package_name}"
        )
    best = max(versions, key=version_key)
    return tag_for(package_name, best)


def _computed_asset_digest(client: HttpClient, url: str) -> str:
    """Return the SHA256 of the official asset bytes, computed here.

    A source that publishes no release digest is pinned by the digest of the
    bytes it serves. The read is bounded, and a failure to fetch the asset
    stops the run rather than skipping the package.
    """
    return hashlib.sha256(client.get_bytes(url, max_bytes=MAX_ASSET_BYTES)).hexdigest()


def _published_at(payload: dict[str, object], *, source: str) -> str:
    """Return the canonical publication instant the release payload reports.

    The instant belongs to the exact upstream release, never to the tag, the
    local build, or the upload. A source that reports no instant leaves the
    date unknown; a source that reports an unusable one stops discovery,
    because a guessed date would be published as fact.
    """
    raw = payload.get("published_at")
    if raw is None or raw == "":
        return ""
    if not isinstance(raw, str):
        raise ValueError(
            f"{source} release date for {payload.get('tag_name')!r} is not a string"
        )
    try:
        return normalize_published_at(raw)
    except ValueError as exc:
        raise ValueError(
            f"{source} release date for {payload.get('tag_name')!r} is unusable: {exc}"
        ) from None


def _latest_npm_release(client: HttpClient, package_name: str, repo: str) -> DiscoveredRelease:
    """Return the reviewed npm release of one package.

    The npm registry answers only for the reviewed wrapper package and the
    reviewed platform packages, and the archives it serves are verified
    against their own integrity digests before their sha256 is computed. No
    GitHub release is consulted for an npm package.
    """
    release = discover_npm(
        client,
        wrapper=npm_package_for(package_name),
        platforms={
            arch: npm_platform_package_for(package_name, arch)
            for arch in arches_for(package_name)
        },
        max_archive_bytes=max_archive_bytes_for(package_name) or MAX_ARCHIVE_BYTES,
    )
    version = release.version
    tag = tag_for(package_name, version)
    assets: dict[str, ReleaseAsset] = {}
    for asset in release.assets:
        name = asset_name(package_name, version, asset.arch)
        if name != asset.name:
            raise ValueError(
                f"the npm archive name for {package_name} {asset.arch} is not "
                f"the reviewed one: {asset.name!r}"
            )
        assets[asset.arch] = ReleaseAsset(
            name=asset.name, url=asset.url, sha256=asset.sha256
        )
    published_at = (
        normalize_published_at(release.published_at) if release.published_at else ""
    )
    return DiscoveredRelease(
        repo=repo,
        tag=tag,
        assets=assets,
        upstream_published_at=published_at,
    )


def latest_release(client: HttpClient, package_name: str) -> DiscoveredRelease:
    """Return the latest stable release with its publication instant.

    Packages with the reviewed exact-tag policy resolve the highest stable tag
    with their own prefix from the tag list and fetch that exact release;
    every other package retains `releases/latest` behavior. Only the reviewed
    architectures carry assets. A GitHub release supplies the asset digest; a
    Codeberg release supplies none, so its digest is computed from the
    official download; an npm release supplies its own integrity digest, and
    its sha256 is computed from the verified archive bytes; a GitLab release
    supplies its selected digests in the release's checksums.txt.
    """
    repo = repo_of(package_name)
    source = source_for(package_name)
    if source == "npm":
        return _latest_npm_release(client, package_name, repo)
    if source == "gitlab":
        # The GitLab adapter lives in its own module; the deferred import keeps
        # this module's source table the single dispatch point.
        from .upstream_gitlab import latest_release as latest_gitlab_release

        return latest_gitlab_release(client, package_name, repo)
    if exact_tag_release_for(package_name):
        tag = _latest_exact_tag(client, package_name, repo)
        # The tag must carry this project's own prefix and be an exact stable
        # version before any asset name is derived from it.
        version = parse_stable_tag(package_name, tag)
        if tag != tag_for(package_name, version):
            raise ValueError(f"{package_name} release tag {tag!r} is not canonical")
        payload = client.get_json(release_tag_api_url(repo, tag))
        if not isinstance(payload, dict):
            raise ValueError(f"GitHub release payload for {package_name} is not an object")
        if payload.get("draft") or payload.get("prerelease"):
            raise ValueError(f"GitHub returned a draft or prerelease for {package_name}")
        payload_tag = payload.get("tag_name")
        if payload_tag != tag:
            raise ValueError(
                f"{package_name} tag-specific release {payload_tag!r} does not match "
                f"selected {tag!r}"
            )
    else:
        payload = client.get_json(releases_api_url(repo, source=source))
        if not isinstance(payload, dict):
            raise ValueError(
                f"{source} release payload for {package_name} is not an object"
            )
        if payload.get("draft") or payload.get("prerelease"):
            raise ValueError(f"{source} returned a draft or prerelease for {package_name}")
        tag = payload.get("tag_name")
        # The tag must carry this project's own prefix and be an exact stable
        # version before any asset name is derived from it.
        version = parse_stable_tag(package_name, tag)
        if tag != tag_for(package_name, version):
            raise ValueError(f"{package_name} release tag {tag!r} is not canonical")
    raw_assets = payload.get("assets")
    if not isinstance(raw_assets, list):
        raise ValueError(f"{source} release for {package_name} carries no asset list")

    by_name = {a.get("name"): a for a in raw_assets if isinstance(a, dict)}
    assets: dict[str, ReleaseAsset] = {}
    for arch in arches_for(package_name):
        name = asset_name(package_name, version, arch)
        asset = by_name.get(name)
        if asset is None:
            raise ValueError(f"{package_name} {arch} is missing asset {name} from {tag}")
        url = check_official_asset_url(
            asset.get("browser_download_url"), repo=repo, tag=tag, name=name, source=source
        )
        # GitHub publishes a digest for every asset; Codeberg publishes none, so
        # the digest of the official bytes is computed here.
        digest = (
            parse_asset_digest(asset)
            if source == "github"
            else _computed_asset_digest(client, url)
        )
        assets[arch] = ReleaseAsset(name=name, url=url, sha256=digest)
    return DiscoveredRelease(
        repo=repo,
        tag=tag,
        assets=assets,
        upstream_published_at=_published_at(payload, source=source),
    )


def latest_stable(
    client: HttpClient, package_name: str,
) -> tuple[str, str, dict[str, ReleaseAsset]]:
    """Return (repo, tag, per-arch assets) for the latest stable release.

    The release itself is resolved by `latest_release`; this wrapper keeps the
    three-value shape the rest of the tooling reads.
    """
    release = latest_release(client, package_name)
    return release.repo, release.tag, release.assets


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
    client: HttpClient,
    package_name: str,
    *,
    vendored_license: bytes | None = None,
) -> Candidate:
    """Discover and fully validate the latest stable release of one package."""
    release = latest_release(client, package_name)
    repo, tag, assets = release.repo, release.tag, release.assets
    version = parse_stable_tag(package_name, tag)
    sources = vendor_license_sources(package_name)
    if vendored_license is not None:
        sources[license_path_for(package_name)] = vendored_license
    primary_url, extra_urls = check_license_sources(
        client, package_name, tag, vendored=sources
    )
    targets = {
        arch: {"url": assets[arch].url, "sha256": assets[arch].sha256}
        for arch in assets
    }
    return Candidate(
        name=package_name, repo=repo, tag=tag, version=version,
        license_url=primary_url, targets=targets, extra_license_urls=extra_urls,
        upstream_published_at=release.upstream_published_at,
    )


def client_from_env() -> UrllibClient:
    """Build the default client; a token raises the API rate limit when present.

    The token is only ever sent to the GitHub API, so a client that also reads
    another source never hands it to that host.
    """
    return UrllibClient(token=os.environ.get("GITHUB_TOKEN") or None)
