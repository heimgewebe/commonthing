import { describe, expect, it } from "vitest";
import type { MapEntityGarnrolle, MapEntityNode } from "./types";
import {
  householdMemberOffset,
  householdPageCapacity,
  indexHouseholdStacks,
} from "./householdStack";

function account(
  id: string,
  lat: number,
  lon: number,
  map_state: MapEntityGarnrolle["map_state"] = "exact",
): MapEntityGarnrolle {
  return {
    type: "garnrolle",
    id,
    title: id,
    created_at: "2026-01-01T00:00:00Z",
    lat,
    lon,
    map_state,
  };
}

describe("household stack projection", () => {
  it("only groups two or more exact public positions", () => {
    const a = account("a", 53.57, 10.04);
    const b = account("b", 53.57, 10.04);
    const c = account("c", 53.58, 10.05);
    const index = indexHouseholdStacks([a, b, c]);
    expect(index.groups.size).toBe(1);
    expect(index.byAccountId.get("a")?.members.map((item) => item.id)).toEqual([
      "a",
      "b",
    ]);
    expect(index.byAccountId.get("b")).toBe(index.byAccountId.get("a"));
    expect(index.byAccountId.has("c")).toBe(false);
  });

  it("never implies that radius or unspecified locations identify a house", () => {
    const unknown = account("unknown", 53.57, 10.04);
    delete unknown.map_state;
    const index = indexHouseholdStacks([
      account("a", 53.57, 10.04),
      account("radius", 53.57, 10.04, "radius"),
      unknown,
    ]);
    expect(index.groups.size).toBe(0);
  });

  it("never rounds two neighboring buildings into one household", () => {
    expect(
      indexHouseholdStacks([
        account("a", 53.57, 10.04),
        account("b", 53.5700001, 10.04),
      ]).groups.size,
    ).toBe(0);
  });

  it("excludes non-finite coordinates and other entity types", () => {
    const node: MapEntityNode = {
      type: "node",
      id: "node",
      title: "Knoten",
      kind: "place",
      tags: [],
      created_at: "2026-01-01T00:00:00Z",
      lat: 53.57,
      lon: 10.04,
    };
    expect(
      indexHouseholdStacks([
        account("a", NaN, 10.04),
        account("b", NaN, 10.04),
        node,
        account("c", 53.57, 10.04),
      ]).groups.size,
    ).toBe(0);
  });
});

describe("spread-out household view", () => {
  it("bounds household pages by viewport size without exceeding 16 markers", () => {
    expect(householdPageCapacity(900, 700)).toBe(16);
    expect(householdPageCapacity(220, 430)).toBe(6);
    expect(householdPageCapacity(240, 200)).toBe(3);
    expect(householdPageCapacity(NaN, Infinity)).toBeGreaterThan(0);
    expect(householdPageCapacity(3000, 3000)).toBe(16);
  });

  it("keeps individual accounts next to each other on desktop", () => {
    expect(
      [0, 1, 2].map((index) => householdMemberOffset(index, 3, 900)),
    ).toEqual([
      { x: -68, y: 0 },
      { x: 0, y: 0 },
      { x: 68, y: 0 },
    ]);
  });

  it("wraps into mobile rows without moving the geographic position", () => {
    const positions = Array.from({ length: 5 }, (_, index) =>
      householdMemberOffset(index, 5, 220),
    );
    expect(new Set(positions.map(({ x, y }) => `${x}:${y}`)).size).toBe(5);
    expect(positions.every(({ x }) => Math.abs(x) <= 68)).toBe(true);
    expect(positions.some(({ y }) => y < 0)).toBe(true);
    expect(positions.some(({ y }) => y > 0)).toBe(true);
  });
});
