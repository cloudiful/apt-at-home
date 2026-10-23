"""Decide whether CI must run the expensive package-build validation."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence

SHA_RE = re.compile(r"[0-9a-f]{40}")
ZERO_SHA = "0" * 40

GIT_DIFF_EVENTS = frozenset({"push", "pull_request"})

HEAVY_PREFIXES = ("packaging/", "scripts/", "tests/", ".github/")
HEAVY_PATHS = frozenset({"pyproject.toml", "uv.lock"})

LIGHT_PREFIXES = ("web/", "worker/", "docs/", "client/")
LIGHT_PATHS = frozenset(
    {
        "README.md",
        "README.zh-CN.md",
        "package.json",
        "package-lock.json",
        "bun.lock",
        "bun.lockb",
        "tsconfig.json",
        "vitest.config.ts",
        "components.json",
        ".gitignore",
    }
)


def path_is_heavy(path: str) -> bool:
    if path in HEAVY_PATHS:
        return True
    if path.startswith(HEAVY_PREFIXES):
        return True
    if path in LIGHT_PATHS:
        return False
    if path.startswith(LIGHT_PREFIXES):
        return False
    return True


def classify_paths(paths: Sequence[str]) -> bool:
    return any(path_is_heavy(path) for path in paths)


def _valid_sha(value: str) -> bool:
    return SHA_RE.fullmatch(value) is not None and value != ZERO_SHA


def _safe_path(raw: bytes) -> str | None:
    try:
        path = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not path or path.startswith("/") or "\\" in path:
        return None
    if any(part in ("", ".", "..") for part in path.split("/")):
        return None
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in path):
        return None
    return path


def changed_paths(base: str, head: str) -> list[str] | None:
    argv = ["git", "diff", "--no-renames", "--name-only", "-z", base, head, "--"]
    try:
        result = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    fields = result.stdout.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    if not fields:
        return None
    paths: list[str] = []
    for field in fields:
        path = _safe_path(field)
        if path is None:
            return None
        paths.append(path)
    return paths


def is_heavy(environ: Mapping[str, str]) -> bool:
    event = environ.get("CI_EVENT_NAME", "")
    if event == "workflow_dispatch":
        return True
    if event not in GIT_DIFF_EVENTS:
        return True
    base = environ.get("CI_BASE_SHA", "")
    head = environ.get("CI_HEAD_SHA", "")
    if not _valid_sha(base) or not _valid_sha(head):
        return True
    paths = changed_paths(base, head)
    if paths is None:
        return True
    return classify_paths(paths)


def main() -> int:
    heavy = is_heavy(os.environ)
    sys.stdout.write(f"heavy={'true' if heavy else 'false'}\n")
    print(f"ci scope: {'heavy' if heavy else 'light'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
