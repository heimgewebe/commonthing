import { afterEach, describe, expect, it, vi } from "vitest";
import type { Map as MapLibreMap } from "maplibre-gl";
import type { MapEntityNode } from "$lib/map/types";
import {
  NATIVE_ENTITY_LAYER_ID,
  NATIVE_ENTITY_SOURCE_ID,
  NodesOverlay,
  type MarkerConstructor,
} from "./nodes";
import type { WeaveRuntime } from "./weaveRuntime";
import { getMapMarkerScale } from "$lib/map/markerScale";

class FakeClassList {
  private values = new Set<string>();

  setFromClassName(value: string) {
    this.values = new Set(value.split(/\s+/).filter(Boolean));
  }

  add(value: string) {
    this.values.add(value);
  }

  remove(value: string) {
    this.values.delete(value);
  }

  toggle(value: string, force?: boolean) {
    const enabled = force ?? !this.values.has(value);
    if (enabled) this.values.add(value);
    else this.values.delete(value);
    return enabled;
  }

  contains(value: string) {
    return this.values.has(value);
  }
}

class FakeStyle {
  borderStyle = "";
  private values = new Map<string, string>();
  private priorities = new Map<string, string>();

  setProperty(name: string, value: string, priority = "") {
    this.values.set(name, value);
    this.priorities.set(name, priority);
  }

  getPropertyValue(name: string) {
    return this.values.get(name) ?? "";
  }

  getPropertyPriority(name: string) {
    return this.priorities.get(name) ?? "";
  }

  removeProperty(name: string) {
    const previous = this.getPropertyValue(name);
    this.values.delete(name);
    this.priorities.delete(name);
    return previous;
  }
}

class FakeElement {
  classList = new FakeClassList();
  dataset: Record<string, string> = {};
  attributes = new Map<string, string>();
  children: FakeElement[] = [];
  style = new FakeStyle();
  title = "";
  type = "";
  src = "";
  alt = "";
  draggable = false;
  private _className = "";

  set className(value: string) {
    this._className = value;
    this.classList.setFromClassName(value);
  }

  get className() {
    return this._className;
  }

  setAttribute(name: string, value: string) {
    this.attributes.set(name, value);
  }

  getAttribute(name: string) {
    return this.attributes.get(name) ?? null;
  }

  removeAttribute(name: string) {
    this.attributes.delete(name);
  }

  append(...children: FakeElement[]) {
    this.children.push(...children);
  }
}

class FakeMarker {
  private lng = 0;
  private lat = 0;
  removed = false;

  constructor(
    public options: { element?: FakeElement; anchor?: string } = {},
  ) {}

  setLngLat([lng, lat]: [number, number]) {
    this.lng = lng;
    this.lat = lat;
    return this;
  }

  addTo() {
    return this;
  }

  getLngLat() {
    return { lng: this.lng, lat: this.lat };
  }

  remove() {
    this.removed = true;
  }
}

type FeatureCollection = {
  type: "FeatureCollection";
  features: Array<{
    id: string;
    properties: Record<string, unknown>;
    geometry: { type: "Point"; coordinates: [number, number] };
  }>;
};

class FakeSource {
  data: FeatureCollection;
  setDataCalls = 0;

  constructor(data: FeatureCollection) {
    this.data = data;
  }

  setData(data: FeatureCollection) {
    this.data = data;
    this.setDataCalls += 1;
  }
}

type Handler = (event: unknown) => void;

class FakeMap {
  styleLoaded = true;
  readonly container = new FakeElement();
  private zoom = 13.5;
  private sources = new Map<string, FakeSource>();
  private layers = new Map<string, Record<string, unknown>>();
  private handlers = new Map<string, Set<Handler>>();
  private layerHandlers = new Map<string, Set<Handler>>();
  private featureStates = new Map<string, Record<string, unknown>>();
  readonly hitIds = new Set<string>();

