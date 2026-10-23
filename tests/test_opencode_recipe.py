"""The OpenCode V2 package comes from its official npm distribution.

The reviewed recipe and the committed manifest entry are checked against each
other and against the catalog derivation, so the asset name, the archive
member, the installed command, the dependency floor, the license provenance,
and the size ceilings can never drift. Discovery runs against an injected
registry double: it reads only the official registry endpoints, selects the
exact stable version the wrapper and the platform packages agree on, and
returns the canonical URLs, the publication instant, and the digest of the
bytes that matched their integrity digest.
"""

from __future__ import annotations

import base64
import hashlib
import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType
from unittest import mock

from scripts.catalog import (
    arches_for,
    archive_member,
    asset_name,
    binary_name,
    depends_for,
    expected_license_for,
    license_path_for,
    license_source_for,
    max_archive_bytes_for,
    max_binary_bytes_for,
    npm_package_for,
    npm_packages_for,
    smoke_args,
    source_for,
    tag_for,
)
from scripts.download import safe_extract_member
from scripts.license_sources import check_license_sources
from scripts.manifest import load_manifest
from scripts.package_licenses import copyright_text, read_vendor_licenses
from scripts.upstream import discover, latest_release
from scripts.upstream_assets import (
    license_urls_for,
    npm_package_endpoint,
    npm_tarball_name,
    npm_tarball_url,
    npm_version_endpoint,
)

NAME = "opencode"
VERSION = "2.1.4"
TAG = "v2.1.4"
PUBLISHED_AT = "2026-10-23T10:13:53Z"
WRAPPER = "@opencode/cli"
PLATFORMS = MappingProxyType({
    "amd64": "@opencode/cli-linux-x64",
    "arm64": "@opencode/cli-linux-arm64",
})
ARCHIVES = MappingProxyType({
    "amd64": b"fake-opencode-amd64-archive",
    "arm64": b"fake-opencode-arm64-archive",
})


def integrity_of(payload: bytes) -> str:
    return "sha512-" + base64.b64encode(hashlib.sha512(payload).digest()).decode()


def registry_payloads() -> dict[str, object]:
    """The registry answers a reviewed OpenCode resolution reads."""
    payloads: dict[str, object] = {
        npm_package_endpoint(WRAPPER): {
            "name": WRAPPER,
            "dist-tags": {"latest": VERSION},
            "time": {VERSION: "2026-10-23T10:13:53.203Z"},
            "versions": {
                VERSION: {
                    "name": WRAPPER,
                    "version": VERSION,
                    "optionalDependencies": {
                        **{platform: VERSION for platform in PLATFORMS.values()},
                        "@opencode/cli-darwin-x64": VERSION,
                    },
                }
            },
        },
    }
    for arch, platform in PLATFORMS.items():
        payloads[npm_version_endpoint(platform, VERSION)] = {
            "name": platform,
            "version": VERSION,
            "dist": {
                "tarball": npm_tarball_url(platform, VERSION),
                "integrity": integrity_of(ARCHIVES[arch]),
            },
        }
        payloads[npm_tarball_url(platform, VERSION)] = ARCHIVES[arch]
    return payloads


class FakeRegistry:
    """Records requests and serves canned registry answers."""

    def __init__(self, payloads: dict[str, object]) -> None:
        self.payloads = payloads
        self.requests: list[str] = []
        self.json_bounds: list[int] = []
        self.byte_bounds: list[int] = []

    def get_json(self, url: str, *, max_bytes: int) -> object:
        self.requests.append(url)
        self.json_bounds.append(max_bytes)
        return self.payloads[url]

    def get_bytes(self, url: str, *, max_bytes: int) -> bytes:
        self.requests.append(url)
        self.byte_bounds.append(max_bytes)
        payload = self.payloads[url]
        assert isinstance(payload, bytes), url
        if len(payload) > max_bytes:
            raise ValueError(f"response exceeded {max_bytes} bytes: {url}")
        return payload


def committed_package():
    return next(pkg for pkg in load_manifest().packages if pkg.name == NAME)


