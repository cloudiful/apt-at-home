"""One shared Release hash parser: sections, sizes, and section boundaries."""

import unittest

from scripts.release_hashes import parse_release_hashes, parse_release_section

RELEASE = (
    "Origin: apt-at-home\n"
    "Suite: stable\n"
    "MD5Sum:\n"
    " 0d1e2f3a4b5c6d7e8f90a1b2c3d4e5f6 12 main/binary-amd64/Packages\n"
    "SHA256:\n"
    " abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789 12 main/binary-amd64/Packages\n"
    " 00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff 34 main/binary-arm64/Packages\n"
    "SHA512:\n"
    " fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210"
    "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210 12 main/binary-amd64/Packages\n"
)


class ParseReleaseHashesTest(unittest.TestCase):
    def test_both_sections_are_keyed_by_path(self) -> None:
        hashes = parse_release_hashes(RELEASE)
        self.assertEqual(
            hashes["SHA256"],
            {
                "main/binary-amd64/Packages": (
                    "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
                    12,
                ),
                "main/binary-arm64/Packages": (
                    "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff",
                    34,
                ),
            },
        )
        self.assertEqual(
            hashes["SHA512"],
            {"main/binary-amd64/Packages": ("fedcba9876543210" * 8, 12)},
        )

    def test_a_section_stops_before_the_next_header(self) -> None:
        entries = parse_release_section(RELEASE, "SHA256")
        self.assertEqual(set(entries), {"main/binary-amd64/Packages", "main/binary-arm64/Packages"})

    def test_md5_is_parsed_only_when_requested(self) -> None:
        self.assertEqual(
            parse_release_section(RELEASE, "MD5Sum"),
            {"main/binary-amd64/Packages": ("0d1e2f3a4b5c6d7e8f90a1b2c3d4e5f6", 12)},
        )

    def test_missing_section_is_empty(self) -> None:
        self.assertEqual(parse_release_section("Origin: apt-at-home\n", "SHA256"), {})


if __name__ == "__main__":
    unittest.main()
