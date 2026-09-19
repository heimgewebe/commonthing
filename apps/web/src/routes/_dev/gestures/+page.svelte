<script lang="ts">
  import {
    swipe,
    type SwipeDirection,
    type SwipeMeta,
    type SwipeOptions,
    type SwipeRejectMeta,
  } from "$lib/gestures";

  let threshold = $state(24);
  let angleRatio = $state(0.5);
  let velocityMin = $state(0.3);
  let lockAxis = $state(true);
  let axisDeadzone = $state(6);
  let passiveMove = $state(true);
  let allowMouse = $state(true);

  let lastDirection: SwipeDirection | "—" = $state("—");
  let lastMeta: SwipeMeta | null = $state(null);
  let lastReject: SwipeRejectMeta | null = $state(null);
  let log: string[] = $state([]);

  function addLog(message: string) {
    log = [message, ...log].slice(0, 6);
  }

  function handleSwipe(direction: SwipeDirection, meta: SwipeMeta) {
    lastDirection = direction;
    lastMeta = meta;
    lastReject = null;
    addLog(
      `${direction === "left" ? "←" : "→"} dx=${meta.dx.toFixed(1)} v=${meta.v.toFixed(2)}`,
    );
  }

  function handleReject(meta: SwipeRejectMeta) {
    lastDirection = "—";
    lastReject = meta;
    addLog(
      `× dx=${meta.dx.toFixed(1)} v=${meta.v.toFixed(2)} ` +
        `[h:${meta.horizontalEnough ? "✓" : "×"} l:${meta.longEnough ? "✓" : "×"} v:${
          meta.fastEnough ? "✓" : "×"
        }]`,
    );
  }

  let currentOptions = $derived({
    threshold,
    angleRatio,
    velocityMin,
    lockAxis,
    axisDeadzone,
    passiveMove,
    allowMouse,
    onSwipe: handleSwipe,
    onReject: handleReject,
  } satisfies SwipeOptions);
</script>

<svelte:head>
  <title>Swipe Debug Playground</title>
</svelte:head>

<div class="col dev-page">
  <header class="col">
    <h1>Swipe Playground</h1>
    <p class="ghost">
      Passe Schwellwerte an und teste horizontale Swipes (Touch/Pen oder Maus
      wenn aktiviert). Der aktive Bereich nutzt die globale <code
        >.swipeable</code
      >-Konfiguration.
    </p>
  </header>

  <section class="panel col">
    <h2>Parameter</h2>
    <div class="row">
      <label class="col dev-page__field">
        <span>threshold: {threshold}px</span>
        <input type="range" min="8" max="64" step="1" bind:value={threshold} />
      </label>
      <label class="col dev-page__field">
        <span>angleRatio: {angleRatio.toFixed(2)}</span>
        <input
          type="range"
          min="0.2"
          max="0.9"
          step="0.05"
          bind:value={angleRatio}
        />
      </label>
    </div>
    <div class="row">
      <label class="col dev-page__field">
        <span>velocityMin: {velocityMin.toFixed(2)} px/ms</span>
        <input
          type="range"
          min="0.1"
          max="0.6"
          step="0.02"
          bind:value={velocityMin}
        />
      </label>
      <label class="col dev-page__field">
        <span>axisDeadzone: {axisDeadzone}px</span>
        <input
          type="range"
          min="0"
          max="20"
          step="1"
          bind:value={axisDeadzone}
        />
      </label>
    </div>
    <div class="row dev-page__toggles">
      <label class="row dev-page__toggle">
        <input type="checkbox" bind:checked={lockAxis} />
        <span>lockAxis</span>
      </label>
      <label class="row dev-page__toggle">
        <input type="checkbox" bind:checked={passiveMove} />
        <span>passiveMove</span>
      </label>
      <label class="row dev-page__toggle">
        <input type="checkbox" bind:checked={allowMouse} />
        <span>allowMouse</span>
      </label>
    </div>
  </section>

  <section class="panel col dev-page__section">
    <h2>Testfläche</h2>
    <div class="swipe-parent dev-page__stage">
      <div class="swipeable panel dev-page__surface" use:swipe={currentOptions}>
        <div>
          <p class="dev-page__direction">{lastDirection}</p>
          <p class="ghost dev-page__flush">
            Wische horizontal, um Richtung und Metadaten zu sehen. Vertikales
            Scrollen bleibt möglich.
          </p>
        </div>
      </div>
    </div>
    <div class="row dev-page__readouts">
      <div class="col dev-page__readout">
        <h3 class="ghost dev-page__flush">Letzter Swipe</h3>
        {#if lastMeta}
          <code
            >dx={lastMeta.dx.toFixed(1)} dy={lastMeta.dy.toFixed(1)} v={lastMeta.v.toFixed(
              2,
            )}</code
          >
        {:else}
          <span class="ghost">noch kein Swipe</span>
        {/if}
      </div>
      <div class="col dev-page__readout">
        <h3 class="ghost dev-page__flush">Letzte Ablehnung</h3>
        {#if lastReject}
          <code>
            dx={lastReject.dx.toFixed(1)} dy={lastReject.dy.toFixed(1)} v={lastReject.v.toFixed(
              2,
            )}
            [h:{lastReject.horizontalEnough ? "✓" : "×"} l:{lastReject.longEnough
              ? "✓"
              : "×"} v:{lastReject.fastEnough ? "✓" : "×"}]
          </code>
        {:else}
          <span class="ghost">bisher keine Ablehnung</span>
        {/if}
      </div>
    </div>
  </section>

  <section class="panel col dev-page__log">
    <h2>Log</h2>
    {#if log.length === 0}
      <span class="ghost">Noch keine Ereignisse</span>
    {:else}
      <ul class="dev-page__log-list">
        {#each log as entry, index (index)}
          <li><code>{entry}</code></li>
        {/each}
      </ul>
    {/if}
  </section>
</div>

<style>
  /* Keine Inline-Styles: style-src laeuft ohne 'unsafe-inline'. */
  .dev-page {
    gap: 1.5rem;
    padding: 1.5rem;
    max-width: 960px;
    margin: 0 auto;
  }

  .dev-page__flush {
    margin: 0;
  }

  .dev-page__field {
    flex: 1;
  }

  .dev-page__toggles {
    flex-wrap: wrap;
    gap: 0.75rem;
  }

  .dev-page__toggle {
    gap: 0.35rem;
  }

  .dev-page__section {
    gap: 1rem;
  }

  .dev-page__stage {
    max-width: 100%;
  }

  .dev-page__surface {
    min-height: 200px;
    display: flex;
    align-items: center;
    justify-content: center;
    text-align: center;
  }

  .dev-page__direction {
    font-size: 2rem;
    margin: 0 0 0.5rem;
  }

  .dev-page__readouts {
    align-items: flex-start;
    gap: 1rem;
  }

  .dev-page__readout {
    flex: 1;
  }

  .dev-page__log {
    gap: 0.75rem;
  }

  .dev-page__log-list {
    margin: 0;
    padding-left: 1.2rem;
  }
</style>
