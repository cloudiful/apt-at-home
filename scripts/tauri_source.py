"""Resolve the official source identity of one release and compile it natively.

Two separate jobs, in this order, with nothing shared between them that should not
be shared:

`identity` asks the official API where the reviewed tag's commit is, reads that
commit's lockfile object, and checks that the bytes it received are exactly the
object the commit names — by git blob id, by size, and by SHA-256. It measures;
it does not decide. What it returns is a `SourceIdentity`: the plan a build is
reproducible from, with no artifact block, because nothing has been compiled yet.

`build` is the other half and the only place a compiler runs. It checks the pinned
toolchain identities, proves the checkout is a real git checkout at the pinned
commit reading the pinned lockfile object, runs the reviewed `--locked` command,
and then measures the finished binary: its ELF architecture, the libraries and
symbol versions it needs against the reviewed runtime mapping, and the two bounded
native probes. Only then does it write an artifact directory — the binary, the
provenance record that states what was built, and the reviewed licence notices —
for a consumer to re-verify without executing any of it.

`checkout` is the step between a producer's plan and that build. It fetches the
reviewed tag shallowly and checks out the pinned commit by name, so a tag that
moved lands somewhere the build's own checkout check refuses, and it never takes a
repository or a commit from a workflow input.

Neither subcommand is reachable from the publishing build path, and neither one
widens a reviewed gate to make a payload fit: an unknown library, a higher symbol
floor, a different compiler, a moved tag, or a changed lockfile stops the run.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .bun_candidate_smoke import check_elf_arch, native_arch
from .common import sha256_bytes
from .source_artifacts import NOTICE_FILES, reviewed_runtime
from .source_build import (
    SCHEMA as IDENTITY_SCHEMA,
    BuildRecipe,
    Lockfile,
    SourceIdentity,
    Toolchain,
    check_source_target,
    check_tag_version,
    document,
    parse_identity,
)
from .tauri_candidate_metadata import (
    BINARY,
    REVIEWED_LOCK_PATH,
    REVIEWED_MANIFEST,
    REVIEWED_PROFILE,
    REVIEWED_REPOSITORY,
    REVIEWED_TARGET,
    REVIEWED_TOOLCHAIN,
    release_tag,
)
from .tauri_candidate_provenance import (
    SOURCE_BUILD_NAME,
    SourceRecordExpectation,
    check_checkout,
    check_tool_identity,
    emit_source_build,
    write_record,
)
from .tauri_candidate_smoke import RuntimePolicy, check_runtime, native_checks
from .upstream import HttpClient, client_from_env
from .upstream_assets import (
    contents_api_url,
    git_ref_api_url,
    git_tag_api_url,
    repo_slug,
    tags_api_url,
    version_key,
)

# The reviewed compile policy. These are the same pins the candidate descriptor
# froze, read from there rather than restated here, so a policy change cannot
# reach one path without reaching the other.
DEFAULT_PACKAGE = BINARY
DEFAULT_ARCH = "arm64"

# Upstream's own tag naming for this one command, shared with `release_tag`.
TAG_PREFIX = "tauri-cli-v"
# Latest stable only: an exact N.N.N after the prefix, with no prerelease part.
_STABLE_VERSION = re.compile(r"^\d+\.\d+\.\d+$")

# Native CI compiles under this bound; a build that exceeds it failed, not slow.
BUILD_TIMEOUT_SECONDS = 45 * 60
# The reviewed lockfile is a few hundred kilobytes; the ceiling is a ceiling.
MAX_LOCK_BYTES = 8 * 1024 * 1024


def source_policy(
    *,
    package: str = DEFAULT_PACKAGE,
    toolchain: str = REVIEWED_TOOLCHAIN,
    manifest: str = REVIEWED_MANIFEST,
    lock_path: str = REVIEWED_LOCK_PATH,
    target: str = REVIEWED_TARGET,
    profile: str = REVIEWED_PROFILE,
) -> BuildRecipe:
    """Return the reviewed command this repository builds the package with."""
    return BuildRecipe(
        manifest=manifest,
        command=(
            "cargo", "build", "--locked",
            "--manifest-path", manifest,
            "--target", target,
            "--profile", profile,
            "--bin", package,
        ),
        target=target,
        profile=profile,
        binary=package,
    )


def git_blob_id(data: bytes) -> str:
    """Return the git blob id of exact bytes: SHA-1 over the git object header."""
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data).hexdigest()  # noqa: S324 - git defines this id as SHA-1


@dataclass(frozen=True)
class LockObject:
    """The lockfile as the official API describes it, and as the bytes hash."""

    data: bytes
    git_blob: str
    size: int

    @property
    def sha256(self) -> str:
        return sha256_bytes(self.data)


def read_lock_object(client: HttpClient, repo: str, commit: str, path: str) -> LockObject:
    """Read one commit's lockfile and prove the bytes are the object it names.

    The API reports the blob id and size of the path at that commit; both are
    recomputed from the bytes that actually arrived, so a substituted or truncated
    response is refused instead of being measured into the record.
    """
    payload = client.get_json(contents_api_url(repo, path, commit))
    if not isinstance(payload, dict):
        raise ValueError("lockfile contents response is not an object")
    if payload.get("type") != "file":
        raise ValueError(f"{path} at {commit} is not a file in that commit")
    declared_blob = str(payload.get("sha", "")).lower()
    if len(declared_blob) != 40 or any(char not in "0123456789abcdef" for char in declared_blob):
        raise ValueError(f"{path} contents carry no usable git object id")
    declared_size = payload.get("size")
    if not isinstance(declared_size, int) or isinstance(declared_size, bool):
        raise ValueError(f"{path} contents carry no usable size")
    if payload.get("encoding") != "base64":
        raise ValueError(f"{path} contents are not base64 encoded and cannot be verified")
    content = payload.get("content")
    if not isinstance(content, str) or not content:
        raise ValueError(f"{path} contents carry no data")
    if declared_size > MAX_LOCK_BYTES:
        raise ValueError(f"{path} is {declared_size} bytes, over the {MAX_LOCK_BYTES} byte limit")
    try:
        data = base64.b64decode("".join(content.split()), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"{path} contents are not valid base64: {exc}") from None
    if len(data) != declared_size:
        raise ValueError(
            f"{path} arrived as {len(data)} bytes, but the API declares {declared_size}"
        )
    measured = git_blob_id(data)
    if measured != declared_blob:
        raise ValueError(
            f"{path} hashes to git object {measured}, but the commit declares "
            f"{declared_blob}; the build would not read the reviewed lockfile"
        )
    return LockObject(data=data, git_blob=measured, size=len(data))


def resolve_tag_commit(client: HttpClient, repo: str, tag: str) -> str:
    """Return the commit an official tag names, following one annotated tag.

    GitHub answers a tag reference with either the commit itself or an annotated
    tag object that points at one. Both are resolved, because a lightweight and
    an annotated tag are the same release and neither may be accepted as "close
    enough" while the other is refused.
    """
    payload = client.get_json(git_ref_api_url(repo, tag))
    if not isinstance(payload, dict):
        raise ValueError(f"tag {tag} resolved to no object")
    reference = payload.get("object")
    if not isinstance(reference, dict):
        raise ValueError(f"tag {tag} carries no object")
    kind = reference.get("type")
    sha = str(reference.get("sha", "")).lower()
    if len(sha) != 40 or any(char not in "0123456789abcdef" for char in sha):
        raise ValueError(f"tag {tag} resolves to no full git object id")
    if kind == "commit":
        return sha
    if kind != "tag":
        raise ValueError(f"tag {tag} resolves to an unsupported object type: {kind!r}")
    annotated = client.get_json(git_tag_api_url(repo, sha))
    if not isinstance(annotated, dict):
        raise ValueError(f"annotated tag {tag} resolved to no object")
    inner = annotated.get("object")
    if not isinstance(inner, dict) or inner.get("type") != "commit":
        raise ValueError(f"annotated tag {tag} does not point at a commit")
    commit = str(inner.get("sha", "")).lower()
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        raise ValueError(f"annotated tag {tag} names no full commit id")
    return commit


def stable_version(tag: object) -> str | None:
    """Return the version a reviewed tag names, or None when it names none.

    The Tauri repository's release tags cover the whole desktop application, so
    discovery has to keep only the exact `tauri-cli-vN.N.N` form. Anything else —
    another product line, a prerelease, a build suffix — is not a candidate.
    """
    if not isinstance(tag, str) or not tag.startswith(TAG_PREFIX):
        return None
    version = tag.removeprefix(TAG_PREFIX)
    return version if _STABLE_VERSION.match(version) else None


def latest_stable_version(
    client: HttpClient, *, repository: str = REVIEWED_REPOSITORY
) -> str:
    """Return the highest reviewed stable version upstream currently tags.

    One bounded request lists the repository's tags; the newest matching one is
    selected by numeric version rather than by list order, and an empty or
    unrecognised listing is a refusal instead of a fallback to a pinned version.
    """
    repo = repo_slug(repository)
    payload = client.get_json(tags_api_url(repo))
    if not isinstance(payload, list):
        raise ValueError("GitHub tag listing for the reviewed repository is not a list")
    versions: set[str] = set()
    for entry in payload:
        if not isinstance(entry, dict):
            raise ValueError("GitHub tag listing entry is not an object")
        version = stable_version(entry.get("name"))
        if version is not None:
            versions.add(version)
    if not versions:
        raise ValueError(
            f"{repository} tags no {TAG_PREFIX}N.N.N release the producer could plan"
        )
    return max(versions, key=version_key)


def discover_identity(
    client: HttpClient,
    version: str,
    *,
    package: str = DEFAULT_PACKAGE,
    arch: str = DEFAULT_ARCH,
    repository: str = REVIEWED_REPOSITORY,
    toolchain: str = REVIEWED_TOOLCHAIN,
) -> SourceIdentity:
    """Resolve one release's source identity from official metadata alone."""
    check_source_target(package, arch)
    if not version:
        raise ValueError("a source identity needs the version it describes")
    tag = release_tag(version)
    repo = repo_slug(repository)
    commit = resolve_tag_commit(client, repo, tag)
    build = source_policy(package=package, toolchain=toolchain)
    lock = read_lock_object(client, repo, commit, REVIEWED_LOCK_PATH)
    return SourceIdentity(
        repository=repository,
        tag=tag,
        commit=commit,
        lockfile=Lockfile(
            path=REVIEWED_LOCK_PATH, git_blob=lock.git_blob, bytes=lock.size, sha256=lock.sha256
        ),
        toolchain=Toolchain(channel=toolchain),
        build=build,
    )


