from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import unittest

import yaml

REPO = Path(__file__).resolve().parents[3]
CONTRACT = REPO / "scripts/ci/postgres-proof-contract.json"
RUNNER = REPO / "scripts/ci/run-postgres-integration-proofs.sh"
RUST_SUPPORT = REPO / "apps/api/tests/support/postgres_proof.rs"
CI = REPO / ".github/workflows/ci.yml"
AUTH_PASSKEY_PROOF = REPO / ".github/workflows/auth-passkey-register-proof.yml"
GOVERNANCE_FULL_FLOW_PROOF = REPO / ".github/workflows/governance-full-flow-proof.yml"

TARGETS = {
    "db_auto_provision_write_path",
    "db_domain_account_write_path",
    "db_domain_backfill",
    "db_domain_edge_write_path",
    "db_domain_node_write_path",
    "db_governance",
    "db_multi_instance_foundation",
    "db_domain_read_path",
    "db_domain_schema_migrations",
    "db_passkey_fk_readiness",
    "db_passkey_schema_preflight",
    "db_passkey_store_persistence",
    "db_session_store_persistence",
    "db_webauthn_user_id_backfill_audit",
    "db_semantic_search_foundation",
    "db_semantic_search_projection_worker",
    "sqlx_postgres_direct_session_crud",
}


def test_disposable_database_contract_is_single_source_for_runner_and_rust_tests() -> None:
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    assert contract["schema_version"] == 1
    assert contract["disposable_database_name_segments"] == ["test", "proof", "ci"]
    assert contract["pgbouncer_port"] == 6432
    runner = RUNNER.read_text(encoding="utf-8")
    rust = RUST_SUPPORT.read_text(encoding="utf-8")
    assert "postgres-proof-contract.json" in runner
    assert "postgres-proof-contract.json" in rust
    for target in TARGETS:
        source = (REPO / "apps/api/tests" / f"{target}.rs").read_text(encoding="utf-8")
        assert "mod support;" in source


def test_runner_preflights_postgres_and_jetstream_before_the_first_target() -> None:
    runner = RUNNER.read_text(encoding="utf-8")
    preflight = runner.index("preflight_postgres\nprovision_jetstream_if_requested")
    loop = runner.index('for target in "${targets[@]}"; do')
    assert preflight < loop
    assert "info.get('jetstream') is not True" in runner
    assert 'trap cleanup_exit EXIT' in runner
    assert 'trap cleanup_int INT' in runner
    assert 'trap cleanup_term TERM' in runner
    assert 'docker rm --force "$owned_nats_container"' in runner
    assert 'docker exec "$POSTGRES_RESET_CONTAINER" pg_isready' in runner
    assert 'docker exec "$POSTGRES_RESET_CONTAINER" psql' in runner


def test_default_runner_executes_push_device_limit_recovery_proof() -> None:
    runner = RUNNER.read_text(encoding="utf-8")
    proof = "private_message_push_preference_cancels_queued_delivery"
    assert 'if [[ -z "${POSTGRES_PROOF_TARGETS:-}" ]]; then' in runner
    assert "--test db_node_conversations" in runner
    assert proof in runner
    default_guard = runner.index('if [[ -z "${POSTGRES_PROOF_TARGETS:-}" ]]; then')
    target_override = runner.index('if [[ -n "${POSTGRES_PROOF_TARGETS:-}" ]]; then')
    assert default_guard > target_override
    proof_command = runner.index("--test db_node_conversations", default_guard)
    reset = runner.rfind("reset_database", default_guard, proof_command)
    assert reset != -1, "the Push proof must run against a freshly reset disposable database"


def test_ci_delegates_jetstream_lifecycle_to_runner() -> None:
    workflow = yaml.safe_load(CI.read_text(encoding="utf-8"))
    job = workflow["jobs"]["postgres-proofs"]
    assert job["env"]["POSTGRES_PROOF_PROVISION_NATS"] == "1"
    assert job["env"]["POSTGRES_PROOF_ALLOW_RESET"] == "1"
    rendered = json.dumps(job, sort_keys=True)
    assert "weltgewebe-ci-nats" not in rendered
    assert "run-postgres-integration-proofs.sh" in rendered


