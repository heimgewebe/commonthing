#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.performance import domain_scale  # noqa: E402


CONFIG = ROOT / "configs/performance/domain-scale.v1.json"
SCRIPT = ROOT / "scripts/performance/domain_scale.py"


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def _good_plan(config: dict[str, object], workload: str) -> list[dict[str, object]]:
    rules = config["plan_budgets"]["workloads"][workload]  # type: ignore[index]
    condition = " AND ".join(
        f"({token} IS NOT NULL)" for token in rules["required_index_cond_identifiers"]  # type: ignore[index]
    )
    return [
        {
            "Plan": {
                "Node Type": "Limit",
                "Temp Read Blocks": 0,
                "Temp Written Blocks": 0,
                "Plans": [
                    {
                        "Node Type": "Index Scan",
                        "Index Name": rules["required_index"],  # type: ignore[index]
                        "Index Cond": condition,
                        "Actual Loops": 1,
                    }
                ],
            },
            "Planning Time": 0.2,
            "Execution Time": 1.5,
        }
    ]


def _write_good_plans(config: dict[str, object], plan_dir: Path) -> None:
    plan_dir.mkdir(parents=True, exist_ok=True)
    for workload in domain_scale.WORKLOAD_ORDER:
        _write_json(plan_dir / f"{workload}.json", _good_plan(config, workload))


