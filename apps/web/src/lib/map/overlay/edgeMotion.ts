import type {
  GeoJSONSource,
  LineLayerSpecification,
  Map as MapLibreMap,
} from "maplibre-gl";
import {
  gradeColorExpression,
  gradeMapCanvasColor,
  gradeMapPaint,
} from "$lib/map/canvasColorGrade";
import { isValidMapCoordinate } from "$lib/map/coordinates";
import type { MapEdge, MapEntityViewModel } from "$lib/map/types";
import { targetThemePalette } from "$lib/map/weaveModel";
import { primaryWeaveColor } from "$lib/map/weaveTheme";
import {
  buildEndpointIndex,
  buildThreadPathState,
  clipThreadPathByProgress,
  EDGE_THREAD_LAYER_IDS,
  EDGE_THREAD_VARIANTS,
  EDGE_VISUAL_STYLE,
  FADEN_OUT_RENDER_KIND,
  type ThreadPathState,
  type ThreadRenderKind,
} from "./edges";

export const EDGE_MOTION_SOURCE = "edge-motion-source";
/** Canonical layer triple for the single untyped Faden-out lane. */
export const EDGE_MOTION_LAYER = "edge-motion-layer-out";
export const EDGE_MOTION_SHADOW_LAYER = "edge-motion-shadow-layer-out";
export const EDGE_MOTION_HIGHLIGHT_LAYER = "edge-motion-highlight-layer-out";
/** @deprecated Prefer EDGE_MOTION_SHADOW_LAYER. */
export const EDGE_MOTION_HALO_LAYER = EDGE_MOTION_SHADOW_LAYER;
export const EDGE_MOTION_DURATION_MS = 720;
export const EDGE_MOTION_MAX_ACTIVE = 8;

/**
 * Bounded, canonical motion layers: one shadow+body+highlight triple per
 * thread type. Same structural types and yarn paint as the static projection —
 * no unbounded layer growth.
 */
export const EDGE_MOTION_LAYER_IDS: readonly string[] =
  EDGE_THREAD_VARIANTS.flatMap((variant) => [
    motionShadowLayerId(variant.fadenType),
    motionMainLayerId(variant.fadenType),
    motionHighlightLayerId(variant.fadenType),
  ]);

type LngLatTuple = [number, number];
type EdgeMotionPhase = "creating" | "releasing";
type LayerFilter = Parameters<MapLibreMap["setFilter"]>[1];

export interface EdgeMotionInput {
  id: string;
  source: LngLatTuple;
  target: LngLatTuple;
  kind: string;
  fadenType?: ThreadRenderKind;
  /** Shared approach corridor (`faden_subject_id`); null/absent = private fallback. */
  fadenSubjectId?: string | null;
  themeColor?: string;
  themeColors?: string[];
  opacity?: number;
}

export interface EdgeMotionScheduler {
  now: () => number;
  requestFrame: (callback: FrameRequestCallback) => number;
  cancelFrame: (handle: number) => void;
  prefersReducedMotion: () => boolean;
}

interface ActiveMotion {
  input: EdgeMotionInput;
  phase: EdgeMotionPhase;
  from: number;
  to: number;
  startedAt: number;
  durationMs: number;
  sequence: number;
  /**
   * Immutable canonical path (samples, arc lengths, colour seams). Built once
   * when the motion is created or its geometry identity changes — never per RAF.
   */
  pathState: ThreadPathState;
}

export interface EdgeMotionSnapshot {
  activeCount: number;
  framePending: boolean;
  frameRequests: number;
  frameCallbacks: number;
  styleRefreshes: number;
  /** Cumulative ThreadPathState builds performed by this controller. */
  pathBuilds: number;
  suppressedIds: string[];
  active: Array<{
    id: string;
    phase: EdgeMotionPhase;
    progress: number;
    pathBuildSerial: number;
  }>;
}

function motionMainLayerId(fadenType: string): string {
  return fadenType === FADEN_OUT_RENDER_KIND
    ? EDGE_MOTION_LAYER
    : `edge-motion-layer-${fadenType}`;
}

function motionShadowLayerId(fadenType: string): string {
  return fadenType === FADEN_OUT_RENDER_KIND
    ? EDGE_MOTION_SHADOW_LAYER
    : `edge-motion-shadow-layer-${fadenType}`;
}

function motionHighlightLayerId(fadenType: string): string {
  return fadenType === FADEN_OUT_RENDER_KIND
    ? EDGE_MOTION_HIGHLIGHT_LAYER
    : `edge-motion-highlight-layer-${fadenType}`;
}

