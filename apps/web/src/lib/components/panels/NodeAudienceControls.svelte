<script lang="ts">
  import { authStore } from "$lib/auth/store";
  import { listMyCircles, type AudienceCircle } from "$lib/api/audienceCircles";
  import {
    getNodeAudience,
    setNodeAudience,
    NodeAudienceError,
    type NodeAudienceScope,
  } from "$lib/api/nodeAudience";

  interface Props {
    nodeId: string;
    onChanged: () => void;
  }
  let { nodeId, onChanged }: Props = $props();
  let scope: NodeAudienceScope | "unavailable" = $state("private");
  let circleIds: string[] = $state([]);
  let originalScope: NodeAudienceScope | "unavailable" = $state("unavailable");
  let originalCircleIds: string[] = $state([]);
  let circles: AudienceCircle[] = $state([]);
  let loading = $state(true);
  let loaded = $state(false);
  let saving = $state(false);
  let error = $state("");
  let notice = $state("");
  let activeCircles = $derived(
    circles.filter(
      (circle) =>
        circle.state === "active" && circle.membership_status === "active",
    ),
  );
  // Existing grants remain part of the owner's CAS snapshot after leaving a
  // circle, even though that circle is no longer selectable for new grants.
  let unavailableCircleIds = $derived(
    circleIds.filter((id) => !activeCircles.some((circle) => circle.id === id)),
  );
  let canSave = $derived.by(() => {
    // Evaluate the union inside the reactive callback: the initial
    // "private" value must not be mistaken for a permanently fixed scope.
    const selected: NodeAudienceScope | "unavailable" = scope;
    return (
      !loading &&
      loaded &&
      !saving &&
      selected !== "unavailable" &&
      (selected !== "circles" || circleIds.length > 0)
    );
  });

  // A change of authentication identity or selected node must discard private
  // membership data and abort the preceding actor's pending requests.
  $effect(() => {
    const actor = $authStore.authenticated ? $authStore.account_id : null;
    const id = nodeId;
    scope = "private";
    circleIds = [];
    originalScope = "unavailable";
    originalCircleIds = [];
    circles = [];
    error = "";
    notice = "";
    saving = false;
    loaded = false;
    loading = Boolean(actor && id);
    if (!actor || !id) return;
    const controller = new AbortController();
    void Promise.all([
      getNodeAudience(id, controller.signal),
      listMyCircles(controller.signal),
    ])
      .then(([audience, memberships]) => {
        if (controller.signal.aborted) return;
        scope = audience.scope;
        circleIds = audience.circle_ids;
        originalScope = audience.scope;
        originalCircleIds = audience.circle_ids;
        circles = memberships;
        loaded = true;
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        loaded = false;
        error =
          cause instanceof NodeAudienceError
            ? cause.message
            : "Die Sichtbarkeit konnte nicht geladen werden.";
      })
      .finally(() => {
        if (!controller.signal.aborted) loading = false;
      });
    return () => controller.abort();
  });

  function toggleCircle(id: string, checked: boolean) {
    circleIds = checked
      ? [...circleIds, id]
      : circleIds.filter((value) => value !== id);
    // Removing the last grant is equivalent to selecting owner-only privacy.
    // Keep originalCircleIds unchanged for the backend's stale-state CAS.
    if (scope === "circles" && circleIds.length === 0) scope = "private";
    notice = "";
  }

  async function save() {
    if (!canSave || scope === "unavailable") return;
    const actor = $authStore.account_id;
    const id = nodeId;
    const chosen = scope === "circles" ? circleIds : [];
    saving = true;
    error = "";
    notice = "";
    try {
      const audience = await setNodeAudience(id, scope, chosen, {
        scope: originalScope,
        circle_ids: originalCircleIds,
      });
      if ($authStore.account_id !== actor || nodeId !== id) return;
      scope = audience.scope;
      circleIds = audience.circle_ids;
      originalScope = audience.scope;
      originalCircleIds = audience.circle_ids;
      notice = "Sichtbarkeit gespeichert.";
      onChanged();
    } catch (cause: unknown) {
      if ($authStore.account_id !== actor || nodeId !== id) return;
      error =
        cause instanceof NodeAudienceError
          ? cause.message
          : "Die Sichtbarkeit konnte nicht gespeichert werden.";
    } finally {
      if ($authStore.account_id === actor && nodeId === id) saving = false;
    }
  }
