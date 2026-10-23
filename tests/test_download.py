import hashlib
import io
import tarfile
import tempfile
import unittest
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from unittest import mock
from urllib.response import addinfourl

from scripts.download import (
    MAX_BINARY_BYTES,
    canonical_member,
    download_file,
    fetch_verified,
    safe_extract_member,
    verify_sha256,
)
from scripts.upstream import SameOriginRedirectHandler


class _FakeResponse:
    """One answered request, with the size the server may declare."""

    def __init__(self, payload: bytes, declared: str | None = None) -> None:
        self._payload = payload
        self.headers: dict[str, str] = {}
        if declared is not None:
            self.headers["Content-Length"] = declared

    def read(self, size: int) -> bytes:
        chunk = self._payload[:size]
        self._payload = self._payload[size:]
        return chunk

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> bool:
        return False


class _RedirectTransport(urllib.request.HTTPSHandler):
    """A real urllib HTTPS transport that answers from a fixed route table."""

    def __init__(self, routes: dict[str, tuple[int, dict[str, str], bytes]]) -> None:
        super().__init__()
        self.routes = routes
        self.requests: list[str] = []

    def https_open(self, req):  # type: ignore[no-untyped-def]
        self.requests.append(req.full_url)
        status, extra, body = self.routes.get(req.full_url, (404, {}, b"missing"))
        message = Message()
        for key, value in extra.items():
            message[key] = value
        response = addinfourl(io.BytesIO(body), message, req.full_url, status)
        response.msg = "Injected"
        return response


