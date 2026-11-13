<script lang="ts">
  import { flip } from "svelte/animate";
  import { onMount } from "svelte";
  import {
    accountAttentionRuntime,
    retainAccountAttentionRuntime,
  } from "$lib/accountAttentionRuntime";
  import {
    setAttentionCardOpen,
    setAttentionOverflowOpen,
  } from "$lib/stores/mapChrome";
  import {
    attentionMeaningLabel,
    attentionMeaningMark,
    unreadMessageBadgeLabel,
  } from "./topBarAttentionState";

  let attentionEl: HTMLElement | undefined = $state();
  let controlCapacity = $state(3);
  let reducedMotion = $state(false);
  let overflowOpen = $state(false);
  let activeItemId: string | null = $state(null);

  let visibleCount = $derived.by(() => {
    const total = $accountAttentionRuntime.items.length;
    return total <= controlCapacity ? total : Math.max(1, controlCapacity - 1);
  });
  let visibleItems = $derived(
    $accountAttentionRuntime.items.slice(0, visibleCount),
  );
  let hiddenItems = $derived(
    $accountAttentionRuntime.items.slice(visibleCount),
  );
  let activeItem = $derived(
    $accountAttentionRuntime.items.find((item) => item.id === activeItemId),
  );

  function attentionSymbol(kind: string): string {
    switch (kind) {
      case "direct_message":
        return "✉";
      case "weber_application":
      case "own_proposal":
      case "waiting_summary":
        return "◌";
      default:
        return "◇";
    }
  }

  function attentionActionLabel(kind: string): string {
    switch (kind) {
      case "direct_message":
        return "Nachricht öffnen";
      case "weber_application":
        return "Weberantrag öffnen";
      case "own_proposal":
        return "Antrag öffnen";
      case "waiting_summary":
        return "Anträge öffnen";
      default:
        return "Antrag öffnen";
    }
  }

  function selectAttention(id: string): void {
    activeItemId = activeItemId === id ? null : id;
    overflowOpen = false;
  }

  function selectOverflowAttention(id: string): void {
    activeItemId = id;
    overflowOpen = false;
  }

  function closeAttentionSurfaces(): void {
    activeItemId = null;
    overflowOpen = false;
  }

  function handleWindowPointerDown(event: PointerEvent): void {
    if ((!activeItem && !overflowOpen) || !attentionEl) return;
    if (!attentionEl.contains(event.target as Node)) closeAttentionSurfaces();
  }

  function handleWindowKeydown(event: KeyboardEvent): void {
    if (event.key !== "Escape" || (!activeItem && !overflowOpen)) return;
    event.preventDefault();
    closeAttentionSurfaces();
  }

  function syncViewportState(): void {
    const topbar = document.querySelector<HTMLElement>(".topbar");
    const topbarWidth =
      topbar?.getBoundingClientRect().width ?? window.innerWidth;
    const rootFontSize =
      Number.parseFloat(getComputedStyle(document.documentElement).fontSize) ||
      16;
    const gap = rootFontSize * 0.35;
    const leftTrackWidth = Math.max(44, (topbarWidth - 24) / 2);
    controlCapacity = Math.max(
      2,
      Math.min(7, Math.floor((leftTrackWidth + gap) / (44 + gap))),
    );
  }

  $effect(() => {
    setAttentionOverflowOpen(hiddenItems.length > 0 && overflowOpen);
  });

  $effect(() => {
    setAttentionCardOpen(Boolean(activeItem));
  });

  $effect(() => {
    if (activeItemId !== null && !activeItem) activeItemId = null;
  });

  onMount(() => {
    const releaseRuntime = retainAccountAttentionRuntime();
    const motionQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
    const syncMotion = () => {
      reducedMotion = motionQuery.matches;
    };
    const topbar = document.querySelector<HTMLElement>(".topbar");
    const resizeObserver = new ResizeObserver(syncViewportState);
    if (topbar) resizeObserver.observe(topbar);
    syncViewportState();
    syncMotion();
    motionQuery.addEventListener("change", syncMotion);

    return () => {
      setAttentionOverflowOpen(false);
      setAttentionCardOpen(false);
      releaseRuntime();
      resizeObserver.disconnect();
      motionQuery.removeEventListener("change", syncMotion);
    };
  });
