import { expect, test } from "@playwright/test";
import {
  demoAccounts,
  demoNodes,
  demoWebgemeindezentren,
} from "../src/lib/demo/demoData";
import { mockApiResponses, mockListResponse } from "./fixtures/mockApi";
import { waitForMapReady } from "./fixtures/mapReady";

const anchor = demoNodes[0].location;
const otherNodeId = "8f8a1d16-0fcb-4354-a27b-b7cfe14e121e";
const nearbyNodeId = "aae87a58-6548-4467-a4f5-399c9406df85";
const secondNode = {
  ...demoNodes[0],
  id: otherNodeId,
  title: "Zweites öffentliches Gut",
};
const nearbyNode = {
  ...demoNodes[0],
  id: nearbyNodeId,
  title: "Naher, anderer Ort",
  location: { lat: anchor.lat + 0.018, lon: anchor.lon + 0.018 },
};
const account = {
  ...demoAccounts[0],
  public_pos: { ...anchor },
  location: { ...anchor },
};
const center = { ...demoWebgemeindezentren[0], location: { ...anchor } };

async function overlappingMap(page: import("@playwright/test").Page) {
  await mockApiResponses(page);
  await page.route("**/api/nodes*", async (route) => {
    if (
      route.request().method() !== "GET" ||
      new URL(route.request().url()).pathname !== "/api/nodes"
    ) {
      await route.fallback();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(
        mockListResponse(route.request().url(), [
          demoNodes[0],
          secondNode,
          nearbyNode,
        ]),
      ),
    });
  });
  await page.route("**/api/accounts*", async (route) => {
    if (
      route.request().method() !== "GET" ||
      new URL(route.request().url()).pathname !== "/api/accounts"
    ) {
      await route.fallback();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(mockListResponse(route.request().url(), [account])),
    });
  });
  await page.route("**/api/webgemeindezentren*", async (route) => {
    if (
      route.request().method() !== "GET" ||
      new URL(route.request().url()).pathname !== "/api/webgemeindezentren"
    ) {
      await route.fallback();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(mockListResponse(route.request().url(), [center])),
    });
  });
  await page.goto("/map");
  await waitForMapReady(page);
  await expect(
    page.getByTestId(`marker-node-${demoNodes[0].id}`),
  ).toBeInViewport({
    ratio: 0.5,
  });
}

async function anchorScreenPoint(page: import("@playwright/test").Page) {
  return await page.evaluate(({ lat, lon }) => {
    const map = window.__TEST_MAP__ as unknown as
      | { project(coords: [number, number]): { x: number; y: number } }
      | undefined;
    if (!map) throw new Error("no map");
    const marker = map.project([lon, lat]);
    const box = document.querySelector("#map")?.getBoundingClientRect();
    if (!box) throw new Error("no viewport");
    return { x: box.x + marker.x, y: box.y + marker.y };
  }, anchor);
}

test("real pointer selects each co-located node, Garnrolle and center through one chooser", async ({
  page,
}) => {
  await overlappingMap(page);
  const point = await anchorScreenPoint(page);
  const targetAtPoint = await page.evaluate(
    ({ x, y }) =>
      document
        .elementFromPoint(x, y)
        ?.closest(".map-marker")
        ?.getAttribute("data-id"),
    point,
  );
  expect([
    demoNodes[0].id,
    otherNodeId,
    nearbyNodeId,
    account.id,
    center.id,
  ]).toContain(targetAtPoint);

  await page.mouse.click(point.x, point.y);
  const chooser = page.getByTestId("map-object-chooser");
  await expect(chooser).toBeVisible();
  for (const id of [demoNodes[0].id, otherNodeId, account.id, center.id]) {
    await expect(page.getByTestId(`choose-map-object-${id}`)).toBeVisible();
  }
  await expect(
    chooser.locator("button.map-object-chooser__item").first(),
  ).toBeFocused();
  await page.getByTestId(`choose-map-object-${otherNodeId}`).click();
  await expect(chooser).toHaveCount(0);
  await expect(page.getByTestId("context-panel")).toContainText(
    secondNode.title,
  );

  // Same-screen hit-box collision does not create a new geographic address.
  const canvasNode = page.getByTestId(`marker-node-${otherNodeId}`);
  await expect(canvasNode).toHaveAttribute("data-id", otherNodeId);
});

