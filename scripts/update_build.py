"""Apply an update plan: restore the published pool, rebuild only what changed.

Unchanged packages are restored byte-for-byte and never rebuilt, so a run that
adds three catalog packages builds exactly six new .deb files while the four
already published stay identical, and a later run that touches one package
produces two. A same-version architecture expansion rebuilds only the newly
reviewed target and keeps the published architecture's bytes. The result is
verified as a complete manifest-derived index before anything is uploaded, and
the pointer still moves last.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .build import build_all
from .manifest import Package, deb_filename
from .publish import publish_tree
from .release_build import build_release
from .release_verify import TrustedRelease
from .restore_packages import restore_pool
from .storage import Source
from .update_plan import UpdatePlan
from .verify import verify_tree


@dataclass(frozen=True)
class UpdateResult:
    """What a published update produced."""

    release_id: str
    debs: list[Path]
    uploaded: list[str]


def changed_targets(plan: UpdatePlan) -> set[tuple[str, str]]:
    """The (package, architecture) pairs this plan builds or drops.

    A package name in `changed` is not enough: a same-version architecture
    expansion changes the package while leaving its published targets exactly
    as they were, so cleanup and the build selection both work per target.
    """
    baseline_by = {pkg.name: pkg for pkg in plan.baseline_manifest.packages}
    final_by = {pkg.name: pkg for pkg in plan.manifest.packages}
    changed: set[tuple[str, str]] = set()
    for name in plan.changed:
        base = baseline_by.get(name)
        final = final_by[name]
        for arch, target in final.targets.items():
            if base is None or arch not in base.targets or base.targets[arch] != target:
                changed.add((name, arch))
        if base is not None:
            for arch in base.targets:
                if arch not in final.targets:
                    changed.add((name, arch))
    return changed


def remove_superseded(plan: UpdatePlan, pool_dir: Path) -> list[str]:
    """Drop the pool entries the overlay replaces; returns the removed names.

    The names come from the baseline manifest, because those are the files the
    published release actually contains; deriving them from the overlay would
    target the new files instead of the old ones. Only a target that actually
    changed is dropped, so a published architecture an expansion keeps still
    has its pool object.
    """
    baseline_by = {pkg.name: pkg for pkg in plan.baseline_manifest.packages}
    replaced = {
        deb_filename(baseline_by[name], arch, plan.baseline_manifest)
        for name, arch in changed_targets(plan)
        if name in baseline_by and arch in baseline_by[name].targets
    }
    removed: list[str] = []
    for name in sorted(replaced):
        stale = pool_dir / name
        if stale.is_file():
            stale.unlink()
            removed.append(name)
    return removed


def _build_subset(plan: UpdatePlan) -> tuple[Package, ...]:
    """The packages and targets this run has to build from scratch."""
    selected = changed_targets(plan)
    final_by = {pkg.name: pkg for pkg in plan.manifest.packages}
    subset: list[Package] = []
    for name in plan.changed:
        arches = sorted(arch for pkg, arch in selected if pkg == name)
        if not arches:
            continue
        pkg = final_by[name]
        subset.append(replace(pkg, targets={arch: pkg.targets[arch] for arch in arches}))
    return tuple(subset)


def apply_update(
    *,
    plan: UpdatePlan,
    release: TrustedRelease | None,
    source: Source,
    output_root: Path,
    cache_dir: Path,
    release_id: str,
    bucket: str,
    endpoint: str | None = None,
    fingerprint: str,
    signing_key_file: Path | None = None,
    key_id: str | None = None,
    client: Any | None = None,
) -> UpdateResult:
    """Restore, rebuild the changed targets, verify, then publish pointer last.

    A published baseline keeps its pool: the existing packages are restored and
    only the superseded targets are dropped, so unchanged entries stay
    byte-identical. A package the plan adds has no published predecessor, so
    nothing is removed for it. Fresh storage has no pool to keep and builds
    every package. The release metadata is always generated from the complete
    planned manifest.
    """
    if plan.is_bootstrap:
        if release is not None:
            raise ValueError("a bootstrap plan must not carry a published baseline")
        build_packages = plan.manifest.packages
    else:
        if release is None:
            raise ValueError("an incremental update requires a trusted baseline")
        restore_pool(release, source, output_root)
        remove_superseded(plan, output_root / "pool")
        build_packages = _build_subset(plan)

    debs = build_all(
        replace(plan.manifest, packages=build_packages),
        pool_dir=output_root / "pool",
        cache_dir=cache_dir,
    )
    build_release(
        output_root=output_root,
        release_id=release_id,
        cache_dir=cache_dir,
        manifest_document=plan.manifest_data,
        packages=(),
        signing_key_file=signing_key_file,
        key_id=key_id,
    )
    verify_tree(output_root, expected_fingerprint=fingerprint)
    uploaded = publish_tree(
        output_root=output_root,
        bucket=bucket,
        release_id=release_id,
        endpoint=endpoint,
        expected_fingerprint=fingerprint,
        client=client,
    )
    return UpdateResult(release_id=release_id, debs=debs, uploaded=uploaded)
