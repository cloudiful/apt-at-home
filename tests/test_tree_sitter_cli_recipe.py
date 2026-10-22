"""The tree-sitter-cli recipe: official Linux ZIP releases for both arches.

Upstream publishes the CLI as a flat `tree-sitter` member inside an
architecture-specific ZIP. Debian installs it under the same command name.
Only the reviewed repository, tag prefix, asset names, archive member, binary,
license source, and glibc floor are pinned here; the version and digests come
from the manifest and discovery.
"""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.catalog import (
    KNOWN_PACKAGES,
    arches_for,
    archive_member,
    archive_suffix_for,
    asset_name,
    binary_name,
    depends_for,
    exact_tag_release_for,
    expected_license_for,
    license_path_for,
    repo_of,
    smoke_args,
    tag_for,
    triple_for,
)
from scripts.deb import build_deb, stage_package
from scripts.manifest import load_manifest
from scripts.package_binaries import extract_binaries
from scripts.upstream import discover, latest_stable
from scripts.upstream_assets import license_url, release_download_url, releases_api_url
from tests.helpers import write_fake_zip

REPO_ROOT = Path(__file__).resolve().parent.parent

VERSION = "0.27.1"
TAG = "v0.27.1"
REPO = "tree-sitter/tree-sitter"
AMD64_ASSET = "tree-sitter-cli-linux-x64.zip"
ARM64_ASSET = "tree-sitter-cli-linux-arm64.zip"
AMD64_SHA256 = "c7e686aec16ba17053c2e6fd87600ccb10cf8ba3d055756b17e756d538e11114"
ARM64_SHA256 = "3c0d0113cae3fea36f2336c271cbdacbcdb349edb006319155652b9386803c55"
DEPENDS = "libc6 (>= 2.39), libgcc-s1"
LICENSE_SHA256 = "c5cfb43042b6b72045f4ba997834d0a7786d2793d91680868b5815b39f14fc78"


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
    names = {"amd64": AMD64_ASSET, "arm64": ARM64_ASSET}
    digests = {"amd64": AMD64_SHA256, "arm64": ARM64_SHA256}
    return {
        releases_api_url(REPO): {
            "tag_name": TAG,
            "draft": False,
            "prerelease": False,
            "assets": [
                {
                    "name": names[arch],
                    "browser_download_url": release_download_url(REPO, TAG, names[arch]),
                    "digest": f"sha256:{digests[arch]}",
                }
                for arch in ("amd64", "arm64")
            ],
        },
        license_url(REPO, TAG, "LICENSE"): (
            REPO_ROOT / "packaging" / "licenses" / "tree-sitter-cli" / "LICENSE.txt"
        ).read_bytes(),
    }


