"""Bounded static and native checks for a built Tauri CLI candidate.

Two rules govern this module.

First, an upstream binary is *untrusted code*. It is never run before its
architecture has been read out of the ELF header and matched against the target
we asked for, so a mislabelled arm64 binary can never be executed on amd64. Its
shared libraries are read from the ELF dynamic section for the same reason: a
binary that needs a library the reviewed mapping does not cover is refused before
it can be packaged or run. When the requested architecture is not this machine's,
the checks stay static and say so. `--static-only` is an unconditional execution
disable: it returns after the static checks no matter what the host is or what
`--require-native` says.

Second, a run is *narrowed*, not sandboxed. It gets a private temporary HOME and
XDG tree, a minimal environment, and a 30 second wall-clock bound. The proxy
variables are set to a closed port, which deters proxy-aware requests — but that
is deterrence, not isolation: a client that ignores the proxy variables or opens
a raw socket is not stopped by this module, and nothing here should be described
as disabling the network. Only `cargo-tauri --version` and `cargo tauri
--version` run; no project is initialised, built, or bundled, and no package
manager is invoked.

The ELF header helpers and the probe environment are shared with the Bun
candidate's checker rather than copied, so both candidates agree on what an
architecture is and on how a probe is bounded.
"""

from __future__ import annotations

import argparse
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .bun_candidate_smoke import (
    TIMEOUT_SECONDS,
    check_elf_arch,
    isolated_env,
    native_arch,
)
from .common import require_tool
from .tauri_candidate_metadata import (
    ARCHES,
    BINARY,
    Candidate,
    RuntimeLibrary,
    check_source_document,
    license_bodies,
    load_candidate,
    repo_root,
    version_key,
)
from .tauri_candidate_provenance import (
    PROVENANCE_NAME_BY_ARCH,
    check_official_asset,
    check_source_build,
    read_record,
)

# Files the candidate installs under /usr/share/doc/cargo-tauri.
NOTICE_FILES = ("LICENSE-MIT.txt", "LICENSE-APACHE-2.0.txt")
DOC_FILES = ("copyright", "changelog.Debian", "README.source", *NOTICE_FILES)
PROVENANCE_FILES = PROVENANCE_NAME_BY_ARCH

_PT_LOAD = 1
_PT_DYNAMIC = 2
_DT_NULL = 0
_DT_NEEDED = 1
_DT_STRTAB = 5
_DT_STRSZ = 10
_DT_VERNEED = 0x6FFFFFFE
_DT_VERNEEDNUM = 0x6FFFFFFF
_ELF64_HEADER = 64
_PHDR_SIZE = 56
_DYN_SIZE = 16
_VERNEED_SIZE = 16
_VERNAUX_SIZE = 16
# A linked binary needs a handful of version records; anything far beyond that is
# a malformed or hostile structure, not a real dependency list.
_MAX_VERSION_RECORDS = 64
# Every range this checker reads is bounded by a ceiling as well as by the file
# size, so a hostile header can never turn into an allocation request.
_MAX_TABLE_BYTES = 64 * 1024
_MAX_DYNAMIC_BYTES = 1024 * 1024
_MAX_STRING_TABLE_BYTES = 1024 * 1024
_MAX_STRUCTURE_BYTES = 1024 * 1024
# One dotted version number, as glibc, GCC, and liblzma name theirs.
_SYMBOL_VERSION = re.compile(r"^[0-9]+(\.[0-9]+)*$")


def version_output(version: str) -> str:
    """The exact line `cargo-tauri --version` prints for this release."""
    return f"tauri-cli {version}"


@dataclass(frozen=True)
class _Dynamic:
    """The parts of an ELF this checker reads: its dynamic entries and segments."""

    path: Path
    entries: Mapping[int, tuple[int, ...]]
    loads: tuple[tuple[int, int, int, int], ...]


