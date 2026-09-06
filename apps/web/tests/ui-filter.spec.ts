import { test, expect } from "@playwright/test";
import { mockApiResponses, mockListResponse } from "./fixtures/mockApi";
import { activateToolFanAction } from "./fixtures/toolFan";

const CENTER = {
  type: "webgemeindezentrum" as const,
  id: "webgemeindezentrum-hammer-park",
  title: "Webgemeindezentrum Hammer Park",
  ortsweberei: {
    id: "ortsweberei-hamm",
    slug: "hamm",
    name: "Ortsweberei Hamm",
    gewebezelle_id: "hamm.weltgewebe.net",
  },
  location_state: "desired" as const,
  location_state_label: "Gewünschter Treffort",
  faden_endpoint_id: "22222222-2222-5222-8222-222222222222",
  conversation_id: "33333333-3333-5333-8333-333333333333",
  location: { lat: 53.5585, lon: 10.058 },
  location_label: "Hammer Park – gewünschter Treffpunkt",
  meeting_note: "Ein bewusst gewählter öffentlicher Treffpunkt.",
  access_note: "Nutzung und Zugänglichkeit sind noch nicht bestätigt.",
  created_at: "2026-08-02T10:08:00.000Z",
  updated_at: "2026-08-02T10:08:00.000Z",
};

