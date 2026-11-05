import type { Map as MapLibreMap, Marker, MarkerOptions } from "maplibre-gl";
import { hasRenderableMapPosition } from "$lib/map/coordinates";
import type { MapEntityViewModel } from "$lib/map/types";
import type { WeaveEntity } from "$lib/map/weaveTheme";
import { getMapMarkerScale } from "$lib/map/markerScale";
import {
  householdMemberOffset,
  householdPageCapacity,
  indexHouseholdStacks,
  type HouseholdIndex,
  type HouseholdStack,
} from "$lib/map/householdStack";
import "./markers.css";
import { garnrolleIcon } from "$lib/ui/icons";
import { weaveRuntime, type WeaveRuntime } from "./weaveRuntime";

export {
  projectMapMarkerViewsForWeave,
  projectMarkersForWeave,
} from "./weaveRuntime";

export function diffSearchMatchIds(
  previous: ReadonlySet<string>,
  next: ReadonlySet<string>,
): { added: string[]; removed: string[] } {
  const added: string[] = [];
  const removed: string[] = [];

  for (const id of next) {
    if (!previous.has(id)) added.push(id);
  }
  for (const id of previous) {
    if (!next.has(id)) removed.push(id);
  }

  return { added, removed };
}

export type MarkerConstructor = new (options?: MarkerOptions) => Marker;

/** Canonical MapLibre marker anchor for nodes, centers, and Garnrollen. */
export const MARKER_GEO_ANCHOR = "center" as const;
const WEAVE_DETAIL_ZOOM = 13.5;
const MARKER_SCALE_WRITE_EPSILON = 0.001;
export const NATIVE_ENTITY_LAYER_MIN_COUNT = 100;
const FULL_DOM_MARKER_LIMIT = NATIVE_ENTITY_LAYER_MIN_COUNT;
export const NATIVE_ENTITY_SOURCE_ID = "commonthing-map-entities";
export const NATIVE_ENTITY_LAYER_ID = "commonthing-map-entities-body";
const NATIVE_ENTITY_DEFAULT_COLOR = "#76523d";
const NATIVE_ENTITY_CENTER_COLOR = "#d7b684";
const NATIVE_ENTITY_SEARCH_COLOR = "#005fcc";
const NATIVE_ENTITY_SELECTED_COLOR = "#c06a32";
/**
 * Fixed Web-Mercator bucket zoom for the DOM-marker compatibility layer.
 * Localized viewports stay bounded without rebuilding the index for every
 * camera zoom; wide views deliberately fall back to the proven full scan.
 */
const MARKER_SPATIAL_INDEX_ZOOM = 14;
const MARKER_SPATIAL_TILE_COUNT = 1 << MARKER_SPATIAL_INDEX_ZOOM;
const MARKER_SPATIAL_MAX_LAT = 85.05112878;
/**
 * A very wide/invalid viewport is cheaper and safer on the proven full-scan
 * path than by walking a huge number of empty spatial buckets.
 */
const MARKER_SPATIAL_QUERY_BUCKET_LIMIT = 4096;

type MarkerSpatialIndex = {
  buckets: Map<number, number[]>;
  indexById: Map<string, number>;
};

type NativeEntityFeatureCollection = {
  type: "FeatureCollection";
  features: Array<{
    type: "Feature";
    id: string;
    geometry: { type: "Point"; coordinates: [number, number] };
    properties: {
      id: string;
      title: string;
      entityType: MapEntityViewModel["type"];
      color: string;
      locationState: string;
    };
  }>;
};

type NativeLayerClickEvent = {
  features?: Array<{ properties?: Record<string, unknown> | null }>;
};

type MapObjectScaleOwnership = {
  owners: Set<symbol>;
  previousValue: string;
  previousPriority: string;
};
const MAP_OBJECT_SCALE_OWNERS = new WeakMap<
  HTMLElement,
  MapObjectScaleOwnership
>();

function markerSpatialTileX(lon: number): number {
  const raw = Math.floor(((lon + 180) / 360) * MARKER_SPATIAL_TILE_COUNT);
  return Math.max(0, Math.min(MARKER_SPATIAL_TILE_COUNT - 1, raw));
}

function markerSpatialTileY(lat: number): number {
  const clamped = Math.max(
    -MARKER_SPATIAL_MAX_LAT,
    Math.min(MARKER_SPATIAL_MAX_LAT, lat),
  );
  const radians = (clamped * Math.PI) / 180;
  const normalized = (1 - Math.asinh(Math.tan(radians)) / Math.PI) / 2;
  const raw = Math.floor(normalized * MARKER_SPATIAL_TILE_COUNT);
  return Math.max(0, Math.min(MARKER_SPATIAL_TILE_COUNT - 1, raw));
}

function markerSpatialBucketKey(lon: number, lat: number): number {
  const x = markerSpatialTileX(lon);
  const y = markerSpatialTileY(lat);
  return y * MARKER_SPATIAL_TILE_COUNT + x;
}

function buildMarkerSpatialIndex(
  points: readonly MapEntityViewModel[],
): MarkerSpatialIndex {
  const buckets = new Map<number, number[]>();
  const indexById = new Map<string, number>();

  points.forEach((item, index) => {
    if (!hasRenderableMapPosition(item)) return;
    const key = markerSpatialBucketKey(item.lon, item.lat);
    const bucket = buckets.get(key);
    if (bucket) bucket.push(index);
    else buckets.set(key, [index]);
    indexById.set(item.id, index);
  });

  return { buckets, indexById };
}

