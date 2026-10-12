"""A source-built target has two honest shapes, and no third.

A *planned* identity names what a reviewed recipe would compile. A *realized*
source target adds the artifact an executed build measured. Everything here is
about the difference between those two and the failure modes that would collapse
them into one: an unknown key, a zero digest, an invented URL, a command that
disagrees with the block that declares it, or a planned target reaching the point
where a manifest is signed.
"""

from __future__ import annotations

import copy
import unittest

from scripts.manifest import default_manifest_path, parse_manifest
from scripts.release_manifest import load_manifest_data
from scripts.source_build import (
    SCHEMA,
    SOURCE_TARGETS,
    ArtifactProvenance,
    RealizedSource,
    SourceIdentity,
    artifact_of,
    check_realized_document,
    check_source_target,
    check_tag_version,
    document,
    identity_of,
    is_realized,
    parse_artifact,
    parse_source_target,
    realize_document,
    version_from_tag,
)
from scripts.tauri_source import source_policy

VERSION = "2.12.2"
COMMIT = "5b23c82ea5ba6b9aec420dd71f60493edc486aa9"
LOCK_BLOB = "6b052c58398b8d6ec246d77f65a748448cd4c859"
LOCK_SHA256 = "e2bd9e7d86c343e1b484ad3e6e30528451040894279009eb3ddb49b77338e1ca"
LOCK_BYTES = 279903
REPOSITORY = "https://github.com/tauri-apps/tauri"
TOOLCHAIN = "1.99.0"
ASSET_URL = "https://github.com/tauri-apps/tauri/releases/download/tauri-cli-v2.12.2/cargo-tauri-x86_64-unknown-linux-gnu.tgz"


def identity_document() -> dict[str, object]:
    """A valid planned source identity, in the exact shape a manifest carries."""
    build = source_policy()
    return {
        "schema": SCHEMA,
        "repository": REPOSITORY,
        "tag": f"tauri-cli-v{VERSION}",
        "commit": COMMIT,
        "lockfile": {
            "path": "Cargo.lock",
            "git_blob": LOCK_BLOB,
            "bytes": LOCK_BYTES,
            "sha256": LOCK_SHA256,
        },
        "toolchain": {"channel": TOOLCHAIN},
        "build": build.document(),
    }


def artifact_document(sha: str = "b" * 64, size: int = 4096, machine: int = 183) -> dict[str, object]:
    return {"sha256": sha, "bytes": size, "elf_machine": machine}


def source_manifest_document(source: dict[str, object] | None = None) -> dict[str, object]:
    """A complete manifest whose arm64 target is built from source."""
    base = copy.deepcopy(load_manifest_data(default_manifest_path()))
    resolved = copy.deepcopy(source or identity_document())
    tag = str(resolved.get("tag", f"tauri-cli-v{VERSION}"))
    version = tag.removeprefix("tauri-cli-v") if tag.startswith("tauri-cli-v") else VERSION
    asset_url = (
        f"https://github.com/tauri-apps/tauri/releases/download/{tag}/"
        "cargo-tauri-x86_64-unknown-linux-gnu.tgz"
    )
    return {
        "maintainer": base["maintainer"],
        "section": "utils",
        "priority": base["priority"],
        "debian_revision": "-1",
        "packages": [
            {
                "name": "cargo-tauri",
                "version": version,
                "homepage": "https://tauri.app",
                "description": "command line interface for building Tauri apps",
                "license": "MIT OR Apache-2.0",
                "copyright": "Tauri contributors",
                "license_url": f"https://raw.githubusercontent.com/tauri-apps/tauri/{tag}/LICENSE-MIT",
                "extra_license_urls": [
                    f"https://raw.githubusercontent.com/tauri-apps/tauri/{tag}/LICENSE-APACHE-2.0"
                ],
                "targets": {
                    "amd64": {
                        "url": asset_url,
                        "sha256": "a" * 64,
                        "archive_member": "cargo-tauri",
                        "binary": "cargo-tauri",
                    },
                    "arm64": {
                        "source_build": resolved,
                        "binary": "cargo-tauri",
                    },
                },
            }
        ],
    }


