import { test, expect, type Page } from "@playwright/test";
import { mockApiResponses } from "./fixtures/mockApi";
import { activateToolFanAction } from "./fixtures/toolFan";

const WEBER_ACCOUNT_ID = "weber-e2e-1";

async function mockMapTiles(page: Page) {
  await page.route("https://demotiles.maplibre.org/style.json", (route) => {
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ version: 8, sources: {}, layers: [] }),
    });
  });
}

async function gotoMapAs(
  page: Page,
  auth: { authenticated: boolean; account_id?: string; role: string },
) {
  await mockApiResponses(page, { auth });
  await mockMapTiles(page);
  await page.goto("/map");
  await page.waitForSelector('[data-testid="tool-fan"]', { timeout: 10000 });
}

async function longPressMapCenter(page: Page) {
  const mapContainer = page.locator("#map");
  await mapContainer.hover({ position: { x: 50, y: 50 } });
  await page.mouse.down();
  await page.waitForTimeout(1000); // 800ms threshold
  await page.mouse.up();
}

test.describe("Komposition Flow (weber)", () => {
  test.beforeEach(async ({ page }) => {
    await gotoMapAs(page, {
      authenticated: true,
      account_id: WEBER_ACCOUNT_ID,
      role: "weber",
    });
  });

  test("Komposition form requires location, name, and address to submit", async ({
    page,
  }) => {
    await activateToolFanAction(page, "weave");
    const panel = page.locator('[data-testid="context-panel"]');
    await expect(panel).toBeVisible();
    await expect(page.locator("#title")).toBeFocused();

    await expect(panel.locator(".state-pending")).toContainText(
      "Ort ausstehend",
    );

    const submitBtn = panel.locator('button[type="submit"]');
    await expect(submitBtn).toBeDisabled();

    await page.fill("#title", "Test Node");
    await expect(submitBtn).toBeDisabled(); // still no location, no address

    await page.fill("#address", "Teststraße 1, Teststadt");
    await expect(submitBtn).toBeDisabled(); // still no location

    await longPressMapCenter(page);
    await expect(panel.locator(".state-set")).toContainText("Ort gewählt");
    await expect(submitBtn).toBeEnabled();

    await page.fill("#address", "");
    await expect(submitBtn).toBeDisabled();
    await page.fill("#address", "Teststraße 1, Teststadt");
    await expect(submitBtn).toBeEnabled();

    await page.fill("#title", "");
    await expect(submitBtn).toBeDisabled();
  });

  test("Cancel flow cleans up state and closes panel", async ({ page }) => {
    await activateToolFanAction(page, "weave");
    const panel = page.locator('[data-testid="context-panel"]');
    await expect(panel).toBeVisible();

    await panel.locator('button:has-text("Abbrechen")').click();

    await expect(panel).toHaveCount(0);
  });

  test("Successful submit creates the node, links it to the account, reloads data, and focuses it", async ({
    page,
  }) => {
    const nodeRequest = page.waitForRequest(
      (req) => req.url().endsWith("/api/nodes") && req.method() === "POST",
    );
    const directEdgePosts: string[] = [];
    page.on("request", (request) => {
      if (request.url().endsWith("/api/edges") && request.method() === "POST") {
        directEdgePosts.push(request.url());
      }
    });

    await activateToolFanAction(page, "weave");
    const panel = page.locator('[data-testid="context-panel"]');

    await longPressMapCenter(page);
    await expect(panel.locator(".state-set")).toContainText("Ort gewählt");

    await page.fill("#title", "My Awesome Node");
    await page.fill("#address", "Musterstraße 1, 12345 Musterstadt");
    await page.fill("#description", "This is a description");
    await page.selectOption("#nodeType", "event");

    const submitBtn = panel.locator('button[type="submit"]');
    await expect(submitBtn).toBeEnabled();
    await page.waitForFunction(() =>
      Boolean(
        (window as any).__TEST_MAP__ &&
        !(window as any).__TEST_MAP__.isMoving(),
      ),
    );
    const boundsBeforeSubmit = await page.evaluate(() => {
      const bounds = (window as any).__TEST_MAP__.getBounds();
      return {
        west: bounds.getWest(),
        south: bounds.getSouth(),
        east: bounds.getEast(),
        north: bounds.getNorth(),
      };
    });
    const routeReloadViewport = page.waitForRequest((request) => {
      const url = new URL(request.url());
      return (
        request.method() === "GET" &&
        url.pathname === "/api/nodes" &&
        url.searchParams.has("bbox")
      );
    });
    await submitBtn.click();
    const routeReloadRequest = await routeReloadViewport;
    const routeReloadUrl = new URL(routeReloadRequest.url());
    const routeReloadBbox = (routeReloadUrl.searchParams.get("bbox") ?? "")
      .split(",")
      .map(Number);
    expect(routeReloadBbox).toHaveLength(4);
    expect(routeReloadBbox[0]).toBeCloseTo(boundsBeforeSubmit.west, 5);
    expect(routeReloadBbox[1]).toBeCloseTo(boundsBeforeSubmit.south, 5);
    expect(routeReloadBbox[2]).toBeCloseTo(boundsBeforeSubmit.east, 5);
    expect(routeReloadBbox[3]).toBeCloseTo(boundsBeforeSubmit.north, 5);

    // Node created with the form's values before any edge is attempted.
    const nodeReq = await nodeRequest;
    const nodePayload = nodeReq.postDataJSON();
    expect(nodePayload).toMatchObject({
      title: "My Awesome Node",
      kind: "event",
      address: "Musterstraße 1, 12345 Musterstadt",
      summary: "This is a description",
    });
    expect(typeof nodePayload.location?.lat).toBe("number");
    expect(typeof nodePayload.location?.lon).toBe("number");
    expect(nodePayload.operation_id).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i,
    );

    // The browser never creates or edits a Faden. The API derives the
    // account-to-node visualization from the successful Webungsaktion.
    expect(directEdgePosts).toEqual([]);

    // Data reloaded: the debug badge's node count increases by one.
    const debugBadge = page.locator('[data-testid="debug-badge"]');
    await expect(debugBadge).toContainText("Nodes: 2");

    // The panel leaves komposition mode and the new node is focused: the
    // context panel now shows "Knoten" instead of the create form.
    await expect(panel.locator("h2")).toContainText("Knoten");

    // `lastCreatedNodeId` is a one-shot intent. After the focus has settled,
    // a later viewport refresh must not snap the camera back to the new node.
    await page.waitForFunction(() =>
      Boolean(
        (window as any).__TEST_MAP__ &&
        !(window as any).__TEST_MAP__.isMoving(),
      ),
    );
    const shiftedViewportRefresh = page.waitForRequest((request) => {
      const url = new URL(request.url());
      return (
        request.method() === "GET" &&
        url.pathname === "/api/nodes" &&
        url.searchParams.has("bbox")
      );
    });
    const shiftedCenter = await page.evaluate((previousCoverage) => {
      const map = (window as any).__TEST_MAP__;
      const center = map.getCenter();
      // Coverage reuse intentionally suppresses contained moveend reloads. Move
      // farther than the previously loaded bbox width so this assertion still
      // observes a real later viewport refresh.
      const previousLongitudeSpan = Math.max(
        0.02,
        Math.abs(previousCoverage.east - previousCoverage.west),
      );
      const target = {
        lng: center.lng + previousLongitudeSpan * 1.1,
        lat: center.lat,
      };
      map.jumpTo({ center: [target.lng, target.lat] });
      return target;
    }, boundsBeforeSubmit);
    await shiftedViewportRefresh;
    await page.waitForFunction(() =>
      Boolean(
        (window as any).__TEST_MAP__ &&
        !(window as any).__TEST_MAP__.isMoving(),
      ),
    );
    const centerAfterRefresh = await page.evaluate(() => {
      const center = (window as any).__TEST_MAP__.getCenter();
      return { lng: center.lng, lat: center.lat };
    });
    expect(centerAfterRefresh.lng).toBeCloseTo(shiftedCenter.lng, 4);
    expect(centerAfterRefresh.lat).toBeCloseTo(shiftedCenter.lat, 4);
  });

  test("Node retry reuses its operation id while unchanged form data starts no second action", async ({
    page,
  }) => {
    const operationIds: string[] = [];
    let attempts = 0;
    await page.route("**/api/nodes", async (route) => {
      if (route.request().method() !== "POST") {
        return route.fallback();
      }
      const payload = route.request().postDataJSON();
      operationIds.push(payload.operation_id);
      attempts += 1;
      if (attempts === 1) {
        return route.fulfill({ status: 500 });
      }
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: "mock-node-replay",
          ...payload,
          tags: [],
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        }),
      });
    });

    await activateToolFanAction(page, "weave");
    const panel = page.locator('[data-testid="context-panel"]');
    await longPressMapCenter(page);
    await page.fill("#title", "Retry Node");
    await page.fill("#address", "Retrystraße 1");

    await panel.locator('button[type="submit"]').click();
    await expect(panel.locator('[role="alert"]')).toBeVisible();
    await panel.locator('button[type="submit"]').click();

    await expect(panel.locator("h2")).toContainText("Knoten");
    expect(operationIds).toHaveLength(2);
    expect(operationIds[1]).toBe(operationIds[0]);
  });
});

test.describe("Komposition Flow (gast/anonymous)", () => {
  test("Authenticated guests can open the node composer", async ({ page }) => {
    await gotoMapAs(page, {
      authenticated: true,
      account_id: "guest-e2e-1",
      role: "gast",
    });
    await activateToolFanAction(page, "weave");
    const panel = page.locator('[data-testid="context-panel"]');
    await expect(panel).toBeVisible();
    await expect(panel.getByLabel("Name")).toBeFocused();
  });

  test("Anonymous visitors get no node write path and no longpress panel", async ({
    page,
  }) => {
    await gotoMapAs(page, { authenticated: false, role: "gast" });

    await page.getByTestId("tool-fan-trigger").click();
    const weave = page.getByTestId("tool-fan-weave");
    await expect(weave).toBeDisabled();
    await expect(weave).toHaveAttribute(
      "aria-label",
      "Weben – Anmeldung erforderlich",
    );
    await expect(page.locator('[data-testid="context-panel"]')).toHaveCount(0);

    await longPressMapCenter(page);
    await expect(page.locator('[data-testid="context-panel"]')).toHaveCount(0);
  });
});
