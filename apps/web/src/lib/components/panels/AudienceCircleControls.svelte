<script lang="ts">
  import { authStore } from "$lib/auth/store";
  import { invalidate } from "$app/navigation";
  import {
    acceptCircle,
    createHousehold,
    inviteToCircle,
    leaveCircle,
    listCircleMembers,
    listMyCircles,
    type AudienceCircle,
    type AudienceCircleMember,
    AudienceCircleError,
  } from "$lib/api/audienceCircles";

  interface Props {
    selectedAccountId: string;
    selectedName?: string;
  }
  let { selectedAccountId, selectedName = "dieser Garnrolle" }: Props =
    $props();

  let circles: AudienceCircle[] = $state([]);
  let loading = $state(false);
  let busy = $state(false);
  let error = $state("");
  let notice = $state("");
  let chosenCircle = $state("");
  let memberList: AudienceCircleMember[] = $state([]);
  let memberCircle = $state("");
  let memberLoading = $state(false);

  let ownView = $derived($authStore.account_id === selectedAccountId);
  let households = $derived(
    circles.filter((circle) => circle.kind === "household"),
  );
  let activeHouseholds = $derived(
    households.filter(
      (circle) =>
        circle.state === "active" && circle.membership_status === "active",
    ),
  );

  // Account changes cancel in-flight reads; a previous account's memberships
  // are never rendered under a new authentication identity.
  $effect(() => {
    const actor = $authStore.authenticated ? $authStore.account_id : undefined;
    const selected = selectedAccountId;
    circles = [];
    chosenCircle = "";
    memberList = [];
    memberCircle = "";
    memberLoading = false;
    notice = "";
    error = "";
    loading = Boolean(actor && selected);
    busy = false;
    if (!actor || !selected) return;

    const controller = new AbortController();
    void listMyCircles(controller.signal)
      .then((items) => {
        if (controller.signal.aborted) return;
        circles = items;
        chosenCircle =
          items.find(
            (circle) =>
              circle.kind === "household" &&
              circle.state === "active" &&
              circle.membership_status === "active",
          )?.id ?? "";
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        error =
          cause instanceof AudienceCircleError
            ? cause.message
            : "Hausgemeinschaft konnte nicht geladen werden.";
      })
      .finally(() => {
        if (!controller.signal.aborted) loading = false;
      });

    return () => controller.abort();
  });

  async function perform(
    action: () => Promise<unknown>,
    success: string,
    revokesReadAccess = false,
  ) {
    if (busy || !$authStore.authenticated || !$authStore.account_id) return;
    const actor = $authStore.account_id;
    const selected = selectedAccountId;
    busy = true;
    error = "";
    notice = "";
    try {
      await action();
      if ($authStore.account_id !== actor || selectedAccountId !== selected)
        return;
      // A revoked circle membership must discard already loaded private map,
      // search, panel, edge and activity caches (not only SvelteKit page data).
      // A full reload is reserved for this sensitive lifecycle transition.
      if (revokesReadAccess) {
        window.location.reload();
        return;
      }
      // New memberships may reveal additional canonical map nodes.
      void invalidate("weltgewebe:domain-data");
      const items = await listMyCircles();
      if ($authStore.account_id !== actor || selectedAccountId !== selected)
        return;
      circles = items;
      if (
        !items.some(
          (circle) => circle.id === chosenCircle && circle.state === "active",
        )
      ) {
        chosenCircle =
          items.find(
            (circle) =>
              circle.kind === "household" &&
              circle.state === "active" &&
              circle.membership_status === "active",
          )?.id ?? "";
      }
      memberCircle = "";
      memberList = [];
      notice = success;
    } catch (cause) {
      if ($authStore.account_id === actor && selectedAccountId === selected) {
        error =
          cause instanceof AudienceCircleError
            ? cause.message
            : "Die Aktion konnte nicht abgeschlossen werden.";
      }
    } finally {
      if ($authStore.account_id === actor && selectedAccountId === selected)
        busy = false;
    }
  }

  async function toggleMembers(id: string) {
    if (memberCircle === id) {
      memberCircle = "";
      memberList = [];
      return;
    }
    const actor = $authStore.account_id;
    memberCircle = "";
    memberList = [];
    memberLoading = true;
    error = "";
    try {
      const members = await listCircleMembers(id);
      if ($authStore.account_id !== actor) return;
      memberList = members;
      memberCircle = id;
    } catch (cause) {
      if ($authStore.account_id === actor)
        error =
          cause instanceof AudienceCircleError
            ? cause.message
            : "Mitglieder konnten nicht geladen werden.";
    } finally {
      if ($authStore.account_id === actor) memberLoading = false;
    }
  }
</script>

