"""Offline, sequential real-artifact verification for the candidate review."""

import hashlib
import shutil
import subprocess
import types
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts import build as production
from scripts import bun_candidate as candidate
from scripts import bun_candidate_smoke as smoke
from scripts.common import sha256_file
from scripts.deb import build_deb, stage_package
from scripts.manifest import load_manifest

ROOT = Path("dist/bun-candidate/reviewer-r2").resolve()
CACHE = Path("dist/cache").resolve()
BASELINE = "94656f53fdf0ecd3e23bc34da967489f23470a9f"


def remove_scratch(path):
    assert path.is_relative_to(ROOT)
    if path.exists():
        shutil.rmtree(path)


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    baseline = types.ModuleType("scripts._review_baseline_deb")
    baseline.__package__ = "scripts"
    text = subprocess.check_output([
        "git", "show", f"{BASELINE}:scripts/deb.py",
    ], text=True)
    exec(compile(text, "baseline:scripts/deb.py", "exec"), baseline.__dict__)
    manifest = load_manifest()
    assert len(manifest.packages) == 9
    compared = 0
    for pkg in sorted(manifest.packages, key=lambda item: item.name):
        for arch in ("amd64", "arm64"):
            asset = CACHE / f"{pkg.name}-{pkg.version}-{arch}.tar.gz"
            assert sha256_file(asset) == pkg.targets[arch].sha256
            hashes = []
            for stage_function in (baseline.stage_package, stage_package):
                scratch = ROOT / "production-scratch"
                remove_scratch(scratch)
                scratch.mkdir()
                with patch.object(production, "stage_package", stage_function):
                    stage = production._stage_target(pkg, arch, manifest, asset, scratch)
                deb = scratch / "verified.deb"
                build_deb(stage, deb)
                hashes.append(sha256_file(deb))
                remove_scratch(scratch)
            assert hashes[0] == hashes[1], (pkg.name, arch, hashes)
            compared += 1
            print("PRODUCTION IDENTICAL", pkg.name, arch, hashes[0], flush=True)
    assert compared == 18
    descriptor = candidate.load_candidate()
    cache = candidate.default_cache()
    assets = {}
    for arch in candidate.ARCHES:
        target = descriptor.package.targets[arch]
        assets[arch] = cache / target.url.rsplit("/", 1)[-1]
        assert sha256_file(assets[arch]) == target.sha256
        assert assets[arch].stat().st_size == descriptor.sizes[arch]
    work = ROOT / "candidate-cache"
    output = ROOT / "candidate-output"
    with patch.object(candidate, "fetch_asset", side_effect=lambda _, arch, __: assets[arch]):
        first = candidate.build(output, candidate.ARCHES, descriptor, cache=work)
        hashes = [sha256_file(path) for path in first]
        second = candidate.build(output, candidate.ARCHES, descriptor, cache=work)
        assert hashes == [sha256_file(path) for path in second]
    for deb, arch, digest in zip(second, candidate.ARCHES, hashes):
        binary = work / "work" / f"payload-{arch}" / "bun"
        with zipfile.ZipFile(assets[arch]) as archive:
            expected = hashlib.sha256(archive.read(
                descriptor.package.targets[arch].archive_member,
            )).hexdigest()
        assert sha256_file(binary) == expected
        stage = work / "work" / "stage" / f"bun_{arch}"
        assert stage.exists(), stage
        doc = stage / "usr/share/doc/bun"
        paths = candidate.Paths(candidate.repo_root())
        for installed, source in candidate.NOTICE_FILES.items():
            assert (doc / installed).read_bytes() == (paths.licenses / source).read_bytes()
        assert (doc / "README.source").read_bytes() == paths.readme_source.read_bytes()
        assert sha256_file(Path("dist/bun-candidate") / deb.name) == digest
        print("CANDIDATE IDENTICAL", arch, digest, flush=True)
        print(smoke.smoke(stage / "usr/bin", arch, "1.4.3", allow_execution=False))
        if arch == smoke.native_arch():
            print(smoke.smoke(stage / "usr/bin", arch, "1.4.3", require_native=True))
    remove_scratch(work)
    print("PASS: 18 real production baseline comparisons; candidate repeat, copies and native checks")


if __name__ == "__main__":
    main()
