"""The smoke checker is the only place a candidate binary is ever executed.

These tests cover the properties that make that defensible without needing a real
cargo-tauri: the ELF architecture and its linked libraries must be read and
matched *before* anything runs, `--static-only` must be an unconditional execution
disable, and a run must use a private temporary HOME rather than shared storage.

The ELF fixtures here are real 64-bit little-endian files with a loadable segment
and a dynamic segment, so `read_needed` parses the same structures it parses in a
release binary. The run itself is a system binary that prints its own version, so
the exact-output check is exercised against something this repository did not
write.

The probe environment is narrow, not sandboxed: its proxy variables only deter a
proxy-honouring client, and nothing here claims otherwise.
"""

import contextlib
import io
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import tauri_candidate_provenance as prov
from scripts import tauri_candidate_smoke as smoke
from scripts.tauri_candidate_metadata import BINARY, ELF_MACHINE, load_candidate, repo_root

VERSION = "2.12.2"

_ECHO = "/bin/echo"

FAKE_OUTPUT = "tauri-cli 2.12.2"


def write_elf(path: Path, machine: int) -> Path:
    """A 64-bit ELF header with one empty program header: parseable, no segments."""
    header = bytearray(64)
    header[0:4] = b"\x7fELF"
    header[4], header[5] = 2, 1
    struct.pack_into("<H", header, 16, 3)  # ET_DYN
    struct.pack_into("<H", header, 18, machine)
    struct.pack_into("<Q", header, 32, 64)
    struct.pack_into("<HH", header, 54, 56, 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(header) + bytes(56))
    return path


def write_dynamic_elf(
    path: Path, machine: int, sonames: tuple[str, ...], *, needed: tuple[str, ...] | None = None,
    strtab_vaddr: int | None = None, strsz: int | None = None, no_dynamic: bool = False,
) -> Path:
    """A real ELF with one PT_LOAD and a PT_DYNAMIC that names the given libraries.

    `needed` names the libraries the dynamic section actually references, so a
    fixture can repeat one; `strtab_vaddr`, `strsz`, and `no_dynamic` produce the
    malformed cases the reader must refuse.
    """
    header_size, phentsize, phnum = 64, 56, 2
    dyn_offset = header_size + phentsize * phnum
    strtab = b""
    offsets: dict[str, int] = {}
    for name in sonames:
        offsets.setdefault(name, len(strtab))
        strtab += name.encode("latin-1") + b"\0"
    dynamic = b"".join(
        struct.pack("<qQ", 1, offsets[name]) for name in (sonames if needed is None else needed)
    )
    strtab_offset = dyn_offset + len(dynamic) + 48  # + DT_STRTAB, DT_STRSZ, DT_NULL
    dynamic += struct.pack("<qQ", 5, strtab_offset if strtab_vaddr is None else strtab_vaddr)
    dynamic += struct.pack("<qQ", 10, len(strtab) if strsz is None else strsz)
    dynamic += struct.pack("<qQ", 0, 0)
    total = strtab_offset + len(strtab)
    phdrs = struct.pack("<IIQQQQQQ", 1, 4, 0, 0, 0, total, total, 4096)
    if not no_dynamic:
        phdrs += struct.pack("<IIQQQQQQ", 2, 6, dyn_offset, dyn_offset, 0, len(dynamic),
                             len(dynamic), 8)
    header = bytearray(header_size)
    header[0:4] = b"\x7fELF"
    header[4], header[5] = 2, 1
    struct.pack_into("<H", header, 16, 3)
    struct.pack_into("<H", header, 18, machine)
    struct.pack_into("<Q", header, 32, header_size)
    struct.pack_into("<HH", header, 54, phentsize, phnum)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(header) + phdrs + dynamic + strtab)
    return path


