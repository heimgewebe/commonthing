import { devices, expect, test, type Page } from "@playwright/test";
import { FADEN_LIFETIME_MS } from "../src/lib/map/edgeLifecycle";
import { demoAccounts, demoNodes } from "../src/lib/demo/demoData";
import { mockApiResponses, mockListResponse } from "./fixtures/mockApi";
import { waitForMapReady } from "./fixtures/mapReady";

const EDGE_ID = "eb5f41ff-3e64-417e-ae7e-eecd9c886ecc";
const NODE_ID = "b52be17c-4ab7-4434-98ce-520f86290cf0";
const MULTI_EDGE_ID = "motion-multi-theme-edge";
const MULTI_NODE_ID = demoNodes[0].id;
const MULTI_ACCOUNT_ID = demoAccounts[0].id;
const FADEN_OUT_FILTER = ["==", ["get", "fadenType"], "out"];
const IPAD_PRO_11_LANDSCAPE = {
  userAgent: devices["iPad Pro 11 landscape"].userAgent,
  viewport: devices["iPad Pro 11 landscape"].viewport,
  deviceScaleFactor: devices["iPad Pro 11 landscape"].deviceScaleFactor,
  isMobile: devices["iPad Pro 11 landscape"].isMobile,
  hasTouch: devices["iPad Pro 11 landscape"].hasTouch,
};

type MotionSnapshot = {
  activeCount: number;
  framePending: boolean;
  frameRequests: number;
  frameCallbacks: number;
  styleRefreshes: number;
  suppressedIds: string[];
  active: Array<{
    id: string;
    phase: "creating" | "releasing";
    progress: number;
  }>;
};

type EdgeMotionHook = {
  start(edgeId: string, phase: "creating" | "releasing"): boolean;
  startForNode(nodeId: string, phase: "creating" | "releasing"): void;
  inspect(): MotionSnapshot | null;
  syncCanonicalIds(ids: string[]): void;
};

type TestMap = {
  getLayer(id: string): unknown;
  getSource(
    id: string,
  ):
    | { serialize(): { data?: GeoJSON.FeatureCollection<GeoJSON.LineString> } }
    | undefined;
  getFilter(id: string): unknown;
  getCenter(): { lng: number; lat: number };
  getZoom(): number;
  jumpTo(options: { center?: [number, number]; zoom?: number }): void;
  setStyle(style: {
    version: 8;
    sources: Record<string, never>;
    layers: never[];
  }): void;
  isStyleLoaded(): boolean;
};

declare global {
  interface Window {
    __TEST_MAP__?: TestMap;
    __TEST_EDGE_MOTION__?: EdgeMotionHook;
    __TEST_SET_ACTIVE_FILTERS__?: (types: string[]) => void;
  }
}

async function openMap(page: Page) {
  const pageErrors: string[] = [];
  const consoleErrors: string[] = [];
  page.on("pageerror", (error) =>
    pageErrors.push(error.stack ?? error.message),
  );
  page.on("console", (message) => {
    if (message.type() === "error" || message.type() === "warning") {
      consoleErrors.push(`[${message.type()}] ${message.text()}`);
    }
  });
  await mockApiResponses(page);
  await page.goto("/map");
  await waitForMapReady(page);
  try {
    await page.waitForFunction(
      () => {
        const map = window.__TEST_MAP__;
        const staticFeatures =
          map?.getSource("edges-source")?.serialize().data?.features ?? [];
        return Boolean(
          map?.isStyleLoaded() &&
          window.__TEST_EDGE_MOTION__ &&
          map.getLayer("edges-layer") &&
          staticFeatures.length > 0,
        );
      },
      undefined,
      { timeout: 5_000 },
    );
  } catch (error) {
    const state = await page.evaluate(() => ({
      href: location.href,
      readyState: document.readyState,
      hasMap: Boolean(window.__TEST_MAP__),
      hasHook: Boolean(window.__TEST_EDGE_MOTION__),
      mapStyleLoaded: window.__TEST_MAP__?.isStyleLoaded() ?? null,
      hasEdgesLayer: Boolean(window.__TEST_MAP__?.getLayer("edges-layer")),
      body: document.body.innerText.slice(0, 500),
    }));
    throw new Error(
      `map bootstrap failed: ${JSON.stringify({ state, pageErrors, consoleErrors })}`,
      { cause: error },
    );
  }
}

