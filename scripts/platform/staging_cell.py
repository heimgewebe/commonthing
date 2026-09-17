#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import copy
import datetime as dt
import errno
import fcntl
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STATE_ROOT = Path.home() / ".local/state/commonthing/staging-cell"
LEGACY_STATE_ROOT = Path.home() / ".local/state/weltgewebe/staging-cell"
DEFAULT_CLUSTER = "commonthing-staging"
LEGACY_CLUSTER = "weltgewebe-staging"
LEGACY_MIGRATION_RECEIPT = "receipts/legacy-state-migration.json"
LEGACY_MIGRATION_PREPARED_STATUS = "legacy-state-data-move-prepared"
LEGACY_MIGRATION_ADOPTED_STATUS = "legacy-state-adopted"
CELL_DOWN_RECEIPT = "receipts/cell-down.json"
CELL_REBUILD_RECEIPT = "receipts/cell-rebuild.json"
DELETE_TO_PROVE_RECEIPT = "receipts/delete-to-prove.json"
HOST_GATEWAY_RECEIPT = "receipts/host-gateway-proof.json"
BACKUP_DOWN_RECEIPT = "receipts/backup-delete-to-prove-down.json"
BACKUP_REBUILD_RECEIPT = "receipts/backup-delete-to-prove-rebuild.json"
BACKUP_DELETE_TO_PROVE_RECEIPT = "receipts/backup-delete-to-prove.json"
STAGING_GATEWAY_NODE_PORT = 31844
STAGING_GATEWAY_HOST_PORT = 18084
SOURCE_NAME = "commonthing-staging-source"
APP_SOURCE_NAME = "commonthing-staging-app-source"
DATA_KUSTOMIZATION = "commonthing-staging-data"
APP_KUSTOMIZATION = "commonthing-staging-app"
MIGRATION_JOB_PREFIX = "commonthing-staging-migration"
MIGRATION_TEMPLATE = ROOT / "platform/apps/weltgewebe/migration/ha/job.yaml"
MIGRATION_TIMEOUT_SECONDS = 8 * 60
MIGRATION_SPEC_ANNOTATION = "commonthing.net/migration-spec-sha256"
CILIUM_POLICY_ENFORCEMENT_TIMEOUT_SECONDS = 45.0
CILIUM_POLICY_ENFORCEMENT_POLL_SECONDS = 1.0
DATA_NAMESPACE = "commonthing-data"
APP_NAMESPACE = "commonthing-staging"
DATABASE_SECRET = "commonthing-staging-database"
RUNTIME_SECRET = "commonthing-runtime"
REGISTRY_SECRET = "commonthing-staging-registry"
GHCR_REGISTRY = "ghcr.io"
LIVE_DEPLOYMENTS = {
    "postgres": (DATA_NAMESPACE, "postgres"),
    "nats": (DATA_NAMESPACE, "nats"),
    "source-controller": ("flux-system", "source-controller"),
    "kustomize-controller": ("flux-system", "kustomize-controller"),
}
APP_DEPLOYMENTS = {
    "api": (APP_NAMESPACE, "commonthing-api"),
    "web": (APP_NAMESPACE, "commonthing-web"),
}
PUBLIC_REPOSITORY = "https://github.com/heimgewebe/commonthing"
SECRET_SOURCE_ANNOTATION = "commonthing.net/external-secret-source-sha256"
REGISTRY_SOURCE_ANNOTATION = "commonthing.net/registry-secret-source-sha256"
DATA_KUSTOMIZATION_TIMEOUT = "8m"
DATA_KUSTOMIZATION_TIMEOUT_SECONDS = 8 * 60.0
PVC_BIND_TIMEOUT_SECONDS = 45.0
ALLOWED_RETAINED_VOLUME_MODES = {"700", "770", "2770"}
REQUIRED_TOOLS = ("kind", "kubectl", "kustomize", "flux", "helm")
REQUIRED_ARTIFACTS = (
    "gateway_api_gatewayclasses",
    "gateway_api_gateways",
    "gateway_api_httproutes",
    "gateway_api_referencegrants",
    "gateway_api_grpcroutes",
    "cilium_chart",
)

sys.path.insert(0, str(ROOT / "scripts/platform"))
import kind_reference as reference  # noqa: E402


class StagingCellError(RuntimeError):
    pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def run(
    argv: list[str],
    *,
    input_text: str | None = None,
    capture: bool = False,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    print("+ external command [arguments redacted]", file=sys.stderr, flush=True)
    kwargs: dict[str, Any] = {"capture_output": True} if capture else {"stdout": sys.stderr}
    return subprocess.run(
        argv,
        cwd=ROOT,
        text=True,
        input=input_text,
        check=True,
        timeout=timeout,
        env=env,
        **kwargs,
    )


def output(argv: list[str], *, timeout: float | None = None) -> str:
    return run(argv, capture=True, timeout=timeout).stdout.strip()


def stream_command_to_file(
    argv: list[str], path: Path, *, timeout: float | None = None
) -> None:
    print("+ external command [arguments redacted]", file=sys.stderr, flush=True)
    ensure_directory_durable(path.parent)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            os.fchmod(handle.fileno(), 0o600)
            subprocess.run(
                argv,
                cwd=ROOT,
                stdout=handle,
                stderr=sys.stderr,
                check=True,
                timeout=timeout,
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
        fsync_directory(path.parent)
    finally:
        if fd >= 0:
            os.close(fd)
        tmp.unlink(missing_ok=True)


def stream_file_to_command(
    path: Path, argv: list[str], *, timeout: float | None = None
) -> None:
    _private_regular_file(path, label="staging backup archive")
    print("+ external command [arguments redacted]", file=sys.stderr, flush=True)
    with path.open("rb") as handle:
        subprocess.run(
            argv,
            cwd=ROOT,
            stdin=handle,
            stdout=sys.stderr,
            stderr=sys.stderr,
            check=True,
            timeout=timeout,
        )




def fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        linked = os.stat(path, follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or opened.st_dev != linked.st_dev
            or opened.st_ino != linked.st_ino
        ):
            raise StagingCellError("atomic write parent directory identity is unsafe")
        os.fsync(fd)
    finally:
        os.close(fd)


def ensure_directory_durable(path: Path, *, mode: int = 0o700) -> None:
    missing: list[Path] = []
    current = path
    while True:
        try:
            linked = current.lstat()
        except FileNotFoundError:
            parent = current.parent
            if parent == current:
                raise StagingCellError(
                    f"cannot find an existing parent for durable directory {path}"
                )
            missing.append(current)
            current = parent
            continue
        if stat.S_ISLNK(linked.st_mode) or not stat.S_ISDIR(linked.st_mode):
            raise StagingCellError(
                f"durable directory parent must be a real directory: {current}"
            )
        break
    for directory in reversed(missing):
        directory.mkdir(mode=mode)
        fsync_directory(directory.parent)


def atomic_json(path: Path, payload: dict[str, Any], *, mode: int = 0o600) -> None:
    ensure_directory_durable(path.parent)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        handle = os.fdopen(fd, "w", encoding="utf-8")
        fd = -1
        with handle:
            os.fchmod(handle.fileno(), mode)
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, mode)
        fsync_directory(path.parent)
    finally:
        if fd >= 0:
            os.close(fd)
        tmp.unlink(missing_ok=True)


def atomic_text(path: Path, text: str, *, mode: int = 0o600) -> None:
    ensure_directory_durable(path.parent)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        handle = os.fdopen(fd, "w", encoding="utf-8")
        fd = -1
        with handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, mode)
        fsync_directory(path.parent)
    finally:
        if fd >= 0:
            os.close(fd)
        tmp.unlink(missing_ok=True)


def atomic_bytes(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    ensure_directory_durable(path.parent)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        handle = os.fdopen(fd, "wb")
        fd = -1
        with handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, mode)
        fsync_directory(path.parent)
    finally:
        if fd >= 0:
            os.close(fd)
        tmp.unlink(missing_ok=True)


def state_root(value: str | None) -> Path:
    resolved = Path(value).expanduser().resolve() if value else DEFAULT_STATE_ROOT.resolve()
    expected = DEFAULT_STATE_ROOT.resolve()
    if resolved != expected:
        raise StagingCellError(f"state root must be exactly {expected}")
    return resolved




@contextmanager
def lifecycle_lock(root: Path):
    ensure_directory_durable(root, mode=0o700)
    lock_path = root / "lifecycle.lock"
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(lock_path, flags, 0o600)
    try:
        opened = os.fstat(fd)
        linked = os.stat(lock_path, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != linked.st_dev
            or opened.st_ino != linked.st_ino
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) & 0o077
        ):
            raise StagingCellError("staging lifecycle lock identity is unsafe")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
            raise StagingCellError(
                "another staging lifecycle mutation is already in progress"
            ) from error
        yield
    finally:
        os.close(fd)


def lifecycle_mutation_locked(function):
    @wraps(function)
    def wrapped(args: argparse.Namespace) -> dict[str, Any]:
        require_singleton_cluster(args.cluster)
        owner_id = getattr(args, "owner_id", None)
        if not owner_id:
            raise StagingCellError("--owner-id is required for real staging ownership")
        reference.validate_owner_id(owner_id)
        root = state_root(getattr(args, "state_root", None))
        with lifecycle_lock(root):
            return function(args)

    return wrapped


def legacy_lifecycle_mutation_locked(function):
    @wraps(function)
    def wrapped(args: argparse.Namespace) -> dict[str, Any]:
        legacy_root = LEGACY_STATE_ROOT.resolve()
        with lifecycle_lock(legacy_root):
            return function(args)

    return wrapped


def configure_reference_paths(root: Path) -> None:
    reference.CACHE = root
    reference.MARKERS = root / "clusters"
    reference.KUBECONFIGS = root / "kubeconfigs"
    reference.OCI_MIRROR_STATE = root / "oci-mirror"


def reference_output_routed(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        original_run = reference.run
        reference.run = run
        try:
            return function(*args, **kwargs)
        finally:
            reference.run = original_run

    return wrapped


def require_locked_file(
    path: Path, expected_sha256: str, *, label: str, executable: bool = False
) -> None:
    if (
        len(expected_sha256) != 64
        or any(ch not in "0123456789abcdef" for ch in expected_sha256)
    ):
        raise StagingCellError(f"{label} has no canonical SHA-256 in the platform lock")
    try:
        linked = path.lstat()
    except OSError as error:
        raise StagingCellError(f"{label} is missing or unreadable: {path}") from error
    if stat.S_ISLNK(linked.st_mode) or not stat.S_ISREG(linked.st_mode):
        raise StagingCellError(f"{label} must be a regular non-symlink file: {path}")
    if linked.st_uid != os.geteuid() or stat.S_IMODE(linked.st_mode) & 0o022:
        raise StagingCellError(f"{label} ownership or write mode is unsafe: {path}")
    if executable and not os.access(path, os.X_OK):
        raise StagingCellError(f"{label} is not executable: {path}")
    actual_sha256 = sha256_file(path)
    if not hmac.compare_digest(actual_sha256, expected_sha256):
        raise StagingCellError(
            f"{label} digest mismatch: expected {expected_sha256}, got {actual_sha256}"
        )


def load_tool_receipt(
    root: Path,
    *,
    required_tools: tuple[str, ...] = REQUIRED_TOOLS,
    required_artifacts: tuple[str, ...] = REQUIRED_ARTIFACTS,
) -> dict[str, Any]:
    receipt_path = root / "toolchain/receipt.json"
    if not receipt_path.is_file() or receipt_path.is_symlink():
        raise StagingCellError(
            "pinned toolchain receipt is missing; run bootstrap_tools.py into the T084 state root first"
        )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    lock_path = ROOT / "platform/toolchain.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    expected_lock = sha256_file(lock_path)
    if receipt.get("schema_version") != 1 or receipt.get("lock_sha256") != expected_lock:
        raise StagingCellError("toolchain receipt is not bound to the current platform lock")
    cache_root = root / "toolchain"
    if cache_root.is_symlink() or not cache_root.is_dir():
        raise StagingCellError("toolchain cache root must be a real directory")
    receipt_cache = Path(str(receipt.get("cache") or ""))
    try:
        cache_matches = receipt_cache.resolve(strict=True) == cache_root.resolve(strict=True)
    except OSError:
        cache_matches = False
    if not cache_matches:
        raise StagingCellError("toolchain receipt cache path is not the canonical T084 cache")
    tools = receipt.get("tools") if isinstance(receipt.get("tools"), dict) else {}
    artifacts = receipt.get("artifacts") if isinstance(receipt.get("artifacts"), dict) else {}
    lock_tools = lock.get("tools") if isinstance(lock.get("tools"), dict) else {}
    lock_artifacts = (
        lock.get("artifacts") if isinstance(lock.get("artifacts"), dict) else {}
    )
    unknown_tools = sorted(set(required_tools) - set(REQUIRED_TOOLS))
    unknown_artifacts = sorted(set(required_artifacts) - set(REQUIRED_ARTIFACTS))
    if unknown_tools or unknown_artifacts:
        raise StagingCellError(
            "toolchain receipt requested unknown staging dependencies: "
            f"tools={unknown_tools}, artifacts={unknown_artifacts}"
        )
    missing_specs = [
        name for name in (*required_tools, *required_artifacts)
        if name not in (lock_tools if name in required_tools else lock_artifacts)
    ]
    if missing_specs:
        raise StagingCellError(f"platform lock is missing staging entries: {missing_specs}")
    for name in required_tools:
        spec = lock_tools[name]
        expected_path = cache_root / "bin" / str(spec.get("binary") or "")
        observed_path = Path(str(tools.get(name) or ""))
        try:
            path_matches = observed_path.resolve(strict=True) == expected_path.resolve(strict=True)
        except OSError:
            path_matches = False
        if not path_matches:
            raise StagingCellError(
                f"toolchain receipt path mismatch for tool {name}: {observed_path}"
            )
        require_locked_file(
            observed_path,
            str(spec.get("binary_sha256") or ""),
            label=f"tool {name}",
            executable=True,
        )
    for name in required_artifacts:
        spec = lock_artifacts[name]
        expected_path = cache_root / "artifacts" / str(spec.get("filename") or "")
        observed_path = Path(str(artifacts.get(name) or ""))
        try:
            path_matches = observed_path.resolve(strict=True) == expected_path.resolve(strict=True)
        except OSError:
            path_matches = False
        if not path_matches:
            raise StagingCellError(
                f"toolchain receipt path mismatch for artifact {name}: {observed_path}"
            )
        require_locked_file(
            observed_path,
            str(spec.get("sha256") or ""),
            label=f"artifact {name}",
        )
    return receipt


def require_clean_commit(
    source_commit: str | None,
    *,
    expected_commit: str | None = None,
    require_public_main: bool = True,
) -> str:
    if output(["git", "status", "--porcelain"]):
        raise StagingCellError("staging cell mutation requires a clean worktree")
    head = output(["git", "rev-parse", "HEAD"])
    if source_commit is not None and source_commit != head:
        raise StagingCellError(
            f"source commit {source_commit} does not equal worktree HEAD {head}"
        )
    if len(head) != 40 or any(ch not in "0123456789abcdef" for ch in head):
        raise StagingCellError("worktree HEAD is not a canonical 40-hex commit")
    if expected_commit is not None:
        if len(expected_commit) != 40 or any(
            ch not in "0123456789abcdef" for ch in expected_commit
        ):
            raise StagingCellError("persisted bootstrap commit is not canonical 40-hex")
        if head != expected_commit:
            raise StagingCellError(
                "existing staging cell is pinned to bootstrap commit "
                f"{expected_commit}; current worktree HEAD is {head}"
            )
    if require_public_main:
        public = output(
            ["git", "ls-remote", PUBLIC_REPOSITORY, "refs/heads/main"],
            timeout=30,
        )
        remote_head = public.split()[0] if public else ""
        if remote_head != head:
            raise StagingCellError(
                f"staging bootstrap requires exact public main; local={head} "
                f"public-main={remote_head or 'missing'}"
            )
    return head


def require_singleton_cluster(cluster: str) -> None:
    if cluster != DEFAULT_CLUSTER:
        raise StagingCellError(
            f"staging persistent state is singleton; cluster must be exactly {DEFAULT_CLUSTER!r}"
        )


def data_node_name(cluster: str) -> str:
    require_singleton_cluster(cluster)
    return f"{cluster}-worker"


def load_cell_receipt(root: Path) -> dict[str, Any]:
    path = root / "receipts/cell-bootstrap.json"
    if not path.exists():
        raise StagingCellError("cell bootstrap receipt is missing; refusing unbound operation")
    linked = path.lstat()
    if stat.S_ISLNK(linked.st_mode) or not stat.S_ISREG(linked.st_mode):
        raise StagingCellError("cell bootstrap receipt must be a regular file")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise StagingCellError("cell bootstrap receipt is malformed")
    return payload


def require_receipt_cluster(cell: dict[str, Any], cluster: str) -> None:
    recorded = cell.get("cluster")
    if recorded != cluster:
        raise StagingCellError(
            f"--cluster {cluster!r} does not match persisted cluster {recorded!r}"
        )


def cell_active_commit(cell: dict[str, Any]) -> str:
    bootstrap = str(cell.get("bootstrap_commit") or "")
    active = str(cell.get("active_commit") or bootstrap)
    for label, value in (("bootstrap_commit", bootstrap), ("active_commit", active)):
        if len(value) != 40 or any(ch not in "0123456789abcdef" for ch in value):
            raise StagingCellError(f"cell receipt has no canonical {label}")
    return active


def recorded_secret_source_sha(root: Path) -> str | None:
    receipt_path = root / "receipts/cell-bootstrap.json"
    if not receipt_path.exists():
        return None
    cell = load_cell_receipt(root)
    external = cell.get("external_secret")
    source_sha = external.get("source_sha256") if isinstance(external, dict) else None
    if (
        not isinstance(source_sha, str)
        or len(source_sha) != 64
        or any(ch not in "0123456789abcdef" for ch in source_sha)
    ):
        raise StagingCellError(
            "cell bootstrap receipt has no canonical external-secret source hash"
        )
    return source_sha


def retained_data_directory_exists(root: Path, name: str) -> bool:
    data_path = root / "data" / name
    if not data_path.exists():
        return False
    linked = data_path.lstat()
    if stat.S_ISLNK(linked.st_mode) or not stat.S_ISDIR(linked.st_mode):
        raise StagingCellError(
            f"staging {name} data path must be a regular directory"
        )
    try:
        with os.scandir(data_path) as entries:
            return next(entries, None) is not None
    except PermissionError:
        # Retained data may intentionally be 0700 and owned by a container UID.
        # Inability to inspect it is evidence to preserve, never permission to
        # bind a new owner/commit or mint replacement credentials.
        return True


def retained_postgres_state_exists(root: Path) -> bool:
    if (root / "receipts/cell-bootstrap.json").is_file():
        return True
    return retained_data_directory_exists(root, "postgres")


def retained_staging_data_exists(root: Path) -> bool:
    return any(
        retained_data_directory_exists(root, name) for name in ("postgres", "nats")
    )



def _real_directory_identity(path: Path, *, label: str) -> dict[str, int]:
    try:
        linked = path.lstat()
    except OSError as error:
        raise StagingCellError(f"{label} is missing or unreadable") from error
    if stat.S_ISLNK(linked.st_mode) or not stat.S_ISDIR(linked.st_mode):
        raise StagingCellError(f"{label} must be a real directory")
    return {
        "device": linked.st_dev,
        "inode": linked.st_ino,
        "uid": linked.st_uid,
        "gid": linked.st_gid,
        "mode": stat.S_IMODE(linked.st_mode),
    }


def _private_regular_file(path: Path, *, label: str) -> os.stat_result:
    try:
        linked = path.lstat()
    except OSError as error:
        raise StagingCellError(f"{label} is missing or unreadable") from error
    if (
        stat.S_ISLNK(linked.st_mode)
        or not stat.S_ISREG(linked.st_mode)
        or linked.st_uid != os.geteuid()
        or stat.S_IMODE(linked.st_mode) != 0o600
    ):
        raise StagingCellError(f"{label} must be an owner-owned mode-0600 regular file")
    return linked


def _tree_manifest(root: Path, *, label: str) -> dict[str, dict[str, Any]]:
    _real_directory_identity(root, label=label)
    result: dict[str, dict[str, Any]] = {}
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in [*directory_names, *file_names]:
            path = current_path / name
            linked = path.lstat()
            relative = path.relative_to(root).as_posix()
            if stat.S_ISLNK(linked.st_mode):
                raise StagingCellError(f"{label} contains a symlink: {relative}")
            if stat.S_ISDIR(linked.st_mode):
                result[relative] = {
                    "type": "directory",
                    "mode": stat.S_IMODE(linked.st_mode),
                    "uid": linked.st_uid,
                    "gid": linked.st_gid,
                }
            elif stat.S_ISREG(linked.st_mode):
                result[relative] = {
                    "type": "file",
                    "sha256": sha256_file(path),
                    "mode": stat.S_IMODE(linked.st_mode),
                    "uid": linked.st_uid,
                    "gid": linked.st_gid,
                }
            else:
                raise StagingCellError(
                    f"{label} contains an unsupported filesystem object: {relative}"
                )
    return dict(sorted(result.items()))


def _copy_private_file_exact(source: Path, target: Path, *, label: str) -> str:
    _private_regular_file(source, label=label)
    payload = source.read_bytes()
    source_sha = sha256_bytes(payload)
    atomic_bytes(target, payload, mode=0o600)
    _private_regular_file(target, label=f"copied {label}")
    if not hmac.compare_digest(sha256_file(target), source_sha):
        raise StagingCellError(f"copied {label} digest mismatch")
    return source_sha


def _fsync_tree(root: Path, *, label: str) -> None:
    directories = [root]
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        directories.extend(current_path / name for name in directory_names)
        for name in file_names:
            path = current_path / name
            flags = os.O_RDONLY | os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(path, flags)
            try:
                opened = os.fstat(fd)
                linked = os.stat(path, follow_symlinks=False)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_dev != linked.st_dev
                    or opened.st_ino != linked.st_ino
                ):
                    raise StagingCellError(f"{label} contains an unsafe copied file")
                os.fsync(fd)
            finally:
                os.close(fd)
    for directory in sorted(
        directories, key=lambda item: len(item.parts), reverse=True
    ):
        fsync_directory(directory)
    fsync_directory(root.parent)


def _copy_tree_exact(
    source: Path, target: Path, *, label: str
) -> dict[str, dict[str, Any]]:
    source_manifest = _tree_manifest(source, label=label)
    if target.exists():
        raise StagingCellError(f"target {label} already exists")
    shutil.copytree(source, target, copy_function=shutil.copy2)
    target_manifest = _tree_manifest(target, label=f"copied {label}")
    if target_manifest != source_manifest:
        raise StagingCellError(f"copied {label} differs from the legacy source")
    _fsync_tree(target, label=f"copied {label}")
    return source_manifest


def _legacy_migration_receipt_path(root: Path) -> Path:
    return root / LEGACY_MIGRATION_RECEIPT


def _read_legacy_state_migration_receipt(root: Path) -> dict[str, Any]:
    path = _legacy_migration_receipt_path(root)
    _private_regular_file(path, label="legacy-state migration receipt")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise StagingCellError("legacy-state migration receipt is malformed") from error
    if not isinstance(payload, dict):
        raise StagingCellError("legacy-state migration receipt is malformed")
    return payload


def load_legacy_state_migration(root: Path, *, owner_id: str) -> dict[str, Any]:
    payload = _read_legacy_state_migration_receipt(root)
    expected = {
        "schema_version": 1,
        "status": LEGACY_MIGRATION_ADOPTED_STATUS,
        "source_root": str(LEGACY_STATE_ROOT.resolve()),
        "target_root": str(root.resolve()),
        "source_cluster": LEGACY_CLUSTER,
        "target_cluster": DEFAULT_CLUSTER,
        "owner_id": owner_id,
        "toolchain_copied": False,
        "production_changed": False,
    }
    mismatched = {
        key: {"expected": value, "observed": payload.get(key)}
        for key, value in expected.items()
        if payload.get(key) != value
    }
    if mismatched:
        raise StagingCellError(
            "legacy-state migration receipt identity mismatch: "
            + json.dumps(mismatched, sort_keys=True)
        )
    data_identity = payload.get("data_identity")
    if not isinstance(data_identity, dict):
        raise StagingCellError("legacy-state migration receipt has no data identity")
    for name in ("postgres", "nats"):
        recorded = data_identity.get(name)
        observed = _real_directory_identity(
            root / "data" / name, label=f"retained {name} data"
        )
        if recorded != observed:
            raise StagingCellError(
                f"retained {name} data identity differs from migration receipt"
            )
    runtime_secret = root / "secrets/staging-runtime.json"
    _private_regular_file(runtime_secret, label="migrated runtime secret")
    secret_sha = payload.get("runtime_secret_sha256")
    if not isinstance(secret_sha, str) or not hmac.compare_digest(
        sha256_file(runtime_secret), secret_sha
    ):
        raise StagingCellError("migrated runtime secret differs from migration receipt")
    registry_secret = root / "secrets/staging-registry.json"
    registry_sha = payload.get("registry_secret_sha256")
    if registry_sha is None:
        if registry_secret.exists():
            raise StagingCellError(
                "migrated registry secret exists without migration-receipt binding"
            )
    else:
        _private_regular_file(registry_secret, label="migrated registry secret")
        if not isinstance(registry_sha, str) or not hmac.compare_digest(
            sha256_file(registry_secret), registry_sha
        ):
            raise StagingCellError(
                "migrated registry secret differs from migration receipt"
            )
    for field, evidence_path in (
        (
            "legacy_cell_receipt_sha256",
            root / "legacy-evidence/receipts/cell-bootstrap.json",
        ),
        (
            "legacy_toolchain_receipt_sha256",
            root / "legacy-evidence/toolchain/receipt.json",
        ),
    ):
        recorded_sha = payload.get(field)
        if not isinstance(recorded_sha, str) or not hmac.compare_digest(
            sha256_file(evidence_path), recorded_sha
        ):
            raise StagingCellError(f"{field} differs from migrated legacy evidence")
    promotion_manifest = payload.get("promotion_manifest")
    migrated_promotion = root / "promotion"
    observed_promotion_manifest = (
        _tree_manifest(migrated_promotion, label="migrated promotion receipts")
        if migrated_promotion.exists() or migrated_promotion.is_symlink()
        else {}
    )
    if promotion_manifest != observed_promotion_manifest:
        raise StagingCellError(
            "migrated promotion receipts differ from migration receipt"
        )
    legacy_evidence_manifest = payload.get("legacy_evidence_manifest")
    if legacy_evidence_manifest != _tree_manifest(
        root / "legacy-evidence", label="migrated legacy evidence"
    ):
        raise StagingCellError(
            "migrated legacy evidence differs from migration receipt"
        )
    return payload


def _legacy_migration_public_result(root: Path) -> dict[str, Any]:
    receipt_path = _legacy_migration_receipt_path(root)
    return {
        "schema_version": 1,
        "status": LEGACY_MIGRATION_ADOPTED_STATUS,
        "cluster": DEFAULT_CLUSTER,
        "toolchain_regeneration_required": not (
            root / "toolchain/receipt.json"
        ).is_file(),
        "production_changed": False,
        "receipt_path": str(receipt_path),
        "receipt_sha256": sha256_file(receipt_path),
    }


def _rename_legacy_data_for_cutover(source: Path, target: Path) -> None:
    source.rename(target)
    fsync_directory(source.parent)
    fsync_directory(target.parent)


def _require_prepared_legacy_state_migration(
    root: Path, legacy_root: Path, *, owner_id: str, payload: dict[str, Any]
) -> str:
    expected = {
        "schema_version": 1,
        "status": LEGACY_MIGRATION_PREPARED_STATUS,
        "source_root": str(legacy_root),
        "target_root": str(root.resolve()),
        "source_cluster": LEGACY_CLUSTER,
        "target_cluster": DEFAULT_CLUSTER,
        "owner_id": owner_id,
        "toolchain_copied": False,
        "production_changed": False,
    }
    mismatched = {
        key: {"expected": value, "observed": payload.get(key)}
        for key, value in expected.items()
        if payload.get(key) != value
    }
    if mismatched:
        raise StagingCellError(
            "prepared legacy-state migration identity mismatch: "
            + json.dumps(mismatched, sort_keys=True)
        )

    _real_directory_identity(legacy_root, label="legacy staging state root")
    legacy_cell = load_cell_receipt(legacy_root)
    if legacy_cell.get("cluster") != LEGACY_CLUSTER:
        raise StagingCellError(
            "prepared legacy-state migration source receipt has wrong cluster"
        )
    if str(legacy_cell.get("owner_id") or "") != owner_id:
        raise StagingCellError(
            "prepared legacy-state migration owner differs from legacy cell"
        )
    if (
        legacy_cell.get("app_activation") is True
        or legacy_cell.get("status") == "app-activation-in-progress"
        or bool(legacy_cell.get("active_commit"))
        or bool(legacy_cell.get("pending_active_commit"))
    ):
        raise StagingCellError(
            "prepared legacy-state migration source became activated"
        )
    legacy_commit = str(legacy_cell.get("bootstrap_commit") or "")
    if payload.get("legacy_bootstrap_commit") != legacy_commit:
        raise StagingCellError("prepared legacy-state migration bootstrap commit drift")
    reference.validate_ownership_binding(legacy_commit, owner_id)

    legacy_cell_path = legacy_root / "receipts/cell-bootstrap.json"
    if payload.get("legacy_cell_receipt_sha256") != sha256_file(legacy_cell_path):
        raise StagingCellError("prepared legacy-state migration cell receipt changed")
    legacy_tool_receipt = legacy_root / "toolchain/receipt.json"
    _private_regular_file(legacy_tool_receipt, label="legacy toolchain receipt")
    if payload.get("legacy_toolchain_receipt_sha256") != sha256_file(
        legacy_tool_receipt
    ):
        raise StagingCellError(
            "prepared legacy-state migration toolchain receipt changed"
        )

    runtime_sha = payload.get("runtime_secret_sha256")
    legacy_runtime = legacy_root / "secrets/staging-runtime.json"
    migrated_runtime = root / "secrets/staging-runtime.json"
    for path, label in (
        (legacy_runtime, "legacy runtime secret"),
        (migrated_runtime, "prepared migrated runtime secret"),
    ):
        _private_regular_file(path, label=label)
        if not isinstance(runtime_sha, str) or not hmac.compare_digest(
            sha256_file(path), runtime_sha
        ):
            raise StagingCellError(
                "prepared legacy-state migration runtime secret drift"
            )
    source_sha = recorded_secret_source_sha(legacy_root)
    if source_sha is None or not hmac.compare_digest(source_sha, runtime_sha):
        raise StagingCellError(
            "prepared legacy-state migration runtime secret lost receipt binding"
        )

    registry_sha = payload.get("registry_secret_sha256")
    legacy_registry = legacy_root / "secrets/staging-registry.json"
    migrated_registry = root / "secrets/staging-registry.json"
    if registry_sha is None:
        if legacy_registry.exists() or migrated_registry.exists():
            raise StagingCellError(
                "prepared legacy-state migration gained an unbound registry secret"
            )
    else:
        if not isinstance(registry_sha, str):
            raise StagingCellError(
                "prepared legacy-state migration registry hash is malformed"
            )
        for path, label in (
            (legacy_registry, "legacy registry secret"),
            (migrated_registry, "prepared migrated registry secret"),
        ):
            _private_regular_file(path, label=label)
            if not hmac.compare_digest(sha256_file(path), registry_sha):
                raise StagingCellError(
                    "prepared legacy-state migration registry secret drift"
                )

    promotion_manifest = payload.get("promotion_manifest")
    for path, label in (
        (legacy_root / "promotion", "legacy promotion receipts"),
        (root / "promotion", "prepared migrated promotion receipts"),
    ):
        observed_manifest = (
            _tree_manifest(path, label=label)
            if path.exists() or path.is_symlink()
            else {}
        )
        if promotion_manifest != observed_manifest:
            raise StagingCellError(
                "prepared legacy-state migration promotion evidence drift"
            )
    legacy_receipts_manifest = payload.get("legacy_receipts_manifest")
    if legacy_receipts_manifest != _tree_manifest(
        legacy_root / "receipts", label="legacy cell receipts"
    ):
        raise StagingCellError("prepared legacy-state migration source receipts drift")
    if payload.get("legacy_evidence_manifest") != _tree_manifest(
        root / "legacy-evidence", label="prepared migrated legacy evidence"
    ):
        raise StagingCellError("prepared legacy-state migration copied evidence drift")

    legacy_toolchain = load_tool_receipt(
        legacy_root, required_tools=("kind",), required_artifacts=()
    )
    configure_reference_paths(legacy_root)
    try:
        if LEGACY_CLUSTER in reference.clusters(legacy_toolchain["tools"]["kind"]):
            raise StagingCellError(
                "legacy staging cluster reappeared during prepared migration recovery"
            )
    finally:
        configure_reference_paths(root)

    legacy_data = legacy_root / "data"
    target_data = root / "data"
    legacy_exists = legacy_data.exists() or legacy_data.is_symlink()
    target_exists = target_data.exists() or target_data.is_symlink()
    if legacy_exists == target_exists:
        raise StagingCellError(
            "prepared legacy-state migration requires data in exactly one state root"
        )
    data_root = target_data if target_exists else legacy_data
    data_identity = payload.get("data_identity")
    if not isinstance(data_identity, dict):
        raise StagingCellError("prepared legacy-state migration has no data identity")
    for name in ("postgres", "nats"):
        if data_identity.get(name) != _real_directory_identity(
            data_root / name, label=f"prepared retained {name} data"
        ):
            raise StagingCellError(f"prepared retained {name} data identity drift")
    return "canonical" if target_exists else "legacy"


