"""Production `cargo-tauri` recipe: official amd64 asset, arm64 source policy.

The eleventh package follows official `tauri-cli-vN.N.N` tags automatically.
Only the reviewed tag prefix, asset name, member, binary, license sources,
runtime mapping and compiler policy are pinned here; versions and digests come
from the manifest and upstream. A source-built architecture names no asset, so
any asset question about it is a refusal rather than a guess.
"""

from __future__ import annotations

import unittest

from scripts.catalog import (
    KNOWN_PACKAGES,
    asset_name,
    archive_member,
    binary_name,
    depends_for,
    expected_license_for,
    extra_license_paths_for,
    extra_license_urls_for,
    license_path_for,
    repo_of,
    source_arch_for,
    tag_for,
    triple_for,
)
from scripts.manifest import load_manifest
from scripts.tauri_runtime import REVIEWED_RUNTIME, dependency_string, RuntimeLibrary


VERSION = "2.12.2"
TAG = "tauri-cli-v2.12.2"
ASSET = "cargo-tauri-x86_64-unknown-linux-gnu.tgz"
DEPENDS = (
    "cargo, libbz2-1.0 (>= 1.0.8-6), libc6 (>= 2.39), "
    "libgcc-s1 (>= 14.2.0), liblzma5 (>= 5.8.1)"
)


class CargoTauriRecipeTest(unittest.TestCase):
    def test_the_package_is_registered_without_widening_other_recipes(self) -> None:
        self.assertIn("cargo-tauri", KNOWN_PACKAGES)
        self.assertEqual(len(KNOWN_PACKAGES), 11)
        # Bun stays candidate-only.
        self.assertNotIn("bun", KNOWN_PACKAGES)

    def test_tag_prefix_and_repo_are_the_reviewed_upstream(self) -> None:
        self.assertEqual(repo_of("cargo-tauri"), "tauri-apps/tauri")
        self.assertEqual(tag_for("cargo-tauri", VERSION), TAG)
        self.assertEqual(tag_for("cargo-tauri", "2.13.0"), "tauri-cli-v2.13.0")

    def test_amd64_asset_and_member_are_the_reviewed_ones(self) -> None:
        self.assertEqual(asset_name("cargo-tauri", VERSION, "amd64"), ASSET)
        self.assertEqual(archive_member("cargo-tauri", VERSION, "amd64"), "cargo-tauri")
        self.assertEqual(binary_name("cargo-tauri"), "cargo-tauri")

    def test_arm64_names_no_asset(self) -> None:
        self.assertEqual(source_arch_for("cargo-tauri"), "arm64")
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

    def test_depends_come_from_the_reviewed_runtime_mapping(self) -> None:
        for arch in ("amd64", "arm64"):
            self.assertEqual(depends_for("cargo-tauri", arch), DEPENDS)
        libraries = tuple(
            sorted(
                (
                    RuntimeLibrary(soname=k, package=v[0], version=v[1], symbols=v[2])
                    for k, v in REVIEWED_RUNTIME.items()
                ),
                key=lambda library: library.soname,
            )
        )
        self.assertEqual(dependency_string(libraries, "cargo"), DEPENDS)

    def test_the_committed_manifest_carries_the_reviewed_entry(self) -> None:
        by_name = {p.name: p for p in load_manifest().packages}
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
        arm64 = pkg.targets["arm64"]
        self.assertTrue(arm64.is_source_target)
        self.assertIsNotNone(arm64.source_build)
        self.assertEqual((arm64.url, arm64.sha256, arm64.archive_member), ("", "", ""))
        self.assertEqual(arm64.binary, "cargo-tauri")
        self.assertEqual(arm64.depends, DEPENDS)

    def test_the_rendered_source_document_names_version_commit_and_asset(self) -> None:
        from scripts.package_licenses import cargo_tauri_source_document
        from scripts.source_build import identity_of

        pkg = next(p for p in load_manifest().packages if p.name == "cargo-tauri")
        rendered = cargo_tauri_source_document(pkg)
        identity = identity_of(pkg.targets["arm64"].source_build)  # type: ignore[arg-type]
        for needle in (pkg.version, identity.tag, identity.commit, pkg.targets["amd64"].url,
                       pkg.targets["amd64"].sha256):
            self.assertIn(needle, rendered)
        # No fixed-copy drift: the template carries no other version.
        self.assertNotIn("{version}", rendered)
        self.assertNotIn("{tag}", rendered)


if __name__ == "__main__":
    unittest.main()
