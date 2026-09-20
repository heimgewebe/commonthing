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


@pytest.mark.parametrize(
    "compose_file", OVERRIDES_WITH_IP_RATE_LIMITS, ids=lambda path: path.name
)
def test_trusted_proxy_default_covers_the_compose_bridge_network(
    compose_file: Path,
) -> None:
    declaration = api_environment(compose_file)[TRUSTED_PROXIES_KEY]

    # Interpolation shape: an operator override wins, the baked-in fallback must
    # still trust the bridge network Caddy reaches the API from.
    assert declaration.startswith("${AUTH_TRUSTED_PROXIES:-"), (
        f"{compose_file.name} must let the operator override the proxy allowlist, "
        f"got: {declaration}"
    )
    fallback = declaration[len("${AUTH_TRUSTED_PROXIES:-") : -1]

    assert "172.16.0.0/12" in fallback, (
        f"{compose_file.name} fallback must trust the Docker bridge range "
        f"(Caddy's peer address), got: {fallback}"
    )
    assert "127.0.0.1" in fallback, (
        f"{compose_file.name} fallback must keep trusting loopback, got: {fallback}"
    )


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
