#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

import kind_reference as ref

import ha_availability as _ha_availability
import ha_dependencies as _ha_dependencies
import ha_migration as _ha_migration
from ha_availability import (
    BASELINE_API_IMAGE, CONTINUOUS_AVAILABILITY_INTERVAL_SECONDS, DIRECT_API_PROBE_LABEL,
    ContinuousAvailabilityMonitor, api_readiness_failure_diagnostic, create_direct_api_probes,
    delete_direct_api_probes, direct_api_probe_document, direct_api_probe_sample,
    direct_api_probe_target, direct_api_projection_sample, gateway_observer_binding,
    gateway_observer_sample, gateway_projection_available, gateway_projection_sample,
    projection_sample_url, summarize_continuous_availability, summarize_failover_recovery,
)
from ha_common import _load_yaml_documents, current_primary, psql, sha256_text, wait_until
from ha_dependencies import NATS_BOX_IMAGE, apply_ha_data, ha_nats_runtime_image, preload_local_nats_dependencies
from ha_migration import (
    finish_migration_election_probe, migration_data_egress_document, migration_failed_attempts,
    migration_history_digest, migration_job_document, migration_network_partition_document,
    prove_migration_retry_and_idempotency, remove_migration_data_egress,
    remove_migration_network_partition, rerun_migration_job, start_migration_election_probe,
)
from ha_wal import (
    S3_BUCKET, object_store_wal_evidence, require_wal_object_identity, s3_list_object_keys,
    wal_archived_at_or_after, wal_object_key_matches, wal_segment_position,
)

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / ".cache/weltgewebe-platform"
DEFAULT_CLUSTER = "weltgewebe-ha-reference"
CNPG_OPERATOR_IMAGE = "ghcr.io/cloudnative-pg/cloudnative-pg@sha256:a2701eb97cdd2a34b1fdb2cb51987f544b706e40bec72ae7146cd8580efefebb"
CERT_MANAGER_IMAGES = {
    "quay.io/jetstack/cert-manager-controller:v1.21.0": "quay.io/jetstack/cert-manager-controller@sha256:e370f7800a53078e9d74324287a7d52b553864e55f5b4e521f911c3f6c7da203",
    "quay.io/jetstack/cert-manager-cainjector:v1.21.0": "quay.io/jetstack/cert-manager-cainjector@sha256:ad1dcc5b2fccc420f9b3fbee7ce8a869450c540fd4f2f41de2d95b1ca0c4d701",
    "quay.io/jetstack/cert-manager-webhook:v1.21.0": "quay.io/jetstack/cert-manager-webhook@sha256:c33cca307541e2d58861a55b1af5f390b7e19c8741e48b433693b73a7cce88b3",
}
BARMAN_CLOUD_PLUGIN_IMAGE = "ghcr.io/cloudnative-pg/plugin-barman-cloud@sha256:71589dbac582333442812b07b31f7ea4d00324a8358aac7ca507dabf9f4b6c96"
BARMAN_CLOUD_SIDECAR_IMAGE = "ghcr.io/cloudnative-pg/plugin-barman-cloud-sidecar@sha256:990361af3319f9e23aafa0f6d7981f99bf1f69b4e6a85cf1bc7d71d6f09bb288"
POSTGRES_IMAGE = "ghcr.io/cloudnative-pg/postgresql:16.14@sha256:05eae7037dc6a7077cc3fc91a65fe023279060572a237d11aa83e11179443ad1"
SEAWEEDFS_IMAGE = "chrislusf/seaweedfs@sha256:f898c91e42d7da5f4bb13f1efd424ff03ba85b420312eb929708a384e8a8b03d"
APP_USER = "welt"
S3_ACCESS_KEY_PREFIX = "wg-ha-"
UPGRADE_API_IMAGE = "weltgewebe-api:ha-upgrade-candidate"
REFERENCE_AVAILABILITY_OBJECTIVE = 0.999
PROOF_WAL_RESERVE_BYTES = 1 << 30
PROOF_PITR_RESERVE_BYTES = 2 << 30
PROOF_KIND_NODE_RESERVE_BYTES = 1 << 30
PROOF_IMAGE_BUILD_RESERVE_BYTES = 4 << 30
PROOF_MIN_FREE_INODES = 100_000


class ClusterLifecycle:
    def __init__(
        self, *, primary_created: bool = False, restore_created: bool = False
    ) -> None:
        self.primary_created = primary_created
        self.restore_created = restore_created


def run_with_environment(argv: list[str], values: dict[str, str], *, timeout: int | None = None) -> None:
    print("+", " ".join(argv), f"[env: {','.join(sorted(values))}]", flush=True)
    environment = os.environ.copy()
    environment.update(values)
    subprocess.run(argv, cwd=ROOT, check=True, text=True, env=environment, timeout=timeout)


def create_kind_cluster(
    kind: str,
    name: str,
    image: str,
    config: str,
    commit: str,
    owner_id: str,
) -> None:
    ref.create_kind_cluster(
        kind,
        name,
        image,
        config,
        commit,
        owner_id,
        timeout=900,
    )


def require_active_cluster_context(kind: str, kubectl: str, cluster: str) -> str:
    kubeconfig = ref.configure_cluster_access(kind, cluster)
    expected_context = f"kind-{cluster}"
    observed_context = ref.output([kubectl, "config", "current-context"])
    if observed_context != expected_context:
        raise ref.ProofError(
            f"kubectl context mismatch for {cluster}: {observed_context!r}"
        )
    if os.environ.get("KUBECONFIG") != str(kubeconfig):
        raise ref.ProofError(
            f"kubectl kubeconfig mismatch for {cluster}: "
            f"{os.environ.get('KUBECONFIG')!r}"
        )
    return kubectl


def apply_secret_contracts(
    kubectl: str,
    app_password: str,
    s3_access_key: str,
    s3_secret_key: str,
) -> None:
    ref.apply_yaml(
        kubectl,
        [
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": "weltgewebe-ha-s3", "namespace": "weltgewebe-data"},
                "type": "Opaque",
                "stringData": {
                    "ACCESS_KEY_ID": s3_access_key,
                    "ACCESS_SECRET_KEY": s3_secret_key,
                },
            },
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": "weltgewebe-ha-app", "namespace": "weltgewebe-data"},
                "type": "kubernetes.io/basic-auth",
                "stringData": {"username": APP_USER, "password": app_password},
            },
        ],
    )


def postgres_runtime_contract() -> dict[str, str]:
    path = ROOT / "platform/infrastructure/ha-data/postgres.yaml"
    clusters = [
        document
        for document in yaml.safe_load_all(path.read_text(encoding="utf-8"))
        if isinstance(document, dict) and document.get("kind") == "Cluster"
    ]
    if len(clusters) != 1:
        raise ref.ProofError(
            f"expected exactly one PostgreSQL Cluster contract in {path}, got {len(clusters)}"
        )
    name = str(clusters[0].get("metadata", {}).get("name", "")).strip()
    if not name:
        raise ref.ProofError("PostgreSQL Cluster contract has no metadata.name")
    return {"cluster": name, "primary_service": f"{name}-rw"}


def apply_runtime_secret(
    kubectl: str,
    app_password: str,
    *,
    postgres_service: str,
) -> None:
    database_url = (
        f"postgres://{APP_USER}:{app_password}@{postgres_service}.weltgewebe-data.svc.cluster.local:5432/weltgewebe"
    )
    ref.apply_yaml(
        kubectl,
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "weltgewebe-ha-runtime", "namespace": "weltgewebe"},
            "type": "Opaque",
            "stringData": {"database-url": database_url},
        },
    )


def parse_kubernetes_bytes(value: str) -> int:
    raw = value.strip()
    units = {"Ki": 1 << 10, "Mi": 1 << 20, "Gi": 1 << 30}
    for suffix, multiplier in units.items():
        if raw.endswith(suffix):
            number = raw[: -len(suffix)]
            if not number.isdigit():
                break
            return int(number) * multiplier
    if raw.isdigit():
        return int(raw)
    raise ref.ProofError(f"unsupported Kubernetes byte quantity: {value!r}")


def declared_ha_capacity_budget(*, keep_primary: bool) -> dict[str, Any]:
    postgres = next(
        document
        for document in yaml.safe_load_all(
            (ROOT / "platform/infrastructure/ha-data/postgres.yaml").read_text(
                encoding="utf-8"
            )
        )
        if isinstance(document, dict) and document.get("kind") == "Cluster"
    )
    nats = next(
        document
        for document in yaml.safe_load_all(
            (ROOT / "platform/infrastructure/ha-data/nats.yaml").read_text(
                encoding="utf-8"
            )
        )
        if isinstance(document, dict) and document.get("kind") == "StatefulSet"
    )
    postgres_bytes = int(postgres["spec"]["instances"]) * parse_kubernetes_bytes(
        str(postgres["spec"]["storage"]["size"])
    )
    nats_bytes = int(nats["spec"]["replicas"]) * parse_kubernetes_bytes(
        str(
            nats["spec"]["volumeClaimTemplates"][0]["spec"]["resources"][
                "requests"
            ]["storage"]
        )
    )
    restore = restore_cluster_document("2000-01-01T00:00:00Z")
    restore_bytes = int(restore["spec"]["instances"]) * parse_kubernetes_bytes(
        str(restore["spec"]["storage"]["size"])
    )
    primary_kind = yaml.safe_load(
        (ROOT / "platform/clusters/ha/kind.yaml").read_text(encoding="utf-8")
    )
    restore_kind = yaml.safe_load(
        (ROOT / "platform/clusters/ha/restore-kind.yaml").read_text(
            encoding="utf-8"
        )
    )
    primary_nodes = len(primary_kind.get("nodes", []))
    restore_nodes = len(restore_kind.get("nodes", []))
    concurrent_pvc_bytes = (
        postgres_bytes + nats_bytes + restore_bytes
        if keep_primary
        else max(postgres_bytes + nats_bytes, restore_bytes)
    )
    concurrent_kind_nodes = (
        primary_nodes + restore_nodes if keep_primary else max(primary_nodes, restore_nodes)
    )
    kind_container_bytes = concurrent_kind_nodes * PROOF_KIND_NODE_RESERVE_BYTES
    required_free_bytes = (
        concurrent_pvc_bytes
        + PROOF_WAL_RESERVE_BYTES
        + PROOF_PITR_RESERVE_BYTES
        + kind_container_bytes
        + PROOF_IMAGE_BUILD_RESERVE_BYTES
    )
    return {
        "postgres_primary_pvc_bytes": postgres_bytes,
        "nats_pvc_bytes": nats_bytes,
        "pitr_restore_pvc_bytes": restore_bytes,
        "wal_object_store_reserve_bytes": PROOF_WAL_RESERVE_BYTES,
        "pitr_object_store_reserve_bytes": PROOF_PITR_RESERVE_BYTES,
        "kind_container_reserve_bytes": kind_container_bytes,
        "image_build_reserve_bytes": PROOF_IMAGE_BUILD_RESERVE_BYTES,
        "primary_kind_nodes": primary_nodes,
        "restore_kind_nodes": restore_nodes,
        "concurrent_pvc_bytes": concurrent_pvc_bytes,
        "required_free_bytes": required_free_bytes,
        "keep_primary": keep_primary,
    }


def _filesystem_capacity(path: Path) -> dict[str, int]:
    try:
        resolved = path.resolve(strict=True)
        fs = os.statvfs(resolved)
        device = os.stat(resolved).st_dev
    except OSError as error:
        raise ref.ProofError(
            f"cannot inspect HA proof filesystem capacity: {type(error).__name__}"
        ) from error
    return {
        "free_bytes": fs.f_frsize * fs.f_bavail,
        "free_inodes": fs.f_favail,
        "device": int(device),
    }


def capacity_preflight(*, keep_primary: bool) -> dict[str, Any]:
    budget = declared_ha_capacity_budget(keep_primary=keep_primary)
    docker_root_raw = ref.output(["docker", "info", "--format", "{{.DockerRootDir}}"])
    docker_root = Path(docker_root_raw)
    if not docker_root.is_absolute():
        raise ref.ProofError("DockerRootDir is not an absolute path")
    repository_fs = _filesystem_capacity(ROOT)
    docker_fs = _filesystem_capacity(docker_root)
    for label, observed in (("repository", repository_fs), ("docker", docker_fs)):
        if observed["free_bytes"] < budget["required_free_bytes"]:
            raise ref.ProofError(
                f"insufficient {label} filesystem space for HA proof: "
                f"required={budget['required_free_bytes']} observed={observed['free_bytes']}"
            )
        if observed["free_inodes"] < PROOF_MIN_FREE_INODES:
            raise ref.ProofError(
                f"insufficient {label} filesystem inodes for HA proof: "
                f"required={PROOF_MIN_FREE_INODES} observed={observed['free_inodes']}"
            )
    shared_filesystem = repository_fs["device"] == docker_fs["device"]
    return {
        **budget,
        "repository_filesystem": {
            "free_bytes": repository_fs["free_bytes"],
            "free_inodes": repository_fs["free_inodes"],
        },
        "docker_filesystem": {
            "free_bytes": docker_fs["free_bytes"],
            "free_inodes": docker_fs["free_inodes"],
        },
        "repository_and_docker_share_filesystem": shared_filesystem,
        "minimum_free_inodes": PROOF_MIN_FREE_INODES,
        "budget_model": (
            "declared PVC plus WAL/PITR, concurrent kind-container and image-build reserves; "
            "the full conservative budget must fit every backing filesystem"
        ),
        "status": "pass",
    }


def credential_preflight(
    s3_access_key: str, s3_secret_key: str, owner_id: str
) -> dict[str, Any]:
    expected_access_length = len(S3_ACCESS_KEY_PREFIX) + 16
    if (
        len(s3_access_key) != expected_access_length
        or not s3_access_key.startswith(S3_ACCESS_KEY_PREFIX)
        or any(character not in "0123456789abcdef" for character in s3_access_key[len(S3_ACCESS_KEY_PREFIX):])
    ):
        raise ref.ProofError("HA proof access key is not run-scoped")
    if len(s3_secret_key) < 32:
        raise ref.ProofError("HA proof object-store secret is unexpectedly short")
    return {
        "status": "pass",
        "credential_lifetime": "single-proof-run",
        "permission_scope": (
            "dedicated ephemeral proof object-store/bucket only; Barman receives read/write "
            "archive/restore access and the verifier uses signed read-only object listing"
        ),
        "external_account_credentials_used": False,
        "repository_secret_used": False,
        "access_key_sha256": sha256_text(s3_access_key),
        "secret_material_recorded": False,
        "owner_id_sha256": sha256_text(owner_id),
    }


