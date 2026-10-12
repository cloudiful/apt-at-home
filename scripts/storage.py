"""Bounded object storage access: the read protocol, byte limits, and S3 policy.

Nothing here trusts a declared length: every read is capped on the bytes it
actually returned, and only a confirmed missing object maps to `ObjectNotFound`
while every other failure propagates so a permission or network problem stops a
run instead of looking like an empty repository.

`PublicHttpsSource` serves the same protocol over anonymous HTTPS for the one
caller that must read the repository without any credential at all: the source
producer. It is read-only by construction — there is no put, no delete, and no
header a caller could supply — and it accepts only an `https://` base, so a
producer cannot be pointed at a bucket endpoint, a file path, or plain HTTP.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any, Protocol

from .publish import _s3_client as build_s3_client

MAX_METADATA_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_DEB_BYTES = 128 * 1024 * 1024
DEB_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+._-]*\.deb$")

PUBLIC_USER_AGENT = "apt-at-home/1.0"
PUBLIC_TIMEOUT_SECONDS = 30
_HTTPS_BASE = re.compile(r"^https://[A-Za-z0-9._~:/-]+/$")
# One object key: no scheme, no traversal, no empty or dot component.
_PUBLIC_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*(?:/[A-Za-z0-9][A-Za-z0-9._+-]*)*$")
_RELATIVE_PATH = re.compile(r"^[A-Za-z0-9._~/-]+$")


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


class PublicHttpsSource:
    """Read the published repository over anonymous HTTPS, one bounded key at a time.

    The base URL must be the public gateway's own `https://.../` root and a key
    must be a plain object path. Both are checked before any request, so a
    caller cannot turn a key into a different host, reach a private route, or
    downgrade the read to plaintext.
    """

    def __init__(
        self,
        base_url: str,
        *,
        reader: Callable[[urllib.request.Request, int, int], bytes] | None = None,
        timeout: int = PUBLIC_TIMEOUT_SECONDS,
    ) -> None:
        if not _HTTPS_BASE.match(base_url):
            raise ValueError(
                f"public repository base must be an https:// root URL: {base_url!r}"
            )
        # The path part is the served repository root, so it may not carry a
        # traversal or an empty segment: either would resolve somewhere the
        # reviewed root does not name.
        path = base_url.split("/", 3)[3]
        if path and (
            not _RELATIVE_PATH.match(path)
            or any(part in ("", ".", "..") for part in path.split("/"))
        ):
            raise ValueError(
                f"public repository base must be an https:// root URL: {base_url!r}"
            )
        self._base = base_url
        self._read = reader or read_bounded
        self._timeout = timeout

    def get(self, key: str, *, max_bytes: int) -> bytes:
        if not _PUBLIC_KEY.match(key):
            raise ValueError(f"public repository key must be a plain object path: {key!r}")
        request = urllib.request.Request(
            self._base + key,
            headers={"User-Agent": PUBLIC_USER_AGENT, "Accept-Encoding": "identity"},
            method="GET",
        )
        try:
            return self._read(request, max_bytes, self._timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise ObjectNotFound(key) from exc
            raise


def read_bounded(request: urllib.request.Request, max_bytes: int, timeout: int) -> bytes:
    """Fetch one object, capping on the bytes actually read rather than a declared length."""
    with urllib.request.urlopen(request, timeout=timeout) as response:
        declared = response.headers.get("Content-Length")
        if declared is not None and declared.isdigit() and int(declared) > max_bytes:
            raise ValueError(f"object {request.full_url} exceeds the {max_bytes} byte limit")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"object {request.full_url} exceeded the {max_bytes} byte limit")
            chunks.append(chunk)
    return b"".join(chunks)


def public_source(base_url: str, **kwargs: Any) -> PublicHttpsSource:
    """Build the credential-free source the producer plans against."""
    return PublicHttpsSource(base_url, **kwargs)


def s3_source(bucket: str, *, endpoint: str | None = None) -> S3Source:
    """Build the storage source from the same R2 environment the publisher uses."""
    return S3Source(build_s3_client(endpoint), bucket)


def _is_not_found(exc: Exception) -> bool:
    message = str(exc).lower()
    return "404" in message or "not found" in message or "nosuchkey" in message
