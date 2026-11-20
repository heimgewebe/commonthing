import { test, expect } from "@playwright/test";
import { mockApiResponses } from "./fixtures/mockApi";

const ROOT_ACTIONS = [
  "tool-fan-find",
  "tool-fan-map-content",
  "tool-fan-weave",
] as const;

async function openToolFan(page: import("@playwright/test").Page) {
  await page.getByTestId("tool-fan-trigger").click();
  await expect(page.getByTestId("tool-fan")).toHaveAttribute(
    "data-expanded",
    "true",
  );
}

test.describe("Tool Fan Layout", () => {
  test.beforeEach(async ({ page }) => {
    await mockApiResponses(page, {
      auth: { authenticated: true, account_id: "e2e-weber", role: "weber" },
    });
    await page.goto("/map");
    await page.waitForSelector('[data-testid="tool-fan"]', {
      timeout: 10000,
    });
  });

  test("root button is closed by default and centered at the bottom", async ({
    page,
  }) => {
    const trigger = page.getByTestId("tool-fan-trigger");
    await expect(trigger).toBeVisible();
    await expect(trigger).toHaveAttribute("aria-expanded", "false");
    await expect(trigger).toHaveAttribute("aria-label", "Aktionen öffnen");
    await expect(trigger).toContainText("Aktionen");

    const box = await trigger.boundingBox();
    const viewport = page.viewportSize();
    expect(box).not.toBeNull();
    expect(viewport).not.toBeNull();
    expect(
      Math.abs(box!.x + box!.width / 2 - viewport!.width / 2),
    ).toBeLessThan(2);
  });

  test("opening reveals Finden, Karteninhalt and Weben in stable order", async ({
    page,
  }) => {
    await openToolFan(page);
    const actions = page.locator(
      "#tool-fan-actions .fan-row--root .fan-action",
    );
    await expect(actions).toHaveCount(3);

    await expect(actions.nth(0)).toHaveAttribute(
      "data-testid",
      ROOT_ACTIONS[0],
    );
    await expect(actions.nth(0)).toContainText("Suchen");
    await expect(actions.nth(1)).toHaveAttribute(
      "data-testid",
      ROOT_ACTIONS[1],
    );
    await expect(actions.nth(1)).toContainText("Filter");
    await expect(actions.nth(2)).toHaveAttribute(
      "data-testid",
      ROOT_ACTIONS[2],
    );
    await expect(actions.nth(2)).toContainText("Erstellen");

    for (const testId of ROOT_ACTIONS) {
      await expect(page.getByTestId(testId)).toHaveAttribute("tabindex", "0");
    }
  });

  test("visible labels form the accessible name, including the filter badge", async ({
    page,
  }) => {
    await openToolFan(page);
    const mapContent = page.getByTestId("tool-fan-map-content");
    await expect(mapContent).not.toHaveAttribute("aria-label", /.+/);
    await expect(mapContent).toHaveAccessibleName("Filter");
  });

  test("Tab leaves the non-modal fan and closes it instead of trapping focus", async ({
    page,
  }) => {
    const trigger = page.getByTestId("tool-fan-trigger");
    await trigger.focus();
    await trigger.press("Enter");

    for (const testId of ROOT_ACTIONS) {
      await page.keyboard.press("Tab");
      await expect(page.getByTestId(testId)).toBeFocused();
    }
    await page.keyboard.press("Tab");
    await expect(page.getByTestId("tool-fan")).toHaveAttribute(
      "data-expanded",
      "false",
    );
  });

  test("Weben opens a second level and Knoten knüpfen starts composition", async ({
    page,
  }) => {
    await openToolFan(page);
    await page.getByTestId("tool-fan-weave").click();

    await expect(page.getByTestId("tool-fan")).toHaveAttribute(
      "data-branch",
      "weave",
    );
    await expect(page.getByTestId("tool-fan-create-node")).toBeEnabled();
    await expect(page.getByTestId("tool-fan-create-proposal")).toBeDisabled();
    await expect(page.getByTestId("tool-fan-create-node")).toBeFocused();

    await page.getByTestId("tool-fan-create-node").click();
    const panel = page.getByTestId("context-panel");
    await expect(panel).toBeVisible();
    await expect(panel.locator("#title")).toBeFocused();
  });

  test("Escape closes the fan and restores focus to the trigger", async ({
    page,
  }) => {
    const trigger = page.getByTestId("tool-fan-trigger");
    await openToolFan(page);
    await page.keyboard.press("Escape");
    await expect(page.getByTestId("tool-fan")).toHaveAttribute(
      "data-expanded",
      "false",
    );
    await expect(trigger).toBeFocused();
  });

  test("opening the tool fan closes an open map lens", async ({ page }) => {
    await openToolFan(page);
    await page.getByTestId("tool-fan-find").click();
    await expect(page.getByTestId("search-overlay")).toBeVisible();

    await page.getByTestId("tool-fan-trigger").click();
    await expect(page.getByTestId("search-overlay")).toHaveCount(0);
    await expect(page.getByTestId("tool-fan")).toHaveAttribute(
      "data-expanded",
      "true",
    );
  });

  test("root and weave branches stay inside a 320 pixel viewport", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 320, height: 568 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await openToolFan(page);

    for (const testId of ["tool-fan-trigger", ...ROOT_ACTIONS]) {
      const box = await page.getByTestId(testId).boundingBox();
      expect(box, `${testId} has no box`).not.toBeNull();
      expect(box!.x).toBeGreaterThanOrEqual(0);
      expect(box!.y).toBeGreaterThanOrEqual(0);
      expect(box!.x + box!.width).toBeLessThanOrEqual(320);
      expect(box!.y + box!.height).toBeLessThanOrEqual(568);
    }

    await page.getByTestId("tool-fan-weave").click();
    for (const testId of [
      "tool-fan-back",
      "tool-fan-create-node",
      "tool-fan-create-proposal",
    ]) {
      const box = await page.getByTestId(testId).boundingBox();
      expect(box, `${testId} has no box`).not.toBeNull();
      expect(box!.x).toBeGreaterThanOrEqual(0);
      expect(box!.x + box!.width).toBeLessThanOrEqual(320);
      expect(box!.y + box!.height).toBeLessThanOrEqual(568);
    }
  });

  test("all interactive controls meet the 44px touch target minimum", async ({
    page,
  }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    const trigger = page.getByTestId("tool-fan-trigger");
    expect((await trigger.boundingBox())?.height).toBeGreaterThanOrEqual(44);

    await openToolFan(page);
    for (const testId of ROOT_ACTIONS) {
      expect(
        (await page.getByTestId(testId).boundingBox())?.height,
      ).toBeGreaterThanOrEqual(44);
    }
  });

  test("respects reduced motion", async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await openToolFan(page);
    const duration = await page
      .locator("#tool-fan-actions")
      .evaluate((el) => getComputedStyle(el).transitionDuration);
    expect(duration).toMatch(/^0s(,\s*0s)*$/);
  });
});

test.describe("Tool Fan Layout — guest role", () => {
  test("Weben exposes the real Weberantrag action without a wide disabled root button", async ({
    page,
  }) => {
    await mockApiResponses(page, {
      auth: { authenticated: true, account_id: "e2e-guest", role: "gast" },
    });
    await page.setViewportSize({ width: 320, height: 568 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto("/map");

    await openToolFan(page);
    const weave = page.getByTestId("tool-fan-weave");
    await expect(weave).toBeEnabled();
    await expect(weave).toContainText("Erstellen");
    expect((await weave.boundingBox())!.width).toBeLessThan(120);

    await weave.click();
    await expect(page.getByTestId("tool-fan-create-node")).toBeEnabled();
    const proposal = page.getByTestId("tool-fan-create-proposal");
    await expect(proposal).toBeEnabled();
    await expect(proposal).toHaveAttribute("href", "/antraege#antrag-stellen");
    await expect(page.getByTestId("tool-fan-create-node")).toBeFocused();
  });
});
