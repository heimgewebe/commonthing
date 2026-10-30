import { isValidMapCoordinate } from "$lib/map/coordinates";
import type { Location } from "$lib/map/types";

/** Blank pairs remove location; incomplete or invalid pairs reject the edit. */
export function parseOptionalNodeLocation(
  latitude: string,
  longitude: string,
): Location | null | undefined {
  if (!latitude.trim() && !longitude.trim()) return null;
  if (!latitude.trim() || !longitude.trim()) return undefined;
  const lat = Number(latitude);
  const lon = Number(longitude);
  return isValidMapCoordinate(lon, lat) ? { lat, lon } : undefined;
}
