import { test, expect, type Locator } from "@playwright/test";
import { mockApiResponses } from "./fixtures/mockApi";
import { centerDemoNode } from "./fixtures/mapCamera";

function overlap(
  a: NonNullable<Awaited<ReturnType<Locator["boundingBox"]>>>,
  b: NonNullable<Awaited<ReturnType<Locator["boundingBox"]>>>,
): boolean {
  return (
    a.x < b.x + b.width &&
    a.x + a.width > b.x &&
    a.y < b.y + b.height &&
    a.y + a.height > b.y
  );
}

test.describe("MapLibre control placement", () => {
  test.beforeEach(async ({ page }) => {
    // This suite measures controls for a returning authenticated user.
    // First-visit onboarding and its marker hitboxes are tested separately.
    await page.addInitScript(() =>
      localStorage.setItem("commonthing.map-welcome.dismissed.v1", "1"),
    );
    await mockApiResponses(page, {
      auth: { authenticated: true, account_id: "e2e-weber", role: "weber" },
    });
    await page.setViewportSize({ width: 1280, height: 800 });
    await page.goto("/map");
    await centerDemoNode(page);
    await page.waitForSelector(".map-marker", { timeout: 10000 });
  });

  test("zoom and attribution occupy stable opposite corners", async ({
    page,
  }) => {
    const zoom = page.locator(".maplibregl-ctrl-bottom-right").first();
    const attribution = page.locator(".maplibregl-ctrl-bottom-left").first();
    const trigger = page.getByTestId("tool-fan-trigger");
    const [zoomBox, attributionBox, triggerBox] = await Promise.all([
      zoom.boundingBox(),
      attribution.boundingBox(),
      trigger.boundingBox(),
    ]);

    expect(zoomBox).not.toBeNull();
    expect(attributionBox).not.toBeNull();
    expect(triggerBox).not.toBeNull();
    expect(overlap(zoomBox!, triggerBox!)).toBe(false);
    expect(overlap(attributionBox!, triggerBox!)).toBe(false);
    expect(zoomBox!.x).toBeGreaterThan(triggerBox!.x + triggerBox!.width);
    expect(attributionBox!.x + attributionBox!.width).toBeLessThan(
      triggerBox!.x,
    );
  });

  test("opening the centered tool fan does not move MapLibre controls", async ({
    page,
  }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    const zoom = page.locator(".maplibregl-ctrl-bottom-right").first();
    const attribution = page.locator(".maplibregl-ctrl-bottom-left").first();
    const baselineZoom = await zoom.boundingBox();
    const baselineAttribution = await attribution.boundingBox();

    await page.getByTestId("tool-fan-trigger").click();
    await expect(page.getByTestId("tool-fan")).toHaveAttribute(
      "data-expanded",
      "true",
    );
    expect(await zoom.boundingBox()).toEqual(baselineZoom);
    expect(await attribution.boundingBox()).toEqual(baselineAttribution);
  });

  test("fan branches and controls do not overlap", async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.getByTestId("tool-fan-trigger").click();

    const controlBoxes = await Promise.all([
      page.locator(".maplibregl-ctrl-bottom-right").first().boundingBox(),
      page.locator(".maplibregl-ctrl-bottom-left").first().boundingBox(),
    ]);
    expect(controlBoxes.every(Boolean)).toBe(true);

    for (const testId of [
      "tool-fan-find",
      "tool-fan-map-content",
      "tool-fan-weave",
    ]) {
      const branchBox = await page.getByTestId(testId).boundingBox();
      expect(branchBox).not.toBeNull();
      for (const controlBox of controlBoxes) {
        expect(
          overlap(controlBox!, branchBox!),
          `${testId} overlaps MapLibre controls`,
        ).toBe(false);
      }
    }
  });

  test("desktop focus panel shifts only the right-hand zoom controls", async ({
    page,
  }) => {
    const zoom = page.locator(".maplibregl-ctrl-bottom-right").first();
    const attribution = page.locator(".maplibregl-ctrl-bottom-left").first();
    const initialZoom = await zoom.boundingBox();
    const initialAttribution = await attribution.boundingBox();

    await page.locator(".map-marker").first().click();
    const panel = page.getByTestId("context-panel");
    await expect(panel).toBeVisible();
    const [panelBox, shiftedZoom, shiftedAttribution] = await Promise.all([
      panel.boundingBox(),
      zoom.boundingBox(),
      attribution.boundingBox(),
    ]);

    expect(panelBox).not.toBeNull();
    expect(shiftedZoom).not.toBeNull();
    expect(shiftedAttribution).not.toBeNull();
    expect(overlap(shiftedZoom!, panelBox!)).toBe(false);
    expect(shiftedZoom!.x).toBeLessThan(initialZoom!.x);
    expect(shiftedAttribution).toEqual(initialAttribution);
  });

  test("the centered tool fan stays inside a narrow remaining desktop map", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 800, height: 800 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.locator(".map-marker").first().click();
    const panelBox = await page.getByTestId("context-panel").boundingBox();
    expect(panelBox).not.toBeNull();

    await page.getByTestId("tool-fan-trigger").click();
    const toolMenuBox = await page.locator("#tool-fan-actions").boundingBox();
    expect(toolMenuBox).not.toBeNull();
    expect(toolMenuBox!.x).toBeGreaterThanOrEqual(0);
    expect(toolMenuBox!.x + toolMenuBox!.width).toBeLessThanOrEqual(
      panelBox!.x,
    );
  });

  test("Karteninhalt stays inside the remaining desktop map", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 800, height: 800 });
    await page.locator(".map-marker").first().click();
    const panel = page.getByTestId("context-panel");
    await expect(panel).toBeVisible();

    await page.getByTestId("tool-fan-trigger").click();
    await page.getByTestId("tool-fan-map-content").click();
    const lens = page.getByTestId("filter-overlay");
    await expect(lens).toBeVisible();

    const [lensBox, panelBox] = await Promise.all([
      lens.boundingBox(),
      panel.boundingBox(),
    ]);
    expect(lensBox).not.toBeNull();
    expect(panelBox).not.toBeNull();
    expect(lensBox!.x).toBeGreaterThanOrEqual(0);
    expect(lensBox!.x + lensBox!.width).toBeLessThanOrEqual(panelBox!.x);
  });

  for (const [viewportWidth, expectedPanelWidth] of [
    [1024, 320],
    [1200, 360],
    [1440, 400],
  ] as const) {
    test(`desktop context panel resolves clamp width at ${viewportWidth}px`, async ({
      page,
    }) => {
      await page.setViewportSize({ width: viewportWidth, height: 800 });
      await page.locator(".map-marker").first().click();

      const panel = page.getByTestId("context-panel");
      await expect(panel).toBeVisible();
      const panelBox = await panel.boundingBox();
      expect(panelBox).not.toBeNull();
      expect(
        Math.abs(panelBox!.width - expectedPanelWidth),
        `expected ${expectedPanelWidth}px context panel at ${viewportWidth}px viewport`,
      ).toBeLessThanOrEqual(2);
    });
  }

  test("mobile focus sheet moves both corner controls above the sheet", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.locator(".map-marker").first().click();
    const panel = page.getByTestId("context-panel");
    const zoom = page.locator(".maplibregl-ctrl-bottom-right").first();
    const attribution = page.locator(".maplibregl-ctrl-bottom-left").first();
    const [panelBox, zoomBox, attributionBox] = await Promise.all([
      panel.boundingBox(),
      zoom.boundingBox(),
      attribution.boundingBox(),
    ]);

    expect(panelBox).not.toBeNull();
    expect(zoomBox).not.toBeNull();
    expect(attributionBox).not.toBeNull();
    expect(zoomBox!.y + zoomBox!.height).toBeLessThan(panelBox!.y);
    expect(attributionBox!.y + attributionBox!.height).toBeLessThan(
      panelBox!.y,
    );
  });
});
