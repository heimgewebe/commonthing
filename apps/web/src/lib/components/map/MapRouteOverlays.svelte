<script lang="ts">
  import { createEventDispatcher } from "svelte";
  import FilterOverlay from "$lib/components/FilterOverlay.svelte";
  import SearchDirectionIndicators from "$lib/components/SearchDirectionIndicators.svelte";
  import ToolFan from "$lib/components/ToolFan.svelte";
  import type { NodeSearchStatus } from "$lib/api/search";
  import type { KnottingTopic } from "$lib/knottingTopics";
  import type { MapFilterOption } from "$lib/map/contentFilters";
  import { createResettableLazyImport } from "$lib/map/lazyImport";
  import type { SearchDirectionIndicator } from "$lib/map/searchNavigation";
  import type { MapEntityViewModel } from "$lib/map/types";
  import { isSearchOpen } from "$lib/stores/searchStore";
  import { contextPanelOpen } from "$lib/stores/uiView";
  import type {
    MapDomainChanged,
    RelatedMapSelection,
  } from "$lib/components/map/mapRouteEvents";

  interface Props {
    mapEntities?: MapEntityViewModel[];
    filteredResults?: MapEntityViewModel[];
    searchStatus?: NodeSearchStatus;
    searchMode?: string | null;
    searchFallbackReason?: string | null;
    searchDirectionIndicators?: SearchDirectionIndicator[];
    availableTypes?: MapFilterOption[];
    availableTopics?: MapFilterOption<KnottingTopic>[];
    filterResultCount?: number;
    filterTotalCount?: number;
    allTopicsCount?: number;
  }

  let {
    mapEntities = [],
    filteredResults = [],
    searchStatus = "idle",
    searchMode = null,
    searchFallbackReason = null,
    searchDirectionIndicators = [],
    availableTypes = [],
    availableTopics = [],
    filterResultCount = 0,
    filterTotalCount = 0,
    allTopicsCount = 0,
  }: Props = $props();

  const dispatch = createEventDispatcher<{
    searchSelect: MapEntityViewModel;
    searchDirectionSelect: MapEntityViewModel;
    relatedSelect: RelatedMapSelection;
    domainChanged: MapDomainChanged;
  }>();

  type ContextPanelModule =
    typeof import("$lib/components/ContextPanel.svelte");
  let contextPanelPromise: Promise<ContextPanelModule> | null = null;
  const loadContextPanelModule = createResettableLazyImport(
    () => import("$lib/components/ContextPanel.svelte"),
  );

  function loadContextPanel(): Promise<ContextPanelModule> {
    const promise = loadContextPanelModule();
    contextPanelPromise = promise;
    void promise.catch(() => {
      if (contextPanelPromise === promise) contextPanelPromise = null;
    });
    return promise;
  }

  type SearchOverlayModule =
    typeof import("$lib/components/SearchOverlay.svelte");
  let retainSearchOverlay = $state(false);
  const loadSearchOverlayModule = createResettableLazyImport(
    () => import("$lib/components/SearchOverlay.svelte"),
  );

  $effect.pre(() => {
    if ($isSearchOpen) retainSearchOverlay = true;
  });

  function loadSearchOverlay(): Promise<SearchOverlayModule> {
    const promise = loadSearchOverlayModule();
    void promise.catch(() => {
      retainSearchOverlay = false;
    });
    return promise;
  }

  function handleSearchSelect(event: CustomEvent<MapEntityViewModel>) {
    dispatch("searchSelect", event.detail);
  }

  function handleSearchDirectionSelect(event: CustomEvent<MapEntityViewModel>) {
    dispatch("searchDirectionSelect", event.detail);
  }

  function handleRelatedSelect(event: CustomEvent<RelatedMapSelection>) {
    dispatch("relatedSelect", event.detail);
  }

  function handleDomainChanged(event: CustomEvent<MapDomainChanged>) {
    dispatch("domainChanged", event.detail);
  }
</script>

{#if $contextPanelOpen}
  {#await loadContextPanel()}
    <p role="status" class="sr-only">Lade Details…</p>
  {:then contextPanelModule}
    <contextPanelModule.default
      on:selectRelated={handleRelatedSelect}
      on:domainChanged={handleDomainChanged}
    />
  {:catch}
    <p role="alert">Details konnten nicht geladen werden.</p>
  {/await}
{/if}
{#if $isSearchOpen || retainSearchOverlay}
  {#await loadSearchOverlay()}
    {#if $isSearchOpen}
      <p role="status" class="sr-only">Lade Suche…</p>
    {/if}
  {:then searchOverlayModule}
    <searchOverlayModule.default
      {mapEntities}
      {filteredResults}
      {searchStatus}
      {searchMode}
      {searchFallbackReason}
      on:select={handleSearchSelect}
    />
  {:catch}
    {#if $isSearchOpen}
      <p role="alert">Suche konnte nicht geladen werden.</p>
    {/if}
  {/await}
{/if}
<SearchDirectionIndicators
  indicators={searchDirectionIndicators}
  on:select={handleSearchDirectionSelect}
/>
<FilterOverlay
  {availableTypes}
  {availableTopics}
  resultCount={filterResultCount}
  totalCount={filterTotalCount}
  {allTopicsCount}
/>
<ToolFan />
