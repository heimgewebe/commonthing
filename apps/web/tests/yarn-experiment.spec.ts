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