class TreeSitterCliRecipeTest(unittest.TestCase):
    def test_the_package_is_registered_for_both_architectures(self) -> None:
        self.assertIn("tree-sitter-cli", KNOWN_PACKAGES)
        self.assertEqual(arches_for("tree-sitter-cli"), ("amd64", "arm64"))
        self.assertFalse(exact_tag_release_for("tree-sitter-cli"))

    def test_upstream_naming_and_tag(self) -> None:
        self.assertEqual(repo_of("tree-sitter-cli"), REPO)
        self.assertEqual(tag_for("tree-sitter-cli", VERSION), TAG)

    def test_assets_are_the_official_zip_releases(self) -> None:
        self.assertEqual(asset_name("tree-sitter-cli", VERSION, "amd64"), AMD64_ASSET)
        self.assertEqual(asset_name("tree-sitter-cli", VERSION, "arm64"), ARM64_ASSET)
        self.assertEqual(triple_for("tree-sitter-cli", "amd64"), "x64")
        self.assertEqual(triple_for("tree-sitter-cli", "arm64"), "arm64")
        self.assertEqual(archive_suffix_for("tree-sitter-cli", VERSION, "amd64"), ".zip")

    def test_the_member_and_installed_binary_are_flat_tree_sitter(self) -> None:
        self.assertEqual(archive_member("tree-sitter-cli", VERSION, "amd64"), "tree-sitter")
        self.assertEqual(archive_member("tree-sitter-cli", VERSION, "arm64"), "tree-sitter")
        self.assertEqual(binary_name("tree-sitter-cli"), "tree-sitter")
        self.assertEqual(smoke_args("tree-sitter-cli"), ("--version",))

    def test_both_architectures_require_the_reviewed_glibc_floor(self) -> None:
        self.assertEqual(depends_for("tree-sitter-cli", "amd64"), DEPENDS)
        self.assertEqual(depends_for("tree-sitter-cli", "arm64"), DEPENDS)

    def test_the_license_is_the_upstream_mit_text(self) -> None:
        self.assertEqual(expected_license_for("tree-sitter-cli"), "MIT")
        self.assertEqual(license_path_for("tree-sitter-cli"), "LICENSE")
        vendor = REPO_ROOT / "packaging" / "licenses" / "tree-sitter-cli" / "LICENSE.txt"
        self.assertEqual(hashlib.sha256(vendor.read_bytes()).hexdigest(), LICENSE_SHA256)

    def test_discovery_selects_both_official_assets_and_their_license(self) -> None:
        _, selected_tag, assets = latest_stable(FakeHttp(discovery_payloads()), "tree-sitter-cli")
        self.assertEqual(selected_tag, TAG)
        self.assertEqual(
            {arch: asset.sha256 for arch, asset in assets.items()},
            {"amd64": AMD64_SHA256, "arm64": ARM64_SHA256},
        )
        candidate = discover(FakeHttp(discovery_payloads()), "tree-sitter-cli")
        self.assertEqual(candidate.version, VERSION)
        self.assertEqual(candidate.license_url, license_url(REPO, TAG, "LICENSE"))

    def test_the_committed_manifest_carries_the_reviewed_entry(self) -> None:
        manifest = load_manifest()
        pkg = next(p for p in manifest.packages if p.name == "tree-sitter-cli")
        self.assertEqual(pkg.version, VERSION)
        self.assertEqual(pkg.license, "MIT")
        self.assertEqual(pkg.license_url, f"https://raw.githubusercontent.com/{REPO}/{TAG}/LICENSE")
        self.assertEqual(set(pkg.targets), {"amd64", "arm64"})
        for arch, name, digest in (
            ("amd64", AMD64_ASSET, AMD64_SHA256),
            ("arm64", ARM64_ASSET, ARM64_SHA256),
        ):
            with self.subTest(arch=arch):
                target = pkg.targets[arch]
                self.assertEqual(target.url, release_download_url(REPO, TAG, name))
                self.assertEqual(target.sha256, digest)
                self.assertEqual((target.archive_member, target.binary), ("tree-sitter", "tree-sitter"))
                self.assertEqual(target.depends, DEPENDS)


class TreeSitterCliPackageTest(unittest.TestCase):
    def test_the_zip_member_becomes_an_executable_package(self) -> None:
        manifest = load_manifest()
        pkg = next(p for p in manifest.packages if p.name == "tree-sitter-cli")
        target = pkg.targets["amd64"]
        payload = b"\x7fELF-fake-tree-sitter"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = write_fake_zip(
                root / AMD64_ASSET, member=target.archive_member, payload=payload,
            )
            sources = extract_binaries(archive, target, root / "extract")
            self.assertEqual(set(sources), {"tree-sitter"})
            stage = stage_package(
                pkg=pkg, arch="amd64", manifest=manifest,
                binary_file=sources["tree-sitter"], stage_root=root / "stages",
            )
            self.assertTrue((stage / "usr" / "bin" / "tree-sitter").exists())
            control = (stage / "DEBIAN" / "control").read_text()
            self.assertIn(f"Depends: {DEPENDS}", control)
            deb = root / "tree-sitter-cli.deb"
            build_deb(stage, deb)
            listing = subprocess.run(
                ["dpkg-deb", "-c", str(deb)], text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(listing.returncode, 0, msg=listing.stderr)
            self.assertIn("./usr/bin/tree-sitter", listing.stdout)


if __name__ == "__main__":
    unittest.main()
