#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / ".cache/weltgewebe-platform"
OCI_MIRROR_STATE = CACHE / "oci-mirror"
OCI_MIRROR_LOCK = ROOT / "platform/oci-proof-mirror.lock.json"
OCI_STRICT_ENV = "WELTGEWEBE_PROOF_OCI_STRICT"
OCI_DOCKERFILE_IMAGES = {
    Path("apps/api/Dockerfile"): ("build_rust", "build_debian"),
    Path("apps/web/Dockerfile"): ("build_node", "build_caddy"),
}
_CONTROLLED_OCI_RUNTIME_REFS: dict[str, str] = {}
_CONTROLLED_OCI_RUNTIME_DIGESTS: dict[str, str] = {}
_CONTROLLED_OCI_RUNTIME_IMAGE_IDS: dict[str, str] = {}
MARKERS = CACHE / "clusters"
KUBECONFIGS = CACHE / "kubeconfigs"
DEFAULT_CLUSTER = "weltgewebe-reference"
FULL_GIT_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
KIND_CREATE_ATTEMPTS = 3
KIND_CREATE_BACKOFF_SECONDS = (5.0, 15.0)
TRANSIENT_KIND_CREATE_MARKERS = (
    "429 too many requests",
    "status code: 429",
    "toomanyrequests",
    "rate limit",
    "tls handshake timeout",
    "i/o timeout",
    "connection reset by peer",
    "temporary failure",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
)
GATEWAY_API_ARTIFACTS = (
    "gateway_api_gatewayclasses",
    "gateway_api_gateways",
    "gateway_api_httproutes",
    "gateway_api_referencegrants",
    "gateway_api_grpcroutes",
)


class ProofError(RuntimeError):
    pass


def run(
    argv: list[str],
    *,
    input_text: str | None = None,
    capture: bool = False,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(argv), flush=True)
    return subprocess.run(
        argv,
        cwd=ROOT,
        input=input_text,
        text=True,
        check=True,
        capture_output=capture,
        timeout=timeout,
    )


def output(argv: list[str]) -> str:
    return run(argv, capture=True).stdout.strip()


def control_plane_address(cluster: str) -> str:
    raw = json.loads(output(["docker", "inspect", f"{cluster}-control-plane"]))
    try:
        networks = raw[0]["NetworkSettings"]["Networks"]
    except (IndexError, KeyError, TypeError) as exc:
        raise ProofError("kind control-plane network metadata is missing") from exc
    preferred = networks.get("kind", {}).get("IPAddress")
    candidates = [
        details.get("IPAddress")
        for details in networks.values()
        if details.get("IPAddress")
    ]
    address = preferred or (candidates[0] if candidates else "")
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError as exc:
        raise ProofError("kind control-plane IPv4 address is missing or invalid") from exc
    if parsed.version != 4:
        raise ProofError("kind control-plane address must be IPv4")
    return str(parsed)


def tool_receipt() -> dict[str, Any]:
    raw = output(
        [
            sys.executable,
            "scripts/platform/bootstrap_tools.py",
            "--json",
            "--tool",
            "kind",
            "--tool",
            "kubectl",
            "--tool",
            "kustomize",
            "--tool",
            "flux",
            "--tool",
            "helm",
            "--tool",
            "kubectl_cnpg",
        ]
    )
    return json.loads(raw)


def _enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}


def controlled_oci_strict() -> bool:
    return _enabled(OCI_STRICT_ENV)


def _normalize_oci_reference(reference: str) -> str:
    name, separator, digest = reference.partition("@")
    first = name.split("/", 1)[0]
    if "/" not in name:
        canonical = f"docker.io/library/{name}"
    elif first == "localhost" or "." in first or ":" in first:
        canonical = name
    else:
        canonical = f"docker.io/{name}"
    return canonical + (separator + digest if separator else "")


def _full_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None
    )


