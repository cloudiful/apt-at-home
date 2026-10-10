"""Full primary-fingerprint helpers for the repository trust anchor."""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

_FINGERPRINT = re.compile(r"^[0-9A-F]{40}$")


def normalize_fingerprint(value: str) -> str:
    """Normalize whitespace/case; require a full 40-hex primary fingerprint."""
    cleaned = re.sub(r"\s+", "", value).upper()
    if not _FINGERPRINT.match(cleaned):
        raise ValueError(
            "APT_SIGNING_FINGERPRINT must be a full 40-hex primary fingerprint"
        )
    return cleaned


def expected_fingerprint_from_env(env_var: str = "APT_SIGNING_FINGERPRINT") -> str:
    """Read and normalize the production pin; missing/malformed fails."""
    import os

    raw = os.environ.get(env_var, "")
    if not raw or not raw.strip():
        raise ValueError(
            f"{env_var} is required for production verification and publish"
        )
    return normalize_fingerprint(raw)


def primary_fingerprint_of_keyfile(key_file: Path) -> str:
    """Return the single primary fingerprint contained in an armored key file."""
    if not key_file.exists():
        raise ValueError(f"signing key not found: {key_file}")
    with tempfile.TemporaryDirectory(prefix="apt-fpr-") as tmp:
        home = Path(tmp)
        home.chmod(0o700)
        env = {"GNUPGHOME": str(home), "GPG_AGENT_INFO": ""}
        import os

        merged = dict(os.environ)
        merged.update(env)
        proc = subprocess.run(
            ["gpg", "--batch", "--import", str(key_file)],
            env=merged,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if proc.returncode != 0:
            raise ValueError(f"gpg import failed: {proc.stderr.strip()}")
        listed = subprocess.run(
            ["gpg", "--batch", "--with-colons", "--list-keys"],
            env=merged,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if listed.returncode != 0:
            raise ValueError(f"gpg list-keys failed: {listed.stderr.strip()}")
        # First pub/fpr pair is the primary; subkey fprs must not satisfy the pin.
        lines = listed.stdout.splitlines()
        primary: str | None = None
        primaries: list[str] = []
        awaiting_fpr = False
        for line in lines:
            parts = line.split(":")
            if parts[0] == "pub":
                awaiting_fpr = True
            elif parts[0] == "fpr" and awaiting_fpr:
                fingerprint = (parts[9] if len(parts) > 9 else "").upper()
                if fingerprint:
                    primaries.append(fingerprint)
                    if primary is None:
                        primary = fingerprint
                awaiting_fpr = False
            elif parts[0] in ("sub", "sec", "ssb"):
                awaiting_fpr = False
        if primary is None or len(primaries) != 1:
            raise ValueError("key file must contain exactly one primary key")
        if not _FINGERPRINT.match(primary):
            raise ValueError("key file primary fingerprint is malformed")
        return primary


def check_key_matches_pin(*, key_file: Path, expected: str) -> str:
    """Normalize the pin and require the key file primary to equal it."""
    wanted = normalize_fingerprint(expected)
    actual = primary_fingerprint_of_keyfile(key_file)
    if actual != wanted:
        raise ValueError("signing key fingerprint mismatch")
    return actual
