"""Preflight: per-IP rate limits and proxy trust must be declared together.

`effective_client_ip` in `apps/api/src/routes/auth.rs` only honours
`X-Forwarded-For` / `Forwarded` for a peer listed in `AUTH_TRUSTED_PROXIES`.
A reverse proxy on a Compose bridge network never presents a loopback peer, so a
compose file that configures `AUTH_RL_IP_PER_*` without also declaring
`AUTH_TRUSTED_PROXIES` collapses every client into one rate-limit bucket.

`AppConfig::validate` refuses that combination at startup. These tests keep the
compose files from regressing into a stack that cannot boot -- and, before the
config gate existed, silently served a broken rate limiter.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


REPO = Path(__file__).resolve().parents[3]
COMPOSE_DIR = REPO / "infra" / "compose"

VPS_OVERRIDE = COMPOSE_DIR / "compose.vps.override.yml"
HEIMSERVER_OVERRIDE = COMPOSE_DIR / "compose.prod.override.yml"
CADDY_VPS = REPO / "infra" / "caddy" / "Caddyfile.vps"
CADDY_HEIM = REPO / "infra" / "caddy" / "Caddyfile.heim"

IP_RATE_LIMIT_KEYS = ("AUTH_RL_IP_PER_MIN", "AUTH_RL_IP_PER_HOUR")
TRUSTED_PROXIES_KEY = "AUTH_TRUSTED_PROXIES"

# Every compose file that may install a per-IP rate limiter.
OVERRIDES_WITH_IP_RATE_LIMITS = (VPS_OVERRIDE, HEIMSERVER_OVERRIDE)


def api_environment(compose_file: Path) -> dict[str, str]:
    document = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    environment = document["services"]["api"].get("environment", {})
    if isinstance(environment, list):
        pairs = (entry.split("=", 1) for entry in environment)
        return {key: value for key, value in pairs}
    return {key: str(value) for key, value in environment.items()}


@pytest.mark.parametrize(
    "compose_file", OVERRIDES_WITH_IP_RATE_LIMITS, ids=lambda path: path.name
)
def test_ip_rate_limits_are_paired_with_a_trusted_proxy_declaration(
    compose_file: Path,
) -> None:
    environment = api_environment(compose_file)

    configures_ip_rate_limit = any(key in environment for key in IP_RATE_LIMIT_KEYS)
    if not configures_ip_rate_limit:
        pytest.skip(f"{compose_file.name} installs no per-IP rate limiter")

    assert TRUSTED_PROXIES_KEY in environment, (
        f"{compose_file.name} sets {IP_RATE_LIMIT_KEYS} without {TRUSTED_PROXIES_KEY}. "
        "Behind a containerised reverse proxy the API would bucket every client "
        "under the proxy IP, and AppConfig::validate refuses to start."
    )


def test_heimserver_trusted_proxy_default_covers_the_compose_bridge_network() -> None:
    declaration = api_environment(HEIMSERVER_OVERRIDE)[TRUSTED_PROXIES_KEY]

    # Interpolation shape: an operator override wins, the baked-in fallback must
    # still trust the bridge network Caddy reaches the API from.
    assert declaration.startswith("${AUTH_TRUSTED_PROXIES:-"), (
        f"{HEIMSERVER_OVERRIDE.name} must let the operator override the proxy "
        f"allowlist, got: {declaration}"
    )
    fallback = declaration[len("${AUTH_TRUSTED_PROXIES:-") : -1]

    assert "172.16.0.0/12" in fallback, (
        f"{HEIMSERVER_OVERRIDE.name} fallback must trust the Docker bridge range "
        f"(Caddy's peer address), got: {fallback}"
    )
    assert "127.0.0.1" in fallback, (
        f"{HEIMSERVER_OVERRIDE.name} fallback must keep trusting loopback, got: {fallback}"
    )


CADDY_INGRESS_IPV4 = "${WELTGEWEBE_INGRESS_CADDY_IPV4:-10.231.64.2}"


def vps_document() -> dict:
    return yaml.safe_load(VPS_OVERRIDE.read_text(encoding="utf-8"))


def test_vps_trusts_exactly_the_pinned_caddy_address() -> None:
    """Audit B3: on the VPS no sibling container may spoof X-Forwarded-For.

    172.16.0.0/12 plus loopback trusted db, nats, schaubild and the ollama /
    search-worker processes that share the API network namespace. The VPS
    override now trusts only Caddy's pinned ingress address.
    """
    declaration = api_environment(VPS_OVERRIDE)[TRUSTED_PROXIES_KEY]
    assert declaration == "${AUTH_TRUSTED_PROXIES:-" + CADDY_INGRESS_IPV4 + "/32}"

    services = vps_document()["services"]
    assert services["caddy"]["networks"] == {"ingress": {"ipv4_address": CADDY_INGRESS_IPV4}}
    assert services["schaubild"]["networks"] == ["ingress"]
    assert set(services["api"]["networks"]) == {"default", "ingress"}
    assert services["api"]["networks"]["default"]["aliases"] == ["weltgewebe-api"]
    command = services["schaubild"]["command"]
    cidr = command[command.index("--trusted-proxy-source-cidr") + 1]
    assert cidr == "${SCHAUWERK_SCHAUBILD_TRUSTED_PROXY_CIDR:-" + CADDY_INGRESS_IPV4 + "/32}"


def test_base_prod_compose_does_not_bake_a_permissive_default() -> None:
    """The base file stays empty-by-default so a bare `-f compose.prod.yml` run
    cannot silently inherit a proxy allowlist it never declared."""
    declaration = api_environment(COMPOSE_DIR / "compose.prod.yml")[TRUSTED_PROXIES_KEY]

    assert declaration == "${AUTH_TRUSTED_PROXIES:-}"


def test_api_refuses_undeclared_proxy_trust_under_ip_rate_limits() -> None:
    """The compose defaults above are a convenience, not the enforcement point.

    Enforcement lives in `AppConfig::validate`; assert the guard is present so the
    compose files and the binary cannot drift apart.
    """
    config_rs = (REPO / "apps" / "api" / "src" / "config.rs").read_text(encoding="utf-8")
    # Collapse whitespace so the assertion survives rustfmt line wrapping.
    normalized = " ".join(config_rs.split())

    assert "fn has_ip_rate_limits" in normalized
    assert (
        "self.has_ip_rate_limits() && self.auth_trusted_proxies.is_none()"
        in normalized
    )
    assert "validate_trusted_proxy_declaration(proxies)?" in normalized
    assert "AUTH_TRUSTED_PROXIES is not set" in normalized
    assert "contains invalid entry" in normalized


@pytest.mark.parametrize("caddyfile", (CADDY_VPS, CADDY_HEIM), ids=lambda path: path.name)
def test_trusted_caddy_hops_overwrite_client_ip_headers(caddyfile: Path) -> None:
    text = caddyfile.read_text(encoding="utf-8")
    trusted_upstream_count = (
        text.count("reverse_proxy api:8080")
        + text.count("reverse_proxy schaubild:8765")
    )
    forwarded_removal_count = text.count("header_up -Forwarded")
    xff_overwrite_count = text.count("header_up X-Forwarded-For {remote_host}")

    assert trusted_upstream_count > 0
    assert forwarded_removal_count == trusted_upstream_count, (
        f"{caddyfile.name} has {trusted_upstream_count} trusted proxy hops but only "
        f"{forwarded_removal_count} Forwarded removals"
    )
    assert xff_overwrite_count == trusted_upstream_count, (
        f"{caddyfile.name} has {trusted_upstream_count} trusted proxy hops but only "
        f"{xff_overwrite_count} X-Forwarded-For overwrites; Caddy would append "
        "the real peer to a client-supplied chain and leave the left-most value spoofable"
    )


def test_deploy_vps_shim_keeps_vps_caddy_and_static_web_guards() -> None:
    shim = (REPO / "scripts" / "deploy_vps.sh").read_text(encoding="utf-8")
    entrypoint = (REPO / "scripts" / "weltgewebe-up").read_text(encoding="utf-8")
    base_compose = (COMPOSE_DIR / "compose.prod.yml").read_text(encoding="utf-8")
    vps_override = VPS_OVERRIDE.read_text(encoding="utf-8")
    caddy_vps = CADDY_VPS.read_text(encoding="utf-8")

    assert 'DEPLOY_TARGET="${DEPLOY_TARGET:-vps}"' in shim
    assert '"${SCRIPT_DIR}/weltgewebe-up" "$@"' in shim
    assert (
        '[[ "$DEPLOY_TARGET" == "vps" && "$WITH_CADDY" == "0" '
        '&& "$DEPLOY_SCOPE" == "full" ]]'
        in entrypoint
    )
    assert "WITH_CADDY=1" in entrypoint
    assert 'export API_VERSION="$HEAD_AFTER"' in entrypoint
    assert 'export WELTGEWEBE_BUILD="$HEAD_AFTER"' in entrypoint
    assert 'export GIT_COMMIT_SHA="$HEAD_AFTER_FULL"' in entrypoint
    assert 'export BUILD_TIMESTAMP="$HEAD_COMMIT_TIMESTAMP"' in entrypoint
    assert "${API_VERSION:?API_VERSION must be set}" in base_compose
    assert "${WELTGEWEBE_BUILD:?WELTGEWEBE_BUILD must be set}" in base_compose
    assert "${GIT_COMMIT_SHA:?GIT_COMMIT_SHA must be set}" in base_compose
    assert "${BUILD_TIMESTAMP:?BUILD_TIMESTAMP must be set}" in base_compose

    static_mount = "../../apps/web/build:/srv/weltgewebe-web:ro"
    assert static_mount in base_compose
    assert static_mount in vps_override
    assert "WEB_UPSTREAM_HOST" not in vps_override
    assert "WEB_UPSTREAM_URL" not in vps_override
    assert "root * /srv/weltgewebe-web" in caddy_vps
    assert 'X-Weltgewebe-Build "{$WELTGEWEBE_BUILD}"' in caddy_vps


# --- scripts/ops/check_ingress_proxy_trust.py -------------------------------

CHECKER = REPO / "scripts" / "ops" / "check_ingress_proxy_trust.py"


def _load_checker():
    import importlib.util

    spec = importlib.util.spec_from_file_location("check_ingress_proxy_trust", CHECKER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _rendered(**overrides) -> dict:
    cfg = {
        "services": {
            "api": {
                "environment": {"AUTH_TRUSTED_PROXIES": "10.231.64.2/32"},
                "networks": {"default": {"aliases": ["weltgewebe-api"]}, "ingress": None},
            },
            "caddy": {"networks": {"ingress": {"ipv4_address": "10.231.64.2"}}},
            "schaubild": {
                "networks": {"ingress": None},
                "command": ["serve", "--trusted-proxy-source-cidr", "10.231.64.2/32"],
            },
        },
        "networks": {
            "ingress": {
                "name": "weltgewebe_ingress",
                "ipam": {
                    "config": [
                        {
                            "subnet": "10.231.64.0/28",
                            "gateway": "10.231.64.1",
                            "ip_range": "10.231.64.8/29",
                        }
                    ]
                },
            }
        },
    }
    for path, value in overrides.items():
        node = cfg
        *parents, leaf = path.split(".")
        for key in parents:
            node = node[key]
        node[leaf] = value
    return cfg


def test_checker_accepts_the_shipped_contract() -> None:
    caddy_ip, errors = _load_checker().check_rendered(_rendered())
    assert (caddy_ip, errors) == ("10.231.64.2", [])


@pytest.mark.parametrize(
    ("path", "value", "needle"),
    [
        (
            "services.api.environment",
            {"AUTH_TRUSTED_PROXIES": "127.0.0.1,::1,172.16.0.0/12"},
            "AUTH_TRUSTED_PROXIES",
        ),
        (
            "services.schaubild.command",
            ["serve", "--trusted-proxy-source-cidr", "172.16.0.0/12"],
            "--trusted-proxy-source-cidr",
        ),
        (
            "services.caddy.networks",
            {"ingress": {"ipv4_address": "10.231.64.2"}, "default": None},
            "ingress network only",
        ),
        (
            "services.caddy.networks",
            {"ingress": {"ipv4_address": "10.231.64.9"}},
            "dynamic ip_range",
        ),
        ("services.caddy.networks", {"ingress": None}, "pinned ipv4_address"),
        ("services.schaubild.networks", {"ingress": None, "default": None}, "schaubild"),
        ("services.api.networks", {"default": None}, "default and ingress"),
    ],
)
def test_checker_rejects_drift(path: str, value, needle: str) -> None:
    _, errors = _load_checker().check_rendered(_rendered(**{path: value}))
    assert any(needle in error for error in errors), errors


def test_checker_rejects_running_caddy_without_the_pinned_address() -> None:
    checker = _load_checker()
    cfg = _rendered()
    assert checker.check_running_caddy(
        cfg, "10.231.64.2", {"weltgewebe_ingress": {"IPAddress": "10.231.64.2"}}
    ) == []
    errors = checker.check_running_caddy(
        cfg, "10.231.64.2", {"weltgewebe_default": {"IPAddress": "172.18.0.5"}}
    )
    assert errors and "full deploy" in errors[0]


def test_checker_accepts_the_real_vps_compose_render() -> None:
    """Render the shipped files through Compose itself, not a YAML reading."""
    import json
    import os
    import shutil
    import subprocess

    if shutil.which("docker") is None:
        pytest.skip("docker CLI not available")
    env = {
        **os.environ,
        "API_VERSION": "test",
        "GIT_COMMIT_SHA": "0" * 40,
        "BUILD_TIMESTAMP": "2026-01-01T00:00:00Z",
        "WELTGEWEBE_BUILD": "test",
        "SCHAUWERK_SCHAUBILD_IMAGE": "example.invalid/schaubild@sha256:" + "0" * 64,
        "WELTGEWEBE_ENV_FILE": os.devnull,
        "REPO_DIR": str(REPO),
    }
    for key in list(env):
        if key.startswith(("AUTH_TRUSTED_PROXIES", "WELTGEWEBE_INGRESS_", "SCHAUWERK_SCHAUBILD_TRUSTED")):
            env.pop(key)
    proc = subprocess.run(
        [
            "docker", "compose", "-p", "weltgewebe",
            "-f", str(COMPOSE_DIR / "compose.prod.yml"),
            "-f", str(VPS_OVERRIDE),
            "config", "--format", "json",
        ],
        capture_output=True, text=True, env=env, check=False,
    )
    if proc.returncode != 0 and "is not a docker command" in proc.stderr:
        pytest.skip("docker compose plugin not available")
    assert proc.returncode == 0, proc.stderr
    caddy_ip, errors = _load_checker().check_rendered(json.loads(proc.stdout))
    assert (caddy_ip, errors) == ("10.231.64.2", [])


def _render_vps(tmp_path: Path, env_file_lines: list[str], shell: dict[str, str]):
    import json
    import os
    import shutil
    import subprocess

    if shutil.which("docker") is None:
        pytest.skip("docker CLI not available")
    env_file = tmp_path / "runtime.env"
    env_file.write_text("\n".join(env_file_lines) + "\n", encoding="utf-8")
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(
            ("AUTH_TRUSTED_PROXIES", "WELTGEWEBE_INGRESS_", "SCHAUWERK_SCHAUBILD_TRUSTED")
        )
    }
    env.update(
        {
            "API_VERSION": "test",
            "GIT_COMMIT_SHA": "0" * 40,
            "BUILD_TIMESTAMP": "2026-01-01T00:00:00Z",
            "WELTGEWEBE_BUILD": "test",
            "SCHAUWERK_SCHAUBILD_IMAGE": "example.invalid/schaubild@sha256:" + "0" * 64,
            "WELTGEWEBE_ENV_FILE": os.devnull,
            "REPO_DIR": str(REPO),
            **shell,
        }
    )
    proc = subprocess.run(
        ["docker", "compose", "--env-file", str(env_file), "-p", "weltgewebe",
         "-f", str(COMPOSE_DIR / "compose.prod.yml"), "-f", str(VPS_OVERRIDE),
         "config", "--format", "json"],
        capture_output=True, text=True, env=env, check=False,
    )
    if proc.returncode != 0 and "is not a docker command" in proc.stderr:
        pytest.skip("docker compose plugin not available")
    assert proc.returncode == 0, proc.stderr
    return _load_checker().check_rendered(json.loads(proc.stdout))


OVERRIDDEN_INGRESS = [
    "WELTGEWEBE_INGRESS_SUBNET=10.99.0.0/28",
    "WELTGEWEBE_INGRESS_GATEWAY=10.99.0.1",
    "WELTGEWEBE_INGRESS_DYNAMIC_RANGE=10.99.0.8/29",
    "WELTGEWEBE_INGRESS_CADDY_IPV4=10.99.0.2",
]


def test_ingress_override_in_the_runtime_env_file_alone_renders_consistently(
    tmp_path: Path,
) -> None:
    """weltgewebe-up never sources the env file into Bash, so every default
    that depends on WELTGEWEBE_INGRESS_CADDY_IPV4 must be resolved by Compose.
    """
    caddy_ip, errors = _render_vps(tmp_path, OVERRIDDEN_INGRESS, {})
    assert (caddy_ip, errors) == ("10.99.0.2", [])


def test_shell_only_schaubild_default_would_shadow_the_env_file(tmp_path: Path) -> None:
    """The shape weltgewebe-up must not produce: a shell-derived default CIDR
    exported over the env file's ingress override is refused, not deployed."""
    _, errors = _render_vps(
        tmp_path,
        OVERRIDDEN_INGRESS,
        {"SCHAUWERK_SCHAUBILD_TRUSTED_PROXY_CIDR": "10.231.64.2/32"},
    )
    assert any("--trusted-proxy-source-cidr" in error for error in errors), errors


def test_weltgewebe_up_does_not_default_the_schaubild_cidr_in_bash() -> None:
    deploy = (REPO / "scripts" / "weltgewebe-up").read_text(encoding="utf-8")
    assert (
        'SCHAUWERK_RUNTIME_TRUSTED_PROXY_CIDR="${SCHAUWERK_SCHAUBILD_TRUSTED_PROXY_CIDR:-}"'
        in deploy
    )
    assert "10.231.64.2" not in deploy
