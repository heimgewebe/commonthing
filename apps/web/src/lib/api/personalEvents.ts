/** Account-owned system messages, not synthetic human direct conversations. */
export type PersonalEventKind =
  | "circle_invited"
  | "circle_invitation_sent"
  | "circle_joined"
  | "circle_declined"
  | "circle_withdrawn"
  | "circle_left";

export interface PersonalEvent {
  id: string;
  event_kind: PersonalEventKind;
  content: string;
  created_at: string;
  read_at: string | null;
  circle_id: string | null;
  circle_name: string | null;
  target_account_id: string | null;
  invitation_created_at: string | null;
  actionable: boolean;
}

export interface PersonalEventPage {
  items: PersonalEvent[];
  unread_count: number;
  has_more: boolean;
}

export async function listPersonalEvents(
  signal?: AbortSignal,
  before?: string,
): Promise<PersonalEventPage> {
  const path = before
    ? `/api/personal-events?before=${encodeURIComponent(before)}`
    : "/api/personal-events";
  const response = await fetch(path, {
    credentials: "include",
    cache: "no-store",
    signal,
  });
  if (!response.ok) {
    throw new Error(`personal event inbox unavailable (${response.status})`);
  }
  return response.json() as Promise<PersonalEventPage>;
}

export async function markPersonalEventRead(id: string): Promise<void> {
  const response = await fetch(
    `/api/personal-events/${encodeURIComponent(id)}/read`,
    { method: "POST", credentials: "include", cache: "no-store" },
  );
  if (!response.ok) {
    throw new Error(
      `personal event read receipt rejected (${response.status})`,
    );
  }
}
