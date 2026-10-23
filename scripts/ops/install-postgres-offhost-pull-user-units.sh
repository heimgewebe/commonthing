#!/usr/bin/env bash
set -euo pipefail
umask 077

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}
require_cmd() { command -v "$1" > /dev/null 2>&1 || fail "required command not found: $1"; }
sha256_file() { sha256sum "$1" | awk '{print $1}'; }
usage() {
  cat << 'USAGE'
Usage: install-postgres-offhost-pull-user-units.sh [--enable-now]

Install the pull program into an immutable commit-named user runtime directory,
generate the user service from its commit placeholder, reload the user manager,
and optionally enable the daily timer immediately.
USAGE
}

enable_now=0
case "$#" in
  0) ;;
  1)
    case "$1" in
      --enable-now) enable_now=1 ;;
      --help | -h)
        usage
        exit 0
        ;;
      *)
        usage >&2
        exit 2
        ;;
    esac
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

for cmd in git install mktemp cmp grep sed sha256sum awk date; do require_cmd "$cmd"; done

REPO_DIR="${REPO_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." > /dev/null 2>&1 && pwd)}"
TARGET_HOME="${TARGET_HOME:-${HOME:?HOME must be set}}"
SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-systemctl}"
[[ -d "$REPO_DIR/.git" || -f "$REPO_DIR/.git" ]] || fail "REPO_DIR must be a Git checkout"
[[ -d "$TARGET_HOME" && ! -L "$TARGET_HOME" ]] || fail "TARGET_HOME must be an existing real directory"

repo_commit="$(git -C "$REPO_DIR" rev-parse HEAD)"
expected_commit="${EXPECTED_COMMIT:-$repo_commit}"
[[ "$expected_commit" =~ ^[0-9a-f]{40}$ ]] || fail "EXPECTED_COMMIT must be a full 40-character Git commit"
[[ "$repo_commit" == "$expected_commit" ]] || fail "checkout HEAD does not match EXPECTED_COMMIT"

pull_rel=scripts/ops/pull-production-postgres-backup.sh
gate_rel=scripts/ops/restic-plaintext-gate.sh
service_rel=scripts/ops/systemd/weltgewebe-postgres-offhost-pull.service
timer_rel=scripts/ops/systemd/weltgewebe-postgres-offhost-pull.timer
for relative in "$pull_rel" "$gate_rel" "$service_rel" "$timer_rel"; do
  [[ -f "$REPO_DIR/$relative" && ! -L "$REPO_DIR/$relative" ]] || fail "required source file missing or unsafe: $relative"
  git -C "$REPO_DIR" ls-files --error-unmatch "$relative" > /dev/null 2>&1 || fail "required source file is not bound to Git: $relative"
done
git -C "$REPO_DIR" diff --quiet -- "$pull_rel" "$gate_rel" "$service_rel" "$timer_rel" || fail "off-host install sources contain uncommitted changes"
git -C "$REPO_DIR" diff --cached --quiet -- "$pull_rel" "$gate_rel" "$service_rel" "$timer_rel" || fail "off-host install sources contain staged changes"

token_count="$(awk '{ count += gsub(/@WELTGEWEBE_COMMIT@/, "&") } END { print count + 0 }' "$REPO_DIR/$service_rel")"
[[ "$token_count" == 1 ]] || fail "off-host service template must contain exactly one commit placeholder"
! grep -q '^ProtectKernelModules=' "$REPO_DIR/$service_rel" || fail "ProtectKernelModules is not portable in a user service"

release_dir="$TARGET_HOME/.local/libexec/weltgewebe/$expected_commit"
unit_dir="$TARGET_HOME/.config/systemd/user"
destination_dir="$TARGET_HOME/merges/weltgewebe-production-backups"
pull_target="$release_dir/pull-production-postgres-backup.sh"
# The Restic snapshot service runs this gate as ExecStartPre (see runbook).
gate_target="$release_dir/restic-plaintext-gate.sh"
receipt="$release_dir/install.receipt"
service_target="$unit_dir/weltgewebe-postgres-offhost-pull.service"
timer_target="$unit_dir/weltgewebe-postgres-offhost-pull.timer"

