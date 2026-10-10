"""Bounded object storage access: the read protocol, byte limits, and S3 policy.

Nothing here trusts a declared length: every read is capped on the bytes it
actually returned, and only a confirmed missing object maps to `ObjectNotFound`
while every other failure propagates so a permission or network problem stops a
run instead of looking like an empty repository.
"""

from __future__ import annotations

import re
from typing import Any, Protocol

from .publish import _s3_client as build_s3_client

MAX_METADATA_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_DEB_BYTES = 128 * 1024 * 1024
DEB_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+._-]*\.deb$")


class ObjectNotFound(Exception):
    """Storage confirmed the object does not exist."""


class Source(Protocol):
    """Bounded object reads; every failure mode is an exception, never empty bytes."""

    def get(self, key: str, *, max_bytes: int) -> bytes: ...


class S3Source:
    """Read published objects with a hard byte ceiling and explicit not-found errors."""

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
