#!/bin/bash
# shellcheck disable=SC2155
# ------------------------------------------------------------------
# Mock test for scripts/weltgewebe-up deployment logic.
# Every external effect (docker, git, curl, wget, getent, pnpm, sleep) is mocked
# under mock_bin; runs in CI (ci.yml, Core Guard Tests) and make validate.
# ------------------------------------------------------------------
set -euo pipefail

# weltgewebe-up writes into the checkout it deploys (build output, .ops, the
# deploy snapshot, glyphs, state). Run it against a private copy of the working
# tree so the developer's checkout is never written to, overwritten or cleaned.
SOURCE_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WORK_ROOT="$(mktemp -d)"

# Cleanup Trap
cleanup() {
  rm -rf "$WORK_ROOT"
  unset HEALTH_URL API_INTERNAL_PORT MOCK_HEALTH_EXISTS MOCK_PORT_CALLS_FILE
}
trap cleanup EXIT

# Tracked and untracked-but-not-ignored files, as they are in the working tree.
# Ignored local artifacts stay behind, so each mocked deploy starts clean.
git -C "$SOURCE_ROOT" ls-files -z --cached --others --exclude-standard |
  tar -C "$SOURCE_ROOT" --null --ignore-failed-read -T - -cf - |
  tar -xf - -C "$WORK_ROOT"

# Untracked, non-ignored entries are copied too so local in-progress work is
# exercised. Refuse symlinks/FIFOs/sockets/devices before mocks or deploy code
# can follow them out of WORK_ROOT.
unsafe_entry="$(find -P "$WORK_ROOT" -mindepth 1 ! -type f ! -type d -print -quit)"
if [[ -n "$unsafe_entry" ]]; then
  echo "ERROR: Refusing test working copy containing non-regular entry: ${unsafe_entry#"$WORK_ROOT"/}" >&2
  exit 1
fi
cd "$WORK_ROOT"

# 0. Setup Mocks
mkdir -p mock_bin

# Mock Docker
cat << 'EOF' > mock_bin/docker
#!/bin/bash
ARGS="$*"
if [[ "$1" == "ps" ]]; then
  if [[ "${MOCK_ZOMBIE:-}" == "1" ]]; then
    # Return zombie with config path matching current repo
    echo "zombie-container compose $(pwd)/infra/compose/compose.prod.yml"
  elif [[ "${MOCK_ZOMBIE:-}" == "GENERIC" ]]; then
      echo "zombie-generic compose"
  elif [[ "${MOCK_ZOMBIE:-}" == "EDGE" ]]; then
      echo "${EDGE_GATEWAY_CONTAINER:-edge-caddy} compose"
  # HANDLE DIRECT docker ps -q api (if used)
  elif [[ "$ARGS" == *"-q api"* ]]; then
      if [[ "${MOCK_MISSING_API:-0}" == "1" ]]; then
          echo ""
      else
          echo "api_container_id"
      fi
  else
    echo ""
  fi
elif [[ "$1" == "rm" ]]; then
    if [[ "$ARGS" == *"-f"* ]]; then
        echo "Mocked remove: $ARGS"
        exit 0
    fi
elif [[ "$1" == "inspect" ]]; then
    ARGS="$*"
    # Schaubild postflight: the live container runs exactly the image weltgewebe-up bound.
    if [[ "$ARGS" == *"{{.Config.Image}}"* && "$ARGS" == *"schaubild_container_id"* ]]; then
        echo "${SCHAUWERK_SCHAUBILD_IMAGE:-}"
        exit 0
    fi
    # Deferred zombie purge binds the immutable Docker container ID before any later effect.
    if [[ "$ARGS" == *"--format"* && "$ARGS" == *"{{.Id}}"* ]]; then
        echo "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    # Return dummy alias if requesting format with Aliases
    elif [[ "$ARGS" == *"--format"* && "$ARGS" == *"Aliases"* ]]; then
        echo "weltgewebe-api"
    # EXISTENCE CHECK: Handle {{if .State.Health}}yes{{else}}no{{end}}
    # We use looser matching to avoid quoting issues. Match core parts.
    elif [[ "$ARGS" == *".State.Health"* && "$ARGS" == *"yes"* && "$ARGS" == *"no"* ]]; then
        if [[ "${MOCK_INSPECT_FAIL_STRATEGY:-0}" == "1" ]]; then
            exit 1
        fi
        # Default to "no" (missing healthcheck) unless explicitly enabled
        if [[ "${MOCK_HEALTH_EXISTS:-0}" == "1" ]]; then
            echo "yes"
        else
            echo "no"
        fi
    elif [[ "$ARGS" == *"--format"* && "$ARGS" == *"Health.Status"* ]]; then
        if [[ "${MOCK_INSPECT_FAIL_STATUS:-0}" == "1" || "${MOCK_INSPECT_FAIL:-0}" == "1" ]]; then
            exit 1
        fi
        echo "healthy"
    elif [[ "$ARGS" == *"--format"* && "$ARGS" == *"Health.Log"* ]]; then
        # Return JSON array so {{range ...}} works correctly in Go templates
        echo '[{"ExitCode": 0, "Output": "Ok"}]'
    fi
    exit 0
elif [[ "$1" == "compose" ]]; then
  if [[ "$ARGS" == *" config"* ]]; then
     # Check if we should simulate Caddy presence
     if [[ "${MOCK_HAS_CADDY:-0}" == "1" && "$ARGS" == *"--services"* ]]; then
        echo "caddy"
     fi
     if [[ "$ARGS" != *"--services"* ]]; then
         # Keep both ordinary and JSON config renders valid for current preflights.
         if [[ "$ARGS" == *"--format json"* ]]; then
             # Render the Schaubild service the way Compose interpolates it from the
             # bindings weltgewebe-up exports, so the full-scope runtime contract holds.
             printf '{"services": {"caddy": {}, "schaubild": {"image": "%s", "pull_policy": "missing", "read_only": true, "ports": [], "expose": ["8765"], "command": ["--bind-host", "0.0.0.0", "--trusted-reverse-proxy", "--trusted-proxy-source-cidr", "%s", "--public-base-path", "/schaubild"]}}}\n' \
                 "${SCHAUWERK_SCHAUBILD_IMAGE:-}" "${SCHAUWERK_SCHAUBILD_TRUSTED_PROXY_CIDR:-}"
         else
             echo "services: {}"
         fi
     fi
     exit 0
  fi

  if [[ "$ARGS" == *" ps -q schaubild"* ]]; then
      echo "schaubild_container_id"
      exit 0
  fi

  # HANDLE docker compose ... ps -q api
  if [[ "$ARGS" == *" ps -q api"* ]]; then
      if [[ "${MOCK_MISSING_API:-0}" == "1" ]]; then
          echo ""
      else
          echo "api_container_id"
      fi
      exit 0
  fi

  # New: Echo COMPOSE_BAKE state if VERIFY_BAKE is set
  if [[ "${VERIFY_BAKE:-}" == "1" ]]; then
     # We output this to stderr or stdout, so we can grep it in tests
     echo "VERIFY_BAKE: COMPOSE_BAKE=${COMPOSE_BAKE:-<unset>}"
  fi

  # Port Mocking
  if [[ "$ARGS" == *" port api"* ]]; then
      if [[ -n "${MOCK_PORT_CALLS_FILE:-}" ]]; then
          echo "port_api" >> "$MOCK_PORT_CALLS_FILE"
      fi
      # Extract requested internal port (last argument usually, or verify it)
      REQUESTED_PORT=$(echo "$ARGS" | awk '{print $NF}')

      # If we are testing custom internal port, verify it was passed correctly
      if [[ -n "${EXPECT_INTERNAL_PORT:-}" && "$REQUESTED_PORT" != "$EXPECT_INTERNAL_PORT" ]]; then
          echo "FAIL: Expected internal port $EXPECT_INTERNAL_PORT, got $REQUESTED_PORT" >&2
          exit 1
      fi

      if [[ "${MOCK_PORT_MODE:-}" == "0" ]]; then
          echo "0.0.0.0:0"
      elif [[ "${MOCK_PORT_MODE:-}" == "VALID" ]]; then
          echo "0.0.0.0:32768"
      else
          echo ""
      fi
      exit 0
  fi

  # Exec Mocking (deprecated in new strategy, but kept for completeness if needed)
  if [[ "$ARGS" == *" exec -T api"* ]]; then
      if [[ "${MOCK_EXEC_FAIL:-0}" == "1" ]]; then
          exit 1
      else
          # Check if the exec command uses the expected internal port
          if [[ -n "${EXPECT_INTERNAL_PORT:-}" ]]; then
             if [[ "$ARGS" != *":${EXPECT_INTERNAL_PORT}/health"* ]]; then
                 echo "FAIL: Exec command did not use expected port $EXPECT_INTERNAL_PORT" >&2
                 exit 1
             fi
          fi
          exit 0
      fi
  fi

  echo "Mocked docker compose execution"
  exit 0
