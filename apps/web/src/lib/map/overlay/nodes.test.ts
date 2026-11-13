import { afterEach, describe, expect, it, vi } from "vitest";
import type { Map as MapLibreMap } from "maplibre-gl";
import type {
  MapEdge,
  MapEntityGarnrolle,
  MapEntityNode,
  MapEntityWeave,
  MapEntityWebgemeindezentrum,
} from "$lib/map/types";
import { FADEN_LIFETIME_MS } from "$lib/map/edgeLifecycle";
import { getMapMarkerScale } from "$lib/map/markerScale";
import {
  MARKER_GEO_ANCHOR,
  NodesOverlay,
  diffSearchMatchIds,
  screenHitBoxesOverlap,
  type MarkerConstructor,
} from "./nodes";
import {
  projectMarkersForWeave,
  weaveRenderSignature,
  weaveRuntime,
} from "./weaveRuntime";

class FakeClassList {
  private values = new Set<string>();

  setFromClassName(className: string) {
    this.values = new Set(className.split(/\s+/).filter(Boolean));
  }

  contains(value: string) {
    return this.values.has(value);
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
}

class FakeStyle {
  [key: string]: unknown;

  setProperty(name: string, value: string, priority = "") {
    this[name] = value;
    const priorityKey = `${name}:priority`;
    if (priority) this[priorityKey] = priority;
    else delete this[priorityKey];
  }

  getPropertyValue(name: string) {
    const value = this[name];
    return typeof value === "string" ? value : "";
  }

  getPropertyPriority(name: string) {
    const priority = this[`${name}:priority`];
    return typeof priority === "string" ? priority : "";
  }

  removeProperty(name: string) {
    const value = this.getPropertyValue(name);
    delete this[name];
    delete this[`${name}:priority`];
    return value;
  }
}

class FakeElement {
  classList = new FakeClassList();
  dataset: Record<string, string> = {};
  title = "";
  textContent = "";
  type = "";
  src = "";
  alt = "";
  draggable = false;
  children: FakeElement[] = [];
  attributes = new Map<string, string>();
  style = new FakeStyle();
  private _className = "";
  private _innerHTML = "";
  private listeners = new Map<string, Set<(event: any) => void>>();
  removed = false;
  focused = false;

  focus() {
    this.focused = true;
  }
  get firstElementChild(): FakeElement | null {
    return this.children[0] ?? null;
  }
  replaceChildren(...children: FakeElement[]) {
    this.children = children;
  }

  addEventListener(type: string, listener: (event: any) => void) {
    const set = this.listeners.get(type) ?? new Set();
    set.add(listener);
    this.listeners.set(type, set);
  }

  removeEventListener(type: string, listener: (event: any) => void) {
    this.listeners.get(type)?.delete(listener);
  }

  fire(type: string, values: Record<string, unknown> = {}) {
    const event = {
      type,
      button: 0,
      clientX: 100,
      clientY: 100,
      preventDefault: vi.fn(),
      stopPropagation: vi.fn(),
      ...values,
    };
    for (const listener of this.listeners.get(type) ?? []) listener(event);
    return event;
  }

  remove() {
    this.removed = true;
  }

  set className(value: string) {
    this._className = value;
    this.classList.setFromClassName(value);
  }

  get className() {
    return this._className;
  }

  setAttribute(name: string, value: string) {
    this.attributes.set(name, value);
    if (name.startsWith("data-")) {
      const key = name
        .slice(5)
        .replace(/-([a-z])/g, (_, letter: string) => letter.toUpperCase());
      this.dataset[key] = value;
    }
  }

  getAttribute(name: string) {
    return this.attributes.get(name) ?? null;
  }

  hasAttribute(name: string) {
    return this.attributes.has(name);
  }

  removeAttribute(name: string) {
    this.attributes.delete(name);
  }

  append(...children: FakeElement[]) {
    this.children.push(...children);
    this._innerHTML = "";
  }

  get firstChild(): FakeElement | null {
    return this.children[0] ?? null;
  }

  removeChild(child: FakeElement): FakeElement {
    const index = this.children.indexOf(child);
    if (index >= 0) this.children.splice(index, 1);
    return child;
  }

  set innerHTML(value: string) {
    this._innerHTML = value;
    this.children = [];
    this.textContent = "";
  }

  get innerHTML(): string {
    if (this.children.length === 0) return this._innerHTML;
    return this.children.map((child) => child.outerHTML).join("");
  }

  get outerHTML(): string {
    const attrs: string[] = [];
    if (this._className) attrs.push(`class="${this._className}"`);
    for (const [name, value] of this.attributes) {
      attrs.push(`${name}="${value}"`);
    }
    if (this.title) attrs.push(`title="${this.title}"`);
    const open = attrs.length ? `<span ${attrs.join(" ")}>` : "<span>";
    const body =
      this.children.length > 0
        ? this.children.map((child) => child.outerHTML).join("")
        : this.textContent || this._innerHTML;
    return `${open}${body}</span>`;
  }

  querySelectorAll(selector = "*"): FakeElement[] {
    const matches: FakeElement[] = [];
    const visit = (node: FakeElement) => {
      for (const child of node.children) {
        if (selector === "*" || FakeElement.matches(child, selector)) {
          matches.push(child);
        }
        visit(child);
      }
    };
    visit(this);
    return matches;
  }

