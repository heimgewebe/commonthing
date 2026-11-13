import { test, expect } from "@playwright/test";
import { mockApiResponses } from "./fixtures/mockApi";

test("guest map starts on neutral nationwide Germany even when content exists", async ({
  page,
}) => {
  await mockApiResponses(page);

  await page.goto("/map");
  await page.waitForFunction(
    () => (window as any).__TEST_MAP__ !== undefined,
    undefined,
    { timeout: 15000 },
  );

  const camera = await page.evaluate(() => {
    const map = (window as any).__TEST_MAP__;
    const center = map.getCenter();
    return { lng: center.lng, lat: center.lat, zoom: map.getZoom() };
  });

  expect(camera.lng).toBeCloseTo(10.4515, 3);
  expect(camera.lat).toBeCloseTo(51.1657, 3);
  expect(camera.zoom).toBeCloseTo(5, 3);

  // Visibility must mean on-screen, not just nonzero CSS dimensions:
  // previously the Hamburg node button was rendered hundreds of pixels above
  // the viewport and Playwright's toBeVisible() would still accept it.
  await expect(
    page.locator('.map-marker[data-marker-category="node"]').first(),
  ).toBeInViewport({ ratio: 0.5 });
});

test("partial guest data uses the neutral basemap fallback", async ({
  page,
}) => {
  await mockApiResponses(page);
  await page.route("**/api/accounts*", async (route) => {
    await route.fulfill({
      status: 503,
      contentType: "text/plain",
      body: "Service Unavailable",
    });
  });

  await page.goto("/map");
  await page.waitForFunction(
    () => (window as any).__TEST_MAP__ !== undefined,
    undefined,
    { timeout: 15000 },
  );
  await expect(page.getByTestId("load-state-partial")).toBeVisible();

  const camera = await page.evaluate(() => {
    const map = (window as any).__TEST_MAP__;
    const center = map.getCenter();
    return { lng: center.lng, lat: center.lat, zoom: map.getZoom() };
  });

  expect(camera.lng).toBeCloseTo(10.4515, 3);
  expect(camera.lat).toBeCloseTo(51.1657, 3);
  expect(camera.zoom).toBeCloseTo(5, 3);
});

test("empty sovereign map starts on neutral nationwide Germany", async ({
  page,
}) => {
  await mockApiResponses(page);
  await page.route(
    /\/api\/(?:nodes|accounts|edges|webgemeindezentren)(?:\?|$)/,
    async (route) => {
      const limit = Number(
        new URL(route.request().url()).searchParams.get("limit") ?? 1000,
      );
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: [],
          page: { limit, next_cursor: null, has_more: false },
        }),
      });
    },
  );

  await page.goto("/map");
  await page.waitForFunction(
    () => (window as any).__TEST_MAP__ !== undefined,
    undefined,
    { timeout: 15000 },
  );

  const camera = await page.evaluate(() => {
    const map = (window as any).__TEST_MAP__;
    const center = map.getCenter();
    return { lng: center.lng, lat: center.lat, zoom: map.getZoom() };
  });

  expect(camera.lng).toBeCloseTo(10.4515, 3);
  expect(camera.lat).toBeCloseTo(51.1657, 3);
  expect(camera.zoom).toBeCloseTo(5, 3);
});

test("signed-in map starts on the own positioned Garnrolle", async ({
  page,
}) => {
  const ownAccountId = "7d97a42e-3704-4a33-a61f-0e0a6b4d65d8";
  await mockApiResponses(page, {
    auth: { authenticated: true, account_id: ownAccountId, role: "weber" },
  });

  await page.goto("/map");
  await page.waitForFunction(
    () => (window as any).__TEST_MAP__ !== undefined,
    undefined,
    { timeout: 15000 },
  );

  await page.waitForFunction(
    () => {
      const map = (window as any).__TEST_MAP__;
      if (!map) return false;
      const center = map.getCenter();
      return (
        Math.abs(center.lng - 10.0629844) < 0.0005 &&
        Math.abs(center.lat - 53.5604148) < 0.0005 &&
        map.getZoom() >= 14
      );
    },
    undefined,
    { timeout: 15000 },
  );

  await expect(
    page.getByTestId(`marker-garnrolle-${ownAccountId}`),
  ).toBeVisible();
  await expect(page.getByTestId("context-panel")).toHaveCount(0);
});

