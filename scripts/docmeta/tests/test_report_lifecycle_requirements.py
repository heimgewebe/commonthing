#!/usr/bin/env python3
from __future__ import annotations

import datetime
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.docmeta.report_lifecycle_requirements import (
    build_truth_contract,
    extract_truth_contract_markdown,
    missing_required_report_field_rules,
    missing_required_report_fields,
    parse_truth_contract_markdown,
    report_truth_migration_state,
    required_report_field_rules,
    source_revision_metadata,
    string_value,
    truth_contract_markdown,
    validate_truth_contract,
)
from scripts.docmeta.report_lifecycle_requirements import _canonical_iso_timestamp


class TestReportLifecycleRequirements(unittest.TestCase):
    def test_non_report_has_no_requirements(self) -> None:
        self.assertEqual(
            missing_required_report_fields(
                {"doc_type": "reference", "status": "active"}
            ),
            (),
        )

    def test_base_and_status_rules(self) -> None:
        cases = [
            ({"doc_type": "report"}, ("lifecycle_state", "status")),
            (
                {"doc_type": "report", "status": "active"},
                ("lifecycle_state", "lifecycle", "review_after"),
            ),
            (
                {"doc_type": "report", "status": "draft"},
                ("lifecycle_state", "review_after"),
            ),
        ]
        for frontmatter, expected in cases:
            with self.subTest(frontmatter=frontmatter):
                self.assertEqual(
                    missing_required_report_fields(frontmatter), expected
                )

    def test_lifecycle_state_rules(self) -> None:
        cases = {
            "active": ("lifecycle", "owner_task", "review_after"),
            "deferred": ("lifecycle", "owner_task", "review_after"),
            "superseded": ("lifecycle", "owner_task", "superseded_by"),
            "archived": ("lifecycle", "owner_task"),
        }
        for state, expected in cases.items():
            with self.subTest(state=state):
                self.assertEqual(
                    missing_required_report_fields(
                        {
                            "doc_type": "report",
                            "status": "deprecated",
                            "lifecycle_state": state,
                        }
                    ),
                    expected,
                )

    def test_archived_report_does_not_require_review_after(self) -> None:
        self.assertEqual(
            missing_required_report_fields(
                {
                    "doc_type": "report",
                    "status": "deprecated",
                    "lifecycle_state": "archived",
                    "lifecycle": "audit",
                    "owner_task": "TASK-1",
                }
            ),
            (),
        )

    def test_unknown_state_adds_no_state_requirements(self) -> None:
        self.assertEqual(
            missing_required_report_fields(
                {
                    "doc_type": "report",
                    "status": "deprecated",
                    "lifecycle_state": "unknown",
                }
            ),
            (),
        )

    def test_deduplication_preserves_first_rule(self) -> None:
        rules = missing_required_report_field_rules(
            {
                "doc_type": "report",
                "status": "active",
                "lifecycle_state": "deferred",
            }
        )
        self.assertEqual(
            tuple((rule.code, rule.field, rule.message) for rule in rules),
            (
                (
                    "missing_lifecycle",
                    "lifecycle",
                    "active reports should define lifecycle",
                ),
                (
                    "missing_review_after",
                    "review_after",
                    "active/draft reports should define review_after",
                ),
                (
                    "missing_owner_task",
                    "owner_task",
                    "deferred reports should define owner_task",
                ),
            ),
        )

    def test_required_rule_precedence_and_messages_are_frozen(self) -> None:
        """Freeze ordered applicable rule definitions before presence filtering.

        When status- and lifecycle_state-derived rules share a finding code,
        the first occurrence wins. This test covers internal compatibility
        ordering. Emitted findings are covered by the validator parity test.
        """

        def rules(frontmatter: dict[str, object]) -> tuple[tuple[str, str, str], ...]:
            return tuple(
                (rule.code, rule.field, rule.message)
                for rule in required_report_field_rules(frontmatter)
            )

        # status=active wins the lifecycle/review_after message over archived.
        self.assertEqual(
            rules(
                {"doc_type": "report", "status": "active", "lifecycle_state": "archived"}
            ),
            (
                (
                    "missing_lifecycle_state",
                    "lifecycle_state",
                    "report documents should define lifecycle_state",
                ),
                (
                    "missing_status",
                    "status",
                    "report documents should define status",
                ),
                (
                    "missing_lifecycle",
                    "lifecycle",
                    "active reports should define lifecycle",
                ),
                (
                    "missing_review_after",
                    "review_after",
                    "active/draft reports should define review_after",
                ),
                (
                    "missing_owner_task",
                    "owner_task",
                    "archived reports should define owner_task",
                ),
            ),
        )
        # superseded contributes superseded_by; status=active still wins lifecycle.
        self.assertEqual(
            rules(
                {"doc_type": "report", "status": "active", "lifecycle_state": "superseded"}
            ),
            (
                (
                    "missing_lifecycle_state",
                    "lifecycle_state",
                    "report documents should define lifecycle_state",
                ),
                (
                    "missing_status",
                    "status",
                    "report documents should define status",
                ),
                (
                    "missing_lifecycle",
                    "lifecycle",
                    "active reports should define lifecycle",
                ),
                (
                    "missing_review_after",
                    "review_after",
                    "active/draft reports should define review_after",
                ),
                (
                    "missing_owner_task",
                    "owner_task",
                    "superseded reports should define owner_task",
                ),
                (
                    "missing_superseded_by",
                    "superseded_by",
                    "superseded reports should define superseded_by",
                ),
            ),
        )
        # status=draft wins review_after message; deferred contributes lifecycle/owner_task.
        self.assertEqual(
            rules(
                {"doc_type": "report", "status": "draft", "lifecycle_state": "deferred"}
            ),
            (
                (
                    "missing_lifecycle_state",
                    "lifecycle_state",
                    "report documents should define lifecycle_state",
                ),
                (
                    "missing_status",
                    "status",
                    "report documents should define status",
                ),
                (
                    "missing_review_after",
                    "review_after",
                    "active/draft reports should define review_after",
                ),
                (
                    "missing_lifecycle",
                    "lifecycle",
                    "deferred reports should define lifecycle",
                ),
                (
                    "missing_owner_task",
                    "owner_task",
                    "deferred reports should define owner_task",
                ),
            ),
        )

    def test_normalization_matches_validator_behavior(self) -> None:
        self.assertEqual(
            missing_required_report_fields(
                {
                    "doc_type": " Report ",
                    "status": " Deprecated ",
                    "lifecycle_state": " ARCHIVED ",
                    "lifecycle": "audit",
                    "owner_task": "TASK-1",
                }
            ),
            (),
        )
        self.assertEqual(string_value(None), "")
        self.assertEqual(string_value([]), "")
        self.assertEqual(string_value({}), "")
        self.assertEqual(string_value(True), "")
        self.assertEqual(string_value(7), "")
        self.assertEqual(string_value(datetime.date(2026, 7, 13)), "")