install -d -m 0755 "$release_dir" "$unit_dir"
install -d -m 0700 "$destination_dir"
if [[ -e "$pull_target" || -L "$pull_target" ]]; then
  [[ -f "$pull_target" && ! -L "$pull_target" ]] || fail "commit runtime pull program is not a regular file"
  cmp -s "$REPO_DIR/$pull_rel" "$pull_target" || fail "commit runtime already exists with different pull program"
  # Identical content alone is not enough: a lost execute bit would leave the
  # unit (or Restic's ExecStartPre) unable to run despite a success receipt.
  chmod 0755 "$pull_target"
else
  install -m 0755 "$REPO_DIR/$pull_rel" "$pull_target"
fi
if [[ -e "$gate_target" || -L "$gate_target" ]]; then
  [[ -f "$gate_target" && ! -L "$gate_target" ]] || fail "commit runtime Restic plaintext gate is not a regular file"
  cmp -s "$REPO_DIR/$gate_rel" "$gate_target" || fail "commit runtime already exists with different Restic plaintext gate"
  # Identical content alone is not enough: a lost execute bit would leave the
  # unit (or Restic's ExecStartPre) unable to run despite a success receipt.
  chmod 0755 "$gate_target"
else
  install -m 0755 "$REPO_DIR/$gate_rel" "$gate_target"
fi

service_tmp="$(mktemp "$unit_dir/.weltgewebe-postgres-offhost-pull.service.XXXXXX")"
timer_tmp="$(mktemp "$unit_dir/.weltgewebe-postgres-offhost-pull.timer.XXXXXX")"
receipt_tmp=""
cleanup() {
  [[ -z "$service_tmp" ]] || rm -f "$service_tmp"
  [[ -z "$timer_tmp" ]] || rm -f "$timer_tmp"
  [[ -z "$receipt_tmp" ]] || rm -f "$receipt_tmp"
}
trap cleanup EXIT
sed "s/@WELTGEWEBE_COMMIT@/$expected_commit/g" "$REPO_DIR/$service_rel" > "$service_tmp"
! grep -q '@WELTGEWEBE_COMMIT@' "$service_tmp" || fail "commit placeholder remained in generated service"
grep -qxF "ExecStart=%h/.local/libexec/weltgewebe/$expected_commit/pull-production-postgres-backup.sh" "$service_tmp" || fail "generated service is not commit-bound"
chmod 0644 "$service_tmp"
install -m 0644 "$REPO_DIR/$timer_rel" "$timer_tmp"
mv -f "$service_tmp" "$service_target"
service_tmp=""
mv -f "$timer_tmp" "$timer_target"
timer_tmp=""

"$SYSTEMCTL_BIN" --user daemon-reload
if ((enable_now == 1)); then
  "$SYSTEMCTL_BIN" --user enable --now weltgewebe-postgres-offhost-pull.timer
fi

receipt_tmp="$(mktemp "$release_dir/.install.receipt.XXXXXX")"
{
  printf 'contract=weltgewebe-postgres-offhost-user-install-v1\n'
  printf 'installed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'git_commit=%s\n' "$expected_commit"
  printf 'pull_sha256=%s\n' "$(sha256_file "$pull_target")"
  printf 'restic_gate_sha256=%s\n' "$(sha256_file "$gate_target")"
  printf 'service_sha256=%s\n' "$(sha256_file "$service_target")"
  printf 'timer_sha256=%s\n' "$(sha256_file "$timer_target")"
} > "$receipt_tmp"
chmod 0600 "$receipt_tmp"
mv -f "$receipt_tmp" "$receipt"
receipt_tmp=""
printf 'Off-host pull runtime installed for commit %s\nService: %s\nReceipt: %s\n' \
  "$expected_commit" "$service_target" "$receipt"
