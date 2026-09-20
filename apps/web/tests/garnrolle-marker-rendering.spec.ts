import { devices, expect, test } from "@playwright/test";
import {
  MAP_MARKER_MAX_SCALE,
  MAP_MARKER_MIN_SCALE,
  MAP_MARKER_REFERENCE_ZOOM,
  MAP_MAX_ZOOM,
  MAP_MIN_ZOOM,
} from "../src/lib/map/markerScale";
import type { Webgemeindezentrum } from "../src/lib/map/types";
import { mockApiResponses, mockListResponse } from "./fixtures/mockApi";

const GARNROLLE_ID = "7d97a42e-3704-4a33-a61f-0e0a6b4d65d8";
const KNOTEN_ID = "b52be17c-4ab7-4434-98ce-520f86290cf0";
const WEBGEMEINDEZENTRUM_ID = "webgemeindezentrum-hammer-park";
const SCALE_ALL_TEXTILE_OBJECTS_TEST =
  "scales all textile objects with zoom while their touch anchors stay stable";
const WEBGEMEINDEZENTRUM: Webgemeindezentrum = {
  type: "webgemeindezentrum",
  id: WEBGEMEINDEZENTRUM_ID,
  title: "Webgemeindezentrum Hammer Park",
  ortsweberei: {
    id: "ortsweberei-hamm",
    slug: "hamm",
    name: "Ortsweberei Hamm",
    gewebezelle_id: "hamm.weltgewebe.net",
  },
  location_state: "desired",
  location_state_label: "Gewünschter Treffort",
  faden_endpoint_id: "22222222-2222-5222-8222-222222222222",
  conversation_id: "33333333-3333-5333-8333-333333333333",
  location: { lat: 53.5585, lon: 10.058 },
  location_label: "Hammer Park – gewünschter Treffpunkt auf der Grünfläche",
  meeting_note:
    "Ein bewusst gewählter öffentlicher Treffpunkt, an dem die Ortsweberei tatsächlich zusammenkommen kann. Die genaue Stelle kann später gemeinsam präzisiert werden.",
  access_note:
    "Gewünschter Treffort: Nutzung, Barrierefreiheit und regelmäßige Verfügbarkeit sind noch nicht bestätigt.",
  created_at: "2026-08-02T10:08:00.000Z",
  updated_at: "2026-08-02T10:08:00.000Z",
};
const IPAD_PRO_11_LANDSCAPE = {
  userAgent: devices["iPad Pro 11 landscape"].userAgent,
  viewport: devices["iPad Pro 11 landscape"].viewport,
  deviceScaleFactor: devices["iPad Pro 11 landscape"].deviceScaleFactor,
  isMobile: devices["iPad Pro 11 landscape"].isMobile,
  hasTouch: devices["iPad Pro 11 landscape"].hasTouch,
};

