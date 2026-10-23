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

# An unavailable or invalid off-host age recipient must NOT discard local
# recoverability: the dump and its digest manifest remain, while the producer
# returns failure and never certifies a complete encrypted off-host copy.
expect_offhost_failure_with_local_recovery() {
  local label="$1" recipients_file="$2" message="$3"
  local stderr_file="$TEMP_DIR/stderr-$1"
  if run_backup "$label" "$recipients_file" > /dev/null 2> "$stderr_file"; then
    echo "off-host encryption must report failure: $label" >&2
    exit 1
  fi
  grep -qF "$message" "$stderr_file" || {
    echo "unexpected off-host failure output for $label:" >&2
    cat "$stderr_file" >&2
    exit 1
  }
  local local_dump="$BACKUP_DIR/weltgewebe-postgres-${label}.sql.gz"
  local local_manifest="$BACKUP_DIR/weltgewebe-postgres-${label}.sha256.manifest"
  [[ -f "$local_dump" && ! -L "$local_dump" && -f "$local_manifest" && ! -L "$local_manifest" ]] || {
    echo "age failure lost the required local dump or manifest: $label" >&2
    exit 1
  }
  gzip -t "$local_dump"
  grep -qx "sha256=$(sha256sum "$local_dump" | awk '{print $1}')" "$local_manifest"
  grep -qx "file=weltgewebe-postgres-${label}.sql.gz" "$local_manifest"
  [[ "$(stat -c %a "$local_dump")" == 600 && "$(stat -c %a "$local_manifest")" == 600 ]]
  for suffix in .sql.gz.age .age.sha256.manifest; do
    [[ ! -e "$BACKUP_DIR/weltgewebe-postgres-${label}${suffix}" && ! -L "$BACKUP_DIR/weltgewebe-postgres-${label}${suffix}" ]] || {
      echo "age failure incorrectly published an off-host artefact: $label$suffix" >&2
      exit 1
    }
  done
}

# A full filesystem must fail before pg_dump creates any plaintext or age output.
# Keep old recovery points unchanged; reject the new run instead.
BACKUP_MIN_FREE_KIB=not-a-number expect_backup_failure invalid-capacity "$RECIPIENTS_FILE" 'BACKUP_MIN_FREE_KIB must be a positive integer'
BACKUP_MIN_FREE_KIB=999999999999 INJECT_BACKUP_COLLISION="$TEMP_DIR/disk-guard-pg-dump-ran" \
  expect_backup_failure low-disk "$RECIPIENTS_FILE" 'insufficient free backup disk space'
[[ ! -e "$TEMP_DIR/disk-guard-pg-dump-ran" ]] || {
  echo 'low free disk space reached pg_dump despite preflight' >&2
  exit 1
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

# Historical-backup fixture: all four expired artefacts must remain intact.
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
# A synthetic producer run must not destroy an earlier recovery candidate.
# Until the replacement was actually restored with the offline identity, even
# an expired and uncertified dump may be the last usable PostgreSQL snapshot.
for suffix in .sql.gz .sql.gz.age .sha256.manifest .age.sha256.manifest; do
  [[ -f "$BACKUP_DIR/weltgewebe-postgres-expired${suffix}" ]] || {
    echo "producer auto-retention destroyed an old backup artefact: $suffix" >&2
    exit 1
  }
done

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

# Fail closed for off-host ciphertext, but retain the local recovery pair.
expect_offhost_failure_with_local_recovery missing "$TEMP_DIR/does-not-exist.txt" 'age recipients file is missing or unreadable'
expect_offhost_failure_with_local_recovery unset "" 'BACKUP_AGE_RECIPIENTS_FILE must name the public age recipients file'
printf '# no recipient yet\n\n' > "$TEMP_DIR/empty-recipients.txt"
expect_offhost_failure_with_local_recovery empty "$TEMP_DIR/empty-recipients.txt" 'contains no age1 recipient'
printf 'ssh-ed25519 AAAAC3Nza fixture\n' > "$TEMP_DIR/foreign-recipients.txt"
expect_offhost_failure_with_local_recovery foreign "$TEMP_DIR/foreign-recipients.txt" 'unsupported entry'
printf '%s\nAGE-SECRET-KEY-1FIXTURE\n' "$AGE_RECIPIENT" > "$TEMP_DIR/secret-recipients.txt"
expect_offhost_failure_with_local_recovery secret "$TEMP_DIR/secret-recipients.txt" 'contains a private age identity'
printf '%s\n# same key again\n  %s\n' "$AGE_RECIPIENT" "$AGE_RECIPIENT" > "$TEMP_DIR/duplicate-recipients.txt"
expect_offhost_failure_with_local_recovery duplicate "$TEMP_DIR/duplicate-recipients.txt" 'lists a recipient twice'
if ((REAL_AGE)); then
  # A one-character typo keeps the age1 shape but breaks the Bech32 checksum;
  # off-host failure must leave the local dump and integrity manifest usable.
  last="${AGE_RECIPIENT: -1}"
  typo="q"
  [[ "$last" != q ]] || typo="p"
  printf '%s%s\n' "${AGE_RECIPIENT%?}" "$typo" > "$TEMP_DIR/typo-recipients.txt"
  expect_offhost_failure_with_local_recovery typo "$TEMP_DIR/typo-recipients.txt" 'age rejected a recipient'
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

# Weekly proof must select by completion-manifest created_at, never by
# the lexically highest manual BACKUP_LABEL. This test never uses real Docker.
weekly_dir="$TEMP_DIR/weekly-selector"
weekly_bin="$weekly_dir/bin"
weekly_backups="$weekly_dir/backups"
weekly_selected="$weekly_dir/selected"
mkdir -p "$weekly_bin" "$weekly_backups" "$weekly_dir/proofs"
cp "$REPO_ROOT/scripts/ops/postgres-restore-latest-proof.sh" "$weekly_bin/postgres-restore-latest-proof.sh"
cat > "$weekly_bin/docker" << 'SH'
#!/usr/bin/env bash
set -euo pipefail
case "$1" in
  run) printf 'synthetic-disposable-container\n' ;;
  exec) [[ "$3" == pg_isready ]] ;;
  rm) exit 0 ;;
  *) echo "mock docker: unsupported operation $1" >&2; exit 1 ;;
