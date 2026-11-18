"""The Woodpecker recipes: an official amd64 Debian package and arm64 tarball.

Woodpecker publishes one release per version. amd64 is upstream's official
Debian package with its own installation layout; arm64 is a tarball holding the
static agent or CLI binary. Only the reviewed repository, tag prefix, asset
names, members, binary, license source, revision, and (static) dependency set
are pinned here; the version and digests come from the manifest and discovery.
"""

from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

from scripts.catalog import (
    KNOWN_PACKAGES,
    arches_for,
    archive_member,
    archive_suffix_for,
    asset_name,
    binary_dir_for,
    binary_name,
    debian_revision_for,
    depends_for,
    exact_tag_release_for,
    expected_license_for,
    license_path_for,
    official_deb_asset_name,
    official_deb_for,
    release_asset_name,
    repo_of,
    smoke_args,
    tag_for,
)
from scripts.manifest import load_manifest
from scripts.upstream import discover, latest_stable
from scripts.upstream_assets import license_url, release_download_url, releases_api_url
from scripts.woodpecker_package import PACKAGING_FILES, PACKAGING_TAG, vendored

REPO_ROOT = Path(__file__).resolve().parent.parent

VERSION = "3.20.1"
TAG = "v3.20.1"
REPO = "woodpecker-ci/woodpecker"
LICENSE_SHA256 = "a203768e1e09819b88c7671c8f4f7a368fb12dee05a57489fa0cc77245afd264"

# One reviewed asset name and digest per package and architecture.
ASSETS: dict[str, dict[str, str]] = {
    "woodpecker-agent": {
        "amd64": "woodpecker-agent_3.20.1_amd64.deb",
        "arm64": "woodpecker-agent_linux_arm64.tar.gz",
    },
    "woodpecker-cli": {
        "amd64": "woodpecker-cli_3.20.1_amd64.deb",
        "arm64": "woodpecker-cli_linux_arm64.tar.gz",
    },
}

DIGESTS: dict[str, dict[str, str]] = {
    "woodpecker-agent": {
        "amd64": "24e70c5225afcc61f3aa4fe798665406c556db642822f23366b08ad1722000d4",
        "arm64": "ac62b815257cb6b90f487366607bc918eb99221082a695fc39135496d4c49b28",
    },
    "woodpecker-cli": {
        "amd64": "e60ce30c33263529a4124f02110072117153f8e3c78e46ea6682fb0f0b21d865",
        "arm64": "683ab0d1ef7bb21e2da6695bec40f56758b5b5958d111f8049afe1199cab0b0c",
    },
}


class FakeHttp:
    def __init__(self, payloads: dict[str, object]) -> None:
        self.payloads = payloads

    def get_json(self, url: str) -> object:
        return self.payloads[url]

    def get_bytes(self, url: str, *, max_bytes: int) -> bytes:
        payload = self.payloads[url]
        assert isinstance(payload, bytes)
        if len(payload) > max_bytes:
            raise ValueError(f"response exceeded {max_bytes} bytes: {url}")
        return payload


def discovery_payloads() -> dict[str, object]:
    assets = []
    for package, names in ASSETS.items():
        for arch, name in names.items():
            assets.append({
                "name": name,
                "browser_download_url": release_download_url(REPO, TAG, name),
                "digest": f"sha256:{DIGESTS[package][arch]}",
            })
    payloads: dict[str, object] = {
        releases_api_url(REPO): {
            "tag_name": TAG,
            "draft": False,
            "prerelease": False,
            "assets": assets,
        },
        license_url(REPO, TAG, "LICENSE"): (
            REPO_ROOT / "packaging" / "licenses" / "woodpecker-agent" / "LICENSE.txt"
        ).read_bytes(),
    }
    for source_path, (relative, _digest) in PACKAGING_FILES.items():
        payloads[license_url(REPO, TAG, source_path)] = (
            REPO_ROOT / "packaging" / "woodpecker" / relative
        ).read_bytes()
    return payloads


