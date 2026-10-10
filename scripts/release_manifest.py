"""Serialize the canonical release manifest artifact and its Release binding.

The published baseline is the exact manifest object a build was made from,
re-serialized deterministically and bound into the signed Release by digest
and size, so a reader never has to trust the copy stored next to the pointer.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .manifest import Manifest, parse_manifest

MANIFEST_NAME = "manifest.json"
SHA256_FIELD = "X-Manifest-SHA256"
SIZE_FIELD = "X-Manifest-Size"
# Fixed once so the artifact bytes stay identical across runs and machines.
JSON_INDENT = 2


def canonical_bytes(data: object) -> bytes:
    """Return the deterministic byte form of a manifest object.

    Key order follows the source document, whitespace is a fixed two-space
    indent, and the file ends with one newline, so the digest of this form is
    stable for a given manifest.
    """
    text = json.dumps(data, indent=JSON_INDENT, ensure_ascii=False, sort_keys=False)
    return (text + "\n").encode("utf-8")


def manifest_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_manifest_data(path: Path) -> dict[str, object]:
    """Read a manifest document as a plain dict without reordering its keys."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("manifest root must be an object")
    return raw


def write_manifest_document(*, data: dict[str, object], destination: Path) -> Path:
    """Write the canonical bytes of an in-memory manifest object; returns its path."""
    payload = canonical_bytes(data)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    return destination


def write_manifest_artifact(*, source: Path, destination: Path) -> Path:
    """Write the canonical manifest copy for one release; returns its path.

    The artifact is written next to the release so `publish` uploads it with
    the rest of the release tree, while `update` treats it as untrusted until
    its digest matches the signed Release.
    """
    return write_manifest_document(data=load_manifest_data(source), destination=destination)


def manifest_binding_fields(manifest_path: Path) -> list[tuple[str, str]]:
    """Return the ``X-Manifest-*`` Release header fields for this artifact."""
    payload = manifest_path.read_bytes()
    return [
        (SHA256_FIELD, manifest_digest(payload)),
        (SIZE_FIELD, str(len(payload))),
    ]


def parse_binding(release_text: str) -> tuple[str | None, str | None]:
    """Extract the manifest digest and size a Release claims, if present."""
    digest: str | None = None
    size: str | None = None
    for line in release_text.splitlines():
        if line.startswith(f"{SHA256_FIELD}:"):
            digest = line.split(":", 1)[1].strip()
        elif line.startswith(f"{SIZE_FIELD}:"):
            size = line.split(":", 1)[1].strip()
    return digest, size


def check_binding_bytes(*, release_text: str, payload: bytes | None) -> None:
    """Require the Release binding to match the artifact bytes exactly.

    The binding is mandatory: a Release that carries neither field, or only one
    of them, always raises, as does a payload that is missing or mismatched. The
    check returns None on success so it reads as validation, not a boolean
    decision a caller could branch around.
    """
    digest, size = parse_binding(release_text)
    if digest is None and size is None:
        raise ValueError(
            f"Release must bind a baseline manifest with {SHA256_FIELD} and {SIZE_FIELD}"
        )
    if digest is None or size is None:
        raise ValueError(f"Release must carry both {SHA256_FIELD} and {SIZE_FIELD}")
    if payload is None:
        raise ValueError(f"Release binds a manifest artifact that is missing: {MANIFEST_NAME}")
    if len(payload) != int(size):
        raise ValueError(
            f"manifest size mismatch for {MANIFEST_NAME}: "
            f"Release declares {size}, artifact has {len(payload)}"
        )
    actual = manifest_digest(payload)
    if actual != digest:
        raise ValueError(
            f"manifest digest mismatch for {MANIFEST_NAME}: "
            f"Release declares {digest}, artifact hashes {actual}"
        )
    return None


def check_binding(*, release_text: str, manifest_path: Path | None) -> None:
    """Validate the Release manifest binding for a verifier that reads from disk."""
    payload = None
    if manifest_path is not None and manifest_path.is_file():
        payload = manifest_path.read_bytes()
    return check_binding_bytes(release_text=release_text, payload=payload)


def validated_document(path: Path) -> tuple[dict[str, object], Manifest]:
    """Read a manifest document and validate it; returns (raw object, manifest).

    The raw object is what a caller re-serializes into a release artifact, and
    the validated manifest is the parsed form used for every comparison, so a
    tampered document can never reach either side unchecked.
    """
    data = load_manifest_data(path)
    return data, parse_manifest(data)
