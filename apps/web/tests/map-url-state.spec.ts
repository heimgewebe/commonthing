import { test, expect, type Page } from "@playwright/test";
import { mockApiResponses, mockListResponse } from "./fixtures/mockApi";
import { waitForMapReady } from "./fixtures/mapReady";

/**
 * Map URL addressing (UI Interaction Doctrine — first executable slice).
 *
 * These tests assert that the `/map` query string is honoured as an
 * *addressing layer* on top of the existing uiView / overlay stores:
 *  - `lens=filter|search` open the matching overlay,
 *  - `focus=<type>:<id>` opens the context panel for an existing entity,
 *  - `compose=node` enters node composition,
 *  - invalid query state is ignored without crashing.
 *
 * Deterministic mock data is layered on top of {@link mockApiResponses} so the
 * deep-link ids stay stable and readable regardless of demo-data changes.
 */

async function installDeferredViewportFetch(page: Page) {
  await page.evaluate(() => {
    const originalFetch = window.fetch.bind(window);
    const pending: Array<{
      url: string;
      aborted: boolean;
      resolve: (response: Response) => void;
    }> = [];
    (window as any).__TEST_VIEWPORT_FETCHES__ = pending;
    window.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
      const rawUrl = input instanceof Request ? input.url : String(input);
      const url = new URL(rawUrl, window.location.origin);
      if (url.pathname === "/api/nodes" && url.searchParams.has("bbox")) {
        let resolveResponse!: (response: Response) => void;
        const response = new Promise<Response>((resolve) => {
          resolveResponse = resolve;
        });
        const entry = {
          url: url.toString(),
          aborted: false,
          resolve: resolveResponse,
        };
        init?.signal?.addEventListener(
          "abort",
          () => {
            entry.aborted = true;
            const reportAbort = (window as any).__TEST_RECORD_VIEWPORT_ABORT__;
            if (typeof reportAbort === "function") void reportAbort(entry.url);
          },
          { once: true },
        );
        pending.push(entry);
        return response;
      }
      return originalFetch(input, init);
    }) as typeof window.fetch;
  });
}

async function waitForDeferredViewportRequests(page: Page, count: number) {
  await page.waitForFunction(
    (expected) =>
      ((window as any).__TEST_VIEWPORT_FETCHES__?.length ?? 0) >= expected,
    count,
  );
}

async function resolveDeferredViewportRequest(
  page: Page,
  index: number,
  node: Record<string, unknown>,
) {
  await page.evaluate(
    ({ requestIndex, item }) => {
      const entry = (window as any).__TEST_VIEWPORT_FETCHES__?.[requestIndex];
      if (!entry)
        throw new Error(`missing deferred viewport request ${requestIndex}`);
      entry.resolve(
        new Response(
          JSON.stringify({
            items: [item],
            page: { limit: 1000, next_cursor: null, has_more: false },
          }),
          {
            status: 200,
            headers: { "content-type": "application/json" },
          },
        ),
      );
    },
    { requestIndex: index, item: node },
  );
}

async function viewportRaceTargets(page: Page) {
  return page.evaluate(() => {
    const map = (window as any).__TEST_MAP__;
    if (!map) throw new Error("test map unavailable");
    const bounds = map.getBounds();
    const center = map.getCenter();
    const span = Math.max(0.05, Math.abs(bounds.getEast() - bounds.getWest()));
    return {
      first: { lng: center.lng + span * 1.25, lat: center.lat },
      second: { lng: center.lng + span * 2.5, lat: center.lat },
    };
  });
}

async function jumpViewport(page: Page, target: { lng: number; lat: number }) {
  await page.evaluate(({ lng, lat }) => {
    const map = (window as any).__TEST_MAP__;
    if (!map) throw new Error("test map unavailable");
    map.jumpTo({ center: [lng, lat] });
  }, target);
}

async function settleViewportUpdates(page: Page) {
  await page.evaluate(
    () =>
      new Promise<void>((resolve) => {
        requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
      }),
  );
}

