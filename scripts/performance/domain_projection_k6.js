// CQ-02 measurement workload: exercise the PostgreSQL-backed domain projection
// through real API reads and authenticated writes. This file deliberately does
// not define pass/fail latency thresholds; it collects evidence so the audit can
// decide whether an architecture change is justified.
import http from 'k6/http';
import { check } from 'k6';
import { Counter, Trend } from 'k6/metrics';

const BASE_URL = __ENV.CQ02_BASE_URL;
const SESSION_ID = __ENV.CQ02_SESSION_ID;
const WRITE_NODE_IDS = (__ENV.CQ02_WRITE_NODE_IDS || '')
  .split(',')
  .map((id) => id.trim())
  .filter((id) => id.length > 0);
const PROFILE = __ENV.CQ02_PROFILE;
const WORKLOAD = __ENV.CQ02_WORKLOAD;
const RUN_ID = __ENV.CQ02_RUN_ID;
const DURATION_SECONDS = Number(__ENV.CQ02_DURATION_SECONDS || 30);
const READ_VUS = Number(__ENV.CQ02_READ_VUS || 10);
const WRITE_MAX_VUS = 4;
const WRITE_RATE = 1;
const WRITE_TIME_UNIT = '2s';
const WRITE_RATE_PER_SECOND = 0.5;

if (!BASE_URL) throw new Error('CQ02_BASE_URL is required');
if (!PROFILE) throw new Error('CQ02_PROFILE is required');
if (!['read_heavy', 'mixed'].includes(WORKLOAD)) {
  throw new Error('CQ02_WORKLOAD must be read_heavy or mixed');
}
if (!RUN_ID || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(RUN_ID)) {
  throw new Error('CQ02_RUN_ID has an invalid format');
}
if (WORKLOAD === 'mixed' && !SESSION_ID) {
  throw new Error('CQ02_SESSION_ID is required for mixed workload');
}
if (WORKLOAD === 'mixed' && WRITE_NODE_IDS.length < WRITE_MAX_VUS) {
  throw new Error(
    `CQ02_WRITE_NODE_IDS must provide at least ${WRITE_MAX_VUS} existing nodes for mixed workload`,
  );
}
if (!Number.isInteger(DURATION_SECONDS) || DURATION_SECONDS <= 0) {
  throw new Error('CQ02_DURATION_SECONDS must be a positive integer');
}
if (!Number.isInteger(READ_VUS) || READ_VUS <= 0) {
  throw new Error('CQ02_READ_VUS must be a positive integer');
}

const readDuration = new Trend('cq02_read_duration_ms', true);
const writeDuration = new Trend('cq02_write_duration_ms', true);
const readRequests = new Counter('cq02_read_requests_total');
const writeRequests = new Counter('cq02_write_requests_total');
const writeSuccesses = new Counter('cq02_write_successes_total');
const readFailures = new Counter('cq02_read_failures_total');
const writeFailures = new Counter('cq02_write_failures_total');
const status503 = new Counter('cq02_503_total');

http.setResponseCallback(http.expectedStatuses(200));
const WRITE_RESPONSE_CALLBACK = http.expectedStatuses(200);

const scenarios = {
  readers: {
    executor: 'constant-vus',
    vus: READ_VUS,
    duration: `${DURATION_SECONDS}s`,
    exec: 'readNodes',
    gracefulStop: '5s',
  },
};
if (WORKLOAD === 'mixed') {
  // Keep the offered rate at one mutation every two seconds. The production
  // default permits 30 patch/replace mutations per account per minute, so 0.5/s
  // exercises repeated reloads without benchmarking an unsustainable client rate.
  // Open-loop arrival still avoids coordinated omission if reloads become slow.
  scenarios.writer = {
    executor: 'constant-arrival-rate',
    rate: WRITE_RATE,
    timeUnit: WRITE_TIME_UNIT,
    duration: `${DURATION_SECONDS}s`,
    preAllocatedVUs: 2,
    maxVUs: WRITE_MAX_VUS,
    exec: 'writeNode',
    gracefulStop: '5s',
  };
}

