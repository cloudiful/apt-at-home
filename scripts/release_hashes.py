"""Parse the digest sections of an APT Release document once, for every reader.

`Release` carries `MD5Sum`, `SHA256`, and `SHA512` blocks whose entries are
`<digest> <size> <path>`. Local verification and the restore trust chain must
read the same blocks with the same rules, so the regex lives here and both
import it instead of keeping their own copies.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

HASH_SECTIONS = ("SHA256", "SHA512")

_ENTRY = re.compile(r"^\s+([0-9a-f]+)\s+(\d+)\s+(\S+)\s*$")


def parse_release_section(release_text: str, section: str) -> dict[str, tuple[str, int]]:
    """Return one section's entries as `path -> (digest, size)`.

    Digests are normalized to lowercase so callers compare against hashlib
    output without repeating `.lower()`. Parsing stops at the first line that
    is neither indented nor blank, so an adjacent section is never merged in.
    """
    out: dict[str, tuple[str, int]] = {}
    in_section = False
    for line in release_text.splitlines():
        if line.strip() == f"{section}:":
            in_section = True
            continue
        if in_section:
            if line and not line.startswith(" "):
                break
            match = _ENTRY.match(line)
            if match:
                digest, size, name = match.groups()
                out[name] = (digest.lower(), int(size))
    return out


def parse_release_hashes(
    release_text: str, sections: Sequence[str] = HASH_SECTIONS
) -> dict[str, dict[str, tuple[str, int]]]:
    """Return the requested sections keyed by section name."""
    return {section: parse_release_section(release_text, section) for section in sections}
