// Isolated event-chain probe, NOT the canonical mixed search/health T048 workload.
// A 503 is an error here: the API must already have healthy PostgreSQL,
// JetStream, Relay and ReceiptConsumer before sampling begins.
import http from 'k6/http';
import { Counter } from 'k6/metrics';

for (const name of ['PROOF_RUN_ID', 'PROOF_VARIANT', 'PROOF_PHASE', 'PROOF_GIT_HEAD', 'PROOF_IMAGE_ID']) {
  if (!__ENV[name]) throw new Error(`${name} is required`);
}
if (!/^[0-9a-f]{40}$/.test(__ENV.PROOF_GIT_HEAD)) throw new Error('invalid head');
if (!/^sha256:[0-9a-f]{64}$/.test(__ENV.PROOF_IMAGE_ID)) throw new Error('invalid image identity');
if (!['baseline', 'fix'].includes(__ENV.PROOF_VARIANT)) throw new Error('invalid variant');
if (!['recent', 'aged'].includes(__ENV.PROOF_PHASE)) throw new Error('invalid event age phase');
if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(__ENV.PROOF_RUN_ID)) throw new Error('invalid run id');

const READY_200 = new Counter('proof_ready_200');
const READY_503 = new Counter('proof_ready_503');
const READY_503_EVENT_CHAIN_TIMEOUT = new Counter('proof_ready_503_event_chain_timeout');
const READY_503_OTHER_CAUSE = new Counter('proof_ready_503_other_cause');
const READY_OTHER = new Counter('proof_ready_other');

http.setResponseCallback(http.expectedStatuses(200));

export const options = {
  vus: 10,
  duration: '30s',
  summaryTrendStats: ['avg', 'min', 'med', 'p(50)', 'p(95)', 'p(99)', 'max'],
};

export default function () {
  const response = http.get('http://127.0.0.1:8787/health/ready', { timeout: '2s' });
  if (response.status === 200) READY_200.add(1);
  else if (response.status === 503) {
    READY_503.add(1);
    let body;
    try {
      body = response.json();
    } catch (_) {
      // A malformed 503 is never credited to the event-chain timeout.
    }
    const checks = body && body.checks;
    const errors = body && body.errors;
    const eventErrors = errors && errors.event_chain;
    const eventChainTimeoutOnly =
      checks && checks.event_chain === false &&
      checks.database === true && checks.nats === true && checks.policy === true &&
      Array.isArray(eventErrors) &&
      eventErrors.includes('readiness check timed out after 750 ms') &&
      !errors.database && !errors.nats && !errors.policy;
    if (eventChainTimeoutOnly) READY_503_EVENT_CHAIN_TIMEOUT.add(1);
    else READY_503_OTHER_CAUSE.add(1);
  } else READY_OTHER.add(1);
}

export function handleSummary(data) {
  const meta = {
    schema_version: 1,
    run_id: __ENV.PROOF_RUN_ID,
    variant: __ENV.PROOF_VARIANT,
    phase: __ENV.PROOF_PHASE,
    git_head: __ENV.PROOF_GIT_HEAD,
    image_id: __ENV.PROOF_IMAGE_ID,
    event_count: 140001,
    event_age_seconds: __ENV.PROOF_PHASE === 'recent' ? 120 : 720,
    virtual_users: 10,
    duration_seconds: 30,
  };
  const output = Object.assign({}, data, { event_chain_proof: meta });
  const path = __ENV.PROOF_SUMMARY_PATH;
  if (!path || !path.startsWith('/evidence/') || !path.endsWith('.json')) {
    throw new Error('PROOF_SUMMARY_PATH must be an absolute /evidence JSON path');
  }
  return { [path]: JSON.stringify(output) };
}
