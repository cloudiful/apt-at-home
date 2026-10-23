"""Download pinned archives, verify hashes, extract safely without execution."""

from __future__ import annotations

import re
import tarfile
import urllib.parse
import urllib.request
from pathlib import Path

from .common import sha256_file

# An extracted upstream binary is a single executable, not a tree, so the
# ceiling is generous but finite: a hostile archive cannot stream forever into
# a build directory.
MAX_BINARY_BYTES = 128 * 1024 * 1024

# The npm registry is the one origin whose download may never be redirected
# outside itself, so its archives are fetched with the same-origin policy the
# discovery client uses.
NPM_REGISTRY_HOST = "registry.npmjs.org"

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


def _open(url: str, timeout: int):
    """Return the response of one download request.

    A registry archive is fetched with the same-origin redirect policy the
    discovery client uses, so a registry answer can never send the build to
    another host. Every other origin keeps its own download behavior.
    """
    request = urllib.request.Request(url, headers={"User-Agent": "apt-at-home/1.0"})
    parts = urllib.parse.urlsplit(url)
    if (parts.hostname or "").lower() != NPM_REGISTRY_HOST:
        return urllib.request.urlopen(request, timeout=timeout)
    # Imported here because the redirect policy lives in the discovery module,
    # which reads this one.
    from .upstream import SameOriginRedirectHandler

    opener = urllib.request.build_opener(SameOriginRedirectHandler())
    return opener.open(request, timeout=timeout)


def download_file(url: str, dest: Path, *, max_bytes: int | None = None) -> None:
    """Fetch one archive into `dest`, bounded by an optional reviewed ceiling.

    A ceiling is checked twice: against the size the server declares and
    against the bytes that actually arrive, so neither a lying header nor a
    endless body can pass it. A failed download leaves no partial file behind,
    and it never touches a file that was already there.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = dest.with_name(f".{dest.name}.partial")
    try:
        with _open(url, 120) as response, temp.open("wb") as out:
            declared = response.headers.get("Content-Length")
            if max_bytes is not None and declared is not None:
                try:
                    size = int(declared)
                except ValueError:
                    raise ValueError(
                        f"the response for {url} declares an unusable size: {declared!r}"
                    ) from None
                if size > max_bytes:
                    raise ValueError(
                        f"the response for {url} declares {size} bytes, over the "
                        f"{max_bytes} byte limit"
                    )
            written = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if max_bytes is not None and written > max_bytes:
                    raise ValueError(
                        f"the response for {url} exceeds the {max_bytes} byte limit"
                    )
                out.write(chunk)
        temp.replace(dest)
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def verify_sha256(path: Path, expected: str) -> None:
    actual = sha256_file(path)
    if actual.lower() != expected.lower():
        raise ValueError(f"sha256 mismatch for {path.name}: expected {expected}, got {actual}")


def fetch_verified(
    url: str, expected_sha256: str, dest: Path, *, max_bytes: int | None = None
) -> Path:
    """Return the verified archive, reusing a cached one and fetching it once.

    A cached archive is reused only when it still matches the pinned digest, so
    a stale cache is never mistaken for the pinned bytes, and a reviewed
    ceiling bounds the cached file exactly as it bounds a fresh download: an
    oversized cache is refused without touching the file. A fresh download is
    bounded by the same ceiling and leaves no partial file behind.
    """
    if dest.exists():
        if max_bytes is not None:
            size = dest.stat().st_size
            if size > max_bytes:
                raise ValueError(
                    f"cached {dest.name} is {size} bytes, over the {max_bytes} byte limit"
                )
        verify_sha256(dest, expected_sha256)
        return dest
    download_file(url, dest, max_bytes=max_bytes)
    verify_sha256(dest, expected_sha256)
    return dest


def safe_extract_member(
    archive: Path, member_name: str, dest: Path, *, max_bytes: int | None = None
) -> Path:
    """Extract exactly one declared regular-file member of an archive.

    The archive format follows the official asset's suffix: a `.zip` asset is
    read as a ZIP, every other reviewed asset as a gzipped tarball. The member
    is selected by exact canonical path, never by basename guess, so an archive
    can only ever contribute the file the manifest pinned. Links, directories,
    devices, traversal, and a second entry claiming the same path are all
    refused rather than skipped. The member's ceiling is the reviewed one for
    this package, or the global one when the package declares none.
    """
    wanted = canonical_member(member_name)
    limit = MAX_BINARY_BYTES if max_bytes is None else max_bytes
    if archive.suffix == ".zip":
        # Imported here because the ZIP reader reuses this module's member rule.
        from .archive_zip import safe_extract_zip_member

        return safe_extract_zip_member(archive, wanted, dest, max_bytes=limit)
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
        if member.size > limit:
            raise ValueError(
                f"member {member.name} declares {member.size} bytes, over the "
                f"{limit} limit"
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
                if written > limit:
                    raise ValueError(
                        f"member {member.name} exceeds the {limit} byte limit"
                    )
                out.write(chunk)
    # Ownership and mode come from the packaging policy, never from the tarball.
    dest.chmod(0o755)
    return dest