def write_fake_cli(path: Path, output: str = FAKE_OUTPUT) -> Path:
    """An executable that prints one line and does nothing else."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{output}'\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def write_versioned_elf(
    path: Path, machine: int, sonames: tuple[str, ...],
    versions: tuple[tuple[str, tuple[str, ...]], ...],
    needed: tuple[str, ...] | None = None,
) -> Path:
    """An ELF that links `sonames` and requires the given symbol versions.

    `versions` maps a library name to the version names it must provide, written
    as the version-requirement records a real toolchain emits. `needed` overrides
    which libraries the dynamic section actually lists, so a fixture can require
    versions from a library it does not declare as linked.
    """
    header_size, phentsize, phnum = 64, 56, 2
    dyn_offset = header_size + phentsize * phnum
    strtab = b""
    offsets: dict[str, int] = {}
    for name in (
        *sonames,
        *(library for library, _ in versions),
        *(name for _, names in versions for name in names),
    ):
        if name not in offsets:
            offsets[name] = len(strtab)
            strtab += name.encode("latin-1") + b"\0"
    verneed = bytearray()
    records: list[tuple[int, int]] = []
    for library, names in versions:
        aux = b"".join(
            struct.pack("<IHHII", 0, 0, 1, offsets[name],
                        16 if position + 1 < len(names) else 0)
            for position, name in enumerate(names)
        )
        record = struct.pack("<HHIII", 1, len(names), offsets[library], 16, 0) + aux
        records.append((len(verneed), len(record)))
        verneed += record
    for index, (offset, _size) in enumerate(records):
        struct.pack_into("<I", verneed, offset + 12,
                         0 if index + 1 == len(records) else records[index + 1][0] - offset)
    verneed = bytes(verneed)
    dynamic = b"".join(
        struct.pack("<qQ", 1, offsets[name])
        for name in (sonames if needed is None else needed)
    )
    strtab_offset = dyn_offset + len(dynamic) + 80
    dynamic += struct.pack("<qQ", 5, strtab_offset)
    dynamic += struct.pack("<qQ", 10, len(strtab))
    dynamic += struct.pack("<qQ", 0x6FFFFFFE, strtab_offset + len(strtab))
    dynamic += struct.pack("<qQ", 0x6FFFFFFF, len(versions))
    dynamic += struct.pack("<qQ", 0, 0)
    total = strtab_offset + len(strtab) + len(verneed)
    phdrs = struct.pack("<IIQQQQQQ", 1, 4, 0, 0, 0, total, total, 4096)
    phdrs += struct.pack("<IIQQQQQQ", 2, 6, dyn_offset, dyn_offset, 0, len(dynamic),
                         len(dynamic), 8)
    header = bytearray(header_size)
    header[0:4] = b"\x7fELF"
    header[4], header[5] = 2, 1
    struct.pack_into("<H", header, 16, 3)
    struct.pack_into("<H", header, 18, machine)
    struct.pack_into("<Q", header, 32, header_size)
    struct.pack_into("<HH", header, 54, phentsize, phnum)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(header) + phdrs + dynamic + strtab + verneed)
    return path


class ReadNeededTest(unittest.TestCase):
    def test_reads_the_named_libraries_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_dynamic_elf(
                Path(tmp) / BINARY, ELF_MACHINE["amd64"],
                ("liblzma.so.5", "libgcc_s.so.1", "libc.so.6"),
            )
            self.assertEqual(
                smoke.read_needed(path), ("liblzma.so.5", "libgcc_s.so.1", "libc.so.6"),
            )

    def test_a_repeated_library_is_reported_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_dynamic_elf(
                Path(tmp) / BINARY, ELF_MACHINE["arm64"], ("libc.so.6",),
                needed=("libc.so.6", "libc.so.6"),
            )
            self.assertEqual(smoke.read_needed(path), ("libc.so.6",))

    def test_a_file_without_a_dynamic_segment_links_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_dynamic_elf(
                Path(tmp) / BINARY, ELF_MACHINE["amd64"], ("libc.so.6",), no_dynamic=True,
            )
            self.assertEqual(smoke.read_needed(path), ())

    def test_a_non_elf_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / BINARY
            path.write_bytes(b"#!/bin/sh\n")
            with self.assertRaisesRegex(ValueError, "not an ELF file"):
                smoke.read_needed(path)

    def test_a_32_bit_elf_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / BINARY
            header = bytearray(64)
            header[0:4] = b"\x7fELF"
            header[4] = 1
            path.write_bytes(bytes(header))
            with self.assertRaisesRegex(ValueError, "64-bit little-endian"):
                smoke.read_needed(path)

    def test_a_file_without_program_headers_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_elf(Path(tmp) / BINARY, ELF_MACHINE["amd64"])
            raw = bytearray(path.read_bytes())
            struct.pack_into("<H", raw, 56, 0)
            path.write_bytes(bytes(raw))
            with self.assertRaisesRegex(ValueError, "no usable program header table"):
                smoke.read_needed(path)

    def test_a_truncated_string_table_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_dynamic_elf(
                Path(tmp) / BINARY, ELF_MACHINE["amd64"], ("libc.so.6",), strsz=4096,
            )
            with self.assertRaisesRegex(ValueError, "outside its loadable file data"):
                smoke.read_needed(path)

    def test_a_string_table_in_the_memory_only_tail_is_refused(self) -> None:
        """`p_memsz` may exceed the file; a structure may not live in that gap."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / BINARY
            header = bytearray(64)
            header[0:4] = b"\x7fELF"
            header[4], header[5] = 2, 1
            struct.pack_into("<Q", header, 32, 64)
            struct.pack_into("<HH", header, 54, 56, 2)
            strings = b"\0libc.so.6\0"
            dyn_offset = 64 + 2 * 56
            str_offset = dyn_offset + 5 * 16
            dynamic = b"".join(struct.pack("<qQ", tag, value) for tag, value in (
                (1, 1), (5, str_offset + 8192), (10, len(strings)), (0, 0),
            ))
            total = str_offset + len(strings)
            phdrs = struct.pack("<IIQQQQQQ", 1, 4, 0, 0, 0, total, total + 8192, 4096)
            phdrs += struct.pack("<IIQQQQQQ", 2, 6, dyn_offset, dyn_offset, 0, len(dynamic),
                                 len(dynamic), 8)
            path.write_bytes(bytes(header) + phdrs + dynamic + strings)
            with self.assertRaisesRegex(ValueError, "outside its loadable file data"):
                smoke.read_needed(path)

    def test_a_duplicate_string_table_entry_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / BINARY
            data = bytearray(write_dynamic_elf(
                Path(tmp) / BINARY, ELF_MACHINE["amd64"], ("libc.so.6",),
            ).read_bytes())
            dyn_offset = 64 + 2 * 56
            struct.pack_into("<qQ", data, dyn_offset + 3 * 16, 5, 176)  # a second DT_STRTAB
            path.write_bytes(bytes(data))
            with self.assertRaisesRegex(ValueError, "exactly one DT_STRTAB"):
                smoke.read_needed(path)

    def test_a_library_name_with_a_control_byte_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_dynamic_elf(
                Path(tmp) / BINARY, ELF_MACHINE["amd64"], ("lib\x01c.so.6",),
            )
            with self.assertRaisesRegex(ValueError, "control byte"):
                smoke.read_needed(path)


class SymbolVersionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.candidate = load_candidate()

    def test_reads_the_required_versions_per_library(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_versioned_elf(
                Path(tmp) / BINARY, ELF_MACHINE["amd64"], ("libc.so.6", "liblzma.so.5"),
                (("libc.so.6", ("GLIBC_2.2.5", "GLIBC_2.34")), ("liblzma.so.5", ("XZ_5.0",))),
            )
            self.assertEqual(
                smoke.read_symbol_versions(path),
                (("libc.so.6", "GLIBC_2.2.5"), ("libc.so.6", "GLIBC_2.34"),
                 ("liblzma.so.5", "XZ_5.0")),
            )

    def test_a_binary_without_version_requirements_reports_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_dynamic_elf(Path(tmp) / BINARY, ELF_MACHINE["amd64"], ("libc.so.6",))
            self.assertEqual(smoke.read_symbol_versions(path), ())

    def test_private_namespaces_carry_no_floor(self) -> None:
        self.assertIsNone(smoke.symbol_floor(("GLIBC_PRIVATE",), "GLIBC_"))
        self.assertEqual(smoke.symbol_floor(("GLIBC_2.34", "GLIBC_2.4"), "GLIBC_"), (2, 34))

    def test_a_name_outside_the_namespace_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "outside the reviewed GLIBC_ namespace"):
            smoke.symbol_floor(("XZ_5.0",), "GLIBC_")

    def test_a_name_without_a_version_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not carry a version number"):
            smoke.symbol_floor(("GLIBC_tip",), "GLIBC_")

    def test_a_floor_at_or_below_the_mapping_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_versioned_elf(
                Path(tmp) / BINARY, ELF_MACHINE["arm64"], ("libc.so.6", "libm.so.6"),
                (("libc.so.6", ("GLIBC_2.34",)), ("libm.so.6", ("GLIBC_2.29",))),
            )
            lines = smoke.check_runtime(path, "arm64", self.candidate)
            self.assertTrue(any("libc6 2.34 (declared >= 2.34)" in line for line in lines))

    def test_a_floor_above_the_declared_mapping_is_refused(self) -> None:
        """A newer glibc than the candidate declares is never widened silently."""
        with tempfile.TemporaryDirectory() as tmp:
            path = write_versioned_elf(
                Path(tmp) / BINARY, ELF_MACHINE["arm64"], ("libc.so.6",),
                (("libc.so.6", ("GLIBC_2.40",)),),
            )
            with self.assertRaisesRegex(ValueError, "which the reviewed mapping does not declare"):
                smoke.check_runtime(path, "arm64", self.candidate)

    def test_a_lower_floor_than_the_mapping_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_versioned_elf(
                Path(tmp) / BINARY, ELF_MACHINE["amd64"], ("libc.so.6",),
                (("libc.so.6", ("GLIBC_2.17",)),),
            )
            smoke.check_runtime(path, "amd64", self.candidate)

    def test_versions_from_an_unmapped_library_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_versioned_elf(
                Path(tmp) / BINARY, ELF_MACHINE["amd64"], ("libc.so.6", "libglib-2.0.so.0"),
                (("libglib-2.0.so.0", ("GLIBC_2.14",)),),
            )
            with self.assertRaisesRegex(ValueError, "does not cover"):
                smoke.check_runtime(path, "amd64", self.candidate)


