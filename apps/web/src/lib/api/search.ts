import { hasRenderableMapPosition } from "$lib/map/coordinates";
import { knottingTagDisplayLabel } from "$lib/knottingTopics";
import type { MapEntityNode, Node } from "$lib/map/types";

export const MAX_SEARCH_QUERY_CHARS = 512;

export type NodeSearchStatus = "idle" | "loading" | "ready" | "error";

export interface NodeSearchResponse {
  items: Node[];
  mode: string;
  fallback_reason?: string;
  generation_id: string;
  offset: number;
}

export interface SearchNodesOptions {
  kinds?: string[];
  tags?: string[];
  limit?: number;
  signal?: AbortSignal;
  fetcher?: typeof fetch;
  apiBase?: string;
}

export class SearchApiError extends Error {
  constructor(public readonly status: number) {
    super(`Search request failed with HTTP ${status}`);
    this.name = "SearchApiError";
  }
}

export function nodeToMapEntity(node: Node): MapEntityNode | null {
  const location = node.location;
  if (!location || !hasRenderableMapPosition(location)) return null;
  return {
    type: "node",
    id: node.id,
    title: node.title,
    lat: location.lat,
    lon: location.lon,
    summary: node.summary,
    info: node.info,
    kind: node.kind,
    tags: node.tags,
    modules: node.modules,
    created_at: node.created_at,
    updated_at: node.updated_at,
  };
}

export function normalizeSearchQuery(query: string): string {
  return Array.from(query.trim()).slice(0, MAX_SEARCH_QUERY_CHARS).join("");
}

export async function searchNodes(
  query: string,
  options: SearchNodesOptions = {},
): Promise<NodeSearchResponse> {
  const normalizedQuery = normalizeSearchQuery(query);
  if (!normalizedQuery) {
    throw new Error("Search query must not be empty");
  }

  const params = new URLSearchParams({
    q: normalizedQuery,
    limit: String(options.limit ?? 10),
  });
  const kinds = Array.from(
    new Set((options.kinds ?? []).map((kind) => kind.trim()).filter(Boolean)),
  ).sort();
  if (kinds.length > 0) params.set("kinds", kinds.join(","));
  const tags = Array.from(
    new Set((options.tags ?? []).map((tag) => tag.trim()).filter(Boolean)),
  ).sort();
  if (tags.length > 0) params.set("tags", tags.join(","));

  const apiBase =
    options.apiBase ?? import.meta.env.PUBLIC_GEWEBE_API_BASE ?? "";
  const fetcher = options.fetcher ?? fetch;
  const response = await fetcher(`${apiBase}/api/search?${params.toString()}`, {
    credentials: "include",
    headers: { Accept: "application/json" },
    signal: options.signal,
  });
  if (!response.ok) throw new SearchApiError(response.status);
  return (await response.json()) as NodeSearchResponse;
}

export interface SimilarNodeSource {
  title?: string | null;
  kind?: string | null;
  summary?: string | null;
  info?: string | null;
  tags?: string[] | null;
}

export function buildSimilarNodeQuery(source: SimilarNodeSource): string {
  const readableTags = (source.tags ?? []).map(knottingTagDisplayLabel);
  const parts = [
    source.title,
    source.kind,
    source.summary,
    source.info,
    ...readableTags,
  ]
    .map((part) => part?.trim())
    .filter((part): part is string => Boolean(part));
  return Array.from(parts.join(" · "))
    .slice(0, MAX_SEARCH_QUERY_CHARS)
    .join("");
}
