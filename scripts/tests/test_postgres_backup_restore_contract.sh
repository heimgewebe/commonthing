#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" > /dev/null 2>&1 && pwd)"
REPO_ROOT="$(dirname "$(dirname "$SCRIPT_DIR")")"
BACKUP_SCRIPT="$REPO_ROOT/scripts/ops/postgres-backup.sh"
RESTORE_SCRIPT="$REPO_ROOT/scripts/ops/postgres-restore-proof.sh"
BACKUP_UNIT="$REPO_ROOT/scripts/ops/systemd/weltgewebe-postgres-backup.service"
PREPARE_UNIT="$REPO_ROOT/scripts/ops/systemd/weltgewebe-postgres-backup-prepare.service"

grep -qxF 'Requires=docker.service weltgewebe-postgres-backup-prepare.service' "$BACKUP_UNIT"
grep -qxF 'After=docker.service weltgewebe-postgres-backup-prepare.service' "$BACKUP_UNIT"
grep -qxF 'ReadWritePaths=/var/backups/weltgewebe/postgres' "$BACKUP_UNIT"
grep -qxF 'Environment=BACKUP_AGE_RECIPIENTS_FILE=/etc/weltgewebe/postgres-backup-age-recipients.txt' "$BACKUP_UNIT"
if grep -q '^ExecStartPre=' "$BACKUP_UNIT" || grep -qxF 'ReadWritePaths=/var/backups' "$BACKUP_UNIT"; then
  echo "backup service must not prepare or write the broad backup parent" >&2
  exit 1
fi
grep -qxF 'Before=weltgewebe-postgres-backup.service' "$PREPARE_UNIT"
grep -qxF 'AssertPathIsDirectory=/var/backups' "$PREPARE_UNIT"
grep -qxF 'ExecStart=/usr/bin/install -d -o root -g root -m 0750 /var/backups/weltgewebe' "$PREPARE_UNIT"
grep -qxF 'ExecStart=/usr/bin/install -d -o root -g root -m 0700 /var/backups/weltgewebe/postgres' "$PREPARE_UNIT"
grep -qxF 'ProtectSystem=full' "$PREPARE_UNIT"
grep -qxF 'ReadOnlyPaths=/var' "$PREPARE_UNIT"
grep -qxF 'ReadWritePaths=/var/backups' "$PREPARE_UNIT"
grep -qxF 'CapabilityBoundingSet=CAP_CHOWN CAP_DAC_OVERRIDE CAP_FOWNER' "$PREPARE_UNIT"

TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TEMP_DIR"' EXIT

MOCK_BIN="$TEMP_DIR/bin"
mkdir -p "$MOCK_BIN"

cat > "$MOCK_BIN/psql" << 'SH'
#!/usr/bin/env bash
set -euo pipefail
args="$*"
if [[ "$args" == *"select 1"* ]]; then
  echo "1"
elif [[ "$args" == *"count(*)"* ]]; then
  echo "0"
elif [[ "$args" == *"string_agg"* ]]; then
  echo ""
else
  cat >/dev/null || true
fi
SH
chmod +x "$MOCK_BIN/psql"

cat > "$MOCK_BIN/pg_dump" << 'SH'
#!/usr/bin/env bash
set -euo pipefail
# Test-only collision injected after producer preflight and before publication.
if [[ -n "${INJECT_BACKUP_COLLISION:-}" ]]; then
  printf 'immutable producer collision sentinel\n' > "$INJECT_BACKUP_COLLISION"
fi
cat <<'SQL'
CREATE TABLE domain_accounts (id text primary key);
CREATE TABLE domain_nodes (id text primary key);
CREATE TABLE domain_edges (id text primary key);
CREATE TABLE sessions (id text primary key);
CREATE TABLE passkey_credentials (credential_id bytea primary key);
CREATE TABLE _sqlx_migrations (version bigint primary key);
SQL
SH
chmod +x "$MOCK_BIN/pg_dump"

# Prefer the real age binary; otherwise a stub that only emits an age-shaped
# file (no cryptography). The round trip below runs only with real age.
REAL_AGE=0
if [[ "${WELTGEWEBE_TEST_FORCE_AGE_STUB:-0}" != 1 ]] && command -v age > /dev/null 2>&1 && command -v age-keygen > /dev/null 2>&1; then
  REAL_AGE=1
  age-keygen -o "$TEMP_DIR/offline-identity.txt" 2> /dev/null
  AGE_RECIPIENT="$(age-keygen -y "$TEMP_DIR/offline-identity.txt")"
