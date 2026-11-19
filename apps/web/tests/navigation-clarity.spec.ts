import { expect, test } from "@playwright/test";
import { mockApiResponses } from "./fixtures/mockApi";

test("alte Wollschrift-Einstellungen bleiben wirkungslos, auch in Überschriften", async ({
  page,
}) => {
  await page.addInitScript(() => {
    localStorage.setItem("commonthing.wool-everywhere", "on");
  });
  await mockApiResponses(page);
  await page.goto("/settings?wollschrift=1");

  await expect(
    page.getByRole("heading", { name: "Einstellungen" }),
  ).toBeVisible();
  await expect(page.getByTestId("wool-experiment-toggle")).toHaveCount(0);
  await expect(page.locator("html")).not.toHaveAttribute(
    "data-wool-experiment",
    /.+/,
  );

  for (const selector of ["body", "h1", "#theme-select"]) {
    const family = await page
      .locator(selector)
      .first()
      .evaluate((element) => getComputedStyle(element).fontFamily);
    expect(family, selector).not.toContain("Garnschrift C");
  }

  await page.goto("/login?wollschrift=1");
  await expect(page.getByRole("heading", { name: "Anmelden" })).toBeVisible();
  const headingFamily = await page
    .locator("h1")
    .evaluate((element) => getComputedStyle(element).fontFamily);
  expect(headingFamily).not.toContain("Garnschrift C");
});

test("Hauptmenü führt verständlich zu Kartenfunktionen und Seiten", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockApiResponses(page, {
    auth: { authenticated: true, account_id: "weber-test", role: "weber" },
  });
  await page.goto("/map");
  const trigger = page.getByTestId("site-menu-trigger");
  await expect(trigger).toHaveText(/Menü/);
  await trigger.click();

  const menu = page.getByRole("navigation", { name: "Hauptmenü" });
  for (const [label, href] of [
    ["Karte", "/map"],
    ["Nachrichten", "/nachrichten"],
    ["Mein Profil (Garnrolle)", "/settings#meine-garnrolle"],
    ["Anträge und Entscheidungen", "/antraege"],
    ["Einstellungen", "/settings"],
  ]) {
    await expect(
      menu.getByRole("link", { name: label, exact: true }),
    ).toHaveAttribute("href", href);
  }

  await page.keyboard.press("Escape");
  await expect(page.getByTestId("site-menu")).not.toHaveAttribute("open");
  await expect(trigger).toBeFocused();
  await expect(page.getByTestId("tool-fan-trigger")).toContainText("Aktionen");
  await page.getByTestId("tool-fan-trigger").click();
  await expect(page.getByTestId("tool-fan-find")).toContainText("Suchen");
  await expect(page.getByTestId("tool-fan-map-content")).toContainText(
    "Filter",
  );
  await expect(page.getByTestId("tool-fan-weave")).toContainText("Erstellen");
});

for (const [width, height] of [
  [320, 480],
  [390, 540],
]) {
  test(`Geöffnetes Hauptmenü bleibt bei ${width}×${height} über der Kartenhilfe anklickbar`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height });
    await page.addInitScript(() => {
      localStorage.setItem("commonthing.map-welcome.dismissed.v1", "1");
    });
    await mockApiResponses(page, {
      auth: { authenticated: true, account_id: "weber-test", role: "weber" },
    });
    await page.goto("/map");
    await expect(
      page.getByRole("button", { name: "Karte kennenlernen" }),
    ).toBeVisible();

    await page.getByTestId("site-menu-trigger").click();
    const menu = page.getByRole("navigation", { name: "Hauptmenü" });
    const firstLink = menu.getByRole("link", { name: "Karte", exact: true });
    await expect(firstLink).toBeVisible();
    const onTop = await firstLink.evaluate((link) => {
      const bounds = link.getBoundingClientRect();
      const hit = document.elementFromPoint(
        bounds.left + bounds.width / 2,
        bounds.top + bounds.height / 2,
      );
      return hit === link || link.contains(hit);
    });
    expect(onTop).toBe(true);

    await menu.getByRole("link", { name: "Nachrichten" }).click();
    await expect(page).toHaveURL(/\/nachrichten(?:[?#]|$)/);
  });
}

for (const width of [320, 390, 510, 511, 768]) {
  test(
    "Hauptmenü und Gaststatus bleiben bei " + width + "px getrennt bedienbar",
    async ({ page }) => {
      await page.setViewportSize({ width, height: 720 });
      await mockApiResponses(page, {
        auth: { authenticated: true, account_id: "guest-test", role: "gast" },
      });
      await page.goto("/map");

      const menu = page.getByTestId("site-menu-trigger");
      const badge = page.getByTestId("topbar-guest-badge");
      await expect(menu).toBeVisible();
      await expect(badge).toBeVisible();

      const menuBox = await menu.boundingBox();
      const badgeBox = await badge.boundingBox();
      expect(menuBox).not.toBeNull();
      expect(badgeBox).not.toBeNull();
      expect(menuBox!.x).toBeGreaterThanOrEqual(0);
      expect(badgeBox!.x + badgeBox!.width).toBeLessThanOrEqual(width);
      expect(menuBox!.x + menuBox!.width).toBeLessThanOrEqual(badgeBox!.x + 1);

      await menu.click();
      await expect(
        page.getByRole("navigation", { name: "Hauptmenü" }),
      ).toBeVisible();
      const linkBox = await page
        .getByRole("navigation", { name: "Hauptmenü" })
        .getByRole("link", { name: "Anträge und Entscheidungen" })
        .boundingBox();
      expect(linkBox).not.toBeNull();
      expect(linkBox!.x).toBeGreaterThanOrEqual(0);
      expect(linkBox!.x + linkBox!.width).toBeLessThanOrEqual(width);
    },
  );
}
