from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()