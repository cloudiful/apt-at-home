"""Generate static APT metadata with apt-ftparchive and deterministic gzip."""

from __future__ import annotations

import gzip
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .common import require_tool, run, sha256_file, sha512_file
from .manifest import Manifest, deb_filename

SUITE = "stable"
COMPONENT = "main"
ARCHES = ("amd64", "arm64")


def _write_gzip_deterministic(source: Path, dest: Path) -> None:
    data = source.read_bytes()
    with dest.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", compresslevel=9, mtime=0, fileobj=raw) as gz:
            gz.write(data)


def generate_packages(pool_dir: Path, dists_arch_dir: Path, debs: list[Path]) -> tuple[Path, Path]:
    """Run apt-ftparchive for one architecture; returns (Packages, Packages.gz)."""
    require_tool("apt-ftparchive")
    dists_arch_dir.mkdir(parents=True, exist_ok=True)
    packages_path = dists_arch_dir / "Packages"
    # apt-ftparchive resolves filenames relative to its working directory, so
    # invoke it from the pool directory with bare file names for stable output.
    rel_names = [Path(d).name for d in sorted(debs, key=lambda p: p.name)]
    proc = run(
        ["apt-ftparchive", "packages", *rel_names],
        cwd=pool_dir,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"apt-ftparchive packages failed: {proc.stderr.strip()}")
    # Normalize: apt-ftparchive emits Filename relative to the archive root.
    lines: list[str] = []
    for line in proc.stdout.splitlines():
        if line.startswith("Filename: "):
            lines.append(f"Filename: pool/{line[len('Filename: '):].strip()}")
        else:
            lines.append(line)
    packages_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    gz_path = dists_arch_dir / "Packages.gz"
    _write_gzip_deterministic(packages_path, gz_path)
    return packages_path, gz_path


def _hash_entry(path: Path, prefix: str) -> tuple[str, str, int, str, str]:
    return (
        sha256_file(path),
        sha512_file(path),
        path.stat().st_size,
        f"{prefix}/{path.name}",
        path.name,
    )


def generate_release(
    *,
    output_root: Path,
    release_id: str,
    files: list[tuple[Path, str]],
) -> Path:
    """Write dists/stable/Release referencing local Packages files only."""
    dists_dir = output_root / "releases" / release_id / "dists" / SUITE
    dists_dir.mkdir(parents=True, exist_ok=True)
    date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S UTC")
    sha256_lines: list[str] = []
    sha512_lines: list[str] = []
    md5_lines: list[str] = []
    import hashlib

    for path, rel in sorted(files, key=lambda item: item[1]):
        size = path.stat().st_size
        sha256_lines.append(f" {sha256_file(path)} {size:16d} {rel}")
        sha512_lines.append(f" {sha512_file(path)} {size:16d} {rel}")
        digest = hashlib.md5(path.read_bytes()).hexdigest()
        md5_lines.append(f" {digest} {size:16d} {rel}")
    body = (
        f"Origin: apt-at-home\n"
        f"Label: apt-at-home\n"
        f"Suite: {SUITE}\n"
        f"Codename: {SUITE}\n"
        f"Date: {date}\n"
        f"Architectures: amd64 arm64\n"
        f"Components: {COMPONENT}\n"
        f"Description: Private APT repository\n"
        f"Acquire-By-Hash: yes\n"
        f"MD5Sum:\n" + "\n".join(md5_lines) + "\n"
        f"SHA256:\n" + "\n".join(sha256_lines) + "\n"
        f"SHA512:\n" + "\n".join(sha512_lines) + "\n"
    )
    release_path = dists_dir / "Release"
    release_path.write_text(body, encoding="utf-8")
    # Minimal per-architecture Release files served by the gateway.
    for arch in ARCHES:
        arch_dir = dists_dir / COMPONENT / f"binary-{arch}"
        arch_dir.mkdir(parents=True, exist_ok=True)
        (arch_dir / "Release").write_text(
            f"Archive: {SUITE}\nVersion: 1.0\nComponent: {COMPONENT}\n"
            f"Origin: apt-at-home\nLabel: apt-at-home\nArchitecture: {arch}\n",
            encoding="utf-8",
        )
    return release_path


def write_indices(
    *,
    output_root: Path,
    release_id: str,
    packages_files: dict[str, list[Path]],
) -> list[Path]:
    """Copy Packages/Packages.gz into shared content-addressed indices."""
    written: list[Path] = []
    for arch, paths in packages_files.items():
        for path in paths:
            digest = sha256_file(path)
            dest = output_root / "indices" / arch / "SHA256" / digest
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists() and sha256_file(dest) != digest:
                raise ValueError(f"index collision at {dest}: digest mismatch")
            shutil.copyfile(path, dest)
            written.append(dest)
    return written

