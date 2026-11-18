<script lang="ts">
  import { onMount } from "svelte";
  import {
    accountAttentionRuntime,
    retainAccountAttentionRuntime,
  } from "$lib/accountAttentionRuntime";
  import { authStore } from "$lib/auth/store";
  import { deriveTopBarAuthView } from "./topBarAuthState";

  let authView = $derived.by(() => deriveTopBarAuthView($authStore));
  let weberApplicationState = $derived.by(
    () => $accountAttentionRuntime.weberApplicationState,
  );
  let pendingWeberApplication = $derived.by(
    () => weberApplicationState === "pending",
  );
  let guestBadgeHref = $derived.by(() =>
    weberApplicationState === "available"
      ? "/antraege#antrag-stellen"
      : "/antraege",
  );
  let guestBadgeAction = $derived.by(() =>
    weberApplicationState === "pending"
      ? "Weberstatus beantragt"
      : weberApplicationState === "available"
        ? "Weber werden"
        : "Weberstatus wird geprüft",
  );
  let guestBadgeTitle = $derived.by(() =>
    weberApplicationState === "pending"
      ? "Weberstatus beantragt – Antrag ansehen"
      : weberApplicationState === "available"
        ? "Weberstatus beantragen"
        : "Weberstatus wird geprüft",
  );
  let guestBadgeCompactSymbol = $derived.by(() =>
    weberApplicationState === "pending"
      ? "◌"
      : weberApplicationState === "available"
        ? "G"
        : "…",
  );

  function retryAuth() {
    void authStore.checkAuth({ force: true });
  }

  let menuEl: HTMLDetailsElement | undefined = $state();

  function closeMenuOnOutsideClick(event: PointerEvent) {
    if (menuEl?.open && !menuEl.contains(event.target as Node)) {
      menuEl.open = false;
    }
  }

  function closeMenuOnEscape(event: KeyboardEvent) {
    if (!menuEl?.open || event.key !== "Escape") return;
    event.preventDefault();
    menuEl.open = false;
    menuEl.querySelector("summary")?.focus();
  }

  onMount(() => retainAccountAttentionRuntime());
</script>

<svelte:window
  onpointerdown={closeMenuOnOutsideClick}
  onkeydown={closeMenuOnEscape}
/>