</script>

<svelte:window
  onpointerdown={handleWindowPointerDown}
  onkeydown={handleWindowKeydown}
/>

{#if $accountAttentionRuntime.items.length > 0}
  <nav
    class="attention-bubbles"
    bind:this={attentionEl}
    data-testid="attention-bubbles"
    aria-label="Aktuelle Aufmerksamkeit"
  >
    <div class="attention-row">
      {#each visibleItems as item (item.id)}
        <button
          type="button"
          class="attention-bubble"
          class:personal={item.kind !== "governance"}
          class:active={activeItem?.id === item.id}
          data-attention-id={item.id}
          data-attention-kind={item.kind}
          data-attention-meaning={item.meaning}
          aria-label={`${attentionMeaningLabel(item.meaning)}. ${item.label}: ${item.detail}${item.deadlineLabel ? `. ${item.deadlineLabel}` : ""}`}
          aria-expanded={activeItem?.id === item.id}
          aria-controls="attention-card"
          title={`${attentionMeaningLabel(item.meaning)} · ${item.label} · ${item.detail}${item.deadlineLabel ? ` · ${item.deadlineLabel}` : ""}`}
          onclick={() => selectAttention(item.id)}
          animate:flip={{ duration: reducedMotion ? 0 : 170 }}
        >
          <span class="attention-symbol" aria-hidden="true">
            {attentionSymbol(item.kind)}
          </span>
          <span class="attention-meaning-mark" aria-hidden="true">
            {attentionMeaningMark(item.meaning)}
          </span>
          {#if item.count && item.count > 1}
            <span class="attention-count" aria-hidden="true">
              {unreadMessageBadgeLabel(item.count)}
            </span>
          {/if}
        </button>
      {/each}

      {#if hiddenItems.length > 0}
        <details
          class="attention-overflow"
          bind:open={overflowOpen}
          ontoggle={() => {
            if (overflowOpen) activeItemId = null;
          }}
        >
          <summary
            class="attention-overflow-trigger"
            aria-label={`${hiddenItems.length} weitere Aufmerksamkeitseinheiten`}
          >
            +{hiddenItems.length}
          </summary>
          <div
            class="attention-overflow-menu"
            data-testid="attention-overflow-menu"
          >
            {#each hiddenItems as item (item.id)}
              <button
                type="button"
                class="attention-overflow-item"
                data-attention-id={item.id}
                data-attention-meaning={item.meaning}
                onclick={() => selectOverflowAttention(item.id)}
              >
                <span
                  class="overflow-symbol"
                  data-attention-meaning={item.meaning}
                  aria-hidden="true"
                >
                  {attentionSymbol(item.kind)}
                  <span class="overflow-meaning-mark">{attentionMeaningMark(item.meaning)}</span>
                </span>
                <span class="overflow-copy">
                  <span class="overflow-meaning"
                    >{attentionMeaningLabel(item.meaning)}</span
                  >
                  <strong>{item.label}</strong>
                  <span class="overflow-detail">{item.detail}</span>
                  {#if item.deadline && item.deadlineLabel}
                    <time class="overflow-deadline" datetime={item.deadline}>{item.deadlineLabel}</time>
                  {/if}
                </span>
                {#if item.count && item.count > 1}
                  <span class="overflow-count">
                    {unreadMessageBadgeLabel(item.count)}
                  </span>
                {/if}
              </button>
            {/each}
          </div>
        </details>
      {/if}
    </div>

    {#if activeItem}
      <section
        id="attention-card"
        class="attention-card"
        data-testid="attention-card"
        data-attention-meaning={activeItem.meaning}
        aria-labelledby="attention-card-title"
      >
        <div
          class="attention-card-symbol"
          data-attention-meaning={activeItem.meaning}
          aria-hidden="true"
        >
          {attentionSymbol(activeItem.kind)}
        </div>
        <div class="attention-card-copy">
          <p class="attention-card-meaning">
            {attentionMeaningLabel(activeItem.meaning)}
          </p>
          <h2 id="attention-card-title">{activeItem.label}</h2>
          <p>{activeItem.detail}</p>
          {#if activeItem.deadline && activeItem.deadlineLabel}
            <time class="attention-card-deadline" datetime={activeItem.deadline}>
              {activeItem.deadlineLabel}
            </time>
          {/if}
        </div>
        <a class="attention-card-action" href={activeItem.href}>
          {attentionActionLabel(activeItem.kind)}
        </a>
      </section>
    {/if}
  </nav>
{/if}

<style>
  .attention-bubbles {
    position: relative;
    grid-column: 1;
    justify-self: start;
    min-width: 0;
    pointer-events: auto;
  }

  .attention-row {
    display: flex;
    align-items: center;
    gap: 0.35rem;
    min-width: 0;
  }

  .attention-bubble,
  .attention-overflow-trigger {
    box-sizing: border-box;
    position: relative;
    display: grid;
    place-items: center;
    width: 44px;
    min-width: 44px;
    height: 44px;
    padding: 0;
    border: 1px solid var(--panel-border-strong);
    border-radius: 50%;
    background: var(--panel);
    color: var(--text);
    box-shadow: var(--shadow);
    backdrop-filter: blur(var(--map-lens-blur));
    font: inherit;
    text-decoration: none;
    cursor: pointer;
  }

  .attention-bubble.active {
    border-color: var(--accent);
    box-shadow:
      0 0 0 2px var(--accent-soft),
      var(--shadow);
  }

  .attention-bubble.personal {
    border-color: color-mix(
      in srgb,
      var(--accent) 65%,
      var(--panel-border-strong)
    );
  }

  .attention-bubble[data-attention-meaning="required"] {
    border-width: 2px;
    border-color: var(--accent);
  }

  .attention-bubble[data-attention-meaning="available"],
  .overflow-symbol[data-attention-meaning="available"],
  .attention-card-symbol[data-attention-meaning="available"] {
    border-width: 2px;
  }

  .attention-bubble[data-attention-meaning="waiting"],
  .overflow-symbol[data-attention-meaning="waiting"],
  .attention-card-symbol[data-attention-meaning="waiting"] {
    box-shadow: none;
  }

  .attention-meaning-mark {
    position: absolute;
    bottom: -0.16rem;
    left: -0.16rem;
    display: grid;
    place-items: center;
    min-width: 1rem;
    height: 1rem;
    padding: 0 0.12rem;
    border: 2px solid var(--panel);
    border-radius: 999px;
    background: var(--text);
    color: var(--panel);
    font-size: 0.62rem;
    font-weight: 900;
    line-height: 1;
  }

  .attention-symbol {
    font-size: 1.08rem;
    line-height: 1;
  }

  .attention-count {
    position: absolute;
    top: -0.3rem;
    right: -0.35rem;
    box-sizing: border-box;
    display: grid;
    place-items: center;
    min-width: 1.2rem;
    height: 1.2rem;
    padding: 0 0.18rem;
    border: 2px solid var(--panel);
    border-radius: 999px;
    background: var(--accent);
    color: var(--accent-contrast, #fff);
    font-size: 0.62rem;
    font-weight: 800;
    line-height: 1;
  }

  .attention-overflow {
    position: relative;
  }

  .attention-overflow > summary {
    list-style: none;
  }

  .attention-overflow > summary::-webkit-details-marker {
    display: none;
  }

  .attention-overflow-trigger {
    font: inherit;
    font-size: 0.72rem;
    font-weight: 800;
  }

  .attention-overflow-menu {
    box-sizing: border-box;
    position: absolute;
    top: calc(100% + 8px);
    left: 0;
    width: min(290px, calc(100vw - 24px));
    max-height: min(55vh, 360px);
    overflow: auto;
    display: grid;
    gap: 0.25rem;
    padding: 0.4rem;
    border: 1px solid var(--panel-border-strong);
    border-radius: 16px;
    background: var(--panel);
    box-shadow: var(--shadow);
    backdrop-filter: blur(var(--map-lens-blur));
  }

  .attention-overflow-item {
    box-sizing: border-box;
    display: grid;
    width: 100%;
    grid-template-columns: 32px minmax(0, 1fr) auto;
    align-items: center;
    gap: 0.5rem;
    min-height: 44px;
    padding: 0.35rem 0.45rem;
    border: 0;
    border-radius: 12px;
    background: transparent;
    color: var(--text);
    font: inherit;
    text-align: left;
    cursor: pointer;
  }

  .attention-overflow-item:hover {
    background: var(--accent-soft);
  }

  .overflow-symbol {
    position: relative;
    display: grid;
    place-items: center;
    width: 32px;
    height: 32px;
    border: 1px solid var(--panel-border-strong);
    border-radius: 50%;
  }

  .overflow-copy {
    display: grid;
    gap: 0.1rem;
    min-width: 0;
  }

  .overflow-meaning-mark {
    position: absolute;
    right: -0.3rem;
    bottom: -0.25rem;
    font-size: 0.65rem;
    font-weight: 900;
  }

  .overflow-copy .overflow-meaning {
    color: var(--accent);
    font-size: 0.62rem;
    font-weight: 800;
  }

  .overflow-copy strong,
  .overflow-copy span {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .overflow-copy strong {
    font-size: 0.78rem;
  }

  .overflow-copy span {
    color: var(--text-muted, var(--text));
    font-size: 0.7rem;
  }

  .overflow-count {
    min-width: 1.5rem;
    padding: 0.2rem 0.35rem;
    border-radius: 999px;
    background: var(--accent-soft);
    color: var(--accent);
    font-size: 0.68rem;
    font-weight: 800;
    text-align: center;
  }

  .attention-card {
    box-sizing: border-box;
    position: absolute;
    top: calc(100% + 8px);
    left: 0;
    width: min(290px, calc(100vw - 24px));
    display: grid;
    grid-template-columns: 36px minmax(0, 1fr);
    gap: 0.6rem;
    padding: 0.7rem;
    border: 1px solid var(--panel-border-strong);
    border-radius: 16px;
    background: var(--panel);
    box-shadow: var(--shadow);
    backdrop-filter: blur(var(--map-lens-blur));
  }

  .attention-card-symbol {
    display: grid;
    place-items: center;
    width: 36px;
    height: 36px;
    border: 1px solid var(--panel-border-strong);
    border-radius: 50%;
    font-size: 1rem;
  }

  .attention-card-copy {
    min-width: 0;
  }

  .overflow-copy .overflow-deadline,
  .attention-card-copy .attention-card-deadline {
    display: block;
    margin-top: 0.16rem;
    color: var(--text);
    font-size: 0.68rem;
    font-weight: 800;
    line-height: 1.25;
  }

  .attention-card-copy .attention-card-meaning {
    margin: 0 0 0.14rem;
    color: var(--accent);
    font-size: 0.66rem;
    font-weight: 800;
    line-height: 1.2;
  }

  .attention-card-copy h2,
  .attention-card-copy p {
    margin: 0;
  }

  .attention-card-copy h2 {
    overflow-wrap: anywhere;
    font-size: 0.86rem;
    line-height: 1.25;
  }

  .attention-card-copy p {
    margin-top: 0.2rem;
    color: var(--text-muted, var(--text));
    font-size: 0.74rem;
    line-height: 1.35;
  }

  .attention-card-action {
    grid-column: 1 / -1;
    box-sizing: border-box;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-height: 44px;
    padding: 0.45rem 0.7rem;
    border-radius: 999px;
    background: var(--accent);
    color: var(--accent-contrast, #fff);
    font-size: 0.78rem;
    font-weight: 800;
    text-decoration: none;
  }

  .attention-bubble:focus-visible,
  .attention-overflow-trigger:focus-visible,
  .attention-overflow-item:focus-visible,
  .attention-card-action:focus-visible {
    outline: 3px solid var(--accent);
    outline-offset: 3px;
  }

  @media (prefers-reduced-transparency: reduce) {
    .attention-bubble,
    .attention-overflow-trigger,
    .attention-overflow-menu,
    .attention-card {
      background: var(--panel-solid);
      backdrop-filter: none;
    }
  }

  @media (prefers-reduced-motion: no-preference) {
    .attention-bubble {
      animation: attention-arrive 150ms ease-out;
    }
  }

  @keyframes attention-arrive {
    from {
      opacity: 0;
    }
    to {
      opacity: 1;
    }
  }
</style>
