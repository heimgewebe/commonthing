import { readFileSync } from "node:fs";
import { describe, expect, it, vi } from "vitest";
import {
  fetchNodeViewport,
  nodeViewportBboxes,
  nodeViewportContains,
} from "./nodeViewport";

function cursorPage(
  items: unknown[],
  hasMore: boolean,
  nextCursor: string | null,
  limit = 1000,
) {
  return new Response(
    JSON.stringify({
      items,
      page: { limit, next_cursor: nextCursor, has_more: hasMore },
    }),
    { status: 200, headers: { "content-type": "application/json" } },
  );
}

describe("nodeViewportBboxes", () => {
  it("formats an ordinary viewport for the existing node bbox API", () => {
    expect(
      nodeViewportBboxes({ west: 9, south: 53, east: 10, north: 54 }),
    ).toEqual(["9,53,10,54"]);
  });

  it("keeps an ordinary +180 degree edge as one bbox", () => {
    expect(
      nodeViewportBboxes({ west: 170, south: -10, east: 180, north: 10 }),
    ).toEqual(["170,-10,180,10"]);
  });

  it("splits an unwrapped antimeridian viewport into two ordinary boxes", () => {
    expect(
      nodeViewportBboxes({ west: 170, south: -10, east: 190, north: 10 }),
    ).toEqual(["170,-10,180,10", "-180,-10,-170,10"]);
  });

  it("collapses a full-world viewport to one bounded request", () => {
    expect(
      nodeViewportBboxes({ west: -200, south: -100, east: 200, north: 100 }),
    ).toEqual(["-180,-90,180,90"]);
  });

  it("fails closed on non-finite or inverted latitude input", () => {
    expect(() =>
      nodeViewportBboxes({ west: 0, south: 10, east: 1, north: -10 }),
    ).toThrow(/south/);
    expect(() =>
      nodeViewportBboxes({ west: Number.NaN, south: 0, east: 1, north: 1 }),
    ).toThrow(/finite/);
  });
});

describe("nodeViewportContains", () => {
  it("reuses an ordinary complete coverage for a contained viewport", () => {
    expect(
      nodeViewportContains(
        { west: 9, south: 53, east: 11, north: 55 },
        { west: 9.5, south: 53.5, east: 10.5, north: 54.5 },
      ),
    ).toBe(true);
  });

  it("rejects a viewport that extends beyond complete coverage", () => {
    expect(
      nodeViewportContains(
        { west: 9, south: 53, east: 11, north: 55 },
        { west: 8.9, south: 53.5, east: 10.5, north: 54.5 },
      ),
    ).toBe(false);
  });

  it("handles contained antimeridian coverage through canonical split boxes", () => {
    expect(
      nodeViewportContains(
        { west: 170, south: -20, east: 190, north: 20 },
        { west: 175, south: -10, east: 185, north: 10 },
      ),
    ).toBe(true);
  });

  it("lets full-world coverage contain ordinary and antimeridian viewports", () => {
    const world = { west: -200, south: -100, east: 200, north: 100 };
    expect(
      nodeViewportContains(world, { west: 9, south: 53, east: 10, north: 54 }),
    ).toBe(true);
    expect(
      nodeViewportContains(world, {
        west: 175,
        south: -10,
        east: 185,
        north: 10,
      }),
    ).toBe(true);
  });

  it("does not treat an ordinary bbox as coverage for a crossing viewport", () => {
    expect(
      nodeViewportContains(
        { west: 170, south: -20, east: 180, north: 20 },
        { west: 175, south: -10, east: 185, north: 10 },
      ),
    ).toBe(false);
  });
});

