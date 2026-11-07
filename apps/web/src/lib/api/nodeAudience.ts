/** Owner-only control of an existing node's effective read scope. */
export type NodeAudienceScope = "public" | "private" | "circles";
export interface NodeAudience {
  node_id: string;
  scope: NodeAudienceScope | "unavailable";
  circle_ids: string[];
}

export class NodeAudienceError extends Error {
  constructor(public readonly status: number) {
    super(
      status === 401
        ? "Bitte melde dich erneut an."
        : status === 404
          ? "Der Knoten oder eine ausgewählte Gemeinschaft ist nicht mehr verfügbar."
          : status === 412
            ? "Die Sichtbarkeit wurde inzwischen verändert. Lade den Knoten neu, bevor du sie speicherst."
            : status === 409
              ? "Verborgene oder widerrufene Knoten können hier nicht freigegeben werden."
              : "Die Sichtbarkeit konnte nicht gespeichert werden.",
    );
    this.name = "NodeAudienceError";
  }
}

async function request(id: string, init: RequestInit): Promise<NodeAudience> {
  const response = await fetch(
    `/api/nodes/${encodeURIComponent(id)}/audience`,
    {
      ...init,
      credentials: "include",
      cache: "no-store",
      headers: {
        ...(init.body ? { "Content-Type": "application/json" } : {}),
        ...init.headers,
      },
    },
  );
  if (!response.ok) throw new NodeAudienceError(response.status);
  return response.json() as Promise<NodeAudience>;
}

export const getNodeAudience = (id: string, signal?: AbortSignal) =>
  request(id, { method: "GET", signal });

export const setNodeAudience = (
  id: string,
  scope: NodeAudienceScope,
  circle_ids: string[],
  expected: Pick<NodeAudience, "scope" | "circle_ids">,
) =>
  request(id, {
    method: "PUT",
    body: JSON.stringify({
      scope,
      circle_ids,
      expected_scope: expected.scope,
      expected_circle_ids: expected.circle_ids,
    }),
  });
