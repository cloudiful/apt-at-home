"""The chezmoi recipe: official canonical Linux tarballs for both arches.

Upstream publishes canonical tarballs named with the Debian architecture. The
default amd64 archive is glibc-linked while the arm64 archive is static. Only
the reviewed repository, tag prefix, asset names, archive member, binary,
license source, and per-architecture dependencies are pinned here; the version
and digests come from the manifest and discovery.
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
)
from scripts.deb import build_deb, stage_package
from scripts.manifest import load_manifest
from scripts.package_binaries import extract_binaries
from scripts.upstream import discover, latest_stable
from scripts.upstream_assets import license_url, release_download_url, releases_api_url
from tests.helpers import write_fake_archive

REPO_ROOT = Path(__file__).resolve().parent.parent

VERSION = "2.73.3"
TAG = "v2.73.3"
REPO = "twpayne/chezmoi"
AMD64_ASSET = "chezmoi_2.73.3_linux_amd64.tar.gz"
ARM64_ASSET = "chezmoi_2.73.3_linux_arm64.tar.gz"
AMD64_SHA256 = "6e5827598cbfc02d132b04f95b5ea10efb5b79230e2a4a0e52df4af144a8ee9f"
ARM64_SHA256 = "c9e6a6e71eb5291018e161a8426d7c313cd2e8553c2ca4029511fec2a33bc28c"
AMD64_DEPENDS = "libc6 (>= 2.34)"
LICENSE_SHA256 = "6ce2375d89522ac7a1417320c8406886bc0ecb984baf41e3c27b8a07a81a30b2"


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
            REPO_ROOT / "packaging" / "licenses" / "chezmoi" / "LICENSE.txt"
        ).read_bytes(),
    }


class ChezmoiRecipeTest(unittest.TestCase):
    def test_the_package_is_registered_for_both_architectures(self) -> None:
        self.assertIn("chezmoi", KNOWN_PACKAGES)
        self.assertEqual(arches_for("chezmoi"), ("amd64", "arm64"))
        self.assertFalse(exact_tag_release_for("chezmoi"))

    def test_upstream_naming_and_tag(self) -> None:
        self.assertEqual(repo_of("chezmoi"), REPO)
        self.assertEqual(tag_for("chezmoi", VERSION), TAG)

    def test_assets_are_the_canonical_tarballs(self) -> None:
        self.assertEqual(asset_name("chezmoi", VERSION, "amd64"), AMD64_ASSET)
        self.assertEqual(asset_name("chezmoi", VERSION, "arm64"), ARM64_ASSET)
        self.assertEqual(archive_suffix_for("chezmoi", VERSION, "amd64"), ".tar.gz")

    def test_the_member_and_installed_binary_are_flat_chezmoi(self) -> None:
        self.assertEqual(archive_member("chezmoi", VERSION, "amd64"), "chezmoi")
        self.assertEqual(archive_member("chezmoi", VERSION, "arm64"), "chezmoi")
        self.assertEqual(binary_name("chezmoi"), "chezmoi")
        self.assertEqual(smoke_args("chezmoi"), ("--version",))

    def test_only_the_amd64_build_declares_glibc(self) -> None:
        self.assertEqual(depends_for("chezmoi", "amd64"), AMD64_DEPENDS)
        self.assertEqual(depends_for("chezmoi", "arm64"), "")

    def test_the_license_is_the_upstream_mit_text(self) -> None:
        self.assertEqual(expected_license_for("chezmoi"), "MIT")
        self.assertEqual(license_path_for("chezmoi"), "LICENSE")
        vendor = REPO_ROOT / "packaging" / "licenses" / "chezmoi" / "LICENSE.txt"
        self.assertEqual(hashlib.sha256(vendor.read_bytes()).hexdigest(), LICENSE_SHA256)

    def test_discovery_selects_both_official_assets_and_their_license(self) -> None:
        _, selected_tag, assets = latest_stable(FakeHttp(discovery_payloads()), "chezmoi")
        self.assertEqual(selected_tag, TAG)
        self.assertEqual(
            {arch: asset.sha256 for arch, asset in assets.items()},
            {"amd64": AMD64_SHA256, "arm64": ARM64_SHA256},
        )
        candidate = discover(FakeHttp(discovery_payloads()), "chezmoi")
        self.assertEqual(candidate.version, VERSION)
        self.assertEqual(candidate.license_url, license_url(REPO, TAG, "LICENSE"))

    def test_the_committed_manifest_carries_the_reviewed_entry(self) -> None:
        manifest = load_manifest()
        pkg = next(p for p in manifest.packages if p.name == "chezmoi")
        self.assertEqual(pkg.version, VERSION)
        self.assertEqual(pkg.license, "MIT")
        self.assertEqual(pkg.license_url, f"https://raw.githubusercontent.com/{REPO}/{TAG}/LICENSE")
        self.assertEqual(set(pkg.targets), {"amd64", "arm64"})
        for arch, name, digest, depends in (
            ("amd64", AMD64_ASSET, AMD64_SHA256, AMD64_DEPENDS),
            ("arm64", ARM64_ASSET, ARM64_SHA256, ""),
        ):
            with self.subTest(arch=arch):
                target = pkg.targets[arch]
                self.assertEqual(target.url, release_download_url(REPO, TAG, name))
                self.assertEqual(target.sha256, digest)
                self.assertEqual((target.archive_member, target.binary), ("chezmoi", "chezmoi"))
                self.assertEqual(target.depends, depends)


class ChezmoiPackageTest(unittest.TestCase):
    def test_the_tarball_member_becomes_an_executable_package(self) -> None:
        manifest = load_manifest()
        pkg = next(p for p in manifest.packages if p.name == "chezmoi")
        target = pkg.targets["amd64"]
        payload = b"\x7fELF-fake-chezmoi"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = write_fake_archive(
                root / AMD64_ASSET, member=target.archive_member, payload=payload,
            )
            sources = extract_binaries(archive, target, root / "extract")
            self.assertEqual(set(sources), {"chezmoi"})
            stage = stage_package(
                pkg=pkg, arch="amd64", manifest=manifest,
                binary_file=sources["chezmoi"], stage_root=root / "stages",
            )
            self.assertTrue((stage / "usr" / "bin" / "chezmoi").exists())
            control = (stage / "DEBIAN" / "control").read_text()
            self.assertIn(f"Depends: {AMD64_DEPENDS}", control)
            deb = root / "chezmoi.deb"
            build_deb(stage, deb)
            listing = subprocess.run(
                ["dpkg-deb", "-c", str(deb)], text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(listing.returncode, 0, msg=listing.stderr)
            self.assertIn("./usr/bin/chezmoi", listing.stdout)


if __name__ == "__main__":
    unittest.main()
