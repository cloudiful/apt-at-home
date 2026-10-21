import copy
import unittest

from scripts.catalog import KNOWN_PACKAGES, RECIPES, arches_for, check_known_names
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
from scripts.upstream_assets import asset_name, release_download_url

TARGET = {
    "url": "https://github.com/example/example/releases/download/v1.0.0/x.tar.gz",
    "sha256": "a" * 64,
    "archive_member": "x",
    "binary": "x",
}


def _document() -> dict:
    return copy.deepcopy(load_manifest_data(default_manifest_path()))


class ManifestTest(unittest.TestCase):
    def test_pins_the_reviewed_catalog_with_reviewed_architectures(self) -> None:
        manifest = load_manifest()
        self.assertEqual(
            sorted(p.name for p in manifest.packages),
            sorted(KNOWN_PACKAGES),
        )
        self.assertEqual(manifest.debian_revision, "-1")
        for pkg in manifest.packages:
            self.assertSetEqual(set(pkg.targets), set(arches_for(pkg.name)))
            for arch, target in pkg.targets.items():
                # Every target is the official asset of its own package, on the
                # source host that package's recipe reviews.
                self.assertEqual(
                    target.url,
                    release_download_url(
                        RECIPES[pkg.name].repo,
                        RECIPES[pkg.name].tag_prefix + pkg.version,
                        asset_name(pkg.name, pkg.version, arch),
                        source=RECIPES[pkg.name].source,
                    ),
                )
                self.assertRegex(target.sha256, r"^[0-9a-f]{64}$")
                self.assertNotIn("latest", target.url)

    def test_rejects_a_url_that_is_not_the_reviewed_official_asset(self) -> None:
        """A target URL is bound to its own package, source, release, and asset."""
        data = _document()
        zellij = next(e for e in data["packages"] if e["name"] == "zellij")
        official = zellij["targets"]["amd64"]["url"]
        swapped = {
            "other_host": official.replace("github.com", "codeberg.org"),
            "other_repo": official.replace("zellij-org/zellij", "attacker/zellij"),
            "other_tag": official.replace("/v0.45.1/", "/v0.46.0/"),
            "other_asset": official.replace(
                "zellij-x86_64-unknown-linux-musl.tar.gz", "zellij-x86_64-unknown-linux-gnu.tar.gz"
            ),
            "credentials": official.replace("https://", "https://user:secret@"),
            "port": official.replace("https://github.com", "https://github.com:8443"),
            "query": official + "?token=secret",
            "fragment": official + "#secret",
            "plain_http": official.replace("https://", "http://"),
        }
        for label, url in swapped.items():
            with self.subTest(case=label):
                bad = _document()
                entry = next(e for e in bad["packages"] if e["name"] == "zellij")
                entry["targets"]["amd64"]["url"] = url
                with self.assertRaises(ValueError, msg=label):
                    parse_manifest(bad)

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
        kandev = by_name["kandev"]
        self.assertEqual(kandev.targets["amd64"].archive_member, "kandev/bin/kandev")
        self.assertEqual(kandev.targets["arm64"].archive_member, "kandev/bin/kandev")
        self.assertEqual(kandev.targets["amd64"].bundle_type, "kandev-standard")
        self.assertEqual(kandev.targets["arm64"].bundle_type, "kandev-standard")
        self.assertEqual(kandev.license, "AGPL-3.0-or-later")
        cargo = by_name["cargo-tauri"]
        self.assertEqual(set(cargo.targets), {"amd64"})
        self.assertEqual(cargo.targets["amd64"].archive_member, "cargo-tauri")
        self.assertEqual(cargo.targets["amd64"].binary, "cargo-tauri")
        self.assertEqual(cargo.license, "MIT OR Apache-2.0")
        self.assertEqual(
            cargo.extra_license_urls,
            ("https://raw.githubusercontent.com/tauri-apps/tauri/tauri-cli-v2.12.2/LICENSE-APACHE-2.0",),
        )

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
        self.assertEqual(by_name["kandev"].targets["amd64"].depends, "libc6 (>= 2.34)")
        self.assertEqual(by_name["kandev"].targets["arm64"].depends, "libc6 (>= 2.34)")
        self.assertEqual(by_name["uv"].targets["amd64"].depends,
                         "libc6 (>= 2.17), libgcc-s1")
        self.assertEqual(by_name["uv"].targets["arm64"].depends,
                         "libc6 (>= 2.28), libgcc-s1")
        self.assertEqual(
            by_name["cargo-tauri"].targets["amd64"].depends,
            "cargo, libc6 (>= 2.34), libgcc-s1 (>= 14.2.0), liblzma5 (>= 5.8.1)",
        )
        # Every package declares exactly the runtime its reviewed recipe pins,
        # so a static build stays free of dependencies and a new recipe cannot
        # drift from the manifest.
        for name, recipe in RECIPES.items():
            for arch in recipe.arches:
                with self.subTest(package=name, arch=arch):
                    self.assertEqual(
                        by_name[name].targets[arch].depends,
                        recipe.depends.get(arch, ""),
                    )

    def test_expected_sets_are_derived_from_the_manifest(self) -> None:
        manifest = load_manifest()
        expected = expected_debs(manifest)
        # Every declared target contributes exactly one deb, and the per-arch
        # set is the same set filtered by the architecture it is built for.
        self.assertEqual(
            expected,
            {
                deb_filename(pkg, arch, manifest)
                for pkg in manifest.packages
                for arch in pkg.targets
            },
        )
        for arch in ("amd64", "arm64"):
            per_arch = expected_debs_for_arch(manifest, arch)
            self.assertEqual(
                per_arch,
                {
                    deb_filename(pkg, arch, manifest)
                    for pkg in manifest.packages
                    if arch in pkg.targets
                },
            )
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
        self.assertEqual(
            len(expected_debs(manifest)),
            sum(len(pkg.targets) for pkg in manifest.packages),
        )

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

    def test_rejects_an_extra_unsupported_architecture(self) -> None:
        data = _document()
        cargo = next(e for e in data["packages"] if e["name"] == "cargo-tauri")
        cargo["targets"]["arm64"] = copy.deepcopy(cargo["targets"]["amd64"])
        with self.assertRaises(ValueError):
            parse_manifest(data)

    def test_rejects_unknown_asset_target_fields_including_removed_source_build(self) -> None:
        for name in ("cargo-tauri", "zellij"):
            with self.subTest(package=name):
                data = _document()
                entry = next(e for e in data["packages"] if e["name"] == name)
                entry["targets"]["amd64"]["source_build"] = {
                    "repository": "https://github.com/tauri-apps/tauri",
                    "tag": "tauri-cli-v2.12.2",
                }
                with self.assertRaises(ValueError) as ctx:
                    parse_manifest(data)
                self.assertIn("unknown field", str(ctx.exception))

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

    def test_kandev_bundle_source_stays_bound_to_its_reviewed_recipe(self) -> None:
        for arch in ("amd64", "arm64"):
            with self.subTest(arch=arch):
                changed_member = _document()
                kandev = next(p for p in changed_member["packages"] if p["name"] == "kandev")
                kandev["targets"][arch]["archive_member"] = "kandev/bin/agentctl"
                with self.assertRaisesRegex(ValueError, "does not match the reviewed"):
                    parse_manifest(changed_member)

                changed_source = _document()
                kandev = next(p for p in changed_source["packages"] if p["name"] == "kandev")
                del kandev["targets"][arch]["archive_member"]
                kandev["targets"][arch]["install_tree"] = "kandev"
                with self.assertRaisesRegex(ValueError, "must use its reviewed"):
                    parse_manifest(changed_source)

    def test_neovim_declares_the_runtime_conflict_and_replacement(self) -> None:
        by_name = {p.name: p for p in load_manifest().packages}
        self.assertEqual(by_name["neovim"].conflicts, "neovim-runtime")
        self.assertEqual(by_name["neovim"].replaces, "neovim-runtime")
        # Every package without a reviewed relation declares none.
        for name, recipe in RECIPES.items():
            if recipe.conflicts or recipe.replaces:
                continue
            with self.subTest(package=name):
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
