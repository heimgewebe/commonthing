#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR=$(dirname "$0")
GUARD_SCRIPT="$SCRIPT_DIR/../preflight/csp_contract_static.sh"
TEST_DIR=$(mktemp -d)
trap 'rm -rf "$TEST_DIR"' EXIT
mkdir -p "$TEST_DIR/apps/web/build"
export ROOT="$TEST_DIR"
export CADDYFILE_PATH="$TEST_DIR/Caddyfile"
INDEX_HTML="$TEST_DIR/apps/web/build/index.html"

run_test() {
  local name="$1" expected_exit="$2"
  set +e
  output=$(bash "$GUARD_SCRIPT" 2>&1)
  actual=$?
  set -e
  if [[ "$actual" -ne "$expected_exit" ]]; then
    echo "FAIL: $name (expected $expected_exit, got $actual)" >&2
    echo "$output" >&2
    exit 1
  fi
  echo "PASS: $name"
}

export REQUIRE_FRONTEND=0
run_test "frontend-disabled" 0
export REQUIRE_FRONTEND=1
printf '%s\n' 'header @frontendResponse Content-Security-Policy "style-src '\''self'\''; frame-ancestors '\''none'\'';"' > "$CADDYFILE_PATH"
rm -f "$INDEX_HTML"
run_test "missing-index" 1

printf '%s\n' '<html><head><meta http-equiv="content-security-policy" content="script-src '\''self'\''"><script src="/app.js"></script></head><body></body></html>' > "$INDEX_HTML"
run_test "external-script-strict" 0

mkdir -p "$TEST_DIR/apps/web/build/weltweberei"
printf '%s\n' '<html><body>secondary static document</body></html>' > "$TEST_DIR/apps/web/build/weltweberei/index.html"
run_test "secondary-html-without-csp" 1
printf '%s\n' '<html><head><meta http-equiv="content-security-policy" content="script-src '\''none'\''"></head><body><main>secondary static document</main></body></html>' > "$TEST_DIR/apps/web/build/weltweberei/index.html"
run_test "secondary-html-strict" 0

printf '%s\n' '<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0h1v1z"/></svg>' > "$TEST_DIR/apps/web/build/favicon.svg"
run_test "passive-svg" 0
printf '%s\n' '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>' > "$TEST_DIR/apps/web/build/favicon.svg"
run_test "active-svg-rejected" 1
printf '%s\n' '<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0h1v1z"/></svg>' > "$TEST_DIR/apps/web/build/favicon.svg"

printf '%s\n' '<html><head><meta http-equiv="content-security-policy" content="script-src '\''self'\''"><script>console.log("inline")</script></head><body></body></html>' > "$INDEX_HTML"
run_test "inline-without-hash" 1

hash=$(
  python3 - << 'PY'
import base64, hashlib
body='console.log("inline")'
print(base64.b64encode(hashlib.sha256(body.encode()).digest()).decode())
PY
)
printf '%s\n' "<html><head><meta http-equiv=\"content-security-policy\" content=\"script-src 'self' 'sha256-$hash'\"><script>console.log(\"inline\")</script></head><body></body></html>" > "$INDEX_HTML"
run_test "inline-with-matching-hash" 0

printf '%s\n' "<html><head><script>console.log(\"inline\")</script><meta http-equiv=\"content-security-policy\" content=\"script-src 'self' 'sha256-$hash'\"></head><body></body></html>" > "$INDEX_HTML"
run_test "csp-meta-after-executable-script-rejected" 1

printf '%s\n' '<html><head><meta http-equiv="content-security-policy" content="script-src '\''self'\''"><script type="application/json">{"safe":true}</script></head><body></body></html>' > "$INDEX_HTML"
run_test "json-data-script-does-not-require-hash" 0

printf '%s\n' '<html><head><meta http-equiv="content-security-policy" content="script-src '\''self'\''"><script type="module">console.log("module")</script></head><body></body></html>' > "$INDEX_HTML"
run_test "inline-module-without-hash-rejected" 1

printf '%s\n' '<html><head><meta http-equiv="content-security-policy" content="script-src '\''self'\'' '\''unsafe-inline'\''"><script>console.log("inline")</script></head><body></body></html>' > "$INDEX_HTML"
run_test "inline-unsafe-inline-rejected" 1

printf '%s\n' 'header { Content-Security-Policy "script-src '\''self'\'' '\''unsafe-inline'\'';" }' > "$CADDYFILE_PATH"
run_test "edge-unsafe-inline-rejected" 1

printf '%s\n' 'header @frontendResponse Content-Security-Policy "style-src '\''self'\''; frame-ancestors '\''none'\'';"' > "$CADDYFILE_PATH"

printf '%s\n' '<html><body><meta http-equiv="content-security-policy" content="script-src '\''self'\''"></body></html>' > "$INDEX_HTML"
run_test "csp-meta-outside-head-rejected" 1

printf '%s\n' '<html><head><meta http-equiv="content-security-policy" content="script-src '\''self'\''"></head><body onclick="alert(1)"></body></html>' > "$INDEX_HTML"
run_test "inline-event-handler-rejected" 1

printf '%s\n' '<html><head><meta http-equiv="content-security-policy" content="script-src '\''self'\''"></head><body><a href="javascript:alert(1)">x</a></body></html>' > "$INDEX_HTML"
run_test "javascript-url-rejected" 1

printf '%s\n' '<html><head><meta http-equiv="content-security-policy" content="script-src '\''self'\''"><script type="text/javascript; charset=utf-8">console.log("mime")</script></head><body></body></html>' > "$INDEX_HTML"
run_test "parameterized-javascript-mime-requires-hash" 1

printf '%s\n' 'header @frontendResponse Content-Security-Policy "default-src '\''self'\''; style-src '\''self'\''; frame-ancestors '\''none'\'';"' > "$CADDYFILE_PATH"
run_test "frontend-edge-default-src-rejected" 1

printf '%s\n' 'header @frontendResponse Content-Security-Policy "script-src '\''self'\''; style-src '\''self'\''; frame-ancestors '\''none'\'';"' > "$CADDYFILE_PATH"
run_test "frontend-edge-script-src-rejected" 1

# Styles have no per-document policy to delegate to, so the edge header itself
# must carry a closed style-src.
printf '%s\n' 'header @frontendResponse Content-Security-Policy "frame-ancestors '\''none'\'';"' > "$CADDYFILE_PATH"
run_test "frontend-edge-missing-style-src-rejected" 1

printf '%s\n' 'header @frontendResponse Content-Security-Policy "style-src '\''self'\'' '\''unsafe-inline'\''; frame-ancestors '\''none'\'';"' > "$CADDYFILE_PATH"
run_test "frontend-edge-style-unsafe-inline-rejected" 1

printf '%s\n' 'header @frontendResponse Content-Security-Policy "style-src '\''self'\''; frame-ancestors '\''none'\'';"' > "$CADDYFILE_PATH"

printf '%s\n' '<html><head><meta http-equiv="content-security-policy" content="script-src '\''self'\''"></head><body><div style="display: contents"></div></body></html>' > "$INDEX_HTML"
run_test "inline-style-attribute-rejected" 1

printf '%s\n' '<html><head><meta http-equiv="content-security-policy" content="script-src '\''self'\''"><style>body{margin:0}</style></head><body></body></html>' > "$INDEX_HTML"
run_test "inline-style-element-rejected" 1

printf '%s\n' '<html><head><meta http-equiv="content-security-policy" content="script-src '\''self'\''"></head><body></body></html>' > "$INDEX_HTML"
run_test "no-inline-style-accepted" 0

echo "All csp_contract_static tests passed"
