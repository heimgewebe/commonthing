import { test, expect, type Page } from "@playwright/test";
import { mockApiResponses } from "./fixtures/mockApi";

const NOT_ON_MAP_ACCOUNT_ID = "00000000-0000-0000-0000-000000000003";
const EXACT_ACCOUNT_ID = "7d97a42e-3704-4a33-a61f-0e0a6b4d65d8";
const MISSING_ACCOUNT_ID = "00000000-0000-0000-0000-000000000099";

async function gotoMapAsAccount(page: Page, accountId: string, role = "weber") {
  await mockApiResponses(page);
  await page.route("**/api/auth/me", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        authenticated: true,
        account_id: accountId,
        role,
      }),
    }),
  );
  await page.route("https://demotiles.maplibre.org/style.json", (route) => {
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ version: 8, sources: {}, layers: [] }),
    });
  });
  await page.goto("/map");
  await page.waitForSelector('[data-testid="tool-fan"]', { timeout: 10000 });
}

async function expectSettingsEntry(page: Page) {
  const trigger = page.getByTestId("site-menu-trigger");
  await expect(trigger).toBeVisible();
  await trigger.click();
  const link = page
    .getByRole("navigation", { name: "Hauptmenü" })
    .getByRole("link", { name: "Einstellungen", exact: true });
  await expect(link).toBeVisible();
  await expect(link).toHaveAttribute("href", "/settings");
}

async function openGarnrolleSettings(page: Page) {
  await page.getByTestId("site-menu-trigger").click();
  const settingsLink = page
    .getByRole("navigation", { name: "Hauptmenü" })
    .getByRole("link", { name: "Einstellungen", exact: true });
  await settingsLink.click();
  await expect(page).toHaveURL(/\/settings$/);

  const garnrolleLink = page
    .getByTestId("settings-menu")
    .getByRole("link", { name: /Meine Garnrolle/ });
  await expect(garnrolleLink).toHaveAttribute("href", "#meine-garnrolle");
  await garnrolleLink.click();
  await expect(page).toHaveURL(/\/settings#meine-garnrolle$/);
}

test.describe("Own settings entry", () => {
  test("uses the same settings entry when the Garnrolle is private", async ({
    page,
  }) => {
    await gotoMapAsAccount(page, NOT_ON_MAP_ACCOUNT_ID);
    await expectSettingsEntry(page);
  });

  test("keeps an existing private anchor separate from onboarding and public visibility", async ({
    page,
  }) => {
    await gotoMapAsAccount(page, NOT_ON_MAP_ACCOUNT_ID, "gast");
    await page.route("**/api/accounts/me/profile", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: NOT_ON_MAP_ACCOUNT_ID,
          title: "Bewusst private Garnrolle",
          summary: "Schon eingerichtet, aber nicht öffentlich verortet.",
          tags: ["account", "garnrolle"],
          address: "Private Ortsnotiz",
          location: { lat: 53.5, lon: 10.0 },
          map_state: "not_on_map",
          radius_m: 0,
        }),
      }),
    );

    await openGarnrolleSettings(page);
    const section = page.locator('[data-testid="my-garnrolle-section"]');
    await expect(
      section.locator('[data-testid="garnrolle-first-user-guide"]'),
    ).toHaveCount(0);
    await expect(
      section.locator('[data-testid="my-garnrolle-status"]'),
    ).toContainText("Privat");
    await expect(
      section.getByLabel("Privat – nicht öffentlich auf der Karte"),
    ).toBeChecked();
    await expect(
      section.locator('[data-testid="garnrolle-location-state"]'),
    ).toContainText("Privater Kartenanker gewählt");
    const mapLink = section.locator('[data-testid="my-garnrolle-map-link"]');
    await expect(mapLink).toHaveAttribute("href", "/map");
    await expect(mapLink).not.toHaveAttribute("aria-disabled");

    await section.getByLabel("Öffentlich exakt").check();
    await expect(
      section.locator('[data-testid="save-garnrolle"]'),
    ).toBeEnabled();
  });

  test("shows an unknown state and keeps saving disabled without an own Garnrolle record", async ({
    page,
  }) => {
    await gotoMapAsAccount(page, MISSING_ACCOUNT_ID, "gast");

    await openGarnrolleSettings(page);

    const section = page.locator('[data-testid="my-garnrolle-section"]');
    await expect(
      section.locator('[data-testid="my-garnrolle-status"]'),
    ).toContainText("Nicht verfügbar");
    await expect(
      section.locator('[data-testid="my-garnrolle-status"]'),
    ).toContainText("Garnrollen-Datensatz fehlt");
    await expect(
      section.locator('[data-testid="garnrolle-error"]'),
    ).toBeVisible();
    await expect(
      section.locator('[data-testid="save-garnrolle"]'),
    ).toBeDisabled();
  });

  test("does not add a redundant map action when the Garnrolle has a public position", async ({
    page,
  }) => {
    await gotoMapAsAccount(page, EXACT_ACCOUNT_ID);
    await expectSettingsEntry(page);
  });
});