else
  [[ "${WELTGEWEBE_TEST_REQUIRE_REAL_AGE:-0}" != 1 ]] || {
    echo "real age/age-keygen required but not found" >&2
    exit 1
  }
  AGE_RECIPIENT="age1$(printf 'q%.0s' $(seq 1 58))"
  cat > "$MOCK_BIN/age" << 'SH'
#!/usr/bin/env bash
set -euo pipefail
out="" recipients=0
while (($# > 1)); do
  case "$1" in
    -r) recipients=$((recipients + 1)); shift 2 ;;
    -o) out="$2"; shift 2 ;;
    *) echo "age stub: unsupported argument $1" >&2; exit 2 ;;
  esac
done
((recipients > 0)) && [[ -n "$out" ]] || { echo "age stub: need -r and -o" >&2; exit 2; }
# Read the named input, or stdin like real age (recipient preflight).
{ printf 'age-encryption.org/v1\n-> X25519 stub\n--- stub\n'; base64 < "${1:-/dev/stdin}"; } > "$out"
SH
  chmod +x "$MOCK_BIN/age"
fi
RECIPIENTS_FILE="$TEMP_DIR/age-recipients.txt"
printf '# public offline-backup recipient\n\n%s\n' "$AGE_RECIPIENT" > "$RECIPIENTS_FILE"

export PATH="$MOCK_BIN:$PATH"

BACKUP_DIR="$TEMP_DIR/backups"
RESTORE_PROOF_DIR="$TEMP_DIR/proofs"

run_backup() {
  local label="$1" recipients_file="$2"
  DATABASE_URL="postgres://welt:secret@db:5432/weltgewebe" \
    BACKUP_DIR="$BACKUP_DIR" \
    BACKUP_LABEL="$label" \
    BACKUP_RETENTION_DAYS=1 \
    BACKUP_AGE_RECIPIENTS_FILE="$recipients_file" \
    bash "$BACKUP_SCRIPT"
}
expect_backup_failure() {
  local label="$1" recipients_file="$2" message="$3" stderr_file="$TEMP_DIR/stderr-$1"
  if run_backup "$label" "$recipients_file" > /dev/null 2> "$stderr_file"; then
    echo "backup should fail closed: $label" >&2
    exit 1
  fi
  grep -qF "$message" "$stderr_file" || {
    echo "unexpected failure output for $label:" >&2
    cat "$stderr_file" >&2
    exit 1
  }
  if compgen -G "$BACKUP_DIR/weltgewebe-postgres-${label}.*" > /dev/null; then
    echo "fail-closed backup must not leave any artefact: $label" >&2
    exit 1
  fi
}

# The producer must reject unsafe filename prefixes before creating any
# plaintext or ciphertext artefact, even with otherwise valid DB credentials.
if BACKUP_PREFIX="unsafe'prefix" run_backup unsafe-prefix "$RECIPIENTS_FILE" \
  > /dev/null 2> "$TEMP_DIR/unsafe-prefix.stderr"; then
  echo 'untrusted BACKUP_PREFIX must fail closed' >&2
  exit 1
fi
grep -qF 'BACKUP_PREFIX contains unsupported characters' "$TEMP_DIR/unsafe-prefix.stderr" || {
  echo 'producer did not reject an unsafe prefix' >&2
  exit 1
}

# Every produced name must be one the off-host reader accepts, or the pull
# would fail on it forever.
name_rule='backup name must start with a letter or digit'
expect_backup_failure "$(printf 'a%.0s' {1..190})" "$RECIPIENTS_FILE" "$name_rule"
expect_backup_failure 'dot..dot' "$RECIPIENTS_FILE" "$name_rule"
if BACKUP_PREFIX=-lead run_backup leading-dash "$RECIPIENTS_FILE" \
  > /dev/null 2> "$TEMP_DIR/leading-dash.stderr"; then
  echo 'a prefix the reader rejects must fail closed' >&2
  exit 1
