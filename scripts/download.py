"""Download pinned archives, verify hashes, extract safely without execution."""

from __future__ import annotations

import tarfile
import urllib.request
from pathlib import Path

from .common import sha256_file


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


def _is_unsafe_name(name: str) -> bool:
    if not name or name.startswith("/") or name.startswith("\\"):
        return True
    parts = Path(name).parts
    if any(part in (".", "..") for part in parts):
        return True
    if "\0" in name:
        return True
    return False


def safe_extract_member(archive: Path, member_name: str, dest: Path) -> Path:
    """Extract one regular-file member; reject escapes, links, and devices."""
    if _is_unsafe_name(member_name):
        raise ValueError(f"refusing to extract unsafe member name: {member_name}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        member = None
        for item in tar.getmembers():
            if _is_unsafe_name(item.name):
                continue
            if item.name == member_name or item.name == f"./{member_name}":
                member = item
                break
        if member is None:
            # Fall back to single-entry archives whose member name is canonical.
            regular = [m for m in tar.getmembers() if m.isfile() and not _is_unsafe_name(m.name)]
            if (
                len(regular) == 1
                and member_name in (regular[0].name, Path(regular[0].name).name)
                and Path(regular[0].name).name == Path(member_name).name
            ):
                member = regular[0]
            else:
                names = ", ".join(m.name for m in tar.getmembers()[:10])
                raise ValueError(f"member {member_name} not found in {archive.name} (saw: {names})")
        if not member.isfile():
            raise ValueError(f"refusing to extract non-regular member {member.name}")
        if member.issym() or member.islnk():
            raise ValueError(f"refusing to extract link member {member.name}")
        # Zip-slip guard: member must be a bare file name (or ./bare).
        stripped = member.name[2:] if member.name.startswith("./") else member.name
        if "/" in stripped or "\\" in stripped or not stripped or stripped in (".", ".."):
            raise ValueError(f"refusing to extract unsafe member {member.name}")
        target = dest
        resolved = target.resolve()
        # Ensure the destination stays inside its parent directory.
        if resolved.parent != dest.resolve().parent and resolved != dest.resolve():
            raise ValueError(f"refusing to extract outside temp directory: {member.name}")
        extracted = tar.extractfile(member)
        if extracted is None:
            raise ValueError(f"cannot read member {member.name}")
        with target.open("wb") as out:
            while True:
                chunk = extracted.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
        target.chmod(0o755)
        return target
