import { expect, test, type Page } from "@playwright/test";
import { mockApiResponses } from "./fixtures/mockApi";
import { waitForMapReady } from "./fixtures/mapReady";
import { demoAccounts } from "../src/lib/demo/demoData";

const ACCOUNT_ID = "00000000-0000-0000-0000-000000000003";

async function openSettingsAsWeber(page: Page) {
  await mockApiResponses(page, {
    auth: {
      authenticated: true,
      account_id: ACCOUNT_ID,
      role: "weber",
    },
  });
  const profileResponse = page.waitForResponse((response) =>
    response.url().endsWith("/api/accounts/me/profile"),
  );
  await page.goto("/settings#meine-garnrolle");
  expect((await profileResponse).status()).toBe(200);
  const section = page.locator('[data-testid="my-garnrolle-section"]');
  await expect(section).toBeVisible();
  await expect(section.getByLabel("Anzeigename")).not.toHaveValue("");
  await expect(section.locator('[data-testid="save-garnrolle"]')).toBeEnabled();
  await expect(
    section.getByText("Ersten Knoten knüpfen", { exact: true }),
  ).toHaveCount(0);
  return section;
}

async function longPressMapCenter(page: Page) {
  const map = page.locator("#map");
  await expect(map).toBeVisible();
  await map.hover({ position: { x: 50, y: 50 } });
  await page.mouse.down();
  await page.waitForTimeout(950);
  await page.mouse.up();
}

async function clickMapCenter(page: Page) {
  const map = page.locator("#map");
  await expect(map).toBeVisible();
  // A plain click (down + up at the same spot, no hold) must set the point in
  // the explicit place-garnrolle mode.
  await map.click({ position: { x: 50, y: 50 } });
}

function expectValidLocation(location: unknown) {
  expect(location).toEqual(
    expect.objectContaining({
      lat: expect.any(Number),
      lon: expect.any(Number),
    }),
  );
  const { lat, lon } = location as { lat: number; lon: number };
  expect(Number.isFinite(lat)).toBe(true);
  expect(Number.isFinite(lon)).toBe(true);
  expect(lat).toBeGreaterThanOrEqual(-90);
  expect(lat).toBeLessThanOrEqual(90);
  expect(lon).toBeGreaterThanOrEqual(-180);
  expect(lon).toBeLessThanOrEqual(180);
}

