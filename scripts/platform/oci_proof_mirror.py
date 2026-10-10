#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

ROOT = Path(__file__).resolve().parents[2]
LOCK_PATH = ROOT / "platform/oci-proof-mirror.lock.json"
DEFAULT_STATE = ROOT / ".cache/weltgewebe-platform/oci-mirror"
FULL_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
HEX64 = re.compile(r"[0-9a-f]{64}")
ALLOWED_SUITES = frozenset({"kind-gitops", "ha-recovery", "app-build"})
BLOCKED_REGISTRIES = (
    "registry-1.docker.io",
    "auth.docker.io",
    "production.cloudflare.docker.com",
    "quay.io",
    "ghcr.io",
)
EXPECTED_IMAGES = frozenset(
    {
        "kind_node",
        "cilium_agent",
        "cilium_operator",
        "cilium_envoy",
        "hubble_relay",
        "flux_source_controller",
        "flux_kustomize_controller",
        "flux_helm_controller",
        "flux_notification_controller",
        "local_postgres",
        "local_nats",
        "cloudnative_pg_operator",
        "cloudnative_pg_postgresql",
        "ha_nats",
        "nats_box",
        "seaweedfs",
        "cert_manager_controller",
        "cert_manager_cainjector",
        "cert_manager_webhook",
        "barman_cloud_plugin",
        "barman_cloud_sidecar",
        "build_rust",
        "build_debian",
        "build_node",
        "build_caddy",
    }
)


class MirrorError(RuntimeError):
    failure_class = "mirror_error"


class ControlledMirrorUnavailableError(MirrorError):
    failure_class = "controlled_mirror_unavailable"


class IntegrityError(MirrorError):
    failure_class = "integrity_mismatch"


class AncestryError(IntegrityError):
    """Generation source commit is outside the allowed, reachable ancestry."""

    failure_class = "ancestry_violation"


class BudgetError(MirrorError):
    failure_class = "budget_exceeded"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_canonical_json(payload))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _directory_lock(path: Path) -> Iterator[None]:
    path.mkdir(parents=True, exist_ok=True)
    lock_path = path / ".lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _run(
    argv: list[str],
    *,
    capture: bool = False,
    timeout: int = 900,
) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(argv), file=sys.stderr, flush=True)
    return subprocess.run(
        argv,
        cwd=ROOT,
        check=True,
        capture_output=capture,
        text=True,
        timeout=timeout,
    )


def _output(argv: list[str], *, timeout: int = 120) -> str:
    return _run(argv, capture=True, timeout=timeout).stdout.strip()


def _load_seed() -> dict[str, Any]:
    path = ROOT / "platform/oci-proof-mirror.seed.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise IntegrityError(f"OCI mirror seed is unreadable: {error}") from error
    if payload.get("schema_version") != 1 or payload.get("owner") != "heimgewebe/weltgewebe":
        raise IntegrityError("OCI mirror seed identity mismatch")
    return payload


def _validate_evidence_binding(name: str, payload: Any) -> None:
    if not isinstance(payload, dict):
        raise IntegrityError(f"mirror generation evidence {name} is missing")
    if not HEX64.fullmatch(str(payload.get("sha256", ""))):
        raise IntegrityError(f"mirror generation evidence {name} lacks SHA-256")
    if not isinstance(payload.get("bytes"), int) or payload["bytes"] <= 0:
        raise IntegrityError(f"mirror generation evidence {name} lacks byte count")


def _git_bytes(argv: list[str]) -> bytes:
    try:
        return subprocess.run(
            ["git", *argv],
            cwd=ROOT,
            check=True,
            capture_output=True,
            timeout=30,
        ).stdout
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise IntegrityError(f"Git binding check failed for {argv}: {error}") from error


