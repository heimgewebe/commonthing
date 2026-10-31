import { describe, expect, it, vi } from "vitest";
import {
  buildSimilarNodeQuery,
  MAX_SEARCH_QUERY_CHARS,
  nodeToMapEntity,
  searchNodes,
} from "./search";
import type { Node } from "$lib/map/types";

const NODE: Node = {
  id: "node-1",
  kind: "Werkstatt",
  title: "Radwerkstatt",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-02T00:00:00Z",
  summary: "Gemeinsam Fahrräder reparieren",
  info: "Offene Werkstatt",
  tags: ["Fahrrad", "Hilfe"],
  location: { lat: 54.9, lon: 8.3 },
};

function searchResponse(): Response {
  return new Response(
    JSON.stringify({
      items: [NODE],
      mode: "hybrid",
      generation_id: "generation-1",
      offset: 0,
    }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );
}

describe("searchNodes", () => {
  it.each([null, undefined])(
    "retains a nongeographic search result (%j) without a map entity",
    async (location) => {
      const node = { ...NODE, location };
      const response = await searchNodes("Commons", {
        fetcher: vi.fn<typeof fetch>(
          async () => new Response(JSON.stringify({ items: [node] })),
        ),
      });
      expect(response.items).toEqual([node]);
      expect(nodeToMapEntity(node)).toBeNull();
    },
  );

  it.each([
    { lat: NaN, lon: 10 },
    { lat: 91, lon: 10 },
    { lat: 53, lon: Infinity },
  ])("refuses invalid coordinates %j at the map boundary", (location) => {
    expect(nodeToMapEntity({ ...NODE, location })).toBeNull();
  });
  it("calls the authorized server search with credentials and stable kind filters", async () => {
    const fetcher = vi.fn<typeof fetch>(async () => searchResponse());

    const response = await searchNodes("  Fahrrad Hilfe  ", {
      kinds: ["Werkstatt", "Treffpunkt", "Werkstatt"],
      apiBase: "https://api.example.test",
      fetcher: fetcher as typeof fetch,
    });

    expect(response.items).toEqual([NODE]);
    expect(fetcher).toHaveBeenCalledOnce();
    const [url, init] = fetcher.mock.calls[0];
    expect(String(url)).toBe(
      "https://api.example.test/api/search?q=Fahrrad+Hilfe&limit=10&kinds=Treffpunkt%2CWerkstatt",
    );
    expect(init).toMatchObject({ credentials: "include" });
  });

  it("passes stable topic tags to the server before result limiting", async () => {
    const fetcher = vi.fn<typeof fetch>(async () => searchResponse());

    await searchNodes("Fahrrad", {
      tags: ["thema:natur", " thema:bildung ", "thema:natur"],
      apiBase: "https://api.example.test",
      fetcher: fetcher as typeof fetch,
    });

    const [url] = fetcher.mock.calls[0];
    expect(String(url)).toBe(
      "https://api.example.test/api/search?q=Fahrrad&limit=10&tags=thema%3Abildung%2Cthema%3Anatur",
    );
  });

  it("bounds overlong queries before they reach the server contract", async () => {
    const fetcher = vi.fn<typeof fetch>(async () => searchResponse());

    await searchNodes(`  ${"😀".repeat(MAX_SEARCH_QUERY_CHARS + 20)}  `, {
      apiBase: "https://api.example.test",
      fetcher: fetcher as typeof fetch,
    });

    const [url] = fetcher.mock.calls[0];
    const query = new URL(String(url)).searchParams.get("q");
    expect(Array.from(query ?? "")).toHaveLength(MAX_SEARCH_QUERY_CHARS);
    expect(query).toBe("😀".repeat(MAX_SEARCH_QUERY_CHARS));
  });

  it("maps server nodes to canonical map entities", () => {
    expect(nodeToMapEntity(NODE)).toMatchObject({
      type: "node",
      id: "node-1",
      lat: 54.9,
      lon: 8.3,
      kind: "Werkstatt",
    });
  });
});

describe("buildSimilarNodeQuery", () => {
  it("uses node content as bounded machine-similarity context", () => {
    expect(buildSimilarNodeQuery(NODE)).toBe(
      "Radwerkstatt · Werkstatt · Gemeinsam Fahrräder reparieren · Offene Werkstatt · Fahrrad · Hilfe",
    );
  });
  it("uses readable labels for stable knotting-topic tags", () => {
    expect(
      buildSimilarNodeQuery({ title: "Küche", tags: ["thema:ernährung"] }),
    ).toBe("Küche · Ernährung");
  });

  it("never exceeds the T006 query contract", () => {
    expect(buildSimilarNodeQuery({ title: "x".repeat(600) })).toHaveLength(
      MAX_SEARCH_QUERY_CHARS,
    );
  });
});
