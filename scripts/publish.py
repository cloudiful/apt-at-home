"""Publish a verified release to S3-compatible storage without deletions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .common import sha256_file
from .fingerprint import check_key_matches_pin, normalize_fingerprint
from .verify import verify_tree


@dataclass(frozen=True)
class PublishPlan:
    pool: list[Path]
    release_files: list[Path]
    indices: list[Path]
    pointer: Path
    key: Path | None


def collect_plan(output_root: Path, release_id: str) -> PublishPlan:
    pool = sorted((output_root / "pool").glob("*.deb"))
    release_base = output_root / "releases" / release_id
    release_files = sorted(p for p in release_base.rglob("*") if p.is_file())
    indices = sorted(p for p in (output_root / "indices").rglob("*") if p.is_file())
    pointer = output_root / "current.json"
    key = output_root / "key.asc"
    return PublishPlan(
        pool=pool,
        release_files=release_files,
        indices=indices,
        pointer=pointer,
        key=key if key.exists() else None,
    )


def _s3_client(endpoint: str | None = None):  # type: ignore[no-untyped-def]
    import os

    import boto3

    kwargs: dict[str, object] = {}
    if endpoint or os.environ.get("APT_S3_ENDPOINT"):
        kwargs["endpoint_url"] = endpoint or os.environ["APT_S3_ENDPOINT"]
    region = os.environ.get("APT_S3_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if region:
        kwargs["region_name"] = region
    return boto3.client("s3", **kwargs)


def _object_exists(client, bucket: str, key: str) -> bool:  # type: ignore[no-untyped-def]
    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except Exception as exc:
        message = str(exc).lower()
        if "404" in message or "not found" in message or "nosuchkey" in message:
            return False
        raise


def _remote_sha256(client, bucket: str, key: str) -> str | None:  # type: ignore[no-untyped-def]
    try:
        response = client.get_object(Bucket=bucket, Key=key)
        body = response["Body"].read()
    except Exception as exc:
        message = str(exc).lower()
        if "404" in message or "not found" in message or "nosuchkey" in message:
            return None
        raise
    import hashlib

    return hashlib.sha256(body).hexdigest()


def publish_tree(
    *,
    output_root: Path,
    bucket: str,
    release_id: str,
    endpoint: str | None = None,
    allow_key_rotation: bool = False,
    expected_fingerprint: str | None = None,
    client=None,  # type: ignore[no-untyped-def]
) -> list[str]:
    """Upload pool, by-hash, release, key, then verify and move the pointer last."""
    if expected_fingerprint is None:
        raise ValueError("APT_SIGNING_FINGERPRINT is required for publish")
    # Normalize first so a malformed pin fails before any storage access.
    wanted = normalize_fingerprint(expected_fingerprint)
    key_path = output_root / "key.asc"
    if not key_path.exists():
        raise ValueError(f"signing key not found: {key_path}")
    # Rotation never bypasses the pin: the replacement key must match the
    # explicitly updated trusted fingerprint.
    check_key_matches_pin(key_file=key_path, expected=wanted)
    findings = verify_tree(output_root, expected_fingerprint=wanted)
    _ = findings
    plan = collect_plan(output_root, release_id)
    s3 = client or _s3_client(endpoint)
    uploaded: list[str] = []

    # Collision guard: the same release id must never select different bytes.
    # An identical remote Release allows an idempotent retry after a partial
    # upload; a different payload with the same id is rejected.
    release_probe = f"releases/{release_id}/dists/stable/Release"
    remote_probe = _remote_sha256(s3, bucket, release_probe)
    if remote_probe is not None:
        local_probe = sha256_file(output_root / release_probe)
        if remote_probe != local_probe:
            raise ValueError(f"release id collision: {release_probe} already exists")

    def put_file(local: Path, key: str, content_type: str) -> None:
        existing = _remote_sha256(s3, bucket, key)
        local_digest = sha256_file(local)
        if existing is not None and existing != local_digest:
            raise ValueError(f"refusing to overwrite immutable object with different bytes: {key}")
        if existing == local_digest:
            return
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=local.read_bytes(),
            ContentType=content_type,
        )
        uploaded.append(key)

    for deb in plan.pool:
        put_file(deb, f"pool/{deb.name}", "application/vnd.debian.binary-package")
    for index in plan.indices:
        rel = index.relative_to(output_root)
        media = "application/gzip" if index.suffix == ".gz" else "text/plain"
        put_file(index, rel.as_posix(), media)
    for local in plan.release_files:
        rel = local.relative_to(output_root).as_posix()
        if local.name.endswith(".gz"):
            media = "application/gzip"
        elif local.name in ("Release", "Packages", "InRelease"):
            media = "text/plain"
        elif local.name == "Release.gpg":
            media = "application/pgp-signature"
        else:
            media = "application/octet-stream"
        put_file(local, rel, media)
    if plan.key is not None:
        existing = _remote_sha256(s3, bucket, "key.asc")
        local_digest = sha256_file(plan.key)
        if existing is not None and existing != local_digest and not allow_key_rotation:
            raise ValueError("key.asc differs remotely; pass --allow-key-rotation with operator confirmation")
        if existing is not None and existing != local_digest and allow_key_rotation:
            s3.put_object(
                Bucket=bucket,
                Key="key.asc",
                Body=plan.key.read_bytes(),
                ContentType="application/pgp-keys",
            )
            uploaded.append("key.asc")
        else:
            put_file(plan.key, "key.asc", "application/pgp-keys")

    # Re-read remote hashes before moving the pointer.
    for deb in plan.pool:
        remote = _remote_sha256(s3, bucket, f"pool/{deb.name}")
        if remote != sha256_file(deb):
            raise ValueError(f"remote verification failed for pool/{deb.name}")
    remote_release = _remote_sha256(s3, bucket, release_probe)
    local_release = sha256_file(output_root / release_probe)
    if remote_release != local_release:
        raise ValueError("remote verification failed for Release; pointer not updated")

    pointer_body = json.dumps({"release": release_id}) + "\n"
    s3.put_object(
        Bucket=bucket,
        Key="current.json",
        Body=pointer_body.encode("utf-8"),
        ContentType="application/json",
    )
    uploaded.append("current.json")
    return uploaded
