from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
COMPOSE = REPO / "infra" / "compose" / "compose.prod.yml"
DOCKERFILE = REPO / "apps" / "api" / "Dockerfile"
DEPLOY = REPO / "scripts" / "weltgewebe-up"
ACTIVATE = REPO / "scripts" / "ops" / "activate-production-search-vps.sh"
WORKER = REPO / "scripts" / "ops" / "search-worker-loop.sh"


def test_ollama_is_digest_pinned_and_loopback_only() -> None:
    compose = COMPOSE.read_text(encoding="utf-8")
    assert "ollama/ollama:0.12.6@sha256:352e045b937ac29d3d9550c22fb85525f60a89e064df34c26579bee5a93b3a16" in compose
    assert "network_mode: service:api" in compose
    assert "OLLAMA_HOST: 127.0.0.1:11434" in compose
    ollama_block = compose.split("  ollama:", 1)[1].split("\n  db:", 1)[0]
    assert "ports:" not in ollama_block
    assert "ollama_models:/root/.ollama" in ollama_block
    assert "WELTGEWEBE_SEARCH_OLLAMA_URL: ${WELTGEWEBE_SEARCH_OLLAMA_URL:-http://127.0.0.1:11434/}" in compose


def test_api_image_contains_bounded_backfill_binary() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert "--bin weltgewebe-api --bin search-backfill" in dockerfile
    assert "target/release/search-backfill /app/search-backfill" in dockerfile


def test_scoped_deploy_treats_api_and_ollama_as_one_unit() -> None:
    deploy = DEPLOY.read_text(encoding="utf-8")
    assert "SCOPED_TARGET_SERVICES=(api)" in deploy
    assert "SCOPED_TARGET_SERVICES+=(ollama)" in deploy
    assert "SCOPED_TARGET_SERVICES+=(search-worker)" in deploy
    assert 'SCOPED_INITIAL_CMD+=("${SCOPED_TARGET_SERVICES[@]}")' in deploy
    assert '"allowed_recreated_services": targets' in deploy
    assert "PROTECTED_SERVICES=(db nats caddy schaubild)" in deploy
    assert "configured_preflight_services" in deploy
    assert "WELTGEWEBE_SEARCH_MIN_DISK_BYTES" in deploy
    assert "WELTGEWEBE_SEARCH_MIN_MEMORY_BYTES" in deploy
    assert "WELTGEWEBE_SEARCH_MIN_CPU_COUNT" in deploy
    assert deploy.index("configured_preflight_services") < deploy.index("# 4. Build Decision")


def test_activation_is_commit_locked_identity_bound_and_gate_first() -> None:
    script = ACTIVATE.read_text(encoding="utf-8")
    assert "flock -n 9" in script
    assert "requested commit is no longer current origin/main" in script
    assert "sha256:df5bd2e3c74cd8d069d21dc038f1b359fcdc9458fce1c99bd43c9eb1518ff907" in script
    assert "search-gen-2e8358273aa6d41e6a59025985a99738614aba725b8f369b3a54f390f8752e5c" in script
    assert "weltgewebe_search_generation_activation_ready" in script
    gate = script.index("weltgewebe_search_generation_activation_ready")
    activate = script.index("weltgewebe_activate_search_generation")
    assert gate < activate
    assert "search_activation=verified" in script
    assert "search worker stopped or was replaced after activation" in script
    assert "active generation has no public semantic probe candidate" in script
    assert "any(.items[]?; .id == $probe_id)" in script
    assert 'semantic_probe_status="candidate_bound"' in script
    assert 'gate_ready="$(psql_exec_sql ' in script
    assert "rollback_activation()" in script
    assert 'rollback_status="verified"' in script
    assert "rollback could not be verified" in script
    assert 'failed to capture previous active search generation' in script
    assert 'worker_cid="$("${compose[@]}" ps -q search-worker)"' in script
    assert "WELTGEWEBE_SEARCH_MIN_CPU_COUNT" in script
    assert "getconf _NPROCESSORS_ONLN" in script
    assert "SemantAH" in script


def test_activation_routes_psql_variables_through_stdin() -> None:
    script = ACTIVATE.read_text(encoding="utf-8")
    helper_body = script.split("psql_exec_sql() {", 1)[1].split("\n}\n", 1)[0]
    helper = "psql_exec_sql() {" + helper_body + "\n}\n"
    command = (
        r"""psql_exec() { printf 'argv=%s\n' "$*" >&2; cat; }
"""
        + helper
        + """psql_exec_sql "SELECT :'gen';" -At -v gen=probe-value
"""
    )
    result = subprocess.run(["bash", "-c", command], check=True, capture_output=True, text=True)
    assert result.stdout == "SELECT :'gen';\n"
    assert result.stderr == "argv=-At -v gen=probe-value\n"

    variable_sql_lines = [line for line in script.splitlines() if ":'" in line]
    assert variable_sql_lines
    assert all("psql_exec_sql" in line for line in variable_sql_lines)
    assert all(" -c " not in line and "-Atc" not in line for line in variable_sql_lines)


