"""Shared fixture for driving the committed ``publish.yml`` run blocks.

The workflow's shell is extracted from the committed YAML and executed against
stub ``uv``/``gpg`` binaries in an isolated fixture, so tests for it assert what
each effective mode actually ran rather than what a document says. GitHub runs a
container job's ``run`` steps with ``sh`` unless a shell is declared, so the
fixture honors the configured shell (job ``defaults.run.shell``, or a step-level
override) instead of assuming the host's own default.

No network, object store, or private key is involved.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "publish.yml"

STEP_SPLIT_RE = re.compile(r"(?m)^(      - )")
RUN_BLOCK_RE = re.compile(r"run: \|\n(?P<body>(?: {10,}[^\n]*\n)+)")
ARG_SEP = b"\x01"

RESOLVE = "Resolve the effective publication mode"
IMPORT = "Import signing key into an isolated homedir"
UPSTREAM = "Update to the latest stable upstream releases"
PREVIEW = "Build an unsigned manifest preview"
MANIFEST = "Build, verify, and publish the committed manifest"

# GitHub runs a container job's `run` steps with `sh` unless a shell is
# declared, so the fixture must honor the configured shell instead of assuming
# the host's own default.
CONTAINER_DEFAULT_SHELL = "sh"
JOB_DEFAULT_SHELL_RE = re.compile(r"(?m)^    defaults:\n      run:\n        shell: (.+)$")
STEP_SHELL_RE = re.compile(r"(?m)^        shell: (.+)$")

STUB_UV = """#!/usr/bin/env bash
set -euo pipefail
{
  for arg in "$@"; do printf '%s\\0' "$arg"; done
  printf '\\001'
} >>"${UV_STUB_LOG:?}"
if [ "${!#}" = "--count" ]; then echo 0; fi
"""

STUB_GPG = "#!/usr/bin/env bash\nexit 0\n"


def step_blocks(text: str) -> list[str]:
    body = text.split("\n    steps:\n", 1)[1]
    parts = STEP_SPLIT_RE.split(body)
    return [parts[i] + parts[i + 1] for i in range(1, len(parts), 2)]


def step_name(block: str) -> str | None:
    match = re.search(r"(?m)^      - name: (.+)$", block)
    return match.group(1).strip() if match else None


def job_default_shell(text: str) -> str:
    """The job's declared `defaults.run.shell`, or the container default."""
    match = JOB_DEFAULT_SHELL_RE.search(text)
    return match.group(1).strip() if match else CONTAINER_DEFAULT_SHELL


def effective_shell(text: str, block: str) -> str:
    """The shell the runner would use for this step: step override, else job default."""
    override = STEP_SHELL_RE.search(block)
    return override.group(1).strip() if override else job_default_shell(text)


def run_body(block: str) -> str:
    match = RUN_BLOCK_RE.search(block)
    if match is None:
        raise AssertionError(f"no run body in step: {block[:40]}")
    return textwrap.dedent(match.group("body"))


class PublishWorkflowCase(unittest.TestCase):
    """Drive the committed shell blocks with stub binaries in an isolated tree."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="apt-publish-wf-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name, body in (("uv", STUB_UV), ("gpg", STUB_GPG)):
            stub = self.bin / name
            stub.write_text(body, encoding="utf-8")
            stub.chmod(0o755)
        self.log = self.root / "uv.log"
        self.log.write_bytes(b"")
        self.github_env = self.root / "github-env"
        self.github_env.write_text("", encoding="utf-8")
        self.runner_temp = self.root / "runner-temp"
        self.runner_temp.mkdir()
        # An empty pool makes the manifest count assertion compare 0 with the
        # stub's 0 rather than failing on a missing directory.
        (self.root / "dist" / "repo" / "pool").mkdir(parents=True)
        self.workflow = WORKFLOW.read_text(encoding="utf-8")
        self.steps = step_blocks(self.workflow)

    def block(self, name: str) -> str:
        for block in self.steps:
            if step_name(block) == name:
                return block
        raise AssertionError(f"workflow has no step named {name!r}")

    def run_block(self, name: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        full = {
            "PATH": f"{self.bin}:{os.environ.get('PATH', '')}",
            "HOME": str(self.root),
            "UV_STUB_LOG": str(self.log),
            "GITHUB_ENV": str(self.github_env),
            "GITHUB_EVENT_NAME": "",
            "RUNNER_TEMP": str(self.runner_temp),
        }
        full.update(env)
        block = self.block(name)
        return subprocess.run(
            [effective_shell(self.workflow, block), "-c", run_body(block)],
            cwd=self.root,
            env=full,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def uv_calls(self) -> list[tuple[str, ...]]:
        data = self.log.read_bytes()
        if not data:
            return []
        return [
            tuple(part.decode() for part in record.split(b"\x00")[:-1])
            for record in data.split(ARG_SEP)[:-1]
            if record
        ]

    def resolved(self) -> dict[str, str]:
        env: dict[str, str] = {}
        for line in self.github_env.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                env[key] = value
        return env

    def resolve(self, event: str, **inputs: str) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
        self.github_env.write_text("", encoding="utf-8")
        env = {"GITHUB_EVENT_NAME": event}
        for key, value in inputs.items():
            env[f"{key.upper()}_INPUT"] = value
        proc = self.run_block(RESOLVE, env)
        return proc, self.resolved()

    def route(self, name: str, source: str, publish: str, rotation: str = "false",
              release_id: str = "") -> subprocess.CompletedProcess[str]:
        return self.run_block(
            name,
            {
                "EFFECTIVE_SOURCE": source,
                "EFFECTIVE_PUBLISH": publish,
                "EFFECTIVE_ROTATION": rotation,
                "RELEASE_ID_INPUT": release_id,
            },
        )
