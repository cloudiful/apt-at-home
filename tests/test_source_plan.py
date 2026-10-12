"""A producer decides from public metadata only, and refuses everything else.

The producer holds no secret, so everything it trusts has to be something the
anonymous gateway serves and the pinned key vouches for: `key.asc`, the
clearsigned and detached-signed `Release`, and the signed `Packages` index. These
tests build that world for real — a throwaway GPG key signs a genuine release —
and then take it apart: a rotated pin, a tampered index, a broken signature, a
missing object. The pointer and the bound manifest are absent from the double on
purpose, so a plan that reached for either fails here rather than in production.

The two modes are pinned against each other. `upstream` skips the compile when
the published version is already the desired one; `manifest` always needs one,
because the committed document never carries a measured artifact and CI builds
from that document.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from scripts import tauri_candidate_provenance as prov
from scripts.fingerprint import primary_fingerprint_of_keyfile
from scripts.metadata import ARCHES, COMPONENT, SUITE, generate_release, write_gzip_deterministic
from scripts.release_verify import BaselineUnavailable
from scripts.sign import export_public_key, sign_release
from scripts.source_plan import (
    MODES,
    SCHEMA,
    SourcePlan,
    check_signed_index,
    main,
    normalize_pin,
    parse_plan,
    plan,
    public_baseline,
    published_in_plan,
    read_plan,
    write_plan,
)
from scripts.source_build import PLAN_NAME
from scripts.storage import ObjectNotFound, PublicHttpsSource
from scripts.tauri_source import (
    check_out,
    latest_stable_version,
    planned_version,
    resolve_plan,
    resolve_plan_identity,
    stable_version,
    write_identity,
)
from scripts.manifest import default_manifest_path
from scripts.release_manifest import load_manifest_data
from tests.helpers import make_gpg_home_with_key
from tests.test_source_artifacts import LOCK, _git_answers, source_identity
from tests.test_source_build import source_manifest_document
from tests.test_tauri_source import COMMIT, VERSION, FakeClient, commit_ref, lock_payload

PACKAGE = "cargo-tauri"
PIN = "94D49782052D6521E0249E4178C3D292443558E2"
RELEASE_ID = "20260102-030405"


class PublicGateway:
    """The anonymous surface, and nothing else.

    `key.asc`, the distribution tree, and the pool. A read of the pointer or the
    bound manifest raises, because those are private routes the gateway does not
    expose and a credential-free producer cannot reach them at all.
    """

    PRIVATE = ("current.json", "manifest.json")

    def __init__(self, objects: dict[str, bytes]) -> None:
        self.reads: list[str] = []
        self.objects = dict(objects)

    def get(self, key: str, *, max_bytes: int) -> bytes:
        self.reads.append(key)
        if any(key.endswith(private) for private in self.PRIVATE):
            raise ObjectNotFound(key)
        payload = self.objects.get(key)
        if payload is None:
            raise ObjectNotFound(key)
        if len(payload) > max_bytes:
            raise ValueError(f"object {key} exceeded the {max_bytes} byte limit")
        return payload


def published_repository(
    root: Path, key_home: Path, key_id: str, *, version: str | None = None
) -> dict[str, bytes]:
    """Sign a real release and return the objects the public gateway would serve.

    `version` is the source target's published version: the arm64 index then
    carries that stanza, which is the one difference between "already published"
    and "not published yet" a plan can legitimately see.
    """
    base = root / "releases" / RELEASE_ID
    dists = base / "dists" / SUITE
    inputs: list[tuple[Path, str]] = []
    for arch in ARCHES:
        directory = dists / COMPONENT / f"binary-{arch}"
        directory.mkdir(parents=True, exist_ok=True)
        stanza = (
            f"Package: zellij\nVersion: 0.45.1\nArchitecture: {arch}\n"
            "Filename: pool/zellij_0.45.1-1_amd64.deb\n"
        )
        if arch == "arm64" and version is not None:
            stanza += (
                f"Package: {PACKAGE}\nVersion: {version}\nArchitecture: arm64\n"
                f"Filename: pool/{PACKAGE}_{version}-1_arm64.deb\n"
            )
        packages = directory / "Packages"
        packages.write_text(stanza, encoding="utf-8")
        compressed = directory / "Packages.gz"
        write_gzip_deterministic(packages, compressed)
        inputs.append((packages, f"{COMPONENT}/binary-{arch}/Packages"))
        inputs.append((compressed, f"{COMPONENT}/binary-{arch}/Packages.gz"))
    release = generate_release(output_root=root, release_id=RELEASE_ID, files=inputs)
    previous = os.environ.get("GNUPGHOME")
    os.environ["GNUPGHOME"] = str(key_home)
    try:
        sign_release(release_file=release, key_id=key_id)
        export_public_key(homedir=key_home, key_id=key_id, dest=root / "key.asc")
    finally:
        if previous is None:
            os.environ.pop("GNUPGHOME", None)
        else:
            os.environ["GNUPGHOME"] = previous
    # The gateway serves the published release without its storage path, which is
    # how `dists/stable/...` becomes a URL a producer can read anonymously.
    served = {"key.asc": (root / "key.asc").read_bytes()}
    for path in sorted(base.rglob("*")):
        if path.is_file():
            served[str(path.relative_to(root)).removeprefix(f"releases/{RELEASE_ID}/")] = (
                path.read_bytes()
            )
    return served


class SignedRepositoryTest(unittest.TestCase):
    """One real signed repository, shared by every case that does not tamper."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._key_tmp, cls._home, cls._key = make_gpg_home_with_key()
        cls._root = tempfile.TemporaryDirectory()
        root = Path(cls._root.name)
        unpublished = published_repository(root, cls._home, cls._key)
        later = published_repository(root / "later", cls._home, cls._key, version=VERSION)
        cls.fingerprint = primary_fingerprint_of_keyfile(root / "key.asc")
        cls.unpublished = PublicGateway(unpublished)
        cls.published = PublicGateway(later)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._root.cleanup()
        cls._key_tmp.cleanup()

    def setUp(self) -> None:
        self.gateway = PublicGateway(self.unpublished.objects)

    def upstream_client(self, *, version: str = VERSION) -> FakeClient:
        """A scripted API: the repository's tags, plus the resolved identity."""
        return FakeClient(
            {
                "/tags?": [{"name": f"tauri-cli-v{version}"}, {"name": "v2.9.0"}],
                "/git/ref/tags/": commit_ref(),
                "/contents/": lock_payload(),
            }
        )

    def manifest(self, root: Path, *, declare_source: bool = True, version: str = VERSION) -> Path:
        if not declare_source:
            # cargo-tauri has no arm64 asset naming, so a source-less fixture
            # cannot be a cargo-tauri asset manifest; use a reviewed asset-only
            # package instead. The plan then correctly finds nothing to build.
            base = copy.deepcopy(load_manifest_data(default_manifest_path()))
            zellij = next(p for p in base["packages"] if p["name"] == "zellij")
            document = {
                "maintainer": base["maintainer"],
                "section": "utils",
                "priority": base["priority"],
                "debian_revision": "-1",
                "packages": [zellij],
            }
            path = root / f"manifest-{version}-plain.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            return path
        identity = source_identity(tag=f"tauri-cli-v{version}")
        document = source_manifest_document(identity.document())
        path = root / f"manifest-{version}{'' if declare_source else '-plain'}.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path


