"""Validate the Tauri CLI candidate descriptor and build its Package from reviewed pins.

The candidate is deliberately *not* registered in the production catalog: the
repository's `--manifest` path discovers new packages through that catalog, and
registering Tauri there would hand it to the updater and publisher. This module
instead reads a closed, reviewed JSON descriptor and constructs the very same
`Manifest`/`Package`/`ArchTarget` dataclasses the rest of the pipeline already
uses, so the candidate reuses the existing staging and `dpkg-deb` machinery
instead of forking it.

Upstream publishes no Linux arm64 release asset for `cargo-tauri`, so the two
architectures do not come from the same kind of input: amd64 is repackaged from
the pinned official asset, while arm64 is compiled from the pinned source by the
native ARM CI job. The arm64 target therefore carries **no** asset URL and **no**
asset digest — an invented URL would be a false provenance claim — and its real
digest lives in the source-build record that the build validates and installs
(see `tauri_candidate_provenance`).

Every field is closed: unknown keys are refused rather than ignored, URLs must
match the exact release-asset path for the pinned tag, upstream references must
equal the values this release was reviewed with, and a hash must be a full-length
digest. Nothing here evaluates input, resolves a "latest" tag, or infers a member
name.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .catalog import check_relations
from .manifest import ArchTarget, Manifest, Package

ARCHES = ("amd64", "arm64")
BINARY = "cargo-tauri"

# ELF e_machine values for the two Debian architectures this repository builds.
# The provenance records carry these numbers, so the mapping lives with the rest
# of the reviewed candidate description rather than inside a checker.
ELF_MACHINE = {"amd64": 62, "arm64": 183}

_VERSION = re.compile(r"^[0-9][A-Za-z0-9.+~:-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DEPENDENCY = re.compile(r"^[0-9][A-Za-z0-9.+~-]*$")

# Pins reviewed against the `tauri-apps/tauri` tag on 2026-10-12. They are
# compared exactly rather than shape-checked, so a descriptor cannot claim a tag,
# commit, lockfile, or toolchain this release was never built from.
REVIEWED_TAG = "tauri-cli-v2.12.2"
REVIEWED_COMMIT = "5b23c82ea5ba6b9aec420dd71f60493edc486aa9"
REVIEWED_REPOSITORY = "https://github.com/tauri-apps/tauri"
REVIEWED_LOCK_PATH = "Cargo.lock"
REVIEWED_LOCK_BLOB = "6b052c58398b8d6ec246d77f65a748448cd4c859"
REVIEWED_LOCK_BYTES = 279903
REVIEWED_LOCK_SHA256 = "e2bd9e7d86c343e1b484ad3e6e30528451040894279009eb3ddb49b77338e1ca"
REVIEWED_MANIFEST = "crates/tauri-cli/Cargo.toml"
REVIEWED_TOOLCHAIN = "1.99.0"
REVIEWED_TARGET = "aarch64-unknown-linux-gnu"
REVIEWED_PROFILE = "release-size-optimized"
REVIEWED_VERSION = "2.12.2"

# The one release asset that exists for this candidate: upstream publishes no
# Linux arm64 asset, so this descriptor names exactly one.
REVIEWED_ASSET_NAME = "cargo-tauri-x86_64-unknown-linux-gnu.tgz"
REVIEWED_ASSET_SHA256 = "35c7b50266256cb6d5f757608d7097a59a5faeb0c75879ee1f504652a9b6087e"
REVIEWED_ASSET_SIZE = 8651900
REVIEWED_ASSET_MEMBERS = ("LICENSE-APACHE-2.0", "LICENSE-MIT", "README.md", "cargo-tauri")
REVIEWED_LICENSE_FILES = ("LICENSE-MIT.txt", "LICENSE-APACHE-2.0.txt")
REVIEWED_LICENSE_MEMBERS = {
    "LICENSE-MIT.txt": "LICENSE-MIT",
    "LICENSE-APACHE-2.0.txt": "LICENSE-APACHE-2.0",
}
REVIEWED_BUILD_COMMAND = (
    "cargo", "build", "--locked",
    "--manifest-path", REVIEWED_MANIFEST,
    "--target", REVIEWED_TARGET,
    "--profile", REVIEWED_PROFILE,
    "--bin", BINARY,
)

# The closed runtime mapping: every shared library the reviewed binaries may link,
# the Debian package that carries it, the minimum version this candidate declares,
# and the symbol-version namespace that version is measured in. `None` marks a
# library that upstream ships without symbol versions: its version is still
# declared, but it is justified by the package that ships it rather than by a
# symbol namespace, and a binary that claims versions from it is refused.
# The mapping is pinned in code as well as in the descriptor so a floor cannot be
# widened by editing JSON alone; a wider floor needs both a descriptor and a code
# change. One floor serves both architectures: the libc6 floor is the highest one
# either reviewed binary needs, measured from the native ARM build (GLIBC_2.39)
# and therefore above what the amd64 asset requires (GLIBC_2.34). Debian 13 ships
# 2.41, and the candidate targets Debian 13 only.
REVIEWED_RUNTIME = {
    "libbz2.so.1.0": ("libbz2-1.0", "1.0.8-6", None),
    "libc.so.6": ("libc6", "2.39", "GLIBC_"),
    "libgcc_s.so.1": ("libgcc-s1", "14.2.0", "GCC_"),
    "liblzma.so.5": ("liblzma5", "5.8.1", "XZ_"),
    "libm.so.6": ("libc6", "2.39", "GLIBC_"),
}

_ALLOWED_KEYS = {
    "schema", "package", "version", "maintainer", "section", "priority",
    "debian_revision", "description", "homepage", "license", "copyright",
    "license_files", "source", "official_asset", "source_build", "runtime",
}
_SOURCE_KEYS = {"repository", "tag", "commit"}
_ASSET_KEYS = {"arch", "url", "sha256", "size", "binary", "members", "license_members"}
_BUILD_KEYS = {
    "arch", "manifest", "lockfile", "toolchain", "target", "profile",
    "binary", "command",
}
_LOCKFILE_KEYS = {"path", "git_blob", "bytes", "sha256"}
_RUNTIME_KEYS = {"extra_depends", "libraries"}
_LIBRARY_KEYS = {"soname", "package", "version", "symbols"}


@dataclass(frozen=True)
class OfficialAsset:
    """The one upstream release asset this candidate repackages."""

    arch: str
    url: str
    sha256: str
    size: int
    binary: str
    members: tuple[str, ...]
    license_members: Mapping[str, str]


@dataclass(frozen=True)
class SourceBuild:
    """The reviewed native ARM source build: command, toolchain, and lockfile."""

    arch: str
    manifest: str
    lock_path: str
    lock_blob: str
    lock_bytes: int
    lock_sha256: str
    toolchain: str
    target: str
    profile: str
    binary: str
    command: tuple[str, ...]


@dataclass(frozen=True)
class RuntimeLibrary:
    """One closed SONAME to Debian package mapping, with its reviewed minimum.

    `symbols` is the namespace its floor is measured in, or `None` for a library
    upstream ships unversioned — a declared version with no symbol namespace to
    check it against.
    """

    soname: str
    package: str
    version: str
    symbols: str | None


@dataclass(frozen=True)
class Candidate:
    """The reviewed candidate: its Manifest, Package, and per-arch provenance."""

    manifest: Manifest
    package: Package
    official: OfficialAsset
    source_build: SourceBuild
    libraries: tuple[RuntimeLibrary, ...]
    extra_depends: str

    def depends(self) -> str:
        return dependency_string(self.libraries, self.extra_depends)

    def sonames(self) -> tuple[str, ...]:
        return tuple(sorted(library.soname for library in self.libraries))

    def declared_floors(self) -> dict[str, str]:
        """The declared minimum version per Debian package, highest first.

        Two SONAMEs share a package (`libc.so.6` and `libm.so.6`), so the higher
        reviewed minimum is the one the package must satisfy.
        """
        floors: dict[str, str] = {}
        for library in self.libraries:
            current = floors.get(library.package)
            if current is None or _version_key(library.version) > _version_key(current):
                floors[library.package] = library.version
        return floors


def default_candidate_path() -> Path:
    return Path(__file__).resolve().parent.parent / "packaging" / "tauri-cli" / "candidate.json"


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def release_tag(version: str) -> str:
    """The one release tag this candidate version may name, upstream's own naming."""
    return f"tauri-cli-v{version}"


