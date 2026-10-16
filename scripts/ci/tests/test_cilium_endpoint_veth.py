"""Fail-closed tests for the nonproductive Cilium host-veth identity proof."""

from __future__ import annotations

import copy
import unittest

from scripts.platform import cilium_endpoint_veth as target


NODE = "commonthing-staging-worker"
NS = "commonthing-staging"
CID = "a" * 64
MAC = "ae:12:34:56:78:90"
IFACE = "lxc2aa99b88542d"
POD_NAME = "commonthing-api-abcdefghi-abcde"


def fixture():
    pods = [
        {
            "metadata": {
                "uid": "99999999-aaaa-4aaa-9aaa-111111111111",
                "namespace": NS,
                "name": POD_NAME,
                "labels": {"app.kubernetes.io/name": "commonthing-api"},
            },
            "spec": {"nodeName": NODE},
            "status": {
                "phase": "Running",
                "podIP": "10.0.1.101",
                "conditions": [{"type": "Ready", "status": "True"}],
                "containerStatuses": [
                    {
                        "name": "api",
                        "ready": True,
                        "containerID": "containerd://" + ("b" * 64),
                    }
                ],
            },
        }
    ]
    endpoints = [
        {
            "id": 39,
            "status": {
                "state": "ready",
                "external-identifiers": {
                    "k8s-namespace": NS,
                    "k8s-pod-name": POD_NAME,
                    "pod-name": NS + "/" + POD_NAME,
                    "container-id": CID,
                    "cni-attachment-id": CID + ":eth0",
                },
                "networking": {
                    "interface-name": IFACE,
                    "interface-index": 21,
                    "host-mac": MAC,
                    "addressing": [{"ipv4": "10.0.1.101"}],
                },
            },
        }
    ]
    links = [
        {
            "ifname": IFACE,
            "ifindex": 21,
            "address": MAC,
            "linkinfo": {"info_kind": "veth"},
        },
        {
            "ifname": "lxc-something-other",
            "ifindex": 23,
            "address": "aa:bb:cc:dd:ee:ff",
            "linkinfo": {"info_kind": "veth"},
        },
    ]
    sandboxes = [
        {
            "id": CID,
            "state": "SANDBOX_READY",
            "metadata": {
                "name": POD_NAME,
                "namespace": NS,
                "uid": pods[0]["metadata"]["uid"],
                "attempt": 1,
            },
            "labels": {
                "io.kubernetes.pod.uid": pods[0]["metadata"]["uid"],
                "io.kubernetes.pod.name": POD_NAME,
                "io.kubernetes.pod.namespace": NS,
                "app.kubernetes.io/name": "commonthing-api",
            },
        }
    ]
    return pods, endpoints, links, sandboxes


