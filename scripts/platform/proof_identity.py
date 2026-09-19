#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
FULL_GIT_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
COMMON_PROOF_INPUTS = (
    ".github/workflows/kubernetes-platform.yml",
    ".github/workflows/kubernetes-platform-proof.yml",
    ".dockerignore",
    ".python-version",
    "Cargo.toml",
    "Cargo.lock",
    "toolchain.versions.yml",
    "tools/py/",
    "apps/api/",
    "apps/web/",
    "configs/",
    "scripts/dev/",
    "scripts/ops/",
    "policies/",
    "infra/compose/compose.prod.yml",
    "platform/",
    "scripts/security/",
    "repo.meta.yaml",
)

SUITE_INPUTS = {
    "kind-gitops": COMMON_PROOF_INPUTS
    + (
        "scripts/platform/bootstrap_tools.py",
        "scripts/platform/proof_identity.py",
        "scripts/platform/oci_proof_mirror.py",
        "scripts/platform/validate_platform.py",
        "scripts/platform/kind_reference.py",
        "scripts/ci/tests/test_kubernetes_platform_contract.py",
        "scripts/ci/tests/test_kubernetes_platform_workflow.py",
        "scripts/ci/tests/test_kubernetes_python_bootstrap.py",
        "scripts/ci/tests/test_trivy_rendered_manifests.py",
    ),
    "ha-recovery": COMMON_PROOF_INPUTS
    + (
        "scripts/platform/bootstrap_tools.py",
        "scripts/platform/proof_identity.py",
        "scripts/platform/oci_proof_mirror.py",
        "scripts/platform/validate_platform.py",
        "scripts/platform/kind_reference.py",
        "scripts/platform/ha_reference.py",
        "scripts/platform/ha_availability.py",
        "scripts/platform/ha_common.py",
        "scripts/platform/ha_dependencies.py",
        "scripts/platform/ha_migration.py",
        "scripts/platform/ha_wal.py",
        "scripts/ci/tests/test_kubernetes_platform_contract.py",
        "scripts/ci/tests/test_kubernetes_platform_workflow.py",
        "scripts/ci/tests/test_kubernetes_python_bootstrap.py",
        "scripts/ci/tests/test_trivy_rendered_manifests.py",
        "scripts/ci/tests/test_kubernetes_ha_contract.py",
    ),
    "staging-cell": COMMON_PROOF_INPUTS
    + (
        "scripts/platform/bootstrap_tools.py",
        "scripts/platform/proof_identity.py",
        "scripts/platform/validate_platform.py",
        "scripts/platform/kind_reference.py",
        "scripts/platform/staging_cell.py",
        "scripts/ci/tests/test_kubernetes_platform_contract.py",
        "scripts/ci/tests/test_staging_cell_runtime_contract.py",
        "scripts/ci/tests/test_staging_gateway_contract.py",
    ),
}

STAGING_EVIDENCE_ROOT = Path("docs/proofs/kubernetes-staging-cell")
STAGING_EVIDENCE_FILES = frozenset(
    {"identity.json", "record.json", "proof.json", "attestation.json"}
)

STAGING_RECEIPT_CONTRACT = {
    "cell-bootstrap": ("cell-bootstrap.json", "gateway-ready"),
    "gateway-proof": ("gateway-proof.json", "gateway-ready"),
    "host-gateway-proof": ("host-gateway-proof.json", "host-gateway-readback-verified"),
    "backup-delete-to-prove-down": (
        "backup-delete-to-prove-down.json",
        "backup-created-cluster-deleted-primary-data-empty",
    ),
    "backup-delete-to-prove-rebuild": (
        "backup-delete-to-prove-rebuild.json",
        "backup-restored-infrastructure-ready-app-reactivation-required",
    ),
    "backup-delete-to-prove": (
        "backup-delete-to-prove.json",
        "backup-delete-to-prove-verified",
    ),
}



class IdentityError(RuntimeError):
    pass


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _tracked_files() -> tuple[str, ...]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return tuple(
        item.decode("utf-8")
        for item in result.stdout.split(b"\0")
        if item
    )


def _matches(path: str, selectors: tuple[str, ...]) -> bool:
    return any(path == selector or (selector.endswith("/") and path.startswith(selector)) for selector in selectors)


