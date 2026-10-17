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
require_cmd flock
require_cmd realpath

REMOTE_HOST="${REMOTE_HOST:-commonserver}"
# Every remote read goes through one root-owned, argument-validating reader
# (scripts/ops/postgres-backup-read.sh); sudo grants nothing else. The backup
# directory is fixed inside that reader, not chosen by this client.
REMOTE_BACKUP_READER="${REMOTE_BACKUP_READER:-/usr/local/libexec/weltgewebe/postgres-backup-read}"
BACKUP_PREFIX="${BACKUP_PREFIX:-weltgewebe-postgres}"
DEST_DIR="${DEST_DIR:-${HOME:?HOME must be set}/merges/weltgewebe-production-backups}"
LOCAL_RETENTION_DAYS="${LOCAL_RETENTION_DAYS:-30}"
PULL_MODE="${PULL_MODE:-normal}"
# Public age recipients the pull accepts, pinned on this host independently of
# commonserver's BACKUP_AGE_RECIPIENTS_FILE. A backup encrypted to any other
# key is refused: the offline identity could not decrypt it, or a foreign key
# could.
PULL_AGE_RECIPIENTS_FILE="${PULL_AGE_RECIPIENTS_FILE-${HOME:?HOME must be set}/.config/weltgewebe/postgres-backup-age-recipients.txt}"
case "$PULL_MODE" in
  normal | migration-stage) ;;
  *) fail "PULL_MODE must be normal or migration-stage" ;;
esac
case "$LOCAL_RETENTION_DAYS" in '' | *[!0-9]*) fail "LOCAL_RETENTION_DAYS must be a non-negative integer" ;; esac
# BACKUP_PREFIX enters a remotely evaluated SSH shell command. Treat it as
# untrusted, like REMOTE_HOST and REMOTE_BACKUP_READER, before making any SSH call.
case "$BACKUP_PREFIX" in
  '' | *..* | *[!A-Za-z0-9._-]*) fail "BACKUP_PREFIX contains unsupported characters" ;;
esac
case "$REMOTE_HOST" in '' | *[!A-Za-z0-9._-]*) fail "REMOTE_HOST contains unsupported characters" ;; esac
# The reader fixes the directory; a leftover override must not be silently ignored.
[[ -z "${REMOTE_BACKUP_DIR+x}" ]] || fail "REMOTE_BACKUP_DIR is no longer supported; the remote reader fixes the backup directory"
[[ "$REMOTE_BACKUP_READER" == /usr/local/libexec/weltgewebe/postgres-backup-read || "${ALLOW_TEST_REMOTE_BACKUP_READER:-0}" == 1 ]] ||
  fail "REMOTE_BACKUP_READER must be /usr/local/libexec/weltgewebe/postgres-backup-read"
