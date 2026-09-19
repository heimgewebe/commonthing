import { expect, test } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import { mockListResponse } from "../fixtures/mockApi";
import { LOCAL_BASEMAP_STYLE_VERSION } from "../../src/lib/map/basemapStyleVersion";

/**
 * Visual Runtime Proof: Real Hamburg PMTiles via MapLibre
 *
 * Proves the full end-to-end pipeline:
 *   Browser → Weltgewebe App → MapLibre → pmtiles:// → /local-basemap/ →
 *   Vite dev-server middleware → build/basemap/basemap-hamburg.pmtiles
 *
 * Two-part proof strategy:
 *
 *   1. SERVER RANGE CONTRACT:
 *      - Explicit direct Range request to /local-basemap/basemap-hamburg.pmtiles
 *      - Must return HTTP 206 Partial Content
 *      - Must include Accept-Ranges: bytes and Content-Range headers
 *      - Proves the Vite middleware correctly delivers Range-capable files
 *
 *   2. BROWSER/MAPLIBRE VISUAL CONTRACT:
 *      - MapLibre canvas visible with non-zero dimensions
 *      - MapLibre isStyleLoaded() returns true (via window.__TEST_MAP__)
 *      - ≥1 local PMTiles request observed (proves MapLibre is using the artifact)
 *      - Zero requests to external tile providers
 *      - Proves the full browser-side pipeline works end-to-end
 *
 * Environment: Requires Vite dev or preview server with
 * local-basemap-serve middleware in vite.config.ts active (configureServer or
 * configurePreviewServer hooks).
 * Run with: PLAYWRIGHT_SKIP_WEBSERVER=1 PORT=5173
 *
 * /local-basemap/style.json and /local-basemap/*.pmtiles are NOT mocked here.
 * Only /api/** and /_app/version.json are mocked (no backend server needed).
 */

const REAL_PMTILES_FILENAME = "basemap-hamburg.pmtiles";
const SOURCE_ID = "basemap";
const REGION_LAYER_IDS = [
  "landcover",
  "landuse",
  "water",
  "roads",
  "buildings",
  "place-labels",
];
const SOURCE_LAYER_IDS = [
  "landcover",
  "landuse",
  "transportation",
  "water",
  "place",
];

type TestMap = {
  isStyleLoaded?: () => boolean;
  isSourceLoaded?: (sourceId: string) => boolean;
  queryRenderedFeatures?: (options?: {
    layers?: string[];
  }) => Array<{ source?: string; layer?: { id?: string } }>;
  querySourceFeatures?: (
    sourceId: string,
    options?: { sourceLayer?: string },
  ) => Array<unknown>;
};

const FORBIDDEN_REMOTE_PROVIDERS = [
  "api.maptiler.com",
  "tiles.mapbox.com",
  "api.mapbox.com",
  "basemaps.cartocdn.com",
  "tile.openstreetmap.org",
  "stamen-tiles",
  "services.arcgisonline.com",
  "maps.googleapis.com",
];