class PublicBaselineTest(SignedRepositoryTest):
    def test_the_signed_index_states_what_is_published(self) -> None:
        baseline = public_baseline(self.gateway, fingerprint=self.fingerprint, arch="arm64")
        self.assertEqual(baseline.fingerprint, self.fingerprint)
        self.assertEqual(baseline.versions["zellij"], "0.45.1")
        # The fixture publishes catalog packages, never the source target: a
        # package the index does not list is the one absence that is allowed.
        self.assertIsNone(baseline.published_version(PACKAGE))

    def test_no_private_object_is_ever_read(self) -> None:
        public_baseline(self.gateway, fingerprint=self.fingerprint, arch="arm64")
        self.assertEqual(
            [read for read in self.gateway.reads if read.endswith(PublicGateway.PRIVATE)],
            [],
        )
        self.assertIn("key.asc", self.gateway.reads)
        self.assertIn(f"dists/{SUITE}/InRelease", self.gateway.reads)
        self.assertIn(f"dists/{SUITE}/main/binary-arm64/Packages", self.gateway.reads)

    def test_a_rotated_key_fails_against_the_pin(self) -> None:
        with self.assertRaisesRegex(ValueError, "fingerprint mismatch"):
            public_baseline(self.gateway, fingerprint="A" * 40, arch="arm64")

    def test_a_malformed_pin_is_refused(self) -> None:
        for pin in ("", "not-a-fingerprint", "94D4978", "A" * 39):
            with self.subTest(pin=pin), self.assertRaisesRegex(ValueError, "full 40-hex"):
                normalize_pin(pin)

    def test_missing_public_metadata_fails_closed(self) -> None:
        for absent in ("key.asc", f"dists/{SUITE}/InRelease", f"dists/{SUITE}/Release.gpg"):
            with self.subTest(absent=absent):
                gateway = PublicGateway(self.unpublished.objects)
                del gateway.objects[absent]
                with self.assertRaisesRegex(BaselineUnavailable, "does not serve"):
                    public_baseline(gateway, fingerprint=self.fingerprint, arch="arm64")

    def test_a_tampered_index_fails_against_the_signed_digest(self) -> None:
        gateway = PublicGateway(self.unpublished.objects)
        key = f"dists/{SUITE}/main/binary-arm64/Packages"
        gateway.objects[key] += b"\nInjected: yes\n"
        with self.assertRaisesRegex(BaselineUnavailable, "does not match the signed SHA256"):
            public_baseline(gateway, fingerprint=self.fingerprint, arch="arm64")

    def test_tampered_release_metadata_fails_the_signature_checks(self) -> None:
        cases = {
            f"dists/{SUITE}/Release": (b"Suite: stable", b"Suite: nightly"),
            f"dists/{SUITE}/Release.gpg": (b"iQEz", b"iQEy"),
            # Text inside the signed block, not after it: the payload a client
            # reads and the body that was signed must still be the same bytes.
            f"dists/{SUITE}/InRelease": (b"apt-at-home", b"attacker"),
        }
        for key, (before, after) in cases.items():
            with self.subTest(key=key):
                gateway = PublicGateway(self.unpublished.objects)
                gateway.objects[key] = gateway.objects[key].replace(before, after, 1)
                self.assertNotEqual(gateway.objects[key], self.unpublished.objects[key])
                with self.assertRaisesRegex(ValueError, "signature is not valid"):
                    public_baseline(gateway, fingerprint=self.fingerprint, arch="arm64")

    def test_the_index_check_is_the_signed_rule_not_a_file_name(self) -> None:
        release = self.unpublished.objects[f"dists/{SUITE}/Release"].decode()
        index = self.unpublished.objects[f"dists/{SUITE}/main/binary-arm64/Packages"]
        check_signed_index(release, "arm64", index)
        with self.assertRaisesRegex(BaselineUnavailable, "omits main/binary-riscv64/Packages"):
            check_signed_index(release, "riscv64", b"")


