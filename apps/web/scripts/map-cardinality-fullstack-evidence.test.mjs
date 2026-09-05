import assert from "node:assert/strict";
import test from "node:test";
import {
  MAP_CARDINALITY_FULLSTACK_BUDGETS,
  buildMapCardinalityFullstackEvidence,
  validateMapCardinalityFullstackSample,
} from "./map-cardinality-fullstack-evidence.mjs";
import {
  expectedMapCardinalityItems,
  expectedMapCardinalityPages,
} from "./map-cardinality-evidence.mjs";

const SOURCE_REVISION = "a".repeat(40);

function sample(cardinality, overrides = {}) {
  const budget = MAP_CARDINALITY_FULLSTACK_BUDGETS[cardinality];
  const items = expectedMapCardinalityItems(cardinality);
  return {
    cardinality,
    page_size: 1000,
    api_request_count: expectedMapCardinalityPages(cardinality),
    api_response_bytes: Math.min(budget.max_api_response_bytes, items * 256),
    api_error_count: 0,
    source_node_count: cardinality,
    bbox_source_item_count: items,
    offscreen_source_item_count: cardinality - items,
    loaded_item_count: items,
    truncated_by_client_limit: false,
    source_has_more_after_last_client_page: false,
    bbox_scoped_request_count: expectedMapCardinalityPages(cardinality),
    bulk_node_request_count: 0,
    readiness_ms: Math.min(1500, budget.readiness_ms),
    interaction_to_next_paint_ms: Math.min(
      50,
      budget.interaction_to_next_paint_ms,
    ),
    api_response_p95_ms: Math.min(25, budget.api_response_p95_ms),
    frame_time_p95_ms: Math.min(20, budget.frame_time_p95_ms),
    frame_time_max_ms: 35,
    js_heap_used_bytes: Math.min(
      48 * 1024 * 1024,
      budget.max_js_heap_used_bytes,
    ),
    dom_marker_count: cardinality > 1000 ? 0 : 100,
    native_layer_expected: cardinality > 1000,
    native_layer_actual: cardinality > 1000,
    seeded_after_api_start: true,
    ...overrides,
  };
}

function build(overrides = {}) {
  return buildMapCardinalityFullstackEvidence({
    sourceRevision: SOURCE_REVISION,
    generatedAt: "2026-09-05T00:00:00Z",
    browser: { name: "chromium", version: "test", headless: true },
    backend: {
      api_build_commit: SOURCE_REVISION,
      database_kind: "postgresql",
      startup_node_count: 0,
      seeded_after_api_start: true,
    },
    samples: [sample(1000), sample(10000), sample(100000)],
    ...overrides,
  });
}

test("accepts bounded real fullstack samples for 1k/10k/100k", () => {
  for (const cardinality of [1000, 10000, 100000]) {
    assert.equal(
      validateMapCardinalityFullstackSample(sample(cardinality)).cardinality,
      cardinality,
    );
  }
  assert.equal(build().verdict, "PASS");
});

test("fails closed when the backend is not exact-revision PostgreSQL", () => {
  assert.throws(
    () =>
      build({
        backend: {
          api_build_commit: "b".repeat(40),
          database_kind: "postgresql",
          startup_node_count: 0,
          seeded_after_api_start: true,
        },
      }),
    /API build commit/,
  );
  assert.throws(
    () =>
      build({
        backend: {
          api_build_commit: SOURCE_REVISION,
          database_kind: "fixture",
          startup_node_count: 0,
          seeded_after_api_start: true,
        },
      }),
    /PostgreSQL backend/,
  );
  assert.throws(
    () =>
      build({
        backend: {
          api_build_commit: SOURCE_REVISION,
          database_kind: "postgresql",
          startup_node_count: 1,
          seeded_after_api_start: true,
        },
      }),
    /empty domain_nodes/,
  );
});

test("new memory, frame and API latency budgets fail closed", () => {
  const budget = MAP_CARDINALITY_FULLSTACK_BUDGETS[100000];
  assert.throws(
    () =>
      validateMapCardinalityFullstackSample(
        sample(100000, {
          js_heap_used_bytes: budget.max_js_heap_used_bytes + 1,
        }),
      ),
    /JS heap/,
  );
  assert.throws(
    () =>
      validateMapCardinalityFullstackSample(
        sample(100000, { frame_time_p95_ms: budget.frame_time_p95_ms + 1 }),
      ),
    /frame p95/,
  );
  assert.throws(
    () =>
      validateMapCardinalityFullstackSample(
        sample(100000, { api_response_p95_ms: budget.api_response_p95_ms + 1 }),
      ),
    /API p95/,
  );
});

test("requires bbox-only real API loading and post-start database seeding", () => {
  assert.throws(
    () =>
      validateMapCardinalityFullstackSample(
        sample(10000, { bulk_node_request_count: 1 }),
      ),
    /bulk node bootstrap/,
  );
  assert.throws(
    () =>
      validateMapCardinalityFullstackSample(
        sample(10000, { bbox_scoped_request_count: 1, api_request_count: 2 }),
      ),
    /every real node request must be bbox scoped/,
  );
  assert.throws(
    () =>
      validateMapCardinalityFullstackSample(
        sample(10000, { seeded_after_api_start: false }),
      ),
    /seeded after API startup/,
  );
});

test("100k global growth must stay viewport-, transfer- and heap-bounded", () => {
  const tenK = sample(10000, {
    api_response_bytes: 400000,
    js_heap_used_bytes: 48 * 1024 * 1024,
  });
  assert.throws(
    () =>
      buildMapCardinalityFullstackEvidence({
        sourceRevision: SOURCE_REVISION,
        generatedAt: "2026-09-05T00:00:00Z",
        browser: { name: "chromium", version: "test", headless: true },
        backend: {
          api_build_commit: SOURCE_REVISION,
          database_kind: "postgresql",
          startup_node_count: 0,
          seeded_after_api_start: true,
        },
        samples: [
          sample(1000),
          tenK,
          sample(100000, { api_response_bytes: 600000 }),
        ],
      }),
    /transfer grew materially/,
  );

  assert.throws(
    () =>
      buildMapCardinalityFullstackEvidence({
        sourceRevision: SOURCE_REVISION,
        generatedAt: "2026-09-05T00:00:00Z",
        browser: { name: "chromium", version: "test", headless: true },
        backend: {
          api_build_commit: SOURCE_REVISION,
          database_kind: "postgresql",
          startup_node_count: 0,
          seeded_after_api_start: true,
        },
        samples: [
          sample(1000),
          tenK,
          sample(100000, { js_heap_used_bytes: 96 * 1024 * 1024 }),
        ],
      }),
    /JS heap grew materially/,
  );
});

test("receipt states its measurement limits instead of overclaiming production capacity", () => {
  const evidence = build();
  assert.equal(evidence.kind, "commonthing_map_cardinality_fullstack_evidence");
  assert.deepEqual(evidence.client_limits, { max_pages: 10, max_items: 10000 });
  assert.match(evidence.limitations[0], /loopback/);
  assert.match(evidence.limitations[1], /after the API is already ready/);
  assert.match(evidence.limitations[2], /does not represent total browser/);
  assert.match(evidence.limitations[4], /basemap style/);
});
