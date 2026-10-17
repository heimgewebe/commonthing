import type { GarnrolleMapState, Location, Node } from "$lib/map/types";

/**
 * Thrown when a domain write is rejected by the API. Carries the HTTP status so callers can map it to an understandable,
 * German, user-facing message without leaking backend/internal detail.
 */
export class ApiRequestError extends Error {
  constructor(
    public status: number,
    public body?: unknown,
  ) {
    super(`API request failed: ${status}`);
  }
}

function isNodeVersionConflictBody(body: unknown, expectedId: string): boolean {
  if (typeof body !== "object" || body === null || Array.isArray(body)) {
    return false;
  }
  const node = body as Record<string, unknown>;
  return (
    node.id === expectedId &&
    typeof node.title === "string" &&
    typeof node.updated_at === "string"
  );
}

export async function readErrorBody(response: Response): Promise<unknown> {
  const text = await response.text().catch(() => "");
  if (!text) return undefined;

  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}

export function preserveOnlyMatchingNodeConflict(
  error: unknown,
  expectedId: string,
): never {
  if (
    error instanceof ApiRequestError &&
    error.status === 412 &&
    !isNodeVersionConflictBody(error.body, expectedId)
  ) {
    error.body = undefined;
  }
  throw error;
}

async function requestJson<T>(
  path: string,
  method: "POST" | "PATCH" | "PUT",
  payload: unknown,
  etag?: string,
  signal?: AbortSignal,
): Promise<T> {
  const headers: HeadersInit = { "Content-Type": "application/json" };
  if (etag) {
    headers["If-Match"] = `"${etag}"`;
  }
  const res = await fetch(path, {
    method,
    headers,
    body: JSON.stringify(payload),
    credentials: "include",
    signal,
  });
  if (!res.ok) {
    throw new ApiRequestError(res.status, await readErrorBody(res));
  }
  return res.json();
}

async function postJson<T>(path: string, payload: unknown): Promise<T> {
  return requestJson<T>(path, "POST", payload);
}

async function patchJson<T>(
  path: string,
  payload: unknown,
  etag?: string,
  signal?: AbortSignal,
): Promise<T> {
  return requestJson<T>(path, "PATCH", payload, etag, signal);
}

async function putJson<T>(
  path: string,
  payload: unknown,
  etag?: string,
): Promise<T> {
  return requestJson<T>(path, "PUT", payload, etag);
}

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(path, { credentials: "include", signal });
  if (!res.ok) {
    throw new ApiRequestError(res.status, await readErrorBody(res));
  }
  return res.json();
}

export interface OwnGarnrolleProfile {
  id: string;
  title: string;
  summary?: string | null;
  tags: string[];
  address?: string | null;
  location?: Location | null;
  map_state: GarnrolleMapState;
  radius_m: number;
}

export interface UpdateOwnGarnrollePayload {
  title: string;
  summary?: string;
  tags: string[];
  address?: string;
  clear_address?: boolean;
  location?: Location;
  clear_location?: boolean;
  map_state: GarnrolleMapState;
  radius_m?: number;
}

/** GET /api/accounts/me/profile — private profile of the active account only. */
export function getOwnGarnrolleProfile(
  signal?: AbortSignal,
): Promise<OwnGarnrolleProfile> {
  return getJson<OwnGarnrolleProfile>("/api/accounts/me/profile", signal);
}

/** PATCH /api/accounts/me/profile — update only the active account's Garnrolle. */
export function updateOwnGarnrolle(
  payload: UpdateOwnGarnrollePayload,
  signal?: AbortSignal,
): Promise<OwnGarnrolleProfile> {
  return patchJson<OwnGarnrolleProfile>(
    "/api/accounts/me/profile",
    payload,
    undefined,
    signal,
  );
}

export interface CreateNodePayload {
  title: string;
  kind: string;
  address?: string | null;
  location?: Location | null;
  summary?: string;
  tags: string[];
  /** Stable UUID for retrying one user action after an uncertain response. */
  operation_id: string;
}

/** POST /api/nodes — create a node. Server owns `id`/`created_at`/`updated_at`. */
export function createNode(payload: CreateNodePayload): Promise<Node> {
  return postJson<Node>("/api/nodes", payload);
}

export interface ReplaceNodePayload {
  title: string;
  kind: string;
  address?: string | null;
  location?: Location | null;
  summary?: string;
  info?: string;
  tags: string[];
}

/** PUT /api/nodes/:id — replace the editable fields of a shared node. */
export function replaceNode(
  id: string,
  payload: ReplaceNodePayload,
  etag?: string,
): Promise<Node> {
  return putJson<Node>(
    `/api/nodes/${encodeURIComponent(id)}`,
    payload,
    etag,
  ).catch((error) => preserveOnlyMatchingNodeConflict(error, id));
}

export type {
  NodeDeleteConversationEffect,
  NodeDeleteReceipt,
} from "./nodeDelete";

/** Lazily load node removal so unrelated routes do not ship its receipt parser. */
export async function deleteNode(id: string, etag?: string) {
  const { deleteNode: remove } = await import("./nodeDelete");
  return remove(id, etag);
}
