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

// Read each server-bounded page. An arbitrary flood of invitations must not
// make older active circles inaccessible to their actual members.
const CIRCLE_PAGE_SIZE = 100;
export async function listMyCircles(
  signal?: AbortSignal,
): Promise<AudienceCircle[]> {
  const circles: AudienceCircle[] = [];
  const seen = new Set<string>();
  let after: string | undefined;
  for (;;) {
    const page = await request<AudienceCircle[]>(
      after
        ? `/api/circles/me?after=${encodeURIComponent(after)}`
        : "/api/circles/me",
      { signal },
    );
    for (const circle of page) {
      if (seen.has(circle.id)) continue;
      seen.add(circle.id);
      circles.push(circle);
    }
    if (page.length < CIRCLE_PAGE_SIZE) return circles;
    const next = page[page.length - 1]?.id;
    if (!next || next === after) throw new AudienceCircleError(502);
    after = next;
  }
}

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

/** Reject only if the actor is still invited; never withdraw an accepted
 * membership because a second tab shows an outdated decline button. */
export const declineCircle = (id: string) =>
  request<void>(
    `/api/circles/${encodeURIComponent(id)}/membership?expected_status=invited`,
    { method: "DELETE" },
  );

/** Inviter-only withdrawal; applies only to a still pending invitation. */
export const withdrawCircleInvitation = (id: string, accountId: string) =>
  request<void>(
    `/api/circles/${encodeURIComponent(id)}/invitations/${encodeURIComponent(accountId)}`,
    { method: "DELETE" },
  );
