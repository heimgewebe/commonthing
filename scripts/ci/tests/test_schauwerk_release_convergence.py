from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest

PREFLIGHT = Path(__file__).resolve().parents[2] / "preflight"
sys.path.insert(0, str(PREFLIGHT))
MODULE_PATH = PREFLIGHT / "schauwerk_release_convergence.py"
SPEC = importlib.util.spec_from_file_location("schauwerk_release_convergence", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

LOCKED = "a" * 40
NEW = "c" * 40
DIGEST = "sha256:" + "b" * 64


def _lock(tmp_path: Path, source: str = LOCKED) -> Path:
    path = tmp_path / "release-lock.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "weltgewebe-schauwerk-runtime-lock.v1",
                "source_repository": "heimgewebe/schauwerk",
                "source_commit": source,
                "image_repository": "ghcr.io/heimgewebe/schauwerk-schaubild",
                "image_digest": DIGEST,
                "public_base_path": "/schaubild",
            }
        ),
        encoding="utf-8",
    )
    return path


def _runs(head: str = NEW) -> dict[str, object]:
    return {
        "workflow_runs": [
            {
                "id": 22,
                "event": "workflow_dispatch",
                "status": "completed",
                "conclusion": "success",
                "head_sha": head,
                "created_at": "2026-10-02T15:32:32Z",
            },
            {
                "id": 21,
                "event": "workflow_dispatch",
                "status": "completed",
                "conclusion": "success",
                "head_sha": LOCKED,
                "created_at": "2026-09-26T18:09:34Z",
            },
        ]
    }


def test_new_accepted_release_reports_promotion_pending(tmp_path: Path) -> None:
    result = MODULE.evaluate_convergence(_lock(tmp_path), _runs())
    assert result["state"] == "promotion_pending"
    assert result["action_required"] is True
    assert result["desired_source_commit"] == NEW


def test_already_current_is_clean_noop_state(tmp_path: Path) -> None:
    result = MODULE.evaluate_convergence(_lock(tmp_path, NEW), _runs())
    assert result["state"] == "current"
    assert result["action_required"] is False


def test_latest_successful_publish_wins_over_input_order() -> None:
    payload = _runs()
    payload["workflow_runs"] = list(reversed(payload["workflow_runs"]))
    result = MODULE.latest_accepted_release(payload)
    assert result["source_commit"] == NEW
    assert result["workflow_run_id"] == 22


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"workflow_runs": []},
        {
            "workflow_runs": [
                {
                    "id": 1,
                    "event": "push",
                    "status": "completed",
                    "conclusion": "success",
                    "head_sha": NEW,
                    "created_at": "2026-10-02T15:32:32Z",
                }
            ]
        },
        {
            "workflow_runs": [
                {
                    "id": 1,
                    "event": "workflow_dispatch",
                    "status": "completed",
                    "conclusion": "success",
                    "head_sha": "not-a-sha",
                    "created_at": "2026-10-02T15:32:32Z",
                }
            ]
        },
    ],
)
def test_invalid_or_missing_accepted_release_fails_closed(
    payload: dict[str, object],
) -> None:
    with pytest.raises(MODULE.ConvergenceError):
        MODULE.latest_accepted_release(payload)
