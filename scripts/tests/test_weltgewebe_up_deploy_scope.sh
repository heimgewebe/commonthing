#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" > /dev/null 2>&1 && pwd)"
REPO_ROOT="$(dirname "$(dirname "$SCRIPT_DIR")")"
SCRIPT_SOURCE="$REPO_ROOT/scripts/weltgewebe-up"
PREFLIGHT_SOURCE="$REPO_ROOT/scripts/preflight/schauwerk_editor_release.py"
WORK_ROOT="$(mktemp -d)"
trap 'rm -rf "$WORK_ROOT"' EXIT

# Synthetic VPS tests must not depend on the host network interfaces.
export CADDY_BIND=203.0.113.10
export CADDY_IPV6_BIND='[2001:db8::10]'

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

assert_contains() {
  local haystack="$1" needle="$2"
  grep -Fq -- "$needle" <<< "$haystack" || fail "expected output to contain: $needle"
}

assert_not_contains() {
  local haystack="$1" needle="$2"
  if grep -Fq -- "$needle" <<< "$haystack"; then
    fail "expected output not to contain: $needle"
  fi
}

new_repo() {
  local name="$1"
  local repo="$WORK_ROOT/$name/repo"
  mkdir -p "$repo"
  (
    cd "$repo"
    git init -q
    git config user.name "Weltgewebe Test"
    git config user.email "tests@weltgewebe.local"
    mkdir -p scripts/preflight infra/compose infra/schauwerk-editor
    cp "$SCRIPT_SOURCE" scripts/weltgewebe-up
    cp "$PREFLIGHT_SOURCE" scripts/preflight/schauwerk_editor_release.py
    chmod +x scripts/weltgewebe-up scripts/preflight/schauwerk_editor_release.py
    cat > .env << 'ENV'
DATABASE_URL=postgres://example
POSTGRES_USER=weltgewebe
POSTGRES_PASSWORD=test
POSTGRES_DB=weltgewebe
ENV
    cat > infra/schauwerk-editor/release-lock.json << 'JSON'
{
  "schema_version": "weltgewebe-schauwerk-runtime-lock.v1",
  "source_repository": "heimgewebe/schauwerk",
  "source_commit": "cccccccccccccccccccccccccccccccccccccccc",
  "image_repository": "ghcr.io/heimgewebe/schauwerk-schaubild",
  "image_digest": "sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
  "public_base_path": "/schaubild"
}
JSON
    cat > infra/compose/compose.prod.yml << 'YAML'
services:
  api:
    image: weltgewebe-api:${API_VERSION}
  db:
    image: postgres:16
  nats:
    image: nats:2
  caddy:
    image: caddy:2
YAML
    cp infra/compose/compose.prod.yml infra/compose/compose.vps.override.yml
    git add .
    git commit -q -m "test: initial"
  )
  echo "$repo"
}

setup_mocks() {
  local state="$1"
  local mock_bin="$state/bin"
  mkdir -p "$mock_bin"
  printf 'api-1\n' > "$state/api.id"
  printf 'db-1\n' > "$state/db.id"
  printf 'nats-1\n' > "$state/nats.id"
  printf 'caddy-1\n' > "$state/caddy.id"
  : > "$state/compose-up.log"
  : > "$state/mutation.log"
  printf 'verify-applied\n' > "$state/api.mode"

  cat > "$mock_bin/docker" << 'MOCK'
#!/usr/bin/env bash
set -euo pipefail
args=("$@")
joined=" $* "
state="${MOCK_STATE:?}"

if [[ "${1:-}" == "ps" ]]; then
  exit 0
fi

if [[ "${1:-}" == "inspect" ]]; then
  format=""
  if [[ "${2:-}" == "--format" ]]; then
    format="${3:-}"
  fi
  case "$format" in
    *'.State.Health}}{{.State.Health.Status'* ) echo healthy ;;
    *'.State.Health}}yes'* ) echo yes ;;
    *'.State.Health.Status'* ) echo healthy ;;
    *'.NetworkSettings.Networks'* ) echo '[weltgewebe-api api]' ;;
    *'.Config.Env'* ) echo "WELTGEWEBE_API_STARTUP_MIGRATIONS=$(cat "$state/api.mode")" ;;
    * ) echo '{}' ;;
  esac
  exit 0
