"""Adversarial tests: no sysctl, Docker, Cilium or Kubernetes effect occurs."""

import copy
import unittest

from scripts.ci.tests.test_cilium_endpoint_veth import fixture
from scripts.platform.cilium_endpoint_veth import EndpointIdentityError
from scripts.platform.cilium_endpoint_veth_lifecycle import (
    LifecycleReconcileBlocked,
    reconcile_once,
)

NODE = "commonthing-staging-worker"
IFACE = "lxc2aa99b88542d"
KEY = f"net.ipv4.conf.{IFACE}.accept_local"


class ScopedLifecycleTests(unittest.TestCase):
    def setUp(self):
        pods, endpoints, links, sandboxes = fixture()
        self.source = {
            "kind_cluster": "commonthing-staging",
            "cluster_uid": "nonproduction-cluster-uid",
            "node": NODE,
            "node_container_id": "c" * 64,
            "node_netns_inode": 4026532500,
            "host_netns_inode": 4026531833,
            "namespace": "commonthing-staging",
            "pods": pods,
            "endpoints": endpoints,
            "links": links,
            "sandboxes": sandboxes,
        }
        self.values = {KEY: "0"}
        self.effects = []
        self.authorized = True

    def sources(self):
        return copy.deepcopy(self.source)

    def read(self, key):
        return self.values[key]

    def write(self, key, old, new):
        self.assertEqual(self.values[key], old)
        self.assertIn(key, self.values)
        self.effects.append((key, old, new))
        self.values[key] = new

    def permit(self, node, key, bound):
        self.assertEqual(node, NODE)
        self.assertEqual(key, KEY)
        self.assertEqual(bound["ifindex"], 21)
        return self.authorized

    def call(self, **overrides):
        ports = {
            "node": NODE,
            "read_sources": self.sources,
            "read_sysctl": self.read,
            "compare_and_write_sysctl": self.write,
            "authorize_effect": self.permit,
        }
        return reconcile_once(**{**ports, **overrides})

    def test_exact_veth_only_is_set_and_verified(self):
        result = self.call()
        self.assertEqual(result["modified"], [KEY])
        self.assertEqual(result["bound_endpoints"], 1)
        self.assertEqual(self.values[KEY], "1")
        self.assertEqual(self.effects, [(KEY, "0", "1")])
        self.assertIn("gateway-http-availability", result["does_not_establish"])

    def test_already_configured_no_effect(self):
        self.values[KEY] = "1"
        out = self.call()
        self.assertEqual(out["modified"], [])
        self.assertEqual(self.effects, [])
        self.assertTrue(out["status"].endswith("not-gateway-pass"))

    def test_missing_independent_authority_fails_before_write(self):
        self.authorized = False
        with self.assertRaises(LifecycleReconcileBlocked) as ctx:
            self.call()
        self.assertFalse(ctx.exception.attempted)
        self.assertEqual(self.effects, [])

    def test_host_namespace_and_production_cluster_are_forbidden(self):
        self.source["node_netns_inode"] = self.source["host_netns_inode"]
        with self.assertRaises(EndpointIdentityError):
            self.call()
        self.source["node_netns_inode"] = 4026532500
        self.source["kind_cluster"] = "production"
        with self.assertRaises(EndpointIdentityError):
            self.call()
        self.assertEqual(self.effects, [])

    def test_exact_registered_cilium_ifindex_required(self):
        self.source["endpoints"][0]["status"]["networking"]["interface-index"] = 999
        with self.assertRaises(EndpointIdentityError):
            self.call()
        self.assertEqual(self.effects, [])

    def test_no_global_sysctl_can_be_addressed(self):
        self.call()
        keys = [effect[0] for effect in self.effects]
        self.assertEqual(keys, [KEY])
        self.assertFalse(any(".all." in x or "src_valid_mark" in x for x in keys))

    def test_container_identity_drift_is_blocking(self):
        count = 0

        def changing_sources():
            nonlocal count
            count += 1
            copy = self.sources()
            if count >= 2:
                copy["node_container_id"] = "d" * 64
            return copy

        with self.assertRaises(LifecycleReconcileBlocked):
            self.call(read_sources=changing_sources)
        self.assertEqual(self.effects, [])

    def test_writer_raises_after_real_effect_then_rollback_is_verified(self):
        def failure_after_write(key, before, after):
            self.write(key, before, after)
            if after == "1":
                raise OSError("ack lost after the effect")

        with self.assertRaises(LifecycleReconcileBlocked) as ctx:
            self.call(compare_and_write_sysctl=failure_after_write)
        self.assertTrue(ctx.exception.attempted)
        self.assertEqual(ctx.exception.rolled_back, (KEY,))
        self.assertEqual(ctx.exception.uncertain_effects, ())
        self.assertEqual(self.values[KEY], "0")

    def test_recreated_or_reused_interface_never_rolls_back_wrong_target(self):
        def recycled_after_write(key, before, after):
            self.write(key, before, after)
            self.source["links"][0]["ifindex"] = 1999

        with self.assertRaises(LifecycleReconcileBlocked) as ctx:
            self.call(compare_and_write_sysctl=recycled_after_write)
        self.assertEqual(ctx.exception.uncertain_effects, (KEY,))
        self.assertEqual(self.values[KEY], "1")
        self.assertEqual(self.effects, [(KEY, "0", "1")])

    def test_bad_readback_and_effect_budget_block(self):
        self.values[KEY] = "2"
        with self.assertRaises(LifecycleReconcileBlocked):
            self.call()
        self.assertEqual(self.effects, [])
        with self.assertRaises(EndpointIdentityError):
            self.call(max_changes=9)

    def test_empty_endpoint_inventory_is_not_an_accepted_gateway(self):
        self.source["pods"] = []
        self.source["endpoints"] = []
        self.source["sandboxes"] = []
        out = self.call()
        self.assertEqual(out["status"], "no-ready-targets-not-gateway-pass")
        self.assertEqual(out["modified"], [])
        self.assertEqual(self.effects, [])


if __name__ == "__main__":
    unittest.main()
