#!/usr/bin/env bash
# shellcheck disable=SC2012
set -euo pipefail

# Regression test: when the deploy target's frontend mode requires the
# frontend (REQUIRE_FRONTEND=1) and the build artifact is missing/stale, a
# missing `node`/`pnpm` toolchain must hard-abort the deploy instead of only
# warning. When the frontend is NOT required (REQUIRE_FRONTEND=0), the same
# missing toolchain must only warn and let the deploy continue.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" > /dev/null 2>&1 && pwd)"
REPO_ROOT="$(dirname "$(dirname "$SCRIPT_DIR")")"
SCRIPT_SOURCE="$REPO_ROOT/scripts/weltgewebe-up"

WORKDIR_ROOT="$(mktemp -d)"
EDGE_CA_FIXTURE="$WORKDIR_ROOT/edge-ca.crt"
printf 'test-ca\n' > "$EDGE_CA_FIXTURE"
cleanup() {
  rm -rf "$WORKDIR_ROOT"
}
trap cleanup EXIT

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

assert_contains() {
  local haystack="$1"
  local needle="$2"
  if ! grep -Fq "$needle" <<< "$haystack"; then
    fail "expected output to contain: $needle"
  fi
}

assert_not_contains() {
  local haystack="$1"
  local needle="$2"
  if grep -Fq "$needle" <<< "$haystack"; then
    fail "expected output to NOT contain: $needle"
  fi
}

# A minimal repo: no remote needed since every run below uses --no-pull, but
# weltgewebe-up still resolves HEAD via git even in that mode.
new_repo() {
  local name="$1"
  local repo="$WORKDIR_ROOT/$name/repo"

  mkdir -p "$repo"
  (
    cd "$repo"
    git init -q
    git config user.name "Weltgewebe Test"
    git config user.email "tests@weltgewebe.local"

    mkdir -p infra/compose scripts apps/web map-style build/basemap
    printf '{}\n' > map-style/style-germany.json
    printf '{}\n' > map-style/style-germany-dark.json
    {
      printf 'PMTiles'
      printf '0%.0s' {1..120}
    } > build/basemap/basemap-germany.pmtiles
    printf '{}\n' > build/basemap/basemap-germany.meta.json
    cp "$SCRIPT_SOURCE" scripts/weltgewebe-up
    chmod +x scripts/weltgewebe-up

    cat > .env << 'EOF'
WEB_UPSTREAM_URL=https://example.com
WEB_UPSTREAM_HOST=example.com
EOF

    cat > infra/compose/compose.prod.yml << 'EOF'
services:
  api:
    image: busybox
EOF

    cat > infra/compose/compose.prod.override.yml << 'EOF'
services:
  api:
    image: busybox
EOF

    git add .
    git commit -q -m "test: initial checkout"
  )

  echo "$repo"
}

