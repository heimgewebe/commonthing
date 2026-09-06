import {
  fetchCursorPages,
  MAP_CURSOR_MAX_ITEMS,
  MAP_CURSOR_MAX_PAGES,
  MAP_CURSOR_PAGE_SIZE,
  type CursorPaginationOptions,
  type CursorTruncationReason,
} from "./cursorPagination";
import type { Node } from "./types";

type FetchLike = (input: string) => Promise<Response>;

export type NodeViewportBounds = {
  west: number;
  south: number;
  east: number;
  north: number;
};

export type NodeViewportResult =
  | { items: Node[]; status: "complete"; pages: number }
  | {
      items: Node[];
      status: "truncated";
      pages: number;
      reason: CursorTruncationReason;
    };

export class NodeViewportError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "NodeViewportError";
  }
}

function finite(value: number, label: string): number {
  if (!Number.isFinite(value)) {
    throw new NodeViewportError(`${label} must be finite`);
  }
  return value;
}

function normalizeLongitude(value: number): number {
  const normalized = ((((value + 180) % 360) + 360) % 360) - 180;
  return Object.is(normalized, -0) ? 0 : normalized;
}

function coordinate(value: number): string {
  const rounded = Number(value.toFixed(6));
  return String(Object.is(rounded, -0) ? 0 : rounded);
}

/**
 * Convert one possibly unwrapped MapLibre viewport into API bboxes.
 * A viewport crossing the antimeridian becomes two ordinary boxes because the
 * existing node API intentionally accepts rectangular min/max bboxes only.
 */
export function nodeViewportBboxes(bounds: NodeViewportBounds): string[] {
  const westRaw = finite(bounds.west, "bounds.west");
  let eastRaw = finite(bounds.east, "bounds.east");
  const southRaw = finite(bounds.south, "bounds.south");
  const northRaw = finite(bounds.north, "bounds.north");
  if (southRaw > northRaw) {
    throw new NodeViewportError("bounds.south must not exceed bounds.north");
  }
  const south = Math.max(-90, southRaw);
  const north = Math.min(90, northRaw);
  if (south > north) {
    throw new NodeViewportError(
      "viewport is outside the supported latitude range",
    );
  }
  if (
    westRaw >= -180 &&
    westRaw <= 180 &&
    eastRaw >= -180 &&
    eastRaw <= 180 &&
    westRaw <= eastRaw
  ) {
    return [
      `${coordinate(westRaw)},${coordinate(south)},${coordinate(eastRaw)},${coordinate(north)}`,
    ];
  }
  while (eastRaw < westRaw) eastRaw += 360;
  const span = eastRaw - westRaw;
  if (span >= 360) {
    return [`-180,${coordinate(south)},180,${coordinate(north)}`];
  }

  const west = normalizeLongitude(westRaw);
  const east = normalizeLongitude(eastRaw);
  if (west <= east) {
    return [
      `${coordinate(west)},${coordinate(south)},${coordinate(east)},${coordinate(north)}`,
    ];
  }
  return [
    `${coordinate(west)},${coordinate(south)},180,${coordinate(north)}`,
    `-180,${coordinate(south)},${coordinate(east)},${coordinate(north)}`,
  ];
}

type CanonicalViewportBbox = {
  west: number;
  south: number;
  east: number;
  north: number;
};

function canonicalViewportBboxes(
  bounds: NodeViewportBounds,
): CanonicalViewportBbox[] {
  return nodeViewportBboxes(bounds).map((bbox) => {
    const [west, south, east, north] = bbox.split(",").map(Number);
    return { west, south, east, north };
  });
}

/**
 * Return true when a previously complete viewport response fully covers the
 * requested viewport. Antimeridian/full-world cases reuse the same canonical
 * split representation as the API request path, so containment cannot diverge
 * from the actual BBOX semantics.
 */
export function nodeViewportContains(
  coverage: NodeViewportBounds,
  requested: NodeViewportBounds,
): boolean {
  const coverageBboxes = canonicalViewportBboxes(coverage);
  const requestedBboxes = canonicalViewportBboxes(requested);
  return requestedBboxes.every((candidate) =>
    coverageBboxes.some(
      (available) =>
        available.west <= candidate.west &&
        available.south <= candidate.south &&
        available.east >= candidate.east &&
        available.north >= candidate.north,
    ),
  );
}

function endpoint(apiUrl: string, bbox: string): string {
  const params = new URLSearchParams({ bbox });
  return `${apiUrl}/api/nodes?${params.toString()}`;
}

function positiveInteger(value: number, label: string): number {
  if (!Number.isInteger(value) || value <= 0) {
    throw new NodeViewportError(`${label} must be a positive integer`);
  }
  return value;
}

/**
 * Fetch exactly the nodes inside the visible map viewport while retaining the
 * existing global cursor safety budget across antimeridian-split requests.
 */
export async function fetchNodeViewport(
  fetcher: FetchLike,
  apiUrl: string,
  bounds: NodeViewportBounds,
  options: CursorPaginationOptions = {},
): Promise<NodeViewportResult> {
  const pageSize = positiveInteger(
    options.pageSize ?? MAP_CURSOR_PAGE_SIZE,
    "pageSize",
  );
  const maxPages = positiveInteger(
    options.maxPages ?? MAP_CURSOR_MAX_PAGES,
    "maxPages",
  );
  const maxItems = positiveInteger(
    options.maxItems ?? MAP_CURSOR_MAX_ITEMS,
    "maxItems",
  );
  const bboxes = nodeViewportBboxes(bounds);
  const byId = new Map<string, Node>();
  let pages = 0;

  for (const bbox of bboxes) {
    const remainingPages = maxPages - pages;
    const remainingItems = maxItems - byId.size;
    if (remainingPages <= 0) {
      return {
        items: Array.from(byId.values()),
        status: "truncated",
        pages,
        reason: "page_limit",
      };
    }
    if (remainingItems <= 0) {
      return {
        items: Array.from(byId.values()),
        status: "truncated",
        pages,
        reason: "item_limit",
      };
    }
    const result = await fetchCursorPages<Node>(
      fetcher,
      endpoint(apiUrl, bbox),
      {
        pageSize,
        maxPages: remainingPages,
        maxItems: remainingItems,
      },
    );
    pages += result.pages;
    for (const node of result.items) byId.set(node.id, node);
    if (result.status === "truncated") {
      return {
        items: Array.from(byId.values()),
        status: "truncated",
        pages,
        reason: result.reason,
      };
    }
  }

  return { items: Array.from(byId.values()), status: "complete", pages };
}
