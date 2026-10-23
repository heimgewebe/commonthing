#!/bin/bash -p
# Read-only interface for the off-host PostgreSQL backup pull on commonserver.
#
# The interpreter is absolute and privileged mode (-p) is set: it runs as root
# for a less privileged caller, so neither a caller PATH (without sudo
# secure_path) nor BASH_ENV, SHELLOPTS or exported functions may choose code.
#
# Installed root:root 0755 as /usr/local/libexec/weltgewebe/postgres-backup-read
# and granted to the pull account by the narrowly scoped inline sudoers rule
# installed as documented in docs/runbooks/db-recovery.md. The rule allows any
# argument list, so this script is the argument boundary: it accepts exactly the
# operations below, validates every name, reads only regular files without
# following symlinks, and has no operation that can name the plaintext dump.
#
#   latest PREFIX           newest PREFIX-*.age.sha256.manifest basename
#   read BASE KIND          KIND: ciphertext | age-manifest | manifest | proof
#   proof-state BASE        "present" or "absent" for proofs/BASE.restore-proof
set -euo pipefail
umask 077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
export LC_ALL=C

# Fixed at install time; sudo resets the environment, and neither value may be
# taken from the caller.
readonly BACKUP_DIR=/var/backups/weltgewebe/postgres
readonly BACKUP_OWNER_UID=0

fail() {
  printf 'postgres-backup-read: %s\n' "$*" >&2
  exit 2
}

# A trusted directory is a real directory owned by the backup owner and not
# writable by group or others, so nobody but that owner can plant entries.
require_trusted_dir() {
  local dir="$1" owner mode
  [[ -d "$dir" && ! -L "$dir" ]] || fail "not a directory: $dir"
  read -r owner mode < <(stat -c '%u %a' -- "$dir") || fail "cannot stat: $dir"
  [[ "$owner" == "$BACKUP_OWNER_UID" ]] || fail "unexpected owner of $dir"
  (((8#$mode & 8#022) == 0)) || fail "group or world writable: $dir"
}

valid_name() {
  [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$ && "$1" != *..* ]]
}

read_regular_file() {
  local path="$1"
  [[ -f "$path" && ! -L "$path" ]] || fail "not a regular file: ${path##*/}"
  # iflag=nofollow closes the window between the check and the open: a symlink
  # swapped in afterwards makes the open fail instead of being followed.
  dd if="$path" iflag=nofollow bs=1M status=none
}

(($# >= 1)) || fail "usage: latest PREFIX | read BASE KIND | proof-state BASE"
operation="$1"
shift
require_trusted_dir "$BACKUP_DIR"

case "$operation" in
  latest)
    (($# == 1)) || fail "latest takes exactly one PREFIX"
    valid_name "$1" || fail "invalid prefix"
    # A free-form manual BACKUP_LABEL can sort after every UTC daily name.
    # Ordering only by filename would then silently pin the off-host pull to
    # one old ciphertext forever. Compare the root-owned producer manifest's
    # canonical UTC creation time; break exact-second ties deterministically.
    candidates="$(find -P "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type f \
      -name "$1-*.age.sha256.manifest" -printf '%f\n')" ||
      fail "cannot list encrypted backup manifests"
    latest_name=""
    latest_created_at=""
    # The producer uses the host UTC clock. Impossible calendar dates or far-
    # future timestamps must not silently dominate subsequent daily backups.
    max_created_at="$(date -u -d '+5 minutes' +%Y-%m-%dT%H:%M:%SZ)" ||
      fail "cannot read current UTC"
    while IFS= read -r candidate; do
      [[ -n "$candidate" ]] || continue
      candidate_base="${candidate%.age.sha256.manifest}"
      valid_name "$candidate_base" || fail "invalid encrypted backup manifest name"
      created_at="$(
        read_regular_file "$BACKUP_DIR/$candidate" |
          awk -F= '$1 == "created_at" { count++; stamp=$2 }
            END { if (count != 1) exit 1; print stamp }'
      )" || fail "encrypted backup manifest has missing or duplicate created_at"
      [[ "$created_at" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] ||
        fail "encrypted backup manifest created_at is not canonical UTC"
      normalized="$(date -u -d "$created_at" +%Y-%m-%dT%H:%M:%SZ 2> /dev/null)" ||
        fail "encrypted backup manifest created_at is invalid UTC"
      [[ "$normalized" == "$created_at" ]] ||
        fail "encrypted backup manifest created_at is not canonical UTC"
      [[ "$created_at" > "$max_created_at" ]] &&
        fail "encrypted backup manifest created_at is in the future"
      if [[ -z "$latest_name" || "$created_at" > "$latest_created_at" ]] ||
        [[ "$created_at" == "$latest_created_at" && "$candidate" > "$latest_name" ]]; then
        latest_name="$candidate"
        latest_created_at="$created_at"
      fi
    done <<< "$candidates"
    [[ -z "$latest_name" ]] || printf '%s\n' "$latest_name"
    ;;
  read)
    (($# == 2)) || fail "read takes exactly BASE and KIND"
    valid_name "$1" || fail "invalid backup name"
    case "$2" in
      ciphertext) read_regular_file "$BACKUP_DIR/$1.sql.gz.age" ;;
      age-manifest) read_regular_file "$BACKUP_DIR/$1.age.sha256.manifest" ;;
      manifest) read_regular_file "$BACKUP_DIR/$1.sha256.manifest" ;;
      proof)
        require_trusted_dir "$BACKUP_DIR/proofs"
        read_regular_file "$BACKUP_DIR/proofs/$1.restore-proof"
        ;;
      *) fail "unsupported kind" ;;
    esac
    ;;
  proof-state)
    (($# == 1)) || fail "proof-state takes exactly one BASE"
    valid_name "$1" || fail "invalid backup name"
    proofs="$BACKUP_DIR/proofs"
    if [[ ! -e "$proofs" && ! -L "$proofs" ]]; then
      echo absent
      exit 0
    fi
    require_trusted_dir "$proofs"
    proof="$proofs/$1.restore-proof"
    if [[ -f "$proof" && ! -L "$proof" ]]; then
      echo present
    elif [[ ! -e "$proof" && ! -L "$proof" ]]; then
      echo absent
    else
      fail "restore proof is not a regular file"
    fi
    ;;
  *) fail "unsupported operation" ;;
esac