{#if $authStore.authenticated && selectedAccountId}
  <section
    class="household"
    data-testid="audience-circle-controls"
    aria-label="Hausgemeinschaft"
  >
    <h4>Hausgemeinschaft</h4>
    <p class="hint">
      Freiwillige Mitgliedschaft. Gleiche Kartenposition bedeutet noch keinen
      Beitritt.
    </p>
    {#if loading}
      <p class="hint" role="status">Hausgemeinschaften werden geladen…</p>
    {:else if ownView}
      {#if households.length === 0}
        <p class="hint">
          Noch keine Hausgemeinschaft. Öffne eine andere Garnrolle auf der
          Karte, um eine gemeinsame Hausgemeinschaft vorzuschlagen.
        </p>
      {:else}
        <ul class="circles">
          {#each households as circle (circle.id)}
            <li>
              <strong>{circle.name}</strong>
              {#if circle.membership_status === "invited"}
                <span class="hint">Einladung – noch keine Mitgliedsrechte</span>
                {#if circle.state !== "inactive"}
                  <button
                    type="button"
                    disabled={busy}
                    onclick={() =>
                      perform(
                        () => acceptCircle(circle.id),
                        "Beitritt bestätigt.",
                      )}>Beitreten</button
                  >
                {/if}
                <button
                  type="button"
                  disabled={busy}
                  onclick={() =>
                    perform(
                      () => leaveCircle(circle.id),
                      "Einladung abgelehnt.",
                    )}>Ablehnen</button
                >
              {:else}
                <span class="hint"
                  >{circle.state === "forming"
                    ? "Einladung versendet – warte auf Bestätigung"
                    : circle.state === "active"
                      ? "Du bist Mitglied"
                      : "Gemeinschaft nicht mehr aktiv"}</span
                >
                {#if circle.state === "active"}
                  <button
                    type="button"
                    disabled={busy || memberLoading}
                    onclick={() => toggleMembers(circle.id)}
                  >
                    {memberCircle === circle.id
                      ? "Mitglieder ausblenden"
                      : "Mitglieder anzeigen"}
                  </button>
                {/if}
                <button
                  type="button"
                  disabled={busy}
                  onclick={() =>
                    perform(
                      () => leaveCircle(circle.id),
                      "Du bist ausgetreten.",
                      true,
                    )}>Verlassen</button
                >
                {#if memberCircle === circle.id}
                  <ul class="members">
                    {#each memberList as member (member.account_id)}
                      <li>{member.title}</li>
                    {/each}
                  </ul>
                {/if}
              {/if}
            </li>
          {/each}
        </ul>
      {/if}
    {:else}
      {#if activeHouseholds.length > 0}
        <label for="household-choice"
          >In eine bestehende Hausgemeinschaft einladen</label
        >
        <select id="household-choice" bind:value={chosenCircle} disabled={busy}>
          {#each activeHouseholds as circle (circle.id)}
            <option value={circle.id}>{circle.name}</option>
          {/each}
        </select>
        <button
          type="button"
          disabled={busy || !chosenCircle}
          onclick={() =>
            perform(
              () => inviteToCircle(chosenCircle, selectedAccountId),
              "Einladung verschickt. Mitgliedschaft entsteht erst durch Annahme.",
            )}>Einladen</button
        >
      {/if}
      <button
        type="button"
        disabled={busy}
        onclick={() =>
          perform(
            () => createHousehold(selectedAccountId),
            "Hausgemeinschaft vorgeschlagen. Sie wird erst durch Annahme aktiv.",
          )}>Neue Hausgemeinschaft mit {selectedName} vorschlagen</button
      >
    {/if}
    {#if notice}<p role="status" class="notice">{notice}</p>{/if}
    {#if error}<p role="alert" class="error">{error}</p>{/if}
  </section>
{/if}

<style>
  .household {
    margin: 1rem 0 0;
    padding: 0.85rem;
    border: 1px solid var(--panel-border-strong);
    border-radius: 12px;
  }
  h4 {
    margin: 0 0 0.5rem;
    font-size: 1.05rem;
  }
  .hint {
    color: var(--muted);
    font-size: 0.85rem;
    margin: 0.35rem 0;
  }
  label {
    display: block;
    margin: 0.6rem 0 0.35rem;
    font-size: 0.9rem;
  }
  button,
  select {
    color: var(--text);
    background: var(--panel-solid);
    border: 1px solid var(--panel-border-strong);
    border-radius: 8px;
    padding: 0.55rem 0.65rem;
    min-height: 44px;
    max-width: 100%;
    margin: 0.35rem 0.35rem 0 0;
  }
  button {
    cursor: pointer;
    text-align: left;
  }
  button:disabled {
    opacity: 0.55;
    cursor: default;
  }
  button:not(:disabled):hover,
  button:focus-visible,
  select:focus-visible {
    border-color: var(--accent);
  }
  .circles,
  .members {
    list-style: none;
    padding: 0;
    margin: 0.5rem 0;
  }
  .circles > li {
    border-top: 1px solid var(--panel-border-strong);
    padding: 0.6rem 0;
  }
  .circles strong,
  .circles span {
    display: block;
  }
  .members {
    padding: 0.3rem 0.6rem;
  }
  .members li {
    padding: 0.15rem 0;
  }
  .notice {
    color: var(--text);
  }
  .error {
    color: var(--error, #a02020);
  }
</style>