case "$REMOTE_BACKUP_READER" in
  /*) ;;
  *) fail "REMOTE_BACKUP_READER must be an absolute path" ;;
esac
case "$REMOTE_BACKUP_READER" in
  *..* | *[!A-Za-z0-9._/-]*) fail "REMOTE_BACKUP_READER contains unsupported characters" ;;
esac

AGE_RECIPIENT_RE='^age1[02-9ac-hj-np-z]{58}$'
pinned_recipients=()
[[ -n "$PULL_AGE_RECIPIENTS_FILE" ]] || fail "PULL_AGE_RECIPIENTS_FILE must name the pinned public age recipients file"
[[ -f "$PULL_AGE_RECIPIENTS_FILE" && -r "$PULL_AGE_RECIPIENTS_FILE" ]] ||
  fail "pinned age recipients file is missing or unreadable: $PULL_AGE_RECIPIENTS_FILE (see docs/runbooks/db-recovery.md)"
if grep -q 'AGE-SECRET-KEY-' "$PULL_AGE_RECIPIENTS_FILE"; then
  fail "pinned age recipients file contains a private age identity; only public age1 recipients belong there: $PULL_AGE_RECIPIENTS_FILE"
fi
while IFS= read -r line || [[ -n "$line" ]]; do
  line="${line#"${line%%[![:space:]]*}"}"
  line="${line%"${line##*[![:space:]]}"}"
  [[ -z "$line" || "$line" == '#'* ]] && continue
  [[ "$line" =~ $AGE_RECIPIENT_RE ]] || fail "pinned age recipients file contains an unsupported entry (expected one X25519 age1 recipient per line): $PULL_AGE_RECIPIENTS_FILE"
  pinned_recipients+=("$line")
done < "$PULL_AGE_RECIPIENTS_FILE"
((${#pinned_recipients[@]} > 0)) || fail "pinned age recipients file contains no age1 recipient: $PULL_AGE_RECIPIENTS_FILE"

ssh_args=(-o BatchMode=yes -o ConnectTimeout=15)
# Only the age-encrypted off-host artefact (*.sql.gz.age) is ever selected or
# transferred. The host-local plaintext dump never crosses the SSH link.
# Candidates come from the off-host manifest, which postgres-backup.sh publishes
# last: an interrupted run leaves ciphertext without it and is never selected.
# Values below are validated locally; expansion on the client is intentional.
# shellcheck disable=SC2029
age_manifest="$(ssh "${ssh_args[@]}" "$REMOTE_HOST" \
  "sudo -n '$REMOTE_BACKUP_READER' latest '$BACKUP_PREFIX'")" ||
  fail "could not list remote backups (SSH, sudo or reader failed)"
case "$age_manifest" in
  "${BACKUP_PREFIX}-"*.age.sha256.manifest) ;;
  *) fail "remote host returned no complete encrypted backup set (*.age.sha256.manifest)" ;;
esac
[[ "$age_manifest" =~ ^[A-Za-z0-9._-]+$ && "$age_manifest" != *'..'* ]] || fail "unsafe remote backup filename"
base="${age_manifest%.age.sha256.manifest}"
latest="${base}.sql.gz.age"
manifest="${base}.sha256.manifest"
proof="${base}.restore-proof"

install -d -m 0700 "$DEST_DIR"
# GNU find -P does not descend into a symlink operand. Keep the entire
# lock, plaintext scan, publication and retention on one resolved directory,
# including when a configured destination is an existing symlink.
DEST_DIR="$(realpath -e -- "$DEST_DIR")" ||
  fail "off-host destination cannot be resolved"
# One pull at a time per destination. A legacy (pre-age) pull does not take
# this lock, so the plaintext scan is repeated right before the receipt.
exec 9> "$DEST_DIR/.pull.lock"
flock -n 9 || fail "another pull holds $DEST_DIR/.pull.lock"
assert_no_legacy_plaintext() {
  local legacy_plaintext
  legacy_plaintext="$(find "$DEST_DIR" \( -type f -o -type l \) -name '*.sql.gz' -print -quit)" ||
    fail "could not scan the off-host destination for legacy plaintext"
  [[ -z "$legacy_plaintext" ]] ||
    fail "legacy plaintext PostgreSQL backups remain in off-host destination; verify an offline restore and retire them deliberately before age-only pull"
}
# Only the explicit migration mode may transfer ciphertext while legacy dumps
# still exist. Its output is isolated from the Restic source and has NO success
# receipt: only an offline decrypt plus actual restore can authorize retirement.
if [[ "$PULL_MODE" == migration-stage ]]; then
  [[ -n "${MIGRATION_STAGE_DIR:-}" ]] || fail "MIGRATION_STAGE_DIR is required for migration-stage"
  require_cmd realpath
  target_dir="$(realpath -m -- "$MIGRATION_STAGE_DIR")"
  dest_real="$(realpath -e -- "$DEST_DIR")"
  # The Heim-PC's live restic-backup-1930 source is artifacts/merges, not
  # necessarily ~/merges. Exclude BOTH roots, repos and fundus.
  excluded_sources=("$dest_real")
  for excluded in "${HOME:?HOME must be set}/merges" "$HOME/artifacts/merges" "$HOME/repos" "$HOME/.local/share/schauwerk/fundus"; do
    excluded_sources+=("$(realpath -m -- "$excluded")")
  done
  for excluded in "${excluded_sources[@]}"; do
    case "$target_dir" in
      "$excluded" | "$excluded"/*)
        fail "migration staging must remain outside the normal backup destination and Restic sources"
        ;;
    esac
  done
  case "$dest_real" in
    "$target_dir"/*) fail "migration staging must remain outside the normal backup destination" ;;
  esac
  [[ ! -L "$target_dir" ]] || fail "migration staging cannot be a symlink"
  install -d -m 0700 -- "$target_dir"
  [[ "$(realpath -e -- "$target_dir")" == "$target_dir" ]] || fail "migration staging target changed during preflight"
  [[ "$(stat -c %a -- "$target_dir")" == 700 ]] || fail "migration staging must have mode 0700"
  # The DEST_DIR lock does not cover a staging directory shared by pulls with
  # different destinations; serialize on the staging directory itself.
  exec 7> "$target_dir/.stage.lock"
  flock -n 7 || fail "another migration staging holds $target_dir/.stage.lock"
else
  # Do not certify an age-only off-host state while legacy plaintext can still
  # be re-snapshotted. Old recovery files are NEVER deleted implicitly. The
  # check ignores BACKUP_PREFIX: a changed prefix must not hide dumps written
  # under an earlier one. It is recursive so that plaintext left in an
  # abandoned .pull.* work directory of an interrupted legacy pull counts too.
  assert_no_legacy_plaintext
  target_dir="$DEST_DIR"
fi
work_dir="$(mktemp -d "$target_dir/.pull.XXXXXX")"
trap 'rm -rf "$work_dir"' EXIT

remote_read() {
  local kind="$1" destination="$2"
  # shellcheck disable=SC2029
  ssh "${ssh_args[@]}" "$REMOTE_HOST" \
    "sudo -n '$REMOTE_BACKUP_READER' read '$base' '$kind'" > "$destination"
  chmod 600 "$destination"
}
manifest_value() { awk -F= -v key="$1" '$1==key {print substr($0, length(key) + 2); exit}' "$2"; }
remote_read ciphertext "$work_dir/$latest"
remote_read age-manifest "$work_dir/$age_manifest"
# Metadata only (file name, plaintext sha256, size, tables); it lets an
# operator verify the decrypted dump and run postgres-restore-proof.sh unchanged.
remote_read manifest "$work_dir/$manifest"

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

# The manifest's recipients line is the producer's claim; X25519 stanzas do not
# reveal recipients. This catches a misconfigured or swapped producer key, not a
# compromised producer.
# Exactly one canonical declaration: no repeated field, no empty, duplicate or
# malformed member, so the receipt never records an ambiguous value.
[[ "$(grep -c '^recipients=' "$work_dir/$age_manifest")" == 1 ]] ||
  fail "off-host manifest must declare age recipients exactly once"
manifest_recipients="$(manifest_value recipients "$work_dir/$age_manifest")"
recipient_list_re='^age1[02-9ac-hj-np-z]{58}(,age1[02-9ac-hj-np-z]{58})*$'
[[ "$manifest_recipients" =~ $recipient_list_re ]] ||
  fail "off-host manifest recipients are not a canonical comma-separated age1 list"
IFS=, read -r -a backup_recipients <<< "$manifest_recipients"
declare -A seen_recipients=()
for backup_recipient in "${backup_recipients[@]}"; do
  [[ -z "${seen_recipients[$backup_recipient]:-}" ]] ||
    fail "off-host manifest lists an age recipient twice: $backup_recipient"
  seen_recipients[$backup_recipient]=1
  pinned=0
  for candidate in "${pinned_recipients[@]}"; do
    [[ "$backup_recipient" == "$candidate" ]] && pinned=1
  done
  ((pinned == 1)) || fail "off-host backup is encrypted to an age recipient that is not pinned in $PULL_AGE_RECIPIENTS_FILE: $backup_recipient"
done

grep -qx 'contract=weltgewebe-postgres-backup-v1' "$work_dir/$manifest" || fail "plaintext metadata manifest contract is not supported"
plaintext_sha="$(manifest_value plaintext_sha256 "$work_dir/$age_manifest")"
[[ "$plaintext_sha" =~ ^[0-9a-f]{64}$ ]] || fail "off-host manifest plaintext_sha256 is invalid"
[[ "$(manifest_value plaintext_file "$work_dir/$age_manifest")" == "${base}.sql.gz" ]] || fail "off-host and plaintext metadata manifests disagree"
[[ "$(manifest_value file "$work_dir/$manifest")" == "${base}.sql.gz" ]] || fail "off-host and plaintext metadata manifests disagree"
[[ "$(manifest_value sha256 "$work_dir/$manifest")" == "$plaintext_sha" ]] || fail "off-host and plaintext metadata manifests disagree"

# Stage everything first. A malformed weekly proof must not leave
# newly published ciphertext/manifests behind on a failed pull.
# A weekly restore proof may not yet exist for the newest daily backup. Copy it
# when present, but never treat its absence as proof that the backup is invalid.
# Only a definite "absent" from the reader may skip the proof. An SSH, sudo or
# reader failure (for example a symlinked proof) fails the pull instead of
# silently dropping restore evidence.
# shellcheck disable=SC2029
proof_state="$(ssh "${ssh_args[@]}" "$REMOTE_HOST" \
  "sudo -n '$REMOTE_BACKUP_READER' proof-state '$base'")" ||
  fail "could not probe the remote restore proof (SSH, sudo or reader failed)"
case "$proof_state" in
  present | absent) ;;
  *) fail "unexpected remote restore proof probe result" ;;
esac
if [[ "$proof_state" == present ]]; then
  remote_read proof "$work_dir/$proof"
  grep -qx 'result=ok' "$work_dir/$proof" || fail "remote restore proof is not successful"
fi

# Staging is a create-only encrypted transfer, not a recovery proof.
# It never touches DEST_DIR's legacy contents or emits an age completion receipt.
if [[ "$PULL_MODE" == migration-stage ]]; then
  for name in "$latest" "$manifest" "$age_manifest" "$proof"; do
    [[ ! -e "$target_dir/$name" && ! -L "$target_dir/$name" ]] ||
      fail "migration staging artefact already exists: $name"
  done
  # Hard links are create-only: an artefact that appeared anyway is never
  # replaced. The work directory (and its links) is removed on exit.
  staged=("$latest" "$manifest" "$age_manifest")
  [[ ! -f "$work_dir/$proof" ]] || staged+=("$proof")
  for name in "${staged[@]}"; do
    ln -- "$work_dir/$name" "$target_dir/$name" ||
      fail "migration staging artefact already exists: $name"
  done
  printf 'Encrypted migration candidate staged (NOT recovery-verified; no completion receipt): %s\n' "$target_dir/$latest"
  exit 0
fi

# The receipt remains the final completion marker for the complete set.
# Core ciphertext and manifests are immutable: existing files are accepted
# only as a full three-file set with byte-identical verified remote contents.
core=("$latest" "$manifest" "$age_manifest")
staged=()
present_core=0
for name in "${core[@]}"; do
  local_path="$DEST_DIR/$name"
  [[ ! -L "$local_path" ]] ||
    fail "normal pull unsafe existing artefact: $name"
  if [[ -e "$local_path" ]]; then
    [[ -f "$local_path" ]] ||
      fail "normal pull unsafe existing artefact: $name"
    cmp -s -- "$work_dir/$name" "$local_path" ||
      fail "normal pull artefact content differs: $name"
    ((present_core += 1))
  else
    staged+=("$name")
  fi
done
((present_core == 0 || present_core == 3)) ||
  fail "normal pull incomplete core artefact set"

# An immutable history receipt seals a published basename; a partial
# transfer or post-retention reappearance cannot quietly reuse that name.
[[ ! -L "$DEST_DIR/receipts" ]] ||
  fail "normal pull unsafe existing receipts directory"
if [[ -e "$DEST_DIR/receipts" ]]; then
  [[ -d "$DEST_DIR/receipts" ]] ||
    fail "normal pull unsafe existing receipts directory"
fi
history_present=0
latest_history=''
for historical in "$DEST_DIR/receipts/${base}.pulled-"*.receipt; do
  [[ -e "$historical" || -L "$historical" ]] || continue
  [[ -f "$historical" && ! -L "$historical" ]] ||
    fail "normal pull unsafe existing history receipt"
  for binding in \
    'contract=weltgewebe-offhost-backup-pull-v2' \
    "remote_host=$REMOTE_HOST" "file=$latest" "sha256=$actual" \
    'encryption=age' "recipients=$manifest_recipients" \
    "plaintext_sha256=$plaintext_sha" 'result=ok'; do
    grep -qxF -- "$binding" "$historical" ||
      fail "normal pull existing history receipt differs: ${binding%%=*}"
  done
  history_present=1
  if [[ -z "$latest_history" || "$historical" > "$latest_history" ]]; then
    latest_history="$historical"
  fi
done
if ((present_core == 3)); then
  ((history_present == 1)) ||
    fail "normal pull existing core has no historical completion receipt"
elif ((history_present == 1)); then
  fail "normal pull historical receipt exists without core artefacts"
fi

# An optional weekly proof can arrive after the daily core was sealed.
# A previously published proof may not be overwritten, even on retry.
if [[ -L "$DEST_DIR/$proof" ]]; then
  fail "normal pull unsafe existing artefact: $proof"
elif [[ -e "$DEST_DIR/$proof" ]]; then
  [[ -f "$DEST_DIR/$proof" ]] ||
    fail "normal pull unsafe existing artefact: $proof"
  ((present_core == 3)) ||
    fail "normal pull incomplete core artefact set"
  [[ -f "$work_dir/$proof" ]] ||
    fail "normal pull local proof is absent upstream: $proof"
  cmp -s -- "$work_dir/$proof" "$DEST_DIR/$proof" ||
    fail "normal pull artefact content differs: $proof"
elif [[ -f "$work_dir/$proof" ]]; then
  staged+=("$proof")
fi

# A complete unchanged set is a true no-op, not a new history event.
assert_no_legacy_plaintext
if ((${#staged[@]} == 0)); then
  # A crash can occur between the create-only history link and the mutable
  # latest pointer rename. Repair it from the newest verified immutable
  # history receipt without creating a second historical completion record.
  [[ -n "$latest_history" ]] ||
    fail "normal pull existing core has no historical completion receipt"
  receipt="$DEST_DIR/latest-pull.receipt"
  [[ ! -L "$receipt" ]] ||
    fail "normal pull unsafe latest receipt pointer"
  [[ ! -e "$receipt" || -f "$receipt" ]] ||
    fail "normal pull unsafe latest receipt pointer"
  repair_pointer=0
  if [[ ! -e "$receipt" ]]; then
    repair_pointer=1
  elif cmp -s -- "$latest_history" "$receipt"; then
    :
  else
    cmp_status=$?
    ((cmp_status == 1)) ||
      fail "normal pull could not compare latest receipt"
    repair_pointer=1
  fi
  if ((repair_pointer)); then
    cp -- "$latest_history" "$work_dir/latest-pull.repaired.receipt" ||
      fail "normal pull could not stage latest receipt recovery"
    chmod 600 "$work_dir/latest-pull.repaired.receipt"
    mv -f -- "$work_dir/latest-pull.repaired.receipt" "$receipt" ||
      fail "normal pull could not restore latest receipt"
  fi
  printf 'Encrypted off-host PostgreSQL backup already verified: %s\n' "$DEST_DIR/$latest"
  exit 0
fi
# Hardlinks preserve create-only semantics even if a competitor creates
# a path after the complete preflight under the destination lock.
for name in "${staged[@]}"; do
  ln -T -- "$work_dir/$name" "$DEST_DIR/$name" ||
    fail "normal pull artefact appeared during publication: $name"
done
# Re-check under the lock: plaintext created while this pull ran (e.g. by a
# legacy pull) must not end up beside an age success receipt.
assert_no_legacy_plaintext

# Every completed pull leaves an immutable receipt under receipts/; the
# hard link fails rather than replace an existing one. latest-pull.receipt is
# only a pointer to the newest of them.
receipt="$DEST_DIR/latest-pull.receipt"
receipt_tmp="$work_dir/latest-pull.receipt"
pulled_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
{
  printf 'contract=weltgewebe-offhost-backup-pull-v2\n'
  printf 'pulled_at=%s\nremote_host=%s\nfile=%s\nsha256=%s\nencryption=age\nrecipients=%s\nplaintext_sha256=%s\nresult=ok\n' \
    "$pulled_at" "$REMOTE_HOST" "$latest" "$actual" "$manifest_recipients" "$plaintext_sha"
} > "$receipt_tmp"
chmod 600 "$receipt_tmp"
install -d -m 0700 "$DEST_DIR/receipts"
history_receipt="$DEST_DIR/receipts/${base}.pulled-$(date -u +%Y%m%dT%H%M%S.%NZ).receipt"
cp -- "$receipt_tmp" "$work_dir/history.receipt"
ln -- "$work_dir/history.receipt" "$history_receipt" || fail "pull receipt already exists: $history_receipt"
mv -f "$receipt_tmp" "$receipt"

# Retention never deletes legacy plaintext recovery points. Their retirement
# needs a separately verified age restore and an explicit operator decision.
if ((LOCAL_RETENTION_DAYS > 0)); then
  find "$DEST_DIR" -maxdepth 1 -type f \
    \( -name "${BACKUP_PREFIX}-*.sql.gz.age" -o -name "${BACKUP_PREFIX}-*.sha256.manifest" -o -name "${BACKUP_PREFIX}-*.restore-proof" \) \
    -mtime +"$LOCAL_RETENTION_DAYS" -delete
fi
printf 'Encrypted off-host PostgreSQL backup copied and verified: %s\n' "$DEST_DIR/$latest"
