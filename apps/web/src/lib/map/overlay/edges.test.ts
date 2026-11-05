import { describe, expect, it } from "vitest";
import {
  FADEN_LIFETIME_MS,
  normalizeEdgeLifecycle,
} from "$lib/map/edgeLifecycle";
import {
  EDGE_CURVE_MAX_HANDLE_FRACTION,
  EDGE_CURVE_MAX_HANDLE_M,
  EDGE_CURVE_MAX_SAMPLES,
  EDGE_CURVE_MERCATOR_RADIUS_M,
  EDGE_CURVE_MIN_SAMPLES,
  EDGE_CURVE_MIN_VISIBLE_SEGMENT_M,
  EDGE_CURVE_TANGENT_ANGLE_TOLERANCE_DEG,
  EDGE_CURVE_TARGET_APPROACH_CONE_DEG,
  EDGE_THREAD_LAYER_IDS,
  EDGE_THREAD_VARIANTS,
  EDGE_VISUAL_STYLE,
  THREAD_CURVE_PROFILES,
  buildEdgeFeatures,
  buildEdgeLayerSpecifications,
  buildEndpointIndex,
  buildProgressClippedThemeSegments,
  buildThemedLineSegments,
  buildThreadPathState,
  clipThreadPathByProgress,
  degreeSpacePolylineArcState,
  getThreadPathBuildSerialForTests,
  hashUnit,
  hasCompleteEdgeThreadStyle,
  pointAtArcProgress,
  projectedChord,
  projectedSampleToLngLat,
  projectLngLatToMercator,
  resetThreadPathBuildSerialForTests,
  sampleThreadCurve,
  shortestLongitudeDelta,
  threadCorridorKey,
  threadCurveAdaptiveBreakpoints,
  threadCurveControlPoints,
  threadCurveControlPointsProjected,
  threadCurveProfile,
  threadCurveSampleCount,
  threadTargetApproachVector,
  threadTargetLocalCorridorAxis,
  unprojectMercatorToLngLat,
  updateEdges,
} from "$lib/map/overlay/edges";
import { LAYERS } from "$lib/map/overlay/layers";
import type { Edge, MapEntityViewModel } from "$lib/map/types";
import { CONVERSATION_THREAD_WIDTH_PX } from "$lib/map/weaveVisualTokens";

const createdAt = Date.parse("2026-07-17T10:00:00Z");
const points = [
  { id: "source", lat: 53.5, lon: 9.9 },
  { id: "target", lat: 53.6, lon: 10.0 },
] as MapEntityViewModel[];
const rawEdge: Edge = {
  id: "edge-1",
  source_id: "source",
  target_id: "target",
  edge_kind: "reference",
  created_at: new Date(createdAt).toISOString(),
  expires_at: new Date(createdAt + FADEN_LIFETIME_MS).toISOString(),
};
const edge = normalizeEdgeLifecycle(rawEdge);

describe("buildEdgeFeatures", () => {
  it("binds continuous opacity to the feature", () => {
    const features = buildEdgeFeatures(
      [edge],
      points,
      true,
      createdAt + FADEN_LIFETIME_MS / 2,
    );
    expect(features).toHaveLength(1);
    expect(features[0].properties?.opacity).toBe(0.5);
    expect(features[0].properties).not.toHaveProperty("themeColor");
  });

  it("indexes Faden endpoints by id and faden_endpoint_id for center attachment", () => {
    const centerEndpointId = "22222222-2222-5222-8222-222222222222";
    const center = {
      type: "webgemeindezentrum",
      id: "webgemeindezentrum-hammer-park",
      faden_endpoint_id: centerEndpointId,
      lat: 53.5585,
      lon: 10.058,
    } as MapEntityViewModel;
    const index = buildEndpointIndex([points[0], center]);
    expect(index.get("source")?.lon).toBe(9.9);
    expect(index.get(centerEndpointId)?.id).toBe(
      "webgemeindezentrum-hammer-park",
    );
    expect(index.get("webgemeindezentrum-hammer-park")?.lat).toBe(53.5585);
  });

  it("resolves a Webgemeindezentrum through its strict Faden UUID alias", () => {
    const centerEndpointId = "22222222-2222-5222-8222-222222222222";
    const center = {
      type: "webgemeindezentrum",
      id: "webgemeindezentrum-hammer-park",
      faden_endpoint_id: centerEndpointId,
      conversation_id: "33333333-3333-5333-8333-333333333333",
      title: "Webgemeindezentrum Hammer Park",
      lat: 53.5585,
      lon: 10.058,
      summary: "Treffpunkt",
      tags: [],
      created_at: new Date(createdAt).toISOString(),
      updated_at: new Date(createdAt).toISOString(),
      location_state: "desired",
      location_state_label: "Gewünschter Treffort",
      location_label: "Hammer Park",
      meeting_note: "Treffpunkt",
      access_note: "Noch zu bestätigen",
      ortsweberei: {
        id: "ortsweberei-hamm",
        slug: "hamm",
        name: "Ortsweberei Hamm",
        gewebezelle_id: "hamm.weltgewebe.net",
      },
    } as MapEntityViewModel;
    const centerEdge = normalizeEdgeLifecycle({
      ...rawEdge,
      id: "edge-center",
      target_id: centerEndpointId,
    });

    const features = buildEdgeFeatures(
      [centerEdge],
      [points[0], center],
      true,
      createdAt,
    );

    expect(features).toHaveLength(1);
    const coords = features[0].geometry.coordinates;
    expect(coords[0]).toEqual([9.9, 53.5]);
    expect(coords.at(-1)).toEqual([10.058, 53.5585]);
    expect(coords.length).toBeGreaterThanOrEqual(2);
    expect(coords.length).toBeLessThanOrEqual(EDGE_CURVE_MAX_SAMPLES);
  });

  it("projects the canonical Faden type and subject without vote content", () => {
    const typed = normalizeEdgeLifecycle({
      ...rawEdge,
      faden_type: "vote",
      faden_subject_id: "11111111-1111-5111-8111-111111111111",
    });
    const themedPoints = [
      points[0],
      {
        ...points[1],
        type: "node",
        weave: {
          zoneOrder: ["knotting", "conversation", "proposal", "vote"],
          themeSegments: [
            {
              id: "natur",
              label: "Natur",
              color: "#5f7a55",
              arm: "northwest",
            },
          ],
          xCoreSegments: [
            {
              arm: "northwest",
              themeId: "natur",
              label: "Natur",
              color: "#5f7a55",
            },
            {
              arm: "northeast",
              themeId: "natur",
              label: "Natur",
              color: "#5f7a55",
            },
            {
              arm: "southeast",
              themeId: "natur",
              label: "Natur",
              color: "#5f7a55",
            },
            {
              arm: "southwest",
              themeId: "natur",
              label: "Natur",
              color: "#5f7a55",
            },
          ],
          armOverlays: [],
          primaryThemeColor: "#5f7a55",
          coreDensity: 0.5,
          conversationRingScale: 0,
          knottingThreadCount: 0,
          conversationThreadCount: 0,
          conversationOpacity: 0,
          proposalArcs: [],
          proposalCount: 0,
          proposalOverflowCount: 0,
          voteThreadCount: 0,
          totalActiveThreadCount: 0,
        },
      },
    ] as MapEntityViewModel[];
    const features = buildEdgeFeatures([typed], themedPoints, true, createdAt);
    expect(features[0].properties).toMatchObject({
      fadenType: "vote",
      fadenSubjectId: "11111111-1111-5111-8111-111111111111",
      themeColor: "#5f7a55",
    });
    expect(features[0].properties).not.toHaveProperty("choice");
  });

  it("omits expired, hidden, and unresolved edges", () => {
    expect(
      buildEdgeFeatures([edge], points, true, createdAt + FADEN_LIFETIME_MS),
    ).toEqual([]);
    expect(buildEdgeFeatures([edge], points, false, createdAt)).toEqual([]);
    expect(
      buildEdgeFeatures([edge], points.slice(0, 1), true, createdAt),
    ).toEqual([]);
  });

  it("defensively drops edges with non-finite or out-of-range endpoints", () => {
    const invalidPoints = [
      { id: "source", lat: Number.NaN, lon: 9.9 },
      { id: "target", lat: 53.6, lon: 10.0 },
    ] as MapEntityViewModel[];
    const outOfRange = [
      { id: "source", lat: 53.5, lon: 9.9 },
      { id: "target", lat: 91, lon: 10.0 },
    ] as MapEntityViewModel[];

    expect(buildEdgeFeatures([edge], invalidPoints, true, createdAt)).toEqual(
      [],
    );
    expect(buildEdgeFeatures([edge], outOfRange, true, createdAt)).toEqual([]);
  });
});