  private static matches(node: FakeElement, selector: string): boolean {
    if (selector === "*") return true;
    if (selector.startsWith(".")) {
      return node.classList.contains(selector.slice(1));
    }
    if (selector.startsWith("[") && selector.endsWith("]")) {
      const body = selector.slice(1, -1);
      const eq = body.indexOf("=");
      if (eq < 0) return node.attributes.has(body) || body in node.dataset;
      const name = body.slice(0, eq);
      const raw = body.slice(eq + 1).replace(/^["']|["']$/g, "");
      if (name.startsWith("data-")) {
        const key = name
          .slice(5)
          .replace(/-([a-z])/g, (_, letter: string) => letter.toUpperCase());
        return node.dataset[key] === raw || node.attributes.get(name) === raw;
      }
      return node.attributes.get(name) === raw;
    }
    return false;
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

function makeWeave(overrides: Partial<MapEntityWeave> = {}): MapEntityWeave {
  return {
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
    coreDensity: 0.55,
    conversationRingScale: 0.9,
    knottingThreadCount: 1,
    conversationThreadCount: 1,
    conversationOpacity: 0.8,
    proposalArcs: [],
    proposalCount: 0,
    proposalOverflowCount: 0,
    voteThreadCount: 0,
    totalActiveThreadCount: 2,
    ...overrides,
  };
}

function makeNode(
  id: string,
  overrides: Partial<MapEntityNode> = {},
): MapEntityNode {
  return {
    type: "node",
    id,
    title: `Node ${id}`,
    kind: "Werkstatt",
    tags: [],
    created_at: "2025-01-01T00:00:00Z",
    lat: 53.5,
    lon: 10,
    weave: makeWeave(),
    ...overrides,
  };
}

function makeAccount(id: string): MapEntityGarnrolle {
  return {
    type: "garnrolle",
    id,
    title: `Garnrolle ${id}`,
    lat: 53.51,
    lon: 10.01,
    created_at: "2025-01-01T00:00:00Z",
  };
}

function makeCenter(
  id = "webgemeindezentrum-hammer-park",
): MapEntityWebgemeindezentrum {
  return {
    type: "webgemeindezentrum",
    id,
    title: "Webgemeindezentrum Hammer Park",
    lat: 53.5585,
    lon: 10.058,
    summary: "Hier kann die Ortsweberei tatsächlich zusammenkommen.",
    tags: ["Webgemeindezentrum", "Ortsweberei Hamm"],
    created_at: "2026-08-02T10:08:00.000Z",
    updated_at: "2026-08-02T10:08:00.000Z",
    location_state: "desired",
    location_state_label: "Gewünschter Treffort",
    faden_endpoint_id: "22222222-2222-5222-8222-222222222222",
    conversation_id: "33333333-3333-5333-8333-333333333333",
    location_label: "Hammer Park – gewünschter Treffpunkt auf der Grünfläche",
    meeting_note: "Hier kann die Ortsweberei tatsächlich zusammenkommen.",
    access_note: "Nutzung und Barrierefreiheit sind noch nicht bestätigt.",
    weave: makeWeave(),
    ortsweberei: {
      id: "ortsweberei-hamm",
      slug: "hamm",
      name: "Ortsweberei Hamm",
      gewebezelle_id: "hamm.weltgewebe.net",
    },
  };
}

function makeOverlay() {
  vi.stubGlobal("document", {
    createElement: () => new FakeElement(),
  });
  return new NodesOverlay(
    {} as MapLibreMap,
    FakeMarker as unknown as MarkerConstructor,
    weaveRuntime,
  );
}

function makeOverlayWithContainer(container = new FakeElement()) {
  vi.stubGlobal("document", {
    createElement: () => new FakeElement(),
  });
  const map = {
    getContainer: () => container as unknown as HTMLElement,
  } as unknown as MapLibreMap;
  return {
    container,
    overlay: new NodesOverlay(
      map,
      FakeMarker as unknown as MarkerConstructor,
      weaveRuntime,
    ),
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("diffSearchMatchIds", () => {
  it("returns only the changed search-match ids", () => {
    expect(
      diffSearchMatchIds(new Set(["keep", "remove"]), new Set(["keep", "add"])),
    ).toEqual({
      added: ["add"],
      removed: ["remove"],
    });
  });

  it("returns an empty delta for unchanged matches", () => {
    expect(
      diffSearchMatchIds(new Set(["a", "b"]), new Set(["a", "b"])),
    ).toEqual({
      added: [],
      removed: [],
    });
  });
});

describe("NodesOverlay household interactions", () => {
  const exact = (id: string): MapEntityGarnrolle => ({
    ...makeAccount(id),
    map_state: "exact",
  });

  it("keeps the most recently selected account in front, with other nodes intact", () => {
    const overlay = makeOverlay();
    overlay.update([exact("a"), exact("b"), makeNode("knoten")], true);
    expect(overlay.getActiveMarker("a")).toBeUndefined();
    const top = overlay.getActiveMarker("b");
    expect(top?.element.dataset.householdCount).toBe("2");
    expect(top?.element.classList.contains("marker-household-stack")).toBe(
      true,
    );
    expect(overlay.getActiveMarker("knoten")).toBeDefined();

    overlay.updateSelection("a");
    expect(overlay.getActiveMarker("b")).toBeUndefined();
    expect(overlay.getActiveMarker("a")?.element.dataset.selected).toBe("true");

    // A refresh may reorder the server response; the front is user-driven.
    overlay.update([exact("b"), exact("a"), makeNode("knoten")], true);
    expect(overlay.getActiveMarker("a")).toBeDefined();
    expect(overlay.getActiveMarker("b")).toBeUndefined();
    overlay.destroy();
  });

  it("keeps an already-selected standalone account in front when a household forms", () => {
    const overlay = makeOverlay();
    overlay.update([exact("a"), makeNode("knoten")], true);
    overlay.updateSelection("a");
    expect(overlay.getActiveMarker("a")?.element.dataset.selected).toBe("true");

    // A normal data refresh adds B at the exact same location; the selection
    // effect may not rerun because the selected ID did not change.
    overlay.update([exact("a"), exact("b"), makeNode("knoten")], true);
    expect(overlay.getActiveMarker("a")?.element.dataset.householdCount).toBe(
      "2",
    );
    expect(overlay.getActiveMarker("a")?.element.dataset.selected).toBe("true");
    expect(overlay.getActiveMarker("b")).toBeUndefined();
    overlay.updateSelection("a");
    expect(overlay.getActiveMarker("a")).toBeDefined();

    overlay.destroy();
  });

  it("opens by longpress, spreads all account hit targets, and closes with last-used on top", () => {
    vi.useFakeTimers();
    try {
      const { overlay, container } = makeOverlayWithContainer();
      overlay.update([exact("a"), exact("b"), makeNode("knoten")], true);
      const top = overlay.getActiveMarker("b")
        ?.element as unknown as FakeElement;
      top.fire("pointerdown");
      vi.advanceTimersByTime(649);
      expect(overlay.getActiveMarker("a")).toBeUndefined();
      vi.advanceTimersByTime(1);
      const first = overlay.getActiveMarker("a")
        ?.element as unknown as FakeElement;
      const second = overlay.getActiveMarker("b")
        ?.element as unknown as FakeElement;
      expect(first).toBeDefined();
      expect(second).toBeDefined();
      expect(first.style.translate).not.toBe(second.style.translate);
      expect(first.classList.contains("marker-household-expanded")).toBe(true);
      expect(overlay.getActiveMarker("knoten")).toBeDefined();

      // Tapping another account changes the front after the household closes.
      first.fire("click");
      const close = container.children.find(
        (child) => child.dataset.testid === "close-household-view",
      );
      expect(close).toBeDefined();
      expect(close?.fire("mousedown").stopPropagation).toHaveBeenCalledOnce();
      expect(close?.fire("touchstart").stopPropagation).toHaveBeenCalledOnce();
      close?.fire("click");
      expect(close?.removed).toBe(true);
      expect(overlay.getActiveMarker("a")).toBeDefined();
      expect(overlay.getActiveMarker("b")).toBeUndefined();
      overlay.destroy();
    } finally {
      vi.useRealTimers();
    }
  });

  it("pages 20 co-located accounts on mobile, without losing focus or individual search matches", () => {
    const { overlay, container } = makeOverlayWithContainer();
    Object.assign(container, { clientWidth: 220, clientHeight: 430 });
    const accounts = Array.from({ length: 20 }, (_, index) =>
      exact(`g-${index}`),
    );
    overlay.update(accounts, true);
    (overlay.getActiveMarker("g-19")?.element as unknown as FakeElement).fire(
      "dblclick",
    );

    const pager = container.children.find(
      (child) => child.dataset.testid === "household-page-controls",
    );
    expect(pager?.children[1].textContent).toBe("Seite 4 von 4");
    expect(pager?.fire("pointerdown").stopPropagation).toHaveBeenCalledOnce();
    expect(pager?.fire("mousedown").stopPropagation).toHaveBeenCalledOnce();
    expect(
      accounts.filter((item) => overlay.getActiveMarker(item.id)),
    ).toHaveLength(2);

    (pager?.children[0] as FakeElement).fire("click");
    expect(pager?.children[1].textContent).toBe("Seite 3 von 4");
    expect(
      accounts.filter((item) => overlay.getActiveMarker(item.id)),
    ).toHaveLength(6);
    overlay.updateSearchMatches(new Set(["g-12"]));
    expect(overlay.getActiveMarker("g-12")?.element.dataset.searchMatch).toBe(
      "true",
    );
    expect(
      overlay.getActiveMarker("g-13")?.element.dataset.searchMatch,
    ).toBeUndefined();

    overlay.updateSelection("g-4");
    expect(pager?.children[1].textContent).toBe("Seite 1 von 4");
    expect(overlay.getActiveMarker("g-4")).toBeDefined();
    expect(overlay.getActiveMarker("g-12")).toBeUndefined();

    container.fire("keydown", { key: "Escape" });
    expect(pager?.removed).toBe(true);
    expect(overlay.getActiveMarker("g-4")?.element.dataset.householdCount).toBe(
      "20",
    );
    expect(overlay.getActiveMarker("g-19")).toBeUndefined();
    overlay.destroy();
  });

  it("removes paging controls when the house becomes a standalone account", () => {
    const { overlay, container } = makeOverlayWithContainer();
    Object.assign(container, { clientWidth: 220, clientHeight: 430 });
    const accounts = Array.from({ length: 20 }, (_, index) =>
      exact(`g-${index}`),
    );
    overlay.update(accounts, true);
    (overlay.getActiveMarker("g-19")?.element as unknown as FakeElement).fire(
      "dblclick",
    );
    const pager = container.children.find(
      (child) => child.dataset.testid === "household-page-controls",
    );
    expect(pager).toBeDefined();
    overlay.update([accounts[0]], true);
    expect(pager?.removed).toBe(true);
    expect(overlay.getActiveMarker("g-0")).toBeDefined();
    overlay.destroy();
  });

  it("prevents a native context menu after the touch hold already opened a household", () => {
    vi.useFakeTimers();
    try {
      const { overlay } = makeOverlayWithContainer();
      const a = { ...makeAccount("a"), map_state: "exact" as const };
      const b = { ...makeAccount("b"), map_state: "exact" as const };
      overlay.update([a, b], true);
      const front = overlay.getActiveMarker("b")
        ?.element as unknown as FakeElement;
      front.fire("pointerdown");
      vi.advanceTimersByTime(650);
      expect(overlay.getActiveMarker("a")).toBeDefined();

      const menu = front.fire("contextmenu");
      expect(menu.preventDefault).toHaveBeenCalledTimes(1);
      expect(overlay.getActiveMarker("a")).toBeDefined();
      overlay.destroy();
    } finally {
      vi.useRealTimers();
    }
  });

  it("cancels a longpress on dragging so normal map panning is unaffected", () => {
    vi.useFakeTimers();
    try {
      const overlay = makeOverlay();
      overlay.update([exact("a"), exact("b")], true);
      const top = overlay.getActiveMarker("b")
        ?.element as unknown as FakeElement;
      top.fire("pointerdown");
      top.fire("pointermove", { clientX: 120 });
      vi.advanceTimersByTime(800);
      expect(overlay.getActiveMarker("a")).toBeUndefined();
      overlay.destroy();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("NodesOverlay runtime robustness", () => {
  it("accepts projected markers without creating markers when the map is absent", () => {
    vi.stubGlobal("document", {
      createElement: () => new FakeElement(),
    });
    const overlay = new NodesOverlay(
      null,
      FakeMarker as unknown as MarkerConstructor,
      weaveRuntime,
    );
    expect(() => overlay.update([makeNode("a")], true)).not.toThrow();
    expect(overlay.getActiveMarker("a")).toBeUndefined();
  });

  it("renders the complete woven marker in its first frame", () => {
    vi.stubGlobal("document", {
      createElement: () => new FakeElement(),
    });
    const overlay = new NodesOverlay(
      {} as MapLibreMap,
      FakeMarker as unknown as MarkerConstructor,
    );
    overlay.update([makeNode("a")], true);
    const root = overlay.getActiveMarker("a")?.element.children[0]
      .children[0] as HTMLElement | undefined;
    expect(root?.dataset.zoneOrder).toBe("knotting,conversation,proposal,vote");
    expect(root?.innerHTML).toContain('data-zone="knotting"');
  });

  it("pins node, center and Garnrolle markers on the shared geographic center", () => {
    const overlay = makeOverlay();
    overlay.update(
      [makeNode("node-a"), makeCenter("center-a"), makeAccount("spool-a")],
      true,
    );
    for (const id of ["node-a", "center-a", "spool-a"] as const) {
      const entry = overlay.getActiveMarker(id);
      const marker = entry?.marker as unknown as FakeMarker;
      expect(marker.options.anchor).toBe(MARKER_GEO_ANCHOR);
      expect(marker.options.anchor).toBe("center");
      expect(marker.getLngLat()).toEqual({
        lng: entry?.item.lon,
        lat: entry?.item.lat,
      });
    }
  });

  it("derives an empty weave instead of crashing when a caller omitted it", () => {
    const overlay = makeOverlay();
    overlay.update([makeNode("a", { weave: undefined })], true);
    const root = overlay.getActiveMarker("a")?.element.children[0]
      .children[0] as HTMLElement | undefined;
    expect(root?.dataset.proposalCount).toBe("0");
    expect(root?.dataset.voteThreads).toBe("0");
  });

  it("preserves full DOM marker semantics for small datasets", () => {
    vi.stubGlobal("document", {
      createElement: () => new FakeElement(),
    });
    const getBounds = vi.fn(() => ({ contains: () => false }));
    const on = vi.fn();
    const off = vi.fn();
    const map = { getBounds, on, off } as unknown as MapLibreMap;
    const overlay = new NodesOverlay(
      map,
      FakeMarker as unknown as MarkerConstructor,
      weaveRuntime,
    );

    overlay.update(
      [makeNode("west", { lon: 10 }), makeNode("east", { lon: 20 })],
      true,
    );

    expect(overlay.getActiveMarker("west")).toBeDefined();
    expect(overlay.getActiveMarker("east")).toBeDefined();
    expect(getBounds).not.toHaveBeenCalled();

    const moveEnd = on.mock.calls.find((call) => call[0] === "moveend")?.[1] as
      | (() => void)
      | undefined;
    moveEnd?.();
    expect(getBounds).not.toHaveBeenCalled();

    overlay.destroy();
    expect(off).toHaveBeenCalledWith("moveend", moveEnd);
  });

  it("virtualizes large DOM marker sets after moveend", () => {
    vi.stubGlobal("document", {
      createElement: () => new FakeElement(),
    });
    let viewport: "west" | "east" = "west";
    const contains = vi.fn((lngLat: [number, number]) =>
      viewport === "west" ? lngLat[0] < 15 : lngLat[0] >= 15,
    );
    const getBounds = vi.fn(() => ({ contains }));
    const on = vi.fn();
    const off = vi.fn();
    const map = { getBounds, on, off } as unknown as MapLibreMap;
    const overlay = new NodesOverlay(
      map,
      FakeMarker as unknown as MarkerConstructor,
      weaveRuntime,
    );
    const westPoints = Array.from({ length: 100 }, (_, index) =>
      makeNode(`west-${index}`, { lon: 10 }),
    );

    overlay.update([...westPoints, makeNode("east", { lon: 20 })], true);

    expect(overlay.getActiveMarker("west-0")).toBeDefined();
    expect(overlay.getActiveMarker("east")).toBeUndefined();
    expect(getBounds).toHaveBeenCalledTimes(1);
    const moveEnd = on.mock.calls.find((call) => call[0] === "moveend")?.[1] as
      | (() => void)
      | undefined;
    expect(moveEnd).toBeTypeOf("function");

    viewport = "east";
    moveEnd?.();

    expect(overlay.getActiveMarker("west-0")).toBeUndefined();
    expect(overlay.getActiveMarker("east")).toBeDefined();
    expect(getBounds).toHaveBeenCalledTimes(2);

    overlay.destroy();
    expect(off).toHaveBeenCalledWith("moveend", moveEnd);
  });

  it("pins selected and search-matched markers outside a virtualized viewport", () => {
    vi.stubGlobal("document", {
      createElement: () => new FakeElement(),
    });
    const contains = vi.fn((lngLat: [number, number]) => lngLat[0] < 15);
    const map = {
      getBounds: () => ({ contains }),
      on: vi.fn(),
      off: vi.fn(),
    } as unknown as MapLibreMap;
    const overlay = new NodesOverlay(
      map,
      FakeMarker as unknown as MarkerConstructor,
      weaveRuntime,
    );
    const westPoints = Array.from({ length: 100 }, (_, index) =>
      makeNode(`west-${index}`, { lon: 10 }),
    );
    overlay.update(
      [
        ...westPoints,
        makeNode("east-search", { lon: 20 }),
        makeNode("east-selected", { lon: 21 }),
      ],
      true,
    );

    expect(overlay.getActiveMarker("east-search")).toBeUndefined();
    expect(overlay.getActiveMarker("east-selected")).toBeUndefined();

    overlay.updateSearchMatches(new Set(["east-search"]));
    const search = overlay.getActiveMarker("east-search");
    expect(search).toBeDefined();
    expect(search?.element.dataset.searchMatch).toBe("true");

    overlay.updateSelection("east-selected");
    const selected = overlay.getActiveMarker("east-selected");
    expect(selected).toBeDefined();
    expect(selected?.element.dataset.selected).toBe("true");
    expect(selected?.element.getAttribute("aria-current")).toBe("true");

    overlay.updateSearchMatches(new Set());
    expect(overlay.getActiveMarker("east-search")).toBeUndefined();
    expect(overlay.getActiveMarker("east-selected")).toBeDefined();

    overlay.updateSelection(null);
    expect(overlay.getActiveMarker("east-selected")).toBeUndefined();

    overlay.destroy();
  });
});

describe("weave projection timing", () => {
  it("removes a target-only weave exactly at its lifecycle boundary", () => {
    const expiresAtMs = 10_000_000;
    const edge: MapEdge = {
      id: "target-only",
      source_id: "hidden-source",
      target_id: "target",
      edge_kind: "reference",
      faden_type: "conversation",
      faden_subject_id: "conversation-target",
      lifecycle: {
        kind: "faden",
        createdAtMs: expiresAtMs - FADEN_LIFETIME_MS,
        expiresAtMs,
      },
    };
    const points = [makeNode("target", { weave: undefined })];

    const active = projectMarkersForWeave(points, [edge], expiresAtMs - 1);
    const expired = projectMarkersForWeave(points, [edge], expiresAtMs);

    if (active[0].type !== "node" || expired[0].type !== "node") {
      throw new Error("woven projection changed the target entity category");
    }
    expect(active[0].weave?.conversationThreadCount).toBe(1);
    expect(expired[0].weave?.conversationThreadCount).toBe(0);
  });
});

describe("NodesOverlay search-highlight lifecycle", () => {
  it("applies stored search state to a marker created later", () => {
    const overlay = makeOverlay();
    overlay.updateSearchMatches(new Set(["a"]));
    overlay.update([makeNode("a")], true);

    expect(overlay.getActiveMarker("a")?.element.dataset.searchMatch).toBe(
      "true",
    );
  });

  it("updates A to B and then clears the highlight without a full marker update", () => {
    const overlay = makeOverlay();
    overlay.update([makeNode("a"), makeNode("b")], true);

    overlay.updateSearchMatches(new Set(["a"]));
    expect(overlay.getActiveMarker("a")?.element.dataset.searchMatch).toBe(
      "true",
    );
    expect(
      overlay.getActiveMarker("b")?.element.dataset.searchMatch,
    ).toBeUndefined();

    overlay.updateSearchMatches(new Set(["b"]));
    expect(
      overlay.getActiveMarker("a")?.element.dataset.searchMatch,
    ).toBeUndefined();
    expect(overlay.getActiveMarker("b")?.element.dataset.searchMatch).toBe(
      "true",
    );

    overlay.updateSearchMatches(new Set());
    expect(
      overlay.getActiveMarker("b")?.element.dataset.searchMatch,
    ).toBeUndefined();
  });

  it("restores the highlight after marker removal and recreation", () => {
    const overlay = makeOverlay();
    overlay.updateSearchMatches(new Set(["a"]));
    overlay.update([makeNode("a")], true);
    overlay.update([], true);
    expect(overlay.getActiveMarker("a")).toBeUndefined();

    overlay.update([makeNode("a")], true);
    expect(overlay.getActiveMarker("a")?.element.dataset.searchMatch).toBe(
      "true",
    );
  });

  it("preserves search state across showNodes false to true", () => {
    const overlay = makeOverlay();
    overlay.updateSearchMatches(new Set(["a"]));
    overlay.update([makeNode("a")], true);
    overlay.update([makeNode("a")], false);
    expect(overlay.getActiveMarker("a")).toBeUndefined();

    overlay.update([makeNode("a")], true);
    expect(overlay.getActiveMarker("a")?.element.dataset.searchMatch).toBe(
      "true",
    );
  });
});

describe("NodesOverlay selection lifecycle", () => {
  it("applies a selected state as a round-halo hook without recreating markers", () => {
    const overlay = makeOverlay();
    overlay.update([makeNode("a"), makeNode("b")], true);
    const markerA = overlay.getActiveMarker("a");

    overlay.updateSelection("a");
    expect(markerA?.element.classList.contains("is-selected")).toBe(true);
    expect(markerA?.element.dataset.selected).toBe("true");
    expect(markerA?.element.getAttribute("aria-current")).toBe("true");
    expect(
      markerA?.element.children[0].classList.contains("marker-node__visual"),
    ).toBe(true);
    expect(
      markerA?.element.children[1].classList.contains("map-marker__halo"),
    ).toBe(true);

    overlay.updateSelection("b");
    expect(markerA?.element.classList.contains("is-selected")).toBe(false);
    expect(markerA?.element.dataset.selected).toBeUndefined();
    expect(markerA?.element.hasAttribute("aria-current")).toBe(false);
    expect(overlay.getActiveMarker("a")?.marker).toBe(markerA?.marker);
    expect(overlay.getActiveMarker("b")?.element.dataset.selected).toBe("true");
  });

  it("restores selection when a filtered marker is recreated", () => {
    const overlay = makeOverlay();
    overlay.updateSelection("a");
    overlay.update([makeNode("a")], true);
    expect(overlay.getActiveMarker("a")?.element.dataset.selected).toBe("true");

    overlay.update([], true);
    overlay.update([makeNode("a")], true);
    expect(overlay.getActiveMarker("a")?.element.dataset.selected).toBe("true");
  });
});

describe("NodesOverlay woven node marker", () => {
  it("renders the diagonal X core, conversation ring and separate proposal-bound vote wreaths", () => {
    const overlay = makeOverlay();
    overlay.update(
      [
        makeNode("woven", {
          weave: makeWeave({
            proposalCount: 2,
            voteThreadCount: 3,
            proposalArcs: [
              {
                subjectId: "proposal-a",
                proposalThreadCount: 1,
                conversationThreadCount: 1,
                voteThreadCount: 2,
                bundledSubjectCount: 1,
                latestActivityAtMs: 10,
                opacity: 0.9,
                color: "#5f7a55",
                startDeg: 20,
                spanDeg: 120,
              },
              {
                subjectId: "proposal-b",
                proposalThreadCount: 1,
                conversationThreadCount: 0,
                voteThreadCount: 1,
                bundledSubjectCount: 1,
                latestActivityAtMs: 9,
                opacity: 0.8,
                color: "#5f7a55",
                startDeg: 190,
                spanDeg: 120,
              },
            ],
          }),
        }),
      ],
      true,
    );

    const marker = overlay.getActiveMarker("woven");
    const root = marker?.element.children[0].children[0] as
      | HTMLElement
      | undefined;
    expect(root?.classList.contains("woven-node")).toBe(true);
    expect(root?.dataset.zoneOrder).toBe("knotting,conversation,proposal,vote");
    expect(root?.dataset.xGeometry).toBe("diagonal");
    expect(root?.innerHTML).toContain('data-zone="knotting"');
    expect(root?.innerHTML).toContain('data-x-geometry="diagonal"');
    expect(root?.innerHTML).toContain('data-strand="a"');
    expect(root?.innerHTML).toContain('data-strand="b"');
    expect(root?.innerHTML).toContain('data-arm="northwest"');
    expect(root?.innerHTML).toContain('data-arm="northeast"');
    expect(root?.innerHTML).toContain('data-arm="southeast"');
    expect(root?.innerHTML).toContain('data-arm="southwest"');
    expect(root?.innerHTML).toContain('data-zone="conversation"');
    expect(root?.innerHTML.match(/data-zone="proposal"/g)).toHaveLength(2);
    expect(root?.innerHTML.match(/data-zone="vote"/g)).toHaveLength(2);
    expect(root?.innerHTML).toContain(
      'data-zone="proposal" data-proposal-slot="1"',
    );
    expect(root?.innerHTML).toContain(
      'data-zone="vote" data-proposal-slot="1"',
    );
    expect(root?.innerHTML).not.toMatch(
      /data-zone="proposal"[^>]*>\s*<span[^>]*data-zone="vote"/,
    );
  });

  it("omits empty vote DOM nodes", () => {
    const overlay = makeOverlay();
    overlay.update(
      [
        makeNode("without-votes", {
          weave: makeWeave({
            proposalCount: 1,
            proposalArcs: [
              {
                subjectId: "proposal-a",
                proposalThreadCount: 1,
                conversationThreadCount: 0,
                voteThreadCount: 0,
                bundledSubjectCount: 1,
                latestActivityAtMs: 10,
                opacity: 1,
                color: "#5f7a55",
                startDeg: 55,
                spanDeg: 250,
              },
            ],
          }),
        }),
      ],
      true,
    );
    const root = overlay.getActiveMarker("without-votes")?.element.children[0]
      .children[0] as HTMLElement | undefined;
    expect(root?.innerHTML).toContain('data-zone="proposal"');
    expect(root?.innerHTML).not.toContain('data-zone="vote"');
  });

  it("switches nodes and Webgemeindezentren between compact and detail zoom", () => {
    const overlay = makeOverlay();
    overlay.update([makeNode("node"), makeCenter("center")], true);

    overlay.updateZoom(13.4);
    const nodeRoot = overlay.getActiveMarker("node")?.element.children[0]
      .children[0] as HTMLElement | undefined;
    const centerRoot = overlay.getActiveMarker("center")?.element.children[0]
      .children[0].children[0] as HTMLElement | undefined;
    expect(nodeRoot?.classList.contains("woven-node--compact")).toBe(true);
    expect(centerRoot?.classList.contains("woven-node--compact")).toBe(true);
    expect(nodeRoot?.dataset.weaveDetail).toBe("compact");
    expect(centerRoot?.dataset.weaveDetail).toBe("compact");

    overlay.updateZoom(13.5);
    expect(nodeRoot?.classList.contains("woven-node--compact")).toBe(false);
    expect(centerRoot?.classList.contains("woven-node--compact")).toBe(false);
    expect(nodeRoot?.dataset.weaveDetail).toBe("detail");
    expect(centerRoot?.dataset.weaveDetail).toBe("detail");
  });

  it("inherits one continuous world scale with O(1) zoom DOM writes", () => {
    const { overlay, container } = makeOverlayWithContainer();
    overlay.updateZoom(7);
    expect(container.style.getPropertyValue("--map-object-scale")).toBe(
      "0.620",
    );
    const containerSetProperty = vi.spyOn(container.style, "setProperty");

    overlay.update(
      [makeNode("node"), makeCenter("center"), makeAccount("spool")],
      true,
    );
    const visualFor = (id: string) =>
      overlay.getActiveMarker(id)?.element.children[0] as
        | FakeElement
        | undefined;
    for (const id of ["node", "center", "spool"]) {
      expect(visualFor(id)?.style.getPropertyValue("--map-object-scale")).toBe(
        "",
      );
    }
    expect(containerSetProperty).not.toHaveBeenCalled();

    // All active markers share one inherited write even within compact mode.
    overlay.updateZoom(10);
    const zoomTenScale = getMapMarkerScale(10).toFixed(3);
    expect(container.style.getPropertyValue("--map-object-scale")).toBe(
      zoomTenScale,
    );
    expect(containerSetProperty).toHaveBeenCalledTimes(1);

    // A later marker inherits automatically; creation adds no scale mutation.
    overlay.update(
      [
        makeNode("node"),
        makeCenter("center"),
        makeAccount("spool"),
        makeNode("new-node"),
      ],
      true,
    );
    expect(
      visualFor("new-node")?.style.getPropertyValue("--map-object-scale"),
    ).toBe("");
    expect(containerSetProperty).toHaveBeenCalledTimes(1);

    overlay.destroy();
    expect(container.style.getPropertyValue("--map-object-scale")).toBe("");
  });

  it("skips shared scale DOM writes below the material-change threshold", () => {
    const { overlay, container } = makeOverlayWithContainer();
    overlay.updateZoom(13.5);
    const setProperty = vi.spyOn(container.style, "setProperty");

    overlay.updateZoom(13.51);
    expect(setProperty).not.toHaveBeenCalled();

    overlay.updateZoom(14.5);
    expect(setProperty).toHaveBeenCalledTimes(1);
  });

  it("writes the reference scale on initial map attachment", () => {
    vi.stubGlobal("document", {
      createElement: () => new FakeElement(),
    });
    const container = new FakeElement();
    const on = vi.fn();
    const map = {
      getContainer: () => container as unknown as HTMLElement,
      getZoom: () => 13.5,
      on,
      off: vi.fn(),
    } as unknown as MapLibreMap;

    const overlay = new NodesOverlay(
      map,
      FakeMarker as unknown as MarkerConstructor,
      weaveRuntime,
    );

    expect(container.style.getPropertyValue("--map-object-scale")).toBe(
      "1.000",
    );
    expect(on).toHaveBeenCalledWith("zoom", expect.any(Function));
    overlay.destroy();
  });

  it("restores a pre-existing container scale when the last overlay leaves", () => {
    const container = new FakeElement();
    container.style.setProperty("--map-object-scale", "0.777");
    const { overlay } = makeOverlayWithContainer(container);

    overlay.updateZoom(7);
    expect(container.style.getPropertyValue("--map-object-scale")).toBe(
      "0.620",
    );
    overlay.destroy();
    expect(container.style.getPropertyValue("--map-object-scale")).toBe(
      "0.777",
    );
  });

  it("does not let an older overlay clear a newer shared scale owner", () => {
    const container = new FakeElement();
    const older = makeOverlayWithContainer(container).overlay;
    const newer = makeOverlayWithContainer(container).overlay;

    older.updateZoom(7);
    newer.updateZoom(10);
    const newerScale = getMapMarkerScale(10).toFixed(3);
    expect(container.style.getPropertyValue("--map-object-scale")).toBe(
      newerScale,
    );

    older.destroy();
    expect(container.style.getPropertyValue("--map-object-scale")).toBe(
      newerScale,
    );

    newer.destroy();
    expect(container.style.getPropertyValue("--map-object-scale")).toBe("");
  });

  it("uses a fixed render signature instead of serializing unrelated fields", () => {
    const weave = makeWeave();
    expect(weaveRenderSignature({ ...weave, totalActiveThreadCount: 99 })).toBe(
      weaveRenderSignature(weave),
    );
    expect(weaveRenderSignature({ ...weave, proposalCount: 3 })).not.toBe(
      weaveRenderSignature(weave),
    );
  });

  it("keeps the render signature free of time-dependent opacity", () => {
    const weave = makeWeave({
      conversationThreadCount: 2,
      conversationOpacity: 0.9,
      proposalCount: 1,
      proposalArcs: [
        {
          subjectId: "proposal-a",
          proposalThreadCount: 1,
          conversationThreadCount: 0,
          voteThreadCount: 2,
          bundledSubjectCount: 1,
          latestActivityAtMs: 10,
          opacity: 0.9,
          color: "#5f7a55",
          startDeg: 55,
          spanDeg: 250,
        },
      ],
    });
    const aged: MapEntityWeave = {
      ...weave,
      conversationOpacity: 0.3,
      proposalArcs: weave.proposalArcs.map((arc) => ({ ...arc, opacity: 0.3 })),
    };

    expect(weaveRenderSignature(aged)).toBe(weaveRenderSignature(weave));
  });

  it("treats a changed proposal identity as a structural change", () => {
    const weave = makeWeave({
      proposalCount: 1,
      proposalArcs: [
        {
          subjectId: "proposal-a",
          proposalThreadCount: 1,
          conversationThreadCount: 0,
          voteThreadCount: 0,
          bundledSubjectCount: 1,
          latestActivityAtMs: 10,
          opacity: 1,
          color: "#5f7a55",
          startDeg: 55,
          spanDeg: 250,
        },
      ],
    });
    const reassigned: MapEntityWeave = {
      ...weave,
      proposalArcs: weave.proposalArcs.map((arc) => ({
        ...arc,
        subjectId: "proposal-b",
      })),
    };

    expect(weaveRenderSignature(reassigned)).not.toBe(
      weaveRenderSignature(weave),
    );
  });

  it("rebuilds arm overlays when the label changes under a stable id", () => {
    const base = makeWeave({
      armOverlays: [{ arm: "northwest", id: "overlay-1", label: "Notiz A" }],
    });
    const renamed = makeWeave({
      armOverlays: [
        { arm: "northwest", id: "overlay-1", label: 'Notiz "B" & <C>' },
      ],
    });
    expect(weaveRenderSignature(renamed)).not.toBe(weaveRenderSignature(base));

    const overlay = makeOverlay();
    overlay.update([makeNode("overlay-label", { weave: base })], true);
    const root = overlay.getActiveMarker("overlay-label")?.element.children[0]
      .children[0] as FakeElement | undefined;
    const before = root?.querySelectorAll(".woven-node__arm-overlay")[0];
    expect(before?.title).toBe("Notiz A");
    expect(before?.getAttribute("data-overlay-id")).toBe("overlay-1");

    overlay.update([makeNode("overlay-label", { weave: renamed })], true);
    const after = root?.querySelectorAll(".woven-node__arm-overlay")[0];
    expect(after?.title).toBe('Notiz "B" & <C>');
    expect(after?.getAttribute("data-overlay-id")).toBe("overlay-1");
    // Attribute APIs must keep raw text — no HTML/script injection path.
    expect(root?.innerHTML).not.toContain("<script");
    expect(after?.getAttribute("title")).toBeNull();
    expect(after?.title).toBe('Notiz "B" & <C>');
  });

  it("marks the conversation ring empty and invisible when no talks are active", () => {
    const overlay = makeOverlay();
    overlay.update(
      [
        makeNode("quiet", {
          weave: makeWeave({
            conversationThreadCount: 0,
            conversationOpacity: 0,
          }),
        }),
      ],
      true,
    );
    const root = overlay.getActiveMarker("quiet")?.element.children[0]
      .children[0] as HTMLElement | undefined;
    expect(root?.innerHTML).toContain(
      'class="woven-node__conversation is-empty" data-zone="conversation"',
    );
    expect(
      (root?.style as unknown as Record<string, string>)[
        "--weave-conversation-opacity"
      ],
    ).toBe("0");
  });

  it("rebuilds the woven body only for structural change, not for ageing", () => {
    const overlay = makeOverlay();
    const weave = makeWeave({
      conversationThreadCount: 2,
      conversationOpacity: 0.9,
    });
    overlay.update([makeNode("ageing", { weave })], true);
    const root = overlay.getActiveMarker("ageing")?.element.children[0]
      .children[0] as HTMLElement | undefined;

    // A sentinel that only a rebuild would overwrite.
    const sentinel = "<!-- kept -->";
    if (root) root.innerHTML = sentinel;
    overlay.update(
      [makeNode("ageing", { weave: { ...weave, conversationOpacity: 0.2 } })],
      true,
    );
    expect(root?.innerHTML).toBe(sentinel);
    expect(
      (root?.style as unknown as Record<string, string>)[
        "--weave-conversation-opacity"
      ],
    ).toBe("0.2");

    // A structural change must still rebuild.
    overlay.update(
      [makeNode("ageing", { weave: { ...weave, proposalCount: 4 } })],
      true,
    );
    expect(root?.innerHTML).not.toBe(sentinel);
  });

  it("does not rewrite an unchanged accessible label", () => {
    const overlay = makeOverlay();
    overlay.update([makeNode("a")], true);
    const element = overlay.getActiveMarker("a")
      ?.element as unknown as FakeElement;
    const setAttribute = vi.spyOn(element, "setAttribute");

    overlay.update([makeNode("a")], true);

    expect(setAttribute).not.toHaveBeenCalledWith(
      "aria-label",
      expect.any(String),
    );
  });

  it("updates the woven body without recreating the stable map marker", () => {
    const overlay = makeOverlay();
    overlay.update([makeNode("a")], true);
    const before = overlay.getActiveMarker("a");
    const rootBefore = before?.element.children[0].children[0] as
      | HTMLElement
      | undefined;

    overlay.update(
      [
        makeNode("a", {
          weave: makeWeave({
            proposalCount: 1,
            proposalArcs: [
              {
                subjectId: "proposal-a",
                proposalThreadCount: 1,
                conversationThreadCount: 0,
                voteThreadCount: 0,
                bundledSubjectCount: 1,
                latestActivityAtMs: 10,
                opacity: 1,
                color: "#5f7a55",
                startDeg: 55,
                spanDeg: 250,
              },
            ],
          }),
        }),
      ],
      true,
    );

    const after = overlay.getActiveMarker("a");
    expect(after?.marker).toBe(before?.marker);
    expect(after?.element.children[0].children[0]).toBe(rootBefore);
    expect(rootBefore?.dataset.proposalCount).toBe("1");
    expect(rootBefore?.innerHTML.match(/data-zone="proposal"/g)).toHaveLength(
      1,
    );
  });
});

describe("NodesOverlay Webgemeindezentrum marker", () => {
  it("renders a distinct desired-location marker without pretending confirmation", () => {
    const overlay = makeOverlay();
    overlay.update([makeCenter()], true);

    const element = overlay.getActiveMarker(
      "webgemeindezentrum-hammer-park",
    )?.element;
    expect(element?.classList.contains("marker-webgemeindezentrum")).toBe(true);
    expect(element?.dataset.markerCategory).toBe("webgemeindezentrum");
    expect(element?.dataset.locationState).toBe("desired");
    expect(element?.dataset.testid).toBe(
      "marker-webgemeindezentrum-webgemeindezentrum-hammer-park",
    );
    expect(element?.children[0].children[0].textContent).toBe("");
    expect(
      element?.children[0].children[0].classList.contains(
        "marker-webgemeindezentrum__icon",
      ),
    ).toBe(true);
    expect(
      (element?.children[0] as HTMLElement | undefined)?.style.borderStyle,
    ).toBe("dashed");
  });

  it("updates desired to confirmed styling without recreating the stable marker", () => {
    const overlay = makeOverlay();
    overlay.update([makeCenter()], true);
    const before = overlay.getActiveMarker("webgemeindezentrum-hammer-park");

    overlay.update(
      [
        {
          ...makeCenter(),
          location_state: "confirmed",
          location_state_label: "Bestätigter Treffort",
        },
      ],
      true,
    );

    const after = overlay.getActiveMarker("webgemeindezentrum-hammer-park");
    expect(after?.marker).toBe(before?.marker);
    expect(after?.element.dataset.locationState).toBe("confirmed");
    expect(
      (after?.element.children[0] as HTMLElement | undefined)?.style
        .borderStyle,
    ).toBe("");
  });

  it("recreates a marker when a stable id changes semantic category", () => {
    const overlay = makeOverlay();
    overlay.update([makeNode("shared")], true);
    const first = overlay.getActiveMarker("shared")
      ?.marker as unknown as FakeMarker;

    overlay.update([makeCenter("shared")], true);

    expect(first.removed).toBe(true);
    expect(
      overlay
        .getActiveMarker("shared")
        ?.element.classList.contains("marker-webgemeindezentrum"),
    ).toBe(true);
  });
});

describe("one on-demand chooser for overlapping map hit boxes", () => {
  function projectedOverlay(
    project: (coords: [number, number]) => { x: number; y: number },
  ) {
    const container = new FakeElement();
    const activate = vi.fn();
    const map = {
      getContainer: () => container as unknown as HTMLElement,
      project,
    } as unknown as MapLibreMap;
    vi.stubGlobal("document", { createElement: () => new FakeElement() });
    return {
      overlay: new NodesOverlay(
        map,
        FakeMarker as unknown as MarkerConstructor,
        weaveRuntime,
        activate,
      ),
      container,
      activate,
    };
  }

  function chooserOf(container: FakeElement): FakeElement | undefined {
    return container.children
      .filter((node) => node.dataset.testid === "map-object-chooser")
      .at(-1);
  }

  it("detects screen hit-box collisions, not exact address identity", () => {
    expect(screenHitBoxesOverlap({ x: 20, y: 20 }, { x: 63, y: 20 })).toBe(
      true,
    );
    expect(screenHitBoxesOverlap({ x: 20, y: 20 }, { x: 64, y: 20 })).toBe(
      false,
    );
    expect(screenHitBoxesOverlap({ x: 20, y: 20 }, { x: 20, y: -23 })).toBe(
      true,
    );
  });

  it("chooses distinct nodes, a Garnrolle and a center at the same public coordinates", () => {
    const { overlay, container, activate } = projectedOverlay(([lon, lat]) => ({
      x: lon * 100,
      y: lat * 100,
    }));
    const node = makeNode("a");
    const second = makeNode("b");
    const spool = { ...makeAccount("spool"), lat: node.lat, lon: node.lon };
    const center = { ...makeCenter(), lat: node.lat, lon: node.lon };
    overlay.update([node, spool, second, center], true);
    const element = overlay.getActiveMarker(node.id)
      ?.element as unknown as FakeElement;
    const click = element.fire("click");
    expect(click.stopPropagation).toHaveBeenCalledTimes(1);
    expect(activate).not.toHaveBeenCalled();
    const chooser = chooserOf(container);
    expect(chooser).toBeDefined();
    expect(
      chooser?.children[1].children.map((button) => button.textContent),
    ).toEqual([
      "Knoten: Node a",
      "Garnrolle: Garnrolle spool",
      "Knoten: Node b",
      "Gemeinschaftszentrum: Webgemeindezentrum Hammer Park",
    ]);
    const target = chooser?.children[1].children[3] as FakeElement;
    expect(target.focused).toBe(false);
    target.fire("click");
    expect(activate).toHaveBeenCalledWith(
      center,
      overlay.getActiveMarker(center.id)?.element,
    );
    expect(chooser?.removed).toBe(true);
    overlay.destroy();
  });

  it("keeps nearby but distinct coordinates separate when zoom resolves a pixel collision", () => {
    let scale = 500;
    const { overlay, container, activate } = projectedOverlay(([lon, lat]) => ({
      x: lon * scale,
      y: lat * scale,
    }));
    const first = makeNode("one");
    const second = makeNode("two", { lon: 10.03 });
    overlay.update([first, second], true);
    const element = overlay.getActiveMarker(first.id)
      ?.element as unknown as FakeElement;
    const overlapped = element.fire("click");
    expect(overlapped.stopPropagation).toHaveBeenCalled();
    const firstChooser = chooserOf(container);
    expect(firstChooser?.children[1].children.length).toBe(2);
    scale = 3000;
    const resolved = element.fire("click");
    expect(resolved.stopPropagation).not.toHaveBeenCalled();
    expect(firstChooser?.removed).toBe(true);
    expect(activate).not.toHaveBeenCalled();
    overlay.destroy();
  });

  it("keeps ordinary Garnrolle profile clicks direct while Alt+click lists nearby Garnrollen", () => {
    const { overlay, container } = projectedOverlay(([lon, lat]) => ({
      x: lon * 100,
      y: lat * 100,
    }));
    const first = makeAccount("visible");
    const fuzzy = {
      ...makeAccount("fuzzy"),
      lat: first.lat,
      lon: first.lon + 0.23, // 23px separation: each marker center remains clickable.
      map_state: "radius" as const,
    };
    overlay.update([first, fuzzy], true);
    const marker = overlay.getActiveMarker(first.id)
      ?.element as unknown as FakeElement;
    const direct = marker.fire("click", { detail: 1 });
    expect(direct.stopPropagation).not.toHaveBeenCalled();
    expect(chooserOf(container)).toBeUndefined();
    const alternatives = marker.fire("click", { detail: 1, altKey: true });
    expect(alternatives.stopPropagation).toHaveBeenCalledOnce();
    expect(chooserOf(container)?.children[1].children).toHaveLength(2);
    overlay.destroy();
  });

  it("opens a touch-reachable chooser for an exact Garnrolle and a same-point radius Garnrolle", () => {
    const { overlay, container } = projectedOverlay(([lon, lat]) => ({
      x: lon * 100,
      y: lat * 100,
    }));
    const exact = makeAccount("exact");
    const radius = {
      ...makeAccount("radius"),
      lon: exact.lon,
      lat: exact.lat,
      map_state: "radius" as const,
    };
    overlay.update([exact, radius], true);
    const marker = overlay.getActiveMarker(exact.id)
      ?.element as unknown as FakeElement;
    const click = marker.fire("click", { detail: 1 });
    expect(click.stopPropagation).toHaveBeenCalledOnce();
    expect(chooserOf(container)?.children[1].children).toHaveLength(2);
    overlay.destroy();
  });

  it("uses expanded household screen offsets rather than false geographic collisions", () => {
    const { overlay, container } = projectedOverlay(([lon, lat]) => ({
      x: lon * 100,
      y: lat * 100,
    }));
    Object.assign(container, { clientWidth: 600, clientHeight: 700 });
    const anchorNode = makeNode("near-house", { lon: 10.01, lat: 53.51 });
    const members = Array.from({ length: 4 }, (_, i) => ({
      ...makeAccount(`resident-${i}`),
      map_state: "exact" as const,
    }));
    overlay.update([anchorNode, ...members], true);
    (
      overlay.getActiveMarker(members[3].id)?.element as unknown as FakeElement
    ).fire("dblclick");
    expect(overlay.getActiveMarker(members[0].id)).toBeDefined();
    expect(overlay.getActiveMarker(members[3].id)).toBeDefined();
    // Displayed x offsets are -102,-34,34,102px (68px spacing).
    const nodeMarker = overlay.getActiveMarker(anchorNode.id)
      ?.element as unknown as FakeElement;
    const click = nodeMarker.fire("click", { detail: 1 });
    expect(click.stopPropagation).toHaveBeenCalledOnce();
    const choices = chooserOf(container)?.children[1].children.map(
      (button) => button.textContent,
    );
    expect(choices).toEqual([
      "Knoten: Node near-house",
      "Garnrolle: Garnrolle resident-1",
      "Garnrolle: Garnrolle resident-2",
    ]);
    overlay.destroy();
  });

  it("never turns an unlisted, private or revoked object into a choice", () => {
    const { overlay, container } = projectedOverlay(([lon, lat]) => ({
      x: lon,
      y: lat,
    }));
    const visible = makeNode("authorized");
    // The browser only receives the server-authorized view; an excluded
    // object is not in latestPoints and must not enter the chooser.
    const excluded = makeNode("private");
    overlay.update([visible], true);
    const click = (
      overlay.getActiveMarker(visible.id)?.element as unknown as FakeElement
    ).fire("click");
    expect(click.stopPropagation).not.toHaveBeenCalled();
    expect(chooserOf(container)).toBeUndefined();
    expect(overlay.getActiveMarker(excluded.id)).toBeUndefined();
    overlay.destroy();
  });

  it("preserves the existing household stack, Escape and selected context", () => {
    const { overlay, container, activate } = projectedOverlay(([lon, lat]) => ({
      x: lon * 100,
      y: lat * 100,
    }));
    const node = makeNode("knoten");
    const exact = (id: string): MapEntityGarnrolle => ({
      ...makeAccount(id),
      lat: node.lat,
      lon: node.lon,
      map_state: "exact",
    });
    overlay.update([node, exact("left"), exact("right")], true);
    expect(overlay.getActiveMarker("left")).toBeUndefined();
    const front = overlay.getActiveMarker("right")
      ?.element as unknown as FakeElement;
    expect(front.dataset.householdCount).toBe("2");
    overlay.updateSelection(node.id);
    const selected = overlay.getActiveMarker(node.id)
      ?.element as unknown as FakeElement;
    const directClick = selected.fire("click");
    expect(directClick.stopPropagation).not.toHaveBeenCalled();
    expect(chooserOf(container)).toBeUndefined();
    const keyboardClick = selected.fire("click", { detail: 0 });
    expect(keyboardClick.stopPropagation).not.toHaveBeenCalled();
    expect(chooserOf(container)).toBeUndefined();
    const chooserClick = selected.fire("click", { altKey: true });
    expect(chooserClick.stopPropagation).toHaveBeenCalled();
    expect(chooserOf(container)).toBeDefined();
    container.fire("keydown", { key: "Escape" });
    expect(chooserOf(container)?.removed).toBe(true);
    expect(activate).not.toHaveBeenCalled();
    overlay.destroy();
  });

  it("renders only one page of controls even when many hits overlap", () => {
    const { overlay, container, activate } = projectedOverlay(([lon, lat]) => ({
      x: lon,
      y: lat,
    }));
    const points = Array.from({ length: 65 }, (_, n) => makeNode(String(n)));
    overlay.update(points, true);
    (overlay.getActiveMarker("0")?.element as unknown as FakeElement).fire(
      "click",
    );
    const chooser = chooserOf(container);
    expect(chooser?.children[1].children.length).toBe(20);
    const pager = chooser?.children[2] as FakeElement;
    expect(pager.children.map((x) => x.textContent)).toEqual([
      "Zurück",
      "Seite 1 von 4",
      "Weiter",
    ]);
    pager.children[2].fire("click");
    expect(chooser?.children[1].children.length).toBe(20);
    expect(chooser?.children[1].children[0].focused).toBe(true);
    pager.children[2].fire("click");
    pager.children[2].fire("click");
    expect(chooser?.children[1].children.length).toBe(5);
    chooser?.children[1].children[4].fire("click");
    expect(activate).toHaveBeenCalledWith(
      points[64],
      overlay.getActiveMarker(points[64].id)?.element,
    );
    overlay.destroy();
  });
});
