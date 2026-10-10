"""Parse APT ``Packages`` stanzas and enforce per-architecture completeness."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .common import sha256_file

REQUIRED_FIELDS = ("Package", "Version", "Architecture", "Filename")


@dataclass(frozen=True)
class Stanza:
    """One ``Packages`` paragraph keyed by its field names."""

    fields: dict[str, str]


def parse_stanzas(text: str) -> list[Stanza]:
    """Split a Packages file into stanzas, ignoring blank separators."""
    stanzas: list[Stanza] = []
    for paragraph in text.strip().split("\n\n"):
        if not paragraph.strip():
            continue
        fields: dict[str, str] = {}
        for line in paragraph.splitlines():
            if ": " in line:
                key, value = line.split(": ", 1)
                fields[key.strip()] = value.strip()
        stanzas.append(Stanza(fields=fields))
    return stanzas


def pool_debs_for_arch(output_root: Path, arch: str) -> set[str]:
    """Return the pool ``.deb`` basenames that belong to ``arch``."""
    return {p.name for p in (output_root / "pool").glob(f"*_{arch}.deb")}


def check_arch_packages(
    text: str,
    *,
    arch: str,
    output_root: Path,
    expected_filenames: set[str],
) -> int:
    """Validate every stanza of one architecture index; returns the stanza count.

    Rejects a missing, duplicated, or foreign-architecture entry even when the
    pool it references is complete and its bytes hash correctly.
    """
    stanzas = parse_stanzas(text)
    seen_filenames: set[str] = set()
    seen_identity: set[tuple[str, str, str]] = set()
    for stanza in stanzas:
        for name in REQUIRED_FIELDS:
            if not stanza.fields.get(name):
                raise ValueError(f"Packages for {arch} has a stanza without {name}")
        if stanza.fields["Architecture"] != arch:
            raise ValueError(
                f"Packages for {arch} lists {stanza.fields['Package']} "
                f"with Architecture: {stanza.fields['Architecture']}"
            )
        filename = stanza.fields["Filename"]
        if not filename.startswith("pool/"):
            raise ValueError(f"Packages Filename must be pool-relative, got: {filename}")
        if filename in seen_filenames:
            raise ValueError(f"duplicate Filename entry in Packages for {arch}: {filename}")
        seen_filenames.add(filename)
        identity = (
            stanza.fields["Package"],
            stanza.fields["Version"],
            stanza.fields["Architecture"],
        )
        if identity in seen_identity:
            raise ValueError(
                f"duplicate package {identity[0]} {identity[1]} {identity[2]} "
                f"in Packages for {arch}"
            )
        seen_identity.add(identity)
        deb = output_root / filename
        if not deb.exists():
            raise ValueError(f"Packages references missing pool file: {filename}")
        if "SHA256" in stanza.fields and sha256_file(deb) != stanza.fields["SHA256"].lower():
            raise ValueError(f"pool hash mismatch for {filename}")

    indexed = {name.removeprefix("pool/") for name in seen_filenames}
    missing = sorted(expected_filenames - indexed)
    if missing:
        raise ValueError(
            f"Packages for {arch} omits pool file(s): {', '.join(missing)}"
        )
    extra = sorted(indexed - expected_filenames)
    if extra:
        raise ValueError(
            f"Packages for {arch} lists unknown pool file(s): {', '.join(extra)}"
        )
    return len(stanzas)
