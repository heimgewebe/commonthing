#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(
  cd -- "$(dirname -- "${BASH_SOURCE[0]}")" > /dev/null 2>&1
  pwd
)"
REPO_ROOT="$(
  cd -- "${SCRIPT_DIR}/../.." > /dev/null 2>&1
  pwd
)"
GUARD_SCRIPT="${REPO_ROOT}/scripts/guard/prod-public-base-url-guard.sh"
BASE_SOURCE="${REPO_ROOT}/infra/compose/compose.prod.yml"

for required_file in "$GUARD_SCRIPT" "$BASE_SOURCE"; do
  if [[ ! -f "$required_file" ]]; then
    echo "ERROR: required test input missing: $required_file" >&2
    exit 2
  fi
done

TEST_TMP="$(mktemp -d)"
LAST_EXIT=0
LAST_OUTPUT=""

cleanup() {
  rm -rf "$TEST_TMP"
}
trap cleanup EXIT

mkdir -p "$TEST_TMP/infra/compose"
cp "$BASE_SOURCE" "$TEST_TMP/infra/compose/compose.prod.yml"

write_override() {
  local app_base="$1"
  local api_web_host="$2"
  local api_web_url="$3"
  local caddy_web_host="$4"
  local caddy_web_url="$5"
  local auth_login="$6"
  local auth_token="$7"
  local webauthn_rp_id="${TEST_WEBAUTHN_RP_ID:-commonthing.net}"
  local webauthn_rp_origin="${TEST_WEBAUTHN_RP_ORIGIN:-https://commonthing.net}"
  local webauthn_rp_name="${TEST_WEBAUTHN_RP_NAME:-commonThing}"

  cat > "$TEST_TMP/infra/compose/compose.prod.override.yml" << EOF_OVERRIDE
services:
  api:
    environment:
      APP_BASE_URL: $app_base
      WEBAUTHN_RP_ID: $webauthn_rp_id
      WEBAUTHN_RP_ORIGIN: $webauthn_rp_origin
      WEBAUTHN_RP_NAME: $webauthn_rp_name
      AUTH_PUBLIC_LOGIN: '$auth_login'
      AUTH_LOG_MAGIC_TOKEN: '$auth_token'
      WEB_UPSTREAM_HOST: $api_web_host
      WEB_UPSTREAM_URL: $api_web_url
  caddy:
EOF_OVERRIDE
  if [[ "$caddy_web_host" == "__absent__" ]]; then
    printf '    environment: {}\n' >> "$TEST_TMP/infra/compose/compose.prod.override.yml"
  else
    cat >> "$TEST_TMP/infra/compose/compose.prod.override.yml" << EOF_CADDY
    environment:
      WEB_UPSTREAM_HOST: $caddy_web_host
      WEB_UPSTREAM_URL: $caddy_web_url
EOF_CADDY
  fi
}

run_guard() {
  LAST_EXIT=0
  LAST_OUTPUT="$(REPO_ROOT="$TEST_TMP" bash "$GUARD_SCRIPT" 2>&1)" || LAST_EXIT=$?
}

assert_result() {
  local name="$1"
  local expected_exit="$2"
  local expected_fragment="$3"

  if [[ "$LAST_EXIT" -ne "$expected_exit" ]]; then
    echo "FAIL: $name: expected exit $expected_exit, got $LAST_EXIT" >&2
    echo "$LAST_OUTPUT" >&2
    exit 1
  fi

  if [[ -n "$expected_fragment" && "$LAST_OUTPUT" != *"$expected_fragment"* ]]; then
    echo "FAIL: $name: expected output fragment '$expected_fragment'" >&2
    echo "$LAST_OUTPUT" >&2
    exit 1
  fi

  echo "PASS: $name"
}

run_case() {
  local name="$1"
  local app_base="$2"
  local api_web_host="$3"
  local api_web_url="$4"
  local caddy_web_host="$5"
  local caddy_web_url="$6"
  local auth_login="$7"
  local auth_token="$8"
  local expected_exit="$9"
  local expected_fragment="${10}"

  write_override \
    "$app_base" \
    "$api_web_host" \
    "$api_web_url" \
    "$caddy_web_host" \
    "$caddy_web_url" \
    "$auth_login" \
    "$auth_token"
  run_guard
  assert_result "$name" "$expected_exit" "$expected_fragment"
}

echo "Running prod-public-base-url guard tests..."

LEGACY_HOME_HOST="weltgewebe.home.arpa" # commonthing-naming: legacy
LEGACY_HOME_URL="https://${LEGACY_HOME_HOST}"

run_case \
  "valid production contract" \
  "https://commonthing.net" \
  "commonthing.home.arpa" \
  "https://commonthing.home.arpa" \
  "__absent__" \
  "__absent__" \
  "1" "0" \
  0 "prod-public-base-url guard passed"

