import type { MapEntityGarnrolle, MapEntityViewModel } from "./types";
import { hasRenderableMapPosition } from "./coordinates";

/**
 * A household is a presentation group, not a new domain entity. Only an
 * explicitly exact PUBLIC position may participate: radius positions can
 * coincide without belonging to the same building.
 */
export type HouseholdStack = {
  key: string;
  members: MapEntityGarnrolle[];
};

export type HouseholdIndex = {
  groups: Map<string, HouseholdStack>;
  byAccountId: Map<string, HouseholdStack>;
};

export function indexHouseholdStacks(
  entities: readonly MapEntityViewModel[],
): HouseholdIndex {
  const candidates = new Map<string, MapEntityGarnrolle[]>();
  for (const entity of entities) {
    if (
      entity.type !== "garnrolle" ||
      entity.map_state !== "exact" ||
      !hasRenderableMapPosition(entity)
    ) {
      continue;
    }
    // Do not round coordinates: two nearby buildings are not one household.
    const key = `${entity.lat}:${entity.lon}`;
    const members = candidates.get(key) ?? [];
    members.push(entity);
    candidates.set(key, members);
  }
  const groups = new Map<string, HouseholdStack>();
  const byAccountId = new Map<string, HouseholdStack>();
  for (const [key, members] of candidates) {
    if (members.length < 2) continue;
    const stack = { key, members };
    groups.set(key, stack);
    for (const member of members) byAccountId.set(member.id, stack);
  }
  return { groups, byAccountId };
}

/** Grid of independent, reachable hit targets, still anchored to one address. */
export function householdMemberOffset(
  index: number,
  total: number,
  viewportWidth: number,
): { x: number; y: number } {
  const count = Math.max(1, Math.trunc(total));
  const width =
    Number.isFinite(viewportWidth) && viewportWidth > 0 ? viewportWidth : 320;
  const columns = Math.min(
    4,
    count,
    Math.max(1, Math.floor((width - 32) / 68)),
  );
  const rows = Math.ceil(count / columns);
  const row = Math.floor(index / columns);
  const column = index % columns;
  const inRow = Math.min(columns, count - row * columns);
  return {
    x: Math.round((column - (inRow - 1) / 2) * 68),
    y: Math.round((row - (rows - 1) / 2) * 72),
  };
}
