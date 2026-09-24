<script lang="ts">
  import { onMount } from "svelte";

  type CellDescriptor = {
    protocol_version: string;
    cell_id: string;
    public_base_url: string;
    active_key: {
      key_id: string;
      algorithm: string;
      public_key: string;
    };
    capabilities: string[];
  };

  type FederatedObject = {
    object_address: string;
    origin_cell_id: string;
    object_kind: "node" | "edge" | "shared-room";
    object_version: number;
    scope: "global";
    payload: Record<string, unknown>;
    deleted: boolean;
    updated_at: string;
  };

  type LoadState = "loading" | "ready" | "unavailable" | "invalid";
  type LookupState = "idle" | "loading" | "found" | "not-found" | "error";

  let descriptor: CellDescriptor | null = $state(null);
  let descriptorState: LoadState = $state("loading");
  let address = $state("");
  let lookupState: LookupState = $state("idle");
  let object: FederatedObject | null = $state(null);

  function isDescriptor(value: unknown): value is CellDescriptor {
    if (!value || typeof value !== "object") return false;
    const candidate = value as Partial<CellDescriptor>;
    return (
      candidate.protocol_version === "wg-federation/1" &&
      typeof candidate.cell_id === "string" &&
      typeof candidate.public_base_url === "string" &&
      Array.isArray(candidate.capabilities) &&
      typeof candidate.active_key?.key_id === "string" &&
      candidate.active_key?.algorithm === "Ed25519"
    );
  }

  function isFederatedObject(value: unknown): value is FederatedObject {
    if (!value || typeof value !== "object") return false;
    const candidate = value as Partial<FederatedObject>;
    return (
      typeof candidate.object_address === "string" &&
      typeof candidate.origin_cell_id === "string" &&
      typeof candidate.object_version === "number" &&
      candidate.scope === "global" &&
      candidate.deleted === false &&
      !!candidate.payload &&
      typeof candidate.payload === "object"
    );
  }

  async function loadDescriptor() {
    descriptorState = "loading";
    descriptor = null;
    try {
      const response = await fetch("/api/federation/v1/cell", {
        cache: "no-store",
      });
      if (!response.ok) {
        descriptorState = "unavailable";
        return;
      }
      const data: unknown = await response.json();
      if (!isDescriptor(data)) {
        descriptorState = "invalid";
        return;
      }
      descriptor = data;
      descriptorState = "ready";
    } catch {
      descriptorState = "unavailable";
    }
  }

  async function lookupObject() {
    const trimmed = address.trim();
    if (!trimmed) return;
    lookupState = "loading";
    object = null;
    try {
      const response = await fetch(
        `/api/federation/v1/objects?address=${encodeURIComponent(trimmed)}`,
        { cache: "no-store" },
      );
      if (response.status === 404) {
        lookupState = "not-found";
        return;
      }
      if (!response.ok) {
        lookupState = "error";
        return;
      }
      const data: unknown = await response.json();
      if (!isFederatedObject(data)) {
        lookupState = "error";
        return;
      }
      object = data;
      lookupState = "found";
    } catch {
      lookupState = "error";
    }
  }

  onMount(loadDescriptor);
</script>

<svelte:head>
  <title>Föderationsdiagnose · commonThing</title>
  <meta
    name="description"
    content="Öffentliche commonThing-Diagnose der internen Zellidentität und global freigegebener Föderationsobjekte."
  />
  <meta name="robots" content="noindex" />
</svelte:head>

