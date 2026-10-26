<script lang="ts">
  import { onMount } from "svelte";
  import buildVersion from "$lib/generated/buildVersion.json";

  type ServerVersion = {
    version?: string;
    build_id?: string;
    built_at?: string;
    commit?: string;
    release?: string;
  };

  type Status = "loading" | "ok" | "unreachable" | "invalid";

  const localVersion: string = buildVersion.version ?? "unknown";
  const localBuildId: string | undefined = (buildVersion as ServerVersion)
    .build_id;
  const localBuiltAt: string | undefined = (buildVersion as ServerVersion)
    .built_at;
  const localCommit: string | undefined = (buildVersion as ServerVersion)
    .commit;

  let serverData: ServerVersion | null = $state(null);
  let status: Status = $state("loading");

  function formatTimestamp(value: string | undefined): string | null {
    if (!value) return null;
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return null;
    return new Intl.DateTimeFormat("de-DE", {
      timeZone: "UTC",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }).format(date);
  }

  let localBuiltAtFormatted = $derived.by(() => formatTimestamp(localBuiltAt));
  let serverBuiltAtFormatted = $derived.by(() =>
    formatTimestamp(serverData?.built_at),
  );

  let serverVersion = $derived.by(() => serverData?.version ?? null);
  let inSync = $derived.by(
    () => status === "ok" && serverVersion === localVersion,
  );

  async function refresh() {
    status = "loading";
    serverData = null;
    try {
      const res = await fetch("/_app/version.json", { cache: "no-store" });
      if (!res.ok) {
        status = "unreachable";
        return;
      }
      const data = (await res.json()) as ServerVersion;
      if (typeof data.version !== "string" || data.version.trim() === "") {
        status = "invalid";
        serverData = data;
        return;
      }
      serverData = data;
      status = "ok";
    } catch {
      status = "unreachable";
    }
  }

  onMount(() => {
    refresh();
  });
</script>

<svelte:head>
  <title>Build · commonThing</title>
  <meta name="robots" content="noindex" />
</svelte:head>

<div class="container">
  <a class="back-link" href="/settings">← Zu den Einstellungen</a>
  <h1>Build-Diagnose</h1>
  <p class="intro">
    Diese Seite zeigt die im Browser geladene Build-Identität und vergleicht sie
    mit dem aktuell auf dem Server ausgelieferten Stand. Sie ist als Support-
    und Debug-Ansicht gedacht und nicht für die normale Nutzung erforderlich.
  </p>

  <section class="card" data-testid="build-local">
    <h2>Lokaler Bundle-Stand</h2>
    <dl>
      <dt>Version</dt>
      <dd data-testid="build-local-version">{localVersion}</dd>
      {#if localBuildId}
        <dt>Build-ID</dt>
        <dd data-testid="build-local-build-id">{localBuildId}</dd>
      {/if}
      {#if localBuiltAtFormatted}
        <dt>Gebaut am</dt>
        <dd data-testid="build-local-built-at">{localBuiltAtFormatted} UTC</dd>
      {/if}
      {#if localCommit}
        <dt>Commit</dt>
        <dd data-testid="build-local-commit">{localCommit}</dd>
      {/if}
    </dl>
  </section>

  <section class="card" data-testid="build-server">
    <h2>Server-Stand (live)</h2>
    {#if status === "loading"}
      <p data-testid="build-server-status">Wird geladen…</p>
    {:else if status === "unreachable"}
      <p class="error" data-testid="build-server-status">
        Server-Versionsdatei nicht erreichbar.
      </p>
    {:else if status === "invalid"}
      <p class="error" data-testid="build-server-status">
        Server-Versionsdatei ohne brauchbare <code>version</code> empfangen.
      </p>
    {:else if serverData}
      <dl>
        <dt>Version</dt>
        <dd data-testid="build-server-version">{serverData.version}</dd>
        {#if serverData.release}
          <dt>Release</dt>
          <dd data-testid="build-server-release">{serverData.release}</dd>
        {/if}
        {#if serverData.build_id}
          <dt>Build-ID</dt>
          <dd data-testid="build-server-build-id">{serverData.build_id}</dd>
        {/if}
        {#if serverBuiltAtFormatted}
          <dt>Gebaut am</dt>
          <dd data-testid="build-server-built-at">
            {serverBuiltAtFormatted} UTC
          </dd>
        {/if}
        {#if serverData.commit}
          <dt>Commit</dt>
          <dd data-testid="build-server-commit">{serverData.commit}</dd>
        {/if}
      </dl>
    {/if}
  </section>

  <section class="card sync" data-testid="build-sync">
    <h2>Abgleich</h2>
    {#if status === "loading"}
      <p>Vergleich noch ausstehend.</p>
    {:else if status === "unreachable" || status === "invalid"}
      <p class="error" data-testid="build-sync-state">
        Abgleich nicht möglich – kein verlässlicher Server-Stand.
      </p>
    {:else if inSync}
      <p class="ok" data-testid="build-sync-state">
        Lokaler Bundle-Stand und Server-Stand stimmen überein.
      </p>
    {:else}
      <p class="warn" data-testid="build-sync-state">
        Lokaler Bundle-Stand und Server-Stand unterscheiden sich. Ein Neuladen
        liefert den frischen Build.
      </p>
    {/if}
    <button
      class="btn"
      type="button"
      onclick={refresh}
      data-testid="build-refresh"
    >
      Erneut abfragen
    </button>
  </section>
</div>

<style>
  .back-link {
    display: inline-flex;
    align-items: center;
    width: fit-content;
    min-height: 44px;
    color: var(--text);
  }
  .container {
    max-width: 720px;
    margin: 40px auto;
    padding: 0 20px;
    color: var(--text);
    display: flex;
    flex-direction: column;
    gap: 16px;
  }
  h1 {
    font-size: 1.75rem;
    margin: 0 0 0.25rem;
  }
  h2 {
    font-size: 1.1rem;
    margin: 0 0 0.5rem;
  }
  .intro {
    margin: 0 0 1rem;
    opacity: 0.8;
  }
  .card {
    background: var(--panel, #141a21);
    padding: 20px 24px;
    border-radius: 12px;
    border: 1px solid var(--panel-border, rgba(255, 255, 255, 0.06));
  }
  dl {
    display: grid;
    grid-template-columns: minmax(120px, max-content) 1fr;
    column-gap: 16px;
    row-gap: 6px;
    margin: 0;
  }
  dt {
    font-weight: 600;
    opacity: 0.8;
  }
  dd {
    margin: 0;
    font-family: monospace;
    word-break: break-all;
  }
  .ok {
    color: var(--color-theme-2, #2ecc71);
  }
  .warn {
    color: var(--accent, #ff8c42);
  }
  .error {
    color: var(--color-danger, #ff6b6b);
  }
  .btn {
    margin-top: 0.5rem;
  }
</style>
