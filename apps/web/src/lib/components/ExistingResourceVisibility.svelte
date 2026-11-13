<script module lang="ts">
  export type TagDraft = { skills: string; goods: string; interests: string };

  const TAG = /^(?:(good|interest|skill):)?(.*)$/s;
  const entriesOf = (field: string) =>
    field
      .split(",")
      .map((entry) => entry.trim())
      .filter(Boolean);

  function withoutEntry(field: string, value: string): string {
    return entriesOf(field)
      .filter((entry) => entry !== value)
      .join(", ");
  }

  /** The field plus the values it does not list yet; its own entries stay. */
  function withEntries(field: string, values: string[]): string {
    return [...new Set([...entriesOf(field), ...entriesOf(values.join(","))])].join(
      ", ",
    );
  }

  /** User-facing text for a failed profile save (kept out of the initial chunk). */
  export function describeSaveError(error: unknown): string {
    switch ((error as { status?: number } | null)?.status) {
      case 401:
        return "Sitzung abgelaufen. Bitte melde dich neu an.";
      case 403:
        return "Dieses Konto darf die Garnrolle nicht bearbeiten.";
      case 400:
        return "Prüfe Name, Tags, Kartenanker und Sichtbarkeit.";
      case 409:
        return "Deine öffentlichen Tags haben sich geändert. Nichts wurde gespeichert. Prüfe sie und speichere erneut.";
      default:
        return "Speichern fehlgeschlagen. Versuche es erneut.";
    }
  }

  const FIELD_OF = { good: "goods", interest: "interests", skill: "skills" } as const;
  // Characters a single-line input drops or that mark a different tag.
  const UNREPRESENTABLE = /[\u0000-\u001f\u007f-\u009f\u2028\u2029]/;

  /**
   * Where a profile tag appears in the form. The form lists trimmed, comma
   * separated entries per field (no prefix = legacy tag, shown with the
   * skills), exactly as validateProfileTags reads them back. `entry` is the
   * trimmed value for a tag the form shows one-to-one, and null for a tag it
   * cannot (a comma splits it, control characters get lost): such a tag has
   * no entry that can be told apart from its siblings.
   */
  function slotOf(tag: string) {
    if (tag === "account" || tag === "garnrolle") return null;
    const [, kind, suffix] = TAG.exec(tag)!;
    const pieces = entriesOf(suffix);
    if (pieces.length === 0) return null;
    const field: keyof TagDraft = kind
      ? FIELD_OF[kind as keyof typeof FIELD_OF]
      : "skills";
    const single = !suffix.includes(",") && !UNREPRESENTABLE.test(suffix);
    return { field, pieces, entry: single ? pieces[0] : null };
  }

  const entryKey = (field: keyof TagDraft, entry: string) =>
    `${field}\u0000${entry}`;

  /**
   * Bring a draft that was based on `savedTags` up to the server's tag set.
   * Values the server dropped (e.g. moved to a private node) leave the draft,
   * values another session added join it; local edits and entries the user
   * deliberately removed stay. Entries are compared the way the form saves
   * them (trimmed), never by the raw tag text.
   *
   * A dropped tag the form cannot show one-to-one (comma, control characters)
   * cannot be located among the entries. That field is rebuilt from what the
   * server still publishes; every other entry of it is returned as `review`
   * and must be accepted explicitly before it may be saved.
   */
  export function reconcileTagDraft(
    savedTags: string[],
    serverTags: string[],
    draft: TagDraft,
  ): { fields: TagDraft; review: TagDraft | null } {
    const fields = { ...draft };
    const stillPublic = new Set<string>();
    for (const tag of serverTags) {
      const slot = slotOf(tag);
      if (slot?.entry) stillPublic.add(entryKey(slot.field, slot.entry));
    }
    const unsure = new Set<keyof TagDraft>();
    for (const tag of savedTags) {
      if (serverTags.includes(tag)) continue;
      const slot = slotOf(tag);
      if (!slot) continue;
      if (slot.entry === null) unsure.add(slot.field);
      // A remaining twin spelling of the same entry is still public anyway.
      else if (!stillPublic.has(entryKey(slot.field, slot.entry))) {
        fields[slot.field] = withoutEntry(fields[slot.field], slot.entry);
      }
    }
    const review = { skills: "", goods: "", interests: "" };
    for (const field of unsure) {
      const mine = entriesOf(fields[field]);
      const isPublic = (entry: string) => stillPublic.has(entryKey(field, entry));
      fields[field] = mine.filter(isPublic).join(", ");
      review[field] = mine.filter((entry) => !isPublic(entry)).join(", ");
    }
    for (const tag of serverTags) {
      const slot = savedTags.includes(tag) ? null : slotOf(tag);
      if (slot) fields[slot.field] = withEntries(fields[slot.field], slot.pieces);
    }
    return {
      fields,
      review:
        review.skills || review.goods || review.interests ? review : null,
    };
  }

  /** `draft` plus the review entries (explicitly accepted by the user). */
  export function adoptReviewedTags(draft: TagDraft, review: TagDraft) {
    return {
      skills: withEntries(draft.skills, [review.skills]),
      goods: withEntries(draft.goods, [review.goods]),
      interests: withEntries(draft.interests, [review.interests]),
    };
  }

  /**
   * Restore the tag fields of a stored draft against the freshly loaded
   * server profile. A draft names the tag set it was based on (`baseTags`),
   * so it can be reconciled. Without a trustworthy baseline (draft written
   * before it was recorded) nothing of it may reach the form: a moved private
   * value could return as public. The server state is shown instead and the
   * differing draft entries are returned for an explicit decision.
   */
  export function reconcileStoredDraft(
    draft: Partial<TagDraft> & { baseTags?: unknown },
    serverTags: string[],
    server: TagDraft,
  ): { fields: TagDraft; review: TagDraft | null } {
    const own = (key: keyof TagDraft) =>
      typeof draft[key] === "string" ? draft[key] : server[key];
    const stored = {
      skills: own("skills"),
      goods: own("goods"),
      interests: own("interests"),
    };
    const base = draft.baseTags;
    if (Array.isArray(base) && base.every((tag) => typeof tag === "string")) {
      return reconcileTagDraft(base, serverTags, stored);
    }
    const unknown = (key: keyof TagDraft) =>
      entriesOf(stored[key])
        .filter((entry) => !entriesOf(server[key]).includes(entry))
        .join(", ");
    const review = {
      skills: unknown("skills"),
      goods: unknown("goods"),
      interests: unknown("interests"),
    };
    const open = review.skills || review.goods || review.interests;
    return { fields: server, review: open ? review : null };
  }
