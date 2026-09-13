from __future__ import annotations

import argparse
import copy
import json
import stat
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import yaml

from scripts.platform import staging_cell as staging

REAL_OBSERVATION = staging.staging_gateway_observation
REAL_DOCUMENTS = staging.staging_gateway_documents
REAL_CLEAN_COMMIT = staging.require_clean_commit
ADDRESSES = ["172.20.0.3", "172.20.0.4"]


class StagingGatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cell = {
            "schema_version": 1,
            "cluster": staging.DEFAULT_CLUSTER,
            "owner_id": "test:t084",
            "bootstrap_commit": "a" * 40,
            "active_commit": "b" * 40,
            "app_activation": True,
            "status": "app-ready-gateway-pending",
            "image_promotion": {
                "source_commit": "b" * 40,
                "images": {"api": "api@sha256:abc", "web": "web@sha256:def"},
                "receipt_sha256": "c" * 64,
            },
        }
        staging.write_cell_receipt(self.root, self.cell)
        self.args = argparse.Namespace(
            cluster=staging.DEFAULT_CLUSTER,
            owner_id="test:t084",
            source_commit="b" * 40,
        )
        self.docs = [
            yaml.safe_load(
                (staging.ROOT / "platform/clusters/staging/gateway" / name).read_text()
            )
            for name in ("gateway.yaml", "httproute.yaml")
        ]
        self.binding = {
            "owner_id": self.cell["owner_id"],
            "active_commit": self.cell["active_commit"],
            "manifest_sha256": staging.sha256_bytes(
                json.dumps(self.docs, sort_keys=True).encode()
            ),
        }
        self.observed = {
            "resources": [
                {
                    "kind": document["kind"],
                    "namespace": document["metadata"]["namespace"],
                    "name": document["metadata"]["name"],
                    "uid": f"{document['kind']}-uid",
                    "gateway_binding": self.binding,
                    "routing_contract": staging.gateway_routing_contract(document),
                }
                for document in self.docs
            ],
            "service": {"uid": "service-uid"},
            "gateway_addresses": ADDRESSES,
            "service_addresses": ADDRESSES,
            "listener_port": 80,
        }
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.mocks = {}
        patches = {
            "state_root": self.root,
            "configure_reference_paths": None,
            "require_clean_commit": "b" * 40,
            "load_promotion_receipt": self.cell["image_promotion"],
            "load_tool_receipt": {
                "tools": {
                    "kind": "kind",
                    "kubectl": "kubectl",
                    "kustomize": "kustomize",
                }
            },
            "require_gateway_app_current": None,
            "staging_gateway_documents": self.docs,
            "staging_gateway_observation": self.observed,
            "gateway_list": [],
            "run": None,
        }
        for name, value in patches.items():
            self.mocks[name] = self.stack.enter_context(
                mock.patch.object(staging, name, return_value=value)
            )
        self.mocks["staging_gateway_documents"].side_effect = lambda _: copy.deepcopy(
            self.docs
        )
        self.owned = self.stack.enter_context(
            mock.patch.object(staging.reference, "require_owned_cluster")
        )
        self.probe = self.stack.enter_context(
            mock.patch.object(
                staging.reference,
                "probe_gateway_http",
                return_value=(
                    "kind-worker",
                    ADDRESSES[0],
                    b"ok",
                    b"<html>",
                    b"[]",
                ),
            )
        )
        self.get = self.stack.enter_context(
            mock.patch.object(staging, "gateway_get", side_effect=self.get_resource)
        )

    def get_resource(self, kubectl, kind, name, namespace=""):
        if kind == "GatewayClass":
            return {
                "metadata": {"generation": 1},
                "spec": {"controllerName": "io.cilium/gateway-controller"},
                "status": {
                    "conditions": [
                        {"type": "Accepted", "status": "True", "observedGeneration": 1}
                    ]
                },
            }
        return {}

    def test_manifests_only_http_canonical_staging_backends(self):
        gateway, route = self.docs
        self.assertEqual(
            gateway["spec"],
            {
                "gatewayClassName": "cilium",
                "listeners": [
                    {
                        "name": "http",
                        "protocol": "HTTP",
                        "port": 80,
                        "allowedRoutes": {"namespaces": {"from": "Same"}},
                    }
                ],
            },
        )
        self.assertEqual(
            route["spec"]["parentRefs"],
            [
                {
                    "name": staging.GATEWAY_NAME,
                    "namespace": staging.APP_NAMESPACE,
                    "sectionName": "http",
                }
            ],
        )
        self.assertEqual(
            [rule["backendRefs"] for rule in route["spec"]["rules"]],
            [
                [{"name": "commonthing-api", "port": 8080}],
                [{"name": "commonthing-web", "port": 8080}],
            ],
        )
        kustomization = yaml.safe_load(
            (
                staging.ROOT / "platform/clusters/staging/gateway/kustomization.yaml"
            ).read_text()
        )
        self.assertEqual(kustomization["resources"], ["gateway.yaml", "httproute.yaml"])
        self.assertFalse(
            (
                staging.ROOT / "platform/clusters/staging/gateway/address-pool.yaml"
            ).exists()
        )
        self.assertNotIn("hostnames", route["spec"])

    def test_success_receipt_private_bound_and_idempotent(self):
        for _ in range(2):
            result = staging.command_prove_gateway(self.args)
            self.assertEqual(result["status"], "gateway-ready")
            self.assertEqual(result["active_commit"], self.cell["active_commit"])
            self.assertEqual(result["owner_id"], self.args.owner_id)
            self.assertEqual(result["resources"], self.observed["resources"])
            self.assertEqual(result["implementation_commit"], self.args.source_commit)
            self.assertEqual(result["gateway_addresses"], ADDRESSES)
            self.assertEqual(result["service_addresses"], ADDRESSES)
            self.assertEqual(result["address"], ADDRESSES[0])
            self.assertEqual(result["api_nodes_sha256"], staging.sha256_bytes(b"[]"))
            self.assertEqual(result["does_not_establish"], staging.GATEWAY_LIMITS)
        path = self.root / "receipts/gateway-proof.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        cell = staging.load_cell_receipt(self.root)
        self.assertNotIn("pending_gateway", cell)
        self.assertEqual(
            cell["gateway_proof"]["receipt_sha256"], staging.sha256_file(path)
        )
        calls = self.mocks["run"].call_args_list
        self.assertEqual(len(calls), 4)
        self.assertIn("--dry-run=server", calls[0].args[0])
        self.assertNotIn("--dry-run=server", calls[1].args[0])
        for call in calls:
            self.assertTrue(call.kwargs["input_text"].startswith("---\n"))
            self.assertEqual(call.kwargs["timeout"], 120)
            applied = list(yaml.safe_load_all(call.kwargs["input_text"]))
            self.assertEqual(
                {d["kind"] for d in applied}, {d[0] for d in staging.GATEWAY_RESOURCES}
            )
            for document in applied:
                self.assertEqual(
                    document["metadata"]["annotations"],
                    {
                        staging.GATEWAY_OWNER_ANNOTATION: self.binding["owner_id"],
                        staging.GATEWAY_ACTIVE_COMMIT_ANNOTATION: self.binding[
                            "active_commit"
                        ],
                        staging.GATEWAY_MANIFEST_SHA256_ANNOTATION: self.binding[
                            "manifest_sha256"
                        ],
                    },
                )
        self.probe.assert_called_with("kind", staging.DEFAULT_CLUSTER, ADDRESSES, 80)

    def test_owner_commit_pending_activation_and_promotion_fail_before_apply(self):
        variants = [
            {"owner_id": "another:owner"},
            {"active_commit": "e" * 40},
            {"app_activation": False},
            {"pending_active_commit": "b" * 40},
            {"status": "app-activation-in-progress"},
            {"image_promotion": {}},
        ]
        for variant in variants:
            with self.subTest(variant=variant):
                staging.write_cell_receipt(self.root, {**self.cell, **variant})
                with self.assertRaises(staging.StagingCellError):
                    staging.command_prove_gateway(self.args)
                self.mocks["run"].assert_not_called()

    def test_failure_and_same_input_recovery(self):
        self.probe.side_effect = staging.reference.ProofError("unreachable")
        with self.assertRaises(staging.reference.ProofError):
            staging.command_prove_gateway(self.args)
        pending = staging.load_cell_receipt(self.root)
        self.assertEqual(pending["status"], "gateway-proof-in-progress")
        self.assertNotIn("gateway_proof", pending)
        self.assertFalse((self.root / "receipts/gateway-proof.json").exists())
        self.probe.side_effect = None
        staging.command_prove_gateway(self.args)
        self.assertEqual(
            staging.load_cell_receipt(self.root)["status"], "gateway-ready"
        )

    def test_changed_pending_manifest_refused(self):
        staging.write_cell_receipt(
            self.root,
            {**self.cell, "status": "gateway-proof-in-progress", "pending_gateway": {}},
        )
        with self.assertRaisesRegex(staging.StagingCellError, "recovery"):
            staging.command_prove_gateway(self.args)
        self.mocks["run"].assert_not_called()

    def test_fresh_proof_refuses_any_preexisting_resource(self):
        self.get.side_effect = lambda k, kind, n, ns="": (
            self.get_resource(k, kind, n, ns)
            if kind == "GatewayClass"
            else {
                "metadata": {
                    "uid": "leftover",
                    "annotations": staging.gateway_annotations(self.binding),
                }
            }
        )
        with self.assertRaisesRegex(staging.StagingCellError, "pre-existing"):
            staging.command_prove_gateway(self.args)
        self.mocks["run"].assert_not_called()

    def test_shadow_route_is_rejected_before_any_gateway_apply(self):
        shadow = copy.deepcopy(self.docs[1])
        shadow["metadata"]["name"] = "shadow"
        self.mocks["gateway_list"].return_value = [shadow]
        with self.assertRaisesRegex(staging.StagingCellError, "another HTTPRoute"):
            staging.command_prove_gateway(self.args)
        self.mocks["run"].assert_not_called()
        self.probe.assert_not_called()
        self.assertEqual(
            staging.load_cell_receipt(self.root)["status"],
            "app-ready-gateway-pending",
        )

    def test_recovery_requires_full_existing_resource_binding(self):
        staging.write_cell_receipt(
            self.root,
            {
                **self.cell,
                "status": "gateway-proof-in-progress",
                "pending_gateway": self.binding,
            },
        )
        existing = {
            "metadata": {
                "uid": "existing",
                "annotations": staging.gateway_annotations(self.binding),
            }
        }
        self.get.side_effect = lambda k, kind, n, ns="": (
            self.get_resource(k, kind, n, ns) if kind == "GatewayClass" else existing
        )
        staging.command_prove_gateway(self.args)
        for annotation, wrong_value in (
            (staging.GATEWAY_ACTIVE_COMMIT_ANNOTATION, "d" * 40),
            (staging.GATEWAY_MANIFEST_SHA256_ANNOTATION, "d" * 64),
        ):
            with self.subTest(annotation=annotation):
                changed = copy.deepcopy(existing)
                changed["metadata"]["annotations"][annotation] = wrong_value
                self.get.side_effect = lambda k, kind, n, ns="", changed=changed: (
                    self.get_resource(k, kind, n, ns)
                    if kind == "GatewayClass"
                    else changed
                )
                with self.assertRaisesRegex(
                    staging.StagingCellError, "exact owner/app/manifest"
                ):
                    staging.command_prove_gateway(self.args)

    def test_changed_resources_during_probe_fail(self):
        self.mocks["staging_gateway_observation"].side_effect = [
            self.observed,
            {**self.observed, "resources": []},
        ]
        with self.assertRaisesRegex(staging.StagingCellError, "changed"):
            staging.command_prove_gateway(self.args)
        self.assertEqual(
            staging.load_cell_receipt(self.root)["status"], "gateway-proof-in-progress"
        )

    def test_status_refuses_changed_receipt_resource_or_active_commit(self):
        staging.command_prove_gateway(self.args)
        cell = staging.load_cell_receipt(self.root)
        self.assertTrue(staging.gateway_receipt_current(self.root, cell, "kubectl"))
        self.assertFalse(
            staging.gateway_receipt_current(
                self.root, {**cell, "active_commit": "e" * 40}, "kubectl"
            )
        )
        self.mocks["staging_gateway_observation"].return_value = {
            **self.observed,
            "resources": [],
        }
        self.assertFalse(staging.gateway_receipt_current(self.root, cell, "kubectl"))
        (self.root / "receipts/gateway-proof.json").write_text("{}")
        self.assertFalse(staging.gateway_receipt_current(self.root, cell, "kubectl"))

    def test_conditions_require_current_generation(self):
        doc = self.get_resource("kubectl", "GatewayClass", "cilium")
        self.assertTrue(staging.current_condition(doc, "Accepted"))
        doc["metadata"]["generation"] = 2
        self.assertFalse(staging.current_condition(doc, "Accepted"))

    def test_lock_excludes_other_mutations(self):
        with staging.lifecycle_lock(self.root):
            with self.assertRaisesRegex(
                staging.StagingCellError, "already in progress"
            ):
                staging.command_prove_gateway(self.args)
        self.mocks["run"].assert_not_called()

    def test_observation_rejects_stale_wrong_parent_and_missing_address(self):
        # Exercise the actual readback validator, not the command's observation stub.
        documents = copy.deepcopy(self.docs)
        for document in documents:
            document["metadata"].update(
                uid=document["kind"],
                generation=2,
                annotations={
                    "commonthing.net/gateway-owner-id": self.binding["owner_id"],
                    "commonthing.net/gateway-active-commit": self.binding["active_commit"],
                    "commonthing.net/gateway-manifest-sha256": self.binding[
                        "manifest_sha256"
                    ],
                },
            )
        gateway, route = documents
        gateway["status"] = {
            "conditions": [
                {"type": "Programmed", "status": "True", "observedGeneration": 2}
            ],
            "addresses": [{"type": "IPAddress", "value": ip} for ip in ADDRESSES],
        }
        route["status"] = {
            "parents": [
                {
                    "controllerName": "io.cilium/gateway-controller",
                    "parentRef": {
                        "name": staging.GATEWAY_NAME,
                        "namespace": staging.APP_NAMESPACE,
                        "sectionName": "http",
                    },
                    "conditions": [
                        {"type": name, "status": "True", "observedGeneration": 2}
                        for name in ("Accepted", "ResolvedRefs")
                    ],
                }
            ]
        }
        service = {
            "metadata": {
                "name": "cilium-gateway-commonthing-staging",
                "uid": "svc",
                "namespace": staging.APP_NAMESPACE,
                "ownerReferences": [
                    {
                        "apiVersion": "gateway.networking.k8s.io/v1",
                        "kind": "Gateway",
                        "name": staging.GATEWAY_NAME,
                        "uid": "Gateway",
                    }
                ],
                "labels": {
                    "gateway.networking.k8s.io/gateway-name": staging.GATEWAY_NAME
                },
            },
            "spec": {"type": "LoadBalancer", "ports": [{"port": 80}]},
            "status": {
                "loadBalancer": {"ingress": [{"ip": ip} for ip in reversed(ADDRESSES)]}
            },
        }
        lookup = {document["kind"]: document for document in documents}
        self.get.side_effect = lambda k, kind, n, ns="": lookup[kind]
        self.mocks["gateway_list"].side_effect = lambda k, kind, ns="", **kwargs: (
            [route] if kind == "HTTPRoute" else [service]
        )
        observed = REAL_OBSERVATION("kubectl")
        self.assertEqual(len(observed["resources"]), 2)
        self.assertEqual(observed["resources"][0]["uid"], "Gateway")
        self.assertTrue(
            all(
                resource["gateway_binding"] == self.binding
                for resource in observed["resources"]
            )
        )
        self.assertEqual(observed["gateway_addresses"], ADDRESSES)
        self.assertEqual(observed["service_addresses"], ADDRESSES)
        self.assertEqual(observed["service"]["name"], "cilium-gateway-commonthing-staging")
        for mutate in (
            lambda: service["metadata"]["ownerReferences"][0].update(uid="old-gateway"),
            lambda: service["metadata"].update(ownerReferences=[]),
            lambda: service["status"]["loadBalancer"].update(
                ingress=[{"ip": ADDRESSES[0]}]
            ),
            lambda: service["status"]["loadBalancer"].update(ingress=[]),
            lambda: service["status"]["loadBalancer"].update(
                ingress=[{"hostname": "example.invalid"}]
            ),
            lambda: gateway["status"]["addresses"][0].update(value="invalid"),
            lambda: route["status"]["parents"][0]["conditions"][0].update(
                observedGeneration=1
            ),
            lambda: gateway["status"]["conditions"][0].update(observedGeneration=1),
            lambda: route["status"]["parents"][0]["parentRef"].update(
                sectionName="other"
            ),
            lambda: route["status"]["parents"][0]["conditions"][1].update(
                status="False"
            ),
            lambda: gateway["status"].update(addresses=[]),
        ):
            saved = copy.deepcopy(lookup)
            saved_service = copy.deepcopy(service)
            mutate()
            with self.assertRaises(staging.StagingCellError):
                REAL_OBSERVATION("kubectl")
            for kind in lookup:
                lookup[kind].clear()
                lookup[kind].update(saved[kind])
            service.clear()
            service.update(saved_service)

    def test_observation_rejects_extra_attached_route_and_duplicate_service(self):
        route = copy.deepcopy(self.docs[1])
        route["metadata"]["uid"] = "route-uid"
        extra = copy.deepcopy(route)
        extra["metadata"].update(name="shadow", uid="shadow-uid")
        with self.assertRaisesRegex(staging.StagingCellError, "exactly its one"):
            with mock.patch.object(
                staging, "gateway_list", return_value=[route, extra]
            ):
                staging.require_single_staging_gateway_route("kubectl", route)

        with self.assertRaisesRegex(staging.StagingCellError, "exactly one Cilium"):
            self._real_observation_with_inventory(service_count=2)

    def _real_observation_with_inventory(self, *, service_count: int = 1):
        documents = copy.deepcopy(self.docs)
        for document in documents:
            document["metadata"].update(
                uid=f"{document['kind']}-uid",
                generation=2,
                annotations=staging.gateway_annotations(self.binding),
            )
        gateway, route = documents
        gateway["status"] = {
            "conditions": [
                {"type": "Programmed", "status": "True", "observedGeneration": 2}
            ],
            "addresses": [{"type": "IPAddress", "value": ip} for ip in ADDRESSES],
        }
        route["status"] = {
            "parents": [
                {
                    "controllerName": "io.cilium/gateway-controller",
                    "parentRef": {
                        "name": staging.GATEWAY_NAME,
                        "namespace": staging.APP_NAMESPACE,
                        "sectionName": "http",
                    },
                    "conditions": [
                        {"type": name, "status": "True", "observedGeneration": 2}
                        for name in ("Accepted", "ResolvedRefs")
                    ],
                }
            ]
        }
        service = {
            "metadata": {
                "name": "controller-chosen-name",
                "uid": "svc",
                "namespace": staging.APP_NAMESPACE,
                "ownerReferences": [
                    {
                        "apiVersion": "gateway.networking.k8s.io/v1",
                        "kind": "Gateway",
                        "name": staging.GATEWAY_NAME,
                        "uid": "Gateway-uid",
                    }
                ],
                "labels": {staging.GATEWAY_SERVICE_LABEL: staging.GATEWAY_NAME},
            },
            "spec": {"type": "LoadBalancer", "ports": [{"port": 80}]},
            "status": {
                "loadBalancer": {"ingress": [{"ip": ip} for ip in ADDRESSES]}
            },
        }
        services = [copy.deepcopy(service) for _ in range(service_count)]
        for index, item in enumerate(services):
            item["metadata"]["uid"] = f"svc-{index}"
        lookup = {document["kind"]: document for document in documents}
        self.get.side_effect = lambda k, kind, n, ns="": lookup[kind]
        self.mocks["gateway_list"].side_effect = lambda k, kind, ns="", **kwargs: (
            [route] if kind == "HTTPRoute" else services
        )
        return REAL_OBSERVATION("kubectl")

    def test_observation_discovers_service_by_gateway_label_not_name(self):
        observed = self._real_observation_with_inventory()
        self.assertEqual(observed["service"]["name"], "controller-chosen-name")

    def test_observation_rejects_missing_gateway_service(self):
        with self.assertRaisesRegex(staging.StagingCellError, "exactly one Cilium"):
            self._real_observation_with_inventory(service_count=0)

    def test_desired_contract_normalizes_gateway_api_reference_defaults(self):
        desired = copy.deepcopy(self.docs[1])
        live = copy.deepcopy(desired)
        parent = live["spec"]["parentRefs"][0]
        parent.update(group="gateway.networking.k8s.io", kind="Gateway")
        for rule in live["spec"]["rules"]:
            for backend in rule["backendRefs"]:
                backend.update(
                    group="",
                    kind="Service",
                    namespace=staging.APP_NAMESPACE,
                    weight=1,
                )
        self.assertEqual(
            staging.gateway_routing_contract(desired),
            staging.gateway_routing_contract(live),
        )

    def test_desired_contract_normalizes_gateway_listener_defaults(self):
        desired = copy.deepcopy(self.docs[0])
        live = copy.deepcopy(desired)
        del desired["spec"]["listeners"][0]["allowedRoutes"]["namespaces"]["from"]
        self.assertEqual(
            staging.gateway_routing_contract(desired),
            staging.gateway_routing_contract(live),
        )
        live["spec"]["listeners"][0]["hostname"] = "staging.example.invalid"
        self.assertNotEqual(
            staging.gateway_routing_contract(desired),
            staging.gateway_routing_contract(live),
        )

    def test_render_rejects_extra_resources(self):
        with mock.patch.object(
            staging,
            "output",
            return_value=yaml.safe_dump_all(
                self.docs + [{"kind": "Secret", "metadata": {"name": "forbidden"}}]
            ),
        ):
            with self.assertRaisesRegex(staging.StagingCellError, "allowlist"):
                REAL_DOCUMENTS("kustomize")

    def test_implementation_requires_clean_exact_public_main_before_runtime(self):
        for dirty, head, public in (
            (" M file", "b" * 40, "b" * 40),
            ("", "d" * 40, "b" * 40),
            ("", "b" * 40, "d" * 40),
        ):
            with self.subTest(dirty=dirty, head=head, public=public):
                self.mocks["require_clean_commit"].side_effect = REAL_CLEAN_COMMIT

                def git_output(argv, **kwargs):
                    return {
                        "status": dirty,
                        "rev-parse": head,
                        "ls-remote": public + "\trefs/heads/main",
                    }[argv[1]]

                with mock.patch.object(staging, "output", side_effect=git_output):
                    with self.assertRaises(staging.StagingCellError):
                        staging.command_prove_gateway(self.args)
                self.owned.assert_not_called()
                self.mocks["run"].assert_not_called()
        self.mocks["require_clean_commit"].assert_called_with(self.args.source_commit)

    def test_second_candidate_success_is_bound_and_rechecked(self):
        self.probe.return_value = ("worker", ADDRESSES[1], b"ok", b"html", b"[]")
        result = staging.command_prove_gateway(self.args)
        self.assertEqual(result["address"], ADDRESSES[1])
        self.assertEqual(result["gateway_addresses"], ADDRESSES)
        self.assertEqual(result["service_addresses"], ADDRESSES)
        cell = staging.load_cell_receipt(self.root)
        self.assertTrue(staging.gateway_receipt_current(self.root, cell, "kubectl"))
        for changes in (
            {"address": "172.20.0.99"},
            {"implementation_commit": "d" * 40},
        ):
            path = self.root / "receipts/gateway-proof.json"
            staging.atomic_json(path, {**result, **changes})
            cell["gateway_proof"]["receipt_sha256"] = staging.sha256_file(path)
            self.assertFalse(
                staging.gateway_receipt_current(self.root, cell, "kubectl")
            )

    def test_status_rejects_gateway_binding_annotation_drift(self):
        staging.command_prove_gateway(self.args)
        cell = staging.load_cell_receipt(self.root)
        changed = copy.deepcopy(self.observed)
        changed["resources"][0]["gateway_binding"]["owner_id"] = "foreign-owner"
        self.mocks["staging_gateway_observation"].return_value = changed
        self.assertFalse(staging.gateway_receipt_current(self.root, cell, "kubectl"))

    def test_proof_rejects_live_gateway_binding_drift(self):
        changed = copy.deepcopy(self.observed)
        changed["resources"][0]["gateway_binding"]["active_commit"] = "d" * 40
        self.mocks["staging_gateway_observation"].return_value = changed
        with self.assertRaisesRegex(staging.StagingCellError, "exact owner/app/manifest"):
            staging.command_prove_gateway(self.args)
        self.probe.assert_not_called()

    def test_proof_rejects_live_routing_contract_drift(self):
        changed = copy.deepcopy(self.observed)
        changed["resources"][1]["routing_contract"]["rules"][0]["backendRefs"][0][
            "port"
        ] = 9090
        self.mocks["staging_gateway_observation"].return_value = changed
        with self.assertRaisesRegex(staging.StagingCellError, "routing contract"):
            staging.command_prove_gateway(self.args)
        self.probe.assert_not_called()

    def test_selected_address_must_be_observed(self):
        self.probe.return_value = ("worker", "172.20.0.99", b"ok", b"html", b"[]")
        with self.assertRaisesRegex(staging.StagingCellError, "unobserved"):
            staging.command_prove_gateway(self.args)
        self.assertFalse((self.root / "receipts/gateway-proof.json").exists())

    def test_address_drift_during_probe_and_after_receipt(self):
        changed = {
            **self.observed,
            "gateway_addresses": [ADDRESSES[1]],
            "service_addresses": [ADDRESSES[1]],
        }
        self.mocks["staging_gateway_observation"].side_effect = [self.observed, changed]
        with self.assertRaisesRegex(staging.StagingCellError, "changed"):
            staging.command_prove_gateway(self.args)
        self.mocks["staging_gateway_observation"].side_effect = None
        staging.command_prove_gateway(self.args)
        cell = staging.load_cell_receipt(self.root)
        self.mocks["staging_gateway_observation"].return_value = changed
        self.assertFalse(staging.gateway_receipt_current(self.root, cell, "kubectl"))

    def test_retire_gateway_before_activation_removes_bound_route_gateway_and_receipt(self):
        annotations = {
            "commonthing.net/gateway-owner-id": self.cell["owner_id"],
            "commonthing.net/gateway-active-commit": self.cell["active_commit"],
        }
        state = {
            ("Gateway", staging.APP_NAMESPACE, staging.GATEWAY_NAME): {
                "metadata": {"annotations": annotations, "uid": "gateway-uid"}
            },
            ("HTTPRoute", staging.APP_NAMESPACE, staging.GATEWAY_NAME): {
                "metadata": {"annotations": annotations, "uid": "route-uid"}
            },
            (
                "Service",
                staging.APP_NAMESPACE,
                f"cilium-gateway-{staging.GATEWAY_NAME}",
            ): {
                "metadata": {
                    "name": f"cilium-gateway-{staging.GATEWAY_NAME}",
                    "uid": "service-uid",
                    "labels": {staging.GATEWAY_SERVICE_LABEL: staging.GATEWAY_NAME},
                    "ownerReferences": [
                        {
                            "apiVersion": "gateway.networking.k8s.io/v1",
                            "kind": "Gateway",
                            "name": staging.GATEWAY_NAME,
                            "uid": "gateway-uid",
                        }
                    ],
                }
            },
        }

        def get_resource(kubectl, kind, name, namespace=""):
            return copy.deepcopy(state.get((kind, namespace, name), {}))

        def delete_resource(argv, **kwargs):
            self.assertEqual(argv[3], "delete")
            identity = (argv[4], argv[2], argv[5])
            state.pop(identity, None)
            if argv[4] == "Gateway":
                state.pop(
                    (
                        "Service",
                        staging.APP_NAMESPACE,
                        f"cilium-gateway-{staging.GATEWAY_NAME}",
                    ),
                    None,
                )

        self.get.side_effect = get_resource
        self.mocks["gateway_list"].side_effect = lambda k, kind, ns="", **kwargs: [
            copy.deepcopy(document)
            for (resource_kind, resource_ns, _), document in state.items()
            if resource_kind == kind and resource_ns == ns
        ]
        self.mocks["run"].side_effect = delete_resource
        receipt = self.root / "receipts/gateway-proof.json"
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text("{}\n", encoding="utf-8")

        staging.retire_gateway_before_activation(
            "kubectl", self.root, self.cell, self.cell["owner_id"]
        )

        self.assertFalse(receipt.exists())
        deleted_kinds = [call.args[0][4] for call in self.mocks["run"].call_args_list]
        self.assertEqual(deleted_kinds, ["HTTPRoute", "Gateway"])
        self.assertEqual(state, {})

    def test_retire_gateway_tracks_service_by_receipt_when_label_disappears(self):
        annotations = {
            staging.GATEWAY_OWNER_ANNOTATION: self.cell["owner_id"],
            staging.GATEWAY_ACTIVE_COMMIT_ANNOTATION: self.cell["active_commit"],
        }
        service_name = f"cilium-gateway-{staging.GATEWAY_NAME}"
        state = {
            ("Gateway", staging.APP_NAMESPACE, staging.GATEWAY_NAME): {
                "metadata": {"annotations": annotations, "uid": "gateway-uid"}
            },
            ("HTTPRoute", staging.APP_NAMESPACE, staging.GATEWAY_NAME): {
                "metadata": {"annotations": annotations, "uid": "route-uid"}
            },
            ("Service", staging.APP_NAMESPACE, service_name): {
                "metadata": {
                    "name": service_name,
                    "uid": "service-uid",
                    "labels": {},
                    "ownerReferences": [
                        {
                            "apiVersion": "gateway.networking.k8s.io/v1",
                            "kind": "Gateway",
                            "name": staging.GATEWAY_NAME,
                            "uid": "gateway-uid",
                        }
                    ],
                }
            },
        }
        receipt = {
            "owner_id": self.cell["owner_id"],
            "active_commit": self.cell["active_commit"],
            "service": {"name": service_name, "uid": "service-uid"},
        }
        receipt_path = self.root / "receipts/gateway-proof.json"
        staging.atomic_json(receipt_path, receipt)
        self.cell.update(
            status="gateway-ready",
            gateway_proof={
                "active_commit": self.cell["active_commit"],
                "receipt_sha256": staging.sha256_file(receipt_path),
            },
        )
        direct_service_reads = 0

        def get_resource(kubectl, kind, name, namespace=""):
            nonlocal direct_service_reads
            identity = (kind, namespace, name)
            if kind == "Service" and name == service_name:
                direct_service_reads += 1
                if direct_service_reads > 1:
                    state.pop(identity, None)
            return copy.deepcopy(state.get(identity, {}))

        def list_resources(kubectl, kind, namespace="", *, label_selector=None):
            services = [
                copy.deepcopy(document)
                for (resource_kind, resource_ns, _), document in state.items()
                if resource_kind == kind and resource_ns == namespace
            ]
            if label_selector:
                return [
                    service
                    for service in services
                    if service.get("metadata", {}).get("labels", {}).get(
                        staging.GATEWAY_SERVICE_LABEL
                    )
                    == staging.GATEWAY_NAME
                ]
            return services

        def delete_resource(argv, **kwargs):
            state.pop((argv[4], argv[2], argv[5]), None)

        self.get.side_effect = get_resource
        self.mocks["gateway_list"].side_effect = list_resources
        self.mocks["run"].side_effect = delete_resource
        with mock.patch.object(staging.time, "sleep"):
            staging.retire_gateway_before_activation(
                "kubectl", self.root, self.cell, self.cell["owner_id"]
            )

        self.assertGreaterEqual(direct_service_reads, 2)
        self.assertFalse(receipt_path.exists())
        self.assertEqual(state, {})

    def test_retire_gateway_before_activation_refuses_orphan_service(self):
        self.get.side_effect = lambda kubectl, kind, name, namespace="": {}
        self.mocks["gateway_list"].return_value = [
            {"metadata": {"uid": "orphan-service"}}
        ]
        with self.assertRaisesRegex(staging.StagingCellError, "orphan"):
            staging.retire_gateway_before_activation(
                "kubectl", self.root, self.cell, self.cell["owner_id"]
            )
        self.mocks["run"].assert_not_called()

    def test_retire_gateway_before_activation_refuses_foreign_binding(self):
        self.get.side_effect = lambda kubectl, kind, name, namespace="": (
            {
                "metadata": {
                    "annotations": {
                        "commonthing.net/gateway-owner-id": "foreign-owner",
                        "commonthing.net/gateway-active-commit": self.cell["active_commit"],
                    }
                }
            }
            if kind == "Gateway"
            else {}
        )
        with self.assertRaisesRegex(staging.StagingCellError, "refusing to retire"):
            staging.retire_gateway_before_activation(
                "kubectl", self.root, self.cell, self.cell["owner_id"]
            )
        self.mocks["run"].assert_not_called()

    def test_cli_requires_owner_and_exact_source(self):
        parsed = staging.parser().parse_args(
            [
                "prove-gateway",
                "--owner-id",
                self.args.owner_id,
                "--source-commit",
                self.args.source_commit,
            ]
        )
        self.assertEqual(parsed.cluster, staging.DEFAULT_CLUSTER)


if __name__ == "__main__":
    unittest.main()