def _oci_mirror_lock() -> dict[str, Any]:
    try:
        payload = json.loads(OCI_MIRROR_LOCK.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProofError(f"controlled OCI mirror lock is unreadable: {error}") from error
    if payload.get("schema_version") != 1 or not isinstance(payload.get("images"), dict):
        raise ProofError("controlled OCI mirror lock is invalid")
    return payload


def prepare_controlled_oci_host(suite: str) -> dict[str, Any]:
    raw = output(
        [
            sys.executable,
            "scripts/platform/oci_proof_mirror.py",
            "--state",
            str(OCI_MIRROR_STATE),
            "verify-host",
            "--suite",
            suite,
            "--suite",
            "app-build",
        ]
    )
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ProofError("controlled OCI offline host verification receipt is malformed") from error
    if result.get("status") != "pass" or result.get("strict") is not True:
        raise ProofError(f"controlled OCI offline host verification did not pass strictly: {result}")
    lock = _oci_mirror_lock()
    result["kind_node_image"] = lock["images"]["kind_node"]["local_ref"]
    return result


def _validate_controlled_oci_kind_receipt(
    result: dict[str, Any], suite: str, cluster: str
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    if result.get("status") != "pass" or result.get("strict") is not True:
        raise ProofError(f"controlled OCI kind load did not pass strictly: {result}")
    if result.get("cluster") != cluster:
        raise ProofError("controlled OCI kind-load receipt cluster binding drift")
    lock = _oci_mirror_lock()
    expected = {
        name: spec
        for name, spec in lock["images"].items()
        if suite in spec.get("suites", []) and spec.get("load_into_kind") is True
    }
    images = result.get("images")
    if not isinstance(images, dict) or set(images) != set(expected):
        raise ProofError("controlled OCI kind-load receipt image inventory drift")
    if result.get("loaded_count") != len(expected):
        raise ProofError("controlled OCI kind-load receipt count drift")
    blockades = result.get("registry_blockades")
    if not isinstance(blockades, list) or not blockades:
        raise ProofError("controlled OCI kind-load receipt lacks node registry blockade")
    blockade_nodes = {
        item.get("node") for item in blockades if isinstance(item, dict)
    }
    if None in blockade_nodes or len(blockade_nodes) != len(blockades):
        raise ProofError("controlled OCI kind-load blockade node inventory is invalid")

    runtime_refs: dict[str, str] = {}
    runtime_digests: dict[str, str] = {}
    runtime_image_ids: dict[str, str] = {}
    image_nodes: set[str] | None = None
    for name, spec in expected.items():
        observed = images[name]
        if not isinstance(observed, dict):
            raise ProofError(f"controlled OCI image receipt is invalid: {name}")
        canonical = spec.get("canonical")
        local_ref = spec.get("local_ref")
        locked_digest = spec.get("digest")
        runtime_ref = observed.get("runtime_ref")
        platform_digest = observed.get("platform_target_digest")
        image_id = observed.get("cri_image_id")
        expected_runtime = _normalize_oci_reference(str(local_ref))
        if (
            observed.get("canonical") != canonical
            or observed.get("local_ref") != local_ref
            or observed.get("locked_index_digest") != locked_digest
            or runtime_ref != expected_runtime
            or not _full_sha256(platform_digest)
            or not _full_sha256(image_id)
            or observed.get("image_id") != image_id
            or not isinstance(canonical, str)
            or not isinstance(local_ref, str)
            or not isinstance(locked_digest, str)
        ):
            raise ProofError(f"controlled OCI CRI image binding drift: {name}")
        nodes = observed.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            raise ProofError(f"controlled OCI node image bindings are missing: {name}")
        current_nodes: set[str] = set()
        for node in nodes:
            if not isinstance(node, dict) or not isinstance(node.get("node"), str):
                raise ProofError(f"controlled OCI node image binding is invalid: {name}")
            current_nodes.add(node["node"])
            repo_tags = node.get("repo_tags")
            repo_digests = node.get("repo_digests")
            selected_repo_digest = node.get("selected_repo_digest")
            containerd_target_digest = node.get("containerd_target_digest")
            platform_evidence_kind = node.get("platform_evidence_kind")
            canonical_name = _normalize_oci_reference(canonical).rsplit("@", 1)[0]
            allowed_repo_digests = {
                f"{expected_runtime}@{platform_digest}",
                f"{canonical_name}@{platform_digest}",
            }
            import_digest = (
                isinstance(selected_repo_digest, str)
                and re.fullmatch(
                    r"docker\.io/library/import-[0-9]{4}-[0-9]{2}-[0-9]{2}@"
                    r"sha256:[0-9a-f]{64}",
                    selected_repo_digest,
                )
                is not None
                and selected_repo_digest.endswith(f"@{platform_digest}")
            )
            repo_digest_evidence = (
                platform_evidence_kind == "cri_repo_digest"
                and isinstance(repo_digests, list)
                and selected_repo_digest in repo_digests
                and (
                    selected_repo_digest in allowed_repo_digests
                    or import_digest
                )
                and containerd_target_digest is None
            )
            containerd_target_evidence = (
                platform_evidence_kind == "containerd_target_digest"
                and repo_digests == []
                and selected_repo_digest is None
                and containerd_target_digest == platform_digest
            )
            if (
                node.get("cri_image_status_verified") is not True
                or node.get("runtime_ref") != expected_runtime
                or node.get("image_id") != image_id
                or node.get("locked_index_digest") != locked_digest
                or node.get("platform_target_digest") != platform_digest
                or not isinstance(repo_tags, list)
                or expected_runtime not in repo_tags
                or not isinstance(repo_digests, list)
                or not (repo_digest_evidence or containerd_target_evidence)
            ):
                raise ProofError(f"controlled OCI node CRI binding drift: {name}")
        if image_nodes is None:
            image_nodes = current_nodes
        elif current_nodes != image_nodes:
            raise ProofError("controlled OCI image node inventories disagree")
        previous_digest = runtime_digests.setdefault(
            expected_runtime, platform_digest
        )
        if previous_digest != platform_digest:
            raise ProofError("controlled OCI runtime tag has conflicting digests")
        previous_image_id = runtime_image_ids.setdefault(expected_runtime, image_id)
        if previous_image_id != image_id:
            raise ProofError("controlled OCI runtime tag has conflicting image IDs")
        for source_ref in (
            canonical,
            f"{local_ref}@{locked_digest}",
            local_ref,
        ):
            runtime_refs[_normalize_oci_reference(source_ref)] = expected_runtime
    if image_nodes != blockade_nodes:
        raise ProofError("controlled OCI image and blockade node inventories disagree")
    return runtime_refs, runtime_digests, runtime_image_ids


def load_controlled_oci_into_kind(kind: str, cluster: str, suite: str) -> dict[str, Any]:
    raw = output(
        [
            sys.executable,
            "scripts/platform/oci_proof_mirror.py",
            "--state",
            str(OCI_MIRROR_STATE),
            "load-kind",
            "--suite",
            suite,
            "--kind",
            kind,
            "--cluster",
            cluster,
        ]
    )
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ProofError("controlled OCI kind-load receipt is malformed") from error
    runtime_refs, runtime_digests, runtime_image_ids = (
        _validate_controlled_oci_kind_receipt(result, suite, cluster)
    )
    global _CONTROLLED_OCI_RUNTIME_REFS
    global _CONTROLLED_OCI_RUNTIME_DIGESTS
    global _CONTROLLED_OCI_RUNTIME_IMAGE_IDS
    _CONTROLLED_OCI_RUNTIME_REFS = runtime_refs
    _CONTROLLED_OCI_RUNTIME_DIGESTS = runtime_digests
    _CONTROLLED_OCI_RUNTIME_IMAGE_IDS = runtime_image_ids
    return result


def controlled_oci_runtime_image(reference: str) -> str:
    if not controlled_oci_strict():
        return reference
    normalized = _normalize_oci_reference(reference)
    runtime = _CONTROLLED_OCI_RUNTIME_REFS.get(normalized)
    if runtime is not None:
        return runtime
    lock = _oci_mirror_lock()
    locked_refs = {
        _normalize_oci_reference(source_ref)
        for spec in lock["images"].values()
        if spec.get("load_into_kind") is True
        for source_ref in (
            str(spec.get("canonical", "")),
            f"{spec.get('local_ref', '')}@{spec.get('digest', '')}",
        )
    }
    if normalized in locked_refs:
        raise ProofError(
            f"controlled OCI runtime tag is unavailable for locked image: {reference}"
        )
    return reference


def controlled_oci_runtime_digest(reference: str) -> str:
    if not controlled_oci_strict():
        digest = reference.rsplit("@", 1)[-1] if "@" in reference else ""
        if not _full_sha256(digest):
            raise ProofError(
                f"non-strict OCI reference is not digest-bound: {reference}"
            )
        return digest
    runtime_ref = controlled_oci_runtime_image(reference)
    digest = _CONTROLLED_OCI_RUNTIME_DIGESTS.get(runtime_ref)
    if not _full_sha256(digest):
        raise ProofError(
            f"controlled OCI runtime digest is unavailable for {reference}"
        )
    return digest


def controlled_oci_runtime_identity_digests(reference: str) -> set[str]:
    if not controlled_oci_strict():
        return {controlled_oci_runtime_digest(reference)}
    runtime_ref = controlled_oci_runtime_image(reference)
    identities = {
        value
        for value in (
            _CONTROLLED_OCI_RUNTIME_DIGESTS.get(runtime_ref),
            _CONTROLLED_OCI_RUNTIME_IMAGE_IDS.get(runtime_ref),
        )
        if _full_sha256(value)
    }
    if not identities:
        raise ProofError(
            f"controlled OCI runtime identities are unavailable for {reference}"
        )
    return identities


def require_host_tools() -> None:
    missing = [name for name in ("docker",) if shutil.which(name) is None]
    if missing:
        raise ProofError(f"missing host tools: {', '.join(missing)}")
    run(["docker", "info"], capture=True, timeout=30)


def marker_path(name: str) -> Path:
    return MARKERS / f"{name}.json"


def ownership_lock_path(name: str) -> Path:
    return MARKERS / f".{name}.ownership.lock"


def kubeconfig_path(name: str) -> Path:
    return KUBECONFIGS / f"{name}.yaml"


def generate_owner_id(prefix: str) -> str:
    return f"{prefix}-{os.getpid()}-{secrets.token_hex(8)}"


def validate_owner_id(owner_id: str) -> None:
    if (
        not owner_id
        or len(owner_id) > 128
        or re.fullmatch(r"[A-Za-z0-9._:@-]+", owner_id) is None
    ):
        raise ProofError("cluster ownership requires a stable owner id")


def validate_ownership_binding(commit: str, owner_id: str) -> None:
    if not FULL_GIT_OBJECT_ID.fullmatch(commit):
        raise ProofError("cluster ownership requires a full lowercase Git object id")
    validate_owner_id(owner_id)


def repository_common_dir(root: Path = ROOT) -> str:
    try:
        candidate = root.resolve(strict=True)
    except OSError as error:
        raise ProofError(f"repository path is unavailable: {root}") from error
    control = candidate / ".git"
    if control.is_symlink():
        raise ProofError(f"repository Git control path is a symlink: {control}")
    if control.is_dir():
        common = control
    elif control.is_file():
        try:
            raw = control.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as error:
            raise ProofError(f"repository Git control file is unreadable: {control}") from error
        prefix = "gitdir: "
        if not raw.startswith(prefix) or not raw[len(prefix):].strip():
            raise ProofError(f"repository Git control file is malformed: {control}")
        gitdir = Path(raw[len(prefix):].strip())
        if not gitdir.is_absolute():
            gitdir = candidate / gitdir
        try:
            gitdir = gitdir.resolve(strict=True)
        except OSError as error:
            raise ProofError(f"repository Git directory is unavailable: {gitdir}") from error
        commondir_file = gitdir / "commondir"
        if commondir_file.is_symlink():
            raise ProofError(f"repository Git commondir is a symlink: {commondir_file}")
        if commondir_file.is_file():
            try:
                raw_common = commondir_file.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError) as error:
                raise ProofError(
                    f"repository Git commondir is unreadable: {commondir_file}"
                ) from error
            if not raw_common:
                raise ProofError(f"repository Git commondir is empty: {commondir_file}")
            common = Path(raw_common)
            if not common.is_absolute():
                common = gitdir / common
        else:
            common = gitdir
    else:
        raise ProofError(f"repository Git control path is missing: {control}")
    try:
        resolved = common.resolve(strict=True)
    except OSError as error:
        raise ProofError(
            f"Git common directory for repository {candidate} is unavailable"
        ) from error
    if not resolved.is_dir():
        raise ProofError(
            f"Git common directory for repository {candidate} is not a directory"
        )
    return str(resolved)


def repository_binding_matches(observed: Any) -> bool:
    if not isinstance(observed, str) or not observed:
        return False
    expected = repository_common_dir(ROOT)
    if observed == expected:
        return True
    candidate = Path(observed)
    if not candidate.is_absolute() or not candidate.exists():
        return False
    try:
        return repository_common_dir(candidate) == expected
    except ProofError:
        return False


@contextmanager
def cluster_ownership_lock(name: str):
    MARKERS.mkdir(parents=True, exist_ok=True)
    path = ownership_lock_path(name)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_marker(name: str) -> dict[str, Any]:
    marker = marker_path(name)
    if marker.is_symlink() or not marker.is_file():
        raise ProofError(f"cluster {name!r} has no regular ownership marker")
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProofError(f"cluster {name!r} ownership marker is unreadable: {error}") from error
    if not isinstance(data, dict):
        raise ProofError(f"cluster {name!r} ownership marker is not an object")
    return data


def _require_marker_binding(
    data: dict[str, Any],
    name: str,
    *,
    expected_commit: str,
    expected_owner_id: str,
) -> None:
    validate_ownership_binding(expected_commit, expected_owner_id)
    expected = {
        "schema_version": 2,
        "cluster": name,
        "commit": expected_commit,
        "owner_id": expected_owner_id,
    }
    mismatched = {
        key: {"expected": value, "observed": data.get(key)}
        for key, value in expected.items()
        if data.get(key) != value
    }
    observed_repository = data.get("repository")
    expected_repository = repository_common_dir(ROOT)
    if not repository_binding_matches(observed_repository):
        mismatched["repository"] = {
            "expected": expected_repository,
            "observed": observed_repository,
        }
    if mismatched:
        raise ProofError(
            f"cluster {name!r} ownership marker does not match exact owner binding: "
            f"{json.dumps(mismatched, sort_keys=True)}"
        )


def configure_cluster_access(kind: str, name: str) -> Path:
    KUBECONFIGS.mkdir(parents=True, exist_ok=True)
    path = kubeconfig_path(name)
    run([kind, "export", "kubeconfig", "--name", name, "--kubeconfig", str(path)])
    os.environ["KUBECONFIG"] = str(path)
    return path


def require_owned_cluster(
    kind: str,
    name: str,
    *,
    expected_commit: str,
    expected_owner_id: str,
) -> dict[str, Any]:
    with cluster_ownership_lock(name):
        if name not in clusters(kind):
            raise ProofError(f"owned cluster {name!r} is absent")
        data = _read_marker(name)
        _require_marker_binding(
            data,
            name,
            expected_commit=expected_commit,
            expected_owner_id=expected_owner_id,
        )
    configure_cluster_access(kind, name)
    return data


def normalize_owned_cluster_repository(
    kind: str,
    name: str,
    *,
    expected_commit: str,
    expected_owner_id: str,
) -> dict[str, Any]:
    with cluster_ownership_lock(name):
        if name not in clusters(kind):
            raise ProofError(f"owned cluster {name!r} is absent")
        data = _read_marker(name)
        _require_marker_binding(
            data,
            name,
            expected_commit=expected_commit,
            expected_owner_id=expected_owner_id,
        )
        stable_repository = repository_common_dir(ROOT)
        if data.get("repository") != stable_repository:
            data = {**data, "repository": stable_repository}
            write_json_atomic(marker_path(name), data)
    configure_cluster_access(kind, name)
    return data


def clusters(kind: str) -> set[str]:
    result = run([kind, "get", "clusters"], capture=True)
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def assert_available_cluster_name(kind: str, name: str) -> None:
    with cluster_ownership_lock(name):
        if name in clusters(kind):
            raise ProofError(
                f"cluster {name!r} already exists; it is never adopted or deleted by this proof"
            )
        if os.path.lexists(marker_path(name)):
            raise ProofError(
                f"cluster {name!r} has a stale ownership marker; "
                "explicit exact-owner cleanup is required"
            )


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _write_marker_locked(name: str, commit: str, owner_id: str) -> None:
    validate_ownership_binding(commit, owner_id)
    MARKERS.mkdir(parents=True, exist_ok=True)
    marker = marker_path(name)
    temporary = MARKERS / f".{name}.{os.getpid()}.{secrets.token_hex(8)}.marker.tmp"
    payload = {
        "schema_version": 2,
        "cluster": name,
        "repository": repository_common_dir(ROOT),
        "commit": commit,
        "owner_id": owner_id,
        "pid": os.getpid(),
    }
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, marker)
        except FileExistsError as error:
            raise ProofError(
                f"cluster {name!r} ownership marker already exists"
            ) from error
        _fsync_directory(MARKERS)
    finally:
        temporary.unlink(missing_ok=True)


