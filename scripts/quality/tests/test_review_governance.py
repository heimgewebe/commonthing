from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from scripts.quality.review_governance import (
    Bundle,
    DiffStats,
    GovernanceError,
    canonical_product_docs_from_manifest_text,
    evaluate_evidence,
    generate_bundle,
    generate_materialized_bundle,
    load_allowed_attesters,
    minimum_risk_for_paths,
    parse_risk_class,
    _materialized_diff_has_text_hunk,
    _parse_numstat_z,
)


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def _comment(
    record: dict,
    *,
    association: str = "OWNER",
    author: str = "alex",
    report_text: str | None = None,
    report_sha256: str | None = None,
    comment_id: int = 1,
) -> dict:
    report = report_text or (
        f"Unabhängiger Reviewbericht von {record.get('reviewer')} zur Achse "
        f"{record.get('review_axis')}. Der exakte Diff wurde auf Korrektheit, "
        "Regressionen, Fehlerszenarien und die im Auftrag genannten Invarianten "
        "geprüft. Es bleiben keine unaufgelösten Mergeblocker."
    )
    payload = dict(record)
    normalized_report = report.replace("\r\n", "\n").replace("\r", "\n").strip()
    payload["report_sha256"] = (
        report_sha256 or hashlib.sha256(normalized_report.encode("utf-8")).hexdigest()
    )
    return {
        "id": comment_id,
        "body": report
        + "\n<!-- weltgewebe-review-evidence\n"
        + json.dumps(payload)
        + "\n-->",
        "author_association": association,
        "user": {"login": author},
        "html_url": "https://example.invalid/review",
        "created_at": "2026-07-14T12:00:00Z",
        "updated_at": "2026-07-14T12:00:00Z",
    }


def _forwarded_comment(
    bundle: Bundle, *, reviewer: str, axis: str, author: str = "alex", comment_id: int = 1
) -> dict:
    report = (
        f"Extern eingeholter Reviewbericht von {reviewer} zur Achse {axis}. "
        "Der vorgelegte Diff wurde vollständig geprüft, einschließlich Korrektheit, "
        "Regressionen und relevanter Fehlerszenarien. Alle benannten Befunde wurden "
        "nachgearbeitet; es verbleiben keine bekannten Mergeblocker."
    )
    payload = {
        "schema_version": 1,
        "base_sha": bundle.base_sha,
        "head_sha": bundle.head_sha,
        "diff_sha256": bundle.diff_sha256,
        "reviewer": reviewer,
        "review_axis": axis,
        "verdict": "PASS",
        "findings_resolved": True,
    }
    return {
        "id": comment_id,
        "body": report + "\n<!-- weltgewebe-forwarded-review\n" + json.dumps(payload) + "\n-->",
        "author_association": "OWNER",
        "user": {"login": author},
        "html_url": "https://example.invalid/forwarded-review",
        "created_at": "2026-07-22T12:00:00Z",
        "updated_at": "2026-07-22T12:00:00Z",
    }


ALLOWED_ATTESTERS = frozenset({"alex"})
TRUSTED_PRODUCT_DOCS = frozenset({"docs/specs/ui-interaction.md"})


def _review(
    bundle: Bundle,
    *,
    state: str = "APPROVED",
    author: str = "alex",
    association: str = "OWNER",
    commit_id: str | None = None,
    review_id: int = 1,
    submitted_at: str = "2026-07-14T12:00:00Z",
) -> dict:
    return {
        "id": review_id,
        "state": state,
        "commit_id": commit_id or bundle.head_sha,
        "author_association": association,
        "user": {"login": author},
        "html_url": "https://example.invalid/native-review",
        "submitted_at": submitted_at,
    }


def _evaluate(
    *,
    bundle: Bundle,
    risk_class: str,
    comments: list[dict],
    reviews: list[dict] | None = None,
    pr_body: str | None = None,
    canonical_product_docs: frozenset[str] | None = TRUSTED_PRODUCT_DOCS,
) -> dict:
    return evaluate_evidence(
        bundle=bundle,
        risk_class=risk_class,
        comments=comments,
        reviews=reviews or [],
        allowed_attesters=ALLOWED_ATTESTERS,
        pr_body=pr_body,
        canonical_product_docs=canonical_product_docs,
    )


def _bundle(
    *,
    paths: tuple[str, ...] = ("docs/example.md",),
    changed_lines: int = 2,
    attention_paths: tuple[str, ...] = (),
) -> Bundle:
    additions = changed_lines // 2
    deletions = changed_lines - additions
    return Bundle(
        pr_number=42,
        base_sha="a" * 40,
        head_sha="b" * 40,
        merge_base_sha="c" * 40,
        diff_sha256="d" * 64,
        patch_sha256="e" * 64,
        manifest_path=Path("manifest.json"),
        diff_path=Path("change.diff"),
        patch_path=Path("change.patch"),
        request_path=Path("request.md"),
        stats=DiffStats(paths, additions, deletions, (), (), attention_paths),
    )


def _record(
    bundle: Bundle, *, reviewer: str, axis: str, risk: str, verdict: str = "PASS"
) -> dict:
    return {
        "schema_version": 1,
        "pr_number": bundle.pr_number,
        "base_sha": bundle.base_sha,
        "head_sha": bundle.head_sha,
        "diff_sha256": bundle.diff_sha256,
        "risk_class": risk,
        "reviewer": reviewer,
        "review_axis": axis,
        "verdict": verdict,
        "findings_resolved": verdict == "PASS",
    }


class AttentionImpactReviewGateTests(unittest.TestCase):
    def _r2_comments(self, bundle: Bundle) -> list[dict]:
        return [
            _comment(
                _record(bundle, reviewer="Reviewer A", axis="correctness", risk="R2"),
                comment_id=1,
            ),
            _comment(
                _record(bundle, reviewer="Reviewer B", axis="testing", risk="R2"),
                comment_id=2,
            ),
        ]

    def test_product_logic_requires_attention_marker(self) -> None:
        bundle = _bundle(paths=("apps/web/src/app.ts",))
        result = _evaluate(
            bundle=bundle,
            risk_class="R2",
            comments=self._r2_comments(bundle),
            pr_body="",
        )
        self.assertFalse(result["pass"])
        self.assertTrue(result["attention_impact_required"])
        self.assertIsNone(result["attention_impact_decision"])
        self.assertTrue(
            any("Attention impact:" in reason for reason in result["reasons"]),
            result["reasons"],
        )

    def test_contract_marker_satisfies_edit_sensitive_meta_gate(self) -> None:
        bundle = _bundle(paths=("apps/web/src/app.ts", "docs/specs/ui-interaction.md"))
        result = _evaluate(
            bundle=bundle,
            risk_class="R2",
            comments=self._r2_comments(bundle),
            pr_body="<!-- weltgewebe-attention-impact: contract -->",
        )
        self.assertTrue(result["pass"], result["reasons"])
        self.assertEqual(result["attention_impact_decision"], "contract")

    def test_rename_out_of_product_logic_still_requires_attention_marker(self) -> None:
        bundle = _bundle(
            paths=("docs/old.ts",),
            attention_paths=("apps/web/src/old.ts", "docs/old.ts"),
        )
        result = _evaluate(
            bundle=bundle,
            risk_class="R2",
            comments=self._r2_comments(bundle),
            pr_body="",
        )
        self.assertFalse(result["pass"])
        self.assertTrue(result["attention_impact_required"])
        self.assertTrue(
            any("Attention impact:" in reason for reason in result["reasons"]),
            result["reasons"],
        )

    def test_contract_marker_cannot_be_swapped_in_after_none_pass(self) -> None:
        bundle = _bundle(paths=("apps/web/src/app.ts",))
        none_body = (
            "<!-- weltgewebe-attention-impact: none -->\n"
            "<!-- weltgewebe-attention-rationale: Pure refactor with no personal domain semantics change. -->"
        )
        initial = _evaluate(
            bundle=bundle,
            risk_class="R2",
            comments=self._r2_comments(bundle),
            pr_body=none_body,
        )
        self.assertTrue(initial["pass"], initial["reasons"])

        edited = _evaluate(
            bundle=bundle,
            risk_class="R2",
            comments=self._r2_comments(bundle),
            pr_body="<!-- weltgewebe-attention-impact: contract -->",
        )
        self.assertFalse(edited["pass"])
        self.assertTrue(
            any("canonical product contract" in reason for reason in edited["reasons"]),
            edited["reasons"],
        )

    def test_none_marker_requires_concrete_rationale(self) -> None:
        bundle = _bundle(paths=("contracts/domain/message.schema.json",))
        result = _evaluate(
            bundle=bundle,
            risk_class="R2",
            comments=self._r2_comments(bundle),
            pr_body="<!-- weltgewebe-attention-impact: none -->",
        )
        self.assertFalse(result["pass"])
        self.assertTrue(any("rationale" in reason for reason in result["reasons"]))

        result = _evaluate(
            bundle=bundle,
            risk_class="R2",
            comments=self._r2_comments(bundle),
            pr_body=(
                "<!-- weltgewebe-attention-impact: none -->\n"
                "<!-- weltgewebe-attention-rationale: Schema-only compatibility refactor; no personal domain semantics change. -->"
            ),
        )
        self.assertTrue(result["pass"], result["reasons"])
        self.assertEqual(result["attention_impact_decision"], "none")

    def test_non_product_diff_does_not_require_marker(self) -> None:
        bundle = _bundle(paths=("docs/example.md",))
        result = _evaluate(
            bundle=bundle,
            risk_class="R0",
            comments=[],
            pr_body="",
        )
        self.assertTrue(result["pass"], result["reasons"])
        self.assertFalse(result["attention_impact_required"])



