"""The Forgejo CLI recipe: the personal-use fork's official release tarballs.

The Forgejo CLI is packaged from the `cloudiful/forgejo-cli` personal-use fork,
whose GitHub release publishes the official binary archives for amd64 and
arm64. The fork names its releases `vX.Y.Z-cloudiful.N`, which the Debian
upstream version `X.Y.Z+cloudiful.N` maps to, so the recipe pins the fork's
own repository, its exact-tag selection, its asset names, archive member,
binary, smoke command, license sources, and dependencies; the version and the
digests come from the manifest and from discovery.
"""

from __future__ import annotations

import hashlib
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
    extra_license_urls_for,
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
from scripts.recipes import Recipe
from tests.helpers import write_fake_archive

REPO_ROOT = Path(__file__).resolve().parent.parent

REPO = "cloudiful/forgejo-cli"
VERSION = "0.7.0+cloudiful.2"
TAG = "v0.7.0-cloudiful.2"
AMD64_ASSET = "forgejo-cli-x86_64-linux.tar.gz"
ARM64_ASSET = "forgejo-cli-aarch64-linux.tar.gz"
DEPENDS = "libc6 (>= 2.39), libgcc-s1, libssl3t64, zlib1g"
# The upstream license bytes vendored at the pinned tag.
MIT_SHA256 = "f4cfb2e3ed810aac9923da015c97868af7a668c1e8e7ebb8201ac53c8370c54a"
APACHE_SHA256 = "f4feae6d79a020a00852124ab4b76b9d0f786e0b9a727e14d7959ba09b2119fb"
# The digests of the official archives, as the fork's release reports them.
AMD64_SHA256 = "4e1e609bc89df86c28fc63f7158918c54098189117adba2c8ce6d7e86bf39164"
ARM64_SHA256 = "66bb4310f3d63c84e222d947ea09981d4fd7fd0ac52a40f01402f266984b896b"


class ForgejoCliRecipeTest(unittest.TestCase):
    def test_the_package_is_registered_for_both_architectures(self) -> None:
        self.assertIn("forgejo-cli", KNOWN_PACKAGES)
        self.assertEqual(arches_for("forgejo-cli"), ("amd64", "arm64"))

    def test_the_source_is_github_and_the_repository_is_the_reviewed_fork(self) -> None:
        self.assertEqual(source_for("forgejo-cli"), "github")
        self.assertEqual(repo_of("forgejo-cli"), REPO)

    def test_the_release_is_selected_by_its_exact_fork_tag(self) -> None:
        """The fork carries the upstream tags beside its own releases."""
        self.assertTrue(exact_tag_release_for("forgejo-cli"))

    def test_the_tag_carries_the_fork_revision(self) -> None:
        self.assertEqual(tag_for("forgejo-cli", VERSION), TAG)
        self.assertEqual(tag_for("forgejo-cli", "0.7.0+cloudiful.10"), "v0.7.0-cloudiful.10")

    def test_assets_are_the_official_tarballs(self) -> None:
        self.assertEqual(asset_name("forgejo-cli", VERSION, "amd64"), AMD64_ASSET)
        self.assertEqual(asset_name("forgejo-cli", VERSION, "arm64"), ARM64_ASSET)
        self.assertEqual(triple_for("forgejo-cli", "amd64"), "x86_64")
        self.assertEqual(triple_for("forgejo-cli", "arm64"), "aarch64")
        self.assertEqual(archive_suffix_for("forgejo-cli", VERSION, "amd64"), ".tar.gz")
        self.assertEqual(archive_suffix_for("forgejo-cli", VERSION, "arm64"), ".tar.gz")

    def test_the_member_is_the_flat_binary(self) -> None:
        self.assertEqual(archive_member("forgejo-cli", VERSION, "amd64"), "fj")
        self.assertEqual(archive_member("forgejo-cli", VERSION, "arm64"), "fj")
        self.assertEqual(binary_name("forgejo-cli"), "fj")

    def test_the_smoke_command_is_the_clis_own_version_subcommand(self) -> None:
        self.assertEqual(smoke_args("forgejo-cli"), ("version",))

    def test_both_architectures_depend_on_the_same_runtime(self) -> None:
        self.assertEqual(depends_for("forgejo-cli", "amd64"), DEPENDS)
        self.assertEqual(depends_for("forgejo-cli", "arm64"), DEPENDS)

    def test_the_license_is_the_reviewed_dual_license(self) -> None:
        self.assertEqual(expected_license_for("forgejo-cli"), "MIT OR Apache-2.0")
        self.assertEqual(license_path_for("forgejo-cli"), "LICENSE-MIT")
        self.assertEqual(extra_license_paths_for("forgejo-cli"), ("LICENSE-APACHE",))
        self.assertEqual(
            extra_license_urls_for("forgejo-cli", VERSION),
            (f"https://raw.githubusercontent.com/{REPO}/{TAG}/LICENSE-APACHE",),
        )

    def test_the_vendored_licenses_are_the_upstream_bytes(self) -> None:
        vendor = REPO_ROOT / "packaging" / "licenses" / "forgejo-cli"
        self.assertEqual(
            hashlib.sha256((vendor / "LICENSE.txt").read_bytes()).hexdigest(), MIT_SHA256
        )
        self.assertEqual(
            hashlib.sha256((vendor / "LICENSE-APACHE").read_bytes()).hexdigest(), APACHE_SHA256
        )

    def test_an_unreviewed_source_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported release source"):
            Recipe(
                name="x",
                upstream="x",
                repo="x/y",
                tag_prefix="v",
                license_id="MIT",
                license_path="LICENSE",
                asset="{upstream}.tar.gz",
                member="{binary}",
                binary="x",
                source="sourcehut",
            )

    def test_the_committed_manifest_carries_the_reviewed_entry(self) -> None:
        manifest = load_manifest()
        pkg = next(p for p in manifest.packages if p.name == "forgejo-cli")
        self.assertEqual(pkg.version, VERSION)
        self.assertEqual(pkg.homepage, f"https://github.com/{REPO}")
        self.assertEqual(pkg.description, "Forgejo CLI — cloudiful personal-use fork")
        self.assertEqual(pkg.license, "MIT OR Apache-2.0")
        self.assertEqual(
            pkg.license_url,
            f"https://raw.githubusercontent.com/{REPO}/{TAG}/LICENSE-MIT",
        )
        self.assertEqual(
            pkg.extra_license_urls,
            (f"https://raw.githubusercontent.com/{REPO}/{TAG}/LICENSE-APACHE",),
        )
        self.assertEqual(pkg.upstream_published_at, "2026-10-24T15:48:56Z")
        self.assertEqual(set(pkg.targets), {"amd64", "arm64"})
        amd64 = pkg.targets["amd64"]
        self.assertEqual(
            amd64.url,
            f"https://github.com/{REPO}/releases/download/{TAG}/{AMD64_ASSET}",
        )
        self.assertEqual(amd64.sha256, AMD64_SHA256)
        self.assertEqual((amd64.archive_member, amd64.binary), ("fj", "fj"))
        self.assertEqual(amd64.depends, DEPENDS)
        arm64 = pkg.targets["arm64"]
        self.assertEqual(
            arm64.url,
            f"https://github.com/{REPO}/releases/download/{TAG}/{ARM64_ASSET}",
        )
        self.assertEqual(arm64.sha256, ARM64_SHA256)
        self.assertEqual((arm64.archive_member, arm64.binary), ("fj", "fj"))
        self.assertEqual(arm64.depends, DEPENDS)


