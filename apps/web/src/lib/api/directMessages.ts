import { invalidateAccountAttention } from "$lib/accountAttention";
import type {
  ConversationMessage,
  MessagePage,
} from "$lib/api/nodeConversation";
import {
  createConversationMessage,
  listConversationMessages,
} from "$lib/api/nodeConversation";

export interface DirectConversation {
  id: string;
  counterpart_account_id: string | null;
  counterpart_title: string;
  created_at: string;
  updated_at: string;
  unread_count: number;
  last_message_preview: string | null;
  last_message_at: string | null;
  blocked_by_me: boolean;
  /**
   * Whether the server currently accepts a new message. It is false as soon as
   * the counterpart has left or either side blocks, so the composer never
   * promises delivery the server would refuse.
   */
  can_send: boolean;
}

interface DirectConversationList {
  items: DirectConversation[];
}

interface ErrorPayload {
  code?: unknown;
  message?: unknown;
}

export class DirectMessagesApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "DirectMessagesApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: "include",
    ...init,
    headers: {
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
  });
  if (!response.ok) {
    const payload = (await response.json().catch(() => ({}))) as ErrorPayload;
    throw new DirectMessagesApiError(
      response.status,
      typeof payload.code === "string" ? payload.code : "request_failed",
      typeof payload.message === "string"
        ? payload.message
        : `HTTP ${response.status}`,
    );
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export async function listDirectConversations(
  signal?: AbortSignal,
): Promise<DirectConversation[]> {
  const response = await request<DirectConversationList>(
    "/api/direct-conversations",
    { signal },
  );
  return response.items;
}

export function openDirectConversation(
  recipientAccountId: string,
): Promise<DirectConversation> {
  return request<DirectConversation>("/api/direct-conversations", {
    method: "POST",
    body: JSON.stringify({ recipient_account_id: recipientAccountId }),
  });
}

export async function markDirectConversationRead(
  conversationId: string,
  throughMessageId: string,
): Promise<void> {
  await request<void>(
    `/api/direct-conversations/${encodeURIComponent(conversationId)}/read`,
    {
      method: "POST",
      body: JSON.stringify({ through_message_id: throughMessageId }),
    },
  );
  invalidateAccountAttention();
}

/**
 * Toggle the caller's own block flag. The server answers with the refreshed
 * conversation because releasing one's own block does not necessarily restore
 * `can_send` — the counterpart may still block or may have left.
 */
export function setDirectConversationBlocked(
  conversationId: string,
  blocked: boolean,
): Promise<DirectConversation> {
  return request<DirectConversation>(
    `/api/direct-conversations/${encodeURIComponent(conversationId)}/block`,
    { method: blocked ? "PUT" : "DELETE" },
  );
}

export function listDirectMessages(
  conversationId: string,
  cursor?: string | null,
  signal?: AbortSignal,
): Promise<MessagePage> {
  return listConversationMessages(conversationId, cursor, signal);
}

export function sendDirectMessage(
  conversationId: string,
  content: string,
  idempotencyKey: string,
): Promise<ConversationMessage> {
  return createConversationMessage(conversationId, content, idempotencyKey);
}
