"""Source-only, fail-closed Kind Cilium host-veth reconcile candidate.

This module does NOT supply a privileged writer, kernel atomicity, hotplug
observer, Cilium restart handler, or gateway availability proof. All ports
must be supplied independently by a trusted operator with source/effect
authority. A real writer must pin the exact network namespace and veth
identity under its own atomic compare-and-write critical section.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from scripts.platform.cilium_endpoint_veth import (
    EndpointIdentityError,
    bind_cilium_endpoint_veths,
    require_unchanged_endpoint_bindings,
)

_NODES = frozenset({"commonthing-staging-worker", "commonthing-staging-worker2"})
_CONTAINER_RE = re.compile(r"[0-9a-f]{64}\Z")
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
_NOT_ESTABLISHED = (
    "trusted-writer-atomicity",
    "endpoint-hotplug-watcher",
    "cilium-restart-convergence",
    "gateway-http-availability",
    "root-cause",
    "independent-R3",
    "B5",
    "production-cutover",
)


class SysctlWriteOutcome(Enum):
    """Only a trusted identity-pinned driver can truthfully return APPLIED."""

    APPLIED = "APPLIED"
    CAS_REJECTED = "CAS_REJECTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class FrozenVethTarget:
    """Immutable authority input: a key alone cannot identify a Linux veth.

    This is a request contract, not a native verified open ifindex handle.
    A trusted writer must verify *all* fields against kernel/CRI/Cilium sources
    inside the bound, pinned network namespace before and during the effect.
    """

    sysctl_key: str
    kind_cluster: str
    cluster_uid: str
    node: str
    namespace: str
    node_container_id: str
    node_netns_inode: int
    host_netns_inode: int
    pod_uid: str
    pod_ip: str
    sandbox_id: str
    endpoint_id: int
    ifname: str
    ifindex: int
    mac: str


class LifecycleReconcileBlocked(RuntimeError):
    """Failure is never a PASS; preserve original and unknown effects."""

    def __init__(
        self,
        reason: str,
        *,
        applied_effects: list[str],
        uncertain_effects: list[str],
        attempted: bool,
    ) -> None:
        super().__init__(f"Cilium veth reconcile blocked: {reason}")
        self.reason = reason
        self.applied_effects = tuple(applied_effects)
        self.uncertain_effects = tuple(uncertain_effects)
        self.attempted = attempted
        # No automatic rollback: a concurrent owner may have set the value.
        # A separate, explicitly authorized recovery must establish causality.
        self.rolled_back: tuple[str, ...] = ()


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
        or _CONTAINER_RE.fullmatch(cid) is None
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


def _freeze_target(
    node: str,
    origin: tuple[str, str, int, int],
    item: Mapping[str, Any],
) -> FrozenVethTarget:
    """Carry every already-validated endpoint identity into the effect port."""
    return FrozenVethTarget(
        sysctl_key=_sysctl(item),
        kind_cluster="commonthing-staging",
        cluster_uid=origin[0],
        node=node,
        namespace="commonthing-staging",
        node_container_id=origin[1],
        node_netns_inode=origin[2],
        host_netns_inode=origin[3],
        pod_uid=item["pod_uid"],
        pod_ip=item["pod_ip"],
        sandbox_id=item["container_id"],
        endpoint_id=item["endpoint_id"],
        ifname=item["ifname"],
        ifindex=item["ifindex"],
        mac=item["mac"],
    )


def _report(
    node: str,
    cluster_uid: str,
    *,
    count: int,
    modified: list[str],
) -> dict[str, Any]:
    return {
        "kind": "commonthing-kind-veth-reconcile-candidate-v1",
        "status": (
            "scoped-effects-verified-not-gateway-pass"
            if count
            else "no-ready-targets-not-gateway-pass"
        ),
        "node": node,
        "cluster_uid": cluster_uid,
        "modified": modified,
        "bound_endpoints": count,
        "does_not_establish": list(_NOT_ESTABLISHED),
    }


def reconcile_once(
    *,
    node: str,
    read_sources: Callable[[], Mapping[str, Any]],
    read_sysctl: Callable[[FrozenVethTarget], str],
    compare_and_write_sysctl: Callable[
        [FrozenVethTarget, str, str], SysctlWriteOutcome
    ],
    authorize_effect: Callable[[FrozenVethTarget], bool],
    max_changes: int = 8,
) -> dict[str, Any]:
    """One bounded, nonproduction, identity-bound effect request.

    The trusted writer's responsibility includes a pinned Kind node netns and
    an atomic full-identity (ifname, ifindex, MAC, Pod/CRI/Cilium provenance,
    cluster/container/netns identity) plus old-value CAS within that namespace.
    No compliant writer is shipped here. The caller's pre-read is NOT that
    atomic check. A CAS_REJECTED outcome proves no write *by this callback*;
    UNKNOWN, exceptions, malformed results or interruptions are uncertain.
    Automatic 1->0 rollback is deliberately forbidden: it could undo a
    legitimate other actor's concurrent modification.
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
        # No targets => no effect and never a Gateway availability proof.
        now_identity, now_bindings = _capture(read_sources, node)
        if now_identity != origin or now_bindings:
            raise EndpointIdentityError("empty endpoint inventory drifted")
        return _report(node, origin[0], count=0, modified=[])

    targets = tuple(_freeze_target(node, origin, item) for item in bound)

    def current() -> None:
        now_identity, now_bindings = _capture(read_sources, node)
        if now_identity != origin:
            raise EndpointIdentityError("cluster, container or netns changed")
        require_unchanged_endpoint_bindings(bound, now_bindings)

    def value(target: FrozenVethTarget) -> str:
        observation = read_sysctl(target)
        if observation not in {"0", "1"}:
            raise EndpointIdentityError("unknown per-veth sysctl readback")
        return observation

    applied: list[str] = []
    uncertain: list[str] = []
    in_flight: FrozenVethTarget | None = None
    attempted = False
    try:
        current()
        for target in targets:
            current()
            if value(target) == "1":
                continue
            if authorize_effect(target) is not True:
                raise EndpointIdentityError(
                    "independent scoped effect authorization absent"
                )
            current()
            if value(target) != "0":
                raise EndpointIdentityError("sysctl drift before effect")

            in_flight = target
            attempted = True
            outcome = compare_and_write_sysctl(target, "0", "1")
            if type(outcome) is not SysctlWriteOutcome:
                # An untyped False or None might mean either no write or a
                # successful write with lost acknowledgement.
                uncertain.append(target.sysctl_key)
                in_flight = None
                raise EndpointIdentityError("untrusted sysctl writer outcome")
            if outcome is SysctlWriteOutcome.UNKNOWN:
                uncertain.append(target.sysctl_key)
                in_flight = None
                raise EndpointIdentityError("effect outcome is unknown")
            if outcome is SysctlWriteOutcome.CAS_REJECTED:
                in_flight = None
                raise EndpointIdentityError("effect CAS rejected or target recycled")
            if outcome is not SysctlWriteOutcome.APPLIED:
                uncertain.append(target.sysctl_key)
                in_flight = None
                raise EndpointIdentityError("unsupported sysctl write outcome")

            applied.append(target.sysctl_key)
            in_flight = None
            current()
            if value(target) != "1":
                raise EndpointIdentityError("post-write sysctl verification failed")

        current()
        if any(value(target) != "1" for target in targets):
            raise EndpointIdentityError("bound endpoint not configured")
    except BaseException as error:
        if in_flight is not None and in_flight.sysctl_key not in uncertain:
            uncertain.append(in_flight.sysctl_key)
        # Deliberately do not compensate any applied key automatically.
        # For concurrent changes, no causally safe rollback token exists.
        raise LifecycleReconcileBlocked(
            type(error).__name__,
            applied_effects=applied,
            uncertain_effects=uncertain,
            attempted=attempted,
        ) from error

    return _report(node, origin[0], count=len(targets), modified=list(applied))
