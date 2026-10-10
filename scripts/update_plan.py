"""Decide what an update run should build from the trusted baseline and discovery.

The plan never rebuilds everything and never trusts a version comparison it has
not validated: every candidate version must strictly advance the signed baseline,
every overlay is re-validated through the existing manifest parser, and an
unchanged plan stays empty so a second run has nothing left to do.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass

from .manifest import Manifest, parse_manifest
from .upstream import Candidate
from .upstream_assets import ARCHES, EXPECTED_LICENSES, version_key


@dataclass(frozen=True)
class UpdatePlan:
    """What one update run would do; `changed` empty means a no-op."""

    baseline_release_id: str | None
    baseline_manifest: Manifest
    manifest_data: dict[str, object]
    manifest: Manifest
    changed: tuple[str, ...]
    versions: dict[str, str]

    @property
    def is_noop(self) -> bool:
        return not self.changed

    @property
    def is_bootstrap(self) -> bool:
        """Fresh storage has no pool to keep, so every package is built."""
        return self.baseline_release_id is None


def overlay_package(entry: dict[str, object], candidate: Candidate) -> dict[str, object]:
    """Return one package entry with the candidate's version and arch targets.

    Description, copyright, and the per-arch archive member and binary name are
    carried over unchanged; only the version, the digests, and the tag-bound
    license URL move, and the result must re-validate through `parse_manifest`.
    """
    license_id = entry.get("license")
    if license_id != EXPECTED_LICENSES.get(candidate.name):
        raise ValueError(
            f"{candidate.name} license {license_id!r} is not the expected "
            f"{EXPECTED_LICENSES.get(candidate.name)!r}"
        )
    targets = entry.get("targets")
    if not isinstance(targets, dict):
        raise ValueError(f"{candidate.name} manifest entry has no targets object")
    updated_targets: dict[str, object] = {}
    for arch, target in targets.items():
        if not isinstance(target, dict):
            raise ValueError(f"{candidate.name} {arch} target is not an object")
        override = candidate.targets.get(arch)
        if override is None:
            raise ValueError(f"{candidate.name} has no discovered target for {arch}")
        updated_targets[arch] = {
            "url": override["url"],
            "sha256": override["sha256"],
            "archive_member": target["archive_member"],
            "binary": target["binary"],
        }
    return {
        **entry,
        "version": candidate.version,
        "license_url": candidate.license_url,
        "targets": updated_targets,
    }


def check_same_version_immutable(entry: dict[str, object], candidate: Candidate) -> None:
    """Refuse to treat an unchanged version as a no-op when its bytes changed.

    A published release is immutable, so a stable version whose upstream asset
    digest moved is not "already up to date": it is a republished tag, and
    rebuilding it would overwrite an existing .deb under a version clients have
    already installed. The run stops and names the architecture instead.
    """
    targets = entry.get("targets")
    if not isinstance(targets, dict):
        raise ValueError(f"{candidate.name} manifest entry has no targets object")
    for arch in ARCHES:
        published = targets.get(arch)
        if not isinstance(published, dict):
            raise ValueError(f"{candidate.name} baseline has no {arch} target")
        current_digest = str(published.get("sha256", "")).lower()
        if current_digest != candidate.targets[arch]["sha256"]:
            raise ValueError(
                f"{candidate.name} {candidate.version} {arch} upstream digest changed "
                f"({current_digest[:12]} -> {candidate.targets[arch]['sha256'][:12]}); "
                "a published release is immutable and a republished tag must not be "
                "silently treated as already up to date"
            )


def build_plan(
    *,
    baseline_release_id: str | None,
    baseline_data: dict[str, object],
    candidates: dict[str, Candidate],
) -> UpdatePlan:
    """Compare discovered stable versions with the trusted baseline."""
    if not candidates:
        raise ValueError("no upstream candidates were discovered")
    data = copy.deepcopy(baseline_data)
    baseline = parse_manifest(baseline_data)
    packages = list(data.get("packages") or [])
    if not isinstance(packages, list):
        raise ValueError("manifest packages must be a list")

    changed: list[str] = []
    versions: dict[str, str] = {}
    for entry in packages:
        if not isinstance(entry, dict):
            raise ValueError("manifest package entry must be an object")
        name = str(entry.get("name"))
        candidate = candidates.get(name)
        if candidate is None:
            raise ValueError(f"no stable upstream release discovered for {name}")
        if name not in {pkg.name for pkg in baseline.packages}:
            raise ValueError(f"manifest has no validated package entry for {name}")
        current = str(entry.get("version"))
        versions[name] = current
        if version_key(candidate.version) < version_key(current):
            raise ValueError(
                f"{name} latest stable {candidate.version} is older than the published "
                f"baseline {current}; refusing to downgrade an immutable release"
            )
        if version_key(candidate.version) == version_key(current):
            check_same_version_immutable(entry, candidate)
            continue
        rebuilt = overlay_package(entry, candidate)
        entry.clear()
        entry.update(rebuilt)
        changed.append(name)
    data["packages"] = packages
    return UpdatePlan(
        baseline_release_id=baseline_release_id,
        baseline_manifest=baseline,
        manifest_data=data,
        manifest=parse_manifest(data),
        changed=tuple(sorted(changed)),
        versions=versions,
    )
