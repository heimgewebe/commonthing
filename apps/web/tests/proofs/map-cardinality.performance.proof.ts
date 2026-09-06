import { expect, test, type Page, type Route } from "@playwright/test";
import { performance } from "node:perf_hooks";
import { readFileSync } from "node:fs";
import { mockApiResponses } from "../fixtures/mockApi";
import {
  MAP_CURSOR_MAX_ITEMS,
  MAP_CURSOR_MAX_PAGES,
  MAP_CURSOR_PAGE_SIZE,
} from "../../src/lib/map/cursorPagination";
import {
  assertExactGitCheckout,
  resolveExactSourceRevision,
} from "../../scripts/web-runtime-evidence.mjs";
// The evidence builder stays Node-native JavaScript so its contract can be
// executed directly by node --test. Keep the typed browser seam explicit here.
// @ts-expect-error The sibling .mjs module is runtime-validated by its Node tests.
import * as mapCardinalityEvidenceRuntime from "../../scripts/map-cardinality-evidence.mjs";

const {
  MAP_CARDINALITY_BUDGETS,
  MAP_CARDINALITY_NATIVE_LAYER_MIN_COUNT,
  MAP_CARDINALITY_PAGE_SIZE,
  MAP_CARDINALITY_CLIENT_MAX_ITEMS,
  MAP_CARDINALITY_CLIENT_MAX_PAGES,
  expectedMapCardinalityPages,
  expectedMapCardinalityItems,
  buildMapCardinalityEvidence,
  writeMapCardinalityEvidence,
} = mapCardinalityEvidenceRuntime as unknown as {
  MAP_CARDINALITY_BUDGETS: Record<
    1000 | 10000 | 100000,
    {
      readiness_ms: number;
      interaction_to_next_paint_ms: number;
      max_dom_markers: number;
      max_api_response_bytes: number;
    }
  >;
  MAP_CARDINALITY_NATIVE_LAYER_MIN_COUNT: number;
  MAP_CARDINALITY_PAGE_SIZE: number;
  MAP_CARDINALITY_CLIENT_MAX_ITEMS: number;
  MAP_CARDINALITY_CLIENT_MAX_PAGES: number;
  expectedMapCardinalityPages: (cardinality: number) => number;
  expectedMapCardinalityItems: (cardinality: number) => number;
  buildMapCardinalityEvidence: (input: {
    sourceRevision: string;
    generatedAt: string;
    browser: { name: string; version: string; headless: boolean };
    samples: CardinalitySample[];
  }) => Record<string, unknown>;
  writeMapCardinalityEvidence: (evidence: Record<string, unknown>) => string;
};

type Cardinality = 1000 | 10000 | 100000;

type CardinalitySample = {
  cardinality: Cardinality;
  page_size: number;
  api_request_count: number;
  api_response_bytes: number;
  source_node_count: number;
  bbox_source_item_count: number;
  offscreen_source_item_count: number;
  loaded_item_count: number;
  truncated_by_client_limit: boolean;
  source_has_more_after_last_client_page: boolean;
  bbox_scoped_request_count: number;
  bulk_node_request_count: number;
  readiness_ms: number;
  interaction_to_next_paint_ms: number;
  dom_marker_count: number;
  native_layer_expected: boolean;
  native_layer_actual: boolean;
};

const CARDINALITIES: Cardinality[] = [1000, 10000, 100000];
const EMPTY_CURSOR_PAGE = {
  items: [],
  page: {
    limit: MAP_CARDINALITY_PAGE_SIZE,
    next_cursor: null,
    has_more: false,
  },
};

function roundMilliseconds(value: number): number {
  return Math.round(value * 1000) / 1000;
}

function benchmarkNode(index: number, visible: boolean) {
  const column = index % 50;
  const row = Math.floor(index / 50) % 50;
  return {
    id: `benchmark-node-${index.toString().padStart(6, "0")}`,
    kind: "Knoten",
    title: `Benchmark ${index}`,
    summary: "Deterministic map cardinality proof node",
    location: visible
      ? {
          lon: 10.4515 + column * 0.001,
          lat: 51.1657 + row * 0.001,
        }
      : {
          // Off-screen source population: real objects exist in the global
          // fixture but are far outside the Germany startup viewport.
          lon: -150 + (index % 100) * 0.001,
          lat: -20 + (Math.floor(index / 100) % 100) * 0.001,
        },
    created_at: "2026-09-04T00:00:00Z",
    updated_at: "2026-09-04T00:00:00Z",
    modules: [],
  };
}

