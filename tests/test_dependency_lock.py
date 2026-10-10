"""Portability regressions for the committed dependency locks.

The Python lock and the npm install-script policy must keep builds working
without the private mirror: every locked artifact resolves from public PyPI
with unchanged versions and hashes, and the only permitted install scripts are
the exact, version-pinned registry packages the lock declares.
"""

import json
import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRIVATE_HOST = "cloud1ful"
EXPECTED_REGISTRY = "https://pypi.org/simple"
EXPECTED_ARTIFACT_PREFIX = "https://files.pythonhosted.org/"
# name -> (version, sorted sha256 hex digests) for the seven resolved packages.
EXPECTED_PYTHON_ARTIFACTS = {
    "boto3": ("1.43.111", [
        "5ae342a16c848909cd42d4be404f69d9082e5705460198d4d3327eca5f6cddcb",
        "c79994619c8d89e45f6fd0edc5c5b5a70c9358f00423f4c99cb64931f89ecf37",
    ]),
    "botocore": ("1.43.111", [
        "44d5e80962ac6cb9e85af72667b77c9586451e3328ab0ce33195380767e213d8",
        "f1f4c28cb2a096bf246d0bb24cbb1a01c5cb696ef499fa71b155adda7b94c90b",
    ]),
    "jmespath": ("1.1.0", [
        "472c87d80f36026ae83c6ddd0f1d05d4e510134ed462851fd5f754c8c3cbb88d",
        "a5663118de4908c91729bea0acadca56526eb2698e83de10cd116ae0f4e97c64",
    ]),
    "python-dateutil": ("2.9.0.post0", [
        "37dd54208da7e1cd875388217d5e00ebd4179249f90fb72437e91a35459a0ad3",
        "a8b2bc7bffae282281c8140a97d3aa9c14da0b136dfe83f850eea9a5f7470427",
    ]),
    "s3transfer": ("0.19.2", [
        "ba0309fd86be3c27dbf78cdd813c13c5e1df16e5874b99d2535ebbdfb9892993",
        "d8168eccca828cbb2cd573675333f3bddd254313a9c42494b84c76b539e8ba25",
    ]),
    "six": ("1.17.0", [
        "4721f391ed90541fddacab5acf947aa0d3dc7d27b2e1e8eda2be8970586c3274",
        "ff70335d468e7eb6ec65b95b99d3a2836546063f63acc5171de367e834932a81",
    ]),
    "urllib3": ("2.8.0", [
        "0cf3cae568d36aa9576b28dfb35f11328f1cb974ca7647d9475ebb86c75ac6e3",
        "63bf2ead4c879426ebf22ef2a781eeb4aa3b4ae798a0435506f8687fd5bb9b63",
    ]),
}
PINNED_ARTIFACT = re.compile(r"^[^@\s]+@\d+\.\d+")


def read_lock() -> dict:
    with (ROOT / "uv.lock").open("rb") as handle:
        return tomllib.load(handle)


def registry_packages(lock: dict) -> dict[str, dict]:
    return {
        package["name"]: package
        for package in lock.get("package", [])
        if package.get("source", {}).get("registry")
    }


def artifact_rows(package: dict) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    if "sdist" in package:
        rows.append((package["sdist"]["url"], package["sdist"]["hash"]))
    for wheel in package.get("wheels", []):
        rows.append((wheel["url"], wheel["hash"]))
    return rows


class LockPortabilityTest(unittest.TestCase):
    def test_every_lock_artifact_resolves_from_public_pypi(self) -> None:
        packages = registry_packages(read_lock())
        self.assertEqual(set(packages), set(EXPECTED_PYTHON_ARTIFACTS))
        for name, package in packages.items():
            self.assertEqual(package["source"]["registry"], EXPECTED_REGISTRY, msg=name)
            for url, digest in artifact_rows(package):
                self.assertTrue(url.startswith(EXPECTED_ARTIFACT_PREFIX), msg=url)
                self.assertTrue(digest.startswith("sha256:"), msg=url)

    def test_locked_versions_and_hashes_are_unchanged(self) -> None:
        packages = registry_packages(read_lock())
        for name, (version, hashes) in EXPECTED_PYTHON_ARTIFACTS.items():
            package = packages[name]
            self.assertEqual(package["version"], version, msg=name)
            observed = sorted(
                digest.removeprefix("sha256:") for _, digest in artifact_rows(package)
            )
            self.assertEqual(observed, sorted(hashes), msg=name)

    def test_no_private_mirror_reference_remains(self) -> None:
        for relative in (
            "uv.lock",
            "pyproject.toml",
            "package.json",
            "package-lock.json",
        ):
            text = (ROOT / relative).read_text(encoding="utf-8")
            self.assertNotIn(PRIVATE_HOST, text, msg=relative)

    def test_pyproject_declares_the_public_index(self) -> None:
        with (ROOT / "pyproject.toml").open("rb") as handle:
            project = tomllib.load(handle)
        indexes = project["tool"]["uv"]["index"]
        defaults = [index for index in indexes if index.get("default")]
        self.assertEqual(len(defaults), 1)
        self.assertEqual(defaults[0]["url"], EXPECTED_REGISTRY)


class InstallScriptPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        self.lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))

    def install_script_packages(self) -> dict[str, str]:
        # Optional deps that the platform skips (for example the darwin-only
        # fsevents) never run their install scripts, so they need no approval.
        return {
            path.split("node_modules/")[-1]: entry["version"]
            for path, entry in self.lock["packages"].items()
            if entry.get("hasInstallScript") and not entry.get("optional")
        }

    def test_allow_scripts_is_a_pinned_exact_map(self) -> None:
        policy = self.manifest["allowScripts"]
        self.assertEqual(
            policy,
            {"esbuild@0.28.2": True, "workerd@1.20261006.1": True},
        )
        for key, value in policy.items():
            self.assertIs(value, True, msg=key)
            self.assertRegex(key, PINNED_ARTIFACT, msg=key)

    def test_approved_versions_match_the_lockfile(self) -> None:
        approved = {
            name: version
            for name, _, version in (key.rpartition("@") for key in self.manifest["allowScripts"])
        }
        self.assertEqual(approved, self.install_script_packages())


if __name__ == "__main__":
    unittest.main()
