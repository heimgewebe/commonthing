#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SEED_PATH = ROOT / "platform/oci-proof-mirror.seed.json"
OUTPUT_ROOT = (ROOT / "build/kubernetes-platform").resolve()
FULL_COMMIT = re.compile(r"[0-9a-f]{40}")
FULL_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
SAFE_NAME = re.compile(r"[a-z0-9_]+")
ALLOWED_SUITES = frozenset({"kind-gitops", "ha-recovery", "app-build"})
NOT_FOUND_MARKERS = (
    "manifest unknown",
    "not found",
    "name unknown",
    "404",
)

EXPECTED_IMAGES: dict[str, tuple[tuple[str, ...], bool, str]] = {
    "kind_node": (("kind-gitops", "ha-recovery"), False, "docker.io/kindest/node"),
    "cilium_agent": (("kind-gitops", "ha-recovery"), True, "quay.io/cilium/cilium"),
    "cilium_operator": (("kind-gitops", "ha-recovery"), True, "quay.io/cilium/operator-generic"),
    "cilium_envoy": (("kind-gitops", "ha-recovery"), True, "quay.io/cilium/cilium-envoy"),
    "hubble_relay": (("kind-gitops", "ha-recovery"), True, "quay.io/cilium/hubble-relay"),
    "flux_source_controller": (("kind-gitops", "ha-recovery"), True, "ghcr.io/fluxcd/source-controller"),
    "flux_kustomize_controller": (("kind-gitops", "ha-recovery"), True, "ghcr.io/fluxcd/kustomize-controller"),
    "flux_helm_controller": (("kind-gitops", "ha-recovery"), True, "ghcr.io/fluxcd/helm-controller"),
    "flux_notification_controller": (("kind-gitops", "ha-recovery"), True, "ghcr.io/fluxcd/notification-controller"),
    "local_postgres": (("kind-gitops",), True, "docker.io/library/postgres"),
    "local_nats": (("kind-gitops",), True, "docker.io/library/nats"),
    "cloudnative_pg_operator": (("ha-recovery",), True, "ghcr.io/cloudnative-pg/cloudnative-pg"),
    "cloudnative_pg_postgresql": (("ha-recovery",), True, "ghcr.io/cloudnative-pg/postgresql"),
    "ha_nats": (("ha-recovery",), True, "docker.io/library/nats"),
    "nats_box": (("ha-recovery",), True, "docker.io/natsio/nats-box"),
    "seaweedfs": (("ha-recovery",), True, "docker.io/chrislusf/seaweedfs"),
    "cert_manager_controller": (("ha-recovery",), True, "quay.io/jetstack/cert-manager-controller"),
    "cert_manager_cainjector": (("ha-recovery",), True, "quay.io/jetstack/cert-manager-cainjector"),
    "cert_manager_webhook": (("ha-recovery",), True, "quay.io/jetstack/cert-manager-webhook"),
    "barman_cloud_plugin": (("ha-recovery",), True, "ghcr.io/cloudnative-pg/plugin-barman-cloud"),
    "barman_cloud_sidecar": (("ha-recovery",), True, "ghcr.io/cloudnative-pg/plugin-barman-cloud-sidecar"),
    "build_rust": (("app-build",), False, "docker.io/library/rust"),
    "build_debian": (("app-build",), False, "docker.io/library/debian"),
    "build_node": (("app-build",), False, "docker.io/library/node"),
    "build_caddy": (("app-build",), False, "docker.io/library/caddy"),
}

EXPECTED_PUBLISHER_POLICY = {
    "canonical_inspect_attempts": 3,
    "mirror_publish_attempts": 3,
    "mirror_verify_attempts": 3,
    "retry_backoff_seconds": [5, 10],
    "operation_timeout_seconds": 900,
    "overall_deadline_seconds": 6300,
    "max_parallelism": 3,
    "require_identical_manifest_digest": True,
    "require_expected_head": True,
    "require_expected_seed_sha256": True,
    "require_protected_main": True,
}


class PublisherError(RuntimeError):
    error_class = "publisher_error"


class AvailabilityError(PublisherError):
    error_class = "registry_unavailable"


