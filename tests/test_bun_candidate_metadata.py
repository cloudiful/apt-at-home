"""The candidate descriptor is a closed contract, so it is tested as one.

`candidate.json` is the only place Bun's provenance is written down, and it sits
outside the production catalog. That makes two things worth proving: that a
reviewer can load the shipped file and get exactly the reviewed pins, and that
no single-field edit can quietly widen what the candidate will accept — a
different host, a floating ref, a short hash, or a member name we did not review.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from scripts.bun_candidate_metadata import (
    default_candidate_path,
    license_bodies,
    load_candidate,
    reviewed_source_refs,
)

EXPECTED_TAG = "bun-v1.4.3"
EXPECTED_ASSETS = {
    "amd64": "bun-linux-x64-baseline.zip",
    "arm64": "bun-linux-aarch64.zip",
}
EXPECTED_MEMBERS = {
    "amd64": "bun-linux-x64-baseline/bun",
    "arm64": "bun-linux-aarch64/bun",
}


def shipped() -> dict:
    return json.loads(default_candidate_path().read_text(encoding="utf-8"))


def write_variant(root: Path, raw: dict) -> Path:
    """Write a variant descriptor next to a copy of the real source document.

    The loader validates the installed document against the descriptor for every
    path, including an explicitly supplied one, so a fixture that writes only a
    descriptor is incomplete.
    """
    path = root / "candidate.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    shutil.copyfile(default_candidate_path().parent / "README.source",
                    root / "README.source")
    return path


class ShippedDescriptorTest(unittest.TestCase):
    """The descriptor that is actually committed must load and pin what we reviewed."""

    def test_loads_into_the_existing_dataclasses(self) -> None:
        manifest, pkg = load_candidate().manifest, load_candidate().package
        self.assertEqual(pkg.name, "bun")
        self.assertEqual(pkg.version, "1.4.3")
        self.assertEqual(manifest.debian_revision, "-1")
        self.assertEqual(manifest.packages, (pkg,))
        self.assertEqual(sorted(pkg.targets), ["amd64", "arm64"])

    def test_pins_the_exact_release_assets(self) -> None:
        for arch, asset in EXPECTED_ASSETS.items():
            target = load_candidate().package.targets[arch]
            self.assertEqual(
                target.url,
                f"https://github.com/oven-sh/bun/releases/download/{EXPECTED_TAG}/{asset}",
            )
            self.assertEqual(target.archive_member, EXPECTED_MEMBERS[arch])
            self.assertEqual(target.binary, "bun")

    def test_every_pin_is_a_full_sha256_and_a_real_size(self) -> None:
        for arch in EXPECTED_ASSETS:
            self.assertRegex(load_candidate().package.targets[arch].sha256, r"^[0-9a-f]{64}$")
            self.assertGreater(load_candidate().sizes[arch], 0)

    def test_depends_is_glibc_only(self) -> None:
        for arch in EXPECTED_ASSETS:
            self.assertEqual(load_candidate().package.targets[arch].depends, "libc6 (>= 2.17)")

    def test_source_reference_is_pinned_not_floating(self) -> None:
        source = shipped()["source"]
        self.assertEqual(source["tag"], EXPECTED_TAG)
        for key in ("commit", "webview_commit", "tinycc_commit"):
            self.assertRegex(source[key], r"^[0-9a-f]{40}$")
            self.assertNotIn("main", source[key])

    def test_license_label_is_custom_not_mit_only(self) -> None:
        """Bun is MIT but statically links LGPL-2 code, so the label says so."""
        self.assertEqual(shipped()["license"], "Bun-upstream")
        self.assertNotEqual(shipped()["license"], "MIT")

    def test_source_document_states_the_same_pins(self) -> None:
        """The shipped README.source must agree with the shipped descriptor."""
        document = default_candidate_path().parent / "README.source"
        text = document.read_text(encoding="utf-8")
        source = shipped()["source"]
        for key in ("tag", "commit", "webview_commit", "tinycc_commit"):
            self.assertIn(source[key], text, key)
        self.assertIn("tcc.h.patch", text)

    def test_license_bodies_are_the_three_reviewed_texts(self) -> None:
        bodies = license_bodies()
        self.assertEqual(len(bodies), 3)
        self.assertIn("MIT-licensed", bodies[0])
        self.assertIn("GNU LIBRARY GENERAL PUBLIC LICENSE", bodies[1])
        self.assertIn("GNU LESSER GENERAL PUBLIC LICENSE", bodies[2])
        for body in bodies:
            self.assertTrue(body.strip())


class DescriptorRejectionTest(unittest.TestCase):
    """Every field is closed: a bad edit must fail, not fall back to a default."""

    def _refuses(self, raw: dict, pattern: str) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, pattern):
                load_candidate(write_variant(Path(tmp), raw))

    def test_missing_file_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "not found"):
            load_candidate(Path("/nonexistent/candidate.json"))

    def test_invalid_json_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "candidate.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not valid JSON"):
                load_candidate(path)

    def test_unknown_top_level_key_refused(self) -> None:
        raw = shipped() | {"extra": 1}
        self._refuses(raw, "unknown keys")

    def test_missing_top_level_key_refused(self) -> None:
        raw = shipped()
        del raw["homepage"]
        self._refuses(raw, "missing keys")

    def test_foreign_package_refused(self) -> None:
        self._refuses(shipped() | {"package": "deno"}, "other than bun")

    def test_unknown_schema_refused(self) -> None:
        self._refuses(shipped() | {"schema": "apt-at-home/other/1"}, "unsupported candidate schema")

    def test_floating_version_refused(self) -> None:
        self._refuses(shipped() | {"version": "latest"}, "invalid candidate version")

    def test_unpinned_debian_revision_refused(self) -> None:
        self._refuses(shipped() | {"debian_revision": "-2"}, "debian_revision")

    def test_license_set_is_fixed(self) -> None:
        self._refuses(shipped() | {"license_files": ["LICENSE.txt"]}, "must be exactly")

    def test_source_tag_must_match_version(self) -> None:
        raw = shipped()
        raw["source"]["tag"] = "bun-v1.4.2"
        self._refuses(raw, "source tag must be exactly 'bun-v1.4.3'")

    def test_short_commit_refused(self) -> None:
        raw = shipped()
        raw["source"]["commit"] = "c6da4a4"
        self._refuses(raw, "source commit must be exactly")

    def test_well_formed_but_unreviewed_commit_refused(self) -> None:
        """A valid 40-hex id is still refused: shape is not provenance."""
        raw = shipped()
        raw["source"]["commit"] = "0" * 40
        self._refuses(raw, "source commit must be exactly")

    def test_unrelated_https_source_url_refused(self) -> None:
        raw = shipped()
        raw["source"]["url"] = "https://example.invalid/unrelated"
        self._refuses(raw, "source url must be exactly")

    def test_non_https_source_url_refused(self) -> None:
        raw = shipped()
        raw["source"]["url"] = "http://github.com/oven-sh/bun"
        self._refuses(raw, "source url must be exactly")

    def test_unrelated_patch_url_refused(self) -> None:
        raw = shipped()
        raw["source"]["patch_url"] = "https://example.invalid/not-a-patch"
        self._refuses(raw, "source patch_url must be exactly")

    def test_missing_source_key_refused(self) -> None:
        raw = shipped()
        del raw["source"]["tinycc_commit"]
        self._refuses(raw, r"source missing key: \['tinycc_commit'\]")

    def test_unknown_source_key_refused(self) -> None:
        raw = shipped()
        raw["source"]["unexpected"] = True
        self._refuses(raw, r"source has unknown keys: \['unexpected'\]")

    def test_unknown_target_key_refused(self) -> None:
        raw = shipped()
        raw["targets"]["amd64"]["unexpected"] = True
        self._refuses(raw, r"target amd64 has unknown keys: \['unexpected'\]")

    def test_missing_target_key_refused(self) -> None:
        raw = shipped()
        del raw["targets"]["amd64"]["member"]
        self._refuses(raw, r"target amd64 missing key: \['member'\]")

    def test_webview_commit_must_be_the_reviewed_one(self) -> None:
        raw = shipped()
        raw["source"]["webview_commit"] = "a" * 40
        self._refuses(raw, "source webview_commit must be exactly")


    def test_targets_must_cover_exactly_both_arches(self) -> None:
        raw = shipped()
        del raw["targets"]["arm64"]
        self._refuses(raw, "must cover exactly")

    def test_foreign_host_refused(self) -> None:
        raw = shipped()
        raw["targets"]["amd64"]["url"] = (
            "https://example.invalid/releases/download/bun-v1.4.3/bun-linux-x64-baseline.zip"
        )
        self._refuses(raw, "must be exactly")

    def test_short_sha256_refused(self) -> None:
        raw = shipped()
        raw["targets"]["amd64"]["sha256"] = "deadbeef"
        self._refuses(raw, "full sha256")

    def test_non_positive_size_refused(self) -> None:
        raw = shipped()
        raw["targets"]["amd64"]["size"] = 0
        self._refuses(raw, "positive size")

    def test_unreviewed_member_name_refused(self) -> None:
        raw = shipped()
        raw["targets"]["amd64"]["member"] = "bun-linux-x64-baseline/evil"
        self._refuses(raw, "must be 'bun-linux-x64-baseline/bun'")

    def test_arch_swapped_member_refused(self) -> None:
        raw = shipped()
        raw["targets"]["arm64"]["member"] = EXPECTED_MEMBERS["amd64"]
        self._refuses(raw, "must be 'bun-linux-aarch64/bun'")


class SourceDocumentTest(unittest.TestCase):
    """The installed source document is validated for every descriptor path."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.real = (default_candidate_path().parent / "README.source").read_text()
        self.raw = shipped()

    def _write(self, document: str) -> Path:
        raw = json.loads(json.dumps(self.raw))
        path = self.root / "candidate.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        (self.root / "README.source").write_text(document, encoding="utf-8")
        return path

    def _refuses_document(self, document: str, pattern: str) -> None:
        path = self._write(document)
        with self.assertRaisesRegex(ValueError, pattern):
            load_candidate(path)

    def test_the_shipped_document_is_accepted(self) -> None:
        self.assertEqual(load_candidate().package.version, "1.4.3")

    def test_explicit_shipped_descriptor_path_is_also_validated(self) -> None:
        """Passing the default path explicitly must not skip the document check."""
        load_candidate(default_candidate_path())

    def test_swapped_source_host_refused(self) -> None:
        """The tag still matches, so only a whole-URL check can catch this."""
        changed = self.real.replace(
            self.raw["source"]["url"], "https://example.invalid/tree/bun-v1.4.3"
        )
        self._refuses_document(changed, "does not state the reviewed source url")

    def test_swapped_patch_url_refused(self) -> None:
        """The patch basename still matches, so a basename check is not enough."""
        changed = self.real.replace(
            self.raw["source"]["patch_url"],
            "https://example.invalid/patches/tcc.h.patch",
        )
        self._refuses_document(changed, "does not state the reviewed patch url")

    def test_source_url_must_be_the_source_tree_link(self) -> None:
        url = self.raw["source"]["url"]
        changed = self.real.replace(url, "https://example.invalid/tree/bun-v1.4.3")
        changed += f"\nReference copied elsewhere: <{url}>\n"
        self._refuses_document(changed, "under Source tree")

    def test_patch_url_must_be_in_the_pinned_patch_section(self) -> None:
        url = self.raw["source"]["patch_url"]
        changed = self.real.replace(url, "https://example.invalid/patches/tcc.h.patch")
        changed += f"\nReference copied elsewhere: <{url}>\n"
        self._refuses_document(changed, "in its patch section")

    def test_empty_document_refused(self) -> None:
        self._refuses_document("", "does not state the reviewed source url")

    def test_document_missing_one_commit_refused(self) -> None:
        changed = self.real.replace(self.raw["source"]["webview_commit"], "redacted")
        self._refuses_document(changed, "does not state the reviewed webkit commit")

    def test_document_missing_a_build_script_refused(self) -> None:
        changed = self.real.replace("build:local", "make")
        self._refuses_document(changed, "does not state the reviewed build script")

    def test_missing_document_file_refused(self) -> None:
        path = self.root / "candidate.json"
        path.write_text(json.dumps(self.raw), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "is missing"):
            load_candidate(path)

    def test_every_reviewed_ref_is_required(self) -> None:
        refs = reviewed_source_refs("1.4.3")
        self.assertEqual(len(refs), 8)
        for _, reference in refs:
            self.assertIn(reference, self.real)


class CandidateIsolationTest(unittest.TestCase):
    """The candidate must be invisible to the production pipeline."""

    def test_bun_is_absent_from_the_production_catalog(self) -> None:
        from scripts.catalog import KNOWN_PACKAGES, RECIPES

        self.assertNotIn("bun", KNOWN_PACKAGES)
        self.assertNotIn("bun", RECIPES)

    def test_production_manifest_declares_eleven_packages_without_bun(self) -> None:
        from scripts.manifest import load_manifest

        manifest = load_manifest()
        names = {pkg.name for pkg in manifest.packages}
        self.assertNotIn("bun", names)
        self.assertIn("kandev", names)
        self.assertIn("cargo-tauri", names)
        self.assertEqual(len(names), 11)

    def test_default_path_points_at_the_repo_descriptor(self) -> None:
        self.assertTrue(default_candidate_path().is_file())
        self.assertEqual(default_candidate_path().name, "candidate.json")


if __name__ == "__main__":
    unittest.main()