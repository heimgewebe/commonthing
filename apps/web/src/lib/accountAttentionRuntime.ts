import { derived, get, writable, type Readable } from "svelte/store";
import { accountAttentionInvalidation } from "$lib/accountAttention";
import {
  listDirectConversations,
  type DirectConversation,
} from "$lib/api/directMessages";
import { listProposals, type Proposal } from "$lib/api/governance";
import { authStore, type AuthRole, type AuthStatus } from "$lib/auth/store";
import {
  hasAcceptedWeberApplication,
  hasPendingWeberApplication,
  projectTopBarAttention,
  type AttentionItem,
} from "$lib/components/topBarAttentionState";

export type WeberApplicationState = "unknown" | "available" | "pending";

export interface AccountAttentionState {
  accountId: string;
  role: AuthRole;
  weberApplicationState: WeberApplicationState;
  conversations: DirectConversation[];
  proposals: Proposal[];
  proposalsObservedAtMs?: number;
  items: AttentionItem[];
}

export interface AccountAttentionControllerDependencies {
  getAuthStatus: () => AuthStatus;
  checkAuth: (options?: { force?: boolean }) => Promise<AuthStatus>;
  listDirectConversations: (
    signal?: AbortSignal,
  ) => Promise<DirectConversation[]>;
  listProposals: (signal?: AbortSignal) => Promise<Proposal[]>;
}

export interface AccountAttentionController extends Readable<AccountAttentionState> {
  refresh: (status?: AuthStatus) => Promise<void>;
  refreshMessages: (status?: AuthStatus, signal?: AbortSignal) => Promise<void>;
  refreshProposals: (
    status?: AuthStatus,
    signal?: AbortSignal,
  ) => Promise<void>;
  reproject: () => void;
}

const MESSAGE_POLL_MS = 30_000;
const PROPOSAL_POLL_MS = 60_000;
const BACKGROUND_REFRESH_TIMEOUT_MS = 10_000;

export interface BoundedBackgroundRefresh {
  trigger: () => void;
  cancel: () => void;
}

export function createCoalescedForegroundRefresh(
  refresh: () => Promise<void>,
): () => void {
  let active: Promise<void> | null = null;

  return () => {
    if (active) return;

    let request: Promise<void>;
    try {
      request = refresh();
    } catch {
      return;
    }
    active = request;
    void request.then(
      () => {
        if (active === request) active = null;
      },
      () => {
        if (active === request) active = null;
      },
    );
  };
}

export function createBoundedBackgroundRefresh(
  refresh: (signal: AbortSignal) => Promise<void>,
  timeoutMs = BACKGROUND_REFRESH_TIMEOUT_MS,
): BoundedBackgroundRefresh {
  let activeController: AbortController | null = null;
  let timeout: ReturnType<typeof setTimeout> | null = null;

  const release = (controller: AbortController) => {
    if (activeController !== controller) return;
    activeController = null;
    if (timeout !== null) {
      clearTimeout(timeout);
      timeout = null;
    }
  };

  const cancel = () => {
    const controller = activeController;
    if (!controller) return;
    controller.abort();
    release(controller);
  };

  const trigger = () => {
    if (activeController) return;
    const controller = new AbortController();
    activeController = controller;
    timeout = setTimeout(() => {
      controller.abort();
      release(controller);
    }, timeoutMs);
    try {
      void refresh(controller.signal).then(
        () => release(controller),
        () => release(controller),
      );
    } catch {
      release(controller);
    }
  };

  return { trigger, cancel };
}

function initialState(): AccountAttentionState {
  return {
    accountId: "",
    role: "gast",
    weberApplicationState: "unknown",
    conversations: [],
    proposals: [],
    items: [],
  };
}

export function maskAccountAttentionForAuth(
  state: AccountAttentionState,
  status: AuthStatus,
): AccountAttentionState {
  const accountId = status.account_id;
  if (!status.authenticated || !accountId || state.accountId !== accountId) {
    return { ...initialState(), role: status.role };
  }
  if (state.role === status.role) return state;

  return {
    ...state,
    role: status.role,
    weberApplicationState:
      status.role === "gast" ? state.weberApplicationState : "unknown",
    items: projectTopBarAttention({
      conversations: state.conversations,
      proposals: state.proposals,
      accountId,
      role: status.role,
      nowMs: Date.now(),
      proposalsObservedAtMs: state.proposalsObservedAtMs,
    }),
  };
}

