"""Upstream discovery policy: exact stable tags, official assets, digests, licenses.

Every case runs against an injected HTTP double, so the network is never
touched and each rejection is asserted on the response that would cause it. The
payload fixtures are a small fixed representative set, not a mirror of the
catalog: they cover the differing release-tag prefixes, asset names, archive
members, and license sources that discovery has to get right, including a
project whose tags carry no `v` prefix, assets whose names embed the version,
a binary nested under a versioned directory, and a ZIP release asset. A source
that publishes no release digest is covered too: its assets are pinned by the
bytes it serves.
"""

import hashlib
import unittest
from pathlib import Path

from scripts.catalog import RECIPES, archive_member, asset_name, extra_license_paths_for
from scripts.upstream import HttpError, UrllibClient, discover, latest_stable
from scripts.upstream_assets import (
    license_url,
    release_download_url,
    releases_api_url,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
ZELLIJ = "zellij-org/zellij"

# name -> (repo, tag, license path, {arch: asset name})
# A representative set of upstream formats, kept independent of the catalog so
# a new package never edits this table.
UPSTREAMS = {
    "zellij": (
        ZELLIJ, "v0.45.1", "LICENSE.md",
        {
            "amd64": "zellij-x86_64-unknown-linux-musl.tar.gz",
            "arm64": "zellij-aarch64-unknown-linux-musl.tar.gz",
        },
    ),
    "git-delta": (
        "dandavison/delta", "0.20.1", "LICENSE",
        {
            "amd64": "delta-0.20.1-x86_64-unknown-linux-musl.tar.gz",
            "arm64": "delta-0.20.1-aarch64-unknown-linux-gnu.tar.gz",
        },
    ),
    "neovim": (
        "neovim/neovim", "v0.12.6", "LICENSE.txt",
        {
            "amd64": "nvim-linux-x86_64.tar.gz",
            "arm64": "nvim-linux-arm64.tar.gz",
        },
    ),
    "uv": (
        "astral-sh/uv", "0.13.0", "LICENSE-MIT",
        {
            "amd64": "uv-x86_64-unknown-linux-gnu.tar.gz",
            "arm64": "uv-aarch64-unknown-linux-gnu.tar.gz",
        },
    ),
    "bun": (
        "oven-sh/bun", "bun-v1.4.3", "LICENSE.md",
        {
            "amd64": "bun-linux-x64.zip",
            "arm64": "bun-linux-aarch64.zip",
        },
    ),
}


def vendored(name: str) -> bytes:
    return (REPO_ROOT / "packaging" / "licenses" / name / "LICENSE.txt").read_bytes()


def release_payload(
    repo: str,
    *,
    tag: str,
    names: tuple[str, ...],
    digest: str | None = "sha256:" + "ab" * 32,
    url_for_tag: str | None = None,
    draft: bool = False,
    prerelease: bool = False,
) -> dict[str, object]:
    used = tag if url_for_tag is None else url_for_tag
    return {
        "tag_name": tag,
        "draft": draft,
        "prerelease": prerelease,
        "assets": [
            {
                "name": name,
                "digest": digest,
                "browser_download_url": f"https://github.com/{repo}/releases/download/{used}/{name}",
            }
            for name in names
        ],
    }


def fixture_payloads(
    package: str, *, tag: str | None = None, digest: str | None = "sha256:" + "ab" * 32,
    license_bytes: bytes | None = None,
    extra_license_bytes: dict[str, bytes] | None = None,
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    """Serve the latest-release API and the tagged license for one package."""
    repo, default_tag, path, names = UPSTREAMS[package]
    used_tag = tag or default_tag
    version = used_tag.removeprefix(RECIPES[package].tag_prefix)
    body = payload if payload is not None else release_payload(
        repo,
        tag=used_tag,
        names=tuple(
            asset_name(package, version, arch) for arch in ("amd64", "arm64")
        ),
        digest=digest,
    )
    responses: dict[str, object] = {
        releases_api_url(repo): body,
        license_url(repo, used_tag, path): (
            license_bytes if license_bytes is not None else vendored(package)
        ),
    }
    for extra_path in extra_license_paths_for(package):
        responses[license_url(repo, used_tag, extra_path)] = (
            (extra_license_bytes or {}).get(
                extra_path,
                (REPO_ROOT / "packaging" / "licenses" / package / extra_path).read_bytes(),
            )
        )
    return responses


class FakeHttp:
    """Records requests and serves canned payloads; an error entry raises."""

    def __init__(self, payloads: dict[str, object]) -> None:
        self.payloads = payloads
        self.requests: list[str] = []

    def get_json(self, url: str) -> object:
        self.requests.append(url)
        payload = self.payloads[url]
        if isinstance(payload, Exception):
            raise payload
        return payload

    def get_bytes(self, url: str, *, max_bytes: int) -> bytes:
        self.requests.append(url)
        payload = self.payloads[url]
        if isinstance(payload, Exception):
            raise payload
        assert isinstance(payload, bytes)
        if len(payload) > max_bytes:
            raise HttpError(f"response exceeded {max_bytes} bytes: {url}")
        return payload


class LatestStableTest(unittest.TestCase):
    def test_every_fixture_upstream_resolves_both_architectures(self) -> None:
        for package, (repo, tag, _path, names) in UPSTREAMS.items():
            with self.subTest(package=package):
                client = FakeHttp(fixture_payloads(package))
                got_repo, got_tag, assets = latest_stable(client, package)
                self.assertEqual((got_repo, got_tag), (repo, tag))
                self.assertEqual(sorted(assets), ["amd64", "arm64"])
                for arch in ("amd64", "arm64"):
                    self.assertEqual(assets[arch].name, names[arch])
                    self.assertEqual(assets[arch].sha256, "ab" * 32)
                    self.assertEqual(
                        assets[arch].url,
                        f"https://github.com/{repo}/releases/download/{tag}/{names[arch]}",
                    )

    def test_a_bare_tag_is_accepted_only_for_its_own_project(self) -> None:
        client = FakeHttp(fixture_payloads("git-delta"))
        self.assertEqual(latest_stable(client, "git-delta")[1], "0.20.1")
        # delta tags carry no prefix, so a v-prefixed tag is not its own.
        with self.assertRaises(ValueError):
            latest_stable(FakeHttp(fixture_payloads("git-delta", tag="v0.20.1")), "git-delta")

    def test_rejects_drafts_prereleases_and_non_exact_tags(self) -> None:
        cases = {
            "draft": {"draft": True},
            "prerelease": {"prerelease": True},
            "prerelease_tag": {"tag": "v0.46.0-rc1"},
            "four_part_tag": {"tag": "v0.46.0.1"},
            "missing_tag": {"tag": None},
        }
        for label, override in cases.items():
            with self.subTest(case=label):
                payload = release_payload(
                    ZELLIJ,
                    tag=override.get("tag", "v0.45.1"),
                    names=tuple(UPSTREAMS["zellij"][3].values()),
                    draft=override.get("draft", False),
                    prerelease=override.get("prerelease", False),
                )
                if label == "missing_tag":
                    payload["tag_name"] = None
                with self.assertRaises(ValueError):
                    latest_stable(
                        FakeHttp(fixture_payloads("zellij", payload=payload)), "zellij"
                    )

    def test_requires_both_architectures_and_an_api_digest(self) -> None:
        both = tuple(UPSTREAMS["zellij"][3].values())
        cases = {
            "one_arch": {"names": (both[0],)},
            "wrong_asset_name": {"names": (
                "zellij-x86_64-unknown-linux-gnu.tar.gz", both[1],
            )},
            "no_digest": {"digest": None},
            "malformed_digest": {"digest": "sha256:zz"},
            "md5_only": {"digest": "md5:" + "ab" * 16},
        }
        for label, override in cases.items():
            with self.subTest(case=label):
                payload = release_payload(
                    ZELLIJ, tag="v0.45.1", **{"names": both, **override}
                )
                with self.assertRaises(ValueError):
                    latest_stable(
                        FakeHttp(fixture_payloads("zellij", payload=payload)), "zellij"
                    )

    def test_rejects_an_asset_served_from_a_foreign_origin(self) -> None:
        payload = release_payload(
            ZELLIJ, tag="v0.45.1", names=tuple(UPSTREAMS["zellij"][3].values())
        )
        payload["assets"][0]["browser_download_url"] = (  # type: ignore[index]
            "https://github.com/attacker/zellij/releases/download/v0.45.1/"
            "zellij-x86_64-unknown-linux-musl.tar.gz"
        )
        with self.assertRaises(ValueError):
            latest_stable(
                FakeHttp(fixture_payloads("zellij", payload=payload)), "zellij"
            )

    def test_surfaces_transport_failures(self) -> None:
        client = FakeHttp({
            releases_api_url(ZELLIJ): HttpError("request failed for api: timed out")
        })
        with self.assertRaises(HttpError):
            latest_stable(client, "zellij")


class RecipeNamingTest(unittest.TestCase):
    def test_a_version_bump_moves_the_nested_member(self) -> None:
        self.assertEqual(
            archive_member("sccache", "0.18.0", "amd64"),
            "sccache-v0.18.0-x86_64-unknown-linux-musl/sccache",
        )
        self.assertEqual(
            archive_member("sccache", "0.19.0", "amd64"),
            "sccache-v0.19.0-x86_64-unknown-linux-musl/sccache",
        )
        # delta embeds a different toolchain per architecture, so both prefixes
        # move with the version.
        self.assertEqual(
            archive_member("git-delta", "0.21.0", "amd64"),
            "delta-0.21.0-x86_64-unknown-linux-musl/delta",
        )
        self.assertEqual(
            archive_member("git-delta", "0.21.0", "arm64"),
            "delta-0.21.0-aarch64-unknown-linux-gnu/delta",
        )
        # A flat archive keeps the same member across versions.
        self.assertEqual(archive_member("zellij", "0.46.0", "amd64"), "zellij")
        self.assertEqual(archive_member("rustfs-cli", "0.2.0", "amd64"), "rc")

    def test_asset_names_embed_the_version_only_where_upstream_does(self) -> None:
        self.assertEqual(
            asset_name("zellij", "0.46.0", "amd64"),
            "zellij-x86_64-unknown-linux-musl.tar.gz",
        )
        self.assertEqual(
            asset_name("sccache", "0.19.0", "arm64"),
            "sccache-v0.19.0-aarch64-unknown-linux-musl.tar.gz",
        )
        self.assertEqual(
            asset_name("rustfs-cli", "0.2.0", "arm64"),
            "rustfs-cli-linux-arm64-v0.2.0.tar.gz",
        )
        self.assertEqual(
            asset_name("kandev", "0.98.0", "amd64"),
            "kandev-linux-x64.tar.gz",
        )
        self.assertEqual(
            archive_member("kandev", "0.98.0", "arm64"),
            "kandev/bin/kandev",
        )


class DiscoverTest(unittest.TestCase):
    def test_builds_a_complete_candidate_for_every_fixture_upstream(self) -> None:
        for package, (repo, tag, path, _names) in UPSTREAMS.items():
            with self.subTest(package=package):
                client = FakeHttp(fixture_payloads(package))
                candidate = discover(client, package, vendored_license=vendored(package))
                self.assertEqual(candidate.repo, repo)
                self.assertEqual(candidate.tag, tag)
                self.assertEqual(
                    candidate.version, tag.removeprefix(RECIPES[package].tag_prefix)
                )
                self.assertEqual(sorted(candidate.targets), ["amd64", "arm64"])
                self.assertEqual(
                    candidate.license_url,
                    f"https://raw.githubusercontent.com/{repo}/{tag}/{path}",
                )

    def test_changed_license_bytes_stop_the_run(self) -> None:
        for package in UPSTREAMS:
            with self.subTest(package=package):
                client = FakeHttp(
                    fixture_payloads(package, license_bytes=b"GENERIC MIT LICENSE\n")
                )
                with self.assertRaises(ValueError) as ctx:
                    discover(client, package, vendored_license=vendored(package))
                self.assertIn("license bytes changed", str(ctx.exception))

    def test_uv_candidate_keeps_exact_primary_and_supplemental_provenance(self) -> None:
        client = FakeHttp(fixture_payloads("uv"))
        candidate = discover(client, "uv", vendored_license=vendored("uv"))
        urls = (
            "https://raw.githubusercontent.com/astral-sh/uv/0.13.0/LICENSE-MIT",
            "https://raw.githubusercontent.com/astral-sh/uv/0.13.0/LICENSE-APACHE",
        )
        self.assertEqual(candidate.license_url, urls[0])
        self.assertEqual(candidate.extra_license_urls, urls[1:])
        self.assertEqual(
            [url for url in client.requests if "/LICENSE" in url],
            list(urls),
        )

    def test_changed_uv_supplemental_license_stops_discovery(self) -> None:
        client = FakeHttp(fixture_payloads(
            "uv", extra_license_bytes={"LICENSE-APACHE": b"changed Apache text"}
        ))
        with self.assertRaises(ValueError) as ctx:
            discover(client, "uv", vendored_license=vendored("uv"))
        self.assertIn("supplemental license LICENSE-APACHE bytes changed", str(ctx.exception))

    def test_a_missing_digest_stops_before_a_candidate_is_built(self) -> None:
        client = FakeHttp(fixture_payloads("zellij", digest=None))
        with self.assertRaises(ValueError) as ctx:
            discover(client, "zellij", vendored_license=vendored("zellij"))
        self.assertIn("digest", str(ctx.exception))

    def test_unknown_package_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            discover(FakeHttp({}), "nvim", vendored_license=b"x")

    def test_neovim_discovery_yields_the_official_tree_assets(self) -> None:
        client = FakeHttp(fixture_payloads("neovim"))
        candidate = discover(client, "neovim", vendored_license=vendored("neovim"))
        self.assertEqual(candidate.version, "0.12.6")
        for arch, name in (("amd64", "nvim-linux-x86_64.tar.gz"), ("arm64", "nvim-linux-arm64.tar.gz")):
            self.assertEqual(
                candidate.targets[arch]["url"],
                f"https://github.com/neovim/neovim/releases/download/v0.12.6/{name}",
            )
            self.assertRegex(candidate.targets[arch]["sha256"], r"^[0-9a-f]{64}$")


class CargoTauriTagSpecificTest(unittest.TestCase):
    """Multiproduct tags never select another product's latest release."""

    def _client(self, *, tag_payload: dict | None = None, tags: list[dict] | None = None) -> FakeHttp:
        repo = "tauri-apps/tauri"
        tag = "tauri-cli-v2.12.2"
        assets = [
            ("cargo-tauri-x86_64-unknown-linux-gnu.tgz", "ab" * 32),
            ("cargo-tauri-aarch64-unknown-linux-gnu.tgz", "cd" * 32),
        ]
        release = tag_payload if tag_payload is not None else {
            "tag_name": tag, "draft": False, "prerelease": False,
            "assets": [
                {
                    "name": name,
                    "browser_download_url": (
                        f"https://github.com/{repo}/releases/download/{tag}/{name}"
                    ),
                    "digest": f"sha256:{digest}",
                }
                for name, digest in assets
            ],
        }
        vendor = REPO_ROOT / "packaging" / "licenses" / "cargo-tauri"
        from scripts.upstream_assets import release_tag_api_url, tags_api_url

        return FakeHttp({
            tags_api_url(repo): (
                tags if tags is not None else [
                    {"name": "tauri-v2.10.0"},
                    {"name": "tauri-cli-v2.12.2"},
                ]
            ),
            release_tag_api_url(repo, tag): release,
            license_url(repo, tag, "LICENSE-MIT"): (vendor / "LICENSE.txt").read_bytes(),
            license_url(repo, tag, "LICENSE-APACHE-2.0"): (
                vendor / "LICENSE-APACHE-2.0"
            ).read_bytes(),
        })

    def test_tag_specific_release_supplies_both_reviewed_assets_without_repo_wide_latest(self) -> None:
        from scripts.upstream import latest_stable

        repo, tag, assets = latest_stable(self._client(), "cargo-tauri")
        self.assertEqual((repo, tag), ("tauri-apps/tauri", "tauri-cli-v2.12.2"))
        self.assertEqual(sorted(assets), ["amd64", "arm64"])
        # Repository-wide latest is never read for the exact-tag package.
        self.assertNotIn(
            "https://api.github.com/repos/tauri-apps/tauri/releases/latest",
            self._client().requests if hasattr(self._client(), "requests") else [],
        )

    def test_selects_the_highest_stable_cli_tag(self) -> None:
        from scripts.upstream import latest_stable

        client = self._client(tags=[
            {"name": "tauri-cli-v2.11.0"},
            {"name": "tauri-cli-v2.12.2"},
            {"name": "tauri-v2.10.0"},
            {"name": "tauri-cli-v2.12.2-rc1"},
        ])
        _repo, tag, _assets = latest_stable(client, "cargo-tauri")
        self.assertEqual(tag, "tauri-cli-v2.12.2")

    def test_cli_tag_discovery_reaches_beyond_unrelated_first_page(self) -> None:
        """A full first page of unrelated product tags never hides the CLI release."""
        from scripts.upstream import latest_stable
        from scripts.upstream_assets import release_tag_api_url, tags_api_url

        repo = "tauri-apps/tauri"
        tag = "tauri-cli-v2.12.2"
        vendor = REPO_ROOT / "packaging" / "licenses" / "cargo-tauri"
        client = FakeHttp({
            tags_api_url(repo): [{"name": f"tauri-v2.0.{n}"} for n in range(100)],
            tags_api_url(repo, page=2): [{"name": tag}],
            release_tag_api_url(repo, tag): {
                "tag_name": tag, "draft": False, "prerelease": False,
                "assets": [
                    {
                        "name": name,
                        "browser_download_url": (
                            f"https://github.com/{repo}/releases/download/{tag}/{name}"
                        ),
                        "digest": f"sha256:{digest}",
                    }
                    for name, digest in (
                        ("cargo-tauri-x86_64-unknown-linux-gnu.tgz", "ab" * 32),
                        ("cargo-tauri-aarch64-unknown-linux-gnu.tgz", "cd" * 32),
                    )
                ],
            },
            license_url(repo, tag, "LICENSE-MIT"): (vendor / "LICENSE.txt").read_bytes(),
            license_url(repo, tag, "LICENSE-APACHE-2.0"): (
                vendor / "LICENSE-APACHE-2.0"
            ).read_bytes(),
        })
        _repo, selected, assets = latest_stable(client, "cargo-tauri")
        self.assertEqual(selected, tag)
        self.assertEqual(sorted(assets), ["amd64", "arm64"])
        self.assertIn(tags_api_url(repo, page=2), client.requests)

    def test_full_pages_without_a_completed_page_fail_closed(self) -> None:
        from scripts.upstream import latest_stable
        from scripts.upstream_assets import tags_api_url

        repo = "tauri-apps/tauri"
        payloads = {
            tags_api_url(repo, page=page): [{"name": f"tauri-v9.{page}.{n}"} for n in range(100)]
            for page in range(1, 31)
        }
        with self.assertRaisesRegex(ValueError, "without a completed page"):
            latest_stable(FakeHttp(payloads), "cargo-tauri")

    def test_mismatched_draft_or_missing_asset_fails_closed(self) -> None:
        from scripts.upstream import latest_stable

        tag = "tauri-cli-v2.12.2"
        mismatched = dict(
            self._client().payloads[
                f"https://api.github.com/repos/tauri-apps/tauri/releases/tags/{tag}"
            ]  # type: ignore[index]
        )
        mismatched = {**mismatched, "tag_name": "tauri-cli-v2.12.3"}
        with self.assertRaisesRegex(ValueError, "does not match selected"):
            latest_stable(self._client(tag_payload=mismatched), "cargo-tauri")
        draft = {**mismatched, "tag_name": tag, "draft": True}
        with self.assertRaisesRegex(ValueError, "draft or prerelease"):
            latest_stable(self._client(tag_payload=draft), "cargo-tauri")


class CodebergDiscoveryTest(unittest.TestCase):
    """A source that publishes no release digest is pinned by its own bytes."""

    REPO = "forgejo-contrib/forgejo-cli"
    TAG = "v0.7.0"
    VERSION = "0.7.0"
    ASSET_BYTES = {
        "amd64": b"forgejo-cli-amd64-archive-bytes",
        "arm64": b"forgejo-cli-arm64-archive-bytes",
    }

    def _asset_url(self, arch: str) -> str:
        return release_download_url(
            self.REPO, self.TAG, asset_name("forgejo-cli", self.VERSION, arch),
            source="codeberg",
        )

    def _license_url(self, path: str) -> str:
        return license_url(self.REPO, self.TAG, path, source="codeberg")

    def _payloads(self, *, assets: dict[str, bytes] | None = None) -> dict[str, object]:
        """Serve a Codeberg release without any digest, plus its assets and licenses."""
        payloads: dict[str, object] = {
            releases_api_url(self.REPO, source="codeberg"): {
                "tag_name": self.TAG,
                "draft": False,
                "prerelease": False,
                "assets": [
                    {
                        "name": asset_name("forgejo-cli", self.VERSION, arch),
                        "browser_download_url": self._asset_url(arch),
                    }
                    for arch in ("amd64", "arm64")
                ],
            },
        }
        for arch, payload in (assets or self.ASSET_BYTES).items():
            payloads[self._asset_url(arch)] = payload
        vendor = REPO_ROOT / "packaging" / "licenses" / "forgejo-cli"
        payloads[self._license_url("LICENSE-MIT")] = (vendor / "LICENSE.txt").read_bytes()
        payloads[self._license_url("LICENSE-APACHE")] = (vendor / "LICENSE-APACHE").read_bytes()
        return payloads

    def test_the_digest_is_computed_from_the_official_bytes(self) -> None:
        client = FakeHttp(self._payloads())
        candidate = discover(client, "forgejo-cli")
        self.assertEqual(candidate.repo, self.REPO)
        self.assertEqual(candidate.tag, self.TAG)
        self.assertEqual(candidate.version, self.VERSION)
        for arch, payload in self.ASSET_BYTES.items():
            with self.subTest(arch=arch):
                self.assertEqual(
                    candidate.targets[arch]["sha256"],
                    hashlib.sha256(payload).hexdigest(),
                )
                self.assertEqual(candidate.targets[arch]["url"], self._asset_url(arch))

    def test_no_upstream_digest_is_required(self) -> None:
        """The release payload carries no digest field at all."""
        payload = self._payloads()[releases_api_url(self.REPO, source="codeberg")]
        for asset in payload["assets"]:  # type: ignore[index]
            self.assertNotIn("digest", asset)

    def test_the_licenses_come_from_the_reviewed_codeberg_source(self) -> None:
        client = FakeHttp(self._payloads())
        candidate = discover(client, "forgejo-cli")
        self.assertEqual(candidate.license_url, self._license_url("LICENSE-MIT"))
        self.assertEqual(candidate.extra_license_urls, (self._license_url("LICENSE-APACHE"),))

    def test_an_unreachable_asset_stops_the_run(self) -> None:
        payloads = self._payloads()
        payloads[self._asset_url("arm64")] = HttpError("request failed: codeberg is down")
        with self.assertRaises(HttpError):
            discover(FakeHttp(payloads), "forgejo-cli")

    def test_a_missing_asset_stops_the_run(self) -> None:
        payload = self._payloads()[releases_api_url(self.REPO, source="codeberg")]
        payload["assets"] = [  # type: ignore[index]
            asset for asset in payload["assets"]  # type: ignore[index]
            if asset["name"] != asset_name("forgejo-cli", self.VERSION, "arm64")
        ]
        payloads = self._payloads()
        payloads[releases_api_url(self.REPO, source="codeberg")] = payload
        with self.assertRaisesRegex(ValueError, "missing asset"):
            discover(FakeHttp(payloads), "forgejo-cli")

    def test_an_asset_from_a_foreign_origin_is_refused(self) -> None:
        payloads = self._payloads()
        payload = payloads[releases_api_url(self.REPO, source="codeberg")]
        payload["assets"][0]["browser_download_url"] = (  # type: ignore[index]
            "https://github.com/forgejo-contrib/forgejo-cli/releases/download/"
            f"{self.TAG}/forgejo-cli-x86_64-linux.tar.gz"
        )
        with self.assertRaisesRegex(ValueError, "not the official"):
            discover(FakeHttp(payloads), "forgejo-cli")

    def test_changed_license_bytes_stop_the_run(self) -> None:
        payloads = self._payloads()
        payloads[self._license_url("LICENSE-MIT")] = b"a different license text"
        with self.assertRaisesRegex(ValueError, "license bytes changed"):
            discover(FakeHttp(payloads), "forgejo-cli")


class UrllibClientTest(unittest.TestCase):
    def test_reads_are_bounded_and_timeouts_are_set(self) -> None:
        client = UrllibClient(timeout=1)
        self.assertEqual(client._timeout, 1)  # noqa: SLF001 - the bound is the contract

    def test_the_token_only_reaches_the_github_api(self) -> None:
        client = UrllibClient(token="secret-value")
        api = client._headers(  # noqa: SLF001 - the header scope is the contract
            "https://api.github.com/repos/x/y/releases/latest"
        )
        self.assertEqual(api["Authorization"], "Bearer secret-value")
        for url in (
            "https://codeberg.org/api/v1/repos/x/y/releases/latest",
            "https://codeberg.org/x/y/releases/download/v1.0.0/x.tar.gz",
            "https://raw.githubusercontent.com/x/y/v1.0.0/LICENSE",
            "https://github.com/x/y/releases/download/v1.0.0/x.tar.gz",
            "http://api.github.com/repos/x/y/releases/latest",
            "https://api.github.com:8443/repos/x/y/releases/latest",
        ):
            with self.subTest(url=url):
                headers = client._headers(url)  # noqa: SLF001
                self.assertNotIn("Authorization", headers)
                self.assertNotIn("application/vnd.github+json", headers["Accept"] if "Accept" in headers else "")


if __name__ == "__main__":
    unittest.main()