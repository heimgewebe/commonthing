"""Read-only Cilium endpoint/host-veth identity guard for Kind staging.

No sysctl writes, no deployment and no privilege grants. A successful binding
is only a precondition for a later authorized lifecycle repair, not a fix.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

TARGET_APPS = frozenset({"commonthing-api", "commonthing-web"})
IFNAME_RE = re.compile(r"lxc[0-9a-f]{12}\Z")
HEX64_RE = re.compile(r"[0-9a-f]{64}\Z")
MAC_RE = re.compile(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\Z")


class EndpointIdentityError(ValueError):
    """A snapshot cannot authorize a host-interface effect."""


def _object(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EndpointIdentityError(f"{name} must be an object")
    return value


def _array(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise EndpointIdentityError(f"{name} must be a list")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise EndpointIdentityError(f"{name} is missing or invalid")
    return value


def _int(value: Any, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise EndpointIdentityError(f"{name} must be a positive integer")
    return value


def _mac(value: Any, name: str) -> str:
    value = _text(value, name)
    if not MAC_RE.fullmatch(value):
        raise EndpointIdentityError(f"{name} must be a lowercase MAC address")
    return value


def _ip(value: Any, name: str) -> str:
    try:
        parsed = ipaddress.IPv4Address(_text(value, name))
    except ipaddress.AddressValueError as exc:
        raise EndpointIdentityError(f"{name} must be an IPv4 address") from exc
    if parsed.is_loopback or parsed.is_multicast or parsed.is_unspecified:
        raise EndpointIdentityError(f"{name} is not a Pod unicast IP")
    return str(parsed)


def _container(value: Any, name: str, *, cri: bool = False) -> str:
    value = _text(value, name)
    if cri:
        if not value.startswith("containerd://"):
            raise EndpointIdentityError(f"{name} has unsupported runtime")
        value = value[len("containerd://") :]
    if not HEX64_RE.fullmatch(value):
        raise EndpointIdentityError(f"{name} is not a full container ID")
    return value


def _eligible_pod(value: Any, *, node: str, namespace: str) -> dict[str, Any] | None:
    pod = _object(value, "Pod")
    meta = _object(pod.get("metadata"), "Pod metadata")
    app = _object(meta.get("labels"), "Pod labels").get("app.kubernetes.io/name")
    if app not in TARGET_APPS:
        return None
    name = _text(meta.get("name"), "Pod name")
    if meta.get("namespace") != namespace:
        raise EndpointIdentityError("Gateway Pod belongs to another namespace")
    if _object(pod.get("spec"), "Pod spec").get("nodeName") != node:
        raise EndpointIdentityError("Gateway Pod is not on the bound Kind node")
    status = _object(pod.get("status"), "Pod status")
    if status.get("phase") != "Running":
        raise EndpointIdentityError("Gateway Pod is not Running")
    conditions = _array(status.get("conditions"), "Pod conditions")
    if (
        sum(
            isinstance(x, Mapping)
            and x.get("type") == "Ready"
            and x.get("status") == "True"
            for x in conditions
        )
        != 1
    ):
        raise EndpointIdentityError("Gateway Pod lacks unique Ready=True")
    containers = _array(status.get("containerStatuses"), "Pod containers")
    if not containers or any(
        not isinstance(x, Mapping) or x.get("ready") is not True for x in containers
    ):
        raise EndpointIdentityError("Gateway Pod contains an unready container")
    container_ids = {
        _container(x.get("containerID"), "Pod container ID", cri=True)
        for x in containers
    }
    if len(container_ids) != len(containers):
        raise EndpointIdentityError("duplicate Pod container IDs")
    return {
        "pod_name": name,
        "pod_uid": _text(meta.get("uid"), "Pod UID"),
        "app": app,
        "pod_ip": _ip(status.get("podIP"), "Pod IP"),
        "container_ids": container_ids,
    }


def bind_cilium_endpoint_veths(
    *,
    node: str,
    namespace: str,
    pods: Sequence[Mapping[str, Any]],
    endpoints: Sequence[Mapping[str, Any]],
    links: Sequence[Mapping[str, Any]],
    sandboxes: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Bind node-scoped Kubernetes Pods to exact ready Cilium veth endpoints.

    Callers must obtain Kubernetes Pod, Cilium registry and host link inventories
    from the same current isolated Kind host. Treat unknown/racing facts as
    errors. No mutation is implemented or authorized here.
    """
    node = _text(node, "Kind node")
    namespace = _text(namespace, "namespace")
    if namespace != "commonthing-staging" or node not in {
        "commonthing-staging-worker",
        "commonthing-staging-worker2",
    }:
        raise EndpointIdentityError("not an allowed isolated Kind staging worker")
    if not all(
        isinstance(x, (list, tuple)) for x in (pods, endpoints, links, sandboxes)
    ):
        raise EndpointIdentityError("missing Pod, Cilium or link inventory")

    wanted: dict[str, dict[str, Any]] = {}
    ip_seen: set[str] = set()
    for item in pods:
        pod = _eligible_pod(item, node=node, namespace=namespace)
        if pod is None:
            continue
        if pod["pod_name"] in wanted or pod["pod_ip"] in ip_seen:
            raise EndpointIdentityError("duplicate Pod name or IP")
        wanted[pod["pod_name"]] = pod
        ip_seen.add(pod["pod_ip"])

    # Cilium's external-identifiers.container-id is a CRI Pod sandbox,
    # not the workload's containerStatuses[*].containerID.
    ready_sandboxes: dict[str, str] = {}
    for item in sandboxes:
        sandbox = _object(item, "CRI sandbox")
        metadata = _object(sandbox.get("metadata"), "CRI sandbox metadata")
        pod_name = metadata.get("name")
        if metadata.get("namespace") != namespace or pod_name not in wanted:
            continue
        if sandbox.get("state") == "SANDBOX_NOTREADY":
            continue  # stale Pod sandbox attempt retained by containerd
        if sandbox.get("state") != "SANDBOX_READY":
            raise EndpointIdentityError("Gateway CRI sandbox is not READY")
        pod = wanted[pod_name]
        labels = _object(sandbox.get("labels"), "CRI sandbox labels")
        if (
            metadata.get("uid") != pod["pod_uid"]
            or labels.get("io.kubernetes.pod.uid") != pod["pod_uid"]
            or labels.get("io.kubernetes.pod.name") != pod_name
            or labels.get("io.kubernetes.pod.namespace") != namespace
            or labels.get("app.kubernetes.io/name") != pod["app"]
        ):
            raise EndpointIdentityError(
                "CRI Pod sandbox provenance differs from ready Pod"
            )
        if pod_name in ready_sandboxes:
            raise EndpointIdentityError(
                "multiple READY CRI sandboxes for one Gateway Pod"
            )
        ready_sandboxes[pod_name] = _container(sandbox.get("id"), "CRI Pod sandbox ID")
    if set(ready_sandboxes) != set(wanted):
        raise EndpointIdentityError(
            "missing unique READY CRI sandbox for a Gateway Pod"
        )
    if len(set(ready_sandboxes.values())) != len(ready_sandboxes):
        raise EndpointIdentityError("READY CRI sandbox reused across Gateway Pods")

    host: dict[tuple[str, int], Mapping[str, Any]] = {}
    link_names: set[str] = set()
    link_indices: set[int] = set()
    for item in links:
        link = _object(item, "host link")
        name = _text(link.get("ifname"), "link name")
        idx = _int(link.get("ifindex"), "link index")
        if name in link_names or idx in link_indices:
            raise EndpointIdentityError("duplicate host link identity")
        link_names.add(name)
        link_indices.add(idx)
        host[(name, idx)] = link

    found: dict[str, dict[str, Any]] = {}
    endpoint_ids: set[int] = set()
    interface_ids: set[tuple[str, int]] = set()
    for item in endpoints:
        e = _object(item, "Cilium endpoint")
        status = _object(e.get("status"), "Cilium endpoint status")
        ext = _object(status.get("external-identifiers"), "Cilium external identity")
        if ext.get("k8s-namespace") != namespace:
            continue
        pod_name = ext.get("k8s-pod-name")
        if pod_name not in wanted:
            continue
        pod = wanted[pod_name]
        eid = _int(e.get("id"), "endpoint ID")
        if eid in endpoint_ids or pod_name in found:
            raise EndpointIdentityError("duplicate Cilium endpoint")
        endpoint_ids.add(eid)
        if status.get("state") != "ready":
            raise EndpointIdentityError("Cilium endpoint is not ready")
        if ext.get("pod-name") != f"{namespace}/{pod_name}":
            raise EndpointIdentityError("Cilium Pod identity drift")
        cid = _container(ext.get("container-id"), "Cilium container ID")
        if cid != ready_sandboxes[pod_name]:
            raise EndpointIdentityError(
                "Cilium container differs from READY CRI Pod sandbox"
            )
        if ext.get("cni-attachment-id") != f"{cid}:eth0":
            raise EndpointIdentityError(
                "Cilium attachment ID differs from READY Pod sandbox"
            )
        net = _object(status.get("networking"), "Cilium networking")
        name = _text(net.get("interface-name"), "host veth name")
        if not IFNAME_RE.fullmatch(name):
            raise EndpointIdentityError("endpoint name is not a Cilium veth")
        idx = _int(net.get("interface-index"), "Cilium host veth index")
        key = name, idx
        if key in interface_ids:
            raise EndpointIdentityError("ambiguous Cilium host interface")
        interface_ids.add(key)
        link = host.get(key)
        if link is None:
            raise EndpointIdentityError("Cilium endpoint host veth missing or drifted")
        if _object(link.get("linkinfo"), "link info").get("info_kind") != "veth":
            raise EndpointIdentityError("host interface has wrong kind")
        mac = _mac(net.get("host-mac"), "Cilium host MAC")
        if _mac(link.get("address"), "host link MAC") != mac:
            raise EndpointIdentityError("host veth MAC differs / ifindex reused")
        ips = [
            x["ipv4"]
            for x in _array(net.get("addressing"), "Cilium IPs")
            if isinstance(x, Mapping) and x.get("ipv4")
        ]
        if len(ips) != 1 or _ip(ips[0], "Cilium IP") != pod["pod_ip"]:
            raise EndpointIdentityError("Cilium IP differs from Pod IP")
        found[pod_name] = {
            "endpoint_id": eid,
            "node": node,
            "namespace": namespace,
            "pod_name": pod_name,
            "pod_uid": pod["pod_uid"],
            "container_id": cid,
            "pod_ip": pod["pod_ip"],
            "app": pod["app"],
            "ifname": name,
            "ifindex": idx,
            "mac": mac,
        }
    if set(found) != set(wanted):
        raise EndpointIdentityError(
            "Cilium registry lacks one ready veth per Gateway Pod"
        )
    return tuple(sorted(found.values(), key=lambda x: (x["ifname"], x["ifindex"])))


