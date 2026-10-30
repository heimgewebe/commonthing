import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

SCRIPT_PATH = [sys.executable, "-m", "scripts.docmeta.audit_domain_edge_references"]

def run_script(*args, env=None):
    env_vars = os.environ.copy()
    if env is not None:
        env_vars.update(env)

    # Remove DATABASE_URL unless explicitly provided
    if env is None or "DATABASE_URL" not in env:
        env_vars.pop("DATABASE_URL", None)

    return subprocess.run(
        SCRIPT_PATH + list(args),
        capture_output=True,
        text=True,
        env=env_vars,
        cwd=REPO_ROOT
    )

class TestAuditDomainEdgeReferences(unittest.TestCase):

    def test_no_input_fails(self):
        result = run_script()
        self.assertNotEqual(result.returncode, 0)

    def test_missing_nodes_file_fails(self):
        result = run_script("--nodes-jsonl", "does_not_exist.jsonl", "--edges-jsonl", "also_does_not_exist.jsonl")
        self.assertNotEqual(result.returncode, 0)

    def test_missing_edges_file_fails(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as nodes_file:
            result = run_script("--nodes-jsonl", nodes_file.name, "--edges-jsonl", "does_not_exist.jsonl")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Cannot read edges file", result.stderr)
            self.assertNotIn("Traceback", result.stderr)

    def test_all_typed_node_references_valid(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as nf, tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as ef:
            nf.write('{"id": "node-a"}\n{"id": "node-b"}\n')
            nf.flush()
            ef.write('{"id": "edge-1", "source_id": "node-a", "source_type": "node", "target_id": "node-b", "target_type": "node"}\n')
            ef.flush()

            result = run_script("--nodes-jsonl", nf.name, "--edges-jsonl", ef.name, "--source-kind", "runtime", "--format", "json")
            self.assertEqual(result.returncode, 0)
            data = json.loads(result.stdout)

            self.assertEqual(data["summary"]["typed_node_references"], 2)
            self.assertIs(data["policy_signals"]["strict_node_fk_ready"], True)
            self.assertIs(data["policy_signals"]["requires_runtime_data_run"], False)
            self.assertIs(data["policy_signals"]["runtime_reference_data_observed"], True)
            self.assertEqual(data["policy_signals"]["fk_compatible_reference_sides"], 2)
            self.assertIs(data["policy_signals"]["type_hint_backfill_recommended"], False)

    def test_all_typed_node_references_valid_but_repo_fixture(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as nf, tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as ef:
            nf.write('{"id": "node-a"}\n{"id": "node-b"}\n')
            nf.flush()
            ef.write('{"id": "edge-1", "source_id": "node-a", "source_type": "node", "target_id": "node-b", "target_type": "node"}\n')
            ef.flush()

            result = run_script("--nodes-jsonl", nf.name, "--edges-jsonl", ef.name, "--source-kind", "repo-fixture", "--format", "json")
            self.assertEqual(result.returncode, 0)
            data = json.loads(result.stdout)

            self.assertEqual(data["summary"]["typed_node_references"], 2)
            self.assertIs(data["policy_signals"]["strict_node_fk_ready"], False)
            self.assertIs(data["policy_signals"]["requires_runtime_data_run"], True)

    def test_typed_node_missing_source_reference(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as nf, tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as ef:
            nf.write('{"id": "node-b"}\n')
            nf.flush()
            ef.write('{"id": "edge-1", "source_id": "missing-a", "source_type": "node", "target_id": "node-b", "target_type": "node"}\n')
            ef.flush()

            result = run_script("--nodes-jsonl", nf.name, "--edges-jsonl", ef.name, "--source-kind", "runtime", "--format", "json")
            self.assertEqual(result.returncode, 0)
            data = json.loads(result.stdout)

            self.assertEqual(data["summary"]["typed_node_missing_references"], 1)
            self.assertIs(data["policy_signals"]["requires_cleanup"], True)
            self.assertIs(data["policy_signals"]["requires_policy_decision"], True)
            self.assertIs(data["policy_signals"]["strict_node_fk_ready"], False)

    def test_typed_node_missing_target_reference(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as nf, tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as ef:
            nf.write('{"id": "node-a"}\n')
            nf.flush()
            ef.write('{"id": "edge-1", "source_id": "node-a", "source_type": "node", "target_id": "missing-b", "target_type": "node"}\n')
            ef.flush()

            result = run_script("--nodes-jsonl", nf.name, "--edges-jsonl", ef.name, "--source-kind", "runtime", "--format", "json")
            self.assertEqual(result.returncode, 0)
            data = json.loads(result.stdout)

            finding = next(f for f in data["findings"] if f["side"] == "target")
            self.assertEqual(finding["classification"], "typed_node_missing_reference")

    def test_both_missing_references(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as nf, tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as ef:
            nf.write('{"id": "node-something"}\n')
            nf.flush()
            ef.write('{"id": "edge-1", "source_id": "missing-a", "source_type": "node", "target_id": "missing-b", "target_type": "node"}\n')
            ef.flush()

            result = run_script("--nodes-jsonl", nf.name, "--edges-jsonl", ef.name, "--format", "json", "--source-kind", "runtime")
            self.assertEqual(result.returncode, 0)
            data = json.loads(result.stdout)

            self.assertEqual(data["summary"]["edges_with_both_missing_node_references"], 1)

    def test_typed_non_node_reference(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as nf, tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as ef:
            nf.write('{"id": "node-a"}\n')
            nf.flush()
            ef.write('{"id": "edge-1", "source_id": "node-a", "source_type": "node", "target_id": "acc-b", "target_type": "account"}\n')
            ef.flush()

            result = run_script("--nodes-jsonl", nf.name, "--edges-jsonl", ef.name, "--format", "json", "--source-kind", "runtime")
            self.assertEqual(result.returncode, 0)
            data = json.loads(result.stdout)

            self.assertEqual(data["summary"]["typed_non_node_references"], 1)
            self.assertIs(data["policy_signals"]["strict_node_fk_ready"], False)
            self.assertIs(data["policy_signals"]["loose_reference_semantics_observed"], True)
            self.assertIs(data["policy_signals"]["requires_policy_decision"], True)

    def test_typed_unknown_reference(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as nf, tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as ef:
            nf.write('{"id": "node-a"}\n')
            nf.flush()
            ef.write('{"id": "edge-1", "source_id": "ext-1", "source_type": "external", "target_id": "node-a", "target_type": "node"}\n')
            ef.flush()

            result = run_script("--nodes-jsonl", nf.name, "--edges-jsonl", ef.name, "--format", "json", "--source-kind", "runtime")
            self.assertEqual(result.returncode, 0)
            data = json.loads(result.stdout)

            self.assertEqual(data["summary"]["typed_unknown_references"], 1)
            self.assertIs(data["policy_signals"]["requires_policy_decision"], True)

    def test_untyped_existing_node_references_do_not_block_node_fk(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as nf, tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as ef:
            nf.write('{"id": "node-a"}\n{"id": "node-b"}\n')
            nf.flush()
            ef.write('{"id": "edge-1", "source_id": "node-a", "target_id": "node-b"}\n')
            ef.flush()

            result = run_script("--nodes-jsonl", nf.name, "--edges-jsonl", ef.name, "--source-kind", "runtime", "--format", "json")
            self.assertEqual(result.returncode, 0)
            data = json.loads(result.stdout)

            self.assertEqual(data["summary"]["untyped_existing_node_references"], 2)
            self.assertIs(data["policy_signals"]["strict_node_fk_ready"], True)
            self.assertIs(data["policy_signals"]["requires_cleanup"], False)
            self.assertIs(data["policy_signals"]["requires_policy_decision"], False)
            self.assertIs(data["policy_signals"]["type_hint_backfill_recommended"], True)
            self.assertEqual(data["findings"], [])
            self.assertEqual(data["policy_signals"]["fk_compatible_reference_sides"], 2)

    def test_untyped_missing_reference(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as nf, tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as ef:
            nf.write('{"id": "node-a"}\n')
            nf.flush()
            ef.write('{"id": "edge-1", "source_id": "node-a", "target_id": "missing-b"}\n')
            ef.flush()

            result = run_script("--nodes-jsonl", nf.name, "--edges-jsonl", ef.name, "--format", "json")
            self.assertEqual(result.returncode, 0)
            data = json.loads(result.stdout)

            self.assertEqual(data["summary"]["untyped_missing_references"], 1)
            self.assertIs(data["policy_signals"]["requires_cleanup"], True)
            self.assertIs(data["policy_signals"]["requires_policy_decision"], True)
            self.assertIs(data["policy_signals"]["strict_node_fk_ready"], False)

    def test_malformed_edge_missing_source_id(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as nf, tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as ef:
            nf.write('{"id": "node-a"}\n')
            nf.flush()
            ef.write('{"id": "edge-1", "target_id": "node-b"}\n')
            ef.flush()
            result = run_script("--nodes-jsonl", nf.name, "--edges-jsonl", ef.name, "--format", "json")
            self.assertEqual(result.returncode, 0)
            data = json.loads(result.stdout)
            self.assertEqual(data["summary"]["malformed_edges"], 1)

    def test_non_string_edge_id_is_malformed(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as nf, tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as ef:
            nf.write('{"id": "node-a"}\n')
            nf.flush()
            ef.write('{"id": 123, "source_id": "node-a", "target_id": "node-b"}\n')
            ef.flush()
            result = run_script("--nodes-jsonl", nf.name, "--edges-jsonl", ef.name, "--format", "json")
            self.assertEqual(result.returncode, 0)
            data = json.loads(result.stdout)
            self.assertEqual(data["summary"]["malformed_edges"], 1)

    def test_jsonl_whitespace_lines_are_ignored(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as nf, tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as ef:
            nf.write(' \n\t\n{"id": "node-a"}\n  \n')
            nf.flush()
            ef.write('  \n{"id": "edge-1", "source_id": "node-a", "target_id": "node-a"}\n \t \n')
            ef.flush()
            result = run_script("--nodes-jsonl", nf.name, "--edges-jsonl", ef.name, "--format", "json", "--source-kind", "runtime")
            self.assertEqual(result.returncode, 0)
            data = json.loads(result.stdout)

            self.assertEqual(data["summary"]["node_records_total"], 1)
            self.assertEqual(data["summary"]["edge_records_total"], 1)
            self.assertEqual(data["summary"]["node_invalid_json_records"], 0)
            self.assertEqual(data["summary"]["invalid_json_records"], 0)

    def test_node_invalid_json_is_reported(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as nf, tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as ef:
            nf.write('{invalid_json_here}\n')
            nf.flush()
            ef.write('{"id": "edge-1", "source_id": "node-a", "target_id": "node-b"}\n')
            ef.flush()
            result = run_script("--nodes-jsonl", nf.name, "--edges-jsonl", ef.name, "--format", "json")
            self.assertEqual(result.returncode, 0)
            data = json.loads(result.stdout)
            self.assertEqual(data["summary"]["node_invalid_json_records"], 1)

    def test_node_non_object_json_is_reported(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as nf, tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as ef:
            nf.write('["an", "array"]\n')
            nf.flush()
            ef.write('{"id": "edge-1", "source_id": "node-a", "target_id": "node-b"}\n')
            ef.flush()
            result = run_script("--nodes-jsonl", nf.name, "--edges-jsonl", ef.name, "--format", "json")
            self.assertEqual(result.returncode, 0)
            data = json.loads(result.stdout)
            self.assertEqual(data["summary"]["node_non_object_json_records"], 1)

    def test_node_missing_id_is_reported(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as nf, tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as ef:
            nf.write('{"name": "something"}\n')
            nf.flush()
            ef.write('{"id": "edge-1", "source_id": "node-a", "target_id": "node-b"}\n')
            ef.flush()
            result = run_script("--nodes-jsonl", nf.name, "--edges-jsonl", ef.name, "--format", "json")
            self.assertEqual(result.returncode, 0)
            data = json.loads(result.stdout)
            self.assertEqual(data["summary"]["nodes_missing_id"], 1)

    def test_node_non_string_id_is_reported(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as nf, tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as ef:
            nf.write('{"id": 123}\n')
            nf.flush()
            ef.write('{"id": "edge-1", "source_id": "node-a", "target_id": "node-b"}\n')
            ef.flush()
            result = run_script("--nodes-jsonl", nf.name, "--edges-jsonl", ef.name, "--format", "json")
            self.assertEqual(result.returncode, 0)
            data = json.loads(result.stdout)
            self.assertEqual(data["summary"]["nodes_non_string_id"], 1)

    def test_edge_sides_total_counts_only_auditable_edges(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as nf, tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as ef:
            nf.write('{"id": "node-a"}\n')
            nf.flush()
            # 1 valid edge
            ef.write('{"id": "edge-1", "source_id": "node-a", "target_id": "node-a"}\n')
            # 1 invalid JSON
            ef.write('{invalid}\n')
            # 1 non-object
            ef.write('[]\n')
            # 1 malformed edge
            ef.write('{"target_id": "node-a"}\n')
            ef.flush()

            result = run_script("--nodes-jsonl", nf.name, "--edges-jsonl", ef.name, "--format", "json")
            self.assertEqual(result.returncode, 0)
            data = json.loads(result.stdout)

            self.assertEqual(data["summary"]["edge_records_total"], 4)
            self.assertEqual(data["summary"]["auditable_edges_total"], 1)
            self.assertEqual(data["summary"]["edge_sides_total"], 2)
            self.assertEqual(data["summary"]["edges_total"], 4)
            self.assertEqual(data["summary"]["invalid_json_records"], 1)
            self.assertEqual(data["summary"]["non_object_json_records"], 1)
            self.assertEqual(data["summary"]["malformed_edges"], 1)

    def test_source_fingerprint_is_populated(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as nf, tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as ef:
            nf.write('{"id": "node-a"}\n')
            nf.flush()
            ef.write('{"id": "edge-1", "source_id": "node-a", "target_id": "node-a"}\n')
            ef.flush()
            result = run_script("--nodes-jsonl", nf.name, "--edges-jsonl", ef.name, "--format", "json")
            self.assertEqual(result.returncode, 0)
            data = json.loads(result.stdout)

            self.assertGreater(data["source"]["nodes_source"]["size_bytes"], 0)
            self.assertGreater(data["source"]["edges_source"]["size_bytes"], 0)
            self.assertEqual(len(data["source"]["nodes_source"]["sha256"]), 64)
            self.assertEqual(len(data["source"]["edges_source"]["sha256"]), 64)

    def test_postgres_cli_preflight_error_does_not_print_database_url(self):
        env = {
            "DATABASE_URL": "host=example.invalid password=SUPER_SECRET_PASSWORD dbname=db"
        }
        result = run_script("--postgres", env=env)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "DATABASE_URL must use a PostgreSQL URI with scheme postgres or postgresql",
            result.stderr,
        )
        self.assertNotIn("SUPER_SECRET_PASSWORD", result.stdout)
        self.assertNotIn("SUPER_SECRET_PASSWORD", result.stderr)
        self.assertNotIn("example.invalid", result.stdout)
        self.assertNotIn("example.invalid", result.stderr)

    def test_sanitize_psql_stderr_redacts_url_password_host(self):
        from scripts.docmeta.audit_domain_edge_references import sanitize_psql_stderr
        old_database_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = (
            "postgresql://user:SUPER_SECRET_PASSWORD@example.invalid/db"
        )
        try:
            raw = (
                "connection to postgresql://user:SUPER_SECRET_PASSWORD@example.invalid/db "
                "failed password=SUPER_SECRET_PASSWORD PGPASSWORD=SUPER_SECRET_PASSWORD"
            )
            clean = sanitize_psql_stderr(raw)
            self.assertNotIn("SUPER_SECRET_PASSWORD", clean)
            self.assertNotIn("example.invalid", clean)
            self.assertNotIn("postgresql://user", clean)
            self.assertNotIn("/db", clean)
        finally:
            if old_database_url is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = old_database_url

    def test_jsonl_output_contains_no_raw_ids_by_default(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as nf, tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as ef:
            nf.write('{"id": "node-a"}\n')
            nf.flush()
            ef.write('{"id": "edge-1", "source_id": "node-a", "target_id": "my-secret-target-id"}\n')
            ef.flush()
            result = run_script("--nodes-jsonl", nf.name, "--edges-jsonl", ef.name, "--format", "json")
            self.assertEqual(result.returncode, 0)
            self.assertNotIn("my-secret-target-id", result.stdout)

    def test_postgres_without_database_url_fails(self):
        result = run_script("--postgres", env={})
        self.assertNotEqual(result.returncode, 0)

    # --- New Tests ---

    def test_postgres_env_excludes_database_url(self):
        # We can test the function directly
        from scripts.docmeta.audit_domain_edge_references import postgres_env_from_database_url
        env = postgres_env_from_database_url("postgresql://foo:bar@example.com/db")
        self.assertNotIn("DATABASE_URL", env)
        self.assertEqual(env.get("PGHOST"), "example.com")
        self.assertEqual(env.get("PGDATABASE"), "db")

    def test_postgres_env_maps_sslmode_and_timeout(self):
        from scripts.docmeta.audit_domain_edge_references import postgres_env_from_database_url
        env = postgres_env_from_database_url("postgresql://localhost/db?sslmode=require&connect_timeout=10")
        self.assertEqual(env.get("PGSSLMODE"), "require")
        self.assertEqual(env.get("PGCONNECT_TIMEOUT"), "10")

    def test_load_postgres_nodes_reads_plain_ids(self):
        from unittest.mock import patch
        from scripts.docmeta.audit_domain_edge_references import load_postgres_nodes

        with patch("scripts.docmeta.audit_domain_edge_references.iter_psql_lines") as mock_iter:
            mock_iter.return_value = ["node-a\n", "node-b\n", "node-a\n", "   \n"]
            node_ids, summary = load_postgres_nodes({})

            self.assertEqual(node_ids, {"node-a", "node-b"})
            self.assertEqual(summary["node_records_total"], 4)
            self.assertEqual(summary["node_ids_total"], 2)
            self.assertEqual(summary["node_duplicate_ids"], 1)
            self.assertEqual(summary["nodes_empty_id"], 1)
            self.assertEqual(summary["node_invalid_json_records"], 0)

    def test_max_findings_truncates_findings(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as nf, tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as ef:
            nf.write('{"id": "node-a"}\n')
            nf.flush()
            # 3 invalid edges
            ef.write('{"id": "edge-1", "source_id": "missing-a", "target_id": "node-a"}\n')
            ef.write('{"id": "edge-2", "source_id": "missing-b", "target_id": "node-a"}\n')
            ef.write('{"id": "edge-3", "source_id": "missing-c", "target_id": "node-a"}\n')
            ef.flush()
            result = run_script("--nodes-jsonl", nf.name, "--edges-jsonl", ef.name, "--format", "json", "--max-findings", "2")
            self.assertEqual(result.returncode, 0)
            data = json.loads(result.stdout)

            self.assertEqual(len(data["findings"]), 2)
            self.assertIs(data["findings_truncated"], True)
            self.assertEqual(data["summary"]["untyped_missing_references"], 3) # Summary still complete

    def test_positive_max_findings_keeps_sorted_top_findings(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as nf, tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as ef:
            nf.write('{"id": "node-a"}\n')
            nf.flush()
            # Create findings in reverse order: edge-z comes first in stream
            ef.write('{"id": "edge-z", "source_id": "missing-a", "target_id": "node-a"}\n')
            ef.write('{"id": "edge-a", "source_id": "missing-a", "target_id": "node-a"}\n')
            ef.flush()

            # Using --show-ids so we can assert on exact edge_ref strings directly
            result = run_script("--nodes-jsonl", nf.name, "--edges-jsonl", ef.name, "--format", "json", "--max-findings", "1", "--show-ids")
            self.assertEqual(result.returncode, 0)
            data = json.loads(result.stdout)

            self.assertEqual(len(data["findings"]), 1)
            self.assertEqual(data["findings"][0]["edge_ref"], "edge-a")
            self.assertIs(data["findings_truncated"], True)
            self.assertEqual(data["findings_limit"], 1)
            self.assertIs(data["findings_unlimited"], False)

    def test_max_findings_zero_keeps_summary_and_truncates(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as nf, tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as ef:
            nf.write('{"id": "node-a"}\n')
            nf.flush()
            ef.write('{"id": "edge-1", "source_id": "missing-a", "target_id": "node-a"}\n')
            ef.flush()
            result = run_script("--nodes-jsonl", nf.name, "--edges-jsonl", ef.name, "--format", "json", "--max-findings", "0")
            self.assertEqual(result.returncode, 0)
            data = json.loads(result.stdout)

            self.assertEqual(len(data["findings"]), 0)
            self.assertIs(data["findings_truncated"], True)
            self.assertEqual(data["findings_limit"], 0)
            self.assertIs(data["findings_unlimited"], False)
            self.assertEqual(data["summary"]["untyped_missing_references"], 1)
            self.assertEqual(data["summary"]["untyped_existing_node_references"], 1)

    def test_max_findings_below_minus_one_fails(self):
        result = run_script("--max-findings", "-2")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--max-findings must be -1 or >= 0", result.stderr)

    def test_max_findings_minus_one_means_unlimited(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as nf, tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as ef:
            nf.write('{"id": "node-a"}\n')
            nf.flush()
            ef.write('{"id": "edge-1", "source_id": "missing-a", "target_id": "node-a"}\n')
            ef.write('{"id": "edge-2", "source_id": "missing-b", "target_id": "node-a"}\n')
            ef.flush()
            result = run_script("--nodes-jsonl", nf.name, "--edges-jsonl", ef.name, "--format", "json", "--max-findings", "-1")
            self.assertEqual(result.returncode, 0)
            data = json.loads(result.stdout)

            self.assertEqual(len(data["findings"]), 2)
            self.assertIs(data["findings_truncated"], False)
            self.assertIsNone(data["findings_limit"])
            self.assertIs(data["findings_unlimited"], True)

    def test_node_duplicate_ids_are_reported(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as nf, tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as ef:
            nf.write('{"id": "node-a"}\n{"id": "node-a"}\n')
            nf.flush()
            ef.write('{"id": "edge-1", "source_id": "node-a", "target_id": "node-a"}\n')
            ef.flush()
            result = run_script("--nodes-jsonl", nf.name, "--edges-jsonl", ef.name, "--format", "json", "--source-kind", "runtime")
            self.assertEqual(result.returncode, 0)
            data = json.loads(result.stdout)

            self.assertEqual(data["summary"]["node_duplicate_ids"], 1)
            self.assertIs(data["policy_signals"]["requires_cleanup"], True)
            self.assertIs(data["policy_signals"]["strict_node_fk_ready"], False)

    def test_non_string_type_hint_is_typed_unknown(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as nf, tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as ef:
            nf.write('{"id": "node-a"}\n')
            nf.flush()
            ef.write('{"id": "edge-1", "source_id": "node-a", "source_type": 123, "target_id": "node-a", "target_type": {"a": "b"}}\n')
            ef.flush()
            result = run_script("--nodes-jsonl", nf.name, "--edges-jsonl", ef.name, "--format", "json")
            self.assertEqual(result.returncode, 0)
            data = json.loads(result.stdout)

            self.assertEqual(data["summary"]["typed_unknown_references"], 2)
            self.assertNotIn("{'a': 'b'}", result.stdout)
            self.assertNotIn('"a": "b"', result.stdout)
            findings = [
                f for f in data["findings"]
                if f["classification"] == "typed_unknown_reference"
            ]
            self.assertEqual(len(findings), 2)
            self.assertEqual({f["type_hint_type"] for f in findings}, {"int", "dict"})
            self.assertEqual({f["type_hint"] for f in findings}, {"<int>", "<dict>"})

    def test_empty_edge_id_is_malformed(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as nf, tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as ef:
            nf.write('{"id": "node-a"}\n')
            nf.flush()
            ef.write('{"id": "  ", "source_id": "node-a", "target_id": "node-b"}\n')
            ef.flush()
            result = run_script("--nodes-jsonl", nf.name, "--edges-jsonl", ef.name, "--format", "json")
            self.assertEqual(result.returncode, 0)
            data = json.loads(result.stdout)
            self.assertEqual(data["summary"]["malformed_edges"], 1)

    def test_empty_source_id_is_malformed(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as nf, tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as ef:
            nf.write('{"id": "node-a"}\n')
            nf.flush()
            ef.write('{"id": "edge-1", "source_id": "", "target_id": "node-b"}\n')
            ef.flush()
            result = run_script("--nodes-jsonl", nf.name, "--edges-jsonl", ef.name, "--format", "json")
            self.assertEqual(result.returncode, 0)
            data = json.loads(result.stdout)
            self.assertEqual(data["summary"]["malformed_edges"], 1)

    def test_whitespace_target_id_is_malformed(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as nf, tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as ef:
            nf.write('{"id": "node-a"}\n')
            nf.flush()
            ef.write('{"id": "edge-1", "source_id": "node-a", "target_id": "  "}\n')
            ef.flush()
            result = run_script("--nodes-jsonl", nf.name, "--edges-jsonl", ef.name, "--format", "json")
            self.assertEqual(result.returncode, 0)
            data = json.loads(result.stdout)
            self.assertEqual(data["summary"]["malformed_edges"], 1)

    def test_node_empty_id_is_reported(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as nf, tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as ef:
            nf.write('{"id": "  "}\n')
            nf.flush()
            ef.write('{"id": "edge-1", "source_id": "node-a", "target_id": "node-b"}\n')
            ef.flush()
            result = run_script("--nodes-jsonl", nf.name, "--edges-jsonl", ef.name, "--format", "json", "--source-kind", "runtime")
            self.assertEqual(result.returncode, 0)
            data = json.loads(result.stdout)
            self.assertEqual(data["summary"]["nodes_empty_id"], 1)
            self.assertIs(data["policy_signals"]["requires_cleanup"], True)
            self.assertIs(data["policy_signals"]["strict_node_fk_ready"], False)

    def test_postgres_env_removes_existing_pg_env(self):
        from scripts.docmeta.audit_domain_edge_references import postgres_env_from_database_url
        old_host = os.environ.get("PGHOST")
        try:
            os.environ["PGHOST"] = "stale-host"
            env = postgres_env_from_database_url("postgresql://user:pw@example.invalid/db")
            self.assertEqual(env["PGHOST"], "example.invalid")
            self.assertNotEqual(env["PGHOST"], "stale-host")
        finally:
            if old_host is not None:
                os.environ["PGHOST"] = old_host
            else:
                os.environ.pop("PGHOST", None)

    def test_postgres_env_ignores_unknown_query_params(self):
        from scripts.docmeta.audit_domain_edge_references import postgres_env_from_database_url
        env = postgres_env_from_database_url("postgresql://localhost/db?sslmode=require&unknown=value")
        self.assertEqual(env.get("PGSSLMODE"), "require")
        # Keep arbitrary inherited OS variables (including Nix triplets).
        # Only new names derived from URL query parameters may be inspected.
        for key in set(env) - set(os.environ):
            self.assertNotIn("unknown", key.lower())

    def test_unknown_string_type_hint_is_redacted_when_not_safe(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as nf, tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl') as ef:
            nf.write('{"id": "node-a"}\n')
            nf.flush()
            ef.write('{"id": "edge-1", "source_id": "node-a", "source_type": "some-long-string-with-sensitives!@#$", "target_id": "node-a", "target_type": "node"}\n')
            ef.flush()
            result = run_script("--nodes-jsonl", nf.name, "--edges-jsonl", ef.name, "--format", "json")
            self.assertEqual(result.returncode, 0)
            data = json.loads(result.stdout)

            self.assertEqual(data["summary"]["typed_unknown_references"], 1)
            self.assertNotIn("some-long-string-with-sensitives", result.stdout)
            finding = data["findings"][0]
            self.assertTrue(finding["type_hint"].startswith("type_hint:sha256:"))

    def test_postgres_env_rejects_libpq_dsn_string(self):
        from scripts.docmeta.audit_domain_edge_references import postgres_env_from_database_url

        with self.assertRaises(ValueError):
            postgres_env_from_database_url(
                "host=localhost port=5432 user=postgres dbname=mydb"
            )

    def test_postgres_rejects_libpq_dsn_before_psql_preflight(self):
        result = run_script(
            "--postgres",
            env={"DATABASE_URL": "host=localhost port=5432 user=postgres dbname=mydb"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DATABASE_URL must use a PostgreSQL URI with scheme postgres or postgresql", result.stderr)

    def test_empty_string_type_hint_is_treated_as_untyped(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl") as nf, tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl") as ef:
            nf.write('{"id": "node-a"}\n')
            nf.flush()
            ef.write(
                '{"id": "edge-1", "source_id": "node-a", "source_type": "", '
                '"target_id": "missing-b", "target_type": "   "}\n'
            )
            ef.flush()

            result = run_script(
                "--nodes-jsonl",
                nf.name,
                "--edges-jsonl",
                ef.name,
                "--format",
                "json",
                "--source-kind",
                "runtime",
            )
            self.assertEqual(result.returncode, 0)
            data = json.loads(result.stdout)

            self.assertEqual(data["summary"]["untyped_existing_node_references"], 1)
            self.assertEqual(data["summary"]["untyped_missing_references"], 1)
            self.assertEqual(data["summary"]["typed_unknown_references"], 0)
            self.assertNotIn('"type_hint": ""', result.stdout)

    def test_edges_path_directory_fails_without_traceback(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl") as nf, tempfile.TemporaryDirectory() as edge_dir:
            nf.write('{"id": "node-a"}\n')
            nf.flush()

            result = run_script(
                "--nodes-jsonl",
                nf.name,
                "--edges-jsonl",
                edge_dir,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("edges file", result.stderr.lower())
            self.assertNotIn("Traceback", result.stderr)

    def test_nodes_path_directory_fails_without_traceback(self):
        with tempfile.TemporaryDirectory() as node_dir, tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl") as ef:
            ef.write('{"id": "edge-1", "source_id": "node-a", "target_id": "node-b"}\n')
            ef.flush()

            result = run_script(
                "--nodes-jsonl",
                node_dir,
                "--edges-jsonl",
                ef.name,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("nodes file", result.stderr.lower())
            self.assertNotIn("Traceback", result.stderr)

    def test_postgres_edges_query_does_not_order_by_id(self):
        import inspect
        from scripts.docmeta.audit_domain_edge_references import iter_postgres_edges

        source = inspect.getsource(iter_postgres_edges)
        self.assertIn("FROM domain_edges", source)
        self.assertNotIn("ORDER BY id", source)

    def test_empty_runtime_smoke_requires_representative_runtime_data(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl") as nf, tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl") as ef:
            nf.flush()
            ef.flush()

            result = run_script(
                "--nodes-jsonl",
                nf.name,
                "--edges-jsonl",
                ef.name,
                "--source-kind",
                "runtime",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 0)
            data = json.loads(result.stdout)

            self.assertEqual(data["summary"]["node_records_total"], 0)
            self.assertEqual(data["summary"]["edge_records_total"], 0)
            self.assertEqual(data["summary"]["auditable_edges_total"], 0)
            self.assertEqual(data["summary"]["edge_sides_total"], 0)

            self.assertIs(data["policy_signals"]["strict_node_fk_ready"], False)
            self.assertIs(data["policy_signals"]["requires_runtime_data_run"], True)
            self.assertIs(data["policy_signals"]["runtime_reference_data_observed"], False)

    def test_postgres_env_removes_ambient_pg_variables(self):
        from unittest.mock import patch
        from scripts.docmeta.audit_domain_edge_references import postgres_env_from_database_url

        with patch.dict(os.environ, {"PGOPTIONS": "ambient-opt", "PGPASSFILE": "ambient-file", "DATABASE_URL": "postgresql://user:pass@example.test:5432/db"}):
            env = postgres_env_from_database_url("postgresql://user:pass@example.test:5432/db?sslmode=require")
            self.assertNotIn("PGOPTIONS", env)
            self.assertNotIn("PGPASSFILE", env)
            self.assertNotIn("DATABASE_URL", env)
            self.assertEqual(env.get("PGHOST"), "example.test")
            self.assertEqual(env.get("PGPORT"), "5432")
            self.assertEqual(env.get("PGUSER"), "user")
            self.assertEqual(env.get("PGPASSWORD"), "pass")
            self.assertEqual(env.get("PGDATABASE"), "db")
            self.assertEqual(env.get("PGSSLMODE"), "require")

if __name__ == "__main__":
    unittest.main()
