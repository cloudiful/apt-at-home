"""Ordinary `cargo-tauri` recipe: official amd64 asset only.

The eleventh package follows the highest stable `tauri-cli-vN.N.N` tag and
that exact release's official amd64 asset. Only the reviewed tag prefix, asset
name, member, binary, license sources, architecture policy, and exact-tag
policy are pinned here; versions and digests come from the manifest and
upstream. Absence on ARM is normal: the recipe declares amd64 only.
"""

from __future__ import annotations

import unittest

from scripts.catalog import (
    KNOWN_PACKAGES,
    arches_for,
    asset_name,
    archive_member,
    binary_name,
    depends_for,
    exact_tag_release_for,
    expected_license_for,
    extra_license_paths_for,
    extra_license_urls_for,
    license_path_for,
    repo_of,
    tag_for,
    triple_for,
)
from scripts.manifest import expected_debs, expected_debs_for_arch, load_manifest


VERSION = "2.12.2"
TAG = "tauri-cli-v2.12.2"
ASSET = "cargo-tauri-x86_64-unknown-linux-gnu.tgz"
DEPENDS = "cargo, libc6 (>= 2.34), libgcc-s1 (>= 14.2.0), liblzma5 (>= 5.8.1)"


class CargoTauriRecipeTest(unittest.TestCase):
    def test_the_package_is_registered_without_widening_other_recipes(self) -> None:
        self.assertIn("cargo-tauri", KNOWN_PACKAGES)
        self.assertEqual(len(KNOWN_PACKAGES), 11)
        self.assertNotIn("bun", KNOWN_PACKAGES)

    def test_tag_prefix_and_repo_are_the_reviewed_upstream(self) -> None:
        self.assertEqual(repo_of("cargo-tauri"), "tauri-apps/tauri")
        self.assertEqual(tag_for("cargo-tauri", VERSION), TAG)
        self.assertEqual(tag_for("cargo-tauri", "2.13.0"), "tauri-cli-v2.13.0")

    def test_architecture_policy_is_explicit_amd64_only(self) -> None:
        self.assertEqual(arches_for("cargo-tauri"), ("amd64",))
        self.assertTrue(exact_tag_release_for("cargo-tauri"))
        for name in (
            "zellij", "starship", "git-delta", "rustfs-cli", "sccache",
            "neovim", "bottom", "uv", "zoxide", "kandev",
        ):
            with self.subTest(package=name):
                self.assertEqual(arches_for(name), ("amd64", "arm64"))
                self.assertFalse(exact_tag_release_for(name))

    def test_amd64_asset_and_member_are_the_reviewed_ones(self) -> None:
        self.assertEqual(asset_name("cargo-tauri", VERSION, "amd64"), ASSET)
        self.assertEqual(archive_member("cargo-tauri", VERSION, "amd64"), "cargo-tauri")
        self.assertEqual(binary_name("cargo-tauri"), "cargo-tauri")

    def test_arm64_names_no_asset(self) -> None:
        with self.assertRaisesRegex(ValueError, "publishes no arm64 release asset"):
            triple_for("cargo-tauri", "arm64")
        with self.assertRaisesRegex(ValueError, "publishes no arm64 release asset"):
            asset_name("cargo-tauri", VERSION, "arm64")

    def test_license_sources_are_tag_bound_and_dual(self) -> None:
        self.assertEqual(expected_license_for("cargo-tauri"), "MIT OR Apache-2.0")
        self.assertEqual(license_path_for("cargo-tauri"), "LICENSE-MIT")
        self.assertEqual(extra_license_paths_for("cargo-tauri"), ("LICENSE-APACHE-2.0",))
        self.assertEqual(
            extra_license_urls_for("cargo-tauri", VERSION),
            (f"https://raw.githubusercontent.com/tauri-apps/tauri/{TAG}/LICENSE-APACHE-2.0",),
        )

    def test_depends_are_the_reviewed_amd64_runtime(self) -> None:
        self.assertEqual(depends_for("cargo-tauri", "amd64"), DEPENDS)

    def test_the_committed_manifest_carries_the_reviewed_entry(self) -> None:
        manifest = load_manifest()
        by_name = {p.name: p for p in manifest.packages}
        pkg = by_name["cargo-tauri"]
        self.assertEqual(pkg.version, VERSION)
        self.assertEqual(pkg.license, "MIT OR Apache-2.0")
        self.assertEqual(
            pkg.license_url,
            f"https://raw.githubusercontent.com/tauri-apps/tauri/{TAG}/LICENSE-MIT",
        )
        self.assertEqual(
            pkg.extra_license_urls,
            (f"https://raw.githubusercontent.com/tauri-apps/tauri/{TAG}/LICENSE-APACHE-2.0",),
        )
        self.assertEqual(set(pkg.targets), {"amd64"})
        amd64 = pkg.targets["amd64"]
        self.assertEqual(
            amd64.url,
            f"https://github.com/tauri-apps/tauri/releases/download/{TAG}/{ASSET}",
        )
        self.assertEqual(
            amd64.sha256,
            "35c7b50266256cb6d5f757608d7097a59a5faeb0c75879ee1f504652a9b6087e",
        )
        self.assertEqual((amd64.archive_member, amd64.binary), ("cargo-tauri", "cargo-tauri"))
        self.assertEqual(amd64.depends, DEPENDS)

    def test_manifest_declares_twenty_one_debs(self) -> None:
        manifest = load_manifest()
        self.assertEqual(len(manifest.packages), 11)
        self.assertEqual(len(expected_debs(manifest)), 21)
        self.assertEqual(len(expected_debs_for_arch(manifest, "amd64")), 11)
        self.assertEqual(len(expected_debs_for_arch(manifest, "arm64")), 10)


if __name__ == "__main__":
    unittest.main()