def test_activation_derives_compose_identity_from_the_verified_release_commit() -> None:
    script = ACTIVATE.read_text(encoding="utf-8")
    assert 'build_identity_short="${COMMIT:0:8}"' in script
    assert 'build_timestamp="$(git -C "$release_dir" show -s --format=%cI "$COMMIT")"' in script
    assert 'export API_VERSION="$build_identity_short"' in script
    assert 'export WELTGEWEBE_BUILD="$build_identity_short"' in script
    assert 'export GIT_COMMIT_SHA="$COMMIT"' in script
    assert 'export BUILD_TIMESTAMP="$build_timestamp"' in script
    public_readback = script.index('public frontend commit mismatch')
    identity_export = script.index('export API_VERSION="$build_identity_short"')
    compose_config = script.index('compose=(docker compose')
    assert public_readback < identity_export < compose_config


def test_activation_compares_canonical_and_unprefixed_model_digests() -> None:
    script = ACTIVATE.read_text(encoding="utf-8")
    assert 'local digest="${1#sha256:}"' in script
    assert '[[ "$digest" =~ ^[0-9a-f]{64}$ ]] || return 1' in script
    assert 'observed_digest_normalized="$(normalize_sha256_digest "$observed_digest")"' in script
    assert 'expected_digest_normalized="$(normalize_sha256_digest "$MODEL_REVISION")"' in script
    assert '[[ "$observed_digest_normalized" == "$expected_digest_normalized" ]]' in script
    assert '[[ "$observed_digest" == "$MODEL_REVISION" ]]' not in script


def test_digest_normalizer_accepts_both_ollama_and_canonical_forms() -> None:
    script = ACTIVATE.read_text(encoding="utf-8")
    function = script.split("normalize_sha256_digest() {", 1)[1].split("\n}\n", 1)[0]
    helper = "normalize_sha256_digest() {" + function + "\n}\n"
    digest = "df5bd2e3c74cd8d069d21dc038f1b359fcdc9458fce1c99bd43c9eb1518ff907"
    command = helper + f'normalize_sha256_digest "{digest}"; normalize_sha256_digest "sha256:{digest}"'
    result = subprocess.run(["bash", "-c", command], check=True, capture_output=True, text=True)
    assert result.stdout.splitlines() == [digest, digest]
    malformed = subprocess.run(
        ["bash", "-c", helper + 'normalize_sha256_digest "sha256:not-a-digest"'],
        capture_output=True,
        text=True,
    )
    assert malformed.returncode != 0


def _shell_function(script: str, name: str) -> str:
    function = script.split(f"{name}() {{", 1)[1].split("\n}\n", 1)[0]
    return f"{name}() {{" + function + "\n}\n"


def _worker_digest_helper() -> str:
    return _shell_function(WORKER.read_text(encoding="utf-8"), "normalize_sha256_digest")


def _worker_provider_ready_helper() -> str:
    script = WORKER.read_text(encoding="utf-8")
    return _shell_function(script, "normalize_sha256_digest") + _shell_function(script, "provider_ready")


def _worker_provider_ready_result(*, observed: str, expected: str) -> subprocess.CompletedProcess[str]:
    payload = json.dumps({"models": [{"name": "qwen3-embedding:8b", "digest": observed}]})
    command = (
        "set -Eeuo pipefail\n"
        "readonly OLLAMA_URL=http://127.0.0.1:11434/\n"
        "readonly MODEL_ID=qwen3-embedding:8b\n"
        f"readonly MODEL_REVISION={shlex.quote(expected)}\n"
        f"wget() {{ printf '%s\\n' {shlex.quote(payload)}; }}\n"
        + _worker_provider_ready_helper()
        + "provider_ready\n"
    )
    return subprocess.run(["bash", "-c", command], capture_output=True, text=True)