run_case \
  "legacy API WEB_UPSTREAM_HOST is rejected" \
  "https://commonthing.net" \
  "$LEGACY_HOME_HOST" \
  "https://commonthing.home.arpa" \
  "__absent__" \
  "__absent__" \
  "1" "0" \
  1 "services.api.environment.WEB_UPSTREAM_HOST"

run_case \
  "legacy API WEB_UPSTREAM_URL is rejected" \
  "https://commonthing.net" \
  "commonthing.home.arpa" \
  "$LEGACY_HOME_URL" \
  "__absent__" \
  "__absent__" \
  "1" "0" \
  1 "services.api.environment.WEB_UPSTREAM_URL"

run_case \
  "legacy public APP_BASE_URL is rejected" \
  "https://weltgewebe.net" \
  "commonthing.home.arpa" \
  "https://commonthing.home.arpa" \
  "__absent__" \
  "__absent__" \
  "1" "0" \
  1 "services.api.environment.APP_BASE_URL"

run_case \
  "internal APP_BASE_URL is rejected" \
  "https://commonthing.home.arpa" \
  "commonthing.home.arpa" \
  "https://commonthing.home.arpa" \
  "__absent__" \
  "__absent__" \
  "1" "0" \
  1 "services.api.environment.APP_BASE_URL"

run_case \
  "public API WEB_UPSTREAM_URL is rejected" \
  "https://commonthing.net" \
  "commonthing.home.arpa" \
  "https://commonthing.net" \
  "__absent__" \
  "__absent__" \
  "1" "0" \
  1 "services.api.environment.WEB_UPSTREAM_URL"

run_case \
  "Caddy WEB_UPSTREAM_HOST is rejected" \
  "https://commonthing.net" \
  "commonthing.home.arpa" \
  "https://commonthing.home.arpa" \
  "commonthing.net" \
  "https://commonthing.home.arpa" \
  "1" "0" \
  1 "services.caddy.environment.WEB_UPSTREAM_HOST"

run_case \
  "Caddy WEB_UPSTREAM_URL is rejected" \
  "https://commonthing.net" \
  "commonthing.home.arpa" \
  "https://commonthing.home.arpa" \
  "commonthing.home.arpa" \
  "https://commonthing.net" \
  "1" "0" \
  1 "services.caddy.environment.WEB_UPSTREAM_URL"

run_case \
  "disabled public login is rejected" \
  "https://commonthing.net" \
  "commonthing.home.arpa" \
  "https://commonthing.home.arpa" \
  "__absent__" \
  "__absent__" \
  "0" "0" \
  1 "services.api.environment.AUTH_PUBLIC_LOGIN"

run_case \
  "magic-token logging is rejected" \
  "https://commonthing.net" \
  "commonthing.home.arpa" \
  "https://commonthing.home.arpa" \
  "__absent__" \
  "__absent__" \
  "1" "1" \
  1 "services.api.environment.AUTH_LOG_MAGIC_TOKEN"

TEST_WEBAUTHN_RP_ID=wrong.example
run_case \
  "non-canonical WebAuthn RP ID is rejected" \
  "https://commonthing.net" \
  "commonthing.home.arpa" \
  "https://commonthing.home.arpa" \
  "__absent__" \
  "__absent__" \
  "1" "0" \
  1 "services.api.environment.WEBAUTHN_RP_ID"
unset TEST_WEBAUTHN_RP_ID

TEST_WEBAUTHN_RP_ORIGIN=https://example.invalid
run_case \
  "non-canonical WebAuthn RP origin is rejected" \
  "https://commonthing.net" \
  "commonthing.home.arpa" \
  "https://commonthing.home.arpa" \
  "__absent__" \
  "__absent__" \
  "1" "0" \
  1 "services.api.environment.WEBAUTHN_RP_ORIGIN"
unset TEST_WEBAUTHN_RP_ORIGIN

TEST_WEBAUTHN_RP_NAME=wrongName
run_case \
  "non-canonical WebAuthn RP name is rejected" \
  "https://commonthing.net" \
  "commonthing.home.arpa" \
  "https://commonthing.home.arpa" \
  "__absent__" \
  "__absent__" \
  "1" "0" \
  1 "services.api.environment.WEBAUTHN_RP_NAME"
unset TEST_WEBAUTHN_RP_NAME

rm "$TEST_TMP/infra/compose/compose.prod.yml"
run_guard
assert_result \
  "missing base Compose file is an execution error" \
  2 \
  "required Compose file missing: $TEST_TMP/infra/compose/compose.prod.yml"
cp "$BASE_SOURCE" "$TEST_TMP/infra/compose/compose.prod.yml"

rm "$TEST_TMP/infra/compose/compose.prod.override.yml"
run_guard
assert_result \
  "missing override Compose file is an execution error" \
  2 \
  "required Compose file missing: $TEST_TMP/infra/compose/compose.prod.override.yml"

echo "All prod-public-base-url guard tests passed."