def write_marker(name: str, commit: str, owner_id: str) -> None:
    with cluster_ownership_lock(name):
        if os.path.lexists(marker_path(name)):
            raise ProofError(f"cluster {name!r} ownership marker already exists")
        _write_marker_locked(name, commit, owner_id)


def reserve_cluster_name(kind: str, name: str, commit: str, owner_id: str) -> None:
    validate_ownership_binding(commit, owner_id)
    with cluster_ownership_lock(name):
        if name in clusters(kind):
            raise ProofError(
                f"cluster {name!r} already exists; it is never adopted or deleted by this proof"
            )
        if os.path.lexists(marker_path(name)):
            raise ProofError(
                f"cluster {name!r} has a stale ownership marker; "
                "explicit exact-owner cleanup is required"
            )
        _write_marker_locked(name, commit, owner_id)


@contextmanager
def cluster_creation_reservation(
    kind: str, name: str, commit: str, owner_id: str
):
    """Reserve one cluster name until creation either succeeds or is rolled back.

    The per-cluster lock intentionally spans marker publication and ``kind create``.
    This prevents an exact-owner reconciliation from deleting the marker in the
    gap between reservation and cluster creation. Persistent lock files are kept
    because unlinking flock files can create inode races between waiters.
    """
    validate_ownership_binding(commit, owner_id)
    with cluster_ownership_lock(name):
        if name in clusters(kind):
            raise ProofError(
                f"cluster {name!r} already exists; it is never adopted or deleted by this proof"
            )
        if os.path.lexists(marker_path(name)):
            raise ProofError(
                f"cluster {name!r} has a stale ownership marker; "
                "explicit exact-owner cleanup is required"
            )
        _write_marker_locked(name, commit, owner_id)
        try:
            yield
        except Exception:
            rollback_error: Exception | None = None
            try:
                if name in clusters(kind):
                    run([kind, "delete", "cluster", "--name", name])
            except Exception as error:
                rollback_error = error
            if rollback_error is None:
                _remove_cluster_state_files_durably(name, marker_missing_ok=True)
            else:
                print(
                    f"cluster {name!r} creation rollback incomplete; "
                    f"preserving ownership marker: {rollback_error}",
                    file=sys.stderr,
                )
            raise


def _remove_cluster_state_files_durably(
    name: str, *, marker_missing_ok: bool = False
) -> None:
    marker = marker_path(name)
    marker.unlink(missing_ok=marker_missing_ok)
    if marker.parent.is_dir():
        _fsync_directory(marker.parent)

    kubeconfig = kubeconfig_path(name)
    kubeconfig.unlink(missing_ok=True)
    if kubeconfig.parent.is_dir():
        _fsync_directory(kubeconfig.parent)


def _delete_owned_cluster_locked(
    kind: str,
    name: str,
    *,
    expected_commit: str,
    expected_owner_id: str,
) -> None:
    data = _read_marker(name)
    _require_marker_binding(
        data,
        name,
        expected_commit=expected_commit,
        expected_owner_id=expected_owner_id,
    )
    if name in clusters(kind):
        run([kind, "delete", "cluster", "--name", name])
    _remove_cluster_state_files_durably(name)


def delete_owned_cluster(
    kind: str,
    name: str,
    *,
    expected_commit: str,
    expected_owner_id: str,
) -> None:
    with cluster_ownership_lock(name):
        _delete_owned_cluster_locked(
            kind,
            name,
            expected_commit=expected_commit,
            expected_owner_id=expected_owner_id,
        )