export class NodesOverlay {
  private activeMarkers = new Map<
    string,
    {
      marker: Marker;
      element: HTMLElement;
      item: MapEntityViewModel;
      weaveRoot: HTMLElement | null;
      weaveSignature: string | null;
      cleanup: () => void;
    }
  >();
  private searchMatchIds = new Set<string>();
  private selectedMarkerId: string | null = null;
  private compactWeave = false;
  private markerScale = 1;
  private markerScaleInitialized = false;
  private latestPoints: MapEntityViewModel[] = [];
  private latestShowNodes = true;
  private households: HouseholdIndex = {
    groups: new Map(),
    byAccountId: new Map(),
  };
  /** Most recently focused Garnrolle stays at the front across data refreshes. */
  private householdFront = new Map<string, string>();
  private expandedHouseholdKey: string | null = null;
  private expandedHouseholdPage = 0;
  private householdCloseButton: HTMLButtonElement | null = null;
  private householdPager: HTMLElement | null = null;
  private suppressClickId: string | null = null;
  private suppressClickUntil = 0;
  private markerSpatialIndex: MarkerSpatialIndex | null = null;
  private nativeLayerEnabled = false;
  private nativeSyncing = false;
  private nativeClickBound = false;
  private garnrollePlacementMode = false;
  private mapScaleContainer: HTMLElement | null = null;
  private readonly mapScaleOwner = Symbol("nodes-overlay-map-scale");
  private readonly handleZoom = () => {
    if (this.map) this.updateZoom(this.map.getZoom());
  };
  private readonly handleHouseholdResize = () => {
    if (!this.expandedHouseholdKey) return;
    const group = this.households.groups.get(this.expandedHouseholdKey);
    if (!group) return;
    this.expandedHouseholdPage = Math.min(
      this.expandedHouseholdPage,
      this.householdPageCount(group) - 1,
    );
    this.syncHouseholdPager(group);
    this.reconcileMarkers(this.latestPoints, this.latestShowNodes, false);
  };
  private readonly handleHouseholdKeyDown = (event: KeyboardEvent) => {
    if (event.key !== "Escape" || !this.expandedHouseholdKey) return;
    event.preventDefault();
    event.stopPropagation();
    this.closeHousehold();
  };
  private readonly handleMoveEnd = () => {
    if (this.nativeLayerEnabled) {
      // Stacked households use DOM markers even in the dense native layer.
      // Reconcile on pan without resending the ordinary GeoJSON features.
      if (this.households.groups.size > 0) {
        this.reconcileMarkers(this.latestPoints, this.latestShowNodes, false);
      }
      return;
    }
    this.reconcileVirtualizedMarkers();
  };
  private readonly handleStyleData = () => {
    if (this.nativeSyncing || !this.latestShowNodes) return;
    if (!this.shouldUseNativeEntityLayer()) return;

    const map = this.map;
    if (
      map?.getSource(NATIVE_ENTITY_SOURCE_ID) &&
      map.getLayer(NATIVE_ENTITY_LAYER_ID)
    ) {
      return;
    }
    this.reconcileMarkers(this.latestPoints, this.latestShowNodes);
  };
  private readonly handleNativeEntityClick = (event: NativeLayerClickEvent) => {
    if (this.garnrollePlacementMode) return;
    const id = event.features?.[0]?.properties?.id;
    if (typeof id !== "string") return;
    const item = this.pointById(id);
    if (item) this.onEntityActivate?.(item);
  };

  constructor(
    private map: MapLibreMap | null,
    private MarkerClass: MarkerConstructor,
    private readonly runtime: WeaveRuntime = weaveRuntime,
    private readonly onEntityActivate?: (item: MapEntityViewModel) => void,
  ) {
    if (this.map && typeof this.map.getContainer === "function") {
      const container = this.map.getContainer();
      this.mapScaleContainer = container;
      let ownership = MAP_OBJECT_SCALE_OWNERS.get(container);
      if (!ownership) {
        ownership = {
          owners: new Set(),
          previousValue: container.style.getPropertyValue("--map-object-scale"),
          previousPriority:
            container.style.getPropertyPriority("--map-object-scale"),
        };
        MAP_OBJECT_SCALE_OWNERS.set(container, ownership);
      }
      ownership.owners.add(this.mapScaleOwner);
      container.addEventListener?.("keydown", this.handleHouseholdKeyDown);
    }
    if (
      this.map &&
      typeof this.map.getZoom === "function" &&
      typeof this.map.on === "function"
    ) {
      this.updateZoom(this.map.getZoom());
      this.map.on("zoom", this.handleZoom);
    }
    if (this.map && typeof this.map.on === "function") {
      this.map.on("moveend", this.handleMoveEnd);
      this.map.on("resize", this.handleHouseholdResize);
      if (this.supportsNativeEntityLayer()) {
        this.map.on("styledata", this.handleStyleData);
        // styledata can fire while MapLibre still reports isStyleLoaded=false.
        // style.load is the terminal retry that guarantees dense data arriving
        // during initial style construction gets another native cut-over chance.
        this.map.on("style.load", this.handleStyleData);
      }
    }
  }

  /** During location picking, markers stay visible as translucent, passive anchors. */
  public setGarnrollePlacementMode(active: boolean): void {
    if (this.garnrollePlacementMode === active) return;
    this.garnrollePlacementMode = active;
    for (const marker of this.activeMarkers.values()) {
      marker.element.inert = active;
    }
    this.syncNativePlacementOpacity();
  }

  private syncNativePlacementOpacity(): void {
    const map = this.map;
    if (
      !map?.getLayer(NATIVE_ENTITY_LAYER_ID) ||
      typeof map.setPaintProperty !== "function"
    )
      return;
    try {
      map.setPaintProperty(
        NATIVE_ENTITY_LAYER_ID,
        "circle-opacity",
        this.garnrollePlacementMode ? 0.22 : 0.92,
      );
      map.setPaintProperty(
        NATIVE_ENTITY_LAYER_ID,
        "circle-stroke-opacity",
        this.garnrollePlacementMode ? 0.22 : 1,
      );
    } catch {
      // A style replacement can remove the layer between lookup and paint.
      // The next native-layer installation uses the current picker mode.
    }
  }

  private supportsNativeEntityLayer(): boolean {
    const map = this.map;
    return Boolean(
      map &&
      typeof map.addSource === "function" &&
      typeof map.getSource === "function" &&
      typeof map.addLayer === "function" &&
      typeof map.getLayer === "function" &&
      typeof map.queryRenderedFeatures === "function" &&
      typeof map.setFeatureState === "function",
    );
  }

  private shouldUseNativeEntityLayer(
    points: readonly MapEntityViewModel[] = this.latestPoints,
  ): boolean {
    return (
      points.length > NATIVE_ENTITY_LAYER_MIN_COUNT &&
      this.supportsNativeEntityLayer()
    );
  }

  private pointById(id: string): MapEntityViewModel | undefined {
    const index = this.markerSpatialIndex?.indexById.get(id);
    if (index !== undefined) return this.latestPoints[index];
    return this.latestPoints.find((item) => item.id === id);
  }

  private nativeEntityColor(item: MapEntityViewModel): string {
    if (item.type === "webgemeindezentrum") return NATIVE_ENTITY_CENTER_COLOR;
    if (item.type === "node") {
      return item.weave?.primaryThemeColor ?? NATIVE_ENTITY_DEFAULT_COLOR;
    }
    return NATIVE_ENTITY_DEFAULT_COLOR;
  }

