"""Generated control data: dependencies follow the binary, and nothing else.

A static binary must produce a control file with no `Depends` line at all, and a
glibc-linked one must carry exactly the relation its architecture declares. The
staged package also carries the vendored license, so a reviewer can confirm the
SPDX identifier and the upstream bytes agree.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.deb import build_deb, copyright_text, debian_control, stage_package
from scripts.manifest import deb_filename, load_manifest


class DebianControlTest(unittest.TestCase):
    def _by_name(self) -> dict:
        return {p.name: p for p in load_manifest().packages}

    def test_depends_is_emitted_only_where_declared(self) -> None:
        manifest = load_manifest()
        by_name = self._by_name()
        static = debian_control(
            by_name["sccache"], "amd64", manifest, installed_size_kb=1
        )
        self.assertNotIn("Depends:", static)
        self.assertIn("Package: sccache\n", static)
        self.assertIn("Description: Compiler caching for Rust and other build systems\n", static)

        dynamic = debian_control(
            by_name["git-delta"], "arm64", manifest, installed_size_kb=1
        )
        self.assertIn("Depends: libc6 (>= 2.18), libgcc-s1\n", dynamic)
        # The field sits before Description, not after it.
        self.assertLess(dynamic.index("Depends:"), dynamic.index("Description:"))
        # The static sibling of the same package stays dependency-free.
        self.assertNotIn(
            "Depends:",
            debian_control(by_name["git-delta"], "amd64", manifest, installed_size_kb=1),
        )

    def test_description_makes_no_unverified_c_libc_claim(self) -> None:
        manifest = load_manifest()
        for name, pkg in self._by_name().items():
            control = debian_control(pkg, "arm64", manifest, installed_size_kb=1)
            self.assertIn(" Repackaged upstream binary for a private APT repository.\n",
                          control, msg=name)
            self.assertNotIn("musl", control, msg=name)


class StagePackageTest(unittest.TestCase):
    def _stage(self, root: Path, name: str, arch: str) -> Path:
        manifest = load_manifest()
        pkg = next(p for p in manifest.packages if p.name == name)
        binary = root / f"src-{name}-{arch}" / pkg.targets[arch].binary
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(f"fake-{name}-{arch}".encode())
        binary.chmod(0o755)
        return stage_package(
            pkg=pkg, arch=arch, manifest=manifest,
            binary_file=binary, stage_root=root / "stages",
        )

    def test_each_package_installs_its_own_binary_and_license(self) -> None:
        manifest = load_manifest()
        expected = {
            "git-delta": ("delta", "MIT", "Dan Davison"),
            "rustfs-cli": ("rc", "MIT", "RustFS Contributors"),
            "sccache": ("sccache", "Apache-2.0", "sccache contributors"),
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, (binary_name, license_id, copyright_holder) in expected.items():
                with self.subTest(package=name):
                    stage = self._stage(root, name, "amd64")
                    installed = stage / "usr" / "bin" / binary_name
                    self.assertTrue(installed.is_file())
                    copyright_file = (
                        stage / "usr" / "share" / "doc" / name / "copyright"
                    ).read_text()
                    self.assertIn(f"License: {license_id}", copyright_file)
                    self.assertIn(copyright_holder, copyright_file)
                    output = root / "pool" / deb_filename(
                        next(p for p in manifest.packages if p.name == name),
                        "amd64", manifest,
                    )
                    build_deb(stage, output)
                    fields = subprocess.run(
                        ["dpkg-deb", "-f", str(output), "Package", "Architecture"],
                        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        check=False,
                    )
                    self.assertEqual(fields.returncode, 0)
                    self.assertIn(name, fields.stdout)
                    contents = subprocess.run(
                        ["dpkg-deb", "-c", str(output)],
                        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        check=False,
                    )
                    self.assertIn(f"./usr/bin/{binary_name}", contents.stdout)
                    self.assertIn(f"./usr/share/doc/{name}/copyright", contents.stdout)

    def test_dynamic_dependencies_reach_the_built_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = load_manifest()
            stage = self._stage(root, "git-delta", "arm64")
            output = root / "git-delta.deb"
            build_deb(stage, output)
            shown = subprocess.run(
                ["dpkg-deb", "-f", str(output), "Depends", "Architecture"],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(shown.returncode, 0)
            self.assertIn("libc6 (>= 2.18), libgcc-s1", shown.stdout)
            self.assertIn("arm64", shown.stdout)

            static = self._stage(root, "git-delta", "amd64")
            static_output = root / "git-delta-static.deb"
            build_deb(static, static_output)
            shown = subprocess.run(
                ["dpkg-deb", "-f", str(static_output), "Depends"],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(shown.stdout.strip(), "")

    def test_changelog_does_not_assert_a_libc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stage = self._stage(Path(tmp), "rustfs-cli", "arm64")
            changelog = (stage / "usr" / "share" / "doc" / "rustfs-cli" / "changelog.Debian").read_text()
            self.assertNotIn("musl", changelog)
            self.assertIn("Repackage upstream 0.1.36 arm64 binary.", changelog)

    def test_copyright_names_the_declared_license_and_holder(self) -> None:
        manifest = load_manifest()
        pkg = next(p for p in manifest.packages if p.name == "sccache")
        text = copyright_text(pkg, "Apache License\nVersion 2.0\n")
        self.assertIn("License: Apache-2.0", text)
        self.assertIn("sccache contributors", text)


if __name__ == "__main__":
    unittest.main()
