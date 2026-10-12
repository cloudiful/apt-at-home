from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.deb import build_deb
from scripts.kandev_bundle import (
    BUNDLE_TYPE,
    extract_bundle,
    inventory_bundle,
    parse_remote_helper_manifest,
    stage_kandev_package,
)
from scripts.manifest import load_manifest
from tests.helpers import make_tree_archive

COMMIT = "a" * 40
HELPERS = {
    "linux/amd64": "agentctl-linux-amd64.gz",
    "linux/arm64": "agentctl-linux-arm64.gz",
    "darwin/amd64": "agentctl-darwin-amd64.gz",
    "darwin/arm64": "agentctl-darwin-arm64.gz",
}
WRAPPER = b'#!/bin/sh\nexec /usr/lib/kandev/bin/kandev "$@"\n'
_MACHINE = {"amd64": 62, "arm64": 183}


def fake_elf(arch: str) -> bytes:
    header = bytearray(64)
    header[:7] = b"\x7fELF\x02\x01\x01"
    header[16:18] = (2).to_bytes(2, "little")
    header[18:20] = _MACHINE[arch].to_bytes(2, "little")
    header[20:24] = (1).to_bytes(4, "little")
    header[52:54] = (64).to_bytes(2, "little")
    return bytes(header)


def helper_manifest(version: str = "v0.97.0") -> bytes:
    return json.dumps({
        "schema_version": 1,
        "version": version,
        "commit": COMMIT,
        "variant": "standard",
        "helpers": [
            {
                "platform": platform,
                "asset": asset,
                "sha256": f"{index:064x}",
                "size_bytes": 128 + index,
            }
            for index, (platform, asset) in enumerate(HELPERS.items(), start=1)
        ],
    }, separators=(",", ":")).encode()


def bundle_entries(
    *, arch: str = "amd64", manifest: bytes | None = None, payload: bytes | None = None,
    extras: list[tuple[str, str, object, int]] | None = None,
) -> list[tuple[str, str, object, int]]:
    payload = fake_elf(arch) if payload is None else payload
    return [
        ("dir", "kandev/", None, 0o755),
        ("dir", "kandev/bin/", None, 0o755),
        ("file", "kandev/bin/kandev", payload, 0o755),
        ("file", "kandev/bin/agentctl", payload, 0o755),
        ("file", "kandev/remote-helpers.json", manifest or helper_manifest(), 0o644),
        *(extras or []),
    ]


def write_bundle_archive(
    path: Path, *, version: str = "v0.97.0", arch: str = "amd64",
    payload: bytes | None = None,
) -> Path:
    return make_tree_archive(path, bundle_entries(
        arch=arch, manifest=helper_manifest(version), payload=payload,
    ))


