<script lang="ts">
  import { createEventDispatcher } from "svelte";
  import FanAction from "./FanAction.svelte";
  import { TOOL_FAN_BRANCH, type ToolFanBranch } from "$lib/stores/mapChrome";

  interface Props {
    open?: boolean;
    branch?: ToolFanBranch;
    searchOpen?: boolean;
    filterOpen?: boolean;
    compositionActive?: boolean;
    activeFilterCount?: number;
    canCreateNode?: boolean;
    canCreateProposal?: boolean;
    hasWeavingAction?: boolean;
  }

  let {
    open = false,
    branch = TOOL_FAN_BRANCH.root,
    searchOpen = false,
    filterOpen = false,
    compositionActive = false,
    activeFilterCount = 0,
    canCreateNode = false,
    canCreateProposal = false,
    hasWeavingAction = false,
  }: Props = $props();

  const dispatch = createEventDispatcher<{
    find: void;
    mapContent: void;
    openWeave: void;
    back: void;
    createNode: void;
    close: void;
  }>();
</script>

<div
  id="tool-fan-actions"
  class="fan-panel"
  class:open
  class:root={branch === TOOL_FAN_BRANCH.root}
  role="group"
  aria-label={branch === TOOL_FAN_BRANCH.root
    ? "Werkzeugfächer"
    : "Webungsaktionen"}
  aria-hidden={!open}