function defaultScheduler(): EdgeMotionScheduler {
  return {
    now: () => performance.now(),
    requestFrame: (callback) => requestAnimationFrame(callback),
    cancelFrame: (handle) => cancelAnimationFrame(handle),
    prefersReducedMotion: () =>
      typeof matchMedia === "function" &&
      matchMedia("(prefers-reduced-motion: reduce)").matches,
  };
}

function clamp01(value: number): number {
  return Math.max(0, Math.min(1, value));
}

function easeInOutCubic(value: number): number {
  const progress = clamp01(value);
  return progress < 0.5
    ? 4 * progress * progress * progress
    : 1 - Math.pow(-2 * progress + 2, 3) / 2;
}

function motionThemeFromTarget(target: MapEntityViewModel): {
  themeColor?: string;
  themeColors?: string[];
} {
  if (target.type !== "node" && target.type !== "webgemeindezentrum") {
    return {};
  }
  const themeColors = target.weave
    ? targetThemePalette(target.weave)
    : [primaryWeaveColor(target)];
  return {
    themeColor: themeColors[0],
    themeColors,
  };
}

/** Resolve the already-authoritative edge and map projection into motion input. */
export function resolveEdgeMotionInput(
  edge: MapEdge,
  points: readonly MapEntityViewModel[],
): EdgeMotionInput | null {
  const pointMap = buildEndpointIndex(points);
  const source = pointMap.get(edge.source_id);
  const target = pointMap.get(edge.target_id);
  if (!source || !target) return null;
  if (
    !isValidMapCoordinate(source.lon, source.lat) ||
    !isValidMapCoordinate(target.lon, target.lat)
  ) {
    return null;
  }
  const theme = motionThemeFromTarget(target);
  return {
    id: edge.id,
    source: [source.lon, source.lat],
    target: [target.lon, target.lat],
    kind: edge.edge_kind,
    fadenType: edge.faden_type ?? FADEN_OUT_RENDER_KIND,
    fadenSubjectId: edge.faden_subject_id ?? null,
    ...theme,
  };
}

/**
 * Short-lived visual overlay for event-bound Faden transitions.
 *
 * The canonical edge source remains untouched. While one transition is visible,
 * the same edge id is filtered from the static layers and drawn in this separate
 * source with the same themed segments and per-type yarn style as the static
 * projection. A single RAF exists only while at least one transition is active.
 */
export class EdgeMotionController {
  private readonly active = new Map<string, ActiveMotion>();
  private readonly releaseSuppressed = new Set<string>();
  private readonly baseFilters = new Map<string, LayerFilter>();
  private readonly appliedFilterSignatures = new Map<string, string>();
  private visibleEdgeIds: Set<string> | null = null;
  private frameHandle: number | null = null;
  private sequence = 0;
  private destroyed = false;
  private rendering = false;
  private frameRequests = 0;
  private frameCallbacks = 0;
  private styleRefreshes = 0;
  private pathBuilds = 0;
  private readonly scheduler: EdgeMotionScheduler;

  private readonly handleStyleData = () => {
    if (this.destroyed || this.hasMotionStyle()) return;
    this.styleRefreshes += 1;
    this.appliedFilterSignatures.clear();
    this.render();
  };

  constructor(
    private readonly map: MapLibreMap,
    scheduler: Partial<EdgeMotionScheduler> = {},
  ) {
    this.scheduler = { ...defaultScheduler(), ...scheduler };
    this.map.on("styledata", this.handleStyleData);
  }

  setVisibleEdgeIds(ids: ReadonlySet<string> | null): void {
    this.visibleEdgeIds = ids == null ? null : new Set(ids);
    this.render();
  }

  /** Release suppression only after canonical data confirms the edge is gone. */
  syncCanonicalEdges(edges: readonly MapEdge[]): void {
    const canonicalIds = new Set(edges.map((edge) => edge.id));
    let changed = false;
    for (const id of this.releaseSuppressed) {
      if (!canonicalIds.has(id)) {
        this.releaseSuppressed.delete(id);
        changed = true;
      }
    }
    if (changed) this.render();
  }

  startCreate(input: EdgeMotionInput): void {
    this.start(input, "creating", 1);
  }

  startRelease(input: EdgeMotionInput): void {
    this.start(input, "releasing", 0);
  }

  cancel(id: string): void {
    const changed = this.active.delete(id) || this.releaseSuppressed.delete(id);
    if (changed) this.render();
    if (this.active.size === 0) this.cancelScheduledFrame();
  }

