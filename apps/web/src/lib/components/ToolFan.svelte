<script lang="ts">
  import { tick } from "svelte";
  import ToolFanMenu from "./ToolFanMenu.svelte";
  import {
    contextPanelOpen,
    systemState,
    enterKomposition,
  } from "$lib/stores/uiView";
  import { isSearchOpen, closeSearch } from "$lib/stores/searchStore";
  import {
    isFilterOpen,
    activeFilterCount,
    closeFilter,
  } from "$lib/stores/filterStore";
  import {
    toggleSearchExclusive,
    toggleFilterExclusive,
  } from "$lib/stores/overlayManager";
  import {
    mapChrome,
    TOOL_FAN_BRANCH,
    toggleToolFan,
    closeToolFan as closeToolFanState,
    showToolFanBranch,
  } from "$lib/stores/mapChrome";
  import {
    setRestoreTarget,
    suppressNextRestore,
  } from "$lib/utils/focusManager";
  import { authStore } from "$lib/auth/store";

  let fanEl: HTMLDivElement | undefined = $state();
  let triggerEl: HTMLButtonElement | undefined = $state();

  let open = $derived($mapChrome.toolFanOpen);
  let branch = $derived($mapChrome.toolFanBranch);
  let canCreateNode = $derived($authStore.authenticated);
  let canCreateProposal = $derived(
    $authStore.authenticated && $authStore.role === "gast",
  );
  let hasWeavingAction = $derived(canCreateNode || canCreateProposal);

  function closeLensesWithoutRestore(): void {
    if ($isSearchOpen) {
      suppressNextRestore("search");
      closeSearch();
    }
    if ($isFilterOpen) {
      suppressNextRestore("filter");
      closeFilter();
    }
  }

  function closeFan(restoreFocus = false): void {
    if (!open) return;
    closeToolFanState();
    if (restoreFocus) tick().then(() => triggerEl?.focus());
  }

  function handleTrigger(): void {
    if (!open) closeLensesWithoutRestore();
    toggleToolFan();
  }

  function onFind(): void {
    if (triggerEl) setRestoreTarget("search", triggerEl);
    closeToolFanState();
    toggleSearchExclusive();
  }

  function onMapContent(): void {
    if (triggerEl) setRestoreTarget("filter", triggerEl);
    closeToolFanState();
    toggleFilterExclusive();
  }

  function openWeaveBranch(): void {
    showToolFanBranch(TOOL_FAN_BRANCH.weave);
    tick().then(() => {
      fanEl
        ?.querySelector<HTMLElement>(
          '[data-testid="tool-fan-create-node"]:not(:disabled), [data-testid="tool-fan-create-proposal"]:not(:disabled)',
        )
        ?.focus();
    });
  }

  function onCreateNode(): void {
    if (!canCreateNode) return;
    closeLensesWithoutRestore();
    enterKomposition({ mode: "new-knoten", source: "tool-fan" });
    closeToolFanState();
  }

  function handleWindowPointerDown(event: PointerEvent): void {
    if (!open || !fanEl) return;
    if (!fanEl.contains(event.target as Node)) closeFan();
  }

  function handleWindowKeydown(event: KeyboardEvent): void {
    if (!open || event.defaultPrevented || event.repeat) return;
    if (event.key === "Escape") {
      event.preventDefault();
      closeFan(true);
    }
  }

  function handleFocusOut(event: FocusEvent): void {
    if (!open) return;
    const next = event.relatedTarget as Node | null;
    if (next && fanEl?.contains(next)) return;
    tick().then(() => {
      if (open && fanEl && !fanEl.contains(document.activeElement)) closeFan();
    });
  }
</script>

<svelte:window
  onpointerdown={handleWindowPointerDown}
  onkeydown={handleWindowKeydown}
/>

<div
  class="tool-fan"
  class:panel-open={$contextPanelOpen}
  class:expanded={open}
  bind:this={fanEl}
  data-testid="tool-fan"
  data-expanded={open ? "true" : "false"}
  data-branch={branch}
  onfocusout={handleFocusOut}
>
  <button
    type="button"
    class="fan-trigger"
    bind:this={triggerEl}
    data-testid="tool-fan-trigger"
    aria-label={open ? "Aktionen schließen" : "Aktionen öffnen"}
    aria-expanded={open}
    aria-controls="tool-fan-actions"
    onclick={handleTrigger}
  >
    <span class="trigger-symbol" aria-hidden="true">{open ? "×" : "✣"}</span>
    <span>Aktionen</span>
  </button>

  <ToolFanMenu
    {open}
    {branch}
    searchOpen={$isSearchOpen}
    filterOpen={$isFilterOpen}
    compositionActive={$systemState === "komposition"}
    activeFilterCount={$activeFilterCount}
    {canCreateNode}
    {canCreateProposal}
    {hasWeavingAction}
    on:find={onFind}
    on:mapContent={onMapContent}
    on:openWeave={openWeaveBranch}
    on:back={() => showToolFanBranch(TOOL_FAN_BRANCH.root)}
    on:createNode={onCreateNode}
    on:close={() => closeToolFanState()}
  />
</div>

<style>
  .tool-fan {
    position: fixed;
    left: 50%;
    bottom: calc(var(--tool-fan-edge) + env(safe-area-inset-bottom));
    z-index: var(--z-map-fan);
    width: max-content;
    transform: translateX(-50%);
    transition:
      left var(--motion-ui),
      bottom var(--motion-ui);
  }

  .fan-trigger {
    box-sizing: border-box;
    min-width: var(--tool-fan-collapsed-width);
    min-height: var(--tool-fan-size);
    padding: 0 0.9rem;
    border: 1px solid var(--panel-border-strong);
    border-radius: 999px;
    background: var(--panel);
    color: var(--text);
    box-shadow: var(--shadow);
    backdrop-filter: blur(var(--map-lens-blur));
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 0.45rem;
    font-weight: 700;
    cursor: pointer;
  }

  .trigger-symbol {
    font-size: 1.2rem;
    line-height: 1;
  }

  .fan-trigger:focus-visible {
    outline: 3px solid var(--accent);
    outline-offset: 3px;
  }

  @media (max-width: 768px) {
    .tool-fan.panel-open {
      display: none;
    }
  }

  @media (min-width: 769px) {
    .tool-fan.panel-open {
      left: calc((100vw - var(--context-panel-width)) / 2);
      --tool-fan-menu-width: max(
        304px,
        calc(100vw - var(--context-panel-width) - 24px)
      );
    }
  }

  @media (prefers-reduced-transparency: reduce) {
    .fan-trigger {
      background: var(--panel-solid);
      backdrop-filter: none;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .tool-fan {
      transition: none;
    }
  }
</style>
