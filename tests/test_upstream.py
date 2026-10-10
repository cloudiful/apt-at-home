"""Upstream discovery policy: exact stable tags, official assets, digests, licenses.

Every case runs against an injected HTTP double, so the network is never
touched and each rejection is asserted on the response that would cause it.
"""

import unittest
from pathlib import Path

from scripts.upstream import HttpError, UrllibClient, discover, latest_stable

REPO_ROOT = Path(__file__).resolve().parent.parent
ZELLIJ = "zellij-org/zellij"
STARSHIP = "starship/starship"


def vendored(name: str) -> bytes:
    return (REPO_ROOT / "packaging" / "licenses" / name / "LICENSE.txt").read_bytes()


def release_payload(
    repo: str = ZELLIJ,
    *,
    tag: str = "v0.45.1",
    names: tuple[str, ...] = (
        "zellij-x86_64-unknown-linux-musl.tar.gz",
        "zellij-aarch64-unknown-linux-musl.tar.gz",
    ),
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


def latest_url(repo: str = ZELLIJ) -> str:
    return f"https://api.github.com/repos/{repo}/releases/latest"


def license_for(name: str, repo: str, tag: str) -> str:
    path = "LICENSE.md" if name == "zellij" else "LICENSE"
    return f"https://raw.githubusercontent.com/{repo}/{tag}/{path}"


class LatestStableTest(unittest.TestCase):
    def test_returns_every_architecture_with_its_api_digest(self) -> None:
        client = FakeHttp({latest_url(): release_payload()})
        repo, tag, assets = latest_stable(client, "zellij")
        self.assertEqual((repo, tag), (ZELLIJ, "v0.45.1"))
        self.assertEqual(sorted(assets), ["amd64", "arm64"])
        self.assertEqual(assets["amd64"].sha256, "ab" * 32)
        self.assertEqual(
            assets["arm64"].url,
            f"https://github.com/{ZELLIJ}/releases/download/v0.45.1/zellij-aarch64-unknown-linux-musl.tar.gz",
        )

    def test_rejects_drafts_prereleases_and_non_exact_tags(self) -> None:
        cases = {
            "draft": release_payload(draft=True),
            "prerelease": release_payload(prerelease=True),
            "prerelease_tag": release_payload(tag="v0.46.0-rc1"),
            "four_part_tag": release_payload(tag="v0.46.0.1"),
            "missing_tag": {**release_payload(), "tag_name": None},
        }
        for label, payload in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(ValueError):
                    latest_stable(FakeHttp({latest_url(): payload}), "zellij")

    def test_requires_both_architectures_and_an_api_digest(self) -> None:
        cases = {
            "one_arch": release_payload(
                names=("zellij-x86_64-unknown-linux-musl.tar.gz",)
            ),
            "wrong_asset_name": release_payload(
                names=(
                    "zellij-x86_64-unknown-linux-gnu.tar.gz",
                    "zellij-aarch64-unknown-linux-musl.tar.gz",
                )
            ),
            "no_digest": release_payload(digest=None),
            "malformed_digest": release_payload(digest="sha256:zz"),
            "md5_only": release_payload(digest="md5:" + "ab" * 16),
        }
        for label, payload in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(ValueError):
                    latest_stable(FakeHttp({latest_url(): payload}), "zellij")

    def test_rejects_an_asset_served_from_a_foreign_origin(self) -> None:
        payload = release_payload()
        payload["assets"][0]["browser_download_url"] = (  # type: ignore[index]
            "https://github.com/attacker/zellij/releases/download/v0.45.1/"
            "zellij-x86_64-unknown-linux-musl.tar.gz"
        )
        with self.assertRaises(ValueError):
            latest_stable(FakeHttp({latest_url(): payload}), "zellij")

    def test_surfaces_transport_failures(self) -> None:
        client = FakeHttp({latest_url(): HttpError("request failed for api: timed out")})
        with self.assertRaises(HttpError):
            latest_stable(client, "zellij")


class DiscoverTest(unittest.TestCase):
    def _payloads(self, *, license_bytes: bytes | None = None) -> dict[str, object]:
        return {
            latest_url(STARSHIP): release_payload(
                STARSHIP,
                tag="v1.26.0",
                names=(
                    "starship-x86_64-unknown-linux-musl.tar.gz",
                    "starship-aarch64-unknown-linux-musl.tar.gz",
                ),
            ),
            license_for("starship", STARSHIP, "v1.26.0"): (
                license_bytes if license_bytes is not None else vendored("starship")
            ),
        }

    def test_builds_a_complete_candidate_from_the_tag(self) -> None:
        client = FakeHttp(self._payloads())
        candidate = discover(client, "starship", vendored_license=vendored("starship"))
        self.assertEqual(candidate.version, "1.26.0")
        self.assertEqual(candidate.tag, "v1.26.0")
        self.assertEqual(sorted(candidate.targets), ["amd64", "arm64"])
        self.assertEqual(
            candidate.license_url, license_for("starship", STARSHIP, "v1.26.0")
        )

    def test_changed_license_bytes_stop_the_run(self) -> None:
        client = FakeHttp(self._payloads(license_bytes=b"GENERIC MIT LICENSE\n"))
        with self.assertRaises(ValueError) as ctx:
            discover(client, "starship", vendored_license=vendored("starship"))
        self.assertIn("license bytes changed", str(ctx.exception))

    def test_unknown_package_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            discover(FakeHttp({}), "nvim", vendored_license=b"x")


class UrllibClientTest(unittest.TestCase):
    def test_reads_are_bounded_and_timeouts_are_set(self) -> None:
        client = UrllibClient(timeout=1)
        self.assertEqual(client._timeout, 1)  # noqa: SLF001 - the bound is the contract
        self.assertNotIn("Authorization", client._headers())  # noqa: SLF001
        token_client = UrllibClient(token="secret-value")
        headers = token_client._headers()  # noqa: SLF001
        self.assertEqual(headers["Authorization"], "Bearer secret-value")


if __name__ == "__main__":
    unittest.main()