  refreshStyle(): void {
    this.appliedFilterSignatures.clear();
    this.render();
  }

  inspect(): EdgeMotionSnapshot {
    const now = this.scheduler.now();
    return {
      activeCount: this.active.size,
      framePending: this.frameHandle !== null,
      frameRequests: this.frameRequests,
      frameCallbacks: this.frameCallbacks,
      styleRefreshes: this.styleRefreshes,
      pathBuilds: this.pathBuilds,
      suppressedIds: Array.from(this.releaseSuppressed).sort(),
      active: Array.from(this.active.values())
        .sort((left, right) => left.sequence - right.sequence)
        .map((motion) => ({
          id: motion.input.id,
          phase: motion.phase,
          progress: this.progressAt(motion, now),
          pathBuildSerial: motion.pathState.buildSerial,
        })),
    };
  }

  destroy(): void {
    if (this.destroyed) return;
    this.destroyed = true;
    this.cancelScheduledFrame();
    this.map.off("styledata", this.handleStyleData);
    this.active.clear();
    this.releaseSuppressed.clear();
    this.restoreStaticFilters();
    try {
      for (const layerId of EDGE_MOTION_LAYER_IDS) {
        if (this.map.getLayer(layerId)) this.map.removeLayer(layerId);
      }
      if (this.map.getSource(EDGE_MOTION_SOURCE)) {
        this.map.removeSource(EDGE_MOTION_SOURCE);
      }
    } catch {
      // Map destruction/style replacement may already have removed these items.
    }
  }

  private start(
    input: EdgeMotionInput,
    phase: EdgeMotionPhase,
    targetProgress: number,
  ): void {
    if (this.destroyed) return;
    const now = this.scheduler.now();
    const existing = this.active.get(input.id);
    const currentProgress = existing
      ? this.progressAt(existing, now)
      : phase === "creating"
        ? 0
        : 1;

    this.releaseSuppressed.delete(input.id);

    if (this.scheduler.prefersReducedMotion()) {
      this.active.delete(input.id);
      if (phase === "releasing") this.releaseSuppressed.add(input.id);
      this.render();
      if (this.active.size === 0) this.cancelScheduledFrame();
      return;
    }

    if (!existing) this.ensureCapacity();
    // Reuse the immutable path when geometry identity is unchanged (e.g. reverse
    // create→release). Rebuild only when endpoints/palette/type/subject change.
    const pathState = this.resolvePathState(input, existing);
    const distance = Math.abs(targetProgress - currentProgress);
    if (distance <= Number.EPSILON) {
      this.complete({
        input,
        phase,
        from: currentProgress,
        to: targetProgress,
        startedAt: now,
        durationMs: 0,
        sequence: ++this.sequence,
        pathState,
      });
      this.render();
      return;
    }

    this.active.set(input.id, {
      input,
      phase,
      from: currentProgress,
      to: targetProgress,
      startedAt: now,
      durationMs: Math.max(120, EDGE_MOTION_DURATION_MS * distance),
      sequence: existing?.sequence ?? ++this.sequence,
      pathState,
    });
    this.render();
    this.scheduleFrame();
  }

  private motionGeometryKey(input: EdgeMotionInput): string {
    const palette = this.motionPalette(input).join(",");
    return [
      input.source[0],
      input.source[1],
      input.target[0],
      input.target[1],
      input.fadenType ?? FADEN_OUT_RENDER_KIND,
      input.fadenSubjectId ?? "",
      input.id,
      palette,
    ].join("|");
  }

  private resolvePathState(
    input: EdgeMotionInput,
    existing: ActiveMotion | undefined,
  ): ThreadPathState {
    if (
      existing &&
      this.motionGeometryKey(existing.input) === this.motionGeometryKey(input)
    ) {
      return existing.pathState;
    }
    const palette = this.motionPalette(input);
    const fadenType = input.fadenType ?? FADEN_OUT_RENDER_KIND;
    this.pathBuilds += 1;
    return buildThreadPathState(input.source, input.target, palette, {
      fadenType,
      threadId: input.id,
      subjectId: input.fadenSubjectId ?? null,
    });
  }

  private ensureCapacity(): void {
    if (this.active.size < EDGE_MOTION_MAX_ACTIVE) return;
    const oldest = Array.from(this.active.values()).sort(
      (left, right) => left.sequence - right.sequence,
    )[0];
    if (!oldest) return;
    this.active.delete(oldest.input.id);
    this.complete(oldest);
  }

