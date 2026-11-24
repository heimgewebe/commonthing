#!/usr/bin/env python3
"""Fail-closed exact authorization and opt-in routine admission for production.

The root-owned authorization binds the exact source/OCI release to genuine
backup and restored-offhost evidence. Privacy and alert operations are monitored
separately; extra acknowledgment hashes do not block a working deployment.
Digest references alone are not independent recovery proofs.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
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
    "schaubild_image_digest", "authorized_by", "approved_at",
    "routine_web_releases", *RECEIPTS,
})
REQUIRED_FIELDS = FIELDS - frozenset((*OPTIONAL_RECEIPTS, "routine_web_releases"))
HOLD_ROOT = Path("/etc")
ROUTINE_FILE = "production-routine-release.json"
ROUTINE_KIND = "weltgewebe.production-routine-release.v1"
ROUTINE_FIELDS = frozenset({
    "kind", "schema_version", "grant_sha256", "commit",
    "schaubild_image_digest", "ci_run_id", "admitted_at",
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


def _read_protected_json(root: Path, filename: str, expected_uid: int, *,
                         missing_ok: bool = False) -> dict[str, object] | None:
    """Read a single root-controlled JSON object without following links."""
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
        file_fd = os.open(filename, flags, dir_fd=parent_fd)
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
    except FileNotFoundError:
        if missing_ok:
            return None
        raise HoldReleaseError("production HOLD authorization absent") from None
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
    if not isinstance(data, dict):
        raise HoldReleaseError("authorization JSON is not an object")
    return data


def read_authorization(
    root: Path = HOLD_ROOT, *, expected_uid: int = 0,
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

    data = _read_protected_json(root, "production-hold-release.json", expected_uid)
    assert data is not None
    if (not isinstance(data, dict) or not REQUIRED_FIELDS <= data.keys()
            or not data.keys() <= FIELDS):
        raise HoldReleaseError("authorization field matrix is incomplete or unexpected")
    if (type(data["schema_version"]) is not int or data["schema_version"] != 1
            or data["kind"] != KIND or data["environment"] != "production"
            or data["decision"] != "release"):
        raise HoldReleaseError("production HOLD is not explicitly released")
    if "routine_web_releases" in data and type(data["routine_web_releases"]) is not bool:
        raise HoldReleaseError("routine web release opt-in must be boolean")
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



def _grant_sha256(grant: dict[str, object]) -> str:
    return hashlib.sha256(json.dumps(
        grant, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()


def _routine_receipt(root: Path, grant: dict[str, object],
                     expected_uid: int) -> dict[str, object] | None:
    receipt = _read_protected_json(root, ROUTINE_FILE, expected_uid, missing_ok=True)
    if receipt is None:
        return None
    if (set(receipt) != ROUTINE_FIELDS or type(receipt["schema_version"]) is not int
            or receipt["schema_version"] != 1 or receipt["kind"] != ROUTINE_KIND
            or not isinstance(receipt["grant_sha256"], str)
            or not SHA256.fullmatch(receipt["grant_sha256"])
            or not isinstance(receipt["commit"], str)
            or not SHA40.fullmatch(receipt["commit"])
            or not isinstance(receipt["schaubild_image_digest"], str)
            or not DIGEST.fullmatch(receipt["schaubild_image_digest"])
            or type(receipt["ci_run_id"]) is not int or receipt["ci_run_id"] <= 0
            or not isinstance(receipt["admitted_at"], str)):
        raise HoldReleaseError("routine release receipt is malformed")
    try:
        admitted_at = datetime.fromisoformat(receipt["admitted_at"])
        if admitted_at.tzinfo is None or admitted_at.utcoffset() is None:
            raise ValueError("timezone missing")
    except ValueError as exc:
        raise HoldReleaseError("routine admission timestamp invalid") from exc
    if (receipt["grant_sha256"] != _grant_sha256(grant)
            or receipt["schaubild_image_digest"] != grant["schaubild_image_digest"]):
        return None  # changing the operator decision invalidates every previous admission
    return receipt


def read_effective_authorization(
    root: Path = HOLD_ROOT, *, expected_uid: int = 0,
    expected_commit: str | None = None, expected_image: str | None = None,
) -> dict[str, object]:
    """Exact manual approval or exact derived approval under a live root grant."""
    grant = read_authorization(root, expected_uid=expected_uid, expected_image=expected_image)
    if expected_commit is None or expected_commit == grant["commit"]:
        return grant
    if not SHA40.fullmatch(expected_commit) or grant.get("routine_web_releases") is not True:
        raise HoldReleaseError("requested commit differs from approved release")
    receipt = _routine_receipt(root, grant, expected_uid)
    if receipt is None or receipt["commit"] != expected_commit:
        raise HoldReleaseError("requested commit has no exact routine admission")
    return {**grant, "commit": expected_commit}


def _routine_git(repository: Path, *args: str) -> bytes:
    # read_locked_image_digest has already checked the root-owned object store.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1",
               GIT_NO_REPLACE_OBJECTS="1", GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(repository), *args],
            capture_output=True, check=False, timeout=20, env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HoldReleaseError("routine source inspection unavailable") from exc
    if result.returncode != 0:
        raise HoldReleaseError("routine source is not a verified descendant")
    return result.stdout


def verify_routine_source(repository: Path, grant: dict[str, object], target: str,
                          *, expected_uid: int = 0) -> None:
    anchor = str(grant["commit"])
    if not SHA40.fullmatch(target) or target == anchor:
        raise HoldReleaseError("routine target must be a new full commit")
    for commit in (anchor, target):
        if (read_locked_image_digest(repository, commit, expected_uid=expected_uid)
                != grant["schaubild_image_digest"]):
            raise HoldReleaseError("routine release changes the approved Schaubild identity")
    _routine_git(repository, "merge-base", "--is-ancestor", anchor, target)
    raw = _routine_git(repository, "diff", "--raw", "-z", "--no-renames",
                       "--no-ext-diff", "--no-textconv", anchor, target, "--")
    entries = raw.split(b"\0")
    if entries[-1] != b"" or len(entries) % 2 != 1:
        raise HoldReleaseError("routine release has no unambiguous changed file set")
    for metadata, path in zip(entries[0:-1:2], entries[1:-1:2], strict=True):
        fields = metadata.split()
        if (len(fields) != 5 or fields[0] not in (b":000000", b":100644")
                or fields[1] not in (b"000000", b"100644") or fields[4] not in (b"A", b"D", b"M")):
            raise HoldReleaseError("routine release contains a non-regular file change")
        if (path.startswith((b"apps/web/src/lib/auth/", b"apps/web/src/routes/auth/",
                             b"apps/web/src/routes/login/", b"apps/web/src/routes/_dev/auth/"))
                or path in (b"apps/web/src/hooks.client.ts", b"apps/web/src/service-worker.ts",
                            b"apps/web/src/lib/components/TopBarAuth.svelte")):
            raise HoldReleaseError("routine release changes authentication-sensitive code")
        if not (path.startswith((b"apps/web/src/", b"apps/web/tests/"))
                or path == b"policies/performance.v1.json"):
            raise HoldReleaseError("routine release changes a non-UI path; exact approval required")


def select_main_ci_run(payload: object, commit: str) -> int:
    """Use only the newest exact push run of the unchanged main CI workflow."""
    if not isinstance(payload, dict) or not isinstance(payload.get("workflow_runs"), list):
        raise HoldReleaseError("main CI response invalid")
    runs = payload["workflow_runs"]
    exact = [
        run for run in runs if isinstance(run, dict)
        and run.get("head_sha") == commit and run.get("head_branch") == "main"
        and run.get("event") == "push" and run.get("path") == ".github/workflows/ci.yml"
        and isinstance(run.get("repository"), dict)
        and run["repository"].get("full_name") == "heimgewebe/commonthing"
        and isinstance(run.get("head_repository"), dict)
        and run["head_repository"].get("full_name") == "heimgewebe/commonthing"
        and type(run.get("run_number")) is int and type(run.get("run_attempt")) is int
    ]
    if not exact:
        raise HoldReleaseError("exact main CI run missing")
    latest = max(exact, key=lambda run: (run["run_number"], run["run_attempt"]))
    if (latest.get("status") != "completed" or latest.get("conclusion") != "success"
            or type(latest.get("id")) is not int or latest["id"] <= 0):
        raise HoldReleaseError("exact main CI has not succeeded")
    return latest["id"]


def verify_main_ci(commit: str) -> int:
    if not SHA40.fullmatch(commit):
        raise HoldReleaseError("invalid main CI target")
    url = ("https://api.github.com/repos/heimgewebe/commonthing/actions/workflows/ci.yml/runs"
           f"?head_sha={commit}&branch=main&event=push&per_page=5")
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json", "User-Agent": "commonthing-production-reconciler",
    })
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            raw = response.read(1_048_577)
        if len(raw) > 1_048_576:
            raise HoldReleaseError("main CI response exceeds budget")
        payload = json.loads(raw)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise HoldReleaseError("main CI evidence unavailable") from exc
    return select_main_ci_run(payload, commit)


def verify_local_recovery(backup_root: Path = Path("/var/backups/weltgewebe/postgres"),
                          *, expected_uid: int = 0, now: datetime | None = None) -> None:
    """Use the existing successful backup/restore manifests and documented freshness windows."""
    now = now or datetime.now(timezone.utc)
    for directory in (backup_root, backup_root / "proofs"):
        try:
            info = directory.lstat()
        except OSError as exc:
            raise HoldReleaseError("recovery directory unavailable") from exc
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != expected_uid
                or stat.S_IMODE(info.st_mode) & 0o022):
            raise HoldReleaseError("unsafe recovery directory")

    def read_record(path: Path) -> dict[str, str]:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != expected_uid
                or stat.S_IMODE(info.st_mode) & 0o022 or not 1 <= info.st_size <= 4096):
            raise HoldReleaseError("unsafe recovery record")
        lines = path.read_text(encoding="utf-8").splitlines()
        pairs = [line.split("=", 1) for line in lines]
        if any(len(pair) != 2 for pair in pairs) or len(dict(pairs)) != len(pairs):
            raise HoldReleaseError("invalid recovery record")
        return dict(pairs)

    def recent(record: dict[str, str], hours: int) -> bool:
        parsed = datetime.fromisoformat(record["created_at"].replace("Z", "+00:00"))
        return 0 <= (now - parsed).total_seconds() <= hours * 3600

    try:
        backups = sorted((path for path in backup_root.glob("weltgewebe-postgres-*.sha256.manifest")
                          if re.fullmatch(r"weltgewebe-postgres-\d{8}T\d{6}Z\.sha256\.manifest", path.name)),
                         reverse=True)
        proofs = sorted((backup_root / "proofs").glob("weltgewebe-postgres-*.restore-proof"), reverse=True)
        if not backups or not proofs:
            raise HoldReleaseError("backup or restore evidence absent")
        backup, proof = read_record(backups[0]), read_record(proofs[0])
        expected_backup = backups[0].name.replace(".sha256.manifest", ".sql.gz")
        expected_restored = proofs[0].name.replace(".restore-proof", ".sql.gz")
        if (backup.get("contract") != "weltgewebe-postgres-backup-v1"
                or proof.get("contract") != "weltgewebe-postgres-restore-proof-v1"
                or proof.get("result") != "ok"
                or not recent(backup, 26) or not recent(proof, 192)
                or not SHA256.fullmatch(backup.get("sha256", ""))
                or not SHA256.fullmatch(proof.get("backup_sha256", ""))
                or backup.get("file") != expected_backup
                or proof.get("backup_file") != expected_restored):
            raise HoldReleaseError("backup or restore evidence is stale or invalid")
        backup_path = backup_root / backup["file"]
        data = backup_path.lstat()
        if (not stat.S_ISREG(data.st_mode) or data.st_uid != expected_uid
                or stat.S_IMODE(data.st_mode) & 0o022
                or data.st_size <= 0 or data.st_size != int(backup["bytes"])):
            raise HoldReleaseError("current backup file does not match its manifest")
        with backup_path.open("rb") as handle:
            if hashlib.file_digest(handle, "sha256").hexdigest() != backup["sha256"]:
                raise HoldReleaseError("current backup digest does not match its manifest")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise HoldReleaseError("backup or restore evidence unavailable") from exc


def admit_routine_release(
    repository: Path, target: str, *, root: Path = HOLD_ROOT,
    expected_uid: int = 0,
) -> dict[str, object]:
    """Called only by the lock-holding reconciler; never overwrite the operator's HOLD."""
    grant = read_authorization(root, expected_uid=expected_uid)
    if grant.get("routine_web_releases") is not True:
        raise HoldReleaseError("routine releases not explicitly authorized")
    verify_routine_source(repository, grant, target, expected_uid=expected_uid)
    previous = _routine_receipt(root, grant, expected_uid)
    if previous is not None:
        _routine_git(repository, "merge-base", "--is-ancestor", str(previous["commit"]), target)
    verify_local_recovery(expected_uid=expected_uid)
    ci_run = verify_main_ci(target)
    receipt = {
        "kind": ROUTINE_KIND, "schema_version": 1, "grant_sha256": _grant_sha256(grant),
        "commit": target, "schaubild_image_digest": grant["schaubild_image_digest"],
        "ci_run_id": ci_run, "admitted_at": datetime.now(timezone.utc).isoformat(),
    }
    directory = root / "weltgewebe"
    descriptor, temp = tempfile.mkstemp(prefix=".routine-release-", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(receipt, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if read_authorization(root, expected_uid=expected_uid) != grant:
            raise HoldReleaseError("operator HOLD changed during routine admission")
        os.replace(temp, directory / ROUTINE_FILE)
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    read_effective_authorization(root, expected_uid=expected_uid, expected_commit=target)
    return receipt


def require_reconciler_lock(path: Path) -> None:
    """The routine admission mutator shares the reconciler's already-held fd 9."""
    try:
        fd = 9
        lock = path.lstat()
        opened = os.fstat(fd)
        if (not stat.S_ISREG(lock.st_mode) or not stat.S_ISREG(opened.st_mode)
                or lock.st_uid != 0 or stat.S_IMODE(lock.st_mode) & 0o022
                or (lock.st_dev, lock.st_ino) != (opened.st_dev, opened.st_ino)):
            raise HoldReleaseError("routine admission lock identity invalid")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise HoldReleaseError("routine admission requires the production deployment lock") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--approved-commit", action="store_true")
    mode.add_argument("--routine-enabled", action="store_true")
    mode.add_argument("--admit-routine")
    mode.add_argument("--commit")
    parser.add_argument("--image-digest")
    parser.add_argument("--source-repository")
    parser.add_argument("--production-lock-file")
    args = parser.parse_args(argv)
    if args.image_digest is not None and args.commit is None:
        parser.error("--image-digest requires --commit")
    if args.source_repository is not None and args.commit is None and args.admit_routine is None:
        parser.error("--source-repository requires --commit or --admit-routine")
    if args.admit_routine is not None and (args.source_repository is None or args.production_lock_file is None):
        parser.error("--admit-routine requires --source-repository and --production-lock-file")
    if args.production_lock_file is not None and args.admit_routine is None:
        parser.error("--production-lock-file requires --admit-routine")
    if os.geteuid() != 0:
        print("production HOLD release verifier must run as root", file=sys.stderr)
        return 1
    try:
        if args.routine_enabled:
            if read_authorization().get("routine_web_releases") is not True:
                return 1
            return 0
        if args.admit_routine is not None:
            require_reconciler_lock(Path(args.production_lock_file))
            receipt = admit_routine_release(Path(args.source_repository), args.admit_routine)
            print(f"production_routine_release=admitted commit={receipt['commit']} ci_run={receipt['ci_run_id']}")
            return 0
        locked_digest = (
            read_locked_image_digest(Path(args.source_repository), args.commit)
            if args.source_repository is not None else None
        )
        if (locked_digest is not None and args.image_digest is not None
                and locked_digest != args.image_digest):
            raise HoldReleaseError("provided OCI digest differs from the exact source commit")
        authorization = read_effective_authorization(
            expected_commit=args.commit,
            expected_image=locked_digest or args.image_digest,
        )
    except HoldReleaseError as exc:
        print(f"production_hold_release=blocked: {exc}", file=sys.stderr)
        return 1
    if args.approved_commit:
        # Commit-only projection of the same root-owned, receipt-validated HOLD.
        # Never return its internal recovery references or approval metadata.
        print(authorization["commit"])
    else:
        print("production_hold_release=verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