def _read_bounded(
    handle, path: Path, size: int, offset: int, ceiling: int, what: str
) -> bytes:
    """Read a declared range only after it is proven to fit both the file and a ceiling.

    The size in an ELF header is attacker-controlled, so it is checked against the
    real file length and a fixed ceiling *before* any read is issued. Without that
    a 352-byte file could ask for a gigabyte.
    """
    if size > ceiling:
        raise ValueError(f"{path} declares a {size} byte {what}, over the {ceiling} byte limit")
    length = path.stat().st_size
    if offset > length or size > length - offset:
        raise ValueError(f"{path} declares a {what} outside its {length} bytes")
    handle.seek(offset)
    raw = handle.read(size)
    if len(raw) < size:
        raise ValueError(f"{path} is truncated inside its {what}")
    return raw


def _dynamic(path: Path) -> _Dynamic | None:
    """Read the dynamic segment through the program headers.

    The result comes from the file itself rather than from an external tool that
    may be absent on a minimal build machine. A statically linked file has no
    dynamic segment and reads as `None`.
    """
    with path.open("rb") as handle:
        header = handle.read(_ELF64_HEADER)
    if len(header) < _ELF64_HEADER or header[:4] != b"\x7fELF":
        raise ValueError(f"{path} is not an ELF file")
    if header[4] != 2 or header[5] != 1:
        raise ValueError(f"{path} is not a 64-bit little-endian ELF file")
    phoff = struct.unpack_from("<Q", header, 32)[0]
    phentsize, phnum = struct.unpack_from("<HH", header, 54)
    if phentsize < _PHDR_SIZE or phnum == 0:
        raise ValueError(f"{path} has no usable program header table")
    with path.open("rb") as handle:
        table = _read_bounded(
            handle, path, phentsize * phnum, phoff, _MAX_TABLE_BYTES, "program header table",
        )
    loads: list[tuple[int, int, int, int]] = []
    segment: tuple[int, int] | None = None
    for index in range(phnum):
        base = index * phentsize
        p_type = struct.unpack_from("<I", table, base)[0]
        p_offset, p_vaddr = struct.unpack_from("<QQ", table, base + 8)
        p_filesz, p_memsz = struct.unpack_from("<QQ", table, base + 32)
        if p_type == _PT_LOAD:
            loads.append((p_vaddr, p_memsz, p_offset, p_filesz))
        elif p_type == _PT_DYNAMIC:
            segment = (p_offset, p_filesz)
    if segment is None:
        return None
    if segment[1] < _DYN_SIZE or segment[1] % _DYN_SIZE:
        raise ValueError(f"{path} has a malformed dynamic segment")
    with path.open("rb") as handle:
        raw = _read_bounded(
            handle, path, segment[1], segment[0], _MAX_DYNAMIC_BYTES, "dynamic segment",
        )
    entries: dict[int, list[int]] = {}
    for offset in range(0, len(raw), _DYN_SIZE):
        tag, value = struct.unpack_from("<qQ", raw, offset)
        if tag == _DT_NULL:
            break
        entries.setdefault(tag, []).append(value)
    return _Dynamic(path=path, entries={tag: tuple(values) for tag, values in entries.items()},
                    loads=tuple(loads))


def _vaddr_to_offset(
    loads: tuple[tuple[int, int, int, int], ...], vaddr: int, size: int, path: Path
) -> int:
    """Map a whole structure into file bytes: it must fit one loadable segment.

    Checking only the starting address would let a structure claim a span that
    runs past the file-backed part of its segment, so the whole range is required
    to lie inside the same segment's file data.
    """
    for base, memsz, offset, filesz in loads:
        if base <= vaddr and vaddr + size <= base + filesz:
            return offset + (vaddr - base)
    raise ValueError(f"{path} has a structure at {vaddr:#x} outside its loadable file data")


def _at(path: Path, loads: tuple[tuple[int, int, int, int], ...], vaddr: int, size: int) -> bytes:
    if size > _MAX_STRUCTURE_BYTES:
        raise ValueError(f"{path} declares a {size} byte structure, over the reader's limit")
    offset = _vaddr_to_offset(loads, vaddr, size, path)
    with path.open("rb") as handle:
        return _read_bounded(handle, path, size, offset, _MAX_STRUCTURE_BYTES, "structure")


