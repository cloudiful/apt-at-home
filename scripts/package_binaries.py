"""Bounded archive extraction and deterministic staging for flat commands."""

from __future__ import annotations

import re
import shutil
import stat
from collections.abc import Mapping
from pathlib import Path

from .download import canonical_member, safe_extract_member
from .manifest import ArchTarget

_BINARY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")


def _members(target: ArchTarget) -> list[tuple[str, str]]:
    declarations = [(target.binary, target.archive_member)] + [
        (binary, member) for member, binary in target.additional_binaries.items()
    ]
    names: set[str] = set()
    members: set[str] = set()
    for name, member in declarations:
        if not _BINARY.fullmatch(name) or name in names:
            raise ValueError(f"unsafe or duplicate installed binary: {name!r}")
        canonical = canonical_member(member)
        if member != canonical or canonical in members:
            raise ValueError(f"duplicate or non-canonical archive member: {member!r}")
        names.add(name)
        members.add(canonical)
    if target.install_tree and target.additional_binaries:
        raise ValueError("tree targets cannot declare additional binaries")
    return declarations


def extract_binaries(archive: Path, target: ArchTarget, destination: Path) -> dict[str, Path]:
    """Extract each exact reviewed flat member without executing its contents."""
    sources: dict[str, Path] = {}
    for name, member in _members(target):
        source = destination / name
        safe_extract_member(archive, member, source)
        sources[name] = source
    return sources


def stage_binaries(
    destination: Path,
    primary_name: str,
    primary_file: Path,
    additional_files: Mapping[str, Path] | None = None,
) -> None:
    """Copy validated regular command files into the package's /usr/bin tree."""
    files = [(primary_name, primary_file), *(additional_files or {}).items()]
    names: set[str] = set()
    for name, source in files:
        if not _BINARY.fullmatch(name) or name in names:
            raise ValueError(f"unsafe or duplicate installed binary: {name!r}")
        try:
            regular = stat.S_ISREG(source.lstat().st_mode)
        except OSError:
            regular = False
        if not regular:
            raise ValueError(f"binary source is not a regular file: {source}")
        names.add(name)
    destination.mkdir(parents=True, exist_ok=True)
    for name, source in files:
        shutil.copyfile(source, destination / name)