class PlanModeTest(SignedRepositoryTest):
    def test_upstream_builds_when_the_published_version_differs(self) -> None:
        decided = plan(
            "upstream", source=self.gateway, fingerprint=self.fingerprint,
            client=self.upstream_client(),
        )
        self.assertTrue(decided.build_needed)
        self.assertEqual(decided.desired_version, VERSION)
        self.assertIsNone(decided.published_version)
        self.assertEqual(decided.source.commit, COMMIT)
        self.assertEqual(decided.source.tag, f"tauri-cli-v{VERSION}")

    def test_upstream_skips_the_compile_when_the_version_is_unchanged(self) -> None:
        decided = plan(
            "upstream", source=PublicGateway(self.published.objects),
            fingerprint=self.fingerprint, client=self.upstream_client(),
        )
        self.assertFalse(decided.build_needed)
        self.assertEqual(decided.published_version, VERSION)
        self.assertEqual(decided.source.commit, COMMIT)
        self.assertIn("already carries", decided.summary())

    def test_upstream_still_resolves_the_identity_it_would_not_build(self) -> None:
        # The decision skips the compile, not the verification of what this
        # repository publishes, and it still names the release it compared with.
        decided = plan(
            "upstream", source=PublicGateway(self.published.objects),
            fingerprint=self.fingerprint, client=self.upstream_client(),
        )
        self.assertEqual(decided.fingerprint, self.fingerprint)

    def test_a_manifest_without_a_source_target_plans_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as work:
            decided = plan(
                "manifest", source=self.gateway, fingerprint=self.fingerprint,
                manifest_path=self.manifest(Path(work), declare_source=False),
            )
        self.assertFalse(decided.build_needed)
        self.assertIsNone(decided.source)
        self.assertIsNone(decided.desired_version)
        # Nothing is declared, so nothing could be decided from the repository
        # and nothing was read from it.
        self.assertEqual(self.gateway.reads, [])
        self.assertIn("nothing is declared", decided.summary())

    def test_a_manifest_source_target_follows_the_published_version(self) -> None:
        with tempfile.TemporaryDirectory() as work:
            manifest = self.manifest(Path(work))
            listed = plan(
                "manifest", source=self.gateway, fingerprint=self.fingerprint,
                manifest_path=manifest,
            )
            same = plan(
                "manifest", source=PublicGateway(self.published.objects),
                fingerprint=self.fingerprint, manifest_path=manifest,
            )
        # A valid baseline that lists no such package needs a compile; one that
        # already carries this version does not, and the publisher carries the
        # published deb over rather than compiling it again.
        self.assertTrue(listed.build_needed)
        self.assertIsNone(listed.published_version)
        self.assertIn("no such package", listed.summary())
        self.assertFalse(same.build_needed)
        self.assertEqual((same.published_version, same.desired_version), (VERSION, VERSION))
        self.assertEqual(same.source, source_identity())

    def test_both_modes_follow_the_same_published_version_rule(self) -> None:
        published = PublicGateway(self.published.objects)
        with tempfile.TemporaryDirectory() as work:
            manifest = self.manifest(Path(work))
            newer = plan(
                "upstream", source=published, fingerprint=self.fingerprint,
                client=self.upstream_client(version="2.13.0"),
            )
            manifest_newer = plan(
                "manifest", source=published, fingerprint=self.fingerprint,
                manifest_path=self.manifest(Path(work), version="2.13.0"),
            )
        for decided in (newer, manifest_newer):
            with self.subTest(version=decided.desired_version):
                self.assertTrue(decided.build_needed)
                self.assertEqual(decided.published_version, VERSION)
                self.assertNotEqual(decided.desired_version, decided.published_version)

    def test_the_published_cue_opens_the_reuse_path_only_when_it_should(self) -> None:
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            same = plan(
                "manifest", source=PublicGateway(self.published.objects),
                fingerprint=self.fingerprint, manifest_path=self.manifest(root),
            )
            compile_needed = plan(
                "manifest", source=self.gateway, fingerprint=self.fingerprint,
                manifest_path=self.manifest(root, version="2.13.0"),
            )
            nothing = plan(
                "manifest", source=self.gateway, fingerprint=self.fingerprint,
                manifest_path=self.manifest(root, declare_source=False),
            )
            upstream = plan(
                "upstream", source=PublicGateway(self.published.objects),
                fingerprint=self.fingerprint, client=self.upstream_client(),
            )
            paths = {
                name: write_plan(root / f"{name}.json", value)
                for name, value in (
                    ("same", same), ("compile", compile_needed),
                    ("nothing", nothing), ("upstream", upstream),
                )
            }
            self.assertTrue(published_in_plan(paths["same"]))
            for name in ("compile", "nothing", "upstream"):
                with self.subTest(plan=name):
                    self.assertFalse(published_in_plan(paths[name]))

    def test_an_unknown_mode_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "source plan mode must be one of"):
            plan("sideways", source=self.gateway, fingerprint=self.fingerprint)
        self.assertEqual(MODES, ("upstream", "manifest", "validation"))

    def test_only_the_reviewed_source_target_is_planned(self) -> None:
        with self.assertRaisesRegex(ValueError, "may not be built from source"):
            plan("manifest", source=self.gateway, fingerprint=self.fingerprint, arch="amd64")

    def test_validation_builds_whenever_the_manifest_declares_source(self) -> None:
        # CI-only: no baseline verification, always builds when declared, even
        # when the published index already carries the same version.
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            manifest = self.manifest(root)
            unpublished = plan(
                "validation", fingerprint=self.fingerprint, manifest_path=manifest,
            )
            published = plan(
                "validation", fingerprint=self.fingerprint, manifest_path=manifest,
            )
            # No gateway reads at all: validation never verifies a baseline.
            self.assertEqual(self.gateway.reads, [])
            for decided in (unpublished, published):
                self.assertTrue(decided.build_needed)
                self.assertEqual(decided.desired_version, VERSION)
                self.assertIsNone(decided.published_version)
                self.assertEqual(decided.source, source_identity())
                self.assertEqual(decided.mode, "validation")

    def test_validation_plans_nothing_without_a_source_target(self) -> None:
        with tempfile.TemporaryDirectory() as work:
            decided = plan(
                "validation", fingerprint=self.fingerprint,
                manifest_path=self.manifest(Path(work), declare_source=False),
            )
        self.assertFalse(decided.build_needed)
        self.assertIsNone(decided.source)
        self.assertIsNone(decided.desired_version)
        self.assertIsNone(decided.published_version)

    def test_validation_never_opens_the_reuse_path(self) -> None:
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            decided = plan(
                "validation", fingerprint=self.fingerprint,
                manifest_path=self.manifest(root),
            )
            path = write_plan(root / "validation.json", decided)
            self.assertFalse(published_in_plan(path))


