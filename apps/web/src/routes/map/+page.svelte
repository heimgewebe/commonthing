<script lang="ts">
  import { onDestroy, onMount, tick } from "svelte";
  import type { PageData } from "./$types";
  import "$lib/styles/tokens.css";
  import "maplibre-gl/dist/maplibre-gl.css";
  import type { Map as MapLibreMap } from "maplibre-gl";

  import TopBar from "$lib/components/TopBar.svelte";
  import MapRouteOverlays from "$lib/components/map/MapRouteOverlays.svelte";
  import MapRouteStatus from "$lib/components/map/MapRouteStatus.svelte";
  import MapRouteSurface from "$lib/components/map/MapRouteSurface.svelte";
  import {
    resolveMapDomainChange,
    type MapDomainChanged,
    type RelatedMapSelection,
  } from "$lib/components/map/mapRouteEvents";
  import type { NodeSearchStatus } from "$lib/api/search";
  import {
    summarizeMapResourceStatus,
    type MapEdge,
    type MapEntityViewModel,
    type MapResourceStatus,
    type Node,
  } from "$lib/map/types";
  import {
    deriveSearchDirectionIndicators,
    resolveInitialMapCamera,
    type SearchDirectionIndicator,
    type ViewportBounds,
  } from "$lib/map/searchNavigation";

  import { page } from "$app/stores";
  import { invalidate } from "$app/navigation";

  import {
    view,
    selection,
    systemState,
    contextPanelOpen,
    enterFokus,
    enterKomposition,
    leaveToNavigation,
    lastCreatedNodeId,
  } from "$lib/stores/uiView";
  import {
    mapContentFilters,
    isFilterOpen,
    closeFilter,
  } from "$lib/stores/filterStore";
  import {
    isSearchOpen,
    searchQuery,
    closeSearch,
  } from "$lib/stores/searchStore";
  import {
    openSearchExclusive,
    openFilterExclusive,
  } from "$lib/stores/overlayManager";
  import { mapChrome } from "$lib/stores/mapChrome";
  import {
    parseMapUrlState,
    type MapUrlFocus,
    type ParsedMapUrlState,
  } from "$lib/map/urlState";
  import {
    deriveMarkerCounts,
    deriveSearchResults,
    deriveSearchMatchIds,
    deriveWeaveEdges,
    deriveLineEdges,
    selectMapEntity,
  } from "$lib/stores/mapView";
  import {
    evaluateMapContentFilters,
    getMapContentType,
  } from "$lib/map/contentFilters";
  import { get } from "svelte/store";
  import { knottingTopicTag } from "$lib/knottingTopics";
  import {
    nodeViewportContains,
    type NodeViewportBounds,
  } from "$lib/map/nodeViewportBounds";

  import { currentBasemap } from "$lib/map/config/basemap.current";
  import { resolveBasemapStyle, rewritePmtilesUrl } from "$lib/map/basemap";
  import {
    observeDocumentColorScheme,
    readDocumentColorScheme,
    type ColorScheme,
  } from "$lib/map/colorScheme";
  import { hasRenderableMapPosition } from "$lib/map/coordinates";
  import { areMapEdgesVisuallyEnabled } from "$lib/map/edgeVisibility";
  import {
    resolveMapInitFailure,
    scheduleMapInitTimeout,
  } from "$lib/map/mapInitFailure";
  import { registerPmtilesProtocol } from "$lib/map/pmtilesProtocol";
  import { applyNodeUpdateOverrides, buildMapScene } from "$lib/map/scene";

  import type { NodesOverlay as NodesOverlayController } from "$lib/map/overlay/nodes";
  import {
    hasCompleteEdgeThreadStyle,
    updateEdges,
  } from "$lib/map/overlay/edges";
  import type {
    EdgeMotionController,
    EdgeMotionInput,
  } from "$lib/map/overlay/edgeMotion";
  import {
    FADEN_PROJECTION_REFRESH_MS,
    nextEdgeExpiryAt,
  } from "$lib/map/edgeLifecycle";
  import { setupKompositionInteraction } from "$lib/map/overlay/komposition";
  import { setupFocusInteraction } from "$lib/map/overlay/focus";

  interface Props {
    data: PageData;
  }

  let { data }: Props = $props();

  let nodeUpdateOverrides: Record<string, Node> = $state({});

  // T007: node search comes from the authorized T006 server contract. Public
  // non-node structures (Garnrollen and Webgemeindezentren) are searched only
  // inside the map projection already delivered to this client; node visibility
  // is never reconstructed from the downloaded scene.
  // The search client itself is lazy-loaded so opening the map does not pay the
  // search runtime cost before a person actually starts searching.
  let nodeSearchItems: MapEntityViewModel[] = $state([]);
  let nodeSearchStatus: NodeSearchStatus = $state("idle");
  let nodeSearchMode: string | null = $state(null);
  let nodeSearchFallbackReason: string | null = $state(null);
  let nodeSearchTimer: ReturnType<typeof setTimeout> | undefined;
  let nodeSearchAbortController: AbortController | null = null;
  let nodeSearchSequence = 0;

  function resetNodeSearch() {
    if (nodeSearchTimer !== undefined) clearTimeout(nodeSearchTimer);
    nodeSearchTimer = undefined;
    nodeSearchAbortController?.abort();
    nodeSearchAbortController = null;
    nodeSearchSequence += 1;
    nodeSearchItems = [];
    nodeSearchStatus = "idle";
    nodeSearchMode = null;
    nodeSearchFallbackReason = null;
  }

  function scheduleNodeSearch(
    query: string,
    kinds: string[],
    tags: string[],
    enabled: boolean,
    open: boolean,
  ) {
    resetNodeSearch();
    const normalizedQuery = query.trim();
    if (!open || !enabled || !normalizedQuery) return;
    const sequence = nodeSearchSequence;
    nodeSearchStatus = "loading";
    nodeSearchTimer = setTimeout(async () => {
      nodeSearchTimer = undefined;
      const controller = new AbortController();
      nodeSearchAbortController = controller;
      try {
        const { nodeToMapEntity, searchNodes } =
          await import("$lib/api/search");
        const response = await searchNodes(normalizedQuery, {
          kinds,
          tags,
          signal: controller.signal,
        });
        if (controller.signal.aborted || sequence !== nodeSearchSequence)
          return;
        nodeSearchItems = response.items.map(nodeToMapEntity);
        nodeSearchStatus = "ready";
        nodeSearchMode = response.mode;
        nodeSearchFallbackReason = response.fallback_reason ?? null;
      } catch {
        if (controller.signal.aborted || sequence !== nodeSearchSequence)
          return;
        nodeSearchStatus = "error";
      } finally {
        if (nodeSearchAbortController === controller)
          nodeSearchAbortController = null;
      }
    }, 180);
  }

  onDestroy(resetNodeSearch);

  let mapContainer: HTMLDivElement | null = $state(null);
  let map: MapLibreMap | null = $state(null);
  let mapStyleReady = $state(false);
  let mapViewportRevision = $state(0);
  let isLoading = $state(true);
  let mapInitFailed = $state(false);
  // True only after MapLibre emitted its first successful `load`.
  let mapHasLoaded = false;
  let edgeProjectionNow = $state(Date.now());
  let edgeExpiryTimeout: ReturnType<typeof setTimeout> | undefined;
  let lastFocusedElement: HTMLElement | null = $state(null);

  let nodesOverlay: NodesOverlayController | null = $state(null);
  let projectMapMarkerViewsForWeave:
    | typeof import("$lib/map/overlay/nodes").projectMapMarkerViewsForWeave
    | null = $state(null);
  let edgeMotion: EdgeMotionController | null = null;
  let resolveMotionInput:
    | typeof import("$lib/map/overlay/edgeMotion").resolveEdgeMotionInput
    | null = null;
  let searchDirectionIndicators: SearchDirectionIndicator[] = $state([]);
  let searchDirectionFrame: number | null = null;
  let usableSearchViewportCache: ViewportBounds | null = null;
  let searchViewportGeometryDirty = true;
  let searchViewportResizeObserver: ResizeObserver | null = null;
  let viewportNodes: Node[] = $state([]);
  let viewportNodeStatus: MapResourceStatus | null = $state(null);
  let viewportNodeCoverage: NodeViewportBounds | null = null;
  let viewportNodeAbortController: AbortController | null = null;
  let viewportNodeSequence = 0;
  let requestNodeViewportRefresh: (() => void) | null = null;
  let pendingViewportRefresh = false;
  let viewportRouteStatusInitialized = false;
  let lastViewportRouteStatus: PageData["resourceStatus"] | null = null;
  let lastViewportBootstrapKey: string | null = null;
  let viewportBootstrapReleased = $state(false);

  function measureUsableSearchViewport(): ViewportBounds | null {
    if (!mapContainer) return null;
    const mapRect = mapContainer.getBoundingClientRect();
    const edgeInset = 28;
    let left = edgeInset;
    let top = edgeInset;
    let right = mapRect.width - edgeInset;
    let bottom = mapRect.height - edgeInset;

    const topBarRect = document
      .querySelector<HTMLElement>(".topbar")
      ?.getBoundingClientRect();
    if (topBarRect)
      top = Math.max(top, topBarRect.bottom - mapRect.top + edgeInset);

    for (const selector of [
      '[data-testid="attention-overflow-menu"]',
      '[data-testid="attention-card"]',
    ]) {
      const attentionRect = document
        .querySelector<HTMLElement>(selector)
        ?.getBoundingClientRect();
      if (attentionRect && attentionRect.height > 0) {
        top = Math.max(top, attentionRect.bottom - mapRect.top + edgeInset);
      }
    }

    const searchRect = document
      .querySelector<HTMLElement>('[data-testid="search-overlay"]')
      ?.getBoundingClientRect();
    if (searchRect) {
      top = Math.max(top, searchRect.bottom - mapRect.top + edgeInset);
    }

    const filterRect = document
      .querySelector<HTMLElement>('[data-testid="filter-overlay"]')
      ?.getBoundingClientRect();
    if (filterRect) {
      top = Math.max(top, filterRect.bottom - mapRect.top + edgeInset);
    }

    const toolFan = document.querySelector<HTMLElement>(
      '[data-testid="tool-fan"]',
    );
    const toolObstacle =
      toolFan?.dataset.expanded === "true"
        ? document.getElementById("tool-fan-actions")
        : document.querySelector<HTMLElement>(
            '[data-testid="tool-fan-trigger"]',
          );
    const toolRect = toolObstacle?.getBoundingClientRect();
    if (toolRect && toolRect.height > 0) {
      bottom = Math.min(bottom, toolRect.top - mapRect.top - edgeInset);
    }

    const panelRect = document
      .querySelector<HTMLElement>('[data-testid="context-panel"]')
      ?.getBoundingClientRect();
    if (panelRect && panelRect.left > mapRect.left) {
      right = Math.min(right, panelRect.left - mapRect.left - edgeInset);
    }

    return right > left && bottom > top ? { left, top, right, bottom } : null;
  }

  function getUsableSearchViewport(): ViewportBounds | null {
    if (searchViewportGeometryDirty) {
      usableSearchViewportCache = measureUsableSearchViewport();
      searchViewportGeometryDirty = false;
    }
    return usableSearchViewportCache;
  }

  function invalidateSearchViewportGeometry() {
    searchViewportGeometryDirty = true;
    scheduleSearchDirectionIndicators();
  }

  function refreshSearchViewportObservers() {
    if (!searchViewportResizeObserver || !mapContainer) return;
    searchViewportResizeObserver.disconnect();
    searchViewportResizeObserver.observe(mapContainer);
    for (const selector of [
      ".topbar",
      '[data-testid="search-overlay"]',
      '[data-testid="filter-overlay"]',
      '[data-testid="attention-overflow-menu"]',
      '[data-testid="attention-card"]',
      '[data-testid="tool-fan"]',
      "#tool-fan-actions",
      '[data-testid="context-panel"]',
    ]) {
      const element = document.querySelector<HTMLElement>(selector);
      if (element) searchViewportResizeObserver.observe(element);
    }
    invalidateSearchViewportGeometry();
  }

  function updateSearchDirectionIndicators() {
    if (
      !map ||
      !$isSearchOpen ||
      !$searchQuery.trim() ||
      filteredResults.length === 0
    ) {
      searchDirectionIndicators = [];
      return;
    }
    const bounds = getUsableSearchViewport();
    if (!bounds) {
      searchDirectionIndicators = [];
      return;
    }
    const projected = filteredResults
      .filter((item) => hasRenderableMapPosition(item))
      .map((item) => ({
        item,
        point: map!.project([item.lon, item.lat]),
      }));
    searchDirectionIndicators = deriveSearchDirectionIndicators(
      projected,
      bounds,
    );
  }

  function scheduleSearchDirectionIndicators() {
    if (searchDirectionFrame !== null) return;
    searchDirectionFrame = window.requestAnimationFrame(() => {
      searchDirectionFrame = null;
      updateSearchDirectionIndicators();
    });
  }

  function syncEdgeMotionProjection() {
    if (!edgeMotion) return;
    edgeMotion.syncCanonicalEdges(scene.edges);
    edgeMotion.setVisibleEdgeIds(
      edgesVisuallyEnabled
        ? new Set(lineEdges.map((edge) => edge.id))
        : new Set(),
    );
  }

  function scheduleNextEdgeExpiryRefresh(edges = weaveEdges) {
    if (edgeExpiryTimeout !== undefined) {
      clearTimeout(edgeExpiryTimeout);
      edgeExpiryTimeout = undefined;
    }
    const nowMs = Date.now();
    const nextExpiryAt = nextEdgeExpiryAt(edges, nowMs);
    if (nextExpiryAt == null) return;
    edgeExpiryTimeout = setTimeout(
      () => {
        // The fired timeout is no longer pending. Clearing the handle before
        // the shared refresh reschedules keeps exactly one expiry timer alive.
        edgeExpiryTimeout = undefined;
        refreshEdgeProjection();
      },
      Math.max(0, nextExpiryAt - nowMs),
    );
  }

  /**
   * The single way the projection instant moves. A thread can expire while it
   * is filtered away or while the tab is in the background; whoever brings it
   * back on screen must read the current time, not the one from before the
   * gap. Every trigger — thread change, minute interval, exact expiry timer and
   * the return of a hidden tab — therefore goes through here, and each one also
   * re-plans the next exact expiry.
   */
  function refreshEdgeProjection() {
    edgeProjectionNow = Date.now();
    scheduleNextEdgeExpiryRefresh(weaveEdges);
  }

  function focusAndFlyToPoint(item: MapEntityViewModel) {
    // Selection state is owned by the map-view store (delegating to uiView);
    // the route only keeps the map-side concern (flyTo).
    selectMapEntity(item);

    if (map && hasRenderableMapPosition(item)) {
      const currentZoom = map.getZoom();
      map.flyTo({
        center: [item.lon, item.lat],
        zoom: Math.max(currentZoom, 14),
        speed: 0.8,
        curve: 1,
      });
    }
  }

  function handleSearchSelect(event: CustomEvent<MapEntityViewModel>) {
    focusAndFlyToPoint(event.detail);
  }

  function handleSearchDirectionSelect(event: CustomEvent<MapEntityViewModel>) {
    closeSearch();
    focusAndFlyToPoint(event.detail);
  }

  async function handleRelatedSelect(event: CustomEvent<RelatedMapSelection>) {
    const related =
      event.detail.data ??
      markersData.find(
        (item) =>
          item.id === event.detail.id && item.type === event.detail.type,
      );
    if (related) {
      focusAndFlyToPoint(related);
      return;
    }

    // Public relations may point to Garnrollen that intentionally have no map
    // position. Open their panel without inventing coordinates or a fly-to.
    enterFokus({
      type: event.detail.type,
      id: event.detail.id,
      data: event.detail.title ? { title: event.detail.title } : undefined,
    });
    await tick();
    document
      .querySelector<HTMLElement>('[data-testid="account-heading"]')
      ?.focus();
  }

  function edgeMotionInput(edge: MapEdge): EdgeMotionInput | null {
    // Projected weave palette (same derivation as static threads), fail-closed
    // until the projector is ready. Never resolve from raw unprojected markers.
    if (!resolveMotionInput || !motionMarkersData) return null;
    return resolveMotionInput(edge, motionMarkersData);
  }

  function animateEdgesForNode(
    nodeId: string,
    phase: "creating" | "releasing",
  ) {
    if (!edgeMotion) return;
    for (const edge of scene.edges) {
      if (edge.source_id !== nodeId && edge.target_id !== nodeId) continue;
      const input = edgeMotionInput(edge);
      if (!input) continue;
      if (phase === "creating") edgeMotion.startCreate(input);
      else edgeMotion.startRelease(input);
    }
  }

  function animateEdgeById(
    edgeId: string,
    phase: "creating" | "releasing",
  ): boolean {
    if (!edgeMotion) return false;
    const edge = scene.edges.find((candidate) => candidate.id === edgeId);
    if (!edge) return false;
    const input = edgeMotionInput(edge);
    if (!input) return false;
    if (phase === "creating") edgeMotion.startCreate(input);
    else edgeMotion.startRelease(input);
    return true;
  }

  async function handleDomainChanged(event: CustomEvent<MapDomainChanged>) {
    const resolution = resolveMapDomainChange(event.detail);
    if (resolution.kind === "local-node-update") {
      nodeUpdateOverrides = {
        ...nodeUpdateOverrides,
        [resolution.node.id]: resolution.node,
      };
      return;
    }

    const removesNode =
      event.detail.kind === "node" &&
      (event.detail.action === "deleted" || event.detail.action === "archived");
    if (removesNode) {
      // Capture the existing canonical geometry before invalidation removes it.
      animateEdgesForNode(event.detail.id, "releasing");
    }
    // Archived removals deliberately keep the panel alive so its archive
    // receipt remains reachable; only a hard deletion closes the selection.
    if (event.detail.action === "deleted") leaveToNavigation();
    await invalidate("weltgewebe:domain-data");
  }

  // --- URL addressing (UI Interaction Doctrine, first slice) -----------------
  // The URL is an addressing layer, not a second state machine: it maps query
  // parameters onto the existing uiView / overlay stores. uiView stays the
  // single source of truth and there is no store -> URL synchronisation here.
  //
  // Two intents are kept strictly apart:
  //  - Immediate intents (compose=node, lens=filter|search) need no map data and
  //    are applied as soon as the URL is known. They also leave any stale
  //    focus/composition state so the addressed surface always starts clean.
  //  - Focus intents (focus=node|garnrolle|account|zentrum:<id>) need the scene entities
  //    and only count as resolved once their target is actually found.
  // Priority is compose > focus > lens. A valid-but-unresolved focus deliberately
  // blocks the lens fallback so a deep link never lands on the wrong surface.
  function findMapEntityForFocus(
    focus: MapUrlFocus,
    items: MapEntityViewModel[],
  ): MapEntityViewModel | null {
    return (
      items.find((item) => item.type === focus.type && item.id === focus.id) ??
      null
    );
  }

  function applyImmediateMapUrlAddressing(parsed: ParsedMapUrlState) {
    if (parsed.compose) {
      closeSearch();
      closeFilter();
      enterKomposition({
        mode: parsed.compose === "garnrolle" ? "place-garnrolle" : "new-knoten",
        source: "tool-fan",
      });
      return;
    }
    // A valid focus takes precedence: leave stale focus/composition and close
    // overlays now; the focus pass resolves the target once data is available.
    // While it is unresolved, no lower-priority lens fallback may run.
    if (parsed.focus) {
      closeSearch();
      closeFilter();
      leaveToNavigation();
      return;
    }
    // Lens-only (or empty / invalid-lens) URL: leave any prior focus/composition
    // first. If the URL still addresses a valid lens, use the exclusive overlay
    // helpers so cross-overlay focus-restore suppression stays intact (and the
    // search query is not cleared unnecessarily). If it addresses no valid lens,
    // close stale lens overlays — this matters for same-route/popstate
    // transitions (e.g. /map?lens=filter -> /map) where stores are not reset.
    leaveToNavigation();
    if (parsed.lens === "filter") {
      openFilterExclusive();
      return;
    }
    if (parsed.lens === "search") {
      openSearchExclusive();
      return;
    }
    closeSearch();
    closeFilter();
  }

  function tryApplyFocusMapUrlAddressing(
    parsed: ParsedMapUrlState,
    items: MapEntityViewModel[],
  ): boolean {
    if (!parsed.focus) return true;
    const item = findMapEntityForFocus(parsed.focus, items);
    if (!item) return false;
    closeSearch();
    closeFilter();
    focusAndFlyToPoint(item);
    return true;
  }

  // Separate locks: immediate intents fire once per distinct query; focus only
  // locks once its target is resolved, and retries while it stays unresolved.
  // `null` sentinels (not '') ensure the very first render — including plain
  // `/map` with an empty query — runs through the effect and leaves stale state.
  let lastAppliedImmediateUrlSearch: string | null = $state(null);
  let lastResolvedFocusUrlSearch: string | null = $state(null);

  let cleanupKomposition: (() => void) | undefined = undefined;
  let cleanupFocus: (() => void) | undefined = undefined;
  let unsubscribeSysState: (() => void) | undefined = undefined;

  const shouldExposeTestMap =
    import.meta.env.DEV ||
    import.meta.env.VITE_PUBLIC_ENABLE_TEST_MAP === "true";

  onMount(() => {
    let releasePmtilesProtocol: (() => void) | undefined;
    let cleanupAuthCamera: (() => void) | undefined;
    let authCameraMoved = false;
    // onMount returns its cleanup synchronously while the initialiser below is
    // still suspended on `await import('maplibre-gl')`. If the component is
    // destroyed in that window the cleanup finds `map`/`nodesOverlay` still
    // undefined and tears down nothing, and the initialiser would then go on to
    // build a map, bind listeners and register protocols that nobody ever
    // removes. Every await in the initialiser therefore re-checks this flag.
    let destroyed = false;
    // A terminal init failure owns this mount. Late async imports may settle,
    // but they must not construct or revive a map after the failure UI won.
    let mapInitTerminated = false;
    // One minute changes a seven-day linear opacity by less than 0.0001.
    // Exact expiry remains a separate timeout and is therefore not rounded.
    const edgeDecayInterval = window.setInterval(
      refreshEdgeProjection,
      FADEN_PROJECTION_REFRESH_MS,
    );
    // A background tab throttles both the interval and the expiry timeout. On
    // return the projection instant is re-read before anything is drawn, so an
    // expired thread cannot reappear.
    const handleVisibilityChange = () => {
      if (document.visibilityState === "visible") refreshEdgeProjection();
    };
    document.addEventListener("visibilitychange", handleVisibilityChange);
    // Hoisted so the cleanup can clear it; otherwise a component destroyed
    // before the style loads leaves a 10s timer pointing at dead state.
    let loadingTimeout: ReturnType<typeof setTimeout> | undefined = undefined;

    let stopColorSchemeObserver: (() => void) | undefined;
    let activeBasemapScheme: ColorScheme = readDocumentColorScheme();
    // Generation counter makes rapid light/dark toggles race-safe: only the
    // latest setStyle completion is allowed to rehydrate overlays.
    let basemapStyleGeneration = 0;

    function teardownMapRuntime() {
      stopColorSchemeObserver?.();
      stopColorSchemeObserver = undefined;
      cleanupKomposition?.();
      cleanupKomposition = undefined;
      cleanupFocus?.();
      cleanupFocus = undefined;
      unsubscribeSysState?.();
      unsubscribeSysState = undefined;
      cleanupAuthCamera?.();
      cleanupAuthCamera = undefined;
      searchViewportResizeObserver?.disconnect();
      searchViewportResizeObserver = null;
      viewportNodeAbortController?.abort();
      viewportNodeAbortController = null;
      viewportNodeCoverage = null;
      viewportNodeSequence += 1;
      requestNodeViewportRefresh = null;
      if (searchDirectionFrame !== null) {
        window.cancelAnimationFrame(searchDirectionFrame);
        searchDirectionFrame = null;
      }
      searchDirectionIndicators = [];
      nodesOverlay?.destroy();
      nodesOverlay = null;
      projectMapMarkerViewsForWeave = null;
      edgeMotion?.destroy();
      edgeMotion = null;
      resolveMotionInput = null;
      if (map) {
        map.off("move", handleSearchMapMove);
        map.off("moveend", handleNodeViewportMoveEnd);
        map.off("resize", handleSearchMapResize);
        map.off("styledata", handleMapStyleData);
        map.off("idle", rehydrateMapOverlays);
        if (typeof map.remove === "function") map.remove();
        map = null;
      }
      mapStyleReady = false;
      mapContainer?.removeEventListener("click", handleMarkerClick);
      releasePmtilesProtocol?.();
      releasePmtilesProtocol = undefined;
    }

    const failMapInit = (
      reason: Parameters<typeof resolveMapInitFailure>[1],
      error?: unknown,
    ) => {
      if (destroyed || mapInitTerminated) return;
      if (resolveMapInitFailure(mapHasLoaded, reason) !== "fail") {
        if (error !== undefined) {
          console.error("Nicht-terminaler Kartenfehler", error);
        }
        return;
      }
      if (error !== undefined) {
        console.error("Karteninitialisierung fehlgeschlagen", error);
      }
      if (loadingTimeout !== undefined) {
        clearTimeout(loadingTimeout);
        loadingTimeout = undefined;
      }
      mapInitTerminated = true;
      teardownMapRuntime();
      isLoading = false;
      mapInitFailed = true;
    };
    const handleSearchMapMove = () => {
      authCameraMoved = true;
      scheduleSearchDirectionIndicators();
    };
    const handleSearchMapResize = () => {
      mapViewportRevision += 1;
      invalidateSearchViewportGeometry();
    };
    const refreshNodeViewport = async (force = false) => {
      if (!map || data.nodeLoadMode !== "viewport" || destroyed) return;
      const bounds = map.getBounds();
      const requestBounds: NodeViewportBounds = {
        west: bounds.getWest(),
        south: bounds.getSouth(),
        east: bounds.getEast(),
        north: bounds.getNorth(),
      };
      if (
        !force &&
        viewportNodeAbortController === null &&
        viewportNodeCoverage !== null &&
        nodeViewportContains(viewportNodeCoverage, requestBounds)
      ) {
        return;
      }
      const sequence = ++viewportNodeSequence;
      viewportNodeAbortController?.abort();
      viewportNodeCoverage = null;
      const controller = new AbortController();
      viewportNodeAbortController = controller;
      try {
        const { fetchNodeViewport } = await import("$lib/map/nodeViewport");
        const result = await fetchNodeViewport(
          (url) => fetch(url, { signal: controller.signal }),
          import.meta.env.PUBLIC_GEWEBE_API_BASE ?? "",
          requestBounds,
        );
        if (
          destroyed ||
          controller.signal.aborted ||
          sequence !== viewportNodeSequence
        )
          return;
        viewportNodes = result.items;
        if (!viewportBootstrapReleased) {
          const viewportIds = new Set(result.items.map((node) => node.id));
          viewportBootstrapReleased = (data.nodes ?? []).every((node) =>
            viewportIds.has(node.id),
          );
        }
        viewportNodeCoverage =
          result.status === "complete" ? requestBounds : null;
        viewportNodeStatus =
          result.status === "complete"
            ? {
                resource: "nodes",
                status: "viewport",
                loaded: result.items.length,
                pages: result.pages,
              }
            : {
                resource: "nodes",
                status: "truncated",
                loaded: result.items.length,
                pages: result.pages,
                reason: result.reason,
              };
      } catch (error) {
        if (
          destroyed ||
          controller.signal.aborted ||
          sequence !== viewportNodeSequence
        )
          return;
        viewportNodeCoverage = null;
        viewportNodeStatus = {
          resource: "nodes",
          status: "failed",
          error: error instanceof Error ? error.message : String(error),
        };
      } finally {
        if (viewportNodeAbortController === controller)
          viewportNodeAbortController = null;
      }
    };
    requestNodeViewportRefresh = () => {
      void refreshNodeViewport(true);
    };
    const handleNodeViewportMoveEnd = () => {
      mapViewportRevision += 1;
      if (!mapHasLoaded) {
        pendingViewportRefresh = true;
        return;
      }
      void refreshNodeViewport();
    };
    let styleRehydrateQueued = false;
    let styleRehydrateGeneration = 0;
    const hasCanonicalEdgeStyle = () => hasCompleteEdgeThreadStyle(map);
    const rehydrateMapOverlays = () => {
      styleRehydrateQueued = false;
      if (destroyed || !map || hasCanonicalEdgeStyle()) return;
      if (styleRehydrateGeneration !== basemapStyleGeneration) return;
      if (!projectedMarkersData) return;
      updateEdges(
        map,
        lineEdges,
        projectedMarkersData,
        edgesVisuallyEnabled,
        edgeProjectionNow,
      );
      syncEdgeMotionProjection();
    };
    const queueOverlayRehydrate = (generation: number) => {
      if (
        destroyed ||
        styleRehydrateQueued ||
        !map ||
        !map.isStyleLoaded() ||
        hasCanonicalEdgeStyle() ||
        generation !== basemapStyleGeneration
      ) {
        return;
      }
      styleRehydrateQueued = true;
      styleRehydrateGeneration = generation;
      queueMicrotask(rehydrateMapOverlays);
    };
    const finishInitialLoading = (generation: number) => {
      if (
        destroyed ||
        mapInitTerminated ||
        !isLoading ||
        generation !== basemapStyleGeneration
      ) {
        return;
      }
      const currentScheme = readDocumentColorScheme();
      if (currentScheme !== activeBasemapScheme) {
        switchBasemapScheme(currentScheme);
        return;
      }
      if (pendingViewportRefresh) {
        pendingViewportRefresh = false;
        lastViewportRouteStatus = data.resourceStatus ?? null;
        void refreshNodeViewport().then(() => finishInitialLoading(generation));
        return;
      }
      mapHasLoaded = true;
      if (loadingTimeout !== undefined) {
        clearTimeout(loadingTimeout);
        loadingTimeout = undefined;
      }
      isLoading = false;
      mapStyleReady = true;
      invalidateSearchViewportGeometry();
    };
    const handleMapStyleData = () => {
      queueOverlayRehydrate(basemapStyleGeneration);
    };
    function switchBasemapScheme(scheme: ColorScheme) {
      if (!map || destroyed || mapInitTerminated) return;
      if (scheme === activeBasemapScheme) return;
      activeBasemapScheme = scheme;
      const generation = ++basemapStyleGeneration;
      styleRehydrateQueued = false;
      mapStyleReady = false;
      // Theme changes need a deterministic style lifecycle. A URL-based
      // diff keeps the previous style alive while the replacement JSON is
      // fetched, which can let an obsolete first render win the startup race.
      // A full style swap aborts/removes that old style while preserving camera
      // and UI controls; domain overlays are rehydrated below.
      map.setStyle(resolveBasemapStyle(currentBasemap, scheme), {
        diff: false,
      });
      map.once("style.load", () => {
        if (destroyed || !map || generation !== basemapStyleGeneration) return;
        mapStyleReady = true;
        queueOverlayRehydrate(generation);
        // style.load only means the style JSON is ready. During the initial
        // reveal, wait for idle so requested tiles/sources and overlay work have
        // produced a complete rendered frame before removing the loading cover.
        if (isLoading) {
          map.once("idle", () => finishInitialLoading(generation));
        }
      });
    }
    const handleMarkerClick = (e: Event) => {
      const target = e.target as HTMLElement;
      const markerBtn = target.closest(
        ".map-marker",
      ) as HTMLButtonElement | null;
      if (!markerBtn || !nodesOverlay) return;

      const id = markerBtn.dataset.id;
      if (!id) return;

      const entry = nodesOverlay.getActiveMarker(id);
      if (!entry) return;

      lastFocusedElement = markerBtn;
      focusAndFlyToPoint(entry.item);
    };

    async function initialiseMap() {
      // Public map rendering never waits for session verification. Auth loads
      // after map creation and may perform one guarded convergence later.
      const [maplibregl, nodesModule, edgeMotionModule, pmtilesModule] =
        await Promise.all([
          import("maplibre-gl"),
          import("$lib/map/overlay/nodes"),
          import("$lib/map/overlay/edgeMotion"),
          currentBasemap.mode === "local-sovereign"
            ? import("pmtiles")
            : Promise.resolve(null),
        ]);
      const initialAuth = { authenticated: false };
      if (destroyed || mapInitTerminated) return;
      projectMapMarkerViewsForWeave = nodesModule.projectMapMarkerViewsForWeave;
      const container = mapContainer;
      if (!container) {
        return;
      }
      searchViewportResizeObserver = new ResizeObserver(() => {
        invalidateSearchViewportGeometry();
      });
      refreshSearchViewportObservers();

      let transformRequestFn:
        | ((url: string, resourceType?: any) => { url: string })
        | undefined = undefined;

      // MapLibre keeps custom protocols in a module-global registry. A fresh
      // Protocol per map mount avoids sharing stale PMTiles header/directory
      // caches, while the lease prevents an older overlapping teardown from
      // deleting the handler that a newer map already installed.
      if (currentBasemap.mode === "local-sovereign") {
        if (!pmtilesModule) {
          throw new Error("PMTiles runtime unavailable for local basemap");
        }
        releasePmtilesProtocol = registerPmtilesProtocol(
          maplibregl,
          new pmtilesModule.Protocol().tile,
        );

        transformRequestFn = (url: string, resourceType?: any) => {
          return { url: rewritePmtilesUrl(url, window.location.origin) };
        };
      }

      // Past the last await boundary: from here on the cleanup below observes
      // every resource this initialiser creates, including the protocol lease.
      container.addEventListener("click", handleMarkerClick);

      const initialUrlState = parseMapUrlState(get(page).url.searchParams);
      const initialCamera = resolveInitialMapCamera(
        markersData,
        initialAuth,
        initialUrlState.focus,
        [currentBasemap.center[0], currentBasemap.center[1]],
        currentBasemap.zoom,
      );

      activeBasemapScheme = readDocumentColorScheme();
      map = new maplibregl.Map({
        container,
        style: resolveBasemapStyle(currentBasemap, activeBasemapScheme),
        center: initialCamera.center,
        zoom: initialCamera.zoom,
        minZoom: currentBasemap.minZoom ?? 10,
        maxZoom: currentBasemap.maxZoom ?? 18,
        pitch: currentBasemap.pitch ?? 0,
        bearing: currentBasemap.bearing ?? 0,
        attributionControl: false,
        transformRequest: transformRequestFn,
      });
      map.on("move", handleSearchMapMove);
      map.on("moveend", handleNodeViewportMoveEnd);
      map.on("styledata", handleMapStyleData);
      stopColorSchemeObserver = observeDocumentColorScheme((scheme) => {
        switchBasemapScheme(scheme);
      });
      resolveMotionInput = edgeMotionModule.resolveEdgeMotionInput;
      edgeMotion = new edgeMotionModule.EdgeMotionController(map);
      void import("$lib/map/authCameraConvergence")
        .then((module) => {
          if (!destroyed && map) {
            cleanupAuthCamera = module.installAuthCameraConvergence(
              map,
              markersData,
              () => authCameraMoved,
            );
          }
        })
        .catch((error) => {
          // Secondary path: never fail the whole map for a delayed auth camera
          // helper, but do not leave an unhandled rejection either.
          failMapInit("auth-camera", error);
        });
      map.addControl(
        new maplibregl.NavigationControl({ showZoom: true }),
        "bottom-right",
      );
      map.addControl(
        new maplibregl.AttributionControl({
          customAttribution:
            '© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap</a> contributors',
        }),
        "bottom-left",
      );

      // Architecture Note: Basemap provides orientation. Overlays (nodes, edges, etc.) carry domain meaning.
      nodesOverlay = new nodesModule.NodesOverlay(
        map,
        maplibregl.Marker,
        undefined,
        focusAndFlyToPoint,
      );
      const isDomainFeatureAtPoint = (point: { x: number; y: number }) =>
        nodesOverlay?.hasNativeEntityAt(point) ?? false;
      cleanupKomposition = setupKompositionInteraction(
        map,
        isDomainFeatureAtPoint,
      );
      let sysStateStr = "";
      unsubscribeSysState = systemState.subscribe((val) => {
        sysStateStr = val;
      });
      cleanupFocus = setupFocusInteraction(
        map,
        () => sysStateStr,
        isDomainFeatureAtPoint,
      );
      map.on("resize", handleSearchMapResize);

      map.once("load", async () => {
        // The initial visible-node request is part of readiness: do not reveal
        // an empty map and let markers pop in a frame later. If a route
        // invalidation or camera move arrives while that request is in flight,
        // replay the latest visible viewport before revealing the map.
        await refreshNodeViewport();
        // MapLibre emits this map-level event only once. Bind acceptance to
        // whatever basemap generation is current at that moment rather than to
        // the constructor's original style. If the document scheme changed at
        // the boundary, finishInitialLoading starts a full style swap and that
        // swap's idle event completes the reveal.
        finishInitialLoading(basemapStyleGeneration);
      });
      map.on("error", (event) => {
        const error =
          event && typeof event === "object" && "error" in event
            ? (event as { error?: unknown }).error
            : event;
        if (mapHasLoaded) {
          failMapInit("post-load-error", error);
          return;
        }
        failMapInit("maplibre-error", error);
      });

      // Expose map for testing
      if (shouldExposeTestMap) {
        (window as any).__TEST_MAP__ = map;
        (window as any).__TEST_SET_ACTIVE_FILTERS__ = (types: string[]) => {
          mapContentFilters.set({
            contentTypes: new Set(types),
            topics: new Set(),
          });
        };
        (window as any).__TEST_REFRESH_EDGE_PROJECTION__ =
          refreshEdgeProjection;
        (window as any).__TEST_EDGE_MOTION__ = {
          start: (edgeId: string, phase: "creating" | "releasing") =>
            animateEdgeById(edgeId, phase),
          startForNode: (nodeId: string, phase: "creating" | "releasing") =>
            animateEdgesForNode(nodeId, phase),
          inspect: () => edgeMotion?.inspect() ?? null,
          syncCanonicalIds: (ids: string[]) =>
            edgeMotion?.syncCanonicalEdges(
              scene.edges.filter((edge) => ids.includes(edge.id)),
            ),
        };
      }
    }

    // Arm before the first await/import inside initialiseMap so a hung
    // maplibre/nodes/edgeMotion/pmtiles chunk still hits the fail-closed
    // deadline instead of leaving the loading overlay forever.
    loadingTimeout = scheduleMapInitTimeout(() => {
      failMapInit(
        "timeout",
        new Error("Kartenladen hat das Zeitlimit überschritten"),
      );
    });

    // An early dynamic import or Map constructor can fail (offline, a chunk
    // that no longer exists after a deploy). Without central handling the
    // rejection is silent and the loading overlay stays up forever, so failure
    // becomes a visible, readable state with a way out.
    void initialiseMap().catch((error) => {
      failMapInit(mapHasLoaded ? "post-load-error" : "import", error);
    });

    return () => {
      // Signals the async initialiser to stop at its next await boundary, so a
      // component destroyed mid-import never finishes building a map.
      destroyed = true;
      document.removeEventListener("visibilitychange", handleVisibilityChange);
      window.clearInterval(edgeDecayInterval);
      if (edgeExpiryTimeout !== undefined) {
        clearTimeout(edgeExpiryTimeout);
        edgeExpiryTimeout = undefined;
      }
      if (loadingTimeout !== undefined) {
        clearTimeout(loadingTimeout);
        loadingTimeout = undefined;
      }
      if (shouldExposeTestMap) {
        delete (window as any).__TEST_MAP__;
        delete (window as any).__TEST_SET_ACTIVE_FILTERS__;
        delete (window as any).__TEST_REFRESH_EDGE_PROJECTION__;
        delete (window as any).__TEST_EDGE_MOTION__;
      }
      teardownMapRuntime();
    };
  });

  function retryMapInitialisation() {
    window.location.reload();
  }
  // A genuinely different deep-link bootstrap node needs one fresh handoff
  // into the viewport scene. Re-fetching the same node during invalidation must
  // not reopen that handoff after it was already released.
  $effect.pre(() => {
    const currentBootstrapKey = (data.nodes ?? [])
      .map((node) => node.id)
      .join("\0");
    if (currentBootstrapKey === lastViewportBootstrapKey) return;
    lastViewportBootstrapKey = currentBootstrapKey;
    viewportBootstrapReleased = false;
  });

  // A domain-data invalidation can create/delete a node without moving the map.
  // Refresh the same visible bbox whenever SvelteKit replaces the route resource
  // status snapshot so the viewport converges even when no moveend occurs.
  $effect.pre(() => {
    const currentRouteStatus = data.resourceStatus ?? null;
    if (!viewportRouteStatusInitialized) {
      viewportRouteStatusInitialized = true;
      lastViewportRouteStatus = currentRouteStatus;
      return;
    }
    if (currentRouteStatus === lastViewportRouteStatus) return;
    if (data.nodeLoadMode !== "viewport") {
      lastViewportRouteStatus = currentRouteStatus;
      pendingViewportRefresh = false;
      return;
    }
    if (!mapHasLoaded) {
      pendingViewportRefresh = true;
      return;
    }
    lastViewportRouteStatus = currentRouteStatus;
    requestNodeViewportRefresh?.();
  });

  // Phase 2: Build the scene from request-scoped route data. The scene stays
  // local to this component instance (never a module-level store), so no
  // request-specific data is shared across module state. The presentation
  // derivations live as pure functions in `$lib/stores/mapView` and are fed the
  // scene together with the ephemeral UI state (filters, search).
  let sceneNodes = $derived.by(() => {
    if (data.nodeLoadMode !== "viewport") return data.nodes || [];
    const byId = new Map<string, Node>();
    for (const node of viewportNodes) byId.set(node.id, node);
    // Route data contains at most the exact deep-link node in viewport mode.
    // Keep that bootstrap anchor only until a successful bbox response includes
    // it; otherwise an offscreen deep link would become a permanent scene leak.
    if (!viewportBootstrapReleased) {
      for (const node of data.nodes || []) byId.set(node.id, node);
    }
    return Array.from(byId.values());
  });
  let effectiveResourceStatus = $derived.by(() => {
    const statuses = data.resourceStatus ?? [];
    if (data.nodeLoadMode !== "viewport" || !viewportNodeStatus)
      return statuses;
    return statuses.map((status) =>
      status.resource === "nodes" ? viewportNodeStatus! : status,
    );
  });
  let resourceSummary = $derived.by(() =>
    summarizeMapResourceStatus(effectiveResourceStatus),
  );
  let scene = $derived.by(() =>
    buildMapScene({
      nodes: applyNodeUpdateOverrides(sceneNodes, nodeUpdateOverrides),
      accounts: data.accounts || [],
      edges: data.edges || [],
      webgemeindezentren: data.webgemeindezentren || [],
      loadState: resourceSummary.loadState,
      resourceStatus: effectiveResourceStatus,
      apiBase: import.meta.env.PUBLIC_GEWEBE_API_BASE,
      basemapMode: currentBasemap.mode,
    }),
  );
  // Presentation state derived via pure functions from the local scene + UI state.
  let loadState = $derived.by(() => scene.loadState);
  let diagnostics = $derived.by(() => scene.diagnostics);
  let markersData = $derived.by(() => scene.entities);
  let markerCounts = $derived.by(() => deriveMarkerCounts(markersData));
  let filterEvaluation = $derived.by(() =>
    evaluateMapContentFilters(markersData, $mapContentFilters),
  );
  let availableTypes = $derived.by(() => filterEvaluation.contentTypes);
  let availableTopics = $derived.by(() => filterEvaluation.topics);
  let filteredMarkersData = $derived.by(() => filterEvaluation.entities);
  let showNodes = $derived.by(() => $view.showNodes);
  let nodeContentTypes = $derived.by(
    () =>
      new Set(
        markersData
          .filter((marker) => marker.type === "node")
          .map(getMapContentType),
      ),
  );
  let activeSearchKinds = $derived.by(() =>
    Array.from($mapContentFilters.contentTypes).filter((filter) =>
      nodeContentTypes.has(filter),
    ),
  );
  let activeSearchTags = $derived.by(() =>
    Array.from($mapContentFilters.topics).map(knottingTopicTag),
  );
  let nodeSearchEnabled = $derived.by(
    () =>
      $mapContentFilters.contentTypes.size === 0 ||
      activeSearchKinds.length > 0,
  );
  $effect.pre(() => {
    scheduleNodeSearch(
      $searchQuery,
      activeSearchKinds,
      activeSearchTags,
      nodeSearchEnabled,
      $isSearchOpen,
    );
  });
  let localPublicStructureResults = $derived.by(() =>
    deriveSearchResults(
      filteredMarkersData.filter((item) => item.type !== "node"),
      $searchQuery,
      $isSearchOpen,
    ),
  );
  let filteredNodeSearchItems = $derived.by(
    () =>
      evaluateMapContentFilters(nodeSearchItems, $mapContentFilters).entities,
  );
  let filteredResults = $derived.by(() => [
    ...filteredNodeSearchItems,
    ...localPublicStructureResults,
  ]);
  let searchMatchIds = $derived.by(() => deriveSearchMatchIds(filteredResults));
  // Fachlich absichtliche Asymmetrie: Der sichtbare Zielkörper trägt sein
  // Gewebe auch dann, wenn die Quelle ausgefiltert ist. Eine Linie benötigt
  // dagegen weiterhin beide sichtbaren Endpunkte.
  let weaveEdges = $derived.by(() =>
    deriveWeaveEdges(scene.edges, filteredMarkersData),
  );
  // Null until the weave projector is loaded. Never fall back to raw markers:
  // without `weave` multi-theme edges and motion collapse to a single colour.
  // Motion resolves geometry against the unfiltered projected set so filter
  // visibility can hide transitions without inventing monochrome from raw
  // markers or losing endpoints that are only temporarily filtered out. The
  // visible view reuses that projection instead of rebuilding every weave.
  let markerWeaveViews = $derived.by(() =>
    projectMapMarkerViewsForWeave
      ? projectMapMarkerViewsForWeave(
          markersData,
          filteredMarkersData,
          scene.edges,
          edgeProjectionNow,
        )
      : null,
  );
  let projectedMarkersData = $derived.by(
    () => markerWeaveViews?.visible ?? null,
  );
  let visibleMapEntities = $derived.by(() => {
    mapViewportRevision;
    if (!$isSearchOpen || !showNodes || !map || !projectedMarkersData)
      return [];
    const bounds = map.getBounds();
    return projectedMarkersData.filter(
      (item) =>
        hasRenderableMapPosition(item) && bounds.contains([item.lon, item.lat]),
    );
  });
  let motionMarkersData = $derived.by(() => markerWeaveViews?.motion ?? null);
  let lineEdges = $derived.by(() =>
    projectedMarkersData
      ? deriveLineEdges(weaveEdges, projectedMarkersData)
      : [],
  );
  // Marker data and search highlighting have separate update paths. Filtering or
  // scene changes may touch the full marker set; search changes only toggle the
  // small delta between the previous and next (maximum ten) search matches.
  $effect.pre(() => {
    if (nodesOverlay && projectedMarkersData) {
      nodesOverlay.update(projectedMarkersData, showNodes);
    }
  });
  $effect.pre(() => {
    if (nodesOverlay) {
      nodesOverlay.updateSearchMatches(searchMatchIds);
    }
  });
  $effect.pre(() => {
    if (nodesOverlay) {
      nodesOverlay.updateSelection($selection?.id ?? null);
    }
  });
  // Search content changes only require indicator and highlight refreshes. ResizeObserver already
  // reports intrinsic overlay-size changes, so it must not be recycled per key.
  $effect.pre(() => {
    if (map) {
      filteredResults;
      $searchQuery;
      $isSearchOpen;
      scheduleSearchDirectionIndicators();
    }
  });
  // Rebind observers only when an observed surface is mounted or removed.
  $effect.pre(() => {
    $isSearchOpen;
    $contextPanelOpen;
    $isFilterOpen;
    $mapChrome.toolFanOpen;
    $mapChrome.toolFanBranch;
    $mapChrome.attentionOverflowOpen;
    $mapChrome.attentionCardOpen;
    if (map) tick().then(refreshSearchViewportObservers);
  });
  // Lines require visible node endpoints. Hiding nodes must therefore hide
  // lines as well; otherwise GeoJSON edges float without their markers.
  let edgesVisuallyEnabled = $derived.by(() =>
    areMapEdgesVisuallyEnabled($view.showEdges, $view.showNodes),
  );
  // Reactive update for edges – only after map style is fully loaded. The
  // transient motion controller receives visibility and canonical ids, but
  // neither filtering nor a render-only difference starts a transition.
  // Wait for weave projection so static and motion threads share one palette.
  $effect.pre(() => {
    if (map && mapStyleReady && projectedMarkersData && $view) {
      updateEdges(
        map,
        lineEdges,
        projectedMarkersData,
        edgesVisuallyEnabled,
        edgeProjectionNow,
      );
      syncEdgeMotionProjection();
    }
  });
  $effect.pre(() => {
    if (map) {
      weaveEdges;
      refreshEdgeProjection();
    }
  });
  // After KompositionPanel creates a node (+ its account->node edge) and
  // reloads route data, focus/fly-to it once it shows up in the freshly
  // rebuilt scene. Retries on later markersData updates while unresolved
  // (e.g. the reload is still in flight when this first runs).
  $effect.pre(() => {
    if ($lastCreatedNodeId) {
      const created = markersData.find(
        (m) => m.id === $lastCreatedNodeId && m.type === "node",
      );
      if (created) {
        // The store is set only by a successful node create. By this point the
        // route reload has supplied the server-derived Faden and its endpoints.
        animateEdgesForNode(created.id, "creating");
        focusAndFlyToPoint(created);
        lastCreatedNodeId.set(null);
      }
    }
  });
  $effect.pre(() => {
    const search = $page.url.search;
    const parsed = parseMapUrlState($page.url.searchParams);
    if (search !== lastAppliedImmediateUrlSearch) {
      lastAppliedImmediateUrlSearch = search;
      applyImmediateMapUrlAddressing(parsed);
      // compose is final; without a valid focus there is nothing for the focus
      // pass to do. Either way the focus lock is satisfied for this query.
      if (parsed.compose || !parsed.focus) {
        lastResolvedFocusUrlSearch = search;
      }
    }
    // Focus resolves directly against the live entity list, so the data
    // dependency stays visible to Svelte without an artificial key. A retry can
    // still happen on a later markersData change while focus is unresolved.
    if (
      parsed.focus &&
      search !== lastResolvedFocusUrlSearch &&
      markersData.length > 0
    ) {
      if (tryApplyFocusMapUrlAddressing(parsed, markersData)) {
        lastResolvedFocusUrlSearch = search;
      }
    }
  });
  // Restore focus when selection is closed or state becomes navigation
  $effect.pre(() => {
    if (($systemState === "navigation" || !$selection) && lastFocusedElement) {
      const elToFocus = lastFocusedElement;
      lastFocusedElement = null; // Clear immediately to prevent loop

      // Use tick() to wait for DOM updates (e.g. context panel removed)
      // and try to focus safely.
      tick().then(() => {
        if (elToFocus && document.body.contains(elToFocus)) {
          try {
            elToFocus.focus();
          } catch (e) {
            // ignore focus errors
          }
        }
      });
    }
  });