type BenchmarkNode = ReturnType<typeof benchmarkNode>;

function benchmarkSource(cardinality: Cardinality): BenchmarkNode[] {
  const visibleItems = expectedMapCardinalityItems(cardinality);
  const source = Array.from({ length: cardinality }, (_, index) =>
    benchmarkNode(index, index < visibleItems),
  );
  expect(source).toHaveLength(cardinality);
  return source;
}

function nodesInsideBbox(
  source: BenchmarkNode[],
  bbox: string,
): BenchmarkNode[] {
  const coordinates = bbox.split(",").map(Number);
  if (
    coordinates.length !== 4 ||
    coordinates.some((value) => !Number.isFinite(value))
  ) {
    throw new Error(`invalid benchmark bbox ${bbox}`);
  }
  const [west, south, east, north] = coordinates;
  if (west > east || south > north)
    throw new Error(`invalid benchmark bbox ${bbox}`);
  return source.filter(
    ({ location }) =>
      location.lon >= west &&
      location.lon <= east &&
      location.lat >= south &&
      location.lat <= north,
  );
}

async function fulfillEmptyList(route: Route): Promise<void> {
  const url = new URL(route.request().url());
  const cursorMode =
    url.searchParams.get("pagination") === "cursor" ||
    url.searchParams.has("cursor");
  await route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(cursorMode ? EMPTY_CURSOR_PAGE : []),
  });
}

async function installCardinalityApi(page: Page, cardinality: Cardinality) {
  const source = benchmarkSource(cardinality);
  let apiRequestCount = 0;
  let apiResponseBytes = 0;
  const loadedItemIds = new Set<string>();
  const bboxSourceItemIds = new Set<string>();
  let lastResponseHasMore = false;
  let bboxScopedRequestCount = 0;
  let bulkNodeRequestCount = 0;

  await page.route("**/api/**", async (route) => {
    const request = route.request();
    if (request.method() !== "GET") {
      await route.fallback();
      return;
    }
    const url = new URL(request.url());
    if (url.pathname === "/api/nodes") {
      apiRequestCount += 1;
      const bbox = url.searchParams.get("bbox");
      const selectedSource =
        bbox === null ? source : nodesInsideBbox(source, bbox);
      if (bbox === null) {
        bulkNodeRequestCount += 1;
      } else {
        bboxScopedRequestCount += 1;
        for (const item of selectedSource) bboxSourceItemIds.add(item.id);
      }

      const requestedLimit = Number(
        url.searchParams.get("limit") ?? MAP_CARDINALITY_PAGE_SIZE,
      );
      const limit = Math.min(
        MAP_CARDINALITY_PAGE_SIZE,
        Number.isInteger(requestedLimit) && requestedLimit > 0
          ? requestedLimit
          : MAP_CARDINALITY_PAGE_SIZE,
      );
      const rawCursor = url.searchParams.get("cursor");
      const offset = rawCursor === null ? 0 : Number(rawCursor);
      if (
        !Number.isInteger(offset) ||
        offset < 0 ||
        offset > selectedSource.length
      ) {
        throw new Error(`invalid benchmark cursor ${rawCursor}`);
      }
      const end = Math.min(selectedSource.length, offset + limit);
      const items = selectedSource.slice(offset, end);
      const hasMore = end < selectedSource.length;
      const body = JSON.stringify({
        items,
        page: {
          limit,
          next_cursor: hasMore ? String(end) : null,
          has_more: hasMore,
        },
      });
      apiResponseBytes += Buffer.byteLength(body);
      for (const item of items) loadedItemIds.add(item.id);
      lastResponseHasMore = hasMore;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body,
      });
      return;
    }
    if (
      url.pathname === "/api/accounts" ||
      url.pathname === "/api/edges" ||
      url.pathname === "/api/webgemeindezentren"
    ) {
      await fulfillEmptyList(route);
      return;
    }
    await route.fallback();
  });

  return {
    snapshot: () => ({
      apiRequestCount,
      apiResponseBytes,
      sourceNodeCount: source.length,
      bboxSourceItemCount: bboxSourceItemIds.size,
      loadedItemCount: loadedItemIds.size,
      lastResponseHasMore,
      bboxScopedRequestCount,
      bulkNodeRequestCount,
    }),
  };
}

