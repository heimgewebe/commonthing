#!/usr/bin/env python3
from __future__ import annotations

import datetime
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.docmeta.validate_report_lifecycle import (
    Finding,
    extract_truth_contract,
    main,
    run,
    validate_truth_report,
    _validate_report,
    _gha_escape_data,
    _gha_escape_property,
    _render_github_warnings,
)


VALID_REPORT_FRONTMATTER = """
id: reports.example
title: Example
doc_type: report
status: active
lifecycle_state: active
lifecycle: audit
owner_task: OPT-ARC-001
review_after: 2026-07-13
"""


def write_report(root: Path, name: str, frontmatter: str, body: str = "Body\n") -> Path:
    path = root / "docs" / "reports" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter.strip()}\n---\n\n{body}", encoding="utf-8")
    return path


class TestValidateReportLifecycle(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_main(self, args: list[str]) -> tuple[int, str, str]:
        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()
        with patch("sys.stdout", stdout_capture), patch("sys.stderr", stderr_capture):
            exit_code = main(args)
        return exit_code, stdout_capture.getvalue(), stderr_capture.getvalue()

    def test_report_mode_exits_zero_even_with_findings(self) -> None:
        # Fixture
        write_report(
            self.tmp_root,
            "example.md",
            """
id: reports.example
title: Example
doc_type: report
status: active
            """
        )

        exit_code, stdout, stderr = self.run_main(["--mode", "report", "--root", str(self.tmp_root)])
        self.assertEqual(exit_code, 0)
        self.assertIn("missing_lifecycle", stdout)
        self.assertIn("missing_review_after", stdout)
        self.assertEqual(stderr, "")

    def test_warn_mode_exits_zero_and_emits_github_warnings(self) -> None:
        # Fixture with at least one lifecycle finding (missing lifecycle_state)
        write_report(
            self.tmp_root,
            "example.md",
            """
id: reports.example
title: Example
doc_type: report
status: active
            """
        )

        exit_code, stdout, stderr = self.run_main(["--mode", "warn", "--root", str(self.tmp_root)])
        self.assertEqual(exit_code, 0)
        self.assertIn("::warning", stdout)
        self.assertIn("docs/reports/example.md", stdout)
        self.assertIn("missing_lifecycle_state", stdout)
        # Markdown report (summary) is still emitted alongside the annotations.
        self.assertIn("## Summary", stdout)
        self.assertEqual(stderr, "")

    def test_warn_mode_exits_zero_without_findings(self) -> None:
        # Fixture: minimal valid report (no findings).
        write_report(self.tmp_root, "example.md", VALID_REPORT_FRONTMATTER)

        exit_code, stdout, stderr = self.run_main(["--mode", "warn", "--root", str(self.tmp_root)])
        self.assertEqual(exit_code, 0)
        self.assertNotIn("::warning", stdout)
        self.assertIn("| findings_total | 0 |", stdout)
        self.assertIn("No findings.", stdout)
        self.assertEqual(stderr, "")

    def test_strict_mode_exits_one_with_findings(self) -> None:
        # Fixture with at least one lifecycle finding.
        write_report(
            self.tmp_root,
            "example.md",
            """
id: reports.example
title: Example
doc_type: report
status: active
            """
        )

        exit_code, stdout, stderr = self.run_main(["--mode", "strict", "--root", str(self.tmp_root)])
        self.assertEqual(exit_code, 1)
        self.assertIn("## Summary", stdout)
        self.assertIn("missing_lifecycle_state", stdout)
        self.assertEqual(stderr, "")

    def test_strict_mode_exits_zero_without_findings(self) -> None:
        # Fixture: minimal valid report (no findings).
        write_report(self.tmp_root, "example.md", VALID_REPORT_FRONTMATTER)

        exit_code, stdout, stderr = self.run_main(["--mode", "strict", "--root", str(self.tmp_root)])
        self.assertEqual(exit_code, 0)
        self.assertIn("| findings_total | 0 |", stdout)
        self.assertIn("No findings.", stdout)
        self.assertEqual(stderr, "")

    def test_run_rejects_invalid_mode(self) -> None:
        # run(root, mode) should reject unsupported modes directly.
        with self.assertRaises(ValueError) as ctx:
            run(self.tmp_root, "nonsense")
        self.assertIn("unsupported report lifecycle mode", str(ctx.exception))

    def test_github_warning_annotation_escapes_special_characters(self) -> None:
        # Data escaping: percent first, then CR/LF.
        self.assertEqual(_gha_escape_data("100%"), "100%25")
        self.assertEqual(_gha_escape_data("line1\nline2"), "line1%0Aline2")
        self.assertEqual(_gha_escape_data("a\rb"), "a%0Db")
        # Property escaping additionally handles ":" and ",".
        self.assertEqual(_gha_escape_property("a:b"), "a%3Ab")
        self.assertEqual(_gha_escape_property("a,b"), "a%2Cb")
        # Percent is escaped first, so no double-escaping of the "%0A"/"%3A" markers.
        self.assertEqual(_gha_escape_property("%\n:"), "%25%0A%3A")

        # End-to-end: the rendered annotation escapes path (property) and message (data).
        finding = Finding(
            path="docs/reports/weird,name.md",
            code="missing_lifecycle_state",
            severity="warn",
            field="lifecycle_state",
            message="needs 100% coverage: now",
        )
        rendered = _render_github_warnings([finding])
        self.assertIn("::warning ", rendered)
        self.assertIn("file=docs/reports/weird%2Cname.md", rendered)
        self.assertIn("title=Report lifecycle finding", rendered)
        # The message uses data escaping, so ":" stays but "%" becomes "%25".
        self.assertIn("missing_lifecycle_state: needs 100%25 coverage: now", rendered)

    def test_invalid_mode_is_rejected(self) -> None:
        # argparse `choices` rejects unknown modes with a non-zero SystemExit.
        with self.assertRaises(SystemExit) as ctx:
            self.run_main(["--mode", "nonsense", "--root", str(self.tmp_root)])
        self.assertNotEqual(ctx.exception.code, 0)

    def test_active_report_missing_lifecycle_and_review_after(self) -> None:
        # Fixture
        fm = {
            "id": "reports.example",
            "title": "Example",
            "doc_type": "report",
            "status": "active"
        }
        path = self.tmp_root / "docs" / "reports" / "example.md"
        findings = _validate_report(path, fm, self.tmp_root)
        codes = [f.code for f in findings]
        self.assertIn("missing_lifecycle", codes)
        self.assertIn("missing_review_after", codes)

    def test_lifecycle_state_active_missing_owner_task(self) -> None:
        # Fixture
        fm = {
            "id": "reports.example",
            "title": "Example",
            "doc_type": "report",
            "status": "active",
            "lifecycle_state": "active",
            "lifecycle": "audit",
            "review_after": "2026-07-13"
        }
        path = self.tmp_root / "docs" / "reports" / "example.md"
        findings = _validate_report(path, fm, self.tmp_root)
        codes = [f.code for f in findings]
        self.assertEqual(codes, ["missing_owner_task"])

    def test_superseded_needs_superseded_by(self) -> None:
        # Fixture
        fm = {
            "id": "reports.example",
            "title": "Example",
            "doc_type": "report",
            "status": "deprecated",
            "lifecycle_state": "superseded"
        }
        path = self.tmp_root / "docs" / "reports" / "example.md"
        findings = _validate_report(path, fm, self.tmp_root)
        codes = [f.code for f in findings]
        self.assertIn("missing_superseded_by", codes)

    def test_archived_does_not_need_superseded_by(self) -> None:
        # Fixture
        fm = {
            "id": "reports.example",
            "title": "Example",
            "doc_type": "report",
            "status": "deprecated",
            "lifecycle_state": "archived"
        }
        path = self.tmp_root / "docs" / "reports" / "example.md"
        findings = _validate_report(path, fm, self.tmp_root)
        codes = [f.code for f in findings]
        self.assertNotIn("missing_superseded_by", codes)

    def test_deprecated_without_lifecycle_state_is_not_supersession(self) -> None:
        # Fixture
        fm = {
            "id": "reports.example",
            "title": "Example",
            "doc_type": "report",
            "status": "deprecated"
        }
        path = self.tmp_root / "docs" / "reports" / "example.md"
        findings = _validate_report(path, fm, self.tmp_root)
        codes = [f.code for f in findings]
        self.assertNotIn("missing_superseded_by", codes)

    def test_case_insensitive_lifecycle_state(self) -> None:
        # Fixture
        fm = {
            "id": "reports.example",
            "title": "Example",
            "doc_type": "report",
            "status": "deprecated",
            "lifecycle_state": "Superseded"
        }
        path = self.tmp_root / "docs" / "reports" / "example.md"
        findings = _validate_report(path, fm, self.tmp_root)
        codes = [f.code for f in findings]
        self.assertIn("missing_superseded_by", codes)

    def test_non_report_doc_type_in_docs_reports_does_not_trigger_required_lifecycle_findings(self) -> None:
        # Fixture
        fm = {
            "id": "reports.reference",
            "title": "Reference",
            "doc_type": "reference",
            "status": "active"
        }
        path = self.tmp_root / "docs" / "reports" / "example.md"
        findings = _validate_report(path, fm, self.tmp_root)
        self.assertEqual(findings, [])

    def test_pilot_shaped_report_passes_active_lifecycle_checks(self) -> None:
        # Fixture
        fm = {
            "id": "reports.domain-account-email-uniqueness-audit",
            "title": "Domain Account E-Mail Uniqueness Audit",
            "doc_type": "report",
            "status": "active",
            "lifecycle_state": "active",
            "lifecycle": "audit",
            "owner_task": "OPT-ARC-001",
            "review_after": "2026-07-13"
        }
        path = self.tmp_root / "docs" / "reports" / "domain-account-email-uniqueness-audit.md"
        findings = _validate_report(path, fm, self.tmp_root)
        self.assertEqual(findings, [])

    def test_invalid_status_is_reported(self) -> None:
        fm = {
            "id": "reports.example",
            "title": "Example",
            "doc_type": "report",
            "status": "mystery",
            "lifecycle_state": "active",
            "lifecycle": "audit",
            "owner_task": "OPT-ARC-001",
            "review_after": "2026-07-13",
        }
        path = self.tmp_root / "docs" / "reports" / "example.md"
        findings = _validate_report(path, fm, self.tmp_root)
        self.assertIn("invalid_status", [f.code for f in findings])

    def test_non_string_required_scalars_are_missing(self) -> None:
        fm = {
            "id": "reports.example",
            "title": "Example",
            "doc_type": "report",
            "status": True,
            "lifecycle_state": 7,
            "lifecycle": ["audit"],
            "owner_task": {"id": "OPT-ARC-001"},
            "review_after": None,
        }
        path = self.tmp_root / "docs" / "reports" / "example.md"
        codes = [finding.code for finding in _validate_report(path, fm, self.tmp_root)]
        self.assertIn("missing_status", codes)
        self.assertIn("missing_lifecycle_state", codes)

    def test_invalid_lifecycle_state_is_reported(self) -> None:
        fm = {
            "id": "reports.example",
            "title": "Example",
            "doc_type": "report",
            "status": "active",
            "lifecycle_state": "current",
            "lifecycle": "audit",
            "owner_task": "OPT-ARC-001",
            "review_after": "2026-07-13",
        }
        path = self.tmp_root / "docs" / "reports" / "example.md"
        findings = _validate_report(path, fm, self.tmp_root)
        self.assertIn("invalid_lifecycle_state", [f.code for f in findings])

    def test_invalid_lifecycle_is_reported(self) -> None:
        fm = {
            "id": "reports.example",
            "title": "Example",
            "doc_type": "report",
            "status": "active",
            "lifecycle_state": "active",
            "lifecycle": "status",
            "owner_task": "OPT-ARC-001",
            "review_after": "2026-07-13",
        }
        path = self.tmp_root / "docs" / "reports" / "example.md"
        findings = _validate_report(path, fm, self.tmp_root)
        self.assertIn("invalid_lifecycle", [f.code for f in findings])

    def test_invalid_review_after_format_is_reported(self) -> None:
        fm = {
            "id": "reports.example",
            "title": "Example",
            "doc_type": "report",
            "status": "active",
            "lifecycle_state": "active",
            "lifecycle": "audit",
            "owner_task": "OPT-ARC-001",
            "review_after": "20260713",
        }
        path = self.tmp_root / "docs" / "reports" / "example.md"
        findings = _validate_report(path, fm, self.tmp_root)
        self.assertIn("invalid_review_after", [f.code for f in findings])

    def test_invalid_review_after_calendar_date_is_reported(self) -> None:
        fm = {
            "id": "reports.example",
            "title": "Example",
            "doc_type": "report",
            "status": "active",
            "lifecycle_state": "active",
            "lifecycle": "audit",
            "owner_task": "OPT-ARC-001",
            "review_after": "2026-02-30",
        }
        path = self.tmp_root / "docs" / "reports" / "example.md"
        findings = _validate_report(path, fm, self.tmp_root)
        self.assertIn("invalid_review_after", [f.code for f in findings])

    def test_superseded_by_must_point_to_existing_file(self) -> None:
        fm = {
            "id": "reports.example",
            "title": "Example",
            "doc_type": "report",
            "status": "deprecated",
            "lifecycle_state": "superseded",
            "lifecycle": "audit",
            "owner_task": "OPT-ARC-001",
            "superseded_by": "docs/reports/missing.md",
        }
        path = self.tmp_root / "docs" / "reports" / "example.md"
        findings = _validate_report(path, fm, self.tmp_root)
        self.assertIn("invalid_superseded_by", [f.code for f in findings])

    def test_superseded_by_must_not_point_to_self(self) -> None:
        fm = {
            "id": "reports.example",
            "title": "Example",
            "doc_type": "report",
            "status": "deprecated",
            "lifecycle_state": "superseded",
            "lifecycle": "audit",
            "owner_task": "OPT-ARC-001",
            "superseded_by": "docs/reports/example.md",
        }
        path = self.tmp_root / "docs" / "reports" / "example.md"
        findings = _validate_report(path, fm, self.tmp_root)
        self.assertIn("invalid_superseded_by", [f.code for f in findings])

    def test_superseded_by_existing_file_is_valid(self) -> None:
        write_report(self.tmp_root, "replacement.md", VALID_REPORT_FRONTMATTER)
        fm = {
            "id": "reports.example",
            "title": "Example",
            "doc_type": "report",
            "status": "deprecated",
            "lifecycle_state": "superseded",
            "lifecycle": "audit",
            "owner_task": "OPT-ARC-001",
            "superseded_by": "docs/reports/replacement.md",
        }
        path = self.tmp_root / "docs" / "reports" / "example.md"
        findings = _validate_report(path, fm, self.tmp_root)
        self.assertNotIn("invalid_superseded_by", [f.code for f in findings])

    def test_unknown_owner_task_is_reported_when_registry_exists(self) -> None:
        task_index = self.tmp_root / "docs" / "tasks" / "index.json"
        task_index.parent.mkdir(parents=True, exist_ok=True)
        task_index.write_text('{"tasks":[{"id":"OPT-ARC-001"}]}', encoding="utf-8")
        fm = {
            "id": "reports.example",
            "title": "Example",
            "doc_type": "report",
            "status": "active",
            "lifecycle_state": "active",
            "lifecycle": "audit",
            "owner_task": "MISSING-TASK",
            "review_after": "2026-07-13",
        }
        path = self.tmp_root / "docs" / "reports" / "example.md"
        findings = _validate_report(path, fm, self.tmp_root)
        self.assertIn("invalid_owner_task", [f.code for f in findings])

    def test_bureau_owner_task_remains_external_when_local_registry_exists(self) -> None:
        task_index = self.tmp_root / "docs" / "tasks" / "index.json"
        task_index.parent.mkdir(parents=True, exist_ok=True)
        task_index.write_text('{"tasks":[{"id":"OPT-ARC-001","status":"obsolete"}]}', encoding="utf-8")
        fm = {
            "id": "reports.example",
            "title": "Example",
            "doc_type": "report",
            "status": "active",
            "lifecycle_state": "active",
            "lifecycle": "audit",
            "owner_task": "BUREAU-COMMONTHING-V1-T001",
            "review_after": "2026-07-13",
        }
        path = self.tmp_root / "docs" / "reports" / "example.md"
        findings = _validate_report(path, fm, self.tmp_root)
        codes = [f.code for f in findings]
        self.assertNotIn("invalid_owner_task", codes)
        self.assertNotIn("invalid_owner_status", codes)

    def test_malformed_bureau_owner_task_does_not_bypass_local_resolution(self) -> None:
        task_index = self.tmp_root / "docs" / "tasks" / "index.json"
        task_index.parent.mkdir(parents=True, exist_ok=True)
        task_index.write_text('{"tasks":[{"id":"OPT-ARC-001"}]}', encoding="utf-8")
        fm = {
            "id": "reports.example",
            "title": "Example",
            "doc_type": "report",
            "status": "active",
            "lifecycle_state": "active",
            "lifecycle": "audit",
            "owner_task": "BUREAU-T001 invalid",
            "review_after": "2026-07-13",
        }
        path = self.tmp_root / "docs" / "reports" / "example.md"
        findings = _validate_report(path, fm, self.tmp_root)
        self.assertIn("invalid_owner_task", [f.code for f in findings])

    def test_owner_task_resolves_from_task_index(self) -> None:
        task_index = self.tmp_root / "docs" / "tasks" / "index.json"
        task_index.parent.mkdir(parents=True, exist_ok=True)
        task_index.write_text('{"tasks":[{"id":"DOCMETA-REPORT-LIFECYCLE-001"}]}', encoding="utf-8")
        fm = {
            "id": "reports.example",
            "title": "Example",
            "doc_type": "report",
            "status": "active",
            "lifecycle_state": "active",
            "lifecycle": "audit",
            "owner_task": "DOCMETA-REPORT-LIFECYCLE-001",
            "review_after": "2026-07-13",
        }
        path = self.tmp_root / "docs" / "reports" / "example.md"
        findings = _validate_report(path, fm, self.tmp_root)
        self.assertNotIn("invalid_owner_task", [f.code for f in findings])

    def test_owner_task_resolves_from_optimization_status_markdown(self) -> None:
        status = self.tmp_root / "docs" / "reports" / "optimierungsstatus.md"
        status.parent.mkdir(parents=True, exist_ok=True)
        status.write_text("| ID | Titel |\n| --- | --- |\n| OPT-API-002 | Auth |\n", encoding="utf-8")
        fm = {
            "id": "reports.example",
            "title": "Example",
            "doc_type": "report",
            "status": "active",
            "lifecycle_state": "active",
            "lifecycle": "audit",
            "owner_task": "OPT-API-002",
            "review_after": "2026-07-13",
        }
        path = self.tmp_root / "docs" / "reports" / "example.md"
        findings = _validate_report(path, fm, self.tmp_root)
        self.assertNotIn("invalid_owner_task", [f.code for f in findings])

    def test_owner_task_check_is_skipped_without_registry_sources(self) -> None:
        fm = {
            "id": "reports.example",
            "title": "Example",
            "doc_type": "report",
            "status": "active",
            "lifecycle_state": "active",
            "lifecycle": "audit",
            "owner_task": "FREEFORM-OWNER",
            "review_after": "2026-07-13",
        }
        path = self.tmp_root / "docs" / "reports" / "example.md"
        findings = _validate_report(path, fm, self.tmp_root)
        self.assertNotIn("invalid_owner_task", [f.code for f in findings])

    def write_task_index(self, task_id: str, status: str) -> None:
        path = self.tmp_root / "docs" / "tasks" / "index.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"tasks":[{"id":"' + task_id + '","status":"' + status + '"}]}',
            encoding="utf-8",
        )

    def write_opt_status(self, task_id: str, status: str) -> None:
        path = self.tmp_root / "docs" / "reports" / "optimierungsstatus.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "## Matrix\n\n"
            "| id | bereich | maßnahme | status | befund_evidenzgrad | risiko | aufwand | priorität | nachweis | test | restlücke | zuletzt_geprüft |\n"
            "|---|---|---|---|---|---|---|---|---|---|---|---|\n"
            f"| {task_id} | API | X | {status} | code | low | low | low | n | t | r | 2026-06-29 |\n",
            encoding="utf-8",
        )

    def test_done_owner_status_remains_valid_for_active_audit_report(self) -> None:
        self.write_task_index("TASK-CTL-001", "done")
        fm = {
            "id": "reports.example",
            "title": "Example",
            "doc_type": "report",
            "status": "active",
            "lifecycle_state": "active",
            "lifecycle": "audit",
            "owner_task": "TASK-CTL-001",
            "review_after": "2026-07-13",
        }
        path = self.tmp_root / "docs" / "reports" / "example.md"
        findings = _validate_report(path, fm, self.tmp_root)
        self.assertNotIn("invalid_owner_status", [f.code for f in findings])

    def test_inconsistent_owner_status_between_sources_is_reported(self) -> None:
        self.write_task_index("OPT-ARC-001", "partial")
        self.write_opt_status("OPT-ARC-001", "done")
        fm = {
            "id": "reports.example",
            "title": "Example",
            "doc_type": "report",
            "status": "active",
            "lifecycle_state": "active",
            "lifecycle": "audit",
            "owner_task": "OPT-ARC-001",
            "review_after": "2026-07-13",
        }
        path = self.tmp_root / "docs" / "reports" / "example.md"
        findings = _validate_report(path, fm, self.tmp_root)
        self.assertIn("invalid_owner_status", [f.code for f in findings])

    def test_active_report_rejects_terminal_owner_status(self) -> None:
        self.write_task_index("TASK-CTL-001", "obsolete")
        fm = {
            "id": "reports.example",
            "title": "Example",
            "doc_type": "report",
            "status": "active",
            "lifecycle_state": "active",
            "lifecycle": "audit",
            "owner_task": "TASK-CTL-001",
            "review_after": "2026-07-13",
        }
        path = self.tmp_root / "docs" / "reports" / "example.md"
        findings = _validate_report(path, fm, self.tmp_root)
        self.assertIn("invalid_owner_status", [f.code for f in findings])

    def test_archived_report_allows_terminal_owner_status(self) -> None:
        self.write_task_index("TASK-CTL-001", "obsolete")
        fm = {
            "id": "reports.example",
            "title": "Example",
            "doc_type": "report",
            "status": "deprecated",
            "lifecycle_state": "archived",
            "lifecycle": "audit",
            "owner_task": "TASK-CTL-001",
        }
        path = self.tmp_root / "docs" / "reports" / "example.md"
        findings = _validate_report(path, fm, self.tmp_root)
        self.assertNotIn("invalid_owner_status", [f.code for f in findings])

    def test_render_output_contains_expected_structure(self) -> None:
        write_report(
            self.tmp_root,
            "example.md",
            """
id: reports.example
title: Example
doc_type: report
status: active
            """
        )

        rendered, exit_code = run(self.tmp_root, "report")
        self.assertEqual(exit_code, 0)
        self.assertIn("# Report Lifecycle Validation", rendered)
        self.assertIn("Mode: report", rendered)
        self.assertIn("| files_scanned |", rendered)
        self.assertIn("| reports_checked |", rendered)
        self.assertIn("| reports_ignored_non_report |", rendered)
        self.assertIn("| findings_total |", rendered)
        self.assertIn("| missing_lifecycle_state |", rendered)
        self.assertIn("| missing_frontmatter |", rendered)
        self.assertIn("| invalid_lifecycle |", rendered)
        self.assertIn("| invalid_review_after |", rendered)
        self.assertIn("| invalid_superseded_by |", rendered)
        self.assertIn("| invalid_owner_task |", rendered)
        self.assertIn("| invalid_owner_status |", rendered)
        self.assertIn("| Path | Severity | Code | Field | Message |", rendered)

    def test_findings_are_deterministically_sorted_by_path_and_code(self) -> None:
        write_report(
            self.tmp_root,
            "b_example.md",
            """
id: reports.b
title: B
doc_type: report
status: active
lifecycle_state: active
            """
        )
        write_report(
            self.tmp_root,
            "a_example.md",
            """
id: reports.a
title: A
doc_type: report
status: active
lifecycle_state: active
            """
        )
        rendered, exit_code = run(self.tmp_root, "report")
        self.assertEqual(exit_code, 0)

        lines = [
            line
            for line in rendered.splitlines()
            if line.startswith("| docs/reports/")
        ]
        self.assertEqual(len(lines), 6)

        extracted = []
        for line in lines:
            parts = [
                part.strip()
                for part in line.strip().strip("|").split("|")
            ]
            self.assertEqual(len(parts), 5)
            extracted.append((parts[0], parts[2]))

        self.assertEqual(
            extracted,
            [
                ("docs/reports/a_example.md", "missing_lifecycle"),
                ("docs/reports/a_example.md", "missing_owner_task"),
                ("docs/reports/a_example.md", "missing_review_after"),
                ("docs/reports/b_example.md", "missing_lifecycle"),
                ("docs/reports/b_example.md", "missing_owner_task"),
                ("docs/reports/b_example.md", "missing_review_after"),
            ],
        )

        rendered_again, exit_code_again = run(self.tmp_root, "report")
        self.assertEqual(exit_code_again, exit_code)
        self.assertEqual(rendered_again, rendered)

    def test_validate_report_with_datetime_date_review_after(self) -> None:
        fm = {
            "id": "reports.example",
            "title": "Example",
            "doc_type": "report",
            "status": "active",
            "review_after": datetime.date(2026, 7, 13),
            "lifecycle": "audit"
        }
        path = self.tmp_root / "docs" / "reports" / "example.md"
        findings = _validate_report(path, fm, self.tmp_root)
        codes = [f.code for f in findings]
        self.assertIn("missing_review_after", codes)

    def test_run_with_unquoted_date_in_frontmatter(self) -> None:
        write_report(
            self.tmp_root,
            "example.md",
            """
id: reports.example
title: Example
doc_type: report
status: active
lifecycle: audit
review_after: 2026-07-13
            """
        )
        rendered, exit_code = run(self.tmp_root, "report")
        self.assertEqual(exit_code, 0)
        self.assertIn("| missing_review_after | 0 |", rendered)

    def test_run_scans_and_checks_precise_metrics(self) -> None:
        # Fixture 1: a report
        write_report(
            self.tmp_root,
            "example_report.md",
            """
id: reports.example_report
title: Example Report
doc_type: report
status: active
lifecycle: audit
review_after: 2026-07-13
            """
        )
        # Fixture 2: a reference (non-report)
        write_report(
            self.tmp_root,
            "example_ref.md",
            """
id: reports.example_ref
title: Example Reference
doc_type: reference
status: active
            """
        )

        rendered, exit_code = run(self.tmp_root, "report")
        self.assertEqual(exit_code, 0)
        self.assertIn("| files_scanned | 2 |", rendered)
        self.assertIn("| reports_checked | 1 |", rendered)
        self.assertIn("| reports_ignored_non_report | 1 |", rendered)

    def test_run_recurses_into_subdirectories(self) -> None:
        write_report(self.tmp_root, "nested/2026/old.md", VALID_REPORT_FRONTMATTER)

        rendered, exit_code = run(self.tmp_root, "report")

        self.assertEqual(exit_code, 0)
        self.assertIn("| files_scanned | 1 |", rendered)
        self.assertIn("| reports_checked | 1 |", rendered)
        self.assertIn("No findings.", rendered)

    def test_changed_only_strict_ignores_unchanged_invalid_report(self) -> None:
        def git(*args: str) -> str:
            completed = subprocess.run(
                ["git", *args],
                cwd=self.tmp_root,
                text=True,
                capture_output=True,
                check=True,
            )
            return completed.stdout.strip()

        git("init")
        git("config", "user.email", "test@example.invalid")
        git("config", "user.name", "Test")
        write_report(
            self.tmp_root,
            "legacy-invalid.md",
            """
id: reports.legacy-invalid
title: Legacy Invalid
doc_type: report
status: active
            """
        )
        git("add", ".")
        git("commit", "-m", "base")
        base = git("rev-parse", "HEAD")
        write_report(self.tmp_root, "new-valid.md", VALID_REPORT_FRONTMATTER)
        git("add", ".")
        git("commit", "-m", "change")

        rendered, exit_code = run(self.tmp_root, "strict", changed_from=base)

        self.assertEqual(exit_code, 0)
        self.assertIn("| files_scanned | 1 |", rendered)
        self.assertIn("No findings.", rendered)

    def test_changed_only_strict_reports_changed_invalid_report(self) -> None:
        def git(*args: str) -> str:
            completed = subprocess.run(
                ["git", *args],
                cwd=self.tmp_root,
                text=True,
                capture_output=True,
                check=True,
            )
            return completed.stdout.strip()

        git("init")
        git("config", "user.email", "test@example.invalid")
        git("config", "user.name", "Test")
        write_report(self.tmp_root, "base.md", VALID_REPORT_FRONTMATTER)
        git("add", ".")
        git("commit", "-m", "base")
        base = git("rev-parse", "HEAD")
        write_report(
            self.tmp_root,
            "changed-invalid.md",
            """
id: reports.changed-invalid
title: Changed Invalid
doc_type: report
status: active
            """
        )
        git("add", ".")
        git("commit", "-m", "change")

        rendered, exit_code = run(self.tmp_root, "strict", changed_from=base)

        self.assertEqual(exit_code, 1)
        self.assertIn("docs/reports/changed-invalid.md", rendered)
        self.assertIn("missing_lifecycle_state", rendered)

    def test_absent_report_metadata_is_reported(self) -> None:
        path = self.tmp_root / "docs" / "reports" / "missing.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Missing metadata\n", encoding="utf-8")

        rendered, exit_code = run(self.tmp_root, "strict")

        self.assertEqual(exit_code, 1)
        self.assertIn("missing_frontmatter", rendered)
        self.assertIn("docs/reports/missing.md", rendered)

    def test_blank_frontmatter_values_are_missing(self) -> None:
        write_report(
            self.tmp_root,
            "example.md",
            """
id: reports.example
title: Example
doc_type: report
status: active
lifecycle:
review_after:
            """
        )
        rendered, exit_code = run(self.tmp_root, "report")
        self.assertEqual(exit_code, 0)
        self.assertIn("missing_lifecycle", rendered)
        self.assertIn("missing_review_after", rendered)

    def test_blank_owner_task_is_missing_for_lifecycle_state_active(self) -> None:
        write_report(
            self.tmp_root,
            "example.md",
            """
id: reports.example
title: Example
doc_type: report
status: active
lifecycle_state: active
lifecycle: audit
owner_task:
review_after: 2026-07-13
            """
        )
        rendered, exit_code = run(self.tmp_root, "report")
        self.assertEqual(exit_code, 0)
        self.assertIn("missing_owner_task", rendered)

    def test_report_without_lifecycle_state_yields_finding(self) -> None:
        fm = {
            "id": "reports.example",
            "title": "Example",
            "doc_type": "report",
            "status": "active",
        }
        path = self.tmp_root / "docs" / "reports" / "example.md"
        findings = _validate_report(path, fm, self.tmp_root)
        codes = [f.code for f in findings]
        self.assertIn("missing_lifecycle_state", codes)

    def test_deferred_lifecycle_state_missing_owner_task_and_review_after(self) -> None:
        fm = {
            "id": "reports.example",
            "title": "Example",
            "doc_type": "report",
            "status": "active",
            "lifecycle_state": "deferred",
            "lifecycle": "audit",
        }
        path = self.tmp_root / "docs" / "reports" / "example.md"
        findings = _validate_report(path, fm, self.tmp_root)
        codes = [f.code for f in findings]
        self.assertIn("missing_owner_task", codes)
        self.assertIn("missing_review_after", codes)

    def test_superseded_lifecycle_state_with_only_superseded_by(self) -> None:
        fm = {
            "id": "reports.example",
            "title": "Example",
            "doc_type": "report",
            "status": "deprecated",
            "lifecycle_state": "superseded",
            "superseded_by": "reports.new_one",
        }
        path = self.tmp_root / "docs" / "reports" / "example.md"
        findings = _validate_report(path, fm, self.tmp_root)
        codes = [f.code for f in findings]
        self.assertIn("missing_lifecycle", codes)
        self.assertIn("missing_owner_task", codes)
        self.assertNotIn("missing_superseded_by", codes)

    def test_archived_lifecycle_state_missing_owner_task(self) -> None:
        fm = {
            "id": "reports.example",
            "title": "Example",
            "doc_type": "report",
            "status": "deprecated",
            "lifecycle_state": "archived",
            "lifecycle": "audit",
        }
        path = self.tmp_root / "docs" / "reports" / "example.md"
        findings = _validate_report(path, fm, self.tmp_root)
        codes = [f.code for f in findings]
        self.assertIn("missing_owner_task", codes)
        self.assertNotIn("missing_superseded_by", codes)

    def test_deferred_lifecycle_state_missing_lifecycle(self) -> None:
        fm = {
            "id": "reports.example",
            "title": "Example",
            "doc_type": "report",
            "status": "active",
            "lifecycle_state": "deferred",
            "owner_task": "OPT-ARC-001",
            "review_after": "2026-07-13",
        }
        path = self.tmp_root / "docs" / "reports" / "example.md"
        findings = _validate_report(path, fm, self.tmp_root)
        codes = [f.code for f in findings]
        self.assertIn("missing_lifecycle", codes)


