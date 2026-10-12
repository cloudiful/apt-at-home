"""Bounds and extraction behaviour for the release-ZIP reader.

`test_archive_zip` covers which members the preflight accepts; this file covers
the two things that happen once a member is accepted: the numeric ceilings that
cap a hostile archive, and whether extraction ever publishes a partial or
unverified file. The fixtures come from `test_archive_zip` so both files agree
on what an upstream-shaped release archive looks like.
"""

import tempfile
import unittest
from pathlib import Path

from scripts import archive_zip
from scripts.archive_zip import ZipMember
from tests.test_archive_zip import PAYLOAD, upstream_shaped, write_zip

SELECTED = "bun-linux-x64-baseline/bun"


class SizeLimitTest(unittest.TestCase):
    """Bounds are enforced from declared sizes, before any byte is written."""

    def test_member_count_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            entries = [(f"bun-{n}", b"x", "file", 0o644)
                       for n in range(archive_zip.MAX_MEMBERS + 1)]
            archive = write_zip(Path(tmp) / "a.zip", entries)
            with self.assertRaisesRegex(ValueError, "over the 32 limit"):
                archive_zip.inventory(archive)

    def test_member_size_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            original = archive_zip.MAX_FILE_BYTES
            archive_zip.MAX_FILE_BYTES = 4
            try:
                archive = write_zip(Path(tmp) / "a.zip", [
                    (SELECTED, PAYLOAD, "file", 0o644),
                ])
                with self.assertRaisesRegex(ValueError, "exceeds the 4 byte limit"):
                    archive_zip.inventory(archive)
            finally:
                archive_zip.MAX_FILE_BYTES = original

    def test_total_size_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            original = archive_zip.MAX_TOTAL_BYTES
            archive_zip.MAX_TOTAL_BYTES = len(PAYLOAD) + 1
            try:
                archive = write_zip(Path(tmp) / "a.zip", [
                    (SELECTED, PAYLOAD, "file", 0o644),
                    ("bun-linux-x64-baseline/other", PAYLOAD, "file", 0o644),
                ])
                with self.assertRaisesRegex(ValueError, "total limit"):
                    archive_zip.inventory(archive)
            finally:
                archive_zip.MAX_TOTAL_BYTES = original


class ExtractionTest(unittest.TestCase):
    """Extraction is bounded, atomic, and refuses to publish a partial file."""

    def _archive_and_member(self, root: Path):
        archive = upstream_shaped(root / "release.zip")
        member = archive_zip.select_single_member(
            archive_zip.inventory(archive, SELECTED), SELECTED
        )
        return archive, member

    def test_extracts_exact_bytes_and_applies_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive, member = self._archive_and_member(root)
            dest = archive_zip.extract_member(archive, member, root / "out" / "bun")
            self.assertEqual(dest.read_bytes(), PAYLOAD)
            self.assertEqual(dest.stat().st_mode & 0o777, 0o755)

    def test_existing_destination_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive, member = self._archive_and_member(root)
            dest = root / "out" / "bun"
            dest.parent.mkdir(parents=True)
            dest.write_bytes(b"existing")
            with self.assertRaisesRegex(ValueError, "already exists"):
                archive_zip.extract_member(archive, member, dest)

    def test_stream_longer_than_declared_fails_without_partial_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive, member = self._archive_and_member(root)
            lying = ZipMember(member.name, member.file_size - 4, member.compress_size, member.crc)
            dest = root / "out" / "bun"
            with self.assertRaisesRegex(ValueError, "declared size"):
                archive_zip.extract_member(archive, lying, dest)
            self.assertFalse(dest.exists())
            self.assertEqual(list(dest.parent.glob(".*")), [])

    def test_stream_shorter_than_declared_fails_without_partial_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive, member = self._archive_and_member(root)
            lying = ZipMember(member.name, member.file_size + 4, member.compress_size, member.crc)
            dest = root / "out" / "bun"
            with self.assertRaisesRegex(ValueError, "streamed"):
                archive_zip.extract_member(archive, lying, dest)
            self.assertFalse(dest.exists())

    def test_crc_mismatch_fails_without_partial_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive, member = self._archive_and_member(root)
            wrong = ZipMember(
                member.name, member.file_size, member.compress_size, member.crc ^ 0xFFFF
            )
            dest = root / "out" / "bun"
            with self.assertRaisesRegex(ValueError, "CRC"):
                archive_zip.extract_member(archive, wrong, dest)
            self.assertFalse(dest.exists())


if __name__ == "__main__":
    unittest.main()