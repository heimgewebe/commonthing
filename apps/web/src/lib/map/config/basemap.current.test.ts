import { describe, it, expect } from "vitest";
import {
  resolveBasemapInitialView,
  resolveBasemapMode,
  currentBasemap,
} from "./basemap.current";
import { resolveBasemapStyle } from "../basemap";

const CARTO_HOST = "basemaps.cartocdn.com";
const CARTO_STYLE_URL =
  "https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json";
const CARTO_DARK_STYLE_URL =
  "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json";

describe("resolveBasemapMode", () => {
  it("honours an explicit local-sovereign value regardless of context", () => {
    expect(resolveBasemapMode("local-sovereign", true)).toBe("local-sovereign");
    expect(resolveBasemapMode("local-sovereign", false)).toBe(
      "local-sovereign",
    );
  });

  it("honours an explicit remote-style value regardless of context", () => {
    expect(resolveBasemapMode("remote-style", true)).toBe("remote-style");
    expect(resolveBasemapMode("remote-style", false)).toBe("remote-style");
  });

  it("falls back to local-sovereign in local context when unset or invalid", () => {
    expect(resolveBasemapMode(undefined, true)).toBe("local-sovereign");
    expect(resolveBasemapMode("garbage", true)).toBe("local-sovereign");
    expect(resolveBasemapMode("", true)).toBe("local-sovereign");
  });

  it("falls back to remote-style in production context when unset or invalid", () => {
    expect(resolveBasemapMode(undefined, false)).toBe("remote-style");
    expect(resolveBasemapMode("garbage", false)).toBe("remote-style");
    expect(resolveBasemapMode("", false)).toBe("remote-style");
  });
});

describe("resolveBasemapInitialView", () => {
  it("uses a neutral Germany view for the nationwide sovereign basemap", () => {
    expect(resolveBasemapInitialView("local-sovereign", "germany")).toEqual({
      center: [10.4515, 51.1657],
      zoom: 7,
      minZoom: 7,
    });
    expect(
      resolveBasemapInitialView("local-sovereign", "germany").center,
    ).not.toEqual([10.058, 53.5585]);
  });

  it("keeps the explicit regional rollback within its real operating region", () => {
    expect(resolveBasemapInitialView("local-sovereign", "regional")).toEqual({
      center: [9.9, 54.2],
      zoom: 7,
      minZoom: 7,
    });
  });

  it("uses a world view only for the worldwide remote style", () => {
    expect(resolveBasemapInitialView("remote-style")).toEqual({
      center: [0, 0],
      zoom: 1,
      minZoom: 1,
    });
  });
});

describe("resolveBasemapStyle", () => {
  it("maps the explicit regional rollback variant to style.json", () => {
    const style = resolveBasemapStyle(
      {
        mode: "local-sovereign",
        variant: "regional",
      } as any,
      "light",
    );
    expect(style).toMatch(
      /^\/local-basemap\/style\.json\?v=0\.4\.0&build=[^&]+$/,
    );
    expect(style).not.toContain(CARTO_HOST);
  });

  it("maps the regional sovereign dark scheme to style-dark.json", () => {
    const style = resolveBasemapStyle(
      {
        mode: "local-sovereign",
        variant: "regional",
      } as any,
      "dark",
    );
    expect(style).toMatch(
      /^\/local-basemap\/style-dark\.json\?v=0\.4\.0&build=[^&]+$/,
    );
    expect(style).not.toContain(CARTO_HOST);
  });

  it("maps the Germany sovereign variant to the isolated Germany styles", () => {
    const light = resolveBasemapStyle(
      {
        mode: "local-sovereign",
        variant: "germany",
      } as any,
      "light",
    );
    const dark = resolveBasemapStyle(
      {
        mode: "local-sovereign",
        variant: "germany",
      } as any,
      "dark",
    );
    expect(light).toMatch(
      /^\/local-basemap\/style-germany\.json\?v=0\.4\.0&build=[^&]+$/,
    );
    expect(dark).toMatch(
      /^\/local-basemap\/style-germany-dark\.json\?v=0\.4\.0&build=[^&]+$/,
    );
    expect(light).not.toContain(CARTO_HOST);
    expect(dark).not.toContain(CARTO_HOST);
  });

  it("keeps legacy local config objects on the nationwide Germany path", () => {
    const style = resolveBasemapStyle({ mode: "local-sovereign" } as any);
    expect(style).toContain("/local-basemap/style-germany.json");
  });

  it("returns the explicit CARTO url only for remote-style light", () => {
    const style = resolveBasemapStyle(
      {
        mode: "remote-style",
        styleUrl: CARTO_STYLE_URL,
      } as any,
      "light",
    );
    expect(style).toBe(CARTO_STYLE_URL);
    expect(style).toContain(CARTO_HOST);
  });

  it("returns Dark Matter for remote-style dark with Voyager light", () => {
    const style = resolveBasemapStyle(
      {
        mode: "remote-style",
        styleUrl: CARTO_STYLE_URL,
      } as any,
      "dark",
    );
    expect(style).toBe(CARTO_DARK_STYLE_URL);
  });
});

describe("currentBasemap (build-time generated config)", () => {
  it("never carries a CARTO style url in local-sovereign mode", () => {
    if (currentBasemap.mode === "local-sovereign") {
      expect(currentBasemap).not.toHaveProperty("styleUrl");
      expect([undefined, "regional", "germany"]).toContain(
        currentBasemap.variant,
      );
      expect(resolveBasemapStyle(currentBasemap, "light")).toMatch(
        /^\/local-basemap\/style(?:-germany)?\.json\?v=0\.4\.0&build=[^&]+$/,
      );
      expect(resolveBasemapStyle(currentBasemap, "dark")).toMatch(
        /^\/local-basemap\/style(?:-germany)?-dark\.json\?v=0\.4\.0&build=[^&]+$/,
      );
    } else {
      expect(currentBasemap.styleUrl).toContain(CARTO_HOST);
    }
  });
});
