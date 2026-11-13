import type { DirectConversation } from "$lib/api/directMessages";
import { formatRemaining, type Proposal } from "$lib/api/governance";
import type { AuthRole } from "$lib/auth/store";

const UNREAD_COUNT_OVERFLOW = 100;

export type AttentionItemKind =
  | "direct_message"
  | "weber_application"
  | "own_proposal"
  | "waiting_summary"
  | "governance";

export type AttentionMeaning = "required" | "new" | "available" | "waiting";

export interface AttentionItem {
  id: string;
  kind: AttentionItemKind;
  meaning: AttentionMeaning;
  label: string;
  detail: string;
  href: string;
  /** Kanonischer Ereigniszeitpunkt; bei gemischten Altständen vorübergehend unbekannt. */
  occurredAt?: string;
  deadline?: string;
  deadlineLabel?: string;
  /** Ephemeral projection value; never domain truth or persisted state. */
  remainingMs?: number;
  count?: number;
}

export interface AttentionProjectionInput {
  conversations: DirectConversation[];
  proposals: Proposal[];
  accountId?: string;
  role: AuthRole;
  nowMs: number;
  proposalsObservedAtMs?: number;
}

function isOwnWeberApplication(proposal: Proposal, accountId: string): boolean {
  return (
    proposal.kind === "weberantrag" &&
    proposal.applicant_account_id === accountId
  );
}

function isOwnProposal(proposal: Proposal, accountId: string): boolean {
  return proposal.applicant_account_id === accountId;
}

function boundedUnreadCount(value: number): number {
  if (!Number.isFinite(value) || !Number.isInteger(value) || value <= 0) {
    return 0;
  }
  return Math.min(value, UNREAD_COUNT_OVERFLOW);
}

function proposalLabel(proposal: Proposal): string {
  if (proposal.kind === "sachantrag") {
    return proposal.title?.trim() || "Sachantrag ohne Titel";
  }
  return `Weberstatus für ${proposal.applicant_title}`;
}

function proposalDeadline(proposal: Proposal): string | undefined {
  return proposal.status === "voting"
    ? proposal.voting_until
    : proposal.consent_until;
}

function sourceTime(value: string | undefined): number {
  if (!value) return Number.NEGATIVE_INFINITY;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : Number.NEGATIVE_INFINITY;
}

function proposalRemainingMs(
  proposal: Proposal,
  deadline: string | undefined,
  nowMs: number,
  proposalsObservedAtMs: number | undefined,
): number {
  const serverRemainingSeconds = proposal.remaining_seconds;
  if (
    typeof serverRemainingSeconds === "number" &&
    Number.isFinite(serverRemainingSeconds) &&
    serverRemainingSeconds >= 0 &&
    typeof proposalsObservedAtMs === "number" &&
    Number.isFinite(proposalsObservedAtMs)
  ) {
    // The server calculated remaining_seconds from its own clock. The client
    // only subtracts elapsed time since that response was accepted, so a
    // misconfigured device wall clock cannot hide or promote participation.
    const elapsedMs = Math.max(0, nowMs - proposalsObservedAtMs);
    return Math.max(0, serverRemainingSeconds * 1000 - elapsedMs);
  }

  // Compatibility fallback for older/mocked responses without remaining_seconds.
  const deadlineMs = sourceTime(deadline);
  return Number.isFinite(deadlineMs)
    ? deadlineMs - nowMs
    : Number.POSITIVE_INFINITY;
}

function attentionRank(item: AttentionItem): number {
  switch (item.meaning) {
    case "required":
      return 0;
    case "new":
      return 1;
    case "available":
      return 2;
    case "waiting":
      return 3;
  }
}

export function attentionMeaningLabel(meaning: AttentionMeaning): string {
  switch (meaning) {
    case "required":
      return "Handlung erforderlich";
    case "new":
      return "Neu für dich";
    case "available":
      return "Mitwirkung möglich";
    case "waiting":
      return "Läuft ohne dein Zutun";
  }
}

export function attentionMeaningMark(meaning: AttentionMeaning): string {
  switch (meaning) {
    case "required":
      return "!";
    case "new":
      return "•";
    case "available":
      return "+";
    case "waiting":
      return "…";
  }
}

export function attentionDeadlineLabel(
  deadline: string | undefined,
  nowMs: number,
  calibratedRemainingMs?: number,
): string | undefined {
  const remainingMs =
    typeof calibratedRemainingMs === "number" &&
    Number.isFinite(calibratedRemainingMs)
      ? calibratedRemainingMs
      : sourceTime(deadline) - nowMs;
  if (!Number.isFinite(remainingMs) || remainingMs <= 0) return undefined;
  const seconds = Math.max(1, Math.ceil(remainingMs / 1000));
  if (seconds < 60) return "Endet in unter 1 Min.";
  return `Endet in ${formatRemaining(seconds)}`;
}