  private complete(motion: ActiveMotion): void {
    if (motion.phase === "releasing") {
      this.releaseSuppressed.add(motion.input.id);
    } else {
      this.releaseSuppressed.delete(motion.input.id);
    }
  }

  private progressAt(motion: ActiveMotion, now: number): number {
    if (motion.durationMs <= 0) return motion.to;
    const elapsed = clamp01((now - motion.startedAt) / motion.durationMs);
    const eased = easeInOutCubic(elapsed);
    return motion.from + (motion.to - motion.from) * eased;
  }

  private scheduleFrame(): void {
    if (this.destroyed || this.frameHandle !== null || this.active.size === 0) {
      return;
    }
    this.frameRequests += 1;
    this.frameHandle = this.scheduler.requestFrame(() => {
      this.frameHandle = null;
      this.frameCallbacks += 1;
      this.tick();
    });
  }

  private cancelScheduledFrame(): void {
    if (this.frameHandle === null) return;
    this.scheduler.cancelFrame(this.frameHandle);
    this.frameHandle = null;
  }

  private tick(): void {
    if (this.destroyed) return;
    const now = this.scheduler.now();
    for (const [id, motion] of this.active) {
      const finished = now - motion.startedAt >= motion.durationMs;
      if (!finished) continue;
      this.active.delete(id);
      this.complete(motion);
    }
    this.render();
    if (this.active.size > 0) this.scheduleFrame();
  }

  private motionPalette(input: EdgeMotionInput): string[] {
    if (input.themeColors?.length) return input.themeColors;
    if (input.themeColor) return [input.themeColor];
    return [EDGE_VISUAL_STYLE.bodyColor];
  }

  private render(): void {
    if (this.destroyed || this.rendering) return;
    this.rendering = true;
    try {
      if (!this.ensureMotionStyle()) return;
      const now = this.scheduler.now();
      const features: GeoJSON.Feature<GeoJSON.LineString>[] = [];
      for (const motion of this.active.values()) {
        if (
          this.visibleEdgeIds !== null &&
          !this.visibleEdgeIds.has(motion.input.id)
        ) {
          continue;
        }
        const progress = this.progressAt(motion, now);
        if (progress <= 0) continue;
        const palette = this.motionPalette(motion.input);
        const fadenType = motion.input.fadenType ?? FADEN_OUT_RENDER_KIND;
        // Path (controls, samples, arcs, seams) is immutable on ActiveMotion.
        // Per frame: progress clip + GeoJSON feature shells only.
        const segments = clipThreadPathByProgress(motion.pathState, progress);
        for (let strand = 0; strand < segments.length; strand += 1) {
          const segment = segments[strand];
          if (segment.coordinates.length < 2) continue;
          features.push({
            type: "Feature",
            geometry: {
              type: "LineString",
              coordinates: segment.coordinates,
            },
            properties: {
              id: motion.input.id,
              kind: motion.input.kind,
              fadenType,
              fadenSubjectId: motion.input.fadenSubjectId ?? null,
              themeColor: segment.color,
              themeColors: palette,
              themeStrand: strand,
              themeStrandCount: segments.length,
              opacity: clamp01(motion.input.opacity ?? 1),
              phase: motion.phase,
              progress,
            },
          });
        }
      }
      const source = this.map.getSource(EDGE_MOTION_SOURCE) as
        | GeoJSONSource
        | undefined;
      source?.setData({ type: "FeatureCollection", features });
      this.applyStaticFilters();
    } catch {
      // A style replacement can briefly make sources/layers unavailable.
    } finally {
      this.rendering = false;
    }
  }

  private hasMotionStyle(): boolean {
    if (!this.map.getSource(EDGE_MOTION_SOURCE)) return false;
    return EDGE_MOTION_LAYER_IDS.every((layerId) =>
      Boolean(this.map.getLayer(layerId)),
    );
  }

