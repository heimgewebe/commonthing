<script lang="ts">
  import { createEventDispatcher } from "svelte";
  import type { MapEntityViewModel } from "$lib/map/types";
  import type { SearchDirectionIndicator } from "$lib/map/searchNavigation";

  interface Props {
    indicators?: SearchDirectionIndicator[];
  }

  let { indicators = [] }: Props = $props();

  const dispatch = createEventDispatcher<{ select: MapEntityViewModel }>();

  function labelFor(indicator: SearchDirectionIndicator): string {
    const kind = indicator.item.type === "garnrolle" ? "Garnrolle" : "Knoten";
    return `${kind} „${indicator.item.title}“ außerhalb des Kartenausschnitts anzeigen`;
  }
</script>

{#if indicators.length > 0}
  <nav
    class="search-directions"
    aria-label="Suchtreffer außerhalb des Kartenausschnitts"
    data-testid="search-direction-indicators"
  >
    {#each indicators as indicator (`${indicator.item.type}:${indicator.item.id}`)}
      <button
        type="button"
        class="search-direction"
        data-testid={`search-direction-${indicator.item.type}-${indicator.item.id}`}
        data-side={indicator.side}
        style:--search-direction-x={`${indicator.x}px`}
        style:--search-direction-y={`${indicator.y}px`}
        style:--search-direction-angle={`${indicator.angle}deg`}
        aria-label={labelFor(indicator)}
        title={indicator.item.title}
        onclick={() => dispatch("select", indicator.item)}
      >
        <span class="search-direction__arrow" aria-hidden="true">➜</span>
      </button>
    {/each}
  </nav>
{/if}

<style>
  .search-directions {
    position: absolute;
    inset: 0;
    z-index: var(--z-map-direction);
    pointer-events: none;
  }

  .search-direction {
    position: absolute;
    left: var(--search-direction-x);
    top: var(--search-direction-y);
    width: 44px;
    height: 44px;
    min-width: 44px;
    min-height: 44px;
    padding: 0;
    border: 2px solid var(--panel-border-strong);
    border-radius: 999px;
    background: var(--panel);
    color: var(--accent);
    box-shadow: var(--shadow);
    display: grid;
    place-items: center;
    transform: translate(-50%, -50%);
    pointer-events: auto;
    cursor: pointer;
  }

  .search-direction:hover {
    background: var(--accent-soft);
  }

  .search-direction:focus-visible {
    outline: 3px solid var(--text);
    outline-offset: 2px;
  }

  .search-direction__arrow {
    display: block;
    font-size: 1.35rem;
    line-height: 1;
    transform: rotate(var(--search-direction-angle));
    transform-origin: center;
  }

  @media (prefers-reduced-motion: no-preference) {
    .search-direction {
      animation: search-direction-arrive 160ms ease-out;
    }
  }

  @keyframes search-direction-arrive {
    from {
      opacity: 0;
    }
    to {
      opacity: 1;
    }
  }
</style>