async function settleFrames(page: Page, frames: number): Promise<void> {
  await page.evaluate(
    (count) =>
      new Promise<void>((resolve) => {
        const next = (remaining: number): void => {
          if (remaining <= 0) {
            resolve();
            return;
          }
          requestAnimationFrame(() => next(remaining - 1));
        };
        next(count);
      }),
    frames,
  );
}

async function measureWheelToNextPaint(page: Page): Promise<number> {
  await page.evaluate(() => {
    const state = window as Window & {
      __mapCardinalityWheelMs?: number | null;
    };
    state.__mapCardinalityWheelMs = null;
    window.addEventListener(
      "wheel",
      () => {
        const startedAt = performance.now();
        requestAnimationFrame(() => {
          requestAnimationFrame(() => {
            state.__mapCardinalityWheelMs = performance.now() - startedAt;
          });
        });
      },
      { once: true, capture: true },
    );
  });
  const canvas = page.locator("canvas.maplibregl-canvas").first();
  const box = await canvas.boundingBox();
  if (!box) throw new Error("map canvas bounding box is unavailable");
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.wheel(0, -320);
  await page.waitForFunction(
    () => {
      const state = window as Window & {
        __mapCardinalityWheelMs?: number | null;
      };
      return typeof state.__mapCardinalityWheelMs === "number";
    },
    undefined,
    { timeout: 5000 },
  );
  return page.evaluate(() => {
    const value = (
      window as Window & { __mapCardinalityWheelMs?: number | null }
    ).__mapCardinalityWheelMs;
    if (typeof value !== "number")
      throw new Error("wheel paint sample missing");
    return value;
  });
}