fi

if [[ "${1:-}" != "compose" ]]; then
  exit 0
fi

if [[ "$joined" == *" config --services "* ]]; then
  printf 'api\ndb\nnats\nschaubild\ncaddy\n'
  exit 0
fi
if [[ "$joined" == *" config --format json "* ]]; then
  printf '%s\n' '{"services":{"api":{},"db":{},"nats":{},"schaubild":{"image":"ghcr.io/heimgewebe/schauwerk-schaubild@sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd","read_only":true,"ports":[],"expose":["8765"],"command":["python","-m","schauwerk.visual.standalone_editor","serve","--bind-host","0.0.0.0","--trusted-reverse-proxy","--trusted-proxy-source-cidr","172.16.0.0/12","--public-base-path","/schaubild","--port","8765"]},"caddy":{"depends_on":{"schaubild":{"condition":"service_healthy"}}}}}'
  exit 0
fi
if [[ "$joined" == *" config "* ]]; then
  printf '%s\n' 'services: {}'
  exit 0
fi

if [[ "$joined" == *" ps -q "* ]]; then
  service="${args[${#args[@]}-1]}"
  if [[ "${MOCK_MISSING_SERVICE:-}" == "$service" ]]; then
    exit 0
  fi
  cat "$state/$service.id"
  exit 0
fi

if [[ "$joined" == *" up -d "* ]]; then
  printf '%s\n' "$*" >>"$state/compose-up.log"
  printf '%s\n' "${WELTGEWEBE_API_STARTUP_MIGRATIONS:-unset}" >"$state/api.mode"
  printf 'up\n' >>"$state/mutation.log"
  if [[ "${MOCK_FAIL_INITIAL_MIGRATION:-0}" == "1" && "${WELTGEWEBE_API_STARTUP_MIGRATIONS:-}" == "run" && ! -f "$state/initial-migration-failed" ]]; then
    : >"$state/initial-migration-failed"
    echo "injected initial migration failure" >&2
    exit 94
  fi
  if [[ "$joined" != *" --no-deps "* ]]; then
    echo "bounded command missing --no-deps" >&2
    exit 91
  fi
  if [[ "$joined" == *" --remove-orphans "* ]]; then
    echo "bounded command contains --remove-orphans" >&2
    exit 92
  fi
  last="${args[${#args[@]}-1]}"
  [[ "$last" == api ]] || { echo "bounded command target is $last" >&2; exit 93; }
  if [[ "${MOCK_DRIFT_SERVICE:-}" != "" && ! -f "$state/drift.done" ]]; then
    printf '%s-2\n' "${MOCK_DRIFT_SERVICE}" >"$state/${MOCK_DRIFT_SERVICE}.id"
    : >"$state/drift.done"
  fi
  exit 0
fi

if [[ "$joined" == *" ps --format "* ]]; then
  printf 'weltgewebe-api-1 running healthy\nweltgewebe-db-1 running healthy\nweltgewebe-nats-1 running healthy\nweltgewebe-caddy-1 running healthy\n'
  exit 0
fi
if [[ "$joined" == *" ps "* ]]; then
  printf 'NAME STATUS\napi healthy\ndb healthy\nnats healthy\ncaddy running\n'
  exit 0
fi
if [[ "$joined" == *" exec -T api "* ]]; then
  exit 0
fi
if [[ "$joined" == *" port api "* ]]; then
  exit 0
fi
if [[ "$joined" == *" logs "* ]]; then
  exit 0
fi
exit 0
MOCK

  cat > "$mock_bin/curl" << 'MOCK'
#!/usr/bin/env bash
exit 0
MOCK
  cat > "$mock_bin/getent" << 'MOCK'
#!/usr/bin/env bash
printf '127.0.0.1 localhost\n'
MOCK
  chmod +x "$mock_bin/docker" "$mock_bin/curl" "$mock_bin/getent"
  echo "$mock_bin"
}

