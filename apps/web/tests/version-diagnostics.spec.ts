import { test, expect } from "@playwright/test";

test.describe("Version diagnostics navigation", () => {
  test("keeps the settings route free of embedded diagnostics", async ({
    page,
  }) => {
    await page.goto("/settings");
    await expect(page.getByTestId("build-diagnostics-link")).toBeVisible();
    await expect(page.locator('[data-testid="version-text"]')).toHaveCount(0);
    await expect(page.getByText("Version unbekannt")).toHaveCount(0);
  });

  test("opens the existing full build diagnostics on demand", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.route("**/_app/version.json", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ version: "test-build" }),
      });
    });

    await page.goto("/settings");
    await page.getByTestId("build-diagnostics-link").click();
    await expect(page).toHaveURL(/\/build$/);
    await expect(
      page.getByRole("heading", { name: "Build-Diagnose", level: 1 }),
    ).toBeVisible();
    await expect(page.getByTestId("build-server-version")).toHaveText(
      "test-build",
    );
    const backToSettings = page.getByRole("link", {
      name: "← Zu den Einstellungen",
    });
    await expect(backToSettings).toBeVisible();
    expect((await backToSettings.boundingBox())?.height).toBeGreaterThanOrEqual(
      44,
    );
    await backToSettings.click();
    await expect(page).toHaveURL(/\/settings$/);
  });
});
