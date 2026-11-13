import { describe, expect, it } from "vitest";
import type { DirectConversation } from "$lib/api/directMessages";
import type {
  Proposal,
  ProposalViewerParticipation,
} from "$lib/api/governance";
import {
  attentionDeadlineLabel,
  attentionMeaningLabel,
  attentionMeaningMark,
  countUnreadDirectMessages,
  hasAcceptedWeberApplication,
  hasPendingWeberApplication,
  projectTopBarAttention,
  unreadMessageAccessibleCount,
  unreadMessageBadgeLabel,
} from "./topBarAttentionState";

const NOW = Date.parse("2026-08-17T12:00:00Z");

function conversation(
  id: string,
  unreadCount: number,
  at: string,
): DirectConversation {
  return {
    id,
    counterpart_account_id: `counterpart-${id}`,
    counterpart_title: `Person ${id}`,
    created_at: at,
    updated_at: at,
    unread_count: unreadCount,
    last_message_preview: `Nachricht ${id}`,
    last_message_at: at,
    blocked_by_me: false,
    can_send: true,
  };
}

function viewer(
  overrides: Partial<ProposalViewerParticipation> = {},
): ProposalViewerParticipation {
  return {
    vote_choice: null,
    has_veto: false,
    may_vote: false,
    may_veto: false,
    ...overrides,
  };
}

function proposal(overrides: Partial<Proposal> = {}): Proposal {
  return {
    id: "proposal-1",
    kind: "sachantrag",
    webgemeindezentrum_id: "wgz-test",
    title: "Parkbank",
    applicant_account_id: "weber-b",
    applicant_title: "Berta",
    status: "consent",
    created_at: "2026-08-17T06:00:00Z",
    consent_until: "2026-08-19T12:00:00Z",
    last_activity_at: "2026-08-17T06:00:00Z",
    veto_count: 0,
    yes_votes: 0,
    no_votes: 0,
    abstain_votes: 0,
    viewer_participation: null,
    ...overrides,
  };
}

