"""The fzf recipe: official static Linux tarballs for amd64 and arm64.

fzf names its assets by Debian architecture and installs the flat `fzf`
command. Only the reviewed repository, tag prefix, asset names, archive
member, binary, license source, and (static) dependency set are pinned here;
the version and digests come from the manifest and discovery.
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

VERSION = "0.74.5"
TAG = "v0.74.5"
REPO = "junegunn/fzf"
AMD64_ASSET = "fzf-0.74.5-linux_amd64.tar.gz"
ARM64_ASSET = "fzf-0.74.5-linux_arm64.tar.gz"
AMD64_SHA256 = "d47d7f882972c7d674dbbba998ffa4116e68379616ffac7695d0671e9a722c5f"
ARM64_SHA256 = "cf058001da21724a469b714a29cf4aab9cbe860d218a0d6c1b5b661efad7784c"
LICENSE_SHA256 = "a296f423c0d30ce3581435e78e7e36c5fe73984a882d8720c72e713b4593588b"


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
            REPO_ROOT / "packaging" / "licenses" / "fzf" / "LICENSE.txt"
        ).read_bytes(),
    }


class FzfRecipeTest(unittest.TestCase):
    def test_the_package_is_registered_for_both_architectures(self) -> None:
        self.assertIn("fzf", KNOWN_PACKAGES)
        self.assertEqual(arches_for("fzf"), ("amd64", "arm64"))
        self.assertFalse(exact_tag_release_for("fzf"))

    def test_upstream_naming_and_tag(self) -> None:
        self.assertEqual(repo_of("fzf"), REPO)
        self.assertEqual(tag_for("fzf", VERSION), TAG)

    def test_assets_use_the_debian_architecture_names(self) -> None:
        self.assertEqual(asset_name("fzf", VERSION, "amd64"), AMD64_ASSET)
        self.assertEqual(asset_name("fzf", VERSION, "arm64"), ARM64_ASSET)
        self.assertEqual(archive_suffix_for("fzf", VERSION, "amd64"), ".tar.gz")

    def test_the_member_and_installed_binary_are_flat_fzf(self) -> None:
        self.assertEqual(archive_member("fzf", VERSION, "amd64"), "fzf")
        self.assertEqual(archive_member("fzf", VERSION, "arm64"), "fzf")
        self.assertEqual(binary_name("fzf"), "fzf")
        self.assertEqual(smoke_args("fzf"), ("--version",))

    def test_the_static_build_declares_no_runtime_dependency(self) -> None:
        self.assertEqual(depends_for("fzf", "amd64"), "")
        self.assertEqual(depends_for("fzf", "arm64"), "")

    def test_the_license_is_the_upstream_mit_text(self) -> None:
        self.assertEqual(expected_license_for("fzf"), "MIT")
        self.assertEqual(license_path_for("fzf"), "LICENSE")
        vendor = REPO_ROOT / "packaging" / "licenses" / "fzf" / "LICENSE.txt"
        self.assertEqual(hashlib.sha256(vendor.read_bytes()).hexdigest(), LICENSE_SHA256)

    def test_discovery_selects_both_official_assets_and_their_license(self) -> None:
        _, selected_tag, assets = latest_stable(FakeHttp(discovery_payloads()), "fzf")
        self.assertEqual(selected_tag, TAG)
        self.assertEqual(
            {arch: asset.sha256 for arch, asset in assets.items()},
            {"amd64": AMD64_SHA256, "arm64": ARM64_SHA256},
        )
        candidate = discover(FakeHttp(discovery_payloads()), "fzf")
        self.assertEqual(candidate.version, VERSION)
        self.assertEqual(candidate.license_url, license_url(REPO, TAG, "LICENSE"))

    def test_the_committed_manifest_carries_the_reviewed_entry(self) -> None:
        manifest = load_manifest()
        pkg = next(p for p in manifest.packages if p.name == "fzf")
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
                self.assertEqual((target.archive_member, target.binary), ("fzf", "fzf"))
                self.assertEqual(target.depends, "")


class FzfPackageTest(unittest.TestCase):
    def test_the_tarball_member_becomes_an_executable_package(self) -> None:
        manifest = load_manifest()
        pkg = next(p for p in manifest.packages if p.name == "fzf")
        target = pkg.targets["amd64"]
        payload = b"\x7fELF-fake-fzf"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = write_fake_archive(
                root / AMD64_ASSET, member=target.archive_member, payload=payload,
            )
            sources = extract_binaries(archive, target, root / "extract")
            self.assertEqual(set(sources), {"fzf"})
            stage = stage_package(
                pkg=pkg, arch="amd64", manifest=manifest,
                binary_file=sources["fzf"], stage_root=root / "stages",
            )
            self.assertTrue((stage / "usr" / "bin" / "fzf").exists())
            deb = root / "fzf.deb"
            build_deb(stage, deb)
            listing = subprocess.run(
                ["dpkg-deb", "-c", str(deb)], text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(listing.returncode, 0, msg=listing.stderr)
            self.assertIn("./usr/bin/fzf", listing.stdout)
            self.assertIn("./usr/share/doc/fzf/copyright", listing.stdout)


if __name__ == "__main__":
    unittest.main()
