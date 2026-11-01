<script lang="ts">
  import YarnWord from "$lib/components/YarnWord.svelte";
  import { onMount } from "svelte";
  import MyGarnrolleSection from "$lib/components/MyGarnrolleSection.svelte";
  import type { PageData } from "./$types";

  interface Props {
    data: PageData;
  }

  let { data }: Props = $props();
  let NotificationSettings:
    | typeof import("$lib/components/NotificationSettings.svelte").default
    | null = $state(null);
  let AccountSection:
    | typeof import("$lib/components/AccountSection.svelte").default
    | null = $state(null);
  let notificationLoadFailed = $state(false);
  let accountLoadFailed = $state(false);

  async function loadNotificationSettings(): Promise<void> {
    notificationLoadFailed = false;
    try {
      const module =
        await import("$lib/components/NotificationSettings.svelte");
      NotificationSettings = module.default;
    } catch {
      notificationLoadFailed = true;
    }
  }

  async function loadAccountSection(): Promise<void> {
    accountLoadFailed = false;
    try {
      const module = await import("$lib/components/AccountSection.svelte");
      AccountSection = module.default;
    } catch {
      accountLoadFailed = true;
    }
  }

  onMount(() => {
    void loadNotificationSettings();
    void loadAccountSection();
  });
</script>

<svelte:head>
  <title>Einstellungen – commonThing</title>
</svelte:head>

<div class="settings-page">
  <div class="container">
    <header class="page-header">
      <a class="back-link touch-target" href="/map">← Zur Karte</a>
      <h1><YarnWord word="Einstellungen" /></h1>
      <p class="intro">
        Verwalte deine Garnrolle, dein Konto, Benachrichtigungen und die
        Darstellung an einem übersichtlichen Ort.
      </p>
    </header>

    <div class="settings-layout">
      <aside
        class="panel settings-menu"
        aria-label="Einstellungsmenü"
        data-testid="settings-menu"
      >
        <div class="col">
          <p class="menu-heading"><strong>Bereiche</strong></p>
          <nav class="menu-links" aria-label="Einstellungsbereiche">
            <a href="#meine-garnrolle">
              <strong>Meine Garnrolle</strong>
              <span>Profil, Kartenanker und Sichtbarkeit</span>
            </a>
            <a href="#benachrichtigungen">
              <strong>Benachrichtigungen</strong>
              <span>Push-Hinweise und dieses Gerät</span>
            </a>
            <a href="#konto-und-sicherheit">
              <strong>Konto &amp; Sicherheit</strong>
              <span>Sitzungen, Geräte und Anmeldung</span>
            </a>
          </nav>
        </div>

        <div class="col appearance-section">
          <label class="menu-heading" for="theme-select"
            ><strong>Darstellung</strong></label
          >
          <p class="menu-hint">
            „System“ folgt deinem Gerät. Die Auswahl gilt nur in diesem Browser.
          </p>
          <select
            id="theme-select"
            class="btn touch-target"
            data-wg-theme-control
            data-testid="theme-select"
          >
            <option value="system">System</option>
            <option value="light">Hell</option>
            <option value="dark">Dunkel</option>
          </select>
          <div class="col">
            <label class="row touch-target" for="wool-experiment-toggle">
              <input
                id="wool-experiment-toggle"
                type="checkbox"
                data-wg-wool-control
                data-testid="wool-experiment-toggle"
              />
              <span>Wollschrift testen (Experiment)</span>
            </label>
            <p class="menu-hint">
              Echte Wollbuchstaben für Überschriften und kurze Navigation.
              Kleine Texte, Eingaben und gefüllte Schaltflächen bleiben lesbar.
              Nur in diesem Browser, jederzeit abschaltbar. Kartenbeschriftungen
              im Kartenbild bleiben unverändert.
            </p>
          </div>
        </div>

        <a
          class="diagnostics-link touch-target"
          href="/build"
          data-testid="build-diagnostics-link"
        >
          Technische Build-Diagnose
        </a>
      </aside>

      <main class="col settings-content">
        <section class="panel primary-card" aria-label="Meine Garnrolle">
          <MyGarnrolleSection
            accounts={data.accounts}
            accountsLoadError={data.accountsLoadError}
          />
        </section>

        <div class="panel notification-card">
          {#if NotificationSettings}
            <NotificationSettings />
          {:else if notificationLoadFailed}
            <div id="benachrichtigungen" class="lazy-status" role="alert">
              <p>Benachrichtigungen konnten nicht geladen werden.</p>
              <button
                class="btn secondary touch-target"
                type="button"
                onclick={loadNotificationSettings}>Erneut versuchen</button
              >
            </div>
          {:else}
            <p id="benachrichtigungen" class="lazy-status" role="status">
              Benachrichtigungen werden geladen …
            </p>
          {/if}
        </div>

        <div id="konto-und-sicherheit" class="panel">
          {#if AccountSection}
            <AccountSection />
          {:else if accountLoadFailed}
            <div class="lazy-status" role="alert">
              <p>Konto &amp; Sicherheit konnte nicht geladen werden.</p>
              <button
                class="btn secondary touch-target"
                type="button"
                onclick={loadAccountSection}>Erneut versuchen</button
              >
            </div>
          {:else}
            <p class="lazy-status" role="status">
              Konto &amp; Sicherheit wird geladen …
            </p>
          {/if}
        </div>
      </main>
    </div>
  </div>
</div>

<style>
  .settings-page {
    min-height: 100dvh;
    padding: clamp(1rem, 3vw, 2.5rem) 1rem 4rem;
  }

  .container {
    max-width: 75rem;
    margin: auto;
  }

  .page-header {
    display: grid;
    margin-bottom: clamp(1.5rem, 4vw, 2.5rem);
  }

  .page-header h1,
  .intro,
  .menu-heading,
  .menu-hint,
  .lazy-status p {
    margin: 0;
  }

  .settings-layout {
    display: grid;
    grid-template-columns: minmax(15rem, 18rem) minmax(0, 1fr);
    align-items: start;
    gap: clamp(1rem, 3vw, 2rem);
  }

  .settings-menu {
    position: sticky;
    top: 1rem;
    display: grid;
    gap: 1rem;
  }

  .menu-links a {
    display: grid;
    padding: 0.6rem;
    color: var(--text);
    text-decoration: none;
  }

  .menu-links span,
  .menu-hint,
  .diagnostics-link {
    color: var(--muted);
    font-size: 0.82rem;
  }

  .back-link,
  .diagnostics-link {
    display: inline-flex;
    align-items: center;
    width: fit-content;
  }

  .touch-target {
    min-height: 44px;
  }

  .primary-card,
  .notification-card {
    padding: 0;
  }

  .primary-card :global(.my-garnrolle) {
    padding: clamp(1rem, 3vw, 1.5rem);
  }

  .lazy-status {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 1rem;
    padding: clamp(1rem, 3vw, 1.5rem);
  }

  @media (max-width: 860px) {
    .settings-layout {
      grid-template-columns: 1fr;
    }

    .settings-menu {
      position: static;
    }
  }

  @media (max-width: 620px) {
    .lazy-status {
      align-items: stretch;
      flex-direction: column;
    }
  }
</style>
