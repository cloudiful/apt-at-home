"""Build the Woodpecker packages from its official Debian package and tarball.

Woodpecker publishes an official amd64 Debian package whose payload, ownership,
and maintainer script define the installation behavior, and an arm64 tarball
that holds only the static binary. The amd64 package is repacked with its data
member and control scripts preserved; only the control file's Version changes.
The arm64 package is assembled from the reviewed tagged packaging files with the
same paths, modes, and owner names.

Nothing here executes an upstream binary, runs a maintainer script, or trusts
release metadata beyond the reviewed digests and the checked control identity.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import os
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .catalog import binary_dir_for
from .common import sha256_file
from .deb import SOURCE_DATE_EPOCH
from .manifest import Manifest, Package, debian_version
from .smoke_package import SmokeError, elf_machine
from .upstream_assets import license_url

PACKAGES = ("woodpecker-agent", "woodpecker-cli")
PACKAGING_TAG = "v3.20.1"
PACKAGING_DIR = Path(__file__).resolve().parent.parent / "packaging" / "woodpecker"
MAX_PACKAGING_FILE_BYTES = 1024 * 1024

# The exact tagged files the arm64 package vendors, with their reviewed
# digests. The source path is the repository path at the tag.
PACKAGING_FILES = {
    "nfpm/agent.yaml": (
        "v3.20.1/agent.yaml",
        "0b19bebe448bedee1f240ec98dfe12fa5086e16e22d73df2d0b715d4f4d885fb",
    ),
    "nfpm/cli.yaml": (
        "v3.20.1/cli.yaml",
        "ca8ab1a1dc8976f7991d646c070b74b94eba1535c1ec391b782db4d1d74ed063",
    ),
    "nfpm/woodpecker-system-user.preinstall.sh": (
        "v3.20.1/woodpecker-system-user.preinstall.sh",
        "ca2717f12a9f079f3cee031b2822498806a2c1f93f293be4b4f2a66aed401f74",
    ),
    "nfpm/woodpecker-agent.service": (
        "v3.20.1/woodpecker-agent.service",
        "44efabc6fe9adedd8b2314494252f62f6316cf41b715fe50c64877dc7838b5bd",
    ),
    "nfpm/woodpecker-agent.env.example": (
        "v3.20.1/woodpecker-agent.env.example",
        "f4c3ca7c6ad734836eec21c3cbbe8b491533a8cc97a6e33d65a5a3d7949e02ac",
    ),
}

# The official amd64 package assets. Their digests are the reviewed source
# identity for the repack; discovery pins the same bytes through its API digest.
OFFICIAL_DEBS = {
    "woodpecker-agent": ("woodpecker-agent_3.20.1_amd64.deb",
                         "24e70c5225afcc61f3aa4fe798665406c556db642822f23366b08ad1722000d4"),
    "woodpecker-cli": ("woodpecker-cli_3.20.1_amd64.deb",
                       "e60ce30c33263529a4124f02110072117153f8e3c78e46ea6682fb0f0b21d865"),
}

# The official control metadata, kept exactly as upstream publishes it.
OFFICIAL_METADATA = {
    "woodpecker-agent": {
        "Section": "daemon/system",
        "Priority": "optional",
        "License": "Apache 2.0",
        "Maintainer": "Woodpecker Authors <maintainer@woodpecker-ci.org>",
        "Homepage": "https://woodpecker-ci.org/",
        "Description": "Woodpecker Agent",
        "installed": "/usr/local/bin/woodpecker-agent",
    },
    "woodpecker-cli": {
        "Section": "utils",
        "Priority": "optional",
        "License": "Apache 2.0",
        "Maintainer": "Woodpecker Authors <maintainer@woodpecker-ci.org>",
        "Homepage": "https://woodpecker-ci.org/",
        "Description": "Woodpecker CLI",
        "installed": "/usr/local/bin/woodpecker-cli",
    },
}

AGENT_SERVICE = "./usr/local/lib/systemd/system/woodpecker-agent.service"
AGENT_ENV_EXAMPLE = "./etc/woodpecker/woodpecker-agent.env.example"
AGENT_STATE_DIR = "./var/lib/woodpecker"
AGENT_PREINST = "./preinst"


@dataclass(frozen=True)
class PayloadEntry:
    """One tar member of a package payload or control archive."""

    name: str
    payload: bytes | None
    mode: int
    uname: str = "root"
    gname: str = "root"


def is_woodpecker(package_name: str) -> bool:
    return package_name in PACKAGES


def vendored(name: str) -> bytes:
    """Return one reviewed tagged packaging file from the repository copy."""
    _source, relative, digest = _packaging_record(name)
    path = PACKAGING_DIR / relative
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != digest:
        raise ValueError(f"vendored Woodpecker packaging file changed: {path}")
    return payload


def _packaging_record(name: str) -> tuple[str, str, str]:
    try:
        relative, digest = PACKAGING_FILES[name]
    except KeyError:
        raise ValueError(f"unreviewed Woodpecker packaging file: {name!r}") from None
    return name, relative, digest


def check_packaging_sources(client, tag: str) -> None:
    """Stop discovery when the tagged packaging semantics changed.

    The arm64 package vendors the unit, environment example, and system-user
    script at the reviewed tag. A newer release may publish the same files; if
    any of them changed, the reviewed layout no longer describes it and the run
    stops instead of silently diverging.
    """
    for source_path, (_relative, digest) in sorted(PACKAGING_FILES.items()):
        url = license_url("woodpecker-ci/woodpecker", tag, source_path)
        payload = client.get_bytes(url, max_bytes=MAX_PACKAGING_FILE_BYTES)
        if hashlib.sha256(payload).hexdigest() != digest:
            raise ValueError(
                f"Woodpecker {tag} changed its official packaging file "
                f"{source_path}; review the packaging before updating"
            )


def _tar_gz(entries: list[PayloadEntry]) -> bytes:
    """Return a deterministic gzipped tar with fixed owner and mtime metadata."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for entry in entries:
            info = tarfile.TarInfo(entry.name)
            info.mode = entry.mode
            info.mtime = SOURCE_DATE_EPOCH
            info.uid = 0
            info.gid = 0
            info.uname = entry.uname
            info.gname = entry.gname
            if entry.payload is None:
                info.type = tarfile.DIRTYPE
                info.size = 0
                tar.addfile(info)
            else:
                info.type = tarfile.REGTYPE
                info.size = len(entry.payload)
                tar.addfile(info, io.BytesIO(entry.payload))
    return gzip.compress(raw.getvalue(), compresslevel=9, mtime=0)


