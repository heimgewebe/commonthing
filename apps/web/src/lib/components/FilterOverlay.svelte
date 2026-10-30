<script lang="ts">
  import { tick } from "svelte";
  import {
    isFilterOpen,
    mapContentFilters,
    activeFilterCount,
    closeFilter,
    toggleFilterType,
    toggleFilterTopic,
    clearTopicFilters,
    clearFilters,
  } from "$lib/stores/filterStore";
  import { contextPanelOpen } from "$lib/stores/uiView";
  import { restoreTarget } from "$lib/utils/focusManager";
  import type { KnottingTopic } from "$lib/knottingTopics";
  import type { MapFilterOption } from "$lib/map/contentFilters";
  import { nodeKindLabel } from "$lib/ui/productLanguage";

  interface Props {
    availableTypes?: MapFilterOption[];
    availableTopics?: MapFilterOption<KnottingTopic>[];
    resultCount?: number;
    totalCount?: number;
    allTopicsCount?: number;
  }

  let {
    availableTypes = [],
    availableTopics = [],
    resultCount = 0,
    totalCount = 0,
    allTopicsCount = 0,
  }: Props = $props();

  let overlayEl: HTMLDivElement | undefined = $state();
  let closeBtnEl: HTMLButtonElement | undefined = $state();
  let wasOpen = $state(false);

  $effect.pre(() => {
    if ($isFilterOpen) {
      wasOpen = true;
      (async () => {
        await tick();
        const first = overlayEl?.querySelector(
          'input[type="checkbox"]',
        ) as HTMLInputElement | null;
        (first || closeBtnEl)?.focus();
      })();
    } else if (wasOpen) {
      wasOpen = false;
      restoreTarget("filter");
    }
  });

  function filterLabel(type: { id: string; label: string }): string {
    if (type.id === "Garnrolle") return "Garnrollen";
    if (type.id === "Webgemeindezentrum") return "Webgemeindezentren";
    return nodeKindLabel(type.id);
  }
  function handleGlobalKeydown(e: KeyboardEvent) {
    if (!$isFilterOpen || e.defaultPrevented || e.repeat) return;
    if (e.key === "Escape") {
      e.preventDefault();
      closeFilter();
    }
  }
</script>

<svelte:window onkeydown={handleGlobalKeydown} />

