import {
  summarizeMapResourceStatus,
  type Account,
  type Edge,
  type MapLoadState,
  type MapResourceName,
  type MapResourceStatus,
  type Node,
  type Webgemeindezentrum,
} from "./types";

export const MAP_CURSOR_PAGE_SIZE = 1000;
export const MAP_CURSOR_MAX_PAGES = 10;
export const MAP_CURSOR_MAX_ITEMS = 10_000;
export const MAP_RESOURCE_LOAD_DEADLINE_MS = 8_000;

export type CursorTruncationReason = "page_limit" | "item_limit";

export type CursorPaginationResult<T> =
  | {
      items: T[];
      status: "complete";
      pages: number;
    }
  | {
      items: T[];
      status: "truncated";
      pages: number;
      reason: CursorTruncationReason;
    };

export type CursorPaginationOptions = {
  pageSize?: number;
  maxPages?: number;
  maxItems?: number;
  signal?: AbortSignal;
};

export type MapResourceTransport = "cursor" | "static-list";
export type MapNodeLoadMode = "global" | "viewport";

export type MapResourceLoadOptions = {
  nodeLoadMode?: MapNodeLoadMode;
  focusedNodeId?: string | null;
  resourceDeadlineMs?: number;
};

type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

type CursorEnvelope<T> = {
  items: T[];
  page: {
    limit: number;
    next_cursor: string | null;
    has_more: boolean;
  };
};

export class CursorPaginationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "CursorPaginationError";
  }
}

function fetchWithSignal(
  fetcher: FetchLike,
  input: string,
  signal?: AbortSignal,
): Promise<Response> {
  return signal ? fetcher(input, { signal }) : fetcher(input);
}

