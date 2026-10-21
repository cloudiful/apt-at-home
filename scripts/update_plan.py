"""Decide what an update run should build from the trusted baseline and discovery.

The published baseline is authoritative for what exists: it is never edited by a
newer working catalog, and a catalog package that the baseline does not carry is
merged in as an addition rather than a replacement. Every candidate version must
strictly advance the entry it applies to, every overlay is re-validated through
the manifest parser, and an unchanged plan stays empty so a second run has
nothing left to do.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass

from .catalog import (
    additional_binaries_for,
    arches_for,
    archive_member,
    binary_name,
    conflicts_for,
    depends_for,
    expected_license_for,
    install_tree_for,
    replaces_for,
)
from .manifest import Manifest, parse_baseline_manifest, parse_manifest
from .upstream import Candidate
from .upstream_assets import version_key


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

    @property
    def added(self) -> tuple[str, ...]:
        """Catalog packages this run introduces to a published release."""
        return tuple(
            sorted(
                {pkg.name for pkg in self.manifest.packages}
                - {pkg.name for pkg in self.baseline_manifest.packages}
            )
        )


def merge_catalog(baseline_data: dict[str, object], catalog_data: dict[str, object]) -> list[dict[str, object]]:
    """Return baseline package entries plus catalog packages the baseline lacks.

    Order and content follow the baseline for everything it already carries, so
    a catalog that pins an older version can never overwrite a published newer
    one; only genuinely new names are appended, in catalog order.
    """
    baseline_entries = baseline_data.get("packages")
    catalog_entries = catalog_data.get("packages")
    if not isinstance(baseline_entries, list) or not isinstance(catalog_entries, list):
        raise ValueError("manifest packages must be a list")
    present = {
        entry.get("name") for entry in baseline_entries if isinstance(entry, dict)
    }
    merged = copy.deepcopy(baseline_entries)
    merged.extend(
        copy.deepcopy(entry)
        for entry in catalog_entries
        if isinstance(entry, dict) and entry.get("name") not in present
    )
    return merged


def _payload_source(name: str, version: str, arch: str) -> dict[str, object]:
    """Return the reviewed payload source and any additional command members."""
    tree = install_tree_for(name, version, arch)
    if tree:
        return {"install_tree": tree}
    source: dict[str, object] = {"archive_member": archive_member(name, version, arch)}
    additional = additional_binaries_for(name, version, arch)
    if additional:
        source["additional_binaries"] = additional
    return source


def _target_for(
    name: str, version: str, arch: str, override: dict[str, str]
) -> dict[str, object]:
    """Return one architecture's reviewed target from its discovered asset."""
    return {
        "url": override["url"],
        "sha256": override["sha256"],
        **_payload_source(name, version, arch),
        "binary": binary_name(name),
        "depends": depends_for(name, arch),
    }


def overlay_package(
    entry: dict[str, object], candidate: Candidate, *, preserve_targets: bool = False
) -> dict[str, object]:
    """Return one package entry at the candidate's version.

    The URL, digest, tag-bound license URL, archive member, installed binary, and
    declared dependencies are all recomputed from the reviewed recipe, so a
    nested member moves with the version and an architecture that gains a
    runtime dependency gains it here too. Only the reviewed architectures are
    emitted. With `preserve_targets`, a target the entry already declares is
    copied unchanged and only a missing reviewed architecture is added, which is
    how a same-version architecture expansion keeps published bytes. The result
    must re-validate through `parse_manifest`.
    """
    name = str(entry.get("name"))
    license_id = entry.get("license")
    expected = expected_license_for(name)
    if license_id != expected:
        raise ValueError(
            f"{name} license {license_id!r} is not the expected {expected!r}"
        )
    targets = entry.get("targets")
    if not isinstance(targets, dict):
        raise ValueError(f"{name} manifest entry has no targets object")
    version = candidate.version
    relations = {"conflicts": conflicts_for(name), "replaces": replaces_for(name)}
    updated_targets: dict[str, object] = {}
    for arch in arches_for(name):
        published = targets.get(arch)
        if preserve_targets and isinstance(published, dict):
            updated_targets[arch] = copy.deepcopy(published)
            continue
        override = candidate.targets.get(arch)
        if override is None:
            raise ValueError(f"{name} has no discovered target for {arch}")
        updated_targets[arch] = _target_for(name, version, arch, override)
    extras = {"extra_license_urls": list(candidate.extra_license_urls)}
    return {
        **{key: value for key, value in entry.items() if key not in relations},
        **{key: value for key, value in relations.items() if value},
        **(extras if candidate.extra_license_urls else {}),
        "version": version,
        "license_url": candidate.license_url,
        "targets": updated_targets,
    }


