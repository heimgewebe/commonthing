// Execute the real T048 k6 module with synthetic HTTP responses. No network or k6 runner.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createContext, SourceTextModule, SyntheticModule } from 'node:vm';

const source = readFileSync(new URL('../../performance/api_runtime_k6.js', import.meta.url), 'utf8');
const healthy = { database: true, nats: true, event_chain: true, policy: true };

async function simulate({ live = 200, ready = 200, search = 200, checks = healthy, malformed = false }) {
  const counters = new Map();
  const statuses = { live, ready, search };
  const outcomes = [];
  const assertions = new Map();
  let defaultResponseCallback;

  class Counter {
    constructor(name) { this.name = name; counters.set(name, 0); }
    add(n) { assert.equal(n, 1); counters.set(this.name, counters.get(this.name) + n); }
  }
  const http = {
    expectedStatuses(...accepted) {
      return (response) => accepted.includes(response.status);
    },
    setResponseCallback(fn) { defaultResponseCallback = fn; },
    get(url, options = {}) {
      const path = new URL(url).pathname;
      const route = path === '/health/live' ? 'live'
        : path === '/health/ready' ? 'ready'
          : path === '/search' ? 'search' : null;
      assert.ok(route, 'unexpected requested path: ' + path);
      const response = {
        status: statuses[route],
        json() {
          if (malformed && route === 'ready') throw new SyntaxError('synthetic invalid JSON');
          return { status: ready === 200 ? 'ok' : 'error', checks };
        },
      };
      const responseCallback = options.responseCallback ?? defaultResponseCallback;
      assert.equal(typeof responseCallback, 'function');
      outcomes.push({ route, failed: !responseCallback(response) });
      return response;
    },
  };
  const check = (response, conditions) => {
    for (const [name, predicate] of Object.entries(conditions)) {
      assertions.set(name, predicate(response));
    }
  };
  const context = createContext({
    __ENV: {
      BASE_URL: 'http://127.0.0.1:8787',
      API_RUNTIME_SEARCH_QUERY: 'scale',
      API_RUNTIME_DATASET_PROFILE: 'domain-scale-ci',
      API_RUNTIME_DATASET_MANIFEST_SHA256: 'a'.repeat(64),
      API_RUNTIME_RUN_ID: 'ct1940-ready-classification-test',
      API_RUNTIME_K6_IMAGE: 'grafana/k6@sha256:' + 'b'.repeat(64),
    },
  });
  const module = new SourceTextModule(source, { context });
  await module.link((specifier) => {
    if (specifier === 'k6/http') {
      return new SyntheticModule(['default'], function () { this.setExport('default', http); }, { context });
    }
    if (specifier === 'k6/metrics') {
      return new SyntheticModule(['Counter'], function () { this.setExport('Counter', Counter); }, { context });
    }
    if (specifier === 'k6') {
      return new SyntheticModule(['check'], function () { this.setExport('check', check); }, { context });
    }
    throw new Error('Unexpected dependency in actual k6 module: ' + specifier);
  });
  await module.evaluate();
  module.namespace.default();
  return {
    failed: outcomes.filter((item) => item.failed).map((item) => item.route),
    counters: Object.fromEntries([...counters.entries()].filter(([, v]) => v > 0)),
    assertions: Object.fromEntries(assertions),
  };
}

const cases = [
  {
    name: 'all endpoints healthy',
    input: {},
    failed: [],
    counters: { t048_ready_samples: 1 },
    readinessPass: true,
  },
  {
    name: 'database and event-chain failing readiness',
    input: { ready: 503, checks: { ...healthy, database: false, event_chain: false } },
    failed: ['ready'],
    counters: {
      t048_ready_samples: 1,
      t048_ready_http_503: 1,
      t048_ready_check_false_database: 1,
      t048_ready_check_false_event_chain: 1,
    },
    readinessPass: false,
  },
  {
    name: '503 without readable component flags',
    input: { ready: 503, malformed: true },
    failed: ['ready'],
    counters: { t048_ready_samples: 1, t048_ready_http_503: 1, t048_ready_unclassified: 1 },
    readinessPass: false,
  },
  {
    name: 'unexpected readiness 502',
    input: { ready: 502 },
    failed: ['ready'],
    counters: { t048_ready_samples: 1 },
    readinessPass: false,
  },
  {
    name: 'search failure remains an HTTP failure',
    input: { search: 503 },
    failed: ['search'],
    counters: { t048_ready_samples: 1 },
    readinessPass: true,
  },
  {
    name: 'liveness failure remains an HTTP failure',
    input: { live: 503 },
    failed: ['live'],
    counters: { t048_ready_samples: 1 },
    readinessPass: true,
  },
];

for (const item of cases) {
  const result = await simulate(item.input);
  assert.deepEqual(result.failed, item.failed, item.name + ': HTTP failure classification');
  assert.deepEqual(result.counters, item.counters, item.name + ': diagnostic counters');
  assert.equal(result.assertions['ready 200'], item.readinessPass, item.name + ': readiness check');
}
console.log('T048 k6 real-module HTTP failure classification: ' + cases.length + ' scenarios passed');
