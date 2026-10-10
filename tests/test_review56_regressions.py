"""Independent issue 56 acceptance regressions; no production changes."""

import tempfile
import unittest
from pathlib import Path

from scripts.archive_tree import inventory_tree
from scripts.deb import build_deb
from scripts.manifest import deb_filename
from scripts.smoke import SmokeError, check_pool
from tests.helpers import make_tree_archive, stage_fixture_target, valid_tree_entries
from tests.test_smoke import _catalog, fake_elf

TOP = "nvim-linux-x86_64"


class ReviewArchiveBoundaryTest(unittest.TestCase):
    def test_regular_file_cannot_be_the_archive_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            entries = valid_tree_entries(TOP)[1:]
            entries.insert(0, ("file", TOP, b"not a directory", 0o644))
            archive = make_tree_archive(Path(tmp) / "root.tar.gz", entries)
            with self.assertRaises(ValueError):
                inventory_tree(archive, TOP)

    def test_file_cannot_be_ancestor_of_an_explicit_empty_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            entries = valid_tree_entries(TOP, [
                ("file", f"{TOP}/share/collision", b"file", 0o644),
                ("dir", f"{TOP}/share/collision/empty/child", None, 0o755),
            ])
            archive = make_tree_archive(Path(tmp) / "collision.tar.gz", entries)
            with self.assertRaises(ValueError):
                inventory_tree(archive, TOP)


class ReviewNeovimStaticBoundaryTest(unittest.TestCase):
    def test_every_bundled_parser_must_exist_and_match_the_architecture(self):
        from scripts.catalog import parsers_for
        from scripts.smoke_tree import check_tree_payload

        manifest = _catalog(("neovim",))
        pkg = manifest.packages[0]
        for parser in parsers_for("neovim"):
            for mutation in ("absent", "wrong machine"):
                with self.subTest(parser=parser, mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    stage = stage_fixture_target(
                        root, pkg, "amd64", manifest, fake_elf("amd64"), root / "stages",
                    )
                    library = stage / "usr/lib/nvim/parser" / f"{parser}.so"
                    if mutation == "absent":
                        library.unlink()
                    else:
                        library.write_bytes(fake_elf("arm64"))
                    with self.assertRaises(SmokeError):
                        check_tree_payload(pkg, "amd64", stage / "usr")

    def test_static_pool_refuses_incomplete_or_incompatible_neovim(self):
        manifest = _catalog(("neovim",))
        pkg = manifest.packages[0]
        mutations = (
            "missing runtime", "missing parser", "wrong parser architecture",
            "missing notices", "missing Conflicts", "missing Replaces",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                stage = stage_fixture_target(
                    root, pkg, "amd64", manifest, fake_elf("amd64"), root / "stages",
                )
                if mutation == "missing runtime":
                    (stage / "usr/share/nvim/runtime/doc/help.txt").unlink()
                elif mutation == "missing parser":
                    (stage / "usr/lib/nvim/parser/lua.so").unlink()
                elif mutation == "wrong parser architecture":
                    (stage / "usr/lib/nvim/parser/lua.so").write_bytes(fake_elf("arm64"))
                elif mutation == "missing notices":
                    (stage / "usr/share/nvim/runtime/doc/uganda.txt").unlink()
                else:
                    field = mutation.removeprefix("missing ")
                    control = stage / "DEBIAN/control"
                    control.write_text("\n".join(
                        line for line in control.read_text().splitlines()
                        if not line.startswith(field + ":")
                    ) + "\n")
                pool = root / "pool"
                build_deb(stage, pool / deb_filename(pkg, "amd64", manifest))
                with self.assertRaises(SmokeError):
                    check_pool(manifest, pool, ("amd64",))
