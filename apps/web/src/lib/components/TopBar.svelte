<script lang="ts">
  import { onMount } from "svelte";
  import { contextPanelOpen } from "$lib/stores/uiView";

  let AttentionSlot: any = $state();
  let AuthSlot: any = $state();

  onMount(() => {
    void import("./AttentionBubbles.svelte").then((module) => {
      AttentionSlot = module.default;
    });
    void import("./TopBarAuth.svelte").then((module) => {
      AuthSlot = module.default;
    });
  });
</script>

<nav
  class="topbar"
  class:panel-open={$contextPanelOpen}
  aria-label="Navigation"
>
  {#if AttentionSlot}
    <AttentionSlot />
  {/if}
  {#if AuthSlot}
    <AuthSlot />
  {/if}
</nav>

<style>
  .topbar {
    position: absolute;
    inset: 0 0 auto 0;
    min-height: var(--toolbar-offset);
    z-index: var(--z-map-topbar);
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
    align-items: center;
    padding: env(safe-area-inset-top) 12px 0;
    background: linear-gradient(
      180deg,
      color-mix(in srgb, var(--bg) 88%, transparent),
      transparent
    );
    color: var(--text);
    pointer-events: none;
    transition: right var(--motion-ui);
  }

  /* The expanded site menu must outrank the map-welcome recall button.
   * Its inner z-index cannot escape this topbar stacking context. */
  .topbar:has(:global(.site-menu[open])) {
    z-index: calc(var(--z-map-fan) + 1);
  }

  @media (min-width: 769px) {
    .topbar.panel-open {
      right: var(--context-panel-width);
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .topbar {
      transition: none;
    }
  }
</style>