test.describe("Eigene Garnrolle speichern", () => {
  test("clears sensitive Garnrolle drafts on logout", async ({ page }) => {
    await mockApiResponses(page, {
      auth: {
        authenticated: true,
        account_id: ACCOUNT_ID,
        role: "weber",
      },
    });
    await page.goto("/settings#meine-garnrolle");
    await expect(
      page.locator('[data-testid="account-section-status"]'),
    ).toBeVisible();
    await page.evaluate((accountId) => {
      sessionStorage.setItem(
        `weltgewebe:garnrolle-draft:${accountId}`,
        JSON.stringify({ address: "private draft" }),
      );
      sessionStorage.setItem(
        "weltgewebe:garnrolle-draft:previous-account",
        JSON.stringify({ address: "stale private draft" }),
      );
      sessionStorage.setItem("weltgewebe:unrelated-test", "keep");
    }, ACCOUNT_ID);
    await page.reload();
    await expect(
      page.locator('[data-testid="account-section-status"]'),
    ).toBeVisible();
    const afterAuthenticatedReload = await page.evaluate(
      (accountId) => ({
        current: sessionStorage.getItem(
          `weltgewebe:garnrolle-draft:${accountId}`,
        ),
        previous: sessionStorage.getItem(
          "weltgewebe:garnrolle-draft:previous-account",
        ),
      }),
      ACCOUNT_ID,
    );
    expect(afterAuthenticatedReload.current).not.toBeNull();
    expect(afterAuthenticatedReload.previous).toBeNull();
    await page.evaluate((accountId) => {
      sessionStorage.setItem(
        `weltgewebe:garnrolle-return-location:${accountId}`,
        JSON.stringify({ lat: 53.5, lon: 10.0 }),
      );
    }, ACCOUNT_ID);

    await page.locator('[data-testid="account-section-logout"]').click();
    await expect(
      page.locator('[data-testid="account-section-anonymous"]'),
    ).toBeVisible();
    const remaining = await page.evaluate(
      (accountId) => ({
        draft: sessionStorage.getItem(
          `weltgewebe:garnrolle-draft:${accountId}`,
        ),
        location: sessionStorage.getItem(
          `weltgewebe:garnrolle-return-location:${accountId}`,
        ),
        unrelated: sessionStorage.getItem("weltgewebe:unrelated-test"),
      }),
      ACCOUNT_ID,
    );
    expect(remaining).toEqual({
      draft: null,
      location: null,
      unrelated: "keep",
    });
  });

  test("guest account can describe and save the own Garnrolle", async ({
    page,
  }) => {
    await mockApiResponses(page, {
      auth: {
        authenticated: true,
        account_id: ACCOUNT_ID,
        role: "gast",
      },
    });
    await page.goto("/settings#meine-garnrolle");
    const section = page.locator('[data-testid="my-garnrolle-section"]');
    await expect(
      section.locator('[data-testid="garnrolle-role-warning"]'),
    ).toHaveCount(0);
    await section.getByLabel("Anzeigename").fill("Mitwebende Gastgarnrolle");
    await section.getByLabel("Privat – nicht öffentlich auf der Karte").check();
    const save = section.locator('[data-testid="save-garnrolle"]');
    await expect(save).toBeEnabled();
    const requestPromise = page.waitForRequest(
      (request) =>
        request.url().endsWith("/api/accounts/me/profile") &&
        request.method() === "PATCH",
    );
    await save.click();
    expect((await requestPromise).postDataJSON()).toMatchObject({
      title: "Mitwebende Gastgarnrolle",
      map_state: "not_on_map",
    });
    await expect(
      section.locator('[data-testid="garnrolle-success"]'),
    ).toContainText("gespeichert");
  });

  test("guest places the own Garnrolle via a plain map click and saves it exact", async ({
    page,
  }) => {
    await mockApiResponses(page, {
      auth: {
        authenticated: true,
        account_id: ACCOUNT_ID,
        role: "gast",
      },
    });
    await page.goto("/settings#meine-garnrolle");
    const section = page.locator('[data-testid="my-garnrolle-section"]');
    await expect(section).toBeVisible();
    await expect(
      section.locator('[data-testid="garnrolle-role-warning"]'),
    ).toHaveCount(0);

    await section.getByLabel("Anzeigename").fill("Gastgarnrolle am Ort");
    await section.getByLabel("Öffentlich exakt").check();

    const save = section.locator('[data-testid="save-garnrolle"]');
    // Without a self-selected point the exact profile cannot be saved.
    await expect(save).toBeDisabled();
    await section.locator('[data-testid="choose-garnrolle-location"]').click();

    await expect(page).toHaveURL(/\/map\?compose=garnrolle/);
    const placement = page.locator('[data-testid="garnrolle-placement"]');
    await expect(placement).toContainText("Ort ausstehend");
    // The regression: a normal click (not an 800ms longpress) must set the point.
    await clickMapCenter(page);
    await expect(placement).toContainText("Kartenanker gewählt");
    await placement
      .locator('[data-testid="confirm-garnrolle-location"]')
      .click();

    await expect(page).toHaveURL(/\/settings#meine-garnrolle$/);
    const returned = page.locator('[data-testid="my-garnrolle-section"]');
    await expect(returned.getByLabel("Anzeigename")).toHaveValue(
      "Gastgarnrolle am Ort",
    );
    await expect(
      returned.locator('[data-testid="garnrolle-location-state"]'),
    ).toContainText("Kartenanker gewählt");
    await expect(
      returned.locator('[data-testid="garnrolle-draft-status"]'),
    ).toContainText("noch nicht gespeichert");
    await expect(
      returned.locator('[data-testid="garnrolle-success"]'),
    ).toHaveCount(0);
    await expect(
      returned.getByRole("button", { name: "Punkt ändern" }),
    ).toBeFocused();
    await expect(
      returned.locator('[data-testid="save-garnrolle"]'),
    ).toBeEnabled();

    const requestPromise = page.waitForRequest(
      (request) =>
        request.url().endsWith("/api/accounts/me/profile") &&
        request.method() === "PATCH",
    );
    await returned.locator('[data-testid="save-garnrolle"]').click();
    const payload = (await requestPromise).postDataJSON();
    expect(payload).toMatchObject({
      title: "Gastgarnrolle am Ort",
      map_state: "exact",
    });
    expect(payload).not.toHaveProperty("address");
    expectValidLocation(payload.location);
    await expect(
      returned.locator('[data-testid="garnrolle-success"]'),
    ).toContainText("gespeichert");
  });

  test("creates a separate circle-only resource through the existing Knoten flow without publishing a profile tag", async ({
    page,
  }) => {
    const section = await openSettingsAsWeber(page);
    const circleId = "00000000-0000-4000-8000-000000000099";
    await page.route("**/api/circles/me*", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          {
            id: circleId,
            kind: "household",
            name: "Test-Haus",
            state: "active",
            membership_status: "active",
          },
        ]),
      }),
    );
    await page.route("**/api/nodes/mock-node-1/audience", (route) => {
      if (route.request().method() === "GET")
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            node_id: "mock-node-1",
            scope: "private",
            circle_ids: [],
          }),
        });
      if (route.request().method() === "PUT")
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            node_id: "mock-node-1",
            scope: "circles",
            circle_ids: [circleId],
          }),
        });
      return route.fulfill({ status: 405 });
    });
    await expect(
      section.getByTestId("garnrolle-public-tags-notice"),
    ).toContainText("öffentlich, auch bei privatem Kartenanker");
    // An unfinished public profile edit must survive the transition.
    await section.getByLabel("Güter").fill("Werkzeug");

    let profileWrites = 0;
    page.on("request", (request) => {
      if (
        request.url().endsWith("/api/accounts/me/profile") &&
        request.method() === "PATCH"
      )
        profileWrites += 1;
    });
    await section.getByTestId("garnrolle-private-resource-link").click();
    await expect(page).toHaveURL(/\/map\?compose=node/);
    const panel = page.getByTestId("context-panel");
    await panel.getByLabel("Mit Kartenort").uncheck();
    await panel.locator("#nodeType").selectOption("resource");
    await panel.locator("#title").fill("Meine Leiter");
    await panel.locator("#new-node-audience-scope").selectOption("circles");
    await panel.getByRole("checkbox", { name: "Test-Haus" }).check();

    const grantRequest = page.waitForRequest(
      (request) =>
        new URL(request.url()).pathname === "/api/nodes/mock-node-1/audience" &&
        request.method() === "PUT",
    );
    const nodeRequest = page.waitForRequest(
      (request) =>
        new URL(request.url()).pathname === "/api/nodes" &&
        request.method() === "POST",
    );
    await panel.getByRole("button", { name: "Knoten knüpfen" }).click();
    const payload = (await nodeRequest).postDataJSON();
    expect(payload).toMatchObject({
      kind: "resource",
      title: "Meine Leiter",
      search_visibility: "private",
    });
    expect(payload.tags).not.toContain("good:Meine Leiter");
    expect((await grantRequest).postDataJSON()).toEqual({
      scope: "circles",
      circle_ids: [circleId],
      expected_scope: "private",
      expected_circle_ids: [],
    });
    expect(profileWrites).toBe(0);

    await page.goto("/settings#meine-garnrolle");
    await expect(
      page.getByTestId("my-garnrolle-section").getByLabel("Güter"),
    ).toHaveValue("Werkzeug");
  });

  test("saves a not-on-map profile and reloads the persisted values", async ({
    page,
  }) => {
    const section = await openSettingsAsWeber(page);
    const save = section.locator('[data-testid="save-garnrolle"]');

    await section.getByLabel("Anzeigename").fill("Alex im Weltgewebe");
    await section
      .getByLabel("Kurzbeschreibung")
      .fill("Ich beginne meine Garnrolle selbst.");
    await section.getByLabel("Fähigkeiten").fill("Organisation, Kochen");
    await section.getByLabel("Güter").fill("Werkzeug");
    await section.getByLabel("Interessen").fill("Commons");
    await section.getByLabel("Privat – nicht öffentlich auf der Karte").check();
    await expect(save).toBeEnabled();

    const requestPromise = page.waitForRequest(
      (request) =>
        request.url().endsWith("/api/accounts/me/profile") &&
        request.method() === "PATCH",
    );
    await save.click();
    const request = await requestPromise;
    expect(request.postDataJSON()).toMatchObject({
      title: "Alex im Weltgewebe",
      summary: "Ich beginne meine Garnrolle selbst.",
      map_state: "not_on_map",
      tags: [
        "skill:Organisation",
        "skill:Kochen",
        "good:Werkzeug",
        "interest:Commons",
      ],
    });
    await expect(
      section.locator('[data-testid="garnrolle-success"]'),
    ).toContainText("gespeichert");

    await page.reload();
    await expect(page.getByLabel("Anzeigename")).toHaveValue(
      "Alex im Weltgewebe",
    );
    await expect(page.getByLabel("Fähigkeiten")).toHaveValue(
      "Organisation, Kochen",
    );
  });

  test("validates the approximate radius and keeps setup steps structurally separate", async ({
    page,
  }) => {
    const section = await openSettingsAsWeber(page);
    await page.evaluate((accountId) => {
      sessionStorage.setItem(
        `weltgewebe:garnrolle-return-location:${accountId}`,
        JSON.stringify({ lat: 53.5, lon: 10.0 }),
      );
    }, ACCOUNT_ID);
    await page.reload();

    const stepTwo = section.getByRole("group", {
      name: "2. Privaten Kartenanker wählen",
    });
    const stepThree = section.getByRole("group", {
      name: "3. Öffentliche Sichtbarkeit wählen",
    });
    await expect(stepTwo).toBeVisible();
    await expect(stepThree).toBeVisible();
    await expect(stepTwo.getByLabel("Öffentlich ungefähr")).toHaveCount(0);

    await stepThree.getByLabel("Öffentlich ungefähr").check();
    const radius = stepThree.getByLabel("Ungefährer Umkreis in Metern");
    const save = section.locator('[data-testid="save-garnrolle"]');

    await radius.fill("50.5");
    await expect(radius).toHaveAttribute("aria-invalid", "true");
    await expect(
      stepThree.getByText(
        "Bitte wähle einen Umkreis zwischen 50 und 5.000 Metern.",
      ),
    ).toBeVisible();
    await expect(save).toBeDisabled();

    await radius.fill("51");
    await expect(radius).toHaveAttribute("aria-invalid", "false");
    await expect(save).toBeEnabled();

    await radius.fill("250");
    await expect(radius).toHaveAttribute("aria-invalid", "false");
    await expect(save).toBeEnabled();
  });

  test("allows the private Kartenanker to be removed explicitly", async ({
    page,
  }) => {
    const section = await openSettingsAsWeber(page);
    await page.evaluate((accountId) => {
      sessionStorage.setItem(
        `weltgewebe:garnrolle-return-location:${accountId}`,
        JSON.stringify({ lat: 53.5, lon: 10.0 }),
      );
    }, ACCOUNT_ID);
    await page.reload();

    await expect(
      section.getByRole("button", { name: "Kartenanker entfernen" }),
    ).toBeVisible();
    await section
      .getByRole("button", { name: "Kartenanker entfernen" })
      .click();
    await expect(
      section.getByLabel("Privat – nicht öffentlich auf der Karte"),
    ).toBeChecked();
    await expect(
      section.locator('[data-testid="garnrolle-location-state"]'),
    ).toContainText("Noch kein Kartenanker gewählt");
    await expect(
      section.getByRole("button", { name: "Punkt auf Karte wählen" }),
    ).toBeFocused();
    await expect(
      section.locator('[data-testid="garnrolle-draft-status"]'),
    ).toContainText("auf „Privat“ gesetzt");

    const requestPromise = page.waitForRequest(
      (request) =>
        request.url().endsWith("/api/accounts/me/profile") &&
        request.method() === "PATCH",
    );
    await section.locator('[data-testid="save-garnrolle"]').click();
    expect((await requestPromise).postDataJSON()).toMatchObject({
      map_state: "not_on_map",
      clear_location: true,
    });
  });

  test("rejects address and clear_address in the same mock API request", async ({
    page,
  }) => {
    await openSettingsAsWeber(page);

    const status = await page.evaluate(async () => {
      const response = await fetch("/api/accounts/me/profile", {
        method: "PATCH",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: "Konfliktprüfung",
          tags: [],
          address: "Private Ortsnotiz",
          clear_address: true,
          map_state: "not_on_map",
        }),
      });
      return response.status;
    });

    expect(status).toBe(400);
  });

  test("requires a self-selected map point and preserves the draft across map navigation", async ({
    page,
  }) => {
    const section = await openSettingsAsWeber(page);
    await section.getByLabel("Anzeigename").fill("Garnrolle am gewählten Ort");
    await section
      .getByLabel("Adresse oder Ortsnotiz")
      .fill("Poelsweg 2, Hamburg");
    await section.getByLabel("Fähigkeiten").fill("Nachbarschaftshilfe");
    await section.getByLabel("Öffentlich exakt").check();

    const save = section.locator('[data-testid="save-garnrolle"]');
    // The address is not silently geocoded. A point chosen by the user is required.
    await expect(save).toBeDisabled();
    await section.locator('[data-testid="choose-garnrolle-location"]').click();

    await expect(page).toHaveURL(/\/map\?compose=garnrolle/);
    const placement = page.locator('[data-testid="garnrolle-placement"]');
    await expect(placement).toContainText("Ort ausstehend");
    await longPressMapCenter(page);
    await expect(placement).toContainText("Kartenanker gewählt");
    await placement
      .locator('[data-testid="confirm-garnrolle-location"]')
      .click();

    await expect(page).toHaveURL(/\/settings#meine-garnrolle$/);
    const returned = page.locator('[data-testid="my-garnrolle-section"]');
    await expect(returned.getByLabel("Anzeigename")).toHaveValue(
      "Garnrolle am gewählten Ort",
    );
    await expect(returned.getByLabel("Adresse oder Ortsnotiz")).toHaveValue(
      "Poelsweg 2, Hamburg",
    );
    await expect(
      returned.locator('[data-testid="garnrolle-location-state"]'),
    ).toContainText("Kartenanker gewählt");
    await expect(
      returned.locator('[data-testid="save-garnrolle"]'),
    ).toBeEnabled();

    const requestPromise = page.waitForRequest(
      (request) =>
        request.url().endsWith("/api/accounts/me/profile") &&
        request.method() === "PATCH",
    );
    await returned.locator('[data-testid="save-garnrolle"]').click();
    const payload = (await requestPromise).postDataJSON();
    expect(payload).toMatchObject({
      title: "Garnrolle am gewählten Ort",
      address: "Poelsweg 2, Hamburg",
      map_state: "exact",
      tags: ["skill:Nachbarschaftshilfe"],
    });
    expectValidLocation(payload.location);
    await expect(
      returned.locator('[data-testid="garnrolle-success"]'),
    ).toContainText("gespeichert");

    await page.goto(`/map?focus=garnrolle:${ACCOUNT_ID}`);
    const panel = page.locator('[data-testid="context-panel"]');
    await expect(panel).toContainText("Fähigkeiten: Nachbarschaftshilfe");
    await expect(panel).not.toContainText("skill:Nachbarschaftshilfe");
  });
});

