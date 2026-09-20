<script lang="ts">
  import { createEventDispatcher } from "svelte";
  import {
    selection,
    systemState,
    contextPanelOpen,
    kompositionDraft,
    leaveToNavigation,
  } from "$lib/stores/uiView";
  import { isSearchOpen } from "$lib/stores/searchStore";
  import { isFilterOpen } from "$lib/stores/filterStore";
  import type {
    KompositionDraft,
    Selection,
    SystemState,
  } from "$lib/stores/uiView";
  import type {
    MapDomainChanged,
    RelatedMapSelection,
  } from "$lib/components/map/mapRouteEvents";

  import NodePanel from "./panels/NodePanel.svelte";
  import EdgePanel from "./panels/EdgePanel.svelte";
  import KompositionPanel from "./panels/KompositionPanel.svelte";

  type KompositionPanelHandle = { requestClose: () => void };
  type SheetStage = "compact" | "full";

  const dispatch = createEventDispatcher<{
    selectRelated: RelatedMapSelection;
    domainChanged: MapDomainChanged;
  }>();
  const DRAG_THRESHOLD_PX = 6;

  let kompositionPanel: KompositionPanelHandle | null = $state(null);
  let sheetStage: SheetStage = $state(
    $systemState === "komposition" ? "full" : "compact",
  );
  let previousPanelIdentity = $state(
    $systemState === "komposition"
      ? `komposition:${$kompositionDraft?.mode ?? "unknown"}`
      : $selection
        ? `${$selection.type}:${$selection.id}`
        : "",
  );
  let panelTitle = $derived.by(() =>
    derivePanelTitle($systemState, $kompositionDraft, $selection),
  );
  let dragStartY = 0;
  let dragStartHeight = 0;
  let dragHeight: number | null = $state(null);
  let dragging = $state(false);
  let dragMoved = false;
  let activePointerId: number | null = null;

  function resetSheetPointerState(): void {
    dragHeight = null;
    dragging = false;
    dragMoved = false;
    activePointerId = null;
  }

  function derivePanelTitle(
    state: SystemState,
    draft: KompositionDraft,
    currentSelection: Selection,
  ): string {
    if (state === "komposition") {
      return draft?.mode === "place-garnrolle"
        ? "Garnrolle auf die Karte setzen"
        : "Knoten knüpfen";
    }

    if (currentSelection?.type === "node") return "Knoten";
    if (currentSelection?.type === "webgemeindezentrum")
      return "Webgemeindezentrum";
    if (
      currentSelection?.type === "account" ||
      currentSelection?.type === "garnrolle"
    ) {
      return "Garnrolle";
    }
    if (currentSelection?.type === "edge") return "Faden";
    return "Details";
  }

  $effect.pre(() => {
    const nextPanelIdentity =
      $systemState === "komposition"
        ? `komposition:${$kompositionDraft?.mode ?? "unknown"}`
        : $selection
          ? `${$selection.type}:${$selection.id}`
          : "";

    if (nextPanelIdentity !== previousPanelIdentity) {
      previousPanelIdentity = nextPanelIdentity;
      sheetStage = $systemState === "komposition" ? "full" : "compact";
      resetSheetPointerState();
    }
  });

  function setSheetStage(stage: SheetStage): void {
    sheetStage = stage;
    resetSheetPointerState();
  }

  function toggleSheetStage(): void {
    setSheetStage(sheetStage === "compact" ? "full" : "compact");
  }

  function nearestSheetStage(height: number): SheetStage {
    const viewport = window.innerHeight;
    const compact = Math.min(270, Math.max(190, viewport * 0.29));
    return Math.abs(height - compact) <= Math.abs(height - viewport * 0.88)
      ? "compact"
      : "full";
  }

  function handleSheetPointerDown(event: PointerEvent): void {
    if (!event.isPrimary || event.button !== 0 || dragging) return;
    const handle = event.currentTarget as HTMLElement;
    if (handle.getClientRects().length === 0) return;
    const aside = handle.closest<HTMLElement>('[data-testid="context-panel"]');
    if (!aside) return;
    event.preventDefault();
    handle.setPointerCapture(event.pointerId);
    activePointerId = event.pointerId;
    dragStartY = event.clientY;
    dragStartHeight = aside.getBoundingClientRect().height;
    dragHeight = dragStartHeight;
    dragging = true;
    dragMoved = false;
  }

  function handleSheetPointerMove(event: PointerEvent): void {
    if (!dragging || event.pointerId !== activePointerId) return;
    const delta = dragStartY - event.clientY;
    if (Math.abs(delta) >= DRAG_THRESHOLD_PX) dragMoved = true;
    if (!dragMoved) return;

    event.preventDefault();
    const minHeight = 190;
    const maxHeight = window.innerHeight * 0.88;
    dragHeight = Math.min(
      maxHeight,
      Math.max(minHeight, dragStartHeight + delta),
    );
  }

  function finishSheetPointer(event: PointerEvent, cancelled: boolean): void {
    if (!dragging || event.pointerId !== activePointerId) return;
    const moved = dragMoved;
    const finalHeight = dragHeight ?? dragStartHeight;
    const handle = event.currentTarget as HTMLElement;

    // Reset before releasePointerCapture(): browsers may synchronously dispatch
    // lostpointercapture, which must then observe an already-clean state.
    resetSheetPointerState();
    if (handle.hasPointerCapture(event.pointerId)) {
      handle.releasePointerCapture(event.pointerId);
    }

    if (cancelled) return;
    if (moved) setSheetStage(nearestSheetStage(finalHeight));
    else toggleSheetStage();
  }

  function handleSheetPointerUp(event: PointerEvent): void {
    finishSheetPointer(event, false);
  }

  function handleSheetPointerCancel(event: PointerEvent): void {
    finishSheetPointer(event, true);
  }

  function handleSheetLostPointerCapture(event: PointerEvent): void {
    if (!dragging || event.pointerId !== activePointerId) return;
    resetSheetPointerState();
  }

  function handleSheetHandleClick(event: MouseEvent): void {
    if (event.detail !== 0) return;
    const handle = event.currentTarget as HTMLElement;
    if (handle.getClientRects().length === 0) return;
    toggleSheetStage();
  }

  function handleSheetKeydown(event: KeyboardEvent): void {
    const handle = event.currentTarget as HTMLElement;
    if (handle.getClientRects().length === 0) return;
    if (event.key === "ArrowUp" || event.key === "End") {
      event.preventDefault();
      setSheetStage("full");
    } else if (event.key === "ArrowDown" || event.key === "Home") {
      event.preventDefault();
      setSheetStage("compact");
    }
  }

  function closePanel() {
    if ($systemState === "komposition") {
      kompositionPanel?.requestClose();
      return;
    }
    leaveToNavigation();
  }

  function handleRelated(event: CustomEvent<RelatedMapSelection>) {
    dispatch("selectRelated", event.detail);
  }

  function handleDomainChanged(event: CustomEvent<MapDomainChanged>) {
    dispatch("domainChanged", event.detail);
  }

  function handleKeydown(event: KeyboardEvent) {
    if (event.repeat || event.defaultPrevented) return;
    if (
      event.key === "Escape" &&
      $contextPanelOpen &&
      !$isSearchOpen &&
      !$isFilterOpen
    )
      closePanel();
  }
