"""The npm adapter resolves one reviewed release from the official registry.

The evidence is the request the adapter made and the bytes it accepted: the
registry endpoints it read are the reviewed ones, the version it selected is
the exact stable version every reviewed platform package agrees on, and the
digest it returns was computed only from archive bytes that matched the
registry's own sha512 integrity digest.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import unittest
from collections.abc import Iterator
from types import MappingProxyType
from unittest import mock

from scripts.catalog import (
    license_source_for,
    max_archive_bytes_for,
    max_binary_bytes_for,
    npm_package_for,
    npm_platform_package_for,
)
from scripts.recipes import RECIPES, Recipe
from scripts.upstream import HttpError
from scripts.upstream_assets import (
    npm_package_endpoint,
    npm_tarball_name,
    npm_tarball_url,
    npm_version_endpoint,
)
from scripts.upstream_npm import (
    MAX_ARCHIVE_BYTES,
    MAX_METADATA_BYTES,
    discover,
    select_version,
    verify_integrity,
)

WRAPPER = "@opencode/cli"
PLATFORMS = {
    "amd64": "@opencode/cli-linux-x64",
    "arm64": "@opencode/cli-linux-arm64",
}
VERSION = "2.1.3"
PUBLISHED_AT = "2026-10-22T12:44:20.273Z"

ARCHIVES = {
    "@opencode/cli-linux-x64": b"opencode-amd64-archive",
    "@opencode/cli-linux-arm64": b"opencode-arm64-archive",
}

NPM_RECIPE = Recipe(
    name="opencode-npm-test",
    upstream="opencode",
    repo="anomalyco/opencode",
    tag_prefix="v",
    license_id="MIT",
    license_path="LICENSE",
    source="npm",
    asset="cli-linux-{triple}-{version}.tgz",
    member="package/bin/opencode",
    binary="opencode",
    triples=MappingProxyType({"amd64": "x64", "arm64": "arm64"}),
    npm_package=WRAPPER,
    npm_packages=MappingProxyType(dict(PLATFORMS)),
    license_source="github",
    max_archive_bytes=128 * 1024 * 1024,
    max_binary_bytes=256 * 1024 * 1024,
)


@contextlib.contextmanager
def npm_recipe(recipe: Recipe = NPM_RECIPE) -> Iterator[Recipe]:
    """Install one reviewed npm recipe for the length of one test.

    The recipe table is the only place a package is reviewed, so a test that
    needs an npm package installs one there and removes it again. The accepted
    name set follows the recipe table, so it is widened for the same length.
    """
    RECIPES[recipe.name] = recipe
    try:
        with mock.patch("scripts.manifest.KNOWN_PACKAGES", frozenset(RECIPES)), \
                mock.patch("scripts.catalog.KNOWN_NAMES", frozenset(RECIPES)):
            yield recipe
    finally:
        RECIPES.pop(recipe.name, None)


def integrity_of(payload: bytes) -> str:
    """Return the sha512 subresource integrity of some archive bytes."""
    return "sha512-" + base64.b64encode(hashlib.sha512(payload).digest()).decode()


def wrapper_packument(
    *,
    version: str = VERSION,
    latest: str = VERSION,
    published_at: str | None = PUBLISHED_AT,
    dependencies: dict[str, str] | None = None,
    name: str = WRAPPER,
) -> dict[str, object]:
    """Return a wrapper packument the adapter may resolve."""
    pinned = dependencies if dependencies is not None else {
        platform: version for platform in PLATFORMS.values()
    }
    payload: dict[str, object] = {
        "name": name,
        "dist-tags": {"latest": latest},
        "versions": {
            version: {
                "name": name,
                "version": version,
                "optionalDependencies": dict(pinned),
            }
        },
    }
    if published_at is not None:
        payload["time"] = {version: published_at}
    return payload


def platform_metadata(
    platform: str,
    *,
    version: str = VERSION,
    tarball: str | None = None,
    integrity: str | None = None,
    deprecated: str | None = None,
    name: str | None = None,
) -> dict[str, object]:
    """Return one platform package's version metadata."""
    payload: dict[str, object] = {
        "name": name if name is not None else platform,
        "version": version,
        "dist": {
            "tarball": tarball if tarball is not None else npm_tarball_url(platform, version),
            "integrity": (
                integrity if integrity is not None else integrity_of(ARCHIVES[platform])
            ),
        },
    }
    if deprecated is not None:
        payload["deprecated"] = deprecated
    return payload


