"""End-to-end coverage for the candidate build, driven with fixture inputs.

These tests build real `.deb` files through the real staging path. The only
substitutions are the upstream inputs themselves: a small `.tgz` carrying a fake
`cargo-tauri` is dropped where the download cache would hold the pinned asset, and
the arm64 source build is a fake ELF with a record whose git queries are mocked.
Everything downstream — the archive inventory, the licence notices, the
provenance records, the control file, `dpkg-deb` — is the production code path.

The properties asserted are the ones the candidate actually promises: the exact
payload, the reviewed licence bytes, a record that matches the reviewed pins, the
architecture-correct Depends, and byte-identical rebuilds of the same binary.
"""

import contextlib
import io
import struct
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import tauri_candidate as candidate_module
from scripts import tauri_candidate_provenance as prov
from scripts import tauri_candidate_smoke as smoke_module
from scripts.common import sha256_file
from scripts.manifest import deb_filename
from scripts.tauri_candidate import NOTICE_FILES, build, inventory_asset, load_candidate, repo_root
from scripts.tauri_candidate_metadata import (
    BINARY,
    ELF_MACHINE,
    REVIEWED_ASSET_MEMBERS,
    REVIEWED_COMMIT,
    REVIEWED_LOCK_BLOB,
    REVIEWED_LOCK_BYTES,
)
from tests.test_tauri_candidate_provenance import fixture_candidate

def elf_bytes(machine: int = ELF_MACHINE["amd64"]) -> bytes:
    """A real 64-bit ELF header with one empty program header.

    The header is enough for the architecture and dynamic-section readers, so the
    build path checks the same fields it checks in a release binary.
    """
    header = bytearray(64)
    header[0:4] = b"\x7fELF"
    header[4], header[5] = 2, 1
    struct.pack_into("<H", header, 16, 3)  # ET_DYN
    struct.pack_into("<H", header, 18, machine)
    struct.pack_into("<Q", header, 32, 64)
    struct.pack_into("<HH", header, 54, 56, 1)
    return bytes(header) + bytes(56)


