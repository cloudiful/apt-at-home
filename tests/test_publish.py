import tempfile
import unittest
from pathlib import Path

from scripts.publish import publish_tree
from tests.helpers import FakeS3, make_gpg_home_with_key
from tests.test_verify import build_complete_tree


def _pin_for(home: Path, key_id: str) -> str:
    return key_id


class PublishTest(unittest.TestCase):
    def test_publish_uploads_all_and_moves_pointer_last(self) -> None:
        tmpdir, home, key_id = make_gpg_home_with_key()
        try:
            with tempfile.TemporaryDirectory() as work:
                root = build_complete_tree(Path(work) / "repo", "20260102-030405",
                                           key_home=home, key_id=key_id)
                s3 = FakeS3()
                uploaded = publish_tree(output_root=root, bucket="repo", release_id="20260102-030405",
                                        expected_fingerprint=_pin_for(home, key_id), client=s3)
                self.assertIn("current.json", uploaded)
                self.assertEqual(uploaded[-1], "current.json")
                self.assertIn(("repo", "pool/zellij_0.45.1-1_amd64.deb"), s3.objects)
                self.assertIn(("repo", "key.asc"), s3.objects)
        finally:
            tmpdir.cleanup()

    def test_missing_pin_leaves_zero_writes(self) -> None:
        tmpdir, home, key_id = make_gpg_home_with_key()
        try:
            with tempfile.TemporaryDirectory() as work:
                root = build_complete_tree(Path(work) / "repo", "20260102-030405",
                                           key_home=home, key_id=key_id)
                s3 = FakeS3()
                with self.assertRaises(ValueError):
                    publish_tree(output_root=root, bucket="repo", release_id="20260102-030405",
                                 expected_fingerprint=None, client=s3)  # type: ignore[arg-type]
                self.assertEqual(s3.objects, {})
                self.assertEqual(s3.puts, [])
        finally:
            tmpdir.cleanup()

    def test_wrong_and_malformed_pin_leave_zero_writes(self) -> None:
        tmpdir, home, key_id = make_gpg_home_with_key()
        other_tmp, other_home, other_id = make_gpg_home_with_key()
        try:
            with tempfile.TemporaryDirectory() as work:
                root = build_complete_tree(Path(work) / "repo", "20260102-030405",
                                           key_home=home, key_id=key_id)
                for bad in (other_id, "ABCD1234", "0" * 64):
                    s3 = FakeS3()
                    with self.assertRaises(ValueError, msg=bad):
                        publish_tree(output_root=root, bucket="repo", release_id="20260102-030405",
                                     expected_fingerprint=bad, client=s3)
                    self.assertEqual(s3.objects, {}, msg=bad)
        finally:
            tmpdir.cleanup()
            other_tmp.cleanup()

    def test_rotation_still_requires_matching_pin(self) -> None:
        tmpdir, home, key_id = make_gpg_home_with_key()
        other_tmp, other_home, other_id = make_gpg_home_with_key()
        try:
            with tempfile.TemporaryDirectory() as work:
                root = build_complete_tree(Path(work) / "repo", "20260102-030405",
                                           key_home=home, key_id=key_id)
                s3 = FakeS3()
                s3.objects[("repo", "key.asc")] = b"-----BEGIN PGP PUBLIC KEY BLOCK-----\nold\n"
                # Even with rotation allowed, a stale pin for the old key does
                # not authorize the replacement.
                with self.assertRaises(ValueError):
                    publish_tree(output_root=root, bucket="repo", release_id="20260102-030405",
                                 expected_fingerprint=other_id, client=s3,
                                 allow_key_rotation=True)
                self.assertNotIn(("repo", "current.json"), s3.objects)
        finally:
            tmpdir.cleanup()
            other_tmp.cleanup()

    def test_immutable_conflict_aborts_without_pointer(self) -> None:
        tmpdir, home, key_id = make_gpg_home_with_key()
        try:
            with tempfile.TemporaryDirectory() as work:
                root = build_complete_tree(Path(work) / "repo", "20260102-030405",
                                           key_home=home, key_id=key_id)
                s3 = FakeS3()
                s3.objects[("repo", "pool/zellij_0.45.1-1_amd64.deb")] = b"different-bytes"
                with self.assertRaises(ValueError):
                    publish_tree(output_root=root, bucket="repo", release_id="20260102-030405",
                                 expected_fingerprint=key_id, client=s3)
                self.assertNotIn(("repo", "current.json"), s3.objects)
        finally:
            tmpdir.cleanup()

    def test_failed_verification_preserves_pointer_and_old_hashes(self) -> None:
        tmpdir, home, key_id = make_gpg_home_with_key()
        try:
            with tempfile.TemporaryDirectory() as work:
                first = build_complete_tree(Path(work) / "first", "20260102-030405",
                                            key_home=home, key_id=key_id)
                s3 = FakeS3()
                publish_tree(output_root=first, bucket="repo", release_id="20260102-030405",
                             expected_fingerprint=key_id, client=s3)
                pointer_before = s3.objects[("repo", "current.json")]
                second = build_complete_tree(Path(work) / "second", "20260203-040506",
                                             key_home=home, key_id=key_id)
                broken = (second / "releases" / "20260203-040506" / "dists" / "stable"
                          / "main" / "binary-amd64" / "Packages")
                with broken.open("a", encoding="utf-8") as handle:
                    handle.write("Broken: yes\n")
                with self.assertRaises(ValueError):
                    publish_tree(output_root=second, bucket="repo", release_id="20260203-040506",
                                 expected_fingerprint=key_id, client=s3)
                self.assertEqual(s3.objects[("repo", "current.json")], pointer_before)
                self.assertIn(("repo", "pool/zellij_0.45.1-1_amd64.deb"), s3.objects)
        finally:
            tmpdir.cleanup()

    def test_key_rotation_needs_explicit_confirmation(self) -> None:
        tmpdir, home, key_id = make_gpg_home_with_key()
        try:
            with tempfile.TemporaryDirectory() as work:
                root = build_complete_tree(Path(work) / "repo", "20260102-030405",
                                           key_home=home, key_id=key_id)
                s3 = FakeS3()
                s3.objects[("repo", "key.asc")] = b"-----BEGIN PGP PUBLIC KEY BLOCK-----\nold\n"
                with self.assertRaises(ValueError):
                    publish_tree(output_root=root, bucket="repo", release_id="20260102-030405",
                                 expected_fingerprint=key_id, client=s3)
                uploaded = publish_tree(output_root=root, bucket="repo", release_id="20260102-030405",
                                        expected_fingerprint=key_id, client=s3,
                                        allow_key_rotation=True)
                self.assertIn("key.asc", uploaded)
        finally:
            tmpdir.cleanup()

    def test_release_collision_is_rejected(self) -> None:
        tmpdir, home, key_id = make_gpg_home_with_key()
        try:
            with tempfile.TemporaryDirectory() as work:
                root = build_complete_tree(Path(work) / "repo", "20260102-030405",
                                           key_home=home, key_id=key_id)
                s3 = FakeS3()
                publish_tree(output_root=root, bucket="repo", release_id="20260102-030405",
                             expected_fingerprint=key_id, client=s3)
                s3.objects[("repo", "releases/20260102-030405/dists/stable/Release")] = b"colliding-release"
                with self.assertRaises(ValueError):
                    publish_tree(output_root=root, bucket="repo", release_id="20260102-030405",
                                 expected_fingerprint=key_id, client=s3)
        finally:
            tmpdir.cleanup()


if __name__ == "__main__":
    unittest.main()
