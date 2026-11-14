<script lang="ts">
  import { onMount, tick } from "svelte";
  import { authStore } from "$lib/auth/store";
  import { contextPanelOpen, enterKomposition } from "$lib/stores/uiView";
  import { isSearchOpen } from "$lib/stores/searchStore";
  import { isFilterOpen } from "$lib/stores/filterStore";
  import { mapChrome } from "$lib/stores/mapChrome";
  import { openSearchExclusive } from "$lib/stores/overlayManager";
  import { setRestoreTarget } from "$lib/utils/focusManager";

  const storageKey = "commonthing.map-welcome.dismissed.v1";
  let ready = $state(false);
  let open = $state(false);
  let trigger: HTMLButtonElement | undefined = $state();
  let card: HTMLElement | undefined = $state();
  let container: HTMLDivElement | undefined = $state();
  const busy = $derived(
    $contextPanelOpen ||
      $isSearchOpen ||
      $isFilterOpen ||
      $mapChrome.toolFanOpen ||
      $mapChrome.attentionOverflowOpen ||
      $mapChrome.attentionCardOpen,
  );

  onMount(() => {
    try {
      open = localStorage.getItem(storageKey) !== "1";
    } catch {
      open = true;
    }
    ready = true;
  });

  function dismiss(restore = false) {
    open = false;
    try {
      localStorage.setItem(storageKey, "1");
    } catch {
      // The introduction also works without browser storage.
    }
    if (restore) void tick().then(() => trigger?.focus());
  }

  $effect(() => {
    // Yield permanently for this visit when another map surface is used.
    if (ready && busy && open) dismiss();
  });

  function find() {
    if (trigger) setRestoreTarget("search", trigger);
    dismiss();
    openSearchExclusive();
  }

  function offer() {
    dismiss();
    enterKomposition({ mode: "new-knoten", source: "tool-fan" });
  }

  function keydown(event: KeyboardEvent) {
    if (!open || busy || event.defaultPrevented || event.key !== "Escape")
      return;
    event.preventDefault();
    dismiss(!!card?.contains(document.activeElement));
  }
</script>

<svelte:window
  onkeydown={keydown}
  onpointerdown={(event) => {
    if (ready && open && !busy && !container?.contains(event.target as Node))
      dismiss();
  }}
/>

{#if ready}
  <div class="welcome" bind:this={container} hidden={busy}>
    {#if open && !busy}
      <section
        bind:this={card}
        id="map-welcome"
        aria-labelledby="map-welcome-title"
        data-testid="map-welcome"
      >
        <header>
          <h2 id="map-welcome-title">Was möchtest du hier tun?</h2>
          <button
            aria-label="Einführung schließen"
            onclick={() => dismiss(true)}>×</button
          >
        </header>
        <p>
          Finde Menschen und Ideen in deiner Nähe oder bring etwas Eigenes ein.
        </p>
        <div class="actions">
          <button onclick={find}
            >Finden <small>Auf der Karte suchen</small></button
          >
          {#if $authStore.authenticated}
            <button onclick={offer}
              >Anbieten <small>Einen Knoten knüpfen</small></button
            >
          {:else}
            <a href="/login">Anbieten <small>Zuerst anmelden</small></a>
          {/if}
          <a href="/antraege" onclick={() => dismiss()}
            >Mitmachen <small>Gemeinsame Entscheidungen ansehen</small></a
          >
        </div>
        <p class="terms">
          Deine <strong>Garnrolle</strong> ist dein Profil. Ein
          <strong>Knoten</strong>
          ist ein Ort, Angebot oder Projekt. Ein <strong>Faden</strong> zeigt eine
          Verbindung.
        </p>
      </section>
    {/if}
    <button
      class="recall"
      bind:this={trigger}
      aria-expanded={open}
      aria-controls={open ? "map-welcome" : undefined}
      onclick={() => {
        open = !open;
        if (!open) dismiss();
        else
          void tick().then(() =>
            card?.querySelector<HTMLButtonElement>(".actions button")?.focus(),
          );
      }}
    >
      {open ? "Später entdecken" : "Karte kennenlernen"}
    </button>
  </div>
{/if}

<style>
  .welcome {
    position: fixed;
    bottom: calc(
      var(--tool-fan-edge) + var(--tool-fan-size) + 56px +
        env(safe-area-inset-bottom)
    );
    left: 50%;
    transform: translateX(-50%);
    width: min(380px, calc(100vw - 24px));
    z-index: var(--z-map-fan);
    display: grid;
    justify-items: center;
    gap: 4px;
  }
  .welcome[hidden] {
    display: none;
  }
  section {
    box-sizing: border-box;
    width: 100%;
    max-height: calc(
      100dvh - var(--toolbar-offset) - 204px - env(safe-area-inset-bottom) -
        env(safe-area-inset-top)
    );
    overflow: auto;
    padding: 12px;
    border: 1px solid var(--panel-border-strong);
    border-radius: 16px;
    background: var(--panel-solid);
    color: var(--text);
    box-shadow: var(--shadow);
  }
  header {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  h2 {
    font-size: 1rem;
    margin: 0;
    flex: 1;
  }
  p {
    font-size: 0.875rem;
    line-height: 1.4;
    margin: 8px 0;
  }
  .terms {
    margin-bottom: 0;
  }
  .actions {
    display: grid;
    gap: 6px;
  }
  button,
  a {
    box-sizing: border-box;
    min-height: 44px;
    border: 1px solid var(--panel-border-strong);
    border-radius: 10px;
    background: var(--panel-solid);
    color: var(--text);
    font: inherit;
    cursor: pointer;
    padding: 8px 10px;
    text-decoration: none;
  }
  header button {
    min-width: 44px;
  }
  .actions button,
  .actions a {
    text-align: left;
    font-weight: 700;
  }
  small {
    display: block;
    font-size: 0.8rem;
    font-weight: 400;
  }
  .recall {
    font-size: 0.8rem;
    border-radius: 999px;
  }
  button:focus-visible,
  a:focus-visible {
    outline: 3px solid var(--accent);
    outline-offset: 2px;
  }
</style>
