import { expect, test } from "@playwright/test";
import { mockApiResponses } from "./fixtures/mockApi";
import { waitForMapReady } from "./fixtures/mapReady";
import { demoAccounts } from "../src/lib/demo/demoData";

const owner = demoAccounts[0];
const initialLocation = owner.location;
if (!initialLocation)
  throw new Error("exact demo account must have a location");
const ownerLocation: { lat: number; lon: number } = initialLocation;

test.use({
  launchOptions: {
    executablePath: process.env.PLAYWRIGHT_TEST_CHROME_PATH ?? undefined,
  },
});

test.describe("Bestehende Garnrolle erneut positionieren", () => {
  test.use({ hasTouch: true, viewport: { width: 820, height: 1180 } });

  async function openExistingProfile(page: import("@playwright/test").Page) {
    await mockApiResponses(page, {
      auth: { authenticated: true, account_id: owner.id, role: "weber" },
    });
    await page.goto("/settings#meine-garnrolle");
    const section = page.getByTestId("my-garnrolle-section");
    await expect(
      section.getByRole("button", { name: "Punkt ändern" }),
    ).toBeEnabled();
    await expect(section.getByLabel("Öffentlich exakt")).toBeChecked();
    return section;
  }

  async function chooseNewTouchPoint(page: import("@playwright/test").Page) {
    await waitForMapReady(page);
    const placement = page.getByTestId("garnrolle-placement");
    await expect(placement).toContainText("Ort ausstehend");
    const canvas = page.locator("#map canvas").first();
    const box = await canvas.boundingBox();
    if (!box) throw new Error("map canvas is not laid out");
    const point = { x: box.x + 105, y: box.y + 115 };
    const cdp = await page.context().newCDPSession(page);
    await cdp.send("Input.dispatchTouchEvent", {
      type: "touchStart",
      touchPoints: [point],
    });
    await cdp.send("Input.dispatchTouchEvent", {
      type: "touchMove",
      touchPoints: [{ x: point.x + 3, y: point.y + 4 }],
    });
    await cdp.send("Input.dispatchTouchEvent", {
      type: "touchEnd",
      touchPoints: [],
    });
    await expect(placement).toContainText("Kartenanker gewählt");
    await expect(page.getByTestId("confirm-garnrolle-location")).toBeEnabled();
    await page.getByTestId("confirm-garnrolle-location").click();
    await expect(page).toHaveURL(/\/settings#meine-garnrolle$/);
  }

  async function verifySavedReplacement(
    page: import("@playwright/test").Page,
    forbiddenLocation?: { lat: number; lon: number },
  ) {
    const section = page.getByTestId("my-garnrolle-section");
    await expect(section.getByTestId("garnrolle-location-state")).toContainText(
      "Kartenanker gewählt",
    );
    const saving = page.waitForRequest(
      (request) =>
        request.url().endsWith("/api/accounts/me/profile") &&
        request.method() === "PATCH",
    );
    await section.getByTestId("save-garnrolle").click();
    const request = await saving;
    const newLocation = request.postDataJSON().location as {
      lat: number;
      lon: number;
    };
    expect(
      Number.isFinite(newLocation.lat) && Number.isFinite(newLocation.lon),
    ).toBe(true);
    expect(newLocation.lat).not.toBe(ownerLocation.lat);
    expect(newLocation.lon).not.toBe(ownerLocation.lon);
    if (forbiddenLocation) expect(newLocation).not.toEqual(forbiddenLocation);
    expect(request.postDataJSON().map_state).toBe("exact");
    await expect(section.getByTestId("garnrolle-success")).toContainText(
      "gespeichert",
    );
    await page.reload();
    await expect(
      page.getByRole("button", { name: "Punkt ändern" }),
    ).toBeEnabled();
    const persisted = await page.evaluate(async () => {
      const response = await fetch("/api/accounts/me/profile", {
        credentials: "include",
      });
      const profile = await response.json();
      return profile.location;
    });
    expect(persisted).toEqual(newLocation);
  }

  test("already-saved exact Garnrolle can move through iPad touch picker and persist", async ({
    page,
  }) => {
    const section = await openExistingProfile(page);
    await section.getByRole("button", { name: "Punkt ändern" }).click();
    await expect(page).toHaveURL(/\/map\?compose=garnrolle/);
    await chooseNewTouchPoint(page);
    await verifySavedReplacement(page);
  });

  test("denied private session storage does not make Punkt ändern inert", async ({
    page,
  }) => {
    await page.addInitScript(() => {
      const setItem = Storage.prototype.setItem;
      Object.assign(window, {
        __testPutStaleGarnrolleReturn: (key: string, value: string) =>
          setItem.call(window.sessionStorage, key, value),
      });
      Storage.prototype.setItem = function (key: string, value: string) {
        if (
          key.startsWith("weltgewebe:garnrolle-draft:") ||
          key.startsWith("weltgewebe:garnrolle-return-location:")
        ) {
          throw new DOMException(
            "Private storage is not writable",
            "QuotaExceededError",
          );
        }
        return setItem.call(this, key, value);
      };
    });
    const section = await openExistingProfile(page);
    await section.getByRole("button", { name: "Punkt ändern" }).click();
    await expect(page).toHaveURL(/\/map\?compose=garnrolle/);
    // A previous return-location key can remain if writes are suddenly denied.
    // The new physical touch must win over that stale browser value.
    const stale = { lat: -20, lon: 100 };
    await page.evaluate(
      ({ id, stale }) => {
        const testWindow = window as typeof window & {
          __testPutStaleGarnrolleReturn: (key: string, value: string) => void;
        };
        testWindow.__testPutStaleGarnrolleReturn(
          `weltgewebe:garnrolle-return-location:${id}`,
          JSON.stringify(stale),
        );
      },
      { id: owner.id, stale },
    );
    await chooseNewTouchPoint(page);
    await verifySavedReplacement(page, stale);
  });
});