class TrustedProductManifestTests(unittest.TestCase):
    def test_extracts_product_docs_from_trusted_manifest(self) -> None:
        manifest = (
            "zones:\n"
            "  product:\n"
            "    path: docs/specs/\n"
            "    canonical_docs:\n"
            "      - alpha.md\n"
            "      - beta.md\n"
            "  reality:\n"
            "    path: runtime/\n"
        )
        self.assertEqual(
            canonical_product_docs_from_manifest_text(manifest),
            {"docs/specs/alpha.md", "docs/specs/beta.md"},
        )

    def test_manifest_parser_fails_closed_on_duplicate_product_zone(self) -> None:
        manifest = (
            "zones:\n"
            "  product:\n"
            "    path: docs/specs/\n"
            "    canonical_docs:\n"
            "      - alpha.md\n"
            "  product:\n"
            "    path: docs/other/\n"
            "    canonical_docs:\n"
            "      - beta.md\n"
        )
        with self.assertRaises(ValueError):
            canonical_product_docs_from_manifest_text(manifest)


class RiskParsingTests(unittest.TestCase):
    def test_exactly_one_risk_marker_is_required(self) -> None:
        self.assertEqual(parse_risk_class("<!-- weltgewebe-risk: R2 -->"), "R2")
        self.assertIsNone(parse_risk_class("none"))
        self.assertIsNone(
            parse_risk_class("<!-- weltgewebe-risk: R1 --><!-- weltgewebe-risk: R2 -->")
        )
        self.assertIsNone(
            parse_risk_class("<!-- weltgewebe-risk: R2 --><!-- weltgewebe-risk: R2 -->")
        )

    def test_sensitive_paths_raise_minimum_risk(self) -> None:
        self.assertEqual(minimum_risk_for_paths(["docs/example.md"]), "R0")
        self.assertEqual(minimum_risk_for_paths(["apps/web/README.md"]), "R0")
        self.assertEqual(minimum_risk_for_paths(["docs/images/map.png"]), "R1")
        self.assertEqual(minimum_risk_for_paths(["apps/web/static/map.webp"]), "R1")
        self.assertEqual(minimum_risk_for_paths(["apps/web/src/app.ts"]), "R2")
        self.assertEqual(minimum_risk_for_paths([".github/workflows/ci.yml"]), "R3")
        self.assertEqual(
            minimum_risk_for_paths([".github/review-evidence-authorities.json"]),
            "R3",
        )
        self.assertEqual(
            minimum_risk_for_paths(["scripts/quality/review_governance.py"]),
            "R3",
        )
        self.assertEqual(
            minimum_risk_for_paths(["docs/process/merge-quality-gate.md"]),
            "R3",
        )
        self.assertEqual(
            minimum_risk_for_paths([".github/PULL_REQUEST_TEMPLATE.md"]),
            "R3",
        )
        self.assertEqual(minimum_risk_for_paths(["apps/api/src/auth.rs"]), "R3")