async function withResourceDeadline<T>(
  resource: MapResourceName,
  deadlineMs: number,
  operation: (signal: AbortSignal) => Promise<T>,
): Promise<T> {
  const controller = new AbortController();
  let timeout: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<never>((_resolve, reject) => {
    timeout = setTimeout(() => {
      reject(
        new CursorPaginationError(
          `Timed out loading ${resource} after ${deadlineMs} ms`,
        ),
      );
      controller.abort();
    }, deadlineMs);
  });

  try {
    return await Promise.race([operation(controller.signal), deadline]);
  } finally {
    if (timeout !== undefined) clearTimeout(timeout);
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function positiveInteger(value: number, label: string): number {
  if (!Number.isInteger(value) || value <= 0) {
    throw new CursorPaginationError(`${label} must be a positive integer`);
  }
  return value;
}

function parseEnvelope<T>(
  value: unknown,
  pageNumber: number,
): CursorEnvelope<T> {
  if (
    !isRecord(value) ||
    !Array.isArray(value.items) ||
    !isRecord(value.page)
  ) {
    throw new CursorPaginationError(
      `Invalid cursor response shape on page ${pageNumber}`,
    );
  }

  const limit = value.page.limit;
  const nextCursor = value.page.next_cursor;
  const hasMore = value.page.has_more;

  if (!Number.isInteger(limit) || (limit as number) <= 0) {
    throw new CursorPaginationError(`Invalid page limit on page ${pageNumber}`);
  }
  if (typeof hasMore !== "boolean") {
    throw new CursorPaginationError(
      `Invalid has_more flag on page ${pageNumber}`,
    );
  }
  if (nextCursor !== null && typeof nextCursor !== "string") {
    throw new CursorPaginationError(
      `Invalid next_cursor on page ${pageNumber}`,
    );
  }
  if (!hasMore && nextCursor !== null) {
    throw new CursorPaginationError(
      `Unexpected next_cursor on final page ${pageNumber}`,
    );
  }

  return {
    items: value.items as T[],
    page: {
      limit: limit as number,
      next_cursor: nextCursor as string | null,
      has_more: hasMore,
    },
  };
}

function cursorUrl(endpoint: string, cursor: string | null, pageSize: number) {
  const params = new URLSearchParams({
    pagination: "cursor",
    limit: String(pageSize),
  });
  if (cursor !== null) params.set("cursor", cursor);
  const separator = endpoint.includes("?") ? "&" : "?";
  return `${endpoint}${separator}${params.toString()}`;
}

/**
 * Walk a cursor endpoint until it is complete or a declared safety limit is hit.
 *
 * A safety-limit hit returns the bounded items with `status: "truncated"`.
 * Broken HTTP, JSON, envelope or cursor-progress contracts throw instead, so the
 * caller can mark the resource as failed rather than silently claim completeness.
 * Contract validation happens before safety-limit truncation, so the final allowed
 * page cannot hide a malformed continuation cursor behind a bounded result.
 */
export async function fetchCursorPages<T>(
  fetcher: FetchLike,
  endpoint: string,
  options: CursorPaginationOptions = {},
): Promise<CursorPaginationResult<T>> {
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

  const items: T[] = [];
  const seenCursors = new Set<string>();
  let cursor: string | null = null;
  let pages = 0;

  while (true) {
    const pageNumber = pages + 1;
    const response = await fetchWithSignal(
      fetcher,
      cursorUrl(endpoint, cursor, pageSize),
      options.signal,
    );
    if (!response.ok) {
      throw new CursorPaginationError(
        `HTTP ${response.status} while loading page ${pageNumber}`,
      );
    }

    let body: unknown;
    try {
      body = await response.json();
    } catch {
      throw new CursorPaginationError(`Invalid JSON on page ${pageNumber}`);
    }

    const envelope = parseEnvelope<T>(body, pageNumber);
    pages = pageNumber;

    let nextCursor: string | null = null;
    if (envelope.page.has_more) {
      if (envelope.items.length === 0) {
        throw new CursorPaginationError(
          `Cursor page ${pageNumber} claims more data without items`,
        );
      }
      nextCursor = envelope.page.next_cursor;
      if (nextCursor === null || nextCursor.length === 0) {
        throw new CursorPaginationError(
          `Missing next_cursor on page ${pageNumber}`,
        );
      }
      if (nextCursor === cursor || seenCursors.has(nextCursor)) {
        throw new CursorPaginationError(
          `Cursor did not advance on page ${pageNumber}`,
        );
      }
    }

    const remaining = maxItems - items.length;
    if (envelope.items.length > remaining) {
      items.push(...envelope.items.slice(0, remaining));
      return { items, status: "truncated", pages, reason: "item_limit" };
    }
    items.push(...envelope.items);

    if (!envelope.page.has_more) {
      return { items, status: "complete", pages };
    }
    if (items.length >= maxItems) {
      return { items, status: "truncated", pages, reason: "item_limit" };
    }
    if (pages >= maxPages) {
      return { items, status: "truncated", pages, reason: "page_limit" };
    }

    const continuationCursor = nextCursor as string;
    seenCursors.add(continuationCursor);
    cursor = continuationCursor;
  }
}

/**
 * Load one complete resource from the in-repository prerendered demo API.
 *
 * These same-origin endpoints intentionally expose their full static dataset as
 * a bare JSON array. Keeping this path explicit prevents remote cursor APIs from
 * silently falling back to the legacy response shape.
 */
async function fetchCompleteStaticList<T>(
  fetcher: FetchLike,
  endpoint: string,
  signal?: AbortSignal,
): Promise<CursorPaginationResult<T>> {
  const response = await fetchWithSignal(fetcher, endpoint, signal);
  if (!response.ok) {
    throw new CursorPaginationError(
      `HTTP ${response.status} while loading static resource`,
    );
  }

  let body: unknown;
  try {
    body = await response.json();
  } catch {
    throw new CursorPaginationError("Invalid JSON in static resource");
  }
  if (!Array.isArray(body)) {
    throw new CursorPaginationError("Invalid static resource response shape");
  }

  return { items: body as T[], status: "complete", pages: 1 };
}

export type MapResourceLoad = {
  nodes: Node[];
  accounts: Account[];
  edges: Edge[];
  webgemeindezentren: Webgemeindezentrum[];
  loadState: MapLoadState;
  loadNotice: string | null;
  resourceStatus: MapResourceStatus[];
  nodeLoadMode: MapNodeLoadMode;
};

async function fetchFocusedNode(
  fetcher: FetchLike,
  apiUrl: string,
  nodeId: string,
  signal?: AbortSignal,
): Promise<Node | null> {
  const response = await fetchWithSignal(
    fetcher,
    `${apiUrl}/api/nodes/${encodeURIComponent(nodeId)}`,
    signal,
  );
  if (response.status === 404) return null;
  if (!response.ok) {
    throw new CursorPaginationError(
      `HTTP ${response.status} while loading focused node`,
    );
  }
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    throw new CursorPaginationError("Invalid JSON while loading focused node");
  }
  if (
    !isRecord(body) ||
    body.id !== nodeId ||
    !isRecord(body.location) ||
    typeof body.location.lat !== "number" ||
    !Number.isFinite(body.location.lat) ||
    typeof body.location.lon !== "number" ||
    !Number.isFinite(body.location.lon)
  ) {
    throw new CursorPaginationError("Invalid focused node response shape");
  }
  return body as unknown as Node;
}

/** Load all map resources while preserving complete, truncated and failed truth. */
export async function loadMapResources(
  fetcher: FetchLike,
  apiUrl: string,
  transport: MapResourceTransport = "cursor",
  options: MapResourceLoadOptions = {},
): Promise<MapResourceLoad> {
  const nodeLoadMode = options.nodeLoadMode ?? "global";
  const resourceDeadlineMs = positiveInteger(
    options.resourceDeadlineMs ?? MAP_RESOURCE_LOAD_DEADLINE_MS,
    "resourceDeadlineMs",
  );
  async function loadResource<T>(
    resource: MapResourceName,
    fallback: T[] = [],
  ): Promise<{ items: T[]; status: MapResourceStatus }> {
    try {
      const endpoint = `${apiUrl}/api/${resource}`;
      const result = await withResourceDeadline(
        resource,
        resourceDeadlineMs,
        (signal) =>
          transport === "static-list"
            ? fetchCompleteStaticList<T>(fetcher, endpoint, signal)
            : fetchCursorPages<T>(fetcher, endpoint, { signal }),
      );
      const status: MapResourceStatus =
        result.status === "complete"
          ? {
              resource,
              status: "complete",
              loaded: result.items.length,
              pages: result.pages,
            }
          : {
              resource,
              status: "truncated",
              loaded: result.items.length,
              pages: result.pages,
              reason: result.reason,
            };
      return { items: result.items, status };
    } catch (error) {
      console.error(`Error fetching ${resource} from`, apiUrl, error);
      return {
        items: fallback,
        status: {
          resource,
          status: "failed",
          error: error instanceof Error ? error.message : String(error),
        },
      };
    }
  }

  async function loadViewportNodeBootstrap(): Promise<{
    items: Node[];
    status: MapResourceStatus;
  }> {
    try {
      const focusedNode = options.focusedNodeId
        ? await withResourceDeadline("nodes", resourceDeadlineMs, (signal) =>
            fetchFocusedNode(fetcher, apiUrl, options.focusedNodeId!, signal),
          )
        : null;
      const items = focusedNode ? [focusedNode] : [];
      return {
        items,
        status: {
          resource: "nodes",
          status: "viewport",
          loaded: items.length,
          pages: 0,
        },
      };
    } catch (error) {
      return {
        items: [],
        status: {
          resource: "nodes",
          status: "failed",
          error: error instanceof Error ? error.message : String(error),
        },
      };
    }
  }

  const [nodesResult, accountsResult, edgesResult, centersResult] =
    await Promise.all([
      nodeLoadMode === "viewport"
        ? loadViewportNodeBootstrap()
        : loadResource<Node>("nodes"),
      loadResource<Account>("accounts"),
      loadResource<Edge>("edges"),
      loadResource<Webgemeindezentrum>("webgemeindezentren"),
    ]);
  const nodes = nodesResult.items;
  const accounts = accountsResult.items;
  const edges = edgesResult.items;
  const webgemeindezentren = centersResult.items;
  const resourceStatus: MapResourceStatus[] = [
    nodesResult.status,
    accountsResult.status,
    edgesResult.status,
    centersResult.status,
  ];
  const { loadState, loadNotice } = summarizeMapResourceStatus(resourceStatus);

  return {
    nodes,
    accounts,
    edges,
    webgemeindezentren,
    loadState,
    loadNotice,
    resourceStatus,
    nodeLoadMode,
  };
}
