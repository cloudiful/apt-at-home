"""End-to-end coverage for the candidate build, driven with fixture assets.

These tests build real `.deb` files through the real staging path, with the only
substitution being the upstream archive itself: a small ZIP carrying a fake
`bun` is dropped into the cache under the pinned name, so `fetch_verified`
re-verifies bytes it never had to download. Everything downstream of that — the
ZIP inventory, the alias, the notice files, the control file, `dpkg-deb` — is the
production code path.

The properties asserted are the ones the candidate actually promises: the exact
payload, the owned relative alias, the notices, and byte-identical rebuilds.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.bun_candidate import NOTICE_FILES, build, load_candidate, repo_root
from scripts.bun_candidate_metadata import Candidate
from scripts.common import sha256_file
from scripts.manifest import deb_filename

FAKE_BUN = b"\x7fELF fake bun payload\n"


def write_asset(
    dest: Path, root: str, payload: bytes = FAKE_BUN, extra_member: str | None = None
) -> Path:
    """Write an upstream-shaped release ZIP: one root directory, one binary."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(zipfile.ZipInfo(f"{root}/"), b"")
        zf.writestr(f"{root}/bun", payload)
        if extra_member is not None:
            zf.writestr(f"{root}/{extra_member}", b"unexpected")
    return dest


class CandidateBuildTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.base = json.loads(
            repo_root().joinpath("packaging/bun/candidate.json").read_text()
        )
        self.descriptor = self.root / "candidate.json"
        # The loader validates the source document beside the descriptor for any
        # descriptor path, so the fixture supplies the real one.
        shutil.copyfile(repo_root() / "packaging/bun/README.source",
                        self.root / "README.source")
        self.cache = self.root / "cache"
        self.seed_cache()

    def seed_cache(self, extra_member: str | None = None) -> Candidate:
        """Place the pinned assets in the cache with the fixture's own digests.

        The descriptor is rewritten to pin the fixture bytes, so `fetch_verified`
        re-verifies what it never downloaded and the rest of the build is the
        unmodified production path.
        """
        raw = json.loads(json.dumps(self.base))
        for arch, target in self.base["targets"].items():
            asset = target["url"].rsplit("/", 1)[-1]
            root = target["member"].rsplit("/", 1)[0]
            path = write_asset(self.cache / asset, root, extra_member=extra_member)
            raw["targets"][arch]["sha256"] = sha256_file(path)
            raw["targets"][arch]["size"] = path.stat().st_size
        self.descriptor.write_text(json.dumps(raw))
        self.candidate = load_candidate(self.descriptor)
        return self.candidate

    def real_document(self) -> str:
        return (repo_root() / "packaging" / "bun" / "README.source").read_text()

    def build_one(self, arch: str = "amd64", output: Path | None = None) -> Path:
        out = output or (self.root / "pool")
        debs = build(out, (arch,), self.candidate, repo_root(), self.cache)
        return debs[0]

    def payload(self, deb: Path) -> Path:
        target = self.root / f"extract-{deb.name}"
        subprocess.run(["dpkg-deb", "-x", str(deb), str(target)], check=True, capture_output=True)
        return target

    def control(self, deb: Path) -> str:
        out = subprocess.run(["dpkg-deb", "-I", str(deb)], check=True, capture_output=True, text=True)
        return out.stdout


class PayloadTest(CandidateBuildTest):
    def test_builds_the_expected_filename(self) -> None:
        deb = self.build_one()
        self.assertEqual(deb.name, deb_filename(self.candidate.package, "amd64", self.candidate.manifest))

    def test_installs_bun_as_a_root_owned_executable(self) -> None:
        payload = self.payload(self.build_one()) / "usr" / "bin" / "bun"
        self.assertTrue(payload.is_file())
        self.assertFalse(payload.is_symlink())
        self.assertEqual(payload.read_bytes(), FAKE_BUN)
        self.assertEqual(payload.stat().st_mode & 0o777, 0o755)

    def test_installs_bunx_as_an_owned_relative_alias(self) -> None:
        """The alias is created by us after validation, never taken from upstream."""
        bin_dir = self.payload(self.build_one()) / "usr" / "bin"
        alias = bin_dir / "bunx"
        self.assertTrue(alias.is_symlink())
        self.assertEqual(os.readlink(alias), "bun")
        self.assertEqual(alias.resolve(), (bin_dir / "bun").resolve())

    def test_alias_refused_when_the_payload_binary_is_absent(self) -> None:
        from scripts.bun_candidate import _install_alias

        bin_dir = self.root / "bare-stage" / "usr" / "bin"
        bin_dir.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "alias target bun is missing"):
            _install_alias(self.root / "bare-stage")

    def test_alias_refused_when_one_already_exists(self) -> None:
        from scripts.bun_candidate import _install_alias

        bin_dir = self.root / "stage" / "usr" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "bun").write_bytes(FAKE_BUN)
        (bin_dir / "bunx").symlink_to("bun")
        with self.assertRaisesRegex(ValueError, "already exists"):
            _install_alias(self.root / "stage")

    def test_installs_every_notice_file(self) -> None:
        doc = self.payload(self.build_one()) / "usr" / "share" / "doc" / "bun"
        for installed in (*NOTICE_FILES, "README.source", "copyright"):
            self.assertTrue((doc / installed).is_file(), installed)

    def test_upstream_license_is_installed_byte_exact(self) -> None:
        doc = self.payload(self.build_one()) / "usr" / "share" / "doc" / "bun"
        source = repo_root() / "packaging" / "licenses" / "bun" / "LICENSE.txt"
        self.assertEqual((doc / "LICENSE.upstream.md").read_bytes(), source.read_bytes())

    def test_gnu_license_texts_are_installed_in_full(self) -> None:
        doc = self.payload(self.build_one()) / "usr" / "share" / "doc" / "bun"
        self.assertIn("GNU LIBRARY GENERAL PUBLIC LICENSE",
                      (doc / "LGPL-2.0.txt").read_text())
        self.assertIn("GNU LESSER GENERAL PUBLIC LICENSE",
                      (doc / "LGPL-2.1.txt").read_text())


