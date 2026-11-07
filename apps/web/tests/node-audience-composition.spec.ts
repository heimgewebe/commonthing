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
