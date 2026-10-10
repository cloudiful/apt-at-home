"""Fetch published pool packages once the release metadata is proven authentic.

The pool is rebuilt from the signed `Packages` files rather than from a bucket
listing: each name comes from a verified stanza, must be a bare `.deb` basename,
and must match the size and digest that stanza declares.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .common import sha256_bytes
from .manifest import deb_filename, debian_version, expected_debs_for_arch
from .metadata import ARCHES
from .package_index import check_arch_packages, parse_stanzas
from .release_verify import BaselineUnavailable, TrustedRelease
from .storage import DEB_NAME, MAX_DEB_BYTES, ObjectNotFound, Source


@dataclass(frozen=True)
class PoolEntry:
    """One package the signed index lists, with the identity it was signed for."""

    arch: str
    name: str
    size: int
    sha256: str


def _declared_identities(release: TrustedRelease) -> dict[str, tuple[str, str, str]]:
    """Map each pool filename the bound manifest declares to its identity."""
    return {
        deb_filename(pkg, arch, release.manifest): (
            pkg.name, debian_version(pkg, release.manifest), arch,
        )
        for pkg in release.manifest.packages
        for arch in ARCHES
    }


def pool_entries(release: TrustedRelease) -> list[PoolEntry]:
    """Return the pool entries the signed indices must list for this manifest.

    The bound manifest, not a file count, decides the inventory: a stanza that
    the manifest does not declare, that declares a different identity, or an
    index that omits a declared package is refused before a byte is fetched.
    """
    declared = _declared_identities(release)
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
            identity = (stanza.fields.get("Package", ""), stanza.fields.get("Version", ""), arch)
            expected_identity = declared.get(name)
            if expected_identity is None:
                raise ValueError(
                    f"Packages for {arch} lists {name}, which the bound manifest does not declare"
                )
            if identity != expected_identity:
                raise ValueError(
                    f"Packages for {arch} lists {name} as {' '.join(identity)}, "
                    f"but the bound manifest declares {' '.join(expected_identity)}"
                )
            entry = PoolEntry(arch=arch, name=name,
                              size=int(stanza.fields.get("Size", "0") or 0),
                              sha256=stanza.fields.get("SHA256", "").lower())
            if not entry.sha256 or entry.size <= 0:
                raise ValueError(f"Packages for {arch} omits size or digest for {name}")
            if name in entries:
                raise ValueError(f"duplicate pool entry across indices: {name}")
            entries[name] = entry
    missing = sorted(set(declared) - set(entries))
    if missing:
        raise ValueError(
            f"published indices omit manifest package(s): {', '.join(missing)}"
        )
    return [entries[name] for name in sorted(entries)]


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
            expected_filenames=expected_debs_for_arch(release.manifest, arch),
        )
    return restored
