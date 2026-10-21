"""Independent final-review regressions and opt-in real-asset verification."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.manifest import load_manifest, parse_manifest, expected_debs


class RemovedSourceContractTest(unittest.TestCase):
    def test_asset_target_rejects_removed_source_build_field(self):
        data = json.loads(Path("packaging/manifest.json").read_text())
        for name in ("cargo-tauri", "zellij", "bun"):
            with self.subTest(package=name):
                altered = copy.deepcopy(data)
                entry = next(p for p in altered["packages"] if p["name"] == name)
                entry["targets"]["amd64"]["source_build"] = {"repository": "https://github.com/tauri-apps/tauri", "tag": "tauri-cli-v2.12.2"}
                with self.assertRaises(ValueError):
                    parse_manifest(altered)

    def test_cli_tag_discovery_reaches_beyond_unrelated_first_page(self):
        from scripts.upstream import latest_stable
        from scripts.upstream_assets import release_tag_api_url
        pkg = next(p for p in load_manifest().packages if p.name == "cargo-tauri")
        target = pkg.targets["amd64"]
        tag = "tauri-cli-v2.12.2"

        class PagedClient:
            def get_json(self, url):
                if url == release_tag_api_url("tauri-apps/tauri", tag):
                    return {"tag_name": tag, "draft": False, "prerelease": False, "assets": [{"name": "cargo-tauri-x86_64-unknown-linux-gnu.tgz", "browser_download_url": target.url, "digest": "sha256:" + target.sha256}]}
                if "page=2" in url:
                    return [{"name": tag}]
                if "/tags?" in url:
                    return [{"name": f"tauri-v2.0.{n}"} for n in range(100)]
                raise AssertionError(url)

        _, selected, assets = latest_stable(PagedClient(), "cargo-tauri")
        self.assertEqual(selected, tag)
        self.assertEqual(set(assets), {"amd64"})

    def test_arm_native_smoke_never_probes_absent_cargo_tauri(self):
        from scripts.smoke_runtime import run_installed
        with tempfile.TemporaryDirectory(prefix="apt-review-arm-") as temp:
            with patch.dict(os.environ, {"TMPDIR": temp}), patch("scripts.smoke_runtime.run_package", return_value=[]) as probe:
                run_installed(load_manifest(), "arm64")
        names = [call.args[0].name for call in probe.call_args_list]
        self.assertEqual(len(names), 11)
        self.assertNotIn("cargo-tauri", names)
        self.assertIn("kandev", names)
        self.assertIn("bun", names)


@unittest.skipUnless(os.environ.get("APT_REVIEW_REAL") == "1", "opt-in real official cached assets")
class RealArtifactReviewTest(unittest.TestCase):
    def test_signed_21_old20_repeat_amd(self):
        from scripts.build import archive_cache_name, build_all
        from scripts.common import sha256_file
        from scripts.release_build import build_release
        from scripts.smoke import check_pool
        from scripts.verify import verify_tree

        self.assertGreater(shutil.disk_usage(".").free, 10 * 1024**3)
        output = Path("dist/simplification/review-r1")
        self.assertFalse(output.exists(), "review output must be fresh")
        manifest = load_manifest()
        baseline = {p.name: sha256_file(p) for p in Path("dist/repo/pool").glob("*.deb")}
        self.assertEqual(len(baseline), 20)
        cache = Path("dist/cache/upstream")
        for pkg in manifest.packages:
            for arch, target in pkg.targets.items():
                archive = cache / archive_cache_name(pkg, arch)
                self.assertEqual(sha256_file(archive), target.sha256)
        with tempfile.TemporaryDirectory(prefix="apt-review-key-") as temp:
            home = Path(temp)
            home.chmod(0o700)
            env = dict(os.environ, GNUPGHOME=str(home))
            subprocess.run(["gpg", "--batch", "--pinentry-mode", "loopback", "--passphrase", "", "--quick-generate-key", "apt review <review@invalid.local>", "rsa2048", "sign", "0"], env=env, check=True, capture_output=True)
            listing = subprocess.check_output(["gpg", "--batch", "--with-colons", "--list-keys"], env=env, text=True)
            fingerprint = next(line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:"))
            private = home / "ephemeral.asc"
            private.write_bytes(subprocess.check_output(["gpg", "--batch", "--armor", "--export-secret-keys"], env=env))
            built = build_release(output_root=output, release_id="review-r1", cache_dir=cache, signing_key_file=private, key_id=fingerprint)
            self.assertEqual({p.name for p in built}, expected_debs(manifest))
            findings = verify_tree(output, expected_fingerprint=fingerprint)
            print("SIGNED21", findings)
        self.assertEqual(len(list((output / "pool").glob("*_amd64.deb"))), 11)
        self.assertEqual(len(list((output / "pool").glob("*_arm64.deb"))), 10)
        for name, digest in baseline.items():
            self.assertEqual(sha256_file(output / "pool" / name), digest, name)
            self.assertEqual(sha256_file(Path("dist/repo/pool") / name), digest)
        print("OLD20_BYTE_IDENTICAL", json.dumps(baseline, sort_keys=True))
        repeat = build_all(manifest, pool_dir=output / "repeat", cache_dir=cache, packages=("cargo-tauri",))
        self.assertEqual(len(repeat), 1)
        cargo = output / "pool" / repeat[0].name
        self.assertEqual(sha256_file(cargo), sha256_file(repeat[0]))
        print("REPEAT_AMD", cargo.name, sha256_file(cargo))
        print("STATIC_POOL", check_pool(manifest, output / "pool", ("amd64", "arm64")))
        print("CARGO_METADATA", subprocess.check_output(["dpkg-deb", "-f", str(cargo)], text=True))
        self.assertLess(sum(p.stat().st_size for p in output.rglob("*") if p.is_file()), 1024**3)
        self.assertGreater(shutil.disk_usage(".").free, 10 * 1024**3)


if __name__ == "__main__":
    unittest.main()