class TestGeneratedTruthContractValidation(unittest.TestCase):
    def _contract(self, digest: str) -> dict[str, object]:
        return {
            "schema_version": 1,
            "status": "partial",
            "coverage": {
                "scope": "one source",
                "complete": False,
                "fresh": True,
                "method": "exact",
                "checked_items": 1,
                "total_items": 1,
                "failures": 0,
            },
            "source_revision": "a" * 40,
            "generated_at": "2026-08-03T00:00:00+00:00",
            "sources": [{"path": "docs/reports/source.md", "sha256": digest}],
            "limitations": ["repository-only"],
            "does_not_establish": ["runtime-health"],
        }

    def test_extract_truth_contract_requires_exact_fence(self) -> None:
        with self.assertRaisesRegex(ValueError, "truth_contract_missing"):
            extract_truth_contract("# no contract\n")

    def test_source_digest_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "docs" / "reports" / "source.md"
            source.parent.mkdir(parents=True)
            source.write_text("actual\n", encoding="utf-8")
            report = root / "docs" / "_generated" / "report-lifecycle.md"
            report.parent.mkdir(parents=True)
            payload = json.dumps(self._contract("b" * 64), indent=2, sort_keys=True)
            report.write_text(
                "```json audit-report-truth.v1\n" + payload + "\n```\n",
                encoding="utf-8",
            )
            violations = validate_truth_report(report, root)
            self.assertIn("source_digest_mismatch_0", violations)


if __name__ == "__main__":
    unittest.main()
