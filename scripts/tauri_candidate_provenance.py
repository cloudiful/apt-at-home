"""Emit and validate the Tauri CLI candidate's per-architecture provenance records.

The two architectures of this candidate come from different kinds of input, so
each package installs the record that describes its own: the arm64 package
installs `build-provenance.json` written by the native source build, and the
amd64 package installs `official-asset.json` written while the pinned release
asset was repackaged.

Two rules govern this module. First, every record is *closed*: unknown keys are
refused, and each pinned field is compared against the reviewed descriptor rather
than shape-checked, so a record cannot claim a commit, lockfile, toolchain,
command, or architecture this candidate was not built from. Second, a record is
an unsigned description of one build, not a cryptographic proof: nothing here
attests that the bytes came from the named upstream, and the arm64 record is only
ever produced by the job that compiled them from a pinned checkout.

The comparison itself is not candidate-specific. The candidate passes its frozen
pins; a production source target passes the identity its manifest declares, and
the same exact comparisons run against either. The candidate's own defaults stay
frozen in `tauri_candidate_metadata` and are read through `reviewed_identity()`,
so this module holds no second copy of a pin.

The emitter reads its measured facts from the build itself — the checkout's git
object ids, the lockfile bytes, the compiler identities, and the ELF header of the
finished binary — and refuses to write a record when any of them contradicts the
reviewed pins.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .bun_candidate_smoke import read_elf
from .common import sha256_file
from .source_build import (
    SOURCE_PROVENANCE_NAME,
    BuildRecipe,
    Lockfile,
    SourceIdentity,
    Toolchain,
)
from .tauri_candidate_metadata import (
    BINARY,
    Candidate,
    ELF_MACHINE,
    REVIEWED_BUILD_COMMAND,
    REVIEWED_COMMIT,
    REVIEWED_LOCK_BLOB,
    REVIEWED_LOCK_BYTES,
    REVIEWED_LOCK_PATH,
    REVIEWED_LOCK_SHA256,
    REVIEWED_MANIFEST,
    REVIEWED_PROFILE,
    REVIEWED_REPOSITORY,
    REVIEWED_TARGET,
    REVIEWED_TOOLCHAIN,
    REVIEWED_VERSION,
    load_candidate,
    release_tag,
)

SOURCE_BUILD_SCHEMA = "apt-at-home/tauri-source-build/1"
OFFICIAL_ASSET_SCHEMA = "apt-at-home/tauri-official-asset/1"

# One provenance document name, shared with the source-build types: a source-built
# package installs the same file whether a candidate built it or the producer did.
SOURCE_BUILD_NAME = SOURCE_PROVENANCE_NAME
OFFICIAL_ASSET_NAME = "official-asset.json"

# The record each architecture's package installs under /usr/share/doc.
PROVENANCE_NAME_BY_ARCH = {"amd64": OFFICIAL_ASSET_NAME, "arm64": SOURCE_BUILD_NAME}

_SHA256 = re.compile(r"^[0-9a-f]{64}$")

_GIT_TIMEOUT_SECONDS = 60

_RECORD_NOTICE = (
    "Unsigned build record: it describes one build and is not a cryptographic "
    "proof of origin."
)

_SOURCE_KEYS = {"repository", "tag", "commit", "manifest", "lockfile"}
_LOCK_KEYS = {"path", "git_blob", "bytes", "sha256"}
_TOOLCHAIN_KEYS = {"channel", "rustc", "cargo"}
_BUILD_KEYS = {"command", "target", "profile", "binary"}
_ARTIFACT_KEYS = {"sha256", "bytes", "elf_machine"}
_SOURCE_BUILD_KEYS = {
    "schema", "package", "version", "arch", "source", "toolchain", "build",
    "artifact", "notice",
}
_OFFICIAL_KEYS = {"schema", "package", "version", "arch", "asset", "binary", "notices", "notice"}
_ASSET_KEYS = {"url", "sha256", "bytes", "members"}
_NOTICE_KEYS = {"LICENSE-MIT.txt", "LICENSE-APACHE-2.0.txt"}


def _check_keys(label: str, raw: dict, allowed: set[str], required: set[str]) -> None:
    missing = required - set(raw)
    if missing:
        raise ValueError(f"{label} missing keys: {sorted(missing)}")
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"{label} has unknown keys: {sorted(unknown)}")


def reviewed_identity() -> SourceIdentity:
    """Return the candidate's own frozen pins as a typed source identity.

    The values are read from the module globals on every call, so a test that
    patches one of the reviewed pins still exercises the same code path a real
    candidate takes.
    """
    return SourceIdentity(
        repository=REVIEWED_REPOSITORY,
        tag=release_tag(REVIEWED_VERSION),
        commit=REVIEWED_COMMIT,
        lockfile=Lockfile(
            path=REVIEWED_LOCK_PATH, git_blob=REVIEWED_LOCK_BLOB,
            bytes=REVIEWED_LOCK_BYTES, sha256=REVIEWED_LOCK_SHA256,
        ),
        toolchain=Toolchain(REVIEWED_TOOLCHAIN),
        build=BuildRecipe(
            manifest=REVIEWED_MANIFEST, command=REVIEWED_BUILD_COMMAND,
            target=REVIEWED_TARGET, profile=REVIEWED_PROFILE, binary=BINARY,
        ),
    )


def candidate_identity(candidate: Candidate) -> SourceIdentity:
    """Return the identity a loaded candidate describes, pins and all."""
    build = candidate.source_build
    return SourceIdentity(
        repository=REVIEWED_REPOSITORY,
        tag=release_tag(candidate.package.version),
        commit=REVIEWED_COMMIT,
        lockfile=Lockfile(
            path=build.lock_path, git_blob=build.lock_blob,
            bytes=build.lock_bytes, sha256=build.lock_sha256,
        ),
        toolchain=Toolchain(build.toolchain),
        build=BuildRecipe(
            manifest=build.manifest, command=tuple(build.command),
            target=build.target, profile=build.profile, binary=build.binary,
        ),
    )


@dataclass(frozen=True)
class SourceRecordExpectation:
    """What one source-build record must state.

    The identity is compared field by field and exactly. `package`, `version`, and
    `arch` are compared whenever the caller knows them, which is always for a
    production target and always for the candidate.
    """

    identity: SourceIdentity
    package: str = ""
    version: str = ""
    arch: str = ""


def _as_expectation(
    plan: Candidate | SourceIdentity | SourceRecordExpectation,
) -> SourceRecordExpectation:
    """Accept the candidate, a bare identity, or a full expectation."""
    if isinstance(plan, SourceRecordExpectation):
        return plan
    if isinstance(plan, SourceIdentity):
        return SourceRecordExpectation(identity=plan)
    if isinstance(plan, Candidate):
        return SourceRecordExpectation(
            identity=candidate_identity(plan),
            package=BINARY,
            version=plan.package.version,
            arch=plan.source_build.arch,
        )
    raise TypeError(f"cannot check a source build against {type(plan).__name__}")


def identity_from_record(raw: object) -> SourceIdentity:
    """Read the identity a record claims, so each field can be compared exactly.

    This is deliberately tolerant of *value* types: the closed key check and the
    field comparisons belong to `check_source_build`, which runs immediately after
    this. What it will not tolerate is a record whose identity cannot be read at
    all, because nothing can then be compared against it.
    """
    if not isinstance(raw, dict):
        raise ValueError("source build record must be an object")
    source = raw.get("source")
    toolchain = raw.get("toolchain")
    build = raw.get("build")
    for label, value in (
        ("source", source), ("toolchain", toolchain), ("build", build),
    ):
        if not isinstance(value, dict):
            raise ValueError(f"source build record {label} must be an object")
    assert isinstance(source, dict) and isinstance(toolchain, dict) and isinstance(build, dict)
    lock = source.get("lockfile")
    if not isinstance(lock, dict):
        raise ValueError("source build record lockfile must be an object")
    command = build.get("command")
    if not isinstance(command, list) or not all(isinstance(part, str) for part in command):
        raise ValueError("source build record command must be a list of strings")
    return SourceIdentity(
        repository=str(source.get("repository")),
        tag=str(source.get("tag")),
        commit=str(source.get("commit", "")).lower(),
        lockfile=Lockfile(
            path=str(lock.get("path")),
            git_blob=str(lock.get("git_blob", "")).lower(),
            bytes=lock.get("bytes"),  # type: ignore[arg-type]
            sha256=str(lock.get("sha256", "")).lower(),
        ),
        toolchain=Toolchain(channel=str(toolchain.get("channel"))),
        build=BuildRecipe(
            manifest=str(source.get("manifest")),
            command=tuple(command),
            target=str(build.get("target")),
            profile=str(build.get("profile")),
            binary=str(build.get("binary")),
        ),
    )


def _git_output(upstream: Path, *arguments: str) -> str:
    """Run one read-only git query inside the pinned checkout."""
    completed = subprocess.run(
        ["git", "-C", str(upstream), *arguments], text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        raise ValueError(
            f"git {' '.join(arguments)} failed in {upstream}: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    return completed.stdout.strip()


def check_checkout(upstream: Path, plan: SourceIdentity | None = None) -> tuple[str, str]:
    """Confirm the checkout, and the lockfile bytes, are the reviewed ones.

    Git's blob id is SHA-1 over the git object header, which is not a SHA-256
    digest of the file, so the two never replace each other. `HEAD:Cargo.lock`
    only says which object the commit names; it says nothing about the file on
    disk that the build actually read, so the working file is hashed too. The
    record then carries the SHA-256 of those exact bytes, and the checker compares
    it with the pinned digest rather than accepting any well-formed one.

    Without an explicit plan the candidate's own frozen pins are used, which is
    the only plan the candidate's own CLI ever compares against.
    """
    identity = plan if plan is not None else reviewed_identity()
    head = _git_output(upstream, "rev-parse", "HEAD")
    if head != identity.commit:
        raise ValueError(
            f"upstream checkout is at {head}, but this candidate pins {identity.commit}"
        )
    blob = _git_output(upstream, "rev-parse", f"HEAD:{identity.lockfile.path}")
    if blob != identity.lockfile.git_blob:
        raise ValueError(
            f"{identity.lockfile.path} is git blob {blob}, but this candidate pins "
            f"{identity.lockfile.git_blob}"
        )
    lockfile = upstream / identity.lockfile.path
    if not lockfile.is_file():
        raise ValueError(f"{lockfile} is missing from the checkout")
    # The checkout may be named by a relative path, and `git -C <dir>` resolves
    # the file it is given inside that directory, so an upstream-prefixed
    # relative path would be looked up twice. An absolute path is unambiguous.
    working = _git_output(upstream, "hash-object", "--", str(lockfile.resolve()))
    if working != identity.lockfile.git_blob:
        raise ValueError(
            f"the working {identity.lockfile.path} hashes to {working}, not the pinned "
            f"{identity.lockfile.git_blob}; the build would not use the reviewed lockfile"
        )
    return head, blob


def emit_source_build(
    plan: Candidate | SourceIdentity | SourceRecordExpectation,
    upstream: Path,
    binary: Path,
    *,
    rustc: str,
    cargo: str,
) -> dict:
    """Measure one native source build and return its closed record."""
    expectation = _as_expectation(plan)
    if not (expectation.package and expectation.version and expectation.arch):
        raise ValueError(
            "a source build record must state the package, version, and architecture "
            "it describes"
        )
    identity = expectation.identity
    commit, lock_blob = check_checkout(upstream, identity)
    lockfile = upstream / identity.lockfile.path
    lock_actual = lockfile.stat().st_size
    if lock_actual != identity.lockfile.bytes:
        raise ValueError(
            f"{identity.lockfile.path} is {lock_actual} bytes, descriptor pins "
            f"{identity.lockfile.bytes}"
        )
    channel = identity.toolchain.channel
    toolchain = {
        "channel": channel,
        "rustc": check_tool_identity("rustc", rustc, channel),
        "cargo": check_tool_identity("cargo", cargo, channel),
    }
    _, machine = read_elf(binary)
    if machine != ELF_MACHINE[expectation.arch]:
        raise ValueError(
            f"{binary} has ELF machine {machine}, but {expectation.arch} requires "
            f"{ELF_MACHINE[expectation.arch]}"
        )
    return {
        "schema": SOURCE_BUILD_SCHEMA,
        "package": expectation.package,
        "version": expectation.version,
        "arch": expectation.arch,
        "source": {
            "repository": identity.repository,
            "tag": identity.tag,
            "commit": commit,
            "manifest": identity.build.manifest,
            "lockfile": {
                "path": identity.lockfile.path,
                "git_blob": lock_blob,
                "bytes": lock_actual,
                "sha256": sha256_file(lockfile),
            },
        },
        "toolchain": toolchain,
        "build": {
            "command": list(identity.build.command),
            "target": identity.build.target,
            "profile": identity.build.profile,
            "binary": identity.build.binary,
        },
        "artifact": {
            "sha256": sha256_file(binary),
            "bytes": binary.stat().st_size,
            "elf_machine": machine,
        },
        "notice": _RECORD_NOTICE,
    }


def check_tool_identity(tool: str, line: str, channel: str) -> str:
    """Require one measured compiler identity to be the pinned channel's.

    `rustc -Vv` and `cargo -V` both start with `<tool> <channel> (`; the rest of
    the line is the build hash and date, which the record keeps verbatim. The
    channel is what the reviewed policy pins, so any other channel — an automatic
    toolchain bump — is refused rather than recorded.
    """
    identity = line.strip().splitlines()[0].strip() if line.strip() else ""
    if not re.match(rf"^{re.escape(tool)} {re.escape(channel)} \(.+\)$", identity):
        raise ValueError(
            f"{tool} identity {identity!r} does not match the pinned {channel} toolchain"
        )
    return identity


def _checked_identity(tool: str, line: str) -> str:
    """Compare one compiler identity against the candidate's frozen channel."""
    return check_tool_identity(tool, line, REVIEWED_TOOLCHAIN)


