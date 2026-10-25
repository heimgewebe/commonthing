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
require_cmd flock
BACKUP_AGE_RECIPIENTS_FILE="${BACKUP_AGE_RECIPIENTS_FILE-/etc/weltgewebe/postgres-backup-age-recipients.txt}"
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
# Serialize all producers using the same root-owned directory. The sequence is
# derived ONLY from completed local backup manifests (published after dumps),
# including when age encryption later fails. No independent counter store.
# This is not a second publication database and does not use wall-clock order.
# The lock covers the whole directory, so the scan does too: prefixes such as
# demo and demo-extra overlap in the readers' globs and must never share a
# sequence number.
[[ ! -L "$BACKUP_DIR/.age-publication.lock" ]] ||
  fail "unsafe age publication lock symlink"
exec 8> "$BACKUP_DIR/.age-publication.lock"
flock -n 8 || fail "another PostgreSQL backup producer is active"
published_manifests="$(find -P "$BACKUP_DIR" -mindepth 1 -maxdepth 1 \
  \( -type f -o -type l \) -name '*.sha256.manifest' \
  ! -name '*.age.sha256.manifest' -printf '%f\n')" ||
  fail "cannot scan existing local completion manifests"
max_publication_sequence=0
declare -A known_publication_sequences=()
while IFS= read -r prior_name; do
  [[ -n "$prior_name" ]] || continue
  [[ "$prior_name" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,249}$ && "$prior_name" != *..* ]] ||
    fail "unsafe existing local completion manifest name"
  prior_manifest="$BACKUP_DIR/$prior_name"
  [[ -f "$prior_manifest" && ! -L "$prior_manifest" ]] ||
    fail "existing local completion manifest is not regular"
  prior_sequence_record="$(awk -F= '$1 == "publication_sequence" { count++; seq=$2 }
    END { if (count > 1) exit 1; printf "%d:%s\n", count, seq }' "$prior_manifest")" ||
    fail "existing local completion manifest has duplicate publication_sequence"
  [[ "$prior_sequence_record" != "0:" ]] || continue
  [[ "$prior_sequence_record" =~ ^1:([1-9][0-9]{0,17})$ ]] ||
    fail "existing local completion manifest has invalid publication_sequence"
  prior_sequence="${BASH_REMATCH[1]}"
  [[ -z "${known_publication_sequences[$prior_sequence]:-}" ]] ||
    fail "duplicate publication sequence in existing local backups"
  known_publication_sequences["$prior_sequence"]="$prior_name"
  if ((10#$prior_sequence > max_publication_sequence)); then
    max_publication_sequence=$((10#$prior_sequence))
  fi
done <<< "$published_manifests"
((max_publication_sequence < 999999999999999999)) ||
  fail "encrypted publication sequence exhausted"
next_publication_sequence=$((max_publication_sequence + 1))
# The PostgreSQL data volume may share this filesystem. Do not start a new
# plaintext + ciphertext backup that would eat into a conservative recovery
# reserve. The next run is estimated from the largest previous local dump:
# x1.25 growth for the plaintext plus the same again for its ciphertext
# (factor 2.5). This is a preflight guard, not an atomic disk quota and not a
# retention policy: nothing is ever deleted to make room.
BACKUP_MIN_FREE_KIB="${BACKUP_MIN_FREE_KIB:-4194304}"
[[ "$BACKUP_MIN_FREE_KIB" =~ ^[1-9][0-9]{0,11}$ ]] ||
  fail "BACKUP_MIN_FREE_KIB must be a positive integer (at most 12 digits)"
require_cmd df
# Sets available_kib; fails in this shell (not a subshell) when unknown.
read_available_kib() {
  available_kib="$(LC_ALL=C df -Pk -- "$BACKUP_DIR" | awk 'NR==2 {print $4}')" ||
    fail "backup disk free space could not be determined"
  [[ "$available_kib" =~ ^[0-9]{1,15}$ ]] || fail "backup disk free space could not be determined"
}
previous_max_bytes="$(find "$BACKUP_DIR" -maxdepth 1 -type f -name "${BACKUP_PREFIX}-*.sql.gz" -printf '%s\n' |
  awk 'BEGIN {max = 0} $1 > max {max = $1} END {printf "%.0f\n", max}')" ||
  fail "previous backup size could not be determined"
[[ "$previous_max_bytes" =~ ^[0-9]{1,18}$ ]] || fail "previous backup size could not be determined"
previous_max_kib=$(((previous_max_bytes + 1023) / 1024))
estimated_kib=$(((previous_max_kib * 5 + 1) / 2))
required_kib=$((BACKUP_MIN_FREE_KIB + estimated_kib))
read_available_kib
((available_kib >= required_kib)) ||
  fail "insufficient free backup disk space: ${available_kib}KiB available, ${required_kib}KiB required (reserve ${BACKUP_MIN_FREE_KIB}KiB + estimated next backup ${estimated_kib}KiB from largest previous dump ${previous_max_kib}KiB)"
[[ "$(psql_db -Atc 'select 1' | tr -d '[:space:]')" == "1" ]] || fail "PostgreSQL health check failed"
check_required_tables

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
label="${BACKUP_LABEL:-$timestamp}"
case "$label" in '' | *[!A-Za-z0-9._-]*) fail "BACKUP_LABEL contains unsupported characters" ;; esac
# A label ending in .age would make the plaintext completion manifest
# (<base>.sha256.manifest) look like the age completion manifest of another
# backup (<other>.age.sha256.manifest) to every *.age.sha256.manifest scan.
case "$label" in *.age) fail "BACKUP_LABEL must not end in .age" ;; esac
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
  printf 'created_at=%s\npublication_sequence=%s\nfile=%s.sql.gz\nsha256=%s\nbytes=%s\n' "$created_at" "$next_publication_sequence" "$base" "$sha256" "$bytes"
  [[ -z "$git_commit" ]] || printf 'git_commit=%s\n' "$git_commit"
  printf 'tables=domain_accounts,domain_nodes,domain_edges,sessions,passkey_credentials,_sqlx_migrations\n'
} > "$tmp_manifest"
chmod 600 "$tmp_manifest"

