"""Build the Bun candidate .debs from reviewed pins, outside the production catalog.

This is an isolated, explicitly invoked path. Bun is deliberately absent from
`scripts/recipes.py`, so it is never discovered by the updater, never indexed by
the publisher, and can never reach the production pool by accident. Everything
here reuses the existing download, staging and `dpkg-deb` code; the only thing
this module adds is the candidate's own descriptor, its single-member ZIP
handling, and the owned `bunx` alias.

Nothing here executes the upstream binary, contacts a publisher, or reads a
production credential.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from . import archive_zip
from .bun_candidate_metadata import (
    Candidate,
    check_source_document,
    license_bodies,
    load_candidate,
)
from .common import sha256_file
from .deb import build_deb, finalize_package_stage, stage_package
from .download import fetch_verified
from .manifest import deb_filename

ARCHES = ("amd64", "arm64")

# The alias is created by this package after its payload is validated. The
# upstream archive's own links are never trusted or copied.
ALIAS_NAME = "bunx"
ALIAS_TARGET = "bun"

# Installed notice files: the upstream LICENSE verbatim plus both GNU LGPL texts.
NOTICE_FILES = {
    "LICENSE.upstream.md": "LICENSE.txt",
    "LGPL-2.0.txt": "LGPL-2.0.txt",
    "LGPL-2.1.txt": "LGPL-2.1.txt",
}


@dataclass(frozen=True)
class Paths:
    """The repository locations this build reads."""

    repo: Path

    @property
    def licenses(self) -> Path:
        return self.repo / "packaging" / "licenses" / "bun"

    @property
    def readme_source(self) -> Path:
        return self.repo / "packaging" / "bun" / "README.source"


def default_output() -> Path:
    return Path("dist/bun-candidate")


def default_cache() -> Path:
    return Path.home() / ".cache" / "opencode" / "apt-bun-candidate"


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def fetch_asset(candidate: Candidate, arch: str, cache_dir: Path) -> Path:
    """Fetch and hash-verify the pinned archive, and check its pinned size.

    The asset digest is checked before the archive is ever opened as a ZIP, so a
    substituted download is rejected without being parsed.
    """
    target = candidate.package.targets[arch]
    cached = cache_dir / target.url.rsplit("/", 1)[-1]
    fetch_verified(target.url, target.sha256, cached)
    actual = cached.stat().st_size
    if actual != candidate.sizes[arch]:
        raise ValueError(f"bun {arch} archive is {actual} bytes, descriptor pins {candidate.sizes[arch]}")
    return cached


def stage_candidate(
    candidate: Candidate, arch: str, work: Path, cache: Path, paths: Paths
) -> Path:
    """Stage one architecture: validated binary, notices, and the owned alias.

    `cache` is the persistent download cache and survives the build; `work` is
    the per-build scratch that is discarded, so a rebuilt candidate never reuses
    a stale extraction.
    """
    pkg = candidate.package
    target = pkg.targets[arch]
    archive = fetch_asset(candidate, arch, cache)
    member = archive_zip.select_single_member(
        archive_zip.inventory(archive, target.archive_member), target.archive_member
    )
    payload = work / f"payload-{arch}"
    payload.mkdir(parents=True, exist_ok=True)
    binary = archive_zip.extract_member(archive, member, payload / target.binary)

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
    _install_alias(stage)
    finalize_package_stage(
        stage=stage, pkg=pkg, arch=arch, manifest=candidate.manifest,
        license_bodies=license_bodies(paths.licenses),
    )
    return stage


def _install_alias(stage: Path) -> None:
    """Create the package-owned `bunx` alias inside the fresh stage."""
    bin_dir = stage / "usr" / "bin"
    if not (bin_dir / ALIAS_TARGET).is_file():
        raise ValueError(f"alias target {ALIAS_TARGET} is missing from the stage")
    alias = bin_dir / ALIAS_NAME
    if alias.exists() or alias.is_symlink():
        raise ValueError(f"alias {ALIAS_NAME} already exists in the stage")
    os.symlink(ALIAS_TARGET, alias)


def build(
    output: Path,
    arches: tuple[str, ...],
    candidate: Candidate,
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
        stage = stage_candidate(candidate, arch, work, cache_dir, paths)
        deb = output / deb_filename(candidate.package, arch, candidate.manifest)
        build_deb(stage, deb)
        built.append(deb)
    return built


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.bun_candidate",
        description="Build the reviewed Bun candidate packages locally. "
                    "This path is not part of the published APT repository.",
    )
    parser.add_argument("--output", type=Path, default=default_output())
    parser.add_argument("--arch", choices=ARCHES, action="append",
                        help="repeat to build several; defaults to both")
    parser.add_argument("--candidate", type=Path, default=None)
    args = parser.parse_args(argv)
    candidate = load_candidate(args.candidate)
    built = build(args.output, tuple(args.arch) if args.arch else ARCHES, candidate)
    for deb in built:
        print(f"{deb.name}  {sha256_file(deb)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())