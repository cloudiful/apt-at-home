import os
import tempfile
import unittest
from pathlib import Path

from scripts.sign import IsolatedHomedir, export_public_key, import_key, sign_release, verify_clearsign, verify_detached
from tests.helpers import make_gpg_home_with_key


class SignTest(unittest.TestCase):
    def test_sign_and_verify_roundtrip_uses_isolated_homedir(self) -> None:
        tmpdir, home, key_id = make_gpg_home_with_key()
        try:
            with tempfile.TemporaryDirectory() as work:
                release = Path(work) / "Release"
                release.write_text("Origin: apt-at-home\nSuite: stable\n", encoding="utf-8")
                old = os.environ.get("GNUPGHOME")
                os.environ["GNUPGHOME"] = str(home)
                try:
                    inrelease, detached = sign_release(release_file=release, key_id=key_id)
                finally:
                    if old is None:
                        os.environ.pop("GNUPGHOME", None)
                    else:
                        os.environ["GNUPGHOME"] = old
                self.assertTrue(inrelease.exists())
                self.assertTrue(detached.exists())
                with IsolatedHomedir() as verify_home:
                    public = Path(work) / "key.asc"
                    export_public_key(homedir=home, key_id=key_id, dest=public)
                    self.assertIn("PGP PUBLIC KEY", public.read_text())
                    import_key(verify_home, public)
                    verify_detached(release_file=release, signature=detached, homedir=verify_home)
                    verify_clearsign(inrelease=inrelease, homedir=verify_home)
        finally:
            tmpdir.cleanup()

    def test_tampered_release_fails_verification(self) -> None:
        tmpdir, home, key_id = make_gpg_home_with_key()
        try:
            with tempfile.TemporaryDirectory() as work:
                release = Path(work) / "Release"
                release.write_text("Origin: apt-at-home\n", encoding="utf-8")
                old = os.environ.get("GNUPGHOME")
                os.environ["GNUPGHOME"] = str(home)
                try:
                    _, detached = sign_release(release_file=release, key_id=key_id)
                finally:
                    if old is None:
                        os.environ.pop("GNUPGHOME", None)
                    else:
                        os.environ["GNUPGHOME"] = old
                release.write_text("Origin: tampered\n", encoding="utf-8")
                with IsolatedHomedir() as verify_home:
                    public = Path(work) / "key.asc"
                    export_public_key(homedir=home, key_id=key_id, dest=public)
                    import_key(verify_home, public)
                    with self.assertRaises(RuntimeError):
                        verify_detached(release_file=release, signature=detached, homedir=verify_home)
        finally:
            tmpdir.cleanup()


if __name__ == "__main__":
    unittest.main()