def _ar_bytes(members: list[tuple[str, bytes]]) -> bytes:
    """Serialize members into a deterministic Debian ar archive."""
    out = bytearray(b"!<arch>\n")
    for name, payload in members:
        if len(name) > 16:
            raise ValueError(f"unusable ar member name: {name}")
        header = (
            f"{name:<16}{0:<12}{0:<6}{0:<6}{100644:<8}{len(payload):<10}`\n"
        )
        out.extend(header.encode("ascii"))
        out.extend(payload)
        if len(payload) % 2:
            out.extend(b"\n")
    return bytes(out)


def assemble_deb(
    output: Path,
    *,
    control_files: list[PayloadEntry],
    data_entries: list[PayloadEntry],
    control_name: str = "control.tar.gz",
    data_name: str = "data.tar.gz",
) -> None:
    """Assemble one Debian package from reviewed control and data members."""
    control = _tar_gz(control_files)
    data = _tar_gz(data_entries)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(
        _ar_bytes(
            [
                ("debian-binary", b"2.0\n"),
                (control_name, control),
                (data_name, data),
            ]
        )
    )


def _read_ar(path: Path) -> dict[str, bytes]:
    """Read a Debian ar archive; extended-name members are refused."""
    payload = path.read_bytes()
    if not payload.startswith(b"!<arch>\n"):
        raise ValueError(f"{path.name} is not an ar archive")
    offset = 8
    members: dict[str, bytes] = {}
    while offset < len(payload):
        header = payload[offset:offset + 60]
        if len(header) != 60 or header[58:60] != b"`\n":
            raise ValueError(f"{path.name} has a malformed ar header")
        name = header[:16].decode("ascii").strip()
        if name.startswith("#1/"):
            raise ValueError(f"{path.name} uses unsupported extended ar names")
        try:
            size = int(header[48:58].decode("ascii").strip())
        except ValueError:
            raise ValueError(f"{path.name} has a malformed ar member size") from None
        offset += 60
        body = payload[offset:offset + size]
        if len(body) != size:
            raise ValueError(f"{path.name} has a truncated ar member {name}")
        members[name] = body
        offset += size
        if size % 2:
            offset += 1
    return members


