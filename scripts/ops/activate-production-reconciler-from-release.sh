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
python3 -I "$authorization_checker" --commit "$COMMIT" ||
  fail "production HOLD authorization missing or mismatched"

# A disabled production timer is an intentional HOLD, not a VAPID provisioning
# opportunity. The installer preflight must succeed before touching secrets.
WELTGEWEBE_SOURCE_CHECKOUT="$SOURCE_CHECKOUT" \
  "$installer" \
  --commit "$COMMIT" \
  --build-user "$BUILD_USER" \
  --defer-reconcile \
  --preflight-only

web_push_bootstrap="$release_dir_real/scripts/ops/ensure_web_push_vapid_env.py"
require_root_safe_regular_file "$web_push_bootstrap" "Web Push VAPID bootstrap"
require_root_safe_regular_file "$RUNTIME_ENV" "runtime environment"
python3 -I "$web_push_bootstrap" \
  --env-file "$RUNTIME_ENV" \
  --contact "$WEB_PUSH_CONTACT" \
  --allowed-host-suffixes "$WEB_PUSH_ALLOWED_HOST_SUFFIXES"

WELTGEWEBE_SOURCE_CHECKOUT="$SOURCE_CHECKOUT" \
  "$installer" \
  --commit "$COMMIT" \
  --build-user "$BUILD_USER" \
  --defer-reconcile

echo "production_reconciler_activation=installed commit=$COMMIT release=$release_dir_real"