class WoodpeckerRecipeTest(unittest.TestCase):
    def test_both_packages_are_registered_for_both_architectures(self) -> None:
        for package in ("woodpecker-agent", "woodpecker-cli"):
            with self.subTest(package=package):
                self.assertIn(package, KNOWN_PACKAGES)
                self.assertEqual(arches_for(package), ("amd64", "arm64"))
                self.assertFalse(exact_tag_release_for(package))
                self.assertEqual(debian_revision_for(package), "-2")

    def test_upstream_naming_and_tag(self) -> None:
        for package in ("woodpecker-agent", "woodpecker-cli"):
            with self.subTest(package=package):
                self.assertEqual(repo_of(package), REPO)
                self.assertEqual(tag_for(package, VERSION), TAG)

    def test_amd64_uses_the_official_deb_and_arm64_the_tarball(self) -> None:
        for package, names in ASSETS.items():
            for arch, name in names.items():
                with self.subTest(package=package, arch=arch):
                    self.assertEqual(
                        release_asset_name(package, VERSION, arch), name
                    )
                    self.assertEqual(
                        asset_name(package, VERSION, arch),
                        f"{package}_linux_{arch}.tar.gz",
                    )
                    self.assertEqual(
                        archive_suffix_for(package, VERSION, arch),
                        ".deb" if arch == "amd64" else ".tar.gz",
                    )
                    self.assertEqual(
                        official_deb_for(package, arch), arch == "amd64"
                    )
                    if arch == "amd64":
                        self.assertEqual(
                            official_deb_asset_name(package, VERSION, arch), name
                        )

    def test_the_member_and_installed_binary_are_flat_and_named_after_the_package(self) -> None:
        for package in ("woodpecker-agent", "woodpecker-cli"):
            for arch in ("amd64", "arm64"):
                with self.subTest(package=package, arch=arch):
                    self.assertEqual(archive_member(package, VERSION, arch), package)
                    self.assertEqual(binary_name(package), package)
                    self.assertEqual(binary_dir_for(package), "usr/local/bin")
                    self.assertEqual(smoke_args(package), ("--version",))

    def test_the_static_builds_declare_no_runtime_dependency(self) -> None:
        for package in ("woodpecker-agent", "woodpecker-cli"):
            for arch in ("amd64", "arm64"):
                with self.subTest(package=package, arch=arch):
                    self.assertEqual(depends_for(package, arch), "")

    def test_the_license_is_the_vendored_apache_text(self) -> None:
        for package in ("woodpecker-agent", "woodpecker-cli"):
            with self.subTest(package=package):
                self.assertEqual(expected_license_for(package), "Apache-2.0")
                self.assertEqual(license_path_for(package), "LICENSE")
                vendor = REPO_ROOT / "packaging" / "licenses" / package / "LICENSE.txt"
                self.assertEqual(
                    hashlib.sha256(vendor.read_bytes()).hexdigest(), LICENSE_SHA256
                )

    def test_the_tagged_packaging_files_are_vendored_byte_for_byte(self) -> None:
        for source_path, (relative, digest) in PACKAGING_FILES.items():
            with self.subTest(source=source_path):
                self.assertEqual(
                    hashlib.sha256(vendored(source_path)).hexdigest(), digest
                )
                self.assertTrue((REPO_ROOT / "packaging" / "woodpecker" / relative).is_file())
        self.assertEqual(PACKAGING_TAG, TAG)

    def test_discovery_selects_the_reviewed_assets_of_the_shared_release(self) -> None:
        for package, digests in DIGESTS.items():
            with self.subTest(package=package):
                _, selected_tag, assets = latest_stable(
                    FakeHttp(discovery_payloads()), package
                )
                self.assertEqual(selected_tag, TAG)
                self.assertEqual(
                    {arch: asset.sha256 for arch, asset in assets.items()},
                    {"amd64": digests["amd64"], "arm64": digests["arm64"]},
                )
                candidate = discover(FakeHttp(discovery_payloads()), package)
                self.assertEqual(candidate.version, VERSION)
                self.assertEqual(candidate.license_url, license_url(REPO, TAG, "LICENSE"))
                self.assertEqual(
                    candidate.targets["amd64"]["url"],
                    release_download_url(REPO, TAG, ASSETS[package]["amd64"]),
                )

    def test_discovery_stops_when_the_tagged_packaging_changes(self) -> None:
        payloads = discovery_payloads()
        payloads[license_url(REPO, TAG, "nfpm/woodpecker-agent.service")] = b"changed"
        with self.assertRaisesRegex(ValueError, "changed its official packaging"):
            discover(FakeHttp(payloads), "woodpecker-agent")

    def test_the_committed_manifest_carries_both_reviewed_entries(self) -> None:
        manifest = load_manifest()
        for package, digests in DIGESTS.items():
            with self.subTest(package=package):
                pkg = next(p for p in manifest.packages if p.name == package)
                self.assertEqual(pkg.version, VERSION)
                self.assertEqual(pkg.debian_revision, "-2")
                self.assertEqual(pkg.license, "Apache-2.0")
                self.assertEqual(
                    pkg.license_url,
                    f"https://raw.githubusercontent.com/{REPO}/{TAG}/LICENSE",
                )
                self.assertEqual(set(pkg.targets), {"amd64", "arm64"})
                for arch in ("amd64", "arm64"):
                    with self.subTest(arch=arch):
                        target = pkg.targets[arch]
                        self.assertEqual(
                            target.url,
                            release_download_url(REPO, TAG, ASSETS[package][arch]),
                        )
                        self.assertEqual(target.sha256, digests[arch])
                        self.assertEqual(
                            (target.archive_member, target.binary), (package, package)
                        )
                        self.assertEqual(target.depends, "")
                        self.assertEqual(target.official_deb, arch == "amd64")


if __name__ == "__main__":
    unittest.main()
