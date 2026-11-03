from __future__ import annotations

import unittest
from pathlib import Path

from scripts.catalog import (
    KNOWN_PACKAGES,
    RECIPES,
    arches_for,
    asset_name,
    tag_for,
    archive_member,
)
from scripts.manifest import load_manifest
from scripts.package_licenses import pi_source_document, read_vendor_licenses


class PiRecipeTest(unittest.TestCase):
    def test_recipe_pins_the_reviewed_upstream_release_and_bundle(self) -> None:
        self.assertIn("pi", KNOWN_PACKAGES)
        recipe = RECIPES["pi"]
        self.assertEqual(recipe.repo, "earendil-works/pi")
        self.assertEqual(recipe.tag_prefix, "v")
        self.assertEqual(recipe.bundle_type, "pi-standard")
        self.assertEqual(recipe.license_id, "MIT")
        self.assertEqual(recipe.license_path, "LICENSE")
        self.assertEqual(tag_for("pi", "1.5.0"), "v1.5.0")
        self.assertEqual(arches_for("pi"), ("amd64", "arm64"))
        self.assertEqual(asset_name("pi", "1.5.0", "amd64"), "pi-linux-x64.tar.gz")
        self.assertEqual(asset_name("pi", "1.5.0", "arm64"), "pi-linux-arm64.tar.gz")
        self.assertEqual(archive_member("pi", "1.5.0", "amd64"), "pi/pi")
        self.assertEqual(archive_member("pi", "1.5.0", "arm64"), "pi/pi")

    def test_manifest_pins_both_official_archives_and_mit_provenance(self) -> None:
        package = next(pkg for pkg in load_manifest().packages if pkg.name == "pi")
        self.assertEqual(package.version, "1.5.0")
        self.assertEqual(package.license, "MIT")
        self.assertEqual(
            package.license_url,
            "https://raw.githubusercontent.com/earendil-works/pi/v1.5.0/LICENSE",
        )
        self.assertEqual(package.upstream_published_at, "2026-11-02T11:05:31Z")
        self.assertEqual(set(package.targets), {"amd64", "arm64"})
        expected = {
            "amd64": (
                "https://github.com/earendil-works/pi/releases/download/v1.5.0/pi-linux-x64.tar.gz",
                "df870ba0e344cb7b3358d8d8f19397b8b8d225a8669784736ad68a5c2169d53b",
            ),
            "arm64": (
                "https://github.com/earendil-works/pi/releases/download/v1.5.0/pi-linux-arm64.tar.gz",
                "5d5711b48600756473682e86360b026b11abf7591e5d112eb411d4da2c100d9a",
            ),
        }
        for arch, (url, digest) in expected.items():
            target = package.targets[arch]
            self.assertEqual(target.url, url)
            self.assertEqual(target.sha256, digest)
            self.assertEqual(target.archive_member, "pi/pi")
            self.assertEqual(target.binary, "pi")
            self.assertEqual(target.bundle_type, "pi-standard")
            self.assertEqual(target.depends, "libc6 (>= 2.17)")

    def test_vendored_license_and_source_pointer_match_the_release(self) -> None:
        package = next(pkg for pkg in load_manifest().packages if pkg.name == "pi")
        bodies = read_vendor_licenses(package)
        self.assertEqual(len(bodies), 1)
        self.assertIn("MIT License", bodies[0])
        self.assertIn("Copyright (c) 2025 Mario Zechner", bodies[0])
        source = pi_source_document("1.5.0")
        self.assertIn("`v1.5.0`", source)
        self.assertIn("/usr/lib/pi", source)
        self.assertIn("PI_SELF_UPDATE_COMMAND", source)
        with self.assertRaises(ValueError):
            pi_source_document("v1.5.0")


if __name__ == "__main__":
    unittest.main()
