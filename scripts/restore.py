"""Restore a published release from object storage under a full trust chain.

Nothing is trusted because it is reachable: the pointer is validated, the
release is verified against the pinned public key, the clearsigned payload must
be the same bytes as the detached-signed `Release`, the signed hashes protect
every `Packages` file, and a baseline manifest must match the digest the
`Release` signed for. Package bytes are fetched only after all of that holds.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .fingerprint import check_key_matches_pin
from .manifest import Manifest, parse_manifest
from .publish import _s3_client as build_s3_client
from .release_manifest import MANIFEST_NAME, check_binding_bytes
from .sign import IsolatedHomedir, import_key, verify_clearsign, verify_detached

RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ARCHES = ("amd64", "arm64")
SUITE = "stable"
COMPONENT = "main"

MAX_METADATA_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_DEB_BYTES = 128 * 1024 * 1024
DEB_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+._-]*\.deb$")


class ObjectNotFound(Exception):
    """Storage confirmed the object does not exist."""


class NoCurrentRelease(Exception):
    """Storage confirmed no pointer object exists; the caller may bootstrap."""


class BaselineUnavailable(Exception):
    """The remote baseline cannot be trusted; a human must republish first."""


class Source(Protocol):
    """Bounded object reads; every failure mode is an exception, never empty bytes."""

    def get(self, key: str, *, max_bytes: int) -> bytes: ...


class S3Source:
    """Read published objects with a hard byte ceiling and explicit not-found errors.

    The ceiling is enforced on the bytes actually read, not on a declared length,
    and only a confirmed missing object raises `ObjectNotFound`; every other
    failure propagates so a permission or network problem stops the run instead
    of looking like an empty repository.
    """

    def __init__(self, client: Any, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    def get(self, key: str, *, max_bytes: int) -> bytes:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
        except Exception as exc:
            if _is_not_found(exc):
                raise ObjectNotFound(key) from exc
            raise
        body = response["Body"]
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = body.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"object {key} exceeded the {max_bytes} byte limit")
            chunks.append(chunk)
        return b"".join(chunks)


def s3_source(bucket: str, *, endpoint: str | None = None) -> S3Source:
    """Build the storage source from the same R2 environment the publisher uses."""
    return S3Source(build_s3_client(endpoint), bucket)


def _is_not_found(exc: Exception) -> bool:
    message = str(exc).lower()
    return "404" in message or "not found" in message or "nosuchkey" in message


@dataclass(frozen=True)
class TrustedRelease:
    """A remote release proven authentic up to (but not including) pool bytes."""

    release_id: str
    key_bytes: bytes
    packages: dict[str, str]
    manifest_data: dict[str, object] | None
    manifest: Manifest | None


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_release_hashes(release_text: str) -> dict[str, dict[str, tuple[str, int]]]:
    """Return the SHA256 and SHA512 entries of a Release, keyed by section."""
    return {section: _hash_section(release_text, section) for section in ("SHA256", "SHA512")}


def _hash_section(release_text: str, section: str) -> dict[str, tuple[str, int]]:
    out: dict[str, tuple[str, int]] = {}
    in_section = False
    for line in release_text.splitlines():
        if line.strip() == f"{section}:":
            in_section = True
            continue
        if in_section:
            if line and not line.startswith(" "):
                break
            match = re.match(r"^\s+([0-9a-f]+)\s+(\d+)\s+(\S+)\s*$", line)
            if match:
                digest, size, name = match.groups()
                out[name] = (digest.lower(), int(size))
    return out


def clearsigned_payload(inrelease: bytes) -> bytes:
    """Extract the signed text of a clearsigned document, keeping line breaks."""
    lines = inrelease.decode("utf-8").splitlines()
    try:
        start = next(
            i for i, line in enumerate(lines)
            if line.startswith("-----BEGIN PGP SIGNED MESSAGE-----")
        )
    except StopIteration:
        raise ValueError("InRelease carries no clearsign header") from None
    # Armor headers run to the first blank line, which then separates them from
    # the text the signature actually covers.
    cursor = start + 1
    while cursor < len(lines) and lines[cursor].strip():
        cursor += 1
    cursor += 1
    body: list[str] = []
    for line in lines[cursor:]:
        if line.startswith("-----BEGIN PGP SIGNATURE-----"):
            break
        # A clearsigned message escapes a leading "-" on an otherwise blank line.
        body.append(line[1:] if line == "-" else line)
    while body and not body[-1]:
        body.pop()
    if not body:
        raise ValueError("InRelease carries no signed payload")
    return ("\n".join(body) + "\n").encode("utf-8")


def verify_release_signatures(
    *, release: bytes, inrelease: bytes, detached: bytes, key_file: Path
) -> None:
    """Verify both signatures and require the clearsigned payload to be the same bytes."""
    with tempfile.TemporaryDirectory(prefix="apt-restore-verify-") as scratch:
        root = Path(scratch)
        release_file, inrelease_file, detached_file = root / "Release", root / "InRelease", root / "Release.gpg"
        release_file.write_bytes(release)
        inrelease_file.write_bytes(inrelease)
        detached_file.write_bytes(detached)
        with IsolatedHomedir() as home:
            import_key(home, key_file)
            try:
                verify_detached(release_file=release_file, signature=detached_file, homedir=home)
                verify_clearsign(inrelease=inrelease_file, homedir=home)
            except RuntimeError as exc:
                raise ValueError(f"published release signature is not valid: {exc}") from exc
    if clearsigned_payload(inrelease) != release:
        raise ValueError("InRelease payload does not match the detached-signed Release")


def read_release_id(source: Source) -> str:
    """Read and validate the current pointer; only a missing object bootstraps."""
    try:
        raw = source.get("current.json", max_bytes=MAX_MANIFEST_BYTES)
    except ObjectNotFound:
        raise NoCurrentRelease("no current.json in storage") from None
    try:
        pointer = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BaselineUnavailable(f"current.json is not valid JSON: {exc}") from exc
    release_id = pointer.get("release") if isinstance(pointer, dict) else None
    if not isinstance(release_id, str) or not RELEASE_ID.match(release_id):
        raise BaselineUnavailable(f"current.json does not select a valid release: {release_id!r}")
    return release_id


def _metadata(source: Source, key: str) -> bytes:
    try:
        return source.get(key, max_bytes=MAX_METADATA_BYTES)
    except ObjectNotFound:
        raise BaselineUnavailable(f"published release is missing {key}") from None


def _verify_signed_index(release_text: str, arch: str, payload: bytes) -> None:
    """Require one architecture's Packages to be exactly the bytes Release signed."""
    hashes = parse_release_hashes(release_text)
    rel = f"{COMPONENT}/binary-{arch}/Packages"
    entry = hashes["SHA256"].get(rel)
    if entry is None:
        raise ValueError(f"Release SHA256 section omits {rel}")
    digest, size = entry
    if len(payload) != size or sha256_bytes(payload) != digest:
        raise ValueError(f"Packages for {arch} does not match the signed SHA256")
    gz_rel = f"{COMPONENT}/binary-{arch}/Packages.gz"
    if gz_rel not in hashes["SHA256"] and gz_rel not in hashes["SHA512"]:
        raise ValueError(f"Release omits the compressed index for {arch}")


