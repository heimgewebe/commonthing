import { expect, test } from "@playwright/test";
import { demoAccounts } from "../src/lib/demo/demoData";
import { mockApiResponses } from "./fixtures/mockApi";
import { waitForMapReady } from "./fixtures/mapReady";

const ACCOUNT_ID = "00000000-0000-0000-0000-000000000003";
const existing = demoAccounts[0];

test.describe("Garnrolle placement over occupied map anchors", () => {
  test.beforeEach(async ({ page }) => {
    await mockApiResponses(page, {
      auth: { authenticated: true, account_id: ACCOUNT_ID, role: "weber" },
    });
    await page.goto("/map?compose=garnrolle");
    await waitForMapReady(page);
  });

  test("fades existing markers and saves the exact publicly visible coordinate", async ({
    page,
  }) => {
    const publicPosition = existing.public_pos;
    if (!publicPosition)
      throw new Error("public exact Garnrolle fixture missing");
    await page.evaluate(({ lat, lon }) => {
      window.__TEST_MAP__?.jumpTo({ center: [lon, lat], zoom: 16 });
    }, publicPosition);

    const marker = page.getByTestId(`marker-garnrolle-${existing.id}`);
    await expect(marker).toBeVisible();
    await expect(page.locator("#map")).toHaveClass(/map-garnrolle-picking/);
    await expect(marker).toHaveCSS("opacity", "0.24");
    await expect(marker).toHaveCSS("pointer-events", "none");

    const xy = await page.evaluate(({ lat, lon }) => {
      const map = window.__TEST_MAP__ as unknown as
        | { project(point: [number, number]): { x: number; y: number } }
        | undefined;
      const point = map?.project([lon, lat]);
      if (!point) throw new Error("test map not exposed");
      return { x: point.x, y: point.y };
    }, publicPosition);
    const canvasBox = await page.locator("#map").boundingBox();
    if (!canvasBox) throw new Error("no map viewport");
    // A deliberate 9px offset inside the old Garnrolle artwork must snap to
    // the canonical public coordinate, not save the slightly shifted pixel.
    await page.mouse.click(canvasBox.x + xy.x + 9, canvasBox.y + xy.y - 7);
    const placement = page.getByTestId("garnrolle-placement");
    await expect(placement).toContainText("Kartenanker gewählt");
    await expect(page.getByTestId("context-panel")).not.toContainText(
      existing.title,
    );

    await page.getByTestId("confirm-garnrolle-location").click();
    await expect(page).toHaveURL(/\/settings#meine-garnrolle$/);
    const request = page.waitForRequest(
      (item) =>
        item.url().endsWith("/api/accounts/me/profile") &&
        item.method() === "PATCH",
    );
    await page.getByTestId("save-garnrolle").click();
    const payload = (await request).postDataJSON();
    expect(payload.location).toEqual(publicPosition);
  });

  test("normal map marker interaction returns when placement mode ends", async ({
    page,
  }) => {
    const marker = page.getByTestId(`marker-garnrolle-${existing.id}`);
    await expect(marker).toHaveCSS("pointer-events", "none");
    await page.goto("/map");
    await waitForMapReady(page);
    const active = page.getByTestId(`marker-garnrolle-${existing.id}`);
    await expect(active).toBeVisible();
    await expect(page.locator("#map")).not.toHaveClass(/map-garnrolle-picking/);
    await expect(active).not.toHaveCSS("pointer-events", "none");
  });
});
