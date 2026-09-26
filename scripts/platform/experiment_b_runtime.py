#!/usr/bin/env python3
"""Bounded lifecycle driver for WELTGEWEBE-OS-V1-T085 Experiment B.

The driver owns only the temporary libvirt VM commonthing-experiment-b and
state below ~/.local/state/commonthing/experiment-b. It never targets production
DNS, production data, or a production Kubernetes context.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import math
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import bootstrap_tools
import experiment_b as contract

ROOT = Path(__file__).resolve().parents[2]
CLUSTER = ROOT / "platform/clusters/experiment-b"
NAMESPACES = CLUSTER / "namespaces"
DEFAULT_STATE_ROOT = Path.home() / ".local/state/commonthing/experiment-b"
VM_NAME = "commonthing-experiment-b"
LIBVIRT_URI = "qemu:///system"
POOL_NAME = "commonthing-experiment-b-pool"
POOL_TARGET = Path("/var/tmp/commonthing-experiment-b-libvirt")
BASE_VOLUME = "commonthing-experiment-b-base.qcow2"
VOLUME_NAME = "commonthing-experiment-b.qcow2"
APP_NAMESPACE = "commonthing-experiment-b"
DATA_NAMESPACE = "commonthing-data"
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
PERFORMANCE_POLICY = ROOT / "policies/performance.v1.json"
DOMAIN_SCALE = ROOT / "scripts/performance/domain_scale.py"
DOMAIN_SCALE_CONFIG = ROOT / "configs/performance/domain-scale.v1.json"
K6_WORKFLOW = ROOT / ".github/workflows/domain-scale.yml"
K6_WORKLOAD = ROOT / "scripts/performance/api_runtime_k6.js"
RETIREMENT_RECEIPT = Path.home() / ".local/state/commonthing/experiment-b-retirement.json"


class RuntimeErrorEB(RuntimeError):
    pass


def run(
    argv: list[str],
    *,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
    capture: bool = True,
    check: bool = True,
    timeout: int = 900,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        argv,
        cwd=ROOT,
        input=input_text,
        env=env,
        text=True,
        capture_output=capture,
        timeout=timeout,
        check=False,
    )
    if check and result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RuntimeErrorEB(
            f"command failed ({result.returncode}): {argv[0]}: {stderr[-2000:]}"
        )
    return result


def require_binary(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeErrorEB(f"required host command is missing: {name}")
    return path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def state_root(value: str | None) -> Path:
    root = (Path(value).expanduser() if value else DEFAULT_STATE_ROOT).resolve()
    allowed_root = DEFAULT_STATE_ROOT.resolve()
    if root != allowed_root:
        try:
            root.relative_to(allowed_root)
        except ValueError as exc:
            raise RuntimeErrorEB(
                f"state root must be {allowed_root} or one of its descendants"
            ) from exc
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    return root


def atomic_json(path: Path, payload: dict[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
        mode="w",
        encoding="utf-8",
    ) as handle:
        tmp = Path(handle.name)
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _begin_live_check_attempt(
    root: Path,
    receipt_stem: str,
    source_commit: str,
) -> tuple[Path, Path, int]:
    if not COMMIT_RE.fullmatch(source_commit):
        raise RuntimeErrorEB(f"{receipt_stem} attempt source commit is not exact")
    receipt_path = root / "receipts" / f"{receipt_stem}.json"
    attempt_path = root / "receipts" / f"{receipt_stem}-attempt.json"
    started_at_unix_ms = time.time_ns() // 1_000_000
    atomic_json(
        attempt_path,
        {
            "schema_version": 1,
            "status": "running",
            "source_commit": source_commit,
            "receipt": receipt_path.name,
            "started_at_unix_ms": started_at_unix_ms,
        },
    )
    receipt_path.unlink(missing_ok=True)
    return receipt_path, attempt_path, started_at_unix_ms


def _complete_live_check_attempt(
    attempt_path: Path,
    receipt_path: Path,
    source_commit: str,
    started_at_unix_ms: int,
    status: str,
) -> None:
    atomic_json(
        attempt_path,
        {
            "schema_version": 1,
            "status": status,
            "source_commit": source_commit,
            "receipt": receipt_path.name,
            "started_at_unix_ms": started_at_unix_ms,
            "finished_at_unix_ms": time.time_ns() // 1_000_000,
            "receipt_sha256": sha256_file(receipt_path),
        },
    )


RELEASE_DEPENDENT_RECEIPTS = (
    "t048-fixture.json",
    "semantic-search.json",
    "semantic-search-attempt.json",
    "functional-readback.json",
    "functional-readback-attempt.json",
    "t048-load.json",
    "t048-load-attempt.json",
    "recovery.json",
    "recovery-failed.json",
    "status.json",
    "status-attempt.json",
    "portability.json",
)


def _begin_release_attempt(
    root: Path,
    source_commit: str,
) -> tuple[Path, Path, int]:
    receipt_path, attempt_path, started_at_unix_ms = _begin_live_check_attempt(
        root, "release", source_commit
    )
    for name in RELEASE_DEPENDENT_RECEIPTS:
        (root / "receipts" / name).unlink(missing_ok=True)
    return receipt_path, attempt_path, started_at_unix_ms


def download(url: str, expected_sha256: str, destination: Path) -> None:
    if destination.is_file() and sha256_file(destination) == expected_sha256:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as handle:
        tmp = Path(handle.name)
    try:
        request = urllib.request.Request(
            url, headers={"User-Agent": "commonthing-experiment-b/1"}
        )
        with urllib.request.urlopen(request, timeout=90) as response, tmp.open("wb") as out:
            shutil.copyfileobj(response, out)
        observed = sha256_file(tmp)
        if observed != expected_sha256:
            raise RuntimeErrorEB(
                f"download digest mismatch: expected {expected_sha256}, got {observed}"
            )
        os.replace(tmp, destination)
    finally:
        tmp.unlink(missing_ok=True)


def load_config() -> dict[str, Any]:
    config = contract.load_config()
    contract.validate_config(config)
    if config["vm"]["name"] != VM_NAME:
        raise RuntimeErrorEB("unexpected VM name")
    return config


def git_head() -> str:
    return run(["git", "rev-parse", "HEAD"]).stdout.strip()


def remote_main() -> str:
    lines = run(["git", "ls-remote", "origin", "refs/heads/main"]).stdout.splitlines()
    if len(lines) != 1:
        raise RuntimeErrorEB("could not resolve exactly one origin/main")
    return lines[0].split()[0]


def preflight(expected_source_commit: str | None = None) -> dict[str, Any]:
    config = load_config()
    for command in (
        "virsh", "virt-install", "qemu-img", "ssh", "scp", "ssh-keygen", "docker"
    ):
        require_binary(command)
    if not Path("/dev/kvm").exists():
        raise RuntimeErrorEB("/dev/kvm is unavailable")
    if expected_source_commit is not None:
        if not COMMIT_RE.fullmatch(expected_source_commit):
            raise RuntimeErrorEB("expected source commit must be exact lowercase SHA-1")
        if git_head() != expected_source_commit:
            raise RuntimeErrorEB("checkout HEAD differs from expected source commit")
        if remote_main() != expected_source_commit:
            raise RuntimeErrorEB("expected source commit is no longer origin/main")
        if run(["git", "status", "--porcelain"]).stdout.strip():
            raise RuntimeErrorEB("expected-source checkout must be clean before runtime effects")

    network = run(
        ["virsh", "-c", LIBVIRT_URI, "net-info", config["vm"]["network"]]
    ).stdout
    if "Active:" not in network or "yes" not in network:
        raise RuntimeErrorEB("libvirt default network is not active")

    domain = run(
        ["virsh", "-c", LIBVIRT_URI, "dominfo", VM_NAME],
        check=False,
    )
    return {
        "status": "ok",
        "source_commit": git_head(),
        "remote_main": remote_main(),
        "domain_present": domain.returncode == 0,
        "vm": {
            "name": VM_NAME,
            "vcpu": config["vm"]["vcpu"],
            "memory_mib": config["vm"]["memory_mib"],
            "disk_gib": config["vm"]["disk_gib"],
            "network": config["vm"]["network"],
        },
    }


def ensure_ssh_key(root: Path) -> tuple[Path, Path]:
    private = root / "ssh/id_ed25519"
    public = root / "ssh/id_ed25519.pub"
    if private.is_file() and public.is_file():
        os.chmod(private, 0o600)
        return private, public
    private.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            "ssh-keygen", "-q", "-t", "ed25519", "-N", "",
            "-C", "commonthing-experiment-b", "-f", str(private),
        ]
    )
    os.chmod(private, 0o600)
    return private, public


def prepare(root: Path) -> dict[str, Any]:
    config = load_config()
    _private_key, public_key = ensure_ssh_key(root)
    image = config["vm"]["image"]
    cloud_image = root / "downloads" / Path(image["url"]).name
    download(image["url"], image["sha256"], cloud_image)

    k3s = config["kubernetes"]
    k3s_binary = root / "downloads/k3s"
    download(k3s["binary_url"], k3s["binary_sha256"], k3s_binary)
    os.chmod(k3s_binary, 0o755)

    cloud_dir = root / "cloud-init"
    cloud = contract.render_cloud_init(public_key, cloud_dir, VM_NAME)
    image_info = json.loads(
        run(["qemu-img", "info", "--output=json", str(cloud_image)]).stdout
    )
    source_virtual_size = int(image_info.get("virtual-size", 0))
    if source_virtual_size <= 0:
        raise RuntimeErrorEB("cloud image virtual size is invalid")

    receipt = {
        "schema_version": 1,
        "status": "prepared",
        "config_sha256": sha256_file(CLUSTER / "config.json"),
        "cloud_image": str(cloud_image),
        "cloud_image_sha256": sha256_file(cloud_image),
        "cloud_image_virtual_size": source_virtual_size,
        "k3s_binary_sha256": sha256_file(k3s_binary),
        "ssh_public_key_sha256": sha256_file(public_key),
        "cloud_init": cloud,
    }
    atomic_json(root / "receipts/prepare.json", receipt)
    return receipt


def create_vm(root: Path) -> dict[str, Any]:
    config = load_config()
    if run(
        ["virsh", "-c", LIBVIRT_URI, "dominfo", VM_NAME],
        check=False,
    ).returncode == 0:
        raise RuntimeErrorEB(
            "Experiment-B VM already exists; refusing implicit replacement"
        )
    if run(
        ["virsh", "-c", LIBVIRT_URI, "pool-info", POOL_NAME],
        check=False,
    ).returncode == 0:
        raise RuntimeErrorEB(
            "Experiment-B libvirt pool already exists; run bounded teardown first"
        )

    prepared = prepare(root)
    cloud_image = Path(prepared["cloud_image"])
    source_virtual_size = int(prepared["cloud_image_virtual_size"])
    if POOL_TARGET.exists() and any(POOL_TARGET.iterdir()):
        raise RuntimeErrorEB("Experiment-B libvirt pool target already contains files")
    POOL_TARGET.mkdir(parents=True, exist_ok=True)

    pool_defined = False
    try:
        run(
            [
                "virsh", "-c", LIBVIRT_URI, "pool-define-as",
                POOL_NAME, "dir", "--target", str(POOL_TARGET),
            ]
        )
        pool_defined = True
        run(["virsh", "-c", LIBVIRT_URI, "pool-build", POOL_NAME])
        run(["virsh", "-c", LIBVIRT_URI, "pool-start", POOL_NAME])
        run(
            [
                "virsh", "-c", LIBVIRT_URI, "vol-create-as",
                POOL_NAME, BASE_VOLUME, f"{source_virtual_size}B",
                "--format", "qcow2",
            ]
        )
        run(
            [
                "virsh", "-c", LIBVIRT_URI, "vol-upload",
                BASE_VOLUME, str(cloud_image), "--pool", POOL_NAME, "--sparse",
            ],
            timeout=300,
        )
        run(["virsh", "-c", LIBVIRT_URI, "pool-refresh", POOL_NAME])
        run(
            [
                "virsh", "-c", LIBVIRT_URI, "vol-create-as",
                POOL_NAME, VOLUME_NAME, f"{config['vm']['disk_gib']}G",
                "--format", "qcow2",
                "--backing-vol", BASE_VOLUME,
                "--backing-vol-format", "qcow2",
            ]
        )
        run(
            [
                "virt-install",
                "--connect", LIBVIRT_URI,
                "--name", VM_NAME,
                "--memory", str(config["vm"]["memory_mib"]),
                "--vcpus", str(config["vm"]["vcpu"]),
                "--import",
                "--disk", f"vol={POOL_NAME}/{VOLUME_NAME},bus=virtio",
                "--network", f"network={config['vm']['network']},model=virtio",
                "--graphics", "none",
                "--noautoconsole",
                "--os-variant", config["vm"]["os_variant"],
                "--cloud-init",
                (
                    f"user-data={root / 'cloud-init/user-data.yaml'},"
                    f"meta-data={root / 'cloud-init/meta-data.yaml'}"
                ),
            ],
            timeout=120,
        )
    except Exception:
        if run(
            ["virsh", "-c", LIBVIRT_URI, "dominfo", VM_NAME],
            check=False,
        ).returncode == 0:
            run(["virsh", "-c", LIBVIRT_URI, "destroy", VM_NAME], check=False)
            undefine = run(
                ["virsh", "-c", LIBVIRT_URI, "undefine", VM_NAME, "--nvram"],
                check=False,
            )
            if undefine.returncode != 0:
                run(["virsh", "-c", LIBVIRT_URI, "undefine", VM_NAME], check=False)
        if pool_defined:
            run(
                ["virsh", "-c", LIBVIRT_URI, "vol-delete", VOLUME_NAME, "--pool", POOL_NAME],
                check=False,
            )
            run(
                ["virsh", "-c", LIBVIRT_URI, "vol-delete", BASE_VOLUME, "--pool", POOL_NAME],
                check=False,
            )
            run(["virsh", "-c", LIBVIRT_URI, "pool-destroy", POOL_NAME], check=False)
            run(["virsh", "-c", LIBVIRT_URI, "pool-delete", POOL_NAME], check=False)
            run(["virsh", "-c", LIBVIRT_URI, "pool-undefine", POOL_NAME], check=False)
        if POOL_TARGET.exists() and not any(POOL_TARGET.iterdir()):
            POOL_TARGET.rmdir()
        raise

    receipt = {
        "schema_version": 1,
        "status": "created",
        "vm": VM_NAME,
        "pool": POOL_NAME,
        "volume": VOLUME_NAME,
        "network": config["vm"]["network"],
    }
    atomic_json(root / "receipts/vm-create.json", receipt)
    return receipt


def vm_ip() -> str:
    for _ in range(120):
        result = run(
            ["virsh", "-c", LIBVIRT_URI, "domifaddr", VM_NAME, "--source", "lease"],
            check=False,
        )
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) >= 4 and "/" in fields[-1]:
                address = fields[-1].split("/", 1)[0]
                try:
                    parsed = ipaddress.ip_address(address)
                except ValueError:
                    continue
                if parsed.version == 4 and parsed.is_private:
                    return address
        time.sleep(2)
    raise RuntimeErrorEB("VM did not receive a libvirt NAT IPv4 lease")


def ssh_argv(root: Path, ip: str) -> list[str]:
    known_hosts = root / "ssh/known_hosts"
    return [
        "ssh",
        "-i", str(root / "ssh/id_ed25519"),
        "-o", f"UserKnownHostsFile={known_hosts}",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=5",
        f"commonthing@{ip}",
    ]


def wait_ssh(root: Path, ip: str) -> None:
    for _ in range(90):
        result = run([*ssh_argv(root, ip), "true"], check=False)
        if result.returncode == 0:
            return
        time.sleep(2)
    raise RuntimeErrorEB("SSH did not become ready")


def scp_to(root: Path, ip: str, source: Path, destination: str) -> None:
    known_hosts = root / "ssh/known_hosts"
    run(
        [
            "scp",
            "-i", str(root / "ssh/id_ed25519"),
            "-o", f"UserKnownHostsFile={known_hosts}",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "BatchMode=yes",
            str(source),
            f"commonthing@{ip}:{destination}",
        ]
    )


def install_k3s(root: Path) -> dict[str, Any]:
    config = load_config()
    ip = vm_ip()
    wait_ssh(root, ip)
    scp_to(root, ip, root / "downloads/k3s", "/tmp/k3s")
    scp_to(root, ip, CLUSTER / "k3s-config.yaml", "/tmp/config.yaml")
    scp_to(root, ip, CLUSTER / "k3s.service", "/tmp/k3s.service")
    command = (
        "sudo install -m 0755 /tmp/k3s /usr/local/bin/k3s && "
        "sudo install -d -m 0755 /etc/rancher/k3s && "
        "sudo install -m 0600 /tmp/config.yaml /etc/rancher/k3s/config.yaml && "
        "sudo install -m 0644 /tmp/k3s.service /etc/systemd/system/k3s.service && "
        "sudo systemctl daemon-reload && "
        "sudo systemctl enable --now k3s"
    )
    run([*ssh_argv(root, ip), command], timeout=180)
    for _ in range(90):
        result = run(
            [*ssh_argv(root, ip), "sudo /usr/local/bin/k3s kubectl get node -o name"],
            check=False,
        )
        if result.returncode == 0 and "node/" in result.stdout:
            break
        time.sleep(2)
    else:
        raise RuntimeErrorEB("k3s node did not become queryable")

    kubeconfig_raw = run(
        [*ssh_argv(root, ip), "sudo cat /etc/rancher/k3s/k3s.yaml"]
    ).stdout
    kubeconfig = kubeconfig_raw.replace(
        "https://127.0.0.1:6443", f"https://{ip}:6443"
    )
    kubeconfig_path = root / "kubeconfig.yaml"
    kubeconfig_path.write_text(kubeconfig, encoding="utf-8")
    os.chmod(kubeconfig_path, 0o600)

    version = run(
        [*ssh_argv(root, ip), "/usr/local/bin/k3s --version"]
    ).stdout.splitlines()[0]
    if config["kubernetes"]["version"] not in version:
        raise RuntimeErrorEB(f"k3s version mismatch: {version}")
    receipt = {
        "schema_version": 1,
        "status": "ready",
        "vm_ip": ip,
        "k3s_version": version,
        "kubeconfig_sha256": sha256_file(kubeconfig_path),
    }
    atomic_json(root / "receipts/k3s.json", receipt)
    return receipt


def toolchain(root: Path) -> dict[str, Any]:
    return bootstrap_tools.install(
        root / "toolchain",
        tool_names=["kubectl", "kustomize", "flux", "helm"],
        include_artifacts=True,
    )


def kube_env(root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["KUBECONFIG"] = str(root / "kubeconfig.yaml")
    return env


def install_platform(root: Path) -> dict[str, Any]:
    receipt = toolchain(root)
    tools = receipt["tools"]
    artifacts = receipt["artifacts"]
    env = kube_env(root)
    kubectl = tools["kubectl"]
    helm = tools["helm"]
    flux = tools["flux"]

    for name in (
        "gateway_api_gatewayclasses",
        "gateway_api_gateways",
        "gateway_api_httproutes",
        "gateway_api_referencegrants",
        "gateway_api_grpcroutes",
    ):
        run([kubectl, "apply", "-f", artifacts[name]], env=env)

    ip = vm_ip()
    run(
        [
            helm, "upgrade", "--install", "cilium", artifacts["cilium_chart"],
            "--namespace", "kube-system",
            "--set", "gatewayAPI.enabled=true",
            "--set", "nodeIPAM.enabled=true",
            "--set", "defaultLBServiceIPAM=nodeipam",
            "--set", "kubeProxyReplacement=true",
            "--set", f"k8sServiceHost={ip}",
            "--set", "k8sServicePort=6443",
            "--set", "ipam.operator.clusterPoolIPv4PodCIDRList={10.42.0.0/16}",
            "--set", "hubble.relay.enabled=true",
            "--set", "hubble.ui.enabled=false",
            "--set", "operator.replicas=1",
            "--wait", "--timeout", "10m",
        ],
        env=env,
        timeout=900,
    )
    run(
        [
            flux, "install",
            "--namespace=flux-system",
            "--components=source-controller,kustomize-controller,helm-controller,notification-controller",
        ],
        env=env,
        timeout=600,
    )
    result = {
        "schema_version": 1,
        "status": "ready",
        "toolchain_lock_sha256": receipt["lock_sha256"],
        "vm_ip": ip,
    }
    atomic_json(root / "receipts/platform.json", result)
    return result


def render_namespaces(root: Path) -> str:
    receipt = toolchain(root)
    kustomize = receipt["tools"]["kustomize"]
    return run([kustomize, "build", str(NAMESPACES)]).stdout


def kubectl_apply(root: Path, manifest: str) -> None:
    tools = toolchain(root)["tools"]
    run(
        [tools["kubectl"], "apply", "-f", "-"],
        input_text=manifest,
        env=kube_env(root),
    )


def secret_manifest(
    namespace: str,
    name: str,
    literals: dict[str, str],
    *,
    secret_type: str | None = None,
    binary_data: dict[str, bytes] | None = None,
) -> str:
    data = {
        key: base64.b64encode(value.encode("utf-8")).decode("ascii")
        for key, value in literals.items()
    }
    for key, value in (binary_data or {}).items():
        data[key] = base64.b64encode(value).decode("ascii")
    payload: dict[str, Any] = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": namespace},
        "type": secret_type or "Opaque",
        "data": data,
    }
    return json.dumps(payload, sort_keys=True)


def ensure_secret_material(root: Path) -> dict[str, str]:
    path = root / "secrets/database.json"
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in data.items()}
    data = {
        "username": "commonthing",
        "database": "commonthing",
        "password": secrets.token_hex(32),
    }
    atomic_json(path, data)
    return data


def inject_secrets(root: Path, registry_config: Path) -> dict[str, Any]:
    if not registry_config.is_file() or registry_config.is_symlink():
        raise RuntimeErrorEB("registry config must be a regular external file")
    try:
        registry_payload = json.loads(registry_config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeErrorEB("registry config is not valid JSON") from exc
    if "ghcr.io" not in registry_payload.get("auths", {}):
        raise RuntimeErrorEB("registry config has no ghcr.io credential")
    kubectl_apply(root, render_namespaces(root))
    db = ensure_secret_material(root)
    database_url = (
        f"postgresql://{db['username']}:{db['password']}"
        f"@postgres.{DATA_NAMESPACE}.svc.cluster.local:5432/{db['database']}"
    )
    kubectl_apply(
        root,
        secret_manifest(
            DATA_NAMESPACE,
            "commonthing-experiment-b-database",
            db,
        ),
    )
    kubectl_apply(
        root,
        secret_manifest(
            APP_NAMESPACE,
            "weltgewebe-runtime",
            {"database-url": database_url},
        ),
    )
    kubectl_apply(
        root,
        secret_manifest(
            APP_NAMESPACE,
            "commonthing-experiment-b-registry",
            {},
            secret_type="kubernetes.io/dockerconfigjson",
            binary_data={".dockerconfigjson": registry_config.read_bytes()},
        ),
    )
    receipt = {
        "schema_version": 1,
        "status": "ready",
        "database_secret": "commonthing-experiment-b-database",
        "runtime_secret": "weltgewebe-runtime",
        "registry_secret": "commonthing-experiment-b-registry",
        "registry_source_sha256": sha256_file(registry_config),
        "secret_values_recorded": False,
    }
    atomic_json(root / "receipts/secrets.json", receipt)
    return receipt


def apply_release(
    root: Path,
    source_commit: str,
    api_digest: str,
    web_digest: str,
) -> dict[str, Any]:
    if not COMMIT_RE.fullmatch(source_commit):
        raise RuntimeErrorEB("source commit is not exact")
    if not DIGEST_RE.fullmatch(api_digest) or not DIGEST_RE.fullmatch(web_digest):
        raise RuntimeErrorEB("image digests must be exact sha256 values")
    if git_head() != source_commit or remote_main() != source_commit:
        raise RuntimeErrorEB("release source is not current protected main")
    output = root / "bootstrap.yaml"
    binding = contract.render_bootstrap(
        source_commit, api_digest, web_digest, output
    )
    receipt_path, attempt_path, attempt_started_at_unix_ms = (
        _begin_release_attempt(root, source_commit)
    )
    kubectl_apply(root, output.read_text(encoding="utf-8"))
    flux = toolchain(root)["tools"]["flux"]
    env = kube_env(root)
    for _ in range(120):
        result = run(
            [flux, "get", "kustomizations", "-A"],
            env=env,
            check=False,
        )
        text = result.stdout
        if (
            result.returncode == 0
            and "commonthing-experiment-b-gateway" in text
            and text.count("True") >= 5
        ):
            break
        time.sleep(5)
    else:
        raise RuntimeErrorEB("Flux Experiment-B kustomizations did not converge")
    receipt = {
        "schema_version": 1,
        "status": "applied",
        **binding,
    }
    atomic_json(receipt_path, receipt)
    _complete_live_check_attempt(
        attempt_path,
        receipt_path,
        source_commit,
        attempt_started_at_unix_ms,
        "pass",
    )
    return receipt


def semantic_activate(root: Path) -> dict[str, Any]:
    config = load_config()
    semantic = config["semantic_search"]
    release_path = root / "receipts/release.json"
    if not release_path.is_file():
        raise RuntimeErrorEB("semantic provider proof requires an applied release receipt")
    release = json.loads(release_path.read_text(encoding="utf-8"))
    source_commit = str(release.get("source_commit", ""))
    if (
        not COMMIT_RE.fullmatch(source_commit)
        or git_head() != source_commit
        or remote_main() != source_commit
    ):
        raise RuntimeErrorEB("semantic provider proof is not bound to current protected main")

    receipt_path, attempt_path, attempt_started_at_unix_ms = (
        _begin_live_check_attempt(root, "semantic-search", source_commit)
    )
    kubectl = toolchain(root)["tools"]["kubectl"]
    env = kube_env(root)
    egress_name = "commonthing-experiment-b-model-bootstrap-egress"
    temporary_egress = json.dumps(
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": egress_name, "namespace": APP_NAMESPACE},
            "spec": {
                "podSelector": {
                    "matchLabels": {"app.kubernetes.io/name": "weltgewebe-api"}
                },
                "policyTypes": ["Egress"],
                "egress": [
                    {
                        "to": [{"ipBlock": {"cidr": "0.0.0.0/0"}}],
                        "ports": [{"protocol": "TCP", "port": 443}],
                    }
                ],
            },
        },
        sort_keys=True,
    )
    kubectl_apply(root, temporary_egress)
    try:
        run(
            [
                kubectl, "-n", APP_NAMESPACE,
                "exec", "deployment/weltgewebe-api",
                "-c", "ollama", "--", "ollama", "pull", semantic["model_id"],
            ],
            env=env,
            timeout=1800,
        )
        tags = run(
            [
                kubectl, "-n", APP_NAMESPACE,
                "exec", "deployment/weltgewebe-api",
                "-c", "search-worker", "--",
                "wget", "-qO-", "http://127.0.0.1:11434/api/tags",
            ],
            env=env,
        ).stdout
        payload = json.loads(tags)
        observed = ""
        for model in payload.get("models", []):
            if model.get("name") == semantic["model_id"]:
                observed = str(model.get("digest", ""))
                break
        expected = str(semantic["model_revision"]).removeprefix("sha256:")
        if observed.removeprefix("sha256:") != expected:
            raise RuntimeErrorEB(
                "Ollama model digest differs from the pinned revision"
            )

        probe_text = "commonThing Experiment B semantic continuity"
        probe_request = json.dumps(
            {"model": semantic["model_id"], "input": probe_text},
            separators=(",", ":"),
        )
        embedding_raw = run(
            [
                kubectl, "-n", APP_NAMESPACE,
                "exec", "deployment/weltgewebe-api",
                "-c", "search-worker", "--",
                "wget", "-qO-",
                "--header=Content-Type: application/json",
                f"--post-data={probe_request}",
                "http://127.0.0.1:11434/api/embed",
            ],
            env=env,
            timeout=300,
        ).stdout
        embedding_payload = json.loads(embedding_raw)
        embeddings = embedding_payload.get("embeddings")
        dimension = int(semantic["dimension"])
        if (
            not isinstance(embeddings, list)
            or len(embeddings) != 1
            or not isinstance(embeddings[0], list)
            or len(embeddings[0]) != dimension
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                for value in embeddings[0]
            )
        ):
            raise RuntimeErrorEB(
                "Ollama embedding smoke does not match the pinned finite dimension"
            )
    finally:
        _kubectl(
            root,
            [
                "-n", APP_NAMESPACE, "delete", "networkpolicy",
                egress_name, "--ignore-not-found=true",
            ],
        )
    remaining_egress = _kubectl(
        root,
        [
            "-n", APP_NAMESPACE, "get", "networkpolicy",
            egress_name, "--ignore-not-found=true", "-o", "name",
        ],
    )
    if remaining_egress.stdout.strip():
        raise RuntimeErrorEB("temporary model-bootstrap egress policy still exists")

    receipt = {
        "schema_version": 1,
        "status": "pass",
        "source_commit": source_commit,
        "provider": semantic["provider"],
        "model_id": semantic["model_id"],
        "model_revision": semantic["model_revision"],
        "runtime_identity": semantic["runtime_identity"],
        "dimension": semantic["dimension"],
        "embedding_probe": True,
        "embedding_probe_sha256": hashlib.sha256(probe_text.encode("utf-8")).hexdigest(),
        "literal_loopback": True,
        "temporary_model_egress_removed": True,
        "database_generation_activation": False,
    }
    atomic_json(receipt_path, receipt)
    _complete_live_check_attempt(
        attempt_path,
        receipt_path,
        source_commit,
        attempt_started_at_unix_ms,
        "pass",
    )
    return receipt



EXPECTED_FLUX_KUSTOMIZATIONS = frozenset(
    {
        "commonthing-experiment-b-namespaces",
        "commonthing-experiment-b-data",
        "commonthing-experiment-b-migration",
        "commonthing-experiment-b-app",
        "commonthing-experiment-b-gateway",
    }
)


def _require_exact_flux_kustomizations(flux_readback: dict[str, Any]) -> None:
    observed = set(flux_readback)
    if observed == EXPECTED_FLUX_KUSTOMIZATIONS:
        return
    missing = sorted(EXPECTED_FLUX_KUSTOMIZATIONS - observed)
    unexpected = sorted(observed - EXPECTED_FLUX_KUSTOMIZATIONS)
    raise RuntimeErrorEB(
        "Experiment-B Flux Kustomization set mismatch: "
        f"missing={missing}; unexpected={unexpected}"
    )


def _deployment_availability_snapshot(
    deployment: dict[str, Any], name: str
) -> dict[str, Any]:
    metadata = deployment.get("metadata", {})
    spec = deployment.get("spec", {})
    status_obj = deployment.get("status", {})
    generation = int(metadata.get("generation") or 0)
    desired = int(spec.get("replicas") or 0)
    observed_generation = int(status_obj.get("observedGeneration") or 0)
    updated = int(status_obj.get("updatedReplicas") or 0)
    ready = int(status_obj.get("readyReplicas") or 0)
    available = int(status_obj.get("availableReplicas") or 0)
    available_condition = any(
        condition.get("type") == "Available" and condition.get("status") == "True"
        for condition in status_obj.get("conditions", [])
        if isinstance(condition, dict)
    )
    if (
        generation < 1
        or desired < 1
        or observed_generation < generation
        or updated < desired
        or ready < desired
        or available < desired
        or not available_condition
    ):
        raise RuntimeErrorEB(
            f"Experiment-B deployment is not currently available: {name}"
        )
    return {
        "generation": generation,
        "observed_generation": observed_generation,
        "desired_replicas": desired,
        "updated_replicas": updated,
        "ready_replicas": ready,
        "available_replicas": available,
        "available": True,
    }


def status(root: Path) -> dict[str, Any]:
    config = load_config()
    tools = toolchain(root)["tools"]
    env = kube_env(root)
    kubectl = tools["kubectl"]

    release_path = root / "receipts/release.json"
    if not release_path.is_file():
        raise RuntimeErrorEB("Experiment-B status requires release receipt")
    release = json.loads(release_path.read_text(encoding="utf-8"))
    source_commit = str(release.get("source_commit", ""))
    receipt_path, attempt_path, attempt_started_at_unix_ms = (
        _begin_live_check_attempt(root, "status", source_commit)
    )

    nodes = json.loads(run([kubectl, "get", "nodes", "-o", "json"], env=env).stdout)
    if len(nodes.get("items", [])) != 1:
        raise RuntimeErrorEB("Experiment B expects exactly one k3s VM node")
    node = nodes["items"][0]
    info = node.get("status", {}).get("nodeInfo", {})
    kubelet = str(info.get("kubeletVersion", ""))
    os_image = str(info.get("osImage", ""))
    if "k3s" not in kubelet:
        raise RuntimeErrorEB("node is not a k3s runtime")
    if "kind" in json.dumps(node).lower():
        raise RuntimeErrorEB("kind marker found in Experiment-B node identity")

    expected_api = f"ghcr.io/heimgewebe/commonthing-api@{release.get('api_digest', '')}"
    expected_web = f"ghcr.io/heimgewebe/commonthing-web@{release.get('web_digest', '')}"

    source = _kubectl_json(
        root,
        ["-n", "flux-system", "get", "gitrepository", "commonthing-experiment-b"],
    )
    source_revision = str(
        source.get("status", {}).get("artifact", {}).get("revision", "")
    )
    if source_commit not in source_revision:
        raise RuntimeErrorEB("Flux GitRepository is not bound to the release commit")

    flux_items = _kubectl_json(
        root, ["-n", "flux-system", "get", "kustomizations"]
    ).get("items", [])
    flux_readback: dict[str, Any] = {}
    for item in flux_items:
        name = str(item.get("metadata", {}).get("name", ""))
        if not name.startswith("commonthing-experiment-b-"):
            continue
        status_obj = item.get("status", {})
        conditions = status_obj.get("conditions", [])
        ready = any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in conditions
            if isinstance(condition, dict)
        )
        revision = str(status_obj.get("lastAppliedRevision", ""))
        if not ready or source_commit not in revision:
            raise RuntimeErrorEB(f"Flux Kustomization is not exact-revision Ready: {name}")
        flux_readback[name] = {"ready": True, "revision": revision}

    _require_exact_flux_kustomizations(flux_readback)

    api = _kubectl_json(
        root, ["-n", APP_NAMESPACE, "get", "deployment", "weltgewebe-api"]
    )
    web = _kubectl_json(
        root, ["-n", APP_NAMESPACE, "get", "deployment", "weltgewebe-web"]
    )
    deployment_readback = {
        "weltgewebe-api": _deployment_availability_snapshot(
            api, "weltgewebe-api"
        ),
        "weltgewebe-web": _deployment_availability_snapshot(
            web, "weltgewebe-web"
        ),
    }
    api_containers = {
        item.get("name"): item.get("image")
        for item in api.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        if isinstance(item, dict)
    }
    web_containers = {
        item.get("name"): item.get("image")
        for item in web.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        if isinstance(item, dict)
    }
    if api_containers.get("api") != expected_api:
        raise RuntimeErrorEB("live API image does not match immutable release digest")
    if web_containers.get("web") != expected_web:
        raise RuntimeErrorEB("live Web image does not match immutable release digest")
    semantic = config["semantic_search"]
    if api_containers.get("ollama") != semantic["ollama_image"]:
        raise RuntimeErrorEB("live Ollama image does not match semantic-search pin")
    if api_containers.get("search-worker") != expected_api:
        raise RuntimeErrorEB("live search-worker image does not match API release digest")

    pvc_items = _kubectl_json(root, ["-A", "get", "pvc"]).get("items", [])
    pvc_readback: dict[str, Any] = {}
    for item in pvc_items:
        namespace = str(item.get("metadata", {}).get("namespace", ""))
        name = str(item.get("metadata", {}).get("name", ""))
        if namespace not in {APP_NAMESPACE, DATA_NAMESPACE}:
            continue
        phase = str(item.get("status", {}).get("phase", ""))
        storage_class = str(item.get("spec", {}).get("storageClassName", ""))
        if phase != "Bound" or storage_class != "local-path":
            raise RuntimeErrorEB(f"Experiment-B PVC is not Bound/local-path: {namespace}/{name}")
        pvc_readback[f"{namespace}/{name}"] = {
            "phase": phase,
            "storage_class": storage_class,
        }

    gateway = _kubectl_json(
        root, ["-n", APP_NAMESPACE, "get", "gateway", "commonthing-experiment-b"]
    )
    gateway_ready = any(
        condition.get("type") == "Programmed" and condition.get("status") == "True"
        for condition in gateway.get("status", {}).get("conditions", [])
        if isinstance(condition, dict)
    )
    if not gateway_ready:
        raise RuntimeErrorEB("Cilium Gateway is not Programmed")

    result = {
        "schema_version": 1,
        "status": "observed",
        "source_commit": source_commit,
        "vm_ip": vm_ip(),
        "node": node["metadata"]["name"],
        "kubelet_version": kubelet,
        "os_image": os_image,
        "flux_source_revision": source_revision,
        "flux": flux_readback,
        "deployments": deployment_readback,
        "images": {
            "api": api_containers.get("api"),
            "web": web_containers.get("web"),
            "ollama": api_containers.get("ollama"),
            "search_worker": api_containers.get("search-worker"),
        },
        "pvcs": pvc_readback,
        "gateway_programmed": True,
        "kind_runtime": False,
        "staging_cell_runtime_controller": False,
    }
    atomic_json(receipt_path, result)
    _complete_live_check_attempt(
        attempt_path,
        receipt_path,
        source_commit,
        attempt_started_at_unix_ms,
        "pass",
    )
    return result


def _libvirt_resource_present(kind: str, name: str) -> bool:
    if kind == "domain":
        argv = ["virsh", "-c", LIBVIRT_URI, "list", "--all", "--name"]
    elif kind == "pool":
        argv = ["virsh", "-c", LIBVIRT_URI, "pool-list", "--all", "--name"]
    else:
        raise RuntimeErrorEB(f"unsupported libvirt resource kind: {kind}")
    result = run(argv, check=False)
    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        raise RuntimeErrorEB(
            f"cannot prove libvirt {kind} state for {name}: {detail[-1000:]}"
        )
    return name in {line.strip() for line in result.stdout.splitlines() if line.strip()}


def teardown(root: Path) -> dict[str, Any]:
    evidence_hashes: dict[str, str] = {}
    receipts_dir = root / "receipts"
    if receipts_dir.is_dir():
        for path in sorted(receipts_dir.glob("*.json")):
            evidence_hashes[path.name] = sha256_file(path)

    if _libvirt_resource_present("domain", VM_NAME):
        run(["virsh", "-c", LIBVIRT_URI, "destroy", VM_NAME], check=False)
        undefine = run(
            ["virsh", "-c", LIBVIRT_URI, "undefine", VM_NAME, "--nvram"],
            check=False,
        )
        if undefine.returncode != 0:
            run(["virsh", "-c", LIBVIRT_URI, "undefine", VM_NAME], check=False)

    if _libvirt_resource_present("pool", POOL_NAME):
        run(
            ["virsh", "-c", LIBVIRT_URI, "vol-delete", VOLUME_NAME, "--pool", POOL_NAME],
            check=False,
        )
        run(
            ["virsh", "-c", LIBVIRT_URI, "vol-delete", BASE_VOLUME, "--pool", POOL_NAME],
            check=False,
        )
        run(["virsh", "-c", LIBVIRT_URI, "pool-destroy", POOL_NAME], check=False)
        run(["virsh", "-c", LIBVIRT_URI, "pool-delete", POOL_NAME], check=False)
        run(["virsh", "-c", LIBVIRT_URI, "pool-undefine", POOL_NAME], check=False)

    if _libvirt_resource_present("domain", VM_NAME):
        raise RuntimeErrorEB("Experiment-B VM still exists after teardown")
    if _libvirt_resource_present("pool", POOL_NAME):
        raise RuntimeErrorEB("Experiment-B storage pool still exists after teardown")
    if POOL_TARGET.exists():
        if any(POOL_TARGET.iterdir()):
            raise RuntimeErrorEB("Experiment-B libvirt pool directory is not empty after teardown")
        POOL_TARGET.rmdir()

    shutil.rmtree(root, ignore_errors=False)
    result = {
        "schema_version": 1,
        "status": "retired",
        "vm": VM_NAME,
        "pool": POOL_NAME,
        "pool_target_removed": not POOL_TARGET.exists(),
        "state_removed": not root.exists(),
        "evidence_receipts": evidence_hashes,
    }
    atomic_json(RETIREMENT_RECEIPT, result)
    return result


def _performance_modules() -> tuple[Any, Any]:
    root_text = str(ROOT)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    from scripts.performance import api_runtime_evidence as evidence
    from scripts.performance import domain_scale

    return evidence, domain_scale


def _kubectl(
    root: Path,
    arguments: list[str],
    *,
    input_text: str | None = None,
    check: bool = True,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    kubectl = toolchain(root)["tools"]["kubectl"]
    return run(
        [kubectl, *arguments],
        input_text=input_text,
        env=kube_env(root),
        check=check,
        timeout=timeout,
    )


def _kubectl_json(root: Path, arguments: list[str]) -> dict[str, Any]:
    result = _kubectl(root, [*arguments, "-o", "json"])
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeErrorEB(f"kubectl JSON readback failed for {arguments!r}") from exc
    if not isinstance(value, dict):
        raise RuntimeErrorEB(f"kubectl JSON readback is not an object for {arguments!r}")
    return value


def _psql(root: Path, sql: str, *, tuples_only: bool = True) -> str:
    argv = [
        "-n", DATA_NAMESPACE,
        "exec", "-i", "deployment/postgres", "--",
        "psql", "-U", "commonthing", "-d", "commonthing", "-v", "ON_ERROR_STOP=1",
    ]
    if tuples_only:
        argv.extend(["-At"])
    return _kubectl(root, argv, input_text=sql, timeout=600).stdout.strip()


def _run_input_file(
    argv: list[str],
    source: Path,
    *,
    env: dict[str, str] | None = None,
    timeout: int = 1200,
) -> None:
    with source.open("rb") as handle:
        result = subprocess.run(
            argv,
            cwd=ROOT,
            stdin=handle,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            timeout=timeout,
            check=False,
        )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace")[-3000:]
        raise RuntimeErrorEB(
            f"streamed command failed ({result.returncode}): {argv[0]}: {detail}"
        )


def _run_binary_to_file(
    argv: list[str],
    destination: Path,
    *,
    env: dict[str, str] | None = None,
    timeout: int = 1200,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.partial")
    temporary.unlink(missing_ok=True)
    try:
        with temporary.open("wb") as output:
            result = subprocess.run(
                argv,
                cwd=ROOT,
                stdout=output,
                stderr=subprocess.PIPE,
                env=env,
                timeout=timeout,
                check=False,
            )
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", "replace")[-3000:]
            raise RuntimeErrorEB(
                f"binary capture failed ({result.returncode}): {argv[0]}: {detail}"
            )
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _write_streamed_fixture_sql(
    load_sql: Path,
    nodes_csv: Path,
    edges_csv: Path,
    destination: Path,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with load_sql.open("r", encoding="utf-8") as source, destination.open(
        "w", encoding="utf-8", newline="\n"
    ) as output:
        for line in source:
            if line.startswith("\\copy ") and "domain_nodes" in line:
                prefix, suffix = line.rstrip("\n").split(" FROM ", 1)
                _old_path, options = suffix.split(" WITH ", 1)
                output.write(f"{prefix} FROM STDIN WITH {options}\n")
                with nodes_csv.open("r", encoding="utf-8") as rows:
                    shutil.copyfileobj(rows, output)
                output.write("\\.\n")
            elif line.startswith("\\copy ") and "domain_edges" in line:
                prefix, suffix = line.rstrip("\n").split(" FROM ", 1)
                _old_path, options = suffix.split(" WITH ", 1)
                output.write(f"{prefix} FROM STDIN WITH {options}\n")
                with edges_csv.open("r", encoding="utf-8") as rows:
                    shutil.copyfileobj(rows, output)
                output.write("\\.\n")
            else:
                output.write(line)


def _t048_canonical_visibility(fixture: dict[str, Any]) -> str:
    kind = fixture.get("kind")
    if not isinstance(kind, str) or not kind:
        raise RuntimeErrorEB("T048 fixture node has no canonical kind")
    return "public" if kind == "Projekt" else "hidden"


def _t048_live_fixture_binding(
    root: Path, manifest: Path, generation_id: str
) -> dict[str, Any]:
    root_text = str(ROOT)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    from scripts.performance import api_runtime_live_binding as live_binding

    _manifest, fixture_rows = live_binding._manifest_and_fixture(manifest)
    db_rows = live_binding._json_lines(
        _psql(
            root,
            r"""
