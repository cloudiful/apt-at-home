"""The glab recipe: official GitLab release tarballs for amd64 and arm64.

GitLab serves an unsigned checksums.txt beside each release, so the archives are
pinned by the digests that file names. Only the reviewed project, source, tag
prefix, asset names, archive member, binary, smoke command, license source, and
dependency policy are pinned here; the version and the digests come from the
manifest and from discovery.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.catalog import (
    KNOWN_PACKAGES,
    arches_for,
    archive_member,
    archive_suffix_for,
    asset_name,
    binary_name,
    depends_for,
    exact_tag_release_for,
    expected_license_for,
    extra_license_paths_for,
    install_tree_for,
    license_path_for,
    repo_of,
    smoke_args,
    source_for,
    tag_for,
    triple_for,
)
from scripts.deb import build_deb, stage_package
from scripts.manifest import load_manifest
from scripts.package_binaries import extract_binaries
from scripts.upstream_assets import license_urls_for
from tests.helpers import write_fake_archive

REPO_ROOT = Path(__file__).resolve().parent.parent

VERSION = "1.124.0"
TAG = "v1.124.0"
AMD64_ASSET = "glab_1.124.0_linux_amd64.tar.gz"
ARM64_ASSET = "glab_1.124.0_linux_arm64.tar.gz"
AMD64_SHA256 = "63df684842b0697e418649dd64374d37ac1da0fbcdcb422d6a5557222f8bb40f"
ARM64_SHA256 = "0716395a0efeb991fc844b33fe55310645afcbd9b9746e50c725e9d1cd1b8d16"
RELEASE_URL = "https://gitlab.com/gitlab-org/cli/-/releases"
LICENSE_URL = "https://gitlab.com/gitlab-org/cli/-/raw/v1.124.0/LICENSE"


class GlabRecipeTest(unittest.TestCase):
    def test_the_package_is_registered_for_both_architectures(self) -> None:
        self.assertIn("glab", KNOWN_PACKAGES)
        self.assertEqual(arches_for("glab"), ("amd64", "arm64"))
        self.assertFalse(exact_tag_release_for("glab"))

    def test_the_source_is_gitlab_and_the_project_is_the_reviewed_one(self) -> None:
        self.assertEqual(source_for("glab"), "gitlab")
        self.assertEqual(repo_of("glab"), "gitlab-org/cli")
        self.assertEqual(tag_for("glab", VERSION), TAG)
        self.assertEqual(tag_for("glab", "1.125.0"), "v1.125.0")

    def test_assets_are_the_official_tarballs(self) -> None:
        self.assertEqual(asset_name("glab", VERSION, "amd64"), AMD64_ASSET)
        self.assertEqual(asset_name("glab", VERSION, "arm64"), ARM64_ASSET)
        self.assertEqual(archive_suffix_for("glab", VERSION, "amd64"), ".tar.gz")
        self.assertEqual(archive_suffix_for("glab", VERSION, "arm64"), ".tar.gz")
        # GitLab's asset names carry the Debian architecture, not a Rust triple.
        with self.assertRaises(ValueError):
            triple_for("glab", "amd64")

    def test_the_member_is_the_nested_binary(self) -> None:
        self.assertEqual(archive_member("glab", VERSION, "amd64"), "bin/glab")
        self.assertEqual(archive_member("glab", "1.125.0", "arm64"), "bin/glab")
        self.assertEqual(binary_name("glab"), "glab")

    def test_the_package_is_a_flat_member_not_a_tree(self) -> None:
        self.assertEqual(install_tree_for("glab", VERSION, "amd64"), "")
        self.assertEqual(install_tree_for("glab", VERSION, "arm64"), "")

    def test_the_smoke_command_is_the_clis_version_subcommand(self) -> None:
        self.assertEqual(smoke_args("glab"), ("version",))

    def test_both_static_builds_declare_no_runtime_dependencies(self) -> None:
        self.assertEqual(depends_for("glab", "amd64"), "")
        self.assertEqual(depends_for("glab", "arm64"), "")

    def test_the_license_is_the_tags_mit_text(self) -> None:
        self.assertEqual(expected_license_for("glab"), "MIT")
        self.assertEqual(license_path_for("glab"), "LICENSE")
        self.assertEqual(extra_license_paths_for("glab"), ())
        self.assertEqual(license_urls_for("glab", TAG), (LICENSE_URL,))

    def test_the_vendored_license_is_the_upstream_bytes(self) -> None:
        vendor = (REPO_ROOT / "packaging" / "licenses" / "glab" / "LICENSE.txt").read_bytes()
        self.assertEqual(len(vendor), 1107)
        text = vendor.decode("utf-8")
        self.assertTrue(text.startswith("MIT License\n"))
        self.assertIn("Copyright (c) 2022-present GitLab Inc.", text)
        self.assertIn("Copyright (c) 2019 GitHub Inc.", text)

    def test_the_committed_manifest_carries_the_reviewed_entry(self) -> None:
        manifest = load_manifest()
        pkg = next(p for p in manifest.packages if p.name == "glab")
        self.assertEqual(pkg.version, VERSION)
        self.assertEqual(pkg.license, "MIT")
        self.assertEqual(pkg.homepage, "https://gitlab.com/gitlab-org/cli")
        self.assertEqual(pkg.license_url, LICENSE_URL)
        self.assertEqual(pkg.upstream_published_at, "2026-10-20T12:17:01Z")
        self.assertEqual(set(pkg.targets), {"amd64", "arm64"})
        amd64 = pkg.targets["amd64"]
        self.assertEqual(amd64.url, f"{RELEASE_URL}/{TAG}/downloads/{AMD64_ASSET}")
        self.assertEqual(amd64.sha256, AMD64_SHA256)
        self.assertEqual((amd64.archive_member, amd64.binary), ("bin/glab", "glab"))
        self.assertEqual(amd64.install_tree, "")
        self.assertEqual(amd64.depends, "")
        arm64 = pkg.targets["arm64"]
        self.assertEqual(arm64.url, f"{RELEASE_URL}/{TAG}/downloads/{ARM64_ASSET}")
        self.assertEqual(arm64.sha256, ARM64_SHA256)
        self.assertEqual((arm64.archive_member, arm64.binary), ("bin/glab", "glab"))
        self.assertEqual(arm64.install_tree, "")
        self.assertEqual(arm64.depends, "")


class GlabPackageTest(unittest.TestCase):
    def test_the_tarball_member_becomes_an_executable_package_with_the_notice(self) -> None:
        manifest = load_manifest()
        pkg = next(p for p in manifest.packages if p.name == "glab")
        target = pkg.targets["amd64"]
        payload = b"\x7fELF-fake-glab"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = write_fake_archive(
                root / AMD64_ASSET, member=target.archive_member, payload=payload,
            )
            sources = extract_binaries(archive, target, root / "extract")
            self.assertEqual(set(sources), {"glab"})
            self.assertEqual(sources["glab"].read_bytes(), payload)
            stage = stage_package(
                pkg=pkg, arch="amd64", manifest=manifest,
                binary_file=sources["glab"], stage_root=root / "stages",
            )
            self.assertTrue((stage / "usr" / "bin" / "glab").exists())
            control = (stage / "DEBIAN" / "control").read_text()
            self.assertNotIn("Depends:", control)
            copyright = (stage / "usr" / "share" / "doc" / "glab" / "copyright").read_text()
            self.assertIn("License: MIT", copyright)
            self.assertIn("Copyright (c) 2022-present GitLab Inc.", copyright)
            deb = root / "glab.deb"
            build_deb(stage, deb)
            listing = subprocess.run(
                ["dpkg-deb", "-c", str(deb)], text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(listing.returncode, 0, msg=listing.stderr)
            self.assertIn("./usr/bin/glab", listing.stdout)
            self.assertIn("./usr/share/doc/glab/copyright", listing.stdout)


if __name__ == "__main__":
    unittest.main()