  private nativeFeatureCollection(): NativeEntityFeatureCollection {
    if (!this.latestShowNodes)
      return { type: "FeatureCollection", features: [] };

    const features: NativeEntityFeatureCollection["features"] = [];
    for (const item of this.latestPoints) {
      if (!hasRenderableMapPosition(item)) continue;
      // Exactly co-located accounts are rendered as one interactive DOM stack,
      // even when the rest of the scene uses the dense native circle layer.
      if (this.households.byAccountId.has(item.id)) continue;
      features.push({
        type: "Feature",
        id: item.id,
        geometry: {
          type: "Point",
          coordinates: [item.lon, item.lat],
        },
        properties: {
          id: item.id,
          title: item.title,
          entityType: item.type,
          color: this.nativeEntityColor(item),
          locationState:
            item.type === "webgemeindezentrum" ? item.location_state : "",
        },
      });
    }
    return { type: "FeatureCollection", features };
  }

  private syncNativeEntityLayer(): boolean {
    const map = this.map;
    if (!map || !this.supportsNativeEntityLayer() || this.nativeSyncing)
      return false;

    // Do not gate this on map.isStyleLoaded(). MapLibre can emit style.load
    // while that broader predicate is still false because requested basemap
    // sources/tiles are settling. addSource/addLayer are valid after style.load;
    // the catch below preserves the DOM fallback for a genuine style race.
    this.nativeSyncing = true;
    try {
      const data = this.nativeFeatureCollection();
      let source = map.getSource(NATIVE_ENTITY_SOURCE_ID) as
        | { setData?: (data: NativeEntityFeatureCollection) => void }
        | undefined;
      let sourceCreated = false;
      if (!source) {
        map.addSource(NATIVE_ENTITY_SOURCE_ID, {
          type: "geojson",
          data,
        });
        sourceCreated = true;
        source = map.getSource(NATIVE_ENTITY_SOURCE_ID) as
          | { setData?: (data: NativeEntityFeatureCollection) => void }
          | undefined;
      }

      if (!map.getLayer(NATIVE_ENTITY_LAYER_ID)) {
        map.addLayer({
          id: NATIVE_ENTITY_LAYER_ID,
          type: "circle",
          source: NATIVE_ENTITY_SOURCE_ID,
          paint: {
            "circle-radius": [
              "interpolate",
              ["linear"],
              ["zoom"],
              10,
              [
                "case",
                ["==", ["get", "entityType"], "webgemeindezentrum"],
                6,
                4,
              ],
              14,
              [
                "case",
                ["==", ["get", "entityType"], "webgemeindezentrum"],
                10,
                7,
              ],
              18,
              [
                "case",
                ["==", ["get", "entityType"], "webgemeindezentrum"],
                14,
                11,
              ],
            ],
            "circle-color": ["get", "color"],
            "circle-opacity": this.garnrollePlacementMode ? 0.22 : 0.92,
            "circle-stroke-opacity": this.garnrollePlacementMode ? 0.22 : 1,
            "circle-stroke-color": [
              "case",
              ["boolean", ["feature-state", "selected"], false],
              NATIVE_ENTITY_SELECTED_COLOR,
              ["boolean", ["feature-state", "searchMatch"], false],
              NATIVE_ENTITY_SEARCH_COLOR,
              ["==", ["get", "entityType"], "webgemeindezentrum"],
              "#664835",
              "#f4e5cd",
            ],
            "circle-stroke-width": [
              "case",
              [
                "any",
                ["boolean", ["feature-state", "selected"], false],
                ["boolean", ["feature-state", "searchMatch"], false],
              ],
              3,
              1.5,
            ],
          },
        });
      }
      if (!sourceCreated) source?.setData?.(data);
      if (!this.nativeClickBound) {
        map.on("click", NATIVE_ENTITY_LAYER_ID, this.handleNativeEntityClick);
        this.nativeClickBound = true;
      }
      this.reapplyNativeFeatureStates();
      return Boolean(source && map.getLayer(NATIVE_ENTITY_LAYER_ID));
    } catch {
      // Style replacement can invalidate sources between readiness and mutation.
      // The caller keeps the proven DOM virtualization path until a later
      // styledata/update can establish the complete native source + layer.
      return false;
    } finally {
      this.nativeSyncing = false;
    }
  }

  private setNativeFeatureState(
    id: string,
    state: { selected?: boolean; searchMatch?: boolean },
  ) {
    const map = this.map;
    if (!this.nativeLayerEnabled || !map || !this.supportsNativeEntityLayer()) {
      return;
    }
    try {
      map.setFeatureState({ source: NATIVE_ENTITY_SOURCE_ID, id }, state);
    } catch {
      // Style/source transitions are rehydrated from canonical state later.
    }
  }

  private reapplyNativeFeatureStates() {
    if (this.selectedMarkerId !== null) {
      this.setNativeFeatureState(this.selectedMarkerId, { selected: true });
    }
    for (const id of this.searchMatchIds) {
      this.setNativeFeatureState(id, { searchMatch: true });
    }
  }

  private clearNativeEntityData() {
    const map = this.map;
    if (!map || !this.supportsNativeEntityLayer()) return;
    try {
      const source = map.getSource(NATIVE_ENTITY_SOURCE_ID) as
        | { setData?: (data: NativeEntityFeatureCollection) => void }
        | undefined;
      source?.setData?.({ type: "FeatureCollection", features: [] });
    } catch {
      // Missing source during a style transition already means no stale points.
    }
  }

  public hasNativeEntityAt(point: { x: number; y: number }): boolean {
    const map = this.map;
    if (
      !this.nativeLayerEnabled ||
      !map ||
      !this.supportsNativeEntityLayer() ||
      !map.getLayer(NATIVE_ENTITY_LAYER_ID)
    ) {
      return false;
    }
    try {
      return (
        map.queryRenderedFeatures([point.x, point.y], {
          layers: [NATIVE_ENTITY_LAYER_ID],
        }).length > 0
      );
    } catch {
      return false;
    }
  }

  private householdFrontItem(group: HouseholdStack) {
    const preferredId = this.householdFront.get(group.key);
    return (
      group.members.find((member) => member.id === preferredId) ??
      group.members[group.members.length - 1]
    );
  }

