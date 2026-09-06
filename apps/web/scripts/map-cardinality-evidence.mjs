import { mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

export const MAP_CARDINALITY_PAGE_SIZE = 1000;
export const MAP_CARDINALITY_NATIVE_LAYER_MIN_COUNT = 100;
export const MAP_CARDINALITY_CLIENT_MAX_PAGES = 10;
export const MAP_CARDINALITY_CLIENT_MAX_ITEMS = 10_000;
export const MAP_CARDINALITY_VIEWPORT_ITEMS = Object.freeze({
  1000: 250,
  10000: 1500,
  100000: 1500,
});

export const MAP_CARDINALITY_BUDGETS = Object.freeze({
  1000: Object.freeze({
    readiness_ms: 5000,
    interaction_to_next_paint_ms: 200,
    max_dom_markers: 1,
    max_api_response_bytes: 500_000,
  }),
  10000: Object.freeze({
    readiness_ms: 9000,
    interaction_to_next_paint_ms: 200,
    max_dom_markers: 1,
    max_api_response_bytes: 2_000_000,
  }),
  100000: Object.freeze({
    readiness_ms: 12000,
    interaction_to_next_paint_ms: 250,
    max_dom_markers: 1,
    max_api_response_bytes: 2_000_000,
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

export function expectedMapCardinalityPages(cardinality) {
  integer(cardinality, "cardinality", 1);
  const viewportItems = MAP_CARDINALITY_VIEWPORT_ITEMS[cardinality];
  if (!viewportItems) throw new Error(`unsupported cardinality ${cardinality}`);
  return Math.ceil(viewportItems / MAP_CARDINALITY_PAGE_SIZE);
}

export function expectedMapCardinalityItems(cardinality) {
  integer(cardinality, "cardinality", 1);
  const viewportItems = MAP_CARDINALITY_VIEWPORT_ITEMS[cardinality];
  if (!viewportItems) throw new Error(`unsupported cardinality ${cardinality}`);
  return viewportItems;
}

export function validateMapCardinalitySample(sample) {
  if (!sample || typeof sample !== "object" || Array.isArray(sample)) {
    throw new Error("sample must be an object");
  }
  const cardinality = integer(sample.cardinality, "sample.cardinality", 1);
  const budget = MAP_CARDINALITY_BUDGETS[cardinality];
  if (!budget) throw new Error(`unsupported cardinality ${cardinality}`);

  integer(sample.api_request_count, "sample.api_request_count", 1);
  integer(sample.api_response_bytes, "sample.api_response_bytes", 1);
  integer(sample.source_node_count, "sample.source_node_count", 1);
  integer(sample.bbox_source_item_count, "sample.bbox_source_item_count", 1);
  integer(
    sample.offscreen_source_item_count,
    "sample.offscreen_source_item_count",
    0,
  );
  integer(sample.loaded_item_count, "sample.loaded_item_count", 1);
  integer(sample.dom_marker_count, "sample.dom_marker_count", 0);
  integer(sample.page_size, "sample.page_size", 1);
  integer(
    sample.bbox_scoped_request_count,
    "sample.bbox_scoped_request_count",
    1,
  );
  integer(sample.bulk_node_request_count, "sample.bulk_node_request_count", 0);
  finite(sample.readiness_ms, "sample.readiness_ms");
  finite(
    sample.interaction_to_next_paint_ms,
    "sample.interaction_to_next_paint_ms",
  );
  if (sample.page_size !== MAP_CARDINALITY_PAGE_SIZE) {
    throw new Error(
      `page size ${sample.page_size} does not match ${MAP_CARDINALITY_PAGE_SIZE}`,
    );
  }
  if (sample.source_node_count !== cardinality) {
    throw new Error(
      `cardinality ${cardinality}: fixture source contains ${sample.source_node_count} nodes`,
    );
  }
  const expectedPages = expectedMapCardinalityPages(cardinality);
  if (sample.api_request_count < expectedPages) {
    throw new Error(
      `cardinality ${cardinality}: expected at least ${expectedPages} consumed node pages, observed ${sample.api_request_count}`,
    );
  }
  const expectedItems = expectedMapCardinalityItems(cardinality);
  if (sample.bbox_source_item_count !== expectedItems) {
    throw new Error(
      `cardinality ${cardinality}: bbox filtered ${sample.bbox_source_item_count} source items, expected ${expectedItems}`,
    );
  }
  if (sample.offscreen_source_item_count !== cardinality - expectedItems) {
    throw new Error(
      `cardinality ${cardinality}: offscreen source count does not reconcile with the global source`,
    );
  }
  if (sample.loaded_item_count !== expectedItems) {
    throw new Error(
      `cardinality ${cardinality}: expected ${expectedItems} loaded items, observed ${sample.loaded_item_count}`,
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
  if (sample.bbox_scoped_request_count !== sample.api_request_count) {
    throw new Error(
      `cardinality ${cardinality}: every node request must be bbox scoped`,
    );
  }
  if (sample.bulk_node_request_count !== 0) {
    throw new Error(
      `cardinality ${cardinality}: bulk node bootstrap request observed`,
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
  const nativeExpected = expectedItems > MAP_CARDINALITY_NATIVE_LAYER_MIN_COUNT;
  if (sample.native_layer_expected !== nativeExpected) {
    throw new Error(
      `cardinality ${cardinality}: native layer expectation is inconsistent`,
    );
  }
  if (typeof sample.native_layer_actual !== "boolean") {
    throw new Error(
      `cardinality ${cardinality}: native layer actual state must be boolean`,
    );
  }
  if (sample.native_layer_actual !== nativeExpected) {
    throw new Error(
      `cardinality ${cardinality}: native layer actual state does not match expectation`,
    );
  }
  return { cardinality, budget };
}

export function buildMapCardinalityEvidence({
  sourceRevision,
  browser,
  generatedAt,
  samples,
}) {
  if (
    typeof sourceRevision !== "string" ||
    !/^[0-9a-f]{40}$/.test(sourceRevision)
  ) {
    throw new Error("sourceRevision must be a 40-character Git SHA");
  }
  if (!Array.isArray(samples) || samples.length !== 3) {
    throw new Error("samples must contain exactly the 1k/10k/100k scenarios");
  }
  const seen = samples.map(
    (sample) => validateMapCardinalitySample(sample).cardinality,
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
      "100k fixture must contain ten times the 10k global source",
    );
  }
  if (
    hundredThousand.bbox_source_item_count !==
    tenThousand.bbox_source_item_count
  ) {
    throw new Error(
      "10k and 100k fixtures must expose the same visible bbox population",
    );
  }
  if (hundredThousand.loaded_item_count !== tenThousand.loaded_item_count) {
    throw new Error("10k and 100k browser loads must stay viewport-bounded");
  }
  if (
    hundredThousand.api_response_bytes >
    tenThousand.api_response_bytes * 1.1
  ) {
    throw new Error(
      "100k bbox transfer grew materially with global source cardinality",
    );
  }
  return {
    schema_version: 1,
    kind: "commonthing_map_cardinality_browser_evidence",
    source_revision: sourceRevision,
    generated_at: generatedAt,
    browser,
    page_size: MAP_CARDINALITY_PAGE_SIZE,
    native_layer_min_count: MAP_CARDINALITY_NATIVE_LAYER_MIN_COUNT,
    client_limits: {
      max_pages: MAP_CARDINALITY_CLIENT_MAX_PAGES,
      max_items: MAP_CARDINALITY_CLIENT_MAX_ITEMS,
    },
    budgets: MAP_CARDINALITY_BUDGETS,
    expected_viewport_items: MAP_CARDINALITY_VIEWPORT_ITEMS,
    samples,
    verdict: "PASS",
    limitations: [
      "Node payloads are served by a deterministic Playwright HTTP fixture so this proof isolates viewport-scoped browser pagination and rendering from backend/database capacity.",
      "The fixture materializes the full 1k/10k/100k global node populations, filters that real source by every requested bbox, and exposes only the deterministic visible subset; the 10k and 100k scenarios intentionally share the same visible geometry so global growth cannot hide inside a changed viewport.",
      "Backend and PostgreSQL scale remain separate evidence lanes; this proof does not claim production network latency or database throughput.",
      "Timing budgets are regression guards on the pinned CI runner class, not user-facing service-level objectives.",
    ],
  };
}

export function writeMapCardinalityEvidence(evidence, outputPath = null) {
  const destination =
    outputPath ??
    resolve(
      process.cwd(),
      "../../build/proofs/web-runtime/map-cardinality.json",
    );
  mkdirSync(resolve(destination, ".."), { recursive: true });
  writeFileSync(destination, `${JSON.stringify(evidence, null, 2)}\n`, "utf8");
  return destination;
}