test.describe("Garnrolle marker rendering", () => {
  test.beforeEach(async ({ page }, testInfo) => {
    await mockApiResponses(page);
    if (testInfo.title === SCALE_ALL_TEXTILE_OBJECTS_TEST) {
      await page.route("**/api/webgemeindezentren**", async (route) => {
        const pathname = new URL(route.request().url()).pathname;
        if (pathname === "/api/webgemeindezentren") {
          await route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify(
              mockListResponse(route.request().url(), [WEBGEMEINDEZENTRUM]),
            ),
          });
          return;
        }
        await route.fallback();
      });
    }
    await page.goto(`/map?focus=garnrolle:${GARNROLLE_ID}`);
  });

  test("uses a reset button and a loaded intrinsic image", async ({ page }) => {
    const marker = page.getByTestId(`marker-garnrolle-${GARNROLLE_ID}`);
    await expect(marker).toBeVisible();
    await expect(marker).toHaveCSS("appearance", "none");
    await expect(marker).toHaveCSS("padding", "0px");
    await expect(marker).toHaveCSS("width", "44px");
    await expect(marker).toHaveCSS("height", "44px");
    await expect(marker).toHaveCSS("background-image", "none");
    await expect(marker).toHaveCSS("transition-duration", "0s");

    const visual = marker.locator(".map-marker__visual");
    await expect(visual).toHaveCount(1);
    await expect(visual).toHaveCSS("transition-property", /scale.*transform/);

    const icon = marker.locator("img.marker-account__icon");
    await expect(icon).toHaveCount(1);
    await expect(icon).toHaveAttribute("alt", "");
    await expect(icon).toHaveAttribute("aria-hidden", "true");
    await expect(icon).toHaveJSProperty("complete", true);
    await expect(icon).toHaveJSProperty("naturalWidth", 256);
    await expect(icon).toHaveJSProperty("naturalHeight", 255);
    await expect(icon).toHaveCSS("width", "44px");
    await expect(icon).toHaveCSS("height", "44px");
    await expect(icon).toHaveCSS("object-fit", "contain");
  });

  test("renders Knoten as woven bodies instead of generic dots", async ({
    page,
  }) => {
    const marker = page.getByTestId(`marker-node-${KNOTEN_ID}`);
    const visual = marker.locator(".marker-node__visual");
    const body = visual.locator(".woven-node");
    await expect(marker).toHaveCSS("outline-style", "none");
    await expect(marker).toHaveCSS("width", "44px");
    await expect(marker).toHaveCSS("height", "44px");
    await expect(visual).toHaveCSS("width", "46px");
    await expect(visual).toHaveCSS("height", "46px");
    await expect(visual).toHaveCSS("border-top-style", "none");
    await expect(body).toHaveCount(1);
    await expect(body).toHaveAttribute(
      "data-zone-order",
      "knotting,conversation,proposal,vote",
    );
    // X body is not a bullseye disc; no circular marker box.
    await expect(body).toHaveCSS("border-radius", "0px");
    await expect(body).toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
    await expect(body.locator('[data-zone="knotting"]')).toHaveCount(1);
    await expect(body.locator('[data-zone="conversation"]')).toHaveCount(1);

    await expect(body).toHaveAttribute("data-x-geometry", "diagonal");
    const xGeometry = await body
      .locator(".woven-node__x")
      .evaluate((element) => {
        const arms = Array.from(
          element.querySelectorAll<HTMLElement>(".woven-node__arm"),
        ).map((arm) => {
          const style = getComputedStyle(arm);
          return {
            arm: arm.dataset.arm,
            transform: style.transform,
            width: Number.parseFloat(style.width),
            height: Number.parseFloat(style.height),
          };
        });
        const strands = Array.from(
          element.querySelectorAll<HTMLElement>(".woven-node__strand"),
        ).map((strand) => ({
          strand: strand.dataset.strand,
          zIndex: Number.parseInt(getComputedStyle(strand).zIndex, 10),
        }));
        return { arms, strands };
      });
    expect(xGeometry.arms.map((arm) => arm.arm).sort()).toEqual([
      "northeast",
      "northwest",
      "southeast",
      "southwest",
    ]);
    // Diagonal X: each arm is an elongated rotated band — never an axis-aligned plus.
    for (const arm of xGeometry.arms) {
      expect(arm.height).toBeGreaterThan(arm.width);
      expect(arm.transform).not.toBe("none");
      expect(arm.transform).toMatch(/matrix/);
    }
    const under = xGeometry.strands.find((strand) => strand.strand === "a");
    const over = xGeometry.strands.find((strand) => strand.strand === "b");
    expect(under && over).toBeTruthy();
    expect((over?.zIndex ?? 0) > (under?.zIndex ?? 0)).toBe(true);

    // No separate crossing blob: the four-arm X is the whole knot.
    await expect(body.locator(".woven-node__crossing")).toHaveCount(0);
    await expect(body.locator(".woven-node__cross")).toHaveCount(0);

    const halo = marker.locator(".map-marker__halo");
    await expect(halo).toHaveCSS("width", "68px");
    await marker.focus();
    await expect(halo).toHaveCSS("opacity", "1");
    const haloExtendsBeyondBody = await marker.evaluate((element) => {
      const haloBox = element
        .querySelector<HTMLElement>(".map-marker__halo")!
        .getBoundingClientRect();
      const bodyBox = element
        .querySelector<HTMLElement>(".marker-node__visual")!
        .getBoundingClientRect();
      return haloBox.width > bodyBox.width && haloBox.height > bodyBox.height;
    });
    expect(haloExtendsBeyondBody).toBe(true);
  });

  test("uses round textile haloes instead of rectangular marker and title boxes", async ({
    page,
  }) => {
    const marker = page.getByTestId(`marker-garnrolle-${GARNROLLE_ID}`);
    await expect(marker).toHaveClass(/is-selected/);
    await expect(marker).toHaveCSS("outline-style", "none");
    await expect(marker).toHaveCSS("box-shadow", "none");

    const halo = marker.locator(".map-marker__halo");
    await expect(halo).toHaveCount(1);
    await expect(halo).toHaveCSS("width", "42px");
    await expect(halo).toHaveCSS("height", "42px");
    await expect(halo).toHaveCSS("border-radius", "50%");
    await expect(halo).toHaveCSS("opacity", "1");

    const heading = page.getByTestId("account-heading");
    await expect(heading).toBeFocused();
    await expect(heading).toHaveCSS("outline-style", "none");
  });

  test("keeps the selected halo static when reduced motion is requested", async ({
    page,
  }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.reload();

    const marker = page.getByTestId(`marker-garnrolle-${GARNROLLE_ID}`);
    const halo = marker.locator(".map-marker__halo");
    const visual = marker.locator(".map-marker__visual");
    const icon = marker.locator(".marker-account__icon");
    await expect(halo).toHaveCSS("transition-duration", "0s");
    await expect(visual).toHaveCSS("transition-duration", "0s");
    await expect(icon).toHaveCSS("transition-duration", "0s");
    await expect(halo).toHaveCSS("opacity", "1");
  });

  test("stays locked to its projected coordinate during a map jump", async ({
    page,
  }) => {
    const marker = page.getByTestId(`marker-garnrolle-${GARNROLLE_ID}`);
    await expect(marker).toBeVisible();

    await page.waitForFunction(() => {
      const map = (
        window as typeof window & { __TEST_MAP__?: { isMoving(): boolean } }
      ).__TEST_MAP__;
      return Boolean(map && !map.isMoving());
    });

    const errorPx = await page.evaluate(
      async ({ markerId }) => {
        const map = (
          window as typeof window & {
            __TEST_MAP__?: {
              getCenter(): { lng: number; lat: number };
              jumpTo(options: { center: [number, number] }): void;
              project(lngLat: [number, number]): { x: number; y: number };
            };
          }
        ).__TEST_MAP__;
        const markerElement = document.querySelector<HTMLElement>(
          `[data-testid="marker-garnrolle-${markerId}"]`,
        );
        if (!map || !markerElement) {
          throw new Error("test map or marker unavailable");
        }

        const center = map.getCenter();
        map.jumpTo({ center: [center.lng + 0.02, center.lat + 0.01] });

        // Two frames allow MapLibre to render the new camera position. A CSS
        // transition on the outer marker would still be visibly catching up.
        await new Promise<void>((resolve) =>
          requestAnimationFrame(() => resolve()),
        );
        await new Promise<void>((resolve) =>
          requestAnimationFrame(() => resolve()),
        );

        const projected = map.project([10.0629844, 53.5604148]);
        const rect = markerElement.getBoundingClientRect();
        // Center anchor: map coordinate is the geometric midpoint of the spool.
        const actualAnchor = {
          x: rect.left + rect.width / 2,
          y: rect.top + rect.height / 2,
        };
        return Math.hypot(
          actualAnchor.x - projected.x,
          actualAnchor.y - projected.y,
        );
      },
      { markerId: GARNROLLE_ID },
    );

    expect(errorPx).toBeLessThanOrEqual(2);
  });

  test(SCALE_ALL_TEXTILE_OBJECTS_TEST, async ({ page }) => {
    const garnrolle = page.getByTestId(`marker-garnrolle-${GARNROLLE_ID}`);
    await expect(garnrolle).toBeVisible();

    const metrics = await page.evaluate(
      async ({
        garnrolleId,
        nodeId,
        centerId,
        minZoom,
        referenceZoom,
        maxZoom,
      }) => {
        type TestMap = {
          getMinZoom(): number;
          getMaxZoom(): number;
          getZoom(): number;
          jumpTo(options: { zoom: number }): void;
        };
        const map = (window as typeof window & { __TEST_MAP__?: TestMap })
          .__TEST_MAP__;
        if (!map) throw new Error("test map unavailable");

        const targets = [
          {
            kind: "garnrolle",
            testId: `marker-garnrolle-${garnrolleId}`,
            baseSize: 44,
          },
          {
            kind: "node",
            testId: `marker-node-${nodeId}`,
            baseSize: 46,
          },
          {
            kind: "webgemeindezentrum",
            testId: `marker-webgemeindezentrum-${centerId}`,
            baseSize: 52,
          },
        ];

        const settle = async () => {
          // Wait for all marker artwork to exist before observing transitions.
          // The old fixed 220 ms sleep mixed initial render latency with the
          // 180 ms CSS scale transition and became flaky on busy CI runners.
          const deadline = performance.now() + 2_000;
          let visuals: HTMLElement[] = [];
          while (performance.now() < deadline) {
            const candidates = targets.map(({ testId }) =>
              document
                .querySelector<HTMLElement>(`[data-testid="${testId}"]`)
                ?.querySelector<HTMLElement>(".map-marker__visual"),
            );
            if (
              candidates.every(
                (visual) => visual !== null && visual !== undefined,
              )
            ) {
              visuals = candidates as HTMLElement[];
              break;
            }
            await new Promise<void>((resolve) =>
              requestAnimationFrame(() => resolve()),
            );
          }
          if (visuals.length !== targets.length) {
            throw new Error(
              "marker artwork did not render before transition wait",
            );
          }

          // Give the zoom-driven scale write a render turn, then wait for the
          // browser's actual CSS transitions instead of guessing wall time.
          await new Promise<void>((resolve) =>
            requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
          );
          const animations = visuals.flatMap((visual) =>
            visual.getAnimations(),
          );
          await Promise.all(animations.map((animation) => animation.finished));
          await new Promise<void>((resolve) =>
            requestAnimationFrame(() => resolve()),
          );
        };
        const measure = () => {
          const objects = targets.map(({ kind, testId, baseSize }) => {
            const outerElement = document.querySelector<HTMLElement>(
              `[data-testid="${testId}"]`,
            );
            const visual = outerElement?.querySelector<HTMLElement>(
              ".map-marker__visual",
            );
            const halo =
              outerElement?.querySelector<HTMLElement>(".map-marker__halo");
            if (!outerElement || !visual || !halo) {
              throw new Error(`${kind} marker artwork unavailable`);
            }
            const outer = outerElement.getBoundingClientRect();
            const artwork = visual.getBoundingClientRect();
            const haloBox = halo.getBoundingClientRect();
            const outerCx = outer.left + outer.width / 2;
            const outerCy = outer.top + outer.height / 2;
            const artworkCx = artwork.left + artwork.width / 2;
            const artworkCy = artwork.top + artwork.height / 2;
            return {
              kind,
              baseSize,
              outerWidth: outer.width,
              outerHeight: outer.height,
              artworkWidth: artwork.width,
              artworkHeight: artwork.height,
              haloWidth: haloBox.width,
              worldScale: Number.parseFloat(
                getComputedStyle(visual).getPropertyValue("--map-object-scale"),
              ),
              inlineWorldScale:
                visual.style.getPropertyValue("--map-object-scale"),
              outerIndividualScale: getComputedStyle(outerElement).scale,
              centerDelta: Math.hypot(outerCx - artworkCx, outerCy - artworkCy),
            };
          });
          return { zoom: map.getZoom(), objects };
        };

        map.jumpTo({ zoom: referenceZoom });
        await settle();
        const reference = measure();

        map.jumpTo({ zoom: maxZoom });
        await settle();
        const near = measure();

        map.jumpTo({ zoom: minZoom });
        await settle();
        const regional = measure();

        return {
          minZoom: map.getMinZoom(),
          maxZoom: map.getMaxZoom(),
          reference,
          near,
          regional,
        };
      },
      {
        garnrolleId: GARNROLLE_ID,
        nodeId: KNOTEN_ID,
        centerId: WEBGEMEINDEZENTRUM_ID,
        minZoom: MAP_MIN_ZOOM,
        referenceZoom: MAP_MARKER_REFERENCE_ZOOM,
        maxZoom: MAP_MAX_ZOOM,
      },
    );

    expect(metrics.minZoom).toBe(MAP_MIN_ZOOM);
    expect(metrics.maxZoom).toBe(MAP_MAX_ZOOM);
    expect(metrics.reference.zoom).toBeCloseTo(MAP_MARKER_REFERENCE_ZOOM, 5);
    expect(metrics.near.zoom).toBeCloseTo(MAP_MAX_ZOOM, 5);
    expect(metrics.regional.zoom).toBeCloseTo(MAP_MIN_ZOOM, 5);

    for (let index = 0; index < metrics.reference.objects.length; index += 1) {
      const reference = metrics.reference.objects[index];
      const near = metrics.near.objects[index];
      const regional = metrics.regional.objects[index];
      expect(near.kind).toBe(reference.kind);
      expect(regional.kind).toBe(reference.kind);

      for (const stage of [reference, near, regional]) {
        expect(stage.outerWidth).toBeCloseTo(44, 1);
        expect(stage.outerHeight).toBeCloseTo(44, 1);
        expect(stage.outerIndividualScale).toBe("none");
        expect(stage.inlineWorldScale).toBe("");
        expect(stage.centerDelta).toBeLessThanOrEqual(0.5);
      }

      expect(reference.worldScale).toBeCloseTo(1, 3);
      expect(regional.worldScale).toBeCloseTo(MAP_MARKER_MIN_SCALE, 3);
      expect(near.worldScale).toBeCloseTo(MAP_MARKER_MAX_SCALE, 3);
      expect(reference.artworkWidth).toBeCloseTo(reference.baseSize, 1);
      expect(regional.artworkWidth / reference.artworkWidth).toBeCloseTo(
        MAP_MARKER_MIN_SCALE,
        2,
      );
      expect(near.artworkWidth / reference.artworkWidth).toBeCloseTo(
        MAP_MARKER_MAX_SCALE,
        2,
      );
      expect(regional.haloWidth).toBeCloseTo(reference.haloWidth, 1);
      expect(near.haloWidth).toBeCloseTo(reference.haloWidth, 1);
    }

    // Interaction transform and world scale are independent transform layers.
    const node = page.getByTestId(`marker-node-${KNOTEN_ID}`);
    const interaction = await node.evaluate(async (element) => {
      const visual = element.querySelector<HTMLElement>(".map-marker__visual");
      if (!visual) throw new Error("node marker artwork unavailable");

      const measure = () => ({
        artworkWidth: visual.getBoundingClientRect().width,
        outerWidth: element.getBoundingClientRect().width,
        worldScale: getComputedStyle(visual)
          .getPropertyValue("--map-object-scale")
          .trim(),
        inlineWorldScale: visual.style.getPropertyValue("--map-object-scale"),
      });
      const before = measure();

      element.classList.add("is-selected");
      try {
        await new Promise<void>((resolve) =>
          requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
        );
        await Promise.all(
          visual.getAnimations().map((animation) => animation.finished),
        );
        await new Promise<void>((resolve) =>
          requestAnimationFrame(() => resolve()),
        );
        return { before, selected: measure() };
      } finally {
        element.classList.remove("is-selected");
      }
    });

    expect(
      interaction.selected.artworkWidth / interaction.before.artworkWidth,
    ).toBeCloseTo(1.13, 1);
    expect(interaction.before.outerWidth).toBeCloseTo(44, 1);
    expect(interaction.selected.outerWidth).toBeCloseTo(44, 1);
    expect(interaction.selected.worldScale).toBe(interaction.before.worldScale);
    expect(interaction.before.inlineWorldScale).toBe("");
    expect(interaction.selected.inlineWorldScale).toBe("");
    await expect(node).not.toHaveClass(/is-selected/);
  });
});

