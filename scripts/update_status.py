"""Produce the bounded public update snapshot without exposing diagnostics."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys

from .publish import _s3_client
from .update_report import ReportInvalid, load_report

KEY = "status/update.json"
MAX_BYTES = 256 * 1024
NAME = re.compile(r"[a-z0-9][a-z0-9+.-]{0,62}\Z")
RUN = re.compile(r"[0-9]+\Z")


def public_document(report: dict[str, object] | None, run_id: str, checked_at: str) -> dict[str, object]:
    if not RUN.fullmatch(run_id):
        raise ValueError("invalid Actions run id")
    result: dict[str, object] = {
        "schema_version": 1,
        "checked_at": checked_at,
        "run_url": f"https://github.com/cloudiful/apt-at-home/actions/runs/{run_id}",
        "outcome": "failure",
        "packages": {},
    }
    if report is None or report["fatal"] is not None or report["dry_run"]:
        return result
    assessed = report.get("assessed")
    if not isinstance(assessed, list) or not assessed or any(not NAME.fullmatch(name) for name in assessed):
        return result
    names = set(assessed)
    changed = set(report["changed"])
    metadata = set(report["metadata_only"])
    deferred = {item["package"]: item["stage"] for item in report["deferred"]}
    if not (changed | metadata | set(deferred)) <= names or len(deferred) != len(report["deferred"]):
        return result
    if changed & set(deferred) or metadata & set(deferred):
        return result
    if (changed or metadata) and not report["published"]:
        return result
    packages = {}
    for name in sorted(names):
        if name in deferred:
            packages[name] = {"outcome": "deferred", "stage": deferred[name]}
        elif report["published"] and name in changed:
            packages[name] = {"outcome": "updated"}
        else:
            packages[name] = {"outcome": "checked"}
    result["outcome"] = "partial" if deferred else "success"
    result["packages"] = packages
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Preview or publish safe public update status")
    parser.add_argument("--diagnostics", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args(argv)
    try:
        try:
            report = load_report(Path(args.diagnostics))
        except ReportInvalid:
            report = None
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = (json.dumps(public_document(report, args.run_id, now), separators=(",", ":")) + "\n").encode()
        if len(payload) > MAX_BYTES:
            raise ValueError("public status exceeds size limit")
        if args.publish:
            if report is not None and report["dry_run"]:
                raise ValueError("cannot publish dry-run diagnostics")
            bucket = os.environ.get("APT_S3_BUCKET", "")
            if not bucket:
                raise ValueError("APT_S3_BUCKET is required")
            _s3_client().put_object(Bucket=bucket, Key=KEY, Body=payload, ContentType="application/json", CacheControl="no-cache")
        else:
            sys.stdout.write(payload.decode())
        return 0
    except Exception:
        print("update status: could not produce or publish snapshot", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