class TestAuditReportTruthContract(unittest.TestCase):
    def _contract(self, status: str = "pass", **changes: object) -> dict[str, object]:
        coverage: dict[str, object] = {
            "scope": "all contract sources",
            "complete": True,
            "fresh": True,
            "method": "exact",
            "checked_items": 2,
            "total_items": 2,
            "failures": 0,
        }
        coverage.update(changes)
        return build_truth_contract(
            status=status,
            scope=str(coverage["scope"]),
            complete=bool(coverage["complete"]),
            fresh=bool(coverage["fresh"]),
            method=str(coverage["method"]),
            checked_items=int(coverage["checked_items"]),
            total_items=int(coverage["total_items"]),
            failures=int(coverage["failures"]),
            source_revision="a" * 40,
            generated_at="2026-08-03T00:00:00+00:00",
            sources=[{"path": "docs/reports/a.md", "sha256": "b" * 64}],
            limitations=["repository-only"],
            does_not_establish=["runtime_health"],
        )

    def test_valid_positive_contract_passes(self) -> None:
        self.assertEqual(validate_truth_contract(self._contract()), ())

    def test_truth_block_is_single_and_line_anchored(self) -> None:
        contract = self._contract(status="partial")
        markdown = truth_contract_markdown(contract)
        self.assertEqual(extract_truth_contract_markdown(markdown), contract)

        conflicting = {**contract, "status": "fail"}
        with self.assertRaisesRegex(ValueError, "truth_contract_multiple"):
            extract_truth_contract_markdown(
                markdown + truth_contract_markdown(conflicting)
            )
        with self.assertRaisesRegex(ValueError, "truth_contract_inline_fence"):
            extract_truth_contract_markdown(
                "prefix ```json audit-report-truth.v1\n{}\n```\n"
            )
        with self.assertRaisesRegex(ValueError, "truth_contract_nested_fence"):
            extract_truth_contract_markdown(
                "```json audit-report-truth.v1\n```json nested\n{}\n```\n"
            )
        self.assertEqual(
            validate_truth_contract(
                parse_truth_contract_markdown(markdown + markdown)
            ),
            ("truth_contract_multiple",),
        )

    def test_unavailable_provenance_is_typed_and_never_zero_sha(self) -> None:
        zero_contract = self._contract(status="partial")
        zero_contract["source_revision"] = "0" * 40
        zero_contract["generated_at"] = "1970-01-01T00:00:00Z"
        self.assertIn(
            "invalid_source_revision", validate_truth_contract(zero_contract)
        )

        unavailable = {"state": "unavailable", "reason": "local_history_missing"}
        unknown_contract = self._contract(status="unknown", fresh=False)
        unknown_contract["source_revision"] = unavailable
        unknown_contract["generated_at"] = unavailable.copy()
        self.assertEqual(validate_truth_contract(unknown_contract), ())

        positive_contract = self._contract()
        positive_contract["source_revision"] = unavailable
        positive_contract["generated_at"] = unavailable.copy()
        violations = validate_truth_contract(positive_contract)
        self.assertIn("positive_status_source_revision_unavailable", violations)
        self.assertIn("positive_status_generated_at_unavailable", violations)

    def test_positive_status_is_downgraded_for_unsafe_coverage(self) -> None:
        for changes in (
            {"complete": False},
            {"fresh": False},
            {"method": "heuristic"},
            {"failures": 1},
            {"checked_items": 1},
        ):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(ValueError, "positive_status"):
                    self._contract(**changes)

    def test_missing_and_unknown_fields_fail_closed(self) -> None:
        contract = self._contract(status="partial")
        contract.pop("sources")
        contract["surprise"] = True
        violations = validate_truth_contract(contract)
        self.assertIn("missing_sources", violations)
        self.assertIn("unknown_surprise", violations)

    def test_false_source_revision_fails_when_revision_blob_differs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=root,
                check=True,
            )
            source = root / "docs" / "reports" / "source.md"
            source.parent.mkdir(parents=True)
            source.write_text("source v1\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source v1"], cwd=root, check=True)
            old_revision = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=True
            ).stdout.strip()
            old_timestamp = subprocess.run(
                ["git", "show", "-s", "--format=%cI", old_revision],
                cwd=root,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            source.write_text("source v2\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source v2"], cwd=root, check=True)
            contract = build_truth_contract(
                status="partial",
                scope="one source",
                complete=True,
                fresh=True,
                method="exact",
                checked_items=1,
                total_items=1,
                failures=0,
                source_revision=old_revision,
                generated_at=old_timestamp,
                sources=[{
                    "path": "docs/reports/source.md",
                    "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                }],
                limitations=["repository-only"],
                does_not_establish=["runtime-health"],
            )
            self.assertIn(
                "source_revision_digest_mismatch_0",
                validate_truth_contract(contract, root=root),
            )

    def test_migration_classification_is_explicit(self) -> None:
        self.assertEqual(
            report_truth_migration_state({"lifecycle_state": "archived", "status": "active"}),
            "deprecated",
        )
        self.assertEqual(
            report_truth_migration_state({"lifecycle_state": "active", "status": "active"}),
            "not_decision_relevant",
        )


class TestSourceRevisionMetadata(unittest.TestCase):
    def test_revision_tracks_latest_source_commit_not_generator_head(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=root,
                check=True,
            )
            source = root / "source.md"
            source.write_text("source v1\n", encoding="utf-8")
            subprocess.run(["git", "add", "source.md"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "source"],
                cwd=root,
                check=True,
            )
            source_revision = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()

            (root / "generator.py").write_text("# generator\n", encoding="utf-8")
            subprocess.run(["git", "add", "generator.py"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "generator"],
                cwd=root,
                check=True,
            )

            revision, generated_at, fresh = source_revision_metadata(root, [source])
            self.assertEqual(revision, source_revision)
            self.assertTrue(generated_at)
            self.assertTrue(fresh)

            source.write_text("source v2\n", encoding="utf-8")
            revision, _, fresh = source_revision_metadata(root, [source])
            self.assertEqual(revision, source_revision)
            self.assertFalse(fresh)


    def test_commit_timestamp_spelling_is_canonical(self) -> None:
        """Generierte Artefakte dürfen nicht von der git-Version abhängen.

        `git show -s --format=%cI` liefert einen Nullversatz je nach Version
        als `+00:00` oder als `Z`. Landete das roh im Artefakt, wäre dessen
        Inhalt nicht mehr reproduzierbar: `validate_generated_artifacts`
        meldete einen Diff, sobald Erzeugung und Prüfung auf unterschiedlichen
        git-Versionen liefen.
        """
        self.assertEqual(
            _canonical_iso_timestamp("2026-08-24T17:04:00Z"),
            _canonical_iso_timestamp("2026-08-24T17:04:00+00:00"),
        )
        self.assertEqual(
            _canonical_iso_timestamp("2026-08-24T17:04:00Z"),
            "2026-08-24T17:04:00+00:00",
        )
        self.assertEqual(
            _canonical_iso_timestamp("2026-09-11T07:02:42+02:00"),
            "2026-09-11T07:02:42+02:00",
        )
        # Ohne Zeitzone oder unparsbar bleibt es fail-closed.
        self.assertIsNone(_canonical_iso_timestamp("2026-08-24T17:04:00"))
        self.assertIsNone(_canonical_iso_timestamp("not-a-timestamp"))

    def test_generated_at_matches_revision_across_utc_offset_spellings(self) -> None:
        """Zero-offset commits: `+00:00` und `Z` sind derselbe Zeitpunkt.

        `git show -s --format=%cI` schreibt einen Nullversatz je nach
        git-Version als `+00:00` oder als `Z`. Ein Stringvergleich machte aus
        dieser Schreibweise einen `generated_at_revision_mismatch`, obwohl die
        Provenance korrekt war.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=root,
                check=True,
            )
            source = root / "source.md"
            source.write_text("source v1\n", encoding="utf-8")
            subprocess.run(["git", "add", "source.md"], cwd=root, check=True)
            utc_date = "2026-08-24T17:04:00 +0000"
            subprocess.run(
                ["git", "commit", "-q", "-m", "source"],
                cwd=root,
                check=True,
                env={
                    **os.environ,
                    "GIT_AUTHOR_DATE": utc_date,
                    "GIT_COMMITTER_DATE": utc_date,
                },
            )
            revision = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            digest = hashlib.sha256(source.read_bytes()).hexdigest()

            def contract_for(generated_at: str) -> dict[str, object]:
                return build_truth_contract(
                    status="pass",
                    scope="one exact source",
                    complete=True,
                    fresh=True,
                    method="exact",
                    checked_items=1,
                    total_items=1,
                    failures=0,
                    source_revision=revision,
                    generated_at=generated_at,
                    sources=[{"path": "source.md", "sha256": digest}],
                    limitations=["repository-only"],
                    does_not_establish=["runtime_health"],
                )

            # Beide Schreibweisen müssen durchgehen, unabhängig davon, welche
            # das lokale git erzeugt.
            for spelling in ("2026-08-24T17:04:00+00:00", "2026-08-24T17:04:00Z"):
                with self.subTest(spelling=spelling):
                    self.assertNotIn(
                        "generated_at_revision_mismatch",
                        validate_truth_contract(contract_for(spelling), root=root),
                    )

            # Ein echter Zeitpunktunterschied bleibt ein Befund.
            self.assertIn(
                "generated_at_revision_mismatch",
                validate_truth_contract(contract_for("2026-08-24T17:04:01Z"), root=root),
            )

    def test_shallow_history_stays_read_only_and_is_explicitly_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            source_repo = temp_root / "source-repo"
            source_repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=source_repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=source_repo, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=source_repo,
                check=True,
            )
            source = source_repo / "source.md"
            source.write_text("source v1\n", encoding="utf-8")
            subprocess.run(["git", "add", "source.md"], cwd=source_repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=source_repo, check=True)
            source_revision = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=source_repo,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            generated_at = subprocess.run(
                ["git", "show", "-s", "--format=%cI", source_revision],
                cwd=source_repo,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            source_digest = hashlib.sha256(source.read_bytes()).hexdigest()

            (source_repo / "generator.py").write_text("# generator\n", encoding="utf-8")
            subprocess.run(["git", "add", "generator.py"], cwd=source_repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "generator"], cwd=source_repo, check=True)

            checkout = temp_root / "checkout"
            subprocess.run(
                ["git", "clone", "-q", "--depth=1", source_repo.resolve().as_uri(), str(checkout)],
                check=True,
            )
            self.assertEqual(
                subprocess.run(
                    ["git", "rev-parse", "--is-shallow-repository"],
                    cwd=checkout,
                    text=True,
                    capture_output=True,
                    check=True,
                ).stdout.strip(),
                "true",
            )

            resolved_revision, resolved_generated_at, resolved_fresh = source_revision_metadata(
                checkout, [checkout / "source.md"]
            )
            self.assertEqual(
                resolved_revision,
                {"state": "unavailable", "reason": "git_history_unavailable"},
            )
            self.assertEqual(resolved_generated_at, resolved_revision)
            self.assertFalse(resolved_fresh)

            contract = build_truth_contract(
                status="pass",
                scope="one exact source",
                complete=True,
                fresh=True,
                method="exact",
                checked_items=1,
                total_items=1,
                failures=0,
                source_revision=source_revision,
                generated_at=generated_at,
                sources=[{"path": "source.md", "sha256": source_digest}],
                limitations=["repository-only"],
                does_not_establish=["runtime_health"],
            )
            self.assertIn(
                "source_history_unavailable",
                validate_truth_contract(contract, root=checkout),
            )
            self.assertEqual(
                subprocess.run(
                    ["git", "rev-parse", "--is-shallow-repository"],
                    cwd=checkout,
                    text=True,
                    capture_output=True,
                    check=True,
                ).stdout.strip(),
                "true",
            )


if __name__ == "__main__":
    unittest.main()
