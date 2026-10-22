"""The fd-find recipe: official musl tarballs installing only fdfind.

Upstream publishes the binary as `fd`, but Debian already has an unrelated
`fd` command, so the package installs it as `fdfind` while the archive member
stays upstream's. Only the reviewed repository, tag prefix, asset names,
archive member, installed binary, license sources, and dependencies are pinned
here; the version and digests come from the manifest and discovery.
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
    extra_license_paths_for,
    extra_license_urls_for,
    license_path_for,
    repo_of,
    smoke_args,
    source_for,
    tag_for,
    triple_for,
)
from scripts.deb import build_deb, stage_package
from scripts.manifest import load_manifest
from scripts.package_binaries import extract_binaries
from scripts.upstream import discover, latest_stable
from scripts.upstream_assets import license_url, release_download_url, releases_api_url
from tests.helpers import write_fake_archive

REPO_ROOT = Path(__file__).resolve().parent.parent

VERSION = "10.5.0"
TAG = "v10.5.0"
REPO = "sharkdp/fd"
AMD64_ASSET = "fd-v10.5.0-x86_64-unknown-linux-musl.tar.gz"
ARM64_ASSET = "fd-v10.5.0-aarch64-unknown-linux-musl.tar.gz"
AMD64_SHA256 = "761c72dc8e120d85b22292063be8a796e2eeb20eb3e4f38b8fa2343ccf3514a7"
ARM64_SHA256 = "d76c4317f7d5dba69f8a2a15856c90c777e7f0dd4e85f0de8c76de6992c374d4"
LICENSE_SHA256 = "322cfc7aa0c774d0eca3b2610f1d414de3ddbd7d8dd4b9dea941a13a6eb07455"
APACHE_SHA256 = "73c83c60d817e7df1943cb3f0af81e4939a8352c9a96c2fd00451b1116fa635c"


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
    payloads: dict[str, object] = {
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
        }
    }
    vendor = REPO_ROOT / "packaging" / "licenses" / "fd-find"
    for source, path in (
        (REPO_ROOT / "packaging" / "licenses" / "fd-find" / "LICENSE.txt", "LICENSE-MIT"),
        (vendor / "LICENSE-APACHE", "LICENSE-APACHE"),
    ):
        payloads[license_url(REPO, TAG, path)] = source.read_bytes()
    return payloads


class FdFindRecipeTest(unittest.TestCase):
    def test_the_package_is_registered_for_both_architectures(self) -> None:
        self.assertIn("fd-find", KNOWN_PACKAGES)
        self.assertEqual(arches_for("fd-find"), ("amd64", "arm64"))
        self.assertFalse(exact_tag_release_for("fd-find"))

    def test_upstream_naming_and_tag(self) -> None:
        self.assertEqual(repo_of("fd-find"), REPO)
        self.assertEqual(source_for("fd-find"), "github")
        self.assertEqual(tag_for("fd-find", VERSION), TAG)

    def test_assets_are_the_official_musl_tarballs(self) -> None:
        self.assertEqual(asset_name("fd-find", VERSION, "amd64"), AMD64_ASSET)
        self.assertEqual(asset_name("fd-find", VERSION, "arm64"), ARM64_ASSET)
        self.assertEqual(triple_for("fd-find", "amd64"), "x86_64-unknown-linux-musl")
        self.assertEqual(triple_for("fd-find", "arm64"), "aarch64-unknown-linux-musl")
        self.assertEqual(archive_suffix_for("fd-find", VERSION, "amd64"), ".tar.gz")

    def test_the_archive_member_is_fd_but_the_package_installs_fdfind(self) -> None:
        self.assertEqual(
            archive_member("fd-find", VERSION, "amd64"),
            "fd-v10.5.0-x86_64-unknown-linux-musl/fd",
        )
        self.assertEqual(
            archive_member("fd-find", VERSION, "arm64"),
            "fd-v10.5.0-aarch64-unknown-linux-musl/fd",
        )
        self.assertEqual(binary_name("fd-find"), "fdfind")

    def test_the_package_is_static_and_uses_the_default_smoke_flag(self) -> None:
        self.assertEqual(depends_for("fd-find", "amd64"), "")
        self.assertEqual(depends_for("fd-find", "arm64"), "")
        self.assertEqual(smoke_args("fd-find"), ("--version",))

    def test_the_license_is_the_reviewed_dual_license(self) -> None:
        self.assertEqual(expected_license_for("fd-find"), "MIT OR Apache-2.0")
        self.assertEqual(license_path_for("fd-find"), "LICENSE-MIT")
        self.assertEqual(extra_license_paths_for("fd-find"), ("LICENSE-APACHE",))
        self.assertEqual(
            extra_license_urls_for("fd-find", VERSION),
            (f"https://raw.githubusercontent.com/{REPO}/{TAG}/LICENSE-APACHE",),
        )

    def test_the_vendored_licenses_are_the_upstream_bytes(self) -> None:
        vendor = REPO_ROOT / "packaging" / "licenses" / "fd-find"
        self.assertEqual(
            hashlib.sha256((vendor / "LICENSE.txt").read_bytes()).hexdigest(),
            LICENSE_SHA256,
        )
        self.assertEqual(
            hashlib.sha256((vendor / "LICENSE-APACHE").read_bytes()).hexdigest(),
            APACHE_SHA256,
        )

    def test_discovery_selects_both_official_assets_and_their_licenses(self) -> None:
        _, selected_tag, assets = latest_stable(FakeHttp(discovery_payloads()), "fd-find")
        self.assertEqual(selected_tag, TAG)
        self.assertEqual(
            {arch: asset.sha256 for arch, asset in assets.items()},
            {"amd64": AMD64_SHA256, "arm64": ARM64_SHA256},
        )
        candidate = discover(FakeHttp(discovery_payloads()), "fd-find")
        self.assertEqual(candidate.version, VERSION)
        self.assertEqual(candidate.license_url, license_url(REPO, TAG, "LICENSE-MIT"))
        self.assertEqual(
            candidate.extra_license_urls,
            (license_url(REPO, TAG, "LICENSE-APACHE"),),
        )

    def test_the_committed_manifest_carries_the_reviewed_entry(self) -> None:
        manifest = load_manifest()
        pkg = next(p for p in manifest.packages if p.name == "fd-find")
        self.assertEqual(pkg.version, VERSION)
        self.assertEqual(pkg.license, "MIT OR Apache-2.0")
        self.assertEqual(pkg.license_url, f"https://raw.githubusercontent.com/{REPO}/{TAG}/LICENSE-MIT")
        self.assertEqual(set(pkg.targets), {"amd64", "arm64"})
        for arch, name, digest in (
            ("amd64", AMD64_ASSET, AMD64_SHA256),
            ("arm64", ARM64_ASSET, ARM64_SHA256),
        ):
            with self.subTest(arch=arch):
                target = pkg.targets[arch]
                self.assertEqual(target.url, release_download_url(REPO, TAG, name))
                self.assertEqual(target.sha256, digest)
                self.assertEqual((target.archive_member, target.binary), (archive_member("fd-find", VERSION, arch), "fdfind"))
                self.assertEqual(target.depends, "")


class FdFindPackageTest(unittest.TestCase):
    def test_the_deb_contains_fdfind_and_no_fd(self) -> None:
        manifest = load_manifest()
        pkg = next(p for p in manifest.packages if p.name == "fd-find")
        target = pkg.targets["amd64"]
        payload = b"\x7fELF-fake-fd"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = write_fake_archive(
                root / AMD64_ASSET, member=target.archive_member, payload=payload,
            )
            sources = extract_binaries(archive, target, root / "extract")
            self.assertEqual(set(sources), {"fdfind"})
            self.assertEqual(sources["fdfind"].read_bytes(), payload)
            stage = stage_package(
                pkg=pkg, arch="amd64", manifest=manifest,
                binary_file=sources["fdfind"], stage_root=root / "stages",
            )
            self.assertTrue((stage / "usr" / "bin" / "fdfind").exists())
            self.assertFalse((stage / "usr" / "bin" / "fd").exists())
            copyright = (stage / "usr" / "share" / "doc" / "fd-find" / "copyright").read_text()
            self.assertIn("License: MIT OR Apache-2.0", copyright)
            self.assertIn("MIT License", copyright)
            self.assertIn("Apache License", copyright)
            deb = root / "fd-find.deb"
            build_deb(stage, deb)
            listing = subprocess.run(
                ["dpkg-deb", "-c", str(deb)], text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(listing.returncode, 0, msg=listing.stderr)
            self.assertIn("./usr/bin/fdfind", listing.stdout)
            self.assertNotIn("./usr/bin/fd\n", listing.stdout)


if __name__ == "__main__":
    unittest.main()