def _finalize_prepared_legacy_state_migration(
    root: Path, *, owner_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    data_identity = payload.get("data_identity")
    if not isinstance(data_identity, dict):
        raise StagingCellError("prepared legacy-state migration has no data identity")
    for name in ("postgres", "nats"):
        if data_identity.get(name) != _real_directory_identity(
            root / "data" / name, label=f"migrated retained {name} data"
        ):
            raise StagingCellError(
                f"migrated retained {name} data identity drift before final receipt"
            )
    terminal = {
        **payload,
        "status": LEGACY_MIGRATION_ADOPTED_STATUS,
        "rollback": {
            "legacy_cluster_absent": True,
            "legacy_cluster_recreatable_from_preserved_commit_and_evidence": True,
            "same_filesystem_rename": True,
            "reverse_data_rename_possible_before_canonical_cluster_writes": True,
        },
    }
    atomic_json(_legacy_migration_receipt_path(root), terminal, mode=0o600)
    validated = load_legacy_state_migration(root, owner_id=owner_id)
    if validated != terminal:
        raise StagingCellError("legacy-state migration receipt readback mismatch")
    return _legacy_migration_public_result(root)


def _resume_prepared_legacy_state_migration(
    root: Path, legacy_root: Path, *, owner_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    data_location = _require_prepared_legacy_state_migration(
        root, legacy_root, owner_id=owner_id, payload=payload
    )
    if data_location == "legacy":
        _rename_legacy_data_for_cutover(legacy_root / "data", root / "data")
    return _finalize_prepared_legacy_state_migration(
        root, owner_id=owner_id, payload=payload
    )


@lifecycle_mutation_locked
@reference_output_routed
@legacy_lifecycle_mutation_locked
def command_migrate_legacy_state(args: argparse.Namespace) -> dict[str, Any]:
    require_singleton_cluster(args.cluster)
    owner_id = args.owner_id
    reference.validate_owner_id(owner_id)
    root = state_root(getattr(args, "state_root", None))
    legacy_root = LEGACY_STATE_ROOT.resolve()
    if legacy_root == root:
        raise StagingCellError("legacy and canonical state roots must differ")

    migration_path = _legacy_migration_receipt_path(root)
    if migration_path.exists() or migration_path.is_symlink():
        migration = _read_legacy_state_migration_receipt(root)
        status = migration.get("status")
        if status == LEGACY_MIGRATION_ADOPTED_STATUS:
            load_legacy_state_migration(root, owner_id=owner_id)
            return _legacy_migration_public_result(root)
        if status == LEGACY_MIGRATION_PREPARED_STATUS:
            return _resume_prepared_legacy_state_migration(
                root, legacy_root, owner_id=owner_id, payload=migration
            )
        raise StagingCellError(
            f"legacy-state migration receipt has unsupported status: {status!r}"
        )

    _real_directory_identity(legacy_root, label="legacy staging state root")

    unexpected_target_entries = sorted(
        path.name for path in root.iterdir() if path.name != "lifecycle.lock"
    )
    if unexpected_target_entries:
        raise StagingCellError(
            "canonical staging state root is not empty before legacy adoption: "
            f"{unexpected_target_entries!r}"
        )

    legacy_cell = load_cell_receipt(legacy_root)
    if legacy_cell.get("cluster") != LEGACY_CLUSTER:
        raise StagingCellError(
            "legacy cell receipt is not bound to the legacy staging cluster"
        )
    if str(legacy_cell.get("owner_id") or "") != owner_id:
        raise StagingCellError("--owner-id does not match the legacy staging owner")
    if (
        legacy_cell.get("app_activation") is True
        or legacy_cell.get("status") == "app-activation-in-progress"
        or bool(legacy_cell.get("active_commit"))
        or bool(legacy_cell.get("pending_active_commit"))
    ):
        raise StagingCellError(
            "legacy-state adoption requires an unactivated legacy cell"
        )

    # Preflight every retained byte and ownership binding before deleting the
    # legacy cluster. Cluster destruction is the first irreversible-looking
    # step, so rollback evidence must already be complete at this point.
    legacy_toolchain = load_tool_receipt(
        legacy_root, required_tools=("kind",), required_artifacts=()
    )
    legacy_commit = str(legacy_cell.get("bootstrap_commit") or "")
    reference.validate_ownership_binding(legacy_commit, owner_id)

    legacy_secret = legacy_root / "secrets/staging-runtime.json"
    _private_regular_file(legacy_secret, label="legacy runtime secret")
    legacy_source_sha = recorded_secret_source_sha(legacy_root)
    if legacy_source_sha is None or not hmac.compare_digest(
        sha256_file(legacy_secret), legacy_source_sha
    ):
        raise StagingCellError(
            "legacy runtime secret is not bound to the legacy cell receipt"
        )
    legacy_registry = legacy_root / "secrets/staging-registry.json"
    if legacy_registry.exists():
        _private_regular_file(legacy_registry, label="legacy registry secret")

    legacy_data = legacy_root / "data"
    _real_directory_identity(legacy_data, label="legacy staging data root")
    data_identity = {
        name: _real_directory_identity(
            legacy_data / name, label=f"legacy retained {name} data"
        )
        for name in ("postgres", "nats")
    }
    if legacy_data.parent.stat().st_dev != root.stat().st_dev:
        raise StagingCellError(
            "legacy and canonical state roots are on different filesystems; refusing non-atomic data copy"
        )

    legacy_promotion = legacy_root / "promotion"
    promotion_preflight = (
        _tree_manifest(legacy_promotion, label="legacy promotion receipts")
        if legacy_promotion.exists() or legacy_promotion.is_symlink()
        else None
    )
    legacy_receipts = legacy_root / "receipts"
    receipts_preflight = _tree_manifest(legacy_receipts, label="legacy cell receipts")
    legacy_tool_receipt = legacy_root / "toolchain/receipt.json"
    _private_regular_file(legacy_tool_receipt, label="legacy toolchain receipt")
    tool_receipt_preflight_sha = sha256_file(legacy_tool_receipt)

    configure_reference_paths(legacy_root)
    try:
        kind = legacy_toolchain["tools"]["kind"]
        legacy_cluster_present = LEGACY_CLUSTER in reference.clusters(kind)
        reference.delete_owned_cluster_if_present(
            kind,
            LEGACY_CLUSTER,
            expected_commit=legacy_commit,
            expected_owner_id=owner_id,
        )
        if LEGACY_CLUSTER in reference.clusters(kind):
            raise StagingCellError(
                "legacy staging cluster still exists after controlled shutdown"
            )
    finally:
        configure_reference_paths(root)

    # Revalidate the preflight snapshot after cluster shutdown and before any
    # copy/move. An unexpected concurrent filesystem write aborts the cutover.
    promotion_after_shutdown = (
        _tree_manifest(legacy_promotion, label="legacy promotion receipts")
        if legacy_promotion.exists() or legacy_promotion.is_symlink()
        else None
    )
    if promotion_preflight != promotion_after_shutdown:
        raise StagingCellError(
            "legacy promotion receipts changed during cluster shutdown"
        )
    if receipts_preflight != _tree_manifest(
        legacy_receipts, label="legacy cell receipts"
    ):
        raise StagingCellError("legacy cell receipts changed during cluster shutdown")
    if not hmac.compare_digest(
        sha256_file(legacy_tool_receipt), tool_receipt_preflight_sha
    ):
        raise StagingCellError(
            "legacy toolchain receipt changed during cluster shutdown"
        )
    for name, identity in data_identity.items():
        if (
            _real_directory_identity(
                legacy_data / name, label=f"legacy retained {name} data"
            )
            != identity
        ):
            raise StagingCellError(
                f"legacy retained {name} data identity changed during cluster shutdown"
            )

    moved_data = False
    copied_paths: list[Path] = []
    target_data = root / "data"
    try:
        promotion_manifest: dict[str, dict[str, Any]] = {}
        if promotion_preflight is not None:
            promotion_manifest = _copy_tree_exact(
                legacy_promotion, root / "promotion", label="promotion receipts"
            )
            copied_paths.append(root / "promotion")

        ensure_directory_durable(root / "secrets", mode=0o700)
        copied_paths.append(root / "secrets")
        runtime_secret_sha = _copy_private_file_exact(
            legacy_secret,
            root / "secrets/staging-runtime.json",
            label="runtime secret",
        )
        registry_secret_sha: str | None = None
        if legacy_registry.exists():
            registry_secret_sha = _copy_private_file_exact(
                legacy_registry,
                root / "secrets/staging-registry.json",
                label="registry secret",
            )

        evidence_root = root / "legacy-evidence"
        ensure_directory_durable(evidence_root, mode=0o700)
        copied_paths.append(evidence_root)
        _copy_tree_exact(
            legacy_receipts,
            evidence_root / "receipts",
            label="legacy cell receipts",
        )
        ensure_directory_durable(evidence_root / "toolchain", mode=0o700)
        _copy_private_file_exact(
            legacy_tool_receipt,
            evidence_root / "toolchain/receipt.json",
            label="legacy toolchain receipt",
        )
        legacy_evidence_manifest = _tree_manifest(
            evidence_root, label="legacy evidence"
        )

        prepared: dict[str, Any] = {
            "schema_version": 1,
            "status": LEGACY_MIGRATION_PREPARED_STATUS,
            "source_root": str(legacy_root),
            "target_root": str(root.resolve()),
            "source_cluster": LEGACY_CLUSTER,
            "target_cluster": DEFAULT_CLUSTER,
            "owner_id": owner_id,
            "legacy_cell_receipt_sha256": sha256_file(
                legacy_root / "receipts/cell-bootstrap.json"
            ),
            "legacy_toolchain_receipt_sha256": sha256_file(legacy_tool_receipt),
            "legacy_bootstrap_commit": str(legacy_cell.get("bootstrap_commit") or ""),
            "runtime_secret_sha256": runtime_secret_sha,
            "promotion_manifest": promotion_manifest,
            "legacy_receipts_manifest": receipts_preflight,
            "legacy_evidence_manifest": legacy_evidence_manifest,
            "data_identity": data_identity,
            "toolchain_copied": False,
            "toolchain_action": "regenerate-under-canonical-state-root",
            "legacy_cluster_deleted": legacy_cluster_present,
            "production_changed": False,
        }
        if registry_secret_sha is not None:
            prepared["registry_secret_sha256"] = registry_secret_sha
        receipt_path = _legacy_migration_receipt_path(root)
        atomic_json(receipt_path, prepared, mode=0o600)
        if _read_legacy_state_migration_receipt(root) != prepared:
            raise StagingCellError(
                "prepared legacy-state migration receipt readback mismatch"
            )

        _rename_legacy_data_for_cutover(legacy_data, target_data)
        moved_data = True
        observed_data_identity = {
            name: _real_directory_identity(
                target_data / name, label=f"migrated retained {name} data"
            )
            for name in ("postgres", "nats")
        }
        if observed_data_identity != data_identity:
            raise StagingCellError(
                "retained staging data identity changed during state-root migration"
            )

        _finalize_prepared_legacy_state_migration(
            root, owner_id=owner_id, payload=prepared
        )
    except Exception:
        if moved_data and target_data.exists() and not legacy_data.exists():
            target_data.rename(legacy_data)
            fsync_directory(root)
            fsync_directory(legacy_root)
        receipts_dir = root / "receipts"
        if (
            receipts_dir.exists()
            and receipts_dir.is_dir()
            and not receipts_dir.is_symlink()
        ):
            shutil.rmtree(receipts_dir)
        for path in reversed(copied_paths):
            if path.exists() and path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
        raise

    return _legacy_migration_public_result(root)


def render_kind_config(root: Path) -> Path:
    template_path = ROOT / "platform/clusters/staging/kind.yaml"
    document = yaml.safe_load(template_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("kind") != "Cluster":
        raise StagingCellError("staging kind template is malformed")
    nodes = document.get("nodes")
    if not isinstance(nodes, list) or len(nodes) != 3:
        raise StagingCellError("staging kind template must contain exactly three nodes")
    roles = [node.get("role") if isinstance(node, dict) else None for node in nodes]
    if roles != ["control-plane", "worker", "worker"]:
        raise StagingCellError("staging kind template node roles drift")
    expected_port_mappings = [
        {
            "containerPort": STAGING_GATEWAY_NODE_PORT,
            "hostPort": STAGING_GATEWAY_HOST_PORT,
            "listenAddress": "127.0.0.1",
            "protocol": "TCP",
        }
    ]
    if nodes[0].get("extraPortMappings") != expected_port_mappings:
        raise StagingCellError(
            "staging control plane must expose exactly the localhost-only Gateway proof port"
        )
    if any(node.get("extraPortMappings") for node in nodes[1:]):
        raise StagingCellError(
            "staging worker nodes must not expose host port mappings"
        )

    retained_mounts = (
        (
            "__COMMONTHING_STAGING_POSTGRES_ROOT__",
            root / "data/postgres",
            "/var/local/commonthing-staging/postgres",
        ),
        (
            "__COMMONTHING_STAGING_NATS_ROOT__",
            root / "data/nats",
            "/var/local/commonthing-staging/nats",
        ),
    )
    for _, host_path, _ in retained_mounts:
        host_path.mkdir(parents=True, exist_ok=True)
        linked = host_path.lstat()
        if stat.S_ISLNK(linked.st_mode) or not stat.S_ISDIR(linked.st_mode):
            raise StagingCellError(
                f"staging retained host path must be a real directory: {host_path}"
            )

    for index, node in enumerate(nodes):
        mounts = node.get("extraMounts", []) if isinstance(node, dict) else []
        if index != 1:
            if mounts:
                raise StagingCellError(
                    f"staging kind node {index} must not mount persistent data"
                )
            continue
        if not isinstance(mounts, list) or len(mounts) != len(retained_mounts):
            raise StagingCellError(
                "staging data worker must bind exactly the PostgreSQL and NATS retained mounts"
            )
        for mount, (placeholder, host_path, container_path) in zip(
            mounts, retained_mounts, strict=True
        ):
            if mount.get("hostPath") != placeholder:
                raise StagingCellError("staging data worker hostPath template drift")
            if mount.get("containerPath") != container_path:
                raise StagingCellError("staging data worker containerPath drift")
            if mount.get("readOnly") is not False:
                raise StagingCellError(
                    "staging data worker persistent mount must be writable"
                )
            mount["hostPath"] = str(host_path.resolve())
    rendered = yaml.safe_dump(document, sort_keys=False)
    path = root / "generated/kind.yaml"
    atomic_text(path, rendered, mode=0o600)
    return path

def load_or_create_secret_material(root: Path) -> tuple[dict[str, str], str]:
    path = root / "secrets/staging-runtime.json"
    if path.exists():
        linked = path.lstat()
        if stat.S_ISLNK(linked.st_mode) or not stat.S_ISREG(linked.st_mode):
            raise StagingCellError("staging secret source must be a regular file")
        if stat.S_IMODE(linked.st_mode) & 0o077:
            raise StagingCellError(
                "staging secret source must not be group/world accessible"
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeError) as exc:
            raise StagingCellError("staging secret source is unreadable or malformed") from exc
        if not isinstance(payload, dict):
            raise StagingCellError("staging secret source is malformed")
    else:
        if retained_postgres_state_exists(root):
            raise StagingCellError(
                "staging secret source is missing while retained PostgreSQL state exists; "
                "restore the original secret source or perform an explicit backup/restore recovery"
            )
        payload = {
            "schema_version": 1,
            "database_user": "weltgewebe",
            "database_name": "weltgewebe",
            "database_password": secrets.token_urlsafe(36),
        }
        atomic_json(path, payload, mode=0o600)
    required = ("database_user", "database_name", "database_password")
    if payload.get("schema_version") != 1 or any(
        not isinstance(payload.get(key), str) or not payload[key] for key in required
    ):
        raise StagingCellError("staging secret source is malformed")
    source_sha = sha256_file(path)
    recorded_sha = recorded_secret_source_sha(root)
    if recorded_sha is not None and not hmac.compare_digest(source_sha, recorded_sha):
        raise StagingCellError(
            "staging secret source differs from the bootstrap receipt while retained PostgreSQL "
            "state exists; restore the original source or use an explicit credential-rotation recovery"
        )
    return {key: str(payload[key]) for key in required}, source_sha


def load_registry_pull_material(root: Path) -> tuple[dict[str, str], str]:
    path = root / "secrets/staging-registry.json"
    try:
        linked = path.lstat()
    except OSError as error:
        raise StagingCellError(
            "staging registry credential source is missing; provide an owner-private external "
            "read:packages credential at secrets/staging-registry.json"
        ) from error
    if (
        stat.S_ISLNK(linked.st_mode)
        or not stat.S_ISREG(linked.st_mode)
        or linked.st_uid != os.geteuid()
        or stat.S_IMODE(linked.st_mode) != 0o600
    ):
        raise StagingCellError(
            "staging registry credential source must be an owner-owned mode-0600 regular file"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeError) as error:
        raise StagingCellError("staging registry credential source is malformed") from error
    required = ("registry", "username", "token")
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("registry") != GHCR_REGISTRY
        or any(
            not isinstance(payload.get(key), str)
            or not payload[key]
            or any(character in payload[key] for character in "\r\n\x00")
            for key in required
        )
    ):
        raise StagingCellError("staging registry credential source is malformed")
    return {key: str(payload[key]) for key in required}, sha256_file(path)


def registry_dockerconfig_json(material: dict[str, str]) -> str:
    if material.get("registry") != GHCR_REGISTRY:
        raise StagingCellError("staging registry credential targets an unexpected registry")
    username = material.get("username") or ""
    token = material.get("token") or ""
    if not username or not token:
        raise StagingCellError("staging registry credential is incomplete")
    auth = base64.b64encode(f"{username}:{token}".encode("utf-8")).decode("ascii")
    return json.dumps(
        {"auths": {GHCR_REGISTRY: {"auth": auth}}},
        sort_keys=True,
        separators=(",", ":"),
    )


class _NoRegistryRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req,
        fp,
        code,
        msg,
        headers,
        newurl,
    ):
        del req, fp, code, msg, headers, newurl
        return None


def registry_urlopen(request: urllib.request.Request):
    opener = urllib.request.build_opener(_NoRegistryRedirectHandler())
    return opener.open(request, timeout=15)


def verify_ghcr_pull_access(
    material: dict[str, str], promotion: dict[str, Any]
) -> dict[str, bool]:
    username = material.get("username") or ""
    token = material.get("token") or ""
    if material.get("registry") != GHCR_REGISTRY or not username or not token:
        raise StagingCellError("staging registry credential is incomplete")
    images = promotion.get("images") if isinstance(promotion, dict) else None
    if not isinstance(images, dict):
        raise StagingCellError("promotion evidence has no verified images")
    basic = base64.b64encode(f"{username}:{token}".encode("utf-8")).decode("ascii")
    verified: dict[str, bool] = {}
    for label in ("api", "web"):
        reference_value = images.get(label)
        if not isinstance(reference_value, str) or "@sha256:" not in reference_value:
            raise StagingCellError(f"verified {label} image is not digest-bound")
        prefix = f"{GHCR_REGISTRY}/"
        if not reference_value.startswith(prefix):
            raise StagingCellError(f"verified {label} image targets an unexpected registry")
        repository, digest = reference_value[len(prefix):].rsplit("@", 1)
        canonical_image_digest(digest, label=label)
        query = urllib.parse.urlencode(
            {
                "service": GHCR_REGISTRY,
                "scope": f"repository:{repository}:pull",
            }
        )
        token_request = urllib.request.Request(
            f"https://{GHCR_REGISTRY}/token?{query}",
            headers={"Authorization": f"Basic {basic}"},
        )
        try:
            with registry_urlopen(token_request) as response:
                token_payload = json.loads(response.read().decode("utf-8"))
            bearer = token_payload.get("token") or token_payload.get("access_token")
            if not isinstance(bearer, str) or not bearer:
                raise StagingCellError(
                    f"registry pull preflight returned no bearer token for {label}"
                )
            manifest_request = urllib.request.Request(
                f"https://{GHCR_REGISTRY}/v2/{repository}/manifests/{digest}",
                headers={
                    "Authorization": f"Bearer {bearer}",
                    "Accept": ", ".join(
                        (
                            "application/vnd.oci.image.index.v1+json",
                            "application/vnd.docker.distribution.manifest.list.v2+json",
                            "application/vnd.oci.image.manifest.v1+json",
                        )
                    ),
                },
                method="HEAD",
            )
            with registry_urlopen(manifest_request) as response:
                observed_digest = response.headers.get("Docker-Content-Digest")
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            urllib.error.HTTPError,
            urllib.error.URLError,
        ) as error:
            raise StagingCellError(
                f"registry pull preflight failed for promoted {label} image"
            ) from error
        if observed_digest != digest:
            raise StagingCellError(
                f"registry pull preflight digest mismatch for promoted {label} image"
            )
        verified[label] = True
    return verified


def registry_secret_document_matches(
    document: dict[str, Any],
    *,
    source_sha: str,
    expected_config_sha256: str,
) -> bool:
    metadata = document.get("metadata") if isinstance(document, dict) else None
    annotations = metadata.get("annotations") if isinstance(metadata, dict) else None
    if (
        not isinstance(metadata, dict)
        or metadata.get("name") != REGISTRY_SECRET
        or metadata.get("namespace") != APP_NAMESPACE
        or not isinstance(annotations, dict)
        or annotations.get(REGISTRY_SOURCE_ANNOTATION) != source_sha
        or "kubectl.kubernetes.io/last-applied-configuration" in annotations
        or document.get("type") != "kubernetes.io/dockerconfigjson"
    ):
        return False
    data = document.get("data")
    if not isinstance(data, dict):
        return False
    encoded = data.get(".dockerconfigjson")
    if not isinstance(encoded, str):
        return False
    try:
        observed = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(sha256_bytes(observed), expected_config_sha256)


def inject_registry_pull_secret(
    kubectl: str,
    root: Path,
    *,
    material: dict[str, str] | None = None,
    source_sha: str | None = None,
) -> dict[str, str]:
    if material is None or source_sha is None:
        material, source_sha = load_registry_pull_material(root)
    config = registry_dockerconfig_json(material)
    config_sha256 = sha256_bytes(config.encode("utf-8"))
    apply_yaml_server_side(
        kubectl,
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": REGISTRY_SECRET,
                "namespace": APP_NAMESPACE,
                "annotations": {REGISTRY_SOURCE_ANNOTATION: source_sha},
            },
            "type": "kubernetes.io/dockerconfigjson",
            "data": {
                ".dockerconfigjson": base64.b64encode(
                    config.encode("utf-8")
                ).decode("ascii")
            },
        },
        field_manager="commonthing-staging-registry",
    )
    return {
        "source_sha256": source_sha,
        "config_sha256": config_sha256,
        "secret_name": REGISTRY_SECRET,
        "registry": GHCR_REGISTRY,
    }


def verify_registry_pull_secret_binding(
    kubectl: str,
    *,
    expected_source_sha: str,
    expected_config_sha256: str,
) -> dict[str, Any]:
    try:
        document = json.loads(
            output(
                [
                    kubectl,
                    "-n",
                    APP_NAMESPACE,
                    "get",
                    "secret",
                    REGISTRY_SECRET,
                    "-o",
                    "json",
                ]
            )
        )
        ready = registry_secret_document_matches(
            document,
            source_sha=expected_source_sha,
            expected_config_sha256=expected_config_sha256,
        )
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return {"ready": False}
    return {
        "ready": ready,
        "source_sha256": expected_source_sha if ready else None,
        "config_sha256": expected_config_sha256 if ready else None,
    }


def database_url(material: dict[str, str]) -> str:
    encoded_user = urllib.parse.quote(material["database_user"], safe="")
    encoded_password = urllib.parse.quote(material["database_password"], safe="")
    encoded_db = urllib.parse.quote(material["database_name"], safe="")
    return (
        f"postgres://{encoded_user}:{encoded_password}@postgres.{DATA_NAMESPACE}.svc.cluster.local:5432/"
        f"{encoded_db}?sslmode=disable"
    )


def secret_document_matches(
    document: dict[str, Any],
    *,
    name: str,
    namespace_name: str,
    source_sha: str,
    expected_values: dict[str, str],
) -> bool:
    metadata = document.get("metadata") if isinstance(document, dict) else None
    if not isinstance(metadata, dict):
        return False
    annotations = metadata.get("annotations")
    if (
        metadata.get("name") != name
        or metadata.get("namespace") != namespace_name
        or not isinstance(annotations, dict)
        or annotations.get(SECRET_SOURCE_ANNOTATION) != source_sha
    ):
        return False
    data = document.get("data")
    if not isinstance(data, dict):
        return False
    for key, expected in expected_values.items():
        encoded = data.get(key)
        if not isinstance(encoded, str):
            return False
        try:
            observed = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            return False
        if not hmac.compare_digest(observed, expected.encode("utf-8")):
            return False
    return True


def verify_external_secret_binding(kubectl: str, root: Path) -> dict[str, Any]:
    material, source_sha = load_or_create_secret_material(root)
    expected = {
        "database": (
            DATA_NAMESPACE,
            DATABASE_SECRET,
            {
                "username": material["database_user"],
                "password": material["database_password"],
                "database": material["database_name"],
            },
        ),
        "runtime": (
            APP_NAMESPACE,
            RUNTIME_SECRET,
            {"database-url": database_url(material)},
        ),
    }
    matches: dict[str, bool] = {}
    for label, (namespace_name, name, expected_values) in expected.items():
        document = json.loads(
            output(
                [
                    kubectl,
                    "-n",
                    namespace_name,
                    "get",
                    "secret",
                    name,
                    "-o",
                    "json",
                ]
            )
        )
        matches[label] = secret_document_matches(
            document,
            name=name,
            namespace_name=namespace_name,
            source_sha=source_sha,
            expected_values=expected_values,
        )
    return {
        "database": matches.get("database", False),
        "runtime": matches.get("runtime", False),
        "ready": bool(matches) and all(matches.values()),
    }


def flux_revision_matches_commit(revision: str, commit: str) -> bool:
    if len(commit) != 40 or any(ch not in "0123456789abcdef" for ch in commit):
        return False
    return revision in {commit, f"sha1:{commit}"} or revision.endswith(
        f"@sha1:{commit}"
    )


def apply_yaml(kubectl: str, documents: list[dict[str, Any]] | dict[str, Any]) -> None:
    docs = documents if isinstance(documents, list) else [documents]
    body = yaml.safe_dump_all(docs, sort_keys=False, explicit_start=True)
    run([kubectl, "apply", "-f", "-"], input_text=body, timeout=120)


def apply_yaml_server_side(
    kubectl: str,
    documents: list[dict[str, Any]] | dict[str, Any],
    *,
    field_manager: str,
) -> None:
    if not field_manager or any(character.isspace() for character in field_manager):
        raise StagingCellError("server-side apply field manager is invalid")
    docs = documents if isinstance(documents, list) else [documents]
    body = yaml.safe_dump_all(docs, sort_keys=False, explicit_start=True)
    run(
        [
            kubectl,
            "apply",
            "--server-side",
            f"--field-manager={field_manager}",
            "-f",
            "-",
        ],
        input_text=body,
        timeout=120,
    )


def namespace(name: str) -> dict[str, Any]:
    labels = {
        "pod-security.kubernetes.io/enforce": "restricted",
        "pod-security.kubernetes.io/audit": "restricted",
        "pod-security.kubernetes.io/warn": "restricted",
    }
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": name, "labels": labels},
    }


def inject_external_secrets(kubectl: str, root: Path) -> dict[str, str]:
    material, source_sha = load_or_create_secret_material(root)
    username = material["database_user"]
    password = material["database_password"]
    database = material["database_name"]
    runtime_database_url = database_url(material)
    annotations = {SECRET_SOURCE_ANNOTATION: source_sha}
    apply_yaml(kubectl, [namespace(DATA_NAMESPACE), namespace(APP_NAMESPACE)])
    apply_yaml_server_side(
        kubectl,
        [
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {
                    "name": DATABASE_SECRET,
                    "namespace": DATA_NAMESPACE,
                    "annotations": annotations,
                },
                "type": "Opaque",
                "stringData": {
                    "username": username,
                    "password": password,
                    "database": database,
                },
            },
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {
                    "name": RUNTIME_SECRET,
                    "namespace": APP_NAMESPACE,
                    "annotations": annotations,
                },
                "type": "Opaque",
                "stringData": {"database-url": runtime_database_url},
            },
        ],
        field_manager="commonthing-staging-secrets",
    )
    return {"source_sha256": source_sha, "required_keys": ["database-url"]}


def public_external_secret_state() -> dict[str, Any]:
    return {"bound": True, "required_keys": ["database-url"]}


def _retained_mount_node(
    kind: str,
    cluster: str,
    root: Path,
    *,
    require_split: bool,
) -> str:
    nodes = reference.kind_nodes(kind, cluster)
    if len(nodes) != 3:
        raise StagingCellError(
            f"staging cluster must expose exactly three kind nodes; observed {len(nodes)}"
        )
    data_node = data_node_name(cluster)
    if data_node not in nodes:
        raise StagingCellError(
            f"staging data worker {data_node!r} is missing from kind nodes {nodes!r}"
        )

    retained_destinations = {
        "/var/local/commonthing-staging",
        "/var/local/commonthing-staging/postgres",
        "/var/local/commonthing-staging/nats",
    }
    parent_expected = {
        "/var/local/commonthing-staging": str((root / "data").resolve())
    }
    split_expected = {
        "/var/local/commonthing-staging/postgres": str(
            (root / "data/postgres").resolve()
        ),
        "/var/local/commonthing-staging/nats": str((root / "data/nats").resolve()),
    }
    for node in nodes:
        raw_mounts = output(
            ["docker", "inspect", "--format", "{{json .Mounts}}", node],
            timeout=30,
        )
        try:
            mounts = json.loads(raw_mounts)
        except json.JSONDecodeError as error:
            raise StagingCellError(
                f"cannot inspect staging kind mount topology for node {node!r}"
            ) from error
        if not isinstance(mounts, list):
            raise StagingCellError(
                f"cannot inspect staging kind mount topology for node {node!r}"
            )
        retained = [
            mount
            for mount in mounts
            if isinstance(mount, dict)
            and mount.get("Destination") in retained_destinations
        ]
        if node != data_node:
            if retained:
                raise StagingCellError(
                    f"staging non-data node {node!r} unexpectedly exposes retained host storage"
                )
            continue

        if any(mount.get("RW") is not True for mount in retained):
            raise StagingCellError(
                "staging data worker retained host storage must be writable"
            )
        observed = {
            str(mount.get("Destination")): str(mount.get("Source"))
            for mount in retained
        }
        if len(observed) != len(retained):
            raise StagingCellError(
                "staging data worker has duplicate retained host mount destinations"
            )
        if require_split:
            if observed != split_expected:
                raise StagingCellError(
                    "staging data worker does not expose the exact split retained host mounts"
                )
        elif observed not in (parent_expected, split_expected):
            raise StagingCellError(
                "staging data worker does not expose an accepted retained host mount topology"
            )
    return data_node


def prepare_volume_permissions(kind: str, cluster: str, root: Path) -> None:
    data_node = _retained_mount_node(
        kind, cluster, root, require_split=True
    )
    for volume_path, identity in (
        ("/var/local/commonthing-staging/postgres", "999:999"),
        ("/var/local/commonthing-staging/nats", "1000:1000"),
    ):
        run(["docker", "exec", data_node, "mkdir", "-p", volume_path], timeout=30)
        observed = output(
            ["docker", "exec", data_node, "stat", "-c", "%u:%g:%a", volume_path],
            timeout=30,
        )
        uid_gid, _, mode = observed.rpartition(":")
        if uid_gid == identity and mode in ALLOWED_RETAINED_VOLUME_MODES:
            continue

        first_entry = output(
            [
                "docker",
                "exec",
                data_node,
                "find",
                volume_path,
                "-mindepth",
                "1",
                "-maxdepth",
                "1",
                "-print",
                "-quit",
            ],
            timeout=30,
        )
        if first_entry:
            raise StagingCellError(
                f"retained staging volume {volume_path!r} has unexpected permissions {observed!r}; "
                "refusing recursive ownership or mode changes over live data"
            )
        run(["docker", "exec", data_node, "chown", identity, volume_path], timeout=30)
        run(["docker", "exec", data_node, "chmod", "0700", volume_path], timeout=30)
        initialized = output(
            ["docker", "exec", data_node, "stat", "-c", "%u:%g:%a", volume_path],
            timeout=30,
        )
        if initialized != f"{identity}:700":
            raise StagingCellError(
                f"staging volume {volume_path!r} permission initialization failed: {initialized!r}"
            )


def _mounted_retained_data_anchors(
    kind: str,
    cluster: str,
    root: Path,
    *,
    require_split: bool,
) -> dict[str, dict[str, Any]]:
    data_node = _retained_mount_node(
        kind, cluster, root, require_split=require_split
    )
    identity = _retained_data_identity(root, include_content=False)
    for name in ("postgres", "nats"):
        volume_path = f"/var/local/commonthing-staging/{name}"
        observed = output(
            [
                "docker",
                "exec",
                data_node,
                "stat",
                "-c",
                "%d:%i:%u:%g:%a",
                volume_path,
            ],
            timeout=30,
        )
        fields = observed.split(":")
        if len(fields) != 5:
            raise StagingCellError(
                f"cannot parse mounted retained {name} directory identity"
            )
        try:
            mounted = {
                "device": int(fields[0]),
                "inode": int(fields[1]),
                "uid": int(fields[2]),
                "gid": int(fields[3]),
                "mode": int(fields[4], 8),
            }
        except ValueError as error:
            raise StagingCellError(
                f"cannot parse mounted retained {name} directory identity"
            ) from error
        for field in ("device", "inode", "uid", "gid", "mode"):
            if mounted[field] != identity[name][field]:
                raise StagingCellError(
                    f"mounted retained {name} directory is not the verified host directory"
                )
    return identity


def _mounted_retained_data_identity(
    kind: str,
    cluster: str,
    root: Path,
    *,
    durable: bool,
    require_split: bool,
) -> dict[str, dict[str, Any]]:
    identity = _mounted_retained_data_anchors(
        kind, cluster, root, require_split=require_split
    )
    data_node = data_node_name(cluster)
    if durable:
        run(["docker", "exec", data_node, "sync"], timeout=120)

    fingerprint_script = (
        "set -euo pipefail\n"
        "volume=\"$1\"\n"
        "if [ ! -d \"$volume\" ] || [ -L \"$volume\" ]; then exit 41; fi\n"
        "if find -P \"$volume\" -mindepth 1 \\( -type l -o \\( ! -type f ! -type d \\) \\) -print -quit | grep -q .; then exit 42; fi\n"
        "LC_ALL=C tar --sort=name --format=gnu --numeric-owner --owner=0 --group=0 --mtime=@0 -cf - -C \"$volume\" . | sha256sum | awk '{print $1}'\n"
    )
    for name in ("postgres", "nats"):
        volume_path = f"/var/local/commonthing-staging/{name}"
        try:
            tree_sha = output(
                [
                    "docker",
                    "exec",
                    data_node,
                    "bash",
                    "-ceu",
                    fingerprint_script,
                    "bash",
                    volume_path,
                ],
                timeout=300,
            )
        except subprocess.CalledProcessError as error:
            raise StagingCellError(
                f"cannot fingerprint mounted retained {name} data safely"
            ) from error
        identity[name]["tree_sha256"] = _canonical_sha256(
            tree_sha, label=f"mounted retained {name} tree hash"
        )
    return identity


def _data_reconciliation_is_suspended(kubectl: str) -> bool:
    observed = output(
        [
            kubectl,
            "get",
            "kustomization",
            DATA_KUSTOMIZATION,
            "-n",
            "flux-system",
            "-o",
            "jsonpath={.spec.suspend}",
        ],
        timeout=30,
    )
    if observed not in {"true", "false"}:
        raise StagingCellError("staging data reconciliation suspension state is invalid")
    return observed == "true"


def _set_data_reconciliation_suspended(kubectl: str, *, suspended: bool) -> None:
    expected = "true" if suspended else "false"
    run(
        [
            kubectl,
            "patch",
            "kustomization",
            DATA_KUSTOMIZATION,
            "-n",
            "flux-system",
            "--type=merge",
            "-p",
            json.dumps({"spec": {"suspend": suspended}}, separators=(",", ":")),
        ],
        timeout=60,
    )
    observed = output(
        [
            kubectl,
            "get",
            "kustomization",
            DATA_KUSTOMIZATION,
            "-n",
            "flux-system",
            "-o",
            "jsonpath={.spec.suspend}",
        ],
        timeout=30,
    )
    if observed != expected:
        state = "suspend" if suspended else "resume"
        raise StagingCellError(
            f"staging data reconciliation did not {state} as requested"
        )


def _set_app_reconciliation_suspended(kubectl: str, *, suspended: bool) -> None:
    expected = "true" if suspended else "false"
    run(
        [
            kubectl,
            "patch",
            "kustomization",
            APP_KUSTOMIZATION,
            "-n",
            "flux-system",
            "--type=merge",
            "-p",
            json.dumps({"spec": {"suspend": suspended}}, separators=(",", ":")),
        ],
        timeout=60,
    )
    observed = output(
        [
            kubectl,
            "get",
            "kustomization",
            APP_KUSTOMIZATION,
            "-n",
            "flux-system",
            "-o",
            "jsonpath={.spec.suspend}",
        ],
        timeout=30,
    )
    if observed != expected:
        state = "suspend" if suspended else "resume"
        raise StagingCellError(
            f"staging app reconciliation did not {state} as requested"
        )


def _quiesce_backup_app(kubectl: str) -> None:
    _set_app_reconciliation_suspended(kubectl, suspended=True)
    run(
        [
            kubectl,
            "scale",
            "deployment/commonthing-api",
            "deployment/commonthing-web",
            "-n",
            APP_NAMESPACE,
            "--replicas=0",
        ],
        timeout=60,
    )
    deadline = time.monotonic() + 120.0
    while True:
        remaining: list[str] = []
        for name, (_, deployment) in APP_DEPLOYMENTS.items():
            pods = output(
                [
                    kubectl,
                    "get",
                    "pods",
                    "-n",
                    APP_NAMESPACE,
                    "-l",
                    f"app.kubernetes.io/name={deployment}",
                    "-o",
                    "name",
                ],
                timeout=30,
            )
            if pods:
                remaining.append(name)
        if not remaining:
            return
        if time.monotonic() >= deadline:
            raise StagingCellError(
                f"staging app workloads did not quiesce before backup: {remaining!r}"
            )
        time.sleep(1.0)


def _quiesce_backup_cell(kubectl: str) -> None:
    _quiesce_backup_app(kubectl)
    _quiesce_retained_data(kubectl)


def _quiesce_retained_data(kubectl: str) -> None:
    _set_data_reconciliation_suspended(kubectl, suspended=True)
    run(
        [
            kubectl,
            "scale",
            "deployment/postgres",
            "deployment/nats",
            "-n",
            DATA_NAMESPACE,
            "--replicas=0",
        ],
        timeout=60,
    )
    deadline = time.monotonic() + 120.0
    while True:
        remaining: list[str] = []
        for name in ("postgres", "nats"):
            pods = output(
                [
                    kubectl,
                    "get",
                    "pods",
                    "-n",
                    DATA_NAMESPACE,
                    "-l",
                    f"app.kubernetes.io/name={name}",
                    "-o",
                    "name",
                ],
                timeout=30,
            )
            if pods:
                remaining.append(name)
        if not remaining:
            break
        if time.monotonic() >= deadline:
            raise StagingCellError(
                f"staging data workloads did not quiesce before down: {remaining!r}"
            )
        time.sleep(1.0)
    for name in ("postgres", "nats"):
        replicas = output(
            [
                kubectl,
                "get",
                "deployment",
                name,
                "-n",
                DATA_NAMESPACE,
                "-o",
                "jsonpath={.spec.replicas}",
            ],
            timeout=30,
        )
        if replicas != "0":
            raise StagingCellError(
                f"staging data deployment {name!r} is not quiescent before down"
            )

