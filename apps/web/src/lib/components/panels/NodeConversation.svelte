<script lang="ts">
  import { onDestroy, onMount, tick } from "svelte";
  import { authStore } from "$lib/auth/store";
  import { formatDate } from "$lib/utils/formatDate";
  import {
    compareApiTimestamps,
    createConversationMessage,
    getConversation,
    getNodeConversation,
    listConversationMessages,
    NodeConversationApiError,
    tombstoneConversationMessage,
    updateConversationMessage,
    type ConversationMessage,
    type NodeConversation,
    type PublicConversation,
  } from "$lib/api/nodeConversation";

  interface Props {
    nodeId?: string;
    conversationId?: string | null;
    heading?: string;
    emptyMessage?: string;
    testId?: string;
  }

  let {
    nodeId = "",
    conversationId = null,
    heading = "Öffentlicher Gesprächsraum",
    emptyMessage = "Noch keine Beiträge. Hier kann das Gespräch über diesen Knoten beginnen.",
    testId = "node-conversation",
  }: Props = $props();

  const POLL_INTERVAL_MS = 5_000;

  let conversation: NodeConversation | PublicConversation | null = null;
  let messages: ConversationMessage[] = $state([]);
  let olderCursor: string | null = $state(null);
  let loading = $state(true);
  let loadingOlder = $state(false);
  let loadedOlder = false;
  let loadError = $state("");
  let draft = $state("");
  let draftOperationId = "";
  let lastSubmittedDraft = "";
  let posting = $state(false);
  let mutationError = $state("");
  let editingId = $state("");
  let editDraft = $state("");
  let savingId = $state("");
  let tombstoningId = $state("");
  let removalCandidate: ConversationMessage | null = $state(null);
  let removalDialog: HTMLDialogElement;
  let removalSourceButton: HTMLButtonElement | null = null;
  let pollTimer: ReturnType<typeof setTimeout> | null = null;
  let pollGeneration = 0;
  let mutationGeneration = 0;
  let refreshController: AbortController | null = null;
  let destroyed = false;
  let mounted = $state(false);
  let loadedTarget = $state("");

  function targetKey(): string {
    return conversationId ? `conversation:${conversationId}` : `node:${nodeId}`;
  }

  function mergeMessages(incoming: ConversationMessage[]) {
    const merged = new Map(messages.map((message) => [message.id, message]));
    for (const message of incoming) {
      const current = merged.get(message.id);
      if (
        !current ||
        compareApiTimestamps(message.updated_at, current.updated_at) >= 0
      ) {
        merged.set(message.id, message);
      }
    }
    messages = Array.from(merged.values()).sort(
      (left, right) =>
        compareApiTimestamps(left.created_at, right.created_at) ||
        left.id.localeCompare(right.id),
    );
  }

  function mergeMutationMessages(incoming: ConversationMessage[]) {
    mutationGeneration += 1;
    mergeMessages(incoming);
  }

  function messageForError(error: unknown): string {
    if (error instanceof NodeConversationApiError) {
      if (error.status === 401 || error.status === 403) {
        return "Du darfst diesen Beitrag derzeit nicht verändern.";
      }
      if (error.code === "message_version_conflict") {
        return "Der Beitrag wurde inzwischen geändert. Dein Entwurf bleibt erhalten.";
      }
      if (error.code === "message_deleted") {
        return "Der Beitrag wurde bereits entfernt.";
      }
      if (error.code === "invalid_message_content") {
        return "Der Beitrag muss zwischen 1 und 4.000 Zeichen enthalten.";
      }
      if (error.code === "message_rate_limited") {
        return "Zu viele neue Beiträge in kurzer Zeit. Bitte versuche es in einer Minute erneut.";
      }
      if (error.code === "idempotency_key_conflict") {
        return "Dieser Sendeversuch gehört zu einem anderen Entwurf. Bitte sende den aktuellen Text erneut.";
      }
    }
    return "Das Gespräch konnte nicht gespeichert werden. Dein Entwurf bleibt erhalten.";
  }

  async function refresh(generation: number) {
    refreshController?.abort();
    const controller = new AbortController();
    refreshController = controller;
    const requestedTarget = targetKey();
    const startedMutationGeneration = mutationGeneration;
    if (loadedTarget !== requestedTarget) {
      conversation = null;
      messages = [];
      olderCursor = null;
      loadedOlder = false;
      loadError = "";
      loadedTarget = requestedTarget;
    }
    try {
      const currentConversation =
        conversation ??
        (conversationId
          ? await getConversation(conversationId, controller.signal)
          : await getNodeConversation(nodeId, controller.signal));
      if (
        destroyed ||
        controller.signal.aborted ||
        generation !== pollGeneration ||
        targetKey() !== requestedTarget
      )
        return;
      conversation = currentConversation;
      const page = await listConversationMessages(
        currentConversation.id,
        null,
        controller.signal,
      );
      if (
        destroyed ||
        controller.signal.aborted ||
        generation !== pollGeneration ||
        targetKey() !== requestedTarget ||
        mutationGeneration !== startedMutationGeneration
      )
        return;
      mergeMessages(page.items);
      if (!loadedOlder) olderCursor = page.page.next_cursor;
      loadError = "";
    } catch (error) {
      if (
        !destroyed &&
        !controller.signal.aborted &&
        refreshController === controller &&
        generation === pollGeneration &&
        targetKey() === requestedTarget &&
        (error as { name?: string } | null)?.name !== "AbortError"
      ) {
        loadError = "Das Gespräch kann gerade nicht geladen werden.";
        console.error(error);
      }
    } finally {
      const isCurrent =
        refreshController === controller &&
        generation === pollGeneration &&
        targetKey() === requestedTarget;
      if (refreshController === controller) refreshController = null;
      if (!destroyed && isCurrent) loading = false;
    }
  }

  async function loadOlder() {
    if (!conversation || !olderCursor || loadingOlder) return;
    const requestedConversationId = conversation.id;
    const startedMutationGeneration = mutationGeneration;
    loadingOlder = true;
    try {
      const page = await listConversationMessages(conversation.id, olderCursor);
      if (
        destroyed ||
        conversation?.id !== requestedConversationId ||
        mutationGeneration !== startedMutationGeneration
      )
        return;
      mergeMessages(page.items);
      olderCursor = page.page.next_cursor;
      loadedOlder = true;
      loadError = "";
    } catch (error) {
      loadError = "Ältere Beiträge können gerade nicht geladen werden.";
      console.error(error);
    } finally {
      loadingOlder = false;
    }
  }

  async function submitMessage() {
    if (!conversation || !draft.trim() || posting) return;
    const submittedDraft = draft.trim();
    posting = true;
    mutationError = "";
    if (!draftOperationId || submittedDraft !== lastSubmittedDraft) {
      draftOperationId = crypto.randomUUID();
      lastSubmittedDraft = submittedDraft;
    }
    try {
      const created = await createConversationMessage(
        conversation.id,
        submittedDraft,
        draftOperationId,
      );
      mergeMutationMessages([created]);
      draft = "";
      draftOperationId = "";
      lastSubmittedDraft = "";
    } catch (error) {
      mutationError = messageForError(error);
    } finally {
      posting = false;
    }
  }

  function beginEdit(message: ConversationMessage) {
    editingId = message.id;
    editDraft = message.content ?? "";
    mutationError = "";
  }

  async function saveEdit(message: ConversationMessage) {
    if (!conversation || !editDraft.trim() || savingId) return;
    savingId = message.id;
    mutationError = "";
    try {
      const updated = await updateConversationMessage(
        conversation.id,
        message.id,
        editDraft.trim(),
        message.updated_at,
      );
      mergeMutationMessages([updated]);
      editingId = "";
      editDraft = "";
    } catch (error) {
      if (error instanceof NodeConversationApiError && error.current) {
        mergeMutationMessages([error.current]);
      }
      mutationError = messageForError(error);
    } finally {
      savingId = "";
    }
  }

  async function requestRemoval(
    message: ConversationMessage,
    event: MouseEvent,
  ) {
    if (tombstoningId) return;
    removalSourceButton =
      event.currentTarget instanceof HTMLButtonElement
        ? event.currentTarget
        : null;
    removalCandidate = message;
    await tick();
    removalDialog.showModal();
    removalDialog.querySelector<HTMLButtonElement>(".secondary")?.focus();
  }

  function removalClosed() {
    removalCandidate = null;
    if (removalSourceButton?.isConnected) removalSourceButton.focus();
    removalSourceButton = null;
  }

  async function confirmRemoval() {
    const message = removalCandidate;
    if (!message || tombstoningId) return;
    removalDialog.close();
    await tombstone(message);
  }

  async function tombstone(message: ConversationMessage) {
    if (!conversation || tombstoningId) return;
    tombstoningId = message.id;
    mutationError = "";
    try {
      const removed = await tombstoneConversationMessage(
        conversation.id,
        message.id,
        message.updated_at,
      );
      mergeMutationMessages([removed]);
      if (editingId === message.id) {
        editingId = "";
        editDraft = "";
      }
    } catch (error) {
      if (error instanceof NodeConversationApiError && error.current) {
        mergeMutationMessages([error.current]);
      }
      mutationError = messageForError(error);
    } finally {
      tombstoningId = "";
    }
  }

  function stopPolling() {
    pollGeneration += 1;
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = null;
    refreshController?.abort();
    refreshController = null;
  }

  async function poll(generation: number) {
    await refresh(generation);
    if (
      destroyed ||
      generation !== pollGeneration ||
      document.visibilityState !== "visible"
    )
      return;
    pollTimer = setTimeout(() => void poll(generation), POLL_INTERVAL_MS);
  }

  function syncPolling() {
    stopPolling();
    if (document.visibilityState !== "visible") return;
    const generation = pollGeneration;
    void poll(generation);
  }

  onMount(() => {
    mounted = true;
    document.addEventListener("visibilitychange", syncPolling);
    syncPolling();
  });

  onDestroy(() => {
    mounted = false;
    destroyed = true;
    stopPolling();
    document.removeEventListener("visibilitychange", syncPolling);
  });
  let canWrite = $derived($authStore.authenticated);
  let requestedTarget = $derived(targetKey());
  $effect.pre(() => {
    if (mounted && requestedTarget !== loadedTarget) {
      loading = true;
      syncPolling();
    }
  });
