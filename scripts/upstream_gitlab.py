"""Resolve one reviewed package's latest stable release from GitLab.

GitLab's release API lists one entry per release with its asset links, not one
digest per asset. A release is accepted only when its tag is this recipe's exact
stable version, its required archives and the release's checksums file carry the
canonical GitLab download URLs for that tag, and checksums.txt names a SHA256
for every selected archive. The checksum file is unsigned: the pinned digest
trusts GitLab's TLS transport and repository integrity, never a cryptographic
signature. The listing is bounded to one newest-first page, and every value used
to build a URL comes from the reviewed recipe rather than from GitLab.
"""

from __future__ import annotations

import re

from .catalog import arches_for, asset_name, tag_for
from .manifest import normalize_published_at
from .upstream import DiscoveredRelease, HttpClient, ReleaseAsset
from .upstream_assets import (
    GITLAB_RELEASES_PER_PAGE,
    check_official_asset_url,
    gitlab_releases_api_url,
    parse_stable_tag,
    version_key,
)

# The release's own checksum file is small; anything larger is refused before
# it is buffered whole.
MAX_CHECKSUMS_BYTES = 64 * 1024
CHECKSUMS_NAME = "checksums.txt"
# One sha256sum line: the digest, two spaces, and the exact file name.
_CHECKSUM_LINE = re.compile(r"^([0-9a-f]{64})  ([^\s/]+)$")


def _release_list(client: HttpClient, repo: str) -> list[object]:
    """Return one bounded GitLab release page for the reviewed project."""
    payload = client.get_json(gitlab_releases_api_url(repo))
    if not isinstance(payload, list):
        raise ValueError(f"GitLab release listing for {repo} is not a list")
    if len(payload) > GITLAB_RELEASES_PER_PAGE:
        raise ValueError(
            f"GitLab release listing for {repo} exceeds "
            f"{GITLAB_RELEASES_PER_PAGE} items"
        )
    return payload


def select_release(package_name: str, payload: list[object]) -> tuple[str, dict[str, object]]:
    """Return the highest stable version and its release entry.

    Every entry must carry an explicit boolean `upcoming_release`; an upcoming
    release is never selected, and a tag that is not this package's exact stable
    version is skipped rather than guessed at.
    """
    selected: tuple[str, dict[str, object]] | None = None
    for entry in payload:
        if not isinstance(entry, dict):
            raise ValueError(
                f"GitLab release listing entry for {package_name} is not an object"
            )
        upcoming = entry.get("upcoming_release")
        if not isinstance(upcoming, bool):
            raise ValueError(
                f"GitLab release entry for {package_name} carries no explicit "
                "upcoming_release flag"
            )
        tag = entry.get("tag_name")
        if not isinstance(tag, str):
            raise ValueError(
                f"GitLab release entry for {package_name} carries no tag name"
            )
        if upcoming:
            continue
        try:
            version = parse_stable_tag(package_name, tag)
        except ValueError:
            continue
        if tag != tag_for(package_name, version):
            continue
        if selected is None or version_key(version) > version_key(selected[0]):
            selected = (version, entry)
    if selected is None:
        raise ValueError(
            f"GitLab lists no {tag_for(package_name, 'N.N.N')} release for "
            f"{package_name}"
        )
    return selected


def _release_links(package_name: str, entry: dict[str, object]) -> dict[str, dict[str, object]]:
    """Return the release's asset links by name, refusing duplicates."""
    assets = entry.get("assets")
    if not isinstance(assets, dict):
        raise ValueError(f"GitLab release for {package_name} carries no assets object")
    links = assets.get("links")
    if not isinstance(links, list):
        raise ValueError(f"GitLab release for {package_name} carries no asset links")
    by_name: dict[str, dict[str, object]] = {}
    for link in links:
        if not isinstance(link, dict):
            raise ValueError(f"GitLab asset link for {package_name} is not an object")
        name = link.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"GitLab asset link for {package_name} carries no name")
        if name in by_name:
            raise ValueError(f"GitLab release for {package_name} repeats asset {name!r}")
        by_name[name] = link
    return by_name


def _parse_checksums(text: str, names: list[str]) -> dict[str, str]:
    """Return the strict SHA256 for every selected asset name.

    A malformed or repeated line stops the run; a valid line for an archive this
    package does not select is accepted and ignored, because upstream publishes
    checksums for every release asset.
    """
    digests: dict[str, str] = {}
    for line in text.splitlines():
        if not line:
            continue
        match = _CHECKSUM_LINE.fullmatch(line)
        if match is None:
            raise ValueError("GitLab checksums.txt carries a malformed line")
        digest, name = match.groups()
        if name in digests:
            raise ValueError(f"GitLab checksums.txt repeats {name!r}")
        digests[name] = digest
    missing = sorted(set(names) - set(digests))
    if missing:
        raise ValueError(
            f"GitLab checksums.txt is missing digests for {', '.join(missing)}"
        )
    return digests


def _published_at(package_name: str, entry: dict[str, object]) -> str:
    """Return the canonical release instant; a missing one is an error."""
    raw = entry.get("released_at")
    if not isinstance(raw, str) or not raw:
        raise ValueError(
            f"GitLab release for {package_name} carries no release date"
        )
    try:
        return normalize_published_at(raw)
    except ValueError as exc:
        raise ValueError(
            f"GitLab release date for {package_name} is unusable: {exc}"
        ) from None


def latest_release(client: HttpClient, package_name: str, repo: str) -> DiscoveredRelease:
    """Return the latest stable GitLab release of one reviewed package."""
    version, entry = select_release(package_name, _release_list(client, repo))
    tag = tag_for(package_name, version)
    by_name = _release_links(package_name, entry)

    urls: dict[str, str] = {}
    names: list[str] = []
    for arch in arches_for(package_name):
        name = asset_name(package_name, version, arch)
        link = by_name.get(name)
        if link is None:
            raise ValueError(
                f"{package_name} {arch} is missing asset {name} from {tag}"
            )
        urls[arch] = check_official_asset_url(
            link.get("direct_asset_url"),
            repo=repo,
            tag=tag,
            name=name,
            source="gitlab",
        )
        names.append(name)

    checksum_link = by_name.get(CHECKSUMS_NAME)
    if checksum_link is None:
        raise ValueError(f"{package_name} release {tag} is missing {CHECKSUMS_NAME}")
    checksum_url = check_official_asset_url(
        checksum_link.get("direct_asset_url"),
        repo=repo,
        tag=tag,
        name=CHECKSUMS_NAME,
        source="gitlab",
    )
    try:
        checksums = client.get_bytes(checksum_url, max_bytes=MAX_CHECKSUMS_BYTES)
        text = checksums.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(
            f"GitLab checksums.txt for {package_name} {tag} is not UTF-8"
        ) from None
    digests = _parse_checksums(text, names)

    assets: dict[str, ReleaseAsset] = {}
    for arch, url in urls.items():
        name = asset_name(package_name, version, arch)
        assets[arch] = ReleaseAsset(name=name, url=url, sha256=digests[name])
    return DiscoveredRelease(
        repo=repo,
        tag=tag,
        assets=assets,
        upstream_published_at=_published_at(package_name, entry),
    )


__all__ = [
    "CHECKSUMS_NAME",
    "MAX_CHECKSUMS_BYTES",
    "latest_release",
    "select_release",
]