class BoundedReadTest(unittest.TestCase):
    """Every declared range is bounded by the file and a ceiling before it is read."""

    def test_a_program_table_outside_the_file_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_elf(Path(tmp) / BINARY, ELF_MACHINE["amd64"])
            raw = bytearray(path.read_bytes())
            struct.pack_into("<Q", raw, 32, 1 << 20)  # e_phoff past the end
            path.write_bytes(bytes(raw))
            with self.assertRaisesRegex(ValueError, "outside its"):
                smoke.read_needed(path)

    def test_an_oversized_program_table_is_refused_before_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_elf(Path(tmp) / BINARY, ELF_MACHINE["amd64"])
            raw = bytearray(path.read_bytes())
            struct.pack_into("<H", raw, 56, 4096)  # e_phnum, far past the ceiling
            path.write_bytes(bytes(raw))
            with self.assertRaisesRegex(ValueError, "over the 65536 byte limit"):
                smoke.read_needed(path)

    def test_a_dynamic_segment_past_the_file_is_refused_before_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_dynamic_elf(
                Path(tmp) / BINARY, ELF_MACHINE["amd64"], ("libc.so.6",),
            )
            raw = bytearray(path.read_bytes())
            struct.pack_into("<Q", raw, 64 + 56 + 32, 1 << 30)  # PT_DYNAMIC p_filesz
            path.write_bytes(bytes(raw))
            with self.assertRaisesRegex(ValueError, "over the 1048576 byte limit"):
                smoke.read_symbol_versions(path)

    def test_a_dynamic_segment_beyond_the_file_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_dynamic_elf(
                Path(tmp) / BINARY, ELF_MACHINE["amd64"], ("libc.so.6",),
            )
            raw = bytearray(path.read_bytes())
            struct.pack_into("<Q", raw, 64 + 56 + 32, 4096)  # larger than the file
            path.write_bytes(bytes(raw))
            with self.assertRaisesRegex(ValueError, "outside its"):
                smoke.read_symbol_versions(path)


