import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.deb import build_deb, stage_package
from scripts.manifest import load_manifest


class DebTest(unittest.TestCase):
    def test_stage_and_build_layout(self) -> None:
        manifest = load_manifest()
        pkg = next(p for p in manifest.packages if p.name == "starship")
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            fake_binary = tmpdir / "starship"
            fake_binary.write_bytes(b"\x7fELF-fake-starship")
            fake_binary.chmod(0o755)
            stage = stage_package(
                pkg=pkg, arch="amd64", manifest=manifest,
                binary_file=fake_binary, stage_root=tmpdir / "stages",
            )
            # Staging never executes the binary; it only copies it.
            self.assertTrue((stage / "usr" / "bin" / "starship").exists())
            self.assertEqual((stage / "usr" / "bin" / "starship").read_bytes(), b"\x7fELF-fake-starship")
            control = (stage / "DEBIAN" / "control").read_text()
            self.assertIn("Package: starship", control)
            self.assertIn("Version: 1.26.0-1", control)
            self.assertIn("Architecture: amd64", control)
            self.assertNotIn("postinst", control)
            copyright_text = (stage / "usr" / "share" / "doc" / "starship" / "copyright").read_text()
            self.assertIn("Starship Contributors", copyright_text)
            self.assertIn("ISC", copyright_text)
            output = tmpdir / "pool" / "starship_1.26.0-1_amd64.deb"
            build_deb(stage, output)
            self.assertTrue(output.exists())
            shown = subprocess.run(
                ["dpkg-deb", "-f", str(output), "Package", "Version", "Architecture"],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(shown.returncode, 0)
            self.assertIn("starship", shown.stdout)
            self.assertIn("1.26.0-1", shown.stdout)
            contents = subprocess.run(
                ["dpkg-deb", "-c", str(output)],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertIn("./usr/bin/starship", contents.stdout)
            self.assertIn("./usr/share/doc/starship/copyright", contents.stdout)
            self.assertNotIn("postinst", contents.stdout)

    def test_zellij_copyright_is_mit(self) -> None:
        manifest = load_manifest()
        pkg = next(p for p in manifest.packages if p.name == "zellij")
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            fake_binary = tmpdir / "zellij"
            fake_binary.write_bytes(b"fake")
            stage = stage_package(
                pkg=pkg, arch="arm64", manifest=manifest,
                binary_file=fake_binary, stage_root=tmpdir / "stages",
            )
            copyright_text = (stage / "usr" / "share" / "doc" / "zellij" / "copyright").read_text()
            self.assertIn("Zellij contributors", copyright_text)
            self.assertIn("MIT", copyright_text)

    def test_deterministic_root_owned_and_byte_repeatable(self) -> None:
        import hashlib

        manifest = load_manifest()
        pkg = next(p for p in manifest.packages if p.name == "starship")
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            digests: list[str] = []
            for attempt in ("first", "second"):
                fake_binary = tmpdir / attempt / "starship"
                fake_binary.parent.mkdir(parents=True, exist_ok=True)
                fake_binary.write_bytes(b"\x7fELF-fake-starship")
                stage = stage_package(
                    pkg=pkg, arch="amd64", manifest=manifest,
                    binary_file=fake_binary, stage_root=tmpdir / attempt / "stages",
                )
                output = tmpdir / attempt / "starship_1.26.0-1_amd64.deb"
                build_deb(stage, output)
                digests.append(hashlib.sha256(output.read_bytes()).hexdigest())
                contents = subprocess.run(
                    ["dpkg-deb", "-c", str(output)],
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                )
                self.assertEqual(contents.returncode, 0)
                for line in contents.stdout.splitlines():
                    self.assertIn("root/root", line)
                self.assertIn("-rwxr-xr-x root/root", contents.stdout)
                self.assertIn("-rw-r--r-- root/root", contents.stdout)
            self.assertEqual(digests[0], digests[1])

    def test_rebuild_into_same_output_is_idempotent(self) -> None:
        from scripts.build import build_all

        manifest = load_manifest()
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            pool = tmpdir / "pool"
            cache = tmpdir / "upstream"
            cache.mkdir(parents=True, exist_ok=True)
            import tarfile
            import io

            for pkg in manifest.packages:
                for arch in ("amd64", "arm64"):
                    archive = cache / f"{pkg.name}-{pkg.version}-{arch}.tar.gz"
                    target = pkg.targets[arch]
                    with tarfile.open(archive, "w:gz") as tar:
                        data = f"fake-{pkg.name}-{arch}".encode()
                        info = tarfile.TarInfo(name=target.archive_member)
                        info.size = len(data)
                        info.mode = 0o755
                        info.mtime = 0
                        tar.addfile(info, io.BytesIO(data))
            from scripts import download as download_module
            import scripts.build as build_module

            original = build_module.fetch_verified

            def fake_fetch(url: str, sha: str, dest: Path) -> Path:
                return dest

            build_module.fetch_verified = fake_fetch  # type: ignore[assignment]
            try:
                first = build_all(manifest, pool_dir=pool, cache_dir=cache)
                second = build_all(manifest, pool_dir=pool, cache_dir=cache)
                self.assertEqual(first, second)
            finally:
                build_module.fetch_verified = original  # type: ignore[assignment]


if __name__ == "__main__":
    unittest.main()