def decided_plan() -> SourcePlan:
    """A plan that builds: one decided identity, as the producer would write it."""
    return SourcePlan(
        package=PACKAGE, arch="arm64", mode="upstream", build_needed=True,
        desired_version=VERSION, published_version=None,
        source=source_identity(), fingerprint=PIN,
    )


class PlanDocumentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = decided_plan()

    def test_a_plan_round_trips_through_its_document(self) -> None:
        self.assertEqual(parse_plan(self.plan.document()), self.plan)
        with tempfile.TemporaryDirectory() as work:
            path = write_plan(Path(work) / "source-plan.json", self.plan)
            self.assertEqual(read_plan(path), self.plan)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["schema"], SCHEMA)

    def test_a_plan_never_carries_an_artifact(self) -> None:
        raw = self.plan.document()
        self.assertNotIn("artifact", raw["source"])
        raw["source"]["artifact"] = {"sha256": "b" * 64, "bytes": 1, "elf_machine": 183}
        with self.assertRaisesRegex(ValueError, "unknown keys"):
            parse_plan(raw)

    def test_unknown_and_missing_keys_are_refused(self) -> None:
        raw = {**self.plan.document(), "reason": "because"}
        with self.assertRaisesRegex(ValueError, "unknown keys"):
            parse_plan(raw)
        without = self.plan.document()
        del without["build_needed"]
        with self.assertRaisesRegex(ValueError, "missing keys"):
            parse_plan(without)
        with self.assertRaisesRegex(ValueError, "must be an object"):
            parse_plan([self.plan.document()])

    def test_a_plan_that_builds_must_name_its_identity(self) -> None:
        with self.assertRaisesRegex(ValueError, "must name the source identity"):
            parse_plan({**self.plan.document(), "source": None})
        nothing = {**self.plan.document(), "source": None, "build_needed": False}
        nothing["desired_version"] = None
        self.assertFalse(parse_plan(nothing).build_needed)
        with self.assertRaisesRegex(ValueError, "must name the source identity"):
            parse_plan({**nothing, "desired_version": VERSION})

    def test_a_version_must_agree_with_its_tag(self) -> None:
        with self.assertRaisesRegex(ValueError, "is not the canonical"):
            parse_plan({**self.plan.document(), "desired_version": "2.12.3"})

    def test_the_pin_the_mode_and_the_flag_are_checked(self) -> None:
        with self.assertRaisesRegex(ValueError, "full 40-hex fingerprint"):
            parse_plan({**self.plan.document(), "trust_fingerprint": "nope"})
        with self.assertRaisesRegex(ValueError, "source plan mode must be one of"):
            parse_plan({**self.plan.document(), "mode": "sideways"})
        with self.assertRaisesRegex(ValueError, "build_needed must be true or false"):
            parse_plan({**self.plan.document(), "build_needed": "yes"})
        with self.assertRaisesRegex(ValueError, "may not be built from source"):
            parse_plan({**self.plan.document(), "arch": "amd64"})

    def test_an_unreadable_plan_is_a_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            broken = root / "broken.json"
            broken.write_text("{ not json", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not valid JSON"):
                read_plan(broken)
            with self.assertRaisesRegex(ValueError, "not found"):
                read_plan(root / "absent.json")


class CommandLineTest(SignedRepositoryTest):
    def argv(self, output: Path, **overrides: object) -> list[str]:
        arguments = [
            "plan",
            "--mode", str(overrides.get("mode", "manifest")),
            "--base-url", "https://apt.example.com/",
            "--fingerprint", str(overrides.get("fingerprint", self.fingerprint)),
            "--output", str(output),
        ]
        if "manifest" in overrides:
            arguments += ["--manifest", str(overrides["manifest"])]
        return arguments

    def run_cli(self, argv: list[str], gateway: PublicGateway) -> int:
        # The CLI writes its decision to stdout and its reason to stderr; neither
        # belongs in the test log, and both are the workflow's real contract.
        with mock.patch("scripts.source_plan.public_source", return_value=gateway), \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = main(argv)
        self.printed = out.getvalue().strip()
        self.reported = err.getvalue().strip()
        return code

    def test_the_cli_writes_the_decision_and_prints_it(self) -> None:
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            output = root / "dist" / "source-artifacts" / "source-plan.json"
            code = self.run_cli(
                self.argv(output, manifest=self.manifest(root)), self.gateway
            )
            self.assertEqual(code, 0)
            self.assertEqual(self.printed, "true")
            self.assertIn(str(output), self.reported)
            self.assertTrue(read_plan(output).build_needed)

    def test_the_cli_refuses_a_rotated_key_without_writing_a_plan(self) -> None:
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            output = root / "source-plan.json"
            code = self.run_cli(
                self.argv(output, manifest=self.manifest(root), fingerprint="B" * 40),
                self.gateway,
            )
            self.assertEqual(code, 1)
            self.assertEqual(self.printed, "")
            self.assertFalse(output.exists())

    def test_the_published_cue_answers_without_touching_the_network(self) -> None:
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            output = root / "source-plan.json"
            self.run_cli(self.argv(output, manifest=self.manifest(root)), self.gateway)
            before = list(self.gateway.reads)
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = main(["published", "--plan", str(output)])
            self.assertEqual(code, 0)
            self.assertEqual(stdout.getvalue().strip(), "false")
            self.assertEqual(self.gateway.reads, before, "the cue only reads the plan file")
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["published", "--plan", str(root / "absent.json")]), 1)

    def test_an_absent_manifest_is_a_failure_not_a_plan(self) -> None:
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            output = root / "source-plan.json"
            code = self.run_cli(self.argv(output, manifest=root / "absent.json"), self.gateway)
            self.assertEqual(code, 1)
            self.assertEqual(self.printed, "")
            self.assertIn("source planning failed", self.reported)
            self.assertFalse(output.exists())


