#!/usr/bin/env python3
"""Tests for the isolated Event-Chain readiness A/B receipt gate."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.performance.event_chain_readiness_proof import (
    AGE_SECONDS,
    EVENT_COUNT,
    FIX_SHA,
    OLD_SHA,
    InvalidEvidence,
    evaluate,
    validate_manifest,
)

BASE_IMAGE = "sha256:" + "a" * 64
FIX_IMAGE = "sha256:" + "b" * 64


def manifest():
    return {
        "schema_version": 1,
        "run_id": "ct1940-test",
        "baseline_sha": OLD_SHA,
        "fix_sha": FIX_SHA,
        "event_count": EVENT_COUNT,
        "duration_seconds": 30,
        "virtual_users": 10,
        "startup_ready": {"baseline": True, "fix": True},
        "run_order": ["fix", "baseline"],
        "images": {"baseline": BASE_IMAGE, "fix": FIX_IMAGE},
    }


def summary(variant="baseline", phase="recent", p95=500.0, p99=690.0, statuses=(100, 0, 0)):
    ok, unavailable, other = statuses
    count = ok + unavailable + other
    sha = OLD_SHA if variant == "baseline" else FIX_SHA
    image = BASE_IMAGE if variant == "baseline" else FIX_IMAGE
    rate = (unavailable + other) / count if count else 1
    return {
        "event_chain_proof": {
            "schema_version": 1,
            "run_id": "ct1940-test",
            "variant": variant,
            "phase": phase,
            "git_head": sha,
            "image_id": image,
            "event_count": EVENT_COUNT,
            "event_age_seconds": AGE_SECONDS[phase],
            "virtual_users": 10,
            "duration_seconds": 30,
        },
        "metrics": {
            "http_reqs": {"values": {"count": count}},
            "http_req_duration": {"values": {"p(95)": p95, "p(99)": p99}},
            "http_req_failed": {"values": {"rate": rate}},
            "proof_ready_200": {"values": {"count": ok}},
            **({"proof_ready_503": {"values": {"count": unavailable}}} if unavailable else {}),
            **({"proof_ready_other": {"values": {"count": other}}} if other else {}),
        },
    }


def policy():
    return {"measurements": {"api_runtime": {"metrics": {
        "http_request_duration_ms": {"max": 300},
        "http_request_duration_p99_ms": {"max": 750},
    }}}}


def runs():
    return {
        ("baseline", "recent"): summary(p95=500, p99=690),
        ("baseline", "aged"): summary(phase="aged", p95=60, p99=90),
        ("fix", "recent"): summary("fix", "recent", p95=70, p99=130),
        ("fix", "aged"): summary("fix", "aged", p95=55, p99=90),
    }


class EventChainReadinessProofTests(unittest.TestCase):
    def test_valid_improvement_with_true_readiness_is_pass(self):
        result = evaluate(manifest(), runs(), policy())
        self.assertEqual(result["status"], "pass")
        self.assertTrue(all(result["checks"].values()))
        self.assertEqual(result["runs"]["fix_recent"]["ready_503"], 0)

    def test_candidate_503_is_real_failure_even_if_it_is_fast(self):
        cases = runs()
        cases[("fix", "recent")] = summary("fix", "recent", p95=5, p99=9, statuses=(90, 10, 0))
        result = evaluate(manifest(), cases, policy())
        self.assertEqual(result["status"], "fail")
        self.assertFalse(result["checks"]["candidate_recent_all_ready"])
        self.assertFalse(result["checks"]["candidate_no_http_errors"])

    def test_equal_fast_control_is_inconclusive_not_success(self):
        cases = runs()
        cases[("baseline", "recent")] = summary(p95=70, p99=130)
        self.assertEqual(evaluate(manifest(), cases, policy())["status"], "inconclusive")

    def test_unhealthy_control_aged_is_not_valid_control(self):
        cases = runs()
        cases[("baseline", "aged")] = summary(phase="aged", p95=50, p99=90, statuses=(0, 100, 0))
        self.assertEqual(evaluate(manifest(), cases, policy())["status"], "fail")

    def test_invalid_source_or_image_is_rejected(self):
        cases = runs()
        cases[("fix", "recent")]["event_chain_proof"]["git_head"] = OLD_SHA
        with self.assertRaisesRegex(InvalidEvidence, "run_binding_invalid"):
            evaluate(manifest(), cases, policy())
        cases = runs()
        cases[("fix", "recent")]["event_chain_proof"]["image_id"] = BASE_IMAGE
        with self.assertRaisesRegex(InvalidEvidence, "run_binding_invalid"):
            evaluate(manifest(), cases, policy())

    def test_mismatched_event_count_or_duration_rejected(self):
        cases = runs()
        cases[("fix", "aged")]["event_chain_proof"]["event_count"] = EVENT_COUNT - 1
        with self.assertRaises(InvalidEvidence):
            evaluate(manifest(), cases, policy())
        m = manifest()
        m["duration_seconds"] = 5
        with self.assertRaises(InvalidEvidence):
            validate_manifest(m)

    def test_baseline_first_order_does_not_prove_causality(self):
        m = manifest()
        m["run_order"] = ["baseline", "fix"]
        with self.assertRaisesRegex(InvalidEvidence, "manifest_conservative_order"):
            evaluate(m, runs(), policy())

    def test_malformed_metric_object_is_invalid_evidence(self):
        sample = runs()
        sample[("fix", "recent")]["metrics"] = None
        with self.assertRaisesRegex(InvalidEvidence, "metrics_object_missing"):
            evaluate(manifest(), sample, policy())

    def test_missing_healthy_worker_startup_rejected(self):
        m = manifest()
        m["startup_ready"]["baseline"] = False
        with self.assertRaisesRegex(InvalidEvidence, "healthy_startup"):
            evaluate(m, runs(), policy())

    def test_count_mismatch_cannot_be_hidden_by_zero_failed_rate(self):
        cases = runs()
        cases[("fix", "aged")]["metrics"]["http_reqs"]["values"]["count"] = 101
        with self.assertRaisesRegex(InvalidEvidence, "request_accounting"):
            evaluate(manifest(), cases, policy())

    def test_other_response_is_failure(self):
        cases = runs()
        cases[("fix", "aged")] = summary("fix", "aged", p95=10, p99=20, statuses=(99, 0, 1))
        self.assertEqual(evaluate(manifest(), cases, policy())["status"], "fail")

    def test_no_requests_is_invalid(self):
        cases = runs()
        cases[("baseline", "recent")] = summary(statuses=(0, 0, 0))
        with self.assertRaises(InvalidEvidence):
            evaluate(manifest(), cases, policy())

    def test_non_finite_or_missing_latency_is_rejected(self):
        cases = runs()
        cases[("fix", "recent")]["metrics"]["http_req_duration"]["values"]["p(95)"] = float("nan")
        with self.assertRaises(InvalidEvidence):
            evaluate(manifest(), cases, policy())
        cases = runs()
        del cases[("fix", "aged")]["metrics"]["http_req_duration"]
        with self.assertRaises(InvalidEvidence):
            evaluate(manifest(), cases, policy())

    def test_report_preserves_epistemic_limitations(self):
        report = evaluate(manifest(), runs(), policy())
        self.assertTrue(any("historical" in item for item in report["limitations"]))
        self.assertTrue(any("not production" in item for item in report["limitations"]))


if __name__ == "__main__":
    unittest.main()