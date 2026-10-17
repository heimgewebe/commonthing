import { expect, test } from "@playwright/test";
import { mockApiResponses, mockListResponse } from "../tests/fixtures/mockApi";

test("create, search, link, open and edit Commons without a map location", async ({
  page,
}) => {
  await mockApiResponses(page, {
    auth: { authenticated: true, account_id: "e2e-weber", role: "weber" },
  });
  let node = {
    id: "digital-commons",
    title: "Digital Commons",
    kind: "knowledge",
    tags: ["commons"],
    address: null,
    location: null,
    created_at: "2026-10-17T10:00:00Z",
    updated_at: "2026-10-17T10:00:00Z",
    created_by_account_id: "e2e-weber",
  };
  let created = false;
  await page.route("**/api/nodes**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() === "POST") {
      const payload = request.postDataJSON();
      expect(payload.location).toBeNull();
      expect(payload.address).toBeNull();
      expect(payload.operation_id).toMatch(/^[0-9a-f-]{36}$/);
      node = { ...node, ...payload };
      created = true;
      return route.fulfill({ status: 201, json: node });
    }
    if (request.method() === "PUT") {
      const payload = request.postDataJSON();
      expect(payload.location).toBeNull();
      expect(payload.address).toBeNull();
      expect(request.headers()["if-match"]).toBe(`"${node.updated_at}"`);
      node = { ...node, ...payload, updated_at: "2026-10-17T11:00:00Z" };
      return route.fulfill({ json: node });
    }
    if (url.pathname === "/api/nodes/digital-commons")
      return route.fulfill({ json: node });
    if (url.pathname === "/api/nodes")
      return route.fulfill({
        json: mockListResponse(request.url(), created ? [node] : []),
      });
    return route.fallback();
  });
  await page.route("**/api/search?**", (route) =>
    route.fulfill({
      json: {
        items: [node],
        mode: "lexical_fallback",
        generation_id: "test",
        offset: 0,
      },
    }),
  );
  await page.goto("/map?compose=node");
  await page.getByLabel("Mit Kartenort").uncheck();
  await page
    .getByLabel("Name", { exact: false })
    .first()
    .fill("Digital Commons");
  await page
    .getByRole("button", { name: "Knoten knüpfen", exact: true })
    .click();
  await expect(page).toHaveURL(/focus=node/);
  const panel = page.locator('[data-testid="context-panel"]');
  await expect(panel).toBeVisible();
  await expect(
    panel.getByText("Digital Commons", { exact: true }).first(),
  ).toBeVisible();

  await page.goto("/commons");
  await page.getByLabel("Knoten suchen").fill("Digital Commons");
  await page.getByRole("button", { name: "Suchen", exact: true }).click();
  const link = page.getByRole("link", { name: "Digital Commons", exact: true });
  await expect(link).toHaveAttribute("href", "/map?focus=node:digital-commons");
  await link.click();
  await expect(panel).toBeVisible();
  await page.reload();
  await expect(
    panel.getByText("Digital Commons", { exact: true }).first(),
  ).toBeVisible();
  await panel.getByRole("tab", { name: "Bearbeiten", exact: true }).click();
  await panel.getByRole("button", { name: "Bearbeiten", exact: true }).click();
  await panel.getByLabel("Titel", { exact: true }).fill("Commons bearbeitet");
  await panel
    .getByRole("button", { name: "Änderungen speichern", exact: true })
    .click();
  await expect(
    panel.getByText("Commons bearbeitet", { exact: true }).first(),
  ).toBeVisible();
});