def external_object_store_names(cluster: str) -> tuple[str, str]:
    normalized = cluster.replace("_", "-").replace(".", "-")
    return f"{normalized}-object-store", f"{normalized}-object-store-data"


def external_object_store_binding(
    cluster: str, commit: str, owner_id: str
) -> dict[str, str]:
    ref.validate_ownership_binding(commit, owner_id)
    return {
        "weltgewebe.net/proof-repository": str(ROOT),
        "weltgewebe.net/proof-cluster": cluster,
        "weltgewebe.net/proof-commit": commit,
        "weltgewebe.net/proof-owner-id": owner_id,
    }


def _docker_label_args(labels: dict[str, str]) -> list[str]:
    return [
        argument
        for key, value in sorted(labels.items())
        for argument in ("--label", f"{key}={value}")
    ]


def _object_store_labels(resource_kind: str, name: str) -> dict[str, str] | None:
    if resource_kind == "container":
        argv = [
            "docker",
            "container",
            "inspect",
            "--format",
            "{{json .Config.Labels}}",
            name,
        ]
    elif resource_kind == "volume":
        argv = [
            "docker",
            "volume",
            "inspect",
            "--format",
            "{{json .Labels}}",
            name,
        ]
    else:
        raise ValueError(f"unsupported object-store resource kind: {resource_kind}")
    result = subprocess.run(
        argv, cwd=ROOT, text=True, capture_output=True, timeout=30
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        missing_markers = (
            "no such container",
            "no such volume",
            "no such object",
        )
        if any(marker in detail.casefold() for marker in missing_markers):
            return None
        raise ref.ProofError(
            f"failed to inspect external object-store {resource_kind} {name}: "
            f"{detail or f'exit {result.returncode}'}"
        )
    try:
        labels = json.loads(result.stdout.strip() or "null")
    except json.JSONDecodeError as error:
        raise ref.ProofError(
            f"external object-store {resource_kind} labels are unreadable: {name}"
        ) from error
    if not isinstance(labels, dict):
        raise ref.ProofError(
            f"external object-store {resource_kind} has no label object: {name}"
        )
    return {str(key): str(value) for key, value in labels.items()}


def _require_object_store_binding(
    resource_kind: str,
    name: str,
    observed: dict[str, str],
    expected: dict[str, str],
) -> None:
    mismatched = {
        key: {"expected": value, "observed": observed.get(key)}
        for key, value in expected.items()
        if observed.get(key) != value
    }
    if mismatched:
        raise ref.ProofError(
            f"refusing to delete foreign object-store {resource_kind} {name}: "
            f"{json.dumps(mismatched, sort_keys=True)}"
        )


def external_object_store_runtime_image() -> str:
    if not ref.controlled_oci_strict():
        return SEAWEEDFS_IMAGE
    lock = ref._oci_mirror_lock()
    spec = lock.get("images", {}).get("seaweedfs")
    if not isinstance(spec, dict):
        raise ref.ProofError("strict OCI mirror lock is missing seaweedfs")
    expected_canonical = f"docker.io/{SEAWEEDFS_IMAGE}"
    if spec.get("canonical") != expected_canonical:
        raise ref.ProofError("strict OCI seaweedfs canonical reference drift")
    local_ref = spec.get("local_ref")
    if not isinstance(local_ref, str) or not local_ref or "@" in local_ref:
        raise ref.ProofError("strict OCI seaweedfs local reference is invalid")
    return local_ref


def start_external_object_store(
    cluster: str,
    commit: str,
    owner_id: str,
    s3_access_key: str,
    s3_secret_key: str,
) -> tuple[str, str, str]:
    container, volume = external_object_store_names(cluster)
    binding = external_object_store_binding(cluster, commit, owner_id)
    if _object_store_labels("container", container) is not None:
        raise ref.ProofError(f"external object-store container already exists: {container}")
    if _object_store_labels("volume", volume) is not None:
        raise ref.ProofError(f"external object-store volume already exists: {volume}")
    ref.run(
        ["docker", "volume", "create", *_docker_label_args(binding), volume]
    )
    try:
        run_with_environment(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                container,
                *_docker_label_args(binding),
                "--network",
                "kind",
                "--env",
                "AWS_ACCESS_KEY_ID",
                "--env",
                "AWS_SECRET_ACCESS_KEY",
                "--env",
                "S3_BUCKET",
                "--volume",
                f"{volume}:/data",
                external_object_store_runtime_image(),
                "mini",
                "-dir=/data",
                "-ip=0.0.0.0",
            ],
            {
                "AWS_ACCESS_KEY_ID": s3_access_key,
                "AWS_SECRET_ACCESS_KEY": s3_secret_key,
                "S3_BUCKET": S3_BUCKET,
            },
            timeout=120,
        )

        def address_probe() -> str | bool:
            address = ref.output(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{(index .NetworkSettings.Networks \"kind\").IPAddress}}",
                    container,
                ]
            )
            return address if address else False

        address = str(
            wait_until(
                "external object-store address", address_probe, timeout_seconds=60
            )
        )

        def port_probe() -> bool:
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    container,
                    "sh",
                    "-c",
                    "wget -qO- http://127.0.0.1:9333/cluster/status >/dev/null",
                ],
                capture_output=True,
            )
            return result.returncode == 0

        wait_until("external object-store readiness", port_probe, timeout_seconds=180)
        return container, volume, address
    except Exception as error:
        cleanup = reconcile_external_object_store(cluster, commit, owner_id)
        if cleanup["errors"]:
            raise ref.ProofError(
                "external object-store start failed and exact-owner cleanup was incomplete: "
                f"{json.dumps(cleanup, sort_keys=True)}"
            ) from error
        raise


def reconcile_external_object_store(
    cluster: str, commit: str, owner_id: str
) -> dict[str, Any]:
    expected = external_object_store_binding(cluster, commit, owner_id)
    container, volume = external_object_store_names(cluster)
    resources: dict[str, str] = {}
    errors: dict[str, str] = {}
    for resource_key, resource_kind, name, delete_argv in (
        (
            "object_store_container",
            "container",
            container,
            ["docker", "rm", "--force", container],
        ),
        (
            "object_store_volume",
            "volume",
            volume,
            ["docker", "volume", "rm", volume],
        ),
    ):
        try:
            labels = _object_store_labels(resource_kind, name)
            if labels is None:
                resources[resource_key] = "absent"
                continue
            _require_object_store_binding(resource_kind, name, labels, expected)
            ref.run(delete_argv)
            resources[resource_key] = "deleted"
        except (
            ref.ProofError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            OSError,
        ) as error:
            resources[resource_key] = "error"
            errors[resource_key] = str(error)
    return {"resources": resources, "errors": errors}


def delete_external_object_store(
    cluster: str, commit: str, owner_id: str
) -> dict[str, Any]:
    result = reconcile_external_object_store(cluster, commit, owner_id)
    if result["errors"]:
        raise ref.ProofError(
            "external object-store cleanup incomplete: "
            f"{json.dumps(result, sort_keys=True)}"
        )
    return result


def reconcile_owned_ha_resources(
    kind: str,
    cluster: str,
    commit: str,
    owner_id: str,
    *,
    include_restore: bool = True,
    include_primary: bool = True,
    include_object_store: bool = True,
) -> dict[str, Any]:
    ref.validate_ownership_binding(commit, owner_id)
    resources: dict[str, str] = {}
    errors: dict[str, str] = {}
    cluster_resources = (
        ("restore_cluster", f"{cluster}-restore", include_restore),
        ("primary_cluster", cluster, include_primary),
    )
    for resource_key, name, included in cluster_resources:
        if not included:
            continue
        try:
            deleted = ref.delete_owned_cluster_if_present(
                kind,
                name,
                expected_commit=commit,
                expected_owner_id=owner_id,
            )
            resources[resource_key] = "deleted" if deleted else "absent"
        except (
            ref.ProofError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            OSError,
        ) as error:
            resources[resource_key] = "error"
            errors[resource_key] = str(error)

    if include_object_store:
        object_store = reconcile_external_object_store(cluster, commit, owner_id)
        resources.update(object_store["resources"])
        errors.update(object_store["errors"])

    deleted_any = any(value == "deleted" for value in resources.values())
    if errors:
        status = "partial" if deleted_any else "error"
    else:
        status = "deleted" if deleted_any else "absent"
    return {
        "status": status,
        "cluster": cluster,
        "commit": commit,
        "owner_id": owner_id,
        "resources": resources,
        "errors": errors,
    }


def retire_primary_cluster_before_restore(
    kind: str, cluster: str, commit: str, owner_id: str
) -> dict[str, Any]:
    # Retire only the ownership-bound primary. The future restore cluster and the
    # external object store must remain untouched so the blank restore can start.
    result = reconcile_owned_ha_resources(
        kind,
        cluster,
        commit,
        owner_id,
        include_restore=False,
        include_primary=True,
        include_object_store=False,
    )
    resources = result.get("resources")
    primary_state = (
        resources.get("primary_cluster") if isinstance(resources, dict) else None
    )
    # "absent" is not enough: this proof must establish that this exact run
    # deleted the ownership-bound primary instead of merely observing it gone.
    if result.get("errors") or primary_state != "deleted":
        raise ref.ProofError(
            "primary cluster retirement before blank restore failed "
            f"(expected primary_cluster='deleted', got {primary_state!r}): "
            f"{json.dumps(result, sort_keys=True)}"
        )
    return result


def create_blank_restore_cluster(
    kind: str,
    primary_name: str,
    restore_name: str,
    node_image: str,
    commit: str,
    owner_id: str,
    *,
    keep_primary: bool,
    lifecycle: ClusterLifecycle,
) -> dict[str, Any] | None:
    retirement: dict[str, Any] | None = None
    if not keep_primary:
        retirement = retire_primary_cluster_before_restore(
            kind, primary_name, commit, owner_id
        )
        # The primary is gone now. Exception cleanup must not diagnose or delete
        # it again if creation of the blank restore cluster fails afterwards.
        lifecycle.primary_created = False

    create_kind_cluster(
        kind,
        restore_name,
        node_image,
        "platform/clusters/ha/restore-kind.yaml",
        commit,
        owner_id,
    )
    lifecycle.restore_created = True
    return retirement


def apply_object_store_endpoint(kubectl: str, address: str) -> None:
    ref.apply_yaml(
        kubectl,
        {
            "apiVersion": "discovery.k8s.io/v1",
            "kind": "EndpointSlice",
            "metadata": {
                "name": "seaweedfs-s3",
                "namespace": "weltgewebe-data",
                "labels": {"kubernetes.io/service-name": "seaweedfs-s3"},
            },
            "addressType": "IPv4",
            "ports": [{"name": "s3", "protocol": "TCP", "port": 8333}],
            "endpoints": [{"addresses": [address], "conditions": {"ready": True}}],
        },
    )


