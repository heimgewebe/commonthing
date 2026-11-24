<script lang="ts">
  import { createEventDispatcher } from "svelte";
  import type { MapDiagnostics, MapLoadState } from "$lib/map/types";

  interface Props {
    loadState: MapLoadState;
    loadNotice?: string | null;
    loading?: boolean;
    initFailed?: boolean;
    showDebug?: boolean;
    nodeCount?: number;
    accountCount?: number;
    centerCount?: number;
    edgeCount?: number;
    diagnostics: MapDiagnostics;
  }

  let {
    loadState,
    loadNotice = null,
    loading = true,
    initFailed = false,
    showDebug = false,
    nodeCount = 0,
    accountCount = 0,
    centerCount = 0,
    edgeCount = 0,
    diagnostics,
  }: Props = $props();

  const dispatch = createEventDispatcher<{ retry: void }>();
</script>

{#if loadState === "partial"}
  <div class="degraded-banner" role="alert" data-testid="load-state-partial">
    {loadNotice}
  </div>
{/if}
{#if loadState === "failed"}
  <div
    class="degraded-banner degraded-banner--failed"
    role="alert"
    data-testid="load-state-failed"
  >
    Kartendaten konnten nicht geladen werden.
  </div>
{/if}

{#if showDebug}
  <div class="debug-badge" data-testid="debug-badge">
    Nodes: {nodeCount} / Accounts: {accountCount} / Zentren: {centerCount} / Edges:
    {edgeCount}
    <br />
    API: {diagnostics.apiMode} / Basemap: {diagnostics.basemapMode}
    {#if diagnostics.degraded}
      <br />⚠ Load: {loadState}
    {/if}
  </div>
{/if}

{#if initFailed}
  <div class="map-init-error" role="alert" data-testid="map-init-error">
    <p>
      Die Karte kann gerade nicht dargestellt werden. Das kann an der
      Grafikunterstützung deines Browsers oder an der Verbindung liegen.
    </p>
    <a class="map-init-error__list" href="/commons">Einträge als Liste öffnen</a
    >
    <button
      type="button"
      class="map-init-error__retry"
      data-testid="map-init-error-retry"
      onclick={() => dispatch("retry")}
    >
      Erneut laden
    </button>
  </div>
{:else if loading}
  <div class="loading-overlay">
    <div class="spinner"></div>
  </div>
{/if}

<style>
  .loading-overlay {
    position: absolute;
    inset: 0;
    background: var(--bg);
    display: grid;
    place-items: center;
    /* Keep the map state above the canvas but below persistent navigation. */
    z-index: calc(var(--z-map-direction) - 10);
    transition: opacity 0.3s;
  }
  .spinner {
    width: 40px;
    height: 40px;
    border: 3px solid rgba(255, 255, 255, 0.1);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin 1s linear infinite;
  }
  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }

  /* Shares the loading layer so the failure state is never covered by the
     overlay it replaces. */
  .map-init-error {
    position: absolute;
    inset: 0;
    background: var(--bg);
    display: grid;
    place-content: center;
    justify-items: center;
    gap: 16px;
    padding: 24px;
    text-align: center;
    /* A failed map must not make settings, messages or recovery tools unreachable. */
    z-index: calc(var(--z-map-direction) - 10);
  }
  .map-init-error p {
    margin: 0;
    max-width: 32rem;
    color: var(--text);
  }
  .map-init-error__list,
  .map-init-error__retry {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-height: 44px;
    padding: 10px 18px;
    border: 1px solid var(--accent);
    border-radius: 8px;
    background: transparent;
    color: var(--text);
    font: inherit;
    cursor: pointer;
  }
  .map-init-error__list {
    font-weight: 600;
    text-decoration: underline;
  }
  .map-init-error__list:hover,
  .map-init-error__retry:hover {
    background: var(--accent);
  }

  .debug-badge {
    position: absolute;
    top: 60px;
    right: 10px;
    z-index: var(--z-map-debug);
    padding: 4px 8px;
    background: rgba(0, 0, 0, 0.7);
    color: #fff;
    font-size: 10px;
    border-radius: 4px;
    pointer-events: none;
    font-family: monospace;
  }

  .degraded-banner--failed {
    background: rgba(180, 40, 40, 0.9);
  }
</style>
