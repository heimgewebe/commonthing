#!/usr/bin/env bash
# Opt-in GitHub Actions experiment; do not run against staging or production.
# Measures recently published versus aged, fully receipted events; this does
# not reproduce historical deployment cold-start state.
set -Eeuo pipefail

[[ "${GITHUB_ACTIONS:-}" == "true" && -n "${RUNNER_TEMP:-}" && -n "${GITHUB_RUN_ID:-}" ]]
[[ "${DATABASE_URL:-}" == "postgres://postgres:postgres@127.0.0.1:5432/postgres" ]]
[[ "${NATS_URL:-}" == "nats://127.0.0.1:4222" ]]

BASELINE="4164c5b337c7d09dc4e3229b0705b9a2076fd9e9"
FIX="66c77f39de4a67783e8ab42daa305193d8e4450d"
ROOT="${RUNNER_TEMP}/ct1940-event-chain-proof"
[[ ! -e "${ROOT}" ]] || {
  echo "proof directory already exists" >&2
  exit 1
}
mkdir -m 700 "${ROOT}"
RUN_ID="ct1940-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT:-1}"
NATS_CONTAINER="ct1940-nats-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT:-1}"
API_CONTAINER="ct1940-api-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT:-1}"
K6_IMAGE="grafana/k6@sha256:65c920dc067d5e2e00befbf982af6ad6ad0117034e8b1c65817c7975c52d4669"
BASE_IMAGE="ct1940-api-baseline:${GITHUB_RUN_ID}"
FIX_IMAGE="ct1940-api-fix:${GITHUB_RUN_ID}"

cleanup() {
  local prior=$? cleanup_failed=0 names
  trap - EXIT
  set +e
  docker rm --force "${API_CONTAINER}" > /dev/null 2>&1 || true
  docker rm --force "${NATS_CONTAINER}" > /dev/null 2>&1 || true
  for variant in baseline fix; do
    if [[ -d "${ROOT}/${variant}" ]]; then
      git worktree remove "${ROOT}/${variant}" > /dev/null 2>&1 || cleanup_failed=1
    fi
    [[ ! -e "${ROOT}/${variant}" ]] || cleanup_failed=1
  done
  for image in "${BASE_IMAGE}" "${FIX_IMAGE}"; do
    if docker image inspect "${image}" > /dev/null 2>&1; then
      docker image rm "${image}" > /dev/null 2>&1 || cleanup_failed=1
    fi
    if docker image inspect "${image}" > /dev/null 2>&1; then cleanup_failed=1; fi
  done
  names="$(docker ps -a --format '{{.Names}}')" || cleanup_failed=1
  if printf '%s\n' "${names}" | grep -Fxq "${API_CONTAINER}"; then cleanup_failed=1; fi
  if printf '%s\n' "${names}" | grep -Fxq "${NATS_CONTAINER}"; then cleanup_failed=1; fi
  # Resource teardown is a separate observation from the experiment exit.
  # An inconclusive/failed performance comparison may still clean up fully.
  if [[ ${cleanup_failed} -eq 0 ]]; then
    echo '{"schema_version":1,"status":"pass","api_removed":true,"nats_removed":true,"worktrees_removed":true,"images_removed":true}' > "${ROOT}/teardown.json"
  else
    echo '{"schema_version":1,"status":"fail"}' > "${ROOT}/teardown.json"
    prior=1
  fi
  exit "${prior}"
}
trap cleanup EXIT

# Exact Git objects must exist in the checkout; never fetch floating refs for comparison.
for sha in "${BASELINE}" "${FIX}"; do
  git cat-file -e "${sha}^{commit}"
done
git -c advice.detachedHead=false worktree add --detach "${ROOT}/baseline" "${BASELINE}"
git -c advice.detachedHead=false worktree add --detach "${ROOT}/fix" "${FIX}"
test "$(git -C "${ROOT}/baseline" rev-parse HEAD)" = "${BASELINE}"
test "$(git -C "${ROOT}/fix" rev-parse HEAD)" = "${FIX}"
# Verify that exactly the intended readiness implementation differs.
[[ "$(git diff --name-only "${BASELINE}" "${FIX}")" == "apps/api/src/routes/health.rs" ]] || {
  echo "unexpected differences between baseline and fix revisions" >&2
  exit 1
}

NATS_IMAGE="$(python3 -c 'import json;print(json.load(open("scripts/ci/postgres-proof-contract.json"))["jetstream_image"])')"
[[ "${NATS_IMAGE}" =~ ^nats@sha256:[0-9a-f]{64}$ ]]
docker pull "${NATS_IMAGE}"
docker pull "${K6_IMAGE}"
docker run --detach --rm --name "${NATS_CONTAINER}" --network host "${NATS_IMAGE}" -js > /dev/null
python3 - << 'PY'
import socket
import time
deadline = time.monotonic() + 35
while time.monotonic() < deadline:
    try:
        with socket.create_connection(("127.0.0.1", 4222), timeout=1):
            break
    except OSError:
        time.sleep(0.3)