SELECT json_build_object(
  'id', id,
  'kind', kind,
  'title', title,
  'lat', lat,
  'lon', lon,
  'created_at', to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
  'updated_at', to_char(updated_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
  'payload', payload,
  'search_visibility', search_visibility
)::text
FROM domain_nodes
ORDER BY id;
""",
        ),
        "Experiment-B domain_nodes query",
    )
    canonical_fixture_rows = [
        {**row, "search_visibility": _t048_canonical_visibility(row)}
        for row in fixture_rows
    ]
    fixture_sha = live_binding._rows_sha256(canonical_fixture_rows)
    database_sha = live_binding._rows_sha256(db_rows)
    if len(db_rows) != len(canonical_fixture_rows) or database_sha != fixture_sha:
        raise RuntimeErrorEB(
            "live domain_nodes content does not match the deterministic T048 fixture"
        )

    generation_literal = live_binding._sql_literal(generation_id)
    generation_rows = live_binding._json_lines(
        _psql(
            root,
            f"""
SELECT json_build_object(
  'generation_id', generation_id,
  'state', state,
  'expected_nodes', expected_nodes,
  'completed_nodes', completed_nodes
)::text
FROM search_index_generations
WHERE generation_id = {generation_literal} AND state = 'active';
""",
        ),
        "Experiment-B active search generation query",
    )
    if len(generation_rows) != 1:
        raise RuntimeErrorEB("Experiment-B requires exactly one active T048 generation")
    generation = generation_rows[0]

    projection_rows = live_binding._json_lines(
        _psql(
            root,
            f"""
SELECT json_build_object(
  'id', p.node_id,
  'kind', p.kind,
  'title', p.title,
  'search_visibility', n.search_visibility,
  'owner_account_id', weltgewebe_search_node_owner_account_id(n.payload)
)::text
FROM search_node_projections p
JOIN domain_nodes n ON n.id = p.node_id
WHERE p.generation_id = {generation_literal}
ORDER BY p.node_id;
""",
        ),
        "Experiment-B active search projection query",
    )
    expected_nodes = generation.get("expected_nodes")
    completed_nodes = generation.get("completed_nodes")
    if (
        len(projection_rows) < 1
        or expected_nodes != len(projection_rows)
        or completed_nodes != len(projection_rows)
    ):
        raise RuntimeErrorEB("Experiment-B active T048 search generation is incomplete")

    fixture_by_id = {row["id"]: row for row in fixture_rows}
    expected_projection_rows: list[dict[str, Any]] = []
    actual_projection_rows: list[dict[str, Any]] = []
    for projection in projection_rows:
        node_id = projection.get("id")
        fixture = fixture_by_id.get(node_id)
        if fixture is None:
            raise RuntimeErrorEB(
                f"Experiment-B search projection {node_id!r} is absent from fixture"
            )
        canonical_visibility = _t048_canonical_visibility(fixture)
        expected_projection_rows.append(
            live_binding._expected_projection_identity(
                {**projection, "search_visibility": canonical_visibility},
                fixture,
            )
        )
        actual_projection_rows.append(
            {
                "id": projection.get("id"),
                "kind": projection.get("kind"),
                "title": projection.get("title"),
                "search_visibility": projection.get("search_visibility"),
            }
        )
    projection_sha = live_binding._rows_sha256(actual_projection_rows)
    expected_projection_sha = live_binding._rows_sha256(expected_projection_rows)
    if projection_sha != expected_projection_sha:
        raise RuntimeErrorEB(
            "live search projection content does not match the deterministic T048 fixture"
        )
    return {
        "manifest_sha256": sha256_file(manifest),
        "domain_nodes_count": len(db_rows),
        "fixture_nodes_content_sha256": fixture_sha,
        "database_nodes_content_sha256": database_sha,
        "generation_id": generation_id,
        "expected_nodes": int(expected_nodes),
        "completed_nodes": int(completed_nodes),
        "active_projection_count": len(projection_rows),
        "fixture_projection_content_sha256": expected_projection_sha,
        "database_projection_content_sha256": projection_sha,
    }


def seed_t048_fixture(root: Path) -> dict[str, Any]:
    evidence, _domain_scale = _performance_modules()
    config = load_config()
    release_path = root / "receipts/release.json"
    if not release_path.is_file():
        raise RuntimeErrorEB("T048 fixture load requires an applied release receipt")
    release = json.loads(release_path.read_text(encoding="utf-8"))
    source_commit = release.get("source_commit")
    if (
        not isinstance(source_commit, str)
        or not COMMIT_RE.fullmatch(source_commit)
        or git_head() != source_commit
        or remote_main() != source_commit
    ):
        raise RuntimeErrorEB("T048 fixture release is not current protected main")
    contract_section = evidence.api_runtime_section(
        evidence.load_policy(PERFORMANCE_POLICY)
    )
    proof = contract_section["dataset_proof"]
    profile = str(proof["profile"])
    evidence_dir = root / "performance"
    fixture = evidence_dir / "fixture"
    manifest = fixture / "manifest.json"
    if not manifest.is_file():
        if fixture.exists():
            raise RuntimeErrorEB(
                "partial T048 fixture directory exists; refusing implicit replacement"
            )
        run(
            [
                sys.executable, "-B", str(DOMAIN_SCALE), "generate",
                "--profile", profile,
                "--output-dir", str(fixture),
            ],
            timeout=900,
        )
    binding = evidence.load_dataset_binding(
        manifest,
        contract_section,
        repo_root=ROOT,
    )
    counts = binding["counts"]
    node_count = int(counts["nodes"])
    edge_count = int(counts["edges"])

    existing_nodes = int(_psql(root, "SELECT count(*) FROM domain_nodes;"))
    existing_edges = int(_psql(root, "SELECT count(*) FROM domain_edges;"))
    generation_id = str(config["semantic_search"]["generation_id"])
    existing_generation = int(
        _psql(
            root,
            "SELECT count(*) FROM search_index_generations "
            f"WHERE generation_id = '{generation_id}';",
        )
    )
    receipt_path = root / "receipts/t048-fixture.json"

    def emit_receipt() -> dict[str, Any]:
        observed_nodes = int(_psql(root, "SELECT count(*) FROM domain_nodes;"))
        observed_edges = int(_psql(root, "SELECT count(*) FROM domain_edges;"))
        public_nodes = int(
            _psql(
                root,
                "SELECT count(*) FROM domain_nodes WHERE search_visibility='public';",
            )
        )
        observed_projections = int(
            _psql(
                root,
                "SELECT count(*) FROM search_node_projections "
                f"WHERE generation_id = '{generation_id}';",
            )
        )
        active_generation = int(
            _psql(
                root,
                "SELECT count(*) FROM search_index_generations "
                f"WHERE generation_id = '{generation_id}' AND state = 'active';",
            )
        )
        pending_jobs = int(
            _psql(
                root,
                "SELECT count(*) FROM search_projection_jobs "
                f"WHERE generation_id = '{generation_id}' AND state <> 'done';",
            )
        )
        if (
            observed_nodes != node_count
            or observed_edges != edge_count
            or public_nodes < 1
            or observed_projections != node_count
            or active_generation != 1
            or pending_jobs != 0
        ):
            raise RuntimeErrorEB(
                "T048 fixture/search projection counts do not match the canonical manifest"
            )
        live_binding = _t048_live_fixture_binding(root, manifest, generation_id)
        receipt = {
            "schema_version": 1,
            "status": "loaded",
            "source_commit": source_commit,
            "profile": profile,
            "manifest": str(manifest),
            "manifest_sha256": binding["manifest_sha256"],
            "nodes": observed_nodes,
            "edges": observed_edges,
            "public_semantic_nodes": public_nodes,
            "search_projections": observed_projections,
            "generation_id": generation_id,
            "generation_state": "active",
            "projection_mode": "synthetic-canonical-t048",
            "pending_projection_jobs": pending_jobs,
            "live_binding": live_binding,
            "production_data_used": False,
        }
        atomic_json(receipt_path, receipt)
        return receipt

    if (
        existing_nodes == node_count
        and existing_edges == edge_count
        and existing_generation == 1
        and receipt_path.is_file()
    ):
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if receipt.get("manifest_sha256") != binding["manifest_sha256"]:
            raise RuntimeErrorEB("existing T048 fixture receipt has a different manifest")
        if receipt.get("source_commit") != source_commit:
            raise RuntimeErrorEB("existing T048 fixture receipt has a different source commit")
        current_live_binding = _t048_live_fixture_binding(
            root, manifest, generation_id
        )
        if receipt.get("live_binding") != current_live_binding:
            raise RuntimeErrorEB(
                "existing T048 fixture receipt does not match live database/search contents"
            )
        return receipt
    if (
        existing_nodes == node_count
        and existing_edges == edge_count
        and existing_generation == 1
        and not receipt_path.is_file()
    ):
        return emit_receipt()
    if existing_nodes or existing_edges or existing_generation:
        raise RuntimeErrorEB(
            "target database is not empty enough for a fresh T048 fixture load"
        )

    load_sql = evidence_dir / "load.sql"
    run(
        [
            sys.executable, "-B", str(DOMAIN_SCALE), "render-load",
            "--manifest", str(manifest),
            "--output", str(load_sql),
        ]
    )
    files = binding["files"]
    nodes_csv = fixture / str(files["nodes"]["name"])
    edges_csv = fixture / str(files["edges"]["name"])
    streamed = evidence_dir / "kubernetes-load.sql"
    _write_streamed_fixture_sql(load_sql, nodes_csv, edges_csv, streamed)
    kubectl = toolchain(root)["tools"]["kubectl"]
    _run_input_file(
        [
            kubectl, "-n", DATA_NAMESPACE, "exec", "-i",
            "deployment/postgres", "--",
            "psql", "-U", "commonthing", "-d", "commonthing",
            "-v", "ON_ERROR_STOP=1",
        ],
        streamed,
        env=kube_env(root),
        timeout=1800,
    )

    semantic = config["semantic_search"]
    seed_sql = f"""
BEGIN;
INSERT INTO search_index_generations (
    generation_id, provider, model_id, model_revision, runtime_identity,
    dimension, document_revision, normalization_revision, ranking_revision,
    state, expected_nodes
) VALUES (
    '{semantic["generation_id"]}',
    '{semantic["provider"]}',
    '{semantic["model_id"]}',
    '{semantic["model_revision"]}',
    '{semantic["runtime_identity"]}',
    {int(semantic["dimension"])},
    'node-document-v4-canonical-visibility',
    'weltgewebe-search-normalization-v1',
    'weltgewebe-hybrid-ranking-v2',
    'building',
    (SELECT count(*) FROM weltgewebe_perf.domain_nodes)
);

INSERT INTO domain_nodes (
    id, kind, title, lat, lon, created_at, updated_at, payload, search_visibility
)
SELECT id, kind, title, lat, lon, created_at, updated_at, payload,
       CASE WHEN kind = 'Projekt' THEN 'public' ELSE 'hidden' END
  FROM weltgewebe_perf.domain_nodes
 ORDER BY id;

INSERT INTO domain_edges (id, source_id, target_id, edge_kind, created_at, payload)
SELECT id, source_id, target_id, edge_kind, created_at, payload
  FROM weltgewebe_perf.domain_edges
 ORDER BY id;

INSERT INTO search_node_projections (
    generation_id, node_id, source_version, source_revision, content_sha256,
    title, tags, searchable_text, language, kind, status, visibility_scopes,
    semantic_state, embedding
)
SELECT
    g.generation_id,
    n.id,
    v.source_version,
    v.source_revision,
    CASE
        WHEN n.search_visibility = 'public' THEN repeat('0', 64)
        ELSE 'e0f631f5602e764ef8a5f14e36d2d81663b20cd305a30af0dad6c0d759e5a955'
    END,
    CASE WHEN n.search_visibility = 'public' THEN n.title ELSE '[nicht öffentlich]' END,
    CASE
        WHEN n.search_visibility = 'public'
        THEN ARRAY(SELECT jsonb_array_elements_text(n.payload -> 'tags'))
        ELSE '{{}}'::TEXT[]
    END,
    CASE
        WHEN n.search_visibility = 'public'
        THEN coalesce(n.payload ->> 'summary', n.title)
        ELSE '[nicht öffentlich]'
    END,
    CASE WHEN n.search_visibility = 'public' THEN 'de' ELSE 'und' END,
    CASE WHEN n.search_visibility = 'public' THEN n.kind ELSE '[nicht öffentlich]' END,
    CASE WHEN n.search_visibility = 'public' THEN 'active' ELSE 'hidden' END,
    CASE
        WHEN n.search_visibility = 'public' THEN ARRAY['public']::TEXT[]
        ELSE '{{}}'::TEXT[]
    END,
    CASE WHEN n.search_visibility = 'public' THEN 'ready' ELSE 'unavailable' END,
    CASE
        WHEN n.search_visibility = 'public'
        THEN array_fill(0.0::DOUBLE PRECISION, ARRAY[g.dimension])
        ELSE NULL
    END
  FROM domain_nodes n
  JOIN search_node_versions v ON v.node_id = n.id
  CROSS JOIN search_index_generations g
 WHERE g.state = 'building'
 ORDER BY n.id;

UPDATE search_projection_jobs
   SET state = 'done', completed_at = clock_timestamp()
 WHERE generation_id = '{semantic["generation_id"]}';

UPDATE search_index_generations
   SET expected_nodes = (SELECT count(*) FROM domain_nodes),
       completed_nodes = (
           SELECT count(*) FROM search_node_versions WHERE deleted_at IS NULL
       )
 WHERE generation_id = '{semantic["generation_id"]}';

DO $$
BEGIN
    IF NOT weltgewebe_search_generation_activation_ready(
        '{semantic["generation_id"]}'
    ) THEN
        RAISE EXCEPTION 'Experiment-B T048 search generation is not activation-ready';
    END IF;
END
$$;
SELECT weltgewebe_activate_search_generation('{semantic["generation_id"]}');
COMMIT;
"""
    _psql(root, seed_sql, tuples_only=False)
    return emit_receipt()


def _k6_image_binding() -> tuple[str, str]:
    text = K6_WORKFLOW.read_text(encoding="utf-8")
    match = re.search(
        r"(?m)^\s*K6_IMAGE:\s*(grafana/k6@sha256:[0-9a-f]{64})\s*$",
        text,
    )
    if match is None:
        raise RuntimeErrorEB("canonical T048 workflow has no digest-bound K6_IMAGE")
    return match.group(1), hashlib.sha256(text.encode("utf-8")).hexdigest()


def _reserve_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as handle:
        handle.bind(("127.0.0.1", 0))
        return int(handle.getsockname()[1])


def _http_read(url: str, *, timeout: int = 10) -> tuple[int, bytes, float]:
    started = time.perf_counter()
    request = urllib.request.Request(
        url, headers={"Accept": "application/json,text/html,text/plain,*/*"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            status_code = int(response.status)
    except urllib.error.HTTPError as exc:
        body = exc.read()
        status_code = int(exc.code)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return status_code, body, elapsed_ms


def _wait_http_200(url: str, process: subprocess.Popen[Any] | None = None) -> None:
    for _ in range(60):
        if process is not None and process.poll() is not None:
            raise RuntimeErrorEB("port-forward exited before the target became ready")
        try:
            status_code, _body, _elapsed = _http_read(url, timeout=2)
        except urllib.error.URLError:
            time.sleep(1)
            continue
        if status_code == 200:
            return
        time.sleep(1)
    raise RuntimeErrorEB(f"HTTP target did not become ready: {url}")


def _start_api_port_forward(root: Path) -> tuple[subprocess.Popen[Any], int, Any, Any]:
    port = _reserve_loopback_port()
    evidence_dir = root / "performance"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    stdout = (evidence_dir / "port-forward.stdout").open("w", encoding="utf-8")
    stderr = (evidence_dir / "port-forward.stderr").open("w", encoding="utf-8")
    kubectl = toolchain(root)["tools"]["kubectl"]
    process = subprocess.Popen(
        [
            kubectl, "-n", APP_NAMESPACE, "port-forward",
            "service/weltgewebe-api", f"{port}:8080", "--address=127.0.0.1",
        ],
        cwd=ROOT,
        stdout=stdout,
        stderr=stderr,
        env=kube_env(root),
        text=True,
    )
    try:
        _wait_http_200(f"http://127.0.0.1:{port}/health/live", process)
    except Exception:
        process.terminate()
        process.wait(timeout=10)
        stdout.close()
        stderr.close()
        raise
    return process, port, stdout, stderr


def _api_pod(root: Path) -> tuple[str, dict[str, Any]]:
    pods = _kubectl_json(
        root,
        [
            "-n", APP_NAMESPACE, "get", "pods",
            "-l", "app.kubernetes.io/name=weltgewebe-api",
        ],
    )
    items = pods.get("items")
    if not isinstance(items, list) or len(items) != 1:
        raise RuntimeErrorEB("Experiment B expects exactly one API pod")
    pod = items[0]
    name = pod.get("metadata", {}).get("name")
    if not isinstance(name, str) or not name:
        raise RuntimeErrorEB("API pod has no name")
    return name, pod


def _parse_cpu_quantity(value: str) -> float:
    if value.endswith("m"):
        return float(value[:-1]) / 1000.0
    return float(value)


def _parse_memory_quantity(value: str) -> int:
    units = {
        "Ki": 1024,
        "Mi": 1024 ** 2,
        "Gi": 1024 ** 3,
        "K": 1000,
        "M": 1000 ** 2,
        "G": 1000 ** 3,
    }
    for suffix, factor in units.items():
        if value.endswith(suffix):
            return int(float(value[: -len(suffix)]) * factor)
    return int(value)


def _sample_api_cgroup(root: Path, pod_name: str) -> dict[str, Any]:
    script = (
        "cat /sys/fs/cgroup/cpu.stat; "
        "printf '\\n__CPU_MAX__\\n'; cat /sys/fs/cgroup/cpu.max; "
        "printf '\\n__MEM_CURRENT__\\n'; cat /sys/fs/cgroup/memory.current; "
        "printf '\\n__MEM_MAX__\\n'; cat /sys/fs/cgroup/memory.max"
    )
    raw = _kubectl(
        root,
        [
            "-n", APP_NAMESPACE, "exec", pod_name, "-c", "api", "--",
            "/bin/sh", "-c", script,
        ],
    ).stdout
    cpu_part, rest = raw.split("__CPU_MAX__", 1)
    cpu_max_part, rest = rest.split("__MEM_CURRENT__", 1)
    mem_current_part, mem_max_part = rest.split("__MEM_MAX__", 1)
    cpu_values = {}
    for line in cpu_part.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1].isdigit():
            cpu_values[fields[0]] = int(fields[1])
    usage_usec = cpu_values.get("usage_usec")
    if usage_usec is None:
        raise RuntimeErrorEB("API cgroup cpu.stat has no usage_usec")
    cpu_max = cpu_max_part.strip().split()
    if len(cpu_max) != 2 or cpu_max[0] == "max":
        raise RuntimeErrorEB("API cgroup does not expose a finite CPU quota")
    quota, period = (int(cpu_max[0]), int(cpu_max[1]))
    memory_current = int(mem_current_part.strip())
    memory_max_raw = mem_max_part.strip()
    if memory_max_raw == "max":
        raise RuntimeErrorEB("API cgroup does not expose a finite memory limit")
    return {
        "observed_at_unix_ms": time.time_ns() // 1_000_000,
        "usage_usec": usage_usec,
        "memory_bytes": memory_current,
        "cpu_limit_cores": quota / period,
        "memory_limit_bytes": int(memory_max_raw),
    }


def _database_connection_count(root: Path) -> int:
    value = _psql(root, "SELECT count(*) FROM pg_stat_activity;")
    try:
        count = int(value)
    except ValueError as exc:
        raise RuntimeErrorEB("PostgreSQL connection count is not an integer") from exc
    if count < 0:
        raise RuntimeErrorEB("PostgreSQL connection count is negative")
    return count


def _stop_process(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _sample_t048_load(
    root: Path,
    pod_name: str,
    load: subprocess.Popen[Any],
    resource_samples: list[dict[str, Any]],
    db_samples: list[int],
) -> int:
    try:
        while load.poll() is None:
            time.sleep(1)
            resource_samples.append(_sample_api_cgroup(root, pod_name))
            db_samples.append(_database_connection_count(root))
        if load.returncode is None:
            raise RuntimeErrorEB("canonical T048 k6 workload has no terminal return code")
        return int(load.returncode)
    finally:
        _stop_process(load)


def t048_load_proof(root: Path, source_commit: str) -> dict[str, Any]:
    evidence, _domain_scale = _performance_modules()
    if git_head() != source_commit or remote_main() != source_commit:
        raise RuntimeErrorEB("T048 proof source is not current protected main")

    report_path, attempt_path, attempt_started_at_unix_ms = (
        _begin_live_check_attempt(root, "t048-load", source_commit)
    )
    fixture_receipt = seed_t048_fixture(root)
    manifest = Path(fixture_receipt["manifest"])
    policy = evidence.load_policy(PERFORMANCE_POLICY)
    contract_section = evidence.api_runtime_section(policy)
    scenario = contract_section["scenario"]
    k6_image, k6_workflow_sha256 = _k6_image_binding()
    k6_summary_path = root / "performance/k6-summary.json"
    metrics_before_path = root / "performance/metrics-before.prom"
    metrics_after_path = root / "performance/metrics-after.prom"
    resource_path = root / "performance/resource-receipt.json"
    db_path = root / "performance/database-connections.json"

    pod_name, pod = _api_pod(root)
    api_container = next(
        (
            item for item in pod.get("spec", {}).get("containers", [])
            if item.get("name") == "api"
        ),
        None,
    )
    if not isinstance(api_container, dict):
        raise RuntimeErrorEB("API pod has no api container")
    limits = api_container.get("resources", {}).get("limits", {})
    declared_cpu = _parse_cpu_quantity(str(limits.get("cpu", "")))
    declared_memory = _parse_memory_quantity(str(limits.get("memory", "")))

    process, port, pf_stdout, pf_stderr = _start_api_port_forward(root)
    base_url = f"http://127.0.0.1:{port}"
    try:
        before_status, before_body, _elapsed = _http_read(f"{base_url}/metrics")
        if before_status != 200:
            raise RuntimeErrorEB("API /metrics pre-snapshot failed")
        metrics_before = before_body.decode("utf-8")
        metrics_before_path.write_text(metrics_before, encoding="utf-8")
        families = evidence.parse_prometheus_text(metrics_before)
        if evidence.measured_api_commit(families) != source_commit:
            raise RuntimeErrorEB("API build_info commit does not match T048 source commit")

        run_id = f"t085-{source_commit[:12]}-{int(time.time())}"
        manifest_sha = sha256_file(manifest)
        evidence_dir = root / "performance"
        stdout_path = evidence_dir / "k6.stdout"
        stderr_path = evidence_dir / "k6.stderr"
        docker_args = [
            "docker", "run", "--rm", "--network", "host",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--volume", f"{ROOT}:/workspace:ro",
            "--volume", f"{evidence_dir}:/evidence",
            "--workdir", "/workspace",
            "--env", f"BASE_URL={base_url}",
            "--env", f"API_RUNTIME_VUS={scenario['virtual_users']}",
            "--env", f"API_RUNTIME_DURATION_SECONDS={scenario['duration_seconds']}",
            "--env", f"API_RUNTIME_DATASET_PROFILE={scenario['dataset_profile']}",
            "--env", f"API_RUNTIME_CONCURRENCY_PROFILE={scenario['concurrency_profile']}",
            "--env", f"API_RUNTIME_DATASET_MANIFEST_SHA256={manifest_sha}",
            "--env", f"API_RUNTIME_SEARCH_QUERY={scenario['search_query']}",
            "--env", f"API_RUNTIME_RUN_ID={run_id}",
            "--env", f"API_RUNTIME_K6_IMAGE={k6_image}",
            "--env", "API_RUNTIME_SUMMARY_PATH=/evidence/k6-summary.json",
            k6_image, "run", str(K6_WORKLOAD.relative_to(ROOT)),
        ]

        initial_cgroup = _sample_api_cgroup(root, pod_name)
        resource_samples = [initial_cgroup]
        db_samples = [_database_connection_count(root)]
        sampler_started = time.time_ns() // 1_000_000
        with stdout_path.open("w", encoding="utf-8") as out, stderr_path.open(
            "w", encoding="utf-8"
        ) as err:
            load = subprocess.Popen(
                docker_args, cwd=ROOT, stdout=out, stderr=err, text=True
            )
            load_returncode = _sample_t048_load(
                root,
                pod_name,
                load,
                resource_samples,
                db_samples,
            )
        resource_samples.append(_sample_api_cgroup(root, pod_name))
        db_samples.append(_database_connection_count(root))
        sampler_finished = time.time_ns() // 1_000_000
        if load_returncode != 0:
            detail = stderr_path.read_text(encoding="utf-8")[-3000:]
            raise RuntimeErrorEB(f"canonical T048 k6 workload failed: {detail}")

        after_status, after_body, _elapsed = _http_read(f"{base_url}/metrics")
        if after_status != 200:
            raise RuntimeErrorEB("API /metrics post-snapshot failed")
        metrics_after = after_body.decode("utf-8")
        metrics_after_path.write_text(metrics_after, encoding="utf-8")

        cpu_percentages: list[float] = []
        for first, second in zip(resource_samples, resource_samples[1:]):
            elapsed_us = (
                int(second["observed_at_unix_ms"]) - int(first["observed_at_unix_ms"])
            ) * 1000
            usage_delta = int(second["usage_usec"]) - int(first["usage_usec"])
            if elapsed_us > 0 and usage_delta >= 0:
                cpu_percentages.append((usage_delta / elapsed_us) * 100.0)
        if not cpu_percentages:
            raise RuntimeErrorEB("API CPU sampler produced no usable intervals")
        peak_cpu = max(cpu_percentages)
        peak_memory = max(int(item["memory_bytes"]) for item in resource_samples)
        cpu_limits = {round(float(item["cpu_limit_cores"]), 9) for item in resource_samples}
        memory_limits = {int(item["memory_limit_bytes"]) for item in resource_samples}
        if cpu_limits != {declared_cpu}:
            raise RuntimeErrorEB("live API cgroup CPU quota differs from the deployment limit")
        if memory_limits != {declared_memory}:
            raise RuntimeErrorEB("live API cgroup memory limit differs from the deployment limit")

        resource_receipt = {
            "schema_version": 3,
            "contract": "api-replica-resource-sample-v3",
            "run_id": run_id,
            "container_name": "weltgewebe-api",
            "started_at_unix_ms": sampler_started,
            "finished_at_unix_ms": sampler_finished,
            "peaks": {
                "cpu_percent": peak_cpu,
                "memory_bytes": peak_memory,
            },
            "sample_count": len(resource_samples),
        }
        database_receipt = {
            "schema_version": 2,
            "contract": "postgres-connection-sample-v2",
            "run_id": run_id,
            "database_container": "postgres",
            "started_at_unix_ms": sampler_started,
            "finished_at_unix_ms": sampler_finished,
            "max_connections": max(db_samples),
            "sample_count": len(db_samples),
            "samples": db_samples,
        }
        atomic_json(resource_path, resource_receipt)
        atomic_json(db_path, database_receipt)
        evidence.load_resource_receipt(resource_path)
        evidence.load_database_connection_receipt(db_path)

        summary = evidence.load_k6_summary(k6_summary_path)
        if evidence.extract_declared_scenario(summary) != scenario:
            raise RuntimeErrorEB("k6 scenario drifted from the canonical T048 policy")
        http_metrics = evidence.extract_http_metrics(summary)
        failures = list(
            evidence.threshold_failures(http_metrics, contract_section["thresholds"])
        )
        workload = evidence.extract_runtime_workload_bindings(summary)
        if (
            workload["started_at_unix_ms"] < sampler_started
            or workload["finished_at_unix_ms"] > sampler_finished
        ):
            failures.append("resource/database sampler window does not cover k6 load")
        if peak_cpu > declared_cpu * 100.0:
            failures.append(
                f"API peak CPU {peak_cpu:.3f}% exceeds its {declared_cpu:.3f}-core hard limit"
            )
        if peak_memory > declared_memory:
            failures.append(
                f"API peak memory {peak_memory} exceeds its {declared_memory}-byte hard limit"
            )

        before = evidence.parse_prometheus_text(metrics_before)
        after = evidence.parse_prometheus_text(metrics_after)
        if evidence.measured_api_commit(after) != source_commit:
            failures.append("API build_info commit changed during T048 load")
        http_search_before = evidence.sum_counter(
            before, evidence.HTTP_COUNTER_NAME, {"path": evidence.SEARCH_PATH_LABEL}
        )
        http_search_after = evidence.sum_counter(
            after, evidence.HTTP_COUNTER_NAME, {"path": evidence.SEARCH_PATH_LABEL}
        )
        expected_queries = int(round(http_search_after - http_search_before))
        delta_hist = evidence.histogram_delta(
            evidence.histogram_snapshot(before, evidence.QUERY_HISTOGRAM_NAME),
            evidence.histogram_snapshot(after, evidence.QUERY_HISTOGRAM_NAME),
        )
        observed_queries = int(round(delta_hist.total_count))
        if expected_queries <= 0 or expected_queries != observed_queries:
            failures.append(
                "API search request count and PostgreSQL repository query count disagree"
            )
        database_metrics = {
            "p50_ms": evidence.histogram_quantile_ms(delta_hist, 0.50),
            "p95_ms": evidence.histogram_quantile_ms(delta_hist, 0.95),
            "p99_ms": evidence.histogram_quantile_ms(delta_hist, 0.99),
            "query_count_expected": expected_queries,
            "query_count_observed": observed_queries,
        }
        report = {
            "schema_version": 1,
            "status": "fail" if failures else "pass",
            "source_commit": source_commit,
            "policy_sha256": sha256_file(PERFORMANCE_POLICY),
            "k6_workflow_sha256": k6_workflow_sha256,
            "k6_image": k6_image,
            "fixture_manifest_sha256": manifest_sha,
            "scenario": scenario,
            "thresholds": contract_section["thresholds"],
            "http": http_metrics,
            "database": database_metrics,
            "resources": {
                "api_peak_cpu_percent": peak_cpu,
                "api_peak_memory_bytes": peak_memory,
                "api_cpu_limit_cores": declared_cpu,
                "api_memory_limit_bytes": declared_memory,
                "postgres_max_connections": max(db_samples),
            },
            "failures": failures,
            "limitations": [
                "This is an Experiment-B target measurement, not a production-capacity claim.",
                "The canonical T048 scenario and latency/error thresholds are read from policies/performance.v1.json.",
                "Kubernetes-specific cgroup and PostgreSQL samplers replace the Docker-only T048 sampler implementations while preserving their measured quantities.",
                "The canonical T048 web-runtime proof is fixture-driven and target-independent; Experiment-B web functionality is covered separately by functional-readback.",
            ],
        }
        atomic_json(report_path, report)
        _complete_live_check_attempt(
            attempt_path,
            report_path,
            source_commit,
            attempt_started_at_unix_ms,
            str(report["status"]),
        )
        if failures:
            raise RuntimeErrorEB("T048 Experiment-B load gate failed: " + "; ".join(failures))
        return report
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        pf_stdout.close()
        pf_stderr.close()


def _gateway_base_url(root: Path) -> str:
    gateway = _kubectl_json(
        root,
        ["-n", APP_NAMESPACE, "get", "gateway", "commonthing-experiment-b"],
    )
    addresses = gateway.get("status", {}).get("addresses")
    if not isinstance(addresses, list) or not addresses:
        raise RuntimeErrorEB("Experiment-B Gateway has no admitted address")
    value = addresses[0].get("value") if isinstance(addresses[0], dict) else None
    if not isinstance(value, str) or not value:
        raise RuntimeErrorEB("Experiment-B Gateway address is invalid")
    return f"http://{value}"


def functional_readback(root: Path, source_commit: str) -> dict[str, Any]:
    receipt_path, attempt_path, attempt_started_at_unix_ms = (
        _begin_live_check_attempt(root, "functional-readback", source_commit)
    )
    base = _gateway_base_url(root)
    checks: dict[str, Any] = {}
    status_code, body, elapsed = _http_read(base + "/")
    checks["web_root"] = {"status": status_code, "elapsed_ms": elapsed}
    if status_code != 200 or not body:
        raise RuntimeErrorEB("Experiment-B web root is not readable through Gateway")

    status_code, body, elapsed = _http_read(base + "/_app/version.json")
    version = json.loads(body) if status_code == 200 else {}
    checks["web_revision"] = {
        "status": status_code,
        "elapsed_ms": elapsed,
        "commit": version.get("commit") if isinstance(version, dict) else None,
    }
    if status_code != 200 or version.get("commit") != source_commit:
        raise RuntimeErrorEB("Experiment-B Web revision does not match source commit")

    status_code, body, elapsed = _http_read(base + "/health/ready")
    health = json.loads(body) if status_code == 200 else {}
    checks["api_ready"] = {"status": status_code, "elapsed_ms": elapsed, "body": health}
    if status_code != 200 or health.get("status") != "ok":
        raise RuntimeErrorEB("Experiment-B API readiness failed through Gateway")

    status_code, body, elapsed = _http_read(
        base + "/api/nodes?pagination=cursor&limit=1"
    )
    checks["domain_nodes"] = {"status": status_code, "elapsed_ms": elapsed}
    if status_code != 200:
        raise RuntimeErrorEB("Experiment-B domain read failed through Gateway")

    query = urllib.parse.urlencode({"q": "scale", "limit": 5})
    status_code, body, elapsed = _http_read(base + "/api/search?" + query)
    search = json.loads(body) if status_code == 200 else {}
    items = search.get("items") if isinstance(search, dict) else None
    checks["search"] = {
        "status": status_code,
        "elapsed_ms": elapsed,
        "generation_id": search.get("generation_id") if isinstance(search, dict) else None,
        "items": len(items) if isinstance(items, list) else None,
    }
    if status_code != 200 or not isinstance(items, list) or not items:
        raise RuntimeErrorEB("Experiment-B search failed through Gateway")

    status_code, body, elapsed = _http_read(base + "/api/auth/me")
    auth = json.loads(body) if status_code == 200 else {}
    checks["anonymous_auth_boundary"] = {
        "status": status_code,
        "elapsed_ms": elapsed,
        "authenticated": auth.get("authenticated") if isinstance(auth, dict) else None,
        "role": auth.get("role") if isinstance(auth, dict) else None,
    }
    if (
        status_code != 200
        or auth.get("authenticated") is not False
        or auth.get("role") != "gast"
    ):
        raise RuntimeErrorEB("Experiment-B anonymous auth boundary is not fail-closed")

    jetstream = _jetstream_signature(root)
    if jetstream["messages"] < 1:
        raise RuntimeErrorEB("Experiment-B JetStream contains no persisted test messages")
    receipt = {
        "schema_version": 1,
        "status": "pass",
        "source_commit": source_commit,
        "gateway": base,
        "checks": checks,
        "jetstream": jetstream,
        "production_endpoint_used": False,
    }
    atomic_json(receipt_path, receipt)
    _complete_live_check_attempt(
        attempt_path,
        receipt_path,
        source_commit,
        attempt_started_at_unix_ms,
        "pass",
    )
    return receipt


def _database_signature(root: Path) -> dict[str, Any]:
    sql = r"""
SELECT json_build_object(
  'nodes_count', (SELECT count(*) FROM domain_nodes),
  'nodes_md5', (
      SELECT md5(coalesce(string_agg(md5(to_jsonb(n)::text), '' ORDER BY n.id), ''))
      FROM domain_nodes n
  ),
  'edges_count', (SELECT count(*) FROM domain_edges),
  'edges_md5', (
      SELECT md5(coalesce(string_agg(md5(to_jsonb(e)::text), '' ORDER BY e.id), ''))
      FROM domain_edges e
  ),
  'outbox_count', (SELECT count(*) FROM domain_outbox),
  'outbox_md5', (
      SELECT md5(coalesce(string_agg(md5(to_jsonb(o)::text), '' ORDER BY o.id), ''))
      FROM domain_outbox o
  ),
  'event_consumptions_count', (SELECT count(*) FROM domain_event_consumptions),
  'event_consumptions_md5', (
      SELECT md5(coalesce(
          string_agg(md5(to_jsonb(c)::text), '' ORDER BY c.consumer_name, c.event_id),
          ''
      ))
      FROM domain_event_consumptions c
  ),
  'projection_state_count', (SELECT count(*) FROM domain_projection_state),
  'projection_state_md5', (
      SELECT md5(coalesce(
          string_agg(md5(to_jsonb(s)::text), '' ORDER BY s.singleton),
          ''
      ))
      FROM domain_projection_state s
  ),
  'search_versions_count', (SELECT count(*) FROM search_node_versions),
  'search_versions_md5', (
      SELECT md5(coalesce(string_agg(md5(to_jsonb(v)::text), '' ORDER BY v.node_id), ''))
      FROM search_node_versions v
  ),
  'search_generations_count', (SELECT count(*) FROM search_index_generations),
  'search_generations_md5', (
      SELECT md5(coalesce(string_agg(md5(to_jsonb(g)::text), '' ORDER BY g.generation_id), ''))
      FROM search_index_generations g
  ),
  'projection_count', (SELECT count(*) FROM search_node_projections),
  'projection_md5', (
      SELECT md5(coalesce(
          string_agg(
              md5(to_jsonb(p)::text),
              '' ORDER BY p.generation_id, p.node_id
          ),
          ''
      ))
      FROM search_node_projections p
  ),
  'projection_jobs_count', (SELECT count(*) FROM search_projection_jobs),
  'projection_jobs_md5', (
      SELECT md5(coalesce(
          string_agg(
              md5(to_jsonb(j)::text),
              '' ORDER BY j.generation_id, j.node_id, j.source_version, j.operation
          ),
          ''
      ))
      FROM search_projection_jobs j
  ),
  'active_generation', (
      SELECT generation_id
      FROM search_index_generations
      WHERE state='active'
      ORDER BY activated_at DESC NULLS LAST
      LIMIT 1
  )
)::text;
"""
    raw = _psql(root, sql)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeErrorEB("database continuity signature is not JSON") from exc
    if not isinstance(value, dict) or int(value.get("nodes_count", 0)) < 1:
        raise RuntimeErrorEB("database continuity signature is incomplete")
    return value


def _jetstream_sequence_progress(value: Any, context: str) -> dict[str, int]:
    if not isinstance(value, dict):
        raise RuntimeErrorEB(f"{context} is missing from JetStream monitoring output")
    try:
        return {
            "consumer_seq": int(value["consumer_seq"]),
            "stream_seq": int(value["stream_seq"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeErrorEB(f"{context} has an invalid JetStream sequence") from exc


def _jetstream_signature_from_monitoring(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeErrorEB("NATS JetStream monitoring output is not an object")
    accounts = value.get("account_details")
    if not isinstance(accounts, list):
        raise RuntimeErrorEB("NATS JetStream monitoring output has no account details")

    stream_signatures: list[dict[str, Any]] = []
    durable_consumer_count = 0
    for account in accounts:
        if not isinstance(account, dict):
            raise RuntimeErrorEB("NATS JetStream account detail is invalid")
        account_name = str(account.get("name") or account.get("id") or "")
        streams = account.get("stream_detail", [])
        if not isinstance(streams, list):
            raise RuntimeErrorEB("NATS JetStream stream detail is invalid")
        for stream in streams:
            if not isinstance(stream, dict):
                raise RuntimeErrorEB("NATS JetStream stream entry is invalid")
            stream_name = str(stream.get("name") or "")
            if not stream_name:
                raise RuntimeErrorEB("NATS JetStream stream detail has no name")
            state = stream.get("state")
            if not isinstance(state, dict):
                raise RuntimeErrorEB(f"NATS JetStream stream {stream_name!r} has no state")
            try:
                stream_state = {
                    "messages": int(state["messages"]),
                    "bytes": int(state["bytes"]),
                    "first_seq": int(state["first_seq"]),
                    "last_seq": int(state["last_seq"]),
                }
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeErrorEB(
                    f"NATS JetStream stream {stream_name!r} state is incomplete"
                ) from exc

            durable_consumers: list[dict[str, Any]] = []
            consumers = stream.get("consumer_detail", [])
            if not isinstance(consumers, list):
                raise RuntimeErrorEB(
                    f"NATS JetStream stream {stream_name!r} consumer detail is invalid"
                )
            for consumer in consumers:
                if not isinstance(consumer, dict):
                    raise RuntimeErrorEB("NATS JetStream consumer detail is invalid")
                config = consumer.get("config")
                if not isinstance(config, dict):
                    continue
                durable_name = config.get("durable_name")
                if not isinstance(durable_name, str) or not durable_name:
                    continue
                consumer_name = str(consumer.get("name") or "")
                consumer_stream = str(consumer.get("stream_name") or "")
                if not consumer_name or consumer_stream != stream_name:
                    raise RuntimeErrorEB(
                        "NATS durable consumer identity is incomplete or cross-stream"
                    )
                try:
                    num_ack_pending = int(consumer["num_ack_pending"])
                    num_redelivered = int(consumer["num_redelivered"])
                    num_pending = int(consumer["num_pending"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise RuntimeErrorEB(
                        f"NATS durable consumer {consumer_name!r} state is incomplete"
                    ) from exc
                durable_consumers.append(
                    {
                        "name": consumer_name,
                        "stream_name": consumer_stream,
                        "config": config,
                        "delivered": _jetstream_sequence_progress(
                            consumer.get("delivered"),
                            f"NATS durable consumer {consumer_name!r} delivered state",
                        ),
                        "ack_floor": _jetstream_sequence_progress(
                            consumer.get("ack_floor"),
                            f"NATS durable consumer {consumer_name!r} ack floor",
                        ),
                        "num_ack_pending": num_ack_pending,
                        "num_redelivered": num_redelivered,
                        "num_pending": num_pending,
                    }
                )
            durable_consumers.sort(
                key=lambda item: (str(item["stream_name"]), str(item["name"]))
            )
            durable_consumer_count += len(durable_consumers)
            stream_signatures.append(
                {
                    "account": account_name,
                    "name": stream_name,
                    "state": stream_state,
                    "durable_consumers": durable_consumers,
                }
            )

    stream_signatures.sort(key=lambda item: (str(item["account"]), str(item["name"])))
    try:
        result = {
            "streams": int(value["streams"]),
            "messages": int(value["messages"]),
            "bytes": int(value["bytes"]),
            "durable_consumers": durable_consumer_count,
            "stream_detail": stream_signatures,
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeErrorEB("NATS JetStream aggregate state is incomplete") from exc
    if result["streams"] < 1 or len(stream_signatures) != result["streams"]:
        raise RuntimeErrorEB("NATS JetStream stream detail does not cover all streams")
    if durable_consumer_count < 1:
        raise RuntimeErrorEB("NATS JetStream has no persisted durable consumer state")
    return result


def _jetstream_signature(root: Path) -> dict[str, Any]:
    raw = _kubectl(
        root,
        [
            "-n", DATA_NAMESPACE, "exec", "deployment/nats", "--",
            "/bin/sh", "-c",
            (
                "wget -qO- "
                "'http://127.0.0.1:8222/jsz?"
                "accounts=true&streams=true&consumers=true&config=true'"
            ),
        ],
    ).stdout
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeErrorEB("NATS JetStream monitoring output is not JSON") from exc
    return _jetstream_signature_from_monitoring(value)


def _scale_deployment(root: Path, namespace: str, name: str, replicas: int) -> None:
    _kubectl(
        root,
        [
            "-n", namespace, "scale", "deployment", name,
            f"--replicas={replicas}",
        ],
    )


def _wait_deployment(root: Path, namespace: str, name: str, timeout: str = "5m") -> None:
    _kubectl(
        root,
        [
            "-n", namespace, "rollout", "status", f"deployment/{name}",
            f"--timeout={timeout}",
        ],
        timeout=360,
    )


def _flux_suspend(root: Path, name: str) -> None:
    flux = toolchain(root)["tools"]["flux"]
    run(
        [flux, "suspend", "kustomization", name, "-n", "flux-system"],
        env=kube_env(root),
    )


def _flux_resume(root: Path, name: str) -> None:
    flux = toolchain(root)["tools"]["flux"]
    run(
        [flux, "resume", "kustomization", name, "-n", "flux-system"],
        env=kube_env(root),
    )
    run(
        [flux, "reconcile", "kustomization", name, "-n", "flux-system", "--with-source"],
        env=kube_env(root),
        timeout=600,
    )


def _wait_pods_absent(
    root: Path,
    namespace: str,
    selector: str,
    *,
    timeout_seconds: int = 180,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        pods = _kubectl_json(
            root,
            ["-n", namespace, "get", "pods", "-l", selector],
        )
        items = pods.get("items")
        if not isinstance(items, list):
            raise RuntimeErrorEB("pod absence readback is not a list")
        if not items:
            return
        time.sleep(2)
    raise RuntimeErrorEB(
        f"pods did not terminate before exclusive PVC access: {namespace} {selector}"
    )


def _nats_transfer_pod(root: Path, name: str) -> None:
    deployment = _kubectl_json(
        root, ["-n", DATA_NAMESPACE, "get", "deployment", "nats"]
    )
    containers = deployment.get("spec", {}).get("template", {}).get("spec", {}).get(
        "containers", []
    )
    image = next(
        (item.get("image") for item in containers if item.get("name") == "nats"),
        None,
    )
    if not isinstance(image, str) or "@sha256:" not in image:
        raise RuntimeErrorEB("NATS restore pod cannot bind the live immutable image")
    manifest = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": DATA_NAMESPACE},
        "spec": {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 1000,
                "runAsGroup": 1000,
                "fsGroup": 1000,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "transfer",
                    "image": image,
                    "command": ["/bin/sh", "-c", "sleep 3600"],
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "capabilities": {"drop": ["ALL"]},
                        "readOnlyRootFilesystem": True,
                    },
                    "volumeMounts": [
                        {"name": "data", "mountPath": "/data"},
                        {"name": "tmp", "mountPath": "/tmp"},
                    ],
                }
            ],
            "volumes": [
                {"name": "data", "persistentVolumeClaim": {"claimName": "nats-data"}},
                {"name": "tmp", "emptyDir": {}},
            ],
        },
    }
    kubectl_apply(root, json.dumps(manifest, sort_keys=True))
    _kubectl(
        root,
        [
            "-n", DATA_NAMESPACE, "wait", "--for=condition=Ready",
            f"pod/{name}", "--timeout=3m",
        ],
        timeout=210,
    )


def _delete_pod(root: Path, namespace: str, name: str) -> None:
    _kubectl(
        root,
        [
            "-n", namespace, "delete", "pod", name,
            "--ignore-not-found=true", "--wait=true", "--timeout=2m",
        ],
        check=False,
        timeout=150,
    )


def recovery_proof(root: Path) -> dict[str, Any]:
    backup_dir = root / "recovery"
    backup_dir.mkdir(parents=True, exist_ok=True)
    db_dump = backup_dir / "postgres.dump"
    nats_tar = backup_dir / "nats.tar"
    before_db: dict[str, Any] | None = None
    before_nats: dict[str, Any] | None = None
    release_path = root / "receipts/release.json"
    if not release_path.is_file():
        raise RuntimeErrorEB("recovery proof requires an applied release receipt")
    release = json.loads(release_path.read_text(encoding="utf-8"))
    source_commit = str(release.get("source_commit", ""))
    if not COMMIT_RE.fullmatch(source_commit):
        raise RuntimeErrorEB("recovery proof release binding is not exact")
    recovery_receipt = root / "receipts/recovery.json"
    recovery_failed_receipt = root / "receipts/recovery-failed.json"
    recovery_receipt.unlink(missing_ok=True)
    recovery_failed_receipt.unlink(missing_ok=True)

    _flux_suspend(root, "commonthing-experiment-b-app")
    _flux_suspend(root, "commonthing-experiment-b-data")
    destructive_started = time.monotonic()
    try:
        _scale_deployment(root, APP_NAMESPACE, "weltgewebe-api", 0)
        _scale_deployment(root, APP_NAMESPACE, "weltgewebe-web", 0)
        _wait_pods_absent(
            root,
            APP_NAMESPACE,
            "app.kubernetes.io/name=weltgewebe-api",
        )
        _wait_pods_absent(
            root,
            APP_NAMESPACE,
            "app.kubernetes.io/name=weltgewebe-web",
        )

        before_db = _database_signature(root)
        before_nats = _jetstream_signature(root)
        if before_nats["streams"] < 1 or before_nats["messages"] < 1:
            raise RuntimeErrorEB("JetStream test state is empty before recovery proof")

        kubectl = toolchain(root)["tools"]["kubectl"]
        _run_binary_to_file(
            [
                kubectl, "-n", DATA_NAMESPACE, "exec", "deployment/postgres", "--",
                "pg_dump", "-U", "commonthing", "-d", "commonthing", "-Fc",
            ],
            db_dump,
            env=kube_env(root),
            timeout=900,
        )

        _scale_deployment(root, DATA_NAMESPACE, "nats", 0)
        _wait_pods_absent(
            root,
            DATA_NAMESPACE,
            "app.kubernetes.io/name=nats",
        )
        _nats_transfer_pod(root, "commonthing-experiment-b-nats-backup")
        try:
            _run_binary_to_file(
                [
                    kubectl, "-n", DATA_NAMESPACE, "exec",
                    "commonthing-experiment-b-nats-backup", "--",
                    "tar", "-C", "/data", "-cf", "-", ".",
                ],
                nats_tar,
                env=kube_env(root),
                timeout=900,
            )
        finally:
            _delete_pod(
                root, DATA_NAMESPACE, "commonthing-experiment-b-nats-backup"
            )

        _scale_deployment(root, DATA_NAMESPACE, "postgres", 0)
        _wait_pods_absent(
            root,
            DATA_NAMESPACE,
            "app.kubernetes.io/name=postgres",
        )
        _kubectl(
            root,
            [
                "-n", DATA_NAMESPACE, "delete", "pvc",
                "postgres-data", "nats-data", "--wait=true", "--timeout=5m",
            ],
            timeout=330,
        )
        storage = (CLUSTER / "data/storage.yaml").read_text(encoding="utf-8")
        kubectl_apply(root, storage)

        _nats_transfer_pod(root, "commonthing-experiment-b-nats-restore")
        try:
            _run_input_file(
                [
                    kubectl, "-n", DATA_NAMESPACE, "exec", "-i",
                    "commonthing-experiment-b-nats-restore", "--",
                    "tar", "-C", "/data", "-xf", "-",
                ],
                nats_tar,
                env=kube_env(root),
                timeout=900,
            )
        finally:
            _delete_pod(
                root, DATA_NAMESPACE, "commonthing-experiment-b-nats-restore"
            )

        _scale_deployment(root, DATA_NAMESPACE, "postgres", 1)
        _wait_deployment(root, DATA_NAMESPACE, "postgres", "5m")
        _run_input_file(
            [
                kubectl, "-n", DATA_NAMESPACE, "exec", "-i",
                "deployment/postgres", "--",
                "pg_restore", "-U", "commonthing", "-d", "commonthing",
                "--clean", "--if-exists", "--no-owner",
            ],
            db_dump,
            env=kube_env(root),
            timeout=1200,
        )
        _scale_deployment(root, DATA_NAMESPACE, "nats", 1)
        _wait_deployment(root, DATA_NAMESPACE, "nats", "5m")

        after_db = _database_signature(root)
        after_nats = _jetstream_signature(root)
        if after_db != before_db:
            raise RuntimeErrorEB("PostgreSQL/search signature changed across delete-to-prove")
        if after_nats != before_nats:
            raise RuntimeErrorEB(
                "JetStream stream/durable-consumer continuity signature changed across restore"
            )
        _flux_resume(root, "commonthing-experiment-b-data")
        _flux_resume(root, "commonthing-experiment-b-app")
        _wait_deployment(root, APP_NAMESPACE, "weltgewebe-api", "8m")
        _wait_deployment(root, APP_NAMESPACE, "weltgewebe-web", "5m")
        rto_seconds = time.monotonic() - destructive_started
    except Exception:
        resuspended: dict[str, bool] = {}
        for name in (
            "commonthing-experiment-b-app",
            "commonthing-experiment-b-data",
        ):
            try:
                _flux_suspend(root, name)
                resuspended[name] = True
            except Exception:
                resuspended[name] = False
        atomic_json(
            recovery_failed_receipt,
            {
                "schema_version": 1,
                "status": "failed",
                "source_commit": source_commit,
                "database_before": before_db,
                "jetstream_before": before_nats,
                "flux_resuspended": resuspended,
            },
        )
        raise
    if before_db is None or before_nats is None:
        raise RuntimeErrorEB("recovery proof has no quiesced before-signature")
    receipt = {
        "schema_version": 1,
        "status": "pass",
        "source_commit": source_commit,
        "rpo_seconds": 0,
        "rto_seconds": round(rto_seconds, 3),
        "postgres_dump_sha256": sha256_file(db_dump),
        "nats_backup_sha256": sha256_file(nats_tar),
        "database_before": before_db,
        "database_after": after_db,
        "jetstream_before": before_nats,
        "jetstream_after": after_nats,
        "pvc_delete_to_prove": True,
        "production_data_used": False,
    }
    atomic_json(recovery_receipt, receipt)
    return receipt


def portability_report(root: Path) -> dict[str, Any]:
    recovery_failed_receipt = root / "receipts/recovery-failed.json"
    if recovery_failed_receipt.is_file():
        raise RuntimeErrorEB(
            "portability report is blocked by the latest failed recovery attempt"
        )
    expected_status = {
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
    payloads: dict[str, dict[str, Any]] = {}
    receipts: dict[str, str] = {}
    for name, required_status in expected_status.items():
        path = root / "receipts" / name
        if not path.is_file():
            raise RuntimeErrorEB(f"portability report is missing receipt: {name}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeErrorEB(
                f"portability receipt is not valid JSON: {name}"
            ) from exc
        if not isinstance(payload, dict) or payload.get("status") != required_status:
            raise RuntimeErrorEB(
                f"portability receipt does not prove success: {name}"
            )
        payloads[name] = payload
        receipts[name] = sha256_file(path)

    source_commit = str(payloads["release.json"].get("source_commit", ""))
    if not COMMIT_RE.fullmatch(source_commit):
        raise RuntimeErrorEB("release receipt has no exact source commit")
    for name in (
        "release-attempt.json",
        "t048-fixture.json",
        "semantic-search.json",
        "semantic-search-attempt.json",
        "functional-readback.json",
        "functional-readback-attempt.json",
        "t048-load.json",
        "t048-load-attempt.json",
        "recovery.json",
        "status.json",
        "status-attempt.json",
    ):
        if payloads[name].get("source_commit") != source_commit:
            raise RuntimeErrorEB(
                f"portability receipt source binding drifted: {name}"
            )
    for receipt_stem in (
        "release",
        "semantic-search",
        "functional-readback",
        "t048-load",
        "status",
    ):
        attempt_name = f"{receipt_stem}-attempt.json"
        receipt_name = f"{receipt_stem}.json"
        attempt = payloads[attempt_name]
        if (
            attempt.get("receipt") != receipt_name
            or attempt.get("receipt_sha256") != receipts[receipt_name]
        ):
            raise RuntimeErrorEB(
                f"latest {receipt_stem} attempt is not bound to its current success receipt"
            )

    result = {
        "schema_version": 1,
        "status": "pass",
        "source_commit": source_commit,
        "receipts": receipts,
        "portable_invariants": [
            "exact protected-main Git commit",
            "immutable GHCR API/Web digests",
            "external registry/database/runtime secrets",
            "Cilium Gateway API and kube-proxy replacement",
            "Flux/Kustomize reconciliation",
            "persistent PostgreSQL and JetStream state",
            "literal-loopback Ollama provider contract",
        ],
        "removed_staging_specific_mechanisms": [
            "kind runtime",
            "staging_cell.py runtime controller",
            "static kind hostPath PVs",
            "kind worker nodeSelector and nodeAffinity",
        ],
        "experiment_b_specific_mechanisms": [
            "single libvirt/KVM Ubuntu VM",
            "k3s local-path storage",
            "temporary model-download egress",
            "host-side pinned k6 load generator",
            "synthetic canonical-T048 search projections",
        ],
        "production_green_open_questions": [
            "multi-node failure-domain and HA behavior",
            "production-grade storage class and backup backend",
            "production ingress/DNS/TLS ownership",
            "production capacity beyond Experiment-B calibration",
        ],
        "does_not_establish": [
            "production Kubernetes cutover",
            "production capacity",
            "independent physical failure domains",
        ],
    }
    atomic_json(root / "receipts/portability.json", result)
    return result


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument("--state-root")
    sub = p.add_subparsers(dest="command", required=True)
    pre = sub.add_parser("preflight")
    pre.add_argument("--source-commit")
    sub.add_parser("prepare")
    sub.add_parser("create-vm")
    sub.add_parser("install-k3s")
    sub.add_parser("install-platform")
    sec = sub.add_parser("inject-secrets")
    sec.add_argument("--registry-config", required=True)
    rel = sub.add_parser("apply-release")
    rel.add_argument("--source-commit", required=True)
    rel.add_argument("--api-digest", required=True)
    rel.add_argument("--web-digest", required=True)
    sub.add_parser("seed-t048-fixture")
    sub.add_parser("semantic-activate")
    functional = sub.add_parser("functional-readback")
    functional.add_argument("--source-commit", required=True)
    performance = sub.add_parser("t048-load-proof")
    performance.add_argument("--source-commit", required=True)
    sub.add_parser("recovery-proof")
    sub.add_parser("portability-report")
    sub.add_parser("status")
    sub.add_parser("teardown")
    return p


def main() -> int:
    args = parser().parse_args()
    root = state_root(args.state_root)
    if args.command == "preflight":
        result = preflight(args.source_commit)
    elif args.command == "prepare":
        result = prepare(root)
    elif args.command == "create-vm":
        result = create_vm(root)
    elif args.command == "install-k3s":
        result = install_k3s(root)
    elif args.command == "install-platform":
        result = install_platform(root)
    elif args.command == "inject-secrets":
        inject_secrets(
            root, Path(args.registry_config).expanduser().resolve()
        )
        result = {
            "schema_version": 1,
            "status": "ready",
            "receipt": "receipts/secrets.json",
        }
    elif args.command == "apply-release":
        result = apply_release(
            root, args.source_commit, args.api_digest, args.web_digest
        )
    elif args.command == "seed-t048-fixture":
        result = seed_t048_fixture(root)
    elif args.command == "semantic-activate":
        result = semantic_activate(root)
    elif args.command == "functional-readback":
        result = functional_readback(root, args.source_commit)
    elif args.command == "t048-load-proof":
        result = t048_load_proof(root, args.source_commit)
    elif args.command == "recovery-proof":
        result = recovery_proof(root)
    elif args.command == "portability-report":
        result = portability_report(root)
    elif args.command == "status":
        result = status(root)
    elif args.command == "teardown":
        result = teardown(root)
    else:
        raise RuntimeErrorEB(f"unsupported command: {args.command}")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeErrorEB, contract.ContractError) as exc:
        print(json.dumps({"status": "error", "error_class": type(exc).__name__}, sort_keys=True))
        raise SystemExit(2)
