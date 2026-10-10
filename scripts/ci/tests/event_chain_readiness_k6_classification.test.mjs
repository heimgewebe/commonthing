// Directly execute the production k6 module against synthetic HTTP bodies.
// No k6 process, database, network access, transpilation or new dependencies.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createContext, SourceTextModule, SyntheticModule } from 'node:vm';

const source = readFileSync(new URL('../../performance/event_chain_readiness_k6.js', import.meta.url), 'utf8');
const timeoutText = 'readiness check timed out after 750 ms';
const healthy = { database: true, nats: true, event_chain: true, policy: true };

async function classify(input) {
  const counters = new Map();
  class Counter {
    constructor(name) { this.name = name; counters.set(name, 0); }
    add(value) { assert.equal(value, 1); counters.set(this.name, counters.get(this.name) + value); }
  }
  const http = {
    expectedStatuses(code) { assert.equal(code, 200); return () => true; },
    setResponseCallback() {},
    get(url, options) {
      assert.equal(url, 'http://127.0.0.1:8787/health/ready');
      assert.equal(options.timeout, '2s');
      return {
        status: input.status,
        json() {
          if (input.malformed) throw new SyntaxError('synthetic invalid JSON');
          return input.body;
        },
      };
    },
  };
  const context = createContext({
    __ENV: {
      PROOF_RUN_ID: 'ct1940-k6-direct-test',
      PROOF_VARIANT: 'baseline',
      PROOF_PHASE: 'recent',
      PROOF_GIT_HEAD: 'a'.repeat(40),
      PROOF_IMAGE_ID: 'sha256:' + 'b'.repeat(64),
      PROOF_SUMMARY_PATH: '/evidence/direct.json',
    },
  });
  const module = new SourceTextModule(source, { context });
  await module.link((specifier) => {
    if (specifier === 'k6/http') {
      return new SyntheticModule(['default'], function () {
        this.setExport('default', http);
      }, { context });
    }
    if (specifier === 'k6/metrics') {
      return new SyntheticModule(['Counter'], function () {
        this.setExport('Counter', Counter);
      }, { context });
    }
    throw new Error('Unexpected import in production k6 code: ' + specifier);
  });
  await module.evaluate();
  module.namespace.default();
  return Object.fromEntries([...counters.entries()].filter(([, count]) => count > 0));
}

const cases = [
  {
    name: 'healthy 200',
    input: { status: 200, body: { status: 'ok', checks: healthy } },
    expected: { proof_ready_200: 1 },
  },
  {
    name: '200 with skipped event chain is incomplete',
    input: { status: 200, body: { status: 'ok', checks: { ...healthy, event_chain: null } } },
    expected: { proof_ready_200: 1, proof_ready_200_incomplete: 1 },
  },
  {
    name: '200 without valid readiness status is incomplete',
    input: { status: 200, body: { status: 'degraded', checks: healthy } },
    expected: { proof_ready_200: 1, proof_ready_200_incomplete: 1 },
  },
  {
    name: 'malformed 200 is incomplete',
    input: { status: 200, malformed: true },
    expected: { proof_ready_200: 1, proof_ready_200_incomplete: 1 },
  },
  {
    name: 'exclusive 750 ms Event-Chain timeout',
    input: { status: 503, body: {
      checks: { ...healthy, event_chain: false },
      errors: { event_chain: [timeoutText] },
    } },
    expected: {
      proof_ready_503: 1,
      proof_ready_503_event_chain_timeout: 1,
      proof_ready_503_check_false_event_chain: 1,
    },
  },
  {
    name: 'mixed database failure and Event-Chain timeout',
    input: { status: 503, body: {
      checks: { ...healthy, event_chain: false, database: false },
      errors: { database: ['database connection refused'], event_chain: [timeoutText] },
    } },
    expected: {
      proof_ready_503: 1,
      proof_ready_503_other_cause: 1,
      proof_ready_503_event_chain_timeout_mixed: 1,
      proof_ready_503_check_false_event_chain: 1,
      proof_ready_503_check_false_database: 1,
    },
  },
  {
    name: 'mixed NATS failure and Event-Chain timeout',
    input: { status: 503, body: {
      checks: { ...healthy, event_chain: false, nats: false },
      errors: { nats: ['JetStream unavailable'], event_chain: [timeoutText] },
    } },
    expected: {
      proof_ready_503: 1,
      proof_ready_503_other_cause: 1,
      proof_ready_503_event_chain_timeout_mixed: 1,
      proof_ready_503_check_false_event_chain: 1,
      proof_ready_503_check_false_nats: 1,
    },
  },
  {
    name: 'missing durable receipt is not 750 ms timeout',
    input: { status: 503, body: {
      checks: { ...healthy, event_chain: false },
      errors: { event_chain: ['1 event(s) without a durable receipt'] },
    } },
    expected: {
      proof_ready_503: 1,
      proof_ready_503_other_cause: 1,
      proof_ready_503_check_false_event_chain: 1,
    },
  },
  {
    name: 'worker-down without timeout is not 750 ms timeout',
    input: { status: 503, body: {
      checks: { ...healthy, event_chain: false },
      errors: { event_chain: ['receipt consumer worker is down'] },
    } },
    expected: {
      proof_ready_503: 1,
      proof_ready_503_other_cause: 1,
      proof_ready_503_check_false_event_chain: 1,
    },
  },
  {
    name: 'malformed 503 is not attributed',
    input: { status: 503, malformed: true },
    expected: {
      proof_ready_503: 1,
      proof_ready_503_other_cause: 1,
      proof_ready_503_parse_error: 1,
    },
  },
  {
    name: 'other HTTP status is not healthy',
    input: { status: 502, body: {} },
    expected: { proof_ready_other: 1 },
  },
];

for (const item of cases) {
  const actual = await classify(item.input);
  assert.deepStrictEqual(actual, item.expected, item.name);
}
console.log('k6 production classification passed ' + cases.length + ' synthetic HTTP scenarios');
