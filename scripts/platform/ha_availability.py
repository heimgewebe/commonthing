from __future__ import annotations

import json
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import kind_reference as ref
from ha_common import ROOT

BASELINE_API_IMAGE = "weltgewebe-api:local"
CONTINUOUS_AVAILABILITY_INTERVAL_SECONDS = 2.0
DIRECT_API_PROBE_LABEL = "weltgewebe-ha-direct-probe"

def projection_sample_url(node: str, url: str, marker: str) -> dict[str, Any]:
    started = time.monotonic()
    result = subprocess.run(
        [
            "docker",
            "exec",
            node,
            "curl",
            "--fail",
            "--silent",
            "--show-error",
            "--max-time",
            "2",
            url,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    stderr = result.stderr if isinstance(result.stderr, str) else ""
    sample: dict[str, Any] = {
        "available": False,
        "returncode": result.returncode,
        "duration_seconds": round(time.monotonic() - started, 3),
        "stderr": stderr.strip()[:500],
        "url": url,
    }
    if result.returncode != 0:
        return sample
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        sample["json_error"] = str(error)
        return sample
    if not isinstance(payload, list):
        sample["response_items"] = None
        sample["response_shape"] = type(payload).__name__
        return sample
    sample["available"] = any(
        isinstance(item, dict) and item.get("id") == marker for item in payload
    )
    sample["response_items"] = len(payload)
    return sample

def gateway_projection_sample(
    node: str, address: str, port: int, marker: str
) -> dict[str, Any]:
    return projection_sample_url(
        node, f"http://{address}:{port}/api/nodes", marker
    )

def gateway_projection_available(
    node: str, address: str, port: int, marker: str
) -> bool:
    return bool(
        gateway_projection_sample(node, address, port, marker)["available"]
    )

def gateway_observer_binding(
    kind: str,
    cluster: str,
    addresses: list[str],
) -> dict[str, Any]:
    gateway_addresses = sorted(set(addresses))
    if not gateway_addresses:
        raise ref.ProofError("gateway observer binding has no advertised addresses")

    nodes = sorted(set(ref.kind_nodes(kind, cluster)))
    node_addresses: dict[str, str] = {}
    owners_by_address: dict[str, list[str]] = {}
    for node in nodes:
        address = ref.output(
            [
                "docker",
                "inspect",
                "--format",
                '{{(index .NetworkSettings.Networks "kind").IPAddress}}',
                node,
            ]
        )
        if not address:
            continue
        node_addresses[node] = address
        owners_by_address.setdefault(address, []).append(node)

    invalid_owners = {
        address: owners_by_address.get(address, [])
        for address in gateway_addresses
        if len(owners_by_address.get(address, [])) != 1
    }
    if invalid_owners:
        raise ref.ProofError(
            "advertised gateway addresses do not map to exactly one kind node owner: "
            f"{invalid_owners}"
        )

    observers = [
        node
        for node in nodes
        if "control-plane" in node
        and node_addresses.get(node) not in gateway_addresses
    ]
    if len(observers) != 1:
        raise ref.ProofError(
            "gateway availability requires exactly one non-owner control-plane observer: "
            f"{observers}"
        )
    observer = observers[0]
    return {
        "gateway_addresses": gateway_addresses,
        "observer_node": observer,
        "observer_address": node_addresses[observer],
        "owner_by_address": {
            address: owners_by_address[address][0]
            for address in gateway_addresses
        },
        "captured_before_failure": True,
    }

def gateway_observer_sample(
    kind: str,
    cluster: str,
    addresses: list[str],
    port: int,
    marker: str,
    *,
    observer_binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    gateway_addresses = sorted(set(addresses))
    if not gateway_addresses:
        raise ref.ProofError("gateway observer probe has no advertised addresses")

    binding = observer_binding or gateway_observer_binding(
        kind, cluster, gateway_addresses
    )
    known_addresses = {
        str(address) for address in binding.get("gateway_addresses", [])
    }
    unknown_addresses = sorted(set(gateway_addresses) - known_addresses)
    if unknown_addresses:
        raise ref.ProofError(
            "gateway advertised addresses drifted outside the pre-failure binding: "
            f"{unknown_addresses}"
        )
    observer = str(binding.get("observer_node", ""))
    observer_address = str(binding.get("observer_address", ""))
    owners = binding.get("owner_by_address")
    if not observer or not observer_address or not isinstance(owners, dict):
        raise ref.ProofError("gateway owner-local binding is incomplete")
    if any(not isinstance(owners.get(addr), str) or not owners[addr] for addr in gateway_addresses):
        raise ref.ProofError("gateway owner-local binding has missing listener owners")

    def run_address(address: str) -> dict[str, Any]:
        owner = owners[address]
        try:
            sample = gateway_projection_sample(owner, address, port, marker)
        except (subprocess.SubprocessError, OSError) as error:
            sample = {
                "available": False,
                "probe_error": f"{type(error).__name__}: {error}",
                "url": f"http://{address}:{port}/api/nodes",
            }
        return {"node": owner, "address": address, **sample}

    with ThreadPoolExecutor(max_workers=min(8, len(gateway_addresses))) as executor:
        probes = list(executor.map(run_address, gateway_addresses))

    failed_paths = [probe for probe in probes if probe.get("available") is not True]
    successful_addresses = [
        str(probe["address"])
        for probe in probes
        if probe.get("available") is True
    ]
    return {
        "available": bool(successful_addresses),
        "probe_mode": "owner-local-any-advertised-address",
        "observer_binding": (
            "pre-failure-snapshot" if observer_binding is not None else "live-preflight"
        ),
        "observer_node": observer,
        "observer_address": observer_address,
        "address_owners": {
            address: owners[address]
            for address in gateway_addresses
        },
        "gateway_address_count": len(gateway_addresses),
        "successful_paths": len(probes) - len(failed_paths),
        "failed_paths": len(failed_paths),
        "successful_addresses": successful_addresses,
        "path_failures": failed_paths,
    }

def direct_api_probe_target(kubectl: str) -> tuple[str, int]:
    service = json.loads(
        ref.output(
            [
                kubectl,
                "-n",
                "weltgewebe",
                "get",
                "service/weltgewebe-api",
                "-o",
                "json",
            ]
        )
    )
    address = str(service.get("spec", {}).get("clusterIP", ""))
    if not address or address == "None":
        raise ref.ProofError("direct API service has no stable clusterIP")
    ports = service.get("spec", {}).get("ports", [])
    port = next(
        (int(item["port"]) for item in ports if item.get("name") == "http"),
        8080,
    )
    return address, port

def direct_api_probe_document(name: str, zone: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": "weltgewebe",
            "labels": {
                "app.kubernetes.io/name": DIRECT_API_PROBE_LABEL,
                "app.kubernetes.io/component": "proof",
            },
        },
        "spec": {
            "automountServiceAccountToken": False,
            "restartPolicy": "Never",
            "nodeSelector": {
                "weltgewebe.net/data-node": "true",
                "topology.kubernetes.io/zone": zone,
            },
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 10001,
                "runAsGroup": 10001,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [{
                "name": "probe",
                "image": BASELINE_API_IMAGE,
                "imagePullPolicy": "Never",
                "command": ["/bin/sh", "-c", "sleep 7200"],
                "resources": {
                    "requests": {"cpu": "5m", "memory": "16Mi"},
                    "limits": {"cpu": "50m", "memory": "64Mi"},
                },
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"]},
                    "readOnlyRootFilesystem": True,
                },
            }],
        },
    }

def create_direct_api_probes(kubectl: str, zones: list[str]) -> list[str]:
    normalized = sorted({str(zone).strip() for zone in zones if str(zone).strip()})
    if len(normalized) != 3:
        raise ref.ProofError(f"direct API proof requires exactly three zones, got {normalized}")
    names: list[str] = []
    for zone in normalized:
        name = f"ha-direct-api-probe-{zone}"
        ref.apply_yaml(kubectl, direct_api_probe_document(name, zone))
        names.append(name)
    for name in names:
        ref.wait_condition(kubectl, "weltgewebe", f"pod/{name}", "Ready", "3m")
    return names

def direct_api_projection_sample(kubectl: str, pod: str, url: str, marker: str) -> dict[str, Any]:
    started = time.monotonic()
    try:
        result = subprocess.run(
            [kubectl, "--request-timeout=3s", "-n", "weltgewebe", "exec", pod, "--", "wget", "-qO-", "-T", "2", url],
            cwd=ROOT, text=True, capture_output=True, timeout=5, check=False,
        )
    except subprocess.TimeoutExpired:
        return {"available": False, "pod": pod, "duration_seconds": round(time.monotonic()-started,3), "probe_error": "kubectl exec timed out", "url": url}
    sample: dict[str, Any] = {
        "available": False, "pod": pod, "returncode": result.returncode,
        "duration_seconds": round(time.monotonic()-started,3),
        "stderr": (result.stderr or "").strip()[:300], "url": url,
    }
    if result.returncode != 0:
        return sample
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        sample["json_error"] = str(error); return sample
    if not isinstance(payload, list):
        sample["response_shape"] = type(payload).__name__; return sample
    sample["available"] = any(isinstance(item, dict) and item.get("id") == marker for item in payload)
    sample["response_items"] = len(payload)
    return sample

def direct_api_probe_sample(kubectl: str, probe_pods: list[str], address: str, port: int, marker: str) -> dict[str, Any]:
    pods = sorted(set(probe_pods))
    if not pods:
        raise ref.ProofError("direct API probe has no same-namespace probe pods")
    url = f"http://{address}:{port}/nodes"
    with ThreadPoolExecutor(max_workers=min(8, len(pods))) as executor:
        probes = list(executor.map(lambda pod: direct_api_projection_sample(kubectl, pod, url, marker), pods))
    failed = [probe for probe in probes if probe.get("available") is not True]
    successful = [str(probe["pod"]) for probe in probes if probe.get("available") is True]
    return {
        "available": bool(successful), "probe_mode": "same-namespace-zone-probes-any",
        "probe_count": len(probes), "successful_probes": successful,
        "failed_paths": len(failed), "path_failures": failed,
        "service_address": address, "service_port": port,
    }

def delete_direct_api_probes(kubectl: str, probe_pods: list[str]) -> dict[str, Any]:
    pods = sorted(set(probe_pods))
    if not pods:
        return {"returncode": 0, "deleted": 0}
    result = subprocess.run(
        [kubectl, "-n", "weltgewebe", "delete", "pod", *pods, "--ignore-not-found=true", "--wait=true"],
        cwd=ROOT, text=True, capture_output=True, timeout=90, check=False,
    )
    return {"returncode": result.returncode, "deleted": len(pods), "stderr": (result.stderr or "").strip()[:500]}

def summarize_continuous_availability(
    samples: list[dict[str, Any]],
    *,
    window_seconds: float,
    interval_seconds: float,
) -> dict[str, Any]:
    if not samples:
        raise ref.ProofError("continuous availability monitor produced no samples")
    failed = [sample for sample in samples if sample.get("available") is not True]
    spacings = [
        float(samples[index]["elapsed_seconds"])
        - float(samples[index - 1]["elapsed_seconds"])
        for index in range(1, len(samples))
    ]
    measurement_gap_seconds = sum(
        max(0.0, spacing - interval_seconds) for spacing in spacings
    )
    outage_seconds = 0.0
    outage_started: float | None = None
    longest_outage = 0.0
    for sample in samples:
        elapsed = float(sample["elapsed_seconds"])
        if sample.get("available") is True:
            if outage_started is not None:
                duration = max(0.0, elapsed - outage_started)
                outage_seconds += duration
                longest_outage = max(longest_outage, duration)
                outage_started = None
        elif outage_started is None:
            outage_started = elapsed
    if outage_started is not None:
        duration = max(0.0, window_seconds - outage_started)
        outage_seconds += duration
        longest_outage = max(longest_outage, duration)
    return {
        "sample_count": len(samples),
        "failed_samples": len(failed),
        "measurement_gap_seconds": round(measurement_gap_seconds, 3),
        "max_sample_spacing_seconds": round(max(spacings, default=0.0), 3),
        "measured_outage_seconds": round(outage_seconds, 3),
        "longest_measured_outage_seconds": round(longest_outage, 3),
        "measurement_interval_seconds": interval_seconds,
        "window_seconds": round(window_seconds, 3),
        "failure_samples": failed[:10],
    }

def summarize_failover_recovery(
    samples: list[dict[str, Any]],
    *,
    failure_elapsed_seconds: float,
    interval_seconds: float,
) -> dict[str, Any]:
    post_failure = [
        sample
        for sample in samples
        if float(sample["elapsed_seconds"]) >= failure_elapsed_seconds
    ]
    if not post_failure:
        raise ref.ProofError("continuous availability has no post-failure samples")
    first_failed_index = next(
        (
            index
            for index, sample in enumerate(post_failure)
            if sample.get("available") is not True
        ),
        None,
    )
    if first_failed_index is None:
        return {
            "outage_observed": False,
            "observed_rto_seconds": 0.0,
            "failed_samples_before_recovery": 0,
            "sampling_resolution_seconds": interval_seconds,
            "first_post_failure_sample_seconds": round(
                max(
                    0.0,
                    float(post_failure[0]["elapsed_seconds"])
                    - failure_elapsed_seconds,
                ),
                3,
            ),
            "interpretation": (
                "no outage was sampled; a shorter interruption below the sampling resolution "
                "is not excluded"
            ),
        }
    recovery = next(
        (
            sample
            for sample in post_failure[first_failed_index + 1 :]
            if sample.get("available") is True
        ),
        None,
    )
    if recovery is None:
        raise ref.ProofError("continuous availability did not observe recovery after failure")
    failed_before_recovery = sum(
        1
        for sample in post_failure[: post_failure.index(recovery)]
        if sample.get("available") is not True
    )
    return {
        "outage_observed": True,
        "observed_rto_seconds": round(
            max(
                0.0,
                float(recovery["elapsed_seconds"]) - failure_elapsed_seconds,
            ),
            3,
        ),
        "failed_samples_before_recovery": failed_before_recovery,
        "sampling_resolution_seconds": interval_seconds,
        "first_failed_sample_seconds": round(
            max(
                0.0,
                float(post_failure[first_failed_index]["elapsed_seconds"])
                - failure_elapsed_seconds,
            ),
            3,
        ),
        "recovery_observation": "first-success-after-first-failed-sample",
    }

class ContinuousAvailabilityMonitor:
    def __init__(
        self,
        kubectl: str,
        kind: str,
        cluster: str,
        marker: str,
        gateway_port: int,
        direct_probe_pods: list[str],
        *,
        interval_seconds: float = CONTINUOUS_AVAILABILITY_INTERVAL_SECONDS,
    ) -> None:
        self.kubectl = kubectl
        self.kind = kind
        self.cluster = cluster
        self.marker = marker
        self.gateway_port = gateway_port
        self.interval_seconds = interval_seconds
        self.gateway_addresses = sorted(set(ref.gateway_addresses(kubectl)))
        self.gateway_binding = gateway_observer_binding(
            kind, cluster, self.gateway_addresses
        )
        self.direct_probe_pods = sorted(set(direct_probe_pods))
        if len(self.direct_probe_pods) != 3:
            raise ref.ProofError("continuous availability monitor requires three direct API probe pods")
        self.direct_address, self.direct_port = direct_api_probe_target(kubectl)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = 0.0
        self._failure_elapsed_seconds: float | None = None
        self._failure_label: str | None = None
        self._gateway_samples: list[dict[str, Any]] = []
        self._direct_samples: list[dict[str, Any]] = []

    def start(self) -> None:
        if self._thread is not None:
            raise ref.ProofError("continuous availability monitor already started")
        self._started = time.monotonic()
        self._thread = threading.Thread(
            target=self._run,
            name="weltgewebe-ha-continuous-availability",
            daemon=True,
        )
        self._thread.start()

    def mark_failure(self, label: str) -> None:
        if self._thread is None or self._started <= 0:
            raise ref.ProofError("continuous availability monitor is not running")
        if self._failure_elapsed_seconds is not None:
            raise ref.ProofError("continuous availability failure was already marked")
        self._failure_elapsed_seconds = time.monotonic() - self._started
        self._failure_label = label

    def _require_current_gateway_binding(self) -> None:
        """Prevent a stale owner-local snapshot from accepting cross-node HTTP."""
        observed = sorted(set(ref.gateway_addresses(self.kubectl)))
        if observed != self.gateway_addresses:
            raise ref.ProofError("Gateway listener addresses drifted during continuous availability")
        live = gateway_observer_binding(self.kind, self.cluster, observed)
        for key in (
            "gateway_addresses", "owner_by_address",
            "observer_node", "observer_address",
        ):
            if live.get(key) != self.gateway_binding.get(key):
                raise ref.ProofError(
                    f"Gateway listener owner binding drifted during continuous availability: {key}"
                )

    def _sample(self, channel: str) -> dict[str, Any]:
        try:
            if channel == "gateway":
                self._require_current_gateway_binding()
                result = gateway_observer_sample(
                    self.kind,
                    self.cluster,
                    self.gateway_addresses,
                    self.gateway_port,
                    self.marker,
                    observer_binding=self.gateway_binding,
                )
                # HTTP can succeed via a stale owner's cross-node path after
                # readdressing. Bind both sides of the single measured sample.
                self._require_current_gateway_binding()
                return {
                    "available": bool(result.get("available")),
                    "failed_paths": int(result.get("failed_paths", 0)),
                }
            result = direct_api_probe_sample(
                self.kubectl, self.direct_probe_pods, self.direct_address, self.direct_port, self.marker
            )
            return {
                "available": bool(result.get("available")),
                "failed_paths": int(result.get("failed_paths", 0)),
                "successful_probes": result.get("successful_probes", []),
                "path_failures": result.get("path_failures", [])[:3],
            }
        except (
            OSError,
            subprocess.SubprocessError,
            json.JSONDecodeError,
            ref.ProofError,
        ) as error:
            return {
                "available": False,
                "probe_error": f"{type(error).__name__}: {error}"[:300],
            }

    def _run(self) -> None:
        next_due = self._started
        while not self._stop.is_set():
            sampled_at = time.monotonic()
            elapsed = sampled_at - self._started
            # Probe the two user-visible paths at the same sampling instant.
            # Sequential probing would make a slow Gateway sample delay direct API evidence.
            with ThreadPoolExecutor(max_workers=2) as executor:
                gateway_future = executor.submit(self._sample, "gateway")
                direct_future = executor.submit(self._sample, "direct")
                gateway = gateway_future.result()
                direct = direct_future.result()
            gateway["elapsed_seconds"] = round(elapsed, 3)
            direct["elapsed_seconds"] = round(elapsed, 3)
            self._gateway_samples.append(gateway)
            self._direct_samples.append(direct)
            next_due += self.interval_seconds
            self._stop.wait(max(0.0, next_due - time.monotonic()))

    def failure_snapshot(self) -> dict[str, Any]:
        def summarize_channel(samples: list[dict[str, Any]]) -> dict[str, Any]:
            post_failure = samples
            if self._failure_elapsed_seconds is not None:
                post_failure = [
                    sample
                    for sample in samples
                    if float(sample.get("elapsed_seconds", -1.0))
                    >= self._failure_elapsed_seconds
                ]
            return {
                "sample_count": len(samples),
                "post_failure_sample_count": len(post_failure),
                "post_failure_successes": sum(
                    1 for sample in post_failure if sample.get("available") is True
                ),
                "post_failure_failures": sum(
                    1 for sample in post_failure if sample.get("available") is not True
                ),
                "last_samples": post_failure[-5:],
            }

        return {
            "failure_marker": {
                "label": self._failure_label,
                "elapsed_seconds": (
                    round(self._failure_elapsed_seconds, 3)
                    if self._failure_elapsed_seconds is not None
                    else None
                ),
            },
            "gateway": summarize_channel(list(self._gateway_samples)),
            "direct_api": summarize_channel(list(self._direct_samples)),
        }

    def stop(self, end_reason: str) -> dict[str, Any]:
        if self._thread is None:
            raise ref.ProofError("continuous availability monitor was not started")
        self._stop.set()
        self._thread.join(timeout=15)
        if self._thread.is_alive():
            raise ref.ProofError("continuous availability monitor did not stop")
        finished = time.monotonic()
        window = finished - self._started
        gateway = summarize_continuous_availability(
            self._gateway_samples,
            window_seconds=window,
            interval_seconds=self.interval_seconds,
        )
        direct_api = summarize_continuous_availability(
            self._direct_samples,
            window_seconds=window,
            interval_seconds=self.interval_seconds,
        )
        failure: dict[str, Any] | None = None
        if self._failure_elapsed_seconds is not None:
            gateway["failover_recovery"] = summarize_failover_recovery(
                self._gateway_samples,
                failure_elapsed_seconds=self._failure_elapsed_seconds,
                interval_seconds=self.interval_seconds,
            )
            direct_api["failover_recovery"] = summarize_failover_recovery(
                self._direct_samples,
                failure_elapsed_seconds=self._failure_elapsed_seconds,
                interval_seconds=self.interval_seconds,
            )
            failure = {
                "label": self._failure_label,
                "elapsed_seconds": round(self._failure_elapsed_seconds, 3),
            }
        return {
            "window": {
                "start": "first-acknowledged-domain-projection-before-change-management",
                "end": end_reason,
                "seconds": round(window, 3),
                "excludes": [
                    "initial-platform-bootstrap-before-service-readiness",
                    "intentional-primary-cluster-retirement-and-blank-PITR-restore",
                ],
            },
            "failure_marker": failure,
            "gateway": gateway,
            "direct_api": direct_api,
        }

def api_readiness_failure_diagnostic(
    kubectl: str,
    monitor: ContinuousAvailabilityMonitor,
) -> dict[str, Any]:
    def read_json(argv: list[str]) -> dict[str, Any]:
        try:
            return json.loads(ref.output(argv))
        except (
            OSError,
            subprocess.SubprocessError,
            json.JSONDecodeError,
            ref.ProofError,
        ) as error:
            return {
                "diagnostic_error": f"{type(error).__name__}: {error}"[:300]
            }

    deployment = read_json(
        [
            kubectl,
            "-n",
            "weltgewebe",
            "get",
            "deployment/weltgewebe-api",
            "-o",
            "json",
        ]
    )
    pods = read_json(
        [
            kubectl,
            "-n",
            "weltgewebe",
            "get",
            "pods",
            "-l",
            "app.kubernetes.io/name=weltgewebe-api",
            "-o",
            "json",
        ]
    )
    endpoint_slices = read_json(
        [
            kubectl,
            "-n",
            "weltgewebe",
            "get",
            "endpointslices.discovery.k8s.io",
            "-l",
            "kubernetes.io/service-name=weltgewebe-api",
            "-o",
            "json",
        ]
    )

    pod_states: list[dict[str, Any]] = []
    health_log_needles = (
        "health check failed",
        "essential domain event worker",
        "domain outbox",
        "domain receipt consumer",
    )
    for item in pods.get("items", []) if isinstance(pods.get("items"), list) else []:
        metadata = item.get("metadata", {})
        status = item.get("status", {})
        name = str(metadata.get("name", ""))
        conditions = {
            condition.get("type"): condition.get("status")
            for condition in status.get("conditions", [])
            if isinstance(condition, dict)
        }
        container_status = next(
            (
                entry
                for entry in status.get("containerStatuses", [])
                if isinstance(entry, dict) and entry.get("name") == "api"
            ),
            {},
        )
        log_excerpt: list[str] = []
        if name:
            try:
                result = subprocess.run(
                    [
                        kubectl,
                        "--request-timeout=5s",
                        "-n",
                        "weltgewebe",
                        "logs",
                        name,
                        "-c",
                        "api",
                        "--tail=200",
                    ],
                    cwd=ROOT,
                    text=True,
                    capture_output=True,
                    timeout=8,
                    check=False,
                )
                log_excerpt = [
                    line[:500]
                    for line in (result.stdout or "").splitlines()
                    if any(needle in line.lower() for needle in health_log_needles)
                ][-20:]
                if result.returncode != 0 and not log_excerpt:
                    log_excerpt = [
                        f"log_read_error: {(result.stderr or '').strip()[:300]}"
                    ]
            except (OSError, subprocess.SubprocessError) as error:
                log_excerpt = [
                    f"log_read_error: {type(error).__name__}: {str(error)[:250]}"
                ]
        pod_states.append(
            {
                "name": name,
                "node": item.get("spec", {}).get("nodeName"),
                "pod_ip": status.get("podIP"),
                "phase": status.get("phase"),
                "ready": conditions.get("Ready") == "True",
                "container_ready": container_status.get("ready"),
                "restart_count": container_status.get("restartCount"),
                "deleting": bool(metadata.get("deletionTimestamp")),
                "health_log_excerpt": log_excerpt,
            }
        )

    endpoint_states: list[dict[str, Any]] = []
    for item in (
        endpoint_slices.get("items", [])
        if isinstance(endpoint_slices.get("items"), list)
        else []
    ):
        for endpoint in item.get("endpoints", []):
            if not isinstance(endpoint, dict):
                continue
            endpoint_states.append(
                {
                    "addresses": endpoint.get("addresses", []),
                    "node": endpoint.get("nodeName"),
                    "target": endpoint.get("targetRef", {}).get("name"),
                    "ready": endpoint.get("conditions", {}).get("ready"),
                    "serving": endpoint.get("conditions", {}).get("serving"),
                    "terminating": endpoint.get("conditions", {}).get(
                        "terminating"
                    ),
                }
            )

    deployment_status = deployment.get("status", {})
    return {
        "capture": "post-timeout-after-measurement",
        "continuous_availability": monitor.failure_snapshot(),
        "deployment": {
            "replicas": deployment_status.get("replicas"),
            "ready_replicas": deployment_status.get("readyReplicas"),
            "available_replicas": deployment_status.get("availableReplicas"),
            "unavailable_replicas": deployment_status.get("unavailableReplicas", 0),
        },
        "pods": pod_states,
        "endpoint_slices": endpoint_states,
    }
