import { expect, test } from "@playwright/test";
import { mockApiResponses } from "./fixtures/mockApi";
import { activateToolFanAction } from "./fixtures/toolFan";

const CREATOR = "weber-e2e-1";
const CIRCLE = "00000000-0000-4000-8000-000000000099";

test.beforeEach(async ({ page }) => {
  await mockApiResponses(page, {
    auth: { authenticated: true, account_id: CREATOR, role: "weber" },
  });
  await page.goto("/map");
  await page.getByTestId("tool-fan").waitFor();
});

test("a new private node is private in the first POST, never temporarily public", async ({
  page,
}) => {
  await activateToolFanAction(page, "weave");
  const panel = page.getByTestId("context-panel");
  await panel.getByLabel("Mit Kartenort").uncheck();
  await panel.locator("#title").fill("Privater Leiter-Knoten");
  await panel.locator("#nodeType").selectOption("resource");
  await panel.locator("#new-node-audience-scope").selectOption("private");

  const post = page.waitForRequest(
    (req) =>
      new URL(req.url()).pathname === "/api/nodes" && req.method() === "POST",
  );
  await panel.getByRole("button", { name: "Knoten knüpfen" }).click();
  expect((await post).postDataJSON()).toMatchObject({
    title: "Privater Leiter-Knoten",
    search_visibility: "private",
  });
});

test("an explicit circle grant follows, never precedes, private node creation", async ({
  page,
}) => {
  const methods: string[] = [];
  await page.route("**/api/circles/me*", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([
        {
          id: CIRCLE,
          kind: "household",
          name: "Test-Haus",
          state: "active",
          membership_status: "active",
        },
      ]),
    }),
  );
  await page.route("**/api/nodes/mock-node-1/audience", async (route) => {
    const req = route.request();
    if (req.method() === "GET")
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          node_id: "mock-node-1",
          scope: "private",
          circle_ids: [],
        }),
      });
    if (req.method() === "PUT") {
      methods.push("PUT-audience");
      const input = req.postDataJSON();
      expect(input).toEqual({
        scope: "circles",
        circle_ids: [CIRCLE],
        expected_scope: "private",
        expected_circle_ids: [],
      });
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          node_id: "mock-node-1",
          scope: "circles",
          circle_ids: [CIRCLE],
        }),
      });
    }
    return route.fulfill({ status: 405 });
  });
  page.on("request", (req) => {
    if (
      new URL(req.url()).pathname === "/api/nodes" &&
      req.method() === "POST"
    ) {
      methods.push("POST-node");
      expect(req.postDataJSON().search_visibility).toBe("private");
    }
  });

  await activateToolFanAction(page, "weave");
  const panel = page.getByTestId("context-panel");
  await panel.getByLabel("Mit Kartenort").uncheck();
  await panel.locator("#title").fill("Hausinterner Leiter-Knoten");
  await panel.locator("#nodeType").selectOption("resource");
  await panel.locator("#new-node-audience-scope").selectOption("circles");
  await panel.getByRole("checkbox", { name: "Test-Haus" }).check();

  await panel.getByRole("button", { name: "Knoten knüpfen" }).click();
  await expect.poll(() => methods.length, { timeout: 10_000 }).toBe(2);
  expect(methods).toEqual(["POST-node", "PUT-audience"]);
});

