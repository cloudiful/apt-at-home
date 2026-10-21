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
    }
