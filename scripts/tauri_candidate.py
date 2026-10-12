"""Build the Tauri CLI candidate .debs from reviewed pins, outside the production catalog.

This is an isolated, explicitly invoked path. Tauri is deliberately absent from
`scripts/recipes.py`, so it is never discovered by the updater, never indexed by
the publisher, and can never reach the production pool by accident. Everything
here reuses the existing download, staging and `dpkg-deb` code; what this module
adds is the candidate's own descriptor, the closed inventory of the one official
asset, and the two provenance records the packages install.

The two architectures are built by two different inputs, and the CLI says so
rather than hiding it. `amd64` repackages the pinned official release asset, which
is downloaded and hash-verified here. `arm64` has no official asset at all: it
takes the binary a native ARM CI job compiled from the pinned upstream commit
together with that job's provenance record, refuses the pair unless the record
matches the reviewed pins and the binary's digest matches the record, and installs
the record. Building `arm64` without that provenance is refused outright — an
invented asset URL or digest would be a false provenance claim.

Nothing here executes the upstream binary, contacts a publisher, or reads a
production credential.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path

from .common import sha256_file
from .deb import build_deb, finalize_package_stage, stage_package
from .download import canonical_member, fetch_verified, safe_extract_member
from .manifest import deb_filename
from .tauri_candidate_metadata import (
    ARCHES,
    BINARY,
    Candidate,
    check_source_document,
    license_bodies,
    load_candidate,
    repo_root,
)
from .tauri_candidate_provenance import (
    PROVENANCE_NAME_BY_ARCH,
    check_official_asset,
    check_source_build,
    emit_official_asset,
    read_record,
    write_record,
)
from .tauri_candidate_smoke import check_runtime

DEFAULT_ARCHES = ("amd64",)

# Bounds on the one official asset, reviewed against the pinned release.
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_MEMBERS = 64
MAX_TOTAL_BYTES = 256 * 1024 * 1024

# Installed notice files: the reviewed upstream licence texts, byte for byte.
NOTICE_FILES = {
    "LICENSE-MIT.txt": "LICENSE-MIT.txt",
    "LICENSE-APACHE-2.0.txt": "LICENSE-APACHE-2.0.txt",
}


@dataclass(frozen=True)
class Paths:
    """The repository locations this build reads."""

    repo: Path

    @property
    def licenses(self) -> Path:
        return self.repo / "packaging" / "licenses" / "tauri-cli"

    @property
    def readme_source(self) -> Path:
        return self.repo / "packaging" / "tauri-cli" / "README.source"


def default_output() -> Path:
    return Path("dist/tauri-candidate")


def default_cache() -> Path:
    return Path.home() / ".cache" / "opencode" / "apt-tauri-candidate"


def inventory_asset(archive: Path, reviewed: tuple[str, ...]) -> tuple[str, ...]:
    """Validate every member of the release tarball against the reviewed set.

    Nothing is extracted before the whole archive has been inspected: a member
    path must canonicalize (no traversal, absolute path, backslash, NUL, or
    control byte), and every member must be a plain, non-setuid regular file
    within the declared count and size bounds. The measured release carries four
    regular files and no directory entries, so a directory is a change worth
    refusing rather than accepting.
    """
    size = archive.stat().st_size
    if size > MAX_ARCHIVE_BYTES:
        raise ValueError(f"asset is {size} bytes, over the {MAX_ARCHIVE_BYTES} limit")
    seen: list[str] = []
    total = 0
    with tarfile.open(archive, "r:gz") as tar:
        items = tar.getmembers()
        if len(items) > MAX_MEMBERS:
            raise ValueError(f"asset carries {len(items)} members, over the {MAX_MEMBERS} limit")
        for item in items:
            if item.issym() or item.islnk() or item.isdev() or item.isfifo():
                raise ValueError(f"refusing link or special member: {item.name!r}")
            if item.mode & 0o6000:
                raise ValueError(f"refusing setuid or setgid member: {item.name!r}")
            if item.isdir():
                raise ValueError(f"the reviewed asset carries no directory entries: {item.name!r}")
            if not item.isfile():
                raise ValueError(f"refusing non-regular member: {item.name!r}")
            relative = canonical_member(item.name)
            if relative in seen:
                raise ValueError(f"asset declares member {relative!r} more than once")
            if item.size > MAX_TOTAL_BYTES:
                raise ValueError(f"member {relative!r} exceeds the {MAX_TOTAL_BYTES} byte limit")
            total += item.size
            if total > MAX_TOTAL_BYTES:
                raise ValueError(f"asset exceeds the {MAX_TOTAL_BYTES} byte total limit")
            seen.append(relative)
    if tuple(sorted(seen)) != reviewed:
        raise ValueError(
            f"asset members {sorted(seen)} do not match the reviewed set {list(reviewed)}"
        )
    return tuple(seen)


def fetch_asset(candidate: Candidate, cache_dir: Path) -> Path:
    """Fetch and hash-verify the pinned archive, and check its pinned size.

    The asset digest is checked before the archive is ever opened, so a
    substituted download is rejected without being parsed.
    """
    asset = candidate.official
    cached = cache_dir / asset.url.rsplit("/", 1)[-1]
    fetch_verified(asset.url, asset.sha256, cached)
    actual = cached.stat().st_size
    if actual != asset.size:
        raise ValueError(f"amd64 asset is {actual} bytes, descriptor pins {asset.size}")
    return cached


def _check_runtime(binary: Path, arch: str, candidate: Candidate) -> None:
    """Refuse a payload whose ABI the reviewed runtime mapping does not cover.

    This runs before anything is staged, so an arm64 binary that needs a newer
    glibc than the candidate declares never reaches a `.deb`. A mismatch is a
    refusal, not a widened dependency: the floor is a reviewed claim.
    """
    check_runtime(binary, arch, candidate)


def _check_record(candidate: Candidate, arch: str, record: dict) -> None:
    """Validate the record before its bytes can enter the package."""
    if arch == candidate.official.arch:
        check_official_asset(record, candidate)
    else:
        check_source_build(record, candidate)


def _finish_stage(
    candidate: Candidate,
    arch: str,
    work: Path,
    paths: Paths,
    binary: Path,
    record: dict,
) -> Path:
    """Stage one architecture: validated binary, notices, and its provenance record."""
    pkg = candidate.package
    _check_record(candidate, arch, record)
    stage = stage_package(
        pkg=pkg, arch=arch, manifest=candidate.manifest, stage_root=work / "stage",
        binary_file=binary, license_bodies=license_bodies(paths.licenses),
    )
    doc_dir = stage / "usr" / "share" / "doc" / pkg.name
    for installed_name, source_name in NOTICE_FILES.items():
        shutil.copyfile(paths.licenses / source_name, doc_dir / installed_name)
    # Recheck the document that is about to be installed. The loader already
    # validated it, but this is the last point before the bytes enter the
    # package, and it is reached identically from every CLI entrypoint.
    check_source_document(paths.readme_source, pkg.version)
    shutil.copyfile(paths.readme_source, doc_dir / "README.source")
    write_record(doc_dir / PROVENANCE_NAME_BY_ARCH[arch], record)
    finalize_package_stage(
        stage=stage, pkg=pkg, arch=arch, manifest=candidate.manifest,
        license_bodies=license_bodies(paths.licenses),
    )
    return stage


def stage_official_asset(
    candidate: Candidate, arch: str, work: Path, cache: Path, paths: Paths
) -> Path:
    """Stage amd64 from the pinned official release asset."""
    asset = candidate.official
    if arch != asset.arch:
        raise ValueError(f"no official asset is reviewed for {arch}")
    archive = fetch_asset(candidate, cache)
    inventory_asset(archive, asset.members)
    payload = work / f"payload-{arch}"
    payload.mkdir(parents=True, exist_ok=True)
    binary = safe_extract_member(archive, asset.binary, payload / BINARY)
    _check_runtime(binary, arch, candidate)
    record = emit_official_asset(
        candidate, binary, {name: paths.licenses / name for name in NOTICE_FILES},
    )
    return _finish_stage(candidate, arch, work, paths, binary, record)


def stage_source_build(
    candidate: Candidate, arch: str, work: Path, paths: Paths,
    binary: Path, provenance: Path,
) -> Path:
    """Stage arm64 from a native source build and its validated provenance record."""
    build = candidate.source_build
    if arch != build.arch:
        raise ValueError(f"no source build is reviewed for {arch}")
    if not binary.is_file():
        raise ValueError(f"source-built binary not found: {binary}")
    record = read_record(provenance)
    check_source_build(record, candidate)
    artifact = record["artifact"]
    digest = sha256_file(binary)
    if digest != artifact["sha256"]:
        raise ValueError(
            f"{binary} has sha256 {digest}, but its provenance record declares "
            f"{artifact['sha256']}"
        )
    size = binary.stat().st_size
    if size != artifact["bytes"]:
        raise ValueError(
            f"{binary} is {size} bytes, but its provenance record declares {artifact['bytes']}"
        )
    _check_runtime(binary, arch, candidate)
    return _finish_stage(candidate, arch, work, paths, binary, record)


def build(
    output: Path,
    arches: tuple[str, ...],
    candidate: Candidate,
    *,
    source_build: tuple[Path, Path] | None = None,
    repo: Path | None = None,
    cache: Path | None = None,
) -> list[Path]:
    """Build the candidate debs for the requested arches into `output`.

    The download cache persists across builds so a repeated build re-verifies the
    same pinned bytes instead of downloading them again; only `work` is recreated.
    """
    paths = Paths(repo or repo_root())
    cache_dir = cache or default_cache()
    work = cache_dir / "work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    output.mkdir(parents=True, exist_ok=True)
    built: list[Path] = []
    for arch in arches:
        if arch == candidate.official.arch:
            stage = stage_official_asset(candidate, arch, work, cache_dir, paths)
        else:
            if source_build is None:
                raise ValueError(
                    "the arm64 candidate is compiled from source and needs --binary "
                    "and --provenance from a pinned native build"
                )
            stage = stage_source_build(candidate, arch, work, paths, *source_build)
        deb = output / deb_filename(candidate.package, arch, candidate.manifest)
        build_deb(stage, deb)
        built.append(deb)
    return built


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.tauri_candidate",
        description="Build the reviewed Tauri CLI candidate packages locally. "
                    "This path is not part of the published APT repository.",
    )
    parser.add_argument("--output", type=Path, default=default_output())
    parser.add_argument("--arch", choices=ARCHES, action="append",
                        help="repeat to build several; defaults to amd64, the official asset")
    parser.add_argument("--candidate", type=Path, default=None)
    parser.add_argument("--binary", type=Path, default=None,
                        help="the compiled cargo-tauri, for --arch arm64")
    parser.add_argument("--provenance", type=Path, default=None,
                        help="its build-provenance.json, for --arch arm64")
    args = parser.parse_args(argv)
    arches = tuple(args.arch) if args.arch else DEFAULT_ARCHES
    source_build = None
    if args.binary is not None or args.provenance is not None:
        if arches != ("arm64",) or args.binary is None or args.provenance is None:
            parser.error("--binary and --provenance are the arm64 source build's own "
                         "inputs and require --arch arm64")
        source_build = (args.binary, args.provenance)
    if "arm64" in arches and source_build is None:
        parser.error("--arch arm64 needs --binary and --provenance from a pinned native build")
    candidate = load_candidate(args.candidate)
    built = build(args.output, arches, candidate, source_build=source_build)
    for deb in built:
        print(f"{deb.name}  {sha256_file(deb)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
