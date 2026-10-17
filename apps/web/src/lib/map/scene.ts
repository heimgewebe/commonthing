/**
 * Map Scene Module
 *
 * Ownership: This module owns the transformation from raw route data
 * (nodes, accounts, edges, loadState) into the MapSceneModel.
 *
 * It is the single transformation point between data loading (+page.ts)
 * and map rendering (+page.svelte). No other module should duplicate
 * the node→entity or account→entity mapping logic.
 *
 * The scene is stateless and pure – it has no side effects.
 */
import { nodeToMapEntity } from "$lib/api/search";
import type {
  Node,
  Account,
  Edge,
  Webgemeindezentrum,
  MapEdge,
  MapEntityViewModel,
  MapEntityNode,
  MapEntityGarnrolle,
  MapEntityWebgemeindezentrum,
  MapLoadState,
  MapResourceStatus,
  MapDiagnostics,
} from "$lib/map/types";
import type { BasemapMode } from "$lib/map/config/basemap.current";
import { normalizeEdgeLifecycle } from "$lib/map/edgeLifecycle";

/**
 * MapSceneModel: the single source of truth for what the map should display.
 * Transforms raw route data into a structured representation.
 */
export type MapSceneModel = {
  entities: MapEntityViewModel[];
  edges: MapEdge[];
  loadState: MapLoadState;
  resourceStatus: MapResourceStatus[];
  diagnostics: MapDiagnostics;
};

export type MapSceneInput = {
  nodes: Node[];
  accounts: Account[];
  edges: Edge[];
  webgemeindezentren: Webgemeindezentrum[];
  loadState: MapLoadState;
  resourceStatus: MapResourceStatus[];
  apiBase: string | undefined;
  basemapMode: BasemapMode;
};

export type NodeUpdateOverrides = Readonly<Record<string, Node>>;

const RFC3339_FRACTION_RE =
  /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.(\d+))?(?:Z|[+-]\d{2}:\d{2})$/;

function compareRfc3339FractionalSeconds(
  left: string,
  right: string,
): -1 | 0 | 1 | null {
  const leftMatch = RFC3339_FRACTION_RE.exec(left);
  const rightMatch = RFC3339_FRACTION_RE.exec(right);
  if (!leftMatch || !rightMatch) return null;

  const leftFraction = leftMatch[1] ?? "";
  const rightFraction = rightMatch[1] ?? "";
  const width = Math.max(leftFraction.length, rightFraction.length);
  const normalizedLeft = leftFraction.padEnd(width, "0");
  const normalizedRight = rightFraction.padEnd(width, "0");
  if (normalizedLeft < normalizedRight) return -1;
  if (normalizedLeft > normalizedRight) return 1;
  return 0;
}

/**
 * Overlay canonical mutation responses onto request-scoped route data without
 * letting an old client-side response outrank a later, fresher route reload.
 * Returning the original array when nothing changes also avoids waking the
 * scene/overlay pipeline for unrelated override entries.
 */
export function applyNodeUpdateOverrides(
  nodes: Node[],
  overrides: NodeUpdateOverrides,
): Node[] {
  if (nodes.length === 0 || Object.keys(overrides).length === 0) return nodes;

  let changed = false;
  const merged = nodes.map((node) => {
    const override = overrides[node.id];
    if (!override || override.id !== node.id) return node;

    const baseUpdatedAt = Date.parse(node.updated_at);
    const overrideUpdatedAt = Date.parse(override.updated_at);
    if (!Number.isFinite(overrideUpdatedAt)) return node;
    if (Number.isFinite(baseUpdatedAt)) {
      if (overrideUpdatedAt < baseUpdatedAt) return node;
      if (overrideUpdatedAt === baseUpdatedAt) {
        const fractionalOrder = compareRfc3339FractionalSeconds(
          override.updated_at,
          node.updated_at,
        );
        if (fractionalOrder === null || fractionalOrder < 0) return node;
      }
    }

    if (override === node) return node;
    changed = true;
    return override;
  });

  return changed ? merged : nodes;
}

/**
 * Resolves the API mode from the API base URL.
 * A configured PUBLIC_GEWEBE_API_BASE means remote; absent means local/demo.
 */
export function resolveApiMode(
  apiBase: string | undefined,
): "remote" | "local" {
  return apiBase ? "remote" : "local";
}

/**
 * Transforms nodes into MapEntityNode[].
 */
function mapNodesToEntities(nodes: Node[]): MapEntityNode[] {
  return nodes.flatMap((node) => {
    const entity = nodeToMapEntity(node);
    return entity ? [entity] : [];
  });
}

/**
 * Transforms accounts into MapEntityGarnrolle[].
 * Only accounts with a public_pos are renderable on the map.
 */
function mapAccountsToEntities(accounts: Account[]): MapEntityGarnrolle[] {
  const result: MapEntityGarnrolle[] = [];
  for (const a of accounts) {
    if (a.public_pos) {
      result.push({
        type: "garnrolle" as const,
        id: a.id,
        title: a.title,
        lat: a.public_pos.lat,
        lon: a.public_pos.lon,
        summary: a.summary,
        tags: a.tags,
        modules: a.modules,
        created_at: a.created_at,
      });
    }
  }
  return result;
}

function mapWebgemeindezentrenToEntities(
  centers: Webgemeindezentrum[],
): MapEntityWebgemeindezentrum[] {
  return centers.map((center) => ({
    type: "webgemeindezentrum" as const,
    id: center.id,
    title: center.title,
    lat: center.location.lat,
    lon: center.location.lon,
    summary: center.meeting_note,
    tags: [
      "Webgemeindezentrum",
      center.ortsweberei.name,
      center.location_state_label,
      center.location_label,
    ],
    created_at: center.created_at,
    updated_at: center.updated_at,
    location_state: center.location_state,
    location_state_label: center.location_state_label,
    faden_endpoint_id: center.faden_endpoint_id,
    conversation_id: center.conversation_id,
    location_label: center.location_label,
    meeting_note: center.meeting_note,
    access_note: center.access_note,
    ortsweberei: center.ortsweberei,
  }));
}

/**
 * Builds the complete map scene from raw route data.
 * This is the single transformation point between data loading and map rendering.
 */
export function buildMapScene(input: MapSceneInput): MapSceneModel {
  const nodeEntities = mapNodesToEntities(input.nodes);
  const accountEntities = mapAccountsToEntities(input.accounts);
  const centerEntities = mapWebgemeindezentrenToEntities(
    input.webgemeindezentren,
  );

  const apiMode = resolveApiMode(input.apiBase);
  const degraded = input.loadState !== "ok";

  return {
    entities: [...nodeEntities, ...accountEntities, ...centerEntities],
    edges: input.edges.map(normalizeEdgeLifecycle),
    loadState: input.loadState,
    resourceStatus: input.resourceStatus,
    diagnostics: {
      apiMode,
      basemapMode: input.basemapMode,
      degraded,
    },
  };
}