class ForgejoCliPackageTest(unittest.TestCase):
    def test_the_tarball_member_becomes_an_executable_package_with_the_notices(self) -> None:
        manifest = load_manifest()
        pkg = next(p for p in manifest.packages if p.name == "forgejo-cli")
        target = pkg.targets["amd64"]
        payload = b"\x7fELF-fake-fj"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = write_fake_archive(
                root / AMD64_ASSET, member=target.archive_member, payload=payload,
            )
            sources = extract_binaries(archive, target, root / "extract")
            self.assertEqual(set(sources), {"fj"})
            self.assertEqual(sources["fj"].read_bytes(), payload)
            stage = stage_package(
                pkg=pkg, arch="amd64", manifest=manifest,
                binary_file=sources["fj"], stage_root=root / "stages",
            )
            self.assertTrue((stage / "usr" / "bin" / "fj").exists())
            control = (stage / "DEBIAN" / "control").read_text()
            self.assertIn(f"Depends: {DEPENDS}", control)
            self.assertIn("Version: 0.7.0+cloudiful.2-1", control)
            self.assertIn("Description: Forgejo CLI — cloudiful personal-use fork", control)
            self.assertIn(f"Homepage: https://github.com/{REPO}", control)
            copyright = (stage / "usr" / "share" / "doc" / "forgejo-cli" / "copyright").read_text()
            self.assertIn("License: MIT OR Apache-2.0", copyright)
            self.assertIn("MIT License", copyright)
            self.assertIn("Apache License", copyright)
            deb = root / "forgejo-cli.deb"
            build_deb(stage, deb)
            listing = subprocess.run(
                ["dpkg-deb", "-c", str(deb)], text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(listing.returncode, 0, msg=listing.stderr)
            self.assertIn("./usr/bin/fj", listing.stdout)
            self.assertIn("./usr/share/doc/forgejo-cli/copyright", listing.stdout)


if __name__ == "__main__":
    unittest.main()
