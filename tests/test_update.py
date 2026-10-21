"""Update planning and the dry-run CLI contract.

Plans are compared against a validated baseline plus the committed catalog, so
the tests drive real manifest documents through the merge and overlay and assert
on versions, digests, members, and refusal messages rather than on internal
state.
"""

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.catalog import KNOWN_PACKAGES, repo_of, source_for, tag_for
from scripts.manifest import default_manifest_path
from scripts.release_manifest import validated_document
from scripts.update_plan import build_plan, merge_catalog
from scripts.upstream import Candidate
from scripts.upstream_assets import (
    asset_name, license_urls_for, release_download_url,
)
from tests.release_fixtures import (
    historical_cargo_baseline_data,
    published_manifest,
    published_manifest_data,
)
from tests.helpers import move_package_version


def committed_sha(package_name: str, arch: str) -> str:
    """The digest the committed manifest pins, so an unchanged candidate is real."""
    _, committed = validated_document(default_manifest_path())
    pkg = next(p for p in committed.packages if p.name == package_name)
    return pkg.targets[arch].sha256


def candidate(name: str, version: str, *, sha: str | None = None) -> Candidate:
    """Build a candidate whose URLs satisfy the same official-origin rules as discovery.

    By default each architecture keeps the digest the committed manifest already
    pins, which is what an unchanged upstream release looks like; pass `sha` to
    model a rebuilt archive. The URLs follow the package's own reviewed source,
    so a Codeberg package never carries a GitHub origin.
    """
    from scripts.catalog import arches_for

    repo = repo_of(name)
    tag = tag_for(name, version)
    source = source_for(name)
    return Candidate(
        name=name,
        repo=repo,
        tag=tag,
        version=version,
        license_url=license_urls_for(name, tag)[0],
        targets={
            arch: {
                "url": release_download_url(
                    repo, tag, asset_name(name, version, arch), source=source
                ),
                "sha256": sha or committed_sha(name, arch),
            }
            for arch in arches_for(name)
        },
        extra_license_urls=license_urls_for(name, tag)[1:],
    )


def baseline_document() -> dict[str, object]:
    """The published baseline: the catalog subset the current release carries.

    The committed catalog is wider than what production has published, so the
    planning tests start from the released subset and let the merge add the rest.
    """
    return published_manifest_data()


def catalog_document() -> dict[str, object]:
    return validated_document(default_manifest_path())[0]


def catalog_names() -> tuple[str, ...]:
    """The package names the committed catalog carries, in document order."""
    return tuple(
        str(entry["name"]) for entry in catalog_document()["packages"]  # type: ignore[index]
    )


def baseline_names() -> tuple[str, ...]:
    """The package names the published baseline fixture carries."""
    return tuple(
        str(entry["name"]) for entry in baseline_document()["packages"]  # type: ignore[index]
    )


def added_names() -> tuple[str, ...]:
    """The catalog packages the published baseline does not carry yet, sorted."""
    return tuple(sorted(set(catalog_names()) - set(baseline_names())))


def merge_names(baseline: dict[str, object], catalog: dict[str, object]) -> list[str]:
    """The names a merge must produce: the baseline's, then the catalog's new ones."""
    baseline_order = [str(entry["name"]) for entry in baseline["packages"]]  # type: ignore[index]
    catalog_order = [str(entry["name"]) for entry in catalog["packages"]]  # type: ignore[index]
    return baseline_order + [name for name in catalog_order if name not in baseline_order]


def candidate_pairs(data: dict[str, object]) -> dict[str, Candidate]:
    """One unchanged candidate per package in a manifest document."""
    return {
        str(entry["name"]): candidate(str(entry["name"]), str(entry["version"]))
        for entry in data["packages"]  # type: ignore[index]
    }


def _catalog_candidates(**overrides: str) -> dict[str, Candidate]:
    """One candidate per catalog package at its pinned version."""
    versions = {pkg.name: pkg.version for pkg in published_manifest().packages}
    _, committed = validated_document(default_manifest_path())
    versions.update({pkg.name: pkg.version for pkg in committed.packages})
    versions.update(overrides)
    return {name: candidate(name, version) for name, version in sorted(versions.items())}


