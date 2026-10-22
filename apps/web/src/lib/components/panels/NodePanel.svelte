<script lang="ts">
  import { parseOptionalNodeLocation } from "$lib/api/nodeLocation";
  import { createEventDispatcher, onDestroy, tick } from "svelte";
  import { selection } from "$lib/stores/uiView";
  import { authStore } from "$lib/auth/store";
  import { ApiRequestError, replaceNode } from "$lib/api/domainWrites";
  import {
    buildPanelEndpoint,
    createPanelDetailsLoader,
  } from "$lib/panels/panelDetails";
  import { formatDate } from "$lib/utils/formatDate";
  import type { MapDomainChanged } from "$lib/components/map/mapRouteEvents";
  import { nodeKindLabel } from "$lib/ui/productLanguage";
  import type { MapEntityViewModel } from "$lib/map/types";
  import {
    combineKnottingTags,
    splitKnottingTags,
    type KnottingTopic,
  } from "$lib/knottingTopics";
  import KnottingTopicsSelector from "$lib/components/KnottingTopicsSelector.svelte";
  import NodeGovernance from "$lib/components/governance/NodeGovernance.svelte";
  import NodeConversation from "./NodeConversation.svelte";

  const dispatch = createEventDispatcher<{
    selectRelated: {
      type: "node" | "garnrolle";
      id: string;
      title?: string;
      data?: MapEntityViewModel;
    };
    domainChanged: MapDomainChanged;
  }>();

  type NodeTab =
    | "uebersicht"
    | "gespraech"
    | "antraege"
    | "verlauf"
    | "bearbeiten";
  let activeTab: NodeTab = $state("uebersicht");
  let overviewTab: HTMLButtonElement | null = $state(null);
  let editTab: HTMLButtonElement | null = $state(null);
  let deleteTriggerButton: HTMLButtonElement | null = $state(null);
  let deleteCancelButton: HTMLButtonElement | null = $state(null);
  let titleInput: HTMLInputElement | null = $state(null);
  let editing = $state(false);
  let saving = $state(false);
  let deleting = $state(false);
  let deleteConfirmationOpen = $state(false);
  let mutationError = $state("");
  let archiveHref = $state("");
  let formTitle = $state("");
  let formKind = $state("");
  let formSummary = $state("");
  let formInfo = $state("");
  let formAddress = $state("");
  let formLat = $state("");
  let formLon = $state("");
  let formTopics: KnottingTopic[] = $state([]);
  let formKeywords = $state("");
  let conflictNode: NodeDetails | null = $state(null);
  let SimilarNodesComponent:
    | typeof import("./SimilarNodes.svelte").default
    | null = $state(null);
  let similarNodesLoadStarted = $state(false);

  type NodeHistoryEvent = {
    date: string;
    event: string;
    kind?: "created" | "updated";
  };

  interface NodeDetails {
    id: string;
    title: string;
    summary?: string | null;
    info?: string | null;
    tags?: string[];
    address?: string | null;
    location?: { lat: number; lon: number } | null;
    created_at?: string;
    updated_at?: string;
    created_by_account_id?: string | null;
    created_by_account_current_title?: string | null;
    kind?: string;
    participants?: {
      account_title?: string;
      account_id: string;
      edge_kind?: string;
    }[];
    history?: NodeHistoryEvent[];
  }

  function resetMutationState() {
    activeTab = "uebersicht";
    editing = false;
    saving = false;
    deleting = false;
    deleteConfirmationOpen = false;
    mutationError = "";
    archiveHref = "";
  }

  const detailsLoader = createPanelDetailsLoader<NodeDetails>(selection, {
    buildEndpoint: (id) => buildPanelEndpoint("node", id),
    onSelectionChange: resetMutationState,
    resourceLabel: "node details",
  });
  const nodeDetailsStore = detailsLoader.details;
  const isLoadingDetailsStore = detailsLoader.isLoading;
  onDestroy(detailsLoader.destroy);

  async function ensureSimilarNodesComponent() {
    if (similarNodesLoadStarted) return;
    similarNodesLoadStarted = true;
    SimilarNodesComponent = (await import("./SimilarNodes.svelte")).default;
  }

  async function focusAfterRender(
    resolveTarget: () => HTMLElement | null,
  ): Promise<void> {
    await tick();
    resolveTarget()?.focus();
  }

  async function describeMutationError(
    error: unknown,
    action: "save" | "delete",
  ): Promise<string> {
    const { nodeMutationMessage } = await import("./nodeMutationMessage");
    return nodeMutationMessage(error, action);
  }

  function setTab(tab: NodeTab) {
    if (tab !== "bearbeiten") deleteConfirmationOpen = false;
    // Mutation feedback survives ordinary tab changes so a conflict cannot be
    // dismissed accidentally. A new mutation, cancellation, selection change
    // or permission loss clears it explicitly.
    activeTab = tab;
  }

  function handleKeydown(e: KeyboardEvent) {
    const currentIndex = tabs.indexOf(activeTab);
    let nextIndex = currentIndex;
    if (e.key === "ArrowRight") nextIndex = (currentIndex + 1) % tabs.length;
    else if (e.key === "ArrowLeft")
      nextIndex = (currentIndex - 1 + tabs.length) % tabs.length;
    else if (e.key === "Home") nextIndex = 0;
    else if (e.key === "End") nextIndex = tabs.length - 1;
    else return;
    e.preventDefault();
    setTab(tabs[nextIndex]);
    const buttons = (e.currentTarget as HTMLElement)
      ?.closest(".tabs")
      ?.querySelectorAll<HTMLButtonElement>('button[role="tab"]');
    buttons?.item(nextIndex)?.focus();
  }

  function beginEdit() {
    const fallback = $selection?.data;
    const location = nodeDetails
      ? nodeDetails.location
      : typeof fallback?.lat === "number" && typeof fallback?.lon === "number"
        ? { lat: fallback.lat, lon: fallback.lon }
        : undefined;
    formTitle = nodeDetails?.title || fallback?.title || "";
    formKind = nodeDetails?.kind || fallback?.kind || "";
    formSummary = nodeDetails?.summary || fallback?.summary || "";
    formInfo = nodeDetails?.info || fallback?.info || "";
    formAddress = nodeDetails?.address || "";
    formLat = location ? String(location.lat) : "";
    formLon = location ? String(location.lon) : "";
    const splitTags = splitKnottingTags(
      nodeDetails?.tags || fallback?.tags || [],
    );
    formTopics = splitTags.topics;
    formKeywords = splitTags.keywords.join(", ");
    mutationError = "";
    conflictNode = null;
    editing = true;
    void focusAfterRender(() => titleInput);
  }

  function cancelEdit() {
    editing = false;
    mutationError = "";
    conflictNode = null;
    void focusAfterRender(() => editTab);
  }

  async function saveNode() {
    const id = nodeDetails?.id || $selection?.id;
    const location = parseOptionalNodeLocation(formLat, formLon);
    if (!id) return;
    if (!formTitle.trim() || !formKind.trim()) {
      mutationError = "Titel und Knotenart werden benötigt.";
      return;
    }
    if (location === undefined) {
      mutationError = "Bitte beide Koordinaten angeben oder beide leer lassen.";
      return;
    }

    saving = true;
    mutationError = "";
    try {
      const updatedNode = await replaceNode(
        id,
        {
          title: formTitle.trim(),
          kind: formKind.trim(),
          address: formAddress.trim() || null,
          location,
          summary: formSummary.trim() || undefined,
          info: formInfo.trim() || undefined,
          tags: combineKnottingTags(formTopics, formKeywords.split(",")),
        },
        nodeDetails?.updated_at,
      );
      detailsLoader.setDetails({
        ...(nodeDetails ?? {}),
        ...updatedNode,
      } as NodeDetails);
      conflictNode = null;
      editing = false;
      activeTab = "uebersicht";
      void focusAfterRender(() => overviewTab);
      dispatch("domainChanged", {
        kind: "node",
        id,
        action: "updated",
        node: updatedNode,
      });
    } catch (error) {
      if (
        error instanceof ApiRequestError &&
        error.status === 412 &&
        error.body
      ) {
        const currentNode = error.body as NodeDetails;
        mutationError =
          "Der Knoten wurde in der Zwischenzeit geändert. Dein Entwurf bleibt erhalten. Vergleiche ihn mit dem aktuellen Stand und speichere anschließend erneut.";
        conflictNode = currentNode;
        detailsLoader.setDetails({
          ...(nodeDetails ?? {}),
          ...currentNode,
        } as NodeDetails);
      } else {
        mutationError = await describeMutationError(error, "save");
      }
    } finally {
      saving = false;
    }
  }

  function requestNodeRemoval() {
    if (deleting) return;
    mutationError = "";
    deleteConfirmationOpen = true;
    void focusAfterRender(() => deleteCancelButton);
  }

  function cancelNodeRemoval() {
    if (deleting) return;
    deleteConfirmationOpen = false;
    void focusAfterRender(() => deleteTriggerButton);
  }

  function handleDeleteConfirmationKeydown(event: KeyboardEvent) {
    if (event.key !== "Escape" || deleting) return;
    event.preventDefault();
    event.stopPropagation();
    cancelNodeRemoval();
  }

  async function removeNode() {
    const id = nodeDetails?.id || $selection?.id;
    if (!id || deleting) return;

    deleting = true;
    mutationError = "";
    try {
      const { deleteNode } = await import("$lib/api/nodeDelete");
      const receipt = await deleteNode(id, nodeDetails?.updated_at);
      let action: "deleted" | "archived" = "deleted";
      if (receipt.conversation.effect === "archived") {
        archiveHref = `/archive?id=${encodeURIComponent(receipt.conversation.archive_id)}`;
        action = "archived";
      }
      dispatch("domainChanged", { kind: "node", id, action });
    } catch (error) {
      if (
        error instanceof ApiRequestError &&
        error.status === 412 &&
        error.body
      ) {
        mutationError =
          "Der Knoten wurde in der Zwischenzeit geändert und konnte nicht gelöscht werden. Die Ansicht zeigt nun den aktuellen Stand.";
        detailsLoader.setDetails({
          ...(nodeDetails ?? {}),
          ...(error.body as object),
        } as NodeDetails);
      } else {
        mutationError = await describeMutationError(error, "delete");
      }
    } finally {
      deleting = false;
      deleteConfirmationOpen = false;
      void focusAfterRender(() => deleteTriggerButton);
    }
  }
  let nodeDetails = $derived.by(() => $nodeDetailsStore);
  let isLoadingDetails = $derived.by(() => $isLoadingDetailsStore);
  let summary = $derived.by(
    () => nodeDetails?.summary || $selection?.data?.summary,
  );
  let kind = $derived.by(() =>
    nodeKindLabel(nodeDetails?.kind || $selection?.data?.kind),
  );
  let nodeCreator = $derived.by(
    () =>
      nodeDetails?.created_by_account_id ||
      ($selection?.data?.created_by_account_id as string | undefined),
  );
  let currentCreatorTitle = $derived.by(
    () => nodeDetails?.created_by_account_current_title || "",
  );
  let timelineEvents = $derived.by(() => nodeDetails?.history || []);
  let visibleTagSplit = $derived.by(() =>
    splitKnottingTags(nodeDetails?.tags || $selection?.data?.tags || []),
  );
  let canMutate = $derived.by(
    () =>
      $authStore.authenticated &&
      ($authStore.role === "weber" ||
        $authStore.role === "admin" ||
        (!!nodeCreator && nodeCreator === $authStore.account_id)),
  );
  let tabs: NodeTab[] = $derived(
    canMutate
      ? ["uebersicht", "gespraech", "verlauf", "antraege", "bearbeiten"]
      : ["uebersicht", "gespraech", "verlauf", "antraege"],
  );
  $effect.pre(() => {
    if (!canMutate) {
      const shouldRestoreFocus = activeTab === "bearbeiten" || editing;
      if (activeTab === "bearbeiten") activeTab = "uebersicht";
      if (editing) editing = false;
      mutationError = "";
      conflictNode = null;
      if (shouldRestoreFocus) void focusAfterRender(() => overviewTab);
    }
  });
  let conflictTagSplit = $derived.by(() =>
    splitKnottingTags(conflictNode?.tags || []),
  );
  $effect.pre(() => {
    if ($selection?.type === "node" && !similarNodesLoadStarted) {
      void ensureSimilarNodesComponent();
    }
  });
