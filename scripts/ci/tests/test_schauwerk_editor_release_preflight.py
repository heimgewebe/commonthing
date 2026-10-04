from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[2] / "preflight" / "schauwerk_editor_release.py"
SPEC = importlib.util.spec_from_file_location("schauwerk_editor_release", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

SOURCE_COMMIT = "a" * 40
IMAGE_DIGEST = "sha256:" + "b" * 64


def _payload() -> dict[str, str]:
    return {
        "schema_version": MODULE.LOCK_SCHEMA,
        "source_repository": MODULE.SOURCE_REPOSITORY,
        "source_commit": SOURCE_COMMIT,
        "image_repository": MODULE.IMAGE_REPOSITORY,
        "image_digest": IMAGE_DIGEST,
        "public_base_path": MODULE.PUBLIC_BASE_PATH,
    }


def _write_lock(tmp_path: Path, payload: dict[str, str] | None = None) -> Path:
    lock = tmp_path / "release-lock.json"
    lock.write_text(json.dumps(payload or _payload(), indent=2) + "\n", encoding="utf-8")
    return lock


def test_valid_runtime_lock_passes(tmp_path: Path) -> None:
    result = MODULE.verify_runtime_lock(_write_lock(tmp_path))
    assert result == {
        "source_repository": MODULE.SOURCE_REPOSITORY,
        "source_commit": SOURCE_COMMIT,
        "image_repository": MODULE.IMAGE_REPOSITORY,
        "image_digest": IMAGE_DIGEST,
        "image_ref": f"{MODULE.IMAGE_REPOSITORY}@{IMAGE_DIGEST}",
        "public_base_path": "/schaubild",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", "wrong"),
        ("source_repository", "other/repo"),
        ("source_commit", "a" * 39),
        ("image_repository", "ghcr.io/heimgewebe/other"),
        ("image_digest", "sha256:" + "g" * 64),
        ("public_base_path", "/other"),
    ],
)
def test_runtime_lock_identity_drift_fails_closed(
    tmp_path: Path, field: str, value: str
) -> None:
    payload = _payload()
    payload[field] = value
    with pytest.raises(MODULE.ReleaseContractError):
        MODULE.verify_runtime_lock(_write_lock(tmp_path, payload))


def test_runtime_lock_rejects_extra_fields_and_symlink(tmp_path: Path) -> None:
    payload = _payload()
    payload["tag"] = "latest"
    lock = _write_lock(tmp_path, payload)
    with pytest.raises(MODULE.ReleaseContractError, match="shape"):
        MODULE.verify_runtime_lock(lock)
    lock.unlink()
    target = tmp_path / "real.json"
    target.write_text(json.dumps(_payload()), encoding="utf-8")
    lock.symlink_to(target)
    with pytest.raises(MODULE.ReleaseContractError, match="missing or unsafe"):
        MODULE.verify_runtime_lock(lock)


def test_vps_deploy_binds_runtime_lock_before_first_compose_render() -> None:
    repo = Path(__file__).resolve().parents[3]
    deploy = (repo / "scripts" / "weltgewebe-up").read_text(encoding="utf-8")
    lock_path = deploy.index("infra/schauwerk-editor/release-lock.json")
    helper = deploy.index("scripts/preflight/schauwerk_editor_release.py", lock_path)
    proxy_cidr = deploy.index(
        'SCHAUWERK_RUNTIME_TRUSTED_PROXY_CIDR="${SCHAUWERK_SCHAUBILD_TRUSTED_PROXY_CIDR:-172.16.0.0/12}"',
        helper,
    )
    proxy_export = deploy.index(
        'export SCHAUWERK_SCHAUBILD_TRUSTED_PROXY_CIDR="$SCHAUWERK_RUNTIME_TRUSTED_PROXY_CIDR"',
        proxy_cidr,
    )
    export = deploy.index('export SCHAUWERK_SCHAUBILD_IMAGE="$SCHAUWERK_RUNTIME_IMAGE_REF"', proxy_export)
    compose = deploy.index('docker compose "${BASE_ARGS[@]}" config > /dev/null', export)
    service_check = deploy.index('services.get("schaubild"', compose)
    deploying = deploy.index('echo ">> Deploying..."', service_check)
    assert lock_path < helper < proxy_cidr < proxy_export < export < compose < service_check < deploying
    assert "SCHAUWERK_RUNTIME_IMAGE_REF" in deploy
    assert "SCHAUWERK_RUNTIME_SOURCE_COMMIT" in deploy
    assert '"--trusted-proxy-source-cidr"' in deploy
    assert "expected_proxy_cidr = sys.argv[2]" in deploy
    assert '"$SCHAUWERK_RUNTIME_IMAGE_REF" "$SCHAUWERK_RUNTIME_TRUSTED_PROXY_CIDR"' in deploy