def test_worker_digest_normalizer_accepts_canonical_and_ollama_forms() -> None:
    digest = "df5bd2e3c74cd8d069d21dc038f1b359fcdc9458fce1c99bd43c9eb1518ff907"
    result = subprocess.run(
        ["bash", "-c", _worker_digest_helper() + f'normalize_sha256_digest "{digest}"; normalize_sha256_digest "sha256:{digest}"'],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.splitlines() == [digest, digest]


def test_worker_digest_normalizer_rejects_missing_or_invalid_digests() -> None:
    helper = _worker_digest_helper()
    for value in ("", "sha256:", "sha256:not-a-digest", "a" * 63, "A" * 64):
        result = subprocess.run(
            ["bash", "-c", helper + f'normalize_sha256_digest "{value}"'],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0


def test_worker_provider_ready_reproduces_prefix_difference_and_stays_fail_closed() -> None:
    digest = "df5bd2e3c74cd8d069d21dc038f1b359fcdc9458fce1c99bd43c9eb1518ff907"
    accepted = _worker_provider_ready_result(observed=digest, expected=f"sha256:{digest}")
    assert accepted.returncode == 0, accepted.stderr

    for observed, expected in (
        ("a" * 64, f"sha256:{digest}"),
        ("sha256:not-a-digest", f"sha256:{digest}"),
        (digest, "sha256:not-a-digest"),
        ("", f"sha256:{digest}"),
    ):
        rejected = _worker_provider_ready_result(observed=observed, expected=expected)
        assert rejected.returncode != 0


def test_worker_compares_normalized_digests_and_stays_fail_closed() -> None:
    script = WORKER.read_text(encoding="utf-8")
    assert 'observed_normalized="$(normalize_sha256_digest "$observed")" || return 1' in script
    assert 'expected_normalized="$(normalize_sha256_digest "$MODEL_REVISION")" || return 1' in script
    assert '[[ "$observed_normalized" == "$expected_normalized" ]]' in script
    assert '[[ "$observed" == "$MODEL_REVISION" ]]' not in script


def test_persistent_worker_waits_for_exact_model_and_runs_bounded_batches() -> None:
    compose = COMPOSE.read_text(encoding="utf-8")
    worker = WORKER.read_text(encoding="utf-8")
    assert "  search-worker:" in compose
    assert "network_mode: service:api" in compose.split("  search-worker:", 1)[1].split("\n  db:", 1)[0]
    assert "search-gen-2e8358273aa6d41e6a59025985a99738614aba725b8f369b3a54f390f8752e5c" in compose
    assert "sha256:df5bd2e3c74cd8d069d21dc038f1b359fcdc9458fce1c99bd43c9eb1518ff907" in compose
    assert "provider_ready" in worker
    assert "waiting_for_pinned_model" in worker
    assert 'GEWEBE_SEED_REAL: "false"' in compose
    assert 'GEWEBE_SEED_DEMO: "false"' in compose
    assert "/app/search-backfill" in worker
    assert 'sleep "$INTERVAL_SECONDS"' in worker


def test_projection_privacy_contract_is_generation_bound() -> None:
    worker = (REPO / "apps" / "api" / "src" / "search" / "worker.rs").read_text(encoding="utf-8")
    repository = (REPO / "apps" / "api" / "src" / "search" / "repository.rs").read_text(encoding="utf-8")
    migration = (
        REPO
        / "apps"
        / "api"
        / "migrations"
        / "20260724000002_semantic_search_projection_privacy_boundary.up.sql"
    ).read_text(encoding="utf-8")
    revision = "node-document-v4-canonical-visibility"
    assert revision in worker
    assert revision in migration
    assert "ADD COLUMN search_visibility TEXT NOT NULL DEFAULT 'public'" in migration
    assert "OLD.search_visibility IS NOT DISTINCT FROM NEW.search_visibility" in migration
    assert "OLD.payload -> 'created_by_account_id'" in migration
    assert "LEFT JOIN domain_nodes" in migration
    assert "spec.validate()?" in worker
    assert "search generation id does not match its derived identity" in worker
    assert "n.search_visibility" in worker
    assert "ARRAY['owner']::TEXT[],'unavailable',NULL" in worker
    assert "p.visibility_scopes = ARRAY['owner']::TEXT[]" in repository
    assert "p.embedding IS NULL" in repository
    assert "p.visibility_scopes = ARRAY['owner']::TEXT[]" in migration
    assert "p.embedding IS NULL" in migration
    assert "n.search_visibility = 'public'" in repository
    assert "n.search_visibility = 'private'" in repository
    assert "n.payload::text AS payload," in repository
    assert "scored.search_visibility" in repository
    assert "InvalidVisibility" in repository
    assert "unwrap_or_default()" not in repository.split("search_visibility_raw", 1)[1]
    assert "n.payload ->> 'search_visibility'" not in repository


def test_privacy_contract_is_wired_into_make_ci_validate() -> None:
    makefile = (REPO / "Makefile").read_text(encoding="utf-8")
    assert (
        "$(UV_RUN) python -m pytest -q scripts/ci/tests/test_semantic_search_production_activation.py"
        in makefile
    )
