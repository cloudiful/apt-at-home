"""Establish the trust chain of a published release before any package is read.

Nothing is trusted because it is reachable: the pointer is validated, the
release is verified against the pinned public key, the clearsigned payload must
be the same bytes as the detached-signed `Release`, the signed hashes protect
every `Packages` file, and the baseline manifest must match the digest the
`Release` signed for.
"""

from __future__ import annotations

import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .common import sha256_bytes
from .fingerprint import check_key_matches_pin
from .manifest import Manifest, parse_baseline_manifest
from .metadata import ARCHES, COMPONENT, SUITE
from .release_hashes import parse_release_hashes
from .release_manifest import MANIFEST_NAME, check_binding_bytes
from .sign import IsolatedHomedir, import_key, verify_clearsign, verify_detached
from .storage import MAX_MANIFEST_BYTES, MAX_METADATA_BYTES, ObjectNotFound, Source

RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class NoCurrentRelease(Exception):
    """Storage confirmed no pointer object exists; the caller may bootstrap."""


class BaselineUnavailable(Exception):
    """The remote baseline cannot be trusted; a human must republish first."""


@dataclass(frozen=True)
class TrustedRelease:
    """A remote release proven authentic up to (but not including) pool bytes."""

    release_id: str
    key_bytes: bytes
    packages: dict[str, str]
    manifest_data: dict[str, object]
    manifest: Manifest


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
        release_file = root / "Release"
        inrelease_file = root / "InRelease"
        detached_file = root / "Release.gpg"
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

    try:
        manifest_bytes: bytes | None = source.get(
            f"releases/{release_id}/{MANIFEST_NAME}", max_bytes=MAX_MANIFEST_BYTES
        )
    except ObjectNotFound:
        manifest_bytes = None
    # The binding is mandatory: absent, partial, missing, or tampered all raise.
    check_binding_bytes(release_text=release_text, payload=manifest_bytes)
    if manifest_bytes is None:  # unreachable: the binding check refuses a missing artifact
        raise ValueError(f"Release binds a manifest artifact that is missing: {MANIFEST_NAME}")
    try:
        parsed = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BaselineUnavailable(f"bound manifest for {release_id} is invalid: {exc}") from exc
    if not isinstance(parsed, dict):
        raise BaselineUnavailable(f"bound manifest for {release_id} is not an object")
    # A published baseline may predate a reviewed architecture asset, so it is
    # parsed with the narrow historical-gap rule after the trust chain passed.
    manifest = parse_baseline_manifest(parsed)

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
        manifest_data=parsed, manifest=manifest,
    )