<main class="container">
  <header>
    <p class="eyebrow">commonThing OS · Föderation v1</p>
    <h1>Föderationsdiagnose</h1>
    <p class="intro">
      Diese Ansicht prüft die öffentliche Zellgrenze. Sie zeigt keine lokalen,
      privaten oder nur für Nachbarzellen bestimmten Inhalte.
    </p>
  </header>

  <section
    class="card"
    aria-labelledby="cell-heading"
    data-testid="federation-cell"
  >
    <div class="section-heading">
      <h2 id="cell-heading">Öffentliche Zellidentität</h2>
      <button type="button" class="secondary" onclick={loadDescriptor}
        >Neu prüfen</button
      >
    </div>

    {#if descriptorState === "loading"}
      <p data-testid="federation-cell-status">Zellidentität wird geprüft…</p>
    {:else if descriptorState === "unavailable"}
      <p class="error" data-testid="federation-cell-status">
        Föderation ist auf dieser Instanz nicht öffentlich erreichbar.
      </p>
    {:else if descriptorState === "invalid"}
      <p class="error" data-testid="federation-cell-status">
        Die Zellbeschreibung entspricht nicht dem Föderationsvertrag v1.
      </p>
    {:else if descriptor}
      <dl data-testid="federation-cell-details">
        <dt>Zelle</dt>
        <dd data-testid="federation-cell-id">{descriptor.cell_id}</dd>
        <dt>Protokoll</dt>
        <dd>{descriptor.protocol_version}</dd>
        <dt>Öffentliche Basis</dt>
        <dd>{descriptor.public_base_url}</dd>
        <dt>Aktiver Schlüssel</dt>
        <dd>
          {descriptor.active_key.key_id} · {descriptor.active_key.algorithm}
        </dd>
      </dl>
      <ul class="capabilities" aria-label="Föderationsfähigkeiten">
        {#each descriptor.capabilities as capability}
          <li>{capability}</li>
        {/each}
      </ul>
    {/if}
  </section>

  <section class="card" aria-labelledby="object-heading">
    <h2 id="object-heading">Globales Objekt nachschlagen</h2>
    <p class="hint">
      Adressen folgen dem Muster <code>wg://zelle/art/id</code>. Nur global
      veröffentlichte, nicht gelöschte Objekte werden ausgegeben.
    </p>
    <form
      onsubmit={(event) => {
        event.preventDefault();
        void lookupObject();
      }}
    >
      <label for="federation-address">Objektadresse</label>
      <div class="lookup-row">
        <input
          id="federation-address"
          data-testid="federation-address"
          bind:value={address}
          required
          autocomplete="off"
          spellcheck="false"
          placeholder="wg://cell-a.example/node/community-garden"
        />
        <button type="submit" disabled={lookupState === "loading"}
          >Prüfen</button
        >
      </div>
    </form>

    <div
      class="result"
      aria-live="polite"
      data-testid="federation-object-result"
    >
      {#if lookupState === "loading"}
        <p>Objekt wird geprüft…</p>
      {:else if lookupState === "not-found"}
        <p>Kein öffentliches globales Objekt unter dieser Adresse gefunden.</p>
      {:else if lookupState === "error"}
        <p class="error">Die Objektantwort war nicht verlässlich auswertbar.</p>
      {:else if lookupState === "found" && object}
        <dl>
          <dt>Adresse</dt>
          <dd data-testid="federation-object-address">
            {object.object_address}
          </dd>
          <dt>Ursprungszelle</dt>
          <dd>{object.origin_cell_id}</dd>
          <dt>Art</dt>
          <dd>{object.object_kind}</dd>
          <dt>Version</dt>
          <dd data-testid="federation-object-version">
            {object.object_version}
          </dd>
        </dl>
        <pre data-testid="federation-object-payload">{JSON.stringify(
            object.payload,
            null,
            2,
          )}</pre>
      {/if}
    </div>
  </section>

  <aside class="boundary" aria-label="Sicherheitsgrenze">
    <strong>Grenze:</strong> Diese Seite kann keine Nachbarn freischalten, Schlüssel
    ändern oder Quarantäne freigeben. Solche Eingriffe bleiben Betreiberentscheidungen
    außerhalb der öffentlichen Browserfläche.
  </aside>
</main>

<style>
  .container {
    width: min(780px, calc(100% - 32px));
    margin: 40px auto 72px;
    display: grid;
    gap: 18px;
    color: var(--text);
  }

  header,
  .card,
  .boundary {
    min-width: 0;
  }

  h1,
  h2,
  p {
    margin-top: 0;
  }

  h1 {
    margin-bottom: 8px;
    font-size: clamp(1.9rem, 5vw, 2.6rem);
  }

  h2 {
    margin-bottom: 12px;
    font-size: 1.15rem;
  }

  .eyebrow {
    margin-bottom: 6px;
    font-size: 0.78rem;
    font-weight: 700;
    letter-spacing: 0.09em;
    text-transform: uppercase;
    opacity: 0.72;
  }

  .intro,
  .hint {
    max-width: 68ch;
    line-height: 1.55;
    opacity: 0.82;
  }

  .card {
    padding: 22px;
    border: 1px solid var(--panel-border, rgba(255, 255, 255, 0.1));
    border-radius: 14px;
    background: var(--panel, #141a21);
  }

  .section-heading {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 16px;
  }

  dl {
    display: grid;
    grid-template-columns: minmax(130px, max-content) minmax(0, 1fr);
    gap: 7px 16px;
    margin: 0;
  }

  dt {
    font-weight: 650;
    opacity: 0.72;
  }

  dd {
    min-width: 0;
    margin: 0;
    overflow-wrap: anywhere;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }

  .capabilities {
    display: flex;
    flex-wrap: wrap;
    gap: 7px;
    margin: 18px 0 0;
    padding: 0;
    list-style: none;
  }

  .capabilities li {
    padding: 5px 9px;
    border: 1px solid var(--panel-border, rgba(255, 255, 255, 0.1));
    border-radius: 999px;
    font-size: 0.78rem;
  }

  form {
    display: grid;
    gap: 8px;
  }

  label {
    font-weight: 650;
  }

  .lookup-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 10px;
  }

  input {
    min-width: 0;
    padding: 11px 12px;
    border: 1px solid var(--panel-border, rgba(255, 255, 255, 0.16));
    border-radius: 9px;
    color: inherit;
    background: var(--surface, rgba(0, 0, 0, 0.18));
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }

  button {
    padding: 10px 14px;
    border: 0;
    border-radius: 9px;
    font: inherit;
    font-weight: 700;
    cursor: pointer;
  }

  button:disabled {
    cursor: progress;
    opacity: 0.58;
  }

  .secondary {
    padding: 7px 10px;
    border: 1px solid var(--panel-border, rgba(255, 255, 255, 0.16));
    color: inherit;
    background: transparent;
  }

  .result {
    min-height: 28px;
    margin-top: 18px;
  }

  pre {
    max-height: 320px;
    margin: 18px 0 0;
    padding: 14px;
    overflow: auto;
    border-radius: 9px;
    background: rgba(0, 0, 0, 0.24);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

  code {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }

  .error {
    color: var(--color-danger, #ff7d7d);
  }

  .boundary {
    padding: 15px 18px;
    border-left: 3px solid currentColor;
    line-height: 1.5;
    opacity: 0.82;
  }

  @media (max-width: 560px) {
    .container {
      margin-top: 24px;
    }

    .card {
      padding: 18px;
    }

    .lookup-row,
    dl {
      grid-template-columns: 1fr;
    }

    dt:not(:first-child) {
      margin-top: 7px;
    }
  }
</style>
