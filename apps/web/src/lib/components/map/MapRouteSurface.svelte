<script lang="ts">
  interface Props {
    panelOpen?: boolean;
    searchOpen?: boolean;
    filterOpen?: boolean;
    loading?: boolean;
    mapElement?: HTMLDivElement | null;
    children?: import("svelte").Snippet;
  }

  let {
    panelOpen = false,
    searchOpen = false,
    filterOpen = false,
    loading = true,
    mapElement = $bindable(null),
    children,
  }: Props = $props();
</script>

<main
  class="shell"
  class:panel-open={panelOpen}
  class:search-open={searchOpen}
  class:filter-open={filterOpen}
>
  {@render children?.()}
  <div
    id="map"
    class:panel-open={panelOpen}
    class:search-open={searchOpen}
    class:filter-open={filterOpen}
    class:map-loading={loading}
    bind:this={mapElement}
  ></div>
</main>

<style>
  .shell {
    position: relative;
    height: 100dvh;
    height: calc(
      100dvh - env(safe-area-inset-top) - env(safe-area-inset-bottom)
    );
    width: 100vw;
    overflow: hidden;
    background: var(--bg);
    color: var(--text);
    padding-top: env(safe-area-inset-top);
    padding-bottom: env(safe-area-inset-bottom);
  }
  #map {
    position: absolute;
    inset: 0;
  }
  #map.map-loading {
    opacity: 0;
    pointer-events: none;
  }
  /* The canvas colour grade is applied to the colours before MapLibre draws
     them (src/lib/map/canvasColorGrade.ts); a CSS filter here would cost a
     full-screen compositor pass per map frame. */

  #map :global(.maplibregl-ctrl-bottom-right) {
    right: calc(
      env(safe-area-inset-right) + var(--map-control-edge)
    ) !important;
    bottom: calc(
      env(safe-area-inset-bottom) + var(--map-control-edge)
    ) !important;
  }

  #map :global(.maplibregl-ctrl-bottom-left) {
    left: calc(env(safe-area-inset-left) + var(--map-control-edge)) !important;
    bottom: calc(
      env(safe-area-inset-bottom) + var(--map-control-edge)
    ) !important;
  }

  #map :global(.maplibregl-ctrl-group button) {
    width: 44px;
    height: 44px;
  }

  @media (min-width: 769px) {
    #map.panel-open :global(.maplibregl-ctrl-bottom-right) {
      right: calc(
        var(--context-panel-width) + env(safe-area-inset-right) +
          var(--map-control-edge)
      ) !important;
    }
  }

  @media (max-width: 768px) {
    #map.panel-open :global(.maplibregl-ctrl-bottom-right),
    #map.panel-open :global(.maplibregl-ctrl-bottom-left) {
      top: calc(env(safe-area-inset-top) + var(--toolbar-offset) + 8px);
      bottom: auto !important;
    }

    #map.panel-open :global(.maplibregl-ctrl-bottom-right) {
      right: calc(env(safe-area-inset-right) + 10px) !important;
    }

    #map.panel-open :global(.maplibregl-ctrl-bottom-left) {
      left: calc(env(safe-area-inset-left) + 10px) !important;
    }
  }
</style>
