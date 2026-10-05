from __future__ import annotations

import base64
import hashlib
import inspect
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

import yaml

import experiment_b as contract
import experiment_b_runtime as runtime


def vm_substrate_fixture() -> dict:
    return {
        "vm": runtime.VM_NAME,
        "uuid": "11111111-1111-4111-8111-111111111111",
        "hypervisor": "kvm",
        "vcpu": 6,
        "current_vcpu": 6,
        "memory_bytes": 12288 * 1024**2,
        "current_memory_bytes": 12288 * 1024**2,
        "interface_type": "network",
        "network": "default",
        "network_uuid": "22222222-2222-4222-8222-222222222222",
        "network_mode": "nat",
        "network_bridge": "virbr0",
        "mac": "52:54:00:12:34:56",
        "pool": runtime.POOL_NAME,
        "pool_uuid": "33333333-3333-4333-8333-333333333333",
        "pool_type": "dir",
        "pool_target": str(runtime.POOL_TARGET),
        "volume": runtime.VOLUME_NAME,
        "disk_path": str(runtime.POOL_TARGET / runtime.VOLUME_NAME),
        "disk_key": str(runtime.POOL_TARGET / runtime.VOLUME_NAME),
        "disk_format": "qcow2",
        "disk_target": "vda",
        "disk_bus": "virtio",
        "disk_capacity_bytes": 60 * 1024**3,
        "disk_device": 1,
        "disk_inode": 2,
        "base_volume": runtime.BASE_VOLUME,
        "base_path": str(runtime.POOL_TARGET / runtime.BASE_VOLUME),
        "base_key": str(runtime.POOL_TARGET / runtime.BASE_VOLUME),
        "base_format": "qcow2",
        "base_image_sha256": runtime.load_config()["vm"]["image"]["sha256"],
    }


def vm_receipt_fixture(
    substrate: dict | None = None, state_root: Path | None = None
) -> dict:
    return {
        "schema_version": 1,
        "status": "created",
        "source_commit": "a" * 40,
        "state_root": str((state_root or runtime.DEFAULT_STATE_ROOT).resolve()),
        "config_sha256": runtime.sha256_file(runtime.CLUSTER / "config.json"),
        "vm": runtime.VM_NAME,
        "pool": runtime.POOL_NAME,
        "volume": runtime.VOLUME_NAME,
        "network": "default",
        "substrate": vm_substrate_fixture() if substrate is None else substrate,
    }


class ExperimentBRuntimeContractTests(unittest.TestCase):
    def test_canonical_k6_image_is_digest_bound_from_workflow(self) -> None:
        commit = runtime.run(["git", "rev-parse", "HEAD"]).stdout.strip()
        image, workflow_sha = runtime._k6_image_binding(commit)
        workflow_bytes = runtime._git_blob_bytes(commit, runtime.K6_WORKFLOW)
        self.assertEqual(
            image,
            "grafana/k6@sha256:65c920dc067d5e2e00befbf982af6ad6ad0117034e8b1c65817c7975c52d4669",
        )
        self.assertEqual(
            workflow_sha,
            hashlib.sha256(workflow_bytes).hexdigest(),
        )

    def test_kubernetes_quantity_parsers_bind_declared_hard_limits(self) -> None:
        self.assertEqual(runtime._parse_cpu_quantity("1"), 1.0)
        self.assertEqual(runtime._parse_cpu_quantity("500m"), 0.5)
        self.assertEqual(runtime._parse_memory_quantity("512Mi"), 512 * 1024 * 1024)
        self.assertEqual(runtime._parse_memory_quantity("2Gi"), 2 * 1024 * 1024 * 1024)

    def test_t048_api_resource_contract_rejects_live_limit_drift(self) -> None:
        config = runtime.load_config()
        pod = {
            "spec": {
                "containers": [
                    {
                        "name": "api",
                        "resources": {
                            "limits": {"cpu": "1", "memory": "512Mi"}
                        },
                    }
                ]
            }
        }
        self.assertEqual(
            runtime._require_api_resource_limits(pod, config),
            (1.0, 512 * 1024 * 1024),
        )
        for key, value in (("cpu", "2"), ("memory", "1Gi")):
            with self.subTest(limit=key):
                drifted = json.loads(json.dumps(pod))
                drifted["spec"]["containers"][0]["resources"]["limits"][key] = value
                with self.assertRaisesRegex(
                    runtime.RuntimeErrorEB,
                    "resource limits drifted",
                ):
                    runtime._require_api_resource_limits(drifted, config)

    def test_fixture_stream_rewrites_only_psql_client_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            load = root / "load.sql"
            nodes = root / "nodes.csv"
            edges = root / "edges.csv"
            output = root / "kubernetes-load.sql"
            load.write_text(
                "\\set ON_ERROR_STOP on\n"
                "BEGIN;\n"
                "\\copy weltgewebe_perf.domain_nodes (id) FROM '/host/nodes.csv' WITH (FORMAT csv, HEADER true)\n"
                "\\copy weltgewebe_perf.domain_edges (id) FROM '/host/edges.csv' WITH (FORMAT csv, HEADER true)\n"
                "COMMIT;\n",
                encoding="utf-8",
            )
            nodes.write_text("id\nn-1\n", encoding="utf-8")
            edges.write_text("id\ne-1\n", encoding="utf-8")
            runtime._write_streamed_fixture_sql(load, nodes, edges, output)
            rendered = output.read_text(encoding="utf-8")
            self.assertNotIn("/host/", rendered)
            self.assertEqual(rendered.count("FROM STDIN"), 2)
            self.assertEqual(rendered.count("\\.\n"), 2)
            self.assertIn("n-1", rendered)
            self.assertIn("e-1", rendered)

    def test_state_root_is_scoped_to_experiment_b_subtree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "experiment-b"
            sibling = Path(tmp) / "other-controller"
            with mock.patch.object(runtime, "DEFAULT_STATE_ROOT", base):
                self.assertEqual(runtime.state_root(str(base)), base.resolve())
                child = base / "attempt-1"
                self.assertEqual(runtime.state_root(str(child)), child.resolve())
                with self.assertRaises(runtime.RuntimeErrorEB):
                    runtime.state_root(str(base.parent))
                with self.assertRaises(runtime.RuntimeErrorEB):
                    runtime.state_root(str(sibling))

    def test_lifecycle_rejects_symlinked_default_state_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            allowed_root = parent / "experiment-b"
            outside = parent / "outside"
            outside.mkdir()
            allowed_root.symlink_to(outside, target_is_directory=True)
            lock_path = parent / "experiment-b.lifecycle.lock"

            with (
                mock.patch.object(runtime, "DEFAULT_STATE_ROOT", allowed_root),
                mock.patch.object(
                    runtime,
                    "EXPERIMENT_B_LIFECYCLE_LOCK",
                    lock_path,
                ),
            ):
                root = runtime.state_root(None)

                @runtime._serialize_experiment_b_lifecycle
                def operation(operation_root: Path) -> None:
                    (operation_root / "escaped").write_text(
                        "escaped",
                        encoding="utf-8",
                    )

                with self.assertRaisesRegex(
                    runtime.RuntimeErrorEB,
                    "state root is unsafe",
                ):
                    operation(root)

            self.assertEqual(list(outside.iterdir()), [])

    def test_lifecycle_rejects_symlinked_lock_parent_without_external_write(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            outside = parent / "outside"
            outside.mkdir()
            state_parent = parent / "commonthing"
            state_parent.symlink_to(outside, target_is_directory=True)
            allowed_root = state_parent / "experiment-b"
            lock_path = state_parent / "experiment-b.lifecycle.lock"

            with (
                mock.patch.object(runtime, "DEFAULT_STATE_ROOT", allowed_root),
                mock.patch.object(
                    runtime,
                    "EXPERIMENT_B_LIFECYCLE_LOCK",
                    lock_path,
                ),
            ):
                root = runtime.state_root(None)

                @runtime._serialize_experiment_b_lifecycle
                def operation(_operation_root: Path) -> None:
                    self.fail("lifecycle unexpectedly entered")

                with self.assertRaises(runtime.RuntimeErrorEB):
                    operation(root)

            self.assertEqual(list(outside.iterdir()), [])

    def test_wait_http_200_retries_transient_connection_refusal(self) -> None:
        responses = [
            runtime.urllib.error.URLError("listener not ready"),
            (200, b"ok", 1.0),
        ]

        def read(*_args, **_kwargs):
            value = responses.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

        with (
            mock.patch.object(runtime, "_http_read", side_effect=read),
            mock.patch.object(runtime.time, "sleep"),
        ):
            runtime._wait_http_200("http://127.0.0.1:1/health/live")
        self.assertEqual(responses, [])

    def test_cilium_expected_runtime_contract_comes_from_pinned_chart_render(
        self,
    ) -> None:
        config = runtime.load_config()
        manifest = """apiVersion: v1
kind: ConfigMap
metadata:
  name: cilium-config
  namespace: kube-system
data:
  enable-policy: default
  enable-gateway-api: "true"
  kube-proxy-replacement: "true"
---
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: cilium
  namespace: kube-system
spec:
  selector:
    matchLabels:
      k8s-app: cilium
  template:
    spec:
      serviceAccountName: cilium
      hostNetwork: true
      initContainers:
        - name: config
          image: quay.io/cilium/startup-script:1
      containers:
        - name: cilium-agent
          image: quay.io/cilium/cilium:v1.19.5
          securityContext:
            privileged: true
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: cilium-operator
  namespace: kube-system
spec:
  selector:
    matchLabels:
      io.cilium/app: operator
  template:
    spec:
      serviceAccountName: cilium-operator
      containers:
        - name: cilium-operator
          image: quay.io/cilium/operator-generic:v1.19.5
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: hubble-relay
  namespace: kube-system
spec:
  replicas: 1
  selector:
    matchLabels:
      k8s-app: hubble-relay
  template:
    spec:
      serviceAccountName: hubble-relay
      containers:
        - name: hubble-relay
          image: quay.io/cilium/hubble-relay:v1.19.5
"""
        runner = mock.Mock(
            return_value=runtime.subprocess.CompletedProcess(
                ["helm"], 0, stdout=manifest, stderr=""
            )
        )
        receipt = {
            "tools": {"helm": "helm"},
            "artifacts": {
                "cilium_chart": "/verified/cilium-1.19.5.tgz"
            },
        }
        with (
            mock.patch.object(runtime, "run", runner),
            mock.patch.object(runtime, "kube_env", return_value={}),
            mock.patch.object(
                runtime, "vm_ip", return_value="192.168.122.10"
            ),
        ):
            contract = runtime._expected_cilium_runtime_contract(
                Path("."), config, receipt
            )
        self.assertEqual(
            contract["config_map"],
            {
                "data": {
                    "enable-policy": "default",
                    "enable-gateway-api": "true",
                    "kube-proxy-replacement": "true",
                },
                "binaryData": {},
                "immutable": False,
            },
        )
        self.assertEqual(
            contract["daemonset"]["images"],
            {
                "containers": {
                    "cilium-agent": "quay.io/cilium/cilium:v1.19.5"
                },
                "init_containers": {
                    "config": "quay.io/cilium/startup-script:1"
                },
            },
        )
        self.assertEqual(
            contract["daemonset"]["selector_labels"],
            {"k8s-app": "cilium"},
        )
        self.assertEqual(
            contract["daemonset"]["rollout"],
            {
                "minReadySeconds": 0,
                "revisionHistoryLimit": 10,
                "updateStrategy": {
                    "type": "RollingUpdate",
                    "rollingUpdate": {
                        "maxUnavailable": 1,
                        "maxSurge": 0,
                    },
                },
            },
        )
        self.assertTrue(
            contract["daemonset"]["pod_spec"]["hostNetwork"]
        )
        self.assertEqual(
            contract["daemonset"]["pod_spec"]["containers"][
                "cilium-agent"
            ]["securityContext"],
            {"privileged": True},
        )
        self.assertEqual(
            contract["operator"]["selector_labels"],
            {"io.cilium/app": "operator"},
        )
        self.assertEqual(
            contract["operator"]["pod_spec"]["serviceAccountName"],
            "cilium-operator",
        )
        self.assertEqual(
            contract["operator"]["rollout"],
            {
                "revisionHistoryLimit": 10,
                "strategy": {
                    "type": "RollingUpdate",
                    "rollingUpdate": {
                        "maxSurge": "25%",
                        "maxUnavailable": "25%",
                    },
                },
            },
        )
        self.assertEqual(
            contract["relay"]["selector_labels"],
            {"k8s-app": "hubble-relay"},
        )
        self.assertEqual(contract["relay"]["replicas"], 1)
        self.assertEqual(
            contract["relay"]["rollout"],
            {
                "revisionHistoryLimit": 10,
                "strategy": {
                    "type": "RollingUpdate",
                    "rollingUpdate": {
                        "maxSurge": "25%",
                        "maxUnavailable": "25%",
                    },
                },
            },
        )
        self.assertEqual(
            contract["relay"]["pod_spec"]["serviceAccountName"],
            "hubble-relay",
        )
        self.assertEqual(
            contract["relay"]["images"]["containers"]["hubble-relay"],
            "quay.io/cilium/hubble-relay:v1.19.5",
        )
        argv = runner.call_args.args[0]
        self.assertEqual(
            argv[:4],
            [
                "helm",
                "template",
                "cilium",
                "/verified/cilium-1.19.5.tgz",
            ],
        )
        self.assertIn("--kube-version", argv)
        self.assertIn("gatewayAPI.enabled=true", argv)
        self.assertIn("kubeProxyReplacement=true", argv)

    def test_cilium_pod_projection_normalizes_only_kubernetes_api_defaults(
        self,
    ) -> None:
        expected = {
            "serviceAccountName": "cilium",
            "priorityClassName": None,
            "volumes": [
                {
                    "name": "bpf-maps",
                    "hostPath": {"path": "/sys/fs/bpf"},
                },
                {
                    "name": "config",
                    "configMap": {"name": "cilium-config"},
                },
            ],
            "initContainers": [
                {
                    "name": "config",
                    "image": "quay.io/cilium/cilium:vfixture",
                    "resources": None,
                },
                {
                    "name": "install-cni-binaries",
                    "image": "quay.io/cilium/cilium:vfixture",
                    "resources": {"limits": {"cpu": 1}},
                },
            ],
            "containers": [
                {
                    "name": "cilium-agent",
                    "image": "quay.io/cilium/cilium:vfixture",
                    "resources": None,
                    "volumeMounts": [
                        {
                            "name": "bpf-maps",
                            "mountPath": "/sys/fs/bpf",
                        }
                    ],
                    "livenessProbe": {"grpc": {"port": 4244}},
                }
            ],
        }
        live = json.loads(json.dumps(expected))
        live["priorityClassName"] = ""
        live["volumes"][0]["hostPath"]["type"] = ""
        live["volumes"][1]["configMap"]["defaultMode"] = 420
        live["containers"][0]["livenessProbe"]["grpc"]["service"] = ""
        live["initContainers"][0]["resources"] = {}
        live["initContainers"][1]["resources"]["limits"]["cpu"] = "1"
        live["containers"][0]["resources"] = {}
        live["containers"][0]["volumeMounts"][0]["readOnly"] = False

        expected_projection = runtime._cilium_pod_spec_projection(
            expected, "expected Cilium Pod"
        )
        live_projection = runtime._cilium_pod_spec_projection(
            live, "live Cilium Pod"
        )
        self.assertEqual(expected_projection, live_projection)

        read_only_drift = json.loads(json.dumps(live))
        read_only_drift["containers"][0]["volumeMounts"][0]["readOnly"] = True
        self.assertNotEqual(
            expected_projection,
            runtime._cilium_pod_spec_projection(
                read_only_drift, "drifted Cilium Pod"
            ),
        )

        host_path_drift = json.loads(json.dumps(live))
        host_path_drift["volumes"][0]["hostPath"]["type"] = "Directory"
        self.assertNotEqual(
            expected_projection,
            runtime._cilium_pod_spec_projection(
                host_path_drift, "drifted Cilium Pod"
            ),
        )

        cpu_drift = json.loads(json.dumps(live))
        cpu_drift["initContainers"][1]["resources"]["limits"]["cpu"] = "2"
        self.assertNotEqual(
            expected_projection,
            runtime._cilium_pod_spec_projection(
                cpu_drift, "drifted Cilium Pod"
            ),
        )

        mode_drift = json.loads(json.dumps(live))
        mode_drift["volumes"][1]["configMap"]["defaultMode"] = 511
        self.assertNotEqual(
            expected_projection,
            runtime._cilium_pod_spec_projection(
                mode_drift, "drifted Cilium Pod"
            ),
        )

        grpc_service_drift = json.loads(json.dumps(live))
        grpc_service_drift["containers"][0]["livenessProbe"]["grpc"][
            "service"
        ] = "shadow"
        self.assertNotEqual(
            expected_projection,
            runtime._cilium_pod_spec_projection(
                grpc_service_drift, "drifted Cilium Pod"
            ),
        )

    def test_t048_fixture_activates_canonical_synthetic_projections_atomically(self) -> None:
        source = inspect.getsource(runtime.seed_t048_fixture)
        self.assertIn("INSERT INTO search_node_projections", source)
        self.assertIn("UPDATE search_projection_jobs", source)
        self.assertIn("state = 'done'", source)
        self.assertIn("weltgewebe_search_generation_activation_ready", source)
        self.assertIn("weltgewebe_activate_search_generation", source)
        self.assertIn("synthetic-canonical-t048", source)

    def test_semantic_provider_smoke_is_separate_from_t048_generation(self) -> None:
        source = inspect.getsource(runtime.semantic_activate)
        live = inspect.getsource(runtime._semantic_provider_live_readback)
        self.assertIn("/api/embed", live)
        self.assertIn("/api/tags", live)
        self.assertIn("_run_bound_container_command(", live)
        self.assertNotIn("deployment/weltgewebe-api", live)
        self.assertIn('"database_generation_activation": False', source)
        self.assertNotIn("weltgewebe_search_generation_activation_ready", source)
        self.assertNotIn("weltgewebe_activate_search_generation", source)

    def test_semantic_provider_live_readback_requires_pinned_model_and_embedding(self) -> None:
        commit = "a" * 40
        config = runtime.load_config()
        semantic = config["semantic_search"]
        dimension = int(semantic["dimension"])
        digest = str(semantic["model_revision"]).removeprefix("sha256:")
        binding = {
            "pod_name": "weltgewebe-api-test",
            "pod_uid": "pod-uid",
            "contract_sha256": "b" * 64,
            "pod_contract_sha256": "c" * 64,
            "runtime_image_ids_sha256": "d" * 64,
            "search_worker_container_id": "containerd://" + "e" * 64,
            "ollama_container_id": "containerd://" + "f" * 64,
        }
        tags = json.dumps(
            {
                "models": [
                    {
                        "name": semantic["model_id"],
                        "digest": digest,
                    }
                ]
            }
        ).encode("utf-8")
        embed = json.dumps(
            {"embeddings": [[0.0] * dimension]}
        ).encode("utf-8")
        with (
            mock.patch.object(runtime, "load_config", return_value=config),
            mock.patch.object(
                runtime,
                "_semantic_provider_runtime_binding",
                side_effect=[binding, binding],
            ) as runtime_binding,
            mock.patch.object(
                runtime,
                "_run_bound_container_command",
                side_effect=[tags, embed],
            ) as bound_exec,
        ):
            observed = runtime._semantic_provider_live_readback(
                Path("."),
                commit,
            )
        self.assertEqual(
            observed["model_revision"],
            semantic["model_revision"],
        )
        self.assertEqual(observed["dimension"], dimension)
        self.assertTrue(observed["embedding_probe"])
        self.assertEqual(
            observed["runtime_binding_sha256"],
            runtime._stable_json_sha256(binding),
        )
        self.assertEqual(runtime_binding.call_count, 2)
        self.assertEqual(bound_exec.call_count, 2)
        for call in bound_exec.call_args_list:
            self.assertEqual(
                call.args[2],
                binding["search_worker_container_id"],
            )

        drifted_binding = {
            **binding,
            "ollama_container_id": "containerd://" + "1" * 64,
        }
        with (
            mock.patch.object(runtime, "load_config", return_value=config),
            mock.patch.object(
                runtime,
                "_semantic_provider_runtime_binding",
                side_effect=[binding, drifted_binding],
            ),
            mock.patch.object(
                runtime,
                "_run_bound_container_command",
                side_effect=[tags, embed],
            ),
            self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "runtime changed during probe",
            ),
        ):
            runtime._semantic_provider_live_readback(
                Path("."),
                commit,
            )

        missing = json.dumps({"models": []}).encode("utf-8")
        with (
            mock.patch.object(runtime, "load_config", return_value=config),
            mock.patch.object(
                runtime,
                "_semantic_provider_runtime_binding",
                return_value=binding,
            ),
            mock.patch.object(
                runtime,
                "_run_bound_container_command",
                return_value=missing,
            ),
        ):
            with self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "model digest",
            ):
                runtime._semantic_provider_live_readback(
                    Path("."),
                    commit,
                )

    def test_semantic_provider_runtime_binding_requires_exact_container_ids(self) -> None:
        pod = {
            "metadata": {
                "name": "weltgewebe-api-test",
                "uid": "pod-uid",
            },
            "status": {
                "containerStatuses": [
                    {
                        "name": "search-worker",
                        "containerID": "containerd://" + "a" * 64,
                    },
                    {
                        "name": "ollama",
                        "containerID": "containerd://" + "b" * 64,
                    },
                ]
            },
        }
        runtime_binding = {
            "contract_sha256": "c" * 64,
            "pod_contract_sha256": "d" * 64,
            "runtime_image_ids_sha256": "e" * 64,
        }
        with mock.patch.object(
            runtime,
            "_require_t048_api_runtime_binding",
            return_value=(
                "weltgewebe-api-test",
                pod,
                runtime_binding,
            ),
        ):
            observed = runtime._semantic_provider_runtime_binding(
                Path("."),
                "f" * 40,
            )
        self.assertEqual(observed["pod_uid"], "pod-uid")
        self.assertEqual(
            observed["search_worker_container_id"],
            "containerd://" + "a" * 64,
        )
        self.assertEqual(
            observed["ollama_container_id"],
            "containerd://" + "b" * 64,
        )

        pod["status"]["containerStatuses"][0]["containerID"] = "broken"
        with (
            mock.patch.object(
                runtime,
                "_require_t048_api_runtime_binding",
                return_value=(
                    "weltgewebe-api-test",
                    pod,
                    runtime_binding,
                ),
            ),
            self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "search-worker containerID",
            ),
        ):
            runtime._semantic_provider_runtime_binding(
                Path("."),
                "f" * 40,
            )

    def test_live_check_attempt_invalidates_stale_success_and_binds_completion(self) -> None:
        commit = "a" * 40
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipts = root / "receipts"
            receipts.mkdir()
            receipt = receipts / "semantic-search.json"
            receipt.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "pass",
                        "source_commit": commit,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            portability = receipts / "portability.json"
            portability.write_text(
                json.dumps({"schema_version": 1, "status": "pass"}) + "\n",
                encoding="utf-8",
            )
            receipt_path, attempt_path, started = runtime._begin_live_check_attempt(
                root,
                "semantic-search",
                commit,
            )
            self.assertEqual(receipt_path, receipt)
            self.assertFalse(receipt.exists())
            self.assertFalse(portability.exists())
            running = json.loads(attempt_path.read_text(encoding="utf-8"))
            self.assertEqual(running["status"], "running")
            self.assertEqual(running["receipt"], "semantic-search.json")

            receipt.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "pass",
                        "source_commit": commit,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            runtime._complete_live_check_attempt(
                attempt_path,
                receipt,
                commit,
                started,
                "pass",
            )
            completed = json.loads(attempt_path.read_text(encoding="utf-8"))
            self.assertEqual(completed["status"], "pass")
            self.assertEqual(completed["receipt_sha256"], runtime.sha256_file(receipt))

    def test_release_attempt_invalidates_release_dependent_evidence(self) -> None:
        commit = "a" * 40
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipts = root / "receipts"
            receipts.mkdir()
            stale_names = ("release.json", *runtime.RELEASE_DEPENDENT_RECEIPTS)
            for name in stale_names:
                (receipts / name).write_text(
                    json.dumps({"schema_version": 1, "status": "stale"}) + "\n",
                    encoding="utf-8",
                )

            receipt_path, attempt_path, _ = runtime._begin_release_attempt(
                root, commit
            )

            self.assertFalse(receipt_path.exists())
            for name in runtime.RELEASE_DEPENDENT_RECEIPTS:
                self.assertFalse((receipts / name).exists(), name)
            attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
            self.assertEqual(attempt["status"], "running")
            self.assertEqual(attempt["source_commit"], commit)
            self.assertEqual(attempt["receipt"], "release.json")

    def test_upstream_reruns_invalidate_complete_downstream_proof_chain(self) -> None:
        release_tail = {
            "release.json",
            "release-attempt.json",
            *runtime.RELEASE_DEPENDENT_RECEIPTS,
        }
        self.assertEqual(
            set(runtime.SECRETS_ATTEMPT_INVALIDATES),
            {"secrets.json", *release_tail},
        )
        self.assertEqual(
            set(runtime.PLATFORM_ATTEMPT_INVALIDATES),
            {"platform.json", *runtime.SECRETS_ATTEMPT_INVALIDATES},
        )
        self.assertEqual(
            set(runtime.K3S_ATTEMPT_INVALIDATES),
            {"k3s.json", *runtime.PLATFORM_ATTEMPT_INVALIDATES},
        )
        self.assertEqual(
            set(runtime.VM_ATTEMPT_INVALIDATES),
            {
                "vm-create.json",
                "vm-create-attempt.json",
                *runtime.K3S_ATTEMPT_INVALIDATES,
            },
        )

        create_vm = inspect.getsource(runtime.create_vm)
        self.assertLess(
            create_vm.index("_invalidate_receipts(root, VM_ATTEMPT_INVALIDATES)"),
            create_vm.index("prepared = prepare(root, source_commit)"),
        )
        self.assertLess(
            create_vm.index("_invalidate_receipts(root, VM_ATTEMPT_INVALIDATES)"),
            create_vm.index("_open_libvirt_pool_target(create=True)"),
        )
        self.assertLess(
            create_vm.index("_libvirt_volume_sha256(root, BASE_VOLUME)"),
            create_vm.index('"virt-install"'),
        )
        self.assertIn(
            "uploaded cloud image digest drifted before VM boot", create_vm
        )

        install_k3s = inspect.getsource(runtime.install_k3s)
        self.assertLess(
            install_k3s.index("_invalidate_receipts(root, K3S_ATTEMPT_INVALIDATES)"),
            install_k3s.index("_current_protected_main_commit()"),
        )
        self.assertLess(
            install_k3s.index("_invalidate_receipts(root, K3S_ATTEMPT_INVALIDATES)"),
            install_k3s.index('scp_fd_to(root, ip, k3s_fd, "/tmp/k3s")'),
        )

        install_platform = inspect.getsource(runtime.install_platform)
        self.assertLess(
            install_platform.index(
                "_invalidate_receipts(root, PLATFORM_ATTEMPT_INVALIDATES)"
            ),
            install_platform.index("_current_protected_main_commit()"),
        )
        self.assertLess(
            install_platform.index(
                "_invalidate_receipts(root, PLATFORM_ATTEMPT_INVALIDATES)"
            ),
            install_platform.index('run([kubectl, "apply", "-f", artifacts[name]]'),
        )

        inject_secrets = inspect.getsource(runtime.inject_secrets)
        self.assertLess(
            inject_secrets.index(
                "_invalidate_receipts(root, SECRETS_ATTEMPT_INVALIDATES)"
            ),
            inject_secrets.index("_current_protected_main_commit()"),
        )
        self.assertLess(
            inject_secrets.index(
                "_invalidate_receipts(root, SECRETS_ATTEMPT_INVALIDATES)"
            ),
            inject_secrets.index(
                "kubectl_apply(root, render_namespaces(root, source_commit))"
            ),
        )

    def test_render_namespaces_is_source_commit_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_commit = "a" * 40
            with mock.patch.object(
                runtime,
                "_source_commit_kustomize_build",
                return_value="rendered",
            ) as render:
                self.assertEqual(
                    runtime.render_namespaces(root, source_commit),
                    "rendered",
                )
            render.assert_called_once_with(
                root,
                source_commit,
                runtime.NAMESPACES,
                runtime.NAMESPACES,
            )
        source = inspect.getsource(runtime.render_namespaces)
        self.assertIn("_source_commit_kustomize_build(", source)
        self.assertNotIn('run([kustomize, "build", str(NAMESPACES)]', source)

    def test_preflight_parses_exact_libvirt_active_field(self) -> None:
        payload = (
            "Name: default\n"
            "Active: no\n"
            "Autostart: yes\n"
            "Persistent: yes\n"
        )
        self.assertEqual(
            runtime._virsh_info_field(
                payload,
                "Active",
                "test network",
            ),
            "no",
        )
        source = inspect.getsource(runtime.preflight)
        self.assertIn("_virsh_info_field(", source)
        self.assertIn(".casefold()", source)
        self.assertNotIn('"yes" not in network', source)

    def test_preflight_rejects_missing_bwrap_before_runtime_effects(self) -> None:
        def require(command: str) -> str:
            if command == "bwrap":
                raise runtime.RuntimeErrorEB("missing bwrap")
            return f"/usr/bin/{command}"

        with (
            mock.patch.object(runtime, "load_config", return_value={}),
            mock.patch.object(runtime, "require_binary", side_effect=require),
            mock.patch.object(runtime, "run") as run,
        ):
            with self.assertRaisesRegex(runtime.RuntimeErrorEB, "missing bwrap"):
                runtime.preflight()
        run.assert_not_called()

    def test_create_vm_rerun_invalidates_stale_chain_before_prepare_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipts = root / "receipts"
            receipts.mkdir()
            for name in runtime.VM_ATTEMPT_INVALIDATES:
                (receipts / name).write_text(
                    json.dumps({"schema_version": 1, "status": "stale"}) + "\n",
                    encoding="utf-8",
                )
            absent = runtime.subprocess.CompletedProcess(
                ["virsh"], 1, stdout="", stderr=""
            )
            retirement = root / "experiment-b-retirement.json"
            with (
                mock.patch.object(runtime, "RETIREMENT_RECEIPT", retirement),
                mock.patch.object(runtime, "_current_protected_main_commit", return_value="a" * 40),
                mock.patch.object(runtime, "load_config", return_value={}),
                mock.patch.object(
                    runtime,
                    "_git_blob_sha256",
                    return_value=runtime.sha256_file(runtime.CONFIG_PATH),
                ),
                mock.patch.object(runtime, "run", return_value=absent),
                mock.patch.object(
                    runtime,
                    "prepare",
                    side_effect=runtime.RuntimeErrorEB("prepare failed"),
                ),
            ):
                with self.assertRaisesRegex(runtime.RuntimeErrorEB, "prepare failed"):
                    runtime.create_vm(root)

            for name in set(runtime.VM_ATTEMPT_INVALIDATES) - {"vm-create-attempt.json"}:
                self.assertFalse((receipts / name).exists(), name)
            attempt = json.loads(
                (receipts / "vm-create-attempt.json").read_text(encoding="utf-8")
            )
            self.assertEqual(attempt["status"], "running")
            self.assertEqual(attempt["source_commit"], "a" * 40)
            self.assertEqual(attempt["state_root"], str(root.resolve()))
            self.assertEqual(attempt["vm"], runtime.VM_NAME)
            self.assertEqual(attempt["pool"], runtime.POOL_NAME)

    def test_create_vm_rerun_invalidates_retirement_before_prepare_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            retirement = root / "experiment-b-retirement.json"
            runtime.atomic_json(
                retirement,
                {"schema_version": 1, "status": "retired"},
            )
            absent = runtime.subprocess.CompletedProcess(
                ["virsh"], 1, stdout="", stderr=""
            )
            with (
                mock.patch.object(runtime, "RETIREMENT_RECEIPT", retirement),
                mock.patch.object(
                    runtime, "_current_protected_main_commit", return_value="a" * 40
                ),
                mock.patch.object(runtime, "load_config", return_value={}),
                mock.patch.object(
                    runtime,
                    "_git_blob_sha256",
                    return_value=runtime.sha256_file(runtime.CONFIG_PATH),
                ),
                mock.patch.object(runtime, "run", return_value=absent),
                mock.patch.object(
                    runtime,
                    "prepare",
                    side_effect=runtime.RuntimeErrorEB("prepare failed"),
                ),
            ):
                with self.assertRaisesRegex(runtime.RuntimeErrorEB, "prepare failed"):
                    runtime.create_vm(root)

            self.assertFalse(retirement.exists())

    def test_dirty_k3s_rerun_invalidates_stale_chain_before_binding_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipts = root / "receipts"
            receipts.mkdir()
            for name in runtime.K3S_ATTEMPT_INVALIDATES:
                (receipts / name).write_text(
                    json.dumps({"schema_version": 1, "status": "stale"}) + "\n",
                    encoding="utf-8",
                )
            with mock.patch.object(
                runtime,
                "_current_protected_main_commit",
                side_effect=runtime.RuntimeErrorEB("dirty checkout"),
            ):
                with self.assertRaisesRegex(runtime.RuntimeErrorEB, "dirty checkout"):
                    runtime.install_k3s(root)
            for name in runtime.K3S_ATTEMPT_INVALIDATES:
                self.assertFalse((receipts / name).exists(), name)

    def test_install_k3s_kubeconfig_write_replaces_symlink_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = parent / "state"
            root.mkdir()
            sentinel = parent / "sentinel"
            sentinel.write_bytes(b"sentinel-bytes")
            kubeconfig = root / "kubeconfig.yaml"
            kubeconfig.symlink_to(sentinel)
            expected = b"apiVersion: v1\nclusters: []\n"

            runtime._write_kubeconfig(root, expected.decode("utf-8"))

            self.assertEqual(sentinel.read_bytes(), b"sentinel-bytes")
            self.assertFalse(kubeconfig.is_symlink())
            self.assertEqual(kubeconfig.read_bytes(), expected)
            self.assertEqual(kubeconfig.stat().st_mode & 0o777, 0o600)

    def test_atomic_outputs_reject_symlinked_parent_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = parent / "state"
            root.mkdir()
            outside = parent / "outside"
            outside.mkdir()
            sentinel = outside / "status.json"
            sentinel.write_text("sentinel\\n", encoding="utf-8")
            (root / "receipts").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "state output parent is unsafe",
            ):
                runtime.atomic_json(
                    root / "receipts/status.json",
                    {"status": "changed"},
                )

            self.assertEqual(
                sentinel.read_text(encoding="utf-8"),
                "sentinel\\n",
            )

        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = parent / "state"
            root.mkdir()
            outside = parent / "outside"
            outside.mkdir()
            sentinel = outside / "registry-auth.json"
            sentinel.write_bytes(b"sentinel-bytes")
            (root / "secrets").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "state output parent is unsafe",
            ):
                runtime.atomic_bytes(
                    root / "secrets/registry-auth.json",
                    b"changed",
                )

            self.assertEqual(sentinel.read_bytes(), b"sentinel-bytes")

    def test_download_rejects_symlinked_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = parent / "state"
            root.mkdir()
            outside = parent / "outside"
            outside.mkdir()
            (root / "downloads").symlink_to(
                outside,
                target_is_directory=True,
            )
            payload = b"download-payload"
            source = parent / "source.bin"
            source.write_bytes(payload)
            destination = root / "downloads/payload.bin"

            with self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "download directory is unsafe",
            ):
                runtime.download(
                    source.as_uri(),
                    hashlib.sha256(payload).hexdigest(),
                    destination,
                )

            self.assertFalse((outside / destination.name).exists())

    def test_prepare_rejects_symlinked_k3s_download_before_chmod(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = parent / "state"
            downloads = root / "downloads"
            downloads.mkdir(parents=True)
            cloud_payload = b"cloud-image"
            (downloads / "cloud.qcow2").write_bytes(cloud_payload)
            k3s_payload = b"k3s-binary"
            sentinel = parent / "sentinel-k3s"
            sentinel.write_bytes(k3s_payload)
            sentinel.chmod(0o640)
            (downloads / "k3s").symlink_to(sentinel)
            config = {
                "vm": {
                    "image": {
                        "url": "https://example.invalid/cloud.qcow2",
                        "sha256": hashlib.sha256(cloud_payload).hexdigest(),
                    }
                },
                "kubernetes": {
                    "binary_url": "https://example.invalid/k3s",
                    "binary_sha256": hashlib.sha256(k3s_payload).hexdigest(),
                },
            }
            bound_contract = mock.Mock()
            bound_contract.render_cloud_init.side_effect = runtime.RuntimeErrorEB(
                "unexpected post-download execution"
            )
            public_key = root / "ssh/id_ed25519.pub"

            with (
                mock.patch.object(runtime, "load_config", return_value=config),
                mock.patch.object(
                    runtime,
                    "_source_bound_contract",
                    return_value=bound_contract,
                ),
                mock.patch.object(
                    runtime,
                    "ensure_ssh_key",
                    return_value=(root / "ssh/id_ed25519", public_key),
                ),
                self.assertRaisesRegex(
                    runtime.RuntimeErrorEB,
                    "download destination is unsafe",
                ),
            ):
                runtime.prepare.__wrapped__(root, "a" * 40)

            self.assertEqual(sentinel.stat().st_mode & 0o777, 0o640)

    def test_ensure_ssh_key_rejects_symlinked_private_key_without_touching_target(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = parent / "state"
            root.mkdir()
            ssh_dir = root / "ssh"
            ssh_dir.mkdir()
            sentinel = parent / "sentinel-key"
            sentinel.write_text("sentinel-key\\n", encoding="utf-8")
            sentinel.chmod(0o644)
            private = ssh_dir / "id_ed25519"
            private.symlink_to(sentinel)
            public = ssh_dir / "id_ed25519.pub"
            public.write_text("ssh-ed25519 test\\n", encoding="utf-8")

            with self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "SSH private key is unsafe",
            ):
                runtime.ensure_ssh_key(root)

            self.assertTrue(private.is_symlink())
            self.assertEqual(
                sentinel.read_text(encoding="utf-8"),
                "sentinel-key\\n",
            )
            self.assertEqual(sentinel.stat().st_mode & 0o777, 0o644)

    def test_ensure_ssh_key_rejects_symlinked_ssh_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = parent / "state"
            root.mkdir()
            outside = parent / "outside-ssh"
            outside.mkdir()
            private = outside / "id_ed25519"
            private.write_text("outside-private\\n", encoding="utf-8")
            private.chmod(0o644)
            (outside / "id_ed25519.pub").write_text(
                "ssh-ed25519 outside\\n",
                encoding="utf-8",
            )
            (root / "ssh").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "SSH state directory is unsafe",
            ):
                runtime.ensure_ssh_key(root)

            self.assertEqual(
                private.read_text(encoding="utf-8"),
                "outside-private\\n",
            )
            self.assertEqual(private.stat().st_mode & 0o777, 0o644)

    def test_ssh_command_binding_survives_private_key_path_swap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = parent / "state"
            ssh_dir = root / "ssh"
            ssh_dir.mkdir(parents=True)
            private = ssh_dir / "id_ed25519"
            private.write_bytes(b"original-private")
            private.chmod(0o600)
            (ssh_dir / "id_ed25519.pub").write_text(
                "ssh-ed25519 public\\n",
                encoding="utf-8",
            )
            sentinel = parent / "sentinel"
            sentinel.write_bytes(b"external-private")

            command = runtime.ssh_argv(root, "192.0.2.10") + ["true"]
            with runtime._bound_ssh_command(command) as (bound, pass_fds):
                retained = ssh_dir / "id_ed25519.retained"
                private.rename(retained)
                private.symlink_to(sentinel)
                key_path = Path(bound[bound.index("-i") + 1])
                self.assertEqual(key_path.parent, Path(f"/proc/{runtime.os.getpid()}/fd"))
                key_fd = runtime.os.open(key_path, runtime.os.O_RDONLY)
                try:
                    self.assertEqual(
                        runtime.os.read(key_fd, 1024),
                        b"original-private",
                    )
                finally:
                    runtime.os.close(key_fd)
                self.assertIn(int(key_path.name), pass_fds)

            self.assertEqual(sentinel.read_bytes(), b"external-private")

    def test_ssh_command_binding_survives_child_closefrom(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            ssh_dir = root / "ssh"
            ssh_dir.mkdir(parents=True)
            private = ssh_dir / "id_ed25519"
            private.write_bytes(b"original-private")
            private.chmod(0o600)
            (ssh_dir / "id_ed25519.pub").write_text(
                "ssh-ed25519 public\n",
                encoding="utf-8",
            )
            known_hosts = ssh_dir / "known_hosts"
            known_hosts.write_bytes(b"known-hosts")

            command = runtime.ssh_argv(root, "192.0.2.10") + ["true"]
            with runtime._bound_ssh_command(command) as (bound, pass_fds):
                key_path = bound[bound.index("-i") + 1]
                known_path = next(
                    value.split("=", 1)[1]
                    for value in bound
                    if value.startswith("UserKnownHostsFile=")
                )
                child = (
                    "import os,sys,pathlib;"
                    "[os.close(int(fd)) for fd in sys.argv[3].split(',') if fd];"
                    "sys.stdout.buffer.write(pathlib.Path(sys.argv[1]).read_bytes()+b'|'+pathlib.Path(sys.argv[2]).read_bytes())"
                )
                result = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        child,
                        key_path,
                        known_path,
                        ",".join(str(fd) for fd in pass_fds),
                    ],
                    pass_fds=pass_fds,
                    capture_output=True,
                    check=False,
                )

            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertEqual(result.stdout, b"original-private|known-hosts")

    def test_run_binds_ssh_credentials_at_process_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            ssh_dir = root / "ssh"
            ssh_dir.mkdir(parents=True)
            private = ssh_dir / "id_ed25519"
            private.write_bytes(b"private")
            private.chmod(0o600)
            (ssh_dir / "id_ed25519.pub").write_text(
                "ssh-ed25519 public\\n",
                encoding="utf-8",
            )
            completed = runtime.subprocess.CompletedProcess(
                ["ssh"],
                0,
                stdout="",
                stderr="",
            )
            with mock.patch.object(
                runtime.subprocess,
                "run",
                return_value=completed,
            ) as process:
                runtime.run(
                    [*runtime.ssh_argv(root, "192.0.2.10"), "true"],
                )

            argv = process.call_args.args[0]
            key_path = argv[argv.index("-i") + 1]
            self.assertTrue(key_path.startswith(f"/proc/{runtime.os.getpid()}/fd/"))
            known_hosts = next(
                value
                for value in argv
                if value.startswith("UserKnownHostsFile=")
            )
            self.assertIn(f"/proc/{runtime.os.getpid()}/fd/", known_hosts)
            self.assertGreaterEqual(len(process.call_args.kwargs["pass_fds"]), 2)

    def test_ssh_binding_closes_private_fd_when_known_hosts_open_fails(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            ssh_dir = root / "ssh"
            ssh_dir.mkdir(parents=True)
            private = ssh_dir / "id_ed25519"
            private.write_bytes(b"private")
            private.chmod(0o600)
            (ssh_dir / "id_ed25519.pub").write_text(
                "ssh-ed25519 public\\n",
                encoding="utf-8",
            )
            command = runtime.ssh_argv(root, "192.0.2.10") + ["true"]
            opened: list[int] = []
            original_open = runtime._open_regular_state_file_at

            def capture_private_fd(*args, **kwargs):
                file_fd = original_open(*args, **kwargs)
                opened.append(file_fd)
                return file_fd

            with (
                mock.patch.object(
                    runtime,
                    "_open_regular_state_file_at",
                    side_effect=capture_private_fd,
                ),
                mock.patch.object(
                    runtime,
                    "_open_or_create_regular_state_file_at",
                    side_effect=runtime.RuntimeErrorEB(
                        "SSH known-hosts file is unsafe"
                    ),
                ),
                mock.patch.object(
                    runtime.os,
                    "close",
                    wraps=runtime.os.close,
                ) as close_fd,
            ):
                with self.assertRaisesRegex(
                    runtime.RuntimeErrorEB,
                    "SSH known-hosts file is unsafe",
                ):
                    with runtime._bound_ssh_command(command):
                        self.fail("SSH binding unexpectedly succeeded")

            self.assertEqual(len(opened), 1)
            self.assertIn(mock.call(opened[0]), close_fd.call_args_list)

    def test_install_k3s_rechecks_pinned_binary_before_copy(self) -> None:
        commit = "a" * 40
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipts = root / "receipts"
            receipts.mkdir()
            binary = root / "downloads/k3s"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"tampered-k3s")
            config = json.loads(json.dumps(runtime.load_config()))
            config["kubernetes"]["binary_sha256"] = "0" * 64
            receipt = vm_receipt_fixture(state_root=root)
            runtime.atomic_json(receipts / "vm-create.json", receipt)
            with (
                mock.patch.object(
                    runtime,
                    "_current_protected_main_commit",
                    return_value=commit,
                ),
                mock.patch.object(runtime, "load_config", return_value=config),
                mock.patch.object(
                    runtime,
                    "_live_vm_substrate",
                    return_value=json.loads(json.dumps(receipt["substrate"])),
                ),
                mock.patch.object(runtime, "vm_ip", return_value="192.0.2.10"),
                mock.patch.object(runtime, "wait_ssh"),
                mock.patch.object(runtime, "scp_fd_to") as copy_to_vm,
            ):
                with self.assertRaisesRegex(
                    runtime.RuntimeErrorEB,
                    "k3s binary digest does not match",
                ):
                    runtime.install_k3s(root)
            copy_to_vm.assert_not_called()

        source = inspect.getsource(runtime.install_k3s)
        self.assertLess(
            source.index("_open_verified_k3s_binary(k3s_binary, expected_k3s_sha256)"),
            source.index('scp_fd_to(root, ip, k3s_fd, "/tmp/k3s")'),
        )
        self.assertNotIn('scp_to(root, ip, k3s_binary, "/tmp/k3s")', source)

        helper_source = inspect.getsource(runtime.scp_fd_to)
        self.assertIn("pass_fds=(source_fd,)", helper_source)
        self.assertIn("_reopenable_proc_fd_path(source_fd)", helper_source)

        self.assertNotIn(
            'scp_to(root, ip, CLUSTER / "k3s-config.yaml"', source
        )
        self.assertNotIn(
            'scp_to(root, ip, CLUSTER / "k3s.service"', source
        )
        self.assertIn("_git_blob_sha256(source_commit, config_path)", source)
        self.assertIn("_git_blob_sha256(source_commit, service_path)", source)
        self.assertLess(
            source.index("staged_digests = _parse_sha256sum_output("),
            source.index("install_command = ("),
        )
        self.assertLess(
            source.index("installed_digests = _parse_sha256sum_output("),
            source.index("service_command = ("),
        )
        self.assertLess(
            source.index("installed k3s files drifted before service restart"),
            source.index("sudo systemctl restart k3s"),
        )

    def test_open_verified_k3s_binary_binds_the_opened_inode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "k3s"
            fixture_bytes = b"k3s-fixture-bytes"
            path.write_bytes(fixture_bytes)
            expected = hashlib.sha256(fixture_bytes).hexdigest()
            file_fd = runtime._open_verified_k3s_binary(path, expected)
            try:
                replacement = Path(tmp) / "replacement"
                replacement.write_bytes(b"tampered-k3s-bytes")
                runtime.os.replace(replacement, path)
                self.assertEqual(runtime.os.read(file_fd, len(fixture_bytes)), fixture_bytes)
            finally:
                runtime.os.close(file_fd)

    def test_open_verified_k3s_binary_rejects_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            target.write_bytes(b"k3s-fixture-bytes")
            path = root / "k3s"
            path.symlink_to(target)
            expected = hashlib.sha256(target.read_bytes()).hexdigest()
            with self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "missing or unsafe",
            ):
                runtime._open_verified_k3s_binary(path, expected)

    def test_git_blob_sha256_binds_k3s_sources_to_exact_commit(self) -> None:
        head = runtime.git_head()
        config = runtime.load_config()
        config_path, service_path = runtime._k3s_contract_paths(config)
        self.assertEqual(
            runtime._git_blob_sha256(head, config_path),
            runtime.sha256_file(config_path),
        )
        self.assertEqual(
            runtime._git_blob_sha256(head, service_path),
            runtime.sha256_file(service_path),
        )

        live_runtime_source = inspect.getsource(runtime._require_live_k3s_runtime)
        self.assertIn(
            "_git_blob_sha256(source_commit, config_path)",
            live_runtime_source,
        )
        self.assertIn(
            "_git_blob_sha256(source_commit, service_path)",
            live_runtime_source,
        )
        self.assertNotIn("sha256_file(config_path)", live_runtime_source)
        self.assertNotIn("sha256_file(service_path)", live_runtime_source)

        portability_source = inspect.getsource(runtime.portability_report)
        self.assertIn(
            "_git_blob_sha256(source_commit, expected_k3s_config)",
            portability_source,
        )
        self.assertIn(
            "_git_blob_sha256(source_commit, expected_k3s_service)",
            portability_source,
        )
        self.assertNotIn(
            "sha256_file(expected_k3s_config)",
            portability_source,
        )
        self.assertNotIn(
            "sha256_file(expected_k3s_service)",
            portability_source,
        )

        source = inspect.getsource(runtime._k3s_contract_paths)
        self.assertIn("config_path.is_symlink()", source)
        self.assertIn("service_path.is_symlink()", source)
        self.assertNotIn(".resolve()", source)

        storage_path = runtime.CLUSTER / "data/storage.yaml"
        self.assertEqual(
            runtime._git_blob_bytes(head, storage_path),
            storage_path.read_bytes(),
        )

    def test_install_k3s_rejects_vm_substrate_drift_before_ssh(self) -> None:
        commit = "a" * 40
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipts = root / "receipts"
            receipts.mkdir()
            config = runtime.load_config()
            receipt = vm_receipt_fixture(state_root=root)
            runtime.atomic_json(receipts / "vm-create.json", receipt)
            drifted = json.loads(json.dumps(receipt["substrate"]))
            drifted["uuid"] = "44444444-4444-4444-8444-444444444444"
            with (
                mock.patch.object(
                    runtime,
                    "_current_protected_main_commit",
                    return_value=commit,
                ),
                mock.patch.object(runtime, "load_config", return_value=config),
                mock.patch.object(
                    runtime,
                    "_live_vm_substrate",
                    return_value=drifted,
                ),
                mock.patch.object(runtime, "vm_ip") as vm_ip,
                mock.patch.object(runtime, "wait_ssh") as wait_ssh,
                mock.patch.object(runtime, "scp_to") as copy_to_vm,
            ):
                with self.assertRaisesRegex(
                    runtime.RuntimeErrorEB,
                    "VM substrate drift",
                ):
                    runtime.install_k3s(root)
            vm_ip.assert_not_called()
            wait_ssh.assert_not_called()
            copy_to_vm.assert_not_called()

    def test_install_k3s_restarts_and_requires_live_pinned_node(self) -> None:
        source = inspect.getsource(runtime.install_k3s)
        self.assertIn("sudo systemctl enable k3s && ", source)
        self.assertIn("sudo systemctl restart k3s", source)
        self.assertNotIn("sudo systemctl enable --now k3s", source)
        self.assertLess(
            source.index("installed_digests = _parse_sha256sum_output("),
            source.index("sudo systemctl restart k3s"),
        )
        self.assertIn("kubectl get nodes -o json", source)
        self.assertIn("_require_exact_k3s_node_inventory(", source)

        expected = "v1.36.1+k3s1"
        inventory = {
            "apiVersion": "v1",
            "kind": "NodeList",
            "items": [
                {
                    "kind": "Node",
                    "metadata": {"name": runtime.VM_NAME},
                    "status": {
                        "conditions": [{"type": "Ready", "status": "True"}],
                        "nodeInfo": {
                            "kubeletVersion": expected,
                            "osImage": "Ubuntu 24.04 LTS",
                        },
                    },
                }
            ],
        }
        readback = runtime._require_exact_k3s_node_inventory(inventory, expected)
        self.assertEqual(readback["kubelet_version"], expected)
        self.assertTrue(readback["ready"])
        self.assertIn("require_ready=False", source)

        not_ready = json.loads(json.dumps(inventory))
        not_ready["items"][0]["status"]["conditions"][0]["status"] = "False"
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "not Ready"):
            runtime._require_exact_k3s_node_inventory(not_ready, expected)
        pre_cni = runtime._require_exact_k3s_node_inventory(
            not_ready,
            expected,
            require_ready=False,
        )
        self.assertEqual(pre_cni["kubelet_version"], expected)
        self.assertFalse(pre_cni["ready"])

        missing_ready = json.loads(json.dumps(inventory))
        missing_ready["items"][0]["status"]["conditions"] = []
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "Ready condition is invalid"):
            runtime._require_exact_k3s_node_inventory(
                missing_ready,
                expected,
                require_ready=False,
            )

        ambiguous_ready = json.loads(json.dumps(inventory))
        ambiguous_ready["items"][0]["status"]["conditions"].append(
            {"type": "Ready", "status": "False"}
        )
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "Ready condition is invalid"):
            runtime._require_exact_k3s_node_inventory(
                ambiguous_ready,
                expected,
                require_ready=False,
            )

        invalid_ready = json.loads(json.dumps(inventory))
        invalid_ready["items"][0]["status"]["conditions"][0]["status"] = "Unknown"
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "Ready condition is invalid"):
            runtime._require_exact_k3s_node_inventory(
                invalid_ready,
                expected,
                require_ready=False,
            )

        stale = json.loads(json.dumps(inventory))
        stale["items"][0]["status"]["nodeInfo"]["kubeletVersion"] = "v1.35.0+k3s1"
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "pinned k3s version"):
            runtime._require_exact_k3s_node_inventory(
                stale,
                expected,
                require_ready=False,
            )

    def test_live_k3s_runtime_binds_vm_kubeconfig_guest_files_and_process(self) -> None:
        commit = runtime.git_head()
        ip = "192.168.122.10"
        config = runtime.load_config()
        config_path, service_path = runtime._k3s_contract_paths(config)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "receipts").mkdir()
            kubeconfig = root / "kubeconfig.yaml"

            def write_kubeconfig(server: str) -> None:
                kubeconfig.write_text(
                    yaml.safe_dump(
                        {
                            "apiVersion": "v1",
                            "kind": "Config",
                            "current-context": "default",
                            "contexts": [
                                {
                                    "name": "default",
                                    "context": {
                                        "cluster": "default",
                                        "user": "default",
                                    },
                                }
                            ],
                            "clusters": [
                                {
                                    "name": "default",
                                    "cluster": {"server": server},
                                }
                            ],
                            "users": [{"name": "default", "user": {}}],
                        },
                        sort_keys=True,
                    ),
                    encoding="utf-8",
                )
                kubeconfig.chmod(0o600)

            def write_receipt() -> None:
                runtime.atomic_json(
                    root / "receipts/k3s.json",
                    {
                        "schema_version": 1,
                        "status": "ready",
                        "source_commit": commit,
                        "vm_ip": ip,
                        "k3s_version": (
                            f"k3s version {config['kubernetes']['version']}"
                        ),
                        "live_kubelet_version": config["kubernetes"]["version"],
                        "binary_sha256": config["kubernetes"]["binary_sha256"],
                        "config_sha256": runtime.sha256_file(config_path),
                        "service_sha256": runtime.sha256_file(service_path),
                        "kubeconfig_sha256": runtime.sha256_file(kubeconfig),
                    },
                )

            write_kubeconfig(f"https://{ip}:6443")
            write_receipt()
            guest_digests = {
                "/usr/local/bin/k3s": config["kubernetes"]["binary_sha256"],
                "/etc/rancher/k3s/config.yaml": runtime.sha256_file(config_path),
                "/etc/systemd/system/k3s.service": runtime.sha256_file(service_path),
            }
            systemctl = {
                "LoadState": "loaded",
                "ActiveState": "active",
                "SubState": "running",
                "UnitFileState": "enabled",
                "FragmentPath": "/etc/systemd/system/k3s.service",
                "DropInPaths": "",
                "MainPID": "4321",
            }
            process_exe = "/usr/local/bin/k3s"
            process_cmdline = "/usr/local/bin/k3s\0server\0"
            process_environment = "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin\0"

            def runner(argv: list[str], **_kwargs):
                nonlocal process_exe, process_cmdline, process_environment
                if "sha256sum" in argv:
                    stdout = "".join(
                        f"{guest_digests[path]}  {path}\n"
                        for path in (
                            "/usr/local/bin/k3s",
                            "/etc/rancher/k3s/config.yaml",
                            "/etc/systemd/system/k3s.service",
                        )
                    )
                elif "systemctl" in argv:
                    stdout = "".join(
                        f"{key}={systemctl[key]}\n"
                        for key in (
                            "LoadState",
                            "ActiveState",
                            "SubState",
                            "UnitFileState",
                            "FragmentPath",
                            "DropInPaths",
                            "MainPID",
                        )
                    )
                elif "readlink" in argv:
                    stdout = process_exe + "\n"
                elif "cat" in argv and any("/cmdline" in item for item in argv):
                    stdout = process_cmdline
                elif "cat" in argv and any("/environ" in item for item in argv):
                    stdout = process_environment
                else:
                    raise AssertionError(f"unexpected k3s readback command: {argv}")
                return runtime.subprocess.CompletedProcess(
                    argv, 0, stdout=stdout, stderr=""
                )

            with (
                mock.patch.object(runtime, "vm_ip", return_value=ip),
                mock.patch.object(runtime, "run", side_effect=runner),
            ):
                observed = runtime._require_live_k3s_runtime(
                    root, config, commit
                )
            self.assertEqual(observed["vm_ip"], ip)
            self.assertEqual(observed["process_exe"], "/usr/local/bin/k3s")
            self.assertEqual(
                observed["binary_sha256"],
                config["kubernetes"]["binary_sha256"],
            )

            write_kubeconfig("https://192.168.122.99:6443")
            write_receipt()
            with mock.patch.object(runtime, "vm_ip", return_value=ip):
                with self.assertRaisesRegex(
                    runtime.RuntimeErrorEB, "VM API server"
                ):
                    runtime._require_live_k3s_runtime(root, config, commit)

            write_kubeconfig(f"https://{ip}:6443")
            write_receipt()
            guest_digests["/usr/local/bin/k3s"] = "0" * 64
            with (
                mock.patch.object(runtime, "vm_ip", return_value=ip),
                mock.patch.object(runtime, "run", side_effect=runner),
            ):
                with self.assertRaisesRegex(
                    runtime.RuntimeErrorEB, "installed k3s files drifted"
                ):
                    runtime._require_live_k3s_runtime(root, config, commit)
            guest_digests["/usr/local/bin/k3s"] = config["kubernetes"][
                "binary_sha256"
            ]

            systemctl["ActiveState"] = "inactive"
            with (
                mock.patch.object(runtime, "vm_ip", return_value=ip),
                mock.patch.object(runtime, "run", side_effect=runner),
            ):
                with self.assertRaisesRegex(
                    runtime.RuntimeErrorEB, "systemd service"
                ):
                    runtime._require_live_k3s_runtime(root, config, commit)
            systemctl["ActiveState"] = "active"

            systemctl["DropInPaths"] = "/etc/systemd/system/k3s.service.d/override.conf"
            with (
                mock.patch.object(runtime, "vm_ip", return_value=ip),
                mock.patch.object(runtime, "run", side_effect=runner),
            ):
                with self.assertRaisesRegex(
                    runtime.RuntimeErrorEB, "systemd service"
                ):
                    runtime._require_live_k3s_runtime(root, config, commit)
            systemctl["DropInPaths"] = ""

            process_cmdline = "/usr/local/bin/k3s\0server\0--disable=metrics-server\0"
            with (
                mock.patch.object(runtime, "vm_ip", return_value=ip),
                mock.patch.object(runtime, "run", side_effect=runner),
            ):
                with self.assertRaisesRegex(
                    runtime.RuntimeErrorEB, "process identity"
                ):
                    runtime._require_live_k3s_runtime(root, config, commit)
            process_cmdline = "/usr/local/bin/k3s\0server\0"

            process_environment = "PATH=/usr/bin\0K3S_DISABLE=traefik\0"
            with (
                mock.patch.object(runtime, "vm_ip", return_value=ip),
                mock.patch.object(runtime, "run", side_effect=runner),
            ):
                with self.assertRaisesRegex(
                    runtime.RuntimeErrorEB, "environment overrides"
                ):
                    runtime._require_live_k3s_runtime(root, config, commit)
            process_environment = "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin\0"

            process_exe = "/usr/local/bin/other"
            with (
                mock.patch.object(runtime, "vm_ip", return_value=ip),
                mock.patch.object(runtime, "run", side_effect=runner),
            ):
                with self.assertRaisesRegex(
                    runtime.RuntimeErrorEB, "process identity"
                ):
                    runtime._require_live_k3s_runtime(root, config, commit)

        source = inspect.getsource(runtime.status)
        self.assertLess(
            source.index("_require_live_k3s_runtime("),
            source.index("toolchain(root)"),
        )

    def test_flux_controller_contract_comes_from_pinned_install_export(self) -> None:
        documents = []
        for name in sorted(runtime.EXPECTED_FLUX_CONTROLLERS):
            documents.append(
                {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "metadata": {
                        "name": name,
                        "namespace": "flux-system",
                    },
                    "spec": {
                        "replicas": 1,
                        "selector": {
                            "matchLabels": {
                                "app.kubernetes.io/name": name,
                            }
                        },
                        "template": {
                            "spec": {
                                "serviceAccountName": name,
                                "securityContext": {"fsGroup": 1337},
                                "volumes": [
                                    {"name": "tmp", "emptyDir": {}},
                                ],
                                "containers": [
                                    {
                                        "name": "manager",
                                        "image": f"ghcr.io/fluxcd/{name}:vfixture",
                                        "args": ["--enable-leader-election"],
                                        "env": [
                                            {
                                                "name": "RUNTIME_NAMESPACE",
                                                "valueFrom": {
                                                    "fieldRef": {
                                                        "fieldPath": "metadata.namespace"
                                                    }
                                                },
                                            },
                                            {
                                                "name": "GOMEMLIMIT",
                                                "valueFrom": {
                                                    "resourceFieldRef": {
                                                        "containerName": "manager",
                                                        "resource": "limits.memory",
                                                    }
                                                },
                                            },
                                        ],
                                        "resources": {
                                            "limits": {
                                                "cpu": "1000m",
                                                "memory": "1Gi",
                                            }
                                        },
                                        "securityContext": {
                                            "allowPrivilegeEscalation": False,
                                            "runAsNonRoot": True,
                                        },
                                        "volumeMounts": [
                                            {
                                                "name": "tmp",
                                                "mountPath": "/tmp",
                                            }
                                        ],
                                    }
                                ],
                            }
                        },
                    },
                }
            )
        rendered = yaml.safe_dump_all(documents, sort_keys=True)
        completed = runtime.subprocess.CompletedProcess(
            ["flux"], 0, stdout=rendered, stderr=""
        )
        with mock.patch.object(runtime, "run", return_value=completed) as runner:
            observed = runtime._expected_flux_controller_contract(
                Path("/tmp/unused"),
                {"tools": {"flux": "/verified/flux"}},
            )
        self.assertEqual(set(observed), runtime.EXPECTED_FLUX_CONTROLLERS)
        runner.assert_called_once_with(
            runtime._flux_install_argv("/verified/flux", export=True)
        )
        for name, value in observed.items():
            self.assertEqual(value["replicas"], 1)
            self.assertEqual(
                value["selector_labels"],
                {"app.kubernetes.io/name": name},
            )
            self.assertEqual(
                value["images"]["containers"]["manager"],
                f"ghcr.io/fluxcd/{name}:vfixture",
            )
            pod_contract = value["contract"]["pod_spec"]
            self.assertEqual(pod_contract["serviceAccountName"], name)
            self.assertEqual(
                pod_contract["containers"]["manager"]["args"],
                ["--enable-leader-election"],
            )
            self.assertEqual(
                pod_contract["containers"]["manager"]["env"][0]["name"],
                "RUNTIME_NAMESPACE",
            )
            self.assertEqual(
                pod_contract["containers"]["manager"]["securityContext"],
                {
                    "allowPrivilegeEscalation": False,
                    "runAsNonRoot": True,
                },
            )
            self.assertEqual(
                pod_contract["volumes"],
                [{"name": "tmp", "emptyDir": {}}],
            )
            self.assertEqual(
                pod_contract["containers"]["manager"]["volumeMounts"],
                [{"name": "tmp", "mountPath": "/tmp"}],
            )
            self.assertEqual(
                value["contract_sha256"],
                runtime._stable_json_sha256(value["contract"]),
            )
            self.assertEqual(
                value["pod_contract_sha256"],
                runtime._stable_json_sha256(pod_contract),
            )

        missing = yaml.safe_dump_all(documents[:-1], sort_keys=True)
        with mock.patch.object(
            runtime,
            "run",
            return_value=runtime.subprocess.CompletedProcess(
                ["flux"], 0, stdout=missing, stderr=""
            ),
        ):
            with self.assertRaisesRegex(
                runtime.RuntimeErrorEB, "Deployment set drifted"
            ):
                runtime._expected_flux_controller_contract(
                    Path("/tmp/unused"),
                    {"tools": {"flux": "/verified/flux"}},
                )

    def test_flux_contract_normalizes_only_kubernetes_runtime_defaults(self) -> None:
        expected = {
            "metadata": {"name": "helm-controller", "namespace": "flux-system"},
            "spec": {
                "replicas": 1,
                "selector": {
                    "matchLabels": {"app.kubernetes.io/name": "helm-controller"}
                },
                "template": {
                    "spec": {
                        "serviceAccountName": "helm-controller",
                        "securityContext": {"fsGroup": 1337},
                        "priorityClassName": "system-cluster-critical",
                        "volumes": [{"name": "temp", "emptyDir": {}}],
                        "containers": [
                            {
                                "name": "manager",
                                "image": "ghcr.io/fluxcd/helm-controller:vfixture",
                                "args": ["--enable-leader-election"],
                                "env": [
                                    {
                                        "name": "RUNTIME_NAMESPACE",
                                        "valueFrom": {
                                            "fieldRef": {
                                                "fieldPath": "metadata.namespace"
                                            }
                                        },
                                    },
                                    {
                                        "name": "GOMEMLIMIT",
                                        "valueFrom": {
                                            "resourceFieldRef": {
                                                "containerName": "manager",
                                                "resource": "limits.memory",
                                            }
                                        },
                                    },
                                ],
                                "resources": {
                                    "limits": {
                                        "cpu": "1000m",
                                        "memory": "1Gi",
                                    }
                                },
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "runAsNonRoot": True,
                                },
                                "volumeMounts": [
                                    {"name": "temp", "mountPath": "/tmp"}
                                ],
                            }
                        ],
                    }
                },
            },
        }
        live_deployment = json.loads(json.dumps(expected))
        live_deployment["spec"]["strategy"] = {
            "type": "RollingUpdate",
            "rollingUpdate": {
                "maxSurge": "25%",
                "maxUnavailable": "25%",
            },
        }
        live_container = live_deployment["spec"]["template"]["spec"][
            "containers"
        ][0]
        live_container["env"][0]["valueFrom"]["fieldRef"]["apiVersion"] = "v1"
        live_container["env"][1]["valueFrom"]["resourceFieldRef"][
            "divisor"
        ] = "0"
        live_container["resources"]["limits"]["cpu"] = "1"

        expected_contract = runtime._flux_deployment_contract(
            expected, "expected Flux Deployment"
        )
        live_contract = runtime._flux_deployment_contract(
            live_deployment, "live Flux Deployment"
        )
        self.assertEqual(expected_contract["contract"], live_contract["contract"])

        expected_pod = expected["spec"]["template"]["spec"]
        live_pod = json.loads(json.dumps(live_deployment["spec"]["template"]["spec"]))
        injected_name = "kube-api-access-abc12"
        live_pod["volumes"].append(
            {
                "name": injected_name,
                "projected": {
                    "defaultMode": 420,
                    "sources": [
                        {
                            "serviceAccountToken": {
                                "expirationSeconds": 3607,
                                "path": "token",
                            }
                        },
                        {
                            "configMap": {
                                "name": "kube-root-ca.crt",
                                "items": [{"key": "ca.crt", "path": "ca.crt"}],
                            }
                        },
                        {
                            "downwardAPI": {
                                "items": [
                                    {
                                        "path": "namespace",
                                        "fieldRef": {
                                            "apiVersion": "v1",
                                            "fieldPath": "metadata.namespace",
                                        },
                                    }
                                ]
                            }
                        },
                    ],
                },
            }
        )
        live_pod["containers"][0]["volumeMounts"].append(
            {
                "mountPath": "/var/run/secrets/kubernetes.io/serviceaccount",
                "name": injected_name,
                "readOnly": True,
            }
        )
        live_pod["tolerations"] = [
            {
                "effect": "NoExecute",
                "key": "node.kubernetes.io/not-ready",
                "operator": "Exists",
                "tolerationSeconds": 300,
            },
            {
                "effect": "NoExecute",
                "key": "node.kubernetes.io/unreachable",
                "operator": "Exists",
                "tolerationSeconds": 300,
            },
        ]
        live_pod["priority"] = 2_000_000_000
        expected_projection = runtime._flux_pod_spec_projection(
            expected_pod,
            "expected Flux Pod",
            synthesize_system_priority=True,
        )
        live_projection = runtime._flux_pod_spec_projection(
            live_pod, "live Flux Pod"
        )
        self.assertEqual(expected_projection, live_projection)

        missing_priority = json.loads(json.dumps(live_pod))
        missing_priority.pop("priority")
        self.assertNotEqual(
            expected_projection,
            runtime._flux_pod_spec_projection(
                missing_priority, "live Flux Pod without admitted priority"
            ),
        )

        priority_drift = json.loads(json.dumps(live_pod))
        priority_drift["priority"] = 1_999_999_999
        self.assertNotEqual(
            expected_projection,
            runtime._flux_pod_spec_projection(
                priority_drift, "drifted Flux Pod"
            ),
        )

        for field, value in (
            ("args", ["--shadow-mode"]),
            ("env", [{"name": "SHADOW", "value": "1"}]),
            (
                "securityContext",
                {"allowPrivilegeEscalation": True, "runAsNonRoot": True},
            ),
        ):
            with self.subTest(container_field=field):
                drifted = json.loads(json.dumps(live_pod))
                drifted["containers"][0][field] = value
                self.assertNotEqual(
                    expected_projection,
                    runtime._flux_pod_spec_projection(
                        drifted, "drifted Flux Pod"
                    ),
                )

        service_account_drift = json.loads(json.dumps(live_pod))
        service_account_drift["serviceAccountName"] = "shadow-account"
        self.assertNotEqual(
            expected_projection,
            runtime._flux_pod_spec_projection(
                service_account_drift, "drifted Flux Pod"
            ),
        )

        volume_drift = json.loads(json.dumps(live_pod))
        volume_drift["volumes"].append(
            {"name": "shadow", "emptyDir": {"medium": "Memory"}}
        )
        self.assertNotEqual(
            expected_projection,
            runtime._flux_pod_spec_projection(
                volume_drift, "drifted Flux Pod"
            ),
        )

    def test_flux_bootstrap_contract_rejects_unexpected_document(self) -> None:
        commit = "a" * 40
        api_digest = "sha256:" + "b" * 64
        web_digest = "sha256:" + "c" * 64
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bootstrap = root / "bootstrap.yaml"
            binding = contract.render_bootstrap(
                commit,
                api_digest,
                web_digest,
                bootstrap,
            )
            bootstrap.write_text(
                bootstrap.read_text(encoding="utf-8")
                + "\n---\napiVersion: v1\nkind: ConfigMap\n"
                + "metadata:\n  name: injected\n  namespace: flux-system\n",
                encoding="utf-8",
            )
            binding["sha256"] = runtime.sha256_file(bootstrap)
            with self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "unexpected document",
            ):
                runtime._flux_bootstrap_contract(root, binding)

    def test_apply_release_requires_exact_flux_revision_and_set(self) -> None:
        source = inspect.getsource(runtime.apply_release)
        self.assertIn("_require_flux_source_revision(", source)
        self.assertIn("_require_exact_flux_revision_ready(", source)
        self.assertIn("_flux_bootstrap_contract(", source)
        self.assertIn(
            'kubectl_apply(root, str(flux_contract["bootstrap_manifest"]))',
            source,
        )
        self.assertIn(
            "_git_blob_bytes(\n        source_commit,\n        BOOTSTRAP_TEMPLATE,",
            source,
        )
        self.assertIn("render_bootstrap_from_template(", source)
        self.assertNotIn("contract.render_bootstrap(", source)
        self.assertNotIn("output.read_text", source)
        bootstrap_contract_source = inspect.getsource(
            runtime._flux_bootstrap_contract
        )
        self.assertIn(
            "_verified_snapshot_bytes(",
            bootstrap_contract_source,
        )
        self.assertNotIn(
            "bootstrap_path.read_text",
            bootstrap_contract_source,
        )
        self.assertIn('"gitrepository"', source)
        self.assertIn('"kustomizations"', source)
        self.assertNotIn('flux, "get", "kustomizations"', source)

        commit = "a" * 40
        api_digest = "sha256:" + "b" * 64
        web_digest = "sha256:" + "c" * 64
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = contract.render_bootstrap(
                commit,
                api_digest,
                web_digest,
                root / "bootstrap.yaml",
            )
            expected = runtime._flux_bootstrap_contract(root, binding)

        source_spec = json.loads(json.dumps(expected["source_spec"]))
        git_source = {
            "metadata": {"generation": 1},
            "spec": {
                **source_spec,
                "suspend": False,
                "provider": "generic",
                "timeout": "60s",
            },
            "status": {
                "artifact": {"revision": f"main@sha1:{commit}"},
                "conditions": [{
                    "type": "Ready",
                    "status": "True",
                    "observedGeneration": 1,
                }],
            },
        }
        self.assertIn(
            commit,
            runtime._require_flux_source_revision(
                git_source, commit, source_spec
            ),
        )
        for exact_revision in (commit, f"sha1:{commit}", f"main@sha1:{commit}"):
            with self.subTest(exact_revision=exact_revision):
                self.assertIn(
                    commit,
                    runtime._require_flux_source_revision(
                        {
                            **git_source,
                            "status": {
                                **git_source["status"],
                                "artifact": {"revision": exact_revision},
                            },
                        },
                        commit,
                        source_spec,
                    ),
                )

        drifted_source = json.loads(json.dumps(git_source))
        drifted_source["spec"]["url"] = "https://example.invalid/other"
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "spec drifted"):
            runtime._require_flux_source_revision(
                drifted_source, commit, source_spec
            )

        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "GitRepository"):
            runtime._require_flux_source_revision(
                {
                    **git_source,
                    "status": {
                        **git_source["status"],
                        "artifact": {"revision": "main@sha1:" + "b" * 40},
                    },
                },
                commit,
                source_spec,
            )
        for malformed in (
            f"main@sha1:{commit}00",
            f"prefix-{commit}",
            f"main@sha256:{commit}",
        ):
            with self.subTest(malformed_revision=malformed):
                with self.assertRaises(runtime.RuntimeErrorEB):
                    runtime._require_flux_source_revision(
                        {
                            **git_source,
                            "status": {
                                **git_source["status"],
                                "artifact": {"revision": malformed},
                            },
                        },
                        commit,
                        source_spec,
                    )

        for name, source_item in (
            ("suspended", {**git_source, "spec": {**git_source["spec"], "suspend": True}}),
            (
                "stale-generation",
                {
                    **git_source,
                    "metadata": {"generation": 2},
                },
            ),
        ):
            with self.subTest(source_state=name):
                with self.assertRaises(runtime.RuntimeErrorEB):
                    runtime._require_flux_source_revision(
                        source_item, commit, source_spec
                    )

        expected_specs = expected["kustomization_specs"]
        items = []
        for name in sorted(runtime.EXPECTED_FLUX_KUSTOMIZATIONS):
            live_spec = json.loads(json.dumps(expected_specs[name]))
            live_spec["suspend"] = False
            live_spec["deletionPolicy"] = "MirrorPrune"
            live_spec.setdefault("force", False)
            items.append(
                {
                    "metadata": {"name": name, "generation": 1},
                    "spec": live_spec,
                    "status": {
                        "conditions": [{
                            "type": "Ready",
                            "status": "True",
                            "observedGeneration": 1,
                        }],
                        "lastAppliedRevision": f"main@sha1:{commit}",
                    },
                }
            )
        readback = runtime._require_exact_flux_revision_ready(
            items, commit, expected_specs
        )
        self.assertEqual(set(readback), runtime.EXPECTED_FLUX_KUSTOMIZATIONS)
        self.assertTrue(
            all("spec_sha256" in value for value in readback.values())
        )

        drifted_items = json.loads(json.dumps(items))
        app_item = next(
            item
            for item in drifted_items
            if item["metadata"]["name"] == "commonthing-experiment-b-app"
        )
        app_item["spec"]["path"] = "./platform/clusters/experiment-b/namespaces"
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "spec drifted"):
            runtime._require_exact_flux_revision_ready(
                drifted_items, commit, expected_specs
            )

        drifted_items = json.loads(json.dumps(items))
        app_item = next(
            item
            for item in drifted_items
            if item["metadata"]["name"] == "commonthing-experiment-b-app"
        )
        app_item["spec"]["prune"] = False
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "spec drifted"):
            runtime._require_exact_flux_revision_ready(
                drifted_items, commit, expected_specs
            )

        stale_items = json.loads(json.dumps(items))
        stale_items[0]["status"]["lastAppliedRevision"] = "main@sha1:" + "b" * 40
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "exact-revision Ready"):
            runtime._require_exact_flux_revision_ready(
                stale_items, commit, expected_specs
            )

        malformed_items = json.loads(json.dumps(items))
        malformed_items[0]["status"]["lastAppliedRevision"] = f"main@sha1:{commit}00"
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "exact-revision Ready"):
            runtime._require_exact_flux_revision_ready(
                malformed_items, commit, expected_specs
            )

        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "set mismatch"):
            runtime._require_exact_flux_revision_ready(
                items[:-1], commit, expected_specs
            )

        suspended_items = json.loads(json.dumps(items))
        suspended_items[0]["spec"]["suspend"] = True
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "suspended"):
            runtime._require_exact_flux_revision_ready(
                suspended_items, commit, expected_specs
            )

        stale_generation_items = json.loads(json.dumps(items))
        stale_generation_items[0]["metadata"]["generation"] = 2
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "current generation"):
            runtime._require_exact_flux_revision_ready(
                stale_generation_items, commit, expected_specs
            )


    def test_apply_release_requires_requested_live_artifacts(self) -> None:
        source = inspect.getsource(runtime.apply_release)
        self.assertIn("_require_requested_release_artifacts(", source)
        self.assertIn("MIGRATION_JOB_NAME", source)
        self.assertIn('"migration_pods"', source)
        self.assertLess(
            source.index("_require_requested_release_artifacts("),
            source.index("receipt = {"),
        )

        api_digest = "sha256:" + "b" * 64
        web_digest = "sha256:" + "c" * 64
        api = {
            "metadata": {"generation": 1},
            "spec": {
                "replicas": 1,
                "template": {"spec": {"containers": [
                    {
                        "name": "api",
                        "image": "ghcr.io/heimgewebe/commonthing-api@" + api_digest,
                    },
                    {
                        "name": "search-worker",
                        "image": "ghcr.io/heimgewebe/commonthing-api@" + api_digest,
                    },
                ]}},
            },
            "status": {
                "observedGeneration": 1,
                "replicas": 1,
                "updatedReplicas": 1,
                "readyReplicas": 1,
                "availableReplicas": 1,
                "unavailableReplicas": 0,
                "conditions": [{"type": "Available", "status": "True"}],
            },
        }
        web = {
            "metadata": {"generation": 1},
            "spec": {
                "replicas": 2,
                "template": {"spec": {"containers": [{
                    "name": "web",
                    "image": "ghcr.io/heimgewebe/commonthing-web@" + web_digest,
                }]}},
            },
            "status": {
                "observedGeneration": 1,
                "replicas": 2,
                "updatedReplicas": 2,
                "readyReplicas": 2,
                "availableReplicas": 2,
                "unavailableReplicas": 0,
                "conditions": [{"type": "Available", "status": "True"}],
            },
        }
        migration = {
            "spec": {"template": {"spec": {"containers": [{
                "name": "migration",
                "image": "ghcr.io/heimgewebe/commonthing-api@" + api_digest,
            }]}}},
            "status": {
                "succeeded": 1,
                "conditions": [{"type": "Complete", "status": "True"}],
            },
        }
        migration_readback = {
            "contract_sha256": "a" * 64,
            "pod_contract_sha256": "b" * 64,
            "pod_names": ["migration-0"],
            "succeeded_pods": 1,
            "canonical": True,
        }
        with mock.patch.object(
            runtime,
            "_require_migration_job_runtime_contract",
            return_value=migration_readback,
        ) as migration_contract:
            observed = runtime._require_requested_release_artifacts(
                Path("/tmp"),
                api,
                web,
                migration,
                [],
                api_digest,
                web_digest,
                1,
                2,
            )
        self.assertTrue(observed["migration_complete"])
        self.assertEqual(observed["migration"], migration_readback)
        migration_contract.assert_called_once()

        stale_api = json.loads(json.dumps(api))
        stale_api["spec"]["template"]["spec"]["containers"][0]["image"] = (
            "ghcr.io/heimgewebe/commonthing-api@sha256:" + "d" * 64
        )
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "live API image"):
            runtime._require_requested_release_artifacts(
                Path("/tmp"),
                stale_api,
                web,
                migration,
                [],
                api_digest,
                web_digest,
                1,
                2,
            )

        stale_migration = json.loads(json.dumps(migration))
        stale_migration["spec"]["template"]["spec"]["containers"][0]["image"] = (
            "ghcr.io/heimgewebe/commonthing-api@sha256:" + "d" * 64
        )
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "migration image"):
            runtime._require_requested_release_artifacts(
                Path("/tmp"),
                api,
                web,
                stale_migration,
                [],
                api_digest,
                web_digest,
                1,
                2,
            )

        incomplete = json.loads(json.dumps(migration))
        incomplete["status"] = {"succeeded": 0, "conditions": []}
        with (
            mock.patch.object(
                runtime,
                "_require_migration_job_runtime_contract",
                side_effect=runtime.RuntimeErrorEB(
                    "Experiment-B migration Job is not canonically complete"
                ),
            ),
            self.assertRaisesRegex(
                runtime.RuntimeErrorEB, "not canonically complete"
            ),
        ):
            runtime._require_requested_release_artifacts(
                Path("/tmp"),
                api,
                web,
                incomplete,
                [],
                api_digest,
                web_digest,
                1,
                2,
            )

    def test_recovery_attempt_invalidates_post_recovery_evidence(self) -> None:
        source = inspect.getsource(runtime.recovery_proof)
        self.assertIn(
            "_invalidate_receipts(root, RECOVERY_ATTEMPT_INVALIDATES)",
            source,
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipts = root / "receipts"
            receipts.mkdir()
            for name in runtime.RECOVERY_ATTEMPT_INVALIDATES:
                (receipts / name).write_text(
                    json.dumps({"schema_version": 1, "status": "stale"}) + "\n",
                    encoding="utf-8",
                )

            runtime._invalidate_receipts(
                root,
                runtime.RECOVERY_ATTEMPT_INVALIDATES,
            )

            for name in runtime.RECOVERY_ATTEMPT_INVALIDATES:
                self.assertFalse((receipts / name).exists(), name)

    def test_derived_portability_is_invalidated_before_rerun_work(self) -> None:
        seed = inspect.getsource(runtime.seed_t048_fixture)
        self.assertLess(
            seed.index("_invalidate_receipts(root, FIXTURE_ATTEMPT_INVALIDATES)"),
            seed.index("_performance_modules(source_commit)"),
        )

        portability = inspect.getsource(runtime.portability_report)
        self.assertLess(
            portability.index(
                "_invalidate_receipts(root, PORTABILITY_DERIVED_RECEIPTS)"
            ),
            portability.index("recovery_failed_receipt"),
        )

    def test_live_checks_start_attempt_before_live_work(self) -> None:
        release = inspect.getsource(runtime.apply_release)
        self.assertLess(
            release.index("_begin_release_attempt("),
            release.index(
                'kubectl_apply(root, str(flux_contract["bootstrap_manifest"]))'
            ),
        )
        self.assertIn("_complete_live_check_attempt(", release)

        semantic = inspect.getsource(runtime.semantic_activate)
        self.assertLess(
            semantic.index("_begin_live_check_attempt("),
            semantic.index("kubectl_apply(root, temporary_egress)"),
        )
        self.assertIn("_complete_live_check_attempt(", semantic)

        functional = inspect.getsource(runtime.functional_readback)
        first_functional_target = functional.index(
            "_require_kubernetes_target_binding"
        )
        endpoint_guard = functional.index(
            "with _guard_functional_service_endpoints("
        )
        gateway_readback = functional.index(
            "_gateway_data_plane_readback("
        )
        nats_binding = functional.index(
            "nats_binding_before = _require_nats_runtime_binding("
        )
        jetstream_readback = functional.index("_jetstream_signature(")
        second_functional_target = functional.index(
            "_require_kubernetes_target_binding",
            first_functional_target + 1,
        )
        self.assertLess(
            functional.index("_begin_live_check_attempt("),
            first_functional_target,
        )
        self.assertLess(first_functional_target, endpoint_guard)
        self.assertLess(endpoint_guard, gateway_readback)
        self.assertLess(gateway_readback, nats_binding)
        self.assertLess(nats_binding, jetstream_readback)
        self.assertLess(jetstream_readback, second_functional_target)
        self.assertGreaterEqual(
            functional.count("_require_nats_runtime_binding("),
            2,
        )
        self.assertIn("nats_binding=nats_binding_before", functional)
        self.assertIn("kubernetes_target_sha256", functional)
        self.assertIn("_complete_live_check_attempt(", functional)

        status = inspect.getsource(runtime.status)
        self.assertLess(
            status.index("_begin_live_check_attempt("),
            status.index('run([kubectl, "get", "nodes", "-o", "json"]'),
        )
        self.assertIn("_complete_live_check_attempt(", status)
        self.assertGreater(
            status.index("_require_live_runtime_contract("),
            status.index("_final_recovery_state_readback(root, source_commit)"),
        )
        self.assertLess(
            status.index("_require_live_runtime_contract("),
            status.index("result = {"),
        )

        for function, begin_marker in (
            (runtime.apply_release, "_begin_release_attempt("),
            (runtime.semantic_activate, "_begin_live_check_attempt("),
            (runtime.functional_readback, "_begin_live_check_attempt("),
            (runtime.t048_load_proof, "_begin_live_check_attempt("),
            (runtime.status, "_begin_live_check_attempt("),
        ):
            source = inspect.getsource(function)
            with self.subTest(protected_main_order=function.__name__):
                self.assertLess(
                    source.index(begin_marker),
                    source.index("_current_protected_main_commit()"),
                )

        recovery = inspect.getsource(runtime.recovery_proof)
        self.assertLess(
            recovery.index("_invalidate_receipts(root, RECOVERY_ATTEMPT_INVALIDATES)"),
            recovery.index("_current_protected_main_commit()"),
        )

    def test_experiment_b_lifecycle_lock_is_global_and_teardown_safe(self) -> None:
        for function in (
            runtime.prepare,
            runtime.create_vm,
            runtime.install_k3s,
            runtime.install_platform,
            runtime.inject_secrets,
            runtime.apply_release,
            runtime.semantic_activate,
            runtime.status,
            runtime.teardown,
            runtime.seed_t048_fixture,
            runtime.t048_load_proof,
            runtime.functional_readback,
            runtime.recovery_proof,
            runtime.portability_report,
        ):
            with self.subTest(function=function.__name__):
                self.assertTrue(hasattr(function, "__wrapped__"))

        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            lock_path = parent / "experiment-b.lifecycle.lock"
            first_root = parent / "experiment-b" / "first"
            second_root = parent / "experiment-b" / "second"
            first_root.mkdir(parents=True)
            second_root.mkdir(parents=True)
            flags = (
                runtime.os.O_RDWR
                | runtime.os.O_CREAT
                | int(getattr(runtime.os, "O_CLOEXEC", 0))
                | int(getattr(runtime.os, "O_NOFOLLOW", 0))
            )
            with mock.patch.object(
                runtime,
                "EXPERIMENT_B_LIFECYCLE_LOCK",
                lock_path,
            ):
                lock_fd = runtime.os.open(lock_path, flags, 0o600)
                runtime.fcntl.flock(
                    lock_fd,
                    runtime.fcntl.LOCK_EX | runtime.fcntl.LOCK_NB,
                )
                try:
                    shutil.rmtree(first_root)

                    @runtime._serialize_experiment_b_lifecycle
                    def second_operation(_root: Path) -> None:
                        raise AssertionError(
                            "second Experiment-B lifecycle operation must not start"
                        )

                    with self.assertRaisesRegex(
                        runtime.RuntimeErrorEB,
                        "Experiment-B lifecycle is already running",
                    ):
                        second_operation(second_root)
                    self.assertTrue(lock_path.exists())
                    self.assertFalse(first_root.exists())
                finally:
                    runtime.fcntl.flock(
                        lock_fd,
                        runtime.fcntl.LOCK_UN,
                    )
                    runtime.os.close(lock_fd)

    def test_state_root_creation_waits_for_global_lifecycle_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            allowed_root = parent / "experiment-b"
            target = allowed_root / "blocked"
            lock_path = parent / "experiment-b.lifecycle.lock"
            flags = (
                runtime.os.O_RDWR
                | runtime.os.O_CREAT
                | int(getattr(runtime.os, "O_CLOEXEC", 0))
                | int(getattr(runtime.os, "O_NOFOLLOW", 0))
            )
            with (
                mock.patch.object(runtime, "DEFAULT_STATE_ROOT", allowed_root),
                mock.patch.object(runtime, "EXPERIMENT_B_LIFECYCLE_LOCK", lock_path),
            ):
                root = runtime.state_root(str(target))
                self.assertEqual(root, target.resolve())
                self.assertFalse(target.exists())

                lock_fd = runtime.os.open(lock_path, flags, 0o600)
                runtime.fcntl.flock(
                    lock_fd,
                    runtime.fcntl.LOCK_EX | runtime.fcntl.LOCK_NB,
                )

                @runtime._serialize_experiment_b_lifecycle
                def operation(operation_root: Path) -> None:
                    self.assertTrue(operation_root.is_dir())

                try:
                    with self.assertRaisesRegex(
                        runtime.RuntimeErrorEB,
                        "Experiment-B lifecycle is already running",
                    ):
                        operation(root)
                    self.assertFalse(target.exists())
                finally:
                    runtime.fcntl.flock(lock_fd, runtime.fcntl.LOCK_UN)
                    runtime.os.close(lock_fd)

                operation(root)
                self.assertTrue(target.is_dir())
                self.assertEqual(target.stat().st_mode & 0o777, 0o700)

    def test_dirty_rerun_invalidates_functional_success_before_binding_failure(self) -> None:
        commit = "a" * 40
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipts = root / "receipts"
            receipts.mkdir()
            success = receipts / "functional-readback.json"
            success.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "pass",
                        "source_commit": commit,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            portability = receipts / "portability.json"
            portability.write_text(
                json.dumps({"schema_version": 1, "status": "pass"}) + "\n",
                encoding="utf-8",
            )
            with mock.patch.object(
                runtime,
                "_current_protected_main_commit",
                side_effect=runtime.RuntimeErrorEB("dirty checkout"),
            ):
                with self.assertRaisesRegex(runtime.RuntimeErrorEB, "dirty checkout"):
                    runtime.functional_readback(root, commit)

            self.assertFalse(success.exists())
            self.assertFalse(portability.exists())
            attempt = json.loads(
                (receipts / "functional-readback-attempt.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(attempt["status"], "running")
            self.assertEqual(attempt["source_commit"], commit)

    def test_functional_readback_rejects_target_change_during_live_work(
        self,
    ) -> None:
        commit = "a" * 40
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "receipts").mkdir()
            target = mock.Mock(
                side_effect=[
                    (
                        {"kubeconfig_sha256": "1" * 64},
                        "192.168.122.10",
                        "https://192.168.122.10:6443",
                    ),
                    (
                        {"kubeconfig_sha256": "2" * 64},
                        "192.168.122.10",
                        "https://192.168.122.10:6443",
                    ),
                ]
            )
            with (
                mock.patch.object(
                    runtime,
                    "_current_protected_main_commit",
                    return_value=commit,
                ),
                mock.patch.object(
                    runtime,
                    "_require_kubernetes_target_binding",
                    target,
                ),
                mock.patch.object(
                    runtime,
                    "_bound_kube_env",
                    return_value=mock.MagicMock(),
                ),
                mock.patch.object(
                    runtime,
                    "_functional_serving_runtime_binding",
                    return_value={"stable": True},
                ),
                mock.patch.object(
                    runtime,
                    "_guard_functional_service_endpoints",
                    return_value=mock.MagicMock(),
                ),
                mock.patch.object(
                    runtime,
                    "_require_nats_runtime_binding",
                    return_value={
                        "container_id": "containerd://" + "3" * 64,
                        "contract_sha256": "4" * 64,
                        "pod_contract_sha256": "5" * 64,
                        "runtime_image_ids_sha256": "6" * 64,
                    },
                ),
                mock.patch.object(
                    runtime,
                    "_gateway_data_plane_readback",
                    return_value={
                        "gateway": "http://192.0.2.10",
                        "checks": {"fixture": True},
                    },
                ),
                mock.patch.object(
                    runtime,
                    "_jetstream_signature",
                    return_value={"messages": 1},
                ),
            ):
                with self.assertRaisesRegex(
                    runtime.RuntimeErrorEB,
                    "target identity changed",
                ):
                    runtime.functional_readback(root, commit)

            self.assertEqual(target.call_count, 2)
            self.assertFalse(
                (root / "receipts/functional-readback.json").exists()
            )

    def test_status_requires_exact_flux_kustomization_set(self) -> None:
        complete = {
            name: {"ready": True}
            for name in runtime.EXPECTED_FLUX_KUSTOMIZATIONS
        }
        runtime._require_exact_flux_kustomizations(complete)

        for name in runtime.EXPECTED_FLUX_KUSTOMIZATIONS:
            with self.subTest(missing=name):
                incomplete = dict(complete)
                incomplete.pop(name)
                with self.assertRaises(runtime.RuntimeErrorEB):
                    runtime._require_exact_flux_kustomizations(incomplete)

        unexpected = dict(complete)
        unexpected["commonthing-experiment-b-shadow"] = {"ready": True}
        with self.assertRaises(runtime.RuntimeErrorEB):
            runtime._require_exact_flux_kustomizations(unexpected)

    def test_deployment_availability_requires_current_ready_rollout(self) -> None:
        healthy = {
            "metadata": {"generation": 7},
            "spec": {"replicas": 1},
            "status": {
                "observedGeneration": 7,
                "replicas": 1,
                "updatedReplicas": 1,
                "readyReplicas": 1,
                "availableReplicas": 1,
                "unavailableReplicas": 0,
                "conditions": [{"type": "Available", "status": "True"}],
            },
        }
        snapshot = runtime._deployment_availability_snapshot(
            healthy, "weltgewebe-api", 1
        )
        self.assertTrue(snapshot["available"])

        failure_paths = (
            ("observedGeneration", 6),
            ("replicas", 2),
            ("updatedReplicas", 0),
            ("updatedReplicas", 2),
            ("readyReplicas", 0),
            ("readyReplicas", 2),
            ("availableReplicas", 0),
            ("availableReplicas", 2),
            ("unavailableReplicas", 1),
        )
        for field, value in failure_paths:
            with self.subTest(field=field):
                broken = json.loads(json.dumps(healthy))
                broken["status"][field] = value
                with self.assertRaises(runtime.RuntimeErrorEB):
                    runtime._deployment_availability_snapshot(
                        broken, "weltgewebe-api", 1
                    )

        no_available_condition = json.loads(json.dumps(healthy))
        no_available_condition["status"]["conditions"] = [
            {"type": "Available", "status": "False"}
        ]
        with self.assertRaises(runtime.RuntimeErrorEB):
            runtime._deployment_availability_snapshot(
                no_available_condition, "weltgewebe-api", 1
            )

        scaled_live_spec = json.loads(json.dumps(healthy))
        scaled_live_spec["spec"]["replicas"] = 2
        for field in (
            "replicas",
            "updatedReplicas",
            "readyReplicas",
            "availableReplicas",
        ):
            scaled_live_spec["status"][field] = 2
        with self.assertRaises(runtime.RuntimeErrorEB):
            runtime._deployment_availability_snapshot(
                scaled_live_spec, "weltgewebe-api", 1
            )

    def test_t048_rerun_invalidates_stale_success_before_early_failure(self) -> None:
        commit = "a" * 40
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipts = root / "receipts"
            receipts.mkdir()
            stale = receipts / "t048-load.json"
            stale.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "pass",
                        "source_commit": commit,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with (
                mock.patch.object(
                    runtime,
                    "_current_protected_main_commit",
                    return_value=commit,
                ),
                mock.patch.object(
                    runtime,
                    "_require_kubernetes_target_binding",
                    return_value=(
                        {"kubeconfig_sha256": "1" * 64},
                        "192.168.122.10",
                        "https://192.168.122.10:6443",
                    ),
                ),
                mock.patch.object(
                    runtime,
                    "_bound_kube_env",
                    return_value=mock.MagicMock(),
                ),
                mock.patch.object(
                    runtime,
                    "_require_t048_postgres_runtime_binding",
                    return_value={
                        "images_sha256": "2" * 64,
                        "runtime_image_ids_sha256": "3" * 64,
                    },
                ),
                mock.patch.object(
                    runtime,
                    "_require_t048_postgres_service_binding",
                    return_value={
                        "canonical": True,
                        "service_resource_version": "101",
                        "endpoint_list_resource_version": "202",
                    },
                ),
                mock.patch.object(
                    runtime,
                    "seed_t048_fixture",
                    side_effect=runtime.RuntimeErrorEB("fixture failed"),
                ),
            ):
                with self.assertRaises(runtime.RuntimeErrorEB):
                    runtime.t048_load_proof(root, commit)

            self.assertFalse(stale.exists())
            attempt = json.loads(
                (receipts / "t048-load-attempt.json").read_text(encoding="utf-8")
            )
            self.assertEqual(attempt["status"], "running")
            self.assertEqual(attempt["source_commit"], commit)

    def test_t048_primes_search_metric_before_baseline_scrape(self) -> None:
        load_source = inspect.getsource(runtime.t048_load_proof)
        self.assertIn("_prime_t048_search_metric(", load_source)
        self.assertLess(
            load_source.index("_prime_t048_search_metric("),
            load_source.index('_http_read(f"{base_url}/metrics")'),
        )

    def test_t048_search_metric_prime_binds_query_and_requires_success(self) -> None:
        with mock.patch.object(
            runtime,
            "_http_read",
            return_value=(200, b"{}", 1.0),
        ) as http_read:
            runtime._prime_t048_search_metric(
                "http://127.0.0.1:1234",
                "welt gewebe",
            )
        http_read.assert_called_once_with(
            "http://127.0.0.1:1234/search?q=welt+gewebe&limit=5"
        )

        with mock.patch.object(
            runtime,
            "_http_read",
            return_value=(503, b"unavailable", 1.0),
        ):
            with self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "T048 /search warm-up failed",
            ):
                runtime._prime_t048_search_metric(
                    "http://127.0.0.1:1234",
                    "welt gewebe",
                )

    def test_http_read_disables_ambient_proxy(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        response.status = 200
        response.read.return_value = b"ok"
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(
            runtime.urllib.request,
            "build_opener",
            return_value=opener,
        ) as build_opener:
            status, body, _elapsed = runtime._http_read(
                "http://127.0.0.1:8080/health",
                timeout=2,
            )
        self.assertEqual(status, 200)
        self.assertEqual(body, b"ok")
        handler = build_opener.call_args.args[0]
        self.assertIsInstance(handler, runtime.urllib.request.ProxyHandler)
        self.assertEqual(handler.proxies, {})
        opener.open.assert_called_once()

    def test_t048_sampler_failure_terminates_and_reaps_k6(self) -> None:
        load = mock.Mock()
        load.poll.return_value = None
        load.wait.return_value = -15
        resource_samples = [{"sample": "initial"}]
        db_samples = [1]
        service_binding = {"canonical": True}
        with (
            mock.patch.object(runtime.time, "sleep"),
            mock.patch.object(
                runtime,
                "_require_t048_postgres_service_binding",
                return_value=service_binding,
            ),
            mock.patch.object(
                runtime,
                "_sample_api_cgroup",
                side_effect=runtime.RuntimeErrorEB("sampler failed"),
            ),
        ):
            with self.assertRaises(runtime.RuntimeErrorEB):
                runtime._sample_t048_load(
                    Path("/tmp"),
                    "a" * 40,
                    "api-pod",
                    {
                        "container_id": "containerd://" + "b" * 64,
                        "contract_sha256": "c" * 64,
                        "pod_contract_sha256": "d" * 64,
                        "runtime_image_ids_sha256": "e" * 64,
                    },
                    service_binding,
                    ("proof_user", "proof_database"),
                    load,
                    resource_samples,
                    db_samples,
                    30,
                )
        load.terminate.assert_called_once_with()
        load.wait.assert_called_once_with(timeout=10)
        load.kill.assert_not_called()

    def test_t048_sampler_deadline_terminates_and_reaps_k6(self) -> None:
        load = mock.Mock()
        load.poll.return_value = None
        load.wait.return_value = -15
        resource_samples = [{"sample": "initial"}]
        db_samples = [1]
        service_binding = {"canonical": True}
        with (
            mock.patch.object(runtime.time, "sleep"),
            mock.patch.object(
                runtime,
                "_require_t048_postgres_service_binding",
                return_value=service_binding,
            ),
            mock.patch.object(
                runtime.time,
                "monotonic",
                side_effect=[10.0, 16.0],
            ),
        ):
            with self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "exceeded bounded runtime",
            ):
                runtime._sample_t048_load(
                    Path("/tmp"),
                    "a" * 40,
                    "api-pod",
                    {
                        "container_id": "containerd://" + "b" * 64,
                        "contract_sha256": "c" * 64,
                        "pod_contract_sha256": "d" * 64,
                        "runtime_image_ids_sha256": "e" * 64,
                    },
                    service_binding,
                    ("proof_user", "proof_database"),
                    load,
                    resource_samples,
                    db_samples,
                    5,
                )
        load.terminate.assert_called_once_with()
        load.wait.assert_called_once_with(timeout=10)
        load.kill.assert_not_called()

    def test_t048_sampler_rejects_postgres_service_endpoint_drift_during_load(self) -> None:
        load = mock.Mock()
        load.poll.side_effect = [None, None]
        load.wait.return_value = -15
        resource_samples = [{"sample": "initial"}]
        db_samples = [1]
        postgres_binding = {
            "pod_name": "postgres-0",
            "pod_uid": "postgres-pod-uid",
            "pod_ip": "10.42.1.10",
            "container_id": "containerd://" + "b" * 64,
            "contract_sha256": "c" * 64,
            "pod_contract_sha256": "d" * 64,
            "runtime_image_ids_sha256": "e" * 64,
        }
        canonical = {
            "service_uid": "postgres-service-uid",
            "pod_name": "postgres-0",
            "pod_uid": "postgres-pod-uid",
            "pod_ip": "10.42.1.10",
            "service_spec_sha256": "f" * 64,
            "endpoint_sha256": "1" * 64,
        }
        with (
            mock.patch.object(runtime.time, "sleep"),
            mock.patch.object(
                runtime,
                "_sample_api_cgroup",
                return_value={"sample": "next"},
            ),
            mock.patch.object(
                runtime,
                "_database_connection_count",
                return_value=1,
            ),
            mock.patch.object(
                runtime,
                "_require_t048_postgres_service_binding",
                create=True,
                side_effect=[
                    canonical,
                    runtime.RuntimeErrorEB(
                        "PostgreSQL Service endpoint target drifted"
                    ),
                ],
            ) as service_binding,
            self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "PostgreSQL Service endpoint target drifted",
            ),
        ):
            runtime._sample_t048_load(
                Path("/tmp"),
                "a" * 40,
                "api-pod",
                postgres_binding,
                canonical,
                ("proof_user", "proof_database"),
                load,
                resource_samples,
                db_samples,
                30,
            )

        self.assertGreaterEqual(service_binding.call_count, 2)
        load.terminate.assert_called_once_with()
        load.wait.assert_called_once_with(timeout=10)
        load.kill.assert_not_called()

    def test_t048_postgres_service_guard_rejects_transient_dependency_changes(
        self,
    ) -> None:
        binding = {
            "service_resource_version": "101",
            "endpoint_list_resource_version": "202",
        }
        changed = json.dumps(
            {
                "type": "MODIFIED",
                "object": {"metadata": {"resourceVersion": "303"}},
            }
        )
        calls: list[list[str]] = []

        def watched(
            argv: list[str],
            **_kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=changed + "\n",
                stderr="",
            )

        with (
            mock.patch.object(
                runtime,
                "toolchain",
                return_value={"tools": {"kubectl": "/kubectl"}},
            ),
            mock.patch.object(runtime, "kube_env", return_value={}),
            mock.patch.object(runtime, "run", side_effect=watched),
            self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "PostgreSQL serving dependency changed during T048 load",
            ),
        ):
            with runtime._guard_t048_postgres_service_endpoints(
                Path("/tmp"),
                binding,
            ):
                pass

        self.assertEqual(len(calls), 1)

    def test_k6_summary_output_channel_is_pathless_and_sealed(self) -> None:
        canonical = {"canonical": True}
        encoded = json.dumps(canonical, separators=(",", ":")).encode("utf-8")
        with runtime._k6_summary_output_channel() as summary_fd:
            target = runtime.os.readlink(f"/proc/self/fd/{summary_fd}")
            self.assertIn("memfd:commonthing-experiment-b-k6-summary", target)
            runtime.os.write(
                summary_fd,
                b"k6 progress\n"
                + runtime.K6_SUMMARY_STDOUT_MARKER.encode("utf-8")
                + encoded
                + b"\n",
            )
            self.assertEqual(
                json.loads(runtime._seal_k6_summary_output(summary_fd)),
                canonical,
            )
            with self.assertRaises(OSError):
                runtime.os.write(summary_fd, b"forged")

        with runtime._k6_summary_output_channel() as duplicate_fd:
            marker = runtime.K6_SUMMARY_STDOUT_MARKER.encode("utf-8")
            runtime.os.write(duplicate_fd, marker + b"{}" + marker + b"{}")
            with self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "no unique authority marker",
            ):
                runtime._seal_k6_summary_output(duplicate_fd)

    def test_jetstream_signature_tracks_durable_consumer_continuity_only(self) -> None:
        monitoring = {
            "streams": 1,
            "messages": 3,
            "bytes": 99,
            "account_details": [
                {
                    "name": "$G",
                    "stream_detail": [
                        {
                            "name": "commonthing-domain-events-v1",
                            "state": {
                                "messages": 3,
                                "bytes": 99,
                                "first_seq": 1,
                                "last_seq": 3,
                                "consumer_count": 2,
                            },
                            "consumer_detail": [
                                {
                                    "stream_name": "commonthing-domain-events-v1",
                                    "name": "weltgewebe-api-domain-receipts-v1",
                                    "config": {
                                        "durable_name": "weltgewebe-api-domain-receipts-v1",
                                        "ack_policy": "explicit",
                                        "filter_subject": "weltgewebe.domain.v1",
                                    },
                                    "delivered": {
                                        "consumer_seq": 3,
                                        "stream_seq": 3,
                                        "last_active": "volatile",
                                    },
                                    "ack_floor": {
                                        "consumer_seq": 2,
                                        "stream_seq": 2,
                                        "last_active": "volatile",
                                    },
                                    "num_ack_pending": 1,
                                    "num_redelivered": 0,
                                    "num_waiting": 7,
                                    "num_pending": 0,
                                    "ts": "volatile",
                                },
                                {
                                    "stream_name": "commonthing-domain-events-v1",
                                    "name": "ephemeral-client",
                                    "config": {"ack_policy": "explicit"},
                                    "delivered": {"consumer_seq": 0, "stream_seq": 3},
                                    "ack_floor": {"consumer_seq": 0, "stream_seq": 0},
                                    "num_ack_pending": 0,
                                    "num_redelivered": 0,
                                    "num_waiting": 1,
                                    "num_pending": 3,
                                },
                            ],
                        }
                    ],
                }
            ],
        }
        signature = runtime._jetstream_signature_from_monitoring(monitoring)
        self.assertEqual(signature["durable_consumers"], 1)
        durable = signature["stream_detail"][0]["durable_consumers"]
        self.assertEqual(len(durable), 1)
        self.assertNotIn("num_waiting", durable[0])
        self.assertNotIn("last_active", durable[0]["delivered"])
        self.assertNotIn("ts", durable[0])

        changed = json.loads(json.dumps(monitoring))
        changed["account_details"][0]["stream_detail"][0]["consumer_detail"][0][
            "ack_floor"
        ]["stream_seq"] = 1
        self.assertNotEqual(
            signature,
            runtime._jetstream_signature_from_monitoring(changed),
        )

    def test_jetstream_message_store_digest_binds_message_block_bytes(self) -> None:
        first = runtime._nats_message_store_sha256_from_output(
            f"{'a' * 64}  /data/jetstream/$G/streams/events/msgs/1.blk\n"
            f"{'b' * 64}  /data/jetstream/$G/streams/events/msgs/2.blk\n"
        )
        reordered = runtime._nats_message_store_sha256_from_output(
            f"{'b' * 64}  /data/jetstream/$G/streams/events/msgs/2.blk\n"
            f"{'a' * 64}  /data/jetstream/$G/streams/events/msgs/1.blk\n"
        )
        changed = runtime._nats_message_store_sha256_from_output(
            f"{'a' * 64}  /data/jetstream/$G/streams/events/msgs/1.blk\n"
            f"{'c' * 64}  /data/jetstream/$G/streams/events/msgs/2.blk\n"
        )
        self.assertEqual(first, reordered)
        self.assertNotEqual(first, changed)

        source = inspect.getsource(runtime._jetstream_signature)
        self.assertEqual(
            source.count("_jetstream_monitoring_signature("),
            2,
        )
        self.assertIn("_nats_message_store_sha256(", source)
        self.assertIn("source_commit=source_commit", source)
        self.assertIn("nats_binding=nats_binding", source)
        self.assertIn("state changed while hashing", source)

    def test_event_pipeline_quiescence_requires_stable_full_drain(self) -> None:
        drained = {
            "database": {
                "unpublished_nonquarantined": 0,
                "published_without_receipt": 0,
            },
            "jetstream": {
                "stream_detail": [
                    {
                        "durable_consumers": [
                            {
                                "name": runtime.DOMAIN_EVENT_CONSUMER,
                                "num_pending": 0,
                                "num_ack_pending": 0,
                            }
                        ]
                    }
                ]
            },
        }
        pending = json.loads(json.dumps(drained))
        pending["database"]["unpublished_nonquarantined"] = 1
        with (
            mock.patch.object(
                runtime,
                "_event_pipeline_quiescence_sample",
                side_effect=[pending, drained, drained],
            ) as sample,
            mock.patch.object(runtime.time, "monotonic", return_value=0.0),
            mock.patch.object(runtime.time, "sleep") as sleep,
        ):
            observed = runtime._wait_event_pipeline_quiescent(
                Path("/tmp/experiment-b-event-drain"),
                source_commit="a" * 40,
                database_identity=("user", "database"),
                timeout_seconds=10,
            )
        self.assertEqual(observed, drained)
        self.assertEqual(sample.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

        ack_pending = json.loads(json.dumps(drained))
        ack_pending["jetstream"]["stream_detail"][0]["durable_consumers"][0][
            "num_ack_pending"
        ] = 1
        self.assertFalse(
            runtime._event_pipeline_snapshot_is_drained(ack_pending)
        )

    def test_recovery_drains_pipeline_before_and_after_restore(self) -> None:
        source = inspect.getsource(runtime.recovery_proof)
        flux_suspend = source.index(
            '_flux_suspend(root, "commonthing-experiment-b-data")'
        )
        pre_drain = source.index(
            "_wait_event_pipeline_quiescent(",
            flux_suspend,
        )
        api_scale = source.index(
            '_scale_deployment(root, APP_NAMESPACE, "weltgewebe-api", 0)'
        )
        frozen = source.index(
            "frozen_pipeline = _event_pipeline_quiescence_sample(",
            api_scale,
        )
        before_db = source.index("before_db = _database_signature(", frozen)
        after_db = source.index("after_db = _database_signature(", before_db)
        restored_continuity = source.index(
            '"recovery restored continuity"',
            after_db,
        )
        app_resume = source.index(
            '_flux_resume(root, "commonthing-experiment-b-app")',
            restored_continuity,
        )
        post_drain = source.index(
            "_wait_event_pipeline_quiescent(",
            app_resume,
        )
        self.assertLess(flux_suspend, pre_drain)
        self.assertLess(pre_drain, api_scale)
        self.assertLess(api_scale, frozen)
        self.assertLess(frozen, before_db)
        self.assertLess(before_db, after_db)
        self.assertLess(after_db, restored_continuity)
        self.assertLess(restored_continuity, app_resume)
        self.assertLess(app_resume, post_drain)

    def test_wait_deployment_process_timeout_exceeds_rollout_timeout(self) -> None:
        root = Path("/tmp/unused-experiment-b-root")
        with mock.patch.object(runtime, "_kubectl") as kubectl:
            runtime._wait_deployment(
                root,
                runtime.APP_NAMESPACE,
                "weltgewebe-api",
                480,
            )

        call_args = kubectl.call_args
        self.assertIsNotNone(call_args)
        self.assertIn("--timeout=480s", call_args.args[1])
        self.assertGreater(call_args.kwargs["timeout"], 480)

    def test_recovery_rto_includes_application_rollout(self) -> None:
        source = inspect.getsource(runtime.recovery_proof)
        api_wait = source.index(
            '_wait_deployment(root, APP_NAMESPACE, "weltgewebe-api", 480)'
        )
        web_wait = source.index(
            '_wait_deployment(root, APP_NAMESPACE, "weltgewebe-web", 300)'
        )
        rto = source.index("rto_seconds = time.monotonic() - destructive_started")
        self.assertLess(api_wait, rto)
        self.assertLess(web_wait, rto)

    def test_protected_main_binding_fails_closed_on_revision_drift(self) -> None:
        commit = "a" * 40
        clean_status = mock.Mock(stdout="")
        dirty_status = mock.Mock(
            stdout=" M scripts/platform/experiment_b_runtime.py\n"
        )
        with (
            mock.patch.object(runtime, "git_head", return_value=commit),
            mock.patch.object(runtime, "remote_main", return_value=commit),
            mock.patch.object(runtime, "run", return_value=clean_status),
        ):
            self.assertEqual(runtime._current_protected_main_commit(), commit)

        with (
            mock.patch.object(runtime, "git_head", return_value=commit),
            mock.patch.object(runtime, "remote_main", return_value="b" * 40),
        ):
            with self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "no longer current protected main",
            ):
                runtime._current_protected_main_commit()

        with (
            mock.patch.object(runtime, "git_head", return_value=commit),
            mock.patch.object(runtime, "remote_main", return_value=commit),
            mock.patch.object(runtime, "run", return_value=dirty_status),
        ):
            with self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "must be clean to bind current protected main",
            ):
                runtime._current_protected_main_commit()

        for function in (
            runtime.create_vm,
            runtime.install_k3s,
            runtime.install_platform,
            runtime.inject_secrets,
        ):
            source = inspect.getsource(function)
            self.assertIn("_current_protected_main_commit()", source)
            self.assertIn('"source_commit": source_commit', source)

        for function in (
            runtime.apply_release,
            runtime.semantic_activate,
            runtime.status,
            runtime.seed_t048_fixture,
            runtime.functional_readback,
            runtime.t048_load_proof,
            runtime.recovery_proof,
            runtime.portability_report,
        ):
            with self.subTest(source_bound_function=function.__name__):
                self.assertIn(
                    "_current_protected_main_commit()",
                    inspect.getsource(function),
                )


    def test_portability_revalidates_functional_evidence_before_certifying(
        self,
    ) -> None:
        commit = "a" * 40
        statuses = {
            "vm-create.json": "created",
            "k3s.json": "ready",
            "platform.json": "ready",
            "secrets.json": "ready",
            "release.json": "applied",
            "release-attempt.json": "pass",
            "t048-fixture.json": "loaded",
            "semantic-search.json": "pass",
            "semantic-search-attempt.json": "pass",
            "functional-readback.json": "pass",
            "functional-readback-attempt.json": "pass",
            "t048-load.json": "pass",
            "t048-load-attempt.json": "pass",
            "recovery.json": "pass",
            "recovery-attempt.json": "pass",
            "status.json": "observed",
            "status-attempt.json": "pass",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipts = root / "receipts"
            receipts.mkdir()
            for name, status in statuses.items():
                if name.endswith("-attempt.json"):
                    continue
                runtime.atomic_json(
                    receipts / name,
                    {
                        "schema_version": 1,
                        "status": status,
                        "source_commit": commit,
                    },
                )
            for name, status in statuses.items():
                if not name.endswith("-attempt.json"):
                    continue
                receipt_name = name.removesuffix("-attempt.json") + ".json"
                runtime.atomic_json(
                    receipts / name,
                    {
                        "schema_version": 1,
                        "status": status,
                        "source_commit": commit,
                        "receipt": receipt_name,
                        "receipt_sha256": runtime.sha256_file(
                            receipts / receipt_name
                        ),
                    },
                )

            fresh_receipt = {
                "schema_version": 1,
                "status": "pass",
                "source_commit": commit,
                "gateway": "http://192.0.2.23",
            }
            with (
                mock.patch.object(
                    runtime,
                    "functional_readback",
                    return_value=fresh_receipt,
                ) as fresh_functional,
                self.assertRaisesRegex(
                    runtime.RuntimeErrorEB,
                    "receipt changed after fresh live validation",
                ),
            ):
                runtime.portability_report(root)
            fresh_functional.assert_called_once_with(root, commit)

            functional_receipt = json.loads(
                (receipts / "functional-readback.json").read_text(
                    encoding="utf-8"
                )
            )
            fresh_t048 = {
                "schema_version": 1,
                "status": "pass",
                "source_commit": commit,
                "scenario": {"virtual_users": 10},
            }
            with (
                mock.patch.object(
                    runtime,
                    "functional_readback",
                    return_value=functional_receipt,
                ) as fresh_functional,
                mock.patch.object(
                    runtime,
                    "t048_load_proof",
                    return_value=fresh_t048,
                ) as fresh_load,
                self.assertRaisesRegex(
                    runtime.RuntimeErrorEB,
                    "T048 load receipt changed after fresh live validation",
                ),
            ):
                runtime.portability_report(root)
            fresh_functional.assert_called_once_with(root, commit)
            fresh_load.assert_called_once_with(root, commit)

            t048_receipt = json.loads(
                (receipts / "t048-load.json").read_text(
                    encoding="utf-8"
                )
            )
            fresh_status = {
                "schema_version": 1,
                "status": "observed",
                "source_commit": commit,
                "cilium": {"gateway_api": True},
            }
            with (
                mock.patch.object(
                    runtime,
                    "functional_readback",
                    return_value=functional_receipt,
                ) as fresh_functional,
                mock.patch.object(
                    runtime,
                    "t048_load_proof",
                    return_value=t048_receipt,
                ) as fresh_load,
                mock.patch.object(
                    runtime,
                    "status",
                    return_value=fresh_status,
                ) as fresh_status_readback,
                self.assertRaisesRegex(
                    runtime.RuntimeErrorEB,
                    "status receipt changed after fresh live validation",
                ),
            ):
                runtime.portability_report(root)
            fresh_functional.assert_called_once_with(root, commit)
            fresh_load.assert_called_once_with(root, commit)
            fresh_status_readback.assert_called_once_with(root)

            status_receipt = json.loads(
                (receipts / "status.json").read_text(encoding="utf-8")
            )
            expected_recovery_state = {
                "recovery_receipt_sha256": runtime.sha256_file(
                    receipts / "recovery.json"
                ),
                "fixture_receipt_sha256": runtime.sha256_file(
                    receipts / "t048-fixture.json"
                ),
            }
            for drift_field in (
                "recovery_receipt_sha256",
                "fixture_receipt_sha256",
            ):
                with self.subTest(drifted_status_dependency=drift_field):
                    recovery_state = dict(expected_recovery_state)
                    recovery_state[drift_field] = "f" * 64
                    fresh_status = {
                        **status_receipt,
                        "recovery_state": recovery_state,
                    }
                    runtime.atomic_json(receipts / "status.json", fresh_status)
                    runtime.atomic_json(
                        receipts / "status-attempt.json",
                        {
                            "schema_version": 1,
                            "status": "pass",
                            "source_commit": commit,
                            "receipt": "status.json",
                            "receipt_sha256": runtime.sha256_file(
                                receipts / "status.json"
                            ),
                        },
                    )
                    with (
                        mock.patch.object(
                            runtime,
                            "functional_readback",
                            return_value=functional_receipt,
                        ) as fresh_functional,
                        mock.patch.object(
                            runtime,
                            "t048_load_proof",
                            return_value=t048_receipt,
                        ) as fresh_load,
                        mock.patch.object(
                            runtime,
                            "status",
                            return_value=fresh_status,
                        ) as fresh_status_readback,
                        self.assertRaisesRegex(
                            runtime.RuntimeErrorEB,
                            "fresh status recovery evidence is not bound to retained "
                            "portability receipts",
                        ),
                    ):
                        runtime.portability_report(root)
                    fresh_functional.assert_called_once_with(root, commit)
                    fresh_load.assert_called_once_with(root, commit)
                    fresh_status_readback.assert_called_once_with(root)

    def test_portability_rejects_failed_or_cross_revision_receipts(self) -> None:
        commit = "a" * 40
        config = runtime.load_config()
        k3s_config_path, k3s_service_path = runtime._k3s_contract_paths(config)
        k3s_config_sha256 = runtime.sha256_file(k3s_config_path)
        k3s_service_sha256 = runtime.sha256_file(k3s_service_path)
        kubeconfig_sha256 = "3" * 64

        def source_k3s_sha256(source_commit: str, path: Path) -> str:
            self.assertEqual(source_commit, commit)
            if path == k3s_config_path:
                return k3s_config_sha256
            if path == k3s_service_path:
                return k3s_service_sha256
            raise AssertionError(f"unexpected source-bound k3s path: {path}")
        flux_expected_contract = {
            name: {
                "replicas": 1,
                "selector_labels": {"app.kubernetes.io/name": name},
                "images": {
                    "containers": {
                        "manager": f"ghcr.io/fluxcd/{name}:vfixture",
                    },
                    "init_containers": {},
                },
            }
            for name in runtime.EXPECTED_FLUX_CONTROLLERS
        }
        application_expected_contract = {
            "weltgewebe-api": {
                "contract": {
                    "replicas": int(config["semantic_search"]["api_replicas"])
                },
                "contract_sha256": "7" * 64,
                "pod_contract_sha256": "8" * 64,
            },
            "weltgewebe-web": {
                "contract": {
                    "replicas": int(config["runtime_binding"]["web_replicas"])
                },
                "contract_sha256": "9" * 64,
                "pod_contract_sha256": "a" * 64,
            },
        }

        application_service_expected_contract = {}
        for name in ("weltgewebe-api", "weltgewebe-web"):
            spec = runtime._service_spec_projection(
                {
                    "spec": {
                        "selector": {"app.kubernetes.io/name": name},
                        "ports": [
                            {
                                "name": "http",
                                "port": 8080,
                                "targetPort": "http",
                                "protocol": "TCP",
                            }
                        ],
                    }
                },
                f"fixture application Service {name}",
            )
            application_service_expected_contract[name] = {
                "spec": spec,
                "spec_sha256": runtime._stable_json_sha256(spec),
            }
        application_service_account_expected_contract = {
            name: {
                "contract": {
                    "labels": {"app.kubernetes.io/name": name},
                    "annotations": {},
                    "automountServiceAccountToken": False,
                    "imagePullSecrets": [],
                    "secrets": [],
                },
                "contract_sha256": runtime._stable_json_sha256(
                    {
                        "labels": {"app.kubernetes.io/name": name},
                        "annotations": {},
                        "automountServiceAccountToken": False,
                        "imagePullSecrets": [],
                        "secrets": [],
                    }
                ),
            }
            for name in ("weltgewebe-api", "weltgewebe-web")
        }
        migration_expected_contract = {
            "contract": {"completions": 1},
            "contract_sha256": "b" * 64,
            "pod_contract_sha256": "c" * 64,
        }
        migration_status = {
            "contract_sha256": "b" * 64,
            "pod_contract_sha256": "c" * 64,
            "pod_names": ["migration-pod-0"],
            "succeeded_pods": 1,
            "canonical": True,
        }
        release_config_map_contract = {
            "data": {
                "SOURCE_COMMIT": commit,
                "API_DIGEST": "sha256:" + "b" * 64,
                "WEB_DIGEST": "sha256:" + "c" * 64,
            },
            "binaryData": {},
            "immutable": False,
        }
        portability_flux_contract = {
            "bootstrap_sha256": "1" * 64,
            "source_spec": {},
            "release_config_map": release_config_map_contract,
            "kustomization_specs": {},
        }
        application_pdb_expected_contract = {}
        for pdb_name in ("weltgewebe-api", "weltgewebe-web"):
            contract = runtime._pdb_spec_projection(
                {
                    "spec": {
                        "minAvailable": 1,
                        "selector": {
                            "matchLabels": {
                                "app.kubernetes.io/name": pdb_name
                            }
                        },
                    }
                },
                f"portability fixture PDB {pdb_name}",
            )
            application_pdb_expected_contract[pdb_name] = {
                "contract": contract,
                "contract_sha256": runtime._stable_json_sha256(
                    contract
                ),
            }
        pvc_documents = [
            {
                "metadata": {
                    "namespace": runtime.DATA_NAMESPACE,
                    "name": "postgres-data",
                },
                "spec": {
                    "accessModes": ["ReadWriteOnce"],
                    "storageClassName": "local-path",
                    "resources": {"requests": {"storage": "10Gi"}},
                },
            },
            {
                "metadata": {
                    "namespace": runtime.DATA_NAMESPACE,
                    "name": "nats-data",
                },
                "spec": {
                    "accessModes": ["ReadWriteOnce"],
                    "storageClassName": "local-path",
                    "resources": {"requests": {"storage": "5Gi"}},
                },
            },
            {
                "metadata": {
                    "namespace": runtime.APP_NAMESPACE,
                    "name": "ollama-models",
                },
                "spec": {
                    "accessModes": ["ReadWriteOnce"],
                    "storageClassName": "local-path",
                    "resources": {"requests": {"storage": "10Gi"}},
                },
            },
        ]
        pvc_expected_contract = {}
        for pvc in pvc_documents:
            key = (
                f"{pvc['metadata']['namespace']}/"
                f"{pvc['metadata']['name']}"
            )
            spec = runtime._pvc_spec_projection(
                pvc, f"portability fixture PVC {key}"
            )
            pvc_expected_contract[key] = {
                "spec": spec,
                "spec_sha256": runtime._stable_json_sha256(spec),
            }
        statuses = {
            "vm-create.json": "created",
            "k3s.json": "ready",
            "platform.json": "ready",
            "secrets.json": "ready",
            "release.json": "applied",
            "release-attempt.json": "pass",
            "t048-fixture.json": "loaded",
            "semantic-search.json": "pass",
            "semantic-search-attempt.json": "pass",
            "functional-readback.json": "pass",
            "functional-readback-attempt.json": "pass",
            "t048-load.json": "pass",
            "t048-load-attempt.json": "pass",
            "recovery.json": "pass",
            "recovery-attempt.json": "pass",
            "status.json": "observed",
            "status-attempt.json": "pass",
        }
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.multiple(
                runtime,
                functional_readback=mock.Mock(
                    return_value={
                        "schema_version": 1,
                        "status": "pass",
                        "source_commit": commit,
                    }
                ),
                t048_load_proof=mock.Mock(
                    return_value={
                        "schema_version": 1,
                        "status": "pass",
                        "source_commit": commit,
                    }
                ),
                status=mock.Mock(
                    side_effect=lambda state_root: json.loads(
                        (
                            state_root / "receipts/status.json"
                        ).read_text(encoding="utf-8")
                    )
                ),
            ),
            mock.patch.object(
                runtime,
                "_current_protected_main_commit",
                return_value=commit,
            ),
            mock.patch.object(
                runtime,
                "_source_commit_config",
                return_value=json.loads(json.dumps(config)),
            ),
            mock.patch.object(
                runtime,
                "_git_blob_sha256",
                side_effect=source_k3s_sha256,
            ),
            mock.patch.object(
                runtime,
                "_versioned_namespace_security_contract",
                return_value=runtime._versioned_namespace_security_contract(),
            ),
            mock.patch.object(
                runtime,
                "_source_commit_data_service_contract",
                side_effect=lambda _source_commit, path, name: (
                    runtime._versioned_data_service_contract(path, name)
                ),
            ),
            mock.patch.object(
                runtime,
                "_source_commit_network_policy_specs",
                side_effect=lambda _source_commit, path, namespace: (
                    runtime._versioned_network_policy_specs(path, namespace)
                ),
            ),
            mock.patch.object(
                runtime,
                "_expected_flux_controller_contract",
                return_value=flux_expected_contract,
            ),
            mock.patch.object(
                runtime,
                "_source_commit_data_deployment_contract",
                side_effect=lambda _source_commit, path, name: (
                    runtime._versioned_data_deployment_contract(path, name)
                ),
            ),
            mock.patch.object(
                runtime,
                "_rendered_application_workload_contract",
                return_value=application_expected_contract,
            ),
            mock.patch.object(
                runtime,
                "_rendered_application_service_contract",
                return_value=application_service_expected_contract,
            ),
            mock.patch.object(
                runtime,
                "_rendered_application_service_account_contract",
                return_value=application_service_account_expected_contract,
            ),
            mock.patch.object(
                runtime,
                "_rendered_migration_job_contract",
                return_value=migration_expected_contract,
            ),
            mock.patch.object(
                runtime,
                "_rendered_pvc_contract",
                return_value=pvc_expected_contract,
            ),
            mock.patch.object(
                runtime,
                "_flux_bootstrap_contract",
                return_value=portability_flux_contract,
            ),
            mock.patch.object(
                runtime,
                "_rendered_application_pdb_contract",
                return_value=application_pdb_expected_contract,
            ),
        ):
            root = Path(tmp)
            receipts = root / "receipts"
            receipts.mkdir()
            for name, status in statuses.items():
                payload: dict[str, object] = {"schema_version": 1, "status": status}
                if name == "vm-create.json":
                    payload.update(vm_receipt_fixture(state_root=root))
                elif name == "k3s.json":
                    payload.update(
                        {
                            "source_commit": commit,
                            "vm_ip": "192.168.122.10",
                            "binary_sha256": config["kubernetes"]["binary_sha256"],
                            "config_sha256": k3s_config_sha256,
                            "service_sha256": k3s_service_sha256,
                            "kubeconfig_sha256": kubeconfig_sha256,
                        }
                    )
                elif name == "release.json":
                    payload["source_commit"] = commit
                    payload["sha256"] = "1" * 64
                    payload["api_digest"] = "sha256:" + "b" * 64
                    payload["web_digest"] = "sha256:" + "c" * 64
                elif name in {
                    "platform.json",
                    "secrets.json",
                    "t048-fixture.json",
                    "semantic-search.json",
                    "functional-readback.json",
                    "recovery.json",
                    "status.json",
                }:
                    payload["source_commit"] = commit
                elif name == "t048-load.json":
                    payload["source_commit"] = commit
                elif name in {
                    "release-attempt.json",
                    "semantic-search-attempt.json",
                    "functional-readback-attempt.json",
                    "t048-load-attempt.json",
                    "recovery-attempt.json",
                    "status-attempt.json",
                }:
                    receipt_name = name.removesuffix("-attempt.json") + ".json"
                    payload["source_commit"] = commit
                    payload["receipt"] = receipt_name
                    payload["receipt_sha256"] = runtime.sha256_file(
                        receipts / receipt_name
                    )
                if name == "status.json":
                    payload["vm_create_sha256"] = runtime.sha256_file(receipts / "vm-create.json")
                    payload["vm_substrate"] = vm_substrate_fixture()
                    payload["vm_ip"] = "192.168.122.10"
                    payload["recovery_state"] = {
                        "recovery_receipt_sha256": runtime.sha256_file(
                            receipts / "recovery.json"
                        ),
                        "fixture_receipt_sha256": runtime.sha256_file(
                            receipts / "t048-fixture.json"
                        ),
                    }
                    payload["k3s_runtime"] = {
                        "vm_ip": "192.168.122.10",
                        "kubeconfig_sha256": kubeconfig_sha256,
                        "kubeconfig_server": "https://192.168.122.10:6443",
                        "binary_sha256": config["kubernetes"]["binary_sha256"],
                        "config_sha256": k3s_config_sha256,
                        "service_sha256": k3s_service_sha256,
                        "service_active": True,
                        "service_substate": "running",
                        "unit_file_state": "enabled",
                        "fragment_path": "/etc/systemd/system/k3s.service",
                        "drop_ins_absent": True,
                        "main_pid": 1234,
                        "process_exe": "/usr/local/bin/k3s",
                        "process_argv": ["/usr/local/bin/k3s", "server"],
                        "environment_overrides_absent": True,
                    }
                    def stored_pod_proof(
                        workload: str,
                        expected_images: dict[str, dict[str, str]],
                        replicas: int = 1,
                    ) -> dict:
                        def runtime_id(image: str) -> str:
                            if "@" in image:
                                digest = image.rsplit("@", 1)[1]
                            else:
                                digest = (
                                    "sha256:"
                                    + hashlib.sha256(
                                        image.encode("utf-8")
                                    ).hexdigest()
                                )
                            return "containerd://" + digest

                        image_sha256 = runtime._stable_json_sha256(expected_images)
                        pods = {
                            f"{workload}-{index}": {
                                "ready": True,
                                "requested_images_sha256": image_sha256,
                                "runtime_image_ids": {
                                    group: {
                                        container_name: runtime_id(image)
                                        for container_name, image
                                        in expected_images[group].items()
                                    }
                                    for group in (
                                        "containers",
                                        "init_containers",
                                    )
                                },
                            }
                            for index in range(replicas)
                        }
                        return {
                            "expected_replicas": replicas,
                            "observed_replicas": replicas,
                            "requested_images_sha256": image_sha256,
                            "runtime_image_ids_sha256": (
                                runtime._pod_runtime_image_ids_sha256(
                                    pods, expected_images
                                )
                            ),
                            "images_canonical": True,
                            "pods": pods,
                        }

                    cilium_images = {
                        "containers": {
                            "cilium-agent": "quay.io/cilium/cilium:v1.19.5",
                        },
                        "init_containers": {
                            "config": "quay.io/cilium/startup-script:1",
                        },
                    }
                    cilium_operator_images = {
                        "containers": {
                            "cilium-operator": (
                                "quay.io/cilium/operator-generic:v1.19.5"
                            ),
                        },
                        "init_containers": {},
                    }
                    cilium_relay_images = {
                        "containers": {
                            "hubble-relay": (
                                "quay.io/cilium/hubble-relay:v1.19.5"
                            ),
                        },
                        "init_containers": {},
                    }
                    payload["cilium"] = {
                        "chart_version": runtime.load_config()["cilium"]["chart_version"],
                        "gateway_api": True,
                        "kube_proxy_replacement": True,
                        "daemonset_desired": 1,
                        "daemonset_ready": 1,
                        "daemonset_images": cilium_images,
                        "daemonset_images_canonical": True,
                        "daemonset_pods": stored_pod_proof(
                            "cilium", cilium_images
                        ),
                        "operator_images": cilium_operator_images,
                        "operator_images_canonical": True,
                        "operator_pods": stored_pod_proof(
                            "cilium-operator", cilium_operator_images
                        ),
                        "operator": {
                            "available": True,
                            "desired_replicas": 1,
                        },
                        "relay_images": cilium_relay_images,
                        "relay_images_canonical": True,
                        "relay_pods": stored_pod_proof(
                            "hubble-relay", cilium_relay_images
                        ),
                        "relay": {
                            "available": True,
                            "desired_replicas": 1,
                        },
                        "kube_proxy_present": False,
                    }
                    payload["flux_bootstrap_sha256"] = "1" * 64
                    payload["flux_source_revision"] = f"main@sha1:{commit}"
                    payload["flux_controllers"] = {
                        name: {
                            "available": True,
                            "desired_replicas": 1,
                            "images": expected["images"],
                            "images_sha256": runtime._stable_json_sha256(
                                expected["images"]
                            ),
                            "images_canonical": True,
                            "pods": stored_pod_proof(
                                name,
                                expected["images"],
                            ),
                        }
                        for name, expected in flux_expected_contract.items()
                    }
                    payload["flux_runtime_image_ids_baseline"] = {
                        name: controller["pods"]["runtime_image_ids_sha256"]
                        for name, controller in payload["flux_controllers"].items()
                    }
                    payload["flux_release_config_map"] = {
                        "contract_sha256": runtime._stable_json_sha256(
                            release_config_map_contract
                        ),
                        "canonical": True,
                    }
                    payload["flux"] = {
                        name: {
                            "ready": True,
                            "spec_sha256": "2" * 64,
                        }
                        for name in runtime.EXPECTED_FLUX_KUSTOMIZATIONS
                    }
                    config = runtime.load_config()
                    payload["node_ready"] = True
                    payload["kubelet_version"] = config["kubernetes"]["version"]
                    payload["data_deployments"] = {
                        name: {
                            "available": True,
                            "desired_replicas": expected["replicas"],
                            "images_canonical": True,
                            "images_sha256": runtime._stable_json_sha256(
                                expected["images"]
                            ),
                            "pods": stored_pod_proof(
                                name,
                                expected["images"],
                                expected["replicas"],
                            ),
                            "contract_sha256": expected[
                                "contract_sha256"
                            ],
                            "pod_contract_sha256": expected[
                                "pod_contract_sha256"
                            ],
                            "pod_names": [
                                f"{name}-{index}"
                                for index in range(expected["replicas"])
                            ],
                            "canonical": True,
                        }
                        for name, expected in (
                            (
                                name,
                                runtime._versioned_data_deployment_contract(
                                    runtime.CLUSTER / f"data/{name}.yaml", name
                                ),
                            )
                            for name in ("postgres", "nats")
                        )
                    }
                    payload["namespace_security"] = {
                        name: {
                            "labels": expected["labels"],
                            "labels_sha256": expected["labels_sha256"],
                            "canonical": True,
                        }
                        for name, expected in (
                            runtime._versioned_namespace_security_contract()
                        ).items()
                    }
                    payload["data_services"] = {
                        name: {
                            "spec": expected["spec"],
                            "spec_sha256": expected["spec_sha256"],
                            "canonical": True,
                        }
                        for name, expected in (
                            (
                                name,
                                runtime._versioned_data_service_contract(
                                    runtime.CLUSTER / f"data/{name}.yaml", name
                                ),
                            )
                            for name in ("postgres", "nats")
                        )
                    }
                    payload["application_workloads"] = {
                        name: {
                            "contract_sha256": expected["contract_sha256"],
                            "pod_contract_sha256": expected[
                                "pod_contract_sha256"
                            ],
                            "pod_names": [
                                f"{name}-{index}"
                                for index in range(
                                    expected["contract"]["replicas"]
                                )
                            ],
                            "canonical": True,
                        }
                        for name, expected in application_expected_contract.items()
                    }
                    payload["application_pod_inventory"] = sorted(
                        [
                            *(
                                pod_name
                                for workload in payload[
                                    "application_workloads"
                                ].values()
                                for pod_name in workload["pod_names"]
                            ),
                            *migration_status["pod_names"],
                        ]
                    )
                    payload["application_services"] = {
                        name: {
                            "spec": expected["spec"],
                            "spec_sha256": expected["spec_sha256"],
                            "canonical": True,
                        }
                        for name, expected in (
                            application_service_expected_contract.items()
                        )
                    }
                    payload["application_service_accounts"] = {
                        name: {
                            "contract_sha256": expected[
                                "contract_sha256"
                            ],
                            "canonical": True,
                        }
                        for name, expected in (
                            application_service_account_expected_contract.items()
                        )
                    }
                    payload["application_disruption_budgets"] = {
                        name: {
                            "contract_sha256": expected[
                                "contract_sha256"
                            ],
                            "canonical": True,
                        }
                        for name, expected in (
                            application_pdb_expected_contract.items()
                        )
                    }
                    payload["migration"] = json.loads(
                        json.dumps(migration_status)
                    )
                    payload["migration_complete"] = True
                    payload["pvcs"] = {
                        key: {
                            "phase": "Bound",
                            "spec_sha256": value["spec_sha256"],
                            "canonical": True,
                        }
                        for key, value in pvc_expected_contract.items()
                    }
                    runtime_binding = config["runtime_binding"]
                    api_images = {
                        "api": "ghcr.io/heimgewebe/commonthing-api@sha256:" + "b" * 64,
                        "search-worker": "ghcr.io/heimgewebe/commonthing-api@sha256:" + "b" * 64,
                        "ollama": config["semantic_search"]["ollama_image"],
                    }
                    web_images = {
                        "web": "ghcr.io/heimgewebe/commonthing-web@sha256:" + "c" * 64,
                    }
                    payload["pods"] = {
                        "weltgewebe-api": {
                            "expected_replicas": int(
                                config["semantic_search"]["api_replicas"]
                            ),
                            "observed_replicas": int(
                                config["semantic_search"]["api_replicas"]
                            ),
                            "requested_images_sha256": runtime._stable_json_sha256(
                                api_images
                            ),
                            "images_canonical": True,
                            "pods": {
                                "weltgewebe-api-0": {
                                    "ready": True,
                                    "requested_images_sha256": runtime._stable_json_sha256(
                                        api_images
                                    ),
                                    "runtime_image_ids": {
                                        name: "containerd://" + image.rsplit("@", 1)[1]
                                        for name, image in api_images.items()
                                    },
                                }
                            },
                        },
                        "weltgewebe-web": {
                            "expected_replicas": int(runtime_binding["web_replicas"]),
                            "observed_replicas": int(runtime_binding["web_replicas"]),
                            "requested_images_sha256": runtime._stable_json_sha256(
                                web_images
                            ),
                            "images_canonical": True,
                            "pods": {
                                f"weltgewebe-web-{index}": {
                                    "ready": True,
                                    "requested_images_sha256": runtime._stable_json_sha256(
                                        web_images
                                    ),
                                    "runtime_image_ids": {
                                        name: "containerd://" + image.rsplit("@", 1)[1]
                                        for name, image in web_images.items()
                                    },
                                }
                                for index in range(int(runtime_binding["web_replicas"]))
                            },
                        },
                    }
                    payload["runtime_contract"] = {
                        "config_map_data_sha256": runtime._stable_json_sha256(
                            runtime_binding["config_map_data"]
                        ),
                        "network_policy_specs_sha256": runtime._stable_json_sha256(
                            runtime_binding["network_policy_specs"]
                        ),
                        "network_policy_names": sorted(
                            runtime_binding["network_policy_specs"]
                        ),
                        "data_network_policy_specs_sha256": runtime._stable_json_sha256(
                            runtime._versioned_network_policy_specs(
                                runtime.CLUSTER / "data/network-policy.yaml",
                                runtime.DATA_NAMESPACE,
                            )
                        ),
                        "data_network_policy_names": sorted(
                            runtime._versioned_network_policy_specs(
                                runtime.CLUSTER / "data/network-policy.yaml",
                                runtime.DATA_NAMESPACE,
                            )
                        ),
                        "cilium_network_policy_specs_sha256": runtime._stable_json_sha256(
                            runtime_binding["cilium_network_policy_specs"]
                        ),
                        "cilium_network_policy_names": sorted(
                            runtime_binding["cilium_network_policy_specs"]
                        ),
                        "temporary_model_egress_absent": True,
                        "policy_specs_canonical": True,
                    }
                (receipts / name).write_text(
                    json.dumps(payload) + "\n", encoding="utf-8"
                )

            status_path = receipts / "status.json"
            status_payload = json.loads(status_path.read_text(encoding="utf-8"))
            cilium_baseline = {
                "daemonset": status_payload["cilium"]["daemonset_pods"][
                    "runtime_image_ids_sha256"
                ],
                "operator": status_payload["cilium"]["operator_pods"][
                    "runtime_image_ids_sha256"
                ],
                "relay": status_payload["cilium"]["relay_pods"][
                    "runtime_image_ids_sha256"
                ],
            }
            status_payload["cilium"]["runtime_image_ids_baseline"] = (
                cilium_baseline
            )
            runtime.atomic_json(status_path, status_payload)
            platform_path = receipts / "platform.json"
            platform_payload = json.loads(
                platform_path.read_text(encoding="utf-8")
            )
            platform_payload["cilium_runtime_image_ids"] = cilium_baseline
            platform_payload["flux_runtime_image_ids"] = status_payload[
                "flux_runtime_image_ids_baseline"
            ]
            runtime.atomic_json(platform_path, platform_payload)
            status_attempt_path = receipts / "status-attempt.json"
            status_attempt = json.loads(
                status_attempt_path.read_text(encoding="utf-8")
            )
            status_attempt["receipt_sha256"] = runtime.sha256_file(status_path)
            runtime.atomic_json(status_attempt_path, status_attempt)

            baseline = runtime.portability_report(root)
            self.assertEqual(baseline["status"], "pass")
            self.assertIn("vm-create.json", baseline["receipts"])

            vm_path = receipts / "vm-create.json"
            vm_receipt = vm_path.read_text(encoding="utf-8")
            vm_path.unlink()
            with self.assertRaisesRegex(runtime.RuntimeErrorEB, "missing receipt: vm-create.json"):
                runtime.portability_report(root)
            self.assertFalse((receipts / "portability.json").exists())
            for key, value, error in (
                ("source_commit", "b" * 40, "source binding drifted: vm-create.json"),
                ("source_commit", None, "source binding drifted: vm-create.json"),
                ("status", "failed", "does not prove success: vm-create.json"),
                ("config_sha256", "0" * 64, "source/config binding drifted"),
                ("substrate", {}, "VM substrate contract drifted"),
            ):
                with self.subTest(vm_receipt_field=key, value=value):
                    changed = json.loads(vm_receipt)
                    changed[key] = value
                    runtime.atomic_json(vm_path, changed)
                    with self.assertRaisesRegex(runtime.RuntimeErrorEB, error):
                        runtime.portability_report(root)
            vm_path.write_text(vm_receipt, encoding="utf-8")

            status_path, attempt_path = receipts / "status.json", receipts / "status-attempt.json"
            original_status, original_attempt = status_path.read_text(), attempt_path.read_text()
            for field, value in (("vm_create_sha256", "0" * 64), ("vm_substrate", {})):
                with self.subTest(status_field=field):
                    changed_status = json.loads(original_status)
                    changed_status[field] = value
                    runtime.atomic_json(status_path, changed_status)
                    changed_attempt = json.loads(original_attempt)
                    changed_attempt["receipt_sha256"] = runtime.sha256_file(status_path)
                    runtime.atomic_json(attempt_path, changed_attempt)
                    with self.assertRaisesRegex(runtime.RuntimeErrorEB, "status is not bound"):
                        runtime.portability_report(root)
            status_path.write_text(original_status, encoding="utf-8")
            attempt_path.write_text(original_attempt, encoding="utf-8")
            self.assertEqual(runtime.portability_report(root)["status"], "pass")

            changed_status = json.loads(original_status)
            changed_status["data_deployments"]["nats"][
                "contract_sha256"
            ] = "0" * 64
            runtime.atomic_json(status_path, changed_status)
            changed_attempt = json.loads(original_attempt)
            changed_attempt["receipt_sha256"] = runtime.sha256_file(status_path)
            runtime.atomic_json(attempt_path, changed_attempt)
            with self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "live data Deployment contract",
            ):
                runtime.portability_report(root)
            status_path.write_text(original_status, encoding="utf-8")
            attempt_path.write_text(original_attempt, encoding="utf-8")


            changed_status = json.loads(original_status)
            changed_status["pvcs"][
                f"{runtime.DATA_NAMESPACE}/postgres-data"
            ]["spec_sha256"] = "0" * 64
            runtime.atomic_json(status_path, changed_status)
            changed_attempt = json.loads(original_attempt)
            changed_attempt["receipt_sha256"] = runtime.sha256_file(status_path)
            runtime.atomic_json(attempt_path, changed_attempt)
            with self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "PVC contract",
            ):
                runtime.portability_report(root)
            status_path.write_text(original_status, encoding="utf-8")
            attempt_path.write_text(original_attempt, encoding="utf-8")

            changed_status = json.loads(original_status)
            changed_status["migration"]["contract_sha256"] = "0" * 64
            runtime.atomic_json(status_path, changed_status)
            changed_attempt = json.loads(original_attempt)
            changed_attempt["receipt_sha256"] = runtime.sha256_file(status_path)
            runtime.atomic_json(attempt_path, changed_attempt)
            with self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "migration Job/Pod contract",
            ):
                runtime.portability_report(root)
            status_path.write_text(original_status, encoding="utf-8")
            attempt_path.write_text(original_attempt, encoding="utf-8")

            changed_status = json.loads(original_status)
            changed_status["application_services"]["weltgewebe-api"][
                "spec_sha256"
            ] = "0" * 64
            runtime.atomic_json(status_path, changed_status)
            changed_attempt = json.loads(original_attempt)
            changed_attempt["receipt_sha256"] = runtime.sha256_file(status_path)
            runtime.atomic_json(attempt_path, changed_attempt)
            with self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "application Service contract",
            ):
                runtime.portability_report(root)
            status_path.write_text(original_status, encoding="utf-8")
            attempt_path.write_text(original_attempt, encoding="utf-8")

            changed_status = json.loads(original_status)
            changed_status["application_service_accounts"][
                "weltgewebe-api"
            ]["contract_sha256"] = "0" * 64
            runtime.atomic_json(status_path, changed_status)
            changed_attempt = json.loads(original_attempt)
            changed_attempt["receipt_sha256"] = runtime.sha256_file(status_path)
            runtime.atomic_json(attempt_path, changed_attempt)
            with self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "application ServiceAccount contract",
            ):
                runtime.portability_report(root)
            status_path.write_text(original_status, encoding="utf-8")
            attempt_path.write_text(original_attempt, encoding="utf-8")

            changed_status = json.loads(original_status)
            changed_status["runtime_contract"]["config_map_data_sha256"] = "0" * 64
            runtime.atomic_json(status_path, changed_status)
            changed_attempt = json.loads(original_attempt)
            changed_attempt["receipt_sha256"] = runtime.sha256_file(status_path)
            runtime.atomic_json(attempt_path, changed_attempt)
            with self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "live runtime configuration contract",
            ):
                runtime.portability_report(root)
            status_path.write_text(original_status, encoding="utf-8")
            attempt_path.write_text(original_attempt, encoding="utf-8")

            changed_status = json.loads(original_status)
            changed_status["cilium"]["daemonset_images_canonical"] = False
            runtime.atomic_json(status_path, changed_status)
            changed_attempt = json.loads(original_attempt)
            changed_attempt["receipt_sha256"] = runtime.sha256_file(status_path)
            runtime.atomic_json(attempt_path, changed_attempt)
            with self.assertRaisesRegex(
                runtime.RuntimeErrorEB, "live Cilium contract"
            ):
                runtime.portability_report(root)
            status_path.write_text(original_status, encoding="utf-8")
            attempt_path.write_text(original_attempt, encoding="utf-8")

            changed_status = json.loads(original_status)
            changed_status["cilium"]["relay_images_canonical"] = False
            runtime.atomic_json(status_path, changed_status)
            changed_attempt = json.loads(original_attempt)
            changed_attempt["receipt_sha256"] = runtime.sha256_file(status_path)
            runtime.atomic_json(attempt_path, changed_attempt)
            with self.assertRaisesRegex(
                runtime.RuntimeErrorEB, "live Cilium contract"
            ):
                runtime.portability_report(root)
            status_path.write_text(original_status, encoding="utf-8")
            attempt_path.write_text(original_attempt, encoding="utf-8")

            changed_status = json.loads(original_status)
            changed_status["cilium"]["runtime_image_ids_baseline"][
                "relay"
            ] = "0" * 64
            runtime.atomic_json(status_path, changed_status)
            changed_attempt = json.loads(original_attempt)
            changed_attempt["receipt_sha256"] = runtime.sha256_file(status_path)
            runtime.atomic_json(attempt_path, changed_attempt)
            with self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "installed Cilium runtime image baseline",
            ):
                runtime.portability_report(root)
            status_path.write_text(original_status, encoding="utf-8")
            attempt_path.write_text(original_attempt, encoding="utf-8")

            changed_status = json.loads(original_status)
            changed_status["cilium"]["relay_pods"]["pods"]["hubble-relay-0"][
                "runtime_image_ids"
            ]["containers"]["hubble-relay"] = "containerd://not-a-digest"
            runtime.atomic_json(status_path, changed_status)
            changed_attempt = json.loads(original_attempt)
            changed_attempt["receipt_sha256"] = runtime.sha256_file(status_path)
            runtime.atomic_json(attempt_path, changed_attempt)
            with self.assertRaisesRegex(
                runtime.RuntimeErrorEB, "Hubble Relay Pod contract"
            ):
                runtime.portability_report(root)
            status_path.write_text(original_status, encoding="utf-8")
            attempt_path.write_text(original_attempt, encoding="utf-8")

            changed_status = json.loads(original_status)
            changed_status["cilium"]["daemonset_pods"]["pods"]["cilium-0"][
                "runtime_image_ids"
            ]["containers"]["cilium-agent"] = "containerd://not-a-digest"
            runtime.atomic_json(status_path, changed_status)
            changed_attempt = json.loads(original_attempt)
            changed_attempt["receipt_sha256"] = runtime.sha256_file(status_path)
            runtime.atomic_json(attempt_path, changed_attempt)
            with self.assertRaisesRegex(
                runtime.RuntimeErrorEB, "Cilium DaemonSet Pod contract"
            ):
                runtime.portability_report(root)
            status_path.write_text(original_status, encoding="utf-8")
            attempt_path.write_text(original_attempt, encoding="utf-8")

            changed_status = json.loads(original_status)
            changed_status["data_deployments"]["postgres"]["pods"]["pods"][
                "postgres-0"
            ]["runtime_image_ids"]["containers"]["postgres"] = (
                "containerd://sha256:" + "0" * 64
            )
            runtime.atomic_json(status_path, changed_status)
            changed_attempt = json.loads(original_attempt)
            changed_attempt["receipt_sha256"] = runtime.sha256_file(status_path)
            runtime.atomic_json(attempt_path, changed_attempt)
            with self.assertRaisesRegex(
                runtime.RuntimeErrorEB, "data Pod postgres contract"
            ):
                runtime.portability_report(root)
            status_path.write_text(original_status, encoding="utf-8")
            attempt_path.write_text(original_attempt, encoding="utf-8")

            changed_status = json.loads(original_status)
            changed_status["cilium"]["operator_images_canonical"] = False
            runtime.atomic_json(status_path, changed_status)
            changed_attempt = json.loads(original_attempt)
            changed_attempt["receipt_sha256"] = runtime.sha256_file(status_path)
            runtime.atomic_json(attempt_path, changed_attempt)
            with self.assertRaisesRegex(
                runtime.RuntimeErrorEB, "live Cilium contract"
            ):
                runtime.portability_report(root)
            status_path.write_text(original_status, encoding="utf-8")
            attempt_path.write_text(original_attempt, encoding="utf-8")

            changed_status = json.loads(original_status)
            changed_status["runtime_contract"]["data_network_policy_specs_sha256"] = (
                "0" * 64
            )
            runtime.atomic_json(status_path, changed_status)
            changed_attempt = json.loads(original_attempt)
            changed_attempt["receipt_sha256"] = runtime.sha256_file(status_path)
            runtime.atomic_json(attempt_path, changed_attempt)
            with self.assertRaisesRegex(
                runtime.RuntimeErrorEB, "live runtime configuration contract"
            ):
                runtime.portability_report(root)
            status_path.write_text(original_status, encoding="utf-8")
            attempt_path.write_text(original_attempt, encoding="utf-8")

            changed_status = json.loads(original_status)
            changed_status["pods"]["weltgewebe-api"]["requested_images_sha256"] = (
                "0" * 64
            )
            runtime.atomic_json(status_path, changed_status)
            changed_attempt = json.loads(original_attempt)
            changed_attempt["receipt_sha256"] = runtime.sha256_file(status_path)
            runtime.atomic_json(attempt_path, changed_attempt)
            with self.assertRaisesRegex(
                runtime.RuntimeErrorEB, "live application Pod contract"
            ):
                runtime.portability_report(root)
            status_path.write_text(original_status, encoding="utf-8")
            attempt_path.write_text(original_attempt, encoding="utf-8")

            changed_status = json.loads(original_status)
            changed_status["pods"]["weltgewebe-api"]["pods"]["weltgewebe-api-0"][
                "runtime_image_ids"
            ]["api"] = "containerd://sha256:" + "0" * 64
            runtime.atomic_json(status_path, changed_status)
            changed_attempt = json.loads(original_attempt)
            changed_attempt["receipt_sha256"] = runtime.sha256_file(status_path)
            runtime.atomic_json(attempt_path, changed_attempt)
            with self.assertRaisesRegex(
                runtime.RuntimeErrorEB, "live application Pod contract"
            ):
                runtime.portability_report(root)
            status_path.write_text(original_status, encoding="utf-8")
            attempt_path.write_text(original_attempt, encoding="utf-8")

            changed_status = json.loads(original_status)
            changed_status["flux_bootstrap_sha256"] = "0" * 64
            runtime.atomic_json(status_path, changed_status)
            changed_attempt = json.loads(original_attempt)
            changed_attempt["receipt_sha256"] = runtime.sha256_file(status_path)
            runtime.atomic_json(attempt_path, changed_attempt)
            with self.assertRaisesRegex(
                runtime.RuntimeErrorEB, "live Flux contract"
            ):
                runtime.portability_report(root)
            status_path.write_text(original_status, encoding="utf-8")
            attempt_path.write_text(original_attempt, encoding="utf-8")

            # A replacement receipt with a valid identity still needs a new live status.
            changed = json.loads(vm_receipt)
            changed["substrate"]["uuid"] = "44444444-4444-4444-8444-444444444444"
            runtime.atomic_json(vm_path, changed)
            with self.assertRaisesRegex(runtime.RuntimeErrorEB, "status is not bound"):
                runtime.portability_report(root)
            vm_path.write_text(vm_receipt, encoding="utf-8")

            upstream = json.loads(
                (receipts / "k3s.json").read_text(encoding="utf-8")
            )
            upstream["source_commit"] = "b" * 40
            (receipts / "k3s.json").write_text(
                json.dumps(upstream) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "source binding drifted: k3s.json",
            ):
                runtime.portability_report(root)
            upstream["source_commit"] = commit
            (receipts / "k3s.json").write_text(
                json.dumps(upstream) + "\n",
                encoding="utf-8",
            )

            failed = json.loads((receipts / "t048-load.json").read_text())
            failed["status"] = "fail"
            (receipts / "t048-load.json").write_text(
                json.dumps(failed) + "\n", encoding="utf-8"
            )
            with self.assertRaises(runtime.RuntimeErrorEB):
                runtime.portability_report(root)

            failed["status"] = "pass"
            failed["source_commit"] = "b" * 40
            (receipts / "t048-load.json").write_text(
                json.dumps(failed) + "\n", encoding="utf-8"
            )
            with self.assertRaises(runtime.RuntimeErrorEB):
                runtime.portability_report(root)

            failed["source_commit"] = commit
            (receipts / "t048-load.json").write_text(
                json.dumps(failed) + "\n", encoding="utf-8"
            )
            attempt = json.loads(
                (receipts / "t048-load-attempt.json").read_text(encoding="utf-8")
            )
            attempt["receipt_sha256"] = "0" * 64
            (receipts / "t048-load-attempt.json").write_text(
                json.dumps(attempt) + "\n", encoding="utf-8"
            )
            with self.assertRaises(runtime.RuntimeErrorEB):
                runtime.portability_report(root)

            attempt["receipt_sha256"] = runtime.sha256_file(
                receipts / "t048-load.json"
            )
            (receipts / "t048-load-attempt.json").write_text(
                json.dumps(attempt) + "\n", encoding="utf-8"
            )
            release_attempt = json.loads(
                (receipts / "release-attempt.json").read_text(encoding="utf-8")
            )
            release_attempt["receipt_sha256"] = "0" * 64
            (receipts / "release-attempt.json").write_text(
                json.dumps(release_attempt) + "\n", encoding="utf-8"
            )
            with self.assertRaises(runtime.RuntimeErrorEB):
                runtime.portability_report(root)

            release_attempt["receipt_sha256"] = runtime.sha256_file(
                receipts / "release.json"
            )
            (receipts / "release-attempt.json").write_text(
                json.dumps(release_attempt) + "\n", encoding="utf-8"
            )
            with mock.patch.object(
                runtime,
                "_current_protected_main_commit",
                return_value="b" * 40,
            ):
                with self.assertRaisesRegex(
                    runtime.RuntimeErrorEB,
                    "no longer current protected main",
                ):
                    runtime.portability_report(root)

    def test_semantic_cleanup_requires_verified_policy_absence(self) -> None:
        source = inspect.getsource(runtime.semantic_activate)
        cleanup = source.split("finally:", 1)[1].split("receipt = {", 1)[0]
        self.assertIn("_kubectl(", cleanup)
        self.assertIn('"--ignore-not-found=true"', cleanup)
        self.assertIn('"-o", "name"', cleanup)
        self.assertIn("remaining_egress.stdout.strip()", cleanup)
        self.assertNotIn("check=False", cleanup)

    def test_recovery_rerun_invalidates_stale_success(self) -> None:
        source = inspect.getsource(runtime.recovery_proof)
        self.assertIn(
            "_invalidate_receipts(root, RECOVERY_ATTEMPT_INVALIDATES)",
            source,
        )
        self.assertEqual(
            set(runtime.RECOVERY_ATTEMPT_INVALIDATES),
            {
                "recovery.json",
                "recovery-attempt.json",
                "recovery-failed.json",
                "status.json",
                "status-attempt.json",
                "portability.json",
            },
        )
        self.assertIn("atomic_json(", source)
        self.assertIn("recovery_failed_receipt,", source)
        self.assertIn("atomic_json(recovery_receipt, receipt)", source)
        portability = inspect.getsource(runtime.portability_report)
        self.assertIn("recovery_failed_receipt.is_file()", portability)

    def test_recovery_running_attempt_blocks_retry_before_live_work(self) -> None:
        commit = "a" * 40
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipts = root / "receipts"
            receipts.mkdir()
            runtime.atomic_json(
                receipts / "release.json",
                {
                    "schema_version": 1,
                    "status": "applied",
                    "source_commit": commit,
                },
            )
            runtime.atomic_json(
                receipts / "recovery-attempt.json",
                {
                    "schema_version": 1,
                    "status": "running",
                    "source_commit": commit,
                    "receipt": "recovery.json",
                    "started_at_unix_ms": 1,
                },
            )
            with mock.patch.object(runtime, "_flux_suspend") as suspend:
                with self.assertRaisesRegex(
                    runtime.RuntimeErrorEB,
                    "incomplete or failed attempt",
                ):
                    runtime.recovery_proof(root)
            suspend.assert_not_called()
            attempt = json.loads(
                (receipts / "recovery-attempt.json").read_text(encoding="utf-8")
            )
            self.assertEqual(attempt["status"], "running")

    def test_fixture_rerun_invalidates_its_downstream_chain_only(self) -> None:
        self.assertEqual(
            set(runtime.FIXTURE_ATTEMPT_INVALIDATES),
            {
                "t048-fixture.json",
                "functional-readback.json",
                "functional-readback-attempt.json",
                "t048-load.json",
                "t048-load-attempt.json",
                "recovery.json",
                "recovery-attempt.json",
                "recovery-failed.json",
                "status.json",
                "status-attempt.json",
                "portability.json",
            },
        )
        self.assertNotIn("semantic-search.json", runtime.FIXTURE_ATTEMPT_INVALIDATES)

        commit = "a" * 40
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipts = root / "receipts"
            receipts.mkdir()
            (receipts / "release.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "applied",
                        "source_commit": commit,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            semantic = receipts / "semantic-search.json"
            semantic.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "pass",
                        "source_commit": commit,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            for name in runtime.FIXTURE_ATTEMPT_INVALIDATES:
                (receipts / name).write_text(
                    json.dumps({"schema_version": 1, "status": "stale"}) + "\n",
                    encoding="utf-8",
                )
            evidence = mock.Mock()
            with (
                mock.patch.object(
                    runtime,
                    "_performance_modules",
                    return_value=(evidence, Path("/unused/domain_scale.py")),
                ),
                mock.patch.object(runtime, "load_config", return_value={}),
                mock.patch.object(
                    runtime,
                    "_current_protected_main_commit",
                    side_effect=runtime.RuntimeErrorEB("dirty checkout"),
                ),
            ):
                with self.assertRaisesRegex(runtime.RuntimeErrorEB, "dirty checkout"):
                    runtime.seed_t048_fixture(root)

            for name in runtime.FIXTURE_ATTEMPT_INVALIDATES:
                self.assertFalse((receipts / name).exists(), name)
            self.assertTrue(semantic.is_file())

    def test_t048_load_validation_preserves_functional_evidence(self) -> None:
        commit = "a" * 40
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipts = root / "receipts"
            receipts.mkdir()
            functional = receipts / "functional-readback.json"
            functional_attempt = receipts / "functional-readback-attempt.json"
            functional.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "pass",
                        "source_commit": commit,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            functional_attempt.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "pass",
                        "source_commit": commit,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with (
                mock.patch.object(
                    runtime,
                    "_current_protected_main_commit",
                    return_value=commit,
                ),
                mock.patch.object(
                    runtime,
                    "_require_kubernetes_target_binding",
                    return_value=(
                        {"kubeconfig_sha256": "1" * 64},
                        "192.168.122.10",
                        "https://192.168.122.10:6443",
                    ),
                ),
                mock.patch.object(
                    runtime,
                    "_require_t048_postgres_runtime_binding",
                    return_value={
                        "images_sha256": "2" * 64,
                        "runtime_image_ids_sha256": "3" * 64,
                    },
                ),
                mock.patch.object(
                    runtime,
                    "_require_t048_postgres_service_binding",
                    return_value={
                        "canonical": True,
                        "service_resource_version": "101",
                        "endpoint_list_resource_version": "202",
                    },
                ),
                mock.patch.object(
                    runtime,
                    "_bound_kube_env",
                    return_value=mock.MagicMock(),
                ),
                mock.patch.object(
                    runtime,
                    "_database_client_identity",
                    return_value=("proof_user", "proof_database"),
                ),
                mock.patch.object(
                    runtime,
                    "_validated_t048_fixture_receipt",
                    side_effect=runtime.RuntimeErrorEB("fixture validation stop"),
                ),
                mock.patch.object(runtime, "seed_t048_fixture") as seed_fixture,
            ):
                with self.assertRaisesRegex(
                    runtime.RuntimeErrorEB,
                    "fixture validation stop",
                ):
                    runtime.t048_load_proof(root, commit)

            self.assertTrue(functional.is_file())
            self.assertTrue(functional_attempt.is_file())
            seed_fixture.assert_not_called()

        source = inspect.getsource(runtime.t048_load_proof)
        self.assertIn("_validated_t048_fixture_receipt(root, source_commit)", source)
        self.assertNotIn("seed_t048_fixture(root)", source)

    def test_t048_seed_serializes_empty_worker_generation(self) -> None:
        source = inspect.getsource(runtime.seed_t048_fixture)
        lock = source.index("SELECT pg_advisory_xact_lock(")
        guarded_worker_generation = source.index("IF generation_count = 1 THEN")
        delete_worker_generation = source.index(
            "DELETE FROM search_index_generations"
        )
        insert_generation = source.index(
            "INSERT INTO search_index_generations (",
            delete_worker_generation,
        )
        insert_nodes = source.index("INSERT INTO domain_nodes (", insert_generation)
        self.assertLess(lock, guarded_worker_generation)
        self.assertLess(guarded_worker_generation, delete_worker_generation)
        self.assertLess(delete_worker_generation, insert_generation)
        self.assertLess(insert_generation, insert_nodes)
        self.assertIn("state = 'building'", source)
        self.assertIn("expected_nodes = 0", source)
        self.assertIn("completed_nodes = 0", source)
        self.assertIn("search_node_versions", source)
        self.assertNotIn(
            "existing_nodes or existing_edges or existing_generation",
            source,
        )

    def test_t048_expected_projection_row_binds_complete_synthetic_state(self) -> None:
        public = {
            "id": "node-public",
            "kind": "Projekt",
            "title": "Public node",
            "payload": {"tags": ["scale", "public"], "summary": "Public summary"},
        }
        public_row = runtime._t048_expected_projection_row(
            public,
            "experiment-b-t048",
            2560,
        )
        self.assertEqual(
            public_row,
            {
                "generation_id": "experiment-b-t048",
                "node_id": "node-public",
                "source_version": 1,
                "source_revision": "node-1",
                "content_sha256": "0" * 64,
                "title": "Public node",
                "tags": ["scale", "public"],
                "searchable_text": "Public summary",
                "language": "de",
                "kind": "Projekt",
                "status": "active",
                "visibility_scopes": ["public"],
                "semantic_state": "ready",
                "embedding_canonical": True,
            },
        )

        hidden = {
            "id": "node-hidden",
            "kind": "Organisation",
            "title": "Hidden node",
            "payload": {"tags": ["private"], "summary": "Hidden summary"},
        }
        hidden_row = runtime._t048_expected_projection_row(
            hidden,
            "experiment-b-t048",
            2560,
        )
        self.assertEqual(hidden_row["source_version"], 1)
        self.assertEqual(hidden_row["source_revision"], "node-1")
        self.assertEqual(hidden_row["content_sha256"], runtime.T048_HIDDEN_CONTENT_SHA256)
        self.assertEqual(hidden_row["title"], runtime.T048_REDACTED_TEXT)
        self.assertEqual(hidden_row["tags"], [])
        self.assertEqual(hidden_row["searchable_text"], runtime.T048_REDACTED_TEXT)
        self.assertEqual(hidden_row["language"], "und")
        self.assertEqual(hidden_row["kind"], runtime.T048_REDACTED_TEXT)
        self.assertEqual(hidden_row["status"], "hidden")
        self.assertEqual(hidden_row["visibility_scopes"], [])
        self.assertEqual(hidden_row["semantic_state"], "unavailable")
        self.assertTrue(hidden_row["embedding_canonical"])

    def test_t048_live_binding_rejects_complete_projection_drift(self) -> None:
        fixture_node = {
            "id": "node-1",
            "kind": "Projekt",
            "title": "Node",
            "lat": 53.5,
            "lon": 10.0,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "payload": {"tags": ["test"], "summary": "Node summary"},
        }
        database_node = {**fixture_node, "search_visibility": "public"}
        canonical_edge = {
            "id": "edge-1",
            "source_id": "node-1",
            "target_id": "node-2",
            "edge_kind": "wirkt_mit",
            "created_at": "2026-01-01T00:00:00Z",
            "payload": {"scale_fixture": True},
        }
        version_row = {
            "node_id": "node-1",
            "source_version": 1,
            "source_revision": "node-1",
            "deleted": False,
        }
        generation_id = "experiment-b-t048"
        semantic = {
            "provider": "local:ollama",
            "model_id": "qwen3-embedding:4b",
            "model_revision": "sha256:" + "a" * 64,
            "runtime_identity": "ollama:test",
            "dimension": 2560,
        }
        generation_row = {
            "generation_id": generation_id,
            **semantic,
            "document_revision": runtime.T048_DOCUMENT_REVISION,
            "normalization_revision": runtime.T048_NORMALIZATION_REVISION,
            "ranking_revision": runtime.T048_RANKING_REVISION,
            "state": "active",
            "expected_nodes": 1,
            "completed_nodes": 1,
        }
        canonical_projection = runtime._t048_expected_projection_row(
            fixture_node,
            generation_id,
            2560,
        )

        root_text = str(runtime.ROOT)
        if root_text not in sys.path:
            sys.path.insert(0, root_text)
        from scripts.performance import api_runtime_live_binding as live_binding

        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "manifest.json"
            manifest_path.write_text("{}\n", encoding="utf-8")
            edge_path = manifest_path.parent / "domain_edges.csv"
            edge_path.write_text(
                "id,source_id,target_id,edge_kind,created_at,payload\n"
                'edge-1,node-1,node-2,wirkt_mit,2026-01-01T00:00:00Z,"{""scale_fixture"":true}"\n',
                encoding="utf-8",
            )
            manifest = {
                "counts": {"nodes": 1, "edges": 1},
                "files": {
                    "edges": {
                        "name": edge_path.name,
                        "sha256": runtime.sha256_file(edge_path),
                    }
                },
            }
            drifts = (
                ("content_sha256", "1" * 64),
                ("tags", ["changed"]),
                ("searchable_text", "changed"),
                ("semantic_state", "unavailable"),
                ("embedding_canonical", False),
            )
            for field, value in drifts:
                with self.subTest(field=field):
                    drifted_projection = {
                        **canonical_projection,
                        field: value,
                    }
                    with (
                        mock.patch.object(
                            runtime,
                            "_source_bound_live_binding",
                            return_value=live_binding,
                        ),
                        mock.patch.object(
                            live_binding,
                            "_manifest_and_fixture",
                            return_value=(manifest, [fixture_node]),
                        ),
                        mock.patch.object(
                            runtime,
                            "_source_commit_config",
                            return_value={"semantic_search": semantic},
                        ),
                        mock.patch.object(
                            runtime,
                            "_run_bound_postgres_sql",
                            side_effect=[
                                json.dumps(database_node) + "\n",
                                json.dumps(canonical_edge) + "\n",
                                json.dumps(version_row) + "\n",
                                json.dumps(generation_row) + "\n",
                                json.dumps(drifted_projection) + "\n",
                            ],
                        ),
                    ):
                        with self.assertRaisesRegex(
                            runtime.RuntimeErrorEB,
                            "live search projection content does not match",
                        ):
                            runtime._t048_live_fixture_binding(
                                Path("/unused-root"),
                                manifest_path,
                                generation_id,
                                "a" * 40,
                                postgres_binding={},
                                database_identity=("proof_user", "proof_database"),
                            )

    def test_t048_live_binding_rejects_version_drift(self) -> None:
        fixture_node = {
            "id": "node-1",
            "kind": "Projekt",
            "title": "Node",
            "lat": 53.5,
            "lon": 10.0,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "payload": {"tags": ["test"]},
        }
        database_node = {**fixture_node, "search_visibility": "public"}
        canonical_edge = {
            "id": "edge-1",
            "source_id": "node-1",
            "target_id": "node-2",
            "edge_kind": "wirkt_mit",
            "created_at": "2026-01-01T00:00:00Z",
            "payload": {"scale_fixture": True},
        }
        root_text = str(runtime.ROOT)
        if root_text not in sys.path:
            sys.path.insert(0, root_text)
        from scripts.performance import api_runtime_live_binding as live_binding

        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "manifest.json"
            manifest_path.write_text("{}\n", encoding="utf-8")
            edge_path = manifest_path.parent / "domain_edges.csv"
            edge_path.write_text(
                "id,source_id,target_id,edge_kind,created_at,payload\n"
                'edge-1,node-1,node-2,wirkt_mit,2026-01-01T00:00:00Z,"{""scale_fixture"":true}"\n',
                encoding="utf-8",
            )
            manifest = {
                "counts": {"nodes": 1, "edges": 1},
                "files": {
                    "edges": {
                        "name": edge_path.name,
                        "sha256": runtime.sha256_file(edge_path),
                    }
                },
            }
            drifted_version = {
                "node_id": "node-1",
                "source_version": 2,
                "source_revision": "node-2",
                "deleted": False,
            }
            with (
                mock.patch.object(
                    runtime,
                    "_source_bound_live_binding",
                    return_value=live_binding,
                ),
                mock.patch.object(
                    live_binding,
                    "_manifest_and_fixture",
                    return_value=(manifest, [fixture_node]),
                ),
                mock.patch.object(
                    runtime,
                    "_run_bound_postgres_sql",
                    side_effect=[
                        json.dumps(database_node) + "\n",
                        json.dumps(canonical_edge) + "\n",
                        json.dumps(drifted_version) + "\n",
                    ],
                ),
            ):
                with self.assertRaisesRegex(
                    runtime.RuntimeErrorEB,
                    "live search_node_versions content does not match",
                ):
                    runtime._t048_live_fixture_binding(
                        Path("/unused-root"),
                        manifest_path,
                        "experiment-b-t048",
                        "a" * 40,
                        postgres_binding={},
                        database_identity=("proof_user", "proof_database"),
                    )

    def test_t048_live_binding_rejects_edge_content_drift(self) -> None:
        fixture_node = {
            "id": "node-1",
            "kind": "Projekt",
            "title": "Node",
            "lat": 53.5,
            "lon": 10.0,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "payload": {"tags": ["test"]},
        }
        database_node = {**fixture_node, "search_visibility": "public"}
        canonical_edge = {
            "id": "edge-1",
            "source_id": "node-1",
            "target_id": "node-2",
            "edge_kind": "wirkt_mit",
            "created_at": "2026-01-01T00:00:00Z",
            "payload": {"scale_fixture": True},
        }
        drifted_edge = {**canonical_edge, "target_id": "node-3"}

        root_text = str(runtime.ROOT)
        if root_text not in sys.path:
            sys.path.insert(0, root_text)
        from scripts.performance import api_runtime_live_binding as live_binding

        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "manifest.json"
            manifest_path.write_text("{}\n", encoding="utf-8")
            edge_path = manifest_path.parent / "domain_edges.csv"
            edge_path.write_text(
                "id,source_id,target_id,edge_kind,created_at,payload\n"
                'edge-1,node-1,node-2,wirkt_mit,2026-01-01T00:00:00Z,"{""scale_fixture"":true}"\n',
                encoding="utf-8",
            )
            manifest = {
                "counts": {"nodes": 1, "edges": 1},
                "files": {
                    "edges": {
                        "name": edge_path.name,
                        "sha256": runtime.sha256_file(edge_path),
                    }
                },
            }
            with (
                mock.patch.object(
                    runtime,
                    "_source_bound_live_binding",
                    return_value=live_binding,
                ),
                mock.patch.object(
                    live_binding,
                    "_manifest_and_fixture",
                    return_value=(manifest, [fixture_node]),
                ),
                mock.patch.object(
                    runtime,
                    "_run_bound_postgres_sql",
                    side_effect=[
                        json.dumps(database_node) + "\n",
                        json.dumps(drifted_edge) + "\n",
                    ],
                ),
            ):
                with self.assertRaisesRegex(
                    runtime.RuntimeErrorEB,
                    "live domain_edges content does not match",
                ):
                    runtime._t048_live_fixture_binding(
                        Path("/unused-root"),
                        manifest_path,
                        "experiment-b-t048",
                        "a" * 40,
                        postgres_binding={},
                        database_identity=("proof_user", "proof_database"),
                    )

    def test_t048_canonical_visibility_is_fixture_derived(self) -> None:
        self.assertEqual(
            runtime._t048_canonical_visibility({"kind": "Projekt"}),
            "public",
        )
        self.assertEqual(
            runtime._t048_canonical_visibility({"kind": "Organisation"}),
            "hidden",
        )
        with self.assertRaises(runtime.RuntimeErrorEB):
            runtime._t048_canonical_visibility({"kind": ""})

    def test_t048_live_binding_rejects_visibility_drift(self) -> None:
        fixture_row = {
            "id": "node-1",
            "kind": "Organisation",
            "title": "Hidden fixture node",
            "lat": 53.5,
            "lon": 10.0,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "payload": {"tags": ["test"]},
        }
        drifted_database_row = {
            **fixture_row,
            "search_visibility": "public",
        }
        root_text = str(runtime.ROOT)
        if root_text not in sys.path:
            sys.path.insert(0, root_text)
        from scripts.performance import api_runtime_live_binding as live_binding

        with (
            mock.patch.object(
                runtime,
                "_source_bound_live_binding",
                return_value=live_binding,
            ),
            mock.patch.object(
                live_binding,
                "_manifest_and_fixture",
                return_value=({}, [fixture_row]),
            ),
            mock.patch.object(
                runtime,
                "_run_bound_postgres_sql",
                return_value=json.dumps(drifted_database_row) + "\n",
            ),
        ):
            with self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "live domain_nodes content does not match",
            ):
                runtime._t048_live_fixture_binding(
                    Path("/unused-root"),
                    Path("/unused-manifest.json"),
                    "experiment-b-t048",
                    "a" * 40,
                    postgres_binding={},
                    database_identity=("proof_user", "proof_database"),
                )

    def test_fixture_rebinds_canonical_live_state_without_stale_receipt(self) -> None:
        commit = "a" * 40
        generation_id = "experiment-b-t048"
        live_binding = {
            "manifest_sha256": "b" * 64,
            "database_nodes_content_sha256": "c" * 64,
            "database_projection_content_sha256": "d" * 64,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipts = root / "receipts"
            receipts.mkdir()
            (receipts / "release.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "applied",
                        "source_commit": commit,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (receipts / "t048-fixture.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "loaded",
                        "source_commit": "b" * 40,
                        "manifest_sha256": "stale",
                        "live_binding": {"stale": True},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            fixture = root / "performance/fixture"
            fixture.mkdir(parents=True)
            (fixture / "manifest.json").write_text("{}\n", encoding="utf-8")

            evidence = mock.Mock()
            evidence.load_policy.return_value = {"policy": "test"}
            evidence.api_runtime_section.return_value = {
                "dataset_proof": {"profile": "domain-scale-ci"}
            }
            evidence.load_dataset_binding.return_value = {
                "counts": {"nodes": 20000, "edges": 100000},
                "manifest_sha256": "b" * 64,
            }
            psql_values = [
                "20000",
                "100000",
                "1",
                "20000",
                "100000",
                "1000",
                "20000",
                "1",
                "0",
            ]
            postgres_binding = {
                "container_id": "containerd://" + "1" * 64,
                "contract_sha256": "2" * 64,
                "pod_contract_sha256": "3" * 64,
                "runtime_image_ids_sha256": "4" * 64,
            }
            with (
                mock.patch.object(
                    runtime,
                    "_source_bound_performance_policy",
                    return_value=({"policy": "test"}, "f" * 64),
                ) as source_policy,
                mock.patch.object(
                    runtime,
                    "_performance_modules",
                    return_value=(evidence, mock.Mock()),
                ),
                mock.patch.object(
                    runtime,
                    "_source_bound_dataset_binding",
                    return_value={
                        "counts": {"nodes": 20000, "edges": 100000},
                        "manifest_sha256": "b" * 64,
                    },
                ),
                mock.patch.object(
                    runtime,
                    "_source_commit_config",
                    return_value={"semantic_search": {"generation_id": generation_id}},
                ),
                mock.patch.object(
                    runtime,
                    "_current_protected_main_commit",
                    return_value=commit,
                ),
                mock.patch.object(
                    runtime,
                    "_require_kubernetes_target_binding",
                    return_value=({}, "192.168.122.10", "https://192.168.122.10:6443"),
                ),
                mock.patch.object(
                    runtime,
                    "_kubernetes_target_identity",
                    return_value={
                        "vm_ip": "192.168.122.10",
                        "kubeconfig_sha256": "a" * 64,
                        "server": "https://192.168.122.10:6443",
                    },
                ),
                mock.patch.object(
                    runtime,
                    "_bound_kube_env",
                    return_value=mock.MagicMock(),
                ),
                mock.patch.object(
                    runtime,
                    "_require_postgres_runtime_binding",
                    return_value=postgres_binding,
                ),
                mock.patch.object(
                    runtime,
                    "_database_client_identity",
                    return_value=("proof_user", "proof_database"),
                ),
                mock.patch.object(
                    runtime,
                    "_run_bound_postgres_sql",
                    side_effect=psql_values,
                ),
                mock.patch.object(
                    runtime,
                    "_t048_live_fixture_binding",
                    return_value=live_binding,
                ) as live_check,
                mock.patch.object(runtime, "run") as run_command,
                mock.patch.object(runtime, "_run_input_file") as load_fixture,
            ):
                receipt = runtime.seed_t048_fixture(root)

            source_policy.assert_called_once_with(commit)
            self.assertEqual(receipt["status"], "loaded")
            self.assertEqual(receipt["source_commit"], commit)
            self.assertEqual(receipt["live_binding"], live_binding)
            self.assertEqual(receipt["nodes"], 20000)
            self.assertEqual(receipt["edges"], 100000)
            self.assertTrue((receipts / "t048-fixture.json").is_file())
            live_check.assert_called_once()
            run_command.assert_not_called()
            load_fixture.assert_not_called()

    def test_postgresql_clients_follow_injected_database_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            secrets_dir = root / "secrets"
            secrets_dir.mkdir()
            (secrets_dir / "database.json").write_text(
                json.dumps(
                    {
                        "username": "proof_user",
                        "database": "proof_database",
                        "password": "unused-by-client-argv",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            database_identity = runtime._database_client_identity(root)
            self.assertEqual(
                database_identity,
                ("proof_user", "proof_database"),
            )
            self.assertEqual(
                runtime._database_client_argv("psql", database_identity),
                [
                    "psql",
                    "-U",
                    "proof_user",
                    "-d",
                    "proof_database",
                ],
            )

            runtime.atomic_json(
                secrets_dir / "database.json",
                {
                    "username": "changed_user",
                    "database": "changed_database",
                    "password": "changed_password",
                },
            )
            completed = runtime.subprocess.CompletedProcess(
                ["kubectl"],
                0,
                stdout="1\n",
                stderr="",
            )
            with mock.patch.object(
                runtime, "_kubectl", return_value=completed
            ) as kubectl:
                self.assertEqual(
                    runtime._psql(
                        root,
                        "SELECT 1;",
                        database_identity=database_identity,
                    ),
                    "1",
                )
            self.assertEqual(
                kubectl.call_args.args[1],
                [
                    "-n",
                    runtime.DATA_NAMESPACE,
                    "exec",
                    "-i",
                    "deployment/postgres",
                    "--",
                    "psql",
                    "-U",
                    "proof_user",
                    "-d",
                    "proof_database",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-At",
                ],
            )
            self.assertEqual(
                runtime._database_client_identity(root),
                ("changed_user", "changed_database"),
            )

            with mock.patch.object(
                runtime, "_kubectl", return_value=completed
            ) as kubectl:
                self.assertEqual(
                    runtime._psql(
                        root,
                        "SELECT 1;",
                        database_identity=database_identity,
                        postgres_pod_name="postgres-bound-0",
                    ),
                    "1",
                )
            self.assertEqual(
                kubectl.call_args.args[1][:5],
                [
                    "-n",
                    runtime.DATA_NAMESPACE,
                    "exec",
                    "-i",
                    "postgres-bound-0",
                ],
            )
            self.assertNotIn(
                "deployment/postgres",
                kubectl.call_args.args[1],
            )

        seed_source = inspect.getsource(runtime.seed_t048_fixture)
        recovery_source = inspect.getsource(runtime.recovery_proof)
        self.assertIn(
            "database_identity = _database_client_identity(root)",
            seed_source,
        )
        self.assertIn("_require_postgres_runtime_binding(", seed_source)
        self.assertIn("_run_bound_postgres_client(", seed_source)
        self.assertNotIn('"deployment/postgres"', seed_source)
        self.assertEqual(
            recovery_source.count(
                "_verified_database_client_identity(root, source_commit)"
            ),
            1,
        )
        identity_capture = recovery_source.index(
            "_verified_database_client_identity(root, source_commit)"
        )
        first_suspend = recovery_source.index(
            '_flux_suspend(root, "commonthing-experiment-b-app")'
        )
        self.assertLess(identity_capture, first_suspend)
        self.assertGreaterEqual(
            recovery_source.count("database_identity=database_identity"),
            2,
        )
        self.assertIn('"pg_dump"', recovery_source)
        self.assertIn('"pg_restore"', recovery_source)
        self.assertGreaterEqual(
            recovery_source.count("_run_bound_postgres_client("),
            2,
        )
        self.assertIn(
            "postgres_dump_snapshot_fd",
            recovery_source,
        )
        self.assertGreaterEqual(
            recovery_source.count(
                '"database_identity_sha256": database_identity_sha256'
            ),
            2,
        )

    def test_verified_database_identity_is_bound_to_secret_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "secrets").mkdir()
            (root / "receipts").mkdir()
            database_path = root / "secrets/database.json"
            registry_path = root / "secrets/registry.json"
            database = {
                "username": "proof_user",
                "database": "proof_database",
                "password": "proof_password",
            }
            runtime.atomic_json(database_path, database)
            registry_path.write_text(
                '{"auths":{"ghcr.io":{"username":"proof-user","password":"proof-token"}}}\n',
                encoding="utf-8",
            )
            source_commit = "a" * 40
            runtime.atomic_json(
                root / "receipts/secrets.json",
                {
                    "schema_version": 1,
                    "status": "ready",
                    "source_commit": source_commit,
                    "database_secret": "commonthing-experiment-b-database",
                    "runtime_secret": "weltgewebe-runtime",
                    "registry_secret": "commonthing-experiment-b-registry",
                    "database_source_sha256": runtime.sha256_file(database_path),
                    "registry_source_sha256": runtime.sha256_file(registry_path),
                    "secret_values_recorded": False,
                },
            )

            self.assertEqual(
                runtime._verified_database_client_identity(
                    root, source_commit
                ),
                ("proof_user", "proof_database"),
            )

            runtime.atomic_json(
                database_path,
                {
                    "username": "changed_user",
                    "database": "changed_database",
                    "password": "changed_password",
                },
            )
            with self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "database Secret source digest drifted",
            ):
                runtime._verified_database_client_identity(
                    root, source_commit
                )

    def test_verified_database_identity_hashes_the_captured_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "secrets").mkdir()
            (root / "receipts").mkdir()
            database_path = root / "secrets/database.json"
            registry_path = root / "secrets/registry.json"
            original_database = {
                "username": "proof_user",
                "database": "proof_database",
                "password": "proof_password",
            }
            changed_database = {
                "username": "changed_user",
                "database": "changed_database",
                "password": "changed_password",
            }
            runtime.atomic_json(database_path, original_database)
            registry_path.write_text(
                '{"auths":{"ghcr.io":{"username":"proof-user","password":"proof-token"}}}\n',
                encoding="utf-8",
            )
            source_commit = "b" * 40
            runtime.atomic_json(
                root / "receipts/secrets.json",
                {
                    "schema_version": 1,
                    "status": "ready",
                    "source_commit": source_commit,
                    "database_secret": "commonthing-experiment-b-database",
                    "runtime_secret": "weltgewebe-runtime",
                    "registry_secret": "commonthing-experiment-b-registry",
                    "database_source_sha256": runtime.sha256_file(database_path),
                    "registry_source_sha256": runtime.sha256_file(registry_path),
                    "secret_values_recorded": False,
                },
            )

            path_type = type(database_path)
            original_read_bytes = path_type.read_bytes
            database_swapped = False

            def read_bytes_and_swap(path: Path) -> bytes:
                nonlocal database_swapped
                captured = original_read_bytes(path)
                if path == database_path and not database_swapped:
                    database_swapped = True
                    runtime.atomic_json(database_path, changed_database)
                return captured

            with mock.patch.object(
                path_type,
                "read_bytes",
                autospec=True,
                side_effect=read_bytes_and_swap,
            ):
                self.assertEqual(
                    runtime._verified_database_client_identity(
                        root, source_commit
                    ),
                    ("proof_user", "proof_database"),
                )

            self.assertTrue(database_swapped)
            self.assertEqual(
                runtime._database_client_identity(root),
                ("changed_user", "changed_database"),
            )
            secret_source = inspect.getsource(
                runtime._expected_live_secret_values
            )
            self.assertNotIn("sha256_file(database_path)", secret_source)
            self.assertNotIn("sha256_file(registry_path)", secret_source)

    def test_secret_material_returns_the_exact_source_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "secrets").mkdir()
            database_path = root / "secrets/database.json"
            existing_bytes = (
                b'{\n'
                b'  "database": "proof_database",\n'
                b'  "password": "proof_password",\n'
                b'  "username": "proof_user"\n'
                b'}\n'
            )
            database_path.write_bytes(existing_bytes)

            existing, captured = runtime.ensure_secret_material(root)
            self.assertEqual(captured, existing_bytes)
            self.assertEqual(
                existing,
                {
                    "username": "proof_user",
                    "database": "proof_database",
                    "password": "proof_password",
                },
            )

            database_path.unlink()
            created, created_bytes = runtime.ensure_secret_material(root)
            self.assertEqual(database_path.read_bytes(), created_bytes)
            self.assertEqual(
                json.loads(created_bytes.decode("utf-8")),
                created,
            )

            invalid_payloads = (
                {
                    "username": "proof_user",
                    "database": "proof_database",
                    "password": "proof_password",
                    "extra": "forbidden",
                },
                {
                    "username": 7,
                    "database": "proof_database",
                    "password": "proof_password",
                },
                {
                    "username": "proof_user",
                    "database": "",
                    "password": "proof_password",
                },
            )
            for payload in invalid_payloads:
                with self.subTest(payload=payload):
                    runtime.atomic_json(database_path, payload)
                    before = database_path.read_bytes()
                    with self.assertRaisesRegex(
                        runtime.RuntimeErrorEB,
                        "database Secret source material is invalid",
                    ):
                        runtime.ensure_secret_material(root)
                    self.assertEqual(database_path.read_bytes(), before)

            target_path = root / "database-target.json"
            runtime.atomic_json(
                target_path,
                {
                    "username": "proof_user",
                    "database": "proof_database",
                    "password": "proof_password",
                },
            )
            database_path.unlink()
            database_path.symlink_to(target_path)
            with self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "database Secret source material is invalid",
            ):
                runtime.ensure_secret_material(root)

    def test_secret_material_rejects_symlink_swap_at_open(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "secrets").mkdir()
            database_path = root / "secrets/database.json"
            target_path = root / "database-target.json"
            payload = {
                "username": "proof_user",
                "database": "proof_database",
                "password": "proof_password",
            }
            runtime.atomic_json(database_path, payload)
            runtime.atomic_json(target_path, payload)
            real_open = runtime.os.open
            swapped = False

            def swap_before_open(candidate, flags, *args, **kwargs):
                nonlocal swapped
                if not swapped and Path(candidate) == database_path:
                    swapped = True
                    database_path.unlink()
                    database_path.symlink_to(target_path)
                return real_open(candidate, flags, *args, **kwargs)

            with mock.patch.object(
                runtime.os, "open", side_effect=swap_before_open
            ):
                with self.assertRaisesRegex(
                    runtime.RuntimeErrorEB,
                    "database Secret source material is invalid",
                ):
                    runtime.ensure_secret_material(root)

            self.assertTrue(swapped)
            self.assertTrue(database_path.is_symlink())

    def test_registry_config_read_rejects_symlink_swap_at_open(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry_config = root / "registry.json"
            replacement = root / "replacement.json"
            registry_config.write_text(
                '{"auths":{"ghcr.io":{"auth":"original"}}}\n',
                encoding="utf-8",
            )
            replacement.write_text(
                '{"auths":{"ghcr.io":{"auth":"replacement"}}}\n',
                encoding="utf-8",
            )
            real_open = runtime.os.open
            swapped = False

            def swap_before_open(
                path: str | Path,
                flags: int,
                *args: object,
                **kwargs: object,
            ) -> int:
                nonlocal swapped
                if Path(path) == registry_config and not swapped:
                    swapped = True
                    registry_config.unlink()
                    registry_config.symlink_to(replacement)
                return real_open(path, flags, *args, **kwargs)

            with mock.patch.object(
                runtime.os,
                "open",
                side_effect=swap_before_open,
            ):
                with self.assertRaisesRegex(
                    runtime.RuntimeErrorEB,
                    "registry config must be a regular external file",
                ):
                    runtime._read_registry_config_bytes(registry_config)

            self.assertTrue(swapped)
            self.assertTrue(registry_config.is_symlink())
        source = inspect.getsource(runtime.inject_secrets)
        self.assertIn(
            "_read_registry_config_bytes(registry_config)",
            source,
        )
        self.assertNotIn("registry_config.read_bytes()", source)

    def test_inject_secrets_rejects_malformed_registry_before_kubernetes_mutation(self) -> None:
        invalid_payloads = (
            [],
            {"auths": []},
            {"auths": {"ghcr.io": []}},
            {"auths": {"ghcr.io": {}}},
            {"auths": {"ghcr.io": {"auth": ""}}},
            {"auths": {"ghcr.io": {"auth": "%%%"}}},
            {
                "auths": {
                    "ghcr.io": {
                        "auth": base64.b64encode(b"no-colon").decode("ascii")
                    }
                }
            },
            {
                "auths": {
                    "ghcr.io": {
                        "auth": base64.b64encode(b":proof-token").decode("ascii")
                    }
                }
            },
            {
                "auths": {
                    "ghcr.io": {
                        "auth": base64.b64encode(b"proof-user:").decode("ascii")
                    }
                }
            },
            {"auths": {"ghcr.io": {"username": "proof-user"}}},
            {"auths": {"ghcr.io": {"password": "proof-token"}}},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "secrets").mkdir()
                registry_config = root / "registry-source.json"
                registry_config.write_text(
                    json.dumps(payload) + "\n",
                    encoding="utf-8",
                )
                source_commit = "d" * 40
                target = {
                    "vm_ip": "192.0.2.11",
                    "kubeconfig_sha256": "e" * 64,
                    "server": "https://192.0.2.11:6443",
                }
                with (
                    mock.patch.object(
                        runtime,
                        "_current_protected_main_commit",
                        return_value=source_commit,
                    ),
                    mock.patch.object(
                        runtime,
                        "_require_kubernetes_target_binding",
                        return_value=None,
                    ),
                    mock.patch.object(
                        runtime,
                        "_kubernetes_target_identity",
                        return_value=target,
                    ),
                    mock.patch.object(
                        runtime, "ensure_secret_material"
                    ) as ensure_secret_material,
                    mock.patch.object(runtime, "kubectl_apply") as kubectl_apply,
                    mock.patch.object(
                        runtime, "_bound_kube_env"
                    ) as bound_kube_env,
                ):
                    with self.assertRaisesRegex(
                        runtime.RuntimeErrorEB,
                        "registry config has no usable ghcr.io credential",
                    ):
                        runtime.inject_secrets(root, registry_config)

                ensure_secret_material.assert_not_called()
                kubectl_apply.assert_not_called()
                bound_kube_env.assert_not_called()
                self.assertFalse((root / "secrets/registry.json").exists())

        valid_payloads = (
            {
                "auths": {
                    "ghcr.io": {
                        "auth": base64.b64encode(
                            b"proof-user:proof-token"
                        ).decode("ascii")
                    }
                }
            },
            {
                "auths": {
                    "ghcr.io": {
                        "username": "proof-user",
                        "password": "proof-token",
                    }
                }
            },
        )
        for payload in valid_payloads:
            with self.subTest(valid_payload=payload):
                auths = payload.get("auths")
                credential = auths.get("ghcr.io") if isinstance(auths, dict) else None
                self.assertIsInstance(credential, dict)

    def test_inject_secrets_rejects_invalid_database_before_kubernetes_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "secrets").mkdir()
            database_path = root / "secrets/database.json"
            registry_config = root / "registry-source.json"
            runtime.atomic_json(
                database_path,
                {
                    "username": ["not", "a", "string"],
                    "database": "proof_database",
                    "password": "proof_password",
                },
            )
            registry_config.write_text(
                '{"auths":{"ghcr.io":{"username":"proof-user","password":"proof-token"}}}\n',
                encoding="utf-8",
            )
            source_commit = "d" * 40
            target = {
                "vm_ip": "192.0.2.11",
                "kubeconfig_sha256": "e" * 64,
                "server": "https://192.0.2.11:6443",
            }
            with (
                mock.patch.object(
                    runtime,
                    "_current_protected_main_commit",
                    return_value=source_commit,
                ),
                mock.patch.object(
                    runtime,
                    "_require_kubernetes_target_binding",
                    return_value=None,
                ),
                mock.patch.object(
                    runtime,
                    "_kubernetes_target_identity",
                    return_value=target,
                ),
                mock.patch.object(runtime, "kubectl_apply") as kubectl_apply,
                mock.patch.object(runtime, "_bound_kube_env") as bound_kube_env,
            ):
                with self.assertRaisesRegex(
                    runtime.RuntimeErrorEB,
                    "database Secret source material is invalid",
                ):
                    runtime.inject_secrets(root, registry_config)

            kubectl_apply.assert_not_called()
            bound_kube_env.assert_not_called()
            self.assertFalse((root / "secrets/registry.json").exists())

            target_path = root / "database-target.json"
            runtime.atomic_json(
                target_path,
                {
                    "username": "proof_user",
                    "database": "proof_database",
                    "password": "proof_password",
                },
            )
            database_path.unlink()
            database_path.symlink_to(target_path)
            with (
                mock.patch.object(
                    runtime,
                    "_current_protected_main_commit",
                    return_value=source_commit,
                ),
                mock.patch.object(
                    runtime,
                    "_require_kubernetes_target_binding",
                    return_value=None,
                ),
                mock.patch.object(
                    runtime,
                    "_kubernetes_target_identity",
                    return_value=target,
                ),
                mock.patch.object(runtime, "kubectl_apply") as kubectl_apply,
                mock.patch.object(runtime, "_bound_kube_env") as bound_kube_env,
            ):
                with self.assertRaisesRegex(
                    runtime.RuntimeErrorEB,
                    "database Secret source material is invalid",
                ):
                    runtime.inject_secrets(root, registry_config)
            kubectl_apply.assert_not_called()
            bound_kube_env.assert_not_called()
            self.assertFalse((root / "secrets/registry.json").exists())

    def test_inject_secrets_receipt_hashes_the_applied_source_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "secrets").mkdir()
            (root / "receipts").mkdir()
            database_path = root / "secrets/database.json"
            registry_state = root / "secrets/registry.json"
            registry_config = root / "registry-source.json"
            original_database = {
                "username": "proof_user",
                "database": "proof_database",
                "password": "proof_password",
            }
            changed_database = {
                "username": "changed_user",
                "database": "changed_database",
                "password": "changed_password",
            }
            runtime.atomic_json(database_path, original_database)
            original_database_bytes = database_path.read_bytes()
            original_registry_bytes = (
                b'{"auths":{"ghcr.io":{"username":"proof-user","password":"proof-original"}}}\n'
            )
            changed_registry_bytes = (
                b'{"auths":{"ghcr.io":{"username":"proof-user","password":"proof-changed"}}}\n'
            )
            registry_config.write_bytes(original_registry_bytes)
            source_commit = "c" * 40
            target = {
                "vm_ip": "192.0.2.10",
                "kubeconfig_sha256": "d" * 64,
                "server": "https://192.0.2.10:6443",
            }
            applied_manifests: list[dict[str, object]] = []

            def capture_apply(_root: Path, manifest: str) -> None:
                applied_manifests.append(json.loads(manifest))

            def swap_sources_before_receipt(*_args: object, **_kwargs: object) -> None:
                runtime.atomic_json(database_path, changed_database)
                runtime.atomic_bytes(registry_state, changed_registry_bytes)

            bound_context = mock.MagicMock()
            bound_context.__enter__.return_value = None
            bound_context.__exit__.return_value = False

            with (
                mock.patch.object(
                    runtime,
                    "_current_protected_main_commit",
                    return_value=source_commit,
                ),
                mock.patch.object(
                    runtime,
                    "_require_kubernetes_target_binding",
                    return_value=None,
                ),
                mock.patch.object(
                    runtime,
                    "_kubernetes_target_identity",
                    return_value=target,
                ),
                mock.patch.object(
                    runtime,
                    "_bound_kube_env",
                    return_value=bound_context,
                ),
                mock.patch.object(runtime, "render_namespaces", return_value="{}"),
                mock.patch.object(
                    runtime,
                    "kubectl_apply",
                    side_effect=capture_apply,
                ),
                mock.patch.object(
                    runtime,
                    "_require_same_kubernetes_target",
                    side_effect=swap_sources_before_receipt,
                ),
            ):
                receipt = runtime.inject_secrets(root, registry_config)

            self.assertEqual(
                receipt["database_source_sha256"],
                hashlib.sha256(original_database_bytes).hexdigest(),
            )
            self.assertEqual(
                receipt["registry_source_sha256"],
                hashlib.sha256(original_registry_bytes).hexdigest(),
            )
            self.assertNotEqual(
                receipt["database_source_sha256"],
                runtime.sha256_file(database_path),
            )
            self.assertNotEqual(
                receipt["registry_source_sha256"],
                runtime.sha256_file(registry_state),
            )

            database_manifest = next(
                manifest
                for manifest in applied_manifests
                if manifest.get("metadata", {}).get("name")
                == "commonthing-experiment-b-database"
            )
            self.assertEqual(
                base64.b64decode(database_manifest["data"]["username"]),
                b"proof_user",
            )
            self.assertEqual(
                base64.b64decode(database_manifest["data"]["database"]),
                b"proof_database",
            )
            registry_manifest = next(
                manifest
                for manifest in applied_manifests
                if manifest.get("metadata", {}).get("name")
                == "commonthing-experiment-b-registry"
            )
            self.assertEqual(
                base64.b64decode(
                    registry_manifest["data"][".dockerconfigjson"]
                ),
                original_registry_bytes,
            )
            source = inspect.getsource(runtime.inject_secrets)
            self.assertNotIn(
                'sha256_file(root / "secrets/database.json")',
                source,
            )
            self.assertNotIn("sha256_file(registry_state)", source)

    def test_database_url_percent_encodes_reserved_components(self) -> None:
        database = {
            "username": "user@name",
            "password": "p:a/s#s%",
            "database": "db/name#one",
        }
        self.assertEqual(
            runtime._database_url(database),
            (
                "postgresql://user%40name:p%3Aa%2Fs%23s%25"
                f"@postgres.{runtime.DATA_NAMESPACE}.svc.cluster.local:5432/"
                "db%2Fname%23one"
            ),
        )
        self.assertIn(
            "database_url = _database_url(db)",
            inspect.getsource(runtime.inject_secrets),
        )
        self.assertIn(
            "database_url = _database_url(database)",
            inspect.getsource(runtime._expected_live_secret_values),
        )


    def test_database_signature_hashes_complete_persisted_domain_and_search_rows(
        self,
    ) -> None:
        source = inspect.getsource(runtime._database_signature)
        self.assertIn("pg_catalog.pg_class", source)
        self.assertIn("pg_catalog.pg_namespace", source)
        self.assertIn("c.relkind IN ('r', 'p')", source)
        self.assertIn("md5(to_jsonb(t)::text)", source)
        self.assertIn("commonthing_signature_sequences", source)
        self.assertIn("pg_catalog.pg_sequence", source)
        self.assertIn("last_value::text, is_called", source)
        self.assertIn('"--schema-only"', source)
        self.assertIn('"--quote-all-identifiers"', source)
        self.assertIn("schema_sha256", source)
        self.assertIn("_run_bound_postgres_client", source)

    def test_database_signature_uses_public_domain_nodes_as_canonical_state(
        self,
    ) -> None:
        payload = {
            "tables": [
                {
                    "schema": "public",
                    "name": "domain_nodes",
                    "rows": 2,
                    "md5": "a" * 32,
                },
                {
                    "schema": "weltgewebe_perf",
                    "name": "domain_nodes",
                    "rows": 2,
                    "md5": "b" * 32,
                },
            ],
            "sequences": [],
        }
        with mock.patch.object(
            runtime,
            "_psql",
            return_value=json.dumps(payload),
        ):
            signature = runtime._database_signature(
                Path("/unused"),
                database_identity=("user", "db"),
            )
        self.assertEqual(signature, payload)

        fixture_only = {
            **payload,
            "tables": [payload["tables"][1]],
        }
        with (
            mock.patch.object(
                runtime,
                "_psql",
                return_value=json.dumps(fixture_only),
            ),
            self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "no canonical domain state",
            ),
        ):
            runtime._database_signature(
                Path("/unused"),
                database_identity=("user", "db"),
            )

    def test_database_runtime_continuity_allows_only_forward_sequence_progress(
        self,
    ) -> None:
        restored = {
            "tables": [
                {
                    "schema": "public",
                    "name": "domain_nodes",
                    "rows": 2,
                    "md5": "a" * 32,
                }
            ],
            "sequences": [
                {
                    "schema": "public",
                    "name": "domain_nodes_id_seq",
                    "last_value": "10",
                    "is_called": True,
                    "start_value": "1",
                    "increment_by": "1",
                    "min_value": "1",
                    "max_value": "9223372036854775807",
                    "cache_size": "1",
                    "cycle": False,
                }
            ],
            "schema_sha256": "b" * 64,
        }
        progressed = json.loads(json.dumps(restored))
        progressed["sequences"][0]["last_value"] = "12"
        runtime._require_database_runtime_continuity(
            restored,
            progressed,
            "test runtime continuity",
        )

        row_drift = json.loads(json.dumps(progressed))
        row_drift["tables"][0]["md5"] = "c" * 32
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB,
            "persisted rows or schema drifted",
        ):
            runtime._require_database_runtime_continuity(
                restored,
                row_drift,
                "test runtime continuity",
            )

        regression = json.loads(json.dumps(progressed))
        regression["sequences"][0]["last_value"] = "9"
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB,
            "sequence position regressed",
        ):
            runtime._require_database_runtime_continuity(
                restored,
                regression,
                "test runtime continuity",
            )

    def test_application_contract_render_uses_sealed_source_commit_tree(
        self,
    ) -> None:
        source = inspect.getsource(runtime._source_commit_kustomize_build)
        self.assertIn("_git_tree_regular_blob_paths", source)
        self.assertIn("_git_blob_bytes", source)
        self.assertIn("_sealed_snapshot_fd", source)
        self.assertIn('"--ro-bind-data"', source)
        self.assertIn('"--tmpfs"', source)
        self.assertIn('"--unshare-all"', source)
        self.assertIn('"--share-net"', source)
        self.assertNotIn("TemporaryDirectory", source)

        application_source = inspect.getsource(
            runtime._source_commit_application_render
        )
        self.assertIn('release.get("source_commit")', application_source)
        self.assertIn("_source_commit_kustomize_build", application_source)
        for helper in (
            runtime._rendered_application_workload_contract,
            runtime._rendered_application_service_contract,
            runtime._rendered_application_pdb_contract,
            runtime._rendered_application_service_account_contract,
        ):
            helper_source = inspect.getsource(helper)
            self.assertIn(
                "_source_commit_application_render(root, release)",
                helper_source,
            )
            self.assertNotIn(
                'run([kustomize, "build", str(APP_OVERLAY)])',
                helper_source,
            )

    def test_recovery_captures_signatures_after_application_quiescence(self) -> None:
        source = inspect.getsource(runtime.recovery_proof)
        api_scale = source.index(
            '_scale_deployment(root, APP_NAMESPACE, "weltgewebe-api", 0)'
        )
        web_scale = source.index(
            '_scale_deployment(root, APP_NAMESPACE, "weltgewebe-web", 0)'
        )
        api_wait = source.index(
            '"app.kubernetes.io/name=weltgewebe-api"', api_scale
        )
        web_wait = source.index(
            '"app.kubernetes.io/name=weltgewebe-web"', web_scale
        )
        db_signature = source.index("before_db = _database_signature(")
        nats_signature = source.index(
            "before_nats = _jetstream_signature("
        )
        dump = source.index('"pg_dump"')
        self.assertLess(api_scale, api_wait)
        self.assertLess(web_scale, web_wait)
        self.assertLess(api_wait, db_signature)
        self.assertLess(web_wait, db_signature)
        self.assertLess(db_signature, nats_signature)
        self.assertLess(nats_signature, dump)

    def test_recovery_proves_restored_signatures_before_flux_resume(self) -> None:
        source = inspect.getsource(runtime.recovery_proof)
        restore = source.index('"pg_restore"')
        nats_restart = source.index(
            '_scale_deployment(root, DATA_NAMESPACE, "nats", 1)',
            restore,
        )
        after_db = source.index(
            "after_db = _database_signature(",
            nats_restart,
        )
        after_nats = source.index(
            "after_nats = _jetstream_signature(",
            after_db,
        )
        db_compare = source.index(
            "if after_db != before_db:",
            after_nats,
        )
        nats_compare = source.index(
            "if after_nats != before_nats:",
            db_compare,
        )
        data_resume = source.index(
            '_flux_resume(root, "commonthing-experiment-b-data")',
            nats_compare,
        )
        app_resume = source.index(
            '_flux_resume(root, "commonthing-experiment-b-app")',
            data_resume,
        )
        api_wait = source.index(
            '_wait_deployment(root, APP_NAMESPACE, "weltgewebe-api", 480)',
            app_resume,
        )
        event_quiescence = source.index(
            "_wait_event_pipeline_quiescent(",
            api_wait,
        )
        database_post_resume = source.index(
            "database_post_resume = _database_signature(",
            event_quiescence,
        )
        runtime_continuity = source.index(
            "_require_database_runtime_continuity(",
            database_post_resume,
        )
        completion = source.index(
            '"recovery completion"',
            runtime_continuity,
        )
        self.assertLess(restore, nats_restart)
        self.assertLess(nats_restart, after_db)
        self.assertLess(after_db, after_nats)
        self.assertLess(after_nats, db_compare)
        self.assertLess(db_compare, nats_compare)
        self.assertLess(nats_compare, data_resume)
        self.assertLess(data_resume, app_resume)
        self.assertLess(app_resume, api_wait)
        self.assertLess(api_wait, event_quiescence)
        self.assertLess(event_quiescence, database_post_resume)
        self.assertLess(database_post_resume, runtime_continuity)
        self.assertLess(runtime_continuity, completion)
        self.assertNotIn(
            "_flux_resume(",
            source[after_db:data_resume],
        )

    def test_recovery_binds_postgres_clients_to_validated_source_pod(
        self,
    ) -> None:
        source = inspect.getsource(runtime.recovery_proof)
        before_signature = source.index(
            "before_db = _database_signature("
        )
        dump = source.index('"pg_dump"')
        restore = source.index('"pg_restore"')
        after_signature = source.index(
            "after_db = _database_signature("
        )
        self.assertLess(before_signature, dump)
        self.assertLess(dump, restore)
        self.assertLess(restore, after_signature)
        self.assertGreaterEqual(
            source.count("_run_bound_postgres_client("),
            2,
        )
        self.assertIn(
            "postgres_binding=postgres_signature_before",
            source,
        )
        self.assertIn(
            "postgres_binding=postgres_signature_after",
            source,
        )
        self.assertIn("postgres_dump_snapshot_fd", source)
        self.assertIn("_create_sealed_snapshot_fd(", source)
        self.assertNotIn('"deployment/postgres"', source)

        binding_source = inspect.getsource(
            runtime._require_postgres_runtime_binding
        )
        client_source = inspect.getsource(
            runtime._run_bound_postgres_client
        )
        container_source = inspect.getsource(
            runtime._run_bound_container_command
        )
        self.assertIn('"container_id": container_id', binding_source)
        self.assertIn('"crictl"', container_source)
        self.assertIn('"exec"', container_source)
        self.assertIn(
            '_postgres_runtime_binding_identity(current)',
            client_source,
        )
        self.assertIn(
            'expected_identity["container_id"]',
            client_source,
        )

    def test_recovery_waits_for_nats_quiescence_before_pvc_backup(self) -> None:
        source = inspect.getsource(runtime.recovery_proof)
        scaled = source.index('_scale_deployment(root, DATA_NAMESPACE, "nats", 0)')
        waited = source.index("_wait_pods_absent(", scaled)
        transfer = source.index(
            '"commonthing-experiment-b-nats-backup",'
        )
        self.assertLess(scaled, waited)
        self.assertLess(waited, transfer)

    def test_nats_transfer_pod_uses_only_explicit_canonical_image(self) -> None:
        root = Path("/tmp/experiment-b-nats-transfer-test")
        image = "nats@sha256:" + "a" * 64
        waited = runtime.subprocess.CompletedProcess(
            ["kubectl"], 0, stdout="", stderr=""
        )
        captured: dict[str, dict] = {}

        def capture_apply(_root, payload):
            captured["manifest"] = json.loads(payload)

        def pod_readback(_root, arguments):
            self.assertEqual(
                arguments,
                [
                    "-n",
                    runtime.DATA_NAMESPACE,
                    "get",
                    "pod",
                    "transfer",
                ],
            )
            manifest = captured["manifest"]
            live_spec = json.loads(json.dumps(manifest["spec"]))
            live_spec["containers"][0]["resources"] = {}
            return {
                "metadata": {
                    "name": "transfer",
                    "namespace": runtime.DATA_NAMESPACE,
                },
                "spec": live_spec,
                "status": {
                    "phase": "Running",
                    "conditions": [
                        {"type": "Ready", "status": "True"}
                    ],
                    "containerStatuses": [
                        {
                            "name": "transfer",
                            "ready": True,
                            "state": {
                                "running": {
                                    "startedAt": "2026-09-29T00:00:00Z"
                                }
                            },
                            "imageID": (
                                "containerd://sha256:" + "a" * 64
                            ),
                            "containerID": (
                                "containerd://" + "b" * 64
                            ),
                        }
                    ],
                },
            }

        with (
            mock.patch.object(
                runtime,
                "kubectl_apply",
                side_effect=capture_apply,
            ) as apply_manifest,
            mock.patch.object(
                runtime,
                "_kubectl",
                return_value=waited,
            ),
            mock.patch.object(
                runtime,
                "_kubectl_json",
                side_effect=pod_readback,
            ),
        ):
            binding = runtime._nats_transfer_pod(
                root,
                "transfer",
                image,
            )

        manifest = captured["manifest"]
        self.assertEqual(
            manifest["spec"]["containers"][0]["image"],
            image,
        )
        self.assertEqual(
            manifest["spec"]["containers"][0]["imagePullPolicy"],
            "IfNotPresent",
        )
        self.assertEqual(
            manifest["spec"]["containers"][0]["resources"],
            {},
        )
        self.assertEqual(
            binding["container_id"],
            "containerd://" + "b" * 64,
        )
        self.assertEqual(apply_manifest.call_count, 1)
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB,
            "source-commit-bound immutable image",
        ):
            runtime._nats_transfer_pod(
                root,
                "transfer",
                "nats:latest",
            )


    def test_nats_transfer_pod_cleans_up_post_apply_validation_failure(
        self,
    ) -> None:
        root = Path("/tmp/experiment-b-nats-transfer-cleanup-test")
        image = "nats@sha256:" + "a" * 64
        with (
            mock.patch.object(runtime, "kubectl_apply") as apply_manifest,
            mock.patch.object(
                runtime,
                "_kubectl",
                side_effect=runtime.RuntimeErrorEB("ready wait failed"),
            ),
            mock.patch.object(runtime, "_delete_pod") as delete_pod,
            self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "ready wait failed",
            ),
        ):
            runtime._nats_transfer_pod(
                root,
                "transfer",
                image,
            )
        apply_manifest.assert_called_once()
        delete_pod.assert_called_once_with(
            root,
            runtime.DATA_NAMESPACE,
            "transfer",
        )

    def test_recovery_binds_nats_transfer_to_source_commit_contract(self) -> None:
        source = inspect.getsource(runtime.recovery_proof)
        source_contract = source.index(
            "nats_source_contract = _source_commit_data_deployment_contract("
        )
        live_contract = source.index(
            "nats_runtime_binding = _require_nats_runtime_binding("
        )
        scale_down = source.index(
            '_scale_deployment(root, DATA_NAMESPACE, "nats", 0)'
        )
        backup = source.index(
            '"commonthing-experiment-b-nats-backup",'
        )
        restore = source.index(
            '"commonthing-experiment-b-nats-restore",'
        )
        self.assertLess(source_contract, live_contract)
        self.assertLess(live_contract, scale_down)
        self.assertLess(scale_down, backup)
        self.assertLess(backup, restore)
        self.assertIn("source_commit=source_commit", source)
        self.assertIn(
            'nats_backup_transfer["container_id"]',
            source,
        )
        self.assertIn(
            'nats_restore_transfer["container_id"]',
            source,
        )
        self.assertGreaterEqual(
            source.count("_run_bound_container_command("),
            2,
        )
        self.assertGreaterEqual(
            source.count("nats_transfer_image"),
            6,
        )
        self.assertNotIn(
            '"NATS restore pod cannot bind the live immutable image"',
            inspect.getsource(runtime._nats_transfer_pod),
        )

    def test_recovery_waits_for_postgres_quiescence_before_pvc_delete(self) -> None:
        source = inspect.getsource(runtime.recovery_proof)
        scaled = source.index('_scale_deployment(root, DATA_NAMESPACE, "postgres", 0)')
        waited = source.index(
            '"app.kubernetes.io/name=postgres"',
            scaled,
        )
        pvc_delete = source.index('"delete", "pvc"', waited)
        self.assertLess(scaled, waited)
        self.assertLess(waited, pvc_delete)

    def test_recovery_compares_complete_jetstream_signature(self) -> None:
        source = inspect.getsource(runtime.recovery_proof)
        self.assertIn("if after_nats != before_nats:", source)
        self.assertIn("stream/message-store/durable-consumer continuity signature", source)

    def test_delete_pod_fails_closed_and_verifies_absence(self) -> None:
        root = Path("/tmp/experiment-b-delete-pod-test")
        deleted = runtime.subprocess.CompletedProcess(
            ["kubectl"], 0, stdout="pod/deleted\n", stderr=""
        )
        absent = runtime.subprocess.CompletedProcess(
            ["kubectl"], 0, stdout="", stderr=""
        )
        with mock.patch.object(runtime, "_kubectl", side_effect=[deleted, absent]) as kubectl:
            runtime._delete_pod(root, "commonthing-data", "transfer")
        self.assertEqual(kubectl.call_count, 2)
        self.assertEqual(
            kubectl.call_args_list[0],
            mock.call(
                root,
                [
                    "-n", "commonthing-data", "delete", "pod", "transfer",
                    "--ignore-not-found=true", "--wait=true", "--timeout=2m",
                ],
                timeout=150,
            ),
        )
        self.assertEqual(
            kubectl.call_args_list[1],
            mock.call(
                root,
                [
                    "-n", "commonthing-data", "get", "pod", "transfer",
                    "--ignore-not-found=true", "-o", "name",
                ],
                timeout=30,
            ),
        )
        self.assertNotIn("check=False", inspect.getsource(runtime._delete_pod))

        with mock.patch.object(
            runtime, "_kubectl", side_effect=runtime.RuntimeErrorEB("delete failed")
        ) as kubectl:
            with self.assertRaisesRegex(runtime.RuntimeErrorEB, "delete failed"):
                runtime._delete_pod(root, "commonthing-data", "transfer")
            self.assertEqual(kubectl.call_count, 1)

        present = runtime.subprocess.CompletedProcess(
            ["kubectl"], 0, stdout="pod/transfer\n", stderr=""
        )
        with mock.patch.object(runtime, "_kubectl", side_effect=[deleted, present]):
            with self.assertRaisesRegex(runtime.RuntimeErrorEB, "still exists"):
                runtime._delete_pod(root, "commonthing-data", "transfer")

    def test_recovery_deletes_restore_transfer_before_nats_restart(self) -> None:
        source = inspect.getsource(runtime.recovery_proof)
        restore = source.index(
            '"commonthing-experiment-b-nats-restore",'
        )
        deleted = source.index("_delete_pod(", restore)
        restarted = source.index(
            '_scale_deployment(root, DATA_NAMESPACE, "nats", 1)',
            deleted,
        )
        self.assertLess(restore, deleted)
        self.assertLess(deleted, restarted)

    def test_libvirt_absence_query_fails_closed(self) -> None:
        failed = runtime.subprocess.CompletedProcess(
            ["virsh"], 1, stdout="", stderr="permission denied"
        )
        with mock.patch.object(runtime, "run", return_value=failed):
            with self.assertRaises(runtime.RuntimeErrorEB):
                runtime._libvirt_resource_present("domain", runtime.VM_NAME)
            with self.assertRaises(runtime.RuntimeErrorEB):
                runtime._libvirt_volume_present(runtime.POOL_NAME, runtime.VOLUME_NAME)

    def test_inject_secrets_cli_does_not_forward_secret_tainted_return(self) -> None:
        source = inspect.getsource(runtime.main)
        branch = source.split('elif args.command == "inject-secrets":', 1)[1].split(
            'elif args.command == "apply-release":', 1
        )[0]
        self.assertIn("inject_secrets(", branch)
        self.assertNotIn("result = inject_secrets(", branch)
        self.assertIn('"receipt": "receipts/secrets.json"', branch)

    def test_status_cli_does_not_forward_secret_tainted_return(self) -> None:
        source = inspect.getsource(runtime.main)
        branch = source.split('elif args.command == "status":', 1)[1].split(
            'elif args.command == "teardown":', 1
        )[0]
        self.assertIn("status(root)", branch)
        self.assertNotIn("result = status(root)", branch)
        self.assertIn('"receipt": "receipts/status.json"', branch)

    def test_cli_exposes_full_t085_proof_sequence(self) -> None:
        parser = runtime.parser()
        commands = {
            action.dest: set(action.choices or {})
            for action in parser._actions
            if action.dest == "command"
        }["command"]
        self.assertTrue(
            {
                "seed-t048-fixture",
                "semantic-activate",
                "functional-readback",
                "t048-load-proof",
                "recovery-proof",
                "portability-report",
                "status",
                "teardown",
            }.issubset(commands)
        )

    def test_runtime_source_has_no_production_mutation_targets(self) -> None:
        source = Path(runtime.__file__).read_text(encoding="utf-8")
        self.assertNotIn("ssh commonserver", source)
        self.assertNotIn("commonthing.net/api", source)
        self.assertNotIn("kubectl config use-context", source)
        self.assertNotIn("get.k3s.io", source)


    def test_t048_performance_outputs_do_not_follow_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = parent / "state"
            root.mkdir()
            performance = root / "performance"
            performance.mkdir()
            sentinel = parent / "sentinel"
            sentinel.write_text("sentinel", encoding="utf-8")
            output = performance / "metrics-before.prom"
            output.symlink_to(sentinel)

            runtime._write_performance_text(
                root,
                "metrics-before.prom",
                "metric 1\n",
            )

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "sentinel")
            self.assertFalse(output.is_symlink())
            self.assertEqual(output.read_text(encoding="utf-8"), "metric 1\n")
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)

            stderr_target = parent / "stderr-sentinel"
            stderr_target.write_text("stderr-sentinel", encoding="utf-8")
            stderr_path = performance / "k6.stderr"
            stderr_path.symlink_to(stderr_target)
            with runtime._open_performance_text_output(
                root,
                "k6.stderr",
            ) as handle:
                handle.write("k6 failed\n")

            self.assertEqual(
                stderr_target.read_text(encoding="utf-8"),
                "stderr-sentinel",
            )
            self.assertFalse(stderr_path.is_symlink())
            self.assertEqual(
                stderr_path.read_text(encoding="utf-8"),
                "k6 failed\n",
            )
            self.assertEqual(stderr_path.stat().st_mode & 0o777, 0o600)

        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = parent / "state"
            root.mkdir()
            outside = parent / "outside"
            outside.mkdir()
            (root / "performance").symlink_to(
                outside,
                target_is_directory=True,
            )

            with self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "performance state directory is unsafe",
            ):
                runtime._write_performance_text(
                    root,
                    "metrics-before.prom",
                    "metric 1\n",
                )
            self.assertEqual(list(outside.iterdir()), [])

        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = parent / "state"
            root.mkdir()
            performance = root / "performance"
            performance.mkdir()
            retained = root / "performance-retained"
            outside = parent / "outside"
            outside.mkdir()
            performance_fd = runtime._open_performance_directory(root)
            try:
                performance.rename(retained)
                performance.symlink_to(
                    outside,
                    target_is_directory=True,
                )
                bound_output = (
                    Path(f"/proc/self/fd/{performance_fd}") / "fixture-proof"
                )
                bound_output.write_text("bound\n", encoding="utf-8")
            finally:
                runtime.os.close(performance_fd)

            self.assertEqual(
                (retained / "fixture-proof").read_text(encoding="utf-8"),
                "bound\n",
            )
            self.assertEqual(list(outside.iterdir()), [])

        seed_source = inspect.getsource(runtime.seed_t048_fixture)
        self.assertIn(
            'Path(f"/proc/self/fd/{performance_fd}")',
            seed_source,
        )
        self.assertIn(
            '"manifest": str(canonical_manifest)',
            seed_source,
        )

        load_source = inspect.getsource(runtime.t048_load_proof)
        self.assertNotIn("metrics_before_path.write_text", load_source)
        self.assertNotIn("metrics_after_path.write_text", load_source)
        self.assertNotIn('stderr_path.open("w"', load_source)
        self.assertIn(
            '_write_performance_text(root, "metrics-before.prom"',
            load_source,
        )
        self.assertIn(
            '_write_performance_text(root, "metrics-after.prom"',
            load_source,
        )
        self.assertIn(
            '_open_performance_text_output(root, "k6.stderr")',
            load_source,
        )

        port_forward_source = inspect.getsource(runtime._start_api_port_forward)
        self.assertNotIn('.open("w"', port_forward_source)
        self.assertEqual(
            port_forward_source.count("_open_performance_text_output"),
            2,
        )


class ExperimentBVMSubstrateTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.patch(
            "RETIREMENT_RECEIPT",
            self.root / "experiment-b-retirement.json",
        )
        self.pool = self.root / "pool"
        self.pool.mkdir()
        self.base_bytes = b"pinned Ubuntu image fixture"
        self.disk = self.pool / runtime.VOLUME_NAME
        self.base = self.pool / runtime.BASE_VOLUME
        self.disk.write_bytes(b"mutable guest overlay")
        self.base.write_bytes(self.base_bytes)
        self.config = runtime.load_config()
        self.config["vm"]["image"]["sha256"] = hashlib.sha256(self.base_bytes).hexdigest()
        self.commit = "a" * 40
        self.domain_present = True
        self.domain_active = True
        self.pool_present = True
        self.patch("POOL_TARGET", self.pool)
        self.patch("load_config", return_value=self.config)
        self.main = self.patch("_current_protected_main_commit", return_value=self.commit)
        self.patch(
            "_git_blob_sha256",
            side_effect=lambda _source_commit, path: runtime.sha256_file(path),
        )
        expected = vm_substrate_fixture()
        self.domain_uuid = expected["uuid"]
        self.pool_uuid = expected["pool_uuid"]
        self.xml = {
            "live": f"""<domain type='kvm' id='7'>
              <name>{runtime.VM_NAME}</name><uuid>{expected['uuid']}</uuid>
              <vcpu current='6'>6</vcpu>
              <memory unit='KiB'>12582912</memory>
              <currentMemory unit='KiB'>12582912</currentMemory>
              <devices>
                <disk type='file' device='disk'>
                  <driver name='qemu' type='qcow2'/><source file='{self.disk}'/>
                  <backingStore type='file'><format type='qcow2'/>
                    <source file='{self.base}'/><backingStore/>
                  </backingStore><target dev='vda' bus='virtio'/>
                </disk>
                <disk type='file' device='cdrom'><readonly/></disk>
                <interface type='network'><mac address='{expected['mac']}'/>
                  <source network='default' bridge='virbr0'/>
                </interface>
              </devices>
            </domain>""",
            "network": f"""<network><name>default</name>
              <uuid>{expected['network_uuid']}</uuid><bridge name='virbr0'/>
              <forward mode='nat'/></network>""",
            "pool": f"""<pool type='dir'><name>{runtime.POOL_NAME}</name>
              <uuid>{expected['pool_uuid']}</uuid><target><path>{self.pool}</path></target>
            </pool>""",
            "disk": f"""<volume type='file'><name>{runtime.VOLUME_NAME}</name>
              <key>{self.disk}</key><capacity unit='bytes'>{60 * 1024**3}</capacity>
              <target><path>{self.disk}</path><format type='qcow2'/></target>
              <backingStore><path>{self.base}</path><format type='qcow2'/></backingStore>
            </volume>""",
            "base": f"""<volume type='file'><name>{runtime.BASE_VOLUME}</name>
              <key>{self.base}</key><target><path>{self.base}</path>
              <format type='qcow2'/></target></volume>""",
        }
        self.xml["inactive"] = self.xml["live"].replace(" id='7'", "")
        self.qmp = {"return": [{
            "removable": False,
            "inserted": {"image": {
                "filename": str(self.disk), "format": "qcow2", "virtual-size": 60 * 1024**3,
                "backing-image": {"filename": str(self.base), "format": "qcow2"},
            }},
        }, {"removable": True, "inserted": {"image": {"format": "raw"}}}]}
        self.node = {
            "apiVersion": "v1",
            "kind": "Node",
            "metadata": {"name": runtime.VM_NAME},
            "status": {
                "conditions": [{"type": "Ready", "status": "True"}],
                "nodeInfo": {
                    "kubeletVersion": self.config["kubernetes"]["version"],
                    "osImage": "Ubuntu 24.04.3 LTS",
                },
            },
        }
        self.node_inventory = {
            "apiVersion": "v1",
            "kind": "NodeList",
            "items": [self.node],
        }
        self.cilium_chart = f"cilium-{self.config['cilium']['chart_version']}"
        self.cilium_values = {
            "gatewayAPI": {"enabled": True},
            "kubeProxyReplacement": True,
            "hubble": {"relay": {"enabled": True}},
        }
        self.cilium_config_contract = {
            "data": {
                "enable-policy": "default",
                "enable-gateway-api": "true",
                "kube-proxy-replacement": "true",
            },
            "binaryData": {},
            "immutable": False,
        }
        self.cilium_config = {
            "metadata": {
                "name": "cilium-config",
                "namespace": "kube-system",
            },
            **json.loads(json.dumps(self.cilium_config_contract)),
        }
        self.cilium_expected_images = {
            "containers": {
                "cilium-agent": "quay.io/cilium/cilium:v1.19.5",
            },
            "init_containers": {
                "config": "quay.io/cilium/startup-script:1",
            },
        }
        self.cilium_expected_operator_images = {
            "containers": {
                "cilium-operator": "quay.io/cilium/operator-generic:v1.19.5",
            },
            "init_containers": {},
        }
        self.cilium_expected_relay_images = {
            "containers": {
                "hubble-relay": "quay.io/cilium/hubble-relay:v1.19.5",
            },
            "init_containers": {},
        }
        self.cilium_operator = {
            "metadata": {
                "name": "cilium-operator",
                "namespace": "kube-system",
                "generation": 1,
            },
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"io.cilium/app": "operator"}},
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": name,
                                "image": image,
                            }
                            for name, image in self.cilium_expected_operator_images[
                                "containers"
                            ].items()
                        ],
                    }
                },
            },
            "status": {
                "observedGeneration": 1,
                "replicas": 1,
                "updatedReplicas": 1,
                "readyReplicas": 1,
                "availableReplicas": 1,
                "unavailableReplicas": 0,
                "conditions": [{"type": "Available", "status": "True"}],
            },
        }
        self.cilium_relay = {
            "metadata": {
                "name": "hubble-relay",
                "namespace": "kube-system",
                "generation": 1,
            },
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"k8s-app": "hubble-relay"}},
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": name,
                                "image": image,
                            }
                            for name, image in self.cilium_expected_relay_images[
                                "containers"
                            ].items()
                        ],
                    }
                },
            },
            "status": {
                "observedGeneration": 1,
                "replicas": 1,
                "updatedReplicas": 1,
                "readyReplicas": 1,
                "availableReplicas": 1,
                "unavailableReplicas": 0,
                "conditions": [{"type": "Available", "status": "True"}],
            },
        }
        self.cilium_daemonset = {
            "metadata": {
                "name": "cilium",
                "namespace": "kube-system",
                "generation": 1,
            },
            "spec": {
                "selector": {"matchLabels": {"k8s-app": "cilium"}},
                "template": {
                    "spec": {
                        "initContainers": [
                            {
                                "name": name,
                                "image": image,
                            }
                            for name, image in self.cilium_expected_images[
                                "init_containers"
                            ].items()
                        ],
                        "containers": [
                            {
                                "name": name,
                                "image": image,
                            }
                            for name, image in self.cilium_expected_images[
                                "containers"
                            ].items()
                        ],
                    }
                }
            },
            "status": {
                "observedGeneration": 1,
                "desiredNumberScheduled": 1,
                "updatedNumberScheduled": 1,
                "numberReady": 1,
                "numberAvailable": 1,
                "numberUnavailable": 0,
            },
        }
        self.flux_controller_deployments = []
        self.flux_controller_pods = []
        self.flux_expected_contract = {}
        self.kube_proxy_daemonsets = []
        self.kube_proxy_pods = []
        runtime_binding = self.config["runtime_binding"]
        self.runtime_config_map = {
            "metadata": {
                "namespace": runtime.APP_NAMESPACE,
                "name": "weltgewebe-runtime",
            },
            "data": json.loads(json.dumps(runtime_binding["config_map_data"])),
        }
        self.network_policies = [
            {
                "metadata": {
                    "namespace": runtime.APP_NAMESPACE,
                    "name": name,
                },
                "spec": json.loads(json.dumps(spec)),
            }
            for name, spec in runtime_binding["network_policy_specs"].items()
        ]
        data_network_specs = runtime._versioned_network_policy_specs(
            runtime.CLUSTER / "data/network-policy.yaml",
            runtime.DATA_NAMESPACE,
        )
        self.data_network_policies = [
            {
                "metadata": {
                    "namespace": runtime.DATA_NAMESPACE,
                    "name": name,
                },
                "spec": json.loads(json.dumps(spec)),
            }
            for name, spec in data_network_specs.items()
        ]
        self.cilium_network_policies = [
            {
                "metadata": {
                    "namespace": runtime.APP_NAMESPACE,
                    "name": name,
                },
                "spec": json.loads(json.dumps(spec)),
            }
            for name, spec in runtime_binding[
                "cilium_network_policy_specs"
            ].items()
        ]
        def fixture_runtime_image_id(image: str) -> str:
            if "@" in image:
                digest = image.rsplit("@", 1)[1]
            else:
                digest = "sha256:" + hashlib.sha256(image.encode("utf-8")).hexdigest()
            return "containerd://" + digest

        def workload_pod(
            namespace: str,
            workload: str,
            ordinal: int,
            labels: dict[str, str],
            images: dict[str, dict[str, str]],
        ) -> dict:
            return {
                "metadata": {
                    "name": f"{workload}-{ordinal}",
                    "namespace": namespace,
                    "uid": f"{namespace}-{workload}-{ordinal}-uid",
                    "labels": json.loads(json.dumps(labels)),
                },
                "spec": {
                    "containers": [
                        {"name": name, "image": image}
                        for name, image in images["containers"].items()
                    ],
                    "initContainers": [
                        {"name": name, "image": image}
                        for name, image in images["init_containers"].items()
                    ],
                },
                "status": {
                    "phase": "Running",
                    "podIP": (
                        f"10.42.{(sum(workload.encode('utf-8')) % 200) + 1}."
                        f"{ordinal + 10}"
                    ),
                    "conditions": [{"type": "Ready", "status": "True"}],
                    "containerStatuses": [
                        {
                            "name": name,
                            "ready": True,
                            "state": {
                                "running": {"startedAt": "2026-09-27T00:00:00Z"}
                            },
                            "imageID": fixture_runtime_image_id(image),
                            "containerID": (
                                "containerd://"
                                + hashlib.sha256(
                                    f"{workload}:{ordinal}:{name}".encode(
                                        "utf-8"
                                    )
                                ).hexdigest()
                            ),
                        }
                        for name, image in images["containers"].items()
                    ],
                    "initContainerStatuses": [
                        {
                            "name": name,
                            "ready": False,
                            "state": {
                                "terminated": {
                                    "exitCode": 0,
                                    "finishedAt": "2026-09-27T00:00:00Z",
                                }
                            },
                            "imageID": fixture_runtime_image_id(image),
                        }
                        for name, image in images["init_containers"].items()
                    ],
                },
            }

        for name in sorted(runtime.EXPECTED_FLUX_CONTROLLERS):
            images = {
                "containers": {
                    "manager": f"ghcr.io/fluxcd/{name}:vfixture",
                },
                "init_containers": {},
            }
            labels = {"app.kubernetes.io/name": name}
            deployment = {
                "metadata": {
                    "name": name,
                    "namespace": "flux-system",
                    "generation": 1,
                },
                "spec": {
                    "replicas": 1,
                    "selector": {
                        "matchLabels": json.loads(json.dumps(labels))
                    },
                    "template": {
                        "spec": {
                            "containers": [
                                {"name": key, "image": value}
                                for key, value in images["containers"].items()
                            ],
                            "initContainers": [],
                        }
                    },
                },
                "status": {
                    "observedGeneration": 1,
                    "replicas": 1,
                    "updatedReplicas": 1,
                    "readyReplicas": 1,
                    "availableReplicas": 1,
                    "unavailableReplicas": 0,
                    "conditions": [{"type": "Available", "status": "True"}],
                },
            }
            self.flux_controller_deployments.append(deployment)
            self.flux_expected_contract[name] = runtime._flux_deployment_contract(
                deployment,
                f"fixture Flux Deployment {name}",
            )
            self.flux_controller_pods.append(
                workload_pod(
                    "flux-system",
                    name,
                    0,
                    labels,
                    images,
                )
            )

        self.data_deployments = {}
        self.data_pods = {}
        for name in ("postgres", "nats"):
            manifest_path = runtime.CLUSTER / f"data/{name}.yaml"
            documents = [
                document
                for document in yaml.safe_load_all(
                    manifest_path.read_text(encoding="utf-8")
                )
                if isinstance(document, dict)
            ]
            source_deployment = next(
                document
                for document in documents
                if document.get("kind") == "Deployment"
                and document.get("metadata", {}).get("name") == name
            )
            expected = runtime._versioned_data_deployment_contract(
                manifest_path, name
            )
            live_spec = json.loads(
                json.dumps(source_deployment["spec"])
            )
            self.data_deployments[name] = {
                "metadata": {
                    "name": name,
                    "namespace": runtime.DATA_NAMESPACE,
                    "generation": 1,
                },
                "spec": live_spec,
                "status": {
                    "observedGeneration": 1,
                    "replicas": expected["replicas"],
                    "updatedReplicas": expected["replicas"],
                    "readyReplicas": expected["replicas"],
                    "availableReplicas": expected["replicas"],
                    "unavailableReplicas": 0,
                    "conditions": [{"type": "Available", "status": "True"}],
                },
            }
            template = live_spec["template"]
            template_metadata = template.get("metadata", {})
            template_labels = json.loads(
                json.dumps(template_metadata.get("labels", {}))
            )
            template_annotations = json.loads(
                json.dumps(template_metadata.get("annotations", {}))
            )
            self.data_pods[name] = []
            for index in range(expected["replicas"]):
                pod = workload_pod(
                    runtime.DATA_NAMESPACE,
                    name,
                    index,
                    template_labels,
                    expected["images"],
                )
                pod["metadata"]["annotations"] = template_annotations
                pod["spec"] = json.loads(
                    json.dumps(template["spec"])
                )
                self.data_pods[name].append(pod)

        self.data_services = {}
        for name in ("postgres", "nats"):
            expected_service = runtime._versioned_data_service_contract(
                runtime.CLUSTER / f"data/{name}.yaml", name
            )
            self.data_services[name] = {
                "metadata": {
                    "name": name,
                    "namespace": runtime.DATA_NAMESPACE,
                    "uid": f"{name}-service-uid",
                    "resourceVersion": (
                        "101" if name == "postgres" else "102"
                    ),
                },
                "spec": json.loads(
                    json.dumps(expected_service["spec"])
                ),
            }

        postgres_pod = self.data_pods["postgres"][0]
        self.data_endpoint_slices = {
            "postgres": {
                "metadata": {"resourceVersion": "202"},
                "items": [
                    {
                        "metadata": {
                            "name": "postgres-slice",
                            "namespace": runtime.DATA_NAMESPACE,
                            "uid": "postgres-slice-uid",
                            "labels": {
                                "kubernetes.io/service-name": "postgres"
                            },
                        },
                        "addressType": "IPv4",
                        "ports": [
                            {
                                "name": "postgres",
                                "protocol": "TCP",
                                "port": 5432,
                            }
                        ],
                        "endpoints": [
                            {
                                "conditions": {
                                    "ready": True,
                                    "serving": True,
                                    "terminating": False,
                                },
                                "targetRef": {
                                    "kind": "Pod",
                                    "namespace": runtime.DATA_NAMESPACE,
                                    "name": postgres_pod["metadata"]["name"],
                                    "uid": postgres_pod["metadata"]["uid"],
                                },
                                "addresses": [
                                    postgres_pod["status"]["podIP"]
                                ],
                            }
                        ],
                    }
                ]
            }
        }

        self.application_service_expected = {}
        self.application_services = {}
        for name in ("weltgewebe-api", "weltgewebe-web"):
            spec = runtime._service_spec_projection(
                {
                    "spec": {
                        "selector": {"app.kubernetes.io/name": name},
                        "ports": [
                            {
                                "name": "http",
                                "port": 8080,
                                "targetPort": "http",
                                "protocol": "TCP",
                            }
                        ],
                    }
                },
                f"fixture application Service {name}",
            )
            self.application_service_expected[name] = {
                "spec": spec,
                "spec_sha256": runtime._stable_json_sha256(spec),
            }
            self.application_services[name] = {
                "metadata": {
                    "name": name,
                    "namespace": runtime.APP_NAMESPACE,
                },
                "spec": json.loads(json.dumps(spec)),
            }

        self.namespaces = {
            name: {
                "metadata": {
                    "name": name,
                    "labels": json.loads(json.dumps(value["labels"])),
                }
            }
            for name, value in (
                runtime._versioned_namespace_security_contract()
            ).items()
        }

        self.application_service_account_expected = {
            name: {
                "contract": {
                    "labels": {"app.kubernetes.io/name": name},
                    "annotations": {},
                    "automountServiceAccountToken": False,
                    "imagePullSecrets": [],
                    "secrets": [],
                },
                "contract_sha256": runtime._stable_json_sha256(
                    {
                        "labels": {"app.kubernetes.io/name": name},
                        "annotations": {},
                        "automountServiceAccountToken": False,
                        "imagePullSecrets": [],
                        "secrets": [],
                    }
                ),
            }
            for name in ("weltgewebe-api", "weltgewebe-web")
        }
        self.application_service_accounts = {
            name: {
                "metadata": {
                    "name": name,
                    "namespace": runtime.APP_NAMESPACE,
                    "labels": {"app.kubernetes.io/name": name},
                },
                "automountServiceAccountToken": False,
            }
            for name in self.application_service_account_expected
        }
        self.application_pdb_expected = {}
        self.application_pdbs = {}
        for name in ("weltgewebe-api", "weltgewebe-web"):
            contract = runtime._pdb_spec_projection(
                {
                    "spec": {
                        "minAvailable": 1,
                        "selector": {
                            "matchLabels": {
                                "app.kubernetes.io/name": name
                            }
                        },
                    }
                },
                f"fixture application PDB {name}",
            )
            self.application_pdb_expected[name] = {
                "contract": contract,
                "contract_sha256": runtime._stable_json_sha256(
                    contract
                ),
            }
            self.application_pdbs[name] = {
                "metadata": {
                    "name": name,
                    "namespace": runtime.APP_NAMESPACE,
                },
                "spec": {
                    "minAvailable": 1,
                    "selector": {
                        "matchLabels": {
                            "app.kubernetes.io/name": name
                        }
                    },
                    "unhealthyPodEvictionPolicy": "IfHealthyBudget",
                },
            }

        api_image = "ghcr.io/heimgewebe/commonthing-api@sha256:" + "b" * 64
        web_image = "ghcr.io/heimgewebe/commonthing-web@sha256:" + "c" * 64
        api_pod_images = {
            "containers": {
                "api": api_image,
                "search-worker": api_image,
                "ollama": self.config["semantic_search"]["ollama_image"],
            },
            "init_containers": {},
        }
        web_pod_images = {
            "containers": {"web": web_image},
            "init_containers": {},
        }
        self.application_pods = {
            "weltgewebe-api": [
                workload_pod(
                    runtime.APP_NAMESPACE,
                    "weltgewebe-api",
                    index,
                    {"app.kubernetes.io/name": "weltgewebe-api"},
                    api_pod_images,
                )
                for index in range(int(self.config["semantic_search"]["api_replicas"]))
            ],
            "weltgewebe-web": [
                workload_pod(
                    runtime.APP_NAMESPACE,
                    "weltgewebe-web",
                    index,
                    {"app.kubernetes.io/name": "weltgewebe-web"},
                    web_pod_images,
                )
                for index in range(int(self.config["runtime_binding"]["web_replicas"]))
            ],
        }
        self.migration_pods = [
            {
                "metadata": {
                    "name": "migration-pod-0",
                    "namespace": runtime.APP_NAMESPACE,
                }
            }
        ]
        self.application_extra_pods: list[dict] = []


        self.cilium_pods = [
            workload_pod(
                "kube-system",
                "cilium",
                0,
                {"k8s-app": "cilium"},
                self.cilium_expected_images,
            )
        ]
        self.cilium_operator_pods = [
            workload_pod(
                "kube-system",
                "cilium-operator",
                0,
                {"io.cilium/app": "operator"},
                self.cilium_expected_operator_images,
            )
        ]
        self.cilium_relay_pods = [
            workload_pod(
                "kube-system",
                "hubble-relay",
                0,
                {"k8s-app": "hubble-relay"},
                self.cilium_expected_relay_images,
            )
        ]

        self.live_secrets = {
            f"{runtime.DATA_NAMESPACE}/commonthing-experiment-b-database": {
                "metadata": {
                    "namespace": runtime.DATA_NAMESPACE,
                    "name": "commonthing-experiment-b-database",
                },
                "type": "Opaque",
                "data": {
                    "username": "dXNlcg==",
                    "database": "ZGI=",
                    "password": "cGFzcw==",
                },
            },
            f"{runtime.APP_NAMESPACE}/weltgewebe-runtime": {
                "metadata": {
                    "namespace": runtime.APP_NAMESPACE,
                    "name": "weltgewebe-runtime",
                },
                "type": "Opaque",
                "data": {
                    "database-url": "cG9zdGdyZXNxbDovL3VzZXI6cGFzc0Bwb3N0Z3Jlcy5jb21tb250aGluZy1kYXRhLnN2Yy5jbHVzdGVyLmxvY2FsOjU0MzIvZGI="
                },
            },
            f"{runtime.APP_NAMESPACE}/commonthing-experiment-b-registry": {
                "metadata": {
                    "namespace": runtime.APP_NAMESPACE,
                    "name": "commonthing-experiment-b-registry",
                },
                "type": "kubernetes.io/dockerconfigjson",
                "data": {".dockerconfigjson": "e30="},
            },
        }
        self.pvcs = [
            {
                "metadata": {"namespace": runtime.DATA_NAMESPACE, "name": "postgres-data"},
                "spec": {
                    "accessModes": ["ReadWriteOnce"],
                    "storageClassName": "local-path",
                    "resources": {"requests": {"storage": "10Gi"}},
                },
                "status": {"phase": "Bound"},
            },
            {
                "metadata": {"namespace": runtime.DATA_NAMESPACE, "name": "nats-data"},
                "spec": {
                    "accessModes": ["ReadWriteOnce"],
                    "storageClassName": "local-path",
                    "resources": {"requests": {"storage": "5Gi"}},
                },
                "status": {"phase": "Bound"},
            },
            {
                "metadata": {"namespace": runtime.APP_NAMESPACE, "name": "ollama-models"},
                "spec": {
                    "accessModes": ["ReadWriteOnce"],
                    "storageClassName": "local-path",
                    "resources": {"requests": {"storage": "10Gi"}},
                },
                "status": {"phase": "Bound"},
            },
        ]
        self.pvc_expected = {}
        for pvc in self.pvcs:
            key = (
                f"{pvc['metadata']['namespace']}/"
                f"{pvc['metadata']['name']}"
            )
            spec = runtime._pvc_spec_projection(
                pvc, f"fixture PVC {key}"
            )
            self.pvc_expected[key] = {
                "spec": spec,
                "spec_sha256": runtime._stable_json_sha256(spec),
            }
        route_parent = {
            "name": "commonthing-experiment-b",
            "namespace": runtime.APP_NAMESPACE,
            "sectionName": "http",
        }
        self.gateway = {
            "metadata": {
                "name": "commonthing-experiment-b",
                "namespace": runtime.APP_NAMESPACE,
                "generation": 1,
                "uid": "gateway-uid",
                "resourceVersion": "300",
            },
            "spec": {
                "gatewayClassName": "cilium",
                "listeners": [{
                    "name": "http",
                    "protocol": "HTTP",
                    "port": 80,
                    "allowedRoutes": {
                        "namespaces": {"from": "Same"},
                        "kinds": [{
                            "group": "gateway.networking.k8s.io",
                            "kind": "HTTPRoute",
                        }],
                    },
                }],
            },
            "status": {"conditions": [{
                "type": "Programmed",
                "status": "True",
                "observedGeneration": 1,
            }]},
        }
        self.httproute = {
            "metadata": {
                "name": "commonthing-experiment-b",
                "namespace": runtime.APP_NAMESPACE,
                "generation": 1,
                "uid": "httproute-uid",
                "resourceVersion": "400",
            },
            "spec": {
                "parentRefs": [route_parent],
                "rules": [
                    {
                        "matches": [
                            {"path": {"type": "PathPrefix", "value": "/health"}},
                            {"path": {"type": "PathPrefix", "value": "/api"}},
                        ],
                        "backendRefs": [{"name": "weltgewebe-api", "port": 8080}],
                    },
                    {
                        "matches": [
                            {"path": {"type": "PathPrefix", "value": "/"}},
                        ],
                        "backendRefs": [{"name": "weltgewebe-web", "port": 8080}],
                    },
                ],
            },
            "status": {
                "parents": [{
                    "parentRef": route_parent,
                    "controllerName": "io.cilium/gateway-controller",
                    "conditions": [
                        {
                            "type": "Accepted",
                            "status": "True",
                            "observedGeneration": 1,
                        },
                        {
                            "type": "ResolvedRefs",
                            "status": "True",
                            "observedGeneration": 1,
                        },
                    ],
                }]
            },
        }
        self.extra_httproutes: list[dict] = []
        self.httproute_list_resource_version = "450"
        self.runner = self.patch("run", side_effect=self.run_fixture)
        self.qemu_uid = self.pool.stat().st_uid
        self.qemu_gid = self.pool.stat().st_gid
        self.patch(
            "_libvirt_qemu_identity",
            return_value=(self.qemu_uid, self.qemu_gid),
        )
        self.created_volume_xml: list[ET.Element] = []

    def patch(self, name: str, *args, **kwargs):
        patcher = mock.patch.object(runtime, name, *args, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def run_fixture(self, argv: list[str], **_kwargs):
        output, code = "", 0
        if argv[0] == "virt-install":
            if "--uuid" in argv:
                planned_uuid = argv[argv.index("--uuid") + 1]
                old_uuid = self.domain_uuid
                self.domain_uuid = planned_uuid
                for key in ("live", "inactive"):
                    self.xml[key] = self.xml[key].replace(
                        old_uuid, planned_uuid
                    )
            self.domain_present = True
            self.domain_active = True
        elif argv[0] == "helm":
            if argv[1] == "list":
                output = json.dumps([{
                    "name": "cilium",
                    "namespace": "kube-system",
                    "status": "deployed",
                    "chart": self.cilium_chart,
                }])
            else:
                self.assertEqual(argv[1:3], ["get", "values"])
                output = json.dumps(self.cilium_values)
        elif argv[0] == "kubectl":
            self.assertEqual(argv[1:], ["get", "nodes", "-o", "json"])
            output = json.dumps(self.node_inventory)
        else:
            self.assertEqual(argv[:3], ["virsh", "-c", runtime.LIBVIRT_URI])
            command = argv[3]
            if command == "dominfo":
                code = 0 if self.domain_present else 1
            elif command == "domstate":
                if self.domain_present:
                    output = "running\n" if self.domain_active else "shut off\n"
                else:
                    code = 1
            elif command == "domuuid":
                if self.domain_present:
                    output = f"{self.domain_uuid}\n"
                else:
                    code = 1
            elif command == "list":
                output = f"{runtime.VM_NAME}\n" if self.domain_present else ""
            elif command == "pool-info":
                code = 0 if self.pool_present else 1
            elif command == "pool-uuid":
                if self.pool_present:
                    output = f"{self.pool_uuid}\n"
                else:
                    code = 1
            elif command == "pool-list":
                output = f"{runtime.POOL_NAME}\n" if self.pool_present else ""
            elif command == "vol-list":
                self.assertIn(argv[4], {runtime.POOL_NAME, self.pool_uuid})
                if not self.pool_present:
                    code = 1
                else:
                    rows = [
                        (name, path)
                        for name, path in (
                            (runtime.VOLUME_NAME, self.disk),
                            (runtime.BASE_VOLUME, self.base),
                        )
                        if path.exists()
                    ]
                    output = " Name Path\n----------------------------------------\n"
                    output += "".join(f" {name} {path}\n" for name, path in rows)
            elif command == "dumpxml":
                output = self.xml["inactive" if "--inactive" in argv else "live"]
            elif command == "net-dumpxml":
                output = self.xml["network"]
            elif command == "pool-dumpxml":
                output = self.xml["pool"]
            elif command == "vol-dumpxml":
                output = self.xml["disk" if argv[4] == runtime.VOLUME_NAME else "base"]
            elif command == "qemu-monitor-command":
                self.assertEqual(json.loads(argv[5]), {"execute": "query-block"})
                output = json.dumps(self.qmp)
            elif command == "vol-download":
                Path(argv[5]).write_bytes(self.base_bytes)
            elif command == "pool-define-as":
                self.pool_present = True
            elif command == "vol-create":
                volume_xml = ET.parse(argv[5]).getroot()
                self.created_volume_xml.append(volume_xml)
                volume_name = volume_xml.findtext("name")
                self.assertIn(volume_name, {runtime.BASE_VOLUME, runtime.VOLUME_NAME})
                volume_path = self.pool / str(volume_name)
                volume_path.write_bytes(b"created volume")
                volume_path.chmod(0o600)
            elif command == "vol-create-as":
                (self.pool / argv[5]).write_bytes(b"created volume")
            elif command == "vol-delete":
                (self.pool / argv[4]).unlink()
            elif command == "destroy":
                if self.domain_present and self.domain_active:
                    self.domain_active = False
                else:
                    code = 1
            elif command == "undefine":
                if self.domain_present and not self.domain_active:
                    self.domain_present = False
                else:
                    code = 1
            elif command == "pool-undefine":
                self.pool_present = False
            else:
                self.assertIn(command, {
                    "pool-build", "pool-start", "vol-upload", "pool-refresh",
                    "pool-destroy", "pool-delete",
                })
        return runtime.subprocess.CompletedProcess(argv, code, stdout=output, stderr="")

    def write_vm_receipt(self) -> dict:
        receipt = vm_receipt_fixture(
            runtime._live_vm_substrate(self.root, self.config), self.root
        )
        runtime.atomic_json(self.root / "receipts/vm-create.json", receipt)
        return receipt

    def prepare_create(self) -> None:
        self.disk.unlink()
        self.base.unlink()
        self.pool.rmdir()
        self.domain_present = self.pool_present = False
        self.domain_active = False
        cloud_dir = self.root / "cloud-init"
        cloud_dir.mkdir(parents=True, exist_ok=True)
        self.cloud_user_data = cloud_dir / "user-data.yaml"
        self.cloud_meta_data = cloud_dir / "meta-data.yaml"
        self.cloud_user_data_bytes = b"#cloud-config\nhostname: fixture\n"
        self.cloud_meta_data_bytes = b"instance-id: fixture\n"
        self.cloud_user_data.write_bytes(self.cloud_user_data_bytes)
        self.cloud_meta_data.write_bytes(self.cloud_meta_data_bytes)
        self.patch(
            "prepare",
            return_value={
                "cloud_image": str(self.root / "ubuntu.img"),
                "cloud_image_virtual_size": 4 * 1024**3,
                "cloud_init": {
                    "user_data": str(self.cloud_user_data),
                    "user_data_sha256": hashlib.sha256(
                        self.cloud_user_data_bytes
                    ).hexdigest(),
                    "meta_data": str(self.cloud_meta_data),
                    "meta_data_sha256": hashlib.sha256(
                        self.cloud_meta_data_bytes
                    ).hexdigest(),
                },
            },
        )

    def test_create_vm_declares_private_qemu_volume_permissions_before_boot(self) -> None:
        self.prepare_create()
        result = runtime.create_vm(self.root)

        self.assertEqual(result["status"], "created")
        self.assertEqual(len(self.created_volume_xml), 2)
        by_name = {item.findtext("name"): item for item in self.created_volume_xml}
        self.assertEqual(set(by_name), {runtime.BASE_VOLUME, runtime.VOLUME_NAME})
        for volume in by_name.values():
            permissions = volume.find("./target/permissions")
            self.assertIsNotNone(permissions)
            self.assertEqual(permissions.findtext("mode"), "0600")
            self.assertEqual(permissions.findtext("owner"), str(self.qemu_uid))
            self.assertEqual(permissions.findtext("group"), str(self.qemu_gid))
        backing = by_name[runtime.VOLUME_NAME].find("./backingStore")
        self.assertIsNotNone(backing)
        self.assertEqual(backing.findtext("path"), str(self.base))
        self.assertEqual(backing.find("./format").get("type"), "qcow2")
        self.assertFalse(any(
            call.args[0][3] == "vol-create-as"
            for call in self.runner.call_args_list
            if call.args and call.args[0][:3] == ["virsh", "-c", runtime.LIBVIRT_URI]
        ))

    def test_verified_snapshot_fd_freezes_exact_source_bytes(self) -> None:
        self.prepare_create()
        expected = hashlib.sha256(self.cloud_user_data_bytes).hexdigest()
        with runtime._verified_snapshot_fd(
            self.cloud_user_data,
            expected,
            "cloud-init fixture",
        ) as snapshot_fd:
            replacement = self.root / "replacement-user-data"
            replacement.write_bytes(b"tampered cloud-init\n")
            runtime.os.replace(replacement, self.cloud_user_data)
            observed = runtime.os.pread(
                snapshot_fd,
                len(self.cloud_user_data_bytes) + 32,
                0,
            )
        self.assertEqual(observed, self.cloud_user_data_bytes)

    def prepare_status(self) -> None:
        namespace_contract = runtime._versioned_namespace_security_contract()
        self.source_commit_config = self.patch(
            "_source_commit_config",
            side_effect=lambda _source_commit: json.loads(
                json.dumps(self.config)
            ),
        )
        self.namespace_contract = self.patch(
            "_versioned_namespace_security_contract",
            return_value=json.loads(json.dumps(namespace_contract)),
        )
        self.source_commit_data_service = self.patch(
            "_source_commit_data_service_contract",
            side_effect=lambda _source_commit, path, name: (
                runtime._versioned_data_service_contract(path, name)
            ),
        )
        self.source_commit_network_policy = self.patch(
            "_source_commit_network_policy_specs",
            side_effect=lambda _source_commit, path, namespace: (
                runtime._versioned_network_policy_specs(path, namespace)
            ),
        )
        self.source_commit_data_resources = self.patch(
            "_source_commit_data_container_resources",
            side_effect=lambda _source_commit, path, deployment, container: (
                runtime._versioned_data_container_resources(
                    path,
                    deployment,
                    container,
                )
            ),
        )
        self.source_commit_data_contract = self.patch(
            "_source_commit_data_deployment_contract",
            side_effect=lambda _source_commit, path, name: (
                runtime._versioned_data_deployment_contract(path, name)
            ),
        )
        api_digest = "sha256:" + "b" * 64
        web_digest = "sha256:" + "c" * 64
        binding = contract.render_bootstrap(
            self.commit,
            api_digest,
            web_digest,
            self.root / "bootstrap.yaml",
        )
        self.flux_contract = runtime._flux_bootstrap_contract(self.root, binding)
        self.release_config_map = {
            "metadata": {
                "name": "commonthing-experiment-b-release",
                "namespace": "flux-system",
            },
            **json.loads(
                json.dumps(self.flux_contract["release_config_map"])
            ),
        }
        self.live_flux_source_spec = json.loads(
            json.dumps(self.flux_contract["source_spec"])
        )
        self.live_flux_source_spec.update(
            {"suspend": False, "provider": "generic", "timeout": "60s"}
        )
        self.live_flux_kustomization_specs = json.loads(
            json.dumps(self.flux_contract["kustomization_specs"])
        )
        for spec in self.live_flux_kustomization_specs.values():
            spec["suspend"] = False
            spec["deletionPolicy"] = "MirrorPrune"
            spec.setdefault("force", False)
        runtime.atomic_json(
            self.root / "receipts/release.json",
            {"schema_version": 1, "status": "applied", **binding},
        )
        runtime.atomic_json(
            self.root / "secrets/database.json",
            {"username": "user", "database": "db", "password": "pass"},
        )
        self.registry_source = self.root / "secrets/registry.json"
        runtime.atomic_bytes(self.registry_source, b"{}")
        daemonset_proof = runtime._require_running_pod_image_contract(
            self.cilium_pods,
            namespace="kube-system",
            workload="cilium",
            expected_replicas=1,
            expected_images=self.cilium_expected_images,
            required_labels={"k8s-app": "cilium"},
            context="Cilium DaemonSet Pod",
        )
        operator_proof = runtime._require_running_pod_image_contract(
            self.cilium_operator_pods,
            namespace="kube-system",
            workload="cilium-operator",
            expected_replicas=1,
            expected_images=self.cilium_expected_operator_images,
            required_labels={"io.cilium/app": "operator"},
            context="Cilium operator Pod",
        )
        flux_proofs = {
            name: runtime._require_running_pod_image_contract(
                [
                    pod
                    for pod in self.flux_controller_pods
                    if pod["metadata"]["labels"].get(
                        "app.kubernetes.io/name"
                    ) == name
                ],
                namespace="flux-system",
                workload=name,
                expected_replicas=1,
                expected_images=expected["images"],
                required_labels=expected["selector_labels"],
                context="Flux controller Pod",
            )
            for name, expected in self.flux_expected_contract.items()
        }
        runtime.atomic_json(
            self.root / "receipts/platform.json",
            {
                "schema_version": 1,
                "status": "ready",
                "source_commit": self.commit,
                "cilium_runtime_image_ids": {
                    "daemonset": daemonset_proof[
                        "runtime_image_ids_sha256"
                    ],
                    "operator": operator_proof[
                        "runtime_image_ids_sha256"
                    ],
                    "relay": runtime._require_running_pod_image_contract(
                        self.cilium_relay_pods,
                        namespace="kube-system",
                        workload="hubble-relay",
                        expected_replicas=1,
                        expected_images=self.cilium_expected_relay_images,
                        required_labels={"k8s-app": "hubble-relay"},
                        context="Hubble Relay Pod",
                    )["runtime_image_ids_sha256"],
                },
                "flux_runtime_image_ids": {
                    name: proof["runtime_image_ids_sha256"]
                    for name, proof in flux_proofs.items()
                },
            },
        )
        runtime.atomic_json(
            self.root / "receipts/secrets.json",
            {
                "schema_version": 1,
                "status": "ready",
                "source_commit": self.commit,
                "database_secret": "commonthing-experiment-b-database",
                "runtime_secret": "weltgewebe-runtime",
                "registry_secret": "commonthing-experiment-b-registry",
                "database_source_sha256": runtime.sha256_file(
                    self.root / "secrets/database.json"
                ),
                "registry_source_sha256": runtime.sha256_file(
                    self.registry_source
                ),
                "secret_values_recorded": False,
            },
        )
        self.k3s_runtime = {
            "vm_ip": "192.168.122.10",
            "kubeconfig_sha256": "3" * 64,
            "kubeconfig_server": "https://192.168.122.10:6443",
            "binary_sha256": self.config["kubernetes"]["binary_sha256"],
            "config_sha256": runtime.sha256_file(
                runtime.CLUSTER / "k3s-config.yaml"
            ),
            "service_sha256": runtime.sha256_file(
                runtime.CLUSTER / "k3s.service"
            ),
            "service_active": True,
            "service_substate": "running",
            "unit_file_state": "enabled",
            "fragment_path": "/etc/systemd/system/k3s.service",
            "drop_ins_absent": True,
            "main_pid": 1234,
            "process_exe": "/usr/local/bin/k3s",
            "process_argv": ["/usr/local/bin/k3s", "server"],
            "environment_overrides_absent": True,
        }
        kubeconfig = self.root / "kubeconfig.yaml"
        kubeconfig.write_text(
            yaml.safe_dump(
                {
                    "current-context": "experiment-b",
                    "contexts": [
                        {
                            "name": "experiment-b",
                            "context": {"cluster": "experiment-b"},
                        }
                    ],
                    "clusters": [
                        {
                            "name": "experiment-b",
                            "cluster": {
                                "server": self.k3s_runtime[
                                    "kubeconfig_server"
                                ]
                            },
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        kubeconfig.chmod(0o600)
        self.k3s_runtime["kubeconfig_sha256"] = runtime.sha256_file(
            kubeconfig
        )
        self.k3s_readback = self.patch(
            "_require_live_k3s_runtime",
            return_value=json.loads(json.dumps(self.k3s_runtime)),
        )
        self.kubernetes_target = {
            "vm_ip": self.k3s_runtime["vm_ip"],
            "kubeconfig_sha256": self.k3s_runtime["kubeconfig_sha256"],
            "server": self.k3s_runtime["kubeconfig_server"],
        }
        self.kubernetes_target_identity = self.patch(
            "_kubernetes_target_identity",
            return_value=json.loads(json.dumps(self.kubernetes_target)),
        )
        self.expected_flux_controllers = self.patch(
            "_expected_flux_controller_contract",
            return_value=json.loads(json.dumps(self.flux_expected_contract)),
        )
        self.application_workload_expected = {
            "weltgewebe-api": {
                "contract": {
                    "replicas": int(
                        self.config["semantic_search"]["api_replicas"]
                    )
                },
                "contract_sha256": "7" * 64,
                "pod_contract_sha256": "8" * 64,
            },
            "weltgewebe-web": {
                "contract": {
                    "replicas": int(
                        self.config["runtime_binding"]["web_replicas"]
                    )
                },
                "contract_sha256": "9" * 64,
                "pod_contract_sha256": "a" * 64,
            },
        }
        self.application_workload_readback = {
            name: {
                "contract_sha256": value["contract_sha256"],
                "pod_contract_sha256": value["pod_contract_sha256"],
                "pod_names": [
                    pod["metadata"]["name"]
                    for pod in self.application_pods[name]
                ],
                "canonical": True,
            }
            for name, value in self.application_workload_expected.items()
        }
        self.application_workload_contract = self.patch(
            "_rendered_application_workload_contract",
            return_value=json.loads(
                json.dumps(self.application_workload_expected)
            ),
        )
        self.migration_contract_expected = {
            "contract": {"completions": 1},
            "contract_sha256": "b" * 64,
            "pod_contract_sha256": "c" * 64,
        }
        self.migration_readback = {
            "contract_sha256": "b" * 64,
            "pod_contract_sha256": "c" * 64,
            "pod_names": ["migration-pod-0"],
            "succeeded_pods": 1,
            "canonical": True,
        }
        self.migration_contract = self.patch(
            "_rendered_migration_job_contract",
            return_value=json.loads(
                json.dumps(self.migration_contract_expected)
            ),
        )
        self.migration_live = self.patch(
            "_require_migration_job_runtime_contract",
            return_value=json.loads(
                json.dumps(self.migration_readback)
            ),
        )
        self.application_workload_live = self.patch(
            "_require_live_application_workloads",
            return_value=json.loads(
                json.dumps(self.application_workload_readback)
            ),
        )
        self.application_service_contract = self.patch(
            "_rendered_application_service_contract",
            return_value=json.loads(
                json.dumps(self.application_service_expected)
            ),
        )
        self.application_service_account_contract = self.patch(
            "_rendered_application_service_account_contract",
            return_value=json.loads(
                json.dumps(self.application_service_account_expected)
            ),
        )
        self.application_pdb_contract = self.patch(
            "_rendered_application_pdb_contract",
            return_value=json.loads(
                json.dumps(self.application_pdb_expected)
            ),
        )
        self.pvc_contract = self.patch(
            "_rendered_pvc_contract",
            return_value=json.loads(json.dumps(self.pvc_expected)),
        )
        self.tools = self.patch(
            "toolchain",
            return_value={
                "tools": {
                    "kubectl": "kubectl",
                    "helm": "helm",
                    "flux": "flux",
                }
            },
        )
        self.patch("kube_env", return_value={})
        self.patch("vm_ip", return_value="192.168.122.10")
        self.gateway_data_plane = self.patch(
            "_gateway_data_plane_readback",
            return_value={
                "gateway": "http://192.168.122.20",
                "checks": {
                    "web_root": {"status": 200},
                    "web_revision": {"status": 200, "commit": self.commit},
                    "api_ready": {"status": 200},
                    "domain_nodes": {"status": 200},
                    "search": {"status": 200, "items": 1},
                    "anonymous_auth_boundary": {
                        "status": 200,
                        "authenticated": False,
                        "role": "gast",
                    },
                },
            },
        )
        self.status_serving_runtime = {
            "services": {
                "weltgewebe-api": {
                    "endpoints": {
                        "resource_version": "101",
                        "sha256": "1" * 64,
                    },
                },
                "weltgewebe-web": {
                    "endpoints": {
                        "resource_version": "202",
                        "sha256": "2" * 64,
                    },
                },
            },
            "gateway_base_url": "http://192.168.122.20",
        }
        self.functional_serving_runtime = self.patch(
            "_functional_serving_runtime_binding",
            side_effect=lambda *_args, **_kwargs: json.loads(
                json.dumps(self.status_serving_runtime)
            ),
        )
        self.functional_endpoint_guard = self.patch(
            "_guard_functional_service_endpoints",
            side_effect=lambda *_args, **_kwargs: mock.MagicMock(),
        )
        self.semantic_provider = self.patch(
            "_semantic_provider_live_readback",
            return_value={
                "source_commit": self.commit,
                "provider": self.config["semantic_search"]["provider"],
                "model_id": self.config["semantic_search"]["model_id"],
                "model_revision": self.config["semantic_search"]["model_revision"],
                "runtime_identity": self.config["semantic_search"]["runtime_identity"],
                "dimension": self.config["semantic_search"]["dimension"],
                "embedding_probe": True,
                "embedding_probe_sha256": "d" * 64,
                "literal_loopback": True,
            },
        )
        self.recovery_state = self.patch(
            "_final_recovery_state_readback",
            return_value={
                "recovery_receipt_sha256": "e" * 64,
                "fixture_receipt_sha256": "f" * 64,
                "rpo_seconds": 0,
                "database_signature": {"state": "stable"},
                "jetstream_signature": {"state": "stable"},
                "fixture_live_binding": {"generation_id": "fixture"},
            },
        )
        self.expected_cilium_runtime = self.patch(
            "_expected_cilium_runtime_contract",
            return_value={
                "config_map": json.loads(
                    json.dumps(self.cilium_config_contract)
                ),
                "daemonset": {
                    "images": json.loads(
                        json.dumps(self.cilium_expected_images)
                    ),
                    "selector_labels": {"k8s-app": "cilium"},
                    "pod_spec": runtime._cilium_pod_spec_projection(
                        self.cilium_daemonset["spec"]["template"]["spec"],
                        "expected Cilium DaemonSet",
                    ),
                    "rollout": runtime._daemonset_rollout_projection(
                        self.cilium_daemonset["spec"],
                        "expected Cilium DaemonSet",
                    ),
                },
                "operator": {
                    "images": json.loads(
                        json.dumps(
                            self.cilium_expected_operator_images
                        )
                    ),
                    "selector_labels": {
                        "io.cilium/app": "operator"
                    },
                    "pod_spec": runtime._cilium_pod_spec_projection(
                        self.cilium_operator["spec"]["template"]["spec"],
                        "expected Cilium operator Deployment",
                    ),
                    "replicas": 1,
                    "lifecycle": {
                        "paused": False,
                        "minReadySeconds": 0,
                        "progressDeadlineSeconds": 600,
                    },
                    "rollout": runtime._deployment_rollout_projection(
                        self.cilium_operator["spec"],
                        "expected Cilium operator Deployment",
                    ),
                },
                "relay": {
                    "images": json.loads(
                        json.dumps(self.cilium_expected_relay_images)
                    ),
                    "selector_labels": {
                        "k8s-app": "hubble-relay"
                    },
                    "pod_spec": runtime._cilium_pod_spec_projection(
                        self.cilium_relay["spec"]["template"]["spec"],
                        "expected Hubble Relay Deployment",
                    ),
                    "replicas": 1,
                    "lifecycle": {
                        "paused": False,
                        "minReadySeconds": 0,
                        "progressDeadlineSeconds": 600,
                    },
                    "rollout": runtime._deployment_rollout_projection(
                        self.cilium_relay["spec"],
                        "expected Hubble Relay Deployment",
                    ),
                },
            },
        )
        self.patch("_kubectl_json", side_effect=self.kubernetes_fixture)
        self.patch(
            "_endpoint_slice_collection_json",
            side_effect=self.endpoint_slice_fixture,
        )

    def endpoint_slice_fixture(self, _root, namespace, service_name):
        if namespace == runtime.DATA_NAMESPACE and service_name == "postgres":
            return self.data_endpoint_slices["postgres"]
        raise runtime.RuntimeErrorEB(
            f"unexpected EndpointSlice fixture lookup: {namespace}/{service_name}"
        )

    def kubernetes_fixture(self, _root, arguments):
        if (
            len(arguments) == 3
            and arguments[:2] == ["get", "namespace"]
            and arguments[-1] in self.namespaces
        ):
            return self.namespaces[str(arguments[-1])]
        if (
            len(arguments) == 5
            and arguments[:4]
            == ["-n", runtime.APP_NAMESPACE, "get", "service"]
            and arguments[-1] in self.application_services
        ):
            return self.application_services[str(arguments[-1])]
        if (
            len(arguments) == 5
            and arguments[:4]
            == [
                "-n",
                runtime.APP_NAMESPACE,
                "get",
                "serviceaccount",
            ]
            and arguments[-1] in self.application_service_accounts
        ):
            return self.application_service_accounts[str(arguments[-1])]
        if (
            len(arguments) == 5
            and arguments[:4]
            == [
                "-n",
                runtime.APP_NAMESPACE,
                "get",
                "poddisruptionbudget",
            ]
            and arguments[-1] in self.application_pdbs
        ):
            return self.application_pdbs[str(arguments[-1])]
        if arguments == [
            "-n",
            "flux-system",
            "get",
            "configmap",
            "commonthing-experiment-b-release",
        ]:
            return self.release_config_map
        if arguments == [
            "-n", runtime.APP_NAMESPACE, "get", "configmap", "weltgewebe-runtime"
        ]:
            return self.runtime_config_map
        if arguments == [
            "-n", "kube-system", "get", "configmap", "cilium-config"
        ]:
            return self.cilium_config
        if arguments == [
            "-n", runtime.APP_NAMESPACE, "get", "networkpolicies"
        ]:
            return {"items": self.network_policies}
        if arguments == [
            "-n", runtime.DATA_NAMESPACE, "get", "networkpolicies"
        ]:
            return {"items": self.data_network_policies}
        if arguments == [
            "-n", runtime.APP_NAMESPACE, "get", "ciliumnetworkpolicies"
        ]:
            return {"items": self.cilium_network_policies}
        if arguments == ["-n", "kube-system", "get", "daemonsets"]:
            return {"items": self.kube_proxy_daemonsets}
        if arguments == ["-n", "kube-system", "get", "pods"]:
            return {
                "items": [
                    *self.cilium_pods,
                    *self.cilium_operator_pods,
                    *self.cilium_relay_pods,
                    *self.kube_proxy_pods,
                ]
            }
        if arguments == [
            "-n", runtime.DATA_NAMESPACE, "get", "pods"
        ]:
            return {
                "items": [
                    *self.data_pods["postgres"],
                    *self.data_pods["nats"],
                ]
            }
        if arguments == [
            "-n",
            runtime.APP_NAMESPACE,
            "get",
            "pods",
        ]:
            return {
                "items": [
                    *self.application_pods["weltgewebe-api"],
                    *self.application_pods["weltgewebe-web"],
                    *self.migration_pods,
                    *self.application_extra_pods,
                ]
            }
        if arguments == [
            "-n",
            runtime.APP_NAMESPACE,
            "get",
            "pods",
            "-l",
            f"batch.kubernetes.io/job-name={runtime.MIGRATION_JOB_NAME}",
        ]:
            return {"items": self.migration_pods}
        if arguments == [
            "-n",
            runtime.APP_NAMESPACE,
            "get",
            "pods",
            "-l",
            "app.kubernetes.io/name=weltgewebe-api",
        ]:
            return {"items": self.application_pods["weltgewebe-api"]}
        if arguments == [
            "-n",
            runtime.APP_NAMESPACE,
            "get",
            "pods",
            "-l",
            "app.kubernetes.io/name=weltgewebe-web",
        ]:
            return {"items": self.application_pods["weltgewebe-web"]}
        if "daemonset" in arguments and arguments[-1] == "cilium":
            return self.cilium_daemonset
        if arguments == [
            "-n", "kube-system", "get", "deployment", "cilium-operator"
        ]:
            return self.cilium_operator
        if arguments == [
            "-n", "kube-system", "get", "deployment", "hubble-relay"
        ]:
            return self.cilium_relay
        if arguments == ["-n", "flux-system", "get", "deployments"]:
            return {"items": self.flux_controller_deployments}
        if arguments == ["-n", "flux-system", "get", "pods"]:
            return {"items": self.flux_controller_pods}
        if "gitrepository" in arguments:
            return {
                "metadata": {"generation": 1},
                "spec": json.loads(json.dumps(self.live_flux_source_spec)),
                "status": {
                    "artifact": {"revision": f"main@sha1:{self.commit}"},
                    "conditions": [{
                        "type": "Ready",
                        "status": "True",
                        "observedGeneration": 1,
                    }],
                },
            }
        if "kustomizations" in arguments:
            return {"items": [{
                "metadata": {"name": name, "generation": 1},
                "spec": json.loads(
                    json.dumps(self.live_flux_kustomization_specs[name])
                ),
                "status": {
                    "lastAppliedRevision": f"main@sha1:{self.commit}",
                    "conditions": [{
                        "type": "Ready",
                        "status": "True",
                        "observedGeneration": 1,
                    }],
                },
            } for name in runtime.EXPECTED_FLUX_KUSTOMIZATIONS]}
        if "job" in arguments:
            return {
                "spec": {"template": {"spec": {"containers": [{
                    "name": "migration",
                    "image": "ghcr.io/heimgewebe/commonthing-api@sha256:" + "b" * 64,
                }]}}},
                "status": {
                    "succeeded": 1,
                    "conditions": [{"type": "Complete", "status": "True"}],
                },
            }
        if (
            len(arguments) == 5
            and arguments[:4]
            == ["-n", runtime.DATA_NAMESPACE, "get", "deployment"]
            and arguments[-1] in self.data_deployments
        ):
            return self.data_deployments[str(arguments[-1])]
        if (
            len(arguments) == 5
            and arguments[:4]
            == ["-n", runtime.DATA_NAMESPACE, "get", "service"]
            and arguments[-1] in self.data_services
        ):
            return self.data_services[str(arguments[-1])]
        if arguments == [
            "-n",
            runtime.DATA_NAMESPACE,
            "get",
            "endpointslices.discovery.k8s.io",
            "-l",
            "kubernetes.io/service-name=postgres",
        ]:
            return self.data_endpoint_slices["postgres"]
        if "deployment" in arguments:
            deployment_name = str(arguments[-1])
            replicas = (
                int(self.config["semantic_search"]["api_replicas"])
                if deployment_name == "weltgewebe-api"
                else int(self.config["runtime_binding"]["web_replicas"])
            )
            return {
                "metadata": {"generation": 1},
                "spec": {"replicas": replicas, "template": {"spec": {"containers": [
                    {"name": name, "image": image} for name, image in {
                        "api": "ghcr.io/heimgewebe/commonthing-api@sha256:" + "b" * 64,
                        "web": "ghcr.io/heimgewebe/commonthing-web@sha256:" + "c" * 64,
                        "search-worker": "ghcr.io/heimgewebe/commonthing-api@sha256:" + "b" * 64,
                        "ollama": self.config["semantic_search"]["ollama_image"],
                    }.items()
                ]}}},
                "status": {
                    "observedGeneration": 1, "replicas": replicas,
                    "updatedReplicas": replicas, "readyReplicas": replicas,
                    "availableReplicas": replicas, "unavailableReplicas": 0,
                    "conditions": [{"type": "Available", "status": "True"}],
                },
            }
        if "secret" in arguments:
            namespace = arguments[arguments.index("-n") + 1]
            name = arguments[-1]
            key = f"{namespace}/{name}"
            if key not in self.live_secrets:
                raise runtime.RuntimeErrorEB(f"unexpected Secret fixture lookup: {key}")
            return self.live_secrets[key]
        if "pvc" in arguments:
            return {"items": self.pvcs}
        if arguments == [
            "-n",
            runtime.APP_NAMESPACE,
            "get",
            "httproutes",
        ]:
            return {
                "metadata": {
                    "resourceVersion": self.httproute_list_resource_version,
                },
                "items": [
                    self.httproute,
                    *self.extra_httproutes,
                ],
            }
        if "httproute" in arguments:
            return self.httproute
        self.assertIn("gateway", arguments)
        return self.gateway

    def test_pool_target_create_establishes_intended_mode_under_restrictive_umask(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pool_target = Path(tmp) / "experiment-b-pool"
            previous_umask = runtime.os.umask(0o077)
            try:
                with mock.patch.object(runtime, "POOL_TARGET", pool_target):
                    pool_fd = runtime._open_libvirt_pool_target(create=True)
                    try:
                        self.assertEqual(pool_target.stat().st_mode & 0o777, 0o755)
                    finally:
                        runtime.os.close(pool_fd)
            finally:
                runtime.os.umask(previous_umask)


    def test_pool_target_create_rejects_nonempty_existing_target_without_widening_mode(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pool_target = Path(tmp) / "experiment-b-pool"
            pool_target.mkdir(mode=0o700)
            (pool_target / "unexpected").write_text("sentinel", encoding="utf-8")
            pool_target.chmod(0o700)

            with mock.patch.object(runtime, "POOL_TARGET", pool_target):
                with self.assertRaisesRegex(
                    runtime.RuntimeErrorEB,
                    "libvirt pool target already contains files",
                ):
                    runtime._open_libvirt_pool_target(create=True)

            self.assertEqual(pool_target.stat().st_mode & 0o777, 0o700)
            self.assertEqual(
                (pool_target / "unexpected").read_text(encoding="utf-8"),
                "sentinel",
            )


    def test_create_vm_rejects_symlinked_pool_target_before_libvirt_mutation(
        self,
    ) -> None:
        self.prepare_create()
        outside = self.root / "outside-pool"
        outside.mkdir()
        self.pool.symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB,
            "libvirt pool target is unsafe",
        ):
            runtime.create_vm(self.root)

        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse(self.pool_present)
        pool_define_calls = [
            call
            for call in self.runner.call_args_list
            if call.args
            and call.args[0][:4]
            == ["virsh", "-c", runtime.LIBVIRT_URI, "pool-define-as"]
        ]
        self.assertEqual(pool_define_calls, [])

    def test_teardown_rejects_symlinked_pool_target_before_storage_mutation(
        self,
    ) -> None:
        self.disk.unlink()
        self.base.unlink()
        self.pool.rmdir()
        outside = self.root / "outside-pool"
        outside.mkdir()
        outside_disk = outside / runtime.VOLUME_NAME
        outside_base = outside / runtime.BASE_VOLUME
        outside_disk.write_bytes(b"outside-volume")
        outside_base.write_bytes(b"outside-base")
        self.pool.symlink_to(outside, target_is_directory=True)

        pool_stat = outside.stat()
        volume_stat = outside_disk.stat()
        runtime.atomic_json(
            self.root / "receipts/vm-create-attempt.json",
            {
                "schema_version": 1,
                "status": "running",
                "source_commit": self.commit,
                "config_sha256": runtime.sha256_file(
                    runtime.CLUSTER / "config.json"
                ),
                "state_root": str(self.root.resolve()),
                "vm": runtime.VM_NAME,
                "pool": runtime.POOL_NAME,
                "domain_target": self.domain_uuid,
                "pool_target": self.pool_uuid,
                "pool_target_device": pool_stat.st_dev,
                "pool_target_inode": pool_stat.st_ino,
                "volume_device": volume_stat.st_dev,
                "volume_inode": volume_stat.st_ino,
                "base_image_sha256": self.config["vm"]["image"]["sha256"],
            },
        )

        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB,
            "libvirt pool target is unsafe",
        ):
            runtime.teardown(self.root)

        self.assertTrue(self.domain_present)
        self.assertTrue(self.pool_present)
        self.assertEqual(outside_disk.read_bytes(), b"outside-volume")
        self.assertEqual(outside_base.read_bytes(), b"outside-base")

    def test_teardown_proves_domain_pool_volume_and_state_absence(self) -> None:
        retirement = self.root.with_name(self.root.name + "-retirement.json")
        self.addCleanup(retirement.unlink, missing_ok=True)
        self.write_vm_receipt()
        runtime.atomic_json(
            self.root / "receipts/example.json",
            {"schema_version": 1, "status": "pass"},
        )
        with mock.patch.object(runtime, "RETIREMENT_RECEIPT", retirement):
            result = runtime.teardown(self.root)

        self.assertEqual(result["status"], "retired")
        self.assertEqual(
            result["volumes_absent"],
            {
                runtime.VOLUME_NAME: True,
                runtime.BASE_VOLUME: True,
            },
        )
        self.assertEqual(
            result["volume_paths_absent"],
            {
                runtime.VOLUME_NAME: True,
                runtime.BASE_VOLUME: True,
            },
        )
        self.assertTrue(result["pool_target_removed"])
        self.assertTrue(result["state_removed"])
        self.assertTrue(result["live_identity_verified"])
        self.assertRegex(result["substrate_sha256"], r"^[0-9a-f]{64}$")
        self.assertFalse(self.domain_present)
        self.assertFalse(self.pool_present)
        self.assertFalse(self.root.exists())
        stored = json.loads(retirement.read_text(encoding="utf-8"))
        self.assertEqual(stored, result)
        self.assertIn("example.json", stored["evidence_receipts"])

    def test_teardown_stops_before_storage_when_destroy_fails(self) -> None:
        self.write_vm_receipt()
        original = self.run_fixture

        def fail_destroy(argv, **kwargs):
            if (
                argv[:3] == ["virsh", "-c", runtime.LIBVIRT_URI]
                and argv[3] == "destroy"
            ):
                return runtime.subprocess.CompletedProcess(
                    argv, 1, stdout="", stderr="destroy failed"
                )
            return original(argv, **kwargs)

        self.runner.side_effect = fail_destroy
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB,
            "storage cleanup is forbidden",
        ):
            runtime.teardown(self.root)
        self.assertTrue(self.domain_present)
        self.assertTrue(self.domain_active)
        self.assertTrue(self.pool_present)
        self.assertTrue(self.disk.exists())
        self.assertTrue(self.base.exists())
        self.assertTrue(self.root.exists())

    def test_teardown_rejects_wrong_state_root_before_global_mutation(self) -> None:
        wrong_root = self.root / "wrong-root"
        (wrong_root / "receipts").mkdir(parents=True)
        self.write_vm_receipt()
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB,
            "state-root ownership",
        ):
            runtime.teardown(wrong_root)
        self.assertTrue(self.domain_present)
        self.assertTrue(self.pool_present)
        self.assertTrue(self.root.exists())

    def test_teardown_refuses_unbound_resources_after_interrupted_creation(self) -> None:
        runtime.atomic_json(
            self.root / "receipts/vm-create-attempt.json",
            {
                "schema_version": 1,
                "status": "running",
                "source_commit": self.commit,
                "config_sha256": runtime.sha256_file(runtime.CLUSTER / "config.json"),
                "state_root": str(self.root.resolve()),
                "vm": runtime.VM_NAME,
                "pool": runtime.POOL_NAME,
            },
        )
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB,
            "no verified pool directory identity",
        ):
            runtime.teardown(self.root)
        self.assertTrue(self.domain_present)
        self.assertTrue(self.pool_present)
        self.assertTrue(self.root.exists())

    def test_teardown_recovers_pool_only_interrupted_creation(self) -> None:
        self.prepare_create()
        original = self.run_fixture

        def interrupt_before_first_volume(argv, **kwargs):
            if (
                argv[:3] == ["virsh", "-c", runtime.LIBVIRT_URI]
                and argv[3] in {"vol-create-as", "vol-create"}
            ):
                raise KeyboardInterrupt("simulated pool-only interruption")
            return original(argv, **kwargs)

        self.runner.side_effect = interrupt_before_first_volume
        with self.assertRaisesRegex(
            KeyboardInterrupt,
            "simulated pool-only interruption",
        ):
            runtime.create_vm(self.root)

        attempt = json.loads(
            (self.root / "receipts/vm-create-attempt.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertFalse(self.domain_present)
        self.assertTrue(self.pool_present)
        self.assertEqual(attempt["pool_target"], self.pool_uuid)
        self.assertNotIn("volume_device", attempt)

        self.runner.side_effect = original
        result = runtime.teardown(self.root)
        self.assertEqual(result["status"], "retired")
        self.assertTrue(result["live_identity_verified"])
        self.assertFalse(self.pool_present)
        self.assertFalse(self.pool.exists())

    def test_teardown_recovers_exact_interrupted_creation(self) -> None:
        self.prepare_create()
        original_live_substrate = runtime._live_vm_substrate

        with mock.patch.object(
            runtime,
            "_live_vm_substrate",
            side_effect=KeyboardInterrupt("simulated hard interruption"),
        ):
            with self.assertRaisesRegex(
                KeyboardInterrupt,
                "simulated hard interruption",
            ):
                runtime.create_vm(self.root)

        attempt = json.loads(
            (self.root / "receipts/vm-create-attempt.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertTrue(self.domain_present)
        self.assertTrue(self.pool_present)
        self.assertFalse((self.root / "receipts/vm-create.json").exists())
        self.assertEqual(attempt["domain_target"], self.domain_uuid)
        self.assertEqual(attempt["pool_target"], self.pool_uuid)
        self.assertEqual(
            attempt["base_image_sha256"],
            self.config["vm"]["image"]["sha256"],
        )
        self.assertEqual(attempt["volume_device"], self.disk.stat().st_dev)
        self.assertEqual(attempt["volume_inode"], self.disk.stat().st_ino)
        self.assertEqual(
            attempt["pool_target_device"], self.pool.stat().st_dev
        )
        self.assertEqual(
            attempt["pool_target_inode"], self.pool.stat().st_ino
        )

        with mock.patch.object(
            runtime,
            "_live_vm_substrate",
            side_effect=original_live_substrate,
        ):
            result = runtime.teardown(self.root)
        self.assertEqual(result["status"], "retired")
        self.assertTrue(result["live_identity_verified"])
        self.assertFalse(self.domain_present)
        self.assertFalse(self.pool_present)
        self.assertFalse(self.disk.exists())
        self.assertFalse(self.base.exists())

    def test_teardown_rejects_interrupted_creation_identity_drift(self) -> None:
        self.prepare_create()
        with mock.patch.object(
            runtime,
            "_live_vm_substrate",
            side_effect=KeyboardInterrupt("simulated hard interruption"),
        ):
            with self.assertRaises(KeyboardInterrupt):
                runtime.create_vm(self.root)
        self.domain_uuid = "44444444-4444-4444-8444-444444444444"
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB,
            "domain UUID drifted",
        ):
            runtime.teardown(self.root)
        self.assertTrue(self.domain_present)
        self.assertTrue(self.pool_present)

    def test_live_readback_captures_actual_substrate_and_base_volume_digest(self) -> None:
        observed = runtime._live_vm_substrate(self.root, self.config)
        expected = vm_substrate_fixture()
        expected.update(disk_device=self.disk.stat().st_dev, disk_inode=self.disk.stat().st_ino)
        self.assertEqual(observed, expected)
        self.assertFalse(list(self.root.glob(".vm-substrate-*")))

    def test_live_readback_rejects_xml_contract_drift_and_missing_evidence(self) -> None:
        cases = (
            ("live", "type='kvm'", "type='qemu'"),
            ("live", " id='7'", ""),
            ("live", runtime.VM_NAME, "retained-vm"),
            ("live", "current='6'>6", "current='6'>8"),
            ("live", "current='6'>6", "current='2'>6"),
            ("live", "12582912", "8388608"),
            ("live", "network='default'", "network='bridged'"),
            ("live", "bridge='virbr0'", "bridge='other-network'"),
            ("live", "type='network'", "type='bridge'"),
            ("live", str(self.disk), "/other/guest.qcow2"),
            ("live", str(self.base), "/other/base.qcow2"),
            ("live", "</devices>", "<filesystem/></devices>"),
            ("live", "</devices>", "<disk device='disk'/></devices>"),
            ("live", "</devices>", "<interface type='bridge'/></devices>"),
            ("inactive", "current='6'>6", "current='4'>4"),
            ("network", "mode='nat'", "mode='bridge'"),
            ("network", "<forward mode='nat'/>", ""),
            ("pool", str(self.pool), "/other/pool"),
            ("disk", str(60 * 1024**3), str(30 * 1024**3)),
            ("disk", str(self.base), "/other/base.qcow2"),
            ("disk", f"<key>{self.disk}</key>", "<key>wrong</key>"),
            ("base", "type='qcow2'", "type='raw'"),
            ("live", "<vcpu current='6'>6</vcpu>", ""),
            ("live", "<domain", "<malformed"),
        )
        original = self.xml.copy()
        for name, before, after in cases:
            with self.subTest(readback=name, before=before, after=after):
                self.xml = original.copy()
                self.assertIn(before, self.xml[name])
                self.xml[name] = self.xml[name].replace(before, after)
                if name == "live":
                    self.xml["inactive"] = self.xml["live"].replace(" id='7'", "")
                with self.assertRaises(runtime.RuntimeErrorEB):
                    runtime._live_vm_substrate(self.root, self.config)

    def test_live_readback_rejects_qemu_disk_and_backing_drift(self) -> None:
        original = json.dumps(self.qmp)
        for key, value in (
            ("filename", "/other/guest.qcow2"), ("format", "raw"),
            ("virtual-size", 59 * 1024**3), ("backing-image", {}),
            ("backing-image", {"filename": "/other/base", "format": "qcow2"}),
            ("backing-image", {"filename": str(self.base), "format": "raw"}),
            ("backing-image", {
                "filename": str(self.base), "format": "qcow2", "backing-filename": "/old/base",
            }),
        ):
            with self.subTest(key=key, value=value):
                self.qmp = json.loads(original)
                self.qmp["return"][0]["inserted"]["image"][key] = value
                with self.assertRaisesRegex(runtime.RuntimeErrorEB, "QEMU capacity/backing"):
                    runtime._live_vm_substrate(self.root, self.config)
        for response in ({"return": []}, {"error": {"desc": "unavailable"}}):
            self.qmp = response
            with self.assertRaises(runtime.RuntimeErrorEB):
                runtime._live_vm_substrate(self.root, self.config)

    def test_status_requires_revision_config_and_complete_substrate_receipt(self) -> None:
        receipt = self.write_vm_receipt()
        self.prepare_status()
        vm_path = self.root / "receipts/vm-create.json"
        cases = [None, [], {}, {**receipt, "source_commit": None},
                 {**receipt, "source_commit": "main"}, {**receipt, "source_commit": "b" * 40},
                 {**receipt, "status": "failed"}, {**receipt, "state_root": "/wrong/root"},
                 {**receipt, "config_sha256": "0" * 64},
                 {**receipt, "substrate": {}},
                 {**receipt, "substrate": {**receipt["substrate"], "vcpu": 4}},
                 {**receipt, "substrate": {**receipt["substrate"], "base_image_sha256": "0" * 64}}]
        for value in cases:
            with self.subTest(receipt=value):
                vm_path.unlink(missing_ok=True)
                if value is not None:
                    vm_path.write_text(json.dumps(value), encoding="utf-8")
                for name in ("status.json", "portability.json"):
                    runtime.atomic_json(self.root / "receipts" / name, {"status": "stale"})
                self.runner.reset_mock()
                with self.assertRaises(runtime.RuntimeErrorEB):
                    runtime.status(self.root)
                self.runner.assert_not_called()
                self.tools.assert_not_called()
                for name in ("status.json", "portability.json"):
                    self.assertFalse((self.root / "receipts" / name).exists())

    def test_status_records_matching_live_substrate_and_creation_receipt_hash(self) -> None:
        receipt = self.write_vm_receipt()
        self.prepare_status()
        result = runtime.status(self.root)
        self.assertEqual(result["status"], "observed")
        self.assertEqual(result["vm_substrate"], receipt["substrate"])
        self.assertEqual(result["cilium"]["chart"], self.cilium_chart)
        self.assertFalse(result["cilium"]["kube_proxy_present"])
        self.assertTrue(result["cilium"]["daemonset_images_canonical"])
        self.assertTrue(result["cilium"]["daemonset_pods"]["images_canonical"])
        self.assertTrue(result["cilium"]["operator_pods"]["images_canonical"])
        self.assertEqual(result["k3s_runtime"], self.k3s_runtime)
        self.assertEqual(
            set(result["flux_controllers"]),
            runtime.EXPECTED_FLUX_CONTROLLERS,
        )
        self.assertEqual(
            set(result["flux_runtime_image_ids_baseline"]),
            runtime.EXPECTED_FLUX_CONTROLLERS,
        )
        self.assertTrue(
            all(
                controller["images_canonical"]
                and controller["pods"]["images_canonical"]
                for controller in result["flux_controllers"].values()
            )
        )
        self.assertEqual(
            result["flux_bootstrap_sha256"],
            self.flux_contract["bootstrap_sha256"],
        )
        self.assertTrue(result["runtime_contract"]["policy_specs_canonical"])
        self.assertTrue(
            result["runtime_contract"]["temporary_model_egress_absent"]
        )
        self.assertEqual(result["vm_create_sha256"], runtime.sha256_file(
            self.root / "receipts/vm-create.json",
        ))
        self.assertEqual(
            result["gateway_data_plane"]["gateway"],
            "http://192.168.122.20",
        )
        self.gateway_data_plane.assert_called_once_with(self.root, self.commit)
        self.semantic_provider.assert_called_once_with(self.root, self.commit)
        self.recovery_state.assert_called_once_with(self.root, self.commit)
        self.assertTrue(result["semantic_provider"]["embedding_probe"])
        self.assertEqual(result["recovery_state"]["rpo_seconds"], 0)
        attempt = json.loads((self.root / "receipts/status-attempt.json").read_text())
        self.assertEqual(attempt["status"], "pass")
        self.assertEqual(attempt["receipt_sha256"], runtime.sha256_file(self.root / "receipts/status.json"))

    def test_status_revalidates_live_cilium_contract(self) -> None:
        self.write_vm_receipt()
        self.prepare_status()

        healthy_chart = self.cilium_chart
        healthy_values = json.loads(json.dumps(self.cilium_values))
        healthy_config = json.loads(json.dumps(self.cilium_config))
        healthy_daemonset = json.loads(json.dumps(self.cilium_daemonset))
        healthy_cilium_pods = json.loads(json.dumps(self.cilium_pods))
        healthy_operator_pods = json.loads(json.dumps(self.cilium_operator_pods))
        healthy_relay = json.loads(json.dumps(self.cilium_relay))
        healthy_relay_pods = json.loads(json.dumps(self.cilium_relay_pods))

        self.cilium_chart = "cilium-9.9.9"
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "Helm release"):
            runtime.status(self.root)
        self.cilium_chart = healthy_chart

        self.cilium_values = {
            "gatewayAPI": {"enabled": False},
            "kubeProxyReplacement": True,
        }
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "configuration drifted"):
            runtime.status(self.root)

        self.cilium_values = {
            "gatewayAPI": {"enabled": True},
            "kubeProxyReplacement": False,
        }
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "configuration drifted"):
            runtime.status(self.root)

        self.cilium_values = {
            "gatewayAPI": {"enabled": "true"},
            "kubeProxyReplacement": "true",
        }
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "configuration drifted"):
            runtime.status(self.root)

        self.cilium_values = json.loads(json.dumps(healthy_values))
        self.cilium_values["hubble"]["relay"]["enabled"] = False
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "configuration drifted"):
            runtime.status(self.root)
        self.cilium_values = healthy_values

        self.cilium_config = json.loads(json.dumps(healthy_config))
        self.cilium_config["data"]["enable-policy"] = "never"
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB, "ConfigMap drifted"
        ):
            runtime.status(self.root)
        self.cilium_config = json.loads(json.dumps(healthy_config))

        self.cilium_daemonset = json.loads(json.dumps(healthy_daemonset))
        self.cilium_daemonset["status"]["numberReady"] = 0
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "DaemonSet"):
            runtime.status(self.root)

        self.cilium_daemonset = json.loads(json.dumps(healthy_daemonset))
        self.cilium_daemonset["status"]["observedGeneration"] = 2
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "DaemonSet"):
            runtime.status(self.root)
        self.cilium_daemonset = json.loads(json.dumps(healthy_daemonset))
        self.cilium_daemonset["spec"]["template"]["spec"]["containers"][0][
            "image"
        ] = "quay.io/cilium/cilium:v9.9.9"
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB, "DaemonSet images drifted"
        ):
            runtime.status(self.root)
        self.cilium_daemonset = json.loads(json.dumps(healthy_daemonset))
        self.cilium_daemonset["spec"]["template"]["spec"]["containers"][0][
            "args"
        ] = ["--shadow-runtime-mode"]
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB, "DaemonSet pod contract drifted"
        ):
            runtime.status(self.root)

        self.cilium_daemonset = json.loads(json.dumps(healthy_daemonset))
        self.cilium_daemonset["spec"]["template"]["spec"]["initContainers"][0][
            "restartPolicy"
        ] = "Always"
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB, "DaemonSet pod contract drifted"
        ):
            runtime.status(self.root)

        self.cilium_daemonset = json.loads(json.dumps(healthy_daemonset))
        self.cilium_daemonset["spec"]["updateStrategy"] = {"type": "OnDelete"}
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB, "DaemonSet rollout drifted"
        ):
            runtime.status(self.root)
        self.cilium_daemonset = healthy_daemonset

        healthy_operator = json.loads(json.dumps(self.cilium_operator))
        self.cilium_operator = json.loads(json.dumps(healthy_operator))
        self.cilium_operator["status"]["readyReplicas"] = 0
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "not currently available"):
            runtime.status(self.root)

        self.cilium_operator = json.loads(json.dumps(healthy_operator))
        self.cilium_operator["spec"]["template"]["spec"]["containers"][0][
            "image"
        ] = "quay.io/cilium/operator-generic:v9.9.9"
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "operator images drifted"):
            runtime.status(self.root)
        self.cilium_operator = json.loads(json.dumps(healthy_operator))
        self.cilium_operator["spec"]["template"]["spec"][
            "serviceAccountName"
        ] = "shadow-operator"
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB, "operator pod contract drifted"
        ):
            runtime.status(self.root)

        self.cilium_operator = json.loads(json.dumps(healthy_operator))
        self.cilium_operator["spec"]["strategy"] = {"type": "Recreate"}
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB, "operator lifecycle drifted"
        ):
            runtime.status(self.root)
        self.cilium_operator = healthy_operator

        self.cilium_pods = json.loads(json.dumps(healthy_cilium_pods))
        self.cilium_pods[0]["spec"]["activeDeadlineSeconds"] = 120
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB,
            "activeDeadlineSeconds drifted from workload contract",
        ):
            runtime.status(self.root)

        self.cilium_pods = json.loads(json.dumps(healthy_cilium_pods))
        self.cilium_pods[0]["spec"]["containers"][0]["image"] = (
            "quay.io/cilium/cilium:v9.9.9"
        )
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "requested images drifted"):
            runtime.status(self.root)
        self.cilium_pods = json.loads(json.dumps(healthy_cilium_pods))
        self.cilium_pods[0]["status"]["initContainerStatuses"][0]["imageID"] = (
            "containerd://not-a-digest"
        )
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "runtime image ID drifted"):
            runtime.status(self.root)

        self.cilium_pods = json.loads(json.dumps(healthy_cilium_pods))
        self.cilium_pods[0]["status"]["containerStatuses"][0]["imageID"] = (
            "containerd://sha256:" + "0" * 64
        )
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB, "drifted from platform installation"
        ):
            runtime.status(self.root)
        self.cilium_pods = healthy_cilium_pods

        self.cilium_operator_pods = json.loads(json.dumps(healthy_operator_pods))
        self.cilium_operator_pods[0]["status"]["containerStatuses"][0][
            "imageID"
        ] = "containerd://not-a-digest"
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "runtime image ID drifted"):
            runtime.status(self.root)
        self.cilium_operator_pods = healthy_operator_pods

        self.cilium_relay = json.loads(json.dumps(healthy_relay))
        self.cilium_relay["status"]["readyReplicas"] = 0
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB, "not currently available"
        ):
            runtime.status(self.root)

        self.cilium_relay = json.loads(json.dumps(healthy_relay))
        self.cilium_relay["spec"]["progressDeadlineSeconds"] = 42
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB, "Hubble Relay lifecycle drifted"
        ):
            runtime.status(self.root)

        self.cilium_relay = json.loads(json.dumps(healthy_relay))
        self.cilium_relay["spec"]["revisionHistoryLimit"] = 3
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB, "Hubble Relay lifecycle drifted"
        ):
            runtime.status(self.root)
        self.cilium_relay = healthy_relay

        self.cilium_relay_pods = json.loads(json.dumps(healthy_relay_pods))
        self.cilium_relay_pods[0]["status"]["containerStatuses"][0][
            "imageID"
        ] = "containerd://not-a-digest"
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB, "runtime image ID drifted"
        ):
            runtime.status(self.root)
        self.cilium_relay_pods = healthy_relay_pods

        self.kube_proxy_daemonsets = [{"metadata": {"name": "kube-proxy"}}]
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "kube-proxy is present"):
            runtime.status(self.root)
        self.kube_proxy_daemonsets = []

        self.kube_proxy_pods = [{"metadata": {"name": "kube-proxy-node"}}]
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "kube-proxy is present"):
            runtime.status(self.root)
        self.kube_proxy_pods = []

    def test_status_requires_live_flux_controllers(self) -> None:
        self.write_vm_receipt()
        self.prepare_status()

        healthy = json.loads(json.dumps(self.flux_controller_deployments))
        healthy_pods = json.loads(json.dumps(self.flux_controller_pods))
        self.flux_controller_deployments = healthy[:-1]
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB, "incomplete or duplicated"
        ):
            runtime.status(self.root)

        self.flux_controller_deployments = json.loads(json.dumps(healthy))
        self.flux_controller_deployments[0]["status"]["readyReplicas"] = 0
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "not currently available"):
            runtime.status(self.root)

        self.flux_controller_deployments = json.loads(json.dumps(healthy))
        self.flux_controller_deployments[0]["spec"]["template"]["spec"][
            "containers"
        ][0]["image"] = "ghcr.io/fluxcd/other:vfixture"
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB, "Deployment contract drifted"
        ):
            runtime.status(self.root)

        for field, value in (
            ("args", ["--shadow-mode"]),
            ("env", [{"name": "SHADOW", "value": "1"}]),
            (
                "securityContext",
                {"allowPrivilegeEscalation": True},
            ),
        ):
            with self.subTest(deployment_container_field=field):
                self.flux_controller_deployments = json.loads(
                    json.dumps(healthy)
                )
                self.flux_controller_deployments[0]["spec"]["template"]["spec"][
                    "containers"
                ][0][field] = value
                with self.assertRaisesRegex(
                    runtime.RuntimeErrorEB, "Deployment contract drifted"
                ):
                    runtime.status(self.root)

        self.flux_controller_deployments = json.loads(json.dumps(healthy))
        self.flux_controller_deployments[0]["spec"]["template"]["spec"][
            "serviceAccountName"
        ] = "shadow-account"
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB, "Deployment contract drifted"
        ):
            runtime.status(self.root)

        self.flux_controller_deployments = json.loads(json.dumps(healthy))
        self.flux_controller_deployments[0]["spec"]["template"]["spec"][
            "volumes"
        ] = [{"name": "shadow", "emptyDir": {}}]
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB, "Deployment contract drifted"
        ):
            runtime.status(self.root)

        self.flux_controller_deployments = json.loads(json.dumps(healthy))
        self.flux_controller_pods = json.loads(json.dumps(healthy_pods))
        self.flux_controller_pods[0]["spec"]["containers"][0]["env"] = [
            {"name": "SHADOW", "value": "1"}
        ]
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB, "Pod contract drifted"
        ):
            runtime.status(self.root)

        self.flux_controller_pods = json.loads(json.dumps(healthy_pods))
        self.flux_controller_pods[0]["spec"]["volumes"] = [
            {"name": "shadow", "emptyDir": {}}
        ]
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB, "Pod contract drifted"
        ):
            runtime.status(self.root)

        self.flux_controller_pods = json.loads(json.dumps(healthy_pods))
        self.flux_controller_pods[0]["spec"]["containers"][0]["image"] = (
            "ghcr.io/fluxcd/other:vfixture"
        )
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB, "requested images drifted"
        ):
            runtime.status(self.root)

        self.flux_controller_pods = json.loads(json.dumps(healthy_pods))
        self.flux_controller_pods[0]["status"]["containerStatuses"][0][
            "imageID"
        ] = "containerd://sha256:" + "0" * 64
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB, "drifted from platform installation"
        ):
            runtime.status(self.root)

        first_name = self.flux_controller_pods[0]["metadata"]["labels"][
            "app.kubernetes.io/name"
        ]
        self.flux_controller_pods = [
            pod
            for pod in json.loads(json.dumps(healthy_pods))
            if pod["metadata"]["labels"]["app.kubernetes.io/name"] != first_name
        ]
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB, "exact replica contract"
        ):
            runtime.status(self.root)

        self.flux_controller_pods = json.loads(
            json.dumps(healthy_pods)
        )
        extra_flux_pod = json.loads(
            json.dumps(healthy_pods[0])
        )
        extra_flux_pod["metadata"]["name"] = "shadow-controller-0"
        extra_flux_pod["metadata"]["labels"] = {
            "app.kubernetes.io/name": "shadow-controller"
        }
        self.flux_controller_pods.append(extra_flux_pod)
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB,
            "Pod inventory contains noncanonical Pods",
        ):
            runtime.status(self.root)

        self.flux_controller_deployments = healthy
        self.flux_controller_pods = healthy_pods
        result = runtime.status(self.root)
        self.assertEqual(
            set(result["flux_controllers"]),
            runtime.EXPECTED_FLUX_CONTROLLERS,
        )
        self.assertTrue(
            all(
                controller["pods"]["images_canonical"]
                for controller in result["flux_controllers"].values()
            )
        )

    def test_status_revalidates_flux_specs_against_bootstrap(self) -> None:
        self.write_vm_receipt()
        self.prepare_status()

        healthy_source = json.loads(json.dumps(self.live_flux_source_spec))
        healthy_kustomizations = json.loads(
            json.dumps(self.live_flux_kustomization_specs)
        )

        self.live_flux_source_spec["url"] = "https://example.invalid/other"
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "spec drifted"):
            runtime.status(self.root)
        self.live_flux_source_spec = healthy_source

        self.live_flux_kustomization_specs[
            "commonthing-experiment-b-app"
        ]["path"] = "./platform/clusters/experiment-b/namespaces"
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "spec drifted"):
            runtime.status(self.root)
        self.live_flux_kustomization_specs = json.loads(
            json.dumps(healthy_kustomizations)
        )

        self.live_flux_kustomization_specs[
            "commonthing-experiment-b-app"
        ]["prune"] = False
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "spec drifted"):
            runtime.status(self.root)
        self.live_flux_kustomization_specs = healthy_kustomizations

        result = runtime.status(self.root)
        self.assertEqual(
            set(result["flux"]),
            runtime.EXPECTED_FLUX_KUSTOMIZATIONS,
        )


    def test_status_revalidates_runtime_config_and_network_policy_contract(self) -> None:
        self.write_vm_receipt()
        self.prepare_status()

        healthy_config_map = json.loads(json.dumps(self.runtime_config_map))
        healthy_policies = json.loads(json.dumps(self.network_policies))
        healthy_data_policies = json.loads(json.dumps(self.data_network_policies))
        healthy_cilium = json.loads(json.dumps(self.cilium_network_policies))

        self.runtime_config_map["data"]["NATS_URL"] = "nats://drift.invalid:4222"
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "runtime ConfigMap drifted"):
            runtime.status(self.root)
        self.runtime_config_map = healthy_config_map

        self.network_policies.append(
            {
                "metadata": {
                    "namespace": runtime.APP_NAMESPACE,
                    "name": "commonthing-experiment-b-model-bootstrap-egress",
                },
                "spec": {
                    "podSelector": {},
                    "policyTypes": ["Egress"],
                    "egress": [
                        {
                            "to": [{"ipBlock": {"cidr": "0.0.0.0/0"}}],
                            "ports": [{"protocol": "TCP", "port": 443}],
                        }
                    ],
                },
            }
        )
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "NetworkPolicy contract drifted"):
            runtime.status(self.root)
        self.network_policies = json.loads(json.dumps(healthy_policies))

        self.network_policies[0]["spec"] = {
            "podSelector": {},
            "policyTypes": ["Egress"],
            "egress": [{"to": [{"ipBlock": {"cidr": "0.0.0.0/0"}}]}],
        }
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "NetworkPolicy contract drifted"):
            runtime.status(self.root)
        self.network_policies = healthy_policies

        self.data_network_policies = json.loads(json.dumps(healthy_data_policies[:-1]))
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB, "data NetworkPolicy contract drifted"
        ):
            runtime.status(self.root)

        self.data_network_policies = json.loads(json.dumps(healthy_data_policies))
        postgres_policy = next(
            item
            for item in self.data_network_policies
            if item["metadata"]["name"] == "allow-app-postgres-access"
        )
        postgres_policy["spec"]["ingress"] = [{"from": [{}]}]
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB, "data NetworkPolicy contract drifted"
        ):
            runtime.status(self.root)
        self.data_network_policies = healthy_data_policies

        self.cilium_network_policies[0]["spec"]["egress"] = [
            {"toEntities": ["world"]}
        ]
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB, "CiliumNetworkPolicy contract drifted"
        ):
            runtime.status(self.root)
        self.cilium_network_policies = healthy_cilium

        result = runtime.status(self.root)
        self.assertTrue(result["runtime_contract"]["policy_specs_canonical"])
        self.assertTrue(result["runtime_contract"]["temporary_model_egress_absent"])
        self.assertEqual(
            set(result["runtime_contract"]["data_network_policy_names"]),
            {
                "default-deny",
                "allow-app-postgres-access",
                "allow-app-nats-access",
                "allow-dns",
            },
        )

    def test_status_requires_ready_nonterminating_node(self) -> None:
        self.write_vm_receipt()
        self.prepare_status()
        healthy = json.loads(json.dumps(self.node))

        self.node["status"]["conditions"] = [{"type": "Ready", "status": "False"}]
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "not Ready"):
            runtime.status(self.root)

        self.node = json.loads(json.dumps(healthy))
        self.node_inventory["items"] = [self.node]
        self.node["status"]["conditions"] = []
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "not Ready"):
            runtime.status(self.root)

        self.node = json.loads(json.dumps(healthy))
        self.node_inventory["items"] = [self.node]
        self.node["metadata"]["deletionTimestamp"] = "2026-09-27T00:00:00Z"
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "identity/deletion"):
            runtime.status(self.root)

        self.node = healthy
        self.node_inventory["items"] = [self.node]

    def test_status_requires_live_data_deployments(self) -> None:
        self.write_vm_receipt()
        self.prepare_status()
        healthy = json.loads(json.dumps(self.data_deployments))
        healthy_pods = json.loads(json.dumps(self.data_pods))

        result = runtime.status(self.root)
        self.assertEqual(set(result["data_deployments"]), {"postgres", "nats"})
        self.assertTrue(result["data_deployments"]["postgres"]["images_canonical"])
        self.assertTrue(result["data_deployments"]["nats"]["images_canonical"])

        self.data_pods = json.loads(json.dumps(healthy_pods))
        extra_pod = json.loads(json.dumps(healthy_pods["nats"][0]))
        extra_pod["metadata"]["name"] = "unversioned-data-writer"
        extra_pod["metadata"]["labels"] = {"app": "unversioned-writer"}
        self.data_pods["nats"].append(extra_pod)
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB,
            "noncanonical Pods",
        ):
            runtime.status(self.root)
        self.data_pods = json.loads(json.dumps(healthy_pods))

        self.data_deployments = json.loads(json.dumps(healthy))
        self.data_deployments["postgres"]["spec"]["replicas"] = 2
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "not currently available"):
            runtime.status(self.root)

        self.data_deployments = json.loads(json.dumps(healthy))
        self.data_deployments["nats"]["spec"]["template"]["spec"]["containers"][0][
            "image"
        ] = "nats:2.10-alpine@sha256:" + "0" * 64
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "images drifted"):
            runtime.status(self.root)

        self.data_deployments = json.loads(json.dumps(healthy))
        self.data_deployments["postgres"]["spec"][
            "minReadySeconds"
        ] = 17
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB,
            "Deployment contract drifted",
        ):
            runtime.status(self.root)

        self.data_deployments = healthy

        self.data_pods = json.loads(json.dumps(healthy_pods))
        self.data_pods["postgres"][0]["spec"]["containers"][0]["image"] = (
            "postgres:16@sha256:" + "0" * 64
        )
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "requested images drifted"):
            runtime.status(self.root)

        self.data_pods = json.loads(json.dumps(healthy_pods))
        self.data_pods["nats"][0]["status"]["containerStatuses"][0]["imageID"] = (
            "containerd://sha256:" + "0" * 64
        )
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "runtime image ID drifted"):
            runtime.status(self.root)

        self.data_pods = json.loads(json.dumps(healthy_pods))
        self.data_pods["postgres"] = []
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "exact replica contract"):
            runtime.status(self.root)

        self.data_pods = healthy_pods

        self.data_deployments = json.loads(json.dumps(healthy))
        nats_container = self.data_deployments["nats"]["spec"]["template"][
            "spec"
        ]["containers"][0]
        nats_container["args"] = ["-js", "-sd", "/tmp/drift", "-m", "8222"]
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB,
            "Deployment contract drifted",
        ):
            runtime.status(self.root)

        self.data_deployments = json.loads(json.dumps(healthy))
        self.data_pods = json.loads(json.dumps(healthy_pods))
        postgres_container = self.data_pods["postgres"][0]["spec"][
            "containers"
        ][0]
        postgres_container["securityContext"]["readOnlyRootFilesystem"] = False
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB,
            "data Pod contract drifted: postgres",
        ):
            runtime.status(self.root)

        self.data_deployments = healthy
        self.data_pods = healthy_pods


    def test_status_revalidates_application_service_selector_and_ports(self) -> None:
        self.write_vm_receipt()
        self.prepare_status()

        result = runtime.status(self.root)
        self.assertEqual(
            set(result["application_services"]),
            {"weltgewebe-api", "weltgewebe-web"},
        )
        self.assertTrue(
            result["application_services"]["weltgewebe-api"]["canonical"]
        )

        healthy = json.loads(json.dumps(self.application_services))
        self.application_services["weltgewebe-api"]["spec"]["selector"] = {
            "app.kubernetes.io/name": "other"
        }
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB,
            "application Service spec drifted: weltgewebe-api",
        ):
            runtime.status(self.root)

        self.application_services = json.loads(json.dumps(healthy))
        self.application_services["weltgewebe-web"]["spec"]["ports"][0][
            "port"
        ] += 1
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB,
            "application Service spec drifted: weltgewebe-web",
        ):
            runtime.status(self.root)

        self.application_services = json.loads(json.dumps(healthy))
        self.application_services["weltgewebe-api"]["spec"]["type"] = (
            "NodePort"
        )
        self.application_services["weltgewebe-api"]["spec"]["ports"][0][
            "nodePort"
        ] = 30080
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB,
            "application Service spec drifted: weltgewebe-api",
        ):
            runtime.status(self.root)

        self.application_services = json.loads(json.dumps(healthy))
        self.application_services["weltgewebe-web"]["spec"][
            "externalIPs"
        ] = ["203.0.113.42"]
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB,
            "application Service spec drifted: weltgewebe-web",
        ):
            runtime.status(self.root)
        self.application_services = healthy

    def test_rendered_application_service_contract_tracks_overlay_services(self) -> None:
        api_digest = "sha256:" + "b" * 64
        web_digest = "sha256:" + "c" * 64
        rendered = """
apiVersion: v1
kind: Service
metadata:
  name: weltgewebe-api
  namespace: commonthing-experiment-b
spec:
  selector:
    app.kubernetes.io/name: weltgewebe-api
  ports:
    - name: http
      port: 8080
      targetPort: api-http
      protocol: TCP
---
apiVersion: v1
kind: Service
metadata:
  name: weltgewebe-web
  namespace: commonthing-experiment-b
spec:
  selector:
    app.kubernetes.io/name: weltgewebe-web
  ports:
    - name: http
      port: 8080
      targetPort: web-http
      protocol: TCP
"""
        with mock.patch.object(
            runtime,
            "_source_commit_application_render",
            return_value=rendered,
        ):
            result = runtime._rendered_application_service_contract(
                self.root,
                {"api_digest": api_digest, "web_digest": web_digest},
            )
        self.assertEqual(
            result["weltgewebe-api"]["spec"]["targetPort"]
            if "targetPort" in result["weltgewebe-api"]["spec"]
            else result["weltgewebe-api"]["spec"]["ports"][0]["targetPort"],
            "api-http",
        )
        self.assertEqual(
            result["weltgewebe-web"]["spec"]["ports"][0]["targetPort"],
            "web-http",
        )
        self.assertEqual(
            result["weltgewebe-api"]["spec"]["type"], "ClusterIP"
        )
        self.assertIsNone(
            result["weltgewebe-api"]["spec"]["ports"][0]["nodePort"]
        )
        self.assertEqual(
            result["weltgewebe-api"]["spec"]["externalIPs"], []
        )

    def test_status_revalidates_flux_release_config_map(self) -> None:
        self.write_vm_receipt()
        self.prepare_status()
        result = runtime.status(self.root)
        self.assertTrue(
            result["flux_release_config_map"]["canonical"]
        )
        self.release_config_map["data"]["API_DIGEST"] = (
            "sha256:" + "0" * 64
        )
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB,
            "release ConfigMap contract drifted",
        ):
            runtime.status(self.root)

    def test_status_revalidates_application_pdbs(self) -> None:
        self.write_vm_receipt()
        self.prepare_status()
        result = runtime.status(self.root)
        self.assertEqual(
            set(result["application_disruption_budgets"]),
            {"weltgewebe-api", "weltgewebe-web"},
        )
        self.assertTrue(
            result["application_disruption_budgets"][
                "weltgewebe-api"
            ]["canonical"]
        )
        self.application_pdbs["weltgewebe-api"]["spec"][
            "minAvailable"
        ] = 0
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB,
            "PDB contract drifted: weltgewebe-api",
        ):
            runtime.status(self.root)

    def test_status_revalidates_application_service_accounts(self) -> None:
        self.write_vm_receipt()
        self.prepare_status()

        result = runtime.status(self.root)
        self.assertEqual(
            set(result["application_service_accounts"]),
            {"weltgewebe-api", "weltgewebe-web"},
        )
        self.assertTrue(
            result["application_service_accounts"]["weltgewebe-api"][
                "canonical"
            ]
        )

        self.application_service_accounts["weltgewebe-api"][
            "automountServiceAccountToken"
        ] = True
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB,
            "ServiceAccount contract drifted: weltgewebe-api",
        ):
            runtime.status(self.root)

        self.application_service_accounts["weltgewebe-api"][
            "automountServiceAccountToken"
        ] = False
        original_fixture = self.kubernetes_fixture

        def missing_account(root, arguments):
            if arguments == [
                "-n",
                runtime.APP_NAMESPACE,
                "get",
                "serviceaccount",
                "weltgewebe-web",
            ]:
                return {}
            return original_fixture(root, arguments)

        with mock.patch.object(
            runtime, "_kubectl_json", side_effect=missing_account
        ):
            with self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "ServiceAccount identity drifted: weltgewebe-web",
            ):
                runtime.status(self.root)

    def test_rendered_service_account_contract_tracks_deployment_references(self) -> None:
        api_digest = "sha256:" + "b" * 64
        web_digest = "sha256:" + "c" * 64
        rendered = """
apiVersion: v1
kind: ServiceAccount
metadata:
  name: weltgewebe-api
  namespace: commonthing-experiment-b
  labels:
    app.kubernetes.io/name: weltgewebe-api
automountServiceAccountToken: false
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: weltgewebe-web
  namespace: commonthing-experiment-b
  labels:
    app.kubernetes.io/name: weltgewebe-web
automountServiceAccountToken: false
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: weltgewebe-api
  namespace: commonthing-experiment-b
spec:
  template:
    spec:
      serviceAccountName: weltgewebe-api
      containers:
        - name: api
          image: example.invalid/api
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: weltgewebe-web
  namespace: commonthing-experiment-b
spec:
  template:
    spec:
      serviceAccountName: weltgewebe-web
      containers:
        - name: web
          image: example.invalid/web
"""
        with mock.patch.object(
            runtime,
            "_source_commit_application_render",
            return_value=rendered,
        ):
            expected = runtime._rendered_application_service_account_contract(
                self.root,
                {
                    "api_digest": api_digest,
                    "web_digest": web_digest,
                },
            )
        self.assertEqual(
            set(expected),
            {"weltgewebe-api", "weltgewebe-web"},
        )
        self.assertFalse(
            expected["weltgewebe-api"]["contract"][
                "automountServiceAccountToken"
            ]
        )
        self.assertRegex(
            expected["weltgewebe-web"]["contract_sha256"],
            r"^[0-9a-f]{64}$",
        )

    def test_status_requires_running_application_pod_images(self) -> None:
        self.write_vm_receipt()
        self.prepare_status()

        healthy_pods = json.loads(json.dumps(self.application_pods))
        result = runtime.status(self.root)
        self.assertEqual(
            set(result["pods"]),
            {"weltgewebe-api", "weltgewebe-web"},
        )
        self.assertTrue(result["pods"]["weltgewebe-api"]["images_canonical"])
        self.assertTrue(result["pods"]["weltgewebe-web"]["images_canonical"])
        self.assertEqual(
            result["application_pod_inventory"],
            sorted(
                [
                    *result["application_workloads"]["weltgewebe-api"]["pod_names"],
                    *result["application_workloads"]["weltgewebe-web"]["pod_names"],
                    *result["migration"]["pod_names"],
                ]
            ),
        )

        self.application_extra_pods = [
            {
                "metadata": {
                    "name": "unversioned-app-writer",
                    "namespace": runtime.APP_NAMESPACE,
                    "labels": {"app": "unversioned-writer"},
                }
            }
        ]
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB,
            "application Pod inventory contains noncanonical Pods",
        ):
            runtime.status(self.root)
        self.application_extra_pods = []

        self.application_pods = json.loads(json.dumps(healthy_pods))
        api_pod = self.application_pods["weltgewebe-api"][0]
        next(
            container
            for container in api_pod["spec"]["containers"]
            if container["name"] == "api"
        )["image"] = "ghcr.io/heimgewebe/commonthing-api@sha256:" + "0" * 64
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB, "requested images drifted"
        ):
            runtime.status(self.root)

        self.application_pods = json.loads(json.dumps(healthy_pods))
        self.application_pods["weltgewebe-web"] = self.application_pods[
            "weltgewebe-web"
        ][:-1]
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB, "exact replica contract"
        ):
            runtime.status(self.root)

        self.application_pods = json.loads(json.dumps(healthy_pods))
        api_status = next(
            item
            for item in self.application_pods["weltgewebe-api"][0]["status"][
                "containerStatuses"
            ]
            if item["name"] == "api"
        )
        api_status["imageID"] = (
            "docker-pullable://ghcr.io/heimgewebe/commonthing-api@sha256:"
            + "0" * 64
        )
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB, "runtime image ID drifted"
        ):
            runtime.status(self.root)

        self.application_pods = json.loads(json.dumps(healthy_pods))
        api_status = next(
            item
            for item in self.application_pods["weltgewebe-api"][0]["status"][
                "containerStatuses"
            ]
            if item["name"] == "api"
        )
        api_status["imageID"] = "containerd://sha256:" + "b" * 64
        result = runtime.status(self.root)
        self.assertTrue(result["pods"]["weltgewebe-api"]["images_canonical"])

        self.application_pods = healthy_pods

    def test_status_requires_live_expected_secrets_without_recording_values(self) -> None:
        self.write_vm_receipt()
        self.prepare_status()

        healthy = json.loads(json.dumps(self.live_secrets))
        result = runtime.status(self.root)
        self.assertEqual(
            set(result["secrets"]),
            {"database", "runtime", "registry"},
        )
        rendered = json.dumps(result["secrets"], sort_keys=True)
        for encoded_value in (
            "dXNlcg==",
            "ZGI=",
            "cGFzcw==",
            "cG9zdGdyZXNxbDovL3VzZXI6cGFzc0Bwb3N0Z3Jlcy5jb21tb250aGluZy1kYXRhLnN2Yy5jbHVzdGVyLmxvY2FsOjU0MzIvZGI=",
            "e30=",
        ):
            self.assertNotIn(encoded_value, rendered)
        self.assertNotIn("postgresql://user:pass@", rendered)
        self.assertNotIn('"content_sha256"', rendered)

        cases = []

        missing_key = json.loads(json.dumps(healthy))
        del missing_key[
            f"{runtime.APP_NAMESPACE}/weltgewebe-runtime"
        ]["data"]["database-url"]
        cases.append(("missing-key", missing_key))

        empty_key = json.loads(json.dumps(healthy))
        empty_key[
            f"{runtime.DATA_NAMESPACE}/commonthing-experiment-b-database"
        ]["data"]["password"] = ""
        cases.append(("empty-key", empty_key))

        wrong_type = json.loads(json.dumps(healthy))
        wrong_type[
            f"{runtime.APP_NAMESPACE}/commonthing-experiment-b-registry"
        ]["type"] = "Opaque"
        cases.append(("wrong-type", wrong_type))

        deleting = json.loads(json.dumps(healthy))
        deleting[
            f"{runtime.DATA_NAMESPACE}/commonthing-experiment-b-database"
        ]["metadata"]["deletionTimestamp"] = "2026-09-27T03:54:28Z"
        cases.append(("deleting", deleting))

        wrong_identity = json.loads(json.dumps(healthy))
        wrong_identity[
            f"{runtime.APP_NAMESPACE}/weltgewebe-runtime"
        ]["metadata"]["name"] = "other"
        cases.append(("wrong-identity", wrong_identity))

        wrong_password = json.loads(json.dumps(healthy))
        wrong_password[
            f"{runtime.DATA_NAMESPACE}/commonthing-experiment-b-database"
        ]["data"]["password"] = base64.b64encode(b"wrong").decode("ascii")
        cases.append(("wrong-nonempty-password", wrong_password))

        wrong_runtime_url = json.loads(json.dumps(healthy))
        wrong_runtime_url[
            f"{runtime.APP_NAMESPACE}/weltgewebe-runtime"
        ]["data"]["database-url"] = base64.b64encode(
            b"postgresql://user:pass@postgres.commonthing-data.svc.cluster.local:5432/other"
        ).decode("ascii")
        cases.append(("wrong-nonempty-runtime-url", wrong_runtime_url))

        malformed_registry = json.loads(json.dumps(healthy))
        malformed_registry[
            f"{runtime.APP_NAMESPACE}/commonthing-experiment-b-registry"
        ]["data"][".dockerconfigjson"] = "***not-base64***"
        cases.append(("malformed-registry-base64", malformed_registry))

        wrong_registry = json.loads(json.dumps(healthy))
        wrong_registry[
            f"{runtime.APP_NAMESPACE}/commonthing-experiment-b-registry"
        ]["data"][".dockerconfigjson"] = base64.b64encode(
            b'{"auths":{"ghcr.io":{"auth":"different"}}}'
        ).decode("ascii")
        cases.append(("wrong-valid-registry-payload", wrong_registry))

        malformed_base64 = json.loads(json.dumps(healthy))
        malformed_base64[
            f"{runtime.APP_NAMESPACE}/weltgewebe-runtime"
        ]["data"]["database-url"] = "***not-base64***"
        cases.append(("malformed-base64", malformed_base64))

        for name, secrets in cases:
            with self.subTest(case=name):
                self.live_secrets = secrets
                for receipt in ("status.json", "portability.json"):
                    runtime.atomic_json(
                        self.root / "receipts" / receipt, {"status": "stale"}
                    )
                with self.assertRaises(runtime.RuntimeErrorEB):
                    runtime.status(self.root)
                self.assertFalse((self.root / "receipts/status.json").exists())
                self.assertFalse((self.root / "receipts/portability.json").exists())

        self.live_secrets = healthy
        result = runtime.status(self.root)
        self.assertTrue(result["secrets"]["registry"]["content_verified"])

        changed_db = {
            "username": "user2",
            "database": "db2",
            "password": "pass2",
        }
        runtime.atomic_json(self.root / "secrets/database.json", changed_db)
        changed_secrets = json.loads(json.dumps(healthy))
        changed_secrets[
            f"{runtime.DATA_NAMESPACE}/commonthing-experiment-b-database"
        ]["data"] = {
            key: base64.b64encode(value.encode("utf-8")).decode("ascii")
            for key, value in changed_db.items()
        }
        changed_url = (
            "postgresql://user2:pass2"
            f"@postgres.{runtime.DATA_NAMESPACE}.svc.cluster.local:5432/db2"
        )
        changed_secrets[
            f"{runtime.APP_NAMESPACE}/weltgewebe-runtime"
        ]["data"]["database-url"] = base64.b64encode(
            changed_url.encode("utf-8")
        ).decode("ascii")
        self.live_secrets = changed_secrets
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB,
            "database Secret source digest drifted",
        ):
            runtime.status(self.root)
        self.assertFalse((self.root / "receipts/status.json").exists())
        self.assertFalse((self.root / "receipts/portability.json").exists())

        runtime.atomic_json(
            self.root / "secrets/database.json",
            {"username": "user", "database": "db", "password": "pass"},
        )
        self.live_secrets = healthy

    def test_live_secret_readback_never_returns_secret_values(self) -> None:
        secret = {
            "metadata": {
                "namespace": runtime.APP_NAMESPACE,
                "name": "weltgewebe-runtime",
            },
            "type": "Opaque",
            "data": {"database-url": "c2Vuc2l0aXZlLXZhbHVl"},
        }
        observed = runtime._require_live_secret(
            secret,
            runtime.APP_NAMESPACE,
            "weltgewebe-runtime",
            "Opaque",
            {"database-url"},
            {"database-url": b"sensitive-value"},
        )
        self.assertIsNone(observed)
        readback = runtime._verified_secret_readback(
            runtime.APP_NAMESPACE,
            "weltgewebe-runtime",
            "Opaque",
            {"database-url"},
        )
        self.assertEqual(readback["required_keys"], ["database-url"])
        self.assertNotIn("data", readback)
        self.assertNotIn("c2Vuc2l0aXZlLXZhbHVl", json.dumps(readback))

    def test_status_requires_exact_healthy_pvc_set(self) -> None:
        self.write_vm_receipt()
        self.prepare_status()

        result = runtime.status(self.root)
        self.assertEqual(set(result["pvcs"]), runtime.EXPECTED_PVCS)
        for value in result["pvcs"].values():
            self.assertTrue(value["canonical"])
            self.assertRegex(value["spec_sha256"], r"^[0-9a-f]{64}$")

        healthy = json.loads(json.dumps(self.pvcs))
        wrong_capacity = json.loads(json.dumps(healthy))
        wrong_capacity[0]["spec"]["resources"]["requests"]["storage"] = "1Gi"
        wrong_access = json.loads(json.dumps(healthy))
        wrong_access[1]["spec"]["accessModes"] = ["ReadWriteMany"]
        wrong_volume_mode = json.loads(json.dumps(healthy))
        wrong_volume_mode[2]["spec"]["volumeMode"] = "Block"
        wrong_selector = json.loads(json.dumps(healthy))
        wrong_selector[0]["spec"]["selector"] = {
            "matchLabels": {"storage": "shadow"}
        }
        cases = [
            ("missing", healthy[:-1], "PVC set mismatch"),
            (
                "unexpected",
                healthy + [{
                    "metadata": {"namespace": runtime.APP_NAMESPACE, "name": "shadow-data"},
                    "spec": {"storageClassName": "local-path"},
                    "status": {"phase": "Bound"},
                }],
                "PVC set mismatch",
            ),
            (
                "deleting",
                [{
                    **healthy[0],
                    "metadata": {
                        **healthy[0]["metadata"],
                        "deletionTimestamp": "2026-09-26T18:40:31Z",
                    },
                }] + healthy[1:],
                "pending deletion",
            ),
            ("duplicate", healthy + [healthy[0]], "duplicate"),
            ("wrong-capacity", wrong_capacity, "PVC contract drifted"),
            ("wrong-access-mode", wrong_access, "PVC contract drifted"),
            ("wrong-volume-mode", wrong_volume_mode, "PVC contract drifted"),
            ("wrong-selector", wrong_selector, "PVC contract drifted"),
        ]

        for name, pvcs, message in cases:
            with self.subTest(case=name):
                self.pvcs = pvcs
                for receipt in ("status.json", "portability.json"):
                    runtime.atomic_json(
                        self.root / "receipts" / receipt, {"status": "stale"}
                    )
                with self.assertRaisesRegex(runtime.RuntimeErrorEB, message):
                    runtime.status(self.root)
                self.assertFalse((self.root / "receipts/status.json").exists())
                self.assertFalse((self.root / "receipts/portability.json").exists())

        self.pvcs = healthy

    def test_status_requires_current_expected_gateway(self) -> None:
        self.write_vm_receipt()
        self.prepare_status()

        healthy = json.loads(json.dumps(self.gateway))
        result = runtime.status(self.root)
        self.assertTrue(result["gateway"]["programmed"])
        self.assertEqual(result["gateway"]["generation"], 1)

        cases = []
        stale_generation = json.loads(json.dumps(healthy))
        stale_generation["metadata"]["generation"] = 2
        cases.append(("stale-generation", stale_generation))

        false_programmed = json.loads(json.dumps(healthy))
        false_programmed["status"]["conditions"][0]["status"] = "False"
        cases.append(("not-programmed", false_programmed))

        wrong_class = json.loads(json.dumps(healthy))
        wrong_class["spec"]["gatewayClassName"] = "other"
        cases.append(("wrong-class", wrong_class))

        wrong_listener = json.loads(json.dumps(healthy))
        wrong_listener["spec"]["listeners"][0]["port"] = 443
        cases.append(("wrong-listener", wrong_listener))

        deleting = json.loads(json.dumps(healthy))
        deleting["metadata"]["deletionTimestamp"] = (
            "2026-09-28T07:24:37Z"
        )
        cases.append(("deleting", deleting))

        for name, gateway in cases:
            with self.subTest(case=name):
                self.gateway = gateway
                for receipt in ("status.json", "portability.json"):
                    runtime.atomic_json(
                        self.root / "receipts" / receipt, {"status": "stale"}
                    )
                with self.assertRaises(runtime.RuntimeErrorEB):
                    runtime.status(self.root)
                self.assertFalse((self.root / "receipts/status.json").exists())
                self.assertFalse((self.root / "receipts/portability.json").exists())

        self.gateway = healthy

    def test_status_requires_live_httproute_accepted_and_resolved(self) -> None:
        self.write_vm_receipt()
        self.prepare_status()

        healthy = json.loads(json.dumps(self.httproute))
        result = runtime.status(self.root)
        self.assertTrue(result["httproute"]["accepted"])
        self.assertTrue(result["httproute"]["resolved_refs"])
        self.assertEqual(result["httproute"]["generation"], 1)

        cases = []

        accepted_false = json.loads(json.dumps(healthy))
        accepted_false["status"]["parents"][0]["conditions"][0]["status"] = "False"
        cases.append(("accepted", accepted_false))

        unresolved = json.loads(json.dumps(healthy))
        unresolved["status"]["parents"][0]["conditions"][1]["status"] = "False"
        cases.append(("resolved", unresolved))

        stale_generation = json.loads(json.dumps(healthy))
        stale_generation["metadata"]["generation"] = 2
        cases.append(("generation", stale_generation))

        deleting = json.loads(json.dumps(healthy))
        deleting["metadata"]["deletionTimestamp"] = (
            "2026-09-28T07:24:37Z"
        )
        cases.append(("deleting", deleting))

        wrong_parent = json.loads(json.dumps(healthy))
        wrong_parent["spec"]["parentRefs"][0]["sectionName"] = "other"
        cases.append(("parent", wrong_parent))

        wrong_controller = json.loads(json.dumps(healthy))
        wrong_controller["status"]["parents"][0]["controllerName"] = "example.invalid/controller"
        cases.append(("controller", wrong_controller))

        missing_api = json.loads(json.dumps(healthy))
        missing_api["spec"]["rules"][0]["matches"] = [
            {"path": {"type": "PathPrefix", "value": "/health"}}
        ]
        cases.append(("missing-api", missing_api))

        wrong_root_backend = json.loads(json.dumps(healthy))
        wrong_root_backend["spec"]["rules"][1]["backendRefs"][0]["name"] = "weltgewebe-api"
        cases.append(("wrong-root-backend", wrong_root_backend))

        wrong_api_port = json.loads(json.dumps(healthy))
        wrong_api_port["spec"]["rules"][0]["backendRefs"][0]["port"] = 8081
        cases.append(("wrong-api-port", wrong_api_port))

        zero_backend_weight = json.loads(json.dumps(healthy))
        zero_backend_weight["spec"]["rules"][0]["backendRefs"][0]["weight"] = 0
        cases.append(("zero-backend-weight", zero_backend_weight))

        extra_filter = json.loads(json.dumps(healthy))
        extra_filter["spec"]["rules"][0]["filters"] = [
            {"type": "RequestHeaderModifier"}
        ]
        cases.append(("extra-filter", extra_filter))

        future_generation = json.loads(json.dumps(healthy))
        future_generation["status"]["parents"][0]["conditions"][0]["observedGeneration"] = 2
        future_generation["status"]["parents"][0]["conditions"][1]["observedGeneration"] = 2
        cases.append(("future-generation", future_generation))

        for name, route in cases:
            with self.subTest(case=name):
                self.httproute = route
                for receipt in ("status.json", "portability.json"):
                    runtime.atomic_json(
                        self.root / "receipts" / receipt, {"status": "stale"}
                    )
                with self.assertRaises(runtime.RuntimeErrorEB):
                    runtime.status(self.root)
                self.assertFalse((self.root / "receipts/status.json").exists())
                self.assertFalse((self.root / "receipts/portability.json").exists())

        self.httproute = healthy

    def test_status_requires_fresh_gateway_data_plane_readback(self) -> None:
        self.write_vm_receipt()
        self.prepare_status()
        self.gateway_data_plane.side_effect = runtime.RuntimeErrorEB(
            "gateway data plane failed"
        )
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "gateway data plane failed"):
            runtime.status(self.root)
        self.assertFalse((self.root / "receipts/status.json").exists())
        self.assertFalse((self.root / "receipts/portability.json").exists())



    def test_status_guards_gateway_probe_with_serving_endpoint_watch(self) -> None:
        self.write_vm_receipt()
        self.prepare_status()
        events: list[str] = []
        gateway_result = self.gateway_data_plane.return_value

        class TrackingGuard:
            def __enter__(self) -> None:
                events.append("enter")

            def __exit__(self, *_args: object) -> bool:
                events.append("exit")
                return False

        self.functional_endpoint_guard.side_effect = (
            lambda *_args, **_kwargs: TrackingGuard()
        )

        def gateway_probe(*_args: object, **_kwargs: object) -> dict:
            events.append("probe")
            return gateway_result

        self.gateway_data_plane.side_effect = gateway_probe
        result = runtime.status(self.root)

        self.assertEqual(events, ["enter", "probe", "exit"])
        self.assertEqual(self.functional_serving_runtime.call_count, 3)
        self.functional_endpoint_guard.assert_called_once_with(
            self.root,
            self.status_serving_runtime,
        )
        self.assertEqual(
            result["gateway_serving_runtime"],
            runtime._functional_serving_runtime_semantic_binding(
                self.status_serving_runtime
            ),
        )

    def test_status_rejects_serving_runtime_change_during_gateway_probe(
        self,
    ) -> None:
        self.write_vm_receipt()
        self.prepare_status()
        before = json.loads(json.dumps(self.status_serving_runtime))
        changed = json.loads(json.dumps(before))
        changed["gateway_base_url"] = "http://192.168.122.99"
        self.functional_serving_runtime.side_effect = [
            before,
            json.loads(json.dumps(before)),
            changed,
        ]

        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB,
            "application serving runtime changed during status readback",
        ):
            runtime.status(self.root)

        self.gateway_data_plane.assert_called_once_with(
            self.root,
            self.commit,
        )
        self.assertFalse((self.root / "receipts/status.json").exists())
        self.assertFalse((self.root / "receipts/portability.json").exists())

    def test_final_recovery_state_readback_binds_current_signatures_and_fixture(
        self,
    ) -> None:
        restored_database = {
            "tables": [
                {
                    "schema": "public",
                    "name": "domain_nodes",
                    "rows": 2,
                    "md5": "a" * 32,
                }
            ],
            "sequences": [
                {
                    "schema": "public",
                    "name": "domain_nodes_id_seq",
                    "last_value": "10",
                    "is_called": True,
                    "start_value": "1",
                    "increment_by": "1",
                    "min_value": "1",
                    "max_value": "9223372036854775807",
                    "cache_size": "1",
                    "cycle": False,
                }
            ],
            "schema_sha256": "b" * 64,
        }
        post_resume_database = json.loads(json.dumps(restored_database))
        post_resume_database["sequences"][0]["last_value"] = "12"
        current_database = json.loads(json.dumps(post_resume_database))
        current_database["sequences"][0]["last_value"] = "14"
        recovery = {
            "schema_version": 1,
            "status": "pass",
            "source_commit": self.commit,
            "rpo_seconds": 0,
            "database_before": restored_database,
            "database_after": restored_database,
            "database_post_resume": post_resume_database,
            "jetstream_before": {"nats": "stable"},
            "jetstream_after": {"nats": "stable"},
            "jetstream_post_resume": {"nats": "stable"},
        }
        runtime.atomic_json(
            self.root / "receipts/recovery.json",
            recovery,
        )
        runtime.atomic_json(
            self.root / "receipts/t048-fixture.json",
            {
                "schema_version": 1,
                "status": "loaded",
                "source_commit": self.commit,
                "live_binding": {"generation_id": "fixture"},
            },
        )
        postgres_binding = {"binding": "stable"}
        nats_binding = {"binding": "nats-stable"}
        with (
            mock.patch.object(
                runtime,
                "_verified_database_client_identity",
                return_value=("user", "db"),
            ),
            mock.patch.object(
                runtime,
                "_require_postgres_runtime_binding",
                return_value=postgres_binding,
            ),
            mock.patch.object(
                runtime,
                "_require_nats_runtime_binding",
                return_value=nats_binding,
            ),
            mock.patch.object(
                runtime,
                "_database_signature",
                return_value=current_database,
            ) as database_signature,
            mock.patch.object(
                runtime,
                "_jetstream_signature",
                return_value={"nats": "stable"},
            ) as jetstream_signature,
            mock.patch.object(
                runtime,
                "_validated_t048_fixture_receipt",
                return_value={
                    "status": "loaded",
                    "source_commit": self.commit,
                    "live_binding": {"generation_id": "fixture"},
                },
            ) as fixture,
        ):
            result = runtime._final_recovery_state_readback(
                self.root,
                self.commit,
            )
        self.assertEqual(
            result["database_signature"],
            current_database,
        )
        self.assertEqual(
            result["jetstream_signature"],
            {"nats": "stable"},
        )
        database_signature.assert_called_once_with(
            self.root,
            database_identity=("user", "db"),
            source_commit=self.commit,
            postgres_binding=postgres_binding,
        )
        jetstream_signature.assert_called_once_with(
            self.root,
            source_commit=self.commit,
            nats_binding=nats_binding,
        )
        fixture.assert_called_once_with(self.root, self.commit)

        runtime.atomic_json(
            self.root / "receipts/recovery-failed.json",
            {
                "schema_version": 1,
                "status": "failed",
                "source_commit": self.commit,
            },
        )
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB,
            "latest failed recovery",
        ):
            runtime._final_recovery_state_readback(
                self.root,
                self.commit,
            )
        (self.root / "receipts/recovery-failed.json").unlink()

        with (
            mock.patch.object(
                runtime,
                "_verified_database_client_identity",
                return_value=("user", "db"),
            ),
            mock.patch.object(
                runtime,
                "_require_postgres_runtime_binding",
                return_value=postgres_binding,
            ),
            mock.patch.object(
                runtime,
                "_require_nats_runtime_binding",
                return_value=nats_binding,
            ),
            mock.patch.object(
                runtime,
                "_database_signature",
                return_value={
                    **current_database,
                    "tables": [
                        {
                            **current_database["tables"][0],
                            "md5": "d" * 32,
                        }
                    ],
                },
            ),
            mock.patch.object(
                runtime,
                "_jetstream_signature",
                return_value={"nats": "stable"},
            ),
            self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "database/search state drifted",
            ),
        ):
            runtime._final_recovery_state_readback(
                self.root,
                self.commit,
            )

    def test_status_rechecks_semantic_provider_live(self) -> None:
        self.write_vm_receipt()
        self.prepare_status()
        self.semantic_provider.side_effect = runtime.RuntimeErrorEB(
            "Ollama model digest differs from the pinned revision"
        )
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "model digest"):
            runtime.status(self.root)
        self.assertFalse((self.root / "receipts/status.json").exists())
        self.assertFalse((self.root / "receipts/portability.json").exists())

    def test_status_revalidates_recovered_state(self) -> None:
        self.write_vm_receipt()
        self.prepare_status()
        self.recovery_state.side_effect = runtime.RuntimeErrorEB(
            "Experiment-B database/search state drifted after recovery"
        )
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "drifted after recovery"):
            runtime.status(self.root)
        self.assertFalse((self.root / "receipts/status.json").exists())
        self.assertFalse((self.root / "receipts/portability.json").exists())

    def test_status_accepts_node_typemeta_with_exact_configured_k3s_version(self) -> None:
        self.write_vm_receipt()
        self.prepare_status()
        for version in (self.config["kubernetes"]["version"], "v1.36.2+k3s1"):
            with self.subTest(version=version):
                self.config["kubernetes"]["version"] = version
                self.node["status"]["nodeInfo"]["kubeletVersion"] = version
                result = runtime.status(self.root)
                self.assertEqual(result["status"], "observed")
                self.assertEqual(result["kubelet_version"], version)
                self.assertIs(result["kind_runtime"], False)

    def test_status_accepts_supported_node_inventory_list_kinds(self) -> None:
        self.write_vm_receipt()
        self.prepare_status()
        for kind in ("NodeList", "List"):
            with self.subTest(kind=kind):
                self.node_inventory = {
                    "apiVersion": "v1",
                    "kind": kind,
                    "items": [self.node],
                }
                result = runtime.status(self.root)
                self.assertEqual(result["status"], "observed")
                self.assertEqual(result["node"], runtime.VM_NAME)
                self.assertEqual(
                    result["kubelet_version"],
                    self.config["kubernetes"]["version"],
                )

    def test_status_requires_exactly_one_node_inventory_item(self) -> None:
        self.write_vm_receipt()
        self.prepare_status()
        valid_node = self.node
        cases = (
            valid_node,
            {"apiVersion": "v1", "kind": "ConfigMapList", "items": [valid_node]},
            {"apiVersion": "v1", "kind": "NodeList", "items": {}},
            {"apiVersion": "v1", "kind": "NodeList", "items": []},
            {"apiVersion": "v1", "kind": "List", "items": [valid_node, valid_node]},
            {"apiVersion": "v1", "kind": "List", "items": [{**valid_node, "kind": "Pod"}]},
        )
        for inventory in cases:
            items = inventory.get("items")
            with self.subTest(
                inventory_kind=inventory.get("kind"),
                item_count=len(items) if isinstance(items, list) else None,
            ):
                self.node_inventory = inventory
                for name in ("status.json", "portability.json"):
                    runtime.atomic_json(self.root / "receipts" / name, {"status": "stale"})
                with self.assertRaises(runtime.RuntimeErrorEB):
                    runtime.status(self.root)
                for name in ("status.json", "portability.json"):
                    self.assertFalse((self.root / "receipts" / name).exists())

    def test_status_rejects_wrong_or_missing_kubelet_version(self) -> None:
        self.write_vm_receipt()
        self.prepare_status()
        for info in (
            {},
            *({"kubeletVersion": version} for version in (
                None, "", "v1.36.1", "v1.36.1+kind", "v1.36.0+k3s1",
                "v1.36.1+k3s2", self.config["kubernetes"]["version"] + "-unexpected",
            )),
        ):
            with self.subTest(node_info=info):
                self.node["status"]["nodeInfo"] = info
                for name in ("status.json", "portability.json"):
                    runtime.atomic_json(self.root / "receipts" / name, {"status": "stale"})
                with self.assertRaisesRegex(runtime.RuntimeErrorEB, "k3s"):
                    runtime.status(self.root)
                for name in ("status.json", "portability.json"):
                    self.assertFalse((self.root / "receipts" / name).exists())
                attempt = json.loads((self.root / "receipts/status-attempt.json").read_text())
                self.assertEqual(attempt["status"], "running")

    def test_status_rejects_live_identity_capacity_network_and_base_drift(self) -> None:
        self.write_vm_receipt()
        self.prepare_status()
        original_xml, original_qmp = self.xml.copy(), json.dumps(self.qmp)
        for change in ("uuid", "mac", "disk_inode", "vcpu", "memory", "network", "capacity", "backing", "base"):
            with self.subTest(drift=change):
                self.xml, self.qmp = original_xml.copy(), json.loads(original_qmp)
                self.base_bytes = b"pinned Ubuntu image fixture"
                self.write_vm_receipt()
                if change in {"uuid", "mac", "vcpu", "memory"}:
                    before, after = {
                        "uuid": ("11111111-1111-4111-8111-111111111111", "44444444-4444-4444-8444-444444444444"),
                        "mac": ("52:54:00:12:34:56", "52:54:00:ab:cd:ef"),
                        "vcpu": ("current='6'>6", "current='4'>4"),
                        "memory": ("12582912", "8388608"),
                    }[change]
                    for key in ("live", "inactive"):
                        self.xml[key] = self.xml[key].replace(before, after)
                elif change == "disk_inode":
                    replacement = self.pool / "replacement"
                    replacement.write_bytes(b"different guest disk at same path")
                    replacement.replace(self.disk)
                elif change == "network":
                    self.xml["network"] = self.xml["network"].replace("mode='nat'", "mode='route'")
                elif change == "capacity":
                    self.qmp["return"][0]["inserted"]["image"]["virtual-size"] = 30 * 1024**3
                elif change == "backing":
                    self.qmp["return"][0]["inserted"]["image"]["backing-image"]["filename"] = "/old/base"
                else:
                    self.base_bytes = b"unapproved Ubuntu image"
                with self.assertRaisesRegex(runtime.RuntimeErrorEB, "VM substrate"):
                    runtime.status(self.root)
                self.assertFalse((self.root / "receipts/status.json").exists())
                self.assertFalse((self.root / "receipts/portability.json").exists())
                self.tools.assert_not_called()

    def test_create_invalidates_before_revision_or_config_failure(self) -> None:
        self.domain_present = False
        self.pool_present = False
        for failed_check in ("_current_protected_main_commit", "load_config"):
            with self.subTest(failed_check=failed_check):
                self.runner.reset_mock()
                for name in runtime.VM_ATTEMPT_INVALIDATES:
                    runtime.atomic_json(self.root / "receipts" / name, {"status": "stale"})
                with mock.patch.object(runtime, failed_check, side_effect=runtime.RuntimeErrorEB("binding failed")):
                    with self.assertRaisesRegex(runtime.RuntimeErrorEB, "binding failed"):
                        runtime.create_vm(self.root)
                for name in runtime.VM_ATTEMPT_INVALIDATES:
                    self.assertFalse((self.root / "receipts" / name).exists(), name)
                self.assertEqual(
                    [call.args[0][3] for call in self.runner.call_args_list],
                    ["dominfo", "pool-info"],
                )

    def test_create_refuses_retained_vm_or_pool_without_cleanup(self) -> None:
        ownership_path = self.root / "receipts/vm-create-attempt.json"
        retirement_path = runtime.RETIREMENT_RECEIPT
        for present_domain in (True, False):
            with self.subTest(present_domain=present_domain):
                self.domain_present = present_domain
                self.pool_present = True
                runtime.atomic_json(
                    ownership_path,
                    {
                        "schema_version": 1,
                        "status": "running",
                        "source_commit": self.commit,
                        "config_sha256": runtime.sha256_file(runtime.CLUSTER / "config.json"),
                        "state_root": str(self.root.resolve()),
                        "vm": runtime.VM_NAME,
                        "pool": runtime.POOL_NAME,
                    },
                )
                runtime.atomic_json(
                    retirement_path,
                    {"schema_version": 1, "status": "retired"},
                )
                ownership_sha = runtime.sha256_file(ownership_path)
                retirement_sha = runtime.sha256_file(retirement_path)
                self.runner.reset_mock()
                with self.assertRaisesRegex(runtime.RuntimeErrorEB, "already exists"):
                    runtime.create_vm(self.root)
                self.assertEqual(runtime.sha256_file(ownership_path), ownership_sha)
                self.assertEqual(runtime.sha256_file(retirement_path), retirement_sha)
                self.assertTrue(
                    all(
                        call.args[0][3] in {"dominfo", "pool-info"}
                        for call in self.runner.call_args_list
                    )
                )

    def test_kubernetes_target_binding_rejects_kubeconfig_drift_before_mutation(self) -> None:
        kubeconfig = self.root / "kubeconfig.yaml"
        kubeconfig.write_text(
            yaml.safe_dump(
                {
                    "current-context": "experiment-b",
                    "contexts": [
                        {
                            "name": "experiment-b",
                            "context": {"cluster": "experiment-b"},
                        }
                    ],
                    "clusters": [
                        {
                            "name": "experiment-b",
                            "cluster": {
                                "server": "https://192.168.122.10:6443"
                            },
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        kubeconfig.chmod(0o600)
        runtime.atomic_json(
            self.root / "receipts/k3s.json",
            {
                "schema_version": 1,
                "status": "ready",
                "source_commit": self.commit,
                "vm_ip": "192.168.122.10",
                "kubeconfig_sha256": runtime.sha256_file(kubeconfig),
            },
        )
        with mock.patch.object(
            runtime, "vm_ip", return_value="192.168.122.10"
        ):
            receipt, ip, server = runtime._require_kubernetes_target_binding(
                self.root, self.commit
            )
            self.assertEqual(receipt["source_commit"], self.commit)
            self.assertEqual(ip, "192.168.122.10")
            self.assertEqual(server, "https://192.168.122.10:6443")

            kubeconfig.write_text(
                kubeconfig.read_text(encoding="utf-8") + "# drift\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                runtime.RuntimeErrorEB, "kubeconfig digest/mode drifted"
            ):
                runtime._require_kubernetes_target_binding(
                    self.root, self.commit
                )

        for function in (
            runtime.install_platform,
            runtime.inject_secrets,
            runtime.apply_release,
            runtime.semantic_activate,
            runtime.seed_t048_fixture,
        ):
            self.assertIn(
                "_require_kubernetes_target_binding",
                inspect.getsource(function),
            )
        self.assertIn(
            "_kubernetes_target_identity",
            inspect.getsource(runtime.recovery_proof),
        )
        source = inspect.getsource(runtime.inject_secrets)
        self.assertLess(
            source.index("_require_kubernetes_target_binding"),
            source.index("kubectl_apply"),
        )

    def test_t048_binds_running_api_pod_to_release_image_identity(self) -> None:
        api_digest = "sha256:" + "b" * 64
        runtime.atomic_json(
            self.root / "receipts/release.json",
            {
                "schema_version": 1,
                "status": "applied",
                "source_commit": self.commit,
                "api_digest": api_digest,
                "web_digest": "sha256:" + "c" * 64,
            },
        )
        config = runtime.load_config()
        self.patch(
            "_source_commit_config",
            return_value=json.loads(json.dumps(config)),
        )
        images = {
            "api": f"ghcr.io/heimgewebe/commonthing-api@{api_digest}",
            "search-worker": (
                f"ghcr.io/heimgewebe/commonthing-api@{api_digest}"
            ),
            "ollama": config["semantic_search"]["ollama_image"],
        }

        def image_id(image: str) -> str:
            digest = image.rsplit("@", 1)[1]
            return "containerd://" + digest

        pod = {
            "metadata": {
                "name": "weltgewebe-api-0",
                "namespace": runtime.APP_NAMESPACE,
                "labels": {
                    "app.kubernetes.io/name": "weltgewebe-api"
                },
            },
            "spec": {
                "containers": [
                    {"name": name, "image": image}
                    for name, image in images.items()
                ]
            },
            "status": {
                "phase": "Running",
                "conditions": [
                    {"type": "Ready", "status": "True"}
                ],
                "containerStatuses": [
                    {
                        "name": name,
                        "ready": True,
                        "state": {"running": {}},
                        "imageID": image_id(image),
                    }
                    for name, image in images.items()
                ],
            },
        }
        with mock.patch.object(
            runtime,
            "_api_pod",
            return_value=("weltgewebe-api-0", pod),
        ):
            name, _pod, proof = runtime._require_t048_api_release_binding(
                self.root, self.commit
            )
        self.assertEqual(name, "weltgewebe-api-0")
        self.assertRegex(
            proof["runtime_image_ids_sha256"], r"^[0-9a-f]{64}$"
        )

        requested_drift = json.loads(json.dumps(pod))
        requested_drift["spec"]["containers"][0]["image"] = (
            "ghcr.io/heimgewebe/commonthing-api@sha256:" + "0" * 64
        )
        with (
            mock.patch.object(
                runtime,
                "_api_pod",
                return_value=("weltgewebe-api-0", requested_drift),
            ),
            self.assertRaisesRegex(
                runtime.RuntimeErrorEB, "requested images drifted"
            ),
        ):
            runtime._require_t048_api_release_binding(
                self.root, self.commit
            )

        runtime_drift = json.loads(json.dumps(pod))
        runtime_drift["status"]["containerStatuses"][0]["imageID"] = (
            "containerd://sha256:" + "0" * 64
        )
        with (
            mock.patch.object(
                runtime,
                "_api_pod",
                return_value=("weltgewebe-api-0", runtime_drift),
            ),
            self.assertRaisesRegex(
                runtime.RuntimeErrorEB, "runtime image ID drifted"
            ),
        ):
            runtime._require_t048_api_release_binding(
                self.root, self.commit
            )
        source = inspect.getsource(runtime.t048_load_proof)
        self.assertGreaterEqual(
            source.count("_require_t048_api_runtime_binding"), 2
        )

    def test_recovery_refuses_retry_after_failed_attempt(self) -> None:
        runtime.atomic_json(
            self.root / "receipts/release.json",
            {
                "schema_version": 1,
                "status": "applied",
                "source_commit": self.commit,
            },
        )
        failed = {
            "schema_version": 1,
            "status": "fail",
            "source_commit": self.commit,
            "failure": "restore interrupted",
        }
        failed_path = self.root / "receipts/recovery-failed.json"
        runtime.atomic_json(failed_path, failed)
        failed_sha = runtime.sha256_file(failed_path)
        with (
            mock.patch.object(runtime, "_flux_suspend") as suspend,
            self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "refuses a retry after a failed attempt",
            ),
        ):
            runtime.recovery_proof(self.root)
        suspend.assert_not_called()
        self.assertEqual(runtime.sha256_file(failed_path), failed_sha)

    def test_recovery_requires_new_empty_persistent_volume_identity(self) -> None:
        source = inspect.getsource(runtime._require_empty_replacement_pvc)
        self.assertIn(
            "_git_blob_bytes(source_commit, manifest_path)",
            source,
        )
        self.assertNotIn("_versioned_data_deployment_contract(", source)
        self.assertNotIn(".read_text(", source)

        old_identity = {
            "pvc_uid": "old-pvc",
            "pv_name": "old-pv",
            "pv_uid": "old-pv-uid",
        }
        new_identity = {
            "pvc_uid": "new-pvc",
            "pv_name": "new-pv",
            "pv_uid": "new-pv-uid",
        }

        def source_blob(source_commit, path):
            self.assertEqual(source_commit, self.commit)
            return path.read_bytes()

        def kubectl_result(_root, arguments, **_kwargs):
            return runtime.subprocess.CompletedProcess(
                arguments, 0, stdout="", stderr=""
            )

        probe_container_id = "containerd://" + "a" * 64

        with (
            mock.patch.object(
                runtime, "_git_blob_bytes", side_effect=source_blob
            ),
            mock.patch.object(runtime, "kubectl_apply") as apply,
            mock.patch.object(
                runtime,
                "_pvc_volume_identity",
                return_value=new_identity,
            ),
            mock.patch.object(
                runtime, "_kubectl", side_effect=kubectl_result
            ),
            mock.patch.object(
                runtime,
                "_require_running_probe_container",
                return_value=probe_container_id,
            ) as bind_probe,
            mock.patch.object(
                runtime,
                "_run_bound_container_command",
                return_value=b"",
            ) as bound_exec,
            mock.patch.object(runtime, "_delete_pod") as delete,
        ):
            result = runtime._require_empty_replacement_pvc(
                self.root,
                "postgres-data",
                old_identity,
                self.commit,
            )
        self.assertEqual(result["old"], old_identity)
        self.assertEqual(result["new"], new_identity)
        self.assertTrue(result["empty_before_restore"])
        apply.assert_called_once()
        applied_manifest = json.loads(apply.call_args.args[1])
        self.assertEqual(
            applied_manifest["spec"]["containers"][0]["resources"],
            {},
        )
        bind_probe.assert_called_once()
        bound_exec.assert_called_once()
        self.assertEqual(bound_exec.call_args.args[2], probe_container_id)
        delete.assert_called_once()

        with (
            mock.patch.object(
                runtime, "_git_blob_bytes", side_effect=source_blob
            ),
            mock.patch.object(runtime, "kubectl_apply"),
            mock.patch.object(
                runtime,
                "_pvc_volume_identity",
                return_value=old_identity,
            ),
            mock.patch.object(
                runtime, "_kubectl", side_effect=kubectl_result
            ),
            mock.patch.object(
                runtime,
                "_require_running_probe_container",
                return_value=probe_container_id,
            ),
            mock.patch.object(
                runtime,
                "_run_bound_container_command",
                return_value=b"",
            ),
            mock.patch.object(runtime, "_delete_pod"),
            self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "reused the previous storage identity",
            ),
        ):
            runtime._require_empty_replacement_pvc(
                self.root,
                "postgres-data",
                old_identity,
                self.commit,
            )

        with (
            mock.patch.object(
                runtime, "_git_blob_bytes", side_effect=source_blob
            ),
            mock.patch.object(runtime, "kubectl_apply"),
            mock.patch.object(
                runtime,
                "_pvc_volume_identity",
                return_value=new_identity,
            ),
            mock.patch.object(
                runtime, "_kubectl", side_effect=kubectl_result
            ),
            mock.patch.object(
                runtime,
                "_require_running_probe_container",
                return_value=probe_container_id,
            ),
            mock.patch.object(
                runtime,
                "_run_bound_container_command",
                return_value=b"lost+found\n",
            ),
            mock.patch.object(runtime, "_delete_pod"),
            self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "not empty before restore",
            ),
        ):
            runtime._require_empty_replacement_pvc(
                self.root,
                "postgres-data",
                old_identity,
                self.commit,
            )

    def test_replacement_pvc_probe_binds_live_container_identity(self) -> None:
        image = "example.invalid/probe@sha256:" + "b" * 64
        expected_spec = {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "containers": [
                {
                    "name": "probe",
                    "image": image,
                    "imagePullPolicy": "IfNotPresent",
                    "command": ["/bin/sh", "-c", "sleep 3600"],
                }
            ],
        }
        container_id = "containerd://" + "c" * 64
        pod = {
            "metadata": {
                "name": "probe-pod",
                "namespace": runtime.DATA_NAMESPACE,
            },
            "spec": json.loads(json.dumps(expected_spec)),
            "status": {
                "phase": "Running",
                "conditions": [{"type": "Ready", "status": "True"}],
                "containerStatuses": [
                    {
                        "name": "probe",
                        "ready": True,
                        "state": {"running": {}},
                        "imageID": (
                            "docker-pullable://example.invalid/probe@sha256:"
                            + "b" * 64
                        ),
                        "containerID": container_id,
                    }
                ],
            },
        }
        with mock.patch.object(
            runtime,
            "_kubectl_json",
            return_value=pod,
        ):
            self.assertEqual(
                runtime._require_running_probe_container(
                    self.root,
                    "probe-pod",
                    expected_spec,
                    image,
                    "replacement PVC probe",
                ),
                container_id,
            )

        drifted = json.loads(json.dumps(pod))
        drifted["status"]["containerStatuses"][0]["containerID"] = (
            "containerd://not-a-valid-id"
        )
        with (
            mock.patch.object(
                runtime,
                "_kubectl_json",
                return_value=drifted,
            ),
            self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "container identity drifted",
            ),
        ):
            runtime._require_running_probe_container(
                self.root,
                "probe-pod",
                expected_spec,
                image,
                "replacement PVC probe",
            )

        source = inspect.getsource(runtime._require_empty_replacement_pvc)
        self.assertIn('"imagePullPolicy": "IfNotPresent"', source)
        self.assertIn("_require_running_probe_container(", source)
        self.assertIn("_run_bound_container_command(", source)
        self.assertNotIn('"exec",\n                pod_name', source)

    def test_teardown_rejects_live_uuid_drift_before_destroy(self) -> None:
        self.write_vm_receipt()
        old_uuid = vm_substrate_fixture()["uuid"]
        new_uuid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        self.xml["live"] = self.xml["live"].replace(old_uuid, new_uuid)
        self.xml["inactive"] = self.xml["inactive"].replace(
            old_uuid, new_uuid
        )
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB,
            "live VM/pool/disk identity drifted",
        ):
            runtime.teardown(self.root)
        self.assertTrue(self.domain_present)
        self.assertTrue(self.pool_present)
        destructive = {
            call.args[0][3]
            for call in self.runner.call_args_list
            if call.args
            and len(call.args[0]) > 3
            and call.args[0][0] == "virsh"
        }
        self.assertNotIn("destroy", destructive)
        self.assertNotIn("vol-delete", destructive)

    def test_status_revalidates_data_service_selector_and_ports(self) -> None:
        self.write_vm_receipt()
        self.prepare_status()
        result = runtime._require_live_data_services(self.root)
        self.assertEqual(set(result), {"postgres", "nats"})
        self.assertTrue(result["postgres"]["canonical"])

        healthy = json.loads(json.dumps(self.data_services))
        self.data_services["postgres"]["spec"]["selector"] = {
            "app.kubernetes.io/name": "other"
        }
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB, "Service spec drifted"
        ):
            runtime._require_live_data_services(self.root)

        self.data_services = json.loads(json.dumps(healthy))
        self.data_services["nats"]["spec"]["ports"][0]["port"] += 1
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB, "Service spec drifted"
        ):
            runtime._require_live_data_services(self.root)

    def test_application_workload_contract_rejects_security_and_env_drift(self) -> None:
        def workload(name: str, image: str) -> tuple[dict, dict, dict]:
            labels = {"app.kubernetes.io/name": name}
            annotations = {"commonthing.test/contract": "v1"}
            pod_spec = {
                "serviceAccountName": name,
                "automountServiceAccountToken": False,
                "terminationGracePeriodSeconds": 20,
                "securityContext": {
                    "runAsNonRoot": True,
                    "seccompProfile": {"type": "RuntimeDefault"},
                },
                "imagePullSecrets": [{"name": "registry"}],
                "volumes": [{"name": "tmp", "emptyDir": {}}],
                "containers": [
                    {
                        "name": "app",
                        "image": image,
                        "imagePullPolicy": "IfNotPresent",
                        "command": ["/app"],
                        "args": ["serve"],
                        "envFrom": [
                            {"configMapRef": {"name": "runtime"}}
                        ],
                        "env": [{"name": "MODE", "value": "test"}],
                        "ports": [
                            {
                                "name": "http",
                                "containerPort": 8080,
                                "protocol": "TCP",
                            }
                        ],
                        "resources": {
                            "requests": {"cpu": "10m"},
                            "limits": {"cpu": "100m"},
                        },
                        "securityContext": {
                            "allowPrivilegeEscalation": False,
                            "readOnlyRootFilesystem": True,
                        },
                        "volumeMounts": [
                            {"name": "tmp", "mountPath": "/tmp"}
                        ],
                    }
                ],
            }
            deployment = {
                "metadata": {
                    "name": name,
                    "namespace": runtime.APP_NAMESPACE,
                },
                "spec": {
                    "replicas": 1,
                    "revisionHistoryLimit": 3,
                    "strategy": {
                        "type": "RollingUpdate",
                        "rollingUpdate": {
                            "maxUnavailable": 0,
                            "maxSurge": 1,
                        },
                    },
                    "selector": {"matchLabels": labels},
                    "template": {
                        "metadata": {
                            "labels": labels,
                            "annotations": annotations,
                        },
                        "spec": json.loads(json.dumps(pod_spec)),
                    },
                },
            }
            pod = {
                "metadata": {
                    "name": f"{name}-0",
                    "namespace": runtime.APP_NAMESPACE,
                    "labels": {
                        **labels,
                        "pod-template-hash": "generated",
                    },
                    "annotations": annotations,
                },
                "spec": json.loads(json.dumps(pod_spec)),
            }
            contract = {
                "replicas": 1,
                "revisionHistoryLimit": 3,
                "strategy": deployment["spec"]["strategy"],
                "paused": False,
                "minReadySeconds": 0,
                "progressDeadlineSeconds": 600,
                "selector_labels": labels,
                "template_labels": labels,
                "template_annotations": annotations,
                "pod_spec": runtime._application_pod_spec_projection(
                    pod_spec, name
                ),
            }
            expected = {
                "contract": contract,
                "contract_sha256": runtime._stable_json_sha256(
                    contract
                ),
                "pod_contract_sha256": runtime._stable_json_sha256(
                    contract["pod_spec"]
                ),
            }
            return deployment, pod, expected

        api = workload(
            "weltgewebe-api",
            "ghcr.io/heimgewebe/commonthing-api@sha256:" + "b" * 64,
        )
        web = workload(
            "weltgewebe-web",
            "ghcr.io/heimgewebe/commonthing-web@sha256:" + "c" * 64,
        )
        expected = {
            "weltgewebe-api": api[2],
            "weltgewebe-web": web[2],
        }
        deployments = {
            "weltgewebe-api": api[0],
            "weltgewebe-web": web[0],
        }
        pods = {
            "weltgewebe-api": [api[1]],
            "weltgewebe-web": [web[1]],
        }
        release = {
            "api_digest": "sha256:" + "b" * 64,
            "web_digest": "sha256:" + "c" * 64,
        }
        with mock.patch.object(
            runtime,
            "_rendered_application_workload_contract",
            return_value=expected,
        ):
            proof = runtime._require_live_application_workloads(
                self.root, release, deployments, pods
            )
            self.assertTrue(proof["weltgewebe-api"]["canonical"])

            deployment_drift = json.loads(json.dumps(deployments))
            deployment_drift["weltgewebe-api"]["spec"]["paused"] = True
            with self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "Deployment contract drifted",
            ):
                runtime._require_live_application_workloads(
                    self.root, release, deployment_drift, pods
                )

            deployment_drift = json.loads(json.dumps(deployments))
            deployment_drift["weltgewebe-api"]["spec"]["template"][
                "spec"
            ]["serviceAccountName"] = "default"
            with self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "Deployment contract drifted",
            ):
                runtime._require_live_application_workloads(
                    self.root, release, deployment_drift, pods
                )

            pod_drift = json.loads(json.dumps(pods))
            pod_drift["weltgewebe-api"][0]["spec"]["containers"][0][
                "envFrom"
            ] = []
            with self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "Pod contract drifted",
            ):
                runtime._require_live_application_workloads(
                    self.root, release, deployments, pod_drift
                )

            pod_deadline_drift = json.loads(json.dumps(pods))
            pod_deadline_drift["weltgewebe-api"][0]["spec"][
                "activeDeadlineSeconds"
            ] = 120
            with self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "Pod contract drifted",
            ):
                runtime._require_live_application_workloads(
                    self.root, release, deployments, pod_deadline_drift
                )

        status_source = inspect.getsource(runtime.status)
        self.assertIn(
            "_require_live_application_workloads", status_source
        )
        portability_source = inspect.getsource(
            runtime.portability_report
        )
        self.assertIn(
            "_rendered_application_workload_contract",
            portability_source,
        )

    def test_application_workload_projection_normalizes_kubernetes_probe_defaults(self) -> None:
        expected = {
            "name": "api",
            "image": "example.invalid/api@sha256:" + "b" * 64,
            "imagePullPolicy": "IfNotPresent",
            "ports": [{"name": "http", "containerPort": 8080}],
            "readinessProbe": {
                "httpGet": {"path": "/health", "port": 8080},
                "periodSeconds": 5,
                "failureThreshold": 3,
                "timeoutSeconds": 2,
            },
        }
        live = json.loads(json.dumps(expected))
        live["readinessProbe"].update(
            {
                "initialDelaySeconds": 0,
                "successThreshold": 1,
            }
        )
        live["readinessProbe"]["httpGet"]["scheme"] = "HTTP"
        live["ports"][0]["protocol"] = "TCP"
        self.assertEqual(
            runtime._container_runtime_contract(expected, "expected"),
            runtime._container_runtime_contract(live, "live"),
        )
        self.assertIsNone(
            runtime._container_runtime_contract(expected, "expected")[
                "restartPolicy"
            ]
        )
        native_sidecar = {
            "name": "bootstrap",
            "image": "example.invalid/init@sha256:" + "d" * 64,
            "restartPolicy": "Always",
        }
        self.assertEqual(
            runtime._container_runtime_contract(
                native_sidecar, "native sidecar"
            )["restartPolicy"],
            "Always",
        )
        native_sidecar_without_policy = json.loads(
            json.dumps(native_sidecar)
        )
        native_sidecar_without_policy.pop("restartPolicy")
        self.assertNotEqual(
            runtime._container_runtime_contract(
                native_sidecar, "native sidecar"
            ),
            runtime._container_runtime_contract(
                native_sidecar_without_policy,
                "plain init container",
            ),
        )

        expected_pod = {
            "automountServiceAccountToken": False,
            "containers": [expected],
        }
        live_pod = json.loads(json.dumps(expected_pod))
        live_pod["serviceAccountName"] = "default"
        live_pod["terminationGracePeriodSeconds"] = 30
        live_pod["containers"][0] = live
        expected_projection = runtime._application_pod_spec_projection(
            expected_pod, "expected Pod"
        )
        live_projection = runtime._application_pod_spec_projection(
            live_pod, "live Pod"
        )
        self.assertEqual(expected_projection, live_projection)
        self.assertIsNone(expected_projection["activeDeadlineSeconds"])

        deadline_pod = json.loads(json.dumps(live_pod))
        deadline_pod["activeDeadlineSeconds"] = 120
        deadline_projection = runtime._application_pod_spec_projection(
            deadline_pod, "deadline Pod"
        )
        self.assertEqual(deadline_projection["activeDeadlineSeconds"], 120)
        self.assertNotEqual(expected_projection, deadline_projection)
        for invalid_deadline in (0, True, "120"):
            invalid_pod = json.loads(json.dumps(live_pod))
            invalid_pod["activeDeadlineSeconds"] = invalid_deadline
            with self.subTest(active_deadline_seconds=invalid_deadline):
                with self.assertRaisesRegex(
                    runtime.RuntimeErrorEB,
                    "activeDeadlineSeconds contract is invalid",
                ):
                    runtime._application_pod_spec_projection(
                        invalid_pod, "invalid deadline Pod"
                    )

        self.assertIn(
            "_application_pod_spec_projection",
            inspect.getsource(runtime._flux_pod_spec_projection),
        )
        self.assertIn(
            "_application_pod_spec_projection",
            inspect.getsource(runtime._cilium_pod_spec_projection),
        )

        for name in ("postgres", "nats"):
            contract = runtime._versioned_data_deployment_contract(
                runtime.CLUSTER / f"data/{name}.yaml",
                name,
            )
            self.assertEqual(
                contract["contract"]["revisionHistoryLimit"],
                10,
            )
            self.assertFalse(contract["contract"]["paused"])
            self.assertEqual(contract["contract"]["minReadySeconds"], 0)
            self.assertEqual(
                contract["contract"]["progressDeadlineSeconds"], 600
            )

    def test_t048_revalidates_target_and_postgres_before_and_after_measurement(self) -> None:
        source = inspect.getsource(runtime.t048_load_proof)
        first_target = source.index("_require_kubernetes_target_binding")
        first_fixture = source.index("_validated_t048_fixture_receipt")
        load = source.index("_sample_t048_load")
        second_fixture = source.index(
            "_validated_t048_fixture_receipt",
            first_fixture + 1,
        )
        second_target = source.index(
            "_require_kubernetes_target_binding",
            first_target + 1,
        )
        first_postgres = source.index(
            "_require_t048_postgres_runtime_binding"
        )
        second_postgres = source.index(
            "_require_t048_postgres_runtime_binding",
            first_postgres + 1,
        )
        first_postgres_service = source.index(
            "_require_t048_postgres_service_binding"
        )
        second_postgres_service = source.index(
            "_require_t048_postgres_service_binding",
            first_postgres_service + 1,
        )
        bound_snapshot = source.index(
            "bound_stack.enter_context(_bound_kube_env(root, target_binding_before, source_commit))"
        )
        first_api = source.index("_require_t048_api_runtime_binding")
        second_api = source.index(
            "_require_t048_api_runtime_binding",
            first_api + 1,
        )
        post_metrics = source.index(
            'after_status, after_body, _elapsed = _http_read(f"{base_url}/metrics")'
        )
        metrics_process_before = source.index(
            "if process.poll() is not None:",
            second_api,
        )
        metrics_process_after = source.index(
            "if process.poll() is not None:",
            metrics_process_before + 1,
        )
        final_api = source.index(
            "_require_t048_api_runtime_binding",
            second_api + 1,
        )
        report = source.index("report = {", post_metrics)
        postgres_service_guard_close = source.index(
            "postgres_service_guard.close()"
        )
        port_forward = source.index("_start_api_port_forward")
        final_bound_close = source.rindex("bound_stack.close()")
        self.assertLess(first_target, bound_snapshot)
        self.assertLess(bound_snapshot, first_postgres)
        self.assertLess(bound_snapshot, first_fixture)
        self.assertLess(bound_snapshot, port_forward)
        self.assertLess(port_forward, load)
        self.assertGreater(final_bound_close, load)
        self.assertLess(first_target, first_fixture)
        self.assertLess(first_postgres, first_postgres_service)
        self.assertLess(first_postgres_service, first_fixture)
        self.assertLess(first_fixture, load)
        self.assertLess(load, second_fixture)
        self.assertLess(load, second_target)
        self.assertLess(load, second_postgres)
        self.assertLess(second_postgres, second_postgres_service)
        self.assertLess(second_api, metrics_process_before)
        self.assertLess(metrics_process_before, post_metrics)
        self.assertLess(post_metrics, metrics_process_after)
        self.assertLess(metrics_process_after, final_api)
        self.assertLess(final_api, postgres_service_guard_close)
        self.assertLess(postgres_service_guard_close, report)
        self.assertIn(
            "final_api_runtime_binding != api_runtime_binding_before",
            source,
        )
        self.assertIn(
            "fixture_binding_after != fixture_binding_before",
            source,
        )
        self.assertIn("fixture_live_binding_sha256", source)
        self.assertEqual(
            source.count("_validated_t048_fixture_receipt"),
            2,
        )
        self.assertIn("kubernetes_target_sha256", source)
        self.assertIn("postgres_runtime_image_ids_sha256", source)
        self.assertIn("postgres_resources_sha256", source)
        self.assertIn("postgres_contract_sha256", source)
        self.assertIn("postgres_pod_contract_sha256", source)
        self.assertIn("postgres_service_binding_sha256", source)
        self.assertIn(
            "_guard_t048_postgres_service_endpoints",
            source,
        )
        sampler = inspect.getsource(runtime._sample_t048_load)
        self.assertIn("postgres_service_binding", sampler)
        self.assertGreaterEqual(
            sampler.count("_require_t048_postgres_service_binding"),
            3,
        )
        self.assertIn(
            "_postgres_runtime_binding_identity(postgres_binding_after)",
            source,
        )
        self.assertIn(
            "database_identity = _database_client_identity(root)",
            source,
        )

    def test_t048_postgres_service_binding_rejects_rogue_endpoint(self) -> None:
        self.write_vm_receipt()
        self.prepare_status()
        postgres_binding = runtime._require_t048_postgres_runtime_binding(
            self.root
        )
        self.assertEqual(
            postgres_binding["pod_uid"],
            self.data_pods["postgres"][0]["metadata"]["uid"],
        )
        self.assertEqual(
            postgres_binding["pod_ip"],
            self.data_pods["postgres"][0]["status"]["podIP"],
        )

        with mock.patch.object(
            runtime,
            "_git_blob_bytes",
            side_effect=lambda _commit, path: path.read_bytes(),
        ):
            proof = runtime._require_t048_postgres_service_binding(
                self.root,
                self.commit,
                postgres_binding,
            )
            self.assertEqual(
                proof["pod_uid"],
                postgres_binding["pod_uid"],
            )
            self.assertEqual(
                proof["pod_ip"],
                postgres_binding["pod_ip"],
            )
            self.assertRegex(
                proof["service_spec_sha256"],
                r"^[0-9a-f]{64}$",
            )
            self.assertRegex(
                proof["endpoint_sha256"],
                r"^[0-9a-f]{64}$",
            )

            self.data_endpoint_slices["postgres"]["items"][0][
                "endpoints"
            ][0]["targetRef"]["uid"] = "rogue-postgres-pod-uid"
            with self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "PostgreSQL Service endpoint target drifted",
            ):
                runtime._require_t048_postgres_service_binding(
                    self.root,
                    self.commit,
                    postgres_binding,
                )

    def test_t048_postgres_binding_rejects_full_runtime_drift(
        self,
    ) -> None:
        self.write_vm_receipt()
        self.prepare_status()
        healthy_deployments = json.loads(
            json.dumps(self.data_deployments)
        )
        healthy_pods = json.loads(json.dumps(self.data_pods))

        proof = runtime._require_t048_postgres_runtime_binding(
            self.root
        )
        self.assertTrue(proof["canonical"])
        self.assertRegex(
            proof["runtime_image_ids_sha256"], r"^[0-9a-f]{64}$"
        )
        self.assertRegex(
            proof["contract_sha256"], r"^[0-9a-f]{64}$"
        )
        self.assertRegex(
            proof["pod_contract_sha256"], r"^[0-9a-f]{64}$"
        )

        self.data_deployments["postgres"]["spec"]["template"]["spec"][
            "containers"
        ][0]["image"] = "postgres:16@sha256:" + "0" * 64
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB,
            "live data Deployment images drifted",
        ):
            runtime._require_t048_postgres_runtime_binding(self.root)

        self.data_deployments = json.loads(
            json.dumps(healthy_deployments)
        )
        self.data_pods = json.loads(json.dumps(healthy_pods))
        self.data_pods["postgres"][0]["status"]["containerStatuses"][0][
            "imageID"
        ] = "containerd://sha256:" + "0" * 64
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB,
            "runtime image ID drifted",
        ):
            runtime._require_t048_postgres_runtime_binding(self.root)

        self.data_deployments = json.loads(
            json.dumps(healthy_deployments)
        )
        self.data_pods = json.loads(json.dumps(healthy_pods))
        postgres_container = next(
            item
            for item in self.data_deployments["postgres"]["spec"][
                "template"
            ]["spec"]["containers"]
            if item["name"] == "postgres"
        )
        postgres_container["resources"]["limits"]["cpu"] = "2"
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB,
            "live data Deployment contract drifted",
        ):
            runtime._require_t048_postgres_runtime_binding(self.root)

        self.data_deployments = json.loads(
            json.dumps(healthy_deployments)
        )
        self.data_pods = json.loads(json.dumps(healthy_pods))
        postgres_pod_container = next(
            item
            for item in self.data_pods["postgres"][0]["spec"][
                "containers"
            ]
            if item["name"] == "postgres"
        )
        postgres_pod_container["resources"]["limits"][
            "memory"
        ] = "1Gi"
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB,
            "live data Pod contract drifted",
        ):
            runtime._require_t048_postgres_runtime_binding(self.root)

        self.data_deployments = json.loads(
            json.dumps(healthy_deployments)
        )
        self.data_pods = json.loads(json.dumps(healthy_pods))
        postgres_container = next(
            item
            for item in self.data_deployments["postgres"]["spec"][
                "template"
            ]["spec"]["containers"]
            if item["name"] == "postgres"
        )
        postgres_container["args"] = [
            "-c",
            "max_connections=999",
        ]
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB,
            "live data Deployment contract drifted",
        ):
            runtime._require_t048_postgres_runtime_binding(self.root)

        self.data_deployments = json.loads(
            json.dumps(healthy_deployments)
        )
        self.data_pods = json.loads(json.dumps(healthy_pods))
        postgres_pod_container = next(
            item
            for item in self.data_pods["postgres"][0]["spec"][
                "containers"
            ]
            if item["name"] == "postgres"
        )
        postgres_pod_container["env"] = [
            {
                "name": "PGOPTIONS",
                "value": "-c fsync=off",
            }
        ]
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB,
            "live data Pod contract drifted",
        ):
            runtime._require_t048_postgres_runtime_binding(self.root)


    def test_source_commit_nats_contract_rejects_live_image_drift(self) -> None:
        source_bytes = (runtime.CLUSTER / "data/nats.yaml").read_bytes()
        nats_container = next(
            item
            for item in self.data_deployments["nats"]["spec"]["template"][
                "spec"
            ]["containers"]
            if item["name"] == "nats"
        )
        nats_container["image"] = "nats@sha256:" + "d" * 64

        with (
            mock.patch.object(
                runtime,
                "_git_blob_bytes",
                return_value=source_bytes,
            ) as git_blob,
            mock.patch.object(
                runtime,
                "_kubectl_json",
                side_effect=self.kubernetes_fixture,
            ),
            self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "images drifted from versioned manifest",
            ),
        ):
            runtime._require_live_data_deployments(
                self.root,
                ("nats",),
                source_commit=self.commit,
            )

        git_blob.assert_called_once_with(
            self.commit,
            runtime.CLUSTER / "data/nats.yaml",
        )

    def test_status_revalidates_namespace_restricted_security_labels(self) -> None:
        self.write_vm_receipt()
        self.prepare_status()
        expected = runtime._versioned_namespace_security_contract()
        result = runtime._require_live_namespace_security_contract(
            self.root
        )
        self.assertEqual(set(result), {runtime.APP_NAMESPACE, runtime.DATA_NAMESPACE})
        self.assertEqual(
            result[runtime.APP_NAMESPACE]["labels"],
            expected[runtime.APP_NAMESPACE]["labels"],
        )

        healthy = json.loads(json.dumps(self.namespaces))
        del self.namespaces[runtime.APP_NAMESPACE]["metadata"]["labels"][
            "pod-security.kubernetes.io/enforce"
        ]
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB,
            "Namespace security labels drifted",
        ):
            runtime._require_live_namespace_security_contract(
                self.root
            )

        self.namespaces = json.loads(json.dumps(healthy))
        self.namespaces[runtime.DATA_NAMESPACE]["metadata"]["labels"][
            "unexpected.example/label"
        ] = "drift"
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB,
            "Namespace security labels drifted",
        ):
            runtime._require_live_namespace_security_contract(
                self.root
            )

        status_source = inspect.getsource(runtime.status)
        portability_source = inspect.getsource(
            runtime.portability_report
        )
        self.assertIn(
            "_require_live_namespace_security_contract",
            status_source,
        )
        self.assertIn(
            "_versioned_namespace_security_contract",
            portability_source,
        )

    def test_create_rollback_refuses_foreign_same_name_domain(self) -> None:
        self.prepare_create()
        original = self.run_fixture
        foreign_uuid = "44444444-4444-4444-8444-444444444444"

        def collide_before_virt_install(argv, **kwargs):
            if argv[0] == "virt-install":
                self.domain_present = True
                self.domain_active = True
                self.domain_uuid = foreign_uuid
                raise runtime.RuntimeErrorEB("simulated create collision")
            return original(argv, **kwargs)

        self.runner.side_effect = collide_before_virt_install
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB,
            "rollback domain UUID drifted",
        ):
            runtime.create_vm(self.root)
        self.assertTrue(self.domain_present)
        self.assertEqual(self.domain_uuid, foreign_uuid)
        self.assertTrue(self.pool_present)
        self.assertTrue(self.disk.exists())
        self.assertTrue(self.base.exists())

    def test_create_rollback_refuses_pool_uuid_drift_before_storage_cleanup(
        self,
    ) -> None:
        self.prepare_create()
        self.main.side_effect = [self.commit, "b" * 40]
        original = self.run_fixture
        foreign_pool_uuid = "55555555-5555-4555-8555-555555555555"

        def drift_pool_after_domain_retirement(argv, **kwargs):
            result = original(argv, **kwargs)
            if (
                argv[:3] == ["virsh", "-c", runtime.LIBVIRT_URI]
                and argv[3] == "undefine"
                and result.returncode == 0
            ):
                self.pool_uuid = foreign_pool_uuid
            return result

        self.runner.side_effect = drift_pool_after_domain_retirement
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB,
            "rollback pool UUID drifted",
        ):
            runtime.create_vm(self.root)
        self.assertFalse(self.domain_present)
        self.assertTrue(self.pool_present)
        self.assertEqual(self.pool_uuid, foreign_pool_uuid)
        self.assertTrue(self.disk.exists())
        self.assertTrue(self.base.exists())

    def test_create_rollback_stops_before_storage_when_destroy_fails(self) -> None:
        self.prepare_create()
        self.main.side_effect = [self.commit, "b" * 40]
        original = self.run_fixture

        def fail_destroy(argv, **kwargs):
            if (
                argv[:3] == ["virsh", "-c", runtime.LIBVIRT_URI]
                and argv[3] == "destroy"
            ):
                return runtime.subprocess.CompletedProcess(
                    argv, 1, stdout="", stderr="destroy failed"
                )
            return original(argv, **kwargs)

        self.runner.side_effect = fail_destroy
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB,
            "storage cleanup is forbidden",
        ):
            runtime.create_vm(self.root)
        self.assertTrue(self.domain_present)
        self.assertTrue(self.domain_active)
        self.assertTrue(self.pool_present)
        self.assertTrue(self.disk.exists())
        self.assertTrue(self.base.exists())

    def test_create_rollback_retries_transient_storage_cleanup_failures(self) -> None:
        self.prepare_create()
        self.main.side_effect = [self.commit, "b" * 40]
        original = self.run_fixture
        failures = {
            "vol-delete": 1,
            "pool-destroy": 1,
            "pool-undefine": 1,
        }

        def transient_cleanup_failures(argv, **kwargs):
            if argv[:3] == ["virsh", "-c", runtime.LIBVIRT_URI]:
                command = argv[3]
                if failures.get(command, 0):
                    failures[command] -= 1
                    return runtime.subprocess.CompletedProcess(
                        argv,
                        1,
                        stdout="",
                        stderr=f"transient {command} failure",
                    )
            return original(argv, **kwargs)

        self.runner.side_effect = transient_cleanup_failures
        with (
            mock.patch.object(runtime.time, "sleep"),
            self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "VM creation source/config changed",
            ),
        ):
            runtime.create_vm(self.root)
        self.assertEqual(failures, {
            "vol-delete": 0,
            "pool-destroy": 0,
            "pool-undefine": 0,
        })
        self.assertFalse(self.domain_present)
        self.assertFalse(self.pool_present)
        self.assertFalse(self.disk.exists())
        self.assertFalse(self.base.exists())
        self.assertFalse(self.pool.exists())

    def test_create_binds_actual_vm_to_current_source_and_config(self) -> None:
        self.prepare_create()
        result = runtime.create_vm(self.root)
        self.assertEqual(
            result, vm_receipt_fixture(result["substrate"], self.root)
        )
        self.assertEqual(result["substrate"], runtime._live_vm_substrate(self.root, self.config))
        self.assertEqual(json.loads((self.root / "receipts/vm-create.json").read_text()), result)
        self.assertEqual(self.main.call_count, 2)

        attempt = json.loads(
            (self.root / "receipts/vm-create-attempt.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(attempt["domain_target"], result["substrate"]["uuid"])
        self.assertEqual(attempt["pool_target"], result["substrate"]["pool_uuid"])
        self.assertEqual(
            attempt["base_image_sha256"],
            result["substrate"]["base_image_sha256"],
        )
        self.assertEqual(
            attempt["volume_device"], result["substrate"]["disk_device"]
        )
        self.assertEqual(
            attempt["volume_inode"], result["substrate"]["disk_inode"]
        )
        self.assertRegex(attempt["substrate_sha256"], r"^[0-9a-f]{64}$")
    def test_create_passes_only_verified_cloud_init_snapshots_to_virt_install(
        self,
    ) -> None:
        self.prepare_create()
        original = self.run_fixture
        observed: dict[str, bytes] = {}

        def capture_cloud_init(argv, **kwargs):
            if argv[0] == "virt-install":
                cloud_init_arg = argv[argv.index("--cloud-init") + 1]
                fields = dict(
                    item.split("=", 1)
                    for item in cloud_init_arg.split(",")
                )
                self.assertEqual(set(fields), {"user-data", "meta-data"})
                pass_fds = tuple(kwargs.get("pass_fds", ()))
                fd_by_field = {
                    name: int(value.rsplit("/", 1)[1])
                    for name, value in fields.items()
                }
                self.assertEqual(set(fd_by_field.values()), set(pass_fds))
                self.assertTrue(
                    all(
                        value.startswith("/proc/self/fd/")
                        for value in fields.values()
                    )
                )
                self.cloud_user_data.write_bytes(b"tampered user-data\n")
                self.cloud_meta_data.write_bytes(b"tampered meta-data\n")
                observed["user-data"] = runtime.os.pread(
                    fd_by_field["user-data"],
                    len(self.cloud_user_data_bytes) + 32,
                    0,
                )
                observed["meta-data"] = runtime.os.pread(
                    fd_by_field["meta-data"],
                    len(self.cloud_meta_data_bytes) + 32,
                    0,
                )
            return original(argv, **kwargs)

        self.runner.side_effect = capture_cloud_init
        result = runtime.create_vm(self.root)
        self.assertEqual(result["status"], "created")
        self.assertEqual(observed["user-data"], self.cloud_user_data_bytes)
        self.assertEqual(observed["meta-data"], self.cloud_meta_data_bytes)

    def test_post_create_validation_and_binding_failures_cleanup_vm_and_pool(self) -> None:
        for failure in ("substrate", "base_digest", "source", "config", "receipt_write"):
            with self.subTest(failure=failure):
                if not self.pool.exists():
                    self.pool.mkdir()
                    self.disk.touch()
                    self.base.touch()
                self.prepare_create()
                self.main.side_effect = [self.commit, "b" * 40] if failure == "source" else None
                self.base_bytes = b"wrong image" if failure == "base_digest" else b"pinned Ubuntu image fixture"
                self.qmp["return"][0]["inserted"]["image"]["virtual-size"] = (
                    30 if failure == "substrate" else 60
                ) * 1024**3
                original_sha256, original_write = runtime.sha256_file, runtime.atomic_json

                def digest(path):
                    if failure == "config" and path == runtime.CLUSTER / "config.json" and self.domain_present:
                        return "0" * 64
                    return original_sha256(path)

                def write(path, payload):
                    if failure == "receipt_write" and path.name == "vm-create.json":
                        raise OSError("receipt write failed")
                    original_write(path, payload)

                with mock.patch.object(runtime, "sha256_file", side_effect=digest), mock.patch.object(
                    runtime, "atomic_json", side_effect=write,
                ):
                    with self.assertRaises((runtime.RuntimeErrorEB, OSError)):
                        runtime.create_vm(self.root)
                self.assertFalse(self.domain_present)
                self.assertFalse(self.pool_present)
                self.assertFalse(self.pool.exists())
                self.assertFalse((self.root / "receipts/vm-create.json").exists())
                self.assertFalse(list(self.root.glob(".vm-substrate-*")))

    def test_teardown_resumes_exact_partial_retirement(self) -> None:
        retirement = self.root.with_name(
            self.root.name + "-resume-retirement.json"
        )
        attempt = retirement.with_name(
            f"{retirement.stem}-attempt{retirement.suffix}"
        )
        self.addCleanup(retirement.unlink, missing_ok=True)
        self.addCleanup(attempt.unlink, missing_ok=True)
        self.write_vm_receipt()
        original = self.run_fixture
        def fail_base_volume_cleanup(argv, **kwargs):
            if (
                argv[:3] == ["virsh", "-c", runtime.LIBVIRT_URI]
                and argv[3] == "vol-delete"
                and argv[4] == runtime.BASE_VOLUME
            ):
                return runtime.subprocess.CompletedProcess(
                    argv,
                    1,
                    stdout="",
                    stderr="persistent base-volume failure",
                )
            return original(argv, **kwargs)

        with (
            mock.patch.object(runtime, "RETIREMENT_RECEIPT", retirement),
            mock.patch.object(runtime.time, "sleep"),
        ):
            self.runner.side_effect = fail_base_volume_cleanup
            with self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "could not remove libvirt volume",
            ):
                runtime.teardown(self.root)
            self.assertTrue(attempt.is_file())
            self.assertFalse(self.domain_present)
            self.assertTrue(self.pool_present)
            self.assertFalse(self.disk.exists())
            self.assertTrue(self.base.exists())

            payload = json.loads(attempt.read_text(encoding="utf-8"))
            self.assertEqual(payload["operation"], "teardown")
            self.assertEqual(payload["domain_target"], self.domain_uuid)
            self.assertEqual(payload["pool_target"], self.pool_uuid)

            self.runner.side_effect = original
            result = runtime.teardown(self.root)

        self.assertEqual(result["status"], "retired")
        self.assertFalse(self.domain_present)
        self.assertFalse(self.pool_present)
        self.assertFalse(self.root.exists())
        self.assertFalse(attempt.exists())
        self.assertTrue(retirement.is_file())



class ExperimentBLatestP1RegressionTests(unittest.TestCase):

    def test_bound_kube_env_freezes_verified_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "kubeconfig.yaml"
            source.write_text(
                yaml.safe_dump(
                    {
                        "current-context": "experiment-b",
                        "contexts": [
                            {
                                "name": "experiment-b",
                                "context": {"cluster": "experiment-b"},
                            }
                        ],
                        "clusters": [
                            {
                                "name": "experiment-b",
                                "cluster": {
                                    "server": "https://192.168.122.10:6443"
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            source.chmod(0o600)
            expected = {
                "vm_ip": "192.168.122.10",
                "kubeconfig_sha256": runtime.sha256_file(source),
                "server": "https://192.168.122.10:6443",
            }
            commit = "a" * 40
            with mock.patch.object(
                runtime,
                "toolchain",
                return_value={"tools": {}, "artifacts": {}},
            ) as bound_tools:
                with runtime._bound_kube_env(
                    root,
                    expected,
                    commit,
                ) as env:
                    snapshot = Path(env["KUBECONFIG"])
                    self.assertNotEqual(snapshot, source)
                    self.assertTrue(
                        str(snapshot).startswith("/proc/self/fd/")
                    )
                    snapshot_fd = int(snapshot.name)
                    self.assertIn(
                        snapshot_fd,
                        runtime._bound_subprocess_pass_fds(),
                    )
                    self.assertEqual(
                        runtime._BOUND_SOURCE_COMMIT.get(),
                        commit,
                    )
                    self.assertEqual(
                        runtime.sha256_file(snapshot),
                        expected["kubeconfig_sha256"],
                    )
                    snapshot_size = runtime.os.fstat(snapshot_fd).st_size
                    self.assertEqual(
                        runtime._kubeconfig_server_payload(
                            runtime.os.pread(
                                snapshot_fd,
                                snapshot_size,
                                0,
                            )
                        ),
                        expected["server"],
                    )
                    self.assertEqual(
                        runtime.kube_env(root)["KUBECONFIG"],
                        str(snapshot),
                    )
                    with self.assertRaises(OSError):
                        snapshot.write_bytes(b"forged kubeconfig")
                    source.write_text("drifted", encoding="utf-8")
                    child = runtime.run(
                        [
                            sys.executable,
                            "-c",
                            (
                                "import os; from pathlib import Path; "
                                "print(Path(os.environ['KUBECONFIG']).read_text())"
                            ),
                        ],
                        env=env,
                    )
                    self.assertIn("experiment-b", child.stdout)
                    self.assertNotIn("drifted", child.stdout)
                    self.assertEqual(
                        runtime.sha256_file(snapshot),
                        expected["kubeconfig_sha256"],
                    )
                self.assertFalse(snapshot.exists())
                self.assertIsNone(
                    runtime._BOUND_SOURCE_COMMIT.get()
                )
            bound_tools.assert_called_once_with(root, commit)

    def test_performance_helpers_are_loaded_from_source_commit(self) -> None:
        commit = "a" * 40
        live_path = (
            runtime.ROOT / "scripts/performance/api_runtime_live_binding.py"
        )
        evidence_path = (
            runtime.ROOT / "scripts/performance/api_runtime_evidence.py"
        )
        payloads = {
            live_path: (
                b"from pathlib import Path\n"
                b"REPO_ROOT = Path(__file__).resolve().parents[2]\n"
                b'MARKER = "live-from-source-commit"\n'
            ),
            runtime.DOMAIN_SCALE: (
                b"from pathlib import Path\n"
                b"ROOT = Path(__file__).resolve().parents[2]\n"
                b'MARKER = "domain-from-source-commit"\n'
            ),
            evidence_path: (
                b"from pathlib import Path\n"
                b"from scripts.performance import api_runtime_live_binding as live_binding\n"
                b"REPO_ROOT = Path(__file__).resolve().parents[2]\n"
                b"MARKER = live_binding.MARKER\n"
            ),
        }

        def blob(source_commit: str, source_path: Path) -> bytes:
            self.assertEqual(source_commit, commit)
            return payloads[source_path]

        with mock.patch.object(
            runtime,
            "_git_blob_bytes",
            side_effect=blob,
        ) as git_blob:
            evidence, domain_scale = runtime._performance_modules(commit)

        expected_source_root = Path(
            f"/__experiment_b_source_commit__/{commit}"
        )
        self.assertEqual(evidence.MARKER, "live-from-source-commit")
        self.assertEqual(domain_scale.MARKER, "domain-from-source-commit")
        self.assertEqual(evidence.live_binding.MARKER, "live-from-source-commit")
        self.assertEqual(evidence.REPO_ROOT, expected_source_root)
        self.assertEqual(domain_scale.ROOT, expected_source_root)
        self.assertEqual(evidence.live_binding.REPO_ROOT, expected_source_root)
        self.assertNotEqual(evidence.REPO_ROOT, runtime.ROOT)
        self.assertNotEqual(domain_scale.ROOT, runtime.ROOT)
        self.assertNotEqual(evidence.live_binding.REPO_ROOT, runtime.ROOT)
        self.assertEqual(git_blob.call_count, 3)

    def test_mutated_worktree_performance_modules_are_not_executed(self) -> None:
        commit = "a" * 40
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            performance = repo_root / "scripts/performance"
            performance.mkdir(parents=True)
            live_path = performance / "api_runtime_live_binding.py"
            domain_path = performance / "domain_scale.py"
            evidence_path = performance / "api_runtime_evidence.py"
            for worktree_path in (live_path, domain_path, evidence_path):
                worktree_path.write_text(
                    'raise RuntimeError("mutable worktree module executed")\n',
                    encoding="utf-8",
                )
            payloads = {
                live_path: b'MARKER = "live-from-commit"\n',
                domain_path: b'MARKER = "domain-from-commit"\n',
                evidence_path: (
                    b"from scripts.performance import api_runtime_live_binding as live_binding\n"
                    b"MARKER = live_binding.MARKER\n"
                ),
            }

            def blob(source_commit: str, source_path: Path) -> bytes:
                self.assertEqual(source_commit, commit)
                return payloads[source_path]

            with (
                mock.patch.object(runtime, "ROOT", repo_root),
                mock.patch.object(runtime, "DOMAIN_SCALE", domain_path),
                mock.patch.object(
                    runtime,
                    "_git_blob_bytes",
                    side_effect=blob,
                ),
            ):
                evidence, domain_scale = runtime._performance_modules(commit)

        self.assertEqual(evidence.MARKER, "live-from-commit")
        self.assertEqual(domain_scale.MARKER, "domain-from-commit")
        self.assertEqual(evidence.live_binding.MARKER, "live-from-commit")

    def test_runtime_local_helpers_are_not_eager_imported(self) -> None:
        source = Path(runtime.__file__).read_text(encoding="utf-8")
        self.assertNotIn("import experiment_b as contract", source)
        self.assertNotIn("import bootstrap_tools", source)
        self.assertIn("_source_bound_contract(source_commit)", source)
        self.assertIn("_source_bound_bootstrap_tools(commit)", source)

    def test_mutated_worktree_contract_helper_is_not_executed(self) -> None:
        commit = "a" * 40
        payload = b"""
def validate_config(config):
    return None

def render_cloud_init(public_key_file, output_dir, hostname):
    return {"authority": "source-commit"}

def render_bootstrap_from_template(
    source_commit,
    api_digest,
    web_digest,
    output,
    template_bytes,
):
    return {"authority": "source-commit"}
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            helper = repo_root / "scripts/platform/experiment_b.py"
            helper.parent.mkdir(parents=True)
            helper.write_text(
                'raise RuntimeError("mutable contract helper executed")\n',
                encoding="utf-8",
            )
            with (
                mock.patch.object(runtime, "ROOT", repo_root),
                mock.patch.object(runtime, "CONTRACT_HELPER", helper),
                mock.patch.object(
                    runtime,
                    "_git_blob_bytes",
                    return_value=payload,
                ),
            ):
                bound = runtime._source_bound_contract(commit)
                rendered = bound.render_cloud_init(
                    Path("unused"),
                    Path("unused"),
                    "unused",
                )

        self.assertEqual(rendered["authority"], "source-commit")

    def test_mutated_worktree_bootstrap_helper_is_not_executed(self) -> None:
        commit = "a" * 40
        payload = b"""
def install(*args, **kwargs):
    return {"authority": "source-commit"}
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            helper = repo_root / "scripts/platform/bootstrap_tools.py"
            helper.parent.mkdir(parents=True)
            helper.write_text(
                'raise RuntimeError("mutable bootstrap helper executed")\n',
                encoding="utf-8",
            )
            with (
                mock.patch.object(runtime, "ROOT", repo_root),
                mock.patch.object(runtime, "BOOTSTRAP_TOOLS_HELPER", helper),
                mock.patch.object(
                    runtime,
                    "_git_blob_bytes",
                    return_value=payload,
                ),
            ):
                bound = runtime._source_bound_bootstrap_tools(commit)
                receipt = bound.install()

        self.assertEqual(receipt["authority"], "source-commit")

    def test_source_bound_dataset_binding_uses_commit_config_snapshot(self) -> None:
        commit = "a" * 40
        config_bytes = b'{"authority":"source-commit"}\n'
        manifest_bytes = b'{"fixture":"runtime-generated"}\n'
        returned_manifest = {
            "generator": "scripts/performance/domain_scale.py",
            "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
            "database_schema": "weltgewebe_perf",
            "profile": "ci",
            "counts": {"nodes": 2, "edges": 1},
            "files": {
                "nodes": {"name": "domain_nodes.csv", "sha256": "b" * 64},
                "edges": {"name": "domain_edges.csv", "sha256": "c" * 64},
            },
        }

        class FakeDomainScale:
            DomainScaleError = RuntimeError

            @staticmethod
            def load_bound_manifest(
                manifest_path: Path,
                config_path: Path,
            ) -> tuple[dict, dict]:
                self.assertEqual(manifest_path.read_bytes(), manifest_bytes)
                self.assertTrue(str(config_path).startswith("/proc/self/fd/"))
                self.assertEqual(config_path.read_bytes(), config_bytes)
                return {}, returned_manifest

            @staticmethod
            def generate_fixture(
                config_path: Path,
                profile_name: str,
                output_dir: Path,
            ) -> dict:
                self.assertTrue(str(config_path).startswith("/proc/self/fd/"))
                self.assertEqual(config_path.read_bytes(), config_bytes)
                self.assertEqual(profile_name, "ci")
                self.assertEqual(output_dir.name, "fixture")
                return returned_manifest

        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir) / "repo"
            domain_path = repo_root / "scripts/performance/domain_scale.py"
            config_path = repo_root / "configs/performance/domain-scale.v1.json"
            domain_path.parent.mkdir(parents=True)
            config_path.parent.mkdir(parents=True)
            domain_path.write_text(
                'raise RuntimeError("mutable worktree generator executed")\n',
                encoding="utf-8",
            )
            config_path.write_bytes(b'{"authority":"mutable-worktree"}\n')
            manifest = repo_root / "fixture/manifest.json"
            manifest.parent.mkdir()
            manifest.write_bytes(manifest_bytes)
            contract = {
                "dataset_proof": {
                    "generator": "scripts/performance/domain_scale.py",
                    "config": "configs/performance/domain-scale.v1.json",
                    "profile": "ci",
                }
            }
            with (
                mock.patch.object(runtime, "ROOT", repo_root),
                mock.patch.object(runtime, "DOMAIN_SCALE", domain_path),
                mock.patch.object(runtime, "DOMAIN_SCALE_CONFIG", config_path),
                mock.patch.object(
                    runtime,
                    "_git_blob_bytes",
                    return_value=config_bytes,
                ) as git_blob,
            ):
                binding = runtime._source_bound_dataset_binding(
                    commit,
                    manifest,
                    contract,
                    FakeDomainScale,
                )

        git_blob.assert_called_once_with(commit, config_path)
        self.assertEqual(binding["config_sha256"], returned_manifest["config_sha256"])
        self.assertEqual(binding["profile"], "ci")
        self.assertEqual(binding["counts"], {"nodes": 2, "edges": 1})

    def test_source_bound_dataset_binding_rejects_self_consistent_mutation(
        self,
    ) -> None:
        commit = "a" * 40
        config_bytes = b'{"authority":"source-commit"}\n'
        canonical_manifest = {
            "generator": "scripts/performance/domain_scale.py",
            "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
            "database_schema": "weltgewebe_perf",
            "profile": "ci",
            "counts": {"nodes": 2, "edges": 1},
            "files": {
                "nodes": {
                    "name": "domain_nodes.csv",
                    "sha256": "b" * 64,
                },
                "edges": {
                    "name": "domain_edges.csv",
                    "sha256": "c" * 64,
                },
            },
        }
        retained_manifest = json.loads(json.dumps(canonical_manifest))
        retained_manifest["files"]["nodes"]["sha256"] = "d" * 64

        class FakeDomainScale:
            DomainScaleError = RuntimeError

            @staticmethod
            def load_bound_manifest(
                manifest_path: Path,
                config_path: Path,
            ) -> tuple[dict, dict]:
                self.assertTrue(manifest_path.is_file())
                self.assertEqual(config_path.read_bytes(), config_bytes)
                return {}, retained_manifest

            @staticmethod
            def generate_fixture(
                config_path: Path,
                profile_name: str,
                output_dir: Path,
            ) -> dict:
                self.assertEqual(config_path.read_bytes(), config_bytes)
                self.assertEqual(profile_name, "ci")
                self.assertEqual(output_dir.name, "fixture")
                return canonical_manifest

        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir) / "repo"
            domain_path = repo_root / "scripts/performance/domain_scale.py"
            config_path = repo_root / "configs/performance/domain-scale.v1.json"
            domain_path.parent.mkdir(parents=True)
            config_path.parent.mkdir(parents=True)
            domain_path.write_text(
                'raise RuntimeError("mutable worktree generator executed")\n',
                encoding="utf-8",
            )
            config_path.write_bytes(b'{"authority":"mutable-worktree"}\n')
            manifest = repo_root / "fixture/manifest.json"
            manifest.parent.mkdir()
            manifest.write_text(
                json.dumps(retained_manifest) + "\n",
                encoding="utf-8",
            )
            contract = {
                "dataset_proof": {
                    "generator": "scripts/performance/domain_scale.py",
                    "config": "configs/performance/domain-scale.v1.json",
                    "profile": "ci",
                }
            }
            with (
                mock.patch.object(runtime, "ROOT", repo_root),
                mock.patch.object(runtime, "DOMAIN_SCALE", domain_path),
                mock.patch.object(runtime, "DOMAIN_SCALE_CONFIG", config_path),
                mock.patch.object(
                    runtime,
                    "_git_blob_bytes",
                    return_value=config_bytes,
                ),
                self.assertRaisesRegex(
                    runtime.RuntimeErrorEB,
                    "not canonical source-commit generator output",
                ),
            ):
                runtime._source_bound_dataset_binding(
                    commit,
                    manifest,
                    contract,
                    FakeDomainScale,
                )

    def test_t048_fixture_receipt_rebinds_source_generator_before_live_state(
        self,
    ) -> None:
        commit = "a" * 40
        generation_id = "experiment-b-t048"
        contract_section = {
            "dataset_proof": {
                "generator": "scripts/performance/domain_scale.py",
                "config": "configs/performance/domain-scale.v1.json",
                "profile": "ci",
            }
        }
        evidence = mock.Mock()
        evidence.api_runtime_section.return_value = contract_section
        domain_scale = object()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "performance/fixture/manifest.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text("{}\n", encoding="utf-8")
            (root / "receipts").mkdir()
            runtime.atomic_json(
                root / "receipts/t048-fixture.json",
                {
                    "schema_version": 1,
                    "status": "loaded",
                    "source_commit": commit,
                    "profile": "ci",
                    "manifest": str(manifest),
                    "manifest_sha256": runtime.sha256_file(manifest),
                    "generation_id": generation_id,
                    "live_binding": {"self_consistent": True},
                },
            )

            with (
                mock.patch.object(
                    runtime,
                    "_performance_modules",
                    return_value=(evidence, domain_scale),
                ),
                mock.patch.object(
                    runtime,
                    "_source_bound_performance_policy",
                    return_value=({"policy": "source-bound"}, "f" * 64),
                ),
                mock.patch.object(
                    runtime,
                    "_source_bound_dataset_binding",
                    side_effect=runtime.RuntimeErrorEB(
                        "canonical generator drift"
                    ),
                ) as source_binding,
                mock.patch.object(
                    runtime,
                    "_t048_live_fixture_binding",
                ) as live_binding,
                self.assertRaisesRegex(
                    runtime.RuntimeErrorEB,
                    "canonical generator drift",
                ),
            ):
                runtime._validated_t048_fixture_receipt(root, commit)

            source_binding.assert_called_once_with(
                commit,
                manifest,
                contract_section,
                domain_scale,
            )
            live_binding.assert_not_called()

    def test_t048_runtime_config_authority_is_source_commit_bound(self) -> None:
        seed_source = inspect.getsource(runtime.seed_t048_fixture)
        receipt_source = inspect.getsource(runtime._validated_t048_fixture_receipt)
        live_source = inspect.getsource(runtime._t048_live_fixture_binding)
        install_source = inspect.getsource(runtime.install_platform)

        self.assertIn("_source_commit_config(source_commit)", seed_source)
        self.assertNotIn("config = load_config()", seed_source)
        self.assertIn("_source_commit_config(source_commit)", receipt_source)
        self.assertIn("_performance_modules(source_commit)", receipt_source)
        self.assertIn("_source_bound_dataset_binding(", receipt_source)
        self.assertNotIn("load_config()", receipt_source)
        self.assertIn("_source_commit_config(source_commit)", live_source)
        self.assertNotIn("load_config()", live_source)
        self.assertIn(
            "_source_commit_config(source_commit)",
            install_source,
        )
        self.assertNotIn(
            "_require_live_cilium_contract(root, load_config())",
            install_source,
        )

    def test_t048_fixture_generation_and_load_are_source_commit_bound(self) -> None:
        source = inspect.getsource(runtime.seed_t048_fixture)
        self.assertIn("_performance_modules(source_commit)", source)
        self.assertIn("domain_scale.generate_fixture(", source)
        self.assertIn("_source_bound_dataset_binding(", source)
        self.assertIn("domain_scale.render_load_sql(", source)
        self.assertGreaterEqual(source.count("_verified_snapshot_fd("), 2)
        self.assertIn('"source-commit T048 load SQL"', source)
        self.assertIn('"source-commit T048 streamed SQL"', source)
        self.assertEqual(
            source.count(
                "postgres_binding = _require_postgres_runtime_binding("
            ),
            1,
        )
        self.assertIn("_run_bound_postgres_client(", source)
        self.assertNotIn(
            "_psql(",
            source.replace("bound_psql(", ""),
        )
        self.assertNotIn('"deployment/postgres"', source)
        self.assertIn(
            "PostgreSQL runtime changed during T048 fixture load",
            source,
        )
        self.assertNotIn("str(DOMAIN_SCALE)", source)
        self.assertNotIn("evidence.load_dataset_binding(", source)

    def test_functional_readback_lives_inside_bound_kube_context(self) -> None:
        source = inspect.getsource(runtime.functional_readback)
        bound = source.index("with _bound_kube_env(")
        endpoint_guard = source.index(
            "with _guard_functional_service_endpoints("
        )
        gateway = source.index("_gateway_data_plane_readback(")
        nats_binding = source.index(
            "nats_binding_before = _require_nats_runtime_binding("
        )
        jetstream = source.index("_jetstream_signature(")
        final_target = source.rindex("_require_kubernetes_target_binding")
        receipt = source.index("receipt = {")
        self.assertLess(bound, endpoint_guard)
        self.assertLess(endpoint_guard, gateway)
        self.assertLess(gateway, nats_binding)
        self.assertLess(nats_binding, jetstream)
        self.assertLess(jetstream, final_target)
        self.assertLess(final_target, receipt)

    def test_t048_authority_inputs_are_source_commit_bound(self) -> None:
        commit = "a" * 40
        workflow = (
            "env:\n"
            "  K6_IMAGE: "
            "grafana/k6@sha256:"
            + "b" * 64
            + "\n"
        ).encode()
        workload = b"import http from 'k6/http';\nimport { check } from 'k6';\n"
        policy = json.dumps(
            {
                "contract_id": "weltgewebe-performance-v1",
                "measurements": {},
            }
        ).encode()

        def blob(source_commit, path):
            self.assertEqual(source_commit, commit)
            return {
                runtime.K6_WORKFLOW: workflow,
                runtime.K6_WORKLOAD: workload,
                runtime.PERFORMANCE_POLICY: policy,
            }[path]

        with mock.patch.object(runtime, "_git_blob_bytes", side_effect=blob):
            image, workflow_sha = runtime._k6_image_binding(commit)
            workload_text, workload_sha = runtime._source_bound_k6_workload(
                commit
            )
            parsed_policy, policy_sha = (
                runtime._source_bound_performance_policy(commit)
            )

        self.assertEqual(
            image,
            "grafana/k6@sha256:" + "b" * 64,
        )
        self.assertEqual(workflow_sha, hashlib.sha256(workflow).hexdigest())
        self.assertEqual(workload_text.encode(), workload)
        self.assertEqual(workload_sha, hashlib.sha256(workload).hexdigest())
        self.assertEqual(
            parsed_policy["contract_id"],
            "weltgewebe-performance-v1",
        )
        self.assertEqual(policy_sha, hashlib.sha256(policy).hexdigest())

        with mock.patch.object(
            runtime,
            "_git_blob_bytes",
            return_value=b"import helper from './helper.js';\n",
        ), self.assertRaisesRegex(
            runtime.RuntimeErrorEB,
            "imports an unbound module",
        ):
            runtime._source_bound_k6_workload(commit)

        load_source = inspect.getsource(runtime.t048_load_proof)
        self.assertIn("_source_bound_performance_policy", load_source)
        self.assertIn("_k6_image_binding(source_commit)", load_source)
        self.assertIn("_source_bound_k6_workload", load_source)
        self.assertIn('"--interactive"', load_source)
        self.assertIn('k6_image, "run", "--quiet", "-"', load_source)
        self.assertIn("stdin=subprocess.PIPE", load_source)
        self.assertIn("_k6_summary_output_channel()", load_source)
        self.assertIn("_seal_k6_summary_output(", load_source)
        self.assertIn("stdout=k6_summary_output_fd", load_source)
        self.assertIn('"API_RUNTIME_SUMMARY_PATH=stdout"', load_source)
        self.assertIn(
            'Path(f"/proc/self/fd/{k6_summary_snapshot_fd}")',
            load_source,
        )
        self.assertNotIn('"--volume"', load_source)
        self.assertNotIn("k6-summary.json", load_source)
        self.assertNotIn("load_k6_summary(k6_summary_path)", load_source)
        workload_source = runtime.K6_WORKLOAD.read_text(encoding="utf-8")
        self.assertIn("outputPath === 'stdout'", workload_source)
        self.assertIn(runtime.K6_SUMMARY_STDOUT_MARKER, workload_source)
        self.assertNotIn("/workspace", load_source)
        self.assertNotIn("sha256_file(PERFORMANCE_POLICY)", load_source)

        seed_source = inspect.getsource(runtime.seed_t048_fixture)
        self.assertIn("_source_bound_performance_policy", seed_source)
        self.assertNotIn("load_policy(PERFORMANCE_POLICY)", seed_source)

    def test_platform_install_mutations_are_bound_to_snapshot_and_rechecked(self) -> None:
        source = inspect.getsource(runtime.install_platform)
        target_capture = source.index(
            "platform_target = _kubernetes_target_identity"
        )
        snapshot = source.index("with _bound_kube_env")
        gateway_apply = source.index(
            'run([kubectl, "apply", "-f", artifacts[name]], env=env)'
        )
        cilium_install = source.index(
            'helm, "upgrade", "--install", "cilium"'
        )
        node_ready = source.index("_require_exact_k3s_node_inventory(")
        flux_install = source.index("_flux_install_argv(flux)")
        cilium_readback = source.index("_require_live_cilium_contract(")
        flux_readback = source.index("_require_live_flux_controller_contract(")
        final_target_check = source.rindex("_require_same_kubernetes_target")
        receipt_write = source.index('atomic_json(root / "receipts/platform.json", result)')
        self.assertLess(target_capture, snapshot)
        self.assertLess(snapshot, gateway_apply)
        self.assertLess(gateway_apply, cilium_install)
        self.assertLess(cilium_install, node_ready)
        self.assertLess(node_ready, flux_install)
        ready_gate = source[cilium_install:flux_install]
        self.assertIn("time.monotonic() + 180", ready_gate)
        self.assertIn("timeout=", ready_gate)
        self.assertIn("subprocess.TimeoutExpired", ready_gate)
        self.assertLess(flux_install, cilium_readback)
        self.assertLess(cilium_readback, flux_readback)
        self.assertLess(flux_readback, final_target_check)
        self.assertIn("\n        cilium_readback =", source)
        self.assertIn("\n                flux_readback =", source)
        self.assertLess(final_target_check, receipt_write)
        self.assertGreaterEqual(
            source.count("_require_same_kubernetes_target"),
            7,
        )
        self.assertIn('"kubernetes_target_sha256"', source)


    def test_all_mutating_workflows_freeze_target_and_revalidate_success(
        self,
    ) -> None:
        for function, target_name in (
            (runtime.inject_secrets, "secrets_target"),
            (runtime.apply_release, "release_target"),
            (runtime.semantic_activate, "semantic_target"),
            (runtime.seed_t048_fixture, "fixture_target"),
        ):
            source = inspect.getsource(function)
            with self.subTest(function=function.__name__):
                capture = source.index(
                    f"{target_name} = _kubernetes_target_identity"
                )
                snapshot = source.index(
                    f"_bound_kube_env(root, {target_name}, source_commit)"
                )
                final = source.rindex(
                    "_require_same_kubernetes_target"
                )
                self.assertLess(capture, snapshot)
                self.assertLess(snapshot, final)
                self.assertIn(
                    f"_stable_json_sha256({target_name})",
                    source,
                )

    def test_toolchain_authority_is_source_commit_bound(self) -> None:
        source = inspect.getsource(runtime.toolchain)
        self.assertIn("_git_blob_bytes(", source)
        self.assertIn("TOOLCHAIN_LOCK_PATH", source)
        self.assertIn("_source_bound_bootstrap_tools(commit)", source)
        self.assertIn("lock_bytes=lock_bytes", source)
        self.assertIn("_open_verified_file(", source)
        self.assertIn("_create_sealed_snapshot_fd(", source)
        self.assertIn('f"/proc/self/fd/{snapshot_fd}"', source)
        self.assertIn('"source_commit": commit', source)
        self.assertNotIn("bootstrap_tools.", source)

        helper_source = inspect.getsource(
            runtime._source_bound_bootstrap_tools
        )
        self.assertIn("_source_commit_python_module(", helper_source)
        self.assertIn("BOOTSTRAP_TOOLS_HELPER", helper_source)

        bound_source = inspect.getsource(runtime._bound_kube_env)
        self.assertIn(
            "toolchain(root, source_commit)",
            bound_source,
        )
        self.assertIn(
            "_BOUND_SOURCE_COMMIT.set(source_commit)",
            bound_source,
        )

    def test_status_uses_one_application_pod_snapshot_for_contracts(
        self,
    ) -> None:
        source = inspect.getsource(runtime.status)
        self.assertEqual(
            source.count(
                '["-n", APP_NAMESPACE, "get", "pods"]'
            ),
            1,
        )
        snapshot = source.index(
            "application_pod_items = _kubectl_json("
        )
        api_partition = source.index(
            "api_pods = _pods_matching_labels(",
            snapshot,
        )
        web_partition = source.index(
            "web_pods = _pods_matching_labels(",
            snapshot,
        )
        migration_partition = source.index(
            "migration_pods = _pods_matching_labels(",
            snapshot,
        )
        workload_check = source.index(
            "_require_live_application_workloads(",
            snapshot,
        )
        inventory_check = source.index(
            "_require_exact_application_pod_inventory(",
            snapshot,
        )
        self.assertLess(snapshot, api_partition)
        self.assertLess(snapshot, web_partition)
        self.assertLess(snapshot, migration_partition)
        self.assertLess(api_partition, workload_check)
        self.assertLess(web_partition, workload_check)
        self.assertLess(migration_partition, workload_check)
        self.assertLess(workload_check, inventory_check)

    def test_controller_pod_contract_rejects_ephemeral_debug_containers(self) -> None:
        digest = "sha256:" + "a" * 64
        image = "example.invalid/controller@" + digest
        expected_images = {
            "containers": {"manager": image},
            "init_containers": {},
        }
        pod = {
            "metadata": {
                "name": "controller-0",
                "namespace": "kube-system",
                "labels": {"k8s-app": "controller"},
            },
            "spec": {
                "containers": [{"name": "manager", "image": image}],
                "initContainers": [],
            },
            "status": {
                "phase": "Running",
                "conditions": [{"type": "Ready", "status": "True"}],
                "containerStatuses": [
                    {
                        "name": "manager",
                        "ready": True,
                        "state": {"running": {"startedAt": "2026-09-28T00:00:00Z"}},
                        "imageID": "containerd://" + digest,
                    }
                ],
                "initContainerStatuses": [],
            },
        }

        def validate(candidate: dict) -> dict:
            return runtime._require_running_pod_image_contract(
                [candidate],
                namespace="kube-system",
                workload="controller",
                expected_replicas=1,
                expected_images=expected_images,
                required_labels={"k8s-app": "controller"},
                context="controller Pod",
            )

        self.assertTrue(validate(pod)["images_canonical"])

        debugged_spec = json.loads(json.dumps(pod))
        debugged_spec["spec"]["ephemeralContainers"] = [
            {
                "name": "debugger",
                "image": "example.invalid/debug@sha256:" + "d" * 64,
                "command": ["sh"],
            }
        ]
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB,
            "ephemeral containers are forbidden",
        ):
            validate(debugged_spec)

        debugged_status = json.loads(json.dumps(pod))
        debugged_status["status"]["ephemeralContainerStatuses"] = [
            {"name": "debugger"}
        ]
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB,
            "ephemeral container statuses are forbidden",
        ):
            validate(debugged_status)

        for function in (
            runtime._require_live_cilium_contract,
            runtime._require_live_flux_controller_contract,
        ):
            self.assertIn(
                "_require_running_pod_image_contract",
                inspect.getsource(function),
            )

    def test_same_kubernetes_target_rejects_identity_drift(self) -> None:
        expected = {
            "vm_ip": "192.168.122.10",
            "kubeconfig_sha256": "a" * 64,
            "server": "https://192.168.122.10:6443",
        }
        with mock.patch.object(
            runtime,
            "_kubernetes_target_identity",
            return_value=json.loads(json.dumps(expected)),
        ):
            self.assertEqual(
                runtime._require_same_kubernetes_target(
                    Path("/tmp/unused"),
                    "b" * 40,
                    expected,
                    "test recovery boundary",
                ),
                expected,
            )

        drifted = json.loads(json.dumps(expected))
        drifted["vm_ip"] = "192.168.122.99"
        with (
            mock.patch.object(
                runtime,
                "_kubernetes_target_identity",
                return_value=drifted,
            ),
            self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "Kubernetes target identity changed",
            ),
        ):
            runtime._require_same_kubernetes_target(
                Path("/tmp/unused"),
                "b" * 40,
                expected,
                "test recovery boundary",
            )

    def test_gateway_base_url_is_bound_to_verified_vm_target(self) -> None:
        commit = "a" * 40
        target = {
            "vm_ip": "192.168.122.10",
            "kubeconfig_sha256": "b" * 64,
            "server": "https://192.168.122.10:6443",
        }
        healthy = {
            "status": {
                "addresses": [
                    {
                        "type": "IPAddress",
                        "value": "192.168.122.10",
                    }
                ]
            }
        }
        with (
            mock.patch.object(
                runtime,
                "_kubernetes_target_identity",
                return_value=target,
            ),
            mock.patch.object(
                runtime,
                "_kubectl_json",
                return_value=healthy,
            ),
        ):
            self.assertEqual(
                runtime._gateway_base_url(Path("/tmp/unused"), commit),
                "http://192.168.122.10",
            )

        external = json.loads(json.dumps(healthy))
        external["status"]["addresses"][0]["value"] = "203.0.113.80"
        with (
            mock.patch.object(
                runtime,
                "_kubernetes_target_identity",
                return_value=target,
            ),
            mock.patch.object(
                runtime,
                "_kubectl_json",
                return_value=external,
            ),
            self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "not bound to the verified VM target",
            ),
        ):
            runtime._gateway_base_url(Path("/tmp/unused"), commit)

    def test_recovery_revalidates_target_around_destructive_boundaries(self) -> None:
        source = inspect.getsource(runtime.recovery_proof)
        bound_context = source.index(
            "with _bound_kube_env(root, recovery_target, source_commit):"
        )
        storage_capture = source.index(
            "_git_blob_bytes(source_commit, storage_path)"
        )
        self.assertNotIn(
            '(CLUSTER / "data/storage.yaml").read_text',
            source,
        )
        self.assertIn('"storage_manifest_sha256"', source)
        self.assertGreaterEqual(
            source.count("_require_same_kubernetes_target"),
            12,
        )
        delete_pvc = source.index('"delete", "pvc"')
        self.assertLess(storage_capture, bound_context)
        self.assertLess(bound_context, delete_pvc)
        self.assertNotEqual(
            source.rfind(
                "_require_same_kubernetes_target",
                0,
                delete_pvc,
            ),
            -1,
        )
        self.assertNotEqual(
            source.find(
                "_require_same_kubernetes_target",
                delete_pvc,
            ),
            -1,
        )
        self.assertIn(
            "\n        except Exception:\n",
            source,
        )
        failure_cleanup = source.index("except Exception:")
        cleanup_guard = source.index(
            "_require_same_kubernetes_target",
            failure_cleanup,
        )
        cleanup_suspend = source.index(
            "_flux_suspend",
            failure_cleanup,
        )
        self.assertLess(cleanup_guard, cleanup_suspend)
        self.assertIn("target_safe_for_cleanup", source)
        self.assertIn('"kubernetes_target_sha256"', source)

    def test_status_revalidates_target_before_success_receipt(self) -> None:
        source = inspect.getsource(runtime.status)
        target_capture = source.index(
            "status_target = _kubernetes_target_identity"
        )
        snapshot = source.index(
            "with _bound_kube_env(root, status_target, source_commit):"
        )
        k3s_runtime = source.index("_require_live_k3s_runtime")
        final_kubernetes_readback = source.index(
            "_require_live_runtime_contract"
        )
        final_target_check = source.rindex(
            "_require_same_kubernetes_target"
        )
        result_start = source.index("result = {")
        receipt_write = source.index("atomic_json(receipt_path, result)")
        self.assertLess(target_capture, snapshot)
        self.assertLess(snapshot, k3s_runtime)
        self.assertLess(k3s_runtime, final_kubernetes_readback)
        self.assertLess(final_kubernetes_readback, final_target_check)
        self.assertLess(final_target_check, result_start)
        self.assertLess(result_start, receipt_write)
        self.assertIn('"kubernetes_target_sha256"', source)

    def test_flux_source_and_kustomization_reject_termination(self) -> None:
        commit = "a" * 40
        source_spec = {
            "url": "https://example.invalid/commonthing.git",
            "interval": "1m",
        }
        source = {
            "metadata": {
                "generation": 1,
                "deletionTimestamp": "2026-09-28T09:00:00Z",
            },
            "spec": json.loads(json.dumps(source_spec)),
            "status": {
                "conditions": [
                    {
                        "type": "Ready",
                        "status": "True",
                        "observedGeneration": 1,
                    }
                ],
                "artifact": {"revision": f"sha1:{commit}"},
            },
        }
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB, "pending deletion"
        ):
            runtime._require_flux_source_revision(
                source, commit, source_spec
            )

        expected_specs = {
            name: {
                "interval": "1m",
                "path": f"./{name}",
            }
            for name in runtime.EXPECTED_FLUX_KUSTOMIZATIONS
        }
        items = []
        for name, spec in expected_specs.items():
            items.append(
                {
                    "metadata": {
                        "name": name,
                        "generation": 1,
                    },
                    "spec": json.loads(json.dumps(spec)),
                    "status": {
                        "conditions": [
                            {
                                "type": "Ready",
                                "status": "True",
                                "observedGeneration": 1,
                            }
                        ],
                        "lastAppliedRevision": f"sha1:{commit}",
                    },
                }
            )
        items[0]["metadata"]["deletionTimestamp"] = (
            "2026-09-28T09:00:00Z"
        )
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB, "pending deletion"
        ):
            runtime._require_exact_flux_revision_ready(
                items, commit, expected_specs
            )

    def test_t048_api_binding_uses_complete_application_contract(self) -> None:
        commit = "a" * 40
        labels = {"app.kubernetes.io/name": "weltgewebe-api"}
        pod_spec = {
            "serviceAccountName": "weltgewebe-api",
            "automountServiceAccountToken": False,
            "volumes": [{"name": "tmp", "emptyDir": {}}],
            "containers": [
                {
                    "name": "api",
                    "image": "example.invalid/api@sha256:" + "a" * 64,
                    "args": ["serve"],
                    "env": [{"name": "MODE", "value": "experiment-b"}],
                    "securityContext": {
                        "allowPrivilegeEscalation": False
                    },
                    "volumeMounts": [
                        {"name": "tmp", "mountPath": "/tmp"}
                    ],
                }
            ],
        }
        deployment = {
            "metadata": {
                "name": "weltgewebe-api",
                "namespace": runtime.APP_NAMESPACE,
            },
            "spec": {
                "replicas": 1,
                "revisionHistoryLimit": 10,
                "strategy": None,
                "selector": {"matchLabels": labels},
                "template": {
                    "metadata": {
                        "labels": labels,
                        "annotations": {},
                    },
                    "spec": json.loads(json.dumps(pod_spec)),
                },
            },
        }
        pod = {
            "metadata": {
                "name": "weltgewebe-api-0",
                "namespace": runtime.APP_NAMESPACE,
                "labels": labels,
                "annotations": {},
            },
            "spec": json.loads(json.dumps(pod_spec)),
        }
        contract = {
            "replicas": 1,
            "revisionHistoryLimit": 10,
            "strategy": None,
            "paused": False,
            "minReadySeconds": 0,
            "progressDeadlineSeconds": 600,
            "selector_labels": labels,
            "template_labels": labels,
            "template_annotations": {},
            "pod_spec": runtime._application_pod_spec_projection(
                pod_spec, "expected API"
            ),
        }
        rendered = {
            "weltgewebe-api": {
                "contract": contract,
                "contract_sha256": runtime._stable_json_sha256(contract),
                "pod_contract_sha256": runtime._stable_json_sha256(
                    contract["pod_spec"]
                ),
            },
            "weltgewebe-web": {
                "contract": {},
                "contract_sha256": "f" * 64,
                "pod_contract_sha256": "e" * 64,
            },
        }
        image_readback = {
            "runtime_image_ids_sha256": "d" * 64,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "receipts").mkdir()
            runtime.atomic_json(
                root / "receipts/release.json",
                {
                    "schema_version": 1,
                    "status": "applied",
                    "source_commit": commit,
                    "api_digest": "sha256:" + "b" * 64,
                    "web_digest": "sha256:" + "c" * 64,
                },
            )
            with (
                mock.patch.object(
                    runtime,
                    "_require_t048_api_release_binding",
                    return_value=(
                        "weltgewebe-api-0",
                        pod,
                        image_readback,
                    ),
                ),
                mock.patch.object(
                    runtime,
                    "_rendered_application_workload_contract",
                    return_value=rendered,
                ),
                mock.patch.object(
                    runtime,
                    "_kubectl_json",
                    return_value=deployment,
                ),
            ):
                _, _, result = (
                    runtime._require_t048_api_runtime_binding(
                        root, commit
                    )
                )
            self.assertTrue(result["canonical"])
            self.assertEqual(
                result["contract_sha256"],
                rendered["weltgewebe-api"]["contract_sha256"],
            )

            deployment_drift = json.loads(json.dumps(deployment))
            deployment_drift["spec"]["template"]["spec"][
                "containers"
            ][0]["args"] = ["shadow"]
            with (
                mock.patch.object(
                    runtime,
                    "_require_t048_api_release_binding",
                    return_value=(
                        "weltgewebe-api-0",
                        pod,
                        image_readback,
                    ),
                ),
                mock.patch.object(
                    runtime,
                    "_rendered_application_workload_contract",
                    return_value=rendered,
                ),
                mock.patch.object(
                    runtime,
                    "_kubectl_json",
                    return_value=deployment_drift,
                ),
                self.assertRaisesRegex(
                    runtime.RuntimeErrorEB,
                    "Deployment contract drifted",
                ),
            ):
                runtime._require_t048_api_runtime_binding(
                    root, commit
                )

            pod_drift = json.loads(json.dumps(pod))
            pod_drift["spec"]["containers"][0]["env"] = [
                {"name": "MODE", "value": "shadow"}
            ]
            with (
                mock.patch.object(
                    runtime,
                    "_require_t048_api_release_binding",
                    return_value=(
                        "weltgewebe-api-0",
                        pod_drift,
                        image_readback,
                    ),
                ),
                mock.patch.object(
                    runtime,
                    "_rendered_application_workload_contract",
                    return_value=rendered,
                ),
                mock.patch.object(
                    runtime,
                    "_kubectl_json",
                    return_value=deployment,
                ),
                self.assertRaisesRegex(
                    runtime.RuntimeErrorEB,
                    "Pod contract drifted",
                ),
            ):
                runtime._require_t048_api_runtime_binding(
                    root, commit
                )

        source = inspect.getsource(runtime.t048_load_proof)
        self.assertEqual(
            source.count("_require_t048_api_runtime_binding"), 3
        )
        self.assertIn("api_contract_sha256", source)
        self.assertIn("api_pod_contract_sha256", source)

    def test_application_pod_spec_projection_binds_behavior_fields(self) -> None:
        base = {
            "containers": [
                {
                    "name": "api",
                    "image": "example.invalid/api@sha256:" + "a" * 64,
                }
            ]
        }
        expected = runtime._application_pod_spec_projection(
            base, "canonical Pod"
        )
        drifts = {
            "shareProcessNamespace": True,
            "dnsPolicy": "None",
            "dnsConfig": {"nameservers": ["192.0.2.53"]},
            "runtimeClassName": "sandboxed",
            "enableServiceLinks": False,
            "hostPID": True,
        }
        for field, value in drifts.items():
            with self.subTest(field=field):
                changed = json.loads(json.dumps(base))
                changed[field] = value
                self.assertNotEqual(
                    expected,
                    runtime._application_pod_spec_projection(
                        changed, f"drifted {field}"
                    ),
                )

        ephemeral = json.loads(json.dumps(base))
        ephemeral["ephemeralContainers"] = [
            {
                "name": "debugger",
                "image": "example.invalid/debug@sha256:" + "d" * 64,
                "command": ["sh"],
            }
        ]
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB,
            "ephemeral containers are forbidden",
        ):
            runtime._application_pod_spec_projection(
                ephemeral, "debugged Pod"
            )

        live_defaults = json.loads(json.dumps(base))
        live_defaults["tolerations"] = [
            {
                "effect": "NoExecute",
                "key": "node.kubernetes.io/not-ready",
                "operator": "Exists",
                "tolerationSeconds": 300,
            },
            {
                "effect": "NoExecute",
                "key": "node.kubernetes.io/unreachable",
                "operator": "Exists",
                "tolerationSeconds": 300,
            },
        ]
        self.assertEqual(
            expected,
            runtime._application_pod_spec_projection(
                live_defaults, "live defaulted Pod"
            ),
        )

    def test_migration_job_and_completed_pod_are_fully_bound(self) -> None:
        api_digest = "sha256:" + "b" * 64
        image = "ghcr.io/heimgewebe/commonthing-api@" + api_digest
        template_labels = {
            "app.kubernetes.io/name": runtime.MIGRATION_JOB_NAME,
            "app.kubernetes.io/component": "database-migration",
        }
        pod_spec = {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "containers": [
                {
                    "name": "migration",
                    "image": image,
                    "imagePullPolicy": "IfNotPresent",
                    "env": [{"name": "MODE", "value": "canonical"}],
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                    },
                }
            ],
        }
        rendered_job = {
            "metadata": {
                "name": runtime.MIGRATION_JOB_NAME,
                "namespace": runtime.APP_NAMESPACE,
            },
            "spec": {
                "backoffLimit": 4,
                "activeDeadlineSeconds": 480,
                "template": {
                    "metadata": {"labels": template_labels},
                    "spec": pod_spec,
                },
            },
        }
        expected_contract = runtime._migration_job_contract(
            rendered_job, "rendered migration"
        )
        expected = {
            "contract": expected_contract,
            "contract_sha256": runtime._stable_json_sha256(
                expected_contract
            ),
            "pod_contract_sha256": runtime._stable_json_sha256(
                expected_contract["pod_spec"]
            ),
        }
        live_job = json.loads(json.dumps(rendered_job))
        live_job["metadata"]["uid"] = "job-uid-1"
        live_job["spec"]["parallelism"] = 1
        live_job["spec"]["completions"] = 1
        live_job["spec"]["completionMode"] = "NonIndexed"
        live_job["spec"]["selector"] = {
            "matchLabels": {
                "batch.kubernetes.io/controller-uid": "job-uid-1"
            }
        }
        live_job["spec"]["template"]["metadata"]["labels"].update(
            {
                "batch.kubernetes.io/controller-uid": "job-uid-1",
                "batch.kubernetes.io/job-name": runtime.MIGRATION_JOB_NAME,
                "controller-uid": "job-uid-1",
                "job-name": runtime.MIGRATION_JOB_NAME,
            }
        )
        live_job["status"] = {
            "succeeded": 1,
            "conditions": [{"type": "Complete", "status": "True"}],
        }

        live_pod_spec = json.loads(json.dumps(pod_spec))
        live_pod_spec.update(
            {
                "dnsPolicy": "ClusterFirst",
                "schedulerName": "default-scheduler",
                "enableServiceLinks": True,
                "preemptionPolicy": "PreemptLowerPriority",
                "priority": 0,
                "tolerations": [
                    {
                        "effect": "NoExecute",
                        "key": "node.kubernetes.io/not-ready",
                        "operator": "Exists",
                        "tolerationSeconds": 300,
                    },
                    {
                        "effect": "NoExecute",
                        "key": "node.kubernetes.io/unreachable",
                        "operator": "Exists",
                        "tolerationSeconds": 300,
                    },
                ],
            }
        )
        live_pod = {
            "metadata": {
                "name": "migration-pod-1",
                "namespace": runtime.APP_NAMESPACE,
                "labels": {
                    **template_labels,
                    "batch.kubernetes.io/controller-uid": "job-uid-1",
                    "batch.kubernetes.io/job-name": runtime.MIGRATION_JOB_NAME,
                    "controller-uid": "job-uid-1",
                    "job-name": runtime.MIGRATION_JOB_NAME,
                },
                "ownerReferences": [
                    {
                        "apiVersion": "batch/v1",
                        "kind": "Job",
                        "name": runtime.MIGRATION_JOB_NAME,
                        "uid": "job-uid-1",
                        "controller": True,
                    }
                ],
            },
            "spec": live_pod_spec,
            "status": {
                "phase": "Succeeded",
                "containerStatuses": [
                    {
                        "name": "migration",
                        "imageID": "containerd://" + api_digest,
                        "state": {"terminated": {"exitCode": 0}},
                    }
                ],
            },
        }
        with mock.patch.object(
            runtime,
            "_rendered_migration_job_contract",
            return_value=expected,
        ):
            observed = runtime._require_migration_job_runtime_contract(
                Path("/tmp"),
                live_job,
                [live_pod],
                api_digest,
            )
        self.assertTrue(observed["canonical"])
        self.assertEqual(observed["succeeded_pods"], 1)

        job_drift = json.loads(json.dumps(live_job))
        job_drift["spec"]["template"]["spec"]["containers"][0][
            "args"
        ] = ["shadow"]
        with (
            mock.patch.object(
                runtime,
                "_rendered_migration_job_contract",
                return_value=expected,
            ),
            self.assertRaisesRegex(
                runtime.RuntimeErrorEB, "migration Job contract drifted"
            ),
        ):
            runtime._require_migration_job_runtime_contract(
                Path("/tmp"),
                job_drift,
                [live_pod],
                api_digest,
            )

        pod_drift = json.loads(json.dumps(live_pod))
        pod_drift["spec"]["containers"].append(
            {
                "name": "sidecar",
                "image": image,
                "imagePullPolicy": "IfNotPresent",
            }
        )
        with (
            mock.patch.object(
                runtime,
                "_rendered_migration_job_contract",
                return_value=expected,
            ),
            self.assertRaisesRegex(
                runtime.RuntimeErrorEB, "migration Pod contract drifted"
            ),
        ):
            runtime._require_migration_job_runtime_contract(
                Path("/tmp"),
                live_job,
                [pod_drift],
                api_digest,
            )

        portability = inspect.getsource(runtime.portability_report)
        self.assertIn(
            "complete migration Job/Pod contract",
            portability,
        )

    def test_t048_port_forward_targets_verified_api_pod(self) -> None:
        source_commit = "a" * 40
        pod = {
            "metadata": {
                "name": "weltgewebe-api-verified",
                "uid": "pod-uid",
            },
            "status": {
                "containerStatuses": [
                    {
                        "name": "api",
                        "containerID": "containerd://" + "b" * 64,
                    }
                ]
            },
        }
        api_binding = {
            "runtime_image_ids_sha256": "c" * 64,
            "contract_sha256": "d" * 64,
            "pod_contract_sha256": "e" * 64,
        }
        expected_binding = runtime._t048_api_runtime_binding_identity(
            "weltgewebe-api-verified",
            pod,
            api_binding,
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            process = mock.Mock()
            process.poll.return_value = None
            with (
                mock.patch.object(
                    runtime, "_reserve_loopback_port", return_value=43123
                ),
                mock.patch.object(
                    runtime,
                    "toolchain",
                    return_value={"tools": {"kubectl": "/verified/kubectl"}},
                ),
                mock.patch.object(runtime, "kube_env", return_value={}),
                mock.patch.object(
                    runtime.subprocess,
                    "Popen",
                    return_value=process,
                ) as popen,
                mock.patch.object(runtime, "_wait_http_200") as wait_http,
                mock.patch.object(
                    runtime,
                    "_require_t048_api_runtime_binding",
                    return_value=(
                        "weltgewebe-api-verified",
                        pod,
                        api_binding,
                    ),
                ) as live_binding,
            ):
                returned, port, stdout, stderr = (
                    runtime._start_api_port_forward(
                        root,
                        source_commit,
                        expected_binding,
                    )
                )
            try:
                self.assertIs(returned, process)
                self.assertEqual(port, 43123)
                argv = popen.call_args.args[0]
                self.assertIn("pod/weltgewebe-api-verified", argv)
                self.assertNotIn("service/weltgewebe-api", argv)
                wait_http.assert_called_once_with(
                    "http://127.0.0.1:43123/health/live",
                    process,
                )
                live_binding.assert_called_once_with(
                    root,
                    source_commit,
                )
            finally:
                stdout.close()
                stderr.close()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            drifted_pod = json.loads(json.dumps(pod))
            drifted_pod["metadata"]["uid"] = "replacement-pod-uid"
            process = mock.Mock()
            process.poll.return_value = None
            process.wait.return_value = -15
            with (
                mock.patch.object(
                    runtime, "_reserve_loopback_port", return_value=43124
                ),
                mock.patch.object(
                    runtime,
                    "toolchain",
                    return_value={"tools": {"kubectl": "/verified/kubectl"}},
                ),
                mock.patch.object(runtime, "kube_env", return_value={}),
                mock.patch.object(
                    runtime.subprocess,
                    "Popen",
                    return_value=process,
                ),
                mock.patch.object(runtime, "_wait_http_200"),
                mock.patch.object(
                    runtime,
                    "_require_t048_api_runtime_binding",
                    return_value=(
                        "weltgewebe-api-verified",
                        drifted_pod,
                        api_binding,
                    ),
                ),
                self.assertRaisesRegex(
                    runtime.RuntimeErrorEB,
                    "changed while establishing T048 port-forward",
                ),
            ):
                runtime._start_api_port_forward(
                    root,
                    source_commit,
                    expected_binding,
                )
            process.terminate.assert_called_once_with()
            process.wait.assert_called_once_with(timeout=10)

        t048_source = inspect.getsource(runtime.t048_load_proof)
        port_forward = t048_source.index("_start_api_port_forward(")
        bound_snapshot = t048_source.index(
            "bound_stack.enter_context(_bound_kube_env(root, target_binding_before, source_commit))"
        )
        self.assertLess(bound_snapshot, port_forward)
        self.assertIn(
            "api_runtime_binding_before",
            t048_source[port_forward : port_forward + 240],
        )
        self.assertIn(
            "api_runtime_binding_after != api_runtime_binding_before",
            t048_source,
        )

    def test_application_pod_runtime_identity_binds_uid_and_container_ids(
        self,
    ) -> None:
        pod = {
            "metadata": {
                "name": "weltgewebe-api-serving",
                "uid": "pod-uid-serving",
            },
            "status": {
                "podIP": "10.42.0.12",
                "containerStatuses": [
                    {
                        "name": "api",
                        "containerID": "containerd://" + "a" * 64,
                    },
                    {
                        "name": "search-worker",
                        "containerID": "containerd://" + "b" * 64,
                    },
                    {
                        "name": "ollama",
                        "containerID": "containerd://" + "c" * 64,
                    },
                ]
            },
        }
        observed = runtime._application_pod_runtime_identity(
            pod,
            {"api", "search-worker", "ollama"},
            "functional serving test",
        )
        self.assertEqual(observed["pod_name"], "weltgewebe-api-serving")
        self.assertEqual(observed["pod_uid"], "pod-uid-serving")
        self.assertEqual(observed["pod_ip"], "10.42.0.12")
        self.assertEqual(
            observed["container_ids"],
            {
                "api": "containerd://" + "a" * 64,
                "ollama": "containerd://" + "c" * 64,
                "search-worker": "containerd://" + "b" * 64,
            },
        )

        drifted = json.loads(json.dumps(pod))
        drifted["status"]["containerStatuses"][0]["containerID"] = (
            "containerd://" + "d" * 63
        )
        with self.assertRaisesRegex(
            runtime.RuntimeErrorEB,
            "container ID is invalid",
        ):
            runtime._application_pod_runtime_identity(
                drifted,
                {"api", "search-worker", "ollama"},
                "functional serving test",
            )

        binding_source = inspect.getsource(
            runtime._functional_serving_runtime_binding
        )
        for required in (
            "_require_live_application_workloads(",
            "_require_running_pod_images(",
            "_require_live_application_services(",
            "_application_service_endpoint_binding(",
            "_require_gateway_ready(",
            "_require_httproute_ready(",
            "_application_pod_runtime_identity(",
        ):
            self.assertIn(required, binding_source)


    def test_endpoint_slice_collection_uses_raw_server_list_metadata(self) -> None:
        raw = {
            "metadata": {"resourceVersion": "5073"},
            "items": [],
        }
        completed = subprocess.CompletedProcess(
            ["kubectl"],
            0,
            stdout=json.dumps(raw),
            stderr="",
        )
        with mock.patch.object(
            runtime,
            "_kubectl",
            return_value=completed,
        ) as kubectl:
            observed = runtime._endpoint_slice_collection_json(
                Path("/tmp"),
                runtime.APP_NAMESPACE,
                "weltgewebe-api",
            )
        self.assertEqual(observed, raw)
        argv = kubectl.call_args.args[1]
        self.assertEqual(argv[:2], ["get", "--raw"])
        url = runtime.urllib.parse.urlsplit(argv[2])
        self.assertEqual(
            url.path,
            (
                "/apis/discovery.k8s.io/v1/namespaces/"
                f"{runtime.APP_NAMESPACE}/endpointslices"
            ),
        )
        self.assertEqual(
            runtime.urllib.parse.parse_qs(url.query),
            {
                "labelSelector": [
                    "kubernetes.io/service-name=weltgewebe-api"
                ]
            },
        )

    def test_httproute_collection_uses_raw_server_list_metadata(self) -> None:
        raw = {
            "metadata": {"resourceVersion": "5074"},
            "items": [],
        }
        completed = subprocess.CompletedProcess(
            ["kubectl"],
            0,
            stdout=json.dumps(raw),
            stderr="",
        )
        with mock.patch.object(
            runtime,
            "_kubectl",
            return_value=completed,
        ) as kubectl:
            observed = runtime._httproute_collection_json(Path("/tmp"))
        self.assertEqual(observed, raw)
        argv = kubectl.call_args.args[1]
        self.assertEqual(argv[:2], ["get", "--raw"])
        url = runtime.urllib.parse.urlsplit(argv[2])
        self.assertEqual(
            url.path,
            (
                "/apis/gateway.networking.k8s.io/v1/namespaces/"
                f"{runtime.APP_NAMESPACE}/httproutes"
            ),
        )
        self.assertEqual(url.query, "")

    def test_application_service_endpoints_bind_to_validated_pods(self) -> None:
        pod_identities = {
            "weltgewebe-api-serving": {
                "pod_name": "weltgewebe-api-serving",
                "pod_uid": "pod-uid-serving",
                "pod_ip": "10.42.0.12",
                "container_ids": {
                    "api": "containerd://" + "a" * 64,
                },
            }
        }
        endpoint_slice = {
            "metadata": {
                "name": "weltgewebe-api-slice",
                "namespace": runtime.APP_NAMESPACE,
                "uid": "slice-uid-serving",
                "resourceVersion": "12344",
                "labels": {
                    "kubernetes.io/service-name": "weltgewebe-api",
                },
            },
            "addressType": "IPv4",
            "endpoints": [
                {
                    "addresses": ["10.42.0.12"],
                    "conditions": {
                        "ready": True,
                        "serving": True,
                        "terminating": False,
                    },
                    "targetRef": {
                        "kind": "Pod",
                        "namespace": runtime.APP_NAMESPACE,
                        "name": "weltgewebe-api-serving",
                        "uid": "pod-uid-serving",
                    },
                }
            ],
        }
        with mock.patch.object(
            runtime,
            "_endpoint_slice_collection_json",
            return_value={
                "metadata": {"resourceVersion": "12345"},
                "items": [endpoint_slice],
            },
        ) as readback:
            observed = runtime._application_service_endpoint_binding(
                Path("/tmp"),
                "weltgewebe-api",
                pod_identities,
            )
        self.assertEqual(
            observed["pods"],
            {
                "weltgewebe-api-serving": {
                    "pod_uid": "pod-uid-serving",
                    "address": "10.42.0.12",
                }
            },
        )
        self.assertEqual(
            observed["sha256"],
            runtime._stable_json_sha256(observed["pods"]),
        )
        self.assertEqual(observed["resource_version"], "12345")
        self.assertEqual(
            observed["object_revisions"],
            {
                "weltgewebe-api-slice": {
                    "uid": "slice-uid-serving",
                    "resource_version": "12344",
                }
            },
        )
        readback.assert_called_once_with(
            Path("/tmp"),
            runtime.APP_NAMESPACE,
            "weltgewebe-api",
        )

        drifted = {
            "metadata": {"resourceVersion": "12346"},
            "items": [json.loads(json.dumps(endpoint_slice))],
        }
        drifted["items"][0]["endpoints"].append(
            {
                "addresses": ["10.42.0.99"],
                "conditions": {"ready": True},
                "targetRef": {
                    "kind": "Pod",
                    "namespace": runtime.APP_NAMESPACE,
                    "name": "unvalidated-api-pod",
                    "uid": "unvalidated-pod-uid",
                },
            }
        )
        with (
            mock.patch.object(
                runtime,
                "_endpoint_slice_collection_json",
                return_value=drifted,
            ),
            self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "endpoint target is invalid",
            ),
        ):
            runtime._application_service_endpoint_binding(
                Path("/tmp"),
                "weltgewebe-api",
                pod_identities,
            )


    def test_functional_serving_semantic_binding_ignores_watch_cursor(
        self,
    ) -> None:
        before = {
            "services": {
                "weltgewebe-api": {
                    "endpoints": {
                        "pods": {
                            "api-1": {
                                "pod_uid": "api-uid",
                                "address": "10.42.0.12",
                            }
                        },
                        "sha256": "a" * 64,
                        "resource_version": "100",
                    }
                }
            },
            "gateway": {"ready": True},
            "httproute_inventory": {
                "resource_version": "200",
                "routes": {
                    "commonthing-experiment-b": {
                        "uid": "route-uid",
                        "resource_version": "201",
                    }
                },
            },
        }
        after = json.loads(json.dumps(before))
        after["services"]["weltgewebe-api"]["endpoints"][
            "resource_version"
        ] = "101"
        after["httproute_inventory"]["resource_version"] = "202"
        after["httproute_inventory"]["routes"][
            "commonthing-experiment-b"
        ]["resource_version"] = "203"

        semantic_before = (
            runtime._functional_serving_runtime_semantic_binding(before)
        )
        semantic_after = (
            runtime._functional_serving_runtime_semantic_binding(after)
        )
        self.assertEqual(semantic_before, semantic_after)
        self.assertNotIn(
            "resource_version",
            semantic_before["services"]["weltgewebe-api"]["endpoints"],
        )
        self.assertNotIn(
            "resource_version",
            semantic_before["httproute_inventory"],
        )
        self.assertNotIn(
            "resource_version",
            semantic_before["httproute_inventory"]["routes"][
                "commonthing-experiment-b"
            ],
        )

        after["services"]["weltgewebe-api"]["endpoints"]["sha256"] = "b" * 64
        self.assertNotEqual(
            semantic_before,
            runtime._functional_serving_runtime_semantic_binding(after),
        )

    def test_functional_dependency_replay_rejects_transient_changes(
        self,
    ) -> None:
        serving_runtime = {
            "services": {
                name: {
                    "endpoints": {
                        "resource_version": str(index + 100),
                    }
                }
                for index, name in enumerate(
                    ("weltgewebe-api", "weltgewebe-web")
                )
            },
            "gateway": {"resource_version": "300"},
            "httproute": {"resource_version": "400"},
            "httproute_inventory": {
                "resource_version": "450",
                "routes": {
                    "commonthing-experiment-b": {
                        "uid": "canonical-route-uid",
                        "resource_version": "400",
                    }
                },
            },
        }

        for mutation_index, subject in (
            (0, "weltgewebe-api"),
            (2, "Gateway"),
            (3, "HTTPRoute"),
        ):
            with self.subTest(subject=subject):
                results = []
                for index in range(4):
                    event_object = {
                        "metadata": {
                            "resourceVersion": str(500 + index),
                        }
                    }
                    if index == 3:
                        event_object = {
                            "metadata": {
                                "name": "rogue-route",
                                "namespace": runtime.APP_NAMESPACE,
                                "resourceVersion": str(500 + index),
                            },
                            "spec": {
                                "parentRefs": [
                                    {
                                        "name": "commonthing-experiment-b",
                                        "namespace": runtime.APP_NAMESPACE,
                                        "sectionName": "http",
                                    }
                                ]
                            },
                        }
                    event = (
                        json.dumps(
                            {
                                "type": "MODIFIED",
                                "object": event_object,
                            }
                        )
                        + "\n"
                        if index == mutation_index
                        else ""
                    )
                    results.append(
                        subprocess.CompletedProcess(
                            ["kubectl"],
                            0,
                            stdout=event,
                            stderr="",
                        )
                    )
                with (
                    mock.patch.object(
                        runtime,
                        "toolchain",
                        return_value={
                            "tools": {"kubectl": "/usr/bin/kubectl"}
                        },
                    ),
                    mock.patch.object(runtime, "kube_env", return_value={}),
                    mock.patch.object(
                        runtime,
                        "run",
                        side_effect=results,
                    ) as replay,
                    mock.patch.object(
                        runtime.subprocess,
                        "Popen",
                        side_effect=AssertionError(
                            "live watch processes are not part of the replay proof"
                        ),
                    ),
                    self.assertRaisesRegex(
                        runtime.RuntimeErrorEB,
                        "dependency changed during Gateway probes",
                    ),
                ):
                    with runtime._guard_functional_service_endpoints(
                        Path("/tmp"),
                        serving_runtime,
                    ):
                        pass

                self.assertEqual(
                    replay.call_count,
                    mutation_index + 1,
                )
                expected = (
                    (
                        "weltgewebe-api",
                        "100",
                        "labelSelector",
                        "kubernetes.io/service-name=weltgewebe-api",
                        "/apis/discovery.k8s.io/v1/namespaces/"
                        f"{runtime.APP_NAMESPACE}/endpointslices",
                    ),
                    (
                        "weltgewebe-web",
                        "101",
                        "labelSelector",
                        "kubernetes.io/service-name=weltgewebe-web",
                        "/apis/discovery.k8s.io/v1/namespaces/"
                        f"{runtime.APP_NAMESPACE}/endpointslices",
                    ),
                    (
                        "Gateway",
                        "300",
                        "fieldSelector",
                        "metadata.name=commonthing-experiment-b",
                        "/apis/gateway.networking.k8s.io/v1/namespaces/"
                        f"{runtime.APP_NAMESPACE}/gateways",
                    ),
                    (
                        "HTTPRoute",
                        "450",
                        None,
                        None,
                        "/apis/gateway.networking.k8s.io/v1/namespaces/"
                        f"{runtime.APP_NAMESPACE}/httproutes",
                    ),
                )
                for index, (
                    expected_subject,
                    resource_version,
                    selector_name,
                    selector_value,
                    expected_path,
                ) in enumerate(expected[: mutation_index + 1]):
                    argv = replay.call_args_list[index].args[0]
                    self.assertEqual(
                        argv[:3],
                        ["/usr/bin/kubectl", "get", "--raw"],
                        expected_subject,
                    )
                    watch_url = runtime.urllib.parse.urlsplit(argv[3])
                    self.assertEqual(
                        watch_url.path,
                        expected_path,
                        expected_subject,
                    )
                    expected_query = {
                        "watch": ["1"],
                        "resourceVersion": [resource_version],
                        "allowWatchBookmarks": ["true"],
                        "timeoutSeconds": ["2"],
                    }
                    if selector_name is not None:
                        self.assertIsNotNone(selector_value)
                        expected_query[selector_name] = [selector_value]
                    self.assertEqual(
                        runtime.urllib.parse.parse_qs(watch_url.query),
                        expected_query,
                        expected_subject,
                    )

    def test_functional_httproute_inventory_rejects_additional_gateway_attachment(
        self,
    ) -> None:
        parent_ref = {
            "name": "commonthing-experiment-b",
            "namespace": runtime.APP_NAMESPACE,
            "sectionName": "http",
        }
        canonical = {
            "metadata": {
                "name": "commonthing-experiment-b",
                "namespace": runtime.APP_NAMESPACE,
                "uid": "canonical-route-uid",
                "resourceVersion": "400",
            },
            "spec": {"parentRefs": [parent_ref]},
        }
        rogue = json.loads(json.dumps(canonical))
        rogue["metadata"]["name"] = "rogue-route"
        rogue["metadata"]["uid"] = "rogue-route-uid"
        rogue["metadata"]["resourceVersion"] = "401"

        with mock.patch.object(
            runtime,
            "_httproute_collection_json",
            return_value={
                "metadata": {"resourceVersion": "450"},
                "items": [canonical],
            },
        ):
            observed = runtime._gateway_httproute_attachment_binding(
                Path("/tmp"),
                "canonical-route-uid",
            )
        self.assertEqual(observed["resource_version"], "450")
        self.assertEqual(
            set(observed["routes"]),
            {"commonthing-experiment-b"},
        )

        with (
            mock.patch.object(
                runtime,
                "_httproute_collection_json",
                return_value={
                    "metadata": {"resourceVersion": "451"},
                    "items": [canonical, rogue],
                },
            ),
            self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "HTTPRoute attachment inventory drifted",
            ),
        ):
            runtime._gateway_httproute_attachment_binding(
                Path("/tmp"),
                "canonical-route-uid",
            )

    def test_functional_dependency_replay_watches_complete_httproute_inventory(
        self,
    ) -> None:
        serving_runtime = {
            "services": {
                name: {
                    "endpoints": {
                        "resource_version": str(index + 100),
                    }
                }
                for index, name in enumerate(
                    ("weltgewebe-api", "weltgewebe-web")
                )
            },
            "gateway": {"resource_version": "300"},
            "httproute": {"resource_version": "400"},
            "httproute_inventory": {
                "resource_version": "450",
                "routes": {
                    "commonthing-experiment-b": {
                        "uid": "canonical-route-uid",
                    }
                },
            },
        }
        result = subprocess.CompletedProcess(
            ["kubectl"],
            0,
            stdout="",
            stderr="",
        )
        with (
            mock.patch.object(
                runtime,
                "toolchain",
                return_value={"tools": {"kubectl": "/usr/bin/kubectl"}},
            ),
            mock.patch.object(runtime, "kube_env", return_value={}),
            mock.patch.object(
                runtime,
                "run",
                return_value=result,
            ) as replay,
        ):
            with runtime._guard_functional_service_endpoints(
                Path("/tmp"),
                serving_runtime,
            ):
                pass

        self.assertEqual(replay.call_count, 4)
        route_argv = replay.call_args_list[3].args[0]
        route_url = runtime.urllib.parse.urlsplit(route_argv[3])
        self.assertEqual(
            route_url.path,
            "/apis/gateway.networking.k8s.io/v1/namespaces/"
            f"{runtime.APP_NAMESPACE}/httproutes",
        )
        route_query = runtime.urllib.parse.parse_qs(route_url.query)
        self.assertEqual(route_query["resourceVersion"], ["450"])
        self.assertNotIn("fieldSelector", route_query)
        self.assertNotIn("labelSelector", route_query)

    def test_functional_routing_binding_carries_exact_resource_versions(
        self,
    ) -> None:
        self.assertEqual(
            runtime._kubernetes_object_revision(
                {
                    "metadata": {
                        "uid": "gateway-uid",
                        "resourceVersion": "300",
                    }
                },
                "Gateway",
            ),
            {
                "uid": "gateway-uid",
                "resource_version": "300",
            },
        )
        for payload in (
            {},
            {"metadata": {"uid": "", "resourceVersion": "300"}},
            {"metadata": {"uid": "gateway-uid", "resourceVersion": ""}},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(runtime.RuntimeErrorEB):
                    runtime._kubernetes_object_revision(
                        payload,
                        "Gateway",
                    )

        source = inspect.getsource(
            runtime._functional_serving_runtime_binding
        )
        self.assertIn(
            '"resource_version": gateway_revision["resource_version"]',
            source,
        )
        self.assertIn(
            '"resource_version": httproute_revision["resource_version"]',
            source,
        )
        self.assertIn(
            "_gateway_httproute_attachment_binding(",
            source,
        )
        self.assertIn(
            '"httproute_inventory": httproute_inventory',
            source,
        )

    def test_gateway_data_plane_validates_domain_nodes_cursor_body(
        self,
    ) -> None:
        source_commit = "a" * 40
        valid_node = {
            "id": "node-1",
            "kind": "knoten",
            "title": "Node 1",
            "created_at": "2026-10-01T00:00:00Z",
            "updated_at": "2026-10-01T00:00:00Z",
            "search_visibility": "public",
            "location": {"lon": 10.0, "lat": 53.5},
        }
        valid_page = {
            "limit": 1,
            "has_more": False,
            "next_cursor": None,
        }

        def responses(nodes_body: str):
            return [
                (200, "<html>ok</html>", 1),
                (200, json.dumps({"commit": source_commit}), 1),
                (200, json.dumps({"status": "ok"}), 1),
                (200, nodes_body, 1),
                (
                    200,
                    json.dumps(
                        {
                            "items": [valid_node],
                            "mode": "hybrid",
                            "generation_id": "experiment-b-t048",
                            "offset": 0,
                        }
                    ),
                    1,
                ),
                (
                    200,
                    json.dumps(
                        {
                            "authenticated": False,
                            "role": "gast",
                        }
                    ),
                    1,
                ),
            ]

        invalid_cases = (
            ("malformed", "{not-json"),
            (
                "empty-items",
                json.dumps({"items": [], "page": valid_page}),
            ),
            (
                "wrong-envelope",
                json.dumps([valid_node]),
            ),
            (
                "nan-longitude",
                json.dumps(
                    {
                        "items": [
                            {
                                **valid_node,
                                "location": {
                                    "lon": float("nan"),
                                    "lat": 53.5,
                                },
                            }
                        ],
                        "page": valid_page,
                    }
                ),
            ),
            (
                "infinite-latitude",
                json.dumps(
                    {
                        "items": [
                            {
                                **valid_node,
                                "location": {
                                    "lon": 10.0,
                                    "lat": float("inf"),
                                },
                            }
                        ],
                        "page": valid_page,
                    }
                ),
            ),
            (
                "longitude-out-of-range",
                json.dumps(
                    {
                        "items": [
                            {
                                **valid_node,
                                "location": {
                                    "lon": 180.1,
                                    "lat": 53.5,
                                },
                            }
                        ],
                        "page": valid_page,
                    }
                ),
            ),
            (
                "latitude-out-of-range",
                json.dumps(
                    {
                        "items": [
                            {
                                **valid_node,
                                "location": {
                                    "lon": 10.0,
                                    "lat": -90.1,
                                },
                            }
                        ],
                        "page": valid_page,
                    }
                ),
            ),
            (
                "boolean-limit",
                json.dumps(
                    {
                        "items": [valid_node],
                        "page": {
                            **valid_page,
                            "limit": True,
                        },
                    }
                ),
            ),
            (
                "floating-limit",
                json.dumps(
                    {
                        "items": [valid_node],
                        "page": {
                            **valid_page,
                            "limit": 1.0,
                        },
                    }
                ),
            ),
        )
        for case, nodes_body in invalid_cases:
            with (
                self.subTest(case=case),
                mock.patch.object(
                    runtime,
                    "_gateway_base_url",
                    return_value="http://192.0.2.23",
                ),
                mock.patch.object(
                    runtime,
                    "_http_read",
                    side_effect=responses(nodes_body),
                ),
                mock.patch.object(
                    runtime,
                    "_source_commit_config",
                    return_value={
                        "semantic_search": {
                            "generation_id": "experiment-b-t048"
                        }
                    },
                ),
                self.assertRaisesRegex(
                    runtime.RuntimeErrorEB,
                    "domain read failed through Gateway",
                ),
            ):
                runtime._gateway_data_plane_readback(
                    Path("/tmp"),
                    source_commit,
                )

        valid_body = json.dumps(
            {
                "items": [valid_node],
                "page": valid_page,
            }
        )
        with (
            mock.patch.object(
                runtime,
                "_gateway_base_url",
                return_value="http://192.0.2.23",
            ),
            mock.patch.object(
                runtime,
                "_http_read",
                side_effect=responses(valid_body),
            ),
            mock.patch.object(
                runtime,
                "_source_commit_config",
                return_value={
                    "semantic_search": {
                        "generation_id": "experiment-b-t048"
                    }
                },
            ),
        ):
            observed = runtime._gateway_data_plane_readback(
                Path("/tmp"),
                source_commit,
            )

        self.assertEqual(observed["checks"]["domain_nodes"]["items"], 1)
        self.assertEqual(
            observed["checks"]["domain_nodes"]["first_node_id"],
            "node-1",
        )
        self.assertIs(
            observed["checks"]["domain_nodes"]["has_more"],
            False,
        )

    def test_gateway_data_plane_validates_search_response_contract(
        self,
    ) -> None:
        source_commit = "a" * 40
        expected_generation = "experiment-b-t048"
        valid_node = {
            "id": "node-1",
            "kind": "knoten",
            "title": "Node 1",
            "created_at": "2026-10-01T00:00:00Z",
            "updated_at": "2026-10-01T00:00:00Z",
            "search_visibility": "public",
            "location": {"lon": 10.0, "lat": 53.5},
        }
        valid_domain_body = json.dumps(
            {
                "items": [valid_node],
                "page": {
                    "limit": 1,
                    "has_more": False,
                    "next_cursor": None,
                },
            }
        )

        def responses(search_body: str):
            return [
                (200, "<html>ok</html>", 1),
                (200, json.dumps({"commit": source_commit}), 1),
                (200, json.dumps({"status": "ok"}), 1),
                (200, valid_domain_body, 1),
                (200, search_body, 1),
                (
                    200,
                    json.dumps(
                        {
                            "authenticated": False,
                            "role": "gast",
                        }
                    ),
                    1,
                ),
            ]

        valid_search = {
            "items": [valid_node],
            "mode": "hybrid",
            "generation_id": expected_generation,
            "offset": 0,
        }
        invalid_cases = (
            (
                "wrong-generation",
                {
                    **valid_search,
                    "generation_id": "unexpected-generation",
                },
            ),
            (
                "missing-mode",
                {
                    key: value
                    for key, value in valid_search.items()
                    if key != "mode"
                },
            ),
            (
                "malformed-item",
                {
                    **valid_search,
                    "items": [{"id": "search-hit"}],
                },
            ),
            (
                "missing-search-visibility",
                {
                    **valid_search,
                    "items": [
                        {
                            key: value
                            for key, value in valid_node.items()
                            if key != "search_visibility"
                        }
                    ],
                },
            ),
        )
        for case, search_payload in invalid_cases:
            with (
                self.subTest(case=case),
                mock.patch.object(
                    runtime,
                    "_gateway_base_url",
                    return_value="http://192.0.2.23",
                ),
                mock.patch.object(
                    runtime,
                    "_http_read",
                    side_effect=responses(json.dumps(search_payload)),
                ),
                mock.patch.object(
                    runtime,
                    "_source_commit_config",
                    return_value={
                        "semantic_search": {
                            "generation_id": expected_generation
                        }
                    },
                ),
                self.assertRaisesRegex(
                    runtime.RuntimeErrorEB,
                    "search failed through Gateway",
                ),
            ):
                runtime._gateway_data_plane_readback(
                    Path("/tmp"),
                    source_commit,
                )

        with (
            mock.patch.object(
                runtime,
                "_gateway_base_url",
                return_value="http://192.0.2.23",
            ),
            mock.patch.object(
                runtime,
                "_http_read",
                side_effect=responses(json.dumps(valid_search)),
            ),
            mock.patch.object(
                runtime,
                "_source_commit_config",
                return_value={
                    "semantic_search": {
                        "generation_id": expected_generation
                    }
                },
            ),
        ):
            observed = runtime._gateway_data_plane_readback(
                Path("/tmp"),
                source_commit,
            )
        self.assertEqual(
            observed["checks"]["search"]["generation_id"],
            expected_generation,
        )
        self.assertEqual(observed["checks"]["search"]["items"], 1)

    def test_functional_readback_rejects_serving_runtime_drift(
        self,
    ) -> None:
        source_commit = "a" * 40
        target_receipt = {"kubeconfig_sha256": "b" * 64}
        target = (
            target_receipt,
            "192.0.2.23",
            "https://192.0.2.23:6443",
        )
        before = {
            "workloads": {
                "weltgewebe-api": {
                    "pods": {
                        "api-1": {
                            "pod_uid": "api-before",
                            "container_ids": {
                                "api": "containerd://" + "c" * 64,
                            },
                        }
                    }
                }
            }
        }
        after = json.loads(json.dumps(before))
        after["workloads"]["weltgewebe-api"]["pods"]["api-1"][
            "pod_uid"
        ] = "api-after"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bound_context = mock.MagicMock()
            bound_context.__enter__.return_value = None
            bound_context.__exit__.return_value = False
            endpoint_context = mock.MagicMock()
            endpoint_context.__enter__.return_value = None
            endpoint_context.__exit__.return_value = False
            with (
                mock.patch.object(
                    runtime,
                    "_begin_live_check_attempt",
                    return_value=(
                        root / "functional-readback.json",
                        root / "functional-readback-attempt.json",
                        1,
                    ),
                ),
                mock.patch.object(
                    runtime,
                    "_current_protected_main_commit",
                    return_value=source_commit,
                ),
                mock.patch.object(
                    runtime,
                    "_require_kubernetes_target_binding",
                    return_value=target,
                ),
                mock.patch.object(
                    runtime,
                    "_bound_kube_env",
                    return_value=bound_context,
                ),
                mock.patch.object(
                    runtime,
                    "_functional_serving_runtime_binding",
                    side_effect=[before, before, after],
                ) as serving_binding,
                mock.patch.object(
                    runtime,
                    "_guard_functional_service_endpoints",
                    return_value=endpoint_context,
                ),
                mock.patch.object(
                    runtime,
                    "_gateway_data_plane_readback",
                    return_value={
                        "gateway": "http://192.0.2.23",
                        "checks": {},
                    },
                ) as gateway_readback,
                mock.patch.object(
                    runtime,
                    "_jetstream_signature",
                ) as jetstream,
                self.assertRaisesRegex(
                    runtime.RuntimeErrorEB,
                    "application serving runtime changed during functional readback",
                ),
            ):
                runtime.functional_readback(root, source_commit)

            self.assertEqual(serving_binding.call_count, 3)
            gateway_readback.assert_called_once_with(root, source_commit)
            jetstream.assert_not_called()


    def test_functional_readback_binds_jetstream_to_exact_nats_runtime(
        self,
    ) -> None:
        source_commit = "a" * 40
        target = (
            {"kubeconfig_sha256": "b" * 64},
            "192.0.2.23",
            "https://192.0.2.23:6443",
        )
        serving = {"stable": True}
        nats_before = {
            "container_id": "containerd://" + "c" * 64,
            "contract_sha256": "d" * 64,
            "pod_contract_sha256": "e" * 64,
            "runtime_image_ids_sha256": "f" * 64,
        }
        nats_after = dict(nats_before)
        nats_after["container_id"] = "containerd://" + "1" * 64

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bound_context = mock.MagicMock()
            bound_context.__enter__.return_value = None
            bound_context.__exit__.return_value = False
            endpoint_context = mock.MagicMock()
            endpoint_context.__enter__.return_value = None
            endpoint_context.__exit__.return_value = False
            with (
                mock.patch.object(
                    runtime,
                    "_begin_live_check_attempt",
                    return_value=(
                        root / "functional-readback.json",
                        root / "functional-readback-attempt.json",
                        1,
                    ),
                ),
                mock.patch.object(
                    runtime,
                    "_current_protected_main_commit",
                    return_value=source_commit,
                ),
                mock.patch.object(
                    runtime,
                    "_require_kubernetes_target_binding",
                    return_value=target,
                ),
                mock.patch.object(
                    runtime,
                    "_bound_kube_env",
                    return_value=bound_context,
                ),
                mock.patch.object(
                    runtime,
                    "_functional_serving_runtime_binding",
                    side_effect=[serving, serving, serving],
                ),
                mock.patch.object(
                    runtime,
                    "_guard_functional_service_endpoints",
                    return_value=endpoint_context,
                ),
                mock.patch.object(
                    runtime,
                    "_gateway_data_plane_readback",
                    return_value={
                        "gateway": "http://192.0.2.23",
                        "checks": {},
                    },
                ),
                mock.patch.object(
                    runtime,
                    "_require_nats_runtime_binding",
                    side_effect=[nats_before, nats_after],
                ) as nats_binding,
                mock.patch.object(
                    runtime,
                    "_jetstream_signature",
                    return_value={"messages": 1},
                ) as jetstream,
                self.assertRaisesRegex(
                    runtime.RuntimeErrorEB,
                    "NATS runtime changed during functional readback",
                ),
            ):
                runtime.functional_readback(root, source_commit)

            self.assertEqual(nats_binding.call_count, 2)
            jetstream.assert_called_once_with(
                root,
                source_commit=source_commit,
                nats_binding=nats_before,
            )

    def test_t048_database_sampler_uses_bound_postgres_container(self) -> None:
        source = inspect.getsource(runtime._database_connection_count)
        self.assertIn("_run_bound_postgres_sql(", source)
        self.assertNotIn(
            "_psql(",
            source.replace("bound_psql(", ""),
        )
        sampler = inspect.getsource(runtime._sample_t048_load)
        self.assertIn("postgres_binding", sampler)
        self.assertIn("database_identity", sampler)


if __name__ == "__main__":
    unittest.main()
