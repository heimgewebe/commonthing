import { describe, expect, it } from "vitest";
import type { MapEntityViewModel } from "$lib/map/types";
import { getMapSearchTermsNormalized } from "./searchIndex";

function makeNode(title: string, tags = ["Commons"]): MapEntityViewModel {
  return {
    type: "node",
    id: title,
    title,
    kind: "resource",
    tags,
    created_at: "2025-01-01T00:00:00Z",
    lat: 53.5,
    lon: 10,
  };
}

describe("map search index", () => {
  it("reuses terms for one entity identity and rebuilds them for new entity state", () => {
    const alpha = makeNode("Alpha");
    const first = getMapSearchTermsNormalized(alpha);
    expect(getMapSearchTermsNormalized(alpha)).toBe(first);
    expect(first).toContain("alpha");

    const beta = makeNode("Beta");
    const second = getMapSearchTermsNormalized(beta);
    expect(second).not.toBe(first);
    expect(second).toContain("beta");
    expect(second).not.toContain("alpha");
  });

  it.each([
    ["place", "ort"],
    ["resource", "ressource"],
    ["project", "projekt"],
    ["event", "ereignis"],
    ["need", "bedarf"],
    ["offer", "angebot"],
  ])("indexes German label %s for canonical kind", (kind, label) => {
    const node = { ...makeNode("Nachbarschaft"), kind };
    const terms = getMapSearchTermsNormalized(node);
    expect(terms).toContain(kind);
    expect(terms).toContain(label);
  });

  it("indexes stable topic tags under their human-readable labels", () => {
    const node = makeNode("Küche", ["thema:ernährung", "Werkstatt"]);
    const terms = getMapSearchTermsNormalized(node);

    expect(terms).toContain("thema:ernährung");
    expect(terms).toContain("ernährung");
    expect(terms).toContain("werkstatt");
  });
});
