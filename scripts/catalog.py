"""The reviewed package catalog: which names exist and how each is fetched.

The recipe table lives in `scripts.recipes`; this module derives every asset
path, member, binary, dependency, and relation from it, so a manifest may pin any
non-empty subset of the reviewed recipes and never invent a name. Every asset
path is computed from the pinned version, so a version bump recomputes a nested
member instead of reusing a stale prefix.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .download import canonical_member
from .recipes import RECIPES, Recipe

__all__ = [
    "ARCHES",
    "KNOWN_NAMES",
    "KNOWN_PACKAGES",
    "RECIPES",
    "Recipe",
    "archive_suffix_for",
    "arches_for",
    "canonical_license_source_for",
    "canonical_source_for",
    "exact_tag_release_for",
    "fork_label_for",
    "fork_tag_from_version",
    "fork_version_from_tag",
    "fork_version_parts",
    "license_source_for",
    "max_archive_bytes_for",
    "max_binary_bytes_for",
    "npm_package_for",
    "npm_packages_for",
    "npm_platform_package_for",
    "source_for",
    "tag_prefix_for",
]

ARCHES = ("amd64", "arm64")

# The archive formats the reviewed recipes publish: a gzipped tarball or a ZIP.
# The suffix is part of the official asset name, so it decides both the archive
# member prefix and the format the build extracts.
_ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".zip")

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

# The reviewed personal-use forks, keyed by the upstream repository that
# publishes them: a fork names its releases `<base>-<label>.<N>` beside the
# upstream tags it carries. Only the registered label of the registered
# repository is ever accepted, so no other recipe can read a fork suffix and
# no other suffix can pass as one.
_FORK_LABELS: Mapping[str, str] = MappingProxyType(
    {"cloudiful/forgejo-cli": "cloudiful"}
)

# One fork release tag: a strictly numeric base version, the fork label, and a
# positive revision.
_FORK_TAG = re.compile(r"^(\d+)\.(\d+)\.(\d+)-([a-z][a-z0-9]*)\.(\d+)$")
# The Debian upstream version the same release publishes: the base version with
# the fork label and revision as `+<label>.<N>`.
_FORK_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)\+([a-z][a-z0-9]*)\.(\d+)$")

@dataclass(frozen=True)
class PublishedOrigin:
    """The upstream that published one already released version of a package."""

    version: str
    source: str
    repo: str
    license_source: str


# The one published origin that is not the current recipe: the Forgejo CLI was
# published from its Codeberg release before the personal-use fork took over,
# so that published version keeps naming the release it was published from and
# the updater can still read the release it published. The record is exact: one
# package and one version, so no other version and no other package can name
# another upstream.
_PUBLISHED_ORIGINS: Mapping[str, PublishedOrigin] = MappingProxyType({
    "forgejo-cli": PublishedOrigin(
        version="0.7.0",
        source="codeberg",
        repo="forgejo-contrib/forgejo-cli",
        license_source="codeberg",
    ),
})


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

    The default is both repository architectures; a recipe narrows the set only
    when its upstream publishes no official asset for one architecture. Support
    is never inferred from triples alone, because rustfs-cli uses no triple
    placeholders.
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


def source_for(package_name: str) -> str:
    """Return the reviewed upstream host this package's release comes from."""
    return recipe_for(package_name).source


def license_source_for(package_name: str) -> str:
    """Return the reviewed host this package's license text comes from.

    A package whose release comes from one host may publish its license text
    on another reviewed host, so the license origin is its own reviewed value.
    """
    recipe = recipe_for(package_name)
    return recipe.license_source or recipe.source


def _published_origin(package_name: str, version: str) -> PublishedOrigin | None:
    """Return the recorded published origin of one exact version, or None."""
    origin = _PUBLISHED_ORIGINS.get(package_name)
    if origin is None or origin.version != version:
        return None
    return origin


def canonical_source_for(package_name: str, version: str) -> tuple[str, str]:
    """Return the (source, repo) that published one exact version.

    A version comes from one reviewed release. A package whose upstream moved
    keeps naming the source its already published version came from, so the
    updater can still read the release it published, while every other version
    comes from the current recipe.
    """
    origin = _published_origin(package_name, version)
    if origin is not None:
        return origin.source, origin.repo
    return source_for(package_name), repo_of(package_name)


def canonical_license_source_for(package_name: str, version: str) -> str:
    """Return the host that published one exact version's license text."""
    origin = _published_origin(package_name, version)
    if origin is not None:
        return origin.license_source
    return license_source_for(package_name)


def npm_package_for(package_name: str) -> str:
    """Return the npm wrapper package this package's platform packages live in."""
    return recipe_for(package_name).npm_package


def npm_packages_for(package_name: str) -> Mapping[str, str]:
    """Return the npm platform package published for each reviewed arch."""
    return recipe_for(package_name).npm_packages


def npm_platform_package_for(package_name: str, arch: str) -> str:
    """Return the npm platform package published for one reviewed arch."""
    try:
        return npm_packages_for(package_name)[_arch(arch)]
    except KeyError:
        raise ValueError(
            f"{package_name} publishes no {arch} npm platform package"
        ) from None


