<script lang="ts">
  import { browser } from "$app/environment";
  import { goto, invalidateAll } from "$app/navigation";
  import {
    kompositionDraft,
    lastCreatedNodeId,
    leaveToNavigation,
    systemState,
  } from "$lib/stores/uiView";
  import { authStore } from "$lib/auth/store";
  import { sensitiveSession } from "$lib/auth/sensitiveSession";
  import { createNode, ApiRequestError } from "$lib/api/domainWrites";
  import { combineKnottingTags, type KnottingTopic } from "$lib/knottingTopics";
  import KnottingTopicsSelector from "$lib/components/KnottingTopicsSelector.svelte";
  import { onMount, tick } from "svelte";

  let title = $state("");
  let description = $state("");
  let address = $state("");
  let nodeType = $state("standard");
  let topics: KnottingTopic[] = $state([]);
  let isSubmitting = $state(false);
  let titleInput: HTMLInputElement | null = $state(null);

  let titleError = $state(false);
  let addressError = $state(false);
  let formError: string | null = $state(null);

  // One operation id identifies one semantic user action. A transport failure
  // may hide a successful server write, so retries reuse the same id while the
  // payload stays unchanged. Editing the form starts a new semantic action.
  let nodeOperationId: string | null = null;
  let nodeOperationSignature: string | null = null;

  let canWrite = $derived($authStore.authenticated);
  let placingGarnrolle = $derived(
    $kompositionDraft?.mode === "place-garnrolle",
  );
  let canSubmit = $derived(
    !!$kompositionDraft?.lngLat && !!title.trim() && !!address.trim(),
  );

  onMount(async () => {
    await tick();
    titleInput?.focus();
  });

  function operationIdForNode(payload: {
    title: string;
    kind: string;
    address: string;
    location: { lat: number; lon: number };
    summary?: string;
    tags: string[];
  }): string {
    const signature = JSON.stringify(payload);
    if (!nodeOperationId || nodeOperationSignature !== signature) {
      nodeOperationId = crypto.randomUUID();
      nodeOperationSignature = signature;
    }
    return nodeOperationId;
  }

  async function returnToGarnrolleSettings(withLocation: boolean) {
    formError = null;
    const location = withLocation ? $kompositionDraft?.lngLat : undefined;
    const accountId = $authStore.account_id;
    if (withLocation) {
      if (!browser || !location || !accountId) {
        formError =
          "Der private Kartenanker konnte nicht für die Rückkehr gespeichert werden. Bitte versuche es erneut.";
        return;
      }
      const stored = sensitiveSession.write(
        accountId,
        `weltgewebe:garnrolle-return-location:${accountId}`,
        JSON.stringify({ lat: location[1], lon: location[0] }),
      );
      if (!stored) {
        formError =
          "Der private Kartenanker konnte im Browser nicht zwischengespeichert werden. Prüfe die Browserspeicher-Einstellungen und versuche es erneut.";
        return;
      }
    }
    leaveToNavigation();
    await goto("/settings#meine-garnrolle");
  }

  export function requestClose() {
    handleCancel();
  }

  function handleCancel() {
    if (placingGarnrolle) {
      void returnToGarnrolleSettings(false);
      return;
    }
    leaveToNavigation();
  }

  function describeCreateNodeError(err: unknown): string {
    if (err instanceof ApiRequestError) {
      if (err.status === 401 || err.status === 403) {
        return "Du hast keine Berechtigung, einen Knoten anzulegen.";
      }
      if (err.status === 400) {
        return "Bitte prüfe deine Eingaben: Name, Knotenart, Adresse und Ort müssen ausgefüllt sein.";
      }
      if (err.status === 409) {
        return "Dieser Knoten existiert bereits.";
      }
    }
    return "Der Knoten konnte nicht gespeichert werden. Bitte versuche es später erneut.";
  }

  async function finalizeSuccess(nodeId: string) {
    await invalidateAll();
    lastCreatedNodeId.set(nodeId);
    leaveToNavigation();
  }

  async function handleSubmit(e: Event) {
    e.preventDefault();
    formError = null;
    titleError = !title.trim();
    addressError = !address.trim();

    if (titleError || addressError || !$kompositionDraft?.lngLat || !canWrite) {
      return;
    }

    isSubmitting = true;
    try {
      // Create a local snapshot to guard against stale state transitions
      const submitDraft = $kompositionDraft;
      const [lon, lat] = submitDraft.lngLat!;

      const nodePayload = {
        title: title.trim(),
        kind: nodeType,
        address: address.trim(),
        location: { lat, lon },
        summary: description.trim() || undefined,
        tags: combineKnottingTags(topics, []),
      };
      const node = await createNode({
        ...nodePayload,
        operation_id: operationIdForNode(nodePayload),
      });

      // Guard: only proceed if we are still in komposition state and the
      // draft hasn't been replaced in the meantime.
      if ($systemState === "komposition" && $kompositionDraft === submitDraft) {
        // Der Faden ist keine zweite Nutzeraktion. Der Server hat ihn bereits
        // als Projektion derselben Webungsaktion erzeugt oder die Anfrage
        // fehlgeschlagen zurückgegeben.
        await finalizeSuccess(node.id);
      }
    } catch (err) {
      formError = describeCreateNodeError(err);
    } finally {
      isSubmitting = false;
    }
  }
