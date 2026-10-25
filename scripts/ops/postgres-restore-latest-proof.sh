#!/usr/bin/env bash
set -euo pipefail
umask 077
fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}
require_cmd() { command -v "$1" > /dev/null 2>&1 || fail "required command not found: $1"; }
require_cmd docker
require_cmd gzip
require_cmd sha256sum
BACKUP_DIR="${BACKUP_DIR:-/var/backups/weltgewebe/postgres}"
BACKUP_PREFIX="${BACKUP_PREFIX:-weltgewebe-postgres}"
RESTORE_PROOF_DIR="${RESTORE_PROOF_DIR:-${BACKUP_DIR}/proofs}"
RESTORE_POSTGRES_IMAGE="${RESTORE_POSTGRES_IMAGE:-postgres:16@sha256:be01cf82fc7dbba824acf0a82e150b4b360f3ff93c6631d7844af431e841a95c}"
# The plaintext checksum manifest is published AFTER its dump, and remains a
# completion marker if optional age encryption subsequently fails.
# Free-form manual BACKUP_LABEL values may sort behind all daily UTC labels.
export LC_ALL=C
[[ -d "$BACKUP_DIR" && ! -L "$BACKUP_DIR" ]] ||
  fail "backup directory does not exist or is a symlink: $BACKUP_DIR"
[[ "$BACKUP_PREFIX" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$ && "$BACKUP_PREFIX" != *..* ]] ||
  fail "invalid backup prefix"
candidates="$(find -P "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type f \
  -name "${BACKUP_PREFIX}-*.sha256.manifest" ! -name '*.age.sha256.manifest' -printf '%f\n')" ||
  fail "cannot list backup completion manifests"
max_created_at="$(date -u -d '+5 minutes' +%Y-%m-%dT%H:%M:%SZ)" ||
  fail "cannot read current UTC"
latest_base=""
latest_name=""
latest_created_at=""
latest_sequence=""
declare -A seen_sequences=()
while IFS= read -r candidate; do
  [[ -n "$candidate" ]] || continue
  base="${candidate%.sha256.manifest}"
  [[ "$base" == "$BACKUP_PREFIX"-* && "$base" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$ && "$base" != *..* ]] ||
    fail "invalid backup completion manifest name"
  manifest_path="$BACKUP_DIR/$candidate"
  [[ -f "$manifest_path" && ! -L "$manifest_path" ]] ||
    fail "backup completion manifest is not a regular file"
  fields="$(dd if="$manifest_path" iflag=nofollow bs=1M status=none |
    awk -F= '
      $1 == "created_at" { created_count++; stamp=substr($0, 12) }
      $1 == "file" { file_count++; file=substr($0, 6) }
      END { if (created_count != 1 || file_count != 1) exit 1; print stamp "|" file }
    ')" || fail "backup manifest has missing or duplicate created_at/file"
  IFS='|' read -r created_at manifest_file <<< "$fields"
  [[ "$manifest_file" == "$base.sql.gz" ]] ||
    fail "backup completion manifest file mismatch"
  [[ "$created_at" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] ||
    fail "backup manifest created_at is not canonical UTC"
  normalized="$(date -u -d "$created_at" +%Y-%m-%dT%H:%M:%SZ 2> /dev/null)" ||
    fail "backup manifest created_at is invalid UTC"
  [[ "$created_at" == "$normalized" ]] ||
    fail "backup manifest created_at is not canonical UTC"
  if [[ "$created_at" > "$max_created_at" ]]; then
    fail "backup manifest created_at is in the future"
  fi
  [[ -f "$BACKUP_DIR/$base.sql.gz" && ! -L "$BACKUP_DIR/$base.sql.gz" ]] ||
    fail "incomplete backup pair: $base"
  # Local completion markers also carry a monotonic publication order.
  # Unlike age markers, they persist even when optional encryption fails.
  sequence_record="$(dd if="$manifest_path" iflag=nofollow bs=1M status=none |
    awk -F= '$1 == "publication_sequence" { count++; seq=$2 }
      END { if (count > 1) exit 1; printf "%d:%s\n", count, seq }'
  )" || fail "backup manifest has duplicate publication_sequence"
  if [[ "$sequence_record" != "0:" ]]; then
    [[ "$sequence_record" =~ ^1:([1-9][0-9]{0,17})$ ]] ||
      fail "backup manifest has invalid publication_sequence"
    candidate_sequence="${BASH_REMATCH[1]}"
    [[ -z "${seen_sequences[$candidate_sequence]:-}" ]] ||
      fail "duplicate publication sequence in completed local backups"
    seen_sequences["$candidate_sequence"]="$candidate"
    if [[ -z "$latest_sequence" ]] ||
      ((10#$candidate_sequence > 10#$latest_sequence)); then
      latest_sequence="$candidate_sequence"
      latest_base="$base"
      latest_name="$candidate"
      latest_created_at="$created_at"
    fi
  elif [[ -z "$latest_sequence" ]]; then
    # Historical local manifests have no sequence; retain created_at order
    # until a new sequence-bound completion manifest exists.
    if [[ -z "$latest_base" || "$created_at" > "$latest_created_at" ]] ||
      [[ "$created_at" == "$latest_created_at" && "$candidate" > "$latest_name" ]]; then
      latest_base="$base"
      latest_name="$candidate"
      latest_created_at="$created_at"
    fi
  fi
done <<< "$candidates"
[[ -n "$latest_base" ]] || fail "no complete backup files found in $BACKUP_DIR"
latest="$BACKUP_DIR/$latest_base.sql.gz"
container="weltgewebe-restore-proof-$(date -u +%Y%m%d%H%M%S)-$$"
cleanup() { docker rm -f "$container" > /dev/null 2>&1 || true; }
trap cleanup EXIT

docker run -d --name "$container" --network none \
  --tmpfs /var/lib/postgresql/data:rw,nosuid,nodev,size=512m \
  -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=weltgewebe_restore_proof \
  "$RESTORE_POSTGRES_IMAGE" > /dev/null
for _ in $(seq 1 60); do
  if docker exec "$container" pg_isready -U postgres -d weltgewebe_restore_proof > /dev/null 2>&1; then break; fi
  sleep 1
done
docker exec "$container" pg_isready -U postgres -d weltgewebe_restore_proof > /dev/null || fail "disposable PostgreSQL did not become ready"
export BACKUP_FILE="$latest"
export BACKUP_MANIFEST="${latest%.sql.gz}.sha256.manifest"
export RESTORE_POSTGRES_CONTAINER="$container"
export RESTORE_PROOF_DIR
"$(dirname "$0")/postgres-restore-proof.sh"