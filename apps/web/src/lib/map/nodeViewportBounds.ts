export type NodeViewportBounds = {
  west: number;
  south: number;
  east: number;
  north: number;
};

export class NodeViewportError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "NodeViewportError";
  }
}

function finite(value: number, label: string): number {
  if (!Number.isFinite(value)) {
    throw new NodeViewportError(`${label} must be finite`);
  }
  return value;
}

function normalizeLongitude(value: number): number {
  const normalized = ((((value + 180) % 360) + 360) % 360) - 180;
  return Object.is(normalized, -0) ? 0 : normalized;
}

function coordinate(value: number): string {
  const rounded = Number(value.toFixed(6));
  return String(Object.is(rounded, -0) ? 0 : rounded);
}

/**
 * Convert one possibly unwrapped MapLibre viewport into API bboxes.
 * A viewport crossing the antimeridian becomes two ordinary boxes because the
 * existing node API intentionally accepts rectangular min/max bboxes only.
 */
export function nodeViewportBboxes(bounds: NodeViewportBounds): string[] {
  const westRaw = finite(bounds.west, "bounds.west");
  let eastRaw = finite(bounds.east, "bounds.east");
  const southRaw = finite(bounds.south, "bounds.south");
  const northRaw = finite(bounds.north, "bounds.north");
  if (southRaw > northRaw) {
    throw new NodeViewportError("bounds.south must not exceed bounds.north");
  }
  const south = Math.max(-90, southRaw);
  const north = Math.min(90, northRaw);
  if (south > north) {
    throw new NodeViewportError(
      "viewport is outside the supported latitude range",
    );
  }
  if (
    westRaw >= -180 &&
    westRaw <= 180 &&
    eastRaw >= -180 &&
    eastRaw <= 180 &&
    westRaw <= eastRaw
  ) {
    return [
      `${coordinate(westRaw)},${coordinate(south)},${coordinate(eastRaw)},${coordinate(north)}`,
    ];
  }
  while (eastRaw < westRaw) eastRaw += 360;
  const span = eastRaw - westRaw;
  if (span >= 360) {
    return [`-180,${coordinate(south)},180,${coordinate(north)}`];
  }

  const west = normalizeLongitude(westRaw);
  const east = normalizeLongitude(eastRaw);
  if (west <= east) {
    return [
      `${coordinate(west)},${coordinate(south)},${coordinate(east)},${coordinate(north)}`,
    ];
  }
  return [
    `${coordinate(west)},${coordinate(south)},180,${coordinate(north)}`,
    `-180,${coordinate(south)},${coordinate(east)},${coordinate(north)}`,
  ];
}

type CanonicalViewportBbox = {
  west: number;
  south: number;
  east: number;
  north: number;
};

function canonicalViewportBboxes(
  bounds: NodeViewportBounds,
): CanonicalViewportBbox[] {
  return nodeViewportBboxes(bounds).map((bbox) => {
    const [west, south, east, north] = bbox.split(",").map(Number);
    return { west, south, east, north };
  });
}

/**
 * Return true when a previously complete viewport response fully covers the
 * requested viewport. Antimeridian/full-world cases reuse the same canonical
 * split representation as the API request path, so containment cannot diverge
 * from the actual BBOX semantics.
 */
export function nodeViewportContains(
  coverage: NodeViewportBounds,
  requested: NodeViewportBounds,
): boolean {
  const coverageBboxes = canonicalViewportBboxes(coverage);
  const requestedBboxes = canonicalViewportBboxes(requested);
  return requestedBboxes.every((candidate) =>
    coverageBboxes.some(
      (available) =>
        available.west <= candidate.west &&
        available.south <= candidate.south &&
        available.east >= candidate.east &&
        available.north >= candidate.north,
    ),
  );
}
