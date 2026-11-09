<script lang="ts">
  import { browser } from "$app/environment";
  import { goto, invalidateAll } from "$app/navigation";
  import { authStore } from "$lib/auth/store";
  import { sensitiveSession } from "$lib/auth/sensitiveSession";
  import {
    rememberGarnrolleDraft,
    readGarnrolleDraft,
    takeGarnrolleLocation,
    clearGarnrolleHandoff,
  } from "$lib/garnrolle/locationHandoff";
  import {
    ApiRequestError,
    getOwnGarnrolleProfile,
    updateOwnGarnrolle,
    type OwnGarnrolleProfile,
  } from "$lib/api/domainWrites";
  import type { Account, GarnrolleMapState, Location } from "$lib/map/types";
  import { createAccountRequestGuard } from "$lib/garnrolle/accountRequestGuard";
  import {
    countUnicodeCodePoints,
    validateProfileTags,
  } from "$lib/garnrolle/codePointInput";
  import {
    describeGarnrolleVisibility,
    findOwnGarnrolle,
  } from "$lib/garnrolle/visibility";
  import { onDestroy, tick } from "svelte";
  // Type only: the editor itself is imported dynamically after an explicit
  // click so the conversion and circle APIs stay out of the initial chunk.
  import type ExistingResourceVisibility from "./ExistingResourceVisibility.svelte";
  import type { TagDraft } from "./ExistingResourceVisibility.svelte";

  interface Props {
    accounts?: Account[];
    accountsLoadError?: string | null;
  }

  let { accounts = [], accountsLoadError = null }: Props = $props();

  type GarnrolleDraft = {
    displayName: string;
    summary: string;
    skills: string;
    goods: string;
    interests: string;
    address: string;
    addressTouched: boolean;
    visibilityChoice: GarnrolleMapState;
    radiusM: number;
    selectedLocation: Location | null;
    clearLocation: boolean;
    /** Server-confirmed public tags the tag fields were based on. */
    baseTags: string[];
  };

  const DISPLAY_NAME_MAX_CODE_POINTS = 200;
  const SUMMARY_MAX_CODE_POINTS = 500;
  const accountRequestGuard = createAccountRequestGuard();
  const saveRequestGuard = createAccountRequestGuard();
  let loadAbortController: AbortController | null = null;
  let saveAbortController: AbortController | null = null;
  let profileKey = $state("");
  let loadedProfileAccountId: string | null = $state(null);
  let displayName = $state("");
  let summary = $state("");
  let skills = $state("");
  let goods = $state("");
  let interests = $state("");
  let address = $state("");
  let initialAddress = "";
  let addressTouched = $state(false);
  let visibilityChoice: GarnrolleMapState = $state("not_on_map");
  let radiusM = $state(250);
  let selectedLocation: Location | null = $state(null);
  let clearLocation = false;
  let isLoadingProfile = $state(false);
  let isSaving = $state(false);
  let profileError: string | null = $state(null);
  let draftMessage: string | null = $state(null);
  let saveMessage: string | null = $state(null);
  let locationButton: HTMLButtonElement | null = $state(null);
  // Public tags exactly as last confirmed by the server. Only these may be
  // moved, and every save names them as its precondition (expected_tags).
  let savedTags: string[] = $state([]);
  let isMoving = $state(false);
  let MoveEditor: typeof ExistingResourceVisibility | null = $state.raw(null);
  let moveEditorOpen = $state(false);
  let moveEditorError = $state(false);
  // Entries of an older draft without a known baseline, awaiting a decision.
  let tagReview: TagDraft | null = $state(null);
  // Bumped on every account reset; stale async tag work compares against it.
  let epoch = 0;
  const MOVABLE_TAG = /^(good|skill):\s*\S/;
  // One import site: each dynamic import() carries its own preload list.
  const loadEditorModule = () => import("./ExistingResourceVisibility.svelte");

  function categoryValues(tags: string[], prefix: string): string[] {
    return tags
      .filter((tag) => tag.startsWith(prefix))
      .map((tag) => tag.slice(prefix.length))
      .filter(Boolean);
  }

  function profileSkills(tags: string[]): string[] {
    const prefixed = categoryValues(tags, "skill:");
    const legacy = tags.filter(
      (tag) => !/^(account$|garnrolle$|(skill|good|interest):)/.test(tag),
    );
    return [...new Set([...prefixed, ...legacy])];
  }

  function draftStorageKey(accountId: string): string {
    return `weltgewebe:garnrolle-draft:${accountId}`;
  }

  function returnLocationStorageKey(accountId: string): string {
    return `weltgewebe:garnrolle-return-location:${accountId}`;
  }

  function clearStoredPrivateDraft(accountId: string) {
    if (!browser) return;
    sensitiveSession.remove(accountId, draftStorageKey(accountId));
    sensitiveSession.remove(accountId, returnLocationStorageKey(accountId));
    clearGarnrolleHandoff(accountId);
  }

  function invalidateAccountOperations() {
    accountRequestGuard.invalidate();
    saveRequestGuard.invalidate();
    loadAbortController?.abort();
    saveAbortController?.abort();
    loadAbortController = null;
    saveAbortController = null;
  }

  onDestroy(invalidateAccountOperations);

  function currentDraft(): GarnrolleDraft {
    return {
      displayName,
      summary,
      skills,
      goods,
      interests,
      address,
      addressTouched,
      visibilityChoice,
      radiusM,
      selectedLocation,
      clearLocation,
      baseTags: savedTags,
    };
  }

  function applyDraft(draft: Partial<GarnrolleDraft>) {
    if (typeof draft.displayName === "string") displayName = draft.displayName;
    if (typeof draft.summary === "string") summary = draft.summary;
    if (typeof draft.skills === "string") skills = draft.skills;
    if (typeof draft.goods === "string") goods = draft.goods;
    if (typeof draft.interests === "string") interests = draft.interests;
    if (typeof draft.address === "string") {
      address = draft.address;
      addressTouched =
        typeof draft.addressTouched === "boolean"
          ? draft.addressTouched
          : draft.address.trim() !== initialAddress;
    }
    if (
      draft.visibilityChoice === "not_on_map" ||
      draft.visibilityChoice === "exact" ||
      draft.visibilityChoice === "radius"
    ) {
      visibilityChoice = draft.visibilityChoice;
    }
    if (typeof draft.radiusM === "number" && Number.isFinite(draft.radiusM)) {
      radiusM = draft.radiusM;
    }
    if (
      draft.selectedLocation === null ||
      (typeof draft.selectedLocation?.lat === "number" &&
        typeof draft.selectedLocation?.lon === "number")
    ) {
      selectedLocation = draft.selectedLocation ?? null;
    }
    if (typeof draft.clearLocation === "boolean") {
      clearLocation = draft.clearLocation;
    }
  }

  function resetDraft() {
    displayName = "";
    summary = "";
    skills = "";
    goods = "";
    interests = "";
    address = "";
    initialAddress = "";
    addressTouched = false;
    visibilityChoice = "not_on_map";
    radiusM = 250;
    selectedLocation = null;
    clearLocation = false;
    profileError = null;
    draftMessage = null;
    saveMessage = null;
    savedTags = [];
    // Closing unmounts the editor, which aborts its account-bound requests.
    moveEditorOpen = false;
    moveEditorError = false;
    isMoving = false;
    tagReview = null;
    epoch++;
  }

  const tagFields = (): TagDraft => ({ skills, goods, interests });
  const setTagFields = (fields: TagDraft) =>
    ({ skills, goods, interests } = fields);

  /**
   * Adopt a newer server tag set without discarding unrelated unsaved edits:
   * values the server dropped leave the draft fields, values another session
   * added join them. The pure merge lives in the lazy editor module;
   * savedTags (= expected_tags of the next save) only advances once the merge
   * is applied, so a failed load (callers catch it) keeps saving blocked by a
   * 409 instead of republishing a moved value or deleting a foreign one.
   */
  async function adoptServerTags(serverTags: string[]) {
    const at = epoch;
    const { reconcileTagDraft } = await loadEditorModule();
    if (at !== epoch) return;
    setTagFields(reconcileTagDraft(savedTags, serverTags, tagFields()));
    savedTags = [...serverTags];
  }

  function applyProfile(profile: OwnGarnrolleProfile) {
    savedTags = [...profile.tags];
    displayName = profile.title;
    summary = profile.summary ?? "";
    skills = profileSkills(profile.tags).join(", ");
    goods = categoryValues(profile.tags, "good:").join(", ");
    interests = categoryValues(profile.tags, "interest:").join(", ");
    initialAddress = profile.address?.trim() ?? "";
    address = initialAddress;
    addressTouched = false;
    visibilityChoice = profile.map_state;
    radiusM = profile.radius_m > 0 ? profile.radius_m : 250;
    selectedLocation = profile.location ?? null;
    clearLocation = false;
  }

  function returnedMapLocation(accountId: string): Location | null {
    if (!browser) return null;
    const stored = sensitiveSession.take(
      accountId,
      returnLocationStorageKey(accountId),
    );
    if (!stored) return null;
    try {
      const value = JSON.parse(stored) as Partial<Location>;
      const { lat, lon } = value;
      // Math.abs is false for NaN and +-Infinity; the type check rules out
      // numeric strings.
      return typeof lat === "number" &&
        typeof lon === "number" &&
        Math.abs(lat) <= 90 &&
        Math.abs(lon) <= 180
        ? { lat, lon }
        : null;
    } catch {
      return null;
    }
  }

  function isAbortError(error: unknown): boolean {
    return error instanceof Error && error.name === "AbortError";
  }

  async function loadPrivateProfile(accountId: string) {
    loadAbortController?.abort();
    const controller = new AbortController();
    loadAbortController = controller;
    const request = accountRequestGuard.begin(accountId);
    const isCurrentRequest = () =>
      loadAbortController === controller &&
      accountRequestGuard.isCurrent(request, activeAccountId);
    let focusReturnedLocation = false;
    loadedProfileAccountId = null;
    isLoadingProfile = true;
    profileError = null;
    draftMessage = null;
    saveMessage = null;
    try {
      const profile = await getOwnGarnrolleProfile(controller.signal);
      if (!isCurrentRequest()) return;
      if (profile.id !== accountId) {
        throw new Error("profile-account-mismatch");
      }
      applyProfile(profile);

      if (browser) {
        const stored =
          readGarnrolleDraft(accountId) ??
          sensitiveSession.read(accountId, draftStorageKey(accountId));
        let draft: Partial<GarnrolleDraft> | null = null;
        try {
          draft = JSON.parse(stored ?? "null");
        } catch {
          sensitiveSession.remove(accountId, draftStorageKey(accountId));
        }
        if (draft) {
          // Only a draft based on exactly the current tags is taken as is.
          // Anything else is reconciled first; a failing import leaves the
          // load failed (form stays disabled), never a stale draft applied.
          if (JSON.stringify(draft.baseTags) !== JSON.stringify(profile.tags)) {
            const lazy = await loadEditorModule();
            if (!isCurrentRequest()) return;
            const { fields, review } = lazy.reconcileStoredDraft(
              draft,
              profile.tags,
              tagFields(),
            );
            draft = { ...draft, ...fields };
            if (review) {
              tagReview = review;
              MoveEditor = lazy.default;
              moveEditorOpen = true;
            }
          }
          applyDraft(draft);
        }
      }
      // Prefer the most recent same-tab touch choice. A denied storage write
      // may leave an older return-location key behind in sessionStorage.
      // Always consume the stored return key, even if the fresh same-tab
      // selection wins. Otherwise a later cancel could resurrect this point.
      const storedReturn = returnedMapLocation(accountId);
      const returned = takeGarnrolleLocation(accountId) ?? storedReturn;
      if (returned) {
        selectedLocation = returned;
        clearLocation = false;
        draftMessage =
          "Privater Kartenanker übernommen, aber noch nicht gespeichert. Wähle nun die öffentliche Sichtbarkeit und speichere deine Garnrolle.";
        focusReturnedLocation = true;
      }
      loadedProfileAccountId = accountId;
    } catch (error) {
      if (!isCurrentRequest() || isAbortError(error)) return;
      loadedProfileAccountId = null;
      if (error instanceof ApiRequestError && error.status === 401) {
        profileError = "Sitzung abgelaufen. Bitte melde dich neu an.";
      } else {
        profileError = "Laden fehlgeschlagen. Bitte lade die Seite neu.";
      }
    } finally {
      if (isCurrentRequest()) {
        isLoadingProfile = false;
        loadAbortController = null;
        if (focusReturnedLocation) {
          await tick();
          if (
            accountRequestGuard.isCurrent(request, activeAccountId) &&
            loadedProfileAccountId === accountId
          ) {
            locationButton?.focus();
          }
        }
      }
    }
  }

  function saveDraftForMap(): boolean {
    if (!browser || !activeAccountId) return false;
    if (tagReview) return false;
    const serialized = JSON.stringify(currentDraft());
    // Normal sessionStorage persists through reloads. When Safari refuses
    // writes, an account-bound in-memory copy still supports this same-tab flow.
    const memoryReady = rememberGarnrolleDraft(activeAccountId, serialized);
    const saved = sensitiveSession.write(
      activeAccountId,
      draftStorageKey(activeAccountId),
      serialized,
    );
    if (!saved && !memoryReady) {
      profileError =
        "Privater Entwurf konnte nicht für die Kartenwahl gesichert werden. Bitte melde dich erneut an.";
    }
    return saved || memoryReady;
  }

  async function chooseMapLocation() {
    clearLocation = false;
    draftMessage = null;
    saveMessage = null;
    profileError = null;
    if (!saveDraftForMap()) return;
    await goto("/map?compose=garnrolle");
  }

  async function createScopedResource() {
    // Reuse the canonical Knoten editor and its owner/circle ACL.
    // Preserve an unsaved Garnrolle draft without publishing profile tags.
    profileError = null;
    if (!saveDraftForMap()) return;
    await goto("/map?compose=node");
  }

  async function removeMapLocation() {
    selectedLocation = null;
    clearLocation = true;
    visibilityChoice = "not_on_map";
    profileError = null;
    saveMessage = null;
    draftMessage =
      "Kartenanker wird entfernt; Sichtbarkeit auf „Privat“ gesetzt. Noch nicht gespeichert.";
    await tick();
    locationButton?.focus();
  }

  /** Reload only the server's tag set after a conflict; keep other drafts. */
  async function refreshSavedTags() {
    const at = epoch;
    try {
      const profile = await getOwnGarnrolleProfile();
      if (at !== epoch || profile.id !== activeAccountId) return;
      await adoptServerTags(profile.tags);
    } catch {
      // The conflict message already asks for a deliberate reload.
    }
  }

  /** The user decided; `merge` (if any) takes the reviewed entries over. */
  function resolveTagReview(merge?: (draft: TagDraft) => TagDraft) {
    if (merge) setTagFields(merge(tagFields()));
    // A stored older draft is reviewed again after a reload, never applied.
    tagReview = null;
  }

  async function toggleMoveEditor() {
    moveEditorOpen = !moveEditorOpen;
    moveEditorError = false;
    if (!moveEditorOpen || MoveEditor) return;
    try {
      MoveEditor = (await loadEditorModule()).default;
    } catch {
      moveEditorOpen = false;
      moveEditorError = true;
    }
  }

  async function handleSave(event: SubmitEvent) {
    event.preventDefault();
    if (!canSave || !activeAccountId || !profileTags) return;

    profileError = null;
    draftMessage = null;
    saveMessage = null;
    const savingAccountId = activeAccountId;
    saveAbortController?.abort();
    const controller = new AbortController();
    saveAbortController = controller;
    const request = saveRequestGuard.begin(savingAccountId);
    const isCurrentSave = () =>
      saveAbortController === controller &&
      saveRequestGuard.isCurrent(request, activeAccountId) &&
      loadedProfileAccountId === savingAccountId;
    const normalizedAddress = address.trim();
    const addressChanged = normalizedAddress !== initialAddress;
    const addressPatch =
      addressTouched && addressChanged
        ? normalizedAddress
          ? { address: normalizedAddress }
          : { clear_address: true }
        : {};

    isSaving = true;
    try {
      const savedProfile = await updateOwnGarnrolle(
        {
          title: displayName.trim(),
          summary: summary.trim() || undefined,
          tags: profileTags,
          ...addressPatch,
          location: selectedLocation ?? undefined,
          ...(clearLocation ? { clear_location: true } : {}),
          map_state: visibilityChoice,
          radius_m: visibilityChoice === "radius" ? radiusM : undefined,
          expected_tags: savedTags,
        },
        controller.signal,
      );
      if (!isCurrentSave()) return;

      applyProfile(savedProfile);
      clearStoredPrivateDraft(savingAccountId);
      await invalidateAll();
      if (!isCurrentSave()) return;

      saveMessage = "Garnrolle gespeichert. Die Sichtbarkeit bleibt änderbar.";
      if (!isCurrentSave()) return;
      await goto("/settings#meine-garnrolle", {
        replaceState: true,
        noScroll: true,
        keepFocus: true,
      });
    } catch (error) {
      if (isCurrentSave() && !isAbortError(error)) {
        // The status texts live in the lazy module; a failed load of it
        // still reports the generic failure.
        let message = "Speichern fehlgeschlagen. Versuche es erneut.";
        try {
          message = (await loadEditorModule()).describeSaveError(error);
        } catch {
          // keep the generic message
        }
        if (isCurrentSave()) {
          profileError = message;
          if (error instanceof ApiRequestError && error.status === 409) {
            await refreshSavedTags();
          }
        }
      }
    } finally {
      if (isCurrentSave()) {
        isSaving = false;
        saveAbortController = null;
      }
    }
  }
  let ownGarnrolle = $derived.by(() =>
    findOwnGarnrolle(accounts, $authStore.account_id),
  );
  let visibility = $derived.by(() => describeGarnrolleVisibility(ownGarnrolle));
  let activeAccountId = $derived.by(() =>
    $authStore.authenticated && $authStore.account_id
      ? $authStore.account_id
      : null,
  );
  let radiusIsValid = $derived.by(
    () => Number.isInteger(radiusM) && radiusM >= 50 && radiusM <= 5000,
  );
  let displayNameCodePointCount = $derived.by(() =>
    countUnicodeCodePoints(displayName.trim()),
  );
  let displayNameTooLong = $derived.by(
    () => displayNameCodePointCount > DISPLAY_NAME_MAX_CODE_POINTS,
  );
  let summaryCodePointCount = $derived.by(() =>
    countUnicodeCodePoints(summary.trim()),
  );
  let summaryTooLong = $derived.by(
    () => summaryCodePointCount > SUMMARY_MAX_CODE_POINTS,
  );
  let profileTags = $derived.by(() =>
    validateProfileTags([skills, goods, interests]),
  );
  $effect.pre(() => {
    if (activeAccountId && activeAccountId !== profileKey) {
      const previousAccountId = profileKey;
      if (previousAccountId) clearStoredPrivateDraft(previousAccountId);
      profileKey = activeAccountId;
      invalidateAccountOperations();
      loadedProfileAccountId = null;
      isLoadingProfile = false;
      isSaving = false;
      resetDraft();
      void loadPrivateProfile(activeAccountId);
    }
  });
  $effect.pre(() => {
    if (!activeAccountId && profileKey) {
      const previousAccountId = profileKey;
      profileKey = "";
      clearStoredPrivateDraft(previousAccountId);
      invalidateAccountOperations();
      loadedProfileAccountId = null;
      isLoadingProfile = false;
      isSaving = false;
      resetDraft();
    }
  });
  let formDisabled = $derived.by(
    () =>
      isLoadingProfile ||
      isSaving ||
      isMoving ||
      loadedProfileAccountId !== activeAccountId,
  );
  let canSave = $derived.by(
    () =>
      !formDisabled &&
      !tagReview &&
      !!displayName.trim() &&
      !displayNameTooLong &&
      !summaryTooLong &&
      !!profileTags &&
      (visibilityChoice === "not_on_map" || selectedLocation !== null) &&
      (visibilityChoice !== "radius" || radiusIsValid),
  );