class BundleTests(unittest.TestCase):
    def test_numstat_parser_handles_tabs_and_mixed_rename_records(self) -> None:
        raw = b"3\t2\tpath\twith-tab.txt\0-\t-\t\0old.bin\0docs/new.bin\0"
        additions, deletions, binary = _parse_numstat_z(raw)
        self.assertEqual((additions, deletions), (3, 2))
        self.assertEqual(binary, ("docs/new.bin",))

    def test_numstat_parser_rejects_malformed_streams(self) -> None:
        malformed = (
            b"1\t0\tfile.txt",
            b"-\t1\tfile.bin\0",
            b"1\t0\t\0old.txt\0",
        )
        for raw in malformed:
            with self.subTest(raw=raw):
                with self.assertRaises(GovernanceError):
                    _parse_numstat_z(raw)

    def test_bundle_hash_is_deterministic_for_same_commits(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            repo.mkdir()
            _git(repo, "init")
            _git(repo, "config", "user.name", "Test")
            _git(repo, "config", "user.email", "test@example.invalid")
            (repo / "README.md").write_text("one\n", encoding="utf-8")
            _git(repo, "add", "README.md")
            _git(repo, "commit", "-m", "base")
            base = _git(repo, "rev-parse", "HEAD")
            (repo / "README.md").write_text("one\ntwo\n", encoding="utf-8")
            _git(repo, "commit", "-am", "change")
            head = _git(repo, "rev-parse", "HEAD")

            first = generate_bundle(
                repo=repo,
                output_dir=Path(temp_dir) / "out-1",
                base_revision=base,
                head_revision=head,
                pr_number=7,
                risk_class="R0",
            )
            second = generate_bundle(
                repo=repo,
                output_dir=Path(temp_dir) / "out-2",
                base_revision=base,
                head_revision=head,
                pr_number=7,
                risk_class="R0",
            )
            self.assertEqual(first.diff_sha256, second.diff_sha256)
            self.assertEqual(first.patch_sha256, second.patch_sha256)
            self.assertEqual(
                first.diff_path.read_bytes(), second.diff_path.read_bytes()
            )
            self.assertEqual(first.stats.changed_files, ("README.md",))

    def test_local_binary_bundle_marks_binary_as_opaque(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            repo.mkdir()
            _git(repo, "init", "-b", "main")
            _git(repo, "config", "user.name", "Test")
            _git(repo, "config", "user.email", "test@example.invalid")
            (repo / "README.md").write_text("base\n", encoding="utf-8")
            _git(repo, "add", "README.md")
            _git(repo, "commit", "-m", "base")
            base = _git(repo, "rev-parse", "HEAD")

            (repo / "image.bin").write_bytes(b"\x00\x01binary-payload\xff")
            _git(repo, "add", "image.bin")
            _git(repo, "commit", "-m", "add binary")
            head = _git(repo, "rev-parse", "HEAD")

            bundle = generate_bundle(
                repo=repo,
                output_dir=Path(temp_dir) / "out",
                base_revision=base,
                head_revision=head,
                pr_number=9,
                risk_class="R2",
            )
            self.assertEqual(bundle.stats.binary_files, ("image.bin",))
            self.assertEqual(bundle.stats.opaque_files, ("image.bin",))
            manifest = json.loads(bundle.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["stats"]["binary_files"], ["image.bin"])
            self.assertEqual(manifest["stats"]["opaque_files"], ["image.bin"])
            self.assertIn(
                "Nicht zuverlässig prüfbare und daher blockierende Dateien",
                bundle.request_path.read_text(encoding="utf-8"),
            )

    def test_local_binary_rename_uses_destination_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            repo.mkdir()
            _git(repo, "init", "-b", "main")
            _git(repo, "config", "user.name", "Test")
            _git(repo, "config", "user.email", "test@example.invalid")
            (repo / "old.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00binary")
            _git(repo, "add", "old.png")
            _git(repo, "commit", "-m", "base image")
            base = _git(repo, "rev-parse", "HEAD")
            (repo / "docs").mkdir()
            _git(repo, "mv", "old.png", "docs/new.png")
            _git(repo, "commit", "-m", "rename image")
            head = _git(repo, "rev-parse", "HEAD")

            bundle = generate_bundle(
                repo=repo,
                output_dir=Path(temp_dir) / "out",
                base_revision=base,
                head_revision=head,
                pr_number=10,
                risk_class="R1",
            )
            self.assertEqual(bundle.stats.changed_files, ("docs/new.png",))
            self.assertEqual(
                set(bundle.stats.attention_files), {"old.png", "docs/new.png"}
            )
            self.assertEqual(bundle.stats.binary_files, ("docs/new.png",))

    def test_local_multiple_text_and_binary_renames_are_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            repo.mkdir()
            _git(repo, "init", "-b", "main")
            _git(repo, "config", "user.name", "Test")
            _git(repo, "config", "user.email", "test@example.invalid")
            (repo / "old.txt").write_text("unchanged text\n", encoding="utf-8")
            (repo / "old.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00binary")
            _git(repo, "add", "old.txt", "old.png")
            _git(repo, "commit", "-m", "base files")
            base = _git(repo, "rev-parse", "HEAD")

            (repo / "docs").mkdir()
            _git(repo, "mv", "old.txt", "docs/new.txt")
            _git(repo, "mv", "old.png", "docs/new.png")
            _git(repo, "commit", "-m", "rename files")
            head = _git(repo, "rev-parse", "HEAD")

            bundle = generate_bundle(
                repo=repo,
                output_dir=Path(temp_dir) / "out",
                base_revision=base,
                head_revision=head,
                pr_number=11,
                risk_class="R1",
            )
            self.assertEqual(
                bundle.stats.changed_files,
                ("docs/new.png", "docs/new.txt"),
            )
            self.assertEqual(bundle.stats.binary_files, ("docs/new.png",))
            self.assertEqual(
                set(bundle.stats.attention_files),
                {"old.txt", "old.png", "docs/new.txt", "docs/new.png"},
            )
            self.assertEqual(bundle.stats.changed_lines, 0)

    def test_materialized_github_bundle_is_hash_bound_and_validated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            diff_file = root / "pr.diff"
            patch_file = root / "pr.patch"
            metadata_file = root / "metadata.json"
            diff_file.write_bytes(
                b"diff --git a/a.txt b/a.txt\n+new\n"
                b"diff --git a/image.png b/image.png\nBinary files differ\n"
            )
            patch_file.write_bytes(b"From abc Mon Sep 17 00:00:00 2001\n+new\n")
            metadata = {
                "schema_version": 1,
                "pr_number": 17,
                "base_sha": "a" * 40,
                "head_sha": "b" * 40,
                "merge_base_sha": "c" * 40,
                "changed_file_count": 2,
                "changed_files": ["a.txt", "image.png"],
                "additions": 1,
                "deletions": 0,
                "opaque_files": ["image.png"],
            }
            metadata_file.write_text(json.dumps(metadata), encoding="utf-8")
            bundle = generate_materialized_bundle(
                output_dir=root / "out",
                metadata_file=metadata_file,
                diff_file=diff_file,
                patch_file=patch_file,
                risk_class="R2",
            )
            self.assertEqual(
                bundle.diff_sha256,
                __import__("hashlib").sha256(diff_file.read_bytes()).hexdigest(),
            )
            self.assertEqual(bundle.stats.opaque_files, ("image.png",))
            manifest = json.loads(bundle.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["source"], "github-pull-api")
            self.assertEqual(manifest["stats"]["opaque_files"], ["image.png"])

            metadata["changed_file_count"] = 1
            metadata_file.write_text(json.dumps(metadata), encoding="utf-8")
            with self.assertRaises(GovernanceError):
                generate_materialized_bundle(
                    output_dir=root / "invalid",
                    metadata_file=metadata_file,
                    diff_file=diff_file,
                    patch_file=patch_file,
                    risk_class="R2",
                )

            metadata["changed_file_count"] = 2
            metadata_file.write_text(json.dumps(metadata), encoding="utf-8")
            diff_file.write_bytes(b"diff --git a/a.txt b/a.txt\n+new\n")
            with self.assertRaisesRegex(GovernanceError, "diff file count"):
                generate_materialized_bundle(
                    output_dir=root / "truncated",
                    metadata_file=metadata_file,
                    diff_file=diff_file,
                    patch_file=patch_file,
                    risk_class="R2",
                )

            metadata["changed_file_count"] = True
            metadata_file.write_text(json.dumps(metadata), encoding="utf-8")
            with self.assertRaisesRegex(GovernanceError, "non-negative integer"):
                generate_materialized_bundle(
                    output_dir=root / "boolean-count",
                    metadata_file=metadata_file,
                    diff_file=diff_file,
                    patch_file=patch_file,
                    risk_class="R2",
                )

    def test_review_workflow_keeps_all_patchless_files_opaque_candidates(self) -> None:
        workflow_path = (
            Path(__file__).resolve().parents[3]
            / ".github/workflows/review-evidence.yml"
        )
        workflow = workflow_path.read_text(encoding="utf-8")
        match = re.search(
            r"opaque_files:\s*\[(.*?)\n\s*\]",
            workflow,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(match)
        block = match.group(1)
        self.assertIn(
            'select((has("patch") | not) or .patch == null)',
            block,
        )
        self.assertNotIn(".additions", block)
        self.assertNotIn(".deletions", block)

    def test_materialized_patchless_text_is_resolved_by_full_diff_hunk(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            diff_file = root / "pr.diff"
            patch_file = root / "pr.patch"
            metadata_file = root / "metadata.json"
            diff_file.write_bytes(
                b"diff --git a/large.py b/large.py\n"
                b"index 1111111..2222222 100644\n"
                b"--- a/large.py\n"
                b"+++ b/large.py\n"
                b"@@ -1 +1,2 @@\n"
                b" old\n"
                b"+new\n"
            )
            patch_file.write_bytes(diff_file.read_bytes())
            metadata_file.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "pr_number": 18,
                        "base_sha": "a" * 40,
                        "head_sha": "b" * 40,
                        "merge_base_sha": "c" * 40,
                        "changed_file_count": 1,
                        "changed_files": ["large.py"],
                        "additions": 5000,
                        "deletions": 20,
                        "opaque_files": ["large.py"],
                    }
                ),
                encoding="utf-8",
            )

            bundle = generate_materialized_bundle(
                output_dir=root / "out",
                metadata_file=metadata_file,
                diff_file=diff_file,
                patch_file=patch_file,
                risk_class="R3",
            )

            self.assertEqual(bundle.stats.opaque_files, ())

    def test_materialized_text_hunk_proof_rejects_binary_markers(self) -> None:
        for binary_marker in (
            b"GIT binary patch\nliteral 0\n",
            b"Binary files a/blob.bin and b/blob.bin differ\n",
        ):
            with self.subTest(binary_marker=binary_marker):
                diff_bytes = (
                    b"diff --git a/blob.bin b/blob.bin\n"
                    + binary_marker
                    + b"@@ -1 +1 @@\n"
                )
                self.assertFalse(
                    _materialized_diff_has_text_hunk(diff_bytes, "blob.bin")
                )

    def test_materialized_text_hunk_proof_rejects_quoted_and_renamed_paths(self) -> None:
        quoted = (
            b'diff --git "a/space name.py" "b/space name.py"\n'
            b"@@ -1 +1 @@\n"
        )
        renamed = (
            b"diff --git a/old.py b/new.py\n"
            b"similarity index 90%\n"
            b"rename from old.py\n"
            b"rename to new.py\n"
            b"@@ -1 +1 @@\n"
        )
        self.assertFalse(
            _materialized_diff_has_text_hunk(quoted, "space name.py")
        )
        self.assertFalse(_materialized_diff_has_text_hunk(renamed, "new.py"))

    def test_materialized_text_hunk_proof_ignores_embedded_fake_headers(self) -> None:
        diff_bytes = (
            b"diff --git a/carrier.txt b/carrier.txt\n"
            b"--- a/carrier.txt\n"
            b"+++ b/carrier.txt\n"
            b"@@ -1 +1,3 @@\n"
            b" safe\n"
            b"+diff --git a/target.py b/target.py\n"
            b"+@@ -1 +1 @@\n"
        )
        self.assertFalse(
            _materialized_diff_has_text_hunk(diff_bytes, "target.py")
        )

    def test_materialized_patchless_file_without_text_hunk_stays_opaque(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            diff_file = root / "pr.diff"
            patch_file = root / "pr.patch"
            metadata_file = root / "metadata.json"
            diff_file.write_bytes(
                b"diff --git a/large.py b/large.py\n"
                b"index 1111111..2222222 100644\n"
                b"--- a/large.py\n"
                b"+++ b/large.py\n"
            )
            patch_file.write_bytes(diff_file.read_bytes())
            metadata_file.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "pr_number": 19,
                        "base_sha": "a" * 40,
                        "head_sha": "b" * 40,
                        "merge_base_sha": "c" * 40,
                        "changed_file_count": 1,
                        "changed_files": ["large.py"],
                        "additions": 5000,
                        "deletions": 20,
                        "opaque_files": ["large.py"],
                    }
                ),
                encoding="utf-8",
            )

            bundle = generate_materialized_bundle(
                output_dir=root / "out",
                metadata_file=metadata_file,
                diff_file=diff_file,
                patch_file=patch_file,
                risk_class="R3",
            )

            self.assertEqual(bundle.stats.opaque_files, ("large.py",))

    def test_materialized_paths_reject_controls_and_opaque_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            diff_file = root / "pr.diff"
            patch_file = root / "pr.patch"
            metadata_file = root / "metadata.json"
            diff_file.write_bytes(b"diff --git a/a.txt b/a.txt\n+new\n")
            patch_file.write_bytes(b"From abc Mon Sep 17 00:00:00 2001\n+new\n")
            metadata = {
                "schema_version": 1,
                "pr_number": 17,
                "base_sha": "a" * 40,
                "head_sha": "b" * 40,
                "merge_base_sha": "c" * 40,
                "changed_file_count": 1,
                "changed_files": ["bad\nname.txt"],
                "additions": 1,
                "deletions": 0,
                "opaque_files": [],
            }
            metadata_file.write_text(json.dumps(metadata), encoding="utf-8")
            with self.assertRaisesRegex(GovernanceError, "invalid path"):
                generate_materialized_bundle(
                    output_dir=root / "bad-path",
                    metadata_file=metadata_file,
                    diff_file=diff_file,
                    patch_file=patch_file,
                    risk_class="R2",
                )

            metadata["changed_files"] = ["a.txt"]
            metadata["opaque_files"] = ["a.txt", "a.txt"]
            metadata_file.write_text(json.dumps(metadata), encoding="utf-8")
            with self.assertRaisesRegex(GovernanceError, "opaque_files.*duplicates"):
                generate_materialized_bundle(
                    output_dir=root / "duplicate-opaque",
                    metadata_file=metadata_file,
                    diff_file=diff_file,
                    patch_file=patch_file,
                    risk_class="R2",
                )

    def test_materialized_bundle_enforces_artifact_size_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            diff_file = root / "pr.diff"
            patch_file = root / "pr.patch"
            metadata_file = root / "metadata.json"
            diff_file.write_bytes(b"diff --git a/a.txt b/a.txt\n" + b"x" * 80)
            patch_file.write_bytes(b"p" * 80)
            metadata_file.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "pr_number": 17,
                        "base_sha": "a" * 40,
                        "head_sha": "b" * 40,
                        "merge_base_sha": "c" * 40,
                        "changed_file_count": 1,
                        "changed_files": ["a.txt"],
                        "additions": 1,
                        "deletions": 0,
                        "opaque_files": [],
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch("scripts.quality.review_governance.MAX_ARTIFACT_BYTES", 64):
                with self.assertRaisesRegex(GovernanceError, "safety limit"):
                    generate_materialized_bundle(
                        output_dir=root / "out",
                        metadata_file=metadata_file,
                        diff_file=diff_file,
                        patch_file=patch_file,
                        risk_class="R1",
                    )

    def test_local_patch_uses_merge_base_not_moving_base_tip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            repo.mkdir()
            _git(repo, "init", "-b", "main")
            _git(repo, "config", "user.name", "Test")
            _git(repo, "config", "user.email", "test@example.invalid")
            (repo / "common.txt").write_text("base\n", encoding="utf-8")
            _git(repo, "add", "common.txt")
            _git(repo, "commit", "-m", "common base")
            _git(repo, "branch", "feature")

            (repo / "main-only.txt").write_text("main side\n", encoding="utf-8")
            _git(repo, "add", "main-only.txt")
            _git(repo, "commit", "-m", "main side change")
            main_tip = _git(repo, "rev-parse", "HEAD")

            _git(repo, "checkout", "feature")
            (repo / "feature.txt").write_text("feature side\n", encoding="utf-8")
            _git(repo, "add", "feature.txt")
            _git(repo, "commit", "-m", "feature side change")
            feature_tip = _git(repo, "rev-parse", "HEAD")

            bundle = generate_bundle(
                repo=repo,
                output_dir=Path(temp_dir) / "out",
                base_revision=main_tip,
                head_revision=feature_tip,
                pr_number=8,
                risk_class="R2",
            )
            patch = bundle.patch_path.read_text(encoding="utf-8")
            self.assertIn("feature side change", patch)
            self.assertNotIn("main side change", patch)