describe("updateEdges source readiness", () => {
  it("installs the empty GeoJSON source and every canonical layer", () => {
    const sources = new Map<string, { data: unknown }>();
    const layers = new Map<string, unknown>();
    const map = {
      getSource: (id: string) => sources.get(id),
      addSource: (id: string, spec: { data: unknown }) => {
        sources.set(id, { data: spec.data });
      },
      getLayer: (id: string) => layers.get(id),
      addLayer: (layer: { id: string }) => {
        layers.set(layer.id, layer);
      },
      getStyle: () => ({ layers: [{ id: "labels", type: "symbol" }] }),
    };

    updateEdges(map as never, [], points, true, createdAt);

    expect(sources.has(LAYERS.EDGES_SOURCE)).toBe(true);
    expect(
      (sources.get(LAYERS.EDGES_SOURCE)?.data as GeoJSON.FeatureCollection)
        .features,
    ).toEqual([]);
    for (const layerId of EDGE_THREAD_LAYER_IDS) {
      expect(layers.has(layerId)).toBe(true);
    }
    expect(hasCompleteEdgeThreadStyle(map)).toBe(true);
  });
});

describe("Garnrolle location-picker thread fading", () => {
  it("dims thread layers only while picking, including after a style reload", () => {
    type Layer = { id: string; type: string; paint?: Record<string, unknown> };
    const sources = new Map<string, { setData(data: unknown): void }>();
    const layers = new Map<string, Layer>();
    let setPaintCount = 0;
    const map = {
      getSource: (id: string) => sources.get(id),
      addSource: (id: string) => sources.set(id, { setData() {} }),
      getLayer: (id: string) => layers.get(id),
      addLayer: (layer: Layer) => {
        layers.set(layer.id, structuredClone(layer));
      },
      getStyle: () => ({ layers: [{ id: "labels", type: "symbol" }] }),
      getPaintProperty: (id: string, property: string) =>
        layers.get(id)?.paint?.[property],
      setPaintProperty: (id: string, property: string, opacity: unknown) => {
        const layer = layers.get(id);
        if (!layer) throw Error("missing layer");
        layer.paint ??= {};
        layer.paint[property] = opacity;
        setPaintCount += 1;
      },
    };
    const original = buildEdgeLayerSpecifications();
    updateEdges(map as never, [], points, true, createdAt);
    expect(setPaintCount).toBe(0);
    updateEdges(map as never, [], points, true, createdAt, true);
    expect(setPaintCount).toBe(EDGE_THREAD_LAYER_IDS.length);
    for (const layer of original) {
      expect(map.getPaintProperty(layer.id, "line-opacity")).toEqual([
        "*",
        layer.paint?.["line-opacity"],
        0.22,
      ]);
    }
    updateEdges(map as never, [], points, true, createdAt, true);
    expect(setPaintCount).toBe(EDGE_THREAD_LAYER_IDS.length);
    updateEdges(map as never, [], points, true, createdAt, false);
    for (const layer of original) {
      expect(map.getPaintProperty(layer.id, "line-opacity")).toEqual(
        layer.paint?.["line-opacity"],
      );
    }
    layers.clear();
    updateEdges(map as never, [], points, true, createdAt, true);
    expect(map.getPaintProperty(original[0].id, "line-opacity")).toEqual([
      "*",
      original[0].paint?.["line-opacity"],
      0.22,
    ]);
    updateEdges(map as never, [], points, true, createdAt, false);
    expect(map.getPaintProperty(original[0].id, "line-opacity")).toEqual(
      original[0].paint?.["line-opacity"],
    );
  });
});

describe("hasCompleteEdgeThreadStyle", () => {
  function probe(
    layerIds: readonly string[],
    sourceId: string = LAYERS.EDGES_SOURCE,
  ) {
    const layers = new Set(layerIds);
    return {
      getSource: (id: string) => (id === sourceId ? {} : undefined),
      getLayer: (id: string) => (layers.has(id) ? {} : undefined),
    };
  }

  it("derives the canonical list from the layer specifications themselves", () => {
    expect(EDGE_THREAD_LAYER_IDS).toEqual(
      buildEdgeLayerSpecifications().map((specification) => specification.id),
    );
    // Shadow + body + highlight for the Faden-out lane and the four typed thread kinds.
    expect(EDGE_THREAD_LAYER_IDS).toHaveLength(15);
  });

  it("accepts the complete set of source and thread layers", () => {
    expect(hasCompleteEdgeThreadStyle(probe(EDGE_THREAD_LAYER_IDS))).toBe(true);
  });

  it("rejects a style that is missing the last typed layer", () => {
    expect(
      hasCompleteEdgeThreadStyle(probe(EDGE_THREAD_LAYER_IDS.slice(0, -1))),
    ).toBe(false);
  });

  it("rejects the old partial check of source plus two old generic layers", () => {
    expect(
      hasCompleteEdgeThreadStyle(
        probe([
          LAYERS.EDGES_SHADOW_LAYER,
          LAYERS.EDGES_LAYER,
          LAYERS.EDGES_HIGHLIGHT_LAYER,
        ]),
      ),
    ).toBe(false);
  });

  it("keeps shadow/body/highlight triples with continuous body and highlight rhythm", () => {
    const specs = buildEdgeLayerSpecifications();
    expect(specs).toHaveLength(15);
    for (let index = 0; index < specs.length; index += 3) {
      const shadow = specs[index];
      const body = specs[index + 1];
      const highlight = specs[index + 2];
      expect(shadow.id).toContain("shadow");
      expect(highlight.id).toContain("highlight");
      expect(shadow.paint?.["line-width"]).toBeGreaterThan(
        Number(body.paint?.["line-width"]),
      );
      expect(Number(highlight.paint?.["line-width"])).toBeLessThan(
        Number(body.paint?.["line-width"]),
      );
      // Highlight always carries the braid/fiber dash rhythm.
      expect(highlight.paint?.["line-dasharray"]).toBeDefined();
      const variant = EDGE_THREAD_VARIANTS[index / 3];
      if (variant.shadowContinuous) {
        expect(shadow.paint?.["line-dasharray"]).toBeUndefined();
      }
      if (variant.bodyContinuous) {
        expect(body.paint?.["line-dasharray"]).toBeUndefined();
      } else {
        // Vote stays stitch-like on the body with continuous shadow link.
        expect(body.paint?.["line-dasharray"]).toEqual([...variant.dashArray]);
        expect(shadow.paint?.["line-dasharray"]).toBeUndefined();
      }
    }
  });

  it("rejects a missing edge source and an absent map", () => {
    expect(
      hasCompleteEdgeThreadStyle(probe(EDGE_THREAD_LAYER_IDS, "other-source")),
    ).toBe(false);
    expect(hasCompleteEdgeThreadStyle(null)).toBe(false);
  });
});