</script>

<div class="komposition-mode">
  {#if !canWrite}
    <div class="state-pending">
      <p><strong>Kein Zugriff</strong></p>
      <p>Jeder angemeldete Account kann einen neuen Knoten knüpfen.</p>
    </div>
    <div class="actions">
      <button type="button" class="btn btn-secondary" onclick={handleCancel}
        >Schließen</button
      >
    </div>
  {:else if placingGarnrolle}
    <div data-testid="garnrolle-placement">
      {#if formError}
        <div class="form-error" role="alert">{formError}</div>
      {/if}
      {#if $kompositionDraft?.lngLat}
        <div class="state-set">
          <p><strong>Privater Kartenanker gewählt</strong></p>
          <p class="ghost">
            Prüfe, ob dies dein gewünschter privater Anker ist. Zum Verschieben
            tippe oder klicke einfach auf eine andere Stelle. Erst in den
            Einstellungen entscheidest du über die öffentliche Sichtbarkeit und
            speicherst die Garnrolle.
          </p>
        </div>
      {:else}
        <div class="state-pending">
          <p><strong>Ort ausstehend</strong></p>
          <p>
            Tippe oder klicke auf den gewünschten Ort auf der Karte, um den
            Punkt zu setzen. Du kannst ihn danach bestätigen oder erneut setzen.
            (Ein längeres Gedrückthalten funktioniert ebenfalls.)
          </p>
        </div>
      {/if}
      <div class="actions">
        <button
          type="button"
          class="btn btn-secondary"
          onclick={() => returnToGarnrolleSettings(false)}
        >
          Abbrechen
        </button>
        <button
          type="button"
          class="btn btn-primary"
          disabled={!$kompositionDraft?.lngLat}
          onclick={() => returnToGarnrolleSettings(true)}
          data-testid="confirm-garnrolle-location"
        >
          Diesen Punkt übernehmen
        </button>
      </div>
    </div>
  {:else}
    <form onsubmit={handleSubmit} class="komposition-form">
      {#if $kompositionDraft?.lngLat}
        <div class="state-set">
          <p><strong>Ort gewählt</strong></p>
          <p class="ghost">
            Du kannst den Ort ändern, indem du einen anderen Punkt auf der Karte
            etwa eine Sekunde gedrückt hältst.
          </p>
        </div>
      {:else}
        <div class="state-pending">
          <p><strong>Ort ausstehend</strong></p>
          <p>
            Halte den gewünschten Ort auf der Karte etwa eine Sekunde gedrückt.
          </p>
        </div>
      {/if}

      {#if formError}
        <div class="form-error" role="alert">{formError}</div>
      {/if}

      <div class="form-group">
        <label for="nodeType">Knotenart</label>
        <select
          id="nodeType"
          bind:value={nodeType}
          class="input"
          disabled={isSubmitting}
        >
          <option value="standard">Allgemeiner Knoten</option>
          <option value="event">Ereignis</option>
          <option value="resource">Ressource</option>
        </select>
      </div>

      <div class="form-group">
        <label for="title">Name *</label>
        <input
          type="text"
          id="title"
          bind:this={titleInput}
          bind:value={title}
          class="input"
          class:error={titleError}
          placeholder="Name des Knotens"
          disabled={isSubmitting}
          required
          aria-invalid={titleError}
          aria-describedby={titleError ? "title-error" : undefined}
        />
        {#if titleError}
          <span id="title-error" class="error-msg">Name ist erforderlich</span>
        {/if}
      </div>

      <KnottingTopicsSelector
        id="create-knot-topics"
        bind:value={topics}
        disabled={isSubmitting}
      />

      <div class="form-group">
        <label for="address">Adresse *</label>
        <input
          type="text"
          id="address"
          bind:value={address}
          class="input"
          class:error={addressError}
          placeholder="Straße, Hausnummer, Ort"
          disabled={isSubmitting}
          required
          aria-invalid={addressError}
          aria-describedby={addressError ? "address-error" : undefined}
        />
        {#if addressError}
          <span id="address-error" class="error-msg"
            >Adresse ist erforderlich</span
          >
        {/if}
      </div>

      <div class="form-group">
        <label for="description">Kurze Beschreibung</label>
        <textarea
          id="description"
          bind:value={description}
          class="input"
          placeholder="Worum geht es hier?"
          rows="4"
          disabled={isSubmitting}></textarea>
      </div>

      <div class="actions">
        <button
          type="button"
          class="btn btn-secondary"
          onclick={handleCancel}
          disabled={isSubmitting}
        >
          Abbrechen
        </button>
        <button
          type="submit"
          class="btn btn-primary"
          disabled={isSubmitting || !canSubmit}
        >
          {isSubmitting ? "Wird geknüpft…" : "Knoten knüpfen"}
        </button>
      </div>
    </form>
  {/if}
</div>

<style>
  .komposition-mode {
    min-height: 0;
  }

  .state-set,
  .state-pending {
    background: var(--panel-border, rgba(255, 255, 255, 0.06));
    padding: 1rem;
    border-radius: var(--radius, 8px);
    margin-bottom: 1.5rem;
  }

  .state-pending {
    border: 2px dashed var(--muted, #9aa4b2);
    background: transparent;
  }

  .ghost {
    color: var(--muted, #9aa4b2);
    font-size: 0.85rem;
    margin-top: 0.5rem;
  }

  .form-error {
    background: rgba(255, 107, 107, 0.12);
    border: 1px solid #ff6b6b;
    border-radius: var(--radius, 8px);
    color: var(--text, #e9eef5);
    padding: 0.75rem 1rem;
    margin-bottom: 1.25rem;
    font-size: 0.9rem;
  }

  .form-group {
    margin-bottom: 1.25rem;
  }

  label {
    display: block;
    margin-bottom: 0.5rem;
    font-weight: 500;
    color: var(--text, #e9eef5);
  }

  .input {
    width: 100%;
    padding: 0.75rem;
    border: 1px solid var(--panel-border, rgba(255, 255, 255, 0.06));
    border-radius: var(--radius, 6px);
    font-family: inherit;
    font-size: 1rem;
    background: var(--bg, #0f1115);
    color: var(--text, #e9eef5);
    box-sizing: border-box;
  }

  .input:focus {
    outline: none;
    border-color: var(--accent, #6aa6ff);
    box-shadow: 0 0 0 2px var(--accent-soft, rgba(106, 166, 255, 0.18));
  }

  .input.error {
    border-color: #ff6b6b;
  }

  .error-msg {
    color: #ff6b6b;
    font-size: 0.85rem;
    display: block;
    margin-top: 0.25rem;
  }

  textarea.input {
    resize: vertical;
  }

  .actions {
    display: flex;
    gap: 1rem;
    margin-top: 2rem;
  }

  .btn {
    flex: 1;
    padding: 0.75rem;
    border: none;
    border-radius: var(--radius, 6px);
    font-size: 1rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s;
  }

  .btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .btn-primary {
    background: var(--accent, #6aa6ff);
    color: var(--bg);
  }

  .btn-primary:hover:not(:disabled) {
    background: #5088e0;
  }

  .btn-secondary {
    background: var(--bg, #0f1115);
    color: var(--text, #e9eef5);
    border: 1px solid var(--panel-border, rgba(255, 255, 255, 0.06));
  }

  .btn-secondary:hover:not(:disabled) {
    background: var(--panel-border, rgba(255, 255, 255, 0.1));
  }
</style>