</script>

<section
  id="meine-garnrolle"
  class="my-garnrolle"
  data-testid="my-garnrolle-section"
>
  <div class="section-head">
    <div>
      <p class="eyebrow">Meine Garnrolle</p>
      <h2>Deine Garnrolle ist dein Anfang im Gewebe.</h2>
    </div>
    <span class="state-pill" data-state={visibility.state}
      >{visibility.label}</span
    >
  </div>

  {#if !$authStore.authenticated}
    <div class="empty-card" data-testid="my-garnrolle-anonymous">
      <p>
        Bei der Registrierung entsteht deine Garnrolle. Nach dem Login kannst du
        sie hier einrichten.
      </p>
      <a class="btn btn-primary" href="/login">Login starten</a>
    </div>
  {:else}
    <div class="status-card" data-testid="my-garnrolle-status">
      <div>
        <strong>{visibility.label}</strong>
        <p>{visibility.description}</p>
        {#if accountsLoadError}
          <p class="warn">
            Öffentliche Ansicht fehlt: {accountsLoadError} Das private Profil bleibt
            bearbeitbar.
          </p>
        {:else if !ownGarnrolle}
          <p class="warn">
            Garnrollen-Datensatz fehlt; das private Profil bleibt bearbeitbar.
          </p>
        {/if}
      </div>
      <a
        class="btn"
        href={ownGarnrolle?.public_pos
          ? `/map?focus=garnrolle:${ownGarnrolle.id}`
          : "/map"}
        data-testid="my-garnrolle-map-link"
      >
        {visibility.canZoomToMap ? "Auf Karte zeigen" : "Karte öffnen"}
      </a>
    </div>

    <form
      class="garnrolle-form"
      aria-describedby="my-garnrolle-save-note"
      onsubmit={handleSave}
    >
      <fieldset disabled={formDisabled}>
        <legend>1. Garnrolle beschreiben</legend>
        <p class="field-intro">
          Nur der Anzeigename ist erforderlich. Den Rest kannst du ergänzen.
        </p>
        <label>
          Anzeigename
          <input
            bind:value={displayName}
            placeholder="Meine Garnrolle"
            aria-invalid={displayNameTooLong}
            aria-describedby="garnrolle-display-name-length"
            required
          />
          <small
            id="garnrolle-display-name-length"
            class={displayNameTooLong
              ? "field-intro form-message error"
              : "field-intro"}
            role={displayNameTooLong ? "alert" : undefined}
          >
            Anzeigename: {displayNameCodePointCount}/{DISPLAY_NAME_MAX_CODE_POINTS}
            Unicode-Zeichen{displayNameTooLong
              ? " – bitte vor dem Speichern kürzen"
              : ""}.
          </small>
        </label>
        <label>
          Kurzbeschreibung
          <textarea
            bind:value={summary}
            rows="3"
            aria-invalid={summaryTooLong}
            aria-describedby="garnrolle-summary-length"
            placeholder="Was bringst du ins Gewebe ein?"></textarea>
          <small
            id="garnrolle-summary-length"
            class={summaryTooLong
              ? "field-intro form-message error"
              : "field-intro"}
            role={summaryTooLong ? "alert" : undefined}
          >
            Kurzbeschreibung: {summaryCodePointCount}/{SUMMARY_MAX_CODE_POINTS}
            Unicode-Zeichen{summaryTooLong
              ? " – bitte vor dem Speichern kürzen"
              : ""}.
          </small>
        </label>
        <label>
          Fähigkeiten
          <input
            bind:value={skills}
            aria-invalid={!profileTags}
            aria-describedby={!profileTags ? "garnrolle-tags-help" : undefined}
            placeholder="z. B. Holzbau, Kochen"
          />
        </label>
        <label>
          Güter
          <input
            bind:value={goods}
            aria-invalid={!profileTags}
            aria-describedby={!profileTags ? "garnrolle-tags-help" : undefined}
            placeholder="z. B. Werkzeug, Lastenrad"
          />
        </label>
        <label>
          Interessen
          <input
            bind:value={interests}
            aria-invalid={!profileTags}
            aria-describedby={!profileTags ? "garnrolle-tags-help" : undefined}
            placeholder="z. B. Nachbarschaft, Commons"
          />
        </label>
        {#if !profileTags}
          <small
            id="garnrolle-tags-help"
            class="form-message error"
            role="alert"
          >
            Maximal 62 Tags; je 64 Unicode-Zeichen samt Präfix.
          </small>
        {/if}
        <p class="field-intro" data-testid="garnrolle-public-tags-notice">
          Diese Profilangaben sind öffentlich, auch bei privatem Kartenanker.
          Soll ein einzelnes Gut oder eine Fähigkeit nur für dich oder eine
          Gemeinschaft sichtbar sein, knüpfe dafür einen Knoten. Trage die
          private Ressource nicht zusätzlich hier ein.
        </p>
        <button
          type="button"
          class="btn"
          data-testid="garnrolle-private-resource-link"
          onclick={createScopedResource}
        >
          Einzelne Ressource mit eigener Sichtbarkeit knüpfen
        </button>
        {#if moveEditorOpen || savedTags.some((tag) => MOVABLE_TAG.test(tag))}
          <button
            type="button"
            class="btn"
            data-testid="garnrolle-move-open"
            aria-expanded={moveEditorOpen}
            disabled={isMoving || !!tagReview}
            onclick={toggleMoveEditor}
          >
            Gespeicherte Güter/Fähigkeiten mit eigener Sichtbarkeit verwalten
          </button>
        {/if}
        {#if moveEditorError}
          <p class="field-intro" role="alert">
            Der Editor konnte nicht geladen werden.
          </p>
        {/if}
        {#if moveEditorOpen && MoveEditor && activeAccountId}
          <MoveEditor
            accountId={activeAccountId}
            {savedTags}
            disabled={formDisabled}
            onBusyChange={(busy) => (isMoving = busy)}
            onTagsMoved={adoptServerTags}
            onStaleTags={refreshSavedTags}
            review={tagReview}
            onResolveReview={resolveTagReview}
          />
        {:else if moveEditorOpen}
          <p class="field-intro" role="status">Lade Editor…</p>
        {/if}
      </fieldset>

      <fieldset disabled={formDisabled}>
        <legend>2. Privaten Kartenanker wählen</legend>
        <p class="field-intro">
          Der Kartenanker ist zunächst privat. Erst deine Auswahl im nächsten
          Schritt bestimmt, ob und wie daraus eine öffentliche Position wird.
        </p>

        <div class="location-card" data-testid="garnrolle-location-state">
          {#if selectedLocation}
            <div>
              <strong>Privater Kartenanker gewählt</strong>
              <small>
                {visibilityChoice === "not_on_map"
                  ? "Der Punkt bleibt unsichtbar."
                  : "Grundlage deiner öffentlichen Darstellung."}
              </small>
            </div>
            <div class="actions">
              <button
                bind:this={locationButton}
                type="button"
                class="btn"
                onclick={chooseMapLocation}
              >
                Punkt ändern
              </button>
              <button type="button" class="btn" onclick={removeMapLocation}>
                Kartenanker entfernen
              </button>
            </div>
          {:else}
            <div>
              <strong>Noch kein Kartenanker gewählt</strong>
              <small>
                Wähle den Kartenpunkt. Die Adresse setzt keine Position.
              </small>
            </div>
            <button
              bind:this={locationButton}
              type="button"
              class="btn btn-primary"
              onclick={chooseMapLocation}
              data-testid="choose-garnrolle-location"
            >
              Punkt auf Karte wählen
            </button>
          {/if}
        </div>

        <label>
          Adresse oder Ortsnotiz <span class="optional">privat, optional</span>
          <input
            bind:value={address}
            oninput={() => (addressTouched = true)}
            maxlength="500"
            placeholder="z. B. Stadtteil"
          />
        </label>
      </fieldset>

      <fieldset disabled={formDisabled}>
        <legend>3. Öffentliche Sichtbarkeit wählen</legend>
        <p class="field-intro">Jederzeit änderbar.</p>
        <div class="radio-group" role="radiogroup" aria-label="Sichtbarkeit">
          <label class="radio-card">
            <input
              type="radio"
              bind:group={visibilityChoice}
              value="not_on_map"
            />
            <span>
              <strong>Privat – nicht öffentlich auf der Karte</strong>
              <small>Der Kartenanker bleibt gespeichert und unsichtbar.</small>
            </span>
          </label>
          <label class="radio-card">
            <input type="radio" bind:group={visibilityChoice} value="radius" />
            <span>
              <strong>Öffentlich ungefähr</strong>
              <small>Versetzte Position im gewählten Umkreis.</small>
            </span>
          </label>
          <label class="radio-card">
            <input type="radio" bind:group={visibilityChoice} value="exact" />
            <span>
              <strong>Öffentlich exakt</strong>
              <small>Der Kartenanker wird genau gezeigt.</small>
            </span>
          </label>
        </div>

        {#if visibilityChoice !== "not_on_map" && !selectedLocation}
          <p class="form-message hint" role="status">
            Für diese Sichtbarkeit fehlt ein Kartenanker.
          </p>
        {/if}

        {#if visibilityChoice === "radius"}
          <label>
            Ungefährer Umkreis in Metern
            <input
              type="number"
              min="50"
              max="5000"
              step="1"
              bind:value={radiusM}
              aria-invalid={!radiusIsValid}
              aria-describedby="garnrolle-radius-help"
            />
          </label>
          <p
            id="garnrolle-radius-help"
            class={radiusIsValid
              ? "field-intro"
              : "field-intro form-message error"}
          >
            {radiusIsValid
              ? "Erlaubt sind 50 bis 5.000 Meter."
              : "Bitte wähle einen Umkreis zwischen 50 und 5.000 Metern."}
          </p>
        {/if}
      </fieldset>

      {#if draftMessage}
        <p
          class="form-message hint"
          role="status"
          data-testid="garnrolle-draft-status"
        >
          {draftMessage}
        </p>
      {/if}
      {#if profileError}
        <p
          class="form-message error"
          role="alert"
          data-testid="garnrolle-error"
        >
          {profileError}
        </p>
      {/if}
      {#if saveMessage}
        <p
          class="form-message success"
          role="status"
          data-testid="garnrolle-success"
        >
          {saveMessage}
        </p>
      {/if}

      <div class="actions">
        <button
          type="submit"
          class="btn btn-primary"
          disabled={!canSave}
          data-testid="save-garnrolle"
        >
          {isSaving ? "Garnrolle wird gespeichert…" : "Garnrolle speichern"}
        </button>
      </div>
      <p id="my-garnrolle-save-note" class="muted">
        Profilangaben sind öffentlich; die Adresse bleibt privat. Der
        Kartenanker wird nur nach Wahl exakt oder ungefähr veröffentlicht.
        Beides wird nicht automatisch abgeglichen.
      </p>
    </form>
  {/if}
</section>

<style>
  .my-garnrolle {
    display: grid;
    gap: 1rem;
  }

  .section-head {
    display: flex;
    justify-content: space-between;
    gap: 1rem;
    align-items: flex-start;
  }

  .section-head h2 {
    margin: 0.2rem 0 0;
  }

  .eyebrow {
    margin: 0;
    color: var(--muted, #9aa4b2);
    font-size: 0.78rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .state-pill {
    border: 1px solid var(--panel-border, rgba(255, 255, 255, 0.12));
    border-radius: 999px;
    color: var(--text, #e9eef5);
    font-size: 0.8rem;
    padding: 0.35rem 0.7rem;
    white-space: nowrap;
  }

  .state-pill[data-state="exact"] {
    background: rgba(84, 225, 166, 0.14);
  }

  .state-pill[data-state="radius"] {
    background: rgba(106, 166, 255, 0.16);
  }

  .state-pill[data-state="private"] {
    background: rgba(255, 255, 255, 0.05);
  }

  .state-pill[data-state="unknown"] {
    background: rgba(255, 210, 138, 0.1);
  }

  .status-card,
  .empty-card,
  .location-card {
    border: 1px solid var(--panel-border, rgba(255, 255, 255, 0.08));
    border-radius: 12px;
    display: flex;
    gap: 1rem;
    justify-content: space-between;
    padding: 1rem;
  }

  .status-card p,
  .empty-card p {
    margin: 0.35rem 0 0;
  }

  .location-card {
    align-items: center;
  }

  .location-card small {
    color: var(--muted, #9aa4b2);
    display: block;
    font-weight: 400;
    margin-top: 0.25rem;
    max-width: 34rem;
  }

  .warn {
    color: #ffd28a;
  }

  .muted,
  .field-intro {
    color: var(--muted, #9aa4b2);
    font-size: 0.92rem;
  }

  .field-intro {
    margin: 0;
  }

  .optional {
    color: var(--muted, #9aa4b2);
    font-size: 0.82rem;
    font-weight: 400;
  }

  .garnrolle-form {
    display: grid;
    gap: 1rem;
  }

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

  input,
  textarea {
    background: var(--bg, #0f1115);
    border: 1px solid var(--panel-border, rgba(255, 255, 255, 0.12));
    border-radius: 8px;
    color: var(--text, #e9eef5);
    font: inherit;
    padding: 0.7rem;
  }

  .radio-group {
    display: grid;
    gap: 0.7rem;
  }

  .radio-card {
    align-items: flex-start;
    border: 1px solid var(--panel-border, rgba(255, 255, 255, 0.08));
    border-radius: 10px;
    display: grid;
    gap: 0.7rem;
    grid-template-columns: auto 1fr;
    padding: 0.75rem;
  }

  .radio-card small {
    color: var(--muted, #9aa4b2);
    display: block;
    font-weight: 400;
    margin-top: 0.25rem;
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

  .form-message.success {
    background: rgba(84, 225, 166, 0.12);
    border: 1px solid rgba(84, 225, 166, 0.7);
  }

  .form-message.hint {
    background: rgba(255, 210, 138, 0.08);
    border: 1px solid rgba(255, 210, 138, 0.55);
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 0.75rem;
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

  .btn-primary {
    background: var(--accent, #6aa6ff);
    color: var(--bg);
  }

  .btn:focus-visible,
  input:focus-visible,
  textarea:focus-visible {
    outline: 3px solid var(--accent, #6aa6ff);
    outline-offset: 2px;
  }

  .radio-card:focus-within {
    border-color: var(--accent, #6aa6ff);
  }

  .btn:disabled {
    cursor: not-allowed;
    opacity: 0.55;
  }

  @media (max-width: 640px) {
    .section-head,
    .status-card,
    .empty-card,
    .location-card {
      display: grid;
    }
  }
</style>
