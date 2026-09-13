#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable

import yaml

ROOT = Path(__file__).resolve().parents[2]
PLATFORM = ROOT / "platform"
PROMOTION_SENTINEL = "promotion-required"
HEX64 = re.compile(r"^[0-9a-f]{64}$")
OVERLAYS = ("local", "ha", "ci", "staging", "production")
NONLOCAL_OVERLAY_TARGETS = frozenset(
    f"platform/apps/weltgewebe/overlays/{name}"
    for name in ("ci", "staging", "production")
)
LOCAL_FIXTURE_SENTINELS = (
    "weltgewebe-local-fixture",
    "local-test-only-weltgewebe",
)
HA_TARGETS = (
    "platform/apps/weltgewebe/overlays/ha",
    "platform/apps/weltgewebe/migration/ha",
    "platform/infrastructure/ha-data",
)
LOCAL_FIXTURE_ROOTS = (
    PLATFORM / "apps/weltgewebe/migration/local",
    PLATFORM / "apps/weltgewebe/overlays/local",
    PLATFORM / "infrastructure/local-data",
    PLATFORM / "clusters/local",
)


class ContractError(RuntimeError):
    pass


def _documents(path: Path) -> Iterable[dict[str, Any]]:
    for document in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(document, dict):
            yield document


def _all_yaml() -> Iterable[tuple[Path, dict[str, Any]]]:
    for path in sorted(PLATFORM.rglob("*.yaml")):
        for document in _documents(path):
            yield path, document


def _container_specs(deployment: dict[str, Any]) -> list[dict[str, Any]]:
    template = deployment.get("spec", {}).get("template", {})
    pod_spec = template.get("spec", {}) if isinstance(template, dict) else {}
    containers = pod_spec.get("containers", []) if isinstance(pod_spec, dict) else []
    return containers if isinstance(containers, list) else []


def _assert_first_party_deployments() -> None:
    base = PLATFORM / "apps/weltgewebe/base"
    deployments = [
        document
        for path in base.glob("*.yaml")
        for document in _documents(path)
        if document.get("kind") == "Deployment"
    ]
    names = {item["metadata"]["name"] for item in deployments}
    if names != {"weltgewebe-api", "weltgewebe-web"}:
        raise ContractError(f"unexpected first-party deployments: {sorted(names)}")
    for deployment in deployments:
        pod = deployment["spec"]["template"]["spec"]
        pod_security = pod.get("securityContext", {})
        if pod.get("automountServiceAccountToken") is not False:
            raise ContractError(f"{deployment['metadata']['name']} mounts service account token")
        if pod_security.get("runAsNonRoot") is not True:
            raise ContractError(f"{deployment['metadata']['name']} lacks runAsNonRoot")
        if pod_security.get("seccompProfile", {}).get("type") != "RuntimeDefault":
            raise ContractError(f"{deployment['metadata']['name']} lacks RuntimeDefault seccomp")
        if deployment["spec"].get("strategy", {}).get("rollingUpdate", {}).get("maxUnavailable") != 0:
            raise ContractError(f"{deployment['metadata']['name']} permits unavailable rollout pods")
        for container in _container_specs(deployment):
            missing = [key for key in ("startupProbe", "readinessProbe", "livenessProbe", "resources") if key not in container]
            if missing:
                raise ContractError(f"{deployment['metadata']['name']} missing {missing}")
            security = container.get("securityContext", {})
            expected = {
                "allowPrivilegeEscalation": False,
                "privileged": False,
                "readOnlyRootFilesystem": True,
            }
            for key, value in expected.items():
                if security.get(key) is not value:
                    raise ContractError(f"{deployment['metadata']['name']} invalid {key}")
            if security.get("capabilities", {}).get("drop") != ["ALL"]:
                raise ContractError(f"{deployment['metadata']['name']} does not drop all capabilities")


def _assert_no_secrets() -> None:
    for path, document in _all_yaml():
        if document.get("kind") == "Secret":
            raise ContractError(f"Secret object committed at {path.relative_to(ROOT)}")
    contract = json.loads((PLATFORM / "apps/weltgewebe/secret-contract.json").read_text())
    if contract.get("required_keys") != ["database-url"]:
        raise ContractError("secret contract changed without matching validator update")


