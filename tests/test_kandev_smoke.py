from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from scripts.kandev_smoke import check_bundle_payload, check_version_output
from scripts.kandev_smoke import SmokeError
from scripts.kandev_bundle import parse_remote_helper_manifest
from scripts.manifest import load_manifest
from scripts.package_licenses import kandev_source_document
from tests.test_kandev_bundle import COMMIT, WRAPPER, helper_manifest

_MACHINE = {"amd64": 62, "arm64": 183}


def fake_elf(arch: str) -> bytes:
    identity = b"\x7fELF" + bytes((2, 1, 1, 0)) + b"\x00" * 8
    return identity + struct.pack("<HHI", 2, _MACHINE[arch], 1) + b"\x00" * 32


def installed_bundle(root: Path, arch: str) -> None:
    wrapper = root / "usr/bin/kandev"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_bytes(WRAPPER)
    wrapper.chmod(0o755)
    bundle = root / "usr/lib/kandev"
    (bundle / "bin").mkdir(parents=True)
    for name in ("kandev", "agentctl"):
        binary = bundle / "bin" / name
        binary.write_bytes(fake_elf(arch))
        binary.chmod(0o755)
    manifest = helper_manifest()
    (bundle / "remote-helpers.json").write_bytes(manifest)
    (bundle / "remote-helpers.json").chmod(0o644)
    remote_commit = parse_remote_helper_manifest(manifest, "v0.97.0")
    source = root / "usr/share/doc/kandev/README.source"
    source.parent.mkdir(parents=True)
    source.write_text(kandev_source_document("0.97.0", remote_commit), encoding="utf-8")
    source.chmod(0o644)


class KandevSmokeTest(unittest.TestCase):
    def test_bundle_payload_proves_architecture_wrapper_and_pinned_source_reference(self) -> None:
        package = next(pkg for pkg in load_manifest().packages if pkg.name == "kandev")
        for arch in ("amd64", "arm64"):
            with self.subTest(arch=arch), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                installed_bundle(root, arch)
                findings = check_bundle_payload(package, arch, root)
                self.assertEqual(len(findings), 2)
                self.assertIn("wrapper", findings[0])
                self.assertIn("ELF", findings[1])

    def test_rejects_wrong_architecture_and_unowned_launcher_content(self) -> None:
        package = next(pkg for pkg in load_manifest().packages if pkg.name == "kandev")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            installed_bundle(root, "arm64")
            with self.assertRaisesRegex(SmokeError, "expected x86-64"):
                check_bundle_payload(package, "amd64", root)
            (root / "usr/bin/kandev").write_text("#!/bin/sh\necho unsafe\n")
            (root / "usr/bin/kandev").chmod(0o755)
            with self.assertRaisesRegex(SmokeError, "quoted arguments"):
                check_bundle_payload(package, "arm64", root)

    def test_source_reference_and_manifest_are_checked_before_version_output(self) -> None:
        package = next(pkg for pkg in load_manifest().packages if pkg.name == "kandev")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            installed_bundle(root, "amd64")
            (root / "usr/share/doc/kandev/README.source").write_text("stale", encoding="utf-8")
            with self.assertRaisesRegex(SmokeError, "README.source"):
                check_bundle_payload(package, "amd64", root)
        with self.assertRaisesRegex(SmokeError, "expected 'v0.97.0'"):
            check_version_output(package, "v0.97.1")
        check_version_output(package, "v0.97.0\n")


if __name__ == "__main__":
    unittest.main()
