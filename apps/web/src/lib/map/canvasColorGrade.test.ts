import { describe, expect, it } from "vitest";
import type { StyleSpecification } from "maplibre-gl";
import {
  gradeMapCanvasColor,
  gradeMapPaint,
  gradeStyleColors,
} from "./canvasColorGrade";

describe("gradeMapCanvasColor", () => {
  // Reference pixels rendered by headless Chromium for a canvas carrying
  // `filter: grayscale(0.2) saturate(0.75) brightness(1.03) contrast(0.95)`.
  it.each([
    ["#000000", "#060606"],
    ["#ffffff", "#f9f9f9"],
    ["#ff0000", "#b11c1c"],
    ["#e0e0e0", "#e2e2e2"],
    ["#a0c8f0", "#b0c8df"],
    ["#333333", "#383838"],
    ["#12181a", "#1a1d1f"],
    ["#1a3348", "#283743"],
  ])("matches Chromium's CSS filter output for %s", (input, rendered) => {
    expect(gradeMapCanvasColor(input)).toBe(rendered);
  });

  it("expands short hex and keeps alpha", () => {
    expect(gradeMapCanvasColor("#f00")).toBe("#b11c1c");
    expect(gradeMapCanvasColor("rgba(255, 0, 0, 0.48)")).toBe(
      "rgba(177, 28, 28, 0.48)",
    );
    expect(gradeMapCanvasColor("rgb(255,0,0)")).toBe("#b11c1c");
  });

  it("leaves non-colour strings untouched", () => {
    for (const value of ["get", "color", "match", "wood", "#zzz", ""]) {
      expect(gradeMapCanvasColor(value)).toBe(value);
    }
  });
});

describe("gradeStyleColors", () => {
  it("grades colour literals in paint values and expressions only", () => {
    const style = {
      version: 8,
      sources: {},
      layers: [
        {
          id: "bg",
          type: "background",
          paint: { "background-color": "#e0e0e0" },
        },
        {
          id: "landcover",
          type: "fill",
          source: "basemap",
          paint: {
            "fill-color": [
              "match",
              ["get", "class"],
              "wood",
              "#ffffff",
              "#000000",
            ],
            "fill-opacity": 0.5,
          },
        },
        {
          id: "roads",
          type: "line",
          source: "basemap",
          paint: { "line-color": { stops: [[5, "#ff0000"]] } },
        },
      ],
    } as unknown as StyleSpecification;

    const graded = gradeStyleColors(style);

    expect(graded.layers[0]).toMatchObject({
      paint: { "background-color": "#e2e2e2" },
    });
    expect(graded.layers[1]).toMatchObject({
      paint: {
        "fill-color": ["match", ["get", "class"], "wood", "#f9f9f9", "#060606"],
        "fill-opacity": 0.5,
      },
    });
    expect(graded.layers[2]).toMatchObject({
      paint: { "line-color": { stops: [[5, "#b11c1c"]] } },
    });
    // The input style is not mutated.
    expect(style.layers[0]).toMatchObject({
      paint: { "background-color": "#e0e0e0" },
    });
  });

  it("grades overlay paint objects without touching data lookups", () => {
    expect(
      gradeMapPaint({
        "circle-color": ["get", "color"],
        "circle-stroke-color": ["case", true, "#ff0000", "#000000"],
        "circle-opacity": 0.92,
      }),
    ).toEqual({
      "circle-color": ["get", "color"],
      "circle-stroke-color": ["case", true, "#b11c1c", "#060606"],
      "circle-opacity": 0.92,
    });
  });
});
