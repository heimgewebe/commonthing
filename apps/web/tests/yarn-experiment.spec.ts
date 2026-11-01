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
      page.evaluate(() => {
        return getComputedStyle(document.querySelector(".intro")!).fontFamily;
      }),
    )
    .toContain("Garnschrift C");
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
  ).toContain("Garnschrift C");

  await page.goto("/settings");
  await expect(page.getByTestId("wool-experiment-toggle")).toBeChecked();
  await page.getByTestId("theme-select").selectOption("dark");
  await expect(page.locator("html")).toHaveAttribute(
    "data-color-scheme",
    "dark",
  );
  expect(
    await page
      .locator(".intro")
      .evaluate((element) => getComputedStyle(element).fontFamily),
  ).toContain("Garnschrift C Woll");

  await page.getByTestId("wool-experiment-toggle").uncheck();
  await expect(page.locator("html")).toHaveAttribute(
    "data-wool-experiment",
    "off",
  );
  expect(
    await page
      .locator(".intro")
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
          .locator(".wg-label")
          .evaluate((element) => getComputedStyle(element).fontFamily),
      )
      .toContain("Garnschrift C");

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
