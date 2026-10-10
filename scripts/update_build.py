"""Apply an update plan: restore the published pool, rebuild only what changed.

The unchanged packages are restored byte-for-byte and never rebuilt, so a run
that touches one package produces exactly two new .deb files while the other two
pool entries stay identical. The result is verified as a complete four-entry
index before anything is uploaded, and the pointer still moves last.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .manifest import deb_filename
from .publish import publish_tree
from .release_build import build_release
from .restore import ARCHES, Source, TrustedRelease
from .restore_packages import restore_pool
from .update_plan import UpdatePlan
from .verify import verify_tree


@dataclass(frozen=True)
class UpdateResult:
    """What a published update produced."""

    release_id: str
    debs: list[Path]
    uploaded: list[str]


def remove_superseded(plan: UpdatePlan, pool_dir: Path) -> list[str]:
    """Drop the pool entries the overlay replaces; returns the removed names.

    The names come from the baseline manifest, because those are the files the
    published release actually contains; deriving them from the overlay would
    target the new files instead of the old ones.
    """
    replaced = {
        deb_filename(pkg, arch, plan.baseline_manifest)
        for pkg in plan.baseline_manifest.packages
        if pkg.name in plan.changed
        for arch in ARCHES
    }
    removed: list[str] = []
    for name in sorted(replaced):
        stale = pool_dir / name
        if stale.is_file():
            stale.unlink()
            removed.append(name)
    return removed


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
    """Restore, rebuild the changed packages, verify, then publish pointer last.

    A published baseline keeps its pool: the four current packages are restored
    and only the superseded ones are dropped, so the unchanged entries stay
    byte-identical. Fresh storage has no pool to keep and builds every package.
    """
    if plan.is_bootstrap:
        if release is not None:
            raise ValueError("a bootstrap plan must not carry a published baseline")
        selection = None
    else:
        if release is None:
            raise ValueError("an incremental update requires a trusted baseline")
        restore_pool(release, source, output_root)
        remove_superseded(plan, output_root / "pool")
        selection = plan.changed

    debs = build_release(
        output_root=output_root,
        release_id=release_id,
        cache_dir=cache_dir,
        manifest_document=plan.manifest_data,
        packages=selection,
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