class CiliumEndpointVethTest(unittest.TestCase):
    def plan(self, pods=None, endpoints=None, links=None, sandboxes=None):
        original = fixture()
        return target.bind_cilium_endpoint_veths(
            node=NODE,
            namespace=NS,
            pods=original[0] if pods is None else pods,
            endpoints=original[1] if endpoints is None else endpoints,
            links=original[2] if links is None else links,
            sandboxes=original[3] if sandboxes is None else sandboxes,
        )

    def test_valid_only_registered_api_endpoint_no_prefix_guess(self):
        items = self.plan()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["ifname"], IFACE)
        self.assertEqual(items[0]["ifindex"], 21)
        self.assertEqual(items[0]["endpoint_id"], 39)
        self.assertEqual(items[0]["pod_ip"], "10.0.1.101")
        self.assertNotIn("lxc-something-other", str(items))

    def test_live_snapshot_drift_before_effect_is_blocking(self):
        old = self.plan()
        pods, endpoints, links, sandboxes = fixture()
        links[0]["ifindex"] = 33
        with self.assertRaises(target.EndpointIdentityError):
            self.plan(pods, endpoints, links, sandboxes)
        with self.assertRaises(target.EndpointIdentityError):
            target.require_unchanged_endpoint_bindings(old, ())

    def test_ifindex_reused_with_changed_mac_is_rejected(self):
        pods, endpoints, links, sandboxes = fixture()
        links[0]["address"] = "b0:11:22:33:44:55"
        with self.assertRaises(target.EndpointIdentityError):
            self.plan(pods, endpoints, links, sandboxes)

    def test_name_looks_like_cilium_but_no_registry_does_not_authorize(self):
        pods, _, links, sandboxes = fixture()
        with self.assertRaises(target.EndpointIdentityError):
            self.plan(pods, [], links, sandboxes)

    def test_wrong_host_and_namespace_are_rejected(self):
        pods, endpoints, links, sandboxes = fixture()
        pods[0]["spec"]["nodeName"] = "different-worker"
        with self.assertRaises(target.EndpointIdentityError):
            self.plan(pods, endpoints, links, sandboxes)
        pods, endpoints, links, sandboxes = fixture()
        endpoints[0]["status"]["external-identifiers"]["k8s-namespace"] = "other-ns"
        with self.assertRaises(target.EndpointIdentityError):
            self.plan(pods, endpoints, links, sandboxes)

    def test_not_ready_and_non_veth_are_rejected(self):
        pods, endpoints, links, sandboxes = fixture()
        endpoints[0]["status"]["state"] = "regenerating"
        with self.assertRaises(target.EndpointIdentityError):
            self.plan(pods, endpoints, links, sandboxes)
        pods, endpoints, links, sandboxes = fixture()
        links[0]["linkinfo"]["info_kind"] = "dummy"
        with self.assertRaises(target.EndpointIdentityError):
            self.plan(pods, endpoints, links, sandboxes)

    def test_stale_container_and_wrong_endpoint_ip_are_rejected(self):
        pods, endpoints, links, sandboxes = fixture()
        endpoints[0]["status"]["external-identifiers"]["container-id"] = "b" * 64
        with self.assertRaises(target.EndpointIdentityError):
            self.plan(pods, endpoints, links, sandboxes)
        pods, endpoints, links, sandboxes = fixture()
        endpoints[0]["status"]["networking"]["addressing"][0]["ipv4"] = "10.0.1.200"
        with self.assertRaises(target.EndpointIdentityError):
            self.plan(pods, endpoints, links, sandboxes)

    def test_duplicate_or_incomplete_endpoint_registry_is_blocking(self):
        pods, endpoints, links, sandboxes = fixture()
        endpoints.append(copy.deepcopy(endpoints[0]))
        with self.assertRaises(target.EndpointIdentityError):
            self.plan(pods, endpoints, links, sandboxes)
        pods, endpoints, links, sandboxes = fixture()
        endpoints[0]["status"]["networking"].pop("interface-index")
        with self.assertRaises(target.EndpointIdentityError):
            self.plan(pods, endpoints, links, sandboxes)

    def test_only_allowlisted_staging_api_web_pods_are_targets(self):
        pods, endpoints, links, sandboxes = fixture()
        pods[0]["metadata"]["labels"]["app.kubernetes.io/name"] = "unknown"
        self.assertEqual(self.plan(pods, endpoints, links, sandboxes), ())
        pods, endpoints, links, sandboxes = fixture()
        pods[0]["metadata"]["namespace"] = "kube-system"
        with self.assertRaises(target.EndpointIdentityError):
            self.plan(pods, endpoints, links, sandboxes)

    def test_report_is_read_only_and_detects_pending_interfaces(self):
        items = self.plan()
        queried = []
        report = target.observe_accept_local(
            items,
            read_value=lambda name: queried.append(name) or "0",
        )
        self.assertEqual(queried, [IFACE])
        self.assertEqual(report[0]["accept_local"], 0)
        self.assertEqual(report[0]["status"], "requires-authorized-lifecycle-fix")
        self.assertEqual(
            target.observe_accept_local(items, read_value=lambda _: "1")[0]["status"],
            "configured",
        )
        with self.assertRaises(target.EndpointIdentityError):
            target.observe_accept_local(items, read_value=lambda _: "2")

    def test_requires_ready_cri_pod_sandbox_matching_uid(self):
        pods, endpoints, links, sandboxes = fixture()
        self.assertEqual(len(self.plan(pods, endpoints, links, sandboxes)), 1)
        with self.assertRaises(target.EndpointIdentityError):
            self.plan(pods, endpoints, links, [])
        tampered = copy.deepcopy(sandboxes)
        tampered[0]["state"] = "SANDBOX_NOTREADY"
        with self.assertRaises(target.EndpointIdentityError):
            self.plan(pods, endpoints, links, tampered)
        tampered = copy.deepcopy(sandboxes)
        tampered[0]["metadata"]["uid"] = "other-pod-uid"
        with self.assertRaises(target.EndpointIdentityError):
            self.plan(pods, endpoints, links, tampered)
        tampered = copy.deepcopy(sandboxes)
        tampered[0]["labels"]["io.kubernetes.pod.uid"] = "other-pod-uid"
        with self.assertRaises(target.EndpointIdentityError):
            self.plan(pods, endpoints, links, tampered)
        tampered = copy.deepcopy(sandboxes)
        tampered.append(copy.deepcopy(sandboxes[0]))
        with self.assertRaises(target.EndpointIdentityError):
            self.plan(pods, endpoints, links, tampered)

    def test_rejects_cilium_sandbox_or_cni_attachment_spoofing(self):
        pods, endpoints, links, sandboxes = fixture()
        altered = copy.deepcopy(endpoints)
        altered[0]["status"]["external-identifiers"]["cni-attachment-id"] = (
            "b" * 64 + ":eth0"
        )
        with self.assertRaises(target.EndpointIdentityError):
            self.plan(pods, altered, links, sandboxes)
        altered = copy.deepcopy(endpoints)
        altered[0]["status"]["external-identifiers"]["container-id"] = "b" * 64
        with self.assertRaises(target.EndpointIdentityError):
            self.plan(pods, altered, links, sandboxes)
        altered = copy.deepcopy(sandboxes)
        altered[0]["id"] = "b" * 64
        with self.assertRaises(target.EndpointIdentityError):
            self.plan(pods, endpoints, links, altered)

    def test_historical_unready_sandbox_is_not_current(self):
        pods, endpoints, links, sandboxes = fixture()
        historical = copy.deepcopy(sandboxes[0])
        historical["id"] = "c" * 64
        historical["state"] = "SANDBOX_NOTREADY"
        sandboxes.append(historical)
        self.assertEqual(len(self.plan(pods, endpoints, links, sandboxes)), 1)


if __name__ == "__main__":
    unittest.main()