def check_source_build(
    raw: object, plan: Candidate | SourceIdentity | SourceRecordExpectation
) -> None:
    """Refuse a source-build record that disagrees with the plan it is checked against."""
    if not isinstance(raw, dict):
        raise ValueError("source build record must be an object")
    _check_keys("source build record", raw, _SOURCE_BUILD_KEYS, _SOURCE_BUILD_KEYS)
    expectation = _as_expectation(plan)
    identity = expectation.identity
    _check_identity_fields(raw, expectation, SOURCE_BUILD_SCHEMA)
    source = raw["source"]
    if not isinstance(source, dict):
        raise ValueError("source build record source must be an object")
    _check_keys("source build record source", source, _SOURCE_KEYS, _SOURCE_KEYS)
    for key, want in (
        ("repository", identity.repository), ("tag", identity.tag),
        ("manifest", identity.build.manifest),
    ):
        if str(source[key]) != want:
            raise ValueError(f"source build record {key} must be exactly {want!r}, got {source[key]!r}")
    # A git object id is hex, so its case carries no meaning and is compared
    # case-insensitively; every other field above is compared exactly.
    if str(source["commit"]).lower() != identity.commit:
        raise ValueError(
            f"source build record commit must be exactly {identity.commit!r}, "
            f"got {source['commit']!r}"
        )
    lock = source["lockfile"]
    if not isinstance(lock, dict):
        raise ValueError("source build record lockfile must be an object")
    _check_keys("source build record lockfile", lock, _LOCK_KEYS, _LOCK_KEYS)
    for key, want in (
        ("path", identity.lockfile.path), ("git_blob", identity.lockfile.git_blob),
        ("bytes", identity.lockfile.bytes), ("sha256", identity.lockfile.sha256),
    ):
        got = lock[key]
        if str(got).lower() != str(want).lower():
            raise ValueError(f"source build record lockfile {key} must be exactly {want!r}, got {got!r}")
    toolchain = raw["toolchain"]
    if not isinstance(toolchain, dict):
        raise ValueError("source build record toolchain must be an object")
    _check_keys("source build record toolchain", toolchain, _TOOLCHAIN_KEYS, _TOOLCHAIN_KEYS)
    if str(toolchain["channel"]) != identity.toolchain.channel:
        raise ValueError(
            f"source build record channel must be exactly {identity.toolchain.channel!r}"
        )
    for tool in ("rustc", "cargo"):
        check_tool_identity(tool, str(toolchain[tool]), identity.toolchain.channel)
    declared = raw["build"]
    if not isinstance(declared, dict):
        raise ValueError("source build record build must be an object")
    _check_keys("source build record build", declared, _BUILD_KEYS, _BUILD_KEYS)
    if [str(part) for part in declared["command"]] != list(identity.build.command):
        raise ValueError("source build record command is not the reviewed build command")
    for key, want in (
        ("target", identity.build.target), ("profile", identity.build.profile),
        ("binary", identity.build.binary),
    ):
        got = str(declared[key])
        if got != want:
            raise ValueError(f"source build record {key} must be exactly {want!r}, got {got!r}")
    artifact = raw["artifact"]
    if not isinstance(artifact, dict):
        raise ValueError("source build record artifact must be an object")
    _check_keys("source build record artifact", artifact, _ARTIFACT_KEYS, _ARTIFACT_KEYS)
    _check_sha256("source build record artifact sha256", artifact["sha256"])
    if int(artifact["bytes"]) <= 0:  # type: ignore[arg-type]
        raise ValueError("source build record artifact bytes must be positive")
    expected_machine = ELF_MACHINE.get(expectation.arch)
    if expected_machine is None:
        raise ValueError(f"source build record names no reviewed architecture: {expectation.arch!r}")
    if int(artifact["elf_machine"]) != expected_machine:  # type: ignore[arg-type]
        raise ValueError(
            f"source build record artifact elf_machine must be {expected_machine}"
        )