class VersionChainTest(unittest.TestCase):
    """A truncated chain hides the requirement that decides the ABI floor."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.candidate = load_candidate()
        self.path = write_versioned_elf(
            self.root / BINARY, ELF_MACHINE["arm64"], ("libc.so.6",),
            (("libc.so.6", ("GLIBC_2.34",)),),
        )
        self.dynamic = 64 + 2 * 56

    def patched(self, offset: int, value: int, fmt: str) -> None:
        raw = bytearray(self.path.read_bytes())
        struct.pack_into(fmt, raw, offset, value)
        self.path.write_bytes(bytes(raw))

    def tag_offset(self, index: int) -> int:
        """The value field of one dynamic entry: NEEDED, STRTAB, STRSZ, VERNEED, …"""
        return self.dynamic + index * 16 + 8

    def test_a_record_list_without_a_count_is_refused(self) -> None:
        self.patched(self.tag_offset(4), 0, "<Q")  # DT_VERNEEDNUM = 0
        with self.assertRaisesRegex(ValueError, "without DT_VERNEEDNUM"):
            smoke.read_symbol_versions(self.path)

    def test_a_count_without_a_record_list_is_refused(self) -> None:
        self.patched(self.tag_offset(3), 0, "<Q")  # DT_VERNEED = 0
        with self.assertRaisesRegex(ValueError, "without DT_VERNEED"):
            smoke.read_symbol_versions(self.path)

    def version_offset(self) -> int:
        raw = bytearray(self.path.read_bytes())
        return struct.unpack_from("<Q", raw, self.tag_offset(3))[0]

    def test_more_records_than_the_count_is_refused(self) -> None:
        self.patched(self.version_offset() + 12, 32, "<I")  # vn_next on the only record
        with self.assertRaisesRegex(ValueError, "more version-requirement records"):
            smoke.read_symbol_versions(self.path)

    def test_fewer_records_than_the_count_is_refused(self) -> None:
        self.patched(self.tag_offset(4), 2, "<Q")  # claim two records
        with self.assertRaisesRegex(ValueError, "ends its version-requirement records"):
            smoke.read_symbol_versions(self.path)

    def test_more_auxiliary_versions_than_declared_is_refused(self) -> None:
        self.patched(self.version_offset() + 16 + 12, 16, "<I")  # vna_next after vn_cnt = 1
        with self.assertRaisesRegex(ValueError, "more auxiliary versions"):
            smoke.read_symbol_versions(self.path)

    def test_an_auxiliary_pointer_before_the_record_is_refused(self) -> None:
        self.patched(self.version_offset() + 8, 8, "<I")  # vn_aux inside the header
        with self.assertRaisesRegex(ValueError, "auxiliary records at offset"):
            smoke.read_symbol_versions(self.path)

    def test_an_unreadable_revision_is_refused(self) -> None:
        self.patched(self.version_offset(), 7, "<H")  # vn_version
        with self.assertRaisesRegex(ValueError, "unsupported version-requirement revision"):
            smoke.read_symbol_versions(self.path)

    def test_an_oversized_record_count_is_refused(self) -> None:
        self.patched(self.tag_offset(4), _MAX + 1, "<Q")
        with self.assertRaisesRegex(ValueError, "version-requirement records"):
            smoke.read_symbol_versions(self.path)


_MAX = smoke._MAX_VERSION_RECORDS


class UnversionedLibraryTest(unittest.TestCase):
    """A library reviewed as unversioned: no namespace, but not an unchecked pass."""

    def setUp(self) -> None:
        self.candidate = load_candidate()

    def test_an_unversioned_library_without_versions_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_versioned_elf(
                Path(tmp) / BINARY, ELF_MACHINE["arm64"],
                ("libbz2.so.1.0", "libc.so.6"),
                (("libc.so.6", ("GLIBC_2.34",)),),
            )
            lines = smoke.check_runtime(path, "arm64", self.candidate)
            self.assertTrue(any("declares no symbol versions" in line for line in lines))

    def test_a_version_claimed_from_an_unversioned_library_is_refused(self) -> None:
        """The mapping names a library that exports no versions; this is another."""
        with tempfile.TemporaryDirectory() as tmp:
            path = write_versioned_elf(
                Path(tmp) / BINARY, ELF_MACHINE["arm64"], ("libbz2.so.1.0",),
                (("libbz2.so.1.0", ("BZ2_1.0",)),),
            )
            with self.assertRaisesRegex(ValueError, "records that library as unversioned"):
                smoke.check_runtime(path, "arm64", self.candidate)

    def test_a_versioned_library_still_requires_its_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_versioned_elf(
                Path(tmp) / BINARY, ELF_MACHINE["amd64"], ("libc.so.6",),
                (("libc.so.6", ("BZ2_1.0",)),),
            )
            with self.assertRaisesRegex(ValueError, "outside the reviewed GLIBC_ namespace"):
                smoke.check_runtime(path, "amd64", self.candidate)

    def test_an_unreviewed_library_is_still_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_versioned_elf(
                Path(tmp) / BINARY, ELF_MACHINE["amd64"], ("libz.so.1",),
                (),
            )
            with self.assertRaisesRegex(ValueError, "does not cover"):
                smoke.check_runtime(path, "amd64", self.candidate)


class VerneedOwnerTest(unittest.TestCase):
    """A version requirement must belong to a library the binary says it links.

    An orphan record is the shape that used to pass: its owner never entered the
    measurement loop, so a requirement nobody inspected slipped past every rule.
    """

    def setUp(self) -> None:
        self.candidate = load_candidate()

    def orphan(self, tmp: str, versions, needed=("libc.so.6",)) -> Path:
        return write_versioned_elf(
            Path(tmp) / BINARY, ELF_MACHINE["amd64"], ("libc.so.6",), versions, needed=needed,
        )

    def test_a_requirement_from_an_undeclared_library_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.orphan(
                tmp, (("liblzma.so.5", ("XZ_5.0",)), ("libc.so.6", ("GLIBC_2.34",))),
            )
            self.assertEqual(smoke.read_needed(path), ("libc.so.6",))
            self.assertIn(("liblzma.so.5", "XZ_5.0"), smoke.read_symbol_versions(path))
            with self.assertRaisesRegex(ValueError, "does not list"):
                smoke.check_runtime(path, "amd64", self.candidate)

    def test_a_requirement_from_an_undeclared_unversioned_library_is_refused(self) -> None:
        """The reviewed mapping covers libbz2; that does not excuse a lost NEEDED."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self.orphan(
                tmp, (("libbz2.so.1.0", ("BZ2_1.0",)), ("libc.so.6", ("GLIBC_2.34",))),
            )
            with self.assertRaises(ValueError):
                smoke.check_runtime(path, "amd64", self.candidate)

    def test_the_owner_check_runs_before_the_unreviewed_library_check(self) -> None:
        """An orphan the mapping has never seen is still reported as an orphan."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self.orphan(
                tmp, (("libz.so.1", ("ZLIB_1.2.9",)), ("libc.so.6", ("GLIBC_2.34",))),
            )
            with self.assertRaisesRegex(ValueError, "does not list"):
                smoke.check_runtime(path, "amd64", self.candidate)

    def test_a_declared_library_is_still_measured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.orphan(
                tmp, (("libc.so.6", ("GLIBC_2.34",)),), needed=("libc.so.6", "libc.so.6"),
            )
            lines = smoke.check_runtime(path, "amd64", self.candidate)
            self.assertTrue(any("libc6 2.34" in line for line in lines))


class PayloadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.candidate = load_candidate()

    def stage(self, root: Path, *, machine: int, sonames: tuple[str, ...]) -> Path:
        bin_dir = root / "usr" / "bin"
        write_dynamic_elf(bin_dir / BINARY, machine, sonames)
        (bin_dir / BINARY).chmod(0o755)
        return bin_dir

    def test_a_payload_whose_libraries_are_all_reviewed_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self.stage(
                Path(tmp), machine=ELF_MACHINE["amd64"],
                sonames=("libc.so.6", "libm.so.6", "libgcc_s.so.1", "liblzma.so.5"),
            )
            lines = smoke.check_payload(bin_dir, "amd64", self.candidate)
            self.assertIn(f"{BINARY}: ELF 64-bit amd64, mode 0755", lines)

    def test_a_library_outside_the_reviewed_mapping_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self.stage(
                Path(tmp), machine=ELF_MACHINE["arm64"],
                sonames=("libc.so.6", "libwebkit2gtk-4.1.so.0"),
            )
            with self.assertRaisesRegex(ValueError, "does not cover"):
                smoke.check_payload(bin_dir, "arm64", self.candidate)

    def test_an_unused_reviewed_library_is_reported_not_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self.stage(Path(tmp), machine=ELF_MACHINE["amd64"], sonames=("libc.so.6",))
            lines = smoke.check_payload(bin_dir, "amd64", self.candidate)
            self.assertTrue(any("not used by this binary" in line for line in lines))

    def test_a_foreign_architecture_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self.stage(Path(tmp), machine=ELF_MACHINE["arm64"], sonames=("libc.so.6",))
            with self.assertRaisesRegex(ValueError, "requires 62"):
                smoke.check_payload(bin_dir, "amd64", self.candidate)

    def test_a_wrong_mode_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self.stage(Path(tmp), machine=ELF_MACHINE["amd64"], sonames=("libc.so.6",))
            os.chmod(bin_dir / BINARY, 0o700)
            with self.assertRaisesRegex(ValueError, "expected 755"):
                smoke.check_payload(bin_dir, "amd64", self.candidate)

    def test_a_symlinked_binary_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self.stage(Path(tmp), machine=ELF_MACHINE["amd64"], sonames=("libc.so.6",))
            target = bin_dir / BINARY
            real = target.read_bytes()
            target.unlink()
            target.symlink_to(bin_dir / "elsewhere")
            (bin_dir / "elsewhere").write_bytes(real)
            with self.assertRaisesRegex(ValueError, "not a regular file"):
                smoke.check_payload(bin_dir, "amd64", self.candidate)


class DocumentTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.candidate = load_candidate()
        self.binary = write_elf(self.root / "asset" / BINARY, ELF_MACHINE["amd64"])
        self.doc = self.root / "usr" / "share" / "doc" / BINARY
        self.doc.mkdir(parents=True)
        licenses = repo_root() / "packaging" / "licenses" / "tauri-cli"
        for name in ("LICENSE-MIT.txt", "LICENSE-APACHE-2.0.txt"):
            shutil.copyfile(licenses / name, self.doc / name)
        shutil.copyfile(repo_root() / "packaging" / "tauri-cli" / "README.source",
                        self.doc / "README.source")
        for name in ("copyright", "changelog.Debian"):
            (self.doc / name).write_text("fixture\n", encoding="utf-8")
        record = prov.emit_official_asset(
            self.candidate, self.binary,
            {name: licenses / name for name in ("LICENSE-MIT.txt", "LICENSE-APACHE-2.0.txt")},
        )
        prov.write_record(self.doc / prov.OFFICIAL_ASSET_NAME, record)

    def test_a_complete_document_set_passes(self) -> None:
        lines = smoke.check_documents(self.doc, "amd64", self.candidate, repo_root())
        self.assertTrue(any("official release asset" in line for line in lines))

    def test_a_missing_notice_is_refused(self) -> None:
        (self.doc / "changelog.Debian").unlink()
        with self.assertRaisesRegex(ValueError, "installed notice .* is missing"):
            smoke.check_documents(self.doc, "amd64", self.candidate, repo_root())

    def test_an_altered_licence_text_is_refused(self) -> None:
        (self.doc / "LICENSE-MIT.txt").write_text("MIT License\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "not the reviewed upstream licence text"):
            smoke.check_documents(self.doc, "amd64", self.candidate, repo_root())

    def test_an_altered_source_document_is_refused(self) -> None:
        (self.doc / "README.source").write_text("# replaced\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "does not state the reviewed"):
            smoke.check_documents(self.doc, "amd64", self.candidate, repo_root())

    def test_the_other_architectures_record_kind_is_refused(self) -> None:
        """An official-asset record under the arm64 name is not a source-build record."""
        record = prov.read_record(self.doc / prov.OFFICIAL_ASSET_NAME)
        prov.write_record(self.doc / prov.SOURCE_BUILD_NAME, {**record, "arch": "arm64"})
        (self.doc / prov.OFFICIAL_ASSET_NAME).unlink()
        with self.assertRaisesRegex(ValueError, "missing keys"):
            smoke.check_documents(self.doc, "arm64", self.candidate, repo_root())


class ControlTest(unittest.TestCase):
    def setUp(self) -> None:
        self.candidate = load_candidate()
        self.fields = {
            "Package": BINARY,
            "Version": f"{VERSION}-1",
            "Architecture": "amd64",
            "Depends": self.candidate.depends(),
        }

    def test_matching_fields_pass(self) -> None:
        lines = smoke.check_control(self.fields, "amd64", self.candidate)
        self.assertTrue(any("Depends" in line for line in lines))

    def test_another_version_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "control Version"):
            smoke.check_control({**self.fields, "Version": "2.12.1-1"}, "amd64", self.candidate)

    def test_another_architecture_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "control Architecture"):
            smoke.check_control(self.fields, "arm64", self.candidate)

    def test_dependencies_outside_the_reviewed_mapping_refused(self) -> None:
        fields = {**self.fields, "Depends": "libc6 (>= 2.34)"}
        with self.assertRaisesRegex(ValueError, "control Depends"):
            smoke.check_control(fields, "amd64", self.candidate)


class NativeCheckTest(unittest.TestCase):
    def test_the_direct_version_is_compared_with_the_pinned_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cli = write_fake_cli(home / "cargo-tauri")
            lines = smoke.native_checks(cli, VERSION, home, None, require_cargo=False)
            self.assertEqual(lines[0], f"{BINARY} --version -> {FAKE_OUTPUT}")
            self.assertTrue(any("skipped" in line for line in lines))

    def test_an_unexpected_version_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cli = write_fake_cli(home / "cargo-tauri", "tauri-cli 9.9.9")
            with self.assertRaisesRegex(ValueError, "printed 'tauri-cli 9.9.9', expected"):
                smoke.native_checks(cli, VERSION, home, None, require_cargo=False)

    def test_a_failing_binary_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cli = home / "cargo-tauri"
            cli.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
            cli.chmod(0o755)
            with self.assertRaisesRegex(ValueError, "exited 3"):
                smoke.native_checks(cli, VERSION, home, None, require_cargo=False)

    def test_a_missing_cargo_is_skipped_or_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cli = write_fake_cli(home / "cargo-tauri")
            lines = smoke.native_checks(cli, VERSION, home, None, require_cargo=False)
            self.assertTrue(any("skipped" in line for line in lines))
            with self.assertRaisesRegex(ValueError, "needs a cargo on PATH"):
                smoke.native_checks(cli, VERSION, home, None, require_cargo=True)

    def test_a_cargo_that_would_resolve_another_binary_is_refused(self) -> None:
        """The probe must run this package's cargo-tauri, not one already installed."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "usr" / "bin"
            cargo_dir = root / "cargo" / "bin"
            bin_dir.mkdir(parents=True)
            cargo_dir.mkdir(parents=True)
            for directory in (bin_dir, cargo_dir):
                script = directory / BINARY
                script.write_text("#!/bin/sh\n", encoding="utf-8")
                script.chmod(0o755)
                cargo = directory / "cargo"
                cargo.write_text("#!/bin/sh\n", encoding="utf-8")
                cargo.chmod(0o755)
            os.chmod(bin_dir / BINARY, 0o644)  # not executable: cargo would find the other one
            with mock.patch.object(smoke, "run_probe", return_value=FAKE_OUTPUT):
                with self.assertRaisesRegex(ValueError, "would not resolve"):
                    smoke.native_checks(bin_dir / BINARY, VERSION, root, str(cargo_dir / "cargo"),
                                        require_cargo=True)

    def test_a_cargo_run_reports_its_own_exact_output(self) -> None:
        seen: list[tuple] = []

        def run(command, home, *, path=None):
            seen.append((command, path))
            return smoke.version_output(VERSION)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "usr" / "bin"
            cargo_dir = root / "cargo" / "bin"
            bin_dir.mkdir(parents=True)
            cargo_dir.mkdir(parents=True)
            for name in (BINARY, "cargo"):
                (bin_dir / name).write_text("#!/bin/sh\n", encoding="utf-8")
                (bin_dir / name).chmod(0o755)
            cargo = cargo_dir / "cargo"
            cargo.write_text("#!/bin/sh\n", encoding="utf-8")
            cargo.chmod(0o755)
            with mock.patch.object(smoke, "run_probe", side_effect=run):
                lines = smoke.native_checks(bin_dir / BINARY, VERSION, root, str(cargo),
                                            require_cargo=True)
        self.assertEqual(seen[0][0], [str(bin_dir / BINARY), "--version"])
        self.assertEqual(seen[1][0], [str(cargo), "tauri", "--version"])
        self.assertEqual(seen[1][1], f"{bin_dir}:{cargo_dir}")
        self.assertTrue(any("external subcommand" in line for line in lines))


