"""Decide, without any credential, whether a source target has to be compiled.

The producer runs on native ARM CI with no production secret, no signing material,
and no private storage access. Everything it needs to decide is public: the
gateway's own key, the clearsigned and detached-signed `Release`, and the signed
`Packages` index it points at. The private pointer and the bound manifest are
never read, because a caller that cannot see the baseline cannot claim to have
verified it.

The decision itself is a comparison of one desired version against one published
version, taken from two different authorities. `upstream` asks the official API
what release exists and resolves that release's exact source identity — tag,
commit, and lockfile object — before anything is fetched from the repository
checkout. `manifest` takes the identity from the committed manifest, which is the
only input a manual rebuild has. Both produce the same closed plan document, and
the plan never carries an artifact block: it states what would be built, and the
measured bytes only exist once a compiler has produced them.

Two rules keep the plan from becoming a promise nobody verified. A missing or
malformed public trust input fails closed, because a plan produced from an
unverified index is indistinguishable from a verified one downstream. An
explicitly declared catalog package that is absent from valid indices means a
build is needed, and that is the one absence the index may legitimately report.

The plan also answers one question for the manual-manifest path, without reading
anything: `published_in_plan` reports whether this repository already publishes
the version the committed document declares, which is what tells the publisher it
may carry the published deb over instead of compiling it again.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from .catalog import tag_prefix_for
from .common import sha256_bytes
from .fingerprint import check_key_matches_pin
from .manifest import Manifest, load_manifest
from .metadata import COMPONENT, SUITE
from .package_index import parse_stanzas
from .release_hashes import parse_release_hashes
from .release_verify import BaselineUnavailable, verify_release_signatures
from .source_build import (
    PLAN_NAME,
    SourceIdentity,
    check_source_target,
    check_tag_version,
    identity_of,
    parse_identity,
)
from .storage import (
    MAX_METADATA_BYTES,
    ObjectNotFound,
    Source,
    public_source,
)
from .tauri_source import DEFAULT_ARCH, DEFAULT_PACKAGE, discover_identity, latest_stable_version
from .upstream import HttpClient, client_from_env

SCHEMA = "apt-at-home/source-plan/1"

# The sources a producer run can plan from. They are spelled out because the
# committed manifest and the upstream release answer different questions, and a
# mode that fell back to the other would plan a build nobody asked for.
# `validation` is CI-only: it never verifies a published baseline and always
# builds when the committed manifest declares the source target, so validation
# still obtains a native artifact after the version is published.
MODES = ("upstream", "manifest", "validation")

_PLAN_KEYS = frozenset(
    {
        "schema", "package", "arch", "mode", "build_needed", "desired_version",
        "published_version", "source", "trust_fingerprint",
    }
)
_FINGERPRINT = re.compile(r"^[0-9A-F]{40}$")
_VERSION = re.compile(r"^[0-9][A-Za-z0-9.+~:-]*$")


@dataclass(frozen=True)
class PublicBaseline:
    """What the published repository states, proven by its own signed metadata."""

    fingerprint: str
    versions: Mapping[str, str]

    def published_version(self, package: str) -> str | None:
        """Return the version the signed index publishes, or None when it lists none."""
        return self.versions.get(package)


@dataclass(frozen=True)
class SourcePlan:
    """One decision: compile this exact source identity, or do not compile."""

    package: str
    arch: str
    mode: str
    build_needed: bool
    desired_version: str | None
    published_version: str | None
    source: SourceIdentity | None
    fingerprint: str

    def document(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "package": self.package,
            "arch": self.arch,
            "mode": self.mode,
            "build_needed": self.build_needed,
            "desired_version": self.desired_version,
            "published_version": self.published_version,
            "source": None if self.source is None else self.source.document(),
            # The pin this run was configured with. It is the anchor the plan was
            # decided against, not a claim that a comparison happened: a plan that
            # names no source target made no comparison and carries no identity.
            "trust_fingerprint": self.fingerprint,
        }

    def summary(self) -> str:
        """Return the one-line reason a compile is or is not needed."""
        state = "compile" if self.build_needed else "skip compile"
        if self.desired_version is None:
            return (
                f"{self.package} {self.arch} ({self.mode}): {state}; nothing is "
                "declared for this target, so nothing can be compiled from it"
            )
        if self.build_needed:
            return (
                f"{self.package} {self.arch} ({self.mode}): {state} {self.desired_version}; "
                f"the published index has {self.published_version or 'no such package'}"
            )
        return (
            f"{self.package} {self.arch} ({self.mode}): {state}; the published index "
            f"already carries {self.desired_version}"
        )


def _metadata(source: Source, key: str) -> bytes:
    """Read one public metadata object, treating only a confirmed 404 as absent."""
    try:
        return source.get(key, max_bytes=MAX_METADATA_BYTES)
    except ObjectNotFound:
        raise BaselineUnavailable(
            f"the published repository does not serve {key}; the producer cannot "
            "verify the baseline it would plan against"
        ) from None


def check_signed_index(release_text: str, arch: str, payload: bytes) -> None:
    """Require one architecture's `Packages` to be exactly the bytes `Release` signed.

    The same rule the restore chain applies, read through the shared digest parser:
    the signed size and SHA256 must both match, and the compressed index the
    gateway also serves has to be accounted for by the same signature.
    """
    hashes = parse_release_hashes(release_text)
    relative = f"{COMPONENT}/binary-{arch}/Packages"
    entry = hashes["SHA256"].get(relative)
    if entry is None:
        raise BaselineUnavailable(f"the signed Release omits {relative}")
    digest, size = entry
    if len(payload) != size or sha256_bytes(payload) != digest:
        raise BaselineUnavailable(
            f"the published Packages for {arch} does not match the signed SHA256"
        )
    compressed = f"{COMPONENT}/binary-{arch}/Packages.gz"
    if compressed not in hashes["SHA256"] and compressed not in hashes["SHA512"]:
        raise BaselineUnavailable(f"the signed Release omits {compressed}")


def normalize_pin(value: object) -> str:
    """Return the configured trust pin, refusing anything that is not one."""
    pin = str(value).strip().upper()
    if not _FINGERPRINT.match(pin):
        raise ValueError("the producer's trust pin must be a full 40-hex primary fingerprint")
    return pin


def public_baseline(source: Source, *, fingerprint: str, arch: str) -> PublicBaseline:
    """Establish the public trust chain and return what the index publishes.

    The remote key is checked against the pin the caller was configured with, so a
    rotated key makes the producer fail with a clear mismatch instead of quietly
    accepting whatever the gateway currently serves. The private pointer and the
    bound manifest are deliberately not read: they are not part of the public
    surface, and a plan that needed them could not be produced without a
    credential.
    """
    pin = normalize_pin(fingerprint)
    key_bytes = _metadata(source, "key.asc")
    release = _metadata(source, f"dists/{SUITE}/Release")
    inrelease = _metadata(source, f"dists/{SUITE}/InRelease")
    detached = _metadata(source, f"dists/{SUITE}/Release.gpg")
    with tempfile.TemporaryDirectory(prefix="apt-source-plan-") as scratch:
        key_file = Path(scratch) / "key.asc"
        key_file.write_bytes(key_bytes)
        check_key_matches_pin(key_file=key_file, expected=pin)
        verify_release_signatures(
            release=release, inrelease=inrelease, detached=detached, key_file=key_file
        )
    try:
        release_text = release.decode("utf-8")
        payload = _metadata(source, f"dists/{SUITE}/{COMPONENT}/binary-{arch}/Packages")
        index_text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BaselineUnavailable(f"published metadata is not valid UTF-8: {exc}") from exc
    check_signed_index(release_text, arch, payload)

    versions: dict[str, str] = {}
    for stanza in parse_stanzas(index_text):
        name = stanza.fields.get("Package")
        version = stanza.fields.get("Version")
        if name and version:
            versions[name] = version
    return PublicBaseline(fingerprint=pin, versions=MappingProxyType(versions))


def desired_from_manifest(
    manifest: Manifest, package: str, arch: str
) -> tuple[str, SourceIdentity] | None:
    """Return the version and planned identity a committed manifest pins, if any."""
    for entry in manifest.packages:
        if entry.name != package:
            continue
        target = entry.targets.get(arch)
        if target is None or target.source_build is None:
            return None
        identity = identity_of(target.source_build)
        return entry.version, identity
    return None


def plan(
    mode: str,
    *,
    source: Source | None = None,
    fingerprint: str,
    package: str = DEFAULT_PACKAGE,
    arch: str = DEFAULT_ARCH,
    manifest_path: Path | None = None,
    client: HttpClient | None = None,
) -> SourcePlan:
    """Return the decision for one mode, having verified the public baseline first.

    `validation` is the CI-only exception: it reads only the committed
    validated manifest, performs no baseline verification, and builds whenever
    the ARM source target is present. Its published fields are always None so
    no caller mistakes it for a verified production no-op/reuse decision.
    """
    if mode not in MODES:
        raise ValueError(f"source plan mode must be one of {list(MODES)}, got {mode!r}")
    check_source_target(package, arch)
    if mode == "validation":
        declared = desired_from_manifest(load_manifest(manifest_path), package, arch)
        if declared is None:
            return SourcePlan(
                package=package, arch=arch, mode=mode, build_needed=False,
                desired_version=None, published_version=None, source=None,
                fingerprint=normalize_pin(fingerprint),
            )
        version, identity = declared
        check_tag_version(version, identity.tag, prefix=tag_prefix_for(package))
        return SourcePlan(
            package=package, arch=arch, mode=mode, build_needed=True,
            desired_version=version, published_version=None, source=identity,
            fingerprint=normalize_pin(fingerprint),
        )
    if source is None:
        raise ValueError(f"source plan mode {mode!r} needs the public gateway source")
    if mode == "manifest":
        declared = desired_from_manifest(load_manifest(manifest_path), package, arch)
        if declared is None:
            # Nothing is declared for this target, so nothing can be compiled from
            # it and no published version could change that. The plan says so
            # without reading the repository at all.
            return SourcePlan(
                package=package, arch=arch, mode=mode, build_needed=False,
                desired_version=None, published_version=None, source=None,
                fingerprint=normalize_pin(fingerprint),
            )
    baseline = public_baseline(source, fingerprint=fingerprint, arch=arch)
    published = baseline.published_version(package)

    desired: tuple[str, SourceIdentity] | None
    if mode == "manifest":
        desired = declared
    else:
        http = client if client is not None else client_from_env()
        version = latest_stable_version(http)
        desired = (version, discover_identity(http, version, package=package, arch=arch))

    if desired is None:
        return SourcePlan(
            package=package, arch=arch, mode=mode, build_needed=False,
            desired_version=None, published_version=published, source=None,
            fingerprint=baseline.fingerprint,
        )
    version, identity = desired
    check_tag_version(version, identity.tag, prefix=tag_prefix_for(package))
    # One rule for both modes: a build is needed when the published index does not
    # already carry this version. `upstream` publishes against the release it
    # restored, so an unchanged version needs no new compilation and keeps its
    # published deb. `manifest` rebuilds what the committed document declares, and
    # the publisher carries an already published target over unchanged rather than
    # compiling it again, so the same comparison applies.
    return SourcePlan(
        package=package, arch=arch, mode=mode, build_needed=published != version,
        desired_version=version, published_version=published, source=identity,
        fingerprint=baseline.fingerprint,
    )


def parse_plan(raw: object) -> SourcePlan:
    """Parse a plan document, refusing anything a plan may not claim.

    A plan with no identity is only honest when it also has no desired version:
    that is the "nothing is declared for this target" case. Anything else that
    asks to compile must name the exact identity it would build, and a source
    block carrying an artifact is refused here because the artifact does not
    exist until after compilation.
    """
    if not isinstance(raw, dict):
        raise ValueError("source plan must be an object")
    missing = _PLAN_KEYS - set(raw)
    if missing:
        raise ValueError(f"source plan missing keys: {sorted(missing)}")
    unknown = set(raw) - _PLAN_KEYS
    if unknown:
        raise ValueError(f"source plan has unknown keys: {sorted(unknown)}")
    if raw["schema"] != SCHEMA:
        raise ValueError(f"unsupported source plan schema: {raw['schema']!r}")
    package = str(raw["package"])
    arch = str(raw["arch"])
    check_source_target(package, arch)
    mode = str(raw["mode"])
    if mode not in MODES:
        raise ValueError(f"source plan mode must be one of {list(MODES)}, got {mode!r}")
    needed = raw["build_needed"]
    if not isinstance(needed, bool):
        raise ValueError("source plan build_needed must be true or false")
    desired = _optional_version(raw["desired_version"], "desired_version")
    published = _optional_version(raw["published_version"], "published_version")
    identity = None if raw["source"] is None else parse_identity(raw["source"])
    fingerprint = str(raw["trust_fingerprint"]).upper()
    if not _FINGERPRINT.match(fingerprint):
        raise ValueError("source plan trust_fingerprint is not a full 40-hex fingerprint")

    if identity is None:
        if needed or desired is not None:
            raise ValueError(
                "a source plan that builds must name the source identity it builds"
            )
    else:
        if desired is None:
            raise ValueError("a source plan with a source identity must state its version")
        check_tag_version(desired, identity.tag, prefix=tag_prefix_for(package))
    return SourcePlan(
        package=package, arch=arch, mode=mode, build_needed=needed,
        desired_version=desired, published_version=published, source=identity,
        fingerprint=fingerprint,
    )


def _optional_version(value: object, label: str) -> str | None:
    if value is None:
        return None
    version = str(value)
    if not _VERSION.match(version):
        raise ValueError(f"source plan {label} is not a package version: {value!r}")
    return version


def read_plan(path: Path) -> SourcePlan:
    """Read a plan document the producer wrote, refusing anything malformed."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"source plan not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"source plan is not valid JSON: {exc}") from exc
    return parse_plan(raw)


