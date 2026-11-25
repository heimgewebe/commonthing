import { describe, expect, it } from "vitest";
import { stageDiscoverySearch, takeDiscoverySearch } from "./searchStore";

const click = {
  button: 0,
  defaultPrevented: false,
  altKey: false,
  ctrlKey: false,
  metaKey: false,
  shiftKey: false,
};

describe("discovery search handoff", () => {
  it("consumes only the addressed view, exactly once", () => {
    stageDiscoverySearch(click, "commons", "  Werkstatt  ");
    expect(takeDiscoverySearch("map")).toBeNull();
    expect(takeDiscoverySearch("commons")).toBe("Werkstatt");
    expect(takeDiscoverySearch("commons")).toBeNull();
  });

  it("hands back a list search without adding it to the URL", () => {
    stageDiscoverySearch(click, "map", "digitales Wissen");
    expect(takeDiscoverySearch("map")).toBe("digitales Wissen");
    expect(takeDiscoverySearch("map")).toBeNull();
  });

  it("ignores new-tab and cancelled activations", () => {
    for (const modifier of [
      { ctrlKey: true },
      { metaKey: true },
      { shiftKey: true },
      { altKey: true },
      { button: 1 },
      { defaultPrevented: true },
    ]) {
      stageDiscoverySearch(
        { ...click, ...modifier },
        "commons",
        "not this tab",
      );
      expect(takeDiscoverySearch("commons")).toBeNull();
    }
  });

  it("does not retain an old transfer when switching without a query", () => {
    stageDiscoverySearch(click, "commons", "old");
    stageDiscoverySearch(click, "commons", " ");
    expect(takeDiscoverySearch("commons")).toBeNull();
  });
});