describe("natural thread curves", () => {
  const source: [number, number] = [9.9, 53.5];
  const target: [number, number] = [10.1, 53.65];

  // Arc-length midpoint, not the array's middle index: curvature-adaptive
  // sampling concentrates points where the curve bends, so the *array*
  // midpoint no longer sits at arc progress 0.5 in general (e.g. a nearly
  // straight span stays at the 4-point floor, whose middle index lands at
  // t≈0.667). `pointAtArcProgress` finds the true half-arc-length point
  // regardless of how the underlying samples are distributed.
  function projectedMidDeflection(
    path: readonly [number, number][],
    from: [number, number],
    to: [number, number],
  ): number {
    const mid = pointAtArcProgress(path, 0.5);
    const { sourceXY, targetXY } = projectedChord(from, to);
    const midXY = projectLngLatToMercator(mid[0], mid[1]);
    const chordMid: [number, number] = [
      (sourceXY[0] + targetXY[0]) / 2,
      (sourceXY[1] + targetXY[1]) / 2,
    ];
    return Math.hypot(midXY[0] - chordMid[0], midXY[1] - chordMid[1]);
  }

  it("keeps exact endpoints on every sampled curve", () => {
    for (const fadenType of [
      "knotting",
      "conversation",
      "proposal",
      "vote",
      "out",
    ] as const) {
      const path = sampleThreadCurve(source, target, {
        fadenType,
        threadId: `edge-${fadenType}`,
        subjectId: fadenType === "vote" ? "subject-1" : null,
      });
      expect(path[0]).toEqual(source);
      expect(path.at(-1)).toEqual(target);
      for (const point of path) {
        expect(Number.isFinite(point[0])).toBe(true);
        expect(Number.isFinite(point[1])).toBe(true);
      }
    }
  });

  it("is deterministic for the same identity", () => {
    const opts = {
      fadenType: "conversation",
      threadId: "edge-stable",
      subjectId: null,
    };
    const a = sampleThreadCurve(source, target, opts);
    const b = sampleThreadCurve(source, target, opts);
    expect(a).toEqual(b);
    const c = threadCurveControlPoints(source, target, opts);
    const d = threadCurveControlPoints(source, target, opts);
    expect(c).toEqual(d);
  });

  it("enforces real lateral deflection for soft conversation profiles", () => {
    const path = sampleThreadCurve(source, target, {
      fadenType: "conversation",
      threadId: "edge-soft",
    });
    expect(path.length).toBeGreaterThanOrEqual(4);
    expect(path.length).toBeLessThanOrEqual(EDGE_CURVE_MAX_SAMPLES);
    const deflection = projectedMidDeflection(path, source, target);
    // Soft profile must leave the projected chord by a meaningful margin.
    expect(deflection).toBeGreaterThan(80);
    const again = threadCurveControlPointsProjected(source, target, {
      fadenType: "conversation",
      threadId: "edge-soft",
    });
    const once = threadCurveControlPointsProjected(source, target, {
      fadenType: "conversation",
      threadId: "edge-soft",
    });
    expect(again.p1[0]).toBeCloseTo(once.p1[0], 10);
    expect(again.p1[1]).toBeCloseTo(once.p1[1], 10);
  });

  it("keeps short paths nearly straight", () => {
    const nearTarget: [number, number] = [9.901, 53.5005];
    const shortPath = sampleThreadCurve(source, nearTarget, {
      fadenType: "conversation",
      threadId: "edge-short",
    });
    const longPath = sampleThreadCurve(source, target, {
      fadenType: "conversation",
      threadId: "edge-short",
    });
    const shortDeflect = projectedMidDeflection(shortPath, source, nearTarget);
    const longDeflect = projectedMidDeflection(longPath, source, target);
    expect(shortDeflect).toBeLessThan(longDeflect * 0.15);
    expect(shortDeflect).toBeLessThan(25);
  });

  it("keeps exactly the four canonical Fadenarten", () => {
    expect(Object.keys(THREAD_CURVE_PROFILES).sort()).toEqual([
      "conversation",
      "knotting",
      "proposal",
      "vote",
    ]);
    expect("legacy" in THREAD_CURVE_PROFILES).toBe(false);
    expect("out" in THREAD_CURVE_PROFILES).toBe(false);
    expect(threadCurveProfile("toString")).toBe(threadCurveProfile("out"));
  });

  it("applies distinct tension profiles per Fadenart", () => {
    /**
     * Lateral offset of the projected Bezier at t=0.5 from the projected chord.
     * Private (no subject): mid-arc profiles must differ by type tension.
     * Subject-bound multi-source approach is covered separately.
     */
    const midDeflection = (fadenType: string, subjectId: string | null) => {
      const { p0, p1, p2, p3, length } = threadCurveControlPointsProjected(
        source,
        target,
        {
          fadenType,
          threadId: `edge-profile-${fadenType}`,
          subjectId,
        },
      );
      // Cubic Bezier at t=0.5: 1/8*(p0+p3)+3/8*(p1+p2)
      const midX = 0.125 * (p0[0] + p3[0]) + 0.375 * (p1[0] + p2[0]);
      const midY = 0.125 * (p0[1] + p3[1]) + 0.375 * (p1[1] + p2[1]);
      const chordMidX = (p0[0] + p3[0]) / 2;
      const chordMidY = (p0[1] + p3[1]) / 2;
      expect(length).toBeGreaterThan(0);
      return Math.hypot(midX - chordMidX, midY - chordMidY);
    };
    const knot = midDeflection("knotting", null);
    const talk = midDeflection("conversation", null);
    const proposal = midDeflection("proposal", null);
    const vote = midDeflection("vote", null);
    // Knüpfung ist am geradesten; Gespräch schwingt am stärksten.
    // Antrag und Stimme bleiben verwandt, aber nicht deckungsgleich.
    expect(talk).toBeGreaterThan(knot);
    expect(talk).toBeGreaterThan(proposal);
    expect(talk).toBeGreaterThan(vote);
    // Proposal mid arc must clearly exceed vote's near-linear stitch — not a
    // weak half-margin that would still pass if the profiles collapsed.
    expect(proposal).toBeGreaterThan(vote);
    expect(proposal / vote).toBeGreaterThan(1.08);
    expect(threadCurveProfile("proposal").tension).toBeLessThan(
      threadCurveProfile("vote").tension,
    );
    expect(threadCurveProfile("knotting").tension).toBeGreaterThan(
      threadCurveProfile("conversation").tension,
    );
    expect(threadCurveProfile("vote").maxBulgeFraction).toBeLessThan(
      threadCurveProfile("proposal").maxBulgeFraction,
    );
    // Subject-bound: mid deflection is not required to rank like private
    // profiles (shared target axis dominates); multi-source tests lock approach.
    expect(EDGE_VISUAL_STYLE.byType.conversation.width).toBe(
      CONVERSATION_THREAD_WIDTH_PX,
    );
    expect(EDGE_VISUAL_STYLE.byType.knotting.width).toBeGreaterThan(
      EDGE_VISUAL_STYLE.byType.conversation.width,
    );
    expect(EDGE_VISUAL_STYLE.byType.knotting.width).toBeGreaterThan(
      EDGE_VISUAL_STYLE.byType.proposal.width,
    );
    expect(EDGE_VISUAL_STYLE.byType.proposal.width).toBeGreaterThan(
      EDGE_VISUAL_STYLE.byType.vote.width,
    );
    expect(EDGE_VISUAL_STYLE.byType.vote.width).toBeGreaterThan(
      EDGE_VISUAL_STYLE.byType.conversation.width,
    );
    expect(threadCurveProfile("knotting").maxBulgeFraction).toBeLessThan(
      threadCurveProfile("vote").maxBulgeFraction,
    );
  });

  it("keeps the production curve budget at no more than 24 points", () => {
    expect(EDGE_CURVE_MAX_SAMPLES).toBe(24);
  });

  it("bounds sample count by EDGE_CURVE_MAX_SAMPLES", () => {
    const far: [number, number] = [12, 55];
    const count = threadCurveSampleCount(source, far, {
      fadenType: "conversation",
      threadId: "edge-long",
    });
    expect(count).toBeLessThanOrEqual(EDGE_CURVE_MAX_SAMPLES);
    expect(
      sampleThreadCurve(source, far, {
        fadenType: "conversation",
        threadId: "edge-long",
      }).length,
    ).toBe(count);
  });

  it("segments multi-colour strands by curve arc length with stable endpoints", () => {
    const colors = ["#111111", "#222222"];
    const segments = buildThemedLineSegments(source, target, colors, {
      fadenType: "proposal",
      threadId: "edge-theme",
    });
    expect(segments).toHaveLength(4);
    expect(segments[0].coordinates[0]).toEqual(source);
    expect(segments.at(-1)?.coordinates.at(-1)).toEqual(target);
    for (const segment of segments) {
      expect(segment.coordinates.length).toBeGreaterThanOrEqual(2);
      expect(segment.coordinates.length).toBeLessThanOrEqual(
        EDGE_CURVE_MAX_SAMPLES,
      );
    }
  });

  it("shares static and motion curve geometry for the same identity", () => {
    const opts = {
      fadenType: "proposal",
      threadId: "edge-shared",
      subjectId: "subject-42",
    };
    const staticPath = sampleThreadCurve(source, target, opts);
    const pathState = buildThreadPathState(source, target, ["#aaaaaa"], opts);
    expect(pathState.samples).toEqual(staticPath);
    const full = clipThreadPathByProgress(pathState, 1);
    const half = clipThreadPathByProgress(pathState, 0.5);
    expect(full[0].coordinates).toEqual(staticPath);
    expect(half[0].coordinates[0]).toEqual(source);
    const tip = half[0].coordinates.at(-1)!;
    const expectedTip = pointAtArcProgress(staticPath, 0.5, {
      cumulative: pathState.cumulative,
      total: pathState.totalLength,
      projected: pathState.projectedSamples,
    });
    expect(tip[0]).toBeCloseTo(expectedTip[0], 10);
    expect(tip[1]).toBeCloseTo(expectedTip[1], 10);
  });

  it("binds antrag-related threads to a shared target approach corridor", () => {
    const subject = "11111111-1111-5111-8111-111111111111";
    expect(threadCorridorKey({ threadId: "vote-1", subjectId: subject })).toBe(
      `subject:${subject}`,
    );
    expect(
      threadCorridorKey({ threadId: "proposal-1", subjectId: subject }),
    ).toBe(`subject:${subject}`);
    // Fallback never invents a subject relationship.
    expect(threadCorridorKey({ threadId: "vote-2", subjectId: null })).toBe(
      "thread:vote-2",
    );
    expect(
      threadCorridorKey({
        threadId: undefined,
        subjectId: null,
        fadenType: "vote",
      }),
    ).toBe("thread:anon:vote");

    const proposalApproach = threadTargetApproachVector(source, target, {
      fadenType: "proposal",
      threadId: "proposal-1",
      subjectId: subject,
    });
    const voteApproach = threadTargetApproachVector(source, target, {
      fadenType: "vote",
      threadId: "vote-1",
      subjectId: subject,
    });
    const talkApproach = threadTargetApproachVector(source, target, {
      fadenType: "conversation",
      threadId: "talk-1",
      subjectId: subject,
    });
    // Target-side approach axes align (dot product near 1) — not only bend sign.
    const dot = (a: [number, number], b: [number, number]) =>
      a[0] * b[0] + a[1] * b[1];
    expect(dot(proposalApproach, voteApproach)).toBeGreaterThan(0.999);
    expect(dot(proposalApproach, talkApproach)).toBeGreaterThan(0.999);

    // Without a subject id the corridor key stays private per thread — never
    // invents a shared relationship between unrelated votes.
    expect(
      threadCorridorKey({ threadId: "vote-private-a", subjectId: null }),
    ).not.toBe(
      threadCorridorKey({ threadId: "vote-private-b", subjectId: null }),
    );

    // Corridor-bound types declare the contract; without subject they stay private.
    expect(threadCurveProfile("vote").corridorBound).toBe(true);
    expect(threadCurveProfile("proposal").corridorBound).toBe(true);
    expect(threadCurveProfile("vote").maxBulgeFraction).toBeLessThan(
      threadCurveProfile("conversation").maxBulgeFraction,
    );
  });

  // Local helpers for the corridor-cone tests below.
  const dot = (a: [number, number], b: [number, number]) =>
    a[0] * b[0] + a[1] * b[1];
  const angleDeg = (a: [number, number], b: [number, number]) => {
    const na = Math.hypot(a[0], a[1]);
    const nb = Math.hypot(b[0], b[1]);
    const cos = Math.max(-1, Math.min(1, dot(a, b) / (na * nb)));
    return (Math.acos(cos) * 180) / Math.PI;
  };
  /** Places a source so its own natural entry direction (reverse chord from
   * target) is exactly `axis`, at `distanceM` projected metres from target. */
  const sourceAtNaturalAxis = (
    target: [number, number],
    axis: [number, number],
    distanceM: number,
  ): [number, number] => {
    const targetXY = projectLngLatToMercator(target[0], target[1]);
    return unprojectMercatorToLngLat(
      targetXY[0] + axis[0] * distanceM,
      targetXY[1] + axis[1] * distanceM,
    );
  };

  it("shares the exact subject corridor axis when a source's own direction already agrees", () => {
    // The common case: a source positioned so its natural entry direction
    // (reverse chord) coincides with the deterministic subject+target axis.
    // Geometry and corridor grouping agree completely, so the shared axis is
    // used exactly — subject-bound threads still visually converge.
    const subject = "22222222-2222-5222-8222-222222222222";
    const sharedTarget: [number, number] = [10.05, 53.55];
    const preferredAxis = threadTargetLocalCorridorAxis(sharedTarget, subject);
    const types = ["proposal", "vote", "conversation"] as const;
    // Perturb each source a little within the safe cone so they are still
    // distinct sources, not one single direction.
    const perturbedAxis = (deg: number): [number, number] => {
      const theta = (deg * Math.PI) / 180;
      const cosT = Math.cos(theta);
      const sinT = Math.sin(theta);
      return [
        preferredAxis[0] * cosT - preferredAxis[1] * sinT,
        preferredAxis[0] * sinT + preferredAxis[1] * cosT,
      ];
    };
    const sources = [
      sourceAtNaturalAxis(sharedTarget, perturbedAxis(-10), 12_000),
      sourceAtNaturalAxis(sharedTarget, perturbedAxis(0), 30_000),
      sourceAtNaturalAxis(sharedTarget, perturbedAxis(15), 6_000),
    ];
    const axes = sources.map(
      (src, index) =>
        threadCurveControlPointsProjected(src, sharedTarget, {
          fadenType: types[index],
          threadId: `multi-src-${index}`,
          subjectId: subject,
        }).corridorAxis,
    );
    for (const axis of axes) {
      expect(dot(axis, preferredAxis)).toBeGreaterThan(0.999);
    }
    for (let i = 0; i < axes.length; i += 1) {
      for (let j = i + 1; j < axes.length; j += 1) {
        expect(dot(axes[i], axes[j])).toBeGreaterThan(0.999);
      }
    }

    // Mid arcs may still differ by type/source; only the final approach axis
    // is shared.
    const midDefl = sources.map((src, index) => {
      const { p0, p1, p2, p3 } = threadCurveControlPointsProjected(
        src,
        sharedTarget,
        {
          fadenType: types[index],
          threadId: `multi-src-${index}`,
          subjectId: subject,
        },
      );
      const midX = 0.125 * (p0[0] + p3[0]) + 0.375 * (p1[0] + p2[0]);
      const midY = 0.125 * (p0[1] + p3[1]) + 0.375 * (p1[1] + p2[1]);
      return Math.hypot(midX - (p0[0] + p3[0]) / 2, midY - (p0[1] + p3[1]) / 2);
    });
    expect(midDefl[2]).toBeGreaterThan(midDefl[1]);
  });

  it("clamps the subject corridor axis into a safe per-source entry cone instead of forcing an identical tangent", () => {
    // Reproduces the real defect: a source whose own natural entry direction
    // (reverse chord) is diametrically opposed to the deterministic
    // subject+target corridor axis. Forcing the exact shared axis here (old
    // behaviour) drives the last handle straight past the target and back —
    // a visible hook. The honest contract is a shared *preferred* direction,
    // clamped per source into a safe cone, not an identical tangent for any
    // origin.
    const subject = "33333333-3333-5333-8333-333333333333";
    const sharedTarget: [number, number] = [10.05, 53.55];
    const preferredAxis = threadTargetLocalCorridorAxis(sharedTarget, subject);
    const naturalOpposite: [number, number] = [
      -preferredAxis[0],
      -preferredAxis[1],
    ];
    const oppositeSource = sourceAtNaturalAxis(
      sharedTarget,
      naturalOpposite,
      20_000,
    );
    const options = {
      fadenType: "proposal",
      threadId: "cone-opposite",
      subjectId: subject,
    };
    const ctrl = threadCurveControlPointsProjected(
      oppositeSource,
      sharedTarget,
      options,
    );

    // The clamp actually engaged: the used axis is not the raw shared axis...
    expect(dot(ctrl.corridorAxis, preferredAxis)).toBeLessThan(0.9);
    // ...but stays within the documented safe cone of *this* source's own
    // natural direction.
    expect(angleDeg(ctrl.corridorAxis, naturalOpposite)).toBeLessThanOrEqual(
      EDGE_CURVE_TARGET_APPROACH_CONE_DEG + 1e-6,
    );
    // Handle length stays bounded exactly as before (relative + absolute cap).
    const targetXY = projectLngLatToMercator(sharedTarget[0], sharedTarget[1]);
    const outLen = Math.hypot(
      ctrl.p2[0] - targetXY[0],
      ctrl.p2[1] - targetXY[1],
    );
    expect(outLen).toBeGreaterThan(0);
    expect(outLen).toBeLessThanOrEqual(
      Math.min(
        ctrl.length * EDGE_CURVE_MAX_HANDLE_FRACTION + 1e-6,
        EDGE_CURVE_MAX_HANDLE_M + 1e-6,
      ),
    );
    // The target approach direction (p3 - p2) keeps a clearly positive
    // forward component toward the target, not a sideways-or-backward one.
    const { dx, dy, length } = projectedChord(oppositeSource, sharedTarget);
    const along: [number, number] = [dx / length, dy / length];
    expect(dot(ctrl.targetApproach, along)).toBeGreaterThan(
      Math.cos((EDGE_CURVE_TARGET_APPROACH_CONE_DEG * Math.PI) / 180) - 1e-6,
    );

    // Hard invariant: control-point projections onto the chord axis are
    // non-decreasing (0 <= proj(p1) <= proj(p2) <= length), which is
    // sufficient for the Bezier derivative along that axis to never go
    // negative — no fold, no reversal, by construction.
    const sourceXY = projectLngLatToMercator(
      oppositeSource[0],
      oppositeSource[1],
    );
    const axialProjection = (p: readonly [number, number]) =>
      (p[0] - sourceXY[0]) * along[0] + (p[1] - sourceXY[1]) * along[1];
    const proj1 = axialProjection(ctrl.p1);
    const proj2 = axialProjection(ctrl.p2);
    expect(proj1).toBeGreaterThanOrEqual(-1e-6);
    expect(proj2).toBeGreaterThanOrEqual(proj1 - 1e-6);
    expect(proj2).toBeLessThanOrEqual(length + 1e-6);

    // The sampled curve itself stays fold-free: exact endpoints, finite, and
    // a clean, non-reversing final approach.
    const path = sampleThreadCurve(oppositeSource, sharedTarget, options);
    expect(path[0]).toEqual(oppositeSource);
    expect(path.at(-1)).toEqual(sharedTarget);
    for (const point of path) {
      expect(Number.isFinite(point[0])).toBe(true);
      expect(Number.isFinite(point[1])).toBe(true);
      expect(point[0] === 0 && point[1] === 0).toBe(false);
    }
    let prevAxial = Number.NEGATIVE_INFINITY;
    for (const [lng, lat] of path) {
      const xy = projectLngLatToMercator(lng, lat);
      const axial = axialProjection(xy);
      // Sampled points never move backward along the source→target axis
      // beyond floating-point noise.
      expect(axial).toBeGreaterThanOrEqual(prevAxial - 1e-6);
      prevAxial = axial;
    }
  });

  it("projects geometry in Web Mercator with stable EW length across latitudes", () => {
    const midSource: [number, number] = [10, 45];
    const midTarget: [number, number] = [10.2, 45];
    const highSource: [number, number] = [10, 70];
    const highTarget: [number, number] = [10.2, 70];
    const midLen = projectedChord(midSource, midTarget).length;
    const highLen = projectedChord(highSource, highTarget).length;
    // Same lon delta → same mercator-x chord at any latitude.
    expect(midLen).toBeCloseTo(highLen, 6);
    expect(midLen).toBeGreaterThan(0);

    const ns = projectedChord([10, 45], [10, 45.2]).length;
    // EW and NS with equal degree span differ in projected metres — geometry
    // uses that projected chord so map-space tension stays comparable.
    expect(Math.abs(midLen - ns)).toBeGreaterThan(1);

    const path = sampleThreadCurve(midSource, midTarget, {
      fadenType: "conversation",
      threadId: "proj-ew",
    });
    expect(path[0]).toEqual(midSource);
    expect(path.at(-1)).toEqual(midTarget);
  });

  it("measures arc progress in projected metres, not degree-space hypot", () => {
    // High-latitude diagonal: degree hypot weights Δlng/Δlat equally; Mercator
    // does not — a degree-space regression is detectable on total and tip.
    const highSource: [number, number] = [10, 68];
    const highTarget: [number, number] = [14, 72];
    const highPath = buildThreadPathState(highSource, highTarget, [], {
      fadenType: "conversation",
      threadId: "arc-high-lat",
    });
    const highDegree = degreeSpacePolylineArcState(highPath.samples);
    // Metres vs degrees: multi-order-of-magnitude gap (regression detector).
    expect(highPath.totalLength).toBeGreaterThan(highDegree.total * 1000);
    const highTip = pointAtArcProgress(highPath.samples, 0.5, {
      cumulative: highPath.cumulative,
      total: highPath.totalLength,
      projected: highPath.projectedSamples,
    });
    const highDegreeTip = pointAtArcProgress(highPath.samples, 0.5, {
      cumulative: highDegree.cumulative,
      total: highDegree.total,
    });
    // Degree-space progress misplaces the tip on a high-lat diagonal curve.
    expect(
      Math.hypot(highTip[0] - highDegreeTip[0], highTip[1] - highDegreeTip[1]),
    ).toBeGreaterThan(1e-4);

    // Pure N-S: projected Y stretch ≠ degree Δlat; totalLength is metres.
    const nsSource: [number, number] = [10, 45];
    const nsTarget: [number, number] = [10, 50];
    const nsPath = buildThreadPathState(nsSource, nsTarget, [], {
      fadenType: "conversation",
      threadId: "arc-ns",
    });
    const nsDegree = degreeSpacePolylineArcState(nsPath.samples);
    expect(nsPath.totalLength).toBeGreaterThan(nsDegree.total * 1000);
    expect(nsPath.projectedSamples.length).toBe(nsPath.samples.length);
    expect(nsPath.cumulative[0]).toBe(0);
    expect(nsPath.cumulative[nsPath.cumulative.length - 1]).toBe(
      nsPath.totalLength,
    );
    // Path state total is in metres (order of hundreds of km for 5° latitude).
    expect(nsPath.totalLength).toBeGreaterThan(100_000);
    // N-S midpoint progress differs once Mercator Y is nonlinear in lat.
    const nsTip = pointAtArcProgress(nsPath.samples, 0.5, {
      cumulative: nsPath.cumulative,
      total: nsPath.totalLength,
      projected: nsPath.projectedSamples,
    });
    const nsDegreeTip = pointAtArcProgress(nsPath.samples, 0.5, {
      cumulative: nsDegree.cumulative,
      total: nsDegree.total,
    });
    expect(Math.abs(nsTip[1] - nsDegreeTip[1])).toBeGreaterThan(1e-5);
  });

  it("never masks invalid projection as null-island [0,0]", () => {
    expect(projectedSampleToLngLat(Number.NaN, 0)).toBeNull();
    expect(projectedSampleToLngLat(0, Number.NaN)).toBeNull();
    expect(projectedSampleToLngLat(Number.POSITIVE_INFINITY, 0)).toBeNull();
    expect(projectedSampleToLngLat(0, Number.NEGATIVE_INFINITY)).toBeNull();
    // Finite projection still works.
    const origin = projectedSampleToLngLat(0, 0);
    expect(origin).not.toBeNull();
    expect(origin![0]).toBeCloseTo(0, 10);
    expect(origin![1]).toBeCloseTo(0, 10);
    // Sampled paths never inject a [0,0] spike on non-zero endpoints.
    const path = sampleThreadCurve([9, 53], [11, 54], {
      fadenType: "proposal",
      threadId: "finite-fallback",
    });
    for (const point of path) {
      expect(Number.isFinite(point[0])).toBe(true);
      expect(Number.isFinite(point[1])).toBe(true);
      expect(point[0] === 0 && point[1] === 0).toBe(false);
    }
  });

  it("uses the short unwrapped antimeridian path without NaN samples", () => {
    const west: [number, number] = [170, 10];
    const east: [number, number] = [-170, 10];
    expect(shortestLongitudeDelta(170, -170)).toBe(20);
    expect(Math.abs(shortestLongitudeDelta(170, -170))).toBeLessThan(180);
    const chord = projectedChord(west, east);
    // Unwrapped target at 190° — short 20° hop, not the 340° long way.
    expect(chord.unwrappedTargetLng).toBeCloseTo(190, 10);
    // Naive lon subtraction without unwrap would span 340°.
    const naiveDx =
      projectLngLatToMercator(-170, 10)[0] -
      projectLngLatToMercator(170, 10)[0];
    const naiveLong = Math.hypot(naiveDx, 0);
    expect(chord.length).toBeLessThan(naiveLong * 0.2);
    expect(chord.length).toBeCloseTo(
      Math.abs((20 * Math.PI * EDGE_CURVE_MERCATOR_RADIUS_M) / 180),
      0,
    );

    const path = sampleThreadCurve(west, east, {
      fadenType: "conversation",
      threadId: "anti-1",
    });
    expect(path[0]).toEqual(west);
    expect(path.at(-1)).toEqual(east);
    for (const point of path) {
      expect(Number.isFinite(point[0])).toBe(true);
      expect(Number.isFinite(point[1])).toBe(true);
    }
    // Intermediate samples follow the unwrapped short corridor (lng ≥ 170).
    for (let index = 1; index < path.length - 1; index += 1) {
      expect(path[index][0]).toBeGreaterThan(169);
    }
  });

  it("builds a path state once and clips progress without rebuilding", () => {
    resetThreadPathBuildSerialForTests();
    const before = getThreadPathBuildSerialForTests();
    const path = buildThreadPathState(source, target, ["#111111", "#222222"], {
      fadenType: "proposal",
      threadId: "cache-1",
    });
    expect(getThreadPathBuildSerialForTests()).toBe(before + 1);
    const a = clipThreadPathByProgress(path, 0.25);
    const b = clipThreadPathByProgress(path, 0.5);
    const c = clipThreadPathByProgress(path, 0.9);
    expect(getThreadPathBuildSerialForTests()).toBe(before + 1);
    expect(a.length).toBeGreaterThan(0);
    expect(b.length).toBeGreaterThanOrEqual(a.length);
    expect(c.length).toBeGreaterThanOrEqual(b.length);
    const tip = c[c.length - 1].coordinates.at(-1)!;
    const expected = pointAtArcProgress(path.samples, 0.9, {
      cumulative: path.cumulative,
      total: path.totalLength,
      projected: path.projectedSamples,
    });
    expect(tip[0]).toBeCloseTo(expected[0], 10);
    expect(tip[1]).toBeCloseTo(expected[1], 10);
  });

  it("keeps every yarn body continuous and differentiates types on the highlight", () => {
    for (const variant of EDGE_THREAD_VARIANTS) {
      expect(variant.bodyContinuous).toBe(true);
      expect(variant.shadowContinuous).toBe(true);
      expect(variant.dashArray[0]).toBeGreaterThan(0);
    }
  });

  it("keeps multi-colour strand colours stable under partial progress", () => {
    const colors = ["#111111", "#222222", "#333333"];
    const full = buildThemedLineSegments(source, target, colors, {
      fadenType: "knotting",
      threadId: "edge-multi",
    });
    const half = buildProgressClippedThemeSegments(
      source,
      target,
      colors,
      0.5,
      { fadenType: "knotting", threadId: "edge-multi" },
    );
    expect(half.length).toBeGreaterThan(0);
    expect(half.length).toBeLessThanOrEqual(full.length);
    expect(half[0].color).toBe(full[0].color);
    expect(half[0].coordinates[0]).toEqual(full[0].coordinates[0]);
  });

  describe("curvature-adaptive sampling", () => {
    it("grows breakpoint count with control-polygon curvature and stays hard bounded", () => {
      // Fully collinear control points: zero tangent rotation anywhere — must
      // stay at the minimum floor, never invent extra points for a straight line.
      const straight = threadCurveAdaptiveBreakpoints(
        [0, 0],
        [10, 0],
        [20, 0],
        [30, 0],
      );
      expect(straight.length).toBe(EDGE_CURVE_MIN_SAMPLES);

      // Wide, clearly-visible (km-scale) hump: real curvature spread across
      // the whole span must pull the count above the minimum.
      const curvy = threadCurveAdaptiveBreakpoints(
        [0, 0],
        [0, 1500],
        [3000, 1500],
        [3000, 0],
      );
      expect(curvy.length).toBeGreaterThan(straight.length);
      expect(curvy.length).toBeLessThanOrEqual(EDGE_CURVE_MAX_SAMPLES);

      // Sharp hook: last handle direction opposes the incoming chord — even
      // higher local curvature — but the hard cap must still hold.
      const hook = threadCurveAdaptiveBreakpoints(
        [0, 0],
        [1000, 200],
        [900, -3000],
        [1000, -2800],
      );
      expect(hook.length).toBeGreaterThan(straight.length);
      expect(hook.length).toBeLessThanOrEqual(EDGE_CURVE_MAX_SAMPLES);

      // Integration: the same relationship holds through the public
      // distance+profile entry points, and sampleThreadCurve's length always
      // matches what threadCurveSampleCount reports for the same geometry.
      const straightPrivate = threadCurveSampleCount(source, target, {
        fadenType: "vote",
        threadId: "adaptive-straight",
      });
      const curvyPrivate = threadCurveSampleCount(source, target, {
        fadenType: "conversation",
        threadId: "adaptive-curvy",
      });
      expect(curvyPrivate).toBeGreaterThan(straightPrivate);
      expect(curvyPrivate).toBeLessThanOrEqual(EDGE_CURVE_MAX_SAMPLES);
      expect(
        sampleThreadCurve(source, target, {
          fadenType: "conversation",
          threadId: "adaptive-curvy",
        }).length,
      ).toBe(curvyPrivate);
    });

    it("keeps a large deterministic sweep fold-free: monotone control points, non-backward samples, exact/finite endpoints", () => {
      // Large deterministic sweep across sources/targets/subjects/types (via
      // the exported hashUnit — no Math.random) so this reproduces identically
      // on every run. Historically, a subject-bound target approach could pick
      // its final handle direction from subject+target only (see
      // threadTargetLocalCorridorAxis), independent of the source chord —
      // for some combinations that folded the curve back on itself right
      // before the target. The control-point invariant now makes that
      // mathematically impossible; this sweep verifies it holds everywhere,
      // not just in the specific case it was found in.
      const types = [
        "proposal",
        "vote",
        "conversation",
        "knotting",
        "out",
      ] as const;
      let worstVisibleTurnDeg = 0;
      let worstInfo = "";
      for (let index = 0; index < 200; index += 1) {
        const subject = `regression-hook-${index}`;
        const target: [number, number] = [
          -30 + hashUnit(`${subject}:lng`) * 60,
          20 + hashUnit(`${subject}:lat`) * 40,
        ];
        const sourceAngle = hashUnit(`${subject}:angle`) * Math.PI * 2;
        const distanceM = 2_000 + hashUnit(`${subject}:dist`) * 40_000;
        const from: [number, number] = [
          target[0] + (Math.cos(sourceAngle) * distanceM) / 111_320,
          target[1] + (Math.sin(sourceAngle) * distanceM) / 110_540,
        ];
        const fadenType = types[index % types.length];
        // Half the sweep is a proven subject relationship (corridor path,
        // the historically risky case); half stays private fallback.
        const subjectId = index % 2 === 0 ? subject : null;
        const options = {
          fadenType,
          threadId: `regression-hook-thread-${index}`,
          subjectId,
        };

        // Control-point invariant: 0 <= proj(p1) <= proj(p2) <= length on the
        // source→target chord axis, for every case in the sweep.
        const ctrl = threadCurveControlPointsProjected(from, target, options);
        const { dx, dy, length: chordLength } = projectedChord(from, target);
        expect(chordLength).toBeGreaterThan(0);
        const along: [number, number] = [dx / chordLength, dy / chordLength];
        const sourceXY = projectLngLatToMercator(from[0], from[1]);
        const axialProjection = (p: readonly [number, number]) =>
          (p[0] - sourceXY[0]) * along[0] + (p[1] - sourceXY[1]) * along[1];
        const proj1 = axialProjection(ctrl.p1);
        const proj2 = axialProjection(ctrl.p2);
        expect(proj1, `index=${index} proj1`).toBeGreaterThanOrEqual(-1e-6);
        expect(proj2, `index=${index} proj2>=proj1`).toBeGreaterThanOrEqual(
          proj1 - 1e-6,
        );
        expect(proj2, `index=${index} proj2<=length`).toBeLessThanOrEqual(
          chordLength + 1e-6,
        );

        const path = sampleThreadCurve(from, target, options);
        expect(path[0]).toEqual(from);
        expect(path.at(-1)).toEqual(target);
        expect(path.length).toBeGreaterThanOrEqual(2);
        expect(path.length).toBeLessThanOrEqual(EDGE_CURVE_MAX_SAMPLES);

        const projected = path.map(([lng, lat]) =>
          projectLngLatToMercator(lng, lat),
        );
        let prevAxial = Number.NEGATIVE_INFINITY;
        for (let s = 0; s < projected.length; s += 1) {
          const [px, py] = projected[s];
          expect(Number.isFinite(px), `index=${index} finite x`).toBe(true);
          expect(Number.isFinite(py), `index=${index} finite y`).toBe(true);
          expect(px === 0 && py === 0, `index=${index} not null-island`).toBe(
            false,
          );
          // Sampled points never move backward along the chord axis — the
          // direct, testable consequence of the control-point invariant.
          const axial = axialProjection([px, py]);
          expect(
            axial,
            `index=${index} sample ${s} axial monotonic`,
          ).toBeGreaterThanOrEqual(prevAxial - 1e-6);
          prevAxial = axial;
        }

        for (let s = 2; s < projected.length; s += 1) {
          const a = projected[s - 2];
          const b = projected[s - 1];
          const c = projected[s];
          const v1x = b[0] - a[0];
          const v1y = b[1] - a[1];
          const v2x = c[0] - b[0];
          const v2y = c[1] - b[1];
          const n1 = Math.hypot(v1x, v1y);
          const n2 = Math.hypot(v2x, v2y);
          // Sub-few-metre segments are indistinguishable from a point at any
          // map zoom that ever renders a Faden; a residual angle confined to
          // that scale is not a visible defect (see
          // EDGE_CURVE_MIN_VISIBLE_SEGMENT_M).
          if (n1 <= 5 || n2 <= 5) continue;
          const cos = Math.max(
            -1,
            Math.min(1, (v1x * v2x + v1y * v2y) / (n1 * n2)),
          );
          const turnDeg = (Math.acos(cos) * 180) / Math.PI;
          if (turnDeg > worstVisibleTurnDeg) {
            worstVisibleTurnDeg = turnDeg;
            worstInfo = `index=${index} type=${fadenType}`;
          }
        }
      }
      // Well inside the pre-existing 120° self-loop guard (see the
      // multi-source corridor test) — the fix keeps a wide margin, not a
      // borderline pass.
      expect(worstVisibleTurnDeg, worstInfo).toBeLessThan(60);
    });

    it("catches an interior S-curve/cusp even when the span's two end tangents agree", () => {
      // Synthetic cubic where B'(0) and B'(1) point the exact same direction
      // (0, 3h) — a pure two-point endpoint-tangent comparison sees zero
      // rotation across the whole span and would never refine it — while the
      // curve actually loops down through p2 and back up, a real interior
      // S-curve/cusp. See the derivation: B'(0) = 3(p1-p0) = (0, 3h);
      // B'(1) = 3(p3-p2) = 3((w,0)-(w,-h)) = (0, 3h) for any h, w.
      const h = 100;
      const w = 100;
      const p0: [number, number] = [0, 0];
      const p1: [number, number] = [0, h];
      const p2: [number, number] = [w, -h];
      const p3: [number, number] = [w, 0];
      // Isolate the whole-span behaviour: minSamples=2 means the only initial
      // span is [0, 1], exactly the one whose end tangents coincide.
      const breakpoints = threadCurveAdaptiveBreakpoints(p0, p1, p2, p3, 2);
      expect(breakpoints.length).toBeGreaterThan(2);
      expect(breakpoints.length).toBeLessThanOrEqual(EDGE_CURVE_MAX_SAMPLES);
    });

    it("does not skip a span whose endpoints are close but whose interior arc is large", () => {
      // p0/p3 sit within EDGE_CURVE_MIN_VISIBLE_SEGMENT_M of each other (a
      // near-loop back to the start), but the control polygon bows out over a
      // kilometre — a real, highly visible detour. The old endpoint-chord-only
      // gate would call this span "invisible" and never refine it.
      const p0: [number, number] = [0, 0];
      const p1: [number, number] = [0, 2000];
      const p2: [number, number] = [2000, 2000];
      const p3: [number, number] = [3, 4]; // chord to p0 is exactly 5m.
      expect(Math.hypot(p3[0] - p0[0], p3[1] - p0[1])).toBe(5);
      // minSamples=2 isolates the single initial span [0, 1] so only the new
      // arc-length-estimate gate (not a later, already-split span) is tested.
      const breakpoints = threadCurveAdaptiveBreakpoints(p0, p1, p2, p3, 2);
      expect(breakpoints.length).toBeGreaterThan(2);
      expect(breakpoints.length).toBeLessThanOrEqual(EDGE_CURVE_MAX_SAMPLES);
    });

    it("refines the deterministic 179-degree base counterexample below the hard cap", () => {
      function cubicBezierPoint2(
        p0: [number, number],
        p1: [number, number],
        p2: [number, number],
        p3: [number, number],
        t: number,
      ): [number, number] {
        const u = 1 - t;
        return [
          u * u * u * p0[0] +
            3 * u * u * t * p1[0] +
            3 * u * t * t * p2[0] +
            t * t * t * p3[0],
          u * u * u * p0[1] +
            3 * u * u * t * p1[1] +
            3 * u * t * t * p2[1] +
            t * t * t * p3[1],
        ];
      }
      // Extracted from a fixed-seed 100,000-case sweep against the base
      // implementation. It stopped at five samples and left a 179.994°
      // junction between a 3.34m segment and a 24.13m visible segment.
      const p0: [number, number] = [-1.290023703291153, 0.6678560138576546];
      const p1: [number, number] = [1.7066032000908127, -11.70957249949549];
      const p2: [number, number] = [-2.4536575258202595, -57.983649109594765];
      const p3: [number, number] = [0.7909150758655675, -6.038544293336091];
      const ts = threadCurveAdaptiveBreakpoints(p0, p1, p2, p3);
      expect(ts.length).toBeLessThanOrEqual(EDGE_CURVE_MAX_SAMPLES);
      const points = ts.map((t) => cubicBezierPoint2(p0, p1, p2, p3, t));

      for (let i = 1; i < points.length - 1; i += 1) {
        const v1 = [
          points[i][0] - points[i - 1][0],
          points[i][1] - points[i - 1][1],
        ];
        const v2 = [
          points[i + 1][0] - points[i][0],
          points[i + 1][1] - points[i][1],
        ];
        const l1 = Math.hypot(v1[0], v1[1]);
        const l2 = Math.hypot(v2[0], v2[1]);
        if (
          l1 > EDGE_CURVE_MIN_VISIBLE_SEGMENT_M ||
          l2 > EDGE_CURVE_MIN_VISIBLE_SEGMENT_M
        ) {
          const cos = Math.max(
            -1,
            Math.min(1, (v1[0] * v2[0] + v1[1] * v2[1]) / (l1 * l2)),
          );
          const turnDeg = (Math.acos(cos) * 180) / Math.PI;
          expect(turnDeg).toBeLessThanOrEqual(
            EDGE_CURVE_TANGENT_ANGLE_TOLERANCE_DEG,
          );
        }
      }
    });

    it("strictly respects the configured sample budget under extreme curvature and multiple kinks", () => {
      const p0: [number, number] = [0, 0];
      const p1: [number, number] = [5000, -2000];
      const p2: [number, number] = [5200, 3000];
      const p3: [number, number] = [4800, 100];
      const ts = threadCurveAdaptiveBreakpoints(p0, p1, p2, p3);
      expect(ts.length).toBeGreaterThan(EDGE_CURVE_MIN_SAMPLES);
      expect(ts.length).toBeLessThanOrEqual(EDGE_CURVE_MAX_SAMPLES);
      for (let i = 0; i < ts.length; i += 1) {
        expect(Number.isFinite(ts[i])).toBe(true);
        if (i > 0) {
          expect(ts[i]).toBeGreaterThan(ts[i - 1]);
        }
      }
    });

    it("handles degenerate control points with zero tangents cleanly and deterministically", () => {
      const p0: [number, number] = [123.4, 567.8];
      const p1: [number, number] = [123.4, 567.8];
      const p2: [number, number] = [123.4, 567.8];
      const p3: [number, number] = [123.4, 567.8];
      const ts = threadCurveAdaptiveBreakpoints(p0, p1, p2, p3);
      expect(ts).toHaveLength(EDGE_CURVE_MIN_SAMPLES);
      expect(ts[0]).toBe(0);
      expect(ts.at(-1)).toBe(1);

      const q0: [number, number] = [0, 0];
      const q1: [number, number] = [0, 0];
      const q2: [number, number] = [1000, 500];
      const q3: [number, number] = [1000, 500];
      const ts2 = threadCurveAdaptiveBreakpoints(q0, q1, q2, q3);
      expect(ts2.length).toBeGreaterThanOrEqual(EDGE_CURVE_MIN_SAMPLES);
      expect(ts2.length).toBeLessThanOrEqual(EDGE_CURVE_MAX_SAMPLES);
    });

    it("preserves sampling bounds and exact endpoints for normal production profiles", () => {
      const src: [number, number] = [9.9, 53.5];
      const tgt: [number, number] = [10.1, 53.65];
      for (const fadenType of [
        "conversation",
        "proposal",
        "vote",
        "knotting",
        "out",
      ] as const) {
        const { p0, p1, p2, p3 } = threadCurveControlPointsProjected(src, tgt, {
          fadenType,
          threadId: `normal-prod-${fadenType}`,
        });
        const ts = threadCurveAdaptiveBreakpoints(p0, p1, p2, p3);
        expect(ts.length).toBeGreaterThanOrEqual(EDGE_CURVE_MIN_SAMPLES);
        expect(ts.length).toBeLessThanOrEqual(EDGE_CURVE_MAX_SAMPLES);
        expect(ts[0]).toBe(0);
        expect(ts.at(-1)).toBe(1);
      }
    });
  });

  describe("extreme projections", () => {
    it("stays finite and keeps exact endpoints across the antimeridian seam", () => {
      const west: [number, number] = [179.999, 12];
      const east: [number, number] = [-179.999, -8];
      const path = sampleThreadCurve(west, east, {
        fadenType: "proposal",
        threadId: "extreme-antimeridian",
      });
      expect(path[0]).toEqual(west);
      expect(path.at(-1)).toEqual(east);
      for (const [lng, lat] of path) {
        expect(Number.isFinite(lng)).toBe(true);
        expect(Number.isFinite(lat)).toBe(true);
        expect(lng === 0 && lat === 0).toBe(false);
      }
    });

    it("stays finite and keeps exact endpoints at very high latitudes", () => {
      const nearNorthPole: [number, number] = [5, 89.9];
      const nearSouthPole: [number, number] = [-170, -89.9];
      const path = sampleThreadCurve(nearNorthPole, nearSouthPole, {
        fadenType: "knotting",
        threadId: "extreme-pole-to-pole",
      });
      expect(path[0]).toEqual(nearNorthPole);
      expect(path.at(-1)).toEqual(nearSouthPole);
      for (const [lng, lat] of path) {
        expect(Number.isFinite(lng)).toBe(true);
        expect(Number.isFinite(lat)).toBe(true);
        expect(lng === 0 && lat === 0).toBe(false);
      }

      // Same-latitude high-lat pair (Mercator projection stresses the
      // internal latitude clamp without a large N-S component too).
      const highA: [number, number] = [10, 88.5];
      const highB: [number, number] = [170, 88.5];
      const highPath = sampleThreadCurve(highA, highB, {
        fadenType: "conversation",
        threadId: "extreme-high-lat-ew",
      });
      expect(highPath[0]).toEqual(highA);
      expect(highPath.at(-1)).toEqual(highB);
      for (const [lng, lat] of highPath) {
        expect(Number.isFinite(lng)).toBe(true);
        expect(Number.isFinite(lat)).toBe(true);
      }
    });

    it("stays finite and keeps exact endpoints for near-antipodal, very long threads", () => {
      const source1: [number, number] = [10, 45];
      // Nearly the antipodal point of source1.
      const antipodal: [number, number] = [-169.5, -44.5];
      const path = sampleThreadCurve(source1, antipodal, {
        fadenType: "out",
        threadId: "extreme-near-antipodal",
      });
      expect(path[0]).toEqual(source1);
      expect(path.at(-1)).toEqual(antipodal);
      expect(path.length).toBeGreaterThanOrEqual(EDGE_CURVE_MIN_SAMPLES);
      expect(path.length).toBeLessThanOrEqual(EDGE_CURVE_MAX_SAMPLES);
      for (const [lng, lat] of path) {
        expect(Number.isFinite(lng)).toBe(true);
        expect(Number.isFinite(lat)).toBe(true);
        expect(lng === 0 && lat === 0).toBe(false);
      }
    });
  });
});