class KandevBundleTest(unittest.TestCase):
    def test_inventory_and_extraction_keep_the_closed_standard_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = write_bundle_archive(root / "bundle.tar.gz")
            inventory = inventory_bundle(
                archive, expected_version="v0.97.0", expected_arch="amd64"
            )
            self.assertEqual(inventory.bundle_type, BUNDLE_TYPE)
            self.assertEqual(inventory.commit, COMMIT)
            self.assertEqual(
                {item.relative for item in inventory.files},
                {"bin/kandev", "bin/agentctl", "remote-helpers.json"},
            )
            extracted = root / "extracted"
            extract_bundle(archive, inventory, extracted)
            self.assertEqual((extracted / "bin/kandev").read_bytes(), fake_elf("amd64"))
            self.assertEqual((extracted / "remote-helpers.json").read_bytes(), inventory.manifest_bytes)
            self.assertFalse((extracted / "kandev").exists())

    def test_package_stages_the_wrapper_bundle_dependencies_and_source_reference(self) -> None:
        manifest = load_manifest()
        package = next(pkg for pkg in manifest.packages if pkg.name == "kandev")
        for arch in ("amd64", "arm64"):
            with self.subTest(arch=arch), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                archive = write_bundle_archive(root / "bundle.tar.gz", arch=arch)
                inventory = inventory_bundle(
                    archive, expected_version="v0.97.0", expected_arch=arch
                )
                stage = stage_kandev_package(
                    pkg=package, arch=arch, manifest=manifest, archive=archive,
                    inventory=inventory, stage_root=root / "stages",
                )
                self.assertEqual((stage / "usr/bin/kandev").read_bytes(), WRAPPER)
                self.assertTrue((stage / "usr/lib/kandev/bin/kandev").is_file())
                self.assertTrue((stage / "usr/lib/kandev/bin/agentctl").is_file())
                self.assertTrue((stage / "usr/lib/kandev/remote-helpers.json").is_file())
                self.assertFalse((stage / "usr/bin/agentctl").exists())
                self.assertIn("Depends: libc6 (>= 2.34)", (stage / "DEBIAN/control").read_text())
                copyright = (stage / "usr/share/doc/kandev/copyright").read_text()
                self.assertIn("License: AGPL-3.0-or-later", copyright)
                source = (stage / "usr/share/doc/kandev/README.source").read_text()
                self.assertIn("/tree/v0.97.0", source)
                self.assertIn(COMMIT, source)
                self.assertEqual((stage / "usr/bin/kandev").stat().st_mode & 0o777, 0o755)
                self.assertEqual(
                    (stage / "usr/lib/kandev/remote-helpers.json").stat().st_mode & 0o777,
                    0o644,
                )
                package_file = root / f"kandev_{package.version}-1_{arch}.deb"
                build_deb(stage, package_file)
                contents = subprocess.run(
                    ["dpkg-deb", "-c", str(package_file)], check=False,
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                self.assertEqual(contents.returncode, 0, msg=contents.stderr)
                self.assertIn("./usr/lib/kandev/bin/agentctl", contents.stdout)
                self.assertIn("./usr/share/doc/kandev/copyright", contents.stdout)
                self.assertNotIn("./usr/bin/agentctl", contents.stdout)

    def test_unreviewed_members_and_links_are_rejected(self) -> None:
        hostile = (
            [("file", "kandev/extra", b"extra", 0o644)],
            [("file", "../escape", b"escape", 0o644)],
            [("symlink", "kandev/bin/agentctl", "kandev/bin/kandev", 0o777)],
            [("fifo", "kandev/bin/agentctl", None, 0o644)],
        )
        for extra in hostile:
            with self.subTest(member=extra[0][1]), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                archive = make_tree_archive(root / "bad.tar.gz", bundle_entries(extras=extra))
                with self.assertRaises(ValueError):
                    inventory_bundle(
                        archive, expected_version="v0.97.0", expected_arch="amd64"
                    )

    def test_payload_modes_must_be_safe_and_match_the_reviewed_role(self) -> None:
        for mode, error in ((0o775, "dangerous"), (0o644, "unexpected executable mode")):
            with self.subTest(mode=oct(mode)), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                entries = bundle_entries()
                entries[2] = ("file", "kandev/bin/kandev", fake_elf("amd64"), mode)
                archive = make_tree_archive(root / "bad-mode.tar.gz", entries)
                with self.assertRaisesRegex(ValueError, error):
                    inventory_bundle(
                        archive, expected_version="v0.97.0", expected_arch="amd64"
                    )

    def test_helper_manifest_requires_exact_version_variant_platforms_and_unique_keys(self) -> None:
        with self.assertRaisesRegex(ValueError, "pinned standard release"):
            parse_remote_helper_manifest(helper_manifest("v0.97.1"), "v0.97.0")
        changed = json.loads(helper_manifest())
        changed["variant"] = "desktop"
        with self.assertRaisesRegex(ValueError, "pinned standard release"):
            parse_remote_helper_manifest(json.dumps(changed).encode(), "v0.97.0")
        changed = json.loads(helper_manifest())
        changed["helpers"].pop()
        with self.assertRaisesRegex(ValueError, "exactly the reviewed"):
            parse_remote_helper_manifest(json.dumps(changed).encode(), "v0.97.0")
        changed = json.loads(helper_manifest())
        changed["helpers"][0]["sha256"] = "not-a-sha256"
        with self.assertRaisesRegex(ValueError, "invalid asset metadata"):
            parse_remote_helper_manifest(json.dumps(changed).encode(), "v0.97.0")
        duplicate = helper_manifest().replace(
            b'"schema_version":1,', b'"schema_version":1,"schema_version":1,'
        )
        with self.assertRaisesRegex(ValueError, "repeats key"):
            parse_remote_helper_manifest(duplicate, "v0.97.0")

    def test_rejects_wrong_release_and_changed_archive_before_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = write_bundle_archive(root / "bundle.tar.gz")
            inventory = inventory_bundle(
                archive, expected_version="v0.97.0", expected_arch="amd64"
            )
            destination = root / "extracted"
            with self.assertRaisesRegex(ValueError, "pinned standard release"):
                inventory_bundle(
                    archive, expected_version="v0.98.0", expected_arch="amd64"
                )
            write_bundle_archive(
                archive, payload=fake_elf("amd64") + b"changed after inventory"
            )
            with self.assertRaisesRegex(ValueError, "changed after its bundle inventory"):
                extract_bundle(archive, inventory, destination)
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