def cnpg_webhook_ready(kubectl: str) -> bool:
    endpoint = subprocess.run(
        [
            kubectl,
            "-n",
            "cnpg-system",
            "get",
            "endpoints",
            "cnpg-webhook-service",
            "-o",
            "jsonpath={.subsets[0].addresses[0].ip}",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=15,
    )
    if endpoint.returncode != 0 or not endpoint.stdout.strip():
        return False
    probe = {
        "apiVersion": "postgresql.cnpg.io/v1",
        "kind": "Cluster",
        "metadata": {"name": "weltgewebe-cnpg-webhook-probe", "namespace": "default"},
        "spec": {"instances": 1, "storage": {"size": "1Gi"}},
    }
    result = subprocess.run(
        [
            kubectl,
            "apply",
            "--server-side",
            "--dry-run=server",
            "--field-manager=weltgewebe-ha-proof",
            "-f",
            "-",
        ],
        cwd=ROOT,
        input=json.dumps(probe),
        text=True,
        capture_output=True,
        timeout=20,
    )
    return result.returncode == 0


def _replace_exact_yaml_scalar(
    value: Any, expected: str, replacement: str
) -> tuple[Any, int]:
    if isinstance(value, dict):
        rewritten: dict[Any, Any] = {}
        count = 0
        for key, item in value.items():
            rewritten_item, observed = _replace_exact_yaml_scalar(
                item, expected, replacement
            )
            rewritten[key] = rewritten_item
            count += observed
        return rewritten, count
    if isinstance(value, list):
        rewritten_items: list[Any] = []
        count = 0
        for item in value:
            rewritten_item, observed = _replace_exact_yaml_scalar(
                item, expected, replacement
            )
            rewritten_items.append(rewritten_item)
            count += observed
        return rewritten_items, count
    if value == expected:
        return replacement, 1
    return value, 0


def rewrite_yaml_documents(
    source: str,
    replacements: dict[str, tuple[str, int]],
    release_name: str,
) -> list[Any]:
    documents = _load_yaml_documents(source, release_name)
    for tagged, (digest_bound, expected_count) in replacements.items():
        rewritten_documents: list[Any] = []
        observed_count = 0
        for document in documents:
            rewritten, observed = _replace_exact_yaml_scalar(
                document, tagged, digest_bound
            )
            rewritten_documents.append(rewritten)
            observed_count += observed
        if observed_count != expected_count:
            raise ref.ProofError(
                f"{release_name} image reference changed unexpectedly: "
                f"{tagged} occurred {observed_count} times, expected {expected_count}"
            )
        documents = rewritten_documents
    return documents


def render_cnpg_manifest(source: str) -> str:
    tagged = "ghcr.io/cloudnative-pg/cloudnative-pg:1.30.0"
    runtime_image = ref.controlled_oci_runtime_image(CNPG_OPERATOR_IMAGE)
    documents = rewrite_yaml_documents(
        source,
        {tagged: (runtime_image, 2)},
        "CloudNativePG release",
    )
    deployments = [
        document
        for document in documents
        if isinstance(document, dict)
        and document.get("kind") == "Deployment"
        and document.get("metadata", {}).get("name")
        == "cnpg-controller-manager"
        and document.get("metadata", {}).get("namespace") == "cnpg-system"
    ]
    if len(deployments) != 1:
        raise ref.ProofError(
            "CloudNativePG controller deployment changed unexpectedly"
        )
    deployment = deployments[0]
    spec = deployment.setdefault("spec", {})
    spec["replicas"] = 3
    spec["strategy"] = {
        "type": "RollingUpdate",
        "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1},
    }
    pod_spec = spec.setdefault("template", {}).setdefault("spec", {})
    affinity = pod_spec.setdefault("affinity", {})
    pod_anti_affinity = affinity.setdefault("podAntiAffinity", {})
    required = pod_anti_affinity.setdefault(
        "requiredDuringSchedulingIgnoredDuringExecution", []
    )
    if required:
        raise ref.ProofError(
            "CloudNativePG release already defines required pod anti-affinity"
        )
    required.append(
        {
            "labelSelector": {
                "matchLabels": {"app.kubernetes.io/name": "cloudnative-pg"}
            },
            "topologyKey": "kubernetes.io/hostname",
        }
    )
    documents = ref.enforce_controlled_oci_pull_policy(documents)
    return yaml.safe_dump_all(documents, sort_keys=False, explicit_start=True)

def verify_cnpg_operator_ha(kubectl: str) -> list[str]:
    ref.wait_rollout(
        kubectl,
        "cnpg-system",
        "deployment/cnpg-controller-manager",
        "8m",
    )
    replicas = ref.output(
        [
            kubectl,
            "-n",
            "cnpg-system",
            "get",
            "deployment/cnpg-controller-manager",
            "-o",
            "jsonpath={.status.availableReplicas}",
        ]
    )
    if replicas != "3":
        raise ref.ProofError(
            f"CloudNativePG operator does not have three available replicas: {replicas}"
        )
    pods = json.loads(
        ref.output(
            [
                kubectl,
                "-n",
                "cnpg-system",
                "get",
                "pods",
                "-l",
                "app.kubernetes.io/name=cloudnative-pg",
                "-o",
                "json",
            ]
        )
    )
    nodes = {
        item.get("spec", {}).get("nodeName")
        for item in pods.get("items", [])
        if item.get("status", {}).get("phase") == "Running"
        and item.get("spec", {}).get("nodeName")
    }
    if len(nodes) != 3:
        raise ref.ProofError(
            f"CloudNativePG operator replicas are not spread across three nodes: {sorted(nodes)}"
        )
    return sorted(nodes)


def install_cnpg(kubectl: str, artifact: str) -> list[str]:
    source = render_cnpg_manifest(
        Path(artifact).read_text(encoding="utf-8")
    )
    ref.run(
        [
            kubectl,
            "apply",
            "--server-side",
            "--force-conflicts",
            "--field-manager=weltgewebe-ha-proof",
            "-f",
            "-",
        ],
        input_text=source,
    )
    ref.run(
        [
            kubectl,
            "wait",
            "--for=condition=Established",
            "crd/clusters.postgresql.cnpg.io",
            "--timeout=3m",
        ]
    )
    operator_nodes = verify_cnpg_operator_ha(kubectl)
    wait_until(
        "CloudNativePG admission webhook",
        lambda: cnpg_webhook_ready(kubectl),
        timeout_seconds=180,
        interval=2,
    )
    observed = ref.output(
        [
            kubectl,
            "-n",
            "cnpg-system",
            "get",
            "deployment/cnpg-controller-manager",
            "-o",
            "jsonpath={.spec.template.spec.containers[0].image}",
        ]
    )
    expected_runtime_image = ref.controlled_oci_runtime_image(CNPG_OPERATOR_IMAGE)
    if observed != expected_runtime_image:
        raise ref.ProofError(
            f"CloudNativePG operator image is not runtime-bound: {observed}"
        )
    return operator_nodes


def apply_digest_locked_manifest(
    kubectl: str,
    artifact: str,
    replacements: dict[str, str],
) -> None:
    documents = rewrite_yaml_documents(
        Path(artifact).read_text(encoding="utf-8"),
        {tagged: (digest, 1) for tagged, digest in replacements.items()},
        "digest-locked release",
    )
    documents = ref.enforce_controlled_oci_pull_policy(documents)
    source = yaml.safe_dump_all(documents, sort_keys=False, explicit_start=True)
    ref.run(
        [
            kubectl,
            "apply",
            "--server-side",
            "--force-conflicts",
            "--field-manager=weltgewebe-ha-proof",
            "-f",
            "-",
        ],
        input_text=source,
    )

def install_cert_manager(kubectl: str, artifact: str) -> None:
    apply_digest_locked_manifest(kubectl, artifact, CERT_MANAGER_IMAGES)
    ref.run(
        [
            kubectl,
            "wait",
            "--for=condition=Established",
            "crd/certificates.cert-manager.io",
            "--timeout=3m",
        ]
    )
    deployments = {
        "cert-manager": ref.controlled_oci_runtime_image(
            CERT_MANAGER_IMAGES["quay.io/jetstack/cert-manager-controller:v1.21.0"]
        ),
        "cert-manager-cainjector": ref.controlled_oci_runtime_image(
            CERT_MANAGER_IMAGES["quay.io/jetstack/cert-manager-cainjector:v1.21.0"]
        ),
        "cert-manager-webhook": ref.controlled_oci_runtime_image(
            CERT_MANAGER_IMAGES["quay.io/jetstack/cert-manager-webhook:v1.21.0"]
        ),
    }
    for deployment, expected_image in deployments.items():
        ref.wait_rollout(kubectl, "cert-manager", f"deployment/{deployment}", "8m")
        observed = ref.output(
            [
                kubectl,
                "-n",
                "cert-manager",
                "get",
                f"deployment/{deployment}",
                "-o",
                "jsonpath={.spec.template.spec.containers[0].image}",
            ]
        )
        if observed != expected_image:
            raise ref.ProofError(
                f"cert-manager image is not digest-bound for {deployment}: {observed}"
            )
    wait_until(
        "cert-manager webhook endpoint",
        lambda: ref.output(
            [
                kubectl,
                "-n",
                "cert-manager",
                "get",
                "endpoints/cert-manager-webhook",
                "-o",
                "jsonpath={.subsets[0].addresses[0].ip}",
            ]
        ),
        timeout_seconds=180,
    )


def render_barman_cloud_manifest(source: str) -> str:
    controller_tagged = "ghcr.io/cloudnative-pg/plugin-barman-cloud:v0.13.0"
    documents = rewrite_yaml_documents(
        source,
        {controller_tagged: (BARMAN_CLOUD_PLUGIN_IMAGE, 1)},
        "Barman Cloud release",
    )
    matching_secrets = [
        document
        for document in documents
        if isinstance(document, dict)
        and document.get("kind") == "Secret"
        and isinstance(document.get("data"), dict)
        and "SIDECAR_IMAGE" in document["data"]
    ]
    if len(matching_secrets) != 1:
        raise ref.ProofError(
            "Barman Cloud sidecar secret changed unexpectedly: "
            f"{len(matching_secrets)} matching Secrets"
        )
    secret = matching_secrets[0]
    encoded = "".join(str(secret["data"]["SIDECAR_IMAGE"]).split())
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as error:
        raise ref.ProofError(
            "Barman Cloud sidecar secret is not valid UTF-8 base64"
        ) from error
    sidecar_tagged = (
        "ghcr.io/cloudnative-pg/plugin-barman-cloud-sidecar:v0.13.0"
    )
    if decoded != sidecar_tagged:
        raise ref.ProofError(
            f"Barman Cloud sidecar reference changed unexpectedly: {decoded}"
        )
    sidecar_runtime_image = ref.controlled_oci_runtime_image(
        BARMAN_CLOUD_SIDECAR_IMAGE
    )
    secret["data"]["SIDECAR_IMAGE"] = base64.b64encode(
        sidecar_runtime_image.encode("utf-8")
    ).decode("ascii")

    deployments = [
        document
        for document in documents
        if isinstance(document, dict)
        and document.get("kind") == "Deployment"
        and document.get("metadata", {}).get("name") == "barman-cloud"
        and document.get("metadata", {}).get("namespace") == "cnpg-system"
    ]
    if len(deployments) != 1:
        raise ref.ProofError("Barman Cloud controller deployment changed unexpectedly")
    deployment = deployments[0]
    spec = deployment.setdefault("spec", {})
    if spec.get("replicas") != 1 or spec.get("strategy") != {"type": "Recreate"}:
        raise ref.ProofError(
            "Barman Cloud controller replica or update strategy changed unexpectedly"
        )
    spec["replicas"] = 3
    pod_spec = spec.setdefault("template", {}).setdefault("spec", {})
    affinity = pod_spec.setdefault("affinity", {})
    pod_anti_affinity = affinity.setdefault("podAntiAffinity", {})
    required = pod_anti_affinity.setdefault(
        "requiredDuringSchedulingIgnoredDuringExecution", []
    )
    if required:
        raise ref.ProofError(
            "Barman Cloud release already defines required pod anti-affinity"
        )
    required.append(
        {
            "labelSelector": {"matchLabels": {"app": "barman-cloud"}},
            "topologyKey": "kubernetes.io/hostname",
        }
    )
    documents = ref.enforce_controlled_oci_pull_policy(documents)
    return yaml.safe_dump_all(documents, sort_keys=False, explicit_start=True)

def apply_barman_cloud_manifest(kubectl: str, artifact: str) -> None:
    source = render_barman_cloud_manifest(
        Path(artifact).read_text(encoding="utf-8")
    )
    ref.run(
        [
            kubectl,
            "apply",
            "--server-side",
            "--force-conflicts",
            "--field-manager=weltgewebe-ha-proof",
            "-f",
            "-",
        ],
        input_text=source,
    )


def barman_plugin_state(kubectl: str, *, require_three_nodes: bool) -> dict[str, Any]:
    payload = json.loads(
        ref.output(
            [kubectl, "-n", "cnpg-system", "get", "pods", "-l", "app=barman-cloud", "-o", "json"]
        )
    )
    observed: dict[str, str] = {}
    ready_pods: set[str] = set()
    for item in payload.get("items", []):
        name = str(item.get("metadata", {}).get("name") or "")
        node = str(item.get("spec", {}).get("nodeName") or "")
        containers = item.get("spec", {}).get("containers", [])
        statuses = {
            status.get("name"): status
            for status in item.get("status", {}).get("containerStatuses", [])
        }
        controller = next(
            (
                container
                for container in containers
                if container.get("name") == "barman-cloud"
                and container.get("image")
                == ref.controlled_oci_runtime_image(BARMAN_CLOUD_PLUGIN_IMAGE)
            ),
            None,
        )
        status = statuses.get(controller.get("name")) if controller else None
        pod_conditions = {
            condition.get("type"): condition.get("status")
            for condition in item.get("status", {}).get("conditions", [])
        }
        if (
            not name
            or not node
            or item.get("status", {}).get("phase") != "Running"
            or controller is None
            or not status
            or not status.get("state", {}).get("running", {}).get("startedAt")
        ):
            if require_three_nodes:
                raise ref.ProofError(
                    f"Barman Cloud plugin process is not running: {name or '<unknown>'}"
                )
            continue
        observed[name] = node
        if pod_conditions.get("Ready") == "True":
            ready_pods.add(name)
    if require_three_nodes and (
        len(observed) != 3 or len(set(observed.values())) != 3
    ):
        raise ref.ProofError(
            "Barman Cloud plugin processes are not running on three distinct nodes: "
            f"{observed}"
        )
    endpoints = json.loads(
        ref.output(
            [kubectl, "-n", "cnpg-system", "get", "endpoints/barman-cloud", "-o", "json"]
        )
    )
    endpoint_pods = {
        str(address.get("targetRef", {}).get("name") or "")
        for subset in endpoints.get("subsets", [])
        for address in subset.get("addresses", [])
        if address.get("ip")
    }
    if len(endpoint_pods) != 1 or endpoint_pods != ready_pods:
        raise ref.ProofError(
            "Barman Cloud service must expose exactly its elected ready leader: "
            f"ready={sorted(ready_pods)}, endpoints={sorted(endpoint_pods)}"
        )
    leader_pod = next(iter(endpoint_pods))
    leader_node = observed.get(leader_pod)
    if not leader_node:
        raise ref.ProofError(
            f"Barman Cloud leader is not a running digest-bound process: {leader_pod}"
        )
    return {
        "nodes": sorted(set(observed.values())),
        "pods": dict(sorted(observed.items())),
        "leader": {"pod": leader_pod, "node": leader_node},
    }


def verify_barman_plugin_ha(kubectl: str) -> dict[str, Any]:
    return dict(
        wait_until(
            "three distributed Barman Cloud processes and one elected service leader",
            lambda: barman_plugin_state(kubectl, require_three_nodes=True),
            timeout_seconds=480,
        )
    )


def prove_barman_plugin_backup(
    kubectl: str,
    cluster: str,
    name: str,
) -> dict[str, str]:
    ref.apply_yaml(
        kubectl,
        {
            "apiVersion": "postgresql.cnpg.io/v1",
            "kind": "Backup",
            "metadata": {"name": name, "namespace": "weltgewebe-data"},
            "spec": {
                "method": "plugin",
                "target": "primary",
                "cluster": {"name": cluster},
                "pluginConfiguration": {
                    "name": "barman-cloud.cloudnative-pg.io"
                },
            },
        },
    )
    backup = wait_backup_complete(kubectl, name)
    status = backup.get("status", {})
    phase = str(status.get("phase") or "")
    method = str(status.get("method") or "")
    pod = str(status.get("instanceID", {}).get("podName") or "")
    if phase.lower() != "completed" or method.lower() != "plugin" or not pod:
        raise ref.ProofError(
            "Barman Cloud plugin backup lacks completed plugin-bound evidence: "
            f"phase={phase!r}, method={method!r}, pod={pod!r}"
        )
    return {"name": name, "phase": phase, "method": method, "pod": pod}


def align_postgres_primary_with_barman_leader(
    kubectl_cnpg: str,
    kubectl: str,
    cluster: str,
    postgres_topology: dict[str, dict[str, str]],
    probe_name: str,
) -> dict[str, Any]:
    before_plugin = verify_barman_plugin_ha(kubectl)
    target_node = before_plugin["leader"]["node"]
    target_instances = sorted(
        pod
        for pod, placement in postgres_topology.items()
        if placement.get("node") == target_node
    )
    if len(target_instances) != 1:
        raise ref.ProofError(
            "PostgreSQL has no unique instance on the Barman Cloud leader node "
            f"{target_node}: {postgres_topology}"
        )
    target_primary = target_instances[0]
    before_primary = current_primary(kubectl, cluster)
    started = time.monotonic()
    if before_primary != target_primary:
        ref.run(
            [
                kubectl_cnpg,
                "--namespace",
                "weltgewebe-data",
                "promote",
                cluster,
                target_primary,
            ],
            timeout=60,
        )
        wait_until(
            f"PostgreSQL primary {target_primary} on Barman Cloud leader node",
            lambda: target_primary
            if current_primary(kubectl, cluster) == target_primary
            else False,
            timeout_seconds=240,
        )
    wait_cluster_ready(kubectl, cluster, "10m")
    aligned_primary = current_primary(kubectl, cluster)
    if aligned_primary != target_primary:
        raise ref.ProofError(
            f"PostgreSQL primary did not align with Barman Cloud leader: "
            f"expected={target_primary}, observed={aligned_primary}"
        )
    after_plugin = verify_barman_plugin_ha(kubectl)
    if after_plugin["leader"]["node"] != target_node:
        raise ref.ProofError(
            "Barman Cloud leader moved during PostgreSQL alignment: "
            f"before={before_plugin['leader']}, after={after_plugin['leader']}"
        )
    communication = prove_barman_plugin_backup(kubectl, cluster, probe_name)
    return {
        "target_node": target_node,
        "plugin_leader_before": before_plugin["leader"],
        "plugin_leader_after": after_plugin["leader"],
        "postgres_primary_before": before_primary,
        "postgres_primary_aligned": aligned_primary,
        "communication": communication,
        "duration_seconds": round(time.monotonic() - started, 3),
    }


def wait_barman_plugin_leader_after_node_loss(
    kubectl: str, failed_node: str
) -> dict[str, str]:
    def surviving_leader_probe():
        state = barman_plugin_state(kubectl, require_three_nodes=False)
        leader = state["leader"]
        return leader if leader["node"] != failed_node else False

    return dict(
        wait_until(
            "Barman Cloud leader after node loss",
            surviving_leader_probe,
            timeout_seconds=180,
        )
    )

def install_barman_cloud_plugin(kubectl: str, artifact: str) -> dict[str, Any]:
    apply_barman_cloud_manifest(kubectl, artifact)
    ref.run(
        [
            kubectl,
            "wait",
            "--for=condition=Established",
            "crd/objectstores.barmancloud.cnpg.io",
            "--timeout=3m",
        ]
    )
    for certificate in ("barman-cloud-client", "barman-cloud-server"):
        ref.wait_condition(
            kubectl,
            "cnpg-system",
            f"certificate/{certificate}",
            "Ready",
            "5m",
        )
    plugin_nodes = verify_barman_plugin_ha(kubectl)
    secrets_payload = json.loads(
        ref.output(
            [
                kubectl,
                "-n",
                "cnpg-system",
                "get",
                "secrets",
                "-o",
                "json",
            ]
        )
    )
    sidecar_images = []
    for item in secrets_payload.get("items", []):
        encoded = item.get("data", {}).get("SIDECAR_IMAGE")
        if not encoded:
            continue
        try:
            sidecar_images.append(
                base64.b64decode(encoded, validate=True).decode("utf-8")
            )
        except (ValueError, UnicodeDecodeError) as exc:
            raise ref.ProofError(
                "installed Barman Cloud sidecar secret is invalid"
            ) from exc
    expected_sidecar_image = ref.controlled_oci_runtime_image(
        BARMAN_CLOUD_SIDECAR_IMAGE
    )
    if sidecar_images != [expected_sidecar_image]:
        raise ref.ProofError(
            "Barman Cloud sidecar image is not digest-bound: "
            f"{sidecar_images}"
        )
    observed = ref.output(
        [
            kubectl,
            "-n",
            "cnpg-system",
            "get",
            "deployment/barman-cloud",
            "-o",
            "jsonpath={.spec.template.spec.containers[0].image}",
        ]
    )
    expected_plugin_image = ref.controlled_oci_runtime_image(
        BARMAN_CLOUD_PLUGIN_IMAGE
    )
    if observed != expected_plugin_image:
        raise ref.ProofError(f"Barman Cloud plugin image is not runtime-bound: {observed}")
    return plugin_nodes


def verify_barman_sidecar_images(
    kubectl: str, cluster: str
) -> dict[str, dict[str, object]]:
    payload = json.loads(
        ref.output(
            [
                kubectl,
                "-n",
                "weltgewebe-data",
                "get",
                "pods",
                "-l",
                f"cnpg.io/cluster={cluster}",
                "-o",
                "json",
            ]
        )
    )
    expected_sidecar_image = ref.controlled_oci_runtime_image(
        BARMAN_CLOUD_SIDECAR_IMAGE
    )
    expected_digests = ref.controlled_oci_runtime_identity_digests(
        BARMAN_CLOUD_SIDECAR_IMAGE
    )
    observed: dict[str, dict[str, object]] = {}
    for pod in payload.get("items", []):
        name = str(pod.get("metadata", {}).get("name", ""))
        containers = [
            *pod.get("spec", {}).get("initContainers", []),
            *pod.get("spec", {}).get("containers", []),
        ]
        sidecars = [
            container
            for container in containers
            if container.get("name") == "plugin-barman-cloud"
        ]
        if len(sidecars) != 1:
            raise ref.ProofError(
                f"PostgreSQL pod {name} has {len(sidecars)} Barman sidecars"
            )
        declared_image = str(sidecars[0].get("image", ""))
        if declared_image != expected_sidecar_image:
            raise ref.ProofError(
                f"PostgreSQL pod {name} has an unbound Barman sidecar: "
                f"{declared_image}"
            )
        statuses = [
            *pod.get("status", {}).get("initContainerStatuses", []),
            *pod.get("status", {}).get("containerStatuses", []),
        ]
        sidecar_statuses = [
            status
            for status in statuses
            if status.get("name") == "plugin-barman-cloud"
        ]
        if len(sidecar_statuses) != 1:
            raise ref.ProofError(
                f"PostgreSQL pod {name} has {len(sidecar_statuses)} Barman statuses"
            )
        status = sidecar_statuses[0]
        image_id = str(status.get("imageID", ""))
        running = status.get("state", {}).get("running", {})
        started_at = running.get("startedAt")
        if not any(image_id.endswith(digest) for digest in expected_digests):
            raise ref.ProofError(
                f"PostgreSQL pod {name} runs an unbound Barman image ID: {image_id}"
            )
        if status.get("ready") is not True or not started_at:
            raise ref.ProofError(
                f"PostgreSQL pod {name} Barman sidecar is not running and ready"
            )
        observed[name] = {
            "declared_image": declared_image,
            "image_id": image_id,
            "ready": True,
            "started_at": started_at,
        }
    if len(observed) != 3:
        raise ref.ProofError(
            f"Barman Cloud requires exactly three ready instance sidecars for "
            f"{cluster}: {sorted(observed)}"
        )
    return observed


def wait_cluster_ready(kubectl: str, cluster: str, timeout: str = "15m") -> None:
    ref.wait_condition(kubectl, "weltgewebe-data", f"cluster/{cluster}", "Ready", timeout)


def ha_diagnostic_snapshot(kubectl: str, name: str) -> None:
    target = CACHE / "failures" / name
    target.mkdir(parents=True, exist_ok=True)
    commands = {
        "cnpg-clusters.yaml": [kubectl, "get", "clusters.postgresql.cnpg.io", "-A", "-o", "yaml"],
        "cnpg-pods-describe.txt": [kubectl, "-n", "weltgewebe-data", "describe", "pods", "-l", "cnpg.io/cluster"],
        "cnpg-pods-logs.txt": [kubectl, "-n", "weltgewebe-data", "logs", "-l", "cnpg.io/cluster", "--all-containers=true", "--prefix=true", "--tail=2000"],
        "cnpg-pods-previous-logs.txt": [kubectl, "-n", "weltgewebe-data", "logs", "-l", "cnpg.io/cluster", "--all-containers=true", "--prefix=true", "--previous=true", "--tail=2000"],
        "cnpg-operator-deployment.yaml": [
            kubectl, "-n", "cnpg-system", "get",
            "deployment/cnpg-controller-manager", "-o", "yaml",
        ],
        "cnpg-operator-replicasets.yaml": [
            kubectl, "-n", "cnpg-system", "get",
            "replicasets", "-l", "app.kubernetes.io/name=cloudnative-pg",
            "-o", "yaml",
        ],
        "cnpg-operator-logs.txt": [
            kubectl, "-n", "cnpg-system", "logs",
            "-l", "app.kubernetes.io/name=cloudnative-pg",
            "--all-containers=true", "--prefix=true", "--tail=2000",
        ],
        "barman-plugin-deployment.yaml": [
            kubectl, "-n", "cnpg-system", "get", "deployment/barman-cloud", "-o", "yaml",
        ],
        "barman-plugin-pods-describe.txt": [
            kubectl, "-n", "cnpg-system", "describe", "pods", "-l", "app=barman-cloud",
        ],
        "barman-plugin-endpoints.yaml": [
            kubectl, "-n", "cnpg-system", "get", "endpoints/barman-cloud", "-o", "yaml",
        ],
        "barman-plugin-logs.txt": [
            kubectl, "-n", "cnpg-system", "logs", "-l", "app=barman-cloud",
            "--all-containers=true", "--prefix=true", "--tail=2000",
        ],
        "barman-objectstores.yaml": [kubectl, "get", "objectstores.barmancloud.cnpg.io", "-A", "-o", "yaml"],
        "certificates.yaml": [kubectl, "get", "certificates.cert-manager.io", "-A", "-o", "yaml"],
        "cluster-dns-deployment.yaml": [
            kubectl, "-n", "kube-system", "get", "deployment/coredns", "-o", "yaml",
        ],
        "cluster-dns-pods-describe.txt": [
            kubectl, "-n", "kube-system", "describe", "pods", "-l", "k8s-app=kube-dns",
        ],
        "cluster-dns-logs.txt": [
            kubectl, "-n", "kube-system", "logs", "-l", "k8s-app=kube-dns",
            "--all-containers=true", "--prefix=true", "--tail=2000",
        ],
        "storage.txt": [kubectl, "get", "pv,pvc", "-A", "-o", "wide"],
    }
    for filename, argv in commands.items():
        try:
            result = subprocess.run(argv, cwd=ROOT, text=True, capture_output=True, timeout=30)
            evidence = result.stdout + result.stderr
        except (OSError, subprocess.TimeoutExpired) as error:
            evidence = f"diagnostic command failed: {error}\n"
        (target / filename).write_text(evidence, encoding="utf-8")


def wait_for_pitr_boundary(kubectl: str, target_time: str) -> None:
    psql(kubectl, "SELECT pg_sleep(2)")
    escaped_target = target_time.replace("'", "''")
    crossed = psql(
        kubectl,
        f"SELECT clock_timestamp() > '{escaped_target}'::timestamptz",
    )
    if crossed != "t":
        raise ref.ProofError(
            f"PostgreSQL clock did not cross PITR target {target_time}"
        )


def pod_topology(kubectl: str, namespace: str, selector: str) -> dict[str, dict[str, str]]:
    pods = json.loads(ref.output([kubectl, "-n", namespace, "get", "pods", "-l", selector, "-o", "json"]))
    nodes = json.loads(ref.output([kubectl, "get", "nodes", "-o", "json"]))
    zones = {item["metadata"]["name"]: item["metadata"].get("labels", {}).get("topology.kubernetes.io/zone", "") for item in nodes["items"]}
    result: dict[str, dict[str, str]] = {}
    for item in pods["items"]:
        metadata = item.get("metadata", {})
        status = item.get("status", {})
        if metadata.get("deletionTimestamp") or status.get("phase") != "Running":
            continue
        ready = any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in status.get("conditions", [])
        )
        if not ready:
            continue
        node = item.get("spec", {}).get("nodeName", "")
        if node:
            result[metadata["name"]] = {"node": node, "zone": zones.get(node, "")}
    return result