class OpenCodeRecipeTest(unittest.TestCase):
    """The reviewed recipe names the official npm distribution."""

    def test_the_release_comes_from_the_official_npm_registry(self) -> None:
        self.assertEqual(source_for(NAME), "npm")
        self.assertEqual(npm_package_for(NAME), WRAPPER)
        self.assertEqual(
            dict(npm_packages_for(NAME)),
            {"amd64": "@opencode/cli-linux-x64", "arm64": "@opencode/cli-linux-arm64"},
        )
        self.assertEqual(arches_for(NAME), ("amd64", "arm64"))
        self.assertEqual(tag_for(NAME, VERSION), TAG)

    def test_the_platform_packages_are_the_ordinary_non_baseline_builds(self) -> None:
        for platform in npm_packages_for(NAME).values():
            self.assertNotIn("baseline", platform)
            self.assertNotIn("musl", platform)

    def test_the_asset_name_and_member_are_the_npm_archive_and_its_binary(self) -> None:
        for arch in arches_for(NAME):
            self.assertEqual(
                asset_name(NAME, VERSION, arch),
                npm_tarball_name(PLATFORMS[arch], VERSION),
            )
            self.assertEqual(archive_member(NAME, VERSION, arch), "package/bin/opencode")
        self.assertEqual(binary_name(NAME), "opencode")
        self.assertEqual(smoke_args(NAME), ("--version",))

    def test_every_architecture_has_the_reviewed_libc_floor(self) -> None:
        for arch in arches_for(NAME):
            self.assertEqual(depends_for(NAME, arch), "libc6 (>= 2.17)")

    def test_the_license_identity_and_source_are_reviewed(self) -> None:
        self.assertEqual(expected_license_for(NAME), "MIT")
        self.assertEqual(license_path_for(NAME), "LICENSE")
        self.assertEqual(license_source_for(NAME), "github")
        self.assertEqual(
            license_urls_for(NAME, TAG),
            (f"https://raw.githubusercontent.com/anomalyco/opencode/{TAG}/LICENSE",),
        )

    def test_the_reviewed_ceilings_are_the_large_binary_ones(self) -> None:
        self.assertEqual(max_archive_bytes_for(NAME), 128 * 1024 * 1024)
        self.assertEqual(max_binary_bytes_for(NAME), 256 * 1024 * 1024)


class OpenCodeManifestTest(unittest.TestCase):
    """The committed entry carries the reviewed pins and nothing else."""

    def test_the_entry_pins_the_reviewed_release(self) -> None:
        pkg = committed_package()
        self.assertEqual(pkg.version, VERSION)
        self.assertEqual(pkg.homepage, "https://opencode.ai/v2/")
        self.assertEqual(pkg.license, "MIT")
        self.assertEqual(pkg.copyright, "Copyright (c) 2025 opencode")
        self.assertEqual(pkg.upstream_published_at, PUBLISHED_AT)
        self.assertEqual(pkg.extra_license_urls, ())
        self.assertEqual(set(pkg.targets), set(arches_for(NAME)))

    def test_every_target_is_the_canonical_registry_archive(self) -> None:
        pkg = committed_package()
        for arch, target in pkg.targets.items():
            self.assertEqual(
                target.url, npm_tarball_url(PLATFORMS[arch], VERSION)
            )
            self.assertEqual(target.archive_member, "package/bin/opencode")
            self.assertEqual(target.binary, "opencode")
            self.assertEqual(target.depends, "libc6 (>= 2.17)")
            self.assertRegex(target.sha256, r"^[0-9a-f]{64}$")

    def test_the_license_url_is_bound_to_the_pinned_tag(self) -> None:
        pkg = committed_package()
        self.assertEqual(pkg.license_url, license_urls_for(NAME, TAG)[0])


class OpenCodeLicenseTest(unittest.TestCase):
    """The vendored notice is the reviewed MIT text and it is installed."""

    def test_the_vendored_primary_license_is_the_mit_grant(self) -> None:
        pkg = committed_package()
        primary, = read_vendor_licenses(pkg)
        self.assertTrue(primary.startswith("MIT License\n"))
        self.assertIn("Permission is hereby granted, free of charge", primary)
        self.assertIn("THE SOFTWARE IS PROVIDED \"AS IS\"", primary)
        self.assertIn("Copyright (c) 2025 opencode", primary)

    def test_the_installed_notice_carries_the_full_license_and_provenance(self) -> None:
        pkg = committed_package()
        primary, = read_vendor_licenses(pkg)
        text = copyright_text(pkg, primary)
        self.assertIn("Upstream-Name: opencode\n", text)
        self.assertIn("Source: https://opencode.ai/v2/\n", text)
        self.assertIn("Copyright: Copyright (c) 2025 opencode\n", text)
        self.assertIn("License: MIT\n", text)
        self.assertIn(primary.rstrip(), text)
        self.assertEqual(text.count("License:"), 1)

    def test_the_vendored_bytes_are_the_tagged_upstream_bytes(self) -> None:
        """The vendored copy is compared with what the pinned tag serves."""
        pkg = committed_package()
        primary, = read_vendor_licenses(pkg)
        url = license_urls_for(NAME, TAG)[0]

        class Source:
            def __init__(self, payload: bytes) -> None:
                self.payload = payload
                self.requests: list[str] = []

            def get_bytes(self, url: str, *, max_bytes: int) -> bytes:
                self.requests.append(url)
                return self.payload

        source = Source(primary.encode("utf-8"))
        self.assertEqual(
            check_license_sources(source, NAME, TAG, vendored={"LICENSE": source.payload}),
            (url, ()),
        )
        self.assertEqual(source.requests, [url])

        with self.assertRaisesRegex(ValueError, "license bytes changed"):
            check_license_sources(
                Source(b"a different license text"),
                NAME,
                TAG,
                vendored={"LICENSE": primary.encode("utf-8")},
            )


