<script lang="ts">
  import { onMount } from "svelte";
  import { searchNodes } from "$lib/api/search";
  import type { Node } from "$lib/map/types";

  let query = $state("");
  let nodes: Node[] = $state([]);
  let cursor: string | null = $state(null);
  let loading = $state(false);
  let error = $state("");
  let searching = $state(false);
  let controller: AbortController | undefined;

  async function load(more = false) {
    controller?.abort();
    const request = new AbortController();
    controller = request;
    loading = true;
    error = "";
    const term = query.trim();
    searching = Boolean(term);
    try {
      let items: Node[];
      let nextCursor: string | null = null;
      if (term) {
        const result = await searchNodes(term, {
          limit: 50,
          signal: request.signal,
        });
        items = result.items;
      } else {
        const params = new URLSearchParams({
          pagination: "cursor",
          limit: "50",
        });
        if (more && cursor) params.set("cursor", cursor);
        const base = import.meta.env.PUBLIC_GEWEBE_API_BASE ?? "";
        const response = await fetch(`${base}/api/nodes?${params}`, {
          signal: request.signal,
        });
        if (!response.ok)
          throw new Error("Knoten konnten nicht geladen werden.");
        const result = await response.json();
        items = Array.isArray(result) ? result : result.items;
        nextCursor = Array.isArray(result) ? null : result.page.next_cursor;
      }
      if (request.signal.aborted) return;
      nodes = more ? [...nodes, ...items] : items;
      cursor = nextCursor;
    } catch {
      if (!request.signal.aborted)
        error = "Knoten konnten nicht geladen werden. Bitte erneut versuchen.";
    } finally {
      if (controller === request) loading = false;
    }
  }

  onMount(() => {
    void load();
    return () => controller?.abort();
  });
</script>

<svelte:head><title>Knoten · commonThing</title></svelte:head>
<main>
  <h1>Knoten im Gewebe</h1>
  <p>Orte, Wissen, digitale Commons und Vorhaben – mit oder ohne Kartenort.</p>
  <nav aria-label="Knotenansichten">
    <a href="/map">Karte öffnen</a>
    <a href="/map?compose=node">Knoten knüpfen</a>
  </nav>
  <form
    onsubmit={(event) => {
      event.preventDefault();
      void load();
    }}
  >
    <label for="commons-query">Knoten suchen</label>
    <input
      id="commons-query"
      type="search"
      maxlength="512"
      bind:value={query}
    />
    <button type="submit" disabled={loading}>Suchen</button>
  </form>
  {#if loading}<p role="status">Knoten werden geladen…</p>{/if}
  {#if error}
    <p role="alert">{error}</p>
    <button onclick={() => void load()}>Erneut versuchen</button>
  {:else}
    <ul aria-label="Knoten">
      {#each nodes as node (node.id)}
        <li>
          <a href={`/map?focus=node:${encodeURIComponent(node.id)}`}
            >{node.title}</a
          >
          <p>
            {node.summary ?? node.kind} · {node.location
              ? "Mit Kartenort"
              : "Ohne Kartenort"}
          </p>
        </li>
      {/each}
    </ul>
    {#if !loading && nodes.length === 0}<p>Keine Knoten gefunden.</p>{/if}
    {#if cursor && !searching && !query.trim()}<button
        disabled={loading}
        onclick={() => void load(true)}>Weitere Knoten</button
      >{/if}
  {/if}
</main>

<style>
  main {
    max-width: 48rem;
    margin: 2rem auto;
    padding: 1rem;
  }
  nav,
  form {
    display: flex;
    flex-wrap: wrap;
    gap: 1rem;
    margin: 1rem 0;
    align-items: center;
  }
  input {
    flex: 1;
    min-width: 10rem;
  }
  li {
    padding: 0.75rem 0;
  }
  li p {
    margin: 0.25rem 0;
  }
</style>