</script>

<script lang="ts">
  /**
   * P2b: move ONE already saved public profile tag into its own node with its
   * own audience. Loaded lazily from MyGarnrolleSection so that neither the
   * resource-conversion API nor the audience-circle API belong to the initial
   * /settings chunk. Everything account-bound lives here and is dropped or
   * aborted when the account changes.
   */
  import { invalidateAll } from "$app/navigation";
  import {
    ApiRequestError,
    readErrorBody,
    type MoveProfileTagPayload,
    type MovedProfileTag,
    type ProfileResourceScope,
  } from "$lib/api/domainWrites";
  import { listMyCircles, type AudienceCircle } from "$lib/api/audienceCircles";
  import { createAccountRequestGuard } from "$lib/garnrolle/accountRequestGuard";
  import { onDestroy } from "svelte";

  interface Props {
    /** Active account. A change drops circles and aborts every request. */
    accountId: string;
    /** Public tags exactly as last confirmed by the server. */
    savedTags: string[];
    /** True while the profile is loading, saving, or belongs to no account. */
    disabled?: boolean;
    /** Reports a running move so the parent can block a concurrent save. */
    onBusyChange?: (busy: boolean) => void;
    /** Server tag set after a successful move. */
    onTagsMoved: (serverTags: string[]) => void | Promise<void>;
    /** The saved tag set is stale (409); the parent should reload it. */
    onStaleTags: () => void | Promise<void>;
    /** Tag entries of an older draft whose origin is unknown; saving waits. */
    review?: TagDraft | null;
    /** The user's explicit decision on `review`: keep its entries or not. */
    onResolveReview?: (merge?: (draft: TagDraft) => TagDraft) => void;
  }

  let {
    accountId,
    savedTags,
    disabled = false,
    onBusyChange,
    onTagsMoved,
    onStaleTags,
    review = null,
    onResolveReview,
  }: Props = $props();

  const MOVABLE_TAG_PREFIXES = ["good:", "skill:"] as const;
  const MAX_MOVE_CIRCLES = 8;
  const circleGuard = createAccountRequestGuard();
  const moveGuard = createAccountRequestGuard();

  let moveTag = $state("");
  let moveScope: ProfileResourceScope = $state("private");
  let moveCircleIds: string[] = $state([]);
  let moveCircles: AudienceCircle[] = $state([]);
  let moveCirclesError: string | null = $state(null);
  let isMoving = $state(false);
  let moveError: string | null = $state(null);
  let moveMessage: string | null = $state(null);
  // One deliberate action = one operation id. An uncertain result may be
  // retried with the same id; any change of tag or audience starts anew.
  let moveOperation: { key: string; id: string } | null = $state(null);
  let moveAbortController: AbortController | null = null;
  let circlesAbortController: AbortController | null = null;
  let circlesLoaded = false;
  let boundAccountId: string | null = null;

  /**
   * POST /api/accounts/me/profile/resources. Kept in this lazy component on
   * purpose: a function exported from the shared domainWrites module would be
   * bundled into the initial /settings chunk.
   */
  async function moveProfileTagToResource(
    payload: MoveProfileTagPayload,
    signal: AbortSignal,
  ): Promise<MovedProfileTag> {
    const res = await fetch("/api/accounts/me/profile/resources", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      credentials: "include",
      cache: "no-store",
      signal,
    });
    if (!res.ok) {
      throw new ApiRequestError(res.status, await readErrorBody(res));
    }
    return res.json();
  }

  function setMoving(value: boolean) {
    if (isMoving === value) return;
    isMoving = value;
    onBusyChange?.(value);
  }

  function abortRequests() {
    circleGuard.invalidate();
    moveGuard.invalidate();
    circlesAbortController?.abort();
    moveAbortController?.abort();
    circlesAbortController = null;
    moveAbortController = null;
  }

  // Never show circle names, selections or results of one account in another.
  $effect.pre(() => {
    const current = accountId;
    if (current === boundAccountId) return;
    boundAccountId = current;
    abortRequests();
    circlesLoaded = false;
    moveCircles = [];
    moveCirclesError = null;
    moveTag = "";
    moveScope = "private";
    moveCircleIds = [];
    moveOperation = null;
    moveError = null;
    moveMessage = null;
    setMoving(false);
  });

  onDestroy(() => {
    abortRequests();
    onBusyChange?.(false);
  });

  function movableValue(tag: string): string | null {
    for (const prefix of MOVABLE_TAG_PREFIXES) {
      if (tag.startsWith(prefix)) {
        const value = tag.slice(prefix.length).trim();
        return value || null;
      }
    }
    return null;
  }

  function movableTagLabel(tag: string): string {
    const value = movableValue(tag) ?? tag;
    return tag.startsWith("good:")
      ? `Gut: ${value}`
      : `Fähigkeit: ${value}`;
  }

  function isAbortError(error: unknown): boolean {
    return error instanceof Error && error.name === "AbortError";
  }

  function describeMoveError(error: unknown): string {
    if (error instanceof ApiRequestError) {
      switch (error.status) {
        case 401:
          return "Sitzung abgelaufen. Bitte melde dich neu an. Der Eintrag bleibt unverändert öffentlich.";
        case 404:
          return "Eine ausgewählte Gemeinschaft ist nicht (mehr) bestätigt. Nichts wurde geändert; der Eintrag bleibt öffentlich.";
        case 409:
          return "Der Eintrag ist nicht mehr gespeichert oder wurde inzwischen verändert. Nichts wurde zusätzlich veröffentlicht; prüfe die aktualisierten Tags.";
        case 429:
          return "Du hast die Höchstzahl eigener Knoten erreicht. Der Eintrag bleibt unverändert öffentlich.";
        case 400:
        case 503:
          return "Der Umzug ist derzeit nicht möglich. Nichts wurde geändert; der Eintrag bleibt öffentlich.";
      }
      if (!isUncertainMoveFailure(error)) {
        return "Der Umzug wurde abgelehnt. Nichts wurde geändert.";
      }
    }
    return "Unklar, ob der Umzug abgeschlossen wurde. „Erneut versuchen“ wiederholt genau dieselbe Aktion, ohne eine zweite Ressource anzulegen.";
  }

  /** Network errors and 5xx (except an explicit 503) may have committed. */
  function isUncertainMoveFailure(error: unknown): boolean {
    if (!(error instanceof ApiRequestError)) return true;
    return error.status >= 500 && error.status !== 503;
  }

  function scopeLabel(scope: string): string {
    return scope === "public"
      ? "öffentlich"
      : scope === "circles"
        ? "nur für bestätigte Gemeinschaft(en)"
        : "nur für dich";
  }

  function loadMoveCircles() {
    if (circlesLoaded || !accountId) return;
    circlesLoaded = true;
    moveCirclesError = null;
    circlesAbortController?.abort();
    const controller = new AbortController();
    circlesAbortController = controller;
    const request = circleGuard.begin(accountId);
    const isCurrent = () =>
      circlesAbortController === controller &&
      circleGuard.isCurrent(request, accountId);
    listMyCircles(controller.signal)
      .then((circles) => {
        if (!isCurrent()) return;
        moveCircles = circles;
        circlesAbortController = null;
      })
      .catch((error) => {
        if (!isCurrent() || isAbortError(error)) return;
        circlesAbortController = null;
        circlesLoaded = false;
        moveCirclesError = "Gemeinschaften konnten nicht geladen werden.";
      });
  }

  function toggleMoveCircle(id: string, checked: boolean) {
    moveCircleIds = checked
      ? [...moveCircleIds, id]
      : moveCircleIds.filter((value) => value !== id);
  }

  async function handleMove() {
    if (!canMove || !accountId) return;
    const requestAccountId = accountId;
    const tag = moveTag;
    const scope = moveScope;
    const circleIds = scope === "circles" ? [...moveCircleIds].sort() : [];
    const key = JSON.stringify([tag, scope, circleIds]);
    if (!moveOperation || moveOperation.key !== key) {
      moveOperation = { key, id: crypto.randomUUID() };
    }
    const operationId = moveOperation.id;
    moveAbortController?.abort();
    const controller = new AbortController();
    moveAbortController = controller;
    const request = moveGuard.begin(requestAccountId);
    const isCurrent = () =>
      moveAbortController === controller &&
      moveGuard.isCurrent(request, accountId);
    setMoving(true);
    moveError = null;
    moveMessage = null;
    try {
      const moved = await moveProfileTagToResource(
        {
          tag,
          scope,
          circle_ids: circleIds,
          operation_id: operationId,
        },
        controller.signal,
      );
      if (!isCurrent()) return;
      moveOperation = null;
      moveTag = "";
      moveScope = "private";
      moveCircleIds = [];
      moveMessage = `„${moved.node.title}“ ist jetzt eine eigene Ressource (${scopeLabel(moved.audience.scope)}) und kein öffentlicher Profil-Tag mehr.`;
      // The move is committed; a failing follow-up must never read as a
      // failed or unknown move.
      try {
        await onTagsMoved(moved.profile.tags);
        await invalidateAll();
      } catch {
        // Other views refresh on their next load.
      }
    } catch (error) {
      if (!isCurrent() || isAbortError(error)) return;
      if (!isUncertainMoveFailure(error)) moveOperation = null;
      moveError = describeMoveError(error);
      if (error instanceof ApiRequestError && error.status === 409) {
        await onStaleTags();
      }
    } finally {
      if (isCurrent()) {
        setMoving(false);
        moveAbortController = null;
      }
    }
  }

  let movableTags = $derived.by(() =>
    savedTags.filter((tag) => movableValue(tag) !== null),
  );
  let activeMoveCircles = $derived(
    moveCircles.filter(
      (circle) =>
        circle.state === "active" && circle.membership_status === "active",
    ),
  );
  let canMove = $derived.by(
    () =>
      !disabled &&
      !isMoving &&
      movableTags.includes(moveTag) &&
      (moveScope !== "circles" ||
        (moveCircleIds.length > 0 &&
          moveCircleIds.length <= MAX_MOVE_CIRCLES &&
          moveCircleIds.every((id) =>
            activeMoveCircles.some((circle) => circle.id === id),
          ))),
  );
  $effect(() => {
    if (moveScope === "circles" && accountId) loadMoveCircles();
  });
  // A tag the server no longer stores cannot stay selected.
  $effect(() => {
    if (moveTag && !movableTags.includes(moveTag)) moveTag = "";
  });