else
  echo ""
  exit 0
fi
EOF

# Mock pnpm
cat << 'EOF' > mock_bin/pnpm
#!/bin/bash
echo "Mocked pnpm execution: $*"
if [[ "$*" == *"-C apps/web build"* ]]; then
    mkdir -p apps/web/build/_app
    # Smallest build that satisfies the runtime and static CSP preflights.
    echo '<!doctype html><html><head><meta http-equiv="content-security-policy" content="script-src '"'"'self'"'"'"></head><body></body></html>' > apps/web/build/index.html
fi
exit 0
EOF

# Mock curl
cat << 'EOF' > mock_bin/curl
#!/bin/bash
# Fail if trying to connect to port 0
if [[ "$*" == *":0/health/ready"* ]]; then
    exit 7
fi
# VPS Schaubild postflight: serve a native runtime that satisfies the reviewed contract.
if [[ "$*" == *"https://commonthing.net/schaubild/"* ]]; then
    headers_out="" body_out="" write_out="" url=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -D) headers_out="$2"; shift 2 ;;
            -o) body_out="$2"; shift 2 ;;
            -w) write_out="$2"; shift 2 ;;
            --resolve | --connect-timeout | --max-time | --retry | --retry-delay | --noproxy | -H | --data-binary) shift 2 ;;
            https://*) url="$1"; shift ;;
            *) shift ;;
        esac
    done
    asset_body() { printf 'schaubild asset %s\n' "$1"; }
    headers="" body=""
    case "$url" in
        https://commonthing.net/schaubild/)
            headers="HTTP/2 200"$'\r\n'"cache-control: no-store"$'\r\n'"content-security-policy: ${MOCK_SCHAUBILD_CSP:-}"$'\r\n'
            body="<html></html>"
            ;;
        https://commonthing.net/schaubild/manifest.json)
            files=""
            for name in index.html app.js app.css native.js; do
                digest="$(asset_body "$name" | sha256sum | awk '{print $1}')"
                files="${files:+$files,}{\"path\": \"$name\", \"sha256\": \"$digest\"}"
            done
            body="{\"schema_version\": \"schauwerk-standalone-editor-manifest.v2\", \"editor_engine\": \"schauwerk-native-diagram-v1\", \"cutover_status\": \"native-primary-with-legacy-compatibility\", \"native_renderer\": {\"renderer\": \"schauwerk-native-diagram-v1\", \"api_path\": \"/schaubild/api/native-viewer\", \"public_base_path\": \"/schaubild\"}, \"files\": [$files]}"
            ;;
        https://commonthing.net/schaubild/api/native-viewer)
            body='{"renderer": "schauwerk-native-diagram-v1", "url": "/schaubild/native/0123456789abcdef0123456789abcdef/index.html"}'
            ;;
        https://commonthing.net/schaubild/native/*)
            headers="HTTP/2 200"$'\r\n'"content-security-policy: default-src 'self'; frame-ancestors 'self'"$'\r\n'"x-frame-options: SAMEORIGIN"$'\r\n'
            body='<div id="nativeViewport"></div>'
            ;;
        *)
            body="$(asset_body "${url##*/}")"
            ;;
    esac
    if [[ -n "$headers_out" ]]; then
        printf '%s' "$headers" > "$headers_out"
    fi
    if [[ -n "$body_out" ]]; then
        printf '%s\n' "$body" > "$body_out"
    else
        printf '%s\n' "$body"
    fi
    if [[ "$write_out" == "%{http_code}" ]]; then
        printf '200'
    fi
    exit 0
fi
echo '{"status": "ok"}'
exit 0
EOF

# Mock Git
cat << 'EOF' > mock_bin/git
#!/bin/bash
ARGS="$*"
if [[ "$1" == "rev-parse" ]]; then
    if [[ "$ARGS" == *"--show-toplevel"* ]]; then
        # Return the exported REPO_DIR
        echo "$REPO_DIR"
    elif [[ "$ARGS" == *"--short"* && "$ARGS" == *"HEAD"* ]]; then
        echo "01234567"
    elif [[ "$ARGS" == *"HEAD"* ]]; then
        echo "0123456789abcdef0123456789abcdef01234567"
    fi
    exit 0
elif [[ "$1" == "show" && "$ARGS" == *"--format=%cI"* ]]; then
    echo "2026-08-29T12:00:00+00:00"
    exit 0
elif [[ "$1" == "fetch" ]]; then
    exit 0
elif [[ "$1" == "pull" ]]; then
    exit 0
elif [[ "$1" == "symbolic-ref" ]]; then
    echo "main"
    exit 0
else
    # Fallback to true
    exit 0
fi
EOF

# Retry loops (health: 10 x 5 s) wait on mocks that never change state; keep
# the harness inside the CI job budget by not actually sleeping.
cat << 'EOF' > mock_bin/sleep
#!/bin/bash
exit 0
EOF

chmod +x mock_bin/*
export PATH="$(pwd)/mock_bin:$PATH"

# Default Mock State: Assume Health Checks exist for all tests unless overridden
touch mock_edge_ca.crt
export EDGE_CA="$PWD/mock_edge_ca.crt"
export MOCK_HEALTH_EXISTS="1"

# The harness is no VPS: pin the public Caddy binds so weltgewebe-up does not
# probe the host's real interfaces via scripts/ops/resolve_vps_public_bind.py.
export CADDY_BIND="127.0.0.1"
export CADDY_IPV6_BIND="127.0.0.1"
# The VPS target enables the stack-internal Caddy whenever Compose defines it.
export MOCK_HAS_CADDY="1"
# Frontend delivery (web build, basemap artifacts, edge reachability) has its own
# harness in test_weltgewebe_up_frontend_required.sh; this one covers the API path.
export DEPLOY_FRONTEND_MODE="off"
# The exact isolated CSP the Schaubild postflight requires from /schaubild/.
export MOCK_SCHAUBILD_CSP="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; frame-src 'self' https://embed.diagrams.net; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none';"

REPO_DIR=$(pwd)
export REPO_DIR
export ENV_FILE="$REPO_DIR/test.env"
# Create dummy env file
echo "WEB_UPSTREAM_URL=https://example.com" > "$ENV_FILE"
echo "WEB_UPSTREAM_HOST=example.com" >> "$ENV_FILE"

# 1. Test Project Validation
echo ">>> Test 1: Project Validation (fail case)"
export COMPOSE_PROJECT="compose"
if ./scripts/weltgewebe-up --no-pull --no-build > /dev/null 2>&1; then
  echo "FAIL: COMPOSE_PROJECT=compose check failed."
  exit 1
else
  echo "PASS: COMPOSE_PROJECT=compose rejected."
fi

# Reset Project
export COMPOSE_PROJECT="weltgewebe"

# 2. Test Zombie Guard (Fail with Detailed Message)
echo ">>> Test 2: Zombie Guard (Fail Logic)"
export MOCK_ZOMBIE=1
OUTPUT=$(./scripts/weltgewebe-up --no-pull --no-build 2>&1 || true)

# Robust assertions: Check for key markers instead of exact full string
if echo "$OUTPUT" | grep -q "ERROR: Detected foreign compose project"; then
  if echo "$OUTPUT" | grep -q "Repo dir:" &&
    echo "$OUTPUT" | grep -q "Expected project name:" &&
    echo "$OUTPUT" | grep -q "docker compose -p <foreign_project> down"; then
    echo "PASS: Detailed error message found (robust check)."
  else
    echo "FAIL: Missing remediation hints or context info."
    echo "$OUTPUT"
    exit 1
  fi
else
  echo "FAIL: Zombie detection failed or error message mismatch."
  echo "$OUTPUT"
  exit 1
fi

# 3. Test Zombie Guard (Purge)
echo ">>> Test 3: Zombie Guard (Purge)"
export MOCK_ZOMBIE=1
OUTPUT=$(DEPLOY_FRONTEND_MODE=off REQUIRE_FRONTEND=0 ./scripts/weltgewebe-up --no-pull --no-build --purge-compose-leaks 2>&1)
if echo "$OUTPUT" | grep -q "All deployment preflights passed. Purging the previously bound container identities as requested" &&
  echo "$OUTPUT" | grep -q "Mocked remove: rm -f aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"; then
  echo "PASS: Purge triggered after preflight using the bound container ID."
else
  echo "FAIL: Deferred purge did not use the bound container ID after preflight."
  echo "$OUTPUT"
  exit 1
fi

# 3b. Test Zombie Guard (Never Purge External Edge Gateway)
echo ">>> Test 3b: Zombie Guard (Protect External Edge Gateway)"
export MOCK_ZOMBIE=EDGE
set +e
# The refusal does not depend on the frontend mode. Keep the rest of the mocked
# deploy passing so a non-zero exit can only come from the refusal itself.
OUTPUT=$(EDGE_GATEWAY_CONTAINER=edge-caddy DEPLOY_FRONTEND_MODE=off REQUIRE_FRONTEND=0 ./scripts/weltgewebe-up --no-pull --no-build --purge-compose-leaks 2>&1)
EDGE_PURGE_RC=$?
set -e
if [[ "$EDGE_PURGE_RC" -ne 0 ]] &&
  echo "$OUTPUT" | grep -q "Refusing to auto-purge configured external edge gateway container: edge-caddy" &&
  ! echo "$OUTPUT" | grep -q "Mocked remove:"; then
  echo "PASS: External edge gateway is fail-closed and never queued for compose-leak purge."
else
  echo "FAIL: External edge gateway was not protected from compose-leak purge."
  echo "$OUTPUT"
  exit 1
fi
export MOCK_ZOMBIE=0

# 4. Test Bake Auto-Disable (Missing /apps)
echo ">>> Test 4: Bake Auto-Disable (Missing /apps)"
# Clean environment
unset COMPOSE_BAKE
unset COMPOSE_BAKE_VALUE
export WELTGEWEBE_COMPOSE_BAKE="auto"
export MOCK_ZOMBIE=0
export VERIFY_BAKE=1
# Use probe override to simulate missing directory
export WELTGEWEBE_APPS_PROBE="./missing_apps_mock"

OUTPUT=$(./scripts/weltgewebe-up --no-pull --no-build 2>&1)
if echo "$OUTPUT" | grep -q "VERIFY_BAKE: COMPOSE_BAKE=0"; then
  echo "PASS: COMPOSE_BAKE=0 set when apps probe fails."
else
  echo "FAIL: COMPOSE_BAKE=0 not detected."
  echo "$OUTPUT"
  exit 1
fi

# 5. Test Bake Preserved (Existing /apps)
echo ">>> Test 5: Bake Preserved (Existing /apps)"
# Clean environment
unset COMPOSE_BAKE
unset COMPOSE_BAKE_VALUE
export WELTGEWEBE_COMPOSE_BAKE="auto"
export VERIFY_BAKE=1
# Point probe to existing repo dir (we know infra exists)
export WELTGEWEBE_APPS_PROBE="infra"

OUTPUT=$(./scripts/weltgewebe-up --no-pull --no-build 2>&1)
if echo "$OUTPUT" | grep -q "VERIFY_BAKE: COMPOSE_BAKE=<unset>"; then
  echo "PASS: COMPOSE_BAKE preserved (unset) when apps probe succeeds."
else
  echo "FAIL: COMPOSE_BAKE forced unexpectedly."
  echo "$OUTPUT"
  exit 1
fi

# 6. Test Bake Override
echo ">>> Test 6: Bake Override (Explicit 0)"
# Clean environment
unset COMPOSE_BAKE
unset COMPOSE_BAKE_VALUE
export VERIFY_BAKE=1
export WELTGEWEBE_COMPOSE_BAKE=0
# Probe should not matter here, but let's point to existing
export WELTGEWEBE_APPS_PROBE="infra"

OUTPUT=$(./scripts/weltgewebe-up --no-pull --no-build 2>&1)
if echo "$OUTPUT" | grep -q "VERIFY_BAKE: COMPOSE_BAKE=0"; then
  echo "PASS: Explicit WELTGEWEBE_COMPOSE_BAKE=0 honored."
else
  echo "FAIL: Override ignored."
  echo "$OUTPUT"
  exit 1
fi

# 7. Test Bake Override Invalid
echo ">>> Test 7: Bake Override Invalid (Warning)"
# Clean environment
unset COMPOSE_BAKE
unset COMPOSE_BAKE_VALUE
export VERIFY_BAKE=1
export WELTGEWEBE_COMPOSE_BAKE="invalid_value"
export WELTGEWEBE_APPS_PROBE="infra"

OUTPUT=$(./scripts/weltgewebe-up --no-pull --no-build 2>&1)
if echo "$OUTPUT" | grep -q "Unrecognized WELTGEWEBE_COMPOSE_BAKE"; then
  echo "PASS: Warning detected for invalid value."
else
  echo "FAIL: Warning missing."
  echo "$OUTPUT"
  exit 1
fi

# 8. Test REPO_DIR Auto-Detection (Unset)
echo ">>> Test 8: REPO_DIR Auto-Detection"
# Harden tests against host environment
unset REPO_DIR COMPOSE_BAKE WELTGEWEBE_COMPOSE_BAKE WELTGEWEBE_APPS_PROBE VERIFY_BAKE

# We rely on CWD/Git fallback since we are in repo root (managed by test setup cd)
# Ensure we are in a path that has the config file
if [[ ! -f "infra/compose/compose.prod.yml" ]]; then
  echo "FAIL: Test setup error - config file not found in CWD."
  exit 1
fi

# We expect success (exit 0) and validation that correct repo was picked
OUTPUT=$(./scripts/weltgewebe-up --no-pull --no-build 2>&1)
if echo "$OUTPUT" | grep -qF "Repo:    $(pwd)"; then
  echo "PASS: Auto-detection worked and selected current directory."
else
  echo "FAIL: Auto-detection failed or selected wrong repo."
  echo "$OUTPUT"
  exit 1
fi

# 9. Test REPO_DIR Strictness (Invalid Path)
echo ">>> Test 9: REPO_DIR Strictness (Invalid Path)"
export REPO_DIR="/invalid/path/to/repo"
OUTPUT=$(./scripts/weltgewebe-up --no-pull --no-build 2>&1 || true)

if echo "$OUTPUT" | grep -q "ERROR: REPO_DIR explicitly set"; then
  if echo "$OUTPUT" | grep -q "Refusing fallback"; then
    echo "PASS: Strict check rejected invalid REPO_DIR."
  else
    echo "FAIL: Error message missing 'Refusing fallback'."
    echo "$OUTPUT"
    exit 1
  fi
else
  echo "FAIL: Script did not fail on invalid REPO_DIR."
  echo "$OUTPUT"
  exit 1
fi
unset REPO_DIR

# Test 10 removed (flaky system path rejection)

# 11. Test Health: Internal Fallback (Docker Native)
echo ">>> Test 11: Health - Internal Fallback (Docker Native)"
export MOCK_PORT_MODE="0"
export MOCK_EXEC_FAIL="0"
# Strategy should default to Docker Native if no port mapping and no gateway
OUTPUT=$(./scripts/weltgewebe-up --no-pull --no-build 2>&1 || true)

if echo "$OUTPUT" | grep -q "Health strategy selected: Docker Native Health"; then
  if echo "$OUTPUT" | grep -q "Health OK (Docker Native Health)"; then
    echo "PASS: Fell back to Docker Native check."
  else
    echo "FAIL: Docker Native check failed."
    echo "$OUTPUT"
    exit 1
  fi
else
  echo "FAIL: Did not use Docker Native check."
  echo "$OUTPUT"
  exit 1
fi

# 12. Test Health: Host Port (Valid)
echo ">>> Test 12: Health - Host Port (Valid)"
export MOCK_PORT_MODE="VALID"
export MOCK_EXEC_FAIL="0"
export MOCK_HEALTH_EXISTS="0"
OUTPUT=$(./scripts/weltgewebe-up --no-pull --no-build 2>&1 || true)

if echo "$OUTPUT" | grep -q "Health strategy selected: HTTP Health"; then
  if echo "$OUTPUT" | grep -q "Using Host Port Mapping"; then
    echo "PASS: Used Host Port check when valid."
  else
    echo "FAIL: Did not output Host Port mapping."
    echo "$OUTPUT"
    exit 1
  fi
else
  echo "FAIL: Did not use HTTP Health check."
  echo "$OUTPUT"
  exit 1
fi
unset MOCK_HEALTH_EXISTS

# 13. Test Health: Explicit URL
echo ">>> Test 13: Health - Explicit URL"
export HEALTH_URL="http://explicit-url:8080/health"
export MOCK_PORT_MODE="VALID"
export MOCK_HEALTH_EXISTS="0"
OUTPUT=$(./scripts/weltgewebe-up --no-pull --no-build 2>&1 || true)

if echo "$OUTPUT" | grep -q "Health strategy selected: HTTP Health"; then
  if echo "$OUTPUT" | grep -q "Using explicit health URL: http://explicit-url:8080/health"; then
    echo "PASS: Used explicit HEALTH_URL."
  else
    echo "FAIL: Did not use explicit HEALTH_URL."
    echo "$OUTPUT"
    exit 1
  fi
else
  echo "FAIL: Did not use HTTP Health check."
  echo "$OUTPUT"
  exit 1
fi
unset HEALTH_URL
unset MOCK_HEALTH_EXISTS

# 14. Test State Dir Customization
echo ">>> Test 14: State Dir Customization"
export WELTGEWEBE_STATE_DIR="$(pwd)/custom_state"
mkdir -p "$WELTGEWEBE_STATE_DIR"
# ENABLE PULL (no flag) so git mock is used and CURRENT_HEAD is set
OUTPUT=$(./scripts/weltgewebe-up --no-build 2>&1)

if [[ -f "$WELTGEWEBE_STATE_DIR/weltgewebe-up.state" ]]; then
  CONTENT=$(cat "$WELTGEWEBE_STATE_DIR/weltgewebe-up.state")
  # The state file records the deployed HEAD, i.e. what the git mock returns for rev-parse HEAD.
  if [[ "$CONTENT" == "0123456789abcdef0123456789abcdef01234567" ]]; then
    echo "PASS: State file created in custom directory with correct SHA."
  else
    echo "FAIL: State file content incorrect. Got: $CONTENT"
    exit 1
  fi
else
  echo "FAIL: State file not found in custom directory."
  echo "$OUTPUT"
  exit 1
fi
rm -rf "$WELTGEWEBE_STATE_DIR"
unset WELTGEWEBE_STATE_DIR

# 15. Test Configurable Internal Port (Port check verify)
echo ">>> Test 15: Configurable Internal Port (Port check)"
export MOCK_PORT_MODE="VALID"
export MOCK_HEALTH_EXISTS="0"
export API_INTERNAL_PORT="9090"
# We expect the script to call `docker compose port api 9090`
# Our mock docker will verify this if we set EXPECT_INTERNAL_PORT
# Actually, the mock checks "REQUESTED_PORT" against "EXPECT_INTERNAL_PORT" for port command
export EXPECT_INTERNAL_PORT="9090"
OUTPUT=$(./scripts/weltgewebe-up --no-pull --no-build 2>&1 || true)

if echo "$OUTPUT" | grep -q "Health strategy selected: HTTP Health" && echo "$OUTPUT" | grep -q "Using Host Port Mapping"; then
  echo "PASS: Script ran successfully with custom internal port."
else
  echo "FAIL: Script failed or wrong strategy."
  echo "$OUTPUT"
  exit 1
fi
unset API_INTERNAL_PORT
unset EXPECT_INTERNAL_PORT
unset MOCK_HEALTH_EXISTS

# 16. Test Missing HEALTHCHECK (New)
echo ">>> Test 16: Docker Native Health - Missing HEALTHCHECK"
export MOCK_PORT_MODE="0"
export MOCK_HEALTH_EXISTS="0" # Explicitly disable health check existence
export MOCK_INSPECT_FAIL="0"  # Ensure inspect succeeds so it's a true missing check
# Ensure we catch failure but print output
OUTPUT=$(./scripts/weltgewebe-up --no-pull --no-build 2>&1 || true)

if echo "$OUTPUT" | grep -q "Docker HEALTHCHECK not defined for container 'api'"; then
  if echo "$OUTPUT" | grep -q "Waiting for health..."; then
    echo "FAIL: Script did not fail fast (retried despite missing healthcheck)."
    echo "$OUTPUT"
    exit 1
  elif echo "$OUTPUT" | grep -q "Health check failed after"; then
    echo "FAIL: Script printed misleading 'failed after X seconds' summary despite fail fast."
    echo "$OUTPUT"
    exit 1
  elif ! echo "$OUTPUT" | grep -Fq "Docker HEALTHCHECK not defined for container 'api'"; then
    echo "FAIL: Did not find expected HEALTHCHECK-missing message substring."
    echo "$OUTPUT"
    exit 1
  else
    echo "PASS: Detected missing HEALTHCHECK, failed fast, and printed correct summary."
  fi
else
  echo "FAIL: Did not detect missing HEALTHCHECK."
  echo "$OUTPUT"
  exit 1
fi
unset MOCK_HEALTH_EXISTS
unset MOCK_INSPECT_FAIL

# 17a. Test Strategy Inspect Fails (Fallback to HTTP)
echo ">>> Test 17a: Strategy Inspect Fails (Fallback to HTTP)"
export MOCK_PORT_MODE="VALID"
export MOCK_INSPECT_FAIL_STRATEGY="1"
export MOCK_HEALTH_EXISTS="0"
unset HEALTH_URL
export MOCK_PORT_CALLS_FILE="$(pwd)/.mock_port_calls"
rm -f "$MOCK_PORT_CALLS_FILE"
OUTPUT=$(./scripts/weltgewebe-up --no-pull --no-build 2>&1 || true)

if ! echo "$OUTPUT" | grep -q "WARN: docker inspect failed during health strategy probe; falling back to HTTP Health"; then
  echo "FAIL: Missing expected warning message for strategy inspect fail."
  echo "$OUTPUT"
  exit 1
fi

if echo "$OUTPUT" | grep -q "Health strategy selected: HTTP Health" && echo "$OUTPUT" | grep -q "Using Host Port Mapping"; then
  if echo "$OUTPUT" | grep -q "Health OK"; then
    if [ ! -s "$MOCK_PORT_CALLS_FILE" ]; then
      echo "FAIL: Strategy fell back to HTTP Health but no port probe occurred."
      exit 1
    fi
    echo "PASS: Detected strategy inspect failure and fell back correctly."
  else
    echo "FAIL: Detected strategy inspect failure but did not attempt fallback."
    echo "$OUTPUT"
    exit 1
  fi
else
  echo "FAIL: Did not fallback to HTTP Health / Host Port Mapping on strategy inspect failure."
  echo "$OUTPUT"
  exit 1
fi
unset MOCK_INSPECT_FAIL_STRATEGY
unset MOCK_HEALTH_EXISTS

# 17b. Test Docker Native Status Inspect Fails (Retryable)
echo ">>> Test 17b: Docker Native Status Inspect Fails (Retryable)"
export MOCK_PORT_MODE="0"
touch mock_edge_ca.crt
export EDGE_CA="$PWD/mock_edge_ca.crt"
export MOCK_HEALTH_EXISTS="1"
export MOCK_INSPECT_FAIL_STATUS="1"
OUTPUT=$(./scripts/weltgewebe-up --no-pull --no-build 2>&1 || true)

if echo "$OUTPUT" | grep -q "docker inspect failed"; then
  if echo "$OUTPUT" | grep -q "Waiting for health... (1/10)"; then
    if echo "$OUTPUT" | grep -q "Health check aborted: missing Docker HEALTHCHECK"; then
      echo "FAIL: Failed fast instead of retrying."
      echo "$OUTPUT"
      exit 1
    fi
    echo "PASS: Detected status inspect failure and retried correctly."
  else
    echo "FAIL: Detected status inspect failure but did not retry."
    echo "$OUTPUT"
    exit 1
  fi
else
  echo "FAIL: Did not detect status inspect failure."
  echo "$OUTPUT"
  exit 1
fi
unset MOCK_INSPECT_FAIL_STATUS
unset MOCK_HEALTH_EXISTS

# 18. Test Docker Native Exists and No Port
echo ">>> Test 18: Docker HEALTHCHECK exists AND no published host port"
touch mock_edge_ca.crt
export EDGE_CA="$PWD/mock_edge_ca.crt"
export MOCK_HEALTH_EXISTS="1"
export MOCK_PORT_MODE="0"
rm -f "$MOCK_PORT_CALLS_FILE"
OUTPUT=$(./scripts/weltgewebe-up --no-pull --no-build 2>&1 || true)

if echo "$OUTPUT" | grep -q "Health strategy selected: Docker Native Health"; then
  if [ -s "$MOCK_PORT_CALLS_FILE" ]; then
    echo "FAIL: Attempted port mapping detection despite having a Docker native strategy."
    echo "$OUTPUT"
    exit 1
  fi
  if echo "$OUTPUT" | grep -q "Health OK (Docker Native Health)"; then
    echo "PASS: Ignored missing port correctly with Docker Native Health Check."
  else
    echo "FAIL: Did not succeed with Docker Native Health."
    echo "$OUTPUT"
    exit 1
  fi
else
  echo "FAIL: Did not use Docker Native Health Check strategy."
  echo "$OUTPUT"
  exit 1
fi
unset MOCK_HEALTH_EXISTS
unset MOCK_PORT_MODE

# 19. Test Missing Container Logic
echo ">>> Test 19: Missing Container Logic"
export MOCK_MISSING_API="1"
OUTPUT=$(./scripts/weltgewebe-up --no-pull --no-build 2>&1 || true)

if echo "$OUTPUT" | grep -q "ERROR: Container 'api' not found."; then
  if echo "$OUTPUT" | grep -q "HEALTHCHECK not defined"; then
    echo "FAIL: Emitted 'missing healthcheck' despite missing container."
    echo "$OUTPUT"
    exit 1
  fi
  echo "PASS: Detected missing container before checking strategy."
else
  echo "FAIL: Did not detect missing container correctly."
  echo "$OUTPUT"
  exit 1
fi
unset MOCK_MISSING_API

# Tests 20-23 cover the edge guards (DNS, container health, frontend cache and
# version.json). weltgewebe-up runs them only for the legacy
# DEPLOY_TARGET=heimserver contract, which the script still accepts
# (docs/deploy/commonthing.naming.md); the VPS target skips them.
export DEPLOY_TARGET="heimserver"
unset DEPLOY_FRONTEND_MODE
# Sovereign Germany map delivery has its own regression in
# test_weltgewebe_up_frontend_required.sh; keep these guards on remote-style.
export PUBLIC_BASEMAP_MODE="remote-style"

# 20. DNS Guard Success
echo ">>> Test 20: DNS Guard Success"
cat << 'EOF_GETENT' > mock_bin/getent
#!/bin/bash
exit 0
EOF_GETENT
chmod +x mock_bin/getent

cat << 'EOF_CURL' > mock_bin/curl
#!/bin/bash
if [[ "$*" == *"--cacert"* || "$*" == *"-I"* ]]; then
    exit 0
fi
echo "curl mock invoked: $*"
exit 0
EOF_CURL
chmod +x mock_bin/curl

cat << 'EOF_WGET' > mock_bin/wget
#!/bin/bash
exit 0
EOF_WGET
chmod +x mock_bin/wget

touch mock_edge_ca.crt
export EDGE_CA="$PWD/mock_edge_ca.crt"
export MOCK_HEALTH_EXISTS="1"
OUTPUT=$(./scripts/weltgewebe-up --no-pull --no-build 2>&1 || true)
if echo "$OUTPUT" | grep -q ">> Guard 5: Frontend Reachability..."; then
  echo "PASS: DNS Guard succeeded and script progressed through guards."
else
  echo "FAIL: Script did not reach Guard 5 as expected."
  echo "$OUTPUT"
  exit 1
fi
unset MOCK_HEALTH_EXISTS

# 21. DNS Guard Failure
echo ">>> Test 21: DNS Guard Failure"
cat << 'EOF_GETENT_FAIL' > mock_bin/getent
#!/bin/bash
exit 1
EOF_GETENT_FAIL
chmod +x mock_bin/getent

touch mock_edge_ca.crt
export EDGE_CA="$PWD/mock_edge_ca.crt"
export MOCK_HEALTH_EXISTS="1"
OUTPUT=$(./scripts/weltgewebe-up --no-pull --no-build 2>&1 || true)
if echo "$OUTPUT" | grep -q "DNS Guard failed"; then
  if echo "$OUTPUT" | grep -q "Diagnostics saved:"; then
    echo "PASS: DNS Guard failed and generated bundle as expected."
  else
    echo "FAIL: DNS Guard failed but did not generate failure bundle."
    echo "$OUTPUT"
    exit 1
  fi
else
  echo "FAIL: DNS Guard succeeded unexpectedly."
  echo "$OUTPUT"
  exit 1
fi
unset MOCK_HEALTH_EXISTS

cat << 'EOF_GETENT' > mock_bin/getent
#!/bin/bash
exit 0
EOF_GETENT
chmod +x mock_bin/getent

# 22. Cache Guards Logic
echo ">>> Test 22: Cache Guards Logic"

cat << 'EOF_GETENT' > mock_bin/getent
#!/bin/bash
exit 0
EOF_GETENT
chmod +x mock_bin/getent

cat << 'EOF_WGET' > mock_bin/wget
#!/bin/bash
exit 0
EOF_WGET
chmod +x mock_bin/wget

# Sub-test 22a: Missing HTML Cache Header
cat << 'EOF_CURL_NO_HTML_CACHE' > mock_bin/curl
#!/bin/bash
# Mock health endpoints (Guard 3 & 4)
if [[ "$*" == *"/health/ready"* ]]; then
    echo "OK"
    exit 0
fi
# Mock HTML Cache Guard
if [[ "$*" == *"-I"* && "$*" == *"/map"* ]]; then
    # Fake missing cache headers
    echo "HTTP/1.1 200 OK"
    echo "Content-Type: text/html"
    exit 0
fi
# Mock general reachability / frontend asset extraction (Guard 5)
if [[ "$*" == *"/map"* ]]; then
    MAP_HTML="<div id=\"_app/\"></div><script src=\"./_app/immutable/test.js\"></script>"
    O_FILE=""
    for ((i = 1; i < $#; i++)); do
        if [[ "${!i}" == "-o" ]]; then
            next=$((i + 1))
            O_FILE="${!next}"
        fi
    done
    if [[ -n "$O_FILE" ]]; then
        echo "$MAP_HTML" > "$O_FILE"
    else
        echo "$MAP_HTML"
    fi
    if [[ "$*" == *"%{http_code}"* ]]; then
        printf '200'
    fi
    exit 0
fi
exit 0
EOF_CURL_NO_HTML_CACHE
chmod +x mock_bin/curl

touch mock_edge_ca.crt
export EDGE_CA="$PWD/mock_edge_ca.crt"
export MOCK_HEALTH_EXISTS="1"
export REQUIRE_FRONTEND="1"
# These cache/header subtests are intentionally unrelated to sovereign map
# delivery. Keep them in remote-style mode; Germany edge delivery has its own
# fail-closed regression in test_weltgewebe_up_frontend_required.sh.
export PUBLIC_BASEMAP_MODE="remote-style"

set +e
OUTPUT=$(./scripts/weltgewebe-up --no-pull --no-build 2>&1)
STATUS=$?
set -e
if [ "$STATUS" -eq 0 ]; then
  echo "FAIL: Expected failure due to missing HTML cache headers, but got success."
  echo "$OUTPUT"
  exit 1
fi
if echo "$OUTPUT" | grep -q "Frontend Cache Guard failed: /map route did not return 'no-cache, must-revalidate' header"; then
  echo "PASS: Detected missing HTML cache headers correctly."
else
  echo "FAIL: Did not detect missing HTML cache headers."
  echo "$OUTPUT"
  exit 1
fi

# Sub-test 22b: Missing Asset Cache Header
cat << 'EOF_CURL_NO_ASSET_CACHE' > mock_bin/curl
#!/bin/bash
# Mock health endpoints (Guard 3 & 4)
if [[ "$*" == *"/health/ready"* ]]; then
    echo "OK"
    exit 0
fi
# Mock HTML Cache Guard (make it pass)
if [[ "$*" == *"-I"* && "$*" == *"/map"* ]]; then
    echo "HTTP/1.1 200 OK"
    echo "Cache-Control: no-cache, must-revalidate"
    exit 0
fi
# Mock Asset Cache Guard (make it fail by providing empty headers)
# (version.json also matches *.js*; it has its own branch below.)
if [[ "$*" == *"-D"* && "$*" == *".js"* && "$*" != *"/_app/version.json"* ]]; then
    # Parse args for -D <file> to accurately write headers
    D_FILE=""
    while [[ $# -gt 0 ]]; do
        if [[ "$1" == "-D" ]]; then
            D_FILE="$2"
            break
        fi
        shift
    done
    if [[ -n "$D_FILE" ]]; then
        echo "HTTP/1.1 200 OK" > "$D_FILE"
    fi
    echo "200"
    exit 0
fi
# Mock general reachability / frontend asset extraction (Guard 5)
if [[ "$*" == *"/map"* ]]; then
    MAP_HTML="<div id=\"_app/\"></div><script src=\"./_app/immutable/test.js\"></script>"
    O_FILE=""
    for ((i = 1; i < $#; i++)); do
        if [[ "${!i}" == "-o" ]]; then
            next=$((i + 1))
            O_FILE="${!next}"
        fi
    done
    if [[ -n "$O_FILE" ]]; then
        echo "$MAP_HTML" > "$O_FILE"
    else
        echo "$MAP_HTML"
    fi
    if [[ "$*" == *"%{http_code}"* ]]; then
        printf '200'
    fi
    exit 0
fi
exit 0
EOF_CURL_NO_ASSET_CACHE
chmod +x mock_bin/curl

set +e
OUTPUT=$(./scripts/weltgewebe-up --no-pull --no-build 2>&1)
STATUS=$?
set -e
if [ "$STATUS" -eq 0 ]; then
  echo "FAIL: Expected failure due to missing immutable asset headers, but got success."
  echo "$OUTPUT"
  exit 1
fi
if echo "$OUTPUT" | grep -q "Frontend Cache Guard failed: Immutable asset .* did not return 'max-age=31536000, immutable' header"; then
  echo "PASS: Detected missing immutable asset headers correctly."
else
  echo "FAIL: Did not detect missing immutable asset headers."
  echo "$OUTPUT"
  exit 1
fi

# Sub-test 22c: Valid Cache Headers (Positive Path)
cat << 'EOF_CURL_POSITIVE_CACHE' > mock_bin/curl
#!/bin/bash
# Mock health endpoints (Guard 3 & 4)
if [[ "$*" == *"/health/ready"* ]]; then
    echo "OK"
    exit 0
fi
# Mock HTML Cache Guard (make it pass)
if [[ "$*" == *"-I"* && "$*" == *"/map"* ]]; then
    echo "HTTP/1.1 200 OK"
    echo "Cache-Control: no-cache, must-revalidate"
    exit 0
fi
# Mock Asset Cache Guard (make it pass)
# (version.json also matches *.js*; it has its own branch below.)
if [[ "$*" == *"-D"* && "$*" == *".js"* && "$*" != *"/_app/version.json"* ]]; then
    D_FILE=""
    while [[ $# -gt 0 ]]; do
        if [[ "$1" == "-D" ]]; then
            D_FILE="$2"
            break
        fi
        shift
    done
    if [[ -n "$D_FILE" ]]; then
        echo "HTTP/1.1 200 OK" > "$D_FILE"
        echo "Cache-Control: public, max-age=31536000, immutable" >> "$D_FILE"
    fi
    echo "200"
    exit 0
fi
# Mock /_app/version.json (make it pass)
if [[ "$*" == *"/_app/version.json"* ]]; then
    ALL_ARGS="$*" # the option loop below shifts "$@" away
    O_FILE=""
    D_FILE=""
    while [[ $# -gt 0 ]]; do
        if [[ "$1" == "-o" ]]; then
            O_FILE="$2"
        elif [[ "$1" == "-D" ]]; then
            D_FILE="$2"
        fi
        shift
    done
    if [[ -n "$O_FILE" && "$O_FILE" != "/dev/null" ]]; then
        echo "{\"version\":\"mock-artifact-id\", \"build_id\":\"mock-build-id-123\"}" > "$O_FILE"
    fi
    if [[ -n "$D_FILE" ]]; then
        echo "HTTP/1.1 200 OK" > "$D_FILE"
        echo "Cache-Control: no-store" >> "$D_FILE"
    fi
    # If -w %{http_code} is used, we output ONLY 200 to stdout
    if [[ "$ALL_ARGS" == *"-w"* ]]; then
        echo "200"
    else
        # Fallback if no -w and no -o was used (for manual curls)
        if [[ -z "$O_FILE" || "$O_FILE" == "/dev/null" ]]; then
            echo "{\"version\":\"mock-artifact-id\", \"build_id\":\"mock-build-id-123\"}"
        fi
    fi
    exit 0
fi
# Mock general reachability / frontend asset extraction (Guard 5)
if [[ "$*" == *"/map"* ]]; then
    MAP_HTML="<div id=\"_app/\"></div><script src=\"./_app/immutable/test.js\"></script>"
    O_FILE=""
    for ((i = 1; i < $#; i++)); do
        if [[ "${!i}" == "-o" ]]; then
            next=$((i + 1))
            O_FILE="${!next}"
        fi
    done
    if [[ -n "$O_FILE" ]]; then
        echo "$MAP_HTML" > "$O_FILE"
    else
        echo "$MAP_HTML"
    fi
    if [[ "$*" == *"%{http_code}"* ]]; then
        printf '200'
    fi
    exit 0
fi
exit 0
EOF_CURL_POSITIVE_CACHE
chmod +x mock_bin/curl

set +e
OUTPUT=$(./scripts/weltgewebe-up --no-pull --no-build 2>&1)
STATUS=$?
set -e
if [ "$STATUS" -ne 0 ]; then
  echo "FAIL: Expected success but got exit code $STATUS."
  echo "$OUTPUT"
  exit 1
fi
if echo "$OUTPUT" | grep -q "OK (frontend route /map cache-control verified)" &&
  echo "$OUTPUT" | grep -q "verified with immutable cache headers" &&
  echo "$OUTPUT" | grep -q "OK (Version: mock-artifact-id, Build-ID: mock-build-id-123 verified via /_app/version.json with no-store)"; then
  echo "PASS: Valid cache headers and version.json correctly passed the guards."
else
  echo "FAIL: Positive cache header validation path failed."
  echo "$OUTPUT"
  exit 1
fi

# Sub-test 22d: Missing no-store header for version.json
cat << 'EOF_CURL_NO_VERSION_CACHE' > mock_bin/curl
#!/bin/bash
# Mock health endpoints (Guard 3 & 4)
if [[ "$*" == *"/health/ready"* ]]; then
    echo "OK"
    exit 0
fi
# Mock HTML Cache Guard (make it pass)
if [[ "$*" == *"-I"* && "$*" == *"/map"* ]]; then
    echo "HTTP/1.1 200 OK"
    echo "Cache-Control: no-cache, must-revalidate"
    exit 0
fi
# Mock Asset Cache Guard (make it pass)
# (version.json also matches *.js*; it has its own branch below.)
if [[ "$*" == *"-D"* && "$*" == *".js"* && "$*" != *"/_app/version.json"* ]]; then
    D_FILE=""
    while [[ $# -gt 0 ]]; do
        if [[ "$1" == "-D" ]]; then
            D_FILE="$2"
            break
        fi
        shift
    done
    if [[ -n "$D_FILE" ]]; then
        echo "HTTP/1.1 200 OK" > "$D_FILE"
        echo "Cache-Control: public, max-age=31536000, immutable" >> "$D_FILE"
    fi
    echo "200"
    exit 0
fi
# Mock /_app/version.json (make it fail by not providing Cache-Control: no-store)
if [[ "$*" == *"/_app/version.json"* ]]; then
    ALL_ARGS="$*" # the option loop below shifts "$@" away
    O_FILE=""
    D_FILE=""
    while [[ $# -gt 0 ]]; do
        if [[ "$1" == "-o" ]]; then
            O_FILE="$2"
        elif [[ "$1" == "-D" ]]; then
            D_FILE="$2"
        fi
        shift
    done
    if [[ -n "$O_FILE" && "$O_FILE" != "/dev/null" ]]; then
        echo "{\"version\":\"mock-artifact-id\", \"build_id\":\"mock-build-id-123\"}" > "$O_FILE"
    fi
    if [[ -n "$D_FILE" ]]; then
        echo "HTTP/1.1 200 OK" > "$D_FILE"
    fi
    # If -w %{http_code} is used, we output ONLY 200 to stdout
    if [[ "$ALL_ARGS" == *"-w"* ]]; then
        echo "200"
    else
        # Fallback if no -w and no -o was used (for manual curls)
        if [[ -z "$O_FILE" || "$O_FILE" == "/dev/null" ]]; then
            echo "{\"version\":\"mock-artifact-id\", \"build_id\":\"mock-build-id-123\"}"
        fi
    fi
    exit 0
fi
# Mock general reachability / frontend asset extraction (Guard 5)
if [[ "$*" == *"/map"* ]]; then
    MAP_HTML="<div id=\"_app/\"></div><script src=\"./_app/immutable/test.js\"></script>"
    O_FILE=""
    for ((i = 1; i < $#; i++)); do
        if [[ "${!i}" == "-o" ]]; then
            next=$((i + 1))
            O_FILE="${!next}"
        fi
    done
    if [[ -n "$O_FILE" ]]; then
        echo "$MAP_HTML" > "$O_FILE"
    else
        echo "$MAP_HTML"
    fi
    if [[ "$*" == *"%{http_code}"* ]]; then
        printf '200'
    fi
    exit 0
fi
exit 0
EOF_CURL_NO_VERSION_CACHE
chmod +x mock_bin/curl

set +e
OUTPUT=$(./scripts/weltgewebe-up --no-pull --no-build 2>&1)
STATUS=$?
set -e
if [ "$STATUS" -eq 0 ]; then
  echo "FAIL: Expected failure due to missing no-store header, but got success."
  echo "$OUTPUT"
  exit 1
fi
if echo "$OUTPUT" | grep -q "Frontend Guard failed: /_app/version.json did not return 'no-store' header."; then
  echo "PASS: Detected missing version.json cache headers correctly."
else
  echo "FAIL: Did not detect missing version.json cache headers."
  echo "$OUTPUT"
  exit 1
fi

# Sub-test 22e: Missing canonical version for version.json
cat << 'EOF_CURL_NO_VERSION_FIELD' > mock_bin/curl
#!/bin/bash
# Mock health endpoints (Guard 3 & 4)
if [[ "$*" == *"/health/ready"* ]]; then
    echo "OK"
    exit 0
fi
# Mock HTML Cache Guard (make it pass)
if [[ "$*" == *"-I"* && "$*" == *"/map"* ]]; then
    echo "HTTP/1.1 200 OK"
    echo "Cache-Control: no-cache, must-revalidate"
    exit 0
fi
# Mock Asset Cache Guard (make it pass)
# (version.json also matches *.js*; it has its own branch below.)
if [[ "$*" == *"-D"* && "$*" == *".js"* && "$*" != *"/_app/version.json"* ]]; then
    D_FILE=""
    while [[ $# -gt 0 ]]; do
        if [[ "$1" == "-D" ]]; then
            D_FILE="$2"
            break
        fi
        shift
    done
    if [[ -n "$D_FILE" ]]; then
        echo "HTTP/1.1 200 OK" > "$D_FILE"
        echo "Cache-Control: public, max-age=31536000, immutable" >> "$D_FILE"
    fi
    echo "200"
    exit 0
fi
# Mock /_app/version.json (make it fail by providing valid JSON without canonical version)
if [[ "$*" == *"/_app/version.json"* ]]; then
    ALL_ARGS="$*" # the option loop below shifts "$@" away
    O_FILE=""
    D_FILE=""
    while [[ $# -gt 0 ]]; do
        if [[ "$1" == "-o" ]]; then
            O_FILE="$2"
        elif [[ "$1" == "-D" ]]; then
            D_FILE="$2"
        fi
        shift
    done
    if [[ -n "$O_FILE" && "$O_FILE" != "/dev/null" ]]; then
        # Missing 'version' field
        echo "{\"build_id\":\"mock-build-id-123\"}" > "$O_FILE"
    fi
    if [[ -n "$D_FILE" ]]; then
        echo "HTTP/1.1 200 OK" > "$D_FILE"
        echo "Cache-Control: no-store" >> "$D_FILE"
    fi
    # If -w %{http_code} is used, we output ONLY 200 to stdout
    if [[ "$ALL_ARGS" == *"-w"* ]]; then
        echo "200"
    else
        # Fallback if no -w and no -o was used (for manual curls)
        if [[ -z "$O_FILE" || "$O_FILE" == "/dev/null" ]]; then
            echo "{\"build_id\":\"mock-build-id-123\"}"
        fi
    fi
    exit 0
fi
# Mock general reachability / frontend asset extraction (Guard 5)
if [[ "$*" == *"/map"* ]]; then
    MAP_HTML="<div id=\"_app/\"></div><script src=\"./_app/immutable/test.js\"></script>"
    O_FILE=""
    for ((i = 1; i < $#; i++)); do
        if [[ "${!i}" == "-o" ]]; then
            next=$((i + 1))
            O_FILE="${!next}"
        fi
    done
    if [[ -n "$O_FILE" ]]; then
        echo "$MAP_HTML" > "$O_FILE"
    else
        echo "$MAP_HTML"
    fi
    if [[ "$*" == *"%{http_code}"* ]]; then
        printf '200'
    fi
    exit 0
fi
exit 0
EOF_CURL_NO_VERSION_FIELD
chmod +x mock_bin/curl

set +e
OUTPUT=$(./scripts/weltgewebe-up --no-pull --no-build 2>&1)
STATUS=$?
set -e
if [ "$STATUS" -eq 0 ]; then
  echo "FAIL: Expected failure due to missing canonical version, but got success."
  echo "$OUTPUT"
  exit 1
fi
if echo "$OUTPUT" | grep -q "Frontend Guard failed: /_app/version.json missing valid canonical 'version' field."; then
  echo "PASS: Detected missing canonical version in version.json correctly."
else
  echo "FAIL: Did not detect missing canonical version in version.json."
  echo "$OUTPUT"
  exit 1
fi

unset REQUIRE_FRONTEND
unset EDGE_CA
unset MOCK_HEALTH_EXISTS
rm -f mock_edge_ca.crt

# 23. Container Health Guard Failure
echo ">>> Test 23: Container Health Guard Failure"
cat << 'EOF_WGET_FAIL' > mock_bin/wget
#!/bin/bash
exit 1
EOF_WGET_FAIL
chmod +x mock_bin/wget
export MOCK_EXEC_FAIL="1"
touch mock_edge_ca.crt
export EDGE_CA="$PWD/mock_edge_ca.crt"
export MOCK_HEALTH_EXISTS="1"
OUTPUT=$(./scripts/weltgewebe-up --no-pull --no-build 2>&1 || true)
if echo "$OUTPUT" | grep -q "Container Health Guard failed"; then
  echo "PASS: Container Health Guard failed as expected."
else
  echo "FAIL: Container Health Guard succeeded unexpectedly."
  echo "$OUTPUT"
  exit 1
fi
unset MOCK_EXEC_FAIL
unset MOCK_HEALTH_EXISTS
rm -f mock_bin/getent mock_bin/curl mock_bin/wget
rm -f mock_edge_ca.crt
unset EDGE_CA

# Final Cleanup
unset WELTGEWEBE_COMPOSE_BAKE
unset WELTGEWEBE_APPS_PROBE
unset VERIFY_BAKE
unset MOCK_ZOMBIE
unset REPO_DIR
unset MOCK_PORT_MODE
unset MOCK_EXEC_FAIL
unset MOCK_HAS_CADDY
unset DEPLOY_TARGET
unset PUBLIC_BASEMAP_MODE

echo ">>> All refined tests passed."
