"""Generate APT `Packages` indices with reprepro instead of apt-ftparchive.

reprepro reads a real package database, so stanza fields come from the ``.deb``
control data rather than from a hand-rolled header writer. The database is a
throwaway directory rebuilt from the pool on every run, and the exported
``Filename`` is flattened back to the pool-relative path the Worker serves.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from .common import require_tool, run
from .metadata import ARCHES, COMPONENT, SUITE, write_gzip_deterministic


def _write_conf(conf_dir: Path) -> Path:
    """Write a minimal reprepro distributions file for the stable suite."""
    lines = [
        "Origin: apt-at-home",
        "Label: apt-at-home",
        f"Suite: {SUITE}",
        f"Codename: {SUITE}",
        f"Architectures: {' '.join(ARCHES)}",
        f"Components: {COMPONENT}",
    ]
    conf_dir.mkdir(parents=True, exist_ok=True)
    path = conf_dir / "distributions"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _pool_relative(filename: str, *, pool_dir: Path) -> str:
    """Validate a reprepro pool path and return the flat `pool/<name>` form."""
    parts = [part for part in filename.strip().split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise ValueError(f"reprepro emitted a non-pool Filename: {filename!r}")
    name = parts[-1]
    if name.startswith("."):
        raise ValueError(f"reprepro emitted a hidden Filename: {filename!r}")
    candidate = pool_dir / name
    if not candidate.is_file():
        raise ValueError(f"reprepro indexed a package missing from the pool: {name}")
    return f"pool/{name}"


def _normalize_stanzas(text: str, *, pool_dir: Path) -> str:
    """Rewrite nested reprepro Filenames to the served flat pool path."""
    stanzas: list[str] = []
    for stanza in text.strip().split("\n\n"):
        if not stanza.strip():
            continue
        lines: list[str] = []
        seen_filename = False
        for line in stanza.splitlines():
            if line.startswith("Filename: "):
                flat = _pool_relative(line[len("Filename: "):], pool_dir=pool_dir)
                lines.append(f"Filename: {flat}")
                seen_filename = True
            else:
                lines.append(line)
        if not seen_filename:
            raise ValueError("reprepro emitted a stanza without a Filename field")
        stanzas.append("\n".join(lines))
    return "\n\n".join(stanzas) + "\n"


def _export(debs: list[Path], *, arches: tuple[str, ...], pool_dir: Path) -> dict[str, str]:
    """Run one throwaway reprepro database and return each arch's Packages text.

    Every deb is passed to a single `includedeb` call so reprepro builds one
    consistent index per architecture, and the scratch database is discarded on
    return so nothing about it is persisted.
    """
    require_tool("reprepro")
    ordered = sorted(debs, key=lambda p: p.name)
    if not ordered:
        raise ValueError("reprepro requires at least one .deb to export an index")
    with tempfile.TemporaryDirectory(prefix="apt-reprepro-") as scratch:
        basedir = Path(scratch) / "repro"
        conf_dir = Path(scratch) / "conf"
        _write_conf(conf_dir)
        base = [
            "reprepro",
            "--basedir", str(basedir),
            "--confdir", str(conf_dir),
        ]
        include = run([*base, "includedeb", SUITE, *[str(d) for d in ordered]])
        if include.returncode != 0:
            raise RuntimeError(f"reprepro includedeb failed: {include.stderr.strip()}")
        export = run([*base, "export", SUITE])
        if export.returncode != 0:
            raise RuntimeError(f"reprepro export failed: {export.stderr.strip()}")
        out: dict[str, str] = {}
        for arch in arches:
            exported = basedir / "dists" / SUITE / COMPONENT / f"binary-{arch}" / "Packages"
            if not exported.is_file():
                raise ValueError(f"reprepro exported no Packages index for {arch}")
            out[arch] = _normalize_stanzas(exported.read_text(encoding="utf-8"), pool_dir=pool_dir)
    return out


def generate_packages(pool_dir: Path, dists_arch_dir: Path, debs: list[Path]) -> tuple[Path, Path]:
    """Write `Packages`/`Packages.gz` for one architecture via reprepro.

    Signature-compatible with the apt-ftparchive helper it replaces, so callers
    keep passing a pool directory and the per-architecture dists directory.
    """
    arch = dists_arch_dir.name.removeprefix("binary-")
    if arch not in ARCHES:
        raise ValueError(f"unexpected Packages output directory: {dists_arch_dir.name}")
    text = _export(debs, arches=(arch,), pool_dir=pool_dir)[arch]
    dists_arch_dir.mkdir(parents=True, exist_ok=True)
    packages_path = dists_arch_dir / "Packages"
    packages_path.write_text(text, encoding="utf-8")
    gz_path = dists_arch_dir / "Packages.gz"
    write_gzip_deterministic(packages_path, gz_path)
    return packages_path, gz_path


def reprepro_version() -> str:
    """Return the installed reprepro version banner, for logs and diagnostics."""
    require_tool("reprepro")
    proc = run(["reprepro", "--version"])
    return (proc.stdout or proc.stderr).strip()
