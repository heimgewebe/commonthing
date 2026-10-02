from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest

PREFLIGHT = Path(__file__).resolve().parents[2] / "preflight"
sys.path.insert(0, str(PREFLIGHT))
MODULE_PATH = PREFLIGHT / "schauwerk_editor_promotion.py"
SPEC = importlib.util.spec_from_file_location("schauwerk_editor_promotion", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

OLD = "a" * 40
NEW = "c" * 40
OLD_DIGEST = "sha256:" + "b" * 64
NEW_DIGEST = "sha256:" + "d" * 64
IMAGE_REPO = "ghcr.io/heimgewebe/schauwerk-schaubild"


def _lock(tmp_path: Path, source: str = OLD, digest: str = OLD_DIGEST) -> Path:
    path = tmp_path / "release-lock.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "weltgewebe-schauwerk-runtime-lock.v1",
                "source_repository": "heimgewebe/schauwerk",
                "source_commit": source,
                "image_repository": IMAGE_REPO,
                "image_digest": digest,
                "public_base_path": "/schaubild",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _workflow() -> dict[str, object]:
    return {
        "workflow_runs": [
            {
                "id": 37027702272,
                "event": "workflow_dispatch",
                "status": "completed",
                "conclusion": "success",
                "head_sha": NEW,
                "created_at": "2026-10-02T15:32:32Z",
            }
        ]
    }


def _packages(digest: str = NEW_DIGEST) -> list[dict[str, object]]:
    return [
        {
            "id": 1327810233,
            "name": digest,
            "metadata": {"container": {"tags": [NEW]}},
        }
    ]


def _image_identity(
    *,
    digest: str = NEW_DIGEST,
    revision: str = NEW,
    source: str = "https://github.com/heimgewebe/schauwerk",
) -> dict[str, object]:
    return {
        "image_ref": f"{IMAGE_REPO}@{digest}",
        "labels": {
            "org.opencontainers.image.revision": revision,
            "org.opencontainers.image.source": source,
        },
    }


def test_new_release_plan_and_apply_updates_exact_lock(tmp_path: Path) -> None:
    lock = _lock(tmp_path)
    plan = MODULE.build_plan(lock, _workflow(), _packages(), _image_identity())
    assert plan["action"] == "update"
    assert plan["source_commit"] == NEW
    assert plan["image_digest"] == NEW_DIGEST
    result = MODULE.apply_plan(lock, plan, expected_plan_sha256=plan["plan_sha256"])
    assert result == "updated"
    payload = json.loads(lock.read_text(encoding="utf-8"))
    assert payload["source_commit"] == NEW
    assert payload["image_digest"] == NEW_DIGEST


def test_already_current_plan_is_noop_and_preserves_bytes(tmp_path: Path) -> None:
    lock = _lock(tmp_path, NEW, NEW_DIGEST)
    before = lock.read_bytes()
    plan = MODULE.build_plan(lock, _workflow(), _packages(), _image_identity())
    assert plan["action"] == "noop"
    assert MODULE.apply_plan(lock, plan, expected_plan_sha256=plan["plan_sha256"]) == "noop"
    assert lock.read_bytes() == before


@pytest.mark.parametrize(
    "identity",
    [
        _image_identity(revision=OLD),
        _image_identity(source="https://github.com/other/repo"),
        _image_identity(digest=OLD_DIGEST),
    ],
)
def test_commit_digest_or_source_mismatch_fails_closed(
    tmp_path: Path, identity: dict[str, object]
) -> None:
    with pytest.raises(MODULE.PromotionError):
        MODULE.build_plan(_lock(tmp_path), _workflow(), _packages(), identity)


def test_missing_image_fails_without_lock_update(tmp_path: Path) -> None:
    lock = _lock(tmp_path)
    before = lock.read_bytes()
    with pytest.raises(MODULE.PromotionError, match="no GHCR"):
        MODULE.build_plan(lock, _workflow(), [], _image_identity())
    assert lock.read_bytes() == before


def test_concurrent_lock_update_rejects_stale_plan(tmp_path: Path) -> None:
    lock = _lock(tmp_path)
    plan = MODULE.build_plan(lock, _workflow(), _packages(), _image_identity())
    lock.write_text(lock.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(MODULE.PromotionError, match="preimage changed"):
        MODULE.apply_plan(lock, plan, expected_plan_sha256=plan["plan_sha256"])


def test_plan_hash_is_required_for_apply(tmp_path: Path) -> None:
    lock = _lock(tmp_path)
    plan = MODULE.build_plan(lock, _workflow(), _packages(), _image_identity())
    with pytest.raises(MODULE.PromotionError, match="plan hash mismatch"):
        MODULE.apply_plan(lock, plan, expected_plan_sha256="0" * 64)
