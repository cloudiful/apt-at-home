from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.manifest import load_manifest
from scripts.package_licenses import pi_source_document
from scripts.pi_bundle import (
    BUNDLE_TYPE,
    _REQUIRED_FILES,
    extract_bundle,
    inventory_bundle,
    launcher_bytes,
    native_helper_path,
    stage_pi_package,
    update_helper_bytes,
)
from tests.helpers import make_tree_archive

VERSION = "1.5.0"
_MACHINE = {"amd64": 62, "arm64": 183}


def fake_elf(arch: str) -> bytes:
    header = bytearray(64)
    header[:7] = b"\x7fELF\x02\x01\x01"
    header[16:18] = (2).to_bytes(2, "little")
    header[18:20] = _MACHINE[arch].to_bytes(2, "little")
    header[20:24] = (1).to_bytes(4, "little")
    header[52:54] = (64).to_bytes(2, "little")
    return bytes(header)


def package_json(version: str = VERSION, name: str = "@earendil-works/pi-coding-agent") -> bytes:
    return json.dumps({
        "name": name,
        "version": version,
        "bin": {"pi": "dist/bundle/cli.js"},
    }, separators=(",", ":")).encode()


def bundle_files(
    *, arch: str = "amd64", version: str = VERSION, manifest: bytes | None = None,
    payload: bytes | None = None,
) -> dict[str, tuple[bytes, int]]:
    return {
        "pi/pi": (fake_elf(arch) if payload is None else payload, 0o755),
        "pi/photon_rs_bg.wasm": (b"\0asm" + b"\0" * 8, 0o644),
        "pi/package.json": (manifest or package_json(version), 0o644),
        "pi/README.md": (b"# pi\n", 0o644),
        "pi/CHANGELOG.md": (b"# changelog\n", 0o644),
        "pi/theme/dark.json": (b"{}", 0o644),
        "pi/theme/light.json": (b"{}", 0o644),
        "pi/export-html/template.html": (b"<html></html>", 0o644),
        "pi/export-html/vendor/marked.min.js": (b"/* marked */", 0o644),
        "pi/assets/clankolas.png": (b"\x89PNG", 0o644),
        "pi/docs/cli.md": (b"# cli\n", 0o644),
        "pi/examples/demo/build.sh": (b"#!/bin/sh\n", 0o755),
        native_helper_path(arch): (fake_elf(arch), 0o755),
    }


def bundle_entries(
    *, arch: str = "amd64", version: str = VERSION, manifest: bytes | None = None,
    payload: bytes | None = None,
    extras: list[tuple[str, str, object, int]] | None = None,
) -> list[tuple[str, str, object, int]]:
    files = bundle_files(arch=arch, version=version, manifest=manifest, payload=payload)
    directories = {
        "/".join(name.split("/")[:index])
        for name in files
        for index in range(1, name.count("/") + 1)
    }
    entries: list[tuple[str, str, object, int]] = [
        ("dir", f"{name}/", None, 0o755) for name in sorted(directories)
    ]
    entries.extend(("file", name, body, mode) for name, (body, mode) in sorted(files.items()))
    entries.extend(extras or [])
    return entries


def write_bundle_archive(
    path: Path, *, arch: str = "amd64", version: str = VERSION,
    manifest: bytes | None = None, payload: bytes | None = None,
    extras: list[tuple[str, str, object, int]] | None = None,
) -> Path:
    return make_tree_archive(path, bundle_entries(
        arch=arch, version=version, manifest=manifest, payload=payload, extras=extras,
    ))


