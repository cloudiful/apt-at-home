"""Reviewed native probes for commands beyond each recipe's primary binary."""

from __future__ import annotations

from dataclasses import dataclass

from .manifest import ArchTarget


@dataclass(frozen=True)
class CommandProbe:
    binary: str
    args: tuple[str, ...]
    output_contains: str = ""


_EXTRA_PROBES: dict[str, tuple[CommandProbe, ...]] = {
    "uv": (CommandProbe("uvx", ("--version",)),),
    "zoxide": (
        CommandProbe("zoxide", ("init", "bash"), "__zoxide_z"),
    ),
}


def binaries_for_target(target: ArchTarget) -> tuple[str, ...]:
    """Return the complete reviewed /usr/bin command inventory for a target."""
    return (target.binary, *target.additional_binaries.values())


def extra_command_probes(package_name: str) -> tuple[CommandProbe, ...]:
    """Return recipe-specific native checks beyond the primary command probe."""
    return _EXTRA_PROBES.get(package_name, ())


def check_probe_output(probe: CommandProbe, output: str) -> None:
    """Validate any stable output contract for a native probe."""
    if probe.output_contains and probe.output_contains not in output:
        raise ValueError(
            f"{probe.binary} {' '.join(probe.args)} output lacks "
            f"{probe.output_contains!r}"
        )
