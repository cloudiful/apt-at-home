"""Bounded extraction of one declared member from a release ZIP.

Every case builds a hostile or benign archive on disk and asserts what the
extractor does with it: the pinned regular file is published with its own
bytes, and a traversal, an absolute path, a link, a duplicate name, an
oversized member, or a corrupted payload is refused without publishing
anything.
"""

from __future__ import annotations

import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest import mock

from scripts.archive_zip import safe_extract_zip_member
from scripts.download import safe_extract_member

MEMBER = "bun-linux-x64/bun"
PAYLOAD = b"\x7fELF-fake-bun"


def _zip(
    path: Path,
    entries: list[tuple[str, bytes, int]],
    compression: int = zipfile.ZIP_DEFLATED,
) -> Path:
    """Write a ZIP from (name, payload, unix mode) entries."""
    with zipfile.ZipFile(path, "w", compression) as archive:
        for name, payload, mode in entries:
            info = zipfile.ZipInfo(name)
            info.external_attr = mode << 16
            info.compress_type = compression
            archive.writestr(info, payload)
    return path


class ZipExtractionTest(unittest.TestCase):
    def test_extracts_the_exact_member_with_its_own_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _zip(
                root / "bun.zip",
                [("bun-linux-x64/", b"", 0o040755), (MEMBER, PAYLOAD, 0o100755)],
            )
            dest = root / "bun"
            self.assertEqual(safe_extract_zip_member(archive, MEMBER, dest), dest)
            self.assertEqual(dest.read_bytes(), PAYLOAD)
            self.assertEqual(dest.stat().st_mode & 0o777, 0o755)

    def test_an_unrelated_member_is_never_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _zip(
                root / "bun.zip",
                [
                    ("bun-linux-x64/", b"", 0o040755),
                    (MEMBER, PAYLOAD, 0o100755),
                    ("bun-linux-x64/other", b"other", 0o100755),
                ],
            )
            dest = root / "bun"
            safe_extract_zip_member(archive, MEMBER, dest)
            self.assertEqual(dest.read_bytes(), PAYLOAD)
            self.assertFalse((root / "other").exists())

    def test_a_missing_member_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _zip(root / "bun.zip", [("bun-linux-x64/", b"", 0o040755)])
            with self.assertRaisesRegex(ValueError, "not found"):
                safe_extract_zip_member(archive, MEMBER, root / "bun")

    def test_a_traversing_path_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _zip(root / "evil.zip", [("../evil", b"evil", 0o100755)])
            with self.assertRaisesRegex(ValueError, "unsafe archive member name"):
                safe_extract_zip_member(archive, "../evil", root / "evil")

    def test_an_absolute_path_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _zip(root / "evil.zip", [("/etc/passwd", b"root", 0o100755)])
            with self.assertRaisesRegex(ValueError, "unsafe archive member name"):
                safe_extract_zip_member(archive, "/etc/passwd", root / "passwd")

    def test_a_symlink_member_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _zip(root / "link.zip", [(MEMBER, b"/etc/passwd", 0o120777)])
            with self.assertRaisesRegex(ValueError, "non-regular member"):
                safe_extract_zip_member(archive, MEMBER, root / "bun")

    def test_a_duplicate_member_name_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                archive = _zip(
                    root / "dup.zip",
                    [(MEMBER, PAYLOAD, 0o100755), (MEMBER, b"other", 0o100755)],
                )
            with self.assertRaisesRegex(ValueError, "more than once"):
                safe_extract_zip_member(archive, MEMBER, root / "bun")

    def test_a_directory_cannot_be_extracted_as_the_member(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _zip(root / "dir.zip", [("bun-linux-x64/", b"", 0o040755)])
            with self.assertRaisesRegex(ValueError, "directory member"):
                safe_extract_zip_member(archive, "bun-linux-x64", root / "bun")

    def test_an_oversized_member_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _zip(root / "big.zip", [(MEMBER, PAYLOAD, 0o100755)])
            with mock.patch("scripts.archive_zip.MAX_BINARY_BYTES", 4):
                with self.assertRaisesRegex(ValueError, "limit"):
                    safe_extract_zip_member(archive, MEMBER, root / "bun")
            self.assertFalse((root / "bun").exists())

    def test_a_corrupted_payload_publishes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _zip(
                root / "broken.zip", [(MEMBER, PAYLOAD, 0o100755)], zipfile.ZIP_STORED,
            )
            raw = bytearray(archive.read_bytes())
            start = raw.index(PAYLOAD)
            raw[start] = raw[start] ^ 0xFF
            archive.write_bytes(bytes(raw))
            with self.assertRaises(Exception):
                safe_extract_zip_member(archive, MEMBER, root / "bun")
            self.assertFalse((root / "bun").exists())

    def test_the_shared_entry_point_reads_a_zip_by_its_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = _zip(
                root / "bun-linux-x64.zip",
                [("bun-linux-x64/", b"", 0o040755), (MEMBER, PAYLOAD, 0o100755)],
            )
            dest = root / "bun"
            self.assertEqual(safe_extract_member(archive, MEMBER, dest), dest)
            self.assertEqual(dest.read_bytes(), PAYLOAD)


if __name__ == "__main__":
    unittest.main()