describe("fetchNodeViewport", () => {
  it("keeps bbox and cursor pagination bound to the same request", async () => {
    const first = {
      id: "n1",
      location: { lon: 9.5, lat: 53.5 },
    };
    const second = {
      id: "n2",
      location: { lon: 9.6, lat: 53.6 },
    };
    const fetcher = vi
      .fn<(input: string) => Promise<Response>>()
      .mockResolvedValueOnce(cursorPage([first], true, "n1", 1))
      .mockResolvedValueOnce(cursorPage([second], false, null, 1));

    const result = await fetchNodeViewport(
      fetcher,
      "https://api.example.test",
      { west: 9, south: 53, east: 10, north: 54 },
      { pageSize: 1, maxPages: 4, maxItems: 4 },
    );

    expect(result).toEqual({
      items: [first, second],
      status: "complete",
      pages: 2,
    });
    expect(fetcher.mock.calls.map(([url]) => url)).toEqual([
      "https://api.example.test/api/nodes?bbox=9%2C53%2C10%2C54&pagination=cursor&limit=1",
      "https://api.example.test/api/nodes?bbox=9%2C53%2C10%2C54&pagination=cursor&limit=1&cursor=n1",
    ]);
  });

  it("shares the page budget across antimeridian segments", async () => {
    const first = { id: "east", location: { lon: 179, lat: 0 } };
    const fetcher = vi.fn(async () => cursorPage([first], false, null, 1));

    const result = await fetchNodeViewport(
      fetcher,
      "",
      { west: 170, south: -10, east: 190, north: 10 },
      { pageSize: 1, maxPages: 1, maxItems: 10 },
    );

    expect(result).toEqual({
      items: [first],
      status: "truncated",
      pages: 1,
      reason: "page_limit",
    });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("deduplicates the same node returned by both antimeridian segments", async () => {
    const seamNode = { id: "seam", location: { lon: 180, lat: 0 } };
    const fetcher = vi.fn(async () =>
      cursorPage([seamNode], false, null, 1000),
    );

    const result = await fetchNodeViewport(
      fetcher,
      "",
      { west: 170, south: -10, east: 190, north: 10 },
      { maxPages: 2, maxItems: 10 },
    );

    expect(result).toEqual({ items: [seamNode], status: "complete", pages: 2 });
    expect(fetcher).toHaveBeenCalledTimes(2);
  });
});

describe("map route viewport architecture guards", () => {
  // These source-shape assertions protect wiring and lazy-loading boundaries.
  // They are intentionally not behavioral race/lifecycle proof; those cases
  // run in Playwright via tests/map-url-state.spec.ts.
  const pageSource = readFileSync(
    new URL("../../routes/map/+page.svelte", import.meta.url),
    "utf8",
  );

  it("keeps pagination lazy and retains explicit latest-request guards", () => {
    expect(pageSource).not.toContain('from "$lib/map/cursorPagination"');
    expect(pageSource).toContain("await refreshNodeViewport();");
    expect(pageSource).toContain("pendingViewportRefresh = true;");
    expect(pageSource).toContain(
      "void refreshNodeViewport().then(() => finishInitialLoading(generation))",
    );
    expect(pageSource).toContain(
      'map.on("moveend", handleNodeViewportMoveEnd)',
    );
    expect(pageSource).toMatch(
      /const currentRouteStatus = data\.resourceStatus \?\? null;[\s\S]*requestNodeViewportRefresh\?\.\(\);/,
    );
    expect(pageSource).toContain("new AbortController()");
    expect(pageSource).toContain(
      "const refreshNodeViewport = async (force = false)",
    );
    expect(pageSource).toMatch(
      /!force[\s\S]*viewportNodeCoverage !== null[\s\S]*nodeViewportContains\(viewportNodeCoverage, requestBounds\)/,
    );
    expect(pageSource).toContain(
      'result.status === "complete" ? requestBounds : null',
    );
    expect(pageSource).toMatch(
      /requestNodeViewportRefresh = \(\) => \{[\s\S]*refreshNodeViewport\(true\)/,
    );
    expect(pageSource).toContain("viewportNodeAbortController?.abort();");
    expect(pageSource).toContain("sequence !== viewportNodeSequence");
    expect(pageSource).toContain("viewportBootstrapReleased");
    expect(pageSource).toMatch(
      /if \(!viewportBootstrapReleased\) \{[\s\S]*viewportBootstrapReleased = \(data\.nodes \?\? \[\]\)\.every/,
    );
    expect(pageSource).toContain(
      "currentBootstrapKey === lastViewportBootstrapKey",
    );
  });
});
