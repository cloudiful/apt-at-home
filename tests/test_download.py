import hashlib
import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.download import (
    MAX_BINARY_BYTES,
    canonical_member,
    fetch_verified,
    safe_extract_member,
    verify_sha256,
)


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


if __name__ == "__main__":
    unittest.main()
