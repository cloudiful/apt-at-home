"""Ordinary `cargo-tauri` recipe: both official Linux assets of one stable tag.

The `cargo-tauri` recipe follows the highest stable `tauri-cli-vN.N.N` tag and
that exact release's official Linux assets. Only the reviewed tag prefix, asset
names, member, binary, license sources, architecture policy, and exact-tag
policy are pinned here; versions and digests come from the manifest and
upstream.
"""

from __future__ import annotations

import unittest

from scripts.catalog import (
    KNOWN_PACKAGES,
    RECIPES,
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
from scripts.manifest import load_manifest


VERSION = "2.12.3"
TAG = "tauri-cli-v2.12.3"
AMD64_ASSET = "cargo-tauri-x86_64-unknown-linux-gnu.tgz"
ARM64_ASSET = "cargo-tauri-aarch64-unknown-linux-gnu.tgz"
AMD64_DEPENDS = "cargo, libc6 (>= 2.34), libgcc-s1 (>= 14.2.0), liblzma5 (>= 5.8.1)"
ARM64_DEPENDS = "cargo, libc6 (>= 2.18), libgcc-s1 (>= 14.2.0)"


class CargoTauriRecipeTest(unittest.TestCase):
    def test_the_package_is_registered(self) -> None:
        self.assertIn("cargo-tauri", KNOWN_PACKAGES)

    def test_tag_prefix_and_repo_are_the_reviewed_upstream(self) -> None:
        self.assertEqual(repo_of("cargo-tauri"), "tauri-apps/tauri")
        self.assertEqual(tag_for("cargo-tauri", VERSION), TAG)
        self.assertEqual(tag_for("cargo-tauri", "2.13.0"), "tauri-cli-v2.13.0")

    def test_architecture_policy_covers_both_repository_architectures(self) -> None:
        self.assertEqual(arches_for("cargo-tauri"), ("amd64", "arm64"))
        self.assertTrue(exact_tag_release_for("cargo-tauri"))
        # Every reviewed recipe publishes both architectures, while exact-tag
        # release selection stays this package's alone.
        for name, recipe in RECIPES.items():
            with self.subTest(package=name):
                self.assertEqual(recipe.arches, ("amd64", "arm64"))
                if name != "cargo-tauri":
                    self.assertFalse(recipe.exact_tag_release)

    def test_both_assets_and_the_flat_member_are_the_reviewed_ones(self) -> None:
        self.assertEqual(
            triple_for("cargo-tauri", "amd64"), "x86_64-unknown-linux-gnu"
        )
        self.assertEqual(
            triple_for("cargo-tauri", "arm64"), "aarch64-unknown-linux-gnu"
        )
        self.assertEqual(asset_name("cargo-tauri", VERSION, "amd64"), AMD64_ASSET)
        self.assertEqual(asset_name("cargo-tauri", VERSION, "arm64"), ARM64_ASSET)
        for arch in ("amd64", "arm64"):
            with self.subTest(arch=arch):
                self.assertEqual(
                    archive_member("cargo-tauri", VERSION, arch), "cargo-tauri"
                )
        self.assertEqual(binary_name("cargo-tauri"), "cargo-tauri")

    def test_license_sources_are_tag_bound_and_dual(self) -> None:
        self.assertEqual(expected_license_for("cargo-tauri"), "MIT OR Apache-2.0")
        self.assertEqual(license_path_for("cargo-tauri"), "LICENSE-MIT")
        self.assertEqual(extra_license_paths_for("cargo-tauri"), ("LICENSE-APACHE-2.0",))
        self.assertEqual(
            extra_license_urls_for("cargo-tauri", VERSION),
            (f"https://raw.githubusercontent.com/tauri-apps/tauri/{TAG}/LICENSE-APACHE-2.0",),
        )

    def test_depends_are_the_reviewed_per_arch_runtime(self) -> None:
        self.assertEqual(depends_for("cargo-tauri", "amd64"), AMD64_DEPENDS)
        self.assertEqual(depends_for("cargo-tauri", "arm64"), ARM64_DEPENDS)

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
        self.assertEqual(set(pkg.targets), {"amd64", "arm64"})
        amd64 = pkg.targets["amd64"]
        self.assertEqual(
            amd64.url,
            f"https://github.com/tauri-apps/tauri/releases/download/{TAG}/{AMD64_ASSET}",
        )
        self.assertEqual(
            amd64.sha256,
            "2f635874cbb0103878cd04794aab9f1fec9adf3950ca7c4b1be7f1d845244501",
        )
        self.assertEqual((amd64.archive_member, amd64.binary), ("cargo-tauri", "cargo-tauri"))
        self.assertEqual(amd64.depends, AMD64_DEPENDS)
        arm64 = pkg.targets["arm64"]
        self.assertEqual(
            arm64.url,
            f"https://github.com/tauri-apps/tauri/releases/download/{TAG}/{ARM64_ASSET}",
        )
        self.assertEqual(
            arm64.sha256,
            "13baa60bb1bfa8567c1e021b1192bbb0d5baa60949f03650492eecfb22c8f8e0",
        )
        self.assertEqual((arm64.archive_member, arm64.binary), ("cargo-tauri", "cargo-tauri"))
        self.assertEqual(arm64.depends, ARM64_DEPENDS)


if __name__ == "__main__":
    unittest.main()