  getContainer() {
    return this.container as unknown as HTMLElement;
  }

  getZoom() {
    return this.zoom;
  }

  setZoom(zoom: number) {
    this.zoom = zoom;
    this.emit("zoom", undefined);
  }

  on(type: string, layerOrHandler: string | Handler, maybeHandler?: Handler) {
    if (typeof layerOrHandler === "string") {
      const key = `${type}:${layerOrHandler}`;
      if (!this.layerHandlers.has(key)) this.layerHandlers.set(key, new Set());
      if (maybeHandler) this.layerHandlers.get(key)!.add(maybeHandler);
      return this;
    }
    if (!this.handlers.has(type)) this.handlers.set(type, new Set());
    this.handlers.get(type)!.add(layerOrHandler);
    return this;
  }

  off(type: string, layerOrHandler: string | Handler, maybeHandler?: Handler) {
    if (typeof layerOrHandler === "string") {
      if (maybeHandler) {
        this.layerHandlers
          .get(`${type}:${layerOrHandler}`)
          ?.delete(maybeHandler);
      }
      return this;
    }
    this.handlers.get(type)?.delete(layerOrHandler);
    return this;
  }

  emit(type: string, event: unknown) {
    for (const handler of this.handlers.get(type) ?? []) handler(event);
  }

  emitLayer(type: string, layer: string, event: unknown) {
    for (const handler of this.layerHandlers.get(`${type}:${layer}`) ?? []) {
      handler(event);
    }
  }

  isStyleLoaded() {
    return this.styleLoaded;
  }

  addSource(id: string, spec: { type: string; data: FeatureCollection }) {
    if (!this.styleLoaded) throw new Error("style is not ready");
    expect(spec.type).toBe("geojson");
    this.sources.set(id, new FakeSource(spec.data));
  }

  getSource(id: string) {
    return this.sources.get(id);
  }

  addLayer(layer: Record<string, unknown>) {
    this.layers.set(String(layer.id), layer);
  }

  getLayer(id: string) {
    return this.layers.get(id);
  }

  setFeatureState(
    target: { source: string; id: string | number },
    state: Record<string, unknown>,
  ) {
    if (!this.sources.has(target.source)) throw new Error("missing source");
    const id = String(target.id);
    this.featureStates.set(id, {
      ...(this.featureStates.get(id) ?? {}),
      ...state,
    });
  }

  featureState(id: string) {
    return this.featureStates.get(id) ?? {};
  }

  queryRenderedFeatures(
    _point: { x: number; y: number },
    options: { layers?: string[] } = {},
  ) {
    if (!options.layers?.includes(NATIVE_ENTITY_LAYER_ID)) return [];
    return Array.from(this.hitIds).map((id) => ({ properties: { id } }));
  }

  source(id = NATIVE_ENTITY_SOURCE_ID) {
    return this.sources.get(id);
  }

  clearStyle() {
    this.sources.clear();
    this.layers.clear();
    this.featureStates.clear();
  }
}

function node(index: number): MapEntityNode {
  return {
    type: "node",
    id: `node-${index}`,
    title: `Node ${index}`,
    kind: "Werkstatt",
    tags: [],
    created_at: "2026-01-01T00:00:00Z",
    lat: 53.5 + index * 0.00001,
    lon: 10 + index * 0.00001,
  };
}

function points(count: number) {
  return Array.from({ length: count }, (_, index) => node(index));
}

const runtime: WeaveRuntime = {
  label: (item) => `Kartenobjekt ${item.title}`,
  createRoot: () => ({
    root: new FakeElement() as unknown as HTMLElement,
    signature: "test",
  }),
  syncRoot: () => "test",
};

