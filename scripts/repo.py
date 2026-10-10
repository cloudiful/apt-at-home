"""Local repository CLI: build, verify, and publish without cloud writes by default."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from .fingerprint import expected_fingerprint_from_env
from .manifest import parse_manifest
from .publish import publish_tree
from .publish_guard import guard_publish
from .release_build import build_release
from .release_manifest import MANIFEST_NAME, load_manifest_data
from .storage import s3_source
from .verify import verify_tree


def _default_output() -> Path:
    return Path("dist/repo")


def _release_id(args_release: str | None) -> str:
    if args_release:
        return args_release
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _key_material_from_env() -> tuple[Path | None, str | None]:
    key_file = os.environ.get("APT_SIGNING_KEY_FILE")
    key_id = os.environ.get("APT_SIGNING_KEY_ID")
    return (Path(key_file) if key_file else None, key_id)


def cmd_build(args: argparse.Namespace) -> int:
    output: Path = Path(args.output)
    env_key_file, env_key_id = _key_material_from_env()
    signing_key = Path(args.signing_key_file) if args.signing_key_file else env_key_file
    key_id = args.key_id or env_key_id
    if signing_key is not None and not signing_key.exists():
        print(f"signing key file not found: {signing_key}", file=sys.stderr)
        return 1
    release_id = _release_id(args.release_id)
    cache_dir = Path(args.cache_dir) if args.cache_dir else output.parent / "cache" / "upstream"
    try:
        debs = build_release(
            output_root=output,
            release_id=release_id,
            cache_dir=cache_dir,
            manifest_path=Path(args.manifest) if args.manifest else None,
            signing_key_file=signing_key,
            key_id=key_id,
        )
    except Exception as exc:
        print(f"build failed: {exc}", file=sys.stderr)
        return 1
    print(f"built release {release_id} with {len(debs)} packages")
    for deb in sorted(debs):
        print(f"  pool/{deb.name}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    output: Path = Path(args.output)
    key_file = Path(args.key_file) if args.key_file else None
    expected: str | None = None
    if os.environ.get("APT_SIGNING_FINGERPRINT", "").strip():
        try:
            expected = expected_fingerprint_from_env()
        except Exception as exc:
            print(f"verify failed: {exc}", file=sys.stderr)
            return 1
    try:
        findings = verify_tree(output, key_file=key_file, expected_fingerprint=expected)
    except Exception as exc:
        print(f"verify failed: {exc}", file=sys.stderr)
        return 1
    for line in findings:
        print(f"ok: {line}")
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    output: Path = Path(args.output)
    try:
        pointer = json.loads((output / "current.json").read_text(encoding="utf-8"))
        release_id = pointer["release"] if isinstance(pointer, dict) else None
    except Exception as exc:
        print(f"cannot read current.json: {exc}", file=sys.stderr)
        return 1
    if not isinstance(release_id, str):
        print("current.json does not select a release", file=sys.stderr)
        return 1
    bucket = args.bucket or os.environ.get("APT_S3_BUCKET", "")
    if not bucket:
        print("bucket is required (--bucket or APT_S3_BUCKET)", file=sys.stderr)
        return 1
    try:
        expected = expected_fingerprint_from_env()
    except Exception as exc:
        print(f"publish failed: {exc}", file=sys.stderr)
        return 1
    try:
        candidate = parse_manifest(
            load_manifest_data(output / "releases" / release_id / MANIFEST_NAME)
        )
        guard_publish(
            candidate=candidate,
            source=s3_source(bucket, endpoint=args.endpoint or None),
            fingerprint=expected,
            allow_key_rotation=bool(args.allow_key_rotation),
        )
    except Exception as exc:
        print(f"publish refused: {exc}", file=sys.stderr)
        return 1
    try:
        uploaded = publish_tree(
            output_root=output,
            bucket=bucket,
            release_id=release_id,
            endpoint=args.endpoint or None,
            allow_key_rotation=bool(args.allow_key_rotation),
            expected_fingerprint=expected,
        )
    except Exception as exc:
        print(f"publish failed: {exc}", file=sys.stderr)
        return 1
    print(f"published release {release_id} ({len(uploaded)} objects, pointer last)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.repo",
        description="Build, verify, and publish the private APT repository (local only by default).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="download pinned archives and build signed metadata")
    build.add_argument("--output", default=str(_default_output()))
    build.add_argument("--manifest", default=None)
    build.add_argument("--release-id", default=None)
    build.add_argument("--cache-dir", default=None)
    build.add_argument("--signing-key-file", default=None, help="armored private key file (path only)")
    build.add_argument("--key-id", default=None)
    build.set_defaults(func=cmd_build)

    verify = sub.add_parser("verify", help="verify hashes and signatures of a built tree")
    verify.add_argument("--output", default=str(_default_output()))
    verify.add_argument("--key-file", default=None)
    verify.set_defaults(func=cmd_verify)

    publish = sub.add_parser("publish", help="publish a verified tree (manual, serialized)")
    publish.add_argument("--output", default=str(_default_output()))
    publish.add_argument("--bucket", default=None)
    publish.add_argument("--endpoint", default=None)
    publish.add_argument("--allow-key-rotation", action="store_true")
    publish.set_defaults(func=cmd_publish)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