def delete_owned_cluster_if_present(
    kind: str,
    name: str,
    *,
    expected_commit: str,
    expected_owner_id: str,
) -> bool:
    with cluster_ownership_lock(name):
        if os.path.lexists(marker_path(name)):
            _delete_owned_cluster_locked(
                kind,
                name,
                expected_commit=expected_commit,
                expected_owner_id=expected_owner_id,
            )
            return True
        if name in clusters(kind):
            raise ProofError(
                f"refusing to delete cluster {name!r}: cluster exists without ownership marker"
            )
        return False


def _kind_create_error_detail(error: BaseException) -> str:
    if isinstance(error, subprocess.CalledProcessError):
        return "\n".join(
            part for part in (error.stdout, error.stderr, str(error)) if part
        )
    return str(error)


def _is_transient_kind_create_error(error: BaseException) -> bool:
    if isinstance(error, subprocess.TimeoutExpired):
        return True
    detail = _kind_create_error_detail(error).lower()
    return any(marker in detail for marker in TRANSIENT_KIND_CREATE_MARKERS)


def create_kind_cluster(
    kind: str,
    name: str,
    image: str,
    config: str,
    commit: str,
    owner_id: str,
    *,
    timeout: int,
) -> None:
    command = [
        kind,
        "create",
        "cluster",
        "--name",
        name,
        "--image",
        image,
        "--config",
        config,
    ]
    for attempt in range(KIND_CREATE_ATTEMPTS):
        try:
            with cluster_creation_reservation(kind, name, commit, owner_id):
                result = run(command, capture=True, timeout=timeout)
                if result.stdout:
                    print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
                if result.stderr:
                    print(
                        result.stderr,
                        end="" if result.stderr.endswith("\n") else "\n",
                        file=sys.stderr,
                    )
                configure_cluster_access(kind, name)
            return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            if (
                not _is_transient_kind_create_error(error)
                or attempt + 1 >= KIND_CREATE_ATTEMPTS
                or os.path.lexists(marker_path(name))
            ):
                raise
            delay = KIND_CREATE_BACKOFF_SECONDS[attempt]
            print(
                f"transient kind cluster creation failure; retrying "
                f"attempt {attempt + 2}/{KIND_CREATE_ATTEMPTS} after {delay:.0f}s: "
                f"{_kind_create_error_detail(error)}",
                file=sys.stderr,
            )
            time.sleep(delay)


def _pod_spec(document: dict[str, Any]) -> dict[str, Any] | None:
    kind = document.get("kind")
    spec = document.get("spec")
    if not isinstance(spec, dict):
        return None
    if (
        controlled_oci_strict()
        and kind == "Cluster"
        and document.get("apiVersion") == "postgresql.cnpg.io/v1"
    ):
        spec["imagePullPolicy"] = "Never"
        return None
    if kind == "Pod":
        return spec
    if kind in {"Deployment", "DaemonSet", "StatefulSet", "ReplicaSet", "Job"}:
        template = spec.get("template")
    elif kind == "CronJob":
        template = spec.get("jobTemplate", {}).get("spec", {}).get("template")
    else:
        return None
    if not isinstance(template, dict):
        return None
    pod = template.get("spec")
    return pod if isinstance(pod, dict) else None


def _rewrite_controlled_oci_images(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"image", "imageName"} and isinstance(item, str):
                value[key] = controlled_oci_runtime_image(item)
            else:
                _rewrite_controlled_oci_images(item)
    elif isinstance(value, list):
        for item in value:
            _rewrite_controlled_oci_images(item)


