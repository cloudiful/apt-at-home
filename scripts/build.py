"""Orchestrate download verification and .deb assembly for all targets."""

from __future__ import annotations

import tempfile
from collections.abc import Iterable
from pathlib import Path

from .archive_tree import inventory_tree
from .catalog import (
    archive_suffix_for,
    max_archive_bytes_for,
    max_binary_bytes_for,
    required_paths_for,
    tag_for,
)
from .common import sha256_file
from .deb import build_deb, stage_package
from .download import fetch_verified
from .manifest import Manifest, Package, deb_filename
from .package_binaries import extract_binaries
from .kandev_bundle import inventory_bundle, stage_kandev_package


def _stage_target(
    pkg: Package, arch: str, manifest: Manifest, archive: Path, tmpdir: Path
) -> Path:
    """Stage one target from its archive: a flat member or a validated upstream tree."""
    target = pkg.targets[arch]
    if target.bundle_type:
        inventory = inventory_bundle(
            archive, expected_version=tag_for(pkg.name, pkg.version), expected_arch=arch,
            bundle_type=target.bundle_type,
        )
        return stage_kandev_package(
            pkg=pkg, arch=arch, manifest=manifest, archive=archive,
            inventory=inventory, stage_root=tmpdir / "stages",
        )
    if target.install_tree:
        inventory = inventory_tree(archive, target.install_tree)
        return stage_package(
            pkg=pkg,
            arch=arch,
            manifest=manifest,
            stage_root=tmpdir / "stages",
            tree=(archive, inventory, required_paths_for(pkg.name)),
        )
    binary_sources = extract_binaries(
        archive,
        target,
        tmpdir / f"{pkg.name}-{arch}-bin",
        max_bytes=max_binary_bytes_for(pkg.name) or None,
    )
    additional = {
        name: source for name, source in binary_sources.items() if name != target.binary
    }
    return stage_package(
        pkg=pkg,
        arch=arch,
        manifest=manifest,
        binary_file=binary_sources[target.binary],
        additional_binary_files=additional,
        stage_root=tmpdir / "stages",
    )


def archive_cache_name(pkg: Package, arch: str) -> str:
    """Return the cache file name for one pinned upstream archive.

    The name carries the official asset's own suffix, so the cached file keeps
    the format the build extracts: a gzipped tarball or a ZIP.
    """
    suffix = archive_suffix_for(pkg.name, pkg.version, arch)
    return f"{pkg.name}-{pkg.version}-{arch}{suffix}"


def build_all(
    manifest: Manifest,
    *,
    pool_dir: Path,
    cache_dir: Path,
    packages: Iterable[str] | None = None,
) -> list[Path]:
    """Build the .deb files; returns the paths in sorted order.

    `packages` narrows the work to the named packages, which lets an incremental
    update rebuild only what changed while the restored pool keeps the rest. The
    default builds every pinned package and every reviewed architecture target.
    """
    pool_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    selected = None if packages is None else set(packages)
    unknown = sorted(selected - {p.name for p in manifest.packages}) if selected else []
    if unknown:
        raise ValueError(f"unknown package selection: {', '.join(unknown)}")
    outputs: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="apt-at-home-build-") as tmp:
        tmpdir = Path(tmp)
        for pkg in sorted(manifest.packages, key=lambda p: p.name):
            if selected is not None and pkg.name not in selected:
                continue
            for arch in sorted(pkg.targets):
                target = pkg.targets[arch]
                archive = cache_dir / archive_cache_name(pkg, arch)
                fetch_verified(
                    target.url,
                    target.sha256,
                    archive,
                    max_bytes=max_archive_bytes_for(pkg.name) or None,
                )
                # Nothing in the archive is executed during the host build.
                stage = _stage_target(pkg, arch, manifest, archive, tmpdir)
                output_deb = pool_dir / deb_filename(pkg, arch, manifest)
                if output_deb.exists():
                    # Same name/version must never change bytes silently.
                    existing_digest = sha256_file(output_deb)
                    build_deb(stage, tmpdir / "candidate.deb")
                    candidate_digest = sha256_file(tmpdir / "candidate.deb")
                    if existing_digest != candidate_digest:
                        raise ValueError(
                            f"refusing to overwrite {output_deb.name} with different bytes"
                        )
                    outputs.append(output_deb)
                else:
                    build_deb(stage, output_deb)
                    outputs.append(output_deb)
    return sorted(outputs)
