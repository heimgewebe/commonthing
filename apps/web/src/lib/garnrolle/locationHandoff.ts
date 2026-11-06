import { browser } from "$app/environment";
import { get } from "svelte/store";
import { authStore } from "$lib/auth/store";
import type { Location } from "$lib/map/types";

/**
 * In-memory fallback for the existing settings -> map -> settings flow.
 * A blocked sessionStorage write must not disable the "Punkt ändern" action.
 *
 * This is intentionally tab/runtime-local: never put private coordinates in
 * URLs, persistent storage, public map data or another account's state.
 * The server-owned account PATCH remains the only operation that saves them.
 */
type PendingHandoff = {
  accountId: string;
  draftJson: string;
  returnLocation?: Location;
};

let pending: PendingHandoff | null = null;

function authorized(accountId: string): boolean {
  if (!browser || !accountId) return false;
  const auth = get(authStore);
  return auth.authenticated && auth.account_id === accountId;
}

authStore.subscribe((auth) => {
  if (!browser) return;
  if (
    !auth.authenticated ||
    !auth.account_id ||
    (pending !== null && pending.accountId !== auth.account_id)
  ) {
    pending = null;
  }
});

export function rememberGarnrolleDraft(
  accountId: string,
  draftJson: string,
): boolean {
  if (!authorized(accountId)) return false;
  pending = { accountId, draftJson };
  return true;
}

export function readGarnrolleDraft(accountId: string): string | null {
  return authorized(accountId) && pending?.accountId === accountId
    ? pending.draftJson
    : null;
}

export function rememberGarnrolleLocation(
  accountId: string,
  location: Location,
): boolean {
  if (
    !authorized(accountId) ||
    pending?.accountId !== accountId ||
    !Number.isFinite(location.lat) ||
    !Number.isFinite(location.lon) ||
    location.lat < -90 ||
    location.lat > 90 ||
    location.lon < -180 ||
    location.lon > 180
  ) {
    return false;
  }
  pending.returnLocation = { lat: location.lat, lon: location.lon };
  return true;
}

export function takeGarnrolleLocation(accountId: string): Location | null {
  if (!authorized(accountId) || pending?.accountId !== accountId) return null;
  const location = pending.returnLocation ?? null;
  if (pending.returnLocation) delete pending.returnLocation;
  return location;
}

export function clearGarnrolleHandoff(accountId: string): void {
  if (pending?.accountId === accountId) pending = null;
}
