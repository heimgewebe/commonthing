import { describe, it, expect, beforeEach } from "vitest";
import { get } from "svelte/store";
import { buildMapScene, type MapSceneModel } from "$lib/map/scene";
import type {
  Account,
  Edge,
  MapEntityViewModel,
  Node,
  Webgemeindezentrum,
} from "$lib/map/types";
import {
  deriveMarkerCounts,
  deriveSearchResults,
  deriveSearchMatchIds,
  deriveWeaveEdges,
  deriveLineEdges,
  toMapSelection,
  selectMapEntity,
} from "./mapView";
import {
  createEmptyMapContentFilters,
  evaluateMapContentFilters,
  getMapContentType,
} from "$lib/map/contentFilters";
import { selection, systemState, leaveToNavigation } from "./uiView";

const makeNode = (overrides: Partial<Node> = {}): Node => ({
  id: "node-1",
  kind: "place",
  title: "Hammer Park",
  created_at: "2025-01-01T00:00:00Z",
  updated_at: "2025-01-01T00:00:00Z",
  tags: [],
  location: { lat: 53.5, lon: 10.0 },
  ...overrides,
});

const makeAccount = (overrides: Partial<Account> = {}): Account => ({
  id: "acc-1",
  type: "garnrolle",
  title: "Eine Garnrolle",
  created_at: "2025-01-01T00:00:00Z",
  tags: [],
  radius_m: 0,
  map_state: "exact",
  public_pos: { lat: 53.56, lon: 10.06 },
  ...overrides,
});

const makeCenter = (
  overrides: Partial<Webgemeindezentrum> = {},
): Webgemeindezentrum => ({
  type: "webgemeindezentrum",
  id: "webgemeindezentrum-hammer-park",
  title: "Webgemeindezentrum Hammer Park",
  ortsweberei: {
    id: "ortsweberei-hamm",
    slug: "hamm",
    name: "Ortsweberei Hamm",
    gewebezelle_id: "hamm.weltgewebe.net",
  },
  location_state: "desired",
  location_state_label: "Gewünschter Treffort",
  faden_endpoint_id: "22222222-2222-5222-8222-222222222222",
  conversation_id: "33333333-3333-5333-8333-333333333333",
  location: { lat: 53.5585, lon: 10.058 },
  location_label: "Hammer Park – gewünschter Treffpunkt auf der Grünfläche",
  meeting_note: "Gemeinsamer Treffpunkt der Ortsweberei.",
  access_note: "Noch nicht bestätigt.",
  created_at: "2026-08-02T10:08:00.000Z",
  updated_at: "2026-08-02T10:08:00.000Z",
  ...overrides,
});

function sceneFrom(
  nodes: Node[],
  accounts: Account[],
  edges: Edge[] = [],
  webgemeindezentren: Webgemeindezentrum[] = [],
): MapSceneModel {
  return buildMapScene({
    nodes,
    accounts,
    edges,
    webgemeindezentren,
    loadState: "ok",
    resourceStatus: [],
    apiBase: undefined,
    basemapMode: "local-sovereign",
  });
}