class PiBundleTest(unittest.TestCase):
    def test_inventory_and_extraction_keep_the_complete_reviewed_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = write_bundle_archive(root / "bundle.tar.gz")
            inventory = inventory_bundle(
                archive, expected_version="v1.5.0", expected_arch="amd64"
            )
            self.assertEqual(inventory.bundle_type, BUNDLE_TYPE)
            self.assertEqual(inventory.expected_version, "v1.5.0")
            self.assertEqual(inventory.expected_arch, "amd64")
            self.assertIn("pi/pi", {item.source for item in inventory.files})
            self.assertIn(
                "native/linux/prebuilds/linux-x64/linux-platform-x11.node",
                {item.relative for item in inventory.files},
            )
            self.assertIn("examples/demo/build.sh", {item.relative for item in inventory.files})
            extracted = root / "extracted"
            extract_bundle(archive, inventory, extracted)
            self.assertEqual(
                (extracted / "native/linux/prebuilds/linux-x64/linux-platform-x11.node").read_bytes(),
                fake_elf("amd64"),
            )
            self.assertTrue((extracted / "examples/demo/build.sh").stat().st_mode & 0o100)
            self.assertTrue((extracted / "pi").is_file())
            self.assertFalse((extracted / "pi/pi").exists())

    def test_package_stages_launcher_helper_resources_and_source_reference(self) -> None:
        manifest = load_manifest()
        package = next(pkg for pkg in manifest.packages if pkg.name == "pi")
        for arch in ("amd64", "arm64"):
            with self.subTest(arch=arch), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                archive = write_bundle_archive(root / "bundle.tar.gz", arch=arch)
                inventory = inventory_bundle(
                    archive, expected_version="v1.5.0", expected_arch=arch
                )
                stage = stage_pi_package(
                    pkg=package, arch=arch, manifest=manifest, archive=archive,
                    inventory=inventory, stage_root=root / "stages",
                )
                self.assertEqual((stage / "usr/bin/pi").read_bytes(), launcher_bytes())
                self.assertEqual(
                    (stage / "usr/lib/pi/apt-update").read_bytes(), update_helper_bytes()
                )
                self.assertEqual((stage / "usr/bin/pi").stat().st_mode & 0o777, 0o755)
                self.assertEqual(
                    (stage / "usr/lib/pi/apt-update").stat().st_mode & 0o777, 0o755
                )
                self.assertEqual((stage / "usr/lib/pi/pi").stat().st_mode & 0o777, 0o755)
                self.assertTrue((stage / "usr/lib/pi/package.json").is_file())
                self.assertTrue((stage / "usr/lib/pi/theme/dark.json").is_file())
                self.assertTrue((stage / "usr/lib/pi/docs/cli.md").is_file())
                self.assertTrue((stage / "usr/lib/pi/examples/demo/build.sh").stat().st_mode & 0o100)
                self.assertEqual(
                    (stage / "usr/share/doc/pi/README.source").read_text(encoding="utf-8"),
                    pi_source_document("1.5.0"),
                )
                copyright_text = (stage / "usr/share/doc/pi/copyright").read_text(encoding="utf-8")
                self.assertIn("MIT", copyright_text)

    def test_rejects_hostile_or_incomplete_bundles(self) -> None:
        cases = {
            "unexpected": [("file", "pi/other.txt", b"x", 0o644)],
            "traversal": [("file", "pi/../escape", b"x", 0o644)],
            "symlink": [("symlink", "pi/link", "pi/pi", 0o777)],
            "duplicate": [("file", "pi/README.md", b"again", 0o644)],
        }
        for label, extras in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                archive = write_bundle_archive(root / "bundle.tar.gz", extras=extras)
                with self.assertRaises(ValueError):
                    inventory_bundle(
                        archive, expected_version="v1.5.0", expected_arch="amd64"
                    )

    def test_rejects_wrong_arch_version_and_unsafe_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wrong_arch = write_bundle_archive(
                root / "arch.tar.gz", arch="amd64", payload=fake_elf("arm64")
            )
            with self.assertRaisesRegex(ValueError, "expected amd64"):
                inventory_bundle(wrong_arch, expected_version="v1.5.0", expected_arch="amd64")
            wrong_version = write_bundle_archive(
                root / "version.tar.gz", manifest=package_json("1.5.1")
            )
            with self.assertRaisesRegex(ValueError, "does not match"):
                inventory_bundle(
                    wrong_version, expected_version="v1.5.0", expected_arch="amd64"
                )
            unsafe_entries = bundle_entries()
            unsafe_entries = [
                (kind, name, body, 0o4755 if name == "pi/README.md" else mode)
                for kind, name, body, mode in unsafe_entries
            ]
            unsafe_mode = make_tree_archive(root / "mode.tar.gz", unsafe_entries)
            with self.assertRaisesRegex(ValueError, "dangerous"):
                inventory_bundle(unsafe_mode, expected_version="v1.5.0", expected_arch="amd64")
            executable_entries = bundle_entries()
            executable_entries = [
                (kind, name, body, 0o755 if name == "pi/docs/cli.md" else mode)
                for kind, name, body, mode in executable_entries
            ]
            executable_doc = make_tree_archive(root / "exec.tar.gz", executable_entries)
            with self.assertRaisesRegex(ValueError, "unexpected executable"):
                inventory_bundle(
                    executable_doc, expected_version="v1.5.0", expected_arch="amd64"
                )

    def _archive_without(self, path: Path, member: str) -> Path:
        files = bundle_files()
        files.pop(member)
        directories = {
            "/".join(name.split("/")[:index])
            for name in files
            for index in range(1, name.count("/") + 1)
        }
        entries: list[tuple[str, str, object, int]] = [
            ("dir", f"{name}/", None, 0o755) for name in sorted(directories)
        ]
        entries += [("file", name, body, mode) for name, (body, mode) in sorted(files.items())]
        return make_tree_archive(path, entries)

    def test_rejects_removal_of_each_required_file(self) -> None:
        """Every required file stays mandatory even while other resources remain."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for member in sorted(_REQUIRED_FILES):
                with self.subTest(member=member):
                    archive = self._archive_without(
                        root / f"missing-{member.replace('/', '-')}.tar.gz", member
                    )
                    with self.assertRaisesRegex(ValueError, "missing a required file"):
                        inventory_bundle(
                            archive, expected_version="v1.5.0", expected_arch="amd64"
                        )

    def test_rejects_a_missing_resource_or_native_helper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            without_docs = self._archive_without(root / "missing.tar.gz", "pi/docs/cli.md")
            with self.assertRaisesRegex(ValueError, "pi/docs"):
                inventory_bundle(
                    without_docs, expected_version="v1.5.0", expected_arch="amd64"
                )
            without_native = self._archive_without(
                root / "native.tar.gz", native_helper_path("amd64")
            )
            with self.assertRaisesRegex(ValueError, "native clipboard helper"):
                inventory_bundle(
                    without_native, expected_version="v1.5.0", expected_arch="amd64"
                )
            unexpected_native = write_bundle_archive(
                root / "other.tar.gz",
                extras=[("file", "pi/native/linux/prebuilds/linux-x64/other.node", b"x", 0o755)],
            )
            with self.assertRaises(ValueError):
                inventory_bundle(
                    unexpected_native, expected_version="v1.5.0", expected_arch="amd64"
                )


if __name__ == "__main__":
    unittest.main()