def _validate_generation_ancestry(
    generation: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Fail closed on source commit, allowed ancestry, and seed binding.

    Machine-checked generation contract (before inventory / full validation):

    - ``source_head`` is a full lowercase 40-hex Git commit id
    - that commit is reachable in this clone (``git cat-file -e``)
    - that commit is an ancestor of ``HEAD`` (allowed ancestry for this checkout)
    - ``seed_sha256`` is a 64-hex digest matching the seed blob at ``source_head``
    - a changed working-tree seed must match an explicitly staged pending digest
    - while staged, the published generation and its mirror images remain authoritative

    Lock updates must re-bind ``source_head`` and ``seed_sha256`` together with the
    publisher evidence fields. Ancestry is never skipped or weakened.
    """
    source_head = str(generation.get("source_head", ""))
    seed_sha256 = str(generation.get("seed_sha256", ""))
    if not re.fullmatch(r"[0-9a-f]{40}", source_head):
        raise AncestryError("OCI mirror generation source_head is invalid")
    if not HEX64.fullmatch(seed_sha256):
        raise AncestryError("OCI mirror generation seed_sha256 is invalid")

    try:
        subprocess.run(
            ["git", "cat-file", "-e", f"{source_head}^{{commit}}"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise AncestryError(
            f"OCI mirror source_head is unreachable in this clone: {source_head}"
        ) from error

    try:
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", source_head, "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise AncestryError(
            "OCI mirror source_head is outside allowed ancestry of HEAD: "
            f"{source_head}"
        ) from error

    try:
        seed_at_head = subprocess.run(
            ["git", "show", f"{source_head}:platform/oci-proof-mirror.seed.json"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            timeout=30,
        ).stdout
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise AncestryError(
            f"OCI mirror seed is not present at source_head {source_head}"
        ) from error
    if hashlib.sha256(seed_at_head).hexdigest() != seed_sha256:
        raise AncestryError(
            "OCI mirror generation seed_sha256 is not bound to source_head"
        )
    current_seed = ROOT / "platform/oci-proof-mirror.seed.json"
    try:
        current_digest = hashlib.sha256(current_seed.read_bytes()).hexdigest()
    except OSError as error:
        raise AncestryError(f"current OCI mirror seed is unreadable: {error}") from error
    pending_digest = generation.get("pending_seed_sha256")
    if current_digest == seed_sha256:
        if pending_digest is not None:
            raise AncestryError("OCI mirror pending seed marker is stale")
        pending = False
    else:
        if (
            not isinstance(pending_digest, str)
            or not HEX64.fullmatch(pending_digest)
            or pending_digest != current_digest
        ):
            raise AncestryError(
                "current OCI mirror seed changed without a new lock generation "
                "or exact pending_seed_sha256"
            )
        pending = True
    try:
        published_seed = json.loads(seed_at_head)
    except json.JSONDecodeError as error:
        raise AncestryError("published OCI mirror seed is invalid JSON") from error
    return published_seed, pending


def _load_lock() -> dict[str, Any]:
    try:
        lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise IntegrityError(f"OCI mirror lock is unreadable: {error}") from error
    if lock.get("schema_version") != 1:
        raise IntegrityError("unsupported OCI mirror lock schema")
    if lock.get("kind") != "weltgewebe.oci-proof-mirror-lock":
        raise IntegrityError("OCI mirror lock kind mismatch")
    if lock.get("owner") != "heimgewebe/weltgewebe":
        raise IntegrityError("OCI mirror lock owner mismatch")

    generation = lock.get("generation")
    mirror = lock.get("mirror")
    budgets = lock.get("budgets")
    images = lock.get("images")
    if not all(isinstance(item, dict) for item in (generation, mirror, budgets, images)):
        raise IntegrityError("OCI mirror lock sections are incomplete")
    # Ancestry / seed binding fails closed before inventory and mirror section checks.
    published_seed, pending_seed = _validate_generation_ancestry(generation)
    if not str(generation.get("workflow_run_id", "")).isdigit():
        raise IntegrityError("OCI mirror workflow run ID is invalid")
    if not str(generation.get("workflow_run_attempt", "")).isdigit():
        raise IntegrityError("OCI mirror workflow run attempt is invalid")
    for field in (
        "publisher_actor",
        "started_at",
        "completed_at",
        "docker_version",
        "buildx_version",
    ):
        if not isinstance(generation.get(field), str) or not generation[field]:
            raise IntegrityError(f"OCI mirror generation {field} is missing")
    for evidence in ("provenance", "package_preflight", "package_metadata", "read_access"):
        _validate_evidence_binding(evidence, generation.get(evidence))
    if generation["read_access"].get("checked_images") != len(EXPECTED_IMAGES):
        raise IntegrityError("OCI mirror read-access evidence did not verify inventory")

    expected_mirror = {
        "repository": "ghcr.io/heimgewebe/weltgewebe-proof-oci",
        "package_type": "container",
        "visibility": "private",
        "repository_binding": "heimgewebe/commonthing",
    }
    if {key: mirror.get(key) for key in expected_mirror} != expected_mirror:
        raise IntegrityError("OCI mirror package binding mismatch")
    if not isinstance(mirror.get("package_id"), int) or mirror["package_id"] <= 0:
        raise IntegrityError("OCI mirror package ID is invalid")
    for timestamp in ("created_at", "updated_at"):
        if not isinstance(mirror.get(timestamp), str) or not mirror[timestamp].endswith("Z"):
            raise IntegrityError(f"OCI mirror package {timestamp} is invalid")

    expected_budget_policy = {
        "pull_attempts": 3,
        "retry_backoff_seconds": [5, 10],
        "max_parallel_pulls": 3,
        "max_inventory_images": 30,
        "package_version_hard_limit": 512,
        "orphan_grace_days": 14,
        "cleanup_scope": "unreferenced-tagged-top-level-versions-only",
    }
    if {key: budgets.get(key) for key in expected_budget_policy} != expected_budget_policy:
        raise IntegrityError("OCI mirror budget policy mismatch")
    observed_versions = budgets.get("observed_package_versions")
    if not isinstance(observed_versions, int) or observed_versions <= 0:
        raise IntegrityError("OCI mirror observed package version count is invalid")
    if observed_versions > budgets["package_version_hard_limit"]:
        raise BudgetError("OCI mirror package already exceeds its hard version limit")

    if set(images) != EXPECTED_IMAGES:
        raise IntegrityError("OCI mirror inventory mismatch")
    if budgets.get("observed_inventory_images") != len(images):
        raise IntegrityError("OCI mirror observed inventory count mismatch")
    seed = _load_seed()
    seed_images = seed.get("images")
    if not isinstance(seed_images, dict) or set(seed_images) != EXPECTED_IMAGES:
        raise IntegrityError("OCI mirror seed inventory mismatch")
    locked_seed_images = seed_images
    if pending_seed:
        # A pending seed is only a reviewed publication request. The old lock
        # remains the sole build/offline proof authority until main publishes
        # and verifies the replacement mirror generation.
        if any(
            seed.get(field) != published_seed.get(field)
            for field in ("schema_version", "owner", "target", "publisher")
        ):
            raise IntegrityError("pending OCI seed changes immutable publisher policy")
        previous_images = published_seed.get("images")
        if (
            not isinstance(previous_images, dict)
            or set(previous_images) != EXPECTED_IMAGES
        ):
            raise IntegrityError("published OCI seed inventory mismatch")
        digest_updates = 0
        expected_fields = {"canonical", "suites", "load_into_kind"}
        for name in sorted(EXPECTED_IMAGES):
            old_spec, staged_spec = previous_images[name], seed_images[name]
            if (
                not isinstance(old_spec, dict)
                or not isinstance(staged_spec, dict)
                or set(old_spec) != expected_fields
                or set(staged_spec) != expected_fields
                or old_spec["suites"] != staged_spec["suites"]
                or old_spec["load_into_kind"] != staged_spec["load_into_kind"]
            ):
                raise IntegrityError("pending OCI seed changes non-digest image policy")
            old_ref, staged_ref = old_spec["canonical"], staged_spec["canonical"]
            if (
                not isinstance(old_ref, str)
                or not isinstance(staged_ref, str)
                or "@" not in old_ref
                or "@" not in staged_ref
                or old_ref.rsplit("@", 1)[0] != staged_ref.rsplit("@", 1)[0]
                or not FULL_SHA256.fullmatch(staged_ref.rsplit("@", 1)[1])
            ):
                raise IntegrityError(
                    "pending OCI seed changes image identity or pinning"
                )
            digest_updates += old_ref != staged_ref
        if not digest_updates:
            raise IntegrityError("pending OCI seed has no image digest changes")
        locked_seed_images = previous_images
    suite_counts = {
        suite: sum(1 for spec in images.values() if suite in spec.get("suites", []))
        for suite in sorted(ALLOWED_SUITES)
    }
    proof_counts = {
        "kind-gitops": sum(
            1
            for spec in images.values()
            if set(spec.get("suites", [])).intersection(
                {"kind-gitops", "app-build"}
            )
        ),
        "ha-recovery": sum(
            1
            for spec in images.values()
            if set(spec.get("suites", [])).intersection(
                {"ha-recovery", "app-build"}
            )
        ),
    }
    if budgets.get("suite_images") != suite_counts:
        raise IntegrityError("OCI mirror suite image counts drifted")
    if budgets.get("proof_pull_images") != proof_counts:
        raise IntegrityError("OCI mirror proof pull counts drifted")
    for name, spec in images.items():
        if not isinstance(spec, dict) or set(spec) != {
            "canonical",
            "local_ref",
            "mirror",
            "mirror_tag",
            "digest",
            "suites",
            "load_into_kind",
        }:
            raise IntegrityError(f"OCI mirror image {name} has invalid fields")
        digest = spec["digest"]
        if not isinstance(digest, str) or not FULL_SHA256.fullmatch(digest):
            raise IntegrityError(f"OCI mirror image {name} has invalid digest")
        if spec["canonical"].rsplit("@", 1)[-1] != digest:
            raise IntegrityError(f"OCI mirror image {name} canonical digest drift")
        expected_mirror_ref = f"{mirror['repository']}@{digest}"
        if spec["mirror"] != expected_mirror_ref:
            raise IntegrityError(f"OCI mirror image {name} target digest drift")
        expected_tag = f"{mirror['repository']}:{name}-{digest.removeprefix('sha256:')}"
        if spec["mirror_tag"] != expected_tag:
            raise IntegrityError(f"OCI mirror image {name} tag drift")
        suites = spec["suites"]
        if (
            not isinstance(suites, list)
            or not suites
            or len(suites) != len(set(suites))
            or not set(suites).issubset(ALLOWED_SUITES)
        ):
            raise IntegrityError(f"OCI mirror image {name} suite drift")
        if not isinstance(spec["load_into_kind"], bool):
            raise IntegrityError(f"OCI mirror image {name} kind-load drift")
        seed_spec = locked_seed_images[name]
        if (
            spec["canonical"] != seed_spec.get("canonical")
            or spec["suites"] != seed_spec.get("suites")
            or spec["load_into_kind"] != seed_spec.get("load_into_kind")
        ):
            raise IntegrityError(f"OCI mirror image {name} differs from current seed")
        local_ref = spec["local_ref"]
        if not isinstance(local_ref, str) or "@" in local_ref or ":" not in local_ref:
            raise IntegrityError(f"OCI mirror image {name} local reference is invalid")
    return lock


def _lock_sha256() -> str:
    return _sha256(LOCK_PATH)


def _selected_images(lock: dict[str, Any], suites: Iterable[str]) -> list[tuple[str, dict[str, Any]]]:
    requested = tuple(dict.fromkeys(suites))
    if not requested or not set(requested).issubset(ALLOWED_SUITES):
        raise MirrorError(f"unsupported OCI proof suites: {requested}")
    selected = [
        (name, spec)
        for name, spec in sorted(lock["images"].items())
        if set(spec["suites"]).intersection(requested)
    ]
    expected = sum(
        1
        for spec in lock["images"].values()
        if set(spec["suites"]).intersection(requested)
    )
    if len(selected) != expected or not selected:
        raise IntegrityError("OCI mirror suite selection is incomplete")
    return selected


def _repo_digests(reference: str) -> list[str]:
    try:
        raw = _output(
            ["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", reference],
            timeout=60,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise IntegrityError(f"Docker returned malformed RepoDigests for {reference}") from error
    if payload is None:
        return []
    if not isinstance(payload, list) or not all(isinstance(item, str) for item in payload):
        raise IntegrityError(f"Docker returned invalid RepoDigests for {reference}")
    return payload


def _image_id(reference: str) -> str | None:
    try:
        value = _output(
            ["docker", "image", "inspect", "--format", "{{.Id}}", reference],
            timeout=60,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return value if FULL_SHA256.fullmatch(value) else None


def _local_image(spec: dict[str, Any]) -> dict[str, str] | None:
    image_id = _image_id(spec["local_ref"])
    if image_id is None:
        return None
    repo_digests = _repo_digests(spec["local_ref"])
    if spec["mirror"] not in repo_digests:
        return None
    return {"image_id": image_id, "source": "local-verified"}


def _pull_one(name: str, spec: dict[str, Any], budgets: dict[str, Any]) -> dict[str, str]:
    local = _local_image(spec)
    if local is not None:
        return {"name": name, "action": "reused", **local}
    failures: list[str] = []
    attempts = budgets["pull_attempts"]
    backoff = budgets["retry_backoff_seconds"]
    for attempt in range(attempts):
        try:
            _run(["docker", "pull", spec["mirror"]], timeout=900)
            repo_digests = _repo_digests(spec["mirror"])
            if spec["mirror"] not in repo_digests:
                raise IntegrityError(
                    f"controlled mirror returned wrong digest for {name}: {repo_digests}"
                )
            _run(["docker", "tag", spec["mirror"], spec["local_ref"]], timeout=60)
            verified = _local_image(spec)
            if verified is None:
                raise IntegrityError(f"controlled mirror local tag verification failed for {name}")
            return {"name": name, "action": "pulled", **verified}
        except IntegrityError:
            raise
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            failures.append(f"attempt {attempt + 1}/{attempts}: {error}")
            if attempt + 1 < attempts:
                time.sleep(float(backoff[attempt]))
    raise ControlledMirrorUnavailableError(
        f"controlled mirror pull failed for {name}: " + " | ".join(failures)
    )


def load_host(state: Path, suites: Iterable[str]) -> dict[str, Any]:
    lock = _load_lock()
    selected = _selected_images(lock, suites)
    results: dict[str, Any] = {}
    failures: dict[str, Any] = {}
    with _directory_lock(state):
        max_workers = min(lock["budgets"]["max_parallel_pulls"], len(selected))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_pull_one, name, spec, lock["budgets"]): name
                for name, spec in selected
            }
            for future in concurrent.futures.as_completed(futures):
                name = futures[future]
                try:
                    results[name] = future.result()
                except Exception as error:
                    failures[name] = {
                        "failure_class": getattr(error, "failure_class", type(error).__name__),
                        "message": str(error),
                    }
        receipt = {
            "schema_version": 1,
            "status": "pass" if not failures else "fail",
            "strict": True,
            "source": "private-ghcr-digest-mirror",
            "lock_sha256": _lock_sha256(),
            "generation": lock["generation"],
            "suites": list(dict.fromkeys(suites)),
            "selected_count": len(selected),
            "images": results,
            "failures": failures,
        }
        _atomic_write(state / "load-host-receipt.json", receipt)
    if failures:
        raise MirrorError(f"controlled OCI mirror host load failed: {sorted(failures)}")
    return receipt


def verify_host(state: Path, suites: Iterable[str]) -> dict[str, Any]:
    lock = _load_lock()
    selected = _selected_images(lock, suites)
    images: dict[str, Any] = {}
    failures: dict[str, str] = {}
    for name, spec in selected:
        verified = _local_image(spec)
        if verified is None:
            failures[name] = "local image is absent or not bound to the mirror digest"
        else:
            images[name] = verified
    receipt = {
        "schema_version": 1,
        "status": "pass" if not failures else "fail",
        "strict": True,
        "source": "offline-local-mirror-verification",
        "lock_sha256": _lock_sha256(),
        "generation": lock["generation"],
        "suites": list(dict.fromkeys(suites)),
        "selected_count": len(selected),
        "images": images,
        "failures": failures,
    }
    _atomic_write(state / "verify-host-receipt.json", receipt)
    if failures:
        raise IntegrityError(f"offline OCI mirror host verification failed: {sorted(failures)}")
    return receipt


def _kind_nodes(kind: str, cluster: str) -> list[str]:
    raw = _output([kind, "get", "nodes", "--name", cluster], timeout=60)
    nodes = [line.strip() for line in raw.splitlines() if line.strip()]
    if not nodes or any(
        not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", node)
        for node in nodes
    ):
        raise IntegrityError(f"kind returned invalid node inventory for {cluster}")
    return nodes


def _containerd_reference(reference: str) -> str:
    name, separator, digest = reference.partition("@")
    first = name.split("/", 1)[0]
    if "/" not in name:
        canonical = f"docker.io/library/{name}"
    elif first == "localhost" or "." in first or ":" in first:
        canonical = name
    else:
        canonical = f"docker.io/{name}"
    return canonical + (separator + digest if separator else "")


def _kind_image_target(node: str, runtime_ref: str) -> str:
    raw = _output(
        [
            "docker",
            "exec",
            node,
            "ctr",
            "--namespace",
            "k8s.io",
            "images",
            "list",
            f"name=={runtime_ref}",
        ],
        timeout=60,
    )
    rows = [line.split() for line in raw.splitlines() if line.strip()]
    if len(rows) != 2 or len(rows[1]) < 3 or rows[1][0] != runtime_ref:
        raise IntegrityError(
            f"containerd returned invalid image listing for {runtime_ref} on {node}"
        )
    target = rows[1][2]
    if not FULL_SHA256.fullmatch(target):
        raise IntegrityError(
            f"containerd returned invalid target digest for {runtime_ref} on {node}"
        )
    return target


def _kind_cri_image_binding(
    node: str,
    local_ref: str,
    canonical_ref: str,
    locked_digest: str,
    expected_image_id: str,
) -> dict[str, Any]:
    runtime_ref = _containerd_reference(local_ref)
    raw = _output(
        [
            "docker",
            "exec",
            node,
            "crictl",
            "--runtime-endpoint",
            "unix:///run/containerd/containerd.sock",
            "inspecti",
            runtime_ref,
        ],
        timeout=60,
    )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise IntegrityError(
            f"CRI returned malformed image status for {runtime_ref} on {node}"
        ) from error
    status = payload.get("status")
    if not isinstance(status, dict):
        raise IntegrityError(
            f"CRI returned no image status for {runtime_ref} on {node}"
        )
    image_id = status.get("id")
    repo_tags = status.get("repoTags")
    repo_digests = status.get("repoDigests")
    if image_id != expected_image_id:
        raise IntegrityError(
            f"CRI image ID drift for {runtime_ref} on {node}: "
            f"{image_id} != {expected_image_id}"
        )
    if (
        not isinstance(repo_tags, list)
        or runtime_ref not in repo_tags
        or not all(isinstance(item, str) for item in repo_tags)
    ):
        raise IntegrityError(
            f"CRI runtime tag is absent for {runtime_ref} on {node}: {repo_tags}"
        )
    if not isinstance(repo_digests, list) or not all(
        isinstance(item, str) for item in repo_digests
    ):
        raise IntegrityError(
            f"CRI returned invalid repo digests for {runtime_ref} on {node}"
        )
    valid_repo_digests = [
        item
        for item in repo_digests
        if "@" in item and FULL_SHA256.fullmatch(item.rsplit("@", 1)[-1])
    ]
    if len(valid_repo_digests) != len(repo_digests):
        raise IntegrityError(
            f"CRI returned malformed repo digests for {runtime_ref} on {node}"
        )
    canonical_name = _containerd_reference(canonical_ref).rsplit("@", 1)[0]
    preferred = [
        item
        for item in valid_repo_digests
        if item.startswith(f"{runtime_ref}@")
        or item.startswith(f"{canonical_name}@")
    ]
    imported = [
        item
        for item in valid_repo_digests
        if re.fullmatch(
            r"docker\.io/library/import-[0-9]{4}-[0-9]{2}-[0-9]{2}@sha256:[0-9a-f]{64}",
            item,
        )
    ]
    selected = preferred if preferred else imported
    selected_digests = {item.rsplit("@", 1)[-1] for item in selected}
    selected_repo_digest: str | None
    containerd_target_digest: str | None
    if len(selected) == 1 and len(selected_digests) == 1:
        selected_repo_digest = selected[0]
        containerd_target_digest = None
        platform_digest = next(iter(selected_digests))
        platform_evidence_kind = "cri_repo_digest"
    elif not valid_repo_digests:
        selected_repo_digest = None
        containerd_target_digest = _kind_image_target(node, runtime_ref)
        platform_digest = containerd_target_digest
        platform_evidence_kind = "containerd_target_digest"
    else:
        raise IntegrityError(
            f"CRI runtime tag has ambiguous platform evidence for {runtime_ref} "
            f"on {node}: preferred={preferred}, imported={imported}, "
            f"repo_digests={valid_repo_digests}"
        )
    return {
        "node": node,
        "runtime_ref": runtime_ref,
        "image_id": image_id,
        "repo_tags": sorted(repo_tags),
        "repo_digests": sorted(repo_digests),
        "selected_repo_digest": selected_repo_digest,
        "containerd_target_digest": containerd_target_digest,
        "platform_evidence_kind": platform_evidence_kind,
        "locked_index_digest": locked_digest,
        "platform_target_digest": platform_digest,
        "cri_image_status_verified": True,
    }


def _block_kind_registries(node: str) -> dict[str, Any]:
    entries = "".join(
        f"127.0.0.1 {host}\n::1 {host}\n" for host in BLOCKED_REGISTRIES
    )
    marker = "# weltgewebe strict OCI registry blockade"
    command = (
        f"if ! grep -qFx '{marker}' /etc/hosts; then "
        "cat >> /etc/hosts <<'WELTGEWEBE_OCI_BLOCK'\n"
        + marker
        + "\n"
        + entries
        + "WELTGEWEBE_OCI_BLOCK\nfi"
    )
    _run(
        ["docker", "exec", node, "sh", "-ceu", command],
        capture=True,
        timeout=60,
    )
    observed: dict[str, Any] = {}
    for host in BLOCKED_REGISTRIES:
        ipv4_raw = _output(
            ["docker", "exec", node, "getent", "ahostsv4", host], timeout=60
        )
        ipv4 = sorted({line.split()[0] for line in ipv4_raw.splitlines() if line.strip()})
        if ipv4 != ["127.0.0.1"]:
            raise IntegrityError(
                f"kind node registry IPv4 blockade failed for {host} on {node}: {ipv4}"
            )
        try:
            ipv6_raw = _output(
                ["docker", "exec", node, "getent", "ahostsv6", host], timeout=60
            )
        except subprocess.CalledProcessError:
            ipv6_raw = ""
        ipv6 = sorted({line.split()[0] for line in ipv6_raw.splitlines() if line.strip()})
        if ipv6 and ipv6 != ["::1"]:
            raise IntegrityError(
                f"kind node registry IPv6 blockade failed for {host} on {node}: {ipv6}"
            )
        observed[host] = {"ipv4": ipv4, "ipv6": ipv6}
    return {"node": node, "registries": observed}


def load_kind(
    state: Path,
    suites: Iterable[str],
    kind: str,
    cluster: str,
) -> dict[str, Any]:
    if not cluster or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,62}", cluster):
        raise MirrorError("kind cluster name is invalid")
    lock = _load_lock()
    selected = [
        (name, spec)
        for name, spec in _selected_images(lock, suites)
        if spec["load_into_kind"]
    ]
    nodes = _kind_nodes(kind, cluster)
    for name, spec in selected:
        if _local_image(spec) is None:
            raise IntegrityError(f"cannot load unverified mirror image into kind: {name}")
    local_refs = [spec["local_ref"] for _name, spec in selected]
    _run(
        [kind, "load", "docker-image", "--name", cluster, *local_refs],
        capture=True,
        timeout=900,
    )
    loaded: dict[str, Any] = {}
    for name, spec in selected:
        verified = _local_image(spec)
        if verified is None:
            raise IntegrityError(f"verified mirror image disappeared during kind load: {name}")
        digest = spec["digest"]
        digest_ref = f"{spec['local_ref']}@{digest}"
        bindings = [
            _kind_cri_image_binding(
                node,
                spec["local_ref"],
                spec["canonical"],
                digest,
                verified["image_id"],
            )
            for node in nodes
        ]
        runtime_refs = {binding["runtime_ref"] for binding in bindings}
        platform_targets = {
            binding["platform_target_digest"] for binding in bindings
        }
        image_ids = {binding["image_id"] for binding in bindings}
        if len(runtime_refs) != 1 or len(platform_targets) != 1 or len(image_ids) != 1:
            raise IntegrityError(
                f"kind nodes disagree on CRI image binding for {name}: {bindings}"
            )
        loaded[name] = {
            "canonical": spec["canonical"],
            "local_ref": spec["local_ref"],
            "digest_ref": digest_ref,
            "runtime_ref": next(iter(runtime_refs)),
            "locked_index_digest": digest,
            "platform_target_digest": next(iter(platform_targets)),
            "cri_image_id": next(iter(image_ids)),
            "nodes": bindings,
            **verified,
        }
    registry_blockades = [_block_kind_registries(node) for node in nodes]
    receipt = {
        "schema_version": 1,
        "status": "pass",
        "strict": True,
        "source": "offline-local-mirror-verification",
        "lock_sha256": _lock_sha256(),
        "generation": lock["generation"],
        "cluster": cluster,
        "suites": list(dict.fromkeys(suites)),
        "loaded_count": len(loaded),
        "images": loaded,
        "registry_blockades": registry_blockades,
    }
    _atomic_write(state / f"load-kind-{cluster}.json", receipt)
    return receipt


def verify_live_package(state: Path) -> dict[str, Any]:
    lock = _load_lock()
    receipt_path = state / "live-package-receipt.json"
    try:
        raw = _output(
            [
                "gh",
                "api",
                "orgs/heimgewebe/packages/container/weltgewebe-proof-oci",
            ],
            timeout=60,
        )
        package = json.loads(raw)
    except (
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
    ) as error:
        receipt = {
            "schema_version": 1,
            "status": "fail",
            "failure_class": "controlled_mirror_unavailable",
            "message": str(error),
            "lock_sha256": _lock_sha256(),
        }
        _atomic_write(receipt_path, receipt)
        raise ControlledMirrorUnavailableError(
            f"live OCI mirror package metadata is unavailable: {error}"
        ) from error

    repository = package.get("repository") or {}
    expected = {
        "id": lock["mirror"]["package_id"],
        "name": "weltgewebe-proof-oci",
        "package_type": "container",
        "visibility": "private",
        "repository": "heimgewebe/commonthing",
    }
    observed = {
        "id": package.get("id"),
        "name": package.get("name"),
        "package_type": package.get("package_type"),
        "visibility": package.get("visibility"),
        "repository": repository.get("full_name"),
    }
    version_count = package.get("version_count")
    hard_limit = lock["budgets"]["package_version_hard_limit"]
    failure = None
    if observed != expected:
        failure = "package identity, privacy, or repository binding drifted"
    elif not isinstance(version_count, int) or version_count <= 0:
        failure = "package version count is invalid"
    elif version_count > hard_limit:
        failure = "package version hard limit exceeded"
    receipt = {
        "schema_version": 1,
        "status": "pass" if failure is None else "fail",
        "failure_class": None if failure is None else "integrity_or_budget_mismatch",
        "message": failure,
        "lock_sha256": _lock_sha256(),
        "generation": lock["generation"],
        "expected": expected,
        "observed": observed,
        "version_count": version_count,
        "package_version_hard_limit": hard_limit,
        "version_headroom": hard_limit - version_count
        if isinstance(version_count, int)
        else None,
    }
    _atomic_write(receipt_path, receipt)
    if failure is not None:
        if isinstance(version_count, int) and version_count > hard_limit:
            raise BudgetError(failure)
        raise IntegrityError(failure)
    return receipt


def retention_report() -> dict[str, Any]:
    lock = _load_lock()
    budgets = lock["budgets"]
    observed = budgets["observed_package_versions"]
    hard_limit = budgets["package_version_hard_limit"]
    return {
        "schema_version": 1,
        "status": "pass" if observed <= hard_limit else "fail",
        "mirror": lock["mirror"],
        "generation": lock["generation"],
        "observed_package_versions": observed,
        "package_version_hard_limit": hard_limit,
        "version_headroom": hard_limit - observed,
        "observed_inventory_images": budgets["observed_inventory_images"],
        "max_inventory_images": budgets["max_inventory_images"],
        "proof_pull_images": budgets["proof_pull_images"],
        "orphan_grace_days": budgets["orphan_grace_days"],
        "cleanup_scope": budgets["cleanup_scope"],
        "unbounded_growth_prevented": observed <= hard_limit,
    }


def validate_contract() -> dict[str, Any]:
    lock = _load_lock()
    retention = retention_report()
    return {
        "schema_version": 1,
        "status": "pass",
        "owner": lock["owner"],
        "source_kind": "private-ghcr-digest-mirror",
        "mirror_repository": lock["mirror"]["repository"],
        "visibility": lock["mirror"]["visibility"],
        "repository_binding": lock["mirror"]["repository_binding"],
        "image_count": len(lock["images"]),
        "lock_sha256": _lock_sha256(),
        "generation": lock["generation"],
        "retention": retention,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate")
    host = sub.add_parser("load-host")
    host.add_argument("--suite", action="append", required=True)
    verify = sub.add_parser("verify-host")
    verify.add_argument("--suite", action="append", required=True)
    kind = sub.add_parser("load-kind")
    kind.add_argument("--suite", action="append", required=True)
    kind.add_argument("--kind", required=True)
    kind.add_argument("--cluster", required=True)
    sub.add_parser("verify-live")
    sub.add_parser("retention-report")
    args = parser.parse_args()
    try:
        if args.command == "validate":
            payload = validate_contract()
        elif args.command == "load-host":
            payload = load_host(args.state, args.suite)
        elif args.command == "verify-host":
            payload = verify_host(args.state, args.suite)
        elif args.command == "load-kind":
            payload = load_kind(args.state, args.suite, args.kind, args.cluster)
        elif args.command == "verify-live":
            payload = verify_live_package(args.state)
        else:
            payload = retention_report()
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    except (
        MirrorError,
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as error:
        print(f"OCI proof mirror failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())