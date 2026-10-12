"""A producer's artifact directory is an input, not a statement of fact.

The directory a separate native job uploads carries a compiled binary, the
provenance record that describes it, and the reviewed licence notices. Nothing in
it is executed here, and nothing in it is believed: the record is checked against
the identity the manifest pins, the binary is hashed and read as an ELF, the
libraries it needs are measured against the reviewed runtime mapping, and the
directory itself must be a plain set of bounded regular files. Every case below is
a way that trust could be claimed without being earned.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import tauri_candidate_provenance as prov
from scripts.common import sha256_bytes, sha256_file
from scripts.manifest import parse_manifest
from scripts.source_artifacts import (
    NOTICE_FILES,
    expected_source_identities,
    index_source_artifacts,
    load_source_artifacts,
    reviewed_runtime,
    select_source_artifacts,
)
from scripts.source_build import (
    ArtifactProvenance,
    Lockfile,
    RealizedSource,
    SourceIdentity,
    Toolchain,
    realize,
)
from scripts.tauri_candidate_metadata import ELF_MACHINE
from scripts.tauri_source import source_policy
from tests.test_source_build import source_manifest_document
from tests.test_tauri_candidate_provenance import CARGO_LINE, RUSTC_LINE
from tests.test_tauri_candidate_smoke import write_versioned_elf

VERSION = "2.12.2"
COMMIT = "5b23c82ea5ba6b9aec420dd71f60493edc486aa9"
BLOB = "6b052c58398b8d6ec246d77f65a748448cd4c859"
REPOSITORY = "https://github.com/tauri-apps/tauri"
LOCK = b"# fixture lockfile\n" + b"0" * 96
NOTICES = {name: f"reviewed {name} text\n" for name in NOTICE_FILES}


def source_identity(**overrides: object) -> SourceIdentity:
    """A typed identity whose lockfile pins match the fixture's own bytes."""
    fields = {
        "repository": REPOSITORY,
        "tag": f"tauri-cli-v{VERSION}",
        "commit": COMMIT,
        "lockfile": Lockfile("Cargo.lock", BLOB, len(LOCK), sha256_bytes(LOCK)),
        "toolchain": Toolchain("1.99.0"),
        "build": source_policy(),
    }
    fields.update(overrides)
    return SourceIdentity(**fields)  # type: ignore[arg-type]


def _git_answers(identity: SourceIdentity):
    def answer(_upstream: Path, *arguments: str) -> str:
        if arguments[:2] == ("rev-parse", "HEAD"):
            return identity.commit
        return identity.lockfile.git_blob
    return answer


def arm64_binary(
    path: Path, *, versions: tuple[tuple[str, tuple[str, ...]], ...] = (("libc.so.6", ("GLIBC_2.34",)),),
    machine: int = ELF_MACHINE["arm64"], sonames: tuple[str, ...] = ("libc.so.6",),
) -> Path:
    """A versioned aarch64 ELF whose needs fit the reviewed runtime mapping."""
    return write_versioned_elf(path, machine, sonames, versions)


def artifact_directory(
    root: Path,
    *,
    identity: SourceIdentity | None = None,
    package: str = "cargo-tauri",
    version: str = VERSION,
    arch: str = "arm64",
    binary_path: Path | None = None,
    versions: tuple[tuple[str, tuple[str, ...]], ...] = (("libc.so.6", ("GLIBC_2.34",)),),
    sonames: tuple[str, ...] = ("libc.so.6",),
    machine: int = ELF_MACHINE["arm64"],
    with_notices: bool = True,
) -> tuple[Path, SourceIdentity]:
    """Write one producer-style artifact directory and return it with its identity."""
    identity = identity or source_identity()
    directory = root / "artifacts"
    directory.mkdir(parents=True, exist_ok=True)
    binary = arm64_binary(
        binary_path or directory / identity.build.binary,
        versions=versions, sonames=sonames, machine=machine,
    )
    upstream = root / "upstream"
    upstream.mkdir(parents=True, exist_ok=True)
    (upstream / "Cargo.lock").write_bytes(LOCK)
    expectation = prov.SourceRecordExpectation(
        identity=identity, package=package, version=version, arch=arch
    )
    with mock.patch.object(prov, "_git_output", side_effect=_git_answers(identity)):
        record = prov.emit_source_build(
            expectation, upstream, binary, rustc=RUSTC_LINE, cargo=CARGO_LINE,
        )
    prov.write_record(directory / prov.SOURCE_BUILD_NAME, record)
    if with_notices:
        for name, body in NOTICES.items():
            (directory / name).write_text(body, encoding="utf-8")
    return directory, identity