export function createAccountAttentionController(
  dependencies: AccountAttentionControllerDependencies,
): AccountAttentionController {
  const store = writable<AccountAttentionState>(initialState());
  let observedAccountId = "";
  let observedRole: AuthRole = "gast";
  let messageRequestRevision = 0;
  let proposalRequestRevision = 0;

  function ownsResult(accountId: string): boolean {
    const current = dependencies.getAuthStatus();
    return (
      current.authenticated &&
      current.account_id === accountId &&
      observedAccountId === accountId
    );
  }

  function project(state: AccountAttentionState): AccountAttentionState {
    return {
      ...state,
      items: projectTopBarAttention({
        conversations: state.conversations,
        proposals: state.proposals,
        accountId: state.accountId || undefined,
        role: state.role,
        nowMs: Date.now(),
        proposalsObservedAtMs: state.proposalsObservedAtMs,
      }),
    };
  }

  function reset(accountId = "", role: AuthRole = "gast"): void {
    observedAccountId = accountId;
    observedRole = role;
    messageRequestRevision += 1;
    proposalRequestRevision += 1;
    store.set({ ...initialState(), accountId, role });
  }

  function prepare(status: AuthStatus): boolean {
    const accountId = status.account_id;
    if (!status.authenticated || !accountId) {
      reset();
      return false;
    }
    if (observedAccountId !== accountId) {
      reset(accountId, status.role);
      return true;
    }
    if (observedRole !== status.role) {
      observedRole = status.role;
      store.update((state) =>
        project({
          ...state,
          role: status.role,
          weberApplicationState:
            status.role === "gast" ? state.weberApplicationState : "unknown",
        }),
      );
    }
    return true;
  }

  async function refreshMessages(
    status = dependencies.getAuthStatus(),
    signal?: AbortSignal,
  ) {
    if (!prepare(status)) return;
    const accountId = status.account_id;
    if (!accountId) return;

    const revision = ++messageRequestRevision;
    try {
      const conversations = await dependencies.listDirectConversations(signal);
      if (revision !== messageRequestRevision || !ownsResult(accountId)) return;
      store.update((state) => project({ ...state, conversations }));
    } catch {
      // Preserve the last confirmed projection. A transient failure must never
      // turn unread work into a false empty state.
    }
  }

  async function refreshProposals(
    status = dependencies.getAuthStatus(),
    signal?: AbortSignal,
  ) {
    if (!prepare(status)) return;
    const accountId = status.account_id;
    if (!accountId) return;

    const revision = ++proposalRequestRevision;
    try {
      const proposals = await dependencies.listProposals(signal);
      if (revision !== proposalRequestRevision || !ownsResult(accountId))
        return;

      const currentStatus = dependencies.getAuthStatus();
      if (!prepare(currentStatus) || currentStatus.account_id !== accountId)
        return;
      if (revision !== proposalRequestRevision || !ownsResult(accountId))
        return;

      let weberApplicationState: WeberApplicationState = "unknown";
      let acceptedApplicationNeedsAuthRefresh = false;
      if (currentStatus.role === "gast") {
        const pending = hasPendingWeberApplication(proposals, accountId);
        const accepted = hasAcceptedWeberApplication(proposals, accountId);
        weberApplicationState = pending || accepted ? "pending" : "available";
        acceptedApplicationNeedsAuthRefresh = !pending && accepted;
      }

      const proposalsObservedAtMs = Date.now();
      store.update((state) =>
        project({
          ...state,
          proposals,
          proposalsObservedAtMs,
          weberApplicationState,
        }),
      );

      if (acceptedApplicationNeedsAuthRefresh && ownsResult(accountId)) {
        // Governance can finalize a guest application before the auth endpoint
        // reflects the promoted role. Keep the non-actionable pending state
        // until the authoritative auth read confirms the transition.
        await dependencies.checkAuth({ force: true });
      }
    } catch {
      // Preserve the last confirmed projection. In particular, an initial
      // proposal failure must not look like permission to submit a new request.
    }
  }

  async function refresh(status = dependencies.getAuthStatus()) {
    if (!prepare(status)) return;
    await Promise.all([refreshMessages(status), refreshProposals(status)]);
  }

  function reproject(): void {
    store.update((state) => project(state));
  }

  return {
    subscribe: store.subscribe,
    refresh,
    refreshMessages,
    refreshProposals,
    reproject,
  };
}

