"""Fail-closed candidate for per-veth Cilium accept_local reconciliation in Kind.

No privileged writer, event source, shell, Kubernetes changes or automatic
lifecycle hooks are provided. A trusted caller must supply fresh primary
sources, an atomically guarded per-veth sysctl port and independent authority.
This source-only candidate never proves a Gateway 200 or B5 acceptance.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from scripts.platform.cilium_endpoint_veth import (
    EndpointIdentityError,
    bind_cilium_endpoint_veths,
    require_unchanged_endpoint_bindings,
)

_NODES = frozenset({"commonthing-staging-worker", "commonthing-staging-worker2"})
_RE = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE_KEYS = frozenset(
    {
        "kind_cluster",
        "cluster_uid",
        "node",
        "node_container_id",
        "node_netns_inode",
        "host_netns_inode",
        "namespace",
        "pods",
        "endpoints",
        "links",
        "sandboxes",
    }
)


class LifecycleReconcileBlocked(RuntimeError):
    """A failed effect must retain any uncertainty about earlier writes."""

    def __init__(
        self,
        reason: str,
        rolled_back: list[str],
        uncertain_effects: list[str],
        attempted: bool,
    ) -> None:
        super().__init__(f"Cilium veth reconcile blocked: {reason}")
        self.reason = reason
        self.rolled_back = tuple(rolled_back)
        self.uncertain_effects = tuple(uncertain_effects)
        self.attempted = attempted


def _sysctl(binding: Mapping[str, Any]) -> str:
    name = binding.get("ifname")
    if not isinstance(name, str) or re.fullmatch(r"lxc[0-9a-f]{12}", name) is None:
        raise EndpointIdentityError("not a bound Cilium host veth")
    return f"net.ipv4.conf.{name}.accept_local"


def _capture(read_sources: Callable[[], Mapping[str, Any]], node: str):
    raw = read_sources()
    if not isinstance(raw, Mapping) or set(raw) != _SOURCE_KEYS:
        raise EndpointIdentityError("incomplete primary-source inventory")
    if (
        node not in _NODES
        or raw["node"] != node
        or raw["kind_cluster"] != "commonthing-staging"
        or raw["namespace"] != "commonthing-staging"
    ):
        raise EndpointIdentityError("not an allowed independent Kind worker")
    uid, cid = raw["cluster_uid"], raw["node_container_id"]
    netns, hostns = raw["node_netns_inode"], raw["host_netns_inode"]
    if (
        not isinstance(uid, str)
        or not uid
        or not isinstance(cid, str)
        or _RE.fullmatch(cid) is None
        or type(netns) is not int
        or type(hostns) is not int
        or netns <= 0
        or hostns <= 0
        or netns == hostns
    ):
        raise EndpointIdentityError("unbound or shared production network namespace")
    bindings = bind_cilium_endpoint_veths(
        node=node,
        namespace="commonthing-staging",
        pods=raw["pods"],
        endpoints=raw["endpoints"],
        links=raw["links"],
        sandboxes=raw["sandboxes"],
    )
    return (uid, cid, netns, hostns), bindings


def reconcile_once(
    *,
    node: str,
    read_sources: Callable[[], Mapping[str, Any]],
    read_sysctl: Callable[[str], str],
    compare_and_write_sysctl: Callable[[str, str, str], None],
    authorize_effect: Callable[[str, str, Mapping[str, Any]], bool],
    max_changes: int = 8,
) -> dict[str, Any]:
    """One bounded lab-only reconcile. No default privileged port exists.

    The supplied writer must perform its OWN final exact interface-ifindex/MAC
    identity check and old-value comparison in the bound worker namespace.
    Implementing that atomicity and delivery of hotplug events is separate.
    """
    if type(max_changes) is not int or not 1 <= max_changes <= 8:
        raise EndpointIdentityError("invalid endpoint action budget")
    if not all(
        callable(fn)
        for fn in (
            read_sources,
            read_sysctl,
            compare_and_write_sysctl,
            authorize_effect,
        )
    ):
        raise EndpointIdentityError("missing trusted port")

    origin, bound = _capture(read_sources, node)
    if len(bound) > max_changes:
        raise EndpointIdentityError("per-call endpoint budget exceeded")
    if not bound:
        # An idle node is never a Gateway availability witness. Bind the
        # empty inventory twice and perform no privileged effect.
        now_identity, now_bindings = _capture(read_sources, node)
        if now_identity != origin or now_bindings:
            raise EndpointIdentityError("empty endpoint inventory drifted")
        return {
            "kind": "commonthing-kind-veth-reconcile-candidate-v1",
            "status": "no-ready-targets-not-gateway-pass",
            "node": node,
            "cluster_uid": origin[0],
            "modified": [],
            "bound_endpoints": 0,
            "does_not_establish": [
                "trusted-writer-atomicity",
                "endpoint-hotplug-watcher",
                "cilium-restart-convergence",
                "gateway-http-availability",
                "root-cause",
                "independent-R3",
                "B5",
                "production-cutover",
            ],
        }

    def current() -> None:
        now_identity, now_bindings = _capture(read_sources, node)
        if now_identity != origin:
            raise EndpointIdentityError("cluster, container or netns changed")
        require_unchanged_endpoint_bindings(bound, now_bindings)

    def value(item: Mapping[str, Any]) -> str:
        observed = read_sysctl(_sysctl(item))
        if observed not in {"0", "1"}:
            raise EndpointIdentityError("unknown per-veth sysctl readback")
        return observed

    def grant(item: Mapping[str, Any]) -> str:
        key = _sysctl(item)
        if authorize_effect(node, key, item) is not True:
            raise EndpointIdentityError(
                "independent scoped effect authorization absent"
            )
        return key

    changed: list[Mapping[str, Any]] = []
    in_flight: Mapping[str, Any] | None = None
    try:
        current()
        for item in bound:
            current()
            if value(item) == "1":
                continue
            key = grant(item)
            current()
            if value(item) != "0":
                raise EndpointIdentityError("sysctl drift before effect")
            in_flight = item
            compare_and_write_sysctl(key, "0", "1")
            changed.append(item)
            in_flight = None
            current()
            if value(item) != "1":
                raise EndpointIdentityError("post-write readback failed")
        current()
        if any(value(item) != "1" for item in bound):
            raise EndpointIdentityError("bound endpoint not configured")
    except Exception as error:
        to_revert = list(changed)
        if in_flight is not None and in_flight not in to_revert:
            to_revert.append(in_flight)
        reverted: list[str] = []
        unknown: list[str] = []
        for item in reversed(to_revert):
            key = _sysctl(item)
            try:
                current()
                if value(item) == "1":
                    grant(item)
                    current()
                    compare_and_write_sysctl(key, "1", "0")
                current()
                if value(item) != "0":
                    raise EndpointIdentityError("rollback not verified")
                reverted.append(key)
            except Exception:
                unknown.append(key)
        raise LifecycleReconcileBlocked(
            type(error).__name__, reverted, unknown, bool(to_revert)
        ) from error

    return {
        "kind": "commonthing-kind-veth-reconcile-candidate-v1",
        "status": (
            "scoped-effects-verified-not-gateway-pass"
            if bound
            else "no-ready-targets-not-gateway-pass"
        ),
        "node": node,
        "cluster_uid": origin[0],
        "modified": [_sysctl(item) for item in changed],
        "bound_endpoints": len(bound),
        "does_not_establish": [
            "trusted-writer-atomicity",
            "endpoint-hotplug-watcher",
            "cilium-restart-convergence",
            "gateway-http-availability",
            "root-cause",
            "independent-R3",
            "B5",
            "production-cutover",
        ],
    }
