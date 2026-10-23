"""Reviewed CLI and Kandev standard-server release recipes."""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .recipes import Recipe

_GNU = MappingProxyType({
    "amd64": "x86_64-unknown-linux-gnu",
    "arm64": "aarch64-unknown-linux-gnu",
})
_GNU_LIBC = MappingProxyType({
    "amd64": "libc6 (>= 2.34), libgcc-s1",
    "arm64": "libc6 (>= 2.34), libgcc-s1",
})
_UV_LIBC = MappingProxyType({
    "amd64": "libc6 (>= 2.17), libgcc-s1",
    "arm64": "libc6 (>= 2.28), libgcc-s1",
})
_MUSL = MappingProxyType({
    "amd64": "x86_64-unknown-linux-musl",
    "arm64": "aarch64-unknown-linux-musl",
})
# fzf does not embed a released version or a Rust triple in its asset name.
_NO_TRIPLES = MappingProxyType({})
_TREE_SITTER = MappingProxyType({"amd64": "x64", "arm64": "arm64"})
_TREE_SITTER_LIBC = MappingProxyType({
    "amd64": "libc6 (>= 2.39), libgcc-s1",
    "arm64": "libc6 (>= 2.39), libgcc-s1",
})
# chezmoi's default arm64 archive is static; only the amd64 build is glibc-linked.
_CHEZMOI_LIBC = MappingProxyType({"amd64": "libc6 (>= 2.34)"})
# Bun publishes one ZIP per architecture, with its own short triple names.
_BUN = MappingProxyType({
    "amd64": "x64",
    "arm64": "aarch64",
})
# Bun's Linux binaries link against glibc 2.17 and libgcc.
_BUN_LIBC = MappingProxyType({
    "amd64": "libc6 (>= 2.17), libgcc-s1",
    "arm64": "libc6 (>= 2.17), libgcc-s1",
})
# The Forgejo CLI publishes one GNU tarball per architecture, named with its
# own short architecture names.
_FORGEJO_LIBC = MappingProxyType({"amd64": "x86_64", "arm64": "aarch64"})
# The amd64 build links against OpenSSL and zlib; the arm64 build does not.
_FORGEJO_LIBC_DEPENDS = MappingProxyType({
    "amd64": "libc6 (>= 2.39), libgcc-s1, libssl3t64, zlib1g",
    "arm64": "libc6 (>= 2.39), libgcc-s1",
})
# OpenCode publishes its official CLI through npm: one wrapper package that
# names the platform packages, one platform package per Linux architecture,
# each named with its own short target triple.
_OPENCODE = MappingProxyType({"amd64": "x64", "arm64": "arm64"})
# Both OpenCode builds link against glibc 2.17 and need nothing beyond it.
_OPENCODE_LIBC = MappingProxyType({
    "amd64": "libc6 (>= 2.17)",
    "arm64": "libc6 (>= 2.17)",
})