# Mocks: docker/curl/getent always behave successfully. `with_pnpm` controls
# whether node/pnpm are present on PATH, so each scenario controls the exact
# "missing toolchain" condition under test.
setup_mocks() {
  local with_pnpm="$1"
  local mock_root
  mock_root="$(mktemp -d "$WORKDIR_ROOT/mocks-XXXXXX")"
  local mock_bin="$mock_root/mock-bin"
  mkdir -p "$mock_bin"

  cat > "$mock_bin/docker" << 'EOF'
#!/usr/bin/env bash
set -euo pipefail
ARGS="$*"
if [[ "$1" == "ps" ]]; then
  exit 0
fi
if [[ "$1" == "inspect" ]]; then
  if [[ "$ARGS" == *"Aliases"* ]]; then
    echo "[weltgewebe-api]"
  else
    echo "{}"
  fi
  exit 0
fi
if [[ "$1" == "compose" ]]; then
  if [[ "$ARGS" == *" config --services"* ]]; then
    echo "api"
    exit 0
  fi
  if [[ "$ARGS" == *" config --format json"* ]]; then
    echo '{"services":{"api":{"ports":[],"networks":{"default":{"aliases":["weltgewebe-api"]}}}}}'
    exit 0
  fi
  if [[ "$ARGS" == *" config"* ]]; then
    echo "services: {}"
    exit 0
  fi
  if [[ "$ARGS" == *" ps -q api"* ]]; then
    echo "api_container_id"
    exit 0
  fi
  if [[ "$ARGS" == *" ps --format"* ]]; then
    echo "weltgewebe-api-1 running healthy"
    exit 0
  fi
  if [[ "$ARGS" == *" port api"* ]]; then
    echo "0.0.0.0:18080"
    exit 0
  fi
  if [[ "$ARGS" == *" exec -T api wget -qO- http://localhost:8080/health/ready"* ]]; then
    echo '{"status":"ok"}'
    exit 0
  fi
  if [[ "$ARGS" == *" up -d"* ]]; then
    exit 0
  fi
  exit 0
fi
exit 0
EOF

  cat > "$mock_bin/curl" << 'EOF'
#!/usr/bin/env bash
set -euo pipefail
ARGS="$*"
if [[ "$ARGS" == *"-w"* ]]; then
  echo "200"
  exit 0
fi
if [[ "$ARGS" == *"-I"* || "$ARGS" == *"-SI"* ]]; then
  printf 'HTTP/2 200\r\ncache-control: no-store\r\nx-weltgewebe-build: test-build\r\n\r\n'
  exit 0
fi
if [[ "$ARGS" == *"/_app/version.json"* ]]; then
  echo '{"version":"test-build","build_id":"test-build"}'
  exit 0
fi
if [[ "$ARGS" == *"/local-basemap/style-germany-dark.json"* ]]; then
  if [[ "${MOCK_FAIL_GERMANY_DARK:-0}" == "1" ]]; then
    exit 22
  fi
  printf '{}\n'
  exit 0
fi
if [[ "$ARGS" == *"/local-basemap/style-germany.json"* ]]; then
  printf '{}\n'
  exit 0
fi
if [[ "$ARGS" == *"/local-basemap/basemap-germany.pmtiles"* ]]; then
  if [[ "${MOCK_FAIL_GERMANY_PMTILES:-0}" == "1" ]]; then
    exit 22
  fi
  D_FILE=""
  O_FILE=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -D) D_FILE="$2"; shift 2 ;;
      -o) O_FILE="$2"; shift 2 ;;
      *) shift ;;
    esac
  done
  if [[ -n "$D_FILE" ]]; then
    printf 'HTTP/2 206\r\nContent-Type: application/octet-stream\r\nContent-Range: bytes 0-126/127\r\nContent-Length: 127\r\nAccept-Ranges: bytes\r\n\r\n' > "$D_FILE"
  fi
  if [[ -n "$O_FILE" ]]; then
    { printf 'PMTiles'; printf '0%.0s' {1..120}; } > "$O_FILE"
  else
    { printf 'PMTiles'; printf '0%.0s' {1..120}; }
  fi
  exit 0
fi
echo '{"status":"ok"}'
exit 0
EOF

  cat > "$mock_bin/getent" << 'EOF'
#!/usr/bin/env bash
echo "127.0.0.1 localhost"
exit 0
EOF

  chmod +x "$mock_bin/docker" "$mock_bin/curl" "$mock_bin/getent"

  if [[ "$with_pnpm" == "1" ]]; then
    cat > "$mock_bin/node" << 'EOF'
#!/usr/bin/env bash
exit 0
EOF
    cat > "$mock_bin/pnpm" << 'EOF'
#!/usr/bin/env bash
exit 0
EOF
    chmod +x "$mock_bin/node" "$mock_bin/pnpm"
  fi

  echo "$mock_bin"
}

