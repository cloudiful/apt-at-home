"""Orchestrate download verification and .deb assembly for all targets."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path

from .archive_tree import inventory_tree
from .catalog import required_paths_for, tag_for
from .common import sha256_file
from .deb import build_deb, finalize_package_stage, stage_package
from .download import fetch_verified
from .manifest import Manifest, Package, deb_filename
from .package_binaries import extract_binaries
from .kandev_bundle import inventory_bundle, stage_kandev_package
from .source_artifacts import ReusedSource, SourceArtifacts, select_source_artifacts
from .source_build import SOURCE_PROVENANCE_NAME, source_target_key


def _stage_target(
    pkg: Package, arch: str, manifest: Manifest, archive: Path, tmpdir: Path
) -> Path:
    """Stage one target from its archive: a flat member or a validated upstream tree."""
    from .package_licenses import cargo_tauri_source_document

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
        archive, target, tmpdir / f"{pkg.name}-{arch}-bin"
    )
    additional = {
        name: source for name, source in binary_sources.items() if name != target.binary
    }
    stage = stage_package(
        pkg=pkg,
        arch=arch,
        manifest=manifest,
        binary_file=binary_sources[target.binary],
        additional_binary_files=additional,
        stage_root=tmpdir / "stages",
    )
    if pkg.name == "cargo-tauri":
        # Production README.source is rendered from the validated package
        # itself (version + official asset + ARM source pins), not a fixed
        # copy, and is installed for both architectures. Notices use the
        # reviewed production names for both arches as well; amd64 carries
        # the same vendored texts the ARM compilation was verified against.
        from .source_artifacts import NOTICE_FILES as _NOTICE_FILES

        doc_dir = stage / "usr" / "share" / "doc" / pkg.name
        (doc_dir / "README.source").write_text(
            cargo_tauri_source_document(pkg), encoding="utf-8"
        )
        vendor = Path(__file__).resolve().parent.parent / "packaging" / "licenses" / pkg.name
        for name in _NOTICE_FILES:
            shutil.copyfile(vendor / name, doc_dir / name)
        # Re-finalize so Installed-Size, modes, and mtimes cover the new docs.
        from .deb import finalize_package_stage as _finalize
        from .package_licenses import read_vendor_licenses as _read_licenses

        _finalize(
            stage=stage, pkg=pkg, arch=arch, manifest=manifest,
            license_bodies=_read_licenses(pkg),
        )
    return stage


def _stage_source_target(
    pkg: Package, arch: str, manifest: Manifest, artifacts: SourceArtifacts, tmpdir: Path
) -> Path:
    """Stage a source-built target from an already validated compilation.

    The binary was compiled by a separate native job and re-validated by
    `source_artifacts`; this only copies those bytes, the reviewed notices, and
    the provenance record that describes them. Nothing here executes the payload
    and nothing here compiles: a source builder is not reachable from this path.
    """
    from .package_licenses import cargo_tauri_source_document

    stage = stage_package(
        pkg=pkg,
        arch=arch,
        manifest=manifest,
        stage_root=tmpdir / "stages",
        binary_file=artifacts.binary,
        license_bodies=artifacts.license_bodies(),
    )
    doc_dir = stage / "usr" / "share" / "doc" / pkg.name
    for name, source in artifacts.notices.items():
        shutil.copyfile(source, doc_dir / name)
    if artifacts.record is not None:
        shutil.copyfile(artifacts.record, doc_dir / SOURCE_PROVENANCE_NAME)
    if pkg.name == "cargo-tauri":
        (doc_dir / "README.source").write_text(
            cargo_tauri_source_document(pkg), encoding="utf-8"
        )
    finalize_package_stage(
        stage=stage, pkg=pkg, arch=arch, manifest=manifest,
        license_bodies=artifacts.license_bodies(),
    )
    return stage


def _install_reused(entry: ReusedSource, pool_dir: Path, manifest: Manifest) -> Path:
    """Put an already published deb in the pool, byte for byte and without building.

    Nothing is staged and nothing is compiled: the bytes are the ones the signed
    index authenticated, so the only work left is copying them and refusing to
    replace an existing pool entry with different bytes.
    """
    package = next(pkg for pkg in manifest.packages if pkg.name == entry.package)
    expected = deb_filename(package, entry.arch, manifest)
    if entry.filename != expected:
        raise ValueError(
            f"{entry.package} {entry.arch} published {entry.filename}, but this build "
            f"expects {expected}"
        )
    size = entry.deb.stat().st_size
    digest = sha256_file(entry.deb)
    if size != entry.size or digest != entry.sha256:
        raise ValueError(
            f"{entry.deb.name} no longer matches the signed entry it was verified against"
        )
    output_deb = pool_dir / expected
    if output_deb.exists():
        if sha256_file(output_deb) != entry.sha256:
            raise ValueError(
                f"refusing to overwrite {output_deb.name} with different bytes"
            )
        return output_deb
    shutil.copyfile(entry.deb, output_deb)
    return output_deb


def build_all(
    manifest: Manifest,
    *,
    pool_dir: Path,
    cache_dir: Path,
    packages: Iterable[str] | None = None,
    source_artifacts: Mapping[tuple[str, str], SourceArtifacts] | None = None,
    reused_sources: Mapping[tuple[str, str], ReusedSource] | None = None,
) -> list[Path]:
    """Build the .deb files; returns the paths in sorted order.

    `packages` narrows the work to the named packages, which lets an incremental
    update rebuild only what changed while the restored pool keeps the rest. The
    default builds every pinned package.

    A source-built target takes its binary from `source_artifacts` instead of a
    download, because it has no asset to fetch; a target whose artifact is
    missing is refused rather than built from something else. A target listed in
    `reused_sources` is not built at all: its published deb is already verified
    against the signed index, so the bytes and the provenance clients installed are
    the ones this release repeats. One target may never be both reused and rebuilt.
    """
    pool_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    artifacts: Mapping[tuple[str, str], SourceArtifacts] = source_artifacts or {}
    reused: Mapping[tuple[str, str], ReusedSource] = reused_sources or {}
    conflicting = sorted(set(reused) & set(artifacts))
    if conflicting:
        package, arch = conflicting[0]
        raise ValueError(
            f"{package} {arch} was given both a reused published deb and a new artifact; "
            "one target cannot be both"
        )
    planned = {
        source_target_key(pkg.name, arch)
        for pkg in manifest.packages
        for arch, target in pkg.targets.items()
        if target.source_build is not None
    }
    stray = sorted(set(reused) - planned)
    if stray:
        package, arch = stray[0]
        raise ValueError(f"{package} {arch} is not a source target of this manifest")
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
            for arch in ("amd64", "arm64"):
                target = pkg.targets[arch]
                key = source_target_key(pkg.name, arch)
                if target.is_source_target and key in reused:
                    outputs.append(_install_reused(reused[key], pool_dir, manifest))
                    continue
                if target.is_source_target:
                    stage = _stage_source_target(
                        pkg, arch, manifest,
                        select_source_artifacts(artifacts, pkg.name, arch, target),
                        tmpdir,
                    )
                else:
                    archive = cache_dir / f"{pkg.name}-{pkg.version}-{arch}.tar.gz"
                    fetch_verified(target.url, target.sha256, archive)
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
