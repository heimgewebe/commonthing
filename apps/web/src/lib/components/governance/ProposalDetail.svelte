<script lang="ts">
  import { createEventDispatcher, onMount } from "svelte";
  import { authStore } from "$lib/auth/store";
  import ProposalProcess from "./ProposalProcess.svelte";
  import {
    GovernanceApiError,
    formatRemaining,
    getProposal,
    listProposalMessages,
    postProposalMessage,
    requestProposalRepeal,
    proposalStatusLabel,
    submitVeto,
    submitVote,
    withdrawProposal,
    type ProposalDetail,
    type ProposalMessage,
    type VoteChoice,
  } from "$lib/api/governance";

  interface Props {
    proposalId: string;
  }

  let { proposalId }: Props = $props();

  const dispatch = createEventDispatcher<{
    messagecountchange: { proposalId: string; messageCount: number };
  }>();

  let proposal: ProposalDetail | null = $state(null);
  let messages: ProposalMessage[] = $state([]);
  let knownMessageCount = $state(0);
  let loading = $state(true);
  let error = $state("");
  let vetoReason = $state("");
  let messageBody = $state("");
  let repealSummary = $state("");
  let submitting = $state(false);

  let canDiscuss = $derived.by(() => $authStore.authenticated);
  let canDecide = $derived.by(
    () =>
      $authStore.authenticated &&
      ($authStore.role === "weber" || $authStore.role === "admin") &&
      !!proposal &&
      proposal.applicant_account_id !== $authStore.account_id,
  );
  let isOpen = $derived.by(
    () => proposal?.status === "consent" || proposal?.status === "voting",
  );
  let canWithdraw = $derived.by(
    () =>
      $authStore.authenticated &&
      !!proposal &&
      isOpen &&
      proposal.applicant_account_id === $authStore.account_id,
  );
  let canRequestRepeal = $derived.by(
    () =>
      $authStore.authenticated &&
      ($authStore.role === "weber" || $authStore.role === "admin") &&
      !!proposal &&
      proposal.kind === "sachantrag" &&
      proposal.status === "accepted" &&
      !proposal.repeals_proposal_id &&
      !proposal.pending_repeal_proposal_id &&
      !proposal.repealed_by_proposal_id,
  );

  function normalizeMessageCount(count: unknown): number {
    return typeof count === "number" &&
      Number.isFinite(count) &&
      Number.isSafeInteger(count) &&
      count >= 0
      ? count
      : 0;
  }

  function incrementMessageCount(count: number): number {
    return count < Number.MAX_SAFE_INTEGER ? count + 1 : count;
  }

  function describeError(cause: unknown): string {
    if (cause instanceof GovernanceApiError) {
      if (cause.status === 403)
        return "Diese Aktion ist für deinen Account nicht zulässig.";
      if (cause.status === 409)
        return "Die Aktion passt nicht mehr zur aktuellen Antragsphase.";
      if (cause.status === 503)
        return "Das Antragssystem ist vorübergehend nicht verfügbar.";
    }
    return "Der Antrag konnte nicht geladen oder geändert werden.";
  }

  async function refresh() {
    loading = true;
    error = "";
    try {
      const [loadedProposal, loadedMessages] = await Promise.all([
        getProposal(proposalId),
        listProposalMessages(proposalId),
      ]);
      proposal = loadedProposal;
      messages = loadedMessages;
      knownMessageCount = Math.max(
        knownMessageCount,
        normalizeMessageCount(loadedProposal.message_count),
        loadedMessages.length,
      );
      dispatch("messagecountchange", {
        proposalId,
        messageCount: knownMessageCount,
      });
    } catch (cause) {
      error = describeError(cause);
    } finally {
      loading = false;
    }
  }

  async function withdraw() {
    if (!proposal || !canWithdraw || submitting) return;
    if (
      !window.confirm(
        "Diesen Antrag zurückziehen? Die bisherige Verfahrensspur bleibt sichtbar, das Verfahren wird aber endgültig beendet.",
      )
    )
      return;
    submitting = true;
    error = "";
    try {
      const updated = await withdrawProposal(proposal.id);
      proposal = {
        ...proposal,
        ...updated,
        remaining_seconds: updated.remaining_seconds,
      };
    } catch (cause) {
      error = describeError(cause);
    } finally {
      submitting = false;
    }
  }

  async function requestRepeal() {
    if (!proposal || !canRequestRepeal || submitting) return;
    submitting = true;
    error = "";
    try {
      const created = await requestProposalRepeal(proposal.id, repealSummary);
      window.location.assign(`/antraege?id=${encodeURIComponent(created.id)}`);
    } catch (cause) {
      error = describeError(cause);
    } finally {
      submitting = false;
    }
  }

  async function veto() {
    if (!proposal || !vetoReason.trim() || submitting) return;
    submitting = true;
    error = "";
    try {
      await submitVeto(proposal.id, vetoReason);
      vetoReason = "";
      await refresh();
    } catch (cause) {
      error = describeError(cause);
    } finally {
      submitting = false;
    }
  }

  async function vote(choice: VoteChoice) {
    if (!proposal || submitting) return;
    submitting = true;
    error = "";
    try {
      await submitVote(proposal.id, choice);
      await refresh();
    } catch (cause) {
      error = describeError(cause);
    } finally {
      submitting = false;
    }
  }

  async function postMessage() {
    if (!proposal || !messageBody.trim() || submitting) return;
    submitting = true;
    error = "";
    try {
      const created = await postProposalMessage(proposal.id, messageBody);
      messages = [...messages, created];
      knownMessageCount = Math.max(
        incrementMessageCount(knownMessageCount),
        messages.length,
      );
      dispatch("messagecountchange", {
        proposalId: proposal.id,
        messageCount: knownMessageCount,
      });
      messageBody = "";
    } catch (cause) {
      error = describeError(cause);
    } finally {
      submitting = false;
    }
  }

  onMount(async () => {
    await authStore.checkAuth();
    await refresh();
  });