class ParsedShapesTest(unittest.TestCase):
    def test_a_planned_target_carries_no_artifact_facts(self) -> None:
        parsed = parse_source_target(identity_document())
        self.assertIsInstance(parsed, SourceIdentity)
        self.assertNotIsInstance(parsed, RealizedSource)
        self.assertFalse(is_realized(parsed))
        self.assertIsNone(artifact_of(parsed))
        self.assertEqual(identity_of(parsed), parsed)
        self.assertEqual(document(parsed), identity_document())

    def test_a_realized_target_adds_the_measured_artifact(self) -> None:
        raw = {**identity_document(), "artifact": artifact_document()}
        parsed = parse_source_target(raw)
        self.assertTrue(is_realized(parsed))
        assert isinstance(parsed, RealizedSource)
        self.assertEqual(parsed.artifact, ArtifactProvenance("b" * 64, 4096, 183))
        self.assertEqual(parsed.identity.tag, f"tauri-cli-v{VERSION}")
        self.assertEqual(document(parsed), raw)

    def test_the_reviewed_source_target_is_exactly_one_pair(self) -> None:
        self.assertEqual(SOURCE_TARGETS, frozenset({("cargo-tauri", "arm64")}))
        check_source_target("cargo-tauri", "arm64")
        for name, arch in (("cargo-tauri", "amd64"), ("zellij", "arm64")):
            with self.subTest(pair=(name, arch)):
                with self.assertRaisesRegex(ValueError, "may not be built from source"):
                    check_source_target(name, arch)

    def test_the_tag_must_name_the_package_version(self) -> None:
        check_tag_version(VERSION, f"tauri-cli-v{VERSION}", prefix="tauri-cli-v")
        with self.assertRaisesRegex(ValueError, "is not the canonical"):
            check_tag_version("2.12.3", f"tauri-cli-v{VERSION}", prefix="tauri-cli-v")

    def test_only_a_complete_canonical_version_satisfies_its_tag(self) -> None:
        """A version that merely ends the tag is not that release's version."""
        prefix = "tauri-cli-v"
        tag = f"tauri-cli-v{VERSION}"
        for rejected in ("12.2", "2.12", "2", "12.2.2", "v2.12.2", "2.12.2-rc1", ""):
            with self.subTest(version=rejected):
                with self.assertRaises(ValueError):
                    check_tag_version(rejected, tag, prefix=prefix)
        # A tag carrying another project's prefix is not this package's either.
        with self.assertRaises(ValueError):
            check_tag_version(VERSION, f"tauri-v{VERSION}", prefix=prefix)
        with self.assertRaises(ValueError):
            check_tag_version(VERSION, f"tauri-cli-v{VERSION}.1", prefix=prefix)
        # Only the complete version of that exact tag passes.
        self.assertEqual(version_from_tag(tag, prefix=prefix), VERSION)
        self.assertIsNone(version_from_tag(f"tauri-v{VERSION}", prefix=prefix))
        self.assertIsNone(version_from_tag(f"tauri-cli-v{VERSION}-rc1", prefix=prefix))


class RefusedShapeTest(unittest.TestCase):
    def test_unknown_keys_are_refused_at_every_level(self) -> None:
        unknown = (
            ("top", None, "payload_url", "https://example.invalid/x"),
            ("lockfile", "lockfile", "vcs", "git"),
            ("toolchain", "toolchain", "rustc", f"rustc {TOOLCHAIN} (deadbeef)"),
            ("build", "build", "cargo_home", "/tmp"),
            ("artifact", None, "machine", 183),
        )
        for label, block, key, value in unknown:
            with self.subTest(label):
                raw = identity_document()
                if label == "artifact":
                    raw["artifact"] = {**artifact_document(), key: value}
                elif block is None:
                    raw[key] = value
                else:
                    raw[block][key] = value  # type: ignore[index]
                with self.assertRaisesRegex(ValueError, "unknown keys"):
                    parse_source_target(raw)

    def test_missing_keys_are_refused(self) -> None:
        for key in ("repository", "tag", "commit", "lockfile", "toolchain", "build", "schema"):
            with self.subTest(key=key):
                raw = identity_document()
                del raw[key]
                with self.assertRaisesRegex(ValueError, "missing keys"):
                    parse_source_target(raw)

    def test_numbers_must_be_integers_not_strings_or_booleans(self) -> None:
        for value in ("279903", True, 0, -1):
            with self.subTest(value=value):
                raw = identity_document()
                raw["lockfile"]["bytes"] = value  # type: ignore[index]
                with self.assertRaises(ValueError):
                    parse_source_target(raw)
        raw = identity_document()
        raw["artifact"] = {**artifact_document(), "bytes": "4096"}
        with self.assertRaises(ValueError):
            parse_source_target(raw)

    def test_an_artifact_must_measure_something(self) -> None:
        for artifact in (
            artifact_document(sha="short"),
            artifact_document(sha="z" * 64),
            artifact_document(size=0),
            artifact_document(machine=0),
            artifact_document(machine="183"),
        ):
            with self.subTest(artifact=artifact):
                with self.assertRaises(ValueError):
                    parse_artifact(artifact)

    def test_upstream_references_are_validated(self) -> None:
        cases = {
            "repository": "https://gitlab.example/tauri-apps/tauri",
            "tag": "tauri cli v2",
            "commit": "5b23c82",
        }
        for key, value in cases.items():
            with self.subTest(key=key):
                raw = identity_document()
                raw[key] = value
                with self.assertRaises(ValueError):
                    parse_source_target(raw)

    def test_a_lockfile_path_may_not_escape_the_checkout(self) -> None:
        for path in ("../Cargo.lock", "/etc/Cargo.lock", "crates/../../Cargo.lock", ""):
            with self.subTest(path=path):
                raw = identity_document()
                raw["lockfile"]["path"] = path  # type: ignore[index]
                with self.assertRaises(ValueError):
                    parse_source_target(raw)

    def test_a_toolchain_channel_must_be_a_version(self) -> None:
        for channel in ("stable", "1.99.0-nightly", "", "1.99.0 (deadbeef)"):
            with self.subTest(channel=channel):
                raw = identity_document()
                raw["toolchain"] = {"channel": channel}
                with self.assertRaises(ValueError):
                    parse_source_target(raw)

    def test_the_command_must_run_the_declared_build(self) -> None:
        def with_command(parts: list[str]) -> dict[str, object]:
            raw = identity_document()
            raw["build"]["command"] = parts  # type: ignore[index]
            return raw

        build = list(source_policy().command)
        cases = {
            "no --locked": [part for part in build if part != "--locked"],
            "not cargo": ["sh", *build[1:]],
            "foreign target": [*build[:-3], "x86_64-unknown-linux-gnu", *build[-2:]],
            "injected argument": [*build, "; rm -rf /"],
            "argument with a space": [*build[:-1], "cargo tauri"],
            "no --bin value": [*build[:-1]],
        }
        for label, parts in cases.items():
            with self.subTest(label):
                with self.assertRaises(ValueError):
                    parse_source_target(with_command(parts))

    def test_the_reviewed_command_is_accepted(self) -> None:
        parsed = parse_source_target(identity_document())
        self.assertEqual(identity_of(parsed).build.command, source_policy().command)
        self.assertEqual(identity_of(parsed).build.command.count("--locked"), 1)