fi
grep -qF "$name_rule" "$TEMP_DIR/leading-dash.stderr" || {
  echo 'producer did not reject a name the reader cannot serve' >&2
  exit 1
}
if compgen -G "$BACKUP_DIR/-lead-*" > /dev/null; then
  echo 'rejected name must not leave any artefact' >&2
  exit 1
fi

# Retention fixture: expired artefacts of every backup kind.
mkdir -p "$BACKUP_DIR"
for suffix in .sql.gz .sql.gz.age .sha256.manifest .age.sha256.manifest; do
  touch -d '10 days ago' "$BACKUP_DIR/weltgewebe-postgres-expired${suffix}"
done

run_backup fixture "$RECIPIENTS_FILE" > /dev/null

# A target appearing after the pre-dump collision check must never be
# overwritten by the producer. This injects it while pg_dump runs.
collision_target="$BACKUP_DIR/weltgewebe-postgres-collision.sql.gz"
if INJECT_BACKUP_COLLISION="$collision_target" run_backup collision "$RECIPIENTS_FILE" \
  > /dev/null 2> "$TEMP_DIR/collision.stderr"; then
  echo 'producer overwrote a same-label backup artefact during publication' >&2
  exit 1
fi
printf 'immutable producer collision sentinel\n' > "$TEMP_DIR/immutable-sentinel"
cmp -s "$TEMP_DIR/immutable-sentinel" "$collision_target" || {
  echo 'producer replaced the collision sentinel' >&2
  exit 1
}
[[ ! -e "$BACKUP_DIR/weltgewebe-postgres-collision.sql.gz.age" ]] || {
  echo 'producer published an age file after collision failure' >&2
  exit 1
}
[[ ! -e "$BACKUP_DIR/weltgewebe-postgres-collision.age.sha256.manifest" ]] || {
  echo 'producer published a completion manifest after collision failure' >&2
  exit 1
}

backup_file="$BACKUP_DIR/weltgewebe-postgres-fixture.sql.gz"
manifest_file="$BACKUP_DIR/weltgewebe-postgres-fixture.sha256.manifest"
age_file="$BACKUP_DIR/weltgewebe-postgres-fixture.sql.gz.age"
age_manifest_file="$BACKUP_DIR/weltgewebe-postgres-fixture.age.sha256.manifest"

test -f "$backup_file"
test -f "$manifest_file"
test -f "$age_file"
test -f "$age_manifest_file"
gzip -t "$backup_file"
grep -q '^contract=weltgewebe-postgres-backup-v1$' "$manifest_file"
grep -q '^sha256=' "$manifest_file"

for artefact in "$backup_file" "$manifest_file" "$age_file" "$age_manifest_file"; do
  mode="$(stat -c '%a' "$artefact")"
  if [[ "$mode" != "600" ]]; then
    echo "backup artefact mode should be 600, got $mode for $artefact" >&2
    exit 1
  fi
done
if compgen -G "$BACKUP_DIR/weltgewebe-postgres-expired*" > /dev/null; then
  echo "retention must expire plaintext, ciphertext and both manifest kinds" >&2
  exit 1
fi

# Off-host artefact: age ciphertext bound to a manifest over the ciphertext.
cmp -s -n 22 "$age_file" <(printf 'age-encryption.org/v1\n')
if cmp -s "$age_file" "$backup_file" || gzip -t "$age_file" 2> /dev/null; then
  echo "off-host artefact must not be the plaintext gzip dump" >&2
  exit 1
fi
grep -qx 'contract=weltgewebe-postgres-backup-age-v1' "$age_manifest_file"
grep -qx 'file=weltgewebe-postgres-fixture.sql.gz.age' "$age_manifest_file"
grep -qx "sha256=$(sha256sum "$age_file" | awk '{print $1}')" "$age_manifest_file"
grep -qx "bytes=$(wc -c < "$age_file" | tr -d '[:space:]')" "$age_manifest_file"
grep -qx "recipients=$AGE_RECIPIENT" "$age_manifest_file"
grep -qx 'plaintext_file=weltgewebe-postgres-fixture.sql.gz' "$age_manifest_file"
grep -qx "plaintext_sha256=$(sha256sum "$backup_file" | awk '{print $1}')" "$age_manifest_file"
grep -qx "$(grep '^sha256=' "$manifest_file" | sed 's/^/plaintext_/')" "$age_manifest_file"