def _read_tar(payload: bytes, *, name: str) -> list[tuple[tarfile.TarInfo, bytes | None]]:
    """Return a tar's `(info, payload)` members for the supported compressions."""
    if name.endswith(".gz"):
        mode = "r:gz"
    elif name.endswith(".xz"):
        mode = "r:xz"
    else:
        raise ValueError(f"unsupported package member compression: {name}")
    entries: list[tuple[tarfile.TarInfo, bytes | None]] = []
    with tarfile.open(fileobj=io.BytesIO(payload), mode=mode) as tar:
        for info in tar:
            body = None
            if info.isreg():
                extracted = tar.extractfile(info)
                body = extracted.read() if extracted is not None else b""
            entries.append((info, body))
    return entries


def _normalized(name: str) -> str:
    normalized = os.path.normpath(name)
    if os.path.isabs(normalized) or normalized.startswith(".."):
        raise ValueError(f"unsafe package member path: {name!r}")
    return "./" + normalized.lstrip("./")


def _control_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith(" ") or ": " not in line:
            continue
        key, value = line.split(": ", 1)
        fields.setdefault(key, value)
    return fields


def _rewrite_control(
    text: str, *, name: str, arch: str, upstream_version: str, version: str
) -> str:
    """Return control text with only its Version line replaced."""
    lines = text.splitlines(keepends=True)
    replaced = 0
    output: list[str] = []
    for line in lines:
        if line.startswith("Version: "):
            declared = line[len("Version: "):].strip()
            if declared != upstream_version:
                raise ValueError(
                    f"{name} {arch} official package declares version "
                    f"{declared!r}, expected {upstream_version!r}"
                )
            newline = "\n" if line.endswith("\n") else ""
            output.append(f"Version: {version}{newline}")
            replaced += 1
        else:
            output.append(line)
    if replaced != 1:
        raise ValueError(f"{name} {arch} official control has no single Version field")
    fields = _control_fields(text)
    if fields.get("Package") != name:
        raise ValueError(
            f"official package {fields.get('Package')!r} is not {name!r}"
        )
    if fields.get("Architecture") != arch:
        raise ValueError(
            f"{name} official package is {fields.get('Architecture')!r}, not {arch!r}"
        )
    metadata = OFFICIAL_METADATA[name]
    for field in ("Section", "License", "Maintainer", "Homepage", "Description"):
        if fields.get(field) != metadata[field]:
            raise ValueError(
                f"{name} {arch} official control field {field} changed: "
                f"{fields.get(field)!r}"
            )
    return "".join(output)


def _expected_data_layout(
    name: str,
) -> dict[str, tuple[str, int, int, int, str, str]]:
    """Return the exact reviewed path, type, mode, numeric owner, and owner names."""
    layout = {
        "./usr": ("dir", 0o755, 0, 0, "root", "root"),
        "./usr/local": ("dir", 0o755, 0, 0, "root", "root"),
        "./usr/local/bin": ("dir", 0o755, 0, 0, "root", "root"),
        f"./{OFFICIAL_METADATA[name]['installed'].lstrip('/')}": (
            "file", 0o755, 0, 0, "root", "root"
        ),
    }
    if name == "woodpecker-agent":
        layout.update({
            "./etc": ("dir", 0o755, 0, 0, "root", "root"),
            "./etc/woodpecker": ("dir", 0o755, 0, 0, "root", "root"),
            AGENT_ENV_EXAMPLE: ("file", 0o664, 0, 0, "root", "root"),
            "./usr/local/lib": ("dir", 0o755, 0, 0, "root", "root"),
            "./usr/local/lib/systemd": ("dir", 0o755, 0, 0, "root", "root"),
            "./usr/local/lib/systemd/system": ("dir", 0o755, 0, 0, "root", "root"),
            AGENT_SERVICE: ("file", 0o664, 0, 0, "root", "root"),
            "./var": ("dir", 0o755, 0, 0, "root", "root"),
            "./var/lib": ("dir", 0o755, 0, 0, "root", "root"),
            AGENT_STATE_DIR: ("dir", 0o750, 0, 0, "woodpecker", "woodpecker"),
        })
    return layout


