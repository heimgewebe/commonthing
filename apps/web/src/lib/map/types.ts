export interface Location {
  lat: number;
  lon: number;
}

export interface Module {
  id: string;
  label: string;
  locked: boolean;
  type: string;
}

export interface Node {
  id: string;
  kind: string;
  title: string;
  created_at: string;
  updated_at: string;
  summary?: string | null;
  info?: string | null;
  tags: string[];
  /** Optional human-readable address, independent of the map projection. */
  address?: string | null;
  location?: Location | null;
  modules?: Module[];
}

export interface AccountBase {
  id: string;
  title: string;
  summary?: string | null;
  created_at: string;
  tags: string[];
  modules?: Module[];
}

export type GarnrolleMapState = "not_on_map" | "exact" | "radius";

export interface Account extends AccountBase {
  type: "garnrolle";
  location?: Location; // internal when available to trusted/local code
  public_pos?: Location; // public projection used by the map
  radius_m?: number;
  map_state?: GarnrolleMapState;
}

export type WebgemeindezentrumLocationState =
  | "desired"
  | "provisional"
  | "confirmed"
  | "unavailable"
  | "relocation_proposed";

export interface OrtswebereiReference {
  id: string;
  slug: string;
  name: string;
  gewebezelle_id: string;
}

/** Stable collective meeting-place structure; deliberately not a normal Knoten. */
export interface Webgemeindezentrum {
  type: "webgemeindezentrum";
  id: string;
  title: string;
  ortsweberei: OrtswebereiReference;
  location_state: WebgemeindezentrumLocationState;
  location_state_label: string;
  faden_endpoint_id: string;
  conversation_id: string;
  location: Location;
  location_label: string;
  meeting_note: string;
  access_note: string;
  created_at: string;
  updated_at: string;
}

/**
 * @deprecated MapPoint uses lat/lng (inconsistent with domain's lat/lon).
 * Not used in the overlay pipeline. Retained only for potential external consumers.
 * See MapEntityViewModel for the canonical map entity type.
 */
export interface MapPoint {
  id: string;
  lat: number;
  lng: number;
  kind: string; // 'node' | 'account'
  data: Node | Account | unknown;
}

export type FadenType = "conversation" | "proposal" | "knotting" | "vote";

export const FADEN_TYPE_LABELS: Record<FadenType, string> = {
  conversation: "Gesprächsfaden",
  proposal: "Antragsfaden",
  knotting: "Knüpffaden",
  vote: "Stimmfaden",
};

export interface Edge {
  id: string;
  source_id: string;
  source_type?: string;
  target_id: string;
  target_type?: string;
  edge_kind: string;
  /** Missing only on Fäden created before the typed participation contract. */
  faden_type?: FadenType;
  /** Stable proposal, conversation or node target behind the drawable endpoint. */
  faden_subject_id?: string;
  created_at?: string | null;
  expires_at?: string | null;
}

export type EdgeLifecycle =
  | { kind: "legacy" }
  | { kind: "invalid" }
  | { kind: "faden"; createdAtMs: number; expiresAtMs: number };

/** Map-only edge model with lifecycle timestamps parsed exactly once. */
export interface MapEdge extends Edge {
  lifecycle: EdgeLifecycle;
}

export type WeaveZone = "knotting" | "conversation" | "proposal" | "vote";

/** Stable diagonal arms of the woven X core (not a plus). */
export type WeaveArm = "northwest" | "northeast" | "southeast" | "southwest";

export const WEAVE_ARMS: readonly WeaveArm[] = [
  "northwest",
  "northeast",
  "southeast",
  "southwest",
] as const;

/** Over/under depth of one diagonal X arm at the centre crossing. */
export type WeaveArmDepth = "under" | "over";

/**
 * Canonical over/under pairing of the diagonal X (compass order NW→NE→SE→SW
 * alternates under/over). Runtime DOM strands and contract tests both read
 * from this single mapping so the under/over arrays cannot drift apart.
 */
