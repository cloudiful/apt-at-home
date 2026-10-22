import copy
import unittest

from scripts.manifest import default_manifest_path, load_manifest, parse_manifest
from scripts.release_manifest import load_manifest_data
from scripts.package_licenses import copyright_text, read_vendor_licenses


class PackageLicensesTest(unittest.TestCase):
    def test_uv_copyright_contains_both_reviewed_license_texts(self) -> None:
        pkg = next(pkg for pkg in load_manifest().packages if pkg.name == "uv")
        primary, apache = read_vendor_licenses(pkg)
        text = copyright_text(pkg, primary, apache)
        self.assertIn("License: MIT OR Apache-2.0\n", text)
        self.assertIn(primary.rstrip(), text)
        self.assertIn(apache.rstrip(), text)
        self.assertEqual(text.count("License:"), 1)

    def test_uv_extra_license_source_is_tag_bound_and_required(self) -> None:
        source = load_manifest_data(default_manifest_path())
        missing = copy.deepcopy(source)
        uv = next(pkg for pkg in missing["packages"] if pkg["name"] == "uv")
        del uv["extra_license_urls"]
        with self.assertRaisesRegex(ValueError, "extra_license_urls"):
            parse_manifest(missing)
        changed = copy.deepcopy(source)
        uv = next(pkg for pkg in changed["packages"] if pkg["name"] == "uv")
        uv["extra_license_urls"][0] = "https://raw.githubusercontent.com/astral-sh/uv/0.13.0/EVIL"
        with self.assertRaisesRegex(ValueError, "extra_license_urls"):
            parse_manifest(changed)

    def test_fd_find_copyright_contains_both_reviewed_license_texts(self) -> None:
        pkg = next(pkg for pkg in load_manifest().packages if pkg.name == "fd-find")
        primary, apache = read_vendor_licenses(pkg)
        text = copyright_text(pkg, primary, apache)
        self.assertIn("License: MIT OR Apache-2.0\n", text)
        self.assertIn("MIT License", text)
        self.assertIn("Apache License", text)
        self.assertIn(primary.rstrip(), text)
        self.assertIn(apache.rstrip(), text)
        self.assertEqual(text.count("License:"), 1)

    def test_existing_single_license_format_is_unchanged(self) -> None:
        pkg = next(pkg for pkg in load_manifest().packages if pkg.name == "starship")
        text = copyright_text(pkg, "ISC License\n")
        self.assertEqual(
            text,
            "Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\n"
            "Upstream-Name: starship\n"
            "Upstream-Contact: https://github.com/starship/starship\n"
            "Source: https://github.com/starship/starship\n\n"
            "Files: *\n"
            "Copyright: Copyright (c) 2019-2022, Starship Contributors\n"
            "License: ISC\n"
            "ISC License\n",
        )


if __name__ == "__main__":
    unittest.main()