export function hasPendingWeberApplication(
  proposals: Proposal[],
  accountId: string | undefined,
): boolean {
  if (!accountId) return false;
  return proposals.some(
    (proposal) =>
      isOwnWeberApplication(proposal, accountId) &&
      (proposal.status === "consent" || proposal.status === "voting"),
  );
}

export function hasAcceptedWeberApplication(
  proposals: Proposal[],
  accountId: string | undefined,
): boolean {
  if (!accountId) return false;
  return proposals.some(
    (proposal) =>
      isOwnWeberApplication(proposal, accountId) &&
      proposal.status === "accepted",
  );
}

export function countUnreadDirectMessages(
  conversations: DirectConversation[],
): number {
  return conversations.reduce(
    (sum, conversation) =>
      Math.min(
        UNREAD_COUNT_OVERFLOW,
        sum + boundedUnreadCount(conversation.unread_count),
      ),
    0,
  );
}

export function unreadMessageBadgeLabel(count: number): string {
  const bounded = boundedUnreadCount(count);
  return bounded >= UNREAD_COUNT_OVERFLOW ? "99+" : String(bounded);
}

export function unreadMessageAccessibleCount(count: number): string {
  const bounded = boundedUnreadCount(count);
  if (bounded >= UNREAD_COUNT_OVERFLOW) {
    return "99 oder mehr ungelesene Nachrichten";
  }
  if (bounded === 1) return "1 ungelesene Nachricht";
  return `${bounded} ungelesene Nachrichten`;
}

export function projectTopBarAttention({
  conversations,
  proposals,
  accountId,
  role,
  nowMs,
  proposalsObservedAtMs,
}: AttentionProjectionInput): AttentionItem[] {
  if (!accountId) return [];

  const activeItems: AttentionItem[] = [];
  const waitingProposals: Proposal[] = [];

  for (const conversation of conversations) {
    const unread = boundedUnreadCount(conversation.unread_count);
    if (unread === 0) continue;
    const occurredAt = conversation.last_message_at ?? conversation.updated_at;
    if (!occurredAt) continue;
    activeItems.push({
      id: `direct:${conversation.id}`,
      kind: "direct_message",
      meaning: "new",
      label: conversation.counterpart_title || "Private Nachricht",
      detail: unreadMessageAccessibleCount(unread),
      href: `/nachrichten?id=${encodeURIComponent(conversation.id)}`,
      occurredAt,
      count: unread,
    });
  }

  const formalRole = role === "weber" || role === "admin";
  for (const proposal of proposals) {
    if (proposal.status !== "consent" && proposal.status !== "voting") continue;

    const occurredAt = proposal.last_activity_at;

    if (isOwnProposal(proposal, accountId)) {
      waitingProposals.push(proposal);
      continue;
    }

    const viewer = proposal.viewer_participation;
    if (!formalRole || !viewer) continue;
    const deadline = proposalDeadline(proposal);
    const remainingMs = proposalRemainingMs(
      proposal,
      deadline,
      nowMs,
      proposalsObservedAtMs,
    );
    if (!Number.isFinite(remainingMs) || remainingMs <= 0) continue;

    const mayParticipate =
      viewer.may_veto || (viewer.may_vote && viewer.vote_choice === null);
    if (!mayParticipate) continue;

    activeItems.push({
      id: `proposal:${proposal.id}`,
      kind: "governance",
      meaning: "available",
      label: proposalLabel(proposal),
      detail:
        proposal.status === "voting"
          ? "Du kannst noch abstimmen"
          : "Du kannst ein begründetes Veto einlegen",
      href: `/antraege?id=${encodeURIComponent(proposal.id)}`,
      occurredAt,
      deadline,
      deadlineLabel: attentionDeadlineLabel(deadline, nowMs, remainingMs),
      remainingMs,
    });
  }

  if (waitingProposals.length > 0) {
    const newestWaiting = waitingProposals
      .map((proposal) => proposal.last_activity_at)
      .filter((value): value is string => Boolean(value))
      .sort((left, right) => sourceTime(right) - sourceTime(left))[0];
    const count = waitingProposals.length;
    activeItems.push({
      id: `waiting-summary:${accountId}`,
      kind: "waiting_summary",
      meaning: "waiting",
      label:
        count === 1
          ? "1 eigener Vorgang läuft"
          : `${count} eigene Vorgänge laufen`,
      detail: "Du musst gerade nichts tun.",
      href: "/antraege",
      occurredAt: newestWaiting,
      count,
    });
  }

  activeItems.sort((left, right) => {
    const leftRank = attentionRank(left);
    const rightRank = attentionRank(right);
    if (leftRank !== rightRank) return leftRank - rightRank;

    const byTime = sourceTime(right.occurredAt) - sourceTime(left.occurredAt);
    if (byTime) return byTime;
    return left.id.localeCompare(right.id);
  });

  return activeItems;
}