def enforce_controlled_oci_pull_policy(
    documents: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not controlled_oci_strict():
        return documents
    for document in documents:
        _rewrite_controlled_oci_images(document)
        pod = _pod_spec(document)
        if pod is None:
            continue
        for key in ("initContainers", "containers", "ephemeralContainers"):
            containers = pod.get(key, [])
            if not isinstance(containers, list):
                continue
            for container in containers:
                if isinstance(container, dict) and isinstance(container.get("image"), str):
                    container["imagePullPolicy"] = "Never"
    return documents


def apply_yaml(kubectl: str, document: dict[str, Any] | list[dict[str, Any]]) -> None:
    documents = document if isinstance(document, list) else [document]
    payload = yaml.safe_dump_all(
        enforce_controlled_oci_pull_policy(documents), sort_keys=False
    )
    run([kubectl, "apply", "-f", "-"], input_text=payload)


def apply_file(kubectl: str, path: Path) -> None:
    if controlled_oci_strict():
        documents = [
            item
            for item in yaml.safe_load_all(path.read_text(encoding="utf-8"))
            if isinstance(item, dict)
        ]
        apply_yaml(kubectl, documents)
        return
    run([kubectl, "apply", "-f", str(path)])


def wait_rollout(kubectl: str, namespace: str, resource: str, timeout: str = "8m") -> None:
    run([kubectl, "-n", namespace, "rollout", "status", resource, f"--timeout={timeout}"])


def wait_condition(
    kubectl: str, namespace: str, resource: str, condition: str, timeout: str = "8m"
) -> None:
    run(
        [
            kubectl,
            "-n",
            namespace,
            "wait",
            resource,
            f"--for=condition={condition}",
            f"--timeout={timeout}",
        ]
    )


def wait_http_route_parent_condition(
    kubectl: str,
    namespace: str,
    route: str,
    condition: str,
    *,
    parent_name: str,
    parent_namespace: str,
    timeout_seconds: int = 480,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_parents: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        document = json.loads(
            output(
                [
                    kubectl,
                    "-n",
                    namespace,
                    "get",
                    f"httproute/{route}",
                    "-o",
                    "json",
                ]
            )
        )
        generation = document.get("metadata", {}).get("generation")
        last_parents = document.get("status", {}).get("parents", [])
        for parent in last_parents:
            reference = parent.get("parentRef", {})
            if (
                reference.get("name") != parent_name
                or reference.get("namespace", namespace) != parent_namespace
            ):
                continue
            for observed in parent.get("conditions", []):
                if (
                    observed.get("type") == condition
                    and observed.get("status") == "True"
                    and observed.get("observedGeneration") == generation
                ):
                    return
        time.sleep(2)
    raise ProofError(
        f"HTTPRoute {namespace}/{route} parent condition {condition} was not true: "
        f"{json.dumps(last_parents, sort_keys=True)}"
    )


def _dockerfile_physical_lines(source: str) -> list[str]:
    parts = source.split("\n")
    return [
        part + ("\n" if index < len(parts) - 1 else "")
        for index, part in enumerate(parts)
    ]


def _dockerfile_line_parts(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith("\n"):
        return line[:-1], "\n"
    return line, ""


def _dockerfile_logical_instructions(
    source: str, relative: Path
) -> list[tuple[str, str, str]]:
    instructions: list[tuple[str, str, str]] = []
    logical_parts: list[str] = []
    original_parts: list[str] = []
    for line in _dockerfile_physical_lines(source):
        body, ending = _dockerfile_line_parts(line)
        if re.match(
            r"^\s*#\s*(?:syntax|escape)\s*=",
            body,
            re.IGNORECASE,
        ):
            raise ProofError(
                f"Dockerfile {relative} uses a forbidden parser directive: {body}"
            )
        if logical_parts and not body.strip():
            raise ProofError(
                f"Dockerfile {relative} has a blank line inside a continued instruction"
            )
        if body.lstrip().startswith("#"):
            if logical_parts:
                raise ProofError(
                    f"Dockerfile {relative} has a comment inside a continued instruction"
                )
            instructions.append((body, line, ending))
            continue
        original_parts.append(line)
        continuation_candidate = body.rstrip(" \t")
        trailing_escape_count = len(continuation_candidate) - len(
            continuation_candidate.rstrip("\\")
        )
        continued = trailing_escape_count % 2 == 1
        if continued:
            logical_parts.append(continuation_candidate[:-1])
            continue
        logical_parts.append(body)
        instructions.append(
            ("".join(logical_parts), "".join(original_parts), ending)
        )
        logical_parts = []
        original_parts = []
    if logical_parts or original_parts:
        raise ProofError(f"Dockerfile {relative} has a dangling line continuation")
    return instructions


def _dockerfile_stage_source_allowed(
    source: str, stage_aliases: set[str], stage_count: int
) -> bool:
    normalized = source.casefold()
    if normalized in stage_aliases:
        return True
    return re.fullmatch(r"[0-9]+", source) is not None and int(source) < stage_count


def _validate_dockerfile_external_sources(
    logical: str,
    *,
    keyword: str,
    relative: Path,
    stage_aliases: set[str],
    stage_count: int,
) -> None:
    remainder = logical.lstrip()[len(keyword) :].lstrip()
    for token in re.split(r"\s+", remainder):
        if not token.startswith("--"):
            break
        if "\\" in token:
            raise ProofError(
                f"Dockerfile {relative} uses unsupported escaped option syntax: {token}"
            )
    if keyword == "ADD":
        raise ProofError(
            f"Dockerfile {relative} uses ADD, which is forbidden in strict OCI mode"
        )
    if keyword == "ONBUILD":
        raise ProofError(
            f"Dockerfile {relative} uses ONBUILD, which is forbidden in strict OCI mode"
        )
    if keyword in {"COPY", "ADD"}:
        sources = re.findall(
            r"(?:^|\s)--from(?:=|\s+)([^\s]+)", logical, re.IGNORECASE
        )
        for source in sources:
            if not _dockerfile_stage_source_allowed(
                source, stage_aliases, stage_count
            ):
                raise ProofError(
                    f"Dockerfile {relative} uses uncontrolled --from source {source}"
                )
    if keyword == "RUN":
        mounts = re.findall(
            r"(?:^|\s)--mount=([^\s]+)", logical, re.IGNORECASE
        )
        for mount in mounts:
            options: dict[str, str] = {}
            for item in mount.split(","):
                key, separator, value = item.partition("=")
                if separator:
                    normalized_key = key.strip().strip("\"'").casefold()
                    normalized_value = value.strip().strip("\"'")
                    options[normalized_key] = normalized_value
            source = options.get("from")
            if source is not None and not _dockerfile_stage_source_allowed(
                source, stage_aliases, stage_count
            ):
                raise ProofError(
                    f"Dockerfile {relative} uses uncontrolled RUN mount source {source}"
                )


def controlled_oci_dockerfile(path: Path) -> str:
    source = path.read_text(encoding="utf-8")
    if source.startswith("\ufeff"):
        source = source.removeprefix("\ufeff")
    if not controlled_oci_strict():
        return source
    relative = path.relative_to(ROOT) if path.is_absolute() else path
    expected = OCI_DOCKERFILE_IMAGES.get(relative)
    if expected is None:
        raise ProofError(f"no controlled OCI Dockerfile contract for {relative}")
    lock = _oci_mirror_lock()
    controlled_refs: dict[str, tuple[str, str]] = {}
    for name in expected:
        spec = lock["images"].get(name)
        if not isinstance(spec, dict):
            raise ProofError(f"controlled OCI build image is missing: {name}")
        canonical = spec.get("canonical")
        local_ref = spec.get("local_ref")
        digest = spec.get("digest")
        if (
            not isinstance(canonical, str)
            or not isinstance(local_ref, str)
            or not isinstance(digest, str)
            or canonical.rsplit("@", 1)[-1] != digest
        ):
            raise ProofError(f"controlled OCI build image is invalid: {name}")
        dockerfile_ref = f"{local_ref}@{digest}"
        if dockerfile_ref in controlled_refs:
            raise ProofError(
                f"controlled OCI build images share Dockerfile reference {dockerfile_ref}"
            )
        controlled_refs[dockerfile_ref] = (name, local_ref)

    from_instruction = re.compile(
        r"^(?P<prefix>\s*FROM(?:\s+--[^\s]+)*\s+)"
        r"(?P<image>[^\s]+)"
        r"(?P<suffix>(?:\s+AS\s+(?P<alias>[A-Za-z0-9._-]+))?\s*)$",
        re.IGNORECASE,
    )
    instruction = re.compile(
        r"^\s*(?P<keyword>[A-Za-z]+)(?=\s|$)", re.IGNORECASE
    )
    observed: dict[str, int] = {name: 0 for name in expected}
    stage_aliases: set[str] = set()
    stage_count = 0
    rewritten: list[str] = []
    for logical, original, ending in _dockerfile_logical_instructions(
        source, relative
    ):
        stripped = logical.lstrip()
        if not stripped or stripped.startswith("#"):
            rewritten.append(original)
            continue
        instruction_match = instruction.match(logical)
        if instruction_match is None:
            raise ProofError(
                f"Dockerfile {relative} has unsupported instruction syntax: {logical}"
            )
        keyword = instruction_match.group("keyword").upper()
        if keyword != "FROM":
            _validate_dockerfile_external_sources(
                logical,
                keyword=keyword,
                relative=relative,
                stage_aliases=stage_aliases,
                stage_count=stage_count,
            )
            rewritten.append(original)
            continue
        match = from_instruction.fullmatch(logical)
        if match is None:
            raise ProofError(
                f"Dockerfile {relative} has unsupported FROM syntax: {logical}"
            )
        image = match.group("image")
        replacement = controlled_refs.get(image)
        if replacement is None:
            if image.casefold() not in stage_aliases:
                raise ProofError(
                    f"Dockerfile {relative} uses uncontrolled OCI base image {image}"
                )
            rewritten.append(original)
        else:
            name, local_ref = replacement
            observed[name] += 1
            rewritten.append(
                f"{match.group('prefix')}{local_ref}{match.group('suffix')}{ending}"
            )
        alias = match.group("alias")
        if alias is not None:
            stage_aliases.add(alias.casefold())
        stage_count += 1

    invalid_counts = {name: count for name, count in observed.items() if count != 1}
    if invalid_counts:
        detail = ", ".join(
            f"{name}={count}" for name, count in sorted(invalid_counts.items())
        )
        raise ProofError(
            f"Dockerfile {relative} must use each controlled OCI base exactly once: {detail}"
        )
    return "".join(rewritten)


def _build_dockerfile(path: Path) -> tuple[list[str], str | None]:
    if not controlled_oci_strict():
        return ["--file", str(path)], None
    return ["--file", "-"], controlled_oci_dockerfile(path)


def build_images(kind: str, cluster: str, commit: str, timestamp: str) -> dict[str, str]:
    images = {
        "api": "weltgewebe-api:local",
        "web": "weltgewebe-web:local",
    }
    pull_args = ["--pull=false"] if controlled_oci_strict() else []
    api_file_args, api_dockerfile = _build_dockerfile(Path("apps/api/Dockerfile"))
    run(
        [
            "docker",
            "build",
            *pull_args,
            *api_file_args,
            "--build-arg",
            f"GIT_COMMIT_SHA={commit}",
            "--build-arg",
            f"BUILD_TIMESTAMP={timestamp}",
            "--tag",
            images["api"],
            ".",
        ],
        input_text=api_dockerfile,
        timeout=3600,
    )
    web_file_args, web_dockerfile = _build_dockerfile(Path("apps/web/Dockerfile"))
    run(
        [
            "docker",
            "build",
            *pull_args,
            *web_file_args,
            "--build-arg",
            f"GIT_COMMIT_SHA={commit}",
            "--build-arg",
            f"BUILD_TIMESTAMP={timestamp}",
            "--tag",
            images["web"],
            ".",
        ],
        input_text=web_dockerfile,
        timeout=1800,
    )
    for image in images.values():
        run([kind, "load", "docker-image", "--name", cluster, image], timeout=600)
    return {
        name: output(["docker", "image", "inspect", "--format", "{{.Id}}", image])
        for name, image in images.items()
    }


def install_platform_components(
    kubectl: str,
    flux: str,
    helm: str,
    artifacts: dict[str, str],
    api_server_host: str,
) -> None:
    for artifact in GATEWAY_API_ARTIFACTS:
        apply_file(kubectl, Path(artifacts[artifact]))
    run(
        [
            helm,
            "upgrade",
            "--install",
            "cilium",
            artifacts["cilium_chart"],
            "--namespace",
            "kube-system",
            "--set",
            "gatewayAPI.enabled=true",
            "--set",
            "nodeIPAM.enabled=true",
            "--set",
            "defaultLBServiceIPAM=nodeipam",
            "--set",
            "kubeProxyReplacement=true",
            "--set",
            f"k8sServiceHost={api_server_host}",
            "--set",
            "k8sServicePort=6443",
            "--set",
            "hubble.relay.enabled=true",
            "--set",
            "hubble.ui.enabled=false",
            "--set",
            "operator.replicas=1",
            *(
                [
                    "--set",
                    "image.pullPolicy=Never",
                    "--set",
                    "operator.image.pullPolicy=Never",
                    "--set",
                    "envoy.image.pullPolicy=Never",
                    "--set",
                    "hubble.relay.image.pullPolicy=Never",
                    "--set",
                    "image.useDigest=false",
                    "--set",
                    "operator.image.useDigest=false",
                    "--set",
                    "envoy.image.useDigest=false",
                    "--set",
                    "hubble.relay.image.useDigest=false",
                ]
                if controlled_oci_strict()
                else []
            ),
            "--wait",
            "--timeout",
            "10m",
        ],
        timeout=900,
    )
    wait_rollout(kubectl, "kube-system", "daemonset/cilium")
    wait_rollout(kubectl, "kube-system", "deployment/cilium-operator")
    wait_rollout(kubectl, "kube-system", "deployment/hubble-relay")
    flux_command = [
        flux,
        "install",
        "--namespace=flux-system",
        "--components=source-controller,kustomize-controller,helm-controller,notification-controller",
    ]
    if controlled_oci_strict():
        exported = output([*flux_command, "--export"])
        flux_documents = [
            item for item in yaml.safe_load_all(exported) if isinstance(item, dict)
        ]
        apply_yaml(kubectl, flux_documents)
    else:
        run(flux_command, timeout=600)
    for deployment in (
        "source-controller",
        "kustomize-controller",
        "helm-controller",
        "notification-controller",
    ):
        wait_rollout(kubectl, "flux-system", f"deployment/{deployment}")


def namespace_document(name: str, *, data_client: bool = False) -> dict[str, Any]:
    labels = {
        "pod-security.kubernetes.io/enforce": "restricted",
        "pod-security.kubernetes.io/audit": "restricted",
        "pod-security.kubernetes.io/warn": "restricted",
    }
    if data_client:
        labels["weltgewebe.net/data-client"] = "true"
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": name, "labels": labels},
    }


def has_exactly_one_value(*values: str | None) -> bool:
    return sum(bool(value) for value in values) == 1


def validate_source_binding(
    mode: str, *, source_ref: str | None, source_commit: str | None
) -> None:
    if mode not in {"direct", "gitops"}:
        raise ProofError(f"unsupported proof mode: {mode}")
    if mode == "direct":
        if source_ref or source_commit:
            raise ProofError(
                "--source-ref and --source-commit are invalid for direct mode"
            )
        return
    if not has_exactly_one_value(source_ref, source_commit):
        raise ProofError(
            "exactly one of --source-ref or --source-commit is required "
            "for gitops mode"
        )
    if source_commit and FULL_GIT_OBJECT_ID.fullmatch(source_commit) is None:
        raise ProofError("--source-commit must be a full lowercase Git object id")


def validate_workspace_binding(
    *, source_commit: str | None, workspace_commit: str
) -> None:
    if source_commit and source_commit != workspace_commit:
        raise ProofError(
            "--source-commit must equal the exact local workspace HEAD "
            f"({workspace_commit})"
        )


def flux_source_document(
    *, branch: str | None = None, commit: str | None = None
) -> dict[str, Any]:
    if not has_exactly_one_value(branch, commit):
        raise ProofError("exactly one Flux source branch or commit is required")
    document = yaml.safe_load((ROOT / "platform/clusters/local/source.yaml").read_text())
    document["spec"]["ref"] = {"branch": branch} if branch else {"commit": commit}
    return document


def flux_kustomization_document(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ProofError(f"Flux Kustomization document is invalid: {path}")
    if not controlled_oci_strict():
        return document
    name = document.get("metadata", {}).get("name")
    if name != "weltgewebe-local-data":
        return document
    spec = document.get("spec")
    if not isinstance(spec, dict) or "patches" in spec:
        raise ProofError("local-data Flux Kustomization patch contract drift")
    lock = _oci_mirror_lock()
    runtime_images: dict[str, str] = {}
    for image_name in ("local_postgres", "local_nats"):
        image = lock["images"].get(image_name)
        if not isinstance(image, dict) or not isinstance(image.get("canonical"), str):
            raise ProofError(f"controlled OCI image is missing: {image_name}")
        runtime_images[image_name] = controlled_oci_runtime_image(image["canonical"])
    patches = []
    for deployment, container, image_name in (
        ("postgres", "postgres", "local_postgres"),
        ("nats", "nats", "local_nats"),
    ):
        patch_document = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": deployment,
                "namespace": "weltgewebe-data",
            },
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": container,
                                "image": runtime_images[image_name],
                                "imagePullPolicy": "Never",
                            }
                        ]
                    }
                }
            },
        }
        patches.append(
            {
                "target": {
                    "kind": "Deployment",
                    "name": deployment,
                    "namespace": "weltgewebe-data",
                },
                "patch": yaml.safe_dump(patch_document, sort_keys=False),
            }
        )
    spec["patches"] = patches
    return document