def release_url(version: str, asset: str) -> str:
    """The exact release-asset URL for the pinned tag; nothing else is accepted."""
    return f"{REVIEWED_REPOSITORY}/releases/download/{release_tag(version)}/{asset}"


def load_candidate(path: Path | None = None) -> Candidate:
    """Read the descriptor into the existing Manifest/Package pair.

    One package only: the candidate is deliberately absent from the production
    catalog, so this Manifest exists solely to carry the reviewed fields into
    the existing filename, control and staging helpers.
    """
    candidate_path = path or default_candidate_path()
    try:
        raw = json.loads(candidate_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"candidate descriptor not found: {candidate_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"candidate descriptor is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("candidate descriptor root must be an object")
    _check_keys("candidate descriptor", raw, _ALLOWED_KEYS, _ALLOWED_KEYS)
    if raw["schema"] != "apt-at-home/tauri-candidate/1":
        raise ValueError(f"unsupported candidate schema: {raw['schema']!r}")
    if raw["package"] != BINARY:
        raise ValueError(f"candidate descriptor describes a package other than {BINARY}")
    version = str(raw["version"])
    if version != REVIEWED_VERSION or not _VERSION.match(version):
        raise ValueError(f"candidate version must be the reviewed {REVIEWED_VERSION}")
    if raw["debian_revision"] != "-1":
        raise ValueError("candidate debian_revision must be '-1'")
    _check_license_files(raw["license_files"])
    _check_source(raw["source"], version)
    official = _parse_asset(raw["official_asset"], version)
    build = _parse_source_build(raw["source_build"])
    libraries, extra_depends = _parse_runtime(raw["runtime"])
    depends = dependency_string(libraries, extra_depends)
    package = Package(
        name=BINARY,
        version=version,
        homepage=str(raw["homepage"]),
        description=str(raw["description"]),
        license=str(raw["license"]),
        copyright=str(raw["copyright"]),
        license_url=str(raw["homepage"]),
        targets={
            # The amd64 target names the asset it repackages. The arm64 target
            # names no asset at all: upstream publishes none, so its URL and
            # digest stay empty and the compiled binary's digest is carried by the
            # source-build record instead.
            official.arch: ArchTarget(
                url=official.url, sha256=official.sha256,
                archive_member=BINARY, binary=BINARY, depends=depends,
            ),
            build.arch: ArchTarget(
                url="", sha256="", archive_member=BINARY, binary=BINARY, depends=depends,
            ),
        },
    )
    manifest = Manifest(
        maintainer=str(raw["maintainer"]),
        section=str(raw["section"]),
        priority=str(raw["priority"]),
        debian_revision="-1",
        packages=(package,),
    )
    # Unconditional: an explicitly supplied descriptor is validated exactly like
    # the default one, so `--candidate packaging/tauri-cli/candidate.json` cannot
    # skip the document check the default path performs.
    check_source_document(candidate_path.parent / "README.source", version)
    return Candidate(
        manifest=manifest, package=package, official=official, source_build=build,
        libraries=libraries, extra_depends=extra_depends,
    )


def _check_keys(label: str, raw: dict, allowed: set[str], required: set[str]) -> None:
    missing = required - set(raw)
    if missing:
        raise ValueError(f"{label} missing keys: {sorted(missing)}")
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"{label} has unknown keys: {sorted(unknown)}")