class IntegrityError(PublisherError):
    error_class = "integrity_mismatch"


class ManifestNotFoundError(PublisherError):
    error_class = "manifest_not_found"


class DeadlineExceededError(PublisherError):
    error_class = "deadline_exceeded"


class Deadline:
    def __init__(self, expires_at: float) -> None:
        self.expires_at = expires_at

    @classmethod
    def after(cls, seconds: int) -> "Deadline":
        return cls(time.monotonic() + float(seconds))

    def remaining(self) -> float:
        return self.expires_at - time.monotonic()

    def timeout(self, maximum: int) -> int:
        remaining = self.remaining()
        if remaining <= 1:
            raise DeadlineExceededError("publisher deadline exhausted")
        return max(1, min(maximum, int(remaining)))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_canonical_json(payload))
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _reference_repository(reference: str) -> str:
    without_digest = reference.rsplit("@", 1)[0]
    head, separator, tail = without_digest.rpartition("/")
    if not separator:
        raise PublisherError(f"source reference is not registry-qualified: {reference}")
    image = tail.split(":", 1)[0]
    return f"{head}/{image}"


def _load_seed() -> dict[str, Any]:
    try:
        seed = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PublisherError(f"mirror seed is unreadable: {error}") from error
    if seed.get("schema_version") != 1:
        raise PublisherError("unsupported mirror seed schema")
    if seed.get("owner") != "heimgewebe/weltgewebe":
        raise PublisherError("mirror seed owner mismatch")
    target = seed.get("target")
    publisher = seed.get("publisher")
    images = seed.get("images")
    if not isinstance(target, dict) or not isinstance(publisher, dict):
        raise PublisherError("mirror seed lacks target or publisher policy")
    expected_target = {
        "registry": "ghcr.io",
        "repository": "ghcr.io/heimgewebe/weltgewebe-proof-oci",
        "visibility": "private",
        "retention": {
            "state": "bootstrap_pending_lock",
            "future_lock_path": "platform/oci-proof-mirror.lock.json",
            "orphan_grace_days": 14,
        },
    }
    if target != expected_target:
        raise PublisherError("unexpected mirror target or bootstrap retention contract")
    if publisher != EXPECTED_PUBLISHER_POLICY:
        raise PublisherError("unexpected mirror publisher policy")
    if not isinstance(images, dict) or set(images) != set(EXPECTED_IMAGES):
        missing = sorted(set(EXPECTED_IMAGES) - set(images or {}))
        extra = sorted(set(images or {}) - set(EXPECTED_IMAGES))
        raise PublisherError(f"mirror inventory mismatch; missing={missing}, extra={extra}")
    for name, expected in EXPECTED_IMAGES.items():
        spec = images[name]
        if not SAFE_NAME.fullmatch(name) or not isinstance(spec, dict):
            raise PublisherError(f"unsafe mirror image entry: {name}")
        if set(spec) != {"canonical", "suites", "load_into_kind"}:
            raise PublisherError(f"mirror source {name} contains unexpected fields")
        canonical = spec["canonical"]
        suites = spec["suites"]
        load_into_kind = spec["load_into_kind"]
        if not isinstance(canonical, str) or "@sha256:" not in canonical:
            raise PublisherError(f"mirror source {name} is not digest-bound")
        if not FULL_DIGEST.fullmatch(canonical.rsplit("@", 1)[1]):
            raise PublisherError(f"mirror source {name} has malformed digest")
        expected_suites, expected_load, expected_repository = expected
        if (
            not isinstance(suites, list)
            or len(suites) != len(set(suites))
            or not set(suites).issubset(ALLOWED_SUITES)
            or tuple(suites) != expected_suites
        ):
            raise PublisherError(f"mirror source {name} has an invalid suite binding")
        if not isinstance(load_into_kind, bool) or load_into_kind is not expected_load:
            raise PublisherError(f"mirror source {name} has an invalid kind-load binding")
        repository = _reference_repository(canonical)
        if repository != expected_repository:
            raise PublisherError(
                f"mirror source {name} repository mismatch: {repository} != {expected_repository}"
            )
        if repository.startswith("ghcr.io/heimgewebe/"):
            raise PublisherError("publisher may not mirror Heimgewebe-owned application images")
        if repository == target["repository"]:
            raise PublisherError("mirror target may not be used as a canonical source")
    return seed


