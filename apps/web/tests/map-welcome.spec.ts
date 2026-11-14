import { expect, test } from "@playwright/test";
import { mockApiResponses } from "./fixtures/mockApi";

const key = "commonthing.map-welcome.dismissed.v1";

test("first visit: dismiss, recall, Escape and persistence", async ({
  page,
}) => {
  await mockApiResponses(page);
  await page.goto("/map");
  const welcome = page.getByTestId("map-welcome");
  await expect(welcome).toBeVisible();
  await expect(welcome).toContainText("Garnrolle");
  await expect(welcome.getByRole("link", { name: /Anbieten/ })).toHaveAttribute(
    "href",
    "/login",
  );
  await expect(
    welcome.getByRole("link", { name: /Mitmachen/ }),
  ).toHaveAttribute("href", "/antraege");
  await welcome.getByRole("button", { name: "Einführung schließen" }).click();
  const recall = page.getByRole("button", { name: "Karte kennenlernen" });
  await expect(recall).toBeFocused();
  await recall.press("Enter");
  await welcome.getByRole("button", { name: /Finden/ }).focus();
  await page.keyboard.press("Escape");
  await expect(recall).toBeFocused();
  await page.reload();
  await expect(welcome).toHaveCount(0);
  await expect(recall).toBeVisible();
  expect(await page.evaluate((k) => localStorage.getItem(k), key)).toBe("1");
});

for (const width of [320, 390]) {
  for (const colorScheme of ["light", "dark"] as const) {
    test(`search handoff and layout ${width} ${colorScheme}`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height: 640 });
      await page.emulateMedia({ colorScheme });
      await mockApiResponses(page);
      await page.goto("/map");
      const welcome = page.getByTestId("map-welcome");
      await expect(welcome).toBeVisible();
      const box = await welcome.boundingBox();
      expect(box!.x).toBeGreaterThanOrEqual(0);
      expect(box!.x + box!.width).toBeLessThanOrEqual(width);
      expect(box!.y).toBeGreaterThanOrEqual(52);
      for (const selector of [
        ".topbar",
        ".maplibregl-ctrl-bottom-right",
        "[data-testid=tool-fan-trigger]",
      ]) {
        const other = await page.locator(selector).first().boundingBox();
        expect(other).not.toBeNull();
        const overlaps =
          box!.x < other!.x + other!.width &&
          box!.x + box!.width > other!.x &&
          box!.y < other!.y + other!.height &&
          box!.y + box!.height > other!.y;
        expect(overlaps, `introduction overlaps ${selector}`).toBe(false);
      }
      for (const target of await welcome.locator("button, a").all()) {
        expect((await target.boundingBox())!.height).toBeGreaterThanOrEqual(44);
      }
      await welcome.getByRole("button", { name: /Finden/ }).click();
      await expect(welcome).toHaveCount(0);
      await expect(page.getByTestId("search-overlay")).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(
        page.getByRole("button", { name: "Karte kennenlernen" }),
      ).toBeFocused();
    });
  }
}

test("authenticated offering opens the existing composition", async ({
  page,
}) => {
  await mockApiResponses(page, {
    auth: { authenticated: true, account_id: "e2e-weber", role: "weber" },
  });
  await page.goto("/map");
  await page
    .getByTestId("map-welcome")
    .getByRole("button", { name: /Anbieten/ })
    .click();
  await expect(page.getByTestId("context-panel")).toBeVisible();
  await expect(
    page.getByTestId("context-panel").locator(".panel-header h2"),
  ).toHaveText("Knoten knüpfen");
  await expect(page.getByTestId("map-welcome")).toHaveCount(0);
});

test("storage failure and existing tools do not block the map", async ({
  page,
}) => {
  await page.addInitScript(() => {
    Storage.prototype.getItem = () => {
      throw new Error("storage unavailable");
    };
    Storage.prototype.setItem = () => {
      throw new Error("storage unavailable");
    };
  });
  await mockApiResponses(page);
  await page.goto("/map");
  await expect(page.getByTestId("map-welcome")).toBeVisible();
  await page.getByTestId("tool-fan-trigger").click();
  await expect(page.getByTestId("map-welcome")).toHaveCount(0);
  await page.keyboard.press("Escape");
  await expect(
    page.getByRole("button", { name: "Karte kennenlernen" }),
  ).toBeVisible();
});

test("deep-linked search has priority over the introduction", async ({
  page,
}) => {
  await mockApiResponses(page);
  await page.goto("/map?lens=search");
  await expect(page.getByTestId("search-overlay")).toBeVisible();
  await expect(page.getByTestId("map-welcome")).toHaveCount(0);
});

test("anonymous offering reaches the real login page", async ({ page }) => {
  await mockApiResponses(page);
  await page.goto("/map");
  await page
    .getByTestId("map-welcome")
    .getByRole("link", { name: /Anbieten/ })
    .click();
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByTestId("login-page")).toBeVisible();
  await page.goBack();
  await expect(page.getByTestId("map-welcome")).toBeVisible();
});

test("participation opens the existing decisions page", async ({ page }) => {
  await mockApiResponses(page);
  await page.goto("/map");
  await page
    .getByTestId("map-welcome")
    .getByRole("link", { name: /Mitmachen/ })
    .click();
  await expect(page).toHaveURL(new RegExp("/antraege$"));
  await expect(page.getByTestId("applications-page")).toBeVisible();
});

test("map interaction dismisses without returning or taking focus", async ({
  page,
}) => {
  await mockApiResponses(page);
  await page.goto("/map");
  await expect(page.getByTestId("map-welcome")).toBeVisible();
  await page.locator("#map").click({ position: { x: 20, y: 120 } });
  await expect(page.getByTestId("map-welcome")).toHaveCount(0);
  const recall = page.getByRole("button", { name: "Karte kennenlernen" });
  await expect(recall).not.toBeFocused();
  await recall.click();
  await expect(
    page.getByTestId("map-welcome").getByRole("button", { name: /Finden/ }),
  ).toBeFocused();
});
