#!/usr/bin/env python3
"""Detect producer-to-consumer Schaubild release convergence drift."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

from schauwerk_editor_release import ReleaseContractError, verify_runtime_lock

SCHEMA = "weltgewebe-schauwerk-release-convergence.v1"
COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")


class ConvergenceError(RuntimeError):
    pass


def _load_json(path: Path, *, label: str) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ConvergenceError(f"{label} evidence is missing or unsafe")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConvergenceError(f"{label} evidence is unreadable or invalid JSON") from exc


def _timestamp(value: Any) -> dt.datetime:
    if not isinstance(value, str) or not value:
        raise ConvergenceError("accepted Schauwerk workflow run timestamp is missing")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConvergenceError("accepted Schauwerk workflow run timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ConvergenceError("accepted Schauwerk workflow run timestamp lacks timezone")
    return parsed.astimezone(dt.timezone.utc)


def latest_accepted_release(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("workflow_runs"), list):
        raise ConvergenceError("Schauwerk workflow evidence shape mismatch")
    accepted: list[tuple[dt.datetime, int, dict[str, Any]]] = []
    for run in payload["workflow_runs"]:
        if not isinstance(run, dict):
            continue
        if run.get("event") != "workflow_dispatch" or run.get("conclusion") != "success":
            continue
        if run.get("status") != "completed":
            raise ConvergenceError("successful Schauwerk publish run is not completed")
        head = run.get("head_sha")
        run_id = run.get("id")
        if not isinstance(head, str) or COMMIT_RE.fullmatch(head) is None:
            raise ConvergenceError("accepted Schauwerk publish run head SHA is invalid")
        if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
            raise ConvergenceError("accepted Schauwerk publish run id is invalid")
        accepted.append((_timestamp(run.get("created_at")), run_id, run))
    if not accepted:
        raise ConvergenceError("no accepted Schauwerk native Schaubild publish exists")
    _created, _id, run = max(accepted, key=lambda item: (item[0], item[1]))
    return {
        "source_commit": run["head_sha"],
        "workflow_run_id": run["id"],
        "created_at": run["created_at"],
    }


def evaluate_convergence(lock_path: Path, workflow_payload: Any) -> dict[str, Any]:
    try:
        lock = verify_runtime_lock(lock_path)
    except ReleaseContractError as exc:
        raise ConvergenceError(str(exc)) from exc
    desired = latest_accepted_release(workflow_payload)
    state = (
        "current"
        if lock["source_commit"] == desired["source_commit"]
        else "promotion_pending"
    )
    return {
        "schema_version": SCHEMA,
        "state": state,
        "locked_source_commit": lock["source_commit"],
        "locked_image_digest": lock["image_digest"],
        "desired_source_commit": desired["source_commit"],
        "desired_workflow_run_id": desired["workflow_run_id"],
        "desired_published_at": desired["created_at"],
        "action_required": state != "current",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--workflow-runs", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        payload = _load_json(args.workflow_runs, label="Schauwerk workflow")
        receipt = evaluate_convergence(args.lock, payload)
        args.output.write_text(
            json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
    except (ConvergenceError, OSError) as exc:
        failure = {
            "schema_version": SCHEMA,
            "state": "invalid",
            "action_required": True,
            "error": str(exc),
        }
        try:
            args.output.write_text(
                json.dumps(failure, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass
        print(f"ERROR: Schaubild release convergence check failed: {exc}", file=__import__("sys").stderr)
        return 1
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0 if receipt["state"] == "current" else 2


if __name__ == "__main__":
    raise SystemExit(main())