>
  {#if branch === TOOL_FAN_BRANCH.root}
    <div class="fan-row fan-row--root">
      <div class="fan-slot fan-slot--left">
        <FanAction
          label="Finden"
          symbol="⌕"
          testId="tool-fan-find"
          active={searchOpen}
          tabIndex={open ? 0 : -1}
          onclick={() => dispatch("find")}
        />
      </div>
      <div class="fan-slot fan-slot--center">
        <FanAction
          label="Karteninhalt"
          symbol="◉"
          testId="tool-fan-map-content"
          active={filterOpen}
          tabIndex={open ? 0 : -1}
          onclick={() => dispatch("mapContent")}
        >
          {#if activeFilterCount > 0}
            <span class="count-badge" aria-label={`${activeFilterCount} aktiv`}
              >{activeFilterCount}</span
            >
          {/if}
        </FanAction>
      </div>
      <div class="fan-slot fan-slot--right">
        <FanAction
          label="Weben"
          symbol="⌁"
          testId="tool-fan-weave"
          active={compositionActive}
          disabled={!hasWeavingAction}
          tabIndex={open && hasWeavingAction ? 0 : -1}
          ariaLabel={hasWeavingAction
            ? "Webungsaktionen öffnen"
            : "Weben – Anmeldung erforderlich"}
          title={hasWeavingAction
            ? "Webungsaktionen öffnen"
            : "Zum Weben ist eine Anmeldung erforderlich"}
          onclick={() => dispatch("openWeave")}
        />
      </div>
    </div>
  {:else}
    <div class="branch-heading">
      <button
        type="button"
        class="branch-back"
        data-testid="tool-fan-back"
        aria-label="Zurück zu den Werkzeugen"
        tabindex={open ? 0 : -1}
        onclick={() => dispatch("back")}>←</button
      >
      <span>Weben</span>
    </div>
    <div class="fan-row fan-row--branch">
      <FanAction
        label="Knoten knüpfen"
        symbol="＋"
        testId="tool-fan-create-node"
        disabled={!canCreateNode}
        tabIndex={open && canCreateNode ? 0 : -1}
        ariaLabel={canCreateNode
          ? undefined
          : "Knoten knüpfen – Anmeldung erforderlich"}
        title={canCreateNode
          ? "Neuen Knoten auf der Karte knüpfen"
          : "Zum Knotenknüpfen musst du angemeldet sein"}
        onclick={() => dispatch("createNode")}
      />
      <FanAction
        label="Antrag stellen"
        symbol="◇"
        testId="tool-fan-create-proposal"
        href={canCreateProposal ? "/antraege#antrag-stellen" : null}
        disabled={!canCreateProposal}
        tabIndex={open && canCreateProposal ? 0 : -1}
        ariaLabel={canCreateProposal
          ? undefined
          : "Antrag stellen – derzeit ist nur der Weberantrag für Gäste freigeschaltet"}
        title={canCreateProposal
          ? "Weberstatus beantragen"
          : "Weitere Antragstypen sind noch nicht freigeschaltet"}
        onclick={() => dispatch("close")}
      />
    </div>
    <p class="branch-note">
      {#if canCreateProposal}
        Dein Weberantrag ist eine eigene Webungsaktion.
      {:else if canCreateNode}
        Allgemeine Antragstypen sind noch nicht produktiv freigeschaltet.
      {:else}
        Melde dich als Gast an, um den Weberstatus zu beantragen.
      {/if}
    </p>
  {/if}
</div>

<style>
  .fan-panel {
    box-sizing: border-box;
    position: absolute;
    left: 50%;
    bottom: calc(100% + var(--tool-fan-menu-gap));
    width: min(var(--tool-fan-menu-width), calc(100vw - 24px));
    padding: 0.75rem;
    border: 1px solid var(--panel-border);
    border-radius: 18px;
    background-color: var(--panel);
    background-image: var(--panel-porcelain-image);
    background-size:
      192px 192px,
      100% 100%;
    background-repeat: repeat, no-repeat;
    box-shadow: var(--shadow);
    backdrop-filter: blur(var(--map-lens-blur));
    opacity: 0;
    visibility: hidden;
    pointer-events: none;
    transform: translateX(-50%) translateY(10px) scale(0.97);
    transform-origin: bottom center;
    transition:
      transform var(--motion-fan),
      opacity var(--motion-fan-fast),
      visibility 0s linear var(--motion-fan-duration);
  }

  .fan-panel.open {
    opacity: 1;
    visibility: visible;
    pointer-events: auto;
    transform: translateX(-50%) translateY(0) scale(1);
    transition-delay: 0s;
  }

  .fan-panel.root {
    width: max-content;
    max-width: min(var(--tool-fan-menu-width), calc(100vw - 24px));
    padding: 0;
    border: 0;
    background: transparent;
    box-shadow: none;
    backdrop-filter: none;
  }

  .fan-panel.root.open {
    pointer-events: none;
  }

  .fan-panel.root .fan-row--root :global(.fan-action) {
    min-width: 104px;
    min-height: 44px;
    padding-inline: 0.7rem;
    pointer-events: auto;
  }

  .fan-row {
    display: flex;
    align-items: flex-end;
    justify-content: center;
    gap: 0.55rem;
  }

  .fan-row--root .fan-slot {
    transition: transform var(--motion-fan);
  }

  .fan-row--root .fan-slot--center {
    transform: translateY(-10px);
  }

  .fan-row--branch {
    align-items: stretch;
    flex-wrap: wrap;
  }

  .branch-heading {
    min-height: 44px;
    margin-bottom: 0.4rem;
    display: flex;
    align-items: center;
    gap: 0.55rem;
    color: var(--muted);
    font-size: 0.8rem;
    font-weight: 750;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .branch-back {
    width: 44px;
    height: 44px;
    border: 1px solid var(--panel-border-strong);
    border-radius: 999px;
    background: transparent;
    color: var(--text);
    font: inherit;
    cursor: pointer;
  }

  .branch-back:focus-visible {
    outline: 3px solid var(--accent);
    outline-offset: 3px;
  }

  .branch-note {
    margin: 0.65rem 0 0;
    color: var(--muted);
    font-size: 0.75rem;
    line-height: 1.35;
    text-align: center;
  }

  .count-badge {
    display: grid;
    place-items: center;
    min-width: 1.25rem;
    height: 1.25rem;
    margin-left: auto;
    padding: 0 0.25rem;
    border-radius: 999px;
    background: var(--accent);
    color: var(--bg);
    font-size: 0.72rem;
    font-variant-numeric: tabular-nums;
  }

  @media (min-width: 769px) and (max-width: 900px) {
    :global(.tool-fan.panel-open) .fan-row--root :global(.fan-action) {
      min-width: 0;
      width: 92px;
      padding-inline: 0.55rem;
      font-size: 0.78rem;
    }
  }

  @media (max-width: 520px) {
    .fan-panel {
      width: min(304px, calc(100vw - 16px));
      padding: 0.6rem;
    }

    .fan-panel.root {
      width: max-content;
      max-width: min(304px, calc(100vw - 16px));
      padding: 0;
    }

    .fan-row {
      gap: 0.35rem;
    }

    .fan-panel.root .fan-row--root :global(.fan-action) {
      min-width: 0;
      width: 92px;
      padding-inline: 0.55rem;
      font-size: 0.78rem;
    }

    .fan-row--branch :global(.fan-action) {
      flex: 1 1 132px;
      min-width: 132px;
    }
  }

  @media (prefers-reduced-transparency: reduce) {
    .fan-panel:not(.root) {
      background-color: var(--panel-solid);
      backdrop-filter: none;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .fan-panel,
    .fan-row--root .fan-slot {
      transition: none;
    }
  }
</style>