</script>

{#if review}
  <fieldset
    class="move-resource"
    data-testid="garnrolle-tag-review"
    disabled={disabled}
  >
    <legend>Tags prüfen</legend>
    <p class="field-intro" role="alert">
      Einträge deines Formulars lassen sich nicht eindeutig dem gespeicherten
      Stand zuordnen (älterer Entwurf oder umgezogener Tag). Sie wurden nicht
      übernommen, weil sie inzwischen privat verschoben sein können. Speichern
      und Kartenwahl sind gesperrt, bis du entscheidest. Übernommene Einträge
      werden mit dem nächsten Speichern öffentlich.
    </p>
    <ul>
      {#each [["Fähigkeiten", review.skills], ["Güter", review.goods], ["Interessen", review.interests]] as [label, value] (label)}
        {#if value}<li>{label}: {value}</li>{/if}
      {/each}
    </ul>
    <button
      type="button"
      class="btn"
      data-testid="garnrolle-tag-review-discard"
      onclick={() => onResolveReview?.()}
    >
      Verwerfen, gespeicherten Stand behalten
    </button>
    <button
      type="button"
      class="btn"
      data-testid="garnrolle-tag-review-adopt"
      onclick={() => onResolveReview?.((draft) => adoptReviewedTags(draft, review))}
    >
      Zusätzlich ins Formular übernehmen
    </button>
  </fieldset>
{/if}

{#if movableTags.length > 0 || moveMessage}
  <fieldset
    class="move-resource"
    data-testid="garnrolle-move-resource"
    disabled={disabled || isMoving}
  >
    <legend>Gespeicherten Eintrag einzeln schützen</legend>
    <p class="field-intro">
      Wähle genau einen bereits gespeicherten öffentlichen Eintrag. Er
      verschwindet aus deiner öffentlichen Garnrolle und wird ein eigener Knoten
      mit der gewählten Sichtbarkeit. Alle anderen Angaben, auch ungespeicherte
      Eingaben, bleiben unverändert.
    </p>
    {#if movableTags.length > 0}
      <label>
        Gespeicherter Eintrag
        <select
          bind:value={moveTag}
          data-testid="garnrolle-move-tag"
          onchange={() => {
            moveError = null;
            moveMessage = null;
          }}
        >
          <option value="">– auswählen –</option>
          {#each movableTags as tag (tag)}
            <option value={tag}>{movableTagLabel(tag)}</option>
          {/each}
        </select>
      </label>
      <label>
        Sichtbarkeit der Ressource
        <select bind:value={moveScope} data-testid="garnrolle-move-scope">
          <option value="private">Nur ich</option>
          <option value="circles">Nur bestätigte Gemeinschaft(en)</option>
          <option value="public">Öffentlich (als eigener Knoten)</option>
        </select>
      </label>
      {#if moveScope === "circles"}
        <fieldset class="move-circles">
          <legend>Bestätigte Gemeinschaften</legend>
          {#each activeMoveCircles as circle (circle.id)}
            <label class="checkbox">
              <input
                type="checkbox"
                checked={moveCircleIds.includes(circle.id)}
                disabled={!moveCircleIds.includes(circle.id) &&
                  moveCircleIds.length >= MAX_MOVE_CIRCLES}
                onchange={(event) =>
                  toggleMoveCircle(circle.id, event.currentTarget.checked)}
              />
              {circle.name}
            </label>
          {:else}
            <small class="field-intro">
              {moveCirclesError ??
                "Keine bestätigte aktive Gemeinschaft vorhanden."}
            </small>
            {#if moveCirclesError}
              <button
                type="button"
                class="btn"
                data-testid="garnrolle-move-circles-retry"
                onclick={loadMoveCircles}
              >
                Gemeinschaften erneut laden
              </button>
            {/if}
          {/each}
        </fieldset>
      {/if}
      <p class="field-intro" data-testid="garnrolle-move-cache-notice">
        Was bisher öffentlich war, kann bereits gesehen, kopiert oder fremd
        zwischengespeichert worden sein. Solche Kopien kann Commonthing nicht
        zurückholen.
      </p>
      <button
        type="button"
        class="btn"
        data-testid="garnrolle-move-submit"
        disabled={!canMove}
        onclick={handleMove}
      >
        {moveOperation && moveError
          ? "Erneut versuchen"
          : "Als eigene Ressource umziehen"}
      </button>
    {/if}
    {#if moveError}
      <p
        class="form-message error"
        role="alert"
        data-testid="garnrolle-move-error"
      >
        {moveError}
      </p>
    {/if}
    {#if moveMessage}
      <p
        class="form-message"
        role="status"
        data-testid="garnrolle-move-success"
      >
        {moveMessage}
      </p>
    {/if}
  </fieldset>
{/if}

<style>
  fieldset {
    border: 1px solid var(--panel-border, rgba(255, 255, 255, 0.08));
    border-radius: 12px;
    display: grid;
    gap: 0.9rem;
    margin: 0;
    padding: 1rem;
  }

  legend {
    font-weight: 700;
    padding: 0 0.35rem;
  }

  label {
    display: grid;
    gap: 0.35rem;
    font-weight: 600;
  }

  input {
    background: var(--bg, #0f1115);
    border: 1px solid var(--panel-border, rgba(255, 255, 255, 0.12));
    border-radius: 8px;
    color: var(--text, #e9eef5);
    font: inherit;
    padding: 0.7rem;
  }

  .field-intro {
    color: var(--muted, #9aa4b2);
    font-size: 0.92rem;
    margin: 0;
  }

  .move-circles .checkbox {
    display: flex;
    gap: 0.5rem;
    align-items: center;
  }

  .form-message {
    border-radius: 8px;
    margin: 0;
    padding: 0.8rem 1rem;
  }

  .form-message.error {
    background: rgba(255, 107, 107, 0.12);
    border: 1px solid #ff6b6b;
  }

  .btn {
    align-items: center;
    background: var(--panel-border, rgba(255, 255, 255, 0.08));
    border: 1px solid var(--panel-border, rgba(255, 255, 255, 0.12));
    border-radius: 8px;
    color: var(--text, #e9eef5);
    cursor: pointer;
    display: inline-flex;
    font-weight: 700;
    justify-content: center;
    min-height: 42px;
    padding: 0 0.9rem;
    text-decoration: none;
  }

  .btn:focus-visible,
  input:focus-visible {
    outline: 3px solid var(--accent, #6aa6ff);
    outline-offset: 2px;
  }

  .btn:disabled {
    cursor: not-allowed;
    opacity: 0.55;
  }
</style>
