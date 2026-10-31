import { expect, test } from "@playwright/test";

for (const [route, word] of [
  ["login", "Login"],
  ["settings", "Einstellungen"],
]) {
  test(`${route}: textile h1, manual themes and local images`, async ({
    page,
  }) => {
    await page.route("**/api/accounts", (route) => route.fulfill({ json: [] }));
    await page.emulateMedia({ colorScheme: "light" });
    await page.goto(`/${route}`);
    const heading = page.getByRole("heading", { name: word, level: 1 });
    await expect(heading).toHaveCount(1);
    const before = await heading.boundingBox();
    for (const theme of ["dark", "light"]) {
      await page.evaluate((theme) => {
        document.documentElement.dataset.colorScheme = theme;
      }, theme);
      const image = heading.locator(`img.${theme}`);
      await expect(image).toHaveAttribute(
        "src",
        `/garnschrift-c/${word.toLowerCase()}-${theme}.svg`,
      );
      await expect(image).toHaveClass(/loaded/);
      await expect(image).toHaveCSS("opacity", "1");
      await expect(heading.locator(".fallback")).toHaveCSS("opacity", "0");
      expect(await heading.boundingBox()).toEqual(before);
      const src = await image.getAttribute("src");
      expect(src).toBe(`/garnschrift-c/${word.toLowerCase()}-${theme}.svg`);
      expect(
        await image.evaluate((el: HTMLImageElement) => el.naturalWidth),
      ).toBeGreaterThan(0);
    }
  });

  test(`${route}: visible text when local images fail`, async ({ page }) => {
    await page.route("**/api/accounts", (route) => route.fulfill({ json: [] }));
    await page.route("**/garnschrift-c/**", (route) => route.abort());
    await page.goto(`/${route}`);
    const heading = page.getByRole("heading", { name: word, level: 1 });
    for (const theme of ["light", "dark"]) {
      await page.evaluate((theme) => {
        document.documentElement.dataset.colorScheme = theme;
      }, theme);
      await expect(heading.locator(".fallback")).toHaveCSS("opacity", "1");
      await expect(heading.locator(`img.${theme}`)).toHaveCSS("opacity", "0");
    }
  });
}

test("login still submits after loading the auth code on demand", async ({
  page,
}) => {
  await page.route("**/api/auth/magic-link/request", async (route) => {
    expect(route.request().postDataJSON()).toEqual({
      email: "weber@example.org",
    });
    await route.fulfill({ status: 204 });
  });
  await page.goto("/login");
  await page.getByLabel("E-Mail").fill("weber@example.org");
  await page.getByRole("button", { name: "Login-Link senden" }).click();
  await expect(page.getByRole("status")).toContainText("Postfach prüfen!");
});

test("forced colors preserves readable heading text", async ({ page }) => {
  await page.emulateMedia({ forcedColors: "active" });
  await page.goto("/login");
  const heading = page.getByRole("heading", { name: "Login", level: 1 });
  await expect(heading.locator(".fallback")).toHaveCSS("opacity", "1");
  await expect(heading.locator("img.light")).toHaveCSS("opacity", "0");
});
