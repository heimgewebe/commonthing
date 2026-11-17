#!/usr/bin/env bash
set -euo pipefail

GEWEBE_API_BASE="${GEWEBE_API_BASE:-http://127.0.0.1:8787}"
REQUEST_URL="${GEWEBE_API_BASE%/}/auth/magic-link/request"
EMAIL_RUN_SUFFIX="${GITHUB_RUN_ID:-local-$(date +%s)}-${GITHUB_RUN_ATTEMPT:-0}-$$-${RANDOM}"
EMAIL="rate-smoke+${EMAIL_RUN_SUFFIX}@example.invalid"

EXPECTED_STATUSES=(200 200 429)
ACTUAL_STATUSES=()
BODY_FILES=()

echo "Testing magic-link email rate limit at ${REQUEST_URL}"

# shellcheck disable=SC2317,SC2329
cleanup() {
  for body_file in "${BODY_FILES[@]:-}"; do
    if [ -n "${body_file:-}" ] && [ -f "$body_file" ]; then
      rm -f "$body_file"
    fi
  done
}
trap cleanup EXIT

for attempt in 1 2 3; do
  body_file="$(mktemp)"
  BODY_FILES+=("$body_file")

  if ! status_code="$(
    curl -sS \
      --max-time 10 \
      -o "$body_file" \
      -w "%{http_code}" \
      -X POST \
      -H "Content-Type: application/json" \
      "$REQUEST_URL" \
      --data "{\"email\":\"${EMAIL}\"}"
  )"; then
    echo "ERROR: request ${attempt} to ${REQUEST_URL} failed before receiving an HTTP status code"
    echo "Response body from failed request ${attempt}:"
    cat "$body_file" || true
    exit 1
  fi

  ACTUAL_STATUSES+=("$status_code")
done

if [ "${ACTUAL_STATUSES[*]}" != "${EXPECTED_STATUSES[*]}" ]; then
  echo "ERROR: magic-link email rate-limit runtime smoke failed"
  echo "Expected status sequence: ${EXPECTED_STATUSES[*]}"
  echo "Actual status sequence:   ${ACTUAL_STATUSES[*]}"
  echo
  echo "Response bodies:"
  for idx in 0 1 2; do
    request_no=$((idx + 1))
    echo "--- Request ${request_no} (status ${ACTUAL_STATUSES[$idx]}) ---"
    cat "${BODY_FILES[$idx]}"
    echo
  done
  exit 1
fi

echo "OK: Magic-link email rate limiting runtime smoke passed (${ACTUAL_STATUSES[*]} for ${EMAIL})"
exit 0