def require_zones(topology: dict[str, dict[str, str]], expected: int, component: str) -> None:
    zones = {item["zone"] for item in topology.values() if item["zone"]}
    if len(topology) < expected or len(zones) != expected:
        raise ref.ProofError(f"{component} is not spread across {expected} zones: {topology}")


def configure_cluster_dns_ha(kubectl: str) -> dict[str, dict[str, str]]:
    patch = {
        "spec": {
            "replicas": 3,
            "template": {
                "spec": {
                    "nodeSelector": {
                        "kubernetes.io/os": "linux",
                        "weltgewebe.net/data-node": "true",
                    },
                    "affinity": {
                        "podAntiAffinity": {
                            "requiredDuringSchedulingIgnoredDuringExecution": [
                                {
                                    "labelSelector": {
                                        "matchLabels": {"k8s-app": "kube-dns"}
                                    },
                                    "topologyKey": "topology.kubernetes.io/zone",
                                }
                            ]
                        }
                    },
                }
            },
        }
    }
    ref.run(
        [
            kubectl,
            "-n",
            "kube-system",
            "patch",
            "deployment/coredns",
            "--type=strategic",
            "--patch",
            json.dumps(patch, separators=(",", ":"), sort_keys=True),
        ],
        timeout=120,
    )
    ref.wait_rollout(kubectl, "kube-system", "deployment/coredns", "8m")
    topology = pod_topology(kubectl, "kube-system", "k8s-app=kube-dns")
    if len(topology) != 3:
        raise ref.ProofError(
            f"cluster DNS requires exactly three replicas: {topology}"
        )
    require_zones(topology, 3, "cluster DNS")
    return topology


def wait_api_replicas(kubectl: str, expected: int = 3) -> None:
    ref.wait_rollout(kubectl, "weltgewebe", "deployment/weltgewebe-api", "10m")
    available = ref.output([kubectl, "-n", "weltgewebe", "get", "deployment/weltgewebe-api", "-o", "jsonpath={.status.availableReplicas}"])
    if available != str(expected):
        raise ref.ProofError(f"expected {expected} available API replicas, got {available!r}")
    ref.wait_rollout(kubectl, "weltgewebe", "deployment/weltgewebe-web", "10m")


