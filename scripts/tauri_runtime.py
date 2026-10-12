"""Reviewed Tauri runtime policy shared by candidate and production builds.

The closed SONAME-to-Debian-package mapping lives here once so the candidate
descriptor checker and the production source-artifact consumer cannot drift
apart. A floor is widened only by changing this file together with the
descriptor that pins it; editing JSON alone never widens one.
"""

from __future__ import annotations

from dataclasses import dataclass

from .catalog import check_relations

# The closed runtime mapping: every shared library the reviewed binaries may
# link, the Debian package that carries it, the minimum version this release
# declares, and the symbol-version namespace that version is measured in.
# `None` marks a library that upstream ships without symbol versions: its
# version is still declared, but it is justified by the package that ships it
# rather than by a symbol namespace, and a binary that claims versions from it
# is refused.
# The mapping is pinned in code as well as in the descriptor so a floor cannot
# be widened by editing JSON alone; a wider floor needs both a descriptor and
# a code change. One floor serves both architectures: the libc6 floor is the
# highest one either reviewed binary needs, measured from the native ARM build
# (GLIBC_2.39) and therefore above what the amd64 asset requires (GLIBC_2.34).
# Debian 13 ships 2.41, and the release targets Debian 13 only.
REVIEWED_RUNTIME = {
    "libbz2.so.1.0": ("libbz2-1.0", "1.0.8-6", None),
    "libc.so.6": ("libc6", "2.39", "GLIBC_"),
    "libgcc_s.so.1": ("libgcc-s1", "14.2.0", "GCC_"),
    "liblzma.so.5": ("liblzma5", "5.8.1", "XZ_"),
    "libm.so.6": ("libc6", "2.39", "GLIBC_"),
}


@dataclass(frozen=True)
class RuntimeLibrary:
    """One closed SONAME to Debian package mapping, with its reviewed minimum.

    `symbols` is the namespace its floor is measured in, or `None` for a
    library upstream ships unversioned — a declared version with no symbol
    namespace to check it against.
    """

    soname: str
    package: str
    version: str
    symbols: str | None


def version_key(version: str) -> tuple[int, ...]:
    """Compare package and symbol versions as comparable tuples."""
    parts: list[int] = []
    for part in version.split("."):
        digits = "".join(char for char in part if char.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


_version_key = version_key


def dependency_string(
    libraries: tuple[RuntimeLibrary, ...], extra_depends: str
) -> str:
    """Render the Depends field from the closed mapping, deterministically.

    Two SONAMEs may live in one Debian package (`libc.so.6` and `libm.so.6`),
    so the highest reviewed minimum wins and each package appears once. The
    relation string is grammar-checked before it can reach a control file.
    """
    floors: dict[str, str] = {}
    for library in libraries:
        current = floors.get(library.package)
        if current is None:
            floors[library.package] = library.version
            continue
        if _version_key(library.version) > _version_key(current):
            floors[library.package] = library.version
    terms = [f"{name} (>= {floors[name]})" for name in sorted(floors)]
    if extra_depends:
        terms = [*[term.strip() for term in extra_depends.split(",")], *terms]
    return check_relations("candidate depends", ", ".join(terms))


__all__ = [
    "REVIEWED_RUNTIME",
    "RuntimeLibrary",
    "dependency_string",
    "version_key",
]
