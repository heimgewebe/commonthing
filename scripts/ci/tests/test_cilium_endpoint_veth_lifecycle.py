"""Adversarial pure-memory tests for source-only Kind veth reconciliation."""

import copy
import unittest
from unittest import mock

from scripts.ci.tests.test_cilium_endpoint_veth import fixture
from scripts.platform.cilium_endpoint_veth import EndpointIdentityError
from scripts.platform import cilium_endpoint_veth_lifecycle as lifecycle
from scripts.platform.cilium_endpoint_veth_lifecycle import (
    FrozenVethTarget,
    LifecycleReconcileBlocked,
    SysctlWriteOutcome,
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

    def ensure_target(self, target):
        self.assertIs(type(target), FrozenVethTarget)
        self.assertEqual(target.node, NODE)
        self.assertEqual(target.kind_cluster, "commonthing-staging")
        self.assertEqual(target.cluster_uid, "nonproduction-cluster-uid")
        self.assertEqual(target.node_container_id, "c" * 64)
        self.assertEqual(target.node_netns_inode, 4026532500)
        self.assertEqual(target.host_netns_inode, 4026531833)
        self.assertEqual(target.ifname, IFACE)
        self.assertEqual(target.ifindex, 21)
        self.assertEqual(target.mac, self.source["links"][0]["address"])
        self.assertEqual(target.sysctl_key, KEY)
        self.assertEqual(target.pod_uid, self.source["pods"][0]["metadata"]["uid"])
        self.assertIsInstance(target.endpoint_id, int)

    def read(self, target):
        self.ensure_target(target)
        return self.values[target.sysctl_key]

    def write(self, target, old, new):
        self.ensure_target(target)
        if self.values[target.sysctl_key] != old:
            return SysctlWriteOutcome.CAS_REJECTED
        self.effects.append((target, old, new))
        self.values[target.sysctl_key] = new
        return SysctlWriteOutcome.APPLIED

    def permit(self, target):
        self.ensure_target(target)
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

    def test_identity_bound_effect_and_no_global_sysctl(self):
        result = self.call()
        self.assertEqual(result["modified"], [KEY])
        self.assertEqual(result["bound_endpoints"], 1)
        self.assertEqual(self.values[KEY], "1")
        self.assertEqual(len(self.effects), 1)
        self.assertEqual(self.effects[0][1:], ("0", "1"))
        self.assertNotIn(".all.", str(self.effects))
        self.assertNotIn("src_valid_mark", str(self.effects))
        self.assertIn("gateway-http-availability", result["does_not_establish"])

    def test_already_configured_no_effect(self):
        self.values[KEY] = "1"
        out = self.call()
        self.assertEqual(out["modified"], [])
        self.assertEqual(self.effects, [])
        self.assertTrue(out["status"].endswith("not-gateway-pass"))

    def test_missing_authority_blocks_before_effect(self):
        self.authorized = False
        with self.assertRaises(LifecycleReconcileBlocked) as ctx:
            self.call()
        self.assertFalse(ctx.exception.attempted)
        self.assertEqual(ctx.exception.applied_effects, ())
        self.assertEqual(ctx.exception.uncertain_effects, ())
        self.assertEqual(self.effects, [])

    def test_refuse_host_namespace_and_production_cluster(self):
        self.source["node_netns_inode"] = self.source["host_netns_inode"]
        with self.assertRaises(EndpointIdentityError):
            self.call()
        self.source["node_netns_inode"] = 4026532500
        self.source["kind_cluster"] = "production"
        with self.assertRaises(EndpointIdentityError):
            self.call()
        self.assertEqual(self.effects, [])

    def test_registered_ifindex_required(self):
        self.source["endpoints"][0]["status"]["networking"]["interface-index"] = 999
        with self.assertRaises(EndpointIdentityError):
            self.call()
        self.assertEqual(self.effects, [])

    def test_container_drift_blocks_before_effect(self):
        calls = 0

        def changing_sources():
            nonlocal calls
            calls += 1
            result = self.sources()
            if calls >= 2:
                result["node_container_id"] = "d" * 64
            return result

        with self.assertRaises(LifecycleReconcileBlocked):
            self.call(read_sources=changing_sources)
        self.assertEqual(self.effects, [])

    def test_cas_rejected_by_concurrent_actor_never_rolls_back_their_value(self):
        def competing_writer(target, old, new):
            self.ensure_target(target)
            self.values[KEY] = "1"  # Other actor won the compare/exchange.
            return SysctlWriteOutcome.CAS_REJECTED

        with self.assertRaises(LifecycleReconcileBlocked) as ctx:
            self.call(compare_and_write_sysctl=competing_writer)
        self.assertTrue(ctx.exception.attempted)
        self.assertEqual(ctx.exception.applied_effects, ())
        self.assertEqual(ctx.exception.uncertain_effects, ())
        self.assertEqual(ctx.exception.rolled_back, ())
        self.assertEqual(self.values[KEY], "1")

    def test_false_return_is_not_treated_as_success(self):
        def malformed_writer(target, old, new):
            return False

        with self.assertRaises(LifecycleReconcileBlocked) as ctx:
            self.call(compare_and_write_sysctl=malformed_writer)
        self.assertEqual(ctx.exception.applied_effects, ())
        self.assertEqual(ctx.exception.uncertain_effects, (KEY,))
        self.assertEqual(self.values[KEY], "0")

    def test_unknown_outcome_never_undoes_another_actor(self):
        def unknown_writer(target, old, new):
            self.values[KEY] = "1"
            return SysctlWriteOutcome.UNKNOWN

        with self.assertRaises(LifecycleReconcileBlocked) as ctx:
            self.call(compare_and_write_sysctl=unknown_writer)
        self.assertEqual(ctx.exception.applied_effects, ())
        self.assertEqual(ctx.exception.uncertain_effects, (KEY,))
        self.assertEqual(ctx.exception.rolled_back, ())
        self.assertEqual(self.values[KEY], "1")

    def test_lost_acknowledgement_preserves_uncertain_effect(self):
        def failure_after_write(target, old, new):
            self.write(target, old, new)
            raise OSError("reply lost after effect")

        with self.assertRaises(LifecycleReconcileBlocked) as ctx:
            self.call(compare_and_write_sysctl=failure_after_write)
        self.assertEqual(ctx.exception.uncertain_effects, (KEY,))
        self.assertEqual(ctx.exception.rolled_back, ())
        self.assertEqual(self.values[KEY], "1")

    def test_recycled_ifindex_at_effect_port_must_reject(self):
        def pinned_identity_writer(target, old, new):
            self.ensure_target(target)
            self.source["links"][0]["ifindex"] = 1999
            # Trusted driver detects current ifindex differs from frozen target.
            return SysctlWriteOutcome.CAS_REJECTED

        with self.assertRaises(LifecycleReconcileBlocked) as ctx:
            self.call(compare_and_write_sysctl=pinned_identity_writer)
        self.assertEqual(ctx.exception.applied_effects, ())
        self.assertEqual(ctx.exception.rolled_back, ())
        self.assertEqual(self.effects, [])

    def test_recycled_ifindex_after_applied_reports_partial_effect(self):
        def recycled_after_write(target, old, new):
            result = self.write(target, old, new)
            self.source["links"][0]["ifindex"] = 1999
            return result

        with self.assertRaises(LifecycleReconcileBlocked) as ctx:
            self.call(compare_and_write_sysctl=recycled_after_write)
        self.assertEqual(ctx.exception.applied_effects, (KEY,))
        self.assertEqual(ctx.exception.rolled_back, ())
        self.assertEqual(self.values[KEY], "1")

    def test_interrupt_after_effect_not_silenced_or_unreported(self):
        def interrupted_writer(target, old, new):
            self.write(target, old, new)
            raise KeyboardInterrupt("operator interrupted just after write")

        with self.assertRaises(LifecycleReconcileBlocked) as ctx:
            self.call(compare_and_write_sysctl=interrupted_writer)
        self.assertEqual(ctx.exception.uncertain_effects, (KEY,))
        self.assertEqual(self.values[KEY], "1")

    def test_bad_value_and_action_budget(self):
        self.values[KEY] = "2"
        with self.assertRaises(LifecycleReconcileBlocked):
            self.call()
        self.assertEqual(self.effects, [])
        with self.assertRaises(EndpointIdentityError):
            self.call(max_changes=9)

    def test_empty_endpoint_inventory_never_accepted_gateway(self):
        self.source["pods"] = []
        self.source["endpoints"] = []
        self.source["sandboxes"] = []
        out = self.call()
        self.assertEqual(out["status"], "no-ready-targets-not-gateway-pass")
        self.assertEqual(out["modified"], [])
        self.assertEqual(self.effects, [])

    def test_known_applied_effect_then_second_cas_rejected_reports_partial(self):
        # Pure-source state-machine test: the identity function was checked in
        # the separate Pod/CRI/Cilium guard suite; here two validated targets
        # expose a dangerous non-atomic batch/rollback interaction.
        origin = ("uid", "c" * 64, 4026532500, 4026531833)
        one = {
            "node": NODE,
            "namespace": "commonthing-staging",
            "pod_uid": "pod1",
            "pod_name": "pod-a",
            "pod_ip": "10.0.1.51",
            "app": "commonthing-web",
            "endpoint_id": 1,
            "container_id": "a" * 64,
            "ifname": IFACE,
            "ifindex": 21,
            "mac": "02:02:02:02:02:01",
        }
        two = {
            **one,
            "pod_uid": "pod2",
            "pod_name": "pod-b",
            "pod_ip": "10.0.1.52",
            "endpoint_id": 2,
            "container_id": "b" * 64,
            "ifname": "lxc2aa99b88542e",
            "ifindex": 22,
            "mac": "02:02:02:02:02:02",
        }
        store = {f"net.ipv4.conf.{x['ifname']}.accept_local": "0" for x in (one, two)}
        writes = []

        def source_capture(read_sources, node):
            return origin, (copy.deepcopy(one), copy.deepcopy(two))

        def read(target):
            return store[target.sysctl_key]

        def write(target, old, new):
            if target.ifname == two["ifname"]:
                store[target.sysctl_key] = "1"  # Other actor's setting.
                return SysctlWriteOutcome.CAS_REJECTED
            self.assertEqual(store[target.sysctl_key], "0")
            store[target.sysctl_key] = new
            writes.append(target.sysctl_key)
            return SysctlWriteOutcome.APPLIED

        with mock.patch.object(lifecycle, "_capture", side_effect=source_capture):
            with self.assertRaises(LifecycleReconcileBlocked) as ctx:
                reconcile_once(
                    node=NODE,
                    read_sources=self.sources,
                    read_sysctl=read,
                    compare_and_write_sysctl=write,
                    authorize_effect=lambda target: True,
                )
        self.assertEqual(ctx.exception.applied_effects, (KEY,))
        self.assertEqual(ctx.exception.uncertain_effects, ())
        self.assertEqual(ctx.exception.rolled_back, ())
        self.assertEqual(store[KEY], "1")  # Our applied work, not reverted.
        self.assertEqual(store[f"net.ipv4.conf.{two['ifname']}.accept_local"], "1")
        self.assertEqual(writes, [KEY])


if __name__ == "__main__":
    unittest.main()