def npm_payloads(
    *,
    wrapper: dict[str, object] | None = None,
    platforms: dict[str, dict[str, object]] | None = None,
    archives: dict[str, bytes] | None = None,
) -> dict[str, object]:
    """Return the registry answers one resolution reads."""
    payloads: dict[str, object] = {
        npm_package_endpoint(WRAPPER): wrapper or wrapper_packument(),
    }
    for arch, platform in PLATFORMS.items():
        metadata = (platforms or {}).get(arch, platform_metadata(platform))
        payloads[npm_version_endpoint(platform, VERSION)] = metadata
        payloads[npm_tarball_url(platform, VERSION)] = (archives or ARCHIVES)[platform]
    return payloads


class FakeNpmClient:
    """Records requests and serves canned registry answers."""

    def __init__(self, payloads: dict[str, object]) -> None:
        self.payloads = payloads
        self.requests: list[str] = []
        self.json_bounds: list[int] = []
        self.byte_bounds: list[int] = []

    def get_json(self, url: str, *, max_bytes: int) -> object:
        self.requests.append(url)
        self.json_bounds.append(max_bytes)
        payload = self.payloads[url]
        if isinstance(payload, Exception):
            raise payload
        size = len(json.dumps(payload).encode("utf-8"))
        if size > max_bytes:
            raise HttpError(f"response exceeded {max_bytes} bytes: {url}")
        return payload

    def get_bytes(self, url: str, *, max_bytes: int) -> bytes:
        self.requests.append(url)
        self.byte_bounds.append(max_bytes)
        payload = self.payloads[url]
        if isinstance(payload, Exception):
            raise payload
        assert isinstance(payload, bytes)
        if len(payload) > max_bytes:
            raise HttpError(f"response exceeded {max_bytes} bytes: {url}")
        return payload


class NpmRecipeTest(unittest.TestCase):
    """Only a reviewed recipe may declare the npm distribution."""

    def _recipe(self, **overrides: object) -> Recipe:
        fields: dict[str, object] = {
            "name": "opencode-npm-test",
            "upstream": "opencode",
            "repo": "anomalyco/opencode",
            "tag_prefix": "v",
            "license_id": "MIT",
            "license_path": "LICENSE",
            "source": "npm",
            "asset": "cli-linux-{triple}-{version}.tgz",
            "member": "package/bin/opencode",
            "binary": "opencode",
            "triples": MappingProxyType({"amd64": "x64", "arm64": "arm64"}),
            "npm_package": WRAPPER,
            "npm_packages": MappingProxyType(dict(PLATFORMS)),
        }
        fields.update(overrides)
        return Recipe(**fields)  # type: ignore[arg-type]

    def test_a_reviewed_npm_recipe_is_accepted(self) -> None:
        recipe = self._recipe()
        self.assertEqual(recipe.source, "npm")
        self.assertEqual(recipe.npm_package, WRAPPER)

    def test_a_malformed_scoped_name_is_refused(self) -> None:
        for package in ("", "@", "@/cli", "@scope/", "UPPER", "with space", "@scope/pkg!"):
            with self.subTest(package=package):
                with self.assertRaises(ValueError):
                    self._recipe(npm_package=package)

    def test_a_platform_package_for_another_architecture_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self._recipe(
                npm_packages=MappingProxyType({"amd64": PLATFORMS["amd64"]})
            )
        with self.assertRaises(ValueError):
            self._recipe(
                npm_packages=MappingProxyType(
                    {"amd64": PLATFORMS["amd64"], "riscv64": "@opencode/cli-linux-riscv64"}
                )
            )

    def test_npm_packages_without_an_npm_source_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            self._recipe(source="github")

    def test_a_negative_or_boolean_ceiling_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self._recipe(max_archive_bytes=-1)
        with self.assertRaises(ValueError):
            self._recipe(max_binary_bytes=True)

    def test_an_unreviewed_license_source_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self._recipe(license_source="sourcehut")

    def test_the_catalog_serves_the_reviewed_npm_settings(self) -> None:
        with npm_recipe():
            self.assertEqual(npm_package_for(NPM_RECIPE.name), WRAPPER)
            self.assertEqual(
                npm_platform_package_for(NPM_RECIPE.name, "amd64"),
                PLATFORMS["amd64"],
            )
            self.assertEqual(
                license_source_for(NPM_RECIPE.name), "github"
            )
            self.assertEqual(max_archive_bytes_for(NPM_RECIPE.name), 128 * 1024 * 1024)
            self.assertEqual(max_binary_bytes_for(NPM_RECIPE.name), 256 * 1024 * 1024)


