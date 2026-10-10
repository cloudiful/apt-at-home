import unittest
from pathlib import Path

from scripts.manifest import deb_filename, debian_version, load_manifest


class ManifestTest(unittest.TestCase):
    def test_pins_two_packages_with_four_targets(self) -> None:
        manifest = load_manifest()
        self.assertEqual(len(manifest.packages), 2)
        self.assertEqual(sorted(p.name for p in manifest.packages), ["starship", "zellij"])
        self.assertEqual(manifest.debian_revision, "-1")
        for pkg in manifest.packages:
            self.assertSetEqual(set(pkg.targets), {"amd64", "arm64"})
            for arch, target in pkg.targets.items():
                self.assertTrue(target.url.startswith("https://github.com/"))
                self.assertRegex(target.sha256, r"^[0-9a-f]{64}$")
                self.assertNotIn("latest", target.url)

    def test_records_exact_reviewed_versions(self) -> None:
        manifest = load_manifest()
        versions = {p.name: p.version for p in manifest.packages}
        self.assertEqual(versions, {"zellij": "0.45.1", "starship": "1.26.0"})

    def test_records_reviewed_archive_hashes(self) -> None:
        manifest = load_manifest()
        by_name = {p.name: p for p in manifest.packages}
        self.assertEqual(
            by_name["zellij"].targets["amd64"].sha256,
            "40bcc2e03f5d5ae8e054e39f676081fe12ab70871506996ba595834c3718eefc",
        )
        self.assertEqual(
            by_name["zellij"].targets["arm64"].sha256,
            "05f0802afadd53f8db9514e7cae53c9ae8432fed1b35b8294aa816ee3044a16b",
        )
        self.assertEqual(
            by_name["starship"].targets["amd64"].sha256,
            "b7c232b0e8249d8e55a40beb79c5c43a7d370f3f9408bd215deb0170daeaadf3",
        )
        self.assertEqual(
            by_name["starship"].targets["arm64"].sha256,
            "dc30189378d2f2e287384e8a692d3f95ad1df64cf0e8c36aa9201516028aed6b",
        )

    def test_debian_naming_uses_revision(self) -> None:
        manifest = load_manifest()
        by_name = {p.name: p for p in manifest.packages}
        self.assertEqual(debian_version(by_name["zellij"], manifest), "0.45.1-1")
        self.assertEqual(deb_filename(by_name["zellij"], "amd64", manifest),
                         "zellij_0.45.1-1_amd64.deb")
        self.assertEqual(deb_filename(by_name["starship"], "arm64", manifest),
                         "starship_1.26.0-1_arm64.deb")

    def test_rejects_bad_manifest(self) -> None:
        from scripts.manifest import parse_manifest

        with self.assertRaises(ValueError):
            parse_manifest({"packages": []})
        with self.assertRaises(ValueError):
            parse_manifest({
                "maintainer": "m", "section": "s", "priority": "p",
                "debian_revision": "-2",
                "packages": [],
            })


if __name__ == "__main__":
    unittest.main()