test.describe("Garnrollen-Kartenpunkt per Touch", () => {
  test.use({ hasTouch: true });

  test("accepts an iPad-style touch longpress", async ({ page }) => {
    await mockApiResponses(page, {
      auth: {
        authenticated: true,
        account_id: ACCOUNT_ID,
        role: "weber",
      },
    });
    await page.goto("/map?compose=garnrolle");
    await waitForMapReady(page);
    const placement = page.locator('[data-testid="garnrolle-placement"]');
    await expect(placement).toContainText("Ort ausstehend");

    const map = page.locator("#map canvas").first();
    const box = await map.boundingBox();
    if (!box) throw new Error("map has no bounding box");
    const touchPoint = { x: box.x + 50, y: box.y + 50 };
    const client = await page.context().newCDPSession(page);
    await client.send("Input.dispatchTouchEvent", {
      type: "touchStart",
      touchPoints: [touchPoint],
    });
    await page.waitForTimeout(950);
    await client.send("Input.dispatchTouchEvent", {
      type: "touchEnd",
      touchPoints: [],
    });
    await expect(placement).toContainText("Kartenanker gewählt");
  });

  test("accepts a real jittery iPad-style touch on an existing Garnrolle", async ({
    page,
  }) => {
    await mockApiResponses(page, {
      auth: {
        authenticated: true,
        account_id: ACCOUNT_ID,
        role: "weber",
      },
    });
    await page.setViewportSize({ width: 820, height: 1180 });
    await page.goto("/map?compose=garnrolle");
    await waitForMapReady(page);
    const publicPosition = demoAccounts[0].public_pos;
    if (!publicPosition) throw Error("no public exact Garnrolle fixture");
    await page.evaluate(({ lon, lat }) => {
      window.__TEST_MAP__?.jumpTo({ center: [lon, lat], zoom: 16 });
    }, publicPosition);

    const panel = page.getByTestId("context-panel");
    await expect(panel).toHaveAttribute("data-sheet-stage", "compact");
    const marker = page.getByTestId(`marker-garnrolle-${demoAccounts[0].id}`);
    await expect(marker).toBeVisible();
    const box = await marker.boundingBox();
    if (!box) throw Error("marker not laid out");
    const point = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
    const panelBox = await panel.boundingBox();
    if (!panelBox || point.y >= panelBox.y - 10)
      throw Error("mobile placement panel still covers the Garnrolle");

    const client = await page.context().newCDPSession(page);
    await client.send("Input.dispatchTouchEvent", {
      type: "touchStart",
      touchPoints: [point],
    });
    // A physical tap is rarely motionless. MapLibre may issue movestart
    // long before the intentional 10px pan threshold is exceeded.
    await client.send("Input.dispatchTouchEvent", {
      type: "touchMove",
      touchPoints: [{ x: point.x + 3, y: point.y + 4 }],
    });
    await client.send("Input.dispatchTouchEvent", {
      type: "touchEnd",
      touchPoints: [],
    });
    await expect(page.getByTestId("garnrolle-placement")).toContainText(
      "Kartenanker gewählt",
    );
    await expect(
      page.getByTestId("confirm-garnrolle-location"),
    ).toBeInViewport();
  });

  test("accepts a plain touch tap (no longpress hold)", async ({ page }) => {
    await mockApiResponses(page, {
      auth: {
        authenticated: true,
        account_id: ACCOUNT_ID,
        role: "gast",
      },
    });
    await page.goto("/map?compose=garnrolle");
    await waitForMapReady(page);
    const placement = page.locator('[data-testid="garnrolle-placement"]');
    await expect(placement).toContainText("Ort ausstehend");

    const map = page.locator("#map canvas").first();
    const box = await map.boundingBox();
    if (!box) throw new Error("map has no bounding box");
    const touchPoint = { x: box.x + 50, y: box.y + 50 };
    const client = await page.context().newCDPSession(page);
    await client.send("Input.dispatchTouchEvent", {
      type: "touchStart",
      touchPoints: [touchPoint],
    });
    // A quick tap, well below the longpress threshold.
    await client.send("Input.dispatchTouchEvent", {
      type: "touchEnd",
      touchPoints: [],
    });
    await expect(placement).toContainText("Kartenanker gewählt");
  });
});
