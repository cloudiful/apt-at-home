"""The reviewed package catalog: which names exist and how each is fetched.

The recipe table lives in `scripts.recipes`; this module derives every asset
path, member, binary, dependency, and relation from it, so a manifest may pin any
non-empty subset of the reviewed recipes and never invent a name. Every asset
path is computed from the pinned version, so a version bump recomputes a nested
member instead of reusing a stale prefix.
"""

from __future__ import annotations

import re

from .download import canonical_member
from .recipes import RECIPES, Recipe

__all__ = [
    "ARCHES",
    "KNOWN_NAMES",
    "KNOWN_PACKAGES",
    "RECIPES",
    "Recipe",
    "arches_for",
    "exact_tag_release_for",
    "tag_prefix_for",
]

ARCHES = ("amd64", "arm64")

# Every package name this repository may publish, derived from the recipes so a
# recipe and the accepted name set can never drift apart.
KNOWN_PACKAGES: frozenset[str] = frozenset(RECIPES)

KNOWN_NAMES: frozenset[str] = KNOWN_PACKAGES

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


def arches_for(package_name: str) -> tuple[str, ...]:
    """Return the reviewed architectures this package publishes for.

    The default is both repository architectures; cargo-tauri is the one
    ordinary package with an explicit amd64-only policy because upstream
    publishes no official arm64 asset for it. Support is never inferred from
    triples alone, because rustfs-cli uses no triple placeholders.
    """
    return recipe_for(package_name).arches


def exact_tag_release_for(package_name: str) -> bool:
    """Whether discovery must select this package by its exact stable tag.

    Only the multiproduct tauri-apps/tauri repository needs it: its latest
    release may name another product line, so the canonical CLI tag is
    selected from the tag list instead.
    """
    return recipe_for(package_name).exact_tag_release


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


def tag_prefix_for(package_name: str) -> str:
    """Return the release-tag prefix this project uses before a version."""
    return recipe_for(package_name).tag_prefix


def tag_for(package_name: str, version: str) -> str:
    """Return the upstream release tag this project uses for a version."""
    return recipe_for(package_name).tag_prefix + version


def asset_name(package_name: str, version: str, arch: str) -> str:
    """Return the only release asset name accepted for this package and arch."""
    recipe = recipe_for(package_name)
    checked = _arch(arch)
    if checked not in recipe.arches:
        raise ValueError(
            f"{package_name} publishes no {arch} release asset naming"
        )
    fields = {
        "name": recipe.name,
        "upstream": recipe.upstream,
        "version": version,
        "arch": checked,
    }
    if "{triple}" in recipe.asset:
        fields["triple"] = triple_for(package_name, checked)
    return recipe.asset.format(**fields)


def archive_member(package_name: str, version: str, arch: str) -> str:
    """Return the exact archive member that holds this package's binary."""
    recipe = recipe_for(package_name)
    raw_asset = asset_name(package_name, version, arch)
    prefix = raw_asset.removesuffix(".tar.gz").removesuffix(".tgz")
    member = recipe.member.format(
        prefix=prefix, binary=recipe.binary, upstream=recipe.upstream
    )
    for component in member.split("/"):
        if not _SAFE_COMPONENT.match(component):
            raise ValueError(f"{package_name} {arch} archive member is unsafe: {member}")
    return member


def additional_binaries_for(package_name: str, version: str, arch: str) -> dict[str, str]:
    """Return exact reviewed archive members and installed names beyond the primary."""
    recipe = recipe_for(package_name)
    raw_asset = asset_name(package_name, version, arch)
    prefix = raw_asset.removesuffix(".tar.gz").removesuffix(".tgz")
    primary = archive_member(package_name, version, arch)
    members: dict[str, str] = {}
    names = {recipe.binary}
    for template, binary in recipe.additional_members.items():
        member = canonical_member(template.format(
            prefix=prefix, binary=recipe.binary, upstream=recipe.upstream
        ))
        if member == primary or member in members:
            raise ValueError(f"{package_name} has duplicate reviewed archive member: {member}")
        if not _SAFE_COMPONENT.match(binary) or binary in names:
            raise ValueError(f"{package_name} has unsafe or duplicate installed binary: {binary!r}")
        members[member] = binary
        names.add(binary)
    return members


def extra_license_paths_for(package_name: str) -> tuple[str, ...]:
    """Return reviewed extra license filenames under the package license directory."""
    return recipe_for(package_name).extra_license_paths


def extra_license_urls_for(package_name: str, version: str) -> tuple[str, ...]:
    """Return exact raw-source URLs for the recipe's additional license texts."""
    recipe = recipe_for(package_name)
    tag = tag_for(package_name, version)
    return tuple(
        f"https://raw.githubusercontent.com/{recipe.repo}/{tag}/{path}"
        for path in recipe.extra_license_paths
    )


def install_tree_for(package_name: str, version: str, arch: str) -> str:
    """Return the upstream top root a tree package installs from, or '' when flat."""
    recipe = recipe_for(package_name)
    if not recipe.install_tree:
        return ""
    raw_asset = asset_name(package_name, version, arch)
    prefix = raw_asset.removesuffix(".tar.gz").removesuffix(".tgz")
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
    unknown = sorted(set(names) - KNOWN_NAMES)
    if unknown:
        raise ValueError(
            f"manifest pins unreviewed package(s): {', '.join(unknown)}; "
            f"reviewed packages are {', '.join(sorted(KNOWN_NAMES))}"
        )


def check_relations(label: str, relations: str) -> str:
    """Return a validated Debian relation list (Depends, Conflicts, Replaces), or ''."""
    if not relations:
        return ""
    if not _RELATIONS.match(relations):
        raise ValueError(f"invalid {label}: {relations!r}")
    return relations
