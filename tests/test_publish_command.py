"""`cmd_publish` through its real argv path, with two distinct ephemeral keys.

The downgrade guard reads the remote `key.asc`, so an authorized key rotation is
exactly where the guard and the rotation flag meet. These tests drive
`scripts.repo.main` against an in-memory store: a matching key publishes, a key
change without the flag is refused, an authorized rotation publishes and moves
the remote key, and a rotation cannot be used to smuggle a downgrade or a tampered
old release past the guard.

Upstream downloads are stubbed with a local tarball, so nothing touches the
network and no production object store is involved.
"""

import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.common import sha256_bytes
from scripts.manifest import default_manifest_path, load_manifest, parse_manifest
from scripts.release_build import build_release
from scripts.release_manifest import load_manifest_data
from scripts.repo import main
from scripts.sign import export_public_key, sign_release
from scripts.source_build import SOURCE_PROVENANCE_NAME, Lockfile
from scripts.storage import ObjectNotFound
from tests.helpers import FakeS3, make_gpg_home_with_key, write_target_archive
from tests.test_kandev_bundle import write_bundle_archive
from tests.release_fixtures import build_release_objects
from tests.test_source_artifacts import LOCK, artifact_directory, source_identity
from tests.test_source_build import source_manifest_document

BUCKET = "test-bucket"
PUBLISHED_ID = "20260102-030405"
CANDIDATE_ID = "20260304-050607"
RELEASE_KEY = f"releases/{PUBLISHED_ID}/dists/stable/Release"


class Store:
    """Adapt the FakeS3 dict to the bounded restore Source protocol."""

    def __init__(self, client: FakeS3) -> None:
        self.client = client

    def get(self, key: str, *, max_bytes: int) -> bytes:
        payload = self.client.objects.get((BUCKET, key))
        if payload is None:
            raise ObjectNotFound(key)
        if len(payload) > max_bytes:
            raise ValueError(f"object {key} exceeded the {max_bytes} byte limit")
        return payload


def _fake_fetch(url: str, expected_sha256: str, dest: Path) -> Path:
    """Stand in for the pinned upstream download with a local stub archive.

    The archive carries the exact member the manifest pins, including the
    versioned directory prefix some upstreams use, so the production extraction
    path is exercised rather than bypassed. The member content matches the
    `fake-<name>-<arch>` binaries the published release is built from, so a
    candidate release carries byte-identical pool files and the
    immutable-overwrite guard stays satisfied.
    """
    stem = dest.name[: -len(".tar.gz")]
    manifest = load_manifest()
    name = next(
        pkg.name
        for pkg in sorted(manifest.packages, key=lambda p: len(p.name), reverse=True)
        if stem.startswith(f"{pkg.name}-")
    )
    pkg = next(p for p in manifest.packages if p.name == name)
    arch = stem.rsplit("-", 1)[1]
    payload = f"fake-{name}-{arch}".encode()
    if name == "kandev":
        return write_bundle_archive(dest, version=f"v{pkg.version}", arch=arch)
    return write_target_archive(dest, pkg, arch, payload)