def apply_direct(kubectl: str, kustomize: str, path: str) -> None:
    rendered = output([kustomize, "build", path])
    if controlled_oci_strict():
        documents = [
            item for item in yaml.safe_load_all(rendered) if isinstance(item, dict)
        ]
        apply_yaml(kubectl, documents)
        return
    run([kubectl, "apply", "-f", "-"], input_text=rendered)


def apply_flux_data(
    kubectl: str, *, source_ref: str | None = None, source_commit: str | None = None
) -> None:
    apply_yaml(
        kubectl,
        flux_source_document(branch=source_ref, commit=source_commit),
    )
    apply_yaml(
        kubectl,
        flux_kustomization_document(
            ROOT / "platform/clusters/local/local-data.yaml"
        ),
    )
    apply_file(kubectl, ROOT / "platform/clusters/local/migration.yaml")
    wait_condition(kubectl, "flux-system", "gitrepository/weltgewebe", "Ready")
    wait_condition(kubectl, "flux-system", "kustomization/weltgewebe-local-data", "Ready")
    wait_condition(kubectl, "flux-system", "kustomization/weltgewebe-migration", "Ready")


def migrate_direct(kubectl: str, kustomize: str) -> None:
    run(
        [
            kubectl,
            "-n",
            "weltgewebe",
            "delete",
            "job",
            "weltgewebe-migration",
            "--ignore-not-found=true",
            "--wait=true",
        ]
    )
    apply_direct(kubectl, kustomize, "platform/apps/weltgewebe/migration/local")
    wait_condition(
        kubectl,
        "weltgewebe",
        "job/weltgewebe-migration",
        "Complete",
        "8m",
    )


def apply_app_and_gateway(
    kubectl: str, kustomize: str, mode: str
) -> None:
    if mode == "gitops":
        apply_file(kubectl, ROOT / "platform/clusters/local/app.yaml")
        apply_file(kubectl, ROOT / "platform/clusters/local/gateway.yaml")
        wait_condition(kubectl, "flux-system", "kustomization/weltgewebe-app", "Ready")
        wait_condition(kubectl, "flux-system", "kustomization/weltgewebe-gateway", "Ready")
    else:
        apply_direct(kubectl, kustomize, "platform/apps/weltgewebe/overlays/local")
        apply_direct(kubectl, kustomize, "platform/infrastructure/gateway")


def wait_apps(kubectl: str, namespace: str) -> None:
    wait_rollout(kubectl, namespace, "deployment/weltgewebe-api")
    wait_rollout(kubectl, namespace, "deployment/weltgewebe-web")
    available = output(
        [
            kubectl,
            "-n",
            namespace,
            "get",
            "deployment/weltgewebe-api",
            "-o",
            "jsonpath={.status.availableReplicas}",
        ]
    )
    if available != "2":
        raise ProofError(f"expected two available API replicas, got {available!r}")


