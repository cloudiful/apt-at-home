"""Typed source identity and realized artifact provenance for a source-built target.

A source-built target has no release asset to pin. Upstream publishes a tag, a
commit, and a lockfile; the binary exists only after something compiled it. This
module gives that fact two explicit shapes instead of one ambiguous one.

`SourceIdentity` is the *plan*: the reviewed repository, tag, commit, lockfile git
object, toolchain channel, and the exact cargo command. It is what a manifest can
declare before anything is compiled, and it is what two runs of the same version
are compared against.

`ArtifactProvenance` is the *measurement*: the sha256, size, and ELF machine of a
binary a build actually produced. It may only sit next to a plan once that binary
exists, which is why a planned target carries no artifact block at all rather than
an empty one, a zero digest, or an invented `https://` download URL.

Both shapes are closed. Unknown keys are refused, every field is shape-checked,
and a command is checked against the target, profile, manifest, and binary it
declares, so a manifest cannot describe a build that no reviewed recipe produces.
That is the difference between an honest "not compiled yet" and a claim.

The provenance *record* a compiled artifact directory carries is a different
document on purpose: it is standalone, so it names the package, version, and
architecture it describes next to the identity, and it adds the measured compiler
identity lines. `scripts.tauri_candidate_provenance` reads and writes that record
and compares it with the identity types defined here.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from .download import canonical_member

SCHEMA = "apt-at-home/source-build/1"

# The provenance document a source-built package installs, and the producer's own
# plan file. The plan is accepted in an artifact directory but never read here: the
# record and the measured binary are the authority, not the producer's summary.
SOURCE_PROVENANCE_NAME = "build-provenance.json"
PLAN_NAME = "source-plan.json"

# The one package/architecture pair whose source-built target this repository
# reviews. Upstream publishes no Linux arm64 asset for it, so the alternative to
# compiling reviewed source is not shipping that architecture at all.
SOURCE_TARGETS: frozenset[tuple[str, str]] = frozenset({("cargo-tauri", "arm64")})

_IDENTITY_KEYS = frozenset(
    {"schema", "repository", "tag", "commit", "lockfile", "toolchain", "build"}
)
_LOCK_KEYS = frozenset({"path", "git_blob", "bytes", "sha256"})
_TOOLCHAIN_KEYS = frozenset({"channel"})
_BUILD_KEYS = frozenset({"manifest", "command", "target", "profile", "binary"})
_ARTIFACT_KEYS = frozenset({"sha256", "bytes", "elf_machine"})

_REPOSITORY = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
# A complete stable version: exactly three numeric parts, with no prerelease or
# build suffix, so a tag remainder that is only a suffix of one cannot pass.
_STABLE_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_CHANNEL = re.compile(r"^[0-9]+(\.[0-9]+)*$")
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
# One argv element: an optional leading dash, then no separator, no space, and no
# shell metacharacter, so a declared command can only ever be a sequence of
# arguments and never a shell fragment.
_ARGV = re.compile(r"^-?[A-Za-z0-9][A-Za-z0-9._+/=:-]*$|^--[A-Za-z0-9][A-Za-z0-9._+/=:-]*$")


def source_target_key(name: str, arch: str) -> tuple[str, str]:
    """Return the map key one source-built package/architecture is filed under."""
    return (name, arch)


def check_source_target(name: str, arch: str) -> None:
    """Refuse a source-built target outside the reviewed package and architecture."""
    if (name, arch) not in SOURCE_TARGETS:
        reviewed = ", ".join(f"{package} {architecture}" for package, architecture in sorted(SOURCE_TARGETS))
        raise ValueError(
            f"{name} {arch} may not be built from source; the reviewed source "
            f"target is {reviewed}"
        )


@dataclass(frozen=True)
class Lockfile:
    """The exact lockfile object a reviewed build reads, in both of its identities."""

    path: str
    git_blob: str
    bytes: int
    sha256: str

    def document(self) -> dict[str, object]:
        return {
            "path": self.path,
            "git_blob": self.git_blob,
            "bytes": self.bytes,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class Toolchain:
    """The pinned compiler channel.

    Only the channel is pinned here. The full `rustc -Vv` and `cargo -V` lines
    carry a build hash and a date that only the executing build can measure, so
    they live in the provenance record rather than in the plan.
    """

    channel: str

    def document(self) -> dict[str, str]:
        return {"channel": self.channel}


@dataclass(frozen=True)
class BuildRecipe:
    """The reviewed cargo invocation: what is built, for which target and profile."""

    manifest: str
    command: tuple[str, ...]
    target: str
    profile: str
    binary: str

    def document(self) -> dict[str, object]:
        return {
            "manifest": self.manifest,
            "command": list(self.command),
            "target": self.target,
            "profile": self.profile,
            "binary": self.binary,
        }


@dataclass(frozen=True)
class SourceIdentity:
    """Everything a reviewed source build is reproducible from, and nothing more."""

    repository: str
    tag: str
    commit: str
    lockfile: Lockfile
    toolchain: Toolchain
    build: BuildRecipe

    def document(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "repository": self.repository,
            "tag": self.tag,
            "commit": self.commit,
            "lockfile": self.lockfile.document(),
            "toolchain": self.toolchain.document(),
            "build": self.build.document(),
        }


@dataclass(frozen=True)
class ArtifactProvenance:
    """What one executed build produced: identity facts a plan cannot carry."""

    sha256: str
    bytes: int
    elf_machine: int

    def document(self) -> dict[str, object]:
        return {"sha256": self.sha256, "bytes": self.bytes, "elf_machine": self.elf_machine}


@dataclass(frozen=True)
class RealizedSource:
    """A source identity plus the artifact an executed build measured from it."""

    identity: SourceIdentity
    artifact: ArtifactProvenance


def identity_of(value: SourceIdentity | RealizedSource) -> SourceIdentity:
    """Return the identity of either shape, so callers never branch on the union."""
    return value.identity if isinstance(value, RealizedSource) else value


def artifact_of(value: SourceIdentity | RealizedSource) -> ArtifactProvenance | None:
    """Return the measured artifact, or None while a target is still only a plan."""
    return value.artifact if isinstance(value, RealizedSource) else None


def is_realized(value: SourceIdentity | RealizedSource) -> bool:
    return isinstance(value, RealizedSource)


def realize(identity: SourceIdentity, artifact: ArtifactProvenance) -> RealizedSource:
    """Return the realized form of a plan, attaching one measured artifact."""
    if not isinstance(identity, SourceIdentity):
        raise ValueError("only a planned source identity can be realized")
    return RealizedSource(identity=identity, artifact=artifact)


def document(value: SourceIdentity | RealizedSource) -> dict[str, object]:
    """Render either shape as its deterministic manifest document."""
    data = identity_of(value).document()
    artifact = artifact_of(value)
    if artifact is not None:
        data["artifact"] = artifact.document()
    return data


def _closed(label: str, raw: object, keys: frozenset[str]) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be an object")
    missing = keys - set(raw)
    if missing:
        raise ValueError(f"{label} missing keys: {sorted(missing)}")
    unknown = set(raw) - keys
    if unknown:
        raise ValueError(f"{label} has unknown keys: {sorted(unknown)}")
    return raw


def _count(raw: object, label: str) -> int:
    """Return a JSON number that is a real integer, never a bool or a string."""
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(f"{label} must be an integer")
    return raw


def _sha256(label: str, value: object) -> str:
    digest = str(value).lower()
    if not _SHA256.match(digest):
        raise ValueError(f"{label} needs a full lowercase sha256, got {value!r}")
    return digest


def _token(label: str, value: object) -> str:
    text = str(value)
    if not _TOKEN.match(text):
        raise ValueError(f"{label} is not a single safe token: {value!r}")
    return text


def parse_lockfile(raw: object) -> Lockfile:
    """Parse the lockfile block; the git object and the file digest are both pinned."""
    data = _closed("source build lockfile", raw, _LOCK_KEYS)
    try:
        path = canonical_member(str(data["path"]))
    except ValueError as exc:
        raise ValueError(f"invalid source build lockfile path: {exc}") from None
    blob = str(data["git_blob"]).lower()
    if not _COMMIT.match(blob):
        raise ValueError(f"source build lockfile git_blob is not a git object id: {blob!r}")
    size = _count(data["bytes"], "source build lockfile bytes")
    if size <= 0:
        raise ValueError("source build lockfile bytes must be positive")
    return Lockfile(
        path=path, git_blob=blob, bytes=size, sha256=_sha256("source build lockfile sha256", data["sha256"])
    )


def parse_toolchain(raw: object) -> Toolchain:
    data = _closed("source build toolchain", raw, _TOOLCHAIN_KEYS)
    channel = str(data["channel"])
    if not _CHANNEL.match(channel):
        raise ValueError(f"source build toolchain channel is not a version: {channel!r}")
    return Toolchain(channel=channel)


def _command_value(argv: tuple[str, ...], flag: str) -> str:
    if argv.count(flag) != 1:
        raise ValueError(f"source build command must pass {flag} exactly once")
    index = argv.index(flag)
    if index + 1 >= len(argv):
        raise ValueError(f"source build command passes {flag} without a value")
    return argv[index + 1]


def parse_build(raw: object) -> BuildRecipe:
    """Parse the build block and prove the command matches what the block declares.

    A command that disagrees with its own target, profile, manifest, or binary is
    refused rather than trusted: the recorded argv is what a build runs, so an
    argv that merely looks right is not evidence of anything.
    """
    data = _closed("source build recipe", raw, _BUILD_KEYS)
    try:
        manifest = canonical_member(str(data["manifest"]))
    except ValueError as exc:
        raise ValueError(f"invalid source build manifest path: {exc}") from None
    target = _token("source build target", data["target"])
    profile = _token("source build profile", data["profile"])
    binary = _token("source build binary", data["binary"])
    command = data["command"]
    if not isinstance(command, list) or not command:
        raise ValueError("source build command must be a non-empty list")
    argv: list[str] = []
    for part in command:
        if not isinstance(part, str) or not _ARGV.match(part):
            raise ValueError(f"unsafe source build command element: {part!r}")
        argv.append(part)
    frozen = tuple(argv)
    if frozen[0] != "cargo":
        raise ValueError(f"a reviewed source build runs cargo, not {frozen[0]!r}")
    if frozen.count("--locked") != 1:
        raise ValueError("a reviewed source build passes --locked exactly once")
    for flag, want in (
        ("--manifest-path", manifest),
        ("--target", target),
        ("--profile", profile),
        ("--bin", binary),
    ):
        got = _command_value(frozen, flag)
        if got != want:
            raise ValueError(
                f"source build command {flag} {got!r} does not match the declared "
                f"{want!r}"
            )
    return BuildRecipe(
        manifest=manifest, command=frozen, target=target, profile=profile, binary=binary
    )


def parse_identity(raw: object) -> SourceIdentity:
    """Parse the planned identity fields of a source-built target."""
    data = _closed("source build identity", raw, _IDENTITY_KEYS)
    if data["schema"] != SCHEMA:
        raise ValueError(f"unsupported source build schema: {data['schema']!r}")
    repository = str(data["repository"])
    if not _REPOSITORY.match(repository):
        raise ValueError(
            f"source build repository is not an official GitHub repository: {repository!r}"
        )
    tag = str(data["tag"])
    if not _TAG.match(tag):
        raise ValueError(f"source build tag is not a safe release tag: {tag!r}")
    commit = str(data["commit"]).lower()
    if not _COMMIT.match(commit):
        raise ValueError(f"source build commit is not a full git object id: {commit!r}")
    return SourceIdentity(
        repository=repository,
        tag=tag,
        commit=commit,
        lockfile=parse_lockfile(data["lockfile"]),
        toolchain=parse_toolchain(data["toolchain"]),
        build=parse_build(data["build"]),
    )


def parse_artifact(raw: object) -> ArtifactProvenance:
    """Parse the measured artifact block; nothing here is ever a placeholder."""
    data = _closed("source build artifact", raw, _ARTIFACT_KEYS)
    size = _count(data["bytes"], "source build artifact bytes")
    if size <= 0:
        raise ValueError("source build artifact bytes must be positive")
    machine = _count(data["elf_machine"], "source build artifact elf_machine")
    if machine <= 0:
        raise ValueError("source build artifact elf_machine must be a positive ELF machine")
    return ArtifactProvenance(
        sha256=_sha256("source build artifact sha256", data["sha256"]),
        bytes=size,
        elf_machine=machine,
    )


def parse_source_target(raw: object) -> SourceIdentity | RealizedSource:
    """Parse a `source_build` block into its planned or realized shape."""
    if not isinstance(raw, dict):
        raise ValueError("source_build must be an object")
    identity = parse_identity({key: value for key, value in raw.items() if key != "artifact"})
    if "artifact" not in raw:
        return identity
    return RealizedSource(identity=identity, artifact=parse_artifact(raw["artifact"]))


def version_from_tag(tag: str, *, prefix: str) -> str | None:
    """Return the complete stable version a canonical source tag names.

    The whole remainder after the prefix must be the version: a suffix or
    substring match is not a naming scheme, it is a coincidence, and `12.2`
    "matches" `tauri-cli-v2.12.2` without being any version of that release.
    """
    if not isinstance(tag, str) or not tag.startswith(prefix):
        return None
    version = tag.removeprefix(prefix)
    return version if _STABLE_VERSION.match(version) else None


def check_tag_version(version: str, tag: str, *, prefix: str) -> None:
    """Require the source tag to be exactly the one this package version publishes.

    A target cannot claim version 2.12.3 while its tag still says 2.12.2: the
    commit, the lockfile, and the compiler all belong to the tag, so the two
    disagreeing is a mismatch rather than a formatting detail. For the same
    reason the comparison is an exact equality against the canonical tag, never
    a prefix or suffix containment test.
    """
    named = version_from_tag(tag, prefix=prefix)
    if named is None or named != version:
        raise ValueError(
            f"source build tag {tag!r} is not the canonical {prefix}{version} tag"
        )


def source_targets(
    data: Mapping[str, object]
) -> Iterator[tuple[tuple[str, str], SourceIdentity | RealizedSource]]:
    """Yield every source-built target of a manifest document with its identity."""
    packages = data.get("packages")
    if not isinstance(packages, list):
        return
    for entry in packages:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name"))
        targets = entry.get("targets")
        if not isinstance(targets, dict):
            continue
        for arch, target in targets.items():
            if not isinstance(target, dict) or target.get("source_build") is None:
                continue
            yield source_target_key(name, str(arch)), parse_source_target(target["source_build"])


def realize_document(
    data: Mapping[str, object], artifacts: Mapping[tuple[str, str], ArtifactProvenance]
) -> dict[str, object]:
    """Return the document with each measured artifact merged into its plan.

    A target that already carries an artifact keeps it: a published release is
    immutable, so a measurement that differs from what was published is refused
    here rather than written over. A target with no measurement stays planned,
    and `check_realized_document` is what refuses to sign it.
    """
    realized = copy.deepcopy(dict(data))
    for key, value in source_targets(realized):
        measured = artifacts.get(key)
        if isinstance(value, RealizedSource):
            if measured is not None and measured != value.artifact:
                raise ValueError(
                    f"{key[0]} {key[1]} was published with artifact "
                    f"{value.artifact.sha256[:12]} but this run measured "
                    f"{measured.sha256[:12]}; a published release is immutable"
                )
            continue
        if measured is None:
            continue
        entry = next(
            item
            for item in realized["packages"]  # type: ignore[index]
            if isinstance(item, dict) and str(item.get("name")) == key[0]
        )
        targets = entry["targets"]  # type: ignore[index]
        targets[key[1]]["source_build"] = document(realize(value, measured))  # type: ignore[index]
    return realized


def check_realized_document(data: Mapping[str, object]) -> None:
    """Refuse a manifest that would publish a source target nothing has built.

    Signing binds these bytes into the Release, so a planned block reaching the
    signature would publish a source package with no provenance for the bytes it
    was built from.
    """
    for (name, arch), value in source_targets(data):
        if not is_realized(value):
            raise ValueError(
                f"{name} {arch} carries a planned source build with no artifact "
                "provenance; a source-built target must be realized before it is signed"
            )