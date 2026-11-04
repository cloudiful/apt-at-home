"""Schema, safe summary, and verdict contract for update diagnostics."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts.update_plan import Deferred
from scripts.update_report import (
    MAX_DETAIL_CHARS,
    ReportInvalid,
    append_summary,
    check_outcome,
    load_report,
    main as report_main,
    render_summary,
    report_document,
    sanitize_detail,
    validate_document,
    write_report,
)


def _deferred(**overrides) -> Deferred:
    fields = {
        "name": "zellij",
        "stage": "discovery",
        "detail": "license bytes changed",
        "retained_version": "0.45.1",
    }
    fields.update(overrides)
    return Deferred(**fields)


def _document(**overrides) -> dict[str, object]:
    fields: dict[str, object] = {
        "baseline_release_id": "20260101-000000",
        "planned_release_id": None,
        "confirmed_release_id": None,
        "changed": (),
        "metadata_only": (),
        "deferred": (),
        "published": False,
        "fatal": None,
        "dry_run": True,
    }
    fields.update(overrides)
    return report_document(**fields)  # type: ignore[arg-type]


class SchemaTest(unittest.TestCase):
    def test_a_report_round_trips_and_carries_the_required_facts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.json"
            document = _document(
                planned_release_id="20260102-000000",
                confirmed_release_id="20260102-000000",
                changed=("rustfs-cli", "starship"),
                metadata_only=("zellij",),
                deferred=(_deferred(),),
                published=True,
                dry_run=False,
            )
            write_report(path, document)
            loaded = load_report(path)
        self.assertEqual(loaded, document)
        for key in (
            "schema_version", "baseline_release_id", "planned_release_id",
            "confirmed_release_id", "changed", "metadata_only", "deferred",
            "published", "fatal", "dry_run",
        ):
            self.assertIn(key, loaded)
        self.assertEqual(loaded["changed"], ["rustfs-cli", "starship"])

    def test_a_missing_or_malformed_report_is_never_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.json"
            with self.assertRaisesRegex(ReportInvalid, "cannot read"):
                load_report(path)
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaisesRegex(ReportInvalid, "not valid JSON"):
                load_report(path)

    def test_invalid_schema_and_types_are_rejected(self) -> None:
        good = _document()
        invalid = [
            [],
            {**good, "schema_version": 2},
            {**good, "changed": "zellij"},
            {**good, "published": "yes"},
            {**good, "deferred": [{}]},
            {**good, "deferred": [dict(vars(_deferred()), extra="x")]},
            {**good, "deferred": [dict(vars(_deferred()), stage="mystery")]},
            {**good, "confirmed_release_id": "r2"},
            {**good, "published": True, "confirmed_release_id": None},
            {**good, "published": True, "confirmed_release_id": "r2", "fatal": "boom"},
        ]
        for document in invalid:
            with self.subTest(document=document):
                with self.assertRaises(ReportInvalid):
                    validate_document(document)

    def test_schema_version_must_be_the_integer_not_a_boolean_or_other_type(self) -> None:
        for value in (True, False, "1", 1.0):
            with self.subTest(value=value):
                with self.assertRaises(ReportInvalid):
                    validate_document({**_document(), "schema_version": value})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.json"
            for value in (True, False, "1", 1.0):
                with self.subTest(cli=value):
                    path.write_text(
                        json.dumps({**_document(), "schema_version": value}),
                        encoding="utf-8",
                    )
                    with contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(
                            report_main(
                                ["--diagnostics", str(path), "--check-deferred"]
                            ),
                            1,
                        )

    def test_every_required_field_must_be_present_even_when_nullable(self) -> None:
        document = _document()
        for key in document:
            with self.subTest(missing=key):
                incomplete = {k: v for k, v in document.items() if k != key}
                with self.assertRaisesRegex(ReportInvalid, "missing required fields"):
                    validate_document(incomplete)

    def test_extra_document_fields_are_ignored_but_deferred_fields_are_exact(self) -> None:
        document = _document(deferred=(_deferred(),))
        validate_document({**document, "comment": "ok"})
        broken = json.loads(json.dumps(document))
        broken["deferred"][0]["unknown"] = "nope"
        with self.assertRaises(ReportInvalid):
            validate_document(broken)


class SanitizeTest(unittest.TestCase):
    def test_detail_is_bounded_single_line_and_credential_free(self) -> None:
        text = "a\nb\r\nc\t" + "x" * (MAX_DETAIL_CHARS + 50)
        cleaned = sanitize_detail(text)
        self.assertNotIn("\n", cleaned)
        self.assertLessEqual(len(cleaned), MAX_DETAIL_CHARS)
        self.assertTrue(cleaned.endswith("..."))
        self.assertEqual(
            sanitize_detail("fetch https://user:secret@example.com/x failed"),
            "fetch https://example.com/x failed",
        )

    def test_url_credentials_queries_and_fragments_are_removed(self) -> None:
        detail = (
            "GET https://user:pass@example.invalid/object"
            "?X-Amz-Signature=example-secret&token=other#frag failed"
        )
        self.assertEqual(
            sanitize_detail(detail), "GET https://example.invalid/object failed"
        )
        self.assertNotIn("example-secret", sanitize_detail(detail))
        self.assertNotIn("token=", sanitize_detail(detail))


class SummaryTest(unittest.TestCase):
    def test_outcomes_distinguish_dry_run_noop_partial_and_confirmed(self) -> None:
        dry = render_summary(_document())
        self.assertIn("planned dry run", dry)
        self.assertNotIn("published", dry.lower().split("outcome:")[-1].split("\n")[0])

        partial = render_summary(
            _document(
                planned_release_id="r2",
                confirmed_release_id="r2",
                published=True,
                dry_run=False,
                changed=("starship",),
                deferred=(_deferred(),),
            )
        )
        self.assertIn("partial publication", partial)
        self.assertIn("Confirmed release: `r2`", partial)

        fatal = render_summary(_document(fatal="upload denied"))
        self.assertIn("fatal", fatal)
        self.assertIn("upload denied", fatal)

        for summary in (dry, partial, fatal):
            self.assertNotIn("every package is already at its latest", summary)

    def test_the_summary_escapes_markdown_and_workflow_commands(self) -> None:
        summary = render_summary(
            _document(
                deferred=(
                    _deferred(
                        detail="bad | value ::error:: injected `code` "
                        "https://user:pass@example.com/path"
                    ),
                    _deferred(name="starship", detail="::set-output name=x::y"),
                )
            )
        )
        self.assertIn("\\|", summary)
        self.assertIn("\\::set-output", summary)
        self.assertNotIn("user:pass@", summary)
        self.assertIn("example.com/path", summary)
        self.assertNotIn("`code`", summary)
        self.assertFalse(any(line.startswith("::") for line in summary.splitlines()))

    def test_reports_and_summaries_never_contain_url_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "report.json"
            summary = root / "summary.md"
            document = _document(
                fatal="failed https://u:p@example.invalid/o?token=example-secret#f",
                deferred=(
                    _deferred(
                        detail="signed https://example.invalid/o?X-Amz-Signature=example-secret"
                    ),
                ),
            )
            write_report(report, document)
            append_summary(summary, document)
            for path in (report, summary):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("example-secret", text, msg=str(path))
                self.assertNotIn("u:p@", text, msg=str(path))
                self.assertNotIn("token=", text, msg=str(path))

    def test_the_summary_appends_to_an_explicit_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "summary.md"
            document = _document(deferred=(_deferred(),))
            append_summary(path, document)
            append_summary(path, document)
            text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count("## Package update result"), 2)


class VerdictTest(unittest.TestCase):
    def test_check_outcome_codes(self) -> None:
        self.assertEqual(check_outcome(_document()), 0)
        self.assertEqual(check_outcome(_document(deferred=(_deferred(),))), 3)
        self.assertEqual(check_outcome(_document(fatal="boom")), 1)
        self.assertEqual(
            check_outcome(_document(fatal="boom", deferred=(_deferred(),))), 1
        )

    def test_a_schema_incomplete_report_is_never_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "incomplete.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "confirmed_release_id": None,
                        "changed": [],
                        "metadata_only": [],
                        "deferred": [],
                        "published": False,
                        "dry_run": False,
                        "fatal": None,
                    }
                ),
                encoding="utf-8",
            )
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(
                    report_main(
                        ["--diagnostics", str(path), "--check-deferred"]
                    ),
                    1,
                )
                self.assertEqual(report_main(["--diagnostics", str(path)]), 1)

    def test_cli_checks_summarizes_and_fails_missing_or_invalid_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "report.json"
            summary = root / "summary.md"
            write_report(report, _document(deferred=(_deferred(),)))
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(
                    report_main(
                        ["--diagnostics", str(report), "--summary", str(summary)]
                    ),
                    0,
                )
                self.assertEqual(
                    report_main(
                        ["--diagnostics", str(report), "--check-deferred"]
                    ),
                    3,
                )
            self.assertIn("Package update result", summary.read_text(encoding="utf-8"))

            fatal = root / "fatal.json"
            write_report(fatal, _document(fatal="boom"))
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(
                    report_main(["--diagnostics", str(fatal), "--check-deferred"]), 1
                )
                self.assertEqual(
                    report_main(["--diagnostics", str(root / "missing.json")]), 1
                )
            (root / "invalid.json").write_text("{}", encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(
                    report_main(["--diagnostics", str(root / "invalid.json")]), 1
                )


if __name__ == "__main__":
    unittest.main()