async function snapshot(page: Page) {
  return page.evaluate(() => window.__TEST_EDGE_MOTION__?.inspect() ?? null);
}

async function motionFeatures(page: Page) {
  return page.evaluate(() => {
    const source = window.__TEST_MAP__?.getSource("edge-motion-source");
    return source?.serialize().data?.features ?? [];
  });
}

test.describe("event-bound Faden motion", () => {
  test.beforeEach(async ({ page }) => {
    await openMap(page);
  });

  test("grows from the canonical node-create event path and stops every frame loop", async ({
    page,
  }) => {
    await page.evaluate((nodeId) => {
      window.__TEST_EDGE_MOTION__?.startForNode(nodeId, "creating");
    }, NODE_ID);

    await expect.poll(async () => (await snapshot(page))?.activeCount).toBe(1);
    await expect.poll(async () => (await motionFeatures(page)).length).toBe(1);

    const during = await page.evaluate(() => {
      const map = window.__TEST_MAP__!;
      const center = map.getCenter();
      map.jumpTo({
        center: [center.lng + 0.015, center.lat + 0.008],
        zoom: map.getZoom() + 0.5,
      });
      const feature = map.getSource("edge-motion-source")?.serialize().data
        ?.features[0];
      return {
        phase: feature?.properties?.phase,
        coordinateCount: feature?.geometry.coordinates.length,
        filter: map.getFilter("edges-layer"),
      };
    });
    expect(during.phase).toBe("creating");
    // Natural thread curves are bounded polylines (not two-point capsules).
    expect(during.coordinateCount).toBeGreaterThanOrEqual(2);
    expect(during.coordinateCount).toBeLessThanOrEqual(96);
    expect(during.filter).not.toBeNull();

    await expect
      .poll(async () => (await snapshot(page))?.activeCount, { timeout: 3000 })
      .toBe(0);
    const finished = await snapshot(page);
    expect(finished?.framePending).toBe(false);
    expect(finished?.frameCallbacks).toBeLessThan(100);
    expect(await motionFeatures(page)).toEqual([]);
    expect(
      await page.evaluate(() => window.__TEST_MAP__?.getFilter("edges-layer")),
    ).toEqual(FADEN_OUT_FILTER);
  });

  test("retracts, survives style reload and remains hidden until canonical removal", async ({
    page,
  }) => {
    // The transient release frame may disappear before a delayed Playwright
    // poll on a loaded runner. Observe the synchronous initial render instead:
    // absence of the real frame still fails the test.
    const initial = await page.evaluate((edgeId) => {
      const accepted =
        window.__TEST_EDGE_MOTION__?.start(edgeId, "releasing") ?? false;
      const frameCount =
        window.__TEST_MAP__?.getSource("edge-motion-source")?.serialize().data
          ?.features.length ?? 0;
      const activeCount =
        window.__TEST_EDGE_MOTION__?.inspect()?.activeCount ?? 0;
      return { accepted, frameCount, activeCount };
    }, EDGE_ID);
    expect(initial).toEqual({
      accepted: true,
      frameCount: 1,
      activeCount: 1,
    });
    await page.evaluate(() => {
      window.__TEST_MAP__?.setStyle({ version: 8, sources: {}, layers: [] });
    });
    await page.waitForFunction(() => {
      const map = window.__TEST_MAP__;
      return Boolean(
        map?.getLayer("edges-layer") &&
        map.getLayer("edge-motion-layer-out") &&
        map.getLayer("edge-motion-layer-proposal") &&
        map.getLayer("edge-motion-layer-conversation") &&
        map.getLayer("edge-motion-layer-knotting") &&
        map.getLayer("edge-motion-layer-vote"),
      );
    });

    await expect
      .poll(async () => (await snapshot(page))?.activeCount, { timeout: 3000 })
      .toBe(0);
    expect((await snapshot(page))?.suppressedIds).toContain(EDGE_ID);
    expect(
      await page.evaluate(() => window.__TEST_MAP__?.getFilter("edges-layer")),
    ).not.toBeNull();

    await page.evaluate(() =>
      window.__TEST_EDGE_MOTION__?.syncCanonicalIds([]),
    );
    expect((await snapshot(page))?.suppressedIds).toEqual([]);
    expect(
      await page.evaluate(() => window.__TEST_MAP__?.getFilter("edges-layer")),
    ).toEqual(FADEN_OUT_FILTER);
  });

  test("filter changes never create motion and only control transition visibility", async ({
    page,
  }) => {
    const before = await snapshot(page);
    await page.evaluate(() =>
      window.__TEST_SET_ACTIVE_FILTERS__?.(["Garnrolle"]),
    );
    await page.waitForTimeout(50);
    const afterFilter = await snapshot(page);
    expect(afterFilter?.activeCount).toBe(0);
    expect(afterFilter?.frameRequests).toBe(before?.frameRequests);

    await page.evaluate(
      (edgeId) => window.__TEST_EDGE_MOTION__?.start(edgeId, "creating"),
      EDGE_ID,
    );
    await page.waitForTimeout(80);
    expect(await motionFeatures(page)).toEqual([]);

    await page.evaluate(() => window.__TEST_SET_ACTIVE_FILTERS__?.([]));
    await expect.poll(async () => (await motionFeatures(page)).length).toBe(1);
    await expect
      .poll(async () => (await snapshot(page))?.activeCount, { timeout: 3000 })
      .toBe(0);
  });

  test("rapid opposing events reverse without spawning multiple frame loops", async ({
    page,
  }) => {
    await page.evaluate(
      (edgeId) => window.__TEST_EDGE_MOTION__?.start(edgeId, "creating"),
      EDGE_ID,
    );
    await page.waitForTimeout(250);
    const before = await snapshot(page);
    expect(before?.activeCount).toBe(1);
    expect(before?.active[0].phase).toBe("creating");
    await page.evaluate(
      (edgeId) => window.__TEST_EDGE_MOTION__?.start(edgeId, "releasing"),
      EDGE_ID,
    );
    const after = await snapshot(page);

    expect(after?.activeCount).toBe(1);
    expect(after?.active[0].phase).toBe("releasing");
    expect(after?.framePending).toBe(true);
    expect(after?.active[0].progress).toBeGreaterThan(0);
    expect(after?.active[0].progress).toBeLessThan(1);
    await expect
      .poll(async () => (await snapshot(page))?.activeCount, { timeout: 3000 })
      .toBe(0);
    expect((await snapshot(page))?.framePending).toBe(false);
  });
});

