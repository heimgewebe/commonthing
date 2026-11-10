import { test, expect } from "@playwright/test";
import { mockApiResponses } from "./fixtures/mockApi";
import { EDGE_VISUAL_STYLE } from "../src/lib/map/overlay/edges";
import {
  gradeColorExpression,
  gradeMapCanvasColor,
} from "../src/lib/map/canvasColorGrade";
import { waitForMapReady } from "./fixtures/mapReady";

test.describe("Edge visibility on load", () => {
  test("edges are rendered after map load without filter toggle", async ({
    page,
  }) => {
    await mockApiResponses(page);

    await page.goto("/map");
    await waitForMapReady(page);

    // Wait for the map to fully load (including style)
    await page.waitForFunction(
      () => {
        const m = (window as any).__TEST_MAP__;
        return m && typeof m.isStyleLoaded === "function" && m.isStyleLoaded();
      },
      undefined,
      { timeout: 15000 },
    );

    // Wait for the yarn stack: shadow, body and highlight for legacy edges.
    await page.waitForFunction(
      () => {
        const m = (window as any).__TEST_MAP__;
        return (
          m &&
          m.getLayer("edges-layer") !== undefined &&
          m.getLayer("edges-shadow-layer") !== undefined &&
          m.getLayer("edges-highlight-layer") !== undefined
        );
      },
      undefined,
      { timeout: 5000 },
    );

    await page.waitForFunction(
      () => {
        const m = (window as any).__TEST_MAP__;
        const source = m?.getSource("edges-source");
        return (source?.serialize?.()?.data?.features?.length ?? 0) > 0;
      },
      undefined,
      { timeout: 10_000 },
    );

    // Verify the full rendering pipeline: source exists, layers exist, features are populated
    const edgeState = await page.evaluate(() => {
      const m = (window as any).__TEST_MAP__;
      if (!m)
        return {
          source: false,
          layer: false,
          shadowLayer: false,
          highlightLayer: false,
          featureCount: 0,
        };

      const source = m.getSource("edges-source");
      const layer = m.getLayer("edges-layer");
      const shadowLayer = m.getLayer("edges-shadow-layer");
      const highlightLayer = m.getLayer("edges-highlight-layer");

      let shadowColor = null;
      let shadowWidth = null;
      let shadowOpacity = null;
      let mainOpacity = null;
      let shadowBlur = null;
      let mainColor = null;
      let mainWidth = null;
      let highlightColor = null;
      let highlightWidth = null;
      let isUnderMain = false;
      let highlightAboveMain = false;

      let shadowDasharray = null;
      let mainDasharray = null;
      let highlightDasharray = null;

      if (shadowLayer && layer && highlightLayer) {
        shadowColor = m.getPaintProperty("edges-shadow-layer", "line-color");
        shadowWidth = m.getPaintProperty("edges-shadow-layer", "line-width");
        shadowOpacity = m.getPaintProperty(
          "edges-shadow-layer",
          "line-opacity",
        );
        mainOpacity = m.getPaintProperty("edges-layer", "line-opacity");
        shadowBlur = m.getPaintProperty("edges-shadow-layer", "line-blur");
        mainColor = m.getPaintProperty("edges-layer", "line-color");
        mainWidth = m.getPaintProperty("edges-layer", "line-width");
        highlightColor = m.getPaintProperty(
          "edges-highlight-layer",
          "line-color",
        );
        highlightWidth = m.getPaintProperty(
          "edges-highlight-layer",
          "line-width",
        );
        shadowDasharray = m.getPaintProperty(
          "edges-shadow-layer",
          "line-dasharray",
        );
        mainDasharray = m.getPaintProperty("edges-layer", "line-dasharray");
        highlightDasharray = m.getPaintProperty(
          "edges-highlight-layer",
          "line-dasharray",
        );

        const styleLayers = m.getStyle().layers;
        if (styleLayers) {
          const shadowIndex = styleLayers.findIndex(
            (l: any) => l.id === "edges-shadow-layer",
          );
          const mainIndex = styleLayers.findIndex(
            (l: any) => l.id === "edges-layer",
          );
          const highlightIndex = styleLayers.findIndex(
            (l: any) => l.id === "edges-highlight-layer",
          );
          if (
            shadowIndex !== -1 &&
            mainIndex !== -1 &&
            shadowIndex < mainIndex
          ) {
            isUnderMain = true;
          }
          if (
            highlightIndex !== -1 &&
            mainIndex !== -1 &&
            highlightIndex > mainIndex
          ) {
            highlightAboveMain = true;
          }
        }
      }

      let featureCount = 0;
      if (source && typeof source.serialize === "function") {
        const serialized = source.serialize();
        featureCount = serialized?.data?.features?.length ?? 0;
      }

      return {
        source: source !== undefined,
        layer: layer !== undefined,
        shadowLayer: shadowLayer !== undefined,
        highlightLayer: highlightLayer !== undefined,
        shadowColor,
        shadowWidth,
        shadowOpacity,
        mainOpacity,
        shadowBlur,
        mainColor,
        mainWidth,
        highlightColor,
        highlightWidth,
        shadowDasharray,
        mainDasharray,
        highlightDasharray,
        isUnderMain,
        highlightAboveMain,
        featureCount,
      };
    });

    expect(edgeState.source).toBe(true);
    expect(edgeState.layer).toBe(true);
    expect(edgeState.shadowLayer).toBe(true);
    expect(edgeState.highlightLayer).toBe(true);
    // Paint colours carry the canvas colour grade (canvasColorGrade.ts).
    expect(edgeState.shadowColor).toBe(
      gradeMapCanvasColor(EDGE_VISUAL_STYLE.shadowColor),
    );
    expect(edgeState.shadowWidth).toBe(
      EDGE_VISUAL_STYLE.bodyWidth + EDGE_VISUAL_STYLE.shadowWidthExtra,
    );
    expect(edgeState.shadowBlur).toBe(EDGE_VISUAL_STYLE.shadowBlur);
    expect(edgeState.mainColor).toEqual(
      gradeColorExpression([
        "coalesce",
        ["get", "themeColor"],
        EDGE_VISUAL_STYLE.bodyColor,
      ]),
    );
    expect(edgeState.mainWidth).toBe(EDGE_VISUAL_STYLE.bodyWidth);
    expect(edgeState.highlightColor).toBe(
      gradeMapCanvasColor(EDGE_VISUAL_STYLE.highlightColor),
    );
    expect(edgeState.highlightWidth).toBe(
      Math.max(
        0.55,
        EDGE_VISUAL_STYLE.bodyWidth * EDGE_VISUAL_STYLE.highlightWidthFactor,
      ),
    );
    expect(edgeState.mainOpacity).toEqual([
      "coalesce",
      ["to-number", ["get", "opacity"]],
      0,
    ]);
    expect(edgeState.shadowOpacity).toEqual([
      "*",
      edgeState.mainOpacity,
      EDGE_VISUAL_STYLE.shadowOpacityFactor,
    ]);
    // Structural invariant: legacy shadow/body stay continuous yarn; braid
    // rhythm lives on the narrow highlight (not three mismatched strokes).
    expect(edgeState.shadowDasharray == null).toBe(true);
    expect(edgeState.mainDasharray == null).toBe(true);
    expect(edgeState.highlightDasharray).toEqual([
      ...EDGE_VISUAL_STYLE.dashArray,
    ]);

    expect(edgeState.isUnderMain).toBe(true);
    expect(edgeState.highlightAboveMain).toBe(true);
    expect(edgeState.featureCount).toBeGreaterThan(0);
  });
});
