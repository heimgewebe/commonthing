#!/usr/bin/env python3
"""Effect-light helpers for T085 Experiment B.

This module deliberately does not create VMs, install Kubernetes, or mutate a
cluster.  It validates the versioned contract and renders bounded bootstrap
artifacts into the external Experiment-B state directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "platform/clusters/experiment-b/config.json"
BOOTSTRAP_TEMPLATE = ROOT / "platform/clusters/experiment-b/bootstrap-template.yaml"
DEFAULT_STATE_ROOT = Path.home() / ".local/state/commonthing/experiment-b"

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
VM_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
TOKEN_RE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")

REQUIRED_K3S_FLAGS = {
    "--flannel-backend=none",
    "--disable-network-policy",
    "--disable-kube-proxy",
    "--disable=traefik",
    "--disable=servicelb",
    "--write-kubeconfig-mode=0600",
}


class ContractError(RuntimeError):
    pass


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def validate_config(config: dict[str, Any]) -> None:
    if config.get("schema_version") != 1:
        raise ContractError("unsupported Experiment-B config schema")
    if config.get("source_policy") != "exact-current-protected-main":
        raise ContractError("source policy must remain exact-current-protected-main")
    if config.get("production_activation") is not False:
        raise ContractError("Experiment B must never activate production")
    if config.get("test_data_only") is not True:
        raise ContractError("Experiment B must use test data only")

    vm = config.get("vm", {})
    if not VM_NAME_RE.fullmatch(str(vm.get("name", ""))):
        raise ContractError("invalid VM name")
    if not (1 <= int(vm.get("vcpu", 0)) <= 8):
        raise ContractError("VM vCPU budget must stay between 1 and 8")
    if not (4096 <= int(vm.get("memory_mib", 0)) <= 12288):
        raise ContractError("VM memory budget must stay between 4 and 12 GiB")
    if not (30 <= int(vm.get("disk_gib", 0)) <= 80):
        raise ContractError("VM disk budget must stay between 30 and 80 GiB")
    if vm.get("network_mode") != "nat-only" or vm.get("network") != "default":
        raise ContractError("Experiment-B VM must remain on libvirt default NAT")
    if vm.get("host_mounts") != []:
        raise ContractError("host mounts are forbidden")
    image = vm.get("image", {})
    if not str(image.get("url", "")).startswith("https://"):
        raise ContractError("OS image must use HTTPS")
    if "/current/" in str(image.get("url", "")) or "/release/" in str(image.get("url", "")):
        raise ContractError("OS image URL must use a fixed release directory")
    if not SHA256_RE.fullmatch(str(image.get("sha256", ""))):
        raise ContractError("OS image SHA-256 is invalid")

    kubernetes = config.get("kubernetes", {})
    if kubernetes.get("distribution") != "k3s":
        raise ContractError("Experiment B currently selects k3s")
    if not str(kubernetes.get("binary_url", "")).startswith("https://"):
        raise ContractError("k3s binary must use HTTPS")
    if not SHA256_RE.fullmatch(str(kubernetes.get("binary_sha256", ""))):
        raise ContractError("k3s binary SHA-256 is invalid")
    if not SHA256_RE.fullmatch(str(kubernetes.get("reexec_binary_sha256", ""))):
        raise ContractError("k3s reexec binary SHA-256 is invalid")
    flags = set(kubernetes.get("server_flags", []))
    if not REQUIRED_K3S_FLAGS.issubset(flags):
        raise ContractError("required k3s/Cilium isolation flags are missing")

    cilium = config.get("cilium", {})
    if cilium.get("gateway_api") is not True:
        raise ContractError("Cilium Gateway API must remain enabled")
    if cilium.get("kube_proxy_replacement") is not True:
        raise ContractError("Cilium kube-proxy replacement must remain enabled")
    if not SHA256_RE.fullmatch(str(cilium.get("chart_sha256", ""))):
        raise ContractError("Cilium chart SHA-256 is invalid")

    external_secrets = config.get("external_secrets", {})
    expected_secret_names = {
        "database_secret": "commonthing-experiment-b-database",
        "runtime_secret": "weltgewebe-runtime",
        "registry_secret": "commonthing-experiment-b-registry",
    }
    if any(
        external_secrets.get(key) != value
        for key, value in expected_secret_names.items()
    ):
        raise ContractError("Experiment-B external secret names drifted")
    if external_secrets.get("registry_required") is not True:
        raise ContractError("Experiment-B GHCR pull secret must remain required")

    semantic = config.get("semantic_search", {})
    expected_semantic = {
        "topology": "api-pod-sidecars",
        "api_replicas": 1,
        "provider": "local:ollama",
        "ollama_url": "http://127.0.0.1:11434/",
        "ollama_image": "ollama/ollama:0.12.6@sha256:352e045b937ac29d3d9550c22fb85525f60a89e064df34c26579bee5a93b3a16",
        "model_id": "qwen3-embedding:4b",
        "model_revision": "sha256:df5bd2e3c74cd8d069d21dc038f1b359fcdc9458fce1c99bd43c9eb1518ff907",
        "runtime_identity": "ollama:0.12.6@http://127.0.0.1:11434",
        "dimension": 2560,
        "generation_id": "search-gen-2e8358273aa6d41e6a59025985a99738614aba725b8f369b3a54f390f8752e5c",
        "model_storage_class": "local-path",
        "model_storage_gib": 10,
    }
    if any(semantic.get(key) != value for key, value in expected_semantic.items()):
        raise ContractError("Experiment-B semantic-search contract drifted")
    if semantic.get("contract") != "preserve-literal-loopback; no clusterwide provider service":
        raise ContractError("Experiment-B semantic-search boundary drifted")
    if int(vm.get("memory_mib", 0)) != 12288:
        raise ContractError("semantic-search Experiment B requires the pinned 12 GiB VM")

    forbidden = config.get("forbidden", {})
    required_forbidden = {
        "kind",
        "staging_cell_runtime_controller",
        "production_dns",
        "production_traffic",
        "production_data",
        "production_writer_authority",
        "bridged_network",
    }
    if any(forbidden.get(key) is not True for key in required_forbidden):
        raise ContractError("all Experiment-B production/isolation prohibitions must stay enabled")

    runtime_binding = config.get("runtime_binding", {})
    expected_runtime_paths = {
        "template": "platform/clusters/experiment-b/bootstrap-template.yaml",
        "k3s_config": "platform/clusters/experiment-b/k3s-config.yaml",
        "k3s_service": "platform/clusters/experiment-b/k3s.service",
    }
    if any(
        runtime_binding.get(key) != value
        for key, value in expected_runtime_paths.items()
    ):
        raise ContractError("Experiment-B runtime binding paths drifted")
    if runtime_binding.get("web_replicas") != 2:
        raise ContractError("Experiment-B Web replica contract drifted")
    if runtime_binding.get("api_resource_limits") != {
        "cpu": "1",
        "memory": "512Mi",
    }:
        raise ContractError("Experiment-B API resource-limit contract drifted")
    runtime_config = runtime_binding.get("config_map_data")
    if (
        not isinstance(runtime_config, dict)
        or not runtime_config
        or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in runtime_config.items()
        )
    ):
        raise ContractError("Experiment-B runtime ConfigMap contract is invalid")
    expected_runtime_config = {
        "APP_BASE_URL": "http://commonthing-experiment-b.invalid",
        "AUTH_COOKIE_SECURE": "0",
        "AUTH_PUBLIC_LOGIN": "0",
        "NATS_URL": "nats://nats.commonthing-data.svc.cluster.local:4222",
        "WELTGEWEBE_SEARCH_GENERATION_ID": semantic["generation_id"],
        "WELTGEWEBE_SEARCH_BACKFILL_MAX_JOBS": "100",
        "WELTGEWEBE_SEARCH_OLLAMA_URL": semantic["ollama_url"],
        "WELTGEWEBE_SEARCH_PROVIDER": semantic["provider"],
        "WELTGEWEBE_SEARCH_MODEL_ID": semantic["model_id"],
        "WELTGEWEBE_SEARCH_MODEL_REVISION": semantic["model_revision"],
        "WELTGEWEBE_SEARCH_RUNTIME_IDENTITY": semantic["runtime_identity"],
        "WELTGEWEBE_SEARCH_DIMENSION": str(semantic["dimension"]),
        "WELTGEWEBE_SEARCH_WORKER_INTERVAL_SECONDS": "15",
    }
    if any(
        runtime_config.get(key) != value
        for key, value in expected_runtime_config.items()
    ):
        raise ContractError("Experiment-B runtime ConfigMap values drifted")
    network_policy_specs = runtime_binding.get("network_policy_specs")
    if (
        not isinstance(network_policy_specs, dict)
        or set(network_policy_specs)
        != {
            "allow-api-data-egress",
            "allow-dns",
            "allow-migration-postgres-egress",
            "allow-same-namespace",
            "default-deny",
        }
        or any(not isinstance(spec, dict) for spec in network_policy_specs.values())
    ):
        raise ContractError("Experiment-B NetworkPolicy contract drifted")
    cilium_policy_specs = runtime_binding.get("cilium_network_policy_specs")
    if (
        not isinstance(cilium_policy_specs, dict)
        or set(cilium_policy_specs) != {"allow-cilium-gateway"}
        or any(not isinstance(spec, dict) for spec in cilium_policy_specs.values())
    ):
        raise ContractError("Experiment-B CiliumNetworkPolicy contract drifted")
    for value in expected_runtime_paths.values():
        if not (ROOT / value).is_file():
            raise ContractError(f"Experiment-B runtime binding is missing: {value}")


def state_root(value: str | None) -> Path:
    root = (Path(value).expanduser() if value else DEFAULT_STATE_ROOT).resolve()
    allowed_root = DEFAULT_STATE_ROOT.resolve()
    if root != allowed_root:
        try:
            root.relative_to(allowed_root)
        except ValueError as exc:
            raise ContractError(
                f"state root must be {allowed_root} or one of its descendants"
            ) from exc
    return root


def require_state_path(root: Path, path: Path) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ContractError(f"output path must stay below {root}") from exc
    return resolved


def _open_output_directory_nofollow(path: Path, context: str) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    cloexec = getattr(os, "O_CLOEXEC", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if not all(isinstance(flag, int) for flag in (nofollow, cloexec, directory)):
        raise ContractError(f"{context} is unsafe")
    flags = os.O_RDONLY | nofollow | cloexec | directory
    absolute = Path(os.path.abspath(path))
    try:
        directory_fd = os.open("/", flags)
    except OSError as exc:
        raise ContractError(f"{context} is unsafe") from exc
    try:
        for component in absolute.parts[1:]:
            try:
                next_fd = os.open(component, flags, dir_fd=directory_fd)
            except FileNotFoundError:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=directory_fd)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise ContractError(f"{context} is unsafe") from exc
                try:
                    next_fd = os.open(component, flags, dir_fd=directory_fd)
                except OSError as exc:
                    raise ContractError(f"{context} is unsafe") from exc
            except OSError as exc:
                raise ContractError(f"{context} is unsafe") from exc
            os.close(directory_fd)
            directory_fd = next_fd
        if not stat.S_ISDIR(os.fstat(directory_fd).st_mode):
            raise ContractError(f"{context} is unsafe")
        result = directory_fd
        directory_fd = -1
        return result
    finally:
        if directory_fd >= 0:
            os.close(directory_fd)


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    if (
        not isinstance(mode, int)
        or isinstance(mode, bool)
        or mode < 0
        or mode > 0o777
    ):
        raise ContractError("state output mode is invalid")
    if not path.name or path.name in {".", ".."}:
        raise ContractError("state output name is invalid")

    directory_fd = _open_output_directory_nofollow(
        path.parent,
        "state output parent",
    )
    temporary_name = f".{path.name}.{secrets.token_hex(12)}.tmp"
    temporary_fd: int | None = None
    try:
        nofollow = getattr(os, "O_NOFOLLOW", None)
        cloexec = getattr(os, "O_CLOEXEC", None)
        if not isinstance(nofollow, int) or not isinstance(cloexec, int):
            raise ContractError("state output cannot be written safely")
        try:
            temporary_fd = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow | cloexec,
                mode,
                dir_fd=directory_fd,
            )
        except OSError as exc:
            raise ContractError("state output cannot be created safely") from exc
        metadata = os.fstat(temporary_fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
        ):
            raise ContractError("state output identity is unsafe")
        os.fchmod(temporary_fd, mode)
        view = memoryview(data)
        offset = 0
        while offset < len(view):
            written = os.write(temporary_fd, view[offset:])
            if written <= 0:
                raise ContractError("state output write failed")
            offset += written
        os.fsync(temporary_fd)
        os.close(temporary_fd)
        temporary_fd = None
        try:
            os.replace(
                temporary_name,
                path.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
        except OSError as exc:
            raise ContractError("state output replacement failed") from exc
    finally:
        if temporary_fd is not None:
            os.close(temporary_fd)
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        finally:
            os.close(directory_fd)


def render_bootstrap_from_template(
    source_commit: str,
    api_digest: str,
    web_digest: str,
    output: Path,
    template_bytes: bytes,
) -> dict[str, str]:
    if not COMMIT_RE.fullmatch(source_commit):
        raise ContractError("source commit must be exactly 40 lowercase hex characters")
    if not DIGEST_RE.fullmatch(api_digest):
        raise ContractError("API digest must be sha256:<64 lowercase hex>")
    if not DIGEST_RE.fullmatch(web_digest):
        raise ContractError("Web digest must be sha256:<64 lowercase hex>")

    try:
        template = template_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractError("bootstrap template must be UTF-8") from exc
    expected_tokens = {"SOURCE_COMMIT", "API_DIGEST", "WEB_DIGEST"}
    if set(TOKEN_RE.findall(template)) != expected_tokens:
        raise ContractError("bootstrap template token set drifted")
    rendered = template
    for key, value in {
        "SOURCE_COMMIT": source_commit,
        "API_DIGEST": api_digest,
        "WEB_DIGEST": web_digest,
    }.items():
        rendered = rendered.replace(f"${{{key}}}", value)
    if TOKEN_RE.search(rendered):
        raise ContractError("unresolved bootstrap token")

    data = rendered.encode("utf-8")
    atomic_write(output, data)
    return {
        "output": str(output),
        "sha256": sha256_bytes(data),
        "source_commit": source_commit,
        "api_digest": api_digest,
        "web_digest": web_digest,
    }


def render_bootstrap(
    source_commit: str,
    api_digest: str,
    web_digest: str,
    output: Path,
) -> dict[str, str]:
    return render_bootstrap_from_template(
        source_commit,
        api_digest,
        web_digest,
        output,
        BOOTSTRAP_TEMPLATE.read_bytes(),
    )


def render_cloud_init(public_key_file: Path, output_dir: Path, hostname: str) -> dict[str, str]:
    public_key = public_key_file.read_text(encoding="utf-8").strip()
    if "\n" in public_key or "\r" in public_key:
        raise ContractError("SSH public key must be exactly one line")
    if not public_key.startswith(("ssh-ed25519 ", "ssh-rsa ", "ecdsa-sha2-nistp256 ")):
        raise ContractError("unsupported SSH public key")

    user_data = f"""#cloud-config