def _check_license_files(raw: object) -> None:
    """The licence set is fixed: both upstream texts, and nothing else."""
    if raw != list(REVIEWED_LICENSE_FILES):
        raise ValueError(f"candidate license_files must be exactly {list(REVIEWED_LICENSE_FILES)}")


def _check_source(raw: object, version: str) -> None:
    """Upstream references must be the exact reviewed ones, not merely well-shaped.

    A well-formed https URL or a well-formed 40-hex commit proves nothing about
    provenance: a descriptor could name an unrelated repository while still
    passing a shape check.
    """
    if not isinstance(raw, dict):
        raise ValueError("candidate source must be an object")
    _check_keys("candidate source", raw, _SOURCE_KEYS, _SOURCE_KEYS)
    exact = {
        "repository": REVIEWED_REPOSITORY,
        "tag": release_tag(version),
        "commit": REVIEWED_COMMIT,
    }
    for key, want in exact.items():
        got = str(raw[key])
        if got != want:
            raise ValueError(f"candidate source {key} must be exactly {want!r}, got {got!r}")


def _parse_asset(raw: object, version: str) -> OfficialAsset:
    if not isinstance(raw, dict):
        raise ValueError("candidate official_asset must be an object")
    _check_keys("candidate official_asset", raw, _ASSET_KEYS, _ASSET_KEYS)
    if raw["arch"] != "amd64":
        raise ValueError("candidate official_asset must be the amd64 asset")
    url = str(raw["url"])
    expected_url = release_url(version, REVIEWED_ASSET_NAME)
    if url != expected_url:
        raise ValueError(f"candidate official asset url must be exactly {expected_url}")
    sha256 = str(raw["sha256"]).lower()
    if sha256 != REVIEWED_ASSET_SHA256:
        raise ValueError(
            "candidate official asset sha256 must be the digest this release was "
            f"reviewed with: {REVIEWED_ASSET_SHA256}"
        )
    size = int(raw["size"])  # type: ignore[arg-type]
    if size != REVIEWED_ASSET_SIZE:
        raise ValueError(f"candidate official asset size must be {REVIEWED_ASSET_SIZE}")
    if raw["binary"] != BINARY:
        raise ValueError(f"candidate official asset binary must be {BINARY}")
    members = tuple(str(member) for member in raw["members"])  # type: ignore[union-attr]
    if members != REVIEWED_ASSET_MEMBERS:
        raise ValueError(
            f"candidate official asset members must be exactly {list(REVIEWED_ASSET_MEMBERS)}"
        )
    license_members = raw["license_members"]
    if not isinstance(license_members, dict) or dict(license_members) != REVIEWED_LICENSE_MEMBERS:
        raise ValueError(
            "candidate official asset license_members must be exactly "
            f"{REVIEWED_LICENSE_MEMBERS}"
        )
    return OfficialAsset(
        arch="amd64", url=url, sha256=sha256, size=size, binary=BINARY,
        members=members, license_members=dict(REVIEWED_LICENSE_MEMBERS),
    )


