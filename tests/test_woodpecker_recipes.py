"""The Woodpecker recipes: official static Linux tarballs for amd64 and arm64.

Woodpecker publishes one release per version that carries the agent and the
CLI as separate archives, each named after its own command and the Debian
architecture, and each holding one flat static ELF. Only the reviewed
repository, tag prefix, asset names, archive member, binary, license source,
and (static) dependency set are pinned here; the version and digests come
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
)
from scripts.deb import build_deb, stage_package
from scripts.manifest import load_manifest
from scripts.package_binaries import extract_binaries
from scripts.upstream import discover, latest_stable
from scripts.upstream_assets import license_url, release_download_url, releases_api_url
from tests.helpers import write_fake_archive

REPO_ROOT = Path(__file__).resolve().parent.parent

VERSION = "3.20.1"
TAG = "v3.20.1"
REPO = "woodpecker-ci/woodpecker"
LICENSE_SHA256 = "a203768e1e09819b88c7671c8f4f7a368fb12dee05a57489fa0cc77245afd264"

# One reviewed asset name and digest per package and architecture.
ASSETS: dict[str, dict[str, str]] = {
    "woodpecker-agent": {
        "amd64": "woodpecker-agent_linux_amd64.tar.gz",
        "arm64": "woodpecker-agent_linux_arm64.tar.gz",
    },
    "woodpecker-cli": {
        "amd64": "woodpecker-cli_linux_amd64.tar.gz",
        "arm64": "woodpecker-cli_linux_arm64.tar.gz",
    },
}

DIGESTS: dict[str, dict[str, str]] = {
    "woodpecker-agent": {
        "amd64": "cb8cf9028cde5a8eaf8612e9aec59a3e8636c1b2ab8d339cfafb770ebba5e3d8",
        "arm64": "ac62b815257cb6b90f487366607bc918eb99221082a695fc39135496d4c49b28",
    },
    "woodpecker-cli": {
        "amd64": "5b74ff1808af35b57bd5c2b528b7fdff24f5f69ad327715c1797f764da79dc13",
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
    return {
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


class WoodpeckerRecipeTest(unittest.TestCase):
    def test_both_packages_are_registered_for_both_architectures(self) -> None:
        for package in ("woodpecker-agent", "woodpecker-cli"):
            with self.subTest(package=package):
                self.assertIn(package, KNOWN_PACKAGES)
                self.assertEqual(arches_for(package), ("amd64", "arm64"))
                self.assertFalse(exact_tag_release_for(package))

    def test_upstream_naming_and_tag(self) -> None:
        for package in ("woodpecker-agent", "woodpecker-cli"):
            with self.subTest(package=package):
                self.assertEqual(repo_of(package), REPO)
                self.assertEqual(tag_for(package, VERSION), TAG)

    def test_assets_name_the_command_and_the_debian_architecture(self) -> None:
        for package, names in ASSETS.items():
            for arch, name in names.items():
                with self.subTest(package=package, arch=arch):
                    self.assertEqual(asset_name(package, VERSION, arch), name)
                    self.assertEqual(archive_suffix_for(package, VERSION, arch), ".tar.gz")

    def test_the_member_and_installed_binary_are_flat_and_named_after_the_package(self) -> None:
        for package in ("woodpecker-agent", "woodpecker-cli"):
            for arch in ("amd64", "arm64"):
                with self.subTest(package=package, arch=arch):
                    self.assertEqual(archive_member(package, VERSION, arch), package)
                    self.assertEqual(binary_name(package), package)
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

    def test_the_committed_manifest_carries_both_reviewed_entries(self) -> None:
        manifest = load_manifest()
        for package, digests in DIGESTS.items():
            with self.subTest(package=package):
                pkg = next(p for p in manifest.packages if p.name == package)
                self.assertEqual(pkg.version, VERSION)
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


class WoodpeckerPackageTest(unittest.TestCase):
    def test_the_tarball_member_becomes_an_executable_package(self) -> None:
        manifest = load_manifest()
        for package in ("woodpecker-agent", "woodpecker-cli"):
            with self.subTest(package=package):
                pkg = next(p for p in manifest.packages if p.name == package)
                target = pkg.targets["amd64"]
                payload = b"\x7fELF-fake-woodpecker"
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    archive = write_fake_archive(
                        root / ASSETS[package]["amd64"],
                        member=target.archive_member, payload=payload,
                    )
                    sources = extract_binaries(archive, target, root / "extract")
                    self.assertEqual(set(sources), {package})
                    stage = stage_package(
                        pkg=pkg, arch="amd64", manifest=manifest,
                        binary_file=sources[package], stage_root=root / "stages",
                    )
                    self.assertTrue((stage / "usr" / "bin" / package).exists())
                    deb = root / f"{package}.deb"
                    build_deb(stage, deb)
                    listing = subprocess.run(
                        ["dpkg-deb", "-c", str(deb)], text=True,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                    )
                    self.assertEqual(listing.returncode, 0, msg=listing.stderr)
                    self.assertIn(f"./usr/bin/{package}", listing.stdout)
                    self.assertIn(f"./usr/share/doc/{package}/copyright", listing.stdout)


if __name__ == "__main__":
    unittest.main()