test("delayed authentication recenters once without blocking public map startup", async ({
  page,
}) => {
  const ownAccountId = "7d97a42e-3704-4a33-a61f-0e0a6b4d65d8";
  await mockApiResponses(page);
  let releaseAuth!: () => void;
  const authGate = new Promise<void>((resolve) => {
    releaseAuth = resolve;
  });
  await page.route("**/api/auth/me", async (route) => {
    await authGate;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        authenticated: true,
        account_id: ownAccountId,
        role: "weber",
      }),
    });
  });
  await page.goto("/map");
  await page.waitForFunction(
    () => (window as any).__TEST_MAP__ !== undefined,
    undefined,
    { timeout: 15000 },
  );
  const initialCamera = await page.evaluate(() => {
    const map = (window as any).__TEST_MAP__;
    const center = map.getCenter();
    return {
      lng: center.lng,
      lat: center.lat,
      zoom: map.getZoom(),
    };
  });
  expect(initialCamera.lng).toBeCloseTo(10.4515, 3);
  expect(initialCamera.lat).toBeCloseTo(51.1657, 3);
  expect(initialCamera.zoom).toBeCloseTo(5, 3);
  releaseAuth();
  await page.waitForFunction(
    () => {
      const map = (window as any).__TEST_MAP__;
      if (!map) return false;
      const center = map.getCenter();
      return (
        Math.abs(center.lng - 10.0629844) < 0.0005 &&
        Math.abs(center.lat - 53.5604148) < 0.0005 &&
        map.getZoom() >= 14
      );
    },
    undefined,
    { timeout: 15000 },
  );
});

test("camera movement remains sticky after returning to the initial view", async ({
  page,
}) => {
  const ownAccountId = "7d97a42e-3704-4a33-a61f-0e0a6b4d65d8";
  await mockApiResponses(page);
  let releaseAuth!: () => void;
  const authGate = new Promise<void>((resolve) => {
    releaseAuth = resolve;
  });
  await page.route("**/api/auth/me", async (route) => {
    await authGate;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        authenticated: true,
        account_id: ownAccountId,
        role: "weber",
      }),
    });
  });
  await page.goto("/map");
  await page.waitForFunction(
    () => (window as any).__TEST_MAP__ !== undefined,
    undefined,
    { timeout: 15000 },
  );
  const initialCamera = await page.evaluate(() => {
    const map = (window as any).__TEST_MAP__;
    const center = map.getCenter();
    return { lng: center.lng, lat: center.lat, zoom: map.getZoom() };
  });
  await page.evaluate((initial) => {
    const map = (window as any).__TEST_MAP__;
    map.jumpTo({ center: [10.071, 53.571], zoom: 13 });
    map.jumpTo({
      center: [initial.lng, initial.lat],
      zoom: initial.zoom,
      bearing: 15,
      pitch: 20,
    });
  }, initialCamera);
  releaseAuth();
  await expect(
    page.getByRole("link", { name: "Einstellungen öffnen" }),
  ).toBeVisible();
  const finalCamera = await page.evaluate(() => {
    const map = (window as any).__TEST_MAP__;
    const center = map.getCenter();
    return {
      lng: center.lng,
      lat: center.lat,
      zoom: map.getZoom(),
      bearing: map.getBearing(),
      pitch: map.getPitch(),
    };
  });
  expect(finalCamera.lng).toBeCloseTo(initialCamera.lng, 3);
  expect(finalCamera.lat).toBeCloseTo(initialCamera.lat, 3);
  expect(finalCamera.zoom).toBeCloseTo(initialCamera.zoom, 3);
  expect(finalCamera.bearing).toBeCloseTo(15, 3);
  expect(finalCamera.pitch).toBeCloseTo(20, 3);
});

test("a highlighted knot stays clickable above a collapsed household marker", async ({
  page,
}) => {
  await mockApiResponses(page);
  await page.goto("/map");

  const node = page.locator('.map-marker[data-marker-category="node"]').first();
  const account = page.locator(".map-marker.marker-account").first();
  await expect(node).toBeInViewport({ ratio: 0.5 });
  await expect(account).toBeVisible();

  // Reproduce the shared-location layering without relying on demo accounts
  // having exactly the same coordinates. Household stacks use inline z=30.
  const box = await node.boundingBox();
  expect(box).not.toBeNull();
  await account.evaluate((element, target) => {
    const original = element.getBoundingClientRect();
    element.style.translate = `${target.x - original.x}px ${target.y - original.y}px`;
    element.style.zIndex = "30";
  }, box!);

  const target = await node.evaluate((element) => {
    const rect = element.getBoundingClientRect();
    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
  });
  // Before highlighting, the collocated household must actually intercept
  // the node's center; otherwise this test would not reproduce the bug.
  const markerAtTarget = () =>
    page.evaluate(
      ({ x, y }) =>
        document
          .elementFromPoint(x, y)
          ?.closest("button.map-marker")
          ?.getAttribute("data-marker-category"),
      target,
    );
  expect(await markerAtTarget()).toBe("account");
  expect(
    await node.evaluate((element) => getComputedStyle(element).zIndex),
  ).toBe("auto");

  await node.evaluate((element) => element.classList.add("search-highlight"));
  await expect
    .poll(() =>
      node.evaluate((element) => Number(getComputedStyle(element).zIndex)),
    )
    .toBeGreaterThan(30);
  await expect
    .poll(async () =>
      page.evaluate(({ x, y }) => {
        const marker = document
          .elementFromPoint(x, y)
          ?.closest("button.map-marker");
        return marker?.getAttribute("data-marker-category");
      }, target),
    )
    .toBe("node");

  // This must be a real Playwright pointer click, not dispatchEvent().
  await node.click();
  await expect(page.getByTestId("context-panel")).toBeVisible();
});
