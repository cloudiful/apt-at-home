"""Independent candidate review regressions against issue 64's boundaries."""

import json
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import archive_zip as archive
from scripts import bun_candidate_smoke as smoke
from scripts.bun_candidate_metadata import default_candidate_path, load_candidate


class CandidateReviewTest(unittest.TestCase):
    def test_static_only_never_runs_native_commands(self):
        with (
            patch.object(Path, "is_file", return_value=True),
            patch.object(Path, "is_symlink", return_value=False),
            patch.object(Path, "stat", return_value=SimpleNamespace(st_mode=0o755)),
            patch.object(smoke, "check_elf_arch"),
            patch.object(smoke, "native_arch", return_value="amd64"),
            patch.object(smoke, "native_checks", return_value=[]) as native,
        ):
            self.assertEqual(smoke.main([
                "--installed", "--arch", "amd64", "--static-only",
            ]), 0)
            native.assert_not_called()

    def test_native_smoke_uses_its_private_temporary_home(self):
        homes = []

        def run(command, cwd, home):
            homes.append(home)
            return "2-4-6" if "-e" in command else "1.4.3"

        with (
            patch.object(Path, "is_file", return_value=True),
            patch.object(Path, "is_symlink", autospec=True,
                         side_effect=lambda path: path.name == "bunx"),
            patch.object(Path, "stat", return_value=SimpleNamespace(st_mode=0o755)),
            patch.object(smoke, "check_elf_arch"),
            patch.object(smoke, "native_arch", return_value="amd64"),
            patch.object(smoke.os, "readlink", return_value="bun"),
            patch.object(smoke, "run_bounded", side_effect=run),
            patch.object(smoke.tempfile, "TemporaryDirectory") as temporary,
        ):
            temporary.return_value.__enter__.return_value = "/private/test-home"
            smoke.smoke(Path("/usr/bin"), "amd64", "1.4.3", require_native=True)
        self.assertEqual(homes, [Path("/private/test-home")] * 3)

    def test_source_contract_rejects_unreviewed_url_and_unknown_fields(self):
        raw = json.loads(default_candidate_path().read_text())
        raw["source"]["url"] = "https://example.invalid/unrelated"
        raw["source"]["patch_url"] = "https://example.invalid/not-a-patch"
        raw["source"]["unexpected"] = True
        raw["targets"]["amd64"]["unexpected"] = True
        with patch.object(Path, "read_text", return_value=json.dumps(raw)):
            with self.assertRaises(ValueError):
                load_candidate()

    def test_source_document_rejects_changed_source_and_patch_urls(self):
        descriptor = default_candidate_path()
        raw = json.loads(descriptor.read_text())
        original = (descriptor.parent / "README.source").read_text()
        changed = original.replace(
            raw["source"]["url"], "https://example.invalid/tree/bun-v1.4.3",
        ).replace(
            raw["source"]["patch_url"], "https://example.invalid/patches/tcc.h.patch",
        )
        self.assertNotEqual(changed, original)
        with patch.object(
            Path, "read_text", autospec=True,
            side_effect=lambda path, **kwargs: (
                json.dumps(raw) if path.suffix == ".json" else changed
            ),
        ):
            with self.assertRaises(ValueError):
                load_candidate()

    def test_explicit_shipped_descriptor_cannot_bypass_source_document_check(self):
        descriptor = default_candidate_path()
        raw = json.loads(descriptor.read_text())
        with patch.object(
            Path, "read_text", autospec=True,
            side_effect=lambda path, **kwargs: (
                json.dumps(raw) if path.suffix == ".json" else ""
            ),
        ):
            with self.assertRaises(ValueError):
                load_candidate(descriptor)

    def _inventory(self, names):
        infos = [zipfile.ZipInfo(name) for name in names]
        for info in infos:
            info.CRC = 0
        with (
            patch.object(Path, "stat", return_value=SimpleNamespace(st_size=1)),
            patch.object(archive.zipfile, "ZipFile") as zip_mock,
        ):
            zip_mock.return_value.__enter__.return_value.infolist.return_value = infos
            return archive.inventory(Path("unused.zip"))

    def test_zip_refuses_duplicate_directories(self):
        with self.assertRaises(ValueError):
            self._inventory(["root/", "root/", "root/bun"])

    def test_zip_refuses_noncanonical_directory_alias(self):
        with self.assertRaises(ValueError):
            self._inventory(["root//", "root/bun"])

    def test_zip_refuses_nul_in_original_member_name(self):
        # ZipInfo retains the original name while truncating filename at NUL.
        with self.assertRaises(ValueError):
            self._inventory(["root/bun\0evil"])


if __name__ == "__main__":
    unittest.main()
