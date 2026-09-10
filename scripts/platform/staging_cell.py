#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import errno
import fcntl
import hashlib
import hmac
import json
import os
import secrets
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
DEFAULT_STATE_ROOT = Path.home() / ".local/state/weltgewebe/staging-cell"
DEFAULT_CLUSTER = "weltgewebe-staging"
SOURCE_NAME = "weltgewebe-staging-source"
APP_SOURCE_NAME = "weltgewebe-staging-app-source"
DATA_KUSTOMIZATION = "weltgewebe-staging-data"
APP_KUSTOMIZATION = "weltgewebe-staging-app"
MIGRATION_JOB_PREFIX = "weltgewebe-staging-migration"
MIGRATION_TEMPLATE = ROOT / "platform/apps/weltgewebe/migration/ha/job.yaml"
MIGRATION_TIMEOUT_SECONDS = 8 * 60
DATA_NAMESPACE = "weltgewebe-data"
APP_NAMESPACE = "weltgewebe-staging"
DATABASE_SECRET = "weltgewebe-staging-database"
RUNTIME_SECRET = "weltgewebe-runtime"
REGISTRY_SECRET = "weltgewebe-staging-registry"
GHCR_REGISTRY = "ghcr.io"
LIVE_DEPLOYMENTS = {
    "postgres": (DATA_NAMESPACE, "postgres"),
    "nats": (DATA_NAMESPACE, "nats"),
    "source-controller": ("flux-system", "source-controller"),
    "kustomize-controller": ("flux-system", "kustomize-controller"),
}
APP_DEPLOYMENTS = {
    "api": (APP_NAMESPACE, "weltgewebe-api"),
    "web": (APP_NAMESPACE, "weltgewebe-web"),
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
    timeout: int | None = None,
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
        **kwargs,
    )


def output(argv: list[str], *, timeout: int | None = None) -> str:
    return run(argv, capture=True, timeout=timeout).stdout.strip()




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
    data_root = str((root / "data").resolve())
    placeholder = "__COMMONTHING_STAGING_DATA_ROOT__"
    for index, node in enumerate(nodes):
        mounts = node.get("extraMounts", []) if isinstance(node, dict) else []
        if index != 1:
            if mounts:
                raise StagingCellError(
                    f"staging kind node {index} must not mount persistent data"
                )
            continue
        if not isinstance(mounts, list) or len(mounts) != 1:
            raise StagingCellError(
                "staging data worker must bind exactly one persistent host mount"
            )
        mount = mounts[0]
        if mount.get("hostPath") != placeholder:
            raise StagingCellError("staging data worker hostPath template drift")
        if mount.get("containerPath") != "/var/local/weltgewebe-staging":
            raise StagingCellError("staging data worker containerPath drift")
        if mount.get("readOnly") is not False:
            raise StagingCellError("staging data worker persistent mount must be writable")
        mount["hostPath"] = data_root
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
        field_manager="weltgewebe-staging-registry",
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
        field_manager="weltgewebe-staging-secrets",
    )
    return {"source_sha256": source_sha, "required_keys": ["database-url"]}


def public_external_secret_state() -> dict[str, Any]:
    return {"bound": True, "required_keys": ["database-url"]}


