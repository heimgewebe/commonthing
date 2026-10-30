import { describe, it, expect } from "vitest";
import {
  applyNodeUpdateOverrides,
  buildMapScene,
  resolveApiMode,
} from "./scene";
import type { Node, Account, Edge, Webgemeindezentrum } from "./types";

const makeNode = (overrides: Partial<Node> = {}): Node => ({
  id: "node-1",
  kind: "Knoten",
  title: "Test Node",
  created_at: "2025-01-01T00:00:00Z",
  updated_at: "2025-01-01T00:00:00Z",
  tags: [],
  location: { lat: 53.5, lon: 10.0 },
  ...overrides,
});

const makeAccount = (overrides: Partial<Account> = {}): Account =>
  ({
    id: "acc-1",
    type: "garnrolle",
    title: "Test Account",
    created_at: "2025-01-01T00:00:00Z",
    tags: [],
    radius_m: 0,
    map_state: "exact",
    public_pos: { lat: 53.56, lon: 10.06 },
    ...overrides,
  }) as Account;

const makeEdge = (overrides: Partial<Edge> = {}): Edge => ({
  id: "edge-1",
  source_id: "acc-1",
  target_id: "node-1",
  edge_kind: "reference",
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
  meeting_note: "Hier kann die Ortsweberei tatsächlich zusammenkommen.",
  access_note: "Nutzung und Barrierefreiheit sind noch nicht bestätigt.",
  created_at: "2026-08-02T10:08:00.000Z",
  updated_at: "2026-08-02T10:08:00.000Z",
  ...overrides,
});

describe("resolveApiMode", () => {
  it("returns 'remote' when apiBase is set", () => {
    expect(resolveApiMode("https://api.example.com")).toBe("remote");
  });

  it("returns 'local' when apiBase is empty string", () => {
    expect(resolveApiMode("")).toBe("local");
  });

  it("returns 'local' when apiBase is undefined", () => {
    expect(resolveApiMode(undefined)).toBe("local");
  });
});

describe("applyNodeUpdateOverrides", () => {
  it("uses a newer canonical mutation response without refetching route data", () => {
    const base = makeNode();
    const updated = makeNode({
      title: "Lokal aktualisiert",
      updated_at: "2025-01-01T00:01:00Z",
      location: { lat: 53.51, lon: 10.01 },
    });
    const nodes = [base];

    const merged = applyNodeUpdateOverrides(nodes, { [base.id]: updated });

    expect(merged).not.toBe(nodes);
    expect(merged[0]).toBe(updated);
    expect(merged[0].title).toBe("Lokal aktualisiert");
    expect(merged[0].location).toEqual({ lat: 53.51, lon: 10.01 });
  });

  it("lets fresher route data supersede an older local mutation response", () => {
    const base = makeNode({
      title: "Frischer Serverstand",
      updated_at: "2025-01-01T00:02:00Z",
    });
    const staleOverride = makeNode({
      title: "Alter lokaler Stand",
      updated_at: "2025-01-01T00:01:00Z",
    });
    const nodes = [base];

    const merged = applyNodeUpdateOverrides(nodes, {
      [base.id]: staleOverride,
    });

    expect(merged).toBe(nodes);
    expect(merged[0]).toBe(base);
  });

  it("preserves sub-millisecond ordering when route data is fresher", () => {
    const base = makeNode({
      title: "Frischer Serverstand",
      updated_at: "2025-01-01T00:02:00.123789+00:00",
    });
    const staleOverride = makeNode({
      title: "Alter lokaler Stand",
      updated_at: "2025-01-01T00:02:00.123456+00:00",
    });
    const nodes = [base];

    expect(Date.parse(base.updated_at)).toBe(
      Date.parse(staleOverride.updated_at),
    );
    expect(applyNodeUpdateOverrides(nodes, { [base.id]: staleOverride })).toBe(
      nodes,
    );
  });

  it("preserves sub-millisecond ordering when the mutation is fresher", () => {
    const base = makeNode({
      updated_at: "2025-01-01T00:02:00.123456+00:00",
    });
    const updated = makeNode({
      title: "Mikrosekunden-neuer Stand",
      updated_at: "2025-01-01T00:02:00.123789+00:00",
    });

    expect(Date.parse(base.updated_at)).toBe(Date.parse(updated.updated_at));
    expect(applyNodeUpdateOverrides([base], { [base.id]: updated })[0]).toBe(
      updated,
    );
  });

  it("fails closed for invalid timestamps and mismatched identities", () => {
    const base = makeNode();
    const invalidTimestamp = makeNode({
      title: "Ungültig",
      updated_at: "not-a-timestamp",
    });
    const mismatched = makeNode({
      id: "node-other",
      updated_at: "2025-01-01T00:03:00Z",
    });
    const nodes = [base];

    expect(
      applyNodeUpdateOverrides(nodes, { [base.id]: invalidTimestamp }),
    ).toBe(nodes);
    expect(applyNodeUpdateOverrides(nodes, { [base.id]: mismatched })).toBe(
      nodes,
    );
  });
});