class ControlFileTest(CandidateBuildTest):
    def test_declares_the_pinned_dependency_and_architecture(self) -> None:
        control = self.control(self.build_one())
        self.assertIn("Package: bun\n", control)
        self.assertIn("Version: 1.4.3-1\n", control)
        self.assertIn("Architecture: amd64\n", control)
        self.assertIn("Depends: libc6 (>= 2.17)\n", control)

    def test_copyright_is_custom_and_carries_the_licence_bodies(self) -> None:
        doc = self.payload(self.build_one()) / "usr" / "share" / "doc" / "bun" / "copyright"
        text = doc.read_text()
        self.assertIn("License: Bun-upstream", text)
        self.assertNotIn("License: MIT\n", text)
        self.assertIn("GNU LIBRARY GENERAL PUBLIC LICENSE", text)
        self.assertIn("GNU LESSER GENERAL PUBLIC LICENSE", text)

    def test_readme_source_states_the_exact_pins(self) -> None:
        doc = self.payload(self.build_one()) / "usr" / "share" / "doc" / "bun" / "README.source"
        text = doc.read_text()
        self.assertIn("bun-v1.4.3", text)
        self.assertIn("c6da4a4d3010e5553438c60f6bd76d981976867c", text)
        self.assertIn("0c06faadf65bf8e8c8ad3a5a8aca83e1e9ed653f", text)
        self.assertIn("tcc.h.patch", text)


class RejectionTest(CandidateBuildTest):
    def test_build_stage_refuses_a_tampered_source_document(self) -> None:
        """The document is rechecked at the last point before it enters the package."""
        from scripts.bun_candidate import Paths, stage_candidate

        source = repo_root() / "packaging/bun/README.source"
        tampered = self.root / "repo"
        (tampered / "packaging" / "bun").mkdir(parents=True)
        (tampered / "packaging" / "licenses" / "bun").mkdir(parents=True)
        for name in ("LICENSE.txt", "LGPL-2.0.txt", "LGPL-2.1.txt"):
            shutil.copyfile(repo_root() / "packaging/licenses/bun" / name,
                            tampered / "packaging" / "licenses" / "bun" / name)
        document = self.real_document().replace(
            self.base["source"]["url"], "https://example.invalid/tree/bun-v1.4.3"
        )
        (tampered / "packaging" / "bun" / "README.source").write_text(
            document, encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "does not state the reviewed source url"):
            stage_candidate(self.candidate, "amd64", self.root / "work", self.cache,
                            Paths(tampered))

    def _mutate(self, mutate) -> Candidate:
        raw = json.loads(self.descriptor.read_text())
        mutate(raw)
        self.descriptor.write_text(json.dumps(raw))
        return load_candidate(self.descriptor)

    def test_wrong_archive_size_refused(self) -> None:
        broken = self._mutate(lambda raw: raw["targets"]["amd64"].__setitem__(
            "size", raw["targets"]["amd64"]["size"] + 1))
        with self.assertRaisesRegex(ValueError, "descriptor pins"):
            build(self.root / "pool", ("amd64",), broken, repo_root(), self.cache)

    def test_wrong_archive_digest_refused(self) -> None:
        broken = self._mutate(
            lambda raw: raw["targets"]["amd64"].__setitem__("sha256", "0" * 64))
        with self.assertRaisesRegex(ValueError, "sha256 mismatch"):
            build(self.root / "pool", ("amd64",), broken, repo_root(), self.cache)

    def test_archive_with_an_extra_regular_file_refused(self) -> None:
        """A tampered archive that still matches its pin is still refused by shape."""
        candidate = self.seed_cache(extra_member="extra")
        with self.assertRaisesRegex(ValueError, "unexpected files"):
            build(self.root / "pool", ("amd64",), candidate, repo_root(), self.cache)


class DeterminismTest(CandidateBuildTest):
    def test_repeated_build_is_byte_identical(self) -> None:
        first = self.build_one(output=self.root / "pool-a")
        second = self.build_one(output=self.root / "pool-b")
        self.assertEqual(first.name, second.name)
        self.assertEqual(sha256_file(first), sha256_file(second))

    def test_building_the_other_arch_does_not_disturb_the_first(self) -> None:
        amd64 = self.build_one("amd64", self.root / "pool-a")
        before = sha256_file(amd64)
        self.build_one("arm64", self.root / "pool-a")
        self.assertEqual(sha256_file(amd64), before)


class ProductionIsolationTest(unittest.TestCase):
    def test_candidate_output_cannot_reach_the_production_pool(self) -> None:
        """A candidate deb is not a production deb, by name and by location."""
        from scripts.manifest import load_manifest

        manifest = load_manifest()
        produced = {deb_filename(pkg, arch, manifest)
                    for pkg in manifest.packages for arch in ("amd64", "arm64")}
        self.assertNotIn("bun_1.4.3-1_amd64.deb", produced)

    def test_bun_candidate_module_is_not_imported_by_the_production_cli(self) -> None:
        source = (repo_root() / "scripts" / "repo.py").read_text(encoding="utf-8")
        self.assertNotIn("bun_candidate", source)


if __name__ == "__main__":
    unittest.main()