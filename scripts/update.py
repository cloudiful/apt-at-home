"""Update CLI: discover new stable releases against the published baseline.

The default is a dry run that only reads and plans: it restores the trusted
metadata, compares it with the latest stable upstream releases, and prints what
an update would change. No key is imported, nothing is built, signed, or
uploaded unless `--publish` is passed, and an unchanged plan never publishes.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .catalog import RECIPES
from .fingerprint import expected_fingerprint_from_env
from .manifest import default_manifest_path
from .release_manifest import validated_document
from .release_verify import (
    BaselineUnavailable,
    NoCurrentRelease,
    TrustedRelease,
    restore_metadata,
)
from .repo import _release_id as new_release_id
from .storage import Source, s3_source
from .update_build import apply_update
from .update_plan import UpdatePlan, build_plan
from .upstream import Candidate, client_from_env, discover

def _default_output() -> Path:
    return Path("dist/update-repo")


def _fingerprint() -> str:
    try:
        return expected_fingerprint_from_env()
    except Exception as exc:
        print(f"update requires APT_SIGNING_FINGERPRINT: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def _catalog() -> dict[str, object]:
    """Return the validated committed catalog this repository may publish."""
    data, _ = validated_document(default_manifest_path())
    return data


def _load_baseline(source: Source, *, fingerprint: str) -> tuple[TrustedRelease | None, dict[str, object]]:
    """Return the trusted baseline release and the manifest document to compare against.

    Only a confirmed missing pointer bootstraps from the committed manifest. Any
    published release must carry the baseline manifest its signed Release binds,
    and a missing or mismatched binding is refused by the trust chain.
    """
    try:
        restored = restore_metadata(source, fingerprint=fingerprint)
    except NoCurrentRelease:
        return None, _catalog()
    return restored, restored.manifest_data


def _discover(client) -> dict[str, Candidate]:  # type: ignore[no-untyped-def]
    """Discover the latest stable release of every reviewed package."""
    candidates: dict[str, Candidate] = {}
    for name in sorted(RECIPES):
        candidates[name] = discover(client, name)
    return candidates


def _report(plan: UpdatePlan) -> None:
    where = plan.baseline_release_id or "committed manifest (fresh storage)"
    print(f"baseline: {where}")
    for name, version in sorted(plan.versions.items()):
        print(f"  {name} {version}")
    if plan.added:
        print(f"adding to the catalog: {', '.join(plan.added)}")
    if plan.is_noop:
        print("no updates: every package is already at its latest stable release")
    else:
        print(f"would rebuild: {', '.join(plan.changed)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.update",
        description="Compare the published release with latest stable upstream versions.",
    )
    parser.add_argument("--output", default=str(_default_output()), help="local build tree (default: dist/update-repo)")
    parser.add_argument("--bucket", default=None, help="R2 bucket (required with --publish)")
    parser.add_argument("--endpoint", default=None, help="S3 endpoint override (default: APT_S3_ENDPOINT)")
    parser.add_argument("--release-id", default=None, help="must be unique; defaults to a UTC timestamp")
    parser.add_argument("--dry-run", action="store_true", help="default; plan only, never writes")
    parser.add_argument("--publish", action="store_true", help="build, sign, and publish the update")
    parser.add_argument("--signing-key-file", default=None, help="armored private key file (path only)")
    parser.add_argument("--key-id", default=None, help="signing key id (default: APT_SIGNING_KEY_ID)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    fingerprint = _fingerprint()
    bucket = args.bucket or os.environ.get("APT_S3_BUCKET", "")
    if args.publish and not bucket:
        print("--publish requires --bucket or APT_S3_BUCKET", file=sys.stderr)
        return 1

    source = s3_source(bucket, endpoint=args.endpoint)
    try:
        baseline, baseline_data = _load_baseline(source, fingerprint=fingerprint)
        plan = build_plan(
            baseline_release_id=baseline.release_id if baseline else None,
            baseline_data=baseline_data,
            catalog_data=_catalog(),
            candidates=_discover(client_from_env()),
        )
    except (BaselineUnavailable, RuntimeError, ValueError, OSError) as exc:
        print(f"update refused: {exc}", file=sys.stderr)
        return 1
    _report(plan)

    if plan.is_noop:
        print("nothing to publish")
        return 0
    if not args.publish:
        print("dry run: no key imported, nothing built, signed, or uploaded")
        return 0

    raw_key = args.signing_key_file or os.environ.get("APT_SIGNING_KEY_FILE")
    key_file = Path(raw_key) if raw_key else None
    if key_file is None or not key_file.is_file():
        print("--publish requires an existing --signing-key-file", file=sys.stderr)
        return 1
    output = Path(args.output)
    try:
        result = apply_update(
            plan=plan,
            release=baseline,
            source=source,
            output_root=output,
            cache_dir=output.parent / "cache" / "upstream",
            release_id=new_release_id(args.release_id),
            bucket=bucket,
            endpoint=args.endpoint,
            fingerprint=fingerprint,
            signing_key_file=key_file,
            key_id=args.key_id or os.environ.get("APT_SIGNING_KEY_ID"),
        )
    except Exception as exc:
        print(f"update failed: {exc}", file=sys.stderr)
        return 1
    print(f"published release {result.release_id} ({len(result.uploaded)} objects, pointer last)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
