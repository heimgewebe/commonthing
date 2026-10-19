#!/usr/bin/env bash
set -euo pipefail

umask 077

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}
require_cmd() { command -v "$1" > /dev/null 2>&1 || fail "required command not found: $1"; }
sha256_file() {
  if command -v sha256sum > /dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  elif command -v shasum > /dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else fail "required command not found: sha256sum or shasum"; fi
}

POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-}"

psql_db() {
  if [[ -n "$POSTGRES_CONTAINER" ]]; then
    docker exec "$POSTGRES_CONTAINER" sh -c \
      'exec psql -X --no-psqlrc -v ON_ERROR_STOP=1 -U "${POSTGRES_USER:-postgres}" -d "${POSTGRES_DB:-postgres}" "$@"' sh "$@"
  elif [[ -n "${DATABASE_URL:-}" ]]; then
    psql -X --no-psqlrc -v ON_ERROR_STOP=1 "$DATABASE_URL" "$@"
  else
    psql -X --no-psqlrc -v ON_ERROR_STOP=1 "$@"
  fi
}

pg_dump_db() {
  if [[ -n "$POSTGRES_CONTAINER" ]]; then
    docker exec "$POSTGRES_CONTAINER" sh -c \
      'exec pg_dump --format=plain --no-owner --no-privileges -U "${POSTGRES_USER:-postgres}" "${POSTGRES_DB:-postgres}"'
  elif [[ -n "${DATABASE_URL:-}" ]]; then
    pg_dump --format=plain --no-owner --no-privileges --dbname="$DATABASE_URL"
  else
    pg_dump --format=plain --no-owner --no-privileges
  fi
}

check_connection_config() {
  if [[ -n "$POSTGRES_CONTAINER" ]]; then
    require_cmd docker
    docker inspect "$POSTGRES_CONTAINER" > /dev/null 2>&1 || fail "PostgreSQL container not found: $POSTGRES_CONTAINER"
    return
  fi
  require_cmd psql
  require_cmd pg_dump
  if [[ -z "${DATABASE_URL:-}" && (-z "${PGHOST:-}" || -z "${PGDATABASE:-}" || -z "${PGUSER:-}") ]]; then
    fail "set POSTGRES_CONTAINER, DATABASE_URL, or PGHOST/PGDATABASE/PGUSER"
  fi
}

check_required_tables() {
  local missing
  missing="$(psql_db -Atc "
with required(name) as (values
 ('domain_accounts'),('domain_nodes'),('domain_edges'),('sessions'),
 ('passkey_credentials'),('_sqlx_migrations')
), missing as (
 select required.name from required
 left join information_schema.tables t
   on t.table_schema='public' and t.table_name=required.name
 where t.table_name is null
)
select coalesce(string_agg(name, ',' order by name), '') from missing;
" | tr -d '[:space:]')"
  [[ -z "$missing" ]] || fail "database is missing required Weltgewebe tables: $missing"
}

