"""Read reviewed vendor license texts and render Debian copyright metadata."""

from __future__ import annotations

import re
from pathlib import Path

from .catalog import extra_license_paths_for
from .manifest import Package


def read_vendor_licenses(pkg: Package) -> tuple[str, ...]:
    root = Path(__file__).resolve().parent.parent / "packaging" / "licenses" / pkg.name
    extras = extra_license_paths_for(pkg.name)
    if len(extras) != len(pkg.extra_license_urls):
        raise ValueError(f"{pkg.name} is missing reviewed extra license provenance")
    return tuple(
        (root / name).read_text(encoding="utf-8")
        for name in ("LICENSE.txt", *extras)
    )


def copyright_text(pkg: Package, license_body: str, *extra_license_bodies: str) -> str:
    bodies = "\n\n".join(
        body.rstrip() for body in (license_body, *extra_license_bodies)
    )
    return (
        "Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\n"
        f"Upstream-Name: {pkg.name}\n"
        f"Upstream-Contact: {pkg.homepage}\n"
        f"Source: {pkg.homepage}\n"
        "\n"
        "Files: *\n"
        f"Copyright: {pkg.copyright}\n"
        f"License: {pkg.license}\n"
        f"{bodies}\n"
    )


def kandev_source_document(version: str, commit: str) -> str:
    """Render the Kandev source pointer against its exact bundle version."""
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError(f"invalid Kandev version: {version!r}")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("invalid Kandev bundle commit")
    template = (
        Path(__file__).resolve().parent.parent / "packaging" / "licenses"
        / "kandev" / "README.source"
    ).read_text(encoding="utf-8")
    try:
        return template.format(version=version, tag=f"v{version}", commit=commit)
    except (KeyError, ValueError) as exc:
        raise ValueError(f"invalid Kandev README.source template: {exc}") from None
