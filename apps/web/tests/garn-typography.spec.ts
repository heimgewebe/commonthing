import { expect, test, type Page, type TestInfo } from "@playwright/test";
import { mockApiResponses } from "./fixtures/mockApi";
import { waitForNodeMarker } from "./fixtures/mapReady";
import { writeFile } from "node:fs/promises";

const family = "Atkinson Hyperlegible Next";
const fontPath = "/fonts/atkinson-hyperlegible-next-latin-wght-normal.woff2";
const specimen = "Gemeinsam weben · ü ö ä ß · 1/I/l O/0";

async function savePixels(
  info: TestInfo,
  name: string,
  { body }: { body: Buffer; contentType: string },
) {
  const path = info.outputPath(`${name}.png`);
  await writeFile(path, body);
  await info.attach(name, { path, contentType: "image/png" });
}

async function samples(page: Page) {
  await page.evaluate((text) => {
    const panel = document.createElement("section");
    panel.id = "garn-specimen";
    panel.className = "panel";
    Object.assign(panel.style, {
      position: "fixed",
      top: "80px",
      left: "16px",
      right: "16px",
      zIndex: "200",
      maxWidth: "700px",
      color: "var(--text)",
    });
    for (const size of [12, 13, 14, 16, 20, 22, 24, 40]) {
      const line = document.createElement("p");
      line.textContent = text;
      line.dataset.size = String(size);
      Object.assign(line.style, {
        fontSize: `${size}px`,
        lineHeight: "1.4",
        margin: "8px 0",
        overflowWrap: "anywhere",
      });
      panel.append(line);
    }
    document.body.append(panel);
  }, specimen);
}

for (const route of ["/map", "/login"]) {
  for (const colorScheme of ["light", "dark"] as const) {
    for (const width of [390, 768, 1280]) {
      test(`${route} ${colorScheme} ${width}: optical ink, font, selection and bounds`, async ({
        page,
      }, testInfo) => {
        await page.setViewportSize({ width, height: 1000 });
        await page.emulateMedia({ colorScheme, reducedMotion: "reduce" });
        await mockApiResponses(page);
        const fontResponse = page.waitForResponse((response) =>
          response.url().endsWith(fontPath),
        );
        await page.goto(route);
        const response = await fontResponse;
        expect(response.status()).toBe(200);
        expect((await response.body()).length).toBe(33996);
        await page.evaluate(() => document.fonts.ready);
        expect(
          await page.evaluate(
            (name) => document.fonts.check(`400 16px "${name}"`, "üöäß1IlO0"),
            family,
          ),
        ).toBe(true);
        expect(
          await page.evaluate(
            (name) =>
              [...document.fonts].some(
                (font) =>
                  font.family.includes(name) && font.status === "loaded",
              ),
            family,
          ),
        ).toBe(true);
        for (const control of await page
          .locator(
            "button:visible:not(#map *), input:visible:not(#map *), select:visible:not(#map *)",
          )
          .all()) {
          expect(
            await control.evaluate((el) => getComputedStyle(el).fontFamily),
          ).toContain(family);
        }
        if (route === "/map") {
          await expect(page.getByTestId("tool-fan-trigger")).toBeVisible();
          expect(
            await page
              .locator("#map")
              .evaluate((el) => getComputedStyle(el).textShadow),
          ).toBe("none");
          expect(
            await page
              .locator(".map-marker")
              .first()
              .evaluate((el) => getComputedStyle(el).textShadow),
          ).toBe("none");
        }
        await samples(page);
        const panel = page.locator("#garn-specimen");
        for (const size of [12, 13, 14, 16, 20, 22, 24, 40]) {
          const line = panel.locator(`[data-size="${size}"]`);
          const metrics = await line.evaluate((el) => {
            const css = getComputedStyle(el);
            return {
              size: css.fontSize,
              line: css.lineHeight,
              width: el.clientWidth,
              scroll: el.scrollWidth,
              height: el.clientHeight,
              scrollHeight: el.scrollHeight,
              filter: css.filter,
              color: css.color,
            };
          });
          expect(metrics.size).toBe(`${size}px`);
          expect(Number.parseFloat(metrics.line)).toBeCloseTo(size * 1.4, 1);
          expect(metrics.scroll).toBeLessThanOrEqual(metrics.width);
          expect(metrics.scrollHeight).toBeLessThanOrEqual(metrics.height + 1);
          expect(metrics.filter).toBe("none");
          expect(metrics.color).not.toBe("rgba(0, 0, 0, 0)");
          const sewn = await line.screenshot();
          await line.evaluate((el) => {
            (el as HTMLElement).style.textShadow = "none";
          });
          const flat = await line.screenshot();
          expect(
            sewn.equals(flat),
            `${size}px relief must affect rendered pixels`,
          ).toBe(false);
          await line.evaluate((el) => {
            (el as HTMLElement).style.removeProperty("text-shadow");
          });
        }
        await panel.evaluate((el) => {
          const range = document.createRange();
          range.selectNodeContents(el);
          window.getSelection()?.removeAllRanges();
          window.getSelection()?.addRange(range);
        });
        expect(
          await page.evaluate(() => window.getSelection()?.toString()),
        ).toContain(specimen);
        await page.evaluate(() => window.getSelection()?.removeAllRanges());
        expect(
          await page.evaluate(
            () => document.documentElement.scrollWidth <= innerWidth,
          ),
        ).toBe(true);
        await savePixels(testInfo, "linen-optical-sizes", {
          body: await panel.screenshot(),
          contentType: "image/png",
        });
        await panel.evaluate((el) => el.remove());
        await savePixels(testInfo, "product-route", {
          body: await page.screenshot(),
          contentType: "image/png",
        });
      });
    }
  }
}

