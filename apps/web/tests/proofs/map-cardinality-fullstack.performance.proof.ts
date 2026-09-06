import { expect, test, type BrowserContext, type Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { performance } from "node:perf_hooks";
import {
  MAP_CURSOR_MAX_ITEMS,
  MAP_CURSOR_MAX_PAGES,
  MAP_CURSOR_PAGE_SIZE,
} from "../../src/lib/map/cursorPagination";
import {
  assertExactGitCheckout,
  resolveExactSourceRevision,
} from "../../scripts/web-runtime-evidence.mjs";
// Runtime contract is covered by the sibling Node tests.
// @ts-expect-error Node-native evidence module intentionally stays JavaScript.
import * as fullstackEvidenceRuntime from "../../scripts/map-cardinality-fullstack-evidence.mjs";
// @ts-expect-error Node-native evidence module intentionally stays JavaScript.
import * as cardinalityEvidenceRuntime from "../../scripts/map-cardinality-evidence.mjs";

const {
  MAP_CARDINALITY_FULLSTACK_BUDGETS,
  MAP_CARDINALITY_FULLSTACK_INTERACTION_SAMPLE_COUNT,
  MAP_CARDINALITY_FULLSTACK_FRAME_SAMPLES_PER_INTERACTION,
  MAP_CARDINALITY_FULLSTACK_FRAME_SAMPLE_COUNT,
  buildMapCardinalityFullstackEvidence,
  writeMapCardinalityFullstackEvidence,
} = fullstackEvidenceRuntime as unknown as {
  MAP_CARDINALITY_FULLSTACK_INTERACTION_SAMPLE_COUNT: number;
  MAP_CARDINALITY_FULLSTACK_FRAME_SAMPLES_PER_INTERACTION: number;
  MAP_CARDINALITY_FULLSTACK_FRAME_SAMPLE_COUNT: number;
  MAP_CARDINALITY_FULLSTACK_BUDGETS: Record<
    Cardinality,
    {
      readiness_ms: number;
      interaction_to_next_paint_ms: number;
      api_response_p95_ms: number;
      frame_time_p95_ms: number;
      max_js_heap_used_bytes: number;
      max_dom_markers: number;
      max_api_response_bytes: number;
    }
  >;
  buildMapCardinalityFullstackEvidence: (input: {
    sourceRevision: string;
    generatedAt: string;
    browser: { name: string; version: string; headless: boolean };
    backend: {
      api_build_commit: string;
      database_kind: "postgresql";
      startup_node_count: number;
      seeded_after_api_start: true;
    };
    samples: CardinalitySample[];
  }) => Record<string, unknown>;
  writeMapCardinalityFullstackEvidence: (
    evidence: Record<string, unknown>,
  ) => string;
};

const {
  MAP_CARDINALITY_NATIVE_LAYER_MIN_COUNT,
  MAP_CARDINALITY_PAGE_SIZE,
  MAP_CARDINALITY_CLIENT_MAX_ITEMS,
  MAP_CARDINALITY_CLIENT_MAX_PAGES,
  expectedMapCardinalityItems,
  expectedMapCardinalityPages,
} = cardinalityEvidenceRuntime as unknown as {
  MAP_CARDINALITY_NATIVE_LAYER_MIN_COUNT: number;
  MAP_CARDINALITY_PAGE_SIZE: number;
  MAP_CARDINALITY_CLIENT_MAX_ITEMS: number;
  MAP_CARDINALITY_CLIENT_MAX_PAGES: number;
  expectedMapCardinalityItems: (cardinality: number) => number;
  expectedMapCardinalityPages: (cardinality: number) => number;
};

type Cardinality = 1000 | 10000 | 100000;

type CardinalitySample = {
  cardinality: Cardinality;
  page_size: number;
  api_request_count: number;
  api_response_bytes: number;
  api_error_count: number;
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
  interaction_sample_count: number;
  api_response_p95_ms: number;
  frame_time_p95_ms: number;
  frame_time_max_ms: number;
  frame_time_sample_count: number;
  js_heap_used_bytes: number;
  dom_marker_count: number;
  native_layer_expected: boolean;
  native_layer_actual: boolean;
  seeded_after_api_start: true;
};

const CARDINALITIES: Cardinality[] = [1000, 10000, 100000];
const PROOF_DATABASE_NAME = "commonthing_map_cardinality_fullstack";
const PROOF_DATABASE_GUARD = "commonthing-map-cardinality-fullstack-v1";
const DATABASE_URL = process.env.MAP_CARDINALITY_FULLSTACK_DATABASE_URL ?? "";
const API_BASE = process.env.MAP_CARDINALITY_FULLSTACK_API_BASE ?? "";

function requireFullstackEnvironment(): void {
  let database: URL;
  try {
    database = new URL(DATABASE_URL);
  } catch {
    throw new Error(
      "MAP_CARDINALITY_FULLSTACK_DATABASE_URL must be an explicit PostgreSQL URL",
    );
  }
  if (
    (database.protocol !== "postgres:" &&
      database.protocol !== "postgresql:") ||
    database.hostname !== "127.0.0.1" ||
    database.port !== "5432" ||
    database.pathname !== `/${PROOF_DATABASE_NAME}` ||
    database.search !== "" ||
    database.hash !== ""
  ) {
    throw new Error(
      `MAP_CARDINALITY_FULLSTACK_DATABASE_URL must point directly at loopback database ${PROOF_DATABASE_NAME} on port 5432 without connection overrides`,
    );
  }

  let api: URL;
  try {
    api = new URL(API_BASE);
  } catch {
    throw new Error(
      "MAP_CARDINALITY_FULLSTACK_API_BASE must be an explicit loopback HTTP API origin",
    );
  }
  if (
    api.protocol !== "http:" ||
    api.hostname !== "127.0.0.1" ||
    api.pathname !== "/" ||
    api.search !== "" ||
    api.hash !== ""
  ) {
    throw new Error(
      "MAP_CARDINALITY_FULLSTACK_API_BASE must be an explicit loopback HTTP API origin",
    );
  }
}

function psql(sql: string, tuplesOnly = false): string {
  const args = [
    "--no-psqlrc",
    `--dbname=${DATABASE_URL}`,
    "--set=ON_ERROR_STOP=1",
    "--quiet",
  ];
  if (tuplesOnly) args.push("--tuples-only", "--no-align");
  args.push("--command", sql);
  return execFileSync("psql", args, {
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  }).trim();
}

function requireProofDatabaseGuard(): void {
  const marker = psql(
    "SELECT marker FROM map_cardinality_fullstack_proof_guard WHERE id = 1;",
    true,
  );
  if (marker !== PROOF_DATABASE_GUARD) {
    throw new Error(
      "refusing destructive cardinality fixture setup without the exact proof-database guard",
    );
  }
}

function databaseNodeCount(): number {
  const raw = psql("SELECT count(*) FROM domain_nodes;", true);
  const value = Number(raw);
  if (!Number.isInteger(value) || value < 0) {
    throw new Error(`invalid domain_nodes count from PostgreSQL: ${raw}`);
  }
  return value;
}

function seedPostgresNodes(cardinality: Cardinality): void {
  requireProofDatabaseGuard();
  const visibleItems = expectedMapCardinalityItems(cardinality);
  if (!Number.isInteger(visibleItems) || visibleItems <= 0) {
    throw new Error(`invalid visible item count for ${cardinality}`);
  }
  psql(`
    BEGIN;
    TRUNCATE TABLE domain_nodes CASCADE;
    INSERT INTO domain_nodes (
      id, kind, title, lat, lon, created_at, updated_at, payload, search_visibility
    )
    SELECT
      'benchmark-node-' || lpad((g - 1)::text, 6, '0'),
      'Knoten',
      'Benchmark ' || (g - 1)::text,
      CASE
        WHEN g <= ${visibleItems}
          THEN 51.1657 + ((((g - 1) / 50) % 50)::double precision * 0.001)
        ELSE -20 + ((((g - 1) / 100) % 100)::double precision * 0.001)
      END,
      CASE
        WHEN g <= ${visibleItems}
          THEN 10.4515 + (((g - 1) % 50)::double precision * 0.001)
        ELSE -150 + (((g - 1) % 100)::double precision * 0.001)
      END,
      TIMESTAMPTZ '2026-09-05T00:00:00Z',
      TIMESTAMPTZ '2026-09-05T00:00:00Z',
      jsonb_build_object('summary', 'Deterministic real PostgreSQL cardinality proof node'),
      'public'
    FROM generate_series(1, ${cardinality}) AS series(g);
    COMMIT;
    ANALYZE domain_nodes;
  `);
}

function roundMilliseconds(value: number): number {
  return Math.round(value * 1000) / 1000;
}

function percentile(values: number[], quantile: number): number {
  if (values.length === 0)
    throw new Error("cannot compute percentile of empty sample");
  const sorted = [...values].sort((a, b) => a - b);
  const index = Math.max(0, Math.ceil(sorted.length * quantile) - 1);
  return sorted[index];
}

async function installDeterministicBasemapStyle(page: Page): Promise<void> {
  const emptyStyleBody = JSON.stringify({
    version: 8,
    sources: {},
    layers: [],
  });
  for (const pattern of [
    "**/local-basemap/style.json*",
    "**/local-basemap/style-dark.json*",
    "**/local-basemap/style-germany.json*",
    "**/local-basemap/style-germany-dark.json*",
  ]) {
    await page.route(pattern, (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: emptyStyleBody,
      }),
    );
  }
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

async function measureWheelInteraction(
  page: Page,
  deltaY: number,
  frameCount: number,
): Promise<{ interactionMs: number; frameDeltas: number[] }> {
  await page.evaluate((count) => {
    const state = window as Window & {
      __mapCardinalityFullstackInteraction?: {
        nextPaintMs: number | null;
        frameDeltas: number[];
        completion: Promise<void>;
      };
    };
    let resolveCompletion: (() => void) | null = null;
    const completion = new Promise<void>((resolve) => {
      resolveCompletion = resolve;
    });
    state.__mapCardinalityFullstackInteraction = {
      nextPaintMs: null,
      frameDeltas: [],
      completion,
    };
    window.addEventListener(
      "wheel",
      () => {
        const startedAt = performance.now();
        const sample = state.__mapCardinalityFullstackInteraction;
        if (!sample) return;
        const maybeResolve = (): void => {
          if (
            typeof sample.nextPaintMs === "number" &&
            sample.frameDeltas.length >= count
          ) {
            resolveCompletion?.();
          }
        };

        requestAnimationFrame(() => {
          requestAnimationFrame(() => {
            sample.nextPaintMs = performance.now() - startedAt;
            maybeResolve();
          });
        });

        let previous: number | null = null;
        const next = (timestamp: number): void => {
          if (previous !== null) sample.frameDeltas.push(timestamp - previous);
          previous = timestamp;
          if (sample.frameDeltas.length < count) requestAnimationFrame(next);
          else maybeResolve();
        };
        requestAnimationFrame(next);
      },
      { once: true, capture: true },
    );
  }, frameCount);

  const canvas = page.locator("canvas.maplibregl-canvas").first();
  const box = await canvas.boundingBox();
  if (!box) throw new Error("map canvas bounding box is unavailable");
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.wheel(0, deltaY);
  return page.evaluate(async () => {
    const sample = (
      window as Window & {
        __mapCardinalityFullstackInteraction?: {
          nextPaintMs: number | null;
          frameDeltas: number[];
          completion: Promise<void>;
        };
      }
    ).__mapCardinalityFullstackInteraction;
    if (!sample) throw new Error("wheel interaction sample missing");
    await new Promise<void>((resolve, reject) => {
      const timeout = setTimeout(
        () => reject(new Error("wheel interaction sample timed out")),
        10_000,
      );
      sample.completion.then(
        () => {
          clearTimeout(timeout);
          resolve();
        },
        (error) => {
          clearTimeout(timeout);
          reject(error);
        },
      );
    });
    if (typeof sample.nextPaintMs !== "number") {
      throw new Error("wheel interaction sample missing");
    }
    return {
      interactionMs: sample.nextPaintMs,
      frameDeltas: sample.frameDeltas,
    };
  });
}

async function measureJsHeap(
  context: BrowserContext,
  page: Page,
): Promise<number> {
  const session = await context.newCDPSession(page);
  try {
    await session.send("HeapProfiler.collectGarbage");
    const result = (await session.send("Runtime.getHeapUsage")) as {
      usedSize?: number;
    };
    if (!Number.isFinite(result.usedSize) || (result.usedSize ?? 0) <= 0) {
      throw new Error(
        "Chromium Runtime.getHeapUsage returned no positive finite usedSize",
      );
    }
    return result.usedSize as number;
  } finally {
    await session.detach();
  }
}

function observeRealNodeApi(page: Page) {
  const deferredEvidence: Array<() => Promise<void>> = [];
  let processedEvidenceCount = 0;
  const loadedIds = new Set<string>();
  const bboxIds = new Set<string>();
  const apiDurations: number[] = [];
  let apiRequestCount = 0;
  let apiResponseBytes = 0;
  let apiErrorCount = 0;
  let bboxScopedRequestCount = 0;
  let bulkNodeRequestCount = 0;
  let lastResponseHasMore = false;

  page.on("response", (response) => {
    let url: URL;
    try {
      url = new URL(response.url());
    } catch {
      return;
    }
    if (url.pathname !== "/api/nodes") return;

    // Keep the timed browser workload observer-free: recording the response
    // handle is cheap, while body transfer, JSON parsing and ID iteration are
    // deferred until flush() runs outside the RAF measurement window.
    deferredEvidence.push(async () => {
      apiRequestCount += 1;
      if (url.searchParams.has("bbox")) bboxScopedRequestCount += 1;
      else bulkNodeRequestCount += 1;
      if (!response.ok()) apiErrorCount += 1;

      const body = await response.body();
      apiResponseBytes += body.byteLength;
      const timing = response.request().timing();
      if (Number.isFinite(timing.responseEnd) && timing.responseEnd > 0) {
        apiDurations.push(timing.responseEnd);
      }

      const parsed = JSON.parse(body.toString("utf8")) as
        | Array<{ id?: unknown }>
        | {
            items?: Array<{ id?: unknown }>;
            page?: { has_more?: unknown };
          };
      const items = Array.isArray(parsed) ? parsed : (parsed.items ?? []);
      for (const item of items) {
        if (typeof item.id === "string") {
          loadedIds.add(item.id);
          if (url.searchParams.has("bbox")) bboxIds.add(item.id);
        }
      }
      lastResponseHasMore =
        !Array.isArray(parsed) && parsed.page?.has_more === true;
    });
  });

  return {
    async flush() {
      while (processedEvidenceCount < deferredEvidence.length) {
        const batchEnd = deferredEvidence.length;
        await Promise.all(
          deferredEvidence
            .slice(processedEvidenceCount, batchEnd)
            .map((collect) => collect()),
        );
        processedEvidenceCount = batchEnd;
        await page.waitForTimeout(0);
      }
    },
    snapshot() {
      return {
        apiRequestCount,
        apiResponseBytes,
        apiErrorCount,
        apiTimingSampleCount: apiDurations.length,
        apiResponseP95Ms:
          apiDurations.length > 0 ? percentile(apiDurations, 0.95) : 0,
        loadedItemCount: loadedIds.size,
        bboxSourceItemCount: bboxIds.size,
        bboxScopedRequestCount,
        bulkNodeRequestCount,
        lastResponseHasMore,
      };
    },
  };
}

async function verifyApiRevision(sourceRevision: string): Promise<string> {
  const response = await fetch(`${API_BASE}/version`);
  if (!response.ok) {
    throw new Error(`real API /version returned HTTP ${response.status}`);
  }
  const headerCommit = response.headers.get("x-weltgewebe-api-build");
  const body = (await response.json()) as { commit?: unknown };
  expect(headerCommit).toBe(sourceRevision);
  expect(body.commit).toBe(sourceRevision);
  return sourceRevision;
}

test("keeps real PostgreSQL → API BBOX → Chromium at 1k/10k/100k inside fixed budgets", async ({
  browser,
}, testInfo) => {
  requireFullstackEnvironment();
  requireProofDatabaseGuard();
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

  const startupNodeCount = databaseNodeCount();
  expect(startupNodeCount).toBe(0);
  const apiBuildCommit = await verifyApiRevision(sourceRevision);
  const samples: CardinalitySample[] = [];

  for (const cardinality of CARDINALITIES) {
    const budget = MAP_CARDINALITY_FULLSTACK_BUDGETS[cardinality];
    const expectedPages = expectedMapCardinalityPages(cardinality);
    const expectedItems = expectedMapCardinalityItems(cardinality);

    // Deliberately seed only after the API is already ready. The subsequent
    // BBOX result therefore proves a live PostgreSQL read rather than a
    // startup-populated node cache.
    seedPostgresNodes(cardinality);
    expect(databaseNodeCount()).toBe(cardinality);

    const context = await browser.newContext({
      viewport: { width: 1440, height: 900 },
      serviceWorkers: "block",
    });
    try {
      const page = await context.newPage();
      await installDeterministicBasemapStyle(page);
      const api = observeRealNodeApi(page);
      const startedAt = performance.now();
      await page.goto("/map", { waitUntil: "domcontentloaded" });
      await page.locator("canvas.maplibregl-canvas").first().waitFor({
        state: "visible",
        timeout: budget.readiness_ms,
      });
      await expect
        .poll(
          async () => {
            await api.flush();
            return api.snapshot().loadedItemCount;
          },
          {
            timeout: budget.readiness_ms,
            message: `${cardinality}-node real API source did not settle at ${expectedItems} unique viewport nodes`,
          },
        )
        .toBe(expectedItems);
      await settleFrames(page, 8);
      await api.flush();
      const initialSnapshot = api.snapshot();
      expect(initialSnapshot.apiRequestCount).toBeGreaterThanOrEqual(
        expectedPages,
      );
      expect(initialSnapshot.apiErrorCount).toBe(0);
      expect(initialSnapshot.apiTimingSampleCount).toBe(
        initialSnapshot.apiRequestCount,
      );
      expect(initialSnapshot.bulkNodeRequestCount).toBe(0);
      expect(initialSnapshot.bboxScopedRequestCount).toBe(
        initialSnapshot.apiRequestCount,
      );
      expect(initialSnapshot.bboxSourceItemCount).toBe(expectedItems);
      expect(initialSnapshot.lastResponseHasMore).toBe(false);

      const readinessMs = performance.now() - startedAt;
      const domMarkerCount = await page.locator(".map-marker").count();
      const nativeLayerExpected =
        expectedItems > MAP_CARDINALITY_NATIVE_LAYER_MIN_COUNT;
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

      const interactionSamples: number[] = [];
      const frameDeltas: number[] = [];
      const rawInteractionFrames: Array<{
        index: number;
        delta_y: number;
        interaction_to_next_paint_ms: number;
        frame_deltas_ms: number[];
      }> = [];
      for (
        let index = 0;
        index < MAP_CARDINALITY_FULLSTACK_INTERACTION_SAMPLE_COUNT;
        index += 1
      ) {
        const deltaY = index % 2 === 0 ? -320 : 320;
        const interaction = await measureWheelInteraction(
          page,
          deltaY,
          MAP_CARDINALITY_FULLSTACK_FRAME_SAMPLES_PER_INTERACTION,
        );
        interactionSamples.push(interaction.interactionMs);
        frameDeltas.push(...interaction.frameDeltas);
        rawInteractionFrames.push({
          index,
          delta_y: deltaY,
          interaction_to_next_paint_ms: interaction.interactionMs,
          frame_deltas_ms: [...interaction.frameDeltas],
        });
        await page.waitForFunction(
          () => {
            const map = (
              window as Window & { __TEST_MAP__?: { isMoving: () => boolean } }
            ).__TEST_MAP__;
            return Boolean(map && !map.isMoving());
          },
          undefined,
          { timeout: 10_000 },
        );
        await settleFrames(page, 2);
      }
      expect(interactionSamples).toHaveLength(
        MAP_CARDINALITY_FULLSTACK_INTERACTION_SAMPLE_COUNT,
      );
      expect(frameDeltas).toHaveLength(
        MAP_CARDINALITY_FULLSTACK_FRAME_SAMPLE_COUNT,
      );
      const interactionMs = Math.max(...interactionSamples);
      const frameCadence = {
        p95: percentile(frameDeltas, 0.95),
        max: Math.max(...frameDeltas),
      };
      // Attach the complete, unfiltered timed vectors before any budget
      // validation can fail. This replaces heavyweight Playwright tracing as
      // the benchmark diagnostic and makes a red p95 reproducible without
      // changing which frames count. Attachment happens after measurement.
      await testInfo.attach(`map-cardinality-fullstack-frames-${cardinality}`, {
        body: Buffer.from(
          JSON.stringify(
            {
              schema_version: 1,
              source_revision: sourceRevision,
              cardinality,
              trace_recording: "off",
              interaction_sample_count: interactionSamples.length,
              frame_sample_count: frameDeltas.length,
              frame_time_p95_ms: frameCadence.p95,
              frame_time_max_ms: frameCadence.max,
              interactions: rawInteractionFrames,
            },
            null,
            2,
          ),
        ),
        contentType: "application/json",
      });
      await settleFrames(page, 4);
      const jsHeapUsedBytes = await measureJsHeap(context, page);
      await api.flush();
      const finalSnapshot = api.snapshot();

      expect(finalSnapshot.apiErrorCount).toBe(0);
      expect(finalSnapshot.apiTimingSampleCount).toBe(
        finalSnapshot.apiRequestCount,
      );
      expect(finalSnapshot.bulkNodeRequestCount).toBe(0);
      expect(finalSnapshot.bboxScopedRequestCount).toBe(
        finalSnapshot.apiRequestCount,
      );
      expect(finalSnapshot.bboxSourceItemCount).toBe(expectedItems);
      expect(finalSnapshot.loadedItemCount).toBe(expectedItems);
      expect(finalSnapshot.lastResponseHasMore).toBe(false);

      samples.push({
        cardinality,
        page_size: MAP_CARDINALITY_PAGE_SIZE,
        api_request_count: finalSnapshot.apiRequestCount,
        api_response_bytes: finalSnapshot.apiResponseBytes,
        api_error_count: finalSnapshot.apiErrorCount,
        source_node_count: databaseNodeCount(),
        bbox_source_item_count: finalSnapshot.bboxSourceItemCount,
        offscreen_source_item_count:
          cardinality - finalSnapshot.bboxSourceItemCount,
        loaded_item_count: finalSnapshot.loadedItemCount,
        truncated_by_client_limit: false,
        source_has_more_after_last_client_page:
          finalSnapshot.lastResponseHasMore,
        bbox_scoped_request_count: finalSnapshot.bboxScopedRequestCount,
        bulk_node_request_count: finalSnapshot.bulkNodeRequestCount,
        readiness_ms: roundMilliseconds(readinessMs),
        interaction_to_next_paint_ms: roundMilliseconds(interactionMs),
        interaction_sample_count: interactionSamples.length,
        api_response_p95_ms: roundMilliseconds(finalSnapshot.apiResponseP95Ms),
        frame_time_p95_ms: roundMilliseconds(frameCadence.p95),
        frame_time_max_ms: roundMilliseconds(frameCadence.max),
        frame_time_sample_count: frameDeltas.length,
        js_heap_used_bytes: Math.round(jsHeapUsedBytes),
        dom_marker_count: domMarkerCount,
        native_layer_expected: nativeLayerExpected,
        native_layer_actual: nativeLayerActual,
        seeded_after_api_start: true,
      });
    } finally {
      await context.close();
    }
  }

  const evidence = buildMapCardinalityFullstackEvidence({
    sourceRevision,
    generatedAt: new Date().toISOString(),
    browser: { name: "chromium", version: browser.version(), headless: true },
    backend: {
      api_build_commit: apiBuildCommit,
      database_kind: "postgresql",
      startup_node_count: startupNodeCount,
      seeded_after_api_start: true,
    },
    samples,
  });
  const evidencePath = writeMapCardinalityFullstackEvidence(evidence);
  await testInfo.attach("map-cardinality-fullstack-evidence", {
    body: readFileSync(evidencePath),
    contentType: "application/json",
  });
});