export const WEAVE_ARM_DEPTH: Readonly<Record<WeaveArm, WeaveArmDepth>> = {
  northwest: "under",
  northeast: "over",
  southeast: "under",
  southwest: "over",
} as const;

/** NW↔SE diagonal: strand A, drawn under the crossing. */
export const WEAVE_UNDER_ARMS: readonly WeaveArm[] = WEAVE_ARMS.filter(
  (arm) => WEAVE_ARM_DEPTH[arm] === "under",
);

/** NE↔SW diagonal: strand B, drawn over the crossing. */
export const WEAVE_OVER_ARMS: readonly WeaveArm[] = WEAVE_ARMS.filter(
  (arm) => WEAVE_ARM_DEPTH[arm] === "over",
);

/**
 * One topic that colours the body. Identity (`id`) is the full normalised topic
 * text (`NFKC` + whitespace unify + trim) — never case-folded, never a
 * truncated display label. At most four topics receive a primary arm; further
 * topics stay in the model.
 */
export interface WeaveThemeSegment {
  id: string;
  label: string;
  color: string;
  /** Primary visual arm when this topic is among the four core colours. */
  arm: WeaveArm | null;
}

/**
 * Exactly one arm slot of the diagonal X.
 * `color` remains part of the model / primaryThemeColor contract; X DOM
 * rendering no longer consumes per-arm colours (root --weave-thread-color only).
 */
export interface WeaveXCoreSegment {
  arm: WeaveArm;
  themeId: string;
  label: string;
  color: string;
}

/**
 * Bounded content resting on an arm. Empty until the public map projection
 * exposes node-attached content; never invent counts.
 */
export interface WeaveArmOverlay {
  arm: WeaveArm;
  id: string;
  label: string;
}

export interface WeaveProposalArc {
  subjectId: string;
  proposalThreadCount: number;
  conversationThreadCount: number;
  voteThreadCount: number;
  bundledSubjectCount: number;
  latestActivityAtMs: number;
  opacity: number;
  color: string;
  startDeg: number;
  spanDeg: number;
}

/**
 * Active map projection of one grown woven body.
 * Counts describe stable Faden relations, not an invented durable history of
 * every individual Webungsschlag.
 */
export interface MapEntityWeave {
  zoneOrder: WeaveZone[];
  /** Full theme identities; visual X uses at most four primary arm colours. */
  themeSegments: WeaveThemeSegment[];
  /** Four arm slots of the diagonal X core. */
  xCoreSegments: WeaveXCoreSegment[];
  /**
   * Deterministic, capped arm overlays. Empty when the map contract does not
   * yet project node-attached content (truth boundary).
   */
  armOverlays: WeaveArmOverlay[];
  primaryThemeColor: string;
  coreDensity: number;
  /**
   * Bounded diameter scale for the conversation ring. Zero means no ring;
   * every active conversation maps monotonically into the fixed visual range,
   * so more currently attached conversation threads produce a larger ring.
   */
  conversationRingScale: number;
  knottingThreadCount: number;
  conversationThreadCount: number;
  conversationOpacity: number;
  proposalArcs: WeaveProposalArc[];
  proposalCount: number;
  proposalOverflowCount: number;
  voteThreadCount: number;
  /** Full vote relation count; visible stitches are capped separately. */
  totalActiveThreadCount: number;
}

// Phase 3: Discriminated union for map entities – eliminates semantic guesswork

/** A node entity rendered on the map. */
export interface MapEntityNode {
  type: "node";
  id: string;
  title: string;
  lat: number;
  lon: number;
  summary?: string | null;
  info?: string | null;
  kind: string;
  tags: string[];
  modules?: Module[];
  created_at: string;
  updated_at?: string;
  weight?: number;
  weave?: MapEntityWeave;
}

/** A Garnrolle entity rendered on the map when it has a public position. */
export interface MapEntityGarnrolle {
  type: "garnrolle";
  id: string;
  title: string;
  lat: number;
  lon: number;
  summary?: string | null;
  modules?: Module[];
  created_at: string;
  tags?: string[];
  weight?: number;
}