test.describe("iPad Pro 11 landscape Garnrolle interaction", () => {
  test.use(IPAD_PRO_11_LANDSCAPE);

  test("keeps the touch target invisible and the selected state circular", async ({
    page,
  }) => {
    await mockApiResponses(page);
    await page.goto("/map");
    await page.waitForFunction(
      () => (window as any).__TEST_MAP__ !== undefined,
      undefined,
      { timeout: 15000 },
    );
    await page.evaluate(() => {
      const map = (window as any).__TEST_MAP__;
      map.jumpTo({ center: [10.0629844, 53.5604148], zoom: 14 });
    });

    const marker = page.getByTestId(`marker-garnrolle-${GARNROLLE_ID}`);
    await expect(marker).toBeVisible();
    await expect(marker).toHaveCSS("width", "44px");
    await expect(marker).toHaveCSS("height", "44px");
    await expect(marker).toHaveCSS("outline-style", "none");
    await expect(marker).toHaveCSS("box-shadow", "none");

    await marker.tap();
    await expect(page.getByTestId("account-heading")).toBeVisible();
    await expect(marker).toHaveClass(/is-selected/);

    const halo = marker.locator(".map-marker__halo");
    await expect(halo).toHaveCSS("border-radius", "50%");
    await expect(halo).toHaveCSS("opacity", "1");

    const hasHorizontalOverflow = await page.evaluate(
      () =>
        document.documentElement.scrollWidth >
        document.documentElement.clientWidth,
    );
    expect(hasHorizontalOverflow).toBe(false);
  });
});
