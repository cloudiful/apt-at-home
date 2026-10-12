"""Validate the Bun candidate descriptor and build its Package from reviewed pins.

The candidate is deliberately *not* registered in the production catalog: the
repository's `--manifest` path discovers new packages through that catalog, and
registering Bun there would hand it to the updater and publisher. This module
instead reads a closed, reviewed JSON descriptor and constructs the very same
`Manifest`/`Package`/`ArchTarget` dataclasses the rest of the pipeline already
uses, so the candidate reuses the existing download, staging and `dpkg-deb`
machinery instead of forking it.

Every field is closed: unknown keys are refused rather than ignored, URLs must
match the exact release-asset path for the pinned version, and a hash must be a
full-length digest. Nothing here evaluates input, resolves a "latest" tag, or
infers a member name.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .manifest import ArchTarget, Manifest, Package

VERSION = re.compile(r"^[0-9][A-Za-z0-9.+~:-]*$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
# The commits this release was reviewed against. They are compared exactly, not
# shape-checked, so a descriptor cannot claim provenance this release never had.
_REVIEWED_COMMITS = {
    "bun": "c6da4a4d3010e5553438c60f6bd76d981976867c",
    "webkit": "0c06faadf65bf8e8c8ad3a5a8aca83e1e9ed653f",
    "tinycc": "05f0fafaa3be31e31d7b4b5c17dc60f62c991171",
}

_ARCHES = ("amd64", "arm64")
_RELEASE = "https://github.com/oven-sh/bun/releases/download"

# A closed top-level shape: adding a key to the descriptor must be a deliberate
# code change, never an accidental extension of what the candidate accepts.
_ALLOWED_KEYS = {
    "schema", "package", "version", "maintainer", "section", "priority",
    "debian_revision", "description", "homepage", "license", "copyright",
    "license_files", "source", "targets",
}
_REQUIRED_KEYS = _ALLOWED_KEYS

# Bun ships one release asset per arch, under an exact versioned path.
_EXPECTED_ASSET = {
    "amd64": "bun-linux-x64-baseline.zip",
    "arm64": "bun-linux-aarch64.zip",
}
_EXPECTED_MEMBER = {
    "amd64": "bun-linux-x64-baseline/bun",
    "arm64": "bun-linux-aarch64/bun",
}


@dataclass(frozen=True)
class Candidate:
    """The reviewed candidate: its Manifest, its Package, and pinned asset sizes."""

    manifest: Manifest
    package: Package
    sizes: Mapping[str, int]


def default_candidate_path() -> Path:
    return Path(__file__).resolve().parent.parent / "packaging" / "bun" / "candidate.json"


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
    missing = _REQUIRED_KEYS - set(raw)
    if missing:
        raise ValueError(f"candidate descriptor missing keys: {sorted(missing)}")
    unknown = set(raw) - _ALLOWED_KEYS
    if unknown:
        raise ValueError(f"candidate descriptor has unknown keys: {sorted(unknown)}")
    if raw["schema"] != "apt-at-home/bun-candidate/1":
        raise ValueError(f"unsupported candidate schema: {raw['schema']!r}")
    if raw["package"] != "bun":
        raise ValueError("candidate descriptor describes a package other than bun")
    version = str(raw["version"])
    if not VERSION.match(version):
        raise ValueError(f"invalid candidate version: {version!r}")
    if raw["debian_revision"] != "-1":
        raise ValueError("candidate debian_revision must be '-1'")
    _check_license_files(raw["license_files"])
    _check_source(raw["source"], version)
    # Unconditional: an explicitly supplied descriptor is validated exactly like
    # the default one, so `--candidate packaging/bun/candidate.json` cannot skip
    # the document check the default path performs.
    check_source_document(candidate_path.parent / "README.source", version)
    targets, sizes = _parse_targets(raw["targets"], version)
    package = Package(
        name="bun",
        version=version,
        homepage=str(raw["homepage"]),
        description=str(raw["description"]),
        # A custom label, not an MIT-only blanket: Bun is MIT but statically
        # links LGPL-2 JavaScriptCore/WebKit, so the real answer is "multiple".
        license=str(raw["license"]),
        copyright=str(raw["copyright"]),
        license_url=str(raw["homepage"]),
        targets=targets,
    )
    manifest = Manifest(
        maintainer=str(raw["maintainer"]),
        section=str(raw["section"]),
        priority=str(raw["priority"]),
        debian_revision="-1",
        packages=(package,),
    )
    return Candidate(manifest=manifest, package=package, sizes=sizes)


def _check_license_files(raw: object) -> None:
    """The licence set is fixed: upstream LICENSE plus both GNU LGPL texts."""
    expected = ["LICENSE.txt", "LGPL-2.0.txt", "LGPL-2.1.txt"]
    if raw != expected:
        raise ValueError(f"candidate license_files must be exactly {expected}")


def _check_source(raw: object, version: str) -> None:
    """Source references must be the exact reviewed ones, not merely well-shaped.

    A well-formed https URL or a well-formed 40-hex commit proves nothing about
    provenance: a descriptor could name an unrelated repository while still
    passing a shape check. So every source field is compared against the value
    this release was reviewed with, and the key set itself is closed.
    """
    if not isinstance(raw, dict):
        raise ValueError("candidate source must be an object")
    expected_keys = {
        "url", "tag", "commit", "webview_commit", "tinycc_commit", "patch_url",
    }
    missing = expected_keys - set(raw)
    if missing:
        raise ValueError(f"candidate source missing key: {sorted(missing)}")
    unknown = set(raw) - expected_keys
    if unknown:
        raise ValueError(f"candidate source has unknown keys: {sorted(unknown)}")
    tag = f"bun-v{version}"
    exact = {
        "tag": tag,
        "url": f"https://github.com/oven-sh/bun/tree/{tag}",
        "patch_url": f"https://github.com/oven-sh/bun/blob/{tag}/patches/tinycc/tcc.h.patch",
        "commit": _REVIEWED_COMMITS["bun"],
        "webview_commit": _REVIEWED_COMMITS["webkit"],
        "tinycc_commit": _REVIEWED_COMMITS["tinycc"],
    }
    for key, want in exact.items():
        got = str(raw[key])
        if got != want:
            raise ValueError(f"candidate source {key} must be exactly {want!r}, got {got!r}")


def reviewed_source_refs(version: str) -> tuple[tuple[str, str], ...]:
    """The complete set of references the installed source document must state.

    Full URLs, not substrings or basenames: a document that swapped the upstream
    host for another one would still contain the tag and the patch basename, so
    only whole-value checks can tell the reviewed provenance from a lookalike.
    The two build scripts are included because the document describes how to
    rebuild, and a document that dropped them would be describing a build
    upstream does not provide.
    """
    tag = f"bun-v{version}"
    return (
        ("source url", f"https://github.com/oven-sh/bun/tree/{tag}"),
        ("patch url", f"https://github.com/oven-sh/bun/blob/{tag}/patches/tinycc/tcc.h.patch"),
        ("release tag", tag),
        ("bun commit", _REVIEWED_COMMITS["bun"]),
        ("webkit commit", _REVIEWED_COMMITS["webkit"]),
        ("tinycc commit", _REVIEWED_COMMITS["tinycc"]),
        ("build script", "build:local"),
        ("webkit checkout script", "sync-webkit-source"),
    )


def check_source_document(document: Path, version: str) -> None:
    """Refuse a source document that does not state every reviewed reference.

    The candidate installs this file verbatim, so a document that disagrees with
    the descriptor would make the package advertise provenance the descriptor
    never authorised. This runs on every entrypoint — the default descriptor, an
    explicit `--candidate` path, and again in the build stage before the file is
    copied — so no path can install an unchecked document. A missing document is
    a refusal too, reported the same way as an incomplete one.
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

    tag = f"bun-v{version}"
    source_url = f"https://github.com/oven-sh/bun/tree/{tag}"
    patch_url = f"https://github.com/oven-sh/bun/blob/{tag}/patches/tinycc/tcc.h.patch"
    source = section("## Corresponding upstream source")
    rows = (
        f"| Release tag | `{tag}` |",
        f"| Bun source commit | `{_REVIEWED_COMMITS['bun']}` |",
        f"| WebKit / JavaScriptCore commit | `{_REVIEWED_COMMITS['webkit']}` |",
        f"| TinyCC commit | `{_REVIEWED_COMMITS['tinycc']}` |",
    )
    if any(source.count(row) != 1 for row in rows):
        raise ValueError(f"{document.name} has inconsistent reviewed source table rows")
    source_tree = source.index("Source tree:")
    next_line = next((line.strip() for line in source[source_tree + 1:] if line.strip()), None)
    if next_line != f"<{source_url}>" or re.findall(
        r"https?://[^\s)>]+", "\n".join(source)
    ) != [source_url]:
        raise ValueError(f"{document.name} must link the reviewed source URL under Source tree")
    patch = section("## Pinned build patch")
    if re.findall(r"https?://[^\s)>]+", "\n".join(patch)) != [patch_url]:
        raise ValueError(f"{document.name} must link the reviewed patch URL in its patch section")
    build = " ".join(" ".join(section("## Rebuilding from source")).split())
    instructions = (
        f"1. Clone `oven-sh/bun` at tag `{tag}`.",
        "2. Check out `oven-sh/WebKit` at the commit above into `vendor/WebKit` "
        "(upstream provides `bun sync-webkit-source` to do this at the pinned version).",
        "3. Run `bun run build:local`.",
    )
    if any(instruction not in build for instruction in instructions):
        raise ValueError(f"{document.name} has inconsistent reviewed build instructions")


