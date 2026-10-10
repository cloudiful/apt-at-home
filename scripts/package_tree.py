"""Install a validated upstream file tree into a Debian package root.

A tree package installs whole reviewed roots (`bin`, `lib`, `share`) under the
package `usr/` directory. Each file keeps the executable bit its archive declared
and everything else is 0644, so the policy is the same root-owned, deterministic
layout the flat binary path uses. The required runtime paths come from the
reviewed recipe and are checked before the package is staged, so an upstream
layout change fails the build instead of shipping a broken tree.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from .archive_tree import TreeInventory, extract_tree


def install_tree(
    archive: Path,
    inventory: TreeInventory,
    usr_root: Path,
    *,
    required: Iterable[str] = (),
) -> tuple[str, ...]:
    """Extract the validated tree under `usr_root` and return the installed paths.

    The caller guarantees `inventory` came from `inventory_tree` on this archive,
    so only the validated members are ever written. Every path in `required`
    (relative to `usr/`) must be present afterwards.
    """
    extract_tree(archive, inventory, usr_root)
    installed = tuple(sorted(
        path.relative_to(usr_root).as_posix()
        for path in usr_root.rglob("*")
        if path.is_file()
    ))
    missing = [path for path in required if path not in installed]
    if missing:
        raise ValueError(f"tree package is missing required paths: {', '.join(missing)}")
    return installed
