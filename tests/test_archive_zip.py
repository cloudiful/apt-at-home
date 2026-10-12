"""Adversarial coverage for the release-ZIP reader.

The candidate's whole supply chain rests on this module: an upstream release
archive is untrusted input, and a single missed member type or a single unbounded
stream would turn a pinned download into arbitrary code. So each test builds an
archive that is hostile in exactly one way and asserts the reader refuses it, and
the extraction tests additionally assert that a refused member leaves nothing
behind on disk.
"""

import stat
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import MagicMock

from scripts import archive_zip

PAYLOAD = b"#!/bin/true\nfake bun\n"


def write_zip(dest: Path, entries: list[tuple]) -> Path:
    """Write a ZIP from (name, payload, kind, mode) tuples.

    `kind` selects the recorded type bits; "file" with mode 0 leaves external_attr
    zeroed, which is what an ordinary ZIP writer that records no Unix mode emits.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, payload, kind, mode in entries:
            info = zipfile.ZipInfo(name)
            if kind == "dir":
                zf.writestr(info, b"")
                continue
            info.compress_type = zipfile.ZIP_DEFLATED
            if kind == "file":
                info.external_attr = mode << 16
                zf.writestr(info, payload)
            else:
                type_bits = {
                    "symlink": stat.S_IFLNK, "fifo": stat.S_IFIFO,
                    "chr": stat.S_IFCHR, "blk": stat.S_IFBLK,
                    "setuid": stat.S_IFREG | stat.S_ISUID,
                    "setgid": stat.S_IFREG | stat.S_ISGID,
                }[kind]
                info.external_attr = (type_bits | (mode & 0o777)) << 16
                zf.writestr(info, payload)
    return dest


def upstream_shaped(path: Path, *, payload: bytes = PAYLOAD) -> Path:
    """The real release shape: one root directory holding exactly one binary."""
    return write_zip(path, [("bun-linux-x64-baseline/", None, "dir", 0),
                           ("bun-linux-x64-baseline/bun", payload, "file", 0o755)])


def inventory_of_names(names: list[str]) -> tuple:
    """Run `inventory` over names that cannot be produced by writing a real ZIP.

    `ZipFile.writestr` stores the already-NUL-truncated `filename`, so a NUL in
    the stored name is unreachable through the writer. Feeding `ZipInfo` objects
    straight to `inventory` is the only way to present that spelling, and it is
    what a hostile archive would hand the reader.
    """
    infos = []
    for name in names:
        info = zipfile.ZipInfo(name)
        info.CRC = 0
        info.compress_type = zipfile.ZIP_STORED
        infos.append(info)
    opener = MagicMock()
    opener.return_value.__enter__.return_value.infolist.return_value = infos
    with mock.patch.object(Path, "stat", return_value=SimpleNamespace(st_size=1)), \
         mock.patch.object(archive_zip.zipfile, "ZipFile", opener):
        return archive_zip.inventory(Path("unused.zip"))


class MemberNameSafetyTest(unittest.TestCase):
    """A member name is data, never a path we will act on."""

    def _refuses(self, name: str) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = write_zip(Path(tmp) / "a.zip", [(name, b"x", "file", 0o644)])
            with self.assertRaisesRegex(ValueError, "unsafe zip member name"):
                archive_zip.inventory(archive)

    def test_absolute_path_refused(self) -> None:
        self._refuses("/etc/passwd")

    def test_parent_traversal_refused(self) -> None:
        self._refuses("../escape")

    def test_traversal_inside_a_valid_prefix_refused(self) -> None:
        self._refuses("bun-linux-x64-baseline/../../escape")

    def test_backslash_separator_refused(self) -> None:
        self._refuses("bun-linux-x64-baseline\\bun")

    def test_nul_byte_refused(self) -> None:
        # zipfile truncates a name at NUL while reading, so the guard is proven
        # on the validator itself rather than through a round-tripped archive.
        with self.assertRaisesRegex(ValueError, "unsafe zip member name"):
            archive_zip.canonical_member("bun\x00evil")

    def test_control_byte_refused(self) -> None:
        self._refuses("bun\x01")

    def test_leading_dot_slash_is_canonicalized(self) -> None:
        """A leading `./` is a spelling of the same path, not an alias."""
        self.assertEqual(archive_zip.canonical_member("./bun"), "bun")

    def test_nul_byte_refused(self) -> None:
        # zipfile truncates a name at NUL while reading, so the guard is proven
        # on the validator itself rather than through a round-tripped archive.
        with self.assertRaisesRegex(ValueError, "unsafe zip member name"):
            archive_zip.canonical_member("bun\x00evil")

    def test_control_byte_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsafe zip member name"):
            archive_zip.canonical_member("bun\x01")

    def test_double_slash_alias_refused(self) -> None:
        self._refuses("bun-linux-x64-baseline//bun")

    def test_relative_dot_component_inside_path_refused(self) -> None:
        self._refuses("bun-linux-x64-baseline/./bun")

    def test_empty_name_refused(self) -> None:
        self._refuses("")


class MemberStructureTest(unittest.TestCase):
    """Names that are individually safe can still be unsafe together."""

    def test_duplicate_member_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with warnings.catch_warnings():
                # Writing the same name twice is the point of this fixture.
                warnings.simplefilter("ignore", UserWarning)
                archive = write_zip(Path(tmp) / "a.zip", [
                    ("bun-linux-x64-baseline/bun", b"a", "file", 0o644),
                    ("bun-linux-x64-baseline/bun", b"b", "file", 0o644),
                ])
            with self.assertRaisesRegex(ValueError, "more than once"):
                archive_zip.inventory(archive)

    def test_file_nested_inside_another_file_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = write_zip(Path(tmp) / "a.zip", [
                ("bun-linux-x64-baseline", b"a", "file", 0o644),
                ("bun-linux-x64-baseline/bun", b"b", "file", 0o644),
            ])
            with self.assertRaisesRegex(ValueError, "is inside file"):
                archive_zip.inventory(archive)

    def test_directory_nested_inside_a_file_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = write_zip(Path(tmp) / "a.zip", [
                ("bun-linux-x64-baseline", b"a", "file", 0o644),
                ("bun-linux-x64-baseline/inner/", None, "dir", 0),
            ])
            with self.assertRaisesRegex(ValueError, "collides with a declared file"):
                archive_zip.inventory(archive)

    def test_file_that_is_also_a_directory_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = write_zip(Path(tmp) / "a.zip", [
                ("bun-linux-x64-baseline/", None, "dir", 0),
                ("bun-linux-x64-baseline", b"a", "file", 0o644),
            ])
            with self.assertRaisesRegex(ValueError, "also declared as a directory"):
                archive_zip.inventory(archive)

    def test_unexpected_second_regular_file_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = write_zip(Path(tmp) / "a.zip", [
                ("bun-linux-x64-baseline/bun", b"a", "file", 0o644),
                ("bun-linux-x64-baseline/extra", b"b", "file", 0o644),
            ])
            members = archive_zip.inventory(archive)
            with self.assertRaisesRegex(ValueError, "unexpected files"):
                archive_zip.select_single_member(members, "bun-linux-x64-baseline/bun")

    def test_archive_with_no_regular_file_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = write_zip(Path(tmp) / "a.zip", [("only/", None, "dir", 0)])
            with self.assertRaisesRegex(ValueError, "no regular file"):
                archive_zip.inventory(archive)

    def test_duplicate_directory_refused(self) -> None:
        """Files and directories share one seen-set, so a repeat is a duplicate."""
        with tempfile.TemporaryDirectory() as tmp:
            with warnings.catch_warnings():
                # Writing the same directory twice is the point of this fixture.
                warnings.simplefilter("ignore", UserWarning)
                archive = write_zip(Path(tmp) / "a.zip", [
                    ("bun-linux-x64-baseline/", None, "dir", 0),
                    ("bun-linux-x64-baseline/", None, "dir", 0),
                    ("bun-linux-x64-baseline/bun", b"a", "file", 0o644),
                ])
            with self.assertRaisesRegex(ValueError, "directory .* more than once"):
                archive_zip.inventory(archive)

    def test_noncanonical_directory_alias_refused(self) -> None:
        """`root//` is the same path as `root/`; only one spelling is canonical."""
        with tempfile.TemporaryDirectory() as tmp:
            archive = write_zip(Path(tmp) / "a.zip", [
                ("bun-linux-x64-baseline//", None, "dir", 0),
                ("bun-linux-x64-baseline/bun", b"a", "file", 0o644),
            ])
            with self.assertRaisesRegex(ValueError, "non-canonical zip directory name"):
                archive_zip.inventory(archive)

    def test_nul_in_the_stored_member_name_refused(self) -> None:
        """zipfile truncates `filename` at NUL, so the original spelling is checked."""
        with self.assertRaisesRegex(ValueError, "unsafe zip member name"):
            inventory_of_names(["bun-linux-x64-baseline/bun\0evil"])

    def test_nul_in_a_stored_directory_name_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsafe zip member name"):
            inventory_of_names(["bun-linux-x64-baseline/\0", "bun-linux-x64-baseline/bun"])

    def test_unrelated_directory_refused_when_a_member_is_selected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = write_zip(Path(tmp) / "a.zip", [
                ("bun-linux-x64-baseline/", None, "dir", 0),
                ("bun-linux-x64-baseline/bun", b"a", "file", 0o644),
                ("unrelated/", None, "dir", 0),
            ])
            with self.assertRaisesRegex(ValueError, "unrelated directories"):
                archive_zip.inventory(archive, "bun-linux-x64-baseline/bun")

    def test_selected_member_prefix_directories_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = upstream_shaped(Path(tmp) / "a.zip")
            (member,) = archive_zip.inventory(archive, "bun-linux-x64-baseline/bun")
            self.assertEqual(member.name, "bun-linux-x64-baseline/bun")


class MemberTypeTest(unittest.TestCase):
    """Only regular files are ever accepted, and "unstated" is not "symlink"."""

    def _refuses(self, kind: str, pattern: str) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = write_zip(Path(tmp) / "a.zip", [
                ("bun-linux-x64-baseline/bun", b"target", kind, 0o777),
            ])
            with self.assertRaisesRegex(ValueError, pattern):
                archive_zip.inventory(archive)

    def test_symlink_member_refused(self) -> None:
        self._refuses("symlink", "non-regular")

    def test_fifo_member_refused(self) -> None:
        self._refuses("fifo", "non-regular")

    def test_character_device_refused(self) -> None:
        self._refuses("chr", "non-regular")

    def test_block_device_refused(self) -> None:
        self._refuses("blk", "non-regular")

    def test_setuid_member_refused(self) -> None:
        self._refuses("setuid", "setuid or setgid")

    def test_setgid_member_refused(self) -> None:
        self._refuses("setgid", "setuid or setgid")

    def test_zero_external_attr_is_a_regular_file(self) -> None:
        """Many writers record no Unix mode at all; that must stay valid."""
        with tempfile.TemporaryDirectory() as tmp:
            archive = write_zip(Path(tmp) / "a.zip", [
                ("bun-linux-x64-baseline/bun", PAYLOAD, "file", 0),
            ])
            (member,) = archive_zip.inventory(archive)
            self.assertEqual(member.name, "bun-linux-x64-baseline/bun")

    def test_encrypted_member_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.zip"
            write_zip(path, [("bun-linux-x64-baseline/bun", PAYLOAD, "file", 0o755)])
            with zipfile.ZipFile(path) as zf:
                infos = zf.infolist()
            raw = bytearray(path.read_bytes())
            for offset in _flag_offsets(raw):
                raw[offset] |= 0x1
            path.write_bytes(bytes(raw))
            del infos
            with self.assertRaisesRegex(ValueError, "encrypted"):
                archive_zip.inventory(path)

    def test_unsupported_compression_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.zip"
            with zipfile.ZipFile(path, "w", zipfile.ZIP_BZIP2) as zf:
                zf.writestr("bun-linux-x64-baseline/bun", PAYLOAD)
            with self.assertRaisesRegex(ValueError, "unsupported compression"):
                archive_zip.inventory(path)


def _flag_offsets(raw: bytes) -> list[int]:
    """Byte offsets of every general-purpose flag field in the ZIP byte stream."""
    offsets = []
    for signature in (b"PK\x03\x04", b"PK\x01\x02"):
        start = 0
        while (index := raw.find(signature, start)) != -1:
            offsets.append(index + (6 if signature == b"PK\x03\x04" else 8))
            start = index + 4
    return offsets


if __name__ == "__main__":
    unittest.main()