def _build_candidate(
    tree: Path, key_home: Path, key_id: str, *, older_zellij: bool = False
) -> None:
    """Build and sign a complete local release tree with the given key."""
    data = copy.deepcopy(load_manifest_data(default_manifest_path()))
    if older_zellij:
        for entry in data["packages"]:
            if entry["name"] == "zellij":
                entry["version"] = "0.44.0"
    # Hermetic local builds use the fixture source identity (fixture lock) so
    # the fixture artifact matches without network; the real pin stays covered
    # by `test_cargo_tauri_recipe`. Planning-only paths keep the real pin.
    from tests.test_source_artifacts import source_identity as _fixture_identity
    from scripts.source_build import document as _document_of

    _fixture_doc = _document_of(_fixture_identity())
    for entry in data["packages"]:
        if entry["name"] == "cargo-tauri":  # type: ignore[index]
            entry["targets"]["arm64"]["source_build"] = _fixture_doc  # type: ignore[index]
    parse_manifest(data)  # the overlay must still validate

    from scripts.source_artifacts import index_source_artifacts, load_source_artifacts
    from tests.test_source_artifacts import artifact_directory, pinned

    artifacts_dir, identity = artifact_directory(tree.parent / "source-artifacts")
    artifacts = index_source_artifacts(
        [load_source_artifacts(artifacts_dir, expected=pinned(identity))]
    )
    with mock.patch("scripts.build.fetch_verified", _fake_fetch):
        build_release(
            output_root=tree,
            release_id=CANDIDATE_ID,
            cache_dir=tree.parent / "cache",
            manifest_document=data,
            source_artifacts=artifacts,
        )
    release_file = tree / "releases" / CANDIDATE_ID / "dists" / "stable" / "Release"
    previous = os.environ.get("GNUPGHOME")
    os.environ["GNUPGHOME"] = str(key_home)
    try:
        sign_release(release_file=release_file, key_id=key_id)
    finally:
        if previous is None:
            os.environ.pop("GNUPGHOME", None)
        else:
            os.environ["GNUPGHOME"] = previous
    export_public_key(homedir=key_home, key_id=key_id, dest=tree / "key.asc")
    (tree / "current.json").write_text(
        json.dumps({"release": CANDIDATE_ID}) + "\n", encoding="utf-8"
    )


class PublishCommandTest(unittest.TestCase):
    """A bucket holding an old-key release and a local tree signed by a new key."""

    def _two_keys(self) -> tuple[Path, str, Path, str]:
        old_tmp, old_home, old_key = make_gpg_home_with_key()
        new_tmp, new_home, new_key = make_gpg_home_with_key()
        self.addCleanup(old_tmp.cleanup)
        self.addCleanup(new_tmp.cleanup)
        return old_home, old_key, new_home, new_key

    def _world(
        self,
        root: Path,
        old_home: Path,
        old_key: str,
        new_home: Path,
        new_key: str,
        *,
        older_zellij: bool = False,
    ) -> tuple[FakeS3, Path]:
        """Publish an old-key release remotely and build a new-key tree locally."""
        client = FakeS3()
        for key, payload in build_release_objects(
            root / "published", key_home=old_home, key_id=old_key
        ).items():
            client.objects[(BUCKET, key)] = payload
        tree = root / "tree"
        _build_candidate(tree, new_home, new_key, older_zellij=older_zellij)
        return client, tree

    def _run(self, tree: Path, client: FakeS3, fingerprint: str, *extra: str) -> int:
        argv = ["publish", "--output", str(tree), *extra]
        with mock.patch.dict(
            os.environ,
            {"APT_SIGNING_FINGERPRINT": fingerprint, "APT_S3_BUCKET": BUCKET},
            clear=False,
        ), mock.patch("scripts.repo.s3_source", return_value=Store(client)), mock.patch(
            "scripts.publish._s3_client", return_value=client
        ):
            return main(argv)

    def test_matching_key_publishes_normally(self) -> None:
        old_home, old_key, new_home, new_key = self._two_keys()
        with tempfile.TemporaryDirectory() as work:
            # Same key on both sides: the ordinary path needs no rotation flag.
            client = FakeS3()
            tree = Path(work) / "tree"
            _build_candidate(tree, old_home, old_key)
            for key, payload in build_release_objects(
                Path(work) / "published", key_home=old_home, key_id=old_key
            ).items():
                client.objects[(BUCKET, key)] = payload

            self.assertEqual(self._run(tree, client, old_key), 0)
            self.assertEqual(client.puts[-1], "current.json")

    def test_key_change_without_the_flag_is_refused(self) -> None:
        old_home, old_key, new_home, new_key = self._two_keys()
        with tempfile.TemporaryDirectory() as work:
            client, tree = self._world(Path(work), old_home, old_key, new_home, new_key)
            remote_key_before = client.objects[(BUCKET, "key.asc")]

            # The pin is the new key while storage still serves the old one.
            self.assertEqual(self._run(tree, client, new_key), 1)
            self.assertEqual(client.puts, [], "a refused publish must write nothing")
            self.assertEqual(client.objects[(BUCKET, "key.asc")], remote_key_before)

    def test_authorized_rotation_publishes_and_updates_the_remote_key(self) -> None:
        old_home, old_key, new_home, new_key = self._two_keys()
        self.assertNotEqual(old_key, new_key)
        with tempfile.TemporaryDirectory() as work:
            client, tree = self._world(Path(work), old_home, old_key, new_home, new_key)

            self.assertEqual(
                self._run(tree, client, new_key, "--allow-key-rotation"),
                0,
                "an authorized rotation must publish",
            )
            self.assertEqual(client.puts[-1], "current.json", "pointer moves last")
            self.assertIn("key.asc", client.puts)
            self.assertEqual(
                client.objects[(BUCKET, "key.asc")], (tree / "key.asc").read_bytes()
            )

    def test_a_rotation_cannot_smuggle_a_tampered_old_release_past_the_guard(self) -> None:
        old_home, old_key, new_home, new_key = self._two_keys()
        with tempfile.TemporaryDirectory() as work:
            client, tree = self._world(Path(work), old_home, old_key, new_home, new_key)
            # Rotation verifies the baseline against the old key, so a tampered
            # old release must still fail closed rather than be waved through.
            client.objects[(BUCKET, RELEASE_KEY)] += b"\nInjected: yes\n"

            self.assertEqual(
                self._run(tree, client, new_key, "--allow-key-rotation"),
                1,
                "rotation must not bypass old-signature checks",
            )
            self.assertEqual(client.puts, [], "a refused rotation must write nothing")

    def test_a_rotation_cannot_smuggle_a_downgrade_past_the_guard(self) -> None:
        old_home, old_key, new_home, new_key = self._two_keys()
        with tempfile.TemporaryDirectory() as work:
            client, tree = self._world(
                Path(work), old_home, old_key, new_home, new_key, older_zellij=True
            )
            self.assertEqual(
                self._run(tree, client, new_key, "--allow-key-rotation"),
                1,
                "rotation must not bypass the downgrade guard",
            )
            self.assertEqual(client.puts, [], "a refused rotation must write nothing")


