from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import experiment_b as eb

ROOT = Path(__file__).resolve().parents[2]
CLUSTER = ROOT / "platform/clusters/experiment-b"
OVERLAY = ROOT / "platform/apps/weltgewebe/overlays/experiment-b"


class ExperimentBContractTests(unittest.TestCase):
    def test_contract_is_pinned_and_production_is_forbidden(self) -> None:
        config = json.loads((CLUSTER / "config.json").read_text(encoding="utf-8"))
        eb.validate_config(config)
        self.assertEqual(config["vm"]["network_mode"], "nat-only")
        self.assertEqual(config["vm"]["host_mounts"], [])
        self.assertEqual(config["vm"]["vcpu"], 6)
        self.assertEqual(config["vm"]["memory_mib"], 12288)
        self.assertEqual(config["vm"]["disk_gib"], 60)
        self.assertEqual(config["kubernetes"]["version"], "v1.36.1+k3s1")
        self.assertIn("--flannel-backend=none", config["kubernetes"]["server_flags"])
        self.assertIn("--disable-kube-proxy", config["kubernetes"]["server_flags"])
        self.assertFalse(config["production_activation"])
        self.assertTrue(config["test_data_only"])
        self.assertTrue(config["external_secrets"]["registry_required"])
        self.assertEqual(
            config["external_secrets"]["registry_secret"],
            "commonthing-experiment-b-registry",
        )
        self.assertTrue(all(config["forbidden"].values()))

    def test_renderer_state_root_is_scoped_to_experiment_b_subtree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "experiment-b"
            sibling = Path(tmp) / "other-controller"
            with mock.patch.object(eb, "DEFAULT_STATE_ROOT", base):
                self.assertEqual(eb.state_root(str(base)), base.resolve())
                self.assertEqual(
                    eb.state_root(str(base / "attempt-1")),
                    (base / "attempt-1").resolve(),
                )
                with self.assertRaises(eb.ContractError):
                    eb.state_root(str(base.parent))
                with self.assertRaises(eb.ContractError):
                    eb.state_root(str(sibling))

    def test_bootstrap_binds_exact_commit_and_image_digests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "bootstrap.yaml"
            result = eb.render_bootstrap(
                "a" * 40,
                "sha256:" + "b" * 64,
                "sha256:" + "c" * 64,
                output,
            )
            rendered = output.read_text(encoding="utf-8")
            self.assertNotIn("${", rendered)
            self.assertIn("commit: " + "a" * 40, rendered)
            self.assertIn("sha256:" + "b" * 64, rendered)
            self.assertIn("sha256:" + "c" * 64, rendered)
            self.assertEqual(result["sha256"], eb.sha256_file(output))

    def test_mutable_release_bindings_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "bootstrap.yaml"
            with self.assertRaises(eb.ContractError):
                eb.render_bootstrap(
                    "main",
                    "sha256:" + "b" * 64,
                    "sha256:" + "c" * 64,
                    output,
                )
            with self.assertRaises(eb.ContractError):
                eb.render_bootstrap(
                    "a" * 40,
                    "latest",
                    "sha256:" + "c" * 64,
                    output,
                )

    def test_k3s_service_is_versioned_without_install_script(self) -> None:
        config = (CLUSTER / "k3s-config.yaml").read_text(encoding="utf-8")
        service = (CLUSTER / "k3s.service").read_text(encoding="utf-8")
        self.assertIn("flannel-backend: none", config)
        self.assertIn("disable-network-policy: true", config)
        self.assertIn("disable-kube-proxy: true", config)
        self.assertIn("cluster-cidr: 10.42.0.0/16", config)
        self.assertIn("ExecStart=/usr/local/bin/k3s server", service)
        self.assertNotIn("get.k3s.io", config + service)

    def test_storage_removes_kind_specific_host_path_binding(self) -> None:
        storage = (CLUSTER / "data/storage.yaml").read_text(encoding="utf-8")
        postgres = (CLUSTER / "data/postgres.yaml").read_text(encoding="utf-8")
        nats = (CLUSTER / "data/nats.yaml").read_text(encoding="utf-8")
        self.assertEqual(storage.count("storageClassName: local-path"), 2)
        self.assertNotIn("hostPath:", storage)
        self.assertNotIn("volumeName:", storage)
        self.assertNotIn("nodeSelector:", postgres)
        self.assertNotIn("nodeSelector:", nats)

    def test_testbed_has_no_production_hostname_or_mutable_image_tag(self) -> None:
        gateway = (CLUSTER / "gateway/gateway.yaml").read_text(encoding="utf-8")
        route = (CLUSTER / "gateway/httproute.yaml").read_text(encoding="utf-8")
        images = (OVERLAY / "image-patch.yaml").read_text(encoding="utf-8")
        self.assertNotIn("commonthing.net", gateway + route)
        self.assertNotIn(":latest", images)
        self.assertIn("commonthing-api@${API_DIGEST}", images)
        self.assertIn("commonthing-web@${WEB_DIGEST}", images)
        self.assertEqual(images.count("commonthing-experiment-b-registry"), 2)
        migration = (CLUSTER / "migration/job.yaml").read_text(encoding="utf-8")
        self.assertIn("commonthing-experiment-b-registry", migration)

    def test_network_policy_allows_only_api_and_migration_to_postgres(self) -> None:
        policy = (CLUSTER / "data/network-policy.yaml").read_text(encoding="utf-8")
        postgres_rule = policy.split(
            "name: allow-app-postgres-access", 1
        )[1].split("---", 1)[0]
        self.assertIn("app.kubernetes.io/name: weltgewebe-api", postgres_rule)
        self.assertIn(
            "app.kubernetes.io/name: commonthing-experiment-b-migration",
            postgres_rule,
        )
        self.assertIn("port: 5432", postgres_rule)
        self.assertNotIn("port: 4222", postgres_rule)

    def test_migration_egress_is_installed_before_migration_and_postgres_only(self) -> None:
        api_patch = (OVERLAY / "network-policy-api-data-egress-patch.yaml").read_text(
            encoding="utf-8"
        )
        migration = (
            CLUSTER / "namespaces/migration-postgres-egress.yaml"
        ).read_text(encoding="utf-8")
        namespace_stage = (
            CLUSTER / "namespaces/kustomization.yaml"
        ).read_text(encoding="utf-8")
        app_overlay = (OVERLAY / "kustomization.yaml").read_text(encoding="utf-8")

        self.assertNotIn("commonthing-experiment-b-migration", api_patch)
        self.assertIn("namespace: commonthing-experiment-b", migration)
        self.assertIn("commonthing-experiment-b-migration", migration)
        self.assertIn("app.kubernetes.io/name: postgres", migration)
        self.assertIn("port: 5432", migration)
        self.assertNotIn("port: 4222", migration)
        self.assertIn("kubernetes.io/metadata.name: kube-system", migration)
        self.assertIn("port: 53", migration)
        self.assertIn("protocol: UDP", migration)
        self.assertIn("protocol: TCP", migration)
        self.assertIn("migration-postgres-egress.yaml", namespace_stage)
        self.assertNotIn("migration-postgres-egress", app_overlay)

    def test_experiment_b_contract_is_registered_in_platform_ci(self) -> None:
        workflow = (ROOT / ".github/workflows/kubernetes-platform-proof.yml").read_text(
            encoding="utf-8"
        )
        validator = (ROOT / "scripts/platform/validate_platform.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("scripts/platform/experiment_b_test.py -v", workflow)
        self.assertIn("scripts/platform/experiment_b_runtime_test.py -v", workflow)
        self.assertIn('"experiment-b"', validator)
        self.assertIn("platform/clusters/experiment-b/data", validator)
        self.assertIn("platform/clusters/experiment-b/gateway", validator)
        self.assertIn("platform/clusters/experiment-b/migration", validator)
        self.assertIn("platform/clusters/experiment-b/namespaces", validator)

    def test_semantic_search_preserves_literal_loopback_contract(self) -> None:
        config = json.loads((CLUSTER / "config.json").read_text(encoding="utf-8"))
        semantic = config["semantic_search"]
        patch = (OVERLAY / "semantic-search-patch.yaml").read_text(encoding="utf-8")
        runtime = (OVERLAY / "config-map-patch.yaml").read_text(encoding="utf-8")
        storage = (OVERLAY / "semantic-search-storage.yaml").read_text(encoding="utf-8")

        self.assertEqual(semantic["topology"], "api-pod-sidecars")
        self.assertEqual(semantic["api_replicas"], 1)
        self.assertEqual(semantic["ollama_url"], "http://127.0.0.1:11434/")
        self.assertEqual(semantic["dimension"], 2560)
        self.assertIn(
            "ollama/ollama:0.12.6@sha256:352e045b937ac29d3d9550c22fb85525f60a89e064df34c26579bee5a93b3a16",
            patch,
        )
        self.assertIn("name: search-worker", patch)
        self.assertIn("commonthing-api@${API_DIGEST}", patch)
        self.assertIn("value: 127.0.0.1:11434", patch)
        self.assertIn(
            "WELTGEWEBE_SEARCH_OLLAMA_URL: http://127.0.0.1:11434/",
            runtime,
        )
        self.assertNotIn("kind: Service", patch)
        self.assertIn("storageClassName: local-path", storage)
        self.assertIn("storage: 10Gi", storage)


if __name__ == "__main__":
    unittest.main()
