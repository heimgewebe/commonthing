#!/usr/bin/env python3
"""Fail-closed evaluation of an isolated recent/aged Event-Chain readiness A/B run.

This is NOT the canonical T048 benchmark or a historical cold-start replay.
Both revisions must run with live PostgreSQL, JetStream and healthy workers.
Only fixed-cardinality counts and revision/digest identifiers enter receipts.
"""
from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path

OLD_SHA = "4164c5b337c7d09dc4e3229b0705b9a2076fd9e9"
FIX_SHA = "66c77f39de4a67783e8ab42daa305193d8e4450d"
EVENT_COUNT = 140_001
AGE_SECONDS = {"recent": 120, "aged": 720}
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
IMAGE_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")


class InvalidEvidence(ValueError):
    """Observed data does not satisfy the experiment identity or safety contract."""


def load_json(path: Path) -> dict:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InvalidEvidence(f"unavailable_or_invalid_json:{path.name}") from exc
    if not isinstance(result, dict):
        raise InvalidEvidence(f"invalid_json_object:{path.name}")
    return result


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidEvidence(f"{label}_not_numeric")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise InvalidEvidence(f"{label}_not_finite_nonnegative")
    return number


def _metric(data: dict, key: str, name: str, *, missing_zero: bool = False) -> float:
    metrics = data.get("metrics")
    if not isinstance(metrics, dict):
        raise InvalidEvidence("metrics_object_missing")
    metric = metrics.get(key)
    if metric is None and missing_zero:
        return 0.0
    if not isinstance(metric, dict) or not isinstance(metric.get("values"), dict):
        raise InvalidEvidence(f"{key}_missing")
    return _finite(metric["values"].get(name), f"{key}_{name}")


def _count(data: dict, key: str, *, missing_zero: bool = False) -> int:
    value = _metric(data, key, "count", missing_zero=missing_zero)
    if not value.is_integer():
        raise InvalidEvidence(f"{key}_count_noninteger")
    return int(value)


def validate_manifest(manifest: dict) -> None:
    if manifest.get("schema_version") != 1 or manifest.get("event_count") != EVENT_COUNT:
        raise InvalidEvidence("manifest_schema_or_event_count")
    if manifest.get("baseline_sha") != OLD_SHA or manifest.get("fix_sha") != FIX_SHA:
        raise InvalidEvidence("manifest_revision_mismatch")
    if manifest.get("virtual_users") != 10 or manifest.get("duration_seconds") != 30:
        raise InvalidEvidence("manifest_load_profile_mismatch")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", str(manifest.get("run_id", ""))):
        raise InvalidEvidence("manifest_run_id_invalid")
    if manifest.get("run_order") != ["fix", "baseline"]:
        raise InvalidEvidence("manifest_conservative_order_missing")
    startup = manifest.get("startup_ready")
    if not isinstance(startup, dict) or any(
        startup.get(variant) is not True for variant in ("baseline", "fix")
    ):
        raise InvalidEvidence("healthy_startup_readback_missing")
    images = manifest.get("images")
    if not isinstance(images, dict) or any(
        not isinstance(images.get(name), str) or not IMAGE_SHA.fullmatch(images[name])
        for name in ("baseline", "fix")
    ):
        raise InvalidEvidence("manifest_image_identity_missing")


def parse_run(data: dict, manifest: dict, variant: str, phase: str) -> dict:
    meta = data.get("event_chain_proof")
    expected_sha = OLD_SHA if variant == "baseline" else FIX_SHA
    if not isinstance(meta, dict) or (
        meta.get("schema_version") != 1
        or meta.get("run_id") != manifest["run_id"]
        or meta.get("variant") != variant
        or meta.get("phase") != phase
        or meta.get("git_head") != expected_sha
        or meta.get("image_id") != manifest["images"][variant]
        or meta.get("event_count") != EVENT_COUNT
        or meta.get("event_age_seconds") != AGE_SECONDS[phase]
        or meta.get("virtual_users") != 10
        or meta.get("duration_seconds") != 30
    ):
        raise InvalidEvidence(f"run_binding_invalid:{variant}_{phase}")
    total = _count(data, "http_reqs")
    ok = _count(data, "proof_ready_200", missing_zero=True)
    unavailable = _count(data, "proof_ready_503", missing_zero=True)
    other = _count(data, "proof_ready_other", missing_zero=True)
    if total < 10 or ok + unavailable + other != total:
        raise InvalidEvidence(f"request_accounting_invalid:{variant}_{phase}")
    p95 = _metric(data, "http_req_duration", "p(95)")
    p99 = _metric(data, "http_req_duration", "p(99)")
    failure = _metric(data, "http_req_failed", "rate")
    if failure > 1:
        raise InvalidEvidence(f"invalid_failure_rate:{variant}_{phase}")
    return {
        "variant": variant,
        "phase": phase,
        "source_sha": expected_sha,
        "image_id": manifest["images"][variant],
        "http_requests": total,
        "ready_200": ok,
        "ready_503": unavailable,
        "ready_other": other,
        "p95_ms": round(p95, 3),
        "p99_ms": round(p99, 3),
        "http_failed_rate": round(failure, 6),
    }