test("keeps 1k/10k/100k map cardinalities inside fixed browser budgets", async ({
  browser,
}, testInfo) => {
  const sourceRevision = resolveExactSourceRevision();
  assertExactGitCheckout({ revision: sourceRevision });
  expect(MAP_CARDINALITY_PAGE_SIZE).toBe(MAP_CURSOR_PAGE_SIZE);
  const overlaySource = readFileSync(
    new URL("../../src/lib/map/overlay/nodes.ts", import.meta.url),
    "utf8",
  );
  expect(overlaySource).toContain(
    `export const NATIVE_ENTITY_LAYER_MIN_COUNT = ${MAP_CARDINALITY_NATIVE_LAYER_MIN_COUNT};`,
  );
  expect(MAP_CARDINALITY_CLIENT_MAX_PAGES).toBe(MAP_CURSOR_MAX_PAGES);
  expect(MAP_CARDINALITY_CLIENT_MAX_ITEMS).toBe(MAP_CURSOR_MAX_ITEMS);
  const samples: CardinalitySample[] = [];

  for (const cardinality of CARDINALITIES) {
    const budget = MAP_CARDINALITY_BUDGETS[cardinality];
    const context = await browser.newContext({
      viewport: { width: 1440, height: 900 },
      serviceWorkers: "block",
    });
    try {
      const page = await context.newPage();
      await mockApiResponses(page);
      const api = await installCardinalityApi(page, cardinality);
      const startedAt = performance.now();
      await page.goto("/map", { waitUntil: "domcontentloaded" });
      await page.locator("canvas.maplibregl-canvas").first().waitFor({
        state: "visible",
        timeout: budget.readiness_ms,
      });
      const expectedPages = expectedMapCardinalityPages(cardinality);
      const expectedItems = expectedMapCardinalityItems(cardinality);
      await expect
        .poll(() => api.snapshot().loadedItemCount, {
          timeout: budget.readiness_ms,
          message: `${cardinality}-node source did not settle at ${expectedItems} unique viewport nodes`,
        })
        .toBe(expectedItems);
      await settleFrames(page, 8);
      const initialSnapshot = api.snapshot();
      expect(initialSnapshot.sourceNodeCount).toBe(cardinality);
      expect(initialSnapshot.bboxSourceItemCount).toBe(expectedItems);
      expect(initialSnapshot.apiRequestCount).toBeGreaterThanOrEqual(
        expectedPages,
      );
      expect(initialSnapshot.bulkNodeRequestCount).toBe(0);
      expect(initialSnapshot.bboxScopedRequestCount).toBe(
        initialSnapshot.apiRequestCount,
      );
      expect(initialSnapshot.lastResponseHasMore).toBe(false);
      const readinessMs = performance.now() - startedAt;
      const domMarkerCount = await page.locator(".map-marker").count();
      if (
        initialSnapshot.loadedItemCount <=
        MAP_CARDINALITY_NATIVE_LAYER_MIN_COUNT
      ) {
        expect(domMarkerCount).toBeGreaterThan(0);
      }
      const nativeLayerExpected =
        initialSnapshot.loadedItemCount >
        MAP_CARDINALITY_NATIVE_LAYER_MIN_COUNT;
      const nativeLayerActual = await page.evaluate(() =>
        Boolean(
          (
            window as Window & {
              __TEST_MAP__?: { getLayer: (id: string) => unknown };
            }
          ).__TEST_MAP__?.getLayer("commonthing-map-entities-body"),
        ),
      );
      expect(nativeLayerActual).toBe(nativeLayerExpected);

      const interactionMs = await measureWheelToNextPaint(page);
      await page.waitForFunction(
        () => {
          const map = (
            window as Window & { __TEST_MAP__?: { isMoving: () => boolean } }
          ).__TEST_MAP__;
          return Boolean(map && !map.isMoving());
        },
        undefined,
        { timeout: 5000 },
      );
      await settleFrames(page, 4);
      const finalSnapshot = api.snapshot();
      expect(finalSnapshot.bulkNodeRequestCount).toBe(0);
      expect(finalSnapshot.bboxScopedRequestCount).toBe(
        finalSnapshot.apiRequestCount,
      );
      expect(finalSnapshot.sourceNodeCount).toBe(cardinality);
      expect(finalSnapshot.bboxSourceItemCount).toBe(expectedItems);
      expect(finalSnapshot.loadedItemCount).toBe(expectedItems);
      expect(finalSnapshot.lastResponseHasMore).toBe(false);

      samples.push({
        cardinality,
        page_size: MAP_CARDINALITY_PAGE_SIZE,
        api_request_count: finalSnapshot.apiRequestCount,
        api_response_bytes: finalSnapshot.apiResponseBytes,
        source_node_count: finalSnapshot.sourceNodeCount,
        bbox_source_item_count: finalSnapshot.bboxSourceItemCount,
        offscreen_source_item_count:
          finalSnapshot.sourceNodeCount - finalSnapshot.bboxSourceItemCount,
        loaded_item_count: finalSnapshot.loadedItemCount,
        truncated_by_client_limit: false,
        source_has_more_after_last_client_page:
          finalSnapshot.lastResponseHasMore,
        bbox_scoped_request_count: finalSnapshot.bboxScopedRequestCount,
        bulk_node_request_count: finalSnapshot.bulkNodeRequestCount,
        readiness_ms: roundMilliseconds(readinessMs),
        interaction_to_next_paint_ms: roundMilliseconds(interactionMs),
        dom_marker_count: domMarkerCount,
        native_layer_expected: nativeLayerExpected,
        native_layer_actual: nativeLayerActual,
      });

      if (cardinality === 1000 && nativeLayerExpected) {
        await page.getByRole("button", { name: "Werkzeuge öffnen" }).click();
        await page.getByTestId("tool-fan-find").click();
        const dialog = page.getByRole("dialog", { name: "Finden" });
        const combobox = page.getByRole("combobox", { name: "Suchbegriff" });
        const listbox = page.getByRole("listbox", {
          name: "Kartenobjekte im Ausschnitt",
        });
        await expect(dialog).toBeVisible();
        await expect(combobox).toBeFocused();
        await expect(listbox).toBeVisible();
        await expect(listbox.getByRole("option")).toHaveCount(50);
        await expect(
          page.getByText("1–50 von 250 Kartenobjekten im Ausschnitt"),
        ).toBeVisible();
        await combobox.press("ArrowDown");
        await combobox.press("Enter");
        await expect(dialog).toBeHidden();
        await expect(
          page.locator('.map-marker[data-selected="true"]'),
        ).toHaveCount(1);
      }
    } finally {
      await context.close();
    }
  }

  const evidence = buildMapCardinalityEvidence({
    sourceRevision,
    generatedAt: new Date().toISOString(),
    browser: { name: "chromium", version: browser.version(), headless: true },
    samples,
  });
  const evidencePath = writeMapCardinalityEvidence(evidence);
  await testInfo.attach("map-cardinality-evidence", {
    body: readFileSync(evidencePath),
    contentType: "application/json",
  });
});
