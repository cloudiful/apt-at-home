"""Post-upgrade assertions for the Debian APT transition onto the packaged Neovim.

CI installs the distro `neovim`, then upgrades it with the generated `.deb` files
through the normal APT solver. This module checks the resulting machine state: the
installed `neovim` version equals the manifest, the old distro `neovim-runtime` is
gone, and no package status is left half-configured. It reads dpkg state only and
never runs the packaged binary; native execution is `scripts.smoke --installed`.
"""

from __future__ import annotations

import subprocess

from .manifest import Manifest
from .smoke_package import SmokeError

_STATUS_FORMAT = "${Package} ${Version} ${Status}\n"


def _query(package: str) -> tuple[str, str] | None:
    """Return (version, status) for an installed package, or None when absent."""
    proc = subprocess.run(
        ["dpkg-query", "-W", f"-f={_STATUS_FORMAT}", package],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if proc.returncode != 0:
        return None
    parts = proc.stdout.strip().split(" ", 2)
    if len(parts) != 3:
        raise SmokeError(f"unexpected dpkg-query output for {package}: {proc.stdout!r}")
    return parts[1], parts[2]


def _owns_files(status: str) -> bool:
    """A removed package keeps only conffiles (`rc`) and no longer owns files."""
    return not status.startswith("deinstall") and "not-installed" not in status and not status.endswith("config-files")


def check_upgraded_state(manifest: Manifest, package: str = "neovim") -> list[str]:
    """Require the packaged neovim to be the installed one and the distro runtime gone."""
    pkg = next((p for p in manifest.packages if p.name == package), None)
    if pkg is None:
        raise SmokeError(f"manifest does not carry {package}")
    expected = f"{pkg.version}{manifest.debian_revision}"
    found = _query(package)
    if found is None:
        raise SmokeError(f"{package} is not installed after the upgrade")
    version, status = found
    if version != expected:
        raise SmokeError(f"{package} is {version}, the manifest expects {expected}")
    if status != "install ok installed":
        raise SmokeError(f"{package} status is {status!r}, expected installed")
    runtime = _query("neovim-runtime")
    if runtime is not None and _owns_files(runtime[1]):
        raise SmokeError(
            f"neovim-runtime {runtime[0]} still owns files ({runtime[1]!r}); "
            "the packaged neovim must replace it through APT"
        )
    return [f"{package} {version} is installed and neovim-runtime is absent"]
