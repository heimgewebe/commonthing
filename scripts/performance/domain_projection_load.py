#!/usr/bin/env python3
"""Assemble revision-bound CQ-02 PostgreSQL projection load evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from scripts.performance.api_runtime_evidence import (
    ApiRuntimeEvidenceError,
    HistogramSnapshot,
    histogram_delta,
    histogram_quantile_ms,
    measured_api_commit,
    parse_prometheus_text,
)

GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RETRY_MARKER = "Domain projection changed during reload; retrying stable snapshot"


class Cq02EvidenceError(RuntimeError):
    pass


def read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Cq02EvidenceError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise Cq02EvidenceError(f"{label} must be a JSON object")
    return parsed


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def k6_value(
    summary: Mapping[str, Any], metric: str, key: str, *, default: float | None = None
) -> float:
    metrics = summary.get("metrics")
    if not isinstance(metrics, dict):
        raise Cq02EvidenceError("k6 summary is missing metrics")
    entry = metrics.get(metric)
    if entry is None and default is not None:
        return default
    if not isinstance(entry, dict):
        raise Cq02EvidenceError(f"k6 summary is missing metric {metric}")
    values = entry.get("values")
    if not isinstance(values, dict) or key not in values:
        if default is not None:
            return default
        raise Cq02EvidenceError(f"k6 summary metric {metric} is missing values.{key}")
    value = values[key]
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
    ):
        raise Cq02EvidenceError(f"k6 summary metric {metric}.{key} must be finite")
    return float(value)


def trend(
    summary: Mapping[str, Any], metric: str, count_metric: str, *, required: bool
) -> dict[str, float | int] | None:
    metrics = summary.get("metrics")
    if not isinstance(metrics, dict):
        raise Cq02EvidenceError("k6 summary is missing metrics")
    if metric not in metrics:
        if required:
            raise Cq02EvidenceError(f"k6 summary is missing required trend {metric}")
        return None
    count = k6_value(summary, count_metric, "count", default=0.0)
    if count <= 0:
        if required:
            raise Cq02EvidenceError(f"k6 summary trend {metric} has no observations")
        return None
    return {
        "count": int(count),
        "p50_ms": k6_value(summary, metric, "p(50)"),
        "p95_ms": k6_value(summary, metric, "p(95)"),
        "p99_ms": k6_value(summary, metric, "p(99)"),
        "avg_ms": k6_value(summary, metric, "avg"),
        "max_ms": k6_value(summary, metric, "max"),
    }


def exact_sample(
    families: Mapping[str, list[tuple[dict[str, str], float]]],
    name: str,
    labels: Mapping[str, str],
    *,
    default: float | None = None,
) -> float:
    matches = []
    for sample_labels, value in families.get(name, []):
        if all(sample_labels.get(key) == expected for key, expected in labels.items()):
            matches.append(value)
    if not matches:
        if default is not None:
            return default
        raise Cq02EvidenceError(
            f"Prometheus metric {name} has no sample for labels {dict(labels)}"
        )
    if len(matches) != 1:
        raise Cq02EvidenceError(
            f"Prometheus metric {name} has duplicate samples for {dict(labels)}"
        )
    return matches[0]


def counter_delta(
    before: Mapping[str, list[tuple[dict[str, str], float]]],
    after: Mapping[str, list[tuple[dict[str, str], float]]],
    name: str,
    labels: Mapping[str, str],
) -> float:
    left = exact_sample(before, name, labels, default=0.0)
    right = exact_sample(after, name, labels, default=0.0)
    if right < left:
        raise Cq02EvidenceError(f"Prometheus counter {name}{dict(labels)} decreased")
    return right - left


def phase_histogram(
    families: Mapping[str, list[tuple[dict[str, str], float]]], phase: str
) -> HistogramSnapshot | None:
    base = "domain_projection_duration_seconds"
    bucket_samples = []
    for labels, value in families.get(f"{base}_bucket", []):
        if labels.get("phase") == phase:
            le = labels.get("le")
            if le is None:
                raise Cq02EvidenceError(f"{base}_bucket phase={phase} lacks le")
            bucket_samples.append((le, value))
    if not bucket_samples:
        return None
    buckets: dict[str, float] = {}
    for le, value in bucket_samples:
        if le in buckets:
            raise Cq02EvidenceError(
                f"duplicate histogram bucket phase={phase}, le={le}"
            )
        buckets[le] = value
    if "+Inf" not in buckets:
        raise Cq02EvidenceError(f"histogram phase={phase} lacks +Inf bucket")
    counts = [
        value
        for labels, value in families.get(f"{base}_count", [])
        if labels.get("phase") == phase
    ]
    sums = [
        value
        for labels, value in families.get(f"{base}_sum", [])
        if labels.get("phase") == phase
    ]
    if len(counts) != 1 or len(sums) != 1:
        raise Cq02EvidenceError(f"histogram phase={phase} lacks unique count/sum")
    return HistogramSnapshot(buckets=buckets, total_count=counts[0], total_sum=sums[0])


def zero_histogram_like(snapshot: HistogramSnapshot) -> HistogramSnapshot:
    return HistogramSnapshot(
        buckets={key: 0.0 for key in snapshot.buckets}, total_count=0.0, total_sum=0.0
    )


def histogram_stats(
    before: Mapping[str, list[tuple[dict[str, str], float]]],
    after: Mapping[str, list[tuple[dict[str, str], float]]],
    phase: str,
) -> dict[str, float | int | None]:
    left = phase_histogram(before, phase)
    right = phase_histogram(after, phase)
    if right is None:
        if left is not None and left.total_count > 0:
            raise Cq02EvidenceError(
                f"histogram phase={phase} disappeared between scrapes"
            )
        return {
            "count": 0,
            "avg_ms": None,
            "p50_ms": None,
            "p95_ms": None,
            "p99_ms": None,
        }
    if left is None:
        left = zero_histogram_like(right)
    try:
        delta = histogram_delta(left, right)
    except ApiRuntimeEvidenceError as exc:
        raise Cq02EvidenceError(str(exc)) from exc
    if delta.total_count == 0:
        return {
            "count": 0,
            "avg_ms": None,
            "p50_ms": None,
            "p95_ms": None,
            "p99_ms": None,
        }
    try:
        return {
            "count": int(delta.total_count),
            "avg_ms": (delta.total_sum / delta.total_count) * 1000.0,
            "p50_ms": histogram_quantile_ms(delta, 0.50),
            "p95_ms": histogram_quantile_ms(delta, 0.95),
            "p99_ms": histogram_quantile_ms(delta, 0.99),
        }
    except ApiRuntimeEvidenceError as exc:
        raise Cq02EvidenceError(str(exc)) from exc


def parse_version(path: Path) -> int:
    try:
        value = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError) as exc:
        raise Cq02EvidenceError(f"invalid domain version file {path}: {exc}") from exc
    if value < 0:
        raise Cq02EvidenceError("domain projection version cannot be negative")
    return value


def read_resource_receipt(path: Path | None, run_id: str) -> dict[str, Any] | None:
    if path is None:
        return None
    receipt = read_json(path, "resource receipt")
    if receipt.get("run_id") != run_id:
        raise Cq02EvidenceError(f"resource receipt run_id does not match {run_id}")
    peaks = receipt.get("peaks")
    if not isinstance(peaks, dict):
        raise Cq02EvidenceError("resource receipt is missing peaks")
    return {
        "peak_cpu_percent": peaks.get("cpu_percent"),
        "peak_memory_bytes": peaks.get("memory_bytes"),
        "sample_count": receipt.get("sample_count"),
    }


def read_connection_receipt(path: Path | None, run_id: str) -> dict[str, Any] | None:
    if path is None:
        return None
    receipt = read_json(path, "PostgreSQL connection receipt")
    if receipt.get("run_id") != run_id:
        raise Cq02EvidenceError(f"PostgreSQL receipt run_id does not match {run_id}")
    return {
        "max_connections": receipt.get("max_connections"),
        "sample_count": receipt.get("sample_count"),
    }


def summarize(args: argparse.Namespace) -> dict[str, Any]:
    summary = read_json(args.k6_summary, "k6 summary")
    cq02 = summary.get("cq02")
    if not isinstance(cq02, dict):
        raise Cq02EvidenceError(
            "k6 summary was not produced by domain_projection_k6.js"
        )
    run_id = cq02.get("run_id")
    profile = cq02.get("profile")
    workload = cq02.get("workload")
    if not isinstance(run_id, str) or not run_id:
        raise Cq02EvidenceError("k6 CQ-02 run_id is invalid")
    if not isinstance(profile, str) or not profile:
        raise Cq02EvidenceError("k6 CQ-02 profile is invalid")
    if workload not in {"read_heavy", "mixed"}:
        raise Cq02EvidenceError("k6 CQ-02 workload is invalid")

    manifest = read_json(args.dataset_manifest, "dataset manifest")
    if manifest.get("profile") != profile:
        raise Cq02EvidenceError("dataset manifest profile does not match k6 profile")
    counts = manifest.get("counts")
    if not isinstance(counts, dict):
        raise Cq02EvidenceError("dataset manifest is missing counts")

    try:
        before = parse_prometheus_text(args.metrics_before.read_text(encoding="utf-8"))
        after = parse_prometheus_text(args.metrics_after.read_text(encoding="utf-8"))
        before_commit = measured_api_commit(before)
        after_commit = measured_api_commit(after)
    except (OSError, ApiRuntimeEvidenceError) as exc:
        raise Cq02EvidenceError(str(exc)) from exc
    if not GIT_SHA_RE.fullmatch(args.expected_head):
        raise Cq02EvidenceError("expected head must be a 40-hex git SHA")
    if before_commit != args.expected_head or after_commit != args.expected_head:
        raise Cq02EvidenceError(
            f"metrics commit mismatch: before={before_commit}, after={after_commit}, expected={args.expected_head}"
        )

    version_before = parse_version(args.version_before)
    version_after = parse_version(args.version_after)
    if version_after < version_before:
        raise Cq02EvidenceError("domain projection version decreased")

    log_before = args.api_log_before.read_text(encoding="utf-8", errors="replace")
    log_after = args.api_log_after.read_text(encoding="utf-8", errors="replace")
    retry_delta = log_after.count(RETRY_MARKER) - log_before.count(RETRY_MARKER)
    if retry_delta < 0:
        raise Cq02EvidenceError("API retry log count decreased")

    dropped_iterations = int(
        k6_value(summary, "dropped_iterations", "count", default=0.0)
    )
    if dropped_iterations > 0:
        raise Cq02EvidenceError(
            f"k6 dropped {dropped_iterations} scheduled iterations; "
            "the offered load was not sustained"
        )

    read_stats = trend(
        summary,
        "cq02_read_duration_ms",
        "cq02_read_requests_total",
        required=True,
    )
    write_stats = trend(
        summary,
        "cq02_write_duration_ms",
        "cq02_write_requests_total",
        required=workload == "mixed",
    )
    read_failures = int(
        k6_value(summary, "cq02_read_failures_total", "count", default=0.0)
    )
    write_failures = int(
        k6_value(summary, "cq02_write_failures_total", "count", default=0.0)
    )
    write_successes = int(
        k6_value(summary, "cq02_write_successes_total", "count", default=0.0)
    )
    status_503 = int(k6_value(summary, "cq02_503_total", "count", default=0.0))
    version_delta = version_after - version_before

    if read_failures != 0:
        raise Cq02EvidenceError(f"k6 recorded {read_failures} failed reads")
    if status_503 != 0:
        raise Cq02EvidenceError(f"k6 recorded {status_503} HTTP 503 responses")
    if workload == "read_heavy":
        if version_delta != 0:
            raise Cq02EvidenceError(
                "read-heavy workload observed an uncontrolled domain version change"
            )
    else:
        if write_stats is None or int(write_stats["count"]) <= 0:
            raise Cq02EvidenceError("mixed workload recorded no logical write attempts")
        write_attempts = int(write_stats["count"])
        if write_failures != 0:
            raise Cq02EvidenceError(f"mixed workload recorded {write_failures} failed writes")
        if write_successes <= 0:
            raise Cq02EvidenceError("mixed workload recorded no successful PATCH writes")
        if write_successes + write_failures != write_attempts:
            raise Cq02EvidenceError(
                "mixed workload write accounting is inconsistent: "
                f"attempts={write_attempts}, successes={write_successes}, failures={write_failures}"
            )
        if version_delta <= 0:
            raise Cq02EvidenceError("mixed workload did not advance the domain projection version")
        if version_delta != write_successes:
            raise Cq02EvidenceError(
                "mixed workload generation accounting is inconsistent: "
                f"version_delta={version_delta}, successful_patches={write_successes}"
            )

    reloads = int(
        counter_delta(
            before, after, "domain_projection_events_total", {"event": "reload_success"}
        )
    )
    refresh_failures = int(
        counter_delta(
            before, after, "domain_projection_events_total", {"event": "refresh_failure"}
        )
    )
    reload_failures = int(
        counter_delta(
            before, after, "domain_projection_events_total", {"event": "reload_failure"}
        )
    )
    max_reloads = getattr(args, "max_reloads", 0)
    if not isinstance(max_reloads, int) or isinstance(max_reloads, bool) or max_reloads < 0:
        raise Cq02EvidenceError("max_reloads must be a non-negative integer")
    if refresh_failures != 0:
        raise Cq02EvidenceError(
            f"projection refresh recorded {refresh_failures} failures"
        )
    if reload_failures != 0:
        raise Cq02EvidenceError(f"projection reload recorded {reload_failures} failures")
    if reloads > max_reloads:
        raise Cq02EvidenceError(
            f"projection performed {reloads} full reloads; allowed maximum is {max_reloads}"
        )
    if max_reloads == 0 and retry_delta != 0:
        raise Cq02EvidenceError(
            f"projection recorded {retry_delta} stable-snapshot retries despite zero-reload contract"
        )

    report = {
        "schema_version": 1,
        "contract": "cq02-domain-projection-load-evidence-v1",
        "revision": {
            "expected_head": args.expected_head,
            "measured_api_commit": after_commit,
        },
        "claim_scope": {
            "api_instances": 1,
            "multi_instance_load_proven": False,
        },
        "dataset": {
            "profile": profile,
            "manifest_sha256": sha256_file(args.dataset_manifest),
            "nodes": counts.get("nodes"),
            "edges": counts.get("edges"),
        },
        "scenario": {
            "run_id": run_id,
            "workload": workload,
            "duration_seconds": cq02.get("duration_seconds"),
            "read_vus": cq02.get("read_vus"),
            "write_rate_per_second": cq02.get("write_rate_per_second"),
            "dropped_iterations": dropped_iterations,
        },
        "requests": {
            "read": read_stats,
            "write": write_stats,
            "read_failures": read_failures,
            "write_failures": write_failures,
            "write_successes": write_successes,
            "status_503": status_503,
        },
        "projection": {
            "refresh_checks": int(
                counter_delta(
                    before,
                    after,
                    "domain_projection_events_total",
                    {"event": "refresh_check"},
                )
            ),
            "refresh_failures": refresh_failures,
            "refresh_deferred": int(
                counter_delta(
                    before,
                    after,
                    "domain_projection_events_total",
                    {"event": "refresh_deferred"},
                )
            ),
            "reload_successes": reloads,
            "reload_failures": reload_failures,
            "stable_snapshot_retries": retry_delta,
            "version_before": version_before,
            "version_after": version_after,
            "version_delta": version_delta,
            "rows_loaded": {
                kind: int(
                    counter_delta(
                        before,
                        after,
                        "domain_projection_rows_loaded_total",
                        {"kind": kind},
                    )
                )
                for kind in ("accounts", "nodes", "edges")
            },
            "rows_loaded_per_reload": None,
            "timings": {
                phase: histogram_stats(before, after, phase)
                for phase in (
                    "reload",
                    "write_gate_wait",
                    "write_gate_hold",
                    "read_gate_wait",
                )
            },
        },
        "resources": {
            "api_container": read_resource_receipt(args.resource_receipt, run_id),
            "postgres_connections": read_connection_receipt(
                args.connection_receipt, run_id
            ),
        },
    }
    if reloads > 0:
        report["projection"]["rows_loaded_per_reload"] = {
            kind: report["projection"]["rows_loaded"][kind] / reloads
            for kind in ("accounts", "nodes", "edges")
        }
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("summarize")
    command.add_argument("--expected-head", required=True)
    command.add_argument(
        "--max-reloads",
        type=int,
        default=0,
        help="maximum full projection reloads allowed in this evidence run (default: 0)",
    )
    command.add_argument("--dataset-manifest", type=Path, required=True)
    command.add_argument("--k6-summary", type=Path, required=True)
    command.add_argument("--metrics-before", type=Path, required=True)
    command.add_argument("--metrics-after", type=Path, required=True)
    command.add_argument("--version-before", type=Path, required=True)
    command.add_argument("--version-after", type=Path, required=True)
    command.add_argument("--api-log-before", type=Path, required=True)
    command.add_argument("--api-log-after", type=Path, required=True)
    command.add_argument("--resource-receipt", type=Path)
    command.add_argument("--connection-receipt", type=Path)
    command.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "summarize":
            report = summarize(args)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            print(json.dumps(report, sort_keys=True))
            return 0
    except Cq02EvidenceError as exc:
        print(f"domain-projection-load: {exc}", file=__import__("sys").stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