def _parse_source_build(raw: object) -> SourceBuild:
    if not isinstance(raw, dict):
        raise ValueError("candidate source_build must be an object")
    _check_keys("candidate source_build", raw, _BUILD_KEYS, _BUILD_KEYS)
    if raw["arch"] != "arm64":
        raise ValueError("candidate source_build must be the arm64 build")
    exact = {
        "manifest": REVIEWED_MANIFEST,
        "toolchain": REVIEWED_TOOLCHAIN,
        "target": REVIEWED_TARGET,
        "profile": REVIEWED_PROFILE,
        "binary": BINARY,
    }
    for key, want in exact.items():
        got = str(raw[key])
        if got != want:
            raise ValueError(f"candidate source_build {key} must be exactly {want!r}, got {got!r}")
    command = raw["command"]
    if not isinstance(command, list) or tuple(str(part) for part in command) != REVIEWED_BUILD_COMMAND:
        raise ValueError(
            "candidate source_build command must be exactly "
            f"{' '.join(REVIEWED_BUILD_COMMAND)!r}"
        )
    lockfile = raw["lockfile"]
    if not isinstance(lockfile, dict):
        raise ValueError("candidate source_build lockfile must be an object")
    _check_keys("candidate source_build lockfile", lockfile, _LOCKFILE_KEYS, _LOCKFILE_KEYS)
    lock_blob = str(lockfile["git_blob"])
    if lock_blob != REVIEWED_LOCK_BLOB:
        raise ValueError(
            f"candidate source_build lockfile git_blob must be exactly {REVIEWED_LOCK_BLOB}"
        )
    lock_bytes = int(lockfile["bytes"])  # type: ignore[arg-type]
    if lock_bytes != REVIEWED_LOCK_BYTES:
        raise ValueError(f"candidate source_build lockfile bytes must be {REVIEWED_LOCK_BYTES}")
    lock_sha256 = str(lockfile["sha256"]).lower()
    if lock_sha256 != REVIEWED_LOCK_SHA256:
        raise ValueError(
            "candidate source_build lockfile sha256 must be the digest of the pinned "
            f"git blob: {REVIEWED_LOCK_SHA256}"
        )
    return SourceBuild(
        arch="arm64", manifest=REVIEWED_MANIFEST, lock_path=REVIEWED_LOCK_PATH,
        lock_blob=lock_blob, lock_bytes=lock_bytes, lock_sha256=lock_sha256,
        toolchain=REVIEWED_TOOLCHAIN, target=REVIEWED_TARGET, profile=REVIEWED_PROFILE,
        binary=BINARY, command=REVIEWED_BUILD_COMMAND,
    )


