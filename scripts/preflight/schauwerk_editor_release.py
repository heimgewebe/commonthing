#!/usr/bin/env python3
"""Fail-closed verifier for the separately versioned Schaubild native runtime."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

LOCK_SCHEMA = "weltgewebe-schauwerk-runtime-lock.v1"
SOURCE_REPOSITORY = "heimgewebe/schauwerk"
IMAGE_REPOSITORY = "ghcr.io/heimgewebe/schauwerk-schaubild"
PUBLIC_BASE_PATH = "/schaubild"
LOCK_KEYS = {
    "schema_version",
    "source_repository",
    "source_commit",
    "image_repository",
    "image_digest",
    "public_base_path",
}
COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
IMAGE_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


class ReleaseContractError(RuntimeError):
    pass


def verify_runtime_lock(lock_path: Path) -> dict[str, str]:
    lock_path = lock_path.expanduser().absolute()
    if lock_path.is_symlink() or not lock_path.is_file():
        raise ReleaseContractError(f"runtime lock is missing or unsafe: {lock_path}")
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseContractError("runtime lock is unreadable or invalid JSON") from exc
    if not isinstance(lock, dict) or set(lock) != LOCK_KEYS:
        raise ReleaseContractError("runtime lock shape mismatch")
    if lock.get("schema_version") != LOCK_SCHEMA:
        raise ReleaseContractError("runtime lock schema mismatch")
    if lock.get("source_repository") != SOURCE_REPOSITORY:
        raise ReleaseContractError("runtime lock source repository mismatch")

    source_commit = lock.get("source_commit")
    image_repository = lock.get("image_repository")
    image_digest = lock.get("image_digest")
    public_base_path = lock.get("public_base_path")
    if not isinstance(source_commit, str) or COMMIT_RE.fullmatch(source_commit) is None:
        raise ReleaseContractError("runtime lock source commit is invalid")
    if image_repository != IMAGE_REPOSITORY:
        raise ReleaseContractError("runtime lock image repository mismatch")
    if not isinstance(image_digest, str) or IMAGE_DIGEST_RE.fullmatch(image_digest) is None:
        raise ReleaseContractError("runtime lock image digest is invalid")
    if public_base_path != PUBLIC_BASE_PATH:
        raise ReleaseContractError("runtime lock public base path mismatch")

    return {
        "source_repository": SOURCE_REPOSITORY,
        "source_commit": source_commit,
        "image_repository": IMAGE_REPOSITORY,
        "image_digest": image_digest,
        "image_ref": f"{IMAGE_REPOSITORY}@{image_digest}",
        "public_base_path": PUBLIC_BASE_PATH,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        result = verify_runtime_lock(args.lock)
    except ReleaseContractError as exc:
        print(
            f"ERROR: Schaubild runtime lock preflight failed: {exc}",
            file=__import__("sys").stderr,
        )
        return 1
    if args.json:
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    else:
        print(
            "schauwerk_runtime_preflight=pass "
            f"source_commit={result['source_commit']} "
            f"image_ref={result['image_ref']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
