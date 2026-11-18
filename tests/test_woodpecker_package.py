"""The Woodpecker package builders: official-deb repack and arm64 assembly.

The tests use synthetic packages that carry the reviewed official metadata,
layout, and maintainer script, so the build behavior is exercised without
upstream bytes or network access. Real official debs are inspected outside the
suite during acceptance validation.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import lzma
import subprocess
import tarfile
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from scripts.deb import SOURCE_DATE_EPOCH, build_deb
from scripts.manifest import deb_filename, load_manifest, parse_manifest
from scripts.package_binaries import extract_binaries
from scripts.smoke import SmokeError, check_pool
from scripts.woodpecker_package import (
    AGENT_ENV_EXAMPLE,
    AGENT_SERVICE,
    AGENT_STATE_DIR,
    OFFICIAL_METADATA,
    OFFICIAL_DEBS,
    PACKAGING_DIR,
    PayloadEntry,
    assemble_deb,
    build_arm64_package,
    check_legacy_pool_package,
    repack_official_deb,
    vendored,
)
from tests.helpers import (
    stage_fixture_target,
    write_fake_official_deb,
    write_target_archive,
)
from tests.test_smoke import fake_elf

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ("woodpecker-agent", "woodpecker-cli")


def _manifest():
    data = json.loads(
        (REPO_ROOT / "packaging" / "manifest.json").read_text(encoding="utf-8")
    )
    data["packages"] = [
        entry for entry in data["packages"] if entry["name"] in PACKAGES
    ]
    return parse_manifest(data)


def _package(manifest, name):
    return next(pkg for pkg in manifest.packages if pkg.name == name)


def _document():
    return json.loads(
        (REPO_ROOT / "packaging" / "manifest.json").read_text(encoding="utf-8")
    )


def _legacy_document():
    """The signed -1 baseline shape: both arches fetch the pre-official tar."""
    data = _document()
    data["packages"] = [
        entry for entry in data["packages"] if entry["name"] in PACKAGES
    ]
    for entry in data["packages"]:
        entry.pop("debian_revision", None)
        amd64 = entry["targets"]["amd64"]
        amd64.pop("official_deb", None)
        amd64["url"] = (
            "https://github.com/woodpecker-ci/woodpecker/releases/download/"
            f"v3.20.1/{entry['name']}_linux_amd64.tar.gz"
        )
        amd64["sha256"] = "ab" * 32
    return data


def _ar_member(deb: Path, member: str) -> bytes:
    proc = subprocess.run(
        ["ar", "p", str(deb), member], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr.decode())
    return proc.stdout


def _control_text(deb: Path) -> str:
    proc = subprocess.run(
        ["dpkg-deb", "-f", str(deb)], text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _repack(root: Path, manifest, name: str) -> tuple[Path, Path]:
    pkg = _package(manifest, name)
    payload = fake_elf("amd64")
    source = write_fake_official_deb(
        root / "source" / f"{name}_3.20.1_amd64.deb", pkg, "amd64", payload
    )
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    reviewed = replace(
        pkg,
        targets={
            arch: replace(target, sha256=digest if arch == "amd64" else target.sha256)
            for arch, target in pkg.targets.items()
        },
    )
    manifest = replace(
        manifest,
        packages=tuple(
            reviewed if item.name == name else item for item in manifest.packages
        ),
    )
    output = root / "pool" / deb_filename(reviewed, "amd64", manifest)
    repack_official_deb(
        package=reviewed, arch="amd64", manifest=manifest, source=source,
        output=output, expected_sha256=digest,
    )
    return source, output


def _arm64(root: Path, manifest, name: str) -> Path:
    pkg = _package(manifest, name)
    archive = write_target_archive(
        root / "source" / f"{name}_linux_arm64.tar.gz", pkg, "arm64",
        fake_elf("arm64"),
    )
    payloads = extract_binaries(archive, pkg.targets["arm64"], root / "extract")
    output = root / "pool" / deb_filename(
        replace(pkg, debian_revision="-2"), "arm64", manifest
    )
    build_arm64_package(
        package=pkg, arch="arm64", manifest=manifest,
        binary_source=payloads[name], output=output,
    )
    return output


class RepackOfficialDebTest(unittest.TestCase):
    def test_it_changes_only_the_control_version(self) -> None:
        manifest = _manifest()
        with tempfile.TemporaryDirectory() as tmp:
            source, output = _repack(Path(tmp), manifest, "woodpecker-agent")
            self.assertEqual(
                _ar_member(source, "data.tar.gz"), _ar_member(output, "data.tar.gz")
            )
            self.assertIn("Version: 3.20.1-2", _control_text(output))
            self.assertIn("Version: 3.20.1\n", _control_text(source))
            for script in ("preinst", "md5sums"):
                proc = subprocess.run(
                    ["dpkg-deb", "--ctrl-tarfile", str(output)], stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, check=True,
                )
                extracted = subprocess.run(
                    ["tar", "-xO", "-f", "-", f"./{script}"],
                    input=proc.stdout, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, check=False,
                )
                if script == "preinst":
                    self.assertEqual(extracted.stdout, vendored(
                        "nfpm/woodpecker-system-user.preinstall.sh"
                    ))
                else:
                    self.assertEqual(extracted.returncode, 0)

    def test_a_repeat_repack_is_byte_for_byte_identical(self) -> None:
        manifest = _manifest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _source, first = _repack(root / "a", manifest, "woodpecker-cli")
            _source, second = _repack(root / "b", manifest, "woodpecker-cli")
            self.assertEqual(
                hashlib.sha256(first.read_bytes()).hexdigest(),
                hashlib.sha256(second.read_bytes()).hexdigest(),
            )

    def test_a_changed_control_identity_is_refused(self) -> None:
        manifest = _manifest()
        pkg = _package(manifest, "woodpecker-cli")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            control = (
                "Package: another-package\nVersion: 3.20.1\nSection: utils\n"
                "Priority: optional\nArchitecture: amd64\nLicense: Apache 2.0\n"
                "Maintainer: Woodpecker Authors <maintainer@woodpecker-ci.org>\n"
                "Installed-Size: 1\nHomepage: https://woodpecker-ci.org/\n"
                "Description: Woodpecker CLI\n"
            )
            source = root / "source.deb"
            binary_payload = fake_elf("amd64")
            md5sums = (
                f"{hashlib.md5(binary_payload).hexdigest()}  "
                "./usr/local/bin/woodpecker-cli\n"
            ).encode()
            assemble_deb(
                source,
                control_files=[
                    PayloadEntry("./control", control.encode(), 0o644),
                    PayloadEntry("./md5sums", md5sums, 0o644),
                ],
                data_entries=[
                    PayloadEntry("./usr", None, 0o755),
                    PayloadEntry("./usr/local", None, 0o755),
                    PayloadEntry("./usr/local/bin", None, 0o755),
                    PayloadEntry("./usr/local/bin/woodpecker-cli", binary_payload, 0o755),
                ],
            )
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            with self.assertRaisesRegex(ValueError, "is not 'woodpecker-cli'"):
                repack_official_deb(
                    package=pkg, arch="amd64", manifest=manifest, source=source,
                    output=root / "out.deb", expected_sha256=digest,
                )

    def test_a_missing_official_payload_path_is_refused(self) -> None:
        manifest = _manifest()
        pkg = _package(manifest, "woodpecker-agent")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            control = (
                "Package: woodpecker-agent\nVersion: 3.20.1\n"
                "Section: daemon/system\nPriority: optional\nArchitecture: amd64\n"
                "License: Apache 2.0\n"
                "Maintainer: Woodpecker Authors <maintainer@woodpecker-ci.org>\n"
                "Installed-Size: 1\nHomepage: https://woodpecker-ci.org/\n"
                "Description: Woodpecker Agent\n"
            )
            source = root / "source.deb"
            assemble_deb(
                source,
                control_files=[PayloadEntry("./control", control.encode(), 0o644)],
                data_entries=[
                    PayloadEntry("./usr", None, 0o755),
                    PayloadEntry("./usr/local/bin/woodpecker-agent", b"x", 0o755),
                ],
            )
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            with self.assertRaisesRegex(ValueError, "missing"):
                repack_official_deb(
                    package=pkg, arch="amd64", manifest=manifest, source=source,
                    output=root / "out.deb", expected_sha256=digest,
                )

    def test_an_unsafe_data_member_path_is_refused(self) -> None:
        manifest = _manifest()
        pkg = _package(manifest, "woodpecker-cli")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            control = (
                "Package: woodpecker-cli\nVersion: 3.20.1\nSection: utils\n"
                "Priority: optional\nArchitecture: amd64\nLicense: Apache 2.0\n"
                "Maintainer: Woodpecker Authors <maintainer@woodpecker-ci.org>\n"
                "Installed-Size: 1\nHomepage: https://woodpecker-ci.org/\n"
                "Description: Woodpecker CLI\n"
            )
            source = root / "source.deb"
            assemble_deb(
                source,
                control_files=[PayloadEntry("./control", control.encode(), 0o644)],
                data_entries=[
                    PayloadEntry("../escape", b"x", 0o755),
                    PayloadEntry("./usr/local/bin/woodpecker-cli", fake_elf("amd64"), 0o755),
                ],
            )
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            with self.assertRaisesRegex(ValueError, "unsafe package member"):
                repack_official_deb(
                    package=pkg, arch="amd64", manifest=manifest, source=source,
                    output=root / "out.deb", expected_sha256=digest,
                )

    def test_a_tampered_source_digest_is_refused(self) -> None:
        manifest = _manifest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg = _package(manifest, "woodpecker-cli")
            source = write_fake_official_deb(
                root / "source.deb", pkg, "amd64", fake_elf("amd64")
            )
            with self.assertRaisesRegex(ValueError, "digest changed"):
                repack_official_deb(
                    package=pkg, arch="amd64", manifest=manifest, source=source,
                    output=root / "out.deb", expected_sha256="0" * 64,
                )


class AssembleArm64Test(unittest.TestCase):
    def test_the_agent_matches_the_official_layout_and_preinstall(self) -> None:
        manifest = _manifest()
        with tempfile.TemporaryDirectory() as tmp:
            output = _arm64(Path(tmp), manifest, "woodpecker-agent")
            listing = subprocess.run(
                ["dpkg-deb", "-c", str(output)], text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(listing.returncode, 0)
            for entry in (
                "./usr/local/bin/woodpecker-agent",
                "./usr/local/lib/systemd/system/woodpecker-agent.service",
                "./etc/woodpecker/woodpecker-agent.env.example",
                "./var/lib/woodpecker/",
            ):
                self.assertIn(entry, listing.stdout)
            self.assertNotIn("./usr/bin/", listing.stdout)
            self.assertNotIn("./usr/share/doc/", listing.stdout)
            self.assertIn("-rwxr-xr-x root/root", listing.stdout)
            self.assertIn("drwxr-x--- woodpecker/woodpecker", listing.stdout)
            self.assertIn("rw-rw-r-- root/root", listing.stdout)
            fields = subprocess.run(
                ["dpkg-deb", "-f", str(output)], text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            ).stdout
            self.assertIn("Version: 3.20.1-2", fields)
            self.assertIn("Section: daemon/system", fields)
            self.assertIn("License: Apache 2.0", fields)

            with tempfile.TemporaryDirectory() as control:
                proc = subprocess.run(
                    ["dpkg-deb", "-e", str(output), control], text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(
                    (Path(control) / "preinst").read_bytes(),
                    vendored("nfpm/woodpecker-system-user.preinstall.sh"),
                )
                for absent in ("postinst", "prerm", "postrm"):
                    self.assertFalse((Path(control) / absent).exists())

    def test_the_cli_is_a_single_binary_with_no_maintainer_scripts(self) -> None:
        manifest = _manifest()
        with tempfile.TemporaryDirectory() as tmp:
            output = _arm64(Path(tmp), manifest, "woodpecker-cli")
            listing = subprocess.run(
                ["dpkg-deb", "-c", str(output)], text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            ).stdout
            self.assertIn("./usr/local/bin/woodpecker-cli", listing)
            self.assertNotIn("./etc/", listing)
            self.assertNotIn("./var/", listing)
            ctrl = subprocess.run(
                ["dpkg-deb", "--ctrl-tarfile", str(output)], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, check=False,
            ).stdout
            self.assertNotIn(b"preinst", ctrl)
            self.assertIn("Section: utils", _control_text(output))

    def test_both_arches_are_deterministic(self) -> None:
        manifest = _manifest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = _arm64(root / "a", manifest, "woodpecker-agent")
            second = _arm64(root / "b", manifest, "woodpecker-agent")
            self.assertEqual(
                hashlib.sha256(first.read_bytes()).hexdigest(),
                hashlib.sha256(second.read_bytes()).hexdigest(),
            )

    def test_the_vendored_provenance_matches_the_module(self) -> None:
        provenance = json.loads(
            (PACKAGING_DIR / "provenance.json").read_text(encoding="utf-8")
        )
        self.assertEqual(provenance["tag"], "v3.20.1")
        for source, record in provenance["files"].items():
            with self.subTest(source=source):
                payload = (
                    PACKAGING_DIR / record["path"]
                ).read_bytes()
                self.assertEqual(
                    hashlib.sha256(payload).hexdigest(), record["sha256"]
                )
                self.assertEqual(payload, vendored(source))
        for name, arches in provenance["official_debs"].items():
            with self.subTest(package=name):
                record = arches["amd64"]
                self.assertEqual(OFFICIAL_DEBS[name], (record["name"], record["sha256"]))

    def test_the_md5sums_cover_every_payload_file(self) -> None:
        manifest = _manifest()
        with tempfile.TemporaryDirectory() as tmp:
            output = _arm64(Path(tmp), manifest, "woodpecker-agent")
            ctrl = subprocess.run(
                ["dpkg-deb", "--ctrl-tarfile", str(output)], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, check=False,
            ).stdout
            md5sums = subprocess.run(
                ["tar", "-xO", "-f", "-", "./md5sums"], input=ctrl,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            ).stdout.decode()
            self.assertIn(" usr/local/bin/woodpecker-agent", md5sums)
            self.assertIn(
                " usr/local/lib/systemd/system/woodpecker-agent.service", md5sums
            )
            self.assertIn(
                " etc/woodpecker/woodpecker-agent.env.example", md5sums
            )
            self.assertEqual(len(md5sums.splitlines()), 3)

    def test_the_agent_service_and_env_example_are_the_reviewed_files(self) -> None:
        self.assertEqual(
            vendored("nfpm/woodpecker-agent.service").count(b"ExecStart=/usr/local/bin/woodpecker-agent"),
            1,
        )
        self.assertEqual(
            vendored("nfpm/woodpecker-agent.env.example"),
            (PACKAGING_DIR / "v3.20.1" / "woodpecker-agent.env.example").read_bytes(),
        )
        self.assertEqual(SOURCE_DATE_EPOCH, 1767225600)
        self.assertEqual(OFFICIAL_METADATA["woodpecker-agent"]["installed"],
                         "/usr/local/bin/woodpecker-agent")


def _rewrite_tar(payload: bytes, target: str, **changes: object) -> bytes:
    """Re-emit one tar member with changed metadata, keeping other bytes intact."""
    output = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as source, \
            tarfile.open(fileobj=output, mode="w", format=tarfile.GNU_FORMAT) as dest:
        for info in source:
            body = source.extractfile(info).read() if info.isreg() else None
            if info.name == target:
                for key, value in changes.items():
                    setattr(info, key, value)
            dest.addfile(info, io.BytesIO(body) if body is not None else None)
    return gzip.compress(output.getvalue(), compresslevel=9, mtime=0)


def _rewrite_ar(deb: Path, prefix: str, target: str, **changes: object) -> Path:
    """Rewrite one ar member's tar metadata; the caller handles any digest."""
    from scripts.woodpecker_package import _ar_bytes, _read_ar

    members = _read_ar(deb)
    rewritten = {
        name: (
            _rewrite_tar(payload, target, **changes)
            if name.startswith(prefix) else payload
        )
        for name, payload in members.items()
    }
    output = deb.with_name(f"tampered-{deb.name}")
    output.write_bytes(_ar_bytes(list(rewritten.items())))
    return output


