"""The GitLab adapter resolves one reviewed release from its official host.

GitLab lists release links rather than per-asset digests, so the adapter must
select one exact stable tag from the newest page, accept only canonical
download links for the reviewed archives and checksums file, and pin the SHA256
values that file names. Every case runs against an injected HTTP double.
"""

from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

from scripts.catalog import RECIPES, asset_name, license_path_for
from scripts.upstream import HttpError, discover
from scripts.upstream_assets import (
    gitlab_releases_api_url,
    license_url,
    release_download_url,
)
from scripts.upstream_gitlab import (
    CHECKSUMS_NAME,
    MAX_CHECKSUMS_BYTES,
    latest_release as gitlab_latest_release,
    select_release,
)

REPO = "gitlab-org/cli"
VERSION = "1.124.0"
TAG = "v1.124.0"
PUBLISHED_AT = "2026-10-20T12:17:01.108Z"
REPO_ROOT = Path(__file__).resolve().parent.parent

ASSET_BYTES = {
    "amd64": b"glab-amd64-archive-bytes",
    "arm64": b"glab-arm64-archive-bytes",
}


def vendored_license() -> bytes:
    return (REPO_ROOT / "packaging" / "licenses" / "glab" / "LICENSE.txt").read_bytes()


def checksums_text(
    *,
    digests: dict[str, str] | None = None,
    extra: tuple[str, ...] = (),
) -> str:
    values = digests if digests is not None else {
        asset_name("glab", VERSION, arch): hashlib.sha256(payload).hexdigest()
        for arch, payload in ASSET_BYTES.items()
    }
    lines = [f"{digest}  {name}" for name, digest in values.items()]
    lines.extend(extra)
    return "\n".join(lines) + "\n"


def asset_link(name: str, *, tag: str = TAG) -> dict[str, object]:
    return {
        "name": name,
        "url": f"https://gitlab.com/api/v4/projects/gitlab-org%2Fcli/packages/generic/glab/{VERSION}/{name}",
        "direct_asset_url": release_download_url(REPO, tag, name, source="gitlab"),
        "link_type": "other",
    }


