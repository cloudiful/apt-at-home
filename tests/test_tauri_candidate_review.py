"""Independent issue 69 regressions: artifact layout, tool PATH, and ABI floors."""
import os
import re
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import tauri_candidate as build
from scripts import tauri_candidate_provenance as prov
from scripts.tauri_candidate_metadata import BINARY, ELF_MACHINE, load_candidate
from tests.test_tauri_candidate_provenance import (
    CARGO_LINE, RUSTC_LINE, checkout, git_answers, write_elf,
)
from tests.test_tauri_candidate_workflow import job_block


class CandidateReviewTest(unittest.TestCase):
    def test_arm_upload_layout_matches_apt_install_path(self):
        upload = job_block("tauri-candidate-arm64").split("actions/upload-artifact@")[1]
        paths = re.search(r"          path: \|\n((?:            .+\n)+)", upload)
        self.assertIsNotNone(paths)
        inputs = [Path(line.strip()) for line in paths.group(1).splitlines()]
        # upload-artifact v4 uses the least common ancestor of multiple search paths.
        root = Path(os.path.commonpath([str(path.parent) for path in inputs]))
        uploaded_deb = inputs[0].relative_to(root)
        downloaded_deb = Path("dist/tauri-candidate") / uploaded_deb
        expected = Path("dist/tauri-candidate/cargo-tauri_2.12.2-1_arm64.deb")
        self.assertEqual(downloaded_deb, expected)

    def test_installer_step_makes_tools_available_before_probing(self):
        block = job_block("tauri-candidate-arm64")
        step = block.split("- name: Install the pinned Rust toolchain")[1].split("- name:")[0]
        before_probe = step.split("rustc -Vv")[0]
        # GITHUB_PATH applies to later steps, not the current shell. --no-modify-path
        # requires an explicit export (or probing the absolute installed tool).
        self.assertRegex(before_probe, r'export PATH=.*(?:CARGO_HOME|cargo-home).*/bin')

    def test_wrong_full_length_lock_digest_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate = load_candidate()
            upstream = checkout(root)
            binary = write_elf(root / BINARY, ELF_MACHINE["arm64"])
            with mock.patch.object(prov, "_git_output", side_effect=git_answers()):
                record = prov.emit_source_build(
                    candidate, upstream, binary, rustc=RUSTC_LINE, cargo=CARGO_LINE,
                )
            record["source"]["lockfile"]["sha256"] = "0" * 64
            with self.assertRaises(ValueError):
                prov.check_source_build(record, candidate)

    def test_arm_glibc_requirement_above_declared_floor_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / BINARY
            # ELF64 LE with PT_LOAD, PT_DYNAMIC and one libc GLIBC_2.40 verneed.
            strings = b"\0libc.so.6\0GLIBC_2.40\0"
            dyn_offset = 64 + 2 * 56
            str_offset = dyn_offset + 6 * 16
            ver_offset = str_offset + len(strings)
            dynamic = b"".join(struct.pack("<qQ", tag, value) for tag, value in (
                (1, 1), (5, str_offset), (10, len(strings)),
                (0x6ffffffe, ver_offset), (0x6fffffff, 1), (0, 0),
            ))
            # Elf64_Verneed plus Elf64_Vernaux. Version name is string offset 11.
            version = struct.pack("<HHIII", 1, 1, 1, 16, 0)
            version += struct.pack("<IHHII", 0x069691a0, 0, 2, 11, 0)
            size = ver_offset + len(version)
            header = bytearray(64)
            header[:7] = b"\x7fELF\x02\x01\x01"
            struct.pack_into("<HHI", header, 16, 3, ELF_MACHINE["arm64"], 1)
            struct.pack_into("<Q", header, 32, 64)
            struct.pack_into("<HHH", header, 52, 64, 56, 2)
            phdrs = struct.pack("<IIQQQQQQ", 1, 4, 0, 0, 0, size, size, 4096)
            phdrs += struct.pack("<IIQQQQQQ", 2, 4, dyn_offset, dyn_offset, 0, 96, 96, 8)
            binary.write_bytes(header + phdrs + dynamic + strings + version)
            with self.assertRaises(ValueError):
                build._check_runtime(binary, "arm64", load_candidate())


