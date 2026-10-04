#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" > /dev/null 2>&1 && pwd)"
CONTRACT_FILE="${SCRIPT_DIR}/postgres-proof-contract.json"

: "${DATABASE_URL:?DATABASE_URL must point at a direct disposable PostgreSQL database}"
export PG_DIRECT_URL="${PG_DIRECT_URL:-$DATABASE_URL}"
export T005_DATABASE_URL="${T005_DATABASE_URL:-$PG_DIRECT_URL}"
export FEDERATION_TEST_DATABASE_URL="$PG_DIRECT_URL"
export AUTH_PG_003_FIXTURE_MUTATION=1

owned_nats_container=""
cleanup_owned_nats() {
  if [[ -n "$owned_nats_container" ]]; then
    docker rm --force "$owned_nats_container" > /dev/null 2>&1 || true
  fi
}
cleanup_exit() {
  local exit_code=$?
  trap - EXIT INT TERM
  cleanup_owned_nats
  exit "$exit_code"
}
cleanup_int() {
  trap - EXIT INT TERM
  cleanup_owned_nats
  exit 130
}
cleanup_term() {
  trap - EXIT INT TERM
  cleanup_owned_nats
  exit 143
}
trap cleanup_exit EXIT
trap cleanup_int INT
trap cleanup_term TERM

contract_value() {
  local key="$1"
  python3 - "$CONTRACT_FILE" "$key" << 'PY'
import json, sys
contract=json.load(open(sys.argv[1], encoding='utf-8'))
if contract.get('schema_version') != 1:
    raise SystemExit('unsupported postgres proof contract schema')
value=contract.get(sys.argv[2])
if value is None:
    raise SystemExit(f'missing postgres proof contract key: {sys.argv[2]}')
if isinstance(value, (list, dict)):
    print(json.dumps(value, separators=(',', ':')))
else:
    print(value)
PY
}

validate_database_url() {
  local url="$1"
  python3 - "$CONTRACT_FILE" "$url" << 'PY'
import json, re, sys
from urllib.parse import unquote, urlsplit
contract=json.load(open(sys.argv[1], encoding='utf-8'))
if contract.get('schema_version') != 1:
    raise SystemExit('unsupported postgres proof contract schema')
segments=contract.get('disposable_database_name_segments') or []
if not segments:
    raise SystemExit('postgres proof contract has no disposable database segments')
url=urlsplit(sys.argv[2])
if url.scheme not in {'postgres','postgresql'}:
    raise SystemExit(f'unsupported PostgreSQL URL scheme: {url.scheme!r}')
if not url.hostname:
    raise SystemExit('PostgreSQL URL must contain a host')
port=url.port or 5432
if port == int(contract['pgbouncer_port']):
    raise SystemExit('PostgreSQL integration proofs require direct PostgreSQL, not PgBouncer')
raw_database=url.path.lstrip('/')
if '%' in raw_database:
    raise SystemExit('PostgreSQL proof database path must not contain percent-encoding')
database=unquote(raw_database)
if not re.fullmatch(r'[A-Za-z0-9_.-]+', database):
    raise SystemExit(f'refusing unsafe disposable database identifier: {database!r}')
database_segments=[segment for segment in database.replace('-', '_').replace('.', '_').split('_') if segment]
if not database.startswith('weltgewebe_') or not database_segments or database_segments[-1] not in segments:
    raise SystemExit(
        f'refusing non-disposable database {database!r}; expected weltgewebe_ prefix and final delimited segment from {segments!r}'
    )
host=url.hostname.rstrip('.').lower()
print(f'{host}\t{port}\t{database}')
PY
}

validated_database="$(validate_database_url "$DATABASE_URL")"
validated_pg_direct="$(validate_database_url "$PG_DIRECT_URL")"
validated_t005="$(validate_database_url "$T005_DATABASE_URL")"
IFS=$'\t' read -r DATABASE_HOST DATABASE_PORT DATABASE_NAME <<< "$validated_database"
IFS=$'\t' read -r PG_DIRECT_HOST PG_DIRECT_PORT PG_DIRECT_DATABASE_NAME <<< "$validated_pg_direct"
IFS=$'\t' read -r T005_HOST T005_PORT T005_DATABASE_NAME <<< "$validated_t005"
for endpoint_field in DATABASE_HOST DATABASE_PORT DATABASE_NAME PG_DIRECT_HOST PG_DIRECT_PORT PG_DIRECT_DATABASE_NAME T005_HOST T005_PORT T005_DATABASE_NAME; do
  if [[ -z "${!endpoint_field}" ]]; then
    echo "PostgreSQL proof URL validation returned an empty field: ${endpoint_field}" >&2
    exit 1
  fi