class DomainScaleTests(unittest.TestCase):
    def test_config_requires_reserved_schema_and_index_contracts(self) -> None:
        config = domain_scale.load_config(CONFIG)
        self.assertEqual(config["database_schema"], domain_scale.BENCHMARK_SCHEMA)
        self.assertIn("ci", config["plan_budgets"]["enforced_profiles"])
        for schema in ("public", "pg_catalog", "information_schema", "other_benchmark"):
            with self.subTest(schema=schema):
                unsafe = dict(config)
                unsafe["database_schema"] = schema
                with self.assertRaisesRegex(domain_scale.DomainScaleError, "reserved benchmark schema"):
                    domain_scale.validate_config(unsafe)

        broken = json.loads(json.dumps(config))
        del broken["plan_budgets"]["workloads"]["bbox_cursor"]["required_index"]
        with self.assertRaisesRegex(domain_scale.DomainScaleError, "required_index"):
            domain_scale.validate_config(broken)

    def test_smoke_fixture_is_deterministic_and_has_exact_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first"
            second = root / "second"
            manifest_a = domain_scale.generate_fixture(CONFIG, "smoke", first)
            manifest_b = domain_scale.generate_fixture(CONFIG, "smoke", second)
            self.assertEqual(manifest_a, manifest_b)
            for filename in ("domain_nodes.csv", "domain_edges.csv", "manifest.json"):
                self.assertEqual((first / filename).read_bytes(), (second / filename).read_bytes())

            with (first / "domain_nodes.csv").open(encoding="utf-8", newline="") as handle:
                node_rows = list(csv.DictReader(handle))
            with (first / "domain_edges.csv").open(encoding="utf-8", newline="") as handle:
                edge_rows = list(csv.DictReader(handle))
            self.assertEqual(len(node_rows), 1000)
            self.assertEqual(len(edge_rows), 5000)
            self.assertEqual(
                sum(row["kind"] == domain_scale.RARE_NODE_KIND for row in node_rows),
                10,
            )
            self.assertTrue(all(row["source_id"] != row["target_id"] for row in edge_rows))
            node_ids = {row["id"] for row in node_rows}
            self.assertEqual({row["source_id"] for row in edge_rows}, node_ids)
            self.assertEqual({row["target_id"] for row in edge_rows}, node_ids)

    def test_load_sql_isolated_to_benchmark_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / "fixture"
            domain_scale.generate_fixture(CONFIG, "smoke", fixture)
            output = root / "load.sql"
            domain_scale.render_load_sql(fixture / "manifest.json", output, CONFIG)
            sql = output.read_text(encoding="utf-8")
            self.assertIn("DROP SCHEMA IF EXISTS weltgewebe_perf CASCADE", sql)
            self.assertIn("LIKE public.domain_nodes INCLUDING ALL", sql)
            self.assertIn("LIKE public.domain_edges INCLUDING ALL", sql)
            self.assertIn(
                "ADD COLUMN IF NOT EXISTS search_visibility TEXT NOT NULL DEFAULT 'public'",
                " ".join(sql.split()),
            )
            self.assertLess(
                sql.index("ADD COLUMN IF NOT EXISTS search_visibility"),
                sql.index("\\copy weltgewebe_perf.domain_nodes"),
            )
            self.assertNotIn("DROP SCHEMA IF EXISTS public", sql)
            self.assertNotIn("TRUNCATE", sql.upper())
            self.assertNotIn("DELETE FROM public", sql)
            self.assertEqual(sql.count("COMMIT;"), 1)
            self.assertLess(sql.index("\\copy weltgewebe_perf.domain_edges"), sql.index("COMMIT;"))

    def test_psql_meta_paths_reject_parser_metacharacters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unsafe_fixture = root / "path with space" / "fixture"
            domain_scale.generate_fixture(CONFIG, "smoke", unsafe_fixture)
            with self.assertRaisesRegex(domain_scale.DomainScaleError, "psql file paths"):
                domain_scale.render_load_sql(
                    unsafe_fixture / "manifest.json", root / "load.sql", CONFIG
                )

            safe_fixture = root / "safe-fixture"
            domain_scale.generate_fixture(CONFIG, "smoke", safe_fixture)
            with self.assertRaisesRegex(domain_scale.DomainScaleError, "psql file paths"):
                domain_scale.render_workload_sql(
                    safe_fixture / "manifest.json",
                    root / "plans'quoted",
                    root / "workload.sql",
                    CONFIG,
                )

    def test_workload_sql_matches_cursor_and_bbox_api_query_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / "fixture"
            plans = root / "plans"
            domain_scale.generate_fixture(CONFIG, "smoke", fixture)
            output = root / "workload.sql"
            domain_scale.render_workload_sql(fixture / "manifest.json", plans, output, CONFIG)
            sql = output.read_text(encoding="utf-8")
            self.assertEqual(sql.count("EXPLAIN (ANALYZE, BUFFERS, WAL, FORMAT JSON)"), 7)
            for workload in domain_scale.WORKLOAD_ORDER:
                self.assertIn(f"{workload}.json", sql)
            self.assertIn("WHERE id >", sql)
            self.assertIn("ORDER BY id ASC LIMIT 1000", sql)
            self.assertIn("WHERE kind = 'project' LIMIT 500", sql)
            self.assertNotIn("WHERE kind = 'project' ORDER BY", sql)
            self.assertIn(
                "SELECT id, kind, title, lat, lon, created_at, updated_at, payload::text, search_visibility FROM weltgewebe_perf.domain_nodes WHERE lat >= -10.0 AND lat <= 10.0 AND lon >= -10.0 AND lon <= 10.0 ORDER BY id ASC LIMIT 1001 OFFSET 0",
                sql,
            )
            self.assertIn(
                "SELECT id, kind, title, lat, lon, created_at, updated_at, payload::text, search_visibility FROM weltgewebe_perf.domain_nodes WHERE lat >= -10.0 AND lat <= 10.0 AND lon >= -10.0 AND lon <= 10.0 AND id >",
                sql,
            )
            self.assertEqual(
                sql.count(
                    "id, kind, title, lat, lon, created_at, updated_at, payload::text, search_visibility FROM weltgewebe_perf.domain_nodes WHERE lat >= -10.0"
                ),
                2,
            )
            self.assertIn("ORDER BY id ASC LIMIT 1001 OFFSET 0", sql)
            self.assertIn("ORDER BY id ASC LIMIT 1001", sql)
            self.assertNotIn("ORDER BY id ASC LIMIT 5000", sql)
            self.assertIn("WHERE source_id = 'node-", sql)
            self.assertIn("WHERE target_id = 'node-", sql)

    def test_bbox_workloads_are_derived_from_production_rust_sql(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / "fixture"
            plans = root / "plans"
            fake_domain_db = root / "domain_db.rs"
            fake_domain_db.write_text(
                """pub async fn load_nodes_bbox_from_postgres() {
    let rows = sqlx::query_as(
        "SELECT id, title, lat, lon FROM domain_nodes WHERE lat >= $1 AND lat <= $2 AND lon >= $3 AND lon <= $4 ORDER BY id ASC LIMIT $5 OFFSET $6",
    );
}

pub async fn load_nodes_bbox_after_id_from_postgres() {
    let rows = sqlx::query_as(
        "SELECT id, title, lat, lon FROM domain_nodes WHERE lat >= $1 AND lat <= $2 AND lon >= $3 AND lon <= $4 AND id > $5 ORDER BY id ASC LIMIT $6",
    );
}
""",
                encoding="utf-8",
                newline="\n",
            )
            domain_scale.generate_fixture(CONFIG, "smoke", fixture)
            output = root / "workload.sql"
            with mock.patch.object(domain_scale, "DOMAIN_DB_SOURCE", fake_domain_db):
                domain_scale.render_workload_sql(
                    fixture / "manifest.json", plans, output, CONFIG
                )
            sql = output.read_text(encoding="utf-8")
            self.assertIn(
                "SELECT id, title, lat, lon FROM weltgewebe_perf.domain_nodes WHERE lat >= -10.0 AND lat <= 10.0 AND lon >= -10.0 AND lon <= 10.0 ORDER BY id ASC LIMIT 1001 OFFSET 0",
                sql,
            )
            self.assertIn(
                "SELECT id, title, lat, lon FROM weltgewebe_perf.domain_nodes WHERE lat >= -10.0 AND lat <= 10.0 AND lon >= -10.0 AND lon <= 10.0 AND id >",
                sql,
            )

    def test_plan_checker_requires_the_workload_specific_index_condition(self) -> None:
        config = domain_scale.load_config(CONFIG)
        with tempfile.TemporaryDirectory() as directory:
            plan_dir = Path(directory)
            _write_good_plans(config, plan_dir)
            report = domain_scale.check_plans(config, plan_dir, "ci")
            self.assertEqual(report["status"], "pass")
            self.assertTrue(report["budget_enforced"])
            for workload in domain_scale.WORKLOAD_ORDER:
                self.assertTrue(report["workloads"][workload]["required_index_evidence"])

            wrong_index = _good_plan(config, "bbox_cursor")
            wrong_index[0]["Plan"]["Plans"][0]["Index Name"] = "domain_nodes_pkey"  # type: ignore[index]
            _write_json(plan_dir / "bbox_cursor.json", wrong_index)
            failed = domain_scale.check_plans(config, plan_dir, "ci")
            self.assertEqual(failed["status"], "fail")
            self.assertTrue(any("domain_nodes_lat_lon" in item for item in failed["failures"]))

            _write_good_plans(config, plan_dir)
            wrong_condition = _good_plan(config, "bbox_cursor")
            wrong_condition[0]["Plan"]["Plans"][0]["Index Cond"] = (
                "(latitude IS NOT NULL) AND (longitude IS NOT NULL)"
            )  # type: ignore[index]
            _write_json(plan_dir / "bbox_cursor.json", wrong_condition)
            condition_failed = domain_scale.check_plans(config, plan_dir, "ci")
            self.assertEqual(condition_failed["status"], "fail")
            self.assertTrue(any("matching Index Cond" in item for item in condition_failed["failures"]))

            _write_good_plans(config, plan_dir)
            not_executed = _good_plan(config, "bbox_cursor")
            not_executed[0]["Plan"]["Plans"][0]["Actual Loops"] = 0  # type: ignore[index]
            _write_json(plan_dir / "bbox_cursor.json", not_executed)
            execution_failed = domain_scale.check_plans(config, plan_dir, "ci")
            self.assertEqual(execution_failed["status"], "fail")
            self.assertTrue(any("was not executed" in item for item in execution_failed["failures"]))

    def test_plan_checker_rejects_seq_scan_and_nested_temp_blocks(self) -> None:
        config = domain_scale.load_config(CONFIG)
        with tempfile.TemporaryDirectory() as directory:
            plan_dir = Path(directory)
            _write_good_plans(config, plan_dir)
            bad_plan = [
                {
                    "Plan": {
                        "Node Type": "Seq Scan",
                        "Temp Read Blocks": 0,
                        "Temp Written Blocks": 0,
                    },
                    "Planning Time": 0.1,
                    "Execution Time": 10.0,
                }
            ]
            _write_json(plan_dir / "node_cursor.json", bad_plan)
            failed = domain_scale.check_plans(config, plan_dir, "ci")
            self.assertEqual(failed["status"], "fail")
            self.assertTrue(any("Seq Scan" in item for item in failed["failures"]))

            observation = domain_scale.check_plans(config, plan_dir, "smoke")
            self.assertEqual(observation["status"], "pass")
            self.assertFalse(observation["budget_enforced"])
            self.assertIn("Seq Scan", observation["workloads"]["node_cursor"]["node_types"])

            _write_good_plans(config, plan_dir)
            temp_plan = _good_plan(config, "kind_filter")
            temp_plan[0]["Plan"]["Plans"][0]["Temp Written Blocks"] = 1  # type: ignore[index]
            _write_json(plan_dir / "kind_filter.json", temp_plan)
            temp_failed = domain_scale.check_plans(config, plan_dir, "ci")
            self.assertTrue(any("temporary blocks" in item for item in temp_failed["failures"]))

    def test_manifest_detects_fixture_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "fixture"
            domain_scale.generate_fixture(CONFIG, "smoke", fixture)
            with (fixture / "domain_nodes.csv").open("a", encoding="utf-8") as handle:
                handle.write("tampered\n")
            with self.assertRaisesRegex(domain_scale.DomainScaleError, "hash mismatch"):
                domain_scale.load_manifest(fixture / "manifest.json")

    def test_manifest_rejects_self_consistent_row_count_lie(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "fixture"
            domain_scale.generate_fixture(CONFIG, "smoke", fixture)
            nodes = fixture / "domain_nodes.csv"
            rows = nodes.read_text(encoding="utf-8").splitlines()
            nodes.write_text("\n".join(rows + [rows[1]]) + "\n", encoding="utf-8", newline="\n")
            manifest_path = fixture / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["files"]["nodes"]["sha256"] = domain_scale.sha256_file(nodes)
            _write_json(manifest_path, manifest)
            with self.assertRaisesRegex(domain_scale.DomainScaleError, "row count mismatch"):
                domain_scale.load_manifest(manifest_path)

    def test_manifest_rejects_schema_path_and_missing_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for mutation, message in (
                (("database_schema", "public"), "reserved benchmark schema"),
                (("files.nodes.name", "../../etc/passwd"), "filename must be local"),
            ):
                fixture = root / mutation[0].replace(".", "-")
                domain_scale.generate_fixture(CONFIG, "smoke", fixture)
                manifest_path = fixture / "manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if mutation[0] == "database_schema":
                    manifest["database_schema"] = mutation[1]
                else:
                    manifest["files"]["nodes"]["name"] = mutation[1]
                _write_json(manifest_path, manifest)
                with self.assertRaisesRegex(domain_scale.DomainScaleError, message):
                    domain_scale.load_manifest(manifest_path)

            fixture = root / "missing"
            domain_scale.generate_fixture(CONFIG, "smoke", fixture)
            manifest_path = fixture / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            del manifest["profile"]
            _write_json(manifest_path, manifest)
            with self.assertRaisesRegex(domain_scale.DomainScaleError, "canonical"):
                domain_scale.load_manifest(manifest_path)

    def test_manifest_profile_counts_and_config_hash_are_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / "fixture"
            domain_scale.generate_fixture(CONFIG, "smoke", fixture)
            manifest_path = fixture / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["profile"] = "ci"
            _write_json(manifest_path, manifest)
            with self.assertRaisesRegex(domain_scale.DomainScaleError, "counts do not match"):
                domain_scale.load_bound_manifest(manifest_path, CONFIG)

            domain_scale.generate_fixture(CONFIG, "smoke", fixture)
            altered_config = root / "altered-config.json"
            altered_config.write_text(
                json.dumps(domain_scale.load_config(CONFIG), indent=4) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            with self.assertRaisesRegex(domain_scale.DomainScaleError, "another scale config"):
                domain_scale.load_bound_manifest(fixture / "manifest.json", altered_config)

    def test_cli_writes_failure_report_before_nonzero_exit(self) -> None:
        base_config = domain_scale.load_config(CONFIG)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = json.loads(json.dumps(base_config))
            config["profiles"] = {"ci": {"nodes": 2, "edges": 2}}
            config["plan_budgets"]["enforced_profiles"] = ["ci"]
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8", newline="\n")
            fixture = root / "fixture"
            domain_scale.generate_fixture(config_path, "ci", fixture)
            plan_dir = root / "plans"
            _write_good_plans(config, plan_dir)
            wrong = _good_plan(config, "outbound_neighbors")
            wrong[0]["Plan"]["Plans"][0]["Index Name"] = "domain_edges_pkey"  # type: ignore[index]
            _write_json(plan_dir / "outbound_neighbors.json", wrong)
            report_path = root / "report.json"
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(SCRIPT),
                    "--config",
                    str(config_path),
                    "check",
                    "--manifest",
                    str(fixture / "manifest.json"),
                    "--plan-dir",
                    str(plan_dir),
                    "--report",
                    str(report_path),
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertTrue(report_path.is_file())
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "fail")
            self.assertIn("required index", result.stderr)


if __name__ == "__main__":
    unittest.main()
