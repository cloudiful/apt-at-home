"""Assemble deterministic .deb packages from verified upstream binaries."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .common import require_tool
from .manifest import Manifest, Package, deb_filename, debian_version

# Fixed per-source epoch so identical manifests rebuild byte-identical .debs.
# Matches the static changelog date below (2026-01-01 00:00:00 UTC).
SOURCE_DATE_EPOCH = 1767225600


def debian_control(pkg: Package, arch: str, manifest: Manifest, installed_size_kb: int) -> str:
    return (
        f"Package: {pkg.name}\n"
        f"Version: {debian_version(pkg, manifest)}\n"
        f"Section: {manifest.section}\n"
        f"Priority: {manifest.priority}\n"
        f"Architecture: {arch}\n"
        f"Maintainer: {manifest.maintainer}\n"
        f"Installed-Size: {installed_size_kb}\n"
        f"Homepage: {pkg.homepage}\n"
        f"Description: {pkg.description}\n"
        f" Repackaged upstream musl binary for a private APT repository.\n"
    )


def copyright_text(pkg: Package, license_body: str) -> str:
    return (
        f"Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\n"
        f"Upstream-Name: {pkg.name}\n"
        f"Upstream-Contact: {pkg.homepage}\n"
        f"Source: {pkg.homepage}\n"
        f"\n"
        f"Files: *\n"
        f"Copyright: {pkg.copyright}\n"
        f"License: {pkg.license}\n"
        f"{license_body.rstrip()}\n"
    )


def read_vendor_license(package_name: str) -> str:
    here = Path(__file__).resolve().parent.parent
    path = here / "packaging" / "licenses" / package_name / "LICENSE.txt"
    return path.read_text(encoding="utf-8")


def stage_package(
    *,
    pkg: Package,
    arch: str,
    manifest: Manifest,
    binary_file: Path,
    stage_root: Path,
) -> Path:
    """Create the staging tree; never executes the upstream binary."""
    binary_name = pkg.targets[arch].binary
    stage = stage_root / f"{pkg.name}_{arch}"
    if stage.exists():
        shutil.rmtree(stage)
    bin_dir = stage / "usr" / "bin"
    doc_dir = stage / "usr" / "share" / "doc" / pkg.name
    debian_dir = stage / "DEBIAN"
    bin_dir.mkdir(parents=True, exist_ok=True)
    doc_dir.mkdir(parents=True, exist_ok=True)
    debian_dir.mkdir(parents=True, exist_ok=True)

    dest_binary = bin_dir / binary_name
    shutil.copyfile(binary_file, dest_binary)

    license_body = read_vendor_license(pkg.name)
    (doc_dir / "copyright").write_text(copyright_text(pkg, license_body), encoding="utf-8")
    (doc_dir / "changelog.Debian").write_text(
        f"{pkg.name} ({debian_version(pkg, manifest)}) stable; urgency=low\n\n"
        f"  * Repackage upstream {pkg.version} musl {arch} binary.\n\n"
        f" -- {manifest.maintainer}  Thu, 01 Jan 2026 00:00:00 +0000\n",
        encoding="utf-8",
    )

    installed_kb = max(1, sum(p.stat().st_size for p in stage.rglob("*") if p.is_file()) // 1024)
    (debian_dir / "control").write_text(
        debian_control(pkg, arch, manifest, installed_kb), encoding="utf-8"
    )
    _apply_deterministic_modes(stage)
    _clamp_mtimes(stage, SOURCE_DATE_EPOCH)
    return stage


def _apply_deterministic_modes(stage: Path) -> None:
    """Root-owned layout: dirs/executables 0755, docs and control 0644."""
    for directory in sorted(p for p in stage.rglob("*") if p.is_dir()):
        directory.chmod(0o755)
    stage.chmod(0o755)
    for path in sorted(p for p in stage.rglob("*") if p.is_file()):
        relative = path.relative_to(stage).as_posix()
        if relative in ("DEBIAN/control",) or relative.startswith("usr/share/doc/"):
            path.chmod(0o644)
        elif relative.startswith("usr/bin/"):
            path.chmod(0o755)
        else:
            path.chmod(0o644)


def _clamp_mtimes(stage: Path, epoch: int) -> None:
    for path in list(stage.rglob("*")) + [stage]:
        os.utime(path, (epoch, epoch))


def build_deb(stage: Path, output_deb: Path) -> None:
    require_tool("dpkg-deb")
    output_deb.parent.mkdir(parents=True, exist_ok=True)
    # Guard the immutable contract: never silently replace different bytes.
    env = dict(os.environ)
    env["SOURCE_DATE_EPOCH"] = str(SOURCE_DATE_EPOCH)
    proc = subprocess.run(
        ["dpkg-deb", "--root-owner-group", "--uniform-compression", "-Zxz",
         "--build", str(stage), str(output_deb)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"dpkg-deb failed: {proc.stderr.strip() or proc.stdout.strip()}")