def ready_api_pods(kubectl: str, namespace: str) -> list[tuple[str, str]]:
    document = json.loads(
        output(
            [
                kubectl,
                "-n",
                namespace,
                "get",
                "pod",
                "-l",
                "app.kubernetes.io/name=weltgewebe-api",
                "-o",
                "json",
            ]
        )
    )
    ready: list[tuple[str, str]] = []
    for item in document.get("items", []):
        metadata = item.get("metadata", {})
        status = item.get("status", {})
        if metadata.get("deletionTimestamp") is not None:
            continue
        if status.get("phase") != "Running":
            continue
        conditions = status.get("conditions", [])
        if not any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in conditions
        ):
            continue
        container_statuses = status.get("containerStatuses", [])
        if not container_statuses or not all(
            container.get("ready") is True for container in container_statuses
        ):
            continue
        name = metadata.get("name")
        uid = metadata.get("uid")
        if isinstance(name, str) and name and isinstance(uid, str) and uid:
            ready.append((name, uid))
    return sorted(ready)


def api_version(kubectl: str, namespace: str) -> str:
    last_error: BaseException | None = None
    for attempt in range(30):
        try:
            pods = ready_api_pods(kubectl, namespace)
        except subprocess.CalledProcessError as error:
            pods = []
            last_error = error
        for pod, _ in pods:
            try:
                return output(
                    [
                        kubectl,
                        "-n",
                        namespace,
                        "exec",
                        pod,
                        "--",
                        "wget",
                        "-qO-",
                        "-T",
                        "5",
                        "http://weltgewebe-api:8080/version",
                    ]
                )
            except subprocess.CalledProcessError as error:
                last_error = error
        if attempt == 29:
            raise ProofError(
                "API service did not become reachable from a current ready API pod"
            ) from last_error
        time.sleep(1)
    raise AssertionError("unreachable API service retry state")


def ready_api_pod_uids(kubectl: str, namespace: str) -> set[str]:
    pods = ready_api_pods(kubectl, namespace)
    if len(pods) != 2:
        raise ProofError(
            f"expected exactly two current ready API pods, got {len(pods)}"
        )
    return {uid for _, uid in pods}


def prove_restart(kubectl: str, namespace: str) -> dict[str, str]:
    before_pods = ready_api_pod_uids(kubectl, namespace)
    before = api_version(kubectl, namespace)
    run([kubectl, "-n", namespace, "rollout", "restart", "deployment/weltgewebe-api"])
    wait_rollout(kubectl, namespace, "deployment/weltgewebe-api")
    after_pods = ready_api_pod_uids(kubectl, namespace)
    overlap = before_pods & after_pods
    if overlap:
        raise ProofError(
            "API rollout did not replace every ready pod: "
            + ", ".join(sorted(overlap))
        )
    after = api_version(kubectl, namespace)
    if json.loads(before).get("git_commit") != json.loads(after).get("git_commit"):
        raise ProofError("API commit changed across restart")
    return {
        "before": hashlib.sha256(before.encode()).hexdigest(),
        "after": hashlib.sha256(after.encode()).hexdigest(),
        "before_pods_sha256": hashlib.sha256(
            "\n".join(sorted(before_pods)).encode()
        ).hexdigest(),
        "after_pods_sha256": hashlib.sha256(
            "\n".join(sorted(after_pods)).encode()
        ).hexdigest(),
        "replaced_replicas": str(len(after_pods)),
    }



def prove_flux_drift(kubectl: str, flux: str, namespace: str) -> None:
    run([kubectl, "-n", namespace, "scale", "deployment/weltgewebe-api", "--replicas=1"])
    run([flux, "reconcile", "kustomization", "weltgewebe-app", "--with-source", "--timeout=5m"])
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        replicas = output(
            [
                kubectl,
                "-n",
                namespace,
                "get",
                "deployment/weltgewebe-api",
                "-o",
                "jsonpath={.spec.replicas}",
            ]
        )
        if replicas == "2":
            wait_rollout(kubectl, namespace, "deployment/weltgewebe-api")
            return
        time.sleep(3)
    raise ProofError("Flux did not repair replica drift")


def gateway_addresses(kubectl: str) -> list[str]:
    document = json.loads(output([kubectl, "-n", "weltgewebe-gateway", "get", "gateway", "weltgewebe", "-o", "json"]))
    addresses: list[str] = []
    for entry in document.get("status", {}).get("addresses", []):
        if entry.get("type") != "IPAddress" or not isinstance(entry.get("value"), str):
            continue
        try:
            address = ipaddress.ip_address(entry["value"])
        except ValueError:
            continue
        if address.version == 4 and not address.is_unspecified:
            value = str(address)
            if value not in addresses:
                addresses.append(value)
    if not addresses:
        raise ProofError("Gateway has no usable IPv4 status address")
    return addresses


def gateway_listener_port(kubectl: str) -> int:
    document = json.loads(
        output(
            [
                kubectl,
                "-n",
                "weltgewebe-gateway",
                "get",
                "gateway",
                "weltgewebe",
                "-o",
                "json",
            ]
        )
    )
    ports = [
        listener.get("port")
        for listener in document.get("spec", {}).get("listeners", [])
        if listener.get("protocol") == "HTTP"
    ]
    if len(ports) != 1 or not isinstance(ports[0], int):
        raise ProofError(f"Gateway must expose exactly one HTTP listener: {ports!r}")
    port = ports[0]
    if not 1 <= port <= 65535:
        raise ProofError(f"Gateway listener port is out of range: {port}")
    return port


def gateway_service_listener_port(
    kubectl: str, service: str, expected_port: int
) -> int:
    document = json.loads(
        output(
            [
                kubectl,
                "-n",
                "weltgewebe-gateway",
                "get",
                "service",
                service,
                "-o",
                "json",
            ]
        )
    )
    spec = document.get("spec", {})
    if spec.get("type") != "LoadBalancer":
        raise ProofError("Gateway service is not a LoadBalancer")
    matches = [
        port
        for port in spec.get("ports", [])
        if port.get("protocol", "TCP") == "TCP" and port.get("port") == expected_port
    ]
    if len(matches) != 1:
        raise ProofError(
            "Gateway LoadBalancer service does not expose its HTTP listener port: "
            f"{expected_port}"
        )
    return expected_port



def kind_nodes(kind: str, cluster: str) -> list[str]:
    nodes = [
        node.strip()
        for node in output([kind, "get", "nodes", "--name", cluster]).splitlines()
        if node.strip()
    ]
    if not nodes:
        raise ProofError(f"kind cluster has no nodes: {cluster}")
    return nodes


def probe_gateway_http(
    kind: str,
    cluster: str,
    addresses: list[str],
    port: int,
    timeout_seconds: int = 30,
) -> tuple[str, str, bytes, bytes, bytes]:
    nodes = kind_nodes(kind, cluster)
    deadline = time.monotonic() + timeout_seconds
    last_error: BaseException | None = None
    while True:
        for node in nodes:
            for address in addresses:
                common = [
                    "docker",
                    "exec",
                    node,
                    "curl",
                    "--fail",
                    "--silent",
                    "--show-error",
                    "--max-time",
                    "10",
                ]
                try:
                    health = subprocess.run(
                        [*common, f"http://{address}:{port}/health/live"],
                        cwd=ROOT,
                        text=False,
                        capture_output=True,
                        check=True,
                        timeout=15,
                    ).stdout
                    web = subprocess.run(
                        [*common, f"http://{address}:{port}/"],
                        cwd=ROOT,
                        text=False,
                        capture_output=True,
                        check=True,
                        timeout=15,
                    ).stdout[:1024]
                    api_nodes = subprocess.run(
                        [*common, f"http://{address}:{port}/api/nodes"],
                        cwd=ROOT,
                        text=False,
                        capture_output=True,
                        check=True,
                        timeout=15,
                    ).stdout
                    try:
                        api_payload = json.loads(api_nodes)
                    except (UnicodeDecodeError, json.JSONDecodeError) as error:
                        raise ProofError(
                            "Gateway /api/nodes route did not return valid JSON"
                        ) from error
                    if not isinstance(api_payload, list):
                        raise ProofError(
                            "Gateway /api/nodes route did not preserve the API list contract"
                        )
                    if health and web:
                        return node, address, health, web, api_nodes
                    last_error = ProofError("Gateway route returned empty content")
                except (
                    subprocess.CalledProcessError,
                    subprocess.TimeoutExpired,
                    ProofError,
                ) as error:
                    last_error = error
        if time.monotonic() >= deadline:
            raise ProofError(
                "Gateway status address did not serve listener HTTP from a kind node: "
                f"nodes={nodes!r} addresses={addresses!r} port={port}"
            ) from last_error
        time.sleep(1)