def prepare_api_upgrade_candidate(
    kind: str, cluster: str, commit: str
) -> dict[str, str]:
    dockerfile = (
        f"FROM {BASELINE_API_IMAGE}\n"
        f"LABEL org.opencontainers.image.revision={commit}\n"
        "LABEL weltgewebe.net/ha-proof-artifact=upgrade-candidate\n"
    )
    ref.run(
        [
            "docker",
            "build",
            "--pull=false",
            "--file",
            "-",
            "--tag",
            UPGRADE_API_IMAGE,
            ".",
        ],
        input_text=dockerfile,
        timeout=600,
    )
    ref.run(
        [kind, "load", "docker-image", "--name", cluster, UPGRADE_API_IMAGE],
        timeout=600,
    )
    baseline_id = ref.output(
        ["docker", "image", "inspect", "--format", "{{.Id}}", BASELINE_API_IMAGE]
    )
    candidate_id = ref.output(
        ["docker", "image", "inspect", "--format", "{{.Id}}", UPGRADE_API_IMAGE]
    )
    if baseline_id == candidate_id:
        raise ref.ProofError("upgrade candidate must be a distinct immutable image")
    return {
        "api_baseline": baseline_id,
        "api_upgrade_candidate": candidate_id,
    }


def ready_api_pod_uids(kubectl: str) -> set[str]:
    payload = json.loads(
        ref.output(
            [
                kubectl,
                "-n",
                "weltgewebe",
                "get",
                "pods",
                "-l",
                "app.kubernetes.io/name=weltgewebe-api",
                "-o",
                "json",
            ]
        )
    )
    ready: set[str] = set()
    for item in payload.get("items", []):
        conditions = {
            condition.get("type"): condition.get("status")
            for condition in item.get("status", {}).get("conditions", [])
        }
        uid = item.get("metadata", {}).get("uid")
        if (
            item.get("metadata", {}).get("deletionTimestamp") is None
            and item.get("status", {}).get("phase") == "Running"
            and conditions.get("Ready") == "True"
            and isinstance(uid, str)
            and uid
        ):
            ready.add(uid)
    if len(ready) != 3:
        raise ref.ProofError(
            f"expected exactly three ready API pod UIDs, got {sorted(ready)}"
        )
    return ready


def api_deployment_image(kubectl: str) -> str:
    return ref.output(
        [
            kubectl,
            "-n",
            "weltgewebe",
            "get",
            "deployment/weltgewebe-api",
            "-o",
            "jsonpath={.spec.template.spec.containers[?(@.name==\"api\")].image}",
        ]
    )


def api_rollout_complete(kubectl: str, expected_image: str) -> bool:
    deployment = json.loads(
        ref.output(
            [
                kubectl,
                "-n",
                "weltgewebe",
                "get",
                "deployment/weltgewebe-api",
                "-o",
                "json",
            ]
        )
    )
    metadata = deployment.get("metadata", {})
    spec = deployment.get("spec", {})
    status = deployment.get("status", {})
    images = {
        container.get("name"): container.get("image")
        for container in spec.get("template", {})
        .get("spec", {})
        .get("containers", [])
    }
    return (
        images.get("api") == expected_image
        and status.get("observedGeneration") == metadata.get("generation")
        and status.get("updatedReplicas") == 3
        and status.get("readyReplicas") == 3
        and status.get("availableReplicas") == 3
        and status.get("unavailableReplicas", 0) == 0
    )


def projection_path_diagnostic(
    kubectl: str,
    kind: str,
    cluster: str,
    *,
    probe_node: str,
    probe_address: str,
    port: int,
    marker: str,
    direct_probe_pods: list[str],
) -> dict[str, Any]:
    service = json.loads(
        ref.output(
            [
                kubectl,
                "-n",
                "weltgewebe",
                "get",
                "service/weltgewebe-api",
                "-o",
                "json",
            ]
        )
    )
    service_ip = service.get("spec", {}).get("clusterIP")
    service_ports = service.get("spec", {}).get("ports", [])
    service_port = next(
        (item.get("port") for item in service_ports if item.get("name") == "http"),
        8080,
    )
    if not isinstance(service_ip, str) or not service_ip or service_ip == "None":
        raise ref.ProofError("API transition diagnostic has no direct service address")
    direct_api = direct_api_probe_sample(
        kubectl, direct_probe_pods, service_ip, int(service_port), marker
    )
    targets: list[tuple[str, str, str, str]] = []
    gateway_addresses = sorted(set(ref.gateway_addresses(kubectl)))
    gateway_nodes = sorted(set(ref.kind_nodes(kind, cluster)))
    for node in gateway_nodes:
        for address in gateway_addresses:
            targets.append(
                (
                    "gateway",
                    node,
                    address,
                    f"http://{address}:{port}/api/nodes",
                )
            )

    def run_target(target: tuple[str, str, str, str]) -> dict[str, Any]:
        layer, node, address, url = target
        try:
            sample = projection_sample_url(node, url, marker)
        except (subprocess.SubprocessError, OSError) as error:
            sample = {
                "available": False,
                "probe_error": f"{type(error).__name__}: {error}",
                "url": url,
            }
        return {
            "layer": layer,
            "node": node,
            "address": address,
            **sample,
        }

    if not targets:
        return {
            "selected_gateway": {
                "node": probe_node,
                "address": probe_address,
                "port": port,
            },
            "probes": [],
        }
    with ThreadPoolExecutor(max_workers=min(16, len(targets))) as executor:
        probes = list(executor.map(run_target, targets))
    return {
        "selected_gateway": {
            "node": probe_node,
            "address": probe_address,
            "port": port,
        },
        "direct_api": direct_api,
        "probes": probes,
    }


def api_transition_diagnostic(
    kubectl: str,
    gateway_sample: dict[str, Any],
    *,
    kind: str,
    cluster: str,
    probe_node: str,
    probe_address: str,
    port: int,
    marker: str,
    direct_probe_pods: list[str],
) -> dict[str, Any]:
    deployment = json.loads(
        ref.output(
            [
                kubectl,
                "-n",
                "weltgewebe",
                "get",
                "deployment/weltgewebe-api",
                "-o",
                "json",
            ]
        )
    )
    pods = json.loads(
        ref.output(
            [
                kubectl,
                "-n",
                "weltgewebe",
                "get",
                "pods",
                "-l",
                "app.kubernetes.io/name=weltgewebe-api",
                "-o",
                "json",
            ]
        )
    )
    endpoint_slices = json.loads(
        ref.output(
            [
                kubectl,
                "-n",
                "weltgewebe",
                "get",
                "endpointslices.discovery.k8s.io",
                "-l",
                "kubernetes.io/service-name=weltgewebe-api",
                "-o",
                "json",
            ]
        )
    )
    pod_state = []
    for item in pods.get("items", []):
        conditions = {
            condition.get("type"): condition.get("status")
            for condition in item.get("status", {}).get("conditions", [])
        }
        pod_state.append(
            {
                "name": item.get("metadata", {}).get("name"),
                "node": item.get("spec", {}).get("nodeName"),
                "pod_ip": item.get("status", {}).get("podIP"),
                "phase": item.get("status", {}).get("phase"),
                "ready": conditions.get("Ready") == "True",
                "deleting": bool(
                    item.get("metadata", {}).get("deletionTimestamp")
                ),
                "image": next(
                    (
                        container.get("image")
                        for container in item.get("spec", {}).get(
                            "containers", []
                        )
                        if container.get("name") == "api"
                    ),
                    None,
                ),
            }
        )
    endpoint_state = []
    for item in endpoint_slices.get("items", []):
        for endpoint in item.get("endpoints", []):
            endpoint_state.append(
                {
                    "addresses": endpoint.get("addresses", []),
                    "node": endpoint.get("nodeName"),
                    "target": endpoint.get("targetRef", {}).get("name"),
                    "ready": endpoint.get("conditions", {}).get("ready"),
                    "serving": endpoint.get("conditions", {}).get("serving"),
                    "terminating": endpoint.get("conditions", {}).get(
                        "terminating"
                    ),
                }
            )
    status = deployment.get("status", {})
    metadata = deployment.get("metadata", {})
    path_diagnostic = projection_path_diagnostic(
        kubectl,
        kind,
        cluster,
        probe_node=probe_node,
        probe_address=probe_address,
        port=port,
        marker=marker,
        direct_probe_pods=direct_probe_pods,
    )
    return {
        "gateway_sample": gateway_sample,
        "path_diagnostic": path_diagnostic,
        "deployment": {
            "generation": metadata.get("generation"),
            "observed_generation": status.get("observedGeneration"),
            "replicas": status.get("replicas"),
            "updated_replicas": status.get("updatedReplicas"),
            "ready_replicas": status.get("readyReplicas"),
            "available_replicas": status.get("availableReplicas"),
            "unavailable_replicas": status.get("unavailableReplicas", 0),
        },
        "pods": pod_state,
        "endpoint_slices": endpoint_state,
    }


def measure_api_transition(
    kubectl: str,
    marker: str,
    expected_image: str,
    action: list[str],
    phase: str,
    *,
    kind: str,
    cluster: str,
    probe_node: str,
    probe_address: str,
    port: int,
    direct_probe_pods: list[str],
) -> dict[str, Any]:
    before_uids = ready_api_pod_uids(kubectl)
    # Bind every advertised listener to its actual Kind owner BEFORE the
    # upgrade/rollback begins. Never let a healthy non-owner path mask an outage.
    gateway_addresses = sorted(set(ref.gateway_addresses(kubectl)))
    gateway_binding = gateway_observer_binding(kind, cluster, gateway_addresses)
    started = time.monotonic()
    ref.run(action)
    deadline = started + 600
    samples = 0
    failed_samples = 0
    unavailable_seconds = 0.0
    longest_unavailable_seconds = 0.0
    outage_started: float | None = None
    outage_diagnostics: list[dict[str, Any]] = []
    pending_diagnostic: dict[str, Any] | None = None
    degraded_path_samples = 0
    failed_gateway_paths = 0
    max_failed_gateway_paths = 0

    def capture_diagnostic(
        pending: dict[str, Any],
        *,
        recovery_elapsed_seconds: float | None,
    ) -> dict[str, Any]:
        diagnostic_started = time.monotonic()
        try:
            diagnostic = api_transition_diagnostic(
                kubectl,
                pending["gateway_sample"],
                kind=kind,
                cluster=cluster,
                probe_node=probe_node,
                probe_address=probe_address,
                port=port,
                marker=marker,
                direct_probe_pods=direct_probe_pods,
            )
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            json.JSONDecodeError,
            ref.ProofError,
        ) as error:
            diagnostic = {
                "gateway_sample": pending["gateway_sample"],
                "diagnostic_error": f"{type(error).__name__}: {error}",
            }
        diagnostic["sample"] = pending["sample"]
        diagnostic["elapsed_seconds"] = pending["elapsed_seconds"]
        diagnostic["capture"] = "post-recovery-after-measurement"
        diagnostic["recovery_elapsed_seconds"] = (
            round(recovery_elapsed_seconds, 3)
            if recovery_elapsed_seconds is not None
            else None
        )
        diagnostic["diagnostic_duration_seconds"] = round(
            time.monotonic() - diagnostic_started, 3
        )
        return diagnostic

    while time.monotonic() < deadline:
        sampled_at = time.monotonic()
        available = False
        rollout_complete = False
        gateway_sample: dict[str, Any] = {"available": False}
        try:
            observed_addresses = sorted(set(ref.gateway_addresses(kubectl)))
            if observed_addresses != gateway_addresses:
                raise ref.ProofError(
                    "Gateway listener addresses drifted during API transition: "
                    f"before={gateway_addresses} now={observed_addresses}"
                )
            observed_binding = gateway_observer_binding(
                kind, cluster, gateway_addresses
            )
            if observed_binding["owner_by_address"] != gateway_binding["owner_by_address"]:
                raise ref.ProofError(
                    "Gateway owner mapping drifted during API transition"
                )
            gateway_sample = gateway_observer_sample(
                kind, cluster, gateway_addresses, port, marker,
                observer_binding=gateway_binding,
            )
            available = bool(gateway_sample["available"])
            sample_failed_paths = int(gateway_sample.get("failed_paths", 0))
            if sample_failed_paths:
                degraded_path_samples += 1
                failed_gateway_paths += sample_failed_paths
                max_failed_gateway_paths = max(
                    max_failed_gateway_paths, sample_failed_paths
                )
            if available:
                rollout_complete = api_rollout_complete(
                    kubectl, expected_image
                )
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            json.JSONDecodeError,
            ref.ProofError,
        ) as error:
            gateway_sample = {
                "available": False,
                "probe_error": f"{type(error).__name__}: {error}",
            }
            available = False
            rollout_complete = False
        samples += 1
        if available:
            if outage_started is not None:
                outage = sampled_at - outage_started
                unavailable_seconds += outage
                longest_unavailable_seconds = max(
                    longest_unavailable_seconds, outage
                )
                outage_started = None
                if pending_diagnostic is not None:
                    outage_diagnostics.append(
                        capture_diagnostic(
                            pending_diagnostic,
                            recovery_elapsed_seconds=sampled_at - started,
                        )
                    )
                    pending_diagnostic = None
        else:
            failed_samples += 1
            if outage_started is None:
                outage_started = sampled_at
                pending_diagnostic = {
                    "gateway_sample": gateway_sample,
                    "sample": samples,
                    "elapsed_seconds": round(sampled_at - started, 3),
                }
        if available and rollout_complete:
            break
        time.sleep(1)
    else:
        if pending_diagnostic is not None:
            outage_diagnostics.append(
                capture_diagnostic(
                    pending_diagnostic,
                    recovery_elapsed_seconds=None,
                )
            )
            pending_diagnostic = None
        raise ref.ProofError(
            f"timed out waiting for API {phase} rollout to {expected_image}"
        )
    finished = time.monotonic()
    if outage_started is not None:
        outage = finished - outage_started
        unavailable_seconds += outage
        longest_unavailable_seconds = max(longest_unavailable_seconds, outage)
    after_uids = ready_api_pod_uids(kubectl)
    overlap = before_uids & after_uids
    if overlap:
        raise ref.ProofError(
            f"API {phase} did not replace every replica: {sorted(overlap)}"
        )
    if api_deployment_image(kubectl) != expected_image:
        raise ref.ProofError(
            f"API {phase} finished with unexpected image: "
            f"{api_deployment_image(kubectl)}"
        )
    result = {
        "phase": phase,
        "image": expected_image,
        "duration_seconds": round(finished - started, 3),
        "availability_samples": samples,
        "failed_availability_samples": failed_samples,
        "degraded_gateway_path_samples": degraded_path_samples,
        "failed_gateway_paths_observed": failed_gateway_paths,
        "max_failed_gateway_paths_per_sample": max_failed_gateway_paths,
        "observed_unavailable_seconds": round(unavailable_seconds, 3),
        "longest_unavailable_seconds": round(longest_unavailable_seconds, 3),
        "probe_node": probe_node,
        "probe_address": probe_address,
        "gateway_probe_mode": "owner-local-any-advertised-address",
        "gateway_owner_by_address": dict(gateway_binding["owner_by_address"]),
        "gateway_observer_node": gateway_sample.get("observer_node"),
        "gateway_observer_address": gateway_sample.get("observer_address"),
        "gateway_addresses": gateway_addresses,
        "before_pods_sha256": sha256_text("\n".join(sorted(before_uids))),
        "after_pods_sha256": sha256_text("\n".join(sorted(after_uids))),
        "replaced_replicas": len(after_uids),
        "outage_diagnostics": outage_diagnostics,
    }
    if failed_samples:
        raise ref.ProofError(
            f"API {phase} violated the zero-observed-outage contract: {result}"
        )
    return result