class ExecutionDisableTest(unittest.TestCase):
    def setUp(self) -> None:
        self.candidate = load_candidate()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.doc = self.root / "usr" / "share" / "doc" / BINARY
        self.doc.mkdir(parents=True)
        licenses = repo_root() / "packaging" / "licenses" / "tauri-cli"
        for name in ("LICENSE-MIT.txt", "LICENSE-APACHE-2.0.txt"):
            shutil.copyfile(licenses / name, self.doc / name)
        shutil.copyfile(repo_root() / "packaging" / "tauri-cli" / "README.source",
                        self.doc / "README.source")
        for name in ("copyright", "changelog.Debian"):
            (self.doc / name).write_text("fixture\n", encoding="utf-8")
        binary = write_elf(self.root / "asset" / BINARY, ELF_MACHINE["amd64"])
        prov.write_record(
            self.doc / prov.OFFICIAL_ASSET_NAME,
            prov.emit_official_asset(
                self.candidate, binary,
                {name: licenses / name for name in ("LICENSE-MIT.txt", "LICENSE-APACHE-2.0.txt")},
            ),
        )
        bin_dir = self.root / "usr" / "bin"
        write_dynamic_elf(bin_dir / BINARY, ELF_MACHINE["amd64"], ("libc.so.6",))
        (bin_dir / BINARY).chmod(0o755)
        self.fields = {
            "Package": BINARY, "Version": f"{VERSION}-1", "Architecture": "amd64",
            "Depends": self.candidate.depends(),
        }

    def test_static_only_never_starts_a_probe(self) -> None:
        with mock.patch.object(smoke, "native_arch", return_value="amd64"), \
             mock.patch.object(smoke, "run_probe") as run:
            lines = smoke.smoke(
                self.root, "amd64", VERSION, self.candidate, self.fields,
                allow_execution=False,
            )
        run.assert_not_called()
        self.assertTrue(any("execution disabled" in line for line in lines))

    def _foreign_arch(self) -> str:
        """The architecture that is not this machine, with its payload re-staged."""
        foreign = "arm64" if smoke.native_arch() != "arm64" else "amd64"
        binary = self.root / "usr" / "bin" / BINARY
        write_dynamic_elf(binary, ELF_MACHINE[foreign], ("libc.so.6",))
        binary.chmod(0o755)
        self.fields = {**self.fields, "Architecture": foreign}
        return foreign

    def test_a_foreign_machine_is_skipped_rather_than_executed(self) -> None:
        foreign = self._foreign_arch()
        with mock.patch.object(smoke, "run_probe") as run, \
             mock.patch.object(smoke, "check_documents", return_value=[]):
            lines = smoke.smoke(
                self.root, foreign, VERSION, self.candidate, self.fields, require_native=False,
            )
        run.assert_not_called()
        self.assertTrue(any("native run skipped" in line for line in lines))

    def test_require_native_refuses_on_a_foreign_machine(self) -> None:
        foreign = self._foreign_arch()
        with mock.patch.object(smoke, "check_documents", return_value=[]):
            with self.assertRaisesRegex(ValueError, "native smoke needs"):
                smoke.smoke(self.root, foreign, VERSION, self.candidate, self.fields,
                            require_native=True)

    def test_contradicting_flags_are_rejected_by_the_cli(self) -> None:
        for arguments in (["--static-only", "--require-native"], ["--static-only", "--require-cargo"]):
            with contextlib.redirect_stderr(io.StringIO()) as captured:
                with self.assertRaises(SystemExit):
                    smoke.main(["--installed", "--arch", "amd64", *arguments])
            self.assertIn("contradicts", captured.getvalue())