done
DATABASE_ENDPOINT="${DATABASE_HOST}:${DATABASE_PORT}/${DATABASE_NAME}"
PG_DIRECT_ENDPOINT="${PG_DIRECT_HOST}:${PG_DIRECT_PORT}/${PG_DIRECT_DATABASE_NAME}"
T005_ENDPOINT="${T005_HOST}:${T005_PORT}/${T005_DATABASE_NAME}"
if [[ "$DATABASE_ENDPOINT" == "$PG_DIRECT_ENDPOINT" && "$DATABASE_ENDPOINT" == "$T005_ENDPOINT" ]]; then
  :
else
  echo "PostgreSQL proof URLs must target the same direct endpoint: DATABASE_URL=${DATABASE_ENDPOINT}, PG_DIRECT_URL=${PG_DIRECT_ENDPOINT}, T005_DATABASE_URL=${T005_ENDPOINT}" >&2
  exit 1
fi

load_t003_connection() {
  local tmp_file
  local -a values=()
  tmp_file="$(mktemp)"
  if ! python3 - "$PG_DIRECT_URL" > "$tmp_file" << 'PY'; then
import sys
from urllib.parse import unquote, urlsplit
url=urlsplit(sys.argv[1])
values=(
    url.hostname or '',
    str(url.port or 5432),
    unquote(url.username or ''),
    unquote(url.password or ''),
    unquote(url.path.lstrip('/')),
)
if not values[0] or not values[2] or not values[4]:
    raise SystemExit('PostgreSQL URL must contain host, user and database')
for value in values:
    sys.stdout.write(value)
    sys.stdout.write('\0')
PY

    rm -f "$tmp_file"
    return 1
  fi
  mapfile -d '' -t values < "$tmp_file"
  rm -f "$tmp_file"
  if ((${#values[@]} != 5)); then
    echo 'failed to derive direct T003 PostgreSQL connection parameters' >&2
    return 1
  fi
  export T003_PG_HOST="${values[0]}"
  export T003_PG_PORT="${values[1]}"
  export T003_PG_USER="${values[2]}"
  export T003_PG_PASSWORD="${values[3]}"
  export T003_PG_DATABASE="${values[4]}"
}
load_t003_connection

postgres_admin_url() {
  python3 - "$PG_DIRECT_URL" << 'PY'
import sys
from urllib.parse import urlsplit, urlunsplit
url=urlsplit(sys.argv[1])
print(urlunsplit((url.scheme, url.netloc, '/postgres', url.query, url.fragment)))
PY
}

validate_reset_container_binding() {
  [[ -n "${POSTGRES_RESET_CONTAINER:-}" ]] || return 0
  command -v docker > /dev/null 2>&1 || {
    echo 'PostgreSQL proof container binding validation requires docker' >&2
    return 1
  }
  case "$PG_DIRECT_HOST" in
    localhost | 127.0.0.1 | ::1) ;;
    *)
      echo "POSTGRES_RESET_CONTAINER requires a loopback PG_DIRECT_URL, got host ${PG_DIRECT_HOST}" >&2
      return 1
      ;;
  esac
  local -a bindings=()
  mapfile -t bindings < <(docker port "$POSTGRES_RESET_CONTAINER" 5432/tcp)
  if [[ "${#bindings[@]}" -ne 1 ]]; then
    echo "PostgreSQL reset container must expose exactly one 5432/tcp binding; found ${#bindings[@]}" >&2
    return 1
  fi
  python3 - "${bindings[0]}" "$PG_DIRECT_PORT" << 'PY'
import ipaddress, sys
binding=sys.argv[1].strip()
expected_port=int(sys.argv[2])
if binding.startswith('['):
    host, sep, tail=binding[1:].partition(']:')
    if not sep:
        raise SystemExit(f'invalid Docker port binding: {binding!r}')
    port=int(tail)
else:
    host, sep, tail=binding.rpartition(':')
    if not sep:
        raise SystemExit(f'invalid Docker port binding: {binding!r}')
    port=int(tail)
try:
    loopback=ipaddress.ip_address(host).is_loopback
except ValueError:
    loopback=host.lower() == 'localhost'
if not loopback:
    raise SystemExit(f'PostgreSQL reset container binding must be loopback-only, got {host!r}')
if port != expected_port:
    raise SystemExit(
        f'PostgreSQL reset container published port {port} does not match PG_DIRECT_URL port {expected_port}'
    )
PY
}

preflight_postgres() {
  local admin
  admin="$(postgres_admin_url)"
  if [[ -n "${POSTGRES_RESET_CONTAINER:-}" ]]; then
    validate_reset_container_binding
    command -v docker > /dev/null 2>&1 || {
      echo 'PostgreSQL proof container preflight requires docker' >&2
      return 1
    }
    if ! docker exec "$POSTGRES_RESET_CONTAINER" pg_isready -U postgres -d postgres > /dev/null 2>&1; then
      echo "PostgreSQL proof preflight container is not ready: ${POSTGRES_RESET_CONTAINER}" >&2
      return 1
    fi
    if ! docker exec "$POSTGRES_RESET_CONTAINER" psql -U postgres -d postgres -X --no-psqlrc -v ON_ERROR_STOP=1 -Atqc 'SELECT 1' > /dev/null; then
      echo "PostgreSQL proof preflight container query failed: ${POSTGRES_RESET_CONTAINER}" >&2
      return 1
    fi
    echo "PostgreSQL proof preflight: isolated container ready; disposable database=${DATABASE_NAME}"
    return 0
  fi
  if ! command -v psql > /dev/null 2>&1; then
    echo 'PostgreSQL proof preflight requires psql before the first test' >&2
    return 1
  fi
  if ! psql "$admin" -X --no-psqlrc -v ON_ERROR_STOP=1 -Atqc 'SELECT 1' > /dev/null; then
    echo 'PostgreSQL proof preflight could not reach the direct PostgreSQL admin database' >&2
    return 1
  fi
  echo "PostgreSQL proof preflight: direct server ready; disposable database=${DATABASE_NAME}"
}

check_jetstream_once() {
  python3 - "$NATS_URL" << 'PY'
import json, socket, sys
from urllib.parse import urlsplit
url=urlsplit(sys.argv[1])
if url.scheme != 'nats' or not url.hostname:
    raise SystemExit('NATS_URL must be a plain nats:// URL for the integration proof')
try:
    with socket.create_connection((url.hostname, url.port or 4222), timeout=2.0) as sock:
        sock.settimeout(2.0)
        data=b''
        while b'\r\n' not in data and len(data) < 65536:
            chunk=sock.recv(4096)
            if not chunk:
                break
            data += chunk
except OSError:
    raise SystemExit(1)
line=data.split(b'\r\n',1)[0]
if not line.startswith(b'INFO '):
    raise SystemExit(f'NATS readiness expected INFO line, got {line[:80]!r}')
try:
    info=json.loads(line[5:].decode('utf-8'))
except (UnicodeDecodeError, json.JSONDecodeError):
    raise SystemExit('NATS readiness received malformed INFO JSON')
if info.get('jetstream') is not True:
    raise SystemExit('NATS server is reachable but JetStream is not enabled')
print('JetStream proof preflight: semantic INFO jetstream=true')
PY
}

wait_for_jetstream() {
  for _ in {1..30}; do
    if check_jetstream_once; then
      return 0
    fi
    sleep 1
  done
  echo "JetStream proof preflight failed for ${NATS_URL}" >&2
  if [[ -n "$owned_nats_container" ]]; then
    docker logs "$owned_nats_container" >&2 || true
  fi
  return 1
}

provision_jetstream_if_requested() {
  if [[ "${POSTGRES_PROOF_PROVISION_NATS:-0}" != "1" ]]; then
    : "${NATS_URL:?NATS_URL is required unless POSTGRES_PROOF_PROVISION_NATS=1}"
    wait_for_jetstream
    return
  fi
  command -v docker > /dev/null 2>&1 || {
    echo 'JetStream proof provisioning requires docker' >&2
    return 1
  }
  local image binding port suffix
  image="$(contract_value jetstream_image)"
  suffix="${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-0}-$$"
  owned_nats_container="weltgewebe-ci-nats-${suffix}"
  docker run --detach --rm \
    --name "$owned_nats_container" \
    --publish 127.0.0.1::4222 \
    "$image" -js > /dev/null
  local -a bindings=()
  mapfile -t bindings < <(docker port "$owned_nats_container" 4222/tcp)
  if [[ "${#bindings[@]}" -ne 1 ]]; then
    echo "JetStream proof container must expose exactly one 4222/tcp binding; found ${#bindings[@]}" >&2
    return 1
  fi
  binding="${bindings[0]}"
  port="${binding##*:}"
  if [[ ! "$port" =~ ^[0-9]+$ ]]; then
    echo "could not resolve dynamically published JetStream port from ${binding}" >&2
    return 1
  fi
  export NATS_URL="nats://127.0.0.1:${port}"
  wait_for_jetstream
}

reset_database() {
  local admin
  if [[ "${POSTGRES_PROOF_ALLOW_RESET:-0}" != "1" ]]; then
    echo 'PostgreSQL proof reset refused: set POSTGRES_PROOF_ALLOW_RESET=1 for destructive disposable-database reset' >&2
    return 1
  fi
  case "$PG_DIRECT_HOST" in
    localhost | 127.0.0.1 | ::1) ;;
    *)
      echo "PostgreSQL proof reset refused for non-loopback host ${PG_DIRECT_HOST}; destructive CI proofs must use an isolated local server" >&2
      return 1
      ;;
  esac
  admin="$(postgres_admin_url)"
  if [[ -n "${POSTGRES_RESET_CONTAINER:-}" ]]; then
    validate_reset_container_binding
    docker exec "$POSTGRES_RESET_CONTAINER" dropdb -U postgres --if-exists --force "$DATABASE_NAME" > /dev/null
    docker exec "$POSTGRES_RESET_CONTAINER" createdb -U postgres "$DATABASE_NAME"
    return
  fi
  command -v dropdb > /dev/null 2>&1 || {
    echo 'PostgreSQL proof reset requires dropdb' >&2
    return 1
  }
  command -v createdb > /dev/null 2>&1 || {
    echo 'PostgreSQL proof reset requires createdb' >&2
    return 1
  }
  dropdb --maintenance-db="$admin" --if-exists --force "$DATABASE_NAME" > /dev/null
  createdb --maintenance-db="$admin" "$DATABASE_NAME" > /dev/null
}

