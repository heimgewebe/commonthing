import { test, expect, type Page } from "@playwright/test";
import { mockApiResponses, mockListResponse } from "./fixtures/mockApi";
import { activateToolFanAction } from "./fixtures/toolFan";

const SOURCE_NODE = {
  id: "source-node",
  title: "Offene Fahrradwerkstatt",
  summary: "Gemeinsam Fahrräder reparieren",
  info: "Werkzeuge teilen und voneinander lernen",
  tags: ["Fahrrad", "Reparatur"],
  kind: "Werkstatt",
  location: { lat: 54.9, lon: 8.3 },
  modules: [],
  created_at: "2026-07-23T00:00:00Z",
  updated_at: "2026-07-23T00:00:00Z",
};

const SIMILAR_NODE = {
  id: "similar-node",
  title: "Gemeinschaftliche Radstation",
  summary: "Hilfe bei Fahrradreparaturen",
  kind: "Werkstatt",
  location: { lat: 54.91, lon: 8.31 },
  tags: ["Fahrrad", "Reparatur"],
  modules: [],
  created_at: "2026-07-23T00:00:00Z",
  updated_at: "2026-07-23T00:00:00Z",
};

const SIMILARITY_QUIESCENCE_MS = 500;

async function openSearch(page: Page) {
  await page.goto("/map");
  await page.waitForSelector(".map-marker");
  await activateToolFanAction(page, "find");
  return page.getByRole("combobox", { name: "Suchbegriff" });
}