</script>

<section
  class="node-audience"
  data-testid="node-audience-controls"
  aria-label="Knoten-Sichtbarkeit"
>
  <h4>Sichtbarkeit und Geltungsbereich</h4>
  <p class="hint">
    Bestimme, wer diesen vorhandenen Knoten lesen darf. Das verändert nicht die
    Karte oder die Schreibrechte.
  </p>
  {#if loading}
    <p role="status">Sichtbarkeit wird geladen…</p>
  {:else if scope === "unavailable"}
    <p class="hint">
      Ein verborgener oder widerrufener Knoten kann hier nicht freigegeben
      werden.
    </p>
  {:else}
    <label for="node-audience-scope">Wer darf diesen Knoten lesen?</label>
    <select id="node-audience-scope" bind:value={scope} disabled={saving}>
      <option value="public">Öffentlich</option>
      <option value="private">Nur ich</option>
      <option value="circles">Nur ausgewählte Gemeinschaften</option>
    </select>
    {#if scope === "circles"}
      <fieldset disabled={saving}>
        <legend>Gemeinschaften auswählen</legend>
        {#each activeCircles as circle (circle.id)}
          <label class="circle-option">
            <input
              type="checkbox"
              checked={circleIds.includes(circle.id)}
              disabled={!circleIds.includes(circle.id) && circleIds.length >= 8}
              onchange={(event) =>
                toggleCircle(circle.id, event.currentTarget.checked)}
            />
            {circle.name}
          </label>
        {:else}
          {#if unavailableCircleIds.length === 0}
            <p class="hint">
              Zum Freigeben musst du einer bestätigten Gemeinschaft angehören.
            </p>
          {/if}
        {/each}
        {#if unavailableCircleIds.length > 0}
          <p class="hint">
            Alte Freigaben an nicht mehr verfügbare Gemeinschaften kannst du
            hier entfernen.
          </p>
        {/if}
        {#each unavailableCircleIds as id (id)}
          <label class="circle-option">
            <input
              type="checkbox"
              checked={circleIds.includes(id)}
              onchange={(event) => toggleCircle(id, event.currentTarget.checked)}
            />
            Frühere Gemeinschaft ({id}) – Freigabe entfernen
          </label>
        {/each}
      </fieldset>
    {/if}
    <button type="button" disabled={!canSave} onclick={save}>
      {saving ? "Speichern…" : "Sichtbarkeit speichern"}
    </button>
  {/if}
  {#if notice}<p role="status">{notice}</p>{/if}
  {#if error}<p role="alert" class="error">{error}</p>{/if}
</section>

<style>
  .node-audience {
    border: 1px solid var(--panel-border-strong);
    border-radius: 12px;
    padding: 0.75rem;
    margin-top: 1rem;
  }
  h4 {
    margin: 0 0 0.4rem;
  }
  .hint {
    color: var(--muted);
    font-size: 0.85rem;
  }
  label,
  legend {
    display: block;
    margin: 0.6rem 0 0.25rem;
  }
  select,
  button {
    color: var(--text);
    background: var(--panel-solid);
    border: 1px solid var(--panel-border-strong);
    border-radius: 8px;
    padding: 0.6rem;
    min-height: 44px;
    max-width: 100%;
  }
  fieldset {
    border: 0;
    padding: 0;
    margin: 0.6rem 0;
  }
  .circle-option {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    min-height: 44px;
  }
  .circle-option input {
    width: 1.1rem;
    height: 1.1rem;
  }
  button:disabled {
    opacity: 0.55;
  }
  .error {
    color: var(--error, #a02020);
  }
</style>