def compute_error_budget(
    upgrade: dict[str, Any], rollback: dict[str, Any]
) -> dict[str, Any]:
    window = float(upgrade["duration_seconds"]) + float(
        rollback["duration_seconds"]
    )
    if window <= 0:
        raise ref.ProofError("change-management measurement window is empty")
    unavailable = float(upgrade["observed_unavailable_seconds"]) + float(
        rollback["observed_unavailable_seconds"]
    )
    allowed = window * (1.0 - REFERENCE_AVAILABILITY_OBJECTIVE)
    consumed_ratio = unavailable / allowed if allowed > 0 else float("inf")
    availability = max(0.0, 1.0 - unavailable / window)
    return {
        "objective": REFERENCE_AVAILABILITY_OBJECTIVE,
        "measurement_window_seconds": round(window, 3),
        "allowed_unavailable_seconds": round(allowed, 6),
        "observed_unavailable_seconds": round(unavailable, 3),
        "observed_availability": round(availability, 9),
        "consumed_ratio": round(consumed_ratio, 6),
        "remaining_seconds": round(max(0.0, allowed - unavailable), 6),
        "within_budget": unavailable <= allowed,
    }


def prove_api_upgrade_and_rollback(
    kubectl: str,
    kind: str,
    cluster: str,
    marker: str,
    gateway_probe: dict[str, str],
    direct_probe_pods: list[str],
) -> dict[str, Any]:
    baseline = api_deployment_image(kubectl)
    if baseline != BASELINE_API_IMAGE:
        raise ref.ProofError(
            f"API baseline image is unexpected before upgrade: {baseline}"
        )
    try:
        probe_node = gateway_probe["probe_node"]
        probe_address = gateway_probe["address"]
        probe_port = int(gateway_probe["listener_port"])
    except (KeyError, TypeError, ValueError) as error:
        raise ref.ProofError(
            f"validated gateway probe receipt is incomplete: {gateway_probe!r}"
        ) from error

    upgrade = measure_api_transition(
        kubectl,
        marker,
        UPGRADE_API_IMAGE,
        [
            kubectl,
            "-n",
            "weltgewebe",
            "set",
            "image",
            "deployment/weltgewebe-api",
            f"api={UPGRADE_API_IMAGE}",
        ],
        "upgrade",
        kind=kind,
        cluster=cluster,
        probe_node=probe_node,
        probe_address=probe_address,
        port=probe_port,
        direct_probe_pods=direct_probe_pods,
    )
    rollback = measure_api_transition(
        kubectl,
        marker,
        baseline,
        [
            kubectl,
            "-n",
            "weltgewebe",
            "rollout",
            "undo",
            "deployment/weltgewebe-api",
        ],
        "rollback",
        kind=kind,
        cluster=cluster,
        probe_node=probe_node,
        probe_address=probe_address,
        port=probe_port,
        direct_probe_pods=direct_probe_pods,
    )
    budget = compute_error_budget(upgrade, rollback)
    if not gateway_contains_node(kubectl, kind, cluster, marker):
        raise ref.ProofError(
            "acknowledged domain mutation was not preserved across upgrade/rollback"
        )
    return {
        "artifact_scope": (
            "distinct immutable metadata-layer image built from the exact "
            "baseline runtime bits"
        ),
        "upgrade": upgrade,
        "rollback": rollback,
        "error_budget": budget,
    }


def measure_wal_archive(
    kubectl: str,
    *,
    cluster: str,
    object_store_address: str,
    s3_access_key: str,
    s3_secret_key: str,
) -> dict[str, Any]:
    started = time.monotonic()
    required = psql(
        kubectl, "SELECT pg_walfile_name(pg_switch_wal())", cluster=cluster
    )
    wal_segment_position(required)

    def archived_probe() -> str | bool:
        observed = psql(
            kubectl,
            "SELECT COALESCE(last_archived_wal, '') FROM pg_stat_archiver",
            cluster=cluster,
        )
        return (
            observed
            if observed and wal_archived_at_or_after(observed, required)
            else False
        )

    observed = str(
        wait_until(
            f"{cluster} WAL archive at or after {required}",
            archived_probe,
            timeout_seconds=300,
        )
    )
    archiver_latency = time.monotonic() - started
    object_store = dict(
        wait_until(
            f"{cluster} exact WAL object {required}",
            lambda: object_store_wal_evidence(
                object_store_address,
                s3_access_key,
                s3_secret_key,
                server_name=cluster,
                wal_segment=required,
            ),
            timeout_seconds=300,
            interval=1,
        )
    )
    object_store_visibility_latency = time.monotonic() - started
    return {
        "required_wal": required,
        "observed_wal": observed,
        # Preserve T004's historical metric semantics: this stops at pg_stat_archiver.
        "latency_seconds": round(archiver_latency, 3),
        "archiver_latency_seconds": round(archiver_latency, 3),
        "object_store_visibility_latency_seconds": round(
            object_store_visibility_latency, 3
        ),
        "archiver_state_verified": True,
        "object_store": object_store,
    }

def insert_domain_node(kubectl: str, node_id: str, title: str, *, cluster: str = "postgres-ha") -> None:
    payload = json.dumps({"summary": "HA recovery proof", "tags": ["ha-proof"]}, separators=(",", ":"))
    sql = (
        "INSERT INTO domain_nodes (id,kind,title,lat,lon,created_at,updated_at,payload) VALUES ("
        + "'" + node_id + "','Werkstatt','" + title.replace("'", "''") + "',53.55,9.99,clock_timestamp(),clock_timestamp(),'"
        + payload.replace("'", "''") + "'::jsonb) ON CONFLICT (id) DO NOTHING"
    )
    psql(kubectl, sql, cluster=cluster)


def node_exists(kubectl: str, node_id: str, *, cluster: str = "postgres-ha") -> bool:
    return psql(kubectl, f"SELECT count(*) FROM domain_nodes WHERE id='{node_id}'", cluster=cluster) == "1"


def ha_gateway_baseline(kubectl: str, kind: str, cluster: str) -> dict[str, str]:
    # ADR-0013 measures HA gateways owner-locally, not cross-node Kind paths.
    return ref.prove_gateway(kubectl, kind, cluster, owner_local=True)


def gateway_contains_node(kubectl: str, kind: str, cluster: str, node_id: str) -> bool:
    ref.wait_condition(kubectl, "weltgewebe-gateway", "gateway/weltgewebe", "Programmed", "5m")
    service = ref.output([kubectl, "-n", "weltgewebe-gateway", "get", "service", "-l", "gateway.networking.k8s.io/gateway-name=weltgewebe", "-o", "jsonpath={.items[0].metadata.name}"])
    port = ref.gateway_listener_port(kubectl)
    _, _, _, _, body = ref.probe_gateway_http(
        kind, cluster, ref.gateway_addresses(kubectl),
        ref.gateway_service_listener_port(kubectl, service, port),
        owner_local=True,
    )
    payload = json.loads(body)
    return any(isinstance(item, dict) and item.get("id") == node_id for item in payload)


def wait_for_gateway_domain_node(
    kubectl: str,
    kind: str,
    cluster: str,
    node_id: str,
    *,
    timeout_seconds: int = 180,
    interval: float = 2.0,
) -> bool:
    # Absence of the domain node is retryable; a broken Gateway matrix is not.
    return wait_until(
        "domain mutation in API projection",
        lambda: gateway_contains_node(kubectl, kind, cluster, node_id),
        timeout_seconds=timeout_seconds,
        interval=interval,
        retry_on_probe_error=False,
    )


def create_nats_box(
    kubectl: str,
    zone: str,
    *,
    image: str = NATS_BOX_IMAGE,
    image_pull_policy: str | None = None,
) -> None:
    container = {
        "name": "nats-box",
        "image": image,
        "command": ["/bin/sh", "-c", "sleep 7200"],
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
        },
        "resources": {
            "requests": {"cpu": "20m", "memory": "32Mi"},
            "limits": {"cpu": "200m", "memory": "128Mi"},
        },
    }
    if image_pull_policy is not None:
        container["imagePullPolicy"] = image_pull_policy
    ref.apply_yaml(
        kubectl,
        {
            "apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": "nats-box", "namespace": "weltgewebe-data"},
            "spec": {
                "automountServiceAccountToken": False,
                "nodeSelector": {"topology.kubernetes.io/zone": zone},
                "restartPolicy": "Never",
                "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000, "seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [container],
            },
        },
    )
    ref.wait_condition(kubectl, "weltgewebe-data", "pod/nats-box", "Ready", "5m")