class ProducerOrchestrationTest(unittest.TestCase):
    """The two steps between a plan and a compiler: discovery and checkout."""

    def test_only_the_reviewed_tag_shape_is_a_candidate(self) -> None:
        for tag, expected in (
            ("tauri-cli-v2.12.2", "2.12.2"),
            ("tauri-cli-v2.9.0", "2.9.0"),
            ("v2.12.2", None),
            ("tauri-cli-v2.12.2-rc.1", None),
            ("tauri-v2.12.2", None),
            (2, None),
            (None, None),
        ):
            with self.subTest(tag=tag):
                self.assertEqual(stable_version(tag), expected)

    def test_the_highest_reviewed_tag_wins_by_version_not_order(self) -> None:
        client = FakeClient(
            {
                "/tags?": [
                    {"name": "tauri-cli-v2.9.0"},
                    {"name": "v2.30.0"},
                    {"name": "tauri-cli-v2.12.2"},
                    {"name": "tauri-cli-v2.12.2-beta.1"},
                ]
            }
        )
        self.assertEqual(latest_stable_version(client), "2.12.2")
        self.assertEqual(
            client.requested,
            ["https://api.github.com/repos/tauri-apps/tauri/tags?per_page=100"],
        )

    def test_a_listing_with_no_reviewed_release_is_a_refusal(self) -> None:
        for payload, message in (
            ([{"name": "v2.12.2"}], "tags no tauri-cli-vN.N.N"),
            ({"name": "tauri-cli-v2.12.2"}, "not a list"),
            (["tauri-cli-v2.12.2"], "entry is not an object"),
        ):
            with self.subTest(payload=payload), self.assertRaisesRegex(ValueError, message):
                latest_stable_version(FakeClient({"/tags?": payload}))

    def test_the_checkout_fetches_the_reviewed_tag_then_proves_the_commit(self) -> None:
        identity = source_identity()
        recorded: list[list[str]] = []

        def runner(argv: list[str], cwd: Path) -> "subprocess.CompletedProcess[str]":
            recorded.append(list(argv))
            if argv[-1] == identity.commit:
                # What a real checkout leaves behind: the pinned lockfile, which
                # the checkout check reads before the compiler is trusted with it.
                (Path(cwd) / "Cargo.lock").write_bytes(LOCK)
            return subprocess.CompletedProcess(argv, 0, "", "")

        with tempfile.TemporaryDirectory() as work:
            destination = Path(work) / "upstream"
            with mock.patch.object(prov, "_git_output", side_effect=_git_answers(identity)):
                check_out(identity, destination, runner=runner)
            self.assertTrue((destination / "Cargo.lock").is_file())
        # The tag is fetched by name and the commit checked out by name, so a tag
        # that moved lands somewhere `check_checkout` refuses rather than somewhere
        # the build would silently accept.
        self.assertEqual(
            recorded,
            [
                ["git", "init", "--quiet"],
                ["git", "remote", "add", "origin", identity.repository],
                [
                    "git", "fetch", "--quiet", "--depth", "1", "--no-tags", "origin",
                    f"+refs/tags/{identity.tag}:refs/tags/{identity.tag}",
                ],
                ["git", "checkout", "--quiet", "--detach", identity.commit],
            ],
        )

    def test_a_checkout_never_writes_into_a_directory_that_has_content(self) -> None:
        identity = source_identity()
        with tempfile.TemporaryDirectory() as work:
            destination = Path(work) / "upstream"
            destination.mkdir()
            (destination / "file").write_text("busy", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not an empty directory"):
                check_out(
                    identity, destination,
                    runner=lambda argv, cwd: subprocess.CompletedProcess(argv, 0, "", ""),
                )

    def test_a_failing_git_command_reports_what_it_ran(self) -> None:
        identity = source_identity()
        calls = 0

        def runner(argv: list[str], cwd: Path) -> "subprocess.CompletedProcess[str]":
            nonlocal calls
            calls += 1
            return subprocess.CompletedProcess(argv, 1, "", "could not read Username")

        with tempfile.TemporaryDirectory() as work:
            with self.assertRaisesRegex(
                ValueError, "git init --quiet failed: could not read Username"
            ):
                check_out(identity, Path(work) / "upstream", runner=runner)
            self.assertEqual(calls, 1)

    def test_a_plan_and_an_identity_document_resolve_the_same_way(self) -> None:
        identity = source_identity()
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            plan_file = write_plan(root / PLAN_NAME, decided_plan())
            self.assertEqual(resolve_plan(plan_file), (identity, VERSION))
            self.assertEqual(resolve_plan_identity(plan_file), identity)
            identity_file = write_identity(root / "identity.json", identity)
            self.assertEqual(resolve_plan(identity_file), (identity, None))
            self.assertEqual(planned_version(identity_file, identity), VERSION)

    def test_a_plan_that_declares_nothing_has_no_identity_to_check_out(self) -> None:
        from scripts.source_plan import SourcePlan

        nothing = SourcePlan(
            package=PACKAGE, arch="arm64", mode="manifest", build_needed=False,
            desired_version=None, published_version=None, source=None, fingerprint=PIN,
        )
        with tempfile.TemporaryDirectory() as work:
            path = write_plan(Path(work) / PLAN_NAME, nothing)
            with self.assertRaisesRegex(ValueError, "declares no source target"):
                resolve_plan_identity(path)
            with self.assertRaisesRegex(ValueError, "not found"):
                resolve_plan_identity(Path(work) / "absent.json")
            broken = Path(work) / "broken.json"
            broken.write_text("{ not json", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not valid JSON"):
                resolve_plan_identity(broken)


class PublicHttpsSourceTest(unittest.TestCase):
    """The credential-free reader: anonymous, bounded, https-only, one object key."""

    BASE = "https://apt.example.com/"

    def reader(self, payload: bytes = b"body"):
        seen: list[urllib.request.Request] = []

        def read(request: urllib.request.Request, max_bytes: int, timeout: int) -> bytes:
            seen.append(request)
            if len(payload) > max_bytes:
                raise ValueError(f"object {request.full_url} exceeded the {max_bytes} byte limit")
            return payload

        return PublicHttpsSource(self.BASE, reader=read), seen

    def test_a_key_is_read_from_the_public_root(self) -> None:
        source, seen = self.reader(b"key bytes")
        self.assertEqual(source.get("dists/stable/Release", max_bytes=4096), b"key bytes")
        request = seen[0]
        self.assertEqual(request.full_url, f"{self.BASE}dists/stable/Release")
        self.assertEqual(request.get_method(), "GET")
        # Anonymous by construction: no credential header, and nothing a caller
        # could add one through.
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(set(headers), {"user-agent", "accept-encoding"})

    def test_only_an_https_root_is_accepted(self) -> None:
        for base in (
            "http://apt.example.com/", "file:///etc/passwd", "apt.example.com/",
            "https://apt.example.com", "https://apt.example.com/../etc/",
        ):
            with self.subTest(base=base), self.assertRaisesRegex(ValueError, "https:// root URL"):
                PublicHttpsSource(base)

    def test_a_key_must_be_a_plain_object_path(self) -> None:
        source, _ = self.reader()
        for key in (
            "", "../secrets", "dists/../../etc/passwd", "/dists", "https://other/x",
            "dists//stable", "./dists", "dists/stable?x=1",
        ):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "plain object path"):
                source.get(key, max_bytes=4096)

    def test_a_missing_object_is_a_not_found_and_a_failure_is_not(self) -> None:
        missing = urllib.error.HTTPError(f"{self.BASE}key.asc", 404, "Not Found", {}, None)
        denied = urllib.error.HTTPError(f"{self.BASE}key.asc", 403, "Forbidden", {}, None)
        self.addCleanup(missing.close)
        self.addCleanup(denied.close)

        def raising(error: urllib.error.HTTPError):
            def read(request, max_bytes, timeout):
                raise error
            return read

        with self.assertRaises(ObjectNotFound):
            PublicHttpsSource(self.BASE, reader=raising(missing)).get("key.asc", max_bytes=4096)
        with self.assertRaises(urllib.error.HTTPError):
            PublicHttpsSource(self.BASE, reader=raising(denied)).get("key.asc", max_bytes=4096)

    def test_the_byte_ceiling_is_enforced_on_what_arrives(self) -> None:
        source, _ = self.reader(b"0123456789")
        self.assertEqual(source.get("key.asc", max_bytes=10), b"0123456789")
        with self.assertRaisesRegex(ValueError, "exceeded the 9 byte limit"):
            source.get("key.asc", max_bytes=9)


if __name__ == "__main__":
    unittest.main()