hostname: {hostname}
manage_etc_hosts: true
ssh_pwauth: false
disable_root: true
package_update: false
users:
  - name: commonthing
    groups: [adm, sudo]
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    ssh_authorized_keys:
      - {public_key}
"""
    meta_data = f"""instance-id: {hostname}
local-hostname: {hostname}
"""
    user_path = output_dir / "user-data.yaml"
    meta_path = output_dir / "meta-data.yaml"
    atomic_write(user_path, user_data.encode("utf-8"))
    atomic_write(meta_path, meta_data.encode("utf-8"))
    return {
        "user_data": str(user_path),
        "user_data_sha256": sha256_file(user_path),
        "meta_data": str(meta_path),
        "meta_data_sha256": sha256_file(meta_path),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-root")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate")

    bootstrap = sub.add_parser("render-bootstrap")
    bootstrap.add_argument("--source-commit", required=True)
    bootstrap.add_argument("--api-digest", required=True)
    bootstrap.add_argument("--web-digest", required=True)
    bootstrap.add_argument("--output", default="bootstrap.yaml")

    cloud = sub.add_parser("render-cloud-init")
    cloud.add_argument("--public-key-file", required=True)
    cloud.add_argument("--output-dir", default="cloud-init")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = load_config()
    validate_config(config)
    root = state_root(args.state_root)

    if args.command == "validate":
        print(json.dumps({
            "status": "ok",
            "config_sha256": sha256_file(CONFIG_PATH),
            "bootstrap_template_sha256": sha256_file(BOOTSTRAP_TEMPLATE),
        }, sort_keys=True))
        return 0

    if args.command == "render-bootstrap":
        output = require_state_path(root, root / args.output)
        result = render_bootstrap(
            args.source_commit,
            args.api_digest,
            args.web_digest,
            output,
        )
        print(json.dumps(result, sort_keys=True))
        return 0

    if args.command == "render-cloud-init":
        output_dir = require_state_path(root, root / args.output_dir)
        result = render_cloud_init(
            Path(args.public_key_file),
            output_dir,
            str(config["vm"]["name"]),
        )
        print(json.dumps(result, sort_keys=True))
        return 0

    raise ContractError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ContractError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, sort_keys=True))
        raise SystemExit(2)