def _make_tarball(
    path: Path,
    members: dict[str, bytes],
    *,
    symlink: tuple[str, str] | None = None,
    hardlink: tuple[str, str] | None = None,
) -> None:
    with tarfile.open(path, "w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mode = 0o644
            info.mtime = 0
            tar.addfile(info, io.BytesIO(data))
        if symlink is not None:
            link_name, link_target = symlink
            info = tarfile.TarInfo(name=link_name)
            info.type = tarfile.SYMTYPE
            info.linkname = link_target
            tar.addfile(info)
        if hardlink is not None:
            link_name, link_target = hardlink
            info = tarfile.TarInfo(name=link_name)
            info.type = tarfile.LNKTYPE
            info.linkname = link_target
            tar.addfile(info)


class CanonicalMemberTest(unittest.TestCase):
    def test_accepts_bare_nested_and_dot_prefixed_paths(self) -> None:
        self.assertEqual(canonical_member("starship"), "starship")
        self.assertEqual(canonical_member("rc"), "rc")
        self.assertEqual(
            canonical_member("sccache-v0.18.0-x86_64-unknown-linux-musl/sccache"),
            "sccache-v0.18.0-x86_64-unknown-linux-musl/sccache",
        )
        self.assertEqual(
            canonical_member("./delta-0.20.1-aarch64-unknown-linux-gnu/delta"),
            "delta-0.20.1-aarch64-unknown-linux-gnu/delta",
        )

    def test_rejects_unsafe_paths(self) -> None:
        for evil in ("", "/etc/passwd", "../escape", "a/../../escape", "a/./b",
                     "a//b", "a/", "./", "a\\b", ".", "..", "x\ny", "x\x00y", "-rc"):
            with self.assertRaises(ValueError, msg=evil):
                canonical_member(evil)


class DownloadTest(unittest.TestCase):
    def test_verify_sha256_accepts_and_rejects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "file.bin"
            target.write_bytes(b"payload")
            expected = hashlib.sha256(b"payload").hexdigest()
            verify_sha256(target, expected)
            with self.assertRaises(ValueError):
                verify_sha256(target, "0" * 64)

    def test_fetch_verified_reuses_matching_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "archive.tar.gz"
            dest.write_bytes(b"cached")
            digest = hashlib.sha256(b"cached").hexdigest()
            # No network: existing matching file must be reused without download.
            self.assertEqual(fetch_verified("https://example.invalid/x", digest, dest), dest)

    def test_safe_extract_single_member(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "a.tar.gz"
            _make_tarball(archive, {"starship": b"ELF-binary"})
            dest = Path(tmp) / "out" / "starship"
            safe_extract_member(archive, "starship", dest)
            self.assertEqual(dest.read_bytes(), b"ELF-binary")
            # Modes come from the packaging policy, never from the tarball.
            self.assertEqual(dest.stat().st_mode & 0o777, 0o755)

    def test_safe_extract_nested_member_of_a_versioned_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "sccache.tar.gz"
            prefix = "sccache-v0.18.0-x86_64-unknown-linux-musl"
            _make_tarball(archive, {
                f"{prefix}/README.md": b"docs",
                f"{prefix}/LICENSE": b"apache",
                f"{prefix}/sccache": b"ELF-sccache",
            })
            dest = Path(tmp) / "out" / "sccache"
            safe_extract_member(archive, f"{prefix}/sccache", dest)
            self.assertEqual(dest.read_bytes(), b"ELF-sccache")

    def test_safe_extract_accepts_the_single_dot_prefix_form(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "a.tar.gz"
            _make_tarball(archive, {"./delta/delta": b"ELF-delta"})
            dest = Path(tmp) / "out" / "delta"
            safe_extract_member(archive, "delta/delta", dest)
            self.assertEqual(dest.read_bytes(), b"ELF-delta")

    def test_safe_extract_never_guesses_by_basename(self) -> None:
        """A same-named file elsewhere in the tree is not the declared member."""
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "a.tar.gz"
            _make_tarball(archive, {
                "sccache-v0.18.0-x86_64-unknown-linux-musl/sccache": b"ELF-sccache",
            })
            with self.assertRaises(ValueError) as ctx:
                safe_extract_member(archive, "sccache", Path(tmp) / "out")
            self.assertIn("not found", str(ctx.exception))

    def test_safe_extract_rejects_duplicate_canonical_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "dup.tar.gz"
            _make_tarball(archive, {
                "rc": b"first",
                "./rc": b"second",
            })
            with self.assertRaises(ValueError) as ctx:
                safe_extract_member(archive, "rc", Path(tmp) / "out")
            self.assertIn("2 entries", str(ctx.exception))

    def test_safe_extract_rejects_missing_member(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "a.tar.gz"
            _make_tarball(archive, {"other": b"x"})
            with self.assertRaises(ValueError):
                safe_extract_member(archive, "starship", Path(tmp) / "out")

    def test_safe_extract_rejects_absolute_and_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for evil in ("/etc/passwd", "../escape", "a/../../escape"):
                archive = Path(tmp) / "evil.tar.gz"
                _make_tarball(archive, {evil: b"x"})
                with self.assertRaises(ValueError, msg=evil):
                    safe_extract_member(archive, evil, Path(tmp) / "out")

    def test_safe_extract_rejects_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "link.tar.gz"
            _make_tarball(archive, {}, symlink=("starship", "/etc/passwd"))
            with self.assertRaises(ValueError):
                safe_extract_member(archive, "starship", Path(tmp) / "out")

    def test_safe_extract_rejects_hardlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "hard.tar.gz"
            _make_tarball(
                archive, {"starship": b"x"}, hardlink=("delta", "starship"),
            )
            with self.assertRaises(ValueError):
                safe_extract_member(archive, "delta", Path(tmp) / "out")

    def test_safe_extract_rejects_a_directory_member(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "dir.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                info = tarfile.TarInfo("sccache-v0.18.0-x86_64-unknown-linux-musl")
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
            with self.assertRaises(ValueError):
                safe_extract_member(
                    archive, "sccache-v0.18.0-x86_64-unknown-linux-musl",
                    Path(tmp) / "out",
                )

    def test_safe_extract_enforces_the_binary_size_ceiling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "big.tar.gz"
            _make_tarball(archive, {"rc": b"0123456789"})
            with mock.patch("scripts.download.MAX_BINARY_BYTES", 4):
                with self.assertRaises(ValueError) as ctx:
                    safe_extract_member(archive, "rc", Path(tmp) / "out")
            self.assertIn(str(4), str(ctx.exception))
        self.assertEqual(MAX_BINARY_BYTES, 128 * 1024 * 1024)

    def test_safe_extract_honours_a_reviewed_member_ceiling(self) -> None:
        """A recipe's own ceiling replaces the global one for its members."""
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "opencode.tar.gz"
            _make_tarball(archive, {"package/bin/opencode": b"0123456789"})
            with self.assertRaises(ValueError) as ctx:
                safe_extract_member(
                    archive, "package/bin/opencode", Path(tmp) / "out", max_bytes=4
                )
            self.assertIn("4", str(ctx.exception))
            self.assertFalse((Path(tmp) / "out").exists())


class BoundedDownloadTest(unittest.TestCase):
    """A download is bounded by its reviewed ceiling and leaves no partial file."""

    URL = "https://registry.npmjs.org/@opencode/cli-linux-x64/-/cli-linux-x64-2.1.3.tgz"

    def test_a_declared_size_over_the_ceiling_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "archive.tgz"
            response = _FakeResponse(b"0123456789", declared="10")
            with mock.patch("scripts.download._open", return_value=response):
                with self.assertRaises(ValueError) as ctx:
                    download_file(self.URL, dest, max_bytes=4)
            self.assertIn("declares 10 bytes", str(ctx.exception))
            self.assertFalse(dest.exists())

    def test_streamed_bytes_over_the_ceiling_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "archive.tgz"
            response = _FakeResponse(b"0123456789")
            with mock.patch("scripts.download._open", return_value=response):
                with self.assertRaises(ValueError) as ctx:
                    download_file(self.URL, dest, max_bytes=4)
            self.assertIn("exceeds the 4 byte limit", str(ctx.exception))
            self.assertFalse(dest.exists())

    def test_a_failed_download_leaves_no_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "archive.tgz"
            response = _FakeResponse(b"0123456789")
            with mock.patch("scripts.download._open", return_value=response):
                with self.assertRaises(ValueError):
                    download_file(self.URL, dest, max_bytes=4)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_a_verified_cache_is_reused_and_never_refetched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "archive.tgz"
            dest.write_bytes(b"cached")
            digest = hashlib.sha256(b"cached").hexdigest()
            with mock.patch("scripts.download._open") as opener:
                self.assertEqual(
                    fetch_verified(self.URL, digest, dest, max_bytes=6), dest
                )
            opener.assert_not_called()

    def test_a_cached_archive_over_the_ceiling_is_refused_before_reuse(self) -> None:
        """A reviewed ceiling bounds a cache hit exactly as it bounds a fetch."""
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "archive.tgz"
            dest.write_bytes(b"12345")
            digest = hashlib.sha256(b"12345").hexdigest()
            with mock.patch("scripts.download._open") as opener:
                with self.assertRaises(ValueError) as ctx:
                    fetch_verified(self.URL, digest, dest, max_bytes=4)
            opener.assert_not_called()
            self.assertIn("over the 4 byte limit", str(ctx.exception))
            # The oversized cached file is neither deleted nor rewritten.
            self.assertTrue(dest.exists())
            self.assertEqual(dest.read_bytes(), b"12345")

    def test_a_cached_archive_at_the_ceiling_is_reused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "archive.tgz"
            dest.write_bytes(b"1234")
            digest = hashlib.sha256(b"1234").hexdigest()
            with mock.patch("scripts.download._open") as opener:
                self.assertEqual(
                    fetch_verified(self.URL, digest, dest, max_bytes=4), dest
                )
            opener.assert_not_called()

    def test_an_unset_ceiling_keeps_the_legacy_cache_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "archive.tgz"
            dest.write_bytes(b"0123456789")
            digest = hashlib.sha256(b"0123456789").hexdigest()
            with mock.patch("scripts.download._open") as opener:
                self.assertEqual(fetch_verified(self.URL, digest, dest), dest)
            opener.assert_not_called()

    def test_a_stale_cache_is_refused_and_left_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "archive.tgz"
            dest.write_bytes(b"stale")
            with self.assertRaises(ValueError):
                fetch_verified(self.URL, "0" * 64, dest, max_bytes=5)
            self.assertEqual(dest.read_bytes(), b"stale")

    def test_a_registry_archive_is_fetched_with_the_same_origin_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "archive.tgz"
            opener = mock.MagicMock()
            opener.open.return_value = _FakeResponse(b"archive")
            with mock.patch("urllib.request.build_opener", return_value=opener) as build:
                download_file(self.URL, dest)
            handlers = build.call_args[0]
            self.assertTrue(
                any(isinstance(h, SameOriginRedirectHandler) for h in handlers)
            )
            self.assertEqual(dest.read_bytes(), b"archive")

    def test_another_origin_keeps_its_own_download_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "archive.tgz"
            url = "https://github.com/x/y/releases/download/v1.0.0/x.tar.gz"
            with mock.patch(
                "urllib.request.urlopen", return_value=_FakeResponse(b"archive")
            ) as urlopen:
                with mock.patch("urllib.request.build_opener") as build:
                    download_file(url, dest)
            self.assertTrue(urlopen.called)
            build.assert_not_called()

    def test_a_registry_redirect_to_another_origin_is_never_followed(self) -> None:
        transport = _RedirectTransport({
            self.URL: (302, {"Location": "https://evil.example/take"}, b""),
        })
        real_build = urllib.request.build_opener

        def build_with_transport(*handlers: object) -> object:
            return real_build(transport, *handlers)

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "archive.tgz"
            with mock.patch("urllib.request.build_opener", build_with_transport):
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    download_file(self.URL, dest)
            ctx.exception.close()
            self.assertIn("cross-origin", str(ctx.exception))
            self.assertEqual(transport.requests, [self.URL])
            self.assertFalse(dest.exists())

    def test_a_registry_redirect_with_a_query_or_fragment_is_never_followed(self) -> None:
        """The build download enforces the canonical identity too, not just discovery."""
        real_build = urllib.request.build_opener

        for suffix in ("?alternate=1", "#alternate"):
            with self.subTest(suffix=suffix):
                target = self.URL + suffix
                transport = _RedirectTransport({
                    self.URL: (302, {"Location": target}, b""),
                })

                def build_with_transport(*handlers: object, transport=transport) -> object:
                    return real_build(transport, *handlers)

                with tempfile.TemporaryDirectory() as tmp:
                    dest = Path(tmp) / "archive.tgz"
                    with mock.patch("urllib.request.build_opener", build_with_transport):
                        with self.assertRaises(urllib.error.HTTPError) as ctx:
                            download_file(self.URL, dest)
                    ctx.exception.close()
                    self.assertIn("registry redirect", str(ctx.exception))
                    # The altered destination was refused before it was requested.
                    self.assertEqual(transport.requests, [self.URL])
                    self.assertFalse(dest.exists())


if __name__ == "__main__":
    unittest.main()
