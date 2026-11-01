import { expect, test } from "@playwright/test";

test("global yarn stays off by default, activates and persists across routes", async ({
  page,
}) => {
  const fontRequests: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes("/fonts/garnschrift-c-")) {
      fontRequests.push(request.url());
    }
  });
  await page.goto("/settings");
  await expect(page.locator("html")).toHaveAttribute(
    "data-wool-experiment",
    "off",
  );
  await expect(page.getByTestId("wool-experiment-toggle")).not.toBeChecked();
  expect(fontRequests).toEqual([]);

  await page.getByTestId("wool-experiment-toggle").check();
  await expect(page.locator("html")).toHaveAttribute(
    "data-wool-experiment",
    "on",
  );
  await expect(page.getByTestId("wool-experiment-toggle")).toBeChecked();
  await expect
    .poll(async () =>
      page
        .locator(".menu-links strong")
        .first()
        .evaluate((element) => getComputedStyle(element).fontFamily),
    )
    .toContain("Garnschrift C");
  expect(
    await page
      .locator(".intro")
      .evaluate((element) => getComputedStyle(element).fontFamily),
  ).not.toContain("Garnschrift C");
  await page.evaluate(async () => {
    await document.fonts.load('16px "Garnschrift C Petrol"');
  });
  expect(
    await page.evaluate(() =>
      document.fonts.check('16px "Garnschrift C Petrol"'),
    ),
  ).toBe(true);

  await page.goto("/login");
  await expect(page.locator("html")).toHaveAttribute(
    "data-wool-experiment",
    "on",
  );
  expect(
    await page
      .locator('input[type="email"]')
      .evaluate((element) => getComputedStyle(element).fontFamily),
  ).not.toContain("Garnschrift C");

  await page.goto("/settings");
  await expect(page.getByTestId("wool-experiment-toggle")).toBeChecked();
  await page.getByTestId("theme-select").selectOption("dark");
  await expect(page.locator("html")).toHaveAttribute(
    "data-color-scheme",
    "dark",
  );
  expect(
    await page
      .locator(".menu-links strong")
      .first()
      .evaluate((element) => getComputedStyle(element).fontFamily),
  ).toContain("Garnschrift C Woll");

  await page.getByTestId("wool-experiment-toggle").uncheck();
  await expect(page.locator("html")).toHaveAttribute(
    "data-wool-experiment",
    "off",
  );
  expect(
    await page
      .locator(".menu-links strong")
      .first()
      .evaluate((element) => getComputedStyle(element).fontFamily),
  ).not.toContain("Garnschrift C");
});

test("experiment URL opt-in/out is reversible and keeps semantic headings", async ({
  page,
}) => {
  await page.goto("/settings?wollschrift=1");
  await expect(page.getByTestId("wool-experiment-toggle")).toBeChecked();
  await expect(
    page.getByRole("heading", { name: "Einstellungen", level: 1 }),
  ).toHaveCount(1);
  await page.goto("/login");
  await expect(page.locator("html")).toHaveAttribute(
    "data-wool-experiment",
    "on",
  );
  await expect(
    page.getByRole("heading", { name: "Login", level: 1 }),
  ).toHaveCount(1);
  await page.goto("/settings?wollschrift=0");
  await expect(page.getByTestId("wool-experiment-toggle")).not.toBeChecked();
  await expect(page.locator("html")).toHaveAttribute(
    "data-wool-experiment",
    "off",
  );
});

test("unavailable experimental font leaves readable native HTML text", async ({
  page,
}) => {
  await page.route("**/fonts/garnschrift-c-*.woff", (route) => route.abort());
  await page.goto("/settings?wollschrift=1");
  const intro = page.locator(".intro");
  await expect(intro).toBeVisible();
  await expect(intro).toContainText("Verwalte deine Garnrolle");
  await expect(page.getByTestId("wool-experiment-toggle")).toBeChecked();
  await expect(
    page.getByRole("heading", { name: "Einstellungen", level: 1 }),
  ).toHaveCount(1);
});

