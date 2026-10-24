#!/usr/bin/env bash
set -Eeuo pipefail

REPO_DIR="${WELTGEWEBE_SOURCE_CHECKOUT:-/opt/weltgewebe}"
BUILD_USER="${WELTGEWEBE_BUILD_USER:-alex}"
BUILD_MIN_FREE_BYTES="${WELTGEWEBE_PRODUCTION_BUILD_MIN_FREE_BYTES:-51539607552}"
COMMIT=""
DEFER_RECONCILE=0
PREFLIGHT_ONLY=0
staging=""

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

cleanup() {
  local rc=$?
  trap - EXIT
  if [[ -n "$staging" && -d "$staging" && ! -L "$staging" ]]; then
    rm -rf -- "$staging"
  fi
  exit "$rc"
}
trap cleanup EXIT

while [[ $# -gt 0 ]]; do
  case "$1" in
    --commit)
      [[ $# -ge 2 ]] || fail "--commit requires a value"
      COMMIT="$2"
      shift 2
      ;;
    --build-user)
      [[ $# -ge 2 ]] || fail "--build-user requires a value"
      BUILD_USER="$2"
      shift 2
      ;;
    --defer-reconcile)
      DEFER_RECONCILE=1
      shift
      ;;
    --preflight-only)
      PREFLIGHT_ONLY=1
      shift
      ;;
    *) fail "unknown argument: $1" ;;
  esac
done

[[ "$EUID" -eq 0 ]] || fail "install-production-reconciler.sh must run as root"
[[ "$COMMIT" =~ ^[0-9a-f]{40}$ ]] || fail "--commit must be a full lowercase SHA-1"
[[ "$BUILD_USER" =~ ^[A-Za-z_][A-Za-z0-9_-]*\$?$ ]] || fail "build user name is unsafe"
[[ "$BUILD_MIN_FREE_BYTES" =~ ^[0-9]+$ ]] || fail "production build minimum free disk is invalid"
((BUILD_MIN_FREE_BYTES >= 8589934592)) || fail "production build minimum free disk is too small"
getent passwd "$BUILD_USER" > /dev/null || fail "build user does not exist: $BUILD_USER"
[[ -d "$REPO_DIR" && ! -L "$REPO_DIR" ]] || fail "source checkout is missing or unsafe: $REPO_DIR"

git_common_dir="$(git -C "$REPO_DIR" rev-parse --git-common-dir)"
if [[ "$git_common_dir" != /* ]]; then
  git_common_dir="$REPO_DIR/$git_common_dir"
fi
git_common_dir="$(realpath "$git_common_dir")"
[[ -d "$git_common_dir" && ! -L "$git_common_dir" ]] || fail "Git object store is unsafe"
[[ "$(stat --format=%u "$REPO_DIR")" == "0" ]] || fail "source checkout is not root-owned"
unsafe_git_path="$(find "$git_common_dir" -xdev \( ! -user root -o -perm /022 \) -print -quit)"
[[ -z "$unsafe_git_path" ]] || fail "Git object store is not entirely root-owned and non-writable by group/world: $unsafe_git_path"

# The authority is outside Git and cannot be manufactured by this installer.
# Validate it BEFORE changing VAPID, systemd, Git tracking refs or state.
installer_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
[[ "$(stat --format=%u "$installer_dir")" == "0" ]] ||
  fail "production installer directory is not root-owned"
installer_dir_mode="$(stat --format=%a "$installer_dir")"
(((8#$installer_dir_mode & 022) == 0)) ||
  fail "production installer directory is group/world writable"
authorization_checker="$installer_dir/verify_production_hold_release.py"
[[ -f "$authorization_checker" && ! -L "$authorization_checker" ]] ||
  fail "root-owned production HOLD authorization checker is missing or unsafe"
[[ "$(stat --format=%u "$authorization_checker")" == "0" ]] ||
  fail "production HOLD checker is not root-owned"
checker_mode="$(stat --format=%a "$authorization_checker")"
(((8#$checker_mode & 022) == 0)) || fail "production HOLD checker is writable by group/world"
python3 -I "$authorization_checker" --commit "$COMMIT" ||
  fail "production HOLD release is not authorized for exact commit $COMMIT"

# Read-only remote check. A later fetch rechecks the same immutable commit.
remote_main="$(git -C "$REPO_DIR" ls-remote --exit-code origin refs/heads/main | awk 'NR==1 {print $1}')" ||
  fail "cannot verify current remote main"
[[ "$remote_main" == "$COMMIT" ]] ||
  fail "installation commit is not current origin/main: expected $COMMIT, got $remote_main"

timer_was_enabled=0
timer_was_active=0
if systemctl is-enabled --quiet weltgewebe-production-reconcile.timer; then
  timer_was_enabled=1
fi
if systemctl is-active --quiet weltgewebe-production-reconcile.timer; then
  timer_was_active=1
fi
if ((DEFER_RECONCILE == 1)) &&
  ((timer_was_enabled != 1 || timer_was_active != 1)); then
  fail "deferred installation requires an existing enabled and active production reconcile timer"
fi
if ((DEFER_RECONCILE != 1)) &&
  ((timer_was_enabled == 1 || timer_was_active == 1)); then
  fail "initial installation requires disabled and inactive production reconcile timer"
fi
if ((PREFLIGHT_ONLY == 1)); then
  echo "production_reconciler_preflight=passed commit=$COMMIT"
  exit 0
fi
if ((DEFER_RECONCILE != 1)); then
  # Disable first; otherwise a stale OnBootSec timer or reboot could run
  # an older unguarded reconciler while its installation is being replaced.
  if systemctl cat weltgewebe-production-reconcile.timer > /dev/null 2>&1; then
    systemctl disable --now weltgewebe-production-reconcile.timer ||
      fail "cannot safely disable production reconcile timer before installation"
  fi
  systemctl is-active --quiet weltgewebe-production-reconcile.service &&
    fail "production reconcile service is already active"
fi

git -C "$REPO_DIR" fetch --no-tags origin \
  "+refs/heads/main:refs/remotes/origin/main"
remote_main="$(git -C "$REPO_DIR" rev-parse refs/remotes/origin/main)"
[[ "$remote_main" == "$COMMIT" ]] ||
  fail "installation commit changed after authorization: expected $COMMIT, got $remote_main"
git -C "$REPO_DIR" cat-file -e "$COMMIT^{commit}"

staging="$(mktemp -d /run/weltgewebe-reconciler-install.XXXXXX)"
chmod 0700 "$staging"

atomic_install() {
  local source_path="$1"
  local target_path="$2"
  local mode="$3"
  local target_directory target_name temporary
  target_directory="$(dirname "$target_path")"
  target_name="$(basename "$target_path")"
  temporary="$(mktemp "$target_directory/.${target_name}.XXXXXX")"
  if ! install -o root -g root -m "$mode" "$source_path" "$temporary"; then
    rm -f -- "$temporary"
    return 1
  fi
  if ! mv -fT -- "$temporary" "$target_path"; then
    rm -f -- "$temporary"
    return 1
  fi
}

stage_file() {
  local source_path="$1"
  local target_name="$2"
  local mode="$3"
  git -C "$REPO_DIR" show "$COMMIT:$source_path" > "$staging/$target_name"
  chmod "$mode" "$staging/$target_name"
}

require_matching_sha256() {
  local source_path="$1"
  local target_path="$2"
  local expected observed
  expected="$(sha256sum "$source_path" | awk '{print $1}')"
  observed="$(sha256sum "$target_path" | awk '{print $1}')"
  [[ "$observed" == "$expected" ]] ||
    fail "installed contract hash mismatch: $target_path"
}

stage_file scripts/ops/deploy-exact-commit-vps.sh weltgewebe-deploy-exact-commit 0755
stage_file scripts/ops/reconcile-production-main-vps.sh weltgewebe-reconcile-production-main 0755
stage_file scripts/ops/validate_web_deploy_archive.py weltgewebe-validate-web-deploy-archive 0755
stage_file scripts/ops/verify_public_release_commit.py weltgewebe-verify-public-release 0755
stage_file scripts/ops/weltgewebe_secure_receipt_io.py weltgewebe_secure_receipt_io.py 0644
stage_file scripts/ops/verify_production_hold_release.py weltgewebe-verify-production-hold-release 0755
stage_file infra/systemd/system/weltgewebe-production-reconcile.service weltgewebe-production-reconcile.service 0644
stage_file infra/systemd/system/weltgewebe-production-reconcile.timer weltgewebe-production-reconcile.timer 0644

bash -n "$staging/weltgewebe-deploy-exact-commit"
bash -n "$staging/weltgewebe-reconcile-production-main"
python3 -m py_compile \
  "$staging/weltgewebe-validate-web-deploy-archive" \
  "$staging/weltgewebe-verify-public-release" \
  "$staging/weltgewebe-verify-production-hold-release" \
  "$staging/weltgewebe_secure_receipt_io.py"

install -d -o root -g root -m 0755 /usr/local/libexec
# Install the dependency before any entrypoint begins importing it. Old
# entrypoints ignore this file, so both sides of the rollout remain executable.
atomic_install "$staging/weltgewebe_secure_receipt_io.py" \
  /usr/local/libexec/weltgewebe_secure_receipt_io.py 0644
atomic_install "$staging/weltgewebe-verify-production-hold-release" \
  /usr/local/libexec/weltgewebe-verify-production-hold-release 0755
atomic_install "$staging/weltgewebe-deploy-exact-commit" \
  /usr/local/libexec/weltgewebe-deploy-exact-commit 0755
atomic_install "$staging/weltgewebe-reconcile-production-main" \
  /usr/local/libexec/weltgewebe-reconcile-production-main 0755
atomic_install "$staging/weltgewebe-validate-web-deploy-archive" \
  /usr/local/libexec/weltgewebe-validate-web-deploy-archive 0755
atomic_install "$staging/weltgewebe-verify-public-release" \
  /usr/local/libexec/weltgewebe-verify-public-release 0755
require_matching_sha256 "$staging/weltgewebe-verify-production-hold-release" \
  /usr/local/libexec/weltgewebe-verify-production-hold-release
require_matching_sha256 "$staging/weltgewebe-deploy-exact-commit" \
  /usr/local/libexec/weltgewebe-deploy-exact-commit
require_matching_sha256 "$staging/weltgewebe-reconcile-production-main" \
  /usr/local/libexec/weltgewebe-reconcile-production-main
require_matching_sha256 "$staging/weltgewebe-validate-web-deploy-archive" \
  /usr/local/libexec/weltgewebe-validate-web-deploy-archive
require_matching_sha256 "$staging/weltgewebe-verify-public-release" \
  /usr/local/libexec/weltgewebe-verify-public-release
require_matching_sha256 "$staging/weltgewebe_secure_receipt_io.py" \
  /usr/local/libexec/weltgewebe_secure_receipt_io.py
systemd-analyze verify \
  "$staging/weltgewebe-production-reconcile.service" \
  "$staging/weltgewebe-production-reconcile.timer"

atomic_install "$staging/weltgewebe-production-reconcile.service" \
  /etc/systemd/system/weltgewebe-production-reconcile.service 0644
atomic_install "$staging/weltgewebe-production-reconcile.timer" \
  /etc/systemd/system/weltgewebe-production-reconcile.timer 0644

require_matching_sha256 "$staging/weltgewebe-production-reconcile.service" \
  /etc/systemd/system/weltgewebe-production-reconcile.service
require_matching_sha256 "$staging/weltgewebe-production-reconcile.timer" \
  /etc/systemd/system/weltgewebe-production-reconcile.timer

install -d -o root -g root -m 0700 /etc/weltgewebe
{
  printf 'WELTGEWEBE_BUILD_USER=%s\n' "$BUILD_USER"
  printf 'WELTGEWEBE_PRODUCTION_BUILD_MIN_FREE_BYTES=%s\n' "$BUILD_MIN_FREE_BYTES"
} > "$staging/production-reconciler.env"
atomic_install "$staging/production-reconciler.env" \
  /etc/weltgewebe/production-reconciler.env 0600

install -d -o root -g root -m 0711 /var/lib/weltgewebe-main-reconciler
install -d -o root -g root -m 0700 \
  /var/lib/weltgewebe-main-reconciler/artifacts \
  /var/lib/weltgewebe-main-reconciler/receipts \
  /var/lib/weltgewebe-main-reconciler/reconcile-receipts \
  /var/lib/weltgewebe-main-reconciler/docker-config
install -d -o root -g root -m 0755 /opt/weltgewebe-releases

manifest="/var/lib/weltgewebe-main-reconciler/installed-contract.sha256"
{
  printf 'commit  %s\n' "$COMMIT"
  sha256sum \
    /usr/local/libexec/weltgewebe-deploy-exact-commit \
    /usr/local/libexec/weltgewebe-verify-production-hold-release \
    /usr/local/libexec/weltgewebe-reconcile-production-main \
    /usr/local/libexec/weltgewebe-validate-web-deploy-archive \
    /usr/local/libexec/weltgewebe-verify-public-release \
    /usr/local/libexec/weltgewebe_secure_receipt_io.py \
    /etc/systemd/system/weltgewebe-production-reconcile.service \
    /etc/systemd/system/weltgewebe-production-reconcile.timer
} > "$staging/installed-contract.sha256"
atomic_install "$staging/installed-contract.sha256" "$manifest" 0600

systemctl daemon-reload
if ((DEFER_RECONCILE == 1)); then
  systemctl is-enabled --quiet weltgewebe-production-reconcile.timer ||
    fail "production reconcile timer lost its enabled state during deferred installation"
  systemctl is-active --quiet weltgewebe-production-reconcile.timer ||
    fail "production reconcile timer lost its active state during deferred installation"
  install_mode="deferred"
else
  # Service runs exactly once first. OnActiveSec starts a fresh timer window;
  # unlike OnBootSec it never catches up immediately upon enable.
  systemctl start weltgewebe-production-reconcile.service
  systemctl enable --now weltgewebe-production-reconcile.timer
  install_mode="started"
fi

echo "production_reconciler=installed commit=$COMMIT build_user=$BUILD_USER mode=$install_mode manifest=$manifest"