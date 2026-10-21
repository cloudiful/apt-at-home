"""Shared test helpers: ephemeral GPG keys, an in-memory S3 fake, and fake archives."""

from __future__ import annotations

import io
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path

from scripts.archive_tree import inventory_tree
from scripts.catalog import archive_suffix_for, parsers_for, required_paths_for
from scripts.deb import stage_package
from scripts.manifest import Manifest, Package


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


def archive_stem(name: str) -> str:
    """Return a cached archive file name without its reviewed archive suffix.

    The suffix follows the official asset, so a cached file is a tarball, a
    `.tgz`, or a ZIP; the stem is the `<package>-<version>-<arch>` part.
    """
    for suffix in (".tar.gz", ".tgz", ".zip"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    raise ValueError(f"unsupported release archive name: {name}")


def write_target_archive(dest: Path, pkg: Package, arch: str, payload: bytes) -> Path:
    """Write the fake upstream archive for one pinned target, flat or tree-shaped.

    The archive keeps the format the official asset uses, so a ZIP-backed
    package is exercised through the ZIP path.
    """
    if arch not in pkg.targets:
        raise ValueError(f"{pkg.name} publishes no {arch} release asset")
    target = pkg.targets[arch]
    if target.install_tree:
        parser_paths = tuple(f"lib/nvim/parser/{parser}.so" for parser in parsers_for(pkg.name))
        required = tuple(dict.fromkeys(required_paths_for(pkg.name) + parser_paths))
        return write_fake_tree_archive(
            dest,
            top=target.install_tree,
            required=required,
            payload=payload,
        )
    if archive_suffix_for(pkg.name, pkg.version, arch) == ".zip":
        return write_fake_zip(
            dest, member=target.archive_member, payload=payload,
            additional_members=tuple(target.additional_binaries),
        )
    return write_fake_archive(
        dest, member=target.archive_member, payload=payload,
        additional_members=tuple(target.additional_binaries),
    )


def stage_fixture_target(
    root: Path, pkg: Package, arch: str, manifest: Manifest, payload: bytes, stage_root: Path
) -> Path:
    """Stage one target from the payload its manifest declares, without network.

    A flat target stages its fake binary; a tree target stages a fake archive of its
    required paths, so the fixture matches the production payload shape exactly.
    Only an architecture in `pkg.targets` may be staged: cargo-tauri is AMD-only,
    so requesting its absent ARM target is refused rather than invented.
    """
    if arch not in pkg.targets:
        raise ValueError(f"{pkg.name} publishes no {arch} release asset")
    target = pkg.targets[arch]
    archive = write_target_archive(
        root / "src" / f"{pkg.name}-{arch}{archive_suffix_for(pkg.name, pkg.version, arch)}",
        pkg, arch, payload,
    )
    if target.install_tree:
        inventory = inventory_tree(archive, target.install_tree)
        return stage_package(
            pkg=pkg, arch=arch, manifest=manifest, stage_root=stage_root,
            tree=(archive, inventory, required_paths_for(pkg.name)),
        )
    source_root = root / "src" / f"{pkg.name}-{arch}"
    binary = source_root / target.binary
    source_root.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(payload)
    binary.chmod(0o755)
    additional: dict[str, Path] = {}
    for name in target.additional_binaries.values():
        source = source_root / name
        source.write_bytes(payload)
        source.chmod(0o755)
        additional[name] = source
    return stage_package(
        pkg=pkg, arch=arch, manifest=manifest, binary_file=binary,
        additional_binary_files=additional, stage_root=stage_root,
    )


def fixture_targets(manifest: Manifest) -> list[tuple[Package, str]]:
    """Every (package, arch) the manifest actually declares, in stable order.

    cargo-tauri contributes only its AMD target; every other package contributes
    both. Fixture builders must iterate this rather than a global arch tuple,
    so the pool holds 12 AMD and 11 ARM debs for the current catalog.
    """
    return [
        (pkg, arch)
        for pkg in sorted(manifest.packages, key=lambda p: p.name)
        for arch in sorted(pkg.targets)
    ]


def make_tree_archive(path: Path, entries: list[tuple[str, str, object, int]]) -> Path:
    """Write a tarball from (kind, name, payload-or-target, mode) entries.

    `kind` is file, dir, symlink, hardlink, or fifo; a file's payload is bytes and a
    link's payload is its target. Used to build both valid and hostile archives.
    """
    with tarfile.open(path, "w:gz") as tar:
        for kind, name, payload, mode in entries:
            info = tarfile.TarInfo(name)
            if kind == "file":
                info.size = len(payload)  # type: ignore[arg-type]
                info.mode = mode
                tar.addfile(info, io.BytesIO(payload))  # type: ignore[arg-type]
            elif kind == "dir":
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = payload  # type: ignore[assignment]
                tar.addfile(info)
            elif kind == "hardlink":
                info.type = tarfile.LNKTYPE
                info.linkname = payload  # type: ignore[assignment]
                tar.addfile(info)
            elif kind == "fifo":
                info.type = tarfile.FIFOTYPE
                tar.addfile(info)
    return path


def valid_tree_entries(
    top: str, extra: list[tuple[str, str, object, int]] | None = None
) -> list[tuple[str, str, object, int]]:
    """A minimal reviewed neovim-shaped layout under `top`, plus any extra entries."""
    return [
        ("dir", f"{top}/", None, 0o755),
        ("file", f"{top}/bin/nvim", b"nvim", 0o755),
        ("file", f"{top}/lib/nvim/parser/lua.so", b"lua", 0o644),
        ("file", f"{top}/share/nvim/runtime/doc/help.txt", b"help", 0o644),
        ("file", f"{top}/share/nvim/runtime/scripts/less.sh", b"#!/bin/sh", 0o755),
        *(extra or []),
    ]


def write_fake_archive(
    dest: Path, *, member: str, payload: bytes, additional_members: tuple[str, ...] = ()
) -> Path:
    """Write a gzipped tar holding the exact pinned flat command members."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dest, "w:gz") as tar:
        for name in (member, *additional_members):
            _add_file(tar, name, payload, mode=0o755)
    return dest


def write_fake_zip(
    dest: Path, *, member: str, payload: bytes, additional_members: tuple[str, ...] = ()
) -> Path:
    """Write a ZIP holding the exact pinned flat command members.

    The archive mirrors an official release ZIP: the member keeps its directory
    prefix, and the prefix is stored as a directory entry beside it.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as archive:
        prefix = member.rsplit("/", 1)[0] if "/" in member else ""
        if prefix:
            directory = zipfile.ZipInfo(f"{prefix}/")
            directory.external_attr = (0o040755 << 16) | 0o10
            directory.compress_type = zipfile.ZIP_STORED
            archive.writestr(directory, b"")
        for name in (member, *additional_members):
            info = zipfile.ZipInfo(name)
            info.external_attr = 0o100755 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, payload)
    return dest


def write_fake_tree_archive(dest: Path, *, top: str, required: tuple[str, ...], payload: bytes) -> Path:
    """Write a gzipped tar whose `top` root carries every required tree path.

    Each required path is a regular file, so the validator sees the same layout a
    real tree package does without shipping upstream bytes into the repository.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dest, "w:gz") as tar:
        for relative in required:
            _add_file(tar, f"{top}/{relative}", payload, mode=0o755 if relative.startswith("bin/") else 0o644)
    return dest


def _add_file(tar: tarfile.TarFile, name: str, payload: bytes, *, mode: int) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mode = mode
    info.mtime = 0
    tar.addfile(info, io.BytesIO(payload))


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
