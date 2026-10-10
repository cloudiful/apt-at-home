import gzip
import json
import os
import tempfile
import unittest
from pathlib import Path

from scripts.common import sha256_file
from scripts.deb import build_deb, stage_package
from scripts.manifest import deb_filename, load_manifest
from scripts.metadata import generate_packages, generate_release, write_indices
from scripts.sign import export_public_key, import_key, sign_release
from scripts.verify import verify_tree
from tests.helpers import make_gpg_home_with_key


def resign_tree(root: Path, release_id: str, *, key_home: Path, key_id: str) -> None:
    """Rebuild gzip, Release, by-hash indices and signatures after editing Packages.

    Keeps every hash and signature self-consistent so a targeted defect surfaces
    through verify_tree as an integrity failure rather than a hash mismatch.
    """
    dists = root / "releases" / release_id / "dists" / "stable"
    release_inputs: list[tuple[Path, str]] = []
    packages_files: dict[str, list[Path]] = {}
    for arch in ("amd64", "arm64"):
        arch_dir = dists / "main" / f"binary-{arch}"
        packages = arch_dir / "Packages"
        packages_gz = arch_dir / "Packages.gz"
        with packages_gz.open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", compresslevel=9, mtime=0, fileobj=raw) as gz:
                gz.write(packages.read_bytes())
        packages_files[arch] = [packages, packages_gz]
        release_inputs.append((packages, f"main/binary-{arch}/Packages"))
        release_inputs.append((packages_gz, f"main/binary-{arch}/Packages.gz"))
    release_file = generate_release(output_root=root, release_id=release_id, files=release_inputs)
    write_indices(output_root=root, release_id=release_id, packages_files=packages_files)
    old = os.environ.get("GNUPGHOME")
    os.environ["GNUPGHOME"] = str(key_home)
    try:
        sign_release(release_file=release_file, key_id=key_id)
        export_public_key(homedir=key_home, key_id=key_id, dest=root / "key.asc")
    finally:
        if old is None:
            os.environ.pop("GNUPGHOME", None)
        else:
            os.environ["GNUPGHOME"] = old


def build_complete_tree(root: Path, release_id: str, *, key_home: Path, key_id: str) -> Path:
    manifest = load_manifest()
    pool = root / "pool"
    pool.mkdir(parents=True, exist_ok=True)
    for pkg in manifest.packages:
        for arch in ("amd64", "arm64"):
            binary = root / "src" / f"{pkg.name}-{arch}" / pkg.targets[arch].binary
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_bytes(f"fake-{pkg.name}-{arch}".encode())
            binary.chmod(0o755)
            stage = stage_package(
                pkg=pkg, arch=arch, manifest=manifest,
                binary_file=binary, stage_root=root / "stages",
            )
            build_deb(stage, pool / deb_filename(pkg, arch, manifest))
    packages_files: dict[str, list[Path]] = {}
    release_inputs: list[tuple[Path, str]] = []
    for arch in ("amd64", "arm64"):
        arch_debs = sorted(pool.glob(f"*_{arch}.deb"))
        arch_dir = root / "releases" / release_id / "dists" / "stable" / "main" / f"binary-{arch}"
        packages, packages_gz = generate_packages(pool, arch_dir, arch_debs)
        packages_files[arch] = [packages, packages_gz]
        release_inputs.append((packages, f"main/binary-{arch}/Packages"))
        release_inputs.append((packages_gz, f"main/binary-{arch}/Packages.gz"))
    release_file = generate_release(output_root=root, release_id=release_id, files=release_inputs)
    write_indices(output_root=root, release_id=release_id, packages_files=packages_files)
    old = os.environ.get("GNUPGHOME")
    os.environ["GNUPGHOME"] = str(key_home)
    try:
        sign_release(release_file=release_file, key_id=key_id)
        export_public_key(homedir=key_home, key_id=key_id, dest=root / "key.asc")
    finally:
        if old is None:
            os.environ.pop("GNUPGHOME", None)
        else:
            os.environ["GNUPGHOME"] = old
    (root / "current.json").write_text(json.dumps({"release": release_id}) + "\n", encoding="utf-8")
    return root