  private isHouseholdSearchMatch(item: MapEntityViewModel): boolean {
    const group = this.households.byAccountId.get(item.id);
    if (!group || group.key === this.expandedHouseholdKey) {
      // An individual Garnrolle must not inherit its neighbours' search hit.
      return this.searchMatchIds.has(item.id);
    }
    return group.members.some((member) => this.searchMatchIds.has(member.id));
  }

  private householdPageCapacity() {
    return householdPageCapacity(
      this.mapScaleContainer?.clientWidth ?? 320,
      this.mapScaleContainer?.clientHeight ?? 600,
    );
  }

  private householdPageCount(group: HouseholdStack) {
    return Math.max(
      1,
      Math.ceil(group.members.length / this.householdPageCapacity()),
    );
  }

  private householdPageMembers(group: HouseholdStack) {
    const capacity = this.householdPageCapacity();
    const start = this.expandedHouseholdPage * capacity;
    return group.members.slice(start, start + capacity);
  }

  private isolateHouseholdControl(element: HTMLElement) {
    // MapLibre and the map's node-composition longpress must not interpret
    // operating the pager/close button as a map pan or a place gesture.
    for (const type of ["pointerdown", "mousedown", "touchstart", "dblclick"]) {
      element.addEventListener(type, (event) => event.stopPropagation());
    }
  }

  private syncHouseholdPager(group: HouseholdStack) {
    const pageCount = this.householdPageCount(group);
    if (pageCount < 2 || !this.mapScaleContainer) {
      this.householdPager?.remove();
      this.householdPager = null;
      return;
    }
    if (!this.householdPager) {
      const pager = document.createElement("nav");
      pager.className = "map-household-pager";
      pager.dataset.testid = "household-page-controls";
      this.isolateHouseholdControl(pager);
      pager.setAttribute(
        "aria-label",
        "Weitere Garnrollen der Hausgemeinschaft",
      );
      const previous = document.createElement("button");
      previous.type = "button";
      previous.textContent = "Zurück";
      previous.addEventListener("click", (event) => {
        event.stopPropagation();
        this.turnHouseholdPage(-1);
      });
      const count = document.createElement("span");
      count.className = "map-household-pager__count";
      count.setAttribute("aria-live", "polite");
      const next = document.createElement("button");
      next.type = "button";
      next.textContent = "Weiter";
      next.addEventListener("click", (event) => {
        event.stopPropagation();
        this.turnHouseholdPage(1);
      });
      pager.append(previous, count, next);
      this.mapScaleContainer.append(pager);
      this.householdPager = pager;
    }
    const previous = this.householdPager.children[0] as HTMLButtonElement;
    const count = this.householdPager.children[1] as HTMLElement;
    const next = this.householdPager.children[2] as HTMLButtonElement;
    previous.disabled = this.expandedHouseholdPage === 0;
    next.disabled = this.expandedHouseholdPage >= pageCount - 1;
    count.textContent = `Seite ${this.expandedHouseholdPage + 1} von ${pageCount}`;
  }

  private turnHouseholdPage(direction: -1 | 1) {
    if (!this.expandedHouseholdKey) return;
    const group = this.households.groups.get(this.expandedHouseholdKey);
    if (!group) return;
    const next = Math.max(
      0,
      Math.min(
        this.householdPageCount(group) - 1,
        this.expandedHouseholdPage + direction,
      ),
    );
    if (next === this.expandedHouseholdPage) return;
    this.expandedHouseholdPage = next;
    this.syncHouseholdPager(group);
    this.reconcileMarkers(this.latestPoints, this.latestShowNodes, false);
    const first = this.householdPageMembers(group)[0];
    if (first) this.getActiveMarker(first.id)?.element.focus?.();
  }

  private syncHouseholdAppearance(
    element: HTMLElement,
    item: MapEntityViewModel,
  ) {
    if (item.type !== "garnrolle") return;
    const group = this.households.byAccountId.get(item.id);
    const expanded = Boolean(group && group.key === this.expandedHouseholdKey);
    element.classList.toggle(
      "marker-household-stack",
      Boolean(group) && !expanded,
    );
    element.classList.toggle("marker-household-expanded", expanded);
    element.classList.toggle(
      "search-highlight",
      this.isHouseholdSearchMatch(item),
    );
    if (this.isHouseholdSearchMatch(item)) element.dataset.searchMatch = "true";
    else delete element.dataset.searchMatch;

    if (!group) {
      delete element.dataset.householdCount;
      delete element.dataset.householdMember;
      return;
    }

    element.dataset.householdMember = "true";
    element.dataset.householdCount = String(group.members.length);
    const label = this.runtime.label(item);
    element.setAttribute(
      "aria-label",
      expanded
        ? `${label}; Hausgemeinschaft, ${group.members.length} Garnrollen; einzeln auswählbar`
        : `${label}; ${group.members.length} Garnrollen an diesem Ort. Gedrückt halten, doppelklicken oder Alt+Enter für die Hausgemeinschaft`,
    );
    element.title = expanded
      ? item.title
      : `Hausgemeinschaft mit ${group.members.length} Garnrollen – gedrückt halten oder doppelklicken`;
    if (expanded) {
      const membersOnPage = this.householdPageMembers(group);
      const index = membersOnPage.findIndex((member) => member.id === item.id);
      const viewportWidth = this.mapScaleContainer?.clientWidth ?? 320;
      const offset = householdMemberOffset(
        index,
        membersOnPage.length,
        viewportWidth,
      );
      element.style.translate = `${offset.x}px ${offset.y}px`;
      element.style.zIndex = String(50 + index);
    } else {
      element.style.translate = "";
      element.style.zIndex = "30";
    }
  }