def restore_metadata(source: Source, *, fingerprint: str) -> TrustedRelease:
    """Establish the trust chain for the published release without fetching packages."""
    release_id = read_release_id(source)
    dists = f"releases/{release_id}/dists/{SUITE}"
    key_bytes = _metadata(source, "key.asc")
    release = _metadata(source, f"{dists}/Release")
    inrelease = _metadata(source, f"{dists}/InRelease")
    detached = _metadata(source, f"{dists}/Release.gpg")

    with tempfile.TemporaryDirectory(prefix="apt-restore-key-") as scratch:
        key_file = Path(scratch) / "key.asc"
        key_file.write_bytes(key_bytes)
        check_key_matches_pin(key_file=key_file, expected=fingerprint)
        verify_release_signatures(
            release=release, inrelease=inrelease, detached=detached, key_file=key_file
        )

    try:
        release_text = release.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BaselineUnavailable(f"Release is not valid UTF-8: {exc}") from exc

    manifest_data: dict[str, object] | None = None
    manifest: Manifest | None = None
    try:
        manifest_bytes: bytes | None = source.get(
            f"releases/{release_id}/{MANIFEST_NAME}", max_bytes=MAX_MANIFEST_BYTES
        )
    except ObjectNotFound:
        manifest_bytes = None
    if check_binding_bytes(release_text=release_text, payload=manifest_bytes):
        try:
            parsed = json.loads((manifest_bytes or b"").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BaselineUnavailable(f"bound manifest for {release_id} is invalid: {exc}") from exc
        if not isinstance(parsed, dict):
            raise BaselineUnavailable(f"bound manifest for {release_id} is not an object")
        manifest_data = parsed
        manifest = parse_manifest(parsed)

    packages: dict[str, str] = {}
    for arch in ARCHES:
        key = f"{dists}/{COMPONENT}/binary-{arch}/Packages"
        payload = _metadata(source, key)
        _verify_signed_index(release_text, arch, payload)
        try:
            packages[arch] = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Packages for {arch} is not valid UTF-8: {exc}") from exc

    return TrustedRelease(
        release_id=release_id, key_bytes=key_bytes, packages=packages,
        manifest_data=manifest_data, manifest=manifest,
    )