def _add_control_member(
    deb: Path, name: str, payload: bytes, mode: int
) -> Path:
    """Append one control-archive member so a scripted package can be tested."""
    from scripts.woodpecker_package import _ar_bytes, _read_ar

    members = _read_ar(deb)
    control_name = next(name for name in members if name.startswith("control.tar"))
    output = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(members[control_name]), mode="r:*") as source, \
            tarfile.open(fileobj=output, mode="w", format=tarfile.GNU_FORMAT) as dest:
        for info in source:
            body = source.extractfile(info).read() if info.isreg() else None
            dest.addfile(info, io.BytesIO(body) if body is not None else None)
        info = tarfile.TarInfo(name)
        info.mode = mode
        info.mtime = SOURCE_DATE_EPOCH
        info.uid = 0
        info.gid = 0
        info.size = len(payload)
        dest.addfile(info, io.BytesIO(payload))
    members[control_name] = (
        lzma.compress(output.getvalue())
        if control_name.endswith(".xz")
        else gzip.compress(output.getvalue(), compresslevel=9, mtime=0)
    )
    scripted = deb.with_name(f"scripted-{deb.name}")
    scripted.write_bytes(_ar_bytes(list(members.items())))
    return scripted


class RetainedLegacyPackageTest(unittest.TestCase):
    """A signed -1 baseline is restored, never rebuilt, and smokes as legacy."""

    def test_a_mixed_release_keeps_the_legacy_agent_and_current_cli(self) -> None:
        data = _legacy_document()
        cli = next(e for e in data["packages"] if e["name"] == "woodpecker-cli")
        cli["debian_revision"] = "-2"
        cli["targets"]["amd64"]["official_deb"] = True
        cli["targets"]["amd64"]["url"] = (
            "https://github.com/woodpecker-ci/woodpecker/releases/download/"
            "v3.20.1/woodpecker-cli_3.20.1_amd64.deb"
        )
        cli["targets"]["amd64"]["sha256"] = OFFICIAL_DEBS["woodpecker-cli"][1]
        manifest = parse_manifest(data)
        by_name = {pkg.name: pkg for pkg in manifest.packages}
        self.assertEqual(by_name["woodpecker-agent"].debian_revision, "")
        self.assertFalse(by_name["woodpecker-agent"].targets["amd64"].official_deb)
        self.assertEqual(by_name["woodpecker-cli"].debian_revision, "-2")
        self.assertTrue(by_name["woodpecker-cli"].targets["amd64"].official_deb)

    def test_build_refuses_to_rebuild_a_retained_legacy_target(self) -> None:
        from scripts import build as build_module
        from scripts.build import build_all

        manifest = parse_manifest(_legacy_document())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "upstream"
            cache.mkdir()
            original = build_module.fetch_verified
            build_module.fetch_verified = (  # type: ignore[assignment]
                lambda url, sha, dest, **kwargs: dest
            )
            try:
                with self.assertRaisesRegex(ValueError, "retained legacy target"):
                    build_all(manifest, pool_dir=root / "pool", cache_dir=cache)
            finally:
                build_module.fetch_verified = original  # type: ignore[assignment]

    def test_smoke_checks_a_retained_legacy_pool_package(self) -> None:
        manifest = parse_manifest(_legacy_document())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pool = root / "pool"
            pool.mkdir()
            for pkg in manifest.packages:
                stage = stage_fixture_target(
                    root, pkg, "amd64", manifest, fake_elf("amd64"),
                    root / "stages",
                )
                build_deb(stage, pool / deb_filename(pkg, "amd64", manifest))
            findings = check_pool(manifest, pool, ("amd64",))
        self.assertEqual(len(findings), len(PACKAGES))
        self.assertTrue(any("retained legacy" in line for line in findings))
        self.assertTrue(
            all("/usr/bin/" in line for line in findings)
        )


    def test_a_retained_legacy_package_with_a_maintainer_script_is_refused(self) -> None:
        manifest = parse_manifest(_legacy_document())
        pkg = _package(manifest, "woodpecker-agent")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pool = root / "pool"
            pool.mkdir()
            stage = stage_fixture_target(
                root, pkg, "amd64", manifest, fake_elf("amd64"), root / "stages"
            )
            deb = pool / deb_filename(pkg, "amd64", manifest)
            build_deb(stage, deb)
            scripted = _add_control_member(
                deb, "./preinst", b"#!/bin/sh\nexit 0\n", 0o755
            )
            extracted = root / "extracted"
            subprocess.run(
                ["dpkg-deb", "-x", str(scripted), str(extracted)],
                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            with self.assertRaisesRegex(SmokeError, "unexpectedly carries"):
                check_legacy_pool_package(
                    package=pkg, arch="amd64", manifest=manifest,
                    deb=scripted, root=extracted,
                )

    def _native_finding(
        self, manifest, name: str, binary_path: Path
    ) -> tuple[str, mock.Mock]:
        from scripts.smoke_runtime import run_package

        pkg = _package(manifest, name)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root / binary_path
            binary.parent.mkdir(parents=True)
            binary.write_bytes(fake_elf("amd64"))
            completed = subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout=f"{name} version 3.20.1\n", stderr="",
            )
            with mock.patch(
                "scripts.smoke_runtime._run", return_value=completed
            ) as runner:
                findings = run_package(
                    pkg, "amd64", prefix=root, scratch=root / "scratch",
                    manifest=manifest,
                )
        return findings[0], runner

    def test_native_smoke_uses_the_legacy_usr_bin_path(self) -> None:
        manifest = parse_manifest(_legacy_document())
        finding, runner = self._native_finding(
            manifest, "woodpecker-agent", Path("usr/bin/woodpecker-agent")
        )
        self.assertIn("/usr/bin/woodpecker-agent", finding)
        self.assertEqual(runner.call_args.args[0].name, "woodpecker-agent")
        self.assertEqual(
            runner.call_args.args[0].parent.name, "bin"
        )
        self.assertEqual(
            runner.call_args.args[0].parent.parent.name, "usr"
        )

    def test_native_smoke_still_uses_the_current_local_bin_path(self) -> None:
        manifest = parse_manifest(_document())
        finding, runner = self._native_finding(
            manifest, "woodpecker-cli", Path("usr/local/bin/woodpecker-cli")
        )
        self.assertIn("/usr/local/bin/woodpecker-cli", finding)
        called = runner.call_args.args[0]
        self.assertEqual(called.name, "woodpecker-cli")
        self.assertEqual(called.parent.name, "bin")
        self.assertEqual(called.parent.parent.name, "local")
        self.assertEqual(called.parent.parent.parent.name, "usr")