run_up() {
  local repo="$1" state="$2"
  shift 2
  local mock_bin
  mock_bin="$(setup_mocks "$state")"
  (
    cd "$repo"
    PATH="$mock_bin:/usr/bin:/bin" \
      MOCK_STATE="$state" \
      REPO_DIR="$repo" \
      ENV_FILE="$repo/.env" \
      DEPLOY_TARGET=vps \
      WELTGEWEBE_STATE_DIR="$repo/.ops" \
      WELTGEWEBE_DEPLOY_LOCK_FILE="$state/deploy.lock" \
      WELTGEWEBE_SCOPED_API_HEALTH_TIMEOUT_SECONDS=2 \
      bash scripts/weltgewebe-up --no-pull --no-build "$@"
  )
}

# Plan-only must produce an exact machine-readable plan without any compose mutation.
repo_plan="$(new_repo plan-only)"
state_plan="$WORK_ROOT/plan-state"
mkdir -p "$state_plan"
out_plan="$(run_up "$repo_plan" "$state_plan" --deploy-scope api --plan-only 2>&1)"
plan_path="$repo_plan/.ops/deploy-plan-api.json"
[[ -f "$plan_path" ]] || fail "plan-only did not create $plan_path"
[[ ! -s "$state_plan/mutation.log" ]] || fail "plan-only mutated compose"
# Same repo-canonical tools/py environment as make validate / UV_RUN.
if ! command -v uv > /dev/null 2>&1; then
  fail "uv is required for deploy-scope plan assertions (tools/py/uv.lock)"
fi
uv run --project "$REPO_ROOT/tools/py" --locked python - "$plan_path" << 'PY'
import json, sys
p=json.load(open(sys.argv[1], encoding='utf-8'))
assert p['scope']=='api'
assert len(p['git_head'])==40
assert all(c in '0123456789abcdef' for c in p['git_head'])
assert p['api_image_tag']
assert p['target_services']==['api']
assert p['protected_services']==['db','nats','caddy']
assert p['startup_migration_mode']=='verify-applied'
assert p['no_deps'] is True
assert p['remove_orphans'] is False
assert p['commands'][0][-1]=='api'
assert '--no-deps' in p['commands'][0]
assert '--remove-orphans' not in p['commands'][0]
PY
assert_contains "$out_plan" "Plan-only mode: no container mutation performed."
echo "PASS: plan-only writes an exact bounded API plan without mutation"

# API scope must target only API and preserve all protected service identities.
repo_api="$(new_repo api)"
state_api="$WORK_ROOT/api-state"
mkdir -p "$state_api"
out_api="$(run_up "$repo_api" "$state_api" --deploy-scope api 2>&1)"
[[ "$(wc -l < "$state_api/compose-up.log")" -eq 1 ]] || fail "API scope should execute one compose up"
api_call="$(cat "$state_api/compose-up.log")"
assert_contains "$api_call" "up -d --no-deps api"
assert_not_contains "$api_call" "--remove-orphans"
assert_not_contains "$api_call" " db"
assert_not_contains "$api_call" " nats"
assert_not_contains "$api_call" " caddy"
[[ "$(cat "$state_api/api.mode")" == "verify-applied" ]] || fail "API scope did not force verify-applied"
assert_contains "$out_api" "Health OK"
echo "PASS: API scope changes only API and forces verify-applied"

# Migration scope must run API-only, then automatically recreate only API in verify-applied mode.
repo_migration="$(new_repo migration)"
state_migration="$WORK_ROOT/migration-state"
mkdir -p "$state_migration"
out_migration="$(run_up "$repo_migration" "$state_migration" --deploy-scope migration 2>&1)"
[[ "$(wc -l < "$state_migration/compose-up.log")" -eq 2 ]] || fail "migration scope should execute exactly two compose up calls"
first_call="$(sed -n '1p' "$state_migration/compose-up.log")"
second_call="$(sed -n '2p' "$state_migration/compose-up.log")"
assert_contains "$first_call" "up -d --no-deps api"
assert_contains "$second_call" "up -d --no-deps --force-recreate api"
[[ "$(cat "$state_migration/api.mode")" == "verify-applied" ]] || fail "migration scope did not restore verify-applied"
uv run --project "$REPO_ROOT/tools/py" --locked python - "$repo_migration/.ops/deploy-plan-migration.json" << 'PY'
import json, sys
p=json.load(open(sys.argv[1], encoding='utf-8'))
assert p['scope']=='migration'
assert p['startup_migration_mode']=='run'
assert p['automatic_restore_mode']=='verify-applied'
assert len(p['commands'])==2
assert p['command_environments']==[
    {'WELTGEWEBE_API_STARTUP_MIGRATIONS':'run'},
    {'WELTGEWEBE_API_STARTUP_MIGRATIONS':'verify-applied'},
]
PY
assert_contains "$out_migration" "Running automatic migration-mode restore"
echo "PASS: migration scope is API-only and automatically restores verify-applied"

