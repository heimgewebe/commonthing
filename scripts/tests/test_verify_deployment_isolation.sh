#!/usr/bin/env bash
# Regression: test_verify_deployment.sh must leave the checkout it is started
# from byte-identical, including ignored local artifacts that weltgewebe-up
# would write to (build output, .ops, deploy snapshot, glyphs) and scratch
# names the harness itself uses (test.env, mock_bin, custom_state).
set -euo pipefail

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CHECKOUT="$(mktemp -d)"
trap 'rm -rf "$CHECKOUT"' EXIT

# A disposable checkout of the current working tree, so the sentinels below
# never touch the real one.
git -C "$SOURCE_ROOT" ls-files -z --cached --others --exclude-standard |
  tar -C "$SOURCE_ROOT" --null --ignore-failed-read -T - -cf - |
  tar -xf - -C "$CHECKOUT"
git -C "$CHECKOUT" init -q

sentinels=(
  "apps/web/build/index.html"
  "apps/web/build/_app/version.json"
  ".ops/weltgewebe-up.state"
  ".ops/failures/sentinel/summary.txt"
  "artifacts/deploy.snapshot.json"
  "map-style/glyphs/Noto Sans Regular/.complete"
  "test.env"
  "mock_bin/docker"
  "custom_state/weltgewebe-up.state"
  "mock_edge_ca.crt"
)
for path in "${sentinels[@]}"; do
  mkdir -p "$CHECKOUT/$(dirname "$path")"
  printf 'sentinel %s\n' "$path" > "$CHECKOUT/$path"
done

inventory() {
  (
    cd "$CHECKOUT"
    find . -path ./.git -prune -o -type f -print0 | sort -z | xargs -0 sha256sum
  )
}

before="$(inventory)"
if ! output="$(bash "$CHECKOUT/scripts/tests/test_verify_deployment.sh" 2>&1)"; then
  echo "FAIL: test_verify_deployment.sh failed in the disposable checkout." >&2
  echo "$output" >&2
  exit 1
fi
after="$(inventory)"

if [[ "$before" != "$after" ]]; then
  echo "FAIL: test_verify_deployment.sh changed the checkout it ran from:" >&2
  diff <(echo "$before") <(echo "$after") >&2 || true
  exit 1
fi
echo "PASS: checkout byte-identical after test_verify_deployment.sh (${#sentinels[@]} sentinels)."
