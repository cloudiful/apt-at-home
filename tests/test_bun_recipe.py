"""The Bun recipe: official release ZIPs for amd64 and arm64.

Bun publishes one ZIP per architecture, each holding the binary under a
versioned directory. Only the reviewed repository, tag prefix, asset names,
archive member, binary, license source, and dependencies are pinned here; the
version and the digests come from the manifest and upstream.
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
    tag_for,
    triple_for,
)
from scripts.deb import build_deb, stage_package
from scripts.manifest import load_manifest
from scripts.package_binaries import extract_binaries
from tests.helpers import write_fake_zip

REPO_ROOT = Path(__file__).resolve().parent.parent

VERSION = "1.4.3"
TAG = "bun-v1.4.3"
AMD64_ASSET = "bun-linux-x64.zip"
ARM64_ASSET = "bun-linux-aarch64.zip"
DEPENDENCY = "libc6 (>= 2.17), libgcc-s1"
# The upstream LICENSE.md bytes vendored at the pinned tag.
LICENSE_SHA256 = "056696884250b0d682365260cf1487a6501b1665a343ec60e23a1e647043c572"


class BunRecipeTest(unittest.TestCase):
    def test_the_package_is_registered_for_both_architectures(self) -> None:
        self.assertIn("bun", KNOWN_PACKAGES)
        self.assertEqual(arches_for("bun"), ("amd64", "arm64"))
        self.assertFalse(exact_tag_release_for("bun"))

    def test_upstream_and_tag_prefix_are_the_reviewed_ones(self) -> None:
        self.assertEqual(repo_of("bun"), "oven-sh/bun")
        self.assertEqual(tag_for("bun", VERSION), TAG)
        self.assertEqual(tag_for("bun", "1.5.0"), "bun-v1.5.0")

    def test_assets_are_the_official_zips(self) -> None:
        self.assertEqual(asset_name("bun", VERSION, "amd64"), AMD64_ASSET)
        self.assertEqual(asset_name("bun", VERSION, "arm64"), ARM64_ASSET)
        self.assertEqual(triple_for("bun", "amd64"), "x64")
        self.assertEqual(triple_for("bun", "arm64"), "aarch64")
        self.assertEqual(archive_suffix_for("bun", VERSION, "amd64"), ".zip")
        self.assertEqual(archive_suffix_for("bun", VERSION, "arm64"), ".zip")

    def test_the_member_is_the_binary_under_its_asset_prefix(self) -> None:
        self.assertEqual(archive_member("bun", VERSION, "amd64"), "bun-linux-x64/bun")
        self.assertEqual(archive_member("bun", VERSION, "arm64"), "bun-linux-aarch64/bun")
        self.assertEqual(binary_name("bun"), "bun")

    def test_depends_are_the_reviewed_runtime(self) -> None:
        self.assertEqual(depends_for("bun", "amd64"), DEPENDENCY)
        self.assertEqual(depends_for("bun", "arm64"), DEPENDENCY)

    def test_the_license_is_the_complete_upstream_combined_notice(self) -> None:
        self.assertEqual(expected_license_for("bun"), "MIT AND LGPL-2.1-or-later")
        self.assertEqual(license_path_for("bun"), "LICENSE.md")
        vendored = (REPO_ROOT / "packaging" / "licenses" / "bun" / "LICENSE.txt").read_bytes()
        self.assertEqual(hashlib.sha256(vendored).hexdigest(), LICENSE_SHA256)
        # The vendored notice names the bundled LGPL component, so the package
        # is never described as MIT-only.
        self.assertIn(b"JavaScriptCore", vendored)
        self.assertIn(b"LGPL", vendored)

    def test_the_committed_manifest_carries_the_reviewed_entry(self) -> None:
        manifest = load_manifest()
        pkg = next(p for p in manifest.packages if p.name == "bun")
        self.assertEqual(pkg.version, VERSION)
        self.assertEqual(pkg.license, "MIT AND LGPL-2.1-or-later")
        self.assertEqual(
            pkg.license_url,
            f"https://raw.githubusercontent.com/oven-sh/bun/{TAG}/LICENSE.md",
        )
        self.assertEqual(set(pkg.targets), {"amd64", "arm64"})
        amd64 = pkg.targets["amd64"]
        self.assertEqual(
            amd64.url,
            f"https://github.com/oven-sh/bun/releases/download/{TAG}/{AMD64_ASSET}",
        )
        self.assertEqual(
            amd64.sha256,
            "3e730528d89775f03f87f05c06787c4df4099d0a37f3f252e7b829cb23816e18",
        )
        self.assertEqual(amd64.archive_member, "bun-linux-x64/bun")
        self.assertEqual(amd64.binary, "bun")
        self.assertEqual(amd64.depends, DEPENDENCY)
        arm64 = pkg.targets["arm64"]
        self.assertEqual(
            arm64.url,
            f"https://github.com/oven-sh/bun/releases/download/{TAG}/{ARM64_ASSET}",
        )
        self.assertEqual(
            arm64.sha256,
            "efa9813da5ed72423bf847f916e8d2c47c0d776add972354026a75e10da9aa21",
        )
        self.assertEqual(arm64.archive_member, "bun-linux-aarch64/bun")
        self.assertEqual(arm64.binary, "bun")
        self.assertEqual(arm64.depends, DEPENDENCY)


class BunPackageTest(unittest.TestCase):
    def test_the_zip_member_becomes_an_executable_package_with_the_notice(self) -> None:
        manifest = load_manifest()
        pkg = next(p for p in manifest.packages if p.name == "bun")
        target = pkg.targets["amd64"]
        payload = b"\x7fELF-fake-bun"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = write_fake_zip(
                root / AMD64_ASSET, member=target.archive_member, payload=payload,
            )
            sources = extract_binaries(archive, target, root / "extract")
            self.assertEqual(set(sources), {"bun"})
            self.assertEqual(sources["bun"].read_bytes(), payload)
            stage = stage_package(
                pkg=pkg, arch="amd64", manifest=manifest,
                binary_file=sources["bun"], stage_root=root / "stages",
            )
            self.assertTrue((stage / "usr" / "bin" / "bun").exists())
            control = (stage / "DEBIAN" / "control").read_text()
            self.assertIn(f"Depends: {DEPENDENCY}", control)
            copyright = (stage / "usr" / "share" / "doc" / "bun" / "copyright").read_text()
            self.assertIn("License: MIT AND LGPL-2.1-or-later", copyright)
            self.assertIn("JavaScriptCore", copyright)
            deb = root / "bun.deb"
            build_deb(stage, deb)
            listing = subprocess.run(
                ["dpkg-deb", "-c", str(deb)], text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(listing.returncode, 0)
            self.assertIn("./usr/bin/bun", listing.stdout)
            self.assertIn("./usr/share/doc/bun/copyright", listing.stdout)


if __name__ == "__main__":
    unittest.main()
