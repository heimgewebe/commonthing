#!/usr/bin/env bash
set -Eeuo pipefail

RELEASE_ROOT="${WELTGEWEBE_RELEASE_ROOT:-/opt/weltgewebe-releases}"
SOURCE_CHECKOUT="${WELTGEWEBE_SOURCE_CHECKOUT:-/opt/weltgewebe}"
RUNTIME_ENV="${WELTGEWEBE_RUNTIME_ENV:-/etc/weltgewebe/weltgewebe.env}"
BUILD_USER="${WELTGEWEBE_BUILD_USER:-alex}"
WEB_PUSH_CONTACT="${WEB_PUSH_VAPID_CONTACT_DEFAULT:-mailto:kontakt@weltweberei.org}"
WEB_PUSH_ALLOWED_HOST_SUFFIXES="${WEB_PUSH_ALLOWED_HOST_SUFFIXES_DEFAULT:-fcm.googleapis.com,push.services.mozilla.com,web.push.apple.com}"
RELEASE_DIR=""
COMMIT=""

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

require_root_safe_directory() {
  local path="$1" label="$2" mode
  [[ -d "$path" && ! -L "$path" ]] || fail "$label is missing or unsafe: $path"
  [[ "$(stat --format=%u "$path")" == "0" ]] || fail "$label is not root-owned: $path"
  mode="$(stat --format=%a "$path")"
  (((8#$mode & 022) == 0)) || fail "$label is group- or world-writable: $path"
}

require_root_safe_regular_file() {
  local path="$1" label="$2" mode
  [[ -f "$path" && ! -L "$path" ]] || fail "$label is missing or unsafe: $path"
  [[ "$(stat --format=%u "$path")" == "0" ]] || fail "$label is not root-owned: $path"
  mode="$(stat --format=%a "$path")"
  (((8#$mode & 022) == 0)) || fail "$label is group- or world-writable: $path"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --release-dir)
      [[ $# -ge 2 ]] || fail "--release-dir requires a value"
      RELEASE_DIR="$2"
      shift 2
      ;;
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
    *) fail "unknown argument: $1" ;;
  esac
done

[[ "$EUID" -eq 0 ]] || fail "production reconciler activation must run as root"
[[ "$COMMIT" =~ ^[0-9a-f]{40}$ ]] || fail "--commit must be a full lowercase SHA-1"
[[ -n "$RELEASE_DIR" ]] || fail "--release-dir is required"
[[ "$BUILD_USER" =~ ^[A-Za-z_][A-Za-z0-9_-]*\$?$ ]] || fail "build user name is unsafe"

require_root_safe_directory "$RELEASE_ROOT" "release root"
require_root_safe_directory "$RELEASE_DIR" "release directory"
if ! release_root_real="$(realpath -- "$RELEASE_ROOT" 2> /dev/null)"; then
  fail "release root cannot be resolved: $RELEASE_ROOT"
fi
if ! release_dir_real="$(realpath -- "$RELEASE_DIR" 2> /dev/null)"; then
  fail "release directory cannot be resolved: $RELEASE_DIR"
fi
require_root_safe_directory "$release_root_real" "resolved release root"
require_root_safe_directory "$release_dir_real" "resolved release directory"
[[ "$release_dir_real" == "$release_root_real/$COMMIT" ]] ||
  fail "release directory is not the exact commit path: $release_dir_real"

require_root_safe_directory "$release_dir_real/scripts" "release scripts directory"
require_root_safe_directory "$release_dir_real/scripts/ops" "release ops directory"
installer="$release_dir_real/scripts/ops/install-production-reconciler.sh"
require_root_safe_regular_file "$installer" "release installer"
[[ -x "$installer" ]] || fail "release installer is not executable: $installer"

if ! release_head="$(git -c core.hooksPath=/dev/null -C "$release_dir_real" rev-parse --verify 'HEAD^{commit}' 2> /dev/null)"; then
  fail "release directory is not a valid Git repository: $release_dir_real"
fi
[[ "$release_head" == "$COMMIT" ]] ||
  fail "release HEAD does not match commit: expected $COMMIT, got $release_head"

authorization_checker="$release_dir_real/scripts/ops/verify_production_hold_release.py"
require_root_safe_regular_file "$authorization_checker" "production HOLD release checker"
python3 -I "$authorization_checker" --commit "$COMMIT" \
  --source-repository "$release_dir_real" ||
  fail "production HOLD authorization or source OCI digest is mismatched"

# A reconciler-triggered deployment re-enters weltgewebe-up while the first
# service is running but before its completion-relative timer is enabled.
# The root grant above was checked; only the existing inherited deployment
# lock handoff may skip recursive installation of the same service.
reconciler_service_state="$(systemctl show --property=ActiveState --value weltgewebe-production-reconcile.service)" ||
  fail "cannot inspect current reconciler service ActiveState"
case "$reconciler_service_state" in
  active|activating|inactive) ;;
  *) fail "unsupported reconciler service state: $reconciler_service_state" ;;
esac
if [[ "$reconciler_service_state" == "active" || "$reconciler_service_state" == "activating" ]]; then
  [[ "${WELTGEWEBE_PRODUCTION_LOCK_OWNER_ENTRYPOINT:-}" == "reconciler" &&
    "${WELTGEWEBE_PRODUCTION_LOCK_DOMAIN:-}" == "weltgewebe-production-deployment-v1" &&
    "${WELTGEWEBE_DEPLOY_INVOCATION_ID:-}" =~ ^[0-9a-f]{64}$ ]] ||
    fail "active reconciler service requires exact inherited deployment handoff"
  # During first-start bootstrap the timer is deliberately disabled while
  # this active service performs its initial reconcile. Never recurse there.
  # On later scheduled upgrades the timer is enabled; update the installed
  # helper contract in deferred mode, without restarting the active service.
  timer_unit_state="$(systemctl show --property=UnitFileState --value weltgewebe-production-reconcile.timer)" ||
    fail "cannot inspect timer enablement during active reconciliation"
  timer_active_state="$(systemctl show --property=ActiveState --value weltgewebe-production-reconcile.timer)" ||
    fail "cannot inspect timer activity during active reconciliation"
  case "$timer_unit_state:$timer_active_state" in
    disabled:inactive)
      echo "production_reconciler_activation=already_running commit=$COMMIT release=$release_dir_real"
      exit 0
      ;;
    enabled:active) ;; # deferred upgrade is safe after timer quiescence
    *) fail "active reconciler has unsupported timer state: $timer_unit_state:$timer_active_state" ;;
  esac
fi

# The release authorization above is a prerequisite, not a consequence of
# the timer state. The intentional first-start HOLD uses disabled/inactive.
# Only a completely active existing timer may use a deferred upgrade.
select_reconciler_start_mode() {
  local unit_state active_state
  unit_state="$(systemctl show --property=UnitFileState --value weltgewebe-production-reconcile.timer)" ||
    fail "cannot inspect production timer installation state"
  active_state="$(systemctl show --property=ActiveState --value weltgewebe-production-reconcile.timer)" ||
    fail "cannot inspect production timer runtime state"
  case "$unit_state:$active_state" in
    disabled:inactive) printf '%s\n' initial ;;
    enabled:active) printf '%s\n' deferred ;;
    *) fail "unsupported production timer state: enabled=$unit_state active=$active_state" ;;
  esac
}