<div class="auth-slot">
  <details class="site-menu" bind:this={menuEl} data-testid="site-menu">
    <summary data-testid="site-menu-trigger" aria-controls="site-menu-links">
      Menü <span aria-hidden="true" class="menu-chevron">⌄</span>
    </summary>
    <nav id="site-menu-links" class="site-menu-links" aria-label="Hauptmenü">
      <a href="/map">Karte</a>
      {#if authView.showAccountLink}
        <a href="/nachrichten">Nachrichten</a>
        <a href="/settings#meine-garnrolle">Mein Profil (Garnrolle)</a>
      {/if}
      <a href="/antraege">Anträge und Entscheidungen</a>
      <a href="/settings">Einstellungen</a>
    </nav>
  </details>

  {#if authView.showAccountLink}
    <a
      class="message-entry"
      href="/nachrichten"
      aria-label="Private Nachrichten"
      title="Private Nachrichten"
    >
      <span class="message-icon" aria-hidden="true">✉</span>
      <span class="message-label">Nachrichten</span>
    </a>
  {/if}

  {#if authView.isGuest}
    <a
      class:pending={pendingWeberApplication}
      class="guest-badge"
      href={guestBadgeHref}
      data-state={weberApplicationState}
      data-testid="topbar-guest-badge"
      aria-label={`Rolle: Gast – ${guestBadgeAction}`}
      title={guestBadgeTitle}
    >
      <span class="guest-badge-role">Gast</span>
      <span class="guest-badge-cta">{guestBadgeAction}</span>
      <span class="guest-badge-compact" aria-hidden="true">
        {guestBadgeCompactSymbol}
      </span>
    </a>
  {/if}

  {#if authView.showAccountLink}
    {#if authView.showRetry}
      <button
        type="button"
        class="auth-retry"
        onclick={retryAuth}
        aria-label="Anmeldung erneut prüfen"
      >
        <span class="auth-symbol" aria-hidden="true">↻</span>
        <span class="auth-label">{authView.retryLabel}</span>
      </button>
    {/if}
  {:else if authView.showLoginLink}
    <a class="login-entry" href="/login" aria-label="Anmelden">
      <span class="auth-symbol" aria-hidden="true">↪</span>
      <span class="auth-label">Anmelden</span>
    </a>
  {:else}
    <button
      type="button"
      class="login-entry"
      onclick={retryAuth}
      aria-label="Anmeldung erneut prüfen"
    >
      <span class="auth-symbol" aria-hidden="true">↻</span>
      <span class="auth-label">{authView.retryLabel}</span>
    </button>
  {/if}
</div>

<style>
  .auth-slot {
    grid-column: 3;
    justify-self: end;
    display: flex;
    align-items: center;
    gap: 0.35rem;
    pointer-events: auto;
  }

  .site-menu {
    position: relative;
    flex: 0 0 auto;
    color: var(--text);
  }

  .site-menu summary {
    box-sizing: border-box;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 0.4rem;
    min-height: 44px;
    padding: 0 0.8rem;
    border: 1px solid var(--panel-border-strong);
    border-radius: 999px;
    background: var(--panel-solid);
    color: var(--text);
    font: inherit;
    font-weight: 700;
    cursor: pointer;
    list-style: none;
  }

  .site-menu summary::-webkit-details-marker {
    display: none;
  }

  .menu-chevron {
    font-size: 1.15rem;
    line-height: 1;
  }

  .site-menu[open] .menu-chevron {
    transform: rotate(180deg);
  }

  .site-menu-links {
    box-sizing: border-box;
    position: absolute;
    top: calc(100% + 0.5rem);
    right: 0;
    display: grid;
    gap: 0.15rem;
    width: min(20rem, calc(100vw - 24px));
    max-height: calc(100dvh - var(--toolbar-offset) - 1rem);
    overflow-y: auto;
    padding: 0.45rem;
    border: 1px solid var(--panel-border-strong);
    border-radius: 14px;
    background: var(--panel-solid);
    box-shadow: var(--shadow);
    z-index: 1;
  }

  .site-menu-links a {
    display: flex;
    align-items: center;
    min-height: 44px;
    padding: 0.25rem 0.75rem;
    border-radius: 9px;
    color: var(--text);
    font-weight: 600;
    text-decoration: none;
  }

  .site-menu-links a:hover,
  .site-menu-links a:focus-visible {
    background: var(--accent-soft);
  }

  @media (max-width: 768px) {
    /* The guest badge can move the trigger away from the screen edge.
     * Anchor the expanded navigation to the viewport, not that trigger. */
    .site-menu-links {
      position: fixed;
      top: calc(var(--toolbar-offset) + env(safe-area-inset-top) + 0.5rem);
      right: 12px;
    }
  }

  .site-menu summary:focus-visible,
  .site-menu-links a:focus-visible,
  .message-entry:focus-visible,
  .login-entry:focus-visible,
  .auth-retry:focus-visible,
  .guest-badge:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 3px;
  }

  .guest-badge {
    box-sizing: border-box;
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    min-height: 44px;
    padding: 0 0.75rem;
    border: 1px solid var(--accent);
    border-radius: 999px;
    background: var(--accent-soft);
    color: var(--accent);
    text-decoration: none;
    font: inherit;
    font-weight: 700;
    white-space: nowrap;
  }

  .guest-badge-role {
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  .guest-badge-cta {
    font-size: 0.75rem;
    opacity: 0.9;
  }

  .guest-badge-compact {
    display: none;
    font-size: 0.9rem;
    line-height: 1;
  }

  .message-entry,
  .login-entry,
  .auth-retry {
    display: grid;
    place-items: center;
    min-height: 44px;
    padding: 0 0.9rem;
    border: 1px solid var(--panel-border-strong);
    border-radius: 999px;
    background: var(--panel);
    color: var(--text);
    text-decoration: none;
    font: inherit;
    font-weight: 600;
  }

  .message-entry {
    grid-auto-flow: column;
    gap: 0.35rem;
    padding-inline: 0.65rem;
    white-space: nowrap;
  }

  .message-icon {
    font-size: 1.15rem;
    line-height: 1;
  }

  .auth-symbol {
    display: none;
    font-size: 1.15rem;
    line-height: 1;
  }

  .auth-retry {
    min-height: 36px;
    max-width: 9rem;
    padding: 0 0.65rem;
    font-size: 0.75rem;
  }

  @media (max-width: 768px) {
    .message-entry {
      box-sizing: border-box;
      width: 44px;
      min-width: 44px;
      padding: 0;
    }

    .message-label {
      display: none;
    }

    .guest-badge {
      box-sizing: border-box;
      width: 44px;
      min-width: 44px;
      gap: 0;
      justify-content: center;
      padding: 0;
    }

    .guest-badge-role,
    .guest-badge-cta {
      display: none;
    }

    .guest-badge-compact {
      display: inline;
    }
  }

  @media (max-width: 510px) {
    .auth-slot {
      gap: 0.125rem;
    }

    .site-menu summary {
      gap: 0;
      padding-inline: 0.45rem;
    }

    .menu-chevron {
      display: none;
    }

    .login-entry,
    .auth-retry {
      box-sizing: border-box;
      width: 44px;
      min-width: 44px;
      min-height: 44px;
      padding: 0;
    }

    .auth-symbol,
    .guest-badge-compact {
      display: inline;
    }

    .auth-label,
    .guest-badge-role,
    .guest-badge-cta {
      display: none;
    }

    .guest-badge {
      box-sizing: border-box;
      width: 44px;
      min-width: 44px;
      gap: 0;
      justify-content: center;
      padding: 0;
    }
  }
</style>