def _git_head() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    head = result.stdout.strip()
    if not FULL_COMMIT.fullmatch(head):
        raise PublisherError("checked-out head is not a full commit")
    return head


def _command_output(argv: list[str], *, timeout: int) -> str:
    print("+", " ".join(argv), file=sys.stderr, flush=True)
    result = subprocess.run(
        argv,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "command failed").strip()
        lowered = details.lower()
        if any(marker in lowered for marker in NOT_FOUND_MARKERS):
            raise ManifestNotFoundError(details)
        raise AvailabilityError(details)
    return result.stdout.strip()


def _manifest_digest_once(reference: str, *, timeout: int) -> str:
    raw = _command_output(
        [
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            reference,
            "--format",
            "{{json .Manifest.Digest}}",
        ],
        timeout=timeout,
    )
    try:
        digest = json.loads(raw)
    except json.JSONDecodeError as error:
        raise IntegrityError(f"Docker returned malformed manifest digest for {reference}") from error
    if not isinstance(digest, str) or not FULL_DIGEST.fullmatch(digest):
        raise IntegrityError(f"Docker returned no full manifest digest for {reference}")
    return digest


def _sleep_backoff(seconds: int, deadline: Deadline) -> None:
    if deadline.remaining() <= seconds + 1:
        raise DeadlineExceededError("publisher deadline cannot accommodate retry backoff")
    time.sleep(float(seconds))


def _manifest_digest(
    reference: str,
    *,
    attempts: int,
    backoff_seconds: list[int],
    operation_timeout: int,
    deadline: Deadline,
    allow_missing: bool = False,
) -> str | None:
    failures: list[str] = []
    for attempt in range(attempts):
        try:
            return _manifest_digest_once(
                reference,
                timeout=deadline.timeout(operation_timeout),
            )
        except ManifestNotFoundError:
            if allow_missing:
                return None
            raise
        except IntegrityError:
            raise
        except (AvailabilityError, subprocess.TimeoutExpired) as error:
            failures.append(f"attempt {attempt + 1}/{attempts}: {error}")
            if attempt + 1 < attempts:
                _sleep_backoff(backoff_seconds[attempt], deadline)
    raise AvailabilityError(
        f"manifest inspection failed for {reference} after bounded retries: "
        + " | ".join(failures)
    )


def _create_mirror_once(
    target_tag: str,
    canonical: str,
    *,
    timeout: int,
) -> None:
    argv = [
        "docker",
        "buildx",
        "imagetools",
        "create",
        "--prefer-index=false",
        "--tag",
        target_tag,
        canonical,
    ]
    print("+", " ".join(argv), file=sys.stderr, flush=True)
    result = subprocess.run(argv, cwd=ROOT, check=False, timeout=timeout)
    if result.returncode != 0:
        raise AvailabilityError(f"mirror copy exited with status {result.returncode}")


def _create_mirror(
    target_tag: str,
    canonical: str,
    *,
    attempts: int,
    backoff_seconds: list[int],
    operation_timeout: int,
    deadline: Deadline,
) -> None:
    failures: list[str] = []
    for attempt in range(attempts):
        try:
            _create_mirror_once(
                target_tag,
                canonical,
                timeout=deadline.timeout(operation_timeout),
            )
            return
        except (AvailabilityError, subprocess.TimeoutExpired) as error:
            failures.append(f"attempt {attempt + 1}/{attempts}: {error}")
            if attempt + 1 < attempts:
                _sleep_backoff(backoff_seconds[attempt], deadline)
    raise AvailabilityError(
        f"mirror publication failed for {target_tag} after bounded retries: "
        + " | ".join(failures)
    )