class VerifyTest(unittest.TestCase):
    def test_complete_tree_verifies(self) -> None:
        tmpdir, home, key_id = make_gpg_home_with_key()
        try:
            with tempfile.TemporaryDirectory() as work:
                root = build_complete_tree(Path(work) / "repo", "20260102-030405",
                                           key_home=home, key_id=key_id)
                findings = verify_tree(root)
                self.assertTrue(any("signatures valid" in f for f in findings))
                pinned = verify_tree(root, expected_fingerprint=key_id)
                self.assertTrue(any("trusted primary" in f for f in pinned))
        finally:
            tmpdir.cleanup()

    def test_wrong_and_malformed_pin_fail(self) -> None:
        tmpdir, home, key_id = make_gpg_home_with_key()
        other_tmp, other_home, other_id = make_gpg_home_with_key()
        try:
            with tempfile.TemporaryDirectory() as work:
                root = build_complete_tree(Path(work) / "repo", "20260102-030405",
                                           key_home=home, key_id=key_id)
                with self.assertRaises(ValueError):
                    verify_tree(root, expected_fingerprint=other_id)
                with self.assertRaises(ValueError):
                    verify_tree(root, expected_fingerprint="ABCD1234")
        finally:
            tmpdir.cleanup()
            other_tmp.cleanup()

    def test_missing_pool_file_fails(self) -> None:
        tmpdir, home, key_id = make_gpg_home_with_key()
        try:
            with tempfile.TemporaryDirectory() as work:
                root = build_complete_tree(Path(work) / "repo", "20260102-030405",
                                           key_home=home, key_id=key_id)
                next((root / "pool").glob("*.deb")).unlink()
                with self.assertRaises(ValueError):
                    verify_tree(root)
        finally:
            tmpdir.cleanup()

    def test_tampered_packages_fails(self) -> None:
        tmpdir, home, key_id = make_gpg_home_with_key()
        try:
            with tempfile.TemporaryDirectory() as work:
                root = build_complete_tree(Path(work) / "repo", "20260102-030405",
                                           key_home=home, key_id=key_id)
                target = root / "releases" / "20260102-030405" / "dists" / "stable" / "main" / "binary-amd64" / "Packages"
                with target.open("a", encoding="utf-8") as handle:
                    handle.write("Tampered: yes\n")
                with self.assertRaises(ValueError):
                    verify_tree(root)
        finally:
            tmpdir.cleanup()

    def test_packages_missing_stanza_fails_full_verify(self) -> None:
        """A dropped package must fail even with consistent Release and signatures."""
        tmpdir, home, key_id = make_gpg_home_with_key()
        try:
            with tempfile.TemporaryDirectory() as work:
                root = build_complete_tree(Path(work) / "repo", "20260102-030405",
                                           key_home=home, key_id=key_id)
                self.assertTrue(verify_tree(root))
                target = root / "releases" / "20260102-030405" / "dists" / "stable" / "main" / "binary-amd64" / "Packages"
                stanzas = target.read_text().strip().split("\n\n")
                self.assertEqual(len(stanzas), 2)
                target.write_text(stanzas[0] + "\n", encoding="utf-8")
                resign_tree(root, "20260102-030405", key_home=home, key_id=key_id)
                with self.assertRaises(ValueError) as ctx:
                    verify_tree(root)
                self.assertIn("omits pool file", str(ctx.exception))
        finally:
            tmpdir.cleanup()

    def test_packages_defects_fail_full_verify(self) -> None:
        cases = {
            "duplicate_filename": lambda s: [s[0], s[0]],
            "wrong_architecture": lambda s: [
                s[0], s[1].replace("Architecture: arm64", "Architecture: amd64", 1)
            ],
        }
        for label, mutate in cases.items():
            with self.subTest(case=label):
                tmpdir, home, key_id = make_gpg_home_with_key()
                try:
                    with tempfile.TemporaryDirectory() as work:
                        root = build_complete_tree(Path(work) / "repo", "20260102-030405",
                                                   key_home=home, key_id=key_id)
                        target = root / "releases" / "20260102-030405" / "dists" / "stable" / "main" / "binary-arm64" / "Packages"
                        stanzas = target.read_text().strip().split("\n\n")
                        self.assertEqual(len(stanzas), 2)
                        target.write_text("\n\n".join(mutate(stanzas)) + "\n", encoding="utf-8")
                        resign_tree(root, "20260102-030405", key_home=home, key_id=key_id)
                        with self.assertRaises(ValueError):
                            verify_tree(root)
                finally:
                    tmpdir.cleanup()


if __name__ == "__main__":
    unittest.main()
