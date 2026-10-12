"""Discovery measures; the native build refuses anything it cannot prove.

`identity` resolves the official tag to a commit and reads the lockfile object
that commit names, recomputing its git blob id and SHA-256 from the bytes that
arrived. `build` runs the reviewed `--locked` command in a checkout it has just
proved is the pinned commit, then measures the result and writes the artifact
directory a consumer will re-verify.

No compiler runs here. The build command is injected, so what these tests prove
is every refusal *around* it — a moved tag, a substituted lockfile, a different
toolchain, a foreign checkout, a payload above the reviewed ABI floor, a
non-native host — plus the round trip that the directory the producer writes is
the directory the consumer accepts.
"""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import tauri_candidate_provenance as prov
from scripts.source_artifacts import NOTICE_FILES, load_source_artifacts
from scripts.source_build import parse_identity
from scripts.tauri_source import (
    MAX_LOCK_BYTES,
    build_native,
    discover_identity,
    git_blob_id,
    native_binary,
    read_identity,
    read_lock_object,
    resolve_tag_commit,
    source_policy,
    write_identity,
)
from scripts.upstream_assets import repo_slug
from tests.test_source_artifacts import (
    LOCK,
    NOTICES,
    arm64_binary,
    pinned,
    source_identity,
)
from tests.test_tauri_candidate_provenance import CARGO_LINE, RUSTC_LINE

VERSION = "2.12.2"
COMMIT = "5b23c82ea5ba6b9aec420dd71f60493edc486aa9"
TAG_OBJECT = "1" * 40
REPO = "tauri-apps/tauri"


class FakeClient:
    """A scripted API client: one payload per endpoint, never the network."""

    def __init__(self, payloads: dict[str, object]) -> None:
        self.payloads = payloads
        self.requested: list[str] = []

    def get_json(self, url: str) -> object:
        self.requested.append(url)
        for fragment, payload in self.payloads.items():
            if fragment in url:
                return payload
        raise AssertionError(f"unexpected request: {url}")

    def get_bytes(self, url: str, *, max_bytes: int) -> bytes:  # pragma: no cover - unused
        raise AssertionError("source identity discovery reads JSON only")


def lock_payload(data: bytes = LOCK, *, blob: str | None = None, size: int | None = None,
                 encoding: str = "base64", kind: str = "file") -> dict:
    return {
        "type": kind,
        "sha": blob if blob is not None else git_blob_id(data),
        "size": len(data) if size is None else size,
        "encoding": encoding,
        "content": base64.b64encode(data).decode("ascii"),
    }


def commit_ref(sha: str = COMMIT, kind: str = "commit") -> dict:
    return {"object": {"type": kind, "sha": sha}}


class ResolveTagCommitTest(unittest.TestCase):
    def test_a_lightweight_tag_resolves_to_its_commit(self) -> None:
        client = FakeClient({"/git/ref/tags/": commit_ref()})
        self.assertEqual(resolve_tag_commit(client, REPO, f"tauri-cli-v{VERSION}"), COMMIT)
        self.assertEqual(client.requested, [f"https://api.github.com/repos/{REPO}/git/ref/tags/tauri-cli-v{VERSION}"])

    def test_an_annotated_tag_is_followed_to_its_commit(self) -> None:
        client = FakeClient(
            {
                "/git/ref/tags/": commit_ref(TAG_OBJECT, kind="tag"),
                "/git/tags/": {"object": {"type": "commit", "sha": COMMIT}},
            }
        )
        self.assertEqual(resolve_tag_commit(client, REPO, "tauri-cli-v2.12.2"), COMMIT)

    def test_a_reference_that_is_not_a_commit_is_refused(self) -> None:
        cases = (
            (FakeClient({"/git/ref/tags/": commit_ref(kind="blob")}), "unsupported object type"),
            (FakeClient({"/git/ref/tags/": {"object": {"type": "commit", "sha": "abc"}}}), "no full git object id"),
            (FakeClient({"/git/ref/tags/": {"nope": True}}), "carries no object"),
            (
                FakeClient({"/git/ref/tags/": commit_ref(TAG_OBJECT, kind="tag"), "/git/tags/": {"object": {}}}),
                "does not point at a commit",
            ),
        )
        for client, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    resolve_tag_commit(client, REPO, "tauri-cli-v2.12.2")

    def test_a_repository_url_must_be_an_official_one(self) -> None:
        self.assertEqual(repo_slug("https://github.com/tauri-apps/tauri"), REPO)
        for url in ("https://gitlab.com/a/b", "https://github.com/a", "git@github.com:a/b", ""):
            with self.subTest(url=url):
                with self.assertRaisesRegex(ValueError, "not an official GitHub repository"):
                    repo_slug(url)