def test_postgres_passkey_browser_proof_provisions_pinned_jetstream_for_readiness() -> None:
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    workflow = yaml.safe_load(AUTH_PASSKEY_PROOF.read_text(encoding="utf-8"))
    job = workflow["jobs"]["auth-passkey-register-proof"]
    assert job["env"]["AUTH_PASSKEY_PROOF_POSTGRES"] == "1"
    steps = job["steps"]
    start = next(
        step
        for step in steps
        if step.get("name") == "Start pinned JetStream for readiness-backed proof"
    )
    cleanup = next(
        step
        for step in steps
        if step.get("name") == "Stop JetStream proof container"
    )
    rendered = start["run"]
    assert "scripts/ci/postgres-proof-contract.json" in rendered
    assert "jetstream_image" in rendered
    assert "--publish 127.0.0.1::4222" in rendered
    assert '"${IMAGE}" -js' in rendered
    assert "NATS_URL=nats://127.0.0.1:%s" in rendered
    assert "PASSKEY_NATS_CONTAINER=%s" in rendered
    assert rendered.index("PASSKEY_NATS_CONTAINER=%s") < rendered.index("docker run --detach")
    assert contract["jetstream_image"].startswith("nats@sha256:")
    assert cleanup["if"] == "always()"
    assert "docker rm --force" in cleanup["run"]


def test_governance_browser_proof_provisions_pinned_jetstream_for_readiness() -> None:
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    workflow = yaml.safe_load(GOVERNANCE_FULL_FLOW_PROOF.read_text(encoding="utf-8"))
    job = workflow["jobs"]["governance-full-flow-proof"]
    steps = job["steps"]
    start = next(
        step
        for step in steps
        if step.get("name") == "Start pinned JetStream for readiness-backed proof"
    )
    cleanup = next(
        step
        for step in steps
        if step.get("name") == "Stop JetStream proof container"
    )
    rendered = start["run"]
    assert "scripts/ci/postgres-proof-contract.json" in rendered
    assert "jetstream_image" in rendered
    assert "--publish 127.0.0.1::4222" in rendered
    assert '"${IMAGE}" -js' in rendered
    assert "NATS_URL=nats://127.0.0.1:%s" in rendered
    assert "GOVERNANCE_NATS_CONTAINER=%s" in rendered
    assert rendered.index("GOVERNANCE_NATS_CONTAINER=%s") < rendered.index(
        "docker run --detach"
    )
    assert contract["jetstream_image"].startswith("nats@sha256:")
    assert cleanup["if"] == "always()"
    assert "docker rm --force" in cleanup["run"]


