from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.manifest import load_manifest
from scripts.package_licenses import pi_source_document
from scripts.pi_bundle import launcher_bytes, update_helper_bytes
from scripts.pi_smoke import check_bundle_payload, check_version_output
from scripts.smoke_package import SmokeError
from tests.test_pi_bundle import fake_elf, package_json


def installed_bundle(root: Path, arch: str) -> None:
    launcher = root / "usr/bin/pi"
    launcher.parent.mkdir(parents=True)
    launcher.write_bytes(launcher_bytes())
    launcher.chmod(0o755)
    bundle = root / "usr/lib/pi"
    bundle.mkdir(parents=True)
    helper = bundle / "apt-update"
    helper.write_bytes(update_helper_bytes())
    helper.chmod(0o755)
    for relative in ("pi", f"native/linux/prebuilds/linux-{'x64' if arch == 'amd64' else 'arm64'}/linux-platform-x11.node"):
        binary = bundle / relative
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(fake_elf(arch))
        binary.chmod(0o755)
    for relative, body in (
        ("photon_rs_bg.wasm", b"\0asm"),
        ("package.json", package_json()),
        ("README.md", b"# pi\n"),
        ("CHANGELOG.md", b"# changelog\n"),
        ("theme/dark.json", b"{}"),
        ("theme/light.json", b"{}"),
        ("export-html/template.html", b"<html></html>"),
        ("export-html/vendor/marked.min.js", b"/* marked */"),
        ("assets/clankolas.png", b"\x89PNG"),
        ("docs/cli.md", b"# cli\n"),
        ("examples/demo/build.sh", b"#!/bin/sh\n"),
    ):
        path = bundle / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        path.chmod(0o755 if relative.endswith("build.sh") else 0o644)
    source = root / "usr/share/doc/pi/README.source"
    source.parent.mkdir(parents=True)
    source.write_text(pi_source_document("1.5.0"), encoding="utf-8")
    source.chmod(0o644)


class PiSmokeTest(unittest.TestCase):
    def test_bundle_payload_proves_launcher_helper_resources_and_architecture(self) -> None:
        package = next(pkg for pkg in load_manifest().packages if pkg.name == "pi")
        for arch in ("amd64", "arm64"):
            with self.subTest(arch=arch), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                installed_bundle(root, arch)
                findings = check_bundle_payload(package, arch, root)
                self.assertEqual(len(findings), 2)
                self.assertIn("APT update helper", findings[0])
                self.assertIn("ELF", findings[1])

    def test_rejects_unowned_launcher_helper_and_wrong_architecture(self) -> None:
        package = next(pkg for pkg in load_manifest().packages if pkg.name == "pi")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            installed_bundle(root, "arm64")
            with self.assertRaisesRegex(SmokeError, "expected x86-64"):
                check_bundle_payload(package, "amd64", root)
            (root / "usr/bin/pi").write_text("#!/bin/sh\necho unsafe\n")
            (root / "usr/bin/pi").chmod(0o755)
            with self.assertRaisesRegex(SmokeError, "APT update helper"):
                check_bundle_payload(package, "arm64", root)

    def test_rejects_a_stale_helper_readme_and_package_json(self) -> None:
        package = next(pkg for pkg in load_manifest().packages if pkg.name == "pi")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            installed_bundle(root, "amd64")
            (root / "usr/lib/pi/apt-update").write_text("#!/bin/sh\napt upgrade pi\n")
            (root / "usr/lib/pi/apt-update").chmod(0o755)
            with self.assertRaisesRegex(SmokeError, "apt-update"):
                check_bundle_payload(package, "amd64", root)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            installed_bundle(root, "amd64")
            (root / "usr/lib/pi/package.json").write_text('{"name":"other","version":"1.5.0"}')
            with self.assertRaisesRegex(SmokeError, "package.json"):
                check_bundle_payload(package, "amd64", root)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            installed_bundle(root, "amd64")
            (root / "usr/share/doc/pi/README.source").write_text("stale", encoding="utf-8")
            with self.assertRaisesRegex(SmokeError, "README.source"):
                check_bundle_payload(package, "amd64", root)

    def test_version_output_is_the_exact_pinned_release(self) -> None:
        package = next(pkg for pkg in load_manifest().packages if pkg.name == "pi")
        check_version_output(package, "1.5.0\n")
        with self.assertRaisesRegex(SmokeError, "expected '1.5.0'"):
            check_version_output(package, "v1.5.0")


if __name__ == "__main__":
    unittest.main()
