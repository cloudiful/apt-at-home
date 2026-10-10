"""Verify a built repository tree: hashes, completeness, and signatures."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .common import sha256_file, sha512_file
from .fingerprint import check_key_matches_pin
from .package_index import check_arch_packages, pool_debs_for_arch
from .sign import IsolatedHomedir, import_key, verify_clearsign, verify_detached

ARCHES = ("amd64", "arm64")


def _parse_release_hashes(release_text: str, section: str) -> dict[str, tuple[str, str, int]]:
    out: dict[str, tuple[str, str, int]] = {}
    in_section = False
    for line in release_text.splitlines():
        if line.strip() == f"{section}:":
            in_section = True
            continue
        if in_section:
            if line and not line.startswith(" "):
                break
            match = re.match(r"^\s+([0-9a-f]+)\s+(\d+)\s+(\S+)\s*$", line)
            if match:
                digest, size, name = match.groups()
                out[name] = (digest, section, int(size))
    return out


def verify_tree(
    output_root: Path,
    *,
    key_file: Path | None = None,
    expected_fingerprint: str | None = None,
) -> list[str]:
    """Verify the current release tree; returns human-readable findings."""
    findings: list[str] = []
    pointer = output_root / "current.json"
    if not pointer.exists():
        raise ValueError("current.json is missing; run build first")
    try:
        release_id = json.loads(pointer.read_text(encoding="utf-8"))["release"]
    except Exception as exc:
        raise ValueError(f"current.json is invalid: {exc}") from exc
    if not isinstance(release_id, str) or not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", release_id):
        raise ValueError(f"invalid release id in current.json: {release_id!r}")

    dists = output_root / "releases" / release_id / "dists" / "stable"
    release_file = dists / "Release"
    inrelease = dists / "InRelease"
    detached = dists / "Release.gpg"
    for required in (release_file, inrelease, detached):
        if not required.exists():
            raise ValueError(f"missing required metadata file: {required.relative_to(output_root)}")
    findings.append(f"release {release_id} metadata present")

    release_text = release_file.read_text(encoding="utf-8")
    for token in ("Origin:", "Suite: stable", "Codename: stable", "Architectures: amd64 arm64",
                  "Components: main", "Acquire-By-Hash: yes"):
        if token not in release_text:
            raise ValueError(f"Release missing required field: {token}")
    if "Valid-Until" in release_text:
        raise ValueError("Release must not carry Valid-Until (deliberate personal-repo tradeoff)")
    findings.append("Release header fields valid")

    sha256_entries = _parse_release_hashes(release_text, "SHA256")
    sha512_entries = _parse_release_hashes(release_text, "SHA512")
    if not sha256_entries or not sha512_entries:
        raise ValueError("Release must carry SHA256 and SHA512 sections")

    # Every entry must reference a local Packages file and match its bytes.
    for rel, (digest, _, size) in sorted(sha256_entries.items()):
        if "Packages" not in rel:
            raise ValueError(f"Release SHA256 entry must reference Packages files, got: {rel}")
        candidate = dists / rel if not rel.startswith("dists/") else output_root / "releases" / release_id / rel
        # Entries are stored relative to dists/stable (e.g. main/binary-amd64/Packages).
        alt = dists / rel
        target = alt if alt.exists() else candidate
        if not target.exists():
            raise ValueError(f"Release references missing file: {rel}")
        if target.stat().st_size != size:
            raise ValueError(f"size mismatch for {rel}")
        if sha256_file(target) != digest.lower():
            raise ValueError(f"SHA256 mismatch for {rel}")
        other = sha512_entries.get(rel)
        if other is None:
            raise ValueError(f"SHA512 entry missing for {rel}")
        if sha512_file(target) != other[0].lower():
            raise ValueError(f"SHA512 mismatch for {rel}")
    findings.append(f"verified {len(sha256_entries)} hashed metadata files")

    # Each Packages file must list exactly the pool .debs of its own architecture.
    pool_files = {p.name: p for p in (output_root / "pool").glob("*.deb")}
    if len(pool_files) != 4:
        raise ValueError(f"pool must hold exactly four .deb files, found {len(pool_files)}")
    for arch in ARCHES:
        packages_path = dists / "main" / f"binary-{arch}" / "Packages"
        if not packages_path.exists():
            raise ValueError(f"missing Packages for {arch}")
        count = check_arch_packages(
            packages_path.read_text(encoding="utf-8"),
            arch=arch,
            output_root=output_root,
            expected_filenames=pool_debs_for_arch(output_root, arch),
        )
        findings.append(f"Packages for {arch} lists all {count} pool packages")

    # By-hash indices must match the served Packages bytes.
    for arch in ARCHES:
        for name in ("Packages", "Packages.gz"):
            served = dists / "main" / f"binary-{arch}" / name
            digest = sha256_file(served)
            indexed = output_root / "indices" / arch / "SHA256" / digest
            if not indexed.exists():
                raise ValueError(f"missing by-hash index for {arch} {name}: {digest}")
            if indexed.read_bytes() != served.read_bytes():
                raise ValueError(f"by-hash content mismatch for {arch} {name}")
    findings.append("by-hash indices match served metadata")

    # Signature verification uses an isolated homedir only.
    key_source = key_file or (output_root / "key.asc")
    if not key_source.exists():
        raise ValueError(f"signing key not found: {key_source}")
    if expected_fingerprint is not None:
        # Full primary pin required in production; local ephemeral builds pass
        # None and skip this gate. A wrong, malformed, or subkey-only value
        # fails here before any trust is placed in the signatures.
        check_key_matches_pin(key_file=key_source, expected=expected_fingerprint)
        findings.append("signing key matches trusted primary fingerprint")
    with IsolatedHomedir() as home:
        import_key(home, key_source)
        verify_detached(release_file=release_file, signature=detached, homedir=home)
        verify_clearsign(inrelease=inrelease, homedir=home)
    findings.append("InRelease and Release.gpg signatures valid")
    return findings