def prove_gateway(kubectl: str, kind: str, cluster: str) -> dict[str, str]:
    wait_condition(kubectl, "weltgewebe-gateway", "gateway/weltgewebe", "Programmed")
    wait_http_route_parent_condition(
        kubectl,
        "weltgewebe",
        "weltgewebe",
        "Accepted",
        parent_name="weltgewebe",
        parent_namespace="weltgewebe-gateway",
    )
    wait_http_route_parent_condition(
        kubectl,
        "weltgewebe",
        "weltgewebe",
        "ResolvedRefs",
        parent_name="weltgewebe",
        parent_namespace="weltgewebe-gateway",
    )
    service = output(
        [
            kubectl,
            "-n",
            "weltgewebe-gateway",
            "get",
            "service",
            "-l",
            "gateway.networking.k8s.io/gateway-name=weltgewebe",
            "-o",
            "jsonpath={.items[0].metadata.name}",
        ]
    )
    if not service:
        raise ProofError("Gateway service was not created")
    listener_port = gateway_listener_port(kubectl)
    probe_node, address, health, web, api_nodes = probe_gateway_http(
        kind,
        cluster,
        gateway_addresses(kubectl),
        gateway_service_listener_port(kubectl, service, listener_port),
    )
    return {
        "service": service,
        "probe_node": probe_node,
        "address": address,
        "listener_port": str(listener_port),
        "health_sha256": hashlib.sha256(health).hexdigest(),
        "web_prefix_sha256": hashlib.sha256(web).hexdigest(),
        "api_nodes_sha256": hashlib.sha256(api_nodes).hexdigest(),
    }


def diagnostic_snapshot(kubectl: str, name: str) -> None:
    target = CACHE / "failures" / name
    target.mkdir(parents=True, exist_ok=True)
    commands = {
        "nodes.txt": [kubectl, "get", "nodes", "-o", "wide"],
        "pods.txt": [kubectl, "get", "pods", "-A", "-o", "wide"],
        "events.txt": [kubectl, "get", "events", "-A", "--sort-by=.lastTimestamp"],
        "flux.txt": [kubectl, "-n", "flux-system", "get", "gitrepositories,kustomizations"],
        "gateway.txt": [kubectl, "get", "gateway,httproute", "-A", "-o", "yaml"],
    }
    for filename, argv in commands.items():
        result = subprocess.run(argv, cwd=ROOT, text=True, capture_output=True)
        (target / filename).write_text(result.stdout + result.stderr, encoding="utf-8")


def proof(args: argparse.Namespace) -> dict[str, Any]:
    validate_source_binding(
        args.mode,
        source_ref=args.source_ref,
        source_commit=args.source_commit,
    )
    if output(["git", "status", "--porcelain"]):
        raise ProofError("reference proof requires a clean, commit-bound worktree")
    commit = output(["git", "rev-parse", "HEAD"])
    validate_workspace_binding(
        source_commit=args.source_commit,
        workspace_commit=commit,
    )
    timestamp = output(["git", "show", "-s", "--format=%cI", "HEAD"])
    require_host_tools()
    receipt = tool_receipt()
    tools = receipt["tools"]
    kind = tools["kind"]
    kubectl = tools["kubectl"]
    kustomize = tools["kustomize"]
    flux = tools["flux"]
    helm = tools["helm"]
    owner_id = args.owner_id or generate_owner_id("kind-proof")
    app_namespace = "weltgewebe"
    oci_host: dict[str, Any] | None = None
    node_image = receipt["kubernetes"]["kind_node_image"]
    if controlled_oci_strict():
        oci_host = prepare_controlled_oci_host("kind-gitops")
        node_image = oci_host["kind_node_image"]
    reserved = False
    created = False
    try:
        create_kind_cluster(
            kind,
            args.cluster,
            node_image,
            "platform/clusters/local/kind.yaml",
            commit,
            owner_id,
            timeout=600,
        )
        reserved = True
        created = True
        oci_cluster: dict[str, Any] | None = None
        if controlled_oci_strict():
            oci_cluster = load_controlled_oci_into_kind(
                kind, args.cluster, "kind-gitops"
            )
        api_server_host = control_plane_address(args.cluster)
        image_ids = build_images(kind, args.cluster, commit, timestamp)
        install_platform_components(
            kubectl, flux, helm, receipt["artifacts"], api_server_host
        )
        run([kubectl, "wait", "--for=condition=Ready", "nodes", "--all", "--timeout=5m"])
        if args.mode == "gitops":
            apply_flux_data(
                kubectl,
                source_ref=args.source_ref,
                source_commit=args.source_commit,
            )
        else:
            apply_direct(kubectl, kustomize, "platform/infrastructure/local-data")
            wait_rollout(kubectl, "weltgewebe-data", "deployment/postgres")
            wait_rollout(kubectl, "weltgewebe-data", "deployment/nats")
            migrate_direct(kubectl, kustomize)
        apply_app_and_gateway(kubectl, kustomize, args.mode)
        wait_apps(kubectl, app_namespace)
        restart = prove_restart(kubectl, app_namespace)
        if args.mode == "gitops":
            prove_flux_drift(kubectl, flux, app_namespace)
        gateway = prove_gateway(kubectl, kind, args.cluster)
        result = {
            "schema_version": 1,
            "status": "pass",
            "cluster": args.cluster,
            "mode": args.mode,
            "source_ref": args.source_ref,
            "source_commit": args.source_commit,
            "commit": commit,
            "owner_id": owner_id,
            "tool_lock_sha256": receipt["lock_sha256"],
            "oci_controlled_source": {
                "strict": controlled_oci_strict(),
                "host": oci_host,
                "cluster": oci_cluster,
            },
            "image_ids": image_ids,
            "bootstrap_api_server": {"host": api_server_host, "port": 6443},
            "api_restart": restart,
            "gateway": gateway,
            "api_replicas": 2,
            "migration_credential_class": "public-local-configmap",
            "migration_workload": "batch/v1 Job/weltgewebe-migration",
            "production_changed": False,
        }
        target = CACHE / "receipts"
        target.mkdir(parents=True, exist_ok=True)
        owner_receipt_key = hashlib.sha256(owner_id.encode("utf-8")).hexdigest()[:16]
        path = target / f"{args.cluster}-{commit}-{owner_receipt_key}.json"
        write_json_atomic(path, result)
        result["receipt_path"] = str(path)
        result["receipt_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        return result
    except Exception:
        if created:
            diagnostic_snapshot(kubectl, args.cluster)
        raise
    finally:
        if reserved and not args.keep:
            delete_owned_cluster(
                kind,
                args.cluster,
                expected_commit=commit,
                expected_owner_id=owner_id,
            )


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    proof_parser = subparsers.add_parser("proof")
    proof_parser.add_argument("--cluster", default=DEFAULT_CLUSTER)
    proof_parser.add_argument("--mode", choices=("direct", "gitops"), default="direct")
    source_group = proof_parser.add_mutually_exclusive_group()
    source_group.add_argument("--source-ref")
    source_group.add_argument("--source-commit")
    proof_parser.add_argument("--keep", action="store_true")
    proof_parser.add_argument("--owner-id")
    down_parser = subparsers.add_parser("down")
    down_parser.add_argument("--cluster", default=DEFAULT_CLUSTER)
    down_parser.add_argument("--commit", required=True)
    down_parser.add_argument("--owner-id", required=True)
    down_parser.add_argument("--receipt", type=Path)
    return parser


def main() -> int:
    args = argument_parser().parse_args()
    try:
        receipt = tool_receipt()
        if args.command == "down":
            deleted = delete_owned_cluster_if_present(
                receipt["tools"]["kind"],
                args.cluster,
                expected_commit=args.commit,
                expected_owner_id=args.owner_id,
            )
            cleanup = {
                "schema_version": 1,
                "status": "pass",
                "cleanup_status": "deleted" if deleted else "absent",
                "cluster": args.cluster,
                "commit": args.commit,
                "owner_id": args.owner_id,
                "owned_resources_remaining": False,
            }
            if args.receipt:
                write_json_atomic(args.receipt, cleanup)
            print(json.dumps(cleanup, sort_keys=True))
            return 0
        result = proof(args)
    except (ProofError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        print(f"kind reference proof failed: {error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError):
            print(error.stdout or "", file=sys.stderr)
            print(error.stderr or "", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
