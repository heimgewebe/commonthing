#!/usr/bin/env python3
"""Fail-closed, read-only authorization check for production reconciliation.

The root-owned authorization binds the exact source/OCI release to genuine
backup and restored-offhost evidence. Privacy and alert operations are monitored
separately; extra acknowledgment hashes do not block a working deployment.
Digest references alone are not independent recovery proofs.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from datetime import datetime
from pathlib import Path

KIND = "weltgewebe.production-hold-release.v1"
SHA40 = re.compile(r"[0-9a-f]{40}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
REQUIRED_RECEIPTS = (
    "offhost_age_recovery_sha256",
    "restic_authority_sha256",
)
# Existing root authorizations with older operational receipts remain valid.
# New releases need actual recovery instead of five independent sign-off gates.
OPTIONAL_RECEIPTS = (
    "privacy_acceptance_sha256",
    "alarm_delivery_sha256",
    "human_acknowledgement_sha256",
)
RECEIPTS = REQUIRED_RECEIPTS + OPTIONAL_RECEIPTS
FIELDS = frozenset({
    "schema_version", "kind", "environment", "decision", "commit",
    "schaubild_image_digest", "authorized_by", "approved_at", *RECEIPTS,
})
REQUIRED_FIELDS = FIELDS - frozenset(OPTIONAL_RECEIPTS)


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
    if (not isinstance(data, dict) or not REQUIRED_FIELDS <= data.keys()
            or not data.keys() <= FIELDS):
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
        if name not in data:
            continue
        value = data[name]
        if not isinstance(value, str) or not SHA256.fullmatch(value):
            raise HoldReleaseError(f"release receipt invalid: {name}")
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


LOCK_SOURCE_PATH = "infra/schauwerk-editor/release-lock.json"
LOCK_SOURCE_FIELDS = frozenset({
    "schema_version", "source_repository", "source_commit",
    "image_repository", "image_digest", "public_base_path",
})


def read_locked_image_digest(
    source_repository: Path, commit: str, *, expected_uid: int = 0,
) -> str:
    """Read the exact Git object, not a mutable worktree or caller-supplied digest.

    The commit object must already exist locally. The production reconciler
    may fetch immutable objects without updating any remote-tracking refs,
    but only after the root HOLD authorizes the exact remote commit.
    """
    if not SHA40.fullmatch(commit):
        raise HoldReleaseError("source commit must be an exact lowercase SHA")
    repository = Path(source_repository)
    if not repository.is_absolute():
        raise HoldReleaseError("source repository must be an absolute directory")
    try:
        info = repository.lstat()
    except OSError as exc:
        raise HoldReleaseError("source repository is unavailable") from exc
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != expected_uid
            or stat.S_IMODE(info.st_mode) & 0o022):
        raise HoldReleaseError("source repository is not securely owned")

    # Never consult inherited Git aliases, replacements, a user config or
    # Git environment overrides while opening a root-authorized commit.
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith("GIT_")
    }
    environment.update({
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
    })
    source = f"{commit}:{LOCK_SOURCE_PATH}"
    command = ["git", "--no-optional-locks", "-C", str(repository)]
    # A trusted repo directory alone is insufficient: objects/info/alternates
    # can redirect object lookups to a writable store without a SHA collision.
    # Verify every effective on-disk object entry before reading release bytes.
    try:
        common_output = subprocess.run(
            [*command, "rev-parse", "--git-common-dir"],
            check=False, capture_output=True, text=True, timeout=20,
            env=environment,
        )
        if common_output.returncode != 0 or not common_output.stdout.strip():
            raise HoldReleaseError("Git common object directory is unavailable")
        common_dir = Path(common_output.stdout.strip())
        if not common_dir.is_absolute():
            common_dir = repository / common_dir
        common_dir = common_dir.resolve(strict=True)
        common_mode = common_dir.stat()
        if (not stat.S_ISDIR(common_mode.st_mode)
                or common_mode.st_uid != expected_uid
                or stat.S_IMODE(common_mode.st_mode) & 0o022):
            raise HoldReleaseError("Git common object directory is not securely owned")
        objects = common_dir / "objects"
        if not objects.is_dir() or objects.is_symlink():
            raise HoldReleaseError("Git object directory is missing or redirected")
        alternates = objects / "info" / "alternates"
        if alternates.exists() or alternates.is_symlink():
            raise HoldReleaseError("Git alternate object stores are forbidden")

        def walk_error(error: OSError) -> None:
            raise HoldReleaseError("Git object store traversal failed") from error

        for directory, subdirs, files in os.walk(
            objects, followlinks=False, onerror=walk_error,
        ):
            for name in (*subdirs, *files):
                entry = Path(directory) / name
                metadata = entry.lstat()
                if (stat.S_ISLNK(metadata.st_mode)
                        or metadata.st_uid != expected_uid
                        or stat.S_IMODE(metadata.st_mode) & 0o022):
                    raise HoldReleaseError("Git object store contains an untrusted entry")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HoldReleaseError("Git object store inspection failed") from exc
    try:
        size = subprocess.run(
            [*command, "cat-file", "-s", source],
            check=False, capture_output=True, text=True, timeout=20,
            env=environment,
        )
        if size.returncode != 0:
            raise HoldReleaseError("source commit lock is unavailable")
        try:
            blob_size = int(size.stdout.strip())
        except ValueError as exc:
            raise HoldReleaseError("source commit lock size is invalid") from exc
        if not 1 <= blob_size <= 4096:
            raise HoldReleaseError("source commit lock exceeds admission budget")
        result = subprocess.run(
            [*command, "show", source],
            check=False, capture_output=True, timeout=20, env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HoldReleaseError("source commit lock inspection failed") from exc
    if result.returncode != 0 or len(result.stdout) != blob_size:
        raise HoldReleaseError("source commit lock bytes could not be verified")
    try:
        record = json.loads(
            result.stdout.decode("utf-8"), object_pairs_hook=_unique_pairs,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise HoldReleaseError("source commit lock JSON is invalid") from exc
    if not isinstance(record, dict) or set(record) != LOCK_SOURCE_FIELDS:
        raise HoldReleaseError("source commit lock fields are invalid")
    if (record["schema_version"] != "weltgewebe-schauwerk-runtime-lock.v1"
            or record["source_repository"] != "heimgewebe/schauwerk"
            or record["image_repository"] != "ghcr.io/heimgewebe/schauwerk-schaubild"
            or record["public_base_path"] != "/schaubild"
            or not isinstance(record["source_commit"], str)
            or not SHA40.fullmatch(record["source_commit"])):
        raise HoldReleaseError("source commit lock identity is invalid")
    digest = record["image_digest"]
    if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
        raise HoldReleaseError("source commit lock OCI digest is invalid")
    return digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--commit")
    parser.add_argument("--image-digest")
    parser.add_argument("--source-repository")
    args = parser.parse_args(argv)
    if args.image_digest is not None and args.commit is None:
        parser.error("--image-digest requires --commit")
    if args.source_repository is not None and args.commit is None:
        parser.error("--source-repository requires --commit")
    if os.geteuid() != 0:
        print("production HOLD release verifier must run as root", file=sys.stderr)
        return 1
    try:
        locked_digest = (
            read_locked_image_digest(Path(args.source_repository), args.commit)
            if args.source_repository is not None else None
        )
        if (locked_digest is not None and args.image_digest is not None
                and locked_digest != args.image_digest):
            raise HoldReleaseError("provided OCI digest differs from the exact source commit")
        read_authorization(
            expected_commit=args.commit,
            expected_image=locked_digest or args.image_digest,
        )
    except HoldReleaseError as exc:
        print(f"production_hold_release=blocked: {exc}", file=sys.stderr)
        return 1
    print("production_hold_release=verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())