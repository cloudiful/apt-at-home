"""Independent acceptance regressions for the closed Kandev bundle."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.build import _stage_target
from scripts.kandev_bundle import inventory_bundle, stage_kandev_package
from scripts.manifest import load_manifest
from tests.helpers import make_tree_archive
from tests.test_kandev_bundle import bundle_entries, fake_elf


class KandevReviewTest(unittest.TestCase):
    def test_foreign_architecture_is_rejected_before_staging_writes(self) -> None:
        manifest = load_manifest()
        pkg = next(p for p in manifest.packages if p.name == "kandev")
        # ELF64 little-endian AArch64 executable header, not runnable host code.
        foreign_elf = fake_elf("arm64")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = make_tree_archive(
                root / "foreign.tar.gz", bundle_entries(payload=foreign_elf)
            )
            with self.assertRaises(ValueError):
                inventory = inventory_bundle(
                    archive, expected_version="v0.97.0", expected_arch="amd64"
                )
                stage_kandev_package(
                    pkg=pkg, arch="amd64", manifest=manifest, archive=archive,
                    inventory=inventory, stage_root=root / "stages",
                )
            self.assertFalse((root / "stages").exists())

    def test_duplicate_and_alias_members_are_rejected(self) -> None:
        for name in ("kandev/bin/kandev", "kandev/bin/./kandev", "/kandev/bin/kandev",
                     "kandev\\bin\\kandev", "kandev/bin/../bin/kandev"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                archive = make_tree_archive(
                    Path(tmp) / "bad.tar.gz",
                    bundle_entries(extras=[("file", name, b"extra", 0o755)]),
                )
                with self.assertRaises(ValueError):
                    inventory_bundle(
                        archive, expected_version="v0.97.0", expected_arch="amd64"
                    )

    def test_both_bundle_elfs_require_complete_target_arch_elf64_headers_prewrite(self) -> None:
        manifest = load_manifest()
        pkg = next(p for p in manifest.packages if p.name == "kandev")
        for arch in ("amd64", "arm64"):
            foreign_arch = "arm64" if arch == "amd64" else "amd64"
            wrong_class = bytearray(fake_elf(arch))
            wrong_class[4] = 1
            wrong_endian = bytearray(fake_elf(arch))
            wrong_endian[5] = 2
            invalid_headers = {
                "foreign architecture": fake_elf(foreign_arch),
                "ELF32": bytes(wrong_class),
                "big endian": bytes(wrong_endian),
                "truncated": fake_elf(arch)[:63],
                "non-ELF": b"not an ELF header".ljust(64, b"\0"),
            }
            for binary, index in (("kandev", 2), ("agentctl", 3)):
                for label, payload in invalid_headers.items():
                    with self.subTest(arch=arch, binary=binary, header=label):
                        with tempfile.TemporaryDirectory() as tmp:
                            root = Path(tmp)
                            entries = bundle_entries(arch=arch)
                            kind, name, _, mode = entries[index]
                            entries[index] = (kind, name, payload, mode)
                            archive = make_tree_archive(root / "invalid-elf.tar.gz", entries)
                            stage_root = root / "stages"
                            with self.assertRaises(ValueError):
                                inventory_bundle(
                                    archive, expected_version="v0.97.0", expected_arch=arch
                                )
                            self.assertFalse(stage_root.exists())

    def test_builder_checks_each_elf_before_creating_or_replacing_stage(self) -> None:
        manifest = load_manifest()
        pkg = next(p for p in manifest.packages if p.name == "kandev")
        for arch in ("amd64", "arm64"):
            foreign = "arm64" if arch == "amd64" else "amd64"
            for index in (2, 3):
                for existing in (False, True):
                    with self.subTest(arch=arch, index=index, existing=existing):
                        with tempfile.TemporaryDirectory() as tmp:
                            root = Path(tmp)
                            entries = bundle_entries(arch=arch)
                            kind, name, _, mode = entries[index]
                            entries[index] = (kind, name, fake_elf(foreign), mode)
                            archive = make_tree_archive(root / "foreign.tar.gz", entries)
                            stage = root / "stages" / f"kandev_{arch}"
                            if existing:
                                stage.mkdir(parents=True)
                                (stage / "sentinel").write_bytes(b"unchanged")
                            with self.assertRaisesRegex(ValueError, "e_machine"):
                                _stage_target(pkg, arch, manifest, archive, root)
                            if existing:
                                self.assertEqual(list(stage.iterdir()), [stage / "sentinel"])
                                self.assertEqual((stage / "sentinel").read_bytes(), b"unchanged")
                            else:
                                self.assertFalse((root / "stages").exists())

    def test_inventory_arch_mismatch_is_rejected_before_stage_replacement(self) -> None:
        manifest = load_manifest()
        pkg = next(p for p in manifest.packages if p.name == "kandev")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = make_tree_archive(root / "arm.tar.gz", bundle_entries(arch="arm64"))
            inventory = inventory_bundle(
                archive, expected_version="v0.97.0", expected_arch="arm64"
            )
            stage = root / "stages" / "kandev_amd64"
            stage.mkdir(parents=True)
            (stage / "sentinel").write_bytes(b"unchanged")
            with self.assertRaisesRegex(ValueError, "does not match"):
                stage_kandev_package(
                    pkg=pkg, arch="amd64", manifest=manifest, archive=archive,
                    inventory=inventory, stage_root=root / "stages",
                )
            self.assertEqual(list(stage.iterdir()), [stage / "sentinel"])
            self.assertEqual((stage / "sentinel").read_bytes(), b"unchanged")


if __name__ == "__main__":
    unittest.main()