class RealizedDocumentTest(unittest.TestCase):
    def test_a_measurement_is_merged_into_the_plan(self) -> None:
        measured = ArtifactProvenance("c" * 64, 8192, 183)
        realized = realize_document(source_manifest_document(), {("cargo-tauri", "arm64"): measured})
        target = realized["packages"][0]["targets"]["arm64"]  # type: ignore[index]
        self.assertEqual(target["source_build"]["artifact"], measured.document())
        self.assertEqual(parse_source_target(target["source_build"]).artifact, measured)
        check_realized_document(realized)
        # The input document is never mutated in place.
        self.assertNotIn(
            "artifact", source_manifest_document()["packages"][0]["targets"]["arm64"]["source_build"]  # type: ignore[index]
        )

    def test_an_unmeasured_target_stays_planned_and_is_refused(self) -> None:
        planned = source_manifest_document()
        with self.assertRaisesRegex(ValueError, "must be realized before it is signed"):
            check_realized_document(planned)

    def test_a_published_artifact_is_never_overwritten_by_a_new_measurement(self) -> None:
        published = ArtifactProvenance("c" * 64, 8192, 183)
        realized = realize_document(
            source_manifest_document(), {("cargo-tauri", "arm64"): published}
        )
        again = realize_document(realized, {("cargo-tauri", "arm64"): published})
        self.assertEqual(
            again["packages"][0]["targets"]["arm64"]["source_build"]["artifact"],  # type: ignore[index]
            published.document(),
        )
        with self.assertRaisesRegex(ValueError, "a published release is immutable"):
            realize_document(
                realized, {("cargo-tauri", "arm64"): ArtifactProvenance("d" * 64, 8192, 183)}
            )

    def test_an_asset_only_manifest_needs_no_measurement(self) -> None:
        base = copy.deepcopy(load_manifest_data(default_manifest_path()))
        # The committed manifest carries a planned cargo-tauri source target;
        # an asset-only document has no source target to realize.
        base["packages"] = [p for p in base["packages"] if p["name"] != "cargo-tauri"]  # type: ignore[index]
        check_realized_document(base)
        self.assertEqual(list(realize_document(base, {})), list(base))


class SourceTargetInAManifestTest(unittest.TestCase):
    def test_a_source_target_parses_into_the_typed_target(self) -> None:
        manifest = parse_manifest(source_manifest_document())
        package = manifest.packages[0]
        self.assertEqual(package.name, "cargo-tauri")
        arm64 = package.targets["arm64"]
        self.assertTrue(arm64.is_source_target)
        self.assertEqual(arm64.url, "")
        self.assertEqual(arm64.sha256, "")
        self.assertEqual(arm64.archive_member, "")
        self.assertFalse(manifest.packages[0].targets["amd64"].is_source_target)

    def test_an_asset_target_may_not_declare_a_source_build(self) -> None:
        raw = source_manifest_document()
        raw["packages"][0]["targets"]["amd64"]["source_build"] = identity_document()  # type: ignore[index]
        with self.assertRaises(ValueError):
            parse_manifest(raw)

    def test_a_realized_source_target_survives_a_manifest_round_trip(self) -> None:
        raw = source_manifest_document(
            {**identity_document(), "artifact": artifact_document()}
        )
        manifest = parse_manifest(raw)
        target = manifest.packages[0].targets["arm64"]
        assert isinstance(target.source_build, RealizedSource)
        self.assertEqual(target.source_build.artifact.bytes, 4096)


if __name__ == "__main__":
    unittest.main()