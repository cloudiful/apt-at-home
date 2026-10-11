import copy
import unittest

from scripts.catalog import KNOWN_PACKAGES, check_known_names
from scripts.manifest import (
    deb_filename,
    debian_version,
    expected_debs,
    expected_debs_for_arch,
    load_manifest,
    parse_manifest,
)
from scripts.release_manifest import load_manifest_data
from scripts.manifest import default_manifest_path

TARGET = {
    "url": "https://github.com/example/example/releases/download/v1.0.0/x.tar.gz",
    "sha256": "a" * 64,
    "archive_member": "x",
    "binary": "x",
}


def _document() -> dict:
    return copy.deepcopy(load_manifest_data(default_manifest_path()))


class ManifestTest(unittest.TestCase):
    def test_pins_the_reviewed_catalog_with_both_architectures(self) -> None:
        manifest = load_manifest()
        self.assertEqual(
            sorted(p.name for p in manifest.packages),
            sorted(KNOWN_PACKAGES),
        )
        self.assertEqual(manifest.debian_revision, "-1")
        for pkg in manifest.packages:
            self.assertSetEqual(set(pkg.targets), {"amd64", "arm64"})
            for arch, target in pkg.targets.items():
                self.assertTrue(target.url.startswith("https://github.com/"))
                self.assertRegex(target.sha256, r"^[0-9a-f]{64}$")
                self.assertNotIn("latest", target.url)

    def test_records_exact_reviewed_versions(self) -> None:
        versions = {p.name: p.version for p in load_manifest().packages}
        self.assertEqual(
            versions,
            {
                "zellij": "0.45.1",
                "starship": "1.26.0",
                "git-delta": "0.20.1",
                "rustfs-cli": "0.1.36",
                "sccache": "0.18.0",
                "neovim": "0.12.6",
                "bottom": "0.14.9",
                "uv": "0.13.0",
                "zoxide": "0.10.0",
            },
        )

    def test_records_reviewed_archive_hashes(self) -> None:
        by_name = {p.name: p for p in load_manifest().packages}
        expected = {
            ("zellij", "amd64"): "40bcc2e03f5d5ae8e054e39f676081fe12ab70871506996ba595834c3718eefc",
            ("zellij", "arm64"): "05f0802afadd53f8db9514e7cae53c9ae8432fed1b35b8294aa816ee3044a16b",
            ("starship", "amd64"): "b7c232b0e8249d8e55a40beb79c5c43a7d370f3f9408bd215deb0170daeaadf3",
            ("starship", "arm64"): "dc30189378d2f2e287384e8a692d3f95ad1df64cf0e8c36aa9201516028aed6b",
            ("git-delta", "amd64"): "69dcb4043f55c29118f408c6efd30115e0a4e0a6709253343b8c8fa814ab4a31",
            ("git-delta", "arm64"): "da7f4338f593572ff426ae153e0870e2fdc72729416eff551b40cdeb67940db8",
            ("rustfs-cli", "amd64"): "4a8128911ccad4e7b481f26635a4cfd1ec064412210526e57ad2c748d356f3b7",
            ("rustfs-cli", "arm64"): "6e9dea7ac4f81562add9e1e31206ba6059d6f5929ad4ed924986223282b3aa9c",
            ("sccache", "amd64"): "45f1447fbe231e3037bde351ef70677dd212216c8d62ae7ca409fecc4d6acc89",
            ("sccache", "arm64"): "2b3284d5da3b46a47dc4229e75bb7b88ac4aa99c8d754fb7d2f84997e5a4354a",
            ("bottom", "amd64"): "e0c325829e8bdcea25a8a7651da05dbb6f1fc109ac5d2f0187867d3d5d9c0df8",
            ("bottom", "arm64"): "a6a0a0dda426b31b2c72e15a26c7e97c2224aafdd66f60632e34ee464a64ec8f",
            ("uv", "amd64"): "1468ebd5a5541121837c5a2817b9972ba6090fa6caa3d142620850a47fb75154",
            ("uv", "arm64"): "3ccfb6af6e242433eb552f7d9676abd5412c6595c497e990d9c8cb7b5bd4d2c3",
            ("zoxide", "amd64"): "2d93385b99f3e82cf2701609a1bffcad863fbeb75aa3fe7eb6be4d29be68b1ae",
            ("zoxide", "arm64"): "f1f16c5d6298d63dee467eedea1cdcd8490e43e493bea43acd416dc9033ef641",
        }
        for (name, arch), digest in expected.items():
            self.assertEqual(by_name[name].targets[arch].sha256, digest, msg=f"{name} {arch}")

    def test_records_nested_members_binaries_and_licenses(self) -> None:
        by_name = {p.name: p for p in load_manifest().packages}
        self.assertEqual(
            by_name["sccache"].targets["amd64"].archive_member,
            "sccache-v0.18.0-x86_64-unknown-linux-musl/sccache",
        )
        self.assertEqual(by_name["sccache"].targets["amd64"].binary, "sccache")
        # delta ships a different toolchain prefix per architecture.
        self.assertEqual(
            by_name["git-delta"].targets["amd64"].archive_member,
            "delta-0.20.1-x86_64-unknown-linux-musl/delta",
        )
        self.assertEqual(
            by_name["git-delta"].targets["arm64"].archive_member,
            "delta-0.20.1-aarch64-unknown-linux-gnu/delta",
        )
        self.assertEqual(by_name["rustfs-cli"].targets["arm64"].archive_member, "rc")
        self.assertEqual(by_name["rustfs-cli"].targets["arm64"].binary, "rc")
        uv = by_name["uv"]
        self.assertEqual(uv.targets["amd64"].additional_binaries,
                         {"uv-x86_64-unknown-linux-gnu/uvx": "uvx"})
        self.assertEqual(uv.extra_license_urls,
                         ("https://raw.githubusercontent.com/astral-sh/uv/0.13.0/LICENSE-APACHE",))
        self.assertEqual(uv.license, "MIT OR Apache-2.0")
        self.assertEqual(by_name["sccache"].license, "Apache-2.0")
        self.assertEqual(by_name["git-delta"].license, "MIT")
        self.assertEqual(by_name["rustfs-cli"].license, "MIT")

    def test_declares_dependencies_only_where_the_binary_needs_them(self) -> None:
        by_name = {p.name: p for p in load_manifest().packages}
        # The arm64 delta build is glibc-linked; its amd64 build is static musl.
        self.assertEqual(
            by_name["git-delta"].targets["arm64"].depends,
            "libc6 (>= 2.18), libgcc-s1",
        )
        self.assertEqual(by_name["git-delta"].targets["amd64"].depends, "")
        self.assertEqual(by_name["bottom"].targets["amd64"].depends,
                         "libc6 (>= 2.34), libgcc-s1")
        self.assertEqual(by_name["uv"].targets["amd64"].depends,
                         "libc6 (>= 2.17), libgcc-s1")
        self.assertEqual(by_name["uv"].targets["arm64"].depends,
                         "libc6 (>= 2.28), libgcc-s1")
        for name in ("sccache", "rustfs-cli", "zellij", "starship", "zoxide"):
            for arch in ("amd64", "arm64"):
                self.assertEqual(by_name[name].targets[arch].depends, "", msg=f"{name} {arch}")

    def test_expected_sets_are_derived_from_the_manifest(self) -> None:
        manifest = load_manifest()
        expected = expected_debs(manifest)
        self.assertEqual(len(expected), len(manifest.packages) * 2)
        self.assertIn("git-delta_0.20.1-1_amd64.deb", expected)
        self.assertIn("sccache_0.18.0-1_arm64.deb", expected)
        for arch in ("amd64", "arm64"):
            per_arch = expected_debs_for_arch(manifest, arch)
            self.assertEqual(len(per_arch), len(manifest.packages))
            self.assertTrue(all(name.endswith(f"_{arch}.deb") for name in per_arch))
        with self.assertRaises(ValueError):
            expected_debs_for_arch(manifest, "i386")

    def test_debian_naming_uses_revision(self) -> None:
        manifest = load_manifest()
        by_name = {p.name: p for p in manifest.packages}
        self.assertEqual(debian_version(by_name["zellij"], manifest), "0.45.1-1")
        self.assertEqual(deb_filename(by_name["zellij"], "amd64", manifest),
                         "zellij_0.45.1-1_amd64.deb")
        self.assertEqual(deb_filename(by_name["starship"], "arm64", manifest),
                         "starship_1.26.0-1_arm64.deb")
        self.assertEqual(deb_filename(by_name["rustfs-cli"], "amd64", manifest),
                         "rustfs-cli_0.1.36-1_amd64.deb")

    def test_a_reviewed_subset_is_still_valid(self) -> None:
        """The currently published two-package manifest stays a valid document."""
        data = _document()
        data["packages"] = [e for e in data["packages"] if e["name"] in ("starship", "zellij")]
        manifest = parse_manifest(data)
        self.assertEqual(sorted(p.name for p in manifest.packages), ["starship", "zellij"])
        self.assertEqual(len(expected_debs(manifest)), 4)

    def test_rejects_bad_manifest(self) -> None:
        with self.assertRaises(ValueError):
            parse_manifest({"packages": []})
        with self.assertRaises(ValueError):
            parse_manifest({
                "maintainer": "m", "section": "s", "priority": "p",
                "debian_revision": "-2",
                "packages": [],
            })

    def test_rejects_an_unreviewed_or_repeated_package(self) -> None:
        data = _document()
        renamed = copy.deepcopy(data["packages"][0])
        renamed["name"] = "vim-unreviewed"
        data["packages"] = data["packages"][:1] + [renamed]
        with self.assertRaises(ValueError) as ctx:
            parse_manifest(data)
        self.assertIn("unreviewed", str(ctx.exception))

        duplicated = copy.deepcopy(data)
        duplicated["packages"] = duplicated["packages"] + [copy.deepcopy(data["packages"][0])]
        with self.assertRaises(ValueError) as ctx:
            parse_manifest(duplicated)
        self.assertIn("duplicate package target", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            check_known_names(["zellij", "zellij"])
        self.assertIn("repeats", str(ctx.exception))

    def test_rejects_a_missing_architecture(self) -> None:
        data = _document()
        del data["packages"][0]["targets"]["arm64"]
        with self.assertRaises(ValueError) as ctx:
            parse_manifest(data)
        self.assertIn("missing arch target", str(ctx.exception))

    def test_rejects_unsafe_archive_members(self) -> None:
        for member in ("", "/etc/passwd", "../escape", "a/../../escape",
                       "sccache-v0.18.0/sccache\\evil", "./", "a//b", "a/./b"):
            data = _document()
            data["packages"][0]["targets"]["amd64"]["archive_member"] = member
            with self.assertRaises(ValueError, msg=member):
                parse_manifest(data)

    def test_accepts_a_single_dot_prefix_and_nested_members(self) -> None:
        data = _document()
        data["packages"][0]["targets"]["amd64"]["archive_member"] = (
            "./sccache-v0.18.0-x86_64-unknown-linux-musl/sccache"
        )
        manifest = parse_manifest(data)
        self.assertEqual(
            manifest.packages[0].targets["amd64"].archive_member,
            "sccache-v0.18.0-x86_64-unknown-linux-musl/sccache",
        )

    def test_rejects_dependency_injection(self) -> None:
        injections = (
            "libc6 (>= 2.18); rm -rf /",
            "libc6 (>= 2.18)\nPre-Depends: evil",
            "libc6 $(id)",
            "libc6 `id`",
            "libc6 && curl evil",
            " libgcc-s1",
            "libgcc-s1 ",
            "libgcc-s1|cat",
        )
        for depends in injections:
            data = _document()
            data["packages"][0]["targets"]["amd64"]["depends"] = depends
            with self.assertRaises(ValueError, msg=repr(depends)):
                parse_manifest(data)

    def test_each_arch_target_declares_exactly_one_payload_source(self) -> None:
        data = _document()
        neovim = next(e for e in data["packages"] if e["name"] == "neovim")
        both = copy.deepcopy(neovim)
        both["targets"]["amd64"]["archive_member"] = "nvim"
        neither = copy.deepcopy(neovim)
        del neither["targets"]["amd64"]["install_tree"]
        subtree = copy.deepcopy(neovim)
        subtree["targets"]["amd64"]["install_tree"] = "nvim-linux-x86_64/share"
        for label, entry in (("both", both), ("neither", neither), ("subtree", subtree)):
            with self.subTest(label):
                bad = _document()
                bad["packages"] = [entry if e["name"] == "neovim" else e for e in bad["packages"]]
                with self.assertRaises(ValueError):
                    parse_manifest(bad)

    def test_neovim_declares_the_runtime_conflict_and_replacement(self) -> None:
        by_name = {p.name: p for p in load_manifest().packages}
        self.assertEqual(by_name["neovim"].conflicts, "neovim-runtime")
        self.assertEqual(by_name["neovim"].replaces, "neovim-runtime")
        for name in ("zellij", "starship", "git-delta", "rustfs-cli", "sccache"):
            self.assertEqual(by_name[name].conflicts, "", msg=name)
            self.assertEqual(by_name[name].replaces, "", msg=name)

    def test_reviewed_extra_metadata_is_required_and_closed(self) -> None:
        data = _document()
        invalid = []
        missing = copy.deepcopy(data)
        del next(p for p in missing["packages"] if p["name"] == "uv")["extra_license_urls"]
        invalid.append(missing)
        renamed = copy.deepcopy(data)
        uv = next(p for p in renamed["packages"] if p["name"] == "uv")
        uv["targets"]["amd64"]["additional_binaries"] = {
            "uv-x86_64-unknown-linux-gnu/uvx": "uv"
        }
        invalid.append(renamed)
        injected = copy.deepcopy(data)
        uv = next(p for p in injected["packages"] if p["name"] == "uv")
        uv["targets"]["amd64"]["additional_binaries"] = {"../uvx": "uvx"}
        invalid.append(injected)
        for bad in invalid:
            with self.assertRaises(ValueError):
                parse_manifest(bad)

    def test_rejects_relation_injection_in_conflicts_and_replaces(self) -> None:
        for key in ("conflicts", "replaces"):
            for value in ("neovim-runtime; rm -rf /", "neovim-runtime\nPre-Depends: x", "a $(id)"):
                data = _document()
                data["packages"][0][key] = value
                with self.assertRaises(ValueError, msg=f"{key}={value!r}"):
                    parse_manifest(data)

    def test_accepts_a_plain_dependency_list(self) -> None:
        data = _document()
        data["packages"][0]["targets"]["amd64"]["depends"] = (
            "libc6 (>= 2.18), libgcc-s1, zlib1g"
        )
        manifest = parse_manifest(data)
        self.assertEqual(
            manifest.packages[0].targets["amd64"].depends,
            "libc6 (>= 2.18), libgcc-s1, zlib1g",
        )


if __name__ == "__main__":
    unittest.main()