def test_full_vps_deploy_reads_back_native_runtime_before_state_commit() -> None:
    repo = Path(__file__).resolve().parents[3]
    deploy = (repo / "scripts" / "weltgewebe-up").read_text(encoding="utf-8")
    deploying = deploy.index('echo ">> Deploying..."')
    postflight = deploy.index(
        'if [[ "$DEPLOY_SCOPE" == "full" && "$SCHAUWERK_RUNTIME_POSTFLIGHT_REQUIRED" == "1" ]]; then',
        deploying,
    )
    image_readback = deploy.index("SCHAUWERK_RUNTIME_LIVE_IMAGE", postflight)
    index_url = deploy.index("https://commonthing.net/schaubild/", image_readback)
    manifest_url = deploy.index("https://commonthing.net/schaubild/manifest.json", index_url)
    native_schema = deploy.index("schauwerk-standalone-editor-manifest.v2", manifest_url)
    native_engine = deploy.index("schauwerk-native-diagram-v1", native_schema)
    native_api = deploy.index("/schaubild/api/native-viewer", native_engine)
    state_commit = deploy.index("# 8. Update State (Post-Health)")
    assert (
        postflight
        < image_readback
        < index_url
        < manifest_url
        < native_schema
        < native_engine
        < native_api
        < state_commit
    )

def test_image_labels_bind_exact_source_commit() -> None:
    result = MODULE.verify_image_labels(
        {
            "org.opencontainers.image.revision": SOURCE_COMMIT,
            "org.opencontainers.image.source": MODULE.IMAGE_SOURCE_URL,
            "other": "allowed",
        },
        expected_commit=SOURCE_COMMIT,
    )
    assert result == {
        "source_commit": SOURCE_COMMIT,
        "source_url": MODULE.IMAGE_SOURCE_URL,
    }


@pytest.mark.parametrize(
    "labels",
    [
        {},
        {
            "org.opencontainers.image.revision": "c" * 40,
            "org.opencontainers.image.source": MODULE.IMAGE_SOURCE_URL,
        },
        {
            "org.opencontainers.image.revision": SOURCE_COMMIT,
            "org.opencontainers.image.source": "https://github.com/other/repo",
        },
    ],
)
def test_image_labels_fail_closed_on_source_digest_binding_drift(
    labels: dict[str, str],
) -> None:
    with pytest.raises(MODULE.ReleaseContractError):
        MODULE.verify_image_labels(labels, expected_commit=SOURCE_COMMIT)

def test_full_vps_deploy_verifies_oci_source_before_edge_mutation() -> None:
    repo = Path(__file__).resolve().parents[3]
    deploy = (repo / "scripts" / "weltgewebe-up").read_text(encoding="utf-8")
    lock_preflight = deploy.index("scripts/preflight/schauwerk_editor_release.py")
    image_probe = deploy.index(
        'docker image inspect "$SCHAUWERK_RUNTIME_IMAGE_REF"', lock_preflight
    )
    pull_config = deploy.index(
        'SCHAUWERK_PULL_DOCKER_CONFIG="$(mktemp -d)"', image_probe
    )
    exact_pull = deploy.index(
        'DOCKER_CONFIG="$SCHAUWERK_PULL_DOCKER_CONFIG" docker pull '
        '"$SCHAUWERK_RUNTIME_IMAGE_REF"',
        pull_config,
    )
    label_probe = deploy.index("SCHAUWERK_RUNTIME_IMAGE_LABELS", exact_pull)
    pull_block = deploy[pull_config:label_probe]
    assert pull_block.count('rm -rf -- "$SCHAUWERK_PULL_DOCKER_CONFIG"') == 2
    source_verify = deploy.index("--image-labels-json", label_probe)
    edge_guard = deploy.index("# 6d. Static UI Runtime Guard & Edge Caddy Recreate")
    edge_recreate = deploy.index('docker rm -f "$EDGE_GATEWAY_CONTAINER"', edge_guard)
    assert (
        lock_preflight
        < image_probe
        < pull_config
        < exact_pull
        < label_probe
        < source_verify
        < edge_guard
        < edge_recreate
    )


def test_full_vps_postflight_reverifies_running_oci_source() -> None:
    repo = Path(__file__).resolve().parents[3]
    deploy = (repo / "scripts" / "weltgewebe-up").read_text(encoding="utf-8")
    postflight = deploy.index("SCHAUWERK_RUNTIME_LIVE_IMAGE")
    live_labels = deploy.index("SCHAUWERK_RUNTIME_LIVE_LABELS", postflight)
    source_verify = deploy.index("--image-labels-json", live_labels)
    health = deploy.index("wait_for_schaubild_runtime_health", source_verify)
    assert postflight < live_labels < source_verify < health
