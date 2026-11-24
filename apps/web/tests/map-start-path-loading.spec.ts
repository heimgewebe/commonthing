import { readFileSync } from "node:fs";
import { expect, test } from "@playwright/test";
import { mockApiResponses } from "./fixtures/mockApi";
import { centerDemoNode } from "./fixtures/mapCamera";
import { activateToolFanAction } from "./fixtures/toolFan";

type ManifestRecord = {
  file?: string;
  name?: string;
};

function emittedFile(
  manifest: Record<string, ManifestRecord>,
  identity: { source: string; name: string },
) {
  const direct = manifest[identity.source]?.file;
  if (direct) return direct;

  const matches = Object.values(manifest).filter(
    (record) =>
      record.name === identity.name && typeof record.file === "string",
  );
  if (matches.length !== 1) {
    throw new Error(
      `expected one manifest entry for ${identity.source} (${identity.name}), found ${matches.length}`,
    );
  }
  return matches[0].file as string;
}

test("keeps interaction-only map chunks outside the startup request path", async ({
  page,
}) => {
  await mockApiResponses(page);
  const requested = new Set<string>();
  page.on("request", (request) => {
    requested.add(new URL(request.url()).pathname.replace(/^\//, ""));
  });

  await page.goto("/map");
  await page.waitForFunction(
    () => Boolean((window as any).__TEST_MAP__),
    null,
    {
      timeout: 15_000,
    },
  );
  await centerDemoNode(page);
  const marker = page
    .locator('.map-marker[data-marker-category="node"]')
    .first();
  await expect(marker).toBeVisible();

  const manifest = JSON.parse(
    readFileSync(".svelte-kit/output/client/.vite/manifest.json", "utf8"),
  ) as Record<string, ManifestRecord>;
  const searchFile = emittedFile(manifest, {
    source: "src/lib/components/SearchOverlay.svelte",
    name: "SearchOverlay",
  });
  const contextFile = emittedFile(manifest, {
    source: "src/lib/components/ContextPanel.svelte",
    name: "ContextPanel",
  });

  expect(requested.has(searchFile)).toBe(false);
  expect(requested.has(contextFile)).toBe(false);

  await activateToolFanAction(page, "find");
  await expect(page.getByTestId("search-overlay")).toBeVisible();
  await expect.poll(() => requested.has(searchFile)).toBe(true);

  await page.keyboard.press("Escape");
  await marker.click();
  await expect(page.getByTestId("context-panel")).toBeVisible();
  await expect.poll(() => requested.has(contextFile)).toBe(true);
});

test("replaces the loading state with a recoverable error when the map runtime chunk fails", async ({
  page,
}) => {
  await mockApiResponses(page);

  const manifest = JSON.parse(
    readFileSync(".svelte-kit/output/client/.vite/manifest.json", "utf8"),
  ) as Record<string, ManifestRecord>;
  const nodesFile = emittedFile(manifest, {
    source: "src/lib/map/overlay/nodes.ts",
    name: "nodes",
  });

  // The list must stay usable independently of the map runtime.
  await page.route("**/api/public/nodes*", (route) =>
    route.fulfill({
      json: {
        items: [
          {
            id: "fallback-node",
            title: "Gemeinschaftswerkstatt",
            kind: "Project",
            summary: "Ein gemeinschaftliches Vorhaben",
            location: null,
            search_visibility: "public",
          },
        ],
        page: { limit: 50, next_cursor: null, has_more: false },
      },
    }),
  );

  // The map runtime is imported early; failing it must not strand the overlay.
  await page.route(`**/${nodesFile}`, (route) => route.abort());

  await page.goto("/map");

  const error = page.getByTestId("map-init-error");
  await expect(error).toBeVisible({ timeout: 15_000 });
  await expect(error).toHaveAttribute("role", "alert");
  await expect(page.getByTestId("map-init-error-retry")).toBeVisible();
  await expect(page.locator(".loading-overlay")).toHaveCount(0);

  const listLink = error.getByRole("link", {
    name: "Einträge als Liste öffnen",
  });
  await expect(listLink).toHaveAttribute("href", "/commons");
  await listLink.focus();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/commons\/?$/);
  await expect(
    page.getByRole("link", { name: "Gemeinschaftswerkstatt" }),
  ).toBeVisible();
});
