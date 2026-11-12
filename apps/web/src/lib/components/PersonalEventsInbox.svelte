<script lang="ts">
  import { onMount } from "svelte";
  import { invalidate } from "$app/navigation";
  import { authStore } from "$lib/auth/store";
  import { refreshAccountAttention } from "$lib/accountAttentionRuntime";
  import {
    listPersonalEvents,
    markPersonalEventRead,
    type PersonalEvent,
    type PersonalEventPage,
  } from "$lib/api/personalEvents";
  import {
    acceptCircle,
    declineCircle,
    withdrawCircleInvitation,
  } from "$lib/api/audienceCircles";

  const emptyPage = (): PersonalEventPage => ({
    items: [],
    unread_count: 0,
    has_more: false,
  });
  let notices = $state<PersonalEventPage>(emptyPage());
  let loading = $state(true);
  let olderLoading = $state(false);
  let busyId = $state<string | null>(null);
  let error = $state("");
  let revision = 0;

  function formatDate(value: string): string {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return new Intl.DateTimeFormat("de-DE", {
      dateStyle: "short",
      timeStyle: "short",
    }).format(date);
  }

  async function refresh(): Promise<void> {
    const actor = $authStore.account_id;
    if (!$authStore.authenticated || !actor) {
      notices = emptyPage();
      loading = false;
      return;
    }
    const current = ++revision;
    loading = true;
    try {
      const page = await listPersonalEvents();
      if (current !== revision || $authStore.account_id !== actor) return;
      // A periodic or focus refresh only fetches the newest page. Retain
      // previously paginated history and its cursor state: replacing the
      // whole collection would make older messages disappear every 30 s.
      const alreadyLoaded = notices.items;
      const refreshedIds = new Set(page.items.map((item) => item.id));
      notices = {
        ...page,
        items: [
          ...page.items,
          ...alreadyLoaded.filter((item) => !refreshedIds.has(item.id)),
        ],
        has_more: alreadyLoaded.length > 0 ? notices.has_more : page.has_more,
      };
      error = "";
    } catch {
      if (current === revision && $authStore.account_id === actor) {
        error = "Persönliche Nachrichten konnten nicht geladen werden.";
      }
    } finally {
      if (current === revision && $authStore.account_id === actor) {
        loading = false;
      }
    }
  }

  async function loadOlder(): Promise<void> {
    if (!notices.has_more || olderLoading || notices.items.length === 0) return;
    const actor = $authStore.account_id;
    const last = notices.items.at(-1);
    if (!last) return;
    olderLoading = true;
    try {
      const older = await listPersonalEvents(undefined, last.id);
      if ($authStore.account_id !== actor) return;
      const known = new Set(notices.items.map((item) => item.id));
      notices = {
        items: [
          ...notices.items,
          ...older.items.filter((item) => !known.has(item.id)),
        ],
        unread_count: older.unread_count,
        has_more: older.has_more,
      };
    } catch {
      error = "Ältere Nachrichten konnten nicht geladen werden.";
    } finally {
      olderLoading = false;
    }
  }

  async function markRead(event: PersonalEvent): Promise<void> {
    if (event.read_at !== null || busyId) return;
    const actor = $authStore.account_id;
    busyId = event.id;
    try {
      await markPersonalEventRead(event.id);
      if ($authStore.account_id !== actor) return;
      notices = {
        ...notices,
        unread_count: Math.max(0, notices.unread_count - 1),
        items: notices.items.map((item) =>
          item.id === event.id
            ? { ...item, read_at: new Date().toISOString() }
            : item,
        ),
      };
      void refreshAccountAttention();
    } catch {
      error = "Die Lesebestätigung konnte nicht gespeichert werden.";
    } finally {
      busyId = null;
    }
  }

  async function respond(
    event: PersonalEvent,
    action: "accept" | "decline" | "withdraw",
  ): Promise<void> {
    if (
      busyId ||
      !event.actionable ||
      !event.circle_id ||
      !event.invitation_created_at
    )
      return;
    const actor = $authStore.account_id;
    busyId = event.id;
    error = "";
    try {
      if (action === "accept") {
        await acceptCircle(event.circle_id, event.invitation_created_at);
      } else if (action === "decline") {
        await declineCircle(event.circle_id, event.invitation_created_at);
      } else {
        if (!event.target_account_id) return;
        await withdrawCircleInvitation(
          event.circle_id,
          event.target_account_id,
          event.invitation_created_at,
        );
      }
      if ($authStore.account_id !== actor) return;

      // The server is the membership truth. Never keep an invitation
      // actionable because of a stale client-side notification snapshot.
      // The membership transition has already committed. A failed read
      // receipt must not claim the membership operation itself failed.
      let ackFailed = false;
      try {
        await markPersonalEventRead(event.id);
      } catch {
        ackFailed = true;
      }
      await refresh();
      if (ackFailed) {
        error =
          "Der Vorgang wurde übernommen; nur die Lesebestätigung ist fehlgeschlagen.";
      }
      void refreshAccountAttention();
      // Declining a pending invitation revokes no private read rights: an
      // invited account was never an active member. Keep the page visible so
      // a failed read receipt remains actionable instead of disappearing in
      // an immediate reload. Active membership departure still reloads.
      void invalidate("weltgewebe:domain-data");
    } catch {
      if ($authStore.account_id === actor) {
        // Refresh may clear its own loading error; set the action outcome
        // afterwards so a stale 409 remains visible to the user.
        await refresh();
        if ($authStore.account_id === actor) {
          error =
            "Der Vorgang konnte nicht abgeschlossen werden. Prüfe den aktuellen Status.";
        }
      }
    } finally {
      if ($authStore.account_id === actor) busyId = null;
    }
  }

  // When an account changes, never render the previous account's private
  // messages until the new server response is authorized.
  $effect(() => {
    const accountId = $authStore.account_id;
    const authenticated = $authStore.authenticated;
    revision += 1;
    notices = emptyPage();
    if (authenticated && accountId) void refresh();
  });

  onMount(() => {
    const refreshOnFocus = () => {
      if (document.visibilityState === "visible") void refresh();
    };
    const poll = window.setInterval(refreshOnFocus, 30_000);
    window.addEventListener("focus", refreshOnFocus);
    return () => {
      window.clearInterval(poll);
      window.removeEventListener("focus", refreshOnFocus);
      revision += 1;
    };
  });