class AuthorityTests(unittest.TestCase):
    def test_authority_file_is_strict_and_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "authorities.json"
            path.write_text(
                json.dumps(
                    {"schema_version": 1, "allowed_attesters": ["Alex-DerMohr"]}
                ),
                encoding="utf-8",
            )
            self.assertEqual(load_allowed_attesters(path), frozenset({"alex-dermohr"}))
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "allowed_attesters": ["alex", "ALEX"],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(GovernanceError):
                load_allowed_attesters(path)


class EvidenceTests(unittest.TestCase):
    def test_r1_accepts_one_exact_authorized_review(self) -> None:
        bundle = _bundle(paths=("config/example.json",))
        result = _evaluate(
            bundle=bundle,
            risk_class="R1",
            comments=[
                _comment(
                    _record(
                        bundle, reviewer="Reviewer A", axis="correctness", risk="R1"
                    )
                )
            ],
        )
        self.assertTrue(result["pass"], result["reasons"])

    def test_stale_review_is_rejected(self) -> None:
        bundle = _bundle(paths=("config/example.json",))
        record = _record(bundle, reviewer="Reviewer A", axis="correctness", risk="R1")
        record["head_sha"] = "f" * 40
        result = _evaluate(bundle=bundle, risk_class="R1", comments=[_comment(record)])
        self.assertFalse(result["pass"])
        self.assertEqual(result["stale_evidence_count"], 1)

    def test_r2_requires_distinct_reviewer_identities_and_axes(self) -> None:
        bundle = _bundle(paths=("apps/web/src/app.ts",))
        comments = [
            _comment(
                _record(bundle, reviewer="Reviewer A", axis="correctness", risk="R2")
            ),
            _comment(_record(bundle, reviewer="Reviewer B", axis="testing", risk="R2")),
        ]
        result = _evaluate(bundle=bundle, risk_class="R2", comments=comments)
        self.assertTrue(result["pass"], result["reasons"])

        duplicate = [
            _comment(
                _record(bundle, reviewer="Reviewer A", axis="correctness", risk="R2")
            ),
            _comment(_record(bundle, reviewer="Reviewer A", axis="testing", risk="R2")),
        ]
        result = _evaluate(bundle=bundle, risk_class="R2", comments=duplicate)
        self.assertFalse(result["pass"])
        self.assertIn("two distinct reviewer identities", " ".join(result["reasons"]))

    def test_r2_accepts_explicit_self_review_fallback_pair(self) -> None:
        bundle = _bundle(paths=("apps/web/src/app.ts",))
        primary = _record(
            bundle, reviewer="Reviewer A", axis="correctness", risk="R2"
        )
        primary.update(
            {"review_mode": "self-primary", "review_session": "pass-primary"}
        )
        fallback = _record(
            bundle, reviewer="Reviewer A", axis="testing", risk="R2"
        )
        fallback.update(
            {
                "review_mode": "self-fallback",
                "review_session": "pass-fallback",
                "fallback_reason": (
                    "External review was attempted but unavailable because the "
                    "configured provider quota was exhausted."
                ),
            }
        )
        result = _evaluate(
            bundle=bundle,
            risk_class="R2",
            comments=[
                _comment(primary, comment_id=1),
                _comment(fallback, comment_id=2),
            ],
        )
        self.assertTrue(result["pass"], result["reasons"])
        self.assertEqual(
            {review["review_mode"] for review in result["accepted_reviews"]},
            {"self-primary", "self-fallback"},
        )

    def test_self_review_fallback_requires_distinct_sessions_and_reason(self) -> None:
        bundle = _bundle(paths=("apps/web/src/app.ts",))
        primary = _record(
            bundle, reviewer="Reviewer A", axis="correctness", risk="R2"
        )
        primary.update(
            {"review_mode": "self-primary", "review_session": "same-pass"}
        )
        fallback = _record(
            bundle, reviewer="Reviewer A", axis="testing", risk="R2"
        )
        fallback.update(
            {"review_mode": "self-fallback", "review_session": "same-pass"}
        )
        result = _evaluate(
            bundle=bundle,
            risk_class="R2",
            comments=[
                _comment(primary, comment_id=1),
                _comment(fallback, comment_id=2),
            ],
        )
        self.assertFalse(result["pass"])
        self.assertGreaterEqual(result["malformed_evidence_count"], 1)

        fallback["fallback_reason"] = (
            "External reviewer unavailable after a documented provider quota failure."
        )
        result = _evaluate(
            bundle=bundle,
            risk_class="R2",
            comments=[
                _comment(primary, comment_id=1),
                _comment(fallback, comment_id=2),
            ],
        )
        self.assertFalse(result["pass"])
        self.assertIn("distinct review sessions", " ".join(result["reasons"]))

        fallback["review_session"] = "fallback-pass"
        result = _evaluate(
            bundle=bundle,
            risk_class="R2",
            comments=[
                _comment(primary, comment_id=1),
                _comment(fallback, comment_id=2),
            ],
        )
        self.assertTrue(result["pass"], result["reasons"])

    def test_self_review_optional_fields_fail_closed(self) -> None:
        bundle = _bundle(paths=("apps/web/src/app.ts",))
        external = _record(
            bundle, reviewer="Reviewer A", axis="correctness", risk="R2"
        )
        external["review_session"] = 7
        result = _evaluate(
            bundle=bundle,
            risk_class="R2",
            comments=[_comment(external)],
        )
        self.assertFalse(result["pass"])
        self.assertEqual(result["malformed_evidence_count"], 1)

        primary = _record(
            bundle, reviewer="Reviewer A", axis="correctness", risk="R2"
        )
        primary.update(
            {
                "review_mode": "self-primary",
                "review_session": " pass-primary",
            }
        )
        result = _evaluate(
            bundle=bundle,
            risk_class="R2",
            comments=[_comment(primary)],
        )
        self.assertFalse(result["pass"])
        self.assertEqual(result["malformed_evidence_count"], 1)

    def test_r3_requires_high_risk_axis(self) -> None:
        bundle = _bundle(paths=(".github/workflows/ci.yml",))
        low_risk_comments = [
            _comment(
                _record(bundle, reviewer="Reviewer A", axis="correctness", risk="R3")
            ),
            _comment(_record(bundle, reviewer="Reviewer B", axis="testing", risk="R3")),
        ]
        result = _evaluate(bundle=bundle, risk_class="R3", comments=low_risk_comments)
        self.assertFalse(result["pass"])
        self.assertIn("R3 requires", " ".join(result["reasons"]))

        high_risk_comments = low_risk_comments + [
            _comment(_record(bundle, reviewer="Reviewer C", axis="security", risk="R3"))
        ]
        result = _evaluate(bundle=bundle, risk_class="R3", comments=high_risk_comments)
        self.assertTrue(result["pass"], result["reasons"])

    def test_report_hash_must_match_substantive_comment_text(self) -> None:
        bundle = _bundle(paths=("config/example.json",))
        mismatched = _comment(
            _record(bundle, reviewer="Reviewer A", axis="correctness", risk="R1"),
            report_sha256="0" * 64,
        )
        result = _evaluate(bundle=bundle, risk_class="R1", comments=[mismatched])
        self.assertFalse(result["pass"])
        self.assertEqual(result["malformed_evidence_count"], 1)

        too_short = _comment(
            _record(bundle, reviewer="Reviewer A", axis="correctness", risk="R1"),
            report_text="zu kurz",
        )
        result = _evaluate(bundle=bundle, risk_class="R1", comments=[too_short])
        self.assertFalse(result["pass"])
        self.assertEqual(result["malformed_evidence_count"], 1)

    def test_r2_requires_distinct_review_report_hashes(self) -> None:
        bundle = _bundle(paths=("apps/web/src/app.ts",))
        shared_report = (
            "Derselbe ausführliche Bericht wird absichtlich zweimal verwendet. "
            "Er beschreibt Korrektheit, Tests, Regressionen und Fehlerszenarien, "
            "darf aber nicht als zwei unabhängige Prüfberichte gezählt werden."
        )
        comments = [
            _comment(
                _record(bundle, reviewer="Reviewer A", axis="correctness", risk="R2"),
                report_text=shared_report,
            ),
            _comment(
                _record(bundle, reviewer="Reviewer B", axis="testing", risk="R2"),
                report_text=shared_report,
            ),
        ]
        result = _evaluate(bundle=bundle, risk_class="R2", comments=comments)
        self.assertFalse(result["pass"])
        self.assertIn("two distinct review reports", " ".join(result["reasons"]))

    def test_safe_raster_asset_is_reviewable_with_r1_approval(self) -> None:
        base = _bundle(paths=("docs/images/map.png",), changed_lines=0)
        bundle = Bundle(
            pr_number=base.pr_number,
            base_sha=base.base_sha,
            head_sha=base.head_sha,
            merge_base_sha=base.merge_base_sha,
            diff_sha256=base.diff_sha256,
            patch_sha256=base.patch_sha256,
            manifest_path=base.manifest_path,
            diff_path=base.diff_path,
            patch_path=base.patch_path,
            request_path=base.request_path,
            stats=DiffStats(
                ("docs/images/map.png",),
                0,
                0,
                (),
                ("docs/images/map.png",),
            ),
        )
        result = _evaluate(
            bundle=bundle,
            risk_class="R1",
            comments=[],
            reviews=[_review(bundle)],
        )
        self.assertTrue(result["pass"], result["reasons"])
        self.assertEqual(result["reviewable_opaque_files"], ["docs/images/map.png"])
        self.assertEqual(result["blocking_opaque_files"], [])

    def test_unsafe_opaque_file_still_blocks(self) -> None:
        base = _bundle(paths=("docs/report.pdf",), changed_lines=0)
        bundle = Bundle(
            pr_number=base.pr_number,
            base_sha=base.base_sha,
            head_sha=base.head_sha,
            merge_base_sha=base.merge_base_sha,
            diff_sha256=base.diff_sha256,
            patch_sha256=base.patch_sha256,
            manifest_path=base.manifest_path,
            diff_path=base.diff_path,
            patch_path=base.patch_path,
            request_path=base.request_path,
            stats=DiffStats(("docs/report.pdf",), 0, 0, (), ("docs/report.pdf",)),
        )
        result = _evaluate(
            bundle=bundle,
            risk_class="R1",
            comments=[],
            reviews=[_review(bundle)],
        )
        self.assertFalse(result["pass"])
        self.assertEqual(result["blocking_opaque_files"], ["docs/report.pdf"])
        self.assertIn("opaque files outside", " ".join(result["reasons"]))

    def test_r1_accepts_native_current_head_approval(self) -> None:
        bundle = _bundle(paths=("config/example.json",))
        result = _evaluate(
            bundle=bundle,
            risk_class="R1",
            comments=[],
            reviews=[_review(bundle)],
        )
        self.assertTrue(result["pass"], result["reasons"])
        self.assertEqual(result["accepted_reviews"][0]["kind"], "github-approval")

    def test_r1_deduplicates_native_and_attested_review_by_identity(self) -> None:
        bundle = _bundle(paths=("config/example.json",))
        comment = _comment(
            _record(bundle, reviewer="Alex", axis="correctness", risk="R1")
        )
        result = _evaluate(
            bundle=bundle,
            risk_class="R1",
            comments=[comment],
            reviews=[_review(bundle, author="alex")],
        )
        self.assertTrue(result["pass"], result["reasons"])
        self.assertEqual(result["accepted_review_count"], 1)
        self.assertEqual(
            [review["kind"] for review in result["accepted_reviews"]],
            ["attested-report"],
        )

    def test_native_approval_must_match_current_head_and_does_not_replace_r2_reports(
        self,
    ) -> None:
        r1_bundle = _bundle(paths=("config/example.json",))
        stale = _evaluate(
            bundle=r1_bundle,
            risk_class="R1",
            comments=[],
            reviews=[_review(r1_bundle, commit_id="f" * 40)],
        )
        self.assertFalse(stale["pass"])
        self.assertEqual(stale["stale_native_review_count"], 1)

        r2_bundle = _bundle(paths=("apps/web/src/app.ts",))
        r2 = _evaluate(
            bundle=r2_bundle,
            risk_class="R2",
            comments=[],
            reviews=[_review(r2_bundle)],
        )
        self.assertFalse(r2["pass"])
        self.assertEqual(r2["accepted_review_count"], 0)

    def test_native_approval_requires_trusted_github_association(self) -> None:
        bundle = _bundle(paths=("config/example.json",))
        result = _evaluate(
            bundle=bundle,
            risk_class="R1",
            comments=[],
            reviews=[
                _review(
                    bundle,
                    author="outside-reviewer",
                    association="CONTRIBUTOR",
                )
            ],
        )
        self.assertFalse(result["pass"])
        self.assertEqual(result["unauthorized_native_review_count"], 1)

    def test_current_head_changes_requested_blocks_r1(self) -> None:
        bundle = _bundle(paths=("config/example.json",))
        result = _evaluate(
            bundle=bundle,
            risk_class="R1",
            comments=[],
            reviews=[_review(bundle, state="CHANGES_REQUESTED")],
        )
        self.assertFalse(result["pass"])
        self.assertIn("request changes", " ".join(result["reasons"]))

    def test_latest_native_review_state_wins_and_dismissal_invalidates(self) -> None:
        bundle = _bundle(paths=("config/example.json",))
        approved = _review(
            bundle,
            state="APPROVED",
            review_id=1,
            submitted_at="2026-07-14T12:00:00Z",
        )
        changes_requested = _review(
            bundle,
            state="CHANGES_REQUESTED",
            review_id=2,
            submitted_at="2026-07-14T13:00:00Z",
        )
        blocked = _evaluate(
            bundle=bundle,
            risk_class="R1",
            comments=[],
            reviews=[approved, changes_requested],
        )
        self.assertFalse(blocked["pass"])
        self.assertIn("request changes", " ".join(blocked["reasons"]))

        dismissed = _review(
            bundle,
            state="DISMISSED",
            review_id=3,
            submitted_at="2026-07-14T14:00:00Z",
        )
        invalidated = _evaluate(
            bundle=bundle,
            risk_class="R1",
            comments=[],
            reviews=[approved, dismissed],
        )
        self.assertFalse(invalidated["pass"])
        self.assertEqual(invalidated["accepted_review_count"], 0)

    def test_report_hash_normalizes_crlf_and_bare_cr(self) -> None:
        bundle = _bundle(paths=("config/example.json",))
        report = (
            "Ausführlicher Bericht zur Korrektheit.\r\n"
            "Geprüft wurden Regressionen, Fehlerszenarien und Invarianten.\r"
            "Es verbleiben keine Mergeblocker und alle Befunde sind aufgelöst."
        )
        result = _evaluate(
            bundle=bundle,
            risk_class="R1",
            comments=[
                _comment(
                    _record(
                        bundle, reviewer="Reviewer A", axis="correctness", risk="R1"
                    ),
                    report_text=report,
                )
            ],
        )
        self.assertTrue(result["pass"], result["reasons"])

    def test_malformed_and_multiple_evidence_blocks_are_diagnostic(self) -> None:
        bundle = _bundle(paths=("config/example.json",))
        malformed = {
            "id": 1,
            "body": "Bericht<!-- weltgewebe-review-evidence {kaputt} -->",
            "author_association": "OWNER",
            "user": {"login": "alex"},
        }
        result = _evaluate(bundle=bundle, risk_class="R1", comments=[malformed])
        self.assertFalse(result["pass"])
        self.assertEqual(result["malformed_evidence_count"], 1)

        first = _comment(
            _record(bundle, reviewer="Reviewer A", axis="correctness", risk="R1")
        )
        first["body"] += first["body"][
            first["body"].index("<!-- weltgewebe-review-evidence") :
        ]
        result = _evaluate(bundle=bundle, risk_class="R1", comments=[first])
        self.assertFalse(result["pass"])
        self.assertGreaterEqual(result["malformed_evidence_count"], 2)

    def test_evidence_json_may_contain_html_comment_terminator_text(self) -> None:
        bundle = _bundle(paths=("config/example.json",))
        result = _evaluate(
            bundle=bundle,
            risk_class="R1",
            comments=[
                _comment(
                    _record(
                        bundle,
                        reviewer="Reviewer --> A",
                        axis="correctness",
                        risk="R1",
                    )
                )
            ],
        )
        self.assertTrue(result["pass"], result["reasons"])

    def test_alternate_html_comment_terminator_is_accepted(self) -> None:
        bundle = _bundle(paths=("config/example.json",))
        comment = _comment(
            _record(bundle, reviewer="Reviewer A", axis="correctness", risk="R1")
        )
        comment["body"] = comment["body"].replace("\n-->", "\n--!>")
        result = _evaluate(
            bundle=bundle,
            risk_class="R1",
            comments=[comment],
        )
        self.assertTrue(result["pass"], result["reasons"])

    def test_oversized_evidence_payload_is_reported_separately(self) -> None:
        bundle = _bundle(paths=("config/example.json",))
        oversized = _record(
            bundle,
            reviewer="R" * (21 * 1024),
            axis="correctness",
            risk="R1",
        )
        result = _evaluate(
            bundle=bundle,
            risk_class="R1",
            comments=[_comment(oversized)],
        )
        self.assertFalse(result["pass"])
        self.assertEqual(result["malformed_evidence_count"], 1)
        self.assertEqual(result["oversized_evidence_count"], 1)

    def test_unicode_equivalent_reviewer_names_are_not_distinct(self) -> None:
        bundle = _bundle(paths=("apps/web/src/app.ts",))
        composed = "Revér A"
        decomposed = "Reve\u0301r A"
        comments = [
            _comment(
                _record(bundle, reviewer=composed, axis="correctness", risk="R2"),
                comment_id=1,
            ),
            _comment(
                _record(bundle, reviewer=decomposed, axis="testing", risk="R2"),
                comment_id=2,
            ),
        ]
        result = _evaluate(bundle=bundle, risk_class="R2", comments=comments)
        self.assertFalse(result["pass"])
        self.assertIn("two distinct reviewer identities", " ".join(result["reasons"]))

    def test_same_timestamp_uses_higher_comment_id_deterministically(self) -> None:
        bundle = _bundle(paths=("config/example.json",))
        passed = _comment(
            _record(bundle, reviewer="Reviewer A", axis="correctness", risk="R1"),
            comment_id=10,
        )
        blocked = _comment(
            _record(
                bundle,
                reviewer="Reviewer A",
                axis="correctness",
                risk="R1",
                verdict="BLOCKED",
            ),
            comment_id=11,
        )
        result = _evaluate(bundle=bundle, risk_class="R1", comments=[blocked, passed])
        self.assertFalse(result["pass"])
        self.assertIn("blocking verdicts", " ".join(result["reasons"]))

    def test_findings_resolved_must_be_true_for_pass(self) -> None:
        bundle = _bundle(paths=("config/example.json",))
        record = _record(bundle, reviewer="Reviewer A", axis="correctness", risk="R1")
        record["findings_resolved"] = False
        result = _evaluate(bundle=bundle, risk_class="R1", comments=[_comment(record)])
        self.assertFalse(result["pass"])
        self.assertEqual(result["accepted_review_count"], 0)

    def test_evidence_schema_and_reviewer_are_strict(self) -> None:
        bundle = _bundle(paths=("config/example.json",))
        extra = _record(bundle, reviewer="Reviewer A", axis="correctness", risk="R1")
        extra["unexpected"] = "not allowed"
        result = _evaluate(bundle=bundle, risk_class="R1", comments=[_comment(extra)])
        self.assertFalse(result["pass"])
        self.assertEqual(result["malformed_evidence_count"], 1)

        boolean_schema = _record(
            bundle, reviewer="Reviewer A", axis="correctness", risk="R1"
        )
        boolean_schema["schema_version"] = True
        result = _evaluate(
            bundle=bundle, risk_class="R1", comments=[_comment(boolean_schema)]
        )
        self.assertFalse(result["pass"])
        self.assertEqual(result["malformed_evidence_count"], 1)

        control_name = _record(
            bundle, reviewer="Reviewer\nA", axis="correctness", risk="R1"
        )
        result = _evaluate(
            bundle=bundle, risk_class="R1", comments=[_comment(control_name)]
        )
        self.assertFalse(result["pass"])
        self.assertEqual(result["malformed_evidence_count"], 1)

    def test_latest_blocking_verdict_overrides_prior_pass(self) -> None:
        bundle = _bundle(paths=("config/example.json",))
        passed = _comment(
            _record(bundle, reviewer="Reviewer A", axis="correctness", risk="R1")
        )
        blocked = _comment(
            _record(
                bundle,
                reviewer="Reviewer A",
                axis="correctness",
                risk="R1",
                verdict="BLOCKED",
            )
        )
        blocked["updated_at"] = "2026-07-14T13:00:00Z"
        result = _evaluate(bundle=bundle, risk_class="R1", comments=[passed, blocked])
        self.assertFalse(result["pass"])
        self.assertIn("blocking verdicts", " ".join(result["reasons"]))

    def test_r0_fails_closed_for_non_markdown_or_large_change(self) -> None:
        code_bundle = _bundle(paths=("script.py",), changed_lines=2)
        result = _evaluate(bundle=code_bundle, risk_class="R0", comments=[])
        self.assertFalse(result["pass"])

        large_bundle = _bundle(paths=("docs/example.md",), changed_lines=51)
        result = _evaluate(bundle=large_bundle, risk_class="R0", comments=[])
        self.assertFalse(result["pass"])

    def test_unauthorized_comment_does_not_count(self) -> None:
        bundle = _bundle(paths=("config/example.json",))
        comment = _comment(
            _record(bundle, reviewer="Reviewer A", axis="correctness", risk="R1"),
            association="NONE",
        )
        result = _evaluate(bundle=bundle, risk_class="R1", comments=[comment])
        self.assertFalse(result["pass"])
        self.assertEqual(result["unauthorized_evidence_count"], 1)

    def test_unlisted_attester_does_not_count_even_with_owner_role(self) -> None:
        bundle = _bundle(paths=("config/example.json",))
        comment = _comment(
            _record(bundle, reviewer="Reviewer A", axis="correctness", risk="R1"),
            association="OWNER",
            author="mallory",
        )
        result = _evaluate(bundle=bundle, risk_class="R1", comments=[comment])
        self.assertFalse(result["pass"])
        self.assertEqual(result["unauthorized_evidence_count"], 1)


class WorkflowContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[3]
        self.workflow = (
            self.repo_root / ".github/workflows/review-evidence.yml"
        ).read_text(encoding="utf-8")
        self.workflow_data = yaml.safe_load(self.workflow)
        self.review_job = self.workflow_data["jobs"]["review-evidence"]

    def test_privileged_workflow_executes_only_literal_main_code(self) -> None:
        self.assertIn("pull_request_target:", self.workflow)
        self.assertIn(
            "types: [opened, synchronize, reopened, edited, ready_for_review]",
            self.workflow,
        )
        self.assertIn("attention_changed_files", self.workflow)
        self.assertIn("previous_filename", self.workflow)
        self.assertIn("issue_comment:", self.workflow)
        self.assertIn("pull_request_review:", self.workflow)
        self.assertIn("ref: main", self.workflow)
        self.assertIn("fetch-depth: 1", self.workflow)
        self.assertIn("persist-credentials: false", self.workflow)
        self.assertNotIn("github.event.pull_request.head.sha", self.workflow)
        self.assertNotIn("github.event.pull_request.head.ref", self.workflow)
        self.assertIn(
            "github.event.pull_request.head.repo.full_name == github.repository",
            self.workflow,
        )
        self.assertIn(
            "github.event.pull_request.user.login != 'dependabot[bot]'",
            self.workflow,
        )
        self.assertIn("github.event.comment.author_association", self.workflow)
        self.assertIn("types: [created, edited, deleted]", self.workflow)
        self.assertIn(
            "contains(github.event.comment.body, '<!-- weltgewebe-review-evidence')",
            self.workflow,
        )
        self.assertIn(
            "startsWith(github.event.comment.body, '/review-evidence recheck')",
            self.workflow,
        )
        self.assertNotIn("actions/checkout@v", self.workflow)
        self.assertNotIn("git fetch", self.workflow)
        self.assertNotIn("refs/pull/", self.workflow)
        self.assertNotIn("git checkout", self.workflow)
        self.assertIn("Accept: application/vnd.github.diff", self.workflow)
        self.assertIn("Accept: application/vnd.github.patch", self.workflow)
        self.assertIn("pr-before.json", self.workflow)
        self.assertIn("pr-after.json", self.workflow)
        self.assertIn("diff_file_count", self.workflow)
        self.assertIn("max_artifact_bytes", self.workflow)
        self.assertIn("/reviews?per_page=100", self.workflow)
        self.assertIn("--reviews-file", self.workflow)
        self.assertIn("Refresh current-head evidence before publication", self.workflow)
        self.assertNotIn("continue-on-error: true", self.workflow)
        self.assertIn(
            "scripts/quality/review_governance.py evaluate-materialized",
            self.workflow,
        )
        self.assertIn(
            "--authorities-file .github/review-evidence-authorities.json",
            self.workflow,
        )

    def test_all_actions_are_pinned_to_full_commit_sha(self) -> None:
        action_lines = [
            line.strip()
            for line in self.workflow.splitlines()
            if line.strip().startswith("uses:")
        ]
        self.assertGreaterEqual(len(action_lines), 2)
        for line in action_lines:
            match = re.fullmatch(r"uses: [^@\s]+@([0-9a-f]{40})(?:\s+#.*)?", line)
            self.assertIsNotNone(match, line)

    def test_status_context_stays_out_of_grabowski_required_check_catalog(self) -> None:
        required = json.loads(
            (self.repo_root / ".github/grabowski-required-checks.json").read_text(
                encoding="utf-8"
            )
        )
        required_context = self.workflow_data["env"]["REVIEW_EVIDENCE_CONTEXT"]
        self.assertNotIn(required_context, required["required_checks"])
        self.assertEqual(required["required_checks"], ["Required merge gate"])
        self.assertEqual(required_context, "Review evidence gate")
        self.assertEqual(self.review_job["name"], "Review evidence evaluator")
        self.assertNotEqual(self.review_job["name"], required_context)
        self.assertNotIn("concurrency", self.workflow_data)
        concurrency = self.review_job["concurrency"]
        self.assertEqual(
            concurrency["group"],
            "review-evidence-${{ github.event.pull_request.number || github.event.issue.number || inputs.pr_number }}",
        )
        self.assertIs(concurrency["cancel-in-progress"], True)
        eligibility = self.review_job["if"]
        self.assertIn("github.event.action == 'edited'", eligibility)
        self.assertIn("github.event_name != 'pull_request_target'", eligibility)
        self.assertIn("github.event_name != 'pull_request_review'", eligibility)

        evaluate_step = next(
            step
            for step in self.review_job["steps"]
            if step.get("name") == "Resolve PR and evaluate exact review evidence"
        )
        publish_step = next(
            step
            for step in self.review_job["steps"]
            if step.get("name") == "Publish exact-head gate status"
        )
        self.assertIn('statuses/${head_sha}', evaluate_step["run"])
        self.assertIn('-f state=pending', evaluate_step["run"])
        self.assertIn('-f context="${REVIEW_EVIDENCE_CONTEXT}"', evaluate_step["run"])
        self.assertEqual(
            publish_step["env"]["HEAD_SHA"],
            "${{ steps.evaluate.outputs.head_sha }}",
        )
        self.assertIn('statuses/${HEAD_SHA}', publish_step["run"])
        self.assertIn('-f state="${state}"', publish_step["run"])
        self.assertIn("state=success", publish_step["run"])
        self.assertIn("state=failure", publish_step["run"])
        self.assertIn('-f context="${REVIEW_EVIDENCE_CONTEXT}"', publish_step["run"])
        self.assertNotIn("context='Review evidence evaluator'", self.workflow)
        self.assertNotIn('context="Review evidence evaluator"', self.workflow)

    def test_cancellable_review_job_name_is_distinct_from_required_status_context(
        self,
    ) -> None:
        # GitHub requires both a check run and a commit status when both share a
        # configured required-check name. Keep the cancellable evaluator job name
        # distinct from the explicit required commit-status context.
        required_context = self.workflow_data["env"]["REVIEW_EVIDENCE_CONTEXT"]
        self.assertEqual(self.review_job["name"], "Review evidence evaluator")
        self.assertEqual(required_context, "Review evidence gate")
        self.assertNotEqual(self.review_job["name"], required_context)

    def test_pr_template_contains_single_risk_marker(self) -> None:
        template = (self.repo_root / ".github/PULL_REQUEST_TEMPLATE.md").read_text(
            encoding="utf-8"
        )
        self.assertEqual(template.count("<!-- weltgewebe-risk: R? -->"), 1)
        self.assertIn("R3 = auth, privacy, security", template)


