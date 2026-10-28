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

# A root-owned checkout can still be replaced through a writable ancestor.
# Reject replaceable Git metadata before privileged Git invocations.
require_root_trusted_git_directory_chain() {
  local target="${1%/}" label="$2" path mode uid
  [[ -n "$target" ]] || target="/"
  [[ "$target" == /* ]] || fail "$label must be absolute"
  [[ "$(realpath -e -- "$target")" == "$target" ]] ||
    fail "$label must be canonical and symlink-free: $target"
  path="$target"
  while :; do
    [[ -d "$path" && ! -L "$path" ]] ||
      fail "$label contains an unsafe directory: $path"
    uid="$(stat --format=%u -- "$path")" ||
      fail "$label ownership cannot be inspected: $path"
    [[ "$uid" == "0" ]] ||
      fail "$label includes a non-root-owned directory: $path"
    mode="$(stat --format=%a -- "$path")" ||
      fail "$label mode cannot be inspected: $path"
    if (((8#$mode & 022) != 0)); then
      # Root-owned sticky parents protect root-owned entries; never allow
      # a writable checkout or common directory as the trust target itself.
      if [[ "$path" == "$target" ]] || (((8#$mode & 01000) == 0)); then
        fail "$label includes a replaceable directory: $path"
      fi
    fi
    [[ "$path" == "/" ]] && break
    path="${path%/*}"
    [[ -n "$path" ]] || path="/"
  done
}
require_root_trusted_git_directory_chain "$REPO_DIR" "source checkout"
git_marker="$REPO_DIR/.git"
[[ (-f "$git_marker" || -d "$git_marker") && ! -L "$git_marker" ]] ||
  fail "Git metadata marker is missing or symlinked: $git_marker"
[[ "$(stat --format=%u -- "$git_marker")" == "0" ]] ||
  fail "Git metadata marker is not root-owned: $git_marker"
git_marker_mode="$(stat --format=%a -- "$git_marker")" ||
  fail "Git metadata marker mode cannot be inspected"
(((8#$git_marker_mode & 022) == 0)) ||
  fail "Git metadata is group/world writable: $git_marker"

# Forbid metadata links before Git can read a redirected config as root.
# A root-owned symlink itself says nothing about who can edit its target.
if [[ -d "$git_marker" ]]; then
  [[ -z "$(find "$git_marker" -xdev -type l -print -quit)" ]] ||
    fail "Git metadata contains a symlink before privileged Git access"
fi

git_common_dir="$(git -C "$REPO_DIR" rev-parse --git-common-dir)"
if [[ "$git_common_dir" != /* ]]; then
  git_common_dir="$REPO_DIR/$git_common_dir"
fi
git_common_dir="$(realpath "$git_common_dir")"
[[ -d "$git_common_dir" && ! -L "$git_common_dir" ]] || fail "Git object store is unsafe"
[[ "$(stat --format=%u "$REPO_DIR")" == "0" ]] || fail "source checkout is not root-owned"
require_root_trusted_git_directory_chain "$git_common_dir" "Git common directory"
# Linked worktrees can store Git metadata outside the .git marker.
# Deny symlinks in the resolved common directory before remote Git access.
[[ -z "$(find "$git_common_dir" -xdev -type l -print -quit)" ]] ||
  fail "Git common metadata contains a symlink"
unsafe_git_path="$(find "$git_common_dir" -xdev \( ! -user root -o -perm /022 \) -print -quit)"
[[ -z "$unsafe_git_path" ]] || fail "Git object store is not entirely root-owned and non-writable by group/world: $unsafe_git_path"
# A root-owned alternates pointer may reference an attacker-writable external
# object store. Reject all Git object indirection before any root Git operation.
[[ -z "${GIT_ALTERNATE_OBJECT_DIRECTORIES:-}" && -z "${GIT_OBJECT_DIRECTORY:-}" ]] ||
  fail "Git object database environment overrides are not allowed"
[[ ! -e "$git_common_dir/objects/info/alternates" && ! -L "$git_common_dir/objects/info/alternates" ]] ||
  fail "Git alternate object stores are not trusted for production"
[[ -z "$(find "$git_common_dir/objects" -xdev -type l -print -quit)" ]] ||
  fail "Git object store contains a symlink outside the trust boundary"

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
# A new, approved main may not yet be in the local object database. Fetch
# only its immutable objects, without updating FETCH_HEAD or tracking refs.
if ! git -C "$REPO_DIR" cat-file -e "$COMMIT^{commit}" 2> /dev/null; then
  git -C "$REPO_DIR" fetch --no-write-fetch-head --no-tags --no-auto-gc --no-recurse-submodules origin "$COMMIT" ||
    fail "authorized source commit cannot be inspected before installation"
fi
python3 -I "$authorization_checker" --commit "$COMMIT" \
  --source-repository "$REPO_DIR" ||
  fail "source commit OCI digest is not authorized before timer or VAPID changes"

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
# An external direct deploy helper may already hold this root production lock.
# In that case the new oneshot reconciler would merely return "already_running"
# with exit 0 and the initial timer would falsely become enabled.
probe_initial_root_deploy_lock() {
  local filename="$1" probe_fd
  exec {probe_fd}< "$filename" || fail "initial production lock probe cannot open lock"
  if ! flock -n "$probe_fd"; then
    fail "first-start blocked: another deployment holds the root production lock"
  fi
  flock -u "$probe_fd" || fail "initial production lock probe cannot unlock"
  exec {probe_fd}<&-
}

if ((DEFER_RECONCILE != 1)); then
  production_state_root="${WELTGEWEBE_DEPLOY_STATE_ROOT:-/var/lib/weltgewebe-main-reconciler}"
  production_lock="$production_state_root/production-deployment.lock"
  if [[ -e "$production_lock" || -L "$production_lock" ]]; then
    [[ -d "$production_state_root" && ! -L "$production_state_root" ]] ||
      fail "initial production lock parent is unsafe"
    [[ "$(stat -c %u -- "$production_state_root")" == "0" ]] ||
      fail "initial production lock parent is not root-owned"
    [[ -f "$production_lock" && ! -L "$production_lock" ]] ||
      fail "initial production lock is unsafe"
    [[ "$(stat -c %u -- "$production_lock")" == "0" ]] ||
      fail "initial production lock is not root-owned"
    lock_mode="$(stat -c %a -- "$production_lock")"
    (((8#$lock_mode & 022) == 0)) ||
      fail "initial production lock is group/world writable"
    probe_initial_root_deploy_lock "$production_lock"
  fi
fi

if ((PREFLIGHT_ONLY == 1)); then
  echo "production_reconciler_preflight=passed commit=$COMMIT"
  exit 0
fi
# Quiesce the timer in BOTH modes before mutable Git refs or helper replacement.
# Disable rather than merely stop: an interrupted install or reboot must never
# re-activate a partially updated production contract. Failure remains HOLD.
if systemctl cat weltgewebe-production-reconcile.timer > /dev/null 2>&1; then
  systemctl disable --now weltgewebe-production-reconcile.timer ||
    fail "cannot safely disable production reconcile timer before installation"
fi
systemctl is-enabled --quiet weltgewebe-production-reconcile.timer &&
  fail "production reconcile timer stayed enabled before contract installation"
systemctl is-active --quiet weltgewebe-production-reconcile.timer &&
  fail "production reconcile timer stayed active before contract installation"
if ((DEFER_RECONCILE != 1)); then
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
  # The current service may already be running (and must not be restarted).
  # Only after every installed helper and unit has been hash-checked may a
  # previously enabled timer become eligible for a new scheduled invocation.
  systemctl is-enabled --quiet weltgewebe-production-reconcile.timer &&
    fail "production reconcile timer re-enabled during deferred installation"
  systemctl is-active --quiet weltgewebe-production-reconcile.timer &&
    fail "production reconcile timer restarted during deferred installation"
  systemctl enable --now weltgewebe-production-reconcile.timer
  systemctl is-enabled --quiet weltgewebe-production-reconcile.timer ||
    fail "production reconcile timer was not enabled after deferred installation"
  systemctl is-active --quiet weltgewebe-production-reconcile.timer ||
    fail "production reconcile timer was not active after deferred installation"
  install_mode="deferred"
else
  # Service runs exactly once first. OnActiveSec starts a fresh timer window;
  # unlike OnBootSec it never catches up immediately upon enable.
  systemctl start weltgewebe-production-reconcile.service
  systemctl enable --now weltgewebe-production-reconcile.timer
  install_mode="started"
fi

echo "production_reconciler=installed commit=$COMMIT build_user=$BUILD_USER mode=$install_mode manifest=$manifest"