esac
SH
cat > "$weekly_bin/postgres-restore-proof.sh" << 'SH'
#!/usr/bin/env bash
set -euo pipefail
[[ -f "$BACKUP_FILE" && -f "$BACKUP_MANIFEST" ]] || exit 4
basename "$BACKUP_FILE" > "$WEEKLY_SELECTED_LOG"
SH
chmod 755 "$weekly_bin/docker" "$weekly_bin/postgres-restore-proof.sh"
daily_base=weltgewebe-postgres-20260801T040000Z
manual_base=weltgewebe-postgres-pre-upgrade
for weekly_base in "$daily_base" "$manual_base"; do
  printf 'synthetic dump\n' > "$weekly_backups/$weekly_base.sql.gz"
done
weekly_manifest() {
  local label="$1" stamp="$2"
  printf 'contract=weltgewebe-postgres-backup-v1\ncreated_at=%s\nfile=%s.sql.gz\nsha256=%064d\n' \
    "$stamp" "$label" 0 > "$weekly_backups/$label.sha256.manifest"
}
weekly_run() {
  WEEKLY_SELECTED_LOG="$weekly_selected" \
    PATH="$weekly_bin:$PATH" BACKUP_DIR="$weekly_backups" \
    BACKUP_PREFIX=weltgewebe-postgres RESTORE_PROOF_DIR="$weekly_dir/proofs" \
    bash "$weekly_bin/postgres-restore-latest-proof.sh" > /dev/null
}
weekly_manifest "$daily_base" 2026-08-01T04:00:00Z
weekly_manifest "$manual_base" 2026-07-31T04:00:00Z
# The encrypted completion marker is NOT a plaintext restore candidate.
printf 'created_at=2999-01-01T00:00:00Z\n' > "$weekly_backups/$manual_base.age.sha256.manifest"
weekly_run
if [[ "$(cat "$weekly_selected")" != "$daily_base.sql.gz" ]]; then
  echo "weekly restore selected stale manual label instead of daily created_at" >&2
  exit 1
fi
weekly_manifest "$manual_base" 2026-08-02T04:00:00Z
weekly_run
[[ "$(cat "$weekly_selected")" == "$manual_base.sql.gz" ]] || {
  echo "weekly restore ignored actually newer manual backup" >&2
  exit 1
}
weekly_manifest "$manual_base" 2026-02-30T04:00:00Z
if weekly_run > /dev/null 2> "$weekly_dir/error"; then
  echo "weekly restore accepted impossible calendar date" >&2
  exit 1
fi
grep -q 'invalid UTC' "$weekly_dir/error"
weekly_manifest "$manual_base" 2999-01-01T04:00:00Z
if weekly_run > /dev/null 2> "$weekly_dir/error"; then
  echo "weekly restore accepted future timestamp" >&2
  exit 1
fi
grep -q 'in the future' "$weekly_dir/error"
printf 'contract=weltgewebe-postgres-backup-v1\ncreated_at=2026-07-31T04:00:00Z\ncreated_at=2026-08-02T04:00:00Z\nfile=%s.sql.gz\n' \
  "$manual_base" > "$weekly_backups/$manual_base.sha256.manifest"
if weekly_run > /dev/null 2> "$weekly_dir/error"; then
  echo "weekly restore accepted duplicate created_at" >&2
  exit 1
fi
grep -q 'missing or duplicate' "$weekly_dir/error"
weekly_manifest "$manual_base" 2026-07-31T04:00:00Z
mv "$weekly_backups/$manual_base.sql.gz" "$weekly_dir/orphan.sql.gz"
if weekly_run > /dev/null 2> "$weekly_dir/error"; then
  echo "weekly restore accepted a manifest with missing dump" >&2
  exit 1
fi
grep -q 'incomplete backup pair' "$weekly_dir/error"
mv "$weekly_dir/orphan.sql.gz" "$weekly_backups/$manual_base.sql.gz"
weekly_run
[[ "$(cat "$weekly_selected")" == "$daily_base.sql.gz" ]] || {
  echo "weekly restore failed after correcting invalid manifest" >&2
  exit 1
}

if ((REAL_AGE == 1)); then
  echo "PASS: postgres backup and restore-proof contract (real age round trip)"
else
  echo "PASS: postgres backup and restore-proof contract (age stub; round trip skipped)"
fi