def _check_data_layout(
    name: str, members: list[tuple[tarfile.TarInfo, bytes | None]]
) -> dict[str, tuple[tarfile.TarInfo, bytes | None]]:
    """Require the exact official payload paths, types, modes, and owners."""
    expected = _expected_data_layout(name)
    found: dict[str, tuple[tarfile.TarInfo, bytes | None]] = {}
    for info, body in members:
        normalized = _normalized(info.name)
        if normalized in found:
            raise ValueError(f"{name} official payload repeats {normalized}")
        found[normalized] = (info, body)
    missing = sorted(set(expected) - set(found))
    extra = sorted(set(found) - set(expected))
    if missing or extra:
        raise ValueError(
            f"{name} official payload path set changed; missing "
            f"{missing or 'none'}, unexpected {extra or 'none'}"
        )
    checked: dict[str, tuple[tarfile.TarInfo, bytes | None]] = {}
    for path, (kind, mode, uid, gid, uname, gname) in expected.items():
        info, body = found[path]
        if kind == "dir" and not info.isdir():
            raise ValueError(f"{name} official payload {path} is not a directory")
        if kind == "file" and not info.isreg():
            raise ValueError(f"{name} official payload {path} is not a regular file")
        if info.mode & 0o7777 != mode:
            raise ValueError(
                f"{name} official payload {path} mode is "
                f"{info.mode & 0o7777:04o}, expected {mode:04o}"
            )
        if (info.uid, info.gid) != (uid, gid):
            raise ValueError(
                f"{name} official payload {path} numeric owner is "
                f"{info.uid}/{info.gid}, expected {uid}/{gid}"
            )
        if (info.uname, info.gname) != (uname, gname):
            raise ValueError(
                f"{name} official payload {path} owner is "
                f"{info.uname}/{info.gname}, expected {uname}/{gname}"
            )
        if kind == "file":
            if not body:
                raise ValueError(f"{name} official payload {path} is empty")
            checked[path] = (info, body)
    for path, expected_bytes in (
        (AGENT_SERVICE, vendored("nfpm/woodpecker-agent.service")),
        (AGENT_ENV_EXAMPLE, vendored("nfpm/woodpecker-agent.env.example")),
    ):
        if name != "woodpecker-agent":
            continue
        body = checked[path][1]
        if body != expected_bytes:
            raise ValueError(f"{name} official payload {path} content changed")
    binary_path = f"./{OFFICIAL_METADATA[name]['installed'].lstrip('/')}"
    _check_amd64_elf(name, checked[binary_path][1] or b"")
    return found


def _check_amd64_elf(name: str, payload: bytes) -> None:
    """Require the official amd64 payload binary to be an ELF x86-64."""
    if len(payload) < 20 or payload[:4] != b"\x7fELF" or payload[5] != 1:
        raise ValueError(f"{name} official payload binary is not a 64-bit ELF")
    machine = int.from_bytes(payload[18:20], "little")
    if machine != 62:
        raise ValueError(
            f"{name} official payload binary is e_machine {machine}, expected x86-64"
        )


def _check_control_layout(
    name: str,
    members: list[tuple[tarfile.TarInfo, bytes | None]],
    data: dict[str, tuple[tarfile.TarInfo, bytes | None]],
) -> None:
    """Require the exact reviewed control members, modes, and scripts."""
    expected = {
        name: {
            "./control": 0o644,
            "./md5sums": 0o644,
            **({AGENT_PREINST: 0o755} if name == "woodpecker-agent" else {}),
        }
    }[name]
    found: dict[str, tuple[tarfile.TarInfo, bytes | None]] = {}
    for info, body in members:
        normalized = _normalized(info.name)
        if normalized in found:
            raise ValueError(f"{name} official control repeats {normalized}")
        found[normalized] = (info, body)
    if set(found) != set(expected):
        raise ValueError(
            f"{name} official control members changed: "
            f"{sorted(set(found) ^ set(expected))}"
        )
    for path, mode in expected.items():
        info, body = found[path]
        if not info.isreg() or body is None:
            raise ValueError(f"{name} official control {path} is not a regular file")
        if info.mode & 0o7777 != mode:
            raise ValueError(
                f"{name} official control {path} mode is "
                f"{info.mode & 0o7777:04o}, expected {mode:04o}"
            )
        if (info.uid, info.gid) != (0, 0):
            raise ValueError(
                f"{name} official control {path} numeric owner is "
                f"{info.uid}/{info.gid}, expected 0/0"
            )
    if name == "woodpecker-agent":
        if found[AGENT_PREINST][1] != vendored(
            "nfpm/woodpecker-system-user.preinstall.sh"
        ):
            raise ValueError(
                "woodpecker-agent official preinstall script changed; review it"
            )
    _verify_md5sums(name, found["./md5sums"][1] or b"", data)