class IsolationTest(unittest.TestCase):
    def test_probes_run_in_a_private_home_with_deterent_proxies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(smoke.subprocess, "run") as run:
                run.return_value = subprocess.CompletedProcess([], 0, "tauri-cli 2.12.2\n", "")
                smoke.run_probe([_ECHO], Path(tmp), path="/usr/bin")
            env, kwargs = run.call_args.kwargs["env"], run.call_args.kwargs
            self.assertEqual(env["HOME"], tmp)
            self.assertEqual(env["PATH"], "/usr/bin")
            self.assertEqual(Path(kwargs["cwd"]), Path(tmp))
            self.assertEqual(kwargs["timeout"], smoke.TIMEOUT_SECONDS)
            self.assertTrue(env["HTTPS_PROXY"].startswith("http://127.0.0.1:"))
            self.assertNotIn("GITHUB_TOKEN", env)

    def test_the_timeout_bound_is_enforced(self) -> None:
        original = smoke.TIMEOUT_SECONDS
        smoke.TIMEOUT_SECONDS = 1
        try:
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaisesRegex(ValueError, "exceeded the 1s bound"):
                    smoke.run_probe(["/bin/sleep", "30"], Path(tmp))
        finally:
            smoke.TIMEOUT_SECONDS = original


if __name__ == "__main__":
    unittest.main()
