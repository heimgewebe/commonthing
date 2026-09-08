from __future__ import annotations

import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from scripts.performance.domain_projection_load import Cq02EvidenceError, summarize

HEAD = "4940e0c335d9173f31c44e2be5701499149879e4"
REPO_ROOT = Path(__file__).resolve().parents[3]


def prometheus(*, after: bool, include_reload: bool = True) -> str:
    lines = [f'build_info{{commit="{HEAD}",version="test",build_timestamp="test"}} 1']
    if after:
        lines.append('domain_projection_events_total{event="refresh_check"} 100')
        lines.append('domain_projection_events_total{event="refresh_failure"} 0')
        lines.append('domain_projection_events_total{event="refresh_deferred"} 12')
        if include_reload:
            lines.extend(
                [
                    'domain_projection_events_total{event="reload_success"} 2',
                    'domain_projection_events_total{event="reload_failure"} 0',
                    'domain_projection_rows_loaded_total{kind="accounts"} 2',
                    'domain_projection_rows_loaded_total{kind="nodes"} 2000',
                    'domain_projection_rows_loaded_total{kind="edges"} 10000',
                ]
            )
    else:
        lines.append('domain_projection_events_total{event="refresh_check"} 10')
        lines.append('domain_projection_events_total{event="refresh_failure"} 0')
        lines.append('domain_projection_events_total{event="refresh_deferred"} 2')

    phases = ["read_gate_wait"]
    if include_reload:
        phases += ["reload", "write_gate_wait", "write_gate_hold"]
    for phase in phases:
        before_count = 1 if not after else 3
        before_sum = 0.001 if not after else 0.021
        if phase == "read_gate_wait":
            before_count = 10 if not after else 100
            before_sum = 0.001 if not after else 0.02
        buckets = [
            ("0.001", before_count // 2),
            ("0.01", before_count),
            ("0.1", before_count),
            ("+Inf", before_count),
        ]
        for le, value in buckets:
            lines.append(
                f'domain_projection_duration_seconds_bucket{{phase="{phase}",le="{le}"}} {value}'
            )
        lines.append(
            f'domain_projection_duration_seconds_count{{phase="{phase}"}} {before_count}'
        )
        lines.append(
            f'domain_projection_duration_seconds_sum{{phase="{phase}"}} {before_sum}'
        )
    return "\n".join(lines) + "\n"


def k6_summary(workload: str, *, dropped_iterations: int = 0) -> dict:
    metrics = {
        "cq02_read_duration_ms": {
            "values": {
                "count": 100,
                "p(50)": 4,
                "p(95)": 12,
                "p(99)": 20,
                "avg": 5,
                "max": 25,
            }
        },
        "cq02_read_requests_total": {"values": {"count": 100}},
        "cq02_read_failures_total": {"values": {"count": 0}},
        "cq02_503_total": {"values": {"count": 0}},
    }
    if dropped_iterations:
        metrics["dropped_iterations"] = {"values": {"count": dropped_iterations}}
    if workload == "mixed":
        metrics["cq02_write_duration_ms"] = {
            "values": {
                "count": 30,
                "p(50)": 20,
                "p(95)": 40,
                "p(99)": 60,
                "avg": 22,
                "max": 65,
            }
        }
        metrics["cq02_write_requests_total"] = {"values": {"count": 30}}
        metrics["cq02_write_successes_total"] = {"values": {"count": 30}}
        metrics["cq02_write_failures_total"] = {"values": {"count": 0}}
    return {
        "cq02": {
            "run_id": f"cq02-smoke-{workload}",
            "profile": "smoke",
            "workload": workload,
            "duration_seconds": 30,
            "read_vus": 10,
            "write_rate_per_second": 0.5 if workload == "mixed" else 0,
        },
        "metrics": metrics,
    }


class DomainProjectionLoadContractTests(unittest.TestCase):
    def test_workflow_watches_projection_runtime_paths(self) -> None:
        workflow = (REPO_ROOT / ".github/workflows/domain-projection-load.yml").read_text(
            encoding="utf-8"
        )
        for path in (
            "apps/api/src/state.rs",
            "apps/api/src/domain_db.rs",
            "apps/api/src/middleware/domain_projection.rs",
            "apps/api/src/routes/nodes.rs",
            "apps/api/src/telemetry/mod.rs",
        ):
            self.assertIn(
                f'- "{path}"',
                workflow,
                f"CQ-02 load evidence must run when critical projection path {path} changes",
            )
        self.assertIn("local workload_duration_seconds=30", workflow)
        self.assertIn(
            "local sampler_duration_seconds=$((workload_duration_seconds + 20))", workflow
        )
        self.assertEqual(workflow.count('--duration-seconds "${sampler_duration_seconds}"'), 2)
        self.assertIn('--env "CQ02_DURATION_SECONDS=${workload_duration_seconds}"', workflow)
        self.assertIn("--max-reloads 0", workflow)

    def make_args(
        self, root: Path, workload: str, *, include_reload: bool
    ) -> Namespace:
        files = {
            "dataset_manifest": {
                "profile": "smoke",
                "counts": {"nodes": 1000, "edges": 5000},
            },
            "k6_summary": k6_summary(workload),
        }
        for name, payload in files.items():
            (root / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")
        (root / "before.prom").write_text(
            prometheus(after=False, include_reload=include_reload), encoding="utf-8"
        )
        (root / "after.prom").write_text(
            prometheus(after=True, include_reload=include_reload), encoding="utf-8"
        )
        (root / "version-before.txt").write_text("10\n", encoding="utf-8")
        version_after = 40 if workload == "mixed" else 10
        (root / "version-after.txt").write_text(f"{version_after}\n", encoding="utf-8")
        (root / "log-before.txt").write_text("startup\n", encoding="utf-8")
        retry = (
            "Domain projection changed during reload; retrying stable snapshot\n"
            if include_reload
            else ""
        )
        (root / "log-after.txt").write_text("startup\n" + retry, encoding="utf-8")
        return Namespace(
            expected_head=HEAD,
            max_reloads=2 if include_reload else 0,
            dataset_manifest=root / "dataset_manifest.json",
            k6_summary=root / "k6_summary.json",
            metrics_before=root / "before.prom",
            metrics_after=root / "after.prom",
            version_before=root / "version-before.txt",
            version_after=root / "version-after.txt",
            api_log_before=root / "log-before.txt",
            api_log_after=root / "log-after.txt",
            resource_receipt=None,
            connection_receipt=None,
            output=root / "report.json",
        )

    def test_mixed_report_preserves_revision_counts_latencies_and_retries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = self.make_args(Path(tmp), "mixed", include_reload=True)
            report = summarize(args)
        self.assertEqual(report["revision"]["measured_api_commit"], HEAD)
        self.assertEqual(report["dataset"]["nodes"], 1000)
        self.assertEqual(report["requests"]["read"]["p99_ms"], 20)
        self.assertEqual(report["requests"]["write"]["p95_ms"], 40)
        self.assertEqual(report["requests"]["write_successes"], 30)
        self.assertEqual(report["claim_scope"]["api_instances"], 1)
        self.assertFalse(report["claim_scope"]["multi_instance_load_proven"])
        self.assertEqual(report["scenario"]["write_rate_per_second"], 0.5)
        self.assertEqual(report["projection"]["refresh_checks"], 90)
        self.assertEqual(report["projection"]["refresh_deferred"], 10)
        self.assertEqual(report["projection"]["reload_successes"], 2)
        self.assertEqual(report["projection"]["stable_snapshot_retries"], 1)
        self.assertEqual(report["projection"]["rows_loaded_per_reload"]["nodes"], 1000)
        self.assertGreater(report["projection"]["timings"]["reload"]["p95_ms"], 0)
        self.assertNotIn("session", json.dumps(report).lower())

    def test_read_heavy_accepts_zero_reload_and_no_write_samples(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = self.make_args(Path(tmp), "read_heavy", include_reload=False)
            report = summarize(args)
        self.assertIsNone(report["requests"]["write"])
        self.assertEqual(report["projection"]["reload_successes"], 0)
        self.assertEqual(report["projection"]["timings"]["reload"]["count"], 0)
        self.assertEqual(report["projection"]["version_delta"], 0)

    def test_current_contract_fails_closed_when_any_full_reload_occurs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = self.make_args(Path(tmp), "mixed", include_reload=True)
            args.max_reloads = 0
            with self.assertRaisesRegex(Cq02EvidenceError, "performed 2 full reloads"):
                summarize(args)

    def test_mixed_report_fails_closed_when_generation_count_does_not_match_successes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = self.make_args(root, "mixed", include_reload=False)
            (root / "version-after.txt").write_text("39\n", encoding="utf-8")
            with self.assertRaisesRegex(Cq02EvidenceError, "generation accounting is inconsistent"):
                summarize(args)

    def test_mixed_report_fails_closed_when_k6_drops_iterations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = self.make_args(root, "mixed", include_reload=True)
            (root / "k6_summary.json").write_text(
                json.dumps(k6_summary("mixed", dropped_iterations=2)), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                Cq02EvidenceError, "offered load was not sustained"
            ):
                summarize(args)

    def test_read_heavy_report_also_fails_closed_when_k6_drops_iterations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = self.make_args(root, "read_heavy", include_reload=False)
            (root / "k6_summary.json").write_text(
                json.dumps(k6_summary("read_heavy", dropped_iterations=1)), encoding="utf-8"
            )
            with self.assertRaisesRegex(Cq02EvidenceError, "offered load was not sustained"):
                summarize(args)

    def test_mixed_report_fails_closed_when_success_accounting_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = self.make_args(root, "mixed", include_reload=True)
            summary = k6_summary("mixed")
            summary["metrics"].pop("cq02_write_successes_total")
            (root / "k6_summary.json").write_text(json.dumps(summary), encoding="utf-8")
            with self.assertRaisesRegex(Cq02EvidenceError, "no successful PATCH writes"):
                summarize(args)

    def test_mixed_report_fails_closed_on_write_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = self.make_args(root, "mixed", include_reload=True)
            summary = k6_summary("mixed")
            summary["metrics"]["cq02_write_successes_total"]["values"]["count"] = 29
            summary["metrics"]["cq02_write_failures_total"]["values"]["count"] = 1
            (root / "k6_summary.json").write_text(json.dumps(summary), encoding="utf-8")
            with self.assertRaisesRegex(Cq02EvidenceError, "recorded 1 failed writes"):
                summarize(args)

    def test_report_fails_closed_on_http_503(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = self.make_args(root, "read_heavy", include_reload=False)
            summary = k6_summary("read_heavy")
            summary["metrics"]["cq02_503_total"]["values"]["count"] = 1
            (root / "k6_summary.json").write_text(json.dumps(summary), encoding="utf-8")
            with self.assertRaisesRegex(Cq02EvidenceError, "recorded 1 HTTP 503 responses"):
                summarize(args)

    def test_read_heavy_report_fails_closed_on_version_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = self.make_args(root, "read_heavy", include_reload=False)
            (root / "version-after.txt").write_text("11\n", encoding="utf-8")
            with self.assertRaisesRegex(Cq02EvidenceError, "uncontrolled domain version change"):
                summarize(args)

    def test_commit_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = self.make_args(Path(tmp), "mixed", include_reload=True)
            args.expected_head = "0" * 40
            with self.assertRaises(Cq02EvidenceError):
                summarize(args)


if __name__ == "__main__":
    unittest.main()