class OfficialMetadataTest(unittest.TestCase):
    """The reviewed official payload metadata is exact, including hidden bits."""

    def _tampered_repack(self, deb: Path) -> Path:
        manifest = _manifest()
        pkg = _package(manifest, "woodpecker-cli")
        output = deb.parent / "out.deb"
        repack_official_deb(
            package=pkg, arch="amd64", manifest=manifest, source=deb,
            output=output,
            expected_sha256=hashlib.sha256(deb.read_bytes()).hexdigest(),
        )
        return output

    def _source(self, root: Path) -> Path:
        return write_fake_official_deb(
            root / "source.deb", _package(_manifest(), "woodpecker-cli"),
            "amd64", fake_elf("amd64"),
        )

    def test_a_special_permission_bit_in_the_official_payload_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = self._source(Path(tmp))
            tampered = _rewrite_ar(
                source, "data.tar", "./usr/local/bin/woodpecker-cli",
                mode=0o4755,
            )
            with self.assertRaisesRegex(ValueError, "mode is 4755"):
                self._tampered_repack(tampered)

    def test_an_unreviewed_numeric_owner_in_the_payload_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = self._source(Path(tmp))
            tampered = _rewrite_ar(
                source, "data.tar", "./usr/local/bin/woodpecker-cli",
                uid=4242,
            )
            with self.assertRaisesRegex(ValueError, "numeric owner is 4242/0"):
                self._tampered_repack(tampered)

    def test_a_special_permission_bit_in_control_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = self._source(Path(tmp))
            tampered = _rewrite_ar(
                source, "control.tar", "./control", mode=0o4644
            )
            with self.assertRaisesRegex(
                ValueError, "control ./control mode is 4644"
            ):
                self._tampered_repack(tampered)


if __name__ == "__main__":
    unittest.main()
