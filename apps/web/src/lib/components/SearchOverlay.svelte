<script lang="ts">
  import { tick, createEventDispatcher } from "svelte";
  import {
    isSearchOpen,
    searchQuery,
    closeSearch,
  } from "$lib/stores/searchStore";
  import { contextPanelOpen } from "$lib/stores/uiView";
  import { activeFilterCount } from "$lib/stores/filterStore";
  import type { MapEntityViewModel } from "$lib/map/types";
  import { MAX_SEARCH_QUERY_CHARS } from "$lib/api/search";
  import type { NodeSearchStatus } from "$lib/api/search";
  import { restoreTarget } from "$lib/utils/focusManager";

  const MAP_ENTITY_BROWSE_PAGE_SIZE = 50;

  interface Props {
    mapEntities?: MapEntityViewModel[];
    filteredResults?: MapEntityViewModel[];
    searchStatus?: NodeSearchStatus;
    searchMode?: string | null;
    searchFallbackReason?: string | null;
  }

  let {
    mapEntities = [],
    filteredResults = [],
    searchStatus = "idle",
    searchMode = null,
    searchFallbackReason = null,
  }: Props = $props();
  const dispatch = createEventDispatcher<{ select: MapEntityViewModel }>();
  let inputEl: HTMLInputElement | undefined = $state();
  let listEl: HTMLUListElement | undefined = $state();
  let activeIndex = $state(-1);
  let wasOpen = false;
  let showAll = $state(false);
  let browsePage = $state(0);
  let previousQuery = "";
  let previousResults: MapEntityViewModel[] | undefined;
  let previousMapEntities: MapEntityViewModel[] | undefined;

  let hasQuery = $derived($searchQuery.trim().length > 0);
  let browsePageCount = $derived(
    Math.max(1, Math.ceil(mapEntities.length / MAP_ENTITY_BROWSE_PAGE_SIZE)),
  );
  let browseStart = $derived(browsePage * MAP_ENTITY_BROWSE_PAGE_SIZE);
  let browseResults = $derived(
    mapEntities.slice(browseStart, browseStart + MAP_ENTITY_BROWSE_PAGE_SIZE),
  );
  let visibleResults = $derived(
    hasQuery
      ? showAll
        ? filteredResults
        : filteredResults.slice(0, 6)
      : browseResults,
  );
  $effect.pre(() => {
    if ($searchQuery !== previousQuery) {
      previousQuery = $searchQuery;
      showAll = false;
      browsePage = 0;
      activeIndex = -1;
    }
  });
  $effect.pre(() => {
    if (filteredResults !== previousResults) {
      previousResults = filteredResults;
      activeIndex = -1;
      if (filteredResults.length <= 6) showAll = false;
    }
  });
  $effect.pre(() => {
    if (mapEntities !== previousMapEntities) {
      previousMapEntities = mapEntities;
      activeIndex = -1;
      browsePage = Math.min(browsePage, browsePageCount - 1);
    }
  });
  $effect(() => {
    if ($isSearchOpen) {
      wasOpen = true;
      (async () => {
        await tick();
        if ($isSearchOpen && inputEl) inputEl.focus();
      })();
    } else {
      activeIndex = -1;
      showAll = false;
      browsePage = 0;
      if (wasOpen) {
        wasOpen = false;
        restoreTarget("search");
      }
    }
  });

  function resultOptionId(item: MapEntityViewModel): string {
    return `search-option-${item.type}-${item.id}`;
  }

  function onSelect(item: MapEntityViewModel) {
    dispatch("select", item);
    closeSearch();
  }

  function setBrowsePage(nextPage: number) {
    browsePage = Math.min(Math.max(nextPage, 0), browsePageCount - 1);
    activeIndex = -1;
    void tick().then(() => inputEl?.focus());
  }
  function handleGlobalKeydown(e: KeyboardEvent) {
    if (!$isSearchOpen || e.defaultPrevented || e.repeat) return;
    if (e.key === "Escape") {
      e.preventDefault();
      closeSearch();
    }
  }
  function handleInputKeydown(e: KeyboardEvent) {
    if (!$isSearchOpen || visibleResults.length === 0) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      activeIndex = (activeIndex + 1) % visibleResults.length;
      scrollToActive();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      activeIndex =
        activeIndex <= 0 ? visibleResults.length - 1 : activeIndex - 1;
      scrollToActive();
    } else if (e.key === "Enter") {
      e.preventDefault();
      const selected = visibleResults[activeIndex >= 0 ? activeIndex : 0];
      if (selected) onSelect(selected);
    } else if (e.key === "Home") {
      e.preventDefault();
      activeIndex = 0;
      scrollToActive();
    } else if (e.key === "End") {
      e.preventDefault();
      activeIndex = visibleResults.length - 1;
      scrollToActive();
    }
  }
  async function scrollToActive() {
    await tick();
    const activeEl = listEl?.children[activeIndex] as HTMLElement | undefined;
    activeEl?.scrollIntoView({ block: "nearest" });
  }
