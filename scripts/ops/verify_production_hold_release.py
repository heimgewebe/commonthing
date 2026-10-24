#!/usr/bin/env python3
"""Fail-closed, read-only authorization check for production reconciliation.

The root-owned authorization is written *outside* this repository only after
backup, offline restore, privacy, alert delivery and human acknowledgment
have independently passed. Digest references are not independent proofs.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from datetime import datetime
from pathlib import Path

KIND = "weltgewebe.production-hold-release.v1"
SHA40 = re.compile(r"[0-9a-f]{40}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
RECEIPTS = (
    "offhost_age_recovery_sha256",
    "restic_authority_sha256",
    "privacy_acceptance_sha256",
    "alarm_delivery_sha256",
    "human_acknowledgement_sha256",
)
FIELDS = frozenset({
    "schema_version", "kind", "environment", "decision", "commit",
    "schaubild_image_digest", "authorized_by", "approved_at", *RECEIPTS,
})


class HoldReleaseError(ValueError):
    """Absent, unsafe, malformed or nonmatching release authorization."""


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise HoldReleaseError("duplicate authorization JSON key")
        result[key] = value
    return result


def _check_directory(fd: int, uid: int) -> None:
    mode = os.fstat(fd)
    if (not stat.S_ISDIR(mode.st_mode) or mode.st_uid != uid
            or (stat.S_IMODE(mode.st_mode) & 0o022)):
        raise HoldReleaseError("authorization directory is not securely owned")


def read_authorization(
    root: Path = Path("/etc"), *, expected_uid: int = 0,
    expected_commit: str | None = None, expected_image: str | None = None,
) -> dict[str, object]:
    """Open /etc/weltgewebe/production-hold-release.json without following links.

    The root/owner parameter is private to direct unit tests; the CLI has no
    path or uid override and always uses /etc and uid 0.
    """
    if expected_commit is not None and not SHA40.fullmatch(expected_commit):
        raise HoldReleaseError("expected commit must be a complete lowercase SHA")
    if expected_image is not None and not DIGEST.fullmatch(expected_image):
        raise HoldReleaseError("expected image digest must be an immutable SHA-256")

    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    dir_flags = flags | os.O_DIRECTORY
    root_fd = -1
    parent_fd = -1
    file_fd = -1
    try:
        root_fd = os.open(root, dir_flags)
        _check_directory(root_fd, expected_uid)
        parent_fd = os.open("weltgewebe", dir_flags, dir_fd=root_fd)
        _check_directory(parent_fd, expected_uid)
        file_fd = os.open("production-hold-release.json", flags, dir_fd=parent_fd)
        before = os.fstat(file_fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != expected_uid
                or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) != 0o600
                or not (1 <= before.st_size <= 4096)):
            raise HoldReleaseError("authorization must be an owner-only regular file")
        raw = os.read(file_fd, 4097)
        after = os.fstat(file_fd)
        if (len(raw) != before.st_size or len(raw) > 4096 or
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)):
            raise HoldReleaseError("authorization changed during read")
    except OSError as exc:
        raise HoldReleaseError("production HOLD authorization absent or unsafe") from exc
    finally:
        for fd in (file_fd, parent_fd, root_fd):
            if fd >= 0:
                os.close(fd)
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise HoldReleaseError("authorization JSON is invalid") from exc
    if not isinstance(data, dict) or set(data) != FIELDS:
        raise HoldReleaseError("authorization field matrix is incomplete or unexpected")
    if (type(data["schema_version"]) is not int or data["schema_version"] != 1
            or data["kind"] != KIND or data["environment"] != "production"
            or data["decision"] != "release"):
        raise HoldReleaseError("production HOLD is not explicitly released")
    commit = data["commit"]
    if not isinstance(commit, str) or not SHA40.fullmatch(commit):
        raise HoldReleaseError("approved commit is invalid")
    if expected_commit is not None and commit != expected_commit:
        raise HoldReleaseError("requested commit differs from approved release")
    image = data["schaubild_image_digest"]
    if not isinstance(image, str) or not DIGEST.fullmatch(image):
        raise HoldReleaseError("approved OCI digest is invalid")
    if expected_image is not None and image != expected_image:
        raise HoldReleaseError("Schaubild OCI digest differs from approved release")
    for name in RECEIPTS:
        value = data[name]
        if not isinstance(value, str) or not SHA256.fullmatch(value):
            raise HoldReleaseError(f"required release receipt invalid: {name}")
    who = data["authorized_by"]
    if not isinstance(who, str) or not re.fullmatch(r"[A-Za-z0-9_.@:-]{2,128}", who):
        raise HoldReleaseError("release authorizer is not identified")
    approved_at = data["approved_at"]
    if not isinstance(approved_at, str):
        raise HoldReleaseError("release authorization timestamp missing")
    try:
        parsed = datetime.fromisoformat(approved_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HoldReleaseError("release authorization timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HoldReleaseError("release authorization timestamp lacks timezone")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--commit")
    parser.add_argument("--image-digest")
    args = parser.parse_args(argv)
    if args.image_digest is not None and args.commit is None:
        parser.error("--image-digest requires --commit")
    if os.geteuid() != 0:
        print("production HOLD release verifier must run as root", file=sys.stderr)
        return 1
    try:
        read_authorization(expected_commit=args.commit, expected_image=args.image_digest)
    except HoldReleaseError as exc:
        print(f"production_hold_release=blocked: {exc}", file=sys.stderr)
        return 1
    print("production_hold_release=verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