def input_manifest(suite: str) -> list[dict[str, str]]:
    try:
        selectors = SUITE_INPUTS[suite]
    except KeyError as error:
        raise IdentityError(f"unsupported proof suite: {suite}") from error
    selected = sorted(path for path in _tracked_files() if _matches(path, selectors))
    missing = [
        selector
        for selector in selectors
        if not selector.endswith("/") and selector not in selected
    ]
    if missing:
        raise IdentityError(f"proof invalidation inputs are missing: {missing}")
    return [
        {
            "path": path,
            "sha256": _sha256_bytes((ROOT / path).read_bytes()),
        }
        for path in selected
    ]


def compute_identity(suite: str, source_commit: str) -> dict[str, Any]:
    if not FULL_GIT_OBJECT_ID.fullmatch(source_commit):
        raise IdentityError("proof source commit must be a full lowercase Git object id")
    manifest = input_manifest(suite)
    manifest_sha256 = _sha256_bytes(_canonical_json({"files": manifest}))
    stable = {
        "schema_version": 2,
        "suite": suite,
        "input_manifest_sha256": manifest_sha256,
        "tool_lock_sha256": _sha256_bytes(
            (ROOT / "platform/toolchain.lock.json").read_bytes()
        ),
        "oci_mirror_lock_sha256": _sha256_bytes(
            (ROOT / "platform/oci-proof-mirror.lock.json").read_bytes()
        ),
        "invalidation_contract": list(SUITE_INPUTS[suite]),
    }
    return {
        **stable,
        "source_commit": source_commit,
        "identity_sha256": _sha256_bytes(_canonical_json(stable)),
    }


def _checkout_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    commit = result.stdout.strip()
    if not FULL_GIT_OBJECT_ID.fullmatch(commit):
        raise IdentityError("checked-out commit is not a full lowercase Git object id")
    return commit


def _read_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise IdentityError(f"invalid proof evidence {path}: {error}") from error
    if not isinstance(payload, dict):
        raise IdentityError(f"proof evidence is not an object: {path}")
    return payload


BLOCKED_REGISTRIES = frozenset(
    {
        "registry-1.docker.io",
        "auth.docker.io",
        "production.cloudflare.docker.com",
        "quay.io",
        "ghcr.io",
    }
)


def _validate_receipt_header(
    receipt: Any,
    *,
    label: str,
    lock_sha256: str,
) -> dict[str, Any]:
    if not isinstance(receipt, dict):
        raise IdentityError(f"reusable proof {label} OCI receipt is missing")
    if receipt.get("status") != "pass" or receipt.get("strict") is not True:
        raise IdentityError(f"reusable proof {label} OCI receipt is not strictly passing")
    if receipt.get("lock_sha256") != lock_sha256:
        raise IdentityError(f"reusable proof {label} OCI receipt uses a different mirror lock")
    return receipt


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


def _validate_registry_blockades(
    receipt: dict[str, Any], *, label: str
) -> set[str]:
    blockades = receipt.get("registry_blockades")
    if not isinstance(blockades, list) or not blockades:
        raise IdentityError(f"reusable proof {label} lacks registry blockade evidence")
    observed_nodes: set[str] = set()
    for blockade in blockades:
        if not isinstance(blockade, dict):
            raise IdentityError(f"reusable proof {label} registry blockade is malformed")
        node = blockade.get("node")
        registries = blockade.get("registries")
        if not isinstance(node, str) or not node or node in observed_nodes:
            raise IdentityError(f"reusable proof {label} registry blockade nodes are invalid")
        observed_nodes.add(node)
        if not isinstance(registries, dict) or set(registries) != BLOCKED_REGISTRIES:
            raise IdentityError(f"reusable proof {label} registry blockade inventory drifted")
        for registry, addresses in registries.items():
            if not isinstance(addresses, dict):
                raise IdentityError(
                    f"reusable proof {label} registry blockade for {registry} is malformed"
                )
            if addresses.get("ipv4") != ["127.0.0.1"]:
                raise IdentityError(
                    f"reusable proof {label} registry IPv4 blockade did not pass for {registry}"
                )
            if addresses.get("ipv6") not in ([], ["::1"]):
                raise IdentityError(
                    f"reusable proof {label} registry IPv6 blockade did not pass for {registry}"
                )
    return observed_nodes


