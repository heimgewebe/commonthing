#!/usr/bin/env python3
"""Tests for the isolated Event-Chain readiness A/B receipt gate."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.performance.event_chain_readiness_proof import (
    AGE_SECONDS,
    EVENT_COUNT,
    FIX_SHA,
    OLD_SHA,
    InvalidEvidence,
    evaluate as evaluate_proof,
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
        "observed_runtime_images": {"baseline": BASE_IMAGE, "fix": FIX_IMAGE},
    }


def summary(variant="baseline", phase="recent", p95=500.0, p99=690.0,
            statuses=(100, 0, 0), event_chain_timeout_count=None, mixed_timeout_count=0):
    ok, unavailable, other = statuses
    if event_chain_timeout_count is None:
        event_chain_timeout_count = unavailable
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
        "state": {"testRunDurationMs": 30_100},
        "metrics": {
            "vus_max": {"values": {"max": 10}},
            "http_reqs": {"values": {"count": count}},
            "http_req_duration": {"values": {"p(95)": p95, "p(99)": p99}},
            "http_req_failed": {"values": {"rate": rate}},
            "proof_ready_200": {"values": {"count": ok}},
            **({"proof_ready_503": {"values": {"count": unavailable}}} if unavailable else {}),
            **({"proof_ready_503_event_chain_timeout": {"values": {"count": event_chain_timeout_count}}}
               if event_chain_timeout_count else {}),
            **({"proof_ready_503_check_false_event_chain": {"values": {"count": event_chain_timeout_count + mixed_timeout_count}}}
               if event_chain_timeout_count + mixed_timeout_count else {}),
            **({"proof_ready_503_check_false_database": {"values": {"count": mixed_timeout_count}}}
               if mixed_timeout_count else {}),
            **({"proof_ready_503_event_chain_timeout_mixed": {"values": {"count": mixed_timeout_count}}}
               if mixed_timeout_count else {}),
            **({"proof_ready_503_other_cause": {"values": {"count": unavailable - event_chain_timeout_count}}}
               if unavailable > event_chain_timeout_count else {}),
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
        ("baseline", "recent"): summary(p95=752, p99=754, statuses=(30, 70, 0)),
        ("baseline", "aged"): summary(phase="aged", p95=60, p99=90),
        ("fix", "recent"): summary("fix", "recent", p95=70, p99=130),
        ("fix", "aged"): summary("fix", "aged", p95=55, p99=90),
    }


def negative_controls():
    result = {}
    for variant in ("baseline", "fix"):
        result[variant] = {
            "schema_version": 1,
            "run_id": "ct1940-test",
            "variant": variant,
            "source_sha": OLD_SHA if variant == "baseline" else FIX_SHA,
            "image_id": BASE_IMAGE if variant == "baseline" else FIX_IMAGE,
            "http_status": 503,
            "event_chain_failed": True,
            "other_checks_ready": True,
            "missing_durable_receipt": True,
            "recovered_http_200": True,
            "worker_up_after_recovery": True,
        }
    return result


def evaluate(manifest_data, run_data, policy_data, negatives=None):
    return evaluate_proof(
        manifest_data, run_data, policy_data,
        negative_controls() if negatives is None else negatives,
    )


class EventChainReadinessProofTests(unittest.TestCase):
    def test_valid_improvement_with_true_readiness_is_pass(self):
        result = evaluate(manifest(), runs(), policy())
        self.assertEqual(result["status"], "pass")
        self.assertTrue(all(result["checks"].values()))
        self.assertEqual(result["runs"]["fix_recent"]["ready_503"], 0)

    def test_mixed_db_event_chain_timeout_is_reported_without_claiming_joint_causality(self):
        cases = runs()
        cases[("baseline", "recent")] = summary(
            p95=752.253, p99=752.632, statuses=(30, 388, 0),
            event_chain_timeout_count=271, mixed_timeout_count=117,
        )
        result = evaluate(manifest(), cases, policy())
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["baseline_timeout_partition"], {
            "exclusive_event_chain_timeout_503": 271,
            "mixed_event_chain_timeout_and_database_failure_503": 117,
            "other_or_unclassified_503": 0,
        })
        self.assertTrue(result["checks"]["baseline_recent_cause_attributed"])
        self.assertTrue(any("not uniquely attributed" in item for item in result["limitations"]))

    def test_non_event_chain_or_nats_timeout_never_counts_as_pure_event_chain_improvement(self):
        cases = runs()
        cases[("baseline", "recent")] = summary(
            p95=752, p99=754, statuses=(30, 388, 0),
            event_chain_timeout_count=271, mixed_timeout_count=100,
        )
        result = evaluate(manifest(), cases, policy())
        self.assertEqual(result["status"], "inconclusive")
        self.assertEqual(result["baseline_timeout_partition"]["other_or_unclassified_503"], 17)
        cases = runs()
        cases[("baseline", "recent")] = summary(
            p95=752, p99=754, statuses=(30, 388, 0),
            event_chain_timeout_count=271, mixed_timeout_count=117,
        )
        cases[("baseline", "recent")]["metrics"]["proof_ready_503_check_false_nats"] = {
            "values": {"count": 1}
        }
        self.assertEqual(evaluate(manifest(), cases, policy())["status"], "inconclusive")

    def test_control_all_transport_errors_cannot_fake_an_effectiveness_pass(self):
        cases = runs()
        cases[("baseline", "recent")] = summary(p95=2_000, p99=2_000, statuses=(0, 0, 100))
        result = evaluate(manifest(), cases, policy())
        self.assertEqual(result["status"], "inconclusive")
        self.assertFalse(result["checks"]["baseline_recent_cause_attributed"])

    def test_baseline_other_component_503_cannot_claim_event_chain_benefit(self):
        cases = runs()
        cases[("baseline", "recent")] = summary(
            p95=752, p99=754, statuses=(10, 90, 0), event_chain_timeout_count=0
        )
        result = evaluate(manifest(), cases, policy())
        self.assertEqual(result["status"], "inconclusive")
        self.assertFalse(result["checks"]["baseline_recent_cause_attributed"])

    def test_event_chain_efficacy_requires_both_count_and_ratio_boundaries(self):
        # Mutation guards: counting mixed 503s, counting all 503s, using OR
        # instead of AND, or dropping either materiality threshold must fail.
        cases_to_check = (
            # These two fixtures independently kill either mutation that
            # counts mixed/all 503s instead of exclusive timeout 503s.
            (19, 1, 100, "inconclusive"),  # count: 19 pure, 20 total 503
            (20, 20, 300, "inconclusive"),  # ratio: 6.7% pure, 13.3% total
            (19, 300, 400, "inconclusive"),
            (19, 0, 100, "inconclusive"),
            (25, 0, 1000, "inconclusive"),
            (20, 0, 200, "pass"),
        )
        for pure, mixed, total, verdict in cases_to_check:
            with self.subTest(pure=pure, mixed=mixed, total=total):
                samples = runs()
                samples[("baseline", "recent")] = summary(
                    p95=752, p99=754,
                    statuses=(total - pure - mixed, pure + mixed, 0),
                    event_chain_timeout_count=pure,
                    mixed_timeout_count=mixed,
                )
                self.assertEqual(evaluate(manifest(), samples, policy())["status"], verdict)

    def test_actual_k6_javascript_classifies_synthetic_http_bodies(self):
        # Exercise the checked-in ESM module itself: do not mock the classifier
        # or guess the published k6 metric names via static text matching.
        root = Path(__file__).resolve().parents[3]
        script = root / "scripts/ci/tests/event_chain_readiness_k6_classification.test.mjs"
        proc = subprocess.run(
            ["node", "--no-warnings", "--experimental-vm-modules", str(script)],
            cwd=root, capture_output=True, text=True, check=False, timeout=20,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_each_missing_receipt_detection_flag_is_required_for_both_variants(self):
        for flag in ("event_chain_failed", "other_checks_ready", "missing_durable_receipt"):
            for variant in ("baseline", "fix"):
                with self.subTest(flag=flag, variant=variant):
                    negatives = negative_controls()
                    negatives[variant][flag] = False
                    result = evaluate(manifest(), runs(), policy(), negatives)
                    self.assertEqual(
                        result["status"],
                        "fail" if variant == "fix" else "inconclusive",
                    )
                    self.assertFalse(result["negative_controls"][variant]["detected"])

    def test_both_negative_control_recovery_signals_are_required(self):
        for variant in ("baseline", "fix"):
            for flag in ("recovered_http_200", "worker_up_after_recovery"):
                with self.subTest(variant=variant, flag=flag):
                    negatives = negative_controls()
                    negatives[variant][flag] = False
                    with self.assertRaisesRegex(
                        InvalidEvidence, "negative_control_recovery_missing"
                    ):
                        evaluate(manifest(), runs(), policy(), negatives)

    def test_mixed_baseline_errors_require_consistent_database_policy_and_parse_counts(self):
        mutations = (
            ("proof_ready_503_parse_error", 1),
            ("proof_ready_503_check_false_database", 116),
            ("proof_ready_503_check_false_policy", 1),
        )
        for metric, count in mutations:
            with self.subTest(metric=metric):
                cases = runs()
                cases[("baseline", "recent")] = summary(
                    p95=752, p99=754, statuses=(30, 388, 0),
                    event_chain_timeout_count=271, mixed_timeout_count=117,
                )
                cases[("baseline", "recent")]["metrics"][metric] = {
                    "values": {"count": count}
                }
                result = evaluate(manifest(), cases, policy())
                self.assertEqual(result["status"], "inconclusive")
                self.assertFalse(result["checks"]["baseline_recent_cause_attributed"])

    def test_mixed_timeout_counter_cannot_exceed_event_chain_component_failures(self):
        cases = runs()
        cases[("baseline", "recent")] = summary(
            p95=752, p99=754, statuses=(30, 388, 0),
            event_chain_timeout_count=271, mixed_timeout_count=117,
        )
        cases[("baseline", "recent")]["metrics"][
            "proof_ready_503_check_false_event_chain"
        ]["values"]["count"] = 387
        with self.assertRaisesRegex(
            InvalidEvidence, "readiness_503_diagnostics_inconsistent"
        ):
            evaluate(manifest(), cases, policy())

    def test_one_stray_event_chain_503_is_not_material_improvement(self):
        cases = runs()
        cases[("baseline", "recent")] = summary(p95=70, p99=130, statuses=(9999, 1, 0))
        result = evaluate(manifest(), cases, policy())
        self.assertEqual(result["status"], "inconclusive")
        self.assertFalse(result["checks"]["relative_improvement_observed"])

    def test_event_chain_cause_counter_mismatch_invalidates_evidence(self):
        cases = runs()
        cases[("baseline", "recent")] = summary(p95=752, p99=754, statuses=(10, 90, 0))
        cases[("baseline", "recent")]["metrics"]["proof_ready_503_event_chain_timeout"]["values"]["count"] = 89
        with self.assertRaisesRegex(InvalidEvidence, "readiness_503_cause_accounting_invalid"):
            evaluate(manifest(), cases, policy())

    def test_http_200_with_skipped_event_chain_is_invalid_evidence(self):
        cases = runs()
        cases[("fix", "recent")]["metrics"]["proof_ready_200_incomplete"] = {
            "values": {"count": 1}
        }
        with self.assertRaisesRegex(InvalidEvidence, "readiness_200_incomplete_checks"):
            evaluate(manifest(), cases, policy())

    def test_observed_vus_and_duration_are_required(self):
        cases = runs()
        cases[("fix", "recent")]["metrics"]["vus_max"]["values"]["max"] = 11
        with self.assertRaisesRegex(InvalidEvidence, "observed_vus_mismatch"):
            evaluate(manifest(), cases, policy())
        cases = runs()
        cases[("fix", "aged")]["state"]["testRunDurationMs"] = 5_000
        with self.assertRaisesRegex(InvalidEvidence, "observed_duration_mismatch"):
            evaluate(manifest(), cases, policy())

    def test_fix_skipping_unreceipted_event_is_a_real_failure(self):
        negatives = negative_controls()
        negatives["fix"]["http_status"] = 200
        negatives["fix"]["event_chain_failed"] = False
        result = evaluate(manifest(), runs(), policy(), negatives)
        self.assertEqual(result["status"], "fail")
        self.assertFalse(result["checks"]["candidate_negative_control_detected"])

    def test_baseline_missing_negative_detection_is_inconclusive(self):
        negatives = negative_controls()
        negatives["baseline"]["http_status"] = 200
        self.assertEqual(
            evaluate(manifest(), runs(), policy(), negatives)["status"], "inconclusive"
        )

    def test_missing_or_wrong_revision_negative_receipt_is_invalid(self):
        cases = negative_controls()
        cases.pop("fix")
        with self.assertRaisesRegex(InvalidEvidence, "negative_control_missing"):
            evaluate(manifest(), runs(), policy(), cases)
        cases = negative_controls()
        cases["fix"]["source_sha"] = OLD_SHA
        with self.assertRaisesRegex(InvalidEvidence, "negative_control_binding"):
            evaluate(manifest(), runs(), policy(), cases)

    def test_corrupt_mixed_503_diagnostic_counts_are_invalid(self):
        cases = runs()
        cases[("baseline", "recent")]["metrics"]["proof_ready_503_event_chain_timeout_mixed"] = {
            "values": {"count": 999}
        }
        with self.assertRaisesRegex(InvalidEvidence, "readiness_503_diagnostics_inconsistent"):
            evaluate(manifest(), cases, policy())

    def test_observed_container_image_identity_is_not_self_attested(self):
        m = manifest()
        m["observed_runtime_images"]["fix"] = BASE_IMAGE
        with self.assertRaisesRegex(InvalidEvidence, "observed_container_image_identity"):
            evaluate(m, runs(), policy())

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
        self.assertEqual(evaluate(manifest(), cases, policy())["status"], "inconclusive")

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

    def _exercise_real_cleanup_trap(self, experiment_exit: int, leak: bool) -> tuple[int, dict]:
        shell = (Path(__file__).resolve().parents[2] / "performance" / "event_chain_readiness_run.sh").read_text(
            encoding="utf-8"
        )
        start = shell.index("cleanup() {")
        end = shell.index("\ntrap cleanup EXIT", start)
        function = shell[start:end]
        # Stub the two potentially mutating external commands. This executes
        # the actual checked-in EXIT trap without Docker, Git or network access.
        script = """