test.describe("Basemap Real Hamburg Visual Runtime Proof", () => {
  test(
    "loads real Hamburg PMTiles artifact via MapLibre with HTTP 206 Range delivery",
    { tag: "@proof" },
    async ({ page }, testInfo) => {
      const buildBasemapDir = path.resolve(
        process.cwd(),
        "../../build/basemap",
      );
      const aliasPath = path.join(buildBasemapDir, REAL_PMTILES_FILENAME);
      expect(
        fs.existsSync(aliasPath),
        `NOT_PROVEN: missing required published PMTiles alias ${aliasPath}; run scripts/basemap/publish-basemap.sh first`,
      ).toBe(true);

      const pmtilesRequests: Array<{
        url: string;
        method: string;
        rangeHeader: string | null;
      }> = [];
      const pmtilesResponses: Array<{
        url: string;
        status: number;
        acceptRanges: string | null;
        contentRange: string | null;
      }> = [];
      const remoteViolations: string[] = [];
      const unexpectedApiRequests: string[] = [];
      const failedResponses: Array<{ url: string; status: number }> = [];
      const consoleErrors: string[] = [];

      page.on("console", (msg) => {
        if (msg.type() === "error") {
          consoleErrors.push(msg.text());
        }
      });

      // Record PMTiles network events
      page.on("request", (req) => {
        const url = req.url();
        if (url.includes(`/local-basemap/${REAL_PMTILES_FILENAME}`)) {
          pmtilesRequests.push({
            url,
            method: req.method(),
            rangeHeader: req.headers()["range"] ?? null,
          });
        }
        for (const provider of FORBIDDEN_REMOTE_PROVIDERS) {
          if (url.includes(provider)) {
            remoteViolations.push(url);
          }
        }
      });

      page.on("response", (res) => {
        const url = res.url();
        if (res.status() >= 400) {
          failedResponses.push({ url, status: res.status() });
        }
        if (url.includes(`/local-basemap/${REAL_PMTILES_FILENAME}`)) {
          pmtilesResponses.push({
            url,
            status: res.status(),
            acceptRanges: res.headers()["accept-ranges"] ?? null,
            contentRange: res.headers()["content-range"] ?? null,
          });
        }
      });

      await page.route("**/favicon.ico", (route) =>
        route.fulfill({ status: 204, body: "" }),
      );

      // Mock /_app/version.json to suppress UpdateBanner overlay
      await page.route("**/_app/version.json", (route) => {
        route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ version: "proof" }),
        });
      });

      // Mock /api/** — no backend server needed
      await page.route("**/api/**", async (route) => {
        const url = new URL(route.request().url());
        const pathname = url.pathname;
        if (pathname === "/api/nodes" || pathname.startsWith("/api/nodes/")) {
          return route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify(mockListResponse(route.request().url(), [])),
          });
        }
        if (
          pathname === "/api/accounts" ||
          pathname.startsWith("/api/accounts/")
        ) {
          return route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify(mockListResponse(route.request().url(), [])),
          });
        }
        if (pathname === "/api/edges" || pathname.startsWith("/api/edges/")) {
          return route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify(mockListResponse(route.request().url(), [])),
          });
        }
        if (
          pathname === "/api/webgemeindezentren" ||
          pathname.startsWith("/api/webgemeindezentren/")
        ) {
          return route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify(mockListResponse(route.request().url(), [])),
          });
        }
        if (pathname === "/api/health" || pathname.startsWith("/api/health/")) {
          return route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify({ status: "Ready" }),
          });
        }
        if (pathname === "/api/auth/me" || pathname === "/api/me") {
          return route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify({ authenticated: false, role: "gast" }),
          });
        }
        const requestPathWithQuery = `${url.pathname}${url.search}`;
        unexpectedApiRequests.push(requestPathWithQuery);
        return route.fulfill({
          status: 500,
          contentType: "application/json",
          body: JSON.stringify({
            error: `Unexpected mocked API request in basemap proof: ${requestPathWithQuery}`,
          }),
        });
      });

      // Navigate to map — /local-basemap/style.json and *.pmtiles are NOT mocked
      await page.goto("/map?proof=1&t=" + Date.now());

      // Preflight: style endpoint must exist and point to the local Hamburg PMTiles alias
      const styleResponse = await page.request.get(
        `/local-basemap/style.json?v=${LOCAL_BASEMAP_STYLE_VERSION}`,
      );
      expect(
        styleResponse.status(),
        "Expected /local-basemap/style.json to return HTTP 200",
      ).toBe(200);
      const styleContentType = styleResponse.headers()["content-type"] ?? "";
      expect(
        styleContentType,
        "Expected /local-basemap/style.json to be application/json",
      ).toContain("application/json");
      const styleJson = (await styleResponse.json()) as {
        sources?: { basemap?: { url?: string } };
      };
      const styleBasemapUrl = styleJson.sources?.basemap?.url ?? "";
      expect(
        styleBasemapUrl,
        "Expected local basemap style to reference the stable Hamburg PMTiles alias",
      ).toBe(`pmtiles://${REAL_PMTILES_FILENAME}`);
      expect(
        styleBasemapUrl,
        "Expected local basemap style source to use pmtiles protocol",
      ).toContain("pmtiles://");

      // === SERVER RANGE CONTRACT ===
      // Prove the Vite middleware can deliver Range requests correctly
      // This is independent of MapLibre's internal timing.
      const directRangeResponse = await page.request.get(
        `/local-basemap/${REAL_PMTILES_FILENAME}`,
        {
          headers: {
            Range: "bytes=0-511",
          },
        },
      );

      expect(
        directRangeResponse.status(),
        "Expected direct HTTP Range request to return 206 Partial Content",
      ).toBe(206);

      expect(
        directRangeResponse.headers()["accept-ranges"] ?? "",
        "Expected Accept-Ranges header for PMTiles delivery",
      ).toContain("bytes");

      expect(
        directRangeResponse.headers()["content-range"] ?? "",
        "Expected Content-Range header for PMTiles delivery",
      ).toContain("bytes 0-511/");

      expect(
        directRangeResponse.headers()["content-type"] ?? "",
        "Expected explicit PMTiles Content-Type",
      ).toContain("application/octet-stream");

      // === BROWSER/MAPLIBRE VISUAL CONTRACT ===
      // Map container must be visible
      await expect(page.locator("#map")).toBeVisible({ timeout: 20000 });

      // MapLibre canvas must appear
      await expect(page.locator("#map canvas")).toBeVisible({ timeout: 25000 });

      // Wait for at least one PMTiles request (MapLibre fetches header bytes first)
      await expect
        .poll(() => pmtilesRequests.length > 0, {
          message: `Expected ≥1 request to /local-basemap/${REAL_PMTILES_FILENAME}`,
          timeout: 25000,
        })
        .toBe(true);

      // Wait for Range requests (PMTiles protocol always uses Range for tile data)
      await expect
        .poll(
          () => pmtilesRequests.filter((r) => r.rangeHeader !== null).length,
          {
            message: "Expected PMTiles Range requests to the real artifact",
            timeout: 15000,
          },
        )
        .toBeGreaterThan(0);

      // Verify HTTP 206 responses (observed from MapLibre's network activity)
      // Note: This is observed traffic and may vary depending on rendering timing.
      // The hard assertion on 206 is provided by the direct Range request above.
      const observedResponses206 = pmtilesResponses.filter(
        (r) => r.status === 206,
      );
      if (observedResponses206.length > 0) {
        console.log(
          `[MapLibre Range observation] Observed ${observedResponses206.length} HTTP 206 responses`,
        );
      }

      // No external tile providers must have been contacted
      expect(
        remoteViolations,
        `External basemap providers were contacted: ${remoteViolations.join(", ")}`,
      ).toHaveLength(0);
      expect(
        unexpectedApiRequests,
        `Unexpected /api/** requests observed in basemap proof: ${unexpectedApiRequests.join(", ")}`,
      ).toHaveLength(0);

      // Verify canvas has non-trivial dimensions (MapLibre rendered something)
      const canvasDimensions = await page.evaluate(() => {
        const canvas = document.querySelector(
          "#map canvas",
        ) as HTMLCanvasElement | null;
        if (!canvas) return null;
        return {
          width: canvas.width,
          height: canvas.height,
          clientWidth: canvas.clientWidth,
          clientHeight: canvas.clientHeight,
        };
      });
      expect(
        canvasDimensions,
        "MapLibre WebGL canvas not found",
      ).not.toBeNull();
      expect(
        canvasDimensions!.clientWidth,
        "Canvas clientWidth must be > 0",
      ).toBeGreaterThan(0);
      expect(
        canvasDimensions!.clientHeight,
        "Canvas clientHeight must be > 0",
      ).toBeGreaterThan(0);

      // Check MapLibre isStyleLoaded() via window.__TEST_MAP__ hook
      await expect
        .poll(
          async () => {
            return await page.evaluate(() => {
              const map = (window as unknown as Record<string, unknown>)
                .__TEST_MAP__ as { isStyleLoaded?: () => boolean } | undefined;
              return map?.isStyleLoaded?.() ?? false;
            });
          },
          {
            message: "MapLibre isStyleLoaded() must resolve to true",
            timeout: 20000,
          },
        )
        .toBeTruthy();

      const styleLoaded = await page.evaluate(() => {
        const map = (window as unknown as Record<string, unknown>)
          .__TEST_MAP__ as { isStyleLoaded?: () => boolean } | undefined;
        return map?.isStyleLoaded?.() ?? false;
      });
      expect(styleLoaded, "MapLibre isStyleLoaded() must resolve to true").toBe(
        true,
      );

      // This proof verifies the Hamburg artifact, not the product's default
      // camera. Keep the proof geographically explicit so a neutral product
      // fallback can evolve without silently changing what this test samples.
      await page.evaluate(() => {
        const map = (window as unknown as Record<string, unknown>)
          .__TEST_MAP__ as
          | {
              jumpTo?: (options: {
                center: [number, number];
                zoom: number;
              }) => void;
            }
          | undefined;
        map?.jumpTo?.({ center: [10.058, 53.5585], zoom: 15 });
      });
      await expect
        .poll(
          async () => {
            return await page.evaluate((sourceId) => {
              const map = (window as unknown as Record<string, unknown>)
                .__TEST_MAP__ as TestMap | undefined;
              return map?.isSourceLoaded?.(sourceId) ?? false;
            }, SOURCE_ID);
          },
          {
            message:
              "Hamburg source must finish loading after the proof camera moves to Hamburg",
            timeout: 30_000,
          },
        )
        .toBeTruthy();

      const readFeatureEvidence = () =>
        page.evaluate(
          ({ sourceId, layerIds, sourceLayerIds }) => {
            const map = (window as unknown as Record<string, unknown>)
              .__TEST_MAP__ as TestMap | undefined;
            const renderedFeatures =
              map?.queryRenderedFeatures?.({ layers: layerIds }) ?? [];
            const sourceFeatureCounts = Object.fromEntries(
              sourceLayerIds.map((sourceLayer) => [
                sourceLayer,
                map?.querySourceFeatures?.(sourceId, { sourceLayer })?.length ??
                  0,
              ]),
            );
            return {
              sourceLoaded: map?.isSourceLoaded?.(sourceId) ?? false,
              renderedFeatureCount: renderedFeatures.length,
              renderedFromExpectedSource: renderedFeatures.filter(
                (feature) => feature.source === sourceId,
              ).length,
              renderedLayerIds: Array.from(
                new Set(
                  renderedFeatures
                    .map((feature) => feature.layer?.id)
                    .filter((layerId): layerId is string => Boolean(layerId)),
                ),
              ),
              sourceFeatureCounts,
              sourceFeatureCount: Object.values(sourceFeatureCounts).reduce(
                (sum, count) => sum + count,
                0,
              ),
            };
          },
          {
            sourceId: SOURCE_ID,
            layerIds: REGION_LAYER_IDS,
            sourceLayerIds: SOURCE_LAYER_IDS,
          },
        );

      await expect
        .poll(
          async () => (await readFeatureEvidence()).renderedFromExpectedSource,
          {
            message: "Expected rendered features from the Hamburg source",
            timeout: 30_000,
          },
        )
        .toBeGreaterThan(0);
      await expect
        .poll(async () => (await readFeatureEvidence()).sourceFeatureCount, {
          message:
            "Expected decoded vector features from Hamburg source-layers",
          timeout: 30_000,
        })
        .toBeGreaterThan(0);
      const featureEvidence = await readFeatureEvidence();

      await expect
        .poll(
          () =>
            pmtilesResponses.filter((response) => response.status === 206)
              .length,
          {
            message: "Expected observed HTTP 206 responses for Hamburg PMTiles",
            timeout: 30_000,
          },
        )
        .toBeGreaterThan(0);

      expect(featureEvidence.sourceLoaded).toBe(true);
      expect(featureEvidence.renderedLayerIds).toEqual(
        expect.arrayContaining(["landcover", "landuse"]),
      );
      expect(
        featureEvidence.sourceFeatureCounts.transportation,
      ).toBeGreaterThan(0);
      expect(featureEvidence.sourceFeatureCounts.landcover).toBeGreaterThan(0);
      expect(featureEvidence.sourceFeatureCounts.landuse).toBeGreaterThan(0);
      expect(featureEvidence.sourceFeatureCounts.water).toBeGreaterThan(0);
      expect(featureEvidence.sourceFeatureCounts.place).toBeGreaterThan(0);
      expect(failedResponses).toHaveLength(0);
      expect(consoleErrors).toHaveLength(0);

      // Screenshot as visual artifact
      await page.screenshot({
        path: testInfo.outputPath("screenshot.png"),
        fullPage: false,
      });

      // Build proof summary for guard consumption
      const proofSummary = {
        timestamp: new Date().toISOString(),
        verdict: "PROVEN",
        region: "hamburg",
        source_id: SOURCE_ID,
        pmtiles_filename: REAL_PMTILES_FILENAME,

        // SERVER RANGE CONTRACT
        direct_range_status: directRangeResponse.status(),
        direct_range_accept_ranges:
          directRangeResponse.headers()["accept-ranges"] ?? null,
        direct_range_content_range:
          directRangeResponse.headers()["content-range"] ?? null,
        direct_range_content_type:
          directRangeResponse.headers()["content-type"] ?? null,

        // BROWSER/MAPLIBRE VISUAL CONTRACT - OBSERVED
        pmtiles_requests_total: pmtilesRequests.length,
        pmtiles_range_requests_observed: pmtilesRequests.filter(
          (r) => r.rangeHeader !== null,
        ).length,
        pmtiles_206_responses_observed: pmtilesResponses.filter(
          (r) => r.status === 206,
        ).length,
        canvas_dimensions: canvasDimensions,
        style_loaded: styleLoaded,
        source_loaded: featureEvidence.sourceLoaded,
        rendered_feature_count: featureEvidence.renderedFeatureCount,
        rendered_from_expected_source:
          featureEvidence.renderedFromExpectedSource,
        rendered_layer_ids: featureEvidence.renderedLayerIds,
        decoded_source_feature_count: featureEvidence.sourceFeatureCount,
        decoded_source_feature_counts_by_layer:
          featureEvidence.sourceFeatureCounts,
        failed_responses: failedResponses,
        console_errors: consoleErrors,
        remote_violations: remoteViolations,
        unexpected_api_requests: unexpectedApiRequests,

        // Artifacts
        screenshot: testInfo.outputPath("screenshot.png"),
        first_request: pmtilesRequests[0] ?? null,
        first_206_response:
          pmtilesResponses.find((r) => r.status === 206) ?? null,
      };

      console.log(
        "BASEMAP_PROOF_SUMMARY:",
        JSON.stringify(proofSummary, null, 2),
      );

      // Persist proof summary next to screenshot
      fs.writeFileSync(
        testInfo.outputPath("proof-summary.json"),
        JSON.stringify(proofSummary, null, 2),
      );

      // Write to build/proofs/basemap-visual/ for guard script access
      const buildProofDir = path.resolve(
        process.cwd(),
        "../../build/proofs/basemap-visual",
      );
      fs.mkdirSync(buildProofDir, { recursive: true });
      fs.writeFileSync(
        path.join(buildProofDir, "proof-summary.json"),
        JSON.stringify(proofSummary, null, 2),
      );
      fs.copyFileSync(
        testInfo.outputPath("screenshot.png"),
        path.join(buildProofDir, "screenshot.png"),
      );

      // All assertions passed → PROVEN
      // Hard assertion: direct Range request must return 206
      expect(
        proofSummary.direct_range_status,
        "Proof requires direct HTTP 206 response for Range delivery (Server Range Contract)",
      ).toBe(206);

      // Hard assertion: direct Range response must include Content-Range
      expect(
        proofSummary.direct_range_content_range,
        "Proof requires Content-Range header in Range response",
      ).toBeTruthy();

      // Hard assertion: direct Range response must include Accept-Ranges
      expect(
        proofSummary.direct_range_accept_ranges,
        "Proof requires Accept-Ranges header",
      ).toContain("bytes");

      expect(
        proofSummary.direct_range_content_type,
        "Proof requires explicit PMTiles Content-Type",
      ).toContain("application/octet-stream");

      // Hard assertion: MapLibre must have requested at least one local PMTiles file
      expect(
        proofSummary.pmtiles_requests_total,
        "Proof requires ≥1 local PMTiles request from MapLibre (Browser/MapLibre Visual Contract)",
      ).toBeGreaterThan(0);

      // Hard assertion: Canvas must have rendered
      expect(
        proofSummary.canvas_dimensions,
        "Proof requires MapLibre canvas to render",
      ).not.toBeNull();

      // Hard assertion: Style must be loaded
      expect(
        proofSummary.style_loaded,
        "Proof requires MapLibre style to be loaded",
      ).toBe(true);

      expect(
        proofSummary.rendered_from_expected_source,
        "Proof requires visibly rendered features from the Hamburg source",
      ).toBeGreaterThan(0);

      expect(
        proofSummary.decoded_source_feature_count,
        "Proof requires decoded vector features from Hamburg source-layers",
      ).toBeGreaterThan(0);

      // Hard assertion: No external providers
      expect(
        proofSummary.remote_violations,
        "Proof requires zero external tile provider requests",
      ).toHaveLength(0);
    },
  );
});
