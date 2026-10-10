"""The reviewed package catalog: which names exist and how each is fetched.

One place decides the Debian name, the official upstream repository, the tag
prefix, the release-asset name, the archive member, the installed binary, the
declared dependencies, and the license identity of every package published
here. A manifest may pin any non-empty subset of these recipes and never invent
a name, and every asset path is computed from the pinned version, so a version
bump recomputes a nested member instead of reusing a stale prefix.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType

ARCHES = ("amd64", "arm64")

# Rust target triples each upstream publishes for, which is not always the same
# for every architecture of the same project.
_MUSL = MappingProxyType(
    {"amd64": "x86_64-unknown-linux-musl", "arm64": "aarch64-unknown-linux-musl"}
)
_DELTA = MappingProxyType(
    {"amd64": "x86_64-unknown-linux-musl", "arm64": "aarch64-unknown-linux-gnu"}
)
_NO_TRIPLES: Mapping[str, str] = MappingProxyType({})
_NO_DEPENDS: Mapping[str, str] = MappingProxyType({})


@dataclass(frozen=True)
class Recipe:
    """How one Debian package is obtained from its official upstream.

    `asset` and `member` are format templates. `asset` is filled with `upstream`
    (the project's own asset stem, which is not always the Debian package name),
    `version`, `arch`, and `triple`; `member` is filled with `prefix` (the asset
    name without its `.tar.gz` suffix), `binary`, and `upstream`, so an archive
    nesting its binary under a versioned directory recomputes that prefix.
    """

    name: str
    upstream: str
    repo: str
    tag_prefix: str
    license_id: str
    license_path: str
    asset: str
    member: str
    binary: str
    triples: Mapping[str, str] = _MUSL
    depends: Mapping[str, str] = _NO_DEPENDS
    smoke: tuple[str, ...] = ("--version",)


# zellij and starship publish one asset name per toolchain and never embed the
# version in it; the other three embed it in both the asset and its member path.
_PLAIN_MUSL_TEMPLATE = "{upstream}-{triple}.tar.gz"
_VERSIONED_MUSL_TEMPLATE = "{upstream}-{version}-{triple}.tar.gz"

RECIPES: dict[str, Recipe] = {
    "zellij": Recipe(
        name="zellij",
        upstream="zellij",
        repo="zellij-org/zellij",
        tag_prefix="v",
        license_id="MIT",
        license_path="LICENSE.md",
        asset=_PLAIN_MUSL_TEMPLATE,
        member="{upstream}",
        binary="zellij",
    ),
    "starship": Recipe(
        name="starship",
        upstream="starship",
        repo="starship/starship",
        tag_prefix="v",
        license_id="ISC",
        license_path="LICENSE",
        asset=_PLAIN_MUSL_TEMPLATE,
        member="{upstream}",
        binary="starship",
    ),
    "git-delta": Recipe(
        name="git-delta",
        upstream="delta",
        repo="dandavison/delta",
        tag_prefix="",
        license_id="MIT",
        license_path="LICENSE",
        asset=_VERSIONED_MUSL_TEMPLATE,
        # delta publishes one toolchain per architecture, so the member prefix
        # and the installed binary both move when the version changes.
        member="{prefix}/{binary}",
        binary="delta",
        triples=_DELTA,
        # delta's arm64 build is glibc-linked; its amd64 build is static musl.
        depends=MappingProxyType({"arm64": "libc6 (>= 2.18), libgcc-s1"}),
    ),
    "rustfs-cli": Recipe(
        name="rustfs-cli",
        upstream="rustfs-cli",
        repo="rustfs/cli",
        tag_prefix="v",
        license_id="MIT",
        license_path="LICENSE-MIT",
        asset="{upstream}-linux-{arch}-v{version}.tar.gz",
        member="{binary}",
        binary="rc",
        triples=_NO_TRIPLES,
        smoke=("--help",),
    ),
    "sccache": Recipe(
        name="sccache",
        upstream="sccache",
        repo="mozilla/sccache",
        tag_prefix="v",
        license_id="Apache-2.0",
        license_path="LICENSE",
        asset="{upstream}-v{version}-{triple}.tar.gz",
        member="{prefix}/{binary}",
        binary="sccache",
    ),
}

# Every package name this repository may publish, derived from the recipes so a
# recipe and the accepted name set can never drift apart.
KNOWN_PACKAGES: frozenset[str] = frozenset(RECIPES)

# One safe POSIX path component: no separator, no dot-prefix, no control byte.
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")

# One comma-separated list of `name [ (relation version) ]` terms. The grammar is
# deliberately closed: a shell metacharacter, a control character, or a newline
# simply cannot match, so a dependency string can never escape the control file.
_TERM = r"[A-Za-z0-9][A-Za-z0-9.+~:-]*(?: \([<>=]{1,2} [A-Za-z0-9][A-Za-z0-9.+~:-]*\))?"
_DEPENDS = re.compile(rf"^{_TERM}(?:, {_TERM})*$")


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


def check_depends(name: str, arch: str, depends: str) -> str:
    """Return the validated dependency string, or '' when the target is static."""
    if not depends:
        return ""
    if not _DEPENDS.match(depends):
        raise ValueError(f"invalid depends for {name} {arch}: {depends!r}")
    return depends