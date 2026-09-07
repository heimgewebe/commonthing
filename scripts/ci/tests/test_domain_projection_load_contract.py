from __future__ import annotations

import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from scripts.performance.domain_projection_load import Cq02EvidenceError, summarize

HEAD = "4940e0c335d9173f31c44e2be5701499149879e4"


def prometheus(*, after: bool, include_reload: bool = True) -> str:
    lines = [f'build_info{{commit="{HEAD}",version="test",build_timestamp="test"}} 1']
    if after:
        lines.append('domain_projection_events_total{event="refresh_check"} 100')
        lines.append('domain_projection_events_total{event="refresh_failure"} 0')
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
        (root / "version-after.txt").write_text(
            "12\n" if include_reload else "10\n", encoding="utf-8"
        )
        (root / "log-before.txt").write_text("startup\n", encoding="utf-8")
        retry = (
            "Domain projection changed during reload; retrying stable snapshot\n"
            if include_reload
            else ""
        )
        (root / "log-after.txt").write_text("startup\n" + retry, encoding="utf-8")
        return Namespace(
            expected_head=HEAD,
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
        self.assertEqual(report["scenario"]["write_rate_per_second"], 0.5)
        self.assertEqual(report["projection"]["refresh_checks"], 90)
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

    def test_mixed_report_fails_closed_when_k6_drops_iterations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = self.make_args(root, "mixed", include_reload=True)
            (root / "k6_summary.json").write_text(
                json.dumps(k6_summary("mixed", dropped_iterations=2)), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                Cq02EvidenceError, "offered mixed-load rate was not sustained"
            ):
                summarize(args)

    def test_commit_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = self.make_args(Path(tmp), "mixed", include_reload=True)
            args.expected_head = "0" * 40
            with self.assertRaises(Cq02EvidenceError):
                summarize(args)


if __name__ == "__main__":
    unittest.main()