class MergeCatalogTest(unittest.TestCase):
    def test_only_absent_packages_are_added(self) -> None:
        baseline = baseline_document()
        catalog = catalog_document()
        merged = merge_catalog(baseline, catalog)
        self.assertEqual(
            [entry["name"] for entry in merged],
            merge_names(baseline, catalog),
        )

    def test_a_newer_published_version_survives_the_merge(self) -> None:
        baseline = baseline_document()
        for entry in baseline["packages"]:  # type: ignore[index]
            if entry["name"] == "starship":
                entry["version"] = "1.27.0"
        merged = merge_catalog(baseline, catalog_document())
        starship = next(e for e in merged if e["name"] == "starship")
        self.assertEqual(starship["version"], "1.27.0")
        self.assertEqual(
            [entry["name"] for entry in merged],
            merge_names(baseline, catalog_document()),
        )

    def test_the_committed_catalog_never_drops_a_published_package(self) -> None:
        """A catalog that lost a name cannot delete what is already published."""
        trimmed = catalog_document()
        trimmed["packages"] = [  # type: ignore[index]
            e for e in trimmed["packages"]  # type: ignore[index]
            if e["name"] != "zellij"
        ]
        merged = merge_catalog(baseline_document(), trimmed)
        self.assertEqual(
            [entry["name"] for entry in merged],
            merge_names(baseline_document(), trimmed),
        )
        self.assertIn("zellij", [e["name"] for e in merged])


