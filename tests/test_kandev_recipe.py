from __future__ import annotations

import unittest

from scripts.catalog import (
    archive_member,
    asset_name,
    depends_for,
    expected_license_for,
    repo_of,
    tag_for,
)
from scripts.manifest import load_manifest
from scripts.package_licenses import kandev_source_document


class KandevRecipeTest(unittest.TestCase):
    def test_release_metadata_is_closed_for_both_published_architectures(self) -> None:
        self.assertEqual(repo_of("kandev"), "kdlbs/kandev")
        self.assertEqual(tag_for("kandev", "0.97.0"), "v0.97.0")
        expected = {
            "amd64": "kandev-linux-x64.tar.gz",
            "arm64": "kandev-linux-arm64.tar.gz",
        }
        for arch, asset in expected.items():
            with self.subTest(arch=arch):
                self.assertEqual(asset_name("kandev", "0.97.0", arch), asset)
                self.assertEqual(archive_member("kandev", "0.97.0", arch), "kandev/bin/kandev")
                self.assertEqual(depends_for("kandev", arch), "libc6 (>= 2.34)")
        self.assertEqual(expected_license_for("kandev"), "AGPL-3.0-or-later")

    def test_manifest_pins_official_assets_and_exact_license_source(self) -> None:
        manifest = load_manifest()
        package = next(pkg for pkg in manifest.packages if pkg.name == "kandev")
        self.assertEqual(package.version, "0.97.0")
        self.assertEqual(package.license, "AGPL-3.0-or-later")
        self.assertEqual(
            package.license_url,
            "https://raw.githubusercontent.com/kdlbs/kandev/v0.97.0/LICENSE",
        )
        self.assertEqual(
            package.targets["amd64"].sha256,
            "d15b5ca901fc051dfefbb40a2e0fbec8e92aaf16db8225921f32a6ccdc7d2457",
        )
        self.assertEqual(
            package.targets["arm64"].sha256,
            "3bba944aa4f73441864203c1a84a96e09bcdfc18d01c32cfbb8ba664b2c6b823",
        )

    def test_source_document_is_tag_and_commit_bound(self) -> None:
        body = kandev_source_document("0.97.0", "a" * 40)
        self.assertIn("Release tag: `v0.97.0`", body)
        self.assertIn("Bundle build commit reported by `remote-helpers.json`: `" + "a" * 40, body)
        self.assertIn("do not assert source closure", body)
        with self.assertRaises(ValueError):
            kandev_source_document("latest", "a" * 40)
        with self.assertRaises(ValueError):
            kandev_source_document("0.97.0", "not-a-full-commit")


if __name__ == "__main__":
    unittest.main()
