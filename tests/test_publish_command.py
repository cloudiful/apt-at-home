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

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.manifest import default_manifest_path, load_manifest, parse_manifest
from scripts.release_build import build_release
from scripts.release_manifest import load_manifest_data
from scripts.repo import main
from scripts.sign import export_public_key, sign_release
from scripts.storage import ObjectNotFound
from tests.helpers import FakeS3, make_gpg_home_with_key, write_target_archive
from tests.test_kandev_bundle import write_bundle_archive
from tests.release_fixtures import build_release_objects

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
    parse_manifest(data)  # the overlay must still validate

    with mock.patch("scripts.build.fetch_verified", _fake_fetch):
        build_release(
            output_root=tree,
            release_id=CANDIDATE_ID,
            cache_dir=tree.parent / "cache",
            manifest_document=data,
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


if __name__ == "__main__":
    unittest.main()