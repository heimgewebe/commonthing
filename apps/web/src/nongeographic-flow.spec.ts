import { expect, test } from "@playwright/test";
import { mockApiResponses, mockListResponse } from "../tests/fixtures/mockApi";

test("commons uses dedicated public endpoint for every page; search retains owner results", async ({
  page,
}) => {
  await mockApiResponses(page, {
    auth: { authenticated: true, account_id: "commons-owner", role: "weber" },
  });
  const listRequests: URL[] = [];
  const searchRequests: URL[] = [];
  await page.route("**/api/public/nodes?**", (route) => {
    const url = new URL(route.request().url());
    listRequests.push(url);
    const more = url.searchParams.has("cursor");
    return route.fulfill({
      json: {
        items: [
          {
            id: more ? "public-two" : "public-one",
            title: more ? "Public two" : "Public one",
            kind: "resource",
            location: null,
            search_visibility: "public",
          },
        ],
        page: {
          next_cursor: more ? null : "next-public-page",
          has_more: !more,
        },
      },
    });
  });
  await page.route("**/api/search?**", (route) => {
    searchRequests.push(new URL(route.request().url()));
    return route.fulfill({
      json: {
        items: [
          {
            id: "owner-private",
            title: "Owner's private Commons",
            kind: "resource",
            location: null,
          },
        ],
        mode: "lexical_fallback",
        generation_id: "test",
        offset: 0,
      },
    });
  });

  await page.goto("/commons");
  await expect(
    page.getByRole("link", { name: "Public one", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Weitere Knoten" }).click();
  await expect(
    page.getByRole("link", { name: "Public two", exact: true }),
  ).toBeVisible();
  // Only the explicit public route may be queried, even when the list
  // requests a second page. Legacy /nodes is never used by the catalog.
  expect(listRequests).toHaveLength(2);
  for (const url of listRequests) {
    expect(url.pathname).toBe("/api/public/nodes");
    expect(url.searchParams.has("public_only")).toBe(false);
    expect(url.searchParams.get("pagination")).toBe("cursor");
    expect(url.searchParams.has("bbox")).toBe(false);
  }
  expect(listRequests[1].searchParams.get("cursor")).toBe("next-public-page");

  await page.getByLabel("Knoten suchen").fill("private Commons");
  await page.getByRole("button", { name: "Suchen", exact: true }).click();
  await expect(
    page.getByRole("link", { name: "Owner's private Commons", exact: true }),
  ).toBeVisible();
  expect(listRequests).toHaveLength(2);
  expect(searchRequests).toHaveLength(1);
  expect(searchRequests[0].searchParams.get("q")).toBe("private Commons");
  expect(searchRequests[0].searchParams.get("limit")).toBe("10");
  expect(searchRequests[0].searchParams.has("public_only")).toBe(false);

  // Switching an owner-only search back to public discovery must clear the
  // private result immediately, not after an arbitrarily slow public response.
  let releasePublicResponse: () => void = () => {};
  const heldPublicResponse = new Promise<void>((resolve) => {
    releasePublicResponse = resolve;
  });
  await page.route("**/api/public/nodes?**", async (route) => {
    await heldPublicResponse;
    return route.fulfill({
      json: {
        items: [
          {
            id: "public-replacement",
            title: "Public replacement",
            kind: "resource",
            location: null,
            search_visibility: "public",
          },
        ],
        page: { next_cursor: null, has_more: false },
      },
    });
  });
  await page.getByLabel("Knoten suchen").fill("");
  await page.getByRole("button", { name: "Suchen", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("Knoten werden geladen");
  await expect(
    page.getByRole("link", { name: "Owner's private Commons", exact: true }),
  ).toHaveCount(0);
  releasePublicResponse();
  await expect(
    page.getByRole("link", { name: "Public replacement", exact: true }),
  ).toBeVisible();
});

test("commons rejects older APIs without the public-only endpoint (no legacy fallback)", async ({
  page,
}) => {
  await mockApiResponses(page);
  let publicRequests = 0;
  let legacyRequests = 0;
  await page.route("**/api/public/nodes?**", (route) => {
    publicRequests += 1;
    return route.fulfill({ status: 404, json: { error: "Not found" } });
  });
  await page.route("**/api/nodes?**", (route) => {
    legacyRequests += 1;
    return route.fulfill({
      json: {
        items: [
          {
            id: "private",
            title: "Private leak",
            search_visibility: "private",
          },
        ],
        page: { next_cursor: "more-private-data", has_more: true },
      },
    });
  });

  await page.goto("/commons");
  await expect(page.getByRole("alert")).toContainText(
    "Knoten konnten nicht geladen werden",
  );
  await expect(page.getByText("Private leak")).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Weitere Knoten" }),
  ).toHaveCount(0);
  expect(publicRequests).toBe(1);
  expect(legacyRequests).toBe(0);
});

test("commons refuses unexpected nonpublic public-route response and its cursor", async ({
  page,
}) => {
  await mockApiResponses(page);
  let publicRequests = 0;
  await page.route("**/api/public/nodes?**", (route) => {
    publicRequests += 1;
    return route.fulfill({
      json: {
        items: [
          {
            id: "public",
            title: "Invalid public page",
            search_visibility: "public",
          },
          {
            id: "private",
            title: "Private title",
            search_visibility: "private",
          },
          { id: "hidden", title: "Hidden title", search_visibility: "hidden" },
          {
            id: "revoked",
            title: "Revoked title",
            search_visibility: "revoked",
          },
          { id: "unknown", title: "Unknown visibility" },
        ],
        page: { next_cursor: "more-private-data", has_more: true },
      },
    });
  });
  await page.goto("/commons");
  await expect(page.getByRole("alert")).toContainText(
    "Knoten konnten nicht geladen werden",
  );
  await expect(
    page.getByRole("button", { name: "Weitere Knoten" }),
  ).toHaveCount(0);
  await expect(page.getByText("Invalid public page")).toHaveCount(0);
  for (const title of [
    "Private title",
    "Hidden title",
    "Revoked title",
    "Unknown visibility",
  ]) {
    await expect(page.getByText(title)).toHaveCount(0);
  }
  expect(publicRequests).toBe(1);
});

test("create, search, link, open and edit Commons without a map location", async ({
  page,
}) => {
  await mockApiResponses(page, {
    auth: { authenticated: true, account_id: "e2e-weber", role: "weber" },
  });
  let node = {
    id: "digital-commons",
    title: "Digital Commons",
    kind: "resource",
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
      expect(payload.kind).toBe("resource");
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
  await page.selectOption("#nodeType", "resource");
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