def _verify_md5sums(
    name: str,
    payload: bytes,
    data: dict[str, tuple[tarfile.TarInfo, bytes | None]],
) -> None:
    """Require md5sums to list exactly the official payload files correctly."""
    expected = {
        path: hashlib.md5(body or b"").hexdigest()
        for path, (info, body) in data.items()
        if info.isreg()
    }
    declared: dict[str, str] = {}
    for line in payload.decode("utf-8").splitlines():
        digest, separator, path = line.partition("  ")
        if not separator or len(digest) != 32 or path in declared:
            raise ValueError(f"{name} official md5sums is malformed")
        declared[path] = digest
    if declared != expected:
        raise ValueError(
            f"{name} official md5sums does not cover the official payload exactly"
        )


def _repack_control(
    control_name: str, members: list[tuple[tarfile.TarInfo, bytes | None]], rewrite
) -> bytes:
    entries: list[PayloadEntry] = []
    seen_control = False
    for info, body in members:
        normalized = _normalized(info.name)
        if normalized.startswith("./postinst") or normalized.startswith("./prerm") or (
            normalized.startswith("./postrm")
        ):
            raise ValueError(f"official package unexpectedly carries {normalized}")
        if not info.isreg():
            raise ValueError(f"unsupported official control member: {normalized}")
        payload = body or b""
        if normalized == "./control":
            payload = rewrite(payload.decode("utf-8")).encode("utf-8")
            seen_control = True
        entries.append(
            PayloadEntry(normalized, payload, info.mode, info.uname, info.gname)
        )
    if not seen_control:
        raise ValueError("official package control archive has no control file")
    return _tar_gz(entries)