def _string_table(dynamic: _Dynamic) -> bytes:
    path = dynamic.path
    strtab = _single(dynamic, _DT_STRTAB, "DT_STRTAB")
    strsz = _single(dynamic, _DT_STRSZ, "DT_STRSZ")
    offset = _vaddr_to_offset(dynamic.loads, strtab, strsz, path)
    with path.open("rb") as handle:
        return _read_bounded(
            handle, path, strsz, offset, _MAX_STRING_TABLE_BYTES, "string table",
        )


def _single(dynamic: _Dynamic, tag: int, label: str) -> int:
    values = dynamic.entries.get(tag, ())
    if len(values) != 1:
        raise ValueError(f"{dynamic.path} must declare exactly one {label}")
    return values[0]


def _string(path: Path, strings: bytes, offset: int, what: str) -> str:
    if offset >= len(strings):
        raise ValueError(f"{path} names {what} outside its string table")
    raw = strings[offset:].split(b"\0", 1)[0]
    if not raw:
        raise ValueError(f"{path} names an empty {what}")
    name = raw.decode("ascii")
    if any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise ValueError(f"{path} names {what} with a control byte")
    return name


def read_needed(path: Path) -> tuple[str, ...]:
    """Return the DT_NEEDED shared libraries of an ELF, without executing it."""
    dynamic = _dynamic(path)
    if dynamic is None:
        return ()
    needed = dynamic.entries.get(_DT_NEEDED, ())
    if not needed:
        return ()
    strings = _string_table(dynamic)
    names: list[str] = []
    for offset in needed:
        name = _string(path, strings, offset, "a shared library")
        if name not in names:
            names.append(name)
    return tuple(names)


def read_symbol_versions(path: Path) -> tuple[tuple[str, str], ...]:
    """Return the (library, symbol version) pairs an ELF requires, without running it.

    A linked library name alone does not justify a dependency floor: glibc, for
    example, keeps old SONAMEs across many versions, so what a binary actually
    requires is the highest symbol version it names. These pairs are read from the
    version-requirement records the dynamic section points at.

    The structure is read strictly: the record list and its count have to agree,
    every record has to hand over exactly the auxiliary versions it declares, and
    both chains have to end exactly where they say they do. A partial read of a
    truncated chain would silently drop a requirement — the one that decides
    whether the binary fits the declared floors — so an inconsistent structure is
    refused instead of read as far as it happens to go.
    """
    dynamic = _dynamic(path)
    if dynamic is None:
        return ()
    if len(dynamic.entries.get(_DT_VERNEED, ())) > 1 or len(
        dynamic.entries.get(_DT_VERNEEDNUM, ())
    ) > 1:
        raise ValueError(f"{path} declares its version requirements more than once")
    address = dynamic.entries.get(_DT_VERNEED, (0,))[0]
    count = dynamic.entries.get(_DT_VERNEEDNUM, (0,))[0]
    if address and not count:
        raise ValueError(f"{path} declares version requirements without DT_VERNEEDNUM")
    if count and not address:
        raise ValueError(f"{path} declares a version-requirement count without DT_VERNEED")
    if not address:
        return ()
    if count > _MAX_VERSION_RECORDS:
        raise ValueError(f"{path} declares {count} version-requirement records")
    strings = _string_table(dynamic)
    required: list[tuple[str, str]] = []
    for record in range(count):
        raw = _at(path, dynamic.loads, address, _VERNEED_SIZE)
        vn_version, vn_cnt, vn_file, vn_aux, vn_next = struct.unpack("<HHIII", raw)
        if vn_version != 1:
            raise ValueError(f"{path} has an unsupported version-requirement revision")
        if vn_cnt == 0 or vn_cnt > _MAX_VERSION_RECORDS:
            raise ValueError(f"{path} declares {vn_cnt} auxiliary version records")
        if vn_aux < _VERNEED_SIZE or vn_aux % 4:
            raise ValueError(f"{path} points its auxiliary records at offset {vn_aux}")
        library = _string(path, strings, vn_file, "a required library")
        aux = address + vn_aux
        for position in range(vn_cnt):
            vernaux = _at(path, dynamic.loads, aux, _VERNAUX_SIZE)
            _, _, _, vna_name, vna_next = struct.unpack("<IHHII", vernaux)
            required.append((library, _string(path, strings, vna_name, "a symbol version")))
            if position + 1 == vn_cnt and vna_next:
                raise ValueError(
                    f"{path} carries more auxiliary versions than its {vn_cnt} records declare"
                )
            if position + 1 < vn_cnt and not vna_next:
                raise ValueError(
                    f"{path} ends its auxiliary versions after {position + 1} of {vn_cnt} records"
                )
            aux += vna_next
        if record + 1 == count and vn_next:
            raise ValueError(
                f"{path} carries more version-requirement records than its {count} count"
            )
        if record + 1 < count and not vn_next:
            raise ValueError(
                f"{path} ends its version-requirement records after {record + 1} of {count}"
            )
        address += vn_next
    return tuple(required)