def _parse_runtime(raw: object) -> tuple[tuple[RuntimeLibrary, ...], str]:
    """The runtime mapping is closed: one SONAME, one Debian package, one floor.

    Each entry must equal the reviewed triple exactly. Reading `DT_NEEDED` alone
    would not be enough to justify a dependency floor — a binary can link a
    library whose *symbol versions* the declared minimum does not provide — so the
    mapping also pins the symbol namespace each floor is measured in, or records
    that the library is unversioned.
    """
    if not isinstance(raw, dict):
        raise ValueError("candidate runtime must be an object")
    _check_keys("candidate runtime", raw, _RUNTIME_KEYS, _RUNTIME_KEYS)
    entries = raw["libraries"]
    if not isinstance(entries, list) or not entries:
        raise ValueError("candidate runtime libraries must be a non-empty list")
    libraries: list[RuntimeLibrary] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("candidate runtime library must be an object")
        _check_keys("candidate runtime library", entry, _LIBRARY_KEYS, _LIBRARY_KEYS)
        soname = str(entry["soname"])
        if soname in seen:
            raise ValueError(f"candidate runtime lists {soname!r} twice")
        seen.add(soname)
        reviewed = REVIEWED_RUNTIME.get(soname)
        if reviewed is None:
            raise ValueError(f"candidate runtime library {soname!r} is not reviewed")
        package, version = str(entry["package"]), str(entry["version"])
        namespace = entry["symbols"]
        # `null` is a reviewed value, not a missing one: it marks a library whose
        # exports carry no symbol versions.
        symbols = None if namespace is None else str(namespace)
        if (package, version, symbols) != reviewed:
            raise ValueError(
                f"candidate runtime {soname} must be exactly {reviewed}, got "
                f"{(package, version, symbols)!r}"
            )
        if not _DEPENDENCY.match(version):
            raise ValueError(f"candidate runtime version is not a package version: {version!r}")
        libraries.append(RuntimeLibrary(soname=soname, package=package, version=version,
                                        symbols=symbols))
    if seen != set(REVIEWED_RUNTIME):
        raise ValueError(f"candidate runtime must cover exactly {sorted(REVIEWED_RUNTIME)}")
    extra = check_relations("candidate runtime extra_depends", str(raw["extra_depends"]))
    return tuple(sorted(libraries, key=lambda library: library.soname)), extra


def dependency_string(libraries: tuple[RuntimeLibrary, ...], extra_depends: str) -> str:
    """Render the Depends field from the closed mapping, deterministically.

    Two SONAMEs may live in one Debian package (`libc.so.6` and `libm.so.6`), so
    the highest reviewed minimum wins and each package appears once. The relation
    string is grammar-checked before it can reach a control file.
    """
    floors: dict[str, str] = {}
    for library in libraries:
        current = floors.get(library.package)
        if current is None:
            floors[library.package] = library.version
            continue
        if _version_key(library.version) > _version_key(current):
            floors[library.package] = library.version
    terms = [f"{name} (>= {floors[name]})" for name in sorted(floors)]
    if extra_depends:
        terms = [*[term.strip() for term in extra_depends.split(",")], *terms]
    return check_relations("candidate depends", ", ".join(terms))