</script>

<section
  id="ereignisse"
  class="personal-events"
  aria-labelledby="personal-event-title"
  data-testid="personal-events-inbox"
>
  <div class="personal-events-heading">
    <h3 id="personal-event-title">Persönliche Mitteilungen</h3>
    {#if notices.unread_count > 0}
      <span
        class="personal-event-count"
        aria-label={`${notices.unread_count} ungelesene persönliche Mitteilungen`}
        >{notices.unread_count}</span
      >
    {/if}
    <button type="button" class="quiet" onclick={refresh} disabled={loading}>
      Aktualisieren
    </button>
  </div>
  {#if error}<p role="alert" class="personal-error">{error}</p>{/if}
  {#if loading && notices.items.length === 0}
    <p class="muted">Persönliche Mitteilungen werden geladen…</p>
  {:else if notices.items.length === 0}
    <p class="muted">Keine persönlichen Mitteilungen.</p>
  {:else}
    <ul class="personal-event-list">
      {#each notices.items as event (event.id)}
        <li class:unread={event.read_at === null}>
          <div class="personal-event-body">
            <strong>{event.circle_name ?? "Gemeinschaft"}</strong>
            <p>{event.content}</p>
            <time datetime={event.created_at}
              >{formatDate(event.created_at)}</time
            >
          </div>
          {#if event.actionable && event.circle_id && event.invitation_created_at}
            <div class="personal-event-actions">
              {#if event.event_kind === "circle_invited"}
                <button
                  type="button"
                  disabled={busyId !== null}
                  onclick={() => respond(event, "accept")}>Annehmen</button
                >
                <button
                  type="button"
                  disabled={busyId !== null}
                  onclick={() => respond(event, "decline")}>Ablehnen</button
                >
              {:else if event.event_kind === "circle_invitation_sent" && event.target_account_id}
                <button
                  type="button"
                  disabled={busyId !== null}
                  onclick={() => respond(event, "withdraw")}
                  >Einladung zurückziehen</button
                >
              {/if}
            </div>
          {:else if event.event_kind === "circle_invited" || event.event_kind === "circle_invitation_sent"}
            <span class="muted">Einladung nicht mehr offen</span>
          {/if}
          {#if event.read_at === null}
            <button
              type="button"
              class="personal-read"
              disabled={busyId !== null}
              onclick={() => markRead(event)}>Als gelesen markieren</button
            >
          {/if}
        </li>
      {/each}
    </ul>
    {#if notices.has_more}
      <button
        type="button"
        class="personal-more"
        disabled={olderLoading}
        onclick={loadOlder}
        >{olderLoading ? "Lade…" : "Ältere Mitteilungen laden"}</button
      >
    {/if}
  {/if}
</section>

<style>
  .personal-events {
    border-bottom: 1px solid var(--panel-border-strong);
    padding: 0.55rem 0 0.85rem;
  }
  .personal-events-heading {
    display: flex;
    flex-wrap: wrap;
    gap: 0.45rem;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 0.6rem;
  }
  .personal-events-heading h3 {
    font-size: 0.95rem;
    margin: 0;
  }
  .personal-event-count {
    display: inline-block;
    border-radius: 999px;
    background: var(--accent);
    color: var(--accent-contrast, #fff);
    padding: 0.15rem 0.45rem;
    font-size: 0.76rem;
  }
  .personal-event-list {
    padding: 0;
    margin: 0;
    list-style: none;
    display: grid;
    gap: 0.4rem;
  }
  .personal-event-list li {
    border: 1px solid var(--panel-border-strong);
    border-radius: 0.7rem;
    padding: 0.65rem;
  }
  .personal-event-list li.unread {
    border-color: var(--accent);
  }
  .personal-event-body p {
    margin: 0.3rem 0;
    overflow-wrap: anywhere;
  }
  .personal-event-body time {
    color: var(--text-muted, var(--text));
    font-size: 0.75rem;
  }
  .personal-event-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    margin-top: 0.6rem;
  }
  .personal-event-actions button,
  .personal-read,
  .personal-more {
    min-height: 38px;
    border: 1px solid var(--panel-border-strong);
    border-radius: 0.55rem;
    padding: 0.45rem 0.7rem;
    background: var(--panel);
    color: var(--text);
    cursor: pointer;
    font: inherit;
  }
  .personal-read {
    margin-top: 0.45rem;
    font-size: 0.78rem;
  }
  .personal-more {
    margin-top: 0.6rem;
  }
  .personal-error {
    color: var(--danger, var(--text));
  }
</style>
