import { describe, expect, it } from "vitest";
import type { Edge, MapEntityViewModel } from "$lib/map/types";
import {
  createEmptyMapContentFilters,
  evaluateMapContentFilters,
  getMapContentTopics,
  type MapContentFilters,
} from "./contentFilters";
import { deriveLineEdges, deriveWeaveEdges } from "$lib/stores/mapView";
import type { KnottingTopic } from "$lib/knottingTopics";

function node(
  id: string,
  kind: string,
  tags: string[] = [],
): MapEntityViewModel {
  return {
    type: "node",
    id,
    title: id,
    kind,
    tags,
    created_at: "2026-08-09T00:00:00Z",
    lat: 53.5,
    lon: 10,
  };
}

const entities: MapEntityViewModel[] = [
  node("event-natur", "event", ["thema:natur"]),
  node("place-kunst", "place", ["thema:kunst"]),
  node("event-kunst", "event", ["thema:kunst"]),
  node("place-untagged", "place", ["Natur"]),
  node("place-both", "place", ["thema:natur", "thema:kunst"]),
  {
    type: "garnrolle",
    id: "garnrolle-untagged",
    title: "Ohne Thema",
    tags: [],
    created_at: "2026-08-09T00:00:00Z",
    lat: 53.6,
    lon: 10.1,
  },
];

function filters(
  contentTypes: string[] = [],
  topics: KnottingTopic[] = [],
): MapContentFilters {
  return { contentTypes: new Set(contentTypes), topics: new Set(topics) };
}

describe("map content filter evaluation", () => {
  it("shows every loaded entity when no axis is selected", () => {
    const result = evaluateMapContentFilters(
      entities,
      createEmptyMapContentFilters(),
    );

    expect(result.entities.map((entity) => entity.id)).toEqual(
      entities.map((entity) => entity.id),
    );
    expect(result.activeCount).toBe(0);
  });

  it("keeps the existing content-type filter semantics", () => {
    const result = evaluateMapContentFilters(entities, filters(["event"]));

    expect(result.entities.map((entity) => entity.id)).toEqual([
      "event-natur",
      "event-kunst",
    ]);
  });

  it("matches one canonical controlled topic", () => {
    const result = evaluateMapContentFilters(entities, filters([], ["Natur"]));

    expect(result.entities.map((entity) => entity.id)).toEqual([
      "event-natur",
      "place-both",
    ]);
  });

  it("ORs multiple selected topics", () => {
    const result = evaluateMapContentFilters(
      entities,
      filters([], ["Natur", "Kunst"]),
    );

    expect(result.entities.map((entity) => entity.id)).toEqual([
      "event-natur",
      "place-kunst",
      "event-kunst",
      "place-both",
    ]);
  });

  it("ANDs content type with the selected topic axis", () => {
    const result = evaluateMapContentFilters(
      entities,
      filters(["place"], ["Natur"]),
    );

    expect(result.entities.map((entity) => entity.id)).toEqual(["place-both"]);
  });

  it("does not reinterpret free keywords or include untagged entities", () => {
    expect(getMapContentTopics(entities[3])).toEqual([]);
    const result = evaluateMapContentFilters(entities, filters([], ["Natur"]));

    expect(result.entities.map((entity) => entity.id)).not.toContain(
      "place-untagged",
    );
    expect(result.entities.map((entity) => entity.id)).not.toContain(
      "garnrolle-untagged",
    );
  });

  it("derives truthful cross-filtered facet counts from loaded entities", () => {
    const byTopic = evaluateMapContentFilters(entities, filters([], ["Natur"]));
    expect(byTopic.contentTypes).toEqual([
      { id: "event", label: "Ereignis", count: 1 },
      { id: "Garnrolle", label: "Garnrolle", count: 0 },
      { id: "place", label: "Ort", count: 1 },
    ]);

    const byType = evaluateMapContentFilters(entities, filters(["place"]));
    expect(byType.topics).toEqual([
      { id: "Natur", label: "Natur", count: 1 },
      { id: "Kunst", label: "Kunst", count: 2 },
    ]);
    expect(byType.allTopicsCount).toBe(3);
  });

  it("keeps active zero-count choices visible so no filter becomes hidden", () => {
    const result = evaluateMapContentFilters(
      entities,
      filters(["Unbekannt"], ["Wohnen"]),
    );

    expect(result.entities).toEqual([]);
    expect(result.contentTypes).toContainEqual({
      id: "Unbekannt",
      label: "Ungültige Knotenart",
      count: 0,
    });
    expect(result.topics).toContainEqual({
      id: "Wohnen",
      label: "Wohnen",
      count: 0,
    });
  });

  it("draws lines only when both topic-filtered endpoints stay visible", () => {
    const visible = evaluateMapContentFilters(
      entities,
      filters([], ["Natur"]),
    ).entities;
    const edges: Edge[] = [
      {
        id: "both-visible",
        source_id: "event-natur",
        target_id: "place-both",
        edge_kind: "reference",
      },
      {
        id: "hidden-source",
        source_id: "event-kunst",
        target_id: "place-both",
        edge_kind: "reference",
      },
      {
        id: "hidden-target",
        source_id: "event-natur",
        target_id: "place-kunst",
        edge_kind: "reference",
      },
    ];

    expect(deriveWeaveEdges(edges, visible).map((edge) => edge.id)).toEqual([
      "both-visible",
      "hidden-source",
    ]);
    expect(deriveLineEdges(edges, visible).map((edge) => edge.id)).toEqual([
      "both-visible",
    ]);
  });
});
