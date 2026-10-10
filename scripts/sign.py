"""GPG signing helpers using an isolated temporary homedir."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .common import run


def _gpg_env(homedir: Path) -> dict[str, str]:
    return {
        "GNUPGHOME": str(homedir),
        "GPG_AGENT_INFO": "",
    }


def import_key(homedir: Path, key_file: Path) -> None:
    proc = run(
        ["gpg", "--batch", "--import", str(key_file)],
        env=_gpg_env(homedir),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gpg import failed: {proc.stderr.strip()}")


def sign_release(*, release_file: Path, key_id: str | None = None) -> tuple[Path, Path]:
    """Create InRelease (clearsign) and Release.gpg (detached) beside Release."""
    homedir = Path(os.environ.get("GNUPGHOME", ""))
    if not homedir or not homedir.exists():
        raise RuntimeError("signing requires GNUPGHOME pointing at an isolated homedir")
    inrelease = release_file.parent / "InRelease"
    detached = release_file.parent / "Release.gpg"
    user_args: list[str] = []
    if key_id:
        user_args = ["--local-user", key_id]
    first = run(
        ["gpg", "--batch", "--yes", "--pinentry-mode", "loopback", *user_args,
         "--clearsign", "--digest-algo", "SHA512", "-o", str(inrelease), str(release_file)],
        env=_gpg_env(homedir),
    )
    if first.returncode != 0:
        raise RuntimeError(f"gpg clearsign failed: {first.stderr.strip()}")
    second = run(
        ["gpg", "--batch", "--yes", "--pinentry-mode", "loopback", *user_args,
         "--armor", "--detach-sign", "--digest-algo", "SHA512",
         "-o", str(detached), str(release_file)],
        env=_gpg_env(homedir),
    )
    if second.returncode != 0:
        raise RuntimeError(f"gpg detach-sign failed: {second.stderr.strip()}")
    return inrelease, detached


def verify_detached(*, release_file: Path, signature: Path, homedir: Path) -> None:
    proc = run(
        ["gpg", "--batch", "--verify", str(signature), str(release_file)],
        env=_gpg_env(homedir),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"signature verification failed: {proc.stderr.strip()}")


def verify_clearsign(*, inrelease: Path, homedir: Path) -> None:
    proc = run(
        ["gpg", "--batch", "--verify", str(inrelease)],
        env=_gpg_env(homedir),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"InRelease verification failed: {proc.stderr.strip()}")


def export_public_key(*, homedir: Path, key_id: str | None, dest: Path) -> None:
    args = ["gpg", "--batch", "--armor", "--export"]
    if key_id:
        args.append(key_id)
    proc = run(args, env=_gpg_env(homedir))
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(f"gpg export failed: {proc.stderr.strip()}")
    dest.write_text(proc.stdout, encoding="utf-8")


class IsolatedHomedir:
    """Context manager for a throwaway GPG homedir (mode 0700)."""

    def __init__(self) -> None:
        self._tmp: tempfile.TemporaryDirectory[str] | None = None
        self.path: Path | None = None

    def __enter__(self) -> Path:
        self._tmp = tempfile.TemporaryDirectory(prefix="apt-gpg-")
        self.path = Path(self._tmp.name)
        self.path.chmod(0o700)
        return self.path

    def __exit__(self, *exc: object) -> None:
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None
            self.path = None
