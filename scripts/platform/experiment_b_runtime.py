#!/usr/bin/env python3
"""Bounded lifecycle driver for WELTGEWEBE-OS-V1-T085 Experiment B.

The driver owns only the temporary libvirt VM commonthing-experiment-b and
state below ~/.local/state/commonthing/experiment-b. It never targets production
DNS, production data, or a production Kubernetes context.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import csv
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
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import yaml

import bootstrap_tools
import experiment_b as contract

ROOT = Path(__file__).resolve().parents[2]
CLUSTER = ROOT / "platform/clusters/experiment-b"
NAMESPACES = CLUSTER / "namespaces"
MIGRATION = CLUSTER / "migration"
APP_OVERLAY = ROOT / "platform/apps/weltgewebe/overlays/experiment-b"
DEFAULT_STATE_ROOT = Path.home() / ".local/state/commonthing/experiment-b"
VM_NAME = "commonthing-experiment-b"
LIBVIRT_URI = "qemu:///system"
POOL_NAME = "commonthing-experiment-b-pool"
POOL_TARGET = Path("/var/tmp/commonthing-experiment-b-libvirt")
BASE_VOLUME = "commonthing-experiment-b-base.qcow2"
VOLUME_NAME = "commonthing-experiment-b.qcow2"
APP_NAMESPACE = "commonthing-experiment-b"
DATA_NAMESPACE = "commonthing-data"
EXPECTED_PVCS = frozenset(
    {
        f"{DATA_NAMESPACE}/postgres-data",
        f"{DATA_NAMESPACE}/nats-data",
        f"{APP_NAMESPACE}/ollama-models",
    }
)
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
PERFORMANCE_POLICY = ROOT / "policies/performance.v1.json"
DOMAIN_SCALE = ROOT / "scripts/performance/domain_scale.py"
DOMAIN_SCALE_CONFIG = ROOT / "configs/performance/domain-scale.v1.json"
K6_WORKFLOW = ROOT / ".github/workflows/domain-scale.yml"
K6_WORKLOAD = ROOT / "scripts/performance/api_runtime_k6.js"
RETIREMENT_RECEIPT = Path.home() / ".local/state/commonthing/experiment-b-retirement.json"
T048_DOCUMENT_REVISION = "node-document-v4-canonical-visibility"
T048_NORMALIZATION_REVISION = "weltgewebe-search-normalization-v1"
T048_RANKING_REVISION = "weltgewebe-hybrid-ranking-v2"
T048_PUBLIC_CONTENT_SHA256 = "0" * 64
T048_HIDDEN_CONTENT_SHA256 = "e0f631f5602e764ef8a5f14e36d2d81663b20cd305a30af0dad6c0d759e5a955"
T048_REDACTED_TEXT = "[nicht öffentlich]"


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


def atomic_bytes(path: Path, payload: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
        mode="wb",
    ) as handle:
        tmp = Path(handle.name)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


PORTABILITY_DERIVED_RECEIPTS = ("portability.json",)
RECOVERY_ATTEMPT_INVALIDATES = (
    "recovery.json",
    "recovery-attempt.json",
    "recovery-failed.json",
    "status.json",
    "status-attempt.json",
    "portability.json",
)
FIXTURE_ATTEMPT_INVALIDATES = (
    "t048-fixture.json",
    "functional-readback.json",
    "functional-readback-attempt.json",
    "t048-load.json",
    "t048-load-attempt.json",
    "recovery.json",
    "recovery-attempt.json",
    "recovery-failed.json",
    "status.json",
    "status-attempt.json",
    "portability.json",
)


def _invalidate_receipts(root: Path, names: tuple[str, ...]) -> None:
    for name in names:
        (root / "receipts" / name).unlink(missing_ok=True)


def _begin_live_check_attempt(
    root: Path,
    receipt_stem: str,
    source_commit: str,
) -> tuple[Path, Path, int]:
    if not COMMIT_RE.fullmatch(source_commit):
        raise RuntimeErrorEB(f"{receipt_stem} attempt source commit is not exact")
    _invalidate_receipts(root, PORTABILITY_DERIVED_RECEIPTS)
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


def _require_recovery_attempt_clear(root: Path) -> None:
    attempt_path = root / "receipts/recovery-attempt.json"
    if not attempt_path.is_file():
        return
    try:
        attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeErrorEB(
            "recovery proof refuses an unreadable previous attempt; rebuild the "
            "Experiment-B cell to establish a fresh baseline"
        ) from exc
    if not isinstance(attempt, dict) or attempt.get("schema_version") != 1:
        raise RuntimeErrorEB(
            "recovery proof refuses an invalid previous attempt; rebuild the "
            "Experiment-B cell to establish a fresh baseline"
        )
    status = attempt.get("status")
    if status in {"running", "failed"}:
        raise RuntimeErrorEB(
            "recovery proof refuses a retry after an incomplete or failed attempt; "
            "rebuild the Experiment-B cell to establish a fresh baseline"
        )
    if status != "pass":
        raise RuntimeErrorEB(
            "recovery proof refuses an unknown previous attempt state; rebuild the "
            "Experiment-B cell to establish a fresh baseline"
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
    "recovery-attempt.json",
    "recovery-failed.json",
    "status.json",
    "status-attempt.json",
    "portability.json",
)


SECRETS_ATTEMPT_INVALIDATES = (
    "secrets.json",
    "release.json",
    "release-attempt.json",
    *RELEASE_DEPENDENT_RECEIPTS,
)
PLATFORM_ATTEMPT_INVALIDATES = (
    "platform.json",
    *SECRETS_ATTEMPT_INVALIDATES,
)
K3S_ATTEMPT_INVALIDATES = (
    "k3s.json",
    *PLATFORM_ATTEMPT_INVALIDATES,
)
VM_ATTEMPT_INVALIDATES = (
    "vm-create.json",
    "vm-create-attempt.json",
    *K3S_ATTEMPT_INVALIDATES,
)


def _begin_release_attempt(
    root: Path,
    source_commit: str,
) -> tuple[Path, Path, int]:
    receipt_path, attempt_path, started_at_unix_ms = _begin_live_check_attempt(
        root, "release", source_commit
    )
    _invalidate_receipts(root, RELEASE_DEPENDENT_RECEIPTS)
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


def _current_protected_main_commit() -> str:
    head = git_head()
    if not COMMIT_RE.fullmatch(head):
        raise RuntimeErrorEB("checkout HEAD is not an exact lowercase commit SHA")
    current_main = remote_main()
    if current_main != head:
        raise RuntimeErrorEB("checkout HEAD is no longer current protected main")
    if run(["git", "status", "--porcelain"]).stdout.strip():
        raise RuntimeErrorEB("checkout must be clean to bind current protected main")
    return head


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


def _libvirt_xml(*arguments: str) -> ET.Element:
    return ET.fromstring(run(["virsh", "-c", LIBVIRT_URI, *arguments]).stdout)


def _xml_bytes(element: ET.Element) -> int:
    units = {"bytes": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}
    return int(element.text or "0") * units[element.get("unit", "KiB")]


def _vm_definition(domain: ET.Element) -> dict[str, Any]:
    disks = domain.findall("./devices/disk[@device='disk']")
    interfaces = domain.findall("./devices/interface")
    if (
        len(disks) != 1 or len(interfaces) != 1
        or domain.findall("./devices/filesystem")
        or domain.findall("./devices/hostdev")
    ):
        raise RuntimeErrorEB("VM substrate requires one disk/NAT interface and no host mounts")
    disk, interface = disks[0], interfaces[0]
    source = disk.find("source")
    if disk.get("type") == "volume" and (
        source.get("pool") == POOL_NAME and source.get("volume") == VOLUME_NAME
    ):
        disk_path = str(POOL_TARGET / VOLUME_NAME)
    elif disk.get("type") == "file":
        disk_path = source.get("file")
    else:
        raise RuntimeErrorEB("VM substrate disk is not the Experiment-B volume")
    backing = disk.find("backingStore")
    if backing is not None and (
        backing.get("type") != "file"
        or backing.find("source").get("file") != str(POOL_TARGET / BASE_VOLUME)
        or backing.find("format").get("type") != "qcow2"
        or backing.find("backingStore/source") is not None
    ):
        raise RuntimeErrorEB("VM substrate libvirt backing relation drifted")
    vcpu = domain.find("vcpu")
    return {
        "vm": domain.findtext("name"),
        "uuid": domain.findtext("uuid"),
        "hypervisor": domain.get("type"),
        "vcpu": int(vcpu.text or "0"),
        "current_vcpu": int(vcpu.get("current", vcpu.text or "0")),
        "memory_bytes": _xml_bytes(domain.find("memory")),
        "current_memory_bytes": _xml_bytes(domain.find("currentMemory")),
        "interface_type": interface.get("type"),
        "network": interface.find("source").get("network"),
        "mac": interface.find("mac").get("address"),
        "disk_path": disk_path,
        "disk_format": disk.find("driver").get("type"),
        "disk_target": disk.find("target").get("dev"),
        "disk_bus": disk.find("target").get("bus"),
    }


def _validate_vm_substrate(substrate: Any, config: dict[str, Any]) -> None:
    vm = config["vm"]
    expected = {
        "vm": VM_NAME,
        "hypervisor": "kvm",
        "vcpu": vm["vcpu"],
        "current_vcpu": vm["vcpu"],
        "memory_bytes": vm["memory_mib"] * 1024**2,
        "current_memory_bytes": vm["memory_mib"] * 1024**2,
        "interface_type": "network",
        "network": vm["network"],
        "network_mode": "nat",
        "pool": POOL_NAME,
        "pool_type": "dir",
        "pool_target": str(POOL_TARGET),
        "volume": VOLUME_NAME,
        "disk_path": str(POOL_TARGET / VOLUME_NAME),
        "disk_key": str(POOL_TARGET / VOLUME_NAME),
        "disk_format": "qcow2",
        "disk_target": "vda",
        "disk_bus": "virtio",
        "disk_capacity_bytes": vm["disk_gib"] * 1024**3,
        "base_volume": BASE_VOLUME,
        "base_path": str(POOL_TARGET / BASE_VOLUME),
        "base_key": str(POOL_TARGET / BASE_VOLUME),
        "base_format": "qcow2",
        "base_image_sha256": vm["image"]["sha256"],
    }
    if not isinstance(substrate, dict):
        raise RuntimeErrorEB("VM substrate evidence is missing")
    for key, value in expected.items():
        if substrate.get(key) != value or type(substrate[key]) is not type(value):
            raise RuntimeErrorEB(f"VM substrate contract drifted: {key}")
    try:
        for key in ("uuid", "pool_uuid", "network_uuid"):
            if str(uuid.UUID(substrate[key])) != substrate[key]:
                raise ValueError(key)
        if not re.fullmatch(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}", substrate["mac"]):
            raise ValueError("mac")
        if not isinstance(substrate["network_bridge"], str) or not substrate["network_bridge"]:
            raise ValueError("network_bridge")
        for key in ("disk_device", "disk_inode"):
            if type(substrate[key]) is not int or substrate[key] < 0:
                raise ValueError(key)
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise RuntimeErrorEB("VM substrate identity is missing or invalid") from exc


def _live_vm_substrate(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Read the active guest and its actual QEMU backing chain, never cached prepare data."""
    try:
        domain = _libvirt_xml("dumpxml", VM_NAME)
        if int(domain.get("id", "-1")) < 0:
            raise RuntimeErrorEB("VM substrate domain is not active")
        substrate = _vm_definition(domain)
        if _vm_definition(_libvirt_xml("dumpxml", VM_NAME, "--inactive")) != substrate:
            raise RuntimeErrorEB("VM substrate live/persistent definitions differ")
        network = _libvirt_xml("net-dumpxml", config["vm"]["network"])
        network_bridge = network.find("bridge").get("name")
        if (
            network.findtext("name") != substrate["network"]
            or domain.find("./devices/interface/source").get("bridge") != network_bridge
        ):
            raise RuntimeErrorEB("VM substrate network attachment drifted")
        pool = _libvirt_xml("pool-dumpxml", POOL_NAME)
        disk = _libvirt_xml("vol-dumpxml", VOLUME_NAME, "--pool", POOL_NAME)
        base = _libvirt_xml("vol-dumpxml", BASE_VOLUME, "--pool", POOL_NAME)
        disk_path, base_path = str(POOL_TARGET / VOLUME_NAME), str(POOL_TARGET / BASE_VOLUME)
        if (
            disk.findtext("target/path") != disk_path
            or disk.findtext("backingStore/path") != base_path
            or disk.find("target/format").get("type") != "qcow2"
            or disk.find("backingStore/format").get("type") != "qcow2"
            or base.findtext("target/path") != base_path
            or base.find("backingStore/path") is not None
        ):
            raise RuntimeErrorEB("VM substrate volume/backing relation drifted")
        # QMP observes the running disk graph even when the host user cannot open
        # libvirt-owned images. It also detects live resize/backing-store overrides.
        blocks = json.loads(run([
            "virsh", "-c", LIBVIRT_URI, "qemu-monitor-command", VM_NAME,
            '{"execute":"query-block"}',
        ]).stdout)["return"]
        images = [
            block["inserted"]["image"] for block in blocks
            if not block.get("removable", False)
        ]
        if len(images) != 1:
            raise RuntimeErrorEB("VM substrate QEMU disk set drifted")
        image = images[0]
        backing = image.get("backing-image", {})
        if (
            image.get("filename") != disk_path or image.get("format") != "qcow2"
            or image.get("virtual-size") != config["vm"]["disk_gib"] * 1024**3
            or backing.get("filename") != base_path or backing.get("format") != "qcow2"
            or backing.get("backing-image") or backing.get("backing-filename")
        ):
            raise RuntimeErrorEB("VM substrate QEMU capacity/backing relation drifted")
        # Hash the uploaded base volume itself through libvirt, not the download
        # cache or the guest's mutable overlay; volume permissions stay unchanged.
        with tempfile.TemporaryDirectory(dir=root, prefix=".vm-substrate-") as tmp:
            downloaded_base = Path(tmp) / BASE_VOLUME
            run([
                "virsh", "-c", LIBVIRT_URI, "vol-download", BASE_VOLUME,
                str(downloaded_base), "--pool", POOL_NAME, "--sparse",
            ])
            base_sha256 = sha256_file(downloaded_base)
        disk_stat = (POOL_TARGET / VOLUME_NAME).stat()
        substrate.update({
            "network_uuid": network.findtext("uuid"),
            "network_mode": network.find("forward").get("mode"),
            "network_bridge": network_bridge,
            "pool": pool.findtext("name"),
            "pool_uuid": pool.findtext("uuid"),
            "pool_type": pool.get("type"),
            "pool_target": pool.findtext("target/path"),
            "volume": disk.findtext("name"),
            "disk_key": disk.findtext("key"),
            "disk_capacity_bytes": _xml_bytes(disk.find("capacity")),
            "disk_device": disk_stat.st_dev,
            "disk_inode": disk_stat.st_ino,
            "base_volume": base.findtext("name"),
            "base_key": base.findtext("key"),
            "base_path": base.findtext("target/path"),
            "base_format": base.find("target/format").get("type"),
            "base_image_sha256": base_sha256,
        })
    except (ET.ParseError, ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
        raise RuntimeErrorEB("VM substrate readback is missing or malformed") from exc
    _validate_vm_substrate(substrate, config)
    return substrate


def _require_vm_create_receipt(
    receipt: Any, source_commit: str, config: dict[str, Any], root: Path
) -> None:
    if not isinstance(receipt, dict) or (
        receipt.get("schema_version") != 1
        or receipt.get("status") != "created"
        or receipt.get("source_commit") != source_commit
        or receipt.get("state_root") != str(root.resolve())
        or receipt.get("config_sha256") != sha256_file(CLUSTER / "config.json")
        or receipt.get("vm") != VM_NAME
        or receipt.get("pool") != POOL_NAME
        or receipt.get("volume") != VOLUME_NAME
        or receipt.get("network") != config["vm"]["network"]
    ):
        raise RuntimeErrorEB("VM creation receipt source/config binding drifted")
    _validate_vm_substrate(receipt.get("substrate"), config)


def create_vm(root: Path) -> dict[str, Any]:
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

    RETIREMENT_RECEIPT.unlink(missing_ok=True)
    _invalidate_receipts(root, VM_ATTEMPT_INVALIDATES)
    source_commit = _current_protected_main_commit()
    config = load_config()
    config_sha256 = sha256_file(CLUSTER / "config.json")
    root_identity = str(root.resolve())
    atomic_json(
        root / "receipts/vm-create-attempt.json",
        {
            "schema_version": 1,
            "status": "running",
            "source_commit": source_commit,
            "config_sha256": config_sha256,
            "state_root": root_identity,
            "vm": VM_NAME,
            "pool": POOL_NAME,
        },
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
        substrate = _live_vm_substrate(root, config)
        if (
            _current_protected_main_commit() != source_commit
            or sha256_file(CLUSTER / "config.json") != config_sha256
        ):
            raise RuntimeErrorEB("VM creation source/config changed during creation")
        receipt = {
            "schema_version": 1,
            "status": "created",
            "source_commit": source_commit,
            "state_root": root_identity,
            "config_sha256": config_sha256,
            "vm": VM_NAME,
            "pool": POOL_NAME,
            "volume": VOLUME_NAME,
            "network": config["vm"]["network"],
            "substrate": substrate,
        }
        atomic_json(root / "receipts/vm-create.json", receipt)
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


def _k3s_contract_paths(config: dict[str, Any]) -> tuple[Path, Path]:
    binding = config.get("runtime_binding", {})
    if not isinstance(binding, dict):
        raise RuntimeErrorEB("Experiment-B runtime binding is invalid")
    config_value = binding.get("k3s_config")
    service_value = binding.get("k3s_service")
    if not isinstance(config_value, str) or not isinstance(service_value, str):
        raise RuntimeErrorEB("Experiment-B k3s file binding is invalid")
    config_path = (ROOT / config_value).resolve()
    service_path = (ROOT / service_value).resolve()
    if (
        config_path != (CLUSTER / "k3s-config.yaml").resolve()
        or service_path != (CLUSTER / "k3s.service").resolve()
        or not config_path.is_file()
        or not service_path.is_file()
    ):
        raise RuntimeErrorEB("Experiment-B k3s file binding drifted")
    return config_path, service_path


def _kubeconfig_server(path: Path) -> str:
    if not path.is_file() or path.is_symlink():
        raise RuntimeErrorEB("Experiment-B kubeconfig must be a regular file")
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise RuntimeErrorEB("Experiment-B kubeconfig is invalid") from exc
    if not isinstance(payload, dict):
        raise RuntimeErrorEB("Experiment-B kubeconfig is invalid")
    current = payload.get("current-context")
    contexts = payload.get("contexts")
    clusters = payload.get("clusters")
    if (
        not isinstance(current, str)
        or not current
        or not isinstance(contexts, list)
        or not isinstance(clusters, list)
    ):
        raise RuntimeErrorEB("Experiment-B kubeconfig context binding is invalid")
    matching_contexts = [
        item
        for item in contexts
        if isinstance(item, dict) and item.get("name") == current
    ]
    if len(matching_contexts) != 1:
        raise RuntimeErrorEB("Experiment-B kubeconfig current context is ambiguous")
    context_value = matching_contexts[0].get("context")
    cluster_name = (
        context_value.get("cluster")
        if isinstance(context_value, dict)
        else None
    )
    matching_clusters = [
        item
        for item in clusters
        if isinstance(item, dict) and item.get("name") == cluster_name
    ]
    if len(matching_clusters) != 1:
        raise RuntimeErrorEB("Experiment-B kubeconfig cluster binding is ambiguous")
    cluster_value = matching_clusters[0].get("cluster")
    server = (
        cluster_value.get("server")
        if isinstance(cluster_value, dict)
        else None
    )
    if not isinstance(server, str) or not server:
        raise RuntimeErrorEB("Experiment-B kubeconfig server binding is invalid")
    return server


def _require_kubernetes_target_binding(
    root: Path,
    source_commit: str | None = None,
) -> tuple[dict[str, Any], str, str]:
    receipt_path = root / "receipts/k3s.json"
    kubeconfig_path = root / "kubeconfig.yaml"
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeErrorEB(
            "Experiment-B Kubernetes target binding requires valid k3s.json"
        ) from exc
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema_version") != 1
        or receipt.get("status") != "ready"
        or (
            source_commit is not None
            and receipt.get("source_commit") != source_commit
        )
        or not isinstance(receipt.get("vm_ip"), str)
        or not receipt.get("vm_ip")
        or not isinstance(receipt.get("kubeconfig_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", receipt["kubeconfig_sha256"]) is None
    ):
        raise RuntimeErrorEB("Experiment-B Kubernetes target receipt binding drifted")

    live_ip = vm_ip()
    if receipt.get("vm_ip") != live_ip:
        raise RuntimeErrorEB("Experiment-B k3s VM address drifted")
    if (
        not kubeconfig_path.is_file()
        or kubeconfig_path.is_symlink()
        or sha256_file(kubeconfig_path) != receipt["kubeconfig_sha256"]
        or (kubeconfig_path.stat().st_mode & 0o777) != 0o600
    ):
        raise RuntimeErrorEB("Experiment-B kubeconfig digest/mode drifted")
    expected_server = f"https://{live_ip}:6443"
    if _kubeconfig_server(kubeconfig_path) != expected_server:
        raise RuntimeErrorEB(
            "Experiment-B kubeconfig is not bound to the VM API server"
        )
    return receipt, live_ip, expected_server


def _parse_sha256sum_output(
    stdout: str, expected_paths: tuple[str, ...]
) -> dict[str, str]:
    observed: dict[str, str] = {}
    for raw_line in stdout.splitlines():
        fields = raw_line.strip().split(maxsplit=1)
        if len(fields) != 2:
            raise RuntimeErrorEB("guest k3s file digest output is malformed")
        digest, path = fields
        path = path.lstrip("*")
        if (
            re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or path not in expected_paths
            or path in observed
        ):
            raise RuntimeErrorEB("guest k3s file digest output is invalid")
        observed[path] = digest
    if set(observed) != set(expected_paths):
        raise RuntimeErrorEB("guest k3s file digest set is incomplete")
    return observed


def _parse_systemctl_properties(stdout: str) -> dict[str, str]:
    properties: dict[str, str] = {}
    for raw_line in stdout.splitlines():
        if "=" not in raw_line:
            raise RuntimeErrorEB("k3s systemd readback is malformed")
        key, value = raw_line.split("=", 1)
        if not key or key in properties:
            raise RuntimeErrorEB("k3s systemd readback is invalid")
        properties[key] = value
    expected = {
        "LoadState",
        "ActiveState",
        "SubState",
        "UnitFileState",
        "FragmentPath",
        "DropInPaths",
        "MainPID",
    }
    if set(properties) != expected:
        raise RuntimeErrorEB("k3s systemd readback is incomplete")
    return properties


def _require_live_k3s_runtime(
    root: Path,
    config: dict[str, Any],
    source_commit: str,
) -> dict[str, Any]:
    receipt, live_ip, expected_server = _require_kubernetes_target_binding(
        root, source_commit
    )
    config_path, service_path = _k3s_contract_paths(config)
    expected_binary_sha256 = str(config["kubernetes"]["binary_sha256"])
    expected_config_sha256 = sha256_file(config_path)
    expected_service_sha256 = sha256_file(service_path)
    if (
        receipt.get("binary_sha256") != expected_binary_sha256
        or receipt.get("config_sha256") != expected_config_sha256
        or receipt.get("service_sha256") != expected_service_sha256
        or str(config["kubernetes"]["version"])
        not in str(receipt.get("k3s_version", ""))
        or receipt.get("live_kubelet_version")
        != str(config["kubernetes"]["version"])
    ):
        raise RuntimeErrorEB("Experiment-B k3s receipt binding drifted")

    guest_paths = (
        "/usr/local/bin/k3s",
        "/etc/rancher/k3s/config.yaml",
        "/etc/systemd/system/k3s.service",
    )
    digest_result = run(
        [
            *ssh_argv(root, live_ip),
            "sudo",
            "sha256sum",
            "--",
            *guest_paths,
        ],
        timeout=30,
    )
    guest_digests = _parse_sha256sum_output(
        digest_result.stdout, guest_paths
    )
    expected_guest_digests = {
        "/usr/local/bin/k3s": expected_binary_sha256,
        "/etc/rancher/k3s/config.yaml": expected_config_sha256,
        "/etc/systemd/system/k3s.service": expected_service_sha256,
    }
    if guest_digests != expected_guest_digests:
        raise RuntimeErrorEB("installed k3s files drifted from the pinned contract")

    systemctl = run(
        [
            *ssh_argv(root, live_ip),
            "sudo",
            "systemctl",
            "show",
            "k3s.service",
            "--no-pager",
            "--property=LoadState",
            "--property=ActiveState",
            "--property=SubState",
            "--property=UnitFileState",
            "--property=FragmentPath",
            "--property=DropInPaths",
            "--property=MainPID",
        ],
        timeout=30,
    )
    properties = _parse_systemctl_properties(systemctl.stdout)
    try:
        main_pid = int(properties["MainPID"])
    except (TypeError, ValueError) as exc:
        raise RuntimeErrorEB("k3s systemd MainPID is invalid") from exc
    if (
        properties["LoadState"] != "loaded"
        or properties["ActiveState"] != "active"
        or properties["SubState"] != "running"
        or properties["UnitFileState"] != "enabled"
        or properties["FragmentPath"] != "/etc/systemd/system/k3s.service"
        or properties["DropInPaths"] != ""
        or main_pid <= 0
    ):
        raise RuntimeErrorEB("k3s systemd service is not the pinned active unit")
    process_exe = run(
        [
            *ssh_argv(root, live_ip),
            "sudo",
            "readlink",
            "-f",
            f"/proc/{main_pid}/exe",
        ],
        timeout=30,
    ).stdout.strip()
    process_cmdline = run(
        [
            *ssh_argv(root, live_ip),
            "sudo",
            "cat",
            f"/proc/{main_pid}/cmdline",
        ],
        timeout=30,
    ).stdout
    argv = [value for value in process_cmdline.split("\0") if value]
    if (
        process_exe != "/usr/local/bin/k3s"
        or argv != ["/usr/local/bin/k3s", "server"]
    ):
        raise RuntimeErrorEB("active k3s process identity drifted")
    process_environment = run(
        [
            *ssh_argv(root, live_ip),
            "sudo",
            "cat",
            f"/proc/{main_pid}/environ",
        ],
        timeout=30,
    ).stdout
    environment_entries = [
        value for value in process_environment.split("\0") if value
    ]
    if any(
        entry.split("=", 1)[0].startswith("K3S_")
        for entry in environment_entries
        if "=" in entry
    ):
        raise RuntimeErrorEB("active k3s process has unexpected K3S environment overrides")

    return {
        "vm_ip": live_ip,
        "kubeconfig_sha256": receipt["kubeconfig_sha256"],
        "kubeconfig_server": expected_server,
        "binary_sha256": expected_binary_sha256,
        "config_sha256": expected_config_sha256,
        "service_sha256": expected_service_sha256,
        "service_active": True,
        "service_substate": "running",
        "unit_file_state": "enabled",
        "fragment_path": "/etc/systemd/system/k3s.service",
        "drop_ins_absent": True,
        "main_pid": main_pid,
        "process_exe": process_exe,
        "process_argv": argv,
        "environment_overrides_absent": True,
    }


def install_k3s(root: Path) -> dict[str, Any]:
    _invalidate_receipts(root, K3S_ATTEMPT_INVALIDATES)
    source_commit = _current_protected_main_commit()
    config = load_config()
    vm_create_path = root / "receipts/vm-create.json"
    try:
        vm_create = json.loads(vm_create_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeErrorEB(
            "k3s installation requires a valid VM creation receipt"
        ) from exc
    _require_vm_create_receipt(vm_create, source_commit, config, root)
    live_substrate = _live_vm_substrate(root, config)
    if live_substrate != vm_create["substrate"]:
        raise RuntimeErrorEB(
            "k3s installation refuses VM substrate drift from creation receipt"
        )
    ip = vm_ip()
    wait_ssh(root, ip)
    k3s_binary = root / "downloads/k3s"
    if not k3s_binary.is_file():
        raise RuntimeErrorEB("prepared k3s binary is missing")
    expected_k3s_sha256 = str(config["kubernetes"]["binary_sha256"])
    observed_k3s_sha256 = sha256_file(k3s_binary)
    if observed_k3s_sha256 != expected_k3s_sha256:
        raise RuntimeErrorEB("prepared k3s binary digest does not match current config")
    scp_to(root, ip, k3s_binary, "/tmp/k3s")
    scp_to(root, ip, CLUSTER / "k3s-config.yaml", "/tmp/config.yaml")
    scp_to(root, ip, CLUSTER / "k3s.service", "/tmp/k3s.service")
    command = (
        "sudo install -m 0755 /tmp/k3s /usr/local/bin/k3s && "
        "sudo install -d -m 0755 /etc/rancher/k3s && "
        "sudo install -m 0600 /tmp/config.yaml /etc/rancher/k3s/config.yaml && "
        "sudo install -m 0644 /tmp/k3s.service /etc/systemd/system/k3s.service && "
        "sudo systemctl daemon-reload && "
        "sudo systemctl enable k3s && "
        "sudo systemctl restart k3s"
    )
    run([*ssh_argv(root, ip), command], timeout=180)
    live_node: dict[str, Any] | None = None
    for _ in range(90):
        result = run(
            [*ssh_argv(root, ip), "sudo /usr/local/bin/k3s kubectl get nodes -o json"],
            check=False,
        )
        if result.returncode == 0:
            try:
                inventory = json.loads(result.stdout)
                live_node = _require_exact_k3s_node_inventory(
                    inventory, str(config["kubernetes"]["version"])
                )
            except (json.JSONDecodeError, RuntimeErrorEB):
                live_node = None
            if live_node is not None:
                break
        time.sleep(2)
    else:
        raise RuntimeErrorEB(
            "k3s node did not become queryable at the exact pinned version"
        )

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
    config_path, service_path = _k3s_contract_paths(config)
    receipt = {
        "schema_version": 1,
        "status": "ready",
        "source_commit": source_commit,
        "vm_ip": ip,
        "k3s_version": version,
        "live_kubelet_version": live_node["kubelet_version"],
        "binary_sha256": expected_k3s_sha256,
        "config_sha256": sha256_file(config_path),
        "service_sha256": sha256_file(service_path),
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


def _cilium_helm_value_args(ip: str) -> list[str]:
    return [
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
    ]


def _flux_install_argv(flux: str, *, export: bool = False) -> list[str]:
    argv = [
        flux,
        "install",
        "--namespace=flux-system",
        "--components=source-controller,kustomize-controller,helm-controller,notification-controller",
    ]
    if export:
        argv.append("--export")
    return argv


def install_platform(root: Path) -> dict[str, Any]:
    _invalidate_receipts(root, PLATFORM_ATTEMPT_INVALIDATES)
    source_commit = _current_protected_main_commit()
    _require_kubernetes_target_binding(root, source_commit)
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
            *_cilium_helm_value_args(ip),
            "--wait", "--timeout", "10m",
        ],
        env=env,
        timeout=900,
    )
    run(
        _flux_install_argv(flux),
        env=env,
        timeout=600,
    )
    cilium_readback = _require_live_cilium_contract(root, load_config())
    flux_readback: dict[str, Any] | None = None
    last_flux_error: RuntimeErrorEB | None = None
    for _ in range(90):
        try:
            flux_readback = _require_live_flux_controller_contract(root, receipt)
        except RuntimeErrorEB as exc:
            last_flux_error = exc
            time.sleep(2)
        else:
            break
    if flux_readback is None:
        raise RuntimeErrorEB(
            "Flux controllers did not converge to the pinned runtime contract"
        ) from last_flux_error
    result = {
        "schema_version": 1,
        "status": "ready",
        "source_commit": source_commit,
        "toolchain_lock_sha256": receipt["lock_sha256"],
        "vm_ip": ip,
        "cilium_runtime_image_ids": {
            "daemonset": cilium_readback["daemonset_pods"][
                "runtime_image_ids_sha256"
            ],
            "operator": cilium_readback["operator_pods"][
                "runtime_image_ids_sha256"
            ],
        },
        "flux_runtime_image_ids": {
            name: controller["pods"]["runtime_image_ids_sha256"]
            for name, controller in flux_readback.items()
        },
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
    _invalidate_receipts(root, SECRETS_ATTEMPT_INVALIDATES)
    source_commit = _current_protected_main_commit()
    _require_kubernetes_target_binding(root, source_commit)
    if not registry_config.is_file() or registry_config.is_symlink():
        raise RuntimeErrorEB("registry config must be a regular external file")
    try:
        registry_bytes = registry_config.read_bytes()
        registry_payload = json.loads(registry_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeErrorEB("registry config is not valid JSON") from exc
    if "ghcr.io" not in registry_payload.get("auths", {}):
        raise RuntimeErrorEB("registry config has no ghcr.io credential")
    registry_state = root / "secrets/registry.json"
    atomic_bytes(registry_state, registry_bytes)
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
            binary_data={".dockerconfigjson": registry_bytes},
        ),
    )
    receipt = {
        "schema_version": 1,
        "status": "ready",
        "source_commit": source_commit,
        "database_secret": "commonthing-experiment-b-database",
        "runtime_secret": "weltgewebe-runtime",
        "registry_secret": "commonthing-experiment-b-registry",
        "database_source_sha256": sha256_file(root / "secrets/database.json"),
        "registry_source_sha256": sha256_file(registry_state),
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
    receipt_path, attempt_path, attempt_started_at_unix_ms = (
        _begin_release_attempt(root, source_commit)
    )
    if _current_protected_main_commit() != source_commit:
        raise RuntimeErrorEB("release source is not current protected main")
    _require_kubernetes_target_binding(root, source_commit)
    config = load_config()
    api_replicas = int(config["semantic_search"]["api_replicas"])
    web_replicas = int(config["runtime_binding"]["web_replicas"])
    output = root / "bootstrap.yaml"
    binding = contract.render_bootstrap(
        source_commit, api_digest, web_digest, output
    )
    flux_contract = _flux_bootstrap_contract(root, binding)
    kubectl_apply(root, output.read_text(encoding="utf-8"))
    kubectl = toolchain(root)["tools"]["kubectl"]
    env = kube_env(root)
    for _ in range(120):
        source_result = run(
            [
                kubectl,
                "-n",
                "flux-system",
                "get",
                "gitrepository",
                "commonthing-experiment-b",
                "-o",
                "json",
            ],
            env=env,
            check=False,
        )
        flux_result = run(
            [
                kubectl,
                "-n",
                "flux-system",
                "get",
                "kustomizations",
                "-o",
                "json",
            ],
            env=env,
            check=False,
        )
        if source_result.returncode == 0 and flux_result.returncode == 0:
            try:
                source_payload = json.loads(source_result.stdout)
                flux_payload = json.loads(flux_result.stdout)
                if not isinstance(source_payload, dict) or not isinstance(
                    flux_payload, dict
                ):
                    raise RuntimeErrorEB("Flux readiness payload is not an object")
                _require_flux_source_revision(
                    source_payload,
                    source_commit,
                    flux_contract["source_spec"],
                )
                _require_exact_flux_revision_ready(
                    flux_payload.get("items", []),
                    source_commit,
                    flux_contract["kustomization_specs"],
                )
                live_results = {
                    "api": run(
                        [
                            kubectl, "-n", APP_NAMESPACE, "get", "deployment",
                            "weltgewebe-api", "-o", "json",
                        ],
                        env=env,
                        check=False,
                    ),
                    "web": run(
                        [
                            kubectl, "-n", APP_NAMESPACE, "get", "deployment",
                            "weltgewebe-web", "-o", "json",
                        ],
                        env=env,
                        check=False,
                    ),
                    "migration": run(
                        [
                            kubectl, "-n", APP_NAMESPACE, "get", "job",
                            MIGRATION_JOB_NAME, "-o", "json",
                        ],
                        env=env,
                        check=False,
                    ),
                    "migration_pods": run(
                        [
                            kubectl, "-n", APP_NAMESPACE, "get", "pods",
                            "-l", f"batch.kubernetes.io/job-name={MIGRATION_JOB_NAME}",
                            "-o", "json",
                        ],
                        env=env,
                        check=False,
                    ),
                }
                if any(result.returncode != 0 for result in live_results.values()):
                    raise RuntimeErrorEB(
                        "Experiment-B release artifacts are not all queryable"
                    )
                migration_pods_payload = json.loads(
                    live_results["migration_pods"].stdout
                )
                if not isinstance(migration_pods_payload, dict):
                    raise RuntimeErrorEB(
                        "Experiment-B migration Pod inventory is not an object"
                    )
                _require_requested_release_artifacts(
                    root,
                    json.loads(live_results["api"].stdout),
                    json.loads(live_results["web"].stdout),
                    json.loads(live_results["migration"].stdout),
                    migration_pods_payload.get("items"),
                    api_digest,
                    web_digest,
                    api_replicas,
                    web_replicas,
                )
            except (json.JSONDecodeError, RuntimeErrorEB):
                pass
            else:
                break
        time.sleep(5)
    else:
        raise RuntimeErrorEB(
            "Flux Experiment-B source and kustomizations did not converge "
            "to the exact release revision"
        )
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


def _semantic_provider_live_readback(
    root: Path, source_commit: str
) -> dict[str, Any]:
    if not COMMIT_RE.fullmatch(source_commit):
        raise RuntimeErrorEB("semantic provider live source commit is not exact")
    semantic = load_config()["semantic_search"]
    kubectl = toolchain(root)["tools"]["kubectl"]
    env = kube_env(root)
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
    return {
        "source_commit": source_commit,
        "provider": semantic["provider"],
        "model_id": semantic["model_id"],
        "model_revision": semantic["model_revision"],
        "runtime_identity": semantic["runtime_identity"],
        "dimension": semantic["dimension"],
        "embedding_probe": True,
        "embedding_probe_sha256": hashlib.sha256(
            probe_text.encode("utf-8")
        ).hexdigest(),
        "literal_loopback": True,
    }


def semantic_activate(root: Path) -> dict[str, Any]:
    release_path = root / "receipts/release.json"
    if not release_path.is_file():
        raise RuntimeErrorEB("semantic provider proof requires an applied release receipt")
    release = json.loads(release_path.read_text(encoding="utf-8"))
    source_commit = str(release.get("source_commit", ""))
    if not COMMIT_RE.fullmatch(source_commit):
        raise RuntimeErrorEB("semantic provider proof release binding is not exact")
    receipt_path, attempt_path, attempt_started_at_unix_ms = (
        _begin_live_check_attempt(root, "semantic-search", source_commit)
    )
    if _current_protected_main_commit() != source_commit:
        raise RuntimeErrorEB("semantic provider proof is not bound to current protected main")
    _require_kubernetes_target_binding(root, source_commit)
    config = load_config()
    semantic = config["semantic_search"]
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
        semantic_live = _semantic_provider_live_readback(root, source_commit)
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
        **semantic_live,
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


def _flux_revision_matches_commit(revision: Any, source_commit: str) -> bool:
    if not COMMIT_RE.fullmatch(source_commit):
        return False
    value = str(revision or "")
    return value in {source_commit, f"sha1:{source_commit}"} or value.endswith(
        f"@sha1:{source_commit}"
    )


def _canonical_flux_spec(spec: Any) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise RuntimeErrorEB("Flux spec is not an object")
    canonical = json.loads(json.dumps(spec))
    for key, default in (
        ("suspend", False),
        ("force", False),
        ("deletionPolicy", "MirrorPrune"),
        ("provider", "generic"),
        ("timeout", "60s"),
    ):
        if canonical.get(key) == default:
            canonical.pop(key, None)

    source_ref = canonical.get("sourceRef")
    if isinstance(source_ref, dict) and source_ref.get("namespace") == "flux-system":
        source_ref.pop("namespace", None)

    ref = canonical.get("ref")
    if isinstance(ref, dict) and ref.get("recurseSubmodules") is False:
        ref.pop("recurseSubmodules", None)

    post_build = canonical.get("postBuild")
    if isinstance(post_build, dict):
        substitute_from = post_build.get("substituteFrom")
        if isinstance(substitute_from, list):
            for item in substitute_from:
                if isinstance(item, dict) and item.get("optional") is False:
                    item.pop("optional", None)

    duration_pattern = re.compile(
        r"^(?:(?P<hours>[0-9]+)h)?(?:(?P<minutes>[0-9]+)m)?(?:(?P<seconds>[0-9]+)s)?$"
    )
    for key in ("interval", "retryInterval", "timeout"):
        value = canonical.get(key)
        if not isinstance(value, str):
            continue
        match = duration_pattern.fullmatch(value)
        if match and any(match.groupdict().values()):
            canonical[key] = (
                int(match.group("hours") or 0) * 3600
                + int(match.group("minutes") or 0) * 60
                + int(match.group("seconds") or 0)
            )
    return canonical


def _require_flux_spec(
    live_spec: Any,
    expected_spec: Any,
    context: str,
) -> None:
    if _canonical_flux_spec(live_spec) != _canonical_flux_spec(expected_spec):
        raise RuntimeErrorEB(f"{context} spec drifted from the rendered bootstrap contract")


def _flux_bootstrap_contract(
    root: Path,
    binding: dict[str, Any],
) -> dict[str, Any]:
    bootstrap_path = root / "bootstrap.yaml"
    expected_sha256 = binding.get("sha256")
    if (
        not isinstance(expected_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
        or not bootstrap_path.is_file()
        or bootstrap_path.is_symlink()
        or not secrets.compare_digest(sha256_file(bootstrap_path), expected_sha256)
    ):
        raise RuntimeErrorEB("Experiment-B rendered bootstrap binding drifted")
    try:
        documents = list(
            yaml.safe_load_all(bootstrap_path.read_text(encoding="utf-8"))
        )
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeErrorEB("Experiment-B rendered bootstrap is invalid") from exc

    source_spec: dict[str, Any] | None = None
    kustomization_specs: dict[str, dict[str, Any]] = {}
    for document in documents:
        if not isinstance(document, dict):
            continue
        metadata = document.get("metadata", {})
        if not isinstance(metadata, dict):
            continue
        name = metadata.get("name")
        namespace = metadata.get("namespace")
        spec = document.get("spec")
        if (
            document.get("kind") == "GitRepository"
            and name == "commonthing-experiment-b"
            and namespace == "flux-system"
        ):
            if source_spec is not None or not isinstance(spec, dict):
                raise RuntimeErrorEB("Experiment-B bootstrap GitRepository is duplicated")
            source_spec = spec
        elif (
            document.get("kind") == "Kustomization"
            and namespace == "flux-system"
            and isinstance(name, str)
            and name.startswith("commonthing-experiment-b-")
        ):
            if name in kustomization_specs or not isinstance(spec, dict):
                raise RuntimeErrorEB(
                    f"Experiment-B bootstrap Kustomization is duplicated: {name}"
                )
            kustomization_specs[name] = spec

    if source_spec is None:
        raise RuntimeErrorEB("Experiment-B bootstrap GitRepository contract is missing")
    _require_exact_flux_kustomizations(kustomization_specs)
    return {
        "bootstrap_sha256": expected_sha256,
        "source_spec": source_spec,
        "kustomization_specs": kustomization_specs,
    }


def _require_current_condition(
    document: Any,
    condition_type: str,
    context: str,
    *,
    conditions: Any = None,
) -> int:
    if not isinstance(document, dict):
        raise RuntimeErrorEB(f"{context} payload is not an object")
    metadata = document.get("metadata", {})
    if not isinstance(metadata, dict):
        raise RuntimeErrorEB(f"{context} metadata is not an object")
    try:
        generation = int(metadata.get("generation") or 0)
    except (TypeError, ValueError) as exc:
        raise RuntimeErrorEB(f"{context} generation is invalid") from exc
    if generation < 1:
        raise RuntimeErrorEB(f"{context} generation is invalid")
    status_obj = document.get("status", {})
    selected = (
        conditions
        if conditions is not None
        else status_obj.get("conditions", []) if isinstance(status_obj, dict) else []
    )
    if not isinstance(selected, list):
        raise RuntimeErrorEB(f"{context} conditions are invalid")
    current = any(
        isinstance(condition, dict)
        and condition.get("type") == condition_type
        and condition.get("status") == "True"
        and condition.get("observedGeneration") == generation
        for condition in selected
    )
    if not current:
        raise RuntimeErrorEB(
            f"{context} is not currently {condition_type} at its current generation"
        )
    return generation


def _require_flux_source_revision(
    source: Any,
    source_commit: str,
    expected_spec: dict[str, Any],
) -> str:
    if not isinstance(source, dict):
        raise RuntimeErrorEB("Flux GitRepository payload is not an object")
    metadata = source.get("metadata", {})
    if (
        not isinstance(metadata, dict)
        or metadata.get("deletionTimestamp") is not None
    ):
        raise RuntimeErrorEB(
            "Flux GitRepository is pending deletion or malformed"
        )
    spec = source.get("spec", {})
    if not isinstance(spec, dict) or spec.get("suspend") is True:
        raise RuntimeErrorEB("Flux GitRepository is suspended or malformed")
    _require_flux_spec(spec, expected_spec, "Flux GitRepository")
    _require_current_condition(source, "Ready", "Flux GitRepository")
    revision = str(
        source.get("status", {}).get("artifact", {}).get("revision", "")
    )
    if not _flux_revision_matches_commit(revision, source_commit):
        raise RuntimeErrorEB("Flux GitRepository is not bound to the release commit")
    return revision


def _require_exact_flux_revision_ready(
    flux_items: Any,
    source_commit: str,
    expected_specs: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if not isinstance(flux_items, list):
        raise RuntimeErrorEB("Flux Kustomization inventory is not a list")
    _require_exact_flux_kustomizations(expected_specs)

    flux_readback: dict[str, Any] = {}
    for item in flux_items:
        if not isinstance(item, dict):
            raise RuntimeErrorEB("Flux Kustomization inventory contains a non-object item")
        metadata = item.get("metadata", {})
        if not isinstance(metadata, dict):
            raise RuntimeErrorEB(
                "Flux Kustomization metadata is not an object"
            )
        name = str(metadata.get("name", ""))
        if not name.startswith("commonthing-experiment-b-"):
            continue
        if metadata.get("deletionTimestamp") is not None:
            raise RuntimeErrorEB(
                f"Flux Kustomization is pending deletion: {name}"
            )
        if name in flux_readback:
            raise RuntimeErrorEB(f"duplicate Flux Kustomization: {name}")
        expected_spec = expected_specs.get(name)
        if not isinstance(expected_spec, dict):
            raise RuntimeErrorEB(f"unexpected Flux Kustomization: {name}")
        spec = item.get("spec", {})
        if not isinstance(spec, dict) or spec.get("suspend") is True:
            raise RuntimeErrorEB(f"Flux Kustomization is suspended or malformed: {name}")
        _require_flux_spec(spec, expected_spec, f"Flux Kustomization {name}")
        generation = _require_current_condition(
            item, "Ready", f"Flux Kustomization {name}"
        )
        status_obj = item.get("status", {})
        revision = str(status_obj.get("lastAppliedRevision", ""))
        if not _flux_revision_matches_commit(revision, source_commit):
            raise RuntimeErrorEB(
                f"Flux Kustomization is not exact-revision Ready: {name}"
            )
        flux_readback[name] = {
            "ready": True,
            "revision": revision,
            "generation": generation,
            "spec_sha256": _stable_json_sha256(_canonical_flux_spec(spec)),
        }

    _require_exact_flux_kustomizations(flux_readback)
    return flux_readback


def _require_exact_k3s_node_inventory(
    nodes: Any, expected_version: str
) -> dict[str, Any]:
    if not isinstance(nodes, dict) or nodes.get("kind") not in {"NodeList", "List"}:
        raise RuntimeErrorEB("Experiment B node inventory is not a Kubernetes node list")
    items = nodes.get("items")
    if not isinstance(items, list) or len(items) != 1:
        raise RuntimeErrorEB("Experiment B expects exactly one k3s VM node")
    node = items[0]
    if not isinstance(node, dict) or node.get("kind") != "Node":
        raise RuntimeErrorEB("Experiment B node inventory item is not a Kubernetes Node")
    metadata = node.get("metadata", {})
    status_obj = node.get("status", {})
    if (
        not isinstance(metadata, dict)
        or not metadata.get("name")
        or metadata.get("deletionTimestamp") is not None
        or not isinstance(status_obj, dict)
    ):
        raise RuntimeErrorEB("Experiment B node identity/deletion state is invalid")
    conditions = status_obj.get("conditions")
    ready_conditions = (
        [
            condition
            for condition in conditions
            if isinstance(condition, dict) and condition.get("type") == "Ready"
        ]
        if isinstance(conditions, list)
        else []
    )
    if len(ready_conditions) != 1 or ready_conditions[0].get("status") != "True":
        raise RuntimeErrorEB("Experiment B k3s node is not Ready")
    info = status_obj.get("nodeInfo", {})
    kubelet = str(info.get("kubeletVersion", ""))
    os_image = str(info.get("osImage", ""))
    if "k3s" not in kubelet:
        raise RuntimeErrorEB("node is not a k3s runtime")
    if kubelet != expected_version:
        raise RuntimeErrorEB("node kubelet version does not match pinned k3s version")
    return {
        "node": str(metadata["name"]),
        "kubelet_version": kubelet,
        "os_image": os_image,
        "ready": True,
    }


def _pvc_spec_projection(
    pvc: Any,
    context: str,
) -> dict[str, Any]:
    if not isinstance(pvc, dict):
        raise RuntimeErrorEB(f"{context} PVC payload is invalid")
    spec = pvc.get("spec", {})
    if not isinstance(spec, dict):
        raise RuntimeErrorEB(f"{context} PVC spec is invalid")
    access_modes = spec.get("accessModes") or []
    resources = spec.get("resources") or {}
    if (
        not isinstance(access_modes, list)
        or any(not isinstance(value, str) or not value for value in access_modes)
        or not isinstance(resources, dict)
    ):
        raise RuntimeErrorEB(f"{context} PVC access/resource contract is invalid")
    storage_class = spec.get("storageClassName")
    if storage_class is not None and (
        not isinstance(storage_class, str) or not storage_class
    ):
        raise RuntimeErrorEB(f"{context} PVC storageClassName is invalid")
    volume_mode = spec.get("volumeMode", "Filesystem")
    if volume_mode not in {"Filesystem", "Block"}:
        raise RuntimeErrorEB(f"{context} PVC volumeMode is invalid")
    return {
        "accessModes": sorted(access_modes),
        "storageClassName": storage_class,
        "volumeMode": volume_mode,
        "resources": json.loads(json.dumps(resources)),
        "selector": spec.get("selector"),
        "dataSource": spec.get("dataSource"),
        "dataSourceRef": spec.get("dataSourceRef"),
        "volumeAttributesClassName": spec.get("volumeAttributesClassName"),
    }


def _rendered_pvc_contract(root: Path) -> dict[str, Any]:
    kustomize = toolchain(root)["tools"].get("kustomize")
    if not isinstance(kustomize, str) or not kustomize:
        raise RuntimeErrorEB("PVC contract requires pinned kustomize")
    documents: list[dict[str, Any]] = []
    for target in (CLUSTER / "data", APP_OVERLAY):
        rendered = run([kustomize, "build", str(target)]).stdout
        try:
            documents.extend(
                document
                for document in yaml.safe_load_all(rendered)
                if isinstance(document, dict)
            )
        except yaml.YAMLError as exc:
            raise RuntimeErrorEB(
                f"rendered Experiment-B PVC contract is invalid: {target}"
            ) from exc

    result: dict[str, Any] = {}
    for document in documents:
        if document.get("kind") != "PersistentVolumeClaim":
            continue
        metadata = document.get("metadata", {})
        if not isinstance(metadata, dict):
            raise RuntimeErrorEB("rendered Experiment-B PVC metadata is invalid")
        namespace = str(metadata.get("namespace", ""))
        name = str(metadata.get("name", ""))
        if namespace not in {APP_NAMESPACE, DATA_NAMESPACE}:
            continue
        key = f"{namespace}/{name}"
        if key in result:
            raise RuntimeErrorEB(
                f"rendered Experiment-B PVC is duplicated: {key}"
            )
        spec = _pvc_spec_projection(
            document, f"rendered Experiment-B PVC {key}"
        )
        result[key] = {
            "spec": spec,
            "spec_sha256": _stable_json_sha256(spec),
        }
    if set(result) != EXPECTED_PVCS:
        raise RuntimeErrorEB(
            "rendered Experiment-B PVC set drifted from the expected contract"
        )
    return result


def _require_exact_healthy_pvcs(
    root: Path,
    pvc_items: Any,
) -> dict[str, Any]:
    if not isinstance(pvc_items, list):
        raise RuntimeErrorEB("Experiment-B PVC inventory is not a list")
    expected = _rendered_pvc_contract(root)

    live: dict[str, dict[str, Any]] = {}
    for item in pvc_items:
        if not isinstance(item, dict):
            raise RuntimeErrorEB("Experiment-B PVC inventory contains a non-object item")
        metadata = item.get("metadata", {})
        if not isinstance(metadata, dict):
            raise RuntimeErrorEB("Experiment-B PVC metadata is not an object")
        namespace = str(metadata.get("namespace", ""))
        name = str(metadata.get("name", ""))
        if namespace not in {APP_NAMESPACE, DATA_NAMESPACE}:
            continue
        key = f"{namespace}/{name}"
        if key in live:
            raise RuntimeErrorEB(f"Experiment-B PVC inventory contains duplicate: {key}")
        live[key] = item

    observed = set(live)
    if observed != set(expected):
        missing = sorted(set(expected) - observed)
        unexpected = sorted(observed - set(expected))
        raise RuntimeErrorEB(
            "Experiment-B PVC set mismatch: "
            f"missing={missing}; unexpected={unexpected}"
        )

    pvc_readback: dict[str, Any] = {}
    for key, expected_value in expected.items():
        item = live[key]
        metadata = item.get("metadata", {})
        if metadata.get("deletionTimestamp"):
            raise RuntimeErrorEB(f"Experiment-B PVC is pending deletion: {key}")
        status = item.get("status", {})
        phase = status.get("phase") if isinstance(status, dict) else None
        if phase != "Bound":
            raise RuntimeErrorEB(f"Experiment-B PVC is not Bound: {key}")
        observed_spec = _pvc_spec_projection(
            item, f"live Experiment-B PVC {key}"
        )
        if observed_spec != expected_value["spec"]:
            raise RuntimeErrorEB(
                f"Experiment-B PVC contract drifted: {key}"
            )
        pvc_readback[key] = {
            "phase": "Bound",
            "spec_sha256": expected_value["spec_sha256"],
            "canonical": True,
        }
    return pvc_readback


def _deployment_availability_snapshot(
    deployment: dict[str, Any], name: str, expected_replicas: int
) -> dict[str, Any]:
    metadata = deployment.get("metadata", {})
    spec = deployment.get("spec", {})
    status_obj = deployment.get("status", {})
    generation = int(metadata.get("generation") or 0)
    desired = int(spec.get("replicas") or 0)
    observed_generation = int(status_obj.get("observedGeneration") or 0)
    replicas = int(status_obj.get("replicas") or 0)
    updated = int(status_obj.get("updatedReplicas") or 0)
    ready = int(status_obj.get("readyReplicas") or 0)
    available = int(status_obj.get("availableReplicas") or 0)
    unavailable = int(status_obj.get("unavailableReplicas") or 0)
    available_condition = any(
        condition.get("type") == "Available" and condition.get("status") == "True"
        for condition in status_obj.get("conditions", [])
        if isinstance(condition, dict)
    )
    if (
        not isinstance(expected_replicas, int)
        or isinstance(expected_replicas, bool)
        or expected_replicas < 1
        or generation < 1
        or desired != expected_replicas
        or observed_generation < generation
        or replicas != desired
        or updated != desired
        or ready != desired
        or available != desired
        or unavailable != 0
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


EXPECTED_FLUX_CONTROLLERS = frozenset(
    {
        "source-controller",
        "kustomize-controller",
        "helm-controller",
        "notification-controller",
    }
)


def _normalize_flux_env(
    value: Any,
    context: str,
) -> Any:
    if value is None:
        return None
    if not isinstance(value, list) or any(
        not isinstance(item, dict) for item in value
    ):
        raise RuntimeErrorEB(f"{context} env contract is invalid")
    normalized = json.loads(json.dumps(value))
    for item in normalized:
        value_from = item.get("valueFrom")
        if not isinstance(value_from, dict):
            continue
        field_ref = value_from.get("fieldRef")
        if isinstance(field_ref, dict) and field_ref.get("apiVersion") == "v1":
            field_ref.pop("apiVersion", None)
        resource_ref = value_from.get("resourceFieldRef")
        if (
            isinstance(resource_ref, dict)
            and resource_ref.get("divisor") == "0"
        ):
            resource_ref.pop("divisor", None)
    return normalized


def _normalize_flux_resources(
    value: Any,
    context: str,
) -> Any:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise RuntimeErrorEB(f"{context} resources contract is invalid")
    normalized = json.loads(json.dumps(value))
    for bucket_name in ("limits", "requests"):
        bucket = normalized.get(bucket_name)
        if bucket is None:
            continue
        if not isinstance(bucket, dict):
            raise RuntimeErrorEB(
                f"{context} {bucket_name} resources contract is invalid"
            )
        cpu = bucket.get("cpu")
        if cpu is not None:
            if not isinstance(cpu, str):
                raise RuntimeErrorEB(
                    f"{context} CPU resource quantity is invalid"
                )
            bucket["cpu"] = _parse_cpu_quantity(cpu)
        memory = bucket.get("memory")
        if memory is not None:
            if not isinstance(memory, str):
                raise RuntimeErrorEB(
                    f"{context} memory resource quantity is invalid"
                )
            bucket["memory"] = _parse_memory_quantity(memory)
    return normalized


def _kube_api_access_volume_name(value: Any) -> str | None:
    if not isinstance(value, dict) or set(value) != {"name", "projected"}:
        return None
    name = value.get("name")
    if not isinstance(name, str) or not name.startswith("kube-api-access-"):
        return None
    suffix = name.removeprefix("kube-api-access-")
    if len(suffix) != 5 or not suffix.isalnum():
        return None
    projected = value.get("projected")
    if (
        not isinstance(projected, dict)
        or set(projected) != {"defaultMode", "sources"}
        or projected.get("defaultMode") != 420
    ):
        return None
    sources = projected.get("sources")
    if not isinstance(sources, list) or len(sources) != 3:
        return None
    by_kind: dict[str, dict[str, Any]] = {}
    for source in sources:
        if not isinstance(source, dict) or len(source) != 1:
            return None
        kind = next(iter(source))
        if kind in by_kind or not isinstance(source[kind], dict):
            return None
        by_kind[kind] = source[kind]
    if set(by_kind) != {
        "serviceAccountToken",
        "configMap",
        "downwardAPI",
    }:
        return None
    token = by_kind["serviceAccountToken"]
    expiration = token.get("expirationSeconds")
    if (
        set(token) != {"expirationSeconds", "path"}
        or token.get("path") != "token"
        or isinstance(expiration, bool)
        or not isinstance(expiration, int)
        or expiration <= 0
    ):
        return None
    if by_kind["configMap"] != {
        "name": "kube-root-ca.crt",
        "items": [{"key": "ca.crt", "path": "ca.crt"}],
    }:
        return None
    if by_kind["downwardAPI"] != {
        "items": [
            {
                "path": "namespace",
                "fieldRef": {
                    "apiVersion": "v1",
                    "fieldPath": "metadata.namespace",
                },
            }
        ]
    }:
        return None
    return name


def _normalize_flux_pod_spec(
    pod_spec: Any,
    context: str,
) -> dict[str, Any]:
    if not isinstance(pod_spec, dict):
        raise RuntimeErrorEB(f"{context} Pod spec is invalid")
    normalized = json.loads(json.dumps(pod_spec))
    volumes = normalized.get("volumes")
    if volumes is None:
        volumes = []
        normalized["volumes"] = volumes
    if not isinstance(volumes, list) or any(
        not isinstance(volume, dict) for volume in volumes
    ):
        raise RuntimeErrorEB(f"{context} volume contract is invalid")

    injected_names = [
        name
        for volume in volumes
        if (name := _kube_api_access_volume_name(volume)) is not None
    ]
    if len(injected_names) == 1:
        injected_name = injected_names[0]
        containers: list[dict[str, Any]] = []
        valid_mounts = True
        for field in ("containers", "initContainers"):
            items = normalized.get(field, [])
            if not isinstance(items, list) or any(
                not isinstance(item, dict) for item in items
            ):
                raise RuntimeErrorEB(
                    f"{context} {field} inventory is invalid"
                )
            containers.extend(items)
        for container in containers:
            mounts = container.get("volumeMounts") or []
            if not isinstance(mounts, list) or any(
                not isinstance(mount, dict) for mount in mounts
            ):
                raise RuntimeErrorEB(
                    f"{context} container volumeMounts contract is invalid"
                )
            matches = [
                mount
                for mount in mounts
                if mount.get("name") == injected_name
            ]
            if matches != [
                {
                    "mountPath": (
                        "/var/run/secrets/kubernetes.io/serviceaccount"
                    ),
                    "name": injected_name,
                    "readOnly": True,
                }
            ]:
                valid_mounts = False
                break
        if valid_mounts and containers:
            normalized["volumes"] = [
                volume
                for volume in volumes
                if volume.get("name") != injected_name
            ]
            for container in containers:
                mounts = container.get("volumeMounts") or []
                remaining = [
                    mount
                    for mount in mounts
                    if mount.get("name") != injected_name
                ]
                if remaining:
                    container["volumeMounts"] = remaining
                else:
                    container.pop("volumeMounts", None)

    tolerations = normalized.get("tolerations") or []
    if not isinstance(tolerations, list) or any(
        not isinstance(item, dict) for item in tolerations
    ):
        raise RuntimeErrorEB(f"{context} toleration contract is invalid")
    remaining_tolerations = json.loads(json.dumps(tolerations))
    for default_toleration in (
        {
            "effect": "NoExecute",
            "key": "node.kubernetes.io/not-ready",
            "operator": "Exists",
            "tolerationSeconds": 300,
        },
        {
            "effect": "NoExecute",
            "key": "node.kubernetes.io/unreachable",
            "operator": "Exists",
            "tolerationSeconds": 300,
        },
    ):
        if default_toleration in remaining_tolerations:
            remaining_tolerations.remove(default_toleration)
    normalized["tolerations"] = remaining_tolerations

    for field in ("containers", "initContainers"):
        items = normalized.get(field, [])
        if not isinstance(items, list):
            raise RuntimeErrorEB(f"{context} {field} inventory is invalid")
        for container in items:
            if not isinstance(container, dict):
                raise RuntimeErrorEB(
                    f"{context} {field} container contract is invalid"
                )
            container["env"] = _normalize_flux_env(
                container.get("env"), context
            )
            container["resources"] = _normalize_flux_resources(
                container.get("resources"), context
            )
    return normalized


def _flux_strategy_projection(
    spec: dict[str, Any],
    context: str,
) -> dict[str, Any]:
    value = spec.get("strategy")
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise RuntimeErrorEB(f"{context} strategy contract is invalid")
    strategy = json.loads(json.dumps(value))
    strategy_type = strategy.get("type", "RollingUpdate")
    if strategy_type == "RollingUpdate":
        rolling = strategy.get("rollingUpdate")
        if rolling is None:
            rolling = {}
        if not isinstance(rolling, dict):
            raise RuntimeErrorEB(
                f"{context} rolling update contract is invalid"
            )
        strategy = {
            "type": "RollingUpdate",
            "rollingUpdate": {
                "maxSurge": rolling.get("maxSurge", "25%"),
                "maxUnavailable": rolling.get(
                    "maxUnavailable", "25%"
                ),
            },
        }
    return strategy


def _flux_pod_spec_projection(
    pod_spec: Any,
    context: str,
) -> dict[str, Any]:
    normalized = _normalize_flux_pod_spec(pod_spec, context)
    projection = _application_pod_spec_projection(normalized, context)
    pod_spec = normalized
    projection.update(
        {
            "hostNetwork": pod_spec.get("hostNetwork", False),
            "hostPID": pod_spec.get("hostPID", False),
            "hostIPC": pod_spec.get("hostIPC", False),
            "dnsPolicy": pod_spec.get("dnsPolicy", "ClusterFirst"),
            "dnsConfig": pod_spec.get("dnsConfig"),
            "priorityClassName": pod_spec.get("priorityClassName", ""),
            "tolerations": pod_spec.get("tolerations") or [],
            "restartPolicy": pod_spec.get("restartPolicy", "Always"),
            "schedulerName": pod_spec.get(
                "schedulerName", "default-scheduler"
            ),
            "enableServiceLinks": pod_spec.get("enableServiceLinks", True),
            "shareProcessNamespace": pod_spec.get(
                "shareProcessNamespace", False
            ),
            "runtimeClassName": pod_spec.get("runtimeClassName"),
        }
    )
    return projection


def _flux_deployment_contract(
    deployment: Any,
    context: str,
) -> dict[str, Any]:
    if not isinstance(deployment, dict):
        raise RuntimeErrorEB(f"{context} Deployment is invalid")
    spec = deployment.get("spec", {})
    template = spec.get("template", {}) if isinstance(spec, dict) else {}
    template_metadata = (
        template.get("metadata", {}) if isinstance(template, dict) else {}
    )
    pod_spec = template.get("spec", {}) if isinstance(template, dict) else {}
    replicas = spec.get("replicas", 1) if isinstance(spec, dict) else None
    if (
        isinstance(replicas, bool)
        or not isinstance(replicas, int)
        or replicas != 1
        or not isinstance(template_metadata, dict)
    ):
        raise RuntimeErrorEB(f"{context} replica/template contract is invalid")
    labels = template_metadata.get("labels", {})
    annotations = template_metadata.get("annotations", {})
    if not isinstance(labels, dict) or not isinstance(annotations, dict):
        raise RuntimeErrorEB(f"{context} template metadata contract is invalid")
    pod_contract = _flux_pod_spec_projection(pod_spec, context)
    deployment_contract = {
        "replicas": replicas,
        "revisionHistoryLimit": spec.get("revisionHistoryLimit", 10),
        "strategy": _flux_strategy_projection(spec, context),
        "minReadySeconds": spec.get("minReadySeconds", 0),
        "progressDeadlineSeconds": spec.get("progressDeadlineSeconds", 600),
        "selector_labels": _pod_selector_match_labels(deployment, context),
        "template_labels": labels,
        "template_annotations": annotations,
        "pod_spec": pod_contract,
    }
    return {
        "replicas": replicas,
        "selector_labels": deployment_contract["selector_labels"],
        "images": _pod_spec_images(pod_spec, context),
        "contract": deployment_contract,
        "contract_sha256": _stable_json_sha256(deployment_contract),
        "pod_contract_sha256": _stable_json_sha256(pod_contract),
    }


def _expected_flux_controller_contract(
    root: Path,
    toolchain_receipt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    receipt = toolchain_receipt or toolchain(root)
    tools = receipt.get("tools", {}) if isinstance(receipt, dict) else {}
    flux = tools.get("flux") if isinstance(tools, dict) else None
    if not isinstance(flux, str) or not flux:
        raise RuntimeErrorEB("pinned Flux toolchain binding is unavailable")
    rendered = run(_flux_install_argv(flux, export=True)).stdout
    try:
        documents = [
            item
            for item in yaml.safe_load_all(rendered)
            if isinstance(item, dict)
        ]
    except yaml.YAMLError as exc:
        raise RuntimeErrorEB("pinned Flux install render is invalid") from exc
    deployments = [
        document
        for document in documents
        if document.get("kind") == "Deployment"
        and document.get("metadata", {}).get("namespace") == "flux-system"
    ]
    names = {
        str(document.get("metadata", {}).get("name", ""))
        for document in deployments
    }
    if names != EXPECTED_FLUX_CONTROLLERS or len(deployments) != len(names):
        raise RuntimeErrorEB("pinned Flux controller Deployment set drifted")
    return {
        str(deployment["metadata"]["name"]): _flux_deployment_contract(
            deployment,
            f"pinned Flux Deployment {deployment['metadata']['name']}",
        )
        for deployment in deployments
    }


def _require_live_flux_controller_contract(
    root: Path,
    toolchain_receipt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    expected = _expected_flux_controller_contract(root, toolchain_receipt)
    items = _kubectl_json(
        root, ["-n", "flux-system", "get", "deployments"]
    ).get("items")
    pods = _kubectl_json(
        root, ["-n", "flux-system", "get", "pods"]
    ).get("items")
    if (
        not isinstance(items, list)
        or any(not isinstance(item, dict) for item in items)
        or not isinstance(pods, list)
        or any(not isinstance(item, dict) for item in pods)
    ):
        raise RuntimeErrorEB("Flux controller live inventory is invalid")
    selected = [
        item
        for item in items
        if str(item.get("metadata", {}).get("name", ""))
        in EXPECTED_FLUX_CONTROLLERS
    ]
    by_name = {
        str(item.get("metadata", {}).get("name", "")): item
        for item in selected
    }
    if (
        len(selected) != len(EXPECTED_FLUX_CONTROLLERS)
        or set(by_name) != EXPECTED_FLUX_CONTROLLERS
    ):
        missing = sorted(EXPECTED_FLUX_CONTROLLERS - set(by_name))
        raise RuntimeErrorEB(
            "Flux controller Deployment set is incomplete or duplicated: "
            f"{missing}"
        )
    result: dict[str, Any] = {}
    for name, contract_value in expected.items():
        deployment = by_name[name]
        metadata = deployment.get("metadata", {})
        if (
            not isinstance(metadata, dict)
            or metadata.get("namespace") != "flux-system"
            or metadata.get("deletionTimestamp") is not None
        ):
            raise RuntimeErrorEB(f"Flux controller identity drifted: {name}")
        observed = _flux_deployment_contract(
            deployment, f"live Flux Deployment {name}"
        )
        if observed["contract"] != contract_value["contract"]:
            raise RuntimeErrorEB(
                f"Flux controller Deployment contract drifted: {name}"
            )
        availability = _deployment_availability_snapshot(deployment, name, 1)
        matching_pods = _pods_matching_labels(
            pods, contract_value["selector_labels"]
        )
        pod_readback = _require_running_pod_image_contract(
            matching_pods,
            namespace="flux-system",
            workload=name,
            expected_replicas=1,
            expected_images=contract_value["images"],
            required_labels=contract_value["selector_labels"],
            context="Flux controller Pod",
        )
        pod_names: list[str] = []
        for pod in matching_pods:
            pod_metadata = (
                pod.get("metadata", {}) if isinstance(pod, dict) else {}
            )
            pod_spec = pod.get("spec", {}) if isinstance(pod, dict) else {}
            labels = (
                pod_metadata.get("labels", {})
                if isinstance(pod_metadata, dict)
                else {}
            )
            annotations = (
                pod_metadata.get("annotations", {})
                if isinstance(pod_metadata, dict)
                else {}
            )
            pod_name = (
                pod_metadata.get("name")
                if isinstance(pod_metadata, dict)
                else None
            )
            if (
                not isinstance(pod_name, str)
                or not pod_name
                or pod_name in pod_names
                or pod_metadata.get("namespace") != "flux-system"
                or pod_metadata.get("deletionTimestamp") is not None
                or not isinstance(labels, dict)
                or not isinstance(annotations, dict)
                or any(
                    labels.get(key) != value
                    for key, value in contract_value["contract"][
                        "template_labels"
                    ].items()
                )
                or any(
                    annotations.get(key) != value
                    for key, value in contract_value["contract"][
                        "template_annotations"
                    ].items()
                )
                or _flux_pod_spec_projection(
                    pod_spec, f"live Flux Pod {pod_name}"
                )
                != contract_value["contract"]["pod_spec"]
            ):
                raise RuntimeErrorEB(
                    f"Flux controller Pod contract drifted: {name}"
                )
            pod_names.append(pod_name)
        result[name] = {
            **availability,
            "images": observed["images"],
            "images_sha256": _stable_json_sha256(observed["images"]),
            "images_canonical": True,
            "pods": pod_readback,
            "contract_sha256": contract_value["contract_sha256"],
            "pod_contract_sha256": contract_value[
                "pod_contract_sha256"
            ],
            "pod_names": sorted(pod_names),
            "canonical": True,
        }
    return result


def _require_flux_runtime_baseline(
    root: Path,
    source_commit: str,
    flux_readback: dict[str, Any],
) -> dict[str, str]:
    path = root / "receipts/platform.json"
    try:
        platform = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeErrorEB(
            "Experiment-B status requires valid Flux runtime baseline"
        ) from exc
    baseline = platform.get("flux_runtime_image_ids")
    if (
        not isinstance(platform, dict)
        or platform.get("schema_version") != 1
        or platform.get("status") != "ready"
        or platform.get("source_commit") != source_commit
        or not isinstance(baseline, dict)
        or set(baseline) != EXPECTED_FLUX_CONTROLLERS
        or any(
            not isinstance(value, str)
            or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in baseline.values()
        )
    ):
        raise RuntimeErrorEB("Experiment-B Flux runtime baseline is invalid")
    current = {
        name: controller.get("pods", {}).get("runtime_image_ids_sha256")
        for name, controller in flux_readback.items()
    }
    if current != baseline:
        raise RuntimeErrorEB(
            "live Flux controller runtime image IDs drifted from platform installation"
        )
    return {str(key): str(value) for key, value in baseline.items()}


def _container_images(document: Any, context: str) -> dict[str, str]:
    if not isinstance(document, dict):
        raise RuntimeErrorEB(f"{context} payload is not an object")
    containers = (
        document.get("spec", {})
        .get("template", {})
        .get("spec", {})
        .get("containers", [])
    )
    if not isinstance(containers, list):
        raise RuntimeErrorEB(f"{context} container inventory is invalid")
    images: dict[str, str] = {}
    for item in containers:
        if not isinstance(item, dict):
            raise RuntimeErrorEB(f"{context} container inventory is invalid")
        name = str(item.get("name", ""))
        image = str(item.get("image", ""))
        if not name or not image or name in images:
            raise RuntimeErrorEB(f"{context} container identity is invalid")
        images[name] = image
    return images


MIGRATION_JOB_NAME = "commonthing-experiment-b-migration"
_MIGRATION_CONTROLLER_LABELS = frozenset(
    {
        "batch.kubernetes.io/controller-uid",
        "batch.kubernetes.io/job-name",
        "controller-uid",
        "job-name",
    }
)


def _migration_template_metadata_projection(
    metadata: Any,
    context: str,
) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        raise RuntimeErrorEB(f"{context} template metadata is invalid")
    labels = metadata.get("labels", {})
    annotations = metadata.get("annotations", {})
    if not isinstance(labels, dict) or not isinstance(annotations, dict):
        raise RuntimeErrorEB(f"{context} template metadata contract is invalid")
    return {
        "labels": {
            str(key): str(value)
            for key, value in sorted(labels.items())
            if key not in _MIGRATION_CONTROLLER_LABELS
        },
        "annotations": {
            str(key): str(value)
            for key, value in sorted(annotations.items())
        },
    }


def _migration_job_contract(
    job: Any,
    context: str,
) -> dict[str, Any]:
    if not isinstance(job, dict):
        raise RuntimeErrorEB(f"{context} Job payload is invalid")
    spec = job.get("spec", {})
    template = spec.get("template", {}) if isinstance(spec, dict) else {}
    template_metadata = (
        template.get("metadata", {}) if isinstance(template, dict) else {}
    )
    pod_spec = template.get("spec", {}) if isinstance(template, dict) else {}
    if not isinstance(spec, dict) or not isinstance(template, dict):
        raise RuntimeErrorEB(f"{context} Job spec/template is invalid")

    parallelism = spec.get("parallelism", 1)
    completions = spec.get("completions", 1)
    backoff_limit = spec.get("backoffLimit", 6)
    suspend = spec.get("suspend", False)
    manual_selector = spec.get("manualSelector", False)
    if suspend is None:
        suspend = False
    if manual_selector is None:
        manual_selector = False
    if (
        isinstance(parallelism, bool)
        or not isinstance(parallelism, int)
        or parallelism < 1
        or isinstance(completions, bool)
        or not isinstance(completions, int)
        or completions < 1
        or isinstance(backoff_limit, bool)
        or not isinstance(backoff_limit, int)
        or backoff_limit < 0
        or not isinstance(suspend, bool)
        or not isinstance(manual_selector, bool)
    ):
        raise RuntimeErrorEB(f"{context} Job execution contract is invalid")
    metadata_contract = _migration_template_metadata_projection(
        template_metadata, context
    )
    pod_contract = _application_pod_spec_projection(
        pod_spec, f"{context} Pod template"
    )
    return {
        "parallelism": parallelism,
        "completions": completions,
        "backoffLimit": backoff_limit,
        "activeDeadlineSeconds": spec.get("activeDeadlineSeconds"),
        "ttlSecondsAfterFinished": spec.get("ttlSecondsAfterFinished"),
        "completionMode": spec.get("completionMode", "NonIndexed"),
        "suspend": suspend,
        "manualSelector": manual_selector,
        "podFailurePolicy": spec.get("podFailurePolicy"),
        "successPolicy": spec.get("successPolicy"),
        "backoffLimitPerIndex": spec.get("backoffLimitPerIndex"),
        "maxFailedIndexes": spec.get("maxFailedIndexes"),
        "managedBy": spec.get("managedBy"),
        "template_labels": metadata_contract["labels"],
        "template_annotations": metadata_contract["annotations"],
        "pod_spec": pod_contract,
    }


def _rendered_migration_job_contract(
    root: Path,
    api_digest: str,
) -> dict[str, Any]:
    if not DIGEST_RE.fullmatch(api_digest):
        raise RuntimeErrorEB(
            "migration Job contract requires an exact API digest"
        )
    kustomize = toolchain(root)["tools"].get("kustomize")
    if not isinstance(kustomize, str) or not kustomize:
        raise RuntimeErrorEB(
            "migration Job contract requires pinned kustomize"
        )
    rendered = run([kustomize, "build", str(MIGRATION)]).stdout
    rendered = rendered.replace("$" + "{API_DIGEST}", api_digest)
    try:
        documents = [
            document
            for document in yaml.safe_load_all(rendered)
            if isinstance(document, dict)
        ]
    except yaml.YAMLError as exc:
        raise RuntimeErrorEB(
            "rendered Experiment-B migration contract is invalid"
        ) from exc
    matches = [
        document
        for document in documents
        if document.get("kind") == "Job"
        and document.get("metadata", {}).get("name")
        == MIGRATION_JOB_NAME
        and document.get("metadata", {}).get("namespace")
        == APP_NAMESPACE
    ]
    if len(matches) != 1:
        raise RuntimeErrorEB(
            "rendered Experiment-B migration Job is ambiguous"
        )
    contract = _migration_job_contract(
        matches[0], "rendered Experiment-B migration Job"
    )
    return {
        "contract": contract,
        "contract_sha256": _stable_json_sha256(contract),
        "pod_contract_sha256": _stable_json_sha256(
            contract["pod_spec"]
        ),
    }


def _require_migration_job_runtime_contract(
    root: Path,
    migration: Any,
    migration_pods: Any,
    api_digest: str,
) -> dict[str, Any]:
    expected = _rendered_migration_job_contract(root, api_digest)
    if not isinstance(migration, dict):
        raise RuntimeErrorEB("Experiment-B migration Job is invalid")
    metadata = migration.get("metadata", {})
    status = migration.get("status", {})
    if (
        not isinstance(metadata, dict)
        or metadata.get("name") != MIGRATION_JOB_NAME
        or metadata.get("namespace") != APP_NAMESPACE
        or metadata.get("deletionTimestamp") is not None
        or not isinstance(metadata.get("uid"), str)
        or not metadata.get("uid")
        or not isinstance(status, dict)
    ):
        raise RuntimeErrorEB(
            "Experiment-B migration Job identity/status drifted"
        )
    observed_contract = _migration_job_contract(
        migration, "live Experiment-B migration Job"
    )
    if observed_contract != expected["contract"]:
        raise RuntimeErrorEB(
            "Experiment-B migration Job contract drifted"
        )
    complete = any(
        isinstance(condition, dict)
        and condition.get("type") == "Complete"
        and condition.get("status") == "True"
        for condition in status.get("conditions", [])
    )
    succeeded = status.get("succeeded", 0) or 0
    if (
        isinstance(succeeded, bool)
        or not isinstance(succeeded, int)
        or succeeded != expected["contract"]["completions"]
        or not complete
    ):
        raise RuntimeErrorEB(
            "Experiment-B migration Job is not canonically complete"
        )
    if not isinstance(migration_pods, list) or not migration_pods:
        raise RuntimeErrorEB(
            "Experiment-B migration Pod inventory is empty or invalid"
        )
    job_uid = metadata["uid"]
    expected_labels = expected["contract"]["template_labels"]
    expected_annotations = expected["contract"]["template_annotations"]
    expected_pod_spec = expected["contract"]["pod_spec"]
    expected_containers = expected_pod_spec["containers"]
    expected_init = expected_pod_spec["init_containers"]
    pod_names: list[str] = []
    succeeded_pods = 0
    for pod in migration_pods:
        if not isinstance(pod, dict):
            raise RuntimeErrorEB(
                "Experiment-B migration Pod inventory is invalid"
            )
        pod_metadata = pod.get("metadata", {})
        pod_spec = pod.get("spec", {})
        pod_status = pod.get("status", {})
        if (
            not isinstance(pod_metadata, dict)
            or not isinstance(pod_status, dict)
        ):
            raise RuntimeErrorEB(
                "Experiment-B migration Pod metadata/status is invalid"
            )
        pod_name = pod_metadata.get("name")
        if (
            not isinstance(pod_name, str)
            or not pod_name
            or pod_name in pod_names
            or pod_metadata.get("namespace") != APP_NAMESPACE
            or pod_metadata.get("deletionTimestamp") is not None
        ):
            raise RuntimeErrorEB(
                "Experiment-B migration Pod identity drifted"
            )
        owners = pod_metadata.get("ownerReferences", [])
        owner_matches = [
            owner
            for owner in owners
            if isinstance(owner, dict)
            and owner.get("apiVersion") == "batch/v1"
            and owner.get("kind") == "Job"
            and owner.get("name") == MIGRATION_JOB_NAME
            and owner.get("uid") == job_uid
            and owner.get("controller") is True
        ] if isinstance(owners, list) else []
        if len(owner_matches) != 1:
            raise RuntimeErrorEB(
                "Experiment-B migration Pod owner binding drifted"
            )
        pod_metadata_contract = _migration_template_metadata_projection(
            pod_metadata, f"live Experiment-B migration Pod {pod_name}"
        )
        if any(
            pod_metadata_contract["labels"].get(key) != value
            for key, value in expected_labels.items()
        ) or any(
            pod_metadata_contract["annotations"].get(key) != value
            for key, value in expected_annotations.items()
        ):
            raise RuntimeErrorEB(
                "Experiment-B migration Pod metadata contract drifted"
            )
        if (
            _application_pod_spec_projection(
                pod_spec, f"live Experiment-B migration Pod {pod_name}"
            )
            != expected_pod_spec
        ):
            raise RuntimeErrorEB(
                "Experiment-B migration Pod contract drifted"
            )
        phase = pod_status.get("phase")
        if phase not in {"Succeeded", "Failed"}:
            raise RuntimeErrorEB(
                "Experiment-B migration Pod is not terminal"
            )
        if phase == "Succeeded":
            succeeded_pods += 1
            for field, expected_group in (
                ("containerStatuses", expected_containers),
                ("initContainerStatuses", expected_init),
            ):
                statuses = pod_status.get(field, [])
                if statuses is None:
                    statuses = []
                if (
                    not isinstance(statuses, list)
                    or any(not isinstance(item, dict) for item in statuses)
                ):
                    raise RuntimeErrorEB(
                        "Experiment-B migration Pod runtime status is invalid"
                    )
                by_name = {
                    str(item.get("name", "")): item
                    for item in statuses
                    if item.get("name")
                }
                if set(by_name) != set(expected_group):
                    raise RuntimeErrorEB(
                        "Experiment-B migration Pod container set drifted"
                    )
                for name, expected_container in expected_group.items():
                    runtime_status = by_name[name]
                    image = expected_container.get("image")
                    image_id = runtime_status.get("imageID")
                    if (
                        isinstance(image, str)
                        and "@" in image
                        and not _runtime_image_id_matches_digest(
                            image_id, image.rsplit("@", 1)[1]
                        )
                    ):
                        raise RuntimeErrorEB(
                            "Experiment-B migration Pod runtime image drifted"
                        )
                    terminated = (
                        runtime_status.get("state", {}).get("terminated")
                        if isinstance(runtime_status.get("state"), dict)
                        else None
                    )
                    if (
                        not isinstance(terminated, dict)
                        or terminated.get("exitCode") != 0
                    ):
                        raise RuntimeErrorEB(
                            "Experiment-B migration Pod did not terminate cleanly"
                        )
        pod_names.append(pod_name)

    if succeeded_pods != expected["contract"]["completions"]:
        raise RuntimeErrorEB(
            "Experiment-B migration succeeded Pod count drifted"
        )
    return {
        "contract_sha256": expected["contract_sha256"],
        "pod_contract_sha256": expected["pod_contract_sha256"],
        "pod_names": sorted(pod_names),
        "succeeded_pods": succeeded_pods,
        "canonical": True,
    }


def _require_requested_release_artifacts(
    root: Path,
    api: Any,
    web: Any,
    migration: Any,
    migration_pods: Any,
    api_digest: str,
    web_digest: str,
    api_replicas: int,
    web_replicas: int,
) -> dict[str, Any]:
    expected_api = f"ghcr.io/heimgewebe/commonthing-api@{api_digest}"
    expected_web = f"ghcr.io/heimgewebe/commonthing-web@{web_digest}"
    api_images = _container_images(api, "Experiment-B API Deployment")
    web_images = _container_images(web, "Experiment-B Web Deployment")
    migration_images = _container_images(migration, "Experiment-B migration Job")

    if api_images.get("api") != expected_api:
        raise RuntimeErrorEB("live API image does not match immutable release digest")
    if api_images.get("search-worker") != expected_api:
        raise RuntimeErrorEB("live search-worker image does not match API release digest")
    if web_images.get("web") != expected_web:
        raise RuntimeErrorEB("live Web image does not match immutable release digest")
    if migration_images.get("migration") != expected_api:
        raise RuntimeErrorEB("live migration image does not match API release digest")

    deployments = {
        "weltgewebe-api": _deployment_availability_snapshot(
            api, "weltgewebe-api", api_replicas
        ),
        "weltgewebe-web": _deployment_availability_snapshot(
            web, "weltgewebe-web", web_replicas
        ),
    }
    migration_readback = _require_migration_job_runtime_contract(
        root,
        migration,
        migration_pods,
        api_digest,
    )

    return {
        "deployments": deployments,
        "images": {
            "api": api_images.get("api"),
            "web": web_images.get("web"),
            "search_worker": api_images.get("search-worker"),
            "migration": migration_images.get("migration"),
        },
        "migration": migration_readback,
        "migration_complete": True,
    }


def _runtime_image_id_digest(image_id: Any) -> str | None:
    if not isinstance(image_id, str) or not image_id:
        return None
    candidate = image_id
    if "@" in candidate:
        candidate = candidate.rsplit("@", 1)[1]
    elif "://" in candidate:
        candidate = candidate.split("://", 1)[1]
    return candidate if DIGEST_RE.fullmatch(candidate) else None


def _runtime_image_id_matches_digest(image_id: Any, expected_digest: str) -> bool:
    if not DIGEST_RE.fullmatch(expected_digest):
        return False
    observed_digest = _runtime_image_id_digest(image_id)
    return (
        observed_digest is not None
        and secrets.compare_digest(observed_digest, expected_digest)
    )


def _pod_selector_match_labels(workload: Any, context: str) -> dict[str, str]:
    if not isinstance(workload, dict):
        raise RuntimeErrorEB(f"{context} workload is invalid")
    selector = workload.get("spec", {}).get("selector", {})
    if not isinstance(selector, dict):
        raise RuntimeErrorEB(f"{context} selector is invalid")
    labels = selector.get("matchLabels")
    expressions = selector.get("matchExpressions", [])
    if (
        not isinstance(labels, dict)
        or not labels
        or any(
            not isinstance(key, str)
            or not key
            or not isinstance(value, str)
            or not value
            for key, value in labels.items()
        )
        or expressions not in (None, [])
    ):
        raise RuntimeErrorEB(f"{context} selector is not an exact matchLabels contract")
    return {str(key): str(value) for key, value in labels.items()}


def _pods_matching_labels(pods: Any, required_labels: dict[str, str]) -> list[dict[str, Any]]:
    if not isinstance(pods, list) or any(not isinstance(item, dict) for item in pods):
        raise RuntimeErrorEB("Pod inventory is invalid")
    matches: list[dict[str, Any]] = []
    for pod in pods:
        metadata = pod.get("metadata", {})
        labels = metadata.get("labels", {}) if isinstance(metadata, dict) else {}
        if (
            isinstance(labels, dict)
            and all(labels.get(key) == value for key, value in required_labels.items())
        ):
            matches.append(pod)
    return matches


def _pod_runtime_image_ids_sha256(
    pods: Any,
    expected_images: dict[str, dict[str, str]],
) -> str:
    if (
        not isinstance(pods, dict)
        or any(not isinstance(pod, dict) for pod in pods.values())
    ):
        raise RuntimeErrorEB("Pod runtime image-ID binding is invalid")
    binding: dict[str, dict[str, list[str]]] = {
        "containers": {},
        "init_containers": {},
    }
    for group in ("containers", "init_containers"):
        for container_name in expected_images[group]:
            values: list[str] = []
            for pod in pods.values():
                runtime_ids = pod.get("runtime_image_ids", {})
                group_ids = (
                    runtime_ids.get(group, {})
                    if isinstance(runtime_ids, dict)
                    else {}
                )
                image_id = (
                    group_ids.get(container_name)
                    if isinstance(group_ids, dict)
                    else None
                )
                observed_digest = _runtime_image_id_digest(image_id)
                if observed_digest is None:
                    raise RuntimeErrorEB("Pod runtime image-ID binding is incomplete")
                values.append(observed_digest)
            binding[group][container_name] = sorted(values)
    return _stable_json_sha256(binding)


def _require_running_pod_image_contract(
    pods: Any,
    *,
    namespace: str,
    workload: str,
    expected_replicas: int,
    expected_images: dict[str, dict[str, str]],
    required_labels: dict[str, str],
    context: str,
) -> dict[str, Any]:
    if (
        not isinstance(namespace, str)
        or not namespace
        or not isinstance(workload, str)
        or not workload
        or not isinstance(expected_replicas, int)
        or isinstance(expected_replicas, bool)
        or expected_replicas < 1
        or not isinstance(expected_images, dict)
        or set(expected_images) != {"containers", "init_containers"}
        or not isinstance(expected_images.get("containers"), dict)
        or not expected_images["containers"]
        or not isinstance(expected_images.get("init_containers"), dict)
        or not isinstance(required_labels, dict)
        or not required_labels
    ):
        raise RuntimeErrorEB(f"{context} contract is invalid: {workload}")
    for group in ("containers", "init_containers"):
        if any(
            not isinstance(name, str)
            or not name
            or not isinstance(image, str)
            or not image
            for name, image in expected_images[group].items()
        ):
            raise RuntimeErrorEB(f"{context} image contract is invalid: {workload}/{group}")
    if not isinstance(pods, list) or len(pods) != expected_replicas:
        raise RuntimeErrorEB(
            f"{context} set does not match exact replica contract: {workload}"
        )

    expected_images_sha256 = _stable_json_sha256(expected_images)
    expected_digests: dict[str, dict[str, str | None]] = {
        "containers": {},
        "init_containers": {},
    }
    for group in ("containers", "init_containers"):
        for name, image in expected_images[group].items():
            digest: str | None = None
            if "@" in image:
                _repository, candidate = image.rsplit("@", 1)
                if not DIGEST_RE.fullmatch(candidate):
                    raise RuntimeErrorEB(
                        f"{context} image digest is invalid: {workload}/{name}"
                    )
                digest = candidate
            expected_digests[group][name] = digest

    observed: dict[str, Any] = {}
    for pod in pods:
        if not isinstance(pod, dict):
            raise RuntimeErrorEB(f"{context} inventory is invalid: {workload}")
        metadata = pod.get("metadata", {})
        spec = pod.get("spec", {})
        status_obj = pod.get("status", {})
        if (
            not isinstance(metadata, dict)
            or not isinstance(spec, dict)
            or not isinstance(status_obj, dict)
        ):
            raise RuntimeErrorEB(f"{context} inventory is invalid: {workload}")
        name = metadata.get("name")
        labels = metadata.get("labels", {})
        if (
            not isinstance(name, str)
            or not name
            or name in observed
            or metadata.get("namespace") != namespace
            or metadata.get("deletionTimestamp") is not None
            or not isinstance(labels, dict)
            or not all(labels.get(key) == value for key, value in required_labels.items())
            or status_obj.get("phase") != "Running"
        ):
            raise RuntimeErrorEB(f"{context} identity/state drifted: {workload}")

        ready = any(
            isinstance(condition, dict)
            and condition.get("type") == "Ready"
            and condition.get("status") == "True"
            for condition in status_obj.get("conditions", [])
        )
        if not ready:
            raise RuntimeErrorEB(f"{context} is not Ready: {workload}/{name}")

        live_images = _pod_spec_images(spec, f"{context} {name}")
        if live_images != expected_images:
            raise RuntimeErrorEB(
                f"{context} requested images drifted: {workload}/{name}"
            )

        runtime_image_ids: dict[str, dict[str, str]] = {
            "containers": {},
            "init_containers": {},
        }
        for status_field, group, must_be_running in (
            ("containerStatuses", "containers", True),
            ("initContainerStatuses", "init_containers", False),
        ):
            statuses = status_obj.get(status_field, [])
            if not isinstance(statuses, list):
                raise RuntimeErrorEB(
                    f"{context} container status is invalid: {workload}/{name}/{group}"
                )
            status_by_name: dict[str, dict[str, Any]] = {}
            for item in statuses:
                if not isinstance(item, dict):
                    raise RuntimeErrorEB(
                        f"{context} container status is invalid: {workload}/{name}/{group}"
                    )
                container_name = item.get("name")
                if (
                    not isinstance(container_name, str)
                    or not container_name
                    or container_name in status_by_name
                ):
                    raise RuntimeErrorEB(
                        f"{context} container status identity is invalid: "
                        f"{workload}/{name}/{group}"
                    )
                status_by_name[container_name] = item
            if set(status_by_name) != set(expected_images[group]):
                raise RuntimeErrorEB(
                    f"{context} container status set drifted: {workload}/{name}/{group}"
                )

            for container_name, requested_image in expected_images[group].items():
                item = status_by_name[container_name]
                state = item.get("state", {})
                image_id = item.get("imageID")
                observed_digest = _runtime_image_id_digest(image_id)
                expected_digest = expected_digests[group][container_name]
                state_valid = False
                if isinstance(state, dict):
                    if must_be_running:
                        state_valid = (
                            item.get("ready") is True
                            and isinstance(state.get("running"), dict)
                        )
                    else:
                        terminated = state.get("terminated")
                        state_valid = (
                            isinstance(state.get("running"), dict)
                            or (
                                isinstance(terminated, dict)
                                and terminated.get("exitCode") == 0
                            )
                        )
                if (
                    not state_valid
                    or observed_digest is None
                    or (
                        expected_digest is not None
                        and not secrets.compare_digest(observed_digest, expected_digest)
                    )
                ):
                    raise RuntimeErrorEB(
                        f"{context} runtime image ID drifted: "
                        f"{workload}/{name}/{container_name}"
                    )
                runtime_image_ids[group][container_name] = str(image_id)

        observed[name] = {
            "ready": True,
            "requested_images_sha256": expected_images_sha256,
            "runtime_image_ids": runtime_image_ids,
        }

    return {
        "expected_replicas": expected_replicas,
        "observed_replicas": len(observed),
        "requested_images_sha256": expected_images_sha256,
        "runtime_image_ids_sha256": _pod_runtime_image_ids_sha256(
            observed, expected_images
        ),
        "images_canonical": True,
        "pods": observed,
    }


def _require_running_pod_images(
    pods: Any,
    workload: str,
    expected_replicas: int,
    expected_images: dict[str, str],
) -> dict[str, Any]:
    result = _require_running_pod_image_contract(
        pods,
        namespace=APP_NAMESPACE,
        workload=workload,
        expected_replicas=expected_replicas,
        expected_images={"containers": expected_images, "init_containers": {}},
        required_labels={"app.kubernetes.io/name": workload},
        context="application Pod",
    )
    flat_sha256 = _stable_json_sha256(expected_images)
    result["requested_images_sha256"] = flat_sha256
    result.pop("runtime_image_ids_sha256", None)
    for pod in result["pods"].values():
        pod["requested_images_sha256"] = flat_sha256
        pod["runtime_image_ids"] = pod["runtime_image_ids"]["containers"]
    return result


def _expected_live_secret_values(
    root: Path, source_commit: str
) -> dict[str, Any]:
    receipt_path = root / "receipts/secrets.json"
    database_path = root / "secrets/database.json"
    registry_path = root / "secrets/registry.json"
    if not database_path.is_file() or database_path.is_symlink():
        raise RuntimeErrorEB(
            "Experiment-B status requires private database Secret source material"
        )
    if not registry_path.is_file() or registry_path.is_symlink():
        raise RuntimeErrorEB(
            "Experiment-B status requires private registry Secret source material"
        )
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        database = json.loads(database_path.read_text(encoding="utf-8"))
        registry_bytes = registry_path.read_bytes()
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeErrorEB(
            "Experiment-B status requires valid private Secret source material"
        ) from exc

    if not isinstance(receipt, dict) or (
        receipt.get("schema_version") != 1
        or receipt.get("status") != "ready"
        or receipt.get("source_commit") != source_commit
        or receipt.get("database_secret") != "commonthing-experiment-b-database"
        or receipt.get("runtime_secret") != "weltgewebe-runtime"
        or receipt.get("registry_secret") != "commonthing-experiment-b-registry"
        or receipt.get("secret_values_recorded") is not False
    ):
        raise RuntimeErrorEB("Experiment-B Secret receipt binding drifted")

    database_source_sha256 = receipt.get("database_source_sha256")
    if (
        not isinstance(database_source_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", database_source_sha256) is None
    ):
        raise RuntimeErrorEB("Experiment-B database Secret source digest is invalid")
    if not secrets.compare_digest(sha256_file(database_path), database_source_sha256):
        raise RuntimeErrorEB("Experiment-B database Secret source digest drifted")

    registry_source_sha256 = receipt.get("registry_source_sha256")
    if (
        not isinstance(registry_source_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", registry_source_sha256) is None
    ):
        raise RuntimeErrorEB("Experiment-B registry Secret source digest is invalid")
    if not secrets.compare_digest(sha256_file(registry_path), registry_source_sha256):
        raise RuntimeErrorEB("Experiment-B registry Secret source digest drifted")

    expected_database_keys = {"username", "database", "password"}
    if (
        not isinstance(database, dict)
        or set(database) != expected_database_keys
        or any(
            not isinstance(database.get(key), str) or not database[key]
            for key in expected_database_keys
        )
    ):
        raise RuntimeErrorEB("Experiment-B database Secret source material is invalid")

    database_url = (
        f"postgresql://{database['username']}:{database['password']}"
        f"@postgres.{DATA_NAMESPACE}.svc.cluster.local:5432/{database['database']}"
    )
    return {
        "database": {
            key: database[key].encode("utf-8")
            for key in expected_database_keys
        },
        "runtime": {"database-url": database_url.encode("utf-8")},
        "registry": {".dockerconfigjson": registry_bytes},
    }


def _require_live_secret(
    secret: Any,
    namespace: str,
    name: str,
    secret_type: str,
    required_keys: set[str],
    expected_values: dict[str, bytes] | None,
) -> None:
    if expected_values is not None and (
        set(expected_values) != required_keys
        or any(
            not isinstance(value, bytes) or not value
            for value in expected_values.values()
        )
    ):
        raise RuntimeErrorEB(
            f"Experiment-B Secret expected-content contract is invalid: {namespace}/{name}"
        )
    if not isinstance(secret, dict):
        raise RuntimeErrorEB(f"Experiment-B Secret is not an object: {namespace}/{name}")
    metadata = secret.get("metadata", {})
    if not isinstance(metadata, dict):
        raise RuntimeErrorEB(f"Experiment-B Secret metadata is invalid: {namespace}/{name}")
    if (
        metadata.get("name") != name
        or metadata.get("namespace") != namespace
        or metadata.get("deletionTimestamp")
    ):
        raise RuntimeErrorEB(
            f"Experiment-B Secret identity/deletion state is invalid: {namespace}/{name}"
        )
    if secret.get("type") != secret_type:
        raise RuntimeErrorEB(f"Experiment-B Secret type drifted: {namespace}/{name}")
    data = secret.get("data", {})
    if not isinstance(data, dict):
        raise RuntimeErrorEB(f"Experiment-B Secret data shape is invalid: {namespace}/{name}")
    present_keys = {
        str(key)
        for key, value in data.items()
        if isinstance(key, str) and isinstance(value, str) and bool(value)
    }
    missing = sorted(required_keys - present_keys)
    if missing:
        raise RuntimeErrorEB(
            f"Experiment-B Secret is missing required keys: {namespace}/{name}: {missing}"
        )
    for key in sorted(required_keys):
        try:
            decoded = base64.b64decode(data[key], validate=True)
        except (ValueError, binascii.Error) as exc:
            raise RuntimeErrorEB(
                f"Experiment-B Secret contains invalid encoded data: {namespace}/{name}/{key}"
            ) from exc
        if (
            expected_values is not None
            and not secrets.compare_digest(decoded, expected_values[key])
        ):
            raise RuntimeErrorEB(
                f"Experiment-B Secret content drifted: {namespace}/{name}/{key}"
            )
    return None


def _verified_secret_readback(
    namespace: str,
    name: str,
    secret_type: str,
    required_keys: set[str],
    *,
    content_verified: bool = True,
) -> dict[str, Any]:
    return {
        "namespace": namespace,
        "name": name,
        "type": secret_type,
        "required_keys": sorted(required_keys),
        "present": True,
        "terminating": False,
        "content_verified": content_verified,
    }


def _require_gateway_ready(gateway: Any) -> dict[str, Any]:
    if not isinstance(gateway, dict):
        raise RuntimeErrorEB("Experiment-B Gateway payload is not an object")
    metadata = gateway.get("metadata", {})
    spec = gateway.get("spec", {})
    if not isinstance(metadata, dict) or not isinstance(spec, dict):
        raise RuntimeErrorEB("Experiment-B Gateway metadata/spec is invalid")
    if (
        metadata.get("name") != "commonthing-experiment-b"
        or metadata.get("namespace") != APP_NAMESPACE
        or metadata.get("deletionTimestamp") is not None
        or spec.get("gatewayClassName") != "cilium"
    ):
        raise RuntimeErrorEB("Experiment-B Gateway identity/class drifted")

    listeners = spec.get("listeners")
    if (
        not isinstance(listeners, list)
        or len(listeners) != 1
        or not isinstance(listeners[0], dict)
    ):
        raise RuntimeErrorEB("Experiment-B Gateway listener set drifted")
    listener = listeners[0]
    allowed = listener.get("allowedRoutes", {})
    namespaces = allowed.get("namespaces", {}) if isinstance(allowed, dict) else {}
    kinds = allowed.get("kinds", []) if isinstance(allowed, dict) else []
    expected_kind = {
        "group": "gateway.networking.k8s.io",
        "kind": "HTTPRoute",
    }
    if (
        listener.get("name") != "http"
        or listener.get("protocol") != "HTTP"
        or listener.get("port") != 80
        or not isinstance(namespaces, dict)
        or namespaces.get("from") != "Same"
        or not isinstance(kinds, list)
        or kinds != [expected_kind]
    ):
        raise RuntimeErrorEB("Experiment-B Gateway listener contract drifted")

    generation = _require_current_condition(
        gateway, "Programmed", "Experiment-B Gateway"
    )
    return {
        "generation": generation,
        "gateway_class": "cilium",
        "listener": "http",
        "programmed": True,
    }


def _require_httproute_ready(route: Any) -> dict[str, Any]:
    if not isinstance(route, dict):
        raise RuntimeErrorEB("Experiment-B HTTPRoute payload is not an object")

    metadata = route.get("metadata", {})
    spec = route.get("spec", {})
    if not isinstance(metadata, dict) or not isinstance(spec, dict):
        raise RuntimeErrorEB("Experiment-B HTTPRoute metadata/spec is invalid")
    if (
        metadata.get("name") != "commonthing-experiment-b"
        or metadata.get("namespace") != APP_NAMESPACE
        or metadata.get("deletionTimestamp") is not None
    ):
        raise RuntimeErrorEB("Experiment-B HTTPRoute identity is invalid")
    generation = int(metadata.get("generation") or 0)
    if generation < 1:
        raise RuntimeErrorEB("Experiment-B HTTPRoute generation is invalid")

    expected_parent = {
        "name": "commonthing-experiment-b",
        "namespace": APP_NAMESPACE,
        "sectionName": "http",
    }
    parent_refs = spec.get("parentRefs")
    if (
        not isinstance(parent_refs, list)
        or len(parent_refs) != 1
        or not isinstance(parent_refs[0], dict)
    ):
        raise RuntimeErrorEB("Experiment-B HTTPRoute parentRef drifted")
    spec_parent = parent_refs[0]
    normalized_spec_parent = {
        "name": str(spec_parent.get("name", "")),
        "namespace": str(spec_parent.get("namespace") or APP_NAMESPACE),
        "sectionName": str(spec_parent.get("sectionName", "")),
    }
    if (
        normalized_spec_parent != expected_parent
        or str(spec_parent.get("group") or "gateway.networking.k8s.io")
        != "gateway.networking.k8s.io"
        or str(spec_parent.get("kind") or "Gateway") != "Gateway"
    ):
        raise RuntimeErrorEB("Experiment-B HTTPRoute parentRef drifted")

    expected_rules = [
        {
            "paths": ["/health", "/api"],
            "backend": {"name": "weltgewebe-api", "port": 8080},
        },
        {
            "paths": ["/"],
            "backend": {"name": "weltgewebe-web", "port": 8080},
        },
    ]
    rules = spec.get("rules")
    if not isinstance(rules, list) or len(rules) != len(expected_rules):
        raise RuntimeErrorEB("Experiment-B HTTPRoute rule set drifted")
    normalized_rules: list[dict[str, Any]] = []
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) - {"matches", "backendRefs"}:
            raise RuntimeErrorEB("Experiment-B HTTPRoute rule shape drifted")
        matches = rule.get("matches")
        backend_refs = rule.get("backendRefs")
        if not isinstance(matches, list) or not matches:
            raise RuntimeErrorEB("Experiment-B HTTPRoute matches drifted")
        if (
            not isinstance(backend_refs, list)
            or len(backend_refs) != 1
            or not isinstance(backend_refs[0], dict)
        ):
            raise RuntimeErrorEB("Experiment-B HTTPRoute backend set drifted")

        paths: list[str] = []
        for match in matches:
            if not isinstance(match, dict) or set(match) != {"path"}:
                raise RuntimeErrorEB("Experiment-B HTTPRoute match shape drifted")
            path = match.get("path")
            if (
                not isinstance(path, dict)
                or set(path) != {"type", "value"}
                or path.get("type") != "PathPrefix"
                or not isinstance(path.get("value"), str)
            ):
                raise RuntimeErrorEB("Experiment-B HTTPRoute path match drifted")
            paths.append(path["value"])

        backend = backend_refs[0]
        if set(backend) - {"group", "kind", "name", "namespace", "port", "weight"}:
            raise RuntimeErrorEB("Experiment-B HTTPRoute backend shape drifted")
        weight = backend.get("weight", 1)
        if (
            str(backend.get("group") or "") != ""
            or str(backend.get("kind") or "Service") != "Service"
            or str(backend.get("namespace") or APP_NAMESPACE) != APP_NAMESPACE
            or not isinstance(weight, int)
            or isinstance(weight, bool)
            or weight != 1
            or not isinstance(backend.get("name"), str)
            or not isinstance(backend.get("port"), int)
            or isinstance(backend.get("port"), bool)
        ):
            raise RuntimeErrorEB("Experiment-B HTTPRoute backend contract drifted")
        normalized_rules.append(
            {
                "paths": paths,
                "backend": {
                    "name": backend["name"],
                    "port": backend["port"],
                },
            }
        )
    if normalized_rules != expected_rules:
        raise RuntimeErrorEB("Experiment-B HTTPRoute routing contract drifted")

    status_obj = route.get("status", {})
    parents = status_obj.get("parents", []) if isinstance(status_obj, dict) else []
    if not isinstance(parents, list):
        raise RuntimeErrorEB("Experiment-B HTTPRoute parent status is invalid")

    matching: list[dict[str, Any]] = []
    for parent in parents:
        if not isinstance(parent, dict):
            raise RuntimeErrorEB("Experiment-B HTTPRoute parent status contains a non-object")
        parent_ref = parent.get("parentRef", {})
        if not isinstance(parent_ref, dict):
            raise RuntimeErrorEB("Experiment-B HTTPRoute status parentRef is invalid")
        normalized_parent = {
            "name": str(parent_ref.get("name", "")),
            "namespace": str(parent_ref.get("namespace") or APP_NAMESPACE),
            "sectionName": str(parent_ref.get("sectionName", "")),
        }
        if (
            normalized_parent != expected_parent
            or parent.get("controllerName") != "io.cilium/gateway-controller"
        ):
            continue
        conditions = parent.get("conditions", [])
        if not isinstance(conditions, list):
            raise RuntimeErrorEB("Experiment-B HTTPRoute conditions are invalid")

        observed: dict[str, dict[str, Any]] = {}
        for condition in conditions:
            if not isinstance(condition, dict):
                continue
            condition_type = str(condition.get("type", ""))
            if condition_type not in {"Accepted", "ResolvedRefs"}:
                continue
            if condition.get("status") != "True":
                continue
            try:
                observed_generation = int(condition.get("observedGeneration"))
            except (TypeError, ValueError):
                continue
            if observed_generation != generation:
                continue
            observed[condition_type] = {
                "status": "True",
                "observed_generation": observed_generation,
            }
        if set(observed) == {"Accepted", "ResolvedRefs"}:
            matching.append(
                {
                    "controller_name": str(parent.get("controllerName", "")),
                    "parent_ref": normalized_parent,
                    "conditions": observed,
                }
            )

    if len(matching) != 1:
        raise RuntimeErrorEB(
            "Experiment-B HTTPRoute is not currently Accepted with ResolvedRefs"
        )
    return {
        "generation": generation,
        "accepted": True,
        "resolved_refs": True,
        **matching[0],
    }


def _final_recovery_state_readback(
    root: Path, source_commit: str
) -> dict[str, Any]:
    if not COMMIT_RE.fullmatch(source_commit):
        raise RuntimeErrorEB("final recovery-state source commit is not exact")
    recovery_failed_path = root / "receipts/recovery-failed.json"
    if recovery_failed_path.is_file():
        raise RuntimeErrorEB(
            "Experiment-B status is blocked by the latest failed recovery attempt"
        )
    recovery_path = root / "receipts/recovery.json"
    if not recovery_path.is_file():
        raise RuntimeErrorEB("Experiment-B status requires recovery receipt")
    try:
        recovery = json.loads(recovery_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeErrorEB("Experiment-B recovery receipt is not valid JSON") from exc
    if (
        not isinstance(recovery, dict)
        or recovery.get("status") != "pass"
        or recovery.get("source_commit") != source_commit
        or recovery.get("rpo_seconds") != 0
        or recovery.get("database_before") != recovery.get("database_after")
        or recovery.get("jetstream_before") != recovery.get("jetstream_after")
    ):
        raise RuntimeErrorEB("Experiment-B recovery receipt binding is invalid")

    current_database = _database_signature(root)
    current_jetstream = _jetstream_signature(root)
    if current_database != recovery.get("database_after"):
        raise RuntimeErrorEB("Experiment-B database/search state drifted after recovery")
    if current_jetstream != recovery.get("jetstream_after"):
        raise RuntimeErrorEB("Experiment-B JetStream state drifted after recovery")

    fixture_path = root / "receipts/t048-fixture.json"
    fixture = _validated_t048_fixture_receipt(root, source_commit)
    return {
        "recovery_receipt_sha256": sha256_file(recovery_path),
        "fixture_receipt_sha256": sha256_file(fixture_path),
        "rpo_seconds": 0,
        "database_signature": current_database,
        "jetstream_signature": current_jetstream,
        "fixture_live_binding": fixture.get("live_binding"),
    }


def _pod_spec_images(pod_spec: Any, context: str) -> dict[str, dict[str, str]]:
    if not isinstance(pod_spec, dict):
        raise RuntimeErrorEB(f"{context} pod spec is invalid")
    result: dict[str, dict[str, str]] = {}
    for field, output_key in (
        ("containers", "containers"),
        ("initContainers", "init_containers"),
    ):
        items = pod_spec.get(field, [])
        if not isinstance(items, list):
            raise RuntimeErrorEB(f"{context} {field} inventory is invalid")
        images: dict[str, str] = {}
        for item in items:
            if not isinstance(item, dict):
                raise RuntimeErrorEB(f"{context} {field} inventory is invalid")
            name = item.get("name")
            image = item.get("image")
            if (
                not isinstance(name, str)
                or not name
                or not isinstance(image, str)
                or not image
                or name in images
            ):
                raise RuntimeErrorEB(f"{context} {field} image binding is invalid")
            images[name] = image
        result[output_key] = images
    if not result["containers"]:
        raise RuntimeErrorEB(f"{context} has no containers")
    return result


def _versioned_namespace_security_contract() -> dict[str, Any]:
    namespace_path = NAMESPACES / "namespaces.yaml"
    kustomization_path = NAMESPACES / "kustomization.yaml"
    try:
        documents = [
            item
            for item in yaml.safe_load_all(
                namespace_path.read_text(encoding="utf-8")
            )
            if isinstance(item, dict)
        ]
        kustomization = yaml.safe_load(
            kustomization_path.read_text(encoding="utf-8")
        )
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeErrorEB(
            "versioned Experiment-B Namespace contract is invalid"
        ) from exc
    expected_names = {APP_NAMESPACE, DATA_NAMESPACE}
    namespaces = [
        document
        for document in documents
        if document.get("kind") == "Namespace"
        and document.get("metadata", {}).get("name") in expected_names
    ]
    if (
        len(namespaces) != len(expected_names)
        or {
            str(item.get("metadata", {}).get("name", ""))
            for item in namespaces
        }
        != expected_names
        or not isinstance(kustomization, dict)
    ):
        raise RuntimeErrorEB(
            "versioned Experiment-B Namespace set is incomplete or duplicated"
        )

    common_labels: dict[str, str] = {}
    label_blocks = kustomization.get("labels", [])
    if not isinstance(label_blocks, list):
        raise RuntimeErrorEB(
            "Experiment-B Namespace Kustomize labels are invalid"
        )
    for block in label_blocks:
        pairs = block.get("pairs") if isinstance(block, dict) else None
        if (
            not isinstance(pairs, dict)
            or any(
                not isinstance(key, str)
                or not key
                or not isinstance(value, str)
                or not value
                for key, value in pairs.items()
            )
        ):
            raise RuntimeErrorEB(
                "Experiment-B Namespace Kustomize label block is invalid"
            )
        for key, value in pairs.items():
            if key in common_labels and common_labels[key] != value:
                raise RuntimeErrorEB(
                    "Experiment-B Namespace Kustomize labels conflict"
                )
            common_labels[str(key)] = str(value)

    result: dict[str, Any] = {}
    for namespace in namespaces:
        metadata = namespace.get("metadata", {})
        name = str(metadata.get("name", ""))
        labels = metadata.get("labels", {})
        if (
            not isinstance(labels, dict)
            or any(
                not isinstance(key, str)
                or not key
                or not isinstance(value, str)
                or not value
                for key, value in labels.items()
            )
        ):
            raise RuntimeErrorEB(
                f"versioned Namespace labels are invalid: {name}"
            )
        expected_labels = {
            **{str(key): str(value) for key, value in labels.items()},
            **common_labels,
            "kubernetes.io/metadata.name": name,
        }
        for required in (
            "pod-security.kubernetes.io/enforce",
            "pod-security.kubernetes.io/audit",
            "pod-security.kubernetes.io/warn",
        ):
            if expected_labels.get(required) != "restricted":
                raise RuntimeErrorEB(
                    f"versioned Namespace loses restricted Pod Security: {name}"
                )
        result[name] = {
            "labels": expected_labels,
            "labels_sha256": _stable_json_sha256(expected_labels),
        }
    return result


def _require_live_namespace_security_contract(root: Path) -> dict[str, Any]:
    expected = _versioned_namespace_security_contract()
    result: dict[str, Any] = {}
    for name, contract_value in expected.items():
        namespace = _kubectl_json(root, ["get", "namespace", name])
        metadata = (
            namespace.get("metadata", {})
            if isinstance(namespace, dict)
            else {}
        )
        labels = metadata.get("labels", {}) if isinstance(metadata, dict) else {}
        if (
            not isinstance(metadata, dict)
            or metadata.get("name") != name
            or metadata.get("deletionTimestamp") is not None
            or not isinstance(labels, dict)
            or labels != contract_value["labels"]
        ):
            raise RuntimeErrorEB(
                f"live Namespace security labels drifted: {name}"
            )
        result[name] = {
            "labels": {str(key): str(value) for key, value in labels.items()},
            "labels_sha256": contract_value["labels_sha256"],
            "canonical": True,
        }
    return result


def _service_spec_projection(service: Any, context: str) -> dict[str, Any]:
    if not isinstance(service, dict):
        raise RuntimeErrorEB(f"{context} Service payload is invalid")
    spec = service.get("spec", {})
    if not isinstance(spec, dict):
        raise RuntimeErrorEB(f"{context} Service spec is invalid")
    selector = spec.get("selector")
    ports = spec.get("ports")
    service_type = spec.get("type", "ClusterIP")
    external_ips = spec.get("externalIPs") or []
    load_balancer_source_ranges = spec.get("loadBalancerSourceRanges") or []
    external_traffic_policy = spec.get("externalTrafficPolicy")
    internal_traffic_policy = spec.get("internalTrafficPolicy", "Cluster")
    session_affinity = spec.get("sessionAffinity", "None")
    session_affinity_config = spec.get("sessionAffinityConfig")
    publish_not_ready = spec.get("publishNotReadyAddresses", False)
    allocate_lb_node_ports = spec.get("allocateLoadBalancerNodePorts")
    load_balancer_class = spec.get("loadBalancerClass")
    load_balancer_ip = spec.get("loadBalancerIP")
    external_name = spec.get("externalName")
    traffic_distribution = spec.get("trafficDistribution")
    health_check_node_port = spec.get("healthCheckNodePort")
    if (
        not isinstance(selector, dict)
        or not selector
        or any(
            not isinstance(key, str)
            or not key
            or not isinstance(value, str)
            or not value
            for key, value in selector.items()
        )
        or not isinstance(ports, list)
        or not ports
        or service_type not in {"ClusterIP", "NodePort", "LoadBalancer"}
        or not isinstance(external_ips, list)
        or any(not isinstance(value, str) or not value for value in external_ips)
        or len(set(external_ips)) != len(external_ips)
        or not isinstance(load_balancer_source_ranges, list)
        or any(
            not isinstance(value, str) or not value
            for value in load_balancer_source_ranges
        )
        or len(set(load_balancer_source_ranges))
        != len(load_balancer_source_ranges)
        or external_traffic_policy not in {None, "Cluster", "Local"}
        or internal_traffic_policy not in {"Cluster", "Local"}
        or session_affinity not in {"None", "ClientIP"}
        or (
            session_affinity_config is not None
            and not isinstance(session_affinity_config, dict)
        )
        or not isinstance(publish_not_ready, bool)
        or (
            allocate_lb_node_ports is not None
            and not isinstance(allocate_lb_node_ports, bool)
        )
        or (
            load_balancer_class is not None
            and (
                not isinstance(load_balancer_class, str)
                or not load_balancer_class
            )
        )
        or (
            load_balancer_ip is not None
            and (not isinstance(load_balancer_ip, str) or not load_balancer_ip)
        )
        or (
            external_name is not None
            and (not isinstance(external_name, str) or not external_name)
        )
        or (
            traffic_distribution is not None
            and (
                not isinstance(traffic_distribution, str)
                or not traffic_distribution
            )
        )
        or (
            health_check_node_port is not None
            and (
                isinstance(health_check_node_port, bool)
                or not isinstance(health_check_node_port, int)
                or health_check_node_port < 1
                or health_check_node_port > 65535
            )
        )
    ):
        raise RuntimeErrorEB(
            f"{context} Service selector/exposure contract is invalid"
        )

    normalized_ports: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for item in ports:
        if not isinstance(item, dict):
            raise RuntimeErrorEB(f"{context} Service port inventory is invalid")
        name = item.get("name")
        port = item.get("port")
        target_port = item.get("targetPort")
        protocol = item.get("protocol", "TCP")
        app_protocol = item.get("appProtocol")
        node_port = item.get("nodePort")
        if (
            not isinstance(name, str)
            or not name
            or name in seen_names
            or isinstance(port, bool)
            or not isinstance(port, int)
            or port < 1
            or port > 65535
            or (
                isinstance(target_port, bool)
                or not isinstance(target_port, (int, str))
                or isinstance(target_port, str)
                and not target_port
            )
            or protocol not in {"TCP", "UDP", "SCTP"}
            or (
                app_protocol is not None
                and (not isinstance(app_protocol, str) or not app_protocol)
            )
            or (
                node_port is not None
                and (
                    isinstance(node_port, bool)
                    or not isinstance(node_port, int)
                    or node_port < 1
                    or node_port > 65535
                )
            )
        ):
            raise RuntimeErrorEB(f"{context} Service port contract is invalid")
        seen_names.add(name)
        normalized_ports.append(
            {
                "name": name,
                "port": port,
                "targetPort": target_port,
                "protocol": protocol,
                "appProtocol": app_protocol,
                "nodePort": node_port,
            }
        )

    return {
        "type": service_type,
        "headless": spec.get("clusterIP") == "None",
        "selector": {
            str(key): str(value)
            for key, value in sorted(selector.items())
        },
        "ports": sorted(normalized_ports, key=lambda value: value["name"]),
        "externalIPs": sorted(external_ips),
        "externalTrafficPolicy": external_traffic_policy,
        "internalTrafficPolicy": internal_traffic_policy,
        "publishNotReadyAddresses": publish_not_ready,
        "sessionAffinity": session_affinity,
        "sessionAffinityConfig": json.loads(
            json.dumps(session_affinity_config)
        )
        if session_affinity_config is not None
        else None,
        "loadBalancerSourceRanges": sorted(load_balancer_source_ranges),
        "loadBalancerClass": load_balancer_class,
        "loadBalancerIP": load_balancer_ip,
        "allocateLoadBalancerNodePorts": allocate_lb_node_ports,
        "healthCheckNodePort": health_check_node_port,
        "externalName": external_name,
        "trafficDistribution": traffic_distribution,
    }


def _versioned_data_service_contract(path: Path, name: str) -> dict[str, Any]:
    try:
        documents = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeErrorEB(
            f"versioned data Service manifest is invalid: {name}"
        ) from exc
    matches = [
        document
        for document in documents
        if isinstance(document, dict)
        and document.get("kind") == "Service"
        and document.get("metadata", {}).get("name") == name
        and document.get("metadata", {}).get("namespace") == DATA_NAMESPACE
    ]
    if len(matches) != 1:
        raise RuntimeErrorEB(
            f"versioned data manifest does not contain exactly one Service: {name}"
        )
    projection = _service_spec_projection(
        matches[0], f"versioned data Service {name}"
    )
    return {
        "spec": projection,
        "spec_sha256": _stable_json_sha256(projection),
    }


def _require_live_data_services(root: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in ("postgres", "nats"):
        expected = _versioned_data_service_contract(
            CLUSTER / f"data/{name}.yaml", name
        )
        service = _kubectl_json(
            root, ["-n", DATA_NAMESPACE, "get", "service", name]
        )
        metadata = (
            service.get("metadata", {}) if isinstance(service, dict) else {}
        )
        if (
            not isinstance(metadata, dict)
            or metadata.get("name") != name
            or metadata.get("namespace") != DATA_NAMESPACE
            or metadata.get("deletionTimestamp") is not None
        ):
            raise RuntimeErrorEB(f"live data Service identity drifted: {name}")
        observed = _service_spec_projection(
            service, f"live data Service {name}"
        )
        if observed != expected["spec"]:
            raise RuntimeErrorEB(
                f"live data Service spec drifted from versioned manifest: {name}"
            )
        result[name] = {
            "spec": observed,
            "spec_sha256": expected["spec_sha256"],
            "canonical": True,
        }
    return result



def _rendered_application_service_contract(
    root: Path,
    release: dict[str, Any],
) -> dict[str, Any]:
    api_digest = release.get("api_digest") if isinstance(release, dict) else None
    web_digest = release.get("web_digest") if isinstance(release, dict) else None
    if (
        not isinstance(api_digest, str)
        or not DIGEST_RE.fullmatch(api_digest)
        or not isinstance(web_digest, str)
        or not DIGEST_RE.fullmatch(web_digest)
    ):
        raise RuntimeErrorEB(
            "application Service contract requires exact release digests"
        )
    kustomize = toolchain(root)["tools"].get("kustomize")
    if not isinstance(kustomize, str) or not kustomize:
        raise RuntimeErrorEB(
            "application Service contract requires pinned kustomize"
        )
    rendered = run([kustomize, "build", str(APP_OVERLAY)]).stdout
    rendered = rendered.replace("${API_DIGEST}", api_digest).replace(
        "${WEB_DIGEST}", web_digest
    )
    try:
        documents = [
            document
            for document in yaml.safe_load_all(rendered)
            if isinstance(document, dict)
        ]
    except yaml.YAMLError as exc:
        raise RuntimeErrorEB(
            "rendered Experiment-B application Service contract is invalid"
        ) from exc

    result: dict[str, Any] = {}
    for name in ("weltgewebe-api", "weltgewebe-web"):
        matches = [
            document
            for document in documents
            if document.get("kind") == "Service"
            and document.get("metadata", {}).get("name") == name
            and document.get("metadata", {}).get("namespace") == APP_NAMESPACE
        ]
        if len(matches) != 1:
            raise RuntimeErrorEB(
                f"rendered application Service is ambiguous: {name}"
            )
        projection = _service_spec_projection(
            matches[0], f"rendered application Service {name}"
        )
        result[name] = {
            "spec": projection,
            "spec_sha256": _stable_json_sha256(projection),
        }
    return result


def _require_live_application_services(
    root: Path,
    release: dict[str, Any],
) -> dict[str, Any]:
    expected = _rendered_application_service_contract(root, release)
    result: dict[str, Any] = {}
    for name, expected_value in expected.items():
        service = _kubectl_json(
            root, ["-n", APP_NAMESPACE, "get", "service", name]
        )
        metadata = service.get("metadata", {}) if isinstance(service, dict) else {}
        if (
            not isinstance(metadata, dict)
            or metadata.get("name") != name
            or metadata.get("namespace") != APP_NAMESPACE
            or metadata.get("deletionTimestamp") is not None
        ):
            raise RuntimeErrorEB(
                f"live application Service identity drifted: {name}"
            )
        observed = _service_spec_projection(
            service, f"live application Service {name}"
        )
        if observed != expected_value["spec"]:
            raise RuntimeErrorEB(
                f"live application Service spec drifted: {name}"
            )
        result[name] = {
            "spec": observed,
            "spec_sha256": expected_value["spec_sha256"],
            "canonical": True,
        }
    return result


def _probe_runtime_contract(value: Any, context: str) -> Any:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise RuntimeErrorEB(f"{context} probe contract is invalid")
    normalized = json.loads(json.dumps(value))
    defaults = {
        "initialDelaySeconds": 0,
        "timeoutSeconds": 1,
        "periodSeconds": 10,
        "successThreshold": 1,
        "failureThreshold": 3,
    }
    for key, default in defaults.items():
        if normalized.get(key) == default:
            normalized.pop(key, None)
    http_get = normalized.get("httpGet")
    if isinstance(http_get, dict) and http_get.get("scheme") == "HTTP":
        http_get.pop("scheme", None)
    return normalized


def _container_runtime_contract(
    container: Any,
    context: str,
) -> dict[str, Any]:
    if not isinstance(container, dict):
        raise RuntimeErrorEB(f"{context} container contract is invalid")
    name = container.get("name")
    image = container.get("image")
    if (
        not isinstance(name, str)
        or not name
        or not isinstance(image, str)
        or not image
    ):
        raise RuntimeErrorEB(f"{context} container identity is invalid")
    fields = (
        "image",
        "imagePullPolicy",
        "command",
        "args",
        "env",
        "envFrom",
        "lifecycle",
        "resources",
        "securityContext",
        "volumeMounts",
        "workingDir",
    )
    ports = container.get("ports", [])
    if ports is None:
        ports = []
    if not isinstance(ports, list) or any(
        not isinstance(port, dict) for port in ports
    ):
        raise RuntimeErrorEB(f"{context} container ports contract is invalid")
    normalized_ports = json.loads(json.dumps(ports))
    for port in normalized_ports:
        port.setdefault("protocol", "TCP")
        if port.get("hostPort") == 0:
            port.pop("hostPort", None)
    result = {
        "name": name,
        **{field: container.get(field) for field in fields},
        "ports": normalized_ports,
    }
    for probe_field in (
        "startupProbe",
        "readinessProbe",
        "livenessProbe",
    ):
        result[probe_field] = _probe_runtime_contract(
            container.get(probe_field),
            f"{context} {name} {probe_field}",
        )
    return result


def _application_pod_spec_projection(
    pod_spec: Any,
    context: str,
) -> dict[str, Any]:
    if not isinstance(pod_spec, dict):
        raise RuntimeErrorEB(f"{context} Pod spec is invalid")

    tolerations = pod_spec.get("tolerations") or []
    if not isinstance(tolerations, list) or any(
        not isinstance(item, dict) for item in tolerations
    ):
        raise RuntimeErrorEB(f"{context} toleration contract is invalid")
    normalized_tolerations = json.loads(json.dumps(tolerations))
    for default_toleration in (
        {
            "effect": "NoExecute",
            "key": "node.kubernetes.io/not-ready",
            "operator": "Exists",
            "tolerationSeconds": 300,
        },
        {
            "effect": "NoExecute",
            "key": "node.kubernetes.io/unreachable",
            "operator": "Exists",
            "tolerationSeconds": 300,
        },
    ):
        if default_toleration in normalized_tolerations:
            normalized_tolerations.remove(default_toleration)

    host_aliases = pod_spec.get("hostAliases") or []
    readiness_gates = pod_spec.get("readinessGates") or []
    ephemeral_containers = pod_spec.get("ephemeralContainers") or []
    if (
        not isinstance(host_aliases, list)
        or any(not isinstance(item, dict) for item in host_aliases)
        or not isinstance(readiness_gates, list)
        or any(not isinstance(item, dict) for item in readiness_gates)
        or not isinstance(ephemeral_containers, list)
        or any(not isinstance(item, dict) for item in ephemeral_containers)
    ):
        raise RuntimeErrorEB(
            f"{context} Pod hostAliases/readinessGates/ephemeralContainers "
            "contract is invalid"
        )
    if ephemeral_containers:
        raise RuntimeErrorEB(
            f"{context} Pod ephemeral containers are forbidden"
        )

    result: dict[str, Any] = {
        "serviceAccountName": pod_spec.get(
            "serviceAccountName", "default"
        ),
        "automountServiceAccountToken": pod_spec.get(
            "automountServiceAccountToken"
        ),
        "terminationGracePeriodSeconds": pod_spec.get(
            "terminationGracePeriodSeconds", 30
        ),
        "securityContext": pod_spec.get("securityContext"),
        "imagePullSecrets": pod_spec.get("imagePullSecrets") or [],
        "volumes": pod_spec.get("volumes") or [],
        "topologySpreadConstraints": pod_spec.get(
            "topologySpreadConstraints"
        ),
        "affinity": pod_spec.get("affinity"),
        "nodeSelector": pod_spec.get("nodeSelector"),
        "hostNetwork": pod_spec.get("hostNetwork", False),
        "hostPID": pod_spec.get("hostPID", False),
        "hostIPC": pod_spec.get("hostIPC", False),
        "dnsPolicy": pod_spec.get("dnsPolicy", "ClusterFirst"),
        "dnsConfig": pod_spec.get("dnsConfig"),
        "priorityClassName": pod_spec.get("priorityClassName", ""),
        "tolerations": normalized_tolerations,
        "restartPolicy": pod_spec.get("restartPolicy", "Always"),
        "schedulerName": pod_spec.get(
            "schedulerName", "default-scheduler"
        ),
        "enableServiceLinks": pod_spec.get("enableServiceLinks", True),
        "shareProcessNamespace": pod_spec.get(
            "shareProcessNamespace", False
        ),
        "runtimeClassName": pod_spec.get("runtimeClassName"),
        "hostname": pod_spec.get("hostname"),
        "subdomain": pod_spec.get("subdomain"),
        "setHostnameAsFQDN": pod_spec.get("setHostnameAsFQDN", False),
        "hostAliases": json.loads(json.dumps(host_aliases)),
        "readinessGates": json.loads(json.dumps(readiness_gates)),
        "preemptionPolicy": pod_spec.get(
            "preemptionPolicy", "PreemptLowerPriority"
        ),
        "priority": pod_spec.get("priority", 0),
    }
    for field, output_key in (
        ("containers", "containers"),
        ("initContainers", "init_containers"),
    ):
        items = pod_spec.get(field, [])
        if not isinstance(items, list):
            raise RuntimeErrorEB(f"{context} {field} inventory is invalid")
        projected: dict[str, Any] = {}
        for item in items:
            value = _container_runtime_contract(item, context)
            name = value["name"]
            if name in projected:
                raise RuntimeErrorEB(
                    f"{context} contains duplicate container identity: {name}"
                )
            projected[name] = value
        if output_key == "containers" and not projected:
            raise RuntimeErrorEB(f"{context} contains no containers")
        result[output_key] = projected
    return result


def _rendered_application_workload_contract(
    root: Path,
    release: dict[str, Any],
) -> dict[str, Any]:
    api_digest = release.get("api_digest") if isinstance(release, dict) else None
    web_digest = release.get("web_digest") if isinstance(release, dict) else None
    if (
        not isinstance(api_digest, str)
        or not DIGEST_RE.fullmatch(api_digest)
        or not isinstance(web_digest, str)
        or not DIGEST_RE.fullmatch(web_digest)
    ):
        raise RuntimeErrorEB(
            "application workload contract requires exact release digests"
        )
    kustomize = toolchain(root)["tools"].get("kustomize")
    if not isinstance(kustomize, str) or not kustomize:
        raise RuntimeErrorEB(
            "application workload contract requires pinned kustomize"
        )
    rendered = run([kustomize, "build", str(APP_OVERLAY)]).stdout
    rendered = rendered.replace("${API_DIGEST}", api_digest).replace(
        "${WEB_DIGEST}", web_digest
    )
    try:
        documents = [
            document
            for document in yaml.safe_load_all(rendered)
            if isinstance(document, dict)
        ]
    except yaml.YAMLError as exc:
        raise RuntimeErrorEB(
            "rendered Experiment-B application contract is invalid"
        ) from exc

    result: dict[str, Any] = {}
    for name in ("weltgewebe-api", "weltgewebe-web"):
        matches = [
            document
            for document in documents
            if document.get("kind") == "Deployment"
            and document.get("metadata", {}).get("name") == name
            and document.get("metadata", {}).get("namespace") == APP_NAMESPACE
        ]
        if len(matches) != 1:
            raise RuntimeErrorEB(
                f"rendered application Deployment is ambiguous: {name}"
            )
        deployment = matches[0]
        spec = deployment.get("spec", {})
        template = spec.get("template", {}) if isinstance(spec, dict) else {}
        template_metadata = (
            template.get("metadata", {}) if isinstance(template, dict) else {}
        )
        pod_spec = template.get("spec", {}) if isinstance(template, dict) else {}
        replicas = spec.get("replicas") if isinstance(spec, dict) else None
        if (
            isinstance(replicas, bool)
            or not isinstance(replicas, int)
            or replicas < 1
            or not isinstance(template_metadata, dict)
        ):
            raise RuntimeErrorEB(
                f"rendered application Deployment contract is invalid: {name}"
            )
        pod_contract = _application_pod_spec_projection(
            pod_spec, f"rendered application Deployment {name}"
        )
        deployment_contract = {
            "replicas": replicas,
            "revisionHistoryLimit": spec.get("revisionHistoryLimit", 10),
            "strategy": spec.get("strategy"),
            "selector_labels": _pod_selector_match_labels(
                deployment, f"rendered application Deployment {name}"
            ),
            "template_labels": template_metadata.get("labels", {}),
            "template_annotations": template_metadata.get("annotations", {}),
            "pod_spec": pod_contract,
        }
        if (
            not isinstance(deployment_contract["template_labels"], dict)
            or not isinstance(
                deployment_contract["template_annotations"], dict
            )
        ):
            raise RuntimeErrorEB(
                f"rendered application template metadata is invalid: {name}"
            )
        result[name] = {
            "contract": deployment_contract,
            "contract_sha256": _stable_json_sha256(deployment_contract),
            "pod_contract_sha256": _stable_json_sha256(pod_contract),
        }
    return result


def _service_account_contract_projection(
    service_account: Any,
    context: str,
) -> dict[str, Any]:
    if not isinstance(service_account, dict):
        raise RuntimeErrorEB(f"{context} ServiceAccount payload is invalid")
    metadata = service_account.get("metadata", {})
    if not isinstance(metadata, dict):
        raise RuntimeErrorEB(f"{context} ServiceAccount metadata is invalid")
    labels = metadata.get("labels", {})
    annotations = metadata.get("annotations", {})
    if not isinstance(labels, dict) or not isinstance(annotations, dict):
        raise RuntimeErrorEB(
            f"{context} ServiceAccount metadata contract is invalid"
        )

    def ref_names(field: str) -> list[str]:
        values = service_account.get(field, [])
        if values is None:
            values = []
        if not isinstance(values, list):
            raise RuntimeErrorEB(
                f"{context} ServiceAccount {field} contract is invalid"
            )
        names: list[str] = []
        for value in values:
            name = value.get("name") if isinstance(value, dict) else None
            if not isinstance(name, str) or not name:
                raise RuntimeErrorEB(
                    f"{context} ServiceAccount {field} entry is invalid"
                )
            names.append(name)
        if len(names) != len(set(names)):
            raise RuntimeErrorEB(
                f"{context} ServiceAccount {field} contains duplicates"
            )
        return sorted(names)

    automount = service_account.get("automountServiceAccountToken")
    if automount is not None and not isinstance(automount, bool):
        raise RuntimeErrorEB(
            f"{context} ServiceAccount automount contract is invalid"
        )
    return {
        "labels": {
            str(key): str(value)
            for key, value in sorted(labels.items())
        },
        "annotations": {
            str(key): str(value)
            for key, value in sorted(annotations.items())
        },
        "automountServiceAccountToken": automount,
        "imagePullSecrets": ref_names("imagePullSecrets"),
        "secrets": ref_names("secrets"),
    }


def _rendered_application_service_account_contract(
    root: Path,
    release: dict[str, Any],
) -> dict[str, Any]:
    api_digest = release.get("api_digest") if isinstance(release, dict) else None
    web_digest = release.get("web_digest") if isinstance(release, dict) else None
    if (
        not isinstance(api_digest, str)
        or not DIGEST_RE.fullmatch(api_digest)
        or not isinstance(web_digest, str)
        or not DIGEST_RE.fullmatch(web_digest)
    ):
        raise RuntimeErrorEB(
            "application ServiceAccount contract requires exact release digests"
        )
    kustomize = toolchain(root)["tools"].get("kustomize")
    if not isinstance(kustomize, str) or not kustomize:
        raise RuntimeErrorEB(
            "application ServiceAccount contract requires pinned kustomize"
        )
    rendered = run([kustomize, "build", str(APP_OVERLAY)]).stdout
    rendered = rendered.replace("$" + "{API_DIGEST}", api_digest).replace(
        "$" + "{WEB_DIGEST}", web_digest
    )
    try:
        documents = [
            document
            for document in yaml.safe_load_all(rendered)
            if isinstance(document, dict)
        ]
    except yaml.YAMLError as exc:
        raise RuntimeErrorEB(
            "rendered Experiment-B ServiceAccount contract is invalid"
        ) from exc

    referenced: set[str] = set()
    for workload in ("weltgewebe-api", "weltgewebe-web"):
        deployments = [
            document
            for document in documents
            if document.get("kind") == "Deployment"
            and document.get("metadata", {}).get("name") == workload
            and document.get("metadata", {}).get("namespace") == APP_NAMESPACE
        ]
        if len(deployments) != 1:
            raise RuntimeErrorEB(
                f"rendered application Deployment is ambiguous: {workload}"
            )
        service_account_name = (
            deployments[0]
            .get("spec", {})
            .get("template", {})
            .get("spec", {})
            .get("serviceAccountName")
        )
        if not isinstance(service_account_name, str) or not service_account_name:
            raise RuntimeErrorEB(
                f"rendered application Deployment has no ServiceAccount: {workload}"
            )
        referenced.add(service_account_name)

    result: dict[str, Any] = {}
    for name in sorted(referenced):
        matches = [
            document
            for document in documents
            if document.get("kind") == "ServiceAccount"
            and document.get("metadata", {}).get("name") == name
            and document.get("metadata", {}).get("namespace") == APP_NAMESPACE
        ]
        if len(matches) != 1:
            raise RuntimeErrorEB(
                f"rendered application ServiceAccount is ambiguous: {name}"
            )
        contract = _service_account_contract_projection(
            matches[0], f"rendered application ServiceAccount {name}"
        )
        result[name] = {
            "contract": contract,
            "contract_sha256": _stable_json_sha256(contract),
        }
    if not result:
        raise RuntimeErrorEB(
            "rendered application ServiceAccount contract is empty"
        )
    return result


def _require_live_application_service_accounts(
    root: Path,
    release: dict[str, Any],
) -> dict[str, Any]:
    expected = _rendered_application_service_account_contract(root, release)
    result: dict[str, Any] = {}
    for name, expected_value in expected.items():
        service_account = _kubectl_json(
            root,
            ["-n", APP_NAMESPACE, "get", "serviceaccount", name],
        )
        metadata = (
            service_account.get("metadata", {})
            if isinstance(service_account, dict)
            else {}
        )
        if (
            not isinstance(metadata, dict)
            or metadata.get("name") != name
            or metadata.get("namespace") != APP_NAMESPACE
            or metadata.get("deletionTimestamp") is not None
        ):
            raise RuntimeErrorEB(
                f"live application ServiceAccount identity drifted: {name}"
            )
        observed = _service_account_contract_projection(
            service_account, f"live application ServiceAccount {name}"
        )
        if observed != expected_value["contract"]:
            raise RuntimeErrorEB(
                f"live application ServiceAccount contract drifted: {name}"
            )
        result[name] = {
            "contract_sha256": expected_value["contract_sha256"],
            "canonical": True,
        }
    return result


def _require_live_application_workloads(
    root: Path,
    release: dict[str, Any],
    deployments: dict[str, Any],
    pods_by_workload: dict[str, Any],
    names: tuple[str, ...] = ("weltgewebe-api", "weltgewebe-web"),
) -> dict[str, Any]:
    rendered = _rendered_application_workload_contract(root, release)
    if (
        not names
        or len(set(names)) != len(names)
        or any(name not in rendered for name in names)
    ):
        raise RuntimeErrorEB(
            "live application workload selection is invalid"
        )
    expected = {name: rendered[name] for name in names}
    if (
        set(deployments) != set(expected)
        or set(pods_by_workload) != set(expected)
    ):
        raise RuntimeErrorEB(
            "live application workload inventory is incomplete"
        )
    result: dict[str, Any] = {}
    for name, expected_value in expected.items():
        deployment = deployments[name]
        contract = expected_value["contract"]
        metadata = (
            deployment.get("metadata", {})
            if isinstance(deployment, dict)
            else {}
        )
        spec = (
            deployment.get("spec", {})
            if isinstance(deployment, dict)
            else {}
        )
        template = spec.get("template", {}) if isinstance(spec, dict) else {}
        template_metadata = (
            template.get("metadata", {}) if isinstance(template, dict) else {}
        )
        if (
            not isinstance(metadata, dict)
            or metadata.get("name") != name
            or metadata.get("namespace") != APP_NAMESPACE
            or metadata.get("deletionTimestamp") is not None
            or not isinstance(spec, dict)
            or not isinstance(template_metadata, dict)
        ):
            raise RuntimeErrorEB(
                f"live application Deployment identity drifted: {name}"
            )
        observed_contract = {
            "replicas": spec.get("replicas"),
            "revisionHistoryLimit": spec.get("revisionHistoryLimit"),
            "strategy": spec.get("strategy"),
            "selector_labels": _pod_selector_match_labels(
                deployment, f"live application Deployment {name}"
            ),
            "template_labels": template_metadata.get("labels", {}),
            "template_annotations": template_metadata.get(
                "annotations", {}
            ),
            "pod_spec": _application_pod_spec_projection(
                template.get("spec", {}),
                f"live application Deployment {name}",
            ),
        }
        if observed_contract != contract:
            raise RuntimeErrorEB(
                f"live application Deployment contract drifted: {name}"
            )

        pods = pods_by_workload[name]
        if not isinstance(pods, list) or len(pods) != contract["replicas"]:
            raise RuntimeErrorEB(
                f"live application Pod set drifted: {name}"
            )
        pod_names: list[str] = []
        for pod in pods:
            if not isinstance(pod, dict):
                raise RuntimeErrorEB(
                    f"live application Pod inventory is invalid: {name}"
                )
            pod_metadata = pod.get("metadata", {})
            pod_spec = pod.get("spec", {})
            labels = (
                pod_metadata.get("labels", {})
                if isinstance(pod_metadata, dict)
                else {}
            )
            annotations = (
                pod_metadata.get("annotations", {})
                if isinstance(pod_metadata, dict)
                else {}
            )
            pod_name = (
                pod_metadata.get("name")
                if isinstance(pod_metadata, dict)
                else None
            )
            if (
                not isinstance(pod_name, str)
                or not pod_name
                or pod_name in pod_names
                or pod_metadata.get("namespace") != APP_NAMESPACE
                or pod_metadata.get("deletionTimestamp") is not None
                or not isinstance(labels, dict)
                or not isinstance(annotations, dict)
                or any(
                    labels.get(key) != value
                    for key, value in contract["template_labels"].items()
                )
                or any(
                    annotations.get(key) != value
                    for key, value in contract[
                        "template_annotations"
                    ].items()
                )
                or _application_pod_spec_projection(
                    pod_spec, f"live application Pod {pod_name}"
                )
                != contract["pod_spec"]
            ):
                raise RuntimeErrorEB(
                    f"live application Pod contract drifted: {name}"
                )
            pod_names.append(pod_name)
        result[name] = {
            "contract_sha256": expected_value["contract_sha256"],
            "pod_contract_sha256": expected_value[
                "pod_contract_sha256"
            ],
            "pod_names": sorted(pod_names),
            "canonical": True,
        }
    return result


def _container_resources_contract(
    pod_spec: Any,
    container_name: str,
    context: str,
) -> dict[str, Any]:
    if not isinstance(pod_spec, dict):
        raise RuntimeErrorEB(f"{context} Pod spec is invalid")
    containers = pod_spec.get("containers", [])
    if not isinstance(containers, list):
        raise RuntimeErrorEB(f"{context} container inventory is invalid")
    matches = [
        item
        for item in containers
        if isinstance(item, dict) and item.get("name") == container_name
    ]
    if len(matches) != 1:
        raise RuntimeErrorEB(
            f"{context} does not contain exactly one container: {container_name}"
        )
    resources = matches[0].get("resources")
    if not isinstance(resources, dict):
        raise RuntimeErrorEB(
            f"{context} resources are missing: {container_name}"
        )
    requests = resources.get("requests")
    limits = resources.get("limits")
    if (
        not isinstance(requests, dict)
        or not requests
        or not isinstance(limits, dict)
        or not limits
        or any(
            not isinstance(key, str)
            or not key
            or not isinstance(value, str)
            or not value
            for mapping in (requests, limits)
            for key, value in mapping.items()
        )
    ):
        raise RuntimeErrorEB(
            f"{context} resource contract is invalid: {container_name}"
        )
    return json.loads(json.dumps(resources))


def _versioned_data_container_resources(
    path: Path,
    deployment_name: str,
    container_name: str,
) -> dict[str, Any]:
    try:
        documents = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeErrorEB(
            f"versioned data resource manifest is invalid: {deployment_name}"
        ) from exc
    matches = [
        document
        for document in documents
        if isinstance(document, dict)
        and document.get("kind") == "Deployment"
        and document.get("metadata", {}).get("name") == deployment_name
        and document.get("metadata", {}).get("namespace") == DATA_NAMESPACE
    ]
    if len(matches) != 1:
        raise RuntimeErrorEB(
            f"versioned data resource Deployment is ambiguous: {deployment_name}"
        )
    return _container_resources_contract(
        matches[0].get("spec", {}).get("template", {}).get("spec"),
        container_name,
        f"versioned data Deployment {deployment_name}",
    )


def _versioned_data_deployment_contract(path: Path, name: str) -> dict[str, Any]:
    try:
        documents = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeErrorEB(
            f"versioned data Deployment manifest is invalid: {name}"
        ) from exc
    matches = [
        document
        for document in documents
        if isinstance(document, dict)
        and document.get("kind") == "Deployment"
        and document.get("metadata", {}).get("name") == name
        and document.get("metadata", {}).get("namespace") == DATA_NAMESPACE
    ]
    if len(matches) != 1:
        raise RuntimeErrorEB(
            f"versioned data manifest does not contain exactly one Deployment: {name}"
        )
    spec = matches[0].get("spec", {})
    replicas = spec.get("replicas")
    if isinstance(replicas, bool) or not isinstance(replicas, int) or replicas != 1:
        raise RuntimeErrorEB(
            f"versioned data Deployment replica contract drifted: {name}"
        )
    deployment = matches[0]
    template = spec.get("template", {}) if isinstance(spec, dict) else {}
    template_metadata = (
        template.get("metadata", {}) if isinstance(template, dict) else {}
    )
    pod_spec = template.get("spec", {}) if isinstance(template, dict) else {}
    if not isinstance(template_metadata, dict):
        raise RuntimeErrorEB(
            f"versioned data Deployment template metadata is invalid: {name}"
        )
    images = _pod_spec_images(
        pod_spec,
        f"versioned data Deployment {name}",
    )
    selector = _pod_selector_match_labels(
        deployment, f"versioned data Deployment {name}"
    )
    pod_contract = _application_pod_spec_projection(
        pod_spec, f"versioned data Deployment {name}"
    )
    deployment_contract = {
        "replicas": replicas,
        "revisionHistoryLimit": spec.get("revisionHistoryLimit", 10),
        "strategy": spec.get("strategy"),
        "selector_labels": selector,
        "template_labels": template_metadata.get("labels", {}),
        "template_annotations": template_metadata.get("annotations", {}),
        "pod_spec": pod_contract,
    }
    if (
        not isinstance(deployment_contract["template_labels"], dict)
        or not isinstance(deployment_contract["template_annotations"], dict)
    ):
        raise RuntimeErrorEB(
            f"versioned data Deployment template metadata is invalid: {name}"
        )
    return {
        "replicas": replicas,
        "images": images,
        "selector_labels": selector,
        "contract": deployment_contract,
        "contract_sha256": _stable_json_sha256(deployment_contract),
        "pod_contract_sha256": _stable_json_sha256(pod_contract),
    }


def _require_live_data_deployments(
    root: Path,
    names: tuple[str, ...] = ("postgres", "nats"),
) -> dict[str, Any]:
    manifests = {
        "postgres": CLUSTER / "data/postgres.yaml",
        "nats": CLUSTER / "data/nats.yaml",
    }
    if (
        not names
        or len(set(names)) != len(names)
        or any(name not in manifests for name in names)
    ):
        raise RuntimeErrorEB("live data Deployment selection is invalid")
    pod_items = _kubectl_json(
        root, ["-n", DATA_NAMESPACE, "get", "pods"]
    ).get("items")
    if not isinstance(pod_items, list) or any(
        not isinstance(item, dict) for item in pod_items
    ):
        raise RuntimeErrorEB("live data Pod inventory is invalid")

    result: dict[str, Any] = {}
    for name in names:
        path = manifests[name]
        expected = _versioned_data_deployment_contract(path, name)
        deployment = _kubectl_json(
            root, ["-n", DATA_NAMESPACE, "get", "deployment", name]
        )
        metadata = (
            deployment.get("metadata", {}) if isinstance(deployment, dict) else {}
        )
        if (
            not isinstance(metadata, dict)
            or metadata.get("name") != name
            or metadata.get("namespace") != DATA_NAMESPACE
            or metadata.get("deletionTimestamp") is not None
        ):
            raise RuntimeErrorEB(f"live data Deployment identity drifted: {name}")
        live_selector = _pod_selector_match_labels(
            deployment, f"live data Deployment {name}"
        )
        if live_selector != expected["selector_labels"]:
            raise RuntimeErrorEB(
                f"live data Deployment selector drifted from versioned manifest: {name}"
            )
        availability = _deployment_availability_snapshot(
            deployment, name, expected["replicas"]
        )
        live_spec = deployment.get("spec", {})
        live_template = (
            live_spec.get("template", {})
            if isinstance(live_spec, dict)
            else {}
        )
        live_template_metadata = (
            live_template.get("metadata", {})
            if isinstance(live_template, dict)
            else {}
        )
        if not isinstance(live_template_metadata, dict):
            raise RuntimeErrorEB(
                f"live data Deployment template metadata is invalid: {name}"
            )
        live_pod_spec = (
            live_template.get("spec", {})
            if isinstance(live_template, dict)
            else {}
        )
        live_images = _pod_spec_images(
            live_pod_spec,
            f"live data Deployment {name}",
        )
        if live_images != expected["images"]:
            raise RuntimeErrorEB(
                f"live data Deployment images drifted from versioned manifest: {name}"
            )
        observed_contract = {
            "replicas": live_spec.get("replicas"),
            "revisionHistoryLimit": live_spec.get(
                "revisionHistoryLimit", 10
            ),
            "strategy": live_spec.get("strategy"),
            "selector_labels": live_selector,
            "template_labels": live_template_metadata.get("labels", {}),
            "template_annotations": live_template_metadata.get(
                "annotations", {}
            ),
            "pod_spec": _application_pod_spec_projection(
                live_pod_spec, f"live data Deployment {name}"
            ),
        }
        if observed_contract != expected["contract"]:
            raise RuntimeErrorEB(
                f"live data Deployment contract drifted from versioned manifest: {name}"
            )

        matching_pods = _pods_matching_labels(
            pod_items, expected["selector_labels"]
        )
        pod_readback = _require_running_pod_image_contract(
            matching_pods,
            namespace=DATA_NAMESPACE,
            workload=name,
            expected_replicas=expected["replicas"],
            expected_images=expected["images"],
            required_labels=expected["selector_labels"],
            context="data Pod",
        )
        if len(matching_pods) != expected["replicas"]:
            raise RuntimeErrorEB(
                f"live data Pod set drifted: {name}"
            )
        pod_names: list[str] = []
        for pod in matching_pods:
            metadata = pod.get("metadata", {}) if isinstance(pod, dict) else {}
            pod_spec = pod.get("spec", {}) if isinstance(pod, dict) else {}
            labels = metadata.get("labels", {}) if isinstance(metadata, dict) else {}
            annotations = (
                metadata.get("annotations", {})
                if isinstance(metadata, dict)
                else {}
            )
            pod_name = metadata.get("name") if isinstance(metadata, dict) else None
            if (
                not isinstance(pod_name, str)
                or not pod_name
                or pod_name in pod_names
                or metadata.get("namespace") != DATA_NAMESPACE
                or metadata.get("deletionTimestamp") is not None
                or not isinstance(labels, dict)
                or not isinstance(annotations, dict)
                or any(
                    labels.get(key) != value
                    for key, value in expected["contract"][
                        "template_labels"
                    ].items()
                )
                or any(
                    annotations.get(key) != value
                    for key, value in expected["contract"][
                        "template_annotations"
                    ].items()
                )
                or _application_pod_spec_projection(
                    pod_spec, f"live data Pod {pod_name}"
                )
                != expected["contract"]["pod_spec"]
            ):
                raise RuntimeErrorEB(
                    f"live data Pod contract drifted: {name}"
                )
            pod_names.append(pod_name)

        result[name] = {
            **availability,
            "images_sha256": _stable_json_sha256(live_images),
            "images_canonical": True,
            "pods": pod_readback,
            "contract_sha256": expected["contract_sha256"],
            "pod_contract_sha256": expected[
                "pod_contract_sha256"
            ],
            "pod_names": sorted(pod_names),
            "canonical": True,
        }
    return result


def _cilium_pod_spec_projection(
    pod_spec: Any,
    context: str,
) -> dict[str, Any]:
    projection = _application_pod_spec_projection(pod_spec, context)
    assert isinstance(pod_spec, dict)
    projection.update(
        {
            "hostNetwork": pod_spec.get("hostNetwork", False),
            "hostPID": pod_spec.get("hostPID", False),
            "hostIPC": pod_spec.get("hostIPC", False),
            "dnsPolicy": pod_spec.get("dnsPolicy", "ClusterFirst"),
            "dnsConfig": pod_spec.get("dnsConfig"),
            "priorityClassName": pod_spec.get("priorityClassName", ""),
            "tolerations": pod_spec.get("tolerations") or [],
            "restartPolicy": pod_spec.get("restartPolicy", "Always"),
            "schedulerName": pod_spec.get(
                "schedulerName", "default-scheduler"
            ),
            "enableServiceLinks": pod_spec.get("enableServiceLinks", True),
            "shareProcessNamespace": pod_spec.get(
                "shareProcessNamespace", False
            ),
        }
    )
    return projection


def _expected_cilium_runtime_contract(
    root: Path,
    config: dict[str, Any],
    toolchain_receipt: dict[str, Any],
) -> dict[str, Any]:
    tools = toolchain_receipt.get("tools", {})
    artifacts = toolchain_receipt.get("artifacts", {})
    helm = tools.get("helm") if isinstance(tools, dict) else None
    chart = artifacts.get("cilium_chart") if isinstance(artifacts, dict) else None
    if not isinstance(helm, str) or not isinstance(chart, str):
        raise RuntimeErrorEB(
            "pinned Cilium chart/toolchain binding is unavailable"
        )

    rendered = run(
        [
            helm,
            "template",
            "cilium",
            chart,
            "--namespace",
            "kube-system",
            "--kube-version",
            str(config["kubernetes"]["kubernetes_version"]),
            *_cilium_helm_value_args(vm_ip()),
        ],
        env=kube_env(root),
    ).stdout
    try:
        documents = [
            document
            for document in yaml.safe_load_all(rendered)
            if isinstance(document, dict)
        ]
    except yaml.YAMLError as exc:
        raise RuntimeErrorEB("pinned Cilium chart render is invalid") from exc

    config_maps = [
        document
        for document in documents
        if document.get("kind") == "ConfigMap"
        and document.get("metadata", {}).get("name") == "cilium-config"
        and document.get("metadata", {}).get("namespace") == "kube-system"
    ]
    if len(config_maps) != 1:
        raise RuntimeErrorEB(
            "pinned Cilium chart does not render exactly one "
            "kube-system ConfigMap cilium-config"
        )
    config_map = config_maps[0]
    config_data = config_map.get("data", {})
    config_binary_data = config_map.get("binaryData", {})
    config_immutable = config_map.get("immutable", False)
    if (
        not isinstance(config_data, dict)
        or not isinstance(config_binary_data, dict)
        or not isinstance(config_immutable, bool)
    ):
        raise RuntimeErrorEB("pinned Cilium ConfigMap contract is invalid")
    config_map_contract = {
        "data": json.loads(json.dumps(config_data)),
        "binaryData": json.loads(json.dumps(config_binary_data)),
        "immutable": config_immutable,
    }

    def workload_contract(
        kind: str,
        name: str,
        context: str,
    ) -> dict[str, Any]:
        workloads = [
            document
            for document in documents
            if document.get("kind") == kind
            and document.get("metadata", {}).get("name") == name
            and document.get("metadata", {}).get("namespace") == "kube-system"
        ]
        if len(workloads) != 1:
            raise RuntimeErrorEB(
                f"pinned Cilium chart does not render exactly one "
                f"kube-system {kind} {name}"
            )
        workload = workloads[0]
        pod_spec = (
            workload.get("spec", {}).get("template", {}).get("spec")
        )
        return {
            "images": _pod_spec_images(pod_spec, context),
            "selector_labels": _pod_selector_match_labels(
                workload, context
            ),
            "pod_spec": _cilium_pod_spec_projection(
                pod_spec, context
            ),
        }

    return {
        "config_map": config_map_contract,
        "daemonset": workload_contract(
            "DaemonSet", "cilium", "pinned Cilium DaemonSet"
        ),
        "operator": workload_contract(
            "Deployment",
            "cilium-operator",
            "pinned Cilium operator Deployment",
        ),
    }


def _require_live_cilium_contract(
    root: Path, config: dict[str, Any]
) -> dict[str, Any]:
    toolchain_receipt = toolchain(root)
    tools = toolchain_receipt["tools"]
    helm = tools["helm"]
    env = kube_env(root)
    try:
        releases = json.loads(
            run(
                [
                    helm,
                    "list",
                    "--namespace",
                    "kube-system",
                    "--filter",
                    "^cilium$",
                    "--output",
                    "json",
                ],
                env=env,
            ).stdout
        )
        values = json.loads(
            run(
                [
                    helm,
                    "get",
                    "values",
                    "cilium",
                    "--namespace",
                    "kube-system",
                    "--all",
                    "--output",
                    "json",
                ],
                env=env,
            ).stdout
        )
    except json.JSONDecodeError as exc:
        raise RuntimeErrorEB(
            "live Cilium Helm state is not valid JSON"
        ) from exc
    expected_chart = f"cilium-{config['cilium']['chart_version']}"
    if (
        not isinstance(releases, list)
        or len(releases) != 1
        or not isinstance(releases[0], dict)
        or releases[0].get("name") != "cilium"
        or releases[0].get("namespace") != "kube-system"
        or releases[0].get("status") != "deployed"
        or releases[0].get("chart") != expected_chart
    ):
        raise RuntimeErrorEB(
            "live Cilium Helm release differs from the pinned contract"
        )
    if (
        not isinstance(values, dict)
        or values.get("kubeProxyReplacement") is not True
        or not isinstance(values.get("gatewayAPI"), dict)
        or values["gatewayAPI"].get("enabled") is not True
    ):
        raise RuntimeErrorEB(
            "live Cilium Gateway API/kube-proxy replacement "
            "configuration drifted"
        )

    expected_runtime = _expected_cilium_runtime_contract(
        root, config, toolchain_receipt
    )
    expected_config_map = expected_runtime["config_map"]
    live_config_map = _kubectl_json(
        root,
        ["-n", "kube-system", "get", "configmap", "cilium-config"],
    )
    config_metadata = (
        live_config_map.get("metadata", {})
        if isinstance(live_config_map, dict)
        else {}
    )
    live_config_data = (
        live_config_map.get("data", {})
        if isinstance(live_config_map, dict)
        else None
    )
    live_config_binary_data = (
        live_config_map.get("binaryData", {})
        if isinstance(live_config_map, dict)
        else None
    )
    live_config_immutable = (
        live_config_map.get("immutable", False)
        if isinstance(live_config_map, dict)
        else None
    )
    if (
        not isinstance(config_metadata, dict)
        or config_metadata.get("name") != "cilium-config"
        or config_metadata.get("namespace") != "kube-system"
        or config_metadata.get("deletionTimestamp") is not None
        or not isinstance(live_config_data, dict)
        or not isinstance(live_config_binary_data, dict)
        or not isinstance(live_config_immutable, bool)
    ):
        raise RuntimeErrorEB(
            "live Cilium ConfigMap identity/shape drifted"
        )
    live_config_contract = {
        "data": live_config_data,
        "binaryData": live_config_binary_data,
        "immutable": live_config_immutable,
    }
    if live_config_contract != expected_config_map:
        raise RuntimeErrorEB(
            "live Cilium ConfigMap drifted from the pinned chart render"
        )

    daemonset = _kubectl_json(
        root, ["-n", "kube-system", "get", "daemonset", "cilium"]
    )
    metadata = daemonset.get("metadata", {})
    status_obj = daemonset.get("status", {})
    if (
        not isinstance(metadata, dict)
        or metadata.get("name") != "cilium"
        or metadata.get("namespace") != "kube-system"
        or metadata.get("deletionTimestamp") is not None
    ):
        raise RuntimeErrorEB("live Cilium DaemonSet identity drifted")
    generation = int(metadata.get("generation") or 0)
    observed_generation = int(status_obj.get("observedGeneration") or 0)
    desired = int(status_obj.get("desiredNumberScheduled") or 0)
    updated = int(status_obj.get("updatedNumberScheduled") or 0)
    ready = int(status_obj.get("numberReady") or 0)
    available = int(status_obj.get("numberAvailable") or 0)
    unavailable = int(status_obj.get("numberUnavailable") or 0)
    if (
        generation < 1
        or observed_generation != generation
        or desired < 1
        or updated != desired
        or ready != desired
        or available != desired
        or unavailable != 0
    ):
        raise RuntimeErrorEB(
            "live Cilium DaemonSet is not fully converged"
        )

    expected_daemonset = expected_runtime["daemonset"]
    daemonset_pod_spec = (
        daemonset.get("spec", {}).get("template", {}).get("spec")
    )
    live_images = _pod_spec_images(
        daemonset_pod_spec,
        "live Cilium DaemonSet",
    )
    if live_images != expected_daemonset["images"]:
        raise RuntimeErrorEB(
            "live Cilium DaemonSet images drifted from the "
            "pinned chart render"
        )
    daemonset_selector = _pod_selector_match_labels(
        daemonset, "live Cilium DaemonSet"
    )
    if (
        daemonset_selector != expected_daemonset["selector_labels"]
        or _cilium_pod_spec_projection(
            daemonset_pod_spec, "live Cilium DaemonSet"
        )
        != expected_daemonset["pod_spec"]
    ):
        raise RuntimeErrorEB(
            "live Cilium DaemonSet pod contract drifted from the "
            "pinned chart render"
        )

    operator = _kubectl_json(
        root,
        ["-n", "kube-system", "get", "deployment", "cilium-operator"],
    )
    operator_metadata = operator.get("metadata", {})
    if (
        not isinstance(operator_metadata, dict)
        or operator_metadata.get("name") != "cilium-operator"
        or operator_metadata.get("namespace") != "kube-system"
        or operator_metadata.get("deletionTimestamp") is not None
    ):
        raise RuntimeErrorEB(
            "live Cilium operator Deployment identity drifted"
        )
    operator_availability = _deployment_availability_snapshot(
        operator, "cilium-operator", 1
    )
    expected_operator = expected_runtime["operator"]
    operator_pod_spec = (
        operator.get("spec", {}).get("template", {}).get("spec")
    )
    operator_images = _pod_spec_images(
        operator_pod_spec,
        "live Cilium operator Deployment",
    )
    if operator_images != expected_operator["images"]:
        raise RuntimeErrorEB(
            "live Cilium operator images drifted from the "
            "pinned chart render"
        )
    operator_selector = _pod_selector_match_labels(
        operator, "live Cilium operator Deployment"
    )
    if (
        operator_selector != expected_operator["selector_labels"]
        or _cilium_pod_spec_projection(
            operator_pod_spec, "live Cilium operator Deployment"
        )
        != expected_operator["pod_spec"]
    ):
        raise RuntimeErrorEB(
            "live Cilium operator pod contract drifted from the "
            "pinned chart render"
        )

    proxy_daemonsets = _kubectl_json(
        root, ["-n", "kube-system", "get", "daemonsets"]
    ).get("items")
    proxy_pods = _kubectl_json(
        root, ["-n", "kube-system", "get", "pods"]
    ).get("items")
    if (
        not isinstance(proxy_daemonsets, list)
        or not isinstance(proxy_pods, list)
        or not all(
            isinstance(item, dict)
            for item in (*proxy_daemonsets, *proxy_pods)
        )
    ):
        raise RuntimeErrorEB("kube-system workload inventory is invalid")

    daemonset_pods = _require_running_pod_image_contract(
        _pods_matching_labels(proxy_pods, daemonset_selector),
        namespace="kube-system",
        workload="cilium",
        expected_replicas=desired,
        expected_images=expected_daemonset["images"],
        required_labels=daemonset_selector,
        context="Cilium DaemonSet Pod",
    )
    operator_pods = _require_running_pod_image_contract(
        _pods_matching_labels(proxy_pods, operator_selector),
        namespace="kube-system",
        workload="cilium-operator",
        expected_replicas=1,
        expected_images=expected_operator["images"],
        required_labels=operator_selector,
        context="Cilium operator Pod",
    )

    def is_kube_proxy(item: dict[str, Any]) -> bool:
        metadata = item.get("metadata", {})
        if not isinstance(metadata, dict):
            return False
        name = str(metadata.get("name") or "")
        labels = metadata.get("labels", {})
        if not isinstance(labels, dict):
            labels = {}
        return (
            name == "kube-proxy"
            or name.startswith("kube-proxy-")
            or labels.get("k8s-app") == "kube-proxy"
            or labels.get("component") == "kube-proxy"
        )

    if any(
        is_kube_proxy(item)
        for item in (*proxy_daemonsets, *proxy_pods)
    ):
        raise RuntimeErrorEB("kube-proxy is present in Experiment B")

    return {
        "chart": expected_chart,
        "chart_version": config["cilium"]["chart_version"],
        "gateway_api": True,
        "kube_proxy_replacement": True,
        "config_map_sha256": _stable_json_sha256(
            live_config_contract
        ),
        "config_map_canonical": True,
        "daemonset_generation": generation,
        "daemonset_desired": desired,
        "daemonset_ready": ready,
        "daemonset_images": live_images,
        "daemonset_images_canonical": True,
        "daemonset_contract_sha256": _stable_json_sha256(
            expected_daemonset
        ),
        "daemonset_pods": daemonset_pods,
        "operator": operator_availability,
        "operator_images": operator_images,
        "operator_images_canonical": True,
        "operator_contract_sha256": _stable_json_sha256(
            expected_operator
        ),
        "operator_pods": operator_pods,
        "kube_proxy_present": False,
    }


def _require_cilium_runtime_baseline(
    root: Path,
    source_commit: str,
    cilium_readback: dict[str, Any],
) -> dict[str, str]:
    path = root / "receipts/platform.json"
    try:
        platform = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeErrorEB(
            "Experiment-B status requires valid platform runtime baseline"
        ) from exc
    baseline = platform.get("cilium_runtime_image_ids")
    if (
        not isinstance(platform, dict)
        or platform.get("schema_version") != 1
        or platform.get("status") != "ready"
        or platform.get("source_commit") != source_commit
        or not isinstance(baseline, dict)
        or set(baseline) != {"daemonset", "operator"}
        or any(
            not isinstance(value, str)
            or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in baseline.values()
        )
    ):
        raise RuntimeErrorEB("Experiment-B Cilium runtime baseline is invalid")
    current = {
        "daemonset": cilium_readback.get("daemonset_pods", {}).get(
            "runtime_image_ids_sha256"
        ),
        "operator": cilium_readback.get("operator_pods", {}).get(
            "runtime_image_ids_sha256"
        ),
    }
    if current != baseline:
        raise RuntimeErrorEB(
            "live Cilium Pod runtime image IDs drifted from platform installation"
        )
    return {str(key): str(value) for key, value in baseline.items()}


def _stable_json_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _versioned_network_policy_specs(path: Path, namespace: str) -> dict[str, Any]:
    try:
        documents = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeErrorEB(
            f"versioned NetworkPolicy contract is unreadable: {namespace}"
        ) from exc
    specs: dict[str, Any] = {}
    for document in documents:
        if not isinstance(document, dict) or document.get("kind") != "NetworkPolicy":
            continue
        metadata = document.get("metadata", {})
        spec = document.get("spec")
        if (
            not isinstance(metadata, dict)
            or metadata.get("namespace") != namespace
            or not isinstance(metadata.get("name"), str)
            or not isinstance(spec, dict)
        ):
            raise RuntimeErrorEB(
                f"versioned NetworkPolicy contract is invalid: {namespace}"
            )
        name = str(metadata["name"])
        if name in specs:
            raise RuntimeErrorEB(
                f"versioned NetworkPolicy contract contains duplicate: {namespace}/{name}"
            )
        specs[name] = spec
    if not specs:
        raise RuntimeErrorEB(
            f"versioned NetworkPolicy contract is empty: {namespace}"
        )
    return specs


def _require_live_runtime_contract(
    root: Path, config: dict[str, Any]
) -> dict[str, Any]:
    runtime_binding = config.get("runtime_binding", {})
    expected_config_data = runtime_binding.get("config_map_data")
    expected_network_specs = runtime_binding.get("network_policy_specs")
    expected_cilium_specs = runtime_binding.get("cilium_network_policy_specs")
    expected_data_network_specs = _versioned_network_policy_specs(
        CLUSTER / "data/network-policy.yaml", DATA_NAMESPACE
    )
    if (
        not isinstance(expected_config_data, dict)
        or not isinstance(expected_network_specs, dict)
        or not isinstance(expected_cilium_specs, dict)
    ):
        raise RuntimeErrorEB("Experiment-B runtime contract configuration is invalid")

    config_map = _kubectl_json(
        root,
        ["-n", APP_NAMESPACE, "get", "configmap", "weltgewebe-runtime"],
    )
    config_metadata = config_map.get("metadata", {})
    live_config_data = config_map.get("data")
    if (
        not isinstance(config_metadata, dict)
        or config_metadata.get("namespace") != APP_NAMESPACE
        or config_metadata.get("name") != "weltgewebe-runtime"
        or config_metadata.get("deletionTimestamp") is not None
        or config_map.get("immutable") not in (None, False)
        or config_map.get("binaryData") not in (None, {})
        or not isinstance(live_config_data, dict)
        or live_config_data != expected_config_data
    ):
        raise RuntimeErrorEB("live Experiment-B runtime ConfigMap drifted")

    policy_items = _kubectl_json(
        root, ["-n", APP_NAMESPACE, "get", "networkpolicies"]
    ).get("items")
    if not isinstance(policy_items, list):
        raise RuntimeErrorEB("live Experiment-B NetworkPolicy inventory is invalid")
    live_network_specs: dict[str, Any] = {}
    for item in policy_items:
        if not isinstance(item, dict):
            raise RuntimeErrorEB("live Experiment-B NetworkPolicy inventory is invalid")
        metadata = item.get("metadata", {})
        spec = item.get("spec")
        if (
            not isinstance(metadata, dict)
            or metadata.get("namespace") != APP_NAMESPACE
            or not isinstance(metadata.get("name"), str)
            or metadata.get("deletionTimestamp") is not None
            or not isinstance(spec, dict)
        ):
            raise RuntimeErrorEB("live Experiment-B NetworkPolicy inventory is invalid")
        name = str(metadata["name"])
        if name in live_network_specs:
            raise RuntimeErrorEB("live Experiment-B NetworkPolicy inventory is duplicated")
        live_network_specs[name] = spec
    if live_network_specs != expected_network_specs:
        raise RuntimeErrorEB("live Experiment-B NetworkPolicy contract drifted")

    data_policy_items = _kubectl_json(
        root, ["-n", DATA_NAMESPACE, "get", "networkpolicies"]
    ).get("items")
    if not isinstance(data_policy_items, list):
        raise RuntimeErrorEB(
            "live Experiment-B data NetworkPolicy inventory is invalid"
        )
    live_data_network_specs: dict[str, Any] = {}
    for item in data_policy_items:
        if not isinstance(item, dict):
            raise RuntimeErrorEB(
                "live Experiment-B data NetworkPolicy inventory is invalid"
            )
        metadata = item.get("metadata", {})
        spec = item.get("spec")
        if (
            not isinstance(metadata, dict)
            or metadata.get("namespace") != DATA_NAMESPACE
            or not isinstance(metadata.get("name"), str)
            or metadata.get("deletionTimestamp") is not None
            or not isinstance(spec, dict)
        ):
            raise RuntimeErrorEB(
                "live Experiment-B data NetworkPolicy inventory is invalid"
            )
        name = str(metadata["name"])
        if name in live_data_network_specs:
            raise RuntimeErrorEB(
                "live Experiment-B data NetworkPolicy inventory is duplicated"
            )
        live_data_network_specs[name] = spec
    if live_data_network_specs != expected_data_network_specs:
        raise RuntimeErrorEB(
            "live Experiment-B data NetworkPolicy contract drifted"
        )

    cilium_items = _kubectl_json(
        root, ["-n", APP_NAMESPACE, "get", "ciliumnetworkpolicies"]
    ).get("items")
    if not isinstance(cilium_items, list):
        raise RuntimeErrorEB("live Experiment-B CiliumNetworkPolicy inventory is invalid")
    live_cilium_specs: dict[str, Any] = {}
    for item in cilium_items:
        if not isinstance(item, dict):
            raise RuntimeErrorEB(
                "live Experiment-B CiliumNetworkPolicy inventory is invalid"
            )
        metadata = item.get("metadata", {})
        spec = item.get("spec")
        if (
            not isinstance(metadata, dict)
            or metadata.get("namespace") != APP_NAMESPACE
            or not isinstance(metadata.get("name"), str)
            or metadata.get("deletionTimestamp") is not None
            or not isinstance(spec, dict)
        ):
            raise RuntimeErrorEB(
                "live Experiment-B CiliumNetworkPolicy inventory is invalid"
            )
        name = str(metadata["name"])
        if name in live_cilium_specs:
            raise RuntimeErrorEB(
                "live Experiment-B CiliumNetworkPolicy inventory is duplicated"
            )
        live_cilium_specs[name] = spec
    if live_cilium_specs != expected_cilium_specs:
        raise RuntimeErrorEB(
            "live Experiment-B CiliumNetworkPolicy contract drifted"
        )

    return {
        "config_map_data_sha256": _stable_json_sha256(live_config_data),
        "network_policy_specs_sha256": _stable_json_sha256(live_network_specs),
        "network_policy_names": sorted(live_network_specs),
        "data_network_policy_specs_sha256": _stable_json_sha256(
            live_data_network_specs
        ),
        "data_network_policy_names": sorted(live_data_network_specs),
        "cilium_network_policy_specs_sha256": _stable_json_sha256(
            live_cilium_specs
        ),
        "cilium_network_policy_names": sorted(live_cilium_specs),
        "temporary_model_egress_absent": (
            "commonthing-experiment-b-model-bootstrap-egress"
            not in live_network_specs
        ),
        "policy_specs_canonical": True,
    }


def status(root: Path) -> dict[str, Any]:
    release_path = root / "receipts/release.json"
    if not release_path.is_file():
        raise RuntimeErrorEB("Experiment-B status requires release receipt")
    release = json.loads(release_path.read_text(encoding="utf-8"))
    source_commit = str(release.get("source_commit", ""))
    if not COMMIT_RE.fullmatch(source_commit):
        raise RuntimeErrorEB("Experiment-B status release binding is not exact")
    receipt_path, attempt_path, attempt_started_at_unix_ms = (
        _begin_live_check_attempt(root, "status", source_commit)
    )
    if _current_protected_main_commit() != source_commit:
        raise RuntimeErrorEB("Experiment-B status release is not current protected main")
    config = load_config()
    vm_create_path = root / "receipts/vm-create.json"
    try:
        vm_create = json.loads(vm_create_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeErrorEB("Experiment-B status requires valid vm-create.json") from exc
    _require_vm_create_receipt(vm_create, source_commit, config, root)
    vm_substrate = _live_vm_substrate(root, config)
    if vm_substrate != vm_create["substrate"]:
        raise RuntimeErrorEB("VM substrate drifted from creation receipt")
    k3s_runtime = _require_live_k3s_runtime(root, config, source_commit)
    toolchain_receipt = toolchain(root)
    tools = toolchain_receipt["tools"]
    env = kube_env(root)
    kubectl = tools["kubectl"]
    cilium_readback = _require_live_cilium_contract(root, config)
    cilium_readback["runtime_image_ids_baseline"] = (
        _require_cilium_runtime_baseline(root, source_commit, cilium_readback)
    )

    nodes = json.loads(run([kubectl, "get", "nodes", "-o", "json"], env=env).stdout)
    node_readback = _require_exact_k3s_node_inventory(
        nodes, str(config["kubernetes"]["version"])
    )
    kubelet = node_readback["kubelet_version"]
    os_image = node_readback["os_image"]

    flux_contract = _flux_bootstrap_contract(root, release)
    flux_controllers = _require_live_flux_controller_contract(
        root, toolchain_receipt
    )
    flux_runtime_baseline = _require_flux_runtime_baseline(
        root, source_commit, flux_controllers
    )
    source = _kubectl_json(
        root,
        ["-n", "flux-system", "get", "gitrepository", "commonthing-experiment-b"],
    )
    source_revision = _require_flux_source_revision(
        source,
        source_commit,
        flux_contract["source_spec"],
    )

    flux_items = _kubectl_json(
        root, ["-n", "flux-system", "get", "kustomizations"]
    ).get("items", [])
    flux_readback = _require_exact_flux_revision_ready(
        flux_items,
        source_commit,
        flux_contract["kustomization_specs"],
    )

    api = _kubectl_json(
        root, ["-n", APP_NAMESPACE, "get", "deployment", "weltgewebe-api"]
    )
    web = _kubectl_json(
        root, ["-n", APP_NAMESPACE, "get", "deployment", "weltgewebe-web"]
    )
    migration = _kubectl_json(
        root,
        [
            "-n", APP_NAMESPACE, "get", "job",
            MIGRATION_JOB_NAME,
        ],
    )
    migration_pods = _kubectl_json(
        root,
        [
            "-n", APP_NAMESPACE, "get", "pods",
            "-l", f"batch.kubernetes.io/job-name={MIGRATION_JOB_NAME}",
        ],
    ).get("items")
    release_artifacts = _require_requested_release_artifacts(
        root,
        api,
        web,
        migration,
        migration_pods,
        str(release.get("api_digest", "")),
        str(release.get("web_digest", "")),
        int(config["semantic_search"]["api_replicas"]),
        int(config["runtime_binding"]["web_replicas"]),
    )
    deployment_readback = release_artifacts["deployments"]
    namespace_security_readback = _require_live_namespace_security_contract(
        root
    )
    data_deployment_readback = _require_live_data_deployments(root)
    data_service_readback = _require_live_data_services(root)
    api_containers = _container_images(api, "Experiment-B API Deployment")
    semantic = config["semantic_search"]
    if api_containers.get("ollama") != semantic["ollama_image"]:
        raise RuntimeErrorEB("live Ollama image does not match semantic-search pin")

    expected_api_pod_images = {
        "api": str(release_artifacts["images"]["api"]),
        "search-worker": str(release_artifacts["images"]["search_worker"]),
        "ollama": str(semantic["ollama_image"]),
    }
    expected_web_pod_images = {
        "web": str(release_artifacts["images"]["web"]),
    }
    api_pods = _kubectl_json(
        root,
        [
            "-n",
            APP_NAMESPACE,
            "get",
            "pods",
            "-l",
            "app.kubernetes.io/name=weltgewebe-api",
        ],
    ).get("items")
    web_pods = _kubectl_json(
        root,
        [
            "-n",
            APP_NAMESPACE,
            "get",
            "pods",
            "-l",
            "app.kubernetes.io/name=weltgewebe-web",
        ],
    ).get("items")
    pod_readback = {
        "weltgewebe-api": _require_running_pod_images(
            api_pods,
            "weltgewebe-api",
            int(config["semantic_search"]["api_replicas"]),
            expected_api_pod_images,
        ),
        "weltgewebe-web": _require_running_pod_images(
            web_pods,
            "weltgewebe-web",
            int(config["runtime_binding"]["web_replicas"]),
            expected_web_pod_images,
        ),
    }
    application_workloads = _require_live_application_workloads(
        root,
        release,
        {
            "weltgewebe-api": api,
            "weltgewebe-web": web,
        },
        {
            "weltgewebe-api": api_pods,
            "weltgewebe-web": web_pods,
        },
    )
    application_services = _require_live_application_services(root, release)
    application_service_accounts = (
        _require_live_application_service_accounts(root, release)
    )
    semantic_provider = _semantic_provider_live_readback(root, source_commit)

    expected_secret_values = _expected_live_secret_values(root, source_commit)
    database_secret = _kubectl_json(
        root,
        [
            "-n", DATA_NAMESPACE, "get", "secret",
            "commonthing-experiment-b-database",
        ],
    )
    runtime_secret = _kubectl_json(
        root,
        ["-n", APP_NAMESPACE, "get", "secret", "weltgewebe-runtime"],
    )
    registry_secret = _kubectl_json(
        root,
        [
            "-n", APP_NAMESPACE, "get", "secret",
            "commonthing-experiment-b-registry",
        ],
    )
    _require_live_secret(
        database_secret,
        DATA_NAMESPACE,
        "commonthing-experiment-b-database",
        "Opaque",
        {"username", "database", "password"},
        expected_secret_values["database"],
    )
    _require_live_secret(
        runtime_secret,
        APP_NAMESPACE,
        "weltgewebe-runtime",
        "Opaque",
        {"database-url"},
        expected_secret_values["runtime"],
    )
    _require_live_secret(
        registry_secret,
        APP_NAMESPACE,
        "commonthing-experiment-b-registry",
        "kubernetes.io/dockerconfigjson",
        {".dockerconfigjson"},
        expected_secret_values["registry"],
    )
    secret_readback = {
        "database": _verified_secret_readback(
            DATA_NAMESPACE,
            "commonthing-experiment-b-database",
            "Opaque",
            {"username", "database", "password"},
        ),
        "runtime": _verified_secret_readback(
            APP_NAMESPACE,
            "weltgewebe-runtime",
            "Opaque",
            {"database-url"},
        ),
        "registry": _verified_secret_readback(
            APP_NAMESPACE,
            "commonthing-experiment-b-registry",
            "kubernetes.io/dockerconfigjson",
            {".dockerconfigjson"},
        ),
    }

    pvc_items = _kubectl_json(root, ["-A", "get", "pvc"]).get("items", [])
    pvc_readback = _require_exact_healthy_pvcs(root, pvc_items)

    gateway = _kubectl_json(
        root, ["-n", APP_NAMESPACE, "get", "gateway", "commonthing-experiment-b"]
    )
    gateway_readback = _require_gateway_ready(gateway)

    httproute = _kubectl_json(
        root, ["-n", APP_NAMESPACE, "get", "httproute", "commonthing-experiment-b"]
    )
    httproute_readback = _require_httproute_ready(httproute)
    gateway_data_plane = _gateway_data_plane_readback(root, source_commit)
    recovery_state = _final_recovery_state_readback(root, source_commit)
    runtime_contract_readback = _require_live_runtime_contract(root, config)

    result = {
        "schema_version": 1,
        "status": "observed",
        "source_commit": source_commit,
        "vm_create_sha256": sha256_file(vm_create_path),
        "vm_substrate": vm_substrate,
        "vm_ip": k3s_runtime["vm_ip"],
        "k3s_runtime": k3s_runtime,
        "node": node_readback["node"],
        "kubelet_version": kubelet,
        "node_ready": node_readback["ready"],
        "os_image": os_image,
        "cilium": cilium_readback,
        "runtime_contract": runtime_contract_readback,
        "flux_bootstrap_sha256": flux_contract["bootstrap_sha256"],
        "flux_source_revision": source_revision,
        "flux_controllers": flux_controllers,
        "flux_runtime_image_ids_baseline": flux_runtime_baseline,
        "flux": flux_readback,
        "deployments": deployment_readback,
        "namespace_security": namespace_security_readback,
        "data_deployments": data_deployment_readback,
        "data_services": data_service_readback,
        "application_workloads": application_workloads,
        "application_services": application_services,
        "application_service_accounts": application_service_accounts,
        "pods": pod_readback,
        "images": {
            **release_artifacts["images"],
            "ollama": api_containers.get("ollama"),
        },
        "migration": release_artifacts["migration"],
        "migration_complete": release_artifacts["migration_complete"],
        "semantic_provider": semantic_provider,
        "secrets": secret_readback,
        "pvcs": pvc_readback,
        "gateway": gateway_readback,
        "gateway_programmed": True,
        "httproute": httproute_readback,
        "gateway_data_plane": gateway_data_plane,
        "recovery_state": recovery_state,
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


def _libvirt_volume_present(pool: str, name: str) -> bool:
    result = run(
        ["virsh", "-c", LIBVIRT_URI, "vol-list", pool],
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        raise RuntimeErrorEB(
            f"cannot prove libvirt volume state for {pool}/{name}: {detail[-1000:]}"
        )
    volume_names: set[str] = set()
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("-"):
            continue
        volume_names.add(stripped.split(None, 1)[0])
    return name in volume_names


def _require_teardown_state_root(root: Path) -> dict[str, Any]:
    root_identity = str(root.resolve())
    creation_path = root / "receipts/vm-create.json"
    attempt_path = root / "receipts/vm-create-attempt.json"
    if creation_path.is_file():
        path = creation_path
        allowed_statuses = {"created"}
    elif attempt_path.is_file():
        path = attempt_path
        allowed_statuses = {"running"}
    else:
        raise RuntimeErrorEB(
            "Experiment-B teardown has no state-root ownership receipt"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeErrorEB(
            "Experiment-B teardown state-root ownership receipt is invalid"
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("status") not in allowed_statuses
        or payload.get("state_root") != root_identity
        or payload.get("vm") != VM_NAME
        or payload.get("pool") != POOL_NAME
    ):
        raise RuntimeErrorEB(
            "Experiment-B teardown state root does not own the VM resources"
        )
    return payload


def _require_teardown_live_identity(
    root: Path,
    ownership: dict[str, Any],
) -> dict[str, Any]:
    domain_present = _libvirt_resource_present("domain", VM_NAME)
    pool_present = _libvirt_resource_present("pool", POOL_NAME)
    if ownership.get("status") == "created":
        source_commit = ownership.get("source_commit")
        if not isinstance(source_commit, str) or not COMMIT_RE.fullmatch(source_commit):
            raise RuntimeErrorEB("teardown creation receipt has no exact source commit")
        config = load_config()
        _require_vm_create_receipt(ownership, source_commit, config, root)
        if not domain_present or not pool_present:
            raise RuntimeErrorEB(
                "teardown cannot prove the complete live creation substrate"
            )
        live = _live_vm_substrate(root, config)
        if live != ownership.get("substrate"):
            raise RuntimeErrorEB(
                "teardown live VM/pool/disk identity drifted from vm-create.json"
            )
        return {
            "identity_verified": True,
            "domain_present": True,
            "pool_present": True,
            "domain_target": str(live["uuid"]),
            "pool_target": str(live["pool_uuid"]),
            "substrate_sha256": _stable_json_sha256(live),
        }

    if domain_present or pool_present:
        raise RuntimeErrorEB(
            "teardown refuses same-named libvirt resources from an interrupted "
            "creation because their UUID/backing identity was never committed"
        )
    return {
        "identity_verified": True,
        "domain_present": False,
        "pool_present": False,
        "domain_target": None,
        "pool_target": None,
        "substrate_sha256": None,
    }


def teardown(root: Path) -> dict[str, Any]:
    ownership = _require_teardown_state_root(root)
    live_identity = _require_teardown_live_identity(root, ownership)
    evidence_hashes: dict[str, str] = {}
    receipts_dir = root / "receipts"
    if receipts_dir.is_dir():
        for path in sorted(receipts_dir.glob("*.json")):
            evidence_hashes[path.name] = sha256_file(path)

    if live_identity["domain_present"]:
        domain_target = str(live_identity["domain_target"])
        run(["virsh", "-c", LIBVIRT_URI, "destroy", domain_target], check=False)
        undefine = run(
            ["virsh", "-c", LIBVIRT_URI, "undefine", domain_target, "--nvram"],
            check=False,
        )
        if undefine.returncode != 0:
            run(
                ["virsh", "-c", LIBVIRT_URI, "undefine", domain_target],
                check=False,
            )

    volume_absence = {
        VOLUME_NAME: False,
        BASE_VOLUME: False,
    }
    if live_identity["pool_present"]:
        pool_target = str(live_identity["pool_target"])
        run(
            [
                "virsh",
                "-c",
                LIBVIRT_URI,
                "vol-delete",
                VOLUME_NAME,
                "--pool",
                pool_target,
            ],
            check=False,
        )
        run(
            [
                "virsh",
                "-c",
                LIBVIRT_URI,
                "vol-delete",
                BASE_VOLUME,
                "--pool",
                pool_target,
            ],
            check=False,
        )
        for volume_name in volume_absence:
            if _libvirt_volume_present(pool_target, volume_name):
                raise RuntimeErrorEB(
                    f"Experiment-B libvirt volume still exists after deletion: {volume_name}"
                )
            volume_absence[volume_name] = True
        run(
            ["virsh", "-c", LIBVIRT_URI, "pool-destroy", pool_target],
            check=False,
        )
        run(
            ["virsh", "-c", LIBVIRT_URI, "pool-delete", pool_target],
            check=False,
        )
        run(
            ["virsh", "-c", LIBVIRT_URI, "pool-undefine", pool_target],
            check=False,
        )
    else:
        for volume_name in volume_absence:
            volume_absence[volume_name] = True

    if _libvirt_resource_present("domain", VM_NAME):
        raise RuntimeErrorEB("Experiment-B VM still exists after teardown")
    if _libvirt_resource_present("pool", POOL_NAME):
        raise RuntimeErrorEB("Experiment-B storage pool still exists after teardown")

    volume_paths = {
        VOLUME_NAME: POOL_TARGET / VOLUME_NAME,
        BASE_VOLUME: POOL_TARGET / BASE_VOLUME,
    }
    for volume_name, path in volume_paths.items():
        if path.exists():
            raise RuntimeErrorEB(
                f"Experiment-B volume path still exists after teardown: {volume_name}"
            )
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
        "state_root": ownership["state_root"],
        "live_identity_verified": live_identity["identity_verified"],
        "substrate_sha256": live_identity["substrate_sha256"],
        "volumes_absent": volume_absence,
        "volume_paths_absent": {
            name: not path.exists() for name, path in volume_paths.items()
        },
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


def _t048_expected_projection_row(
    fixture: dict[str, Any],
    generation_id: str,
    dimension: int,
) -> dict[str, Any]:
    node_id = fixture.get("id")
    kind = fixture.get("kind")
    title = fixture.get("title")
    payload = fixture.get("payload")
    if (
        not isinstance(node_id, str)
        or not node_id
        or not isinstance(kind, str)
        or not kind
        or not isinstance(title, str)
        or not title
        or not isinstance(payload, dict)
        or not isinstance(dimension, int)
        or isinstance(dimension, bool)
        or dimension < 1
    ):
        raise RuntimeErrorEB("T048 fixture projection input is not canonical")
    visibility = _t048_canonical_visibility(fixture)
    is_public = visibility == "public"
    if is_public:
        raw_tags = payload.get("tags", [])
        if not isinstance(raw_tags, list) or any(
            not isinstance(tag, str) for tag in raw_tags
        ):
            raise RuntimeErrorEB("T048 public fixture tags are not canonical")
        summary = payload.get("summary")
        if summary is None:
            searchable_text = title
        elif isinstance(summary, str) and summary:
            searchable_text = summary
        else:
            raise RuntimeErrorEB("T048 public fixture summary is not canonical")
        projection_kind = kind
        projection_title = title
        tags = raw_tags
        language = "de"
        status = "active"
        visibility_scopes = ["public"]
        semantic_state = "ready"
        content_sha256 = T048_PUBLIC_CONTENT_SHA256
    else:
        projection_kind = T048_REDACTED_TEXT
        projection_title = T048_REDACTED_TEXT
        tags = []
        searchable_text = T048_REDACTED_TEXT
        language = "und"
        status = "hidden"
        visibility_scopes = []
        semantic_state = "unavailable"
        content_sha256 = T048_HIDDEN_CONTENT_SHA256
    return {
        "generation_id": generation_id,
        "node_id": node_id,
        "source_version": 1,
        "source_revision": "node-1",
        "content_sha256": content_sha256,
        "title": projection_title,
        "tags": tags,
        "searchable_text": searchable_text,
        "language": language,
        "kind": projection_kind,
        "status": status,
        "visibility_scopes": visibility_scopes,
        "semantic_state": semantic_state,
        "embedding_canonical": True,
    }


def _t048_fixture_edge_rows(
    manifest_path: Path,
    manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    files = manifest.get("files")
    counts = manifest.get("counts")
    if not isinstance(files, dict) or not isinstance(counts, dict):
        raise RuntimeErrorEB("T048 fixture manifest is missing canonical files/counts")
    edge_file = files.get("edges")
    edge_count = counts.get("edges")
    if (
        not isinstance(edge_file, dict)
        or not isinstance(edge_count, int)
        or isinstance(edge_count, bool)
        or edge_count < 1
    ):
        raise RuntimeErrorEB("T048 fixture manifest has no canonical edge binding")
    name = edge_file.get("name")
    expected_sha256 = edge_file.get("sha256")
    if (
        not isinstance(name, str)
        or not name
        or Path(name).name != name
        or not isinstance(expected_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
    ):
        raise RuntimeErrorEB("T048 fixture manifest edge binding is malformed")
    edge_path = manifest_path.parent / name
    if not edge_path.is_file() or sha256_file(edge_path) != expected_sha256:
        raise RuntimeErrorEB("T048 fixture edge CSV does not match its manifest digest")

    expected_header = (
        "id",
        "source_id",
        "target_id",
        "edge_kind",
        "created_at",
        "payload",
    )
    rows: list[dict[str, Any]] = []
    try:
        with edge_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if tuple(reader.fieldnames or ()) != expected_header:
                raise RuntimeErrorEB("T048 fixture edge CSV header is not canonical")
            for row in reader:
                rows.append(
                    {
                        "id": row["id"],
                        "source_id": row["source_id"],
                        "target_id": row["target_id"],
                        "edge_kind": row["edge_kind"],
                        "created_at": row["created_at"],
                        "payload": json.loads(row["payload"]),
                    }
                )
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        raise RuntimeErrorEB("cannot parse canonical T048 edge fixture") from exc
    if len(rows) != edge_count:
        raise RuntimeErrorEB(
            "T048 fixture edge CSV row count does not match its manifest"
        )
    return rows


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

    fixture_edge_rows = _t048_fixture_edge_rows(manifest, _manifest)
    database_edge_rows = live_binding._json_lines(
        _psql(
            root,
            r"""
SELECT json_build_object(
  'id', id,
  'source_id', source_id,
  'target_id', target_id,
  'edge_kind', edge_kind,
  'created_at', to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
  'payload', payload
)::text
FROM domain_edges
ORDER BY id;
""",
        ),
        "Experiment-B domain_edges query",
    )
    fixture_edges_sha = live_binding._rows_sha256(fixture_edge_rows)
    database_edges_sha = live_binding._rows_sha256(database_edge_rows)
    if (
        len(database_edge_rows) != len(fixture_edge_rows)
        or database_edges_sha != fixture_edges_sha
    ):
        raise RuntimeErrorEB(
            "live domain_edges content does not match the deterministic T048 fixture"
        )

    version_rows = live_binding._json_lines(
        _psql(
            root,
            r"""
SELECT json_build_object(
  'node_id', node_id,
  'source_version', source_version,
  'source_revision', source_revision,
  'deleted', deleted_at IS NOT NULL
)::text
FROM search_node_versions
ORDER BY node_id;
""",
        ),
        "Experiment-B search_node_versions query",
    )
    expected_version_rows = [
        {
            "node_id": row["id"],
            "source_version": 1,
            "source_revision": "node-1",
            "deleted": False,
        }
        for row in fixture_rows
    ]
    fixture_versions_sha = live_binding._rows_sha256(expected_version_rows)
    database_versions_sha = live_binding._rows_sha256(version_rows)
    if (
        len(version_rows) != len(expected_version_rows)
        or database_versions_sha != fixture_versions_sha
    ):
        raise RuntimeErrorEB(
            "live search_node_versions content does not match the deterministic T048 fixture"
        )

    generation_literal = live_binding._sql_literal(generation_id)
    generation_rows = live_binding._json_lines(
        _psql(
            root,
            f"""
SELECT json_build_object(
  'generation_id', generation_id,
  'provider', provider,
  'model_id', model_id,
  'model_revision', model_revision,
  'runtime_identity', runtime_identity,
  'dimension', dimension,
  'document_revision', document_revision,
  'normalization_revision', normalization_revision,
  'ranking_revision', ranking_revision,
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
    semantic = load_config()["semantic_search"]
    expected_generation_identity = {
        "generation_id": generation_id,
        "provider": semantic["provider"],
        "model_id": semantic["model_id"],
        "model_revision": semantic["model_revision"],
        "runtime_identity": semantic["runtime_identity"],
        "dimension": int(semantic["dimension"]),
        "document_revision": T048_DOCUMENT_REVISION,
        "normalization_revision": T048_NORMALIZATION_REVISION,
        "ranking_revision": T048_RANKING_REVISION,
        "state": "active",
    }
    if any(
        generation.get(key) != value
        for key, value in expected_generation_identity.items()
    ):
        raise RuntimeErrorEB(
            "Experiment-B active T048 generation identity is not canonical"
        )
    dimension = int(semantic["dimension"])

    projection_rows = live_binding._json_lines(
        _psql(
            root,
            f"""
SELECT json_build_object(
  'generation_id', p.generation_id,
  'node_id', p.node_id,
  'source_version', p.source_version,
  'source_revision', p.source_revision,
  'content_sha256', p.content_sha256,
  'title', p.title,
  'tags', p.tags,
  'searchable_text', p.searchable_text,
  'language', p.language,
  'kind', p.kind,
  'status', p.status,
  'visibility_scopes', p.visibility_scopes,
  'semantic_state', p.semantic_state,
  'embedding_canonical',
    CASE
      WHEN n.search_visibility = 'public'
      THEN p.embedding = array_fill(0.0::DOUBLE PRECISION, ARRAY[g.dimension])
      ELSE p.embedding IS NULL
    END
)::text
FROM search_node_projections p
JOIN domain_nodes n ON n.id = p.node_id
JOIN search_index_generations g ON g.generation_id = p.generation_id
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
    for projection in projection_rows:
        node_id = projection.get("node_id")
        fixture = fixture_by_id.get(node_id)
        if fixture is None:
            raise RuntimeErrorEB(
                f"Experiment-B search projection {node_id!r} is absent from fixture"
            )
        expected_projection_rows.append(
            _t048_expected_projection_row(
                fixture,
                generation_id,
                dimension,
            )
        )
    projection_sha = live_binding._rows_sha256(projection_rows)
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
        "domain_edges_count": len(database_edge_rows),
        "fixture_edges_content_sha256": fixture_edges_sha,
        "database_edges_content_sha256": database_edges_sha,
        "search_node_versions_count": len(version_rows),
        "fixture_versions_content_sha256": fixture_versions_sha,
        "database_versions_content_sha256": database_versions_sha,
        "generation_id": generation_id,
        "expected_nodes": int(expected_nodes),
        "completed_nodes": int(completed_nodes),
        "active_projection_count": len(projection_rows),
        "fixture_projection_content_sha256": expected_projection_sha,
        "database_projection_content_sha256": projection_sha,
    }


def _validated_t048_fixture_receipt(
    root: Path,
    source_commit: str,
) -> dict[str, Any]:
    if not COMMIT_RE.fullmatch(source_commit):
        raise RuntimeErrorEB("T048 fixture validation source commit is not exact")
    receipt_path = root / "receipts/t048-fixture.json"
    if not receipt_path.is_file():
        raise RuntimeErrorEB("T048 load proof requires a seeded fixture receipt")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeErrorEB("T048 fixture receipt is not valid JSON") from exc
    if (
        not isinstance(receipt, dict)
        or receipt.get("status") != "loaded"
        or receipt.get("source_commit") != source_commit
    ):
        raise RuntimeErrorEB("T048 fixture receipt is not bound to the load-proof source")

    manifest = root / "performance/fixture/manifest.json"
    if receipt.get("manifest") != str(manifest) or not manifest.is_file():
        raise RuntimeErrorEB("T048 fixture receipt does not name the canonical manifest")
    if receipt.get("manifest_sha256") != sha256_file(manifest):
        raise RuntimeErrorEB("T048 fixture receipt manifest digest is stale")

    generation_id = receipt.get("generation_id")
    expected_generation = str(load_config()["semantic_search"]["generation_id"])
    if (
        not isinstance(generation_id, str)
        or generation_id != expected_generation
    ):
        raise RuntimeErrorEB("T048 fixture receipt generation is not current")
    current_live_binding = _t048_live_fixture_binding(
        root, manifest, generation_id
    )
    if receipt.get("live_binding") != current_live_binding:
        raise RuntimeErrorEB(
            "T048 fixture receipt does not match current live fixture contents"
        )
    return receipt


def seed_t048_fixture(root: Path) -> dict[str, Any]:
    _invalidate_receipts(root, FIXTURE_ATTEMPT_INVALIDATES)
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
        or _current_protected_main_commit() != source_commit
    ):
        raise RuntimeErrorEB("T048 fixture release is not current protected main")
    _require_kubernetes_target_binding(root, source_commit)
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
        and not receipt_path.is_file()
    ):
        return emit_receipt()
    if existing_nodes or existing_edges:
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
SELECT pg_advisory_xact_lock(
    hashtextextended('weltgewebe.search.generation.activation', 0)
);
DO $$
DECLARE
    generation_count BIGINT;
BEGIN
    IF EXISTS (SELECT 1 FROM domain_nodes)
       OR EXISTS (SELECT 1 FROM domain_edges) THEN
        RAISE EXCEPTION 'Experiment-B T048 target domain changed before seed lock';
    END IF;
    IF EXISTS (SELECT 1 FROM search_node_versions)
       OR EXISTS (SELECT 1 FROM search_projection_jobs)
       OR EXISTS (SELECT 1 FROM search_node_projections) THEN
        RAISE EXCEPTION 'Experiment-B T048 search ledger is not empty before fresh seed';
    END IF;
    SELECT count(*) INTO generation_count FROM search_index_generations;
    IF generation_count > 1 THEN
        RAISE EXCEPTION 'Experiment-B T048 has unexpected pre-seed generations';
    END IF;
    IF generation_count = 1 THEN
        IF NOT EXISTS (
            SELECT 1
              FROM search_index_generations
             WHERE generation_id = '{semantic["generation_id"]}'
               AND provider = '{semantic["provider"]}'
               AND model_id = '{semantic["model_id"]}'
               AND model_revision = '{semantic["model_revision"]}'
               AND runtime_identity = '{semantic["runtime_identity"]}'
               AND dimension = {int(semantic["dimension"])}
               AND document_revision = '{T048_DOCUMENT_REVISION}'
               AND normalization_revision = '{T048_NORMALIZATION_REVISION}'
               AND ranking_revision = '{T048_RANKING_REVISION}'
               AND state = 'building'
               AND expected_nodes = 0
               AND completed_nodes = 0
               AND activated_at IS NULL
        ) THEN
            RAISE EXCEPTION 'Experiment-B T048 pre-seed generation is not the empty worker generation';
        END IF;
        DELETE FROM search_index_generations
         WHERE generation_id = '{semantic["generation_id"]}';
    END IF;
END
$$;
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
    '{T048_DOCUMENT_REVISION}',
    '{T048_NORMALIZATION_REVISION}',
    '{T048_RANKING_REVISION}',
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
        WHEN n.search_visibility = 'public' THEN '{T048_PUBLIC_CONTENT_SHA256}'
        ELSE '{T048_HIDDEN_CONTENT_SHA256}'
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


def _start_api_port_forward(
    root: Path,
    pod_name: str,
) -> tuple[subprocess.Popen[Any], int, Any, Any]:
    if not isinstance(pod_name, str) or not pod_name:
        raise RuntimeErrorEB("API port-forward requires a verified Pod name")
    port = _reserve_loopback_port()
    evidence_dir = root / "performance"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    stdout = (evidence_dir / "port-forward.stdout").open("w", encoding="utf-8")
    stderr = (evidence_dir / "port-forward.stderr").open("w", encoding="utf-8")
    kubectl = toolchain(root)["tools"]["kubectl"]
    process = subprocess.Popen(
        [
            kubectl, "-n", APP_NAMESPACE, "port-forward",
            f"pod/{pod_name}", f"{port}:8080", "--address=127.0.0.1",
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


def _require_t048_api_release_binding(
    root: Path,
    source_commit: str,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    release_path = root / "receipts/release.json"
    try:
        release = json.loads(release_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeErrorEB("T048 proof requires a valid release receipt") from exc
    api_digest = release.get("api_digest") if isinstance(release, dict) else None
    if (
        not isinstance(release, dict)
        or release.get("schema_version") != 1
        or release.get("status") != "applied"
        or release.get("source_commit") != source_commit
        or not isinstance(api_digest, str)
        or not DIGEST_RE.fullmatch(api_digest)
    ):
        raise RuntimeErrorEB("T048 proof release image binding is invalid")

    pod_name, pod = _api_pod(root)
    expected_images = {
        "containers": {
            "api": f"ghcr.io/heimgewebe/commonthing-api@{api_digest}",
            "search-worker": f"ghcr.io/heimgewebe/commonthing-api@{api_digest}",
            "ollama": str(load_config()["semantic_search"]["ollama_image"]),
        },
        "init_containers": {},
    }
    readback = _require_running_pod_image_contract(
        [pod],
        namespace=APP_NAMESPACE,
        workload="weltgewebe-api",
        expected_replicas=1,
        expected_images=expected_images,
        required_labels={"app.kubernetes.io/name": "weltgewebe-api"},
        context="T048 API Pod",
    )
    return pod_name, pod, readback


def _require_t048_api_runtime_binding(
    root: Path,
    source_commit: str,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    pod_name, pod, image_readback = _require_t048_api_release_binding(
        root, source_commit
    )
    release_path = root / "receipts/release.json"
    try:
        release = json.loads(release_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeErrorEB(
            "T048 proof requires a valid release receipt"
        ) from exc
    deployment = _kubectl_json(
        root,
        [
            "-n",
            APP_NAMESPACE,
            "get",
            "deployment",
            "weltgewebe-api",
        ],
    )
    workload = _require_live_application_workloads(
        root,
        release,
        {"weltgewebe-api": deployment},
        {"weltgewebe-api": [pod]},
        names=("weltgewebe-api",),
    ).get("weltgewebe-api")
    if (
        not isinstance(workload, dict)
        or workload.get("canonical") is not True
        or not isinstance(workload.get("contract_sha256"), str)
        or not isinstance(workload.get("pod_contract_sha256"), str)
    ):
        raise RuntimeErrorEB(
            "T048 API full runtime contract is invalid"
        )
    return (
        pod_name,
        pod,
        {
            **image_readback,
            "contract_sha256": workload["contract_sha256"],
            "pod_contract_sha256": workload["pod_contract_sha256"],
            "canonical": True,
        },
    )


def _require_t048_postgres_runtime_binding(
    root: Path,
) -> dict[str, Any]:
    postgres_manifest = CLUSTER / "data/postgres.yaml"
    expected = _versioned_data_deployment_contract(
        postgres_manifest, "postgres"
    )
    expected_resources = _versioned_data_container_resources(
        postgres_manifest, "postgres", "postgres"
    )
    live_data = _require_live_data_deployments(
        root, ("postgres",)
    )
    postgres = live_data.get("postgres")
    if (
        not isinstance(postgres, dict)
        or postgres.get("canonical") is not True
        or postgres.get("contract_sha256")
        != expected["contract_sha256"]
        or postgres.get("pod_contract_sha256")
        != expected["pod_contract_sha256"]
        or postgres.get("images_sha256")
        != _stable_json_sha256(expected["images"])
    ):
        raise RuntimeErrorEB(
            "T048 PostgreSQL full runtime contract drifted"
        )
    pod_readback = postgres.get("pods")
    if not isinstance(pod_readback, dict):
        raise RuntimeErrorEB(
            "T048 PostgreSQL Pod runtime contract is missing"
        )
    runtime_image_ids_sha256 = pod_readback.get(
        "runtime_image_ids_sha256"
    )
    if (
        not isinstance(runtime_image_ids_sha256, str)
        or re.fullmatch(
            r"[0-9a-f]{64}", runtime_image_ids_sha256
        )
        is None
    ):
        raise RuntimeErrorEB(
            "T048 PostgreSQL runtime image identity is invalid"
        )
    return {
        "images_sha256": postgres["images_sha256"],
        "resources_sha256": _stable_json_sha256(
            expected_resources
        ),
        "contract_sha256": postgres["contract_sha256"],
        "pod_contract_sha256": postgres["pod_contract_sha256"],
        "runtime_image_ids_sha256": runtime_image_ids_sha256,
        "pods": pod_readback,
        "canonical": True,
    }


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


def _require_api_resource_limits(
    pod: Any, config: dict[str, Any]
) -> tuple[float, int]:
    if not isinstance(pod, dict):
        raise RuntimeErrorEB("API pod resource contract payload is not an object")
    containers = pod.get("spec", {}).get("containers", [])
    if not isinstance(containers, list):
        raise RuntimeErrorEB("API pod container inventory is invalid")
    api_containers = [
        item for item in containers
        if isinstance(item, dict) and item.get("name") == "api"
    ]
    if len(api_containers) != 1:
        raise RuntimeErrorEB("API pod must contain exactly one api container")
    live_limits = api_containers[0].get("resources", {}).get("limits", {})
    expected_limits = config.get("runtime_binding", {}).get("api_resource_limits")
    if not isinstance(live_limits, dict) or not isinstance(expected_limits, dict):
        raise RuntimeErrorEB("Experiment-B API resource limits are missing")
    try:
        live_cpu = _parse_cpu_quantity(str(live_limits.get("cpu", "")))
        live_memory = _parse_memory_quantity(str(live_limits.get("memory", "")))
        expected_cpu = _parse_cpu_quantity(str(expected_limits.get("cpu", "")))
        expected_memory = _parse_memory_quantity(str(expected_limits.get("memory", "")))
    except (TypeError, ValueError) as exc:
        raise RuntimeErrorEB("Experiment-B API resource limits are invalid") from exc
    if live_cpu != expected_cpu or live_memory != expected_memory:
        raise RuntimeErrorEB(
            "live API resource limits drifted from the Experiment-B contract"
        )
    return expected_cpu, expected_memory


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
    report_path, attempt_path, attempt_started_at_unix_ms = (
        _begin_live_check_attempt(root, "t048-load", source_commit)
    )
    if _current_protected_main_commit() != source_commit:
        raise RuntimeErrorEB("T048 proof source is not current protected main")
    (
        target_receipt_before,
        target_ip_before,
        target_server_before,
    ) = _require_kubernetes_target_binding(root, source_commit)
    target_binding_before = {
        "vm_ip": target_ip_before,
        "kubeconfig_sha256": target_receipt_before["kubeconfig_sha256"],
        "server": target_server_before,
    }
    postgres_binding_before = _require_t048_postgres_runtime_binding(root)
    fixture_receipt = _validated_t048_fixture_receipt(root, source_commit)
    fixture_binding_before = {
        "manifest": fixture_receipt.get("manifest"),
        "manifest_sha256": fixture_receipt.get("manifest_sha256"),
        "generation_id": fixture_receipt.get("generation_id"),
        "live_binding": fixture_receipt.get("live_binding"),
    }
    evidence, _domain_scale = _performance_modules()
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

    pod_name, pod, api_image_binding_before = _require_t048_api_runtime_binding(
        root, source_commit
    )
    declared_cpu, declared_memory = _require_api_resource_limits(
        pod, load_config()
    )

    process, port, pf_stdout, pf_stderr = _start_api_port_forward(
        root, pod_name
    )
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
        (
            post_pod_name,
            _post_pod,
            api_image_binding_after,
        ) = _require_t048_api_runtime_binding(root, source_commit)
        postgres_binding_after = _require_t048_postgres_runtime_binding(root)
        (
            target_receipt_after,
            target_ip_after,
            target_server_after,
        ) = _require_kubernetes_target_binding(root, source_commit)
        target_binding_after = {
            "vm_ip": target_ip_after,
            "kubeconfig_sha256": target_receipt_after["kubeconfig_sha256"],
            "server": target_server_after,
        }
        if (
            post_pod_name != pod_name
            or api_image_binding_after["runtime_image_ids_sha256"]
            != api_image_binding_before["runtime_image_ids_sha256"]
            or api_image_binding_after["contract_sha256"]
            != api_image_binding_before["contract_sha256"]
            or api_image_binding_after["pod_contract_sha256"]
            != api_image_binding_before["pod_contract_sha256"]
        ):
            raise RuntimeErrorEB(
                "API runtime contract changed during the T048 measurement"
            )
        if (
            postgres_binding_after["runtime_image_ids_sha256"]
            != postgres_binding_before["runtime_image_ids_sha256"]
            or postgres_binding_after["images_sha256"]
            != postgres_binding_before["images_sha256"]
            or postgres_binding_after["resources_sha256"]
            != postgres_binding_before["resources_sha256"]
            or postgres_binding_after["contract_sha256"]
            != postgres_binding_before["contract_sha256"]
            or postgres_binding_after["pod_contract_sha256"]
            != postgres_binding_before["pod_contract_sha256"]
        ):
            raise RuntimeErrorEB(
                "PostgreSQL runtime contract changed during the T048 measurement"
            )
        if target_binding_after != target_binding_before:
            raise RuntimeErrorEB(
                "Kubernetes target identity changed during the T048 measurement"
            )
        fixture_receipt_after = _validated_t048_fixture_receipt(
            root, source_commit
        )
        fixture_binding_after = {
            "manifest": fixture_receipt_after.get("manifest"),
            "manifest_sha256": fixture_receipt_after.get("manifest_sha256"),
            "generation_id": fixture_receipt_after.get("generation_id"),
            "live_binding": fixture_receipt_after.get("live_binding"),
        }
        if fixture_binding_after != fixture_binding_before:
            raise RuntimeErrorEB(
                "T048 fixture changed during the load measurement"
            )
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
            "fixture_live_binding_sha256": _stable_json_sha256(
                fixture_binding_before
            ),
            "api_runtime_image_ids_sha256": api_image_binding_before[
                "runtime_image_ids_sha256"
            ],
            "api_contract_sha256": api_image_binding_before[
                "contract_sha256"
            ],
            "api_pod_contract_sha256": api_image_binding_before[
                "pod_contract_sha256"
            ],
            "postgres_runtime_image_ids_sha256": postgres_binding_before[
                "runtime_image_ids_sha256"
            ],
            "postgres_resources_sha256": postgres_binding_before[
                "resources_sha256"
            ],
            "postgres_contract_sha256": postgres_binding_before[
                "contract_sha256"
            ],
            "postgres_pod_contract_sha256": postgres_binding_before[
                "pod_contract_sha256"
            ],
            "kubernetes_target_sha256": _stable_json_sha256(
                target_binding_before
            ),
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


def _gateway_data_plane_readback(
    root: Path, source_commit: str
) -> dict[str, Any]:
    if not COMMIT_RE.fullmatch(source_commit):
        raise RuntimeErrorEB("Gateway data-plane source commit is not exact")
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

    return {"gateway": base, "checks": checks}


def functional_readback(root: Path, source_commit: str) -> dict[str, Any]:
    if not COMMIT_RE.fullmatch(source_commit):
        raise RuntimeErrorEB("functional readback source commit is not exact")
    receipt_path, attempt_path, attempt_started_at_unix_ms = (
        _begin_live_check_attempt(root, "functional-readback", source_commit)
    )
    if _current_protected_main_commit() != source_commit:
        raise RuntimeErrorEB("functional readback source is not current protected main")
    (
        target_receipt_before,
        target_ip_before,
        target_server_before,
    ) = _require_kubernetes_target_binding(root, source_commit)
    target_binding_before = {
        "vm_ip": target_ip_before,
        "kubeconfig_sha256": target_receipt_before["kubeconfig_sha256"],
        "server": target_server_before,
    }
    data_plane = _gateway_data_plane_readback(root, source_commit)
    base = str(data_plane["gateway"])
    checks = data_plane["checks"]
    jetstream = _jetstream_signature(root)
    if jetstream["messages"] < 1:
        raise RuntimeErrorEB("Experiment-B JetStream contains no persisted test messages")
    (
        target_receipt_after,
        target_ip_after,
        target_server_after,
    ) = _require_kubernetes_target_binding(root, source_commit)
    target_binding_after = {
        "vm_ip": target_ip_after,
        "kubeconfig_sha256": target_receipt_after["kubeconfig_sha256"],
        "server": target_server_after,
    }
    if target_binding_after != target_binding_before:
        raise RuntimeErrorEB(
            "Kubernetes target identity changed during functional readback"
        )
    receipt = {
        "schema_version": 1,
        "status": "pass",
        "source_commit": source_commit,
        "gateway": base,
        "checks": checks,
        "jetstream": jetstream,
        "kubernetes_target_sha256": _stable_json_sha256(
            target_binding_before
        ),
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


def _pvc_volume_identity(root: Path, claim_name: str) -> dict[str, str]:
    pvc = _kubectl_json(
        root, ["-n", DATA_NAMESPACE, "get", "pvc", claim_name]
    )
    metadata = pvc.get("metadata", {}) if isinstance(pvc, dict) else {}
    spec = pvc.get("spec", {}) if isinstance(pvc, dict) else {}
    pvc_uid = metadata.get("uid") if isinstance(metadata, dict) else None
    volume_name = spec.get("volumeName") if isinstance(spec, dict) else None
    if (
        metadata.get("name") != claim_name
        or metadata.get("namespace") != DATA_NAMESPACE
        or metadata.get("deletionTimestamp") is not None
        or not isinstance(pvc_uid, str)
        or not pvc_uid
        or not isinstance(volume_name, str)
        or not volume_name
    ):
        raise RuntimeErrorEB(
            f"data PVC identity is invalid or unbound: {claim_name}"
        )
    pv = _kubectl_json(root, ["get", "pv", volume_name])
    pv_metadata = pv.get("metadata", {}) if isinstance(pv, dict) else {}
    pv_spec = pv.get("spec", {}) if isinstance(pv, dict) else {}
    claim_ref = pv_spec.get("claimRef", {}) if isinstance(pv_spec, dict) else {}
    pv_uid = pv_metadata.get("uid") if isinstance(pv_metadata, dict) else None
    if (
        pv_metadata.get("name") != volume_name
        or pv_metadata.get("deletionTimestamp") is not None
        or not isinstance(pv_uid, str)
        or not pv_uid
        or pv_spec.get("storageClassName") != "local-path"
        or not isinstance(claim_ref, dict)
        or claim_ref.get("namespace") != DATA_NAMESPACE
        or claim_ref.get("name") != claim_name
        or claim_ref.get("uid") != pvc_uid
    ):
        raise RuntimeErrorEB(
            f"data PV identity is not bound to the expected claim: {claim_name}"
        )
    return {
        "pvc_uid": pvc_uid,
        "pv_name": volume_name,
        "pv_uid": pv_uid,
    }


def _wait_pv_absent(
    root: Path,
    pv_name: str,
    *,
    timeout_seconds: int = 180,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        result = _kubectl(
            root,
            ["get", "pv", pv_name, "--ignore-not-found=true", "-o", "name"],
            timeout=30,
        )
        if not result.stdout.strip():
            return
        time.sleep(2)
    raise RuntimeErrorEB(
        f"old persistent volume did not disappear before restore: {pv_name}"
    )


def _require_empty_replacement_pvc(
    root: Path,
    claim_name: str,
    old_identity: dict[str, str],
) -> dict[str, Any]:
    workload = "postgres" if claim_name == "postgres-data" else "nats"
    expected = _versioned_data_deployment_contract(
        CLUSTER / f"data/{workload}.yaml", workload
    )
    image = expected["images"]["containers"].get(workload)
    if not isinstance(image, str) or not image:
        raise RuntimeErrorEB(
            f"replacement PVC probe image is unavailable: {claim_name}"
        )
    try:
        documents = [
            item
            for item in yaml.safe_load_all(
                (CLUSTER / f"data/{workload}.yaml").read_text(encoding="utf-8")
            )
            if isinstance(item, dict)
            and item.get("kind") == "Deployment"
            and item.get("metadata", {}).get("name") == workload
        ]
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeErrorEB(
            f"replacement PVC probe contract is invalid: {claim_name}"
        ) from exc
    if len(documents) != 1:
        raise RuntimeErrorEB(
            f"replacement PVC probe Deployment is ambiguous: {claim_name}"
        )
    security = documents[0].get("spec", {}).get("template", {}).get("spec", {}).get(
        "securityContext", {}
    )
    run_as_user = security.get("runAsUser") if isinstance(security, dict) else None
    run_as_group = security.get("runAsGroup") if isinstance(security, dict) else None
    fs_group = security.get("fsGroup") if isinstance(security, dict) else None
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in (run_as_user, run_as_group, fs_group)
    ):
        raise RuntimeErrorEB(
            f"replacement PVC probe security contract is invalid: {claim_name}"
        )

    pod_name = f"commonthing-experiment-b-{workload}-empty-probe"
    manifest = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": pod_name, "namespace": DATA_NAMESPACE},
        "spec": {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": run_as_user,
                "runAsGroup": run_as_group,
                "fsGroup": fs_group,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "probe",
                    "image": image,
                    "command": ["/bin/sh", "-c", "sleep 3600"],
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "capabilities": {"drop": ["ALL"]},
                        "readOnlyRootFilesystem": True,
                    },
                    "volumeMounts": [
                        {"name": "data", "mountPath": "/probe"},
                        {"name": "tmp", "mountPath": "/tmp"},
                    ],
                }
            ],
            "volumes": [
                {
                    "name": "data",
                    "persistentVolumeClaim": {"claimName": claim_name},
                },
                {"name": "tmp", "emptyDir": {}},
            ],
        },
    }
    kubectl_apply(root, json.dumps(manifest, sort_keys=True))
    try:
        _kubectl(
            root,
            [
                "-n",
                DATA_NAMESPACE,
                "wait",
                "--for=condition=Ready",
                f"pod/{pod_name}",
                "--timeout=3m",
            ],
            timeout=210,
        )
        new_identity = _pvc_volume_identity(root, claim_name)
        if (
            new_identity["pvc_uid"] == old_identity["pvc_uid"]
            or new_identity["pv_name"] == old_identity["pv_name"]
            or new_identity["pv_uid"] == old_identity["pv_uid"]
        ):
            raise RuntimeErrorEB(
                f"replacement PVC reused the previous storage identity: {claim_name}"
            )
        contents = _kubectl(
            root,
            [
                "-n",
                DATA_NAMESPACE,
                "exec",
                pod_name,
                "--",
                "find",
                "/probe",
                "-mindepth",
                "1",
                "-maxdepth",
                "1",
                "-print",
                "-quit",
            ],
            timeout=30,
        )
        if contents.stdout.strip():
            raise RuntimeErrorEB(
                f"replacement PVC is not empty before restore: {claim_name}"
            )
        return {
            "old": old_identity,
            "new": new_identity,
            "empty_before_restore": True,
        }
    finally:
        _delete_pod(root, DATA_NAMESPACE, pod_name)


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
        timeout=150,
    )
    readback = _kubectl(
        root,
        [
            "-n", namespace, "get", "pod", name,
            "--ignore-not-found=true", "-o", "name",
        ],
        timeout=30,
    )
    if readback.stdout.strip():
        raise RuntimeErrorEB(
            f"transfer pod still exists after deletion: {namespace}/{name}"
        )


def recovery_proof(root: Path) -> dict[str, Any]:
    release_path = root / "receipts/release.json"
    if not release_path.is_file():
        raise RuntimeErrorEB("recovery proof requires an applied release receipt")
    release = json.loads(release_path.read_text(encoding="utf-8"))
    source_commit = str(release.get("source_commit", ""))
    if not COMMIT_RE.fullmatch(source_commit):
        raise RuntimeErrorEB("recovery proof release binding is not exact")
    recovery_failed_receipt = root / "receipts/recovery-failed.json"
    if recovery_failed_receipt.is_file():
        raise RuntimeErrorEB(
            "recovery proof refuses a retry after a failed attempt; rebuild the "
            "Experiment-B cell to establish a fresh baseline"
        )
    _require_recovery_attempt_clear(root)
    _invalidate_receipts(root, RECOVERY_ATTEMPT_INVALIDATES)
    if _current_protected_main_commit() != source_commit:
        raise RuntimeErrorEB("recovery proof release is not current protected main")
    _require_kubernetes_target_binding(root, source_commit)
    backup_dir = root / "recovery"
    backup_dir.mkdir(parents=True, exist_ok=True)
    db_dump = backup_dir / "postgres.dump"
    nats_tar = backup_dir / "nats.tar"
    before_db: dict[str, Any] | None = None
    before_nats: dict[str, Any] | None = None
    recovery_receipt, recovery_attempt, recovery_started_at = (
        _begin_live_check_attempt(root, "recovery", source_commit)
    )

    destructive_started = time.monotonic()
    try:
        _flux_suspend(root, "commonthing-experiment-b-app")
        _flux_suspend(root, "commonthing-experiment-b-data")
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
        old_pvc_identities = {
            name: _pvc_volume_identity(root, name)
            for name in ("postgres-data", "nats-data")
        }
        _kubectl(
            root,
            [
                "-n", DATA_NAMESPACE, "delete", "pvc",
                "postgres-data", "nats-data", "--wait=true", "--timeout=5m",
            ],
            timeout=330,
        )
        for identity in old_pvc_identities.values():
            _wait_pv_absent(root, identity["pv_name"])
        storage = (CLUSTER / "data/storage.yaml").read_text(encoding="utf-8")
        kubectl_apply(root, storage)
        pvc_replacements = {
            name: _require_empty_replacement_pvc(
                root, name, old_pvc_identities[name]
            )
            for name in ("postgres-data", "nats-data")
        }

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
        atomic_json(
            recovery_attempt,
            {
                "schema_version": 1,
                "status": "failed",
                "source_commit": source_commit,
                "receipt": recovery_receipt.name,
                "started_at_unix_ms": recovery_started_at,
                "finished_at_unix_ms": time.time_ns() // 1_000_000,
                "failure_receipt": recovery_failed_receipt.name,
                "failure_receipt_sha256": sha256_file(
                    recovery_failed_receipt
                ),
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
        "pvc_replacements": pvc_replacements,
        "pvc_delete_to_prove": True,
        "replacement_pvcs_empty_before_restore": True,
        "production_data_used": False,
    }
    atomic_json(recovery_receipt, receipt)
    _complete_live_check_attempt(
        recovery_attempt,
        recovery_receipt,
        source_commit,
        recovery_started_at,
        "pass",
    )
    return receipt


def _require_stored_pod_image_contract(
    observed: Any,
    expected_replicas: int,
    expected_images: dict[str, dict[str, str]],
    context: str,
) -> None:
    if not isinstance(observed, dict) or not isinstance(
        observed.get("pods"), dict
    ):
        raise RuntimeErrorEB(f"status does not prove the live {context} contract")
    try:
        runtime_binding_sha256 = _pod_runtime_image_ids_sha256(
            observed["pods"], expected_images
        )
    except RuntimeErrorEB as exc:
        raise RuntimeErrorEB(
            f"status does not prove the live {context} contract"
        ) from exc
    if (
        observed.get("expected_replicas") != expected_replicas
        or observed.get("observed_replicas") != expected_replicas
        or observed.get("requested_images_sha256")
        != _stable_json_sha256(expected_images)
        or observed.get("runtime_image_ids_sha256")
        != runtime_binding_sha256
        or observed.get("images_canonical") is not True
        or len(observed["pods"]) != expected_replicas
    ):
        raise RuntimeErrorEB(f"status does not prove the live {context} contract")
    for pod in observed["pods"].values():
        runtime_ids = pod.get("runtime_image_ids") if isinstance(pod, dict) else None
        if (
            not isinstance(pod, dict)
            or pod.get("ready") is not True
            or pod.get("requested_images_sha256")
            != _stable_json_sha256(expected_images)
            or not isinstance(runtime_ids, dict)
            or set(runtime_ids) != {"containers", "init_containers"}
        ):
            raise RuntimeErrorEB(f"status does not prove the live {context} contract")
        for group in ("containers", "init_containers"):
            ids = runtime_ids.get(group)
            if (
                not isinstance(ids, dict)
                or set(ids) != set(expected_images[group])
            ):
                raise RuntimeErrorEB(
                    f"status does not prove the live {context} contract"
                )
            for name, image in expected_images[group].items():
                observed_digest = _runtime_image_id_digest(ids.get(name))
                if observed_digest is None:
                    raise RuntimeErrorEB(
                        f"status does not prove the live {context} contract"
                    )
                if "@" in image:
                    expected_digest = image.rsplit("@", 1)[1]
                    if (
                        not DIGEST_RE.fullmatch(expected_digest)
                        or not secrets.compare_digest(
                            observed_digest, expected_digest
                        )
                    ):
                        raise RuntimeErrorEB(
                            f"status does not prove the live {context} contract"
                        )


def portability_report(root: Path) -> dict[str, Any]:
    _invalidate_receipts(root, PORTABILITY_DERIVED_RECEIPTS)
    recovery_failed_receipt = root / "receipts/recovery-failed.json"
    if recovery_failed_receipt.is_file():
        raise RuntimeErrorEB(
            "portability report is blocked by the latest failed recovery attempt"
        )
    expected_status = {
        "vm-create.json": "created",
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
        "recovery-attempt.json": "pass",
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
        "vm-create.json",
        "k3s.json",
        "platform.json",
        "secrets.json",
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
        "recovery",
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

    config = load_config()
    _require_vm_create_receipt(
        payloads["vm-create.json"], source_commit, config, root
    )
    if (
        payloads["status.json"].get("vm_create_sha256") != receipts["vm-create.json"]
        or payloads["status.json"].get("vm_substrate")
        != payloads["vm-create.json"]["substrate"]
    ):
        raise RuntimeErrorEB("status is not bound to the current VM creation substrate receipt")
    cilium_status = payloads["status.json"].get("cilium")
    if (
        not isinstance(cilium_status, dict)
        or cilium_status.get("chart_version") != config["cilium"]["chart_version"]
        or cilium_status.get("gateway_api") is not True
        or cilium_status.get("kube_proxy_replacement") is not True
        or cilium_status.get("daemonset_images_canonical") is not True
        or cilium_status.get("operator_images_canonical") is not True
        or not isinstance(cilium_status.get("daemonset_images"), dict)
        or not isinstance(cilium_status.get("operator_images"), dict)
        or not isinstance(cilium_status.get("operator"), dict)
        or cilium_status["operator"].get("available") is not True
        or cilium_status["operator"].get("desired_replicas") != 1
        or not isinstance(cilium_status.get("daemonset_desired"), int)
        or isinstance(cilium_status.get("daemonset_desired"), bool)
        or cilium_status["daemonset_desired"] < 1
        or cilium_status.get("daemonset_ready")
        != cilium_status.get("daemonset_desired")
        or cilium_status.get("kube_proxy_present") is not False
    ):
        raise RuntimeErrorEB("status does not prove the live Cilium contract")
    _require_stored_pod_image_contract(
        cilium_status.get("daemonset_pods"),
        cilium_status["daemonset_desired"],
        cilium_status["daemonset_images"],
        "Cilium DaemonSet Pod",
    )
    _require_stored_pod_image_contract(
        cilium_status.get("operator_pods"),
        1,
        cilium_status["operator_images"],
        "Cilium operator Pod",
    )
    platform_cilium_baseline = payloads["platform.json"].get(
        "cilium_runtime_image_ids"
    )
    if (
        not isinstance(platform_cilium_baseline, dict)
        or set(platform_cilium_baseline) != {"daemonset", "operator"}
        or cilium_status.get("runtime_image_ids_baseline")
        != platform_cilium_baseline
        or cilium_status["daemonset_pods"].get("runtime_image_ids_sha256")
        != platform_cilium_baseline.get("daemonset")
        or cilium_status["operator_pods"].get("runtime_image_ids_sha256")
        != platform_cilium_baseline.get("operator")
    ):
        raise RuntimeErrorEB(
            "status does not prove the installed Cilium runtime image baseline"
        )

    status_payload = payloads["status.json"]
    k3s_receipt = payloads["k3s.json"]
    k3s_status = status_payload.get("k3s_runtime")
    expected_k3s_config, expected_k3s_service = _k3s_contract_paths(config)
    expected_k3s_values = {
        "binary_sha256": str(config["kubernetes"]["binary_sha256"]),
        "config_sha256": sha256_file(expected_k3s_config),
        "service_sha256": sha256_file(expected_k3s_service),
    }
    if (
        not isinstance(k3s_status, dict)
        or status_payload.get("vm_ip") != k3s_status.get("vm_ip")
        or k3s_receipt.get("vm_ip") != k3s_status.get("vm_ip")
        or k3s_status.get("kubeconfig_sha256")
        != k3s_receipt.get("kubeconfig_sha256")
        or not isinstance(k3s_status.get("kubeconfig_sha256"), str)
        or re.fullmatch(
            r"[0-9a-f]{64}", k3s_status["kubeconfig_sha256"]
        )
        is None
        or k3s_status.get("kubeconfig_server")
        != f"https://{k3s_status.get('vm_ip')}:6443"
        or any(
            k3s_status.get(key) != value
            or k3s_receipt.get(key) != value
            for key, value in expected_k3s_values.items()
        )
        or k3s_status.get("service_active") is not True
        or k3s_status.get("service_substate") != "running"
        or k3s_status.get("unit_file_state") != "enabled"
        or k3s_status.get("fragment_path")
        != "/etc/systemd/system/k3s.service"
        or k3s_status.get("drop_ins_absent") is not True
        or not isinstance(k3s_status.get("main_pid"), int)
        or isinstance(k3s_status.get("main_pid"), bool)
        or k3s_status["main_pid"] <= 0
        or k3s_status.get("process_exe") != "/usr/local/bin/k3s"
        or k3s_status.get("process_argv")
        != ["/usr/local/bin/k3s", "server"]
        or k3s_status.get("environment_overrides_absent") is not True
    ):
        raise RuntimeErrorEB("status does not prove the live pinned k3s runtime")

    if (
        status_payload.get("node_ready") is not True
        or status_payload.get("kubelet_version")
        != str(config["kubernetes"]["version"])
    ):
        raise RuntimeErrorEB("status does not prove a Ready pinned k3s node")

    release_bootstrap_sha256 = payloads["release.json"].get("sha256")
    flux_controllers = status_payload.get("flux_controllers")
    flux_readback = status_payload.get("flux")
    if (
        not isinstance(release_bootstrap_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", release_bootstrap_sha256) is None
        or status_payload.get("flux_bootstrap_sha256")
        != release_bootstrap_sha256
        or not _flux_revision_matches_commit(
            status_payload.get("flux_source_revision"), source_commit
        )
        or not isinstance(flux_controllers, dict)
        or set(flux_controllers) != EXPECTED_FLUX_CONTROLLERS
        or any(
            not isinstance(value, dict)
            or value.get("available") is not True
            or value.get("desired_replicas") != 1
            or value.get("images_canonical") is not True
            or not isinstance(value.get("images"), dict)
            or value.get("images_sha256")
            != _stable_json_sha256(value["images"])
            for value in flux_controllers.values()
        )
        or not isinstance(flux_readback, dict)
        or set(flux_readback) != EXPECTED_FLUX_KUSTOMIZATIONS
        or any(
            not isinstance(value, dict)
            or value.get("ready") is not True
            or re.fullmatch(r"[0-9a-f]{64}", str(value.get("spec_sha256") or ""))
            is None
            for value in flux_readback.values()
        )
    ):
        raise RuntimeErrorEB("status does not prove the live Flux contract")
    expected_flux_controllers = _expected_flux_controller_contract(root)
    flux_baseline = payloads["platform.json"].get("flux_runtime_image_ids")
    if (
        not isinstance(flux_baseline, dict)
        or set(flux_baseline) != EXPECTED_FLUX_CONTROLLERS
        or status_payload.get("flux_runtime_image_ids_baseline")
        != flux_baseline
        or set(expected_flux_controllers) != EXPECTED_FLUX_CONTROLLERS
    ):
        raise RuntimeErrorEB(
            "status does not prove the installed Flux runtime image baseline"
        )
    for name, expected in expected_flux_controllers.items():
        observed = flux_controllers.get(name)
        if (
            not isinstance(observed, dict)
            or observed.get("images") != expected["images"]
            or observed.get("images_sha256")
            != _stable_json_sha256(expected["images"])
            or not isinstance(observed.get("pods"), dict)
            or observed["pods"].get("runtime_image_ids_sha256")
            != flux_baseline.get(name)
        ):
            raise RuntimeErrorEB(
                f"status does not prove the live Flux controller contract: {name}"
            )
        _require_stored_pod_image_contract(
            observed["pods"],
            1,
            expected["images"],
            f"Flux controller Pod {name}",
        )

    namespace_status = status_payload.get("namespace_security")
    expected_namespaces = _versioned_namespace_security_contract()
    if (
        not isinstance(namespace_status, dict)
        or set(namespace_status) != set(expected_namespaces)
    ):
        raise RuntimeErrorEB(
            "status does not prove the live Namespace security contract"
        )
    for name, expected in expected_namespaces.items():
        observed = namespace_status.get(name)
        if (
            not isinstance(observed, dict)
            or observed.get("canonical") is not True
            or observed.get("labels") != expected["labels"]
            or observed.get("labels_sha256") != expected["labels_sha256"]
        ):
            raise RuntimeErrorEB(
                f"status does not prove the live Namespace security contract: {name}"
            )

    data_deployment_status = status_payload.get("data_deployments")
    expected_data_deployments = {
        name: _versioned_data_deployment_contract(
            CLUSTER / f"data/{name}.yaml", name
        )
        for name in ("postgres", "nats")
    }
    if (
        not isinstance(data_deployment_status, dict)
        or set(data_deployment_status) != set(expected_data_deployments)
    ):
        raise RuntimeErrorEB("status does not prove the live data Deployment contract")
    for name, expected in expected_data_deployments.items():
        observed = data_deployment_status.get(name)
        if (
            not isinstance(observed, dict)
            or observed.get("available") is not True
            or observed.get("desired_replicas") != expected["replicas"]
            or observed.get("images_canonical") is not True
            or observed.get("images_sha256")
            != _stable_json_sha256(expected["images"])
            or observed.get("canonical") is not True
            or observed.get("contract_sha256")
            != expected["contract_sha256"]
            or observed.get("pod_contract_sha256")
            != expected["pod_contract_sha256"]
            or not isinstance(observed.get("pod_names"), list)
            or len(observed["pod_names"]) != expected["replicas"]
        ):
            raise RuntimeErrorEB(
                f"status does not prove the live data Deployment contract: {name}"
            )
        _require_stored_pod_image_contract(
            observed.get("pods"),
            expected["replicas"],
            expected["images"],
            f"data Pod {name}",
        )

    pvc_status = status_payload.get("pvcs")
    expected_pvcs = _rendered_pvc_contract(root)
    if (
        not isinstance(pvc_status, dict)
        or set(pvc_status) != set(expected_pvcs)
    ):
        raise RuntimeErrorEB(
            "status does not prove the complete PVC contract"
        )
    for key, expected in expected_pvcs.items():
        observed = pvc_status.get(key)
        if (
            not isinstance(observed, dict)
            or observed.get("phase") != "Bound"
            or observed.get("canonical") is not True
            or observed.get("spec_sha256") != expected["spec_sha256"]
        ):
            raise RuntimeErrorEB(
                f"status does not prove the complete PVC contract: {key}"
            )

    data_service_status = status_payload.get("data_services")
    expected_data_services = {
        name: _versioned_data_service_contract(
            CLUSTER / f"data/{name}.yaml", name
        )
        for name in ("postgres", "nats")
    }
    if (
        not isinstance(data_service_status, dict)
        or set(data_service_status) != set(expected_data_services)
    ):
        raise RuntimeErrorEB("status does not prove the live data Service contract")
    for name, expected in expected_data_services.items():
        observed = data_service_status.get(name)
        if (
            not isinstance(observed, dict)
            or observed.get("canonical") is not True
            or observed.get("spec") != expected["spec"]
            or observed.get("spec_sha256") != expected["spec_sha256"]
        ):
            raise RuntimeErrorEB(
                f"status does not prove the live data Service contract: {name}"
            )


    migration_status = status_payload.get("migration")
    expected_migration = _rendered_migration_job_contract(
        root, str(payloads["release.json"].get("api_digest", ""))
    )
    if (
        status_payload.get("migration_complete") is not True
        or not isinstance(migration_status, dict)
        or migration_status.get("canonical") is not True
        or migration_status.get("contract_sha256")
        != expected_migration["contract_sha256"]
        or migration_status.get("pod_contract_sha256")
        != expected_migration["pod_contract_sha256"]
        or migration_status.get("succeeded_pods")
        != expected_migration["contract"]["completions"]
        or not isinstance(migration_status.get("pod_names"), list)
        or len(migration_status["pod_names"])
        < expected_migration["contract"]["completions"]
    ):
        raise RuntimeErrorEB(
            "status does not prove the complete migration Job/Pod contract"
        )

    application_service_status = status_payload.get("application_services")
    expected_application_services = _rendered_application_service_contract(
        root, payloads["release.json"]
    )
    if (
        not isinstance(application_service_status, dict)
        or set(application_service_status) != set(expected_application_services)
    ):
        raise RuntimeErrorEB(
            "status does not prove the application Service contract"
        )
    for name, expected in expected_application_services.items():
        observed = application_service_status.get(name)
        if (
            not isinstance(observed, dict)
            or observed.get("canonical") is not True
            or observed.get("spec") != expected["spec"]
            or observed.get("spec_sha256") != expected["spec_sha256"]
        ):
            raise RuntimeErrorEB(
                f"status does not prove the application Service contract: {name}"
            )

    pod_status = status_payload.get("pods")
    api_digest = str(payloads["release.json"].get("api_digest", ""))
    web_digest = str(payloads["release.json"].get("web_digest", ""))
    expected_pod_contracts = {
        "weltgewebe-api": {
            "replicas": int(config["semantic_search"]["api_replicas"]),
            "images": {
                "api": f"ghcr.io/heimgewebe/commonthing-api@{api_digest}",
                "search-worker": f"ghcr.io/heimgewebe/commonthing-api@{api_digest}",
                "ollama": str(config["semantic_search"]["ollama_image"]),
            },
        },
        "weltgewebe-web": {
            "replicas": int(config["runtime_binding"]["web_replicas"]),
            "images": {
                "web": f"ghcr.io/heimgewebe/commonthing-web@{web_digest}",
            },
        },
    }
    if (
        not isinstance(pod_status, dict)
        or set(pod_status) != set(expected_pod_contracts)
    ):
        raise RuntimeErrorEB("status does not prove the live application Pod contract")
    for workload, expected in expected_pod_contracts.items():
        observed = pod_status.get(workload)
        expected_digests = {
            name: image.rsplit("@", 1)[1]
            for name, image in expected["images"].items()
        }
        if (
            not isinstance(observed, dict)
            or observed.get("expected_replicas") != expected["replicas"]
            or observed.get("observed_replicas") != expected["replicas"]
            or observed.get("requested_images_sha256")
            != _stable_json_sha256(expected["images"])
            or observed.get("images_canonical") is not True
            or not isinstance(observed.get("pods"), dict)
            or len(observed["pods"]) != expected["replicas"]
            or any(
                not isinstance(pod, dict)
                or pod.get("ready") is not True
                or pod.get("requested_images_sha256")
                != _stable_json_sha256(expected["images"])
                or not isinstance(pod.get("runtime_image_ids"), dict)
                or set(pod["runtime_image_ids"]) != set(expected_digests)
                or any(
                    not _runtime_image_id_matches_digest(
                        pod["runtime_image_ids"].get(container_name),
                        expected_digest,
                    )
                    for container_name, expected_digest in expected_digests.items()
                )
                for pod in observed["pods"].values()
            )
        ):
            raise RuntimeErrorEB(
                f"status does not prove the live application Pod contract: {workload}"
            )

    application_workload_status = status_payload.get(
        "application_workloads"
    )
    expected_application_workloads = _rendered_application_workload_contract(
        root, payloads["release.json"]
    )
    if (
        not isinstance(application_workload_status, dict)
        or set(application_workload_status)
        != set(expected_application_workloads)
    ):
        raise RuntimeErrorEB(
            "status does not prove the complete application workload contract"
        )
    for name, expected in expected_application_workloads.items():
        observed = application_workload_status.get(name)
        if (
            not isinstance(observed, dict)
            or observed.get("canonical") is not True
            or observed.get("contract_sha256")
            != expected["contract_sha256"]
            or observed.get("pod_contract_sha256")
            != expected["pod_contract_sha256"]
            or not isinstance(observed.get("pod_names"), list)
            or len(observed["pod_names"])
            != expected["contract"]["replicas"]
        ):
            raise RuntimeErrorEB(
                f"status does not prove the complete application workload contract: {name}"
            )

    service_account_status = status_payload.get(
        "application_service_accounts"
    )
    expected_service_accounts = (
        _rendered_application_service_account_contract(
            root, payloads["release.json"]
        )
    )
    if (
        not isinstance(service_account_status, dict)
        or set(service_account_status) != set(expected_service_accounts)
    ):
        raise RuntimeErrorEB(
            "status does not prove the application ServiceAccount contract"
        )
    for name, expected in expected_service_accounts.items():
        observed = service_account_status.get(name)
        if (
            not isinstance(observed, dict)
            or observed.get("canonical") is not True
            or observed.get("contract_sha256")
            != expected["contract_sha256"]
        ):
            raise RuntimeErrorEB(
                f"status does not prove the application ServiceAccount contract: {name}"
            )

    runtime_status = status_payload.get("runtime_contract")
    runtime_binding = config["runtime_binding"]
    data_network_specs = _versioned_network_policy_specs(
        CLUSTER / "data/network-policy.yaml", DATA_NAMESPACE
    )
    if (
        not isinstance(runtime_status, dict)
        or runtime_status.get("config_map_data_sha256")
        != _stable_json_sha256(runtime_binding["config_map_data"])
        or runtime_status.get("network_policy_specs_sha256")
        != _stable_json_sha256(runtime_binding["network_policy_specs"])
        or runtime_status.get("network_policy_names")
        != sorted(runtime_binding["network_policy_specs"])
        or runtime_status.get("data_network_policy_specs_sha256")
        != _stable_json_sha256(data_network_specs)
        or runtime_status.get("data_network_policy_names")
        != sorted(data_network_specs)
        or runtime_status.get("cilium_network_policy_specs_sha256")
        != _stable_json_sha256(runtime_binding["cilium_network_policy_specs"])
        or runtime_status.get("cilium_network_policy_names")
        != sorted(runtime_binding["cilium_network_policy_specs"])
        or runtime_status.get("temporary_model_egress_absent") is not True
        or runtime_status.get("policy_specs_canonical") is not True
    ):
        raise RuntimeErrorEB(
            "status does not prove the live runtime configuration contract"
        )

    if _current_protected_main_commit() != source_commit:
        raise RuntimeErrorEB(
            "portability release is no longer current protected main"
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
        status(root)
        result = {
            "schema_version": 1,
            "status": "observed",
            "receipt": "receipts/status.json",
        }
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