</script>

<svelte:window onkeydown={handleGlobalKeydown} />

{#if $isSearchOpen}
  <div
    class="search-overlay"
    class:panel-open={$contextPanelOpen}
    data-testid="search-overlay"
    role="dialog"
    aria-label="Finden"
    aria-modal="false"
    aria-busy={searchStatus === "loading"}
  >
    <div class="search-box">
      <span class="search-icon" aria-hidden="true">⌕</span>
      <input
        bind:this={inputEl}
        bind:value={$searchQuery}
        type="search"
        maxlength={MAX_SEARCH_QUERY_CHARS}
        placeholder="Gewebe durchsuchen…"
        role="combobox"
        aria-label="Suchbegriff"
        aria-autocomplete="list"
        aria-haspopup="listbox"
        aria-expanded={visibleResults.length > 0}
        aria-controls={visibleResults.length > 0
          ? "search-results-listbox"
          : undefined}
        aria-activedescendant={activeIndex >= 0 &&
        activeIndex < visibleResults.length
          ? resultOptionId(visibleResults[activeIndex])
          : undefined}
        onkeydown={handleInputKeydown}
      />
      <button
        class="close-btn"
        onclick={closeSearch}
        aria-label="Finden schließen">✕</button
      >
    </div>

    {#if hasQuery && searchStatus === "loading"}
      <div class="search-status" role="status" aria-live="polite">
        Knoten werden sicher auf dem Server gesucht…
      </div>
    {/if}
    {#if hasQuery && searchStatus === "error"}
      <div class="search-error" role="status" aria-live="polite">
        Die sichere Knotensuche ist gerade nicht verfügbar. Es wird nicht auf
        eine lokale Knotensuche zurückgefallen.
      </div>
    {/if}
    {#if hasQuery && (searchMode === "lexical_fallback" || searchFallbackReason === "provider_unavailable")}
      <div class="search-note" role="status">
        Die semantische Ergänzung ist vorübergehend nicht verfügbar. Die
        serverseitige lexikalische Suche bleibt aktiv.
      </div>
    {/if}

    {#if visibleResults.length > 0}
      <div class="result-meta" aria-live="polite">
        {#if hasQuery}
          {#if $activeFilterCount > 0}
            {filteredResults.length === 1
              ? "1 gefilterter Treffer"
              : `${filteredResults.length} gefilterte Treffer`}
          {:else}
            {filteredResults.length === 1
              ? "1 Treffer"
              : `${filteredResults.length} Treffer`}
          {/if}
        {:else}
          {browseStart + 1}–{Math.min(
            browseStart + visibleResults.length,
            mapEntities.length,
          )} von {mapEntities.length} Kartenobjekten im Ausschnitt
        {/if}
      </div>
      <ul
        class="results"
        id="search-results-listbox"
        role="listbox"
        aria-label={hasQuery ? "Suchvorschläge" : "Kartenobjekte im Ausschnitt"}
        bind:this={listEl}
      >
        {#each visibleResults as result, index}
          <li
            id={resultOptionId(result)}
            class="result-item"
            role="option"
            aria-selected={activeIndex === index}
            class:active={activeIndex === index}
            onclick={() => onSelect(result)}
            onkeydown={(e) => {
              if (e.key === "Enter") onSelect(result);
            }}
            onmouseenter={() => (activeIndex = index)}
          >
            <div class="result-content">
              <span class="result-title">{result.title}</span>
              {#if result.summary}<span class="result-summary"
                  >{result.summary.length > 80
                    ? result.summary.slice(0, 80) + "…"
                    : result.summary}</span
                >{/if}
            </div>
            <span class="result-type"
              >{result.type === "node" ? "Knoten" : "Garnrolle"}</span
            >
          </li>
        {/each}
      </ul>
      {#if hasQuery && filteredResults.length > 6}
        <button
          type="button"
          class="show-more"
          aria-expanded={showAll}
          onclick={() => {
            showAll = !showAll;
            activeIndex = -1;
          }}
          >{showAll
            ? "Weniger Vorschläge"
            : `Alle ${filteredResults.length} Vorschläge zeigen`}</button
        >
      {:else if !hasQuery && browsePageCount > 1}
        <div
          class="browse-pagination"
          role="group"
          aria-label="Seiten der Kartenobjekte"
        >
          <button
            type="button"
            disabled={browsePage === 0}
            aria-label="Vorherige Kartenobjekte"
            onclick={() => setBrowsePage(browsePage - 1)}>←</button
          >
          <span>Seite {browsePage + 1} von {browsePageCount}</span>
          <button
            type="button"
            disabled={browsePage >= browsePageCount - 1}
            aria-label="Nächste Kartenobjekte"
            onclick={() => setBrowsePage(browsePage + 1)}>→</button
          >
        </div>
      {/if}
    {:else if hasQuery && searchStatus !== "loading" && searchStatus !== "error"}
      <div class="no-results" role="status">
        Keine Treffer für „{$searchQuery}“
      </div>
    {:else if !hasQuery}
      <div class="no-results" role="status">
        Keine Kartenobjekte im aktuellen Ausschnitt.
      </div>
    {/if}
  </div>
{/if}

<style>
  .search-overlay {
    position: fixed;
    top: calc(env(safe-area-inset-top) + var(--toolbar-offset) + 10px);
    left: 50%;
    width: min(520px, calc(100vw - 32px));
    max-height: min(360px, calc(100dvh - 110px));
    transform: translateX(-50%);
    z-index: var(--z-map-lens);
    padding: 0.65rem;
    border: 1px solid var(--panel-border-strong);
    border-radius: 16px;
    background: var(--panel);
    box-shadow: var(--shadow);
    backdrop-filter: blur(var(--map-lens-blur));
    display: flex;
    flex-direction: column;
    box-sizing: border-box;
  }
  .search-box {
    display: flex;
    align-items: center;
    gap: 0.4rem;
  }
  .search-icon {
    width: 1.4rem;
    color: var(--accent);
    text-align: center;
    font-size: 1.15rem;
  }
  .search-box input {
    flex: 1;
    min-width: 0;
    min-height: 44px;
    padding: 0.65rem 0.25rem;
    border: 0;
    outline: 0;
    font-size: 1rem;
    background: transparent;
    color: var(--text);
    box-sizing: border-box;
  }
  .search-box input:focus-visible {
    box-shadow: inset 0 -2px var(--accent);
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
  .result-meta {
    padding: 0.35rem 0.45rem 0.15rem;
    color: var(--muted);
    font-size: 0.78rem;
  }
  .search-status,
  .search-error,
  .search-note {
    margin: 0.4rem 0.35rem 0;
    padding: 0.55rem 0.65rem;
    border-radius: 9px;
    font-size: 0.8rem;
    line-height: 1.35;
  }
  .search-status,
  .search-note {
    background: var(--accent-soft);
    color: var(--muted);
  }
  .search-error {
    border: 1px solid color-mix(in srgb, #a33 55%, transparent);
    color: var(--text);
  }
  .results {
    list-style: none;
    padding: 0;
    margin: 0.35rem 0 0;
    overflow-y: auto;
    border-top: 1px solid var(--panel-border);
  }
  .result-item {
    width: 100%;
    box-sizing: border-box;
    text-align: left;
    padding: 0.65rem 0.5rem;
    cursor: pointer;
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 1rem;
    color: var(--text);
    border-bottom: 1px solid var(--panel-border);
  }
  .result-item:hover,
  .result-item.active {
    background: var(--accent-soft);
  }
  .result-item.active {
    outline: 2px solid var(--accent);
    outline-offset: -2px;
  }
  .result-content {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
    overflow: hidden;
    min-width: 0;
  }
  .result-title {
    font-weight: 650;
  }
  .result-summary {
    font-size: 0.82rem;
    color: var(--muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .result-type {
    flex: 0 0 auto;
    font-size: 0.75rem;
    color: var(--muted);
  }
  .show-more {
    min-height: 44px;
    margin-top: 0.35rem;
    border: 0;
    border-radius: 10px;
    background: transparent;
    color: var(--accent);
    cursor: pointer;
    font-weight: 650;
  }
  .show-more:hover,
  .show-more:focus-visible {
    background: var(--accent-soft);
  }
  .browse-pagination {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
    padding: 0.35rem 0.25rem 0;
    color: var(--muted);
    font-size: 0.8rem;
  }
  .browse-pagination button {
    min-width: 44px;
    min-height: 44px;
    border: 0;
    border-radius: 10px;
    background: transparent;
    color: var(--accent);
    cursor: pointer;
  }
  .browse-pagination button:hover:not(:disabled),
  .browse-pagination button:focus-visible {
    background: var(--accent-soft);
  }
  .browse-pagination button:disabled {
    cursor: default;
    opacity: 0.4;
  }
  .no-results {
    padding: 0.8rem 0.5rem 0.35rem;
    color: var(--muted);
  }

  @media (min-width: 769px) {
    .search-overlay.panel-open {
      left: calc((100vw - var(--context-panel-width)) / 2);
      width: min(520px, calc(100vw - var(--context-panel-width) - 32px));
    }
  }

  @media (prefers-reduced-transparency: reduce) {
    .search-overlay {
      background: var(--panel-solid);
      backdrop-filter: none;
    }
  }

  @media (max-width: 520px) {
    .search-overlay {
      top: calc(env(safe-area-inset-top) + var(--toolbar-offset) + 4px);
      width: calc(100vw - 20px);
      max-height: min(42dvh, 340px);
      padding: 0.5rem;
      border-radius: 14px;
    }
  }
</style>
