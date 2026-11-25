import { writable } from "svelte/store";
import { browser } from "$app/environment";
import type { NavigationType } from "@sveltejs/kit";
import { authStore } from "$lib/auth/store";

export const isSearchOpen = writable<boolean>(false);
export const searchQuery = writable<string>("");

// Explicit cross-view handoff and bounded per-entry history, in memory only.
// History state may carry an opaque ID, never a query or owner-only results.
type DiscoveryView = "commons" | "map";
let discoveryTransfer: [DiscoveryView, string] | undefined;

export function stageDiscoverySearch(
  event: Pick<
    MouseEvent,
    | "button"
    | "altKey"
    | "ctrlKey"
    | "metaKey"
    | "shiftKey"
    | "defaultPrevented"
  >,
  target: DiscoveryView,
  query: string,
) {
  // Native new-tab/window and cancelled activations must not alter this tab.
  if (
    event.defaultPrevented ||
    event.button ||
    event.altKey ||
    event.ctrlKey ||
    event.metaKey ||
    event.shiftKey
  )
    return;

  discoveryTransfer = [target, query.trim()];
}

export function takeDiscoverySearch(target: DiscoveryView): string | null {
  if (discoveryTransfer?.[0] !== target) return null;
  const query = discoveryTransfer[1];
  discoveryTransfer = undefined;
  return query || null;
}

const MAX_DISCOVERY_HISTORY = 64;
type DiscoveryEntry = { view: DiscoveryView; query: string | null };
const discoveryHistory = new Map<string, DiscoveryEntry>();
let historyAccount: string | null | undefined;

// Do not resurrect a prior account's searches, even after switching back.
// A first authoritative observation binds the newly loaded runtime.
export function bindDiscoveryHistoryAccount(account: string | null) {
  if (historyAccount !== undefined && historyAccount !== account) {
    discoveryHistory.clear();
    discoveryTransfer = undefined;
  }
  historyAccount = account;
}
if (browser) {
  authStore.subscribe((status) => {
    if (status.state === "authenticated" && status.account_id)
      bindDiscoveryHistoryAccount(status.account_id);
    else if (status.state === "unauthenticated")
      bindDiscoveryHistoryAccount(null);
  });
}

export function enterDiscoveryHistory(
  view: DiscoveryView,
  historyId: string | undefined,
  navigationType: NavigationType,
): { id: string; query: string | null } {
  const remembered =
    navigationType === "popstate" && historyId
      ? discoveryHistory.get(historyId)
      : undefined;
  const transfer = takeDiscoverySearch(view);
  // A pending handoff never survives an unrelated or history navigation.
  discoveryTransfer = undefined;
  if (historyId && remembered?.view === view) {
    return { id: historyId, query: remembered.query };
  }
  const query =
    navigationType === "popstate" || navigationType === "enter"
      ? null
      : transfer;
  const id = crypto.randomUUID();
  discoveryHistory.set(id, { view, query });
  if (discoveryHistory.size > MAX_DISCOVERY_HISTORY) {
    const oldest = discoveryHistory.keys().next().value;
    if (oldest !== undefined) discoveryHistory.delete(oldest);
  }
  return { id, query };
}

export function rememberDiscoveryHistory(
  id: string | undefined,
  view: DiscoveryView,
  query: string | null,
) {
  if (!id || discoveryHistory.get(id)?.view !== view) return;
  // Updating only existing entries prevents an old page from repopulating
  // history after an account change or eviction.
  discoveryHistory.delete(id);
  discoveryHistory.set(id, { view, query });
}

export function toggleSearch() {
  isSearchOpen.update((v) => {
    if (v) {
      searchQuery.set("");
      return false;
    }
    return true;
  });
}

/**
 * Opens the search overlay unconditionally. Used by URL deep-link addressing,
 * which must never toggle (a toggle could close an already-open overlay).
 */
export function openSearch() {
  isSearchOpen.set(true);
}

export function closeSearch() {
  isSearchOpen.set(false);
  searchQuery.set("");
}