preflight_postgres
provision_jetstream_if_requested

if [[ "${POSTGRES_PROOF_PREFLIGHT_ONLY:-0}" == "1" ]]; then
  echo 'PostgreSQL integration proof preflight-only mode: PASS'
  exit 0
fi

targets=(
  db_migration_checksum_recovery
  db_auto_provision_write_path
  db_domain_account_write_path
  db_domain_backfill
  db_domain_edge_write_path
  db_domain_node_write_path
  db_governance
  db_federation_persistence
  db_multi_instance_foundation
  db_ortsweberei_webgemeindezentrum
  db_domain_read_path
  db_domain_schema_migrations
  db_passkey_fk_readiness
  db_passkey_schema_preflight
  db_passkey_store_persistence
  db_session_store_persistence
  db_webauthn_user_id_backfill_audit
  db_semantic_search_foundation
  db_semantic_search_projection_worker
  db_semantic_search_owner_lifecycle
  sqlx_postgres_direct_session_crud
)

if [[ -n "${POSTGRES_PROOF_TARGETS:-}" ]]; then
  read -r -a targets <<< "$POSTGRES_PROOF_TARGETS"
fi

for target in "${targets[@]}"; do
  printf '=== PostgreSQL proof: %s ===\n' "$target"
  reset_database
  AUTH_GUEST_EXIT_STEP_UP_ENABLED=1 \
    cargo test --locked -p weltgewebe-api --test "$target" -- --include-ignored --test-threads=1