</script>

<div class="node-mode" class:editing>
  {#if archiveHref}
    <a href={archiveHref} aria-live="polite">Knoten entfernt: Archiv öffnen</a>
  {:else}
    <h3>{nodeDetails?.title || $selection?.data?.title || $selection?.id}</h3>
    {#if summary}<p class="summary node-summary">{summary}</p>{/if}
    <div
      class="compact-node-summary"
      data-testid="node-compact-summary"
      role="region"
      aria-label="Knotenübersicht"
    >
      <p><strong>Knotenart:</strong> {kind}</p>
    </div>

    {#if editing}
      <form
        class="edit-form node-full-content"
        onsubmit={(event) => {
          event.preventDefault();
          void saveNode();
        }}
      >
        <label>
          Titel
          <input
            bind:this={titleInput}
            bind:value={formTitle}
            maxlength="200"
            required
          />
        </label>
        <label>
          Knotenart
          <input bind:value={formKind} maxlength="100" required />
        </label>
        <label>
          Kurzbeschreibung
          <textarea bind:value={formSummary} maxlength="500" rows="3"
          ></textarea>
        </label>
        <label>
          Ausführliche Information
          <textarea bind:value={formInfo} maxlength="20000" rows="6"></textarea>
        </label>
        <label>
          Adresse
          <input bind:value={formAddress} maxlength="500" />
        </label>
        <p class="field-help">Ohne Kartenort: beide Koordinaten leer lassen.</p>
        <div class="coordinate-grid">
          <label>
            Breitengrad
            <input bind:value={formLat} inputmode="decimal" />
          </label>
          <label>
            Längengrad
            <input bind:value={formLon} inputmode="decimal" />
          </label>
        </div>
        <KnottingTopicsSelector
          id="edit-knot-topics"
          bind:value={formTopics}
          disabled={saving}
        />
        <label>
          Weitere Schlagwörter, durch Kommas getrennt
          <input bind:value={formKeywords} />
          <span class="field-help">
            Freie Schlagwörter ergänzen Suche und Beschreibung; sie ersetzen die
            ausgewählten Themen nicht.
          </span>
        </label>

        {#if mutationError}<p class="error" role="alert">
            {mutationError}
          </p>{/if}
        {#if conflictNode}
          <section class="conflict-current" aria-label="Aktueller Serverstand">
            <strong>Aktueller Stand in commonThing</strong>
            <dl>
              <div>
                <dt>Titel</dt>
                <dd>{conflictNode.title}</dd>
              </div>
              <div>
                <dt>Knotenart</dt>
                <dd>{conflictNode.kind}</dd>
              </div>
              {#if conflictNode.summary}<div>
                  <dt>Kurzbeschreibung</dt>
                  <dd>{conflictNode.summary}</dd>
                </div>{/if}
              {#if conflictNode.info}<div>
                  <dt>Information</dt>
                  <dd>{conflictNode.info}</dd>
                </div>{/if}
              {#if conflictNode.address}<div>
                  <dt>Adresse (optional)</dt>
                  <dd>{conflictNode.address}</dd>
                </div>{/if}
              {#if conflictTagSplit.topics.length}<div>
                  <dt>Themen</dt>
                  <dd>{conflictTagSplit.topics.join(", ")}</dd>
                </div>{/if}
              {#if conflictTagSplit.keywords.length}<div>
                  <dt>Schlagwörter</dt>
                  <dd>{conflictTagSplit.keywords.join(", ")}</dd>
                </div>{/if}
            </dl>
          </section>
        {/if}
        <div class="form-actions">
          <button
            type="button"
            class="secondary"
            onclick={cancelEdit}
            disabled={saving}>Abbrechen</button
          >
          <button type="submit" class="primary" disabled={saving}
            >{saving ? "Speichert…" : "Änderungen speichern"}</button
          >
        </div>
      </form>
    {:else}
      <div
        class="tabs node-tabs node-full-content"
        role="tablist"
        aria-label="Knoten-Tabs"
      >
        <button
          class:active={activeTab === "uebersicht"}
          onclick={() => setTab("uebersicht")}
          onkeydown={handleKeydown}
          role="tab"
          aria-selected={activeTab === "uebersicht"}
          aria-controls="panel-uebersicht"
          id="tab-uebersicht"
          bind:this={overviewTab}
          tabindex={activeTab === "uebersicht" ? 0 : -1}>Übersicht</button
        >
        <button
          class:active={activeTab === "gespraech"}
          onclick={() => setTab("gespraech")}
          onkeydown={handleKeydown}
          role="tab"
          aria-selected={activeTab === "gespraech"}
          aria-controls="panel-gespraech"
          id="tab-gespraech"
          tabindex={activeTab === "gespraech" ? 0 : -1}>Gespräch</button
        >
        <button
          class:active={activeTab === "verlauf"}
          onclick={() => setTab("verlauf")}
          onkeydown={handleKeydown}
          role="tab"
          aria-selected={activeTab === "verlauf"}
          aria-controls="panel-verlauf"
          id="tab-verlauf"
          tabindex={activeTab === "verlauf" ? 0 : -1}>Verlauf</button
        >
        <button
          class:active={activeTab === "antraege"}
          onclick={() => setTab("antraege")}
          onkeydown={handleKeydown}
          role="tab"
          aria-selected={activeTab === "antraege"}
          aria-controls="panel-antraege"
          id="tab-antraege"
          tabindex={activeTab === "antraege" ? 0 : -1}>Anträge</button
        >
        {#if canMutate}
          <button
            class:active={activeTab === "bearbeiten"}
            onclick={() => setTab("bearbeiten")}
            onkeydown={handleKeydown}
            role="tab"
            aria-selected={activeTab === "bearbeiten"}
            aria-controls="panel-bearbeiten"
            id="tab-bearbeiten"
            bind:this={editTab}
            tabindex={activeTab === "bearbeiten" ? 0 : -1}>Bearbeiten</button
          >
        {/if}
      </div>

      <div class="tab-content node-full-content">
        <div
          class="overview"
          id="panel-uebersicht"
          role="tabpanel"
          aria-labelledby="tab-uebersicht"
          tabindex={activeTab === "uebersicht" ? 0 : -1}
          hidden={activeTab !== "uebersicht"}
        >
          {#if activeTab === "uebersicht"}
            {#if isLoadingDetails}<p class="ghost">Lade Details…</p>
            {:else}
              {#if nodeDetails?.created_at || $selection?.data?.created_at}<p>
                  <strong>Geknüpft am:</strong>
                  {formatDate(
                    nodeDetails?.created_at || $selection?.data?.created_at,
                  )}
                </p>{/if}
              <p><strong>Knotenart:</strong> {kind}</p>
              {#if nodeDetails?.address}<p>
                  <strong>Adresse:</strong>
                  {nodeDetails.address}
                </p>{/if}
              {#if nodeDetails?.info}<p class="long-info">
                  <strong>Information:</strong>
                  {nodeDetails.info}
                </p>{/if}
              {#if visibleTagSplit.topics.length}<p>
                  <strong>Themen:</strong>
                  {visibleTagSplit.topics.join(", ")}
                </p>{/if}
              {#if visibleTagSplit.keywords.length}<p>
                  <strong>Schlagwörter:</strong>
                  {visibleTagSplit.keywords.join(", ")}
                </p>{/if}
              {#if nodeDetails?.participants?.length}
                <div class="participants">
                  <p><strong>Beteiligte Garnrollen</strong></p>
                  <ul>
                    {#each nodeDetails.participants as participant}<li>
                        <button
                          type="button"
                          onclick={() =>
                            dispatch("selectRelated", {
                              type: "garnrolle",
                              id: participant.account_id,
                            })}
                          >{participant.account_title ||
                            participant.account_id}</button
                        >
                      </li>{/each}
                  </ul>
                </div>
              {/if}
              {#if SimilarNodesComponent}
                <SimilarNodesComponent
                  sourceId={nodeDetails?.id || $selection?.id || ""}
                  title={nodeDetails?.title || $selection?.data?.title}
                  kind={nodeDetails?.kind || $selection?.data?.kind}
                  summary={nodeDetails?.summary || $selection?.data?.summary}
                  info={nodeDetails?.info || $selection?.data?.info}
                  tags={nodeDetails?.tags || $selection?.data?.tags}
                  on:select={(event) => dispatch("selectRelated", event.detail)}
                />
              {/if}
            {/if}
          {/if}
        </div>

        <div
          id="panel-gespraech"
          role="tabpanel"
          aria-labelledby="tab-gespraech"
          tabindex={activeTab === "gespraech" ? 0 : -1}
          hidden={activeTab !== "gespraech"}
        >
          {#if activeTab === "gespraech"}
            {#key nodeDetails?.id || $selection?.id || ""}
              <NodeConversation
                nodeId={nodeDetails?.id || $selection?.id || ""}
              />
            {/key}
          {/if}
        </div>

        <div
          id="panel-verlauf"
          role="tabpanel"
          aria-labelledby="tab-verlauf"
          tabindex={activeTab === "verlauf" ? 0 : -1}
          hidden={activeTab !== "verlauf"}
        >
          {#if activeTab === "verlauf"}
            {#if isLoadingDetails}<p class="ghost">Lade Verlauf…</p>
            {:else if timelineEvents.length}<ul class="timeline">
                {#each timelineEvents as event}<li>
                    <span class="date">{formatDate(event.date)}</span><span
                      class="event">{event.event}</span
                    >
                    {#if event.kind === "created" && currentCreatorTitle && nodeCreator}<span
                        class="creator"
                      >
                        <span>Urheber:</span>
                        <button
                          type="button"
                          aria-label={`Garnrolle ${currentCreatorTitle} öffnen`}
                          onclick={() =>
                            dispatch("selectRelated", {
                              type: "garnrolle",
                              id: nodeCreator,
                              title: currentCreatorTitle,
                            })}>{currentCreatorTitle}</button
                        >
                      </span>{/if}
                  </li>{/each}
              </ul>
            {:else}<p class="ghost">Noch kein Verlauf.</p>{/if}
          {/if}
        </div>

        <div
          id="panel-antraege"
          role="tabpanel"
          aria-labelledby="tab-antraege"
          tabindex={activeTab === "antraege" ? 0 : -1}
          hidden={activeTab !== "antraege"}
        >
          {#if activeTab === "antraege"}
            {#key nodeDetails?.id || $selection?.id || ""}
              <NodeGovernance
                nodeId={nodeDetails?.id || $selection?.id || ""}
                nodeTitle={nodeDetails?.title ||
                  $selection?.data?.title ||
                  $selection?.id ||
                  "Knoten"}
              />
            {/key}
          {/if}
        </div>

        {#if canMutate}
          <div
            class="mutation-actions"
            id="panel-bearbeiten"
            role="tabpanel"
            aria-labelledby="tab-bearbeiten"
            tabindex={activeTab === "bearbeiten" ? 0 : -1}
            hidden={activeTab !== "bearbeiten"}
          >
            {#if activeTab === "bearbeiten"}
              {#if mutationError}<p class="error" role="alert">
                  {mutationError}
                </p>{/if}
              <button type="button" class="secondary" onclick={beginEdit}
                >Bearbeiten</button
              >
              {#if deleteConfirmationOpen}
                <div class="delete-confirmation" aria-live="polite">
                  <p><strong>Knoten wirklich entfernen?</strong></p>
                  <p>
                    Alle verbundenen Fäden werden entfernt. Bestehende
                    Gesprächsbeiträge bleiben als schreibgeschütztes Archiv
                    erhalten.
                  </p>
                  <div class="delete-confirmation-actions">
                    <button
                      type="button"
                      class="secondary"
                      bind:this={deleteCancelButton}
                      onclick={cancelNodeRemoval}
                      onkeydown={handleDeleteConfirmationKeydown}
                      disabled={deleting}>Abbrechen</button
                    >
                    <button
                      type="button"
                      class="danger danger-solid"
                      onclick={removeNode}
                      onkeydown={handleDeleteConfirmationKeydown}
                      disabled={deleting}
                      >{deleting ? "Wird entfernt…" : "Jetzt entfernen"}</button
                    >
                  </div>
                </div>
              {:else}
                <button
                  type="button"
                  class="danger"
                  bind:this={deleteTriggerButton}
                  onclick={requestNodeRemoval}
                  disabled={deleting}>Aus dem Gewebe entfernen</button
                >
              {/if}
              <p class="collective-note">
                Eigene Knoten kannst du selbst pflegen. Weber können zusätzlich
                gemeinschaftliche Knoten weiterentwickeln.
              </p>
            {/if}
          </div>
        {/if}
      </div>
    {/if}
  {/if}
</div>

<style>
  h3 {
    margin: 0;
    font-size: 1.5rem;
    line-height: 1.2;
  }
  .compact-node-summary {
    display: none;
  }
  .node-mode.editing .node-summary {
    display: none;
  }
  .node-tabs {
    gap: 0;
  }
  .node-tabs > button {
    /* Let longer labels claim a little more width instead of forcing every
     * tab into an equal column. Words stay intact and all tabs remain visible. */
    flex: 1 1 auto;
    min-width: 0;
    padding-inline: 0.2rem;
    line-height: 1.15;
    overflow-wrap: normal;
    white-space: nowrap;
  }
  .tab-content > [hidden] {
    display: none;
  }
  .summary {
    color: var(--muted);
    margin: 0.5rem 0 1.25rem;
  }
  .ghost,
  .collective-note {
    color: var(--muted);
    font-size: 0.9rem;
  }
  .overview > p {
    margin: 0 0 0.65rem;
    font-size: 0.95rem;
  }
  .long-info {
    white-space: pre-wrap;
  }
  .participants {
    margin-top: 1rem;
    padding-top: 1rem;
    border-top: 1px solid var(--panel-border);
  }
  .mutation-actions {
    padding-top: 0.25rem;
  }
  .field-help {
    display: block;
    margin-top: 0.35rem;
    color: var(--muted);
    font-size: 0.85rem;
    font-weight: 400;
  }
  .participants p,
  .collective-note {
    margin: 0 0 0.5rem;
  }
  .participants ul {
    list-style: none;
    padding: 0;
    margin: 0;
    display: grid;
    gap: 0.5rem;
  }
  .participants button {
    width: 100%;
    min-height: 44px;
    padding: 0.65rem 0.75rem;
    border: 1px solid var(--panel-border-strong);
    border-radius: 8px;
    background: var(--panel-solid);
    color: var(--text);
    text-align: left;
    font-weight: 600;
    cursor: pointer;
  }
  .participants button:hover,
  .participants button:focus-visible {
    border-color: var(--accent);
  }
  .creator {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.25rem;
    margin-top: 0.25rem;
    color: var(--muted);
    font-size: 0.85rem;
  }
  .creator button {
    min-width: 44px;
    min-height: 44px;
    max-width: 100%;
    padding: 0.35rem 0.25rem;
    border: 0;
    border-radius: 0.25rem;
    background: transparent;
    color: var(--text);
    font: inherit;
    font-weight: 650;
    overflow-wrap: anywhere;
    white-space: normal;
    text-align: left;
    text-decoration: underline;
    text-underline-offset: 0.15em;
    cursor: pointer;
  }
  .creator button:hover {
    color: var(--accent);
  }
  .creator button:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }
  .edit-form,
  .mutation-actions {
    display: grid;
    gap: 0.85rem;
  }
  .edit-form {
    margin-top: 1rem;
  }
  .edit-form label {
    display: grid;
    gap: 0.35rem;
    font-size: 0.9rem;
    font-weight: 650;
  }
  .edit-form input,
  .edit-form textarea {
    width: 100%;
    box-sizing: border-box;
    padding: 0.7rem;
    border: 1px solid var(--panel-border-strong);
    border-radius: 8px;
    background: var(--panel-solid);
    color: var(--text);
    font: inherit;
  }
  .coordinate-grid,
  .form-actions {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 0.75rem;
  }
  .form-actions button,
  .mutation-actions > button {
    min-height: 44px;
    border-radius: 8px;
    padding: 0.65rem 0.85rem;
    font: inherit;
    font-weight: 700;
    cursor: pointer;
  }
  .primary {
    border: 1px solid var(--accent);
    background: var(--accent);
    color: var(--panel-solid);
  }
  .secondary {
    border: 1px solid var(--panel-border-strong);
    background: var(--panel-solid);
    color: var(--text);
  }
  .danger {
    border: 1px solid #a33;
    background: transparent;
    color: #a33;
  }
  .delete-confirmation {
    display: grid;
    gap: 0.65rem;
    padding: 0.8rem;
    border: 1px solid color-mix(in srgb, #a33 45%, var(--panel-border));
    border-radius: 8px;
    background: color-mix(in srgb, #a33 6%, var(--panel-solid));
  }
  .delete-confirmation p {
    margin: 0;
  }
  .delete-confirmation-actions {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 0.65rem;
  }
  .delete-confirmation-actions button {
    min-height: 44px;
    border-radius: 8px;
    padding: 0.65rem 0.85rem;
    font: inherit;
    font-weight: 700;
    cursor: pointer;
  }
  .danger-solid {
    background: #a33;
    color: white;
  }
  button:disabled {
    cursor: wait;
    opacity: 0.65;
  }
  .error {
    margin: 0;
    color: #a33;
    font-weight: 650;
  }
  .conflict-current {
    border: 1px solid color-mix(in srgb, #a33 35%, transparent);
    border-radius: 0.5rem;
    padding: 0.75rem;
  }
  .conflict-current dl {
    display: grid;
    gap: 0.4rem;
    margin: 0.55rem 0 0;
  }
  .conflict-current dl div {
    display: grid;
    grid-template-columns: minmax(6rem, auto) 1fr;
    gap: 0.65rem;
  }
  .conflict-current dt {
    font-weight: 600;
  }
  .conflict-current dd {
    margin: 0;
    overflow-wrap: anywhere;
    white-space: pre-wrap;
  }
  @media (max-width: 420px) {
    .node-tabs > button {
      padding-inline: 0.1rem;
      font-size: 0.75rem;
    }
    .coordinate-grid,
    .form-actions {
      grid-template-columns: 1fr;
    }
  }
</style>