  private ensureMotionStyle(): boolean {
    if (!this.map.getSource(EDGE_MOTION_SOURCE)) {
      this.map.addSource(EDGE_MOTION_SOURCE, {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });
    }

    const firstSymbolId = this.map
      .getStyle()
      ?.layers?.find((layer) => layer.type === "symbol")?.id;
    const commonLayout: LineLayerSpecification["layout"] = {
      "line-join": "round",
      "line-cap": "round",
    };

    for (const variant of EDGE_THREAD_VARIANTS) {
      const shadowId = motionShadowLayerId(variant.fadenType);
      const bodyId = motionMainLayerId(variant.fadenType);
      const highlightId = motionHighlightLayerId(variant.fadenType);
      const filter = [
        "==",
        ["get", "fadenType"],
        variant.fadenType,
      ] as LineLayerSpecification["filter"];
      const dashArray = [...variant.dashArray] as [number, number];
      const bodyWidth = variant.width;
      if (!this.map.getLayer(shadowId)) {
        const shadowPaint: LineLayerSpecification["paint"] = {
          "line-color": EDGE_VISUAL_STYLE.shadowColor,
          "line-width": bodyWidth + EDGE_VISUAL_STYLE.shadowWidthExtra,
          "line-blur": EDGE_VISUAL_STYLE.shadowBlur,
          "line-opacity": [
            "*",
            ["coalesce", ["to-number", ["get", "opacity"]], 0],
            EDGE_VISUAL_STYLE.shadowOpacityFactor,
          ],
        };
        if (!variant.shadowContinuous) {
          shadowPaint["line-dasharray"] = dashArray;
        }
        this.map.addLayer(
          {
            id: shadowId,
            type: "line",
            source: EDGE_MOTION_SOURCE,
            filter,
            layout: commonLayout,
            paint: gradeMapPaint(shadowPaint),
          },
          this.map.getLayer(bodyId)
            ? bodyId
            : this.map.getLayer(highlightId)
              ? highlightId
              : firstSymbolId,
        );
      }
      if (!this.map.getLayer(bodyId)) {
        const bodyPaint: LineLayerSpecification["paint"] = {
          "line-color": gradeColorExpression([
            "coalesce",
            ["get", "themeColor"],
            variant.fallbackColor,
          ]),
          "line-width": bodyWidth,
          "line-opacity": ["coalesce", ["to-number", ["get", "opacity"]], 0],
        };
        if (!variant.bodyContinuous) {
          bodyPaint["line-dasharray"] = dashArray;
        }
        this.map.addLayer(
          {
            id: bodyId,
            type: "line",
            source: EDGE_MOTION_SOURCE,
            filter,
            layout: commonLayout,
            paint: gradeMapPaint(bodyPaint),
          },
          this.map.getLayer(highlightId) ? highlightId : firstSymbolId,
        );
      }
      if (!this.map.getLayer(highlightId)) {
        this.map.addLayer(
          {
            id: highlightId,
            type: "line",
            source: EDGE_MOTION_SOURCE,
            filter,
            layout: commonLayout,
            paint: {
              "line-color": gradeMapCanvasColor(
                EDGE_VISUAL_STYLE.highlightColor,
              ),
              "line-width": Math.max(
                0.55,
                bodyWidth * EDGE_VISUAL_STYLE.highlightWidthFactor,
              ),
              "line-opacity": [
                "*",
                ["coalesce", ["to-number", ["get", "opacity"]], 0],
                EDGE_VISUAL_STYLE.highlightOpacityFactor,
              ],
              // Braid/fiber rhythm lives primarily on the narrow highlight.
              "line-dasharray": dashArray,
            },
          },
          firstSymbolId,
        );
      }
    }
    return true;
  }

  private hiddenIds(): string[] {
    return Array.from(
      new Set([...this.active.keys(), ...this.releaseSuppressed]),
    ).sort();
  }

  private applyStaticFilters(): void {
    const hiddenIds = this.hiddenIds();
    for (const layerId of EDGE_THREAD_LAYER_IDS) {
      if (!this.map.getLayer(layerId)) continue;
      if (!this.baseFilters.has(layerId)) {
        this.baseFilters.set(layerId, this.map.getFilter(layerId) ?? null);
      }
      const baseFilter = this.baseFilters.get(layerId) ?? null;
      const hideFilter = ["!", ["in", ["get", "id"], ["literal", hiddenIds]]];
      const nextFilter =
        hiddenIds.length === 0
          ? baseFilter
          : baseFilter
            ? (["all", baseFilter, hideFilter] as unknown as LayerFilter)
            : (hideFilter as unknown as LayerFilter);
      const signature = JSON.stringify(nextFilter ?? null);
      if (this.appliedFilterSignatures.get(layerId) === signature) continue;
      this.map.setFilter(layerId, nextFilter);
      this.appliedFilterSignatures.set(layerId, signature);
    }
  }

  private restoreStaticFilters(): void {
    for (const [layerId, filter] of this.baseFilters) {
      try {
        if (this.map.getLayer(layerId)) this.map.setFilter(layerId, filter);
      } catch {
        // Map may already be tearing down.
      }
    }
    this.appliedFilterSignatures.clear();
  }
}
