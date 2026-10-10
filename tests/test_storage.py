"""Bounded storage source: byte ceilings, not-found mapping, error propagation."""

import unittest
from unittest import mock

from scripts.storage import (
    DEB_NAME,
    MAX_DEB_BYTES,
    MAX_MANIFEST_BYTES,
    MAX_METADATA_BYTES,
    ObjectNotFound,
    S3Source,
    s3_source,
)


class _Body:
    """Stream one payload in the chunk size the source asks for."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = len(self._payload)
        chunk, self._payload = self._payload[:size], self._payload[size:]
        return chunk


class _Client:
    """Minimal S3 double: a missing key is a 404, `error` overrides every call."""

    def __init__(self, objects: dict[str, bytes] | None = None, *, error: Exception | None = None) -> None:
        self.objects = objects or {}
        self.error = error

    def get_object(self, Bucket: str, Key: str) -> dict[str, object]:
        if self.error is not None:
            raise self.error
        if Key not in self.objects:
            raise Exception(f"An error occurred (404) Not Found: {Key}")
        return {"Body": _Body(self.objects[Key])}


class S3SourceTest(unittest.TestCase):
    def test_reads_a_bounded_object(self) -> None:
        source = S3Source(_Client({"key.asc": b"payload"}), "bucket")
        self.assertEqual(source.get("key.asc", max_bytes=64), b"payload")

    def test_missing_object_maps_to_not_found(self) -> None:
        source = S3Source(_Client({}), "bucket")
        with self.assertRaises(ObjectNotFound):
            source.get("absent", max_bytes=64)

    def test_each_not_found_spelling_maps_to_not_found(self) -> None:
        for message in ("NoSuchKey", "not found"):
            with self.subTest(message=message):
                source = S3Source(_Client({}, error=Exception(message)), "bucket")
                with self.assertRaises(ObjectNotFound):
                    source.get("x", max_bytes=64)

    def test_a_denied_error_is_not_not_found(self) -> None:
        source = S3Source(_Client({}, error=Exception("403 Forbidden")), "bucket")
        with self.assertRaises(Exception) as ctx:
            source.get("x", max_bytes=64)
        self.assertNotIsInstance(ctx.exception, ObjectNotFound)

    def test_non_not_found_errors_propagate(self) -> None:
        source = S3Source(_Client({}, error=RuntimeError("network unreachable")), "bucket")
        with self.assertRaises(RuntimeError):
            source.get("x", max_bytes=64)

    def test_ceiling_is_enforced_on_the_bytes_read(self) -> None:
        source = S3Source(_Client({"big": b"x" * 10}), "bucket")
        with self.assertRaises(ValueError):
            source.get("big", max_bytes=9)

    def test_ceiling_spans_multiple_chunks(self) -> None:
        payload = b"y" * (2 * 1024 * 1024)
        source = S3Source(_Client({"big": payload}), "bucket")
        self.assertEqual(len(source.get("big", max_bytes=len(payload))), len(payload))
        with self.assertRaises(ValueError):
            source.get("big", max_bytes=len(payload) - 1)


class LimitsTest(unittest.TestCase):
    def test_declared_read_ceilings(self) -> None:
        self.assertEqual(MAX_METADATA_BYTES, 8 * 1024 * 1024)
        self.assertEqual(MAX_MANIFEST_BYTES, 64 * 1024)
        self.assertEqual(MAX_DEB_BYTES, 128 * 1024 * 1024)

    def test_pool_name_pattern(self) -> None:
        for name in ("zellij_0.45.1-1_amd64.deb", "starship_1.26.0-1_arm64.deb"):
            self.assertTrue(DEB_NAME.match(name), msg=name)
        for name in ("../escape.deb", "/abs.deb", ".hidden.deb", "no-extension"):
            self.assertIsNone(DEB_NAME.match(name), msg=name)


class S3SourceFactoryTest(unittest.TestCase):
    def test_factory_wraps_the_publisher_client(self) -> None:
        client = _Client({"x": b"y"})
        with mock.patch("scripts.storage.build_s3_client", return_value=client) as factory:
            source = s3_source("bucket", endpoint="https://example.test")
        factory.assert_called_once_with("https://example.test")
        self.assertEqual(source.get("x", max_bytes=8), b"y")


if __name__ == "__main__":
    unittest.main()
