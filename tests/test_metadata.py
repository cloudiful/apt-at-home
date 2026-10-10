import tempfile
import unittest
from pathlib import Path

from scripts.common import sha256_file
from scripts.deb import build_deb, stage_package
from scripts.manifest import deb_filename, load_manifest
from scripts.metadata import generate_release, write_indices
from scripts.reprepro_adapter import generate_packages


def _fields(stanza: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in stanza.splitlines():
        if ": " in line:
            key, value = line.split(": ", 1)
            fields[key.strip()] = value.strip()
    return fields


def _packages_names(text: str) -> set[str]:
    return {
        _fields(stanza)["Package"]
        for stanza in text.strip().split("\n\n")
        if _fields(stanza).get("Package")
    }


def _fake_debs(tmpdir: Path) -> tuple[list[Path], Path]:
    manifest = load_manifest()
    pool = tmpdir / "pool"
    pool.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    for pkg in manifest.packages:
        for arch in ("amd64", "arm64"):
            binary = tmpdir / f"src-{pkg.name}-{arch}" / pkg.targets[arch].binary
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_bytes(f"fake-{pkg.name}-{arch}".encode())
            binary.chmod(0o755)
            stage = stage_package(
                pkg=pkg, arch=arch, manifest=manifest,
                binary_file=binary, stage_root=tmpdir / "stages",
            )
            out = pool / deb_filename(pkg, arch, manifest)
            build_deb(stage, out)
            outputs.append(out)
    return outputs, pool


class MetadataTest(unittest.TestCase):
    def test_release_and_indices_from_reprepro_packages(self) -> None:
        manifest = load_manifest()
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _, pool = _fake_debs(tmpdir)
            output = tmpdir / "repo"
            release_id = "20260102-030405"
            packages_files: dict[str, list[Path]] = {}
            release_inputs: list[tuple[Path, str]] = []
            for arch in ("amd64", "arm64"):
                arch_debs = sorted(pool.glob(f"*_{arch}.deb"))
                self.assertEqual(len(arch_debs), len(manifest.packages))
                arch_dir = output / "releases" / release_id / "dists" / "stable" / "main" / f"binary-{arch}"
                packages, packages_gz = generate_packages(pool, arch_dir, arch_debs)
                self.assertEqual(_packages_names(packages.read_text()), {pkg.name for pkg in manifest.packages})
                for stanza in packages.read_text().strip().split("\n\n"):
                    fields = _fields(stanza)
                    self.assertEqual(fields["Architecture"], arch)
                    self.assertTrue(fields["Filename"].startswith("pool/"))
                    self.assertTrue((pool / fields["Filename"][len("pool/"):]).exists())
                self.assertEqual(
                    len(packages.read_text().strip().split("\n\n")), len(manifest.packages)
                )
                packages_files[arch] = [packages, packages_gz]
                release_inputs.append((packages, f"main/binary-{arch}/Packages"))
                release_inputs.append((packages_gz, f"main/binary-{arch}/Packages.gz"))
            release = generate_release(output_root=output, release_id=release_id, files=release_inputs)
            text = release.read_text()
            self.assertIn("Suite: stable", text)
            self.assertIn("Codename: stable", text)
            self.assertIn("Architectures: amd64 arm64", text)
            self.assertIn("Acquire-By-Hash: yes", text)
            self.assertNotIn("Valid-Until", text)
            written = write_indices(output_root=output, release_id=release_id, packages_files=packages_files)
            self.assertEqual(len(written), 4)
            for arch in ("amd64", "arm64"):
                served = output / "releases" / release_id / "dists" / "stable" / "main" / f"binary-{arch}" / "Packages"
                digest = sha256_file(served)
                self.assertTrue((output / "indices" / arch / "SHA256" / digest).exists())


if __name__ == "__main__":
    unittest.main()