class ReadLockObjectTest(unittest.TestCase):
    def test_the_bytes_are_bound_to_the_object_the_commit_names(self) -> None:
        lock = read_lock_object(FakeClient({"/contents/": lock_payload()}), REPO, COMMIT, "Cargo.lock")
        self.assertEqual(lock.git_blob, git_blob_id(LOCK))
        self.assertEqual(lock.size, len(LOCK))
        self.assertEqual(lock.sha256, hashlib.sha256(LOCK).hexdigest())

    def test_a_substituted_or_truncated_lockfile_is_refused(self) -> None:
        cases = (
            lock_payload(blob="a" * 40),
            lock_payload(size=len(LOCK) + 1),
            lock_payload(encoding="none"),
            lock_payload(kind="dir"),
            lock_payload(size=MAX_LOCK_BYTES + 1),
        )
        for payload in cases:
            with self.subTest(payload=payload["size"]):
                with self.assertRaises(ValueError):
                    read_lock_object(FakeClient({"/contents/": payload}), REPO, COMMIT, "Cargo.lock")

    def test_an_unusable_object_id_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "no usable git object id"):
            read_lock_object(
                FakeClient({"/contents/": lock_payload(blob="nope")}), REPO, COMMIT, "Cargo.lock"
            )

    def test_the_git_blob_id_is_gits_own_definition(self) -> None:
        body = b"a fixture lockfile\n"
        expected = hashlib.sha1(b"blob " + str(len(body)).encode() + b"\0" + body).hexdigest()
        self.assertEqual(git_blob_id(body), expected)


