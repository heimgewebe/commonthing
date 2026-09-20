<script lang="ts">
  const links = [
    {
      href: "/_dev/auth",
      title: "Login / Auth",
      description:
        "Entwickler-Login mit verschiedenen Rollen (Admin, Weber, Gast).",
    },
    {
      href: "/_dev/gestures",
      title: "Swipe Playground",
      description:
        "Teste die Swipe-Action mit konfigurierbaren Schwellenwerten und sieh dir Events live an.",
    },
  ];
</script>

<svelte:head>
  <title>Dev-Werkzeuge</title>
</svelte:head>

<div class="col dev-page">
  <header class="col dev-page__header">
    <h1>Dev-Werkzeuge</h1>
    <p class="ghost dev-page__flush">
      Interne Helferlein für lokale Entwicklung. Diese Seiten sind nicht Teil
      des Produktiv-Builds.
    </p>
  </header>

  <ul class="col dev-page__list">
    {#each links as link}
      <li class="panel col dev-page__item">
        <h2 class="dev-page__item-title">{link.title}</h2>
        <p class="ghost dev-page__flush">{link.description}</p>
        <div class="row dev-page__actions">
          <a class="btn" rel="nofollow" href={link.href}>Öffnen</a>
        </div>
      </li>
    {/each}
  </ul>
</div>

<style>
  /* Keine Inline-Styles: style-src laeuft ohne 'unsafe-inline'. */
  .dev-page {
    gap: 1.5rem;
    padding: 1.5rem;
    max-width: 720px;
    margin: 0 auto;
  }

  .dev-page__header {
    gap: 0.25rem;
  }

  .dev-page__flush {
    margin: 0;
  }

  .dev-page__list {
    gap: 1rem;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .dev-page__item {
    gap: 0.5rem;
  }

  .dev-page__item-title {
    margin: 0;
    font-size: 1.1rem;
  }

  .dev-page__actions {
    justify-content: flex-end;
  }
</style>
