#!/usr/bin/env bash
set -euo pipefail
umask 077

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}
require_cmd() { command -v "$1" > /dev/null 2>&1 || fail "required command not found: $1"; }
require_cmd ssh
require_cmd sha256sum
require_cmd awk
require_cmd find
require_cmd wc
require_cmd cmp
require_cmd grep

REMOTE_HOST="${REMOTE_HOST:-commonserver}"
REMOTE_BACKUP_DIR="${REMOTE_BACKUP_DIR:-/var/backups/weltgewebe/postgres}"
BACKUP_PREFIX="${BACKUP_PREFIX:-weltgewebe-postgres}"
DEST_DIR="${DEST_DIR:-${HOME:?HOME must be set}/merges/weltgewebe-production-backups}"
LOCAL_RETENTION_DAYS="${LOCAL_RETENTION_DAYS:-30}"
case "$LOCAL_RETENTION_DAYS" in '' | *[!0-9]*) fail "LOCAL_RETENTION_DAYS must be a non-negative integer" ;; esac
case "$REMOTE_HOST" in '' | *[!A-Za-z0-9._-]*) fail "REMOTE_HOST contains unsupported characters" ;; esac
[[ "$REMOTE_BACKUP_DIR" == /var/backups/weltgewebe/* || "${ALLOW_TEST_REMOTE_BACKUP_DIR:-0}" == 1 ]] || fail "REMOTE_BACKUP_DIR must remain below /var/backups/weltgewebe"
case "$REMOTE_BACKUP_DIR" in
  *..* | *[!A-Za-z0-9._/-]*) fail "REMOTE_BACKUP_DIR contains unsupported characters" ;;
esac

ssh_args=(-o BatchMode=yes -o ConnectTimeout=15)
# Only the age-encrypted off-host artefact (*.sql.gz.age) is ever selected or
# transferred. The host-local plaintext dump never crosses the SSH link.
# Values below are validated locally; expansion on the client is intentional.
# shellcheck disable=SC2029
latest="$(ssh "${ssh_args[@]}" "$REMOTE_HOST" \
  "sudo -n find '$REMOTE_BACKUP_DIR' -maxdepth 1 -type f -name '${BACKUP_PREFIX}-*.sql.gz.age' -printf '%f\\n' | sort | tail -n1")"
case "$latest" in
  "${BACKUP_PREFIX}-"*.sql.gz.age) ;;
  *) fail "remote host returned no encrypted (*.sql.gz.age) backup filename" ;;
esac
[[ "$latest" =~ ^[A-Za-z0-9._-]+$ && "$latest" != *'..'* ]] || fail "unsafe remote backup filename"
base="${latest%.sql.gz.age}"
age_manifest="${base}.age.sha256.manifest"
manifest="${base}.sha256.manifest"
proof="${base}.restore-proof"

install -d -m 0700 "$DEST_DIR"
work_dir="$(mktemp -d "$DEST_DIR/.pull.XXXXXX")"
trap 'rm -rf "$work_dir"' EXIT

remote_cat() {
  local relative="$1" destination="$2"
  # shellcheck disable=SC2029
  ssh "${ssh_args[@]}" "$REMOTE_HOST" \
    "sudo -n cat -- '$REMOTE_BACKUP_DIR/$relative'" > "$destination"
  chmod 600 "$destination"
}
manifest_value() { awk -F= -v key="$1" '$1==key {print substr($0, length(key) + 2); exit}' "$2"; }
remote_cat "$latest" "$work_dir/$latest"
remote_cat "$age_manifest" "$work_dir/$age_manifest"
# Metadata only (file name, plaintext sha256, size, tables); it lets an
# operator verify the decrypted dump and run postgres-restore-proof.sh unchanged.
remote_cat "$manifest" "$work_dir/$manifest"

grep -qx 'contract=weltgewebe-postgres-backup-age-v1' "$work_dir/$age_manifest" || fail "off-host manifest contract is not weltgewebe-postgres-backup-age-v1"
[[ "$(manifest_value file "$work_dir/$age_manifest")" == "$latest" ]] || fail "off-host manifest does not describe $latest"
expected="$(manifest_value sha256 "$work_dir/$age_manifest")"
actual="$(sha256sum "$work_dir/$latest" | awk '{print $1}')"
[[ "$expected" =~ ^[0-9a-f]{64}$ && "$actual" == "$expected" ]] || fail "off-host backup ciphertext sha256 mismatch"
expected_bytes="$(manifest_value bytes "$work_dir/$age_manifest")"
actual_bytes="$(wc -c < "$work_dir/$latest" | tr -d '[:space:]')"
[[ "$expected_bytes" =~ ^[0-9]+$ && "$actual_bytes" == "$expected_bytes" ]] || fail "off-host backup ciphertext size mismatch"
age_header='age-encryption.org/v1'
cmp -s -n "$((${#age_header} + 1))" "$work_dir/$latest" <(printf '%s\n' "$age_header") || fail "off-host backup is not an age-encrypted file"

grep -qx 'contract=weltgewebe-postgres-backup-v1' "$work_dir/$manifest" || fail "plaintext metadata manifest contract is not supported"
plaintext_sha="$(manifest_value plaintext_sha256 "$work_dir/$age_manifest")"
[[ "$plaintext_sha" =~ ^[0-9a-f]{64}$ ]] || fail "off-host manifest plaintext_sha256 is invalid"
[[ "$(manifest_value plaintext_file "$work_dir/$age_manifest")" == "${base}.sql.gz" ]] || fail "off-host and plaintext metadata manifests disagree"
[[ "$(manifest_value file "$work_dir/$manifest")" == "${base}.sql.gz" ]] || fail "off-host and plaintext metadata manifests disagree"
[[ "$(manifest_value sha256 "$work_dir/$manifest")" == "$plaintext_sha" ]] || fail "off-host and plaintext metadata manifests disagree"

mv -f "$work_dir/$latest" "$DEST_DIR/$latest"
mv -f "$work_dir/$manifest" "$DEST_DIR/$manifest"
mv -f "$work_dir/$age_manifest" "$DEST_DIR/$age_manifest"

# A weekly restore proof may not yet exist for the newest daily backup. Copy it
# when present, but never treat its absence as proof that the backup is invalid.
# shellcheck disable=SC2029
if ssh "${ssh_args[@]}" "$REMOTE_HOST" \
  "sudo -n test -f '$REMOTE_BACKUP_DIR/proofs/$proof'"; then
  # shellcheck disable=SC2029
  ssh "${ssh_args[@]}" "$REMOTE_HOST" \
    "sudo -n cat -- '$REMOTE_BACKUP_DIR/proofs/$proof'" > "$work_dir/$proof"
  chmod 600 "$work_dir/$proof"
  grep -qx 'result=ok' "$work_dir/$proof" || fail "remote restore proof is not successful"
  mv -f "$work_dir/$proof" "$DEST_DIR/$proof"
fi

receipt="$DEST_DIR/latest-pull.receipt"
receipt_tmp="$work_dir/latest-pull.receipt"
{
  printf 'contract=weltgewebe-offhost-backup-pull-v2\n'
  printf 'pulled_at=%s\nremote_host=%s\nfile=%s\nsha256=%s\nencryption=age\nplaintext_sha256=%s\nresult=ok\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$REMOTE_HOST" "$latest" "$actual" "$plaintext_sha"
} > "$receipt_tmp"
chmod 600 "$receipt_tmp"
mv -f "$receipt_tmp" "$receipt"

# *.sql.gz only matches legacy plaintext copies pulled before the age contract;
# retention keeps expiring them, but operators should delete them right away.
if ((LOCAL_RETENTION_DAYS > 0)); then
  find "$DEST_DIR" -maxdepth 1 -type f \
    \( -name "${BACKUP_PREFIX}-*.sql.gz.age" -o -name "${BACKUP_PREFIX}-*.sql.gz" -o -name "${BACKUP_PREFIX}-*.sha256.manifest" -o -name "${BACKUP_PREFIX}-*.restore-proof" \) \
    -mtime +"$LOCAL_RETENTION_DAYS" -delete
fi
printf 'Encrypted off-host PostgreSQL backup copied and verified: %s\n' "$DEST_DIR/$latest"