{#if $isFilterOpen}
  <div
    bind:this={overlayEl}
    class="filter-overlay"
    class:panel-open={$contextPanelOpen}
    data-testid="filter-overlay"
    role="dialog"
    aria-label="Karteninhalt"
    aria-modal="false"
  >
    <div class="filter-header">
      <div>
        <span class="eyebrow">Kartenlinse</span>
        <h3>Karteninhalt</h3>
      </div>
      <button
        class="close-btn"
        bind:this={closeBtnEl}
        onclick={closeFilter}
        aria-label="Karteninhalt schließen">✕</button
      >
    </div>

    <p class="filter-summary" aria-live="polite">
      {$activeFilterCount === 0
        ? `Alle ${totalCount} Elemente auf der Karte`
        : $activeFilterCount === 1
          ? `1 Auswahl aktiv · ${resultCount} von ${totalCount} Elementen sichtbar`
          : `${$activeFilterCount} Auswahlen aktiv · ${resultCount} von ${totalCount} Elementen sichtbar`}
    </p>

    {#if availableTypes.length > 0}
      <fieldset class="filter-group">
        <legend>Inhaltstypen</legend>
        <ul class="filter-list">
          {#each availableTypes as type}
            <li>
              <label
                class="filter-item"
                class:active={$mapContentFilters.contentTypes.has(type.id)}
                ><input
                  type="checkbox"
                  checked={$mapContentFilters.contentTypes.has(type.id)}
                  onchange={() => toggleFilterType(type.id)}
                /><span class="filter-label">{filterLabel(type)}</span><span
                  class="filter-count">{type.count}</span
                ></label
              >
            </li>
          {/each}
        </ul>
      </fieldset>
    {/if}

    {#if availableTopics.length > 0}
      <fieldset class="filter-group topic-group">
        <legend>Themen</legend>
        <p class="group-hint">Bei mehreren Themen reicht eines davon.</p>
        <ul class="filter-list">
          <li>
            <button
              type="button"
              class="filter-item all-topics"
              class:active={$mapContentFilters.topics.size === 0}
              aria-pressed={$mapContentFilters.topics.size === 0}
              onclick={clearTopicFilters}
            >
              <span class="filter-label">Alle Themen</span>
              <span class="filter-count">{allTopicsCount}</span>
            </button>
          </li>
          {#each availableTopics as topic}
            <li>
              <label
                class="filter-item"
                class:active={$mapContentFilters.topics.has(topic.id)}
                ><input
                  type="checkbox"
                  checked={$mapContentFilters.topics.has(topic.id)}
                  onchange={() => toggleFilterTopic(topic.id)}
                /><span class="filter-label">{topic.label}</span><span
                  class="filter-count">{topic.count}</span
                ></label
              >
            </li>
          {/each}
        </ul>
      </fieldset>
    {/if}

    {#if availableTypes.length === 0 && availableTopics.length === 0}
      <div class="no-filters" role="status">
        Keine auswählbaren Elemente vorhanden
      </div>
    {/if}

    {#if $activeFilterCount > 0}
      <button class="clear-btn" type="button" onclick={clearFilters}
        >Alles wieder zeigen</button
      >
    {/if}
  </div>
{/if}

<style>
  .filter-overlay {
    position: fixed;
    top: calc(env(safe-area-inset-top) + var(--toolbar-offset) + 10px);
    right: calc(16px + env(safe-area-inset-right));
    width: min(380px, calc(100vw - 32px));
    max-height: min(560px, calc(100dvh - 112px));
    z-index: var(--z-map-lens);
    padding: 0.9rem;
    border: 1px solid var(--panel-border-strong);
    border-radius: 16px;
    background-color: var(--panel);
    background-image: var(--context-weave);
    box-shadow: var(--shadow);
    backdrop-filter: blur(var(--map-lens-blur));
    display: flex;
    flex-direction: column;
    box-sizing: border-box;
    overflow-y: auto;
  }
  .filter-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 1rem;
  }
  .filter-header h3 {
    margin: 0.1rem 0 0;
    font-size: 1.15rem;
  }
  .eyebrow {
    color: var(--muted);
    font-size: 0.7rem;
    font-weight: 700;
    letter-spacing: 0.09em;
    text-transform: uppercase;
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
  .filter-summary {
    margin: 0.45rem 0 0.75rem;
    color: var(--muted);
    font-size: 0.82rem;
  }
  .filter-group {
    border: 0;
    padding: 0;
    margin: 0;
  }
  .topic-group {
    margin-top: 0.85rem;
    padding-top: 0.75rem;
    border-top: 1px solid var(--panel-border);
  }
  .filter-group legend {
    margin: 0 0 0.55rem;
    color: var(--muted);
    font-size: 0.78rem;
  }
  .group-hint {
    margin: -0.25rem 0 0.55rem;
    color: var(--muted);
    font-size: 0.72rem;
  }
  .filter-list {
    list-style: none;
    padding: 0;
    margin: 0;
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 0.45rem;
  }
  .filter-item {
    width: 100%;
    box-sizing: border-box;
    min-height: 44px;
    padding: 0.45rem 0.6rem;
    border: 1px solid var(--panel-border);
    border-radius: 12px;
    display: flex;
    align-items: center;
    gap: 0.5rem;
    cursor: pointer;
    background: var(--panel-solid);
  }
  button.filter-item {
    color: var(--text);
    font: inherit;
    text-align: left;
  }
  .filter-item.active {
    border-color: var(--accent);
    background: var(--accent-soft);
  }
  .filter-item input {
    width: 1.15rem;
    height: 1.15rem;
    margin: 0;
    accent-color: var(--accent);
  }
  .filter-label {
    min-width: 0;
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .filter-count {
    color: var(--muted);
    font-size: 0.78rem;
    font-variant-numeric: tabular-nums;
  }
  .clear-btn {
    min-height: 44px;
    margin-top: 0.75rem;
    border: 1px solid var(--panel-border-strong);
    border-radius: 12px;
    background: transparent;
    color: var(--accent);
    cursor: pointer;
    font-weight: 650;
  }
  .clear-btn:hover,
  .clear-btn:focus-visible {
    background: var(--accent-soft);
  }
  .no-filters {
    color: var(--muted);
    padding: 0.75rem 0;
  }

  @media (min-width: 769px) {
    .filter-overlay.panel-open {
      right: calc(var(--context-panel-width) + 16px);
      width: min(380px, calc(100vw - var(--context-panel-width) - 32px));
    }
  }

  @media (prefers-reduced-transparency: reduce) {
    .filter-overlay {
      background-color: var(--panel-solid);
      backdrop-filter: none;
    }
  }

  @media (max-width: 520px) {
    .filter-overlay {
      top: calc(env(safe-area-inset-top) + var(--toolbar-offset) + 4px);
      left: 10px;
      right: 10px;
      width: auto;
      max-height: min(46dvh, 390px);
      padding: 0.75rem;
      border-radius: 14px;
    }
    .filter-list {
      grid-template-columns: 1fr;
    }
  }
</style>