# Migration recovery must remain possible when Caddy is already absent. Existing
# DB and NATS containers are still mandatory and protected.
repo_migration_no_caddy="$(new_repo migration-no-caddy)"
state_migration_no_caddy="$WORK_ROOT/migration-no-caddy-state"
mkdir -p "$state_migration_no_caddy"
mock_bin_migration_no_caddy="$(setup_mocks "$state_migration_no_caddy")"
out_migration_no_caddy="$( (cd "$repo_migration_no_caddy" && PATH="$mock_bin_migration_no_caddy:/usr/bin:/bin" MOCK_STATE="$state_migration_no_caddy" WELTGEWEBE_DEPLOY_LOCK_FILE="$state_migration_no_caddy/deploy.lock" MOCK_MISSING_SERVICE=caddy REPO_DIR="$repo_migration_no_caddy" ENV_FILE="$repo_migration_no_caddy/.env" DEPLOY_TARGET=vps WELTGEWEBE_STATE_DIR="$repo_migration_no_caddy/.ops" WELTGEWEBE_SCOPED_API_HEALTH_TIMEOUT_SECONDS=2 bash scripts/weltgewebe-up --no-pull --no-build --deploy-scope migration) 2>&1)"
[[ "$(wc -l < "$state_migration_no_caddy/compose-up.log")" -eq 2 ]] || fail "migration recovery without caddy should execute two API-only compose calls"
assert_contains "$out_migration_no_caddy" "Running automatic migration-mode restore"
assert_not_contains "$out_migration_no_caddy" "requires the protected caddy service"
echo "PASS: migration scope can recover an API when Caddy is already absent"

# The ordinary API scope must keep its stricter VPS dependency on Caddy.
repo_api_no_caddy="$(new_repo api-no-caddy)"
state_api_no_caddy="$WORK_ROOT/api-no-caddy-state"
mkdir -p "$state_api_no_caddy"
mock_bin_api_no_caddy="$(setup_mocks "$state_api_no_caddy")"
set +e
out_api_no_caddy="$( (cd "$repo_api_no_caddy" && PATH="$mock_bin_api_no_caddy:/usr/bin:/bin" MOCK_STATE="$state_api_no_caddy" WELTGEWEBE_DEPLOY_LOCK_FILE="$state_api_no_caddy/deploy.lock" MOCK_MISSING_SERVICE=caddy REPO_DIR="$repo_api_no_caddy" ENV_FILE="$repo_api_no_caddy/.env" DEPLOY_TARGET=vps WELTGEWEBE_STATE_DIR="$repo_api_no_caddy/.ops" bash scripts/weltgewebe-up --no-pull --no-build --deploy-scope api) 2>&1)"
rc_api_no_caddy=$?
set -e
[[ "$rc_api_no_caddy" -ne 0 ]] || fail "API scope without Caddy should fail"
[[ ! -s "$state_api_no_caddy/mutation.log" ]] || fail "API scope without Caddy still mutated compose"
assert_contains "$out_api_no_caddy" "requires the protected caddy service"
echo "PASS: API scope still rejects a missing VPS Caddy before mutation"