# The off-host copy is always age ciphertext to public recipients only. The
# private identity stays offline and is never needed on this host.
AGE_HEADER='age-encryption.org/v1'
AGE_RECIPIENT_RE='^age1[02-9ac-hj-np-z]{58}$'
age_recipient_args=()
age_recipients_csv=""
load_age_recipients() {
  local file="$1" line
  [[ -n "$file" ]] || fail "BACKUP_AGE_RECIPIENTS_FILE must name the public age recipients file for the encrypted off-host copy"
  [[ -f "$file" && -r "$file" ]] || fail "age recipients file is missing or unreadable: $file (the off-host copy must be encrypted; see docs/runbooks/db-recovery.md)"
  if grep -q 'AGE-SECRET-KEY-' "$file"; then
    fail "age recipients file contains a private age identity; only public age1 recipients belong on this host: $file"
  fi
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [[ -z "$line" || "$line" == '#'* ]] && continue
    [[ "$line" =~ $AGE_RECIPIENT_RE ]] || fail "age recipients file contains an unsupported entry (expected one X25519 age1 recipient per line): $file"
    # The off-host pull rejects a manifest naming a recipient twice, so a
    # duplicate must fail here rather than leave every later pull red.
    [[ ",$age_recipients_csv," != *",$line,"* ]] || fail "age recipients file lists a recipient twice: $file"
    age_recipient_args+=(-r "$line")
    age_recipients_csv="${age_recipients_csv:+${age_recipients_csv},}${line}"
  done < "$file"
  ((${#age_recipient_args[@]} > 0)) || fail "age recipients file contains no age1 recipient: $file"
  # The pattern above cannot see a Bech32 checksum typo. Let age parse every
  # recipient on an empty input now, before any plaintext dump exists.
  age "${age_recipient_args[@]}" -o /dev/null < /dev/null 2> /dev/null ||
    fail "age rejected a recipient in $file (check for typos; expected X25519 age1 public keys)"
}

require_cmd gzip
require_cmd awk
require_cmd wc
require_cmd find
require_cmd grep
require_cmd cmp
require_cmd age
BACKUP_AGE_RECIPIENTS_FILE="${BACKUP_AGE_RECIPIENTS_FILE-/etc/weltgewebe/postgres-backup-age-recipients.txt}"
load_age_recipients "$BACKUP_AGE_RECIPIENTS_FILE"
check_connection_config

BACKUP_DIR="${BACKUP_DIR:-/var/backups/weltgewebe/postgres}"
BACKUP_PREFIX="${BACKUP_PREFIX:-weltgewebe-postgres}"
# A path component used for all create-only backup artefacts must be canonical.
case "$BACKUP_PREFIX" in
  '' | *..* | *[!A-Za-z0-9._-]*) fail "BACKUP_PREFIX contains unsupported characters" ;;
esac
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
PGCONNECT_TIMEOUT="${PGCONNECT_TIMEOUT:-10}"
export PGCONNECT_TIMEOUT
case "$BACKUP_RETENTION_DAYS" in '' | *[!0-9]*) fail "BACKUP_RETENTION_DAYS must be a non-negative integer" ;; esac

install -d -m 0700 "$BACKUP_DIR"
[[ -w "$BACKUP_DIR" ]] || fail "backup directory is not writable: $BACKUP_DIR"
[[ "$(psql_db -Atc 'select 1' | tr -d '[:space:]')" == "1" ]] || fail "PostgreSQL health check failed"
check_required_tables

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
label="${BACKUP_LABEL:-$timestamp}"
case "$label" in '' | *[!A-Za-z0-9._-]*) fail "BACKUP_LABEL contains unsupported characters" ;; esac
base="${BACKUP_PREFIX}-${label}"
# The off-host reader (postgres-backup-read.sh) serves only names of this shape;
# a backup it cannot name would make every later off-host pull fail.
[[ "$base" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$ && "$base" != *..* ]] ||
  fail "backup name must start with a letter or digit, have at most 200 characters and no '..': $base"
dump_file="${BACKUP_DIR}/${base}.sql.gz"
manifest_file="${BACKUP_DIR}/${base}.sha256.manifest"
age_file="${BACKUP_DIR}/${base}.sql.gz.age"
age_manifest_file="${BACKUP_DIR}/${base}.age.sha256.manifest"
for target in "$dump_file" "$manifest_file" "$age_file" "$age_manifest_file"; do
  [[ ! -e "$target" && ! -L "$target" ]] || fail "backup target already exists for label: $label"
done

work_dir="$(mktemp -d "${BACKUP_DIR}/.tmp.${BACKUP_PREFIX}.XXXXXX")"
trap 'rm -rf "$work_dir"' EXIT
tmp_dump="${work_dir}/${base}.sql.gz"
tmp_manifest="${work_dir}/${base}.sha256.manifest"
tmp_age="${work_dir}/${base}.sql.gz.age"
tmp_age_manifest="${work_dir}/${base}.age.sha256.manifest"

