"""Refuse a manual release that would downgrade the published repository.

Automatic updates publish newer versions on their own, so the committed manifest
can fall behind what clients already have. A manual release must never move the
pointer backwards, and it must decide that from the *verified signed baseline*:
the versions come from the `Packages` stanzas whose bytes `restore` already
matched against the signed `Release`, never from a raw pointer or a filename
guess. With no published release at all there is nothing to downgrade, so a
first release is allowed.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from .fingerprint import primary_fingerprint_of_keyfile
from .manifest import Manifest
from .package_index import parse_stanzas
from .restore import (
    MAX_METADATA_BYTES,
    BaselineUnavailable,
    NoCurrentRelease,
    ObjectNotFound,
    Source,
    TrustedRelease,
    restore_metadata,
)
from .upstream_assets import version_key


def upstream_version(debian_version: str) -> str:
    """Strip the fixed Debian revision so upstream versions can be ordered.

    Packages carry the Debian version (`<upstream>-1`); the manifest pins the
    upstream version alone. Comparing the two directly would compare a revision
    against a version, so the trailing revision is removed first.
    """
    base, separator, _revision = debian_version.rpartition("-")
    if not separator or not base:
        raise ValueError(f"published version has no Debian revision: {debian_version!r}")
    return base


def published_versions(release: TrustedRelease) -> dict[str, str]:
    """Return each indexed package's Debian version from the verified indices.

    `restore` has already proved these bytes are the ones `Release` signed, so
    the stanzas are trustworthy even for a pre-binding release that carries no
    manifest. Both architectures must agree, since the same package is indexed
    twice and a disagreement means the baseline cannot be read.
    """
    versions: dict[str, str] = {}
    for arch, text in sorted(release.packages.items()):
        for stanza in parse_stanzas(text):
            name = stanza.fields.get("Package", "")
            version = stanza.fields.get("Version", "")
            if not name or not version:
                continue
            existing = versions.get(name)
            if existing is not None and existing != version:
                raise BaselineUnavailable(
                    f"published index disagrees on the {name} version across "
                    f"architectures ({existing} vs {version}); refusing to judge "
                    "the baseline against an ambiguous index"
                )
            versions[name] = version
    return versions


def check_not_older(candidate: Manifest, baseline: dict[str, str]) -> None:
    """Refuse any candidate package older than its published version."""
    for pkg in sorted(candidate.packages, key=lambda p: p.name):
        published = baseline.get(pkg.name)
        if published is None:
            # The package is new to the index; there is nothing to downgrade.
            continue
        if version_key(upstream_version(published)) > version_key(pkg.version):
            raise ValueError(
                f"{pkg.name} {pkg.version} is older than the published "
                f"{published}; a manual release must not downgrade what clients "
                "already have. Publish a build whose manifest is at least as new "
                "as the current release"
            )


def rotation_baseline_pin(source: Source) -> str:
    """Resolve the baseline verification pin from the current remote public key.

    During an authorized rotation the configured pin is already the *new* key
    while the published release was signed by the *old* one, so verifying the
    baseline against the configured pin can never succeed. For that case only,
    the baseline is verified against the remote `key.asc`'s own primary
    fingerprint: the old release is still cryptographically verified by the key
    that actually signed it, and the new candidate key is still required to match
    the configured pin by `publish_tree`. The downgrade guard below still runs
    unchanged, so a rotation can never bypass signature, payload, hash,
    manifest, or version checks.
    """
    try:
        key_bytes = source.get("key.asc", max_bytes=MAX_METADATA_BYTES)
    except ObjectNotFound as exc:
        raise BaselineUnavailable(
            "storage has no remote key.asc to establish a rotation baseline from"
        ) from exc
    with tempfile.TemporaryDirectory(prefix="apt-rotation-pin-") as scratch:
        key_file = Path(scratch) / "key.asc"
        key_file.write_bytes(key_bytes)
        try:
            return primary_fingerprint_of_keyfile(key_file)
        except ValueError as exc:
            raise BaselineUnavailable(
                f"remote key.asc cannot be used as a rotation baseline: {exc}"
            ) from exc


def guard_publish(
    *,
    candidate: Manifest,
    source: Source,
    fingerprint: str,
    allow_key_rotation: bool = False,
) -> str:
    """Check the candidate against the published baseline; returns what was checked.

    Raises `ValueError` on a downgrade and `BaselineUnavailable` when the remote
    cannot be trusted. An unreadable baseline is never treated as "nothing
    published": proving there is no downgrade requires a trustworthy baseline.
    """
    if allow_key_rotation:
        # Only an explicit rotation may establish the baseline pin from the
        # remote key instead of the configured (already-rotated) pin.
        fingerprint = rotation_baseline_pin(source)
    try:
        release = restore_metadata(source, fingerprint=fingerprint)
    except NoCurrentRelease:
        return "no published release; nothing to downgrade"
    baseline = published_versions(release)
    check_not_older(candidate, baseline)
    return f"published release {release.release_id}"