def write_plan(path: Path, value: SourcePlan) -> Path:
    """Write the plan the compile step and the publisher both read."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value.document(), indent=2) + "\n", encoding="utf-8")
    return path


def default_plan_path(root: Path | None = None) -> Path:
    """Return the plan's own name inside a producer's artifact root."""
    return (root or Path("dist")) / "source-artifacts" / PLAN_NAME


def published_in_plan(path: Path) -> bool:
    """Return whether a plan says the repository already publishes that version.

    This is the workflow's cue for the manual-manifest path: the producer planned
    no compile because the published index already carries the committed version,
    so the build may carry the published deb over instead of compiling. Anything
    else — an upstream plan, a plan that compiles, a plan for a target that is not
    declared — says no, and the caller keeps the ordinary artifact path.
    """
    decided = read_plan(path)
    return (
        decided.mode == "manifest"
        and decided.source is not None
        and not decided.build_needed
        and decided.published_version == decided.desired_version
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.source_plan",
        description=(
            "Verify the public repository metadata and decide whether the reviewed "
            "source target has to be compiled."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    planner = sub.add_parser("plan", help="verify the public baseline and write the decision")
    planner.add_argument("--mode", choices=MODES, required=True)
    planner.add_argument(
        "--base-url",
        required=True,
        help="public gateway root URL, e.g. https://apt.example.com/",
    )
    planner.add_argument(
        "--fingerprint", required=True,
        help="the public signing fingerprint the served key.asc must match",
    )
    planner.add_argument("--output", type=Path, default=None, help="where to write the plan")
    planner.add_argument("--package", default=DEFAULT_PACKAGE)
    planner.add_argument("--arch", default=DEFAULT_ARCH)
    planner.add_argument(
        "--manifest", type=Path, default=None,
        help="committed manifest for --mode manifest (default: packaging/manifest.json)",
    )

    published = sub.add_parser(
        "published", help="print whether a plan says the version is already published"
    )
    published.add_argument("--plan", type=Path, required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "published":
            print("true" if published_in_plan(args.plan) else "false")
            return 0
        decided = plan(
            args.mode,
            source=public_source(args.base_url),
            fingerprint=args.fingerprint,
            package=args.package,
            arch=args.arch,
            manifest_path=args.manifest,
        )
        print("true" if decided.build_needed else "false")
        if args.output is not None:
            write_plan(args.output, decided)
            print(f"{args.output}: {decided.summary()}", file=sys.stderr)
        else:
            print(decided.summary(), file=sys.stderr)
    except (ValueError, BaselineUnavailable, OSError) as exc:
        print(f"source planning failed: {exc}", file=sys.stderr)
        return 1
    return 0


__all__ = [
    "MODES",
    "PublicBaseline",
    "SCHEMA",
    "SourcePlan",
    "check_signed_index",
    "default_plan_path",
    "desired_from_manifest",
    "main",
    "normalize_pin",
    "parse_plan",
    "plan",
    "public_baseline",
    "published_in_plan",
    "read_plan",
    "write_plan",
]


if __name__ == "__main__":
    sys.exit(main())