</script>

<MapRouteSurface
  panelOpen={$contextPanelOpen}
  searchOpen={$isSearchOpen}
  filterOpen={$isFilterOpen}
  loading={isLoading}
  bind:mapElement={mapContainer}
>
  <MapRouteStatus
    {loadState}
    loadNotice={resourceSummary.loadNotice}
    loading={isLoading}
    initFailed={mapInitFailed}
    showDebug={import.meta.env.DEV || import.meta.env.MODE === "test"}
    nodeCount={markerCounts.nodes}
    accountCount={markerCounts.accounts}
    centerCount={markerCounts.webgemeindezentren}
    edgeCount={lineEdges.length}
    {diagnostics}
    on:retry={retryMapInitialisation}
  />
  <MapRouteOverlays
    mapEntities={visibleMapEntities}
    selectedEntityId={$selection?.id ?? null}
    {filteredResults}
    searchStatus={nodeSearchStatus}
    searchMode={nodeSearchMode}
    searchFallbackReason={nodeSearchFallbackReason}
    {searchDirectionIndicators}
    {availableTypes}
    {availableTopics}
    filterResultCount={filteredMarkersData.length}
    filterTotalCount={filterEvaluation.totalCount}
    allTopicsCount={filterEvaluation.allTopicsCount}
    on:searchSelect={handleSearchSelect}
    on:searchDirectionSelect={handleSearchDirectionSelect}
    on:relatedSelect={handleRelatedSelect}
    on:domainChanged={handleDomainChanged}
  />
  <TopBar />
</MapRouteSurface>