def test_runner_rejects_incidental_disposable_name_substrings() -> None:
    env = os.environ.copy()
    for database in ("social", "official", "citizen"):
        url = f"postgres://postgres:postgres@127.0.0.1:5432/{database}"
        env.update(
            DATABASE_URL=url,
            PG_DIRECT_URL=url,
            T005_DATABASE_URL=url,
        )
        completed = subprocess.run(
            [str(RUNNER)],
            cwd=REPO,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert completed.returncode != 0
        assert "refusing non-disposable database" in completed.stderr


def test_runner_requires_weltgewebe_prefix_and_final_disposable_segment() -> None:
    runner = RUNNER.read_text(encoding="utf-8")
    assert "database.startswith('weltgewebe_')" in runner
    assert "database_segments[-1] not in segments" in runner
    for database in ("proof_of_concept", "ci_test_db", "weltgewebe_ci_data"):
        url = f"postgres://postgres:postgres@127.0.0.1:5432/{database}"
        completed = _run_runner_with_urls(url, url, url)
        assert completed.returncode != 0
        assert "refusing non-disposable database" in completed.stderr
    for database in ("weltgewebe_proof", "weltgewebe_ci_proof", "weltgewebe_t002_test"):
        assert database.startswith("weltgewebe_")
        assert database.replace("-", "_").replace(".", "_").split("_")[-1] in {"test", "proof", "ci"}

def _run_runner_with_urls(database_url: str, direct_url: str, t005_url: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        DATABASE_URL=database_url,
        PG_DIRECT_URL=direct_url,
        T005_DATABASE_URL=t005_url,
        POSTGRES_PROOF_PREFLIGHT_ONLY="1",
    )
    return subprocess.run(
        [str(RUNNER)],
        cwd=REPO,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def test_runner_rejects_same_database_name_on_different_endpoints_before_preflight() -> None:
    completed = _run_runner_with_urls(
        "postgres://postgres:postgres@127.0.0.1:5432/weltgewebe_ci_proof",
        "postgres://postgres:postgres@127.0.0.1:55432/weltgewebe_ci_proof",
        "postgres://postgres:postgres@127.0.0.1:5432/weltgewebe_ci_proof",
    )
    assert completed.returncode != 0
    assert "must target the same direct endpoint" in completed.stderr


def test_runner_rejects_percent_encoded_database_path_before_preflight() -> None:
    encoded = "postgres://postgres:postgres@127.0.0.1:5432/weltgewebe%5Fci%5Fproof"
    completed = _run_runner_with_urls(encoded, encoded, encoded)
    assert completed.returncode != 0
    assert "must not contain percent-encoding" in completed.stderr


def test_reset_and_container_guards_are_fail_closed() -> None:
    runner = RUNNER.read_text(encoding="utf-8")
    assert 'POSTGRES_PROOF_ALLOW_RESET:-0' in runner
    assert 'reset refused for non-loopback host' in runner
    assert 'validate_reset_container_binding' in runner
    assert 'must expose exactly one 5432/tcp binding' in runner
    assert 'requires a loopback PG_DIRECT_URL' in runner
    assert 'does not match PG_DIRECT_URL port' in runner
    assert 'must expose exactly one 4222/tcp binding' in runner
    rust = RUST_SUPPORT.read_text(encoding="utf-8")
    assert "must not contain percent-encoding" in rust


def test_runner_binds_validator_failures_before_read_and_rejects_unsafe_identifiers() -> None:
    runner = RUNNER.read_text(encoding="utf-8")
    assert 'validated_database="$(validate_database_url "$DATABASE_URL")"' in runner
    assert 'validated_pg_direct="$(validate_database_url "$PG_DIRECT_URL")"' in runner
    assert 'validated_t005="$(validate_database_url "$T005_DATABASE_URL")"' in runner
    assert '<<< "$(validate_database_url' not in runner
    assert "re.fullmatch(r'[A-Za-z0-9_.-]+', database)" in runner
    unsafe = "postgres://postgres:postgres@127.0.0.1:5432/weltgewebe_ci_proof%22"
    completed = _run_runner_with_urls(unsafe, unsafe, unsafe)
    assert completed.returncode != 0
    assert "percent-encoding" in completed.stderr


def test_t003_loader_binds_python_exit_status_and_preserves_nul_records() -> None:
    runner = RUNNER.read_text(encoding="utf-8")
    assert 'tmp_file="$(mktemp)"' in runner
    assert 'if ! python3 - "$PG_DIRECT_URL" > "$tmp_file"' in runner
    assert "mapfile -d '' -t values < \"$tmp_file\"" in runner
    assert 'if ((${#values[@]} != 5)); then' in runner
    assert "mapfile -d '' -t values < <(" not in runner


def test_local_reset_uses_postgres_cli_arguments_not_interpolated_sql() -> None:
    runner = RUNNER.read_text(encoding="utf-8")
    assert 'dropdb --maintenance-db="$admin" --if-exists --force "$DATABASE_NAME"' in runner
    assert 'createdb --maintenance-db="$admin" "$DATABASE_NAME"' in runner
    assert 'DROP DATABASE IF EXISTS' not in runner
    assert 'CREATE DATABASE \"${DATABASE_NAME}\"' not in runner


def test_jetstream_retry_handles_expected_socket_failures_without_tracebacks() -> None:
    runner = RUNNER.read_text(encoding="utf-8")
    assert "except OSError:" in runner
    assert "except (UnicodeDecodeError, json.JSONDecodeError):" in runner
    assert "raise SystemExit(1)" in runner


def test_rust_support_rejects_unsafe_database_identifier_characters() -> None:
    rust = RUST_SUPPORT.read_text(encoding="utf-8")
    assert "character.is_ascii_alphanumeric()" in rust
    assert "matches!(character, '_' | '-' | '.')" in rust
    assert "unsafe disposable database identifier" in rust


class FederationDeliveryDbProofTest(unittest.TestCase):
    """The runner must name every ignored PostgreSQL unit test in federation_delivery.rs.

    `--test <target>` never reaches ignored unit tests in src/, so this list is
    the only path by which CI executes them.
    """

    def test_runner_runs_every_ignored_federation_delivery_db_test_by_name(self) -> None:
        source = (REPO / "apps/api/src/federation_delivery.rs").read_text(encoding="utf-8")
        ignored = set(
            re.findall(
                r'#\[ignore = "requires an isolated FEDERATION_TEST_DATABASE_URL"\]'
                r"(?:\s*#\[[^\]]+\])*\s*async fn (\w+)",
                source,
            )
        )
        self.assertEqual(len(ignored), 9)
        runner = RUNNER.read_text(encoding="utf-8")
        listed = re.search(r"federation_delivery_db_tests=\(\n(.*?)\n  \)", runner, re.S)
        self.assertIsNotNone(listed)
        self.assertEqual(set(listed.group(1).split()), ignored)

    def test_runner_fails_when_a_listed_test_did_not_run(self) -> None:
        runner = RUNNER.read_text(encoding="utf-8")
        block = runner[runner.index("federation_delivery_db_tests=(") :]
        self.assertIn("--lib federation_delivery::tests::postgres_", block)
        self.assertIn("-- --ignored --test-threads=1", block)
        self.assertIn('grep -Fxq "test federation_delivery::tests::${test_name} ... ok"', block)
        self.assertIn('"$federation_ran" -ne "${#federation_delivery_db_tests[@]}"', block)
        self.assertLess(block.index("reset_database"), block.index("cargo test"))
