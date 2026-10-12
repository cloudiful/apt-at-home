"""The candidate descriptor is the whole trust boundary: refuse it unless exact.

Every field here is compared against a reviewed pin rather than shape-checked, so
these tests mutate one field at a time and require the refusal. A descriptor that
could re-pin the upstream commit, the release asset digest, the build command, or
the runtime mapping would let the candidate claim provenance nobody reviewed.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from scripts.tauri_candidate_metadata import (
    BINARY,
    REVIEWED_BUILD_COMMAND,
    REVIEWED_COMMIT,
    REVIEWED_LOCK_BLOB,
    REVIEWED_LOCK_SHA256,
    REVIEWED_PROFILE,
    REVIEWED_RUNTIME,
    REVIEWED_TARGET,
    REVIEWED_TOOLCHAIN,
    RuntimeLibrary,
    check_source_document,
    dependency_string,
    license_bodies,
    load_candidate,
    repo_root,
    reviewed_source_refs,
)


def descriptor_path() -> Path:
    return repo_root() / "packaging" / "tauri-cli" / "candidate.json"


def document_path() -> Path:
    return repo_root() / "packaging" / "tauri-cli" / "README.source"


class DescriptorTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.base = json.loads(descriptor_path().read_text(encoding="utf-8"))
        self.document = self.root / "README.source"
        shutil.copyfile(document_path(), self.document)
        self.descriptor = self.root / "candidate.json"

    def write(self, mutate=None) -> Path:
        raw = json.loads(json.dumps(self.base))
        if mutate is not None:
            mutate(raw)
        self.descriptor.write_text(json.dumps(raw), encoding="utf-8")
        return self.descriptor

    def refused(self, mutate, pattern: str) -> None:
        with self.assertRaisesRegex(ValueError, pattern):
            load_candidate(self.write(mutate))

    def load_mutated(self, mutate):
        return load_candidate(self.write(mutate))


class ShippedDescriptorTest(DescriptorTest):
    def test_the_shipped_descriptor_loads(self) -> None:
        candidate = load_candidate()
        self.assertEqual(candidate.package.name, BINARY)
        self.assertEqual(candidate.package.version, "2.12.2")

    def test_the_amd64_target_carries_the_pinned_official_asset(self) -> None:
        candidate = load_candidate()
        target = candidate.package.targets["amd64"]
        self.assertEqual(target.url, candidate.official.url)
        self.assertEqual(target.sha256, candidate.official.sha256)
        self.assertEqual(target.binary, BINARY)

    def test_the_arm64_target_names_no_asset_at_all(self) -> None:
        """Upstream ships no Linux arm64 asset; an invented URL would be a lie."""
        candidate = load_candidate()
        target = candidate.package.targets["arm64"]
        self.assertEqual(target.url, "")
        self.assertEqual(target.sha256, "")
        self.assertEqual(target.binary, BINARY)
        self.assertEqual(candidate.official.arch, "amd64")
        self.assertEqual(candidate.source_build.arch, "arm64")

    def test_depends_is_derived_from_the_closed_runtime_mapping(self) -> None:
        candidate = load_candidate()
        self.assertEqual(
            candidate.depends(),
            "cargo, libc6 (>= 2.34), libgcc-s1 (>= 14.2.0), liblzma5 (>= 5.8.1)",
        )
        self.assertEqual(candidate.package.targets["arm64"].depends, candidate.depends())

    def test_two_sonames_in_one_package_keep_the_higher_floor(self) -> None:
        libraries = (
            RuntimeLibrary("libm.so.6", "libc6", "2.34", "GLIBC_"),
            RuntimeLibrary("libc.so.6", "libc6", "2.29", "GLIBC_"),
            RuntimeLibrary("libgcc_s.so.1", "libgcc-s1", "14.2.0", "GCC_"),
        )
        self.assertEqual(
            dependency_string(libraries, "cargo"),
            "cargo, libc6 (>= 2.34), libgcc-s1 (>= 14.2.0)",
        )

    def test_the_declared_floor_per_package_is_the_highest_reviewed_one(self) -> None:
        self.assertEqual(
            load_candidate().declared_floors(),
            {"libc6": "2.34", "libgcc-s1": "14.2.0", "liblzma5": "5.8.1"},
        )

    def test_the_descriptor_runtime_matches_the_reviewed_mapping(self) -> None:
        raw = json.loads(descriptor_path().read_text(encoding="utf-8"))
        declared = {
            entry["soname"]: (entry["package"], entry["version"], entry["symbols"])
            for entry in raw["runtime"]["libraries"]
        }
        self.assertEqual(declared, REVIEWED_RUNTIME)

    def test_the_source_build_command_is_the_reviewed_cargo_invocation(self) -> None:
        candidate = load_candidate()
        self.assertEqual(
            candidate.source_build.command,
            ("cargo", "build", "--locked", "--manifest-path", "crates/tauri-cli/Cargo.toml",
             "--target", "aarch64-unknown-linux-gnu", "--profile",
             "release-size-optimized", "--bin", "cargo-tauri"),
        )
        self.assertEqual(candidate.source_build.toolchain, REVIEWED_TOOLCHAIN)

    def test_both_licence_texts_are_read_for_the_copyright_file(self) -> None:
        bodies = license_bodies()
        self.assertEqual(len(bodies), 2)
        self.assertIn("MIT License", bodies[0])
        self.assertIn("Apache License", bodies[1])


class RefusedDescriptorTest(DescriptorTest):
    def test_unknown_top_level_key_refused(self) -> None:
        self.refused(lambda raw: raw.update(extra=True), "unknown keys")

    def test_missing_key_refused(self) -> None:
        self.refused(lambda raw: raw.pop("runtime"), "missing keys")

    def test_wrong_schema_refused(self) -> None:
        self.refused(lambda raw: raw.update(schema="apt-at-home/bun-candidate/1"), "schema")

    def test_other_package_refused(self) -> None:
        self.refused(lambda raw: raw.update(package="bun"), "package other than")

    def test_unreviewed_version_refused(self) -> None:
        self.refused(lambda raw: raw.update(version="2.13.0"), "reviewed 2.12.2")

    def test_other_debian_revision_refused(self) -> None:
        self.refused(lambda raw: raw.update(debian_revision="-2"), "debian_revision")

    def test_other_licence_set_refused(self) -> None:
        self.refused(lambda raw: raw.update(license_files=["LICENSE.txt"]), "license_files")

    def test_unknown_source_key_refused(self) -> None:
        self.refused(lambda raw: raw["source"].update(branch="dev"), "candidate source has unknown")

    def test_re_pinned_commit_refused(self) -> None:
        self.refused(lambda raw: raw["source"].update(commit="0" * 40), "source commit must be exactly")

    def test_re_pinned_tag_refused(self) -> None:
        self.refused(lambda raw: raw["source"].update(tag="tauri-cli-v2.12.1"), "source tag must be exactly")

    def test_other_repository_refused(self) -> None:
        self.refused(lambda raw: raw["source"].update(repository="https://example.invalid/tauri"),
                     "source repository must be exactly")


class RefusedAssetTest(DescriptorTest):
    def test_re_pinned_asset_digest_refused(self) -> None:
        self.refused(lambda raw: raw["official_asset"].update(sha256="0" * 64), "digest this release")

    def test_re_pinned_asset_size_refused(self) -> None:
        self.refused(lambda raw: raw["official_asset"].update(size=1), "size must be")

    def test_other_asset_url_refused(self) -> None:
        self.refused(
            lambda raw: raw["official_asset"].update(url="https://example.invalid/cargo-tauri.tgz"),
            "url must be exactly",
        )

    def test_extra_asset_member_refused(self) -> None:
        self.refused(
            lambda raw: raw["official_asset"].update(members=["LICENSE-APACHE-2.0", "LICENSE-MIT",
                                                            "README.md", "cargo-tauri", "extra"]),
            "members must be exactly",
        )

    def test_missing_asset_member_refused(self) -> None:
        self.refused(
            lambda raw: raw["official_asset"].update(members=["LICENSE-MIT", "cargo-tauri"]),
            "members must be exactly",
        )

    def test_other_licence_member_mapping_refused(self) -> None:
        self.refused(
            lambda raw: raw["official_asset"].update(license_members={"LICENSE-MIT.txt": "LICENSE"}),
            "license_members must be exactly",
        )

    def test_asset_for_the_wrong_architecture_refused(self) -> None:
        self.refused(lambda raw: raw["official_asset"].update(arch="arm64"), "amd64 asset")


class RefusedSourceBuildTest(DescriptorTest):
    def test_other_toolchain_refused(self) -> None:
        self.refused(lambda raw: raw["source_build"].update(toolchain="1.90.0"), "toolchain must be exactly")

    def test_other_target_refused(self) -> None:
        self.refused(lambda raw: raw["source_build"].update(target="aarch64-apple-darwin"),
                     "target must be exactly")

    def test_other_profile_refused(self) -> None:
        self.refused(lambda raw: raw["source_build"].update(profile="release"), "profile must be exactly")

    def test_other_manifest_refused(self) -> None:
        self.refused(lambda raw: raw["source_build"].update(manifest="crates/tauri/Cargo.toml"),
                     "manifest must be exactly")

    def test_build_command_without_lock_refused(self) -> None:
        self.refused(
            lambda raw: raw["source_build"].update(command=list(REVIEWED_BUILD_COMMAND[:2])
                                                   + list(REVIEWED_BUILD_COMMAND[3:])),
            "command must be exactly",
        )

    def test_build_command_with_a_different_profile_refused(self) -> None:
        command = list(REVIEWED_BUILD_COMMAND)
        command[command.index("--profile") + 1] = "release"
        self.refused(lambda raw: raw["source_build"].update(command=command), "command must be exactly")

    def test_other_lockfile_blob_refused(self) -> None:
        self.refused(lambda raw: raw["source_build"]["lockfile"].update(git_blob="0" * 40),
                     "git_blob must be exactly")

    def test_other_lockfile_size_refused(self) -> None:
        self.refused(lambda raw: raw["source_build"]["lockfile"].update(bytes=1), "bytes must be")

    def test_unknown_source_build_key_refused(self) -> None:
        self.refused(lambda raw: raw["source_build"].update(vendor="crates.io"), "unknown keys")


class RefusedRuntimeTest(DescriptorTest):
    def test_duplicate_soname_refused(self) -> None:
        def mutate(raw):
            raw["runtime"]["libraries"].append(
                {"soname": "libc.so.6", "package": "libc6", "version": "2.34", "symbols": "GLIBC_"},
            )
        self.refused(mutate, "twice")

    def test_an_unreviewed_library_refused(self) -> None:
        self.refused(
            lambda raw: raw["runtime"]["libraries"].append(
                {"soname": "libgtk-3.so.0", "package": "libgtk-3-0", "version": "3.24.0",
                 "symbols": "GTK_"},
            ),
            "is not reviewed",
        )

    def test_a_dropped_library_refused(self) -> None:
        def mutate(raw):
            raw["runtime"]["libraries"].pop()
        self.refused(mutate, "must cover exactly")

    def test_a_widened_floor_refused(self) -> None:
        """A floor the ELF evidence does not support cannot be claimed in JSON."""
        def mutate(raw):
            raw["runtime"]["libraries"][0]["version"] = "2.41"
        self.refused(mutate, "must be exactly")

    def test_a_narrowed_floor_refused(self) -> None:
        def mutate(raw):
            raw["runtime"]["libraries"][0]["version"] = "2.2"
        self.refused(mutate, "must be exactly")

    def test_another_symbol_namespace_refused(self) -> None:
        def mutate(raw):
            raw["runtime"]["libraries"][0]["symbols"] = "GLIBCXX_"
        self.refused(mutate, "must be exactly")

    def test_non_library_soname_refused(self) -> None:
        def mutate(raw):
            raw["runtime"]["libraries"][0]["soname"] = "cargo"
        self.refused(mutate, "not reviewed")

    def test_unknown_runtime_library_key_refused(self) -> None:
        def mutate(raw):
            raw["runtime"]["libraries"][0]["source"] = "readelf"
        self.refused(mutate, "unknown keys")

    def test_relation_shaped_extra_depends_refused(self) -> None:
        self.refused(lambda raw: raw["runtime"].update(extra_depends="cargo; rm -rf /"),
                     "invalid candidate runtime extra_depends")


class SourceDocumentTest(DescriptorTest):
    def test_the_shipped_document_passes(self) -> None:
        check_source_document(document_path(), "2.12.2")

    def test_a_missing_document_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "is missing"):
            check_source_document(self.root / "absent.md", "2.12.2")

    def test_a_document_dropping_a_pin_is_refused(self) -> None:
        text = document_path().read_text(encoding="utf-8").replace(REVIEWED_COMMIT, "HEAD")
        self.document.write_text(text, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "does not state the reviewed source commit"):
            check_source_document(self.document, "2.12.2")

    def test_a_document_dropping_the_build_command_is_refused(self) -> None:
        text = document_path().read_text(encoding="utf-8").replace(
            " ".join(REVIEWED_BUILD_COMMAND), "cargo build --release",
        )
        self.document.write_text(text, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "reviewed build command"):
            check_source_document(self.document, "2.12.2")

    def test_a_document_pointing_at_another_tree_is_refused(self) -> None:
        text = document_path().read_text(encoding="utf-8").replace(
            "<https://github.com/tauri-apps/tauri/tree/tauri-cli-v2.12.2>",
            "<https://example.invalid/tauri>",
        )
        self.document.write_text(text, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "reviewed source url"):
            check_source_document(self.document, "2.12.2")

    def test_a_duplicated_table_row_is_refused(self) -> None:
        row = f"| Cargo profile | `{REVIEWED_PROFILE}` |"
        text = document_path().read_text(encoding="utf-8").replace(row, f"{row}\n{row}")
        self.document.write_text(text, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "inconsistent reviewed source table rows"):
            check_source_document(self.document, "2.12.2")

    def test_every_reviewed_reference_is_checked(self) -> None:
        labels = {label for label, _ in reviewed_source_refs("2.12.2")}
        self.assertLessEqual(
            {"source url", "release tag", "source commit", "lockfile git blob", "crate manifest",
             "toolchain", "target triple", "cargo profile", "build command", "official asset",
             "official asset digest"},
            labels,
        )

    def test_an_unrelated_document_is_refused_by_the_loader(self) -> None:
        self.document.write_text("# nothing reviewed here\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "does not state the reviewed"):
            load_candidate(self.write())


class ReviewedPinsTest(unittest.TestCase):
    def test_the_descriptor_agrees_with_the_module_pins(self) -> None:
        raw = json.loads(descriptor_path().read_text(encoding="utf-8"))
        self.assertEqual(raw["source"]["commit"], REVIEWED_COMMIT)
        self.assertEqual(raw["source_build"]["lockfile"]["git_blob"], REVIEWED_LOCK_BLOB)
        self.assertEqual(raw["source_build"]["lockfile"]["sha256"], REVIEWED_LOCK_SHA256)
        self.assertEqual(raw["source_build"]["target"], REVIEWED_TARGET)
        self.assertEqual(raw["source_build"]["profile"], REVIEWED_PROFILE)
        self.assertEqual(raw["source_build"]["toolchain"], REVIEWED_TOOLCHAIN)


if __name__ == "__main__":
    unittest.main()
