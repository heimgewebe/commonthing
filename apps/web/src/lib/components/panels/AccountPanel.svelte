<script lang="ts">
  import { createEventDispatcher, onDestroy, onMount } from "svelte";
  import { selection } from "$lib/stores/uiView";
  import {
    buildPanelEndpoint,
    createPanelDetailsLoader,
  } from "$lib/panels/panelDetails";
  import { formatDate } from "$lib/utils/formatDate";
  import { authStore } from "$lib/auth/store";
  import AudienceCircleControls from "./AudienceCircleControls.svelte";

  const dispatch = createEventDispatcher<{
    selectRelated: { type: "node"; id: string };
  }>();
  let activeTab = $state("profil");
  let heading: HTMLHeadingElement | undefined = $state();

  interface AccountDetails {
    id: string;
    title: string;
    summary?: string;
    tags?: string[];
    created_at?: string;
    nodes?: {
      node_id: string;
      node_title: string;
      node_kind: string;
      edge_kind: string;
    }[];
    activity?: { date: string; event: string }[];
  }

  const detailsLoader = createPanelDetailsLoader<AccountDetails>(selection, {
    buildEndpoint: (id) => buildPanelEndpoint("account", id),
    onSelectionChange: () => {
      activeTab = "profil";
    },
    resourceLabel: "account details",
  });
  const accountDetailsStore = detailsLoader.details;
  const isLoadingDetailsStore = detailsLoader.isLoading;
  onDestroy(detailsLoader.destroy);
  onMount(() => heading?.focus());

  function categoryValues(
    tags: string[],
    prefix: string,
    includeLegacy = false,
  ): string[] {
    const values = tags
      .filter((tag) => tag.startsWith(prefix))
      .map((tag) => tag.slice(prefix.length))
      .filter(Boolean);
    if (includeLegacy)
      values.push(
        ...tags.filter(
          (tag) =>
            tag !== "account" && tag !== "garnrolle" && !tag.includes(":"),
        ),
      );
    return values.filter((value, index, all) => all.indexOf(value) === index);
  }

  const tabs = ["profil", "aktivitaet", "knoten"];
  function setTab(tab: string) {
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
  let accountDetails = $derived($accountDetailsStore);
  let isLoadingDetails = $derived($isLoadingDetailsStore);
  let summary = $derived(accountDetails?.summary || $selection?.data?.summary);
  let profileTags = $derived(
    (accountDetails?.tags || $selection?.data?.tags || []) as string[],
  );
  let skills = $derived(categoryValues(profileTags, "skill:", true));
  let goods = $derived(categoryValues(profileTags, "good:"));
  let interests = $derived(categoryValues(profileTags, "interest:"));
</script>

<div class="account-mode">
  <h3 bind:this={heading} tabindex="-1" data-testid="account-heading">
    {accountDetails?.title || $selection?.data?.title || "Garnrolle"}
  </h3>
  {#if summary}<p class="summary">{summary}</p>{/if}
  {#if $authStore.authenticated && $selection?.id && $selection.id !== $authStore.account_id}
    <a
      class="message-link"
      href={`/nachrichten?mit=${encodeURIComponent($selection.id)}`}
      >Private Nachricht</a
    >
  {/if}

  <div
    class="compact-account-summary"
    data-testid="account-compact-summary"
    role="region"
    aria-label="Garnrollenprofil"
  >
    {#if isLoadingDetails && !accountDetails}
      <p class="ghost">Lade Profil…</p>
    {:else}
      {#if accountDetails?.created_at || $selection?.data?.created_at}<p>
          <strong>Dabei seit:</strong>
          {formatDate(
            accountDetails?.created_at || $selection?.data?.created_at,
          )}
        </p>{/if}
      {#if skills.length > 0}<p>
          <strong>Fähigkeiten:</strong>
          {skills.join(", ")}
        </p>{/if}
      {#if goods.length > 0}<p>
          <strong>Güter:</strong>
          {goods.join(", ")}
        </p>{/if}
      {#if interests.length > 0}<p>
          <strong>Interessen:</strong>
          {interests.join(", ")}
        </p>{/if}
    {/if}
  </div>

  <div
    class="tabs account-full-content"
    role="tablist"
    aria-label="Garnrollen-Tabs"
  >
    <button
      class:active={activeTab === "profil"}
      onclick={() => setTab("profil")}
      onkeydown={handleKeydown}
      role="tab"
      aria-selected={activeTab === "profil"}
      aria-controls="panel-profil"
      id="tab-profil"
      tabindex={activeTab === "profil" ? 0 : -1}>Profil</button
    >
    <button
      class:active={activeTab === "aktivitaet"}
      onclick={() => setTab("aktivitaet")}
      onkeydown={handleKeydown}
      role="tab"
      aria-selected={activeTab === "aktivitaet"}
      aria-controls="panel-aktivitaet"
      id="tab-aktivitaet"
      tabindex={activeTab === "aktivitaet" ? 0 : -1}>Aktivität</button
    >
    <button
      class:active={activeTab === "knoten"}
      onclick={() => setTab("knoten")}
      onkeydown={handleKeydown}
      role="tab"
      aria-selected={activeTab === "knoten"}
      aria-controls="panel-knoten"
      id="tab-knoten"
      tabindex={activeTab === "knoten" ? 0 : -1}>Knoten</button
    >
  </div>

  <div class="tab-content account-full-content">
    {#if isLoadingDetails && !accountDetails}
      <p class="ghost">Lade Details…</p>
    {:else if activeTab === "profil"}
      <div
        class="overview"
        id="panel-profil"
        role="tabpanel"
        aria-labelledby="tab-profil"
      >
        {#if accountDetails?.created_at || $selection?.data?.created_at}<p>
            <strong>Dabei seit:</strong>
            {formatDate(
              accountDetails?.created_at || $selection?.data?.created_at,
            )}
          </p>{/if}
        {#if skills.length > 0}<p>
            <strong>Fähigkeiten:</strong>
            {skills.join(", ")}
          </p>{/if}
        {#if goods.length > 0}<p>
            <strong>Güter:</strong>
            {goods.join(", ")}
          </p>{/if}
        {#if interests.length > 0}<p>
            <strong>Interessen:</strong>
            {interests.join(", ")}
          </p>{/if}
        <AudienceCircleControls
          selectedAccountId={$selection?.id ?? ""}
          selectedName={accountDetails?.title ||
            $selection?.data?.title ||
            "dieser Garnrolle"}
        />
      </div>
    {:else if activeTab === "aktivitaet"}
      <div
        id="panel-aktivitaet"
        role="tabpanel"
        aria-labelledby="tab-aktivitaet"
      >
        {#if isLoadingDetails}<p class="ghost">Lade Verlauf…</p>
        {:else if accountDetails?.activity?.length}<ul class="timeline">
            {#each accountDetails.activity as event}<li>
                <span class="date">{formatDate(event.date)}</span><span
                  class="event">{event.event}</span
                >
              </li>{/each}
          </ul>
        {:else}<p class="ghost">Noch keine Aktivität.</p>{/if}
      </div>
    {:else}
      <div id="panel-knoten" role="tabpanel" aria-labelledby="tab-knoten">
        {#if isLoadingDetails}<p class="ghost">Lade Knoten…</p>
        {:else if accountDetails?.nodes?.length}<ul class="node-list">
            {#each accountDetails.nodes as node}<li>
                <button
                  type="button"
                  onclick={() =>
                    dispatch("selectRelated", {
                      type: "node",
                      id: node.node_id,
                    })}>{node.node_title || node.node_id}</button
                >
              </li>{/each}
          </ul>
        {:else}<p class="ghost">Noch keine geknüpften Knoten.</p>{/if}
      </div>
    {/if}
  </div>
</div>

<style>
  h3 {
    margin: 0;
    font-size: 1.5rem;
    line-height: 1.2;
  }
  h3[tabindex="-1"]:focus,
  h3[tabindex="-1"]:focus-visible {
    outline: none;
  }
  .summary {
    color: var(--muted);
    margin: 0.5rem 0 1.25rem;
  }
  .message-link {
    display: inline-flex;
    align-items: center;
    min-height: 44px;
    margin: 0.75rem 0 1rem;
    padding: 0 0.9rem;
    border: 1px solid var(--panel-border-strong);
    border-radius: 999px;
    background: var(--panel-solid);
    color: var(--text);
    text-decoration: none;
    font-weight: 700;
  }
  .message-link:hover,
  .message-link:focus-visible {
    border-color: var(--accent);
  }
  .ghost {
    color: var(--muted);
    font-size: 0.9rem;
  }
  .compact-account-summary {
    display: none;
  }
  .compact-account-summary p,
  .overview p {
    margin: 0 0 0.65rem;
    font-size: 0.95rem;
  }
  .node-list {
    list-style: none;
    padding: 0;
    margin: 0;
    display: grid;
    gap: 0.5rem;
  }
  .node-list button {
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
  .node-list button:hover,
  .node-list button:focus-visible {
    border-color: var(--accent);
  }
</style>
