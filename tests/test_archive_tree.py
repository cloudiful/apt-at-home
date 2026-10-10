"""Tree archives: the top root is the only accepted install path, and every hostile
member refuses the whole archive before anything is written.
"""

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import archive_tree
from scripts.archive_tree import extract_tree, inventory_tree, top_root
from tests.helpers import make_tree_archive, valid_tree_entries

TOP = "nvim-linux-x86_64"


class TopRootTest(unittest.TestCase):
    def test_accepts_exactly_one_upstream_top_directory(self) -> None:
        self.assertEqual(top_root(TOP), TOP)
        self.assertEqual(top_root("./" + TOP), TOP)

    def test_refuses_subtrees_traversal_and_nesting(self) -> None:
        for bad in ("nvim-linux-x86_64/share", "share", "bin", "../escape",
                    "a/b", f"{TOP}/", "", "/abs"):
            with self.assertRaises(ValueError, msg=bad):
                top_root(bad)


class InventoryTest(unittest.TestCase):
    def test_installs_bin_lib_and_share_under_the_top_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = make_tree_archive(Path(tmp) / "a.tar.gz", valid_tree_entries(TOP))
            inventory = inventory_tree(archive, TOP)
            installed = {f.relative: f.executable for f in inventory.files}
            self.assertEqual(installed, {
                "bin/nvim": True,
                "lib/nvim/parser/lua.so": False,
                "share/nvim/runtime/doc/help.txt": False,
                "share/nvim/runtime/scripts/less.sh": True,
            })

    def test_refuses_a_file_outside_bin_lib_and_share(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = make_tree_archive(
                Path(tmp) / "a.tar.gz",
                valid_tree_entries(TOP, [("file", f"{TOP}/etc/evil.conf", b"x", 0o644)]),
            )
            with self.assertRaises(ValueError) as ctx:
                inventory_tree(archive, TOP)
            self.assertIn("not installable", str(ctx.exception))

    def test_every_hostile_member_refuses_the_wholemake_tree_archive(self) -> None:
        hostile = {
            "symlink": ("symlink", f"{TOP}/share/nvim/link", "/etc/passwd", 0),
            "hardlink": ("hardlink", f"{TOP}/share/nvim/hl", f"{TOP}/bin/nvim", 0),
            "fifo": ("fifo", f"{TOP}/share/nvim/pipe", None, 0),
            "setuid": ("file", f"{TOP}/bin/suid", b"x", 0o4755),
            "traversal": ("file", f"{TOP}/share/../../escape", b"x", 0o644),
            "absolute": ("file", "/etc/passwd", b"x", 0o644),
            "dot": ("file", f"{TOP}/./share/nvim/a", b"x", 0o644),
            "backslash": ("file", f"{TOP}\\share\\a", b"x", 0o644),
            "outside prefix": ("file", "other/share/nvim/a", b"x", 0o644),
            "hidden": ("file", f"{TOP}/share/nvim/.hidden", b"x", 0o644),
        }
        with tempfile.TemporaryDirectory() as tmp:
            for label, entry in hostile.items():
                with self.subTest(label):
                    archive = make_tree_archive(Path(tmp) / f"{label}.tar.gz", valid_tree_entries(TOP, [entry]))
                    with self.assertRaises(ValueError):
                        inventory_tree(archive, TOP)

    def test_a_file_colliding_with_a_directory_or_parent_is_refused(self) -> None:
        collisions = {
            "archive directory": ("file", f"{TOP}/share/nvim/runtime", b"x", 0o644),
            "nested under a file": ("file", f"{TOP}/share/nvim/runtime/doc/help.txt/child", b"x", 0o644),
            "ancestor of a file": ("file", f"{TOP}/lib/nvim", b"x", 0o644),
        }
        with tempfile.TemporaryDirectory() as tmp:
            for label, entry in collisions.items():
                with self.subTest(label):
                    archive = make_tree_archive(
                        Path(tmp) / f"{label.replace(' ', '_')}.tar.gz",
                        valid_tree_entries(TOP, [entry]),
                    )
                    with self.assertRaises(ValueError) as ctx:
                        inventory_tree(archive, TOP)
                    self.assertIn("both file and directory", str(ctx.exception))

    def test_duplicate_member_and_file_directory_conflict_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dup = make_tree_archive(
                Path(tmp) / "dup.tar.gz",
                valid_tree_entries(TOP, [("file", f"{TOP}/bin/nvim", b"again", 0o755)]),
            )
            with self.assertRaises(ValueError):
                inventory_tree(dup, TOP)
            clash = make_tree_archive(
                Path(tmp) / "clash.tar.gz",
                [
                    ("file", f"{TOP}/share/nvim/a", b"1", 0o644),
                    ("file", f"{TOP}/share/nvim/a/b", b"2", 0o644),
                ],
            )
            with self.assertRaises(ValueError) as ctx:
                inventory_tree(clash, TOP)
            self.assertIn("both file and directory", str(ctx.exception))

    def test_an_empty_tree_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = make_tree_archive(Path(tmp) / "empty.tar.gz", [("dir", f"{TOP}/", None, 0o755)])
            with self.assertRaises(ValueError) as ctx:
                inventory_tree(archive, TOP)
            self.assertIn("no regular files", str(ctx.exception))

    def test_the_reviewed_byte_ceilings_are_enforced(self) -> None:
        self.assertEqual(archive_tree.MAX_FILE_BYTES, 128 * 1024 * 1024)
        self.assertEqual(archive_tree.MAX_TOTAL_BYTES, 256 * 1024 * 1024)
        with tempfile.TemporaryDirectory() as tmp:
            archive = make_tree_archive(Path(tmp) / "big.tar.gz", valid_tree_entries(TOP))
            with mock.patch.object(archive_tree, "MAX_FILE_BYTES", 2):
                with self.assertRaises(ValueError) as ctx:
                    inventory_tree(archive, TOP)
            self.assertIn("byte limit", str(ctx.exception))
            with mock.patch.object(archive_tree, "MAX_TOTAL_BYTES", 2):
                with self.assertRaises(ValueError) as ctx:
                    inventory_tree(archive, TOP)
            self.assertIn("byte total limit", str(ctx.exception))

    def test_a_single_dot_prefix_is_canonical_and_its_alias_is_a_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            aliased = make_tree_archive(
                root / "alias.tar.gz",
                valid_tree_entries(TOP, [("file", f"./{TOP}/bin/extra", b"x", 0o755)]),
            )
            self.assertIn("bin/extra", {f.relative for f in inventory_tree(aliased, TOP).files})
            duplicate = make_tree_archive(
                root / "dup.tar.gz",
                valid_tree_entries(TOP, [("file", f"./{TOP}/bin/nvim", b"again", 0o755)]),
            )
            with self.assertRaises(ValueError) as ctx:
                inventory_tree(duplicate, TOP)
            self.assertIn("more than once", str(ctx.exception))
            doubled = make_tree_archive(
                root / "double.tar.gz",
                valid_tree_entries(TOP, [("file", f"././{TOP}/bin/extra", b"x", 0o755)]),
            )
            with self.assertRaises(ValueError):
                inventory_tree(doubled, TOP)

    def test_a_directory_outside_the_reviewed_roots_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = make_tree_archive(
                Path(tmp) / "dir.tar.gz",
                valid_tree_entries(TOP, [("dir", f"{TOP}/etc/", None, 0o755)]),
            )
            with self.assertRaises(ValueError) as ctx:
                inventory_tree(archive, TOP)
            self.assertIn("not installable", str(ctx.exception))

    def test_a_member_whose_bytes_disagree_with_its_declared_size_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError) as ctx:
                archive_tree._copy_bounded(io.BytesIO(b"nvim"), root / "short", 9, "nvim")
            self.assertIn("declared 9", str(ctx.exception))
            with self.assertRaises(ValueError) as ctx:
                archive_tree._copy_bounded(io.BytesIO(b"nvim"), root / "long", 2, "nvim")
            self.assertIn("more bytes than declared", str(ctx.exception))
            archive_tree._copy_bounded(io.BytesIO(b"nvim"), root / "exact", 4, "nvim")
            self.assertEqual((root / "exact").read_bytes(), b"nvim")

    def test_the_member_count_ceiling_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = make_tree_archive(Path(tmp) / "many.tar.gz", valid_tree_entries(TOP))
            with mock.patch.object(archive_tree, "MAX_MEMBERS", 2):
                with self.assertRaises(ValueError) as ctx:
                    inventory_tree(archive, TOP)
            self.assertIn("members", str(ctx.exception))


class ExtractTest(unittest.TestCase):
    def test_writes_only_validated_files_with_policy_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = make_tree_archive(root / "a.tar.gz", valid_tree_entries(TOP))
            inventory = inventory_tree(archive, TOP)
            usr = root / "usr"
            extract_tree(archive, inventory, usr)
            self.assertEqual(oct((usr / "bin/nvim").stat().st_mode & 0o777), "0o755")
            self.assertEqual(oct((usr / "lib/nvim/parser/lua.so").stat().st_mode & 0o777), "0o644")
            self.assertEqual(
                oct((usr / "share/nvim/runtime/scripts/less.sh").stat().st_mode & 0o777),
                "0o755",
            )
            written = sorted(p.relative_to(usr).as_posix() for p in usr.rglob("*") if p.is_file())
            self.assertEqual(written, [f.relative for f in sorted(inventory.files, key=lambda f: f.relative)])

    def test_an_existing_destination_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = make_tree_archive(root / "a.tar.gz", valid_tree_entries(TOP))
            inventory = inventory_tree(archive, TOP)
            (root / "usr").mkdir()
            with self.assertRaises(ValueError) as ctx:
                extract_tree(archive, inventory, root / "usr")
            self.assertIn("already exists", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