def make_cli_recipes(recipe_type: type[Recipe]) -> dict[str, Recipe]:
    return {
        "bottom": recipe_type(
            name="bottom",
            upstream="bottom",
            repo="ClementTsang/bottom",
            tag_prefix="",
            license_id="MIT",
            license_path="LICENSE",
            asset="{upstream}_{triple}.tar.gz",
            member="{binary}",
            binary="btm",
            triples=_GNU,
            depends=_GNU_LIBC,
        ),
        "uv": recipe_type(
            name="uv",
            upstream="uv",
            repo="astral-sh/uv",
            tag_prefix="",
            license_id="MIT OR Apache-2.0",
            license_path="LICENSE-MIT",
            asset="{upstream}-{triple}.tar.gz",
            member="{prefix}/{binary}",
            binary="uv",
            triples=_GNU,
            depends=_UV_LIBC,
            additional_members=MappingProxyType({"{prefix}/uvx": "uvx"}),
            extra_license_paths=("LICENSE-APACHE",),
        ),
        "zoxide": recipe_type(
            name="zoxide",
            upstream="zoxide",
            repo="ajeetdsouza/zoxide",
            tag_prefix="v",
            license_id="MIT",
            license_path="LICENSE",
            asset="{upstream}-{version}-{triple}.tar.gz",
            member="{binary}",
            binary="zoxide",
            triples=_MUSL,
        ),
        "kandev": recipe_type(
            name="kandev",
            upstream="kandev",
            repo="kdlbs/kandev",
            tag_prefix="v",
            license_id="AGPL-3.0-or-later",
            license_path="LICENSE",
            asset="{upstream}-linux-{triple}.tar.gz",
            member="kandev/bin/kandev",
            binary="kandev",
            triples=MappingProxyType({"amd64": "x64", "arm64": "arm64"}),
            depends=MappingProxyType({
                "amd64": "libc6 (>= 2.34)",
                "arm64": "libc6 (>= 2.34)",
            }),
            bundle_type="kandev-standard",
        ),
        "bun": recipe_type(
            name="bun",
            upstream="bun",
            repo="oven-sh/bun",
            tag_prefix="bun-v",
            # Upstream's notice is a combined one: Bun itself is MIT, and the
            # published binary statically links LGPL-2.1 and other components.
            license_id="MIT AND LGPL-2.1-or-later",
            license_path="LICENSE.md",
            asset="{upstream}-linux-{triple}.zip",
            member="{prefix}/{binary}",
            binary="bun",
            triples=_BUN,
            depends=_BUN_LIBC,
        ),
        "forgejo-cli": recipe_type(
            name="forgejo-cli",
            upstream="forgejo-cli",
            repo="forgejo-contrib/forgejo-cli",
            # The Forgejo CLI is published on Codeberg, which serves no release
            # digest: its archives are pinned by a locally computed SHA256.
            source="codeberg",
            tag_prefix="v",
            license_id="MIT OR Apache-2.0",
            license_path="LICENSE-MIT",
            asset="{upstream}-{triple}-linux.tar.gz",
            member="{binary}",
            binary="fj",
            triples=_FORGEJO_LIBC,
            depends=_FORGEJO_LIBC_DEPENDS,
            # The CLI reports its version through a subcommand, not a flag.
            smoke=("version",),
            extra_license_paths=("LICENSE-APACHE",),
        ),
        "fd-find": recipe_type(
            name="fd-find",
            upstream="fd",
            repo="sharkdp/fd",
            tag_prefix="v",
            license_id="MIT OR Apache-2.0",
            license_path="LICENSE-MIT",
            asset="{upstream}-v{version}-{triple}.tar.gz",
            # Upstream's archive member is `fd`; Debian installs it as fdfind
            # so it cannot collide with the fdclone package's /usr/bin/fd.
            member="{prefix}/fd",
            binary="fdfind",
            triples=_MUSL,
            extra_license_paths=("LICENSE-APACHE",),
        ),
        "fzf": recipe_type(
            name="fzf",
            upstream="fzf",
            repo="junegunn/fzf",
            tag_prefix="v",
            license_id="MIT",
            license_path="LICENSE",
            asset="{upstream}-{version}-linux_{arch}.tar.gz",
            member="{binary}",
            binary="fzf",
            triples=_NO_TRIPLES,
        ),
        "tree-sitter-cli": recipe_type(
            name="tree-sitter-cli",
            upstream="tree-sitter",
            repo="tree-sitter/tree-sitter",
            tag_prefix="v",
            license_id="MIT",
            license_path="LICENSE",
            asset="{upstream}-cli-linux-{triple}.zip",
            member="{binary}",
            binary="tree-sitter",
            triples=_TREE_SITTER,
            depends=_TREE_SITTER_LIBC,
        ),
        "chezmoi": recipe_type(
            name="chezmoi",
            upstream="chezmoi",
            repo="twpayne/chezmoi",
            tag_prefix="v",
            license_id="MIT",
            license_path="LICENSE",
            asset="{upstream}_{version}_linux_{arch}.tar.gz",
            member="{binary}",
            binary="chezmoi",
            triples=_NO_TRIPLES,
            depends=_CHEZMOI_LIBC,
        ),
        "opencode": recipe_type(
            name="opencode",
            upstream="opencode",
            repo="anomalyco/opencode",
            tag_prefix="v",
            license_id="MIT",
            license_path="LICENSE",
            # OpenCode V2 is distributed only through the official npm
            # registry: the wrapper names the platform packages, and each
            # reviewed architecture uses the ordinary (non-baseline) build.
            source="npm",
            npm_package="@opencode/cli",
            npm_packages=MappingProxyType({
                "amd64": "@opencode/cli-linux-x64",
                "arm64": "@opencode/cli-linux-arm64",
            }),
            # The release comes from the registry, while the tagged license
            # text lives in the project's own repository.
            license_source="github",
            asset="cli-linux-{triple}-{version}.tgz",
            member="package/bin/opencode",
            binary="opencode",
            triples=_OPENCODE,
            depends=_OPENCODE_LIBC,
            # The official archive is about 114MiB and its binary about
            # 240MiB, above the legacy ceilings, so this reviewed package
            # carries its own reviewed ones.
            max_archive_bytes=128 * 1024 * 1024,
            max_binary_bytes=256 * 1024 * 1024,
        ),
        "glab": recipe_type(
            name="glab",
            upstream="glab",
            repo="gitlab-org/cli",
            tag_prefix="v",
            license_id="MIT",
            license_path="LICENSE",
            # GitLab publishes the release archives, checksums, and the tagged
            # license on its own host. Both Linux builds are static Go ELFs.
            source="gitlab",
            asset="{upstream}_{version}_linux_{arch}.tar.gz",
            member="bin/{binary}",
            binary="glab",
            triples=_NO_TRIPLES,
            # The CLI reports its version through a subcommand, not a flag.
            smoke=("version",),
        ),
    }
