import { writable } from "svelte/store";

export const isSearchOpen = writable<boolean>(false);
export const searchQuery = writable<string>("");

// One explicit cross-view handoff, kept in memory only. Never put owner-only
// results or search terms in the address bar, history state or local storage.
type DiscoveryView = "commons" | "map";
let discoveryTransfer: { target: DiscoveryView; query: string } | null = null;

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
    event.button !== 0 ||
    event.altKey ||
    event.ctrlKey ||
    event.metaKey ||
    event.shiftKey
  )
    return;

  const term = query.trim();
  discoveryTransfer = term ? { target, query: term } : null;
}

export function takeDiscoverySearch(target: DiscoveryView): string | null {
  if (discoveryTransfer?.target !== target) return null;
  const query = discoveryTransfer.query;
  discoveryTransfer = null;
  return query;
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