# A failed migration-active API start must still attempt an API-only verify-applied restore.
repo_migration_failure="$(new_repo migration-failure)"
state_migration_failure="$WORK_ROOT/migration-failure-state"
mkdir -p "$state_migration_failure"
mock_bin_migration_failure="$(setup_mocks "$state_migration_failure")"
set +e
out_migration_failure="$( (cd "$repo_migration_failure" && PATH="$mock_bin_migration_failure:/usr/bin:/bin" MOCK_STATE="$state_migration_failure" WELTGEWEBE_DEPLOY_LOCK_FILE="$state_migration_failure/deploy.lock" MOCK_FAIL_INITIAL_MIGRATION=1 REPO_DIR="$repo_migration_failure" ENV_FILE="$repo_migration_failure/.env" DEPLOY_TARGET=vps WELTGEWEBE_STATE_DIR="$repo_migration_failure/.ops" WELTGEWEBE_SCOPED_API_HEALTH_TIMEOUT_SECONDS=2 bash scripts/weltgewebe-up --no-pull --no-build --deploy-scope migration) 2>&1)"
rc_migration_failure=$?
set -e
[[ "$rc_migration_failure" -ne 0 ]] || fail "injected migration failure should fail the overall deploy"
[[ "$(wc -l < "$state_migration_failure/compose-up.log")" -eq 2 ]] || fail "failed migration should trigger exactly one restore call"
failed_initial_call="$(sed -n '1p' "$state_migration_failure/compose-up.log")"
failed_restore_call="$(sed -n '2p' "$state_migration_failure/compose-up.log")"
assert_contains "$failed_initial_call" "up -d --no-deps api"
assert_contains "$failed_restore_call" "up -d --no-deps --force-recreate api"
[[ "$(cat "$state_migration_failure/api.mode")" == "verify-applied" ]] || fail "failed migration did not restore verify-applied"
assert_contains "$out_migration_failure" "Running automatic migration-mode restore"
assert_contains "$out_migration_failure" "Scoped API container startup failed"
echo "PASS: failed migration-active start triggers API-only verify-applied restoration"

# Any protected identity drift must fail closed and be diagnosed.
repo_drift="$(new_repo drift)"
state_drift="$WORK_ROOT/drift-state"
mkdir -p "$state_drift"
mock_bin_drift="$(setup_mocks "$state_drift")"
set +e
out_drift="$( (cd "$repo_drift" && PATH="$mock_bin_drift:/usr/bin:/bin" MOCK_STATE="$state_drift" WELTGEWEBE_DEPLOY_LOCK_FILE="$state_drift/deploy.lock" MOCK_DRIFT_SERVICE=db REPO_DIR="$repo_drift" ENV_FILE="$repo_drift/.env" DEPLOY_TARGET=vps WELTGEWEBE_STATE_DIR="$repo_drift/.ops" WELTGEWEBE_SCOPED_API_HEALTH_TIMEOUT_SECONDS=2 bash scripts/weltgewebe-up --no-pull --no-build --deploy-scope api) 2>&1)"
rc_drift=$?
set -e
[[ "$rc_drift" -ne 0 ]] || fail "protected db drift should fail"
assert_contains "$out_drift" "protected service 'db' changed identity"
echo "PASS: protected service identity drift fails closed"

# Missing dependencies must stop before compose up.
repo_missing="$(new_repo missing)"
state_missing="$WORK_ROOT/missing-state"
mkdir -p "$state_missing"
mock_bin_missing="$(setup_mocks "$state_missing")"
set +e
out_missing="$( (cd "$repo_missing" && PATH="$mock_bin_missing:/usr/bin:/bin" MOCK_STATE="$state_missing" WELTGEWEBE_DEPLOY_LOCK_FILE="$state_missing/deploy.lock" MOCK_MISSING_SERVICE=db REPO_DIR="$repo_missing" ENV_FILE="$repo_missing/.env" DEPLOY_TARGET=vps WELTGEWEBE_STATE_DIR="$repo_missing/.ops" bash scripts/weltgewebe-up --no-pull --no-build --deploy-scope api) 2>&1)"
rc_missing=$?
set -e
[[ "$rc_missing" -ne 0 ]] || fail "missing db should fail"
[[ ! -s "$state_missing/mutation.log" ]] || fail "missing protected db still mutated compose"
assert_contains "$out_missing" "requires running protected service 'db'"
echo "PASS: missing protected dependencies stop before mutation"