def repack_official_deb(
    *,
    package: Package,
    arch: str,
    manifest: Manifest,
    source: Path,
    output: Path,
    expected_sha256: str,
) -> None:
    """Repack the official Debian package, changing only its control Version."""
    if arch != "amd64":
        raise ValueError("only the reviewed amd64 official Debian package is repacked")
    if package.name not in PACKAGES:
        raise ValueError(f"{package.name} has no reviewed official Debian package")
    if expected_sha256 and sha256_file(source) != expected_sha256:
        raise ValueError(f"{package.name} official source digest changed: {source}")
    members = _read_ar(source)
    if members.get("debian-binary") != b"2.0\n":
        raise ValueError(f"{package.name} official package is not format 2.0")
    control_name = next((name for name in members if name.startswith("control.tar")), None)
    data_name = next((name for name in members if name.startswith("data.tar")), None)
    if control_name is None or data_name is None:
        raise ValueError(f"{package.name} official package has no control or data member")
    control_members = _read_tar(members[control_name], name=control_name)
    data_members = _read_tar(members[data_name], name=data_name)
    data = _check_data_layout(package.name, data_members)
    _check_control_layout(package.name, control_members, data)
    control_text = next(
        (
            body
            for info, body in control_members
            if _normalized(info.name) == "./control"
        ),
        None,
    )
    if control_text is None:
        raise ValueError(f"{package.name} official package has no control file")
    version = debian_version(package, manifest)
    rewritten = _rewrite_control(
        control_text.decode("utf-8"),
        name=package.name,
        arch=arch,
        upstream_version=package.version,
        version=version,
    )
    control_tar = _repack_control(
        control_name,
        control_members,
        lambda _text: rewritten,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(
        _ar_bytes(
            [
                ("debian-binary", members["debian-binary"]),
                (control_name, control_tar),
                (data_name, members[data_name]),
            ]
        )
    )


def _package_control(
    *, package: Package, arch: str, manifest: Manifest, installed_size_kb: int
) -> str:
    metadata = OFFICIAL_METADATA[package.name]
    return (
        f"Package: {package.name}\n"
        f"Version: {debian_version(package, manifest)}\n"
        f"Section: {metadata['Section']}\n"
        f"Priority: {metadata['Priority']}\n"
        f"Architecture: {arch}\n"
        f"License: {metadata['License']}\n"
        f"Maintainer: {metadata['Maintainer']}\n"
        f"Installed-Size: {installed_size_kb}\n"
        f"Homepage: {metadata['Homepage']}\n"
        f"Description: {metadata['Description']}\n"
    )


def build_arm64_package(
    *,
    package: Package,
    arch: str,
    manifest: Manifest,
    binary_source: Path,
    output: Path,
) -> None:
    """Assemble the reviewed arm64 package from the official binary and files."""
    if arch != "arm64":
        raise ValueError("the assembled Woodpecker package is the arm64 target")
    if package.name not in PACKAGES:
        raise ValueError(f"{package.name} has no reviewed Woodpecker layout")
    binary = binary_source.read_bytes()
    binary_dir = binary_dir_for(package.name)
    installed = f"./{binary_dir}/{package.name}"
    data: list[PayloadEntry] = []
    for directory in _parent_dirs(installed):
        data.append(PayloadEntry(directory, None, 0o755))
    data.append(PayloadEntry(installed, binary, 0o755))
    if package.name == "woodpecker-agent":
        data.extend(
            [
                PayloadEntry("./etc", None, 0o755),
                PayloadEntry("./etc/woodpecker", None, 0o755),
                PayloadEntry(
                    AGENT_ENV_EXAMPLE,
                    vendored("nfpm/woodpecker-agent.env.example"),
                    0o664,
                ),
                PayloadEntry("./usr/local/lib", None, 0o755),
                PayloadEntry("./usr/local/lib/systemd", None, 0o755),
                PayloadEntry(
                    "./usr/local/lib/systemd/system", None, 0o755
                ),
                PayloadEntry(
                    AGENT_SERVICE,
                    vendored("nfpm/woodpecker-agent.service"),
                    0o664,
                ),
                PayloadEntry("./var", None, 0o755),
                PayloadEntry("./var/lib", None, 0o755),
                PayloadEntry(
                    AGENT_STATE_DIR, None, 0o750, "woodpecker", "woodpecker"
                ),
            ]
        )
    data = sorted(data, key=lambda entry: entry.name)
    total = sum(len(entry.payload or b"") for entry in data)
    control_files = [
        PayloadEntry(
            "./control",
            _package_control(
                package=package,
                arch=arch,
                manifest=manifest,
                installed_size_kb=max(1, total // 1024),
            ).encode("utf-8"),
            0o644,
        ),
        PayloadEntry("./md5sums", _md5sums(data), 0o644),
    ]
    if package.name == "woodpecker-agent":
        control_files.append(
            PayloadEntry(
                AGENT_PREINST,
                vendored("nfpm/woodpecker-system-user.preinstall.sh"),
                0o755,
            )
        )
    assemble_deb(output, control_files=control_files, data_entries=data)


def _parent_dirs(path: str) -> list[str]:
    parts = path.strip("./").split("/")[:-1]
    return ["./" + "/".join(parts[: index + 1]) for index in range(len(parts))]


def _md5sums(entries: list[PayloadEntry]) -> bytes:
    lines = [
        f"{hashlib.md5(entry.payload or b'').hexdigest()}  {entry.name[2:]}\n"
        for entry in entries
        if entry.payload is not None
    ]
    return "".join(lines).encode("utf-8")


def check_pool_package(
    *, package: Package, arch: str, manifest: Manifest, deb: Path, root: Path
) -> list[str]:
    """Check the official payload, control metadata, and maintainer scripts."""
    from .smoke_package import control_fields

    fields = control_fields(deb)
    metadata = OFFICIAL_METADATA[package.name]
    expected = {
        "Package": package.name,
        "Version": debian_version(package, manifest),
        "Architecture": arch,
        "Section": metadata["Section"],
        "Priority": metadata["Priority"],
        "License": metadata["License"],
        "Maintainer": metadata["Maintainer"],
        "Homepage": metadata["Homepage"],
        "Description": metadata["Description"],
    }
    for key, value in expected.items():
        if fields.get(key) != value:
            raise SmokeError(
                f"{deb.name} declares {key}: {fields.get(key)!r}, expected {value!r}"
            )
    installed = f"{binary_dir_for(package.name)}/{package.name}"
    binary = root / installed
    if not binary.is_file():
        raise SmokeError(f"{deb.name} is missing /{installed}")
    machine = elf_machine(binary)
    expected_machine = "x86-64" if arch == "amd64" else "aarch64"
    if machine != expected_machine:
        raise SmokeError(f"{binary} is {machine}, expected {expected_machine}")
    listing = _listing(deb)
    if listing.get(f"./{installed}") != "rwxr-xr-x":
        raise SmokeError(f"{deb.name} does not install /{installed} as 0755")
    if any(path.startswith("./usr/bin/") for path in listing):
        raise SmokeError(f"{deb.name} unexpectedly installs under /usr/bin")
    if any(path.startswith("./usr/share/doc/") for path in listing):
        raise SmokeError(f"{deb.name} unexpectedly installs /usr/share/doc")
    if package.name == "woodpecker-agent":
        for path, mode in (
            (AGENT_SERVICE, "rw-rw-r--"),
            (AGENT_ENV_EXAMPLE, "rw-rw-r--"),
            (AGENT_STATE_DIR + "/", "rwxr-x---"),
        ):
            if listing.get(path) != mode:
                raise SmokeError(
                    f"{deb.name} does not carry {path} as {mode}"
                )
        _check_scripts(deb, expected_preinst=vendored(
            "nfpm/woodpecker-system-user.preinstall.sh"
        ))
    else:
        _check_scripts(deb, expected_preinst=None)
    return [
        f"{deb.name}: {package.name} {fields.get('Version')} {arch} installs /{installed}"
    ]


def _listing(deb: Path) -> dict[str, str]:
    import subprocess

    proc = subprocess.run(
        ["dpkg-deb", "-c", str(deb)], text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if proc.returncode != 0:
        raise SmokeError(f"dpkg-deb -c {deb.name} failed: {proc.stderr.strip()}")
    result: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        columns = line.split()
        if len(columns) < 6:
            continue
        result[columns[-1]] = columns[0][1:]
    return result


def check_legacy_pool_package(
    *, package: Package, arch: str, manifest: Manifest, deb: Path, root: Path
) -> list[str]:
    """Validate a retained pre-official package from a trusted baseline."""
    from .smoke_package import check_copyright, control_fields

    fields = control_fields(deb)
    expected = {
        "Package": package.name,
        "Version": debian_version(package, manifest),
        "Architecture": arch,
    }
    for key, value in expected.items():
        if fields.get(key) != value:
            raise SmokeError(
                f"{deb.name} declares {key}: {fields.get(key)!r}, expected {value!r}"
            )
    if fields.get("Depends", "") != package.targets[arch].depends:
        raise SmokeError(f"{deb.name} declares unexpected Depends")
    binary = root / "usr" / "bin" / package.name
    if not binary.is_file():
        raise SmokeError(f"{deb.name} is missing /usr/bin/{package.name}")
    machine = elf_machine(binary)
    expected_machine = "x86-64" if arch == "amd64" else "aarch64"
    if machine != expected_machine:
        raise SmokeError(f"{binary} is {machine}, expected {expected_machine}")
    listing = _listing(deb)
    for path, mode in (
        (f"./usr/bin/{package.name}", "rwxr-xr-x"),
        (f"./usr/share/doc/{package.name}/copyright", "rw-r--r--"),
        (f"./usr/share/doc/{package.name}/changelog.Debian", "rw-r--r--"),
    ):
        if listing.get(path) != mode:
            raise SmokeError(f"{deb.name} does not carry {path} as {mode}")
    if any(path.startswith("./usr/local/") for path in listing):
        raise SmokeError(f"{deb.name} unexpectedly installs under /usr/local")
    _check_scripts(deb, expected_preinst=None)
    check_copyright(package, root)
    return [
        f"{deb.name}: retained legacy {package.name} {fields.get('Version')} {arch} "
        f"installs /usr/bin/{package.name}"
    ]


def _check_scripts(deb: Path, *, expected_preinst: bytes | None) -> None:
    with tempfile.TemporaryDirectory(prefix="woodpecker-control-") as scratch:
        proc = subprocess.run(
            ["dpkg-deb", "-e", str(deb), scratch], text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if proc.returncode != 0:
            raise SmokeError(f"dpkg-deb -e {deb.name} failed: {proc.stderr.strip()}")
        control = Path(scratch)
        scripts = {
            name: (control / name).read_bytes()
            for name in ("preinst", "postinst", "prerm", "postrm")
            if (control / name).is_file()
        }
    if expected_preinst is None:
        if scripts:
            raise SmokeError(f"{deb.name} unexpectedly carries {sorted(scripts)}")
        return
    if set(scripts) != {"preinst"}:
        raise SmokeError(f"{deb.name} carries unexpected control scripts: {sorted(scripts)}")
    if scripts["preinst"] != expected_preinst:
        raise SmokeError(f"{deb.name} preinst does not match the reviewed official script")