class PlanTest(unittest.TestCase):
    def test_unchanged_candidates_produce_an_empty_plan(self) -> None:
        plan = build_plan(
            baseline_release_id="20260102-030405",
            baseline_data=baseline_document(),
            catalog_data=baseline_document(),
            candidates=candidate_pairs(baseline_document()),
        )
        self.assertTrue(plan.is_noop)
        self.assertFalse(plan.is_bootstrap)
        self.assertEqual(plan.changed, ())

    def test_a_wider_catalog_adds_only_the_absent_packages(self) -> None:
        plan = build_plan(
            baseline_release_id="20260102-030405",
            baseline_data=baseline_document(),
            catalog_data=catalog_document(),
            candidates=_catalog_candidates(),
        )
        # Every package sits at its pinned version, but the packages the
        # published baseline does not carry still have to be built.
        additions = added_names()
        self.assertEqual(plan.changed, additions)
        self.assertEqual(plan.added, additions)
        self.assertEqual(
            sorted(p.name for p in plan.manifest.packages),
            sorted(catalog_names()),
        )
        by_name = {p.name: p for p in plan.manifest.packages}
        # An added package keeps its catalog member and dependency profile.
        self.assertEqual(
            by_name["git-delta"].targets["arm64"].archive_member,
            "delta-0.20.1-aarch64-unknown-linux-gnu/delta",
        )
        self.assertEqual(
            by_name["git-delta"].targets["arm64"].depends,
            "libc6 (>= 2.18), libgcc-s1",
        )
        self.assertEqual(by_name["rustfs-cli"].targets["amd64"].binary, "rc")
        self.assertEqual(by_name["kandev"].targets["amd64"].archive_member, "kandev/bin/kandev")
        self.assertEqual(by_name["kandev"].targets["arm64"].depends, "libc6 (>= 2.34)")

    def test_one_changed_package_moves_only_its_own_fields(self) -> None:
        plan = build_plan(
            baseline_release_id="20260102-030405",
            baseline_data=baseline_document(),
            catalog_data=catalog_document(),
            candidates=_catalog_candidates(starship="1.27.0", sccache="0.19.0"),
        )
        additions = added_names()
        self.assertEqual(plan.changed, tuple(sorted(additions + ("starship",))))
        self.assertEqual(plan.added, additions)
        by_name = {p.name: p for p in plan.manifest.packages}
        starship = by_name["starship"]
        self.assertTrue(starship.license_url.endswith("/v1.27.0/LICENSE"))
        self.assertTrue(
            starship.targets["amd64"].url.endswith(
                "/v1.27.0/starship-x86_64-unknown-linux-musl.tar.gz"
            )
        )
        # A version bump recomputes the nested member instead of reusing it.
        self.assertEqual(
            by_name["sccache"].targets["amd64"].archive_member,
            "sccache-v0.19.0-x86_64-unknown-linux-musl/sccache",
        )
        self.assertEqual(
            by_name["sccache"].targets["arm64"].archive_member,
            "sccache-v0.19.0-aarch64-unknown-linux-musl/sccache",
        )
        # The unchanged package keeps its pinned digests byte for byte.
        zellij = by_name["zellij"]
        self.assertEqual(zellij.targets["arm64"].sha256, committed_sha("zellij", "arm64"))

    def test_a_bumped_delta_keeps_its_arm64_dependency(self) -> None:
        plan = build_plan(
            baseline_release_id="20260102-030405",
            baseline_data=catalog_document(),
            catalog_data=catalog_document(),
            candidates=_catalog_candidates(**{"git-delta": "0.21.0"}),
        )
        delta = next(p for p in plan.manifest.packages if p.name == "git-delta")
        self.assertEqual(delta.version, "0.21.0")
        self.assertEqual(
            delta.targets["arm64"].archive_member,
            "delta-0.21.0-aarch64-unknown-linux-gnu/delta",
        )
        self.assertEqual(delta.targets["arm64"].depends, "libc6 (>= 2.18), libgcc-s1")
        self.assertEqual(delta.targets["amd64"].depends, "")

    def test_a_published_version_newer_than_the_catalog_is_never_replaced(self) -> None:
        """The catalog pins starship 1.26.0; a published 1.27.0 must survive."""
        baseline = baseline_document()
        for entry in baseline["packages"]:  # type: ignore[index]
            if entry["name"] == "starship":
                move_package_version(entry, "1.27.0")
        plan = build_plan(
            baseline_release_id="20260102-030405",
            baseline_data=baseline,
            catalog_data=catalog_document(),
            candidates=_catalog_candidates(starship="1.27.0"),
        )
        starship = next(p for p in plan.manifest.packages if p.name == "starship")
        self.assertEqual(starship.version, "1.27.0")
        self.assertNotIn("starship", plan.changed)
        # Only the catalog packages the published baseline lacks remain to be
        # built.
        self.assertEqual(plan.changed, added_names())

    def test_an_older_candidate_for_a_newer_published_version_is_refused(self) -> None:
        baseline = baseline_document()
        for entry in baseline["packages"]:  # type: ignore[index]
            if entry["name"] == "starship":
                move_package_version(entry, "1.27.0")
        with self.assertRaises(ValueError) as ctx:
            build_plan(
                baseline_release_id="20260102-030405",
                baseline_data=baseline,
                catalog_data=catalog_document(),
                candidates=_catalog_candidates(starship="1.26.0"),
            )
        self.assertIn("older than the published baseline", str(ctx.exception))

    def test_downgrade_and_missing_candidate_are_refused(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            build_plan(
                baseline_release_id="r",
                baseline_data=baseline_document(),
                catalog_data=catalog_document(),
                candidates=_catalog_candidates(starship="1.25.0"),
            )
        self.assertIn("older than the published baseline", str(ctx.exception))
        with self.assertRaises(ValueError):
            build_plan(
                baseline_release_id="r",
                baseline_data=baseline_document(),
                catalog_data=catalog_document(),
                candidates={},
            )

    def test_same_version_with_a_changed_digest_is_refused(self) -> None:
        """A republished tag is not an unchanged release: it must stop, not no-op."""
        versions = {pkg.name: pkg.version for pkg in published_manifest().packages}
        bumped = {
            n: candidate(n, v, sha="cd" * 32) for n, v in sorted(versions.items())
        }
        with self.assertRaises(ValueError) as ctx:
            build_plan(
                baseline_release_id="20260102-030405",
                baseline_data=baseline_document(),
                catalog_data=baseline_document(),
                candidates=bumped,
            )
        message = str(ctx.exception)
        self.assertIn("immutable", message)
        self.assertIn("amd64", message)

        # The published digest itself must stay a clean no-op.
        plan = build_plan(
            baseline_release_id="20260102-030405",
            baseline_data=baseline_document(),
            catalog_data=baseline_document(),
            candidates={n: candidate(n, v) for n, v in sorted(versions.items())},
        )
        self.assertTrue(plan.is_noop)

    def test_bootstrap_plan_is_marked_and_keeps_every_package(self) -> None:
        plan = build_plan(
            baseline_release_id=None,
            baseline_data=baseline_document(),
            catalog_data=catalog_document(),
            candidates=_catalog_candidates(starship="1.26.0", zellij="0.46.0"),
        )
        self.assertTrue(plan.is_bootstrap)
        self.assertIn("zellij", plan.changed)

    def test_unexpected_license_in_the_baseline_is_refused(self) -> None:
        data = baseline_document()
        for entry in data["packages"]:  # type: ignore[index]
            if entry["name"] == "starship":
                entry["license"] = "GPL-3.0"
        with self.assertRaises(ValueError) as ctx:
            build_plan(
                baseline_release_id="r",
                baseline_data=data,
                catalog_data=catalog_document(),
                candidates=_catalog_candidates(starship="1.27.0"),
            )
        self.assertIn("license", str(ctx.exception))


class SameVersionArchitectureExpansionTest(unittest.TestCase):
    """A published amd64-only baseline gains the reviewed arm64 target in place."""

    def _baseline(self) -> dict[str, object]:
        return historical_cargo_baseline_data()

    def _cargo_entry(self, data: dict[str, object]) -> dict[str, object]:
        packages = data["packages"]
        assert isinstance(packages, list)
        return next(e for e in packages if e.get("name") == "cargo-tauri")

    def _plan(self, *, baseline: dict[str, object] | None = None, candidates=None):
        return build_plan(
            baseline_release_id="20260102-030405",
            baseline_data=baseline if baseline is not None else self._baseline(),
            catalog_data=catalog_document(),
            candidates=_catalog_candidates() if candidates is None else candidates,
        )

    def test_only_the_new_target_is_planned_for_the_same_version(self) -> None:
        plan = self._plan()
        self.assertEqual(plan.changed, ("cargo-tauri",))
        self.assertEqual(plan.added, ())
        cargo = next(p for p in plan.manifest.packages if p.name == "cargo-tauri")
        self.assertEqual(cargo.version, "2.12.3")
        self.assertEqual(set(cargo.targets), {"amd64", "arm64"})

    def test_the_published_amd64_target_is_preserved_field_by_field(self) -> None:
        baseline_entry = self._cargo_entry(self._baseline())
        baseline_amd64 = baseline_entry["targets"]["amd64"]
        cargo = next(
            p for p in self._plan().manifest.packages if p.name == "cargo-tauri"
        )
        published = cargo.targets["amd64"]
        self.assertEqual(published.url, baseline_amd64["url"])
        self.assertEqual(published.sha256, baseline_amd64["sha256"])
        self.assertEqual(published.archive_member, baseline_amd64["archive_member"])
        self.assertEqual(published.binary, baseline_amd64["binary"])
        self.assertEqual(published.depends, baseline_amd64["depends"])
        self.assertIn("liblzma5 (>= 5.8.1)", published.depends)
        # Only the new arm64 target carries the reviewed arm64 runtime.
        self.assertEqual(
            cargo.targets["arm64"].depends,
            "cargo, libc6 (>= 2.18), libgcc-s1 (>= 14.2.0)",
        )

    def test_a_changed_published_amd64_field_is_refused(self) -> None:
        cases = {
            "sha256": "cd" * 32,
            "archive_member": "other-member",
            "binary": "other-binary",
            "depends": "libc6 (>= 2.34)",
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                baseline = self._baseline()
                self._cargo_entry(baseline)["targets"]["amd64"][field] = value
                with self.assertRaises(ValueError) as ctx:
                    self._plan(baseline=baseline)
                self.assertIn("immutable", str(ctx.exception))

    def test_a_published_target_from_another_asset_is_refused(self) -> None:
        baseline = self._baseline()
        self._cargo_entry(baseline)["targets"]["amd64"]["url"] = (
            "https://github.com/tauri-apps/tauri/releases/download/"
            "tauri-cli-v2.12.3/cargo-tauri-aarch64-unknown-linux-gnu.tgz"
        )
        with self.assertRaises(ValueError) as ctx:
            self._plan(baseline=baseline)
        self.assertIn("official asset", str(ctx.exception))

    def test_a_missing_arm_asset_stops_the_expansion(self) -> None:
        from dataclasses import replace as dataclass_replace

        candidates = _catalog_candidates()
        original = candidates["cargo-tauri"]
        candidates["cargo-tauri"] = dataclass_replace(
            original,
            targets={
                arch: target
                for arch, target in original.targets.items()
                if arch != "arm64"
            },
        )
        with self.assertRaises(ValueError) as ctx:
            self._plan(candidates=candidates)
        self.assertIn("arm64", str(ctx.exception))
        self.assertIn("discovered", str(ctx.exception))


class CodebergPlanTest(unittest.TestCase):
    """A package from a source without release digests still plans and freezes."""

    def test_a_new_codeberg_version_moves_only_its_own_fields(self) -> None:
        plan = build_plan(
            baseline_release_id="20260102-030405",
            baseline_data=baseline_document(),
            catalog_data=catalog_document(),
            candidates=_catalog_candidates(**{"forgejo-cli": "0.8.0"}),
        )
        pkg = next(p for p in plan.manifest.packages if p.name == "forgejo-cli")
        self.assertEqual(pkg.version, "0.8.0")
        self.assertIn("forgejo-cli", plan.changed)
        self.assertEqual(
            pkg.targets["amd64"].url,
            "https://codeberg.org/forgejo-contrib/forgejo-cli/releases/download/"
            "v0.8.0/forgejo-cli-x86_64-linux.tar.gz",
        )
        self.assertEqual(
            pkg.license_url,
            "https://codeberg.org/forgejo-contrib/forgejo-cli/raw/tag/v0.8.0/LICENSE-MIT",
        )
        self.assertEqual(pkg.targets["amd64"].archive_member, "fj")
        self.assertEqual(pkg.targets["arm64"].depends, "libc6 (>= 2.39), libgcc-s1")

    def test_a_republished_codeberg_tag_is_refused(self) -> None:
        """The same version with different bytes is a republished tag, not a no-op."""
        candidates = _catalog_candidates()
        moved = candidate("forgejo-cli", "0.7.0", sha="cd" * 32)
        candidates["forgejo-cli"] = moved
        with self.assertRaises(ValueError) as ctx:
            build_plan(
                baseline_release_id="20260102-030405",
                baseline_data=catalog_document(),
                catalog_data=catalog_document(),
                candidates=candidates,
            )
        message = str(ctx.exception)
        self.assertIn("immutable", message)
        self.assertIn("amd64", message)


class DiscoverExactTagTest(unittest.TestCase):
    """The updater derives cargo-tauri from its exact stable tag, not repo latest."""

    def test_exact_tag_discovery_supplies_both_reviewed_assets(self) -> None:
        from scripts import update as update_module

        repo = "tauri-apps/tauri"
        tag = "tauri-cli-v2.12.3"
        assets = {
            "amd64": ("cargo-tauri-x86_64-unknown-linux-gnu.tgz", "a" * 64),
            "arm64": ("cargo-tauri-aarch64-unknown-linux-gnu.tgz", "b" * 64),
        }
        license_mit = Path("packaging/licenses/cargo-tauri/LICENSE.txt").read_bytes()
        license_apache = Path(
            "packaging/licenses/cargo-tauri/LICENSE-APACHE-2.0"
        ).read_bytes()

        class ExactTagClient:
            """Tags list plus tag-specific release and both licenses."""

            def get_json(self, url: str) -> object:
                if url.endswith("/releases/latest"):
                    raise AssertionError(
                        "canonical CLI discovery must not use repository-wide latest"
                    )
                if "/tags?" in url:
                    if "page=2" in url:
                        return [{"name": tag}]
                    if "&page=" in url:
                        return []
                    first = [{"name": "tauri-v2.10.0"}, {"name": tag}]
                    if len(first) < 100:
                        return first
                    return first
                if f"/releases/tags/{tag}" in url:
                    return {
                        "tag_name": tag, "draft": False, "prerelease": False,
                        "assets": [
                            {
                                "name": asset,
                                "browser_download_url": (
                                    f"https://github.com/{repo}/releases/download/{tag}/{asset}"
                                ),
                                "digest": f"sha256:{digest}",
                            }
                            for asset, digest in assets.values()
                        ],
                    }
                raise AssertionError(f"unexpected JSON URL: {url}")

            def get_bytes(self, url: str, *, max_bytes: int) -> bytes:
                if url.endswith("/LICENSE-MIT"):
                    return license_mit
                if url.endswith("/LICENSE-APACHE-2.0"):
                    return license_apache
                raise AssertionError(f"unexpected bytes URL: {url}")

        with mock.patch.object(
            update_module, "RECIPES", {"cargo-tauri": object()}
        ):
            result = update_module._discover(ExactTagClient())
        candidate = result["cargo-tauri"]
        self.assertEqual(candidate.version, "2.12.3")
        self.assertEqual(candidate.tag, tag)
        self.assertEqual(sorted(candidate.targets), ["amd64", "arm64"])

    def test_exact_tag_discovery_reaches_beyond_unrelated_first_page(self) -> None:
        """A full first page of unrelated tags never hides the CLI release."""
        from scripts import update as update_module

        repo = "tauri-apps/tauri"
        tag = "tauri-cli-v2.12.3"
        assets = {
            "amd64": ("cargo-tauri-x86_64-unknown-linux-gnu.tgz", "a" * 64),
            "arm64": ("cargo-tauri-aarch64-unknown-linux-gnu.tgz", "b" * 64),
        }
        license_mit = Path("packaging/licenses/cargo-tauri/LICENSE.txt").read_bytes()
        license_apache = Path(
            "packaging/licenses/cargo-tauri/LICENSE-APACHE-2.0"
        ).read_bytes()

        class PagedTagClient:
            def __init__(self) -> None:
                self.tags_requests: list[str] = []

            def get_json(self, url: str) -> object:
                if url.endswith("/releases/latest"):
                    raise AssertionError(
                        "canonical CLI discovery must not use repository-wide latest"
                    )
                if "/tags?" in url:
                    self.tags_requests.append(url)
                    if "page=2" in url:
                        return [{"name": tag}]
                    return [{"name": f"tauri-v2.0.{n}"} for n in range(100)]
                if f"/releases/tags/{tag}" in url:
                    return {
                        "tag_name": tag, "draft": False, "prerelease": False,
                        "assets": [
                            {
                                "name": asset,
                                "browser_download_url": (
                                    f"https://github.com/{repo}/releases/download/{tag}/{asset}"
                                ),
                                "digest": f"sha256:{digest}",
                            }
                            for asset, digest in assets.values()
                        ],
                    }
                raise AssertionError(f"unexpected JSON URL: {url}")

            def get_bytes(self, url: str, *, max_bytes: int) -> bytes:
                if url.endswith("/LICENSE-MIT"):
                    return license_mit
                if url.endswith("/LICENSE-APACHE-2.0"):
                    return license_apache
                raise AssertionError(f"unexpected bytes URL: {url}")

        client = PagedTagClient()
        with mock.patch.object(
            update_module, "RECIPES", {"cargo-tauri": object()}
        ):
            result = update_module._discover(client)
        candidate = result["cargo-tauri"]
        self.assertEqual(candidate.tag, tag)
        self.assertEqual(candidate.version, "2.12.3")
        self.assertEqual(sorted(candidate.targets), ["amd64", "arm64"])
        self.assertTrue(any("page=2" in url for url in client.tags_requests))


class CliTest(unittest.TestCase):
    def _run(self, argv: list[str], *, storage, candidates, fingerprint: str = "A" * 40) -> tuple[int, str]:
        from scripts import update as update_module

        out = io.StringIO()
        with mock.patch.object(update_module, "s3_source", return_value=storage), mock.patch.object(
            update_module, "_discover", return_value=candidates
        ), mock.patch.object(
            update_module, "_fingerprint", return_value=fingerprint
        ), mock.patch.dict(
            os.environ, {"APT_S3_BUCKET": ""}, clear=False
        ), contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            return update_module.main(argv), out.getvalue()

    def _storage(self, objects: dict[str, bytes]):
        from tests.release_fixtures import FakeStorage

        return FakeStorage(objects)

    def _published_storage(self, work: Path):
        """A real signed two-package release in an in-memory object store."""
        from tests.release_fixtures import build_release_objects
        from tests.helpers import make_gpg_home_with_key

        tmpdir, home, key_id = make_gpg_home_with_key()
        self.addCleanup(tmpdir.cleanup)
        storage = self._storage(
            build_release_objects(work / "published", key_home=home, key_id=key_id)
        )
        return storage, key_id

    def test_a_dry_run_plans_the_catalog_addition_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            storage, key_id = self._published_storage(root)
            code, output = self._run(
                ["--output", str(root / "out")],
                storage=storage, candidates=_catalog_candidates(), fingerprint=key_id,
            )
            self.assertEqual(code, 0, msg=output)
            expected = ", ".join(added_names())
            self.assertIn(f"adding to the catalog: {expected}", output)
            self.assertIn(f"would rebuild: {expected}", output)
            self.assertIn("dry run: no key imported", output)
            self.assertFalse((root / "out").exists())
            self.assertEqual([key for key in storage.reads if key.startswith("pool/")], [])

    def test_discovery_that_misses_a_catalog_package_stops_the_run(self) -> None:
        """A partial discovery must never quietly drop a catalog package."""
        with tempfile.TemporaryDirectory() as work:
            storage, key_id = self._published_storage(Path(work))
            code, output = self._run(
                ["--output", str(Path(work) / "out")],
                storage=storage,
                candidates={k: v for k, v in _catalog_candidates().items()
                            if k in {"starship", "zellij"}},
                fingerprint=key_id,
            )
            self.assertEqual(code, 1)
            self.assertIn("no stable upstream release discovered for git-delta", output)

    def test_fresh_storage_bootstraps_from_the_committed_manifest(self) -> None:
        code, output = self._run(
            [], storage=self._storage({}), candidates=_catalog_candidates()
        )
        self.assertEqual(code, 0, msg=output)
        self.assertIn("committed manifest (fresh storage)", output)
        self.assertIn("nothing to publish", output)

    def test_publish_without_bucket_is_refused(self) -> None:
        code, output = self._run(["--publish"], storage=self._storage({}), candidates={})
        self.assertEqual(code, 1)
        self.assertIn("requires --bucket", output)

    def test_untrusted_baseline_is_refused(self) -> None:
        from tests.release_fixtures import DeniedStorage

        code, output = self._run([], storage=DeniedStorage({}), candidates={})
        self.assertEqual(code, 1)
        self.assertIn("update refused", output)

    def test_missing_fingerprint_is_refused(self) -> None:
        from scripts import update as update_module
        from tests.release_fixtures import FakeStorage

        with mock.patch.dict(os.environ, {"APT_SIGNING_FINGERPRINT": ""}, clear=False):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                update_module._fingerprint()
        self.assertEqual(self._run([], storage=FakeStorage({}), candidates={})[0], 1)


if __name__ == "__main__":
    unittest.main()