def _assert_images() -> None:
    lock = json.loads((PLATFORM / "toolchain.lock.json").read_text())
    for section in (lock["tools"], lock["artifacts"]):
        for name, entry in section.items():
            if not HEX64.fullmatch(entry["sha256"]):
                raise ContractError(f"{name} lacks sha256 pin")
    node = lock["kubernetes"]["kind_node_image"]
    if "@sha256:" not in node or not HEX64.fullmatch(node.rsplit("@sha256:", 1)[1]):
        raise ContractError("kind node image is not digest-bound")
    expected_images = {
        "cloudnative_pg_operator",
        "cloudnative_pg_postgresql",
        "cert_manager_controller",
        "cert_manager_cainjector",
        "cert_manager_webhook",
        "barman_cloud_plugin",
        "barman_cloud_sidecar",
        "nats",
        "nats_box",
        "seaweedfs",
    }
    images = lock.get("images", {})
    if set(images) != expected_images:
        raise ContractError(f"unexpected HA image lock: {sorted(images)}")
    for name, image in images.items():
        if "@sha256:" not in image or not HEX64.fullmatch(image.rsplit("@sha256:", 1)[1]):
            raise ContractError(f"{name} is not digest-bound")
    promotion = json.loads((PLATFORM / "image-promotion.contract.json").read_text())
    if promotion.get("status") != "blocked" or promotion.get("production_activation") is not False:
        raise ContractError("image promotion contract must remain blocked in T003")
    for overlay in ("staging", "production"):
        data = yaml.safe_load(
            (PLATFORM / f"apps/weltgewebe/overlays/{overlay}/kustomization.yaml").read_text()
        )
        for image in data.get("images", []):
            if image.get("newTag") != PROMOTION_SENTINEL:
                raise ContractError(f"{overlay} bypasses the image promotion gate: {image}")
    for path, document in _all_yaml():
        if document.get("kind") != "Deployment":
            continue
        for container in _container_specs(document):
            image = str(container.get("image", ""))
            if image.endswith(":latest") or ":latest@" in image:
                raise ContractError(f"unbounded latest image in {path.relative_to(ROOT)}")


