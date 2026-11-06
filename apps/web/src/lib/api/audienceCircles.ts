/** Voluntary membership scopes; never derive read rights from map positions. */
export interface AudienceCircle {
  id: string;
  kind: "household" | "community";
  name: string;
  state: "forming" | "active" | "inactive";
  membership_status: "invited" | "active";
}

export interface AudienceCircleMember {
  account_id: string;
  title: string;
}

export class AudienceCircleError extends Error {
  constructor(public readonly status: number) {
    super(
      status === 401
        ? "Bitte melde dich erneut an."
        : status === 503 || status === 404
          ? "Die Hausgemeinschaft ist derzeit nicht verfügbar."
          : "Die Aktion konnte nicht abgeschlossen werden.",
    );
    this.name = "AudienceCircleError";
  }
}

async function request<T>(url: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(url, {
    ...init,
    credentials: "include",
    cache: "no-store",
    headers: {
      ...(init.body ? { "Content-Type": "application/json" } : {}),
      ...init.headers,
    },
  });
  if (!response.ok) throw new AudienceCircleError(response.status);
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export const listMyCircles = (signal?: AbortSignal) =>
  request<AudienceCircle[]>("/api/circles/me", { signal });

export const listCircleMembers = (id: string, signal?: AbortSignal) =>
  request<AudienceCircleMember[]>(
    `/api/circles/${encodeURIComponent(id)}/members`,
    { signal },
  );

export const createHousehold = (inviteeAccountId: string) =>
  request<AudienceCircle>("/api/circles", {
    method: "POST",
    body: JSON.stringify({
      kind: "household",
      invitee_account_id: inviteeAccountId,
    }),
  });

export const acceptCircle = (id: string) =>
  request<AudienceCircle>(`/api/circles/${encodeURIComponent(id)}/accept`, {
    method: "POST",
  });

export const inviteToCircle = (id: string, accountId: string) =>
  request<void>(`/api/circles/${encodeURIComponent(id)}/invitations`, {
    method: "POST",
    body: JSON.stringify({ account_id: accountId }),
  });

export const leaveCircle = (id: string) =>
  request<void>(`/api/circles/${encodeURIComponent(id)}/membership`, {
    method: "DELETE",
  });
