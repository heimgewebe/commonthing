import { test, expect } from "@playwright/test";
import { mockApiResponses } from "./fixtures/mockApi";
import { activateToolFanAction } from "./fixtures/toolFan";
import {
  ACCOUNT_MARKER_SELECTOR,
  NODE_MARKER_SELECTOR,
  waitForMapReady,
} from "./fixtures/mapReady";

test.describe("Map Interaction & Context Panel", () => {
  test.beforeEach(async ({ page }) => {
    await mockApiResponses(page, {
      auth: { authenticated: true, account_id: "e2e-weber", role: "weber" },
    });
    await page.goto("/map");
    await waitForMapReady(page);
  });

  test("displays explicit OSM/ODbL attribution", async ({ page }) => {
    const attributionLink = page.locator(".maplibregl-ctrl-attrib-inner a", {
      hasText: "OpenStreetMap",
    });
    await expect(attributionLink).toBeVisible();
    await expect(attributionLink).toHaveAttribute(
      "href",
      "https://www.openstreetmap.org/copyright",
    );
    await expect(attributionLink).toHaveAttribute("target", "_blank");
    await expect(attributionLink).toHaveAttribute("rel", "noopener noreferrer");
  });

  test("Initial state is navigation with closed panel", async ({ page }) => {
    await page.waitForSelector(".map-marker", { timeout: 10000 });
    await expect(page.locator('[data-testid="context-panel"]')).toHaveCount(0);
  });

  test("Clicking a marker opens the context panel in fokus mode", async ({
    page,
  }) => {
    await page.waitForSelector(".map-marker", { timeout: 10000 });

    // Click a marker
    // using page.evaluate because map markers overlap with other invisible MapLibre overlay elements
    await page.evaluate(() => {
      (document.querySelector(".map-marker") as HTMLElement)?.dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
    });

    // Context panel should open
    const panel = page.locator('[data-testid="context-panel"]');
    await expect(panel).toBeVisible();

    // The panel header should have close button
    const closeBtn = panel.locator(".close-btn");
    await expect(closeBtn).toBeVisible();

    // Click close
    await closeBtn.click({ force: true });
    await expect(page.locator('[data-testid="context-panel"]')).toHaveCount(0);
  });

  test("Escape closes ContextPanel when in fokus mode", async ({ page }) => {
    await page.waitForSelector(".map-marker", { timeout: 10000 });
    await page.evaluate(() => {
      (document.querySelector(".map-marker") as HTMLElement)?.dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
    });

    const panel = page.locator('[data-testid="context-panel"]');
    await expect(panel).toBeVisible();

    // Press Escape key
    await page.keyboard.press("Escape");

    // Panel should close
    await expect(panel).toHaveCount(0);
  });

  test("Escape closes ContextPanel when composing", async ({ page }) => {
    await page.waitForSelector('[data-testid="tool-fan"]', { timeout: 10000 });

    // Enter komposition mode via the tool fan
    await activateToolFanAction(page, "weave");

    const panel = page.locator('[data-testid="context-panel"]');
    await expect(panel).toBeVisible();

    // Press Escape key
    await page.keyboard.press("Escape");

    // Panel should close
    await expect(panel).toHaveCount(0);
  });

  test("Escape does NOT close ContextPanel when search is open", async ({
    page,
  }) => {
    await page.waitForSelector('[data-testid="tool-fan"]', { timeout: 10000 });

    // Enter komposition mode to open panel
    await activateToolFanAction(page, "weave");
    const panel = page.locator('[data-testid="context-panel"]');
    await expect(panel).toBeVisible();

    // Open search
    await activateToolFanAction(page, "find");
    const searchOverlay = page.locator('[data-testid="search-overlay"]');
    await expect(searchOverlay).toBeVisible();

    // Press Escape key
    await page.keyboard.press("Escape");
    await expect(searchOverlay).toHaveCount(0);
    await expect(panel).toBeVisible();

    // Press Escape again
    await page.keyboard.press("Escape");
    await expect(panel).toHaveCount(0);
  });

  test("Escape does NOT close ContextPanel when filter is open", async ({
    page,
  }) => {
    await page.waitForSelector('[data-testid="tool-fan"]', { timeout: 10000 });
    await activateToolFanAction(page, "weave");
    const panel = page.locator('[data-testid="context-panel"]');
    await expect(panel).toBeVisible();
    await activateToolFanAction(page, "sight");
    const filterOverlay = page.locator('[data-testid="filter-overlay"]');
    await expect(filterOverlay).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(filterOverlay).toHaveCount(0);
    await expect(panel).toBeVisible();

    // Press Escape again
    await page.keyboard.press("Escape");
    await expect(panel).toHaveCount(0);
  });

  test("Clicking empty map area closes the context panel", async ({ page }) => {
    await page.waitForSelector(".map-marker", { timeout: 10000 });

    // Open panel first
    // using page.evaluate because map markers overlap with other invisible MapLibre overlay elements
    await page.evaluate(() => {
      (document.querySelector(".map-marker") as HTMLElement)?.dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
    });
    await expect(page.locator('[data-testid="context-panel"]')).toBeVisible();

    // Click empty map area (stabile Leerklick-Zone determined at 50,50)
    // MapLibre's canvas handles clicks
    await page
      .locator("canvas.maplibregl-canvas")
      .click({ position: { x: 50, y: 50 } });

    // Panel should close
    await expect(page.locator('[data-testid="context-panel"]')).toHaveCount(0);
  });

  test("Switching between markers resets the active tab", async ({ page }) => {
    await page.waitForSelector(".map-marker", { timeout: 10000 });

    // Open panel on first marker
    // using page.evaluate because map markers overlap with other invisible MapLibre overlay elements
    await page
      .locator(NODE_MARKER_SELECTOR)
      .first()
      .evaluate((marker) => {
        marker.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      });

    const panel = page.locator('[data-testid="context-panel"]');
    await expect(panel).toBeVisible();

    // Switch to the only productive secondary tab.
    const verlaufTab = panel.locator('button:has-text("Verlauf")');
    if (await verlaufTab.isVisible()) {
      await verlaufTab.click();
      await expect(verlaufTab).toHaveClass(/active/, { timeout: 5000 });

      // Click a different marker
      // using page.evaluate because map markers overlap with other invisible MapLibre overlay elements
      await page
        .locator(ACCOUNT_MARKER_SELECTOR)
        .first()
        .evaluate((marker) => {
          marker.dispatchEvent(new MouseEvent("click", { bubbles: true }));
        });

      // Harte Tab-Assertion: Die neue Selection sollte (falls Node) den Übersicht-Tab aktiv haben oder (falls Account) den Profil-Tab
      // Wir scopen auf das Panel und warten explizit auf den finalen DOM-Zustand, um Flakiness zu vermeiden.
      const activeTab = panel.locator("button.active");
      await expect(activeTab).toHaveText(/^(Übersicht|Profil)$/, {
        timeout: 5000,
      });
    }
  });

  test("Komposition mode initializes correctly from the tool fan", async ({
    page,
  }) => {
    await page.waitForSelector('[data-testid="tool-fan"]', { timeout: 10000 });

    // Click Weben in the tool fan
    await activateToolFanAction(page, "weave");

    // Context panel should open
    const panel = page.locator('[data-testid="context-panel"]');
    await expect(panel).toBeVisible();

    // Should show "Ort ausstehend" state
    await expect(panel.locator(".state-pending")).toContainText(
      "Ort ausstehend",
    );
  });

  test("Longpress on map initializes komposition mode with coordinates", async ({
    page,
  }) => {
    await page.waitForSelector(".map-marker", { timeout: 10000 });

    // Simulate longpress by dispatching mousedown and waiting
    const mapCanvas = page.locator("canvas.maplibregl-canvas");
    // Ensure we hover and click on empty space (50, 50)
    await mapCanvas.hover({ position: { x: 50, y: 50 } });
    await page.mouse.down();
    await page.waitForTimeout(1000); // 800ms is the longpress threshold
    await page.mouse.up();

    const panel = page.locator('[data-testid="context-panel"]');
    await panel.waitFor({ state: "visible", timeout: 5000 });
    await expect(panel.locator(".state-set")).toContainText("Ort gewählt");
  });

  test("Empty map click does not close context panel in komposition mode", async ({
    page,
  }) => {
    await page.waitForSelector('[data-testid="tool-fan"]', { timeout: 10000 });

    // Enter komposition mode via the tool fan
    await activateToolFanAction(page, "weave");
    const panel = page.locator('[data-testid="context-panel"]');
    await expect(panel).toBeVisible();

    // Click on an empty area of the map
    await page
      .locator("canvas.maplibregl-canvas")
      .click({ position: { x: 50, y: 50 } });

    // Panel should still be visible (komposition protection)
    await expect(panel).toBeVisible();
  });

  test("NodePanel keyboard navigation allows arrow keys, Home, and End", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1024, height: 768 });
    await page.waitForSelector(".map-marker", { timeout: 10000 });

    // Ensure we open a node. In our mock data, nodes usually have the title "Demo Node" or "Hamburg Workshop".
    // We can evaluate and click the first node marker.
    await page
      .locator(NODE_MARKER_SELECTOR)
      .first()
      .evaluate((marker) => {
        marker.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      });

    const panel = page.locator('[data-testid="context-panel"]');
    await expect(panel).toBeVisible();

    // Ensure we are in a NodePanel by checking for the 'Übersicht' tab
    const uebersichtTab = panel.locator('button[role="tab"]', {
      hasText: "Übersicht",
    });
    await expect(uebersichtTab).toBeVisible();

    const tabList = panel.getByRole("tablist", { name: "Knoten-Tabs" });
    const allTabs = tabList.getByRole("tab");
    await expect(allTabs).toHaveCount(5);
    const controlledPanelIds = await allTabs.evaluateAll((elements) =>
      elements.map((element) => element.getAttribute("aria-controls")),
    );
    expect(controlledPanelIds).toEqual([
      "panel-uebersicht",
      "panel-gespraech",
      "panel-verlauf",
      "panel-antraege",
      "panel-bearbeiten",
    ]);
    for (const panelId of controlledPanelIds) {
      expect(panelId).not.toBeNull();
      await expect(panel.locator(`#${panelId}`)).toHaveCount(1);
    }
    await expect(panel.locator("#panel-uebersicht")).toBeVisible();
    await expect(panel.locator("#panel-gespraech")).toBeHidden();
    await expect(panel.locator("#panel-verlauf")).toBeHidden();
    await expect(panel.locator("#panel-antraege")).toBeHidden();
    await expect(panel.locator("#panel-bearbeiten")).toBeHidden();

    // Focus the active tab (usually Übersicht by default)
    await uebersichtTab.focus();
    await expect(uebersichtTab).toBeFocused();
    await expect(uebersichtTab).toHaveAttribute("aria-selected", "true");
    await expect(uebersichtTab).toHaveAttribute(
      "aria-controls",
      "panel-uebersicht",
    );
    await expect(uebersichtTab).toHaveAttribute("tabindex", "0");
    await expect(panel.locator("#panel-uebersicht")).toHaveAttribute(
      "tabindex",
      "0",
    );

    // Press ArrowRight -> Should move through the three productive tabs in order.
    await page.keyboard.press("ArrowRight");
    const gespraechTab = panel.locator('button[role="tab"]', {
      hasText: "Gespräch",
    });
    await expect(gespraechTab).toBeFocused();
    await expect(gespraechTab).toHaveAttribute("aria-selected", "true");
    await expect(panel.locator("#panel-gespraech")).toBeVisible();

    // ArrowLeft from the middle tab proves reverse adjacent navigation.
    await page.keyboard.press("ArrowLeft");
    await expect(uebersichtTab).toBeFocused();
    await expect(uebersichtTab).toHaveAttribute("aria-selected", "true");

    await page.keyboard.press("ArrowRight");
    await expect(gespraechTab).toBeFocused();
    await page.keyboard.press("ArrowRight");
    const verlaufTab = panel.locator('button[role="tab"]', {
      hasText: "Verlauf",
    });
    await expect(verlaufTab).toBeFocused();
    await expect(verlaufTab).toHaveAttribute("aria-selected", "true");
    await expect(verlaufTab).toHaveAttribute("aria-controls", "panel-verlauf");
    await expect(verlaufTab).toHaveAttribute("tabindex", "0");
    await expect(uebersichtTab).toHaveAttribute("tabindex", "-1");
    await expect(panel.locator("#panel-verlauf")).toBeVisible();
    await expect(panel.locator("#panel-verlauf")).toHaveAttribute(
      "tabindex",
      "0",
    );

    const antraegeTab = panel.getByRole("tab", { name: "Anträge" });
    await page.keyboard.press("ArrowRight");
    await expect(antraegeTab).toBeFocused();
    await expect(antraegeTab).toHaveAttribute("aria-selected", "true");
    await expect(panel.locator("#panel-antraege")).toBeVisible();

    const bearbeitenTab = panel.getByRole("tab", { name: "Bearbeiten" });
    await page.keyboard.press("ArrowRight");
    await expect(bearbeitenTab).toBeFocused();
    await expect(bearbeitenTab).toHaveAttribute("aria-selected", "true");
    await expect(bearbeitenTab).toHaveAttribute(
      "aria-controls",
      "panel-bearbeiten",
    );
    await expect(bearbeitenTab).toHaveAttribute("tabindex", "0");
    await expect(antraegeTab).toHaveAttribute("tabindex", "-1");
    await expect(panel.locator("#panel-bearbeiten")).toBeVisible();

    // Full-size tab labels may extend beyond the panel. They remain intact,
    // horizontally scrollable and follow the keyboard's selected tab.
    await expect(tabList).toHaveCSS("overflow-x", "auto");
    await expect
      .poll(() =>
        bearbeitenTab.evaluate((element) => {
          const tab = element.getBoundingClientRect();
          const list = element.parentElement?.getBoundingClientRect();
          return (
            !!list && tab.left >= list.left - 1 && tab.right <= list.right + 1
          );
        }),
      )
      .toBe(true);
    await expect
      .poll(() =>
        bearbeitenTab.evaluate(
          (element) => element.scrollWidth <= element.clientWidth + 1,
        ),
      )
      .toBe(true);

    // Mobile hides tabs in the compact card. Open the full sheet before
    // checking that the same labels remain readable without ellipsis.
    await page.setViewportSize({ width: 320, height: 667 });
    await page.getByTestId("sheet-handle").click();
    await expect(panel).toHaveAttribute("data-sheet-stage", "full");
    await expect(tabList).toBeVisible();
    await expect(tabList).toHaveCSS("overflow-x", "auto");
    await expect
      .poll(() =>
        tabList
          .getByRole("tab")
          .evaluateAll((elements) =>
            elements.every(
              (element) => element.scrollWidth <= element.clientWidth + 1,
            ),
          ),
      )
      .toBe(true);

    // Opening the mobile sheet focuses its handle. Return focus to the last
    // tab before verifying the tablist's own wraparound behavior.
    await bearbeitenTab.focus();
    await expect(bearbeitenTab).toBeFocused();

    // ArrowRight from the last tab wraps to the first tab.
    await page.keyboard.press("ArrowRight");
    await expect(uebersichtTab).toBeFocused();
    await expect(uebersichtTab).toHaveAttribute("aria-selected", "true");

    // ArrowLeft from the first tab wraps back to the last tab.
    await page.keyboard.press("ArrowLeft");
    await expect(bearbeitenTab).toBeFocused();
    await expect(bearbeitenTab).toHaveAttribute("aria-selected", "true");

    // Reverse adjacent navigation returns from editing to node proposals.
    await page.keyboard.press("ArrowLeft");
    await expect(antraegeTab).toBeFocused();
    await expect(antraegeTab).toHaveAttribute("aria-selected", "true");

    // Home and End address the first and last productive tabs explicitly.
    await page.keyboard.press("Home");
    await expect(uebersichtTab).toBeFocused();
    await expect(uebersichtTab).toHaveAttribute("aria-selected", "true");
    await page.keyboard.press("End");
    await expect(bearbeitenTab).toBeFocused();
    await expect(bearbeitenTab).toHaveAttribute("aria-selected", "true");
  });

  test("NodePanel keeps the public creator on a typed creation event", async ({
    page,
  }) => {
    const nodeId = "b52be17c-4ab7-4434-98ce-520f86290cf0";
    const creatorId = "unlocated-public-account";
    let releaseAccountResponse!: () => void;
    const accountResponseBarrier = new Promise<void>((resolve) => {
      releaseAccountResponse = resolve;
    });
    await page.route(`**/api/accounts/${creatorId}`, async (route) => {
      await accountResponseBarrier;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: creatorId,
          title: "Unverortete Garnrolle",
          summary: "Öffentliche Garnrolle ohne Kartenposition",
          created_at: "2025-01-01T11:00:00Z",
          updated_at: "2025-01-01T11:00:00Z",
        }),
      });
    });
    await page.route(`**/api/nodes/${nodeId}`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: nodeId,
          kind: "Knoten",
          title: "fairschenkbox",
          summary: "Öffentliche Fair-Schenk-Box",
          location: { lat: 53.558894813662505, lon: 10.060228407382967 },
          created_at: "2025-01-01T12:00:00Z",
          updated_at: "2025-01-02T12:00:00Z",
          created_by_account_id: creatorId,
          created_by_account_current_title: "Unverortete Garnrolle",
          history: [
            {
              date: "2025-01-02T12:00:00Z",
              event: "Knoten aktualisiert.",
              kind: "updated",
            },
            {
              date: "2025-01-01T12:00:00Z",
              event: "Knoten wurde im Gewebe verankert.",
              kind: "created",
            },
          ],
        }),
      });
    });

    await page.waitForSelector(`.map-marker[data-id="${nodeId}"]`, {
      timeout: 10000,
    });
    await page
      .locator(`.map-marker[data-id="${nodeId}"]`)
      .evaluate((marker) => {
        marker.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      });

    const panel = page.locator(`[data-testid="context-panel"]`);
    await expect(panel).toBeVisible();
    await panel.getByRole("tab", { name: "Verlauf" }).click();

    await expect(
      panel.getByText("Knoten aktualisiert.", { exact: true }),
    ).toBeVisible();
    await expect(
      panel.getByText("Knoten wurde im Gewebe verankert.", { exact: true }),
    ).toHaveCount(1);
    const creatorButton = panel.getByRole("button", {
      name: "Garnrolle Unverortete Garnrolle öffnen",
    });
    await expect(creatorButton).toHaveText("Unverortete Garnrolle");

    await creatorButton.click();
    const accountHeading = panel.getByTestId("account-heading");
    await expect(accountHeading).toHaveText("Unverortete Garnrolle");
    await expect(accountHeading).toBeFocused();
    await expect(panel.getByText(creatorId, { exact: false })).toHaveCount(0);
    await expect(panel.getByRole("tab", { name: "Profil" })).toBeVisible();

    releaseAccountResponse();
    await expect(
      panel.getByText("Öffentliche Garnrolle ohne Kartenposition", {
        exact: true,
      }),
    ).toBeVisible();
    await expect(accountHeading).toHaveText("Unverortete Garnrolle");
  });

  test("NodePanel never exposes a creator id when no public title exists", async ({
    page,
  }) => {
    const nodeId = "b52be17c-4ab7-4434-98ce-520f86290cf0";
    const creatorId = "7d97a42e-3704-4a33-a61f-0e0a6b4d65d8";
    await page.route(`**/api/nodes/${nodeId}`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: nodeId,
          kind: "Knoten",
          title: "fairschenkbox",
          location: { lat: 53.558894813662505, lon: 10.060228407382967 },
          created_at: "2025-01-01T12:00:00Z",
          updated_at: "2025-01-01T12:00:00Z",
          created_by_account_id: creatorId,
          history: [
            {
              date: "2025-01-01T12:00:00Z",
              event: "Knoten wurde im Gewebe verankert.",
              kind: "created",
            },
          ],
        }),
      });
    });

    await page.waitForSelector(`.map-marker[data-id="${nodeId}"]`, {
      timeout: 10000,
    });
    await page
      .locator(`.map-marker[data-id="${nodeId}"]`)
      .evaluate((marker) => {
        marker.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      });

    const panel = page.locator(`[data-testid="context-panel"]`);
    await expect(panel).toBeVisible();
    await panel.getByRole("tab", { name: "Verlauf" }).click();

    await expect(
      panel.getByText("Knoten wurde im Gewebe verankert.", { exact: true }),
    ).toBeVisible();
    await expect(panel.getByText("Urheber:", { exact: true })).toHaveCount(0);
    await expect(panel.getByText(creatorId, { exact: false })).toHaveCount(0);
  });

  test("AccountPanel keyboard navigation allows arrow keys, Home, and End", async ({
    page,
  }) => {
    await page.waitForSelector(".map-marker", { timeout: 10000 });

    // Open an account. In our mock data, accounts usually use garnrolle markers.
    await page
      .locator(ACCOUNT_MARKER_SELECTOR)
      .first()
      .evaluate((marker) => {
        marker.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      });

    const panel = page.locator('[data-testid="context-panel"]');
    await expect(panel).toBeVisible();

    // Ensure we are in an AccountPanel by checking for the 'Profil' tab
    const profilTab = panel.locator('button[role="tab"]', {
      hasText: "Profil",
    });
    await expect(profilTab).toBeVisible();

    // Focus the active tab (usually Profil by default)
    await profilTab.focus();
    await expect(profilTab).toBeFocused();
    await expect(profilTab).toHaveAttribute("aria-selected", "true");

    // Press ArrowRight -> Should move to "Aktivität"
    await page.keyboard.press("ArrowRight");
    const aktivitaetTab = panel.locator('button[role="tab"]', {
      hasText: "Aktivität",
    });
    await expect(aktivitaetTab).toBeFocused();
    await expect(aktivitaetTab).toHaveAttribute("aria-selected", "true");
    await expect(panel.locator("#panel-aktivitaet")).toBeVisible();

    // Press End -> Should move to "Knoten"
    await page.keyboard.press("End");
    const knotenTab = panel.locator('button[role="tab"]', {
      hasText: "Knoten",
    });
    await expect(knotenTab).toBeFocused();
    await expect(knotenTab).toHaveAttribute("aria-selected", "true");
    await expect(panel.locator("#panel-knoten")).toBeVisible();

    // Press Home -> Should move back to "Profil"
    await page.keyboard.press("Home");
    await expect(profilTab).toBeFocused();
    await expect(profilTab).toHaveAttribute("aria-selected", "true");

    // Press ArrowLeft -> Should wrap around to "Knoten"
    await page.keyboard.press("ArrowLeft");
    await expect(knotenTab).toBeFocused();
    await expect(knotenTab).toHaveAttribute("aria-selected", "true");
  });
});
