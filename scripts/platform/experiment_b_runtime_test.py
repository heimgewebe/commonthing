from __future__ import annotations

import base64
import hashlib
import inspect
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

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
        image, workflow_sha = runtime._k6_image_binding()
        self.assertEqual(
            image,
            "grafana/k6@sha256:65c920dc067d5e2e00befbf982af6ad6ad0117034e8b1c65817c7975c52d4669",
        )
        self.assertEqual(
            workflow_sha,
            hashlib.sha256(runtime.K6_WORKFLOW.read_bytes()).hexdigest(),
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

    def test_cilium_expected_images_come_from_pinned_chart_render(self) -> None:
        config = runtime.load_config()
        manifest = """apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: cilium
  namespace: kube-system
spec:
  template:
    spec:
      initContainers:
        - name: config
          image: quay.io/cilium/startup-script:1
      containers:
        - name: cilium-agent
          image: quay.io/cilium/cilium:v1.19.5
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: cilium-operator
  namespace: kube-system
spec:
  template:
    spec:
      containers:
        - name: cilium-operator
          image: quay.io/cilium/operator-generic:v1.19.5
"""
        runner = mock.Mock(
            return_value=runtime.subprocess.CompletedProcess(
                ["helm"], 0, stdout=manifest, stderr=""
            )
        )
        receipt = {
            "tools": {"helm": "helm"},
            "artifacts": {"cilium_chart": "/verified/cilium-1.19.5.tgz"},
        }
        with (
            mock.patch.object(runtime, "run", runner),
            mock.patch.object(runtime, "kube_env", return_value={}),
            mock.patch.object(runtime, "vm_ip", return_value="192.168.122.10"),
        ):
            images = runtime._expected_cilium_workload_images(
                Path("."), config, receipt
            )
        self.assertEqual(
            images,
            {
                "daemonset": {
                    "containers": {
                        "cilium-agent": "quay.io/cilium/cilium:v1.19.5"
                    },
                    "init_containers": {
                        "config": "quay.io/cilium/startup-script:1"
                    },
                },
                "operator": {
                    "containers": {
                        "cilium-operator": "quay.io/cilium/operator-generic:v1.19.5"
                    },
                    "init_containers": {},
                },
            },
        )
        argv = runner.call_args.args[0]
        self.assertEqual(argv[:4], [
            "helm",
            "template",
            "cilium",
            "/verified/cilium-1.19.5.tgz",
        ])
        self.assertIn("--kube-version", argv)
        self.assertIn("gatewayAPI.enabled=true", argv)
        self.assertIn("kubeProxyReplacement=true", argv)


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
        self.assertIn('"database_generation_activation": False', source)
        self.assertNotIn("weltgewebe_search_generation_activation_ready", source)
        self.assertNotIn("weltgewebe_activate_search_generation", source)

    def test_semantic_provider_live_readback_requires_pinned_model_and_embedding(self) -> None:
        commit = "a" * 40
        config = runtime.load_config()
        semantic = config["semantic_search"]
        dimension = int(semantic["dimension"])
        digest = str(semantic["model_revision"]).removeprefix("sha256:")
        tags = runtime.subprocess.CompletedProcess(
            ["kubectl"], 0,
            stdout=json.dumps({"models": [{"name": semantic["model_id"], "digest": digest}]}),
            stderr="",
        )
        embed = runtime.subprocess.CompletedProcess(
            ["kubectl"], 0,
            stdout=json.dumps({"embeddings": [[0.0] * dimension]}),
            stderr="",
        )
        with (
            mock.patch.object(runtime, "load_config", return_value=config),
            mock.patch.object(runtime, "toolchain", return_value={"tools": {"kubectl": "kubectl"}}),
            mock.patch.object(runtime, "kube_env", return_value={}),
            mock.patch.object(runtime, "run", side_effect=[tags, embed]),
        ):
            observed = runtime._semantic_provider_live_readback(Path("."), commit)
        self.assertEqual(observed["model_revision"], semantic["model_revision"])
        self.assertEqual(observed["dimension"], dimension)
        self.assertTrue(observed["embedding_probe"])

        missing = runtime.subprocess.CompletedProcess(
            ["kubectl"], 0, stdout=json.dumps({"models": []}), stderr=""
        )
        with (
            mock.patch.object(runtime, "load_config", return_value=config),
            mock.patch.object(runtime, "toolchain", return_value={"tools": {"kubectl": "kubectl"}}),
            mock.patch.object(runtime, "kube_env", return_value={}),
            mock.patch.object(runtime, "run", return_value=missing),
        ):
            with self.assertRaisesRegex(runtime.RuntimeErrorEB, "model digest"):
                runtime._semantic_provider_live_readback(Path("."), commit)

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
            create_vm.index("prepared = prepare(root)"),
        )
        self.assertLess(
            create_vm.index("_invalidate_receipts(root, VM_ATTEMPT_INVALIDATES)"),
            create_vm.index("POOL_TARGET.mkdir"),
        )

        install_k3s = inspect.getsource(runtime.install_k3s)
        self.assertLess(
            install_k3s.index("_invalidate_receipts(root, K3S_ATTEMPT_INVALIDATES)"),
            install_k3s.index("_current_protected_main_commit()"),
        )
        self.assertLess(
            install_k3s.index("_invalidate_receipts(root, K3S_ATTEMPT_INVALIDATES)"),
            install_k3s.index('scp_to(root, ip, k3s_binary, "/tmp/k3s")'),
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
            inject_secrets.index("kubectl_apply(root, render_namespaces(root))"),
        )

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

    def test_install_k3s_rechecks_pinned_binary_before_copy(self) -> None:
        commit = "a" * 40
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root / "downloads/k3s"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"tampered-k3s")
            config = {
                "kubernetes": {
                    "binary_sha256": "0" * 64,
                }
            }
            with (
                mock.patch.object(
                    runtime,
                    "_current_protected_main_commit",
                    return_value=commit,
                ),
                mock.patch.object(runtime, "load_config", return_value=config),
                mock.patch.object(runtime, "vm_ip", return_value="192.0.2.10"),
                mock.patch.object(runtime, "wait_ssh"),
                mock.patch.object(runtime, "scp_to") as copy_to_vm,
            ):
                with self.assertRaisesRegex(
                    runtime.RuntimeErrorEB,
                    "k3s binary digest does not match",
                ):
                    runtime.install_k3s(root)
            copy_to_vm.assert_not_called()

        source = inspect.getsource(runtime.install_k3s)
        self.assertLess(
            source.index("observed_k3s_sha256 = sha256_file(k3s_binary)"),
            source.index('scp_to(root, ip, k3s_binary, "/tmp/k3s")'),
        )

    def test_install_k3s_restarts_and_requires_live_pinned_node(self) -> None:
        source = inspect.getsource(runtime.install_k3s)
        self.assertIn("sudo systemctl enable k3s && ", source)
        self.assertIn("sudo systemctl restart k3s", source)
        self.assertNotIn("sudo systemctl enable --now k3s", source)
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

        stale = json.loads(json.dumps(inventory))
        stale["items"][0]["status"]["nodeInfo"]["kubeletVersion"] = "v1.35.0+k3s1"
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "pinned k3s version"):
            runtime._require_exact_k3s_node_inventory(stale, expected)

    def test_live_k3s_runtime_binds_vm_kubeconfig_guest_files_and_process(self) -> None:
        commit = "a" * 40
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
                                "containers": [
                                    {
                                        "name": "manager",
                                        "image": f"ghcr.io/fluxcd/{name}:vfixture",
                                    }
                                ]
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

    def test_apply_release_requires_exact_flux_revision_and_set(self) -> None:
        source = inspect.getsource(runtime.apply_release)
        self.assertIn("_require_flux_source_revision(", source)
        self.assertIn("_require_exact_flux_revision_ready(", source)
        self.assertIn("_flux_bootstrap_contract(", source)
        self.assertIn('"gitrepository"', source)
        self.assertIn('"kustomizations"', source)
        self.assertNotIn('flux, "get", "kustomizations"', source)

        commit = "a" * 40
        api_digest = "sha256:" + "b" * 64
        web_digest = "sha256:" + "c" * 64
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = runtime.contract.render_bootstrap(
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
        self.assertIn('"commonthing-experiment-b-migration"', source)
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
        observed = runtime._require_requested_release_artifacts(
            api, web, migration, api_digest, web_digest, 1, 2
        )
        self.assertTrue(observed["migration_complete"])

        stale_api = json.loads(json.dumps(api))
        stale_api["spec"]["template"]["spec"]["containers"][0]["image"] = (
            "ghcr.io/heimgewebe/commonthing-api@sha256:" + "d" * 64
        )
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "live API image"):
            runtime._require_requested_release_artifacts(
                stale_api, web, migration, api_digest, web_digest, 1, 2
            )

        stale_migration = json.loads(json.dumps(migration))
        stale_migration["spec"]["template"]["spec"]["containers"][0]["image"] = (
            "ghcr.io/heimgewebe/commonthing-api@sha256:" + "d" * 64
        )
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "migration image"):
            runtime._require_requested_release_artifacts(
                api, web, stale_migration, api_digest, web_digest, 1, 2
            )

        incomplete = json.loads(json.dumps(migration))
        incomplete["status"] = {"succeeded": 0, "conditions": []}
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "not complete"):
            runtime._require_requested_release_artifacts(
                api, web, incomplete, api_digest, web_digest, 1, 2
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
            seed.index("_performance_modules()"),
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
            release.index("kubectl_apply(root, output.read_text"),
        )
        self.assertIn("_complete_live_check_attempt(", release)

        semantic = inspect.getsource(runtime.semantic_activate)
        self.assertLess(
            semantic.index("_begin_live_check_attempt("),
            semantic.index("kubectl_apply(root, temporary_egress)"),
        )
        self.assertIn("_complete_live_check_attempt(", semantic)

        functional = inspect.getsource(runtime.functional_readback)
        self.assertLess(
            functional.index("_begin_live_check_attempt("),
            functional.index("_gateway_data_plane_readback(root, source_commit)"),
        )
        self.assertIn("_complete_live_check_attempt(", functional)

        status = inspect.getsource(runtime.status)
        self.assertLess(
            status.index("_begin_live_check_attempt("),
            status.index('run([kubectl, "get", "nodes", "-o", "json"]'),
        )
        self.assertIn("_complete_live_check_attempt(", status)
        self.assertGreater(
            status.index("_require_live_runtime_contract(root, config)"),
            status.index("_final_recovery_state_readback(root, source_commit)"),
        )
        self.assertLess(
            status.index("_require_live_runtime_contract(root, config)"),
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

    def test_t048_sampler_failure_terminates_and_reaps_k6(self) -> None:
        load = mock.Mock()
        load.poll.return_value = None
        load.wait.return_value = -15
        resource_samples = [{"sample": "initial"}]
        db_samples = [1]
        with (
            mock.patch.object(runtime.time, "sleep"),
            mock.patch.object(
                runtime,
                "_sample_api_cgroup",
                side_effect=runtime.RuntimeErrorEB("sampler failed"),
            ),
        ):
            with self.assertRaises(runtime.RuntimeErrorEB):
                runtime._sample_t048_load(
                    Path("/tmp"),
                    "api-pod",
                    load,
                    resource_samples,
                    db_samples,
                )
        load.terminate.assert_called_once_with()
        load.wait.assert_called_once_with(timeout=10)
        load.kill.assert_not_called()

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

    def test_recovery_rto_includes_application_rollout(self) -> None:
        source = inspect.getsource(runtime.recovery_proof)
        api_wait = source.index(
            '_wait_deployment(root, APP_NAMESPACE, "weltgewebe-api", "8m")'
        )
        web_wait = source.index(
            '_wait_deployment(root, APP_NAMESPACE, "weltgewebe-web", "5m")'
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

    def test_portability_rejects_failed_or_cross_revision_receipts(self) -> None:
        commit = "a" * 40
        config = runtime.load_config()
        k3s_config_path, k3s_service_path = runtime._k3s_contract_paths(config)
        k3s_config_sha256 = runtime.sha256_file(k3s_config_path)
        k3s_service_sha256 = runtime.sha256_file(k3s_service_path)
        kubeconfig_sha256 = "3" * 64
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
            "status.json": "observed",
            "status-attempt.json": "pass",
        }
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(
                runtime,
                "_current_protected_main_commit",
                return_value=commit,
            ),
            mock.patch.object(
                runtime,
                "_expected_flux_controller_contract",
                return_value=flux_expected_contract,
            ),
            mock.patch.object(
                runtime,
                "_rendered_application_workload_contract",
                return_value=application_expected_contract,
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
            changed_status["cilium"]["runtime_image_ids_baseline"][
                "daemonset"
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
                            live_binding,
                            "_manifest_and_fixture",
                            return_value=(manifest, [fixture_node]),
                        ),
                        mock.patch.object(
                            runtime,
                            "load_config",
                            return_value={"semantic_search": semantic},
                        ),
                        mock.patch.object(
                            runtime,
                            "_psql",
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
                    live_binding,
                    "_manifest_and_fixture",
                    return_value=(manifest, [fixture_node]),
                ),
                mock.patch.object(
                    runtime,
                    "_psql",
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
                    live_binding,
                    "_manifest_and_fixture",
                    return_value=(manifest, [fixture_node]),
                ),
                mock.patch.object(
                    runtime,
                    "_psql",
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
                live_binding,
                "_manifest_and_fixture",
                return_value=({}, [fixture_row]),
            ),
            mock.patch.object(
                runtime,
                "_psql",
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
            with (
                mock.patch.object(
                    runtime,
                    "_performance_modules",
                    return_value=(evidence, Path("/unused/domain_scale.py")),
                ),
                mock.patch.object(
                    runtime,
                    "load_config",
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
                mock.patch.object(runtime, "_psql", side_effect=psql_values),
                mock.patch.object(
                    runtime,
                    "_t048_live_fixture_binding",
                    return_value=live_binding,
                ) as live_check,
                mock.patch.object(runtime, "run") as run_command,
                mock.patch.object(runtime, "_run_input_file") as load_fixture,
            ):
                receipt = runtime.seed_t048_fixture(root)

            self.assertEqual(receipt["status"], "loaded")
            self.assertEqual(receipt["source_commit"], commit)
            self.assertEqual(receipt["live_binding"], live_binding)
            self.assertEqual(receipt["nodes"], 20000)
            self.assertEqual(receipt["edges"], 100000)
            self.assertTrue((receipts / "t048-fixture.json").is_file())
            live_check.assert_called_once()
            run_command.assert_not_called()
            load_fixture.assert_not_called()

    def test_database_signature_hashes_complete_persisted_domain_and_search_rows(self) -> None:
        source = inspect.getsource(runtime._database_signature)
        self.assertIn("md5(to_jsonb(n)::text)", source)
        self.assertIn("md5(to_jsonb(e)::text)", source)
        self.assertIn("domain_outbox", source)
        self.assertIn("md5(to_jsonb(o)::text)", source)
        self.assertIn("domain_event_consumptions", source)
        self.assertIn("md5(to_jsonb(c)::text)", source)
        self.assertIn("domain_projection_state", source)
        self.assertIn("md5(to_jsonb(s)::text)", source)
        self.assertIn("search_node_versions", source)
        self.assertIn("md5(to_jsonb(v)::text)", source)
        self.assertIn("search_index_generations", source)
        self.assertIn("md5(to_jsonb(g)::text)", source)
        self.assertIn("search_node_projections", source)
        self.assertIn("md5(to_jsonb(p)::text)", source)
        self.assertIn("search_projection_jobs", source)
        self.assertIn("md5(to_jsonb(j)::text)", source)

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
        db_signature = source.index("before_db = _database_signature(root)")
        nats_signature = source.index("before_nats = _jetstream_signature(root)")
        dump = source.index('"pg_dump"')
        self.assertLess(api_scale, api_wait)
        self.assertLess(web_scale, web_wait)
        self.assertLess(api_wait, db_signature)
        self.assertLess(web_wait, db_signature)
        self.assertLess(db_signature, nats_signature)
        self.assertLess(nats_signature, dump)

    def test_recovery_waits_for_nats_quiescence_before_pvc_backup(self) -> None:
        source = inspect.getsource(runtime.recovery_proof)
        scaled = source.index('_scale_deployment(root, DATA_NAMESPACE, "nats", 0)')
        waited = source.index("_wait_pods_absent(", scaled)
        transfer = source.index(
            '_nats_transfer_pod(root, "commonthing-experiment-b-nats-backup")'
        )
        self.assertLess(scaled, waited)
        self.assertLess(waited, transfer)

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
        self.assertIn("stream/durable-consumer continuity signature", source)

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
            '_nats_transfer_pod(root, "commonthing-experiment-b-nats-restore")'
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
        self.pool_present = True
        self.patch("POOL_TARGET", self.pool)
        self.patch("load_config", return_value=self.config)
        self.main = self.patch("_current_protected_main_commit", return_value=self.commit)
        expected = vm_substrate_fixture()
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
                    "conditions": [{"type": "Ready", "status": "True"}],
                    "containerStatuses": [
                        {
                            "name": name,
                            "ready": True,
                            "state": {
                                "running": {"startedAt": "2026-09-27T00:00:00Z"}
                            },
                            "imageID": fixture_runtime_image_id(image),
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
            self.flux_expected_contract[name] = {
                "replicas": 1,
                "selector_labels": json.loads(json.dumps(labels)),
                "images": json.loads(json.dumps(images)),
            }
            self.flux_controller_deployments.append(
                {
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
            expected = runtime._versioned_data_deployment_contract(
                runtime.CLUSTER / f"data/{name}.yaml", name
            )
            pod_spec = {
                "containers": [
                    {"name": container_name, "image": image}
                    for container_name, image in expected["images"]["containers"].items()
                ],
                "initContainers": [
                    {"name": container_name, "image": image}
                    for container_name, image in expected["images"][
                        "init_containers"
                    ].items()
                ],
            }
            self.data_deployments[name] = {
                "metadata": {
                    "name": name,
                    "namespace": runtime.DATA_NAMESPACE,
                    "generation": 1,
                },
                "spec": {
                    "replicas": expected["replicas"],
                    "selector": {
                        "matchLabels": json.loads(
                            json.dumps(expected["selector_labels"])
                        )
                    },
                    "template": {"spec": pod_spec},
                },
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
            self.data_pods[name] = [
                workload_pod(
                    runtime.DATA_NAMESPACE,
                    name,
                    index,
                    expected["selector_labels"],
                    expected["images"],
                )
                for index in range(expected["replicas"])
            ]

        self.data_services = {}
        for name in ("postgres", "nats"):
            expected_service = runtime._versioned_data_service_contract(
                runtime.CLUSTER / f"data/{name}.yaml", name
            )
            self.data_services[name] = {
                "metadata": {
                    "name": name,
                    "namespace": runtime.DATA_NAMESPACE,
                },
                "spec": json.loads(
                    json.dumps(expected_service["spec"])
                ),
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
                "spec": {"storageClassName": "local-path"},
                "status": {"phase": "Bound"},
            },
            {
                "metadata": {"namespace": runtime.DATA_NAMESPACE, "name": "nats-data"},
                "spec": {"storageClassName": "local-path"},
                "status": {"phase": "Bound"},
            },
            {
                "metadata": {"namespace": runtime.APP_NAMESPACE, "name": "ollama-models"},
                "spec": {"storageClassName": "local-path"},
                "status": {"phase": "Bound"},
            },
        ]
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
        self.runner = self.patch("run", side_effect=self.run_fixture)

    def patch(self, name: str, *args, **kwargs):
        patcher = mock.patch.object(runtime, name, *args, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def run_fixture(self, argv: list[str], **_kwargs):
        output, code = "", 0
        if argv[0] == "virt-install":
            self.domain_present = True
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
            elif command == "list":
                output = f"{runtime.VM_NAME}\n" if self.domain_present else ""
            elif command == "pool-info":
                code = 0 if self.pool_present else 1
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
            elif command == "vol-create-as":
                (self.pool / argv[5]).write_bytes(b"created volume")
            elif command == "vol-delete":
                (self.pool / argv[4]).unlink()
            elif command == "undefine":
                self.domain_present = False
            elif command == "pool-undefine":
                self.pool_present = False
            else:
                self.assertIn(command, {
                    "pool-build", "pool-start", "vol-upload", "pool-refresh",
                    "destroy", "pool-destroy", "pool-delete",
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
        self.patch("prepare", return_value={
            "cloud_image": str(self.root / "ubuntu.img"), "cloud_image_virtual_size": 4 * 1024**3,
        })

    def prepare_status(self) -> None:
        api_digest = "sha256:" + "b" * 64
        web_digest = "sha256:" + "c" * 64
        binding = runtime.contract.render_bootstrap(
            self.commit,
            api_digest,
            web_digest,
            self.root / "bootstrap.yaml",
        )
        self.flux_contract = runtime._flux_bootstrap_contract(self.root, binding)
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
        self.k3s_readback = self.patch(
            "_require_live_k3s_runtime",
            return_value=json.loads(json.dumps(self.k3s_runtime)),
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
        self.application_workload_live = self.patch(
            "_require_live_application_workloads",
            return_value=json.loads(
                json.dumps(self.application_workload_readback)
            ),
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
        self.expected_cilium_images = self.patch(
            "_expected_cilium_workload_images",
            return_value={
                "daemonset": json.loads(json.dumps(self.cilium_expected_images)),
                "operator": json.loads(
                    json.dumps(self.cilium_expected_operator_images)
                ),
            },
        )
        self.patch("_kubectl_json", side_effect=self.kubernetes_fixture)

    def kubernetes_fixture(self, _root, arguments):
        if arguments == [
            "-n", runtime.APP_NAMESPACE, "get", "configmap", "weltgewebe-runtime"
        ]:
            return self.runtime_config_map
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
        if "httproute" in arguments:
            return self.httproute
        self.assertIn("gateway", arguments)
        return self.gateway

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
            "refuses same-named libvirt resources",
        ):
            runtime.teardown(self.root)
        self.assertTrue(self.domain_present)
        self.assertTrue(self.pool_present)
        self.assertTrue(self.root.exists())

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
        healthy_daemonset = json.loads(json.dumps(self.cilium_daemonset))
        healthy_cilium_pods = json.loads(json.dumps(self.cilium_pods))
        healthy_operator_pods = json.loads(json.dumps(self.cilium_operator_pods))

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
        self.cilium_values = healthy_values

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
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "images drifted"):
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
        self.cilium_operator = healthy_operator

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
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "images drifted"):
            runtime.status(self.root)

        self.flux_controller_deployments = json.loads(json.dumps(healthy))
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

        healthy = list(self.pvcs)
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

    def test_final_recovery_state_readback_binds_current_signatures_and_fixture(self) -> None:
        recovery = {
            "schema_version": 1,
            "status": "pass",
            "source_commit": self.commit,
            "rpo_seconds": 0,
            "database_before": {"db": "stable"},
            "database_after": {"db": "stable"},
            "jetstream_before": {"nats": "stable"},
            "jetstream_after": {"nats": "stable"},
        }
        runtime.atomic_json(self.root / "receipts/recovery.json", recovery)
        runtime.atomic_json(
            self.root / "receipts/t048-fixture.json",
            {
                "schema_version": 1,
                "status": "loaded",
                "source_commit": self.commit,
                "live_binding": {"generation_id": "fixture"},
            },
        )
        with (
            mock.patch.object(runtime, "_database_signature", return_value={"db": "stable"}),
            mock.patch.object(runtime, "_jetstream_signature", return_value={"nats": "stable"}),
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
            result = runtime._final_recovery_state_readback(self.root, self.commit)
        self.assertEqual(result["database_signature"], {"db": "stable"})
        self.assertEqual(result["jetstream_signature"], {"nats": "stable"})
        fixture.assert_called_once_with(self.root, self.commit)

        runtime.atomic_json(
            self.root / "receipts/recovery-failed.json",
            {
                "schema_version": 1,
                "status": "failed",
                "source_commit": self.commit,
            },
        )
        with self.assertRaisesRegex(runtime.RuntimeErrorEB, "latest failed recovery"):
            runtime._final_recovery_state_readback(self.root, self.commit)
        (self.root / "receipts/recovery-failed.json").unlink()

        with (
            mock.patch.object(runtime, "_database_signature", return_value={"db": "drift"}),
            mock.patch.object(runtime, "_jetstream_signature", return_value={"nats": "stable"}),
        ):
            with self.assertRaisesRegex(runtime.RuntimeErrorEB, "database/search state drifted"):
                runtime._final_recovery_state_readback(self.root, self.commit)

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
            runtime.recovery_proof,
        ):
            self.assertIn(
                "_require_kubernetes_target_binding",
                inspect.getsource(function),
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
            source.count("_require_t048_api_release_binding"), 2
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

        def kubectl_result(_root, arguments, **_kwargs):
            stdout = ""
            if "exec" in arguments:
                stdout = ""
            return runtime.subprocess.CompletedProcess(
                arguments, 0, stdout=stdout, stderr=""
            )

        with (
            mock.patch.object(runtime, "kubectl_apply") as apply,
            mock.patch.object(
                runtime,
                "_pvc_volume_identity",
                return_value=new_identity,
            ),
            mock.patch.object(
                runtime, "_kubectl", side_effect=kubectl_result
            ),
            mock.patch.object(runtime, "_delete_pod") as delete,
        ):
            result = runtime._require_empty_replacement_pvc(
                self.root, "postgres-data", old_identity
            )
        self.assertEqual(result["old"], old_identity)
        self.assertEqual(result["new"], new_identity)
        self.assertTrue(result["empty_before_restore"])
        apply.assert_called_once()
        delete.assert_called_once()

        with (
            mock.patch.object(runtime, "kubectl_apply"),
            mock.patch.object(
                runtime,
                "_pvc_volume_identity",
                return_value=old_identity,
            ),
            mock.patch.object(
                runtime, "_kubectl", side_effect=kubectl_result
            ),
            mock.patch.object(runtime, "_delete_pod"),
            self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "reused the previous storage identity",
            ),
        ):
            runtime._require_empty_replacement_pvc(
                self.root, "postgres-data", old_identity
            )

        def nonempty_result(_root, arguments, **_kwargs):
            stdout = "lost+found\n" if "exec" in arguments else ""
            return runtime.subprocess.CompletedProcess(
                arguments, 0, stdout=stdout, stderr=""
            )

        with (
            mock.patch.object(runtime, "kubectl_apply"),
            mock.patch.object(
                runtime,
                "_pvc_volume_identity",
                return_value=new_identity,
            ),
            mock.patch.object(
                runtime, "_kubectl", side_effect=nonempty_result
            ),
            mock.patch.object(runtime, "_delete_pod"),
            self.assertRaisesRegex(
                runtime.RuntimeErrorEB,
                "not empty before restore",
            ),
        ):
            runtime._require_empty_replacement_pvc(
                self.root, "postgres-data", old_identity
            )

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
        self.assertEqual(
            runtime._container_runtime_contract(expected, "expected"),
            runtime._container_runtime_contract(live, "live"),
        )

    def test_create_binds_actual_vm_to_current_source_and_config(self) -> None:
        self.prepare_create()
        result = runtime.create_vm(self.root)
        self.assertEqual(
            result, vm_receipt_fixture(result["substrate"], self.root)
        )
        self.assertEqual(result["substrate"], runtime._live_vm_substrate(self.root, self.config))
        self.assertEqual(json.loads((self.root / "receipts/vm-create.json").read_text()), result)
        self.assertEqual(self.main.call_count, 2)

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


if __name__ == "__main__":
    unittest.main()
