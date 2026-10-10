"""Orchestrate download verification and .deb assembly for all targets."""

from __future__ import annotations

import tempfile
from pathlib import Path

from .common import sha256_file
from .deb import build_deb, stage_package
from .download import fetch_verified, safe_extract_member
from .manifest import Manifest, deb_filename


def build_all(manifest: Manifest, *, pool_dir: Path, cache_dir: Path) -> list[Path]:
    """Build the four .deb files; returns the .deb paths in sorted order."""
    pool_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="apt-at-home-build-") as tmp:
        tmpdir = Path(tmp)
        for pkg in sorted(manifest.packages, key=lambda p: p.name):
            for arch in ("amd64", "arm64"):
                target = pkg.targets[arch]
                archive = cache_dir / f"{pkg.name}-{pkg.version}-{arch}.tar.gz"
                fetch_verified(target.url, target.sha256, archive)
                binary_src = tmpdir / f"{pkg.name}-{arch}-bin" / target.binary
                binary_src.parent.mkdir(parents=True, exist_ok=True)
                safe_extract_member(archive, target.archive_member, binary_src)
                # The extracted binary is never executed during the host build.
                stage = stage_package(
                    pkg=pkg,
                    arch=arch,
                    manifest=manifest,
                    binary_file=binary_src,
                    stage_root=tmpdir / "stages",
                )
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
