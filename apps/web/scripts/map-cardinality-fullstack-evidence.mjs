import { mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import {
  MAP_CARDINALITY_CLIENT_MAX_ITEMS,
  MAP_CARDINALITY_CLIENT_MAX_PAGES,
  MAP_CARDINALITY_PAGE_SIZE,
  expectedMapCardinalityItems,
  expectedMapCardinalityPages,
} from "./map-cardinality-evidence.mjs";

const MIB = 1024 * 1024;

export const MAP_CARDINALITY_FULLSTACK_BUDGETS = Object.freeze({
  1000: Object.freeze({
    readiness_ms: 8000,
    interaction_to_next_paint_ms: 250,
    api_response_p95_ms: 500,
    frame_time_p95_ms: 50,
    max_js_heap_used_bytes: 160 * MIB,
    max_dom_markers: 250,
    max_api_response_bytes: 600_000,
  }),
  10000: Object.freeze({
    readiness_ms: 12000,
    interaction_to_next_paint_ms: 250,
    api_response_p95_ms: 500,
    frame_time_p95_ms: 60,
    max_js_heap_used_bytes: 192 * MIB,
    max_dom_markers: 1,
    max_api_response_bytes: 2_200_000,
  }),
  100000: Object.freeze({
    readiness_ms: 15000,
    interaction_to_next_paint_ms: 300,
    api_response_p95_ms: 500,
    frame_time_p95_ms: 60,
    max_js_heap_used_bytes: 192 * MIB,
    max_dom_markers: 1,
    max_api_response_bytes: 2_200_000,
  }),
});

function finite(value, label) {
  if (!Number.isFinite(value) || value < 0) {
    throw new Error(`${label} must be a finite number >= 0`);
  }
  return value;
}

function integer(value, label, minimum = 0) {
  if (!Number.isInteger(value) || value < minimum) {
    throw new Error(`${label} must be an integer >= ${minimum}`);
  }
  return value;
}

function gitSha(value, label) {
  if (typeof value !== "string" || !/^[0-9a-f]{40}$/.test(value)) {
    throw new Error(`${label} must be a 40-character Git SHA`);
  }
  return value;
}

export function validateMapCardinalityFullstackSample(sample) {
  if (!sample || typeof sample !== "object" || Array.isArray(sample)) {
    throw new Error("sample must be an object");
  }
  const cardinality = integer(sample.cardinality, "sample.cardinality", 1);
  const budget = MAP_CARDINALITY_FULLSTACK_BUDGETS[cardinality];
  if (!budget) throw new Error(`unsupported cardinality ${cardinality}`);

  integer(sample.page_size, "sample.page_size", 1);
  integer(sample.api_request_count, "sample.api_request_count", 1);
  integer(sample.api_response_bytes, "sample.api_response_bytes", 1);
  integer(sample.api_error_count, "sample.api_error_count", 0);
  integer(sample.source_node_count, "sample.source_node_count", 1);
  integer(sample.bbox_source_item_count, "sample.bbox_source_item_count", 1);
  integer(
    sample.offscreen_source_item_count,
    "sample.offscreen_source_item_count",
    0,
  );
  integer(sample.loaded_item_count, "sample.loaded_item_count", 1);
  integer(
    sample.bbox_scoped_request_count,
    "sample.bbox_scoped_request_count",
    1,
  );
  integer(sample.bulk_node_request_count, "sample.bulk_node_request_count", 0);
  integer(sample.dom_marker_count, "sample.dom_marker_count", 0);
  finite(sample.readiness_ms, "sample.readiness_ms");
  finite(
    sample.interaction_to_next_paint_ms,
    "sample.interaction_to_next_paint_ms",
  );
  finite(sample.api_response_p95_ms, "sample.api_response_p95_ms");
  finite(sample.frame_time_p95_ms, "sample.frame_time_p95_ms");
  finite(sample.frame_time_max_ms, "sample.frame_time_max_ms");
  finite(sample.js_heap_used_bytes, "sample.js_heap_used_bytes");

  if (sample.page_size !== MAP_CARDINALITY_PAGE_SIZE) {
    throw new Error(
      `page size ${sample.page_size} does not match ${MAP_CARDINALITY_PAGE_SIZE}`,
    );
  }
  if (sample.source_node_count !== cardinality) {
    throw new Error(
      `cardinality ${cardinality}: PostgreSQL contains ${sample.source_node_count} nodes`,
    );
  }
  const expectedItems = expectedMapCardinalityItems(cardinality);
  const expectedPages = expectedMapCardinalityPages(cardinality);
  if (sample.bbox_source_item_count !== expectedItems) {
    throw new Error(
      `cardinality ${cardinality}: real API loaded ${sample.bbox_source_item_count} bbox items, expected ${expectedItems}`,
    );
  }
  if (sample.offscreen_source_item_count !== cardinality - expectedItems) {
    throw new Error(
      `cardinality ${cardinality}: offscreen source count does not reconcile with PostgreSQL cardinality`,
    );
  }
  if (sample.loaded_item_count !== expectedItems) {
    throw new Error(
      `cardinality ${cardinality}: expected ${expectedItems} browser items, observed ${sample.loaded_item_count}`,
    );
  }
  if (sample.api_request_count < expectedPages) {
    throw new Error(
      `cardinality ${cardinality}: expected at least ${expectedPages} real node requests, observed ${sample.api_request_count}`,
    );
  }
  if (sample.bbox_scoped_request_count !== sample.api_request_count) {
    throw new Error(
      `cardinality ${cardinality}: every real node request must be bbox scoped`,
    );
  }
  if (sample.bulk_node_request_count !== 0) {
    throw new Error(
      `cardinality ${cardinality}: bulk node bootstrap request observed`,
    );
  }
  if (sample.api_error_count !== 0) {
    throw new Error(
      `cardinality ${cardinality}: observed ${sample.api_error_count} API errors`,
    );
  }
  if (sample.truncated_by_client_limit !== false) {
    throw new Error(
      `cardinality ${cardinality}: viewport fetch must complete without client truncation`,
    );
  }
  if (sample.source_has_more_after_last_client_page !== false) {
    throw new Error(
      `cardinality ${cardinality}: viewport source continuation must be exhausted`,
    );
  }
  if (sample.seeded_after_api_start !== true) {
    throw new Error(
      `cardinality ${cardinality}: PostgreSQL fixture must be seeded after API startup`,
    );
  }
  if (sample.readiness_ms > budget.readiness_ms) {
    throw new Error(
      `cardinality ${cardinality}: readiness ${sample.readiness_ms}ms exceeds ${budget.readiness_ms}ms`,
    );
  }
  if (
    sample.interaction_to_next_paint_ms > budget.interaction_to_next_paint_ms
  ) {
    throw new Error(
      `cardinality ${cardinality}: interaction ${sample.interaction_to_next_paint_ms}ms exceeds ${budget.interaction_to_next_paint_ms}ms`,
    );
  }
  if (sample.api_response_p95_ms > budget.api_response_p95_ms) {
    throw new Error(
      `cardinality ${cardinality}: API p95 ${sample.api_response_p95_ms}ms exceeds ${budget.api_response_p95_ms}ms`,
    );
  }
  if (sample.frame_time_p95_ms > budget.frame_time_p95_ms) {
    throw new Error(
      `cardinality ${cardinality}: frame p95 ${sample.frame_time_p95_ms}ms exceeds ${budget.frame_time_p95_ms}ms`,
    );
  }
  if (sample.js_heap_used_bytes > budget.max_js_heap_used_bytes) {
    throw new Error(
      `cardinality ${cardinality}: JS heap ${sample.js_heap_used_bytes} exceeds ${budget.max_js_heap_used_bytes} bytes`,
    );
  }
  if (sample.dom_marker_count > budget.max_dom_markers) {
    throw new Error(
      `cardinality ${cardinality}: DOM marker count ${sample.dom_marker_count} exceeds ${budget.max_dom_markers}`,
    );
  }
  if (sample.api_response_bytes > budget.max_api_response_bytes) {
    throw new Error(
      `cardinality ${cardinality}: API response bytes ${sample.api_response_bytes} exceeds ${budget.max_api_response_bytes}`,
    );
  }
  const nativeExpected = expectedItems > MAP_CARDINALITY_PAGE_SIZE;
  if (sample.native_layer_expected !== nativeExpected) {
    throw new Error(
      `cardinality ${cardinality}: native layer expectation is inconsistent`,
    );
  }
  if (sample.native_layer_actual !== nativeExpected) {
    throw new Error(
      `cardinality ${cardinality}: native layer actual state does not match expectation`,
    );
  }
  return { cardinality, budget };
}

export function buildMapCardinalityFullstackEvidence({
  sourceRevision,
  generatedAt,
  browser,
  backend,
  samples,
}) {
  gitSha(sourceRevision, "sourceRevision");
  if (!backend || typeof backend !== "object" || Array.isArray(backend)) {
    throw new Error("backend must be an object");
  }
  gitSha(backend.api_build_commit, "backend.api_build_commit");
  if (backend.api_build_commit !== sourceRevision) {
    throw new Error(
      "API build commit must equal the exact browser proof revision",
    );
  }
  if (backend.database_kind !== "postgresql") {
    throw new Error("fullstack proof requires PostgreSQL backend");
  }
  if (backend.seeded_after_api_start !== true) {
    throw new Error("fullstack proof must seed PostgreSQL after API startup");
  }
  if (
    integer(backend.startup_node_count, "backend.startup_node_count", 0) !== 0
  ) {
    throw new Error("API must start against an empty domain_nodes table");
  }
  if (!Array.isArray(samples) || samples.length !== 3) {
    throw new Error("samples must contain exactly the 1k/10k/100k scenarios");
  }
  const seen = samples.map(
    (sample) => validateMapCardinalityFullstackSample(sample).cardinality,
  );
  if (seen.join(",") !== "1000,10000,100000") {
    throw new Error(`unexpected cardinality sequence: ${seen.join(",")}`);
  }

  const tenThousand = samples[1];
  const hundredThousand = samples[2];
  if (
    hundredThousand.source_node_count !==
    tenThousand.source_node_count * 10
  ) {
    throw new Error(
      "100k PostgreSQL source must contain ten times the 10k source",
    );
  }
  if (
    hundredThousand.bbox_source_item_count !==
    tenThousand.bbox_source_item_count
  ) {
    throw new Error(
      "10k and 100k runs must expose the same viewport population",
    );
  }
  if (hundredThousand.loaded_item_count !== tenThousand.loaded_item_count) {
    throw new Error("10k and 100k browser loads must stay viewport-bounded");
  }
  if (
    hundredThousand.api_response_bytes >
    tenThousand.api_response_bytes * 1.25
  ) {
    throw new Error(
      "100k real API transfer grew materially with global source cardinality",
    );
  }
  const heapGrowthCeiling = tenThousand.js_heap_used_bytes * 1.35 + 16 * MIB;
  if (hundredThousand.js_heap_used_bytes > heapGrowthCeiling) {
    throw new Error(
      "100k JS heap grew materially with offscreen global source cardinality",
    );
  }

  return {
    schema_version: 1,
    kind: "commonthing_map_cardinality_fullstack_evidence",
    source_revision: sourceRevision,
    generated_at: generatedAt,
    browser,
    backend,
    page_size: MAP_CARDINALITY_PAGE_SIZE,
    client_limits: {
      max_pages: MAP_CARDINALITY_CLIENT_MAX_PAGES,
      max_items: MAP_CARDINALITY_CLIENT_MAX_ITEMS,
    },
    budgets: MAP_CARDINALITY_FULLSTACK_BUDGETS,
    samples,
    verdict: "PASS",
    limitations: [
      "The proof uses the real Rust API and PostgreSQL BBOX/cursor path on loopback; it is a code-path and cardinality proof, not a production-network latency SLO.",
      "Deterministic production-shaped nodes are inserted directly into PostgreSQL after the API is already ready, proving viewport reads cannot depend on a startup-only node cache while intentionally excluding production ingestion cost.",
      "Chromium Runtime.getHeapUsage measures JavaScript heap after explicit garbage collection; it does not represent total browser, GPU, tile-cache, or operating-system memory.",
      "Frame-time evidence is requestAnimationFrame cadence on the pinned Chromium/runner class and acts as a regression budget rather than hardware-independent FPS telemetry.",
      "The proof replaces only the basemap style with a deterministic empty MapLibre style so missing PMTiles cannot gate the node data-path measurement; real basemap delivery is covered by separate basemap proofs.",
    ],
  };
}

export function writeMapCardinalityFullstackEvidence(
  evidence,
  outputPath = null,
) {
  const destination =
    outputPath ??
    resolve(
      process.cwd(),
      "../../build/proofs/web-runtime/map-cardinality-fullstack.json",
    );
  mkdirSync(resolve(destination, ".."), { recursive: true });
  writeFileSync(destination, `${JSON.stringify(evidence, null, 2)}\n`, "utf8");
  return destination;
}