test("fallback ink stays visible when the font fails", async ({
  page,
}, testInfo) => {
  await mockApiResponses(page);
  await page.route(`**${fontPath}`, (route) => route.abort());
  await page.goto("/login");
  await page.evaluate(() => document.fonts.ready);
  const heading = page.locator("h1");
  await expect(heading).toHaveText("Login");
  expect(
    await page.evaluate(() =>
      [...document.fonts].some((font) => font.status === "error"),
    ),
  ).toBe(true);
  const ink = await heading.screenshot();
  await heading.evaluate((el) => {
    Object.assign((el as HTMLElement).style, {
      color: "transparent",
      textShadow: "none",
    });
  });
  expect(ink.equals(await heading.screenshot())).toBe(false);
  await heading.evaluate((el) => {
    (el as HTMLElement).style.removeProperty("color");
    (el as HTMLElement).style.removeProperty("text-shadow");
  });
  await savePixels(testInfo, "visible-fallback", {
    body: ink,
    contentType: "image/png",
  });
});

test("real touch opens sewn search and fills the native login input", async ({
  browser,
}, testInfo) => {
  const context = await browser.newContext({
    hasTouch: true,
    viewport: { width: 768, height: 1024 },
  });
  const page = await context.newPage();
  await mockApiResponses(page);
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/map");
  await page.getByTestId("tool-fan-trigger").tap();
  const find = page.getByTestId("tool-fan-find");
  expect((await find.boundingBox())!.height).toBeGreaterThanOrEqual(44);
  await expect(find).toHaveCSS("font-family", new RegExp(family));
  await find.tap();
  const search = page.getByTestId("search-overlay").locator("input");
  await search.tap();
  await search.fill("ü ö ä ß 1Il O0");
  await expect(search).toHaveValue("ü ö ä ß 1Il O0");
  await savePixels(testInfo, "touch-search", {
    body: await page.screenshot(),
    contentType: "image/png",
  });
  await page.goto("/login");
  await page.locator("#email").tap();
  await page.locator("#email").fill("weben@example.com");
  await expect(
    page.getByRole("button", { name: "Login-Link senden" }),
  ).toBeEnabled();
  await expect(page.locator("#email")).toHaveCSS(
    "font-family",
    new RegExp(family),
  );
  await context.close();
});

