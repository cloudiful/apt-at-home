"""The reviewed package catalog: which names exist and how each is fetched.

The recipe table lives in `scripts.recipes`; this module derives every asset
path, member, binary, dependency, and relation from it, so a manifest may pin any
non-empty subset of the reviewed recipes and never invent a name. Every asset
path is computed from the pinned version, so a version bump recomputes a nested
member instead of reusing a stale prefix.
"""

from __future__ import annotations

import re

from .recipes import RECIPES, Recipe

__all__ = ["ARCHES", "KNOWN_PACKAGES", "RECIPES", "Recipe"]

ARCHES = ("amd64", "arm64")

# Every package name this repository may publish, derived from the recipes so a
# recipe and the accepted name set can never drift apart.
KNOWN_PACKAGES: frozenset[str] = frozenset(RECIPES)

# One safe POSIX path component: no separator, no dot-prefix, no control byte.
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")

# One comma-separated list of `name [ (relation version) ]` terms. The grammar is
# deliberately closed: a shell metacharacter, a control character, or a newline
# simply cannot match, so a relation string can never escape the control file.
_TERM = r"[A-Za-z0-9][A-Za-z0-9.+~:-]*(?: \([<>=]{1,2} [A-Za-z0-9][A-Za-z0-9.+~:-]*\))?"
_RELATIONS = re.compile(rf"^{_TERM}(?:, {_TERM})*$")


def recipe_for(package_name: str) -> Recipe:
    try:
        return RECIPES[package_name]
    except KeyError:
        raise ValueError(f"no reviewed recipe for {package_name!r}") from None


def _arch(arch: str) -> str:
    if arch not in ARCHES:
        raise ValueError(f"unsupported architecture: {arch!r}")
    return arch


def triple_for(package_name: str, arch: str) -> str:
    """Return the upstream target triple this package publishes for an arch."""
    try:
        return recipe_for(package_name).triples[_arch(arch)]
    except KeyError:
        raise ValueError(
            f"{package_name} publishes no {arch} release asset naming"
        ) from None


def repo_of(package_name: str) -> str:
    """Return the only upstream repository this package may be fetched from."""
    return recipe_for(package_name).repo


def tag_for(package_name: str, version: str) -> str:
    """Return the upstream release tag this project uses for a version."""
    return recipe_for(package_name).tag_prefix + version


def asset_name(package_name: str, version: str, arch: str) -> str:
    """Return the only release asset name accepted for this package and arch."""
    recipe = recipe_for(package_name)
    fields = {
        "name": recipe.name,
        "upstream": recipe.upstream,
        "version": version,
        "arch": _arch(arch),
    }
    if "{triple}" in recipe.asset:
        fields["triple"] = triple_for(package_name, fields["arch"])
    return recipe.asset.format(**fields)


def archive_member(package_name: str, version: str, arch: str) -> str:
    """Return the exact archive member that holds this package's binary."""
    recipe = recipe_for(package_name)
    prefix = asset_name(package_name, version, arch).removesuffix(".tar.gz")
    member = recipe.member.format(
        prefix=prefix, binary=recipe.binary, upstream=recipe.upstream
    )
    for component in member.split("/"):
        if not _SAFE_COMPONENT.match(component):
            raise ValueError(f"{package_name} {arch} archive member is unsafe: {member}")
    return member


def install_tree_for(package_name: str, version: str, arch: str) -> str:
    """Return the upstream top root a tree package installs from, or '' when flat."""
    recipe = recipe_for(package_name)
    if not recipe.install_tree:
        return ""
    prefix = asset_name(package_name, version, arch).removesuffix(".tar.gz")
    return recipe.install_tree.format(prefix=prefix)


def required_paths_for(package_name: str) -> tuple[str, ...]:
    """Return the package-relative paths a tree package must carry after install."""
    return recipe_for(package_name).required


def parsers_for(package_name: str) -> tuple[str, ...]:
    """Return the reviewed parser library names a tree package must ship."""
    return recipe_for(package_name).parsers


def binary_name(package_name: str) -> str:
    """Return the file name installed under `/usr/bin` for this package."""
    return recipe_for(package_name).binary


def expected_license_for(package_name: str) -> str:
    """Return the SPDX identifier the reviewed recipe pins for this package."""
    return recipe_for(package_name).license_id


def license_path_for(package_name: str) -> str:
    """Return the license file path inside this package's upstream repository."""
    return recipe_for(package_name).license_path


def depends_for(package_name: str, arch: str) -> str:
    """Return the declared runtime dependencies for one architecture, if any."""
    return recipe_for(package_name).depends.get(_arch(arch), "")


def conflicts_for(package_name: str) -> str:
    """Return the package-level Debian Conflicts relation, or '' when none."""
    return recipe_for(package_name).conflicts


def replaces_for(package_name: str) -> str:
    """Return the package-level Debian Replaces relation, or '' when none."""
    return recipe_for(package_name).replaces


def smoke_args(package_name: str) -> tuple[str, ...]:
    """Return the read-only flags that prove the binary runs, per its own CLI."""
    return recipe_for(package_name).smoke


def check_known_names(names: list[str]) -> None:
    """Refuse a manifest that repeats a package name or names an unreviewed one."""
    repeated = sorted({name for name in names if names.count(name) > 1})
    if repeated:
        raise ValueError(f"manifest repeats package name(s): {', '.join(repeated)}")
    unknown = sorted(set(names) - KNOWN_PACKAGES)
    if unknown:
        raise ValueError(
            f"manifest pins unreviewed package(s): {', '.join(unknown)}; "
            f"reviewed packages are {', '.join(sorted(KNOWN_PACKAGES))}"
        )


def check_relations(label: str, relations: str) -> str:
    """Return a validated Debian relation list (Depends, Conflicts, Replaces), or ''."""
    if not relations:
        return ""
    if not _RELATIONS.match(relations):
        raise ValueError(f"invalid {label}: {relations!r}")
    return relations
