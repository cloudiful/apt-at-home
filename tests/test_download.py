import hashlib
import tarfile
import tempfile
import unittest
from pathlib import Path

from scripts.download import fetch_verified, safe_extract_member, verify_sha256


def _make_tarball(path: Path, members: dict[str, bytes], *, symlink: tuple[str, str] | None = None) -> None:
    with tarfile.open(path, "w:gz") as tar:
        for name, data in members.items():
            import io

            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mode = 0o755
            info.mtime = 0
            tar.addfile(info, io.BytesIO(data))
        if symlink is not None:
            link_name, link_target = symlink
            info = tarfile.TarInfo(name=link_name)
            info.type = tarfile.SYMTYPE
            info.linkname = link_target
            tar.addfile(info)


class DownloadTest(unittest.TestCase):
    def test_verify_sha256_accepts_and_rejects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "file.bin"
            target.write_bytes(b"payload")
            expected = hashlib.sha256(b"payload").hexdigest()
            verify_sha256(target, expected)
            with self.assertRaises(ValueError):
                verify_sha256(target, "0" * 64)

    def test_fetch_verified_reuses_matching_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "archive.tar.gz"
            dest.write_bytes(b"cached")
            digest = hashlib.sha256(b"cached").hexdigest()
            # No network: existing matching file must be reused without download.
            self.assertEqual(fetch_verified("https://example.invalid/x", digest, dest), dest)

    def test_safe_extract_single_member(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "a.tar.gz"
            _make_tarball(archive, {"starship": b"ELF-binary"})
            dest = Path(tmp) / "out" / "starship"
            safe_extract_member(archive, "starship", dest)
            self.assertEqual(dest.read_bytes(), b"ELF-binary")

    def test_safe_extract_rejects_missing_member(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "a.tar.gz"
            _make_tarball(archive, {"other": b"x"})
            with self.assertRaises(ValueError):
                safe_extract_member(archive, "starship", Path(tmp) / "out")

    def test_safe_extract_rejects_absolute_and_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for evil in ("/etc/passwd", "../escape", "a/../../escape"):
                archive = Path(tmp) / "evil.tar.gz"
                _make_tarball(archive, {evil: b"x"})
                with self.assertRaises(ValueError, msg=evil):
                    safe_extract_member(archive, evil, Path(tmp) / "out")

    def test_safe_extract_rejects_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "link.tar.gz"
            _make_tarball(archive, {}, symlink=("starship", "/etc/passwd"))
            with self.assertRaises(ValueError):
                safe_extract_member(archive, "starship", Path(tmp) / "out")


if __name__ == "__main__":
    unittest.main()
