import { test, expect, type Page } from "@playwright/test";
import { mockApiResponses } from "./fixtures/mockApi";
import { activateToolFanAction } from "./fixtures/toolFan";
import {
  ACCOUNT_MARKER_SELECTOR,
  NODE_MARKER_SELECTOR,
  waitForNodeMarker,
} from "./fixtures/mapReady";

async function centerDemoMarkers(page: Page) {
  await page.waitForFunction(
    () => (window as any).__TEST_MAP__ !== undefined,
    undefined,
    { timeout: 15000 },
  );
  await page.evaluate(() => {
    const map = (window as any).__TEST_MAP__;
    map.jumpTo({ center: [10.060228407382967, 53.558894813662505], zoom: 14 });
  });
}

test.describe("ContextPanel mobile compact and full states", () => {
  test.beforeEach(async ({ page }) => {
    await mockApiResponses(page, {
      auth: { authenticated: true, account_id: "e2e-weber", role: "weber" },
    });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("/map");
    await centerDemoMarkers(page);
    await waitForNodeMarker(page);
  });

  test("Fokus opens as a compact card without mobile tabs or size buttons", async ({
    page,
  }) => {
    await page.locator(NODE_MARKER_SELECTOR).first().click();

    const panel = page.getByTestId("context-panel");
    const handle = page.getByTestId("sheet-handle");
    await expect(panel).toBeVisible();
    await expect(panel.locator(".panel-header h2")).toHaveCount(1);
    await expect(panel).toHaveAttribute("data-sheet-stage", "compact");
    await expect(handle).toHaveAttribute("aria-expanded", "false");
    await expect(
      panel.getByRole("region", { name: "Knotenübersicht" }),
    ).toBeVisible();
    await expect(panel.locator(".tabs")).toBeHidden();
    await expect(panel.getByRole("tab")).toHaveCount(0);
    await expect(page.getByLabel("Panelgröße")).toHaveCount(0);
    expect((await handle.boundingBox())?.height).toBeGreaterThanOrEqual(44);
  });

  test("handle and panel title toggle directly between compact and full", async ({
    page,
  }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.locator(NODE_MARKER_SELECTOR).first().click();

    const panel = page.getByTestId("context-panel");
    const handle = page.getByTestId("sheet-handle");
    const title = panel.locator(".mobile-panel-title");
    const compactBox = await panel.boundingBox();

    await handle.click();
    await expect(panel).toHaveAttribute("data-sheet-stage", "full");
    await expect(handle).toHaveAttribute("aria-expanded", "true");
    await expect(panel.locator(".tabs")).toBeVisible();
    const fullBox = await panel.boundingBox();
    expect(fullBox!.height).toBeGreaterThan(compactBox!.height);

    await title.click();
    await expect(panel).toHaveAttribute("data-sheet-stage", "compact");
    await expect(panel.locator(".tabs")).toBeHidden();
  });

  test("compact mode always shows the summary instead of the previously selected tab", async ({
    page,
  }) => {
    await page.locator(NODE_MARKER_SELECTOR).first().click();
    const panel = page.getByTestId("context-panel");
    const handle = page.getByTestId("sheet-handle");

    await handle.click();
    await panel.getByRole("tab", { name: "Gespräch" }).click();
    await expect(panel.locator("#panel-gespraech")).toBeVisible();

    await panel.locator(".mobile-panel-title").click();
    await expect(panel).toHaveAttribute("data-sheet-stage", "compact");
    await expect(panel.getByTestId("node-compact-summary")).toBeVisible();
    await expect(panel.locator("#panel-gespraech")).toHaveCount(1);
    await expect(panel.locator("#panel-gespraech")).toBeHidden();

    await handle.click();
    await expect(panel.getByTestId("node-compact-summary")).toBeHidden();
    await expect(panel.locator("#panel-gespraech")).toBeVisible();
    await expect(panel.getByRole("tab", { name: "Gespräch" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  test("collapsing preserves an unsaved edit draft", async ({ page }) => {
    await page.locator(NODE_MARKER_SELECTOR).first().click();
    const panel = page.getByTestId("context-panel");
    const handle = page.getByTestId("sheet-handle");

    await handle.click();
    await panel.getByRole("tab", { name: "Bearbeiten" }).click();
    await panel
      .getByRole("button", { name: "Bearbeiten", exact: true })
      .click();
    const titleInput = panel.getByLabel("Titel");
    await titleInput.fill("Ungespeicherter Entwurf");

    await panel.locator(".mobile-panel-title").click();
    await expect(panel).toHaveAttribute("data-sheet-stage", "compact");
    await expect(titleInput).toBeHidden();
    await expect(panel.getByTestId("node-compact-summary")).toBeVisible();

    await handle.click();
    await expect(titleInput).toBeVisible();
    await expect(titleInput).toHaveValue("Ungespeicherter Entwurf");
  });

  test("Komposition starts full and can collapse through the same handle", async ({
    page,
  }) => {
    await page.waitForSelector('[data-testid="tool-fan"]', {
      timeout: 10000,
    });
    await activateToolFanAction(page, "weave");

    const panel = page.getByTestId("context-panel");
    await expect(panel).toBeVisible();
    await expect(panel).toHaveAttribute("data-sheet-stage", "full");
    await page.getByTestId("sheet-handle").click();
    await expect(panel).toHaveAttribute("data-sheet-stage", "compact");
  });

  test("keyboard, taps and dragging change state exactly once", async ({
    page,
  }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.locator(NODE_MARKER_SELECTOR).first().click();
    const panel = page.getByTestId("context-panel");
    const handle = page.getByTestId("sheet-handle");

    await handle.focus();
    await handle.press("Enter");
    await expect(panel).toHaveAttribute("data-sheet-stage", "full");
    await handle.press(" ");
    await expect(panel).toHaveAttribute("data-sheet-stage", "compact");

    await handle.press("ArrowUp");
    await expect(panel).toHaveAttribute("data-sheet-stage", "full");
    await handle.press("ArrowDown");
    await expect(panel).toHaveAttribute("data-sheet-stage", "compact");
    await handle.press("End");
    await expect(panel).toHaveAttribute("data-sheet-stage", "full");
    await handle.press("Home");
    await expect(panel).toHaveAttribute("data-sheet-stage", "compact");

    const box = await handle.boundingBox();
    expect(box).not.toBeNull();
    await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2);
    await page.mouse.down();
    await page.mouse.move(box!.x + box!.width / 2, box!.y - 360, { steps: 8 });
    await page.mouse.up();
    await expect(panel).toHaveAttribute("data-sheet-stage", "full");
    await handle.dispatchEvent("click", { detail: 1 });
    await expect(panel).toHaveAttribute("data-sheet-stage", "full");
  });

  // Die Ziehhoehe haengt an einer style:-Direktive, die die Eigenschaft ueber
  // das CSSOM setzt und wieder entfernt. Das style-Attribut bleibt danach als
  // leeres Attribut stehen, also wird hier die Eigenschaft geprueft.
  test("non-primary and secondary pointers cannot start a sheet drag", async ({
    page,
  }) => {
    await page.locator(NODE_MARKER_SELECTOR).first().click();
    const panel = page.getByTestId("context-panel");
    const handle = page.getByTestId("sheet-handle");

    for (const event of [
      { pointerId: 40, pointerType: "touch", isPrimary: false, button: 0 },
      { pointerId: 41, pointerType: "mouse", isPrimary: true, button: 1 },
      { pointerId: 42, pointerType: "mouse", isPrimary: true, button: 2 },
    ]) {
      await handle.dispatchEvent("pointerdown", {
        ...event,
        clientY: 700,
      });
      await expect(panel).not.toHaveClass(/dragging/);
      await expect(panel).toHaveAttribute("data-sheet-stage", "compact");
      await expect(panel).toHaveJSProperty("style.height", "");
    }
  });

  test("foreign pointer ids and pointercancel leave no drag state", async ({
    page,
  }) => {
    await page.locator(NODE_MARKER_SELECTOR).first().click();
    const panel = page.getByTestId("context-panel");
    const handle = page.getByTestId("sheet-handle");
    const box = await handle.boundingBox();
    expect(box).not.toBeNull();

    await handle.evaluate((element) => {
      element.addEventListener(
        "pointerdown",
        (event) =>
          element.setAttribute(
            "data-test-pointer-id",
            String((event as PointerEvent).pointerId),
          ),
        { once: true },
      );
    });
    await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2);
    await page.mouse.down();
    const pointerId = Number(await handle.getAttribute("data-test-pointer-id"));
    expect(Number.isFinite(pointerId)).toBe(true);
    await expect(panel).toHaveClass(/dragging/);

    await handle.dispatchEvent("pointermove", {
      pointerId: pointerId + 1,
      pointerType: "mouse",
      isPrimary: true,
      clientY: box!.y - 300,
    });
    await handle.dispatchEvent("pointerup", {
      pointerId: pointerId + 1,
      pointerType: "mouse",
      isPrimary: true,
      button: 0,
    });
    await expect(panel).toHaveClass(/dragging/);
    await expect(panel).toHaveAttribute("data-sheet-stage", "compact");

    await handle.dispatchEvent("pointercancel", {
      pointerId,
      pointerType: "mouse",
      isPrimary: true,
      button: 0,
    });
    await expect(panel).not.toHaveClass(/dragging/);
    await expect(panel).toHaveJSProperty("style.height", "");
    await page.mouse.up();
  });

  test("lostpointercapture deterministically clears an active drag", async ({
    page,
  }) => {
    await page.locator(NODE_MARKER_SELECTOR).first().click();
    const panel = page.getByTestId("context-panel");
    const handle = page.getByTestId("sheet-handle");
    const box = await handle.boundingBox();
    expect(box).not.toBeNull();

    await handle.evaluate((element) => {
      element.addEventListener(
        "pointerdown",
        (event) =>
          element.setAttribute(
            "data-test-pointer-id",
            String((event as PointerEvent).pointerId),
          ),
        { once: true },
      );
    });
    await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2);
    await page.mouse.down();
    const pointerId = Number(await handle.getAttribute("data-test-pointer-id"));
    await expect(panel).toHaveClass(/dragging/);

    await handle.dispatchEvent("lostpointercapture", {
      pointerId,
      pointerType: "mouse",
      isPrimary: true,
    });
    await expect(panel).not.toHaveClass(/dragging/);
    await expect(panel).toHaveJSProperty("style.height", "");
    await page.mouse.up();
  });

  test("compact Garnrolle keeps tabs and the selected panel in the DOM", async ({
    page,
  }) => {
    await page.locator(ACCOUNT_MARKER_SELECTOR).first().click();
    const panel = page.getByTestId("context-panel");
    const handle = page.getByTestId("sheet-handle");

    await expect(
      panel.getByRole("region", { name: "Garnrollenprofil" }),
    ).toBeVisible();
    await expect(panel.locator('[role="tab"]')).toHaveCount(3);
    await expect(panel.getByRole("tab")).toHaveCount(0);

    await handle.click();
    await panel.getByRole("tab", { name: "Aktivität" }).click();
    await expect(panel.locator("#panel-aktivitaet")).toBeVisible();

    await panel.locator(".mobile-panel-title").click();
    await expect(panel.getByTestId("account-compact-summary")).toBeVisible();
    await expect(panel.locator('[role="tablist"]')).toBeHidden();
    await expect(panel.locator("#panel-aktivitaet")).toHaveCount(1);
    await expect(panel.locator("#panel-aktivitaet")).toBeHidden();

    await handle.click();
    await expect(panel.getByTestId("account-compact-summary")).toBeHidden();
    await expect(panel.locator("#panel-aktivitaet")).toBeVisible();
    await expect(panel.getByRole("tab", { name: "Aktivität" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  test("reduced motion removes the height transition", async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.locator(NODE_MARKER_SELECTOR).first().click();
    const transitionDuration = await page
      .getByTestId("context-panel")
      .evaluate((element) => getComputedStyle(element).transitionDuration);
    expect(transitionDuration).toBe("0s");
  });

  test("rotating a coarse-pointer phone keeps the live panel mobile", async ({
    browser,
  }) => {
    const context = await browser.newContext({
      viewport: { width: 390, height: 844 },
      hasTouch: true,
      isMobile: true,
    });
    const landscapePage = await context.newPage();
    await mockApiResponses(landscapePage, {
      auth: { authenticated: true, account_id: "e2e-weber", role: "weber" },
    });
    await landscapePage.goto("/map");
    await centerDemoMarkers(landscapePage);
    await waitForNodeMarker(landscapePage);
    expect(
      await landscapePage.evaluate(
        () => window.matchMedia("(pointer: coarse)").matches,
      ),
    ).toBe(true);

    await landscapePage.locator(NODE_MARKER_SELECTOR).first().click();
    const panel = landscapePage.getByTestId("context-panel");
    const handle = landscapePage.getByTestId("sheet-handle");
    await expect(handle).toBeVisible();
    await expect(panel).toHaveAttribute("data-sheet-stage", "compact");
    await expect(panel.getByTestId("node-compact-summary")).toBeVisible();

    await landscapePage.setViewportSize({ width: 844, height: 390 });
    await expect(handle).toBeVisible();
    await expect(panel).toHaveAttribute("data-sheet-stage", "compact");
    await expect(panel.getByTestId("node-compact-summary")).toBeVisible();
    await handle.click();
    await expect(panel).toHaveAttribute("data-sheet-stage", "full");

    const box = await panel.boundingBox();
    expect(box).not.toBeNull();
    expect(Math.round(box!.width)).toBe(844);
    expect(box!.y + box!.height).toBeLessThanOrEqual(390);
    await context.close();
  });

  test("switching the selected marker resets the sheet to compact", async ({
    page,
  }) => {
    await page.locator(NODE_MARKER_SELECTOR).first().click();
    const panel = page.getByTestId("context-panel");
    await page.getByTestId("sheet-handle").click();
    await expect(panel).toHaveAttribute("data-sheet-stage", "full");

    await page
      .locator(ACCOUNT_MARKER_SELECTOR)
      .first()
      .evaluate((marker) => {
        marker.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      });
    await expect(panel).toHaveAttribute("data-sheet-stage", "compact");
  });
});

test.describe("ContextPanel desktop layout stays unchanged", () => {
  test.beforeEach(async ({ page }) => {
    await mockApiResponses(page, {
      auth: { authenticated: true, account_id: "e2e-weber", role: "weber" },
    });
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto("/map");
    await centerDemoMarkers(page);
    await waitForNodeMarker(page);
  });

  test("the sheet affordances stay hidden and tabs retain their desktop behavior", async ({
    page,
  }) => {
    await page.locator(NODE_MARKER_SELECTOR).first().click();
    const panel = page.getByTestId("context-panel");
    await expect(panel).toBeVisible();
    const handle = page.getByTestId("sheet-handle");
    await expect(handle).toBeHidden();
    await expect(panel.locator(".mobile-panel-title")).toBeHidden();
    await expect(panel.locator(".tabs")).toBeVisible();
    await handle.dispatchEvent("click", { detail: 0 });
    await expect(panel).toHaveAttribute("data-sheet-stage", "compact");

    const box = await panel.boundingBox();
    expect(Math.round(box!.width)).toBe(400);
  });
});

test.describe("ContextPanel touch input", () => {
  test.use({ hasTouch: true, viewport: { width: 390, height: 844 } });

  test.beforeEach(async ({ page }) => {
    await mockApiResponses(page, {
      auth: { authenticated: true, account_id: "e2e-weber", role: "weber" },
    });
    await page.goto("/map");
    await centerDemoMarkers(page);
    await waitForNodeMarker(page);
  });

  test("one real touch tap toggles the handle exactly once", async ({
    page,
  }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.locator(NODE_MARKER_SELECTOR).first().click();
    const panel = page.getByTestId("context-panel");
    const handle = page.getByTestId("sheet-handle");
    const box = await handle.boundingBox();
    expect(box).not.toBeNull();

    await page.touchscreen.tap(
      box!.x + box!.width / 2,
      box!.y + box!.height / 2,
    );

    await expect(panel).toHaveAttribute("data-sheet-stage", "full");
  });
});