class CandidateRoundTwoReviewTest(unittest.TestCase):
    def versioned(self, root):
        from tests.test_tauri_candidate_smoke import write_versioned_elf
        return write_versioned_elf(
            root / BINARY, ELF_MACHINE["arm64"], ("libc.so.6",),
            (("libc.so.6", ("GLIBC_2.34", "GLIBC_2.40")),),
        )

    def test_relative_upstream_lock_is_hashed_from_the_checkout(self):
        import hashlib
        # Match the workflow's relative --upstream input, without creating a git
        # repo or mutating refs; git -C inherits this workspace's repository.
        with tempfile.TemporaryDirectory(dir="dist/tauri-candidate") as tmp:
            upstream = Path(os.path.relpath(Path(tmp) / "upstream"))
            upstream.mkdir()
            body = b"fixture lockfile\n"
            (upstream / "Cargo.lock").write_bytes(body)
            blob = hashlib.sha1(b"blob " + str(len(body)).encode() + b"\0" + body).hexdigest()
            original = prov._git_output

            def git_output(path, *args):
                if args == ("rev-parse", "HEAD"):
                    return prov.REVIEWED_COMMIT
                if args == ("rev-parse", "HEAD:Cargo.lock"):
                    return blob
                return original(path, *args)

            with mock.patch.object(prov, "REVIEWED_LOCK_BLOB", blob), \
                 mock.patch.object(prov, "_git_output", side_effect=git_output):
                self.assertEqual(prov.check_checkout(upstream)[1], blob)

    def test_inconsistent_version_record_count_is_rejected(self):
        from scripts import tauri_candidate_smoke as smoke
        with tempfile.TemporaryDirectory() as tmp:
            path = self.versioned(Path(tmp))
            data = bytearray(path.read_bytes())
            # Set DT_VERNEEDNUM to zero while leaving a nonzero DT_VERNEED.
            dynamic_offset = 64 + 2 * 56
            struct.pack_into("<Q", data, dynamic_offset + 4 * 16 + 8, 0)
            path.write_bytes(data)
            with self.assertRaises(ValueError):
                smoke.check_runtime(path, "arm64", load_candidate())

    def test_premature_auxiliary_termination_is_rejected(self):
        from scripts import tauri_candidate_smoke as smoke
        with tempfile.TemporaryDirectory() as tmp:
            path = self.versioned(Path(tmp))
            data = bytearray(path.read_bytes())
            dynamic_offset = 64 + 2 * 56
            version_offset = struct.unpack_from("<Q", data, dynamic_offset + 3 * 16 + 8)[0]
            # vn_cnt remains 2, but terminate the first auxiliary record early,
            # hiding the higher GLIBC_2.40 requirement from the current reader.
            struct.pack_into("<I", data, version_offset + 16 + 12, 0)
            path.write_bytes(data)
            with self.assertRaises(ValueError):
                smoke.check_runtime(path, "arm64", load_candidate())

    def test_oversized_dynamic_segment_is_rejected_before_read(self):
        from scripts import tauri_candidate_smoke as smoke
        with tempfile.TemporaryDirectory() as tmp:
            path = self.versioned(Path(tmp))
            data = bytearray(path.read_bytes())
            # Avoid allocating hostile data: 2**63 cannot be a Python read size.
            # The parser must validate ELF ranges against the actual file first.
            struct.pack_into("<Q", data, 64 + 56 + 32, 1 << 63)
            path.write_bytes(data)
            with self.assertRaises(ValueError):
                smoke.read_symbol_versions(path)

    def test_large_dynamic_read_is_bounded_by_the_file_before_allocation(self):
        from scripts import tauri_candidate_smoke as smoke
        with tempfile.TemporaryDirectory() as tmp:
            path = self.versioned(Path(tmp))
            data = bytearray(path.read_bytes())
            struct.pack_into("<Q", data, 64 + 56 + 32, 1 << 30)
            path.write_bytes(data)
            original_open = Path.open

            class GuardedFile:
                def __init__(self, handle):
                    self.handle = handle

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    self.handle.close()

                def seek(self, *args):
                    return self.handle.seek(*args)

                def read(self, size=-1):
                    if size > len(data):
                        raise AssertionError(f"unsafe read request of {size} bytes for {len(data)}-byte ELF")
                    return self.handle.read(size)

            def guarded_open(file, *args, **kwargs):
                return GuardedFile(original_open(file, *args, **kwargs))

            # Intercept the hostile allocation; never actually allocate 1 GiB.
            with mock.patch.object(Path, "open", new=guarded_open):
                with self.assertRaises(ValueError):
                    smoke.read_symbol_versions(path)