else:
    raise SystemExit("isolated JetStream never became reachable")
PY

build_image() {
  local variant="$1" sha="$2" image="$3"
  local path="${ROOT}/${variant}" timestamp
  timestamp="$(git -C "${path}" show -s --format=%cI HEAD)"
  [[ "${timestamp}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T ]]
  docker build --file "${path}/apps/api/Dockerfile" --tag "${image}" \
    --build-arg "GIT_COMMIT_SHA=${sha}" \
    --build-arg "BUILD_TIMESTAMP=${timestamp}" \
    --build-arg CARGO_BUILD_JOBS=2 "${path}"
  local identity
  identity="$(docker image inspect "${image}" --format '{{.Id}}')"
  [[ "${identity}" =~ ^sha256:[0-9a-f]{64}$ ]] || return 1
  printf '%s' "${identity}" > "${ROOT}/${variant}.image"
}
build_image baseline "${BASELINE}" "${BASE_IMAGE}"
build_image fix "${FIX}" "${FIX_IMAGE}"

# Same migration version in both compared Git objects (PR #1944 only changed
# health.rs). Migrations run once on this disposable PostgreSQL service.
docker run --rm --network host \
  --env "DATABASE_URL=${DATABASE_URL}" \
  --env WELTGEWEBE_API_STARTUP_MIGRATIONS=run \
  --env WELTGEWEBE_API_MIGRATION_ONLY=1 "${FIX_IMAGE}"

start_api() {
  local variant="$1" sha="$2" image="$3"
  docker rm --force "${API_CONTAINER}" > /dev/null 2>&1 || true
  docker run --detach --rm --name "${API_CONTAINER}" --network host \
    --env API_BIND=127.0.0.1:8787 \
    --env "DATABASE_URL=${DATABASE_URL}" --env "NATS_URL=${NATS_URL}" \
    --env WELTGEWEBE_DOMAIN_READ_SOURCE=postgres \
    --env WELTGEWEBE_DOMAIN_JETSTREAM_REPLICAS=1 \
    --env WELTGEWEBE_API_STARTUP_MIGRATIONS=verify-applied \
    --env READINESS_VERBOSE=true \
    "${image}" > /dev/null
  # Bind the running container, not only the local image tag, to the measured SHA.
  [[ "$(docker inspect "${API_CONTAINER}" --format '{{.Image}}')" == "$(cat "${ROOT}/${variant}.image")" ]]
  local consecutive=0
  for _ in $(seq 1 90); do
    if curl --fail --silent --output /dev/null --max-time 2 http://127.0.0.1:8787/health/ready; then
      consecutive=$((consecutive + 1))
    else
      consecutive=0
    fi
    if [[ ${consecutive} -ge 3 ]]; then break; fi
    sleep 1
  done
  [[ ${consecutive} -ge 3 ]] || {
    echo "${variant}: startup never had 3 successful readiness probes" >&2
    exit 1
  }
  curl --fail --silent --show-error http://127.0.0.1:8787/metrics > "${ROOT}/${variant}-startup.prom"
  grep -F "commit=\"${sha}\"" "${ROOT}/${variant}-startup.prom" > /dev/null
  grep -Eq '^domain_event_worker_up\{worker="relay"\} 1([.]0)?$' "${ROOT}/${variant}-startup.prom"
  grep -Eq '^domain_event_worker_up\{worker="receipt_consumer"\} 1([.]0)?$' "${ROOT}/${variant}-startup.prom"
  printf '%s\n' "${variant}: read/write event chain healthy before synthetic load"
}

seed_recent() {
  # Explicitly isolated service DB; events are already published and receipted,
  # so the live relay/consumer remains healthy without processing synthetic payloads.
  psql "${DATABASE_URL}" -v ON_ERROR_STOP=1 << 'SQL' > /dev/null
TRUNCATE TABLE domain_event_consumptions, domain_outbox RESTART IDENTITY CASCADE;
INSERT INTO domain_outbox (
  aggregate_type, aggregate_id, event_type, payload,
  created_at, available_at, published_at
)
SELECT 'readiness-proof', 'fixture-' || g, 'fixture', '{}'::jsonb,
       NOW() - INTERVAL '2 minutes',
       NOW() - INTERVAL '2 minutes',
       NOW() - INTERVAL '2 minutes'
  FROM generate_series(1, 140001) AS g;
INSERT INTO domain_event_consumptions (consumer_name, event_id)
SELECT 'weltgewebe-api-domain-receipts-v1', id
  FROM domain_outbox
 WHERE aggregate_type = 'readiness-proof';
ANALYZE domain_outbox;
ANALYZE domain_event_consumptions;
SQL
  local event_count receipt_count
  event_count="$(psql "${DATABASE_URL}" -At -c "SELECT count(*) FROM domain_outbox WHERE aggregate_type='readiness-proof'")"
  receipt_count="$(psql "${DATABASE_URL}" -At -c "SELECT count(*) FROM domain_event_consumptions WHERE consumer_name='weltgewebe-api-domain-receipts-v1'")"
  [[ "${event_count}" == "140001" && "${receipt_count}" == "140001" ]]
}

age_events() {
  psql "${DATABASE_URL}" -v ON_ERROR_STOP=1 << 'SQL' > /dev/null
UPDATE domain_outbox
   SET published_at = NOW() - INTERVAL '12 minutes'
 WHERE aggregate_type = 'readiness-proof';
ANALYZE domain_outbox;
SQL
  [[ "$(psql "${DATABASE_URL}" -At -c "SELECT count(*) FROM domain_outbox WHERE aggregate_type='readiness-proof' AND published_at < NOW() - INTERVAL '10 minutes'")" == "140001" ]]
}

measure() {
  local variant="$1" phase="$2" sha="$3"
  local image_id filename
  image_id="$(cat "${ROOT}/${variant}.image")"
  filename="/evidence/${variant}-${phase}.json"
  curl --fail --silent --show-error http://127.0.0.1:8787/metrics > "${ROOT}/${variant}-${phase}-before.prom"
  docker run --rm --network host \
    --user "$(id -u):$(id -g)" \
    --volume "${PWD}:/workspace:ro" \
    --volume "${ROOT}:/evidence" \
    --workdir /workspace \
    --env "PROOF_RUN_ID=${RUN_ID}" \
    --env "PROOF_VARIANT=${variant}" \
    --env "PROOF_PHASE=${phase}" \
    --env "PROOF_GIT_HEAD=${sha}" \
    --env "PROOF_IMAGE_ID=${image_id}" \
    --env "PROOF_SUMMARY_PATH=${filename}" \
    "${K6_IMAGE}" run scripts/performance/event_chain_readiness_k6.js
  curl --fail --silent --show-error http://127.0.0.1:8787/metrics > "${ROOT}/${variant}-${phase}-after.prom"
  # A fast response is not healthy when relay or receipt consumer has exited.
  grep -F "commit=\"${sha}\"" "${ROOT}/${variant}-${phase}-after.prom" > /dev/null
  for worker in relay receipt_consumer; do
    grep -Eq "^domain_event_worker_up\\{worker=\"${worker}\"\\} 1([.]0)?$" "${ROOT}/${variant}-${phase}-after.prom"
  done
  [[ -s "${ROOT}/${variant}-${phase}.json" ]]
}

# Run the FIX first. Host and PostgreSQL cache warming would otherwise
# spuriously favor the new revision run second after a cold baseline. Both
# revisions receive the same 140001 receipted recent and aged-event fixtures.
start_api fix "${FIX}" "${FIX_IMAGE}"
seed_recent
measure fix recent "${FIX}"
age_events
measure fix aged "${FIX}"
start_api baseline "${BASELINE}" "${BASE_IMAGE}"
seed_recent
measure baseline recent "${BASELINE}"
age_events
measure baseline aged "${BASELINE}"

python3 - "${ROOT}/manifest.json" "${RUN_ID}" "${ROOT}/baseline.image" "${ROOT}/fix.image" << 'PY'
import json
import pathlib
import sys
path, run_id, baseline, fix = sys.argv[1:]
manifest = {
    "schema_version": 1,
    "run_id": run_id,
    "baseline_sha": "4164c5b337c7d09dc4e3229b0705b9a2076fd9e9",
    "fix_sha": "66c77f39de4a67783e8ab42daa305193d8e4450d",
    "event_count": 140001,
    "duration_seconds": 30,
    "virtual_users": 10,
    "startup_ready": {"baseline": True, "fix": True},
    "run_order": ["fix", "baseline"],
    "images": {
        "baseline": pathlib.Path(baseline).read_text(encoding="utf-8"),
        "fix": pathlib.Path(fix).read_text(encoding="utf-8"),
    },
}
pathlib.Path(path).write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
PY
python3 -B scripts/performance/event_chain_readiness_proof.py \
  --manifest "${ROOT}/manifest.json" \
  --baseline-recent "${ROOT}/baseline-recent.json" \
  --baseline-aged "${ROOT}/baseline-aged.json" \
  --fix-recent "${ROOT}/fix-recent.json" \
  --fix-aged "${ROOT}/fix-aged.json" \
  --report "${ROOT}/report.json"
