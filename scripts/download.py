"""Download pinned archives, verify hashes, extract safely without execution."""

from __future__ import annotations

import re
import tarfile
import urllib.request
from pathlib import Path

from .common import sha256_file

# An extracted upstream binary is a single executable, not a tree, so the
# ceiling is generous but finite: a hostile archive cannot stream forever into
# a build directory.
MAX_BINARY_BYTES = 128 * 1024 * 1024

# One safe POSIX path component: no separator, no dot-prefix, no control byte.
_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")


def canonical_member(name: str) -> str:
    """Validate an archive member path and return its canonical relative form.

    Nested paths are allowed because upstream release archives are prefixed with
    a versioned directory, but the path must be relative, must use POSIX
    separators, and may carry at most a single `./` prefix. Traversal, absolute
    paths, backslashes, empty components, and dot components are refused here so
    the same rule binds manifest parsing and extraction.
    """
    if not name or "\\" in name or "\0" in name or name.startswith("/"):
        raise ValueError(f"unsafe archive member name: {name!r}")
    if any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise ValueError(f"unsafe archive member name: {name!r}")
    relative = name[2:] if name.startswith("./") else name
    if not relative or relative.endswith("/"):
        raise ValueError(f"unsafe archive member name: {name!r}")
    for component in relative.split("/"):
        if not _COMPONENT.match(component):
            raise ValueError(f"unsafe archive member name: {name!r}")
    return relative


def download_file(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "apt-at-home/1.0"})
    with urllib.request.urlopen(request, timeout=120) as response, dest.open("wb") as out:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)


def verify_sha256(path: Path, expected: str) -> None:
    actual = sha256_file(path)
    if actual.lower() != expected.lower():
        raise ValueError(f"sha256 mismatch for {path.name}: expected {expected}, got {actual}")


def fetch_verified(url: str, expected_sha256: str, dest: Path) -> Path:
    if dest.exists():
        verify_sha256(dest, expected_sha256)
        return dest
    download_file(url, dest)
    verify_sha256(dest, expected_sha256)
    return dest


def safe_extract_member(archive: Path, member_name: str, dest: Path) -> Path:
    """Extract exactly one declared regular-file member of an archive.

    The member is selected by exact canonical path, never by basename guess, so
    an archive can only ever contribute the file the manifest pinned. Links,
    directories, devices, traversal, and a second entry claiming the same path
    are all refused rather than skipped.
    """
    wanted = canonical_member(member_name)
    with tarfile.open(archive, "r:gz") as tar:
        matches: list[tarfile.TarInfo] = []
        for item in tar.getmembers():
            try:
                if canonical_member(item.name) != wanted:
                    continue
            except ValueError:
                # An entry we could never select safely is not our member.
                continue
            matches.append(item)
        if not matches:
            raise ValueError(f"member {wanted} not found in {archive.name}")
        if len(matches) > 1:
            names = ", ".join(sorted(m.name for m in matches))
            raise ValueError(
                f"archive declares {len(matches)} entries for member {wanted} (saw: {names})"
            )
        member = matches[0]
        if not member.isfile():
            raise ValueError(f"refusing to extract non-regular member {member.name}")
        if member.issym() or member.islnk():
            raise ValueError(f"refusing to extract link member {member.name}")
        if member.size > MAX_BINARY_BYTES:
            raise ValueError(
                f"member {member.name} declares {member.size} bytes, over the "
                f"{MAX_BINARY_BYTES} limit"
            )
        extracted = tar.extractfile(member)
        if extracted is None:
            raise ValueError(f"cannot read member {member.name}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with dest.open("wb") as out:
            while True:
                chunk = extracted.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_BINARY_BYTES:
                    raise ValueError(
                        f"member {member.name} exceeds the {MAX_BINARY_BYTES} byte limit"
                    )
                out.write(chunk)
    # Ownership and mode come from the packaging policy, never from the tarball.
    dest.chmod(0o755)
    return dest