def _parse_targets(raw: object, version: str) -> tuple[dict[str, ArchTarget], dict[str, int]]:
    """Build the per-arch targets, refusing any asset path that is not exact."""
    if not isinstance(raw, dict):
        raise ValueError("candidate targets must be an object")
    if set(raw) != set(_ARCHES):
        raise ValueError(f"candidate targets must cover exactly {_ARCHES}")
    targets: dict[str, ArchTarget] = {}
    sizes: dict[str, int] = {}
    expected_keys = {"url", "sha256", "size", "member", "depends"}
    for arch, entry in raw.items():
        if not isinstance(entry, dict):
            raise ValueError(f"candidate target for {arch} must be an object")
        missing = expected_keys - set(entry)
        if missing:
            raise ValueError(f"candidate target {arch} missing key: {sorted(missing)}")
        unknown = set(entry) - expected_keys
        if unknown:
            raise ValueError(f"candidate target {arch} has unknown keys: {sorted(unknown)}")
        url = str(entry["url"])
        expected_url = f"{_RELEASE}/bun-v{version}/{_EXPECTED_ASSET[arch]}"
        if url != expected_url:
            raise ValueError(f"candidate URL for {arch} must be exactly {expected_url}")
        sha256 = str(entry["sha256"]).lower()
        if not SHA256.match(sha256):
            raise ValueError(f"candidate target {arch} needs a full sha256")
        size = int(entry["size"])  # type: ignore[arg-type]
        if size <= 0:
            raise ValueError(f"candidate target {arch} needs a positive size")
        member = str(entry["member"])
        if member != _EXPECTED_MEMBER[arch]:
            raise ValueError(f"candidate member for {arch} must be {_EXPECTED_MEMBER[arch]!r}")
        sizes[arch] = size
        targets[arch] = ArchTarget(
            url=url, sha256=sha256, archive_member=member,
            binary="bun", depends=str(entry["depends"]),
        )
    return targets, sizes


def license_bodies(root: Path | None = None) -> tuple[str, ...]:
    """Read the reviewed licence texts for the candidate copyright file."""
    base = root or Path(__file__).resolve().parent.parent / "packaging" / "licenses" / "bun"
    return tuple((base / name).read_text(encoding="utf-8") for name in
                 ("LICENSE.txt", "LGPL-2.0.txt", "LGPL-2.1.txt"))