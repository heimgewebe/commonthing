from __future__ import annotations

import hashlib
import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import experiment_b_runtime as runtime


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
        self.assertIn("/api/embed", source)
        self.assertIn('"database_generation_activation": False', source)
        self.assertNotIn("weltgewebe_search_generation_activation_ready", source)
        self.assertNotIn("weltgewebe_activate_search_generation", source)

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

    def test_portability_rejects_failed_or_cross_revision_receipts(self) -> None:
        commit = "a" * 40
        statuses = {
            "k3s.json": "ready",
            "platform.json": "ready",
            "secrets.json": "ready",
            "release.json": "applied",
            "t048-fixture.json": "loaded",
            "semantic-search.json": "pass",
            "functional-readback.json": "pass",
            "t048-load.json": "pass",
            "recovery.json": "pass",
            "status.json": "observed",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipts = root / "receipts"
            receipts.mkdir()
            for name, status in statuses.items():
                payload: dict[str, object] = {"schema_version": 1, "status": status}
                if name == "release.json":
                    payload["source_commit"] = commit
                elif name in {
                    "t048-fixture.json",
                    "semantic-search.json",
                    "functional-readback.json",
                    "recovery.json",
                    "status.json",
                }:
                    payload["source_commit"] = commit
                elif name == "t048-load.json":
                    payload["source_commit"] = commit
                (receipts / name).write_text(
                    json.dumps(payload) + "\n", encoding="utf-8"
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
        self.assertIn('recovery_receipt = root / "receipts/recovery.json"', source)
        self.assertIn("recovery_receipt.unlink(missing_ok=True)", source)
        self.assertIn("recovery_failed_receipt.unlink(missing_ok=True)", source)
        self.assertIn("atomic_json(", source)
        self.assertIn("recovery_failed_receipt,", source)
        self.assertIn("atomic_json(recovery_receipt, receipt)", source)
        portability = inspect.getsource(runtime.portability_report)
        self.assertIn("recovery_failed_receipt.is_file()", portability)

    def test_fixture_reuse_requires_live_content_binding(self) -> None:
        source = inspect.getsource(runtime.seed_t048_fixture)
        self.assertIn(
            "current_live_binding = _t048_live_fixture_binding(",
            source,
        )
        self.assertIn(
            'receipt.get("live_binding") != current_live_binding',
            source,
        )
        self.assertIn('"live_binding": live_binding', source)

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
        self.assertIn("streams/messages/bytes signature", source)

    def test_libvirt_absence_query_fails_closed(self) -> None:
        failed = runtime.subprocess.CompletedProcess(
            ["virsh"], 1, stdout="", stderr="permission denied"
        )
        with mock.patch.object(runtime, "run", return_value=failed):
            with self.assertRaises(runtime.RuntimeErrorEB):
                runtime._libvirt_resource_present("domain", runtime.VM_NAME)

    def test_inject_secrets_cli_does_not_forward_secret_tainted_return(self) -> None:
        source = inspect.getsource(runtime.main)
        branch = source.split('elif args.command == "inject-secrets":', 1)[1].split(
            'elif args.command == "apply-release":', 1
        )[0]
        self.assertIn("inject_secrets(", branch)
        self.assertNotIn("result = inject_secrets(", branch)
        self.assertIn('"receipt": "receipts/secrets.json"', branch)

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


if __name__ == "__main__":
    unittest.main()
