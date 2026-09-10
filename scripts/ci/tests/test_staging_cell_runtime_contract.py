from __future__ import annotations

import argparse
import base64
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from scripts.platform import staging_cell as staging  # noqa: E402


class StagingCellRuntimeContractTests(unittest.TestCase):
    def _documents(self, relative: str) -> list[dict]:
        path = ROOT / relative
        return [
            document
            for document in yaml.safe_load_all(path.read_text(encoding="utf-8"))
            if isinstance(document, dict)
        ]

    def _tool_receipt(self) -> dict:
        return {
            "tools": {
                "kind": "kind",
                "kubectl": "kubectl",
                "flux": "flux",
                "helm": "helm",
            },
            "kubernetes": {"kind_node_image": "kind-node-image"},
            "artifacts": {},
            "lock_sha256": "f" * 64,
        }

    def _write_bound_receipt(self, root: Path, *, owner: str, commit: str) -> str:
        (root / "data/postgres").mkdir(parents=True, exist_ok=True)
        _, source_sha = staging.load_or_create_secret_material(root)
        staging.write_cell_receipt(
            root,
            {
                "schema_version": 1,
                "status": "infrastructure-ready-image-promotion-blocked",
                "cluster": staging.DEFAULT_CLUSTER,
                "owner_id": owner,
                "bootstrap_commit": commit,
                "external_secret": {
                    "source_sha256": source_sha,
                    "required_keys": ["database-url"],
                },
            },
        )
        return source_sha

    def test_operational_commands_keep_public_stdout_reserved_for_json(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        completed = subprocess.CompletedProcess(["tool"], 0, stdout="", stderr="")
        with (
            mock.patch.object(staging.subprocess, "run", return_value=completed) as run_mock,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            staging.run(["tool"])

        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("+ external command [arguments redacted]", stderr.getvalue())
        self.assertIs(run_mock.call_args.kwargs["stdout"], stderr)
        self.assertNotIn("capture_output", run_mock.call_args.kwargs)

    def test_reference_commands_use_scoped_staging_stdout_routing(self) -> None:
        original = staging.reference.run
        observed = []

        @staging.reference_output_routed
        def exercise() -> None:
            observed.append(staging.reference.run)

        exercise()
        self.assertEqual(observed, [staging.run])
        self.assertIs(staging.reference.run, original)

    def test_tool_receipt_revalidates_locked_binary_and_artifact_digests(self) -> None:
        with tempfile.TemporaryDirectory(prefix="staging-tool-receipt-") as tmp_name:
            temp = Path(tmp_name)
            repo_root = temp / "repo"
            state_root = temp / "state"
            cache = state_root / "toolchain"
            (repo_root / "platform").mkdir(parents=True)
            (cache / "bin").mkdir(parents=True)
            (cache / "artifacts").mkdir(parents=True)

            lock: dict[str, object] = {
                "schema_version": 1,
                "tools": {},
                "artifacts": {},
            }
            tools: dict[str, str] = {}
            artifacts: dict[str, str] = {}
            for name in staging.REQUIRED_TOOLS:
                payload = f"tool:{name}".encode()
                path = cache / "bin" / name
                path.write_bytes(payload)
                path.chmod(0o700)
                lock["tools"][name] = {
                    "binary": name,
                    "binary_sha256": staging.sha256_bytes(payload),
                }
                tools[name] = str(path)
            for name in staging.REQUIRED_ARTIFACTS:
                payload = f"artifact:{name}".encode()
                filename = f"{name}.locked"
                path = cache / "artifacts" / filename
                path.write_bytes(payload)
                path.chmod(0o600)
                lock["artifacts"][name] = {
                    "filename": filename,
                    "sha256": staging.sha256_bytes(payload),
                }
                artifacts[name] = str(path)

            lock_path = repo_root / "platform/toolchain.lock.json"
            lock_path.write_text(json.dumps(lock), encoding="utf-8")
            receipt = {
                "schema_version": 1,
                "lock_sha256": staging.sha256_file(lock_path),
                "cache": str(cache),
                "tools": tools,
                "artifacts": artifacts,
                "kubernetes": {"kind_node_image": "kind-node-image"},
            }
            receipt_path = cache / "receipt.json"
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

            with mock.patch.object(staging, "ROOT", repo_root):
                loaded = staging.load_tool_receipt(state_root)
                self.assertEqual(loaded["lock_sha256"], receipt["lock_sha256"])

                kind = Path(tools["kind"])
                kind.write_bytes(b"tampered-kind")
                kind.chmod(0o700)
                with self.assertRaisesRegex(staging.StagingCellError, "tool kind digest mismatch"):
                    staging.load_tool_receipt(state_root)

                kind.write_bytes(b"tool:kind")
                kind.chmod(0o700)
                first_artifact = staging.REQUIRED_ARTIFACTS[0]
                Path(artifacts[first_artifact]).write_bytes(b"tampered-artifact")
                with self.assertRaisesRegex(
                    staging.StagingCellError, f"artifact {first_artifact} digest mismatch"
                ):
                    staging.load_tool_receipt(state_root)

                flux = Path(tools["flux"])
                flux.write_bytes(b"tampered-unused-flux")
                flux.chmod(0o700)
                reduced = staging.load_tool_receipt(
                    state_root, required_tools=("kind",), required_artifacts=()
                )
                self.assertEqual(reduced["tools"]["kind"], tools["kind"])

    def test_persistent_volumes_are_static_prebound_and_fenced_to_data_worker(self) -> None:
        documents = self._documents(
            "platform/clusters/staging/data/persistent-volumes.yaml"
        )
        by_kind_name = {
            (document.get("kind"), document.get("metadata", {}).get("name")): document
            for document in documents
        }
        for volume, claim in (
            ("weltgewebe-staging-postgres", "postgres-data"),
            ("weltgewebe-staging-nats", "nats-data"),
        ):
            pv = by_kind_name[("PersistentVolume", volume)]
            pvc = by_kind_name[("PersistentVolumeClaim", claim)]
            self.assertEqual(pv["spec"].get("storageClassName"), "")
            self.assertEqual(pv["spec"].get("persistentVolumeReclaimPolicy"), "Retain")
            terms = pv["spec"]["nodeAffinity"]["required"]["nodeSelectorTerms"]
            self.assertEqual(
                terms,
                [
                    {
                        "matchExpressions": [
                            {
                                "key": "kubernetes.io/hostname",
                                "operator": "In",
                                "values": [staging.data_node_name(staging.DEFAULT_CLUSTER)],
                            }
                        ]
                    }
                ],
            )
            self.assertEqual(pvc["spec"].get("storageClassName"), "")
            self.assertEqual(pvc["spec"].get("volumeName"), volume)

    def test_network_policies_allow_only_staging_api_pods(self) -> None:
        documents = self._documents("platform/clusters/staging/data/network-policy.yaml")
        policies = {document["metadata"]["name"]: document for document in documents}
        expected = {
            "allow-app-postgres-access": ("postgres", 5432),
            "allow-app-nats-access": ("nats", 4222),
        }
        for name, (app_name, port) in expected.items():
            policy = policies[name]
            self.assertEqual(
                policy["spec"]["podSelector"]["matchLabels"],
                {"app.kubernetes.io/name": app_name},
            )
            ingress = policy["spec"]["ingress"]
            self.assertEqual(len(ingress), 1)
            peer = ingress[0]["from"][0]
            self.assertEqual(
                peer["namespaceSelector"]["matchLabels"],
                {"kubernetes.io/metadata.name": staging.APP_NAMESPACE},
            )
            self.assertEqual(
                peer["podSelector"]["matchLabels"],
                {"app.kubernetes.io/name": "weltgewebe-api"},
            )
            self.assertEqual(ingress[0]["ports"], [{"port": port, "protocol": "TCP"}])
        self.assertNotIn("allow-app-data-access", policies)

        api_documents = self._documents(
            "platform/apps/weltgewebe/base/api-deployment.yaml"
        )
        api = next(
            document
            for document in api_documents
            if document.get("kind") == "Deployment"
            and document.get("metadata", {}).get("name") == "weltgewebe-api"
        )
        expected_api_selector = {"app.kubernetes.io/name": "weltgewebe-api"}
        self.assertEqual(api["spec"]["selector"]["matchLabels"], expected_api_selector)
        self.assertEqual(
            {
                "app.kubernetes.io/name": api["spec"]["template"]["metadata"][
                    "labels"
                ]["app.kubernetes.io/name"]
            },
            expected_api_selector,
        )

    def test_data_workloads_are_pinned_and_avoid_recursive_fs_group_churn(self) -> None:
        for relative in (
            "platform/clusters/staging/data/postgres.yaml",
            "platform/clusters/staging/data/nats.yaml",
        ):
            documents = self._documents(relative)
            deployment = next(
                document for document in documents if document.get("kind") == "Deployment"
            )
            pod_spec = deployment["spec"]["template"]["spec"]
            self.assertEqual(
                pod_spec["nodeSelector"],
                {"kubernetes.io/hostname": staging.data_node_name(staging.DEFAULT_CLUSTER)},
            )
            self.assertEqual(
                pod_spec["securityContext"]["fsGroupChangePolicy"], "OnRootMismatch"
            )
            self.assertEqual(deployment["spec"]["strategy"], {"type": "Recreate"})

    def test_postgres_has_startup_budget_and_known_writable_mounts(self) -> None:
        documents = self._documents("platform/clusters/staging/data/postgres.yaml")
        deployment = next(
            document for document in documents if document.get("kind") == "Deployment"
        )
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        startup = container["startupProbe"]
        self.assertGreaterEqual(startup["failureThreshold"] * startup["periodSeconds"], 300)
        mounts = {entry["mountPath"] for entry in container["volumeMounts"]}
        self.assertTrue(
            {"/var/lib/postgresql/data", "/var/run/postgresql", "/tmp"}.issubset(mounts)
        )

    def test_kind_template_mounts_retained_data_on_exactly_one_worker(self) -> None:
        config = yaml.safe_load(
            (ROOT / "platform/clusters/staging/kind.yaml").read_text(encoding="utf-8")
        )
        nodes = config["nodes"]
        self.assertEqual([node["role"] for node in nodes], ["control-plane", "worker", "worker"])
        mounted = [
            (index, node, node.get("extraMounts", []))
            for index, node in enumerate(nodes)
            if node.get("extraMounts")
        ]
        self.assertEqual(len(mounted), 1)
        index, node, mounts = mounted[0]
        self.assertEqual(index, 1)
        self.assertEqual(node["role"], "worker")
        self.assertEqual(len(mounts), 1)
        self.assertEqual(mounts[0]["hostPath"], "__COMMONTHING_STAGING_DATA_ROOT__")
        self.assertEqual(mounts[0]["containerPath"], "/var/local/weltgewebe-staging")
        self.assertFalse(mounts[0]["readOnly"])

    def test_apply_yaml_emits_native_multi_document_stream(self) -> None:
        documents = [
            {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "one"}},
            {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "two"}},
        ]
        with mock.patch.object(staging, "run") as run_mock:
            staging.apply_yaml("kubectl", documents)
        body = run_mock.call_args.kwargs["input_text"]
        self.assertTrue(body.startswith("---\n"))
        self.assertEqual(list(yaml.safe_load_all(body)), documents)

    def test_public_parser_does_not_offer_state_root_override(self) -> None:
        parser = staging.parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["status", "--state-root", "/tmp/other"])

    def test_public_parser_does_not_offer_cluster_override(self) -> None:
        parser = staging.parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["status", "--cluster", "other"])
        parsed = parser.parse_args(["status"])
        self.assertEqual(parsed.cluster, staging.DEFAULT_CLUSTER)

    def test_public_status_keeps_diagnostics_without_secret_material(self) -> None:
        result = {
            "status": "degraded",
            "cluster": staging.DEFAULT_CLUSTER,
            "bootstrap_commit": "a" * 40,
            "source_revision": "main@sha1:" + "b" * 40,
            "source_matches_commit": False,
            "data_ready": "False",
            "data_revision": "main@sha1:" + "d" * 40,
            "data_matches_commit": False,
            "pvcs": {"postgres-data": "Bound", "nats-data": "Pending"},
            "live_workloads": {
                "postgres": "True",
                "nats": "False",
                "source-controller": "True",
                "kustomize-controller": "True",
            },
            "external_secret": {
                "database": True,
                "runtime": False,
                "ready": False,
                "source_sha256": "c" * 64,
                "password": "must-not-escape",
            },
        }
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            staging.emit_public_success("status", result)
        public = json.loads(stdout.getvalue())
        self.assertEqual(public["status"], "degraded")
        self.assertFalse(public["source_matches_commit"])
        self.assertEqual(public["source_ready_status"], "missing")
        self.assertEqual(public["data_ready_status"], "False")
        self.assertFalse(public["data_matches_commit"])
        self.assertEqual(public["data_revision"], "main@sha1:" + "d" * 40)
        self.assertEqual(public["pvcs"]["nats-data"], "Pending")
        self.assertEqual(public["live_workloads"]["nats"], "False")
        self.assertFalse(public["external_secret"]["runtime"])
        rendered = stdout.getvalue()
        self.assertNotIn("must-not-escape", rendered)
        self.assertNotIn("c" * 64, rendered)

    def test_public_down_output_preserves_actual_status(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            staging.emit_public_success(
                "down",
                {
                    "status": "cluster-absent-state-preserved",
                    "cluster": staging.DEFAULT_CLUSTER,
                },
            )
        public = json.loads(stdout.getvalue())
        self.assertEqual(public["status"], "cluster-absent-state-preserved")
        self.assertEqual(public["cluster"], staging.DEFAULT_CLUSTER)

    def test_retained_secret_preflight_blocks_cluster_mutation(self) -> None:
        args = argparse.Namespace(
            cluster=staging.DEFAULT_CLUSTER,
            owner_id="owner",
            source_commit=None,
        )
        receipt = self._tool_receipt()
        with tempfile.TemporaryDirectory(prefix="staging-cell-preflight-") as tmp_name:
            root = Path(tmp_name)
            with (
                mock.patch.object(staging, "state_root", return_value=root),
                mock.patch.object(staging, "configure_reference_paths"),
                mock.patch.object(staging, "load_tool_receipt", return_value=receipt),
                mock.patch.object(staging.reference, "clusters", return_value=[]),
                mock.patch.object(staging, "retained_staging_data_exists", return_value=True),
                mock.patch.object(staging, "require_clean_commit") as commit_mock,
                mock.patch.object(staging.reference, "create_kind_cluster") as create_mock,
            ):
                with self.assertRaisesRegex(staging.StagingCellError, "without a bootstrap receipt"):
                    staging.command_up(args)
        commit_mock.assert_not_called()
        create_mock.assert_not_called()

    def test_invalid_owner_ids_fail_before_any_staging_write(self) -> None:
        for owner_id in ("team ops", "x" * 129):
            with self.subTest(owner_id=owner_id):
                args = argparse.Namespace(
                    cluster=staging.DEFAULT_CLUSTER,
                    owner_id=owner_id,
                    source_commit=None,
                )
                with (
                    mock.patch.object(staging, "state_root") as state_root_mock,
                    mock.patch.object(staging, "load_or_create_secret_material") as secret_mock,
                    mock.patch.object(staging, "write_cell_receipt") as receipt_mock,
                ):
                    with self.assertRaisesRegex(
                        staging.reference.ProofError, "stable owner id"
                    ):
                        staging.command_up(args)
                state_root_mock.assert_not_called()
                secret_mock.assert_not_called()
                receipt_mock.assert_not_called()

    def test_bootstrap_receipt_is_persisted_before_cluster_creation(self) -> None:
        args = argparse.Namespace(
            cluster=staging.DEFAULT_CLUSTER,
            owner_id="owner-a",
            source_commit=None,
        )
        commit = "a" * 40
        receipt = self._tool_receipt()
        with tempfile.TemporaryDirectory(prefix="staging-cell-bootstrap-receipt-") as tmp_name:
            root = Path(tmp_name)

            def fail_after_receipt(*call_args, **call_kwargs):
                del call_kwargs
                bound = staging.load_cell_receipt(root)
                self.assertEqual(bound["status"], "bootstrap-in-progress")
                self.assertEqual(bound["owner_id"], "owner-a")
                self.assertEqual(bound["bootstrap_commit"], commit)
                self.assertEqual(call_args[4], commit)
                self.assertEqual(call_args[5], "owner-a")
                raise RuntimeError("stop after ownership receipt proof")

            with (
                mock.patch.object(staging, "state_root", return_value=root),
                mock.patch.object(staging, "configure_reference_paths"),
                mock.patch.object(staging, "load_tool_receipt", return_value=receipt),
                mock.patch.object(staging.reference, "clusters", return_value=[]),
                mock.patch.object(staging, "require_clean_commit", return_value=commit),
                mock.patch.object(
                    staging.reference,
                    "create_kind_cluster",
                    side_effect=fail_after_receipt,
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "ownership receipt proof"):
                    staging.command_up(args)

            self.assertTrue((root / "receipts/cell-bootstrap.json").is_file())
            self.assertTrue((root / "secrets/staging-runtime.json").is_file())
            self.assertTrue((root / "clusters").is_dir())

    def test_recreate_after_down_preserves_receipt_owner_and_commit_pin(self) -> None:
        owner = "owner-a"
        persisted_commit = "b" * 40
        args = argparse.Namespace(
            cluster=staging.DEFAULT_CLUSTER,
            owner_id=owner,
            source_commit=None,
        )
        receipt = self._tool_receipt()
        with tempfile.TemporaryDirectory(prefix="staging-cell-recreate-") as tmp_name:
            root = Path(tmp_name)
            source_sha = self._write_bound_receipt(
                root, owner=owner, commit=persisted_commit
            )
            with (
                mock.patch.object(staging, "state_root", return_value=root),
                mock.patch.object(staging, "configure_reference_paths"),
                mock.patch.object(staging, "load_tool_receipt", return_value=receipt),
                mock.patch.object(staging.reference, "clusters", return_value=[]),
                mock.patch.object(
                    staging,
                    "require_clean_commit",
                    return_value=persisted_commit,
                ) as commit_mock,
                mock.patch.object(staging.reference, "create_kind_cluster") as create_mock,
                mock.patch.object(staging, "prepare_volume_permissions"),
                mock.patch.object(staging.reference, "control_plane_address", return_value="127.0.0.1"),
                mock.patch.object(staging.reference, "install_platform_components"),
                mock.patch.object(staging, "run"),
                mock.patch.object(
                    staging,
                    "inject_external_secrets",
                    return_value={"source_sha256": source_sha, "required_keys": ["database-url"]},
                ),
                mock.patch.object(staging, "apply_yaml"),
                mock.patch.object(staging, "reconcile_data") as reconcile_mock,
                mock.patch.object(
                    staging,
                    "staging_live_health",
                    return_value={name: "True" for name in staging.LIVE_DEPLOYMENTS},
                ) as live_mock,
                mock.patch.object(
                    staging,
                    "output",
                    return_value="node-a\nnode-b\nnode-c",
                ),
                mock.patch.object(
                    staging,
                    "image_promotion_state",
                    return_value={"status": "blocked"},
                ),
            ):
                result = staging.command_up(args)

            commit_mock.assert_called_once_with(
                None,
                expected_commit=persisted_commit,
                require_public_main=False,
            )
            create_args = create_mock.call_args.args
            self.assertEqual(create_args[4], persisted_commit)
            self.assertEqual(create_args[5], owner)
            reconcile_mock.assert_called_once_with("kubectl", persisted_commit)
            live_mock.assert_called_once_with("kubectl")
            self.assertEqual(result["bootstrap_commit"], persisted_commit)

    def test_recreate_after_down_rejects_different_owner_before_mutation(self) -> None:
        persisted_commit = "c" * 40
        args = argparse.Namespace(
            cluster=staging.DEFAULT_CLUSTER,
            owner_id="owner-b",
            source_commit=None,
        )
        with tempfile.TemporaryDirectory(prefix="staging-cell-owner-pin-") as tmp_name:
            root = Path(tmp_name)
            self._write_bound_receipt(root, owner="owner-a", commit=persisted_commit)
            with (
                mock.patch.object(staging, "state_root", return_value=root),
                mock.patch.object(staging, "configure_reference_paths"),
                mock.patch.object(staging, "load_tool_receipt", return_value=self._tool_receipt()),
                mock.patch.object(staging.reference, "clusters", return_value=[]),
                mock.patch.object(staging, "require_clean_commit") as commit_mock,
                mock.patch.object(staging.reference, "create_kind_cluster") as create_mock,
            ):
                with self.assertRaisesRegex(staging.StagingCellError, "persisted cluster owner"):
                    staging.command_up(args)
        commit_mock.assert_not_called()
        create_mock.assert_not_called()

    def test_existing_cluster_commit_check_is_receipt_bound_without_remote_lookup(self) -> None:
        head = "a" * 40

        def fake_output(argv: list[str], *, timeout: int | None = None) -> str:
            del timeout
            if argv[:3] == ["git", "status", "--porcelain"]:
                return ""
            if argv[:3] == ["git", "rev-parse", "HEAD"]:
                return head
            self.fail(f"unexpected command: {argv}")

        with mock.patch.object(staging, "output", side_effect=fake_output):
            observed = staging.require_clean_commit(
                None,
                expected_commit=head,
                require_public_main=False,
            )
        self.assertEqual(observed, head)

    def test_status_degrades_when_external_secret_binding_is_invalid(self) -> None:
        owner = "owner-a"
        commit = "d" * 40
        args = argparse.Namespace(cluster=staging.DEFAULT_CLUSTER)
        with tempfile.TemporaryDirectory(prefix="staging-cell-status-secret-") as tmp_name:
            root = Path(tmp_name)
            self._write_bound_receipt(root, owner=owner, commit=commit)
            with (
                mock.patch.object(staging, "state_root", return_value=root),
                mock.patch.object(staging, "configure_reference_paths"),
                mock.patch.object(
                    staging, "load_tool_receipt", return_value=self._tool_receipt()
                ) as tool_receipt_mock,
                mock.patch.object(
                    staging.reference,
                    "clusters",
                    return_value=[staging.DEFAULT_CLUSTER],
                ),
                mock.patch.object(staging.reference, "require_owned_cluster"),
                mock.patch.object(
                    staging,
                    "output",
                    side_effect=[
                        f"main@sha1:{commit}",
                        "1|1|True",
                        f"1|1|True|main@sha1:{commit}",
                        "Bound",
                        "Bound",
                    ],
                ),
                mock.patch.object(
                    staging,
                    "verify_external_secret_binding",
                    side_effect=staging.StagingCellError("secret source missing"),
                ),
                mock.patch.object(
                    staging,
                    "image_promotion_state",
                    return_value={"status": "blocked"},
                ),
            ):
                result = staging.command_status(args)

        tool_receipt_mock.assert_called_once_with(
            root, required_tools=("kind", "kubectl"), required_artifacts=()
        )
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(
            result["external_secret"],
            {"database": False, "runtime": False, "ready": False},
        )

    def test_status_degrades_when_external_secret_source_is_malformed_json(self) -> None:
        owner = "owner-a"
        commit = "d" * 40
        args = argparse.Namespace(cluster=staging.DEFAULT_CLUSTER)
        cases = (
            ("invalid-syntax", '{"schema_version": 1,'),
            ("array-root", "[]"),
            ("null-root", "null"),
        )
        for label, secret_text in cases:
            with self.subTest(label=label):
                with tempfile.TemporaryDirectory(
                    prefix=f"staging-cell-status-malformed-secret-{label}-"
                ) as tmp_name:
                    root = Path(tmp_name)
                    self._write_bound_receipt(root, owner=owner, commit=commit)
                    secret_path = root / "secrets/staging-runtime.json"
                    secret_path.write_text(secret_text, encoding="utf-8")
                    secret_path.chmod(0o600)
                    with (
                        mock.patch.object(staging, "state_root", return_value=root),
                        mock.patch.object(staging, "configure_reference_paths"),
                        mock.patch.object(
                            staging,
                            "load_tool_receipt",
                            return_value=self._tool_receipt(),
                        ),
                        mock.patch.object(
                            staging.reference,
                            "clusters",
                            return_value=[staging.DEFAULT_CLUSTER],
                        ),
                        mock.patch.object(staging.reference, "require_owned_cluster"),
                        mock.patch.object(
                            staging,
                            "output",
                            side_effect=[
                                f"main@sha1:{commit}",
                                "1|1|True",
                                f"1|1|True|main@sha1:{commit}",
                                "Bound",
                                "Bound",
                            ],
                        ),
                        mock.patch.object(
                            staging,
                            "image_promotion_state",
                            return_value={"status": "blocked"},
                        ),
                    ):
                        result = staging.command_status(args)

                self.assertEqual(result["status"], "degraded")
                self.assertEqual(
                    result["external_secret"],
                    {"database": False, "runtime": False, "ready": False},
                )

    def test_status_degrades_when_injected_secret_is_missing(self) -> None:
        owner = "owner-a"
        commit = "d" * 40
        args = argparse.Namespace(cluster=staging.DEFAULT_CLUSTER)
        with tempfile.TemporaryDirectory(prefix="staging-cell-status-missing-secret-") as tmp_name:
            root = Path(tmp_name)
            self._write_bound_receipt(root, owner=owner, commit=commit)
            with (
                mock.patch.object(staging, "state_root", return_value=root),
                mock.patch.object(staging, "configure_reference_paths"),
                mock.patch.object(staging, "load_tool_receipt", return_value=self._tool_receipt()),
                mock.patch.object(staging.reference, "clusters", return_value=[staging.DEFAULT_CLUSTER]),
                mock.patch.object(staging.reference, "require_owned_cluster"),
                mock.patch.object(
                    staging,
                    "output",
                    side_effect=[
                        f"main@sha1:{commit}",
                        "1|1|True",
                        f"1|1|True|main@sha1:{commit}",
                        "Bound",
                        "Bound",
                    ],
                ),
                mock.patch.object(
                    staging,
                    "verify_external_secret_binding",
                    side_effect=subprocess.CalledProcessError(
                        1, ["kubectl", "get", "secret"]
                    ),
                ),
                mock.patch.object(
                    staging, "image_promotion_state", return_value={"status": "blocked"}
                ),
            ):
                result = staging.command_status(args)
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(
            result["external_secret"],
            {"database": False, "runtime": False, "ready": False},
        )

    def test_down_cleans_exactly_bound_marker_when_cluster_is_absent(self) -> None:
        owner = "owner-a"
        commit = "e" * 40
        args = argparse.Namespace(cluster=staging.DEFAULT_CLUSTER, owner_id=owner)
        with tempfile.TemporaryDirectory(prefix="staging-cell-down-absent-") as tmp_name:
            root = Path(tmp_name)
            self._write_bound_receipt(root, owner=owner, commit=commit)
            with (
                mock.patch.object(staging, "state_root", return_value=root),
                mock.patch.object(staging, "configure_reference_paths"),
                mock.patch.object(
                    staging, "load_tool_receipt", return_value=self._tool_receipt()
                ) as tool_receipt_mock,
                mock.patch.object(staging.reference, "clusters", return_value=[]),
                mock.patch.object(
                    staging.reference,
                    "delete_owned_cluster_if_present",
                    return_value=True,
                ) as delete_mock,
            ):
                result = staging.command_down(args)
        tool_receipt_mock.assert_called_once_with(
            root, required_tools=("kind",), required_artifacts=()
        )
        delete_mock.assert_called_once_with(
            "kind",
            staging.DEFAULT_CLUSTER,
            expected_commit=commit,
            expected_owner_id=owner,
        )
        self.assertEqual(result["status"], "cluster-absent-state-preserved")

    def test_down_fails_closed_for_wrong_owner_or_marker_binding(self) -> None:
        owner = "owner-a"
        commit = "e" * 40
        with tempfile.TemporaryDirectory(prefix="staging-cell-down-binding-") as tmp_name:
            root = Path(tmp_name)
            self._write_bound_receipt(root, owner=owner, commit=commit)
            with (
                mock.patch.object(staging, "state_root", return_value=root),
                mock.patch.object(staging, "configure_reference_paths"),
                mock.patch.object(staging, "load_tool_receipt", return_value=self._tool_receipt()),
                mock.patch.object(staging.reference, "clusters", return_value=[]),
                mock.patch.object(
                    staging.reference, "delete_owned_cluster_if_present"
                ) as delete_mock,
            ):
                with self.assertRaisesRegex(
                    staging.StagingCellError, "persisted cluster owner"
                ):
                    staging.command_down(
                        argparse.Namespace(
                            cluster=staging.DEFAULT_CLUSTER, owner_id="owner-b"
                        )
                    )
            delete_mock.assert_not_called()

            with (
                mock.patch.object(staging, "state_root", return_value=root),
                mock.patch.object(staging, "configure_reference_paths"),
                mock.patch.object(staging, "load_tool_receipt", return_value=self._tool_receipt()),
                mock.patch.object(staging.reference, "clusters", return_value=[]),
                mock.patch.object(
                    staging.reference,
                    "delete_owned_cluster_if_present",
                    side_effect=staging.reference.ProofError("marker binding mismatch"),
                ),
            ):
                with self.assertRaisesRegex(
                    staging.reference.ProofError, "marker binding mismatch"
                ):
                    staging.command_down(
                        argparse.Namespace(
                            cluster=staging.DEFAULT_CLUSTER, owner_id=owner
                        )
                    )
        self.assertFalse((root / "receipts/cell-down.json").exists())

    def test_lifecycle_lock_rejects_parallel_mutations(self) -> None:
        with tempfile.TemporaryDirectory(prefix="staging-cell-lifecycle-lock-") as tmp_name:
            root = Path(tmp_name)
            with staging.lifecycle_lock(root):
                with self.assertRaisesRegex(
                    staging.StagingCellError, "lifecycle mutation is already in progress"
                ):
                    with staging.lifecycle_lock(root):
                        self.fail("parallel lifecycle lock unexpectedly acquired")
            with staging.lifecycle_lock(root):
                pass

    def test_status_degrades_when_flux_or_pvc_resources_are_missing(self) -> None:
        owner = "owner-a"
        commit = "f" * 40
        args = argparse.Namespace(cluster=staging.DEFAULT_CLUSTER)
        with tempfile.TemporaryDirectory(prefix="staging-cell-status-missing-resources-") as tmp_name:
            root = Path(tmp_name)
            self._write_bound_receipt(root, owner=owner, commit=commit)
            with (
                mock.patch.object(staging, "state_root", return_value=root),
                mock.patch.object(staging, "configure_reference_paths"),
                mock.patch.object(staging, "load_tool_receipt", return_value=self._tool_receipt()),
                mock.patch.object(staging.reference, "clusters", return_value=[staging.DEFAULT_CLUSTER]),
                mock.patch.object(staging.reference, "require_owned_cluster"),
                mock.patch.object(staging, "output", side_effect=["", "", "", "", ""]) as output_mock,
                mock.patch.object(
                    staging,
                    "verify_external_secret_binding",
                    return_value={"database": True, "runtime": True, "ready": True},
                ),
                mock.patch.object(
                    staging, "image_promotion_state", return_value={"status": "blocked"}
                ),
            ):
                result = staging.command_status(args)
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["source_revision"], "missing")
        self.assertEqual(result["data_ready"], "missing")
        self.assertEqual(
            result["pvcs"],
            {"postgres-data": "missing", "nats-data": "missing"},
        )
        for call in output_mock.call_args_list:
            self.assertIn("--ignore-not-found", call.args[0])

    def test_atomic_writes_fsync_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="staging-cell-fsync-parent-") as tmp_name:
            root = Path(tmp_name)
            json_path = root / "receipt.json"
            text_path = root / "receipt.txt"
            with mock.patch.object(staging, "fsync_directory") as fsync_mock:
                staging.atomic_json(json_path, {"schema_version": 1})
                fsync_mock.assert_called_once_with(root)
            with mock.patch.object(staging, "fsync_directory") as fsync_mock:
                staging.atomic_text(text_path, "ok\n")
                fsync_mock.assert_called_once_with(root)

    def test_atomic_write_fsyncs_new_parent_directory_entry(self) -> None:
        with tempfile.TemporaryDirectory(prefix="staging-cell-fsync-new-parent-") as tmp_name:
            root = Path(tmp_name)
            receipt_path = root / "receipts/cell.json"
            with mock.patch.object(staging, "fsync_directory") as fsync_mock:
                staging.atomic_json(receipt_path, {"schema_version": 1})
        self.assertEqual(
            fsync_mock.call_args_list,
            [mock.call(root), mock.call(root / "receipts")],
        )

    def test_status_reports_not_bootstrapped_without_toolchain(self) -> None:
        args = argparse.Namespace(cluster=staging.DEFAULT_CLUSTER)
        with tempfile.TemporaryDirectory(prefix="staging-cell-not-bootstrapped-") as tmp_name:
            root = Path(tmp_name)
            with (
                mock.patch.object(staging, "state_root", return_value=root),
                mock.patch.object(staging, "configure_reference_paths"),
                mock.patch.object(staging, "load_tool_receipt") as tool_receipt_mock,
            ):
                result = staging.command_status(args)
        self.assertEqual(result["status"], "not-bootstrapped")
        tool_receipt_mock.assert_not_called()

    def test_status_requires_current_gitrepository_ready_condition(self) -> None:
        owner = "owner-a"
        commit = "7" * 40
        args = argparse.Namespace(cluster=staging.DEFAULT_CLUSTER)
        with tempfile.TemporaryDirectory(prefix="staging-cell-source-ready-") as tmp_name:
            root = Path(tmp_name)
            self._write_bound_receipt(root, owner=owner, commit=commit)
            with (
                mock.patch.object(staging, "state_root", return_value=root),
                mock.patch.object(staging, "configure_reference_paths"),
                mock.patch.object(staging, "load_tool_receipt", return_value=self._tool_receipt()),
                mock.patch.object(staging.reference, "clusters", return_value=[staging.DEFAULT_CLUSTER]),
                mock.patch.object(staging.reference, "require_owned_cluster"),
                mock.patch.object(
                    staging,
                    "output",
                    side_effect=[
                        f"main@sha1:{commit}",
                        "3|3|False",
                        f"1|1|True|main@sha1:{commit}",
                        "Bound",
                        "Bound",
                    ],
                ),
                mock.patch.object(
                    staging,
                    "verify_external_secret_binding",
                    return_value={"database": True, "runtime": True, "ready": True},
                ),
                mock.patch.object(
                    staging, "image_promotion_state", return_value={"status": "blocked"}
                ),
            ):
                result = staging.command_status(args)
        self.assertEqual(result["status"], "degraded")
        self.assertTrue(result["source_matches_commit"])
        self.assertEqual(result["source_ready"], "False")

    def test_status_binds_data_kustomization_to_current_revision(self) -> None:
        owner = "owner-a"
        commit = "6" * 40
        other_commit = "8" * 40
        args = argparse.Namespace(cluster=staging.DEFAULT_CLUSTER)
        cases = (
            (
                "stale-generation",
                f"4|3|True|main@sha1:{commit}",
                "degraded",
                "stale",
                True,
            ),
            (
                "wrong-revision",
                f"4|4|True|main@sha1:{other_commit}",
                "degraded",
                "True",
                False,
            ),
            (
                "current-revision",
                f"4|4|True|main@sha1:{commit}",
                "ready",
                "True",
                True,
            ),
        )
        for name, data_health, expected_status, expected_ready, expected_match in cases:
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory(
                    prefix=f"staging-cell-data-status-{name}-"
                ) as tmp_name:
                    root = Path(tmp_name)
                    self._write_bound_receipt(root, owner=owner, commit=commit)
                    with (
                        mock.patch.object(staging, "state_root", return_value=root),
                        mock.patch.object(staging, "configure_reference_paths"),
                        mock.patch.object(
                            staging,
                            "load_tool_receipt",
                            return_value=self._tool_receipt(),
                        ),
                        mock.patch.object(
                            staging.reference,
                            "clusters",
                            return_value=[staging.DEFAULT_CLUSTER],
                        ),
                        mock.patch.object(staging.reference, "require_owned_cluster"),
                        mock.patch.object(
                            staging,
                            "output",
                            side_effect=[
                                f"main@sha1:{commit}",
                                "1|1|True",
                                data_health,
                                "Bound",
                                "Bound",
                            ],
                        ),
                        mock.patch.object(
                            staging,
                            "verify_external_secret_binding",
                            return_value={
                                "database": True,
                                "runtime": True,
                                "ready": True,
                            },
                        ),
                        mock.patch.object(
                            staging,
                            "staging_live_health",
                            return_value={
                                name: "True" for name in staging.LIVE_DEPLOYMENTS
                            },
                        ),
                        mock.patch.object(
                            staging,
                            "image_promotion_state",
                            return_value={"status": "blocked"},
                        ),
                    ):
                        result = staging.command_status(args)
                self.assertEqual(result["status"], expected_status)
                self.assertEqual(result["data_ready"], expected_ready)
                self.assertEqual(result["data_matches_commit"], expected_match)
                self.assertEqual(
                    result["data_revision"], data_health.rsplit("|", 1)[-1]
                )

    def test_pvc_wait_batches_queries_and_starts_bind_budget_after_visibility(self) -> None:
        missing = json.dumps({"apiVersion": "v1", "kind": "List", "items": []})
        pending = json.dumps(
            {
                "apiVersion": "v1",
                "kind": "List",
                "items": [
                    {
                        "metadata": {"name": "postgres-data"},
                        "status": {"phase": "Pending"},
                    },
                    {
                        "metadata": {"name": "nats-data"},
                        "status": {"phase": "Pending"},
                    },
                ],
            }
        )
        bound = json.dumps(
            {
                "apiVersion": "v1",
                "kind": "List",
                "items": [
                    {
                        "metadata": {"name": "postgres-data"},
                        "status": {"phase": "Bound"},
                    },
                    {
                        "metadata": {"name": "nats-data"},
                        "status": {"phase": "Bound"},
                    },
                ],
            }
        )
        with (
            mock.patch.object(staging, "output", side_effect=[missing, pending, bound]) as output_mock,
            mock.patch.object(staging.time, "monotonic", side_effect=[0.0, 100.0, 150.0]),
            mock.patch.object(staging.time, "sleep"),
        ):
            staging.wait_pvcs_bound(
                "kubectl",
                visibility_timeout_seconds=200.0,
                bind_timeout_seconds=10.0,
            )
        self.assertEqual(output_mock.call_count, 3)
        for call in output_mock.call_args_list:
            argv = call.args[0]
            self.assertEqual(argv.count("postgres-data"), 1)
            self.assertEqual(argv.count("nats-data"), 1)
            self.assertIn("json", argv)
            self.assertFalse(any(str(value).startswith("jsonpath=") for value in argv))

    def test_pvc_wait_keeps_bind_deadline_after_visible_claim_disappears(self) -> None:
        pending = json.dumps(
            {
                "apiVersion": "v1",
                "kind": "List",
                "items": [
                    {
                        "metadata": {"name": "postgres-data"},
                        "status": {"phase": "Pending"},
                    },
                    {
                        "metadata": {"name": "nats-data"},
                        "status": {"phase": "Pending"},
                    },
                ],
            }
        )
        one_missing = json.dumps(
            {
                "apiVersion": "v1",
                "kind": "List",
                "items": [
                    {
                        "metadata": {"name": "postgres-data"},
                        "status": {"phase": "Pending"},
                    }
                ],
            }
        )
        with (
            mock.patch.object(staging, "output", side_effect=[pending, one_missing]),
            mock.patch.object(
                staging.time, "monotonic", side_effect=[0.0, 1.0, 12.0]
            ),
            mock.patch.object(staging.time, "sleep"),
        ):
            with self.assertRaisesRegex(
                staging.StagingCellError,
                "did not bind within 10s after becoming visible",
            ):
                staging.wait_pvcs_bound(
                    "kubectl",
                    visibility_timeout_seconds=100.0,
                    bind_timeout_seconds=10.0,
                )

    def test_pvc_wait_uses_flux_budget_before_claims_exist(self) -> None:
        missing = json.dumps({"apiVersion": "v1", "kind": "List", "items": []})
        with (
            mock.patch.object(staging, "output", side_effect=[missing, missing]),
            mock.patch.object(staging.time, "monotonic", side_effect=[0.0, 50.0, 101.0]),
            mock.patch.object(staging.time, "sleep"),
        ):
            with self.assertRaisesRegex(
                staging.StagingCellError, "Flux Kustomization budget"
            ):
                staging.wait_pvcs_bound(
                    "kubectl",
                    visibility_timeout_seconds=100.0,
                    bind_timeout_seconds=1.0,
                )

    def test_flux_wait_requires_current_generation_exact_revision_and_request(self) -> None:
        commit = "a" * 40
        requested_at = "staging-up-123"
        states = [
            {
                "ready": "True",
                "revision": f"main@sha1:{commit}",
                "matches_commit": True,
                "current_generation": True,
                "last_handled_reconcile_at": "older-request",
            },
            {
                "ready": "stale",
                "revision": f"main@sha1:{commit}",
                "matches_commit": True,
                "current_generation": False,
                "last_handled_reconcile_at": requested_at,
            },
            {
                "ready": "True",
                "revision": f"main@sha1:{'b' * 40}",
                "matches_commit": False,
                "current_generation": True,
                "last_handled_reconcile_at": requested_at,
            },
            {
                "ready": "True",
                "revision": f"main@sha1:{commit}",
                "matches_commit": True,
                "current_generation": True,
                "last_handled_reconcile_at": requested_at,
            },
        ]
        with (
            mock.patch.object(
                staging, "flux_resource_current_state", side_effect=states
            ) as state_mock,
            mock.patch.object(
                staging.time, "monotonic", side_effect=[0.0, 1.0, 2.0, 3.0]
            ),
            mock.patch.object(staging.time, "sleep") as sleep_mock,
        ):
            result = staging.wait_flux_resource_current(
                "kubectl",
                "kustomization",
                staging.DATA_KUSTOMIZATION,
                commit,
                timeout_seconds=10.0,
                requested_at=requested_at,
            )
        self.assertTrue(result["matches_commit"])
        self.assertEqual(result["ready"], "True")
        self.assertEqual(result["last_handled_reconcile_at"], requested_at)
        self.assertEqual(state_mock.call_count, 4)
        self.assertEqual(sleep_mock.call_count, 3)

    def test_flux_state_reads_last_handled_reconcile_token(self) -> None:
        commit = "c" * 40
        requested_at = "staging-up-456"
        payload = {
            "metadata": {"generation": 2},
            "status": {
                "observedGeneration": 2,
                "conditions": [{"type": "Ready", "status": "True"}],
                "lastAppliedRevision": f"main@sha1:{commit}",
                "lastHandledReconcileAt": requested_at,
            },
        }
        with mock.patch.object(staging, "output", return_value=json.dumps(payload)):
            state = staging.flux_resource_current_state(
                "kubectl", "kustomization", staging.DATA_KUSTOMIZATION, commit
            )
        self.assertEqual(state["ready"], "True")
        self.assertTrue(state["matches_commit"])
        self.assertEqual(state["last_handled_reconcile_at"], requested_at)

    def test_request_flux_reconcile_uses_requested_at_annotation(self) -> None:
        requested_at = "staging-up-789"
        with mock.patch.object(staging, "run") as run_mock:
            staging.request_flux_reconcile(
                "kubectl", "gitrepository", staging.SOURCE_NAME, requested_at
            )
        run_mock.assert_called_once_with(
            [
                "kubectl",
                "-n",
                "flux-system",
                "annotate",
                f"gitrepository/{staging.SOURCE_NAME}",
                f"reconcile.fluxcd.io/requestedAt={requested_at}",
                "--field-manager=flux-client-side-apply",
                "--overwrite",
            ],
            timeout=30,
        )

    def test_reconcile_data_keeps_pvc_fail_fast_inside_shared_budget(self) -> None:
        events: list[str] = []
        commit = "d" * 40
        requested_at = "staging-up-123456789"
        wait_calls: list[tuple[str, float, str | None]] = []

        def request(
            _kubectl: str, resource: str, name: str, token: str
        ) -> None:
            self.assertEqual(token, requested_at)
            events.append(f"request:{resource}/{name}")

        def wait_flux(
            _kubectl: str,
            resource: str,
            name: str,
            observed_commit: str,
            *,
            timeout_seconds: float = staging.DATA_KUSTOMIZATION_TIMEOUT_SECONDS,
            requested_at: str | None = None,
        ) -> dict[str, object]:
            self.assertEqual(observed_commit, commit)
            wait_calls.append((resource, timeout_seconds, requested_at))
            events.append(f"wait:{resource}/{name}")
            return {
                "ready": "True",
                "revision": f"main@sha1:{commit}",
                "matches_commit": True,
                "current_generation": True,
                "last_handled_reconcile_at": requested_at,
            }

        def wait_pvcs(_kubectl: str, **kwargs: float) -> None:
            self.assertEqual(
                kwargs["visibility_timeout_seconds"],
                staging.DATA_KUSTOMIZATION_TIMEOUT_SECONDS,
            )
            events.append("pvcs-bound")

        with (
            mock.patch.object(staging.time, "time_ns", return_value=123456789),
            mock.patch.object(
                staging.time, "monotonic", side_effect=[100.0, 100.0, 120.0]
            ),
            mock.patch.object(staging, "request_flux_reconcile", side_effect=request),
            mock.patch.object(
                staging, "wait_flux_resource_current", side_effect=wait_flux
            ),
            mock.patch.object(staging, "wait_pvcs_bound", side_effect=wait_pvcs),
        ):
            token = staging.reconcile_data("kubectl", commit)

        self.assertEqual(token, requested_at)
        self.assertEqual(
            events,
            [
                f"request:gitrepository/{staging.SOURCE_NAME}",
                f"wait:gitrepository/{staging.SOURCE_NAME}",
                f"request:kustomization/{staging.DATA_KUSTOMIZATION}",
                "pvcs-bound",
                f"wait:kustomization/{staging.DATA_KUSTOMIZATION}",
            ],
        )
        self.assertEqual(wait_calls[0][2], requested_at)
        self.assertEqual(wait_calls[1], ("kustomization", 460.0, requested_at))

    def test_deployment_ready_state_requires_current_live_replicas(self) -> None:
        healthy = {
            "metadata": {"generation": 3},
            "spec": {"replicas": 1},
            "status": {
                "observedGeneration": 3,
                "availableReplicas": 1,
                "readyReplicas": 1,
                "updatedReplicas": 1,
            },
        }
        stale = json.loads(json.dumps(healthy))
        stale["status"]["observedGeneration"] = 2
        unavailable = json.loads(json.dumps(healthy))
        unavailable["status"]["availableReplicas"] = 0
        for payload, expected in (
            (healthy, "True"),
            (stale, "stale"),
            (unavailable, "False"),
        ):
            with self.subTest(expected=expected):
                with mock.patch.object(
                    staging, "output", return_value=json.dumps(payload)
                ):
                    observed = staging.deployment_ready_state(
                        "kubectl", "flux-system", "source-controller"
                    )
                self.assertEqual(observed, expected)

    def test_status_degrades_when_live_workload_is_unavailable(self) -> None:
        owner = "owner-a"
        commit = "9" * 40
        args = argparse.Namespace(cluster=staging.DEFAULT_CLUSTER)
        live = {name: "True" for name in staging.LIVE_DEPLOYMENTS}
        live["nats"] = "False"
        with tempfile.TemporaryDirectory(prefix="staging-cell-live-status-") as tmp_name:
            root = Path(tmp_name)
            self._write_bound_receipt(root, owner=owner, commit=commit)
            with (
                mock.patch.object(staging, "state_root", return_value=root),
                mock.patch.object(staging, "configure_reference_paths"),
                mock.patch.object(
                    staging, "load_tool_receipt", return_value=self._tool_receipt()
                ),
                mock.patch.object(
                    staging.reference,
                    "clusters",
                    return_value=[staging.DEFAULT_CLUSTER],
                ),
                mock.patch.object(staging.reference, "require_owned_cluster"),
                mock.patch.object(
                    staging,
                    "output",
                    side_effect=[
                        f"main@sha1:{commit}",
                        "1|1|True",
                        f"1|1|True|main@sha1:{commit}",
                        "Bound",
                        "Bound",
                    ],
                ),
                mock.patch.object(
                    staging,
                    "verify_external_secret_binding",
                    return_value={
                        "database": True,
                        "runtime": True,
                        "ready": True,
                    },
                ),
                mock.patch.object(
                    staging, "staging_live_health", return_value=live
                ),
                mock.patch.object(
                    staging, "image_promotion_state", return_value={"status": "blocked"}
                ),
            ):
                result = staging.command_status(args)
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["live_workloads"]["nats"], "False")

    def test_volume_permissions_verify_single_mount_and_do_not_touch_healthy_data(self) -> None:
        nodes = [
            f"{staging.DEFAULT_CLUSTER}-control-plane",
            staging.data_node_name(staging.DEFAULT_CLUSTER),
            f"{staging.DEFAULT_CLUSTER}-worker2",
        ]
        with tempfile.TemporaryDirectory(prefix="staging-cell-volume-proof-") as tmp_name:
            root = Path(tmp_name)
            (root / "data").mkdir()
            expected_source = str((root / "data").resolve())

            def fake_output(argv: list[str], *, timeout: int | None = None) -> str:
                del timeout
                if argv[:2] == ["docker", "inspect"]:
                    node = argv[-1]
                    mounts = (
                        [
                            {
                                "Destination": "/var/local/weltgewebe-staging",
                                "Source": expected_source,
                                "RW": True,
                            }
                        ]
                        if node == staging.data_node_name(staging.DEFAULT_CLUSTER)
                        else []
                    )
                    return json.dumps(mounts)
                if "stat" in argv:
                    volume_path = argv[-1]
                    if volume_path.endswith("/postgres"):
                        return "999:999:770"
                    if volume_path.endswith("/nats"):
                        return "1000:1000:2770"
                self.fail(f"unexpected command: {argv}")

            with (
                mock.patch.object(staging.reference, "kind_nodes", return_value=nodes),
                mock.patch.object(staging, "run") as run_mock,
                mock.patch.object(staging, "output", side_effect=fake_output) as output_mock,
            ):
                staging.prepare_volume_permissions("kind", staging.DEFAULT_CLUSTER, root)

        self.assertEqual(run_mock.call_count, 2)
        self.assertEqual(output_mock.call_count, 5)
        flattened = [str(item) for call in run_mock.call_args_list for item in call.args[0]]
        self.assertNotIn("chown", flattened)
        self.assertNotIn("chmod", flattened)

    def test_volume_permission_initialization_never_uses_recursive_chown(self) -> None:
        nodes = [
            f"{staging.DEFAULT_CLUSTER}-control-plane",
            staging.data_node_name(staging.DEFAULT_CLUSTER),
            f"{staging.DEFAULT_CLUSTER}-worker2",
        ]
        with tempfile.TemporaryDirectory(prefix="staging-cell-volume-init-") as tmp_name:
            root = Path(tmp_name)
            (root / "data").mkdir()
            expected_source = str((root / "data").resolve())
            stat_calls = {"postgres": 0, "nats": 0}

            def fake_output(argv: list[str], *, timeout: int | None = None) -> str:
                del timeout
                if argv[:2] == ["docker", "inspect"]:
                    node = argv[-1]
                    mounts = (
                        [
                            {
                                "Destination": "/var/local/weltgewebe-staging",
                                "Source": expected_source,
                                "RW": True,
                            }
                        ]
                        if node == staging.data_node_name(staging.DEFAULT_CLUSTER)
                        else []
                    )
                    return json.dumps(mounts)
                if "find" in argv:
                    return ""
                if "stat" in argv:
                    volume = "postgres" if argv[-1].endswith("/postgres") else "nats"
                    stat_calls[volume] += 1
                    if stat_calls[volume] == 1:
                        return "0:0:755"
                    return "999:999:700" if volume == "postgres" else "1000:1000:700"
                self.fail(f"unexpected command: {argv}")

            with (
                mock.patch.object(staging.reference, "kind_nodes", return_value=nodes),
                mock.patch.object(staging, "run") as run_mock,
                mock.patch.object(staging, "output", side_effect=fake_output),
            ):
                staging.prepare_volume_permissions("kind", staging.DEFAULT_CLUSTER, root)

        commands = [call.args[0] for call in run_mock.call_args_list]
        chowns = [argv for argv in commands if "chown" in argv]
        self.assertEqual(len(chowns), 2)
        self.assertTrue(all("-R" not in argv for argv in chowns))
        self.assertEqual(len([argv for argv in commands if "chmod" in argv]), 2)

    def test_nats_only_retained_state_blocks_unbound_recreation(self) -> None:
        args = argparse.Namespace(
            cluster=staging.DEFAULT_CLUSTER,
            owner_id="owner",
            source_commit=None,
        )
        with tempfile.TemporaryDirectory(prefix="staging-cell-nats-retained-") as tmp_name:
            root = Path(tmp_name)
            nats = root / "data/nats"
            nats.mkdir(parents=True)
            (nats / "jetstream.marker").write_text("retained", encoding="utf-8")
            with (
                mock.patch.object(staging, "state_root", return_value=root),
                mock.patch.object(staging, "configure_reference_paths"),
                mock.patch.object(
                    staging, "load_tool_receipt", return_value=self._tool_receipt()
                ),
                mock.patch.object(staging.reference, "clusters", return_value=[]),
                mock.patch.object(staging, "require_clean_commit") as commit_mock,
                mock.patch.object(
                    staging.reference, "create_kind_cluster"
                ) as create_mock,
            ):
                with self.assertRaisesRegex(
                    staging.StagingCellError, "without a bootstrap receipt"
                ):
                    staging.command_up(args)
        commit_mock.assert_not_called()
        create_mock.assert_not_called()

    def test_retained_postgres_permission_error_is_preservation_evidence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="staging-cell-retained-permission-") as tmp_name:
            root = Path(tmp_name)
            pgdata = root / "data/postgres"
            pgdata.mkdir(parents=True)
            with mock.patch.object(staging.os, "scandir", side_effect=PermissionError):
                self.assertTrue(staging.retained_postgres_state_exists(root))


    def test_cluster_repository_binding_accepts_legacy_worktree_of_same_repo(self) -> None:
        commit = "a" * 40
        owner = "owner-proof"
        with tempfile.TemporaryDirectory(prefix="staging-marker-repo-") as tmp_name:
            legacy = Path(tmp_name) / "legacy-worktree"
            legacy.mkdir()
            marker = {
                "schema_version": 2,
                "cluster": "proof",
                "repository": str(legacy),
                "commit": commit,
                "owner_id": owner,
            }
            with mock.patch.object(
                staging.reference,
                "repository_common_dir",
                return_value="/repo/commonthing/.git",
            ):
                staging.reference._require_marker_binding(
                    marker,
                    "proof",
                    expected_commit=commit,
                    expected_owner_id=owner,
                )

    def test_cluster_repository_binding_rejects_different_repo(self) -> None:
        commit = "a" * 40
        owner = "owner-proof"
        with tempfile.TemporaryDirectory(prefix="staging-marker-foreign-") as tmp_name:
            legacy = Path(tmp_name) / "foreign-worktree"
            legacy.mkdir()
            marker = {
                "schema_version": 2,
                "cluster": "proof",
                "repository": str(legacy),
                "commit": commit,
                "owner_id": owner,
            }

            def common_dir(root=staging.reference.ROOT):
                return (
                    "/repo/commonthing/.git"
                    if Path(root) == staging.reference.ROOT
                    else "/repo/foreign/.git"
                )

            with mock.patch.object(
                staging.reference, "repository_common_dir", side_effect=common_dir
            ):
                with self.assertRaisesRegex(
                    staging.reference.ProofError, "exact owner binding"
                ):
                    staging.reference._require_marker_binding(
                        marker,
                        "proof",
                        expected_commit=commit,
                        expected_owner_id=owner,
                    )

    def test_cluster_repository_normalization_preserves_owner_and_bootstrap(self) -> None:
        commit = "b" * 40
        owner = "owner-proof"
        with tempfile.TemporaryDirectory(prefix="staging-marker-normalize-") as tmp_name:
            root = Path(tmp_name)
            markers = root / "markers"
            markers.mkdir()
            legacy = root / "legacy-worktree"
            legacy.mkdir()
            original_markers = staging.reference.MARKERS
            staging.reference.MARKERS = markers
            try:
                marker_path = staging.reference.marker_path("proof")
                marker_path.write_text(
                    json.dumps(
                        {
                            "schema_version": 2,
                            "cluster": "proof",
                            "repository": str(legacy),
                            "commit": commit,
                            "owner_id": owner,
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )

                def common_dir(path=staging.reference.ROOT):
                    del path
                    return "/repo/commonthing/.git"

                with (
                    mock.patch.object(staging.reference, "clusters", return_value={"proof"}),
                    mock.patch.object(
                        staging.reference, "repository_common_dir", side_effect=common_dir
                    ),
                    mock.patch.object(staging.reference, "configure_cluster_access"),
                ):
                    result = staging.reference.normalize_owned_cluster_repository(
                        "kind",
                        "proof",
                        expected_commit=commit,
                        expected_owner_id=owner,
                    )
                stored = json.loads(marker_path.read_text(encoding="utf-8"))
                self.assertEqual(result["repository"], "/repo/commonthing/.git")
                self.assertEqual(stored["repository"], "/repo/commonthing/.git")
                self.assertEqual(stored["commit"], commit)
                self.assertEqual(stored["owner_id"], owner)
            finally:
                staging.reference.MARKERS = original_markers

    def test_active_commit_defaults_to_bootstrap_and_validates_override(self) -> None:
        bootstrap = "c" * 40
        active = "d" * 40
        self.assertEqual(
            staging.cell_active_commit({"bootstrap_commit": bootstrap}), bootstrap
        )
        self.assertEqual(
            staging.cell_active_commit(
                {"bootstrap_commit": bootstrap, "active_commit": active}
            ),
            active,
        )
        with self.assertRaisesRegex(staging.StagingCellError, "active_commit"):
            staging.cell_active_commit(
                {"bootstrap_commit": bootstrap, "active_commit": "not-a-commit"}
            )

    def test_promotion_receipt_binds_commit_and_digest_images(self) -> None:
        commit = "e" * 40
        api_digest = "sha256:" + "1" * 64
        web_digest = "sha256:" + "2" * 64
        with tempfile.TemporaryDirectory(prefix="staging-promotion-") as tmp_name:
            root = Path(tmp_name)
            directory = root / "promotion" / commit
            directory.mkdir(parents=True)
            receipt = directory / "receipt.json"
            payload = {
                "schema_version": 1,
                "status": "pass",
                "scope": "staging-only",
                "source_commit": commit,
                "repository": "heimgewebe/commonthing",
                "image_identity": "digest-authoritative",
                "production_activation": False,
                "images": {
                    "api": {
                        "canonical": "ghcr.io/heimgewebe/commonthing-api",
                        "digest": api_digest,
                        "canonical_reference": "ghcr.io/heimgewebe/commonthing-api@" + api_digest,
                    },
                    "web": {
                        "canonical": "ghcr.io/heimgewebe/commonthing-web",
                        "digest": web_digest,
                        "canonical_reference": "ghcr.io/heimgewebe/commonthing-web@" + web_digest,
                    },
                },
            }
            receipt.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            receipt.chmod(0o600)
            result = staging.load_promotion_receipt(root, commit)
            self.assertEqual(result["source_commit"], commit)
            self.assertEqual(
                result["images"]["api"],
                "ghcr.io/heimgewebe/commonthing-api@" + api_digest,
            )
            self.assertEqual(
                result["images"]["web"],
                "ghcr.io/heimgewebe/commonthing-web@" + web_digest,
            )
            payload["production_activation"] = True
            receipt.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            receipt.chmod(0o600)
            with self.assertRaisesRegex(staging.StagingCellError, "identity mismatch"):
                staging.load_promotion_receipt(root, commit)

    def test_registry_pull_material_is_external_owner_private_and_never_created(self) -> None:
        with tempfile.TemporaryDirectory(prefix="staging-registry-source-") as tmp_name:
            root = Path(tmp_name)
            with self.assertRaisesRegex(staging.StagingCellError, "credential source is missing"):
                staging.load_registry_pull_material(root)
            path = root / "secrets/staging-registry.json"
            path.parent.mkdir(parents=True)
            payload = {
                "schema_version": 1,
                "registry": staging.GHCR_REGISTRY,
                "username": "registry-user",
                "token": "token-value",
            }
            path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            path.chmod(0o600)
            material, source_sha = staging.load_registry_pull_material(root)
            self.assertEqual(material, {key: str(payload[key]) for key in ("registry", "username", "token")})
            self.assertEqual(source_sha, staging.sha256_file(path))
            path.chmod(0o640)
            with self.assertRaisesRegex(staging.StagingCellError, "mode-0600"):
                staging.load_registry_pull_material(root)

    def test_registry_pull_preflight_checks_exact_promoted_digests(self) -> None:
        api_digest = "sha256:" + "a" * 64
        web_digest = "sha256:" + "b" * 64
        promotion = {
            "images": {
                "api": "ghcr.io/heimgewebe/commonthing-api@" + api_digest,
                "web": "ghcr.io/heimgewebe/commonthing-web@" + web_digest,
            }
        }
        material = {
            "registry": staging.GHCR_REGISTRY,
            "username": "registry-user",
            "token": "registry-token",
        }

        class FakeResponse:
            def __init__(self, *, payload: dict | None = None, digest: str | None = None):
                self._payload = payload
                self.headers = {} if digest is None else {"Docker-Content-Digest": digest}

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                del exc_type, exc, tb
                return False

            def read(self) -> bytes:
                return json.dumps(self._payload or {}).encode("utf-8")

        responses = [
            FakeResponse(payload={"token": "bearer-api"}),
            FakeResponse(digest=api_digest),
            FakeResponse(payload={"token": "bearer-web"}),
            FakeResponse(digest=web_digest),
        ]
        with mock.patch.object(
            staging, "registry_urlopen", side_effect=responses
        ) as urlopen:
            result = staging.verify_ghcr_pull_access(material, promotion)
        self.assertEqual(result, {"api": True, "web": True})
        self.assertEqual(urlopen.call_count, 4)
        requests = [call.args[0] for call in urlopen.call_args_list]
        self.assertIn(
            "scope=repository%3Aheimgewebe%2Fcommonthing-api%3Apull",
            requests[0].full_url,
        )
        self.assertEqual(requests[1].get_method(), "HEAD")
        self.assertTrue(requests[1].full_url.endswith("/manifests/" + api_digest))
        self.assertIn(
            "scope=repository%3Aheimgewebe%2Fcommonthing-web%3Apull",
            requests[2].full_url,
        )
        self.assertEqual(requests[3].get_method(), "HEAD")
        self.assertTrue(requests[3].full_url.endswith("/manifests/" + web_digest))
        joined = " ".join(request.full_url for request in requests)
        self.assertNotIn(material["token"], joined)

        bad_responses = [
            FakeResponse(payload={"token": "bearer-api"}),
            FakeResponse(digest="sha256:" + "c" * 64),
        ]
        with mock.patch.object(
            staging, "registry_urlopen", side_effect=bad_responses
        ):
            with self.assertRaisesRegex(
                staging.StagingCellError, "digest mismatch"
            ):
                staging.verify_ghcr_pull_access(material, promotion)

    def test_registry_secret_binding_compares_exact_external_source(self) -> None:
        material = {
            "registry": staging.GHCR_REGISTRY,
            "username": "registry-user",
            "token": "token-value",
        }
        config = staging.registry_dockerconfig_json(material)
        source_sha = "9" * 64
        document = {
            "metadata": {
                "name": staging.REGISTRY_SECRET,
                "namespace": staging.APP_NAMESPACE,
                "annotations": {staging.REGISTRY_SOURCE_ANNOTATION: source_sha},
            },
            "type": "kubernetes.io/dockerconfigjson",
            "data": {
                ".dockerconfigjson": base64.b64encode(config.encode("utf-8")).decode("ascii")
            },
        }
        config_sha256 = staging.sha256_bytes(config.encode("utf-8"))
        self.assertTrue(
            staging.registry_secret_document_matches(
                document,
                source_sha=source_sha,
                expected_config_sha256=config_sha256,
            )
        )
        document["metadata"]["annotations"][
            "kubectl.kubernetes.io/last-applied-configuration"
        ] = "credential-copy"
        self.assertFalse(
            staging.registry_secret_document_matches(
                document,
                source_sha=source_sha,
                expected_config_sha256=config_sha256,
            )
        )
        document["metadata"]["annotations"].pop(
            "kubectl.kubernetes.io/last-applied-configuration"
        )
        document["metadata"]["annotations"][staging.REGISTRY_SOURCE_ANNOTATION] = "8" * 64
        self.assertFalse(
            staging.registry_secret_document_matches(
                document,
                source_sha=source_sha,
                expected_config_sha256=config_sha256,
            )
        )

    def test_activate_registry_preflight_fails_before_cluster_mutation(self) -> None:
        bootstrap = "1" * 40
        active = "2" * 40
        owner = "owner-a"
        args = argparse.Namespace(
            cluster=staging.DEFAULT_CLUSTER,
            owner_id=owner,
            source_commit=active,
        )
        promotion = {
            "source_commit": active,
            "receipt_sha256": "c" * 64,
            "images": {
                "api": "ghcr.io/heimgewebe/commonthing-api@sha256:" + "a" * 64,
                "web": "ghcr.io/heimgewebe/commonthing-web@sha256:" + "b" * 64,
            },
        }
        with tempfile.TemporaryDirectory(prefix="staging-registry-preflight-") as tmp_name:
            root = Path(tmp_name)
            with (
                mock.patch.object(staging, "state_root", return_value=root),
                mock.patch.object(staging, "configure_reference_paths"),
                mock.patch.object(staging, "load_tool_receipt", return_value=self._tool_receipt()),
                mock.patch.object(
                    staging,
                    "load_cell_receipt",
                    return_value={
                        "schema_version": 1,
                        "cluster": staging.DEFAULT_CLUSTER,
                        "owner_id": owner,
                        "bootstrap_commit": bootstrap,
                    },
                ),
                mock.patch.object(staging, "require_clean_commit", return_value=active),
                mock.patch.object(staging, "load_promotion_receipt", return_value=promotion),
                mock.patch.object(
                    staging,
                    "load_registry_pull_material",
                    return_value=(
                        {
                            "registry": staging.GHCR_REGISTRY,
                            "username": "registry-user",
                            "token": "bad-token",
                        },
                        "e" * 64,
                    ),
                ),
                mock.patch.object(
                    staging,
                    "verify_ghcr_pull_access",
                    side_effect=staging.StagingCellError("registry pull preflight failed"),
                ),
                mock.patch.object(
                    staging.reference, "normalize_owned_cluster_repository"
                ) as normalize,
                mock.patch.object(staging, "inject_external_secrets") as inject_external,
            ):
                with self.assertRaisesRegex(staging.StagingCellError, "registry pull preflight"):
                    staging.command_activate(args)
        normalize.assert_not_called()
        inject_external.assert_not_called()

    def test_staging_migration_network_isolation_reuses_exact_app_policies(self) -> None:
        documents = staging.migration_network_policy_documents()
        names = [document["metadata"]["name"] for document in documents]
        self.assertEqual(
            names,
            ["default-deny", "allow-dns", "allow-api-data-egress"],
        )
        self.assertTrue(
            all(
                document["apiVersion"] == "networking.k8s.io/v1"
                and document["kind"] == "NetworkPolicy"
                and document["metadata"]["namespace"] == staging.APP_NAMESPACE
                for document in documents
            )
        )
        default_deny, allow_dns, allow_data = documents
        self.assertEqual(set(default_deny["spec"]["policyTypes"]), {"Ingress", "Egress"})
        self.assertEqual(allow_dns["spec"]["policyTypes"], ["Egress"])
        self.assertEqual(
            allow_data["spec"]["podSelector"]["matchLabels"]["app.kubernetes.io/name"],
            "weltgewebe-api",
        )
        self.assertEqual(
            {entry["port"] for rule in allow_data["spec"]["egress"] for entry in rule["ports"]},
            {5432, 4222},
        )
        policy_uids = {name: f"uid-{index}" for index, name in enumerate(names)}
        policy_documents = [
            json.dumps(
                {
                    "metadata": {
                        "name": name,
                        "namespace": staging.APP_NAMESPACE,
                        "uid": policy_uids[name],
                    }
                }
            )
            for name in names
        ]
        with (
            mock.patch.object(staging, "apply_yaml") as apply_yaml,
            mock.patch.object(staging, "output", side_effect=policy_documents),
            mock.patch.object(
                staging,
                "wait_migration_network_policy_enforcement",
                return_value={
                    "processed": True,
                    "cilium_agent_count": 3,
                    "minimum_policy_revision": 11,
                },
            ) as wait_enforcement,
        ):
            result = staging.apply_migration_network_isolation("kubectl")
        apply_yaml.assert_called_once_with("kubectl", documents)
        wait_enforcement.assert_called_once_with("kubectl", policy_uids)
        self.assertEqual(result["policy_names"], names)
        self.assertEqual(result["policy_uids"], policy_uids)
        self.assertTrue(result["processed"])
        self.assertEqual(result["cilium_agent_count"], 3)
        self.assertEqual(result["minimum_policy_revision"], 11)

    def test_cilium_policy_repository_binds_exact_networkpolicy_uid(self) -> None:
        raw = json.dumps(
            [
                {
                    "Labels": [
                        {
                            "key": "io.cilium.k8s.policy.derived-from",
                            "value": "NetworkPolicy",
                            "source": "k8s",
                        },
                        {
                            "key": "io.cilium.k8s.policy.name",
                            "value": "default-deny",
                            "source": "k8s",
                        },
                        {
                            "key": "io.cilium.k8s.policy.namespace",
                            "value": staging.APP_NAMESPACE,
                            "source": "k8s",
                        },
                        {
                            "key": "io.cilium.k8s.policy.uid",
                            "value": "uid-default-deny",
                            "source": "k8s",
                        },
                    ]
                }
            ]
        ) + "\nRevision: 12"
        bindings, revision = staging.cilium_network_policy_bindings(raw)
        self.assertEqual(
            bindings,
            {("default-deny", staging.APP_NAMESPACE, "uid-default-deny")},
        )
        self.assertEqual(revision, 12)

    def test_cilium_agent_inventory_requires_ready_agent_on_every_node(self) -> None:
        nodes = json.dumps(
            {
                "items": [
                    {"metadata": {"name": "node-a"}},
                    {"metadata": {"name": "node-b"}},
                ]
            }
        )
        pods = json.dumps(
            {
                "items": [
                    {
                        "metadata": {"name": "cilium-a"},
                        "spec": {"nodeName": "node-a"},
                        "status": {
                            "phase": "Running",
                            "conditions": [{"type": "Ready", "status": "True"}],
                        },
                    }
                ]
            }
        )
        with mock.patch.object(staging, "output", side_effect=[nodes, pods]):
            with self.assertRaisesRegex(staging.StagingCellError, "coverage is incomplete"):
                staging.ready_cilium_agents("kubectl")

    def test_cilium_policy_wait_requires_exact_uid_on_every_ready_agent(self) -> None:
        expected = {
            "default-deny": "uid-default",
            "allow-dns": "uid-dns",
            "allow-api-data-egress": "uid-data",
        }
        rules = []
        for name, uid in expected.items():
            rules.append(
                {
                    "Labels": [
                        {
                            "key": "io.cilium.k8s.policy.derived-from",
                            "value": "NetworkPolicy",
                            "source": "k8s",
                        },
                        {
                            "key": "io.cilium.k8s.policy.name",
                            "value": name,
                            "source": "k8s",
                        },
                        {
                            "key": "io.cilium.k8s.policy.namespace",
                            "value": staging.APP_NAMESPACE,
                            "source": "k8s",
                        },
                        {
                            "key": "io.cilium.k8s.policy.uid",
                            "value": uid,
                            "source": "k8s",
                        },
                    ]
                }
            )
        raw_a = json.dumps(rules) + "\nRevision: 20"
        raw_b = json.dumps(rules) + "\nRevision: 19"
        with (
            mock.patch.object(
                staging,
                "ready_cilium_agents",
                return_value=[("node-a", "cilium-a"), ("node-b", "cilium-b")],
            ),
            mock.patch.object(staging, "output", side_effect=[raw_a, raw_b]) as output,
            mock.patch.object(
                staging.time, "monotonic", side_effect=[0.0, 0.0, 1.0, 1.0, 2.0, 2.0]
            ),
        ):
            result = staging.wait_migration_network_policy_enforcement(
                "kubectl", expected, timeout_seconds=45.0, poll_seconds=0.0
            )
        self.assertTrue(result["processed"])
        self.assertEqual(result["cilium_agent_count"], 2)
        self.assertEqual(result["minimum_policy_revision"], 19)
        self.assertEqual(output.call_count, 2)
        for call in output.call_args_list:
            argv = call.args[0]
            self.assertEqual(argv[1:3], ["-n", "kube-system"])
            self.assertIn("cilium-dbg", argv)
            self.assertEqual(argv[-2:], ["policy", "get"])
            self.assertEqual(call.kwargs["timeout"], 30.0)

    def test_cilium_inventory_reads_share_the_remaining_deadline(self) -> None:
        clock = {"now": 0.0}
        nodes = json.dumps({"items": [{"metadata": {"name": "node-a"}}]})
        pods = json.dumps(
            {
                "items": [
                    {
                        "metadata": {"name": "cilium-a"},
                        "spec": {"nodeName": "node-a"},
                        "status": {
                            "phase": "Running",
                            "conditions": [{"type": "Ready", "status": "True"}],
                        },
                    }
                ]
            }
        )

        def read_inventory(*args: object, **kwargs: object) -> str:
            clock["now"] += 20.0
            return nodes if clock["now"] == 20.0 else pods

        with (
            mock.patch.object(staging.time, "monotonic", side_effect=lambda: clock["now"]),
            mock.patch.object(staging, "output", side_effect=read_inventory) as output,
        ):
            agents = staging.ready_cilium_agents("kubectl", deadline=45.0)
        self.assertEqual(agents, [("node-a", "cilium-a")])
        self.assertEqual(
            [call.kwargs["timeout"] for call in output.call_args_list],
            [30.0, 25.0],
        )

    def test_cilium_policy_wait_rejects_success_observed_after_total_deadline(self) -> None:
        policy_uids = {"default-deny": "uid-default"}
        rules = [
            {
                "Labels": [
                    {
                        "key": "io.cilium.k8s.policy.derived-from",
                        "value": "NetworkPolicy",
                        "source": "k8s",
                    },
                    {
                        "key": "io.cilium.k8s.policy.name",
                        "value": "default-deny",
                        "source": "k8s",
                    },
                    {
                        "key": "io.cilium.k8s.policy.namespace",
                        "value": staging.APP_NAMESPACE,
                        "source": "k8s",
                    },
                    {
                        "key": "io.cilium.k8s.policy.uid",
                        "value": "uid-default",
                        "source": "k8s",
                    },
                ]
            }
        ]
        raw = json.dumps(rules) + "\nRevision: 12"
        clock = {"now": 0.0}

        def slow_policy_read(*args: object, **kwargs: object) -> str:
            clock["now"] += 20.0
            return raw

        with (
            mock.patch.object(staging.time, "monotonic", side_effect=lambda: clock["now"]),
            mock.patch.object(
                staging,
                "ready_cilium_agents",
                return_value=[
                    ("node-a", "cilium-a"),
                    ("node-b", "cilium-b"),
                    ("node-c", "cilium-c"),
                ],
            ),
            mock.patch.object(staging, "output", side_effect=slow_policy_read) as output,
        ):
            with self.assertRaisesRegex(staging.StagingCellError, "bounded activation deadline"):
                staging.wait_migration_network_policy_enforcement(
                    "kubectl", policy_uids, timeout_seconds=45.0, poll_seconds=0.0
                )
        self.assertEqual(clock["now"], 60.0)
        self.assertEqual(
            [call.kwargs["timeout"] for call in output.call_args_list],
            [30.0, 25.0, 5.0],
        )

    def test_cilium_policy_wait_fails_closed_when_exact_uid_is_not_processed(self) -> None:
        with (
            mock.patch.object(
                staging,
                "ready_cilium_agents",
                return_value=[("node-a", "cilium-a")],
            ),
            mock.patch.object(staging, "output", return_value="[]\nRevision: 3"),
            mock.patch.object(
                staging.time, "monotonic", side_effect=[10.0, 10.0, 10.0, 10.0, 11.0]
            ),
            mock.patch.object(staging.time, "sleep") as sleep,
        ):
            with self.assertRaisesRegex(staging.StagingCellError, "bounded activation deadline"):
                staging.wait_migration_network_policy_enforcement(
                    "kubectl",
                    {"default-deny": "uid-default-deny"},
                    timeout_seconds=1.0,
                    poll_seconds=0.0,
                )
        sleep.assert_not_called()

    def test_staging_migration_job_is_bound_to_promoted_api_digest(self) -> None:
        commit = "2" * 40
        promotion = {
            "source_commit": commit,
            "receipt_sha256": "c" * 64,
            "images": {
                "api": "ghcr.io/heimgewebe/commonthing-api@sha256:" + "a" * 64,
                "web": "ghcr.io/heimgewebe/commonthing-web@sha256:" + "b" * 64,
            },
        }
        plan = staging.migration_plan(commit, promotion)
        document = staging.migration_job_document(commit, promotion)
        self.assertEqual(document["kind"], "Job")
        self.assertEqual(document["metadata"]["name"], plan["job_name"])
        self.assertEqual(document["metadata"]["namespace"], staging.APP_NAMESPACE)
        self.assertEqual(
            document["metadata"]["annotations"]["commonthing.net/source-commit"],
            commit,
        )
        self.assertEqual(
            document["metadata"]["annotations"][staging.MIGRATION_SPEC_ANNOTATION],
            staging.migration_job_spec_sha256(document),
        )
        pod_spec = document["spec"]["template"]["spec"]
        self.assertEqual(
            document["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/name"],
            "weltgewebe-api",
        )
        self.assertEqual(
            pod_spec["readinessGates"],
            [{"conditionType": "commonthing.net/migration-not-service"}],
        )
        self.assertEqual(pod_spec["imagePullSecrets"], [{"name": staging.REGISTRY_SECRET}])
        container = pod_spec["containers"][0]
        self.assertEqual(container["image"], promotion["images"]["api"])
        environment = {item["name"]: item for item in container["env"]}
        self.assertEqual(environment["WELTGEWEBE_API_MIGRATION_ONLY"]["value"], "1")
        self.assertEqual(environment["WELTGEWEBE_API_STARTUP_MIGRATIONS"]["value"], "run")
        self.assertEqual(
            environment["DATABASE_URL"]["valueFrom"]["secretKeyRef"],
            {"name": staging.RUNTIME_SECRET, "key": "database-url"},
        )
        self.assertNotIn("password", json.dumps(document).lower())

    def test_staging_migration_waits_for_complete_exact_job_readback(self) -> None:
        commit = "2" * 40
        promotion = {
            "source_commit": commit,
            "receipt_sha256": "c" * 64,
            "images": {
                "api": "ghcr.io/heimgewebe/commonthing-api@sha256:" + "a" * 64,
                "web": "ghcr.io/heimgewebe/commonthing-web@sha256:" + "b" * 64,
            },
        }
        document = staging.migration_job_document(commit, promotion)
        observed = json.loads(json.dumps(document))
        observed["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
        with (
            mock.patch.object(staging, "apply_yaml_server_side") as apply_server_side,
            mock.patch.object(staging, "run") as run,
            mock.patch.object(
                staging, "output", side_effect=["", json.dumps(observed)]
            ),
        ):
            result = staging.run_staging_migration("kubectl", commit, promotion)
        apply_server_side.assert_called_once()
        self.assertEqual(
            apply_server_side.call_args.kwargs["field_manager"],
            "weltgewebe-staging-migration",
        )
        wait_argv = run.call_args.args[0]
        self.assertIn("--for=condition=Complete", wait_argv)
        self.assertIn(f"job/{result['job_name']}", wait_argv)
        self.assertTrue(result["complete"])
        self.assertEqual(result["api_image"], promotion["images"]["api"])

    def test_staging_migration_reuses_completed_exact_job_without_mutation(self) -> None:
        commit = "2" * 40
        promotion = {
            "source_commit": commit,
            "receipt_sha256": "c" * 64,
            "images": {
                "api": "ghcr.io/heimgewebe/commonthing-api@sha256:" + "a" * 64,
                "web": "ghcr.io/heimgewebe/commonthing-web@sha256:" + "b" * 64,
            },
        }
        observed = staging.migration_job_document(commit, promotion)
        observed["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
        with (
            mock.patch.object(staging, "output", return_value=json.dumps(observed)),
            mock.patch.object(staging, "apply_yaml_server_side") as apply_server_side,
            mock.patch.object(staging, "run") as run,
        ):
            result = staging.run_staging_migration("kubectl", commit, promotion)
        apply_server_side.assert_not_called()
        run.assert_not_called()
        self.assertTrue(result["complete"])

    def test_staging_migration_recreates_failed_exact_job(self) -> None:
        commit = "2" * 40
        promotion = {
            "source_commit": commit,
            "receipt_sha256": "c" * 64,
            "images": {
                "api": "ghcr.io/heimgewebe/commonthing-api@sha256:" + "a" * 64,
                "web": "ghcr.io/heimgewebe/commonthing-web@sha256:" + "b" * 64,
            },
        }
        failed = staging.migration_job_document(commit, promotion)
        failed["status"] = {"conditions": [{"type": "Failed", "status": "True"}]}
        complete = staging.migration_job_document(commit, promotion)
        complete["status"] = {
            "conditions": [{"type": "Complete", "status": "True"}]
        }
        with (
            mock.patch.object(
                staging,
                "output",
                side_effect=[json.dumps(failed), "", json.dumps(complete)],
            ),
            mock.patch.object(staging, "apply_yaml_server_side") as apply_server_side,
            mock.patch.object(staging, "run") as run,
        ):
            result = staging.run_staging_migration("kubectl", commit, promotion)
        self.assertEqual(run.call_count, 2)
        delete_argv = run.call_args_list[0].args[0]
        self.assertEqual(delete_argv[3:6], ["delete", "job", result["job_name"]])
        self.assertIn("--wait=true", delete_argv)
        wait_argv = run.call_args_list[1].args[0]
        self.assertIn("--for=condition=Complete", wait_argv)
        apply_server_side.assert_called_once()
        self.assertTrue(result["complete"])

    def test_staging_migration_never_deletes_mismatched_failed_job(self) -> None:
        commit = "2" * 40
        promotion = {
            "source_commit": commit,
            "receipt_sha256": "c" * 64,
            "images": {
                "api": "ghcr.io/heimgewebe/commonthing-api@sha256:" + "a" * 64,
                "web": "ghcr.io/heimgewebe/commonthing-web@sha256:" + "b" * 64,
            },
        }
        failed = staging.migration_job_document(commit, promotion)
        failed["metadata"]["annotations"][
            "commonthing.net/promotion-receipt-sha256"
        ] = "d" * 64
        failed["status"] = {"conditions": [{"type": "Failed", "status": "True"}]}
        with (
            mock.patch.object(staging, "output", return_value=json.dumps(failed)),
            mock.patch.object(staging, "apply_yaml_server_side") as apply_server_side,
            mock.patch.object(staging, "run") as run,
        ):
            with self.assertRaisesRegex(
                staging.StagingCellError, "refusing automatic replacement"
            ):
                staging.run_staging_migration("kubectl", commit, promotion)
        apply_server_side.assert_not_called()
        run.assert_not_called()

    def test_app_kustomization_uses_runtime_digest_patches(self) -> None:
        commit = "f" * 40
        promotion = {
            "images": {
                "api": "ghcr.io/heimgewebe/commonthing-api@sha256:" + "3" * 64,
                "web": "ghcr.io/heimgewebe/commonthing-web@sha256:" + "4" * 64,
            }
        }
        document = staging.app_kustomization_document(commit, promotion)
        spec = document["spec"]
        self.assertEqual(spec["path"], "./platform/apps/weltgewebe/overlays/staging")
        source = staging.app_source_document(commit)
        self.assertEqual(source["metadata"]["name"], staging.APP_SOURCE_NAME)
        self.assertEqual(source["spec"]["ref"]["commit"], commit)
        self.assertEqual(spec["sourceRef"]["name"], staging.APP_SOURCE_NAME)
        data_documents = staging.flux_documents("a" * 40)
        self.assertEqual(
            data_documents[1]["spec"]["sourceRef"]["name"], staging.SOURCE_NAME
        )
        self.assertNotEqual(staging.SOURCE_NAME, staging.APP_SOURCE_NAME)
        self.assertEqual(spec["dependsOn"], [{"name": staging.DATA_KUSTOMIZATION}])
        self.assertEqual(len(spec["patches"]), 2)
        rendered = "\n".join(item["patch"] for item in spec["patches"])
        self.assertIn(promotion["images"]["api"], rendered)
        self.assertIn(promotion["images"]["web"], rendered)
        self.assertIn("imagePullSecrets", rendered)
        self.assertIn(staging.REGISTRY_SECRET, rendered)
        static_overlay = (
            ROOT / "platform/apps/weltgewebe/overlays/staging/kustomization.yaml"
        ).read_text(encoding="utf-8")
        self.assertIn("promotion-required", static_overlay)

    def test_activate_uses_bootstrap_for_ownership_and_active_commit_for_flux(self) -> None:
        bootstrap = "1" * 40
        active = "2" * 40
        owner = "owner-a"
        api = "ghcr.io/heimgewebe/commonthing-api@sha256:" + "a" * 64
        web = "ghcr.io/heimgewebe/commonthing-web@sha256:" + "b" * 64
        args = argparse.Namespace(
            cluster=staging.DEFAULT_CLUSTER,
            owner_id=owner,
            source_commit=active,
        )
        promotion = {
            "source_commit": active,
            "receipt_sha256": "c" * 64,
            "images": {"api": api, "web": web},
        }
        registry_material = {
            "registry": staging.GHCR_REGISTRY,
            "username": "registry-user",
            "token": "registry-token",
        }
        registry_config_sha = staging.sha256_bytes(
            staging.registry_dockerconfig_json(registry_material).encode("utf-8")
        )
        activation_order: list[str] = []
        with tempfile.TemporaryDirectory(prefix="staging-activate-") as tmp_name:
            root = Path(tmp_name)
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(staging, "state_root", return_value=root))
                stack.enter_context(mock.patch.object(staging, "configure_reference_paths"))
                stack.enter_context(
                    mock.patch.object(
                        staging, "load_tool_receipt", return_value=self._tool_receipt()
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        staging,
                        "load_cell_receipt",
                        return_value={
                            "schema_version": 1,
                            "cluster": staging.DEFAULT_CLUSTER,
                            "owner_id": owner,
                            "bootstrap_commit": bootstrap,
                            "external_secret": {"source_sha256": "d" * 64},
                        },
                    )
                )
                stack.enter_context(
                    mock.patch.object(staging, "require_clean_commit", return_value=active)
                )
                stack.enter_context(
                    mock.patch.object(
                        staging, "load_promotion_receipt", return_value=promotion
                    )
                )
                load_registry = stack.enter_context(
                    mock.patch.object(
                        staging,
                        "load_registry_pull_material",
                        return_value=(registry_material, "e" * 64),
                    )
                )
                verify_pull = stack.enter_context(
                    mock.patch.object(
                        staging,
                        "verify_ghcr_pull_access",
                        return_value={"api": True, "web": True},
                    )
                )
                require_owned = stack.enter_context(
                    mock.patch.object(staging.reference, "require_owned_cluster")
                )
                normalize = stack.enter_context(
                    mock.patch.object(staging.reference, "normalize_owned_cluster_repository")
                )
                stack.enter_context(
                    mock.patch.object(
                        staging,
                        "inject_external_secrets",
                        return_value={"source_sha256": "d" * 64},
                    )
                )
                inject_registry = stack.enter_context(
                    mock.patch.object(
                        staging,
                        "inject_registry_pull_secret",
                        return_value={
                            "source_sha256": "e" * 64,
                            "config_sha256": registry_config_sha,
                            "secret_name": staging.REGISTRY_SECRET,
                            "registry": staging.GHCR_REGISTRY,
                        },
                    )
                )
                apply_yaml = stack.enter_context(mock.patch.object(staging, "apply_yaml"))
                require_data = stack.enter_context(
                    mock.patch.object(
                        staging, "require_bootstrap_data_current", return_value={}
                    )
                )
                apply_network = stack.enter_context(
                    mock.patch.object(
                        staging,
                        "apply_migration_network_isolation",
                        side_effect=lambda kubectl: (
                            activation_order.append("network"),
                            {
                                "policy_names": [
                                    "default-deny",
                                    "allow-dns",
                                    "allow-api-data-egress",
                                ],
                                "policy_uids": {
                                    "default-deny": "uid-default-deny",
                                    "allow-dns": "uid-allow-dns",
                                    "allow-api-data-egress": "uid-allow-api-data-egress",
                                },
                                "processed": True,
                                "cilium_agent_count": 3,
                                "minimum_policy_revision": 12,
                            },
                        )[-1],
                    )
                )
                run_migration = stack.enter_context(
                    mock.patch.object(
                        staging,
                        "run_staging_migration",
                        side_effect=lambda kubectl, commit, receipt: (
                            activation_order.append("migration"),
                            {
                                **staging.migration_plan(active, promotion),
                                "complete": True,
                            },
                        )[-1],
                    )
                )
                reconcile_app = stack.enter_context(
                    mock.patch.object(staging, "reconcile_app")
                )
                stack.enter_context(
                    mock.patch.object(
                        staging,
                        "app_live_health",
                        return_value={"api": "True", "web": "True"},
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        staging,
                        "app_image_references",
                        return_value={"api": api, "web": web},
                    )
                )
                verify_registry_binding = stack.enter_context(
                    mock.patch.object(
                        staging,
                        "verify_registry_pull_secret_binding",
                        return_value={
                            "ready": True,
                            "source_sha256": "e" * 64,
                            "config_sha256": registry_config_sha,
                        },
                    )
                )
                write_receipt = stack.enter_context(
                    mock.patch.object(
                        staging, "write_cell_receipt", return_value="/receipt.json"
                    )
                )
                result = staging.command_activate(args)
        load_registry.assert_called_once_with(root)
        verify_pull.assert_called_once()
        require_owned.assert_called_once_with(
            "kind",
            staging.DEFAULT_CLUSTER,
            expected_commit=bootstrap,
            expected_owner_id=owner,
        )
        normalize.assert_called_once_with(
            "kind",
            staging.DEFAULT_CLUSTER,
            expected_commit=bootstrap,
            expected_owner_id=owner,
        )
        inject_registry.assert_called_once()
        self.assertEqual(require_data.call_count, 2)
        for call in require_data.call_args_list:
            self.assertEqual(call.args, ("kubectl", bootstrap))
        apply_network.assert_called_once_with("kubectl")
        run_migration.assert_called_once_with("kubectl", active, promotion)
        self.assertEqual(activation_order, ["network", "migration"])
        reconcile_app.assert_called_once_with("kubectl", active)
        apply_yaml.assert_called_once()
        app_documents = apply_yaml.call_args.args[1]
        self.assertEqual(len(app_documents), 2)
        self.assertEqual(app_documents[0]["metadata"]["name"], staging.APP_SOURCE_NAME)
        self.assertEqual(app_documents[0]["spec"]["ref"]["commit"], active)
        self.assertEqual(app_documents[1]["metadata"]["name"], staging.APP_KUSTOMIZATION)
        verify_registry_binding.assert_called_once_with(
            "kubectl",
            expected_source_sha="e" * 64,
            expected_config_sha256=registry_config_sha,
        )
        self.assertTrue(result["app_activation"])
        self.assertFalse(result["production_changed"])
        self.assertEqual(result["active_commit"], active)
        self.assertEqual(write_receipt.call_count, 2)
        pending = write_receipt.call_args_list[0].args[1]
        self.assertEqual(pending["status"], "app-activation-in-progress")
        self.assertEqual(pending["pending_active_commit"], active)
        self.assertEqual(
            pending["pending_registry_pull_secret"]["config_sha256"],
            registry_config_sha,
        )
        self.assertEqual(
            pending["pending_image_promotion"]["receipt_sha256"], "c" * 64
        )
        self.assertEqual(
            pending["pending_migration"], staging.migration_plan(active, promotion)
        )
        stored = write_receipt.call_args_list[-1].args[1]
        self.assertEqual(stored["bootstrap_commit"], bootstrap)
        self.assertEqual(stored["active_commit"], active)
        self.assertEqual(stored["data_source_commit"], bootstrap)
        self.assertEqual(stored["app_source_commit"], active)
        self.assertNotIn("pending_active_commit", stored)
        self.assertNotIn("pending_image_promotion", stored)
        self.assertNotIn("pending_migration", stored)
        self.assertNotIn("pending_registry_pull_secret", stored)
        self.assertTrue(stored["migration"]["complete"])
        self.assertEqual(
            stored["migration"]["network_isolation"]["policy_names"],
            ["default-deny", "allow-dns", "allow-api-data-egress"],
        )
        self.assertTrue(stored["migration"]["network_isolation"]["processed"])
        self.assertEqual(
            stored["migration"]["network_isolation"]["cilium_agent_count"],
            3,
        )
        self.assertEqual(stored["image_promotion"]["images"], {"api": api, "web": web})
        self.assertEqual(stored["registry_pull_secret"]["secret_name"], staging.REGISTRY_SECRET)
        self.assertNotIn("token", json.dumps(stored["registry_pull_secret"]))

    def test_up_refuses_to_rewrite_activated_cell_before_release_mutation(self) -> None:
        owner = "owner-a"
        bootstrap = "7" * 40
        args = argparse.Namespace(
            cluster=staging.DEFAULT_CLUSTER,
            owner_id=owner,
            source_commit=None,
        )
        with tempfile.TemporaryDirectory(prefix="staging-up-activated-") as tmp_name:
            root = Path(tmp_name)
            (root / "receipts").mkdir(parents=True)
            (root / "receipts/cell-bootstrap.json").write_text("{}\n", encoding="utf-8")
            with (
                mock.patch.object(staging, "state_root", return_value=root),
                mock.patch.object(staging, "configure_reference_paths"),
                mock.patch.object(staging, "ensure_directory_durable"),
                mock.patch.object(staging.os, "chmod"),
                mock.patch.object(staging, "load_tool_receipt", return_value=self._tool_receipt()),
                mock.patch.object(
                    staging.reference,
                    "clusters",
                    return_value=[staging.DEFAULT_CLUSTER],
                ),
                mock.patch.object(
                    staging,
                    "load_cell_receipt",
                    return_value={
                        "schema_version": 1,
                        "cluster": staging.DEFAULT_CLUSTER,
                        "owner_id": owner,
                        "bootstrap_commit": bootstrap,
                        "active_commit": "8" * 40,
                        "app_activation": True,
                    },
                ),
                mock.patch.object(staging, "require_clean_commit") as clean_commit,
                mock.patch.object(staging, "apply_yaml") as apply_yaml,
                mock.patch.object(staging, "write_cell_receipt") as write_receipt,
            ):
                with self.assertRaisesRegex(staging.StagingCellError, "activated or activating"):
                    staging.command_up(args)
        clean_commit.assert_not_called()
        apply_yaml.assert_not_called()
        write_receipt.assert_not_called()

    def test_registry_secret_injection_uses_server_side_apply_and_persists_only_hashes(self) -> None:
        material = {
            "registry": staging.GHCR_REGISTRY,
            "username": "registry-user",
            "token": "registry-token",
        }
        source_sha = "9" * 64
        with mock.patch.object(staging, "apply_yaml_server_side") as server_apply:
            result = staging.inject_registry_pull_secret(
                "kubectl",
                Path("/unused"),
                material=material,
                source_sha=source_sha,
            )
        server_apply.assert_called_once()
        self.assertEqual(server_apply.call_args.kwargs["field_manager"], "weltgewebe-staging-registry")
        document = server_apply.call_args.args[1]
        self.assertIn(".dockerconfigjson", document["data"])
        decoded = base64.b64decode(document["data"][".dockerconfigjson"], validate=True)
        self.assertEqual(
            staging.sha256_bytes(decoded),
            result["config_sha256"],
        )
        self.assertEqual(result["source_sha256"], source_sha)
        self.assertEqual(len(result["config_sha256"]), 64)
        self.assertNotIn(material["token"], json.dumps(result))

    def test_server_side_secret_apply_never_requests_last_applied_annotation(self) -> None:
        with mock.patch.object(staging, "run") as run:
            staging.apply_yaml_server_side(
                "kubectl",
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {"name": "proof", "namespace": staging.APP_NAMESPACE},
                    "type": "Opaque",
                    "stringData": {"value": "sensitive"},
                },
                field_manager="weltgewebe-staging-registry",
            )
        argv = run.call_args.args[0]
        self.assertEqual(argv[:2], ["kubectl", "apply"])
        self.assertIn("--server-side", argv)
        self.assertIn("--field-manager=weltgewebe-staging-registry", argv)
        self.assertNotIn("--save-config", argv)

    def test_registry_redirect_handler_refuses_all_redirects(self) -> None:
        handler = staging._NoRegistryRedirectHandler()
        request = staging.urllib.request.Request("https://ghcr.io/token")
        self.assertIsNone(
            handler.redirect_request(
                request,
                None,
                302,
                "redirect",
                {},
                "https://example.invalid/other",
            )
        )

    def test_activated_status_does_not_require_registry_pat_file(self) -> None:
        owner = "owner-a"
        bootstrap = "5" * 40
        active = "6" * 40
        source_sha = "a" * 64
        config_sha = "b" * 64
        args = argparse.Namespace(cluster=staging.DEFAULT_CLUSTER)
        live = {name: "True" for name in staging.LIVE_DEPLOYMENTS}
        app_live = {name: "True" for name in staging.APP_DEPLOYMENTS}
        images = {
            "api": "ghcr.io/heimgewebe/commonthing-api@sha256:" + "c" * 64,
            "web": "ghcr.io/heimgewebe/commonthing-web@sha256:" + "d" * 64,
        }
        with tempfile.TemporaryDirectory(prefix="staging-activated-status-") as tmp_name:
            root = Path(tmp_name)
            self._write_bound_receipt(root, owner=owner, commit=bootstrap)
            receipt_path = root / "receipts/cell-bootstrap.json"
            payload = json.loads(receipt_path.read_text(encoding="utf-8"))
            payload.update(
                {
                    "active_commit": active,
                    "app_activation": True,
                    "image_promotion": {"images": images},
                    "registry_pull_secret": {
                        "source_sha256": source_sha,
                        "config_sha256": config_sha,
                        "secret_name": staging.REGISTRY_SECRET,
                        "registry": staging.GHCR_REGISTRY,
                    },
                }
            )
            receipt_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            with (
                mock.patch.object(staging, "state_root", return_value=root),
                mock.patch.object(staging, "configure_reference_paths"),
                mock.patch.object(staging, "load_tool_receipt", return_value=self._tool_receipt()),
                mock.patch.object(staging.reference, "clusters", return_value=[staging.DEFAULT_CLUSTER]),
                mock.patch.object(staging.reference, "require_owned_cluster"),
                mock.patch.object(
                    staging,
                    "output",
                    side_effect=[
                        f"main@sha1:{bootstrap}",
                        "1|1|True",
                        f"1|1|True|main@sha1:{bootstrap}",
                        "Bound",
                        "Bound",
                    ],
                ),
                mock.patch.object(
                    staging,
                    "verify_external_secret_binding",
                    return_value={"database": True, "runtime": True, "ready": True},
                ),
                mock.patch.object(staging, "staging_live_health", return_value=live),
                mock.patch.object(
                    staging,
                    "flux_resource_current_state",
                    side_effect=[
                        {"ready": "True", "revision": f"sha1:{active}", "matches_commit": True},
                        {"ready": "True", "revision": f"sha1:{active}", "matches_commit": True},
                    ],
                ),
                mock.patch.object(staging, "app_live_health", return_value=app_live),
                mock.patch.object(staging, "app_image_references", return_value=images),
                mock.patch.object(
                    staging,
                    "verify_registry_pull_secret_binding",
                    return_value={
                        "ready": True,
                        "source_sha256": source_sha,
                        "config_sha256": config_sha,
                    },
                ) as verify_registry,
                mock.patch.object(staging, "load_registry_pull_material") as load_registry,
            ):
                result = staging.command_status(args)
        load_registry.assert_not_called()
        verify_registry.assert_called_once_with(
            "kubectl",
            expected_source_sha=source_sha,
            expected_config_sha256=config_sha,
        )
        self.assertEqual(result["status"], "ready")
        self.assertTrue(result["app_source_matches_commit"])
        self.assertTrue(result["app_kustomization_matches_commit"])
        self.assertTrue(result["registry_pull_secret_ready"])

    def test_status_degrades_while_app_activation_is_in_progress(self) -> None:
        owner = "owner-a"
        bootstrap = "5" * 40
        pending = "6" * 40
        args = argparse.Namespace(cluster=staging.DEFAULT_CLUSTER)
        live = {name: "True" for name in staging.LIVE_DEPLOYMENTS}
        with tempfile.TemporaryDirectory(prefix="staging-pending-status-") as tmp_name:
            root = Path(tmp_name)
            self._write_bound_receipt(root, owner=owner, commit=bootstrap)
            receipt_path = root / "receipts/cell-bootstrap.json"
            payload = json.loads(receipt_path.read_text(encoding="utf-8"))
            payload.update(
                {
                    "status": "app-activation-in-progress",
                    "pending_active_commit": pending,
                    "app_activation": False,
                }
            )
            receipt_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            with (
                mock.patch.object(staging, "state_root", return_value=root),
                mock.patch.object(staging, "configure_reference_paths"),
                mock.patch.object(staging, "load_tool_receipt", return_value=self._tool_receipt()),
                mock.patch.object(staging.reference, "clusters", return_value=[staging.DEFAULT_CLUSTER]),
                mock.patch.object(staging.reference, "require_owned_cluster"),
                mock.patch.object(
                    staging,
                    "output",
                    side_effect=[
                        f"main@sha1:{bootstrap}",
                        "1|1|True",
                        f"1|1|True|main@sha1:{bootstrap}",
                        "Bound",
                        "Bound",
                    ],
                ),
                mock.patch.object(
                    staging,
                    "verify_external_secret_binding",
                    return_value={"database": True, "runtime": True, "ready": True},
                ),
                mock.patch.object(staging, "staging_live_health", return_value=live),
                mock.patch.object(
                    staging, "image_promotion_state", return_value={"status": "blocked"}
                ),
            ):
                result = staging.command_status(args)
        self.assertEqual(result["status"], "degraded")
        self.assertTrue(result["activation_in_progress"])
        self.assertEqual(result["pending_active_commit"], pending)
        stream = io.StringIO()
        with redirect_stdout(stream):
            staging.emit_public_success("status", result)
        public = json.loads(stream.getvalue())
        self.assertTrue(public["activation_in_progress"])
        self.assertEqual(public["pending_active_commit"], pending)

    def test_activation_recovery_rejects_a_different_pending_commit_before_mutation(self) -> None:
        bootstrap = "1" * 40
        pending = "2" * 40
        different = "3" * 40
        owner = "owner-a"
        args = argparse.Namespace(
            cluster=staging.DEFAULT_CLUSTER,
            owner_id=owner,
            source_commit=different,
        )
        with tempfile.TemporaryDirectory(prefix="staging-pending-resume-") as tmp_name:
            root = Path(tmp_name)
            with (
                mock.patch.object(staging, "state_root", return_value=root),
                mock.patch.object(staging, "configure_reference_paths"),
                mock.patch.object(staging, "load_tool_receipt", return_value=self._tool_receipt()),
                mock.patch.object(
                    staging,
                    "load_cell_receipt",
                    return_value={
                        "schema_version": 1,
                        "cluster": staging.DEFAULT_CLUSTER,
                        "owner_id": owner,
                        "bootstrap_commit": bootstrap,
                        "status": "app-activation-in-progress",
                        "pending_active_commit": pending,
                    },
                ),
                mock.patch.object(staging, "require_clean_commit") as clean_commit,
                mock.patch.object(staging, "load_promotion_receipt") as promotion,
                mock.patch.object(staging.reference, "normalize_owned_cluster_repository") as normalize,
            ):
                with self.assertRaisesRegex(staging.StagingCellError, "exact pending app commit"):
                    staging.command_activate(args)
        clean_commit.assert_not_called()
        promotion.assert_not_called()
        normalize.assert_not_called()

    def test_activation_recovery_same_pending_commit_does_not_recheck_moving_public_main(self) -> None:
        bootstrap = "1" * 40
        pending = "2" * 40
        owner = "owner-a"
        promotion = {
            "source_commit": pending,
            "receipt_sha256": "c" * 64,
            "images": {
                "api": "ghcr.io/heimgewebe/commonthing-api@sha256:" + "a" * 64,
                "web": "ghcr.io/heimgewebe/commonthing-web@sha256:" + "b" * 64,
            },
        }
        args = argparse.Namespace(
            cluster=staging.DEFAULT_CLUSTER,
            owner_id=owner,
            source_commit=pending,
        )
        cell = {
            "schema_version": 1,
            "cluster": staging.DEFAULT_CLUSTER,
            "owner_id": owner,
            "bootstrap_commit": bootstrap,
            "status": "app-activation-in-progress",
            "pending_active_commit": pending,
            "pending_image_promotion": {
                "source_commit": pending,
                "receipt_sha256": promotion["receipt_sha256"],
                "images": promotion["images"],
            },
            "pending_migration": staging.migration_plan(pending, promotion),
        }
        with tempfile.TemporaryDirectory(prefix="staging-pending-main-advance-") as tmp_name:
            root = Path(tmp_name)
            with (
                mock.patch.object(staging, "state_root", return_value=root),
                mock.patch.object(staging, "configure_reference_paths"),
                mock.patch.object(staging, "load_tool_receipt", return_value=self._tool_receipt()),
                mock.patch.object(staging, "load_cell_receipt", return_value=cell),
                mock.patch.object(staging, "load_promotion_receipt", return_value=promotion),
                mock.patch.object(staging, "require_clean_commit", return_value=pending) as clean_commit,
                mock.patch.object(
                    staging,
                    "load_registry_pull_material",
                    side_effect=staging.StagingCellError("stop-after-commit-check"),
                ),
            ):
                with self.assertRaisesRegex(staging.StagingCellError, "stop-after-commit-check"):
                    staging.command_activate(args)
        clean_commit.assert_called_once_with(
            pending,
            require_public_main=False,
        )

    def test_activation_recovery_rejects_changed_promotion_before_public_main_bypass(self) -> None:
        bootstrap = "1" * 40
        pending = "2" * 40
        owner = "owner-a"
        original = {
            "source_commit": pending,
            "receipt_sha256": "c" * 64,
            "images": {
                "api": "ghcr.io/heimgewebe/commonthing-api@sha256:" + "a" * 64,
                "web": "ghcr.io/heimgewebe/commonthing-web@sha256:" + "b" * 64,
            },
        }
        changed = {
            **original,
            "receipt_sha256": "d" * 64,
            "images": {
                **original["images"],
                "api": "ghcr.io/heimgewebe/commonthing-api@sha256:" + "e" * 64,
            },
        }
        cell = {
            "schema_version": 1,
            "cluster": staging.DEFAULT_CLUSTER,
            "owner_id": owner,
            "bootstrap_commit": bootstrap,
            "status": "app-activation-in-progress",
            "pending_active_commit": pending,
            "pending_image_promotion": {
                "source_commit": pending,
                "receipt_sha256": original["receipt_sha256"],
                "images": original["images"],
            },
            "pending_migration": staging.migration_plan(pending, original),
        }
        args = argparse.Namespace(
            cluster=staging.DEFAULT_CLUSTER,
            owner_id=owner,
            source_commit=pending,
        )
        with tempfile.TemporaryDirectory(prefix="staging-pending-promotion-drift-") as tmp_name:
            root = Path(tmp_name)
            with (
                mock.patch.object(staging, "state_root", return_value=root),
                mock.patch.object(staging, "configure_reference_paths"),
                mock.patch.object(staging, "load_tool_receipt", return_value=self._tool_receipt()),
                mock.patch.object(staging, "load_cell_receipt", return_value=cell),
                mock.patch.object(staging, "load_promotion_receipt", return_value=changed),
                mock.patch.object(staging, "require_clean_commit") as clean_commit,
                mock.patch.object(staging, "load_registry_pull_material") as registry_material,
                mock.patch.object(staging.reference, "require_owned_cluster") as require_owned,
            ):
                with self.assertRaisesRegex(staging.StagingCellError, "exact pending release"):
                    staging.command_activate(args)
        clean_commit.assert_not_called()
        registry_material.assert_not_called()
        require_owned.assert_not_called()

    def test_activation_recovery_rejects_changed_registry_before_cluster_mutation(self) -> None:
        bootstrap = "1" * 40
        pending = "2" * 40
        owner = "owner-a"
        promotion = {
            "source_commit": pending,
            "receipt_sha256": "c" * 64,
            "images": {
                "api": "ghcr.io/heimgewebe/commonthing-api@sha256:" + "a" * 64,
                "web": "ghcr.io/heimgewebe/commonthing-web@sha256:" + "b" * 64,
            },
        }
        original_registry = {
            "registry": staging.GHCR_REGISTRY,
            "username": "registry-user",
            "token": "original-token",
        }
        changed_registry = {**original_registry, "token": "rotated-token"}
        original_config_sha = staging.sha256_bytes(
            staging.registry_dockerconfig_json(original_registry).encode("utf-8")
        )
        cell = {
            "schema_version": 1,
            "cluster": staging.DEFAULT_CLUSTER,
            "owner_id": owner,
            "bootstrap_commit": bootstrap,
            "status": "app-activation-in-progress",
            "pending_active_commit": pending,
            "pending_image_promotion": {
                "source_commit": pending,
                "receipt_sha256": promotion["receipt_sha256"],
                "images": promotion["images"],
            },
            "pending_migration": staging.migration_plan(pending, promotion),
            "pending_registry_pull_secret": {
                "source_sha256": "e" * 64,
                "config_sha256": original_config_sha,
                "secret_name": staging.REGISTRY_SECRET,
                "registry": staging.GHCR_REGISTRY,
            },
        }
        args = argparse.Namespace(
            cluster=staging.DEFAULT_CLUSTER,
            owner_id=owner,
            source_commit=pending,
        )
        with tempfile.TemporaryDirectory(prefix="staging-pending-registry-drift-") as tmp_name:
            root = Path(tmp_name)
            with (
                mock.patch.object(staging, "state_root", return_value=root),
                mock.patch.object(staging, "configure_reference_paths"),
                mock.patch.object(
                    staging, "load_tool_receipt", return_value=self._tool_receipt()
                ),
                mock.patch.object(staging, "load_cell_receipt", return_value=cell),
                mock.patch.object(
                    staging, "load_promotion_receipt", return_value=promotion
                ),
                mock.patch.object(
                    staging, "require_clean_commit", return_value=pending
                ),
                mock.patch.object(
                    staging,
                    "load_registry_pull_material",
                    return_value=(changed_registry, "f" * 64),
                ),
                mock.patch.object(staging, "verify_ghcr_pull_access") as verify_pull,
                mock.patch.object(
                    staging.reference, "require_owned_cluster"
                ) as require_owned,
                mock.patch.object(
                    staging.reference, "normalize_owned_cluster_repository"
                ) as normalize,
                mock.patch.object(staging, "inject_external_secrets") as inject_external,
            ):
                with self.assertRaisesRegex(
                    staging.StagingCellError, "registry credential differs"
                ):
                    staging.command_activate(args)
        verify_pull.assert_not_called()
        require_owned.assert_not_called()
        normalize.assert_not_called()
        inject_external.assert_not_called()

    def test_activate_establishes_owned_kubeconfig_before_kubernetes_preflight(self) -> None:
        bootstrap = "1" * 40
        active = "2" * 40
        owner = "owner-a"
        promotion = {
            "source_commit": active,
            "receipt_sha256": "c" * 64,
            "images": {
                "api": "ghcr.io/heimgewebe/commonthing-api@sha256:" + "a" * 64,
                "web": "ghcr.io/heimgewebe/commonthing-web@sha256:" + "b" * 64,
            },
        }
        registry_material = {
            "registry": staging.GHCR_REGISTRY,
            "username": "registry-user",
            "token": "registry-token",
        }
        args = argparse.Namespace(
            cluster=staging.DEFAULT_CLUSTER,
            owner_id=owner,
            source_commit=active,
        )
        events: list[str] = []
        with tempfile.TemporaryDirectory(prefix="staging-kubeconfig-order-") as tmp_name:
            root = Path(tmp_name)
            with (
                mock.patch.object(staging, "state_root", return_value=root),
                mock.patch.object(staging, "configure_reference_paths"),
                mock.patch.object(staging, "load_tool_receipt", return_value=self._tool_receipt()),
                mock.patch.object(
                    staging,
                    "load_cell_receipt",
                    return_value={
                        "schema_version": 1,
                        "cluster": staging.DEFAULT_CLUSTER,
                        "owner_id": owner,
                        "bootstrap_commit": bootstrap,
                    },
                ),
                mock.patch.object(staging, "require_clean_commit", return_value=active),
                mock.patch.object(staging, "load_promotion_receipt", return_value=promotion),
                mock.patch.object(
                    staging,
                    "load_registry_pull_material",
                    return_value=(registry_material, "e" * 64),
                ),
                mock.patch.object(
                    staging,
                    "verify_ghcr_pull_access",
                    return_value={"api": True, "web": True},
                ),
                mock.patch.object(
                    staging.reference,
                    "require_owned_cluster",
                    side_effect=lambda *args, **kwargs: events.append("owned"),
                ),
                mock.patch.object(
                    staging,
                    "require_bootstrap_data_current",
                    side_effect=lambda *args, **kwargs: (
                        events.append("data"),
                        (_ for _ in ()).throw(staging.StagingCellError("stop-after-data-preflight")),
                    )[-1],
                ),
                mock.patch.object(staging.reference, "normalize_owned_cluster_repository") as normalize,
            ):
                with self.assertRaisesRegex(staging.StagingCellError, "stop-after-data-preflight"):
                    staging.command_activate(args)
        self.assertEqual(events, ["owned", "data"])
        normalize.assert_not_called()


    def test_external_database_and_runtime_secrets_use_server_side_apply(self) -> None:
        material = {
            "database_user": "weltgewebe",
            "database_name": "weltgewebe",
            "database_password": "password-value",
        }
        with (
            mock.patch.object(
                staging,
                "load_or_create_secret_material",
                return_value=(material, "a" * 64),
            ),
            mock.patch.object(staging, "apply_yaml") as namespace_apply,
            mock.patch.object(staging, "apply_yaml_server_side") as secret_apply,
        ):
            result = staging.inject_external_secrets("kubectl", Path("/unused"))
        namespace_apply.assert_called_once()
        secret_apply.assert_called_once()
        self.assertEqual(
            secret_apply.call_args.kwargs["field_manager"],
            "weltgewebe-staging-secrets",
        )
        documents = secret_apply.call_args.args[1]
        self.assertEqual({doc["kind"] for doc in documents}, {"Secret"})
        self.assertNotIn(material["database_password"], json.dumps(result))

    def test_activate_public_output_redacts_promotion_details(self) -> None:
        result = {
            "status": "app-ready-gateway-pending",
            "cluster": staging.DEFAULT_CLUSTER,
            "bootstrap_commit": "a" * 40,
            "active_commit": "b" * 40,
            "app_activation": True,
            "production_changed": False,
            "image_promotion": {"images": {"api": "secret-looking-image"}},
            "external_secret": {"source_sha256": "secret-looking-hash"},
        }
        stream = io.StringIO()
        with redirect_stdout(stream):
            staging.emit_public_success("activate", result)
        payload = json.loads(stream.getvalue())
        self.assertTrue(payload["app_activation"])
        self.assertFalse(payload["production_changed"])
        self.assertNotIn("image_promotion", payload)
        self.assertNotIn("external_secret", payload)


    def test_status_uses_active_commit_but_bootstrap_commit_for_cluster_ownership(self) -> None:
        owner = "owner-a"
        bootstrap = "5" * 40
        active = "6" * 40
        args = argparse.Namespace(cluster=staging.DEFAULT_CLUSTER)
        live = {name: "True" for name in staging.LIVE_DEPLOYMENTS}
        with tempfile.TemporaryDirectory(prefix="staging-active-status-") as tmp_name:
            root = Path(tmp_name)
            self._write_bound_receipt(root, owner=owner, commit=bootstrap)
            receipt_path = root / "receipts/cell-bootstrap.json"
            payload = json.loads(receipt_path.read_text(encoding="utf-8"))
            payload["active_commit"] = active
            receipt_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            with (
                mock.patch.object(staging, "state_root", return_value=root),
                mock.patch.object(staging, "configure_reference_paths"),
                mock.patch.object(
                    staging, "load_tool_receipt", return_value=self._tool_receipt()
                ),
                mock.patch.object(
                    staging.reference,
                    "clusters",
                    return_value=[staging.DEFAULT_CLUSTER],
                ),
                mock.patch.object(
                    staging.reference, "require_owned_cluster"
                ) as require_owned,
                mock.patch.object(
                    staging,
                    "output",
                    side_effect=[
                        f"main@sha1:{bootstrap}",
                        "1|1|True",
                        f"1|1|True|main@sha1:{bootstrap}",
                        "Bound",
                        "Bound",
                    ],
                ),
                mock.patch.object(
                    staging,
                    "verify_external_secret_binding",
                    return_value={"database": True, "runtime": True, "ready": True},
                ),
                mock.patch.object(staging, "staging_live_health", return_value=live),
                mock.patch.object(
                    staging, "image_promotion_state", return_value={"status": "blocked"}
                ),
            ):
                result = staging.command_status(args)
        require_owned.assert_called_once_with(
            "kind",
            staging.DEFAULT_CLUSTER,
            expected_commit=bootstrap,
            expected_owner_id=owner,
        )
        self.assertEqual(result["bootstrap_commit"], bootstrap)
        self.assertEqual(result["active_commit"], active)
        self.assertTrue(result["source_matches_commit"])
        self.assertTrue(result["data_matches_commit"])
        self.assertEqual(result["status"], "ready")


if __name__ == "__main__":
    unittest.main()
