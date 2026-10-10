"""Restore trust chain: pointer, signatures, payload equality, signed hashes, binding.

A signed release is built in a throwaway GPG homedir and served from an
in-memory object store; each link is then broken one at a time to prove the
restore refuses it. No storage credentials and no operator key are involved.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from scripts.deb import build_deb, stage_package
from scripts.manifest import default_manifest_path, deb_filename, load_manifest
from scripts.metadata import generate_release, write_indices
from scripts.release_manifest import (
    MANIFEST_NAME,
    load_manifest_data,
    manifest_binding_fields,
    write_manifest_document,
)
from scripts.reprepro_adapter import generate_packages
from scripts.restore import (
    ARCHES,
    MAX_DEB_BYTES,
    MAX_MANIFEST_BYTES,
    BaselineUnavailable,
    NoCurrentRelease,
    ObjectNotFound,
    TrustedRelease,
    read_release_id,
    restore_metadata,
)
from scripts.restore_packages import pool_entries
from scripts.sign import export_public_key, sign_release
from tests.helpers import make_gpg_home_with_key

RELEASE_ID = "20260102-030405"
COMMITTED = load_manifest()


class FakeStorage:
    """Object store double: a missing key raises, byte ceilings are enforced."""

    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = dict(objects)
        self.reads: list[str] = []

    def get(self, key: str, *, max_bytes: int) -> bytes:
        self.reads.append(key)
        if key not in self.objects:
            raise ObjectNotFound(key)
        payload = self.objects[key]
        if len(payload) > max_bytes:
            raise ValueError(f"object {key} exceeded the {max_bytes} byte limit")
        return payload


class DeniedStorage(FakeStorage):
    """Every read fails the way a permissions error does, never like a 404."""

    def get(self, key: str, *, max_bytes: int) -> bytes:
        raise RuntimeError("An error occurred (403) Forbidden: access denied")


def _build_debs(root: Path) -> None:
    """Stage the four pinned packages as real .deb files under root/pool."""
    pool = root / "pool"
    pool.mkdir(parents=True, exist_ok=True)
    for pkg in COMMITTED.packages:
        for arch in ARCHES:
            binary = root / "src" / f"{pkg.name}-{arch}" / pkg.targets[arch].binary
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_bytes(f"fake-{pkg.name}-{arch}".encode())
            binary.chmod(0o755)
            stage = stage_package(
                pkg=pkg, arch=arch, manifest=COMMITTED,
                binary_file=binary, stage_root=root / "stages",
            )
            build_deb(stage, pool / deb_filename(pkg, arch, COMMITTED))


def build_release_objects(root: Path, *, key_home: Path, key_id: str, bind: bool = True) -> dict[str, bytes]:
    """Build a signed (optionally manifest-bound) release and return its objects."""
    _build_debs(root)
    pool = root / "pool"
    dists = root / "releases" / RELEASE_ID / "dists" / "stable"
    packages_files: dict[str, list[Path]] = {}
    inputs: list[tuple[Path, str]] = []
    for arch in ARCHES:
        packages, packages_gz = generate_packages(
            pool, dists / "main" / f"binary-{arch}", sorted(pool.glob(f"*_{arch}.deb"))
        )
        packages_files[arch] = [packages, packages_gz]
        inputs.append((packages, f"main/binary-{arch}/Packages"))
        inputs.append((packages_gz, f"main/binary-{arch}/Packages.gz"))

    extra: list[tuple[str, str]] = []
    if bind:
        artifact = write_manifest_document(
            data=load_manifest_data(default_manifest_path()),
            destination=root / "releases" / RELEASE_ID / MANIFEST_NAME,
        )
        extra = manifest_binding_fields(artifact)
    release_file = generate_release(
        output_root=root, release_id=RELEASE_ID, files=inputs, extra_fields=extra
    )
    write_indices(output_root=root, release_id=RELEASE_ID, packages_files=packages_files)

    previous = os.environ.get("GNUPGHOME")
    os.environ["GNUPGHOME"] = str(key_home)
    try:
        sign_release(release_file=release_file, key_id=key_id)
        export_public_key(homedir=key_home, key_id=key_id, dest=root / "key.asc")
    finally:
        if previous is None:
            os.environ.pop("GNUPGHOME", None)
        else:
            os.environ["GNUPGHOME"] = previous

    objects: dict[str, bytes] = {
        "current.json": (json.dumps({"release": RELEASE_ID}) + "\n").encode(),
        "key.asc": (root / "key.asc").read_bytes(),
    }
    base = root / "releases" / RELEASE_ID
    for path in sorted(base.rglob("*")):
        if path.is_file():
            objects[str(path.relative_to(root))] = path.read_bytes()
    for deb in sorted(pool.glob("*.deb")):
        objects[f"pool/{deb.name}"] = deb.read_bytes()
    return objects


class PointerTest(unittest.TestCase):
    def test_only_a_confirmed_missing_pointer_bootstraps(self) -> None:
        with self.assertRaises(NoCurrentRelease):
            read_release_id(FakeStorage({}))
        with self.assertRaises(RuntimeError):
            read_release_id(DeniedStorage({}))
        with self.assertRaises(BaselineUnavailable):
            read_release_id(FakeStorage({"current.json": b"not json"}))
        for pointer in (b'{"release": "../escape"}', b'{"release": 7}', b"{}"):
            with self.subTest(pointer=pointer):
                with self.assertRaises(BaselineUnavailable):
                    read_release_id(FakeStorage({"current.json": pointer}))
        self.assertEqual(read_release_id(FakeStorage({"current.json": b'{"release":"r1"}'})), "r1")


class RestoreMetadataTest(unittest.TestCase):
    def _storage(self, root: Path, key_home: Path, key_id: str, *, bind: bool = True) -> FakeStorage:
        return FakeStorage(build_release_objects(root, key_home=key_home, key_id=key_id, bind=bind))

    def test_trusted_release_is_restored_without_touching_the_pool(self) -> None:
        tmpdir, home, key_id = make_gpg_home_with_key()
        try:
            with tempfile.TemporaryDirectory() as work:
                storage = self._storage(Path(work), home, key_id)
                restored = restore_metadata(storage, fingerprint=key_id)
                self.assertEqual(restored.release_id, RELEASE_ID)
                self.assertEqual(restored.manifest_data["debian_revision"], "-1")
                self.assertEqual(
                    {pkg.name for pkg in restored.manifest.packages}, {"starship", "zellij"}
                )
                for arch in ARCHES:
                    self.assertIn(f"Architecture: {arch}", restored.packages[arch])
                self.assertEqual([key for key in storage.reads if key.startswith("pool/")], [])
        finally:
            tmpdir.cleanup()

    def test_wrong_key_pin_is_refused_before_any_signature_check(self) -> None:
        tmpdir, home, key_id = make_gpg_home_with_key()
        other_tmp, _, other_id = make_gpg_home_with_key()
        try:
            with tempfile.TemporaryDirectory() as work:
                storage = self._storage(Path(work), home, key_id)
                for pin in (other_id, "ABCD1234"):
                    with self.subTest(pin=pin):
                        with self.assertRaises(ValueError):
                            restore_metadata(storage, fingerprint=pin)
        finally:
            tmpdir.cleanup()
            other_tmp.cleanup()

    def test_each_signed_object_is_covered_by_the_trust_chain(self) -> None:
        cases = {
            "release": (
                f"releases/{RELEASE_ID}/dists/stable/Release",
                lambda data: data + b"\nInjected: yes\n",
            ),
            "inrelease": (
                f"releases/{RELEASE_ID}/dists/stable/InRelease",
                lambda data: data.replace(b"Origin: apt-at-home", b"Origin: attacker  "),
            ),
            "detached": (
                f"releases/{RELEASE_ID}/dists/stable/Release.gpg",
                lambda data: data.replace(
                    b"-----BEGIN PGP SIGNATURE-----", b"-----BEGIN PGP SIGNATURX-----"
                ),
            ),
            "amd64_index": (
                f"releases/{RELEASE_ID}/dists/stable/main/binary-amd64/Packages",
                lambda data: data + b"\nInjected: yes\n",
            ),
            "arm64_index": (
                f"releases/{RELEASE_ID}/dists/stable/main/binary-arm64/Packages",
                lambda data: data + b"\nInjected: yes\n",
            ),
            "bound_manifest": (
                f"releases/{RELEASE_ID}/{MANIFEST_NAME}",
                lambda data: data + b"\n",
            ),
        }
        for label, (key, tamper) in cases.items():
            with self.subTest(object=label):
                tmpdir, home, key_id = make_gpg_home_with_key()
                try:
                    with tempfile.TemporaryDirectory() as work:
                        storage = self._storage(Path(work), home, key_id)
                        tampered = tamper(storage.objects[key])
                        self.assertNotEqual(tampered, storage.objects[key])
                        storage.objects[key] = tampered
                        with self.assertRaises(ValueError):
                            restore_metadata(storage, fingerprint=key_id)
                finally:
                    tmpdir.cleanup()

    def test_a_substituted_public_key_is_refused(self) -> None:
        tmpdir, home, key_id = make_gpg_home_with_key()
        other_tmp, other_home, _ = make_gpg_home_with_key()
        try:
            with tempfile.TemporaryDirectory() as work:
                storage = self._storage(Path(work), home, key_id)
                substitute = Path(work) / "other.asc"
                export_public_key(homedir=other_home, key_id=None, dest=substitute)
                storage.objects["key.asc"] = substitute.read_bytes()
                with self.assertRaises(ValueError):
                    restore_metadata(storage, fingerprint=key_id)
        finally:
            tmpdir.cleanup()
            other_tmp.cleanup()

    def test_independently_signed_payload_mismatch_is_refused(self) -> None:
        """A Release and an InRelease that each verify but disagree must not pass."""
        tmpdir, home, key_id = make_gpg_home_with_key()
        try:
            with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
                one = build_release_objects(Path(first), key_home=home, key_id=key_id)
                alternate = build_release_objects(Path(second), key_home=home, key_id=key_id)
                # Re-sign a differently dated Release so both signatures are valid
                # on their own while the two documents cover different bytes.
                swap = alternate[f"releases/{RELEASE_ID}/dists/stable/Release"]
                resigned = swap + b"X-Synthetic: second\n"
                swap_path = Path(second) / "releases" / RELEASE_ID / "dists" / "stable" / "Release"
                swap_path.write_bytes(resigned)
                previous = os.environ.get("GNUPGHOME")
                os.environ["GNUPGHOME"] = str(home)
                try:
                    sign_release(release_file=swap_path, key_id=key_id)
                finally:
                    if previous is None:
                        os.environ.pop("GNUPGHOME", None)
                    else:
                        os.environ["GNUPGHOME"] = previous
                storage = FakeStorage(
                    {
                        **one,
                        f"releases/{RELEASE_ID}/dists/stable/Release": resigned,
                        f"releases/{RELEASE_ID}/dists/stable/Release.gpg": (
                            swap_path.parent / "Release.gpg"
                        ).read_bytes(),
                    }
                )
                with self.assertRaises(ValueError) as ctx:
                    restore_metadata(storage, fingerprint=key_id)
                self.assertIn("does not match", str(ctx.exception))
        finally:
            tmpdir.cleanup()

    def test_a_missing_release_object_is_not_a_bootstrap(self) -> None:
        tmpdir, home, key_id = make_gpg_home_with_key()
        try:
            with tempfile.TemporaryDirectory() as work:
                storage = self._storage(Path(work), home, key_id)
                del storage.objects[f"releases/{RELEASE_ID}/dists/stable/InRelease"]
                with self.assertRaises(BaselineUnavailable):
                    restore_metadata(storage, fingerprint=key_id)
        finally:
            tmpdir.cleanup()

    def test_permission_failures_stop_instead_of_looking_empty(self) -> None:
        with self.assertRaises(RuntimeError):
            restore_metadata(DeniedStorage({}), fingerprint="A" * 40)

    def test_oversized_object_is_refused_by_the_source(self) -> None:
        tmpdir, home, key_id = make_gpg_home_with_key()
        try:
            with tempfile.TemporaryDirectory() as work:
                storage = self._storage(Path(work), home, key_id)
                storage.objects["current.json"] = b"x" * (MAX_MANIFEST_BYTES + 1)
                with self.assertRaises(ValueError):
                    restore_metadata(storage, fingerprint=key_id)
        finally:
            tmpdir.cleanup()

    def test_legacy_release_without_a_binding_restores(self) -> None:
        tmpdir, home, key_id = make_gpg_home_with_key()
        try:
            with tempfile.TemporaryDirectory() as work:
                storage = self._storage(Path(work), home, key_id, bind=False)
                restored = restore_metadata(storage, fingerprint=key_id)
                self.assertIsNone(restored.manifest)
                self.assertIsNone(restored.manifest_data)
        finally:
            tmpdir.cleanup()

    def test_declared_read_ceilings(self) -> None:
        self.assertEqual(MAX_DEB_BYTES, 128 * 1024 * 1024)
        self.assertEqual(MAX_MANIFEST_BYTES, 64 * 1024)


class PoolEntryTest(unittest.TestCase):
    """The signed index is the only pool inventory, so every entry is checked."""

    def _release(self, amd64: list[str], arm64: list[str]) -> TrustedRelease:
        return TrustedRelease(
            release_id="r", key_bytes=b"", manifest_data=None, manifest=None,
            packages={"amd64": "\n\n".join(amd64), "arm64": "\n\n".join(arm64)},
        )

    def _stanza(self, name: str, arch: str, filename: str | None = None, *, digest: bool = True) -> str:
        fields = [
            f"Filename: {filename or f'pool/{name}'}",
            f"Package: {name.split('_')[0]}",
            f"Architecture: {arch}",
            "Version: 1",
            "Size: 10",
        ]
        if digest:
            fields.append(f"SHA256: {'ab' * 32}")
        return "\n".join(fields)

    @property
    def _amd64(self) -> list[str]:
        return [
            self._stanza("zellij_0.45.1-1_amd64.deb", "amd64"),
            self._stanza("starship_1.26.0-1_amd64.deb", "amd64"),
        ]

    @property
    def _arm64(self) -> list[str]:
        return [
            self._stanza("zellij_0.45.1-1_arm64.deb", "arm64"),
            self._stanza("starship_1.26.0-1_arm64.deb", "arm64"),
        ]

    def test_a_healthy_index_yields_the_four_pool_entries(self) -> None:
        entries = pool_entries(self._release(self._amd64, self._arm64))
        self.assertEqual(len(entries), 4)
        self.assertEqual(
            sorted(entry.name for entry in entries),
            [
                "starship_1.26.0-1_amd64.deb",
                "starship_1.26.0-1_arm64.deb",
                "zellij_0.45.1-1_amd64.deb",
                "zellij_0.45.1-1_arm64.deb",
            ],
        )

    def test_hostile_pool_inventory_is_refused(self) -> None:
        cases = {
            "path_traversal": (
                [self._stanza("x", "amd64", "pool/../../etc/passwd"), self._amd64[1]],
                "unusable pool name",
            ),
            "non_pool_path": (
                [self._stanza("x", "amd64", "evil/zellij.deb"), self._amd64[1]],
                "non-pool Filename",
            ),
            "duplicate": (
                [self._amd64[0], self._amd64[0]],
                "duplicate pool entry",
            ),
            "missing_entry": (
                [self._amd64[0]],
                "must hold four",
            ),
            "foreign_architecture": (
                [self._stanza("zellij_0.45.1-1_amd64.deb", "arm64"), self._amd64[1]],
                "as arm64",
            ),
            "missing_digest": (
                [self._stanza("zellij_0.45.1-1_amd64.deb", "amd64", digest=False), self._amd64[1]],
                "omits size or digest",
            ),
        }
        for label, (amd64, expected) in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(ValueError) as ctx:
                    pool_entries(self._release(amd64, self._arm64))
                self.assertIn(expected, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
