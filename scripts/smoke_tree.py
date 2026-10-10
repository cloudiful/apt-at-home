"""Static checks for a packaged upstream tree: payload inventory, parser ELF, relations.

A tree package is checked against its reviewed recipe, never against a copy of its
paths. Every required runtime, notice, man, desktop, and icon path must be present;
each parser library must match the package architecture; and the package-level
Conflicts and Replaces must name the distribution runtime they replace. Nothing is
executed: the checks read the extracted files and the control fields only.
"""

from __future__ import annotations

from pathlib import Path

from .catalog import conflicts_for, parsers_for, replaces_for, required_paths_for
from .manifest import Package
from .smoke_package import ELF_MACHINE, SmokeError, elf_machine

PARSER_DIR = "lib/nvim/parser"


def check_tree_payload(pkg: Package, arch: str, usr_root: Path) -> list[str]:
    """Require the recipe's full tree inventory and matching parser architectures.

    `usr_root` is the extracted package's `usr/` directory. Returns one finding
    per parser checked; raises SmokeError on the first missing or wrong file.
    """
    findings: list[str] = []
    for relative in required_paths_for(pkg.name):
        if not (usr_root / relative).is_file():
            raise SmokeError(f"{pkg.name} {arch} payload is missing usr/{relative}")
    expected = ELF_MACHINE[arch]
    for parser in parsers_for(pkg.name):
        library = usr_root / PARSER_DIR / f"{parser}.so"
        if not library.is_file():
            raise SmokeError(f"{pkg.name} {arch} payload is missing usr/{PARSER_DIR}/{parser}.so")
        machine = elf_machine(library)
        if machine != expected:
            raise SmokeError(
                f"{pkg.name} {arch} parser {parser}.so is {machine}, expected {expected}"
            )
        findings.append(f"{parser}.so is {expected}")
    return findings


def check_relations(pkg: Package, control: dict[str, str]) -> None:
    """Require the recipe's Conflicts and Replaces relations in the control fields."""
    for field, expected in (("Conflicts", conflicts_for(pkg.name)), ("Replaces", replaces_for(pkg.name))):
        if not expected:
            continue
        if control.get(field, "") != expected:
            raise SmokeError(
                f"{pkg.name} declares {field}: {control.get(field, '')!r}, expected {expected!r}"
            )