for (const colorScheme of ["light", "dark"] as const) {
  test(`actual panel tabs, body and metadata share ink: ${colorScheme}`, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize({ width: 768, height: 1024 });
    await page.emulateMedia({ colorScheme, reducedMotion: "reduce" });
    await mockApiResponses(page, {
      auth: { authenticated: true, account_id: "e2e-weber", role: "weber" },
    });
    await page.goto("/map");
    await page.waitForFunction(() => "__TEST_MAP__" in window);
    await page.evaluate(() => {
      const map = (
        window as Window & {
          __TEST_MAP__: {
            jumpTo: (position: {
              center: [number, number];
              zoom: number;
            }) => void;
          };
        }
      ).__TEST_MAP__;
      map.jumpTo({
        center: [10.060228407382967, 53.558894813662505],
        zoom: 14,
      });
    });
    await (await waitForNodeMarker(page)).click();
    const panel = page.getByTestId("context-panel");
    await page.getByTestId("sheet-handle").click();
    await expect(panel).toHaveAttribute("data-sheet-stage", "full");
    for (const el of await panel
      .locator(
        ".mobile-panel-title:visible, h3:visible, p:visible, small:visible, .tabs button:visible",
      )
      .all()) {
      expect(
        await el.evaluate((node) => getComputedStyle(node).fontFamily),
      ).toContain(family);
      const ink = await el.evaluate((node) => {
        const css = getComputedStyle(node);
        return {
          relief: css.textShadow,
          texture: css.backgroundImage,
          clip: css.backgroundClip,
          fill: css.webkitTextFillColor,
        };
      });
      expect(
        ink.relief !== "none" ||
          (ink.clip === "text" &&
            ink.texture.includes("repeating-linear-gradient") &&
            ink.fill === "rgba(0, 0, 0, 0)"),
        "each panel text must have readable relief or directional thread ink",
      ).toBe(true);
      const bounds = await el.evaluate((node) => ({
        text: node.textContent,
        tag: node.tagName,
        width: node.clientWidth,
        scroll: node.scrollWidth,
        css: node.className,
      }));
      expect(bounds.scroll, JSON.stringify(bounds)).toBeLessThanOrEqual(
        bounds.width + 1,
      );
    }
    await expect(panel.getByRole("tab", { name: "Gespräch" })).toBeVisible();
    await panel.getByRole("tab", { name: "Gespräch" }).click();
    await expect(panel.getByRole("tab", { name: "Gespräch" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await savePixels(testInfo, "product-tabs", {
      body: await page.screenshot(),
      contentType: "image/png",
    });
  });
}

test("readable display ink contains directional stitches", async ({
  page,
}, testInfo) => {
  await mockApiResponses(page);
  await page.setViewportSize({ width: 768, height: 1024 });
  await page.goto("/login");
  await page.evaluate(() => document.fonts.ready);
  const heading = page.locator("h1");
  await expect(heading).toHaveText("Login");
  const css = await heading.evaluate((el) => {
    const style = getComputedStyle(el);
    return {
      image: style.backgroundImage,
      clip: style.backgroundClip,
      fill: style.webkitTextFillColor,
      shadow: style.textShadow,
      size: parseFloat(style.fontSize),
    };
  });
  expect(css.size).toBeGreaterThanOrEqual(22);
  expect(css.image).toContain("repeating-linear-gradient");
  expect(css.clip).toBe("text");
  expect(css.fill).toBe("rgba(0, 0, 0, 0)");
  expect(css.shadow).toBe("none");
  const sewn = await heading.screenshot();
  await heading.evaluate((el) => {
    (el as HTMLElement).style.backgroundImage = "none";
    (el as HTMLElement).style.webkitTextFillColor = "currentcolor";
  });
  const flat = await heading.screenshot();
  expect(
    sewn.equals(flat),
    "stitch ribs must change real rendered pixels",
  ).toBe(false);
  await savePixels(testInfo, "visible-satin-heading", {
    body: sewn,
    contentType: "image/png",
  });

  // A Garnrolle uses an h3, not the login page's h1. Catch future selector
  // drift that would leave real profile names plain on the map again.
  await page.evaluate(() => {
    const accountHeading = document.createElement("h3");
    accountHeading.dataset.testid = "garnrolle-embroidery-heading";
    accountHeading.textContent = "Garnrolle · ü ö ä ß";
    document.body.append(accountHeading);
  });
  const accountHeading = page.getByTestId("garnrolle-embroidery-heading");
  await expect(accountHeading).toHaveCSS("background-clip", "text");
  const accountInk = await accountHeading.evaluate((el) => {
    const style = getComputedStyle(el);
    return {
      image: style.backgroundImage,
      fill: style.webkitTextFillColor,
      font: style.fontFamily,
    };
  });
  expect(accountInk.image).toContain("repeating-linear-gradient");
  expect(accountInk.fill).toBe("rgba(0, 0, 0, 0)");
  expect(accountInk.font).toContain(family);
  await savePixels(testInfo, "visible-garnrolle-heading", {
    body: await accountHeading.screenshot(),
    contentType: "image/png",
  });
});

test("embroidered headings remain opaque when backgrounds are unavailable", async ({
  page,
}) => {
  await mockApiResponses(page);
  await page.goto("/login");
  const heading = page.locator("h1");
  for (const media of ["print", "forced"] as const) {
    await page.emulateMedia(
      media === "print"
        ? { media: "print", forcedColors: "none" }
        : { media: "screen", forcedColors: "active" },
    );
    await expect(heading).toHaveCSS("background-image", "none");
    const fill = await heading.evaluate(
      (el) => getComputedStyle(el).webkitTextFillColor,
    );
    expect(fill, media).not.toBe("rgba(0, 0, 0, 0)");
    expect(fill, media).not.toBe("transparent");
  }
});
