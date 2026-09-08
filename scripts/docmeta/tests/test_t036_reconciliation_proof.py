from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import subprocess
import unittest


PROOF = Path("docs/proofs/weltgewebe-os-v1-t036-documentation-drift-reconciliation.md")
SOURCE = Path("docs/proofs/sources/weltgewebe-os-v1-t036-documentation-drift-audit.json")
SOURCE_SHA256 = "56e4a159d8b3dc79d6275ac46248f845337dbbe124b9f26f695a6a9ccfff8c0b"
BASE_REVISION = "e34a27a160a37b86e06ba906e320ff24e871db0d"
AUDIT_REVISION = "39d5a8f5fa637ba9f8a487074c86856e6a6b897c"
LIFECYCLE = Path("docs/_generated/report-lifecycle.md")
DOCS_GUARD = Path(".github/workflows/docs-guard.yml")
ROW_RE = re.compile(
    r"^\| DRIFT-(\d{3}) \| (P[12]) \| "
    r"(weiterhin gültig|bereits behoben|superseded|falsch-positiv) \|"
)
EXPECTED = {
    "weiterhin gültig": 16,
    "bereits behoben": 5,
    "superseded": 8,
    "falsch-positiv": 23,
}
ARCHIVED_BY_T036 = (
    "auth-persistence-runtime-target-reconciliation.md",
    "domain-account-email-uniqueness-audit.md",
    "domain-account-write-path-proof.md",
    "domain-backfill-proof.md",
    "domain-edge-reference-audit.md",
    "domain-node-write-path-proof.md",
    "domain-read-path-proof.md",
)


class T036ReconciliationProofTests(unittest.TestCase):
    def test_docs_guard_triggers_for_every_markdown_pull_request(self):
        workflow = DOCS_GUARD.read_text(encoding="utf-8")
        self.assertEqual(
            sum(line.strip() == "- '**/*.md'" for line in workflow.splitlines()),
            1,
        )

    def test_all_52_findings_are_uniquely_dispositioned_and_matrix_is_derived(self):
        text = PROOF.read_text(encoding="utf-8")
        rows = [match.groups() for line in text.splitlines() if (match := ROW_RE.match(line))]

        ids = [int(item_id) for item_id, _, _ in rows]
        self.assertEqual(ids, list(range(1, 53)))
        self.assertEqual(len(set(ids)), 52)

        counts = Counter(disposition for _, _, disposition in rows)
        self.assertEqual(dict(counts), EXPECTED)
        self.assertEqual(sum(counts.values()), 52)

        matrix_row = "| 52 | 16 | 5 | 8 | 23 |"
        self.assertEqual(text.count(matrix_row), 1)

    def test_historical_source_is_byte_bound_and_matches_proof_ids_and_severities(self):
        source_bytes = SOURCE.read_bytes()
        self.assertEqual(hashlib.sha256(source_bytes).hexdigest(), SOURCE_SHA256)
        source = json.loads(source_bytes)
        self.assertEqual(source["current_head"], AUDIT_REVISION)

        source_rows = [(item["id"], item["severity"]) for item in source["findings"]]
        self.assertEqual(len(source_rows), 52)
        self.assertEqual(
            [item_id for item_id, _ in source_rows],
            [f"DRIFT-{index:03d}" for index in range(1, 53)],
        )

        proof_text = PROOF.read_text(encoding="utf-8")
        proof_rows = [
            (f"DRIFT-{item_id}", severity)
            for line in proof_text.splitlines()
            if (match := ROW_RE.match(line))
            for item_id, severity, _ in [match.groups()]
        ]
        self.assertEqual(proof_rows, source_rows)
        self.assertIn(str(SOURCE), proof_text)
        self.assertIn(SOURCE_SHA256, proof_text)

    def test_claimed_source_revisions_resolve_to_local_commit_objects(self):
        proof_text = PROOF.read_text(encoding="utf-8")
        self.assertIn(BASE_REVISION, proof_text)
        for revision in (BASE_REVISION, AUDIT_REVISION):
            with self.subTest(revision=revision):
                subprocess.run(
                    ["git", "cat-file", "-e", f"{revision}^{{commit}}"],
                    check=True,
                    capture_output=True,
                )

    def test_generated_lifecycle_projection_contains_t036_archivals(self):
        text = LIFECYCLE.read_text(encoding="utf-8")
        active, archived_and_after = text.split("## Archived Reports", 1)
        for filename in ARCHIVED_BY_T036:
            self.assertNotIn(filename, active.split("## Active Reports", 1)[1])
            self.assertIn(filename, archived_and_after)
        # T036 owns the exact archival identities above, not the global number
        # of active/archived reports. Unrelated later reports must not invalidate
        # this historical reconciliation proof merely by changing those totals.
        self.assertIn("| findings_total | 0 |", text)


if __name__ == "__main__":
    unittest.main()