class ForwardedExternalReviewTests(unittest.TestCase):
    def test_owner_forwarded_external_reviews_count_for_r2(self) -> None:
        bundle = _bundle(paths=("apps/web/src/app.ts",), changed_lines=20)
        comments = [
            _forwarded_comment(bundle, reviewer="External LLM A", axis="correctness", comment_id=11),
            _forwarded_comment(bundle, reviewer="External LLM B", axis="testing", comment_id=12),
        ]
        result = _evaluate(bundle=bundle, risk_class="R2", comments=comments)
        self.assertTrue(result["pass"], result["reasons"])
        self.assertEqual(result["accepted_review_count"], 2)
        self.assertEqual(
            {item["kind"] for item in result["accepted_reviews"]},
            {"forwarded-external-review"},
        )


    def test_forwarded_review_is_stale_after_base_or_diff_change(self) -> None:
        bundle = _bundle(paths=("apps/web/src/app.ts",), changed_lines=20)
        comment = _forwarded_comment(bundle, reviewer="External LLM A", axis="correctness")
        changed_base = Bundle(**{**bundle.__dict__, "base_sha": "e" * 40})
        changed_diff = Bundle(**{**bundle.__dict__, "diff_sha256": "f" * 64})
        self.assertEqual(
            _evaluate(bundle=changed_base, risk_class="R2", comments=[comment])["stale_evidence_count"],
            1,
        )
        self.assertEqual(
            _evaluate(bundle=changed_diff, risk_class="R2", comments=[comment])["stale_evidence_count"],
            1,
        )

    def test_forwarded_review_rejects_internal_prefixed_payload_keys(self) -> None:
        bundle = _bundle(paths=("apps/web/src/app.ts",), changed_lines=20)
        comment = _forwarded_comment(bundle, reviewer="External LLM A", axis="correctness")
        comment["body"] = comment["body"].replace(
            '"findings_resolved": true',
            '"findings_resolved": true, "_forwarded": true',
        )
        result = _evaluate(bundle=bundle, risk_class="R2", comments=[comment])
        self.assertFalse(result["pass"])
        self.assertEqual(result["malformed_evidence_count"], 1)

    def test_mixed_native_and_forwarded_blocks_in_one_comment_are_rejected(self) -> None:
        bundle = _bundle(paths=("apps/web/src/app.ts",), changed_lines=20)
        native = _comment(_record(bundle, reviewer="Reviewer A", axis="correctness", risk="R2"))
        forwarded = _forwarded_comment(bundle, reviewer="Reviewer B", axis="testing")
        native["body"] += "\n" + forwarded["body"]
        result = _evaluate(bundle=bundle, risk_class="R2", comments=[native])
        self.assertFalse(result["pass"])
        self.assertGreaterEqual(result["malformed_evidence_count"], 2)

    def test_forwarded_review_rejects_boolean_schema_version(self) -> None:
        bundle = _bundle(paths=("apps/web/src/app.ts",), changed_lines=20)
        comment = _forwarded_comment(bundle, reviewer="External LLM A", axis="correctness")
        comment["body"] = comment["body"].replace(
            '"schema_version": 1',
            '"schema_version": true',
        )
        result = _evaluate(bundle=bundle, risk_class="R2", comments=[comment])
        self.assertFalse(result["pass"])
        self.assertEqual(result["malformed_evidence_count"], 1)

    def test_forwarded_blocking_verdict_does_not_count(self) -> None:
        bundle = _bundle(paths=("apps/web/src/app.ts",), changed_lines=20)
        comment = _forwarded_comment(bundle, reviewer="External LLM A", axis="correctness")
        comment["body"] = comment["body"].replace(
            '"verdict": "PASS"',
            '"verdict": "BLOCKED"',
        ).replace(
            '"findings_resolved": true',
            '"findings_resolved": false',
        )
        result = _evaluate(bundle=bundle, risk_class="R2", comments=[comment])
        self.assertFalse(result["pass"])
        self.assertEqual(result["accepted_review_count"], 0)
        self.assertTrue(any("blocking verdicts" in reason for reason in result["reasons"]))

    def test_forwarded_review_is_stale_after_head_change(self) -> None:
        bundle = _bundle(paths=("apps/web/src/app.ts",), changed_lines=20)
        comment = _forwarded_comment(bundle, reviewer="External LLM A", axis="correctness")
        changed = Bundle(**{**bundle.__dict__, "head_sha": "f" * 40})
        result = _evaluate(bundle=changed, risk_class="R2", comments=[comment])
        self.assertFalse(result["pass"])
        self.assertEqual(result["stale_evidence_count"], 1)

if __name__ == "__main__":
    unittest.main()