def nats(kubectl: str, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    argv = [kubectl, "-n", "weltgewebe-data", "exec", "nats-box", "--", "nats", "--server", "nats://nats:4222", "--js-domain", "weltgewebe-ha", *args]
    return ref.run(argv, capture=True) if check else subprocess.run(argv, cwd=ROOT, text=True, capture_output=True)


def nats_message_count(kubectl: str) -> int:
    document = json.loads(nats(kubectl, ["stream", "info", "WG_PROOF", "--json"]).stdout)
    return int(document.get("state", {}).get("messages", -1))


def domain_jetstream_contract(kubectl: str) -> dict[str, Any]:
    stream_name = "WELTGEWEBE_DOMAIN"
    consumer_name = "weltgewebe-api-domain-receipts-v1"
    stream = json.loads(
        nats(kubectl, ["stream", "info", stream_name, "--json"]).stdout
    )
    consumer = json.loads(
        nats(
            kubectl,
            ["consumer", "info", stream_name, consumer_name, "--json"],
        ).stdout
    )

    def replicas(document: dict[str, Any], label: str) -> int:
        config = document.get("config")
        if not isinstance(config, dict):
            raise ref.ProofError(f"{label} has no JetStream config")
        observed = config.get("num_replicas")
        if not isinstance(observed, int):
            raise ref.ProofError(f"{label} has no integer num_replicas")
        if observed != 3:
            raise ref.ProofError(f"{label} has {observed} replicas, expected 3")
        return observed

    return {
        "stream": stream_name,
        "stream_replicas": replicas(stream, "domain JetStream"),
        "consumer": consumer_name,
        "consumer_replicas": replicas(consumer, "domain receipt consumer"),
        "status": "pass",
    }


def wait_backup_complete(kubectl: str, name: str) -> dict[str, Any]:
    def probe():
        document = json.loads(ref.output([kubectl, "-n", "weltgewebe-data", "get", f"backup/{name}", "-o", "json"]))
        return document if str(document.get("status", {}).get("phase", "")).lower() == "completed" else False
    return wait_until(f"CloudNativePG backup {name}", probe, timeout_seconds=1200, interval=5)


def restore_cluster_document(target_time: str) -> dict[str, Any]:
    return {
        "apiVersion": "postgresql.cnpg.io/v1", "kind": "Cluster",
        "metadata": {"name": "postgres-restore", "namespace": "weltgewebe-data"},
        "spec": {
            "instances": 3,
            "imageCatalogRef": {
                "apiGroup": "postgresql.cnpg.io",
                "kind": "ImageCatalog",
                "name": "weltgewebe-postgres",
                "major": 16,
            },
            "bootstrap": {
                "recovery": {
                    "source": "postgres-ha",
                    "database": "weltgewebe", "owner": APP_USER,
                    "secret": {"name": "weltgewebe-ha-app"},
                    "recoveryTarget": {"targetTime": target_time, "exclusive": True},
                }
            },
            "externalClusters": [{
                "name": "postgres-ha",
                "plugin": {
                    "name": "barman-cloud.cloudnative-pg.io",
                    "parameters": {
                        "barmanObjectName": "weltgewebe-ha-backup",
                        "serverName": "postgres-ha",
                    },
                },
            }],
            "plugins": [{
                "name": "barman-cloud.cloudnative-pg.io",
                "isWALArchiver": True,
                "parameters": {
                    "barmanObjectName": "weltgewebe-ha-backup"
                },
            }],
            "storage": {"size": "1Gi"},
            "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"cpu": "1", "memory": "1Gi"}},
            "affinity": {
                "enablePodAntiAffinity": True, "podAntiAffinityType": "required",
                "topologyKey": "topology.kubernetes.io/zone",
                "nodeSelector": {"weltgewebe.net/data-node": "true"},
            },
        },
    }


def prove(args: argparse.Namespace) -> dict[str, Any]:
    if ref.output(["git", "status", "--porcelain"]):
        raise ref.ProofError("HA recovery proof requires a clean, commit-bound worktree")
    commit = ref.output(["git", "rev-parse", "HEAD"])
    timestamp = ref.output(["git", "show", "-s", "--format=%cI", "HEAD"])
    ref.require_host_tools()
    receipt = ref.tool_receipt()
    tools = receipt["tools"]
    kind, kubectl, kubectl_cnpg, kustomize, flux, helm = (
        tools[name]
        for name in (
            "kind",
            "kubectl",
            "kubectl_cnpg",
            "kustomize",
            "flux",
            "helm",
        )
    )
    restore_name = f"{args.cluster}-restore"
    owner_id = args.owner_id or ref.generate_owner_id("ha-proof")
    oci_host: dict[str, Any] | None = None
    node_image = receipt["kubernetes"]["kind_node_image"]
    if ref.controlled_oci_strict():
        oci_host = ref.prepare_controlled_oci_host("ha-recovery")
        node_image = oci_host["kind_node_image"]
    primary_oci_cluster: dict[str, Any] | None = None
    restore_oci_cluster: dict[str, Any] | None = None
    local_nats_dependency_supply: dict[str, Any] | None = None
    primary_cluster_retirement: dict[str, Any] | None = None
    lifecycle = ClusterLifecycle()
    object_store_created = False
    stopped_node = ""
    object_store_address = ""
    availability_monitor: ContinuousAvailabilityMonitor | None = None
    continuous_availability: dict[str, Any] | None = None
    direct_probe_pods: list[str] = []
    app_password = secrets.token_urlsafe(24)
    s3_access_key = f"{S3_ACCESS_KEY_PREFIX}{secrets.token_hex(8)}"
    s3_secret_key = secrets.token_urlsafe(32)
    postgres_contract = postgres_runtime_contract()
    capacity = capacity_preflight(keep_primary=args.keep)
    credentials = credential_preflight(s3_access_key, s3_secret_key, owner_id)
    proof_started = time.monotonic()
    try:
        create_kind_cluster(
            kind, args.cluster, node_image,
            "platform/clusters/ha/kind.yaml", commit, owner_id
        )
        lifecycle.primary_created = True
        local_nats_dependency_supply = preload_local_nats_dependencies(
            kind, args.cluster
        )
        if ref.controlled_oci_strict():
            primary_oci_cluster = ref.load_controlled_oci_into_kind(
                kind, args.cluster, "ha-recovery"
            )
        kubectl = require_active_cluster_context(kind, kubectl, args.cluster)
        image_ids = ref.build_images(kind, args.cluster, commit, timestamp)
        image_ids.update(
            prepare_api_upgrade_candidate(kind, args.cluster, commit)
        )
        ref.install_platform_components(kubectl, flux, helm, receipt["artifacts"], ref.control_plane_address(args.cluster))
        ref.run([kubectl, "wait", "--for=condition=Ready", "nodes", "--all", "--timeout=8m"])
        primary_dns_topology = configure_cluster_dns_ha(kubectl)
        _, _, object_store_address = start_external_object_store(
            args.cluster, commit, owner_id, s3_access_key, s3_secret_key
        )
        object_store_created = True
        ref.apply_file(kubectl, ROOT / "platform/infrastructure/ha-data/namespace.yaml")
        apply_secret_contracts(kubectl, app_password, s3_access_key, s3_secret_key)
        ref.apply_file(kubectl, ROOT / "platform/infrastructure/ha-data/object-store.yaml")
        apply_object_store_endpoint(kubectl, object_store_address)
        install_cert_manager(kubectl, receipt["artifacts"]["cert_manager"])
        primary_operator_nodes = install_cnpg(
            kubectl, receipt["artifacts"]["cloudnative_pg_operator"]
        )
        primary_barman_plugin_state = install_barman_cloud_plugin(
            kubectl,
            receipt["artifacts"]["barman_cloud_plugin"],
        )
        apply_ha_data(kubectl, kustomize, local_nats_dependency_supply)
        ref.wait_rollout(kubectl, "weltgewebe-data", "statefulset/nats", "12m")
        wait_cluster_ready(kubectl, "postgres-ha", "15m")
        primary_barman_sidecars = verify_barman_sidecar_images(
            kubectl, "postgres-ha"
        )
        ref.apply_file(kubectl, ROOT / "platform/apps/weltgewebe/migration/ha/namespace.yaml")
        apply_runtime_secret(
            kubectl,
            app_password,
            postgres_service=postgres_contract["primary_service"],
        )
        ref.apply_direct(kubectl, kustomize, "platform/apps/weltgewebe/migration/ha")
        ref.wait_condition(kubectl, "weltgewebe", "job/weltgewebe-migration", "Complete", "10m")
        migration_recovery = prove_migration_retry_and_idempotency(
            kubectl,
            kustomize,
            postgres_cluster=postgres_contract["cluster"],
        )
        ref.apply_direct(kubectl, kustomize, "platform/apps/weltgewebe/overlays/ha")
        ref.apply_direct(kubectl, kustomize, "platform/infrastructure/gateway")
        wait_api_replicas(kubectl)
        gateway_before = ha_gateway_baseline(kubectl, kind, args.cluster)

        topology = {
            "postgres": pod_topology(kubectl, "weltgewebe-data", "cnpg.io/cluster=postgres-ha"),
            "nats": pod_topology(kubectl, "weltgewebe-data", "app.kubernetes.io/name=nats"),
            "api": pod_topology(kubectl, "weltgewebe", "app.kubernetes.io/name=weltgewebe-api"),
        }
        for component, observed in topology.items():
            require_zones(observed, 3, component)
        direct_probe_pods = create_direct_api_probes(
            kubectl,
            [str(details["zone"]) for details in topology["api"].values() if isinstance(details, dict) and details.get("zone")],
        )

        marker = "00000000-0000-4000-8000-00000000a004"
        insert_domain_node(kubectl, marker, "T004 acknowledged domain mutation")
        wait_for_gateway_domain_node(kubectl, kind, args.cluster, marker)
        availability_monitor = ContinuousAvailabilityMonitor(
            kubectl,
            kind,
            args.cluster,
            marker,
            int(gateway_before["listener_port"]),
            direct_probe_pods,
        )
        availability_monitor.start()
        change_management = prove_api_upgrade_and_rollback(
            kubectl, kind, args.cluster, marker, gateway_before, direct_probe_pods
        )

        barman_leader_alignment = align_postgres_primary_with_barman_leader(
            kubectl_cnpg,
            kubectl,
            "postgres-ha",
            topology["postgres"],
            f"t004-align-{commit[:8]}",
        )
        primary_before = barman_leader_alignment["postgres_primary_aligned"]
        primary_topology = topology["postgres"].get(primary_before)
        if not primary_topology:
            raise ref.ProofError(f"primary pod missing from topology: {primary_before}")
        failure_zone = primary_topology["zone"]
        stopped_node = primary_topology["node"]
        if stopped_node != barman_leader_alignment["target_node"]:
            raise ref.ProofError(
                "PostgreSQL primary and Barman Cloud leader are not co-located "
                f"before failure: {barman_leader_alignment}"
            )
        alternate_zone = next(zone for zone in ("zone-a", "zone-b", "zone-c") if zone != failure_zone)
        nats_box_image = NATS_BOX_IMAGE
        nats_box_pull_policy: str | None = None
        if (
            local_nats_dependency_supply
            and local_nats_dependency_supply.get("mode") == "direct-digest-preload"
        ):
            nats_box_supply = local_nats_dependency_supply.get("images", {}).get(
                "nats-box"
            )
            if not isinstance(nats_box_supply, dict) or not isinstance(
                nats_box_supply.get("runtime_image"), str
            ):
                raise ref.ProofError("local nats-box dependency supply is missing")
            nats_box_image = nats_box_supply["runtime_image"]
            nats_box_pull_policy = "Never"
        create_nats_box(
            kubectl,
            alternate_zone,
            image=nats_box_image,
            image_pull_policy=nats_box_pull_policy,
        )
        domain_jetstream_before = domain_jetstream_contract(kubectl)
        nats(kubectl, ["stream", "add", "WG_PROOF", "--subjects", "wg.proof", "--storage", "file", "--replicas", "3", "--retention", "limits", "--max-msgs", "100", "--defaults"])
        nats(kubectl, ["pub", "wg.proof", "before-zone-failure"])
        if nats_message_count(kubectl) != 1:
            raise ref.ProofError("JetStream did not acknowledge the baseline message")

        # Capture the migration ledger while PostgreSQL is still healthy. Reading a
        # "baseline" after the node stop races CNPG promotion: currentPrimary may
        # already name the promoted pod before psql is ready there.
        migration_election_baseline = migration_history_digest(
            kubectl, cluster=postgres_contract["cluster"]
        )
        failure_started = time.monotonic()
        if availability_monitor is None:
            raise ref.ProofError("continuous availability monitor disappeared before zone failure")
        availability_monitor.mark_failure("single-zone-node-stop")
        ref.run(["docker", "stop", "--timeout", "10", stopped_node], timeout=60)
        start_migration_election_probe(
            kubectl,
            kustomize,
            healthy_zone=alternate_zone,
            baseline_history_sha256=migration_election_baseline,
        )
        def new_primary_probe():
            candidate = current_primary(kubectl)
            return candidate if candidate and candidate != primary_before else False
        primary_after = str(wait_until("PostgreSQL primary failover", new_primary_probe, timeout_seconds=180))
        postgres_rto = time.monotonic() - failure_started
        wait_until("acknowledged domain mutation after failover", lambda: node_exists(kubectl, marker), timeout_seconds=60)
        domain_jetstream_after = wait_until(
            "three-replica domain JetStream after zone failure",
            lambda: domain_jetstream_contract(kubectl),
            timeout_seconds=120,
            interval=1,
        )
        domain_jetstream_rto = time.monotonic() - failure_started
        gateway_addresses_after_failure = sorted(set(ref.gateway_addresses(kubectl)))
        try:
            gateway_recovery = wait_until(
                "Gateway API projection after zone failure",
                lambda: (
                    sample
                    if (
                        sample := gateway_observer_sample(
                            kind,
                            args.cluster,
                            gateway_addresses_after_failure,
                            int(gateway_before["listener_port"]),
                            marker,
                            observer_binding=availability_monitor.gateway_binding,
                        )
                    ).get("available")
                    else False
                ),
                timeout_seconds=180,
                interval=1,
            )
        except ref.ProofError:
            diagnostic_started = time.monotonic()
            diagnostic = api_readiness_failure_diagnostic(
                kubectl, availability_monitor
            )
            diagnostic["diagnostic_duration_seconds"] = round(
                time.monotonic() - diagnostic_started, 3
            )
            print(
                json.dumps(
                    {"zone_failure_readiness_diagnostic": diagnostic},
                    sort_keys=True,
                ),
                file=sys.stderr,
                flush=True,
            )
            raise
        api_validation_upper_bound = time.monotonic() - failure_started
        direct_address, direct_port = direct_api_probe_target(kubectl)
        wait_until(
            "direct API projection after zone failure",
            lambda: direct_api_probe_sample(
                kubectl, direct_probe_pods, direct_address, direct_port, marker
            ).get("available"),
            timeout_seconds=180,
            interval=1,
        )
        direct_api_validation_upper_bound = time.monotonic() - failure_started
        def nats_publish_probe():
            result = nats(kubectl, ["pub", "wg.proof", "after-zone-failure"], check=False)
            return result.returncode == 0
        wait_until("JetStream publish after zone failure", nats_publish_probe, timeout_seconds=120, interval=1)
        nats_rto = time.monotonic() - failure_started
        component_recovery_rto = nats_rto
        if nats_message_count(kubectl) != 2:
            raise ref.ProofError("JetStream lost an acknowledged message during zone failure")

        # The migration proof was started during the election but is intentionally
        # awaited only after the core PG/API/NATS recovery clock has stopped.
        migration_recovery["election_interruption"] = finish_migration_election_probe(
            kubectl,
            postgres_cluster=postgres_contract["cluster"],
            baseline_history_sha256=migration_election_baseline,
            healthy_zone=alternate_zone,
        )
        barman_leader_after_failure = wait_barman_plugin_leader_after_node_loss(
            kubectl, stopped_node
        )
        barman_leader_rto = time.monotonic() - failure_started
        barman_communication_after_failure = prove_barman_plugin_backup(
            kubectl,
            "postgres-ha",
            f"t004-failover-{commit[:8]}",
        )
        barman_rto = time.monotonic() - failure_started

        ref.run(["docker", "start", stopped_node], timeout=60)
        stopped_node = ""
        ref.run([kubectl, "wait", "--for=condition=Ready", f"node/{primary_topology['node']}", "--timeout=8m"])
        ref.wait_rollout(kubectl, "kube-system", "daemonset/cilium", "8m")
        ref.wait_rollout(kubectl, "kube-system", "daemonset/cilium-envoy", "8m")
        primary_barman_plugin_recovered = verify_barman_plugin_ha(kubectl)
        wait_cluster_ready(kubectl, "postgres-ha", "15m")
        ref.wait_rollout(kubectl, "weltgewebe-data", "statefulset/nats", "12m")
        wait_api_replicas(kubectl)
        recovered_topology = {
            "postgres": pod_topology(kubectl, "weltgewebe-data", "cnpg.io/cluster=postgres-ha"),
            "nats": pod_topology(kubectl, "weltgewebe-data", "app.kubernetes.io/name=nats"),
            "api": pod_topology(kubectl, "weltgewebe", "app.kubernetes.io/name=weltgewebe-api"),
        }
        for component, observed in recovered_topology.items():
            require_zones(observed, 3, f"recovered {component}")
        full_redundancy_rto = time.monotonic() - failure_started

        before_id = "00000000-0000-4000-8000-00000000b004"
        after_id = "00000000-0000-4000-8000-00000000c004"
        insert_domain_node(kubectl, before_id, "T004 before PITR target")
        backup_name = f"t004-{commit[:8]}"
        ref.apply_yaml(
            kubectl,
            {
                "apiVersion": "postgresql.cnpg.io/v1",
                "kind": "Backup",
                "metadata": {"name": backup_name, "namespace": "weltgewebe-data"},
                "spec": {
                    "method": "plugin",
                    "target": "prefer-standby",
                    "cluster": {"name": "postgres-ha"},
                    "pluginConfiguration": {
                        "name": "barman-cloud.cloudnative-pg.io"
                    },
                },
            },
        )
        backup = wait_backup_complete(kubectl, backup_name)
        target_time = psql(kubectl, "SELECT to_char(clock_timestamp() AT TIME ZONE 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')")
        wait_for_pitr_boundary(kubectl, target_time)
        insert_domain_node(kubectl, after_id, "T004 after PITR target")
        primary_wal_archive = measure_wal_archive(
            kubectl,
            cluster="postgres-ha",
            object_store_address=object_store_address,
            s3_access_key=s3_access_key,
            s3_secret_key=s3_secret_key,
        )
        if availability_monitor is None:
            raise ref.ProofError("continuous availability monitor disappeared before closeout")
        continuous_availability = availability_monitor.stop(
            "application-service-window-complete-before-blank-PITR-restore"
            if args.keep
            else "intentional-primary-cluster-retirement-before-blank-PITR-restore"
        )
        availability_monitor = None
        direct_probe_contract = {"mode": "same-namespace-zone-probes-any", "pods": list(direct_probe_pods)}
        direct_probe_cleanup = delete_direct_api_probes(kubectl, direct_probe_pods)
        if direct_probe_cleanup["returncode"] != 0:
            raise ref.ProofError("direct API proof-pod cleanup failed: " + json.dumps(direct_probe_cleanup, sort_keys=True))
        direct_probe_pods = []
        continuous_availability["direct_api"]["probe_contract"] = direct_probe_contract
        continuous_availability["direct_api"]["probe_cleanup"] = direct_probe_cleanup
        gateway_failover = continuous_availability["gateway"]["failover_recovery"]
        direct_api_failover = continuous_availability["direct_api"]["failover_recovery"]
        service_rto = float(gateway_failover["observed_rto_seconds"])
        direct_api_observed_rto = float(direct_api_failover["observed_rto_seconds"])

        primary_cluster_retirement = create_blank_restore_cluster(
            kind,
            args.cluster,
            restore_name,
            node_image,
            commit,
            owner_id,
            keep_primary=args.keep,
            lifecycle=lifecycle,
        )
        if ref.controlled_oci_strict():
            restore_oci_cluster = ref.load_controlled_oci_into_kind(
                kind, restore_name, "ha-recovery"
            )
        restore_kubectl = require_active_cluster_context(
            kind, kubectl, restore_name
        )
        ref.run([restore_kubectl, "wait", "--for=condition=Ready", "nodes", "--all", "--timeout=8m"])
        restore_dns_topology = configure_cluster_dns_ha(restore_kubectl)
        ref.apply_file(restore_kubectl, ROOT / "platform/infrastructure/ha-data/namespace.yaml")
        apply_secret_contracts(restore_kubectl, app_password, s3_access_key, s3_secret_key)
        ref.apply_file(restore_kubectl, ROOT / "platform/infrastructure/ha-data/object-store.yaml")
        apply_object_store_endpoint(restore_kubectl, object_store_address)
        install_cert_manager(
            restore_kubectl,
            receipt["artifacts"]["cert_manager"],
        )
        restore_operator_nodes = install_cnpg(
            restore_kubectl,
            receipt["artifacts"]["cloudnative_pg_operator"],
        )
        restore_barman_plugin_state = install_barman_cloud_plugin(
            restore_kubectl,
            receipt["artifacts"]["barman_cloud_plugin"],
        )
        ref.apply_file(
            restore_kubectl,
            ROOT / "platform/infrastructure/ha-data/barman-object-store.yaml",
        )
        ref.apply_file(
            restore_kubectl,
            ROOT / "platform/infrastructure/ha-data/postgres-image-catalog.yaml",
        )
        restore_started = time.monotonic()
        ref.apply_yaml(restore_kubectl, restore_cluster_document(target_time))
        wait_cluster_ready(restore_kubectl, "postgres-restore", "20m")
        restore_rto = time.monotonic() - restore_started
        continuity_started = time.monotonic()
        restore_barman_sidecars = verify_barman_sidecar_images(
            restore_kubectl, "postgres-restore"
        )
        restore_wal_archive = measure_wal_archive(
            restore_kubectl,
            cluster="postgres-restore",
            object_store_address=object_store_address,
            s3_access_key=s3_access_key,
            s3_secret_key=s3_secret_key,
        )
        continuity_validation_seconds = time.monotonic() - continuity_started
        restored_topology = pod_topology(restore_kubectl, "weltgewebe-data", "cnpg.io/cluster=postgres-restore")
        require_zones(restored_topology, 3, "restored postgres")
        preserved = {
            marker: node_exists(restore_kubectl, marker, cluster="postgres-restore"),
            before_id: node_exists(restore_kubectl, before_id, cluster="postgres-restore"),
            after_id: node_exists(restore_kubectl, after_id, cluster="postgres-restore"),
        }
        if preserved != {marker: True, before_id: True, after_id: False}:
            raise ref.ProofError(f"PITR data comparison failed: {preserved}")

        result = {
            "schema_version": 1, "status": "pass", "commit": commit,
            "primary_cluster": args.cluster, "restore_cluster": restore_name,
            "primary_cluster_retired_before_restore": (
                primary_cluster_retirement is not None
            ),
            "primary_cluster_retirement": primary_cluster_retirement,
            "tool_lock_sha256": receipt["lock_sha256"], "image_ids": image_ids,
            "oci_controlled_source": {
                "strict": ref.controlled_oci_strict(),
                "host": oci_host,
                "primary_cluster": primary_oci_cluster,
                "restore_cluster": restore_oci_cluster,
            },
            "local_nats_dependency_supply": local_nats_dependency_supply,
            "operator_nodes": {
                "primary": primary_operator_nodes,
                "restore": restore_operator_nodes,
            },
            "cluster_dns": {
                "primary": primary_dns_topology,
                "restore": restore_dns_topology,
            },
            "barman_plugin": {
                "primary_initial": primary_barman_plugin_state,
                "leader_alignment": barman_leader_alignment,
                "leader_after_zone_failure": barman_leader_after_failure,
                "communication_after_zone_failure": barman_communication_after_failure,
                "primary_recovered": primary_barman_plugin_recovered,
                "restore": restore_barman_plugin_state,
            },
            "barman_sidecar_images": {
                "primary": primary_barman_sidecars,
                "restore": restore_barman_sidecars,
            },
            "topology": topology,
            "recovered_topology": recovered_topology,
            "restored_topology": restored_topology,
            "preflight": {
                "capacity": capacity,
                "credentials": credentials,
            },
            "migration_recovery": migration_recovery,
            "continuous_availability": continuous_availability,
            "zone_failure": {
                "zone": failure_zone, "node": primary_topology["node"],
                "postgres_primary_before": primary_before, "postgres_primary_after": primary_after,
                "barman_leader_rto_seconds": round(barman_leader_rto, 3),
                "barman_plugin_rto_seconds": round(barman_rto, 3),
                "postgres_rto_seconds": round(postgres_rto, 3),
                # Backward-compatible validation upper bounds; continuous samples below
                # carry the service-level observation without sequential-probe inflation.
                "api_rto_seconds": round(api_validation_upper_bound, 3),
                "direct_api_rto_seconds": round(direct_api_validation_upper_bound, 3),
                "nats_rto_seconds": round(nats_rto, 3),
                "domain_jetstream_rto_seconds": round(domain_jetstream_rto, 3),
                "domain_jetstream_before": domain_jetstream_before,
                "domain_jetstream_after": domain_jetstream_after,
                "service_rto_seconds": round(service_rto, 3),
                "direct_api_observed_rto_seconds": round(direct_api_observed_rto, 3),
                "component_recovery_rto_seconds": round(component_recovery_rto, 3),
                "full_redundancy_rto_seconds": round(full_redundancy_rto, 3),
                "rto_semantics": {
                    "service": (
                        "continuous Gateway observation from injected zone failure to the first "
                        "sampled recovery; zero means no outage was sampled and does not exclude "
                        "a sub-resolution interruption"
                    ),
                    "components": (
                        "conservative elapsed time until PostgreSQL promotion, Gateway/direct API "
                        "readbacks and JetStream publish have all been validated"
                    ),
                    "full_redundancy": (
                        "stopped node rejoined and PostgreSQL, NATS, API and Barman returned to "
                        "three-zone readiness"
                    ),
                },
                "gateway_recovery_sample": gateway_recovery,
                "acknowledged_domain_mutation_preserved": True,
                "acknowledged_jetstream_messages": 2,
            },
            "backup": {
                "name": backup_name,
                "phase": backup.get("status", {}).get("phase"),
                "target_time": target_time,
                "required_archived_wal": primary_wal_archive["required_wal"],
                "observed_archived_wal": primary_wal_archive["observed_wal"],
                "wal_archive_latency_seconds": primary_wal_archive["latency_seconds"],
                "wal_archive": primary_wal_archive,
            },
            "restore": {
                "rto_seconds": round(restore_rto, 3),
                "pitr_comparison": preserved,
                "blank_kind_cluster": True,
                "continued_wal_archiving": restore_wal_archive,
                "continuity_validation_seconds": round(
                    continuity_validation_seconds, 3
                ),
            },
            "rpo": {
                "acknowledged_domain_mutations_lost": 0,
                "acknowledged_jetstream_messages_lost": 0,
                "measured_archive_rpo_upper_bound_seconds": max(
                    primary_wal_archive["latency_seconds"],
                    restore_wal_archive["latency_seconds"],
                ),
                "measurement_model": (
                    "maximum observed forced-WAL pg_stat_archiver latency under the "
                    "reference-cell workload; preserved for backward compatibility"
                ),
                "measured_object_store_visibility_rpo_upper_bound_seconds": max(
                    primary_wal_archive["object_store_visibility_latency_seconds"],
                    restore_wal_archive["object_store_visibility_latency_seconds"],
                ),
                "object_store_measurement_model": (
                    "maximum observed forced-WAL latency until the exact segment identity is "
                    "read-only visible in the external proof object store"
                ),
                "primary_wal_archive_latency_seconds": primary_wal_archive["latency_seconds"],
                "restore_wal_archive_latency_seconds": restore_wal_archive["latency_seconds"],
                "primary_wal_object_visibility_latency_seconds": primary_wal_archive[
                    "object_store_visibility_latency_seconds"
                ],
                "restore_wal_object_visibility_latency_seconds": restore_wal_archive[
                    "object_store_visibility_latency_seconds"
                ],
            },
            "change_management": change_management,
            "gateway_before": gateway_before,
            "proof_duration_seconds": round(time.monotonic() - proof_started, 3),
            "maintainability": {
                "runtime_optimizations_applied": [],
                "reason": "T016 applies no unmeasured runtime optimization",
            },
            "proof_owner_id": owner_id,
            "production_changed": False,
            "does_not_establish": [
                "production rollout", "managed multi-region object-store durability",
                "multi-region production HA", "multi-cloud production HA", "production HA cutover",
                "RTO or RPO under production load", "survival of two simultaneous failure domains",
                "semantic compatibility of a distinct application release",
                "production error-budget consumption under representative traffic",
                "external load-balancer reachability from outside the kind bridge",
            ],
        }
        target = CACHE / "receipts"
        target.mkdir(parents=True, exist_ok=True)
        owner_receipt_key = hashlib.sha256(owner_id.encode("utf-8")).hexdigest()[:16]
        path = target / (
            f"{args.cluster}-{commit}-{owner_receipt_key}-ha-recovery.json"
        )
        ref.write_json_atomic(path, result)
        result["receipt_path"] = str(path)
        result["receipt_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        return result
    except Exception:
        if availability_monitor is not None:
            try:
                availability = availability_monitor.stop("proof-failure-before-service-window-close")
                print(
                    "continuous availability at proof failure: "
                    + json.dumps(availability, sort_keys=True),
                    file=sys.stderr,
                )
            except Exception as monitor_error:
                print(
                    f"continuous availability monitor cleanup failed: {monitor_error}",
                    file=sys.stderr,
                )
            availability_monitor = None
        if lifecycle.primary_created:
            ref.configure_cluster_access(kind, args.cluster)
            ha_diagnostic_snapshot(kubectl, args.cluster)
            ref.diagnostic_snapshot(kubectl, args.cluster)
        if lifecycle.restore_created:
            ref.configure_cluster_access(kind, restore_name)
            ha_diagnostic_snapshot(kubectl, restore_name)
            ref.diagnostic_snapshot(kubectl, restore_name)
        raise
    finally:
        active_error = sys.exc_info()[1]
        if stopped_node:
            subprocess.run(["docker", "start", stopped_node], capture_output=True)
        if not args.keep and (
            lifecycle.restore_created
            or lifecycle.primary_created
            or object_store_created
        ):
            cleanup = reconcile_owned_ha_resources(
                kind,
                args.cluster,
                commit,
                owner_id,
                include_restore=lifecycle.restore_created,
                include_primary=lifecycle.primary_created,
                include_object_store=object_store_created,
            )
            if cleanup["errors"]:
                detail = json.dumps(cleanup, sort_keys=True)
                if active_error is None:
                    raise ref.ProofError(
                        f"HA proof cleanup incomplete: {detail}"
                    )
                print(
                    f"HA proof cleanup incomplete while preserving original failure: {detail}",
                    file=sys.stderr,
                )


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    proof_parser = sub.add_parser("proof")
    proof_parser.add_argument("--cluster", default=DEFAULT_CLUSTER)
    proof_parser.add_argument("--keep", action="store_true")
    proof_parser.add_argument("--owner-id")
    down = sub.add_parser("down")
    down.add_argument("--cluster", default=DEFAULT_CLUSTER)
    down.add_argument("--commit", required=True)
    down.add_argument("--owner-id", required=True)
    down.add_argument("--receipt", type=Path)
    return parser


def _captured_subprocess_evidence(error: subprocess.CalledProcessError) -> str:
    fields: list[str] = []
    for label, value in (("stdout", error.stdout), ("stderr", error.stderr)):
        if not value:
            continue
        payload = value if isinstance(value, bytes) else str(value).encode("utf-8")
        fields.append(f"{label}_bytes={len(payload)}")
        fields.append(f"{label}_sha256={hashlib.sha256(payload).hexdigest()}")
    return "; ".join(fields)


def main() -> int:
    args = argument_parser().parse_args()
    try:
        receipt = ref.tool_receipt()
        if args.command == "down":
            kind = receipt["tools"]["kind"]
            cleanup = reconcile_owned_ha_resources(
                kind, args.cluster, args.commit, args.owner_id
            )
            cleanup_receipt = {
                "schema_version": 1,
                "status": "pass" if not cleanup["errors"] else "fail",
                "cluster": args.cluster,
                "commit": args.commit,
                "owner_id": args.owner_id,
                "owned_resources_remaining": bool(cleanup["errors"]),
                "cleanup": cleanup,
            }
            if args.receipt:
                ref.write_json_atomic(args.receipt, cleanup_receipt)
            print(json.dumps(cleanup_receipt, sort_keys=True))
            return 1 if cleanup["errors"] else 0
        prove(args)
    except (ref.ProofError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        if isinstance(error, subprocess.CalledProcessError):
            detail = f"subprocess exited with status {error.returncode}"
            evidence = _captured_subprocess_evidence(error)
            if evidence:
                detail += f"; {evidence}"
        elif isinstance(error, subprocess.TimeoutExpired):
            detail = "subprocess timed out"
        else:
            detail = str(error)
        print(f"HA recovery proof failed: {detail}", file=sys.stderr)
        return 1
    print(json.dumps({"status": "pass"}, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
