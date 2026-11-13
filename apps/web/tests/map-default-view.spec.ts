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
  expect(camera.zoom).toBeCloseTo(7, 3);
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
  expect(camera.zoom).toBeCloseTo(7, 3);
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
  expect(camera.zoom).toBeCloseTo(7, 3);
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
  expect(initialCamera.zoom).toBeCloseTo(7, 3);
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