pg_dump_db | gzip -c > "$tmp_dump" || fail "pg_dump or gzip failed"
chmod 600 "$tmp_dump"
gzip -t "$tmp_dump" || fail "gzip integrity test failed"
sha256="$(sha256_file "$tmp_dump")"
bytes="$(wc -c < "$tmp_dump" | tr -d '[:space:]')"
created_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
git_commit="${GIT_COMMIT_SHA:-}"
if [[ -z "$git_commit" && -n "${GIT_REPO_DIR:-}" ]]; then
  git_commit="$(git -c safe.directory="$GIT_REPO_DIR" -C "$GIT_REPO_DIR" rev-parse HEAD 2> /dev/null || true)"
fi
{
  printf 'contract=weltgewebe-postgres-backup-v1\n'
  printf 'created_at=%s\nfile=%s.sql.gz\nsha256=%s\nbytes=%s\n' "$created_at" "$base" "$sha256" "$bytes"
  [[ -z "$git_commit" ]] || printf 'git_commit=%s\n' "$git_commit"
  printf 'tables=domain_accounts,domain_nodes,domain_edges,sessions,passkey_credentials,_sqlx_migrations\n'
} > "$tmp_manifest"
chmod 600 "$tmp_manifest"

# Off-host artefact: ciphertext only. The plaintext dump above stays on this host.
age "${age_recipient_args[@]}" -o "$tmp_age" "$tmp_dump" || fail "age encryption of the off-host copy failed"
chmod 600 "$tmp_age"
cmp -s -n "$((${#AGE_HEADER} + 1))" "$tmp_age" <(printf '%s\n' "$AGE_HEADER") || fail "encrypted off-host copy does not carry the age header"
age_sha256="$(sha256_file "$tmp_age")"
age_bytes="$(wc -c < "$tmp_age" | tr -d '[:space:]')"
{
  printf 'contract=weltgewebe-postgres-backup-age-v1\n'
  printf 'created_at=%s\nfile=%s.sql.gz.age\nsha256=%s\nbytes=%s\n' "$created_at" "$base" "$age_sha256" "$age_bytes"
  printf 'format=%s\nrecipients=%s\n' "$AGE_HEADER" "$age_recipients_csv"
  printf 'plaintext_file=%s.sql.gz\nplaintext_sha256=%s\n' "$base" "$sha256"
  [[ -z "$git_commit" ]] || printf 'git_commit=%s\n' "$git_commit"
} > "$tmp_age_manifest"
chmod 600 "$tmp_age_manifest"

# Same-filesystem create-only links prevent another producer or symlink
# appearing after preflight from replacing any historical backup file.
# The off-host manifest is the final marker of a complete encrypted set.
ln -T -- "$tmp_dump" "$dump_file" ||
  fail "backup target appeared during publication: $dump_file"
ln -T -- "$tmp_age" "$age_file" ||
  fail "backup target appeared during publication: $age_file"
ln -T -- "$tmp_manifest" "$manifest_file" ||
  fail "backup target appeared during publication: $manifest_file"
ln -T -- "$tmp_age_manifest" "$age_manifest_file" ||
  fail "backup target appeared during publication: $age_manifest_file"

# The producer must not silently retire previously usable local recovery
# points. BACKUP_RETENTION_DAYS remains accepted for old unit compatibility,
# but neither plaintext, ciphertext nor either manifest is age-deleted here.
# Only a separately authorized retirement after a real offline-identity
# decrypt and isolated PostgreSQL restore may remove historic backups.
printf 'PostgreSQL backup written: %s\nManifest written: %s\n' "$dump_file" "$manifest_file"
printf 'Encrypted off-host copy written: %s\nOff-host manifest written: %s\n' "$age_file" "$age_manifest_file"