const controller = createAccountAttentionController({
  getAuthStatus: () => get(authStore),
  checkAuth: (options) => authStore.checkAuth(options),
  listDirectConversations: (signal) => listDirectConversations(signal),
  listProposals: (signal) => listProposals(signal),
});

export const accountAttentionRuntime: Readable<AccountAttentionState> = derived(
  [controller, authStore],
  ([state, status]) => maskAccountAttentionForAuth(state, status),
);

export function refreshAccountAttention(): Promise<void> {
  return controller.refresh(get(authStore));
}

let retainCount = 0;
let stopRuntime: (() => void) | null = null;

function installRuntime(): () => void {
  if (typeof window === "undefined" || typeof document === "undefined") {
    return () => undefined;
  }

  // null forces the first subscription event to reconcile even when the
  // current auth state is unauthenticated. The controller store outlives the
  // map component, so skipping that first empty key could briefly expose the
  // previous account's retained attention after a client-side remount.
  let authKey: string | null = null;
  const unsubscribeAuth = authStore.subscribe((status) => {
    const nextAuthKey = status.authenticated
      ? `${status.account_id ?? ""}:${status.role}`
      : "";
    if (nextAuthKey === authKey) return;
    authKey = nextAuthKey;
    void controller.refresh(status);
  });

  let attentionSignalPrimed = false;
  const unsubscribeAttention = accountAttentionInvalidation.subscribe(() => {
    if (!attentionSignalPrimed) {
      attentionSignalPrimed = true;
      return;
    }
    void refreshAccountAttention();
  });

  // A foreground transition commonly emits visibilitychange and focus in quick
  // succession. One full refresh is enough: coalesce only while that first
  // foreground refresh is still in flight. Auth and explicit invalidation
  // refreshes intentionally bypass this coalescer and remain independent.
  const refreshFromForeground = createCoalescedForegroundRefresh(() =>
    refreshAccountAttention(),
  );
  const refreshWhenVisible = () => {
    if (document.visibilityState === "visible") refreshFromForeground();
  };
  const refreshOnFocus = refreshFromForeground;
  const refreshMessagesFromPoll = createBoundedBackgroundRefresh((signal) =>
    controller.refreshMessages(get(authStore), signal),
  );
  const messagePoll = window.setInterval(() => {
    if (document.visibilityState === "visible") {
      refreshMessagesFromPoll.trigger();
    }
  }, MESSAGE_POLL_MS);
  // Governance can change because another account acts or the server advances a
  // phase. Re-read the canonical proposal list at a modest cadence while the
  // surface is visible; this remains one list request, not client-owned truth.
  // Timer-driven reads share a bounded lifecycle: one request at a time and a
  // real AbortSignal after 10 seconds. Event-driven refreshes stay independent
  // and may still supersede this background read through the revision contract.
  const refreshProposalsFromPoll = createBoundedBackgroundRefresh((signal) =>
    controller.refreshProposals(get(authStore), signal),
  );
  const proposalPoll = window.setInterval(() => {
    if (document.visibilityState === "visible")
      refreshProposalsFromPoll.trigger();
  }, PROPOSAL_POLL_MS);
  // Deadlines can cross an attention boundary without a network event. Reproject
  // the already confirmed facts locally; this does not invent or refresh domain truth.
  const deadlineProjectionClock = window.setInterval(() => {
    if (document.visibilityState === "visible") controller.reproject();
  }, 60_000);

  document.addEventListener("visibilitychange", refreshWhenVisible);
  window.addEventListener("focus", refreshOnFocus);

  return () => {
    unsubscribeAuth();
    unsubscribeAttention();
    window.clearInterval(messagePoll);
    window.clearInterval(proposalPoll);
    window.clearInterval(deadlineProjectionClock);
    refreshMessagesFromPoll.cancel();
    refreshProposalsFromPoll.cancel();
    document.removeEventListener("visibilitychange", refreshWhenVisible);
    window.removeEventListener("focus", refreshOnFocus);
  };
}

export function retainAccountAttentionRuntime(): () => void {
  retainCount += 1;
  if (!stopRuntime) stopRuntime = installRuntime();
  let released = false;
  return () => {
    if (released) return;
    released = true;
    retainCount = Math.max(0, retainCount - 1);
    if (retainCount === 0 && stopRuntime) {
      stopRuntime();
      stopRuntime = null;
    }
  };
}