</script>

<svelte:head><title>Antrag · commonThing</title></svelte:head>

<main class="page-shell">
  <a class="back-link" href="/antraege">← Alle Anträge</a>

  {#if loading}
    <p>Antrag wird geladen…</p>
  {:else if error && !proposal}
    <div class="error" role="alert">{error}</div>
  {:else if proposal}
    <header class="proposal-header">
      <div class="topline">
        <span class:open={isOpen}>{proposalStatusLabel(proposal)}</span>
        {#if proposal.remaining_seconds !== undefined}<strong
            >Noch {formatRemaining(proposal.remaining_seconds)}</strong
          >{/if}
      </div>
      <p class="eyebrow">
        {proposal.kind === "sachantrag" ? "Sachantrag" : "Weberantrag"}
      </p>
      <h1>
        {proposal.kind === "sachantrag"
          ? proposal.title || "Sachantrag"
          : proposal.applicant_title}
      </h1>
      {#if proposal.summary}<p class="summary">{proposal.summary}</p>{/if}
      <dl>
        <div>
          <dt>Gestellt</dt>
          <dd>{new Date(proposal.created_at).toLocaleString("de-DE")}</dd>
        </div>
        <div>
          <dt>Verfahrensart</dt>
          <dd>
            {proposal.kind === "sachantrag"
              ? proposal.repeals_proposal_id
                ? "Aufhebung eines Beschlusses"
                : "Gemeinschaftlicher Beschluss"
              : "Aufnahme als Weber"}
          </dd>
        </div>
        <div>
          <dt>Gestellt von</dt>
          <dd>{proposal.applicant_title}</dd>
        </div>
        <div>
          <dt>Webgemeindezentrum</dt>
          <dd>
            <a
              href={`/map?focus=${encodeURIComponent(
                `webgemeindezentrum:${proposal.webgemeindezentrum_id}`,
              )}`}>Zentrum auf der Karte öffnen</a
            >
          </dd>
        </div>
        {#if proposal.kind === "sachantrag" && proposal.target_node_title}
          <div>
            <dt>Knotenbezug</dt>
            <dd>
              {#if proposal.target_node_id}
                <a
                  href={`/map?focus=${encodeURIComponent(
                    `node:${proposal.target_node_id}`,
                  )}`}>{proposal.target_node_title}</a
                >
              {:else}
                {proposal.target_node_title} (aus dem aktiven Gewebe entfernt)
              {/if}
            </dd>
          </div>
        {/if}
      </dl>
    </header>

    {#if error}<div class="error" role="alert">{error}</div>{/if}

    {#if proposal.repeals_proposal_id || proposal.pending_repeal_proposal_id || proposal.repealed_by_proposal_id}
      <section
        class="card lifecycle-card"
        aria-labelledby="proposal-lifecycle-heading"
        data-testid="proposal-lifecycle"
      >
        <h2 id="proposal-lifecycle-heading">Beschlussgeschichte</h2>
        {#if proposal.repeals_proposal_id}
          <p>
            Dieser Sachantrag beantragt die Aufhebung eines früheren
            Beschlusses.
            <a
              href={`/antraege?id=${encodeURIComponent(proposal.repeals_proposal_id)}`}
              >Früheren Beschluss öffnen</a
            >.
          </p>
        {/if}
        {#if proposal.repealed_by_proposal_id}
          <p>
            Dieser Beschluss wurde
            {#if proposal.repealed_at}
              am {new Date(proposal.repealed_at).toLocaleString("de-DE")}
            {/if}
            durch einen späteren gemeinschaftlichen Beschluss aufgehoben.
            <a
              href={`/antraege?id=${encodeURIComponent(proposal.repealed_by_proposal_id)}`}
              >Aufhebungsantrag öffnen</a
            >.
          </p>
        {:else if proposal.pending_repeal_proposal_id}
          <p>
            Für diesen Beschluss läuft bereits ein Aufhebungsverfahren.
            <a
              href={`/antraege?id=${encodeURIComponent(proposal.pending_repeal_proposal_id)}`}
              >Aufhebungsverfahren öffnen</a
            >.
          </p>
        {/if}
      </section>
    {/if}

    <ProposalProcess {proposal} messageCount={knownMessageCount} />

    {#if proposal.vetoes.length > 0}
      <section class="card" aria-labelledby="vetos-heading">
        <h2 id="vetos-heading">Vetos</h2>
        <div class="entries">
          {#each proposal.vetoes as item}
            <article>
              <div class="entry-meta">
                <strong>{item.weber_title}</strong><time
                  datetime={item.created_at}
                  >{new Date(item.created_at).toLocaleString("de-DE")}</time
                >
              </div>
              <p>{item.reason}</p>
            </article>
          {/each}
        </div>
      </section>
    {/if}

    {#if canWithdraw}
      <section
        class="card action-card"
        aria-labelledby="withdraw-heading"
        data-testid="proposal-withdraw-action"
      >
        <h2 id="withdraw-heading">Eigenen Antrag zurückziehen</h2>
        <p>
          Die Rücknahme beendet das laufende Verfahren. Der Antrag, seine
          bisherigen Beiträge und Verfahrensschritte bleiben als
          nachvollziehbare Geschichte sichtbar.
        </p>
        <button class="primary" onclick={withdraw} disabled={submitting}
          >Antrag zurückziehen</button
        >
      </section>
    {/if}

    {#if canRequestRepeal}
      <section
        class="card action-card"
        aria-labelledby="repeal-heading"
        data-testid="proposal-repeal-action"
      >
        <h2 id="repeal-heading">Aufhebung beantragen</h2>
        <p>
          Der bestehende Beschluss wird nicht überschrieben. Stattdessen
          entsteht ein neuer Sachantrag, der das normale Konsent-, Veto- und
          Abstimmungsverfahren durchläuft.
        </p>
        <label for="repeal-summary">Begründung (optional)</label>
        <textarea
          id="repeal-summary"
          bind:value={repealSummary}
          maxlength="2000"
          rows="4"
          placeholder="Warum soll dieser Beschluss aufgehoben werden?"
        ></textarea>
        <button class="primary" onclick={requestRepeal} disabled={submitting}
          >Aufhebung beantragen</button
        >
      </section>
    {/if}

    {#if canDecide && proposal.status === "consent"}
      <section class="card action-card" aria-labelledby="veto-heading">
        <h2 id="veto-heading">Begründetes Veto einlegen</h2>
        <p>
          Ein Veto verhindert keine Entscheidung. Es eröffnet nach Ablauf der
          ersten sieben Tage eine weitere siebentägige Gesprächs- und
          Abstimmungsphase.
        </p>
        <textarea
          bind:value={vetoReason}
          maxlength="2000"
          rows="4"
          placeholder="Konkreter Einwand und mögliche Lösung"></textarea>
        <button
          class="primary"
          onclick={veto}
          disabled={!vetoReason.trim() || submitting}>Veto einlegen</button
        >
      </section>
    {/if}

    {#if canDecide && proposal.status === "voting"}
      <section class="card action-card" aria-labelledby="vote-heading">
        <h2 id="vote-heading">Abstimmen</h2>
        <p>
          Es gibt keine Mindestbeteiligung. Der Antrag wird angenommen, wenn am
          Ende mehr Ja- als Nein-Stimmen vorliegen.
        </p>
        <div class="vote-buttons">
          {#each ["ja", "nein", "enthaltung"] as choice}
            <button
              class:active={proposal.own_vote === choice}
              onclick={() => vote(choice as VoteChoice)}
              disabled={submitting}
            >
              {choice === "ja"
                ? "Ja"
                : choice === "nein"
                  ? "Nein"
                  : "Enthaltung"}
            </button>
          {/each}
        </div>
      </section>
    {/if}

    <section class="card" aria-labelledby="conversation-heading">
      <h2 id="conversation-heading">Gesprächsraum</h2>
      {#if messages.length === 0}<p class="muted">
          Noch keine Gesprächsbeiträge.
        </p>{/if}
      <div class="entries">
        {#each messages as message}
          <article>
            <div class="entry-meta">
              <strong>{message.author_title}</strong><time
                datetime={message.created_at}
                >{new Date(message.created_at).toLocaleString("de-DE")}</time
              >
            </div>
            <p>{message.body}</p>
          </article>
        {/each}
      </div>
      {#if canDiscuss && isOpen}
        <div class="message-form">
          <label for="message-body">Beitrag verfassen</label>
          <textarea
            id="message-body"
            bind:value={messageBody}
            maxlength="4000"
            rows="4"></textarea>
          <button
            class="primary"
            onclick={postMessage}
            disabled={!messageBody.trim() || submitting}>Beitrag senden</button
          >
        </div>
      {:else if !$authStore.authenticated}
        <p class="guest-note">
          Melde dich an, um im Gesprächsraum mitzuschreiben.
        </p>
      {/if}
    </section>
  {/if}
</main>

<style>
  :global(body) {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: system-ui, sans-serif;
  }
  .page-shell {
    width: min(820px, calc(100% - 32px));
    margin: 0 auto;
    padding: 32px 0 72px;
  }
  .back-link {
    color: inherit;
    text-decoration: none;
    font-weight: 700;
  }
  .proposal-header {
    padding: 44px 0 28px;
  }
  .topline,
  .entry-meta,
  dl,
  .vote-buttons {
    display: flex;
    gap: 12px;
    align-items: center;
  }
  .topline {
    justify-content: space-between;
    color: var(--muted);
  }
  .topline span {
    padding: 5px 10px;
    border-radius: 999px;
    background: var(--panel-border);
  }
  .topline span.open {
    background: var(--accent-soft);
    color: var(--accent);
  }
  .eyebrow {
    margin: 28px 0 5px;
    text-transform: uppercase;
    letter-spacing: 0.09em;
    font-size: 0.76rem;
    font-weight: 750;
    color: var(--accent);
  }
  h1 {
    margin: 0;
    font-size: clamp(2.2rem, 8vw, 4.2rem);
    line-height: 1;
  }
  h2 {
    margin: 0 0 12px;
  }
  .summary {
    max-width: 65ch;
    font-size: 1.1rem;
    line-height: 1.6;
  }
  dl {
    flex-wrap: wrap;
    margin: 24px 0 0;
  }
  dl div {
    min-width: 150px;
  }
  dt {
    color: var(--muted);
    font-size: 0.8rem;
  }
  dd {
    margin: 3px 0 0;
    font-weight: 700;
  }
  .card,
  .error {
    border: 1px solid var(--panel-border);
    border-radius: 18px;
    background: var(--panel);
    padding: 22px;
    margin-top: 16px;
  }
  .action-card {
    background: var(--accent-soft);
  }
  .lifecycle-card {
    border-inline-start: 4px solid var(--accent);
  }
  .lifecycle-card p {
    line-height: 1.55;
  }
  .entries {
    display: grid;
    gap: 14px;
  }
  article {
    border-top: 1px solid var(--panel-border);
    padding-top: 14px;
  }
  article:first-child {
    border-top: 0;
    padding-top: 0;
  }
  .entry-meta {
    justify-content: space-between;
    color: var(--muted);
    font-size: 0.84rem;
  }
  article p {
    margin-bottom: 0;
    white-space: pre-wrap;
    line-height: 1.5;
  }
  textarea {
    box-sizing: border-box;
    width: 100%;
    resize: vertical;
    border: 1px solid var(--panel-border-strong);
    border-radius: 12px;
    padding: 12px;
    font: inherit;
    background: var(--panel-solid);
  }
  button {
    border: 1px solid var(--panel-border-strong);
    border-radius: 999px;
    padding: 10px 16px;
    font: inherit;
    font-weight: 720;
    cursor: pointer;
    background: var(--panel-solid);
  }
  button.active {
    outline: 3px solid var(--accent-soft);
    background: var(--accent-soft);
  }
  button:disabled {
    opacity: 0.55;
    cursor: wait;
  }
  .primary {
    border: 0;
    background: var(--accent);
    color: #fff;
  }
  .action-card,
  .message-form {
    display: grid;
    gap: 12px;
  }
  .message-form {
    margin-top: 20px;
  }
  .vote-buttons {
    flex-wrap: wrap;
  }
  .error {
    color: var(--danger);
    background: color-mix(in srgb, var(--danger) 10%, var(--panel-solid));
  }
  .muted,
  .guest-note {
    color: var(--muted);
  }
  .guest-note {
    padding: 12px;
    border-radius: 10px;
    background: var(--panel-solid);
  }
  @media (max-width: 560px) {
    .topline,
    .entry-meta {
      align-items: flex-start;
      flex-direction: column;
    }
  }
</style>
