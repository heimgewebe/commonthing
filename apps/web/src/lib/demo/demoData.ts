// apps/web/src/lib/demo/demoData.ts

// Using real UUIDs to match domain schema contracts
// IDs generated via `uuidgen` (or /proc/sys/kernel/random/uuid)

export const demoNodes = [
  {
    id: "b52be17c-4ab7-4434-98ce-520f86290cf0",
    kind: "resource", // Die Fair-Schenk-Box ist eine gemeinschaftlich nutzbare Ressource.
    title: "fairschenkbox",
    summary: "Öffentliche Fair-Schenk-Box",
    // Schema requirement: location must have lat/lon
    location: {
      lat: 53.558894813662505,
      lon: 10.060228407382967,
    },
    // Schema requirement: timestamps are usually expected by consumers
    created_at: "2025-01-01T12:00:00Z",
    updated_at: "2025-01-01T12:00:00Z",
    modules: [
      { id: "profile", label: "Übersicht", locked: true, type: "standard" },
      {
        id: "forum",
        label: "Gespräch",
        locked: true,
        type: "standard",
      },
      {
        id: "responsibilities",
        label: "Verantwortungen",
        locked: true,
        type: "standard",
      },
    ],
  },
];

export const demoAccounts = [
  {
    id: "7d97a42e-3704-4a33-a61f-0e0a6b4d65d8",
    type: "garnrolle",
    title: "weltgewebeknoten1",
    summary: "Lokale Garnrolle / Account",
    // Public view: only public_pos is used by the map.
    location: {
      lat: 53.5604148,
      lon: 10.0629844,
    },
    public_pos: {
      lat: 53.5604148,
      lon: 10.0629844,
    },
    radius_m: 0,
    map_state: "exact" as const,
    created_at: "2025-01-01T12:00:00Z",
    tags: ["account", "garnrolle", "demo"],
    modules: [
      { id: "profile", label: "Übersicht", locked: true, type: "standard" },
      {
        id: "forum",
        label: "Gespräch",
        locked: true,
        type: "standard",
      },
      {
        id: "responsibilities",
        label: "Verantwortungen",
        locked: true,
        type: "standard",
      },
    ],
  },
  {
    id: "00000000-0000-0000-0000-000000000002",
    type: "garnrolle",
    title: "PrivateSpinner (Fuzzed)",
    summary: "Account with fuzziness enabled",
    // Public view: only the projected public_pos is shown.
    location: {
      lat: 53.561,
      lon: 10.063,
    },
    public_pos: {
      lat: 53.561,
      lon: 10.063,
    },
    radius_m: 250,
    map_state: "radius" as const,
    created_at: "2025-01-01T12:00:00Z",
    tags: ["account", "garnrolle", "demo", "fuzzed"],
    modules: [],
  },
  {
    id: "00000000-0000-0000-0000-000000000003",
    type: "garnrolle",
    title: "Garnrolle noch nicht auf der Karte",
    summary: "Ein Account mit Garnrolle, aber ohne öffentliche Kartenposition.",
    map_state: "not_on_map" as const,
    created_at: "2025-01-01T12:00:00Z",
    tags: ["account", "garnrolle", "not-on-map", "demo"],
    modules: [],
  },
];

export const demoEdges = [
  {
    id: "eb5f41ff-3e64-417e-ae7e-eecd9c886ecc",
    source_type: "account",
    // Must match the UUID of weltgewebeknoten1
    source_id: "7d97a42e-3704-4a33-a61f-0e0a6b4d65d8",
    target_type: "node",
    // Must match the UUID of fairschenkbox
    target_id: "b52be17c-4ab7-4434-98ce-520f86290cf0",
    // Schema requirement: 'edge_kind' must be a valid enum value from the domain contract.
    // Verified against contracts/domain/edge.schema.json: ["delegation", "membership", "ownership", "reference"]
    edge_kind: "reference",
    note: "faden", // Storing the metaphor here
    created_at: "2025-01-01T12:00:00Z",
  },
];

export const demoWebgemeindezentren = [
  {
    type: "webgemeindezentrum" as const,
    id: "webgemeindezentrum-hammer-park",
    title: "Webgemeindezentrum Hammer Park",
    ortsweberei: {
      id: "ortsweberei-hamm",
      slug: "hamm",
      name: "Ortsweberei Hamm",
      gewebezelle_id: "hamm.weltgewebe.net",
    },
    location_state: "desired" as const,
    location_state_label: "Gewünschter Treffort",
    location: { lat: 53.5585, lon: 10.058 },
    location_label: "Hammer Park – gewünschter Treffpunkt auf der Grünfläche",
    meeting_note:
      "Ein bewusst gewählter öffentlicher Treffpunkt, an dem die Ortsweberei tatsächlich zusammenkommen kann. Die genaue Stelle kann später gemeinsam präzisiert werden.",
    access_note:
      "Gewünschter Treffort: Nutzung, Barrierefreiheit und regelmäßige Verfügbarkeit sind noch nicht bestätigt.",
    created_at: "2026-08-02T10:08:00.000Z",
    updated_at: "2026-08-02T10:08:00.000Z",
  },
];

export const demoOrtswebereien = [
  {
    id: "ortsweberei-hamm",
    slug: "hamm",
    name: "Ortsweberei Hamm",
    description: "Die erste lokale Ortsweberei der bisherigen Gewebezelle.",
    gewebezelle_id: "hamm.weltgewebe.net",
    lifecycle_state: "active",
    created_at: "2026-08-02T10:08:00.000Z",
    updated_at: "2026-08-02T10:08:00.000Z",
    webgemeindezentrum: demoWebgemeindezentren[0],
  },
];

export const demoWebgemeindezentrumDetails = [
  {
    ...demoWebgemeindezentren[0],
    location_history: [
      {
        event_id: 1,
        event_type: "placement_desired",
        location_state: "desired" as const,
        location_state_label: "Gewünschter Treffort",
        location: { lat: 53.5585, lon: 10.058 },
        location_label:
          "Hammer Park – gewünschter Treffpunkt auf der Grünfläche",
        reason:
          "Erste Ortsweberei: gewünschter gemeinsamer Treffpunkt auf einer Grünfläche im Hammer Park.",
        decided_at: "2026-08-02T10:08:00.000Z",
      },
    ],
  },
];
