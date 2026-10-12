"""A provenance record describes one build; it may not claim another.

The emitter measures the checkout, the lockfile, the compiler, and the finished
binary, and refuses to write a record when any measured fact contradicts the
reviewed pins. The checker then refuses a record whose pinned fields, measured
fields, or architecture disagree. These tests drive both directions, with only the
git queries mocked: everything else is measured from real files.
"""

import json
import shutil
import struct
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from scripts import tauri_candidate_provenance as prov
from scripts.common import sha256_file
from scripts.tauri_candidate_metadata import (
    BINARY,
    ELF_MACHINE,
    REVIEWED_COMMIT,
    REVIEWED_LOCK_BLOB,
    REVIEWED_LOCK_BYTES,
    REVIEWED_LOCK_SHA256,
    Candidate,
    load_candidate,
)

RUSTC_LINE = "rustc 1.99.0 (5f94df478 2026-08-27)\nbinary: rustc\nhost: aarch64-unknown-linux-gnu"
CARGO_LINE = "cargo 1.99.0 (5f94df478 2026-08-27)"


def write_elf(path: Path, machine: int) -> Path:
    """A minimal 64-bit ELF with one empty program header: a real header, no segments."""
    header = bytearray(64)
    header[0:4] = b"\x7fELF"
    header[4] = 2  # ELFCLASS64
    header[5] = 1  # ELFDATA2LSB
    header[6] = 1  # EV_CURRENT
    struct.pack_into("<H", header, 16, 1)  # ET_REL is enough to be parsed
    struct.pack_into("<H", header, 18, machine)
    struct.pack_into("<Q", header, 32, 64)  # e_phoff
    struct.pack_into("<HH", header, 54, 56, 1)  # e_phentsize, e_phnum
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(header) + bytes(56))
    return path


def checkout(root: Path, *, bytes_: int = REVIEWED_LOCK_BYTES) -> Path:
    """A checkout-shaped directory holding a lockfile of the reviewed size."""
    upstream = root / "upstream"
    (upstream / "crates").mkdir(parents=True, exist_ok=True)
    (upstream / "Cargo.lock").write_bytes(b"# fixture lockfile\n" + b"0" * (bytes_ - 19))
    return upstream


def fixture_candidate(upstream: Path) -> Candidate:
    """A candidate whose pinned lock digest matches the fixture's own bytes.

    The record checker compares the measured lockfile digest with the descriptor's
    pin. Production builds the reviewed lockfile, so the shipped pin is that
    digest; a fixture carries its own bytes, so its pin is stated here instead of
    weakening the checker for every other test.
    """
    base = load_candidate()
    digest = sha256_file(upstream / "Cargo.lock")
    return replace(base, source_build=replace(base.source_build, lock_sha256=digest))


def git_answers(head: str = REVIEWED_COMMIT, blob: str = REVIEWED_LOCK_BLOB):
    def answer(_upstream: Path, *arguments: str) -> str:
        return head if arguments[:1] == ("rev-parse",) and arguments[1] == "HEAD" else blob
    return answer


class EmitSourceBuildTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.upstream = checkout(self.root)
        self.candidate = fixture_candidate(self.upstream)
        self.binary = write_elf(self.root / "build" / BINARY, ELF_MACHINE["arm64"])

    def emit(self, **kwargs):
        with mock.patch.object(prov, "_git_output", side_effect=git_answers(**kwargs)):
            return prov.emit_source_build(
                self.candidate, self.upstream, self.binary, rustc=RUSTC_LINE, cargo=CARGO_LINE,
            )

    def test_the_record_states_the_measured_build(self) -> None:
        record = self.emit()
        prov.check_source_build(record, self.candidate)
        self.assertEqual(record["schema"], prov.SOURCE_BUILD_SCHEMA)
        self.assertEqual(record["arch"], "arm64")
        self.assertEqual(record["source"]["commit"], REVIEWED_COMMIT)
        self.assertEqual(record["source"]["lockfile"]["git_blob"], REVIEWED_LOCK_BLOB)
        self.assertEqual(
            record["source"]["lockfile"]["sha256"],
            sha256_file(self.upstream / "Cargo.lock"),
        )
        self.assertEqual(record["toolchain"]["channel"], "1.99.0")
        self.assertEqual(record["toolchain"]["rustc"], "rustc 1.99.0 (5f94df478 2026-08-27)")
        self.assertEqual(record["artifact"]["sha256"], sha256_file(self.binary))
        self.assertEqual(record["artifact"]["elf_machine"], ELF_MACHINE["arm64"])

    def test_a_checkout_at_another_commit_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "pins " + REVIEWED_COMMIT):
            self.emit(head="1" * 40)

    def test_a_checkout_with_another_lockfile_blob_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "Cargo.lock is git blob"):
            self.emit(blob="2" * 40)

    def test_a_working_lockfile_that_is_not_the_pinned_object_is_refused(self) -> None:
        """`HEAD:Cargo.lock` says nothing about the file the build actually read."""
        def answer(_upstream: Path, *arguments: str) -> str:
            if arguments[:2] == ("rev-parse", "HEAD"):
                return REVIEWED_COMMIT
            if arguments[:1] == ("hash-object",):
                return "3" * 40
            return REVIEWED_LOCK_BLOB

        with mock.patch.object(prov, "_git_output", side_effect=answer):
            with self.assertRaisesRegex(ValueError, "hashes to"):
                prov.emit_source_build(
                    self.candidate, self.upstream, self.binary,
                    rustc=RUSTC_LINE, cargo=CARGO_LINE,
                )

    def test_a_lockfile_of_another_size_is_refused(self) -> None:
        upstream = checkout(self.root / "other", bytes_=REVIEWED_LOCK_BYTES - 1)
        with mock.patch.object(prov, "_git_output", side_effect=git_answers()):
            with self.assertRaisesRegex(ValueError, "descriptor pins"):
                prov.emit_source_build(
                    fixture_candidate(upstream), upstream, self.binary,
                    rustc=RUSTC_LINE, cargo=CARGO_LINE,
                )

    def test_another_toolchain_identity_is_refused(self) -> None:
        with mock.patch.object(prov, "_git_output", side_effect=git_answers()):
            with self.assertRaisesRegex(ValueError, "does not match the pinned 1.99.0 toolchain"):
                prov.emit_source_build(
                    self.candidate, self.upstream, self.binary,
                    rustc="rustc 1.90.0 (abcdef 2025-01-01)", cargo=CARGO_LINE,
                )

    def test_a_foreign_elf_machine_is_refused(self) -> None:
        binary = write_elf(self.root / "x86" / BINARY, ELF_MACHINE["amd64"])
        with mock.patch.object(prov, "_git_output", side_effect=git_answers()):
            with self.assertRaisesRegex(ValueError, "ELF machine 62"):
                prov.emit_source_build(
                    self.candidate, self.upstream, binary, rustc=RUSTC_LINE, cargo=CARGO_LINE,
                )


class RefusedSourceBuildRecordTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        upstream = checkout(self.root)
        self.candidate = fixture_candidate(upstream)
        binary = write_elf(self.root / "build" / BINARY, ELF_MACHINE["arm64"])
        with mock.patch.object(prov, "_git_output", side_effect=git_answers()):
            self.record = prov.emit_source_build(
                self.candidate, upstream, binary, rustc=RUSTC_LINE, cargo=CARGO_LINE,
            )

    def refused(self, mutate, pattern: str) -> None:
        record = json.loads(json.dumps(self.record))
        mutate(record)
        with self.assertRaisesRegex(ValueError, pattern):
            prov.check_source_build(record, self.candidate)

    def test_the_emitted_record_is_accepted(self) -> None:
        prov.check_source_build(self.record, self.candidate)

    def test_the_lockfile_handed_to_git_is_an_absolute_path(self) -> None:
        """A relative checkout plus `git -C` would resolve the path twice."""
        seen: list[tuple[str, ...]] = []
        answer = git_answers()

        def record(upstream: Path, *arguments: str) -> str:
            seen.append(arguments)
            return answer(upstream, *arguments)

        upstream = Path("dist/tauri-candidate/upstream-fixture")
        upstream.mkdir(parents=True, exist_ok=True)
        (upstream / "Cargo.lock").write_bytes(b"# fixture\n")
        try:
            with mock.patch.object(prov, "_git_output", side_effect=record):
                prov.check_checkout(upstream)
        finally:
            shutil.rmtree(upstream)
        hashed = next(args for args in seen if args[:1] == ("hash-object",))
        self.assertTrue(Path(hashed[-1]).is_absolute())

    def test_another_commit_refused(self) -> None:
        self.refused(lambda r: r["source"].update(commit="3" * 40), "commit must be exactly")

    def test_another_lockfile_blob_refused(self) -> None:
        self.refused(lambda r: r["source"]["lockfile"].update(git_blob="4" * 40), "git_blob must be exactly")

    def test_a_substituted_lockfile_digest_refused(self) -> None:
        """A well-formed digest for different bytes is still a different lockfile."""
        self.refused(lambda r: r["source"]["lockfile"].update(sha256="0" * 64), "lockfile sha256")

    def test_a_truncated_lockfile_digest_refused(self) -> None:
        self.refused(lambda r: r["source"]["lockfile"].update(sha256="abc"), "lockfile sha256")

    def test_the_shipped_pin_is_the_reviewed_lockfile_digest(self) -> None:
        self.assertEqual(load_candidate().source_build.lock_sha256, REVIEWED_LOCK_SHA256)

    def test_another_toolchain_channel_refused(self) -> None:
        self.refused(lambda r: r["toolchain"].update(channel="1.98.0"), "channel must be exactly")

    def test_another_compiler_identity_refused(self) -> None:
        self.refused(lambda r: r["toolchain"].update(cargo="cargo 1.90.0 (x 2025-01-01)"),
                     "pinned 1.99.0 toolchain")

    def test_another_build_command_refused(self) -> None:
        self.refused(lambda r: r["build"]["command"].remove("--locked"), "reviewed build command")

    def test_another_architecture_refused(self) -> None:
        self.refused(lambda r: r.update(arch="amd64"), "arch must be exactly")

    def test_another_schema_refused(self) -> None:
        self.refused(lambda r: r.update(schema="apt-at-home/tauri-official-asset/1"), "unsupported record schema")

    def test_an_unreviewed_elf_machine_refused(self) -> None:
        self.refused(lambda r: r["artifact"].update(elf_machine=62), "elf_machine must be 183")

    def test_a_truncated_artifact_digest_refused(self) -> None:
        self.refused(lambda r: r["artifact"].update(sha256="abc"), "artifact sha256")

    def test_an_unknown_key_refused(self) -> None:
        self.refused(lambda r: r.update(signature="signed"), "unknown keys")

    def test_an_unknown_nested_key_refused(self) -> None:
        self.refused(lambda r: r["build"].update(rustflags=["-C", "target-cpu=native"]), "unknown keys")

    def test_a_dropped_uncertainty_notice_refused(self) -> None:
        self.refused(lambda r: r.update(notice="verified by the build system"), "unsigned description")

    def test_a_record_that_is_not_an_object_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be an object"):
            prov.check_source_build([], self.candidate)


class OfficialAssetRecordTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.candidate = load_candidate()
        self.binary = write_elf(self.root / "asset" / BINARY, ELF_MACHINE["amd64"])
        self.notices = {
            name: self.root / name for name in prov._NOTICE_KEYS
        }
        for index, path in enumerate(self.notices.values()):
            path.write_text(f"notice {index}\n", encoding="utf-8")
        self.record = prov.emit_official_asset(self.candidate, self.binary, self.notices)

    def refused(self, mutate, pattern: str) -> None:
        record = json.loads(json.dumps(self.record))
        mutate(record)
        with self.assertRaisesRegex(ValueError, pattern):
            prov.check_official_asset(record, self.candidate)

    def test_the_record_states_the_pinned_asset_and_the_measured_binary(self) -> None:
        prov.check_official_asset(self.record, self.candidate)
        self.assertEqual(self.record["asset"]["url"], self.candidate.official.url)
        self.assertEqual(self.record["asset"]["sha256"], self.candidate.official.sha256)
        self.assertEqual(self.record["binary"]["sha256"], sha256_file(self.binary))
        self.assertEqual(
            self.record["notices"]["LICENSE-MIT.txt"], sha256_file(self.notices["LICENSE-MIT.txt"]),
        )

    def test_another_asset_url_refused(self) -> None:
        self.refused(lambda r: r["asset"].update(url="https://example.invalid/a.tgz"),
                     "url must be exactly")

    def test_another_asset_digest_refused(self) -> None:
        self.refused(lambda r: r["asset"].update(sha256="0" * 64), "sha256 must be exactly")

    def test_another_asset_size_refused(self) -> None:
        self.refused(lambda r: r["asset"].update(bytes=1), "bytes must be exactly")

    def test_another_member_set_refused(self) -> None:
        self.refused(lambda r: r["asset"].update(members=["cargo-tauri"]), "reviewed member set")

    def test_a_missing_notice_refused(self) -> None:
        self.refused(lambda r: r["notices"].pop("LICENSE-MIT.txt"), "notices must cover exactly")

    def test_a_foreign_binary_machine_refused(self) -> None:
        self.refused(lambda r: r["binary"].update(elf_machine=183), "elf_machine must be 62")

    def test_a_dropped_uncertainty_notice_refused(self) -> None:
        self.refused(lambda r: r.update(notice="verified by the build system"), "unsigned description")

    def test_the_arm64_record_is_not_an_official_asset_record(self) -> None:
        with self.assertRaisesRegex(ValueError, "arch must be exactly 'amd64'"):
            prov.check_official_asset({**self.record, "arch": "arm64"}, self.candidate)


class RecordIoTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.document = {"b": 2, "a": 1}

    def test_records_render_deterministically(self) -> None:
        self.assertEqual(
            prov.record_text(self.document),
            prov.record_text({"a": 1, "b": 2}),
        )
        self.assertTrue(prov.record_text(self.document).endswith("\n"))

    def test_a_written_record_reads_back_identically(self) -> None:
        path = prov.write_record(self.root / "nested" / "record.json", self.document)
        self.assertEqual(prov.read_record(path), self.document)

    def test_a_missing_record_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "not found"):
            prov.read_record(self.root / "absent.json")

    def test_a_malformed_record_is_refused(self) -> None:
        path = self.root / "broken.json"
        path.write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "not valid JSON"):
            prov.read_record(path)


if __name__ == "__main__":
    unittest.main()
