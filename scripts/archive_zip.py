"""Bounded extraction of one declared member from a release ZIP.

A release archive is untrusted input: every entry is inspected before a byte is
written, and the archive is refused unless the pinned member is exactly one
regular file. Nothing is ever unpacked wholesale, and the selected member is
copied as a bounded stream whose real size and CRC are both verified, so a
truncated or corrupted payload fails instead of publishing a partial binary.
"""

from __future__ import annotations

import binascii
import stat
import zipfile
from pathlib import Path

from .download import MAX_BINARY_BYTES, canonical_member

_CHUNK = 1024 * 1024

# ZIP's only real compression methods; anything else would need a codec we do
# not want to guess at.
_SUPPORTED_COMPRESSION = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}

# The encrypted flag; an encrypted member is never opened.
_ENCRYPTED_FLAG = 0x1


def _declared_type(info: zipfile.ZipInfo) -> int | None:
    """The Unix file type carried by `external_attr`, or None when unstated.

    Many ordinary ZIP writers record the Unix mode but leave the file-type bits
    at zero, and others record no Unix mode at all. Zero type bits mean the type
    was never stated: they are not a claim about a symlink, so such an entry
    stays a regular file.
    """
    mode = info.external_attr >> 16
    if not mode:
        return None
    return stat.S_IFMT(mode) or None


def _entry_path(info: zipfile.ZipInfo) -> str:
    """Return the canonical path of one archive entry.

    A directory entry is spelled as its path plus a trailing separator; the
    separator is removed before the same path rule every other entry obeys, so
    a directory can never be spelled two ways.
    """
    name = info.orig_filename
    if name.endswith("/"):
        stripped = name[:-1]
        if not stripped or stripped.endswith("/"):
            raise ValueError(f"unsafe archive member name: {name!r}")
        return canonical_member(stripped)
    return canonical_member(name)


def safe_extract_zip_member(archive: Path, member_name: str, dest: Path) -> Path:
    """Extract exactly one declared regular-file member of a ZIP archive.

    The member is selected by exact canonical path, never by basename guess, so
    an archive can only ever contribute the file the manifest pinned. Unsafe
    paths, links, devices, encrypted entries, unsupported compression, and a
    second entry claiming the same path are refused rather than skipped.
    """
    wanted = canonical_member(member_name)
    written = 0
    crc = 0
    with zipfile.ZipFile(archive) as zf:
        matches: list[zipfile.ZipInfo] = []
        seen: set[str] = set()
        for info in zf.infolist():
            if info.flag_bits & _ENCRYPTED_FLAG:
                raise ValueError(f"refusing encrypted member: {info.orig_filename!r}")
            if info.compress_type not in _SUPPORTED_COMPRESSION:
                raise ValueError(
                    f"unsupported compression for member: {info.orig_filename!r}"
                )
            mode = (info.external_attr >> 16) & 0o7777
            if mode & 0o6000:
                raise ValueError(f"refusing setuid or setgid member: {info.orig_filename!r}")
            declared = _declared_type(info)
            path = _entry_path(info)
            if info.orig_filename.endswith("/"):
                if declared is not None and declared != stat.S_IFDIR:
                    raise ValueError(f"refusing non-directory entry: {info.orig_filename!r}")
                if info.file_size:
                    raise ValueError(f"directory entry carries data: {info.orig_filename!r}")
            elif declared is not None and declared != stat.S_IFREG:
                raise ValueError(f"refusing non-regular member: {info.orig_filename!r}")
            if path in seen:
                raise ValueError(f"archive declares member {path!r} more than once")
            seen.add(path)
            if path == wanted:
                matches.append(info)
        if not matches:
            raise ValueError(f"member {wanted} not found in {archive.name}")
        member = matches[0]
        if member.orig_filename.endswith("/"):
            raise ValueError(f"refusing to extract directory member {member.orig_filename!r}")
        if member.file_size > MAX_BINARY_BYTES:
            raise ValueError(
                f"member {member.orig_filename} declares {member.file_size} bytes, over the "
                f"{MAX_BINARY_BYTES} limit"
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        temp = dest.with_name(f".{dest.name}.partial")
        try:
            with zf.open(member) as source, temp.open("wb") as out:
                while True:
                    chunk = source.read(_CHUNK)
                    if not chunk:
                        break
                    written += len(chunk)
                    crc = binascii.crc32(chunk, crc)
                    if written > MAX_BINARY_BYTES:
                        raise ValueError(
                            f"member {member.orig_filename} exceeds the "
                            f"{MAX_BINARY_BYTES} byte limit"
                        )
                    out.write(chunk)
            if written != member.file_size:
                raise ValueError(
                    f"member {member.orig_filename} streamed {written} bytes, "
                    f"declared {member.file_size}"
                )
            if crc != member.CRC:
                raise ValueError(f"member {member.orig_filename} failed its CRC check")
            temp.replace(dest)
        except Exception:
            temp.unlink(missing_ok=True)
            raise
    # Ownership and mode come from the packaging policy, never from the archive.
    dest.chmod(0o755)
    return dest
