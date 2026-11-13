<script lang="ts">
  import { onMount, tick } from "svelte";
  import { goto } from "$app/navigation";
  import { page } from "$app/stores";
  import { authStore } from "$lib/auth/store";
  import type { ConversationMessage } from "$lib/api/nodeConversation";
  import { NodeConversationApiError } from "$lib/api/nodeConversation";
  import {
    DirectMessagesApiError,
    listDirectConversations,
    listDirectMessages,
    markDirectConversationRead,
    openDirectConversation,
    sendDirectMessage,
    setDirectConversationBlocked,
    type DirectConversation,
  } from "$lib/api/directMessages";
  import "$lib/styles/page-system.css";

  let conversations: DirectConversation[] = $state([]);
  let selected: DirectConversation | null = $state(null);
  let messages: ConversationMessage[] = $state([]);
  let draft = $state("");
  let loadingInbox = $state(true);
  let loadingConversation = $state(false);
  let sending = $state(false);
  let changingBlock = $state(false);
  let error = $state("");
  let initialized = $state(false);
  let handledRecipient: string | null = $state(null);
  let messageList: HTMLElement | null = $state(null);
  let selectionGeneration = 0;

  function describeError(cause: unknown): string {
    if (
      cause instanceof DirectMessagesApiError ||
      cause instanceof NodeConversationApiError
    ) {
      if (cause.status === 401)
        return "Bitte melde dich an, um Nachrichten zu lesen.";
      if (cause.status === 404)
        return "Diese Unterhaltung ist nicht mehr verfügbar.";
      if (cause.code === "direct_conversation_blocked") {
        return "In dieser Unterhaltung sind neue Nachrichten blockiert.";
      }
      if (cause.code === "direct_conversation_pair_retired") {
        return "Mit diesem früheren Konto kann keine neue Unterhaltung verknüpft werden.";
      }
      if (cause.code === "direct_conversation_counterpart_unavailable") {
        return "Das andere Konto besteht nicht mehr. Die bisherige Unterhaltung bleibt lesbar.";
      }
      if (cause.code === "direct_conversation_create_rate_limited") {
        return "Du hast in kurzer Zeit zu viele neue Unterhaltungen begonnen. Versuche es später erneut.";
      }
      if (
        cause.code === "direct_message_rate_limited" ||
        cause.code === "message_rate_limited"
      ) {
        return "Du sendest gerade zu viele Nachrichten. Versuche es in einer Minute erneut.";
      }
      if (cause.code === "invalid_message_content") {
        return "Eine Nachricht muss zwischen 1 und 4.000 Zeichen enthalten.";
      }
    }
    return "Die privaten Nachrichten konnten nicht verarbeitet werden.";
  }

  function sortMessages(items: ConversationMessage[]): ConversationMessage[] {
    return [...items].sort(
      (left, right) =>
        left.created_at.localeCompare(right.created_at) ||
        left.id.localeCompare(right.id),
    );
  }

  function formatDate(value: string | null): string {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return new Intl.DateTimeFormat("de-DE", {
      dateStyle: "short",
      timeStyle: "short",
    }).format(date);
  }

  async function refreshInbox(): Promise<void> {
    loadingInbox = true;
    error = "";
    try {
      conversations = await listDirectConversations();
      if (selected) {
        selected =
          conversations.find((item) => item.id === selected?.id) ?? selected;
      }
    } catch (cause) {
      error = describeError(cause);
    } finally {
      loadingInbox = false;
    }
  }

  async function scrollToNewest(generation?: number): Promise<void> {
    await tick();
    if (generation !== undefined && generation !== selectionGeneration) return;
    messageList?.scrollTo({ top: messageList.scrollHeight });
  }

  async function selectConversation(
    conversation: DirectConversation,
    generation = ++selectionGeneration,
  ): Promise<void> {
    if (generation !== selectionGeneration) return;
    selected = conversation;
    messages = [];
    loadingConversation = true;
    error = "";
    try {
      const response = await listDirectMessages(conversation.id);
      if (generation !== selectionGeneration) return;
      messages = sortMessages(response.items);
      const newestLoadedMessage = messages.at(-1);
      if (newestLoadedMessage) {
        await markDirectConversationRead(
          conversation.id,
          newestLoadedMessage.id,
        );
      }
      conversations = conversations.map((item) =>
        item.id === conversation.id ? { ...item, unread_count: 0 } : item,
      );
      if (generation !== selectionGeneration) return;
      selected = { ...conversation, unread_count: 0 };
      await scrollToNewest(generation);
    } catch (cause) {
      if (generation === selectionGeneration) {
        error = describeError(cause);
      }
    } finally {
      if (generation === selectionGeneration) {
        loadingConversation = false;
      }
    }
  }

  async function startConversation(accountId: string): Promise<void> {
    if (!$authStore.authenticated || accountId === $authStore.account_id)
      return;
    const generation = ++selectionGeneration;
    error = "";
    try {
      const conversation = await openDirectConversation(accountId);
      const existingIndex = conversations.findIndex(
        (item) => item.id === conversation.id,
      );
      const isCurrentSelection = generation === selectionGeneration;
      if (existingIndex < 0) {
        conversations = [conversation, ...conversations];
      } else if (isCurrentSelection) {
        conversations = conversations.map((item) =>
          item.id === conversation.id ? conversation : item,
        );
      }
      if (!isCurrentSelection) return;
      await selectConversation(conversation, generation);
      if (generation !== selectionGeneration) return;
      await goto(`/nachrichten?id=${encodeURIComponent(conversation.id)}`, {
        replaceState: true,
        keepFocus: true,
        noScroll: true,
      });
    } catch (cause) {
      if (generation === selectionGeneration) {
        error = describeError(cause);
      }
    }
  }

  async function send(): Promise<void> {
    const content = draft.trim();
    if (!selected || !content || sending || !selected.can_send) return;
    sending = true;
    error = "";
    try {
      const created = await sendDirectMessage(
        selected.id,
        content,
        crypto.randomUUID(),
      );
      messages = sortMessages([...messages, created]);
      draft = "";
      const preview = content.slice(0, 160);
      conversations = conversations
        .map((item) =>
          item.id === selected?.id
            ? {
                ...item,
                last_message_preview: preview,
                last_message_at: created.created_at,
                updated_at: created.created_at,
              }
            : item,
        )
        .sort((left, right) => right.updated_at.localeCompare(left.updated_at));
      await scrollToNewest();
    } catch (cause) {
      error = describeError(cause);
    } finally {
      sending = false;
    }
  }

  async function toggleBlock(): Promise<void> {
    if (!selected || changingBlock) return;
    changingBlock = true;
    error = "";
    try {
      const updated = await setDirectConversationBlocked(
        selected.id,
        !selected.blocked_by_me,
      );
      selected = updated;
      conversations = conversations.map((item) =>
        item.id === updated.id ? updated : item,
      );
    } catch (cause) {
      error = describeError(cause);
    } finally {
      changingBlock = false;
    }
  }

  function handleComposerKeydown(event: KeyboardEvent): void {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      void send();
    }
  }

  onMount(async () => {
    if (window.location.hash === "#benachrichtigungen") {
      await goto("/settings#benachrichtigungen", { replaceState: true });
      return;
    }

    await authStore.checkAuth();
    if ($authStore.authenticated) {
      await refreshInbox();
      const requestedId = $page.url.searchParams.get("id");
      if (requestedId) {
        const conversation = conversations.find(
          (item) => item.id === requestedId,
        );
        if (conversation) await selectConversation(conversation);
      }
    } else {
      loadingInbox = false;
    }
    initialized = true;
  });
  let requestedRecipient = $derived($page.url.searchParams.get("mit"));
  $effect.pre(() => {
    if (
      initialized &&
      requestedRecipient &&
      requestedRecipient !== handledRecipient
    ) {
      handledRecipient = requestedRecipient;
      void startConversation(requestedRecipient);
    }
  });