install_mode="$(select_reconciler_start_mode)"
installer_options=(--commit "$COMMIT" --build-user "$BUILD_USER")
if [[ "$install_mode" == "deferred" ]]; then
  installer_options+=(--defer-reconcile)
fi
# The exact, authorized mode is preflighted before any VAPID write.
WELTGEWEBE_SOURCE_CHECKOUT="$SOURCE_CHECKOUT" \
  "$installer" "${installer_options[@]}" --preflight-only

web_push_bootstrap="$release_dir_real/scripts/ops/ensure_web_push_vapid_env.py"
require_root_safe_regular_file "$web_push_bootstrap" "Web Push VAPID bootstrap"
require_root_safe_regular_file "$RUNTIME_ENV" "runtime environment"
[[ "$(select_reconciler_start_mode)" == "$install_mode" ]] ||
  fail "production timer state changed before VAPID provisioning"
python3 -I "$web_push_bootstrap" \
  --env-file "$RUNTIME_ENV" \
  --contact "$WEB_PUSH_CONTACT" \
  --allowed-host-suffixes "$WEB_PUSH_ALLOWED_HOST_SUFFIXES"

# A second read-only mode check before installation prevents a changed timer
# from silently turning a deferred update into an initial service start.
[[ "$(select_reconciler_start_mode)" == "$install_mode" ]] ||
  fail "production timer state changed after first-start preflight"
WELTGEWEBE_SOURCE_CHECKOUT="$SOURCE_CHECKOUT" \
  "$installer" "${installer_options[@]}"

echo "production_reconciler_activation=installed commit=$COMMIT release=$release_dir_real mode=$install_mode"
