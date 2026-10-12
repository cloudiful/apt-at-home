"""A same-version rebuild reuses the published deb, and only that deb.

`--reuse-published-source` is the one place a build is allowed to read the
published release, and everything here is about what it may conclude from that
read. The fixture is a real signed release whose bound manifest carries a realized
source target and whose pool holds the exact bytes that release shipped, so the
trust chain under test is the production one rather than a stand-in.

The cases are the ways reuse could go wrong: a manifest with no source target, a
baseline that never published one or published only a plan, an identity or commit
that moved under the same version, pool bytes that do not match the signed index,
a reuse without a bucket or a fingerprint, and a target asked to be both reused
and recompiled.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.build import build_all
from scripts.common import sha256_file
from scripts.fingerprint import primary_fingerprint_of_keyfile
from scripts.manifest import Manifest, parse_manifest
from scripts.metadata import (
    ARCHES,
    COMPONENT,
    SUITE,
    generate_release,
    write_gzip_deterministic,
)
from scripts.repo import main
from scripts.release_build import build_release
from scripts.release_manifest import (
    MANIFEST_NAME,
    manifest_binding_fields,
    write_manifest_document,
)
from scripts.release_verify import BaselineUnavailable
from scripts.sign import export_public_key, sign_release
from scripts.source_artifacts import reuse_published_source, select_source_artifacts
from scripts.source_build import (
    ArtifactProvenance,
    SourceIdentity,
    realize,
)
from scripts.source_build import document as document_of
from scripts.storage import ObjectNotFound
from tests.helpers import FakeS3, make_gpg_home_with_key, write_target_archive
from tests.test_source_artifacts import source_identity
from tests.test_source_build import source_manifest_document

BUCKET = "test-bucket"
VERSION = "2.12.2"
RELEASE_ID = "20260102-030405"
PACKAGE = "cargo-tauri"
ARCH = "arm64"
ARM64_DEB = f"{PACKAGE}_{VERSION}-1_arm64.deb"
AMD64_DEB = f"{PACKAGE}_{VERSION}-1_amd64.deb"
# The deb the published release shipped. It is never unpacked or executed: reuse
# copies these bytes, and the chain that authenticated them is the one under test.
PUBLISHED_ARM64 = b"published arm64 deb bytes\n" * 64
PUBLISHED_AMD64 = b"published amd64 deb bytes\n" * 64
ARTIFACT = ArtifactProvenance("c" * 64, 4096, 183)


def realized_document(
    identity: SourceIdentity | None = None, *, version: str = VERSION
) -> dict[str, object]:
    """A manifest document whose arm64 target is realized, as a published one is."""
    document = source_manifest_document((identity or source_identity()).document())
    document["packages"][0]["version"] = version
    document["packages"][0]["license_url"] = (  # type: ignore[index]
        "https://raw.githubusercontent.com/tauri-apps/tauri/tauri-cli-v2.12.2/LICENSE-MIT"
    )
    document["packages"][0]["extra_license_urls"] = [  # type: ignore[index]
        "https://raw.githubusercontent.com/tauri-apps/tauri/tauri-cli-v2.12.2/LICENSE-APACHE-2.0"
    ]
    document["packages"][0]["targets"][ARCH]["source_build"] = document_of(  # type: ignore[index]
        realize(identity or source_identity(), ARTIFACT)
    )
    return document


def asset_manifest_document() -> dict[str, object]:
    """An asset-only manifest: no source target to reuse.

    `cargo-tauri` arm64 cannot be an asset (upstream publishes none, so the
    catalog refuses one), so this uses a reviewed asset package instead. Reuse
    of `cargo-tauri` against it still refuses with "not built from reviewed
    source", which is the path under test.
    """
    import copy as _copy

    from scripts.release_manifest import load_manifest_data as _load_data
    from scripts.manifest import default_manifest_path as _default_path

    data = _copy.deepcopy(_load_data(_default_path()))
    packages = data.get("packages")
    assert isinstance(packages, list)
    data["packages"] = [e for e in packages if e.get("name") == "zellij"]
    assert len(data["packages"]) == 1
    return data  # type: ignore[return-value]


def planned_document(identity=None) -> dict[str, object]:
    """A planned source manifest as a valid production document.

    P1/P2 helper predates the production recipe and carries no
    `extra_license_urls` and a placeholder `license_url`; production parsing
    requires the reviewed tag-bound dual-license URLs, so add them here rather
    than weakening the validator.
    """
    from tests.test_source_build import source_manifest_document as _source_doc

    document = _source_doc(identity.document() if identity is not None else None)
    document["packages"][0]["license_url"] = (  # type: ignore[index]
        "https://raw.githubusercontent.com/tauri-apps/tauri/tauri-cli-v2.12.2/LICENSE-MIT"
    )
    document["packages"][0]["extra_license_urls"] = [  # type: ignore[index]
        "https://raw.githubusercontent.com/tauri-apps/tauri/tauri-cli-v2.12.2/LICENSE-APACHE-2.0"
    ]
    return document


class Store:
    """Adapt the FakeS3 dict to the bounded restore `Source` protocol."""

    def __init__(self, client: FakeS3) -> None:
        self.client = client

    def get(self, key: str, *, max_bytes: int) -> bytes:
        payload = self.client.objects.get((BUCKET, key))
        if payload is None:
            raise ObjectNotFound(key)
        if len(payload) > max_bytes:
            raise ValueError(f"object {key} exceeded the {max_bytes} byte limit")
        return payload


def _index(packages: Path, destination: Path, entries: list[tuple[str, str, Path]]) -> tuple[Path, Path]:
    """Write one architecture's index over already-known pool files.

    reprepro needs real `.deb` files, and a fixture for a package the catalog does
    not carry yet has none. The trust chain under test starts at the *signed*
    index, so the fixture writes the same fields reprepro would and lets
    `generate_release` sign them.
    """
    destination.mkdir(parents=True, exist_ok=True)
    stanzas = []
    for name, arch, deb in entries:
        raw = deb.read_bytes()
        stanzas.append(
            f"Package: {name}\nVersion: {VERSION}-1\nArchitecture: {arch}\n"
            f"Filename: pool/{deb.name}\nSize: {len(raw)}\n"
            f"MD5sum: {hashlib.md5(raw).hexdigest()}\n"
            f"SHA256: {hashlib.sha256(raw).hexdigest()}\n"
            f"Description: published fixture package\n"
        )
    packages.write_text("\n".join(stanzas), encoding="utf-8")
    compressed = packages.with_name("Packages.gz")
    write_gzip_deterministic(packages, compressed)
    return packages, compressed


class PublishedRelease:
    """A signed release with a realized source target and both pool entries."""

    def __init__(
        self, root: Path, *, key_home: Path, key_id: str,
        document: dict[str, object] | None = None,
        arm64_bytes: bytes = PUBLISHED_ARM64,
    ) -> None:
        self.document = document if document is not None else realized_document()
        pool = root / "pool"
        pool.mkdir(parents=True, exist_ok=True)
        (pool / AMD64_DEB).write_bytes(PUBLISHED_AMD64)
        (pool / ARM64_DEB).write_bytes(arm64_bytes)
        base = root / "releases" / RELEASE_ID
        dists = base / "dists" / SUITE
        inputs: list[tuple[Path, str]] = []
        for arch in ARCHES:
            deb = pool / f"{PACKAGE}_{VERSION}-1_{arch}.deb"
            generated, generated_gz = _index(
                dists / COMPONENT / f"binary-{arch}" / "Packages",
                dists / COMPONENT / f"binary-{arch}",
                [(PACKAGE, arch, deb)],
            )
            inputs.append((generated, f"{COMPONENT}/binary-{arch}/Packages"))
            inputs.append((generated_gz, f"{COMPONENT}/binary-{arch}/Packages.gz"))
        artifact = write_manifest_document(
            data=self.document, destination=base / MANIFEST_NAME
        )
        release = generate_release(
            output_root=root, release_id=RELEASE_ID, files=inputs,
            extra_fields=manifest_binding_fields(artifact),
        )
        previous = os.environ.get("GNUPGHOME")
        os.environ["GNUPGHOME"] = str(key_home)
        try:
            sign_release(release_file=release, key_id=key_id)
            export_public_key(homedir=key_home, key_id=key_id, dest=root / "key.asc")
        finally:
            if previous is None:
                os.environ.pop("GNUPGHOME", None)
            else:
                os.environ["GNUPGHOME"] = previous
        self.client = FakeS3()
        self.client.objects[(BUCKET, "current.json")] = (
            json.dumps({"release": RELEASE_ID}) + "\n"
        ).encode()
        self.client.objects[(BUCKET, "key.asc")] = (root / "key.asc").read_bytes()
        for path in sorted(base.rglob("*")):
            if path.is_file():
                self.client.objects[(BUCKET, str(path.relative_to(root)))] = path.read_bytes()
        for deb in sorted(pool.glob("*.deb")):
            self.client.objects[(BUCKET, f"pool/{deb.name}")] = deb.read_bytes()
        self.fingerprint = primary_fingerprint_of_keyfile(root / "key.asc")

    def source(self) -> Store:
        return Store(self.client)


class ReuseTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._key_tmp, cls._home, cls._key = make_gpg_home_with_key()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._key_tmp.cleanup()

    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)

    def published(self, **kwargs: object) -> PublishedRelease:
        return PublishedRelease(
            self.root / "published", key_home=self._home, key_id=self._key, **kwargs
        )  # type: ignore[arg-type]

    def reuse(
        self, release: PublishedRelease, document: dict[str, object],
        destination: Path | None = None,
    ):
        return reuse_published_source(
            source=release.source(), fingerprint=release.fingerprint,
            document=document, package=PACKAGE, arch=ARCH,
            destination=destination or self.root / "reuse",
        )

    def fake_asset_fetch(self, url: str, expected: str, dest: Path) -> Path:
        """Stand in for the upstream asset download; only that target downloads."""
        package = parse_manifest(
            json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
        ).packages[0]
        return write_target_archive(dest, package, "amd64", b"fake-cargo-tauri-amd64")

    @contextlib.contextmanager
    def stub_asset_target(self, index: bool = False):
        """Replace the asset architecture with a placeholder .deb.

        Staging any `cargo-tauri` package needs the reviewed recipe and its
        vendored licence texts, which the catalog phase adds. Every assertion
        here is about the *compiled* architecture, so the asset target is stubbed
        rather than staged, and nothing about the reuse path is touched.
        """
        stage = self.root / "stage"
        stage.mkdir(exist_ok=True)
        counter = {"index": 0}

        def build_deb(stage_dir: Path, destination: Path) -> None:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"placeholder asset deb\n")

        def generate_packages(pool: Path, arch_dir: Path, debs: list[Path]):
            arch_dir.mkdir(parents=True, exist_ok=True)
            counter["index"] += 1
            entries = [(path.name.split("_")[0], arch_dir.name.split("-")[-1], path)
                       for path in sorted(debs)]
            return _index(arch_dir / "Packages", arch_dir, entries)

        with mock.patch("scripts.build._stage_target", return_value=stage), \
                mock.patch("scripts.build.build_deb", side_effect=build_deb), \
                mock.patch("scripts.release_build.generate_packages",
                           side_effect=generate_packages):
            yield

    def write_manifest(self, document: dict[str, object]) -> Path:
        path = self.root / "manifest.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path


class PublishedReuseTest(ReuseTestCase):
    def test_the_published_deb_is_reused_with_its_exact_bytes(self) -> None:
        release = self.published()
        entry = self.reuse(release, realized_document())
        self.assertEqual(entry.filename, ARM64_DEB)
        self.assertEqual(entry.deb.read_bytes(), PUBLISHED_ARM64)
        self.assertEqual(entry.sha256, sha256_file(entry.deb))
        self.assertEqual(entry.size, len(PUBLISHED_ARM64))
        # The realized provenance carried is the published one, unchanged.
        self.assertEqual(entry.identity, source_identity())
        self.assertEqual(entry.artifact, ARTIFACT)
        self.assertEqual(entry.version, f"{VERSION}-1")

    def test_a_moved_lockfile_is_refused_before_anything_is_written(self) -> None:
        release = self.published()
        identity = source_identity()
        moved = source_identity(
            lockfile=type(identity.lockfile)("Cargo.lock", "b" * 40, 4096, "d" * 64)
        )
        with self.assertRaisesRegex(ValueError, "a published release is immutable"):
            self.reuse(release, realized_document(moved))
        self.assertFalse((self.root / "reuse").exists())

    def test_a_moved_commit_under_the_same_version_is_refused(self) -> None:
        release = self.published()
        with self.assertRaisesRegex(ValueError, "a published release is immutable"):
            self.reuse(release, realized_document(source_identity(commit="e" * 40)))

    def test_a_manifest_version_that_is_only_a_tag_suffix_is_refused(self) -> None:
        """The reuse path validates the manifest against the pinned canonical tag.

        The same suffix hole the artifact consumer closed is closed here: a
        manifest claiming `12.2` is not the `2.12.2` release its own pinned
        identity names, so it is a malformed document rather than a reuse.
        """
        release = self.published()
        for rejected in ("12.2", "2.12", "2.12.2-rc1"):
            with self.subTest(version=rejected):
                with self.assertRaises(ValueError):
                    self.reuse(release, realized_document(version=rejected))

    def test_the_true_pinned_version_is_the_one_that_reuses(self) -> None:
        """The positive control: the complete canonical version still reuses."""
        release = self.published()
        entry = self.reuse(release, realized_document(version=VERSION))
        self.assertEqual(entry.deb.read_bytes(), PUBLISHED_ARM64)
        self.assertEqual(entry.identity.tag, f"tauri-cli-v{VERSION}")

    def test_a_baseline_that_never_published_this_package_needs_a_compilation(self) -> None:
        release = self.published()
        release.client.objects = {
            key: value for key, value in release.client.objects.items()
            if not key[1].endswith(ARM64_DEB)
        }
        with self.assertRaises(BaselineUnavailable):
            self.reuse(release, realized_document())

    def test_a_published_target_that_is_still_a_plan_needs_a_compilation(self) -> None:
        release = self.published(document=planned_document(source_identity()))
        with self.assertRaisesRegex(BaselineUnavailable, "carries no measured artifact"):
            self.reuse(release, realized_document())

    def test_pool_bytes_that_do_not_match_the_signed_index_are_refused(self) -> None:
        release = self.published()
        release.client.objects[(BUCKET, f"pool/{ARM64_DEB}")] = PUBLISHED_ARM64 + b"injected"
        with self.assertRaisesRegex(ValueError, "does not match the signed Packages entry"):
            self.reuse(release, realized_document())

    def test_a_tampered_baseline_is_refused_by_the_existing_chain(self) -> None:
        release = self.published()
        release.client.objects[(BUCKET, f"releases/{RELEASE_ID}/{MANIFEST_NAME}")] = json.dumps(
            planned_document()
        ).encode()
        with self.assertRaises(ValueError):
            self.reuse(release, realized_document())

    def test_a_manifest_without_a_source_target_cannot_reuse(self) -> None:
        release = self.published()
        with self.assertRaisesRegex(ValueError, "not built from reviewed source"):
            self.reuse(release, asset_manifest_document())

    def test_only_the_reviewed_source_target_may_be_reused(self) -> None:
        release = self.published()
        with self.assertRaisesRegex(ValueError, "may not be built from source"):
            reuse_published_source(
                source=release.source(), fingerprint=release.fingerprint,
                document=realized_document(), package=PACKAGE, arch="amd64",
                destination=self.root / "reuse",
            )


class BuildWithReuseTest(ReuseTestCase):
    """`build_all` installs the reused bytes, and stages nothing for them."""

    def build(self, manifest: Manifest, *, fetch: mock.MagicMock | None = None,
              **kwargs) -> list[Path]:
        with mock.patch(
            "scripts.build.fetch_verified", side_effect=self.fake_asset_fetch
        ) as recorded, self.stub_asset_target():
            outputs = build_all(
                manifest, pool_dir=self.root / "pool",
                cache_dir=self.root / "cache", **kwargs,
            )
        if fetch is not None:
            fetch.extend(recorded.call_args_list)
        return outputs

    def test_a_reused_target_is_copied_never_staged_or_fetched(self) -> None:
        release = self.published()
        document = realized_document()
        entry = self.reuse(release, document)
        self.write_manifest(document)
        downloads: list[object] = []
        outputs = self.build(
            parse_manifest(document), fetch=downloads,
            reused_sources={(PACKAGE, ARCH): entry},
        )
        # Only the asset target downloads; the compiled architecture is a copy.
        self.assertEqual(len(downloads), 1)
        self.assertEqual(sorted(path.name for path in outputs), [AMD64_DEB, ARM64_DEB])
        self.assertEqual((self.root / "pool" / ARM64_DEB).read_bytes(), PUBLISHED_ARM64)
        # And it is byte-identical to what the published release shipped.
        self.assertEqual(
            (self.root / "pool" / ARM64_DEB).read_bytes(),
            (self.root / "published" / "pool" / ARM64_DEB).read_bytes(),
        )

    def test_the_same_target_cannot_be_both_reused_and_recompiled(self) -> None:
        release = self.published()
        entry = self.reuse(release, realized_document())
        self.write_manifest(realized_document())
        with self.assertRaisesRegex(ValueError, "one target cannot be both"):
            self.build(
                parse_manifest(realized_document()),
                source_artifacts={(PACKAGE, ARCH): object()},  # type: ignore[dict-item]
                reused_sources={(PACKAGE, ARCH): entry},
            )

    def test_a_reused_entry_for_another_target_is_refused(self) -> None:
        release = self.published()
        entry = self.reuse(release, realized_document())
        self.write_manifest(planned_document())
        with self.assertRaisesRegex(ValueError, "is not a source target of this manifest"):
            self.build(
                parse_manifest(planned_document()),
                reused_sources={("zellij", ARCH): entry},
            )

    def test_reuse_does_not_open_the_artifact_selection_path(self) -> None:
        # Reuse and a compiled artifact are separate routes into a source target:
        # the artifact path still refuses when it has nothing to select.
        manifest = parse_manifest(realized_document())
        target = manifest.packages[0].targets[ARCH]
        with self.assertRaisesRegex(ValueError, "needs its compiled artifact"):
            select_source_artifacts({}, PACKAGE, ARCH, target)

    def test_tampered_reuse_bytes_never_reach_the_pool(self) -> None:
        release = self.published()
        entry = self.reuse(release, realized_document())
        self.write_manifest(realized_document())
        entry.deb.write_bytes(b"different bytes entirely")
        with self.assertRaisesRegex(ValueError, "no longer matches the signed entry"):
            self.build(
                parse_manifest(realized_document()),
                reused_sources={(PACKAGE, ARCH): entry},
            )
        self.assertFalse((self.root / "pool" / ARM64_DEB).exists())

    def test_an_existing_pool_entry_is_never_replaced_with_different_bytes(self) -> None:
        release = self.published()
        entry = self.reuse(release, realized_document())
        self.write_manifest(realized_document())
        pool = self.root / "pool"
        pool.mkdir(parents=True, exist_ok=True)
        (pool / ARM64_DEB).write_bytes(b"a different deb with the same name")
        with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
            self.build(
                parse_manifest(realized_document()),
                reused_sources={(PACKAGE, ARCH): entry},
            )


class ReleaseWithReuseTest(ReuseTestCase):
    """The bound manifest states the reused provenance, not a fresh measurement."""

    def test_the_signed_manifest_carries_the_published_realized_block(self) -> None:
        release = self.published()
        entry = self.reuse(release, realized_document())
        planned = planned_document(entry.identity)
        self.write_manifest(planned)
        with mock.patch("scripts.build.fetch_verified", side_effect=self.fake_asset_fetch), \
                self.stub_asset_target(index=True):
            build_release(
                output_root=self.root / "tree",
                release_id=RELEASE_ID,
                cache_dir=self.root / "cache",
                manifest_document=planned,
                reused_sources={(PACKAGE, ARCH): entry},
            )
        published = json.loads(
            (self.root / "tree" / "releases" / RELEASE_ID / MANIFEST_NAME).read_text()
        )
        self.assertEqual(
            published["packages"][0]["targets"][ARCH]["source_build"],
            release.document["packages"][0]["targets"][ARCH]["source_build"],
        )
        self.assertEqual(
            (self.root / "tree" / "pool" / ARM64_DEB).read_bytes(), PUBLISHED_ARM64
        )

    def test_a_planned_target_is_still_refused_when_nothing_measured_it(self) -> None:
        release = self.published()
        entry = self.reuse(release, realized_document())
        planned = planned_document(entry.identity)
        self.write_manifest(planned)
        with mock.patch("scripts.build.fetch_verified", side_effect=self.fake_asset_fetch), \
                self.stub_asset_target(index=True), \
                self.assertRaisesRegex(ValueError, "needs its compiled artifact directory"):
            build_release(
                output_root=self.root / "tree",
                release_id=RELEASE_ID,
                cache_dir=self.root / "cache",
                manifest_document=planned,
            )


class CommandLineTest(ReuseTestCase):
    """The option is explicit, and every way to misuse it is a clear refusal."""

    def run_build(self, release: PublishedRelease | None, *extra: str,
                  document: dict[str, object] | None = None,
                  store: mock.MagicMock | None = None,
                  bucket: str | None = None,
                  fingerprint: str | None = None) -> tuple[int, str]:
        self.write_manifest(document if document is not None else realized_document())
        argv = [
            "build", "--output", str(self.root / "tree"),
            "--manifest", str(self.root / "manifest.json"), *extra,
        ]
        storage = store if store is not None else (
            release.source() if release is not None else None
        )
        bucket_value = BUCKET if bucket is None else bucket
        fingerprint_value = (
            (release.fingerprint if release else "") if fingerprint is None
            else fingerprint
        )
        with mock.patch.dict(
            os.environ,
            {
                "APT_S3_BUCKET": bucket_value,
                "APT_SIGNING_FINGERPRINT": fingerprint_value,
            },
            clear=False,
        ), mock.patch("scripts.repo.s3_source", return_value=storage), \
                mock.patch("scripts.build.fetch_verified", self.fake_asset_fetch), \
                self.stub_asset_target(index=True):
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = main(argv)
        return code, stderr.getvalue()

    def test_reuse_builds_the_release_from_the_published_deb(self) -> None:
        release = self.published()
        code, error = self.run_build(release, "--reuse-published-source")
        self.assertEqual(code, 0, error)
        self.assertEqual((self.root / "tree" / "pool" / ARM64_DEB).read_bytes(), PUBLISHED_ARM64)

    def test_reuse_without_a_bucket_is_refused(self) -> None:
        release = self.published()
        code, error = self.run_build(
            release, "--reuse-published-source", bucket=""
        )
        self.assertEqual(code, 1)
        self.assertIn("needs --bucket or APT_S3_BUCKET", error)

    def test_reuse_without_a_fingerprint_is_refused(self) -> None:
        release = self.published()
        code, error = self.run_build(
            release, "--reuse-published-source", fingerprint=""
        )
        self.assertEqual(code, 1)
        self.assertIn("APT_SIGNING_FINGERPRINT is required", error)

    def test_reuse_without_a_source_target_is_refused(self) -> None:
        release = self.published()
        code, error = self.run_build(
            release, "--reuse-published-source", document=asset_manifest_document()
        )
        self.assertEqual(code, 1)
        self.assertIn("needs a source-built target", error)

    def test_a_build_without_the_option_never_reads_storage(self) -> None:
        # No implicit network: the option is the only route to the published
        # release, so an ordinary build of the same manifest must not take it.
        self.write_manifest(realized_document())
        argv = [
            "build", "--output", str(self.root / "tree"),
            "--manifest", str(self.root / "manifest.json"),
        ]
        with mock.patch.dict(
            os.environ,
            {"APT_S3_BUCKET": BUCKET, "APT_SIGNING_FINGERPRINT": ""},
            clear=False,
        ), mock.patch(
            "scripts.repo.s3_source", side_effect=AssertionError("no storage read")
        ) as probed, mock.patch(
            "scripts.build.fetch_verified", self.fake_asset_fetch
        ), self.stub_asset_target(index=True):
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = main(argv)
            error = stderr.getvalue()
        self.assertEqual(code, 1)
        self.assertNotIn("no storage read", error)
        self.assertIn("needs its compiled artifact directory", error)
        probed.assert_not_called()


if __name__ == "__main__":
    unittest.main()