test.describe("Sicht mode", () => {
  test.beforeEach(async ({ page }) => {
    // 1. Load the base mock API to prevent CartoCDN errors, etc.
    await mockApiResponses(page);

    // 2. Explicit, deterministic mock data overrides
    await page.route("**/api/webgemeindezentren*", async (route) => {
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(mockListResponse(route.request().url(), [CENTER])),
      });
    });

    await page.route("**/api/nodes*", async (route) => {
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(
          mockListResponse(route.request().url(), [
            {
              id: "node-1",
              title: "Test Node 1",
              kind: "Event",
              location: { lat: 53.5, lon: 10.0 },
              summary: "A test event node.",
              tags: ["thema:natur"],
            },
            {
              id: "node-2",
              title: "Test Node 2",
              kind: "Place",
              location: { lat: 53.6, lon: 10.1 },
              summary: "A test place node.",
              tags: ["thema:kunst"],
            },
          ]),
        ),
      });
    });

    await page.route("**/api/search**", async (route) => {
      const params = new URL(route.request().url()).searchParams;
      const query = params.get("q")?.toLocaleLowerCase("de-DE") ?? "";
      const kinds = new Set(
        (params.get("kinds") ?? "")
          .split(",")
          .map((kind) => kind.trim())
          .filter(Boolean),
      );
      const nodes = [
        {
          id: "node-1",
          title: "Test Node 1",
          kind: "Event",
          location: { lat: 53.5, lon: 10.0 },
          summary: "A test event node.",
          tags: ["thema:natur"],
          created_at: "2026-07-23T00:00:00Z",
          updated_at: "2026-07-23T00:00:00Z",
        },
        {
          id: "node-2",
          title: "Test Node 2",
          kind: "Place",
          location: { lat: 53.6, lon: 10.1 },
          summary: "A test place node.",
          tags: ["thema:kunst"],
          created_at: "2026-07-23T00:00:00Z",
          updated_at: "2026-07-23T00:00:00Z",
        },
      ].filter(
        (node) =>
          node.title.toLocaleLowerCase("de-DE").includes(query) &&
          (kinds.size === 0 || kinds.has(node.kind)),
      );
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: nodes,
          mode: "hybrid",
          generation_id: "mock-search-generation",
          offset: 0,
        }),
      });
    });

    await page.route("**/api/accounts*", async (route) => {
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(
          mockListResponse(route.request().url(), [
            {
              id: "account-1",
              title: "Test Account",
              type: "Garnrolle",
              public_pos: { lat: 53.55, lon: 10.05 },
              summary: "A test account.",
            },
          ]),
        ),
      });
    });

    await page.route("**/api/edges*", async (route) => {
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(mockListResponse(route.request().url(), [])), // No edges needed for this test
      });
    });

    await page.goto("/map");
    // Wait for the map to be ready
    await page.waitForFunction(
      () => (window as any).__TEST_MAP__ !== undefined,
      undefined,
      { timeout: 15000 },
    );
    await page.waitForSelector(".map-marker");
  });

  test("Case A: No filters -> Search global", async ({ page }) => {
    await activateToolFanAction(page, "find");

    const searchOverlay = page.getByTestId("search-overlay");
    await expect(searchOverlay).toBeVisible();

    const searchInput = page.getByRole("combobox", { name: "Suchbegriff" });
    await expect(searchInput).toBeVisible();

    // Search for a node item
    await searchInput.clear();
    await searchInput.fill("Test Node 2");
    await expect(page.locator(".result-item")).toHaveCount(1);
    await expect(page.locator(".result-item").first()).toHaveText(
      /Test Node 2/,
    );

    // Search for an account item
    await searchInput.clear();
    await searchInput.fill("Test Account");
    await expect(page.locator(".result-item")).toHaveCount(1);
    await expect(page.locator(".result-item").first()).toHaveText(
      /Test Account/,
    );

    // Search by the domain term, not only by the individual title.
    await searchInput.clear();
    await searchInput.fill("Garnrollen");
    await expect(page.locator(".result-item")).toHaveCount(1);
    await expect(page.locator(".result-item").first()).toHaveText(
      /Test Account/,
    );
  });

  test("active topic reaches server search before the top-ten cut", async ({
    page,
  }) => {
    let observedTags = "";
    await page.route("**/api/search**", async (route) => {
      const params = new URL(route.request().url()).searchParams;
      observedTags = params.get("tags") ?? "";
      const items = observedTags.split(",").includes("thema:natur")
        ? [
            {
              id: "node-1",
              title: "Test Node 1",
              kind: "Event",
              location: { lat: 53.5, lon: 10.0 },
              summary: "A test event node.",
              tags: ["thema:natur"],
              created_at: "2026-07-23T00:00:00Z",
              updated_at: "2026-07-23T00:00:00Z",
            },
          ]
        : Array.from({ length: 10 }, (_, index) => ({
            id: `off-topic-${index}`,
            title: `Test Fremdtreffer ${index}`,
            kind: "Event",
            location: { lat: 52 + index * 0.001, lon: 9 },
            summary: "Higher-ranked but off-topic",
            tags: ["thema:kunst"],
            created_at: "2026-07-23T00:00:00Z",
            updated_at: "2026-07-23T00:00:00Z",
          }));
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items,
          mode: "hybrid",
          generation_id: "mock-search-generation",
          offset: 0,
        }),
      });
    });

    await activateToolFanAction(page, "sight");
    const sichtOverlay = page.getByTestId("filter-overlay");
    await sichtOverlay
      .locator("label.filter-item", { hasText: "Natur" })
      .click();

    await activateToolFanAction(page, "find");
    await page.getByRole("combobox", { name: "Suchbegriff" }).fill("Test");

    await expect(
      page.getByRole("option", { name: /Test Node 1/ }),
    ).toBeVisible();
    expect(observedTags).toBe("thema:natur");
  });

  test("Sicht narrows the map, and the narrowed marker stays selectable on the map", async ({
    page,
  }) => {
    await activateToolFanAction(page, "sight");

    const sichtOverlay = page.getByTestId("filter-overlay");
    const garnrolleLabel = page.locator("label.filter-item", {
      hasText: "Garnrolle",
    });
    await garnrolleLabel.click();

    // Sicht itself carries no automatic hit list — only the active count.
    await expect(sichtOverlay.getByText(/^1 Auswahl aktiv/)).toBeVisible();
    await expect(page.locator(".filter-result")).toHaveCount(0);

    // The map is the primary result surface: only the selected type remains.
    await expect(page.locator(".map-marker, .marker-account")).toHaveCount(1);
    const marker = page.getByTestId("marker-garnrolle-account-1");
    await expect(marker).toBeVisible();
    await marker.focus();
    await expect(marker).toBeFocused();
    await page.keyboard.press("Enter");

    const contextPanel = page.getByTestId("context-panel");
    await expect(contextPanel).toBeVisible();
    await expect(contextPanel.locator(".panel-header h2")).toContainText(
      "Garnrolle",
    );

    await page.waitForFunction(() => {
      const map = (window as any).__TEST_MAP__;
      if (!map) return false;
      const center = map.getCenter();
      return (
        Math.abs(center.lng - 10.05) < 0.0001 &&
        Math.abs(center.lat - 53.55) < 0.0001
      );
    });
  });

  test("labels Webgemeindezentren as a distinct content type", async ({
    page,
  }) => {
    await activateToolFanAction(page, "sight");

    await expect(
      page
        .getByTestId("filter-overlay")
        .locator("label.filter-item", { hasText: "Webgemeindezentren" }),
    ).toBeVisible();
  });

  test("Case B & E: Active Sicht -> Search strictly bounded, and reset", async ({
    page,
  }) => {
    const markerSelector = ".map-marker, .marker-account";

    // Initial marker count is 4
    await expect(page.locator(markerSelector)).toHaveCount(4);

    // Open Sicht
    await activateToolFanAction(page, "sight");
    const sichtOverlay = page.getByTestId("filter-overlay");
    await expect(sichtOverlay).toBeVisible();

    // Filter to ONLY show "Ereignis"
    const eventLabel = page.locator("label.filter-item", {
      hasText: "Ereignis",
    });
    await eventLabel.click();

    // Marker count drops strictly to 1
    await expect(page.locator(markerSelector)).toHaveCount(1);

    // Open search, verify it strictly respects the active Sicht selection
    await activateToolFanAction(page, "find");
    await expect(sichtOverlay).not.toBeVisible();

    const searchInput = page.getByRole("combobox", { name: "Suchbegriff" });
    await expect(searchInput).toBeVisible();

    // Search for excluded items should return no results
    await searchInput.clear();
    await searchInput.fill("Test Node 2");
    await expect(page.locator(".result-item")).toHaveCount(0);
    await expect(page.getByRole("status")).toHaveText(
      `Keine Treffer für „Test Node 2“`,
    );

    await searchInput.clear();
    await searchInput.fill("Test Account");
    await expect(page.locator(".result-item")).toHaveCount(0);
    await expect(page.getByRole("status")).toHaveText(
      `Keine Treffer für „Test Account“`,
    );

    // Search for included item
    await searchInput.clear();
    await searchInput.fill("Test Node 1");
    await expect(page.locator(".result-item")).toHaveCount(1);
    await expect(page.locator(".result-item").first()).toHaveText(
      /Test Node 1/,
    );

    // Case E: reset restores the full marker count
    await activateToolFanAction(page, "sight");
    const clearBtn = page.getByRole("button", { name: "Alles wieder zeigen" });
    await clearBtn.click();
    await expect(clearBtn).not.toBeVisible();
    await expect(page.locator(markerSelector)).toHaveCount(4);
  });

  test("Case C: Overlay exclusivity and focus shift", async ({ page }) => {
    const sichtOverlay = page.getByTestId("filter-overlay");
    const searchOverlay = page.getByTestId("search-overlay");

    // Open search
    await activateToolFanAction(page, "find");
    await expect(searchOverlay).toBeVisible();
    await expect(sichtOverlay).not.toBeVisible();

    // Open Sicht -> Search closes, focus should be inside Sicht
    await activateToolFanAction(page, "sight");
    await expect(sichtOverlay).toBeVisible();
    await expect(searchOverlay).not.toBeVisible();
    const firstCheckbox = page.locator('input[type="checkbox"]').first();
    await expect(firstCheckbox).toBeFocused();

    // Open search -> Sicht closes, focus should be inside search
    await activateToolFanAction(page, "find");
    await expect(searchOverlay).toBeVisible();
    await expect(sichtOverlay).not.toBeVisible();
    const searchInput = page.getByRole("combobox", { name: "Suchbegriff" });
    await expect(searchInput).toBeFocused();
  });

  test("Case D: Focus management for Sicht", async ({ page }) => {
    const trigger = page.getByTestId("tool-fan-trigger");

    // Open Sicht
    await activateToolFanAction(page, "sight");
    const sichtOverlay = page.getByTestId("filter-overlay");
    await expect(sichtOverlay).toBeVisible();

    // First checkbox should be focused
    const firstCheckbox = page.locator('input[type="checkbox"]').first();
    await expect(firstCheckbox).toBeFocused();

    // Close with Escape
    await page.keyboard.press("Escape");
    await expect(sichtOverlay).not.toBeVisible();

    // Focus lands back on the tool fan trigger
    await expect(trigger).toBeFocused();
  });

  test("active type count and reset are visible in Sicht", async ({ page }) => {
    await activateToolFanAction(page, "sight");
    const sichtOverlay = page.getByTestId("filter-overlay");

    await expect(
      sichtOverlay.getByText(/^Alle \d+ Elemente auf der Karte/),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Alles wieder zeigen" }),
    ).toHaveCount(0);

    await page.locator("label.filter-item", { hasText: "Ereignis" }).click();
    await expect(sichtOverlay.getByText(/^1 Auswahl aktiv/)).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Alles wieder zeigen" }),
    ).toBeVisible();
  });

  test("topics use loaded canonical tags, OR semantics, counts and Alle Themen", async ({
    page,
  }) => {
    await activateToolFanAction(page, "sight");
    const sichtOverlay = page.getByTestId("filter-overlay");
    const markerSelector = ".map-marker, .marker-account";

    await expect(
      sichtOverlay.locator("label.filter-item", { hasText: "Natur" }),
    ).toContainText("1");
    await expect(
      sichtOverlay.locator("label.filter-item", { hasText: "Kunst" }),
    ).toContainText("1");

    await sichtOverlay
      .locator("label.filter-item", { hasText: "Natur" })
      .click();
    await expect(page.locator(markerSelector)).toHaveCount(1);
    await expect(
      sichtOverlay.getByText("1 Auswahl aktiv · 1 von 4 Elementen sichtbar"),
    ).toBeVisible();

    await sichtOverlay
      .locator("label.filter-item", { hasText: "Kunst" })
      .click();
    await expect(page.locator(markerSelector)).toHaveCount(2);
    await expect(
      sichtOverlay.getByText("2 Auswahlen aktiv · 2 von 4 Elementen sichtbar"),
    ).toBeVisible();

    const allTopics = sichtOverlay.getByRole("button", {
      name: /Alle Themen/,
    });
    await allTopics.click();
    await expect(allTopics).toHaveAttribute("aria-pressed", "true");
    await expect(page.locator(markerSelector)).toHaveCount(4);
    await expect(
      page.getByRole("button", { name: "Alles wieder zeigen" }),
    ).toHaveCount(0);
  });
});
