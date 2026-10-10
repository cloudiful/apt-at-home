import tempfile
import unittest
from pathlib import Path

from scripts.package_index import check_arch_packages, parse_stanzas, pool_debs_for_arch


class PackageIndexTest(unittest.TestCase):
    def test_parse_stanzas_reads_fields(self) -> None:
        text = (
            "Package: alpha\nArchitecture: amd64\nFilename: pool/alpha_1_amd64.deb\n\n"
            "Package: beta\nArchitecture: amd64\nFilename: pool/beta_1_amd64.deb\n"
        )
        stanzas = parse_stanzas(text)
        self.assertEqual([s.fields["Package"] for s in stanzas], ["alpha", "beta"])

    def test_duplicate_identity_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            pool = output / "pool"
            pool.mkdir()
            for name in ("alpha_1_amd64.deb", "beta_1_amd64.deb"):
                (pool / name).write_bytes(b"x")
            text = (
                "Package: alpha\nVersion: 1\nArchitecture: amd64\n"
                "Filename: pool/alpha_1_amd64.deb\n\n"
                "Package: alpha\nVersion: 1\nArchitecture: amd64\n"
                "Filename: pool/alpha_1_amd64.deb\n"
            )
            with self.assertRaises(ValueError) as ctx:
                check_arch_packages(
                    text, arch="amd64", output_root=output,
                    expected_filenames=pool_debs_for_arch(output, "amd64"),
                )
            self.assertIn("duplicate", str(ctx.exception))

    def test_missing_and_foreign_arch_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            pool = output / "pool"
            pool.mkdir()
            for name in ("alpha_1_amd64.deb", "beta_1_amd64.deb"):
                (pool / name).write_bytes(b"x")
            expected = pool_debs_for_arch(output, "amd64")
            only_alpha = (
                "Package: alpha\nVersion: 1\nArchitecture: amd64\n"
                "Filename: pool/alpha_1_amd64.deb\n"
            )
            with self.assertRaises(ValueError) as ctx:
                check_arch_packages(only_alpha, arch="amd64", output_root=output,
                                    expected_filenames=expected)
            self.assertIn("omits pool file", str(ctx.exception))
            foreign = only_alpha.replace("Architecture: amd64", "Architecture: arm64")
            with self.assertRaises(ValueError) as ctx:
                check_arch_packages(foreign, arch="amd64", output_root=output,
                                    expected_filenames=expected)
            self.assertIn("arm64", str(ctx.exception))

    def test_complete_index_returns_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            pool = output / "pool"
            pool.mkdir()
            for name in ("alpha_1_amd64.deb", "beta_1_amd64.deb"):
                (pool / name).write_bytes(b"x")
            text = (
                "Package: alpha\nVersion: 1\nArchitecture: amd64\n"
                "Filename: pool/alpha_1_amd64.deb\n\n"
                "Package: beta\nVersion: 1\nArchitecture: amd64\n"
                "Filename: pool/beta_1_amd64.deb\n"
            )
            count = check_arch_packages(
                text, arch="amd64", output_root=output,
                expected_filenames=pool_debs_for_arch(output, "amd64"),
            )
            self.assertEqual(count, 2)


if __name__ == "__main__":
    unittest.main()