# The host-local recovery pair is independent from the additional age off-host
# copy. Publish its dump first, then the digest manifest as completion marker,
# before checking age or its recipients. An off-host failure must remain a
# nonzero job result without losing the already completed local recovery.
# Same-filesystem hard links are create-only even if a target appears during
# the dump, protecting all older recovery points.
ln -T -- "$tmp_dump" "$dump_file" ||
  fail "backup target appeared during publication: $dump_file"
ln -T -- "$tmp_manifest" "$manifest_file" ||
  fail "backup target appeared during publication: $manifest_file"
printf 'PostgreSQL backup written: %s\nManifest written: %s\n' "$dump_file" "$manifest_file"

# Only now validate the independently optional off-host encryption layer.
# Its failure is explicit; no age completion manifest may be published.
require_cmd age
load_age_recipients "$BACKUP_AGE_RECIPIENTS_FILE"

# Re-check capacity with the measured dump size before writing the ciphertext
# (about the size of the dump plus age's per-chunk overhead), so encryption does
# not consume the reserve. The local recovery pair above is kept either way.
dump_kib=$(((bytes + 1023) / 1024))
encrypt_required_kib=$((BACKUP_MIN_FREE_KIB + dump_kib + dump_kib / 1000 + 64))
read_available_kib
((available_kib >= encrypt_required_kib)) ||
  fail "insufficient free backup disk space for the encrypted off-host copy: ${available_kib}KiB available, ${encrypt_required_kib}KiB required (reserve ${BACKUP_MIN_FREE_KIB}KiB + ciphertext of a ${dump_kib}KiB dump)"

# Off-host artefact: ciphertext only. The plaintext dump above stays on this host.
age "${age_recipient_args[@]}" -o "$tmp_age" "$tmp_dump" || fail "age encryption of the off-host copy failed"
chmod 600 "$tmp_age"
cmp -s -n "$((${#AGE_HEADER} + 1))" "$tmp_age" <(printf '%s\n' "$AGE_HEADER") || fail "encrypted off-host copy does not carry the age header"
age_sha256="$(sha256_file "$tmp_age")"
age_bytes="$(wc -c < "$tmp_age" | tr -d '[:space:]')"
{
  printf 'contract=weltgewebe-postgres-backup-age-v1\n'
  printf 'created_at=%s\npublication_sequence=%s\nfile=%s.sql.gz.age\nsha256=%s\nbytes=%s\n' "$created_at" "$next_publication_sequence" "$base" "$age_sha256" "$age_bytes"
  printf 'format=%s\nrecipients=%s\n' "$AGE_HEADER" "$age_recipients_csv"
  printf 'plaintext_file=%s.sql.gz\nplaintext_sha256=%s\n' "$base" "$sha256"
  [[ -z "$git_commit" ]] || printf 'git_commit=%s\n' "$git_commit"
} > "$tmp_age_manifest"
chmod 600 "$tmp_age_manifest"

# Off-host publication also uses create-only hard links. Its age manifest
# is published LAST and is the sole encrypted-set completion marker.
ln -T -- "$tmp_age" "$age_file" ||
  fail "backup target appeared during publication: $age_file"
ln -T -- "$tmp_age_manifest" "$age_manifest_file" ||
  fail "backup target appeared during publication: $age_manifest_file"

# The producer must not silently retire previously usable local recovery
# points. BACKUP_RETENTION_DAYS remains accepted for old unit compatibility,
# but neither plaintext, ciphertext nor either manifest is age-deleted here.
# Only a separately authorized retirement after a real offline-identity
# decrypt and isolated PostgreSQL restore may remove historic backups.
printf 'Encrypted off-host copy written: %s\nOff-host manifest written: %s\n' "$age_file" "$age_manifest_file"