def require_unchanged_endpoint_bindings(
    before: tuple[dict[str, Any], ...],
    after: tuple[dict[str, Any], ...],
) -> None:
    """Require a new identical primary readback immediately before any effect."""
    if not before or before != after:
        raise EndpointIdentityError("Cilium endpoint snapshot drifted or is empty")


def observe_accept_local(
    bindings: tuple[dict[str, Any], ...],
    *,
    read_value: Callable[[str], str],
) -> tuple[dict[str, Any], ...]:
    """Observe per-veth sysctl; never writes it or claims lifecycle readiness."""
    result = []
    for binding in bindings:
        name = binding.get("ifname")
        if not isinstance(name, str) or not IFNAME_RE.fullmatch(name):
            raise EndpointIdentityError("invalid sysctl target interface")
        try:
            current = read_value(name)
        except (OSError, ValueError, RuntimeError) as exc:
            raise EndpointIdentityError("sysctl read unavailable") from exc
        if current not in {"0", "1"}:
            raise EndpointIdentityError("invalid accept_local state")
        result.append(
            {
                "ifname": name,
                "ifindex": binding["ifindex"],
                "endpoint_id": binding["endpoint_id"],
                "node": binding["node"],
                "pod_name": binding["pod_name"],
                "accept_local": int(current),
                "status": (
                    "configured"
                    if current == "1"
                    else "requires-authorized-lifecycle-fix"
                ),
            }
        )
    return tuple(result)
