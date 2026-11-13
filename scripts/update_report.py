"""Schema, Markdown summary, and final verdict for update diagnostics.

`scripts.update --diagnostics PATH` writes one versioned JSON document for every
run that requests it, including a dry run and every fatal outcome. This module
owns that schema, a bounded and escaped human summary, and the final gate that
turns deferred packages into a nonzero workflow result so a partial publication
never looks like a clean one.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from .update_plan import DEFERRED_STAGES, Deferred

SCHEMA_VERSION = 1
MAX_DETAIL_CHARS = 300

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s]+")
_REQUIRED_FIELDS = (
    "schema_version",
    "baseline_release_id",
    "planned_release_id",
    "confirmed_release_id",
    "changed",
    "metadata_only",
    "deferred",
    "published",
    "fatal",
    "dry_run",
)
_RELEASE_FIELDS = (
    "baseline_release_id",
    "planned_release_id",
    "confirmed_release_id",
    "fatal",
)
_LIST_FIELDS = ("changed", "metadata_only")
_DEFERRED_FIELDS = {"package", "stage", "detail", "retained_version"}


class ReportInvalid(ValueError):
    """The diagnostics report is missing, unreadable, or not schema version 1."""


def _strip_url(match: re.Match[str]) -> str:
    """Drop URL credentials, query strings, and fragments from one URL."""
    scheme, separator, rest = match.group(0).partition("://")
    rest = rest.split("#", 1)[0].split("?", 1)[0]
    if "@" in rest:
        rest = rest.rsplit("@", 1)[1]
    return f"{scheme}{separator}{rest}"


def sanitize_detail(text: str) -> str:
    """One bounded, single-line description without control or credential data."""
    cleaned = _CONTROL_RE.sub(" ", text)
    cleaned = " ".join(cleaned.split())
    cleaned = _URL_RE.sub(_strip_url, cleaned)
    if len(cleaned) > MAX_DETAIL_CHARS:
        cleaned = cleaned[: MAX_DETAIL_CHARS - 3] + "..."
    return cleaned


def report_document(
    *,
    baseline_release_id: str | None,
    planned_release_id: str | None,
    confirmed_release_id: str | None,
    changed: tuple[str, ...] | list[str],
    metadata_only: tuple[str, ...] | list[str],
    deferred: tuple[Deferred, ...] | list[Deferred],
    published: bool,
    fatal: str | None,
    dry_run: bool,
    assessed: tuple[str, ...] | list[str] | None = None,
) -> dict[str, object]:
    """Build one schema version 1 report document."""
    document = {
        "schema_version": SCHEMA_VERSION,
        "baseline_release_id": baseline_release_id,
        "planned_release_id": planned_release_id,
        "confirmed_release_id": confirmed_release_id,
        "changed": sorted(changed),
        "metadata_only": sorted(metadata_only),
        "deferred": [
            {
                "package": item.name,
                "stage": item.stage,
                "detail": sanitize_detail(item.detail),
                "retained_version": item.retained_version,
            }
            for item in deferred
        ],
        "published": published,
        "fatal": sanitize_detail(fatal) if fatal else None,
        "dry_run": dry_run,
    }
    if assessed is not None:
        document["assessed"] = sorted(assessed)
    return document


def validate_document(document: object) -> dict[str, object]:
    """Return the document or raise when it is not a valid schema version 1 report."""
    if not isinstance(document, dict):
        raise ReportInvalid("report must be a JSON object")
    missing = [key for key in _REQUIRED_FIELDS if key not in document]
    if missing:
        raise ReportInvalid(
            f"report is missing required fields: {', '.join(missing)}"
        )
    schema_version = document["schema_version"]
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise ReportInvalid(
            f"report schema_version must be the integer {SCHEMA_VERSION}"
        )
    if schema_version != SCHEMA_VERSION:
        raise ReportInvalid(f"report schema_version must be {SCHEMA_VERSION}")
    for key in _RELEASE_FIELDS:
        value = document.get(key)
        if value is not None and not isinstance(value, str):
            raise ReportInvalid(f"report {key} must be a string or null")
    for key in _LIST_FIELDS:
        value = document.get(key)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ReportInvalid(f"report {key} must be a list of package names")
    assessed = document.get("assessed")
    if assessed is not None and (
        not isinstance(assessed, list)
        or not all(isinstance(item, str) and item for item in assessed)
        or len(set(assessed)) != len(assessed)
    ):
        raise ReportInvalid("report assessed must be a list of unique package names or null")
    for key in ("published", "dry_run"):
        if not isinstance(document.get(key), bool):
            raise ReportInvalid(f"report {key} must be a boolean")
    deferred = document.get("deferred")
    if not isinstance(deferred, list):
        raise ReportInvalid("report deferred must be a list")
    for item in deferred:
        if not isinstance(item, dict) or set(item) != _DEFERRED_FIELDS:
            raise ReportInvalid("each deferred record must carry exactly the schema fields")
        if not isinstance(item["package"], str) or not item["package"]:
            raise ReportInvalid("deferred package must be a non-empty string")
        if item["stage"] not in DEFERRED_STAGES:
            raise ReportInvalid(f"deferred stage must be one of {', '.join(DEFERRED_STAGES)}")
        if not isinstance(item["detail"], str):
            raise ReportInvalid("deferred detail must be a string")
        if not isinstance(item["retained_version"], str) or not item["retained_version"]:
            raise ReportInvalid("deferred retained_version must be a non-empty string")
    confirmed = document["confirmed_release_id"]
    published = document["published"]
    if published != (confirmed is not None):
        raise ReportInvalid("published must be true exactly when a release is confirmed")
    if document["fatal"] is not None and published:
        raise ReportInvalid("a fatal outcome cannot claim a confirmed publication")
    return document


def write_report(path: Path, document: dict[str, object]) -> None:
    """Validate, then write one report document."""
    validate_document(document)
    path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def load_report(path: Path) -> dict[str, object]:
    """Read and validate one report; a missing or malformed report is never clean."""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ReportInvalid(f"cannot read diagnostics report {path}: {exc}") from exc
    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise ReportInvalid(f"diagnostics report {path} is not valid JSON: {exc}") from exc
    return validate_document(document)


def _cell(value: str) -> str:
    """Escape one value for a Markdown table cell and workflow-command safety."""
    text = sanitize_detail(value)
    text = text.replace("\\", "\\\\").replace("|", "\\|").replace("`", "'")
    if text.startswith("::"):
        text = "\\" + text
    return text


def outcome_label(document: dict[str, object]) -> str:
    """A short label that distinguishes dry run, no-op, partial, and confirmed."""
    deferred = bool(document["deferred"])
    if document["fatal"] is not None:
        return "fatal (nothing published)"
    if document["published"]:
        return "partial publication (deferred packages retained)" if deferred else "published"
    if document["dry_run"]:
        return "planned dry run with deferred packages" if deferred else "planned dry run"
    return "deferred packages retained; nothing republished" if deferred else "no changes"


def render_summary(document: dict[str, object]) -> str:
    """Render one safe Markdown summary for an explicit GitHub summary path."""
    deferred = document["deferred"]
    lines = [
        "## Package update result",
        "",
        f"- Outcome: **{outcome_label(document)}**",
        f"- Baseline release: {_release_cell(document['baseline_release_id'])}",
        f"- Planned release: {_release_cell(document['planned_release_id'])}",
        f"- Confirmed release: {_release_cell(document['confirmed_release_id'])}",
        f"- Changed packages: {len(document['changed'])}",
        f"- Metadata-only packages: {len(document['metadata_only'])}",
        f"- Deferred packages: {len(deferred)}",
        "",
    ]
    if document["fatal"] is not None:
        lines.extend([f"Fatal: {_cell(document['fatal'])}", ""])
    if document["changed"]:
        lines.append("Changed: " + ", ".join(_cell(name) for name in document["changed"]))
        lines.append("")
    if document["metadata_only"]:
        lines.append(
            "Metadata only: " + ", ".join(_cell(name) for name in document["metadata_only"])
        )
        lines.append("")
    if deferred:
        lines.extend(
            [
                "| Package | Stage | Retained version | Detail |",
                "| --- | --- | --- | --- |",
            ]
        )
        for item in deferred:
            lines.append(
                f"| {_cell(item['package'])} | {_cell(item['stage'])} | "
                f"{_cell(item['retained_version'])} | {_cell(item['detail'])} |"
            )
        lines.append("")
    else:
        lines.extend(["Deferred packages: none", ""])
    return "\n".join(lines)


def _release_cell(value: object) -> str:
    return "none" if value is None else f"`{_cell(str(value))}`"


def append_summary(path: Path, document: dict[str, object]) -> None:
    """Append the rendered summary to an explicit summary file."""
    text = render_summary(document)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text.rstrip("\n") + "\n\n")


def check_outcome(document: dict[str, object]) -> int:
    """0 clean, 3 deferred, 1 fatal."""
    if document["fatal"] is not None:
        return 1
    if document["deferred"]:
        return 3
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.update_report",
        description="Validate an update diagnostics report and publish its outcome.",
    )
    parser.add_argument(
        "--diagnostics",
        required=True,
        metavar="PATH",
        help="JSON report written by scripts.update --diagnostics",
    )
    parser.add_argument(
        "--summary",
        default=None,
        metavar="PATH",
        help="append a safe Markdown summary to an explicit summary file",
    )
    parser.add_argument(
        "--check-deferred",
        action="store_true",
        help="exit 3 when packages were deferred, 1 on a fatal, missing, or invalid report",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        document = load_report(Path(args.diagnostics))
    except ReportInvalid as exc:
        print(f"update report: {exc}", file=sys.stderr)
        return 1
    if args.summary:
        try:
            append_summary(Path(args.summary), document)
        except OSError as exc:
            print(f"update summary: {exc}", file=sys.stderr)
            return 1
    if args.check_deferred:
        code = check_outcome(document)
        if code:
            print(f"update report: {outcome_label(document)}", file=sys.stderr)
        return code
    if args.summary is None:
        print(f"update report: {outcome_label(document)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
