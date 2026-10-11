"""Reviewed release recipes for the bottom, uv, and zoxide command-line tools."""

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
    }
