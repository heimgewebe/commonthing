from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import threading

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


def test_build_plan_binds_semantics_and_preimage_to_same_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = _lock(tmp_path)
    before = lock.read_bytes()
    real_snapshot = MODULE.read_runtime_lock_snapshot
    snapshot_calls = 0

    def change_after_snapshot(path: Path) -> tuple[bytes, dict[str, str]]:
        nonlocal snapshot_calls
        snapshot = real_snapshot(path)
        snapshot_calls += 1
        if snapshot_calls == 1:
            _lock(tmp_path, NEW, NEW_DIGEST)
        return snapshot

    monkeypatch.setattr(MODULE, "read_runtime_lock_snapshot", change_after_snapshot)
    plan = MODULE.build_plan(lock, _workflow(), _packages(), _image_identity())
    assert plan["current_source_commit"] == OLD
    assert plan["current_image_digest"] == OLD_DIGEST
    assert plan["lock_preimage_sha256"] == MODULE._sha256_bytes(before)
    with pytest.raises(MODULE.PromotionError, match="preimage changed"):
        MODULE.apply_plan(lock, plan, expected_plan_sha256=plan["plan_sha256"])


def test_concurrent_apply_serializes_preimage_check_with_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = _lock(tmp_path)
    plan = MODULE.build_plan(lock, _workflow(), _packages(), _image_identity())
    real_replace = MODULE.os.replace
    first_at_replace = threading.Event()
    release_first = threading.Event()
    second_done = threading.Event()
    counter_lock = threading.Lock()
    replace_calls = 0
    outcomes: list[object] = []

    def controlled_replace(source: str, target: str) -> None:
        nonlocal replace_calls
        with counter_lock:
            replace_calls += 1
            call = replace_calls
        if call == 1:
            first_at_replace.set()
            assert release_first.wait(2)
        real_replace(source, target)

    monkeypatch.setattr(MODULE.os, "replace", controlled_replace)

    def apply(*, done: threading.Event | None = None) -> None:
        try:
            outcomes.append(
                MODULE.apply_plan(
                    lock,
                    plan,
                    expected_plan_sha256=plan["plan_sha256"],
                )
            )
        except Exception as exc:  # captured for deterministic cross-thread assertion
            outcomes.append(exc)
        finally:
            if done is not None:
                done.set()

    first = threading.Thread(target=apply)
    first.start()
    assert first_at_replace.wait(2)

    second = threading.Thread(target=apply, kwargs={"done": second_done})
    second.start()
    assert not second_done.wait(0.1)

    release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)
    assert not first.is_alive()
    assert not second.is_alive()
    assert outcomes.count("updated") == 1
    failures = [item for item in outcomes if isinstance(item, MODULE.PromotionError)]
    assert len(failures) == 1
    assert "preimage changed" in str(failures[0])


def test_plan_hash_is_required_for_apply(tmp_path: Path) -> None:
    lock = _lock(tmp_path)
    plan = MODULE.build_plan(lock, _workflow(), _packages(), _image_identity())
    with pytest.raises(MODULE.PromotionError, match="plan hash mismatch"):
        MODULE.apply_plan(lock, plan, expected_plan_sha256="0" * 64)
