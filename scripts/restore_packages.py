"""Fetch published pool packages once the release metadata is proven authentic.

The pool is rebuilt from the signed `Packages` files rather than from a bucket
listing: each name comes from a verified stanza, must be a bare `.deb` basename,
and must match the size and digest that stanza declares.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .manifest import Manifest, deb_filename
from .package_index import check_arch_packages, parse_stanzas, pool_debs_for_arch
from .restore import (
    ARCHES,
    DEB_NAME,
    MAX_DEB_BYTES,
    BaselineUnavailable,
    ObjectNotFound,
    Source,
    TrustedRelease,
    sha256_bytes,
)


@dataclass(frozen=True)
class PoolEntry:
    """One package the signed index lists, with the identity it was signed for."""

    arch: str
    name: str
    size: int
    sha256: str


def pool_entries(release: TrustedRelease) -> list[PoolEntry]:
    """Return the deduplicated pool entries declared by the signed indices."""
    entries: dict[str, PoolEntry] = {}
    for arch, text in release.packages.items():
        for stanza in parse_stanzas(text):
            filename = stanza.fields.get("Filename", "")
            if not filename.startswith("pool/"):
                raise ValueError(f"Packages for {arch} lists a non-pool Filename: {filename!r}")
            name = filename[len("pool/"):]
            if not DEB_NAME.match(name):
                raise ValueError(f"Packages for {arch} lists an unusable pool name: {name!r}")
            if stanza.fields.get("Architecture") != arch:
                raise ValueError(
                    f"Packages for {arch} lists {stanza.fields.get('Package')} "
                    f"as {stanza.fields.get('Architecture')}"
                )
            entry = PoolEntry(
                arch=arch, name=name,
                size=int(stanza.fields.get("Size", "0") or 0),
                sha256=stanza.fields.get("SHA256", "").lower(),
            )
            if not entry.sha256 or entry.size <= 0:
                raise ValueError(f"Packages for {arch} omits size or digest for {name}")
            if name in entries:
                raise ValueError(f"duplicate pool entry across indices: {name}")
            entries[name] = entry
    if len(entries) != len(ARCHES) * 2:
        raise ValueError(f"published pool must hold four .deb files, found {len(entries)}")
    return [entries[name] for name in sorted(entries)]


def check_legacy_baseline(release: TrustedRelease, committed: Manifest) -> Manifest:
    """Accept a pre-binding release only when it matches the committed manifest exactly.

    Such a release carries no signed baseline, so it may start an update only
    where a human can audit the equality: every indexed name, version, revision,
    and architecture must equal `packaging/manifest.json`. Anything else stops
    the run with an actionable republish instruction instead of silently
    repairing an incomplete index.
    """
    if release.manifest is not None:
        return release.manifest
    indexed = {
        arch: {
            line.split("Filename: ", 1)[1].strip().removeprefix("pool/")
            for line in text.splitlines() if line.startswith("Filename: ")
        }
        for arch, text in release.packages.items()
    }
    expected = {
        arch: {deb_filename(pkg, arch, committed) for pkg in committed.packages}
        for arch in ARCHES
    }
    if indexed != expected:
        published = sorted(name for names in indexed.values() for name in names)
        raise BaselineUnavailable(
            "the published baseline predates manifest binding and does not match "
            f"packaging/manifest.json (published: {', '.join(published)}); publish one "
            "release from the current main with `python -m scripts.repo build && "
            "python -m scripts.repo verify && python -m scripts.repo publish` before "
            "automatic updates can run"
        )
    return committed


def restore_pool(release: TrustedRelease, source: Source, output_root: Path) -> list[Path]:
    """Fetch and verify every indexed package into `output_root/pool`."""
    pool_dir = output_root / "pool"
    pool_dir.mkdir(parents=True, exist_ok=True)
    for stale in pool_dir.glob("*.deb"):
        stale.unlink()
    restored: list[Path] = []
    for entry in pool_entries(release):
        if entry.size > MAX_DEB_BYTES:
            raise ValueError(
                f"{entry.name} declares {entry.size} bytes, over the {MAX_DEB_BYTES} limit"
            )
        try:
            payload = source.get(f"pool/{entry.name}", max_bytes=MAX_DEB_BYTES)
        except ObjectNotFound as exc:
            raise BaselineUnavailable(f"published pool is missing {entry.name}") from exc
        if len(payload) != entry.size:
            raise ValueError(f"{entry.name} size does not match the signed Packages entry")
        if sha256_bytes(payload) != entry.sha256:
            raise ValueError(f"{entry.name} digest does not match the signed Packages entry")
        destination = pool_dir / entry.name
        destination.write_bytes(payload)
        restored.append(destination)
    for arch in ARCHES:
        check_arch_packages(
            release.packages[arch],
            arch=arch,
            output_root=output_root,
            expected_filenames=pool_debs_for_arch(output_root, arch),
        )
    return restored