def _previously_verified_images() -> dict[str, Any]:
    """Reuse only entries from the reviewed, ancestry-validated active lock."""
    if __name__ == "__main__":
        import oci_proof_mirror
    else:
        from scripts.platform import oci_proof_mirror

    if not oci_proof_mirror.LOCK_PATH.exists():
        # First publication has no prior generation to reuse.
        return {}
    lock = oci_proof_mirror._load_lock()
    committed = json.loads(
        oci_proof_mirror._git_bytes(
            ["show", "HEAD:platform/oci-proof-mirror.lock.json"]
        )
    )
    if lock != committed:
        raise IntegrityError("OCI mirror lock differs from reviewed HEAD")
    return lock["images"]


def _publish_one(
    target_repository: str,
    name: str,
    canonical: str,
    policy: dict[str, Any],
    deadline: Deadline,
    previously_verified: bool = False,
) -> dict[str, Any]:
    canonical_digest = canonical.rsplit("@", 1)[1]
    target_tag = f"{target_repository}:{name}-{canonical_digest.removeprefix('sha256:')}"
    existing = _manifest_digest(
        target_tag,
        attempts=policy["mirror_verify_attempts"],
        backoff_seconds=policy["retry_backoff_seconds"],
        operation_timeout=policy["operation_timeout_seconds"],
        deadline=deadline,
        allow_missing=True,
    )
    if existing is not None and existing != canonical_digest:
        raise IntegrityError(
            f"existing mirror tag digest mismatch for {name}: "
            f"{existing} != {canonical_digest}"
        )
    # A prior verified source binding plus a fresh identical target digest
    # is sufficient for an unchanged image. New or missing images still
    # require a fresh source check, including on a partially completed run.
    if existing is None or not previously_verified:
        observed_canonical = _manifest_digest(
            canonical,
            attempts=policy["canonical_inspect_attempts"],
            backoff_seconds=policy["retry_backoff_seconds"],
            operation_timeout=policy["operation_timeout_seconds"],
            deadline=deadline,
        )
        if observed_canonical != canonical_digest:
            raise IntegrityError(
                f"canonical manifest digest mismatch for {name}: "
                f"{observed_canonical} != {canonical_digest}"
            )
    if existing is not None:
        return {
            "action": "reused",
            "canonical": canonical,
            "canonical_digest": canonical_digest,
            "mirror_tag": target_tag,
            "mirror": f"{target_repository}@{existing}",
            "mirror_digest": existing,
        }
    _create_mirror(
        target_tag,
        canonical,
        attempts=policy["mirror_publish_attempts"],
        backoff_seconds=policy["retry_backoff_seconds"],
        operation_timeout=policy["operation_timeout_seconds"],
        deadline=deadline,
    )
    mirror_digest = _manifest_digest(
        target_tag,
        attempts=policy["mirror_verify_attempts"],
        backoff_seconds=policy["retry_backoff_seconds"],
        operation_timeout=policy["operation_timeout_seconds"],
        deadline=deadline,
    )
    if mirror_digest != canonical_digest:
        raise IntegrityError(
            f"mirror digest mismatch for {name}: {mirror_digest} != {canonical_digest}"
        )
    return {
        "action": "published",
        "canonical": canonical,
        "canonical_digest": canonical_digest,
        "mirror_tag": target_tag,
        "mirror": f"{target_repository}@{mirror_digest}",
        "mirror_digest": mirror_digest,
    }


