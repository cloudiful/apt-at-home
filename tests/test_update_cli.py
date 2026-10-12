"""Update overlays retain every reviewed uv command and license source."""

import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from scripts.manifest import deb_filename
from scripts.release_verify import restore_metadata
from scripts.smoke_package import check_copyright, check_package
from scripts.storage import MAX_DEB_BYTES
from scripts.upstream_assets import license_urls_for
from scripts.update_build import apply_update
from scripts.update_plan import build_plan
from tests.helpers import FakeS3, make_gpg_home_with_key
from tests.release_fixtures import CATALOG_NAMES, build_release_objects
from tests.test_update import _catalog_candidates, candidate, catalog_document
from tests.test_update_build import Store, _export_private_key, _stub_archive


class UvUpdateOverlayTest(unittest.TestCase):
    def test_a_uv_upgrade_preserves_uvx_and_both_license_urls(self) -> None:
        manifest = catalog_document()
        candidates = _catalog_candidates(uv="0.14.0")

        plan = build_plan(
            baseline_release_id="published",
            baseline_data=manifest,
            catalog_data=manifest,
            candidates=candidates,
        )

        uv = next(pkg for pkg in plan.manifest.packages if pkg.name == "uv")
        target = uv.targets["arm64"]
        self.assertEqual(plan.changed, ("uv",))
        self.assertEqual(target.archive_member, "uv-aarch64-unknown-linux-gnu/uv")
        self.assertEqual(
            dict(target.additional_binaries),
            {"uv-aarch64-unknown-linux-gnu/uvx": "uvx"},
        )
        self.assertEqual(uv.extra_license_urls, license_urls_for("uv", "0.14.0")[1:])

    def test_uv_upgrade_rebuilds_two_commands_then_plans_a_zero_write_noop(self) -> None:
        tmpdir, home, key_id = make_gpg_home_with_key()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                store = Store(FakeS3())
                # Focused uv overlay: exclude the source-built package so this
                # asset-only upgrade stays hermetic without a producer artifact.
                # Full-catalog source builds are covered by the incremental and
                # update-build suites with fixture artifacts.
                uv_names = tuple(name for name in CATALOG_NAMES if name not in ("kandev", "cargo-tauri"))
                store.seed(build_release_objects(
                    root / "published", key_home=home, key_id=key_id, names=uv_names
                ))
                restored = restore_metadata(store, fingerprint=key_id)
                published_pool = {
                    path.name: store.get(f"pool/{path.name}", max_bytes=MAX_DEB_BYTES)
                    for path in (root / "published" / "pool").glob("*.deb")
                }
                uv_catalog = catalog_document()
                uv_catalog["packages"] = [
                    entry for entry in uv_catalog["packages"] if entry["name"] not in ("kandev", "cargo-tauri")
                ]
                uv_candidates = {
                    name: item for name, item in _catalog_candidates(uv="0.14.0").items()
                    if name not in ("kandev", "cargo-tauri")
                }
                plan = build_plan(
                    baseline_release_id=restored.release_id,
                    baseline_data=restored.manifest_data,
                    catalog_data=uv_catalog,
                    candidates=uv_candidates,
                )
                self.assertEqual(plan.changed, ("uv",))
                key_file = _export_private_key(home, root / "test-private.asc")
                output = root / "out"
                with _stub_archive():
                    result = apply_update(
                        plan=plan, release=restored, source=store, output_root=output,
                        cache_dir=output.parent / "cache" / "upstream",
                        release_id="uv-upgrade", bucket=Store.bucket,
                        fingerprint=key_id, signing_key_file=key_file, key_id=key_id,
                        client=store.client,
                    )

                self.assertEqual(
                    sorted(path.name for path in result.debs),
                    ["uv_0.14.0-1_amd64.deb", "uv_0.14.0-1_arm64.deb"],
                )
                pool = {path.name: path.read_bytes() for path in (output / "pool").glob("*.deb")}
                self.assertEqual(len(pool), 18)
                self.assertNotIn("uv_0.13.0-1_amd64.deb", pool)
                for name, payload in published_pool.items():
                    if not name.startswith("uv_"):
                        self.assertEqual(pool[name], payload, msg=name)

                uv = next(pkg for pkg in plan.manifest.packages if pkg.name == "uv")
                for arch in ("amd64", "arm64"):
                    check_package(uv, arch, manifest=plan.manifest, pool=output / "pool")
                    extracted_root = root / f"uv-upgraded-{arch}"
                    proc = subprocess.run(
                        ["dpkg-deb", "-x", str(output / "pool" / deb_filename(uv, arch, plan.manifest)), str(extracted_root)],
                        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                    )
                    self.assertEqual(proc.returncode, 0, msg=proc.stderr)
                    check_copyright(uv, extracted_root)

                writes = list(store.client.puts)
                follow_up = restore_metadata(store, fingerprint=key_id)
                noop = build_plan(
                    baseline_release_id=follow_up.release_id,
                    baseline_data=follow_up.manifest_data,
                    catalog_data=uv_catalog,
                    candidates=uv_candidates,
                )
                self.assertTrue(noop.is_noop)
                self.assertEqual(store.client.puts, writes)
        finally:
            tmpdir.cleanup()

    def test_same_version_supplemental_provenance_cannot_be_dropped(self) -> None:
        manifest = catalog_document()
        candidates = _catalog_candidates()
        candidates["uv"] = replace(candidate("uv", "0.13.0"), extra_license_urls=())

        with self.assertRaisesRegex(ValueError, "supplemental license sources changed"):
            build_plan(
                baseline_release_id="published",
                baseline_data=manifest,
                catalog_data=manifest,
                candidates=candidates,
            )


if __name__ == "__main__":
    unittest.main()