def pinned(identity: SourceIdentity) -> dict[tuple[str, str], SourceIdentity]:
    """The manifest-side authority the loader compares every record against."""
    return expected_source_identities(
        source_manifest_document(identity.document())
    )


class LoadSourceArtifactsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.directory, self.identity = artifact_directory(self.root)
        self.expected = pinned(self.identity)
        self.record = json.loads((self.directory / prov.SOURCE_BUILD_NAME).read_text())

    def load(self, **kwargs):
        kwargs.setdefault("expected", self.expected)
        return load_source_artifacts(self.directory, **kwargs)

    def test_a_valid_directory_yields_the_measured_build(self) -> None:
        artifacts = self.load()
        self.assertEqual(artifacts.package, "cargo-tauri")
        self.assertEqual(artifacts.arch, "arm64")
        self.assertEqual(artifacts.identity, self.identity)
        self.assertEqual(artifacts.artifact.sha256, sha256_file(artifacts.binary))
        self.assertEqual(artifacts.artifact.elf_machine, ELF_MACHINE["arm64"])
        self.assertEqual(
            artifacts.license_bodies(), tuple(NOTICES[name] for name in NOTICE_FILES)
        )

    def test_an_expected_identity_must_match_the_record_exactly(self) -> None:
        moved = source_identity(commit="a" * 40)
        with self.assertRaisesRegex(ValueError, "commit must be exactly"):
            load_source_artifacts(self.directory, expected=pinned(moved))

    def test_a_directory_with_no_pinned_target_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "no source target is pinned"):
            self.load(expected={})

    def test_a_swapped_binary_loses_the_records_vouch(self) -> None:
        arm64_binary(
            self.directory / self.identity.build.binary,
            versions=(("libc.so.6", ("GLIBC_2.35",)),),
        )
        with self.assertRaisesRegex(ValueError, "has sha256"):
            self.load()

    def test_a_record_for_another_package_version_or_arch_is_refused(self) -> None:
        record_path = self.directory / prov.SOURCE_BUILD_NAME
        for field, value in (
            ("package", "zellij"),
            ("version", "2.12.1"),
            ("arch", "amd64"),
        ):
            with self.subTest(field=field):
                record = json.loads(record_path.read_text())
                record[field] = value
                prov.write_record(record_path, record)
                with self.assertRaises(ValueError):
                    self.load()
        prov.write_record(record_path, self.record)

    def test_a_version_that_is_only_a_suffix_of_the_pinned_tag_is_refused(self) -> None:
        """The record may not satisfy its own version with a substring of the tag.

        `tauri-cli-v2.12.2` ends with `12.2`, but that is not the version of the
        release: a suffix match would accept a record that names no upstream
        release at all. The expected version is derived from the trusted pinned
        identity, so a self-claimed copy is compared against it exactly.
        """
        record_path = self.directory / prov.SOURCE_BUILD_NAME
        for rejected in ("12.2", "2.12", "2", "12.2.2", "2.12.2-rc1", ""):
            with self.subTest(version=rejected):
                record = json.loads(record_path.read_text())
                record["version"] = rejected
                prov.write_record(record_path, record)
                with self.assertRaisesRegex(ValueError, "record version must be exactly"):
                    self.load()
        prov.write_record(record_path, self.record)

    def test_the_trusted_pinned_tag_supplies_the_expected_version(self) -> None:
        """A different pinned tag moves the expectation with it, not the record."""
        moved = source_identity(tag="tauri-cli-v2.13.0")
        with self.assertRaisesRegex(ValueError, "record version must be exactly"):
            load_source_artifacts(self.directory, expected=pinned(moved))
        # An unpinnable tag names no version at all, so nothing could match it.
        untagged = source_identity(tag="tauri-v2.12.2")
        with self.assertRaisesRegex(ValueError, "names no canonical version"):
            load_source_artifacts(self.directory, expected=pinned(untagged))

    def test_the_true_version_of_the_pinned_tag_is_accepted(self) -> None:
        """The positive control: the record's own version is the pinned one."""
        artifacts = self.load()
        self.assertEqual(
            json.loads((self.directory / prov.SOURCE_BUILD_NAME).read_text())["version"],
            VERSION,
        )
        self.assertEqual(artifacts.identity.tag, f"tauri-cli-v{VERSION}")

    def test_a_record_with_no_measurement_is_refused(self) -> None:
        record_path = self.directory / prov.SOURCE_BUILD_NAME
        record = json.loads(record_path.read_text())
        del record["artifact"]
        record_path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "states no measured artifact"):
            self.load()

    def test_a_record_that_names_another_lockfile_is_refused(self) -> None:
        record_path = self.directory / prov.SOURCE_BUILD_NAME
        record = json.loads(record_path.read_text())
        record["source"]["lockfile"]["sha256"] = "0" * 64
        record_path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "lockfile sha256 must be exactly"):
            self.load()

    def test_a_foreign_elf_machine_is_refused(self) -> None:
        record_path = self.directory / prov.SOURCE_BUILD_NAME
        record = json.loads(record_path.read_text())
        record["artifact"]["elf_machine"] = ELF_MACHINE["amd64"]
        prov.write_record(record_path, record)
        with self.assertRaisesRegex(ValueError, "artifact elf_machine must be"):
            self.load()
        prov.write_record(record_path, self.record)
        arm64_binary(self.directory / self.identity.build.binary, machine=ELF_MACHINE["amd64"])
        with self.assertRaises(ValueError):
            self.load()

    def test_a_payload_above_the_reviewed_abi_floor_is_refused(self) -> None:
        directory, identity = artifact_directory(
            self.root / "high",
            versions=(("libc.so.6", ("GLIBC_2.34", "GLIBC_2.40")),),
        )
        # The record and the binary agree with each other; only the reviewed
        # floor stops this, which is the point of measuring against it.
        record = json.loads((directory / prov.SOURCE_BUILD_NAME).read_text())
        self.assertEqual(record["artifact"]["elf_machine"], ELF_MACHINE["arm64"])
        self.assertEqual(
            sha256_file(directory / identity.build.binary), record["artifact"]["sha256"]
        )
        with self.assertRaisesRegex(ValueError, "which the reviewed mapping does not declare"):
            load_source_artifacts(directory, expected=pinned(identity))

    def test_an_unmapped_library_is_refused(self) -> None:
        root = self.root / "extra-lib"
        directory, identity = artifact_directory(
            root, versions=(), sonames=("libssl.so.3", "libc.so.6"),
        )
        with self.assertRaisesRegex(ValueError, "runtime mapping does not cover"):
            load_source_artifacts(directory, expected=pinned(identity))

    def test_a_directory_must_be_plain_regular_files(self) -> None:
        os.unlink(self.directory / self.identity.build.binary)
        os.symlink(self.root / "elsewhere", self.directory / self.identity.build.binary)
        with self.assertRaisesRegex(ValueError, "refusing non-regular entry"):
            self.load()

    def test_an_unexpected_or_missing_file_is_refused(self) -> None:
        (self.directory / "target").mkdir()
        with self.assertRaisesRegex(ValueError, "non-regular entry"):
            self.load()
        (self.directory / "target").rmdir()
        (self.directory / "extra.txt").write_text("x", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unexpected files"):
            self.load()
        (self.directory / "extra.txt").unlink()
        (self.directory / NOTICE_FILES[0]).unlink()
        with self.assertRaisesRegex(ValueError, "missing"):
            self.load()

    def test_the_producer_plan_file_is_tolerated_but_not_read(self) -> None:
        (self.directory / "source-plan.json").write_text('{"anything": true}', encoding="utf-8")
        self.assertEqual(self.load().identity, self.identity)

    def test_notices_must_be_the_reviewed_texts_when_they_are_supplied(self) -> None:
        self.load(reviewed_notices=NOTICES)
        with self.assertRaisesRegex(ValueError, "not the reviewed licence text"):
            self.load(reviewed_notices={**NOTICES, NOTICE_FILES[0]: "different\n"})

    def test_an_empty_notice_is_refused(self) -> None:
        (self.directory / NOTICE_FILES[0]).write_text("   \n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "is empty"):
            self.load()

    def test_a_missing_directory_or_record_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "one real directory"):
            load_source_artifacts(self.root / "absent", expected=self.expected)
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaisesRegex(ValueError, "no build-provenance.json"):
            load_source_artifacts(empty, expected=self.expected)

    def test_a_symlinked_root_is_refused(self) -> None:
        link = self.root / "linked"
        os.symlink(self.directory, link)
        with self.assertRaisesRegex(ValueError, "one real directory"):
            load_source_artifacts(link, expected=self.expected)


class TargetSelectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.directory, self.identity = artifact_directory(self.root)
        self.artifacts = index_source_artifacts(
            [load_source_artifacts(self.directory, expected=pinned(self.identity))]
        )

    def target(self, source: dict | None = None):
        manifest = parse_manifest(source_manifest_document(source or self.identity.document()))
        return manifest.packages[0].targets["arm64"]

    def test_the_matching_target_is_selected(self) -> None:
        found = select_source_artifacts(self.artifacts, "cargo-tauri", "arm64", self.target())
        self.assertEqual(found.identity, self.identity)

    def test_a_missing_artifact_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "needs its compiled artifact"):
            select_source_artifacts({}, "cargo-tauri", "arm64", self.target())

    def test_a_different_identity_is_refused(self) -> None:
        moved = self.identity.document()
        moved["lockfile"] = {**moved["lockfile"], "git_blob": "b" * 40}  # type: ignore[operator]
        with self.assertRaisesRegex(ValueError, "does not match the pinned source identity"):
            select_source_artifacts(self.artifacts, "cargo-tauri", "arm64", self.target(moved))

    def test_a_published_artifact_must_match_exactly(self) -> None:
        published = self.artifacts[("cargo-tauri", "arm64")].artifact
        same = self.target({**self.identity.document(), "artifact": published.document()})
        self.assertIsInstance(same.source_build, RealizedSource)
        self.assertEqual(
            select_source_artifacts(self.artifacts, "cargo-tauri", "arm64", same).artifact,
            published,
        )
        moved = self.target(
            {
                **self.identity.document(),
                "artifact": ArtifactProvenance(published.sha256, published.bytes + 1, 183).document(),
            }
        )
        with self.assertRaisesRegex(ValueError, "a published release is immutable"):
            select_source_artifacts(self.artifacts, "cargo-tauri", "arm64", moved)

    def test_an_asset_target_has_no_artifact_to_select(self) -> None:
        from tests.test_source_build import source_manifest_document as document_of

        manifest = parse_manifest(document_of())
        with self.assertRaisesRegex(ValueError, "no source_build block"):
            select_source_artifacts(
                self.artifacts, "cargo-tauri", "amd64", manifest.packages[0].targets["amd64"]
            )

    def test_two_directories_for_one_target_are_refused(self) -> None:
        loaded = load_source_artifacts(self.directory, expected=pinned(self.identity))
        with self.assertRaisesRegex(ValueError, "two artifact directories"):
            index_source_artifacts([loaded, loaded])

    def test_the_reviewed_runtime_mapping_is_the_candidate_evidence(self) -> None:
        policy = reviewed_runtime("cargo-tauri")
        self.assertEqual(policy.sonames()[0], "libbz2.so.1.0")
        self.assertEqual(policy.declared_floors()["libc6"], "2.39")
        self.assertIsNotNone(realize(self.identity, self.artifacts[("cargo-tauri", "arm64")].artifact).artifact)


if __name__ == "__main__":
    unittest.main()