class SourceArtifactHandOffTest(unittest.TestCase):
    """The producer's directory, handed to the publisher through the real CLI.

    This is the same-run association the workflows rely on: one directory produced
    by a credential-free ARM job, read by the job that holds the signing key. Refusals
    prove a credential-free producer cannot make the publisher package something it
    did not measure; the positive case proves a validated artifact now packages
    through the registered production recipe.
    """

    def _manifest(self, root: Path) -> tuple[Path, dict[str, object]]:
        from tests.test_source_build import source_manifest_document as _source_doc

        data = _source_doc(source_identity().document())
        # P1/P2 helper predates the production recipe and carries no
        # `extra_license_urls` and a placeholder `license_url`; production
        # parsing requires the reviewed tag-bound dual-license URLs, so add
        # them here rather than weakening the validator.
        data["packages"][0]["license_url"] = (  # type: ignore[index]
            "https://raw.githubusercontent.com/tauri-apps/tauri/tauri-cli-v2.12.2/LICENSE-MIT"
        )
        data["packages"][0]["extra_license_urls"] = [  # type: ignore[index]
            "https://raw.githubusercontent.com/tauri-apps/tauri/tauri-cli-v2.12.2/LICENSE-APACHE-2.0"
        ]
        path = root / "manifest.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path, data

    def _build(self, tree: Path, manifest: Path, *extra: str) -> tuple[int, str]:
        package = parse_manifest(json.loads(manifest.read_text(encoding="utf-8"))).packages[0]

        def fetch(url: str, expected_sha256: str, dest: Path) -> Path:
            # The asset target is upstream's business; this test is about the
            # compiled architecture, so the download is stubbed and never fetched.
            return write_target_archive(dest, package, "amd64", b"fake-cargo-tauri-amd64")

        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch("scripts.build.fetch_verified", fetch), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["build", "--output", str(tree), "--manifest", str(manifest), *extra])
        return code, stderr.getvalue()

    def test_a_manifest_with_no_artifact_directory_builds_no_arm64_package(self) -> None:
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            manifest, _ = self._manifest(root)
            tree = root / "tree"
            code, error = self._build(tree, manifest)
            self.assertEqual(code, 1)
            self.assertIn("build failed", error)
            self.assertEqual(sorted((tree / "pool").glob("*_arm64.deb")), [])

    def test_an_artifact_for_another_identity_is_refused(self) -> None:
        from tests.test_source_build import source_manifest_document as _source_doc

        from scripts.source_artifacts import NOTICE_FILES as _NOTICE_FILES

        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            manifest, _ = self._manifest(root)
            directory, _ = artifact_directory(root / "artifacts")
            vendor = Path("packaging/licenses/cargo-tauri")
            for name in _NOTICE_FILES:
                (directory / name).write_bytes((vendor / name).read_bytes())
            moved = source_identity(
                lockfile=Lockfile("Cargo.lock", "b" * 40, len(LOCK), sha256_bytes(LOCK))
            )
            moved_doc = _source_doc(moved.document())
            moved_doc["packages"][0]["license_url"] = (  # type: ignore[index]
                "https://raw.githubusercontent.com/tauri-apps/tauri/tauri-cli-v2.12.2/LICENSE-MIT"
            )
            moved_doc["packages"][0]["extra_license_urls"] = [  # type: ignore[index]
                "https://raw.githubusercontent.com/tauri-apps/tauri/tauri-cli-v2.12.2/LICENSE-APACHE-2.0"
            ]
            manifest.write_text(
                json.dumps(moved_doc), encoding="utf-8"
            )
            code, error = self._build(
                root / "tree", manifest, "--source-artifacts", str(directory)
            )
            self.assertEqual(code, 1)
            self.assertIn("lockfile git_blob must be exactly", error)

    def test_a_directory_without_its_record_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            manifest, _ = self._manifest(root)
            directory, _ = artifact_directory(root / "artifacts")
            (directory / SOURCE_PROVENANCE_NAME).unlink()
            code, error = self._build(
                root / "tree", manifest, "--source-artifacts", str(directory)
            )
            self.assertEqual(code, 1)
            self.assertIn(f"no {SOURCE_PROVENANCE_NAME}", error)

    def test_the_arm64_package_is_not_built_from_a_refused_directory(self) -> None:
        # No path may produce an arm64 .deb from a directory the build refused: the
        # pool the publisher would sign stays empty for every refusal below.
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            manifest, _ = self._manifest(root)
            directory, _ = artifact_directory(root / "artifacts")
            (directory / SOURCE_PROVENANCE_NAME).unlink()
            tree = root / "tree"
            code, _ = self._build(tree, manifest, "--source-artifacts", str(directory))
            self.assertEqual(code, 1)
            self.assertEqual(sorted((tree / "pool").glob("*_arm64.deb")), [])

    def test_a_validated_artifact_packages_through_the_registered_recipe(self) -> None:
        # The catalog phase registered the production recipe and vendored licence
        # texts, so a validated artifact now builds both architectures: the
        # official amd64 asset (stubbed download) and the compiled arm64 binary.
        # Negative trust cases above are retained; this is the positive close.
        # Production consumption compares notices against the trusted vendored
        # texts, so the fixture directory carries those bytes here.
        import subprocess

        from scripts.source_artifacts import NOTICE_FILES

        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            manifest, _ = self._manifest(root)
            directory, identity = artifact_directory(root / "artifacts")
            vendor = Path("packaging/licenses/cargo-tauri")
            for name in NOTICE_FILES:
                (directory / name).write_bytes((vendor / name).read_bytes())
            code, error = self._build(
                root / "tree", manifest, "--source-artifacts", str(directory)
            )
            code, error = self._build(
                root / "tree", manifest, "--source-artifacts", str(directory)
            )
            self.assertEqual(code, 0, msg=error)
            pool = root / "tree" / "pool"
            self.assertTrue((pool / "cargo-tauri_2.12.2-1_amd64.deb").is_file())
            self.assertTrue((pool / "cargo-tauri_2.12.2-1_arm64.deb").is_file())
            shown = subprocess.run(
                ["dpkg-deb", "-f", str(pool / "cargo-tauri_2.12.2-1_arm64.deb"),
                 "Package", "Version", "Architecture"],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(shown.returncode, 0, msg=shown.stderr)
            self.assertIn("cargo-tauri", shown.stdout)
            self.assertIn("2.12.2-1", shown.stdout)


if __name__ == "__main__":
    unittest.main()