describe("topBarAttentionState", () => {
  it("recognizes pending and accepted Weber applications only for the current account", () => {
    const proposals = [
      proposal({
        kind: "weberantrag",
        applicant_account_id: "guest-a",
        status: "consent",
      }),
      proposal({
        id: "p2",
        kind: "weberantrag",
        applicant_account_id: "guest-b",
        status: "voting",
      }),
      proposal({
        id: "p3",
        kind: "weberantrag",
        applicant_account_id: "guest-a",
        status: "accepted",
      }),
    ];
    expect(hasPendingWeberApplication(proposals, "guest-a")).toBe(true);
    expect(hasPendingWeberApplication(proposals, "guest-c")).toBe(false);
    expect(hasAcceptedWeberApplication(proposals, "guest-a")).toBe(true);
    expect(hasAcceptedWeberApplication(proposals, "guest-b")).toBe(false);
  });

  it("bounds unread counts and accessible labels", () => {
    const conversations = [
      conversation("a", 60, "2026-08-17T10:00:00Z"),
      conversation("b", 50, "2026-08-17T11:00:00Z"),
    ];
    expect(countUnreadDirectMessages(conversations)).toBe(100);
    expect(unreadMessageBadgeLabel(4)).toBe("4");
    expect(unreadMessageBadgeLabel(100)).toBe("99+");
    expect(unreadMessageBadgeLabel(Number.POSITIVE_INFINITY)).toBe("0");
    expect(unreadMessageAccessibleCount(1)).toBe("1 ungelesene Nachricht");
    expect(unreadMessageAccessibleCount(100)).toBe(
      "99 oder mehr ungelesene Nachrichten",
    );
  });

  it("keeps meaning labels and explicit visual marks stable", () => {
    expect(attentionMeaningLabel("required")).toBe("Handlung erforderlich");
    expect(attentionMeaningLabel("new")).toBe("Neu für dich");
    expect(attentionMeaningLabel("available")).toBe("Mitwirkung möglich");
    expect(attentionMeaningLabel("waiting")).toBe("Läuft ohne dein Zutun");
    expect(attentionMeaningMark("required")).toBe("!");
    expect(attentionMeaningMark("new")).toBe("•");
    expect(attentionMeaningMark("available")).toBe("+");
    expect(attentionMeaningMark("waiting")).toBe("…");
  });

  it("formats only live canonical deadlines", () => {
    expect(attentionDeadlineLabel("2026-08-17T13:30:00Z", NOW)).toBe(
      "Endet in 1 Std. 30 Min.",
    );
    expect(attentionDeadlineLabel("2026-08-17T12:00:30Z", NOW)).toBe(
      "Endet in unter 1 Min.",
    );
    expect(attentionDeadlineLabel("2026-08-17T11:59:59Z", NOW)).toBeUndefined();
    expect(attentionDeadlineLabel("invalid", NOW)).toBeUndefined();
  });

  it("projects unread direct conversations as new personal attention", () => {
    const items = projectTopBarAttention({
      accountId: "weber-a",
      role: "weber",
      nowMs: NOW,
      conversations: [
        conversation("conversation-1", 4, "2026-08-17T11:00:00Z"),
      ],
      proposals: [],
    });
    expect(items).toEqual([
      expect.objectContaining({
        id: "direct:conversation-1",
        kind: "direct_message",
        meaning: "new",
        count: 4,
      }),
    ]);
  });

  it("aggregates all own open proposals into one quiet summary", () => {
    const items = projectTopBarAttention({
      accountId: "weber-a",
      role: "weber",
      nowMs: NOW,
      conversations: [],
      proposals: [
        proposal({
          id: "own-weber",
          kind: "weberantrag",
          applicant_account_id: "weber-a",
        }),
        proposal({
          id: "own-sach",
          applicant_account_id: "weber-a",
          title: "Garten",
        }),
      ],
    });
    expect(items).toEqual([
      expect.objectContaining({
        id: "waiting-summary:weber-a",
        kind: "waiting_summary",
        meaning: "waiting",
        label: "2 eigene Vorgänge laufen",
        detail: "Du musst gerade nichts tun.",
        count: 2,
        href: "/antraege",
      }),
    ]);
  });

  it("keeps the quiet waiting summary behind active attention", () => {
    const items = projectTopBarAttention({
      accountId: "weber-a",
      role: "weber",
      nowMs: NOW,
      conversations: [conversation("new", 1, "2026-08-17T11:00:00Z")],
      proposals: [proposal({ applicant_account_id: "weber-a" })],
    });
    expect(items.map((item) => item.id)).toEqual([
      "direct:new",
      "waiting-summary:weber-a",
    ]);
  });

  it("keeps unread information ahead of optional participation even near a deadline", () => {
    const items = projectTopBarAttention({
      accountId: "weber-a",
      role: "weber",
      nowMs: NOW,
      conversations: [conversation("newer-message", 1, "2026-08-17T11:59:00Z")],
      proposals: [
        proposal({
          id: "urgent-vote",
          status: "voting",
          consent_until: "2026-08-16T12:00:00Z",
          voting_until: "2026-08-17T13:00:00Z",
          viewer_participation: viewer({ may_vote: true }),
        }),
      ],
    });
    expect(items.map((item) => item.id)).toEqual([
      "direct:newer-message",
      "proposal:urgent-vote",
    ]);
    expect(items[1].deadlineLabel).toBe("Endet in 1 Std. 0 Min.");
  });

  it("uses server remaining time instead of a skewed device wall clock", () => {
    const skewedNow = Date.parse("2030-01-01T00:00:00Z");
    const items = projectTopBarAttention({
      accountId: "weber-a",
      role: "weber",
      nowMs: skewedNow,
      proposalsObservedAtMs: skewedNow,
      conversations: [],
      proposals: [
        proposal({
          id: "server-timed-vote",
          status: "voting",
          voting_until: "2026-08-17T13:00:00Z",
          remaining_seconds: 3_600,
          viewer_participation: viewer({ may_vote: true }),
        }),
      ],
    });

    expect(items[0]).toMatchObject({
      id: "proposal:server-timed-vote",
      deadlineLabel: "Endet in 1 Std. 0 Min.",
      remainingMs: 3_600_000,
    });
  });

  it("keeps far-deadline optional participation behind new information", () => {
    const items = projectTopBarAttention({
      accountId: "weber-a",
      role: "weber",
      nowMs: NOW,
      conversations: [conversation("message", 1, "2026-08-17T08:00:00Z")],
      proposals: [
        proposal({
          id: "later-vote",
          status: "voting",
          consent_until: "2026-08-16T12:00:00Z",
          voting_until: "2026-08-18T12:01:00Z",
          viewer_participation: viewer({ may_vote: true }),
        }),
      ],
    });
    expect(items.map((item) => item.id)).toEqual([
      "direct:message",
      "proposal:later-vote",
    ]);
  });

  it("keeps legacy proposals visible without inventing an activity timestamp", () => {
    const legacy = proposal({
      id: "legacy",
      last_activity_at: undefined,
      viewer_participation: viewer({ may_veto: true }),
    });
    const current = proposal({
      id: "current",
      last_activity_at: "2026-08-17T10:00:00Z",
      viewer_participation: viewer({ may_veto: true }),
    });

    const items = projectTopBarAttention({
      accountId: "weber-a",
      role: "weber",
      nowMs: NOW,
      conversations: [],
      proposals: [legacy, current],
    });

    expect(items.map((item) => item.id)).toEqual([
      "proposal:current",
      "proposal:legacy",
    ]);
    expect(items[1].occurredAt).toBeUndefined();
  });

  it("orders optional participation by relevant activity rather than deadline", () => {
    const items = projectTopBarAttention({
      accountId: "weber-a",
      role: "weber",
      nowMs: NOW,
      conversations: [],
      proposals: [
        proposal({
          id: "older-early-deadline",
          created_at: "2026-08-17T11:30:00Z",
          consent_until: "2026-08-17T13:00:00Z",
          last_activity_at: "2026-08-17T09:00:00Z",
          viewer_participation: viewer({ may_veto: true }),
        }),
        proposal({
          id: "newer-late-deadline",
          created_at: "2026-08-17T05:00:00Z",
          consent_until: "2026-08-17T20:00:00Z",
          last_activity_at: "2026-08-17T10:00:00Z",
          viewer_participation: viewer({ may_veto: true }),
        }),
      ],
    });
    expect(items.map((item) => item.id)).toEqual([
      "proposal:newer-late-deadline",
      "proposal:older-early-deadline",
    ]);
  });

  it("drops locally expired availability even if retained server facts still say may_vote", () => {
    const items = projectTopBarAttention({
      accountId: "weber-a",
      role: "weber",
      nowMs: NOW,
      conversations: [],
      proposals: [
        proposal({
          status: "voting",
          voting_until: "2026-08-17T11:59:59Z",
          viewer_participation: viewer({ may_vote: true }),
        }),
      ],
    });
    expect(items).toEqual([]);
  });

  it("projects voting only when explicit viewer participation says no vote exists", () => {
    const eligible = proposal({
      status: "voting",
      voting_until: "2026-08-17T13:00:00Z",
      viewer_participation: viewer({ may_vote: true, vote_choice: null }),
    });
    expect(
      projectTopBarAttention({
        accountId: "weber-a",
        role: "weber",
        nowMs: NOW,
        conversations: [],
        proposals: [eligible],
      }),
    ).toHaveLength(1);
    eligible.viewer_participation = viewer({
      may_vote: true,
      vote_choice: "ja",
    });
    expect(
      projectTopBarAttention({
        accountId: "weber-a",
        role: "weber",
        nowMs: NOW,
        conversations: [],
        proposals: [eligible],
      }),
    ).toEqual([]);
  });

  it("projects a fresh veto only from explicit viewer participation", () => {
    const items = projectTopBarAttention({
      accountId: "weber-a",
      role: "weber",
      nowMs: NOW,
      conversations: [],
      proposals: [
        proposal({
          consent_until: "2026-08-17T13:00:00Z",
          viewer_participation: viewer({ may_veto: true }),
        }),
      ],
    });
    expect(items[0]).toMatchObject({
      meaning: "available",
      detail: "Du kannst ein begründetes Veto einlegen",
    });
  });

  it("does not infer participation from role or phase and masks a role downgrade", () => {
    const p = proposal({
      consent_until: "2026-08-17T13:00:00Z",
      viewer_participation: viewer({ may_veto: true }),
    });
    expect(
      projectTopBarAttention({
        accountId: "weber-a",
        role: "weber",
        nowMs: NOW,
        conversations: [],
        proposals: [proposal({ viewer_participation: null })],
      }),
    ).toEqual([]);
    expect(
      projectTopBarAttention({
        accountId: "guest-a",
        role: "gast",
        nowMs: NOW,
        conversations: [],
        proposals: [p],
      }),
    ).toEqual([]);
  });

  it("uses stable identity as the final tie-breaker", () => {
    const items = projectTopBarAttention({
      accountId: "weber-a",
      role: "weber",
      nowMs: NOW,
      proposals: [],
      conversations: [
        conversation("zeta", 1, "2026-08-17T11:00:00Z"),
        conversation("alpha", 1, "2026-08-17T11:00:00Z"),
      ],
    });
    expect(items.map((item) => item.id)).toEqual([
      "direct:alpha",
      "direct:zeta",
    ]);
  });
});