set -Eeuo pipefail
docker() {
  if [[ "$1" == "ps" ]]; then
    if [[ "$STUB_LEAK" == "yes" ]]; then echo "$API_CONTAINER"; fi
    return 0
  fi
  if [[ "$1" == "image" && "$2" == "inspect" ]]; then return 1; fi
  return 0
}
git() { return 0; }
""" + function + "\ntrap cleanup EXIT\nexit " + str(experiment_exit) + "\n"
        with tempfile.TemporaryDirectory(prefix="ct1940-teardown-test-") as root:
            env = dict(os.environ, ROOT=root, API_CONTAINER="ct1940-test-api",
                       NATS_CONTAINER="ct1940-test-nats", BASE_IMAGE="ct1940-base:test",
                       FIX_IMAGE="ct1940-fix:test", STUB_LEAK="yes" if leak else "no")
            proc = subprocess.run(["bash", "-c", script], env=env, capture_output=True,
                                  text=True, check=False, timeout=10)
            receipt = json.loads((Path(root) / "teardown.json").read_text(encoding="utf-8"))
        return proc.returncode, receipt

    def test_cleanup_pass_can_coexist_with_expected_inconclusive_exit(self):
        exit_code, receipt = self._exercise_real_cleanup_trap(experiment_exit=2, leak=False)
        self.assertEqual(exit_code, 2)
        self.assertEqual(receipt["status"], "pass")
        self.assertTrue(receipt["api_removed"])

    def test_cleanup_failure_blocks_successful_experiment(self):
        exit_code, receipt = self._exercise_real_cleanup_trap(experiment_exit=0, leak=True)
        self.assertNotEqual(exit_code, 0)
        self.assertEqual(receipt["status"], "fail")


if __name__ == "__main__":
    unittest.main()