/** A stable Webgemeindezentrum rendered independently from ordinary Knoten. */
export interface MapEntityWebgemeindezentrum {
  type: "webgemeindezentrum";
  id: string;
  title: string;
  lat: number;
  lon: number;
  summary: string;
  tags: string[];
  created_at: string;
  updated_at: string;
  location_state: WebgemeindezentrumLocationState;
  location_state_label: string;
  faden_endpoint_id: string;
  conversation_id: string;
  location_label: string;
  meeting_note: string;
  access_note: string;
  ortsweberei: OrtswebereiReference;
  weight?: number;
  weave?: MapEntityWeave;
}

/**
 * Discriminated union of all map-renderable entities.
 * The `type` field is the discriminant – it is always present and determines the variant.
 * Garnrollen without a public position are excluded from map rendering.
 */
export type MapEntityViewModel =
  | MapEntityNode
  | MapEntityGarnrolle
  | MapEntityWebgemeindezentrum;

/**
 * @deprecated Use MapEntityViewModel for new code.
 * Retained as structural compatibility alias during migration.
 * The key difference: MapEntityViewModel requires `type` as a discriminant.
 */
export interface RenderableMapPoint {
  id: string;
  title: string;
  lat: number;
  lon: number;
  summary?: string | null;
  info?: string | null;
  type?: string;
  modules?: Module[];
  created_at?: string;
  updated_at?: string;
  kind?: string;
  tags?: string[];
  weight?: number;
}

// Phase 1: Explicit load state – replaces silent fallback-to-empty semantics
export type MapLoadState = "ok" | "partial" | "failed";

export type MapResourceName =
  | "nodes"
  | "accounts"
  | "edges"
  | "webgemeindezentren";

export type MapResourceStatus =
  | {
      resource: MapResourceName;
      status: "complete";
      loaded: number;
      pages: number;
    }
  | {
      resource: MapResourceName;
      status: "viewport";
      loaded: number;
      pages: number;
    }
  | {
      resource: MapResourceName;
      status: "truncated";
      loaded: number;
      pages: number;
      reason: "page_limit" | "item_limit";
    }
  | {
      resource: MapResourceName;
      status: "failed";
      error: string;
    };

export function summarizeMapResourceStatus(
  resourceStatus: MapResourceStatus[],
): { loadState: MapLoadState; loadNotice: string | null } {
  const failedCount = resourceStatus.filter(
    (status) => status.status === "failed",
  ).length;
  const completeCount = resourceStatus.filter(
    (status) => status.status === "complete" || status.status === "viewport",
  ).length;
  const loadState: MapLoadState =
    failedCount === resourceStatus.length
      ? "failed"
      : completeCount === resourceStatus.length
        ? "ok"
        : "partial";
  const resourceLabels: Record<MapResourceName, string> = {
    nodes: "Knoten",
    accounts: "Garnrollen",
    edges: "Fäden",
    webgemeindezentren: "Webgemeindezentren",
  };
  const labelsFor = (status: "failed" | "truncated") =>
    resourceStatus
      .filter((entry) => entry.status === status)
      .map((entry) => resourceLabels[entry.resource]);
  const failedLabels = labelsFor("failed");
  const truncatedLabels = labelsFor("truncated");
  const loadNotice =
    loadState === "partial"
      ? [
          failedLabels.length > 0
            ? `Einige Kartendaten konnten nicht geladen werden (${failedLabels.join(", ")}).`
            : null,
          truncatedLabels.length > 0
            ? `Der geladene Kartenbestand ist bewusst unvollständig (${truncatedLabels.join(", ")}).`
            : null,
        ]
          .filter(Boolean)
          .join(" ")
      : null;
  return { loadState, loadNotice };
}

// Phase 4: Diagnostics – separates API mode from basemap mode
export type MapDiagnostics = {
  apiMode: "remote" | "local";
  basemapMode: "local-sovereign" | "remote-style";
  degraded: boolean;
};