def check_same_version_immutable(entry: dict[str, object], candidate: Candidate) -> None:
    """Refuse to treat an unchanged version as a no-op when its bytes changed.

    A published release is immutable, so a stable version whose upstream asset
    digest moved is not "already up to date": it is a republished tag, and
    rebuilding it would overwrite an existing .deb under a version clients have
    already installed. The run stops and names the architecture instead.

    Every field a published target declares must still be exactly what the
    reviewed recipe derives for that target. A published baseline that predates
    one reviewed architecture asset may omit that target only; adding it later
    is an expansion, never a reason to rewrite an existing one.
    """
    if entry.get("license_url") != candidate.license_url:
        raise ValueError(
            f"{candidate.name} {candidate.version} license source changed; "
            "a published release is immutable"
        )
    extra_urls = entry.get("extra_license_urls", [])
    if not isinstance(extra_urls, list) or tuple(extra_urls) != candidate.extra_license_urls:
        raise ValueError(
            f"{candidate.name} {candidate.version} supplemental license sources changed; "
            "a published release is immutable"
        )
    targets = entry.get("targets")
    if not isinstance(targets, dict):
        raise ValueError(f"{candidate.name} manifest entry has no targets object")
    reviewed = arches_for(candidate.name)
    for arch in targets:
        if arch not in reviewed:
            raise ValueError(
                f"{candidate.name} baseline declares {arch}, which the reviewed "
                "recipe no longer publishes"
            )
    for arch in reviewed:
        published = targets.get(arch)
        if not isinstance(published, dict):
            if candidate.targets.get(arch) is None:
                raise ValueError(
                    f"{candidate.name} {candidate.version} {arch} has no discovered "
                    "upstream release asset"
                )
            continue
        override = candidate.targets.get(arch)
        if override is None:
            raise ValueError(
                f"{candidate.name} {candidate.version} {arch} has no discovered "
                "upstream release asset"
            )
        expected = _target_for(candidate.name, candidate.version, arch, override)
        for field, value in published.items():
            if expected.get(field) != value:
                raise ValueError(
                    f"{candidate.name} {candidate.version} {arch} {field} changed "
                    "in the published recipe; a published release is immutable and "
                    "a changed baseline target must not be silently replaced"
                )


def build_plan(
    *,
    baseline_release_id: str | None,
    baseline_data: dict[str, object],
    catalog_data: dict[str, object],
    candidates: dict[str, Candidate],
) -> UpdatePlan:
    """Compare discovered stable versions with the trusted baseline."""
    if not candidates:
        raise ValueError("no upstream candidates were discovered")
    data = copy.deepcopy(baseline_data)
    baseline = parse_baseline_manifest(baseline_data)
    catalog = parse_manifest(catalog_data)
    baseline_names = {pkg.name for pkg in baseline.packages}
    validated = baseline_names | {pkg.name for pkg in catalog.packages}
    packages = merge_catalog(data, catalog_data)

    changed: list[str] = []
    versions: dict[str, str] = {}
    for entry in packages:
        if not isinstance(entry, dict):
            raise ValueError("manifest package entry must be an object")
        name = str(entry.get("name"))
        candidate = candidates.get(name)
        if candidate is None:
            raise ValueError(f"no stable upstream release discovered for {name}")
        if name not in validated:
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
            published = entry.get("targets")
            if not isinstance(published, dict):
                raise ValueError(f"{name} manifest entry has no targets object")
            if any(arch not in published for arch in arches_for(name)):
                # The same upstream version gained a reviewed architecture: the
                # published targets keep their bytes and only the new one is
                # built, so the package is changed without rewriting history.
                rebuilt = overlay_package(entry, candidate, preserve_targets=True)
                entry.clear()
                entry.update(rebuilt)
                changed.append(name)
            elif name not in baseline_names:
                # A new package already pinned at its latest stable release still
                # has to be built: nothing published carries it yet.
                changed.append(name)
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
