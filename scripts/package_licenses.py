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


def cargo_tauri_source_document(pkg: Package) -> str:
    """Render the production `cargo-tauri` source pointer for one package.

    The template lives at `packaging/licenses/cargo-tauri/README.source` and is
    filled from the validated package itself: its version, its amd64 official
    asset URL/SHA, and the immutable source pins already in its arm64 target
    (tag/commit/lock/compiler/build). No extra network lookup happens at
    signing; stale docs fail because smoke checks the rendered version, commit,
    and official digest.
    """
    from .source_build import identity_of

    if pkg.name != "cargo-tauri":
        raise ValueError(f"cargo-tauri source document needs cargo-tauri, got {pkg.name!r}")
    amd64 = pkg.targets.get("amd64")
    arm64 = pkg.targets.get("arm64")
    if amd64 is None or arm64 is None or arm64.source_build is None:
        raise ValueError("cargo-tauri package needs both amd64 asset and arm64 source targets")
    identity = identity_of(arm64.source_build)
    template = (
        Path(__file__).resolve().parent.parent / "packaging" / "licenses"
        / "cargo-tauri" / "README.source"
    ).read_text(encoding="utf-8")
    try:
        return template.format(
            version=pkg.version,
            tag=identity.tag,
            commit=identity.commit,
            manifest=identity.build.manifest,
            lock_blob=identity.lockfile.git_blob,
            lock_bytes=identity.lockfile.bytes,
            toolchain=identity.toolchain.channel,
            target=identity.build.target,
            profile=identity.build.profile,
            command=" ".join(identity.build.command),
            asset_url=amd64.url,
            asset_sha256=amd64.sha256,
        )
    except (KeyError, ValueError) as exc:
        raise ValueError(f"invalid cargo-tauri README.source template: {exc}") from None