def write_elf(path: Path, machine: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(elf_bytes(machine))
    return path


def write_asset(dest: Path, *, duplicate: str | None = None, **members) -> Path:
    """Write an upstream-shaped release tarball with the reviewed member names.

    A member value is its bytes, its text, or a `(TarInfo, bytes)` pair for the
    header cases the inventory has to refuse: links, directories, and setuid bits.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    licenses = repo_root() / "packaging" / "licenses" / "tauri-cli"
    payloads: dict[str, object] = {
        BINARY: elf_bytes(),
        "LICENSE-MIT": (licenses / "LICENSE-MIT.txt").read_text(encoding="utf-8"),
        "LICENSE-APACHE-2.0": (licenses / "LICENSE-APACHE-2.0.txt").read_text(encoding="utf-8"),
        "README.md": "# cargo-tauri\n",
    }
    payloads.update(members)

    def add(tar: tarfile.TarFile, name: str, value: object) -> None:
        if isinstance(value, tuple):
            info, data = value
            tar.addfile(info, io.BytesIO(data))
            return
        data = value if isinstance(value, bytes) else str(value).encode("utf-8")
        info = tarfile.TarInfo(name)
        info.size = len(data)
        info.mode = 0o755 if name == BINARY else 0o644
        tar.addfile(info, io.BytesIO(data))

    with tarfile.open(dest, "w:gz") as tar:
        for name, value in payloads.items():
            add(tar, name, value)
        if duplicate is not None:
            add(tar, duplicate, payloads[duplicate])
    return dest


def link_member(name: str, target: str) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.type = tarfile.SYMTYPE
    info.linkname = target
    return info, b""


def directory_member(name: str) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    return info, b""


def setuid_member(name: str, payload: bytes) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mode = 0o4755
    return info, payload


class CandidateBuildTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.candidate = load_candidate()
        self.cache = self.root / "cache"
        self.pool = self.root / "pool"
        self.archive = write_asset(
            self.cache / self.candidate.official.url.rsplit("/", 1)[-1],
        )

    def build_amd64(self, output: Path | None = None) -> Path:
        with mock.patch.object(candidate_module, "fetch_asset", return_value=self.archive):
            return build(output or self.pool, ("amd64",), self.candidate, cache=self.cache)[0]

    def payload(self, deb: Path) -> Path:
        target = self.root / f"extract-{deb.name}-{len(list(self.root.iterdir()))}"
        subprocess.run(["dpkg-deb", "-x", str(deb), str(target)], check=True, capture_output=True)
        return target

    def control(self, deb: Path) -> str:
        completed = subprocess.run(
            ["dpkg-deb", "-f", str(deb)], check=True, capture_output=True, text=True,
        )
        return completed.stdout


class OfficialAssetTest(CandidateBuildTest):
    def test_the_reviewed_member_set_is_the_one_accepted(self) -> None:
        members = inventory_asset(self.archive, REVIEWED_ASSET_MEMBERS)
        self.assertEqual(sorted(members), sorted(REVIEWED_ASSET_MEMBERS))

    def test_it_builds_the_expected_filename(self) -> None:
        deb = self.build_amd64()
        self.assertEqual(
            deb.name,
            deb_filename(self.candidate.package, "amd64", self.candidate.manifest),
        )

    def test_it_installs_the_binary_as_a_root_owned_executable(self) -> None:
        binary = self.payload(self.build_amd64()) / "usr" / "bin" / BINARY
        self.assertTrue(binary.is_file())
        self.assertFalse(binary.is_symlink())
        self.assertEqual(binary.read_bytes(), elf_bytes())
        self.assertEqual(binary.stat().st_mode & 0o777, 0o755)

    def test_it_installs_both_licence_texts_byte_exact(self) -> None:
        doc = self.payload(self.build_amd64()) / "usr" / "share" / "doc" / BINARY
        licenses = repo_root() / "packaging" / "licenses" / "tauri-cli"
        for name in NOTICE_FILES:
            self.assertEqual((doc / name).read_bytes(), (licenses / name).read_bytes(), name)
            self.assertEqual((doc / name).stat().st_mode & 0o777, 0o644)

    def test_it_installs_the_source_document_and_the_official_asset_record(self) -> None:
        doc = self.payload(self.build_amd64()) / "usr" / "share" / "doc" / BINARY
        self.assertIn(REVIEWED_COMMIT, (doc / "README.source").read_text(encoding="utf-8"))
        record = prov.read_record(doc / prov.OFFICIAL_ASSET_NAME)
        prov.check_official_asset(record, self.candidate)
        self.assertEqual(record["arch"], "amd64")
        self.assertEqual(record["asset"]["sha256"], self.candidate.official.sha256)

    def test_the_control_file_states_the_reviewed_metadata(self) -> None:
        control = self.control(self.build_amd64())
        self.assertIn(f"Package: {BINARY}\n", control)
        self.assertIn("Version: 2.12.2-1\n", control)
        self.assertIn("Architecture: amd64\n", control)
        self.assertIn(f"Depends: {self.candidate.depends()}\n", control)

    def test_rebuilding_the_same_binary_gives_identical_bytes(self) -> None:
        first = self.build_amd64(self.pool)
        second = self.build_amd64(self.root / "pool-again")
        self.assertEqual(sha256_file(first), sha256_file(second))

    def test_the_download_is_verified_against_the_pinned_digest(self) -> None:
        """A cache entry that is not the pinned asset never reaches the inventory."""
        self.archive.write_bytes(b"not the pinned asset")
        with self.assertRaisesRegex(ValueError, "sha256 mismatch"):
            candidate_module.fetch_asset(self.candidate, self.cache)

    def test_a_cached_asset_of_another_size_is_refused(self) -> None:
        self.archive.write_bytes(b"another archive of the right length?")
        with mock.patch.object(candidate_module, "fetch_verified"):
            with self.assertRaisesRegex(ValueError, "descriptor pins"):
                candidate_module.fetch_asset(self.candidate, self.cache)


class HostileArchiveTest(CandidateBuildTest):
    def refused(self, pattern: str, **members) -> None:
        archive = write_asset(self.root / "hostile.tgz", **members)
        with self.assertRaisesRegex(ValueError, pattern):
            inventory_asset(archive, REVIEWED_ASSET_MEMBERS)

    def test_an_unexpected_member_is_refused(self) -> None:
        self.refused("do not match the reviewed set", extra="surprise")

    def test_a_missing_member_is_refused(self) -> None:
        archive = self.root / "partial.tgz"
        with tarfile.open(archive, "w:gz") as tar:
            data = elf_bytes()
            info = tarfile.TarInfo(BINARY)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        with self.assertRaisesRegex(ValueError, "do not match the reviewed set"):
            inventory_asset(archive, REVIEWED_ASSET_MEMBERS)

    def test_a_traversal_member_is_refused(self) -> None:
        self.refused("unsafe archive member name", **{"../escape": "x"})

    def test_an_absolute_member_is_refused(self) -> None:
        self.refused("unsafe archive member name", **{"/etc/passwd": "x"})

    def test_a_backslash_member_is_refused(self) -> None:
        self.refused("unsafe archive member name", **{"dir\\file": "x"})

    def test_a_symlink_member_is_refused(self) -> None:
        self.refused("link or special member", shadow=link_member("shadow", "/bin/sh"))

    def test_a_directory_member_is_refused(self) -> None:
        self.refused("no directory entries", **{"dir": directory_member("dir")})

    def test_a_duplicate_member_is_refused(self) -> None:
        archive = write_asset(self.root / "duplicate.tgz", duplicate=BINARY)
        with self.assertRaisesRegex(ValueError, "more than once"):
            inventory_asset(archive, REVIEWED_ASSET_MEMBERS)

    def test_a_setuid_member_is_refused(self) -> None:
        self.refused("setuid or setgid", **{BINARY: setuid_member(BINARY, elf_bytes())})


class SourceBuildTest(CandidateBuildTest):
    def setUp(self) -> None:
        super().setUp()
        self.upstream = self.root / "upstream"
        (self.upstream / "crates").mkdir(parents=True)
        (self.upstream / "Cargo.lock").write_bytes(b"0" * REVIEWED_LOCK_BYTES)
        # The record checker compares the measured lockfile digest with the
        # descriptor's pin, so the fixture states its own bytes rather than
        # weakening that check for every other test.
        self.candidate = fixture_candidate(self.upstream)
        self.binary = write_elf(self.root / "build" / BINARY, ELF_MACHINE["arm64"])

    def record(self, **overrides) -> Path:
        def answer(_upstream: Path, *arguments: str) -> str:
            return REVIEWED_COMMIT if arguments[1] == "HEAD" else REVIEWED_LOCK_BLOB
        with mock.patch.object(prov, "_git_output", side_effect=answer):
            document = prov.emit_source_build(
                self.candidate, self.upstream, self.binary,
                rustc="rustc 1.99.0 (5f94df478 2026-08-27)",
                cargo="cargo 1.99.0 (5f94df478 2026-08-27)",
            )
        document.update(overrides)
        path = prov.write_record(self.root / "build-provenance.json", document)
        return path

    def test_it_packages_the_arm64_build_from_its_provenance(self) -> None:
        record = self.record()
        deb = build(
            self.pool, ("arm64",), self.candidate,
            source_build=(self.binary, record), cache=self.cache,
        )[0]
        self.assertEqual(deb.name, f"{BINARY}_2.12.2-1_arm64.deb")
        payload = self.payload(deb)
        self.assertEqual((payload / "usr" / "bin" / BINARY).read_bytes(), self.binary.read_bytes())
        doc = payload / "usr" / "share" / "doc" / BINARY
        prov.check_source_build(prov.read_record(doc / prov.SOURCE_BUILD_NAME), self.candidate)
        self.assertIn("Architecture: arm64\n", self.control(deb))

    def test_it_refuses_a_binary_whose_digest_the_record_does_not_carry(self) -> None:
        record = self.record()
        self.binary.write_bytes(self.binary.read_bytes() + b"tampered")
        with self.assertRaisesRegex(ValueError, "provenance record declares"):
            build(
                self.pool, ("arm64",), self.candidate,
                source_build=(self.binary, record), cache=self.cache,
            )

    def test_it_refuses_a_record_for_another_commit(self) -> None:
        record = self.record()
        document = prov.read_record(record)
        document["source"]["commit"] = "0" * 40
        prov.write_record(record, document)
        with self.assertRaisesRegex(ValueError, "commit must be exactly"):
            build(
                self.pool, ("arm64",), self.candidate,
                source_build=(self.binary, record), cache=self.cache,
            )

    def test_it_refuses_a_record_for_the_other_architecture(self) -> None:
        record = self.record()
        document = prov.read_record(record)
        document["arch"] = "amd64"
        prov.write_record(record, document)
        with self.assertRaisesRegex(ValueError, "arch must be exactly"):
            build(
                self.pool, ("arm64",), self.candidate,
                source_build=(self.binary, record), cache=self.cache,
            )

    def test_it_refuses_a_record_whose_elf_machine_is_not_the_builds(self) -> None:
        record = self.record()
        document = prov.read_record(record)
        document["artifact"]["elf_machine"] = ELF_MACHINE["amd64"]
        prov.write_record(record, document)
        with self.assertRaisesRegex(ValueError, "elf_machine must be 183"):
            build(
                self.pool, ("arm64",), self.candidate,
                source_build=(self.binary, record), cache=self.cache,
            )

    def test_it_refuses_a_binary_the_record_does_not_describe(self) -> None:
        """A binary from another build never reaches staging, whatever its arch."""
        record = self.record()
        foreign = write_elf(self.root / "x86" / BINARY, ELF_MACHINE["amd64"])
        with self.assertRaisesRegex(ValueError, "provenance record declares"):
            build(
                self.pool, ("arm64",), self.candidate,
                source_build=(foreign, record), cache=self.cache,
            )

    def test_it_refuses_a_payload_linking_an_unreviewed_library(self) -> None:
        record = self.record()
        with mock.patch.object(smoke_module, "read_needed", return_value=("libgtk-3.so.0",)):
            with self.assertRaisesRegex(ValueError, "does not cover"):
                build(
                    self.pool, ("arm64",), self.candidate,
                    source_build=(self.binary, record), cache=self.cache,
                )

    def test_rebuilding_the_same_source_build_gives_identical_bytes(self) -> None:
        record = self.record()
        first = build(self.pool, ("arm64",), self.candidate,
                      source_build=(self.binary, record), cache=self.cache)[0]
        second = build(self.root / "again", ("arm64",), self.candidate,
                       source_build=(self.binary, record), cache=self.cache)[0]
        self.assertEqual(sha256_file(first), sha256_file(second))


class CommandLineTest(unittest.TestCase):
    def test_the_default_build_is_the_official_amd64_asset(self) -> None:
        """Building without --arch must not reach for a source build."""
        with mock.patch.object(candidate_module, "build", return_value=[]) as built:
            self.assertEqual(candidate_module.main(["--output", "dist/unused"]), 0)
        built.assert_called_once()
        self.assertEqual(built.call_args.args[1], ("amd64",))
        self.assertIsNone(built.call_args.kwargs["source_build"])

    def test_the_arm64_build_carries_its_own_inputs(self) -> None:
        with mock.patch.object(candidate_module, "build", return_value=[]) as built:
            candidate_module.main(
                ["--arch", "arm64", "--binary", "b", "--provenance", "p"],
            )
        self.assertEqual(
            built.call_args.kwargs["source_build"], (Path("b"), Path("p")),
        )

    def test_arm64_without_its_own_inputs_is_refused(self) -> None:
        for arguments in (["--arch", "arm64"],
                          ["--arch", "arm64", "--binary", "b"],
                          ["--arch", "arm64", "--provenance", "p"]):
            with contextlib.redirect_stderr(io.StringIO()) as captured:
                with self.assertRaises(SystemExit):
                    candidate_module.main(arguments)
            self.assertIn("arm64", captured.getvalue())

    def test_source_build_inputs_without_arm64_are_refused(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                candidate_module.main(["--binary", "b", "--provenance", "p"])

    def test_build_without_source_build_inputs_refuses_arm64(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "needs --binary and --provenance"):
                build(Path(tmp), ("arm64",), load_candidate())


if __name__ == "__main__":
    unittest.main()