def symbol_floor(names: tuple[str, ...], prefix: str) -> tuple[int, ...] | None:
    """The highest version a binary requires in one symbol namespace.

    `GLIBC_PRIVATE` and the equivalent private namespaces carry no public version
    floor, so they are ignored; any other name that cannot be read as a version is
    refused rather than guessed, because an unreadable requirement is an
    unverifiable one.
    """
    best: tuple[int, ...] | None = None
    for name in names:
        if name.endswith("_PRIVATE"):
            continue
        if not name.startswith(prefix):
            raise ValueError(f"symbol version {name!r} is outside the reviewed {prefix} namespace")
        digits = name[len(prefix):]
        if not _SYMBOL_VERSION.match(digits):
            raise ValueError(f"symbol version {name!r} does not carry a version number")
        key = version_key(digits)
        if best is None or key > best:
            best = key
    return best


class RuntimePolicy(Protocol):
    """The reviewed SONAME mapping a binary's links are measured against.

    The candidate descriptor and a manifest recipe both satisfy this shape, so
    one implementation of the ABI check serves the candidate build and the
    production source-artifact consumer.
    """

    @property
    def libraries(self) -> tuple[RuntimeLibrary, ...]: ...

    def sonames(self) -> tuple[str, ...]: ...

    def declared_floors(self) -> dict[str, str]: ...


