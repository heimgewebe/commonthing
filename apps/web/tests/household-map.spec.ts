import { expect, test } from "@playwright/test";
import { demoAccounts } from "../src/lib/demo/demoData";
import { mockApiResponses, mockListResponse } from "./fixtures/mockApi";
import { waitForMapReady } from "./fixtures/mapReady";

// Every member has the same explicitly public, exact map coordinate. An
// imprecise position may coincide on the map but must never join the house.
const HOUSE_ACCOUNTS = Array.from({ length: 20 }, (_, index) => ({
  ...demoAccounts[0],
  id: `10000000-0000-4000-8000-${String(index + 1).padStart(12, "0")}`,
  title: `Hausbewohner ${index + 1}`,
  map_state: "exact" as const,
}));
const RADIUS_ACCOUNT = {
  ...demoAccounts[1],
  public_pos: HOUSE_ACCOUNTS[0].public_pos,
};

test.describe("household map view", () => {
  test.beforeEach(async ({ page }) => {
    await mockApiResponses(page);
    await page.route("**/api/accounts*", async (route) => {
      if (new URL(route.request().url()).pathname !== "/api/accounts") {
        await route.fallback();
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(
          mockListResponse(route.request().url(), [
            RADIUS_ACCOUNT,
            ...HOUSE_ACCOUNTS,
          ]),
        ),
      });
    });
    await page.goto("/map");
    await waitForMapReady(page);
  });

  test("opens the exact-location stack with keyboard and keeps radius separate", async ({
    page,
  }) => {
    const frontId = HOUSE_ACCOUNTS[19].id;
    const front = page.getByTestId(`marker-garnrolle-${frontId}`);
    await expect(front).toBeVisible();
    await expect(front).toHaveAttribute("data-household-count", "20");
    await expect(
      page.getByTestId(`marker-garnrolle-${HOUSE_ACCOUNTS[0].id}`),
    ).toHaveCount(0);
    await expect(
      page.getByTestId(`marker-garnrolle-${RADIUS_ACCOUNT.id}`),
    ).toHaveCount(1);

    await front.focus();
    await page.keyboard.press("Alt+Enter");
    const close = page.getByTestId("close-household-view");
    const pager = page.getByTestId("household-page-controls");
    await expect(close).toBeVisible();
    await expect(pager).toBeVisible();
    await expect(pager).toContainText("Seite 2 von 2");

    await pager.getByRole("button", { name: "Zurück" }).click();
    await expect(pager).toContainText("Seite 1 von 2");
    const firstMember = page.getByTestId(
      `marker-garnrolle-${HOUSE_ACCOUNTS[0].id}`,
    );
    await expect(firstMember).toBeVisible();
    await firstMember.click();
    await expect(page.locator('[data-testid="context-panel"]')).toBeVisible();

    await close.click();
    await expect(close).toHaveCount(0);
    await expect(pager).toHaveCount(0);
    await expect(firstMember).toHaveAttribute("data-household-count", "20");
  });

  test("opens by holding the visible stack without opening composition", async ({
    page,
  }) => {
    const front = page.getByTestId(`marker-garnrolle-${HOUSE_ACCOUNTS[19].id}`);
    await expect(front).toBeVisible();
    // "Visible" alone also matches MapLibre markers positioned outside the
    // viewport. A physical mouse/touch press must hit the actual map marker.
    const publicPosition = HOUSE_ACCOUNTS[0].public_pos;
    if (!publicPosition)
      throw new Error("household fixture has no public position");
    await page.evaluate(({ lon, lat }) => {
      const map = (
        window as typeof window & {
          __TEST_MAP__?: {
            jumpTo(options: { center: [number, number]; zoom: number }): void;
          };
        }
      ).__TEST_MAP__;
      map?.jumpTo({ center: [lon, lat], zoom: 16 });
    }, publicPosition);
    await expect(front).toBeInViewport({ ratio: 0.8 });
    const box = await front.boundingBox();
    expect(box).toBeTruthy();
    await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2);
    await page.mouse.down();
    await expect(page.getByTestId("close-household-view")).toBeVisible({
      timeout: 3_000,
    });
    await page.mouse.up();
    await expect(page.locator('[data-testid="context-panel"]')).toHaveCount(0);
  });
});
