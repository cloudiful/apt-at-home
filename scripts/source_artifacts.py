"""Consume one same-run directory of compiled source artifacts, without executing it.

The producer job compiles an architecture and uploads a single-root directory: the
binary, its provenance record, and the reviewed licence notices it was built
against. This module is the only consumer of that directory, and it treats every
byte in it as untrusted input rather than as the producer's word.

Nothing here is executed. The binary is read as an ELF and hashed, never run; a
native probe belongs to the producer's own build, whose result is what this
directory is evidence of. What *is* checked, before any of it can reach a package:

* the directory is a real directory of plain regular files — no symlink, no
  subdirectory, no unrecognised file, so nothing is followed out of the root;
* every file is bounded before it is read;
* the provenance record names the reviewed package and architecture, and its
  identity is compared with the identity the caller expects, so a recompiled
  binary from a different commit, lockfile, compiler, or command is refused;
* the record's measured sha256, size, and ELF machine must match the binary that
  is actually there, so a swapped payload cannot keep a record that vouches for
  it;
* the linked libraries and their symbol floors must fit the reviewed runtime
  mapping — a floor is never widened to make a payload fit;
* the licence notices are the reviewed set, and byte-compared when the reviewed
  texts are supplied.

`ReusedSource` is the other half of the same idea, for a build that must not
compile at all. Where `SourceArtifacts` is what a producer measured minutes ago,
this is what the *published* release already states for a version clients have
installed: the exact deb bytes the signed index vouches for, and the realized
provenance that shipped with them. Reusing it is not a shortcut — the release it
came from is verified by the same restore chain as any other read, the identity
must equal the committed one in full, and the bytes must match the signed size
and digest before anything is written to disk.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from .bun_candidate_smoke import check_elf_arch, read_elf
from .catalog import tag_prefix_for
from .common import sha256_bytes, sha256_file
from .download import MAX_BINARY_BYTES
from .manifest import ArchTarget, deb_filename, debian_version, parse_manifest
from .package_index import parse_stanzas
from .release_verify import BaselineUnavailable, TrustedRelease, restore_metadata
from .restore_packages import pool_entries
from .source_build import (
    PLAN_NAME,
    SOURCE_PROVENANCE_NAME,
    ArtifactProvenance,
    RealizedSource,
    SourceIdentity,
    check_source_target,
    check_tag_version,
    identity_of,
    artifact_of,
    is_realized,
    parse_artifact,
    source_target_key,
    source_targets,
    version_from_tag,
)
from .storage import MAX_DEB_BYTES, ObjectNotFound, Source
from .tauri_candidate_metadata import ELF_MACHINE, REVIEWED_RUNTIME, RuntimeLibrary
from .tauri_candidate_provenance import (
    SourceRecordExpectation,
    check_source_build,
)
from .tauri_candidate_smoke import RuntimePolicy, check_runtime

# Production notices: the vendored filenames under
# `packaging/licenses/cargo-tauri/`, exactly in MIT then Apache order, matching
# `package_licenses.read_vendor_licenses` (primary `LICENSE.txt`, then the
# recipe's `extra_license_paths`). Candidate `tauri-cli/*.txt` filenames stay
# frozen and independent; production never reads them.
NOTICE_FILES: tuple[str, ...] = ("LICENSE.txt", "LICENSE-APACHE-2.0")

MAX_RECORD_BYTES = 1024 * 1024
MAX_NOTICE_BYTES = 1024 * 1024


@dataclass(frozen=True)
class ReviewedRuntime:
    """The closed SONAME-to-Debian-package mapping a payload is measured against."""

    libraries: tuple[RuntimeLibrary, ...]

    def sonames(self) -> tuple[str, ...]:
        return tuple(sorted(library.soname for library in self.libraries))

    def declared_floors(self) -> dict[str, str]:
        from .tauri_candidate_metadata import version_key

        floors: dict[str, str] = {}
        for library in self.libraries:
            current = floors.get(library.package)
            if current is None or version_key(library.version) > version_key(current):
                floors[library.package] = library.version
        return floors


def reviewed_runtime(package: str) -> ReviewedRuntime:
    """Return the code-pinned runtime mapping for a reviewed source-built package."""
    if not REVIEWED_RUNTIME:
        raise ValueError("no reviewed runtime mapping is available")
    libraries = tuple(
        sorted(
            (
                RuntimeLibrary(soname=soname, package=package_name, version=version, symbols=symbols)
                for soname, (package_name, version, symbols) in REVIEWED_RUNTIME.items()
            ),
            key=lambda library: library.soname,
        )
    )
    return ReviewedRuntime(libraries=libraries)


def production_reviewed_notices(package: str = "cargo-tauri") -> dict[str, bytes]:
    """Return the trusted vendored production notices for byte comparison.

    Production defaults resolve reviewed catalog paths, so untrusted artifact
    bytes are always compared against these. Direct unit fixtures may inject
    explicit `reviewed_notices` instead; production consumer paths never pass
    `None` to skip the check.
    """
    from .package_licenses import read_vendor_licenses
    from .manifest import load_manifest

    manifest = load_manifest()
    pkg = next(p for p in manifest.packages if p.name == package)
    bodies = read_vendor_licenses(pkg)
    if len(bodies) != len(NOTICE_FILES):
        raise ValueError(
            f"{package} vendored notices do not match production {list(NOTICE_FILES)}"
        )
    return {name: body.encode("utf-8") for name, body in zip(NOTICE_FILES, bodies, strict=True)}


@dataclass(frozen=True)
class SourceArtifacts:
    """One validated compilation result, ready to be packaged."""

    package: str
    arch: str
    identity: SourceIdentity
    artifact: ArtifactProvenance
    binary: Path
    notices: Mapping[str, Path] = MappingProxyType({})
    record: Path | None = None

    def license_bodies(self) -> tuple[str, ...]:
        """Return the reviewed licence texts in their reviewed order."""
        return tuple(self.notices[name].read_text(encoding="utf-8") for name in NOTICE_FILES)


@dataclass(frozen=True)
class ReusedSource:
    """One source target's published deb, verified, and ready to be reused as is.

    `sha256` and `size` authenticate the .deb the signed index vouches for, and
    `artifact` is the measurement the published manifest already carries for the
    binary inside it. Nothing is re-derived here: reusing this entry publishes the
    bytes and the provenance that clients already have, which is the only version
    of them a same-version rebuild is allowed to ship.
    """

    package: str
    arch: str
    version: str
    filename: str
    identity: SourceIdentity
    artifact: ArtifactProvenance
    deb: Path
    sha256: str
    size: int


def reuse_published_source(
    *,
    source: Source,
    fingerprint: str,
    document: Mapping[str, object],
    package: str,
    arch: str,
    destination: Path,
) -> ReusedSource:
    """Return the published, verified deb for a source target already at this version.

    The published release is established with `restore_metadata` first: the pointer,
    the served key against the pin, both signatures, the bound manifest, and both
    signed indices. Only then is the one desired target selected — its identity has
    to equal the committed one in full, and it has to be realized in the published
    manifest, because a planned published target states no bytes to reuse. The deb
    is fetched under the existing ceiling and must match the signed size and digest
    before it is written, and the payload is never unpacked or executed: the
    published chain already authenticated it.
    """
    check_source_target(package, arch)
    desired = parse_manifest(document)
    wanted = next((entry for entry in desired.packages if entry.name == package), None)
    target = wanted.targets.get(arch) if wanted is not None else None
    if wanted is None or target is None or target.source_build is None:
        raise ValueError(
            f"{package} {arch} is not built from reviewed source in the committed manifest"
        )
    identity = identity_of(target.source_build)
    version = debian_version(wanted, desired)

    release = restore_metadata(source, fingerprint=fingerprint)
    published = next((entry for entry in release.manifest.packages if entry.name == package), None)
    published_target = published.targets.get(arch) if published is not None else None
    if published is None or published_target is None or published_target.source_build is None:
        raise BaselineUnavailable(
            f"the published release carries no {package} {arch} source target; "
            "a new compilation is required"
        )
    carried = published_target.source_build
    if not is_realized(carried):
        raise BaselineUnavailable(
            f"the published {package} {arch} carries no measured artifact; "
            "a new compilation is required"
        )
    if identity_of(carried) != identity:
        raise ValueError(
            f"{package} {arch} is published from source identity "
            f"{identity_of(carried).commit[:12]} but the committed manifest pins "
            f"{identity.commit[:12]}; a published release is immutable"
        )
    published_version = debian_version(published, release.manifest)
    if published_version != version:
        raise ValueError(
            f"{package} {arch} is published at {published_version}, not the requested "
            f"{version}; a published release is immutable"
        )

    filename = deb_filename(published, arch, release.manifest)
    entries = {entry.name: entry for entry in pool_entries(release)}
    entry = entries.get(filename)
    if entry is None:
        raise BaselineUnavailable(
            f"the published indices omit {filename}; a new compilation is required"
        )
    if entry.size > MAX_DEB_BYTES:
        raise ValueError(
            f"{filename} declares {entry.size} bytes, over the {MAX_DEB_BYTES} limit"
        )
    try:
        payload = source.get(f"pool/{filename}", max_bytes=MAX_DEB_BYTES)
    except ObjectNotFound as exc:
        raise BaselineUnavailable(f"published pool is missing {filename}") from exc
    if len(payload) != entry.size or sha256_bytes(payload) != entry.sha256:
        raise ValueError(f"{filename} does not match the signed Packages entry")

    realized: ArtifactProvenance = artifact_of(carried)  # type: ignore[assignment]
    destination.mkdir(parents=True, exist_ok=True)
    deb = destination / filename
    deb.write_bytes(payload)
    return ReusedSource(
        package=package, arch=arch, version=version, filename=filename,
        identity=identity, artifact=realized, deb=deb,
        sha256=entry.sha256, size=entry.size,
    )


def expected_source_identities(
    document: Mapping[str, object]
) -> dict[tuple[str, str], SourceIdentity]:
    """Return the identity every source-built target of a manifest pins."""
    return {key: identity_of(value) for key, value in source_targets(document)}


def index_source_artifacts(
    artifacts: tuple[SourceArtifacts, ...] | list[SourceArtifacts],
) -> dict[tuple[str, str], SourceArtifacts]:
    """Index artifacts by their package and architecture, refusing duplicates."""
    indexed: dict[tuple[str, str], SourceArtifacts] = {}
    for item in artifacts:
        key = source_target_key(item.package, item.arch)
        if key in indexed:
            raise ValueError(f"two artifact directories claim {item.package} {item.arch}")
        indexed[key] = item
    return indexed


def _bounded_file(path: Path, ceiling: int, what: str) -> bytes:
    """Read a plain file only after its size is known to fit both file and ceiling."""
    _check_file(path, ceiling, what)
    return path.read_bytes()


def _check_file(path: Path, ceiling: int, what: str) -> None:
    """Require a bounded plain file, without reading it."""
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{path} is not a regular file ({what})")
    size = path.stat().st_size
    if size > ceiling:
        raise ValueError(f"{path} is {size} bytes, over the {ceiling} byte {what} limit")


def _directory_entries(directory: Path) -> list[Path]:
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError(f"source artifacts must be one real directory: {directory}")
    if not (directory / SOURCE_PROVENANCE_NAME).exists():
        raise ValueError(f"source artifacts carry no {SOURCE_PROVENANCE_NAME}")
    entries = sorted(directory.iterdir())
    for entry in entries:
        mode = os.lstat(entry).st_mode
        if not stat.S_ISREG(mode):
            raise ValueError(f"refusing non-regular entry in source artifacts: {entry.name}")
    return entries


def _read_record(directory: Path) -> dict:
    raw = _bounded_file(directory / SOURCE_PROVENANCE_NAME, MAX_RECORD_BYTES, "provenance record")
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"provenance record is not valid JSON: {exc}") from exc
    if not isinstance(record, dict):
        raise ValueError("provenance record root must be an object")
    return record


def load_source_artifacts(
    directory: Path,
    *,
    expected: Mapping[tuple[str, str], SourceIdentity],
    runtime: RuntimePolicy | None = None,
    reviewed_notices: Mapping[str, bytes | str] | None = None,
) -> SourceArtifacts:
    """Validate one artifact directory against the targets a manifest declares.

    `expected` maps every `(package, arch)` the manifest plans from source to the
    identity it pins, and it is the authority: the record's own identity is only
    read so it can be compared with the pinned one. A directory with no pinned
    target, or one whose record describes a different commit, lockfile, compiler,
    or command, is refused here rather than at some later packaging step.
    """
    # Fail on a non-regular entry before parsing anything, then list the
    # directory again: the binary's name is only known once the record has been
    # read, and the check has to be against what is actually there.
    _directory_entries(directory)
    record = _read_record(directory)
    package = str(record.get("package"))
    arch = str(record.get("arch"))
    check_source_target(package, arch)
    identity = expected.get(source_target_key(package, arch))
    if identity is None:
        raise ValueError(
            f"no source target is pinned for {package} {arch}, so this artifact "
            "directory has nothing it could be used for"
        )
    # The expected version comes from the trusted manifest's own identity, never
    # from the record: a record that claimed its own version would be comparing
    # itself with itself. The canonical tag is the only place that version is
    # written down, so it is resolved here and the record's copy is compared
    # against it exactly below.
    expected_version = version_from_tag(identity.tag, prefix=tag_prefix_for(package))
    if expected_version is None:
        raise ValueError(
            f"the pinned identity tag {identity.tag!r} names no canonical version "
            f"for {package}"
        )
    claimed = record.get("version")
    if not isinstance(claimed, str) or claimed != expected_version:
        raise ValueError(
            f"source artifact record version must be exactly {expected_version!r}, "
            f"got {claimed!r}"
        )
    check_tag_version(claimed, identity.tag, prefix=tag_prefix_for(package))
    measured = record.get("artifact")
    if not isinstance(measured, dict):
        raise ValueError(f"{package} {arch} provenance record states no measured artifact")
    artifact = parse_artifact(measured)
    check_source_build(
        record, SourceRecordExpectation(identity, package, expected_version, arch)
    )
    policy = runtime if runtime is not None else reviewed_runtime(package)

    binary_name = identity.build.binary
    allowed = {SOURCE_PROVENANCE_NAME, PLAN_NAME, binary_name, *NOTICE_FILES}
    names = {entry.name for entry in _directory_entries(directory)}
    unexpected = sorted(names - allowed)
    if unexpected:
        raise ValueError(
            f"source artifacts carry unexpected files: {', '.join(unexpected)}"
        )
    missing = sorted(name for name in (*NOTICE_FILES, binary_name) if name not in names)
    if missing:
        raise ValueError(f"source artifacts are missing: {', '.join(missing)}")

    binary = directory / binary_name
    _check_file(binary, MAX_BINARY_BYTES, "compiled binary")
    digest = sha256_file(binary)
    if digest != artifact.sha256:
        raise ValueError(
            f"{binary.name} has sha256 {digest[:12]}, but its provenance record "
            f"declares {artifact.sha256[:12]}"
        )
    size = binary.stat().st_size
    if size != artifact.bytes:
        raise ValueError(
            f"{binary.name} is {size} bytes, but its provenance record declares "
            f"{artifact.bytes}"
        )
    check_elf_arch(binary, arch)
    _, machine = read_elf(binary)
    if machine != artifact.elf_machine or machine != ELF_MACHINE[arch]:
        raise ValueError(
            f"{binary.name} has ELF machine {machine}, but its provenance record "
            f"declares {artifact.elf_machine} and {arch} requires {ELF_MACHINE[arch]}"
        )
    check_runtime(binary, arch, policy)

    notices: dict[str, Path] = {}
    for name in NOTICE_FILES:
        path = directory / name
        raw = _bounded_file(path, MAX_NOTICE_BYTES, "licence notice")
        if not raw.strip():
            raise ValueError(f"licence notice {name} is empty")
        if reviewed_notices is not None:
            if name not in reviewed_notices:
                raise ValueError(f"{name} is not a reviewed licence notice")
            reviewed = reviewed_notices[name]
            wanted = reviewed if isinstance(reviewed, bytes) else reviewed.encode("utf-8")
            if raw != wanted:
                raise ValueError(
                    f"{name} is not the reviewed licence text this package installs"
                )
        notices[name] = path
    return SourceArtifacts(
        package=package,
        arch=arch,
        identity=identity,
        artifact=artifact,
        binary=binary,
        notices=MappingProxyType(notices),
        record=directory / SOURCE_PROVENANCE_NAME,
    )


def select_source_artifacts(
    artifacts: Mapping[tuple[str, str], SourceArtifacts],
    name: str,
    arch: str,
    target: ArchTarget,
) -> SourceArtifacts:
    """Return the artifact a manifest target may be built from, or refuse.

    The manifest is the authority: the artifact must state the same identity the
    target pins, and a target that already publishes a measured artifact must be
    matched by exactly that artifact. Neither check is a comparison a caller can
    skip, because skipping either is how a published source package gets replaced
    under a version clients already installed.
    """
    key = source_target_key(name, arch)
    pinned = target.source_build
    if pinned is None:
        raise ValueError(
            f"{name} {arch} has no source_build block: it is not built from reviewed source"
        )
    found = artifacts.get(key)
    if found is None:
        raise ValueError(
            f"{name} {arch} is built from reviewed source and needs its compiled "
            "artifact directory; nothing was supplied for it"
        )
    identity = identity_of(pinned)
    if identity != found.identity:
        raise ValueError(
            f"{name} {arch} artifact identity does not match the pinned source "
            f"identity ({found.identity.commit[:12]} vs {identity.commit[:12]})"
        )
    published = artifact_of(pinned)
    if is_realized(pinned) and published != found.artifact:
        raise ValueError(
            f"{name} {arch} was published with artifact {published.sha256[:12]} but "
            f"this run measured {found.artifact.sha256[:12]}; a published release is immutable"
        )
    return found


__all__ = [
    "NOTICE_FILES",
    "ReusedSource",
    "ReviewedRuntime",
    "SourceArtifacts",
    "expected_source_identities",
    "index_source_artifacts",
    "load_source_artifacts",
    "production_reviewed_notices",
    "reviewed_runtime",
    "reuse_published_source",
    "select_source_artifacts",
]