def check_runtime(binary: Path, arch: str, policy: RuntimePolicy) -> list[str]:
    """Check the linked libraries and their symbol floors against the mapping.

    Three things can go wrong and all of them are refusals: a library the reviewed
    mapping does not cover, a library whose required symbol versions exceed the
    floor the mapping declares for its Debian package, and a version requirement
    that belongs to no library the binary says it links. The second is the one a
    `DT_NEEDED`-only check misses, and it is never resolved by quietly widening a
    dependency — a floor the policy cannot justify is a decision for review.

    The requirement records are checked as a whole before anything is filtered to
    the libraries the binary links, so a record whose owner is missing from
    `DT_NEEDED` cannot slip past the measurement that follows.
    """
    check_elf_arch(binary, arch)
    needed = read_needed(binary)
    reviewed = policy.sonames()
    unmapped = sorted(name for name in needed if name not in reviewed)
    if unmapped:
        raise ValueError(
            f"{binary} links {unmapped}, which the reviewed runtime mapping does "
            "not cover; the mapping needs reviewed evidence before it can be used"
        )
    required = read_symbol_versions(binary)
    orphans = sorted({library for library, _ in required} - set(needed))
    if orphans:
        raise ValueError(
            f"{binary} requires symbol versions from {orphans}, which it does not list "
            "as a linked library; a version requirement must belong to a DT_NEEDED entry"
        )
    # The unversioned rule is applied to every requirement the file declares, not to
    # the subset that survives the loop below, so no requirement can escape it.
    unversioned_sonames = {item.soname for item in policy.libraries if item.symbols is None}
    unversioned_owners = sorted(
        {library for library, _ in required if library in unversioned_sonames}
    )
    if unversioned_owners:
        raise ValueError(
            f"{binary} requires symbol versions from {unversioned_owners}, but the "
            "reviewed mapping records that library as unversioned; the mapping needs "
            "reviewed evidence"
        )
    unmapped_versions = sorted(
        {library for library, _ in required if library not in reviewed}
    )
    if unmapped_versions:
        raise ValueError(
            f"{binary} requires symbol versions from {unmapped_versions}, which the "
            "reviewed runtime mapping does not cover"
        )
    declared = policy.declared_floors()
    measured: dict[str, tuple[int, ...]] = {}
    for library in needed:
        entry = next(item for item in policy.libraries if item.soname == library)
        if entry.symbols is None:
            # Reviewed as unversioned, so there is no namespace to measure a floor
            # in; any version requirement from it was already refused above.
            continue
        names = tuple(name for owner, name in required if owner == library)
        floor = symbol_floor(names, entry.symbols)
        if floor is None:
            continue
        package = entry.package
        if floor > (measured.get(package) or ()):
            measured[package] = floor
    for package, floor in sorted(measured.items()):
        declared_floor = version_key(declared[package])
        if floor > declared_floor:
            raise ValueError(
                f"{binary} requires {package} {'.'.join(str(part) for part in floor)}, "
                f"which the reviewed mapping does not declare "
                f"({package} (>= {declared[package]})); widening a floor needs "
                "reviewed evidence"
            )
    results = [
        f"{BINARY}: links {', '.join(needed) if needed else 'nothing (static)'} "
        f"(reviewed mapping: {', '.join(reviewed)})"
    ]
    if measured:
        results.append(
            f"{BINARY}: highest required symbols "
            + ", ".join(
                f"{package} {'.'.join(str(part) for part in floor)}"
                f" (declared >= {declared[package]})"
                for package, floor in sorted(measured.items())
            )
        )
    unversioned = sorted(
        soname for soname in unversioned_sonames if soname in needed
    )
    if unversioned:
        results.append(
            f"{BINARY}: {', '.join(unversioned)} declares no symbol versions, as reviewed"
        )
    unused = sorted(name for name in reviewed if name not in needed)
    if unused:
        results.append(f"reviewed mapping not used by this binary: {', '.join(unused)}")
    return results


def check_payload(bin_dir: Path, arch: str, candidate: Candidate) -> list[str]:
    """Static checks on the installed binary: shape, mode, and ABI requirements."""
    binary = bin_dir / BINARY
    if not binary.is_file() or binary.is_symlink():
        raise ValueError(f"{binary} is missing or is not a regular file")
    check_elf_arch(binary, arch)
    mode = binary.stat().st_mode & 0o777
    if mode != 0o755:
        raise ValueError(f"{binary} has mode {mode:o}, expected 755")
    return [f"{BINARY}: ELF 64-bit {arch}, mode 0755", *check_runtime(binary, arch, candidate)]


def check_documents(doc_dir: Path, arch: str, candidate: Candidate, repo: Path) -> list[str]:
    """Static checks on the installed notices and the architecture's record."""
    for name in DOC_FILES:
        path = doc_dir / name
        if not path.is_file():
            raise ValueError(f"installed notice {path} is missing")
        if path.stat().st_mode & 0o111:
            raise ValueError(f"installed notice {path} must not be executable")
    check_source_document(doc_dir / "README.source", candidate.package.version)
    reviewed = license_bodies(repo / "packaging" / "licenses" / "tauri-cli")
    for name, body in zip(NOTICE_FILES, reviewed, strict=True):
        installed = (doc_dir / name).read_text(encoding="utf-8")
        if installed != body:
            raise ValueError(f"installed {name} is not the reviewed upstream licence text")
    record_name = PROVENANCE_FILES[arch]
    raw = read_record(doc_dir / record_name)
    if arch == "arm64":
        check_source_build(raw, candidate)
        summary = f"native source build at commit {raw['source']['commit']}"
    else:
        check_official_asset(raw, candidate)
        summary = f"official release asset {raw['asset']['sha256'][:12]}"
    return [
        "notices: both licence texts byte-identical to the reviewed upstream files",
        "README.source: states every reviewed source reference",
        f"{record_name}: {summary}",
    ]