def version_key(version: str) -> tuple[int, ...]:
    """Compare package and symbol versions as comparable tuples."""
    parts: list[int] = []
    for part in version.split("."):
        digits = "".join(char for char in part if char.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


_version_key = version_key


def reviewed_source_refs(version: str) -> tuple[tuple[str, str], ...]:
    """The complete set of references the installed source document must state.

    Full URLs and exact command lines, not substrings or basenames: a document
    that swapped the upstream host for another one would still contain the tag
    and the file names, so only whole-value checks can tell the reviewed
    provenance from a lookalike.
    """
    return (
        ("source url", f"{REVIEWED_REPOSITORY}/tree/{release_tag(version)}"),
        ("release tag", release_tag(version)),
        ("source commit", REVIEWED_COMMIT),
        ("lockfile git blob", REVIEWED_LOCK_BLOB),
        ("crate manifest", REVIEWED_MANIFEST),
        ("toolchain", REVIEWED_TOOLCHAIN),
        ("target triple", REVIEWED_TARGET),
        ("cargo profile", REVIEWED_PROFILE),
        ("build command", " ".join(REVIEWED_BUILD_COMMAND)),
        ("official asset", release_url(version, REVIEWED_ASSET_NAME)),
        ("official asset digest", REVIEWED_ASSET_SHA256),
    )


def check_source_document(document: Path, version: str) -> None:
    """Refuse a source document that does not state every reviewed reference.

    The candidate installs this file verbatim, so a document that disagrees with
    the descriptor would make the package advertise provenance the descriptor
    never authorised. This runs on every entrypoint — the default descriptor, an
    explicit `--candidate` path, and again in the build stage before the file is
    copied — so no path can install an unchecked document.
    """
    try:
        text = document.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ValueError(f"{document} is missing, so no source document can be installed") from exc
    for label, reference in reviewed_source_refs(version):
        if reference not in text:
            raise ValueError(
                f"{document.name} does not state the reviewed {label}: {reference!r}"
            )

    lines = text.splitlines()

    def section(heading: str) -> list[str]:
        starts = [index for index, line in enumerate(lines) if line == heading]
        if len(starts) != 1:
            raise ValueError(f"{document.name} must contain one {heading!r} section")
        start = starts[0]
        end = next((index for index in range(start + 1, len(lines))
                    if lines[index].startswith("## ")), len(lines))
        return lines[start + 1:end]

    source = section("## Corresponding upstream source")
    rows = (
        f"| Release tag | `tauri-cli-v{version}` |",
        f"| Source commit | `{REVIEWED_COMMIT}` |",
        f"| Crate manifest | `{REVIEWED_MANIFEST}` |",
        f"| `Cargo.lock` git blob | `{REVIEWED_LOCK_BLOB}` |",
        f"| Rust toolchain | `{REVIEWED_TOOLCHAIN}` |",
        f"| Target triple | `{REVIEWED_TARGET}` |",
        f"| Cargo profile | `{REVIEWED_PROFILE}` |",
    )
    if any(source.count(row) != 1 for row in rows):
        raise ValueError(f"{document.name} has inconsistent reviewed source table rows")
    source_url = f"{REVIEWED_REPOSITORY}/tree/{release_tag(version)}"
    source_tree = source.index("Source tree:")
    next_line = next((line.strip() for line in source[source_tree + 1:] if line.strip()), None)
    if next_line != f"<{source_url}>" or re.findall(
        r"https?://[^\s)>]+", "\n".join(source)
    ) != [source_url]:
        raise ValueError(f"{document.name} must link the reviewed source URL under Source tree")
    build = " ".join(" ".join(section("## Rebuilding from source")).split())
    if " ".join(REVIEWED_BUILD_COMMAND) not in build:
        raise ValueError(
            f"{document.name} does not state the reviewed cargo build command"
        )


def license_bodies(root: Path | None = None) -> tuple[str, ...]:
    """Read the reviewed licence texts for the candidate copyright file."""
    base = root or Path(__file__).resolve().parent.parent / "packaging" / "licenses" / "tauri-cli"
    return tuple((base / name).read_text(encoding="utf-8") for name in REVIEWED_LICENSE_FILES)
