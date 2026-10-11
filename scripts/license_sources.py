"""Fetch and byte-compare the reviewed primary and supplemental license sources."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from .catalog import extra_license_paths_for, license_path_for
from .upstream_assets import license_urls_for

MAX_LICENSE_BYTES = 1024 * 1024


class LicenseClient(Protocol):
    def get_bytes(self, url: str, *, max_bytes: int) -> bytes: ...


def vendor_license_sources(package_name: str) -> dict[str, bytes]:
    """Read the package's primary and recipe-named license texts by source name."""
    root = Path(__file__).resolve().parent.parent / "packaging" / "licenses" / package_name
    primary = license_path_for(package_name)
    paths = (primary, *extra_license_paths_for(package_name))
    return {
        source: (root / ("LICENSE.txt" if index == 0 else source)).read_bytes()
        for index, source in enumerate(paths)
    }


def check_license_sources(
    client: LicenseClient,
    package_name: str,
    tag: str,
    *,
    vendored: Mapping[str, bytes] | None = None,
) -> tuple[str, tuple[str, ...]]:
    """Require exact tagged license bytes and return primary/extra source URLs."""
    paths = (license_path_for(package_name), *extra_license_paths_for(package_name))
    local = dict(vendored if vendored is not None else vendor_license_sources(package_name))
    if set(local) != set(paths):
        raise ValueError(
            f"{package_name} vendored license files do not match the reviewed source list"
        )
    urls = license_urls_for(package_name, tag)
    for path, url in zip(paths, urls, strict=True):
        remote = client.get_bytes(url, max_bytes=MAX_LICENSE_BYTES)
        if remote != local[path]:
            label = "license" if path == paths[0] else f"supplemental license {path}"
            raise ValueError(
                f"{package_name} {label} bytes changed in {tag}; "
                f"human review required before republishing ({url})"
            )
    return urls[0], urls[1:]
