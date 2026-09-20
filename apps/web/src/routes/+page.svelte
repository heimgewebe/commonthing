<script lang="ts">
  interface Props {
    data: unknown;
  }

  let { data }: Props = $props();
</script>

{#if false}{data}{/if}

<div class="dev-root">
  <h1>commonThing Dev Root</h1>
  <p>Der Dev-Server läuft erfolgreich.</p>

  <p>
    <a class="dev-root__cta" href="/map"> Karte öffnen (/map) </a>
  </p>

  <div class="dev-root__note">
    <p class="dev-root__note-title"><strong>Warum sehe ich das?</strong></p>
    <p class="dev-root__note-body">
      In Production wird automatisch nach <code>/map</code> weitergeleitet. Im Dev-Modus
      zeigen wir diese Seite, um Probleme mit automatischen Redirects in Codespaces
      oder Safari zu vermeiden.
    </p>
  </div>
</div>

<style>
  /* Keine Inline-Styles: style-src laeuft ohne 'unsafe-inline'. */
  .dev-root {
    padding: 2rem;
    font-family: sans-serif;
    max-width: 600px;
    margin: 0 auto;
  }

  .dev-root__cta {
    display: inline-block;
    background: #0070f3;
    color: white;
    padding: 1rem 2rem;
    text-decoration: none;
    border-radius: 4px;
    font-weight: bold;
  }

  .dev-root__note {
    margin-top: 2rem;
    padding: 1rem;
    background: #f5f5f5;
    border-radius: 4px;
    color: #666;
    font-size: 0.9em;
  }

  .dev-root__note-title {
    margin: 0 0 0.5rem 0;
  }

  .dev-root__note-body {
    margin: 0;
  }
</style>
