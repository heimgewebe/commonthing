import { test, expect } from "@playwright/test";
import { mockApiResponses } from "./fixtures/mockApi";
import { activateToolFanAction } from "./fixtures/toolFan";

test.describe("Interaction Clarity & State Feedback", () => {
  test.beforeEach(async ({ page }) => {
    await mockApiResponses(page, {
      auth: { authenticated: true, account_id: "e2e-weber", role: "weber" },
    });
    await page.goto("/map");
    await page.waitForSelector('[data-testid="tool-fan"]', {
      timeout: 10000,
    });
  });

  test("entering komposition closes open search overlay", async ({ page }) => {
    // Open search overlay via the tool fan
    await activateToolFanAction(page, "find");
    const searchOverlay = page.locator('[data-testid="search-overlay"]');
    await expect(searchOverlay).toBeVisible();

    // Open the fan again and trigger "Weben" while search is open
    await activateToolFanAction(page, "weave");

    // Search overlay must be closed
    await expect(searchOverlay).toHaveCount(0);

    // Context panel must be open in komposition mode
    const panel = page.locator('[data-testid="context-panel"]');
    await expect(panel).toBeVisible();
    await expect(panel.locator(".panel-header h2")).toHaveText(
      "Knoten knüpfen",
    );
  });

  test("entering komposition closes open filter overlay", async ({ page }) => {
    // Open filter overlay via the tool fan
    await activateToolFanAction(page, "sight");
    const filterOverlay = page.locator('[data-testid="filter-overlay"]');
    await expect(filterOverlay).toBeVisible();

    // Open the fan again and trigger "Weben" while Sicht is open
    await activateToolFanAction(page, "weave");

    // Filter overlay must be closed
    await expect(filterOverlay).toHaveCount(0);

    // Context panel must be open in komposition mode
    const panel = page.locator('[data-testid="context-panel"]');
    await expect(panel).toBeVisible();
  });

  test("Weben is an explicit branch and reports active composition after reopening", async ({
    page,
  }) => {
    await activateToolFanAction(page, "weave");
    await expect(page.getByTestId("context-panel")).toBeVisible();

    await page.getByTestId("tool-fan-trigger").click();
    await expect(page.getByTestId("tool-fan-weave")).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  test("Einstellungen bündeln Garnrolle, Konto und Darstellung nachvollziehbar", async ({
    page,
  }) => {
    await page.getByTestId("site-menu-trigger").click();
    const settingsLink = page
      .getByRole("navigation", { name: "Hauptmenü" })
      .getByRole("link", { name: "Einstellungen", exact: true });

    await expect(settingsLink).toBeVisible();
    await expect(settingsLink).toHaveAttribute("href", "/settings");
    await settingsLink.click();
    await expect(page).toHaveURL(/\/settings$/);

    const menu = page.getByTestId("settings-menu");
    const garnrolleLink = menu.getByRole("link", { name: /Meine Garnrolle/ });
    await expect(garnrolleLink).toHaveAttribute("href", "#meine-garnrolle");
    await garnrolleLink.click();
    await expect(page).toHaveURL(/\/settings#meine-garnrolle$/);
  });

  test("focus does not return to the tool fan trigger when entering komposition from Finden", async ({
    page,
  }) => {
    const trigger = page.getByTestId("tool-fan-trigger");

    // Open search overlay (sets restore target to the tool fan trigger)
    await trigger.click();
    await page.getByTestId("tool-fan-find").click();
    const searchOverlay = page.locator('[data-testid="search-overlay"]');
    await expect(searchOverlay).toBeVisible();

    // Trigger "Weben" — suppressNextRestore should prevent focus restore
    await trigger.click();
    await page.getByTestId("tool-fan-weave").click();
    await page.getByTestId("tool-fan-create-node").click();
    await expect(searchOverlay).toHaveCount(0);

    // Focus must NOT be on the tool fan trigger
    await expect(trigger).not.toBeFocused();
  });

  test("focus does not return to the tool fan trigger when entering komposition from Sicht", async ({
    page,
  }) => {
    const trigger = page.getByTestId("tool-fan-trigger");

    // Open filter overlay (sets restore target to the tool fan trigger)
    await trigger.click();
    await page.getByTestId("tool-fan-map-content").click();
    const filterOverlay = page.locator('[data-testid="filter-overlay"]');
    await expect(filterOverlay).toBeVisible();

    // Trigger "Weben" — suppressNextRestore should prevent focus restore
    await trigger.click();
    await page.getByTestId("tool-fan-weave").click();
    await page.getByTestId("tool-fan-create-node").click();
    await expect(filterOverlay).toHaveCount(0);

    // Focus must NOT be on the tool fan trigger
    await expect(trigger).not.toBeFocused();
  });
});