def max_archive_bytes_for(package_name: str) -> int:
    """Return this package's reviewed archive ceiling, or 0 for the legacy one."""
    return recipe_for(package_name).max_archive_bytes


def max_binary_bytes_for(package_name: str) -> int:
    """Return this package's reviewed member ceiling, or 0 for the legacy one."""
    return recipe_for(package_name).max_binary_bytes


def tag_prefix_for(package_name: str) -> str:
    """Return the release-tag prefix this project uses before a version."""
    return recipe_for(package_name).tag_prefix


def fork_label_for(package_name: str) -> str:
    """Return the reviewed fork label of one package's upstream, or ''.

    The label belongs to the upstream repository that publishes the fork's
    releases, so a package whose reviewed repository is the fork reads its
    fork tags and every other package keeps the plain stable parser.
    """
    return _FORK_LABELS.get(recipe_for(package_name).repo, "")


def _fork_parts(
    pattern: re.Pattern[str], text: str
) -> tuple[str, tuple[int, int, int, int]] | None:
    """Return the label and the numeric parts of one fork tag or version."""
    match = pattern.match(text)
    if match is None:
        return None
    revision = int(match.group(5))
    if revision < 1:
        return None
    parts = (int(match.group(1)), int(match.group(2)), int(match.group(3)), revision)
    return match.group(4), parts


def fork_version_from_tag(package_name: str, tag: str) -> str | None:
    """Return the version one reviewed fork tag publishes, or None.

    A fork tag is `<base>-<label>.<N>` and the version it publishes is
    `<base>+<label>.<N>`. Only the package's own reviewed label is accepted
    and the revision must be a positive number, so a plain upstream tag or any
    other suffix never passes as a reviewed fork release.
    """
    label = fork_label_for(package_name)
    if not label:
        return None
    parsed = _fork_parts(_FORK_TAG, tag)
    if parsed is None or parsed[0] != label:
        return None
    base, revision = parsed[1][:3], parsed[1][3]
    return f"{base[0]}.{base[1]}.{base[2]}+{label}.{revision}"


def fork_tag_from_version(package_name: str, version: str) -> str | None:
    """Return the fork tag one version publishes, or None."""
    label = fork_label_for(package_name)
    if not label:
        return None
    parsed = _fork_parts(_FORK_VERSION, version)
    if parsed is None or parsed[0] != label:
        return None
    base, revision = parsed[1][:3], parsed[1][3]
    return f"{base[0]}.{base[1]}.{base[2]}-{label}.{revision}"


def fork_version_parts(version: str) -> tuple[int, int, int, int] | None:
    """Return the base version and revision of one reviewed fork version.

    Only a version whose label is a reviewed fork label qualifies, so an
    arbitrary `+suffix` never passes as a fork version.
    """
    parsed = _fork_parts(_FORK_VERSION, version)
    if parsed is None or parsed[0] not in _FORK_LABELS.values():
        return None
    return parsed[1]


def tag_for(package_name: str, version: str) -> str:
    """Return the upstream release tag this project uses for a version."""
    recipe = recipe_for(package_name)
    fork = fork_tag_from_version(package_name, version)
    if fork is not None:
        return recipe.tag_prefix + fork
    return recipe.tag_prefix + version


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


def archive_suffix_for(package_name: str, version: str, arch: str) -> str:
    """Return the archive suffix this package's official release asset uses."""
    asset = asset_name(package_name, version, arch)
    for suffix in _ARCHIVE_SUFFIXES:
        if asset.endswith(suffix):
            return suffix
    raise ValueError(f"{package_name} {arch} release asset has no reviewed suffix: {asset}")


def _asset_prefix(asset: str) -> str:
    """Return the asset name without its archive suffix."""
    for suffix in _ARCHIVE_SUFFIXES:
        if asset.endswith(suffix):
            return asset[: -len(suffix)]
    raise ValueError(f"unsupported release asset suffix: {asset}")


def archive_member(package_name: str, version: str, arch: str) -> str:
    """Return the exact archive member that holds this package's binary."""
    recipe = recipe_for(package_name)
    prefix = _asset_prefix(asset_name(package_name, version, arch))
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
    prefix = _asset_prefix(raw_asset)
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
    # The official URL forms live in `upstream_assets`, which reads this
    # module; the import is deferred so the two stay acyclic.
    from .upstream_assets import license_url

    recipe = recipe_for(package_name)
    tag = tag_for(package_name, version)
    source, repo = canonical_source_for(package_name, version)
    return tuple(
        license_url(
            repo, tag, path,
            source=canonical_license_source_for(package_name, version),
        )
        for path in recipe.extra_license_paths
    )


def install_tree_for(package_name: str, version: str, arch: str) -> str:
    """Return the upstream top root a tree package installs from, or '' when flat."""
    recipe = recipe_for(package_name)
    if not recipe.install_tree:
        return ""
    raw_asset = asset_name(package_name, version, arch)
    prefix = _asset_prefix(raw_asset)
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