</script>

<svelte:head>
  <title>Nachrichten · commonThing</title>
  <meta
    name="description"
    content="Private Nachrichten zwischen zwei angemeldeten commonThing-Konten."
  />
</svelte:head>

<main class="page-shell messages-page">
  <header class="page-heading">
    <div>
      <p class="eyebrow">Direkter Austausch</p>
      <h1>Nachrichten</h1>
    </div>
    <a class="back-link" href="/map">Zur Karte</a>
  </header>

  {#if !$authStore.authenticated}
    <section class="state-card" aria-labelledby="login-required">
      <h2 id="login-required">Anmeldung erforderlich</h2>
      <p>Private Nachrichten sind nur für angemeldete Konten verfügbar.</p>
      <a class="primary-link" href="/login?returnTo=%2Fnachrichten">Anmelden</a>
    </section>
  {:else}
    <p class="privacy-note">
      Nur die beiden beteiligten Konten können diese Unterhaltung öffnen. Die
      Nachrichten sind jedoch nicht Ende-zu-Ende verschlüsselt.
    </p>

    <div class="message-settings">
      <a class="notification-settings-link" href="/settings#benachrichtigungen">
        Benachrichtigungen einstellen
      </a>
    </div>

    {#if error}<div class="error" role="alert">{error}</div>{/if}

    <div class="messages-layout">
      <section class="inbox" aria-labelledby="inbox-title">
        <div class="section-heading">
          <h2 id="inbox-title">Postfach</h2>
          <button
            type="button"
            class="quiet"
            onclick={refreshInbox}
            disabled={loadingInbox}
          >
            Aktualisieren
          </button>
        </div>

        {#if loadingInbox}
          <p class="muted">Lade Unterhaltungen…</p>
        {:else if conversations.length === 0}
          <p class="muted">
            Noch keine Unterhaltung. Öffne eine Garnrolle auf der Karte und
            wähle „Private Nachricht“.
          </p>
        {:else}
          <ul class="conversation-list">
            {#each conversations as conversation}
              <li>
                <button
                  type="button"
                  class:active={selected?.id === conversation.id}
                  onclick={() => selectConversation(conversation)}
                  aria-current={selected?.id === conversation.id
                    ? "true"
                    : undefined}
                >
                  <span class="conversation-title">
                    {conversation.counterpart_title}
                    {#if conversation.unread_count > 0}
                      <span
                        class="unread"
                        aria-label={`${conversation.unread_count} ungelesene Nachrichten`}
                      >
                        {conversation.unread_count}
                      </span>
                    {/if}
                  </span>
                  <span class="preview">
                    {conversation.last_message_preview ??
                      "Noch keine Nachricht"}
                  </span>
                  {#if conversation.last_message_at}
                    <time datetime={conversation.last_message_at}>
                      {formatDate(conversation.last_message_at)}
                    </time>
                  {/if}
                </button>
              </li>
            {/each}
          </ul>
        {/if}
      </section>

      <section class="conversation" aria-labelledby="conversation-title">
        {#if selected}
          <div class="section-heading conversation-heading">
            <div>
              <h2 id="conversation-title">{selected.counterpart_title}</h2>
              {#if selected.counterpart_account_id === null}
                <p class="account-exited">Dieses Konto besteht nicht mehr.</p>
              {/if}
            </div>
            <button
              type="button"
              class="quiet danger"
              onclick={toggleBlock}
              disabled={changingBlock}
            >
              {selected.blocked_by_me ? "Freigeben" : "Blockieren"}
            </button>
          </div>

          <div class="message-list" bind:this={messageList} aria-live="polite">
            {#if loadingConversation}
              <p class="muted">Lade Nachrichten…</p>
            {:else if messages.length === 0}
              <p class="muted">Noch keine Nachricht.</p>
            {:else}
              {#each messages as message}
                <article
                  class="message"
                  class:own={message.author_account_id ===
                    $authStore.account_id}
                >
                  <header>
                    <strong>{message.author_title}</strong>
                    <time datetime={message.created_at}
                      >{formatDate(message.created_at)}</time
                    >
                  </header>
                  {#if message.deleted_at}
                    <p class="deleted">Nachricht entfernt</p>
                  {:else}
                    <p>{message.content}</p>
                  {/if}
                </article>
              {/each}
            {/if}
          </div>

          {#if selected.blocked_by_me}
            <p class="blocked-note">
              Du hast diese Unterhaltung blockiert. Neue Nachrichten sind für
              beide Seiten deaktiviert.
            </p>
          {:else if selected.counterpart_account_id === null}
            <p class="blocked-note">
              An ein aufgelöstes Konto können keine neuen Nachrichten gesendet
              werden.
            </p>
          {:else if !selected.can_send}
            <p class="blocked-note">
              In dieser Unterhaltung sind neue Nachrichten derzeit deaktiviert.
              Die bisherigen Nachrichten bleiben lesbar.
            </p>
          {:else}
            <form
              class="composer"
              onsubmit={(event) => {
                event.preventDefault();
                void send();
              }}
            >
              <label for="direct-message">Nachricht</label>
              <textarea
                id="direct-message"
                bind:value={draft}
                maxlength="4000"
                rows="4"
                onkeydown={handleComposerKeydown}
                placeholder="Nachricht schreiben…"></textarea>
              <div class="composer-footer">
                <span>{draft.length}/4000 · Strg/⌘ + Enter</span>
                <button
                  type="submit"
                  disabled={sending || draft.trim().length === 0}
                >
                  {sending ? "Sende…" : "Senden"}
                </button>
              </div>
            </form>
          {/if}
        {:else}
          <div class="empty-conversation">
            <h2 id="conversation-title">Unterhaltung auswählen</h2>
            <p>
              Wähle links eine Unterhaltung oder öffne eine Garnrolle auf der
              Karte.
            </p>
          </div>
        {/if}
      </section>
    </div>
  {/if}
</main>

<style>
  .messages-page {
    max-width: 78rem;
  }

  .page-heading,
  .section-heading,
  .conversation-heading,
  .composer-footer,
  .message header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
  }

  .page-heading h1,
  .section-heading h2,
  .empty-conversation h2 {
    margin: 0;
  }

  .eyebrow {
    margin: 0 0 0.25rem;
    color: var(--muted);
    font-size: 0.8rem;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .back-link,
  .primary-link,
  .notification-settings-link {
    min-height: 44px;
    display: inline-flex;
    align-items: center;
    padding: 0 0.9rem;
    border: 1px solid var(--panel-border-strong);
    border-radius: 999px;
    color: var(--text);
    text-decoration: none;
    font-weight: 650;
  }

  .privacy-note,
  .state-card,
  .error {
    margin: 1rem 0;
    padding: 0.85rem 1rem;
    border: 1px solid var(--panel-border-strong);
    border-radius: 12px;
    background: var(--panel);
  }

  .privacy-note {
    color: var(--muted);
    font-size: 0.9rem;
  }

  .message-settings {
    display: flex;
    justify-content: flex-end;
    margin: 0 0 1rem;
  }

  .notification-settings-link {
    border-color: transparent;
    color: var(--accent);
  }

  .error {
    border-color: var(--danger, #b44343);
  }

  .messages-layout {
    min-height: min(70vh, 48rem);
    display: grid;
    grid-template-columns: minmax(16rem, 0.38fr) minmax(0, 1fr);
    overflow: hidden;
    border: 1px solid var(--panel-border-strong);
    border-radius: 16px;
    background: var(--panel);
  }

  .inbox,
  .conversation {
    min-width: 0;
    padding: 1rem;
  }

  .inbox {
    border-right: 1px solid var(--panel-border-strong);
  }

  .quiet {
    min-height: 40px;
    padding: 0 0.75rem;
    border: 1px solid var(--panel-border-strong);
    border-radius: 999px;
    background: transparent;
    color: var(--text);
    font: inherit;
    cursor: pointer;
  }

  .quiet:disabled {
    cursor: wait;
    opacity: 0.6;
  }

  .danger {
    color: var(--danger, #b44343);
  }

  .conversation-list {
    list-style: none;
    margin: 1rem -0.35rem 0;
    padding: 0;
    display: grid;
    gap: 0.25rem;
  }

  .conversation-list button {
    width: 100%;
    min-height: 70px;
    display: grid;
    gap: 0.25rem;
    padding: 0.7rem;
    border: 1px solid transparent;
    border-radius: 10px;
    background: transparent;
    color: var(--text);
    text-align: left;
    cursor: pointer;
  }

  .conversation-list button:hover,
  .conversation-list button:focus-visible,
  .conversation-list button.active {
    border-color: var(--panel-border-strong);
    background: var(--panel-solid);
  }

  .conversation-title {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
    font-weight: 700;
  }

  .unread {
    min-width: 1.5rem;
    height: 1.5rem;
    display: grid;
    place-items: center;
    padding: 0 0.35rem;
    border-radius: 999px;
    background: var(--accent);
    color: var(--panel-solid, white);
    font-size: 0.75rem;
  }

  .preview,
  .conversation-list time,
  .muted,
  .account-exited,
  .message time,
  .composer-footer span {
    color: var(--muted);
  }

  .preview {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .conversation-list time,
  .message time,
  .composer-footer span {
    font-size: 0.78rem;
  }

  .conversation {
    display: grid;
    grid-template-rows: auto minmax(16rem, 1fr) auto;
    gap: 1rem;
  }

  .account-exited {
    margin: 0.2rem 0 0;
    font-size: 0.85rem;
  }

  .message-list {
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 0.65rem;
    padding: 0.25rem;
  }

  .message {
    max-width: min(78%, 38rem);
    align-self: flex-start;
    padding: 0.7rem 0.8rem;
    border: 1px solid var(--panel-border-strong);
    border-radius: 12px 12px 12px 4px;
    background: var(--panel-solid);
  }

  .message.own {
    align-self: flex-end;
    border-radius: 12px 12px 4px 12px;
  }

  .message header {
    align-items: baseline;
    gap: 0.75rem;
  }

  .message p {
    margin: 0.4rem 0 0;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

  .deleted {
    color: var(--muted);
    font-style: italic;
  }

  .composer {
    display: grid;
    gap: 0.5rem;
  }

  .composer label {
    font-weight: 700;
  }

  .composer textarea {
    width: 100%;
    resize: vertical;
    box-sizing: border-box;
    padding: 0.75rem;
    border: 1px solid var(--panel-border-strong);
    border-radius: 10px;
    background: var(--panel-solid);
    color: var(--text);
    font: inherit;
  }

  .composer button {
    min-height: 44px;
    padding: 0 1rem;
    border: 0;
    border-radius: 999px;
    background: var(--accent);
    color: var(--panel-solid, white);
    font: inherit;
    font-weight: 700;
    cursor: pointer;
  }

  .composer button:disabled {
    cursor: not-allowed;
    opacity: 0.55;
  }

  .blocked-note {
    margin: 0;
    padding: 0.75rem;
    border: 1px solid var(--panel-border-strong);
    border-radius: 10px;
    color: var(--muted);
  }

  .empty-conversation {
    align-self: center;
    justify-self: center;
    max-width: 26rem;
    text-align: center;
  }

  @media (max-width: 720px) {
    .page-heading {
      align-items: flex-start;
    }

    .message-settings {
      justify-content: stretch;
    }

    .notification-settings-link {
      width: 100%;
      justify-content: center;
    }

    .messages-layout {
      grid-template-columns: 1fr;
      overflow: visible;
    }

    .inbox {
      border-right: 0;
      border-bottom: 1px solid var(--panel-border-strong);
    }

    .conversation {
      min-height: 34rem;
    }

    .message {
      max-width: 92%;
    }
  }
</style>