test("filled primary actions retain contrast in both color schemes", async ({
  page,
}) => {
  for (const scheme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await page.goto("/login?wollschrift=1");
    await expect(page.locator("html")).toHaveAttribute(
      "data-color-scheme",
      scheme,
    );
    const button = page.getByRole("button", { name: "Login-Link senden" });
    await page.getByLabel("E-Mail").fill("sample@example.org");
    await expect(button).toBeEnabled();
    const buttonStyle = await button.evaluate((element) => {
      const css = getComputedStyle(element);
      return {
        font: css.fontFamily,
        background: css.backgroundColor,
        foreground: css.color,
      };
    });
    expect(buttonStyle.font).not.toContain("Garnschrift C");
    await expect
      .poll(async () =>
        page
          .getByRole("heading", { name: "Login", level: 1 })
          .evaluate((element) => getComputedStyle(element).fontFamily),
      )
      .toContain("Garnschrift C");
    expect(
      await page
        .locator(".wg-label")
        .evaluate((element) => getComputedStyle(element).fontFamily),
    ).not.toContain("Garnschrift C");

    const relativeLight = (rgb: string) => {
      const channels = rgb
        .match(/[0-9]+(?:\\.[0-9]+)?/g)
        ?.slice(0, 3)
        .map(Number);
      if (!channels || channels.length !== 3) return -1;
      const [red, green, blue] = channels.map((value) => {
        const normalized = value / 255;
        return normalized <= 0.04045
          ? normalized / 12.92
          : ((normalized + 0.055) / 1.055) ** 2.4;
      });
      return 0.2126 * red + 0.7152 * green + 0.0722 * blue;
    };
    const fg = relativeLight(buttonStyle.foreground);
    const bg = relativeLight(buttonStyle.background);
    expect(fg).toBeGreaterThanOrEqual(0);
    expect(bg).toBeGreaterThanOrEqual(0);
    expect(
      (Math.max(fg, bg) + 0.05) / (Math.min(fg, bg) + 0.05),
    ).toBeGreaterThanOrEqual(4.5);
  }
});

test("all filled primary links keep contrast in both themes", async ({
  page,
}) => {
  for (const scheme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await page.goto("/settings?wollschrift=1");
    const cta = page.getByRole("link", { name: "Login starten" });
    await expect(cta).toBeVisible();
    const style = await cta.evaluate((element) => {
      const computed = getComputedStyle(element);
      return {
        font: computed.fontFamily,
        foreground: computed.color,
        background: computed.backgroundColor,
      };
    });
    expect(style.font).not.toContain("Garnschrift C");
    const luminance = (rgb: string) => {
      const rgbValues = rgb
        .match(/[0-9]+/g)
        ?.slice(0, 3)
        .map(Number);
      if (!rgbValues || rgbValues.length !== 3) throw new Error("bad rgb");
      const components = rgbValues.map((v) => {
        const c = v / 255;
        return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
      });
      return (
        0.2126 * components[0] + 0.7152 * components[1] + 0.0722 * components[2]
      );
    };
    const foreground = luminance(style.foreground);
    const background = luminance(style.background);
    expect(
      (Math.max(foreground, background) + 0.05) /
        (Math.min(foreground, background) + 0.05),
    ).toBeGreaterThanOrEqual(4.5);
    expect(
      await page
        .locator(".menu-links strong")
        .first()
        .evaluate((element) => getComputedStyle(element).fontFamily),
    ).toContain("Garnschrift C");
    for (const selector of [
      ".intro",
      ".menu-hint",
      "#theme-select",
      ".back-link",
      ".btn",
      "button",
    ]) {
      expect(
        await page
          .locator(selector)
          .first()
          .evaluate((element) => getComputedStyle(element).fontFamily),
      ).not.toContain("Garnschrift C");
    }
  }
});

test("technical identifiers remain monospace during wool experiment", async ({
  page,
}) => {
  await page.goto("/federation?wollschrift=1");
  const sample = page.locator("code").first();
  await expect(sample).toBeVisible();
  const family = await sample.evaluate(
    (element) => getComputedStyle(element).fontFamily,
  );
  expect(family).toContain("monospace");
  expect(family).not.toContain("Garnschrift C");
});

test("forced colors switches back to the native readable system font", async ({
  page,
}) => {
  await page.emulateMedia({ forcedColors: "active" });
  await page.goto("/settings?wollschrift=1");
  await expect(page.getByTestId("wool-experiment-toggle")).toBeChecked();
  const font = await page
    .locator(".intro")
    .evaluate((element) => getComputedStyle(element).fontFamily);
  expect(font).not.toContain("Garnschrift C");
  await expect(
    page.getByRole("heading", {
      name: "Einstellungen",
      level: 1,
    }),
  ).toHaveCount(1);
});