def evaluate(manifest: dict, runs: dict, policy: dict) -> dict:
    validate_manifest(manifest)
    section = policy.get("measurements", {}).get("api_runtime", {})
    metrics = section.get("metrics", {})
    try:
        p95_limit = _finite(metrics["http_request_duration_ms"]["max"], "policy_p95")
        p99_limit = _finite(metrics["http_request_duration_p99_ms"]["max"], "policy_p99")
    except (KeyError, TypeError) as exc:
        raise InvalidEvidence("canonical_policy_missing_limits") from exc
    if p95_limit <= 0 or p99_limit <= 0:
        raise InvalidEvidence("canonical_policy_limits_invalid")

    results = {
        f"{variant}_{phase}": parse_run(runs[(variant, phase)], manifest, variant, phase)
        for variant in ("baseline", "fix")
        for phase in AGE_SECONDS
    }
    candidate = results["fix_recent"]
    candidate_aged = results["fix_aged"]
    baseline = results["baseline_recent"]

    checks = {
        "baseline_aged_all_ready": results["baseline_aged"]["ready_200"] == results["baseline_aged"]["http_requests"],
        "candidate_recent_all_ready": candidate["ready_200"] == candidate["http_requests"],
        "candidate_aged_all_ready": candidate_aged["ready_200"] == candidate_aged["http_requests"],
        "candidate_recent_p95": candidate["p95_ms"] <= p95_limit,
        "candidate_recent_p99": candidate["p99_ms"] <= p99_limit,
        "candidate_aged_p95": candidate_aged["p95_ms"] <= p95_limit,
        "candidate_aged_p99": candidate_aged["p99_ms"] <= p99_limit,
        "candidate_no_http_errors": (
            candidate["http_failed_rate"] == 0 and candidate_aged["http_failed_rate"] == 0
        ),
    }
    # An old-vs-new difference on one runner is suggestive, never a historical
    # attribution or proof of a production speedup. Do not award efficacy PASS
    # just because the candidate was fast if the control was equally fast.
    observed_improvement = (
        baseline["ready_503"] > candidate["ready_503"]
        or baseline["p95_ms"] >= 1.2 * max(candidate["p95_ms"], 0.001)
    )
    checks["relative_improvement_observed"] = observed_improvement
    # An inconclusive comparison may satisfy absolute readiness limits without
    # establishing a measurable benefit over the unchanged baseline.
    absolute_pass = all(v for key, v in checks.items() if key != "relative_improvement_observed")
    verdict = "pass" if absolute_pass and observed_improvement else (
        "inconclusive" if absolute_pass else "fail"
    )
    return {
        "schema_version": 1,
        "kind": "commonthing.event_chain_readiness_ab_proof",
        "status": verdict,
        "run_id": manifest["run_id"],
        "baseline_sha": OLD_SHA,
        "fix_sha": FIX_SHA,
        "event_count": EVENT_COUNT,
        "load_profile": {"virtual_users": 10, "duration_seconds": 30},
        "event_age_seconds": AGE_SECONDS,
        "canonical_limits_ms": {"p95": p95_limit, "p99": p99_limit},
        "checks": checks,
        "runs": results,
        "limitations": [
            "isolated GitHub runner, not production or a complete Experiment-B cell",
            "recent versus aged published events, not a historical deployment cold start",
            "one paired run does not prove the cause of the archived 267 readiness 503s",
            "mixed-health-and-search T048 is not rerun; the probe measures readiness only",
            "performance changes on a shared runner require repetition before a capacity claim",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--policy", type=Path, default=Path("policies/performance.v1.json"))
    parser.add_argument("--baseline-recent", type=Path, required=True)
    parser.add_argument("--baseline-aged", type=Path, required=True)
    parser.add_argument("--fix-recent", type=Path, required=True)
    parser.add_argument("--fix-aged", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = evaluate(
            load_json(args.manifest),
            {
                ("baseline", "recent"): load_json(args.baseline_recent),
                ("baseline", "aged"): load_json(args.baseline_aged),
                ("fix", "recent"): load_json(args.fix_recent),
                ("fix", "aged"): load_json(args.fix_aged),
            },
            load_json(args.policy),
        )
    except InvalidEvidence as exc:
        report = {
            "schema_version": 1,
            "kind": "commonthing.event_chain_readiness_ab_proof",
            "status": "invalid_evidence",
            "error_code": str(exc),
        }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report.get(key) for key in ("status", "error_code", "checks")}, sort_keys=True))
    return 0 if report["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())