class NpmEndpointTest(unittest.TestCase):
    """The registry endpoints are built from the reviewed name alone."""

    def test_a_scoped_package_has_one_canonical_endpoint(self) -> None:
        self.assertEqual(
            npm_package_endpoint(WRAPPER),
            "https://registry.npmjs.org/@opencode%2Fcli",
        )
        self.assertEqual(
            npm_version_endpoint(WRAPPER, "2.1.3"),
            "https://registry.npmjs.org/@opencode%2Fcli/2.1.3",
        )

    def test_an_unscoped_package_has_one_canonical_endpoint(self) -> None:
        self.assertEqual(
            npm_package_endpoint("opencode"),
            "https://registry.npmjs.org/opencode",
        )

    def test_the_tarball_url_is_built_from_the_scope_and_version(self) -> None:
        self.assertEqual(
            npm_tarball_url("@opencode/cli-linux-x64", "2.1.3"),
            "https://registry.npmjs.org/@opencode/cli-linux-x64/-/"
            "cli-linux-x64-2.1.3.tgz",
        )

    def test_a_malformed_package_name_builds_no_endpoint(self) -> None:
        for name in ("", "@", "@scope/", "UPPER", "with space"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    npm_package_endpoint(name)

    def test_a_malformed_version_builds_no_endpoint(self) -> None:
        for version in ("2.1", "v2.1.3", "2.1.3-rc.1", "latest"):
            with self.subTest(version=version):
                with self.assertRaises(ValueError):
                    npm_version_endpoint(WRAPPER, version)


class NpmResolutionTest(unittest.TestCase):
    """A reviewed release resolves to canonical URLs and verified digests."""

    def test_a_reviewed_release_resolves_to_its_canonical_urls(self) -> None:
        client = FakeNpmClient(npm_payloads())
        release = discover(client, wrapper=WRAPPER, platforms=PLATFORMS)

        self.assertEqual(release.version, VERSION)
        self.assertEqual(release.published_at, PUBLISHED_AT)
        self.assertEqual([asset.arch for asset in release.assets], ["amd64", "arm64"])
        for arch, platform in PLATFORMS.items():
            asset = next(a for a in release.assets if a.arch == arch)
            self.assertEqual(asset.package, platform)
            self.assertEqual(asset.name, npm_tarball_name(platform, VERSION))
            self.assertEqual(asset.url, npm_tarball_url(platform, VERSION))
            self.assertEqual(
                asset.sha256, hashlib.sha256(ARCHIVES[platform]).hexdigest()
            )
            self.assertEqual(asset.integrity, integrity_of(ARCHIVES[platform]))

    def test_only_the_registry_is_consulted(self) -> None:
        client = FakeNpmClient(npm_payloads())
        discover(client, wrapper=WRAPPER, platforms=PLATFORMS)

        self.assertTrue(client.requests)
        for url in client.requests:
            self.assertTrue(url.startswith("https://registry.npmjs.org/"), url)
        self.assertNotIn("api.github.com", " ".join(client.requests))

    def test_the_metadata_and_archive_reads_stay_bounded(self) -> None:
        client = FakeNpmClient(npm_payloads())
        discover(client, wrapper=WRAPPER, platforms=PLATFORMS)

        self.assertEqual(set(client.json_bounds), {MAX_METADATA_BYTES})
        self.assertEqual(set(client.byte_bounds), {MAX_ARCHIVE_BYTES})
        self.assertEqual(MAX_METADATA_BYTES, 8 * 1024 * 1024)
        self.assertEqual(MAX_ARCHIVE_BYTES, 128 * 1024 * 1024)

    def test_a_reviewed_ceiling_bounds_the_archive_read(self) -> None:
        client = FakeNpmClient(npm_payloads())
        discover(
            client, wrapper=WRAPPER, platforms=PLATFORMS, max_archive_bytes=1024
        )

        self.assertEqual(set(client.byte_bounds), {1024})

    def test_an_oversized_archive_is_refused(self) -> None:
        client = FakeNpmClient(npm_payloads())
        with self.assertRaises(HttpError):
            discover(
                client, wrapper=WRAPPER, platforms=PLATFORMS, max_archive_bytes=4
            )

    def test_metadata_over_the_bound_is_refused(self) -> None:
        client = FakeNpmClient(npm_payloads())
        with mock.patch("scripts.upstream_npm.MAX_METADATA_BYTES", 4):
            with self.assertRaises(HttpError):
                discover(client, wrapper=WRAPPER, platforms=PLATFORMS)

    def test_a_missing_publication_time_stays_unknown(self) -> None:
        client = FakeNpmClient(
            npm_payloads(wrapper=wrapper_packument(published_at=None))
        )
        release = discover(client, wrapper=WRAPPER, platforms=PLATFORMS)

        self.assertEqual(release.published_at, "")

    def test_a_malformed_publication_time_stops_discovery(self) -> None:
        client = FakeNpmClient(
            npm_payloads(wrapper=wrapper_packument(published_at="yesterday"))
        )
        with self.assertRaises(ValueError):
            discover(client, wrapper=WRAPPER, platforms=PLATFORMS)


class NpmSelectionTest(unittest.TestCase):
    """Only an exact stable version of a reviewed line may be selected."""

    def test_a_newer_major_line_is_refused(self) -> None:
        client = FakeNpmClient(
            npm_payloads(wrapper=wrapper_packument(latest="3.0.0"))
        )
        with self.assertRaises(ValueError) as ctx:
            discover(client, wrapper=WRAPPER, platforms=PLATFORMS)
        self.assertIn("reviewed", str(ctx.exception))

    def test_a_prerelease_or_a_range_is_refused(self) -> None:
        for latest in ("2.1.3-rc.1", "^2.1.3", "2.1", "2.1.3+build", "v2.1.3"):
            with self.subTest(latest=latest):
                with self.assertRaises(ValueError):
                    select_version(
                        wrapper_packument(latest=latest), wrapper=WRAPPER
                    )

    def test_a_missing_latest_tag_is_refused(self) -> None:
        payload = wrapper_packument()
        payload.pop("dist-tags")
        with self.assertRaises(ValueError):
            select_version(payload, wrapper=WRAPPER)

    def test_a_wrapper_that_names_another_package_is_refused(self) -> None:
        client = FakeNpmClient(
            npm_payloads(wrapper=wrapper_packument(name="@opencode/other"))
        )
        with self.assertRaises(ValueError):
            discover(client, wrapper=WRAPPER, platforms=PLATFORMS)


class NpmPlatformTest(unittest.TestCase):
    """Every reviewed platform package must agree with the wrapper."""

    def test_a_wrapper_that_pins_a_range_is_refused(self) -> None:
        client = FakeNpmClient(
            npm_payloads(
                wrapper=wrapper_packument(
                    dependencies={
                        PLATFORMS["amd64"]: f"^{VERSION}",
                        PLATFORMS["arm64"]: VERSION,
                    }
                )
            )
        )
        with self.assertRaises(ValueError):
            discover(client, wrapper=WRAPPER, platforms=PLATFORMS)

    def test_a_wrapper_that_omits_a_platform_is_refused(self) -> None:
        client = FakeNpmClient(
            npm_payloads(
                wrapper=wrapper_packument(
                    dependencies={PLATFORMS["amd64"]: VERSION}
                )
            )
        )
        with self.assertRaises(ValueError):
            discover(client, wrapper=WRAPPER, platforms=PLATFORMS)

    def test_a_platform_metadata_that_names_another_version_is_refused(self) -> None:
        client = FakeNpmClient(
            npm_payloads(
                platforms={
                    "amd64": platform_metadata(PLATFORMS["amd64"], version="2.1.4")
                }
            )
        )
        with self.assertRaises(ValueError):
            discover(client, wrapper=WRAPPER, platforms=PLATFORMS)

    def test_a_platform_metadata_that_names_another_package_is_refused(self) -> None:
        client = FakeNpmClient(
            npm_payloads(
                platforms={
                    "amd64": platform_metadata(
                        PLATFORMS["amd64"], name="@opencode/cli-linux-x86"
                    )
                }
            )
        )
        with self.assertRaises(ValueError):
            discover(client, wrapper=WRAPPER, platforms=PLATFORMS)

    def test_a_deprecated_release_is_refused(self) -> None:
        client = FakeNpmClient(
            npm_payloads(
                platforms={
                    "amd64": platform_metadata(
                        PLATFORMS["amd64"], deprecated="use another package"
                    )
                }
            )
        )
        with self.assertRaises(ValueError) as ctx:
            discover(client, wrapper=WRAPPER, platforms=PLATFORMS)
        self.assertIn("deprecated", str(ctx.exception))

    def test_a_deprecated_wrapper_is_refused_before_any_download(self) -> None:
        wrapper = wrapper_packument()
        wrapper["versions"][VERSION]["deprecated"] = "withdrawn release"
        client = FakeNpmClient(npm_payloads(wrapper=wrapper))

        with self.assertRaises(ValueError) as ctx:
            discover(client, wrapper=WRAPPER, platforms=PLATFORMS)

        self.assertIn("wrapper", str(ctx.exception))
        self.assertIn("deprecated", str(ctx.exception))
        self.assertEqual(client.requests, [npm_package_endpoint(WRAPPER)])
        self.assertEqual(client.byte_bounds, [])

    def test_a_tarball_from_another_host_is_refused(self) -> None:
        client = FakeNpmClient(
            npm_payloads(
                platforms={
                    "amd64": platform_metadata(
                        PLATFORMS["amd64"],
                        tarball="https://evil.example/cli-linux-x64-2.1.3.tgz",
                    )
                }
            )
        )
        with self.assertRaises(ValueError):
            discover(client, wrapper=WRAPPER, platforms=PLATFORMS)

    def test_a_tarball_for_another_version_is_refused(self) -> None:
        client = FakeNpmClient(
            npm_payloads(
                platforms={
                    "amd64": platform_metadata(
                        PLATFORMS["amd64"],
                        tarball=npm_tarball_url(PLATFORMS["amd64"], "2.1.4"),
                    )
                }
            )
        )
        with self.assertRaises(ValueError):
            discover(client, wrapper=WRAPPER, platforms=PLATFORMS)


class NpmIntegrityTest(unittest.TestCase):
    """The archive bytes must match the registry's own integrity digest."""

    def test_a_matching_digest_yields_the_sha256_of_the_bytes(self) -> None:
        payload = b"archive bytes"
        digest = verify_integrity(payload, integrity_of(payload))

        self.assertEqual(digest, hashlib.sha256(payload).hexdigest())

    def test_a_mismatching_digest_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            verify_integrity(b"archive bytes", integrity_of(b"other bytes"))

    def test_a_missing_or_malformed_digest_is_refused(self) -> None:
        for digest in (
            None,
            "",
            "sha256-" + "0" * 64,
            "sha512-@@@",
            "sha512-" + base64.b64encode(b"short").decode(),
            "sha512-" + base64.b64encode(hashlib.sha512(b"x").digest()).decode() + "=",
        ):
            with self.subTest(digest=digest):
                with self.assertRaises(ValueError):
                    verify_integrity(b"archive bytes", digest)

    def test_an_archive_that_does_not_match_its_digest_is_refused(self) -> None:
        client = FakeNpmClient(
            npm_payloads(
                platforms={
                    "amd64": platform_metadata(
                        PLATFORMS["amd64"], integrity=integrity_of(b"other bytes")
                    )
                }
            )
        )
        with self.assertRaises(ValueError) as ctx:
            discover(client, wrapper=WRAPPER, platforms=PLATFORMS)
        self.assertIn("integrity", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
