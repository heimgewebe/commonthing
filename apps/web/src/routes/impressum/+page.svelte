<svelte:head>
  <title>Impressum – commonThing</title>
  <meta
    name="description"
    content="Anbieterkennzeichnung und Kontakt für commonThing."
  />
</svelte:head>

<main class="legal-page">
  <h1>Impressum</h1>
  <p>Angaben gemäß § 5 DDG</p>
  <address>
    Alexander Mohr<br />
    Huskoppelallee 13<br />
    23795 Klein Rönnau<br />
    Deutschland
  </address>

  <h2>Kontakt</h2>
  <p>
    E-Mail: <a href="mailto:kontakt@commonthing.net">kontakt@commonthing.net</a
    ><br />
    Telefon: <a href="tel:+4915563658682">+49 155 63658682</a>
  </p>

  <p>
    <a href="/datenschutz">Datenschutzerklärung</a> ·
    <a href="/map">Zur Karte</a>
  </p>
</main>

<style>
  .legal-page {
    max-width: 48rem;
    margin: 0 auto;
    padding: 2rem 1.25rem 4rem;
    font:
      1rem/1.6 system-ui,
      sans-serif;
  }
  h1,
  h2 {
    line-height: 1.2;
  }
  address {
    font-style: normal;
  }
  a {
    color: inherit;
    text-decoration-thickness: 0.1em;
    text-underline-offset: 0.15em;
  }
</style>
