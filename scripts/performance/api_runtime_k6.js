// Mixed-health-and-read workload for the api_runtime measurement declared in
// policies/performance.v1.json (measurements.api_runtime.scenario).
//
// This script only produces samples (a k6 JSON summary via handleSummary).
// scripts/performance/api_runtime_evidence.py applies the canonical
// thresholds from that policy file, so the pass/fail decision has exactly
// one source of truth. Do not add k6 `thresholds` here: a second, drifting
// copy of the budget would defeat that single-source-of-truth guarantee.
import http from 'k6/http';
import { check } from 'k6';

const BASE_URL = __ENV.BASE_URL;
if (!BASE_URL) {
  throw new Error('BASE_URL is required, e.g. http://127.0.0.1:8080');
}

// The query is evidence, not a convenience default: a run must explicitly
// name the search workload whose live database binding was captured.
const SEARCH_QUERY = __ENV.API_RUNTIME_SEARCH_QUERY;
if (!SEARCH_QUERY) {
  throw new Error('API_RUNTIME_SEARCH_QUERY is required');
}
const DATASET_PROFILE = __ENV.API_RUNTIME_DATASET_PROFILE;
if (!DATASET_PROFILE) {
  throw new Error('API_RUNTIME_DATASET_PROFILE is required');
}
const DATASET_MANIFEST_SHA256 = __ENV.API_RUNTIME_DATASET_MANIFEST_SHA256;
if (
  !DATASET_MANIFEST_SHA256 ||
  !/^[0-9a-f]{64}$/.test(DATASET_MANIFEST_SHA256)
) {
  throw new Error('API_RUNTIME_DATASET_MANIFEST_SHA256 must be a 64-hex sha256');
}
const RUN_ID = __ENV.API_RUNTIME_RUN_ID;
if (!RUN_ID || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(RUN_ID)) {
  throw new Error('API_RUNTIME_RUN_ID has an invalid format');
}
const K6_IMAGE = __ENV.API_RUNTIME_K6_IMAGE;
if (!K6_IMAGE || !/^.+@sha256:[0-9a-f]{64}$/.test(K6_IMAGE)) {
  throw new Error('API_RUNTIME_K6_IMAGE must be an exact @sha256 image reference');
}
const CONCURRENCY_PROFILE =
  __ENV.API_RUNTIME_CONCURRENCY_PROFILE || 'mixed-health-and-read';

// k6's http_req_failed metric only reflects network-level errors by default
// (DNS/TCP/timeout), never HTTP status codes, which would make the
// http_request_failed_rate gate blind to real 5xx responses. Only
// /health/ready legitimately reports 503 for expected unavailability (its
// dependencies are still starting up); every other endpoint, including
// /search, must return 200 to count as a success, so a 503 there is a real
// failure and must count against http_req_failed. The 503 tolerance below is
// therefore scoped to the /health/ready request only, not set globally.
http.setResponseCallback(http.expectedStatuses(200));
const READY_RESPONSE_CALLBACK = http.expectedStatuses(200, 503);

export const options = {
  vus: Number(__ENV.API_RUNTIME_VUS || 10),
  duration: `${Number(__ENV.API_RUNTIME_DURATION_SECONDS || 30)}s`,
  // Explicit percentiles so scripts/performance/api_runtime_evidence.py can
  // read exact p50/p95/p99 values out of the summary without guessing at
  // k6's default stat set.
  summaryTrendStats: ['avg', 'min', 'med', 'p(50)', 'p(95)', 'p(99)', 'max'],
};

export default function () {
  const live = http.get(`${BASE_URL}/health/live`);
  check(live, { 'live 200': (r) => r.status === 200 });

  const ready = http.get(`${BASE_URL}/health/ready`, {
    responseCallback: READY_RESPONSE_CALLBACK,
  });
  check(ready, { 'ready 2xx/5xx': (r) => r.status === 200 || r.status === 503 });

  // The only read endpoint in the mix that is wired to PostgreSQL repository
  // telemetry (search_repository_duration_seconds); scripts/performance/
  // api_runtime_evidence.py cross-checks its request count against that
  // histogram's observation count as its deterministic query-count evidence.
  // Unlike /health/ready, /search has no legitimate 503 case in this
  // scenario, so it uses the default (200-only) response callback: a 503
  // here is real evidence of /search unavailability and must surface in
  // http_req_failed rather than being silently tolerated.
  const search = http.get(
    `${BASE_URL}/search?q=${encodeURIComponent(SEARCH_QUERY)}&limit=5`,
  );
  check(search, {
    'search 200': (r) => r.status === 200,
  });
}

// Binds this run's k6 summary to the exact scenario, dataset/query workload,
// run identity, and digest-pinned k6 image declared by the invoking
// experiment. The live binding and resource receipts independently record
// the same run identity; the assembler rejects disagreement.
export function handleSummary(data) {
  const loadFinishedAtUnixMs = Date.now();
  const testRunDurationMs = data?.state?.testRunDurationMs;
  if (!Number.isFinite(testRunDurationMs) || testRunDurationMs <= 0) {
    throw new Error('k6 summary testRunDurationMs is invalid');
  }
  const loadStartedAtUnixMs = Math.floor(loadFinishedAtUnixMs - testRunDurationMs);
  if (loadStartedAtUnixMs < 0 || loadStartedAtUnixMs >= loadFinishedAtUnixMs) {
    throw new Error('k6 load wall-clock interval is invalid');
  }
  const enriched = Object.assign({}, data, {
    weltgewebe_dataset_manifest_sha256: DATASET_MANIFEST_SHA256,
    weltgewebe_search_query: SEARCH_QUERY,
    weltgewebe_run_id: RUN_ID,
    weltgewebe_load_started_at_unix_ms: loadStartedAtUnixMs,
    weltgewebe_load_finished_at_unix_ms: loadFinishedAtUnixMs,
    weltgewebe_k6_image: K6_IMAGE,
    weltgewebe_scenario: {
      virtual_users: options.vus,
      duration_seconds: Number(String(options.duration).replace(/s$/, '')),
      dataset_profile: DATASET_PROFILE,
      concurrency_profile: CONCURRENCY_PROFILE,
      search_query: SEARCH_QUERY,
    },
  });
  const outputPath =
    __ENV.API_RUNTIME_SUMMARY_PATH || 'api-runtime-summary.json';
  const encoded = JSON.stringify(enriched);
  if (outputPath === 'stdout') {
    return { stdout: `__WELTGEWEBE_K6_SUMMARY_V1__${encoded}\n` };
  }
  return { [outputPath]: encoded };
}