def _assert_oci_proof_mirror() -> None:
    command = [
        sys.executable,
        str(ROOT / "scripts/platform/oci_proof_mirror.py"),
        "validate",
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        result = json.loads(completed.stdout)
    except (
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
    ) as error:
        raise ContractError(f"OCI proof mirror contract failed: {error}") from error
    expected = {
        "status": "pass",
        "owner": "heimgewebe/weltgewebe",
        "source_kind": "private-ghcr-digest-mirror",
        "mirror_repository": "ghcr.io/heimgewebe/weltgewebe-proof-oci",
        "visibility": "private",
        "repository_binding": "heimgewebe/commonthing",
        "image_count": 25,
    }
    observed = {key: result.get(key) for key in expected}
    if observed != expected:
        raise ContractError(f"OCI proof mirror contract mismatch: {observed}")
    retention = result.get("retention", {})
    if retention.get("unbounded_growth_prevented") is not True:
        raise ContractError("OCI proof mirror version growth is not bounded")
    if retention.get("orphan_grace_days") != 14:
        raise ContractError("OCI proof mirror orphan grace must remain 14 days")

def _assert_two_operator_pilot_contract() -> None:
    command = [
        sys.executable,
        str(ROOT / "scripts/platform/validate_two_operator_pilot.py"),
        "--mode",
        "example",
        str(PLATFORM / "cell-pilot/two-operator-pilot.example.invalid.json"),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        result = json.loads(completed.stdout)
    except (
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
    ) as error:
        raise ContractError(f"two-operator cell pilot contract failed: {error}") from error
    expected = {
        "status": "pass",
        "contract_id": "gewebezelle-two-operator-pilot-v1",
        "document_mode": "example",
        "activatable": False,
    }
    observed = {key: result.get(key) for key in expected}
    if observed != expected:
        raise ContractError(f"two-operator cell pilot contract mismatch: {observed}")


def _assert_local_fixture_scope() -> None:
    """Keep public disposable fixture credentials and names local."""
    for path in sorted(PLATFORM.rglob("*")):
        if not path.is_file() or path.suffix not in {".json", ".yaml", ".yml"}:
            continue
        source = path.read_text(encoding="utf-8")
        observed = [item for item in LOCAL_FIXTURE_SENTINELS if item in source]
        if not observed:
            continue
        if any(path.is_relative_to(root) for root in LOCAL_FIXTURE_ROOTS):
            continue
        raise ContractError(
            f"local-only fixture marker(s) {observed} escaped into "
            f"{path.relative_to(ROOT)}"
        )


def _assert_nonlocal_overlay_fixture_boundary(target: str, rendered: str) -> None:
    if target not in NONLOCAL_OVERLAY_TARGETS:
        return
    observed = [item for item in LOCAL_FIXTURE_SENTINELS if item in rendered]
    if observed:
        raise ContractError(
            f"{target} renders local-only fixture marker(s): {observed}"
        )


def _assert_migration_job() -> None:
    job = next(
        _documents(PLATFORM / "apps/weltgewebe/migration/local/job.yaml")
    )
    if job.get("apiVersion") != "batch/v1" or job.get("kind") != "Job":
        raise ContractError("local migration workload must be a batch/v1 Job")
    spec = job.get("spec", {})
    if spec.get("backoffLimit") != 0 or spec.get("activeDeadlineSeconds") != 480:
        raise ContractError("local migration Job must fail once and have a bounded deadline")
    pod = spec.get("template", {}).get("spec", {})
    if pod.get("restartPolicy") != "Never" or pod.get("automountServiceAccountToken") is not False:
        raise ContractError("local migration Job has an unsafe pod lifecycle")
    if pod.get("securityContext", {}).get("runAsNonRoot") is not True:
        raise ContractError("local migration Job must run as non-root")
    containers = pod.get("containers", [])
    if len(containers) != 1:
        raise ContractError("local migration Job must contain exactly one container")
    container = containers[0]
    security = container.get("securityContext", {})
    if security.get("readOnlyRootFilesystem") is not True:
        raise ContractError("local migration Job requires a read-only root filesystem")
    if security.get("capabilities", {}).get("drop") != ["ALL"]:
        raise ContractError("local migration Job must drop all capabilities")
    environment = {item.get("name"): item for item in container.get("env", [])}
    if environment.get("WELTGEWEBE_API_MIGRATION_ONLY", {}).get("value") != "1":
        raise ContractError("local migration Job does not enable migration-only mode")
    if environment.get("WELTGEWEBE_API_STARTUP_MIGRATIONS", {}).get("value") != "run":
        raise ContractError("local migration Job does not run migrations")
    database = environment.get("DATABASE_URL", {}).get("valueFrom", {})
    if database.get("configMapKeyRef") != {
        "name": "weltgewebe-local-fixture",
        "key": "database-url",
    }:
        raise ContractError("local migration Job must use the public ConfigMap fixture")

def _assert_flux_chain() -> None:
    cluster = PLATFORM / "clusters/local"
    source = next(_documents(cluster / "source.yaml"))
    if source.get("kind") != "GitRepository" or source["spec"].get("url") != "https://github.com/heimgewebe/commonthing":
        raise ContractError("local Flux source is not the canonical repository")
    expected = {
        "local-data.yaml": [],
        "migration.yaml": ["weltgewebe-local-data"],
        "app.yaml": ["weltgewebe-migration"],
        "gateway.yaml": ["weltgewebe-app"],
    }
    for filename, dependencies in expected.items():
        document = next(_documents(cluster / filename))
        spec = document["spec"]
        if spec.get("prune") is not True or spec.get("wait") is not True:
            raise ContractError(f"{filename} must prune and wait")
        observed = [item["name"] for item in spec.get("dependsOn", [])]
        if observed != dependencies:
            raise ContractError(f"{filename} dependencies {observed} != {dependencies}")


def _assert_ha_contract() -> None:
    lock = json.loads((PLATFORM / "toolchain.lock.json").read_text())
    postgres = next(_documents(PLATFORM / "infrastructure/ha-data/postgres.yaml"))
    if postgres.get("kind") != "Cluster" or postgres.get("apiVersion") != "postgresql.cnpg.io/v1":
        raise ContractError("HA PostgreSQL must use CloudNativePG Cluster v1")
    spec = postgres.get("spec", {})
    if spec.get("instances") != 3:
        raise ContractError("HA PostgreSQL requires exactly three instances")
    catalog_ref = spec.get("imageCatalogRef", {})
    expected_catalog_ref = {
        "apiGroup": "postgresql.cnpg.io",
        "kind": "ImageCatalog",
        "name": "weltgewebe-postgres",
        "major": 16,
    }
    if catalog_ref != expected_catalog_ref or "imageName" in spec:
        raise ContractError("HA PostgreSQL must resolve its major version through the pinned ImageCatalog")
    catalog = next(_documents(PLATFORM / "infrastructure/ha-data/postgres-image-catalog.yaml"))
    if catalog.get("metadata", {}).get("namespace") != "weltgewebe-data":
        raise ContractError("HA PostgreSQL ImageCatalog must be directly applicable in weltgewebe-data")
    images = catalog.get("spec", {}).get("images", [])
    if images != [{"major": 16, "image": lock["images"]["cloudnative_pg_postgresql"]}]:
        raise ContractError("HA PostgreSQL ImageCatalog differs from the digest lock")
    affinity = spec.get("affinity", {})
    if affinity.get("podAntiAffinityType") != "required" or affinity.get("topologyKey") != "topology.kubernetes.io/zone":
        raise ContractError("HA PostgreSQL does not require zone anti-affinity")
    expected_plugin = {
        "name": "barman-cloud.cloudnative-pg.io",
        "isWALArchiver": True,
        "parameters": {"barmanObjectName": "weltgewebe-ha-backup"},
    }
    if spec.get("plugins") != [expected_plugin] or "backup" in spec:
        raise ContractError("HA PostgreSQL must use only the Barman Cloud plugin for WAL archiving")
    backup_store = next(
        _documents(PLATFORM / "infrastructure/ha-data/barman-object-store.yaml")
    )
    if backup_store.get("kind") != "ObjectStore" or backup_store.get("apiVersion") != "barmancloud.cnpg.io/v1":
        raise ContractError("HA PostgreSQL backup must use a Barman Cloud ObjectStore")
    if backup_store.get("metadata", {}).get("namespace") != "weltgewebe-data":
        raise ContractError("HA Barman Cloud ObjectStore must be directly applicable in weltgewebe-data")
    backup_spec = backup_store.get("spec", {})
    configuration = backup_spec.get("configuration", {})
    if not str(configuration.get("destinationPath", "")).startswith("s3://"):
        raise ContractError("HA Barman Cloud ObjectStore lacks an S3 destination")
    if backup_spec.get("retentionPolicy") != "7d":
        raise ContractError("HA Barman Cloud ObjectStore must retain a seven-day recovery window")

    nats_docs = list(_documents(PLATFORM / "infrastructure/ha-data/nats.yaml"))
    stateful = next((item for item in nats_docs if item.get("kind") == "StatefulSet"), None)
    budget = next((item for item in nats_docs if item.get("kind") == "PodDisruptionBudget"), None)
    if stateful is None or stateful.get("spec", {}).get("replicas") != 3:
        raise ContractError("HA NATS requires a three-replica StatefulSet")
    container = stateful["spec"]["template"]["spec"]["containers"][0]
    if container.get("image") != lock["images"]["nats"]:
        raise ContractError("HA NATS image differs from the digest lock")
    if budget is None or budget.get("spec", {}).get("minAvailable") != 2:
        raise ContractError("HA NATS requires a quorum-preserving disruption budget")
    config = next(item for item in nats_docs if item.get("kind") == "ConfigMap")["data"]["nats.conf"]
    for marker in ("jetstream {", "routes:", "domain: weltgewebe-ha"):
        if marker not in config:
            raise ContractError(f"HA NATS config lacks {marker}")

    app_base = PLATFORM / "apps/weltgewebe/base"
    app_budgets = {
        document["metadata"]["name"]: document
        for filename in ("api-pdb.yaml", "web-pdb.yaml")
        for document in _documents(app_base / filename)
    }
    for name in ("weltgewebe-api", "weltgewebe-web"):
        budget = app_budgets.get(name)
        if budget is None or budget.get("kind") != "PodDisruptionBudget":
            raise ContractError(f"HA application lacks disruption budget {name}")
        if budget.get("spec", {}).get("minAvailable") != 1:
            raise ContractError(f"HA application disruption budget {name} differs from the contract")
        labels = budget.get("spec", {}).get("selector", {}).get("matchLabels", {})
        if labels.get("app.kubernetes.io/name") != name:
            raise ContractError(f"HA application disruption budget {name} selects the wrong pods")

    patch_docs = list(_documents(PLATFORM / "apps/weltgewebe/overlays/ha/deployment-patch.yaml"))
    api = next(item for item in patch_docs if item["metadata"]["name"] == "weltgewebe-api")
    api_spec = api.get("spec", {})
    if api_spec.get("replicas") != 3:
        raise ContractError("HA API requires three replicas")
    rolling = api_spec.get("strategy", {}).get("rollingUpdate", {})
    if (
        api_spec.get("strategy", {}).get("type") != "RollingUpdate"
        or rolling.get("maxSurge") != 1
        or rolling.get("maxUnavailable") != 0
    ):
        raise ContractError(
            "HA API rollout must admit one surge replica before terminating an available pod"
        )
    pod = api_spec["template"]["spec"]
    spread = pod.get("topologySpreadConstraints", [])
    if (
        not spread
        or spread[0].get("maxSkew") != 1
        or spread[0].get("topologyKey") != "topology.kubernetes.io/zone"
        or spread[0].get("whenUnsatisfiable") != "DoNotSchedule"
    ):
        raise ContractError("HA API does not require bounded zone spread")
    preferred = (
        pod.get("affinity", {})
        .get("podAntiAffinity", {})
        .get("preferredDuringSchedulingIgnoredDuringExecution", [])
    )
    if (
        not preferred
        or preferred[0].get("weight") != 100
        or preferred[0].get("podAffinityTerm", {}).get("topologyKey")
        != "topology.kubernetes.io/zone"
    ):
        raise ContractError("HA API does not prefer zone anti-affinity during surge rollouts")

    object_store = next(_documents(PLATFORM / "infrastructure/ha-data/object-store.yaml"))
    if object_store.get("kind") != "Service" or object_store.get("spec", {}).get("selector"):
        raise ContractError("HA proof object store must be an external selectorless Service")

    primary_kind = yaml.safe_load((PLATFORM / "clusters/ha/kind.yaml").read_text())
    restore_kind = yaml.safe_load((PLATFORM / "clusters/ha/restore-kind.yaml").read_text())
    for name, document in (("primary", primary_kind), ("restore", restore_kind)):
        zones = []
        for node in document.get("nodes", []):
            for patch in node.get("kubeadmConfigPatches", []):
                zones.extend(re.findall(r"topology\.kubernetes\.io/zone=(zone-[abc])", patch))
        if sorted(zones) != ["zone-a", "zone-b", "zone-c"]:
            raise ContractError(f"{name} HA kind cluster lacks three explicit zones")

    proof = "\n".join(
        (ROOT / relative).read_text()
        for relative in (
            "scripts/platform/ha_reference.py",
            "scripts/platform/ha_availability.py",
            "scripts/platform/ha_dependencies.py",
            "scripts/platform/ha_migration.py",
            "scripts/platform/ha_wal.py",
        )
    )
    required_markers = (
        "docker", "stop", "postgres_rto_seconds", "nats_rto_seconds",
        "recoveryTarget", "blank_kind_cluster", "production_changed",
        "restore-kind.yaml", "PITR data comparison failed",
        "install_cert_manager", "install_barman_cloud_plugin",
        "render_cnpg_manifest", "verify_cnpg_operator_ha",
        "verify_barman_plugin_ha", "align_postgres_primary_with_barman_leader",
        "prove_barman_plugin_backup", "kubectl_cnpg",
        "wait_barman_plugin_leader_after_node_loss", "barman_leader_rto_seconds",
        "barman_plugin_rto_seconds",
        "verify_barman_sidecar_images",
        "configure_cluster_dns_ha", "cluster_dns",
        "BARMAN_CLOUD_SIDECAR_IMAGE",
        "pg_stat_archiver", "pluginConfiguration",
        "prove_api_upgrade_and_rollback", "UPGRADE_API_IMAGE",
        "rollout", "undo", "compute_error_budget",
        "zero-observed-outage", "within_budget",
        "non-owner-control-plane-any-advertised-address", "degraded_gateway_path_samples",
        "continued_wal_archiving", "continuity_validation_seconds",
        "measured_archive_rpo_upper_bound_seconds",
    )
    for marker in required_markers:
        if marker not in proof:
            raise ContractError(f"HA proof runner lacks {marker}")
    if "kind: Secret" in "\n".join(path.read_text() for path in (PLATFORM / "infrastructure/ha-data").glob("*.yaml")):
        raise ContractError("HA manifests commit a Secret")

    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/kubernetes-platform-proof.yml").read_text()
    )
    steps = workflow["jobs"]["kind-ha-recovery-proof"]["steps"]
    cleanup = next(
        (item for item in steps if item.get("name") == "Reconcile owned HA proof resources"),
        None,
    )
    cleanup_condition = cleanup.get("if") if cleanup else None
    if cleanup_condition not in {
        "always()",
        "always() && steps.proof-cache.outputs.cache-hit != 'true'",
    }:
        raise ContractError(
            "HA workflow lacks owned-resource reconciliation after every resource-producing path"
        )
    cleanup_command = cleanup.get("run", "")
    for marker in (
        "ha_reference.py down",
        '--cluster "$CLUSTER_NAME"',
        '--commit "$(git rev-parse HEAD)"',
        '--owner-id "$PROOF_OWNER_ID"',
        "--receipt build/kubernetes-platform/ha-recovery-cleanup.json",
    ):
        if marker not in cleanup_command:
            raise ContractError(f"HA workflow cleanup lacks {marker}")

    kind_steps = workflow["jobs"]["kind-gitops-proof"]["steps"]
    kind_cleanup = next(
        (item for item in kind_steps if item.get("name") == "Reconcile owned kind proof resources"),
        None,
    )
    kind_cleanup_condition = kind_cleanup.get("if") if kind_cleanup else None
    if kind_cleanup_condition not in {
        "always()",
        "always() && steps.proof-cache.outputs.cache-hit != 'true'",
    }:
        raise ContractError(
            "kind workflow lacks owned-resource reconciliation after every resource-producing path"
        )
    kind_cleanup_command = kind_cleanup.get("run", "")
    for marker in (
        "kind_reference.py down",
        '--cluster "$CLUSTER_NAME"',
        '--commit "$(git rev-parse HEAD)"',
        '--owner-id "$PROOF_OWNER_ID"',
        "--receipt build/kubernetes-platform/kind-gitops-cleanup.json",
    ):
        if marker not in kind_cleanup_command:
            raise ContractError(f"kind workflow cleanup lacks {marker}")


def _assert_compose_parity() -> None:
    config = next(_documents(PLATFORM / "apps/weltgewebe/base/config-map.yaml"))["data"]
    required = {
        "WELTGEWEBE_DOMAIN_READ_SOURCE",
        "WELTGEWEBE_DOMAIN_ACCOUNT_WRITE_SOURCE",
        "WELTGEWEBE_DOMAIN_NODE_WRITE_SOURCE",
        "WELTGEWEBE_DOMAIN_EDGE_WRITE_SOURCE",
        "WELTGEWEBE_PASSKEY_CREDENTIAL_SOURCE",
        "WELTGEWEBE_API_STARTUP_MIGRATIONS",
        "NATS_URL",
        "API_BIND",
    }
    missing = sorted(required - set(config))
    if missing:
        raise ContractError(f"Kubernetes config misses Compose invariants: {missing}")
    compose = (ROOT / "infra/compose/compose.prod.yml").read_text(encoding="utf-8")
    for key in required:
        if key not in compose:
            raise ContractError(f"Compose parity source lacks {key}")


def _run(command: list[str], *, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=ROOT,
        input=input_text,
        text=True,
        check=True,
        capture_output=True,
    )


def _render_and_validate() -> dict[str, int]:
    receipt = json.loads(
        _run(
            [
                sys.executable,
                "scripts/platform/bootstrap_tools.py",
                "--json",
                "--tool",
                "kustomize",
                "--tool",
                "kubeconform",
                "--skip-artifacts",
            ]
        ).stdout
    )
    kustomize = receipt["tools"]["kustomize"]
    kubeconform = receipt["tools"]["kubeconform"]
    targets = [
        *(f"platform/apps/weltgewebe/overlays/{name}" for name in OVERLAYS),
        "platform/infrastructure/local-data",
        "platform/apps/weltgewebe/migration/local",
        *HA_TARGETS,
        "platform/infrastructure/gateway",
        "platform/clusters/local",
        "platform/clusters/staging/data",
        "platform/clusters/staging/gateway",
    ]
    counts: dict[str, int] = {}
    with tempfile.TemporaryDirectory() as tmp:
        temp = Path(tmp)
        for target in targets:
            rendered = _run([kustomize, "build", target]).stdout
            _assert_nonlocal_overlay_fixture_boundary(target, rendered)
            docs = [item for item in yaml.safe_load_all(rendered) if isinstance(item, dict)]
            if not docs:
                raise ContractError(f"empty Kustomize output for {target}")
            counts[target] = len(docs)
            output = temp / (target.replace("/", "_") + ".yaml")
            output.write_text(rendered, encoding="utf-8")
            _run(
                [
                    kubeconform,
                    "-strict",
                    "-summary",
                    "-ignore-missing-schemas",
                    "-kubernetes-version",
                    "1.36.1",
                    str(output),
                ]
            )
    return counts


def _assert_staging_cell_contract() -> None:
    cluster_path = PLATFORM / "clusters/staging/kind.yaml"
    cluster_docs = list(_documents(cluster_path))
    if len(cluster_docs) != 1:
        raise ContractError("staging kind config must contain exactly one Cluster")
    cluster = cluster_docs[0]
    if cluster.get("kind") != "Cluster" or cluster.get("name") != "commonthing-staging":
        raise ContractError("staging kind cluster identity is invalid")
    nodes = cluster.get("nodes")
    if not isinstance(nodes, list) or len(nodes) != 3:
        raise ContractError("staging kind cluster must have one control-plane and two workers")
    roles = [node.get("role") if isinstance(node, dict) else None for node in nodes]
    if roles != ["control-plane", "worker", "worker"]:
        raise ContractError("staging kind node-role contract drift")

    expected_host = "__COMMONTHING_STAGING_DATA_ROOT__"
    expected_container = "/var/local/commonthing-staging"
    for index, node in enumerate(nodes):
        mounts = node.get("extraMounts", []) if isinstance(node, dict) else []
        if index != 1:
            if mounts:
                raise ContractError(
                    f"staging non-data node {index} must not expose retained host storage"
                )
            continue
        if not isinstance(mounts, list) or len(mounts) != 1:
            raise ContractError("staging data worker must bind exactly one persistent host mount")
        mount = mounts[0]
        if mount.get("hostPath") != expected_host or mount.get("containerPath") != expected_container:
            raise ContractError("staging data-worker persistent host mount drift")
        if mount.get("readOnly") is not False:
            raise ContractError("staging data-worker persistent host mount must be writable")

    data_root = PLATFORM / "clusters/staging/data"
    documents = [
        document
        for path in sorted(data_root.glob("*.yaml"))
        for document in _documents(path)
    ]
    pvs = {
        d.get("metadata", {}).get("name"): d
        for d in documents
        if d.get("kind") == "PersistentVolume"
    }
    pvcs = {
        d.get("metadata", {}).get("name"): d
        for d in documents
        if d.get("kind") == "PersistentVolumeClaim"
    }
    expected_pvs = {
        "commonthing-staging-postgres": (
            "/var/local/commonthing-staging/postgres",
            "postgres-data",
        ),
        "commonthing-staging-nats": (
            "/var/local/commonthing-staging/nats",
            "nats-data",
        ),
    }
    if set(pvs) != set(expected_pvs):
        raise ContractError(f"unexpected staging persistent volumes: {sorted(pvs)}")
    if set(pvcs) != {claim for _, claim in expected_pvs.values()}:
        raise ContractError(f"unexpected staging persistent volume claims: {sorted(pvcs)}")
    expected_affinity = {
        "required": {
            "nodeSelectorTerms": [
                {
                    "matchExpressions": [
                        {
                            "key": "kubernetes.io/hostname",
                            "operator": "In",
                            "values": ["commonthing-staging-worker"],
                        }
                    ]
                }
            ]
        }
    }
    for name, (host_path, claim) in expected_pvs.items():
        spec = pvs[name].get("spec", {})
        if spec.get("persistentVolumeReclaimPolicy") != "Retain":
            raise ContractError(f"staging PV {name} must retain data across cluster deletion")
        if spec.get("storageClassName") != "":
            raise ContractError(f"staging PV {name} must use the explicit classless static binding")
        if spec.get("hostPath", {}).get("path") != host_path:
            raise ContractError(f"staging PV {name} hostPath drift")
        if spec.get("nodeAffinity") != expected_affinity:
            raise ContractError(f"staging PV {name} must be fenced to the dedicated data worker")
        claim_spec = pvcs[claim].get("spec", {})
        if claim_spec.get("storageClassName") != "":
            raise ContractError(f"staging PVC {claim} must disable dynamic provisioning")
        if claim_spec.get("volumeName") != name:
            raise ContractError(f"staging PVC {claim} must be pre-bound to {name}")

    deployments = {
        d.get("metadata", {}).get("name"): d
        for d in documents
        if d.get("kind") == "Deployment"
    }
    if set(deployments) != {"postgres", "nats"}:
        raise ContractError(f"unexpected staging data deployments: {sorted(deployments)}")
    for name, claim in (("postgres", "postgres-data"), ("nats", "nats-data")):
        deployment = deployments[name]
        deployment_spec = deployment.get("spec", {})
        if deployment_spec.get("strategy") != {"type": "Recreate"}:
            raise ContractError(
                f"staging {name} must use Recreate as an additional single-writer guard"
            )
        pod_spec = deployment_spec.get("template", {}).get("spec", {})
        if pod_spec.get("nodeSelector") != {
            "kubernetes.io/hostname": "commonthing-staging-worker"
        }:
            raise ContractError(f"staging {name} must be pinned to the dedicated data worker")
        if pod_spec.get("securityContext", {}).get("fsGroupChangePolicy") != "OnRootMismatch":
            raise ContractError(
                f"staging {name} must avoid repeated recursive fsGroup ownership changes"
            )
        volumes = pod_spec.get("volumes", [])
        data = next((item for item in volumes if item.get("name") == "data"), None)
        if not isinstance(data, dict) or data.get("persistentVolumeClaim", {}).get("claimName") != claim:
            raise ContractError(f"staging {name} data volume must use PVC {claim}")
        if "emptyDir" in data:
            raise ContractError(f"staging {name} data volume must not be ephemeral")
        containers = pod_spec.get("containers", [])
        if not containers or containers[0].get("securityContext", {}).get("readOnlyRootFilesystem") is not True:
            raise ContractError(f"staging {name} must keep the container root filesystem read-only")

    postgres = deployments["postgres"].get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])[0]
    env = {item.get("name"): item for item in postgres.get("env", []) if isinstance(item, dict)}
    for name, key in (("POSTGRES_USER", "username"), ("POSTGRES_PASSWORD", "password"), ("POSTGRES_DB", "database")):
        ref = env.get(name, {}).get("valueFrom", {}).get("secretKeyRef", {})
        if ref.get("name") != "commonthing-staging-database" or ref.get("key") != key:
            raise ContractError(f"staging postgres {name} must come from external Secret binding")
    startup = postgres.get("startupProbe", {})
    if int(startup.get("failureThreshold", 0)) * int(startup.get("periodSeconds", 0)) < 300:
        raise ContractError("staging postgres startup probe must allow at least 300 seconds")
    postgres_mounts = {
        item.get("mountPath")
        for item in postgres.get("volumeMounts", [])
        if isinstance(item, dict)
    }
    if not {"/var/lib/postgresql/data", "/var/run/postgresql", "/tmp"}.issubset(postgres_mounts):
        raise ContractError("staging postgres writable mount contract drift")

    policies = {
        d.get("metadata", {}).get("name"): d
        for d in documents
        if d.get("kind") == "NetworkPolicy"
    }
    for policy_name, (pod_name, port) in {
        "allow-app-postgres-access": ("postgres", 5432),
        "allow-app-nats-access": ("nats", 4222),
    }.items():
        policy = policies.get(policy_name)
        if not isinstance(policy, dict):
            raise ContractError(f"staging network policy {policy_name} is missing")
        spec = policy.get("spec", {})
        if spec.get("podSelector", {}).get("matchLabels") != {
            "app.kubernetes.io/name": pod_name
        }:
            raise ContractError(f"staging network policy {policy_name} target drift")
        ingress = spec.get("ingress")
        expected_ingress = [
            {
                "from": [
                    {
                        "namespaceSelector": {
                            "matchLabels": {
                                "kubernetes.io/metadata.name": "commonthing-staging"
                            }
                        },
                        "podSelector": {
                            "matchLabels": {"app.kubernetes.io/name": "commonthing-api"}
                        },
                    }
                ],
                "ports": [{"port": port, "protocol": "TCP"}],
            }
        ]
        if ingress != expected_ingress:
            raise ContractError(
                f"staging network policy {policy_name} must allow only staging API pods"
            )

    secret_contract = json.loads((PLATFORM / "apps/weltgewebe/secret-contract.json").read_text(encoding="utf-8"))
    if secret_contract.get("required_keys") != ["database-url"]:
        raise ContractError("staging controller assumes the canonical single required database-url secret key")

    _run([sys.executable, "scripts/platform/staging_cell.py", "self-check"])