test("failed circle grant keeps one private node and disables another creation", async ({
  page,
}) => {
  let nodeCreates = 0;
  let grantAttempts = 0;
  await page.route("**/api/circles/me*", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([
        {
          id: CIRCLE,
          kind: "household",
          name: "Test-Haus",
          state: "active",
          membership_status: "active",
        },
      ]),
    }),
  );
  await page.route("**/api/nodes/mock-node-1/audience", async (route) => {
    if (route.request().method() === "GET") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          node_id: "mock-node-1",
          scope: "private",
          circle_ids: [],
        }),
      });
    }
    if (route.request().method() === "PUT") {
      grantAttempts += 1;
      return route.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify({ error: "membership changed" }),
      });
    }
    return route.fulfill({ status: 405 });
  });
  page.on("request", (request) => {
    if (
      new URL(request.url()).pathname === "/api/nodes" &&
      request.method() === "POST"
    ) {
      nodeCreates += 1;
      expect(request.postDataJSON().search_visibility).toBe("private");
    }
  });

  await activateToolFanAction(page, "weave");
  const panel = page.getByTestId("context-panel");
  await panel.getByLabel("Mit Kartenort").uncheck();
  await panel.locator("#title").fill("Privat gespeicherter Hausknoten");
  await panel.locator("#nodeType").selectOption("resource");
  await panel.locator("#new-node-audience-scope").selectOption("circles");
  await panel.getByRole("checkbox", { name: "Test-Haus" }).check();
  await panel.getByRole("button", { name: "Knoten knüpfen" }).click();

  await expect(panel.getByRole("alert")).toContainText(
    "Der Knoten wurde privat gespeichert",
  );
  await expect(
    panel.getByRole("link", { name: "Privat gespeicherten Knoten öffnen" }),
  ).toHaveAttribute("href", /focus=node:mock-node-1/);
  expect(nodeCreates).toBe(1);
  expect(grantAttempts).toBe(1);

  // Even after changing the proposed audience, this form must not issue
  // another POST with a different idempotency key for the already-saved node.
  await panel.locator("#new-node-audience-scope").selectOption("public");
  await expect(
    panel.getByRole("button", { name: "Knoten knüpfen" }),
  ).toBeDisabled();
  await page.waitForTimeout(250);
  expect(nodeCreates).toBe(1);
  expect(grantAttempts).toBe(1);
});

test("a former circle grant remains removable without reopening a private node", async ({
  page,
}) => {
  const former = "00000000-0000-4000-8000-000000000098";
  let putCount = 0;
  await page.route("**/api/circles/me*", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([
        {
          id: CIRCLE,
          kind: "household",
          name: "Aktives Haus",
          state: "active",
          membership_status: "active",
        },
      ]),
    }),
  );
  await page.route("**/api/nodes/mock-node-1/audience", async (route) => {
    if (route.request().method() === "GET") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          node_id: "mock-node-1",
          scope: "circles",
          circle_ids: [former, CIRCLE],
        }),
      });
    }
    if (route.request().method() === "PUT") {
      putCount += 1;
      expect(route.request().postDataJSON()).toEqual({
        scope: "circles",
        circle_ids: [CIRCLE],
        expected_scope: "circles",
        expected_circle_ids: [former, CIRCLE],
      });
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          node_id: "mock-node-1",
          scope: "circles",
          circle_ids: [CIRCLE],
        }),
      });
    }
    return route.fulfill({ status: 405 });
  });

  // Create one canonical owner-controlled node through the real form.
  await activateToolFanAction(page, "weave");
  const form = page.getByTestId("context-panel");
  await form.getByLabel("Mit Kartenort").uncheck();
  await form.locator("#title").fill("Verwaiste Hausfreigabe");
  await form.locator("#nodeType").selectOption("resource");
  await form.locator("#new-node-audience-scope").selectOption("private");
  await form.getByRole("button", { name: "Knoten knüpfen" }).click();
  await expect(page).toHaveURL(/focus=node:mock-node-1/);

  const panel = page.getByTestId("context-panel");
  await panel.getByRole("tab", { name: "Bearbeiten" }).click();
  const audience = panel.getByTestId("node-audience-controls");
  await expect(
    audience.getByText(`Frühere Gemeinschaft (${former}) – Freigabe entfernen`),
  ).toBeVisible();
  await audience
    .getByRole("checkbox", { name: /Frühere Gemeinschaft/ })
    .click();
  await audience
    .getByRole("button", { name: "Sichtbarkeit speichern" })
    .click();
  await expect(audience.getByText("Sichtbarkeit gespeichert.")).toBeVisible();
  expect(putCount).toBe(1);
});
