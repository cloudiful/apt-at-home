"""A tree package installs its validated roots and refuses a layout missing a required path."""

import tempfile
import unittest
from pathlib import Path

from scripts.archive_tree import inventory_tree
from scripts.catalog import required_paths_for
from scripts.deb import stage_package
from scripts.manifest import load_manifest
from scripts.package_tree import install_tree
from tests.helpers import make_tree_archive, valid_tree_entries

TOP = "nvim-linux-x86_64"
REQUIRED = ("bin/nvim", "lib/nvim/parser/lua.so", "share/nvim/runtime/doc/help.txt")


class InstallTreeTest(unittest.TestCase):
    def test_installs_every_validated_file_and_reports_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = make_tree_archive(root / "a.tar.gz", valid_tree_entries(TOP))
            inventory = inventory_tree(archive, TOP)
            installed = install_tree(archive, inventory, root / "usr", required=REQUIRED)
            self.assertEqual(
                list(installed),
                sorted(f.relative for f in inventory.files),
            )
            self.assertTrue((root / "usr" / "bin" / "nvim").is_file())

    def test_a_missing_required_path_fails_the_build(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = make_tree_archive(root / "a.tar.gz", [
                ("file", f"{TOP}/bin/nvim", b"nvim", 0o755),
                ("file", f"{TOP}/share/nvim/runtime/doc/help.txt", b"help", 0o644),
            ])
            inventory = inventory_tree(archive, TOP)
            with self.assertRaises(ValueError) as ctx:
                install_tree(archive, inventory, root / "usr", required=REQUIRED)
            self.assertIn("lib/nvim/parser/lua.so", str(ctx.exception))


class StageMutualExclusionTest(unittest.TestCase):
    def test_a_tree_target_refuses_a_flat_binary_and_a_flat_target_refuses_a_tree(self) -> None:
        manifest = load_manifest()
        neovim = next(p for p in manifest.packages if p.name == "neovim")
        zellij = next(p for p in manifest.packages if p.name == "zellij")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root / "nvim"
            binary.write_bytes(b"fake")
            archive = make_tree_archive(root / "tree.tar.gz", valid_tree_entries(TOP))
            inventory = inventory_tree(archive, TOP)
            with self.assertRaises(ValueError) as ctx:
                stage_package(pkg=neovim, arch="amd64", manifest=manifest,
                              binary_file=binary, stage_root=root / "s1")
            self.assertIn("install_tree", str(ctx.exception))
            with self.assertRaises(ValueError) as ctx:
                stage_package(pkg=zellij, arch="amd64", manifest=manifest,
                              tree=(archive, inventory, required_paths_for("zellij")),
                              stage_root=root / "s2")
            self.assertIn("archive_member", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