</script>

<section
  class="conversation"
  aria-labelledby="conversation-room-heading"
  data-testid={testId}
>
  <h4 id="conversation-room-heading">{heading}</h4>

  {#if loadError}<p class="error" role="alert">{loadError}</p>{/if}
  {#if loading}<p class="muted">Lade Gespräch…</p>
  {:else if messages.length === 0}<p class="empty">
      {emptyMessage}
    </p>
  {:else}
    {#if olderCursor}<button
        type="button"
        class="older"
        onclick={loadOlder}
        disabled={loadingOlder}
        >{loadingOlder ? "Lädt…" : "Ältere Beiträge laden"}</button
      >{/if}
    <ol
      class="messages"
      role="log"
      aria-live="polite"
      aria-relevant="additions text"
      aria-label="Gesprächsbeiträge"
    >
      {#each messages as message (message.id)}<li
          class:deleted={message.deleted_at !== null}
        >
          <header>
            <strong>{message.author_title}</strong>
            <time datetime={message.created_at}
              >{formatDate(message.created_at)}</time
            >
          </header>
          {#if message.deleted_at}<p class="tombstone">Beitrag entfernt.</p>
          {:else if editingId === message.id}
            <form
              onsubmit={(event) => {
                event.preventDefault();
                void saveEdit(message);
              }}
            >
              <label for={`edit-message-${message.id}`}
                >Beitrag bearbeiten</label
              >
              <textarea
                id={`edit-message-${message.id}`}
                bind:value={editDraft}
                maxlength="4000"
                rows="4"
                required></textarea>
              <div class="actions">
                <button
                  type="button"
                  class="secondary"
                  onclick={() => (editingId = "")}>Abbrechen</button
                >
                <button type="submit" disabled={savingId === message.id}
                  >{savingId === message.id
                    ? "Speichert…"
                    : "Änderung speichern"}</button
                >
              </div>
            </form>
          {:else}<p class="content">{message.content}</p>
            {#if (message.author_account_id !== null && message.author_account_id === $authStore.account_id) || $authStore.role === "admin"}<div
                class="message-actions"
                aria-label={`Beitrag von ${message.author_title}`}
              >
                {#if message.author_account_id !== null && message.author_account_id === $authStore.account_id}<button
                    type="button"
                    class="link"
                    onclick={() => beginEdit(message)}>Bearbeiten</button
                  >{/if}
                <button
                  type="button"
                  class="link danger-link"
                  onclick={(event) => void requestRemoval(message, event)}
                  disabled={tombstoningId === message.id}
                  >{tombstoningId === message.id
                    ? "Entfernt…"
                    : "Entfernen"}</button
                >
              </div>{/if}
          {/if}
        </li>{/each}
    </ol>
  {/if}

  {#if canWrite}
    <form
      class="composer"
      onsubmit={(event) => {
        event.preventDefault();
        void submitMessage();
      }}
    >
      <label for="conversation-room-draft">Neuer Beitrag</label>
      <textarea
        id="conversation-room-draft"
        bind:value={draft}
        maxlength="4000"
        rows="4"
        placeholder="Schreibe einen Beitrag…"
        required></textarea>
      {#if mutationError}<p class="error" role="alert">{mutationError}</p>{/if}
      <button type="submit" disabled={posting || !draft.trim()}
        >{posting ? "Veröffentlicht…" : "Beitrag veröffentlichen"}</button
      >
    </form>
  {:else}<p class="guest-note">
      Melde dich an, um im Gesprächsraum mitzuschreiben.
    </p>{/if}
  <dialog
    bind:this={removalDialog}
    class="removal-dialog"
    role="alertdialog"
    aria-label="Beitrag entfernen"
    onkeydown={(event) => {
      // Escape closes this dialog, not the underlying map context panel.
      if (event.key === "Escape") event.stopPropagation();
    }}
    onclose={removalClosed}
  >
    <h5>Beitrag wirklich entfernen?</h5>
    <p>
      Der Inhalt wird durch „Beitrag entfernt.“ ersetzt. Das lässt sich nicht
      rückgängig machen.
    </p>
    <div class="actions">
      <button
        type="button"
        class="secondary"
        onclick={() => removalDialog.close()}>Abbrechen</button
      >
      <button
        type="button"
        class="danger-confirm"
        onclick={() => void confirmRemoval()}
        disabled={Boolean(tombstoningId)}>Beitrag entfernen</button
      >
    </div>
  </dialog>
</section>

<style>
  .conversation {
    display: grid;
    gap: 1rem;
  }
  h4,
  p {
    margin: 0;
  }
  .muted,
  .empty,
  .guest-note,
  time,
  .tombstone {
    color: var(--muted);
  }
  .messages {
    display: grid;
    gap: 0.75rem;
    list-style: none;
    padding: 0;
    margin: 0;
  }
  .messages li {
    padding: 0.8rem;
    border: 1px solid var(--panel-border);
    border-radius: 10px;
    background: var(--panel-solid);
  }
  .messages li.deleted {
    background: transparent;
  }
  header {
    display: flex;
    justify-content: space-between;
    gap: 0.75rem;
    margin-bottom: 0.45rem;
    font-size: 0.9rem;
  }
  .content {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  form,
  .composer {
    display: grid;
    gap: 0.55rem;
  }
  textarea {
    width: 100%;
    box-sizing: border-box;
    resize: vertical;
    border: 1px solid var(--panel-border-strong);
    border-radius: 8px;
    padding: 0.65rem;
    background: var(--panel-solid);
    color: var(--text);
    font: inherit;
  }
  button {
    min-height: 44px;
    border: 1px solid var(--accent);
    border-radius: 8px;
    padding: 0.55rem 0.8rem;
    background: var(--accent);
    color: var(--panel-solid);
    font-weight: 700;
    cursor: pointer;
  }
  button:disabled {
    opacity: 0.6;
    cursor: wait;
  }
  .actions,
  .message-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
  }
  .secondary,
  .older {
    background: transparent;
    color: var(--text);
    border-color: var(--panel-border-strong);
  }
  .link {
    min-height: 32px;
    padding: 0.25rem 0;
    border: 0;
    background: transparent;
    color: var(--accent);
  }
  .danger-link,
  .error {
    color: var(--danger);
  }
  .removal-dialog {
    box-sizing: border-box;
    width: min(30rem, calc(100vw - 2rem));
    max-height: calc(100dvh - 2rem);
    overflow-y: auto;
    border: 1px solid var(--panel-border-strong);
    border-radius: 12px;
    padding: 1.25rem;
    background: var(--panel-solid);
    color: var(--text);
    box-shadow: 0 12px 40px rgb(0 0 0 / 25%);
  }
  .removal-dialog::backdrop {
    background: rgb(0 0 0 / 60%);
  }
  .removal-dialog h5 {
    margin: 0 0 0.75rem;
    font-size: 1.05rem;
  }
  .removal-dialog .actions {
    justify-content: flex-end;
    margin-top: 1rem;
  }
  .removal-dialog .danger-confirm {
    background: var(--danger);
    border-color: var(--danger);
    color: white;
  }
  .guest-note {
    padding-top: 0.75rem;
    border-top: 1px solid var(--panel-border);
    font-size: 0.9rem;
  }
</style>
