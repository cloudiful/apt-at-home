"""Bounded, hostile-resistant inspection of a single-member release ZIP.

An upstream release archive is untrusted input: this module inventories *every*
member before a byte is written, and refuses the whole archive unless each
entry is safe. Nothing is ever extracted with `extractall`, and the selected
member is copied as a bounded stream so a declared size that disagrees with the
real bytes, or a bad CRC, fails instead of producing a truncated binary.

The type check is deliberately conservative in the one place ZIP is ambiguous:
the Unix mode in `external_attr` is authoritative when it carries a file type,
but many ordinary ZIP writers (including Python's) leave those bits zero for a
plain regular file, so a zero type is read as a regular file rather than being
mistaken for a symlink.
"""

from __future__ import annotations

import binascii
import re
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path

# One safe POSIX path component: no separator, no dot-prefix, no control byte.
_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")

MAX_MEMBERS = 32
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024

_CHUNK = 1024 * 1024

# ZIP's only real compression methods; anything else would need a codec we do
# not want to guess at.
_SUPPORTED_COMPRESSION = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}

_ENCRYPTED_FLAG = 0x1


@dataclass(frozen=True)
class ZipMember:
    """One accepted regular file, with the facts a reviewer needs."""

    name: str
    file_size: int
    compress_size: int
    crc: int

    @property
    def is_empty_dir_entry(self) -> bool:
        return self.file_size == 0


def canonical_member(name: str) -> str:
    """Validate an archive member path and return its canonical relative form."""
    if not name or "\\" in name or "\0" in name or name.startswith("/"):
        raise ValueError(f"unsafe zip member name: {name!r}")
    if any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise ValueError(f"unsafe zip member name: {name!r}")
    relative = name[2:] if name.startswith("./") else name
    if not relative or relative.endswith("/"):
        raise ValueError(f"unsafe zip member name: {name!r}")
    for component in relative.split("/"):
        if not _COMPONENT.match(component):
            raise ValueError(f"unsafe zip member name: {name!r}")
    return relative


def _declared_type(info: zipfile.ZipInfo) -> int | None:
    """The Unix file type carried by `external_attr`, or None when unstated.

    Many ordinary ZIP writers record the Unix mode but leave the file-type bits
    at zero, and others record no Unix mode at all. Zero type bits mean the type
    was never stated — they are not a claim about a symlink, and not a claim that
    the entry is a device. Only a nonzero type is read as a real declaration, so
    such an entry stays a regular file.
    """
    mode = info.external_attr >> 16
    if not mode:
        return None
    file_type = stat.S_IFMT(mode)
    return file_type or None


def canonical_directory(name: str) -> str:
    """Validate a directory entry's spelling and return its canonical form.

    A directory is spelled as its path plus at most one trailing separator.
    `root/` is canonical; `root//` and `root///` are aliases of the same path
    and are refused rather than silently folded, so two spellings can never be
    admitted as two different directories.
    """
    if not name.endswith("/"):
        raise ValueError(f"unsafe zip directory name: {name!r}")
    stripped = name[:-1]
    if stripped.endswith("/"):
        raise ValueError(f"non-canonical zip directory name: {name!r}")
    if not stripped:
        raise ValueError(f"unsafe zip directory name: {name!r}")
    return canonical_member(stripped)


def _ancestors(path: str) -> set[str]:
    """Every proper ancestor directory of a canonical member path."""
    parts = path.split("/")
    return {"/".join(parts[:index]) for index in range(1, len(parts))}