test.describe("reduced motion", () => {
  test("uses the canonical end state immediately and requests no RAF", async ({
    page,
  }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await openMap(page);

    await page.evaluate(
      (edgeId) => window.__TEST_EDGE_MOTION__?.start(edgeId, "creating"),
      EDGE_ID,
    );
    expect(await snapshot(page)).toMatchObject({
      activeCount: 0,
      framePending: false,
      frameRequests: 0,
    });

    await page.evaluate(
      (edgeId) => window.__TEST_EDGE_MOTION__?.start(edgeId, "releasing"),
      EDGE_ID,
    );
    expect(await snapshot(page)).toMatchObject({
      activeCount: 0,
      framePending: false,
      frameRequests: 0,
      suppressedIds: [EDGE_ID],
    });
  });
});

test.describe("motion/static multi-theme palette parity", () => {
  test("motion reuses the projected multi-theme braid and never invents monochrome", async ({
    page,
  }) => {
    const createdAtMs = Date.now() - 60_000;
    const createdAt = new Date(createdAtMs).toISOString();
    const expiresAt = new Date(createdAtMs + FADEN_LIFETIME_MS).toISOString();
    const multiThemeNode = {
      ...demoNodes[0],
      tags: ["Natur", "Bildung", "Kunst"],
      kind: "Knoten",
    };
    const multiEdge = {
      id: MULTI_EDGE_ID,
      source_id: MULTI_ACCOUNT_ID,
      source_type: "account",
      target_id: MULTI_NODE_ID,
      target_type: "node",
      edge_kind: "reference",
      faden_type: "knotting",
      faden_subject_id: MULTI_NODE_ID,
      created_at: createdAt,
      expires_at: expiresAt,
    };

    await mockApiResponses(page);
    await page.route("**/api/nodes*", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(
          mockListResponse(route.request().url(), [multiThemeNode]),
        ),
      });
    });
    await page.route("**/api/edges*", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(
          mockListResponse(route.request().url(), [multiEdge]),
        ),
      });
    });

    await page.goto("/map");
    await page.waitForFunction(
      () => {
        const map = window.__TEST_MAP__;
        const staticFeatures =
          map?.getSource("edges-source")?.serialize()?.data?.features ?? [];
        return Boolean(
          map?.isStyleLoaded() &&
          window.__TEST_EDGE_MOTION__ &&
          staticFeatures.some(
            (feature) =>
              Array.isArray(feature.properties?.themeColors) &&
              feature.properties.themeColors.length > 1,
          ),
        );
      },
      undefined,
      { timeout: 15_000 },
    );

    const staticPalette = await page.evaluate((edgeId) => {
      const features =
        window.__TEST_MAP__?.getSource("edges-source")?.serialize()?.data
          ?.features ?? [];
      const match = features.find(
        (feature) => feature.properties?.id === edgeId,
      );
      return (match?.properties?.themeColors as string[] | undefined) ?? [];
    }, MULTI_EDGE_ID);
    expect(staticPalette.length).toBeGreaterThan(1);

    expect(
      await page.evaluate(
        (edgeId) => window.__TEST_EDGE_MOTION__?.start(edgeId, "creating"),
        MULTI_EDGE_ID,
      ),
    ).toBe(true);

    await expect
      .poll(async () => (await motionFeatures(page)).length)
      .toBeGreaterThan(1);

    const motionState = await page.evaluate(() => {
      const features =
        window.__TEST_MAP__?.getSource("edge-motion-source")?.serialize()?.data
          ?.features ?? [];
      return {
        count: features.length,
        palettes: features.map(
          (feature) =>
            (feature.properties?.themeColors as string[] | undefined) ?? [],
        ),
        strandColors: features.map(
          (feature) => feature.properties?.themeColor as string | undefined,
        ),
      };
    });

    expect(motionState.count).toBeGreaterThan(1);
    for (const palette of motionState.palettes) {
      expect(palette).toEqual(staticPalette);
      expect(palette.length).toBeGreaterThan(1);
    }
    expect(new Set(motionState.strandColors).size).toBeGreaterThan(1);
    for (const color of motionState.strandColors) {
      expect(staticPalette).toContain(color);
    }

    await expect
      .poll(async () => (await snapshot(page))?.activeCount, { timeout: 3000 })
      .toBe(0);
  });
});

test.describe("iPad-like touch profile", () => {
  test.use(IPAD_PRO_11_LANDSCAPE);

  test("keeps map interaction responsive and leaves no persistent activity", async ({
    page,
  }) => {
    await openMap(page);
    await page.evaluate(
      (edgeId) => window.__TEST_EDGE_MOTION__?.start(edgeId, "creating"),
      EDGE_ID,
    );
    await page.evaluate(() => {
      const map = window.__TEST_MAP__!;
      const center = map.getCenter();
      map.jumpTo({
        center: [center.lng - 0.01, center.lat + 0.01],
        zoom: map.getZoom() + 0.25,
      });
    });

    await expect
      .poll(async () => (await snapshot(page))?.activeCount, { timeout: 3000 })
      .toBe(0);
    const result = await snapshot(page);
    expect(result?.framePending).toBe(false);
    expect(result?.frameCallbacks).toBeLessThan(100);
    expect(
      await page.evaluate(
        () =>
          document.documentElement.scrollWidth <=
          document.documentElement.clientWidth,
      ),
    ).toBe(true);
  });
});