function setup(onActivate = vi.fn()) {
  vi.stubGlobal("document", {
    createElement: () => new FakeElement(),
  });
  const map = new FakeMap();
  const overlay = new NodesOverlay(
    map as unknown as MapLibreMap,
    FakeMarker as unknown as MarkerConstructor,
    runtime,
    onActivate,
  );
  return { map, overlay, onActivate };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("NodesOverlay native dense-entity layer", () => {
  it("avoids zoom DOM scale writes until a native selection needs one marker", () => {
    const { map, overlay } = setup();
    overlay.update(points(101), true);

    const setProperty = vi.spyOn(map.container.style, "setProperty");
    map.setZoom(14);
    expect(setProperty).not.toHaveBeenCalled();

    overlay.updateSelection("node-0");
    expect(setProperty).toHaveBeenCalledTimes(1);
    expect(map.container.style.getPropertyValue("--map-object-scale")).toBe(
      getMapMarkerScale(14).toFixed(3),
    );
  });

  it("moves dense ordinary entities out of DOM markers into one GeoJSON circle layer", () => {
    const { map, overlay } = setup();
    overlay.update(points(101), true);

    const source = map.source();
    expect(source?.data.features).toHaveLength(101);
    expect(map.getLayer(NATIVE_ENTITY_LAYER_ID)).toBeDefined();
    expect(overlay.getActiveMarker("node-0")).toBeUndefined();
    expect(source?.data.features[0]).toMatchObject({
      id: "node-0",
      geometry: { type: "Point", coordinates: [10, 53.5] },
      properties: { id: "node-0", entityType: "node" },
    });
  });

  it("keeps the dense renderer structurally bounded at 10k entities", () => {
    const { map, overlay } = setup();
    overlay.update(points(10_000), true);

    const source = map.source()!;
    expect(source.data.features).toHaveLength(10_000);
    expect(overlay.getActiveMarker("node-0")).toBeUndefined();

    const beforeSetDataCalls = source.setDataCalls;
    overlay.updateSelection("node-9999");
    expect(overlay.getActiveMarker("node-9999")).toBeDefined();
    expect(source.setDataCalls).toBe(beforeSetDataCalls);
  });

  it("keeps the proven DOM renderer for the bounded small-data path", () => {
    const { map, overlay } = setup();
    overlay.update(points(100), true);

    expect(map.getLayer(NATIVE_ENTITY_LAYER_ID)).toBeUndefined();
    expect(overlay.getActiveMarker("node-0")).toBeDefined();
  });

  it("moves the measured 250-item medium-density viewport onto the native renderer", () => {
    const { map, overlay } = setup();
    overlay.update(points(250), true);

    expect(map.getLayer(NATIVE_ENTITY_LAYER_ID)).toBeDefined();
    expect(map.source()?.data.features).toHaveLength(250);
    expect(overlay.getActiveMarker("node-0")).toBeUndefined();
  });

  it("keeps only the selected medium-density entity as a DOM marker and updates feature-state without retransmitting GeoJSON", () => {
    const { map, overlay } = setup();
    overlay.update(points(250), true);
    const source = map.source()!;
    const beforeSetDataCalls = source.setDataCalls;

    overlay.updateSelection("node-3");

    expect(overlay.getActiveMarker("node-3")?.element.dataset.selected).toBe(
      "true",
    );
    expect(overlay.getActiveMarker("node-4")).toBeUndefined();
    expect(map.featureState("node-3").selected).toBe(true);
    expect(source.setDataCalls).toBe(beforeSetDataCalls);

    overlay.updateSelection("node-4");
    expect(overlay.getActiveMarker("node-3")).toBeUndefined();
    expect(overlay.getActiveMarker("node-4")).toBeDefined();
    expect(map.featureState("node-3").selected).toBe(false);
    expect(map.featureState("node-4").selected).toBe(true);
    expect(source.setDataCalls).toBe(beforeSetDataCalls);
  });

  it("projects search highlighting through feature-state without creating search DOM markers", () => {
    const { map, overlay } = setup();
    overlay.update(points(1_001), true);
    const source = map.source()!;
    const beforeSetDataCalls = source.setDataCalls;

    overlay.updateSearchMatches(new Set(["node-7"]));
    expect(map.featureState("node-7").searchMatch).toBe(true);
    expect(overlay.getActiveMarker("node-7")).toBeUndefined();
    expect(source.setDataCalls).toBe(beforeSetDataCalls);

    overlay.updateSearchMatches(new Set());
    expect(map.featureState("node-7").searchMatch).toBe(false);
    expect(source.setDataCalls).toBe(beforeSetDataCalls);
  });

  it("activates the canonical entity from a layer-bound click", () => {
    const onActivate = vi.fn();
    const { map, overlay } = setup(onActivate);
    const allPoints = points(1_001);
    overlay.update(allPoints, true);

    map.emitLayer("click", NATIVE_ENTITY_LAYER_ID, {
      features: [{ properties: { id: "node-9" } }],
    });

    expect(onActivate).toHaveBeenCalledTimes(1);
    expect(onActivate).toHaveBeenCalledWith(allPoints[9]);
  });

  it("exposes an exact native-layer hit test for focus and composition boundaries", () => {
    const { map, overlay } = setup();
    overlay.update(points(1_001), true);

    expect(overlay.hasNativeEntityAt({ x: 4, y: 5 })).toBe(false);
    map.hitIds.add("node-2");
    expect(overlay.hasNativeEntityAt({ x: 4, y: 5 })).toBe(true);
  });

  it("keeps the DOM fallback until the basemap style is ready, then cuts over natively", () => {
    const { map, overlay } = setup();
    map.styleLoaded = false;
    overlay.update(points(1_001), true);

    expect(map.getLayer(NATIVE_ENTITY_LAYER_ID)).toBeUndefined();
    expect(overlay.getActiveMarker("node-0")).toBeDefined();

    // Real MapLibre can emit styledata before isStyleLoaded() flips true.
    // That intermediate event must keep the proven DOM fallback intact.
    map.emit("styledata", {});
    expect(map.getLayer(NATIVE_ENTITY_LAYER_ID)).toBeUndefined();
    expect(overlay.getActiveMarker("node-0")).toBeDefined();

    map.styleLoaded = true;
    map.emit("style.load", {});

    expect(map.getLayer(NATIVE_ENTITY_LAYER_ID)).toBeDefined();
    expect(map.source()?.data.features).toHaveLength(1_001);
    expect(overlay.getActiveMarker("node-0")).toBeUndefined();
  });

  it("ignores routine styledata events once the native source and layer already exist", () => {
    const { map, overlay } = setup();
    overlay.update(points(1_001), true);
    const source = map.source()!;
    const beforeSetDataCalls = source.setDataCalls;

    map.emit("styledata", {});

    expect(source.setDataCalls).toBe(beforeSetDataCalls);
    expect(map.getLayer(NATIVE_ENTITY_LAYER_ID)).toBeDefined();
  });

  it("rehydrates source, layer and feature-state after a basemap style replacement", () => {
    const { map, overlay } = setup();
    overlay.update(points(1_001), true);
    overlay.updateSelection("node-5");
    overlay.updateSearchMatches(new Set(["node-6"]));

    map.clearStyle();
    map.emit("styledata", {});

    expect(map.source()?.data.features).toHaveLength(1_001);
    expect(map.getLayer(NATIVE_ENTITY_LAYER_ID)).toBeDefined();
    expect(map.featureState("node-5").selected).toBe(true);
    expect(map.featureState("node-6").searchMatch).toBe(true);
  });

  it("clears native data when node rendering is disabled", () => {
    const { map, overlay } = setup();
    const allPoints = points(1_001);
    overlay.update(allPoints, true);
    overlay.update(allPoints, false);

    expect(map.source()?.data.features).toEqual([]);
    expect(overlay.hasNativeEntityAt({ x: 0, y: 0 })).toBe(false);
  });
});