def _parse_control(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def deb_control(deb: Path) -> dict[str, str]:
    require_tool("dpkg-deb")
    completed = subprocess.run(
        ["dpkg-deb", "-f", str(deb)], text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if completed.returncode != 0:
        raise ValueError(f"dpkg-deb -f {deb.name} failed: {completed.stderr.strip()}")
    return _parse_control(completed.stdout)


def installed_control(prefix: Path) -> dict[str, str]:
    """Read the installed package's fields from dpkg's own status file."""
    status = prefix / "var" / "lib" / "dpkg" / "status"
    try:
        text = status.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ValueError(f"dpkg status file not found: {status}") from exc
    for stanza in text.split("\n\n"):
        fields = _parse_control(stanza)
        if fields.get("Package") == BINARY:
            return fields
    raise ValueError(f"{status} records no installed {BINARY} package")


def check_control(fields: dict[str, str], arch: str, candidate: Candidate) -> list[str]:
    """The installed metadata must match the reviewed version, arch, and Depends."""
    expected = {
        "Package": BINARY,
        "Version": f"{candidate.package.version}-1",
        "Architecture": arch,
    }
    for key, want in expected.items():
        got = fields.get(key, "")
        if got != want:
            raise ValueError(f"control {key} is {got!r}, expected {want!r}")
    expected_terms = {term.strip() for term in candidate.depends().split(",")}
    got_terms = {term.strip() for term in fields.get("Depends", "").split(",") if term.strip()}
    if got_terms != expected_terms:
        raise ValueError(
            f"control Depends is {sorted(got_terms)}, expected {sorted(expected_terms)}"
        )
    return [f"control: {BINARY} {expected['Version']} {arch}, Depends {candidate.depends()}"]


def run_probe(command: list[str], home: Path, *, path: str | None = None) -> str:
    """Run one bounded probe in the shared isolated environment.

    `path` overrides only `PATH`, which is how the Cargo external-subcommand
    check points Cargo at the candidate's own `usr/bin` instead of at whatever
    else the machine has installed.
    """
    env = isolated_env(home)
    if path is not None:
        env["PATH"] = path
    try:
        completed = subprocess.run(
            command, cwd=home, env=env, timeout=TIMEOUT_SECONDS,
            capture_output=True, text=True, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"{command[0]} exceeded the {TIMEOUT_SECONDS}s bound") from exc
    if completed.returncode != 0:
        raise ValueError(
            f"{' '.join(command)} exited {completed.returncode}: {completed.stderr.strip()}"
        )
    return completed.stdout.strip()


def native_checks(
    binary: Path, version: str, home: Path, cargo: str | None, *, require_cargo: bool
) -> list[str]:
    """Run the two bounded checks inside `home` and return their result lines."""
    # Both workflows pass a relative --upstream, so the compiled binary path is
    # relative too. The probe runs with cwd set to the isolated home, so every
    # executable path is resolved to absolute first; the proof still runs with
    # the private HOME/XDG tree.
    binary = Path(binary).resolve()
    cargo_path = str(Path(cargo).resolve()) if cargo is not None else None
    expected = version_output(version)
    reported = run_probe([str(binary), "--version"], home)
    if reported != expected:
        raise ValueError(
            f"{BINARY} --version printed {reported!r}, expected {expected!r}"
        )
    results = [f"{BINARY} --version -> {reported}"]
    if cargo_path is None:
        if require_cargo:
            raise ValueError("the cargo external-subcommand check needs a cargo on PATH")
        results.append("cargo tauri --version skipped: no cargo on PATH")
        return results
    probe_path = f"{binary.parent}:{Path(cargo_path).parent}"
    resolved = shutil.which(BINARY, path=probe_path)
    if resolved is None or Path(resolved).resolve() != binary.resolve():
        raise ValueError(
            f"cargo would not resolve {BINARY} to the candidate binary (found {resolved!r})"
        )
    external = run_probe([cargo_path, "tauri", "--version"], home, path=probe_path)
    if external != expected:
        raise ValueError(f"cargo tauri --version printed {external!r}, expected {expected!r}")
    results.append(f"cargo tauri --version -> {external} (cargo external subcommand)")
    return results


def smoke(
    root: Path,
    arch: str,
    version: str,
    candidate: Candidate,
    fields: dict[str, str],
    *,
    require_native: bool = False,
    allow_execution: bool = True,
    cargo: str | None = None,
    require_cargo: bool = False,
) -> list[str]:
    """Check a built or installed package; one result line per property."""
    bin_dir = root / "usr" / "bin"
    doc_dir = root / "usr" / "share" / "doc" / BINARY
    results = check_payload(bin_dir, arch, candidate)
    results.extend(check_documents(doc_dir, arch, candidate, repo_root()))
    results.extend(check_control(fields, arch, candidate))
    if not allow_execution:
        results.append("execution disabled by --static-only; static checks only")
        return results
    host = native_arch()
    if host != arch:
        if require_native:
            raise ValueError(f"native smoke needs an {arch} machine, this is {host}")
        results.append(f"native run skipped: this machine is {host}, not {arch}")
        return results
    with tempfile.TemporaryDirectory(prefix="apt-tauri-smoke-") as private:
        results.extend(
            native_checks(
                bin_dir / BINARY, version, Path(private), cargo,
                require_cargo=require_cargo,
            )
        )
    return results


def _extract_deb(deb: Path) -> Path:
    require_tool("dpkg-deb")
    target = Path(tempfile.mkdtemp(prefix="apt-tauri-pool-"))
    subprocess.run(
        ["dpkg-deb", "-x", str(deb), str(target)], check=True, capture_output=True,
    )
    return target


def _pool_deb(pool: Path, arch: str, version: str) -> Path:
    matches = sorted(pool.glob(f"{BINARY}_{version}-1_{arch}.deb"))
    if len(matches) != 1:
        raise ValueError(
            f"expected exactly one {BINARY} {arch} deb in {pool}, found {len(matches)}"
        )
    return matches[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.tauri_candidate_smoke",
        description="Static and bounded native checks for a Tauri CLI candidate package.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--pool", type=Path, help="directory holding the built candidate debs")
    source.add_argument("--installed", action="store_true", help="check the installed package")
    parser.add_argument("--arch", choices=ARCHES, required=True)
    parser.add_argument("--candidate", type=Path, default=None)
    parser.add_argument("--prefix", type=Path, default=Path("/"),
                        help="install root for --installed; defaults to /")
    parser.add_argument("--static-only", action="store_true",
                        help="never execute the binary, whatever the host architecture")
    parser.add_argument("--require-native", action="store_true",
                        help="fail instead of skipping when this machine is not --arch")
    parser.add_argument("--cargo", default=None,
                        help="cargo to use for the external-subcommand check; defaults to PATH")
    parser.add_argument("--require-cargo", action="store_true",
                        help="fail instead of skipping when no cargo is available")
    args = parser.parse_args(argv)
    if args.static_only and args.require_native:
        parser.error("--static-only disables execution and contradicts --require-native")
    if args.static_only and args.require_cargo:
        parser.error("--static-only disables execution and contradicts --require-cargo")
    try:
        candidate = load_candidate(args.candidate)
        version = candidate.package.version
        if args.pool is not None:
            deb = _pool_deb(args.pool, args.arch, version)
            root = _extract_deb(deb)
            fields = deb_control(deb)
        else:
            root = args.prefix
            fields = installed_control(args.prefix)
        cargo = args.cargo or shutil.which("cargo")
        for line in smoke(
            root, args.arch, version, candidate, fields,
            require_native=args.require_native, allow_execution=not args.static_only,
            cargo=cargo, require_cargo=args.require_cargo,
        ):
            print(line)
    except (ValueError, subprocess.CalledProcessError) as exc:
        print(f"smoke failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
