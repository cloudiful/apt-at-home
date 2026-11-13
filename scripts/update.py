"""Update CLI: discover new stable releases against the published baseline.

The default is a dry run that only reads and plans: it restores the trusted
metadata, compares it with the latest stable upstream releases, and prints what
an update would change. No key is imported, nothing is built, signed, or
uploaded unless `--publish` is passed, and an unchanged plan never publishes.
One explicit exception to "never writes": an explicit `--diagnostics PATH` also
writes that JSON report in a dry run and on every fatal outcome.

The default remains strict: one package that cannot be planned stops the run.
`--keep-blocked` opts a scheduled run into retaining an already-published
package at its trusted baseline when only its own candidate failed, while every
other package still plans normally; the retained packages are reported as
deferred instead of a clean success. A deferred run still exits 0 so reporting
can run, and the report's `--check-deferred` gate turns any deferred package
into a nonzero (3) result.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError

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
from .update_plan import Deferred, UpdatePlan, build_plan
from .update_report import report_document, sanitize_detail, write_report
from .upstream import Candidate, client_from_env, discover


def _default_output() -> Path:
    return Path("dist/update-repo")


def _fingerprint() -> str:
    try:
        return expected_fingerprint_from_env()
    except Exception as exc:
        print(
            f"update requires APT_SIGNING_FINGERPRINT: {sanitize_detail(str(exc))}",
            file=sys.stderr,
        )
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


def _discover(
    client,  # type: ignore[no-untyped-def]
    *,
    keep_blocked: bool = False,
) -> tuple[dict[str, Candidate], dict[str, str]]:
    """Discover the latest stable release of every reviewed package.

    Production is asset-only: the canonical stable CLI tag for the
    multiproduct tauri-apps/tauri repository is selected from its tag list
    inside `upstream.latest_stable`, never from repository-wide latest for
    another product line.

    Only a per-package ValueError can be retained: HTTP, authentication,
    network, and storage failures are not ValueErrors and still abort the run.
    """
    candidates: dict[str, Candidate] = {}
    blocked: dict[str, str] = {}
    for name in sorted(RECIPES):
        try:
            candidates[name] = discover(client, name)
        except ValueError as exc:
            if not keep_blocked:
                raise
            blocked[name] = str(exc)
    if not candidates:
        raise ValueError("no upstream candidates were discovered")
    return candidates, blocked


def _report(plan: UpdatePlan) -> None:
    where = plan.baseline_release_id or "committed manifest (fresh storage)"
    print(f"baseline: {where}")
    for name, version in sorted(plan.versions.items()):
        print(f"  {name} {version}")
    if plan.added:
        print(f"adding to the catalog: {', '.join(plan.added)}")
    for item in plan.deferred:
        print(
            f"deferred: {item.name} ({item.stage}) retaining {item.retained_version}: "
            f"{sanitize_detail(item.detail)}"
        )
    if plan.is_noop:
        if plan.deferred:
            print(
                f"no updates to publish: {len(plan.deferred)} package(s) deferred, "
                "the retained packages are unchanged"
            )
        else:
            print("no updates: every package is already at its latest stable release")
        return
    if plan.changed:
        print(f"would rebuild: {', '.join(plan.changed)}")
    if plan.metadata_changed:
        print(f"metadata only: {', '.join(plan.metadata_changed)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.update",
        description="Compare the published release with latest stable upstream versions.",
    )
    parser.add_argument("--output", default=str(_default_output()), help="local build tree (default: dist/update-repo)")
    parser.add_argument("--bucket", default=None, help="R2 bucket (required with --publish)")
    parser.add_argument("--endpoint", default=None, help="S3 endpoint override (default: APT_S3_ENDPOINT)")
    parser.add_argument("--release-id", default=None, help="must be unique; defaults to a UTC timestamp")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "default; plan only, builds/signs/uploads nothing "
            "(except the report given explicitly to --diagnostics)"
        ),
    )
    parser.add_argument("--publish", action="store_true", help="build, sign, and publish the update")
    parser.add_argument(
        "--keep-blocked",
        action="store_true",
        help=(
            "retain an already-published package at its trusted baseline when only its "
            "candidate failed, and report it as deferred instead of aborting; the run "
            "still exits 0, and the report's --check-deferred gate must fail the job"
        ),
    )
    parser.add_argument(
        "--diagnostics",
        default=None,
        metavar="PATH",
        help="write a JSON update report to PATH, including a dry run and fatal outcomes",
    )
    parser.add_argument("--signing-key-file", default=None, help="armored private key file (path only)")
    parser.add_argument("--key-id", default=None, help="signing key id (default: APT_SIGNING_KEY_ID)")
    return parser


@dataclass
class _Outcome:
    """The facts the optional diagnostics report records about this run."""

    baseline_release_id: str | None = None
    planned_release_id: str | None = None
    confirmed_release_id: str | None = None
    changed: tuple[str, ...] = ()
    metadata_only: tuple[str, ...] = ()
    deferred: tuple[Deferred, ...] = ()
    assessed: tuple[str, ...] | None = None
    published: bool = False
    fatal: str | None = None


def _write_diagnostics(path: Path, outcome: _Outcome, *, dry_run: bool) -> bool:
    """Write the requested report; a failure to write is itself a failed run."""
    document = report_document(
        baseline_release_id=outcome.baseline_release_id,
        planned_release_id=outcome.planned_release_id,
        confirmed_release_id=outcome.confirmed_release_id,
        changed=outcome.changed,
        metadata_only=outcome.metadata_only,
        deferred=outcome.deferred,
        assessed=outcome.assessed,
        published=outcome.published,
        fatal=outcome.fatal,
        dry_run=dry_run,
    )
    try:
        write_report(path, document)
    except (OSError, ValueError) as exc:
        print(f"could not write update diagnostics to {path}: {exc}", file=sys.stderr)
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dry_run = not args.publish
    diagnostics = Path(args.diagnostics) if args.diagnostics else None
    outcome = _Outcome()

    def finish(code: int) -> int:
        if diagnostics is not None and not _write_diagnostics(
            diagnostics, outcome, dry_run=dry_run
        ):
            return 1
        return code

    def fail(exc: Exception) -> int:
        outcome.fatal = sanitize_detail(str(exc))
        print(f"update refused: {sanitize_detail(str(exc))}", file=sys.stderr)
        return finish(1)

    try:
        fingerprint = _fingerprint()
    except SystemExit as exc:
        outcome.fatal = "configuration error: APT_SIGNING_FINGERPRINT is required"
        return finish(exc.code if isinstance(exc.code, int) else 1)

    bucket = args.bucket or os.environ.get("APT_S3_BUCKET", "")
    if args.publish and not bucket:
        outcome.fatal = "--publish requires --bucket or APT_S3_BUCKET"
        print("--publish requires --bucket or APT_S3_BUCKET", file=sys.stderr)
        return finish(1)

    try:
        source = s3_source(bucket, endpoint=args.endpoint)
    except (RuntimeError, ValueError, OSError, BotoCoreError, ClientError) as exc:
        outcome.fatal = sanitize_detail(f"storage configuration error: {exc}")
        print(f"update refused: {sanitize_detail(str(exc))}", file=sys.stderr)
        return finish(1)

    try:
        baseline, baseline_data = _load_baseline(source, fingerprint=fingerprint)
    except (
        BaselineUnavailable,
        RuntimeError,
        ValueError,
        OSError,
        BotoCoreError,
        ClientError,
    ) as exc:
        return fail(exc)
    # The authenticated release identity is known from here on: a later
    # discovery, catalog, or planning failure must still diagnose against it.
    outcome.baseline_release_id = baseline.release_id if baseline else None
    try:
        candidates, blocked = _discover(
            client_from_env(), keep_blocked=args.keep_blocked
        )
        plan = build_plan(
            baseline_release_id=outcome.baseline_release_id,
            baseline_data=baseline_data,
            catalog_data=_catalog(),
            candidates=candidates,
            keep_blocked=args.keep_blocked,
            blocked=blocked,
        )
    except (
        BaselineUnavailable,
        RuntimeError,
        ValueError,
        OSError,
        BotoCoreError,
        ClientError,
    ) as exc:
        return fail(exc)
    outcome.changed = plan.changed
    outcome.metadata_only = plan.metadata_changed
    outcome.deferred = plan.deferred
    outcome.assessed = tuple(pkg.name for pkg in plan.manifest.packages)
    _report(plan)

    if plan.is_noop:
        print("nothing to publish")
        return finish(0)
    if not args.publish:
        print("dry run: no key imported, nothing built, signed, or uploaded")
        return finish(0)

    raw_key = args.signing_key_file or os.environ.get("APT_SIGNING_KEY_FILE")
    key_file = Path(raw_key) if raw_key else None
    if key_file is None or not key_file.is_file():
        outcome.fatal = "--publish requires an existing --signing-key-file"
        print("--publish requires an existing --signing-key-file", file=sys.stderr)
        return finish(1)
    output = Path(args.output)
    release_id = new_release_id(args.release_id)
    outcome.planned_release_id = release_id
    try:
        result = apply_update(
            plan=plan,
            release=baseline,
            source=source,
            output_root=output,
            cache_dir=output.parent / "cache" / "upstream",
            release_id=release_id,
            bucket=bucket,
            endpoint=args.endpoint,
            fingerprint=fingerprint,
            signing_key_file=key_file,
            key_id=args.key_id or os.environ.get("APT_SIGNING_KEY_ID"),
        )
    except Exception as exc:
        outcome.fatal = sanitize_detail(str(exc))
        print(f"update failed: {sanitize_detail(str(exc))}", file=sys.stderr)
        return finish(1)
    outcome.published = True
    outcome.confirmed_release_id = result.release_id
    print(f"published release {result.release_id} ({len(result.uploaded)} objects, pointer last)")
    return finish(0)


if __name__ == "__main__":
    raise SystemExit(main())