def prepare_volume_permissions(kind: str, cluster: str, root: Path) -> None:
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

    expected_source = str((root / "data").resolve())
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
        data_mounts = [
            mount
            for mount in mounts
            if isinstance(mount, dict)
            and mount.get("Destination") == "/var/local/weltgewebe-staging"
        ]
        if node == data_node:
            if (
                len(data_mounts) != 1
                or data_mounts[0].get("Source") != expected_source
                or data_mounts[0].get("RW") is not True
            ):
                raise StagingCellError(
                    "staging data worker does not expose the exact writable retained host mount"
                )
        elif data_mounts:
            raise StagingCellError(
                f"staging non-data node {node!r} unexpectedly exposes retained host storage"
            )

    for volume_path, identity in (
        ("/var/local/weltgewebe-staging/postgres", "999:999"),
        ("/var/local/weltgewebe-staging/nats", "1000:1000"),
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


def flux_documents(commit: str) -> list[dict[str, Any]]:
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
            "spec": {
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
            },
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
    labels["app.kubernetes.io/name"] = "weltgewebe-api"
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
    return document


def run_staging_migration(
    kubectl: str, commit: str, promotion: dict[str, Any]
) -> dict[str, Any]:
    plan = migration_plan(commit, promotion)
    document = migration_job_document(commit, promotion)
    apply_yaml_server_side(
        kubectl,
        document,
        field_manager="weltgewebe-staging-migration",
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
    observed = json.loads(
        output(
            [
                kubectl,
                "-n",
                APP_NAMESPACE,
                "get",
                "job",
                plan["job_name"],
                "-o",
                "json",
            ]
        )
    )
    metadata = observed.get("metadata") if isinstance(observed, dict) else None
    annotations = metadata.get("annotations") if isinstance(metadata, dict) else None
    pod_spec = (
        observed.get("spec", {}).get("template", {}).get("spec", {})
        if isinstance(observed, dict)
        else {}
    )
    containers = pod_spec.get("containers") if isinstance(pod_spec, dict) else None
    conditions = observed.get("status", {}).get("conditions", []) if isinstance(observed, dict) else []
    complete = any(
        isinstance(condition, dict)
        and condition.get("type") == "Complete"
        and condition.get("status") == "True"
        for condition in conditions
    )
    if (
        not isinstance(annotations, dict)
        or annotations.get("commonthing.net/source-commit") != commit
        or annotations.get("commonthing.net/promotion-receipt-sha256") != plan["receipt_sha256"]
        or not isinstance(containers, list)
        or len(containers) != 1
        or not isinstance(containers[0], dict)
        or containers[0].get("image") != plan["api_image"]
        or not complete
    ):
        raise StagingCellError("staging migration Job readback does not match the promoted release")
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
    patches = []
    for label, deployment in (("api", "weltgewebe-api"), ("web", "weltgewebe-web")):
        image = images.get(label)
        if not isinstance(image, str) or "@sha256:" not in image:
            raise StagingCellError(f"verified {label} image is not digest-bound")
        patches.append({
            "target": {"kind": "Deployment", "name": deployment},
            "patch": yaml.safe_dump(
                [
                    {
                        "op": "replace",
                        "path": "/spec/template/spec/containers/0/image",
                        "value": image,
                    },
                    {
                        "op": "add",
                        "path": "/spec/template/spec/imagePullSecrets",
                        "value": [{"name": REGISTRY_SECRET}],
                    },
                ],
                sort_keys=False,
            ),
        })
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
            "path": "./platform/apps/weltgewebe/overlays/staging",
            "patches": patches,
            "healthChecks": [
                {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "name": deployment,
                    "namespace": APP_NAMESPACE,
                }
                for deployment in ("weltgewebe-api", "weltgewebe-web")
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
    if existing and cell is None:
        raise StagingCellError(
            "staging cluster exists without a bootstrap receipt; refusing unbound recovery"
        )
    if cell is None and retained_staging_data_exists(root):
        raise StagingCellError(
            "retained staging data exists without a bootstrap receipt; explicit recovery is required"
        )
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
            write_cell_receipt(
                root,
                {
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
                },
            )
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
        commit = require_clean_commit(
            args.source_commit,
            require_public_main=False,
        )
    else:
        commit = require_clean_commit(args.source_commit)
        promotion = load_promotion_receipt(root, commit)
        migration_plan_value = migration_plan(commit, promotion)

    registry_material, registry_source_sha = load_registry_pull_material(root)
    registry_pull_access = verify_ghcr_pull_access(registry_material, promotion)
    pending_config_sha256 = sha256_bytes(
        registry_dockerconfig_json(registry_material).encode("utf-8")
    )

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

    if not activation_in_progress:
        pending_state = {
            **cell,
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
            "production_changed": False,
        }
        write_cell_receipt(root, pending_state)

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
        for key, value in cell.items()
        if key
        not in {
            "pending_active_commit",
            "pending_image_promotion",
            "pending_migration",
            "pending_registry_pull_secret",
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
        "migration": migration_receipt,
        "image_promotion": promotion_state,
        "app_activation": True,
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
    promotion_state = (
        owner.get("image_promotion")
        if activated and isinstance(owner.get("image_promotion"), dict)
        else image_promotion_state()
    )
    return {
        "schema_version": 1,
        "status": "ready" if ready else "degraded",
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


@lifecycle_mutation_locked
@reference_output_routed
def command_down(args: argparse.Namespace) -> dict[str, Any]:
    require_singleton_cluster(args.cluster)
    reference.validate_owner_id(args.owner_id)
    root = state_root(getattr(args, "state_root", None))
    configure_reference_paths(root)
    receipt = load_tool_receipt(
        root, required_tools=("kind",), required_artifacts=()
    )
    cell = load_cell_receipt(root)
    require_receipt_cluster(cell, args.cluster)
    commit = str(cell.get("bootstrap_commit") or "")
    owner_id = str(cell.get("owner_id") or "")
    if args.owner_id != owner_id:
        raise StagingCellError("--owner-id does not match the persisted cluster owner")
    reference.validate_ownership_binding(commit, owner_id)
    kind = receipt["tools"]["kind"]
    cluster_present = args.cluster in reference.clusters(kind)
    reference.delete_owned_cluster_if_present(
        kind,
        args.cluster,
        expected_commit=commit,
        expected_owner_id=owner_id,
    )
    result = {
        "schema_version": 1,
        "status": (
            "cluster-deleted-state-preserved"
            if cluster_present
            else "cluster-absent-state-preserved"
        ),
        "cluster": args.cluster,
        "owner_id": owner_id,
        "bootstrap_commit": commit,
        "state_preserved": ["data", "secrets", "toolchain", "receipts"],
        "production_changed": False,
    }
    path = root / "receipts/cell-down.json"
    atomic_json(path, result)
    return {
        **result,
        "receipt_path": str(path),
        "receipt_sha256": sha256_file(path),
    }


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
        expected = str((root / "data").resolve())
        observed = [
            mount.get("hostPath")
            for node in rendered.get("nodes", [])
            for mount in node.get("extraMounts", [])
        ]
        if observed != [expected]:
            raise StagingCellError(
                "self-check rendered kind data-worker mount does not bind the state root"
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
            "single-data-worker-kind-render",
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
    status = sub.add_parser("status")
    status.set_defaults(cluster=DEFAULT_CLUSTER)
    down = sub.add_parser("down")
    down.set_defaults(cluster=DEFAULT_CLUSTER)
    down.add_argument("--owner-id", required=True)
    sub.add_parser("self-check")
    return p


def emit_public_success(command: str, result: dict[str, Any]) -> None:
    if command == "up":
        print(
            '{"command":"up","schema_version":1,'
            '"status":"infrastructure-ready-image-promotion-blocked"}'
        )
        return
    if command == "activate":
        safe = {
            "command": "activate",
            "schema_version": 1,
            "status": str(result.get("status") or "degraded"),
            "cluster": str(result.get("cluster") or DEFAULT_CLUSTER),
            "bootstrap_commit": str(result.get("bootstrap_commit") or ""),
            "active_commit": str(result.get("active_commit") or ""),
            "app_activation": bool(result.get("app_activation")),
            "production_changed": bool(result.get("production_changed")),
        }
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
    print('{"command":"self-check","schema_version":1,"status":"pass"}')


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "up":
            result = command_up(args)
        elif args.command == "activate":
            result = command_activate(args)
        elif args.command == "status":
            result = command_status(args)
        elif args.command == "down":
            result = command_down(args)
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