def inventory(archive: Path, selected: str | None = None) -> tuple[ZipMember, ...]:
    """Inspect every member and return the accepted regular files.

    Every member is validated, so a hostile entry anywhere in the archive fails
    the whole archive rather than being skipped. Names are checked against the
    original stored spelling (`orig_filename`), because `zipfile` truncates
    `filename` at a NUL byte and would otherwise hide the rest of the name from
    every check. Files and directories share one `seen` set, so a repeated path
    is a duplicate whichever kind it is.

    When `selected` names the member the caller intends to install, every
    directory must be one of its ancestors; an unrelated directory is refused.
    Without `selected` only the structural rules apply, because the intended
    member is not yet known.

    Links, devices, fifos, encrypted entries, unsupported compression,
    setuid/setgid bits, duplicates, and a path that is both a file and a
    directory are all refused.
    """
    size = archive.stat().st_size
    if size > MAX_ARCHIVE_BYTES:
        raise ValueError(f"archive is {size} bytes, over the {MAX_ARCHIVE_BYTES} limit")

    members: list[ZipMember] = []
    directories: set[str] = set()
    seen: set[str] = set()
    total = 0
    with zipfile.ZipFile(archive) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_MEMBERS:
            raise ValueError(f"archive carries {len(infos)} members, over the {MAX_MEMBERS} limit")
        for info in infos:
            if info.flag_bits & _ENCRYPTED_FLAG:
                raise ValueError(f"refusing encrypted member: {info.orig_filename!r}")
            if info.compress_type not in _SUPPORTED_COMPRESSION:
                raise ValueError(f"unsupported compression for member: {info.orig_filename!r}")
            declared = _declared_type(info)
            mode = (info.external_attr >> 16) & 0o7777
            if mode & 0o6000:
                raise ValueError(f"refusing setuid or setgid member: {info.orig_filename!r}")
            if info.orig_filename.endswith("/"):
                # A root directory entry is expected and carries S_IFDIR, so the
                # directory branch is taken before the regular-file type check.
                if declared is not None and declared != stat.S_IFDIR:
                    raise ValueError(f"refusing non-directory entry: {info.orig_filename!r}")
                relative = canonical_directory(info.orig_filename)
                if info.file_size:
                    raise ValueError(f"directory entry carries data: {info.orig_filename!r}")
                # A directory may only be added where no already-seen file is
                # the same path or an ancestor of it, in either order.
                if any(
                    other.name == relative or relative.startswith(f"{other.name}/")
                    for other in members
                ):
                    raise ValueError(f"directory {relative!r} collides with a declared file")
                if relative in seen:
                    raise ValueError(f"archive declares directory {relative!r} more than once")
                seen.add(relative)
                directories.add(relative)
                continue
            if declared is not None and declared != stat.S_IFREG:
                raise ValueError(f"refusing non-regular member: {info.orig_filename!r}")
            relative = canonical_member(info.orig_filename)
            # A file living *inside* a declared directory is the normal shape of
            # a release ZIP. Only the reverse collisions are faults: the same
            # path being both a file and a directory, or a directory nested
            # under a path that is itself a file.
            if relative in directories:
                raise ValueError(f"member is also declared as a directory: {relative!r}")
            if any(d.startswith(f"{relative}/") for d in directories):
                raise ValueError(f"member {relative!r} would have to be a directory")
            for other in members:
                if relative.startswith(f"{other.name}/"):
                    raise ValueError(f"member {relative!r} is inside file {other.name!r}")
                if other.name.startswith(f"{relative}/"):
                    raise ValueError(f"member {other.name!r} is inside file {relative!r}")
            if relative in seen:
                raise ValueError(f"archive declares member {relative!r} more than once")
            seen.add(relative)
            if info.file_size > MAX_FILE_BYTES:
                raise ValueError(f"member {relative!r} exceeds the {MAX_FILE_BYTES} byte limit")
            total += info.file_size
            if total > MAX_TOTAL_BYTES:
                raise ValueError(f"archive exceeds the {MAX_TOTAL_BYTES} byte total limit")
            members.append(
                ZipMember(
                    name=relative,
                    file_size=info.file_size,
                    compress_size=info.compress_size,
                    crc=info.CRC,
                )
            )
    if not members:
        raise ValueError(f"archive holds no regular file: {archive.name}")
    if selected is not None:
        wanted = canonical_member(selected)
        allowed = _ancestors(wanted)
        unrelated = sorted(directories - allowed)
        if unrelated:
            raise ValueError(f"archive carries unrelated directories: {unrelated}")
    return tuple(members)


def select_single_member(members: tuple[ZipMember, ...], expected: str) -> ZipMember:
    """Return the one member the candidate pin names, refusing anything else."""
    wanted = canonical_member(expected)
    matches = [m for m in members if m.name == wanted]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one member {wanted!r}, found {len(matches)}")
    extra = sorted(m.name for m in members if m.name != wanted)
    if extra:
        raise ValueError(f"archive carries unexpected files beside {wanted!r}: {extra}")
    return matches[0]


def extract_member(archive: Path, member: ZipMember, dest: Path, *, mode: int = 0o755) -> Path:
    """Stream one validated member to `dest`, refusing partial output on any fault.

    The copy is bounded by the declared size and verified against both the real
    streamed byte count and the member CRC, so a truncated or corrupted member
    fails before `dest` is published.
    """
    if dest.exists():
        raise ValueError(f"extraction destination already exists: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = dest.with_name(f".{dest.name}.partial")
    try:
        with zipfile.ZipFile(archive) as zf, zf.open(member.name) as source:
            written = 0
            with temp.open("wb") as out:
                while True:
                    chunk = source.read(_CHUNK)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > member.file_size:
                        raise ValueError(f"member {member.name!r} streams past its declared size")
                    out.write(chunk)
        if written != member.file_size:
            raise ValueError(
                f"member {member.name!r} streamed {written} bytes, declared {member.file_size}"
            )
        if _crc32(archive, member.name) != member.crc:
            raise ValueError(f"member {member.name!r} failed its CRC check")
        temp.replace(dest)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    dest.chmod(mode)
    return dest


def _crc32(archive: Path, name: str) -> int:
    """Re-read the decompressed member and return its CRC, so corruption is caught."""
    crc = 0
    with zipfile.ZipFile(archive) as zf, zf.open(name) as source:
        while True:
            chunk = source.read(_CHUNK)
            if not chunk:
                break
            crc = binascii.crc32(chunk, crc)
    return crc & 0xFFFFFFFF