def emit_official_asset(candidate: Candidate, binary: Path, notices: dict[str, Path]) -> dict:
    """Describe the repackaged official asset, including the measured notices.

    The asset fields repeat the reviewed pin rather than a fresh measurement: the
    download is verified against that pin before this runs, so recording a
    different number here would only add a second, weaker claim. What is measured
    here is the binary and the licence bytes that actually enter the package.
    """
    asset = candidate.official
    _, machine = read_elf(binary)
    if machine != ELF_MACHINE[asset.arch]:
        raise ValueError(
            f"{binary} has ELF machine {machine}, but {asset.arch} requires "
            f"{ELF_MACHINE[asset.arch]}"
        )
    return {
        "schema": OFFICIAL_ASSET_SCHEMA,
        "package": BINARY,
        "version": candidate.package.version,
        "arch": asset.arch,
        "asset": {
            "url": asset.url,
            "sha256": asset.sha256,
            "bytes": asset.size,
            "members": list(asset.members),
        },
        "binary": {
            "name": asset.binary,
            "sha256": sha256_file(binary),
            "bytes": binary.stat().st_size,
            "elf_machine": machine,
        },
        "notices": {name: sha256_file(path) for name, path in sorted(notices.items())},
        "notice": _RECORD_NOTICE,
    }