def flux_documents(
    commit: str, *, suspend_data: bool = False
) -> list[dict[str, Any]]:
    data_spec: dict[str, Any] = {
        "interval": "2m",
        "retryInterval": "20s",
        "timeout": DATA_KUSTOMIZATION_TIMEOUT,
        "prune": True,
        "wait": True,
        "sourceRef": {"kind": "GitRepository", "name": SOURCE_NAME},
        "path": "./platform/clusters/staging/data",
        "healthChecks": [
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "name": "postgres",
                "namespace": DATA_NAMESPACE,
            },
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "name": "nats",
                "namespace": DATA_NAMESPACE,
            },
        ],
    }
    if suspend_data:
        data_spec["suspend"] = True
    return [
        {
            "apiVersion": "source.toolkit.fluxcd.io/v1",
            "kind": "GitRepository",
            "metadata": {"name": SOURCE_NAME, "namespace": "flux-system"},
            "spec": {
                "interval": "1m",
                "url": PUBLIC_REPOSITORY,
                "ref": {"commit": commit},
            },
        },
        {
            "apiVersion": "kustomize.toolkit.fluxcd.io/v1",
            "kind": "Kustomization",
            "metadata": {"name": DATA_KUSTOMIZATION, "namespace": "flux-system"},
            "spec": data_spec,
        },
    ]

def pvc_phase_snapshot(kubectl: str) -> dict[str, str]:
    pvcs = ("postgres-data", "nats-data")
    raw = output(
        [
            kubectl,
            "-n",
            DATA_NAMESPACE,
            "get",
            "pvc",
            *pvcs,
            "--ignore-not-found",
            "-o",
            "json",
        ]
    )
    if not raw:
        return {pvc: "missing" for pvc in pvcs}
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as error:
        raise StagingCellError("cannot parse staging PVC status JSON") from error
    if not isinstance(document, dict):
        raise StagingCellError("staging PVC status payload is not an object")
    items = document.get("items")
    if isinstance(items, list):
        documents = items
    elif document.get("kind") == "PersistentVolumeClaim":
        documents = [document]
    else:
        raise StagingCellError("staging PVC status payload has no resource items")
    observed = {pvc: "missing" for pvc in pvcs}
    for item in documents:
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata")
        status = item.get("status")
        if not isinstance(metadata, dict) or not isinstance(status, dict):
            continue
        name = metadata.get("name")
        if name in observed:
            phase = status.get("phase")
            observed[str(name)] = phase if isinstance(phase, str) and phase else "missing"
    return observed


def wait_pvcs_bound(
    kubectl: str,
    *,
    visibility_timeout_seconds: float = DATA_KUSTOMIZATION_TIMEOUT_SECONDS,
    bind_timeout_seconds: float = PVC_BIND_TIMEOUT_SECONDS,
) -> None:
    pvcs = ("postgres-data", "nats-data")
    visibility_deadline = time.monotonic() + visibility_timeout_seconds
    bind_deadline: float | None = None
    while True:
        observed = pvc_phase_snapshot(kubectl)
        if all(observed[pvc] == "Bound" for pvc in pvcs):
            return
        unexpected = {
            pvc: phase
            for pvc, phase in observed.items()
            if phase not in {"missing", "Pending", "Bound"}
        }
        if unexpected:
            raise StagingCellError(f"staging PVC entered unexpected phase: {unexpected!r}")
        now = time.monotonic()
        all_visible = all(observed[pvc] != "missing" for pvc in pvcs)
        if all_visible and bind_deadline is None:
            bind_deadline = min(now + bind_timeout_seconds, visibility_deadline)
        if bind_deadline is not None and now >= bind_deadline:
            raise StagingCellError(
                "staging PVCs did not bind within "
                f"{bind_timeout_seconds:g}s after becoming visible: {observed!r}"
            )
        if bind_deadline is None and now >= visibility_deadline:
            raise StagingCellError(
                "staging PVCs did not become visible within the Flux Kustomization "
                f"budget of {visibility_timeout_seconds:g}s: {observed!r}"
            )
        time.sleep(1)


def flux_resource_current_state(
    kubectl: str, resource: str, name: str, commit: str
) -> dict[str, Any]:
    if resource not in {"gitrepository", "kustomization"}:
        raise StagingCellError(f"unsupported Flux resource type: {resource}")
    raw = output(
        [
            kubectl,
            "-n",
            "flux-system",
            "get",
            resource,
            name,
            "--ignore-not-found",
            "-o",
            "json",
        ]
    )
    if not raw:
        return {
            "ready": "missing",
            "revision": "missing",
            "matches_commit": False,
            "current_generation": False,
        }
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as error:
        raise StagingCellError(f"cannot parse Flux {resource} status JSON") from error
    if not isinstance(document, dict):
        raise StagingCellError(f"Flux {resource} status payload is not an object")
    metadata = document.get("metadata")
    status_payload = document.get("status")
    if not isinstance(metadata, dict) or not isinstance(status_payload, dict):
        return {
            "ready": "missing",
            "revision": "missing",
            "matches_commit": False,
            "current_generation": False,
        }
    generation = metadata.get("generation")
    observed_generation = status_payload.get("observedGeneration")
    current_generation = (
        isinstance(generation, int)
        and not isinstance(generation, bool)
        and isinstance(observed_generation, int)
        and not isinstance(observed_generation, bool)
        and generation == observed_generation
    )
    ready_status = "missing"
    conditions = status_payload.get("conditions")
    if isinstance(conditions, list):
        for condition in conditions:
            if isinstance(condition, dict) and condition.get("type") == "Ready":
                value = condition.get("status")
                if isinstance(value, str) and value:
                    ready_status = value
                break
    ready = ready_status if current_generation else "stale"
    last_handled_value = status_payload.get("lastHandledReconcileAt")
    last_handled = (
        last_handled_value
        if isinstance(last_handled_value, str) and last_handled_value
        else "missing"
    )
    if resource == "gitrepository":
        artifact = status_payload.get("artifact")
        revision_value = artifact.get("revision") if isinstance(artifact, dict) else None
    else:
        revision_value = status_payload.get("lastAppliedRevision")
    revision = revision_value if isinstance(revision_value, str) and revision_value else "missing"
    return {
        "ready": ready,
        "revision": revision,
        "matches_commit": flux_revision_matches_commit(revision, commit),
        "current_generation": current_generation,
        "last_handled_reconcile_at": last_handled,
    }


def wait_flux_resource_current(
    kubectl: str,
    resource: str,
    name: str,
    commit: str,
    *,
    timeout_seconds: float = DATA_KUSTOMIZATION_TIMEOUT_SECONDS,
    requested_at: str | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    observed: dict[str, Any] = {}
    while True:
        observed = flux_resource_current_state(kubectl, resource, name, commit)
        handled_request = (
            requested_at is None
            or observed.get("last_handled_reconcile_at") == requested_at
        )
        if (
            observed["ready"] == "True"
            and observed["matches_commit"]
            and handled_request
        ):
            return observed
        if time.monotonic() >= deadline:
            raise StagingCellError(
                f"Flux {resource}/{name} did not reach the current receipt-bound revision "
                f"within {timeout_seconds:g}s: ready={observed.get('ready')!r} "
                f"revision={observed.get('revision')!r} "
                f"lastHandledReconcileAt={observed.get('last_handled_reconcile_at')!r}"
            )
        time.sleep(1)


def request_flux_reconcile(
    kubectl: str, resource: str, name: str, requested_at: str
) -> None:
    if resource not in {"gitrepository", "kustomization"}:
        raise StagingCellError(f"unsupported Flux reconcile resource type: {resource}")
    run(
        [
            kubectl,
            "-n",
            "flux-system",
            "annotate",
            f"{resource}/{name}",
            f"reconcile.fluxcd.io/requestedAt={requested_at}",
            "--field-manager=flux-client-side-apply",
            "--overwrite",
        ],
        timeout=30,
    )


def reconcile_data(kubectl: str, commit: str) -> str:
    requested_at = f"staging-up-{time.time_ns()}"
    request_flux_reconcile(kubectl, "gitrepository", SOURCE_NAME, requested_at)
    wait_flux_resource_current(
        kubectl,
        "gitrepository",
        SOURCE_NAME,
        commit,
        requested_at=requested_at,
    )

    request_flux_reconcile(
        kubectl, "kustomization", DATA_KUSTOMIZATION, requested_at
    )
    deadline = time.monotonic() + DATA_KUSTOMIZATION_TIMEOUT_SECONDS
    pvc_budget = max(0.0, deadline - time.monotonic())
    wait_pvcs_bound(kubectl, visibility_timeout_seconds=pvc_budget)
    remaining = max(0.0, deadline - time.monotonic())
    wait_flux_resource_current(
        kubectl,
        "kustomization",
        DATA_KUSTOMIZATION,
        commit,
        timeout_seconds=remaining,
        requested_at=requested_at,
    )
    return requested_at


def require_bootstrap_data_current(kubectl: str, commit: str) -> dict[str, dict[str, Any]]:
    states = {
        "source": flux_resource_current_state(
            kubectl, "gitrepository", SOURCE_NAME, commit
        ),
        "data": flux_resource_current_state(
            kubectl, "kustomization", DATA_KUSTOMIZATION, commit
        ),
    }
    unhealthy = {
        name: state
        for name, state in states.items()
        if state.get("ready") != "True" or state.get("matches_commit") is not True
    }
    if unhealthy:
        raise StagingCellError(
            "staging bootstrap data plane is not current; refusing app activation: "
            f"{unhealthy!r}"
        )
    return states


def deployment_ready_state(kubectl: str, namespace: str, name: str) -> str:
    raw = output(
        [
            kubectl,
            "-n",
            namespace,
            "get",
            "deployment",
            name,
            "--ignore-not-found",
            "-o",
            "json",
        ]
    )
    if not raw:
        return "missing"
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as error:
        raise StagingCellError(
            f"cannot parse deployment health JSON for {namespace}/{name}"
        ) from error
    if not isinstance(document, dict):
        return "missing"
    metadata = document.get("metadata")
    spec = document.get("spec")
    status_payload = document.get("status")
    if not all(isinstance(value, dict) for value in (metadata, spec, status_payload)):
        return "missing"
    generation = metadata.get("generation")
    observed_generation = status_payload.get("observedGeneration")
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or not isinstance(observed_generation, int)
        or isinstance(observed_generation, bool)
        or generation != observed_generation
    ):
        return "stale"
    desired = spec.get("replicas", 1)
    if not isinstance(desired, int) or isinstance(desired, bool) or desired < 1:
        return "False"
    for field in ("availableReplicas", "readyReplicas", "updatedReplicas"):
        value = status_payload.get(field, 0)
        if not isinstance(value, int) or isinstance(value, bool) or value < desired:
            return "False"
    return "True"


def staging_live_health(kubectl: str) -> dict[str, str]:
    return {
        label: deployment_ready_state(kubectl, namespace, name)
        for label, (namespace, name) in LIVE_DEPLOYMENTS.items()
    }


def image_promotion_state() -> dict[str, Any]:
    contract = json.loads(
        (ROOT / "platform/image-promotion.contract.json").read_text(encoding="utf-8")
    )
    return {
        "status": contract.get("status"),
        "production_activation": contract.get("production_activation"),
        "required_images": contract.get("required_images", []),
    }


