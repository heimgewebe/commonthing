"""Tests for scripts/performance/api_runtime_evidence.py.

Covers: policy schema/scenario binding, Prometheus text parsing (counters and
histograms, including counter-reset and malformed-input rejection), k6
summary parsing, end-to-end evidence assembly (pass and fail), the
deterministic query-count cross-check on missing/contradictory evidence,
git-HEAD/policy revision binding via `verify`, and the regression-fixture
negative case that must demonstrably fail the gate.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.performance import api_runtime_evidence as evidence  # noqa: E402
from scripts.performance import domain_scale  # noqa: E402

POLICY_PATH = ROOT / "policies/performance.v1.json"
FAKE_GIT_HEAD = "a" * 40
FAKE_POLICY_SHA256 = "b" * 64
FAKE_DATASET_MANIFEST_SHA256 = "c" * 64
FAKE_K6_IMAGE = "grafana/k6@sha256:" + ("1" * 64)
FAKE_RUN_ID = "api-runtime-test-run"
FAKE_LOAD_STARTED_AT_UNIX_MS = 1_000_000
FAKE_LOAD_FINISHED_AT_UNIX_MS = 1_030_000
FAKE_SAMPLE_STARTED_AT_UNIX_MS = 999_000
FAKE_SAMPLE_FINISHED_AT_UNIX_MS = 1_031_000


def _load_policy() -> dict:
    return evidence.load_policy(POLICY_PATH)


def _canonical_scenario() -> dict:
    policy_scenario = _load_policy()["measurements"]["api_runtime"]["scenario"]
    return {
        "virtual_users": 10,
        "duration_seconds": 30,
        "dataset_profile": "domain-scale-ci",
        "concurrency_profile": "mixed-health-and-read",
        "search_query": policy_scenario["search_query"],
    }


def _k6_summary(
    *,
    p50=20.0,
    p95=40.0,
    p99=60.0,
    failed_rate=0.0,
    total_requests=300,
    scenario=None,
    dataset_manifest_sha256=FAKE_DATASET_MANIFEST_SHA256,
    search_query="Scale",
    k6_image=FAKE_K6_IMAGE,
    run_id=FAKE_RUN_ID,
    load_started_at_unix_ms=FAKE_LOAD_STARTED_AT_UNIX_MS,
    load_finished_at_unix_ms=FAKE_LOAD_FINISHED_AT_UNIX_MS,
) -> dict:
    return {
        "metrics": {
            "http_req_duration": {"values": {"p(50)": p50, "p(95)": p95, "p(99)": p99}},
            "http_req_failed": {"values": {"rate": failed_rate}},
            "http_reqs": {"values": {"count": total_requests}},
        },
        evidence.DATASET_MANIFEST_SUMMARY_KEY: dataset_manifest_sha256,
        evidence.live_binding.SEARCH_QUERY_SUMMARY_KEY: search_query,
        evidence.live_binding.RUN_ID_SUMMARY_KEY: run_id,
        evidence.K6_LOAD_STARTED_SUMMARY_KEY: load_started_at_unix_ms,
        evidence.K6_LOAD_FINISHED_SUMMARY_KEY: load_finished_at_unix_ms,
        evidence.live_binding.K6_IMAGE_SUMMARY_KEY: k6_image,
        "weltgewebe_scenario": scenario if scenario is not None else _canonical_scenario(),
    }


def _prometheus_text(
    *,
    search_count: int,
    search_sum_seconds: float,
    http_search_requests: int,
    git_commit: str = FAKE_GIT_HEAD,
) -> str:
    return evidence._render_prometheus_fixture(
        search_count=search_count,
        search_sum_seconds=search_sum_seconds,
        http_search_requests=http_search_requests,
        git_commit=git_commit,
    )


def _dataset_binding() -> dict:
    return {
        "manifest_sha256": FAKE_DATASET_MANIFEST_SHA256,
        "generator": evidence.DOMAIN_SCALE_GENERATOR,
        "config_sha256": "d" * 64,
        "database_schema": "weltgewebe_perf",
        "profile": "ci",
        "counts": {"nodes": 20000, "edges": 100000},
        "files": {
            "nodes": {"name": "domain_nodes.csv", "sha256": "e" * 64},
            "edges": {"name": "domain_edges.csv", "sha256": "f" * 64},
        },
    }


def _runtime_binding(
    *,
    git_head: str = FAKE_GIT_HEAD,
    manifest_sha256: str = FAKE_DATASET_MANIFEST_SHA256,
    node_count: int = 20000,
    search_query: str = "Scale",
    k6_image: str = FAKE_K6_IMAGE,
    run_id: str = FAKE_RUN_ID,
) -> dict:
    candidate_limit, candidate_source_sha256 = evidence.live_binding.candidate_limit_binding(ROOT)
    active = node_count
    content_sha256 = "2" * 64
    image_id = "sha256:" + ("3" * 64)
    return evidence.live_binding.validate_receipt(
        {
            "schema_version": evidence.live_binding.SCHEMA_VERSION,
            "contract": evidence.live_binding.CONTRACT,
            "run_id": run_id,
            "git_head": git_head,
            "dataset": {
                "manifest_sha256": manifest_sha256,
                "domain_nodes_count": node_count,
                "fixture_nodes_content_sha256": content_sha256,
                "database_nodes_content_sha256": content_sha256,
            },
            "search": {
                "query": search_query,
                "mode": "lexical_fallback",
                "generation_id": "search-generation-test",
                "candidate_limit_contract": candidate_limit,
                "candidate_limit_source": str(evidence.live_binding.CANDIDATE_LIMIT_SOURCE),
                "candidate_limit_source_sha256": candidate_source_sha256,
                "expected_nodes": active,
                "completed_nodes": active,
                "active_projection_count": active,
                "fixture_projection_content_sha256": content_sha256,
                "database_projection_content_sha256": content_sha256,
                "sampled_items": [{"id": "node-test", "title": "Scale node test"}],
            },
            "runtime": {
                "api_commit": git_head,
                "api_container": {
                    "name": "api-test",
                    "image_reference": "fixture/api@sha256:" + ("4" * 64),
                    "image_id": image_id,
                },
                "postgres_container": {
                    "name": "db-test",
                    "image_reference": "fixture/postgres@sha256:" + ("5" * 64),
                    "image_id": image_id,
                },
                "k6_image_reference": k6_image,
            },
        }
    )


def _resource_receipt(
    *,
    run_id: str = FAKE_RUN_ID,
    container_name: str = "api-test",
    started_at_unix_ms: int = FAKE_SAMPLE_STARTED_AT_UNIX_MS,
    finished_at_unix_ms: int = FAKE_SAMPLE_FINISHED_AT_UNIX_MS,
) -> dict:
    return {
        "run_id": run_id,
        "container_name": container_name,
        "started_at_unix_ms": started_at_unix_ms,
        "finished_at_unix_ms": finished_at_unix_ms,
        "sample_count": 30,
        "peak_cpu_percent": 12.5,
        "peak_memory_bytes": 104857600,
    }


def _raw_resource_receipt(
    *,
    run_id: str = FAKE_RUN_ID,
    container_name: str = "api-test",
    started_at_unix_ms: int = FAKE_SAMPLE_STARTED_AT_UNIX_MS,
    finished_at_unix_ms: int = FAKE_SAMPLE_FINISHED_AT_UNIX_MS,
) -> dict:
    return {
        "schema_version": 3,
        "contract": evidence.RESOURCE_RECEIPT_CONTRACT,
        "run_id": run_id,
        "container_name": container_name,
        "started_at_unix_ms": started_at_unix_ms,
        "finished_at_unix_ms": finished_at_unix_ms,
        "sample_count": 30,
        "peaks": {"cpu_percent": 12.5, "memory_bytes": 104857600},
    }


def _database_connection_receipt(
    *,
    run_id: str = FAKE_RUN_ID,
    database_container: str = "db-test",
    samples: list[int] | None = None,
    started_at_unix_ms: int = FAKE_SAMPLE_STARTED_AT_UNIX_MS,
    finished_at_unix_ms: int = FAKE_SAMPLE_FINISHED_AT_UNIX_MS,
) -> dict:
    values = [4, 5, 4] if samples is None else list(samples)
    return {
        "run_id": run_id,
        "database_container": database_container,
        "started_at_unix_ms": started_at_unix_ms,
        "finished_at_unix_ms": finished_at_unix_ms,
        "max_connections": max(values),
        "sample_count": len(values),
        "samples": values,
    }


def _raw_database_connection_receipt(
    *,
    run_id: str = FAKE_RUN_ID,
    database_container: str = "db-test",
    started_at_unix_ms: int = FAKE_SAMPLE_STARTED_AT_UNIX_MS,
    finished_at_unix_ms: int = FAKE_SAMPLE_FINISHED_AT_UNIX_MS,
) -> dict:
    return {
        "schema_version": 2,
        "contract": evidence.DATABASE_CONNECTION_RECEIPT_CONTRACT,
        **_database_connection_receipt(
            run_id=run_id,
            database_container=database_container,
            started_at_unix_ms=started_at_unix_ms,
            finished_at_unix_ms=finished_at_unix_ms,
        ),
    }


def _write_tiny_dataset_contract(root: Path) -> tuple[Path, Path]:
    config = json.loads(
        (ROOT / "configs/performance/domain-scale.v1.json").read_text(encoding="utf-8")
    )
    config["profiles"]["ci"] = {"nodes": 4, "edges": 8}
    config_path = root / "domain-scale.v1.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    fixture_dir = root / "dataset"
    domain_scale.generate_fixture(config_path, "ci", fixture_dir)

    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    policy["measurements"]["database_scale"]["config"] = str(config_path)
    policy_path = root / "performance.v1.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    return policy_path, fixture_dir / "manifest.json"


class PolicyBindingTests(unittest.TestCase):
    def test_real_policy_scenario_matches_the_documented_ci_contract(self) -> None:
        policy = _load_policy()
        contract = evidence.api_runtime_section(policy)
        self.assertEqual(contract["scenario"], _canonical_scenario())
        self.assertEqual(contract["thresholds"]["http_request_duration_ms"], 300.0)
        self.assertEqual(contract["thresholds"]["http_request_duration_p99_ms"], 750.0)
        self.assertEqual(contract["thresholds"]["http_request_failed_rate"], 0.01)
        self.assertEqual(
            contract["dataset_proof"],
            {
                "generator": evidence.DOMAIN_SCALE_GENERATOR,
                "config": "configs/performance/domain-scale.v1.json",
                "profile": "ci",
            },
        )
        self.assertEqual(
            policy["measurements"]["api_replica_resources"]["metrics"],
            ["peak_cpu_percent", "peak_memory_bytes", "database_connections"],
        )

    def test_post_deploy_readiness_503_policy_is_explicit(self) -> None:
        policy = _load_policy()["measurements"]["api_runtime"]
        self.assertEqual(policy["metrics"]["http_request_failed_rate"]["max"], 0.01)
        limitations = policy["limitations"]
        self.assertTrue(
            any(
                "a /health/ready HTTP 503 during the measured load counts" in value
                for value in limitations
            )
        )
        self.assertTrue(
            any(
                "earlier 503-tolerant run artifacts are not retrospectively reclassified"
                in value
                for value in limitations
            )
        )
        self.assertTrue(
            any("does not establish bulk event ingestion throughput" in item for item in limitations)
        )

    def test_policy_defines_repository_duration_span(self) -> None:
        policy = _load_policy()
        limitations = policy["measurements"]["api_runtime"]["limitations"]
        matching = [
            item
            for item in limitations
            if item.startswith(
                "search_repository_duration_seconds measures the API repository span"
            )
        ]
        self.assertEqual(len(matching), 1)
        self.assertIn(
            "active-generation lookup plus authorized candidate retrieval and decoding",
            matching[0],
        )
        self.assertIn("search_provider_duration_seconds", matching[0])
        self.assertIn("not PostgreSQL-server-only execution time", matching[0])

    def test_load_policy_rejects_malformed_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaisesRegex(evidence.ApiRuntimeEvidenceError, "not valid JSON"):
                evidence.load_policy(path)

    def test_load_policy_rejects_wrong_contract_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            path.write_text(json.dumps({"contract_id": "other", "measurements": {}}), encoding="utf-8")
            with self.assertRaisesRegex(evidence.ApiRuntimeEvidenceError, "contract_id"):
                evidence.load_policy(path)

    def test_scenario_must_define_exactly_the_canonical_fields(self) -> None:
        policy = _load_policy()
        broken = json.loads(json.dumps(policy))
        del broken["measurements"]["api_runtime"]["scenario"]["dataset_profile"]
        with self.assertRaisesRegex(evidence.ApiRuntimeEvidenceError, "scenario"):
            evidence.api_runtime_section(broken)

    def test_search_query_must_be_a_non_empty_string(self) -> None:
        policy = _load_policy()
        for invalid in ("", None, 42):
            with self.subTest(invalid=invalid):
                broken = json.loads(json.dumps(policy))
                broken["measurements"]["api_runtime"]["scenario"]["search_query"] = invalid
                with self.assertRaisesRegex(evidence.ApiRuntimeEvidenceError, "search_query"):
                    evidence.api_runtime_section(broken)

    def test_metrics_must_define_exactly_the_canonical_threshold_set(self) -> None:
        policy = _load_policy()
        broken = json.loads(json.dumps(policy))
        del broken["measurements"]["api_runtime"]["metrics"]["http_request_failed_rate"]
        with self.assertRaisesRegex(evidence.ApiRuntimeEvidenceError, "metrics"):
            evidence.api_runtime_section(broken)

    def test_metric_max_must_be_finite_and_non_negative(self) -> None:
        policy = _load_policy()
        broken = json.loads(json.dumps(policy))
        broken["measurements"]["api_runtime"]["metrics"]["http_request_failed_rate"]["max"] = -1
        with self.assertRaisesRegex(evidence.ApiRuntimeEvidenceError, "max"):
            evidence.api_runtime_section(broken)


class PrometheusParsingTests(unittest.TestCase):
    def test_parses_counters_and_histograms(self) -> None:
        text = _prometheus_text(search_count=5, search_sum_seconds=1.0, http_search_requests=5)
        families = evidence.parse_prometheus_text(text)
        self.assertIn("search_repository_duration_seconds_bucket", families)
        self.assertEqual(
            evidence.sum_counter(families, "http_requests_total", {"path": "/search"}), 5.0
        )

    def test_rejects_unparseable_lines(self) -> None:
        with self.assertRaisesRegex(evidence.ApiRuntimeEvidenceError, "cannot parse"):
            evidence.parse_prometheus_text("this is not a metric line at all {{{")

    def test_sum_counter_raises_when_metric_absent(self) -> None:
        families = evidence.parse_prometheus_text("some_other_metric 1\n")
        with self.assertRaisesRegex(evidence.ApiRuntimeEvidenceError, "http_requests_total"):
            evidence.sum_counter(families, "http_requests_total")

    def test_sum_counter_raises_when_label_filter_matches_nothing(self) -> None:
        families = evidence.parse_prometheus_text(
            'http_requests_total{method="GET",path="/health/live",status="200"} 3\n'
        )
        with self.assertRaisesRegex(evidence.ApiRuntimeEvidenceError, "no samples matching"):
            evidence.sum_counter(families, "http_requests_total", {"path": "/search"})

    def test_histogram_snapshot_requires_the_inf_bucket(self) -> None:
        families = evidence.parse_prometheus_text(
            'search_repository_duration_seconds_bucket{le="0.1"} 1\n'
            "search_repository_duration_seconds_sum 0.05\n"
            "search_repository_duration_seconds_count 1\n"
        )
        with self.assertRaisesRegex(evidence.ApiRuntimeEvidenceError, r"\+Inf"):
            evidence.histogram_snapshot(families, "search_repository_duration_seconds")

    def test_histogram_delta_detects_counter_reset(self) -> None:
        before = evidence.parse_prometheus_text(
            _prometheus_text(search_count=10, search_sum_seconds=1.0, http_search_requests=10)
        )
        after = evidence.parse_prometheus_text(
            _prometheus_text(search_count=3, search_sum_seconds=0.3, http_search_requests=3)
        )
        before_hist = evidence.histogram_snapshot(before, "search_repository_duration_seconds")
        after_hist = evidence.histogram_snapshot(after, "search_repository_duration_seconds")
        with self.assertRaisesRegex(evidence.ApiRuntimeEvidenceError, "counter reset"):
            evidence.histogram_delta(before_hist, after_hist)

    def test_histogram_quantile_linear_interpolation(self) -> None:
        # 10 observations spread evenly across two buckets: exercise the
        # interpolation path (not just the exact-boundary shortcuts).
        text = (
            "# TYPE x histogram\n"
            'x_bucket{le="0.1"} 5\n'
            'x_bucket{le="0.2"} 10\n'
            'x_bucket{le="+Inf"} 10\n'
            "x_sum 1.0\n"
            "x_count 10\n"
        )
        families = evidence.parse_prometheus_text(text)
        snapshot = evidence.histogram_snapshot(families, "x")
        median_ms = evidence.histogram_quantile_ms(snapshot, 0.5)
        self.assertAlmostEqual(median_ms, 100.0, places=6)

    def test_histogram_quantile_in_overflow_bucket_fails_closed(self) -> None:
        text = (
            'x_bucket{le="0.1"} 0\n'
            'x_bucket{le="+Inf"} 5\n'
            "x_sum 5.0\n"
            "x_count 5\n"
        )
        families = evidence.parse_prometheus_text(text)
        snapshot = evidence.histogram_snapshot(families, "x")
        with self.assertRaisesRegex(evidence.ApiRuntimeEvidenceError, r"\+Inf"):
            evidence.histogram_quantile_ms(snapshot, 0.99)


class K6SummaryParsingTests(unittest.TestCase):
    def test_extracts_http_metrics(self) -> None:
        summary = _k6_summary(p95=123.0, p99=456.0, failed_rate=0.002, total_requests=42)
        result = evidence.extract_http_metrics(summary)
        self.assertEqual(result["p95_ms"], 123.0)
        self.assertEqual(result["p99_ms"], 456.0)
        self.assertEqual(result["failed_rate"], 0.002)
        self.assertEqual(result["total_requests"], 42)

    def test_rejects_zero_requests(self) -> None:
        summary = _k6_summary(total_requests=0)
        with self.assertRaisesRegex(evidence.ApiRuntimeEvidenceError, "zero HTTP requests"):
            evidence.extract_http_metrics(summary)

    def test_missing_scenario_block_is_rejected(self) -> None:
        summary = _k6_summary()
        del summary["weltgewebe_scenario"]
        with self.assertRaisesRegex(evidence.ApiRuntimeEvidenceError, "weltgewebe_scenario"):
            evidence.extract_declared_scenario(summary)


class K6ScriptContractTests(unittest.TestCase):
    def test_post_deploy_ready_503_is_an_http_failure(self) -> None:
        source = (ROOT / "scripts/performance/api_runtime_k6.js").read_text(encoding="utf-8")
        self.assertIn("http.setResponseCallback(http.expectedStatuses(200));", source)
        self.assertIn("READY_RESPONSE_CALLBACK = http.expectedStatuses(200)", source)
        self.assertNotIn("expectedStatuses(200, 503)", source)
        self.assertIn("responseCallback: READY_RESPONSE_CALLBACK", source)
        self.assertIn("'ready 200': (r) => r.status === 200", source)
        self.assertIn("'search 200': (r) => r.status === 200", source)

        self.assertIn("API_RUNTIME_DATASET_PROFILE is required", source)
        self.assertIn("API_RUNTIME_SEARCH_QUERY is required", source)
        self.assertIn("API_RUNTIME_DATASET_MANIFEST_SHA256", source)
        self.assertIn("weltgewebe_dataset_manifest_sha256", source)
        self.assertIn("search_query: SEARCH_QUERY", source)
        self.assertIn("API_RUNTIME_K6_IMAGE must be an exact @sha256 image reference", source)
        self.assertIn("weltgewebe_search_query", source)
        self.assertIn("API_RUNTIME_RUN_ID has an invalid format", source)
        self.assertIn("weltgewebe_run_id", source)
        self.assertIn("weltgewebe_load_started_at_unix_ms", source)
        self.assertIn("weltgewebe_load_finished_at_unix_ms", source)
        self.assertIn("data?.state?.testRunDurationMs", source)
        self.assertIn("weltgewebe_k6_image", source)
        self.assertNotIn("API_RUNTIME_DATASET_PROFILE || 'domain-scale-ci'", source)
        self.assertNotRegex(source, r"API_RUNTIME_SEARCH_QUERY\s*\|\|")

    def test_real_k6_module_classifies_503_as_an_http_failure(self) -> None:
        completed = subprocess.run(
            [
                "node",
                "--experimental-vm-modules",
                str(ROOT / "scripts/ci/tests/api_runtime_k6_classification.test.mjs"),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)


class AssembleReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = _load_policy()

    def test_passing_run_produces_a_bound_pass_report(self) -> None:
        k6_summary = _k6_summary(p50=15.0, p95=40.0, p99=60.0, failed_rate=0.0, total_requests=300)
        before = _prometheus_text(search_count=0, search_sum_seconds=0.0, http_search_requests=0)
        after = _prometheus_text(search_count=100, search_sum_seconds=1.0, http_search_requests=100)

        report = evidence.assemble_report(
            policy=self.policy,
            k6_summary=k6_summary,
            metrics_before_text=before,
            metrics_after_text=after,
            resource_receipt=_resource_receipt(),
            dataset_binding=_dataset_binding(),
            runtime_binding=_runtime_binding(),
            database_connection_receipt=_database_connection_receipt(),
            git_head=FAKE_GIT_HEAD,
            policy_sha256=FAKE_POLICY_SHA256,
        )

        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["failures"], [])
        self.assertEqual(report["revision"]["git_head"], FAKE_GIT_HEAD)
        self.assertEqual(report["revision"]["policy_sha256"], FAKE_POLICY_SHA256)
        self.assertEqual(report["scenario"], _canonical_scenario())
        self.assertEqual(
            report["metrics"]["load_window"],
            {
                "run_id": FAKE_RUN_ID,
                "started_at_unix_ms": FAKE_LOAD_STARTED_AT_UNIX_MS,
                "finished_at_unix_ms": FAKE_LOAD_FINISHED_AT_UNIX_MS,
            },
        )
        self.assertEqual(
            report["metrics"]["database"]["query_count"],
            {
                "expected_from_http_requests_total": 100,
                "observed_from_search_repository_duration_seconds_count": 100,
            },
        )
        self.assertEqual(
            report["metrics"]["resources"]["postgres_connections"],
            _database_connection_receipt(),
        )
        self.assertEqual(report["metrics"]["resources"]["api_replica"], _resource_receipt())

    def test_folds_in_a_valid_resource_receipt(self) -> None:
        k6_summary = _k6_summary()
        before = _prometheus_text(search_count=0, search_sum_seconds=0.0, http_search_requests=0)
        after = _prometheus_text(search_count=10, search_sum_seconds=0.1, http_search_requests=10)
        receipt = _resource_receipt()
        report = evidence.assemble_report(
            policy=self.policy,
            k6_summary=k6_summary,
            metrics_before_text=before,
            metrics_after_text=after,
            resource_receipt=receipt,
            dataset_binding=_dataset_binding(),
            runtime_binding=_runtime_binding(),
            database_connection_receipt=_database_connection_receipt(),
            git_head=FAKE_GIT_HEAD,
            policy_sha256=FAKE_POLICY_SHA256,
        )
        self.assertEqual(report["metrics"]["resources"]["api_replica"], receipt)

    def test_threshold_breach_produces_a_soft_fail_with_explicit_failures(self) -> None:
        k6_summary = _k6_summary(p95=999.0, p99=999.0, failed_rate=0.5, total_requests=300)
        before = _prometheus_text(search_count=0, search_sum_seconds=0.0, http_search_requests=0)
        after = _prometheus_text(search_count=10, search_sum_seconds=0.1, http_search_requests=10)

        report = evidence.assemble_report(
            policy=self.policy,
            k6_summary=k6_summary,
            metrics_before_text=before,
            metrics_after_text=after,
            resource_receipt=_resource_receipt(),
            dataset_binding=_dataset_binding(),
            runtime_binding=_runtime_binding(),
            database_connection_receipt=_database_connection_receipt(),
            git_head=FAKE_GIT_HEAD,
            policy_sha256=FAKE_POLICY_SHA256,
        )
        self.assertEqual(report["status"], "fail")
        self.assertEqual(len(report["failures"]), 3)

    def test_missing_resource_receipt_is_a_hard_failure(self) -> None:
        k6_summary = _k6_summary()
        before = _prometheus_text(search_count=0, search_sum_seconds=0.0, http_search_requests=0)
        after = _prometheus_text(search_count=10, search_sum_seconds=0.1, http_search_requests=10)
        with self.assertRaisesRegex(evidence.ApiRuntimeEvidenceError, "resource_receipt is required"):
            evidence.assemble_report(
                policy=self.policy,
                k6_summary=k6_summary,
                metrics_before_text=before,
                metrics_after_text=after,
                resource_receipt=None,
                dataset_binding=_dataset_binding(),
                runtime_binding=_runtime_binding(),
                database_connection_receipt=_database_connection_receipt(),
                git_head=FAKE_GIT_HEAD,
                policy_sha256=FAKE_POLICY_SHA256,
            )

    def test_scenario_mismatch_is_a_hard_failure(self) -> None:
        mismatched_scenario = _canonical_scenario()
        mismatched_scenario["virtual_users"] = 50
        k6_summary = _k6_summary(scenario=mismatched_scenario)
        before = _prometheus_text(search_count=0, search_sum_seconds=0.0, http_search_requests=0)
        after = _prometheus_text(search_count=10, search_sum_seconds=0.1, http_search_requests=10)

        with self.assertRaisesRegex(evidence.ApiRuntimeEvidenceError, "scenario"):
            evidence.assemble_report(
                policy=self.policy,
                k6_summary=k6_summary,
                metrics_before_text=before,
                metrics_after_text=after,
                resource_receipt=_resource_receipt(),
                dataset_binding=_dataset_binding(),
                runtime_binding=_runtime_binding(),
                database_connection_receipt=_database_connection_receipt(),
                git_head=FAKE_GIT_HEAD,
                policy_sha256=FAKE_POLICY_SHA256,
            )

    def test_contradictory_query_count_is_a_hard_failure(self) -> None:
        k6_summary = _k6_summary()
        before = _prometheus_text(search_count=0, search_sum_seconds=0.0, http_search_requests=0)
        # http_requests_total says 40 requests reached /search, but the
        # repository histogram only observed 10 queries: a contradiction that
        # must fail closed rather than silently averaging away.
        after = _prometheus_text(search_count=10, search_sum_seconds=0.1, http_search_requests=40)

        with self.assertRaisesRegex(evidence.ApiRuntimeEvidenceError, "contradictory evidence"):
            evidence.assemble_report(
                policy=self.policy,
                k6_summary=k6_summary,
                metrics_before_text=before,
                metrics_after_text=after,
                resource_receipt=_resource_receipt(),
                dataset_binding=_dataset_binding(),
                runtime_binding=_runtime_binding(),
                database_connection_receipt=_database_connection_receipt(),
                git_head=FAKE_GIT_HEAD,
                policy_sha256=FAKE_POLICY_SHA256,
            )

    def test_zero_observed_queries_is_a_hard_failure(self) -> None:
        k6_summary = _k6_summary()
        before = _prometheus_text(search_count=0, search_sum_seconds=0.0, http_search_requests=0)
        after = _prometheus_text(search_count=0, search_sum_seconds=0.0, http_search_requests=0)

        with self.assertRaisesRegex(evidence.ApiRuntimeEvidenceError, "no PostgreSQL"):
            evidence.assemble_report(
                policy=self.policy,
                k6_summary=k6_summary,
                metrics_before_text=before,
                metrics_after_text=after,
                resource_receipt=_resource_receipt(),
                dataset_binding=_dataset_binding(),
                runtime_binding=_runtime_binding(),
                database_connection_receipt=_database_connection_receipt(),
                git_head=FAKE_GIT_HEAD,
                policy_sha256=FAKE_POLICY_SHA256,
            )

    def test_rejects_a_malformed_git_head(self) -> None:
        k6_summary = _k6_summary()
        before = _prometheus_text(search_count=0, search_sum_seconds=0.0, http_search_requests=0)
        after = _prometheus_text(search_count=10, search_sum_seconds=0.1, http_search_requests=10)
        with self.assertRaisesRegex(evidence.ApiRuntimeEvidenceError, "git_head"):
            evidence.assemble_report(
                policy=self.policy,
                k6_summary=k6_summary,
                metrics_before_text=before,
                metrics_after_text=after,
                resource_receipt=_resource_receipt(),
                dataset_binding=_dataset_binding(),
                runtime_binding=_runtime_binding(),
                database_connection_receipt=_database_connection_receipt(),
                git_head="not-a-sha",
                policy_sha256=FAKE_POLICY_SHA256,
            )

    def test_measured_api_commit_must_match_the_checkout_revision(self) -> None:
        before = _prometheus_text(
            search_count=0,
            search_sum_seconds=0.0,
            http_search_requests=0,
            git_commit="9" * 40,
        )
        after = _prometheus_text(
            search_count=10,
            search_sum_seconds=0.1,
            http_search_requests=10,
            git_commit="9" * 40,
        )
        with self.assertRaisesRegex(evidence.ApiRuntimeEvidenceError, "does not match git HEAD"):
            evidence.assemble_report(
                policy=self.policy,
                k6_summary=_k6_summary(),
                metrics_before_text=before,
                metrics_after_text=after,
                resource_receipt=_resource_receipt(),
                dataset_binding=_dataset_binding(),
                runtime_binding=_runtime_binding(),
                database_connection_receipt=_database_connection_receipt(),
                git_head=FAKE_GIT_HEAD,
                policy_sha256=FAKE_POLICY_SHA256,
            )

    def test_measured_api_commit_must_not_change_during_the_run(self) -> None:
        before = _prometheus_text(
            search_count=0, search_sum_seconds=0.0, http_search_requests=0
        )
        after = _prometheus_text(
            search_count=10,
            search_sum_seconds=0.1,
            http_search_requests=10,
            git_commit="9" * 40,
        )
        with self.assertRaisesRegex(evidence.ApiRuntimeEvidenceError, "changed between"):
            evidence.assemble_report(
                policy=self.policy,
                k6_summary=_k6_summary(),
                metrics_before_text=before,
                metrics_after_text=after,
                resource_receipt=_resource_receipt(),
                dataset_binding=_dataset_binding(),
                runtime_binding=_runtime_binding(),
                database_connection_receipt=_database_connection_receipt(),
                git_head=FAKE_GIT_HEAD,
                policy_sha256=FAKE_POLICY_SHA256,
            )

    def test_k6_dataset_digest_must_match_the_validated_manifest(self) -> None:
        before = _prometheus_text(
            search_count=0, search_sum_seconds=0.0, http_search_requests=0
        )
        after = _prometheus_text(
            search_count=10, search_sum_seconds=0.1, http_search_requests=10
        )
        with self.assertRaisesRegex(evidence.ApiRuntimeEvidenceError, "validated fixture manifest"):
            evidence.assemble_report(
                policy=self.policy,
                k6_summary=_k6_summary(dataset_manifest_sha256="9" * 64),
                metrics_before_text=before,
                metrics_after_text=after,
                resource_receipt=_resource_receipt(),
                dataset_binding=_dataset_binding(),
                runtime_binding=_runtime_binding(),
                database_connection_receipt=_database_connection_receipt(),
                git_head=FAKE_GIT_HEAD,
                policy_sha256=FAKE_POLICY_SHA256,
            )


    def test_missing_runtime_binding_is_a_hard_failure(self) -> None:
        with self.assertRaisesRegex(evidence.ApiRuntimeEvidenceError, "runtime_binding is required"):
            evidence.assemble_report(
                policy=self.policy,
                k6_summary=_k6_summary(),
                metrics_before_text=_prometheus_text(search_count=0, search_sum_seconds=0.0, http_search_requests=0),
                metrics_after_text=_prometheus_text(search_count=10, search_sum_seconds=0.1, http_search_requests=10),
                resource_receipt=_resource_receipt(),
                dataset_binding=_dataset_binding(),
                runtime_binding=None,
                database_connection_receipt=_database_connection_receipt(),
                git_head=FAKE_GIT_HEAD,
                policy_sha256=FAKE_POLICY_SHA256,
            )

    def test_runtime_binding_manifest_must_match_the_fixture(self) -> None:
        with self.assertRaisesRegex(evidence.ApiRuntimeEvidenceError, "live binding manifest digest"):
            evidence.assemble_report(
                policy=self.policy,
                k6_summary=_k6_summary(),
                metrics_before_text=_prometheus_text(search_count=0, search_sum_seconds=0.0, http_search_requests=0),
                metrics_after_text=_prometheus_text(search_count=10, search_sum_seconds=0.1, http_search_requests=10),
                resource_receipt=_resource_receipt(),
                dataset_binding=_dataset_binding(),
                runtime_binding=_runtime_binding(manifest_sha256="9" * 64),
                database_connection_receipt=_database_connection_receipt(),
                git_head=FAKE_GIT_HEAD,
                policy_sha256=FAKE_POLICY_SHA256,
            )

    def test_k6_query_must_match_live_search_binding(self) -> None:
        with self.assertRaisesRegex(evidence.ApiRuntimeEvidenceError, "k6 search query"):
            evidence.assemble_report(
                policy=self.policy,
                k6_summary=_k6_summary(search_query="Other"),
                metrics_before_text=_prometheus_text(search_count=0, search_sum_seconds=0.0, http_search_requests=0),
                metrics_after_text=_prometheus_text(search_count=10, search_sum_seconds=0.1, http_search_requests=10),
                resource_receipt=_resource_receipt(),
                dataset_binding=_dataset_binding(),
                runtime_binding=_runtime_binding(),
                database_connection_receipt=_database_connection_receipt(),
                git_head=FAKE_GIT_HEAD,
                policy_sha256=FAKE_POLICY_SHA256,
            )

    def test_k6_image_must_match_live_runtime_binding(self) -> None:
        with self.assertRaisesRegex(evidence.ApiRuntimeEvidenceError, "k6 image identity"):
            evidence.assemble_report(
                policy=self.policy,
                k6_summary=_k6_summary(k6_image="grafana/k6@sha256:" + ("9" * 64)),
                metrics_before_text=_prometheus_text(search_count=0, search_sum_seconds=0.0, http_search_requests=0),
                metrics_after_text=_prometheus_text(search_count=10, search_sum_seconds=0.1, http_search_requests=10),
                resource_receipt=_resource_receipt(),
                dataset_binding=_dataset_binding(),
                runtime_binding=_runtime_binding(),
                database_connection_receipt=_database_connection_receipt(),
                git_head=FAKE_GIT_HEAD,
                policy_sha256=FAKE_POLICY_SHA256,
            )

    def test_missing_database_connection_receipt_is_a_hard_failure(self) -> None:
        with self.assertRaisesRegex(evidence.ApiRuntimeEvidenceError, "database_connection_receipt is required"):
            evidence.assemble_report(
                policy=self.policy,
                k6_summary=_k6_summary(),
                metrics_before_text=_prometheus_text(search_count=0, search_sum_seconds=0.0, http_search_requests=0),
                metrics_after_text=_prometheus_text(search_count=10, search_sum_seconds=0.1, http_search_requests=10),
                resource_receipt=_resource_receipt(),
                dataset_binding=_dataset_binding(),
                runtime_binding=_runtime_binding(),
                database_connection_receipt=None,
                git_head=FAKE_GIT_HEAD,
                policy_sha256=FAKE_POLICY_SHA256,
            )

    def test_k6_run_id_must_match_live_runtime_binding(self) -> None:
        with self.assertRaisesRegex(evidence.ApiRuntimeEvidenceError, "k6 run_id"):
            evidence.assemble_report(
                policy=self.policy,
                k6_summary=_k6_summary(run_id="other-run"),
                metrics_before_text=_prometheus_text(search_count=0, search_sum_seconds=0.0, http_search_requests=0),
                metrics_after_text=_prometheus_text(search_count=10, search_sum_seconds=0.1, http_search_requests=10),
                resource_receipt=_resource_receipt(),
                dataset_binding=_dataset_binding(),
                runtime_binding=_runtime_binding(),
                database_connection_receipt=_database_connection_receipt(),
                git_head=FAKE_GIT_HEAD,
                policy_sha256=FAKE_POLICY_SHA256,
            )

    def test_api_resource_run_id_must_match_live_runtime_binding(self) -> None:
        with self.assertRaisesRegex(evidence.ApiRuntimeEvidenceError, "API resource receipt run_id"):
            evidence.assemble_report(
                policy=self.policy,
                k6_summary=_k6_summary(),
                metrics_before_text=_prometheus_text(search_count=0, search_sum_seconds=0.0, http_search_requests=0),
                metrics_after_text=_prometheus_text(search_count=10, search_sum_seconds=0.1, http_search_requests=10),
                resource_receipt=_resource_receipt(run_id="old-run"),
                dataset_binding=_dataset_binding(),
                runtime_binding=_runtime_binding(),
                database_connection_receipt=_database_connection_receipt(),
                git_head=FAKE_GIT_HEAD,
                policy_sha256=FAKE_POLICY_SHA256,
            )

    def test_database_resource_run_id_must_match_live_runtime_binding(self) -> None:
        with self.assertRaisesRegex(evidence.ApiRuntimeEvidenceError, "PostgreSQL connection receipt run_id"):
            evidence.assemble_report(
                policy=self.policy,
                k6_summary=_k6_summary(),
                metrics_before_text=_prometheus_text(search_count=0, search_sum_seconds=0.0, http_search_requests=0),
                metrics_after_text=_prometheus_text(search_count=10, search_sum_seconds=0.1, http_search_requests=10),
                resource_receipt=_resource_receipt(),
                dataset_binding=_dataset_binding(),
                runtime_binding=_runtime_binding(),
                database_connection_receipt=_database_connection_receipt(run_id="old-run"),
                git_head=FAKE_GIT_HEAD,
                policy_sha256=FAKE_POLICY_SHA256,
            )

    def test_k6_load_window_must_be_strictly_positive(self) -> None:
        with self.assertRaisesRegex(
            evidence.ApiRuntimeEvidenceError, "k6 load wall-clock interval is invalid"
        ):
            evidence.assemble_report(
                policy=self.policy,
                k6_summary=_k6_summary(
                    load_finished_at_unix_ms=FAKE_LOAD_STARTED_AT_UNIX_MS
                ),
                metrics_before_text=_prometheus_text(
                    search_count=0, search_sum_seconds=0.0, http_search_requests=0
                ),
                metrics_after_text=_prometheus_text(
                    search_count=10, search_sum_seconds=0.1, http_search_requests=10
                ),
                resource_receipt=_resource_receipt(),
                dataset_binding=_dataset_binding(),
                runtime_binding=_runtime_binding(),
                database_connection_receipt=_database_connection_receipt(),
                git_head=FAKE_GIT_HEAD,
                policy_sha256=FAKE_POLICY_SHA256,
            )

    def test_api_resource_sampler_must_cover_complete_k6_window(self) -> None:
        with self.assertRaisesRegex(
            evidence.ApiRuntimeEvidenceError,
            "API resource sampler interval does not cover",
        ):
            evidence.assemble_report(
                policy=self.policy,
                k6_summary=_k6_summary(),
                metrics_before_text=_prometheus_text(
                    search_count=0, search_sum_seconds=0.0, http_search_requests=0
                ),
                metrics_after_text=_prometheus_text(
                    search_count=10, search_sum_seconds=0.1, http_search_requests=10
                ),
                resource_receipt=_resource_receipt(
                    started_at_unix_ms=FAKE_LOAD_STARTED_AT_UNIX_MS + 1
                ),
                dataset_binding=_dataset_binding(),
                runtime_binding=_runtime_binding(),
                database_connection_receipt=_database_connection_receipt(),
                git_head=FAKE_GIT_HEAD,
                policy_sha256=FAKE_POLICY_SHA256,
            )

    def test_database_sampler_must_cover_complete_k6_window(self) -> None:
        with self.assertRaisesRegex(
            evidence.ApiRuntimeEvidenceError,
            "PostgreSQL connection sampler interval does not cover",
        ):
            evidence.assemble_report(
                policy=self.policy,
                k6_summary=_k6_summary(),
                metrics_before_text=_prometheus_text(
                    search_count=0, search_sum_seconds=0.0, http_search_requests=0
                ),
                metrics_after_text=_prometheus_text(
                    search_count=10, search_sum_seconds=0.1, http_search_requests=10
                ),
                resource_receipt=_resource_receipt(),
                dataset_binding=_dataset_binding(),
                runtime_binding=_runtime_binding(),
                database_connection_receipt=_database_connection_receipt(
                    finished_at_unix_ms=FAKE_LOAD_FINISHED_AT_UNIX_MS - 1
                ),
                git_head=FAKE_GIT_HEAD,
                policy_sha256=FAKE_POLICY_SHA256,
            )

    def test_resource_containers_must_match_live_runtime_binding(self) -> None:
        with self.assertRaisesRegex(evidence.ApiRuntimeEvidenceError, "API resource receipt container"):
            evidence.assemble_report(
                policy=self.policy,
                k6_summary=_k6_summary(),
                metrics_before_text=_prometheus_text(search_count=0, search_sum_seconds=0.0, http_search_requests=0),
                metrics_after_text=_prometheus_text(search_count=10, search_sum_seconds=0.1, http_search_requests=10),
                resource_receipt=_resource_receipt(container_name="other-api"),
                dataset_binding=_dataset_binding(),
                runtime_binding=_runtime_binding(),
                database_connection_receipt=_database_connection_receipt(),
                git_head=FAKE_GIT_HEAD,
                policy_sha256=FAKE_POLICY_SHA256,
            )

    def test_live_binding_allows_complete_index_larger_than_query_candidate_limit(self) -> None:
        receipt = _runtime_binding()
        self.assertGreater(
            receipt["search"]["active_projection_count"],
            receipt["search"]["candidate_limit_contract"],
        )
        normalized = evidence.live_binding.validate_receipt(receipt)
        self.assertEqual(
            normalized["search"]["active_projection_count"],
            receipt["search"]["active_projection_count"],
        )

    def test_live_binding_rejects_incomplete_active_generation(self) -> None:
        receipt = _runtime_binding()
        receipt["search"]["completed_nodes"] -= 1
        with self.assertRaisesRegex(evidence.live_binding.LiveBindingError, "incomplete"):
            evidence.live_binding.validate_receipt(receipt)

    def test_projection_identity_redacts_hidden_fixture_values(self) -> None:
        fixture = {"id": "node-hidden", "kind": "Angebot", "title": "Geheimer Titel"}
        projection = {
            "id": "node-hidden",
            "kind": "[nicht öffentlich]",
            "title": "[nicht öffentlich]",
            "search_visibility": "hidden",
            "owner_account_id": None,
        }
        self.assertEqual(
            evidence.live_binding._expected_projection_identity(projection, fixture),
            {
                "id": "node-hidden",
                "kind": "[nicht öffentlich]",
                "title": "[nicht öffentlich]",
                "search_visibility": "hidden",
            },
        )

    def test_projection_identity_preserves_public_fixture_values(self) -> None:
        fixture = {"id": "node-public", "kind": "Projekt", "title": "Öffentlicher Titel"}
        projection = {
            "id": "node-public",
            "kind": "Projekt",
            "title": "Öffentlicher Titel",
            "search_visibility": "public",
            "owner_account_id": None,
        }
        self.assertEqual(
            evidence.live_binding._expected_projection_identity(projection, fixture),
            {
                "id": "node-public",
                "kind": "Projekt",
                "title": "Öffentlicher Titel",
                "search_visibility": "public",
            },
        )


class MissingAndInvalidInputTests(unittest.TestCase):
    def test_missing_k6_summary_file_fails_closed(self) -> None:
        with self.assertRaisesRegex(evidence.ApiRuntimeEvidenceError, "cannot read"):
            evidence._read_text(Path("/nonexistent/does-not-exist.json"), "metrics-before snapshot")

    def test_missing_resource_receipt_file_fails_closed(self) -> None:
        with self.assertRaisesRegex(evidence.ApiRuntimeEvidenceError, "cannot read"):
            evidence.load_resource_receipt(Path("/nonexistent/receipt.json"))

    def test_resource_receipt_rejects_wrong_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 3,
                        "contract": "something-else",
                        "run_id": FAKE_RUN_ID,
                        "container_name": "x",
                        "started_at_unix_ms": FAKE_SAMPLE_STARTED_AT_UNIX_MS,
                        "finished_at_unix_ms": FAKE_SAMPLE_FINISHED_AT_UNIX_MS,
                        "sample_count": 1,
                        "peaks": {"cpu_percent": 1.0, "memory_bytes": 1},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(evidence.ApiRuntimeEvidenceError, "contract"):
                evidence.load_resource_receipt(path)

    def test_resource_receipt_rejects_negative_memory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 3,
                        "contract": evidence.RESOURCE_RECEIPT_CONTRACT,
                        "run_id": FAKE_RUN_ID,
                        "container_name": "x",
                        "started_at_unix_ms": FAKE_SAMPLE_STARTED_AT_UNIX_MS,
                        "finished_at_unix_ms": FAKE_SAMPLE_FINISHED_AT_UNIX_MS,
                        "sample_count": 1,
                        "peaks": {"cpu_percent": 1.0, "memory_bytes": -1},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(evidence.ApiRuntimeEvidenceError, "memory_bytes"):
                evidence.load_resource_receipt(path)

    def test_database_connection_receipt_rejects_inconsistent_maximum(self) -> None:
        receipt = _database_connection_receipt()
        receipt["max_connections"] += 1
        with self.assertRaisesRegex(evidence.ApiRuntimeEvidenceError, "max_connections"):
            evidence.validate_normalized_database_connection_receipt(receipt)

    def test_database_connection_receipt_rejects_invalid_sample(self) -> None:
        receipt = _database_connection_receipt()
        receipt["samples"][1] = -1
        with self.assertRaisesRegex(evidence.ApiRuntimeEvidenceError, "invalid sample"):
            evidence.validate_normalized_database_connection_receipt(receipt)


class RevisionBindingTests(unittest.TestCase):
    def _report(self, *, k6_summary=None) -> dict:
        return evidence.assemble_report(
            policy=_load_policy(),
            k6_summary=k6_summary if k6_summary is not None else _k6_summary(),
            metrics_before_text=_prometheus_text(
                search_count=0, search_sum_seconds=0.0, http_search_requests=0
            ),
            metrics_after_text=_prometheus_text(
                search_count=10, search_sum_seconds=0.1, http_search_requests=10
            ),
            resource_receipt=_resource_receipt(),
            dataset_binding=_dataset_binding(),
            runtime_binding=_runtime_binding(),
            database_connection_receipt=_database_connection_receipt(),
            git_head=FAKE_GIT_HEAD,
            policy_sha256=FAKE_POLICY_SHA256,
        )

    def _verify(self, report: dict) -> None:
        evidence.verify_report(
            report,
            policy=_load_policy(),
            expected_git_head=FAKE_GIT_HEAD,
            expected_policy_sha256=FAKE_POLICY_SHA256,
            expected_dataset_binding=_dataset_binding(),
        )

    def test_verify_accepts_a_matching_complete_report(self) -> None:
        self._verify(self._report())

    def test_verify_rejects_a_threshold_failing_report(self) -> None:
        report = self._report(
            k6_summary=_k6_summary(p95=999.0, p99=999.0, failed_rate=0.5)
        )
        with self.assertRaisesRegex(evidence.ApiRuntimeEvidenceError, "does not pass"):
            self._verify(report)

    def test_verify_rejects_a_stale_git_head(self) -> None:
        report = self._report()
        report["revision"]["git_head"] = "c" * 40
        with self.assertRaisesRegex(evidence.ApiRuntimeEvidenceError, "git HEAD"):
            self._verify(report)

    def test_verify_rejects_a_stale_measured_api_commit(self) -> None:
        report = self._report()
        report["revision"]["measured_api_commit"] = "c" * 40
        with self.assertRaisesRegex(evidence.ApiRuntimeEvidenceError, "measured API commit"):
            self._verify(report)

    def test_verify_rejects_a_stale_policy_revision(self) -> None:
        report = self._report()
        report["revision"]["policy_sha256"] = "d" * 64
        with self.assertRaisesRegex(evidence.ApiRuntimeEvidenceError, "performance.v1.json revision"):
            self._verify(report)

    def test_verify_rejects_a_truncated_pass_artifact(self) -> None:
        report = {
            "schema_version": evidence.SCHEMA_VERSION,
            "contract_id": evidence.CONTRACT_ID,
            "status": "pass",
            "revision": {
                "git_head": FAKE_GIT_HEAD,
                "measured_api_commit": FAKE_GIT_HEAD,
                "policy_sha256": FAKE_POLICY_SHA256,
            },
        }
        with self.assertRaisesRegex(evidence.ApiRuntimeEvidenceError, "complete api_runtime"):
            self._verify(report)

    def test_verify_recomputes_thresholds_instead_of_trusting_status(self) -> None:
        report = self._report()
        report["metrics"]["http"]["p95_ms"] = 500.0
        report["metrics"]["http"]["p99_ms"] = 600.0
        with self.assertRaisesRegex(evidence.ApiRuntimeEvidenceError, "failure list"):
            self._verify(report)

    def test_verify_rejects_tampered_resource_load_window_binding(self) -> None:
        report = self._report()
        report["metrics"]["resources"]["api_replica"]["started_at_unix_ms"] = (
            FAKE_LOAD_STARTED_AT_UNIX_MS + 1
        )
        with self.assertRaisesRegex(
            evidence.ApiRuntimeEvidenceError,
            "report API resource sampler interval does not cover",
        ):
            self._verify(report)

    def test_verify_rejects_a_different_dataset_binding(self) -> None:
        report = self._report()
        report["dataset"]["manifest_sha256"] = "9" * 64
        with self.assertRaisesRegex(evidence.ApiRuntimeEvidenceError, "dataset binding"):
            self._verify(report)

    def test_git_head_resolves_a_real_sha_in_this_checkout(self) -> None:
        head = evidence.git_head(ROOT)
        self.assertRegex(head, r"^[0-9a-f]{40}$")


class RegressionFixtureCliTests(unittest.TestCase):
    """The controlled negative mode: prove the gate fails closed end-to-end
    through the actual CLI, not just the internal function."""

    def test_regression_fixture_makes_check_exit_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy_path, dataset_manifest = _write_tiny_dataset_contract(root)
            fixture_dir = root / "fixture"
            report_path = root / "report.json"

            generate = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(ROOT / "scripts/performance/api_runtime_evidence.py"),
                    "--policy",
                    str(policy_path),
                    "regression-fixture",
                    "--output-dir",
                    str(fixture_dir),
                    "--dataset-manifest",
                    str(dataset_manifest),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(generate.returncode, 0, generate.stderr)

            check = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(ROOT / "scripts/performance/api_runtime_evidence.py"),
                    "--policy",
                    str(policy_path),
                    "check",
                    "--k6-summary",
                    str(fixture_dir / "k6-summary.json"),
                    "--metrics-before",
                    str(fixture_dir / "metrics-before.prom"),
                    "--metrics-after",
                    str(fixture_dir / "metrics-after.prom"),
                    "--resource-receipt",
                    str(fixture_dir / "resource-receipt.json"),
                    "--dataset-manifest",
                    str(dataset_manifest),
                    "--runtime-binding",
                    str(fixture_dir / "runtime-binding.json"),
                    "--database-connections-receipt",
                    str(fixture_dir / "database-connections.json"),
                    "--report",
                    str(report_path),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(check.returncode, 2, check.stdout + check.stderr)
            report = json.loads(check.stdout)
            self.assertEqual(report["status"], "fail")
            self.assertTrue(report["failures"], "the regression fixture must record concrete failures")
            self.assertRegex(report["revision"]["git_head"], r"^[0-9a-f]{40}$")

            verify = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(ROOT / "scripts/performance/api_runtime_evidence.py"),
                    "--policy",
                    str(policy_path),
                    "verify",
                    "--report",
                    str(report_path),
                    "--dataset-manifest",
                    str(dataset_manifest),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(verify.returncode, 2, verify.stdout + verify.stderr)
            self.assertIn("does not pass", verify.stderr)

    def test_check_passes_cleanly_against_a_hand_built_healthy_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy_path, dataset_manifest = _write_tiny_dataset_contract(root)
            manifest_sha256 = evidence.sha256_file(dataset_manifest)
            current_head = evidence.git_head(ROOT)
            k6_path = root / "k6-summary.json"
            before_path = root / "before.prom"
            after_path = root / "after.prom"
            report_path = root / "report.json"
            resource_path = root / "resource-receipt.json"
            runtime_path = root / "runtime-binding.json"
            database_connections_path = root / "database-connections.json"

            k6_path.write_text(
                json.dumps(_k6_summary(dataset_manifest_sha256=manifest_sha256)),
                encoding="utf-8",
            )
            before_path.write_text(
                _prometheus_text(
                    search_count=0,
                    search_sum_seconds=0.0,
                    http_search_requests=0,
                    git_commit=current_head,
                ),
                encoding="utf-8",
            )
            after_path.write_text(
                _prometheus_text(
                    search_count=50,
                    search_sum_seconds=0.5,
                    http_search_requests=50,
                    git_commit=current_head,
                ),
                encoding="utf-8",
            )
            resource_path.write_text(json.dumps(_raw_resource_receipt()), encoding="utf-8")
            database_connections_path.write_text(
                json.dumps(_raw_database_connection_receipt()), encoding="utf-8"
            )
            runtime_path.write_text(
                json.dumps(
                    _runtime_binding(
                        git_head=current_head,
                        manifest_sha256=manifest_sha256,
                        node_count=4,
                    )
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(ROOT / "scripts/performance/api_runtime_evidence.py"),
                    "--policy",
                    str(policy_path),
                    "check",
                    "--k6-summary",
                    str(k6_path),
                    "--metrics-before",
                    str(before_path),
                    "--metrics-after",
                    str(after_path),
                    "--resource-receipt",
                    str(resource_path),
                    "--dataset-manifest",
                    str(dataset_manifest),
                    "--runtime-binding",
                    str(runtime_path),
                    "--database-connections-receipt",
                    str(database_connections_path),
                    "--report",
                    str(report_path),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["revision"]["measured_api_commit"], current_head)
            self.assertEqual(report["dataset"]["manifest_sha256"], manifest_sha256)


if __name__ == "__main__":
    unittest.main()
