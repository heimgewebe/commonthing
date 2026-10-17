<script lang="ts">
  import { createEventDispatcher, onDestroy } from "svelte";
  import InfoHeading from "$lib/components/InfoHeading.svelte";
  import { nodeKindLabel } from "$lib/ui/productLanguage";
  import {
    buildSimilarNodeQuery,
    nodeToMapEntity,
    searchNodes,
  } from "$lib/api/search";
  import type { Node, MapEntityViewModel } from "$lib/map/types";

  interface Props {
    sourceId: string;
    title?: string | null | undefined;
    kind?: string | null | undefined;
    summary?: string | null | undefined;
    info?: string | null | undefined;
    tags?: string[] | null | undefined;
  }

  let {
    sourceId,
    title = null,
    kind = null,
    summary = null,
    info = null,
    tags = null,
  }: Props = $props();

  const dispatch = createEventDispatcher<{
    select: {
      type: "node";
      id: string;
      data?: MapEntityViewModel;
      title?: string;
    };
  }>();

  let similarNodes: Node[] = $state([]);
  let requested = $state(false);
  let loading = $state(false);
  let error = $state("");
  let mode: string | null = $state(null);
  let previousKey = $state("");
  let abortController: AbortController | null = $state(null);
  let requestSequence = $state(0);

  let query = $derived(
    buildSimilarNodeQuery({ title, kind, summary, info, tags }),
  );

  async function load(query: string) {
    if (!sourceId || !query) return;
    abortController?.abort();
    const controller = new AbortController();
    abortController = controller;
    const sequence = ++requestSequence;
    requested = true;
    loading = true;
    error = "";
    mode = null;
    similarNodes = [];
    try {
      const response = await searchNodes(query, {
        limit: 6,
        signal: controller.signal,
      });
      if (controller.signal.aborted || sequence !== requestSequence) return;
      mode = response.mode;
      similarNodes = response.items
        .filter((item) => item.id !== sourceId)
        .slice(0, 4);
    } catch {
      if (controller.signal.aborted || sequence !== requestSequence) return;
      error =
        "Maschinell berechnete ähnliche Knoten sind gerade nicht verfügbar.";
    } finally {
      if (sequence === requestSequence) loading = false;
      if (abortController === controller) abortController = null;
    }
  }

  $effect.pre(() => {
    const key = sourceId && query ? `${sourceId}\u0000${query}` : "";
    if (key !== previousKey) {
      previousKey = key;
      requestSequence += 1;
      abortController?.abort();
      abortController = null;
      similarNodes = [];
      requested = false;
      loading = false;
      error = "";
      mode = null;
    }
  });

  onDestroy(() => {
    requestSequence += 1;
    abortController?.abort();
  });
</script>

<section
  class="similar-nodes"
  aria-labelledby="similar-nodes-heading"
  aria-busy={requested && loading}
>
  <div class="similar-heading-row">
    <InfoHeading id="similar-nodes-heading" label="Ähnliche Knoten" level={4}>
      <p class="similar-explainer">
        Maschinell aus Inhalt und Schlagwörtern dieses Knotens berechnet. Das
        sind Vorschläge – keine Fäden, keine kuratierten Beziehungen und keine
        Aussage über gemeinsame Autorenschaft.
      </p>
      <p class="similar-explainer">
        Die Suche wird erst nach deinem ausdrücklichen Klick an den Server
        gesendet.
      </p>
    </InfoHeading>
    {#if !requested}
      <button
        class="similar-trigger"
        type="button"
        aria-label="Ähnliche Knoten suchen"
        disabled={!sourceId || !query}
        onclick={() => void load(query)}>Suchen</button
      >
    {/if}
  </div>
  {#if requested && loading}
    <p class="ghost" role="status" aria-live="polite">
      Ähnliche Knoten werden berechnet…
    </p>
  {:else if error}
    <p class="similar-error" role="status" aria-live="polite">{error}</p>
    <button
      class="similar-trigger"
      type="button"
      onclick={() => void load(query)}>Erneut versuchen</button
    >
  {:else}
    {#if mode === "lexical_fallback"}
      <p class="similar-note" role="status" aria-live="polite">
        Die semantische Ergänzung ist gerade nicht verfügbar; die Vorschläge
        stammen aus dem lexikalischen Serverpfad.
      </p>
    {/if}
    {#if similarNodes.length > 0}
      <p class="similar-result-status" role="status" aria-live="polite">
        {similarNodes.length}
        {similarNodes.length === 1 ? "Vorschlag" : "Vorschläge"}
      </p>
      <ul aria-label="Vorgeschlagene ähnliche Knoten">
        {#each similarNodes as similar}
          <li>
            <button
              type="button"
              onclick={() =>
                dispatch("select", {
                  type: "node",
                  id: similar.id,
                  title: similar.title,
                  data: nodeToMapEntity(similar) ?? undefined,
                })}
            >
              <span>{similar.title}</span>
              <small>{nodeKindLabel(similar.kind)}</small>
            </button>
          </li>
        {/each}
      </ul>
    {:else}
      <p class="ghost" role="status" aria-live="polite">
        Keine ähnlichen Knoten gefunden.
      </p>
    {/if}
  {/if}
</section>

<style>
  .similar-nodes {
    margin-top: 0.85rem;
    padding-top: 0.75rem;
    border-top: 1px solid var(--panel-border);
  }
  .similar-heading-row {
    display: flex;
    align-items: center;
  }
  .similar-nodes :global(.info-heading-title) {
    font-size: 1rem;
  }
  .similar-explainer,
  .similar-note,
  .similar-error,
  .similar-result-status {
    margin: 0.45rem 0 0.65rem;
    font-size: 0.82rem;
    line-height: 1.4;
    color: var(--muted);
  }
  .similar-result-status {
    margin-top: 0.65rem;
  }
  .similar-error {
    color: var(--text);
    border-left: 3px solid color-mix(in srgb, #a33 70%, var(--text));
    padding-left: 0.6rem;
  }
  ul {
    list-style: none;
    padding: 0;
    margin: 0;
    display: grid;
    gap: 0.5rem;
  }
  button {
    width: 100%;
    min-height: 44px;
    padding: 0.65rem 0.75rem;
    border: 1px solid var(--panel-border-strong);
    border-radius: 8px;
    background: var(--panel-solid);
    color: var(--text);
    text-align: left;
    cursor: pointer;
    display: grid;
    gap: 0.15rem;
  }
  button:hover,
  button:focus-visible {
    border-color: var(--accent);
  }
  button:disabled {
    cursor: not-allowed;
    opacity: 0.6;
  }
  .similar-trigger {
    width: auto;
    margin-left: auto;
    padding: 0.45rem 0.8rem;
    border-radius: 999px;
    font-weight: 650;
  }
  button span {
    font-weight: 650;
  }
  button small {
    color: var(--muted);
  }
</style>