done

# The required all-target CI lane also executes the existing conversation Push proof.
# Targeted proof workflows set POSTGRES_PROOF_TARGETS and intentionally stay scoped.
if [[ -z "${POSTGRES_PROOF_TARGETS:-}" ]]; then
  printf '=== PostgreSQL proof: push_device_limit_recovery ===\n'
  reset_database
  cargo test --locked -p weltgewebe-api --test db_node_conversations \
    private_message_push_preference_cancels_queued_delivery \
    -- --include-ignored --test-threads=1

  # Federation delivery keeps its PostgreSQL proofs as ignored unit tests in
  # src/, which `--test <target>` never reaches. Run them by exact name and
  # fail unless every one of them actually ran and passed.
  federation_delivery_db_tests=(
    postgres_reconcile_noop_preserves_timestamps_and_delivery_state
    postgres_reconcile_empty_bindings_clear_all_delivery_endpoints
    postgres_reconcile_clear_only_batch_handles_nullable_arrays
    postgres_reconcile_updates_and_backfills_multiple_changed_peers
    postgres_reconcile_validates_before_mutation_and_rolls_back_unknown_peer
    postgres_reconcile_backfills_trusted_but_not_blocked_peer
    postgres_batch_backfill_preserves_existing_delivery_state_semantics
    postgres_reconcile_preserves_sequential_duplicate_binding_semantics
    postgres_delivery_retries_and_is_multi_instance_safe
  )
  printf '=== PostgreSQL proof: federation_delivery (lib) ===\n'
  reset_database
  federation_log="$(mktemp)"
  cargo test --locked -p weltgewebe-api --lib federation_delivery::tests::postgres_ \
    -- --ignored --test-threads=1 2>&1 | tee "$federation_log"
  for test_name in "${federation_delivery_db_tests[@]}"; do
    if ! grep -Fxq "test federation_delivery::tests::${test_name} ... ok" "$federation_log"; then
      echo "federation delivery PostgreSQL proof did not pass: ${test_name}" >&2
      exit 1
    fi
  done
  federation_ran="$(grep -c '^test federation_delivery::tests::postgres_.* \.\.\. ' "$federation_log" || true)"
  if [[ "$federation_ran" -ne "${#federation_delivery_db_tests[@]}" ]]; then
    echo "federation delivery PostgreSQL proof ran ${federation_ran} tests, expected ${#federation_delivery_db_tests[@]}" >&2
    exit 1
  fi
  rm -f "$federation_log"
fi