describe("mapView presentation helpers", () => {
  beforeEach(() => {
    // Only uiView carries effectful state (via selectMapEntity); reset it.
    leaveToNavigation();
  });

  it("can build an empty, non-degraded scene as the starting point", () => {
    const scene = sceneFrom([], []);
    expect(scene.entities).toHaveLength(0);
    expect(scene.diagnostics.degraded).toBe(false);
  });

  it("exposes markers and diagnostic counts", () => {
    const scene = sceneFrom([makeNode()], [makeAccount()]);

    expect(scene.entities).toHaveLength(2);
    expect(deriveMarkerCounts(scene.entities)).toEqual({
      nodes: 1,
      accounts: 1,
      webgemeindezentren: 0,
    });
  });

  it("counts, filters and selects Webgemeindezentren independently from Knoten", () => {
    const scene = sceneFrom([], [], [], [makeCenter()]);

    expect(deriveMarkerCounts(scene.entities)).toEqual({
      nodes: 0,
      accounts: 0,
      webgemeindezentren: 1,
    });
    expect(
      evaluateMapContentFilters(scene.entities, createEmptyMapContentFilters())
        .contentTypes,
    ).toEqual([
      {
        id: "Webgemeindezentrum",
        label: "Webgemeindezentrum",
        count: 1,
      },
    ]);
    expect(
      deriveSearchResults(scene.entities, "Ortsweberei Hamm", true),
    ).toHaveLength(1);
    expect(toMapSelection(scene.entities[0])).toMatchObject({
      type: "webgemeindezentrum",
      id: "webgemeindezentrum-hammer-park",
    });
  });

  it("derives filterable types with counts and labels", () => {
    const scene = sceneFrom(
      [
        makeNode({ id: "n1", kind: "place" }),
        makeNode({ id: "n2", kind: "place" }),
      ],
      [makeAccount()],
    );

    expect(
      evaluateMapContentFilters(scene.entities, createEmptyMapContentFilters())
        .contentTypes,
    ).toEqual([
      { id: "Garnrolle", label: "Garnrolle", count: 1 },
      { id: "place", label: "Ort", count: 2 },
    ]);
  });

  it("filters markers by the active filter set", () => {
    const scene = sceneFrom([makeNode({ kind: "place" })], [makeAccount()]);

    expect(
      evaluateMapContentFilters(scene.entities, createEmptyMapContentFilters())
        .entities,
    ).toHaveLength(2);

    const filtered = evaluateMapContentFilters(scene.entities, {
      contentTypes: new Set(["Garnrolle"]),
      topics: new Set(),
    }).entities;
    expect(filtered).toHaveLength(1);
    expect(filtered[0].type).toBe("garnrolle");
  });

  it("returns search matches only when search is open with a query", () => {
    const scene = sceneFrom(
      [makeNode({ title: "Hammer Park" })],
      [makeAccount({ title: "Garn" })],
    );

    expect(deriveSearchResults(scene.entities, "hammer", false)).toHaveLength(
      0,
    );
    expect(deriveSearchResults(scene.entities, "", true)).toHaveLength(0);

    const results = deriveSearchResults(scene.entities, "hammer", true);
    expect(results).toHaveLength(1);
    expect(results[0].id).toBe("node-1");
    expect(deriveSearchMatchIds(results).has("node-1")).toBe(true);
  });

  it("stops scanning once the ten-result search limit is satisfied", () => {
    const matches: MapEntityViewModel[] = Array.from(
      { length: 10 },
      (_, index) => ({
        type: "node",
        id: `node-${index}`,
        title: `Needle ${index}`,
        kind: "place",
        tags: [],
        created_at: "2025-01-01T00:00:00Z",
        lat: 53.5 + index * 0.001,
        lon: 10 + index * 0.001,
      }),
    );
    const sentinel = {
      ...matches[0],
      id: "must-not-be-scanned",
      title: "placeholder",
    };
    Object.defineProperty(sentinel, "title", {
      get() {
        throw new Error("search scanned beyond its ten-result limit");
      },
    });

    const results = deriveSearchResults([...matches, sentinel], "needle", true);
    expect(results).toHaveLength(10);
    expect(results.map((item) => item.id)).toEqual(
      matches.map((item) => item.id),
    );
  });

  it("reuses lifecycle-bound normalized search terms for repeated queries", () => {
    const scene = sceneFrom([makeNode({ title: "Hammer Park" })], []);
    const marker = scene.entities[0];

    expect(deriveSearchResults(scene.entities, "hammer", true)).toHaveLength(1);
    Object.defineProperty(marker, "title", {
      get() {
        throw new Error("search recomputed an already indexed title");
      },
    });

    expect(deriveSearchResults(scene.entities, "hammer", true)).toHaveLength(1);
  });

  it("keeps search fields separate instead of matching across field boundaries", () => {
    const scene = sceneFrom(
      [makeNode({ title: "Park", tags: ["Commons"] })],
      [],
    );

    expect(deriveSearchResults(scene.entities, "park", true)).toHaveLength(1);
    expect(deriveSearchResults(scene.entities, "commons", true)).toHaveLength(
      1,
    );
    expect(
      deriveSearchResults(scene.entities, "parkcommons", true),
    ).toHaveLength(0);
  });

  it("finds Garnrollen by semantic type, plural and profile tags", () => {
    const scene = sceneFrom(
      [],
      [
        makeAccount({
          title: "Alexander Mohr",
          summary: "schaunmermal",
          tags: ["interest:Commons"],
        }),
      ],
    );

    for (const query of ["Garnrolle", "Garnrollen", "Commons"]) {
      const results = deriveSearchResults(scene.entities, query, true);
      expect(results).toHaveLength(1);
      expect(results[0].id).toBe("acc-1");
    }
  });

  it("finds Knoten by semantic type and kind", () => {
    const scene = sceneFrom(
      [makeNode({ title: "Fairschenkbox", kind: "resource" })],
      [],
    );

    expect(deriveSearchResults(scene.entities, "Knoten", true)).toHaveLength(1);
    expect(deriveSearchResults(scene.entities, "resource", true)).toHaveLength(
      1,
    );
  });

  it("scopes search to the visible markers it is handed", () => {
    const scene = sceneFrom(
      [makeNode({ title: "Findbar", kind: "place" })],
      [makeAccount({ title: "Findbar" })],
    );

    // No filter: search sees the full marker set.
    expect(deriveSearchResults(scene.entities, "findbar", true)).toHaveLength(
      2,
    );

    // With a filter active, the caller hands search only the visible markers.
    const visible = evaluateMapContentFilters(scene.entities, {
      contentTypes: new Set(["Garnrolle"]),
      topics: new Set(),
    }).entities;
    const results = deriveSearchResults(visible, "findbar", true);
    expect(results).toHaveLength(1);
    expect(results[0].type).toBe("garnrolle");
  });

  it("separates target-visible weave edges from two-endpoint line edges", () => {
    const scene = sceneFrom([makeNode()], [makeAccount()]);
    const edges: Edge[] = [
      {
        id: "e1",
        source_id: "node-1",
        target_id: "acc-1",
        edge_kind: "reference",
      },
      {
        id: "e2",
        source_id: "outside-visible-markers",
        target_id: "acc-1",
        edge_kind: "reference",
      },
      {
        id: "e3",
        source_id: "node-1",
        target_id: "missing",
        edge_kind: "reference",
      },
    ];

    const allVisible = evaluateMapContentFilters(
      scene.entities,
      createEmptyMapContentFilters(),
    ).entities;
    expect(deriveWeaveEdges(edges, allVisible).map((edge) => edge.id)).toEqual([
      "e1",
      "e2",
    ]);
    expect(deriveLineEdges(edges, allVisible).map((edge) => edge.id)).toEqual([
      "e1",
    ]);

    // Filtering out the Garnrolle removes the target body and therefore both
    // the Gewebekante and its strictere Linienkante.
    const onlyNodes = evaluateMapContentFilters(scene.entities, {
      contentTypes: new Set(["place"]),
      topics: new Set(),
    }).entities;
    expect(deriveWeaveEdges(edges, onlyNodes)).toHaveLength(0);
    expect(deriveLineEdges(edges, onlyNodes)).toHaveLength(0);
  });

  it("keeps governance Fäden visible through the center endpoint alias", () => {
    const account = makeAccount();
    const center = makeCenter();
    const scene = sceneFrom([], [account], [], [center]);
    const edges: Edge[] = [
      {
        id: "proposal-edge",
        source_id: account.id,
        target_id: center.faden_endpoint_id,
        edge_kind: "reference",
        faden_type: "proposal",
        faden_subject_id: "proposal-a",
      },
    ];

    expect(deriveWeaveEdges(edges, scene.entities)).toHaveLength(1);
    expect(deriveLineEdges(edges, scene.entities)).toHaveLength(1);
  });

  it("getMapContentType distinguishes nodes, Garnrollen and Webgemeindezentren", () => {
    const nodeEntity: MapEntityViewModel = {
      type: "node",
      id: "node-1",
      title: "Test Node",
      lat: 53.5,
      lon: 10.0,
      kind: "place",
      tags: [],
      created_at: "2025-01-01T00:00:00Z",
    };

    const garnrolleEntity: MapEntityViewModel = {
      type: "garnrolle",
      id: "acc-1",
      title: "Test Garnrolle",
      lat: 53.56,
      lon: 10.06,
      tags: [],
      created_at: "2025-01-01T00:00:00Z",
    };
    const centerEntity = sceneFrom([], [], [], [makeCenter()]).entities[0];

    expect(getMapContentType(nodeEntity)).toBe("place");
    expect(getMapContentType(garnrolleEntity)).toBe("Garnrolle");
    expect(getMapContentType(centerEntity)).toBe("Webgemeindezentrum");
  });

  it("toMapSelection carries panel data and normalizes the type", () => {
    const node: MapEntityViewModel = {
      type: "node",
      id: "node-1",
      title: "Hammer Park",
      lat: 53.5,
      lon: 10.0,
      kind: "place",
      tags: [],
      created_at: "2025-01-01T00:00:00Z",
    };

    const sel = toMapSelection(node);
    expect(sel).toEqual({ type: "node", id: "node-1", data: node });
  });

  it("selectMapEntity moves selection into fokus with panel data", () => {
    const node: MapEntityViewModel = {
      type: "node",
      id: "node-1",
      title: "Hammer Park",
      lat: 53.5,
      lon: 10.0,
      kind: "place",
      tags: [],
      created_at: "2025-01-01T00:00:00Z",
    };

    selectMapEntity(node);

    expect(get(systemState)).toBe("fokus");
    const sel = get(selection);
    expect(sel?.type).toBe("node");
    expect(sel?.id).toBe("node-1");
    expect(sel?.data).toBe(node);
  });
});
