#!/usr/bin/env python3
"""Plan and apply exact, CAS-bound Schaubild runtime-lock promotions."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from schauwerk_editor_release import (
    IMAGE_REPOSITORY,
    LOCK_SCHEMA,
    PUBLIC_BASE_PATH,
    SOURCE_REPOSITORY,
    ReleaseContractError,
    verify_image_labels,
    verify_runtime_lock,
)
from schauwerk_release_convergence import ConvergenceError, latest_accepted_release

PLAN_SCHEMA = "weltgewebe-schauwerk-release-promotion-plan.v1"
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


class PromotionError(RuntimeError):
    pass


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_json(path: Path, *, label: str) -> Any:
    if path.is_symlink() or not path.is_file():
        raise PromotionError(f"{label} evidence is missing or unsafe")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PromotionError(f"{label} evidence is unreadable or invalid JSON") from exc


def package_digest_for_commit(payload: Any, source_commit: str) -> tuple[str, int]:
    if not isinstance(payload, list):
        raise PromotionError("GHCR package evidence shape mismatch")
    matches: list[tuple[str, int]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata")
        container = metadata.get("container") if isinstance(metadata, dict) else None
        tags = container.get("tags") if isinstance(container, dict) else None
        if not isinstance(tags, list) or source_commit not in tags:
            continue
        digest = item.get("name")
        version_id = item.get("id")
        if not isinstance(digest, str) or DIGEST_RE.fullmatch(digest) is None:
            raise PromotionError("tagged GHCR package digest is invalid")
        if isinstance(version_id, bool) or not isinstance(version_id, int) or version_id <= 0:
            raise PromotionError("tagged GHCR package version id is invalid")
        matches.append((digest, version_id))
    if not matches:
        raise PromotionError("no GHCR Schaubild image is tagged with accepted source commit")
    digests = {digest for digest, _version in matches}
    if len(digests) != 1:
        raise PromotionError("accepted source commit resolves to multiple GHCR image digests")
    digest = next(iter(digests))
    version_id = max(version for candidate, version in matches if candidate == digest)
    return digest, version_id


def verify_image_evidence(payload: Any, *, image_ref: str, source_commit: str) -> None:
    if not isinstance(payload, dict) or set(payload) != {"image_ref", "labels"}:
        raise PromotionError("OCI image identity evidence shape mismatch")
    if payload.get("image_ref") != image_ref:
        raise PromotionError("OCI image identity evidence is bound to another digest")
    try:
        verify_image_labels(payload.get("labels"), expected_commit=source_commit)
    except ReleaseContractError as exc:
        raise PromotionError(str(exc)) from exc


def build_plan(
    lock_path: Path,
    workflow_payload: Any,
    package_payload: Any,
    image_identity_payload: Any,
) -> dict[str, Any]:
    try:
        current = verify_runtime_lock(lock_path)
        accepted = latest_accepted_release(workflow_payload)
    except (ReleaseContractError, ConvergenceError) as exc:
        raise PromotionError(str(exc)) from exc
    digest, package_version_id = package_digest_for_commit(
        package_payload, accepted["source_commit"]
    )
    image_ref = f"{IMAGE_REPOSITORY}@{digest}"
    verify_image_evidence(
        image_identity_payload,
        image_ref=image_ref,
        source_commit=accepted["source_commit"],
    )
    try:
        lock_bytes = lock_path.read_bytes()
    except OSError as exc:
        raise PromotionError("runtime lock preimage is unreadable") from exc
    action = (
        "noop"
        if current["source_commit"] == accepted["source_commit"]
        and current["image_digest"] == digest
        else "update"
    )
    core = {
        "schema_version": PLAN_SCHEMA,
        "action": action,
        "lock_path": str(lock_path.expanduser().absolute()),
        "lock_preimage_sha256": _sha256_bytes(lock_bytes),
        "current_source_commit": current["source_commit"],
        "current_image_digest": current["image_digest"],
        "source_commit": accepted["source_commit"],
        "image_digest": digest,
        "image_ref": image_ref,
        "workflow_run_id": accepted["workflow_run_id"],
        "package_version_id": package_version_id,
    }
    return {**core, "plan_sha256": _sha256_bytes(_canonical_json(core))}


def _expected_lock(plan: dict[str, Any]) -> dict[str, str]:
    return {
        "schema_version": LOCK_SCHEMA,
        "source_repository": SOURCE_REPOSITORY,
        "source_commit": str(plan["source_commit"]),
        "image_repository": IMAGE_REPOSITORY,
        "image_digest": str(plan["image_digest"]),
        "public_base_path": PUBLIC_BASE_PATH,
    }


def apply_plan(lock_path: Path, plan: Any, *, expected_plan_sha256: str) -> str:
    if not isinstance(plan, dict) or plan.get("schema_version") != PLAN_SCHEMA:
        raise PromotionError("promotion plan shape or schema is invalid")
    recorded = plan.get("plan_sha256")
    core = {key: value for key, value in plan.items() if key != "plan_sha256"}
    computed = _sha256_bytes(_canonical_json(core))
    if recorded != computed or expected_plan_sha256 != computed:
        raise PromotionError("promotion plan hash mismatch")
    resolved = str(lock_path.expanduser().absolute())
    if plan.get("lock_path") != resolved:
        raise PromotionError("promotion plan targets another runtime lock")
    if lock_path.is_symlink() or not lock_path.is_file():
        raise PromotionError("runtime lock is missing or unsafe")
    current_bytes = lock_path.read_bytes()
    if _sha256_bytes(current_bytes) != plan.get("lock_preimage_sha256"):
        raise PromotionError("runtime lock preimage changed after promotion plan")
    try:
        current = verify_runtime_lock(lock_path)
    except ReleaseContractError as exc:
        raise PromotionError(str(exc)) from exc
    expected = _expected_lock(plan)
    action = plan.get("action")
    if action == "noop":
        if (
            current["source_commit"] != expected["source_commit"]
            or current["image_digest"] != expected["image_digest"]
        ):
            raise PromotionError("no-op promotion plan no longer matches runtime lock")
        return "noop"
    if action != "update":
        raise PromotionError("promotion plan action is invalid")

    encoded = json.dumps(expected, indent=2).encode("utf-8") + b"\n"
    fd, temporary = tempfile.mkstemp(prefix=".release-lock.", dir=str(lock_path.parent))
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        if _sha256_bytes(lock_path.read_bytes()) != plan["lock_preimage_sha256"]:
            raise PromotionError("runtime lock changed during promotion apply")
        os.replace(temporary, lock_path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return "updated"


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan")
    plan.add_argument("--lock", required=True, type=Path)
    plan.add_argument("--workflow-runs", required=True, type=Path)
    plan.add_argument("--package-versions", required=True, type=Path)
    plan.add_argument("--image-identity", required=True, type=Path)
    plan.add_argument("--output", required=True, type=Path)

    apply = sub.add_parser("apply")
    apply.add_argument("--lock", required=True, type=Path)
    apply.add_argument("--plan", required=True, type=Path)
    apply.add_argument("--expected-plan-sha256", required=True)

    args = parser.parse_args()
    try:
        if args.command == "plan":
            result = build_plan(
                args.lock,
                _load_json(args.workflow_runs, label="Schauwerk workflow"),
                _load_json(args.package_versions, label="GHCR package"),
                _load_json(args.image_identity, label="OCI image identity"),
            )
            args.output.write_bytes(_canonical_json(result) + b"\n")
            print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        else:
            result = apply_plan(
                args.lock,
                _load_json(args.plan, label="promotion plan"),
                expected_plan_sha256=args.expected_plan_sha256,
            )
            print(f"schauwerk_promotion={result}")
    except (PromotionError, OSError) as exc:
        print(f"ERROR: Schaubild promotion failed: {exc}", file=__import__("sys").stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
