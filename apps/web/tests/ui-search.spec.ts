import { test, expect } from "@playwright/test";
import { mockApiResponses, mockListResponse } from "./fixtures/mockApi";
import { moveSearchTargetOffscreen } from "./fixtures/mapCamera";
import { activateToolFanAction } from "./fixtures/toolFan";

test.describe("Search mode", () => {
  test.beforeEach(async ({ page }) => {
    await mockApiResponses(page);

    await page.route("**/api/nodes*", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(
          mockListResponse(route.request().url(), [
            {
              id: "mock-node-1",
              title: "Abendliches Stricken",
              summary: "Wir stricken gemeinsam",
              kind: "Treffen",
              location: { lat: 51, lon: 10 },
              modules: [],
              created_at: new Date().toISOString(),
            },
          ]),
        ),
      });
    });

    await page.route("**/api/search**", async (route) => {
      const query =
        new URL(route.request().url()).searchParams
          .get("q")
          ?.toLocaleLowerCase("de-DE") ?? "";
      const matches =
        query.includes("strick") || query === "a"
          ? [
              {
                id: "mock-node-1",
                title: "Abendliches Stricken",
                summary: "Wir stricken gemeinsam",
                kind: "Treffen",
                location: { lat: 51, lon: 10 },
                tags: ["Stricken"],
                modules: [],
                created_at: new Date().toISOString(),
                updated_at: new Date().toISOString(),
              },
            ]
          : [];
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: matches,
          mode: "hybrid",
          generation_id: "mock-search-generation",
          offset: 0,
        }),
      });
    });

    await page.route("**/api/node/mock-node-1", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: "mock-node-1",
          title: "Abendliches Stricken",
          summary: "Wir stricken gemeinsam",
          kind: "Treffen",
          location: { lat: 51, lon: 10 },
          modules: [],
          created_at: new Date().toISOString(),
        }),
      });
    });
  });

  test("toggles search overlay, highlights markers, and supports keyboard navigation with map centering", async ({
    page,
  }) => {
    // Navigate to map
    await page.goto("/map");
    // Wait for the map to be ready
    await page.waitForFunction(
      () => (window as any).__TEST_MAP__ !== undefined,
      undefined,
      { timeout: 15000 },
    );
    await moveSearchTargetOffscreen(page);
    await page.waitForSelector(".map-marker");

    // Open Finden via the tool fan
    await activateToolFanAction(page, "find");

    // Verify search overlay appears
    const searchOverlay = page.locator('[data-testid="search-overlay"]');
    await expect(searchOverlay).toBeVisible();

    // Type query
    const searchInput = page.locator(".search-box input");
    await searchInput.fill("Strick");

    // Wait for results to render
    const resultItem = page.locator("li[role='option']").first();
    await expect(resultItem).toBeVisible({ timeout: 10000 });

    // Validate that the result type bubble is rendered
    await expect(resultItem.locator(".result-type")).toBeVisible();
    await expect(resultItem.locator(".result-title")).toContainText("Stricken");

    // Verify the map marker gets highlighted
    const highlightedMarker = page.locator(
      '.map-marker[data-search-match="true"]',
    );
    await expect(highlightedMarker).toBeVisible();
    await expect(highlightedMarker).toHaveAttribute("data-id", "mock-node-1");
    const searchHalo = highlightedMarker.locator(".map-marker__halo");
    await expect(searchHalo).toHaveCSS("width", "68px");
    await expect(searchHalo).toHaveCSS("opacity", "1");

    const direction = page.getByTestId("search-direction-node-mock-node-1");
    await expect(direction).toBeVisible();
    const directionBox = await direction.boundingBox();
    const searchBox = await searchOverlay.boundingBox();
    expect(directionBox).not.toBeNull();
    expect(searchBox).not.toBeNull();
    expect(directionBox!.width).toBeGreaterThanOrEqual(44);
    expect(directionBox!.height).toBeGreaterThanOrEqual(44);
    // The search capsule now sits above the map; a direction marker must stay
    // clear of it regardless of which edge it points to.
    const overlapsSearchBox =
      directionBox!.y < searchBox!.y + searchBox!.height &&
      directionBox!.y + directionBox!.height > searchBox!.y &&
      directionBox!.x < searchBox!.x + searchBox!.width &&
      directionBox!.x + directionBox!.width > searchBox!.x;
    expect(overlapsSearchBox).toBe(false);
    const toolTriggerBox = await page
      .getByTestId("tool-fan-trigger")
      .boundingBox();
    expect(toolTriggerBox).not.toBeNull();
    const overlapsToolTrigger =
      directionBox!.y < toolTriggerBox!.y + toolTriggerBox!.height &&
      directionBox!.y + directionBox!.height > toolTriggerBox!.y &&
      directionBox!.x < toolTriggerBox!.x + toolTriggerBox!.width &&
      directionBox!.x + directionBox!.width > toolTriggerBox!.x;
    expect(overlapsToolTrigger).toBe(false);

    // Clear search and verify highlight and direction marker are removed
    await searchInput.fill("");
    await expect(
      page.locator('.map-marker[data-search-match="true"]'),
    ).toHaveCount(0);
    await expect(direction).toHaveCount(0);

    // Refill and proceed to test keyboard navigation
    await searchInput.fill("Strick");
    await expect(resultItem).toBeVisible();

    // Focus is still on input. Press ArrowDown to highlight first result
    await searchInput.press("ArrowDown");

    // Verify ARIA activedescendant is set
    const firstResultId = `search-option-node-mock-node-1`;
    await expect(searchInput).toHaveAttribute(
      "aria-activedescendant",
      firstResultId,
    );

    // Check that the item has active class
    const activeItem = page.locator(`li#${firstResultId}.active`);
    await expect(activeItem).toBeVisible();

    // Press Enter to select
    await searchInput.press("Enter");

    // Verify search overlay is closed and ContextPanel is open (Fokus state)
    await expect(searchOverlay).not.toBeVisible();
    const contextPanel = page.locator('[data-testid="context-panel"]');
    await expect(contextPanel).toBeVisible();
    await expect(page.locator(".context-panel .panel-header h2")).toContainText(
      /Knoten/,
    );

    // Verify map is centered on the selected node (lon: 10, lat: 51)
    await page.waitForFunction(
      () => {
        const map = (window as any).__TEST_MAP__;
        if (!map) return false;
        const center = map.getCenter();
        // Allow slight epsilon for floating point math
        return (
          Math.abs(center.lng - 10) < 0.0001 &&
          Math.abs(center.lat - 51) < 0.0001
        );
      },
      { timeout: 10000 },
    );

    // Also verify search field was cleared
    await activateToolFanAction(page, "find");
    await expect(searchInput).toHaveValue("");
  });

  test("resets keyboard selection when visible results change without a query change", async ({
    page,
  }) => {
    await page.goto("/map");
    await page.waitForSelector(".map-marker");
    await activateToolFanAction(page, "find");
    const input = page.getByLabel("Suchbegriff");
    await input.fill("a");
    await expect(page.getByRole("option").first()).toBeVisible();

    await input.press("ArrowDown");
    await input.press("ArrowDown");
    await expect(input).toHaveAttribute("aria-activedescendant", /.+/);

    await page.evaluate(() => {
      const setActiveFilters = (window as any).__TEST_SET_ACTIVE_FILTERS__;
      if (typeof setActiveFilters !== "function") {
        throw new Error("test filter control is unavailable");
      }
      setActiveFilters(["Garnrolle"]);
    });

    await expect(input).not.toHaveAttribute("aria-activedescendant", /.+/);
    await input.press("Enter");
    await expect(page.getByTestId("context-panel")).toBeVisible();
  });

  test("zero results show one unambiguous status message", async ({ page }) => {
    await page.goto("/map");
    await page.waitForSelector(".map-marker");
    await activateToolFanAction(page, "find");
    await page
      .getByRole("combobox", { name: "Suchbegriff" })
      .fill("Unauffindbar");

    await expect(
      page.getByText("Keine Treffer für „Unauffindbar“"),
    ).toBeVisible();
    await expect(page.getByText("0 Treffer auf der Karte")).toHaveCount(0);
  });

  test("node and Garnrolle with the same id keep unique option ids", async ({
    page,
  }) => {
    await page.route("**/api/accounts*", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(
          mockListResponse(route.request().url(), [
            {
              id: "mock-node-1",
              title: "Abendliches Stricken Garnrolle",
              role: "weber",
              public_pos: { lat: 51.01, lon: 10.01 },
            },
          ]),
        ),
      });
    });
    await page.goto("/map");
    await page.waitForSelector(".map-marker");
    await activateToolFanAction(page, "find");
    const input = page.getByRole("combobox", { name: "Suchbegriff" });
    await input.fill("Abendliches Stricken");

    const options = page.getByRole("option");
    await expect(options).toHaveCount(2);
    const ids = await options.evaluateAll((elements) =>
      elements.map((element) => element.id),
    );
    expect(new Set(ids).size).toBe(2);
    expect(ids).toContain("search-option-node-mock-node-1");
    expect(ids).toContain("search-option-garnrolle-mock-node-1");
  });

  test("shows at most 6 automatic suggestions with the map as the primary result surface", async ({
    page,
  }) => {
    const manyNodes = Array.from({ length: 9 }, (_, i) => ({
      id: `mock-node-${i}`,
      title: `Strickabend ${i}`,
      summary: "Wir stricken gemeinsam",
      kind: "Treffen",
      location: { lat: 51 + i * 0.001, lon: 10 + i * 0.001 },
      modules: [],
      created_at: new Date().toISOString(),
    }));
    await page.route("**/api/nodes*", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(
          mockListResponse(route.request().url(), manyNodes),
        ),
      });
    });
    await page.route("**/api/search**", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: manyNodes,
          mode: "hybrid",
          generation_id: "mock-search-generation",
          offset: 0,
        }),
      });
    });

    await page.goto("/map");
    await page.waitForFunction(
      () => (window as any).__TEST_MAP__ !== undefined,
      undefined,
      { timeout: 15000 },
    );
    await activateToolFanAction(page, "find");

    const searchInput = page.locator(".search-box input");
    await searchInput.fill("Strick");

    const results = page.locator("li[role='option']");
    await expect(results).toHaveCount(6);
    await expect(page.getByText("9 Treffer", { exact: true })).toBeVisible();

    const showMore = page.getByRole("button", {
      name: "Alle 9 Vorschläge zeigen",
    });
    await expect(showMore).toBeVisible();
    await showMore.click();
    await expect(results).toHaveCount(9);

    // All matches stay highlighted on the map regardless of how many the
    // capsule lists — the map remains the primary result surface.
    await expect(
      page.locator('.map-marker[data-search-match="true"]'),
    ).toHaveCount(9);
  });

  test("offscreen direction marker opens and centers its search result", async ({
    page,
  }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto("/map");
    await page.waitForFunction(
      () => (window as any).__TEST_MAP__ !== undefined,
      undefined,
      { timeout: 15000 },
    );

    await moveSearchTargetOffscreen(page);
    await activateToolFanAction(page, "find");
    await page.getByRole("combobox", { name: "Suchbegriff" }).fill("Strick");

    const direction = page.getByTestId("search-direction-node-mock-node-1");
    await expect(direction).toBeVisible();
    await direction.click();

    await expect(page.getByTestId("search-overlay")).toHaveCount(0);
    await expect(page.getByTestId("context-panel")).toBeVisible();
    await expect(page.getByTestId("search-direction-indicators")).toHaveCount(
      0,
    );
    await page.waitForFunction(
      () => {
        const map = (window as any).__TEST_MAP__;
        if (!map) return false;
        const center = map.getCenter();
        return (
          Math.abs(center.lng - 10) < 0.0001 &&
          Math.abs(center.lat - 51) < 0.0001 &&
          map.getZoom() >= 14
        );
      },
      undefined,
      { timeout: 10000 },
    );
  });

  test("focus restores to the tool fan trigger on Escape", async ({ page }) => {
    await page.goto("/map");
    // Wait for the map to be ready
    await page.waitForFunction(
      () => (window as any).__TEST_MAP__ !== undefined,
      undefined,
      { timeout: 15000 },
    );
    await page.waitForSelector(".map-marker");

    const trigger = page.getByTestId("tool-fan-trigger");
    await activateToolFanAction(page, "find");

    const searchOverlay = page.locator('[data-testid="search-overlay"]');
    await expect(searchOverlay).toBeVisible();

    const searchInput = page.locator(".search-box input");
    await expect(searchInput).toBeFocused();

    await page.keyboard.press("Escape");
    await expect(searchOverlay).not.toBeVisible();

    // Verify focus is restored back to the tool fan trigger
    await expect(trigger).toBeFocused();
  });
});