def canonical_image_digest(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise StagingCellError(f"promotion receipt {label} digest is not sha256-bound")
    digest = value.removeprefix("sha256:")
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise StagingCellError(f"promotion receipt {label} digest is malformed")
    return value


def load_promotion_receipt(root: Path, commit: str) -> dict[str, Any]:
    if len(commit) != 40 or any(ch not in "0123456789abcdef" for ch in commit):
        raise StagingCellError("promotion commit must be canonical 40-hex")
    directory = root / "promotion" / commit
    path = directory / "receipt.json"
    if directory.is_symlink() or not directory.is_dir():
        raise StagingCellError("promotion receipt directory is missing or unsafe")
    try:
        linked = path.lstat()
    except OSError as error:
        raise StagingCellError("promotion receipt is missing or unreadable") from error
    if (
        stat.S_ISLNK(linked.st_mode)
        or not stat.S_ISREG(linked.st_mode)
        or linked.st_uid != os.geteuid()
        or stat.S_IMODE(linked.st_mode) & 0o077
    ):
        raise StagingCellError("promotion receipt must be an owner-private regular file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise StagingCellError("promotion receipt is malformed") from error
    if not isinstance(payload, dict):
        raise StagingCellError("promotion receipt is not an object")
    expected = {
        "schema_version": 1,
        "status": "pass",
        "scope": "staging-only",
        "source_commit": commit,
        "repository": "heimgewebe/commonthing",
        "image_identity": "digest-authoritative",
        "production_activation": False,
    }
    mismatched = {
        key: {"expected": value, "observed": payload.get(key)}
        for key, value in expected.items()
        if payload.get(key) != value
    }
    if mismatched:
        raise StagingCellError(
            "promotion receipt identity mismatch: "
            + json.dumps(mismatched, sort_keys=True)
        )
    images = payload.get("images")
    if not isinstance(images, dict):
        raise StagingCellError("promotion receipt has no image map")
    result_images: dict[str, str] = {}
    for label, canonical in (
        ("api", "ghcr.io/heimgewebe/commonthing-api"),
        ("web", "ghcr.io/heimgewebe/commonthing-web"),
    ):
        image = images.get(label)
        if not isinstance(image, dict) or image.get("canonical") != canonical:
            raise StagingCellError(f"promotion receipt {label} image identity mismatch")
        digest = canonical_image_digest(image.get("digest"), label=label)
        reference_value = f"{canonical}@{digest}"
        if image.get("canonical_reference") != reference_value:
            raise StagingCellError(
                f"promotion receipt {label} canonical reference does not match digest"
            )
        result_images[label] = reference_value
    return {
        "schema_version": 1,
        "status": "pass",
        "source_commit": commit,
        "receipt_path": str(path),
        "receipt_sha256": sha256_file(path),
        "images": result_images,
    }


def migration_plan(commit: str, promotion: dict[str, Any]) -> dict[str, str]:
    if len(commit) != 40 or any(ch not in "0123456789abcdef" for ch in commit):
        raise StagingCellError("migration source commit must be canonical 40-hex")
    receipt_sha = promotion.get("receipt_sha256") if isinstance(promotion, dict) else None
    if (
        not isinstance(receipt_sha, str)
        or len(receipt_sha) != 64
        or any(ch not in "0123456789abcdef" for ch in receipt_sha)
    ):
        raise StagingCellError("promotion evidence has no canonical receipt hash")
    images = promotion.get("images") if isinstance(promotion, dict) else None
    api_image = images.get("api") if isinstance(images, dict) else None
    prefix = "ghcr.io/heimgewebe/commonthing-api@"
    if not isinstance(api_image, str) or not api_image.startswith(prefix):
        raise StagingCellError("promotion evidence has no canonical API image")
    digest = api_image[len(prefix):]
    canonical_image_digest(digest, label="api")
    digest_hex = digest.removeprefix("sha256:")
    job_name = f"{MIGRATION_JOB_PREFIX}-{commit[:10]}-{digest_hex[:12]}"
    if len(job_name) > 63:
        raise StagingCellError("staging migration Job name exceeds Kubernetes limits")
    return {
        "job_name": job_name,
        "source_commit": commit,
        "receipt_sha256": receipt_sha,
        "api_image": api_image,
    }


def require_pending_promotion_matches(
    cell: dict[str, Any], commit: str, promotion: dict[str, Any]
) -> dict[str, str]:
    pending = cell.get("pending_image_promotion")
    expected = {
        "source_commit": commit,
        "receipt_sha256": promotion.get("receipt_sha256"),
        "images": promotion.get("images"),
    }
    if not isinstance(pending, dict) or pending != expected:
        raise StagingCellError(
            "activation recovery promotion evidence differs from the exact pending release"
        )
    plan = migration_plan(commit, promotion)
    if cell.get("pending_migration") != plan:
        raise StagingCellError(
            "activation recovery migration evidence differs from the exact pending release"
        )
    return plan


def require_pending_registry_matches(
    cell: dict[str, Any],
    *,
    source_sha: str,
    config_sha256: str,
) -> dict[str, str]:
    expected = {
        "source_sha256": source_sha,
        "config_sha256": config_sha256,
        "secret_name": REGISTRY_SECRET,
        "registry": GHCR_REGISTRY,
    }
    pending = cell.get("pending_registry_pull_secret")
    if not isinstance(pending, dict) or pending != expected:
        raise StagingCellError(
            "activation recovery registry credential differs from the exact pending release"
        )
    return expected


def migration_network_policy_documents() -> list[dict[str, Any]]:
    expected = (
        ("default-deny", ROOT / "platform/apps/weltgewebe/base/network-policy-default-deny.yaml"),
        ("allow-dns", ROOT / "platform/apps/weltgewebe/base/network-policy-dns.yaml"),
        ("allow-api-data-egress", ROOT / "platform/apps/weltgewebe/base/network-policy-api-data-egress.yaml"),
    )
    documents: list[dict[str, Any]] = []
    for expected_name, path in expected:
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as error:
            raise StagingCellError(
                f"cannot load staging migration NetworkPolicy {expected_name!r}"
            ) from error
        metadata = document.get("metadata") if isinstance(document, dict) else None
        if (
            not isinstance(document, dict)
            or document.get("apiVersion") != "networking.k8s.io/v1"
            or document.get("kind") != "NetworkPolicy"
            or not isinstance(metadata, dict)
            or metadata.get("name") != expected_name
        ):
            raise StagingCellError(
                f"staging migration NetworkPolicy {expected_name!r} is malformed"
            )
        isolated = json.loads(json.dumps(document))
        isolated["metadata"]["namespace"] = APP_NAMESPACE
        if expected_name == "allow-api-data-egress":
            isolated["spec"]["podSelector"]["matchLabels"][
                "app.kubernetes.io/name"
            ] = "commonthing-api"
            isolated["spec"]["egress"][0]["to"][0]["namespaceSelector"]["matchLabels"][
                "kubernetes.io/metadata.name"
            ] = DATA_NAMESPACE
        documents.append(isolated)
    return documents


def _load_json_object(raw: str, *, label: str) -> dict[str, Any]:
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as error:
        raise StagingCellError(f"{label} returned malformed JSON") from error
    if not isinstance(document, dict):
        raise StagingCellError(f"{label} did not return a JSON object")
    return document


def migration_network_policy_bindings(
    kubectl: str, names: list[str]
) -> dict[str, str]:
    bindings: dict[str, str] = {}
    for name in names:
        document = _load_json_object(
            output(
                [
                    kubectl,
                    "-n",
                    APP_NAMESPACE,
                    "get",
                    "networkpolicy",
                    name,
                    "-o",
                    "json",
                ]
            ),
            label=f"staging migration NetworkPolicy {name!r}",
        )
        metadata = document.get("metadata")
        observed_name = metadata.get("name") if isinstance(metadata, dict) else None
        namespace_name = metadata.get("namespace") if isinstance(metadata, dict) else None
        uid = metadata.get("uid") if isinstance(metadata, dict) else None
        if (
            observed_name != name
            or namespace_name != APP_NAMESPACE
            or not isinstance(uid, str)
            or not uid
            or len(uid) > 128
        ):
            raise StagingCellError(
                f"staging migration NetworkPolicy {name!r} has no exact live UID binding"
            )
        bindings[name] = uid
    return bindings


def _remaining_cilium_policy_timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise StagingCellError(
            "Cilium policy enforcement exceeded the bounded activation deadline"
        )
    return min(30.0, remaining)


def ready_cilium_agents(
    kubectl: str, *, deadline: float | None = None
) -> list[tuple[str, str]]:
    node_timeout = (
        30.0 if deadline is None else _remaining_cilium_policy_timeout(deadline)
    )
    nodes = _load_json_object(
        output([kubectl, "get", "nodes", "-o", "json"], timeout=node_timeout),
        label="staging node inventory",
    ).get("items")
    if not isinstance(nodes, list):
        raise StagingCellError("staging node inventory has no item list")
    node_names = {
        str(metadata.get("name"))
        for node in nodes
        if isinstance(node, dict)
        and isinstance((metadata := node.get("metadata")), dict)
        and isinstance(metadata.get("name"), str)
        and metadata.get("name")
    }
    if not node_names or len(node_names) != len(nodes):
        raise StagingCellError("staging node inventory is incomplete")

    pod_timeout = (
        30.0 if deadline is None else _remaining_cilium_policy_timeout(deadline)
    )
    pod_document = _load_json_object(
        output(
            [
                kubectl,
                "-n",
                "kube-system",
                "get",
                "pods",
                "-l",
                "k8s-app=cilium",
                "-o",
                "json",
            ],
            timeout=pod_timeout,
        ),
        label="Cilium agent inventory",
    )
    pods = pod_document.get("items")
    if not isinstance(pods, list):
        raise StagingCellError("Cilium agent inventory has no item list")
    agents: dict[str, str] = {}
    for pod in pods:
        if not isinstance(pod, dict):
            continue
        metadata = pod.get("metadata")
        spec = pod.get("spec")
        status = pod.get("status")
        pod_name = metadata.get("name") if isinstance(metadata, dict) else None
        node_name = spec.get("nodeName") if isinstance(spec, dict) else None
        conditions = status.get("conditions") if isinstance(status, dict) else None
        ready = bool(
            isinstance(conditions, list)
            and any(
                isinstance(condition, dict)
                and condition.get("type") == "Ready"
                and condition.get("status") == "True"
                for condition in conditions
            )
        )
        if (
            not isinstance(pod_name, str)
            or not pod_name
            or not isinstance(node_name, str)
            or node_name not in node_names
            or not isinstance(status, dict)
            or status.get("phase") != "Running"
            or not ready
        ):
            continue
        if node_name in agents:
            raise StagingCellError(
                f"multiple Ready Cilium agents observed for staging node {node_name!r}"
            )
        agents[node_name] = pod_name
    if set(agents) != node_names:
        raise StagingCellError(
            "staging Cilium agent coverage is incomplete; refusing migration pod creation"
        )
    return sorted(agents.items())


def parse_cilium_policy_repository(raw: str) -> tuple[list[dict[str, Any]], int]:
    text = raw.lstrip()
    try:
        payload, offset = json.JSONDecoder().raw_decode(text)
    except json.JSONDecodeError as error:
        raise StagingCellError("Cilium policy repository returned malformed JSON") from error
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise StagingCellError("Cilium policy repository is not a JSON rule list")
    trailer = text[offset:].strip()
    prefix = "Revision:"
    if not trailer.startswith(prefix):
        raise StagingCellError("Cilium policy repository has no revision trailer")
    revision_text = trailer[len(prefix):].strip()
    if not revision_text.isdigit():
        raise StagingCellError("Cilium policy repository revision is malformed")
    return payload, int(revision_text)


def cilium_network_policy_bindings(raw: str) -> tuple[set[tuple[str, str, str]], int]:
    policies, revision = parse_cilium_policy_repository(raw)
    bindings: set[tuple[str, str, str]] = set()
    for policy in policies:
        labels = policy.get("Labels")
        if not isinstance(labels, list):
            continue
        mapped = {
            str(label.get("key")): str(label.get("value"))
            for label in labels
            if isinstance(label, dict)
            and label.get("source") == "k8s"
            and isinstance(label.get("key"), str)
            and isinstance(label.get("value"), str)
        }
        if mapped.get("io.cilium.k8s.policy.derived-from") != "NetworkPolicy":
            continue
        name = mapped.get("io.cilium.k8s.policy.name")
        namespace_name = mapped.get("io.cilium.k8s.policy.namespace")
        uid = mapped.get("io.cilium.k8s.policy.uid")
        if name and namespace_name and uid:
            bindings.add((name, namespace_name, uid))
    return bindings, revision


def wait_migration_network_policy_enforcement(
    kubectl: str,
    policy_uids: dict[str, str],
    *,
    timeout_seconds: float = CILIUM_POLICY_ENFORCEMENT_TIMEOUT_SECONDS,
    poll_seconds: float = CILIUM_POLICY_ENFORCEMENT_POLL_SECONDS,
) -> dict[str, Any]:
    if not policy_uids:
        raise StagingCellError("staging migration NetworkPolicy bindings are empty")
    expected = {
        (name, APP_NAMESPACE, uid) for name, uid in policy_uids.items()
    }
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    last_missing: dict[str, list[str]] = {}
    while True:
        try:
            agents = ready_cilium_agents(kubectl, deadline=deadline)
        except subprocess.TimeoutExpired as error:
            raise StagingCellError(
                "Cilium agent inventory exceeded the bounded activation deadline"
            ) from error
        last_missing = {}
        revisions: list[int] = []
        for node_name, pod_name in agents:
            command_timeout = _remaining_cilium_policy_timeout(deadline)
            try:
                raw = output(
                    [
                        kubectl,
                        "-n",
                        "kube-system",
                        "exec",
                        pod_name,
                        "-c",
                        "cilium-agent",
                        "--",
                        "cilium-dbg",
                        "policy",
                        "get",
                    ],
                    timeout=command_timeout,
                )
            except subprocess.TimeoutExpired as error:
                raise StagingCellError(
                    "Cilium policy read exceeded the bounded activation deadline"
                ) from error
            observed, revision = cilium_network_policy_bindings(raw)
            if time.monotonic() > deadline:
                raise StagingCellError(
                    "Cilium policy enforcement exceeded the bounded activation deadline"
                )
            revisions.append(revision)
            missing = expected - observed
            if missing:
                last_missing[node_name] = sorted(name for name, _, _ in missing)
        now = time.monotonic()
        if now > deadline:
            raise StagingCellError(
                "Cilium policy enforcement exceeded the bounded activation deadline"
            )
        if not last_missing:
            return {
                "processed": True,
                "cilium_agent_count": len(agents),
                "minimum_policy_revision": min(revisions),
            }
        remaining = deadline - now
        if remaining <= 0:
            break
        delay = min(max(0.0, poll_seconds), remaining)
        if delay > 0:
            time.sleep(delay)
    missing_names = sorted({name for names in last_missing.values() for name in names})
    raise StagingCellError(
        "Cilium did not process all staging migration NetworkPolicies before the bounded deadline: "
        f"missing={missing_names!r}"
    )


def apply_migration_network_isolation(kubectl: str) -> dict[str, Any]:
    documents = migration_network_policy_documents()
    apply_yaml(kubectl, documents)
    names = [str(document["metadata"]["name"]) for document in documents]
    policy_uids = migration_network_policy_bindings(kubectl, names)
    enforcement = wait_migration_network_policy_enforcement(kubectl, policy_uids)
    return {
        "policy_names": names,
        "policy_uids": policy_uids,
        **enforcement,
    }


def canonical_migration_job_spec(document: dict[str, Any]) -> dict[str, Any]:
    spec = document.get("spec") if isinstance(document, dict) else None
    if not isinstance(spec, dict):
        raise StagingCellError("staging migration Job has no canonical spec")
    canonical = json.loads(json.dumps(spec))
    metadata = document.get("metadata") if isinstance(document, dict) else None
    uid = metadata.get("uid") if isinstance(metadata, dict) else None
    job_name = metadata.get("name") if isinstance(metadata, dict) else None

    # Kubernetes 1.36 defaults and Job-controller identity fields are added by
    # the API server. Strip only the exact values the live staging API server
    # injects; any changed or additional execution field remains hash-visible.
    for key, default in (
        ("completionMode", "NonIndexed"),
        ("completions", 1),
        ("manualSelector", False),
        ("parallelism", 1),
        ("podReplacementPolicy", "TerminatingOrFailed"),
        ("suspend", False),
    ):
        if canonical.get(key) == default:
            canonical.pop(key)
    selector = canonical.get("selector")
    if (
        isinstance(uid, str)
        and uid
        and selector
        == {"matchLabels": {"batch.kubernetes.io/controller-uid": uid}}
    ):
        canonical.pop("selector")

    template = canonical.get("template")
    template_metadata = template.get("metadata") if isinstance(template, dict) else None
    template_labels = (
        template_metadata.get("labels") if isinstance(template_metadata, dict) else None
    )
    if isinstance(template_labels, dict) and isinstance(uid, str) and uid:
        generated_labels = {
            "batch.kubernetes.io/controller-uid": uid,
            "batch.kubernetes.io/job-name": job_name,
            "controller-uid": uid,
            "job-name": job_name,
        }
        for key, expected in generated_labels.items():
            if expected is not None and template_labels.get(key) == expected:
                template_labels.pop(key)

    pod_spec = template.get("spec") if isinstance(template, dict) else None
    if isinstance(pod_spec, dict):
        for key, default in (
            ("dnsPolicy", "ClusterFirst"),
            ("schedulerName", "default-scheduler"),
            ("terminationGracePeriodSeconds", 30),
        ):
            if pod_spec.get(key) == default:
                pod_spec.pop(key)
        containers = pod_spec.get("containers")
        if isinstance(containers, list):
            for container in containers:
                if not isinstance(container, dict):
                    continue
                for key, default in (
                    ("terminationMessagePath", "/dev/termination-log"),
                    ("terminationMessagePolicy", "File"),
                ):
                    if container.get(key) == default:
                        container.pop(key)
    return canonical


def migration_job_spec_sha256(document: dict[str, Any]) -> str:
    canonical = json.dumps(
        canonical_migration_job_spec(document),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256_bytes(canonical)


def read_staging_migration_job(
    kubectl: str, job_name: str
) -> dict[str, Any] | None:
    raw = output(
        [
            kubectl,
            "-n",
            APP_NAMESPACE,
            "get",
            "job",
            job_name,
            "--ignore-not-found",
            "-o",
            "json",
        ]
    )
    if not raw:
        return None
    return _load_json_object(raw, label="staging migration Job")


def require_staging_migration_job_matches(
    observed: dict[str, Any],
    desired: dict[str, Any],
    plan: dict[str, str],
) -> str:
    metadata = observed.get("metadata") if isinstance(observed, dict) else None
    annotations = metadata.get("annotations") if isinstance(metadata, dict) else None
    desired_metadata = desired.get("metadata") if isinstance(desired, dict) else None
    desired_annotations = (
        desired_metadata.get("annotations") if isinstance(desired_metadata, dict) else None
    )
    pod_spec = (
        observed.get("spec", {}).get("template", {}).get("spec", {})
        if isinstance(observed, dict)
        else {}
    )
    containers = pod_spec.get("containers") if isinstance(pod_spec, dict) else None
    expected_spec_sha = (
        desired_annotations.get(MIGRATION_SPEC_ANNOTATION)
        if isinstance(desired_annotations, dict)
        else None
    )
    observed_spec_sha = migration_job_spec_sha256(observed)
    matches = (
        isinstance(metadata, dict)
        and metadata.get("name") == plan["job_name"]
        and metadata.get("namespace") == APP_NAMESPACE
        and isinstance(annotations, dict)
        and annotations.get("commonthing.net/source-commit") == plan["source_commit"]
        and annotations.get("commonthing.net/promotion-receipt-sha256")
        == plan["receipt_sha256"]
        and isinstance(expected_spec_sha, str)
        and annotations.get(MIGRATION_SPEC_ANNOTATION) == expected_spec_sha
        and hmac.compare_digest(observed_spec_sha, expected_spec_sha)
        and isinstance(containers, list)
        and len(containers) == 1
        and isinstance(containers[0], dict)
        and containers[0].get("image") == plan["api_image"]
    )
    if not matches:
        raise StagingCellError(
            "existing staging migration Job does not match the promoted release; "
            "refusing automatic replacement"
        )
    conditions = observed.get("status", {}).get("conditions", [])
    complete = any(
        isinstance(condition, dict)
        and condition.get("type") == "Complete"
        and condition.get("status") == "True"
        for condition in conditions
    )
    failed = any(
        isinstance(condition, dict)
        and condition.get("type") == "Failed"
        and condition.get("status") == "True"
        for condition in conditions
    )
    if complete and failed:
        raise StagingCellError("staging migration Job has contradictory terminal conditions")
    if complete:
        return "complete"
    if failed:
        return "failed"
    return "active"


def migration_job_document(commit: str, promotion: dict[str, Any]) -> dict[str, Any]:
    plan = migration_plan(commit, promotion)
    try:
        template = yaml.safe_load(MIGRATION_TEMPLATE.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise StagingCellError("cannot load canonical staging migration Job template") from error
    if not isinstance(template, dict) or template.get("kind") != "Job":
        raise StagingCellError("canonical staging migration template is not a Job")
    document = json.loads(json.dumps(template))
    metadata = document.setdefault("metadata", {})
    metadata["name"] = plan["job_name"]
    metadata["namespace"] = APP_NAMESPACE
    metadata["annotations"] = {
        "commonthing.net/source-commit": commit,
        "commonthing.net/promotion-receipt-sha256": plan["receipt_sha256"],
    }
    spec = document.get("spec")
    if not isinstance(spec, dict):
        raise StagingCellError("canonical staging migration template has no Job spec")
    spec["ttlSecondsAfterFinished"] = 3600
    pod_template = spec.get("template")
    pod_metadata = pod_template.get("metadata") if isinstance(pod_template, dict) else None
    pod_spec = pod_template.get("spec") if isinstance(pod_template, dict) else None
    if not isinstance(pod_metadata, dict) or not isinstance(pod_spec, dict):
        raise StagingCellError("canonical staging migration template has no Pod template")
    labels = pod_metadata.setdefault("labels", {})
    labels["app.kubernetes.io/name"] = "commonthing-api"
    labels["app.kubernetes.io/component"] = "database-migration"
    pod_spec["imagePullSecrets"] = [{"name": REGISTRY_SECRET}]
    # The migration pod deliberately shares the API network identity so the
    # bootstrap-pinned data NetworkPolicy admits PostgreSQL. This readiness
    # gate keeps the one-shot pod out of the API Service while it is running.
    pod_spec["readinessGates"] = [
        {"conditionType": "commonthing.net/migration-not-service"}
    ]
    containers = pod_spec.get("containers")
    if not isinstance(containers, list) or len(containers) != 1 or not isinstance(containers[0], dict):
        raise StagingCellError("canonical staging migration template must have one container")
    container = containers[0]
    container["image"] = plan["api_image"]
    container["imagePullPolicy"] = "IfNotPresent"
    env = container.get("env")
    if not isinstance(env, list):
        raise StagingCellError("canonical staging migration template has no environment")
    environment = {
        item.get("name"): item
        for item in env
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    database_url = environment.get("DATABASE_URL")
    if not isinstance(database_url, dict):
        raise StagingCellError("canonical staging migration template has no DATABASE_URL")
    database_url.pop("value", None)
    database_url["valueFrom"] = {
        "secretKeyRef": {"name": RUNTIME_SECRET, "key": "database-url"}
    }
    for name, value in (
        ("WELTGEWEBE_API_MIGRATION_ONLY", "1"),
        ("WELTGEWEBE_API_STARTUP_MIGRATIONS", "run"),
    ):
        item = environment.get(name)
        if not isinstance(item, dict):
            raise StagingCellError(f"canonical staging migration template has no {name}")
        item.pop("valueFrom", None)
        item["value"] = value
    metadata["annotations"][MIGRATION_SPEC_ANNOTATION] = migration_job_spec_sha256(
        document
    )
    return document


def run_staging_migration(
    kubectl: str, commit: str, promotion: dict[str, Any]
) -> dict[str, Any]:
    plan = migration_plan(commit, promotion)
    document = migration_job_document(commit, promotion)
    existing = read_staging_migration_job(kubectl, plan["job_name"])
    if existing is not None:
        existing_state = require_staging_migration_job_matches(existing, document, plan)
        if existing_state == "complete":
            return {**plan, "complete": True}
        if existing_state == "failed":
            run(
                [
                    kubectl,
                    "-n",
                    APP_NAMESPACE,
                    "delete",
                    "job",
                    plan["job_name"],
                    "--cascade=foreground",
                    "--wait=true",
                    "--timeout=60s",
                ],
                timeout=75,
            )
            if read_staging_migration_job(kubectl, plan["job_name"]) is not None:
                raise StagingCellError(
                    "failed staging migration Job still exists after bounded deletion"
                )
            existing = None

    if existing is None:
        apply_yaml_server_side(
            kubectl,
            document,
            field_manager="commonthing-staging-migration",
        )

    run(
        [
            kubectl,
            "-n",
            APP_NAMESPACE,
            "wait",
            "--for=condition=Complete",
            f"job/{plan['job_name']}",
            f"--timeout={int(MIGRATION_TIMEOUT_SECONDS)}s",
        ],
        timeout=int(MIGRATION_TIMEOUT_SECONDS + 30),
    )
    observed = read_staging_migration_job(kubectl, plan["job_name"])
    if observed is None:
        raise StagingCellError("staging migration Job disappeared before final readback")
    if require_staging_migration_job_matches(observed, document, plan) != "complete":
        raise StagingCellError(
            "staging migration Job readback does not match the promoted release"
        )
    return {**plan, "complete": True}


def app_source_document(commit: str) -> dict[str, Any]:
    if len(commit) != 40 or any(ch not in "0123456789abcdef" for ch in commit):
        raise StagingCellError("app source commit must be canonical 40-hex")
    return {
        "apiVersion": "source.toolkit.fluxcd.io/v1",
        "kind": "GitRepository",
        "metadata": {"name": APP_SOURCE_NAME, "namespace": "flux-system"},
        "spec": {
            "interval": "1m",
            "url": PUBLIC_REPOSITORY,
            "ref": {"commit": commit},
        },
    }


def app_kustomization_document(commit: str, promotion: dict[str, Any]) -> dict[str, Any]:
    images = promotion.get("images") if isinstance(promotion, dict) else None
    if not isinstance(images, dict):
        raise StagingCellError("promotion evidence has no verified images")
    verified_images: dict[str, str] = {}
    for label in ("api", "web"):
        image = images.get(label)
        if not isinstance(image, str) or "@sha256:" not in image:
            raise StagingCellError(f"verified {label} image is not digest-bound")
        verified_images[label] = image

    # commonthing-naming: legacy
    # The shared base still carries production-compatible Weltgewebe object names.
    # Flux applies this staging-only transform after rendering, so the live staging
    # runtime is commonthing-* without mutating production object identities.
    patches: list[dict[str, Any]] = []

    def json_patch(kind: str, name: str, operations: list[dict[str, Any]]) -> None:
        patches.append(
            {
                "target": {"kind": kind, "name": name},
                "patch": yaml.safe_dump(operations, sort_keys=False),
            }
        )

    json_patch(
        "Deployment",
        "weltgewebe-api",
        [
            {"op": "replace", "path": "/metadata/name", "value": "commonthing-api"},
            {"op": "replace", "path": "/metadata/labels/app.kubernetes.io~1name", "value": "commonthing-api"},
            {"op": "replace", "path": "/spec/selector/matchLabels/app.kubernetes.io~1name", "value": "commonthing-api"},
            {"op": "replace", "path": "/spec/template/metadata/labels/app.kubernetes.io~1name", "value": "commonthing-api"},
            {"op": "replace", "path": "/spec/template/spec/serviceAccountName", "value": "commonthing-api"},
            {"op": "replace", "path": "/spec/template/spec/topologySpreadConstraints/0/labelSelector/matchLabels/app.kubernetes.io~1name", "value": "commonthing-api"},
            {"op": "replace", "path": "/spec/template/spec/containers/0/image", "value": verified_images["api"]},
            {"op": "replace", "path": "/spec/template/spec/containers/0/envFrom/0/configMapRef/name", "value": RUNTIME_SECRET},
            {"op": "replace", "path": "/spec/template/spec/containers/0/env/0/valueFrom/secretKeyRef/name", "value": RUNTIME_SECRET},
            {"op": "replace", "path": "/spec/template/spec/containers/0/env/1/valueFrom/secretKeyRef/name", "value": RUNTIME_SECRET},
            {"op": "add", "path": "/spec/template/spec/imagePullSecrets", "value": [{"name": REGISTRY_SECRET}]},
        ],
    )
    json_patch(
        "Deployment",
        "weltgewebe-web",
        [
            {"op": "replace", "path": "/metadata/name", "value": "commonthing-web"},
            {"op": "replace", "path": "/metadata/labels/app.kubernetes.io~1name", "value": "commonthing-web"},
            {"op": "replace", "path": "/spec/selector/matchLabels/app.kubernetes.io~1name", "value": "commonthing-web"},
            {"op": "replace", "path": "/spec/template/metadata/labels/app.kubernetes.io~1name", "value": "commonthing-web"},
            {"op": "replace", "path": "/spec/template/spec/serviceAccountName", "value": "commonthing-web"},
            {"op": "replace", "path": "/spec/template/spec/topologySpreadConstraints/0/labelSelector/matchLabels/app.kubernetes.io~1name", "value": "commonthing-web"},
            {"op": "replace", "path": "/spec/template/spec/containers/0/image", "value": verified_images["web"]},
            {"op": "add", "path": "/spec/template/spec/imagePullSecrets", "value": [{"name": REGISTRY_SECRET}]},
        ],
    )
    for kind in ("Service", "PodDisruptionBudget"):
        for old_name, new_name in (
            ("weltgewebe-api", "commonthing-api"),
            ("weltgewebe-web", "commonthing-web"),
        ):
            selector_path = (
                "/spec/selector/app.kubernetes.io~1name"
                if kind == "Service"
                else "/spec/selector/matchLabels/app.kubernetes.io~1name"
            )
            operations = [
                {"op": "replace", "path": "/metadata/name", "value": new_name},
                {"op": "replace", "path": selector_path, "value": new_name},
            ]
            if kind == "Service":
                operations.insert(
                    1,
                    {"op": "replace", "path": "/metadata/labels/app.kubernetes.io~1name", "value": new_name},
                )
            json_patch(kind, old_name, operations)
    for old_name, new_name in (
        ("weltgewebe-api", "commonthing-api"),
        ("weltgewebe-web", "commonthing-web"),
    ):
        json_patch(
            "ServiceAccount",
            old_name,
            [
                {"op": "replace", "path": "/metadata/name", "value": new_name},
                {"op": "replace", "path": "/metadata/labels/app.kubernetes.io~1name", "value": new_name},
            ],
        )
    json_patch(
        "ConfigMap",
        "weltgewebe-runtime",
        [
            {"op": "replace", "path": "/metadata/name", "value": RUNTIME_SECRET},
            {"op": "replace", "path": "/metadata/labels/app.kubernetes.io~1name", "value": "commonthing-api"},
            {"op": "replace", "path": "/data/NATS_URL", "value": "nats://nats.commonthing-data.svc.cluster.local:4222"},
            {"op": "replace", "path": "/data/OTEL_SERVICE_NAME", "value": "commonthing-api"},
        ],
    )
    json_patch(
        "NetworkPolicy",
        "allow-api-data-egress",
        [
            {"op": "replace", "path": "/spec/podSelector/matchLabels/app.kubernetes.io~1name", "value": "commonthing-api"},
            {"op": "replace", "path": "/spec/egress/0/to/0/namespaceSelector/matchLabels/kubernetes.io~1metadata.name", "value": DATA_NAMESPACE},
        ],
    )
    json_patch(
        "CiliumNetworkPolicy",
        "allow-cilium-gateway",
        [
            {"op": "replace", "path": "/spec/endpointSelector/matchExpressions/0/values", "value": ["commonthing-api", "commonthing-web"]},
        ],
    )
    return {
        "apiVersion": "kustomize.toolkit.fluxcd.io/v1",
        "kind": "Kustomization",
        "metadata": {"name": APP_KUSTOMIZATION, "namespace": "flux-system"},
        "spec": {
            "interval": "2m",
            "retryInterval": "20s",
            "timeout": "8m",
            "prune": True,
            "wait": True,
            "dependsOn": [{"name": DATA_KUSTOMIZATION}],
            "sourceRef": {"kind": "GitRepository", "name": APP_SOURCE_NAME},
            # Source-layout compatibility only: the shared app tree still serves
            # production. Effective Staging object identity is rewritten below to
            # commonthing-* without changing production manifests.
            "path": "./platform/apps/weltgewebe/overlays/staging",
            "patches": patches,
            "healthChecks": [
                {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "name": deployment,
                    "namespace": APP_NAMESPACE,
                }
                for deployment in ("commonthing-api", "commonthing-web")
            ],
        },
    }


def reconcile_app(kubectl: str, commit: str) -> str:
    requested_at = f"staging-app-{time.time_ns()}"
    request_flux_reconcile(kubectl, "gitrepository", APP_SOURCE_NAME, requested_at)
    wait_flux_resource_current(
        kubectl,
        "gitrepository",
        APP_SOURCE_NAME,
        commit,
        requested_at=requested_at,
    )
    request_flux_reconcile(kubectl, "kustomization", APP_KUSTOMIZATION, requested_at)
    wait_flux_resource_current(
        kubectl,
        "kustomization",
        APP_KUSTOMIZATION,
        commit,
        requested_at=requested_at,
    )
    return requested_at


def app_live_health(kubectl: str) -> dict[str, str]:
    return {
        label: deployment_ready_state(kubectl, namespace, name)
        for label, (namespace, name) in APP_DEPLOYMENTS.items()
    }


def app_image_references(kubectl: str) -> dict[str, str]:
    return {
        label: output([
            kubectl,
            "-n",
            namespace,
            "get",
            "deployment",
            name,
            "-o",
            "jsonpath={.spec.template.spec.containers[0].image}",
        ])
        for label, (namespace, name) in APP_DEPLOYMENTS.items()
    }


def write_cell_receipt(root: Path, payload: dict[str, Any]) -> str:
    path = root / "receipts/cell-bootstrap.json"
    atomic_json(path, payload, mode=0o600)
    return str(path)


@lifecycle_mutation_locked
@reference_output_routed
def command_up(args: argparse.Namespace) -> dict[str, Any]:
    require_singleton_cluster(args.cluster)
    owner_id = args.owner_id
    if not owner_id:
        raise StagingCellError("--owner-id is required for real staging ownership")
    reference.validate_owner_id(owner_id)
    root = state_root(getattr(args, "state_root", None))
    ensure_directory_durable(root, mode=0o700)
    os.chmod(root, 0o700)
    configure_reference_paths(root)
    ensure_directory_durable(root / "clusters", mode=0o700)
    receipt = load_tool_receipt(root)
    data_root = root / "data"
    ensure_directory_durable(data_root, mode=0o755)
    os.chmod(data_root, 0o755)
    ensure_directory_durable(data_root / "postgres", mode=0o700)
    ensure_directory_durable(data_root / "nats", mode=0o700)
    tools = receipt["tools"]
    kind = tools["kind"]
    kubectl = tools["kubectl"]
    flux = tools["flux"]
    helm = tools["helm"]

    existing = args.cluster in reference.clusters(kind)
    cell_path = root / "receipts/cell-bootstrap.json"
    cell = load_cell_receipt(root) if cell_path.exists() else None
    legacy_migration: dict[str, Any] | None = None
    if existing and cell is None:
        raise StagingCellError(
            "staging cluster exists without a bootstrap receipt; refusing unbound recovery"
        )
    if cell is None and retained_staging_data_exists(root):
        legacy_migration = load_legacy_state_migration(root, owner_id=owner_id)
    if cell is not None:
        require_receipt_cluster(cell, args.cluster)
        persisted_owner = str(cell.get("owner_id") or "")
        if owner_id != persisted_owner:
            raise StagingCellError("--owner-id does not match the persisted cluster owner")
        if (
            cell.get("app_activation") is True
            or cell.get("status") == "app-activation-in-progress"
            or bool(cell.get("active_commit"))
            or bool(cell.get("pending_active_commit"))
        ):
            raise StagingCellError(
                "up cannot rewrite an activated or activating staging cell; "
                "use activate or an explicit recovery path"
            )

    _, source_sha = load_or_create_secret_material(root)

    if cell is not None:
        persisted_commit = str(cell.get("bootstrap_commit") or "")
        commit = require_clean_commit(
            args.source_commit,
            expected_commit=persisted_commit,
            require_public_main=False,
        )
    else:
        commit = require_clean_commit(args.source_commit)

    if existing:
        reference.require_owned_cluster(
            kind,
            args.cluster,
            expected_commit=commit,
            expected_owner_id=owner_id,
        )
        created = False
    else:
        if cell is None:
            bootstrap_payload: dict[str, Any] = {
                "schema_version": 1,
                "status": "bootstrap-in-progress",
                "cluster": args.cluster,
                "owner_id": owner_id,
                "bootstrap_commit": commit,
                "public_source": PUBLIC_REPOSITORY,
                "external_secret": {
                    "source_sha256": source_sha,
                    "required_keys": ["database-url"],
                },
                "production_changed": False,
            }
            if legacy_migration is not None:
                bootstrap_payload["legacy_state_migration"] = {
                    "receipt_sha256": sha256_file(_legacy_migration_receipt_path(root)),
                    "source_cluster": LEGACY_CLUSTER,
                    "legacy_bootstrap_commit": legacy_migration["legacy_bootstrap_commit"],
                }
            write_cell_receipt(root, bootstrap_payload)
        rendered_kind_config = render_kind_config(root)
        reference.create_kind_cluster(
            kind,
            args.cluster,
            receipt["kubernetes"]["kind_node_image"],
            str(rendered_kind_config),
            commit,
            owner_id,
            timeout=900,
        )
        created = True

    prepare_volume_permissions(kind, args.cluster, root)
    api_server_host = reference.control_plane_address(args.cluster)
    reference.install_platform_components(
        kubectl, flux, helm, receipt["artifacts"], api_server_host
    )
    run(
        [
            kubectl,
            "wait",
            "--for=condition=Ready",
            "nodes",
            "--all",
            "--timeout=5m",
        ],
        timeout=360,
    )
    secret_receipt = inject_external_secrets(kubectl, root)
    apply_yaml(kubectl, flux_documents(commit))
    reconcile_data(kubectl, commit)
    live_workloads = staging_live_health(kubectl)
    unhealthy_workloads = {
        name: state for name, state in live_workloads.items() if state != "True"
    }
    if unhealthy_workloads:
        raise StagingCellError(
            f"staging workloads are not live after reconciliation: {unhealthy_workloads!r}"
        )
    node_names = output([kubectl, "get", "nodes", "-o", "name"]).splitlines()
    if len(node_names) != 3:
        raise StagingCellError(
            f"staging Kubernetes node count drift: expected 3, observed {len(node_names)}"
        )
    node_count = len(node_names)
    base_result = {
        "schema_version": 1,
        "status": "infrastructure-ready-image-promotion-blocked",
        "cluster": args.cluster,
        "owner_id": owner_id,
        "bootstrap_commit": commit,
        "public_source": PUBLIC_REPOSITORY,
        "gitops_source_commit": commit,
        "cluster_created": created,
        "node_count": node_count,
        "toolchain_lock_sha256": receipt["lock_sha256"],
        "kubeconfig": str(reference.kubeconfig_path(args.cluster)),
        "persistent_storage": {
            "postgres": "Bound",
            "nats": "Bound",
            "host_state_root": str(root / "data"),
        },
        "flux": {
            "source": SOURCE_NAME,
            "data_kustomization": DATA_KUSTOMIZATION,
            "ready": True,
        },
        "live_workloads": live_workloads,
        "image_promotion": image_promotion_state(),
        "app_activation": False,
        "production_changed": False,
        "does_not_establish": [
            "first-party GHCR image promotion",
            "staging app rollout",
            "staging gateway proof",
            "delete-to-prove",
            "production Kubernetes cutover",
        ],
    }
    if legacy_migration is not None:
        base_result["legacy_state_migration"] = {
            "receipt_sha256": sha256_file(_legacy_migration_receipt_path(root)),
            "source_cluster": LEGACY_CLUSTER,
            "legacy_bootstrap_commit": legacy_migration["legacy_bootstrap_commit"],
        }
    private_result = {**base_result, "external_secret": secret_receipt}
    receipt_path = write_cell_receipt(root, private_result)
    return {
        **base_result,
        "external_secret": public_external_secret_state(),
        "receipt_path": receipt_path,
    }


@lifecycle_mutation_locked
@reference_output_routed
def command_activate(args: argparse.Namespace) -> dict[str, Any]:
    require_singleton_cluster(args.cluster)
    reference.validate_owner_id(args.owner_id)
    root = state_root(getattr(args, "state_root", None))
    configure_reference_paths(root)
    requested_commit = str(args.source_commit or "")
    _require_no_pending_backup_down_before_activation(root, requested_commit)
    receipt = load_tool_receipt(
        root, required_tools=("kind", "kubectl"), required_artifacts=()
    )
    kind = receipt["tools"]["kind"]
    kubectl = receipt["tools"]["kubectl"]
    cell = load_cell_receipt(root)
    require_receipt_cluster(cell, args.cluster)
    bootstrap_commit = str(cell.get("bootstrap_commit") or "")
    owner_id = str(cell.get("owner_id") or "")
    if args.owner_id != owner_id:
        raise StagingCellError("--owner-id does not match the persisted cluster owner")
    reference.validate_ownership_binding(bootstrap_commit, owner_id)
    pending_commit = str(cell.get("pending_active_commit") or "")
    activation_in_progress = cell.get("status") == "app-activation-in-progress"
    if activation_in_progress:
        if (
            len(pending_commit) != 40
            or any(ch not in "0123456789abcdef" for ch in pending_commit)
        ):
            raise StagingCellError("activation recovery has no canonical pending app commit")
        if args.source_commit != pending_commit:
            raise StagingCellError(
                "activation recovery must resume the exact pending app commit"
            )
        promotion = load_promotion_receipt(root, pending_commit)
        migration_plan_value = require_pending_promotion_matches(
            cell, pending_commit, promotion
        )
        backup_controller = _backup_recovery_controller_commit(
            root, cell, pending_commit
        )
        backup_reactivation = _backup_recovery_activation_binding(
            root, cell, pending_commit, backup_controller
        )
        if backup_reactivation is not None:
            commit = pending_commit
        else:
            commit = require_clean_commit(
                args.source_commit,
                require_public_main=False,
            )
    else:
        backup_controller = _backup_recovery_controller_commit(
            root, cell, requested_commit
        )
        completed_backup_reactivation = _completed_backup_recovery_activation_result(
            root, cell, requested_commit, backup_controller
        )
        if completed_backup_reactivation is not None:
            return completed_backup_reactivation
        backup_reactivation = _backup_recovery_activation_binding(
            root, cell, requested_commit, backup_controller
        )
        if backup_reactivation is not None:
            commit = requested_commit
        else:
            commit = require_clean_commit(args.source_commit)
        promotion = load_promotion_receipt(root, commit)
        migration_plan_value = migration_plan(commit, promotion)

    delete_to_prove_recovery = _delete_to_prove_reactivation_binding(
        root, cell, commit, promotion
    )

    registry_material, registry_source_sha = load_registry_pull_material(root)
    pending_config_sha256 = sha256_bytes(
        registry_dockerconfig_json(registry_material).encode("utf-8")
    )
    if activation_in_progress:
        require_pending_registry_matches(
            cell,
            source_sha=registry_source_sha,
            config_sha256=pending_config_sha256,
        )
    registry_pull_access = verify_ghcr_pull_access(registry_material, promotion)

    # Establish the owned staging kubeconfig before the first kubectl preflight.
    # This is read-only with respect to the cluster and prevents an ambient
    # KUBECONFIG from making us inspect the wrong cluster.
    reference.require_owned_cluster(
        kind,
        args.cluster,
        expected_commit=bootstrap_commit,
        expected_owner_id=owner_id,
    )
    require_bootstrap_data_current(kubectl, bootstrap_commit)
    retire_gateway_before_activation(kubectl, root, cell, owner_id)

    if not activation_in_progress:
        pending_state = {
            **without_gateway_state(cell),
            "status": "app-activation-in-progress",
            "pending_active_commit": commit,
            "pending_image_promotion": {
                "source_commit": commit,
                "receipt_sha256": promotion["receipt_sha256"],
                "images": promotion["images"],
            },
            "pending_migration": migration_plan_value,
            "pending_registry_pull_secret": {
                "source_sha256": registry_source_sha,
                "config_sha256": pending_config_sha256,
                "secret_name": REGISTRY_SECRET,
                "registry": GHCR_REGISTRY,
            },
            **(
                {"pending_delete_to_prove_recovery": delete_to_prove_recovery}
                if delete_to_prove_recovery is not None
                else {}
            ),
            **(
                {"pending_backup_recovery_reactivation": backup_reactivation}
                if backup_reactivation is not None
                else {}
            ),
            "production_changed": False,
        }
        write_cell_receipt(root, pending_state)

    # Only discard the old proof after the durable activation state no longer
    # depends on it. An interruption before this point can safely retry retirement.
    discard_retired_gateway_receipt(root)

    reference.normalize_owned_cluster_repository(
        kind,
        args.cluster,
        expected_commit=bootstrap_commit,
        expected_owner_id=owner_id,
    )
    secret_receipt = inject_external_secrets(kubectl, root)
    registry_secret_receipt = inject_registry_pull_secret(
        kubectl,
        root,
        material=registry_material,
        source_sha=registry_source_sha,
    )
    if registry_secret_receipt.get("config_sha256") != pending_config_sha256:
        raise StagingCellError("staging registry Secret hash drifted after preflight")

    migration_network_policies = apply_migration_network_isolation(kubectl)
    migration_receipt = run_staging_migration(kubectl, commit, promotion)
    if migration_receipt != {**migration_plan_value, "complete": True}:
        raise StagingCellError("staging migration evidence drifted from pending release")

    apply_yaml(
        kubectl,
        [app_source_document(commit), app_kustomization_document(commit, promotion)],
    )
    reconcile_app(kubectl, commit)
    require_bootstrap_data_current(kubectl, bootstrap_commit)
    workloads = app_live_health(kubectl)
    unhealthy = {name: state for name, state in workloads.items() if state != "True"}
    if unhealthy:
        raise StagingCellError(f"staging app workloads are not live: {unhealthy!r}")
    references = app_image_references(kubectl)
    expected_images = promotion["images"]
    if references != expected_images:
        raise StagingCellError(
            "staging app deployment images differ from promotion receipt: "
            f"expected={expected_images!r} observed={references!r}"
        )
    registry_binding = verify_registry_pull_secret_binding(
        kubectl,
        expected_source_sha=registry_secret_receipt["source_sha256"],
        expected_config_sha256=registry_secret_receipt["config_sha256"],
    )
    if registry_binding.get("ready") is not True:
        raise StagingCellError("staging registry pull Secret binding is not ready")
    promotion_state = {
        "status": "pass",
        "source_commit": commit,
        "receipt_sha256": promotion["receipt_sha256"],
        "images": references,
    }
    terminal_cell = {
        key: value
        for key, value in without_gateway_state(cell).items()
        if key
        not in {
            "pending_active_commit",
            "pending_image_promotion",
            "pending_migration",
            "pending_registry_pull_secret",
            "pending_delete_to_prove_recovery",
            "pending_backup_recovery_reactivation",
        }
    }
    updated = {
        **terminal_cell,
        "status": "app-ready-gateway-pending",
        "active_commit": commit,
        "gitops_source_commit": commit,
        "data_source_commit": bootstrap_commit,
        "app_source_commit": commit,
        "external_secret": secret_receipt,
        "registry_pull_secret": registry_secret_receipt,
        "registry_pull_access": registry_pull_access,
        "migration": {
            **migration_receipt,
            "network_isolation": migration_network_policies,
        },
        "image_promotion": promotion_state,
        "app_activation": True,
        **(
            {"backup_recovery_reactivation_consumed": backup_reactivation}
            if backup_reactivation is not None
            else {}
        ),
        "app_workloads": workloads,
        "production_changed": False,
        "does_not_establish": [
            "staging gateway proof",
            "staging DNS/TLS proof",
            "delete-to-prove",
            "production Kubernetes cutover",
        ],
    }
    receipt_path = write_cell_receipt(root, updated)
    return {**updated, "receipt_path": receipt_path}


GATEWAY_NAME = "commonthing-staging"
GATEWAY_LIMITS = ["DNS", "TLS", "external-LB", "Delete-to-Prove", "production cutover"]
GATEWAY_OWNER_ANNOTATION = "commonthing.net/gateway-owner-id"
GATEWAY_ACTIVE_COMMIT_ANNOTATION = "commonthing.net/gateway-active-commit"
GATEWAY_MANIFEST_SHA256_ANNOTATION = "commonthing.net/gateway-manifest-sha256"
GATEWAY_BINDING_ANNOTATIONS = {
    "owner_id": GATEWAY_OWNER_ANNOTATION,
    "active_commit": GATEWAY_ACTIVE_COMMIT_ANNOTATION,
    "manifest_sha256": GATEWAY_MANIFEST_SHA256_ANNOTATION,
}
GATEWAY_SERVICE_LABEL = "gateway.networking.k8s.io/gateway-name"
GATEWAY_RESOURCES = (
    ("Gateway", APP_NAMESPACE, GATEWAY_NAME),
    ("HTTPRoute", APP_NAMESPACE, GATEWAY_NAME),
)


def without_gateway_state(cell: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in cell.items()
        if key != "gateway" and not key.startswith(("gateway_", "pending_gateway"))
    }


def gateway_receipt_service_identity(
    root: Path, cell: dict[str, Any]
) -> tuple[str, str] | None:
    binding = cell.get("gateway_proof")
    if not isinstance(binding, dict):
        return None
    path = root / "receipts/gateway-proof.json"
    if (
        path.is_symlink()
        or not path.exists()
        or stat.S_IMODE(path.stat().st_mode) != 0o600
        or binding.get("active_commit") != cell_active_commit(cell)
        or binding.get("receipt_sha256") != sha256_file(path)
    ):
        raise StagingCellError("staging gateway proof receipt is invalid during retirement")
    try:
        receipt = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise StagingCellError(
            "staging gateway proof receipt is unreadable during retirement"
        ) from error
    if (
        receipt.get("owner_id") != cell.get("owner_id")
        or receipt.get("active_commit") != cell_active_commit(cell)
    ):
        raise StagingCellError(
            "staging gateway proof receipt lost its owner/app binding during retirement"
        )
    service = receipt.get("service")
    if not isinstance(service, dict):
        raise StagingCellError("staging gateway proof receipt lacks Service identity")
    name = str(service.get("name") or "")
    uid = str(service.get("uid") or "")
    if not name or not uid:
        raise StagingCellError("staging gateway proof receipt has invalid Service identity")
    return name, uid


def gateway_service_owned_by(service: dict, gateway_uid: str) -> bool:
    return bool(gateway_uid) and any(
        owner.get("apiVersion") == "gateway.networking.k8s.io/v1"
        and owner.get("kind") == "Gateway"
        and owner.get("name") == GATEWAY_NAME
        and owner.get("uid") == gateway_uid
        for owner in service.get("metadata", {}).get("ownerReferences", [])
    )


def retire_gateway_before_activation(
    kubectl: str, root: Path, cell: dict[str, Any], owner_id: str
) -> None:
    active_commit = str(cell.get("active_commit") or "")
    observed: list[tuple[str, str, str]] = []
    gateway_uid = ""
    for kind, namespace, name in GATEWAY_RESOURCES:
        document = gateway_get(kubectl, kind, name, namespace)
        if not document:
            continue
        annotations = document.get("metadata", {}).get("annotations", {})
        if (
            len(active_commit) != 40
            or any(ch not in "0123456789abcdef" for ch in active_commit)
            or annotations.get(GATEWAY_OWNER_ANNOTATION) != owner_id
            or annotations.get(GATEWAY_ACTIVE_COMMIT_ANNOTATION) != active_commit
        ):
            raise StagingCellError(
                "refusing to retire a staging gateway resource without the current owner/app binding"
            )
        if kind == "Gateway":
            gateway_uid = str(document.get("metadata", {}).get("uid") or "")
            if not gateway_uid:
                raise StagingCellError("staging Gateway lacks UID during retirement")
        observed.append((kind, namespace, name))

    labeled_services = gateway_list(
        kubectl,
        "Service",
        APP_NAMESPACE,
        label_selector=f"{GATEWAY_SERVICE_LABEL}={GATEWAY_NAME}",
    )
    tracked_services = set()
    receipt_service = gateway_receipt_service_identity(root, cell)
    if receipt_service is not None:
        tracked_services.add(receipt_service)
    if gateway_uid:
        all_services = gateway_list(kubectl, "Service", APP_NAMESPACE)
        owned_services = [
            service
            for service in all_services
            if gateway_service_owned_by(service, gateway_uid)
        ]
        if any(
            not gateway_service_owned_by(service, gateway_uid)
            for service in labeled_services
        ):
            raise StagingCellError(
                "refusing app activation while a mislabeled staging gateway Service remains"
            )
        for service in owned_services:
            metadata = service.get("metadata", {})
            name = str(metadata.get("name") or "")
            uid = str(metadata.get("uid") or "")
            if not name or not uid:
                raise StagingCellError(
                    "staging gateway Service lacks stable name/UID identity"
                )
            tracked_services.add((name, uid))
    elif labeled_services and receipt_service is None:
        raise StagingCellError(
            "refusing app activation while an orphan staging gateway Service remains"
        )

    for kind, namespace, name in reversed(observed):
        run(
            [
                kubectl,
                "-n",
                namespace,
                "delete",
                kind,
                name,
                "--ignore-not-found=true",
                "--wait=true",
                "--timeout=120s",
            ]
        )

    if observed or tracked_services:
        deadline = time.monotonic() + 120
        while True:
            remaining = [
                gateway_get(kubectl, kind, name, namespace)
                for kind, namespace, name in GATEWAY_RESOURCES
            ]
            tracked_remaining = []
            for name, uid in tracked_services:
                service = gateway_get(kubectl, "Service", name, APP_NAMESPACE)
                if not service:
                    continue
                if service.get("metadata", {}).get("uid") != uid:
                    raise StagingCellError(
                        "staging gateway Service identity changed during retirement"
                    )
                tracked_remaining.append(service)
            labeled_remaining = gateway_list(
                kubectl,
                "Service",
                APP_NAMESPACE,
                label_selector=f"{GATEWAY_SERVICE_LABEL}={GATEWAY_NAME}",
            )
            if not any(remaining) and not tracked_remaining and not labeled_remaining:
                break
            if time.monotonic() >= deadline:
                raise StagingCellError(
                    "staging gateway did not retire before app activation"
                )
            time.sleep(2)


def discard_retired_gateway_receipt(root: Path) -> None:
    (root / "receipts/gateway-proof.json").unlink(missing_ok=True)


def gateway_get(kubectl: str, kind: str, name: str, namespace: str = "") -> dict:
    scope = ["-n", namespace] if namespace else []
    raw = output(
        [kubectl, *scope, "get", kind, name, "--ignore-not-found", "-o", "json"]
    )
    return json.loads(raw) if raw else {}


def gateway_list(
    kubectl: str,
    kind: str,
    namespace: str = "",
    *,
    label_selector: str | None = None,
) -> list[dict]:
    scope = ["-n", namespace] if namespace else []
    argv = [kubectl, *scope, "get", kind]
    if label_selector:
        argv.extend(["-l", label_selector])
    raw = output([*argv, "-o", "json"])
    payload = json.loads(raw) if raw else {"items": []}
    items = payload.get("items")
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise StagingCellError(f"staging {kind} inventory is malformed")
    return items


def current_condition(
    document: dict, condition: str, conditions: list | None = None
) -> bool:
    generation = document.get("metadata", {}).get("generation")
    return generation is not None and any(
        item.get("type") == condition
        and item.get("status") == "True"
        and item.get("observedGeneration") == generation
        for item in (
            conditions
            if conditions is not None
            else document.get("status", {}).get("conditions", [])
        )
    )


def staging_gateway_documents(kustomize: str) -> list[dict]:
    rendered = output(
        [kustomize, "build", str(ROOT / "platform/clusters/staging/gateway")]
    )
    documents = [doc for doc in yaml.safe_load_all(rendered) if doc]
    identities = [
        (
            doc.get("kind"),
            doc.get("metadata", {}).get("namespace", ""),
            doc.get("metadata", {}).get("name"),
        )
        for doc in documents
    ]
    if len(identities) != len(GATEWAY_RESOURCES) or set(identities) != set(
        GATEWAY_RESOURCES
    ):
        raise StagingCellError(
            "staging gateway render escaped its exact resource allowlist"
        )
    return documents


def gateway_annotation_binding(document: dict) -> dict[str, Any]:
    annotations = document.get("metadata", {}).get("annotations", {})
    return {
        key: annotations.get(annotation)
        for key, annotation in GATEWAY_BINDING_ANNOTATIONS.items()
    }


def gateway_annotations(binding: dict[str, Any]) -> dict[str, str]:
    if set(binding) != set(GATEWAY_BINDING_ANNOTATIONS) or any(
        not isinstance(binding.get(key), str) or not binding[key]
        for key in GATEWAY_BINDING_ANNOTATIONS
    ):
        raise StagingCellError("staging gateway binding fields are invalid")
    return {
        annotation: binding[key]
        for key, annotation in GATEWAY_BINDING_ANNOTATIONS.items()
    }


def gateway_routing_contract(document: dict) -> dict[str, Any]:
    kind = document.get("kind")
    if kind not in {"Gateway", "HTTPRoute"}:
        raise StagingCellError("unexpected resource in staging gateway routing contract")
    namespace = document.get("metadata", {}).get("namespace", APP_NAMESPACE)
    spec = document.get("spec", {})
    if kind == "Gateway":
        listeners = []
        for listener in spec.get("listeners", []):
            allowed = listener.get("allowedRoutes", {})
            namespaces = allowed.get("namespaces", {})
            listeners.append(
                {
                    "name": listener.get("name"),
                    "protocol": listener.get("protocol"),
                    "port": listener.get("port"),
                    "hostname": listener.get("hostname"),
                    "tls": copy.deepcopy(listener.get("tls")),
                    "allowedRoutes": {
                        "namespaces": {
                            "from": namespaces.get("from", "Same"),
                            "selector": copy.deepcopy(namespaces.get("selector")),
                        },
                        "kinds": (
                            [
                                {
                                    "group": route_kind.get(
                                        "group", "gateway.networking.k8s.io"
                                    ),
                                    "kind": route_kind.get("kind"),
                                }
                                for route_kind in allowed.get("kinds", [])
                            ]
                            if "kinds" in allowed
                            else None
                        ),
                    },
                }
            )
        return {
            "gatewayClassName": spec.get("gatewayClassName"),
            "addresses": copy.deepcopy(spec.get("addresses", [])),
            "listeners": listeners,
            "infrastructure": copy.deepcopy(spec.get("infrastructure")),
        }

    parent_refs = []
    for parent in spec.get("parentRefs", []):
        normalized = copy.deepcopy(parent)
        normalized.setdefault("group", "gateway.networking.k8s.io")
        normalized.setdefault("kind", "Gateway")
        normalized.setdefault("namespace", namespace)
        parent_refs.append(normalized)
    rules = []
    for rule in spec.get("rules", []):
        matches = copy.deepcopy(rule.get("matches", []))
        for match in matches:
            if "path" in match:
                match["path"].setdefault("type", "PathPrefix")
                match["path"].setdefault("value", "/")
        backends = []
        for backend in rule.get("backendRefs", []):
            normalized = copy.deepcopy(backend)
            normalized.setdefault("group", "")
            normalized.setdefault("kind", "Service")
            normalized.setdefault("namespace", namespace)
            normalized.setdefault("weight", 1)
            backends.append(normalized)
        rules.append(
            {
                "matches": matches,
                "filters": copy.deepcopy(rule.get("filters", [])),
                "backendRefs": backends,
                "timeouts": copy.deepcopy(rule.get("timeouts")),
            }
        )
    return {
        "hostnames": copy.deepcopy(spec.get("hostnames", [])),
        "parentRefs": parent_refs,
        "rules": rules,
    }


def gateway_resource_binding(document: dict) -> dict:
    metadata = document.get("metadata", {})
    if not metadata.get("uid") or not metadata.get("generation"):
        raise StagingCellError("gateway resource lacks UID/generation")
    return {
        "kind": document["kind"],
        "namespace": metadata.get("namespace", ""),
        "name": metadata["name"],
        "uid": metadata["uid"],
        "generation": metadata["generation"],
        "spec_sha256": sha256_bytes(
            json.dumps(document["spec"], sort_keys=True).encode()
        ),
        "routing_contract": gateway_routing_contract(document),
        "gateway_binding": gateway_annotation_binding(document),
    }


def require_gateway_observation_binding(observed: dict, binding: dict) -> None:
    resources = observed.get("resources", [])
    if not resources or any(
        resource.get("gateway_binding") != binding for resource in resources
    ):
        raise StagingCellError(
            "staging gateway resources lost their exact owner/app/manifest binding"
        )


def require_gateway_desired_contract(observed: dict, documents: list[dict]) -> None:
    desired = {
        (
            document.get("kind"),
            document.get("metadata", {}).get("namespace", ""),
            document.get("metadata", {}).get("name"),
        ): gateway_routing_contract(document)
        for document in documents
    }
    live = {
        (
            resource.get("kind"),
            resource.get("namespace", ""),
            resource.get("name"),
        ): resource.get("routing_contract")
        for resource in observed.get("resources", [])
    }
    if live != desired:
        raise StagingCellError(
            "staging gateway live routing contract differs from the rendered manifests"
        )


def route_targets_staging_gateway(route: dict) -> bool:
    namespace = route.get("metadata", {}).get("namespace", APP_NAMESPACE)
    return any(
        parent.get("group", "gateway.networking.k8s.io")
        == "gateway.networking.k8s.io"
        and parent.get("kind", "Gateway") == "Gateway"
        and parent.get("namespace", namespace) == APP_NAMESPACE
        and parent.get("name") == GATEWAY_NAME
        for parent in route.get("spec", {}).get("parentRefs", [])
    )


def staging_gateway_routes(kubectl: str) -> list[dict]:
    return [
        candidate
        for candidate in gateway_list(kubectl, "HTTPRoute", APP_NAMESPACE)
        if route_targets_staging_gateway(candidate)
    ]


def require_no_shadow_staging_gateway_routes(kubectl: str) -> None:
    shadows = [
        route
        for route in staging_gateway_routes(kubectl)
        if route.get("metadata", {}).get("name") != GATEWAY_NAME
    ]
    if shadows:
        raise StagingCellError(
            "refusing to apply staging Gateway while another HTTPRoute targets it"
        )


def require_single_staging_gateway_route(kubectl: str, route: dict) -> None:
    attached = staging_gateway_routes(kubectl)
    if (
        len(attached) != 1
        or attached[0].get("metadata", {}).get("name") != GATEWAY_NAME
        or attached[0].get("metadata", {}).get("uid")
        != route.get("metadata", {}).get("uid")
    ):
        raise StagingCellError(
            "staging Gateway must have exactly its one owner-bound HTTPRoute"
        )


def gateway_ip_addresses(entries: list, key: str, *, gateway: bool = False) -> list[str]:
    addresses = set()
    for entry in entries:
        value = entry.get(key)
        if (gateway and entry.get("type", "IPAddress") != "IPAddress") or not value:
            raise StagingCellError("staging gateway requires IP addresses only")
        try:
            addresses.add(str(ipaddress.ip_address(value)))
        except ValueError as exc:
            raise StagingCellError("staging gateway has an invalid IP address") from exc
    if not addresses:
        raise StagingCellError("staging gateway has no current IP addresses")
    return sorted(addresses)


def staging_gateway_observation(kubectl: str) -> dict:
    documents = [
        gateway_get(kubectl, kind, name, ns) for kind, ns, name in GATEWAY_RESOURCES
    ]
    gateway, route = documents
    if not current_condition(gateway, "Programmed"):
        raise StagingCellError("staging Gateway is not currently Programmed")
    parents = [
        parent
        for parent in route.get("status", {}).get("parents", [])
        if parent.get("controllerName") == "io.cilium/gateway-controller"
        and parent.get("parentRef", {}).get("name") == GATEWAY_NAME
        and parent.get("parentRef", {}).get("namespace", APP_NAMESPACE) == APP_NAMESPACE
        and parent.get("parentRef", {}).get("sectionName") == "http"
    ]
    if not any(
        all(
            current_condition(route, condition, parent.get("conditions", []))
            for condition in ("Accepted", "ResolvedRefs")
        )
        for parent in parents
    ):
        raise StagingCellError(
            "staging HTTPRoute lacks current Accepted/ResolvedRefs for its Cilium listener"
        )
    require_single_staging_gateway_route(kubectl, route)
    gateway_addresses = gateway_ip_addresses(
        gateway.get("status", {}).get("addresses", []), "value", gateway=True
    )
    services = gateway_list(
        kubectl,
        "Service",
        APP_NAMESPACE,
        label_selector=f"{GATEWAY_SERVICE_LABEL}={GATEWAY_NAME}",
    )
    if len(services) != 1:
        raise StagingCellError(
            "staging Gateway requires exactly one Cilium LoadBalancer Service"
        )
    service = services[0]
    if (
        service.get("metadata", {}).get("labels", {}).get(GATEWAY_SERVICE_LABEL)
        != GATEWAY_NAME
        or service.get("spec", {}).get("type") != "LoadBalancer"
        or not any(
            port.get("port") == 80 and port.get("protocol", "TCP") == "TCP"
            for port in service.get("spec", {}).get("ports", [])
        )
        or service.get("metadata", {}).get("namespace") != APP_NAMESPACE
        or not service.get("metadata", {}).get("uid")
        or not any(
            owner.get("apiVersion") == "gateway.networking.k8s.io/v1"
            and owner.get("kind") == "Gateway"
            and owner.get("name") == GATEWAY_NAME
            and owner.get("uid") == gateway.get("metadata", {}).get("uid")
            and owner.get("uid")
            for owner in service.get("metadata", {}).get("ownerReferences", [])
        )
    ):
        raise StagingCellError(
            "staging Cilium Service does not bind the current Gateway and HTTP listener"
        )
    service_addresses = gateway_ip_addresses(
        service.get("status", {}).get("loadBalancer", {}).get("ingress", []), "ip"
    )
    if gateway_addresses != service_addresses:
        raise StagingCellError("staging Gateway and Service IP addresses differ")
    http_ports = [
        port
        for port in service.get("spec", {}).get("ports", [])
        if isinstance(port, dict)
        and port.get("port") == 80
        and port.get("protocol", "TCP") == "TCP"
    ]
    if len(http_ports) != 1:
        raise StagingCellError("staging Gateway Service HTTP port identity is ambiguous")
    node_port = http_ports[0].get("nodePort")
    if not isinstance(node_port, int) or isinstance(node_port, bool):
        raise StagingCellError("staging Gateway Service lacks an integer NodePort")
    return {
        "resources": [gateway_resource_binding(doc) for doc in documents],
        "service": {
            "name": service["metadata"]["name"],
            "uid": service["metadata"]["uid"],
            "node_port": node_port,
            "spec_sha256": sha256_bytes(
                json.dumps(service["spec"], sort_keys=True).encode()
            ),
        },
        "gateway_addresses": gateway_addresses,
        "service_addresses": service_addresses,
        "listener_port": 80,
    }


def gateway_service_node_port(kubectl: str, *, require_exact: bool = False) -> tuple[str, str, int]:
    services = gateway_list(
        kubectl,
        "Service",
        APP_NAMESPACE,
        label_selector=f"{GATEWAY_SERVICE_LABEL}={GATEWAY_NAME}",
    )
    if len(services) != 1:
        raise StagingCellError(
            "staging Gateway requires exactly one Cilium LoadBalancer Service"
        )
    service = services[0]
    metadata = service.get("metadata", {})
    name = str(metadata.get("name") or "")
    uid = str(metadata.get("uid") or "")
    ports = service.get("spec", {}).get("ports", [])
    matches = [
        (index, port)
        for index, port in enumerate(ports)
        if isinstance(port, dict)
        and port.get("port") == 80
        and port.get("protocol", "TCP") == "TCP"
    ]
    if not name or not uid or len(matches) != 1:
        raise StagingCellError("staging Gateway Service HTTP port identity is ambiguous")
    index, port = matches[0]
    node_port = port.get("nodePort")
    if not isinstance(node_port, int) or isinstance(node_port, bool):
        raise StagingCellError("staging Gateway Service lacks an integer NodePort")
    if require_exact and node_port != STAGING_GATEWAY_NODE_PORT:
        raise StagingCellError(
            f"staging Gateway NodePort drift: expected {STAGING_GATEWAY_NODE_PORT}, observed {node_port}"
        )
    return name, uid, node_port


def ensure_gateway_node_port(kubectl: str) -> tuple[str, str, int]:
    services = gateway_list(
        kubectl,
        "Service",
        APP_NAMESPACE,
        label_selector=f"{GATEWAY_SERVICE_LABEL}={GATEWAY_NAME}",
    )
    if len(services) != 1:
        raise StagingCellError(
            "staging Gateway requires exactly one Cilium LoadBalancer Service"
        )
    service = services[0]
    ports = service.get("spec", {}).get("ports", [])
    matches = [
        (index, port)
        for index, port in enumerate(ports)
        if isinstance(port, dict)
        and port.get("port") == 80
        and port.get("protocol", "TCP") == "TCP"
    ]
    if len(matches) != 1:
        raise StagingCellError("staging Gateway Service HTTP port identity is ambiguous")
    index, port = matches[0]
    if port.get("nodePort") != STAGING_GATEWAY_NODE_PORT:
        name = str(service.get("metadata", {}).get("name") or "")
        if not name:
            raise StagingCellError("staging Gateway Service lacks a name")
        patch = [
            {
                "op": "replace" if "nodePort" in port else "add",
                "path": f"/spec/ports/{index}/nodePort",
                "value": STAGING_GATEWAY_NODE_PORT,
            }
        ]
        run(
            [
                kubectl,
                "-n",
                APP_NAMESPACE,
                "patch",
                "service",
                name,
                "--type=json",
                "-p",
                json.dumps(patch, separators=(",", ":")),
            ],
            timeout=60,
        )
    return gateway_service_node_port(kubectl, require_exact=True)


HOST_HTTP_PROOF_MAX_BYTES = 1024 * 1024
API_NODES_PROOF_PAGE_LIMIT = 10
API_NODES_PROOF_MAX_PAGES = 10000
API_NODES_HASH_SCOPE = "complete-node-set-canonical-json-v2"
API_NODES_DB_HTTP_CONSISTENCY = "postgres-share-lock-http-match-v1"


def _host_http_bytes(path: str) -> bytes:
    if not path.startswith("/") or "//" in path:
        raise StagingCellError("host Gateway probe path is invalid")
    url = f"http://127.0.0.1:{STAGING_GATEWAY_HOST_PORT}{path}"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status != 200:
                raise StagingCellError(
                    f"host Gateway readback returned HTTP {response.status} for {path}"
                )
            body = response.read(HOST_HTTP_PROOF_MAX_BYTES + 1)
            if len(body) > HOST_HTTP_PROOF_MAX_BYTES:
                raise StagingCellError(
                    f"host Gateway response exceeds {HOST_HTTP_PROOF_MAX_BYTES} bytes for {path}"
                )
            return body
    except (OSError, urllib.error.URLError, urllib.error.HTTPError) as error:
        raise StagingCellError(
            f"host Gateway readback failed for localhost:{STAGING_GATEWAY_HOST_PORT}{path}"
        ) from error


def _canonical_api_nodes_snapshot(items: list[Any], *, page_count: int) -> dict[str, Any]:
    if not isinstance(page_count, int) or isinstance(page_count, bool) or page_count < 1:
        raise StagingCellError("API node snapshot page count is invalid")
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            raise StagingCellError("API node snapshot contains a non-object item")
        node_id = item.get("id")
        if not isinstance(node_id, str) or not node_id or node_id in seen:
            raise StagingCellError("API node snapshot contains an invalid or duplicate id")
        seen.add(node_id)
        normalized.append(item)
    normalized.sort(key=lambda item: item["id"])
    canonical = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "api_nodes_sha256": sha256_bytes(canonical),
        "api_nodes_count": len(normalized),
        "api_nodes_pages": page_count,
        "api_nodes_hash_scope": API_NODES_HASH_SCOPE,
    }


def _complete_api_nodes_readback(fetch_bytes: Any) -> dict[str, Any]:
    items: list[Any] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    page_count = 0
    while True:
        query = {
            "pagination": "cursor",
            "limit": str(API_NODES_PROOF_PAGE_LIMIT),
        }
        if cursor is not None:
            query["cursor"] = cursor
        path = "/api/nodes?" + urllib.parse.urlencode(query)
        raw = fetch_bytes(path)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise StagingCellError(
                "Gateway /api/nodes cursor page is not valid JSON"
            ) from error
        if not isinstance(payload, dict):
            raise StagingCellError(
                "Gateway /api/nodes cursor page did not return an envelope"
            )
        page_items = payload.get("items")
        page = payload.get("page")
        if not isinstance(page_items, list) or not isinstance(page, dict):
            raise StagingCellError("Gateway /api/nodes cursor envelope is malformed")
        if page.get("limit") != API_NODES_PROOF_PAGE_LIMIT or not isinstance(
            page.get("has_more"), bool
        ):
            raise StagingCellError("Gateway /api/nodes cursor metadata is malformed")
        items.extend(page_items)
        page_count += 1
        if page_count > API_NODES_PROOF_MAX_PAGES:
            raise StagingCellError(
                "Gateway /api/nodes proof exceeded the bounded page snapshot limit"
            )
        has_more = page["has_more"]
        next_cursor = page.get("next_cursor")
        if not has_more:
            if next_cursor is not None:
                raise StagingCellError(
                    "Gateway /api/nodes terminal cursor page unexpectedly has a next cursor"
                )
            break
        if not isinstance(next_cursor, str) or not next_cursor:
            raise StagingCellError(
                "Gateway /api/nodes non-terminal page lacks a next cursor"
            )
        if next_cursor in seen_cursors:
            raise StagingCellError("Gateway /api/nodes cursor loop detected")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    return _canonical_api_nodes_snapshot(items, page_count=page_count)


def _rfc3339_postgres_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise StagingCellError("PostgreSQL node snapshot contains an invalid timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise StagingCellError("PostgreSQL node snapshot timestamp is malformed") from error
    if parsed.tzinfo is None:
        raise StagingCellError("PostgreSQL node snapshot timestamp lacks a timezone")
    utc = parsed.astimezone(dt.timezone.utc)
    if utc.microsecond == 0:
        timespec = "seconds"
    elif utc.microsecond % 1000 == 0:
        timespec = "milliseconds"
    else:
        timespec = "microseconds"
    # Chrono DateTime<Utc>::to_rfc3339() uses the shortest exact subsecond
    # width for PostgreSQL's microsecond precision: .123, .123400, or none.
    return utc.isoformat(timespec=timespec)


def _api_node_from_postgres_snapshot_row(row: Any) -> dict[str, Any] | None:
    if not isinstance(row, list) or len(row) != 9:
        raise StagingCellError("PostgreSQL node snapshot row is malformed")
    node_id, kind, title, lat, lon, created_raw, updated_raw, payload_raw, visibility = row
    if not all(isinstance(value, str) for value in (node_id, kind, title)) or not node_id:
        raise StagingCellError("PostgreSQL node snapshot lost required node strings")
    if lat is None or lon is None:
        # Mirrors load_nodes_from_postgres: invalid NULL-location rows are not
        # part of the API projection and therefore not part of the semantic proof.
        return None
    if (
        not isinstance(lat, (int, float))
        or isinstance(lat, bool)
        or not isinstance(lon, (int, float))
        or isinstance(lon, bool)
    ):
        raise StagingCellError("PostgreSQL node snapshot has invalid coordinates")
    if visibility not in {"public", "private", "hidden", "revoked"}:
        raise StagingCellError("PostgreSQL node snapshot has invalid search visibility")
    payload = payload_raw if isinstance(payload_raw, dict) else {}
    created = _rfc3339_postgres_timestamp(created_raw)
    updated = _rfc3339_postgres_timestamp(updated_raw)
    default_timestamp = "1970-01-01T00:00:00Z"
    node: dict[str, Any] = {
        "id": node_id,
        "kind": kind,
        "title": title,
        "created_at": created or updated or default_timestamp,
        "updated_at": updated or created or default_timestamp,
        "search_visibility": visibility,
        # PostgreSQL json_build_array may encode integral DOUBLE PRECISION values
        # as JSON integers, while the Rust API serializes the same f64 as 10.0.
        # Normalize the projection to API numeric semantics before hashing.
        "location": {"lat": float(lat), "lon": float(lon)},
    }
    creator = payload.get("created_by_account_id")
    if isinstance(creator, str) and creator.strip():
        node["created_by_account_id"] = creator.strip()
    for key in ("summary", "info", "address"):
        value = payload.get(key)
        if isinstance(value, str):
            node[key] = value
    tags = payload.get("tags")
    if isinstance(tags, list):
        filtered = [tag for tag in tags if isinstance(tag, str)]
        if filtered:
            node["tags"] = filtered
    return node


def postgres_api_nodes_complete_readback(kubectl: str) -> dict[str, Any]:
    sql = (
        "SELECT json_build_array(id,kind,title,lat,lon,created_at,updated_at,payload,"
        "search_visibility)::text FROM domain_nodes ORDER BY id ASC"
    )
    raw = output(
        [
            kubectl,
            "-n",
            DATA_NAMESPACE,
            "exec",
            "deployment/postgres",
            "-c",
            "postgres",
            "--",
            "sh",
            "-eu",
            "-c",
            'exec psql -XAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"',
            "sh",
            sql,
        ],
        timeout=120,
    )
    items: list[dict[str, Any]] = []
    if raw:
        for line in raw.splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise StagingCellError(
                    "PostgreSQL node snapshot emitted malformed JSON"
                ) from error
            node = _api_node_from_postgres_snapshot_row(row)
            if node is not None:
                items.append(node)
    pages = max(1, (len(items) + API_NODES_PROOF_PAGE_LIMIT - 1) // API_NODES_PROOF_PAGE_LIMIT)
    result = _canonical_api_nodes_snapshot(items, page_count=pages)
    return {**result, "api_nodes_source": "quiesced-postgres-api-projection-v1"}


def _bind_locked_api_nodes_http_to_postgres(
    kubectl: str, http_snapshot: dict[str, Any], *, label: str
) -> dict[str, Any]:
    database = postgres_api_nodes_complete_readback(kubectl)
    expected_source = "quiesced-postgres-api-projection-v1"
    if database.get("api_nodes_source") != expected_source:
        raise StagingCellError(f"{label} PostgreSQL projection source is invalid")
    for key in (
        "api_nodes_sha256",
        "api_nodes_count",
        "api_nodes_pages",
        "api_nodes_hash_scope",
    ):
        if http_snapshot.get(key) != database.get(key):
            raise StagingCellError(
                f"{label} differs from the locked PostgreSQL API projection: {key}"
            )
    return {
        "api_nodes_consistency": API_NODES_DB_HTTP_CONSISTENCY,
        "postgres_api_nodes_sha256": database["api_nodes_sha256"],
        "postgres_api_nodes_count": database["api_nodes_count"],
        "postgres_api_nodes_pages": database["api_nodes_pages"],
        "postgres_api_nodes_hash_scope": database["api_nodes_hash_scope"],
        "postgres_api_nodes_source": database["api_nodes_source"],
    }


def _kind_gateway_http_bytes(node: str, address: str, port: int, path: str) -> bytes:
    if not path.startswith("/") or "//" in path:
        raise StagingCellError("kind Gateway probe path is invalid")
    host = f"[{address}]" if ":" in address and not address.startswith("[") else address
    try:
        return subprocess.run(
            [
                "docker",
                "exec",
                node,
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--max-time",
                "10",
                f"http://{host}:{port}{path}",
            ],
            cwd=ROOT,
            text=False,
            capture_output=True,
            check=True,
            timeout=15,
        ).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise StagingCellError("kind Gateway API snapshot readback failed") from error


def _postgres_proof_scalar(kubectl: str, sql: str, *, timeout: float = 30) -> str:
    return output(
        [
            kubectl,
            "-n",
            DATA_NAMESPACE,
            "exec",
            "deployment/postgres",
            "-c",
            "postgres",
            "--",
            "sh",
            "-eu",
            "-c",
            'exec psql -XAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"',
            "sh",
            sql,
        ],
        timeout=timeout,
    )


def _postgres_domain_nodes_write_freeze_count(kubectl: str, application_name: str) -> int:
    sql = (
        "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid "
        "WHERE a.application_name='" + application_name + "' "
        "AND l.locktype='relation' AND l.relation='domain_nodes'::regclass "
        "AND l.mode='ShareLock' AND l.granted"
    )
    raw = _postgres_proof_scalar(kubectl, sql)
    try:
        count = int(raw)
    except ValueError as error:
        raise StagingCellError("PostgreSQL proof write-freeze lock state is invalid") from error
    if count not in {0, 1}:
        raise StagingCellError("PostgreSQL proof write-freeze lock state is ambiguous")
    return count


def _postgres_domain_nodes_write_freeze_session_count(
    kubectl: str, application_name: str
) -> int:
    raw = _postgres_proof_scalar(
        kubectl,
        "SELECT count(*) FROM pg_stat_activity WHERE application_name='"
        + application_name
        + "'",
    )
    try:
        count = int(raw)
    except ValueError as error:
        raise StagingCellError("PostgreSQL proof write-freeze session state is invalid") from error
    if count < 0 or count > 1:
        raise StagingCellError("PostgreSQL proof write-freeze session state is ambiguous")
    return count


@contextmanager
def _postgres_domain_nodes_write_freeze(kubectl: str):
    application_name = "commonthing-t084-proof-" + secrets.token_hex(8)
    command = [
        kubectl,
        "-n",
        DATA_NAMESPACE,
        "exec",
        "-i",
        "deployment/postgres",
        "-c",
        "postgres",
        "--",
        "sh",
        "-eu",
        "-c",
        'exec psql -XAtq -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"',
    ]
    print("+ external command [arguments redacted]", file=sys.stderr, flush=True)
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        text=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    acquired = False
    try:
        if process.stdin is None:
            raise StagingCellError("PostgreSQL proof write-freeze stdin is unavailable")
        process.stdin.write(
            "SET application_name='"
            + application_name
            + "'; BEGIN; SET LOCAL lock_timeout='10s'; "
            "LOCK TABLE domain_nodes IN SHARE MODE;\n"
        )
        process.stdin.flush()
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if process.poll() is not None:
                detail = process.stderr.read().strip() if process.stderr is not None else ""
                raise StagingCellError(
                    "PostgreSQL proof write-freeze exited before the lock was acquired"
                    + (f": {detail}" if detail else "")
                )
            if _postgres_domain_nodes_write_freeze_count(kubectl, application_name) == 1:
                acquired = True
                break
            time.sleep(0.1)
        if not acquired:
            raise StagingCellError("PostgreSQL proof write-freeze lock acquisition timed out")
        yield
        if (
            process.poll() is not None
            or _postgres_domain_nodes_write_freeze_count(kubectl, application_name) != 1
        ):
            raise StagingCellError(
                "PostgreSQL proof write-freeze was lost during the Gateway snapshot"
            )
    finally:
        if process.poll() is None and process.stdin is not None:
            try:
                process.stdin.write("ROLLBACK;\n\\q\n")
                process.stdin.flush()
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        try:
            remaining = _postgres_domain_nodes_write_freeze_session_count(
                kubectl, application_name
            )
        except (subprocess.CalledProcessError, StagingCellError):
            remaining = 1
        if remaining:
            try:
                _postgres_proof_scalar(
                    kubectl,
                    "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity "
                    "WHERE application_name='" + application_name + "'",
                )
            except subprocess.CalledProcessError as error:
                raise StagingCellError(
                    "PostgreSQL proof write-freeze session could not be terminated"
                ) from error
            if _postgres_domain_nodes_write_freeze_session_count(
                kubectl, application_name
            ) != 0:
                raise StagingCellError(
                    "PostgreSQL proof write-freeze session remained after cleanup"
                )


def gateway_api_nodes_complete_readback(
    kind: str, cluster: str, gateway_receipt: dict[str, Any]
) -> dict[str, Any]:
    node = str(gateway_receipt.get("probe_node") or "")
    address = str(gateway_receipt.get("address") or "")
    port = gateway_receipt.get("listener_port")
    if not node or node not in reference.kind_nodes(kind, cluster):
        raise StagingCellError("Gateway API snapshot lost its proven kind probe node")
    if not address or not isinstance(port, int) or isinstance(port, bool):
        raise StagingCellError("Gateway API snapshot lost its proven address or listener")
    return _complete_api_nodes_readback(
        lambda path: _kind_gateway_http_bytes(node, address, port, path)
    )


def host_gateway_http_readback() -> dict[str, Any]:
    health = _host_http_bytes("/health/live")
    web = _host_http_bytes("/")
    web_prefix = web[:1024]
    nodes = _complete_api_nodes_readback(_host_http_bytes)
    return {
        "probe_scope": "heim-pc-host-outside-kubernetes",
        "endpoint": f"http://127.0.0.1:{STAGING_GATEWAY_HOST_PORT}",
        "health_sha256": sha256_bytes(health),
        "web_prefix_sha256": sha256_bytes(web_prefix),
        "web_prefix_bytes": len(web_prefix),
        **nodes,
    }


def require_gateway_app_current(kubectl: str, cell: dict, promotion: dict) -> None:
    commit = cell_active_commit(cell)
    require_bootstrap_data_current(kubectl, cell["bootstrap_commit"])
    for kind, name in (
        ("gitrepository", APP_SOURCE_NAME),
        ("kustomization", APP_KUSTOMIZATION),
    ):
        state = flux_resource_current_state(kubectl, kind, name, commit)
        if state.get("ready") != "True" or state.get("matches_commit") is not True:
            raise StagingCellError(
                "gateway proof requires the exact active app Flux revision"
            )
    if (
        app_live_health(kubectl) != {name: "True" for name in APP_DEPLOYMENTS}
        or app_image_references(kubectl) != promotion["images"]
    ):
        raise StagingCellError("gateway proof requires healthy promoted app images")


@lifecycle_mutation_locked
@reference_output_routed
def command_prove_gateway(args: argparse.Namespace) -> dict[str, Any]:
    root = state_root(getattr(args, "state_root", None))
    configure_reference_paths(root)
    cell = load_cell_receipt(root)
    require_receipt_cluster(cell, args.cluster)
    if args.owner_id != cell.get("owner_id"):
        raise StagingCellError("--owner-id does not match the persisted cluster owner")
    commit = cell_active_commit(cell)
    if args.source_commit != commit:
        raise StagingCellError("gateway proof requires the exact active app commit")
    if (
        cell.get("app_activation") is not True
        or cell.get("pending_active_commit")
        or cell.get("status")
        not in {
            "app-ready-gateway-pending",
            "gateway-proof-in-progress",
            "gateway-ready",
        }
    ):
        raise StagingCellError("gateway proof requires completed app activation")
    backup_controller = _backup_recovery_controller_commit(root, cell, commit)
    if backup_controller is not None:
        implementation_commit = backup_controller
    else:
        implementation_commit = require_clean_commit(args.source_commit)
        if implementation_commit != commit:
            raise StagingCellError(
                "gateway implementation commit must equal the active app commit"
            )
    promotion = load_promotion_receipt(root, commit)
    recorded = cell.get("image_promotion", {})
    if (
        recorded.get("source_commit") != commit
        or recorded.get("images") != promotion["images"]
        or recorded.get("receipt_sha256") != promotion["receipt_sha256"]
    ):
        raise StagingCellError(
            "gateway proof promotion differs from the active app receipt"
        )
    tools = load_tool_receipt(
        root, required_tools=("kind", "kubectl", "kustomize"), required_artifacts=()
    )["tools"]
    kubectl = tools["kubectl"]
    reference.require_owned_cluster(
        tools["kind"],
        args.cluster,
        expected_commit=cell["bootstrap_commit"],
        expected_owner_id=args.owner_id,
    )
    require_gateway_app_current(kubectl, cell, promotion)
    gateway_class = gateway_get(kubectl, "GatewayClass", "cilium")
    if gateway_class.get("spec", {}).get(
        "controllerName"
    ) != "io.cilium/gateway-controller" or not current_condition(
        gateway_class, "Accepted"
    ):
        raise StagingCellError("Cilium GatewayClass is not currently Accepted")
    documents = staging_gateway_documents(tools["kustomize"])
    manifest_sha = sha256_bytes(json.dumps(documents, sort_keys=True).encode())
    binding = {
        "owner_id": args.owner_id,
        "active_commit": commit,
        "manifest_sha256": manifest_sha,
    }
    if (
        cell.get("status") == "gateway-proof-in-progress"
        and cell.get("pending_gateway") != binding
    ):
        raise StagingCellError(
            "gateway recovery requires the exact pending owner/commit/manifests"
        )
    annotations = gateway_annotations(binding)
    for document in documents:
        meta = document["metadata"]
        existing = gateway_get(
            kubectl, document["kind"], meta["name"], meta.get("namespace", "")
        )
        if existing:
            if cell.get("status") == "app-ready-gateway-pending":
                raise StagingCellError(
                    "fresh gateway proof refuses a pre-existing staging gateway resource"
                )
            if gateway_annotation_binding(existing) != binding:
                raise StagingCellError(
                    "refusing to adopt a staging gateway resource without its exact owner/app/manifest binding"
                )
        meta["annotations"] = {**meta.get("annotations", {}), **annotations}
    require_no_shadow_staging_gateway_routes(kubectl)
    rendered = yaml.safe_dump_all(documents, sort_keys=False, explicit_start=True)
    run(
        [
            kubectl,
            "apply",
            "--server-side",
            "--dry-run=server",
            "--field-manager=commonthing-staging-gateway",
            "-f",
            "-",
        ],
        input_text=rendered,
        timeout=120,
    )
    pending = {
        **cell,
        "status": "gateway-proof-in-progress",
        "pending_gateway": binding,
    }
    pending.pop("gateway_proof", None)
    write_cell_receipt(root, pending)
    run(
        [
            kubectl,
            "apply",
            "--server-side",
            "--field-manager=commonthing-staging-gateway",
            "-f",
            "-",
        ],
        input_text=rendered,
        timeout=120,
    )
    deadline = time.monotonic() + 120
    while True:
        try:
            # Pin the controller-created Service first. Only the post-pin
            # observation is proof-worthy; the prior shape is transient.
            ensure_gateway_node_port(kubectl)
            observed = staging_gateway_observation(kubectl)
            break
        except StagingCellError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(2)
    require_gateway_observation_binding(observed, binding)
    require_gateway_desired_contract(observed, documents)
    node, address, health, web, api_nodes = reference.probe_gateway_http(
        tools["kind"], args.cluster, observed["gateway_addresses"], observed["listener_port"]
    )
    if address not in observed["gateway_addresses"]:
        raise StagingCellError("HTTP proof selected an unobserved Gateway address")
    require_gateway_app_current(kubectl, cell, promotion)
    if staging_gateway_observation(kubectl) != observed:
        raise StagingCellError("gateway resources changed during HTTP proof")
    result = {
        "schema_version": 1,
        "status": "gateway-ready",
        "cluster": args.cluster,
        **binding,
        "bootstrap_commit": cell["bootstrap_commit"],
        "implementation_commit": implementation_commit,
        **observed,
        "probe_node": node,
        "address": address,
        "probe_network": "kind-node",
        "app_activation": True,
        "health_sha256": sha256_bytes(health),
        "web_prefix_sha256": sha256_bytes(web),
        "web_prefix_bytes": len(web),
        "api_nodes_sha256": sha256_bytes(api_nodes),
        "does_not_establish": GATEWAY_LIMITS,
        "production_changed": False,
    }
    path = root / "receipts/gateway-proof.json"
    atomic_json(path, result)
    updated = {
        **pending,
        "status": "gateway-ready",
        "does_not_establish": GATEWAY_LIMITS,
        "gateway_proof": {"active_commit": commit, "receipt_sha256": sha256_file(path)},
    }
    updated.pop("pending_gateway", None)
    write_cell_receipt(root, updated)
    return result


@lifecycle_mutation_locked
@reference_output_routed
def command_prove_host_gateway(args: argparse.Namespace) -> dict[str, Any]:
    require_singleton_cluster(args.cluster)
    reference.validate_owner_id(args.owner_id)
    root = state_root(getattr(args, "state_root", None))
    configure_reference_paths(root)
    cell = load_cell_receipt(root)
    require_receipt_cluster(cell, args.cluster)
    if args.owner_id != cell.get("owner_id"):
        raise StagingCellError("--owner-id does not match the persisted cluster owner")
    active_commit = cell_active_commit(cell)
    if args.source_commit != active_commit:
        raise StagingCellError("host Gateway proof requires the exact active app commit")
    tools = load_tool_receipt(
        root, required_tools=("kind", "kubectl"), required_artifacts=()
    )["tools"]
    kubectl = tools["kubectl"]
    reference.require_owned_cluster(
        tools["kind"],
        args.cluster,
        expected_commit=cell["bootstrap_commit"],
        expected_owner_id=args.owner_id,
    )
    if not gateway_receipt_current(root, cell, kubectl):
        raise StagingCellError("host Gateway proof requires the current gateway proof")
    promotion = _exact_cell_promotion(root, cell, active_commit)
    service_name, service_uid, node_port = gateway_service_node_port(
        kubectl, require_exact=True
    )
    if node_port != STAGING_GATEWAY_NODE_PORT:
        raise StagingCellError("host Gateway proof NodePort is not pinned")
    require_gateway_app_current(kubectl, cell, promotion)
    with _postgres_domain_nodes_write_freeze(kubectl):
        readback = host_gateway_http_readback()
        api_nodes_consistency = _bind_locked_api_nodes_http_to_postgres(
            kubectl, readback, label="host Gateway API snapshot"
        )
        require_gateway_app_current(kubectl, cell, promotion)
        if gateway_service_node_port(kubectl, require_exact=True) != (
            service_name,
            service_uid,
            node_port,
        ):
            raise StagingCellError("staging Gateway Service changed during host readback")
        if not gateway_receipt_current(root, cell, kubectl):
            raise StagingCellError("staging gateway changed during host readback")
        verified_at_unix = int(time.time())
    result = {
        "schema_version": 1,
        "status": "host-gateway-readback-verified",
        "cluster": args.cluster,
        "owner_id": args.owner_id,
        "bootstrap_commit": cell["bootstrap_commit"],
        "active_commit": active_commit,
        "gateway_receipt_sha256": sha256_file(root / "receipts/gateway-proof.json"),
        "service": {
            "name": service_name,
            "uid": service_uid,
            "node_port": node_port,
        },
        **readback,
        **api_nodes_consistency,
        "production_changed": False,
        "does_not_establish": ["public DNS", "public TLS", "production cutover"],
        "verified_at_unix": verified_at_unix,
    }
    path = root / HOST_GATEWAY_RECEIPT
    atomic_json(path, result)
    updated = {
        **cell,
        "host_gateway_proof": {
            "active_commit": active_commit,
            "receipt_sha256": sha256_file(path),
        },
    }
    write_cell_receipt(root, updated)
    return {**result, "receipt_path": str(path), "receipt_sha256": sha256_file(path)}


def host_gateway_receipt_current(root: Path, cell: dict[str, Any], kubectl: str) -> bool:
    try:
        path = root / HOST_GATEWAY_RECEIPT
        binding = cell.get("host_gateway_proof")
        if not isinstance(binding, dict) or path.is_symlink():
            return False
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            return False
        if binding.get("active_commit") != cell_active_commit(cell):
            return False
        if binding.get("receipt_sha256") != sha256_file(path):
            return False
        receipt = _private_json_receipt(path, label="host Gateway proof receipt")
        service_name, service_uid, node_port = gateway_service_node_port(
            kubectl, require_exact=True
        )
        return (
            receipt.get("status") == "host-gateway-readback-verified"
            and receipt.get("active_commit") == cell_active_commit(cell)
            and receipt.get("gateway_receipt_sha256")
            == sha256_file(root / "receipts/gateway-proof.json")
            and receipt.get("service")
            == {"name": service_name, "uid": service_uid, "node_port": node_port}
            and receipt.get("probe_scope") == "heim-pc-host-outside-kubernetes"
            and receipt.get("api_nodes_consistency") == API_NODES_DB_HTTP_CONSISTENCY
            and receipt.get("postgres_api_nodes_source")
            == "quiesced-postgres-api-projection-v1"
            and receipt.get("postgres_api_nodes_sha256")
            == receipt.get("api_nodes_sha256")
            and receipt.get("postgres_api_nodes_count")
            == receipt.get("api_nodes_count")
            and receipt.get("postgres_api_nodes_pages")
            == receipt.get("api_nodes_pages")
            and receipt.get("postgres_api_nodes_hash_scope")
            == receipt.get("api_nodes_hash_scope")
        )
    except (OSError, ValueError, StagingCellError, subprocess.CalledProcessError):
        return False


def gateway_receipt_current(root: Path, cell: dict, kubectl: str) -> bool:
    if cell.get("status") != "gateway-ready":
        return False
    try:
        path = root / "receipts/gateway-proof.json"
        if path.is_symlink() or stat.S_IMODE(path.stat().st_mode) != 0o600:
            return False
        binding = cell.get("gateway_proof", {})
        if binding.get("active_commit") != cell_active_commit(cell) or binding.get(
            "receipt_sha256"
        ) != sha256_file(path):
            return False
        receipt = json.loads(path.read_text())
        if any(
            receipt.get(key) != cell.get(key)
            for key in ("owner_id", "cluster", "bootstrap_commit", "active_commit")
        ):
            return False
        observed = staging_gateway_observation(kubectl)
        expected_binding = {
            "owner_id": receipt.get("owner_id"),
            "active_commit": receipt.get("active_commit"),
            "manifest_sha256": receipt.get("manifest_sha256"),
        }
        require_gateway_observation_binding(observed, expected_binding)
        implementation_commit = receipt.get("implementation_commit")
        implementation_valid = implementation_commit == cell_active_commit(cell)
        if not implementation_valid:
            rebuild_path = root / BACKUP_REBUILD_RECEIPT
            if rebuild_path.exists() and not rebuild_path.is_symlink():
                rebuild = _private_json_receipt(
                    rebuild_path, label="backup rebuild receipt"
                )
                implementation_valid = (
                    rebuild.get("status")
                    == "backup-restored-infrastructure-ready-app-reactivation-required"
                    and rebuild.get("release_commit") == cell_active_commit(cell)
                    and rebuild.get("controller_commit") == implementation_commit
                )
        return (
            implementation_valid
            and receipt.get("address") in observed["gateway_addresses"]
            and all(receipt.get(key) == value for key, value in observed.items())
        )
    except (OSError, ValueError, StagingCellError, subprocess.CalledProcessError):
        return False


@reference_output_routed
def command_status(args: argparse.Namespace) -> dict[str, Any]:
    require_singleton_cluster(args.cluster)
    root = state_root(getattr(args, "state_root", None))
    configure_reference_paths(root)
    owner_path = root / "receipts/cell-bootstrap.json"
    if not owner_path.exists():
        return {
            "schema_version": 1,
            "status": "not-bootstrapped",
            "cluster": args.cluster,
        }
    receipt = load_tool_receipt(
        root, required_tools=("kind", "kubectl"), required_artifacts=()
    )
    kind = receipt["tools"]["kind"]
    kubectl = receipt["tools"]["kubectl"]
    owner = load_cell_receipt(root)
    require_receipt_cluster(owner, args.cluster)
    bootstrap_commit = str(owner.get("bootstrap_commit") or "")
    active_commit = cell_active_commit(owner)
    owner_id = str(owner.get("owner_id") or "")
    if args.cluster not in reference.clusters(kind):
        return {
            "schema_version": 1,
            "status": "cluster-absent-state-preserved",
            "cluster": args.cluster,
            "owner_id": owner_id,
            "bootstrap_commit": bootstrap_commit,
            "active_commit": active_commit,
            "production_changed": False,
        }
    reference.require_owned_cluster(
        kind,
        args.cluster,
        expected_commit=bootstrap_commit,
        expected_owner_id=owner_id,
    )
    if owner.get("status") == "bootstrap-in-progress":
        return {
            "schema_version": 1,
            "status": "bootstrap-in-progress",
            "cluster": args.cluster,
            "owner_id": owner_id,
            "bootstrap_commit": bootstrap_commit,
            "active_commit": active_commit,
            "production_changed": False,
        }
    source_revision = output(
        [
            kubectl,
            "-n",
            "flux-system",
            "get",
            "gitrepository",
            SOURCE_NAME,
            "--ignore-not-found",
            "-o",
            "jsonpath={.status.artifact.revision}",
        ]
    ) or "missing"
    source_matches_commit = flux_revision_matches_commit(source_revision, bootstrap_commit)
    source_health_raw = output(
        [
            kubectl,
            "-n",
            "flux-system",
            "get",
            "gitrepository",
            SOURCE_NAME,
            "--ignore-not-found",
            "-o",
            "jsonpath={.metadata.generation}|{.status.observedGeneration}|{.status.conditions[?(@.type=='Ready')].status}",
        ]
    )
    source_health_parts = source_health_raw.split("|", 2) if source_health_raw else []
    if len(source_health_parts) != 3:
        source_ready = "missing"
    else:
        source_generation, source_observed_generation, source_ready_status = source_health_parts
        if (
            not source_generation
            or not source_observed_generation
            or source_generation != source_observed_generation
        ):
            source_ready = "stale"
        else:
            source_ready = source_ready_status or "missing"
    data_health_raw = output(
        [
            kubectl,
            "-n",
            "flux-system",
            "get",
            "kustomization",
            DATA_KUSTOMIZATION,
            "--ignore-not-found",
            "-o",
            "jsonpath={.metadata.generation}|{.status.observedGeneration}|{.status.conditions[?(@.type=='Ready')].status}|{.status.lastAppliedRevision}",
        ]
    )
    data_health_parts = data_health_raw.split("|", 3) if data_health_raw else []
    if len(data_health_parts) != 4:
        data_ready = "missing"
        data_revision = "missing"
        data_matches_commit = False
    else:
        (
            data_generation,
            data_observed_generation,
            data_ready_status,
            data_revision,
        ) = data_health_parts
        if (
            not data_generation
            or not data_observed_generation
            or data_generation != data_observed_generation
        ):
            data_ready = "stale"
        else:
            data_ready = data_ready_status or "missing"
        data_revision = data_revision or "missing"
        data_matches_commit = flux_revision_matches_commit(data_revision, bootstrap_commit)
    pvcs = {
        pvc: output(
            [
                kubectl,
                "-n",
                DATA_NAMESPACE,
                "get",
                "pvc",
                pvc,
                "--ignore-not-found",
                "-o",
                "jsonpath={.status.phase}",
            ]
        ) or "missing"
        for pvc in ("postgres-data", "nats-data")
    }
    try:
        external_secret = verify_external_secret_binding(kubectl, root)
    except (StagingCellError, subprocess.CalledProcessError):
        external_secret = {
            "database": False,
            "runtime": False,
            "ready": False,
        }
    base_ready = (
        source_matches_commit
        and source_ready == "True"
        and data_ready == "True"
        and data_matches_commit
        and all(value == "Bound" for value in pvcs.values())
        and external_secret["ready"]
    )
    live_workloads = {name: "unchecked" for name in LIVE_DEPLOYMENTS}
    if base_ready:
        try:
            live_workloads = staging_live_health(kubectl)
        except (StagingCellError, subprocess.CalledProcessError):
            live_workloads = {name: "missing" for name in LIVE_DEPLOYMENTS}
    infrastructure_ready = base_ready and all(
        value == "True" for value in live_workloads.values()
    )
    activation_in_progress = owner.get("status") == "app-activation-in-progress"
    pending_active_commit = str(owner.get("pending_active_commit") or "")
    activated = owner.get("app_activation") is True
    app_workloads = {name: "unchecked" for name in APP_DEPLOYMENTS}
    image_references: dict[str, str] = {}
    expected_images: dict[str, str] = {}
    registry_pull_secret = {"ready": False}
    app_source_state = {
        "ready": "unchecked",
        "revision": "missing",
        "matches_commit": False,
    }
    app_kustomization_state = {
        "ready": "unchecked",
        "revision": "missing",
        "matches_commit": False,
    }
    if activated and infrastructure_ready:
        try:
            app_source_state = flux_resource_current_state(
                kubectl, "gitrepository", APP_SOURCE_NAME, active_commit
            )
            app_kustomization_state = flux_resource_current_state(
                kubectl, "kustomization", APP_KUSTOMIZATION, active_commit
            )
            app_workloads = app_live_health(kubectl)
            image_references = app_image_references(kubectl)
            registry_receipt = owner.get("registry_pull_secret")
            if isinstance(registry_receipt, dict):
                registry_pull_secret = verify_registry_pull_secret_binding(
                    kubectl,
                    expected_source_sha=str(
                        registry_receipt.get("source_sha256") or ""
                    ),
                    expected_config_sha256=str(
                        registry_receipt.get("config_sha256") or ""
                    ),
                )
        except (StagingCellError, subprocess.CalledProcessError):
            app_workloads = {name: "missing" for name in APP_DEPLOYMENTS}
        promotion = owner.get("image_promotion")
        if isinstance(promotion, dict) and isinstance(promotion.get("images"), dict):
            expected_images = {
                str(key): str(value) for key, value in promotion["images"].items()
            }
    app_ready = (
        not activated
        or (
            app_source_state.get("ready") == "True"
            and app_source_state.get("matches_commit") is True
            and app_kustomization_state.get("ready") == "True"
            and app_kustomization_state.get("matches_commit") is True
            and all(value == "True" for value in app_workloads.values())
            and registry_pull_secret.get("ready") is True
            and bool(expected_images)
            and image_references == expected_images
        )
    )
    ready = infrastructure_ready and app_ready and not activation_in_progress
    gateway_ready = ready and gateway_receipt_current(root, owner, kubectl)
    promotion_state = (
        owner.get("image_promotion")
        if activated and isinstance(owner.get("image_promotion"), dict)
        else image_promotion_state()
    )
    return {
        "schema_version": 1,
        "status": "ready" if ready else "degraded",
        "gateway_ready": gateway_ready,
        "gateway_phase": "gateway-ready" if gateway_ready else "gateway-pending",
        "does_not_establish": GATEWAY_LIMITS,
        "cluster": args.cluster,
        "owner_id": owner_id,
        "bootstrap_commit": bootstrap_commit,
        "active_commit": active_commit,
        "source_revision": source_revision,
        "source_matches_commit": source_matches_commit,
        "source_ready": source_ready,
        "data_ready": data_ready,
        "data_revision": data_revision,
        "data_matches_commit": data_matches_commit,
        "pvcs": pvcs,
        "external_secret": external_secret,
        "live_workloads": live_workloads,
        "image_promotion": promotion_state,
        "activation_in_progress": activation_in_progress,
        "pending_active_commit": pending_active_commit,
        "app_activation": activated,
        "app_workloads": app_workloads,
        "app_image_references": image_references,
        "app_source_revision": app_source_state.get("revision"),
        "app_source_matches_commit": app_source_state.get("matches_commit") is True,
        "app_kustomization_revision": app_kustomization_state.get("revision"),
        "app_kustomization_matches_commit": (
            app_kustomization_state.get("matches_commit") is True
        ),
        "registry_pull_secret_ready": registry_pull_secret.get("ready") is True,
        "production_changed": False,
    }


def _private_json_receipt(path: Path, *, label: str) -> dict[str, Any]:
    _private_regular_file(path, label=label)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise StagingCellError(f"{label} is unreadable or malformed") from error
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise StagingCellError(f"{label} is malformed")
    return payload


def _canonical_sha256(value: Any, *, label: str) -> str:
    digest = str(value or "")
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise StagingCellError(f"{label} is not a canonical sha256")
    return digest


def _exact_cell_promotion(root: Path, cell: dict[str, Any], commit: str) -> dict[str, Any]:
    promotion = load_promotion_receipt(root, commit)
    expected = {
        "status": "pass",
        "source_commit": commit,
        "receipt_sha256": promotion["receipt_sha256"],
        "images": promotion["images"],
    }
    if cell.get("image_promotion") != expected:
        raise StagingCellError(
            "promotion evidence differs from the active app receipt"
        )
    return expected


def _retained_tree_sha256(path: Path, *, label: str) -> str:
    digest = hashlib.sha256()

    def walk(directory: Path) -> None:
        try:
            directory_before = directory.stat(follow_symlinks=False)
        except OSError as error:
            raise StagingCellError(f"{label} cannot be fingerprinted") from error
        relative_directory = directory.relative_to(path)
        if stat.S_ISLNK(directory_before.st_mode):
            raise StagingCellError(
                f"{label} contains a symlink: {relative_directory}"
            )
        if not stat.S_ISDIR(directory_before.st_mode):
            raise StagingCellError(
                f"{label} contains unsupported filesystem state: {relative_directory}"
            )
        try:
            entries = sorted(directory.iterdir(), key=lambda item: os.fsencode(item.name))
        except OSError as error:
            raise StagingCellError(f"{label} cannot be fingerprinted") from error
        for entry in entries:
            relative = entry.relative_to(path)
            encoded_relative = os.fsencode(str(relative))
            try:
                before = entry.lstat()
            except OSError as error:
                raise StagingCellError(f"{label} changed during fingerprinting") from error
            if stat.S_ISLNK(before.st_mode):
                raise StagingCellError(f"{label} contains a symlink: {relative}")
            if stat.S_ISDIR(before.st_mode):
                digest.update(b"D\0" + encoded_relative + b"\0")
                walk(entry)
                continue
            if not stat.S_ISREG(before.st_mode):
                raise StagingCellError(
                    f"{label} contains unsupported filesystem state: {relative}"
                )
            digest.update(b"F\0" + encoded_relative + b"\0")
            try:
                with entry.open("rb") as handle:
                    while True:
                        chunk = handle.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                after = entry.lstat()
            except OSError as error:
                raise StagingCellError(f"{label} changed during fingerprinting") from error
            stable_file_fields = (
                "st_dev",
                "st_ino",
                "st_mode",
                "st_uid",
                "st_gid",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
            )
            if any(getattr(before, field) != getattr(after, field) for field in stable_file_fields):
                raise StagingCellError(f"{label} changed during fingerprinting")
            digest.update(b"\0")
        try:
            directory_after = directory.stat(follow_symlinks=False)
        except OSError as error:
            raise StagingCellError(f"{label} changed during fingerprinting") from error
        stable_directory_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_gid",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if any(
            getattr(directory_before, field) != getattr(directory_after, field)
            for field in stable_directory_fields
        ):
            raise StagingCellError(f"{label} changed during fingerprinting")

    walk(path)
    return digest.hexdigest()


def _retained_data_identity(
    root: Path, *, include_content: bool = True
) -> dict[str, dict[str, Any]]:
    identity: dict[str, dict[str, Any]] = {}
    for name in ("postgres", "nats"):
        if not retained_data_directory_exists(root, name):
            raise StagingCellError(
                "delete-to-prove requires retained PostgreSQL and NATS data"
            )
        path = root / "data" / name
        stable = _real_directory_identity(path, label=f"retained {name} data")
        linked = path.lstat()
        observed: dict[str, Any] = {
            **stable,
            "size": linked.st_size,
            "mtime_ns": linked.st_mtime_ns,
            "ctime_ns": linked.st_ctime_ns,
        }
        if include_content:
            observed["tree_sha256"] = _retained_tree_sha256(
                path, label=f"retained {name} data"
            )
        identity[name] = observed
    return identity


def _same_retained_data_anchors(
    before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]
) -> bool:
    anchor_fields = ("device", "inode", "uid", "gid", "mode")
    return all(
        isinstance(before.get(name), dict)
        and isinstance(after.get(name), dict)
        and all(before[name].get(field) == after[name].get(field) for field in anchor_fields)
        for name in ("postgres", "nats")
    )


def _require_durable_retained_fingerprint(
    identity: dict[str, dict[str, Any]], *, label: str
) -> None:
    for name in ("postgres", "nats"):
        observed = identity.get(name)
        if not isinstance(observed, dict):
            raise StagingCellError(f"{label} has no {name} identity")
        for field in ("device", "inode", "uid", "gid", "mode"):
            value = observed.get(field)
            if not isinstance(value, int) or isinstance(value, bool):
                raise StagingCellError(f"{label} has invalid {name} {field}")
        _canonical_sha256(
            observed.get("tree_sha256"),
            label=f"{label} {name} tree hash",
        )


def _down_receipt_binding(
    cell: dict[str, Any],
    cell_sha: str,
    gateway_sha: str,
    *,
    data_identity: dict[str, dict[str, Any]] | None = None,
    image_promotion: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": 1,
        "cluster": cell.get("cluster"),
        "owner_id": cell.get("owner_id"),
        "bootstrap_commit": cell.get("bootstrap_commit"),
        "cell_status": str(cell.get("status") or ""),
        "cell_receipt_sha256": cell_sha,
        "active_commit": str(cell.get("active_commit") or ""),
        "app_activation": cell.get("app_activation") is True,
        "gateway_proof_receipt_sha256": gateway_sha,
        "state_preserved": ["data", "secrets", "toolchain", "receipts"],
        "production_changed": False,
    }
    if cell.get("app_activation") is True:
        if data_identity is None or image_promotion is None:
            raise StagingCellError(
                "activated staging down requires exact data and promotion evidence"
            )
        result["pre_delete_data_identity"] = data_identity
        result["image_promotion"] = image_promotion
    return result


def _require_receipt_binding(payload: dict[str, Any], expected: dict[str, Any], *, label: str) -> None:
    for key, value in expected.items():
        if payload.get(key) != value:
            raise StagingCellError(f"{label} lost its {key} binding")


def load_delete_to_prove_down_receipt(
    root: Path,
    cell: dict[str, Any],
    *,
    require_current_cell_match: bool,
    require_retained_data_match: bool = True,
) -> dict[str, Any]:
    path = root / CELL_DOWN_RECEIPT
    payload = _private_json_receipt(path, label="staging down receipt")
    expected = {
        "status": "cluster-deleted-state-preserved",
        "cluster_was_present": True,
        "cluster": cell.get("cluster"),
        "owner_id": cell.get("owner_id"),
        "bootstrap_commit": cell.get("bootstrap_commit"),
        "active_commit": cell_active_commit(cell),
        "app_activation": True,
        "production_changed": False,
    }
    _require_receipt_binding(payload, expected, label="staging down receipt")
    if payload.get("state_preserved") != ["data", "secrets", "toolchain", "receipts"]:
        raise StagingCellError("staging down receipt does not preserve the complete recovery state")
    cell_sha = _canonical_sha256(
        payload.get("cell_receipt_sha256"), label="staging down cell receipt hash"
    )
    _canonical_sha256(
        payload.get("gateway_proof_receipt_sha256"),
        label="staging down gateway proof receipt hash",
    )
    pre_delete_data_identity = payload.get("pre_delete_data_identity")
    retained_data_identity = payload.get("retained_data_identity")
    if not isinstance(pre_delete_data_identity, dict) or not isinstance(
        retained_data_identity, dict
    ):
        raise StagingCellError("staging down receipt has no retained data identity")
    _require_durable_retained_fingerprint(
        pre_delete_data_identity, label="staging pre-delete data identity"
    )
    if not _same_retained_data_anchors(
        pre_delete_data_identity, retained_data_identity
    ):
        raise StagingCellError(
            "retained staging data directory anchor changed during cluster deletion"
        )
    # After deletion there is deliberately no new content baseline.  We only
    # prove that the same retained host directories remain present.  Content is
    # re-checked from the actual rebuilt mounts before data reconciliation starts.
    observed_data_identity = _retained_data_identity(root, include_content=False)
    if not _same_retained_data_anchors(retained_data_identity, observed_data_identity):
        phase = "before rebuild" if require_retained_data_match else "after rebuild"
        raise StagingCellError(
            f"retained staging data directory anchor changed {phase}"
        )
    image_promotion = payload.get("image_promotion")
    if not isinstance(image_promotion, dict):
        raise StagingCellError("staging down receipt has no image promotion identity")
    current_promotion = _exact_cell_promotion(root, cell, cell_active_commit(cell))
    if image_promotion != current_promotion:
        raise StagingCellError(
            "promotion evidence differs from the pre-delete release"
        )
    if require_current_cell_match and cell_sha != sha256_file(root / "receipts/cell-bootstrap.json"):
        raise StagingCellError("staging cell receipt changed after down; refusing rebuild")
    return {**payload, "receipt_sha256": sha256_file(path)}


@lifecycle_mutation_locked
@reference_output_routed
def command_down(args: argparse.Namespace) -> dict[str, Any]:
    require_singleton_cluster(args.cluster)
    reference.validate_owner_id(args.owner_id)
    root = state_root(getattr(args, "state_root", None))
    configure_reference_paths(root)
    cell = load_cell_receipt(root)
    require_receipt_cluster(cell, args.cluster)
    commit = str(cell.get("bootstrap_commit") or "")
    owner_id = str(cell.get("owner_id") or "")
    if args.owner_id != owner_id:
        raise StagingCellError("--owner-id does not match the persisted cluster owner")
    reference.validate_ownership_binding(commit, owner_id)

    cell_path = root / "receipts/cell-bootstrap.json"
    cell_sha = sha256_file(cell_path)
    activated = cell.get("app_activation") is True
    if activated and cell.get("status") != "gateway-ready":
        raise StagingCellError(
            "activated staging may be downed for delete-to-prove only from gateway-ready"
        )
    receipt = load_tool_receipt(
        root,
        required_tools=(("kind", "kubectl") if activated else ("kind",)),
        required_artifacts=(),
    )
    if not (root / CELL_DOWN_RECEIPT).exists() and (
        (root / CELL_REBUILD_RECEIPT).exists()
        or (root / DELETE_TO_PROVE_RECEIPT).exists()
    ):
        raise StagingCellError(
            "previous delete-to-prove recovery receipts must be retired before a new down cycle"
        )
    gateway_sha = ""
    gateway_binding = cell.get("gateway_proof")
    if activated and not isinstance(gateway_binding, dict):
        raise StagingCellError(
            "gateway-ready activated staging down requires a persisted gateway proof binding"
        )
    if isinstance(gateway_binding, dict):
        gateway_path = root / "receipts/gateway-proof.json"
        _private_regular_file(gateway_path, label="staging gateway proof receipt")
        gateway_sha = sha256_file(gateway_path)
        if gateway_binding.get("receipt_sha256") != gateway_sha:
            raise StagingCellError("staging gateway proof receipt changed before down")
        if gateway_binding.get("active_commit") != cell_active_commit(cell):
            raise StagingCellError("staging gateway proof active commit changed before down")

    image_promotion = (
        _exact_cell_promotion(root, cell, cell_active_commit(cell))
        if activated
        else None
    )
    kind = receipt["tools"]["kind"]
    kubectl = receipt.get("tools", {}).get("kubectl")
    if activated and (not isinstance(kubectl, str) or not kubectl):
        raise StagingCellError("activated staging down requires kubectl from the tool receipt")
    path = root / CELL_DOWN_RECEIPT
    cluster_present = args.cluster in reference.clusters(kind)
    cluster_was_present = cluster_present
    started_at_unix = int(time.time())
    previous: dict[str, Any] | None = None
    previous_status = ""
    if path.exists() or path.is_symlink():
        previous = _private_json_receipt(path, label="staging down receipt")
        previous_status = str(previous.get("status") or "")
        if previous_status not in {
            "cluster-delete-in-progress",
            "cluster-deleted-state-preserved",
            "cluster-absent-state-preserved",
        }:
            raise StagingCellError("staging down receipt has an unexpected status")

    pre_delete_data_identity: dict[str, dict[str, Any]] | None = None
    if activated:
        if previous is not None:
            previous_pre_delete = previous.get("pre_delete_data_identity")
            if not isinstance(previous_pre_delete, dict):
                raise StagingCellError(
                    "staging down receipt lost its durable pre-delete data identity"
                )
            _require_durable_retained_fingerprint(
                previous_pre_delete, label="pending staging pre-delete data identity"
            )
            pre_delete_data_identity = previous_pre_delete
        elif not cluster_present:
            raise StagingCellError(
                "activated staging cannot establish a pre-delete data baseline after the cluster is absent"
            )

        if previous_status == "cluster-delete-in-progress" and not cluster_present:
            observed_anchors = _retained_data_identity(root, include_content=False)
            if not _same_retained_data_anchors(
                pre_delete_data_identity, observed_anchors
            ):
                raise StagingCellError(
                    "pending staging down receipt lost its retained data directory anchors"
                )
        elif previous_status not in {
            "cluster-deleted-state-preserved",
            "cluster-absent-state-preserved",
        }:
            assert isinstance(kubectl, str)
            _quiesce_retained_data(kubectl)
            current_pre_delete = _mounted_retained_data_identity(
                kind,
                args.cluster,
                root,
                durable=True,
                require_split=False,
            )
            if pre_delete_data_identity is None:
                pre_delete_data_identity = current_pre_delete
            elif pre_delete_data_identity != current_pre_delete:
                raise StagingCellError(
                    "pending staging down receipt no longer matches the durable quiescent data fingerprint"
                )

    binding = _down_receipt_binding(
        cell,
        cell_sha,
        gateway_sha,
        data_identity=pre_delete_data_identity,
        image_promotion=image_promotion,
    )

    if previous is not None:
        if previous_status in {
            "cluster-deleted-state-preserved",
            "cluster-absent-state-preserved",
        }:
            if any(previous.get(key) != value for key, value in binding.items()):
                raise StagingCellError(
                    "previous delete-to-prove recovery cycle must be retired before a new down cycle"
                )
        else:
            _require_receipt_binding(
                previous, binding, label="pending staging down receipt"
            )
        if previous_status == "cluster-delete-in-progress":
            if not isinstance(previous.get("cluster_was_present"), bool):
                raise StagingCellError(
                    "pending staging down receipt lacks cluster presence evidence"
                )
            cluster_was_present = bool(previous["cluster_was_present"])
            started_at_unix = int(previous.get("started_at_unix") or 0)
            if started_at_unix <= 0:
                raise StagingCellError(
                    "pending staging down receipt lacks a valid start time"
                )
        else:
            if activated:
                retained_identity = previous.get("retained_data_identity")
                if not isinstance(retained_identity, dict):
                    raise StagingCellError(
                        "terminal staging down receipt lost its retained data identity"
                    )
                observed_anchors = _retained_data_identity(root, include_content=False)
                if not _same_retained_data_anchors(
                    retained_identity, observed_anchors
                ):
                    raise StagingCellError(
                        "terminal staging down receipt lost its retained data directory anchors"
                    )
            if cluster_present:
                raise StagingCellError(
                    "staging down receipt is already terminal but the same bound cluster exists; refusing ambiguous reuse"
                )
            return {
                **previous,
                "receipt_path": str(path),
                "receipt_sha256": sha256_file(path),
            }

    # For activated delete-to-prove, this durable receipt is the point of no
    # return: the quiescent Data-Node fingerprint exists on disk before delete.
    # Unactivated teardown does not need a data baseline and can remain retry-safe
    # without writing a misleading pre-delete receipt.
    if activated and previous is None:
        pending = {
            **binding,
            "status": "cluster-delete-in-progress",
            "cluster_was_present": cluster_was_present,
            "started_at_unix": started_at_unix,
        }
        atomic_json(path, pending)

    reference.delete_owned_cluster_if_present(
        kind,
        args.cluster,
        expected_commit=commit,
        expected_owner_id=owner_id,
    )
    retained_data_identity = (
        _retained_data_identity(root, include_content=False) if activated else None
    )
    if activated and (
        not isinstance(pre_delete_data_identity, dict)
        or not isinstance(retained_data_identity, dict)
        or not _same_retained_data_anchors(
            pre_delete_data_identity, retained_data_identity
        )
    ):
        raise StagingCellError(
            "retained staging data directory anchor changed during cluster deletion"
        )
    result = {
        **binding,
        **(
            {"retained_data_identity": retained_data_identity}
            if activated
            else {}
        ),
        "status": (
            "cluster-deleted-state-preserved"
            if cluster_was_present
            else "cluster-absent-state-preserved"
        ),
        "cluster_was_present": cluster_was_present,
        "started_at_unix": started_at_unix,
        "completed_at_unix": int(time.time()),
    }
    atomic_json(path, result)
    return {
        **result,
        "receipt_path": str(path),
        "receipt_sha256": sha256_file(path),
    }


def _rebuild_receipt_binding(
    cell: dict[str, Any], down: dict[str, Any], source_commit: str
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "cluster": cell.get("cluster"),
        "owner_id": cell.get("owner_id"),
        "bootstrap_commit": cell.get("bootstrap_commit"),
        "active_commit": cell_active_commit(cell),
        "implementation_commit": source_commit,
        "pre_delete_cell_receipt_sha256": down["cell_receipt_sha256"],
        "pre_delete_gateway_proof_receipt_sha256": down["gateway_proof_receipt_sha256"],
        "image_promotion": down["image_promotion"],
        "pre_delete_data_identity": down["pre_delete_data_identity"],
        "retained_data_identity": down["retained_data_identity"],
        "down_receipt_sha256": down["receipt_sha256"],
        "production_changed": False,
    }


def _delete_to_prove_reactivation_binding(
    root: Path,
    cell: dict[str, Any],
    commit: str,
    promotion: dict[str, Any],
) -> dict[str, Any] | None:
    rebuild_path = root / CELL_REBUILD_RECEIPT
    if not (rebuild_path.exists() or rebuild_path.is_symlink()):
        return None
    rebuild = _private_json_receipt(rebuild_path, label="staging rebuild receipt")
    final_path = root / DELETE_TO_PROVE_RECEIPT
    if final_path.exists() or final_path.is_symlink():
        final = _private_json_receipt(final_path, label="delete-to-prove receipt")
        if (
            final.get("status") != "delete-to-prove-verified"
            or final.get("rebuild_receipt_sha256") != sha256_file(rebuild_path)
        ):
            raise StagingCellError(
                "delete-to-prove terminal receipt lost its rebuild binding"
            )
        return None
    if rebuild.get("status") != "infrastructure-rebuilt-app-reactivation-required":
        raise StagingCellError(
            "delete-to-prove recovery must finish infrastructure rebuild before activation"
        )
    if rebuild.get("active_commit") != commit:
        raise StagingCellError(
            "delete-to-prove recovery must reactivate the exact pre-delete app commit"
        )
    activation_in_progress = cell.get("status") == "app-activation-in-progress"
    down = load_delete_to_prove_down_receipt(
        root,
        cell,
        require_current_cell_match=not activation_in_progress,
        require_retained_data_match=False,
    )
    expected_rebuild = _rebuild_receipt_binding(cell, down, commit)
    _require_receipt_binding(
        rebuild, expected_rebuild, label="staging rebuild receipt before reactivation"
    )
    promotion_identity = {
        "status": "pass",
        "source_commit": commit,
        "receipt_sha256": promotion["receipt_sha256"],
        "images": promotion["images"],
    }
    if rebuild.get("image_promotion") != promotion_identity:
        raise StagingCellError(
            "promotion evidence differs from the pre-delete release before reactivation"
        )
    binding = {
        "down_receipt_sha256": down["receipt_sha256"],
        "rebuild_receipt_sha256": sha256_file(rebuild_path),
        "image_promotion": promotion_identity,
    }
    if activation_in_progress and cell.get("pending_delete_to_prove_recovery") != binding:
        raise StagingCellError(
            "activation recovery lost its delete-to-prove rebuild binding"
        )
    return binding


@lifecycle_mutation_locked
@reference_output_routed
def command_rebuild(args: argparse.Namespace) -> dict[str, Any]:
    require_singleton_cluster(args.cluster)
    reference.validate_owner_id(args.owner_id)
    root = state_root(getattr(args, "state_root", None))
    configure_reference_paths(root)
    tool_receipt = load_tool_receipt(root)
    cell = load_cell_receipt(root)
    require_receipt_cluster(cell, args.cluster)
    owner_id = str(cell.get("owner_id") or "")
    bootstrap_commit = str(cell.get("bootstrap_commit") or "")
    if args.owner_id != owner_id:
        raise StagingCellError("--owner-id does not match the persisted cluster owner")
    reference.validate_ownership_binding(bootstrap_commit, owner_id)
    if cell.get("app_activation") is not True or cell.get("status") != "gateway-ready":
        raise StagingCellError(
            "delete-to-prove rebuild requires a previously gateway-ready activated cell"
        )
    active_commit = cell_active_commit(cell)
    implementation_commit = require_clean_commit(args.source_commit)
    if implementation_commit != active_commit:
        raise StagingCellError(
            "delete-to-prove rebuild implementation must equal the active app commit"
        )
    promotion = _exact_cell_promotion(root, cell, active_commit)
    down = load_delete_to_prove_down_receipt(
        root, cell, require_current_cell_match=True
    )
    if down.get("image_promotion") != promotion:
        raise StagingCellError(
            "promotion evidence differs from the pre-delete release"
        )
    observed_host_anchors = _retained_data_identity(root, include_content=False)
    retained_after_delete = down.get("retained_data_identity")
    if not isinstance(retained_after_delete, dict) or not _same_retained_data_anchors(
        retained_after_delete, observed_host_anchors
    ):
        raise StagingCellError(
            "retained staging data directory anchor differs from the down receipt"
        )

    _, secret_sha = load_or_create_secret_material(root)
    external = (
        cell.get("external_secret")
        if isinstance(cell.get("external_secret"), dict)
        else {}
    )
    if external.get("source_sha256") != secret_sha:
        raise StagingCellError(
            "retained runtime secret differs from the pre-delete cell receipt"
        )
    registry_material, registry_source_sha = load_registry_pull_material(root)
    registry_binding = (
        cell.get("registry_pull_secret")
        if isinstance(cell.get("registry_pull_secret"), dict)
        else {}
    )
    if registry_binding.get("source_sha256") != registry_source_sha:
        raise StagingCellError(
            "retained registry secret differs from the pre-delete cell receipt"
        )
    expected_registry_config = sha256_bytes(
        registry_dockerconfig_json(registry_material).encode("utf-8")
    )
    if registry_binding.get("config_sha256") != expected_registry_config:
        raise StagingCellError(
            "retained registry config differs from the pre-delete cell receipt"
        )

    binding = _rebuild_receipt_binding(cell, down, implementation_commit)
    rebuild_path = root / CELL_REBUILD_RECEIPT
    existing_rebuild: dict[str, Any] | None = None
    if rebuild_path.exists() or rebuild_path.is_symlink():
        existing_rebuild = _private_json_receipt(
            rebuild_path, label="staging rebuild receipt"
        )
        _require_receipt_binding(
            existing_rebuild, binding, label="staging rebuild receipt"
        )
        if existing_rebuild.get("status") not in {
            "rebuild-in-progress",
            "retained-mount-verified-data-reconcile-authorized",
            "infrastructure-rebuilt-app-reactivation-required",
        }:
            raise StagingCellError("staging rebuild receipt has an unexpected status")

    kind = tool_receipt["tools"]["kind"]
    kubectl = tool_receipt["tools"]["kubectl"]
    flux = tool_receipt["tools"]["flux"]
    helm = tool_receipt["tools"]["helm"]
    cluster_present = args.cluster in reference.clusters(kind)
    if existing_rebuild is None and cluster_present:
        raise StagingCellError(
            "delete-to-prove rebuild requires the downed cluster to be absent before first recovery"
        )
    if (
        existing_rebuild is not None
        and existing_rebuild.get("status")
        in {
            "retained-mount-verified-data-reconcile-authorized",
            "infrastructure-rebuilt-app-reactivation-required",
        }
        and not cluster_present
    ):
        raise StagingCellError(
            "staging rebuild lost its already mount-verified cluster; refusing to create a second cluster under the same recovery receipt"
        )

    started_at_unix = int(time.time())
    if existing_rebuild is not None:
        started_at_unix = int(existing_rebuild.get("started_at_unix") or 0)
        if started_at_unix <= 0:
            raise StagingCellError("staging rebuild receipt lacks a valid start time")
    else:
        atomic_json(
            rebuild_path,
            {
                **binding,
                "status": "rebuild-in-progress",
                "started_at_unix": started_at_unix,
            },
        )

    if cluster_present:
        reference.require_owned_cluster(
            kind,
            args.cluster,
            expected_commit=bootstrap_commit,
            expected_owner_id=owner_id,
        )
        created = bool(
            existing_rebuild is not None
            and existing_rebuild.get("cluster_created") is True
        )
    else:
        if existing_rebuild is not None:
            reference.clear_stale_cluster_reservation(
                kind,
                args.cluster,
                expected_commit=bootstrap_commit,
                expected_owner_id=owner_id,
            )
        rendered_kind_config = render_kind_config(root)
        reference.create_kind_cluster(
            kind,
            args.cluster,
            tool_receipt["kubernetes"]["kind_node_image"],
            str(rendered_kind_config),
            bootstrap_commit,
            owner_id,
            timeout=900,
        )
        created = True
        cluster_present = True

    current_status = (
        str(existing_rebuild.get("status") or "")
        if existing_rebuild is not None
        else "rebuild-in-progress"
    )
    pre_delete_identity = down.get("pre_delete_data_identity")
    if not isinstance(pre_delete_identity, dict):
        raise StagingCellError("staging down receipt lost its pre-delete data identity")
    _require_durable_retained_fingerprint(
        pre_delete_identity, label="staging pre-delete data identity"
    )

    if current_status == "infrastructure-rebuilt-app-reactivation-required":
        retained_mount_identity = existing_rebuild.get("retained_mount_identity")
        if not isinstance(retained_mount_identity, dict):
            raise StagingCellError(
                "completed staging rebuild lost its retained mount proof"
            )
        mounted_anchors = _mounted_retained_data_anchors(
            kind, args.cluster, root, require_split=True
        )
        if not _same_retained_data_anchors(
            retained_mount_identity, mounted_anchors
        ) or not _same_retained_data_anchors(pre_delete_identity, mounted_anchors):
            raise StagingCellError(
                "completed staging rebuild no longer exposes the proven retained mounts"
            )
        live_workloads = staging_live_health(kubectl)
        unhealthy = {
            name: state for name, state in live_workloads.items() if state != "True"
        }
        if unhealthy:
            raise StagingCellError(
                f"rebuilt staging infrastructure is not live: {unhealthy!r}"
            )
        return {
            **existing_rebuild,
            "receipt_path": str(rebuild_path),
            "receipt_sha256": sha256_file(rebuild_path),
        }

    prepare_volume_permissions(kind, args.cluster, root)
    if current_status == "rebuild-in-progress":
        retained_mount_identity = _mounted_retained_data_identity(
            kind,
            args.cluster,
            root,
            durable=False,
            require_split=True,
        )
        if retained_mount_identity != pre_delete_identity:
            raise StagingCellError(
                "rebuilt staging retained mount content differs from the durable pre-delete fingerprint"
            )
        mount_authorized = {
            **binding,
            "status": "retained-mount-verified-data-reconcile-authorized",
            "cluster_created": created,
            "retained_mount_identity": retained_mount_identity,
            "started_at_unix": started_at_unix,
            "mount_verified_at_unix": int(time.time()),
        }
        atomic_json(rebuild_path, mount_authorized)
        existing_rebuild = mount_authorized
    else:
        assert existing_rebuild is not None
        retained_mount_identity = existing_rebuild.get("retained_mount_identity")
        if not isinstance(retained_mount_identity, dict):
            raise StagingCellError(
                "mount-authorized staging rebuild lost its retained mount proof"
            )
        mounted_anchors = _mounted_retained_data_anchors(
            kind, args.cluster, root, require_split=True
        )
        if not _same_retained_data_anchors(
            retained_mount_identity, mounted_anchors
        ) or not _same_retained_data_anchors(pre_delete_identity, mounted_anchors):
            raise StagingCellError(
                "mount-authorized staging rebuild no longer exposes the proven retained mounts"
            )
        created = existing_rebuild.get("cluster_created") is True

    api_server_host = reference.control_plane_address(args.cluster)
    reference.install_platform_components(
        kubectl, flux, helm, tool_receipt["artifacts"], api_server_host
    )
    run(
        [
            kubectl,
            "wait",
            "--for=condition=Ready",
            "nodes",
            "--all",
            "--timeout=5m",
        ],
        timeout=360,
    )
    secret_receipt = inject_external_secrets(kubectl, root)
    registry_receipt = inject_registry_pull_secret(
        kubectl,
        root,
        material=registry_material,
        source_sha=registry_source_sha,
    )
    if secret_receipt.get("source_sha256") != secret_sha:
        raise StagingCellError(
            "rebuilt runtime Secret source hash drifted during injection"
        )
    if registry_receipt.get("source_sha256") != registry_source_sha:
        raise StagingCellError(
            "rebuilt registry Secret source hash drifted during injection"
        )
    if registry_receipt.get("config_sha256") != expected_registry_config:
        raise StagingCellError(
            "rebuilt registry Secret config hash drifted during injection"
        )

    # The new data Kustomization is born suspended, so installing Flux cannot
    # race the retained-mount proof by starting PostgreSQL or NATS early.
    apply_yaml(
        kubectl,
        flux_documents(bootstrap_commit, suspend_data=True),
    )
    _set_data_reconciliation_suspended(kubectl, suspended=True)

    # At this point the mount proof is already durable.  Resuming reconciliation
    # may legitimately change database contents, so later retries compare only
    # the stable physical mount anchors rather than the pre-start tree hash.
    _set_data_reconciliation_suspended(kubectl, suspended=False)
    reconcile_data(kubectl, bootstrap_commit)
    live_workloads = staging_live_health(kubectl)
    unhealthy = {
        name: state for name, state in live_workloads.items() if state != "True"
    }
    if unhealthy:
        raise StagingCellError(
            f"rebuilt staging infrastructure is not live: {unhealthy!r}"
        )
    node_names = output([kubectl, "get", "nodes", "-o", "name"]).splitlines()
    if len(node_names) != 3:
        raise StagingCellError(
            f"rebuilt staging node count drift: expected 3, observed {len(node_names)}"
        )
    result = {
        **binding,
        "status": "infrastructure-rebuilt-app-reactivation-required",
        "cluster_created": created,
        "retained_mount_identity": retained_mount_identity,
        "node_count": len(node_names),
        "live_workloads": live_workloads,
        "external_secret_source_sha256": secret_receipt["source_sha256"],
        "registry_secret_source_sha256": registry_receipt["source_sha256"],
        "started_at_unix": started_at_unix,
        "completed_at_unix": int(time.time()),
    }
    atomic_json(rebuild_path, result)
    return {
        **result,
        "receipt_path": str(rebuild_path),
        "receipt_sha256": sha256_file(rebuild_path),
    }


def _backup_cycle_directory(root: Path, release_commit: str) -> Path:
    if len(release_commit) != 40 or any(ch not in "0123456789abcdef" for ch in release_commit):
        raise StagingCellError("backup recovery release commit is not canonical")
    return root / "backups" / "t084-backup-delete-to-prove" / release_commit


def _backup_archive_paths(root: Path, release_commit: str) -> dict[str, Path]:
    directory = _backup_cycle_directory(root, release_commit)
    return {name: directory / f"{name}.tar" for name in ("postgres", "nats")}


def _require_fresh_backup_archive_paths(root: Path, release_commit: str) -> None:
    for name, path in _backup_archive_paths(root, release_commit).items():
        if path.exists() or path.is_symlink():
            raise StagingCellError(
                f"staging backup archive already exists without a bound backup intent: {name}"
            )


def _backup_archive_entry(name: str, path: Path) -> dict[str, Any]:
    _private_regular_file(path, label=f"staging {name} backup archive")
    size = path.stat().st_size
    if size <= 0:
        raise StagingCellError(f"staging {name} backup archive is empty")
    return {"path": str(path), "sha256": sha256_file(path), "bytes": size}


def _verify_backup_archive_entry(name: str, expected_path: Path, entry: Any) -> None:
    if not isinstance(entry, dict):
        raise StagingCellError(f"staging {name} backup receipt is malformed")
    if entry.get("path") != str(expected_path):
        raise StagingCellError(f"staging {name} backup path drift")
    observed = _backup_archive_entry(name, expected_path)
    if entry.get("sha256") != observed["sha256"]:
        raise StagingCellError(f"staging {name} backup archive hash drift")
    if entry.get("bytes") != observed["bytes"]:
        raise StagingCellError(f"staging {name} backup archive size drift")


def _backup_volume_archives(
    kind: str,
    cluster: str,
    root: Path,
    release_commit: str,
    *,
    existing_archives: dict[str, Any] | None = None,
    progress: Any | None = None,
) -> dict[str, dict[str, Any]]:
    data_node = _retained_mount_node(kind, cluster, root, require_split=True)
    paths = _backup_archive_paths(root, release_commit)
    ensure_directory_durable(next(iter(paths.values())).parent)
    result: dict[str, dict[str, Any]] = dict(existing_archives or {})
    if set(result) - set(paths):
        raise StagingCellError("staging backup progress contains an unknown archive")
    for name, path in paths.items():
        recorded = result.get(name)
        if recorded is not None:
            _verify_backup_archive_entry(name, path, recorded)
        elif path.exists() or path.is_symlink():
            # stream_command_to_file publishes by fsync + atomic rename. A crash
            # can therefore leave a complete archive just before its receipt
            # update. The earlier backup-intent receipt authorises adopting only
            # this exact private path while the source data remains quiescent.
            result[name] = _backup_archive_entry(name, path)
        else:
            volume = f"/var/local/commonthing-staging/{name}"
            stream_command_to_file(
                [
                    "docker",
                    "exec",
                    data_node,
                    "tar",
                    "--sort=name",
                    "--format=gnu",
                    "--numeric-owner",
                    "-C",
                    volume,
                    "-cf",
                    "-",
                    ".",
                ],
                path,
                timeout=600,
            )
            result[name] = _backup_archive_entry(name, path)
        if progress is not None:
            progress(dict(result))
    return result


def _verify_backup_archives(
    root: Path, release_commit: str, archives: dict[str, Any]
) -> dict[str, Path]:
    expected_paths = _backup_archive_paths(root, release_commit)
    if set(archives) != set(expected_paths):
        raise StagingCellError("staging backup receipt archive set is incomplete")
    for name, expected_path in expected_paths.items():
        _verify_backup_archive_entry(name, expected_path, archives.get(name))
    return expected_paths


def _identity_anchor_matches(expected: dict[str, Any], path: Path) -> bool:
    try:
        observed = _real_directory_identity(path, label="backup recovery data directory")
    except (OSError, StagingCellError):
        return False
    return all(observed.get(field) == expected.get(field) for field in ("device", "inode"))


def _prepare_empty_restore_roots(
    root: Path,
    release_commit: str,
    pre_delete_identity: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    snapshot_root = root / "recovery-snapshots" / release_commit / "retained-original"
    ensure_directory_durable(snapshot_root)
    result: dict[str, dict[str, Any]] = {}
    for name in ("postgres", "nats"):
        expected = pre_delete_identity.get(name)
        if not isinstance(expected, dict):
            raise StagingCellError(f"backup recovery lost pre-delete {name} identity")
        source = root / "data" / name
        retained = snapshot_root / name
        if not retained.exists():
            if not _identity_anchor_matches(expected, source):
                raise StagingCellError(
                    f"refusing to rotate staging {name}: active directory is not the proven pre-delete root"
                )
            os.replace(source, retained)
            fsync_directory(source.parent)
            fsync_directory(retained.parent)
        elif not _identity_anchor_matches(expected, retained):
            raise StagingCellError(
                f"staging {name} forensic original does not match the proven pre-delete root"
            )
        if source.exists():
            linked = source.lstat()
            if stat.S_ISLNK(linked.st_mode) or not stat.S_ISDIR(linked.st_mode):
                raise StagingCellError(f"staging {name} restore root is unsafe")
            try:
                has_entries = next(source.iterdir(), None) is not None
            except OSError as error:
                raise StagingCellError(f"staging {name} restore root is unreadable") from error
            if has_entries:
                raise StagingCellError(
                    f"staging {name} restore root is not empty before backup restore"
                )
        else:
            source.mkdir(mode=0o700)
            fsync_directory(source.parent)
        result[name] = {
            **_real_directory_identity(source, label=f"empty staging {name} restore root"),
            "empty": True,
            "forensic_original": str(retained),
        }
    return result


def _restore_volume_archives(
    kind: str,
    cluster: str,
    root: Path,
    release_commit: str,
    archives: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    paths = _verify_backup_archives(root, release_commit, archives)
    data_node = _retained_mount_node(kind, cluster, root, require_split=True)
    for name, path in paths.items():
        volume = f"/var/local/commonthing-staging/{name}"
        occupied = output(
            [
                "docker",
                "exec",
                data_node,
                "find",
                volume,
                "-mindepth",
                "1",
                "-maxdepth",
                "1",
                "-print",
                "-quit",
            ],
            timeout=30,
        )
        if occupied:
            raise StagingCellError(
                f"staging {name} restore target is not empty before archive extraction"
            )
        stream_file_to_command(
            path,
            [
                "docker",
                "exec",
                "-i",
                data_node,
                "tar",
                "--numeric-owner",
                "-C",
                volume,
                "-xf",
                "-",
            ],
            timeout=600,
        )
    return _mounted_retained_data_identity(
        kind, cluster, root, durable=True, require_split=True
    )


def _same_data_tree_hashes(before: dict[str, Any], after: dict[str, Any]) -> bool:
    return all(
        isinstance(before.get(name), dict)
        and isinstance(after.get(name), dict)
        and before[name].get("tree_sha256") == after[name].get("tree_sha256")
        and isinstance(before[name].get("tree_sha256"), str)
        for name in ("postgres", "nats")
    )


def _same_data_mount_anchors(before: dict[str, Any], after: dict[str, Any]) -> bool:
    return all(
        isinstance(before.get(name), dict)
        and isinstance(after.get(name), dict)
        and all(
            before[name].get(field) == after[name].get(field)
            for field in ("device", "inode")
        )
        for name in ("postgres", "nats")
    )


def _load_completed_backup_rebuild_receipt(
    root: Path, down: dict[str, Any]
) -> dict[str, Any]:
    path = root / BACKUP_REBUILD_RECEIPT
    if not (path.exists() or path.is_symlink()):
        raise StagingCellError(
            "terminal backup-down state requires a completed backup rebuild before activation"
        )
    rebuild = _private_json_receipt(path, label="backup rebuild receipt")
    expected = {
        "status": "backup-restored-infrastructure-ready-app-reactivation-required",
        "cluster": down.get("cluster"),
        "owner_id": down.get("owner_id"),
        "bootstrap_commit": down.get("bootstrap_commit"),
        "release_commit": down.get("release_commit"),
        "controller_commit": down.get("controller_commit"),
        "backup_down_receipt_sha256": down.get("receipt_sha256"),
        "production_changed": False,
    }
    for key, value in expected.items():
        if rebuild.get(key) != value:
            raise StagingCellError(
                f"completed backup rebuild lost its backup-down binding: {key}"
            )
    empty_roots = down.get("empty_restore_roots")
    restored = rebuild.get("restored_data_identity")
    if (
        not isinstance(empty_roots, dict)
        or not isinstance(restored, dict)
        or not _same_data_mount_anchors(empty_roots, restored)
    ):
        raise StagingCellError(
            "completed backup rebuild is not bound to the proven empty restore roots"
        )
    return {
        **rebuild,
        "receipt_path": str(path),
        "receipt_sha256": sha256_file(path),
    }


def _require_no_pending_backup_down_before_activation(
    root: Path, requested_commit: str
) -> None:
    path = root / BACKUP_DOWN_RECEIPT
    if not (path.exists() or path.is_symlink()):
        return
    receipt = _load_backup_down_receipt(root, allow_pending=True)
    if receipt.get("status") != "backup-created-cluster-deleted-primary-data-empty":
        raise StagingCellError(
            "cannot activate while backup delete-to-prove is pending; "
            "resume the existing backup cycle first"
        )
    rebuild = _load_completed_backup_rebuild_receipt(root, receipt)
    terminal_path = root / BACKUP_DELETE_TO_PROVE_RECEIPT
    if terminal_path.exists() or terminal_path.is_symlink():
        completed = _validated_existing_backup_delete_to_prove_receipt(
            root,
            cluster=str(receipt.get("cluster") or ""),
            owner_id=str(receipt.get("owner_id") or ""),
            release_commit=str(rebuild.get("release_commit") or ""),
            controller_commit=str(rebuild.get("controller_commit") or ""),
            down=receipt,
            rebuild=rebuild,
        )
        if completed is not None:
            return
    if requested_commit != rebuild.get("release_commit"):
        raise StagingCellError(
            "activation before terminal backup proof must use the restored historical release"
        )


def _load_backup_down_receipt(
    root: Path, *, allow_pending: bool = False
) -> dict[str, Any]:
    path = root / BACKUP_DOWN_RECEIPT
    payload = _private_json_receipt(path, label="backup delete-to-prove down receipt")
    status = payload.get("status")
    allowed = {"backup-created-cluster-deleted-primary-data-empty"}
    if allow_pending:
        allowed.update(
            {
                "backup-quiesce-pending",
                "backup-app-quiesced-data-stop-pending",
                "backup-archive-creation-pending",
                "backup-created-cluster-delete-pending",
            }
        )
    if status not in allowed:
        raise StagingCellError("backup delete-to-prove down receipt has unexpected state")
    if payload.get("production_changed") is not False:
        raise StagingCellError("backup delete-to-prove down receipt lost production isolation")
    release_commit = str(payload.get("release_commit") or "")
    if status != "backup-quiesce-pending":
        api_hash = str(payload.get("pre_delete_api_nodes_sha256") or "")
        api_count = payload.get("pre_delete_api_nodes_count")
        api_pages = payload.get("pre_delete_api_nodes_pages")
        if (
            len(api_hash) != 64
            or any(ch not in "0123456789abcdef" for ch in api_hash)
            or not isinstance(api_count, int)
            or isinstance(api_count, bool)
            or api_count < 0
            or not isinstance(api_pages, int)
            or isinstance(api_pages, bool)
            or api_pages < 1
            or payload.get("pre_delete_api_nodes_hash_scope") != API_NODES_HASH_SCOPE
            or payload.get("pre_delete_api_nodes_source")
            != "quiesced-postgres-api-projection-v1"
        ):
            raise StagingCellError("backup delete-to-prove receipt lost its quiesced API baseline")
    archives = payload.get("backup_archives", {})
    if not isinstance(archives, dict):
        raise StagingCellError("backup delete-to-prove down receipt lost its archives")
    expected_paths = _backup_archive_paths(root, release_commit)
    if set(archives) - set(expected_paths):
        raise StagingCellError("backup delete-to-prove receipt has unknown archive state")
    for name, entry in archives.items():
        _verify_backup_archive_entry(name, expected_paths[name], entry)
    if status in {
        "backup-archive-creation-pending",
        "backup-created-cluster-delete-pending",
        "backup-created-cluster-deleted-primary-data-empty",
    }:
        pre_delete = payload.get("pre_delete_data_identity")
        if not isinstance(pre_delete, dict):
            raise StagingCellError("backup delete-to-prove down receipt lost data identity")
        _require_durable_retained_fingerprint(
            pre_delete, label="backup delete-to-prove pre-delete data identity"
        )
    if status in {
        "backup-created-cluster-delete-pending",
        "backup-created-cluster-deleted-primary-data-empty",
    }:
        _verify_backup_archives(root, release_commit, archives)
    if status == "backup-created-cluster-deleted-primary-data-empty":
        empty_roots = payload.get("empty_restore_roots")
        if not isinstance(empty_roots, dict):
            raise StagingCellError("backup delete-to-prove down receipt lost empty restore roots")
        for name in ("postgres", "nats"):
            root_identity = empty_roots.get(name)
            if not isinstance(root_identity, dict) or root_identity.get("empty") is not True:
                raise StagingCellError(f"backup down receipt lost empty {name} restore identity")
    return {**payload, "receipt_sha256": sha256_file(path)}


def _complete_backup_down_from_pending(
    root: Path,
    args: argparse.Namespace,
    pending: dict[str, Any],
    *,
    resumed: bool,
) -> dict[str, Any]:
    tools = load_tool_receipt(
        root, required_tools=("kind",), required_artifacts=()
    )["tools"]
    reference.delete_owned_cluster_if_present(
        tools["kind"],
        args.cluster,
        expected_commit=pending["bootstrap_commit"],
        expected_owner_id=args.owner_id,
    )
    started_at_unix = pending.get("started_at_unix")
    if not isinstance(started_at_unix, int) or isinstance(started_at_unix, bool):
        raise StagingCellError("backup delete-to-prove pending receipt lost its start boundary")
    if resumed:
        cluster_deleted_at_unix = started_at_unix
        recovery_boundary_basis = "conservative-cycle-start-after-unobserved-delete"
    else:
        cluster_deleted_at_unix = int(time.time())
        recovery_boundary_basis = "cluster-delete-observed"
    empty_roots = _prepare_empty_restore_roots(
        root, pending["release_commit"], pending["pre_delete_data_identity"]
    )
    persisted_pending = dict(pending)
    persisted_pending.pop("receipt_sha256", None)
    persisted_pending.pop("receipt_path", None)
    result = {
        **persisted_pending,
        "status": "backup-created-cluster-deleted-primary-data-empty",
        "empty_restore_roots": empty_roots,
        "cluster_deleted_at_unix": cluster_deleted_at_unix,
        "recovery_boundary_basis": recovery_boundary_basis,
        "completed_at_unix": int(time.time()),
    }
    path = root / BACKUP_DOWN_RECEIPT
    atomic_json(path, result)
    return {**result, "receipt_path": str(path), "receipt_sha256": sha256_file(path)}


def _require_backup_pending_release_current(
    root: Path, pending: dict[str, Any]
) -> None:
    release_commit = str(pending.get("release_commit") or "")
    cluster = str(pending.get("cluster") or "")
    owner_id = str(pending.get("owner_id") or "")
    expected_cell_sha = _canonical_sha256(
        pending.get("cell_receipt_sha256"),
        label="backup pending cell receipt hash",
    )
    expected_gateway_sha = _canonical_sha256(
        pending.get("gateway_receipt_sha256"),
        label="backup pending Gateway receipt hash",
    )
    cell_path = root / "receipts/cell-bootstrap.json"
    gateway_path = root / "receipts/gateway-proof.json"
    cell = _private_json_receipt(cell_path, label="backup pending cell receipt")
    if sha256_file(cell_path) != expected_cell_sha:
        raise StagingCellError("backup pending cell receipt changed before resume")
    require_receipt_cluster(cell, cluster)
    if cell.get("owner_id") != owner_id:
        raise StagingCellError("backup pending cell owner changed before resume")
    if cell_active_commit(cell) != release_commit:
        raise StagingCellError("backup pending app release changed before resume")
    _private_json_receipt(gateway_path, label="backup pending Gateway receipt")
    if sha256_file(gateway_path) != expected_gateway_sha:
        raise StagingCellError("backup pending Gateway receipt changed before resume")
    promotion = _exact_cell_promotion(root, cell, release_commit)
    if pending.get("image_promotion") != promotion:
        raise StagingCellError("backup pending promotion evidence changed before resume")


def _resume_backup_creation(
    root: Path,
    args: argparse.Namespace,
    pending: dict[str, Any],
    *,
    resumed: bool,
) -> dict[str, Any]:
    status = pending.get("status")
    if status in {
        "backup-quiesce-pending",
        "backup-app-quiesced-data-stop-pending",
        "backup-archive-creation-pending",
        "backup-created-cluster-delete-pending",
    }:
        _require_backup_pending_release_current(root, pending)
    if status in {
        "backup-quiesce-pending",
        "backup-app-quiesced-data-stop-pending",
        "backup-archive-creation-pending",
    }:
        tools = load_tool_receipt(
            root, required_tools=("kind", "kubectl"), required_artifacts=()
        )["tools"]
        kind = tools["kind"]
        kubectl = tools["kubectl"]
        reference.require_owned_cluster(
            kind,
            args.cluster,
            expected_commit=pending["bootstrap_commit"],
            expected_owner_id=args.owner_id,
        )
        if status == "backup-quiesce-pending":
            _quiesce_backup_app(kubectl)
            baseline = postgres_api_nodes_complete_readback(kubectl)
            persisted = dict(pending)
            persisted.pop("receipt_sha256", None)
            persisted.pop("receipt_path", None)
            pending = {
                **persisted,
                "status": "backup-app-quiesced-data-stop-pending",
                "pre_delete_api_nodes_sha256": baseline["api_nodes_sha256"],
                "pre_delete_api_nodes_count": baseline["api_nodes_count"],
                "pre_delete_api_nodes_pages": baseline["api_nodes_pages"],
                "pre_delete_api_nodes_hash_scope": baseline["api_nodes_hash_scope"],
                "pre_delete_api_nodes_source": baseline["api_nodes_source"],
                "backup_archives": {},
            }
            atomic_json(root / BACKUP_DOWN_RECEIPT, pending)
            status = pending["status"]
        if status == "backup-app-quiesced-data-stop-pending":
            # Reassert the app-side write freeze after any process restart. The
            # baseline was captured only after this freeze became observable.
            _quiesce_backup_app(kubectl)
            _quiesce_retained_data(kubectl)
            pre_delete = _mounted_retained_data_identity(
                kind, args.cluster, root, durable=True, require_split=True
            )
            persisted = dict(pending)
            persisted.pop("receipt_sha256", None)
            persisted.pop("receipt_path", None)
            pending = {
                **persisted,
                "status": "backup-archive-creation-pending",
                "pre_delete_data_identity": pre_delete,
                "backup_archives": {},
            }
            atomic_json(root / BACKUP_DOWN_RECEIPT, pending)
        expected_data = pending.get("pre_delete_data_identity")
        observed_data = _mounted_retained_data_identity(
            kind, args.cluster, root, durable=True, require_split=True
        )
        if observed_data != expected_data:
            raise StagingCellError("staging cold data changed before backup archive creation")

        def record_progress(archives: dict[str, Any]) -> None:
            nonlocal pending
            persisted = dict(pending)
            persisted.pop("receipt_sha256", None)
            persisted.pop("receipt_path", None)
            pending = {**persisted, "backup_archives": archives}
            atomic_json(root / BACKUP_DOWN_RECEIPT, pending)

        archives = _backup_volume_archives(
            kind,
            args.cluster,
            root,
            pending["release_commit"],
            existing_archives=pending.get("backup_archives", {}),
            progress=record_progress,
        )
        after_backup = _mounted_retained_data_identity(
            kind, args.cluster, root, durable=True, require_split=True
        )
        if after_backup != expected_data:
            raise StagingCellError("staging data changed while the cold backup was created")
        persisted = dict(pending)
        persisted.pop("receipt_sha256", None)
        persisted.pop("receipt_path", None)
        pending = {
            **persisted,
            "status": "backup-created-cluster-delete-pending",
            "backup_archives": archives,
        }
        atomic_json(root / BACKUP_DOWN_RECEIPT, pending)
        status = pending["status"]
    if status != "backup-created-cluster-delete-pending":
        raise StagingCellError("backup recovery is not ready for cluster deletion")
    return _complete_backup_down_from_pending(root, args, pending, resumed=resumed)


@lifecycle_mutation_locked
@reference_output_routed
def command_backup_delete_to_prove_down(args: argparse.Namespace) -> dict[str, Any]:
    require_singleton_cluster(args.cluster)
    reference.validate_owner_id(args.owner_id)
    root = state_root(getattr(args, "state_root", None))
    configure_reference_paths(root)
    terminal_path = root / BACKUP_DOWN_RECEIPT
    if terminal_path.exists() or terminal_path.is_symlink():
        existing = _load_backup_down_receipt(root, allow_pending=True)
        if existing.get("owner_id") != args.owner_id:
            raise StagingCellError("backup down receipt owner mismatch")
        if existing.get("cluster") != args.cluster:
            raise StagingCellError("backup down receipt cluster mismatch")
        if existing.get("release_commit") != args.source_commit:
            raise StagingCellError("backup down receipt release mismatch")
        if existing.get("status") == "backup-created-cluster-deleted-primary-data-empty":
            return existing
        controller_commit = require_clean_commit(
            None, require_public_main=False
        )
        if existing.get("controller_commit") != controller_commit:
            raise StagingCellError(
                "backup down retry must use the exact controller commit that created the backup"
            )
        return _resume_backup_creation(root, args, existing, resumed=True)

    cell = load_cell_receipt(root)
    require_receipt_cluster(cell, args.cluster)
    if args.owner_id != cell.get("owner_id"):
        raise StagingCellError("--owner-id does not match the persisted cluster owner")
    release_commit = cell_active_commit(cell)
    if args.source_commit != release_commit:
        raise StagingCellError(
            "backup delete-to-prove down must target the exact active app release"
        )
    if cell.get("status") != "gateway-ready" or cell.get("app_activation") is not True:
        raise StagingCellError("backup delete-to-prove down requires a gateway-ready cell")
    controller_commit = require_clean_commit(None)
    tools = load_tool_receipt(
        root, required_tools=("kind", "kubectl"), required_artifacts=()
    )["tools"]
    kind = tools["kind"]
    kubectl = tools["kubectl"]
    reference.require_owned_cluster(
        kind,
        args.cluster,
        expected_commit=cell["bootstrap_commit"],
        expected_owner_id=args.owner_id,
    )
    require_bootstrap_data_current(kubectl, cell["bootstrap_commit"])
    if app_live_health(kubectl) != {name: "True" for name in APP_DEPLOYMENTS}:
        raise StagingCellError("backup delete-to-prove requires healthy app workloads")
    if not gateway_receipt_current(root, cell, kubectl):
        raise StagingCellError("backup delete-to-prove requires the current Gateway receipt")
    promotion = _exact_cell_promotion(root, cell, release_commit)
    require_gateway_app_current(kubectl, cell, promotion)
    _require_fresh_backup_archive_paths(root, release_commit)
    started_at_unix = int(time.time())
    pending = {
        "schema_version": 1,
        "status": "backup-quiesce-pending",
        "cluster": args.cluster,
        "owner_id": args.owner_id,
        "bootstrap_commit": cell["bootstrap_commit"],
        "release_commit": release_commit,
        "controller_commit": controller_commit,
        "cell_receipt_sha256": sha256_file(root / "receipts/cell-bootstrap.json"),
        "gateway_receipt_sha256": sha256_file(root / "receipts/gateway-proof.json"),
        "previous_delete_to_prove_receipt_sha256": (
            sha256_file(root / DELETE_TO_PROVE_RECEIPT)
            if (root / DELETE_TO_PROVE_RECEIPT).exists()
            else None
        ),
        "image_promotion": promotion,
        "backup_archives": {},
        "started_at_unix": started_at_unix,
        "production_changed": False,
    }
    # Persist authority before scaling anything down or publishing an archive.
    # Every later destructive step can therefore resume from an exact bound state.
    atomic_json(terminal_path, pending)
    return _resume_backup_creation(root, args, pending, resumed=False)


@lifecycle_mutation_locked
@reference_output_routed
def command_backup_delete_to_prove_rebuild(args: argparse.Namespace) -> dict[str, Any]:
    require_singleton_cluster(args.cluster)
    reference.validate_owner_id(args.owner_id)
    root = state_root(getattr(args, "state_root", None))
    configure_reference_paths(root)
    down = _load_backup_down_receipt(root)
    if down.get("owner_id") != args.owner_id or down.get("cluster") != args.cluster:
        raise StagingCellError("backup rebuild owner or cluster binding mismatch")
    if args.source_commit != down.get("release_commit"):
        raise StagingCellError("backup rebuild must restore the exact pre-delete release")
    controller_commit = require_clean_commit(None, require_public_main=False)
    if controller_commit != down.get("controller_commit"):
        raise StagingCellError("backup rebuild controller commit differs from backup creation")
    result_path = root / BACKUP_REBUILD_RECEIPT
    existing: dict[str, Any] | None = None
    terminal_existing = False
    if result_path.exists() or result_path.is_symlink():
        existing = _private_json_receipt(result_path, label="backup rebuild receipt")
        if existing.get("owner_id") != args.owner_id or existing.get("cluster") != args.cluster:
            raise StagingCellError("backup rebuild receipt owner or cluster mismatch")
        if existing.get("release_commit") != down.get("release_commit"):
            raise StagingCellError("backup rebuild receipt release mismatch")
        if existing.get("controller_commit") != controller_commit:
            raise StagingCellError("backup rebuild receipt controller mismatch")
        if existing.get("backup_down_receipt_sha256") != down["receipt_sha256"]:
            raise StagingCellError("backup rebuild receipt is not bound to current backup-down receipt")
        if existing.get("production_changed") is not False:
            raise StagingCellError("backup rebuild receipt lost production isolation")
        terminal_existing = (
            existing.get("status")
            == "backup-restored-infrastructure-ready-app-reactivation-required"
        )
        if not terminal_existing and existing.get("status") not in {
            "backup-restore-pending",
            "backup-data-restored-platform-reconcile-pending",
            "backup-platform-ready-data-reconcile-pending",
        }:
            raise StagingCellError("backup rebuild receipt has unexpected state")

    tool_receipt = load_tool_receipt(root)
    kind = tool_receipt["tools"]["kind"]
    kubectl = tool_receipt["tools"]["kubectl"]
    flux = tool_receipt["tools"]["flux"]
    helm = tool_receipt["tools"]["helm"]
    if terminal_existing:
        reference.require_owned_cluster(
            kind,
            args.cluster,
            expected_commit=down["bootstrap_commit"],
            expected_owner_id=args.owner_id,
        )
        restored_identity = existing.get("restored_data_identity")
        if not isinstance(restored_identity, dict):
            raise StagingCellError("completed backup rebuild lost restored mount identity")
        observed_anchors = _mounted_retained_data_anchors(
            kind, args.cluster, root, require_split=True
        )
        if not _same_data_mount_anchors(restored_identity, observed_anchors):
            raise StagingCellError("completed backup rebuild lost restored mount identity")
        return {
            **existing,
            "receipt_path": str(result_path),
            "receipt_sha256": sha256_file(result_path),
        }
    if args.cluster not in reference.clusters(kind):
        reference.clear_stale_cluster_reservation(
            kind,
            args.cluster,
            expected_commit=down["bootstrap_commit"],
            expected_owner_id=args.owner_id,
        )
        reference.create_kind_cluster(
            kind,
            args.cluster,
            tool_receipt["kubernetes"]["kind_node_image"],
            str(render_kind_config(root)),
            down["bootstrap_commit"],
            args.owner_id,
            timeout=900,
        )
    reference.require_owned_cluster(
        kind,
        args.cluster,
        expected_commit=down["bootstrap_commit"],
        expected_owner_id=args.owner_id,
    )
    prepare_volume_permissions(kind, args.cluster, root)

    if existing is None:
        anchors = _mounted_retained_data_anchors(
            kind, args.cluster, root, require_split=True
        )
        if not _same_data_mount_anchors(down["empty_restore_roots"], anchors):
            raise StagingCellError(
                "backup rebuild empty restore roots are not the proven post-delete roots"
            )
        existing = {
            "schema_version": 1,
            "status": "backup-restore-pending",
            "cluster": args.cluster,
            "owner_id": args.owner_id,
            "bootstrap_commit": down["bootstrap_commit"],
            "release_commit": down["release_commit"],
            "controller_commit": controller_commit,
            "backup_down_receipt_sha256": down["receipt_sha256"],
            "backup_archives": down["backup_archives"],
            "pre_delete_data_identity": down["pre_delete_data_identity"],
            "empty_restore_roots": down["empty_restore_roots"],
            "restore_started_at_unix": int(time.time()),
            "production_changed": False,
        }
        atomic_json(result_path, existing)

    if existing["status"] == "backup-restore-pending":
        observed = _mounted_retained_data_identity(
            kind, args.cluster, root, durable=True, require_split=True
        )
        if not _same_data_mount_anchors(existing["empty_restore_roots"], observed):
            raise StagingCellError(
                "backup restore target identity changed before retry"
            )
        if _same_data_tree_hashes(down["pre_delete_data_identity"], observed):
            restored = observed
        else:
            # A crash can leave a partial extraction. The pending receipt proves
            # these are the fresh post-delete roots and binds the immutable backup,
            # so clearing only their contents is retry-safe and cannot touch the
            # forensic originals.
            data_node = _retained_mount_node(kind, args.cluster, root, require_split=True)
            for name in ("postgres", "nats"):
                volume = f"/var/local/commonthing-staging/{name}"
                run(
                    [
                        "docker",
                        "exec",
                        data_node,
                        "find",
                        volume,
                        "-mindepth",
                        "1",
                        "-delete",
                    ],
                    timeout=120,
                )
                occupied = output(
                    [
                        "docker",
                        "exec",
                        data_node,
                        "find",
                        volume,
                        "-mindepth",
                        "1",
                        "-maxdepth",
                        "1",
                        "-print",
                        "-quit",
                    ],
                    timeout=30,
                )
                if occupied:
                    raise StagingCellError(
                        f"staging {name} restore target could not be reset for retry"
                    )
            restored = _restore_volume_archives(
                kind,
                args.cluster,
                root,
                down["release_commit"],
                down["backup_archives"],
            )
        if not _same_data_tree_hashes(down["pre_delete_data_identity"], restored):
            raise StagingCellError(
                "restored staging data tree hashes differ from the cold backup source"
            )
        existing = {
            **existing,
            "status": "backup-data-restored-platform-reconcile-pending",
            "restored_data_identity": restored,
            "restore_completed_at_unix": int(time.time()),
        }
        atomic_json(result_path, existing)

    if existing["status"] == "backup-data-restored-platform-reconcile-pending":
        restored_now = _mounted_retained_data_identity(
            kind, args.cluster, root, durable=True, require_split=True
        )
        if not _same_data_tree_hashes(
            existing["pre_delete_data_identity"], restored_now
        ):
            raise StagingCellError(
                "restored staging data drifted before platform reconcile"
            )
        if not _same_data_mount_anchors(
            existing["restored_data_identity"], restored_now
        ):
            raise StagingCellError(
                "restored staging mount identity changed before platform reconcile"
            )
        api_server_host = reference.control_plane_address(args.cluster)
        reference.install_platform_components(
            kubectl, flux, helm, tool_receipt["artifacts"], api_server_host
        )
        run(
            [kubectl, "wait", "--for=condition=Ready", "nodes", "--all", "--timeout=5m"],
            timeout=360,
        )
        secret_receipt = inject_external_secrets(kubectl, root)
        registry_material, registry_source_sha = load_registry_pull_material(root)
        registry_receipt = inject_registry_pull_secret(
            kubectl,
            root,
            material=registry_material,
            source_sha=registry_source_sha,
        )
        apply_yaml(
            kubectl,
            flux_documents(down["bootstrap_commit"], suspend_data=True),
        )
        _set_data_reconciliation_suspended(kubectl, suspended=True)
        mounted_before_resume = _mounted_retained_data_identity(
            kind, args.cluster, root, durable=True, require_split=True
        )
        if not _same_data_tree_hashes(
            down["pre_delete_data_identity"], mounted_before_resume
        ):
            raise StagingCellError("backup-restored data changed before workload start")
        if not _same_data_mount_anchors(
            existing["restored_data_identity"], mounted_before_resume
        ):
            raise StagingCellError(
                "backup-restored mount identity changed before workload start"
            )
        existing = {
            **existing,
            "status": "backup-platform-ready-data-reconcile-pending",
            "restored_data_identity": mounted_before_resume,
            "external_secret_source_sha256": secret_receipt["source_sha256"],
            "registry_secret_source_sha256": registry_receipt["source_sha256"],
            "platform_ready_at_unix": int(time.time()),
        }
        atomic_json(result_path, existing)

    if existing["status"] != "backup-platform-ready-data-reconcile-pending":
        raise StagingCellError("backup rebuild did not reach data-reconcile state")
    if _data_reconciliation_is_suspended(kubectl):
        restored_before_resume = _mounted_retained_data_identity(
            kind, args.cluster, root, durable=True, require_split=True
        )
        if not _same_data_tree_hashes(
            down["pre_delete_data_identity"], restored_before_resume
        ):
            raise StagingCellError(
                "backup-restored data changed before workload start"
            )
        if not _same_data_mount_anchors(
            existing["restored_data_identity"], restored_before_resume
        ):
            raise StagingCellError(
                "backup-restored mount identity changed before data reconcile"
            )
    else:
        # A retry can arrive after reconciliation was already resumed but before
        # the terminal receipt was written. At that point workload writes are
        # legitimate, so only the physical restore-root anchors remain stable.
        anchors_before_resume = _mounted_retained_data_anchors(
            kind, args.cluster, root, require_split=True
        )
        if not _same_data_mount_anchors(
            existing["restored_data_identity"], anchors_before_resume
        ):
            raise StagingCellError(
                "backup-restored mount identity changed before data reconcile"
            )
    _set_data_reconciliation_suspended(kubectl, suspended=False)
    reconcile_data(kubectl, down["bootstrap_commit"])
    live_workloads = staging_live_health(kubectl)
    unhealthy = {
        name: state for name, state in live_workloads.items() if state != "True"
    }
    if unhealthy:
        raise StagingCellError(
            f"backup-restored staging infrastructure is not live: {unhealthy!r}"
        )
    anchors_after_resume = _mounted_retained_data_anchors(
        kind, args.cluster, root, require_split=True
    )
    if not _same_data_mount_anchors(
        existing["restored_data_identity"], anchors_after_resume
    ):
        raise StagingCellError("backup-restored mount identity changed during data reconcile")
    result = {
        **existing,
        "status": "backup-restored-infrastructure-ready-app-reactivation-required",
        "live_workloads": live_workloads,
        "completed_at_unix": int(time.time()),
    }
    atomic_json(result_path, result)
    return {**result, "receipt_path": str(result_path), "receipt_sha256": sha256_file(result_path)}


def _backup_recovery_controller_commit(
    root: Path, cell: dict[str, Any], release_commit: str
) -> str | None:
    path = root / BACKUP_REBUILD_RECEIPT
    if not (path.exists() or path.is_symlink()):
        return None
    receipt = _private_json_receipt(path, label="backup rebuild receipt")
    if (
        receipt.get("status")
        != "backup-restored-infrastructure-ready-app-reactivation-required"
        or receipt.get("cluster") != cell.get("cluster")
        or receipt.get("owner_id") != cell.get("owner_id")
        or receipt.get("bootstrap_commit") != cell.get("bootstrap_commit")
        or receipt.get("release_commit") != release_commit
    ):
        return None
    controller_commit = str(receipt.get("controller_commit") or "")
    observed = require_clean_commit(None, require_public_main=False)
    if observed != controller_commit:
        raise StagingCellError(
            "backup recovery must continue from the exact controller commit that restored data"
        )
    return controller_commit


def _completed_backup_recovery_activation_result(
    root: Path,
    cell: dict[str, Any],
    release_commit: str,
    controller_commit: str | None,
) -> dict[str, Any] | None:
    consumed = cell.get("backup_recovery_reactivation_consumed")
    if consumed is None:
        return None
    if not isinstance(consumed, dict):
        raise StagingCellError(
            "backup recovery reactivation consumption marker is malformed"
        )
    if cell_active_commit(cell) != release_commit:
        return None
    if controller_commit is None:
        raise StagingCellError(
            "completed backup recovery activation lost its controller binding"
        )
    binding = {
        "release_commit": release_commit,
        "controller_commit": controller_commit,
        "rebuild_receipt_sha256": sha256_file(root / BACKUP_REBUILD_RECEIPT),
    }
    if consumed != binding:
        raise StagingCellError(
            "completed backup recovery activation binding differs from the recovery receipt"
        )
    if cell.get("status") not in {"app-ready-gateway-pending", "gateway-ready"}:
        return None
    expected_fields = {
        "gitops_source_commit": release_commit,
        "data_source_commit": cell.get("bootstrap_commit"),
        "app_source_commit": release_commit,
        "app_activation": True,
    }
    for key, value in expected_fields.items():
        if cell.get(key) != value:
            raise StagingCellError(
                f"completed backup recovery activation has drifted terminal field: {key}"
            )
    if any(
        key in cell
        for key in (
            "pending_active_commit",
            "pending_image_promotion",
            "pending_migration",
            "pending_registry_pull_secret",
            "pending_backup_recovery_reactivation",
        )
    ):
        raise StagingCellError(
            "completed backup recovery activation still contains pending activation state"
        )
    _exact_cell_promotion(root, cell, release_commit)
    return {
        **cell,
        "receipt_path": str(root / "receipts/cell-bootstrap.json"),
    }


def _backup_recovery_activation_binding(
    root: Path,
    cell: dict[str, Any],
    release_commit: str,
    controller_commit: str | None,
) -> dict[str, Any] | None:
    pending = cell.get("pending_backup_recovery_reactivation")
    activation_in_progress = cell.get("status") == "app-activation-in-progress"
    if controller_commit is None:
        if activation_in_progress and pending is not None:
            raise StagingCellError(
                "activation recovery lost its backup recovery controller binding"
            )
        return None
    rebuild_path = root / BACKUP_REBUILD_RECEIPT
    binding = {
        "release_commit": release_commit,
        "controller_commit": controller_commit,
        "rebuild_receipt_sha256": sha256_file(rebuild_path),
    }
    if activation_in_progress:
        if pending != binding:
            raise StagingCellError(
                "activation recovery lost its backup recovery reactivation binding"
            )
        return binding
    consumed = cell.get("backup_recovery_reactivation_consumed")
    if consumed is not None and not isinstance(consumed, dict):
        raise StagingCellError(
            "backup recovery reactivation consumption marker is malformed"
        )
    if cell_active_commit(cell) != release_commit:
        return None
    if consumed == binding:
        return None
    return binding


def _validated_existing_backup_delete_to_prove_receipt(
    root: Path,
    *,
    cluster: str,
    owner_id: str,
    release_commit: str,
    controller_commit: str,
    down: dict[str, Any],
    rebuild: dict[str, Any],
) -> dict[str, Any] | None:
    path = root / BACKUP_DELETE_TO_PROVE_RECEIPT
    if not (path.exists() or path.is_symlink()):
        return None
    receipt = _private_json_receipt(path, label="backup delete-to-prove receipt")
    if len(controller_commit) != 40 or any(
        ch not in "0123456789abcdef" for ch in controller_commit
    ):
        raise StagingCellError(
            "backup rebuild receipt controller is not a canonical 40-hex commit"
        )
    expected = {
        "schema_version": 1,
        "status": "backup-delete-to-prove-verified",
        "cluster": cluster,
        "owner_id": owner_id,
        "bootstrap_commit": down["bootstrap_commit"],
        "active_commit": release_commit,
        "controller_commit": controller_commit,
        "backup_down_receipt_sha256": down["receipt_sha256"],
        "backup_rebuild_receipt_sha256": sha256_file(root / BACKUP_REBUILD_RECEIPT),
        "pre_delete_data_identity": down["pre_delete_data_identity"],
        "restored_data_identity": rebuild["restored_data_identity"],
        "pre_delete_api_nodes_sha256": down["pre_delete_api_nodes_sha256"],
        "post_restore_api_nodes_sha256": down["pre_delete_api_nodes_sha256"],
        "pre_delete_api_nodes_pages": down["pre_delete_api_nodes_pages"],
        "post_restore_api_nodes_pages": down["pre_delete_api_nodes_pages"],
        "api_nodes_count": down["pre_delete_api_nodes_count"],
        "api_nodes_hash_scope": down["pre_delete_api_nodes_hash_scope"],
        "api_nodes_consistency": API_NODES_DB_HTTP_CONSISTENCY,
        "postgres_api_nodes_sha256": down["pre_delete_api_nodes_sha256"],
        "postgres_api_nodes_count": down["pre_delete_api_nodes_count"],
        "postgres_api_nodes_pages": down["pre_delete_api_nodes_pages"],
        "postgres_api_nodes_hash_scope": down["pre_delete_api_nodes_hash_scope"],
        "postgres_api_nodes_source": "quiesced-postgres-api-projection-v1",
        "app_workloads": {name: "True" for name in APP_DEPLOYMENTS},
        "live_workloads": {name: "True" for name in LIVE_DEPLOYMENTS},
        "rpo_observation": {
            "confirmed_mutations_lost": 0,
            "boundary": "quiesced-cold-backup-snapshot",
        },
        "production_changed": False,
        "does_not_establish": ["public DNS", "public TLS", "production cutover"],
    }
    for key, value in expected.items():
        if receipt.get(key) != value:
            raise StagingCellError(
                f"existing backup delete-to-prove receipt has different binding: {key}"
            )
    final_anchors = receipt.get("final_data_mount_anchors")
    if not isinstance(final_anchors, dict) or not _same_data_mount_anchors(
        rebuild["restored_data_identity"], final_anchors
    ):
        raise StagingCellError(
            "existing backup delete-to-prove receipt lost its restored mount binding"
        )
    # Gateway and host-Gateway receipts are live runtime receipts. Their exact
    # hashes remain part of the historical terminal proof, but a later normal
    # activation is allowed to retire/replace those mutable files. Requiring
    # today's runtime receipts here would turn historical evidence into a live
    # monitor and make a legitimate later release invalidate an already proven
    # recovery cycle.
    for key, label in (
        ("gateway_receipt_sha256", "Gateway proof receipt hash"),
        ("host_gateway_receipt_sha256", "host Gateway proof receipt hash"),
    ):
        _canonical_sha256(receipt.get(key), label=label)
    recovery_start = down.get("cluster_deleted_at_unix")
    verified_at = receipt.get("verified_at_unix")
    rto = receipt.get("rto_observed_seconds")
    if (
        not isinstance(recovery_start, int)
        or isinstance(recovery_start, bool)
        or recovery_start <= 0
        or not isinstance(verified_at, int)
        or isinstance(verified_at, bool)
        or verified_at < recovery_start
        or not isinstance(rto, int)
        or isinstance(rto, bool)
        or rto != verified_at - recovery_start
    ):
        raise StagingCellError("existing backup proof has invalid recovery timing")
    return {
        **receipt,
        "receipt_path": str(path),
        "receipt_sha256": sha256_file(path),
    }


@lifecycle_mutation_locked
@reference_output_routed
def command_prove_backup_delete_to_prove(args: argparse.Namespace) -> dict[str, Any]:
    require_singleton_cluster(args.cluster)
    reference.validate_owner_id(args.owner_id)
    root = state_root(getattr(args, "state_root", None))
    configure_reference_paths(root)
    release_commit = str(args.source_commit or "")
    down = _load_backup_down_receipt(root)
    if down.get("cluster") != args.cluster or down.get("owner_id") != args.owner_id:
        raise StagingCellError("backup proof owner or cluster mismatch")
    if down.get("release_commit") != release_commit:
        raise StagingCellError("backup proof release differs from the historical backup cycle")
    rebuild = _load_completed_backup_rebuild_receipt(root, down)
    rebuild_path = root / BACKUP_REBUILD_RECEIPT
    rebuild_controller_commit = str(rebuild.get("controller_commit") or "")
    completed = _validated_existing_backup_delete_to_prove_receipt(
        root,
        cluster=args.cluster,
        owner_id=args.owner_id,
        release_commit=release_commit,
        controller_commit=rebuild_controller_commit,
        down=down,
        rebuild=rebuild,
    )
    if completed is not None:
        return completed
    cell = load_cell_receipt(root)
    require_receipt_cluster(cell, args.cluster)
    if cell.get("owner_id") != args.owner_id:
        raise StagingCellError("backup proof owner mismatch")
    if cell_active_commit(cell) != release_commit:
        raise StagingCellError("backup proof requires the restored release to be active")
    path = root / BACKUP_DELETE_TO_PROVE_RECEIPT
    controller_commit = require_clean_commit(None, require_public_main=False)
    if controller_commit != rebuild_controller_commit:
        raise StagingCellError(
            "backup proof controller commit differs from the rebuild-bound controller"
        )
    tools = load_tool_receipt(
        root, required_tools=("kind", "kubectl"), required_artifacts=()
    )["tools"]
    kubectl = tools["kubectl"]
    reference.require_owned_cluster(
        tools["kind"],
        args.cluster,
        expected_commit=cell["bootstrap_commit"],
        expected_owner_id=args.owner_id,
    )
    require_bootstrap_data_current(kubectl, cell["bootstrap_commit"])
    promotion = _exact_cell_promotion(root, cell, release_commit)
    require_gateway_app_current(kubectl, cell, promotion)
    workloads = app_live_health(kubectl)
    if workloads != {name: "True" for name in APP_DEPLOYMENTS}:
        raise StagingCellError("backup proof requires healthy restored app workloads")
    if not gateway_receipt_current(root, cell, kubectl):
        raise StagingCellError("backup proof requires a current Gateway receipt")
    if not host_gateway_receipt_current(root, cell, kubectl):
        raise StagingCellError("backup proof requires a current host-external Gateway readback")
    final_data_anchors = _mounted_retained_data_anchors(
        tools["kind"], args.cluster, root, require_split=True
    )
    if not _same_data_mount_anchors(
        rebuild["restored_data_identity"], final_data_anchors
    ):
        raise StagingCellError(
            "restored data mount identity changed after workload reactivation"
        )
    live_workloads = staging_live_health(kubectl)
    if any(state != "True" for state in live_workloads.values()):
        raise StagingCellError(
            f"backup proof requires healthy restored data and Flux workloads: {live_workloads!r}"
        )
    host_path = root / HOST_GATEWAY_RECEIPT
    host_receipt = _private_json_receipt(
        host_path, label="host Gateway proof receipt"
    )
    with _postgres_domain_nodes_write_freeze(kubectl):
        fresh_host = host_gateway_http_readback()
        fresh_api_nodes_consistency = _bind_locked_api_nodes_http_to_postgres(
            kubectl, fresh_host, label="final host Gateway API snapshot"
        )
        require_gateway_app_current(kubectl, cell, promotion)
        if not gateway_receipt_current(root, cell, kubectl):
            raise StagingCellError("staging Gateway changed during final host readback")
        if not host_gateway_receipt_current(root, cell, kubectl):
            raise StagingCellError("host Gateway binding changed during final host readback")
        for key in (
            "probe_scope",
            "endpoint",
            "health_sha256",
            "web_prefix_sha256",
            "api_nodes_sha256",
            "api_nodes_count",
            "api_nodes_pages",
            "api_nodes_hash_scope",
        ):
            if host_receipt.get(key) != fresh_host.get(key):
                raise StagingCellError(
                    f"host Gateway readback changed before final backup proof: {key}"
                )
        for key, value in fresh_api_nodes_consistency.items():
            if host_receipt.get(key) != value:
                raise StagingCellError(
                    f"host Gateway PostgreSQL binding changed before final backup proof: {key}"
                )
        if (
            fresh_host.get("api_nodes_sha256") != down.get("pre_delete_api_nodes_sha256")
            or fresh_host.get("api_nodes_count") != down.get("pre_delete_api_nodes_count")
            or fresh_host.get("api_nodes_pages") != down.get("pre_delete_api_nodes_pages")
            or fresh_host.get("api_nodes_hash_scope")
            != down.get("pre_delete_api_nodes_hash_scope")
        ):
            raise StagingCellError(
                "restored PostgreSQL-backed API data differs from the pre-delete full snapshot"
            )
        refreshed_data_anchors = _mounted_retained_data_anchors(
            tools["kind"], args.cluster, root, require_split=True
        )
        if not _same_data_mount_anchors(
            rebuild["restored_data_identity"], refreshed_data_anchors
        ):
            raise StagingCellError(
                "restored data mount identity changed during final host readback"
            )
        final_data_anchors = refreshed_data_anchors
        live_workloads = staging_live_health(kubectl)
        if any(state != "True" for state in live_workloads.values()):
            raise StagingCellError(
                "restored data or Flux workload changed during final host readback: "
                f"{live_workloads!r}"
            )
        observed_at_unix = int(time.time())
    recovery_start = int(down.get("cluster_deleted_at_unix") or 0)
    if recovery_start <= 0 or observed_at_unix < recovery_start:
        raise StagingCellError("backup proof recovery timing evidence is invalid")
    verified_at_unix = observed_at_unix
    rto_observed_seconds = observed_at_unix - recovery_start
    result = {
        "schema_version": 1,
        "status": "backup-delete-to-prove-verified",
        "cluster": args.cluster,
        "owner_id": args.owner_id,
        "bootstrap_commit": cell["bootstrap_commit"],
        "active_commit": release_commit,
        "controller_commit": controller_commit,
        "backup_down_receipt_sha256": down["receipt_sha256"],
        "backup_rebuild_receipt_sha256": sha256_file(rebuild_path),
        "gateway_receipt_sha256": sha256_file(root / "receipts/gateway-proof.json"),
        "host_gateway_receipt_sha256": sha256_file(root / HOST_GATEWAY_RECEIPT),
        "pre_delete_data_identity": down["pre_delete_data_identity"],
        "restored_data_identity": rebuild["restored_data_identity"],
        "final_data_mount_anchors": final_data_anchors,
        "pre_delete_api_nodes_sha256": down["pre_delete_api_nodes_sha256"],
        "post_restore_api_nodes_sha256": fresh_host["api_nodes_sha256"],
        "pre_delete_api_nodes_pages": down["pre_delete_api_nodes_pages"],
        "post_restore_api_nodes_pages": fresh_host["api_nodes_pages"],
        "api_nodes_count": fresh_host["api_nodes_count"],
        "api_nodes_hash_scope": fresh_host["api_nodes_hash_scope"],
        **fresh_api_nodes_consistency,
        "app_workloads": workloads,
        "live_workloads": live_workloads,
        "rto_observed_seconds": rto_observed_seconds,
        "rpo_observation": {
            "confirmed_mutations_lost": 0,
            "boundary": "quiesced-cold-backup-snapshot",
        },
        "verified_at_unix": verified_at_unix,
        "production_changed": False,
        "does_not_establish": ["public DNS", "public TLS", "production cutover"],
    }
    atomic_json(path, result)
    return {**result, "receipt_path": str(path), "receipt_sha256": sha256_file(path)}


@lifecycle_mutation_locked
@reference_output_routed
def command_prove_delete_to_prove(args: argparse.Namespace) -> dict[str, Any]:
    require_singleton_cluster(args.cluster)
    reference.validate_owner_id(args.owner_id)
    root = state_root(getattr(args, "state_root", None))
    configure_reference_paths(root)
    tool_receipt = load_tool_receipt(
        root, required_tools=("kind", "kubectl"), required_artifacts=()
    )
    cell = load_cell_receipt(root)
    require_receipt_cluster(cell, args.cluster)
    owner_id = str(cell.get("owner_id") or "")
    bootstrap_commit = str(cell.get("bootstrap_commit") or "")
    if args.owner_id != owner_id:
        raise StagingCellError("--owner-id does not match the persisted cluster owner")
    active_commit = cell_active_commit(cell)
    implementation_commit = require_clean_commit(args.source_commit)
    if implementation_commit != active_commit:
        raise StagingCellError("delete-to-prove proof must run from the active app commit")
    if cell.get("app_activation") is not True or cell.get("status") != "gateway-ready":
        raise StagingCellError("delete-to-prove proof requires a gateway-ready activated cell")

    kind = tool_receipt["tools"]["kind"]
    kubectl = tool_receipt["tools"]["kubectl"]
    reference.require_owned_cluster(
        kind,
        args.cluster,
        expected_commit=bootstrap_commit,
        expected_owner_id=owner_id,
    )
    require_bootstrap_data_current(kubectl, bootstrap_commit)
    workloads = app_live_health(kubectl)
    if any(state != "True" for state in workloads.values()):
        raise StagingCellError("delete-to-prove proof requires live API and Web workloads")
    if not gateway_receipt_current(root, cell, kubectl):
        raise StagingCellError("delete-to-prove proof requires the current live gateway receipt")

    down = load_delete_to_prove_down_receipt(
        root,
        cell,
        require_current_cell_match=False,
        require_retained_data_match=False,
    )
    rebuild_path = root / CELL_REBUILD_RECEIPT
    rebuild = _private_json_receipt(rebuild_path, label="staging rebuild receipt")
    expected_rebuild = _rebuild_receipt_binding(cell, down, implementation_commit)
    _require_receipt_binding(rebuild, expected_rebuild, label="staging rebuild receipt")
    if rebuild.get("status") != "infrastructure-rebuilt-app-reactivation-required":
        raise StagingCellError("delete-to-prove proof requires a completed infrastructure rebuild")
    if int(rebuild.get("completed_at_unix") or 0) < int(down.get("completed_at_unix") or 0):
        raise StagingCellError("staging rebuild receipt predates the down receipt")

    gateway_path = root / "receipts/gateway-proof.json"
    gateway_sha = sha256_file(gateway_path)
    current_cell_sha = sha256_file(root / "receipts/cell-bootstrap.json")
    if gateway_sha == down["gateway_proof_receipt_sha256"]:
        raise StagingCellError("gateway proof identity did not change across delete-to-prove")
    if current_cell_sha == down["cell_receipt_sha256"]:
        raise StagingCellError("cell receipt identity did not change across delete-to-prove")
    result = {
        "schema_version": 1,
        "status": "delete-to-prove-verified",
        "cluster": args.cluster,
        "owner_id": owner_id,
        "bootstrap_commit": bootstrap_commit,
        "active_commit": active_commit,
        "implementation_commit": implementation_commit,
        "pre_delete_cell_receipt_sha256": down["cell_receipt_sha256"],
        "post_rebuild_cell_receipt_sha256": current_cell_sha,
        "pre_delete_gateway_proof_receipt_sha256": down["gateway_proof_receipt_sha256"],
        "post_rebuild_gateway_proof_receipt_sha256": gateway_sha,
        "down_receipt_sha256": down["receipt_sha256"],
        "rebuild_receipt_sha256": sha256_file(rebuild_path),
        "app_workloads": workloads,
        "verified_at_unix": int(time.time()),
        "production_changed": False,
        "does_not_establish": ["DNS", "TLS", "external-LB", "production cutover"],
    }
    path = root / DELETE_TO_PROVE_RECEIPT
    if path.exists() or path.is_symlink():
        previous = _private_json_receipt(path, label="delete-to-prove receipt")
        stable = {key: value for key, value in result.items() if key != "verified_at_unix"}
        previous_stable = {key: value for key, value in previous.items() if key != "verified_at_unix"}
        if previous_stable != stable:
            raise StagingCellError("existing delete-to-prove receipt has different proof bindings")
        return {**previous, "receipt_path": str(path), "receipt_sha256": sha256_file(path)}
    atomic_json(path, result)
    return {**result, "receipt_path": str(path), "receipt_sha256": sha256_file(path)}


def command_self_check() -> dict[str, Any]:
    require_singleton_cluster(DEFAULT_CLUSTER)
    try:
        require_singleton_cluster(f"{DEFAULT_CLUSTER}-other")
    except StagingCellError:
        pass
    else:
        raise StagingCellError(
            "self-check accepted a second cluster over singleton persistent state"
        )

    if state_root(None) != DEFAULT_STATE_ROOT.resolve():
        raise StagingCellError("self-check default state root drift")
    try:
        state_root(str(DEFAULT_STATE_ROOT.parent / "unexpected-root"))
    except StagingCellError:
        pass
    else:
        raise StagingCellError("self-check accepted a non-canonical state root")

    public_secret = public_external_secret_state()
    if public_secret != {"bound": True, "required_keys": ["database-url"]}:
        raise StagingCellError(
            "self-check public external-secret state exposes unexpected fields"
        )

    commit = "a" * 40
    if not (
        flux_revision_matches_commit(commit, commit)
        and flux_revision_matches_commit(f"sha1:{commit}", commit)
        and flux_revision_matches_commit(f"main@sha1:{commit}", commit)
        and not flux_revision_matches_commit(f"sha1:{'b' * 40}", commit)
    ):
        raise StagingCellError("self-check Flux revision binding is invalid")

    with tempfile.TemporaryDirectory(
        prefix="commonthing-staging-cell-self-check-"
    ) as tmp_name:
        root = Path(tmp_name)
        (root / "data/postgres").mkdir(parents=True)
        material, source_sha = load_or_create_secret_material(root)
        secret_path = root / "secrets/staging-runtime.json"
        if not secret_path.is_file() or stat.S_IMODE(secret_path.stat().st_mode) != 0o600:
            raise StagingCellError("self-check secret source permissions are invalid")
        if len(source_sha) != 64 or not material.get("database_password"):
            raise StagingCellError("self-check secret source binding is invalid")

        annotations = {SECRET_SOURCE_ANNOTATION: source_sha}
        database_document = {
            "metadata": {
                "name": DATABASE_SECRET,
                "namespace": DATA_NAMESPACE,
                "annotations": annotations,
            },
            "data": {
                key: base64.b64encode(value.encode("utf-8")).decode("ascii")
                for key, value in {
                    "username": material["database_user"],
                    "password": material["database_password"],
                    "database": material["database_name"],
                }.items()
            },
        }
        if not secret_document_matches(
            database_document,
            name=DATABASE_SECRET,
            namespace_name=DATA_NAMESPACE,
            source_sha=source_sha,
            expected_values={
                "username": material["database_user"],
                "password": material["database_password"],
                "database": material["database_name"],
            },
        ):
            raise StagingCellError(
                "self-check rejected a valid injected database Secret"
            )
        database_document["data"]["password"] = base64.b64encode(b"wrong").decode(
            "ascii"
        )
        if secret_document_matches(
            database_document,
            name=DATABASE_SECRET,
            namespace_name=DATA_NAMESPACE,
            source_sha=source_sha,
            expected_values={
                "username": material["database_user"],
                "password": material["database_password"],
                "database": material["database_name"],
            },
        ):
            raise StagingCellError("self-check accepted changed injected Secret data")

        atomic_json(
            root / "receipts/cell-bootstrap.json",
            {
                "schema_version": 1,
                "cluster": DEFAULT_CLUSTER,
                "external_secret": {"source_sha256": source_sha},
            },
        )
        changed = dict(material)
        changed["schema_version"] = 1
        changed["database_password"] = "replacement-password"
        atomic_json(secret_path, changed)
        try:
            load_or_create_secret_material(root)
        except StagingCellError as error:
            if "differs from the bootstrap receipt" not in str(error):
                raise
        else:
            raise StagingCellError(
                "self-check accepted credential rotation over retained PostgreSQL state"
            )

        atomic_json(
            secret_path,
            {"schema_version": 1, **material},
        )
        secret_path.unlink()
        (root / "data/postgres/PG_VERSION").write_text("16\n", encoding="utf-8")
        try:
            load_or_create_secret_material(root)
        except StagingCellError as error:
            if "retained PostgreSQL state exists" not in str(error):
                raise
        else:
            raise StagingCellError(
                "self-check regenerated credentials over retained PostgreSQL state"
            )

        rendered_path = render_kind_config(root)
        rendered = yaml.safe_load(rendered_path.read_text(encoding="utf-8"))
        expected = [
            str((root / "data/postgres").resolve()),
            str((root / "data/nats").resolve()),
        ]
        observed = [
            mount.get("hostPath")
            for node in rendered.get("nodes", [])
            for mount in node.get("extraMounts", [])
        ]
        if observed != expected:
            raise StagingCellError(
                "self-check rendered kind data-worker mounts do not bind the exact retained roots"
            )
    return {
        "schema_version": 1,
        "status": "pass",
        "checks": [
            "singleton-cluster",
            "fixed-state-root",
            "flux-source-exact-commit",
            "retained-secret-fail-closed",
            "retained-secret-rotation-fail-closed",
            "injected-secret-integrity",
            "public-secret-output-redaction",
            "split-data-worker-kind-render",
        ],
    }


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Persistent owner-bound T084 staging GewebeZelle controller"
    )
    sub = p.add_subparsers(dest="command", required=True)
    up = sub.add_parser("up")
    up.set_defaults(cluster=DEFAULT_CLUSTER)
    up.add_argument("--owner-id", required=True)
    up.add_argument("--source-commit")
    activate = sub.add_parser("activate")
    activate.set_defaults(cluster=DEFAULT_CLUSTER)
    activate.add_argument("--owner-id", required=True)
    activate.add_argument("--source-commit", required=True)
    gateway = sub.add_parser("prove-gateway")
    gateway.set_defaults(cluster=DEFAULT_CLUSTER)
    gateway.add_argument("--owner-id", required=True)
    gateway.add_argument("--source-commit", required=True)
    host_gateway = sub.add_parser("prove-host-gateway")
    host_gateway.set_defaults(cluster=DEFAULT_CLUSTER)
    host_gateway.add_argument("--owner-id", required=True)
    host_gateway.add_argument("--source-commit", required=True)
    backup_down = sub.add_parser("backup-delete-to-prove-down")
    backup_down.set_defaults(cluster=DEFAULT_CLUSTER)
    backup_down.add_argument("--owner-id", required=True)
    backup_down.add_argument("--source-commit", required=True)
    backup_rebuild = sub.add_parser("backup-delete-to-prove-rebuild")
    backup_rebuild.set_defaults(cluster=DEFAULT_CLUSTER)
    backup_rebuild.add_argument("--owner-id", required=True)
    backup_rebuild.add_argument("--source-commit", required=True)
    backup_proof = sub.add_parser("prove-backup-delete-to-prove")
    backup_proof.set_defaults(cluster=DEFAULT_CLUSTER)
    backup_proof.add_argument("--owner-id", required=True)
    backup_proof.add_argument("--source-commit", required=True)
    status = sub.add_parser("status")
    status.set_defaults(cluster=DEFAULT_CLUSTER)
    down = sub.add_parser("down")
    down.set_defaults(cluster=DEFAULT_CLUSTER)
    down.add_argument("--owner-id", required=True)
    rebuild = sub.add_parser("rebuild")
    rebuild.set_defaults(cluster=DEFAULT_CLUSTER)
    rebuild.add_argument("--owner-id", required=True)
    rebuild.add_argument("--source-commit", required=True)
    delete_to_prove = sub.add_parser("prove-delete-to-prove")
    delete_to_prove.set_defaults(cluster=DEFAULT_CLUSTER)
    delete_to_prove.add_argument("--owner-id", required=True)
    delete_to_prove.add_argument("--source-commit", required=True)
    migrate = sub.add_parser("migrate-legacy-state")
    migrate.set_defaults(cluster=DEFAULT_CLUSTER)
    migrate.add_argument("--owner-id", required=True)
    sub.add_parser("self-check")
    return p


def emit_public_success(command: str, result: dict[str, Any]) -> None:
    if command == "up":
        print(
            '{"command":"up","schema_version":1,'
            '"status":"infrastructure-ready-image-promotion-blocked"}'
        )
        return
    if command in {"activate", "prove-gateway"}:
        safe = {
            "command": command,
            "schema_version": 1,
            "status": str(result.get("status") or "degraded"),
            "cluster": str(result.get("cluster") or DEFAULT_CLUSTER),
            "bootstrap_commit": str(result.get("bootstrap_commit") or ""),
            "active_commit": str(result.get("active_commit") or ""),
            "app_activation": bool(result.get("app_activation")),
            "production_changed": bool(result.get("production_changed")),
        }
        if command == "prove-gateway":
            safe["does_not_establish"] = GATEWAY_LIMITS
        print(json.dumps(safe, ensure_ascii=False, sort_keys=True))
        return
    if command in {
        "prove-host-gateway",
        "backup-delete-to-prove-down",
        "backup-delete-to-prove-rebuild",
        "prove-backup-delete-to-prove",
    }:
        safe = {
            "command": command,
            "schema_version": 1,
            "status": str(result.get("status") or "degraded"),
            "cluster": str(result.get("cluster") or DEFAULT_CLUSTER),
            "bootstrap_commit": str(result.get("bootstrap_commit") or ""),
            "active_commit": str(
                result.get("active_commit") or result.get("release_commit") or ""
            ),
            "production_changed": bool(result.get("production_changed")),
        }
        if command == "prove-backup-delete-to-prove":
            safe["rto_observed_seconds"] = int(result.get("rto_observed_seconds") or 0)
            safe["rpo_observation"] = result.get("rpo_observation")
            safe["does_not_establish"] = result.get("does_not_establish", [])
        if command == "prove-host-gateway":
            safe["does_not_establish"] = result.get("does_not_establish", [])
        print(json.dumps(safe, ensure_ascii=False, sort_keys=True))
        return
    if command == "status":
        status = str(result.get("status") or "degraded")
        safe: dict[str, Any] = {
            "command": "status",
            "schema_version": 1,
            "status": status,
            "cluster": str(result.get("cluster") or DEFAULT_CLUSTER),
        }
        if status in {"ready", "degraded"}:
            pvcs = result.get("pvcs") if isinstance(result.get("pvcs"), dict) else {}
            external = (
                result.get("external_secret")
                if isinstance(result.get("external_secret"), dict)
                else {}
            )
            live = (
                result.get("live_workloads")
                if isinstance(result.get("live_workloads"), dict)
                else {}
            )
            safe.update(
                {
                    "bootstrap_commit": str(result.get("bootstrap_commit") or ""),
                    "active_commit": str(result.get("active_commit") or ""),
                    "source_revision": str(result.get("source_revision") or ""),
                    "source_matches_commit": bool(result.get("source_matches_commit")),
                    "source_ready": result.get("source_ready") == "True",
                    "source_ready_status": str(result.get("source_ready") or "missing"),
                    "data_ready": result.get("data_ready") == "True",
                    "data_ready_status": str(result.get("data_ready") or "missing"),
                    "data_revision": str(result.get("data_revision") or ""),
                    "data_matches_commit": bool(result.get("data_matches_commit")),
                    "pvcs": {
                        "postgres-data": str(pvcs.get("postgres-data") or "missing"),
                        "nats-data": str(pvcs.get("nats-data") or "missing"),
                    },
                    "external_secret": {
                        "database": bool(external.get("database")),
                        "runtime": bool(external.get("runtime")),
                        "ready": bool(external.get("ready")),
                    },
                    "live_workloads": {
                        name: str(live.get(name) or "unchecked")
                        for name in LIVE_DEPLOYMENTS
                    },
                    "activation_in_progress": bool(
                        result.get("activation_in_progress")
                    ),
                    "pending_active_commit": str(
                        result.get("pending_active_commit") or ""
                    ),
                    "gateway_ready": bool(result.get("gateway_ready")),
                    "does_not_establish": GATEWAY_LIMITS,
                    "app_activation": bool(result.get("app_activation")),
                    "registry_pull_secret_ready": bool(
                        result.get("registry_pull_secret_ready")
                    ),
                    "app_source_matches_commit": bool(
                        result.get("app_source_matches_commit")
                    ),
                    "app_kustomization_matches_commit": bool(
                        result.get("app_kustomization_matches_commit")
                    ),
                    "app_workloads": {
                        name: str(
                            (result.get("app_workloads") or {}).get(name) or "unchecked"
                        )
                        for name in APP_DEPLOYMENTS
                    },
                }
            )
        elif result.get("bootstrap_commit"):
            safe["bootstrap_commit"] = str(result.get("bootstrap_commit") or "")
        print(json.dumps(safe, ensure_ascii=False, sort_keys=True))
        return
    if command == "down":
        safe = {
            "command": "down",
            "schema_version": 1,
            "status": str(
                result.get("status") or "cluster-deleted-state-preserved"
            ),
            "cluster": str(result.get("cluster") or DEFAULT_CLUSTER),
        }
        print(json.dumps(safe, ensure_ascii=False, sort_keys=True))
        return
    if command in {"rebuild", "prove-delete-to-prove"}:
        safe = {
            "command": command,
            "schema_version": 1,
            "status": str(result.get("status") or "degraded"),
            "cluster": str(result.get("cluster") or DEFAULT_CLUSTER),
            "bootstrap_commit": str(result.get("bootstrap_commit") or ""),
            "active_commit": str(result.get("active_commit") or ""),
            "production_changed": bool(result.get("production_changed")),
        }
        if command == "prove-delete-to-prove":
            safe["does_not_establish"] = ["DNS", "TLS", "external-LB", "production cutover"]
        print(json.dumps(safe, ensure_ascii=False, sort_keys=True))
        return
    if command == "migrate-legacy-state":
        safe = {
            "command": "migrate-legacy-state",
            "schema_version": 1,
            "status": str(result.get("status") or "legacy-state-adopted"),
            "cluster": str(result.get("cluster") or DEFAULT_CLUSTER),
            "toolchain_regeneration_required": bool(
                result.get("toolchain_regeneration_required")
            ),
            "production_changed": bool(result.get("production_changed")),
        }
        print(json.dumps(safe, ensure_ascii=False, sort_keys=True))
        return
    print('{"command":"self-check","schema_version":1,"status":"pass"}')


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "up":
            result = command_up(args)
        elif args.command == "activate":
            result = command_activate(args)
        elif args.command == "prove-gateway":
            result = command_prove_gateway(args)
        elif args.command == "prove-host-gateway":
            result = command_prove_host_gateway(args)
        elif args.command == "backup-delete-to-prove-down":
            result = command_backup_delete_to_prove_down(args)
        elif args.command == "backup-delete-to-prove-rebuild":
            result = command_backup_delete_to_prove_rebuild(args)
        elif args.command == "prove-backup-delete-to-prove":
            result = command_prove_backup_delete_to_prove(args)
        elif args.command == "status":
            result = command_status(args)
        elif args.command == "down":
            result = command_down(args)
        elif args.command == "rebuild":
            result = command_rebuild(args)
        elif args.command == "prove-delete-to-prove":
            result = command_prove_delete_to_prove(args)
        elif args.command == "migrate-legacy-state":
            result = command_migrate_legacy_state(args)
        else:
            result = command_self_check()
        emit_public_success(args.command, result)
        return 0
    except StagingCellError as error:
        print(f"staging cell failed: {error}", file=sys.stderr)
        return 1
    except reference.ProofError:
        print("staging cell failed: platform proof operation failed", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as error:
        print(
            f"staging cell failed: external command exited with status {error.returncode}",
            file=sys.stderr,
        )
        return 1
    except subprocess.TimeoutExpired:
        print("staging cell failed: external command timed out", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