test.describe("Map URL addressing", () => {
  test.beforeEach(async ({ page }) => {
    await mockApiResponses(page, {
      auth: { authenticated: true, account_id: "e2e-weber", role: "weber" },
    });

    // mockApiResponses mocks the local-sovereign style (/local-basemap/style-germany.json,
    // the active mode in the e2e build). This extra route is a defensive mock for
    // the external MapLibre demo style so the test never depends on the network.
    await page.route(
      "https://demotiles.maplibre.org/style.json",
      async (route) => {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ version: 8, sources: {}, layers: [] }),
        });
      },
    );

    await page.route("**/api/nodes*", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(
          mockListResponse(route.request().url(), [
            {
              id: "url-node-1",
              title: "URL Deep-Link Node",
              kind: "Event",
              location: { lat: 53.5, lon: 10.0 },
              summary: "A node reachable via focus deep link.",
              tags: [],
              modules: [],
              created_at: "2025-01-01T12:00:00Z",
              updated_at: "2025-01-01T12:00:00Z",
            },
          ]),
        ),
      });
    });

    await page.route("**/api/accounts*", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(
          mockListResponse(route.request().url(), [
            {
              id: "url-acc-1",
              type: "garnrolle",
              title: "URL Deep-Link Garnrolle",
              summary: "A garnrolle reachable via focus deep link.",
              public_pos: { lat: 53.55, lon: 10.05 },
              map_state: "exact",
              radius_m: 0,
              tags: [],
              modules: [],
              created_at: "2025-01-01T12:00:00Z",
            },
          ]),
        ),
      });
    });

    await page.route("**/api/edges*", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(mockListResponse(route.request().url(), [])),
      });
    });
  });

  test("opens the filter lens from the URL", async ({ page }) => {
    await page.goto("/map?lens=filter");
    await expect(page.getByTestId("filter-overlay")).toBeVisible();
    await expect(page.getByTestId("search-overlay")).toHaveCount(0);
  });

  test("opens the search lens from the URL", async ({ page }) => {
    await page.goto("/map?lens=search");
    await expect(page.getByTestId("search-overlay")).toBeVisible();
    await expect(page.getByTestId("filter-overlay")).toHaveCount(0);
  });

  test("opens the context panel for a node focus deep link", async ({
    page,
  }) => {
    const nodeGetUrls: string[] = [];
    page.on("request", (request) => {
      const url = new URL(request.url());
      if (
        request.method() === "GET" &&
        (url.pathname === "/api/nodes" ||
          url.pathname.startsWith("/api/nodes/"))
      ) {
        nodeGetUrls.push(request.url());
      }
    });
    const firstViewportRequest = page.waitForRequest((request) => {
      const url = new URL(request.url());
      return (
        request.method() === "GET" &&
        url.pathname === "/api/nodes" &&
        url.searchParams.has("bbox")
      );
    });

    await page.goto("/map?focus=node:url-node-1");
    await firstViewportRequest;
    const panel = page.getByTestId("context-panel");
    await expect(panel).toBeVisible();
    await expect(panel.locator(".panel-header h2")).toContainText("Knoten");
    await page.waitForFunction(
      () => {
        const map = (window as any).__TEST_MAP__;
        if (!map) return false;
        const center = map.getCenter();
        return (
          Math.abs(center.lng - 10) < 0.0005 &&
          Math.abs(center.lat - 53.5) < 0.0005 &&
          map.getZoom() >= 14
        );
      },
      undefined,
      { timeout: 15000 },
    );

    const detailRequests = nodeGetUrls.filter((value) =>
      new URL(value).pathname.startsWith("/api/nodes/"),
    );
    const listRequests = nodeGetUrls.filter(
      (value) => new URL(value).pathname === "/api/nodes",
    );
    // The route bootstrap and the existing NodePanel details loader may both
    // read the same detail endpoint. What matters here is that no other node is
    // fetched and no global node list is used for deep-link resolution.
    expect(detailRequests.length).toBeGreaterThanOrEqual(1);
    expect(
      detailRequests.every(
        (value) => new URL(value).pathname === "/api/nodes/url-node-1",
      ),
    ).toBe(true);
    expect(listRequests.length).toBeGreaterThan(0);
    expect(
      listRequests.every((value) => new URL(value).searchParams.has("bbox")),
    ).toBe(true);
    await expect(page.locator('.map-marker[data-id="url-node-1"]')).toHaveCount(
      1,
    );
    await expect(panel).toBeVisible();
  });

  test("replays a camera move that lands while the initial bbox request is pending", async ({
    page,
  }) => {
    let bboxRequestCount = 0;
    let resolveFirstSeen!: () => void;
    let resolveFirstRelease!: () => void;
    const firstSeen = new Promise<void>((resolve) => {
      resolveFirstSeen = resolve;
    });
    const firstRelease = new Promise<void>((resolve) => {
      resolveFirstRelease = resolve;
    });

    await page.route("**/api/nodes*", async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (
        request.method() !== "GET" ||
        url.pathname !== "/api/nodes" ||
        !url.searchParams.has("bbox")
      ) {
        await route.fallback();
        return;
      }

      bboxRequestCount += 1;
      const isInitialRequest = bboxRequestCount === 1;
      if (isInitialRequest) {
        resolveFirstSeen();
        await firstRelease;
      }
      const node = isInitialRequest
        ? {
            id: "stale-initial-node",
            title: "Stale initial viewport",
            kind: "Event",
            location: { lat: 51.1657, lon: 10.4515 },
            summary: "Must be replaced before the map becomes ready.",
            tags: [],
            modules: [],
            created_at: "2025-01-01T12:00:00Z",
            updated_at: "2025-01-01T12:00:00Z",
          }
        : {
            id: "fresh-moved-node",
            title: "Fresh moved viewport",
            kind: "Event",
            location: { lat: 52.5, lon: 11.5 },
            summary: "Belongs to the latest viewport.",
            tags: [],
            modules: [],
            created_at: "2025-01-01T12:00:00Z",
            updated_at: "2025-01-01T12:00:00Z",
          };
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(mockListResponse(request.url(), [node])),
      });
    });

    await page.goto("/map", { waitUntil: "domcontentloaded" });
    await firstSeen;
    await page.waitForFunction(() => Boolean((window as any).__TEST_MAP__));
    await page.evaluate(() => {
      (window as any).__TEST_MAP__.jumpTo({
        center: [11.5, 52.5],
        zoom: 8,
      });
    });
    resolveFirstRelease();

    await expect
      .poll(() => bboxRequestCount, { timeout: 10_000 })
      .toBeGreaterThanOrEqual(2);
    await waitForMapReady(page);
    await expect(
      page.locator('.map-marker[data-id="fresh-moved-node"]'),
    ).toHaveCount(1);
    await expect(
      page.locator('.map-marker[data-id="stale-initial-node"]'),
    ).toHaveCount(0);
  });

  test("aborts an older active viewport request and ignores its late response", async ({
    page,
  }) => {
    await page.goto("/map");
    await waitForMapReady(page);
    await installDeferredViewportFetch(page);
    const targets = await viewportRaceTargets(page);

    await jumpViewport(page, targets.first);
    await waitForDeferredViewportRequests(page, 1);
    await jumpViewport(page, targets.second);
    await waitForDeferredViewportRequests(page, 2);

    await expect
      .poll(() =>
        page.evaluate(
          () => (window as any).__TEST_VIEWPORT_FETCHES__?.[0]?.aborted,
        ),
      )
      .toBe(true);
    expect(
      await page.evaluate(
        () => (window as any).__TEST_VIEWPORT_FETCHES__?.[1]?.aborted,
      ),
    ).toBe(false);

    const freshNode = {
      id: "latest-active-viewport-node",
      title: "Latest active viewport",
      kind: "Event",
      location: { lat: targets.second.lat, lon: targets.second.lng },
      summary: "Belongs to the newest active viewport request.",
      tags: [],
      modules: [],
      created_at: "2025-01-01T12:00:00Z",
      updated_at: "2025-01-01T12:00:00Z",
    };
    await resolveDeferredViewportRequest(page, 1, freshNode);
    await expect(
      page.locator('.map-marker[data-id="latest-active-viewport-node"]'),
    ).toHaveCount(1);

    const staleNode = {
      id: "stale-active-viewport-node",
      title: "Stale active viewport",
      kind: "Event",
      location: { lat: targets.first.lat, lon: targets.first.lng },
      summary: "Must never overwrite the newer viewport result.",
      tags: [],
      modules: [],
      created_at: "2025-01-01T12:00:00Z",
      updated_at: "2025-01-01T12:00:00Z",
    };
    await resolveDeferredViewportRequest(page, 0, staleNode);
    await settleViewportUpdates(page);

    await expect(
      page.locator('.map-marker[data-id="stale-active-viewport-node"]'),
    ).toHaveCount(0);
    await expect(
      page.locator('.map-marker[data-id="latest-active-viewport-node"]'),
    ).toHaveCount(1);
  });

  test("aborts an in-flight viewport request when the map route unmounts", async ({
    page,
  }) => {
    const abortedViewportUrls: string[] = [];
    await page.exposeFunction(
      "__TEST_RECORD_VIEWPORT_ABORT__",
      (url: string) => {
        abortedViewportUrls.push(url);
      },
    );

    await page.goto("/map");
    await waitForMapReady(page);
    await expect(
      page.getByRole("link", { name: "Einstellungen öffnen" }),
    ).toBeVisible();
    await installDeferredViewportFetch(page);
    const targets = await viewportRaceTargets(page);

    await jumpViewport(page, targets.first);
    await waitForDeferredViewportRequests(page, 1);
    expect(abortedViewportUrls).toEqual([]);

    await page.getByRole("link", { name: "Einstellungen öffnen" }).click();
    await expect(page).toHaveURL(/\/settings(?:[?#]|$)/);
    await expect.poll(() => abortedViewportUrls.length).toBe(1);
    expect(new URL(abortedViewportUrls[0]).pathname).toBe("/api/nodes");
    expect(new URL(abortedViewportUrls[0]).searchParams.has("bbox")).toBe(true);
  });

  test("opens the context panel for a garnrolle focus deep link", async ({
    page,
  }) => {
    await page.goto("/map?focus=garnrolle:url-acc-1");
    const panel = page.getByTestId("context-panel");
    await expect(panel).toBeVisible();
    await expect(panel.locator(".panel-header h2")).toContainText("Garnrolle");
  });

  test("enters node composition from the URL", async ({ page }) => {
    await page.goto("/map?compose=node");
    const panel = page.getByTestId("context-panel");
    await expect(panel).toBeVisible();
    await expect(panel.locator(".state-pending")).toContainText(
      "Ort ausstehend",
    );
  });

  test("opens garnrolle focus panel via account alias", async ({ page }) => {
    await page.goto("/map?focus=account:url-acc-1");
    const panel = page.getByTestId("context-panel");
    await expect(panel).toBeVisible();
    await expect(panel.locator(".panel-header h2")).toContainText("Garnrolle");
  });

  test("does not fall back to lens while a valid focus target is unresolved", async ({
    page,
  }) => {
    await page.goto("/map?focus=node:missing&lens=filter");
    // A valid-but-unresolved focus has priority and blocks the lens fallback.
    await expect(page.locator("#map")).toBeVisible();
    await expect(page.getByTestId("filter-overlay")).toHaveCount(0);
  });

  test("navigating to lens URL does not show stale composition panel", async ({
    page,
  }) => {
    await page.goto("/map?compose=node");
    await expect(page.getByTestId("context-panel")).toBeVisible();

    await page.goto("/map?lens=filter");
    await expect(page.getByTestId("filter-overlay")).toBeVisible();
    await expect(page.getByTestId("context-panel")).toHaveCount(0);
  });

  test("navigating to lens URL does not show stale focus panel", async ({
    page,
  }) => {
    await page.goto("/map?focus=node:url-node-1");
    await expect(page.getByTestId("context-panel")).toBeVisible();

    await page.goto("/map?lens=filter");
    await expect(page.getByTestId("filter-overlay")).toBeVisible();
    await expect(page.getByTestId("context-panel")).toHaveCount(0);
  });

  test("plain map URL does not show stale composition panel", async ({
    page,
  }) => {
    await page.goto("/map?compose=node");
    await expect(page.getByTestId("context-panel")).toBeVisible();

    await page.goto("/map");
    await expect(page.locator("#map")).toBeVisible();
    await expect(page.getByTestId("context-panel")).toHaveCount(0);
    await expect(page.getByTestId("filter-overlay")).toHaveCount(0);
    await expect(page.getByTestId("search-overlay")).toHaveCount(0);
  });

  test("plain map URL does not show stale focus panel", async ({ page }) => {
    await page.goto("/map?focus=node:url-node-1");
    await expect(page.getByTestId("context-panel")).toBeVisible();

    await page.goto("/map");
    await expect(page.locator("#map")).toBeVisible();
    await expect(page.getByTestId("context-panel")).toHaveCount(0);
    await expect(page.getByTestId("filter-overlay")).toHaveCount(0);
    await expect(page.getByTestId("search-overlay")).toHaveCount(0);
  });

  test("unresolved focus closes an already open lens instead of falling back to it", async ({
    page,
  }) => {
    await page.goto("/map?lens=filter");
    await expect(page.getByTestId("filter-overlay")).toBeVisible();

    await page.goto("/map?focus=node:missing&lens=filter");
    await expect(page.locator("#map")).toBeVisible();
    await expect(page.getByTestId("filter-overlay")).toHaveCount(0);
    await expect(page.getByTestId("context-panel")).toHaveCount(0);
  });

  test("opens filter lens even when no markers are available", async ({
    page,
  }) => {
    // Empty datasets (registered after the beforeEach defaults, so they win):
    // the lens is an immediate intent and must not depend on map data.
    await page.route("**/api/nodes*", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(mockListResponse(route.request().url(), [])),
      });
    });
    await page.route("**/api/accounts*", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(mockListResponse(route.request().url(), [])),
      });
    });
    await page.route("**/api/edges*", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(mockListResponse(route.request().url(), [])),
      });
    });

    await page.goto("/map?lens=filter");
    await expect(page.getByTestId("filter-overlay")).toBeVisible();
  });

  test("ignores invalid URL state without crashing", async ({ page }) => {
    await page.goto("/map?focus=node:&lens=nope&compose=edge");
    // The map shell still renders and no overlay/panel is forced open.
    await expect(page.locator("#map")).toBeVisible();
    await expect(page.getByTestId("filter-overlay")).toHaveCount(0);
    await expect(page.getByTestId("search-overlay")).toHaveCount(0);
    await expect(page.getByTestId("context-panel")).toHaveCount(0);
  });
});