if ((REAL_AGE == 1)); then
  # Offline restore path: only the offline identity can decrypt the copy.
  age -d -i "$TEMP_DIR/offline-identity.txt" -o "$TEMP_DIR/decrypted.sql.gz" "$age_file"
  cmp -s "$TEMP_DIR/decrypted.sql.gz" "$backup_file" || {
    echo "age round trip did not restore identical bytes" >&2
    exit 1
  }
  wrong_identity="$TEMP_DIR/wrong-identity.txt"
  age-keygen -o "$wrong_identity" 2> /dev/null
  if age -d -i "$wrong_identity" -o "$TEMP_DIR/wrong.sql.gz" "$age_file" 2> /dev/null; then
    echo "a foreign identity must not decrypt the off-host copy" >&2
    exit 1
  fi
fi

# Fail closed: no recipient configuration means no backup set at all.
expect_backup_failure missing "$TEMP_DIR/does-not-exist.txt" 'age recipients file is missing or unreadable'
expect_backup_failure unset "" 'BACKUP_AGE_RECIPIENTS_FILE must name the public age recipients file'
printf '# no recipient yet\n\n' > "$TEMP_DIR/empty-recipients.txt"
expect_backup_failure empty "$TEMP_DIR/empty-recipients.txt" 'contains no age1 recipient'
printf 'ssh-ed25519 AAAAC3Nza fixture\n' > "$TEMP_DIR/foreign-recipients.txt"
expect_backup_failure foreign "$TEMP_DIR/foreign-recipients.txt" 'unsupported entry'
printf '%s\nAGE-SECRET-KEY-1FIXTURE\n' "$AGE_RECIPIENT" > "$TEMP_DIR/secret-recipients.txt"
expect_backup_failure secret "$TEMP_DIR/secret-recipients.txt" 'contains a private age identity'
printf '%s\n# same key again\n  %s\n' "$AGE_RECIPIENT" "$AGE_RECIPIENT" > "$TEMP_DIR/duplicate-recipients.txt"
expect_backup_failure duplicate "$TEMP_DIR/duplicate-recipients.txt" 'lists a recipient twice'
if ((REAL_AGE)); then
  # A one-character typo keeps the age1 shape but breaks the Bech32 checksum;
  # it must fail before pg_dump, not after a full plaintext dump.
  last="${AGE_RECIPIENT: -1}"
  typo="q"
  [[ "$last" != q ]] || typo="p"
  printf '%s%s\n' "${AGE_RECIPIENT%?}" "$typo" > "$TEMP_DIR/typo-recipients.txt"
  expect_backup_failure typo "$TEMP_DIR/typo-recipients.txt" 'age rejected a recipient'
fi

BACKUP_FILE="$backup_file" \
  BACKUP_MANIFEST="$manifest_file" \
  RESTORE_DATABASE_URL="postgres://welt:secret@db:5432/weltgewebe_restore_proof" \
  RESTORE_PROOF_DIR="$RESTORE_PROOF_DIR" \
  bash "$RESTORE_SCRIPT" > /dev/null

proof_file="$RESTORE_PROOF_DIR/weltgewebe-postgres-fixture.restore-proof"
test -f "$proof_file"
grep -q '^contract=weltgewebe-postgres-restore-proof-v1$' "$proof_file"
grep -q '^result=ok$' "$proof_file"

mode="$(stat -c '%a' "$proof_file")"
if [[ "$mode" != "600" ]]; then
  echo "restore proof mode should be 600, got $mode" >&2
  exit 1
fi

if BACKUP_FILE="$backup_file" \
  BACKUP_MANIFEST="$manifest_file" \
  RESTORE_DATABASE_URL="postgres://welt:secret@db:5432/weltgewebe" \
  RESTORE_PROOF_DIR="$RESTORE_PROOF_DIR" \
  bash "$RESTORE_SCRIPT" > /dev/null 2>&1; then
  echo "restore proof should reject non-disposable target database" >&2
  exit 1
fi

if ((REAL_AGE == 1)); then
  echo "PASS: postgres backup and restore-proof contract (real age round trip)"
else
  echo "PASS: postgres backup and restore-proof contract (age stub; round trip skipped)"
fi