class OpenCodeDiscoveryTest(unittest.TestCase):
    """Discovery selects the reviewed release from the official registry only."""

    def test_discovery_selects_the_exact_stable_registry_release(self) -> None:
        client = FakeRegistry(registry_payloads())
        release = latest_release(client, NAME)

        self.assertEqual(release.tag, TAG)
        self.assertEqual(release.upstream_published_at, PUBLISHED_AT)
        for arch in arches_for(NAME):
            asset = release.assets[arch]
            self.assertEqual(asset.name, asset_name(NAME, VERSION, arch))
            self.assertEqual(asset.url, npm_tarball_url(PLATFORMS[arch], VERSION))
            self.assertEqual(
                asset.sha256, hashlib.sha256(ARCHIVES[arch]).hexdigest()
            )

    def test_discovery_reads_only_the_registry_endpoints(self) -> None:
        client = FakeRegistry(registry_payloads())
        latest_release(client, NAME)

        self.assertTrue(client.requests)
        for url in client.requests:
            self.assertTrue(url.startswith("https://registry.npmjs.org/"), url)
        self.assertNotIn("api.github.com", " ".join(client.requests))
        self.assertNotIn("releases/download", " ".join(client.requests))

    def test_the_reviewed_ceiling_bounds_the_registry_reads(self) -> None:
        client = FakeRegistry(registry_payloads())
        latest_release(client, NAME)

        self.assertEqual(set(client.byte_bounds), {max_archive_bytes_for(NAME)})
        self.assertEqual(set(client.json_bounds), {8 * 1024 * 1024})

    def test_the_candidate_carries_the_registry_urls_and_the_tagged_license(self) -> None:
        client = FakeRegistry(registry_payloads())
        pkg = committed_package()
        primary, = read_vendor_licenses(pkg)
        client.payloads[license_urls_for(NAME, TAG)[0]] = primary.encode("utf-8")

        candidate = discover(client, NAME)

        self.assertEqual(candidate.version, VERSION)
        self.assertEqual(candidate.tag, TAG)
        self.assertEqual(candidate.upstream_published_at, PUBLISHED_AT)
        self.assertEqual(candidate.license_url, pkg.license_url)
        self.assertEqual(candidate.extra_license_urls, ())
        for arch in arches_for(NAME):
            self.assertEqual(
                candidate.targets[arch]["url"], pkg.targets[arch].url
            )


class OpenCodeExtractionLimitTest(unittest.TestCase):
    """The reviewed member ceiling replaces the legacy global one."""

    def test_the_reviewed_ceiling_accepts_a_member_the_global_one_refuses(self) -> None:
        member = archive_member(NAME, VERSION, "amd64")
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "cli-linux-x64-2.1.3.tgz"
            with tarfile.open(archive, "w:gz") as tar:
                info = tarfile.TarInfo(member)
                info.size = len(ARCHIVES["amd64"])
                tar.addfile(info, io.BytesIO(ARCHIVES["amd64"]))

            with mock.patch("scripts.download.MAX_BINARY_BYTES", 4):
                with self.assertRaisesRegex(ValueError, "limit"):
                    safe_extract_member(archive, member, Path(tmp) / "global")
                extracted = safe_extract_member(
                    archive,
                    member,
                    Path(tmp) / "reviewed",
                    max_bytes=max_binary_bytes_for(NAME),
                )
            self.assertEqual(extracted.read_bytes(), ARCHIVES["amd64"])


if __name__ == "__main__":
    unittest.main()
