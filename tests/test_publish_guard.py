"""Manual publish must never downgrade what automatic updates already published.

The baseline is judged from the verified signed `Packages` stanzas, so these
tests publish a real signed release into an in-memory store and then try to
replace it with an older, equal, and newer manifest.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.catalog import canonical_source_for, tag_for
from scripts.manifest import Manifest, load_manifest, parse_manifest
from scripts.publish_guard import (
    check_not_older,
    guard_publish,
    published_versions,
    upstream_version,
)
from scripts.release_manifest import validated_document
from scripts.release_verify import BaselineUnavailable, TrustedRelease, restore_metadata
from scripts.storage import ObjectNotFound
from scripts.upstream_assets import asset_name, license_url, release_download_url
from tests.helpers import make_gpg_home_with_key, move_package_version
from tests.release_fixtures import build_release_objects


class Store:
    """Read-only object store double for the published baseline."""

    bucket = "test-bucket"

    def __init__(self, client) -> None:  # type: ignore[no-untyped-def]
        self.client = client

    def get(self, key: str, *, max_bytes: int) -> bytes:
        payload = self.client.objects.get((self.bucket, key))
        if payload is None:
            raise ObjectNotFound(key)
        if len(payload) > max_bytes:
            raise ValueError(f"object {key} exceeded the {max_bytes} byte limit")
        return payload


class _Client:
    def __init__(self, objects: dict[tuple[str, str], bytes]) -> None:
        self.objects = objects


def _with_bumped_version(data: dict, name: str, version: str) -> dict:
    """Copy a manifest document with one package moved to a newer upstream version."""
    import copy

    out = copy.deepcopy(data)
    for entry in out["packages"]:
        if entry["name"] == name:
            move_package_version(entry, version)
    return out


class UpstreamVersionTest(unittest.TestCase):
    def test_the_fixed_debian_revision_is_stripped(self) -> None:
        self.assertEqual(upstream_version("0.45.1-1"), "0.45.1")
        self.assertEqual(upstream_version("1.27.0-1"), "1.27.0")

    def test_a_version_without_a_revision_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            upstream_version("0.45.1")


class PublishedVersionTest(unittest.TestCase):
    def _release(self, amd64: str, arm64: str):  # type: ignore[no-untyped-def]
        return TrustedRelease(
            release_id="r", key_bytes=b"",
            manifest_data={}, manifest=load_manifest(),
            packages={"amd64": amd64, "arm64": arm64},
        )

    def test_versions_come_from_the_verified_stanzas(self) -> None:
        stanza = (
            "Package: zellij\nVersion: 0.45.1-1\nArchitecture: amd64\n"
            "Filename: pool/zellij_0.45.1-1_amd64.deb\n"
        )
        release = self._release(stanza, stanza.replace("amd64", "arm64"))
        self.assertEqual(published_versions(release), {"zellij": "0.45.1-1"})

    def test_architectures_that_disagree_are_refused(self) -> None:
        release = self._release(
            "Package: zellij\nVersion: 0.45.1-1\nArchitecture: amd64\n"
            "Filename: pool/zellij_0.45.1-1_amd64.deb\n",
            "Package: zellij\nVersion: 0.46.0-1\nArchitecture: arm64\n"
            "Filename: pool/zellij_0.46.0-1_arm64.deb\n",
        )
        with self.assertRaises(BaselineUnavailable):
            published_versions(release)


class CandidateComparisonTest(unittest.TestCase):
    def _manifest(self, starship: str, zellij: str):  # type: ignore[no-untyped-def]
        from scripts.release_manifest import validated_document
        from scripts.manifest import default_manifest_path

        data, _ = validated_document(default_manifest_path())
        for entry in data["packages"]:
            if entry["name"] == "starship":
                move_package_version(entry, starship)
            elif entry["name"] == "zellij":
                move_package_version(entry, zellij)
        return parse_manifest(data)

    def test_an_older_package_is_refused(self) -> None:
        baseline = {"starship": "1.27.0-1", "zellij": "0.46.0-1"}
        with self.assertRaises(ValueError) as ctx:
            check_not_older(self._manifest("1.26.0", "0.46.0"), baseline)
        self.assertIn("must not downgrade", str(ctx.exception))
        self.assertIn("starship", str(ctx.exception))

    def test_equal_and_newer_are_allowed(self) -> None:
        baseline = {"starship": "1.27.0-1", "zellij": "0.46.0-1"}
        check_not_older(self._manifest("1.27.0", "0.46.0"), baseline)
        check_not_older(self._manifest("1.28.0", "0.47.0"), baseline)

    def test_a_package_absent_from_the_index_is_not_a_downgrade(self) -> None:
        check_not_older(self._manifest("1.27.0", "0.46.0"), {"zellij": "0.46.0-1"})


def _forgejo_entry(version: str) -> dict:
    """One reviewed Forgejo CLI entry at a version, with its canonical URLs."""
    source, repo = canonical_source_for("forgejo-cli", version)
    tag = tag_for("forgejo-cli", version)
    return {
        "name": "forgejo-cli",
        "version": version,
        "homepage": "https://github.com/cloudiful/forgejo-cli",
        "description": "Forgejo CLI — cloudiful personal-use fork",
        "license": "MIT OR Apache-2.0",
        "copyright": "Copyright (c) 2026 The forgejo-cli developers",
        "license_url": license_url(repo, tag, "LICENSE-MIT", source=source),
        "upstream_published_at": "2026-10-24T15:48:56Z",
        "extra_license_urls": [license_url(repo, tag, "LICENSE-APACHE", source=source)],
        "targets": {
            arch: {
                "url": release_download_url(
                    repo, tag, asset_name("forgejo-cli", version, arch), source=source
                ),
                "sha256": "a" * 64,
                "archive_member": "fj",
                "binary": "fj",
                "depends": "libc6 (>= 2.39), libgcc-s1, libssl3t64, zlib1g",
            }
            for arch in ("amd64", "arm64")
        },
    }


class ForkVersionGuardTest(unittest.TestCase):
    """A fork revision orders like the Debian version comparison."""

    def _manifest(self, version: str) -> Manifest:
        from scripts.manifest import default_manifest_path

        data, _ = validated_document(default_manifest_path())
        data["packages"] = [
            _forgejo_entry(version) if entry["name"] == "forgejo-cli" else entry
            for entry in data["packages"]
        ]
        return parse_manifest(data)

    def test_a_fork_revision_is_not_a_downgrade_of_its_base(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            check_not_older(
                self._manifest("0.7.0"), {"forgejo-cli": "0.7.0+cloudiful.2-1"}
            )
        self.assertIn("must not downgrade", str(ctx.exception))
        self.assertIn("forgejo-cli", str(ctx.exception))

    def test_a_fork_revision_upgrades_the_published_base(self) -> None:
        check_not_older(
            self._manifest("0.7.0+cloudiful.2"), {"forgejo-cli": "0.7.0-1"}
        )

    def test_a_later_fork_revision_upgrades_an_earlier_one(self) -> None:
        check_not_older(
            self._manifest("0.7.0+cloudiful.10"), {"forgejo-cli": "0.7.0+cloudiful.2-1"}
        )

    def test_the_next_upstream_release_upgrades_the_latest_fork_revision(self) -> None:
        check_not_older(
            self._manifest("0.7.1"), {"forgejo-cli": "0.7.0+cloudiful.10-1"}
        )

    def test_the_guard_agrees_with_the_debian_version_comparison(self) -> None:
        cases = [
            ("0.7.0+cloudiful.2", "0.7.0-1"),
            ("0.7.0+cloudiful.10", "0.7.0+cloudiful.2-1"),
            ("0.7.1", "0.7.0+cloudiful.10-1"),
        ]
        for newer, older in cases:
            with self.subTest(newer=newer, older=older):
                compared = subprocess.run(
                    ["dpkg", "--compare-versions", newer, "gt", older],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertEqual(compared.returncode, 0, msg=compared.stderr)
                check_not_older(self._manifest(newer), {"forgejo-cli": older})
                with self.assertRaises(ValueError):
                    check_not_older(self._manifest(older), {"forgejo-cli": newer})


class GuardPublishTest(unittest.TestCase):
    def test_a_first_release_with_no_baseline_is_allowed(self) -> None:
        from scripts.manifest import load_manifest

        note = guard_publish(
            candidate=load_manifest(), source=Store(_Client({})), fingerprint="A" * 40
        )
        self.assertIn("nothing to downgrade", note)

    def test_a_downgrade_against_a_real_signed_release_is_refused(self) -> None:
        from scripts.manifest import default_manifest_path
        from scripts.release_manifest import load_manifest_data

        tmpdir, home, key_id = make_gpg_home_with_key()
        try:
            with tempfile.TemporaryDirectory() as work:
                # The published baseline is built from the committed manifest,
                # which is what an automatic update has already shipped.
                objects = build_release_objects(Path(work) / "published", key_home=home, key_id=key_id)
                store = Store(_Client({(Store.bucket, k): v for k, v in objects.items()}))

                note = guard_publish(
                    candidate=parse_manifest(load_manifest_data(default_manifest_path())),
                    source=store,
                    fingerprint=key_id,
                )
                self.assertIn("published release", note)

                # A manual release of an older manifest is the downgrade to stop.
                older = parse_manifest(
                    _with_bumped_version(load_manifest_data(default_manifest_path()), "zellij", "0.44.0")
                )
                with self.assertRaises(ValueError) as ctx:
                    guard_publish(candidate=older, source=store, fingerprint=key_id)
                self.assertIn("must not downgrade", str(ctx.exception))
                self.assertIn("zellij", str(ctx.exception))

                # A newer manifest is the correct way to publish again.
                newer = parse_manifest(
                    _with_bumped_version(load_manifest_data(default_manifest_path()), "zellij", "0.46.0")
                )
                self.assertIn(
                    "published release",
                    guard_publish(candidate=newer, source=store, fingerprint=key_id),
                )
        finally:
            tmpdir.cleanup()

    def test_an_unreadable_baseline_is_not_treated_as_empty(self) -> None:
        from scripts.manifest import load_manifest

        tmpdir, home, key_id = make_gpg_home_with_key()
        try:
            with tempfile.TemporaryDirectory() as work:
                objects = build_release_objects(Path(work) / "published", key_home=home, key_id=key_id)
                # Drop the signed Release: the baseline can no longer be trusted.
                del objects["releases/20260102-030405/dists/stable/Release"]
                store = Store(_Client({(Store.bucket, k): v for k, v in objects.items()}))
                with self.assertRaises(Exception):
                    guard_publish(candidate=load_manifest(), source=store, fingerprint=key_id)
        finally:
            tmpdir.cleanup()


if __name__ == "__main__":
    unittest.main()