def _tool_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    commands = {
        "docker": ["docker", "version", "--format", "{{.Client.Version}}"],
        "buildx": ["docker", "buildx", "version"],
    }
    for name, argv in commands.items():
        result = subprocess.run(
            argv,
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        versions[name] = result.stdout.strip()
    return versions


def _validated_output_path(output: Path) -> Path:
    resolved = output.resolve()
    if not resolved.is_relative_to(OUTPUT_ROOT):
        raise PublisherError(f"provenance output must remain below {OUTPUT_ROOT}")
    return resolved


def publish(expected_head: str, expected_seed_sha256: str, output: Path) -> dict[str, Any]:
    if not FULL_COMMIT.fullmatch(expected_head):
        raise PublisherError("expected head must be a full lowercase commit")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_seed_sha256):
        raise PublisherError("expected seed SHA-256 must contain 64 lowercase hex characters")
    output = _validated_output_path(output)
    observed_head = _git_head()
    observed_seed = _sha256(SEED_PATH)
    if observed_head != expected_head:
        raise PublisherError(f"head mismatch: {observed_head} != {expected_head}")
    if observed_seed != expected_seed_sha256:
        raise PublisherError(f"seed mismatch: {observed_seed} != {expected_seed_sha256}")
    seed = _load_seed()
    previous_images = _previously_verified_images()
    policy = seed["publisher"]
    deadline = Deadline.after(policy["overall_deadline_seconds"])
    payload: dict[str, Any] = {
        "schema_version": 2,
        "status": "running",
        "owner": seed["owner"],
        "source_head": observed_head,
        "seed_sha256": observed_seed,
        "target": seed["target"],
        "started_at": _utc_now(),
        "workflow": {
            "repository": os.environ.get("GITHUB_REPOSITORY"),
            "run_id": os.environ.get("GITHUB_RUN_ID"),
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
            "actor": os.environ.get("GITHUB_ACTOR"),
            "event_name": os.environ.get("GITHUB_EVENT_NAME"),
        },
        "tool_versions": {},
        "image_count": len(seed["images"]),
        "completed_count": 0,
        "images": {},
        "failures": {},
    }
    _atomic_write(output, payload)
    try:
        payload["tool_versions"] = _tool_versions()
        _atomic_write(output, payload)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        payload["status"] = "failed"
        payload["completed_at"] = _utc_now()
        payload["failures"]["publisher_toolchain"] = {
            "error_class": "toolchain_unavailable",
            "message": str(error),
        }
        _atomic_write(output, payload)
        raise PublisherError("publisher toolchain inspection failed") from error
    ordered = list(seed["images"].items())
    max_parallelism = policy["max_parallelism"]
    for batch_start in range(0, len(ordered), max_parallelism):
        batch = ordered[batch_start : batch_start + max_parallelism]
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(batch)) as executor:
            futures = {}
            for offset, (name, spec) in enumerate(batch, start=batch_start + 1):
                print(f"Publishing {offset}/{len(ordered)}: {name}", file=sys.stderr, flush=True)
                future = executor.submit(
                    _publish_one,
                    seed["target"]["repository"],
                    name,
                    spec["canonical"],
                    policy,
                    deadline,
                    previous_images.get(name, {}).get("canonical") == spec["canonical"],
                )
                futures[future] = (name, spec)
            for future in concurrent.futures.as_completed(futures):
                name, spec = futures[future]
                try:
                    result = future.result()
                    result["suites"] = spec["suites"]
                    result["load_into_kind"] = spec["load_into_kind"]
                    payload["images"][name] = result
                    payload["completed_count"] = len(payload["images"])
                except Exception as error:  # captured into durable partial provenance
                    error_class = getattr(error, "error_class", type(error).__name__)
                    payload["failures"][name] = {
                        "error_class": error_class,
                        "message": str(error),
                    }
        _atomic_write(output, payload)
        if payload["failures"]:
            payload["status"] = "partial" if payload["images"] else "failed"
            payload["completed_at"] = _utc_now()
            _atomic_write(output, payload)
            failed = ", ".join(sorted(payload["failures"]))
            raise PublisherError(f"mirror publication stopped after failed batch: {failed}")
    payload["status"] = "pass"
    payload["completed_at"] = _utc_now()
    _atomic_write(output, payload)
    return payload


def validate() -> dict[str, Any]:
    seed = _load_seed()
    return {
        "schema_version": 2,
        "status": "pass",
        "owner": seed["owner"],
        "target_repository": seed["target"]["repository"],
        "visibility": seed["target"]["visibility"],
        "retention_state": seed["target"]["retention"]["state"],
        "inventory": sorted(seed["images"]),
        "image_count": len(seed["images"]),
        "seed_sha256": _sha256(SEED_PATH),
        "publisher_policy": seed["publisher"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate")
    publish_parser = sub.add_parser("publish")
    publish_parser.add_argument("--expected-head", required=True)
    publish_parser.add_argument("--expected-seed-sha256", required=True)
    publish_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "validate":
            result = validate()
        else:
            result = publish(
                args.expected_head,
                args.expected_seed_sha256,
                args.output,
            )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (
        PublisherError,
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as error:
        print(f"OCI mirror publisher failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
