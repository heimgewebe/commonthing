import {
  fetchCursorPages,
  MAP_CURSOR_MAX_ITEMS,
  MAP_CURSOR_MAX_PAGES,
  MAP_CURSOR_PAGE_SIZE,
  type CursorPaginationOptions,
  type CursorTruncationReason,
} from "./cursorPagination";
import type { Node } from "./types";
import {
  NodeViewportError,
  nodeViewportBboxes,
  type NodeViewportBounds,
} from "./nodeViewportBounds";

export {
  NodeViewportError,
  nodeViewportBboxes,
  nodeViewportContains,
  type NodeViewportBounds,
} from "./nodeViewportBounds";

type FetchLike = (input: string) => Promise<Response>;

export type NodeViewportResult =
  | { items: Node[]; status: "complete"; pages: number }
  | {
      items: Node[];
      status: "truncated";
      pages: number;
      reason: CursorTruncationReason;
    };

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