# Runs weltgewebe-up with a minimal PATH: only the mock bin dir plus the
# handful of real system binaries the script needs for basic shell/git
# plumbing (bash builtins aside). This guarantees node/pnpm are only found
# when setup_mocks explicitly provided them.
run_up() {
  local repo="$1"
  local require_frontend="$2"
  local with_pnpm="$3"
  shift 3

  local mock_bin
  mock_bin="$(setup_mocks "$with_pnpm")"

  # A minimal real-tool PATH: git, coreutils, bash all typically live under
  # /usr/bin:/bin. This keeps the mock bin dir authoritative for
  # docker/curl/getent/node/pnpm while everything else still resolves.
  (
    cd "$repo"
    PATH="$mock_bin:/usr/bin:/bin" \
      REPO_DIR="$repo" \
      ENV_FILE="$repo/.env" \
      EDGE_CA="$EDGE_CA_FIXTURE" \
      DEPLOY_TARGET=heimserver \
      REQUIRE_FRONTEND="$require_frontend" \
      WELTGEWEBE_STATE_DIR="$repo/.ops" \
      MOCK_FAIL_GERMANY_DARK="${MOCK_FAIL_GERMANY_DARK:-0}" \
      MOCK_FAIL_GERMANY_PMTILES="${MOCK_FAIL_GERMANY_PMTILES:-0}" \
      bash scripts/weltgewebe-up --no-pull "$@"
  )
}

# 1) REQUIRE_FRONTEND=1, build artifact missing, node/pnpm missing:
#    must hard-abort before attempting anything else.
repo_required_missing_tool="$(new_repo required-missing-tool)"
set +e
out_required_missing_tool="$(run_up "$repo_required_missing_tool" 1 0 2>&1)"
rc_required_missing_tool=$?
set -e
[[ "$rc_required_missing_tool" -ne 0 ]] || fail "REQUIRE_FRONTEND=1 with missing node/pnpm must abort"
assert_contains "$out_required_missing_tool" "ERROR: Frontend build required for DEPLOY_FRONTEND_MODE"
assert_not_contains "$out_required_missing_tool" "up -d"

echo "PASS: REQUIRE_FRONTEND=1 + missing node/pnpm hard-aborts the deploy"

# 2) REQUIRE_FRONTEND=1, build artifact missing, node+pnpm present: the
#    frontend-build gate itself must not abort (it reaches "Building..."
#    without a missing-tool error). The mocked deploy may still fail later at
#    unrelated steps (e.g. the DNS-alias contract check) that this test does
#    not mock in full — that is out of scope here; only the frontend-build
#    gate is under test.
repo_required_with_tool="$(new_repo required-with-tool)"
set +e
out_required_with_tool="$(run_up "$repo_required_with_tool" 1 1 2>&1)"
set -e
assert_not_contains "$out_required_with_tool" "ERROR: Frontend build required for DEPLOY_FRONTEND_MODE"
assert_contains "$out_required_with_tool" ">> Frontend Build"
assert_contains "$out_required_with_tool" "Building..."

echo "PASS: REQUIRE_FRONTEND=1 + present node/pnpm builds without aborting"

# 3) REQUIRE_FRONTEND=0, build artifact missing, node/pnpm missing:
#    must only warn at the frontend-build gate, not abort there — other modes
#    must not break unnecessarily. As in scenario 2, later unrelated deploy
#    steps are not fully mocked and are out of scope for this assertion.
repo_not_required_missing_tool="$(new_repo not-required-missing-tool)"
set +e
out_not_required_missing_tool="$(run_up "$repo_not_required_missing_tool" 0 0 2>&1)"
set -e
assert_contains "$out_not_required_missing_tool" "WARNING: Frontend artifact missing but frontend delivery is not required for this deploy run."
assert_not_contains "$out_not_required_missing_tool" "ERROR: Frontend build required for DEPLOY_FRONTEND_MODE"
assert_not_contains "$out_not_required_missing_tool" "Nationwide Germany Basemap Pre-Build Guard"

echo "PASS: REQUIRE_FRONTEND=0 + missing node/pnpm skips Germany frontend prerequisites and continues"

# 4) An existing bundle from the explicit regional rollback must never be reused
#    when the normal Germany contract is requested on a later run of the same
#    commit. The basemap build identity, not just version.json/local route text,
#    decides whether the artifact is reusable.
repo_regional_stale="$(new_repo regional-stale)"
mkdir -p \
  "$repo_regional_stale/map-style" \
  "$repo_regional_stale/build/basemap" \
  "$repo_regional_stale/apps/web/build/_app/immutable"