export const options = {
  scenarios,
  summaryTrendStats: ['avg', 'min', 'med', 'p(50)', 'p(95)', 'p(99)', 'max'],
};

export function readNodes() {
  const response = http.get(`${BASE_URL}/nodes?limit=100`);
  readRequests.add(1);
  readDuration.add(response.timings.duration);
  if (response.status === 503) status503.add(1);
  if (response.status !== 200) readFailures.add(1);
  check(response, { 'nodes read 200': (r) => r.status === 200 });
}

function operationOrdinal() {
  // __ITER is local to a VU. Combining it with __VU keeps each patch payload
  // distinct when the constant-arrival-rate executor temporarily uses more VUs.
  return (__VU * 100000000 + __ITER) % 1000000000000;
}

function recordLogicalWriteFailure(startedAtMs, response, phase) {
  writeFailures.add(1);
  writeDuration.add(Date.now() - startedAtMs);
  if (response && response.status === 503) status503.add(1);
  const status = response ? response.status : 'invalid-response';
  console.error(`CQ02 logical write failed phase=${phase} status=${status}`);
}

export function writeNode() {
  const startedAtMs = Date.now();
  writeRequests.add(1);

  // Each writer VU gets a stable existing fixture node. That prevents the load
  // generator from creating its own optimistic-lock conflict if a slow reload
  // causes several open-loop writer VUs to be active at the same time.
  const writeNodeId = WRITE_NODE_IDS[(__VU - 1) % WRITE_NODE_IDS.length];
  const current = http.get(`${BASE_URL}/nodes/${writeNodeId}`, {
    headers: {
      Cookie: `gewebe_session=${SESSION_ID}`,
    },
    responseCallback: WRITE_RESPONSE_CALLBACK,
  });
  if (current.status !== 200) {
    check(current, { 'node precondition read 200': () => false });
    recordLogicalWriteFailure(startedAtMs, current, 'precondition-read');
    return;
  }
  check(current, { 'node precondition read 200': () => true });

  let updatedAt;
  try {
    const currentNode = current.json();
    updatedAt = currentNode && currentNode.updated_at;
  } catch (_error) {
    recordLogicalWriteFailure(startedAtMs, current, 'precondition-json');
    return;
  }
  if (typeof updatedAt !== 'string' || updatedAt.length === 0) {
    recordLogicalWriteFailure(startedAtMs, current, 'precondition-etag');
    return;
  }

  const ordinal = operationOrdinal();
  // PATCH keeps the fixture cardinality fixed. POST /nodes also creates an
  // origin Faden and therefore hits the 500k edge ceiling in the 100k profile
  // before it can exercise projection reloads. The API requires If-Match with
  // the exact current updated_at value, so each logical write reads that value
  // immediately before patching.
  const payload = JSON.stringify({
    info: `Synthetic CQ-02 projection reload measurement ${ordinal}`,
  });
  const response = http.patch(`${BASE_URL}/nodes/${writeNodeId}`, payload, {
    headers: {
      'Content-Type': 'application/json',
      'If-Match': `"${updatedAt}"`,
      Origin: BASE_URL,
      Cookie: `gewebe_session=${SESSION_ID}`,
    },
    responseCallback: WRITE_RESPONSE_CALLBACK,
  });
  if (response.status !== 200) {
    check(response, { 'node patch 200': () => false });
    recordLogicalWriteFailure(startedAtMs, response, 'patch');
    return;
  }

  writeSuccesses.add(1);
  writeDuration.add(Date.now() - startedAtMs);
  check(response, { 'node patch 200': () => true });
}

export function handleSummary(data) {
  const outputPath = __ENV.CQ02_SUMMARY_PATH || 'cq02-k6-summary.json';
  const enriched = Object.assign({}, data, {
    cq02: {
      run_id: RUN_ID,
      profile: PROFILE,
      workload: WORKLOAD,
      duration_seconds: DURATION_SECONDS,
      read_vus: READ_VUS,
      write_rate_per_second:
        WORKLOAD === 'mixed' ? WRITE_RATE_PER_SECOND : 0,
    },
  });
  return { [outputPath]: JSON.stringify(enriched) };
}
