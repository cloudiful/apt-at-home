"""The reviewed upstream recipe table: how each Debian package is fetched and laid out.

Each `Recipe` pins one package's official repository, tag prefix, asset and member
templates, installed binary, declared dependencies, package-level relations, and
license identity. `scripts.catalog` is the only consumer that derives values from
these records.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

# Rust target triples each upstream publishes for, which is not always the same
# for every architecture of the same project.
_MUSL = MappingProxyType(
    {"amd64": "x86_64-unknown-linux-musl", "arm64": "aarch64-unknown-linux-musl"}
)
_DELTA = MappingProxyType(
    {"amd64": "x86_64-unknown-linux-musl", "arm64": "aarch64-unknown-linux-gnu"}
)
_NO_TRIPLES: Mapping[str, str] = MappingProxyType({})
_NEOVIM = MappingProxyType({"amd64": "x86_64", "arm64": "arm64"})
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
    conflicts: str = ""
    replaces: str = ""
    smoke: tuple[str, ...] = ("--version",)
    # A tree package installs the upstream top root's bin, lib, and share. The
    # top root is `install_tree` filled from `prefix` (the asset stem), and
    # `required` lists package-relative paths that must exist after install.
    install_tree: str = ""
    required: tuple[str, ...] = ()
    # Tree-sitter parser libraries under lib/nvim/parser, each checked for the
    # package architecture's ELF machine.
    parsers: tuple[str, ...] = ()


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
    "neovim": Recipe(
        name="neovim",
        upstream="nvim",
        repo="neovim/neovim",
        tag_prefix="v",
        license_id="Neovim",
        license_path="LICENSE.txt",
        asset="nvim-linux-{triple}.tar.gz",
        member="{prefix}/bin/nvim",
        binary="nvim",
        triples=_NEOVIM,
        depends=MappingProxyType({
            "amd64": "libc6 (>= 2.34), libgcc-s1",
            "arm64": "libc6 (>= 2.34), libgcc-s1",
        }),
        conflicts="neovim-runtime",
        replaces="neovim-runtime",
        install_tree="{prefix}",
        required=(
            "bin/nvim",
            "lib/nvim/parser/lua.so",
            "share/nvim/runtime/doc/help.txt",
            "share/nvim/runtime/scripts/less.sh",
            "share/nvim/runtime/doc/uganda.txt",
            "share/nvim/runtime/pack/dist/opt/netrw/LICENSE.txt",
            "share/man/man1/nvim.1",
            "share/applications/nvim.desktop",
            "share/icons/hicolor/128x128/apps/nvim.png",
        ),
        parsers=(
            "c", "lua", "markdown_inline", "markdown", "query", "vimdoc", "vim",
        ),
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