  /** MapLibre's geographic anchors and the underlying Fäden never move. */
  private openHousehold(key: string) {
    const group = this.households.groups.get(key);
    if (!group || !this.map) return;
    if (this.expandedHouseholdKey !== key) {
      const chosenId = this.householdFrontItem(group).id;
      const firstIndex = group.members.findIndex(
        (item) => item.id === chosenId,
      );
      this.expandedHouseholdPage = Math.max(
        0,
        Math.floor(firstIndex / this.householdPageCapacity()),
      );
    }
    this.expandedHouseholdKey = key;
    const map = this.map;
    const zoom = typeof map.getZoom === "function" ? map.getZoom() : 15;
    if (typeof map.easeTo === "function") {
      map.easeTo({
        center: [group.members[0].lon, group.members[0].lat],
        zoom: Number.isFinite(zoom) ? Math.min(18, Math.max(16, zoom + 1)) : 16,
        duration: 350,
      });
    }
    if (!this.householdCloseButton && this.mapScaleContainer) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "map-household-close";
      button.dataset.testid = "close-household-view";
      this.isolateHouseholdControl(button);
      button.addEventListener("click", (event) => {
        event.stopPropagation();
        this.closeHousehold();
      });
      this.mapScaleContainer.append(button);
      this.householdCloseButton = button;
    }
    if (this.householdCloseButton) {
      this.householdCloseButton.textContent = `Hausgemeinschaft · ${group.members.length} Garnrollen · Schließen ×`;
      this.householdCloseButton.setAttribute(
        "aria-label",
        "Hausgemeinschaftssicht schließen",
      );
    }
    this.syncHouseholdPager(group);
    this.reconcileMarkers(this.latestPoints, this.latestShowNodes, false);
  }

  private closeHousehold() {
    const group = this.expandedHouseholdKey
      ? this.households.groups.get(this.expandedHouseholdKey)
      : undefined;
    this.expandedHouseholdKey = null;
    this.expandedHouseholdPage = 0;
    this.householdCloseButton?.remove();
    this.householdCloseButton = null;
    this.householdPager?.remove();
    this.householdPager = null;
    this.reconcileMarkers(this.latestPoints, this.latestShowNodes, false);
    if (group)
      this.getActiveMarker(
        this.householdFrontItem(group).id,
      )?.element.focus?.();
  }

  private installHouseholdGesture(
    element: HTMLElement,
    id: string,
  ): () => void {
    let timer: ReturnType<typeof setTimeout> | undefined;
    let origin: { x: number; y: number } | null = null;
    const cancel = () => {
      if (timer !== undefined) clearTimeout(timer);
      timer = undefined;
      origin = null;
    };
    const groupForStack = () => {
      if (this.garnrollePlacementMode) return null;
      const group = this.households.byAccountId.get(id);
      return group &&
        this.expandedHouseholdKey !== group.key &&
        this.householdFrontItem(group).id === id
        ? group
        : null;
    };
    const pointerDown = (event: PointerEvent) => {
      if (event.button !== 0) return;
      const group = groupForStack();
      if (!group) return;
      cancel();
      origin = { x: event.clientX, y: event.clientY };
      const key = group.key;
      timer = setTimeout(() => {
        timer = undefined;
        origin = null;
        if (!groupForStack() || !this.latestShowNodes) return;
        this.suppressClickId = id;
        this.suppressClickUntil = Date.now() + 900;
        this.openHousehold(key);
      }, 650);
    };
    const pointerMove = (event: PointerEvent) => {
      if (!origin) return;
      const dx = event.clientX - origin.x;
      const dy = event.clientY - origin.y;
      if (dx * dx + dy * dy > 100) cancel();
    };
    const activate = (event: Event) => {
      const group = groupForStack();
      if (!group) return;
      event.preventDefault();
      event.stopPropagation();
      cancel();
      this.openHousehold(group.key);
    };
    const contextMenu = (event: MouseEvent) => {
      // iOS Safari can dispatch contextmenu after the longpress timer has
      // already expanded the house. Suppress the browser menu in both states;
      // only the still-collapsed stack may open the household.
      if (!this.households.byAccountId.has(id)) return;
      event.preventDefault();
      if (groupForStack()) activate(event);
    };
    const doubleClick = (event: MouseEvent) => activate(event);
    const keyDown = (event: KeyboardEvent) => {
      if (event.altKey && event.key === "Enter") activate(event);
    };
    const click = (event: MouseEvent) => {
      if (this.suppressClickId === id && Date.now() < this.suppressClickUntil) {
        this.suppressClickId = null;
        event.preventDefault();
        event.stopPropagation();
        return;
      }
      const group = this.households.byAccountId.get(id);
      if (group) this.householdFront.set(group.key, id);
    };
    element.addEventListener("pointerdown", pointerDown);
    element.addEventListener("pointermove", pointerMove);
    element.addEventListener("pointerup", cancel);
    element.addEventListener("pointercancel", cancel);
    element.addEventListener("pointerleave", cancel);
    element.addEventListener("contextmenu", contextMenu);
    element.addEventListener("dblclick", doubleClick);
    element.addEventListener("keydown", keyDown);
    element.addEventListener("click", click);
    return () => {
      cancel();
      element.removeEventListener("pointerdown", pointerDown);
      element.removeEventListener("pointermove", pointerMove);
      element.removeEventListener("pointerup", cancel);
      element.removeEventListener("pointercancel", cancel);
      element.removeEventListener("pointerleave", cancel);
      element.removeEventListener("contextmenu", contextMenu);
      element.removeEventListener("dblclick", doubleClick);
      element.removeEventListener("keydown", keyDown);
      element.removeEventListener("click", click);
    };
  }

  private syncWeaveDetail(root: HTMLElement) {
    root.classList.toggle("woven-node--compact", this.compactWeave);
    root.dataset.weaveDetail = this.compactWeave ? "compact" : "detail";
  }

  private syncMapObjectScale() {
    if (!this.mapScaleContainer) return;
    this.mapScaleContainer.style.setProperty(
      "--map-object-scale",
      this.markerScale.toFixed(3),
    );
  }

  public updateZoom(zoom: number) {
    const compact = zoom < WEAVE_DETAIL_ZOOM;
    const nextMarkerScale = getMapMarkerScale(zoom);
    const compactChanged = compact !== this.compactWeave;
    const markerScaleChanged =
      !this.markerScaleInitialized ||
      Math.abs(nextMarkerScale - this.markerScale) >=
        MARKER_SCALE_WRITE_EPSILON;
    if (!compactChanged && !markerScaleChanged) return;

    if (compactChanged) {
      this.compactWeave = compact;
      for (const { weaveRoot } of this.activeMarkers.values()) {
        if (weaveRoot) this.syncWeaveDetail(weaveRoot);
      }
    }
    if (markerScaleChanged) {
      this.markerScale = nextMarkerScale;
      if (!this.nativeLayerEnabled || this.activeMarkers.size > 0) {
        this.syncMapObjectScale();
      }
      this.markerScaleInitialized = true;
    }
  }

  private getMarkerCategory(
    type: MapEntityViewModel["type"],
  ): "node" | "account" | "webgemeindezentrum" {
    if (type === "garnrolle") return "account";
    if (type === "webgemeindezentrum") return "webgemeindezentrum";
    return "node";
  }

  private shouldVirtualizeMarkers(
    points: readonly MapEntityViewModel[] = this.latestPoints,
  ): boolean {
    return points.length > FULL_DOM_MARKER_LIMIT;
  }

  private rebuildMarkerSpatialIndex(points: readonly MapEntityViewModel[]) {
    this.markerSpatialIndex = this.shouldVirtualizeMarkers(points)
      ? buildMarkerSpatialIndex(points)
      : null;
  }

  private spatialCandidateIndexes(
    viewportBounds: ReturnType<MapLibreMap["getBounds"]>,
  ): number[] | null {
    const index = this.markerSpatialIndex;
    if (!index) return null;
    if (
      typeof viewportBounds.getWest !== "function" ||
      typeof viewportBounds.getEast !== "function" ||
      typeof viewportBounds.getSouth !== "function" ||
      typeof viewportBounds.getNorth !== "function"
    ) {
      return null;
    }

    const west = viewportBounds.getWest();
    const east = viewportBounds.getEast();
    const south = viewportBounds.getSouth();
    const north = viewportBounds.getNorth();
    if (
      ![west, east, south, north].every(Number.isFinite) ||
      west < -180 ||
      west > 180 ||
      east < -180 ||
      east > 180 ||
      south > north
    ) {
      return null;
    }

    const northY = markerSpatialTileY(north);
    const southY = markerSpatialTileY(south);
    const minY = Math.min(northY, southY);
    const maxY = Math.max(northY, southY);
    const xRanges: Array<readonly [number, number]> =
      west <= east
        ? [[markerSpatialTileX(west), markerSpatialTileX(east)]]
        : [
            [markerSpatialTileX(west), MARKER_SPATIAL_TILE_COUNT - 1],
            [0, markerSpatialTileX(east)],
          ];

    const ySpan = maxY - minY + 1;
    const bucketCount = xRanges.reduce(
      (count, [minX, maxX]) => count + (maxX - minX + 1) * ySpan,
      0,
    );
    if (bucketCount > MARKER_SPATIAL_QUERY_BUCKET_LIMIT) return null;

    const candidateIndexes = new Set<number>();
    for (const [minX, maxX] of xRanges) {
      for (let y = minY; y <= maxY; y += 1) {
        for (let x = minX; x <= maxX; x += 1) {
          const bucket = index.buckets.get(y * MARKER_SPATIAL_TILE_COUNT + x);
          if (!bucket) continue;
          for (const itemIndex of bucket) candidateIndexes.add(itemIndex);
        }
      }
    }

    if (this.selectedMarkerId !== null) {
      const selectedIndex = index.indexById.get(this.selectedMarkerId);
      if (selectedIndex !== undefined) candidateIndexes.add(selectedIndex);
    }
    for (const id of this.searchMatchIds) {
      const searchIndex = index.indexById.get(id);
      if (searchIndex !== undefined) candidateIndexes.add(searchIndex);
    }

    return Array.from(candidateIndexes).sort((left, right) => left - right);
  }

  private reconcileVirtualizedMarkers() {
    if (this.nativeLayerEnabled) return;
    if (this.shouldVirtualizeMarkers()) {
      this.reconcileMarkers(this.latestPoints, this.latestShowNodes);
    }
  }

  private syncWebgemeindezentrumAppearance(
    element: HTMLElement,
    item: MapEntityViewModel,
  ) {
    if (item.type !== "webgemeindezentrum") return;
    const visual = element.children[0] as HTMLElement | undefined;
    if (!visual) return;
    const borderStyle = item.location_state === "confirmed" ? "" : "dashed";
    if (visual.style.borderStyle !== borderStyle) {
      visual.style.borderStyle = borderStyle;
    }
  }

  private reconcileMarkers(
    points: MapEntityViewModel[],
    showNodes: boolean,
    syncNativeData = true,
  ): void {
    const nativeRequested =
      showNodes && this.shouldUseNativeEntityLayer(points);
    let useNativeLayer = false;
    if (nativeRequested) {
      if (syncNativeData) {
        // State must be temporarily enabled so selected/search feature-state can
        // be reapplied while the newly created source/layer is synchronized.
        this.nativeLayerEnabled = true;
        useNativeLayer = this.syncNativeEntityLayer();
      } else {
        useNativeLayer =
          this.nativeLayerEnabled &&
          Boolean(this.map?.getLayer(NATIVE_ENTITY_LAYER_ID));
      }
    }
    this.nativeLayerEnabled = useNativeLayer;
    if (!nativeRequested) this.clearNativeEntityData();

    if (!showNodes) {
      this.activeMarkers.forEach(({ cleanup }) => cleanup());
      this.activeMarkers.clear();
      if (this.expandedHouseholdKey) this.closeHousehold();
      return;
    }

    const map = this.map;
    if (!map) return;

    const shouldVirtualize =
      !useNativeLayer && this.shouldVirtualizeMarkers(points);
    const viewportBounds =
      (shouldVirtualize || useNativeLayer) &&
      typeof map.getBounds === "function"
        ? map.getBounds()
        : null;
    const spatialIndexes =
      shouldVirtualize && viewportBounds
        ? this.spatialCandidateIndexes(viewportBounds)
        : null;
    const nativeSelected =
      useNativeLayer &&
      this.selectedMarkerId !== null &&
      !this.households.byAccountId.has(this.selectedMarkerId)
        ? this.pointById(this.selectedMarkerId)
        : undefined;
    const householdMarkers = Array.from(
      this.households.groups.values(),
    ).flatMap((group) =>
      this.expandedHouseholdKey === group.key
        ? this.householdPageMembers(group)
        : [this.householdFrontItem(group)],
    );
    const expandedGroup = this.expandedHouseholdKey
      ? this.households.groups.get(this.expandedHouseholdKey)
      : undefined;
    const visibleExpandedIds = new Set(
      expandedGroup?.members
        ? this.householdPageMembers(expandedGroup).map((member) => member.id)
        : [],
    );
    const candidates = (
      useNativeLayer
        ? [...(nativeSelected ? [nativeSelected] : []), ...householdMarkers]
        : spatialIndexes === null
          ? points
          : spatialIndexes.flatMap((index) => {
              const item = points[index];
              return item ? [item] : [];
            })
    ).filter((item) => {
      const group = this.households.byAccountId.get(item.id);
      if (!group) return true;
      return this.expandedHouseholdKey === group.key
        ? visibleExpandedIds.has(item.id)
        : this.householdFrontItem(group).id === item.id;
    });
    const currentIds = new Set<string>();

    for (const item of candidates) {
      if (!hasRenderableMapPosition(item)) {
        const existing = this.activeMarkers.get(item.id);
        if (existing) {
          existing.cleanup();
          this.activeMarkers.delete(item.id);
        }
        continue;
      }

      if (
        (shouldVirtualize || useNativeLayer) &&
        this.selectedMarkerId !== item.id &&
        !this.searchMatchIds.has(item.id) &&
        viewportBounds &&
        typeof viewportBounds.contains === "function" &&
        !viewportBounds.contains([item.lon, item.lat])
      ) {
        const existing = this.activeMarkers.get(item.id);
        if (existing) {
          existing.cleanup();
          this.activeMarkers.delete(item.id);
        }
        continue;
      }

      currentIds.add(item.id);
      const markerCategory = this.getMarkerCategory(item.type);
      let existing = this.activeMarkers.get(item.id);

      if (
        existing &&
        (existing.element.dataset.markerCategory !== markerCategory ||
          (existing.element.dataset.householdMember === "true") !==
            this.households.byAccountId.has(item.id))
      ) {
        existing.cleanup();
        this.activeMarkers.delete(item.id);
        existing = undefined;
      }

      if (existing) {
        existing.item = item;
        const { marker, element } = existing;
        const lngLat = marker.getLngLat();
        if (
          Math.abs(lngLat.lng - item.lon) > 0.000001 ||
          Math.abs(lngLat.lat - item.lat) > 0.000001
        ) {
          marker.setLngLat([item.lon, item.lat]);
        }
        if (element.title !== item.title) element.title = item.title;
        const ariaLabel = this.runtime.label(item);
        if (element.getAttribute("aria-label") !== ariaLabel) {
          element.setAttribute("aria-label", ariaLabel);
        }
        if (item.type === "webgemeindezentrum") {
          if (element.dataset.locationState !== item.location_state) {
            element.dataset.locationState = item.location_state;
          }
        } else if ("locationState" in element.dataset) {
          delete element.dataset.locationState;
        }
        this.syncWebgemeindezentrumAppearance(element, item);
        if (existing.weaveRoot && item.type !== "garnrolle") {
          existing.weaveSignature = this.runtime.syncRoot(
            existing.weaveRoot,
            item,
            existing.weaveSignature,
          );
        }
        this.syncHouseholdAppearance(element, item);
      } else {
        if (
          this.nativeLayerEnabled &&
          this.activeMarkers.size === 0 &&
          this.markerScaleInitialized
        ) {
          this.syncMapObjectScale();
        }
        const element = document.createElement("button");
        element.type = "button";
        element.className =
          markerCategory === "account"
            ? "map-marker marker-account"
            : markerCategory === "webgemeindezentrum"
              ? "map-marker marker-webgemeindezentrum"
              : "map-marker";

        element.inert = this.garnrollePlacementMode;
        element.dataset.id = item.id;
        element.dataset.markerCategory = markerCategory;
        if (item.type === "webgemeindezentrum") {
          element.dataset.locationState = item.location_state;
        }
        element.dataset.testid = `marker-${item.type}-${item.id}`;

        const visual = document.createElement("span");
        visual.className =
          markerCategory === "account"
            ? "map-marker__visual marker-account__visual"
            : markerCategory === "webgemeindezentrum"
              ? "map-marker__visual marker-webgemeindezentrum__visual"
              : "map-marker__visual marker-node__visual";
        visual.setAttribute("aria-hidden", "true");

        let weaveRoot: HTMLElement | null = null;
        let weaveSignature: string | null = null;
        if (item.type === "garnrolle") {
          if (this.households.byAccountId.has(item.id)) {
            const behind = document.createElement("span");
            behind.className = "marker-household__behind";
            for (let layer = 0; layer < 2; layer += 1) {
              const img = document.createElement("img");
              img.src = garnrolleIcon;
              img.alt = "";
              img.draggable = false;
              img.className = `marker-household__back marker-household__back--${layer + 1}`;
              img.setAttribute("aria-hidden", "true");
              behind.append(img);
            }
            visual.append(behind);
          }
          const icon = document.createElement("img");
          icon.className = "marker-account__icon";
          icon.src = garnrolleIcon;
          icon.alt = "";
          icon.setAttribute("aria-hidden", "true");
          icon.draggable = false;
          visual.append(icon);
        } else {
          const category =
            item.type === "webgemeindezentrum" ? "webgemeindezentrum" : "node";
          const woven = this.runtime.createRoot(item as WeaveEntity, category);
          weaveRoot = woven.root;
          weaveSignature = woven.signature;
          this.syncWeaveDetail(weaveRoot);
          if (item.type === "webgemeindezentrum") {
            const icon = document.createElement("span");
            icon.className = "marker-webgemeindezentrum__icon";
            icon.setAttribute("aria-hidden", "true");
            icon.append(weaveRoot);
            visual.append(icon);
          } else {
            visual.append(weaveRoot);
          }
        }

        const halo = document.createElement("span");
        halo.className = "map-marker__halo";
        halo.setAttribute("aria-hidden", "true");

        element.append(visual, halo);
        this.syncWebgemeindezentrumAppearance(element, item);

        element.setAttribute("aria-label", this.runtime.label(item));
        element.title = item.title;

        // Center anchor: map coordinate, MapLibre pin, and visible knot/spool
        // center share one geographic point so Fäden reach the true midpoint.
        const marker = new this.MarkerClass({
          element,
          anchor: MARKER_GEO_ANCHOR,
        })
          .setLngLat([item.lon, item.lat])
          .addTo(map);
        // MapLibre assigns its generic "Map marker" label in the constructor.
        // Restore the domain-specific woven summary after that synchronous step.
        element.setAttribute("aria-label", this.runtime.label(item));
        this.syncHouseholdAppearance(element, item);

        if (this.isHouseholdSearchMatch(item)) {
          element.classList.add("search-highlight");
          element.dataset.searchMatch = "true";
        }
        if (this.selectedMarkerId === item.id) {
          element.classList.add("is-selected");
          element.dataset.selected = "true";
          element.setAttribute("aria-current", "true");
        }

        const cleanupGesture =
          item.type === "garnrolle" &&
          this.households.byAccountId.has(item.id) &&
          typeof element.addEventListener === "function"
            ? this.installHouseholdGesture(element, item.id)
            : () => {};
        this.activeMarkers.set(item.id, {
          marker,
          element,
          item,
          weaveRoot,
          weaveSignature,
          cleanup: () => {
            cleanupGesture();
            marker.remove();
          },
        });
      }
    }

    for (const [id, { cleanup }] of this.activeMarkers.entries()) {
      if (!currentIds.has(id)) {
        cleanup();
        this.activeMarkers.delete(id);
      }
    }
  }

  public update(points: MapEntityViewModel[], showNodes: boolean): void {
    this.latestPoints = points;
    this.latestShowNodes = showNodes;
    this.households = indexHouseholdStacks(points);
    for (const key of this.householdFront.keys()) {
      if (!this.households.groups.has(key)) this.householdFront.delete(key);
    }
    // When a previously standalone selection becomes a household, preserve
    // the selected account as its front instead of defaulting to the last item.
    if (this.selectedMarkerId !== null) {
      const group = this.households.byAccountId.get(this.selectedMarkerId);
      if (group && !this.householdFront.has(group.key)) {
        this.householdFront.set(group.key, this.selectedMarkerId);
      }
    }
    if (this.expandedHouseholdKey) {
      const group = this.households.groups.get(this.expandedHouseholdKey);
      if (!group) {
        this.expandedHouseholdKey = null;
        this.expandedHouseholdPage = 0;
        this.householdCloseButton?.remove();
        this.householdCloseButton = null;
        this.householdPager?.remove();
        this.householdPager = null;
      } else {
        this.expandedHouseholdPage = Math.min(
          this.expandedHouseholdPage,
          this.householdPageCount(group) - 1,
        );
        this.syncHouseholdPager(group);
      }
    }
    this.rebuildMarkerSpatialIndex(points);
    this.reconcileMarkers(points, showNodes);
  }

  private setSearchMatch(id: string, highlighted: boolean) {
    const entry = this.activeMarkers.get(id);
    if (!entry) return;

    if (highlighted) {
      entry.element.classList.add("search-highlight");
      entry.element.dataset.searchMatch = "true";
    } else {
      entry.element.classList.remove("search-highlight");
      delete entry.element.dataset.searchMatch;
    }
  }

  private setSelected(id: string, selected: boolean) {
    const entry = this.activeMarkers.get(id);
    if (!entry) return;

    entry.element.classList.toggle("is-selected", selected);
    if (selected) {
      entry.element.dataset.selected = "true";
      entry.element.setAttribute("aria-current", "true");
    } else {
      delete entry.element.dataset.selected;
      entry.element.removeAttribute("aria-current");
    }
  }

  public updateSelection(nextSelectedMarkerId: string | null) {
    if (this.selectedMarkerId === nextSelectedMarkerId) return;

    const previousSelectedMarkerId = this.selectedMarkerId;
    if (previousSelectedMarkerId !== null) {
      this.setSelected(previousSelectedMarkerId, false);
      this.setNativeFeatureState(previousSelectedMarkerId, { selected: false });
    }
    this.selectedMarkerId = nextSelectedMarkerId;
    const household = nextSelectedMarkerId
      ? this.households.byAccountId.get(nextSelectedMarkerId)
      : undefined;
    if (household && nextSelectedMarkerId) {
      this.householdFront.set(household.key, nextSelectedMarkerId);
      if (this.expandedHouseholdKey === household.key) {
        const index = household.members.findIndex(
          (member) => member.id === nextSelectedMarkerId,
        );
        this.expandedHouseholdPage = Math.max(
          0,
          Math.floor(index / this.householdPageCapacity()),
        );
        this.syncHouseholdPager(household);
      }
    }
    if (nextSelectedMarkerId !== null) {
      this.setSelected(nextSelectedMarkerId, true);
      this.setNativeFeatureState(nextSelectedMarkerId, { selected: true });
    }
    this.reconcileMarkers(this.latestPoints, this.latestShowNodes, false);
  }

  public updateSearchMatches(nextSearchMatchIds: ReadonlySet<string>) {
    const { added, removed } = diffSearchMatchIds(
      this.searchMatchIds,
      nextSearchMatchIds,
    );

    for (const id of removed) {
      this.setSearchMatch(id, false);
      this.setNativeFeatureState(id, { searchMatch: false });
    }
    for (const id of added) {
      this.setSearchMatch(id, true);
      this.setNativeFeatureState(id, { searchMatch: true });
    }

    this.searchMatchIds = new Set(nextSearchMatchIds);
    this.reconcileMarkers(this.latestPoints, this.latestShowNodes, false);
  }

  public getActiveMarker(id: string) {
    return this.activeMarkers.get(id);
  }

  public destroy() {
    if (this.map && typeof this.map.off === "function") {
      this.map.off("zoom", this.handleZoom);
      this.map.off("moveend", this.handleMoveEnd);
      this.map.off("resize", this.handleHouseholdResize);
      if (this.supportsNativeEntityLayer()) {
        this.map.off("styledata", this.handleStyleData);
        this.map.off("style.load", this.handleStyleData);
        if (this.nativeClickBound) {
          this.map.off(
            "click",
            NATIVE_ENTITY_LAYER_ID,
            this.handleNativeEntityClick,
          );
        }
      }
    }
    if (this.mapScaleContainer) {
      const container = this.mapScaleContainer;
      container.removeEventListener?.("keydown", this.handleHouseholdKeyDown);
      const ownership = MAP_OBJECT_SCALE_OWNERS.get(container);
      if (
        ownership?.owners.delete(this.mapScaleOwner) &&
        !ownership.owners.size
      ) {
        if (ownership.previousValue) {
          container.style.setProperty(
            "--map-object-scale",
            ownership.previousValue,
            ownership.previousPriority,
          );
        } else {
          container.style.removeProperty("--map-object-scale");
        }
        MAP_OBJECT_SCALE_OWNERS.delete(container);
      }
    }
    this.householdCloseButton?.remove();
    this.householdCloseButton = null;
    this.householdPager?.remove();
    this.householdPager = null;
    this.expandedHouseholdKey = null;
    this.expandedHouseholdPage = 0;
    this.households.groups.clear();
    this.households.byAccountId.clear();
    this.householdFront.clear();
    this.mapScaleContainer = null;
    this.activeMarkers.forEach(({ cleanup }) => cleanup());
    this.activeMarkers.clear();
    this.searchMatchIds.clear();
    this.selectedMarkerId = null;
    this.latestPoints = [];
    this.latestShowNodes = false;
    this.markerSpatialIndex = null;
    this.nativeLayerEnabled = false;
    this.nativeClickBound = false;
    this.garnrollePlacementMode = false;
  }
}
