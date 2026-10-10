"""Shared test helpers: ephemeral GPG keys and an in-memory S3 fake."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path


def make_gpg_home_with_key() -> tuple[tempfile.TemporaryDirectory[str], Path, str]:
    """Generate one ephemeral RSA key; returns (tmpdir, homedir, key_id)."""
    tmpdir = tempfile.TemporaryDirectory(prefix="apt-test-gpg-")
    home = Path(tmpdir.name) / "gnupg"
    home.mkdir(mode=0o700)
    batch = (
        "%no-protection\n"
        "Key-Type: RSA\n"
        "Key-Length: 2048\n"
        "Subkey-Type: RSA\n"
        "Subkey-Length: 2048\n"
        "Name-Real: apt-at-home test\n"
        "Name-Email: test@apt-at-home.local\n"
        "Expire-Date: 0\n"
        "%commit\n"
    )
    batch_file = Path(tmpdir.name) / "batch"
    batch_file.write_text(batch, encoding="utf-8")
    env = {"GNUPGHOME": str(home), "GPG_AGENT_INFO": "", "PATH": __import__("os").environ["PATH"]}
    proc = subprocess.run(
        ["gpg", "--batch", "--generate-key", str(batch_file)],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        tmpdir.cleanup()
        raise RuntimeError(f"test key generation failed: {proc.stderr}")
    list_proc = subprocess.run(
        ["gpg", "--batch", "--list-keys", "--with-colons"],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    key_id = ""
    for line in list_proc.stdout.splitlines():
        if line.startswith("fpr:"):
            key_id = line.split(":")[9]
            break
    if not key_id:
        tmpdir.cleanup()
        raise RuntimeError("could not read test key fingerprint")
    return tmpdir, home, key_id


class FakeS3:
    """Minimal dict-backed S3 double with boto3-like errors."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.puts: list[str] = []

    def _not_found(self, key: str) -> Exception:
        return Exception(f"An error occurred (404) Not Found: {key}")

    def head_object(self, Bucket: str, Key: str) -> dict[str, object]:
        if (Bucket, Key) not in self.objects:
            raise self._not_found(Key)
        return {"ContentLength": len(self.objects[(Bucket, Key)])}

    def get_object(self, Bucket: str, Key: str) -> dict[str, object]:
        if (Bucket, Key) not in self.objects:
            raise self._not_found(Key)

        data = self.objects[(Bucket, Key)]

        class Body:
            def __init__(self, payload: bytes) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload

        return {"Body": Body(data)}

    def put_object(self, Bucket: str, Key: str, Body: bytes | str, ContentType: str = "") -> dict[str, object]:
        if isinstance(Body, str):
            Body = Body.encode("utf-8")
        self.objects[(Bucket, Key)] = bytes(Body)
        self.puts.append(Key)
        return {}