def check_official_asset(raw: object, candidate: Candidate) -> None:
    """Refuse an official-asset record that disagrees with the reviewed pins."""
    if not isinstance(raw, dict):
        raise ValueError("official asset record must be an object")
    _check_keys("official asset record", raw, _OFFICIAL_KEYS, _OFFICIAL_KEYS)
    asset = candidate.official
    _check_identity_fields(
        raw,
        SourceRecordExpectation(
            identity=candidate_identity(candidate), package=BINARY,
            version=candidate.package.version, arch=asset.arch,
        ),
        OFFICIAL_ASSET_SCHEMA,
    )
    declared = raw["asset"]
    if not isinstance(declared, dict):
        raise ValueError("official asset record asset must be an object")
    _check_keys("official asset record asset", declared, _ASSET_KEYS, _ASSET_KEYS)
    for key, want in (
        ("url", asset.url), ("sha256", asset.sha256), ("bytes", asset.size),
    ):
        got = str(declared[key])
        if got != str(want):
            raise ValueError(f"official asset record {key} must be exactly {want!r}, got {got!r}")
    if [str(member) for member in declared["members"]] != list(asset.members):
        raise ValueError("official asset record members are not the reviewed member set")
    binary = raw["binary"]
    if not isinstance(binary, dict):
        raise ValueError("official asset record binary must be an object")
    _check_keys("official asset record binary", binary, {"name", *_ARTIFACT_KEYS}, {"name", *_ARTIFACT_KEYS})
    if str(binary["name"]) != asset.binary:
        raise ValueError(f"official asset record binary name must be exactly {asset.binary!r}")
    _check_sha256("official asset record binary sha256", binary["sha256"])
    if int(binary["bytes"]) <= 0:  # type: ignore[arg-type]
        raise ValueError("official asset record binary bytes must be positive")
    if int(binary["elf_machine"]) != ELF_MACHINE[asset.arch]:  # type: ignore[arg-type]
        raise ValueError(
            f"official asset record binary elf_machine must be {ELF_MACHINE[asset.arch]}"
        )
    notices = raw["notices"]
    if not isinstance(notices, dict) or set(notices) != _NOTICE_KEYS:
        raise ValueError(f"official asset record notices must cover exactly {sorted(_NOTICE_KEYS)}")
    for name, digest in notices.items():
        _check_sha256(f"official asset record notice {name}", digest)