describe("buildMapScene", () => {
  it("transforms nodes into entities with type 'node'", () => {
    const scene = buildMapScene({
      nodes: [makeNode()],
      accounts: [],
      edges: [],
      webgemeindezentren: [],
      loadState: "ok",
      resourceStatus: [
        { resource: "nodes", status: "complete", loaded: 1, pages: 1 },
      ],
      apiBase: undefined,
      basemapMode: "local-sovereign",
    });

    expect(scene.entities).toHaveLength(1);
    expect(scene.entities[0].type).toBe("node");
    expect(scene.entities[0].id).toBe("node-1");
    expect(scene.entities[0].lat).toBe(53.5);
    expect(scene.entities[0].lon).toBe(10.0);
  });

  it("transforms accounts with public_pos into entities", () => {
    const scene = buildMapScene({
      nodes: [],
      accounts: [makeAccount({ tags: ["skill:Kochen", "interest:Commons"] })],
      edges: [],
      webgemeindezentren: [],
      loadState: "ok",
      resourceStatus: [
        { resource: "accounts", status: "complete", loaded: 1, pages: 1 },
      ],
      apiBase: undefined,
      basemapMode: "local-sovereign",
    });

    expect(scene.entities).toHaveLength(1);
    expect(scene.entities[0].type).toBe("garnrolle");
    expect(scene.entities[0].lat).toBe(53.56);
    expect(scene.entities[0].lon).toBe(10.06);
    expect(scene.entities[0].tags).toEqual([
      "skill:Kochen",
      "interest:Commons",
    ]);
  });

  it("excludes Garnrollen without a public position", () => {
    const account = makeAccount({
      id: "account-without-position",
      map_state: "not_on_map",
    });
    delete account.public_pos;

    const scene = buildMapScene({
      nodes: [],
      accounts: [account],
      edges: [],
      webgemeindezentren: [],
      loadState: "ok",
      resourceStatus: [],
      apiBase: undefined,
      basemapMode: "local-sovereign",
    });

    expect(scene.entities).toHaveLength(0);
  });

  it("keeps nongeographic nodes out of markers and drawable edge endpoints", () => {
    const scene = buildMapScene({
      nodes: [makeNode({ id: "digital", location: null }), makeNode()],
      accounts: [],
      edges: [makeEdge({ source_id: "digital", target_id: "node-1" })],
      webgemeindezentren: [],
      loadState: "ok",
      resourceStatus: [],
      apiBase: undefined,
      basemapMode: "local-sovereign",
    });
    expect(scene.entities.map((entity) => entity.id)).toEqual(["node-1"]);
  });

  it("merges nodes and accounts into entities", () => {
    const scene = buildMapScene({
      nodes: [makeNode()],
      accounts: [makeAccount()],
      edges: [makeEdge()],
      webgemeindezentren: [],
      loadState: "ok",
      resourceStatus: [],
      apiBase: undefined,
      basemapMode: "local-sovereign",
    });

    expect(scene.entities).toHaveLength(2);
    expect(scene.edges).toHaveLength(1);
  });

  it("maps a desired Webgemeindezentrum as an independent permanent structure", () => {
    const scene = buildMapScene({
      nodes: [],
      accounts: [],
      edges: [],
      webgemeindezentren: [makeCenter()],
      loadState: "ok",
      resourceStatus: [
        {
          resource: "webgemeindezentren",
          status: "complete",
          loaded: 1,
          pages: 1,
        },
      ],
      apiBase: undefined,
      basemapMode: "local-sovereign",
    });

    expect(scene.entities).toHaveLength(1);
    expect(scene.entities[0]).toMatchObject({
      type: "webgemeindezentrum",
      id: "webgemeindezentrum-hammer-park",
      title: "Webgemeindezentrum Hammer Park",
      lat: 53.5585,
      lon: 10.058,
      location_state: "desired",
      location_state_label: "Gewünschter Treffort",
    });
    expect(scene.entities[0].tags).toContain("Ortsweberei Hamm");
  });

  it("passes through loadState and resourceStatus", () => {
    const scene = buildMapScene({
      nodes: [],
      accounts: [],
      edges: [],
      webgemeindezentren: [],
      loadState: "partial",
      resourceStatus: [
        { resource: "nodes", status: "complete", loaded: 1, pages: 1 },
        { resource: "accounts", status: "failed", error: "HTTP 500" },
      ],
      apiBase: undefined,
      basemapMode: "local-sovereign",
    });

    expect(scene.loadState).toBe("partial");
    expect(scene.resourceStatus).toHaveLength(2);
    expect(scene.resourceStatus[1].status).toBe("failed");
  });

  it("sets diagnostics correctly for local mode", () => {
    const scene = buildMapScene({
      nodes: [],
      accounts: [],
      edges: [],
      webgemeindezentren: [],
      loadState: "ok",
      resourceStatus: [],
      apiBase: undefined,
      basemapMode: "local-sovereign",
    });

    expect(scene.diagnostics.apiMode).toBe("local");
    expect(scene.diagnostics.basemapMode).toBe("local-sovereign");
    expect(scene.diagnostics.degraded).toBe(false);
  });

  it("sets diagnostics correctly for remote mode with degraded state", () => {
    const scene = buildMapScene({
      nodes: [],
      accounts: [],
      edges: [],
      webgemeindezentren: [],
      loadState: "failed",
      resourceStatus: [],
      apiBase: "https://api.example.com",
      basemapMode: "remote-style",
    });

    expect(scene.diagnostics.apiMode).toBe("remote");
    expect(scene.diagnostics.basemapMode).toBe("remote-style");
    expect(scene.diagnostics.degraded).toBe(true);
  });
});
