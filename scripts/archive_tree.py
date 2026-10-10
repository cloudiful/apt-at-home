"""Validate and extract a reviewed upstream file tree without trusting the archive.

A tree target names one upstream top root (`nvim-linux-<arch>`). Its `bin`,
`lib`, and `share` subtrees are the installable roots, and every member is
checked before a single byte is written: the path must canonicalize under the
top root, the entry must be a plain file or a directory, and the archive must
stay within the declared count and size limits. Nothing is ever extracted with
`extractall`, and no link, device, or setuid bit is honoured.
"""

from __future__ import annotations

import posixpath
import re
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

from .download import canonical_member

ALLOWED_ROOTS = ("bin", "lib", "share")
MAX_MEMBERS = 4096
MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
_CHUNK = 1024 * 1024

# A tree component may begin with an underscore (upstream ships `_meta.lua`),
# but never a dot, so `.`, `..`, and hidden names stay refused.
_TREE_COMPONENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._+-]*$")


@dataclass(frozen=True)
class TreeFile:
    """One regular file to install, relative to the package's `usr/` directory."""

    relative: str
    source: str
    executable: bool


@dataclass
class TreeInventory:
    """Everything a validated tree archive contributes, computed before extraction."""

    root: str
    files: list[TreeFile] = field(default_factory=list)
    directories: set[str] = field(default_factory=set)


def top_root(install_tree: str) -> str:
    """Return the upstream top root that holds the installable bin, lib, and share.

    `install_tree` is one path component, the directory the archive nests every
    member under (`nvim-linux-x86_64`). A nested or dot path is refused, so the
    installable roots are always the archive's own bin, lib, and share.
    """
    try:
        relative = canonical_member(install_tree)
    except ValueError:
        raise ValueError(f"unsafe install_tree: {install_tree!r}") from None
    if "/" in relative or relative in ALLOWED_ROOTS:
        raise ValueError(f"install_tree must be one upstream top directory: {install_tree!r}")
    return relative


def inventory_tree(archive: Path, install_tree: str) -> TreeInventory:
    """Validate every member under the top root and list the installable files.

    Every member of the archive is checked, so a hostile entry anywhere in the
    tarball fails the whole archive. Only regular files under the reviewed
    `bin`, `lib`, and `share` subtrees are installed; any other file is refused.
    """
    prefix = top_root(install_tree)
    inventory = TreeInventory(root=prefix)
    seen: set[str] = set()
    total = 0
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        if len(members) > MAX_MEMBERS:
            raise ValueError(f"archive carries {len(members)} members, over the {MAX_MEMBERS} limit")
        for item in members:
            relative = _archive_path(item.name, prefix)
            if relative in seen:
                raise ValueError(f"archive declares member {item.name!r} more than once")
            seen.add(relative)
            if item.issym() or item.islnk() or item.isdev() or item.isfifo():
                raise ValueError(f"refusing link or special member: {item.name!r}")
            if item.mode & 0o6000:
                raise ValueError(f"refusing setuid or setgid member: {item.name!r}")
            if relative == prefix:
                if not item.isdir():
                    raise ValueError(f"upstream top root must be a directory: {item.name!r}")
                continue
            installed = relative[len(prefix) + 1 :]
            if installed.split("/", 1)[0] not in ALLOWED_ROOTS:
                raise ValueError(f"path outside bin, lib, and share is not installable: {item.name!r}")
            if item.isdir():
                inventory.directories.add(installed)
                continue
            if not item.isfile():
                raise ValueError(f"refusing non-regular member: {item.name!r}")
            if item.size > MAX_FILE_BYTES:
                raise ValueError(f"member {item.name!r} exceeds the {MAX_FILE_BYTES} byte limit")
            total += item.size
            if total > MAX_TOTAL_BYTES:
                raise ValueError(f"tree exceeds the {MAX_TOTAL_BYTES} byte total limit")
            inventory.files.append(
                TreeFile(
                    relative=installed,
                    source=item.name,
                    executable=bool(item.mode & 0o111),
                )
            )
    if not inventory.files:
        raise ValueError(f"install_tree {prefix} holds no regular files in {archive.name}")
    _reject_file_directory_conflicts(inventory)
    return inventory


def _archive_path(name: str, prefix: str) -> str:
    """Canonical member path; anything outside the top root is refused."""
    stripped = name.rstrip("/") if name.endswith("/") else name
    if not stripped or stripped.startswith("/") or "\\" in stripped or "\0" in stripped:
        raise ValueError(f"unsafe archive member name: {name!r}")
    if any(ord(char) < 32 or ord(char) == 127 for char in stripped):
        raise ValueError(f"unsafe archive member name: {name!r}")
    if stripped.startswith("./"):
        stripped = stripped[2:]
    components = stripped.split("/")
    if any(not _TREE_COMPONENT.match(c) for c in components):
        raise ValueError(f"unsafe archive member name: {name!r}")
    if stripped != prefix and not stripped.startswith(f"{prefix}/"):
        raise ValueError(f"archive member outside the expected prefix: {name!r}")
    return stripped


def _reject_file_directory_conflicts(inventory: TreeInventory) -> None:
    """Refuse any path used as a file that is also a directory or an ancestor of one.

    Ancestors come from both declared directories and files, so a file that sits
    above a declared child, and a declared directory that sits under a file, are
    both caught.
    """
    file_paths = {f.relative for f in inventory.files}
    directory_paths = set(inventory.directories)
    ancestors_of_all = _ancestors(file_paths | directory_paths)
    ancestors_of_directories = _ancestors(directory_paths)
    clash = sorted(
        (file_paths & ancestors_of_all)
        | (file_paths & directory_paths)
        | (ancestors_of_directories & file_paths)
    )
    if clash:
        raise ValueError(f"path is both file and directory: {clash[0]}")


def _ancestors(paths: set[str]) -> set[str]:
    """Every proper ancestor directory of the given relative paths."""
    found: set[str] = set()
    for path in paths:
        parent = posixpath.dirname(path)
        while parent:
            found.add(parent)
            parent = posixpath.dirname(parent)
    return found


def extract_tree(archive: Path, inventory: TreeInventory, dest_root: Path) -> None:
    """Copy the validated files into `dest_root`, which must not already exist."""
    if dest_root.exists():
        raise ValueError(f"tree destination already exists: {dest_root}")
    dest_root.mkdir(parents=True, mode=0o755)
    wanted = {f.source: f for f in inventory.files}
    with tarfile.open(archive, "r:gz") as tar:
        for item in tar.getmembers():
            tree_file = wanted.pop(item.name, None)
            if tree_file is None:
                continue
            source = tar.extractfile(item)
            if source is None:
                raise ValueError(f"cannot read member {item.name}")
            target = dest_root / tree_file.relative
            target.parent.mkdir(parents=True, exist_ok=True)
            _copy_bounded(source, target, item.size, item.name)
            target.chmod(0o755 if tree_file.executable else 0o644)
    if wanted:
        raise ValueError(f"validated members vanished during extraction: {sorted(wanted)}")


def _copy_bounded(source, target: Path, declared: int, name: str) -> None:
    """Stream one member, failing if its bytes disagree with the declared size."""
    written = 0
    with target.open("wb") as out:
        while True:
            chunk = source.read(_CHUNK)
            if not chunk:
                break
            written += len(chunk)
            if written > declared or written > MAX_FILE_BYTES:
                raise ValueError(f"member {name!r} streams more bytes than declared")
            out.write(chunk)
    if written != declared:
        raise ValueError(f"member {name!r} streamed {written} bytes, declared {declared}")
