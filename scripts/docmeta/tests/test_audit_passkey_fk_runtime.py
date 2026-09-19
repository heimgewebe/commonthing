import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from scripts.docmeta import audit_passkey_fk_runtime as audit  # noqa: E402


def test_hash_account_id_is_stable_and_redacted() -> None:
    first = audit.hash_account_id("account-a")
    second = audit.hash_account_id("account-a")

    assert first == second
    assert first.startswith("account:sha256:")
    assert "account-a" not in first
    assert len(first.rsplit(":", 1)[-1]) == 12


def test_redact_audit_removes_raw_account_ids_and_preserves_counts() -> None:
    raw = {
        "schema_version": 1,
        "audit": "passkey_fk_runtime",
        "read_only": True,
        "mutation_performed": False,
        "totals": {
            "credential_records_total": 3,
            "credential_account_ids_total": 2,
            "domain_accounts_total": 9,
            "credentials_with_account_total": 1,
            "orphan_credentials_total": 2,
            "orphan_account_ids_total": 1,
        },
        "orphan_samples": [
            {"account_id": "real-account-id", "credential_count": 2},
        ],
    }

    redacted = audit.redact_audit(raw, source_label="test-db", sample_limit=10)
    serialized = json.dumps(redacted, sort_keys=True)

    assert redacted["read_only"] is True
    assert redacted["mutation_performed"] is False
    assert redacted["totals"]["orphan_credentials_total"] == 2
    assert redacted["orphan_account_samples"] == [
        {
            "account_id_hash": audit.hash_account_id("real-account-id"),
            "credential_count": 2,
        }
    ]
    assert redacted["findings"] == {
        "fk_ready_by_count": False,
        "requires_runtime_review": True,
    }
    assert "real-account-id" not in serialized
    assert "real-credential-id" not in serialized


def test_redact_audit_marks_zero_orphans_fk_ready_by_count() -> None:
    raw = {
        "totals": {
            "credential_records_total": 1,
            "credential_account_ids_total": 1,
            "domain_accounts_total": 1,
            "credentials_with_account_total": 1,
            "orphan_credentials_total": 0,
            "orphan_account_ids_total": 0,
        },
        "orphan_samples": [],
    }

    redacted = audit.redact_audit(raw, source_label="test-db", sample_limit=10)

    assert redacted["findings"]["fk_ready_by_count"] is True
    assert redacted["findings"]["requires_runtime_review"] is False


def test_build_audit_sql_is_read_only_and_contains_no_mutation_statements() -> None:
    sql = audit.build_audit_sql(5)
    lowered = sql.lower()

    assert "begin transaction read only" in lowered
    assert "rollback" in lowered
    assert "passkey_credentials" in lowered
    assert "domain_accounts" in lowered
    assert "left join" in lowered
    for forbidden in [" insert ", " update ", " delete ", " drop ", " alter ", " create "]:
        assert forbidden not in lowered


def test_postgres_env_from_database_url_redacts_ambient_pg(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgres://ambient:bad@example.invalid/ambient")
    monkeypatch.setenv("PGPASSWORD", "ambient-secret")
    env = audit.postgres_env_from_database_url(
        "postgres://user:secret@db.example.invalid:5432/welt?sslmode=require&connect_timeout=5"
    )

    assert env["PGHOST"] == "db.example.invalid"
    assert env["PGPORT"] == "5432"
    assert env["PGUSER"] == "user"
    assert env["PGPASSWORD"] == "secret"
    assert env["PGDATABASE"] == "welt"
    assert env["PGSSLMODE"] == "require"
    assert env["PGCONNECT_TIMEOUT"] == "5"
    assert env["PGAPPNAME"] == "weltgewebe-passkey-fk-runtime-audit"
    assert env.get("DATABASE_URL") is None


def test_sanitize_psql_stderr_redacts_url_parts(monkeypatch) -> None:
    url = "postgres://user:secret@db.example.invalid:5432/welt"
    stderr = f"could not connect to {url}; password=secret; PGPASSWORD=secret"

    sanitized = audit.sanitize_psql_stderr(stderr, url)

    assert url not in sanitized
    assert "secret" not in sanitized
    assert "db.example.invalid" not in sanitized
    assert "<redacted>" in sanitized