def _check_identity_fields(raw: dict, expectation: SourceRecordExpectation, schema: str) -> None:
    """Compare the fields every record carries, wherever the caller knows them."""
    if raw["schema"] != schema:
        raise ValueError(f"unsupported record schema: {raw['schema']!r}")
    if expectation.package and raw["package"] != expectation.package:
        raise ValueError(f"record describes a package other than {expectation.package}")
    if expectation.version and raw["version"] != expectation.version:
        raise ValueError(
            f"record version must be exactly {expectation.version!r}, got {raw['version']!r}"
        )
    if expectation.arch and raw["arch"] != expectation.arch:
        raise ValueError(f"record arch must be exactly {expectation.arch!r}, got {raw['arch']!r}")
    if raw["notice"] != _RECORD_NOTICE:
        raise ValueError("record must state that it is an unsigned description of one build")


def _check_sha256(label: str, value: object) -> None:
    digest = str(value).lower()
    if not _SHA256.match(digest):
        raise ValueError(f"{label} needs a full sha256, got {value!r}")


def record_text(document: dict) -> str:
    """Render a record deterministically, so identical facts give identical bytes."""
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def write_record(path: Path, document: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(record_text(document), encoding="utf-8")
    return path


def read_record(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"provenance record not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"provenance record is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("provenance record root must be an object")
    return raw


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.tauri_candidate_provenance",
        description="Emit and check the Tauri candidate's source-build provenance record.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    emit = sub.add_parser("emit-source-build", help="measure a native build and write its record")
    emit.add_argument("--upstream", type=Path, required=True, help="the pinned tauri checkout")
    emit.add_argument("--binary", type=Path, required=True, help="the compiled cargo-tauri")
    emit.add_argument("--rustc", required=True, help="output of `rustc -Vv`")
    emit.add_argument("--cargo", required=True, help="output of `cargo -V`")
    emit.add_argument("--output", type=Path, required=True)
    emit.add_argument("--candidate", type=Path, default=None)
    check = sub.add_parser("check", help="check an installed or built record")
    check.add_argument("--record", type=Path, required=True)
    check.add_argument("--arch", choices=("amd64", "arm64"), required=True)
    check.add_argument("--candidate", type=Path, default=None)
    args = parser.parse_args(argv)
    candidate = load_candidate(args.candidate)
    try:
        if args.command == "emit-source-build":
            document = emit_source_build(
                candidate, args.upstream, args.binary, rustc=args.rustc, cargo=args.cargo,
            )
            write_record(args.output, document)
            print(f"{args.output}: source build record for {document['arch']} written")
            return 0
        raw = read_record(args.record)
        if args.arch == "arm64":
            check_source_build(raw, candidate)
        else:
            check_official_asset(raw, candidate)
    except ValueError as exc:
        print(f"provenance check failed: {exc}", file=sys.stderr)
        return 1
    print(f"{args.record}: {args.arch} provenance record matches the reviewed pins")
    return 0


if __name__ == "__main__":
    sys.exit(main())