def validate(render: bool) -> dict[str, Any]:
    required_paths = [
        PLATFORM / "apps/weltgewebe/base/kustomization.yaml",
        PLATFORM / "apps/weltgewebe/overlays/local/kustomization.yaml",
        PLATFORM / "apps/weltgewebe/overlays/ci/kustomization.yaml",
        PLATFORM / "apps/weltgewebe/overlays/staging/kustomization.yaml",
        PLATFORM / "apps/weltgewebe/overlays/production/kustomization.yaml",
        PLATFORM / "clusters/local/kind.yaml",
        PLATFORM / "clusters/local/kustomization.yaml",
        PLATFORM / "clusters/local/migration.yaml",
        PLATFORM / "clusters/staging/kind.yaml",
        PLATFORM / "clusters/staging/data/kustomization.yaml",
        PLATFORM / "apps/weltgewebe/migration/local/kustomization.yaml",
        PLATFORM / "apps/weltgewebe/migration/local/job.yaml",
        PLATFORM / "infrastructure/gateway/kustomization.yaml",
        PLATFORM / "infrastructure/local-data/kustomization.yaml",
        PLATFORM / "clusters/ha/kind.yaml",
        PLATFORM / "clusters/ha/restore-kind.yaml",
        PLATFORM / "apps/weltgewebe/overlays/ha/kustomization.yaml",
        PLATFORM / "apps/weltgewebe/migration/ha/kustomization.yaml",
        PLATFORM / "infrastructure/ha-data/kustomization.yaml",
        ROOT / "scripts/platform/ha_reference.py",
        ROOT / "scripts/platform/staging_cell.py",
        ROOT / "scripts/platform/oci_proof_mirror.py",
        PLATFORM / "oci-proof-mirror.lock.json",
        PLATFORM / "cell-profile.contract.json",
        PLATFORM / "cell-pilot/two-operator-pilot.contract.json",
        PLATFORM / "cell-pilot/two-operator-pilot.example.invalid.json",
        ROOT / "scripts/platform/validate_two_operator_pilot.py",
    ]
    missing = [str(path.relative_to(ROOT)) for path in required_paths if not path.is_file()]
    if missing:
        raise ContractError(f"missing platform paths: {missing}")
    _assert_no_secrets()
    _assert_first_party_deployments()
    _assert_images()
    _assert_oci_proof_mirror()
    _assert_two_operator_pilot_contract()
    _assert_local_fixture_scope()
    _assert_staging_cell_contract()
    _assert_migration_job()
    _assert_flux_chain()
    _assert_compose_parity()
    _assert_ha_contract()
    rendered = _render_and_validate() if render else {}
    return {"status": "pass", "rendered_documents": rendered}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--render", action="store_true")
    args = parser.parse_args()
    try:
        result = validate(args.render)
    except (ContractError, subprocess.CalledProcessError, yaml.YAMLError) as error:
        print(f"platform contract failed: {error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError):
            print(error.stdout, file=sys.stderr)
            print(error.stderr, file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
