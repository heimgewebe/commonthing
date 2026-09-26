from __future__ import annotations

import hashlib
import inspect
import json
import sys
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
            {"vm-create.json", *runtime.K3S_ATTEMPT_INVALIDATES},
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
            with (
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

            for name in runtime.VM_ATTEMPT_INVALIDATES:
                self.assertFalse((receipts / name).exists(), name)

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
            functional.index("_gateway_base_url(root)"),
        )
        self.assertIn("_complete_live_check_attempt(", functional)

        status = inspect.getsource(runtime.status)
        self.assertLess(
            status.index("_begin_live_check_attempt("),
            status.index('run([kubectl, "get", "nodes", "-o", "json"]'),
        )
        self.assertIn("_complete_live_check_attempt(", status)

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
                "updatedReplicas": 1,
                "readyReplicas": 1,
                "availableReplicas": 1,
                "conditions": [{"type": "Available", "status": "True"}],
            },
        }
        snapshot = runtime._deployment_availability_snapshot(
            healthy, "weltgewebe-api"
        )
        self.assertTrue(snapshot["available"])

        failure_paths = (
            ("observedGeneration", 6),
            ("updatedReplicas", 0),
            ("readyReplicas", 0),
            ("availableReplicas", 0),
        )
        for field, value in failure_paths:
            with self.subTest(field=field):
                broken = json.loads(json.dumps(healthy))
                broken["status"][field] = value
                with self.assertRaises(runtime.RuntimeErrorEB):
                    runtime._deployment_availability_snapshot(
                        broken, "weltgewebe-api"
                    )

        no_available_condition = json.loads(json.dumps(healthy))
        no_available_condition["status"]["conditions"] = [
            {"type": "Available", "status": "False"}
        ]
        with self.assertRaises(runtime.RuntimeErrorEB):
            runtime._deployment_availability_snapshot(
                no_available_condition, "weltgewebe-api"
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
        statuses = {
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
        ):
            root = Path(tmp)
            receipts = root / "receipts"
            receipts.mkdir()
            for name, status in statuses.items():
                payload: dict[str, object] = {"schema_version": 1, "status": status}
                if name == "release.json":
                    payload["source_commit"] = commit
                elif name in {
                    "k3s.json",
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
                (receipts / name).write_text(
                    json.dumps(payload) + "\n", encoding="utf-8"
                )

            baseline = runtime.portability_report(root)
            self.assertEqual(baseline["status"], "pass")

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