test.describe("T007 authorized server search contract", () => {
  test.beforeEach(async ({ page }) => {
    await mockApiResponses(page);
    await page.route("**/api/nodes*", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(
          mockListResponse(route.request().url(), [SOURCE_NODE]),
        ),
      });
    });
    await page.route("**/api/node/source-node", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(SOURCE_NODE),
      });
    });
  });

  test("does not fall back to the locally loaded node scene when server search fails", async ({
    page,
  }) => {
    await page.route("**/api/search**", async (route) => {
      await route.fulfill({ status: 503, body: "search_unavailable" });
    });

    const input = await openSearch(page);
    await input.fill("Offene Fahrradwerkstatt");

    await expect(
      page.getByText(
        "Die sichere Knotensuche ist gerade nicht verfügbar. Es wird nicht auf eine lokale Knotensuche zurückgefallen.",
      ),
    ).toBeVisible();
    await expect(page.getByRole("option")).toHaveCount(0);
  });

  test("renders lexical fallback explicitly while keeping server results usable", async ({
    page,
  }) => {
    await page.route("**/api/search**", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: [SOURCE_NODE],
          mode: "lexical_fallback",
          fallback_reason: "provider_unavailable",
          generation_id: "fallback-generation",
          offset: 0,
        }),
      });
    });

    const input = await openSearch(page);
    await input.fill("Fahrradwerkstatt");

    await expect(
      page.getByText(
        "Die semantische Ergänzung ist vorübergehend nicht verfügbar. Die serverseitige lexikalische Suche bleibt aktiv.",
      ),
    ).toBeVisible();
    await expect(page.getByRole("option").first()).toContainText(
      "Offene Fahrradwerkstatt",
    );
  });

  test("ignores a stale slower response after the query changes", async ({
    page,
  }) => {
    await page.route("**/api/search**", async (route) => {
      const query = new URL(route.request().url()).searchParams.get("q") ?? "";
      if (query === "alt") {
        await new Promise((resolve) => setTimeout(resolve, 550));
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            items: [
              { ...SOURCE_NODE, id: "old-node", title: "Altes Ergebnis" },
            ],
            mode: "hybrid",
            generation_id: "old-generation",
            offset: 0,
          }),
        });
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: [{ ...SOURCE_NODE, id: "new-node", title: "Neues Ergebnis" }],
          mode: "hybrid",
          generation_id: "new-generation",
          offset: 0,
        }),
      });
    });

    const input = await openSearch(page);
    await input.fill("alt");
    await page.waitForRequest((request) => {
      const url = new URL(request.url());
      return (
        url.pathname === "/api/search" && url.searchParams.get("q") === "alt"
      );
    });
    await input.fill("neu");

    await expect(page.getByRole("option").first()).toContainText(
      "Neues Ergebnis",
    );
    await expect(page.getByText("Altes Ergebnis")).toHaveCount(0);
    await page.waitForTimeout(600);
    await expect(page.getByRole("option").first()).toContainText(
      "Neues Ergebnis",
    );
    await expect(page.getByText("Altes Ergebnis")).toHaveCount(0);
  });

  test("loads similar nodes only after explicit request and navigates without mutation", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 800, height: 900 });
    let mutationRequests = 0;
    let similarityRequests = 0;
    await page.route("**/api/search**", async (route) => {
      const query = new URL(route.request().url()).searchParams.get("q") ?? "";
      const isSimilarityRequest = query.includes("Werkzeuge teilen");
      if (isSimilarityRequest) similarityRequests += 1;
      const items = isSimilarityRequest
        ? [SOURCE_NODE, SIMILAR_NODE]
        : [SOURCE_NODE];
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items,
          mode: "hybrid",
          generation_id: "similar-generation",
          offset: 0,
        }),
      });
    });
    await page.route("**/api/**", async (route) => {
      if (!["GET", "HEAD"].includes(route.request().method()))
        mutationRequests += 1;
      await route.fallback();
    });

    const input = await openSearch(page);
    await input.fill("Fahrradwerkstatt");
    await page.getByRole("option").first().click();

    const section = page.locator("section.similar-nodes");
    const disclosure = section.locator("details.info-heading");
    const headingTrigger = disclosure.locator("summary");
    const explanation = disclosure.getByRole("dialog", {
      name: "Ähnliche Knoten",
    });

    await expect(
      section.getByRole("heading", { name: "Ähnliche Knoten" }),
    ).toBeVisible();
    await expect(headingTrigger).toHaveAttribute(
      "aria-controls",
      "similar-nodes-heading-explanation",
    );
    await expect(disclosure).not.toHaveAttribute("open", "");
    await expect(explanation).not.toBeVisible();
    await expect(section.locator(":scope > p.similar-note")).toHaveCount(0);

    const trigger = section.getByRole("button", {
      name: "Ähnliche Knoten suchen",
    });
    await expect(trigger).toHaveText("Suchen");
    const sectionBox = await section.boundingBox();
    const headingBox = await headingTrigger.boundingBox();
    const triggerBox = await trigger.boundingBox();
    expect(sectionBox).not.toBeNull();
    expect(headingBox).not.toBeNull();
    expect(triggerBox).not.toBeNull();
    expect(triggerBox!.height).toBeGreaterThanOrEqual(44);
    expect(triggerBox!.width).toBeLessThan(sectionBox!.width / 2);
    expect(triggerBox!.x).toBeGreaterThan(headingBox!.x);
    expect(triggerBox!.y).toBeLessThan(headingBox!.y + headingBox!.height);
    expect(headingBox!.y).toBeLessThan(triggerBox!.y + triggerBox!.height);

    await headingTrigger.focus();
    await headingTrigger.press("Enter");
    await expect(disclosure).toHaveAttribute("open", "");
    await expect(explanation).toBeVisible();
    await expect(explanation).toContainText(
      "Maschinell aus Inhalt und Schlagwörtern dieses Knotens berechnet.",
    );
    await expect(explanation).toContainText("keine Fäden");
    await expect(explanation).toContainText("keine kuratierten Beziehungen");
    await expect(explanation).toContainText(
      "Die Suche wird erst nach deinem ausdrücklichen Klick an den Server gesendet.",
    );

    await headingTrigger.press("Escape");
    await expect(disclosure).not.toHaveAttribute("open", "");
    await expect(explanation).not.toBeVisible();
    await expect(page.getByTestId("context-panel")).toBeVisible();
    await expect(headingTrigger).toBeFocused();

    await headingTrigger.dispatchEvent("contextmenu");
    await expect(disclosure).toHaveAttribute("open", "");
    await expect(explanation).toBeVisible();
    const explanationBox = await explanation.boundingBox();
    expect(explanationBox).not.toBeNull();
    expect(explanationBox!.x).toBeGreaterThanOrEqual(0);
    expect(explanationBox!.width).toBeLessThanOrEqual(384);
    expect(explanationBox!.x + explanationBox!.width).toBeLessThanOrEqual(800);
    expect(explanationBox!.x + explanationBox!.width).toBeCloseTo(784, 0);
    expect(explanationBox!.y + explanationBox!.height).toBeLessThanOrEqual(900);
    await explanation.getByRole("button", { name: "Schließen" }).click();
    await expect(disclosure).not.toHaveAttribute("open", "");
    await expect(explanation).not.toBeVisible();
    await expect(headingTrigger).toBeFocused();

    await page.waitForTimeout(SIMILARITY_QUIESCENCE_MS);
    expect(similarityRequests).toBe(0);

    await section
      .getByRole("button", { name: "Ähnliche Knoten suchen" })
      .click();
    await expect(section.getByText("1 Vorschlag")).toBeVisible();
    await expect(
      section.getByRole("button", { name: /Gemeinschaftliche Radstation/ }),
    ).toBeVisible();
    await page.waitForTimeout(SIMILARITY_QUIESCENCE_MS);
    expect(similarityRequests).toBe(1);
    expect(mutationRequests).toBe(0);

    await section
      .getByRole("button", { name: /Gemeinschaftliche Radstation/ })
      .click();
    await expect(page.getByTestId("context-panel")).toBeVisible();
    expect(mutationRequests).toBe(0);
  });

  test("keeps lexical fallback visible after an explicit similar-nodes request", async ({
    page,
  }) => {
    await page.route("**/api/search**", async (route) => {
      const query = new URL(route.request().url()).searchParams.get("q") ?? "";
      const items = [SOURCE_NODE];
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items,
          mode: query.includes("Werkzeuge teilen")
            ? "lexical_fallback"
            : "hybrid",
          fallback_reason: query.includes("Werkzeuge teilen")
            ? "provider_unavailable"
            : undefined,
          generation_id: "similar-fallback-generation",
          offset: 0,
        }),
      });
    });

    const input = await openSearch(page);
    await input.fill("Fahrradwerkstatt");
    await page.getByRole("option").first().click();

    const section = page.locator("section.similar-nodes");
    await section
      .getByRole("button", { name: "Ähnliche Knoten suchen" })
      .click();
    await expect(section).toContainText(
      "Die semantische Ergänzung ist gerade nicht verfügbar; die Vorschläge stammen aus dem lexikalischen Serverpfad.",
    );
    await expect(section).toContainText("Keine ähnlichen Knoten gefunden.");
  });
});