def _validate_cached_cluster_image(
    observed: Any,
    spec: dict[str, Any],
    *,
    expected_cluster: str,
    host_image_id: str,
    label: str,
    name: str,
) -> set[str]:
    canonical = spec.get("canonical")
    local_ref = spec.get("local_ref")
    locked_digest = spec.get("digest")
    if not all(isinstance(value, str) for value in (canonical, local_ref, locked_digest)):
        raise IdentityError(f"current OCI mirror image is invalid: {name}")
    expected_runtime = _normalize_oci_reference(local_ref)
    if not isinstance(observed, dict):
        raise IdentityError(f"reusable proof {label} OCI image is invalid: {name}")
    image_id = observed.get("cri_image_id")
    platform_digest = observed.get("platform_target_digest")
    if (
        observed.get("canonical") != canonical
        or observed.get("local_ref") != local_ref
        or observed.get("digest_ref") != f"{local_ref}@{locked_digest}"
        or observed.get("runtime_ref") != expected_runtime
        or observed.get("locked_index_digest") != locked_digest
        or observed.get("image_id") != image_id
        or image_id != host_image_id
        or not isinstance(image_id, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None
        or not isinstance(platform_digest, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", platform_digest) is None
    ):
        raise IdentityError(
            f"reusable proof {label} OCI image binding is invalid: {name}"
        )
    nodes = observed.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise IdentityError(
            f"reusable proof {label} OCI node image bindings are missing: {name}"
        )
    node_names: set[str] = set()
    canonical_name = _normalize_oci_reference(canonical).rsplit("@", 1)[0]
    for node in nodes:
        if not isinstance(node, dict):
            raise IdentityError(
                f"reusable proof {label} OCI node binding is invalid: {name}"
            )
        node_name = node.get("node")
        if not isinstance(node_name, str) or not node_name or node_name in node_names:
            raise IdentityError(
                f"reusable proof {label} OCI node inventory is invalid: {name}"
            )
        if not node_name.startswith(f"{expected_cluster}-"):
            raise IdentityError(
                f"reusable proof {label} OCI node cluster binding drifted: {name}"
            )
        node_names.add(node_name)
        repo_tags = node.get("repo_tags")
        repo_digests = node.get("repo_digests")
        selected_repo_digest = node.get("selected_repo_digest")
        containerd_target_digest = node.get("containerd_target_digest")
        evidence_kind = node.get("platform_evidence_kind")
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
            evidence_kind == "cri_repo_digest"
            and isinstance(repo_digests, list)
            and all(isinstance(item, str) for item in repo_digests)
            and selected_repo_digest in repo_digests
            and (selected_repo_digest in allowed_repo_digests or import_digest)
            and containerd_target_digest is None
        )
        containerd_evidence = (
            evidence_kind == "containerd_target_digest"
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
            or not all(isinstance(item, str) for item in repo_tags)
            or expected_runtime not in repo_tags
            or not (repo_digest_evidence or containerd_evidence)
        ):
            raise IdentityError(
                f"reusable proof {label} OCI node binding drifted: {name}"
            )
    return node_names


def _validate_controlled_oci_proof(
    identity: dict[str, Any], proof: dict[str, Any]
) -> None:
    suite = identity.get("suite")
    if suite not in SUITE_INPUTS:
        raise IdentityError(f"reusable proof has unsupported OCI suite: {suite}")
    lock_path = ROOT / "platform/oci-proof-mirror.lock.json"
    try:
        lock_bytes = lock_path.read_bytes()
        lock = json.loads(lock_bytes)
    except (OSError, json.JSONDecodeError) as error:
        raise IdentityError(f"current OCI mirror lock is unreadable: {error}") from error
    lock_sha256 = _sha256_bytes(lock_bytes)
    if identity.get("oci_mirror_lock_sha256") != lock_sha256:
        raise IdentityError("proof identity OCI mirror lock does not match current inputs")
    images = lock.get("images")
    if not isinstance(images, dict) or not images:
        raise IdentityError("current OCI mirror lock image inventory is invalid")
    requested_suites = {suite, "app-build"}
    expected_host = {
        name
        for name, spec in images.items()
        if isinstance(spec, dict)
        and isinstance(spec.get("suites"), list)
        and set(spec["suites"]).intersection(requested_suites)
    }
    expected_cluster = {
        name
        for name in expected_host
        if images[name].get("load_into_kind") is True
    }
    if not expected_host or not expected_cluster:
        raise IdentityError("current OCI mirror suite inventory is incomplete")

    controlled = proof.get("oci_controlled_source")
    if not isinstance(controlled, dict) or controlled.get("strict") is not True:
        raise IdentityError("reusable proof lacks strict controlled OCI source evidence")
    host = _validate_receipt_header(
        controlled.get("host"), label="host", lock_sha256=lock_sha256
    )
    host_images = host.get("images")
    if not isinstance(host_images, dict) or set(host_images) != expected_host:
        raise IdentityError("reusable proof host OCI image inventory drifted")
    if host.get("selected_count") != len(expected_host) or host.get("failures") != {}:
        raise IdentityError("reusable proof host OCI count or failure contract drifted")
    for name, observed in host_images.items():
        if (
            not isinstance(observed, dict)
            or observed.get("source") != "local-verified"
            or not isinstance(observed.get("image_id"), str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", observed["image_id"])
        ):
            raise IdentityError(f"reusable proof host OCI image binding is invalid: {name}")

    if suite == "kind-gitops":
        cluster_fields = (("cluster", proof.get("cluster")),)
    else:
        primary_cluster = proof.get("primary_cluster")
        restore_cluster = proof.get("restore_cluster")
        if not isinstance(primary_cluster, str) or not primary_cluster:
            raise IdentityError("reusable proof primary_cluster binding is missing")
        if not isinstance(restore_cluster, str) or not restore_cluster:
            raise IdentityError("reusable proof restore_cluster binding is missing")
        if primary_cluster == restore_cluster:
            raise IdentityError("reusable proof HA cluster bindings must be distinct")
        cluster_fields = (
            ("primary_cluster", primary_cluster),
            ("restore_cluster", restore_cluster),
        )
    cluster_node_inventories: dict[str, set[str]] = {}
    for field, expected_cluster_name in cluster_fields:
        receipt = _validate_receipt_header(
            controlled.get(field), label=field, lock_sha256=lock_sha256
        )
        if not isinstance(expected_cluster_name, str) or not expected_cluster_name:
            raise IdentityError(f"reusable proof {field} cluster binding is missing")
        if receipt.get("cluster") != expected_cluster_name:
            raise IdentityError(f"reusable proof {field} cluster binding drifted")
        cluster_images = receipt.get("images")
        if not isinstance(cluster_images, dict) or set(cluster_images) != expected_cluster:
            raise IdentityError(f"reusable proof {field} OCI image inventory drifted")
        if receipt.get("loaded_count") != len(expected_cluster):
            raise IdentityError(f"reusable proof {field} OCI image count drifted")
        image_nodes: set[str] | None = None
        for name, observed in cluster_images.items():
            host_image_id = host_images[name]["image_id"]
            current_nodes = _validate_cached_cluster_image(
                observed,
                images[name],
                expected_cluster=expected_cluster_name,
                host_image_id=host_image_id,
                label=field,
                name=name,
            )
            if image_nodes is None:
                image_nodes = current_nodes
            elif current_nodes != image_nodes:
                raise IdentityError(
                    f"reusable proof {field} OCI image node inventories disagree"
                )
        blockade_nodes = _validate_registry_blockades(receipt, label=field)
        if image_nodes != blockade_nodes:
            raise IdentityError(
                f"reusable proof {field} OCI image and blockade nodes disagree"
            )
        if image_nodes is None:
            raise IdentityError(f"reusable proof {field} OCI node inventory is missing")
        if any(image_nodes.intersection(nodes) for nodes in cluster_node_inventories.values()):
            raise IdentityError("reusable proof HA cluster node inventories overlap")
        cluster_node_inventories[field] = image_nodes


def _canonical_sha(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not FULL_GIT_OBJECT_ID.fullmatch(value):
        raise IdentityError(f"{label} is not a canonical Git object id")
    return value


def _validate_staging_cell_proof(
    identity: dict[str, Any], proof: dict[str, Any]
) -> None:
    if proof.get("suite") != "staging-cell":
        raise IdentityError("staging proof suite binding is missing")
    commit = _canonical_sha(proof.get("commit"), label="staging proof commit")
    controller = _canonical_sha(
        proof.get("controller_commit"), label="staging controller commit"
    )
    source = _canonical_sha(
        proof.get("source_commit"), label="staging proof source commit"
    )
    _canonical_sha(proof.get("release_commit"), label="staging release commit")
    if commit != controller or source != commit:
        raise IdentityError("staging proof is not bound to its exact controller commit")
    if proof.get("tool_lock_sha256") != identity.get("tool_lock_sha256"):
        raise IdentityError("staging proof tool lock differs from the proof identity")
    if proof.get("production_changed") is not False:
        raise IdentityError("staging proof must assert production_changed=false")
    if proof.get("acceptance") != "backup-delete-to-prove-v1":
        raise IdentityError("staging proof acceptance contract is unsupported")
    receipts = proof.get("receipts")
    if not isinstance(receipts, dict) or set(receipts) != set(STAGING_RECEIPT_CONTRACT):
        raise IdentityError("staging proof receipt inventory is incomplete")
    for label, (_filename, expected_status) in STAGING_RECEIPT_CONTRACT.items():
        item = receipts.get(label)
        if (
            not isinstance(item, dict)
            or set(item) != {"sha256", "status"}
            or item.get("status") != expected_status
            or not isinstance(item.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is None
        ):
            raise IdentityError(f"staging proof receipt binding drifted: {label}")


SUITE_VALIDATORS = {
    "kind-gitops": _validate_controlled_oci_proof,
    "ha-recovery": _validate_controlled_oci_proof,
    "staging-cell": _validate_staging_cell_proof,
}


def _validate_suite_proof(identity: dict[str, Any], proof: dict[str, Any]) -> None:
    suite = identity.get("suite")
    validator = SUITE_VALIDATORS.get(suite)
    if validator is None:
        raise IdentityError(f"reusable proof has unsupported validator suite: {suite}")
    validator(identity, proof)


def summarize_staging_proof(
    identity_path: Path, state_root: Path, output: Path
) -> dict[str, Any]:
    identity = _read_object(identity_path)
    if identity.get("suite") != "staging-cell":
        raise IdentityError("staging summary requires the staging-cell suite")
    expected = compute_identity(
        identity.get("suite", ""), identity.get("source_commit", "")
    )
    if identity != expected:
        raise IdentityError("staging proof identity no longer matches current inputs")
    checkout_commit = _checkout_commit()
    if identity.get("source_commit") != checkout_commit:
        raise IdentityError("staging identity must target the checked-out commit")

    receipt_root = state_root.expanduser().resolve() / "receipts"
    payloads: dict[str, dict[str, Any]] = {}
    hashes: dict[str, str] = {}
    for label, (filename, expected_status) in STAGING_RECEIPT_CONTRACT.items():
        path = receipt_root / filename
        raw = path.read_bytes()
        payload = _read_object(path)
        if (
            payload.get("schema_version") != 1
            or payload.get("status") != expected_status
            or payload.get("production_changed") is not False
        ):
            raise IdentityError(f"staging runtime receipt is not terminal: {label}")
        payloads[label] = payload
        hashes[label] = _sha256_bytes(raw)

    rebuild = payloads["backup-delete-to-prove-rebuild"]
    terminal = payloads["backup-delete-to-prove"]
    controller = _canonical_sha(
        terminal.get("controller_commit"), label="terminal staging controller"
    )
    if controller != checkout_commit or rebuild.get("controller_commit") != controller:
        raise IdentityError("staging recovery was not executed by the target controller")
    release = _canonical_sha(
        terminal.get("active_commit"), label="terminal staging release"
    )
    if terminal.get("backup_down_receipt_sha256") != hashes["backup-delete-to-prove-down"]:
        raise IdentityError("terminal staging proof lost its backup-down binding")
    if terminal.get("backup_rebuild_receipt_sha256") != hashes["backup-delete-to-prove-rebuild"]:
        raise IdentityError("terminal staging proof lost its rebuild binding")
    if terminal.get("post_restore_cell_receipt_sha256") != hashes["cell-bootstrap"]:
        raise IdentityError("terminal staging proof lost its restored cell binding")
    if terminal.get("post_restore_gateway_receipt_sha256") != hashes["gateway-proof"]:
        raise IdentityError("terminal staging proof lost its restored gateway binding")
    if terminal.get("host_gateway_receipt_sha256") != hashes["host-gateway-proof"]:
        raise IdentityError("terminal staging proof lost its host-gateway binding")
    if payloads["cell-bootstrap"].get("active_commit") != release:
        raise IdentityError("restored cell is not bound to the proven release")
    if payloads["gateway-proof"].get("active_commit") != release:
        raise IdentityError("restored gateway is not bound to the proven release")

    proof = {
        "schema_version": 1,
        "status": "pass",
        "suite": "staging-cell",
        "acceptance": "backup-delete-to-prove-v1",
        "commit": checkout_commit,
        "source_commit": checkout_commit,
        "controller_commit": controller,
        "release_commit": release,
        "tool_lock_sha256": identity["tool_lock_sha256"],
        "production_changed": False,
        "receipts": {
            label: {
                "sha256": hashes[label],
                "status": STAGING_RECEIPT_CONTRACT[label][1],
            }
            for label in STAGING_RECEIPT_CONTRACT
        },
        "does_not_establish": [
            "public DNS",
            "public TLS",
            "production Kubernetes cutover",
        ],
    }
    _validate_staging_cell_proof(identity, proof)
    _atomic_write(output, _canonical_json(proof))
    return proof


def _finding_reference(
    finding_id: str, finding_sha256: str, checkpoint: str
) -> dict[str, str]:
    checkpoint = _canonical_sha(checkpoint, label="observer checkpoint")
    if (
        not isinstance(finding_id, str)
        or re.fullmatch(r"ga-[A-Za-z0-9TZ-]{8,96}", finding_id) is None
    ):
        raise IdentityError("observer finding_id is malformed")
    if (
        not isinstance(finding_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", finding_sha256) is None
    ):
        raise IdentityError("observer finding_sha256 is malformed")
    return {
        "kind": "grosser-adler-finding-reference",
        "finding_id": finding_id,
        "finding_sha256": finding_sha256,
        "checkpoint": checkpoint,
    }


def staging_attestation(
    record_path: Path,
    proof_path: Path,
    *,
    finding_id: str,
    finding_sha256: str,
    checkpoint: str,
) -> dict[str, Any]:
    record_bytes = record_path.read_bytes()
    record = _read_object(record_path)
    proof_bytes = proof_path.read_bytes()
    proof = _read_object(proof_path)
    implementation_commit = _canonical_sha(
        record.get("proof_commit"), label="staging record proof commit"
    )
    if proof.get("commit") != implementation_commit:
        raise IdentityError("staging record and proof commit differ")
    observer = _finding_reference(finding_id, finding_sha256, checkpoint)
    if observer["checkpoint"] != implementation_commit:
        raise IdentityError("observer finding is bound to a different checkpoint")
    if record.get("suite") != "staging-cell" or proof.get("suite") != "staging-cell":
        raise IdentityError("staging attestation requires staging-cell evidence")
    if record.get("proof_receipt_sha256") != _sha256_bytes(proof_bytes):
        raise IdentityError("staging attestation proof hash differs from record")
    return {
        "schema_version": 1,
        "status": "pass",
        "suite": "staging-cell",
        "implementation_commit": implementation_commit,
        "proof_record_sha256": _sha256_bytes(record_bytes),
        "proof_receipt_sha256": _sha256_bytes(proof_bytes),
        "observer": observer,
        "observer_authenticity": "live-verification-required",
        "observer_hash_semantics": "integrity-not-authenticity",
        "production_changed": False,
    }


def write_staging_attestation(
    record_path: Path,
    proof_path: Path,
    output: Path,
    *,
    finding_id: str,
    finding_sha256: str,
    checkpoint: str,
) -> dict[str, Any]:
    attestation = staging_attestation(
        record_path,
        proof_path,
        finding_id=finding_id,
        finding_sha256=finding_sha256,
        checkpoint=checkpoint,
    )
    _atomic_write(output, _canonical_json(attestation))
    return attestation


def _git_lines(*arguments: str) -> list[str]:
    result = subprocess.run(
        ["git", *arguments],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def validate_evidence_commit(
    identity_path: Path,
    record_path: Path,
    proof_path: Path,
    attestation_path: Path,
) -> dict[str, Any]:
    reusable = validate(identity_path, record_path, proof_path)
    identity = _read_object(identity_path)
    record_bytes = record_path.read_bytes()
    proof_bytes = proof_path.read_bytes()
    proof = _read_object(proof_path)
    attestation = _read_object(attestation_path)

    implementation_commit = _canonical_sha(
        reusable.get("proof_commit"), label="evidence implementation commit"
    )
    if identity.get("source_commit") != implementation_commit:
        raise IdentityError("staging identity is not bound to the implementation commit")
    if proof.get("commit") != implementation_commit:
        raise IdentityError("staging proof is not bound to the implementation commit")

    expected_attestation = {
        "schema_version": 1,
        "status": "pass",
        "suite": "staging-cell",
        "implementation_commit": implementation_commit,
        "proof_record_sha256": _sha256_bytes(record_bytes),
        "proof_receipt_sha256": _sha256_bytes(proof_bytes),
        "observer_authenticity": "live-verification-required",
        "observer_hash_semantics": "integrity-not-authenticity",
        "production_changed": False,
    }
    for key, expected in expected_attestation.items():
        if attestation.get(key) != expected:
            raise IdentityError(f"staging attestation field drifted: {key}")
    observer = attestation.get("observer")
    if not isinstance(observer, dict):
        raise IdentityError("staging attestation has no observer reference")
    normalized_observer = _finding_reference(
        observer.get("finding_id"),
        observer.get("finding_sha256"),
        observer.get("checkpoint"),
    )
    if observer != normalized_observer or observer["checkpoint"] != implementation_commit:
        raise IdentityError("staging observer reference is not exact")

    current_commit = _checkout_commit()
    if current_commit == implementation_commit:
        raise IdentityError("evidence commit must follow the implementation commit")
    parents = _git_lines("rev-list", "--parents", "-n", "1", current_commit)
    if len(parents) != 1:
        raise IdentityError("cannot resolve evidence commit parent")
    parent_fields = parents[0].split()
    if len(parent_fields) != 2 or parent_fields[1] != implementation_commit:
        raise IdentityError("evidence commit must be the direct child of implementation commit")

    changed = _git_lines(
        "diff", "--name-only", "--no-renames", implementation_commit, current_commit
    )
    allowed = {
        (STAGING_EVIDENCE_ROOT / name).as_posix()
        for name in STAGING_EVIDENCE_FILES
    }
    if set(changed) != allowed:
        raise IdentityError(
            "evidence commit changes files outside the exact staging evidence set"
        )

    for selector in SUITE_INPUTS["staging-cell"]:
        result = subprocess.run(
            [
                "git",
                "diff",
                "--quiet",
                implementation_commit,
                current_commit,
                "--",
                selector,
            ],
            cwd=ROOT,
            check=False,
        )
        if result.returncode != 0:
            raise IdentityError(
                f"staging proof input changed in evidence-only commit: {selector}"
            )

    return {
        "schema_version": 1,
        "status": "pass",
        "suite": "staging-cell",
        "implementation_commit": implementation_commit,
        "evidence_commit": current_commit,
        "changed_evidence_files": sorted(changed),
        "observer": normalized_observer,
        "observer_live_verification_required": True,
        "production_changed": False,
    }


def record(identity_path: Path, proof_receipt: Path, output_dir: Path) -> dict[str, Any]:
    identity = _read_object(identity_path)
    expected = compute_identity(identity.get("suite", ""), identity.get("source_commit", ""))
    if identity != expected:
        raise IdentityError("proof identity no longer matches the checked-out inputs")
    proof = _read_object(proof_receipt)
    if proof.get("status") != "pass":
        raise IdentityError("only a passing proof receipt may be reused")
    if proof.get("tool_lock_sha256") != identity["tool_lock_sha256"]:
        raise IdentityError("proof receipt tool lock does not match the proof identity")
    checkout_commit = _checkout_commit()
    if proof.get("commit") != checkout_commit:
        raise IdentityError("proof receipt is not bound to the current checkout commit")
    if proof.get("production_changed") is not False:
        raise IdentityError("reusable proof must explicitly assert production_changed=false")
    _validate_suite_proof(identity, proof)
    output_dir.mkdir(parents=True, exist_ok=True)
    proof_bytes = _canonical_json(proof)
    proof_path = output_dir / "proof.json"
    _atomic_write(proof_path, proof_bytes)
    reusable = {
        "schema_version": 2,
        "status": "pass",
        "suite": identity["suite"],
        "identity_sha256": identity["identity_sha256"],
        "proof_input_manifest_sha256": identity["input_manifest_sha256"],
        "proof_receipt_sha256": _sha256_bytes(proof_bytes),
        "proof_commit": proof.get("commit"),
        "proof_source_commit": proof.get("source_commit"),
        "production_changed": proof.get("production_changed"),
    }
    _atomic_write(output_dir / "record.json", _canonical_json(reusable))
    return reusable


def validate(identity_path: Path, record_path: Path, proof_path: Path) -> dict[str, Any]:
    identity = _read_object(identity_path)
    expected = compute_identity(identity.get("suite", ""), identity.get("source_commit", ""))
    if identity != expected:
        raise IdentityError("current proof inputs do not match the requested identity")
    reusable = _read_object(record_path)
    if reusable.get("schema_version") != 2 or reusable.get("status") != "pass":
        raise IdentityError("reusable proof record is not a passing v2 record")
    if reusable.get("suite") != identity.get("suite"):
        raise IdentityError("reusable proof record suite differs from current inputs")
    if reusable.get("identity_sha256") != identity.get("identity_sha256"):
        raise IdentityError("reusable proof record is bound to different proof inputs")
    if reusable.get("proof_input_manifest_sha256") != identity.get("input_manifest_sha256"):
        raise IdentityError("reusable proof input manifest differs from current inputs")
    proof_bytes = proof_path.read_bytes()
    if _sha256_bytes(proof_bytes) != reusable.get("proof_receipt_sha256"):
        raise IdentityError("reusable proof receipt digest mismatch")
    proof = _read_object(proof_path)
    if proof.get("status") != "pass":
        raise IdentityError("reusable proof payload is not passing")
    if proof.get("tool_lock_sha256") != identity["tool_lock_sha256"]:
        raise IdentityError("reusable proof payload uses a different tool lock")
    if proof.get("production_changed") is not False:
        raise IdentityError("reusable proof payload must assert production_changed=false")
    if reusable.get("production_changed") is not False:
        raise IdentityError("reusable proof record must assert production_changed=false")
    if reusable.get("proof_commit") != proof.get("commit"):
        raise IdentityError("reusable proof record commit does not match the proof payload")
    if reusable.get("proof_source_commit") != proof.get("source_commit"):
        raise IdentityError("reusable proof record source commit does not match the proof payload")
    _validate_suite_proof(identity, proof)
    return reusable


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    compute = sub.add_parser("compute")
    compute.add_argument("--suite", choices=tuple(SUITE_INPUTS), required=True)
    compute.add_argument("--source-commit", required=True)
    compute.add_argument("--output", type=Path, required=True)
    compute.add_argument("--github-output", type=Path)
    summarize = sub.add_parser("summarize-staging")
    summarize.add_argument("--identity", type=Path, required=True)
    summarize.add_argument("--state-root", type=Path, required=True)
    summarize.add_argument("--output", type=Path, required=True)
    create = sub.add_parser("record")
    create.add_argument("--identity", type=Path, required=True)
    create.add_argument("--proof-receipt", type=Path, required=True)
    create.add_argument("--output-dir", type=Path, required=True)
    attest = sub.add_parser("attest-staging")
    attest.add_argument("--record", type=Path, required=True)
    attest.add_argument("--proof", type=Path, required=True)
    attest.add_argument("--finding-id", required=True)
    attest.add_argument("--finding-sha256", required=True)
    attest.add_argument("--checkpoint", required=True)
    attest.add_argument("--output", type=Path, required=True)
    evidence = sub.add_parser("validate-evidence-commit")
    evidence.add_argument("--identity", type=Path, required=True)
    evidence.add_argument("--record", type=Path, required=True)
    evidence.add_argument("--proof", type=Path, required=True)
    evidence.add_argument("--attestation", type=Path, required=True)
    check = sub.add_parser("validate")
    check.add_argument("--identity", type=Path, required=True)
    check.add_argument("--record", type=Path, required=True)
    check.add_argument("--proof", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "compute":
            payload = compute_identity(args.suite, args.source_commit)
            _atomic_write(args.output, _canonical_json(payload))
            if args.github_output:
                with args.github_output.open("a", encoding="utf-8") as handle:
                    handle.write(f"identity={payload['identity_sha256']}\n")
            print(json.dumps(payload, sort_keys=True))
        elif args.command == "summarize-staging":
            print(
                json.dumps(
                    summarize_staging_proof(args.identity, args.state_root, args.output),
                    sort_keys=True,
                )
            )
        elif args.command == "record":
            print(json.dumps(record(args.identity, args.proof_receipt, args.output_dir), sort_keys=True))
        elif args.command == "attest-staging":
            print(
                json.dumps(
                    write_staging_attestation(
                        args.record,
                        args.proof,
                        args.output,
                        finding_id=args.finding_id,
                        finding_sha256=args.finding_sha256,
                        checkpoint=args.checkpoint,
                    ),
                    sort_keys=True,
                )
            )
        elif args.command == "validate-evidence-commit":
            print(
                json.dumps(
                    validate_evidence_commit(
                        args.identity,
                        args.record,
                        args.proof,
                        args.attestation,
                    ),
                    sort_keys=True,
                )
            )
        else:
            print(json.dumps(validate(args.identity, args.record, args.proof), sort_keys=True))
    except (IdentityError, OSError, subprocess.CalledProcessError) as error:
        print(f"proof identity failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
