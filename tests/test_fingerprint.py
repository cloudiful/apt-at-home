import tempfile
import unittest
from pathlib import Path

from scripts.fingerprint import (
    check_key_matches_pin,
    normalize_fingerprint,
    primary_fingerprint_of_keyfile,
)
from scripts.sign import export_public_key
from tests.helpers import make_gpg_home_with_key


class FingerprintTest(unittest.TestCase):
    def test_normalize_accepts_case_and_whitespace(self) -> None:
        self.assertEqual(normalize_fingerprint("  abcd" * 10), "ABCD" * 10)
        self.assertEqual(normalize_fingerprint("aBcD" * 10), "ABCD" * 10)

    def test_normalize_rejects_short_and_malformed(self) -> None:
        for bad in ["ABCD1234", "ZZZZ" * 10, "", "  ", "0" * 39, "0" * 41]:
            with self.assertRaises(ValueError, msg=bad):
                normalize_fingerprint(bad)

    def test_primary_extraction_and_two_key_rejection(self) -> None:
        first_tmp, first_home, first_id = make_gpg_home_with_key()
        second_tmp, second_home, second_id = make_gpg_home_with_key()
        try:
            self.assertNotEqual(first_id, second_id)
            with tempfile.TemporaryDirectory() as work:
                first_pub = Path(work) / "first.asc"
                second_pub = Path(work) / "second.asc"
                export_public_key(homedir=first_home, key_id=first_id, dest=first_pub)
                export_public_key(homedir=second_home, key_id=second_id, dest=second_pub)
                self.assertEqual(primary_fingerprint_of_keyfile(first_pub), first_id.upper())
                # A wrong primary never satisfies the pin, even though both
                # keys are well-formed and self-consistent.
                with self.assertRaises(ValueError):
                    check_key_matches_pin(key_file=first_pub, expected=second_id)
                check_key_matches_pin(key_file=first_pub, expected=first_id.lower())
        finally:
            first_tmp.cleanup()
            second_tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