</script>

<svelte:window onkeydown={handleKeydown} />

{#if $contextPanelOpen}
  <aside
    class="context-panel"
    class:stage-compact={sheetStage === "compact"}
    class:stage-full={sheetStage === "full"}
    class:composition={$systemState === "komposition"}
    class:dragging
    data-testid="context-panel"
    data-sheet-stage={sheetStage}
    aria-label={panelTitle}
    style:height={dragHeight === null ? null : `${dragHeight}px`}
  >
    <button
      type="button"
      class="sheet-handle"
      data-testid="sheet-handle"
      aria-controls="context-panel-content"
      aria-expanded={sheetStage === "full"}
      aria-label={sheetStage === "compact"
        ? "Panel vollständig öffnen oder ziehen"
        : "Panel kompakt anzeigen oder ziehen"}
      onpointerdown={handleSheetPointerDown}
      onpointermove={handleSheetPointerMove}
      onpointerup={handleSheetPointerUp}
      onpointercancel={handleSheetPointerCancel}
      onlostpointercapture={handleSheetLostPointerCapture}
      onclick={handleSheetHandleClick}
      onkeydown={handleSheetKeydown}
    >
      <span aria-hidden="true"></span>
    </button>
    <header
      class="panel-header"
      class:composition={$systemState === "komposition"}
    >
      <div class="heading-group">
        <h2 class="desktop-panel-title">{panelTitle}</h2>
        <button
          type="button"
          class="mobile-panel-title"
          aria-controls="context-panel-content"
          aria-expanded={sheetStage === "full"}
          aria-label={`${panelTitle}: ${
            sheetStage === "compact" ? "vollständig öffnen" : "kompakt anzeigen"
          }`}
          onclick={toggleSheetStage}
        >
          <span>{panelTitle}</span>
        </button>
      </div>
      <button class="close-btn" onclick={closePanel} aria-label="Schließen"
        >✕</button
      >
    </header>

    <div class="panel-content" id="context-panel-content">
      {#if $systemState === "komposition"}
        <KompositionPanel bind:this={kompositionPanel} />
      {:else if $selection}
        {#if $selection.type === "node"}
          <NodePanel
            on:selectRelated={handleRelated}
            on:domainChanged={handleDomainChanged}
          />
        {:else if $selection.type === "account" || $selection.type === "garnrolle"}
          {#await import("./panels/AccountPanel.svelte")}
            <p role="status">Lade Garnrolle…</p>
          {:then accountPanel}
            <accountPanel.default on:selectRelated={handleRelated} />
          {/await}
        {:else if $selection.type === "webgemeindezentrum"}
          {#await import("./panels/WebgemeindezentrumPanel.svelte")}
            <p role="status">Lade Webgemeindezentrum…</p>
          {:then centerPanel}
            <centerPanel.default />
          {/await}
        {:else if $selection.type === "edge"}
          <EdgePanel />
        {/if}
      {/if}
    </div>
  </aside>
{/if}

<style>
  .context-panel {
    position: fixed;
    top: 0;
    right: 0;
    bottom: 0;
    width: var(--context-panel-width);
    z-index: var(--z-map-context-panel);
    background: var(--panel);
    color: var(--text);
    box-shadow: -4px 0 16px rgba(0, 0, 0, 0.28);
    display: flex;
    flex-direction: column;
    overflow: hidden;
    box-sizing: border-box;
  }

  .sheet-handle,
  .mobile-panel-title {
    display: none;
  }

  .panel-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    min-height: 64px;
    padding: 0.6rem 0.8rem 0.6rem 1rem;
    border-bottom: 1px solid var(--panel-border);
    flex: 0 0 auto;
  }

  .heading-group {
    min-width: 0;
  }

  .desktop-panel-title,
  .mobile-panel-title {
    margin: 0;
    color: var(--muted);
    font-size: 0.78rem;
    font-weight: 700;
    letter-spacing: 0.09em;
    text-transform: uppercase;
  }

  .panel-header.composition .desktop-panel-title,
  .panel-header.composition .mobile-panel-title {
    color: var(--text);
    font-size: 1.1rem;
    letter-spacing: 0;
    text-transform: none;
  }

  .close-btn {
    background: none;
    border: none;
    font-size: 1.2rem;
    cursor: pointer;
    color: var(--text);
    min-width: 44px;
    min-height: 44px;
    display: grid;
    place-items: center;
  }

  .panel-content {
    padding: 1rem;
    flex: 1 1 auto;
    min-height: 0;
    overflow-y: auto;
    overscroll-behavior: contain;
  }

  @media (max-width: 768px), (max-height: 500px) and (pointer: coarse) {
    .context-panel {
      top: auto;
      bottom: 0;
      left: 0;
      right: 0;
      width: auto;
      max-height: none;
      padding-bottom: env(safe-area-inset-bottom);
      border-radius: 18px 18px 0 0;
      box-shadow: var(--shadow);
      transition: height var(--motion-ui);
    }

    .context-panel.stage-compact {
      height: clamp(190px, 29dvh, 270px);
    }

    .context-panel.stage-full {
      height: 88dvh;
    }

    .sheet-handle {
      width: 100%;
      min-height: 44px;
      margin: 0;
      padding: 0;
      border: 0;
      background: transparent;
      display: grid;
      place-items: center;
      cursor: ns-resize;
      touch-action: none;
    }

    .sheet-handle span {
      width: 44px;
      height: 4px;
      border-radius: 999px;
      background: var(--panel-border-strong);
    }

    .sheet-handle:focus-visible,
    .mobile-panel-title:focus-visible {
      outline: 3px solid var(--accent);
      outline-offset: -4px;
    }

    .context-panel.dragging {
      transition: none;
      user-select: none;
    }

    .panel-header {
      min-height: 64px;
      padding-top: 0.25rem;
    }

    .heading-group {
      flex: 1;
      text-align: left;
    }

    .desktop-panel-title {
      position: absolute;
      width: 1px;
      height: 1px;
      padding: 0;
      margin: -1px;
      overflow: hidden;
      clip: rect(0, 0, 0, 0);
      white-space: nowrap;
      border: 0;
    }

    .mobile-panel-title {
      display: flex;
      align-items: center;
      width: 100%;
      min-height: 44px;
      padding: 0;
      border: 0;
      background: transparent;
      color: inherit;
      font: inherit;
      font-weight: inherit;
      letter-spacing: inherit;
      text-align: left;
      text-transform: inherit;
      cursor: pointer;
    }

    .stage-compact .panel-content {
      padding-top: 0.65rem;
    }

    .stage-compact :global(.compact-node-summary),
    .stage-compact :global(.compact-account-summary),
    .stage-compact :global(.node-mode.editing .node-summary) {
      display: block;
    }

    .stage-compact :global(.node-full-content),
    .stage-compact :global(.tabs),
    .stage-compact :global(.account-full-content) {
      display: none;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .context-panel {
      transition: none;
    }
  }
</style>