# Plan destinations are restricted to one non-symlink file inside the state directory.
repo_plan_path="$(new_repo plan-path)"
state_plan_path="$WORK_ROOT/plan-path-state"
mkdir -p "$state_plan_path"
mock_bin_plan_path="$(setup_mocks "$state_plan_path")"
set +e
out_plan_path="$( (cd "$repo_plan_path" && PATH="$mock_bin_plan_path:/usr/bin:/bin" MOCK_STATE="$state_plan_path" WELTGEWEBE_DEPLOY_LOCK_FILE="$state_plan_path/deploy.lock" REPO_DIR="$repo_plan_path" ENV_FILE="$repo_plan_path/.env" DEPLOY_TARGET=vps WELTGEWEBE_STATE_DIR="$repo_plan_path/.ops" bash scripts/weltgewebe-up --no-pull --no-build --deploy-scope api --plan-only --deploy-plan-file ../escape.json) 2>&1)"
rc_plan_path=$?
set -e
[[ "$rc_plan_path" -ne 0 ]] || fail "plan path traversal should fail"
assert_contains "$out_plan_path" "deploy plan name must be one plain file name"
[[ ! -e "$repo_plan_path/escape.json" ]] || fail "plan path escaped the state directory"
echo "PASS: deploy plan path is confined to the state directory"

# A second deploy with the same host/project lock must fail before compose mutation.
repo_lock="$(new_repo lock)"
state_lock="$WORK_ROOT/lock-state"
mkdir -p "$state_lock"
mock_bin_lock="$(setup_mocks "$state_lock")"
exec {held_lock_fd}> "$state_lock/deploy.lock"
flock -n "$held_lock_fd" || fail "could not acquire test deployment lock"
set +e
out_lock="$( (cd "$repo_lock" && PATH="$mock_bin_lock:/usr/bin:/bin" MOCK_STATE="$state_lock" WELTGEWEBE_DEPLOY_LOCK_FILE="$state_lock/deploy.lock" REPO_DIR="$repo_lock" ENV_FILE="$repo_lock/.env" DEPLOY_TARGET=vps WELTGEWEBE_STATE_DIR="$repo_lock/.ops" bash scripts/weltgewebe-up --no-pull --no-build --deploy-scope api) 2>&1)"
rc_lock=$?
set -e
flock -u "$held_lock_fd"
exec {held_lock_fd}>&-
[[ "$rc_lock" -ne 0 ]] || fail "parallel deploy should fail"
[[ ! -s "$state_lock/mutation.log" ]] || fail "parallel deploy mutated compose"
assert_contains "$out_lock" "another Weltgewebe deployment is already active"
echo "PASS: host/project deployment lock rejects parallel effects"

# Incompatible frontend/caddy effects are rejected before runtime inspection.
repo_incompatible="$(new_repo incompatible)"
state_incompatible="$WORK_ROOT/incompatible-state"
mkdir -p "$state_incompatible"
mock_bin_incompatible="$(setup_mocks "$state_incompatible")"
set +e
out_incompatible="$( (cd "$repo_incompatible" && PATH="$mock_bin_incompatible:/usr/bin:/bin" MOCK_STATE="$state_incompatible" WELTGEWEBE_DEPLOY_LOCK_FILE="$state_incompatible/deploy.lock" REPO_DIR="$repo_incompatible" ENV_FILE="$repo_incompatible/.env" DEPLOY_TARGET=vps DEPLOY_FRONTEND_MODE=internal bash scripts/weltgewebe-up --no-pull --no-build --deploy-scope api) 2>&1)"
rc_incompatible=$?
set -e
[[ "$rc_incompatible" -ne 0 ]] || fail "internal frontend mode should be incompatible with API scope"
assert_contains "$out_incompatible" "requires DEPLOY_FRONTEND_MODE=off"
[[ ! -s "$state_incompatible/mutation.log" ]] || fail "incompatible frontend mode mutated compose"

echo "PASS: bounded scopes reject frontend and Caddy effects"
echo "test_weltgewebe_up_deploy_scope: OK"
