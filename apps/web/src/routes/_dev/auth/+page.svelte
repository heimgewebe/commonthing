<script lang="ts">
  import { onMount } from "svelte";
  import { authStore } from "$lib/auth/store";

  interface DevAccount {
    id: string;
    title: string;
    role: string;
    summary?: string;
  }

  let accounts: DevAccount[] = $state([]);
  let error: string | null = $state(null);
  let loading = $state(true);

  onMount(async () => {
    try {
      const res = await fetch("/api/auth/dev/accounts");
      if (res.ok) {
        accounts = await res.json();
      } else if (res.status === 404) {
        error = "Dev login disabled (AUTH_DEV_LOGIN=0).";
      } else {
        error = `Failed to load accounts: ${res.status}`;
      }
    } catch (e) {
      error = String(e);
    } finally {
      loading = false;
    }
  });

  async function login(id: string) {
    try {
      await authStore.devLogin(id);
      // Reactivity handles UI update
    } catch (e) {
      error = "Login failed: " + String(e);
    }
  }

  async function logout() {
    await authStore.logout();
    // Reactivity handles UI update
  }
</script>

<svelte:head>
  <title>Dev Login</title>
</svelte:head>

<div class="col dev-page">
  <header class="col dev-page__header">
    <h1>Dev Login</h1>
    <p class="ghost">
      Wähle einen Account zum Einloggen. Nur verfügbar wenn AUTH_DEV_LOGIN=1.
    </p>

    {#if $authStore.authenticated}
      <div class="panel row dev-page__session">
        <div class="col">
          <strong>Angemeldet als:</strong>
          <span>{$authStore.role} (Account: {$authStore.account_id})</span>
        </div>
        <button class="btn" onclick={logout}>Logout</button>
      </div>
    {/if}
  </header>

  {#if loading}
    <p>Lade Accounts...</p>
  {:else if error}
    <div class="panel dev-page__error">
      Error: {error}
    </div>
  {:else if accounts.length === 0}
    <p>Keine Accounts gefunden.</p>
  {:else}
    <ul class="col dev-page__list">
      {#each accounts as account}
        <li class="panel col dev-page__item">
          <div class="row dev-page__item-head">
            <div class="col">
              <h2 class="dev-page__item-title">{account.title}</h2>
              <code class="dev-page__item-id">{account.id}</code>
            </div>
            <span class="badge">{account.role}</span>
          </div>

          {#if account.summary}
            <p>{account.summary}</p>
          {/if}

          <div class="row dev-page__actions">
            <button
              class="btn"
              onclick={() => login(account.id)}
              disabled={$authStore.authenticated &&
                $authStore.account_id === account.id}
            >
              {#if $authStore.authenticated && $authStore.account_id === account.id}
                Aktuell
              {:else}
                Login als {account.role}
              {/if}
            </button>
          </div>
        </li>
      {/each}
    </ul>
  {/if}
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
    gap: 0.5rem;
  }

  .dev-page__session {
    align-items: center;
    justify-content: space-between;
    border-color: var(--color-theme-1);
  }

  .dev-page__error {
    border-color: var(--color-danger);
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

  .dev-page__item-head {
    justify-content: space-between;
    align-items: flex-start;
  }

  .dev-page__item-title {
    margin: 0;
    font-size: 1.1rem;
  }

  .dev-page__item-id {
    font-size: 0.8rem;
    opacity: 0.7;
  }

  .dev-page__actions {
    justify-content: flex-end;
  }

  .badge {
    background: var(--color-bg-2);
    padding: 0.2rem 0.5rem;
    border-radius: 4px;
    font-size: 0.8rem;
    font-weight: bold;
    text-transform: uppercase;
  }
</style>