def release_entry(
    *,
    tag: str = TAG,
    upcoming: object = False,
    released_at: object = PUBLISHED_AT,
    links: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    if links is None:
        links = [
            asset_link(asset_name("glab", VERSION, arch))
            for arch in ("amd64", "arm64")
        ]
        links.append(asset_link(CHECKSUMS_NAME))
    return {
        "name": tag,
        "tag_name": tag,
        "upcoming_release": upcoming,
        "released_at": released_at,
        "assets": {"count": len(links), "sources": [], "links": links},
    }


class FakeHttp:
    def __init__(self, payloads: dict[str, object]) -> None:
        self.payloads = payloads
        self.requests: list[tuple[str, int | None]] = []

    def get_json(self, url: str, *, max_bytes: int = 0) -> object:
        self.requests.append((url, None))
        payload = self.payloads[url]
        if isinstance(payload, Exception):
            raise payload
        return payload

    def get_bytes(self, url: str, *, max_bytes: int) -> bytes:
        self.requests.append((url, max_bytes))
        payload = self.payloads[url]
        if isinstance(payload, Exception):
            raise payload
        assert isinstance(payload, bytes)
        if len(payload) > max_bytes:
            raise HttpError(f"response exceeded {max_bytes} bytes: {url}")
        return payload


def payloads(
    *,
    releases: list[object] | None = None,
    checksums: bytes | None = None,
    license_bytes: bytes | None = None,
) -> dict[str, object]:
    """Serve one GitLab release page, its selected archive checksums, and license."""
    page = releases if releases is not None else [release_entry()]
    responses: dict[str, object] = {
        gitlab_releases_api_url(REPO): page,
        release_download_url(REPO, TAG, CHECKSUMS_NAME, source="gitlab"): (
            checksums if checksums is not None else checksums_text().encode()
        ),
        license_url(REPO, TAG, license_path_for("glab"), source="gitlab"): (
            license_bytes if license_bytes is not None else vendored_license()
        ),
    }
    return responses


class GitlabReleaseSelectionTest(unittest.TestCase):
    def _select(self, entries: object):
        client = FakeHttp(payloads(releases=entries))
        release = gitlab_latest_release(client, "glab", REPO)
        return client, release

    def test_the_reviewed_api_url_is_one_bounded_newest_first_page(self) -> None:
        self.assertEqual(
            gitlab_releases_api_url(REPO),
            "https://gitlab.com/api/v4/projects/gitlab-org%2Fcli/releases"
            "?per_page=100&order_by=released_at&sort=desc",
        )
        self.assertEqual(RECIPES["glab"].repo, REPO)

    def test_the_highest_exact_stable_version_wins(self) -> None:
        entries = [
            release_entry(
                tag="v1.123.0",
                links=[
                    {**asset_link("glab_1.123.0_linux_amd64.tar.gz", tag="v1.123.0")},
                ],
            ),
            release_entry(),
            release_entry(tag="v1.125.0-rc1"),
            release_entry(tag="v2026-10-20"),
        ]
        _client, release = self._select(entries)
        self.assertEqual(release.tag, TAG)
        self.assertEqual(release.assets["amd64"].sha256, hashlib.sha256(ASSET_BYTES["amd64"]).hexdigest())

    def test_an_upcoming_release_is_never_selected(self) -> None:
        entries = [release_entry(tag="v1.126.0", upcoming=True), release_entry()]
        _client, release = self._select(entries)
        self.assertEqual(release.tag, TAG)

    def test_no_stable_release_fails_closed(self) -> None:
        for entries in ([], [release_entry(tag="v1.126.0", upcoming=True)], [release_entry(tag="1.124.0")]):
            with self.subTest(entries=entries):
                with self.assertRaises(ValueError):
                    self._select(entries)

    def test_a_malformed_listing_fails_closed(self) -> None:
        cases = [
            {"not": "a list"},
            ["not an object"],
            [{**release_entry(), "upcoming_release": None}],
            [{**release_entry(), "tag_name": None}],
        ]
        for entries in cases:
            with self.subTest(entries=entries):
                with self.assertRaises(ValueError):
                    self._select(entries)  # type: ignore[arg-type]

    def test_select_release_returns_the_highest_version_from_one_page(self) -> None:
        version, entry = select_release("glab", [release_entry(tag="v1.123.0"), release_entry()])
        self.assertEqual(version, VERSION)
        self.assertEqual(entry["tag_name"], TAG)

    def test_a_transport_failure_surfaces(self) -> None:
        client = FakeHttp({
            gitlab_releases_api_url(REPO): HttpError("request failed for api: timed out")
        })
        with self.assertRaises(HttpError):
            gitlab_latest_release(client, "glab", REPO)


class GitlabAssetTest(unittest.TestCase):
    def _release(self, **overrides: object):
        return gitlab_latest_release(FakeHttp(payloads(**overrides)), "glab", REPO)

    def test_both_reviewed_archives_and_the_checksums_file_are_selected(self) -> None:
        client = FakeHttp(payloads())
        release = gitlab_latest_release(client, "glab", REPO)
        self.assertEqual(sorted(release.assets), ["amd64", "arm64"])
        for arch, payload in ASSET_BYTES.items():
            with self.subTest(arch=arch):
                name = asset_name("glab", VERSION, arch)
                self.assertEqual(release.assets[arch].name, name)
                self.assertEqual(
                    release.assets[arch].url,
                    release_download_url(REPO, TAG, name, source="gitlab"),
                )
                self.assertEqual(
                    release.assets[arch].sha256,
                    hashlib.sha256(payload).hexdigest(),
                )
        self.assertEqual(
            [url for url, _ in client.requests],
            [
                gitlab_releases_api_url(REPO),
                release_download_url(REPO, TAG, CHECKSUMS_NAME, source="gitlab"),
            ],
        )

    def test_a_missing_archive_fails_closed(self) -> None:
        entry = release_entry(
            links=[asset_link(asset_name("glab", VERSION, "amd64")), asset_link(CHECKSUMS_NAME)]
        )
        with self.assertRaisesRegex(ValueError, "missing asset"):
            gitlab_latest_release(FakeHttp(payloads(releases=[entry])), "glab", REPO)

    def test_a_foreign_archive_url_fails_closed(self) -> None:
        links = [
            asset_link(asset_name("glab", VERSION, arch)) for arch in ("amd64", "arm64")
        ]
        links[0]["direct_asset_url"] = (
            "https://github.com/gitlab-org/cli/releases/download/"
            f"{TAG}/glab_{VERSION}_linux_amd64.tar.gz"
        )
        links.append(asset_link(CHECKSUMS_NAME))
        with self.assertRaisesRegex(ValueError, "not the official"):
            gitlab_latest_release(FakeHttp(payloads(releases=[release_entry(links=links)])), "glab", REPO)

    def test_the_links_url_is_never_used_as_a_fallback(self) -> None:
        links = [
            asset_link(asset_name("glab", VERSION, arch)) for arch in ("amd64", "arm64")
        ]
        for link in links:
            link["url"] = "https://example.invalid/elsewhere"
            link["direct_asset_url"] = "https://example.invalid/elsewhere"
        links.append(asset_link(CHECKSUMS_NAME))
        with self.assertRaises(ValueError):
            gitlab_latest_release(FakeHttp(payloads(releases=[release_entry(links=links)])), "glab", REPO)

    def test_a_duplicate_asset_link_fails_closed(self) -> None:
        links = [
            asset_link(asset_name("glab", VERSION, arch)) for arch in ("amd64", "arm64")
        ]
        links.extend([asset_link(CHECKSUMS_NAME), asset_link(CHECKSUMS_NAME)])
        with self.assertRaisesRegex(ValueError, "repeats asset"):
            gitlab_latest_release(FakeHttp(payloads(releases=[release_entry(links=links)])), "glab", REPO)

    def test_a_missing_checksums_file_fails_closed(self) -> None:
        links = [
            asset_link(asset_name("glab", VERSION, arch)) for arch in ("amd64", "arm64")
        ]
        with self.assertRaisesRegex(ValueError, "checksums.txt"):
            gitlab_latest_release(FakeHttp(payloads(releases=[release_entry(links=links)])), "glab", REPO)

    def test_a_foreign_checksums_url_fails_closed(self) -> None:
        entry = release_entry()
        entry["assets"]["links"][-1]["direct_asset_url"] = (  # type: ignore[index]
            f"https://github.com/gitlab-org/cli/releases/download/{TAG}/checksums.txt"
        )
        with self.assertRaisesRegex(ValueError, "not the official"):
            gitlab_latest_release(FakeHttp(payloads(releases=[entry])), "glab", REPO)


class GitlabChecksumTest(unittest.TestCase):
    def _fetch(self, text: bytes) -> object:
        return gitlab_latest_release(FakeHttp(payloads(checksums=text)), "glab", REPO)

    def test_the_checksums_read_is_bounded(self) -> None:
        client = FakeHttp(payloads())
        gitlab_latest_release(client, "glab", REPO)
        checksum_request = [
            bound for url, bound in client.requests
            if url.endswith(f"/downloads/{CHECKSUMS_NAME}")
        ]
        self.assertEqual(checksum_request, [MAX_CHECKSUMS_BYTES])

    def test_other_platform_lines_are_accepted_and_ignored(self) -> None:
        extra = (
            f"{'ab' * 32}  glab_{VERSION}_linux_386.tar.gz",
            f"{'cd' * 32}  glab_{VERSION}_darwin_amd64.tar.gz",
        )
        release = self._fetch(checksums_text(extra=extra).encode())
        self.assertEqual(sorted(release.assets), ["amd64", "arm64"])

    def test_a_malformed_line_fails_closed(self) -> None:
        for text in (
            b"not a checksum\n",
            f"{'a' * 63}  glab_{VERSION}_linux_amd64.tar.gz\n".encode(),
            f"{'a' * 64} glab_{VERSION}_linux_amd64.tar.gz\n".encode(),
        ):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    self._fetch(text)

    def test_a_repeated_name_fails_closed(self) -> None:
        digest = hashlib.sha256(ASSET_BYTES["amd64"]).hexdigest()
        text = checksums_text(extra=(f"{digest}  {asset_name('glab', VERSION, 'amd64')}",))
        with self.assertRaisesRegex(ValueError, "repeats"):
            self._fetch(text.encode())

    def test_a_missing_selected_digest_fails_closed(self) -> None:
        digests = {
            asset_name("glab", VERSION, "amd64"): hashlib.sha256(ASSET_BYTES["amd64"]).hexdigest(),
        }
        with self.assertRaisesRegex(ValueError, "missing digests"):
            self._fetch(checksums_text(digests=digests).encode())

    def test_non_utf8_checksums_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            self._fetch(b"\xff\xfe\n")

    def test_an_unreachable_checksums_file_stops_the_run(self) -> None:
        response = payloads()
        response[release_download_url(REPO, TAG, CHECKSUMS_NAME, source="gitlab")] = HttpError(
            "request failed: gitlab is down"
        )
        with self.assertRaises(HttpError):
            gitlab_latest_release(FakeHttp(response), "glab", REPO)


class GitlabPublishedAtTest(unittest.TestCase):
    def test_an_offset_aware_date_is_normalized_to_utc_seconds(self) -> None:
        entry = release_entry(released_at="2026-10-20T14:17:01.108+02:00")
        release = gitlab_latest_release(FakeHttp(payloads(releases=[entry])), "glab", REPO)
        self.assertEqual(release.upstream_published_at, "2026-10-20T12:17:01Z")

    def test_a_canonical_date_passes_through_unchanged(self) -> None:
        release = gitlab_latest_release(FakeHttp(payloads()), "glab", REPO)
        self.assertEqual(release.upstream_published_at, "2026-10-20T12:17:01Z")

    def test_a_missing_naive_or_unparseable_date_fails_closed(self) -> None:
        for value in (None, "", "2026-10-20T12:17:01", "yesterday", 123):
            with self.subTest(value=value):
                entry = release_entry(released_at=value)
                with self.assertRaises(ValueError):
                    gitlab_latest_release(FakeHttp(payloads(releases=[entry])), "glab", REPO)


class GitlabDiscoveryTest(unittest.TestCase):
    def test_discovery_carries_the_canonical_assets_and_tagged_license(self) -> None:
        client = FakeHttp(payloads())
        candidate = discover(client, "glab", vendored_license=vendored_license())
        self.assertEqual(candidate.repo, REPO)
        self.assertEqual(candidate.tag, TAG)
        self.assertEqual(candidate.version, VERSION)
        self.assertEqual(candidate.upstream_published_at, "2026-10-20T12:17:01Z")
        self.assertEqual(
            candidate.license_url,
            license_url(REPO, TAG, license_path_for("glab"), source="gitlab"),
        )
        self.assertEqual(candidate.extra_license_urls, ())
        for arch, payload in ASSET_BYTES.items():
            self.assertEqual(
                candidate.targets[arch],
                {
                    "url": release_download_url(
                        REPO, TAG, asset_name("glab", VERSION, arch), source="gitlab"
                    ),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                },
            )

    def test_changed_license_bytes_stop_the_run(self) -> None:
        with self.assertRaisesRegex(ValueError, "license bytes changed"):
            discover(
                FakeHttp(payloads(license_bytes=b"a different license text")),
                "glab",
                vendored_license=vendored_license(),
            )

    def test_no_github_release_is_consulted(self) -> None:
        client = FakeHttp(payloads())
        discover(client, "glab", vendored_license=vendored_license())
        requested = [url for url, _ in client.requests]
        self.assertFalse(any("github.com" in url for url in requested))
        self.assertFalse(any("api.github.com" in url for url in requested))


if __name__ == "__main__":
    unittest.main()
