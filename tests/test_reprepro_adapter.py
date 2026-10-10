"""Real reprepro export: flat pool filenames, complete per-arch indices, determinism.

These tests run the installed ``reprepro`` binary against throwaway databases
built from tiny staged packages. They are skipped nowhere: a missing reprepro
must surface as a failure, because the production index depends on it.
"""

import gzip
import tempfile
import unittest
from pathlib import Path

from scripts.deb import build_deb
from scripts.manifest import deb_filename, load_manifest
from tests.helpers import stage_fixture_target
from scripts.reprepro_adapter import (
    _normalize_stanzas,
    generate_packages,
    reprepro_version,
)

ARCHES = ("amd64", "arm64")


def _fields(stanza: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in stanza.splitlines():
        if ": " in line:
            key, value = line.split(": ", 1)
            fields[key.strip()] = value.strip()
    return fields


def _stanzas(text: str) -> list[dict[str, str]]:
    return [_fields(block) for block in text.strip().split("\n\n") if block.strip()]


def _fake_debs(root: Path) -> tuple[list[Path], Path]:
    """Stage one .deb per pinned package and architecture, without the network."""
    manifest = load_manifest()
    pool = root / "pool"
    pool.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    for pkg in manifest.packages:
        for arch in ARCHES:
            stage = stage_fixture_target(
                root, pkg, arch, manifest, f"fake-{pkg.name}-{arch}".encode(), root / "stages",
            )
            out = pool / deb_filename(pkg, arch, manifest)
            build_deb(stage, out)
            outputs.append(out)
    return outputs, pool


class RepreproAdapterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        print(f"reprepro under test: {reprepro_version()}")

    def test_exports_complete_flat_pool_indices(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = load_manifest()
            debs, pool = _fake_debs(root)
            dists = root / "dists"
            for arch in ARCHES:
                arch_debs = sorted(pool.glob(f"*_{arch}.deb"))
                self.assertEqual(len(arch_debs), len(manifest.packages))
                packages, packages_gz = generate_packages(
                    pool, dists / "stable" / "main" / f"binary-{arch}", arch_debs
                )
                stanzas = _stanzas(packages.read_text())
                self.assertEqual(len(stanzas), len(manifest.packages))
                names = set()
                for stanza in stanzas:
                    self.assertEqual(stanza["Architecture"], arch)
                    filename = stanza["Filename"]
                    # reprepro's nested pool layout must be flattened to the
                    # exact pool-relative path the Worker serves.
                    self.assertNotIn("pool/main/", filename)
                    self.assertTrue(filename.startswith("pool/"))
                    self.assertNotIn("/", filename[len("pool/"):])
                    self.assertTrue((root / filename).is_file())
                    names.add(filename.removeprefix("pool/"))
                self.assertEqual(names, {deb.name for deb in arch_debs})
                self.assertEqual(gzip.decompress(packages_gz.read_bytes()), packages.read_bytes())

    def test_indices_carry_no_foreign_architecture_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, pool = _fake_debs(root)
            dists = root / "dists"
            for arch, other in (("amd64", "arm64"), ("arm64", "amd64")):
                packages, _ = generate_packages(
                    pool, dists / "stable" / "main" / f"binary-{arch}",
                    sorted(pool.glob(f"*_{arch}.deb")),
                )
                listed = {s["Filename"] for s in _stanzas(packages.read_text())}
                self.assertTrue(listed)
                for deb in pool.glob(f"*_{other}.deb"):
                    self.assertNotIn(f"pool/{deb.name}", listed)

    def test_one_export_of_the_full_pool_matches_per_arch_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            debs, pool = _fake_debs(root)
            whole = root / "whole"
            per_arch = root / "per-arch"
            # The full pool (both architectures) exported once per index; the
            # per-arch call consumes only its own debs. Both must agree.
            for arch in ARCHES:
                generate_packages(pool, whole / "stable" / "main" / f"binary-{arch}", debs)
                generate_packages(
                    pool, per_arch / f"binary-{arch}",
                    [deb for deb in debs if deb.name.endswith(f"_{arch}.deb")],
                )
            for arch in ARCHES:
                for name in ("Packages", "Packages.gz"):
                    self.assertEqual(
                        (whole / "stable" / "main" / f"binary-{arch}" / name).read_bytes(),
                        (per_arch / f"binary-{arch}" / name).read_bytes(),
                        msg=f"{arch}/{name}",
                    )

    def test_output_is_deterministic_across_independent_databases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, pool = _fake_debs(root)
            arch_debs = sorted(pool.glob("*_amd64.deb"))
            first, first_gz = generate_packages(pool, root / "a" / "binary-amd64", arch_debs)
            reversed_call, reversed_gz = generate_packages(
                pool, root / "b" / "binary-amd64", list(reversed(arch_debs))
            )
            self.assertEqual(first.read_bytes(), reversed_call.read_bytes())
            self.assertEqual(first_gz.read_bytes(), reversed_gz.read_bytes())

    def test_filename_normalization_rejects_unpooled_and_traversing_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp)
            (pool / "ok_1_amd64.deb").write_bytes(b"")
            good = "Package: ok\nFilename: pool/main/o/ok/ok_1_amd64.deb\n"
            self.assertEqual(
                _normalize_stanzas(good, pool_dir=pool),
                "Package: ok\nFilename: pool/ok_1_amd64.deb\n",
            )
            for bad in (
                "Package: ok\nFilename: pool/main/o/ok/missing_1_amd64.deb\n",
                "Package: ok\nFilename: pool/../../etc/passwd\n",
                "Package: ok\nVersion: 1\n",
            ):
                with self.subTest(text=bad.splitlines()[-1]):
                    with self.assertRaises(ValueError):
                        _normalize_stanzas(bad, pool_dir=pool)

    def test_empty_input_and_unexpected_output_directory_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, pool = _fake_debs(root)
            with self.assertRaises(ValueError):
                generate_packages(pool, root / "binary-amd64", [])
            with self.assertRaises(ValueError):
                generate_packages(pool, root / "binary-i386", sorted(pool.glob("*_amd64.deb")))


if __name__ == "__main__":
    unittest.main()
