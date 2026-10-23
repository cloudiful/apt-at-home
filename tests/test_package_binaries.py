import tempfile
import unittest
from pathlib import Path

from scripts.manifest import load_manifest
from scripts.package_binaries import extract_binaries, stage_binaries
from tests.helpers import write_fake_archive


class PackageBinariesTest(unittest.TestCase):
    def setUp(self) -> None:
        manifest = load_manifest()
        self.uv = next(pkg for pkg in manifest.packages if pkg.name == "uv")
        self.target = self.uv.targets["amd64"]

    def test_extracts_primary_and_exact_additional_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = write_fake_archive(
                root / "uv.tar.gz", member=self.target.archive_member, payload=b"uv",
                additional_members=tuple(self.target.additional_binaries),
            )
            sources = extract_binaries(archive, self.target, root / "extract")
            self.assertEqual(set(sources), {"uv", "uvx"})
            self.assertEqual(sources["uv"].read_bytes(), b"uv")
            self.assertEqual(sources["uvx"].read_bytes(), b"uv")

    def test_missing_additional_member_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = write_fake_archive(
                root / "uv.tar.gz", member=self.target.archive_member, payload=b"uv"
            )
            with self.assertRaisesRegex(ValueError, "member .* not found"):
                extract_binaries(archive, self.target, root / "extract")

    def test_staging_rejects_option_like_installed_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "uv"
            source.write_bytes(b"uv")
            with self.assertRaisesRegex(ValueError, "unsafe or duplicate"):
                stage_binaries(root / "usr" / "bin", "uv", source, {"-uvx": source})

    def test_a_reviewed_member_ceiling_reaches_the_extraction(self) -> None:
        """The recipe's own ceiling replaces the global one for its members."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = write_fake_archive(
                root / "uv.tar.gz", member=self.target.archive_member, payload=b"uv",
                additional_members=tuple(self.target.additional_binaries),
            )
            with self.assertRaisesRegex(ValueError, "limit"):
                extract_binaries(
                    archive, self.target, root / "extract", max_bytes=1
                )
            self.assertFalse((root / "extract").exists())


if __name__ == "__main__":
    unittest.main()
