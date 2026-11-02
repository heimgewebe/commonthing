import { expect, test } from "@playwright/test";

test("wool is the default and persists across routes", async ({ page }) => {
  const fontRequests: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes("/fonts/garnschrift-c-")) {
      fontRequests.push(request.url());
    }
  });
  await page.goto("/settings");
  await expect(page.locator("html")).toHaveAttribute(
    "data-wool-experiment",
    "on",
  );
  await expect(page.getByTestId("wool-experiment-toggle")).toBeChecked();
  await expect.poll(() => fontRequests.length).toBeGreaterThan(0);
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
  ).toContain("Garnschrift C");
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
    expect(buttonStyle.font).toContain("Garnschrift C");
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
    ).toContain("Garnschrift C");

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
    expect(style.font).toContain("Garnschrift C");
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
      ).toContain("Garnschrift C");
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

test("wool navigation uses legible optical size without mobile overflow", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  for (const scheme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await page.goto("/settings?wollschrift=1");
    await expect(page.getByTestId("wool-experiment-toggle")).toBeChecked();
    await page.evaluate(async () => {
      await document.fonts.ready;
    });
    for (const selector of [".menu-heading", ".menu-links strong"]) {
      const measurements = await page
        .locator(selector)
        .first()
        .evaluate((element) => {
          const style = getComputedStyle(element);
          return { size: parseFloat(style.fontSize), family: style.fontFamily };
        });
      expect(measurements.size).toBeGreaterThanOrEqual(18);
      expect(measurements.family).toContain("Garnschrift C");
    }
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth),
    ).toBeLessThanOrEqual(391);
  }
  await page.goto("/settings?wollschrift=0");
  expect(
    await page
      .locator(".menu-links strong")
      .first()
      .evaluate((element) => getComputedStyle(element).fontSize),
  ).toBe("14px");
});

test("wool covers prose, navigation and entry fields by default, while retaining opt-out", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/settings");
  await expect(page.getByTestId("wool-experiment-toggle")).toBeChecked();
  for (const selector of [
    ".intro",
    ".menu-hint",
    ".menu-links span",
    "#theme-select",
    ".back-link",
    ".btn",
  ]) {
    const computed = await page
      .locator(selector)
      .first()
      .evaluate((el) => {
        const css = getComputedStyle(el);
        return { family: css.fontFamily, fontSize: parseFloat(css.fontSize) };
      });
    expect(computed.family, selector).toContain("Garnschrift C");
    expect(computed.fontSize, selector).toBeGreaterThanOrEqual(17);
  }
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(391);
  await page.goto("/login");
  for (const selector of [".wg-label", "#email", ".wg-button--primary"]) {
    const computed = await page
      .locator(selector)
      .first()
      .evaluate((el) => {
        const css = getComputedStyle(el);
        return { family: css.fontFamily, fontSize: parseFloat(css.fontSize) };
      });
    expect(computed.family, selector).toContain("Garnschrift C");
    expect(computed.fontSize, selector).toBeGreaterThanOrEqual(17);
  }
  await page.goto("/settings?wollschrift=0");
  await expect(page.getByTestId("wool-experiment-toggle")).not.toBeChecked();
  expect(
    await page
      .locator(".intro")
      .evaluate((el) => getComputedStyle(el).fontFamily),
  ).not.toContain("Garnschrift C");
});

test("classless message actions keep wool ink on a contrasting textile surface", async ({
  page,
}) => {
  for (const theme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme: theme });
    await page.goto("/settings?wollschrift=1");
    const styles = await page.evaluate(() => {
      const section = document.createElement("section");
      section.className = "conversation";
      section.innerHTML =
        '<form class="composer"><button type="button">Senden</button></form>' +
        '<form><button type="button">Veröffentlichen</button></form>' +
        '<button class="danger danger-solid" type="button">Jetzt entfernen</button>' +
        '<a class="attention-card-action" href="#test">Weiter</a>' +
        '<div class="account-section"><span class="badge">Dieses Gerät</span></div>' +
        '<span class="unread">2</span>' +
        '<span class="count-badge">3</span>' +
        '<span class="attention-count">1</span>' +
        '<span class="attention-meaning-mark">!</span>';
      document.body.appendChild(section);
      // Reproduce the small fixed component badge boxes with CSSOM.
      for (const [name, height] of [
        ["attention-count", "1.2rem"],
        ["count-badge", "1.25rem"],
        ["attention-meaning-mark", "1rem"],
        ["unread", "1.5rem"],
      ]) {
        const badge = section.querySelector<HTMLElement>("." + name);
        if (!badge) throw new Error("missing badge " + name);
        badge.style.display = "grid";
        badge.style.placeItems = "center";
        badge.style.height = height;
      }
      const result = Array.from(
        section.querySelectorAll("button, a, span"),
      ).map((item) => {
        const computed = getComputedStyle(item);
        return {
          family: computed.fontFamily,
          background: computed.backgroundColor,
          color: computed.color,
          dangerSolid: item.classList.contains("danger-solid"),
          border: computed.borderTopColor,
          isBadge:
            item.classList.contains("unread") ||
            item.classList.contains("count-badge") ||
            item.classList.contains("attention-count") ||
            item.classList.contains("attention-meaning-mark"),
          lineHeight: Number.parseFloat(computed.lineHeight),
          clientHeight: item.clientHeight,
        };
      });
      section.remove();
      return result;
    });
    for (const item of styles) {
      expect(item.family).toContain("Garnschrift C");
      expect(item.background).toBe(
        theme === "light" ? "rgb(251, 248, 241)" : "rgb(21, 31, 33)",
      );
      expect(item.color).not.toBe(item.background);
      if (item.dangerSolid) {
        expect(item.border).toBe(
          theme === "light" ? "rgb(182, 72, 80)" : "rgb(255, 141, 146)",
        );
      }
      if (item.isBadge) {
        expect(item.lineHeight).toBeLessThanOrEqual(item.clientHeight);
      }
    }
    expect(styles).toHaveLength(9);
  }
});