printf '{}\n' > "$repo_regional_stale/map-style/style-germany.json"
printf '{}\n' > "$repo_regional_stale/map-style/style-germany-dark.json"
printf 'pmtiles\n' > "$repo_regional_stale/build/basemap/basemap-germany.pmtiles"
printf '{}\n' > "$repo_regional_stale/build/basemap/basemap-germany.meta.json"
printf '<!doctype html>\n' > "$repo_regional_stale/apps/web/build/index.html"
printf 'local-basemap\n' > "$repo_regional_stale/apps/web/build/_app/immutable/app.js"
cat > "$repo_regional_stale/apps/web/build/_app/basemap-build.json" << 'EOF'
{"schema_version":1,"mode":"local-sovereign","variant":"regional"}
EOF

set +e
out_regional_stale="$(run_up "$repo_regional_stale" 1 1 2>&1)"
rc_regional_stale=$?
set -e
[[ "$rc_regional_stale" -ne 0 ]] || fail "mocked build should still fail after leaving the stale regional identity unchanged"
assert_contains "$out_regional_stale" ">> Frontend Build (Auto: basemap contract mismatch)"

echo "PASS: normal Germany run rebuilds instead of reusing same-commit regional rollback bundle"

# 5) The non-VPS full-deploy postflight must fail closed when the new normal
#    Germany contract is selected but an actual Germany edge route is broken.
prepare_reusable_germany_build() {
  local repo="$1"
  mkdir -p "$repo/apps/web/build/_app/immutable"
  printf '<!doctype html><script src="/_app/immutable/app.js"></script>\n' > "$repo/apps/web/build/index.html"
  printf 'local-basemap\n' > "$repo/apps/web/build/_app/immutable/app.js"
  cat > "$repo/apps/web/build/_app/basemap-build.json" << 'EOF'
{"schema_version":1,"mode":"local-sovereign","variant":"germany"}
EOF
}

repo_edge_dark_fail="$(new_repo edge-dark-fail)"
prepare_reusable_germany_build "$repo_edge_dark_fail"
set +e
out_edge_dark_fail="$(MOCK_FAIL_GERMANY_DARK=1 run_up "$repo_edge_dark_fail" 1 1 --no-build-web 2>&1)"
rc_edge_dark_fail=$?
set -e
[[ "$rc_edge_dark_fail" -ne 0 ]] || fail "broken Germany dark-style edge route must abort"
assert_contains "$out_edge_dark_fail" "Nationwide Germany Basemap Edge Guard failed: Could not fetch style-germany-dark.json via Edge Route"

echo "PASS: Germany postflight fails closed when the dark style edge route is missing"

repo_edge_pmtiles_fail="$(new_repo edge-pmtiles-fail)"
prepare_reusable_germany_build "$repo_edge_pmtiles_fail"
set +e
out_edge_pmtiles_fail="$(MOCK_FAIL_GERMANY_PMTILES=1 run_up "$repo_edge_pmtiles_fail" 1 1 --no-build-web 2>&1)"
rc_edge_pmtiles_fail=$?
set -e
[[ "$rc_edge_pmtiles_fail" -ne 0 ]] || fail "broken Germany PMTiles edge route must abort"
assert_contains "$out_edge_pmtiles_fail" "Nationwide Germany Basemap Edge Guard failed: Could not fetch the bounded basemap-germany.pmtiles range via Edge Route"

echo "PASS: Germany postflight fails closed when the PMTiles edge route is missing"

repo_edge_ok="$(new_repo edge-ok)"
prepare_reusable_germany_build "$repo_edge_ok"
set +e
out_edge_ok="$(run_up "$repo_edge_ok" 1 1 --no-build-web 2>&1)"
set -e
assert_contains "$out_edge_ok" "OK (nationwide Germany light/dark styles and PMTiles range verified via Edge Route)"

echo "PASS: Germany postflight accepts exact light/dark styles and bound PMTiles range"

echo "test_weltgewebe_up_frontend_required: OK"