class DiscoverIdentityTest(unittest.TestCase):
    def test_the_identity_names_the_commit_and_its_lockfile(self) -> None:
        client = FakeClient({"/git/ref/tags/": commit_ref(), "/contents/": lock_payload()})
        identity = discover_identity(client, VERSION)
        self.assertEqual(identity.tag, f"tauri-cli-v{VERSION}")
        self.assertEqual(identity.commit, COMMIT)
        self.assertEqual(identity.lockfile.git_blob, git_blob_id(LOCK))
        self.assertEqual(identity.lockfile.bytes, len(LOCK))
        self.assertEqual(identity.toolchain.channel, "1.99.0")
        self.assertEqual(identity.build, source_policy())
        # The plan carries no artifact block: nothing has been compiled yet.
        self.assertNotIn("artifact", identity.document())

    def test_only_the_reviewed_source_target_may_be_resolved(self) -> None:
        client = FakeClient({"/git/ref/tags/": commit_ref(), "/contents/": lock_payload()})
        with self.assertRaisesRegex(ValueError, "may not be built from source"):
            discover_identity(client, VERSION, arch="amd64")
        with self.assertRaisesRegex(ValueError, "may not be built from source"):
            discover_identity(client, VERSION, package="zellij")
        with self.assertRaisesRegex(ValueError, "needs the version"):
            discover_identity(client, "")

    def test_an_identity_document_round_trips(self) -> None:
        identity = discover_identity(
            FakeClient({"/git/ref/tags/": commit_ref(), "/contents/": lock_payload()}), VERSION
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = write_identity(Path(tmp) / "identity.json", identity)
            self.assertEqual(read_identity(path), identity)
            written = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(parse_identity(written), identity)
            path.write_text("{ not json", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not valid JSON"):
                read_identity(path)
            with self.assertRaisesRegex(ValueError, "not found"):
                read_identity(Path(tmp) / "absent.json")


class _Runner:
    """A scripted stand-in for the compiler: the only part of `build` not run."""

    def __init__(self, binary: Path, *, rustc: str = RUSTC_LINE, cargo: str = CARGO_LINE,
                 build_code: int = 0, produce: bool = True) -> None:
        self.binary = binary
        self.rustc = rustc
        self.cargo = cargo
        self.build_code = build_code
        self.produce = produce
        self.commands: list[list[str]] = []

    def __call__(self, argv: list[str], cwd: Path) -> "subprocess.CompletedProcess[str]":
        self.commands.append(list(argv))
        if argv[0] == "rustc":
            return subprocess.CompletedProcess(argv, 0, self.rustc, "")
        if argv[0] == "cargo" and argv[1] != "build":
            return subprocess.CompletedProcess(argv, 0, self.cargo, "")
        if self.build_code:
            return subprocess.CompletedProcess(argv, self.build_code, "", "compiler failed")
        if self.produce:
            self.binary.parent.mkdir(parents=True, exist_ok=True)
            arm64_binary(self.binary)
        return subprocess.CompletedProcess(argv, 0, "compiled", "")


class BuildNativeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.identity = source_identity(
            tag=f"tauri-cli-v{VERSION}",
            commit=COMMIT,
        )
        self.upstream = self.root / "checkout"
        (self.upstream).mkdir()
        (self.upstream / "Cargo.lock").write_bytes(LOCK)
        self.notices = self.root / "notices"
        self.notices.mkdir()
        for name, body in NOTICES.items():
            (self.notices / name).write_text(body, encoding="utf-8")
        self.output = self.root / "out"
        self.binary = native_binary(self.upstream, self.identity)

    def build(self, runner: _Runner, **kwargs):
        with mock.patch.object(prov, "_git_output", side_effect=self.git_answers()):
            return build_native(
                identity=kwargs.pop("identity", self.identity),
                package="cargo-tauri",
                version=VERSION,
                upstream=self.upstream,
                output=self.output,
                notices={name: self.notices / name for name in NOTICE_FILES},
                require_native=False,
                runner=runner,
                **kwargs,
            )

    def git_answers(self):
        def answer(_upstream: Path, *arguments: str) -> str:
            if arguments[:2] == ("rev-parse", "HEAD"):
                return self.identity.commit
            return self.identity.lockfile.git_blob
        return answer

    def test_the_build_runs_the_reviewed_command_and_writes_its_artifacts(self) -> None:
        runner = _Runner(self.binary)
        written = self.build(runner)
        self.assertEqual(runner.commands[2], list(self.identity.build.command))
        self.assertEqual(
            sorted(path.name for path in written),
            sorted([*NOTICE_FILES, "cargo-tauri", prov.SOURCE_BUILD_NAME]),
        )
        # The directory the producer writes is the one the consumer accepts.
        artifacts = load_source_artifacts(self.output, expected=pinned(self.identity))
        self.assertEqual(artifacts.identity, self.identity)
        self.assertEqual(artifacts.license_bodies(), tuple(NOTICES[n] for n in NOTICE_FILES))

    def test_the_compiler_must_be_the_pinned_toolchain(self) -> None:
        runner = _Runner(self.binary, rustc="rustc 1.98.0 (deadbeef 2025-01-01)")
        with self.assertRaisesRegex(ValueError, "does not match the pinned 1.99.0 toolchain"):
            self.build(runner)

    def test_a_missing_compiler_is_refused(self) -> None:
        def runner(argv: list[str], cwd: Path):
            return subprocess.CompletedProcess(argv, 127, "", "not found")

        with self.assertRaisesRegex(ValueError, "rustc is not usable"):
            self.build(runner)  # type: ignore[arg-type]

    def test_a_failed_compilation_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "compiler failed"):
            self.build(_Runner(self.binary, build_code=101))

    def test_a_checkout_that_is_not_the_pinned_commit_is_refused(self) -> None:
        runner = _Runner(self.binary)
        with mock.patch.object(
            prov, "_git_output", side_effect=lambda *_: "a" * 40
        ):
            with self.assertRaisesRegex(ValueError, "pins"):
                build_native(
                    identity=self.identity, package="cargo-tauri", version=VERSION,
                    upstream=self.upstream, output=self.output,
                    notices={n: self.notices / n for n in NOTICE_FILES},
                    require_native=False, runner=runner,
                )
        self.assertFalse(self.output.exists())

    def test_a_command_that_produces_nothing_is_refused(self) -> None:
        runner = _Runner(self.binary, produce=False)
        with self.assertRaisesRegex(ValueError, "produced no binary"):
            self.build(runner)
        self.assertFalse(self.output.exists())

    def test_a_foreign_host_may_not_claim_native_evidence(self) -> None:
        runner = _Runner(self.binary)
        with mock.patch("scripts.tauri_source.native_arch", return_value="amd64"):
            with mock.patch.object(prov, "_git_output", side_effect=self.git_answers()):
                with self.assertRaisesRegex(ValueError, "needs an arm64 machine"):
                    build_native(
                        identity=self.identity, package="cargo-tauri", version=VERSION,
                        upstream=self.upstream, output=self.output,
                        notices={n: self.notices / n for n in NOTICE_FILES},
                        require_native=True, runner=runner,
                    )

    def test_the_tag_must_name_the_version_being_built(self) -> None:
        with self.assertRaisesRegex(ValueError, "is not the canonical"):
            self.build(_Runner(self.binary), identity=source_identity(commit=COMMIT, tag="tauri-cli-v2.12.1"))

    def test_a_missing_reviewed_notice_stops_the_artifact(self) -> None:
        with mock.patch.object(prov, "_git_output", side_effect=self.git_answers()):
            with self.assertRaisesRegex(ValueError, "reviewed licence notices are missing"):
                build_native(
                    identity=self.identity, package="cargo-tauri", version=VERSION,
                    upstream=self.upstream, output=self.output, notices={},
                    require_native=False, runner=_Runner(self.binary),
                )

    def test_only_the_reviewed_source_target_may_be_built(self) -> None:
        with self.assertRaisesRegex(ValueError, "may not be built from source"):
            self.build(_Runner(self.binary), arch="amd64")


if __name__ == "__main__":
    unittest.main()