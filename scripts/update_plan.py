"""Decide what an update run should build from the trusted baseline and discovery.

The published baseline is authoritative for what exists: it is never edited by a
newer working catalog, and a catalog package that the baseline does not carry is
merged in as an addition rather than a replacement. Every candidate version must
strictly advance the entry it applies to, every overlay is re-validated through
the manifest parser, and an unchanged plan stays empty so a second run has
nothing left to do. A publication instant known by more than one same-version
input must agree across all of them, so precedence never hides a conflict.
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
    """What one update run would do; nothing to change means a no-op.

    `changed` names the packages whose targets this run rebuilds, and
    `metadata_changed` names the packages whose publication instant this run
    fills in. A run that changes neither is a no-op.
    """

    baseline_release_id: str | None
    baseline_manifest: Manifest
    manifest_data: dict[str, object]
    manifest: Manifest
    changed: tuple[str, ...]
    versions: dict[str, str]
    metadata_changed: tuple[str, ...] = ()

    @property
    def is_noop(self) -> bool:
        return not self.changed and not self.metadata_changed

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


def _published_at(entry: dict[str, object]) -> str:
    """The publication instant a manifest entry declares, or the empty unknown."""
    value = entry.get("upstream_published_at", "")
    return value if isinstance(value, str) else ""


def merge_catalog(baseline_data: dict[str, object], catalog_data: dict[str, object]) -> list[dict[str, object]]:
    """Return baseline package entries plus catalog packages the baseline lacks.

    Order and content follow the baseline for everything it already carries, so
    a catalog that pins an older version can never overwrite a published newer
    one; only genuinely new names are appended, in catalog order.

    A baseline entry that carries no publication instant borrows the catalog's
    one, but only for the very same version: a date belongs to one release, so
    a catalog entry for another version never dates a published release. An
    instant both sides know must already agree: `build_plan` refuses a
    disagreement before this merge runs, so the copy that keeps baseline
    precedence can never silently drop a conflicting pinned value.
    """
    baseline_entries = baseline_data.get("packages")
    catalog_entries = catalog_data.get("packages")
    if not isinstance(baseline_entries, list) or not isinstance(catalog_entries, list):
        raise ValueError("manifest packages must be a list")
    present = {
        entry.get("name") for entry in baseline_entries if isinstance(entry, dict)
    }
    merged = copy.deepcopy(baseline_entries)
    catalog_by_name = {
        entry.get("name"): entry
        for entry in catalog_entries
        if isinstance(entry, dict)
    }
    for entry in merged:
        if not isinstance(entry, dict) or _published_at(entry):
            continue
        other = catalog_by_name.get(entry.get("name"))
        if not isinstance(other, dict):
            continue
        if str(other.get("version")) != str(entry.get("version")):
            continue
        published_at = _published_at(other)
        if published_at:
            entry["upstream_published_at"] = published_at
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

    The publication instant follows the version: a new version takes the
    candidate's date and drops the old one when the candidate knows none,
    because a date belongs to exactly one release. A preserved version keeps
    the date the entry already carries.
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
    published_at = (
        _published_at(entry) if preserve_targets else candidate.upstream_published_at
    )
    result = {
        **{key: value for key, value in entry.items() if key not in relations},
        **{key: value for key, value in relations.items() if value},
        **(extras if candidate.extra_license_urls else {}),
        "version": version,
        "license_url": candidate.license_url,
        "targets": updated_targets,
    }
    if published_at:
        result["upstream_published_at"] = published_at
    else:
        result.pop("upstream_published_at", None)
    return result


def check_same_version_immutable(entry: dict[str, object], candidate: Candidate) -> None:
    """Refuse to treat an unchanged version as a no-op when its bytes changed.

    A published release is immutable, so a stable version whose upstream asset
    digest moved is not "already up to date": it is a republished tag, and
    rebuilding it would overwrite an existing .deb under a version clients have
    already installed. The run stops and names the architecture instead.

    Every field a published target declares must still be exactly what the
    reviewed recipe derives for that target. A published baseline that predates
    one reviewed architecture asset may omit that target only; adding it later
    is an expansion, never a reason to rewrite an existing one. A published
    publication instant is immutable for the same reason: a release that was
    published at one instant cannot be republished at another.
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
    published_at = _published_at(entry)
    candidate_at = candidate.upstream_published_at
    if published_at and candidate_at and published_at != candidate_at:
        raise ValueError(
            f"{candidate.name} {candidate.version} upstream release date changed; "
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


def check_known_same_version_dates(
    name: str, version: str, *known: tuple[str, str]
) -> None:
    """Refuse two different known publication instants for one release.

    The committed catalog, the signed baseline, and the discovered candidate all
    describe the same upstream release, so any of them that knows an instant for
    this version must agree with the others. The check runs before the merge can
    drop one of them: merging keeps the baseline's value for precedence, and
    that precedence must never hide a disagreement about when the release was
    published. Each entry is `(source, value)` so a refusal names which inputs
    disagreed.
    """
    seen: dict[str, list[str]] = {}
    for source, value in known:
        if value:
            seen.setdefault(value, []).append(source)
    if len(seen) > 1:
        detail = ", ".join(
            f"{'/'.join(sources)} {value}" for value, sources in sorted(seen.items())
        )
        raise ValueError(
            f"{name} {version} has conflicting upstream release dates: {detail}; "
            "a published release is immutable"
        )


def known_dates_by_version(
    baseline: Manifest,
    catalog: Manifest,
    candidates: dict[str, Candidate],
    *,
    names: set[str],
) -> dict[tuple[str, str], list[tuple[str, str]]]:
    """Every known publication instant, grouped by its own package version.

    A date belongs to exactly one release, so only instants that share a
    `(package, version)` may be compared: a baseline left behind at an older
    version never constrains the date of the version the catalog and the
    candidate now describe. Grouping here, before the merge rewrites an entry,
    keeps every input pair independently comparable.
    """
    groups: dict[tuple[str, str], list[tuple[str, str]]] = {}

    def record(source: str, name: str, version: str, value: str) -> None:
        if name not in names:
            return
        groups.setdefault((name, version), []).append((source, value))

    for pkg in baseline.packages:
        record("baseline", pkg.name, pkg.version, pkg.upstream_published_at)
    for pkg in catalog.packages:
        record("catalog", pkg.name, pkg.version, pkg.upstream_published_at)
    for name, candidate in candidates.items():
        record("candidate", name, candidate.version, candidate.upstream_published_at)
    return groups


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
    # All three inputs are still distinct here, so every equality the contract
    # requires is checked before any of them is merged away.
    for (name, version), known in sorted(
        known_dates_by_version(baseline, catalog, candidates, names=validated).items()
    ):
        check_known_same_version_dates(name, version, *known)
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
            if not _published_at(entry) and candidate.upstream_published_at:
                # The published release carries no instant yet: the candidate's
                # known date backfills it without touching a single target.
                entry["upstream_published_at"] = candidate.upstream_published_at
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
    baseline_dates = {pkg.name: pkg.upstream_published_at for pkg in baseline.packages}
    rebuilt_names = set(changed)
    metadata_changed = tuple(
        sorted(
            str(entry.get("name"))
            for entry in packages
            if isinstance(entry, dict)
            and str(entry.get("name")) not in rebuilt_names
            and _published_at(entry) != baseline_dates.get(str(entry.get("name")), "")
        )
    )
    return UpdatePlan(
        baseline_release_id=baseline_release_id,
        baseline_manifest=baseline,
        manifest_data=data,
        manifest=parse_manifest(data),
        changed=tuple(sorted(changed)),
        versions=versions,
        metadata_changed=metadata_changed,
    )
