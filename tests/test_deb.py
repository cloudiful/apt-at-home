"""Generated control data: dependencies follow the binary, and nothing else.

A static binary must produce a control file with no `Depends` line at all, and a
glibc-linked one must carry exactly the relation its architecture declares. The
staged package also carries the vendored license, so a reviewer can confirm the
SPDX identifier and the upstream bytes agree.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.deb import (
    SOURCE_DATE_EPOCH,
    build_deb,
    copyright_text,
    debian_control,
    finalize_package_stage,
    stage_package,
)
from scripts.manifest import deb_filename, load_manifest
from scripts.package_licenses import read_vendor_licenses
from tests.helpers import stage_fixture_target


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

    def test_neovim_control_declares_its_runtime_conflict_and_replacement(self) -> None:
        manifest = load_manifest()
        for arch in ("amd64", "arm64"):
            control = debian_control(self._by_name()["neovim"], arch, manifest, installed_size_kb=1)
            self.assertIn("Conflicts: neovim-runtime\n", control, msg=arch)
            self.assertIn("Replaces: neovim-runtime\n", control, msg=arch)
            self.assertLess(control.index("Depends:"), control.index("Conflicts:"), msg=arch)
            self.assertLess(control.index("Replaces:"), control.index("Description:"), msg=arch)
        sccache = debian_control(self._by_name()["sccache"], "amd64", manifest, installed_size_kb=1)
        self.assertNotIn("Conflicts:", sccache)
        self.assertNotIn("Replaces:", sccache)

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
        source_root = root / f"src-{name}-{arch}"
        source_root.mkdir(parents=True, exist_ok=True)
        binary = source_root / pkg.targets[arch].binary
        binary.write_bytes(f"fake-{name}-{arch}".encode())
        binary.chmod(0o755)
        additional = {}
        for binary_name in pkg.targets[arch].additional_binaries.values():
            source = source_root / binary_name
            source.write_bytes(f"fake-{name}-{binary_name}".encode())
            source.chmod(0o755)
            additional[binary_name] = source
        return stage_package(
            pkg=pkg, arch=arch, manifest=manifest,
            binary_file=binary, additional_binary_files=additional,
            stage_root=root / "stages",
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

    def test_repeated_staging_replaces_flat_commands_and_tree_payloads(self) -> None:
        manifest = load_manifest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            flat = self._stage(root, "uv", "amd64")
            stale_command = flat / "usr" / "bin" / "stale-command"
            stale_command.write_text("stale", encoding="utf-8")
            flat_again = self._stage(root, "uv", "amd64")
            self.assertEqual(flat_again, flat)
            self.assertFalse(stale_command.exists())
            self.assertEqual(
                {path.name for path in (flat / "usr" / "bin").iterdir()}, {"uv", "uvx"}
            )

            neovim = next(pkg for pkg in manifest.packages if pkg.name == "neovim")
            tree = stage_fixture_target(
                root, neovim, "amd64", manifest, b"first", root / "stages"
            )
            stale_tree_file = tree / "usr" / "share" / "doc" / "neovim" / "stale.txt"
            stale_tree_file.write_text("stale", encoding="utf-8")
            tree_again = stage_fixture_target(
                root, neovim, "amd64", manifest, b"second", root / "stages"
            )
            self.assertEqual(tree_again, tree)
            self.assertFalse(stale_tree_file.exists())
            self.assertEqual((tree / "usr" / "bin" / "nvim").read_bytes(), b"second")

    def test_uv_stages_both_commands_and_both_license_texts(self) -> None:
        manifest = load_manifest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = self._stage(root, "uv", "amd64")
            for name, payload in (("uv", b"fake-uv-amd64"), ("uvx", b"fake-uv-uvx")):
                self.assertEqual((stage / "usr" / "bin" / name).read_bytes(), payload)
                self.assertEqual((stage / "usr" / "bin" / name).stat().st_mode & 0o777, 0o755)
            copyright_file = stage / "usr" / "share" / "doc" / "uv" / "copyright"
            text = copyright_file.read_text()
            self.assertIn("License: MIT OR Apache-2.0", text)
            for body in read_vendor_licenses(next(p for p in manifest.packages if p.name == "uv")):
                self.assertIn(body.rstrip(), text)
            output = root / "uv.deb"
            build_deb(stage, output)
            contents = subprocess.run(
                ["dpkg-deb", "-c", str(output)], text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(contents.returncode, 0)
            self.assertIn("./usr/bin/uvx", contents.stdout)


class FinalizePackageStageTest(unittest.TestCase):
    """The extracted finalizer is reusable, idempotent, and alias-safe.

    `finalize_package_stage` is called by `stage_package` and then called again
    by the candidate after it adds its own payload, so it must tolerate being
    run twice over a tree that now contains a symlink this package owns.
    """

    def setUp(self) -> None:
        self.manifest = load_manifest()
        self.pkg = next(pkg for pkg in self.manifest.packages if pkg.name == "uv")

    def _finalize(self, stage: Path) -> None:
        finalize_package_stage(
            stage=stage, pkg=self.pkg, arch="amd64", manifest=self.manifest,
            license_bodies=read_vendor_licenses(self.pkg),
        )

    def _stage(self, root: Path) -> Path:
        stage = root / "stage"
        bin_dir = stage / "usr" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "uv").write_bytes(b"fake")
        (bin_dir / "uv").chmod(0o755)
        self._finalize(stage)
        return stage

    def test_is_idempotent_over_its_own_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stage = self._stage(Path(tmp))
            first = _snapshot(stage)
            self._finalize(stage)
            self.assertEqual(_snapshot(stage), first)

    def test_recounts_installed_size_when_payload_grows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stage = self._stage(Path(tmp))
            before = _installed_size_kb(stage)
            extra = stage / "usr" / "share" / "doc" / "uv" / "README.source"
            extra.write_bytes(b"x" * 4096)
            self._finalize(stage)
            self.assertGreater(_installed_size_kb(stage), before)

    def test_excludes_a_symlinked_binary_from_the_installed_size(self) -> None:
        """The alias is a link, not a second copy, so it must not be counted."""
        with tempfile.TemporaryDirectory() as tmp:
            stage = self._stage(Path(tmp))
            before = _installed_size_kb(stage)
            (stage / "usr" / "bin" / "uvx").symlink_to("uv")
            self._finalize(stage)
            self.assertEqual(_installed_size_kb(stage), before)

    def test_clamps_the_alias_mtime_without_following_the_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stage = self._stage(Path(tmp))
            alias = stage / "usr" / "bin" / "uvx"
            alias.symlink_to("uv")
            os.utime(alias, (0, 0), follow_symlinks=False)
            self._finalize(stage)
            self.assertEqual(alias.lstat().st_mtime, float(SOURCE_DATE_EPOCH))
            # The link target is untouched by the finalizer's own file policy.
            self.assertEqual(
                (stage / "usr" / "bin" / "uv").stat().st_mode & 0o777, 0o755
            )

    def test_does_not_chmod_the_alias_target(self) -> None:
        """A doc-mode file behind a link must stay 0644, proving no dereference."""
        with tempfile.TemporaryDirectory() as tmp:
            stage = self._stage(Path(tmp))
            doc = stage / "usr" / "share" / "doc" / "uv" / "copyright"
            (stage / "usr" / "share" / "doc" / "uv" / "link").symlink_to(doc.name)
            self._finalize(stage)
            self.assertEqual(doc.stat().st_mode & 0o777, 0o644)

    def test_uses_the_explicit_license_bodies_it_was_given(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stage = self._stage(Path(tmp))
            self._finalize(stage)
            text = (stage / "usr" / "share" / "doc" / "uv" / "copyright").read_text()
            self.assertIn("License: MIT OR Apache-2.0", text)

    def test_explicit_bodies_replace_the_catalog_ones(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp) / "stage"
            (stage / "usr" / "bin").mkdir(parents=True)
            finalize_package_stage(
                stage=stage, pkg=self.pkg, arch="amd64", manifest=self.manifest,
                license_bodies=("CUSTOM BODY",),
            )
            text = (stage / "usr" / "share" / "doc" / "uv" / "copyright").read_text()
            self.assertIn("CUSTOM BODY", text)
            self.assertNotIn("Apache License", text)


def _snapshot(stage: Path) -> list[tuple[str, int, int]]:
    """Every entry's relative path, mode and mtime, without dereferencing links."""
    return sorted(
        (p.relative_to(stage).as_posix(), p.lstat().st_mode & 0o777, int(p.lstat().st_mtime))
        for p in [stage, *stage.rglob("*")]
    )


def _installed_size_kb(stage: Path) -> int:
    for line in (stage / "DEBIAN" / "control").read_text().splitlines():
        if line.startswith("Installed-Size:"):
            return int(line.split(":", 1)[1])
    raise AssertionError("control file has no Installed-Size")


if __name__ == "__main__":
    unittest.main()