def native_binary(upstream: Path, identity: SourceIdentity) -> Path:
    """Return where cargo places the compiled command for this reviewed profile."""
    build = identity.build
    return upstream / "target" / build.target / build.profile / build.binary


Runner = Callable[[list[str], Path], "subprocess.CompletedProcess[str]"]


def _run(argv: list[str], cwd: Path) -> "subprocess.CompletedProcess[str]":
    """Run one reviewed command in the pinned checkout, under a wall-clock bound."""
    return subprocess.run(
        argv,
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=BUILD_TIMEOUT_SECONDS,
    )


def _tool_identity(runner: Runner, tool: str, upstream: Path, channel: str) -> str:
    completed = runner([tool, "-Vv" if tool == "rustc" else "-V"], upstream)
    if completed.returncode != 0:
        raise ValueError(
            f"{tool} is not usable in this environment: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    return check_tool_identity(tool, completed.stdout, channel)


def build_native(
    *,
    identity: SourceIdentity,
    package: str,
    version: str,
    upstream: Path,
    output: Path,
    notices: Mapping[str, Path],
    arch: str = DEFAULT_ARCH,
    runtime: RuntimePolicy | None = None,
    require_native: bool = True,
    runner: Runner | None = None,
) -> list[Path]:
    """Compile one reviewed source identity and write its artifact directory.

    Every check runs before the record is written, and the record is the last
    thing produced: a build that fails any of them leaves an artifact directory
    that a consumer will refuse rather than one that vouches for a payload it did
    not check.
    """
    check_source_target(package, arch)
    check_tag_version(version, identity.tag, prefix=TAG_PREFIX)
    run = runner if runner is not None else _run
    channel = identity.toolchain.channel
    rustc = _tool_identity(run, "rustc", upstream, channel)
    cargo_identity = _tool_identity(run, "cargo", upstream, channel)
    check_checkout(upstream, identity)

    completed = run(list(identity.build.command), upstream)
    if completed.returncode != 0:
        raise ValueError(
            f"{' '.join(identity.build.command)} exited {completed.returncode}: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )

    binary = native_binary(upstream, identity)
    if not binary.is_file() or binary.is_symlink():
        raise ValueError(f"the reviewed command produced no binary at {binary}")
    check_elf_arch(binary, arch)
    policy: RuntimePolicy = runtime if runtime is not None else reviewed_runtime(package)
    check_runtime(binary, arch, policy)

    if require_native and native_arch() != arch:
        raise ValueError(
            f"the native build of {package} {arch} needs an {arch} machine to run its "
            f"probes; this machine is {native_arch()}"
        )
    if native_arch() == arch:
        with tempfile.TemporaryDirectory(prefix="apt-tauri-source-") as private:
            for line in native_checks(
                binary, version, Path(private), shutil.which("cargo"), require_cargo=True,
            ):
                print(line)
    else:
        print(f"native probes skipped: this machine is {native_arch()}, not {arch}")

    expectation = SourceRecordExpectation(
        identity=identity, package=package, version=version, arch=arch
    )
    record = emit_source_build(
        expectation, upstream, binary, rustc=rustc, cargo=cargo_identity
    )

    output.mkdir(parents=True, exist_ok=True)
    staged = output / identity.build.binary
    shutil.copyfile(binary, staged)
    staged.chmod(0o755)
    written = [staged, write_record(output / SOURCE_BUILD_NAME, record)]
    missing = sorted(name for name in NOTICE_FILES if name not in notices)
    if missing:
        raise ValueError(f"the reviewed licence notices are missing: {', '.join(missing)}")
    for name in NOTICE_FILES:
        source = notices[name]
        destination = output / name
        shutil.copyfile(source, destination)
        written.append(destination)
    return written


def read_identity(path: Path) -> SourceIdentity:
    """Read a planned identity document written by the `identity` subcommand."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"source identity not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"source identity is not valid JSON: {exc}") from exc
    return parse_identity(raw)


def resolve_plan(path: Path) -> tuple[SourceIdentity, str | None]:
    """Read the identity a producer's plan pins, with the version it states.

    A producer hands its plan file to the steps that follow it, so those steps
    accept either shape rather than making the workflow extract and rewrite one.
    The version is the plan's own when it has one; a bare identity document states
    none, and its tag is the only place the version appears. The plan's schema
    belongs to the planner, which builds on this module, so the import is local:
    the cycle would only be a problem between two module headers.
    """
    from .source_plan import read_plan

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"source identity not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"source identity is not valid JSON: {exc}") from exc
    if isinstance(raw, dict) and raw.get("schema") == IDENTITY_SCHEMA:
        return parse_identity(raw), None
    decided = read_plan(path)
    if decided.source is None:
        raise ValueError(f"{path} declares no source target to build")
    return decided.source, decided.desired_version


def resolve_plan_identity(path: Path) -> SourceIdentity:
    """Return the identity a producer's plan or identity document pins."""
    return resolve_plan(path)[0]


def planned_version(path: Path, identity: SourceIdentity) -> str:
    """Return the version a plan states, or the one the pinned tag names."""
    stated = resolve_plan(path)[1]
    if stated is not None:
        return stated
    version = stable_version(identity.tag)
    if version is None:
        raise ValueError(f"{identity.tag} names no reviewed stable version")
    return version


def _git(run: Runner, argv: list[str], cwd: Path) -> str:
    completed = run(["git", *argv], cwd)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ValueError(f"git {' '.join(argv)} failed: {detail}")
    return completed.stdout.strip()


def check_out(
    identity: SourceIdentity, destination: Path, *, runner: Runner | None = None
) -> Path:
    """Fetch the reviewed tag shallowly and check out exactly the commit it pins.

    The commit is checked out by name rather than by fetching it directly, so a
    tag that moved cannot silently supply a different tree: the checkout lands
    wherever the tag now points, and `check_checkout` then refuses it against the
    pinned commit. The repository URL and tag come from the validated identity,
    never from a workflow input.
    """
    run = runner if runner is not None else _run
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError(f"{destination} is not an empty directory to check out into")
    destination.mkdir(parents=True, exist_ok=True)
    _git(run, ["init", "--quiet"], destination)
    _git(run, ["remote", "add", "origin", identity.repository], destination)
    _git(run, ["fetch", "--quiet", "--depth", "1", "--no-tags", "origin",
               f"+refs/tags/{identity.tag}:refs/tags/{identity.tag}"], destination)
    _git(run, ["checkout", "--quiet", "--detach", identity.commit], destination)
    check_checkout(destination, identity)
    return destination


def write_identity(path: Path, identity: SourceIdentity) -> Path:
    """Write the plan in its manifest form, so the same bytes can be committed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document(identity), indent=2) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.tauri_source",
        description="Resolve a reviewed source identity and compile it on this machine.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    identity = sub.add_parser("identity", help="resolve the official tag, commit, and lockfile")
    identity.add_argument("--version", required=True)
    identity.add_argument("--output", type=Path, default=None)
    identity.add_argument("--package", default=DEFAULT_PACKAGE)
    identity.add_argument("--arch", default=DEFAULT_ARCH)

    build = sub.add_parser("build", help="compile a resolved identity and write its artifacts")
    build.add_argument("--identity", type=Path, required=True,
                       help="a producer source plan or an identity document")
    build.add_argument("--version", default=None,
                       help="defaults to the version the plan states or the tag names")
    build.add_argument("--upstream", type=Path, required=True)
    build.add_argument("--notices", type=Path, required=True,
                       help="directory holding the reviewed licence notices")
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--package", default=DEFAULT_PACKAGE)
    build.add_argument("--arch", default=DEFAULT_ARCH)
    build.add_argument("--allow-non-native", action="store_true",
                       help="skip the native probes instead of refusing a foreign host")

    checkout = sub.add_parser(
        "checkout", help="fetch the reviewed tag and check out exactly the commit it pins"
    )
    checkout.add_argument("--identity", type=Path, required=True,
                          help="a producer source plan or an identity document")
    checkout.add_argument("--into", type=Path, required=True,
                          help="empty directory to check the reviewed commit out into")
    checkout.add_argument("--package", default=DEFAULT_PACKAGE)
    checkout.add_argument("--arch", default=DEFAULT_ARCH)
    args = parser.parse_args(argv)
    try:
        if args.command == "identity":
            resolved = discover_identity(
                client_from_env(), args.version, package=args.package, arch=args.arch
            )
            if args.output is None:
                print(json.dumps(document(resolved), indent=2))
                return 0
            write_identity(args.output, resolved)
            print(
                f"{args.output}: source identity for {args.package} {args.version} "
                f"at {resolved.commit[:12]}"
            )
            return 0
        identity = resolve_plan_identity(args.identity)
        check_source_target(args.package, args.arch)
        if args.command == "checkout":
            check_out(identity, args.into)
            print(f"{args.into}: {identity.repository} at {identity.commit[:12]}")
            return 0
        version = args.version or planned_version(args.identity, identity)
        notices = {name: args.notices / name for name in NOTICE_FILES}
        for path in build_native(
            identity=identity,
            package=args.package,
            version=version,
            upstream=args.upstream,
            output=args.output,
            notices=notices,
            arch=args.arch,
            require_native=not args.allow_non_native,
        ):
            print(path)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"source build failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())