test("choosing an overlap restores marker keyboard focus after closing the panel", async ({
  page,
}) => {
  await overlappingMap(page);
  const point = await anchorScreenPoint(page);
  await page.mouse.click(point.x, point.y);
  const chooser = page.getByTestId("map-object-chooser");
  await expect(chooser).toBeVisible();
  const choice = page.getByTestId(`choose-map-object-${otherNodeId}`);
  await choice.focus();
  await page.keyboard.press("Enter");
  const panel = page.getByTestId("context-panel");
  await expect(panel).toContainText(secondNode.title);
  await expect(chooser).toHaveCount(0);
  await page.keyboard.press("Escape");
  await expect(panel).toHaveCount(0);
  await expect(page.getByTestId(`marker-node-${otherNodeId}`)).toBeFocused();
});

test("zoom separates nearby locations; Escape and keyboard access work", async ({
  page,
}) => {
  await overlappingMap(page);
  const point = await anchorScreenPoint(page);
  await page.mouse.click(point.x, point.y);
  const chooser = page.getByTestId("map-object-chooser");
  await expect(chooser).toBeVisible();

  // Escape restores focus to the original mapped marker.
  await page.keyboard.press("Escape");
  await expect(chooser).toHaveCount(0);

  await page.evaluate(({ lat, lon }) => {
    window.__TEST_MAP__?.jumpTo({ center: [lon, lat], zoom: 16 });
  }, anchor);
  await expect(
    page.getByTestId(`marker-node-${demoNodes[0].id}`),
  ).toBeInViewport({
    ratio: 0.5,
  });
  const zoomed = await anchorScreenPoint(page);
  await page.mouse.click(zoomed.x, zoomed.y);
  await expect(chooser).toBeVisible();
  await expect(
    page.getByTestId(`choose-map-object-${nearbyNodeId}`),
  ).toHaveCount(0);
  await page.keyboard.press("Enter");
  await expect(chooser).toHaveCount(0);
  await expect(page.getByTestId("context-panel")).toBeVisible();
});

test("panning the map closes an obsolete screen-space object choice", async ({
  page,
}) => {
  await overlappingMap(page);
  const point = await anchorScreenPoint(page);
  await page.mouse.click(point.x, point.y);
  const chooser = page.getByTestId("map-object-chooser");
  await expect(chooser).toBeVisible();
  await page.evaluate(() => {
    window.__TEST_MAP__?.jumpTo({ center: [12.5, 50], zoom: 5 });
  });
  await expect(chooser).toHaveCount(0);
});

test("focused marker Enter opens its detail directly despite nearby overlapping objects", async ({
  page,
}) => {
  await overlappingMap(page);
  const marker = page.getByTestId(`marker-node-${demoNodes[0].id}`);
  await marker.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("context-panel")).toContainText(
    demoNodes[0].title,
  );
  await expect(page.getByTestId("map-object-chooser")).toHaveCount(0);
});

test.describe("touchscreen emulation, not physical iPad Safari", () => {
  test.use({ hasTouch: true, viewport: { width: 820, height: 1180 } });

  test("touch on a covered marker exposes all authorized object choices", async ({
    page,
  }) => {
    await overlappingMap(page);
    const point = await anchorScreenPoint(page);
    await page.touchscreen.tap(point.x, point.y);
    const chooser = page.getByTestId("map-object-chooser");
    await expect(chooser).toBeVisible();
    await page.getByTestId(`choose-map-object-${account.id}`).tap();
    await expect(chooser).toHaveCount(0);
    await expect(page.getByTestId("context-panel")).toContainText(
      account.title,
    );
  });
});
