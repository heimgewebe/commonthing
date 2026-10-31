<script lang="ts">
  import YarnWord from "$lib/components/YarnWord.svelte";

  let email = $state("");
  let error: string | null = $state(null);
  let success = $state(false);
  let loading = $state(false);

  async function handleSubmit() {
    loading = true;
    error = null;
    success = false;
    try {
      const { authStore } = await import("$lib/auth/store");
      await authStore.requestLogin(email);
      success = true;
    } catch (e) {
      if (e instanceof Error && e.message.includes("disabled")) {
        error = "Öffentlicher Login ist derzeit deaktiviert.";
      } else {
        error = "Login fehlgeschlagen. Bitte versuche es erneut.";
      }
      console.error(e);
    } finally {
      loading = false;
    }
  }
</script>

<svelte:head>
  <title>Login</title>
</svelte:head>

<main class="wg-page" data-testid="login-page">
  <div class="wg-page__shell wg-page__shell--narrow">
    <section
      class="wg-card wg-card--padded wg-stack"
      aria-labelledby="login-heading"
    >
      <header class="wg-stack">
        <p class="wg-eyebrow">Gewebekonto</p>
        <h1 id="login-heading" class="wg-title wg-title--compact">
          <YarnWord word="Login" />
        </h1>
      </header>

      {#if success}
        <div class="wg-state wg-state--success" role="status">
          <p><strong>Postfach prüfen!</strong></p>
          <p>
            Falls deine E-Mail registriert ist, haben wir dir einen Login-Link
            gesendet.
          </p>
        </div>
      {:else}
        <form
          onsubmit={(event) => {
            event.preventDefault();
            void handleSubmit();
          }}
          class="wg-stack"
        >
          <div class="wg-field">
            <label class="wg-label" for="email">E-Mail</label>
            <input
              class="wg-control"
              id="email"
              type="email"
              bind:value={email}
              placeholder="du@example.com"
              autocomplete="email"
              required
              disabled={loading}
              aria-describedby={error ? "login-error" : undefined}
            />
          </div>

          {#if error}
            <div id="login-error" class="wg-state wg-state--error" role="alert">
              {error}
            </div>
          {/if}

          <div class="wg-actions">
            <button
              type="submit"
              class="wg-button wg-button--primary"
              disabled={loading || !email}
            >
              {#if loading}Wird gesendet…{:else}Login-Link senden{/if}
            </button>
          </div>
        </form>
      {/if}
    </section>
  </div>
</main>

<style>
  .wg-page {
    min-height: 100vh;
    min-height: 100dvh;
    box-sizing: border-box;
    padding: clamp(1rem, 4vw, 2rem) 0 clamp(3rem, 8vw, 5rem);
    background:
      radial-gradient(circle at 12% 0, var(--accent-soft), transparent 34rem),
      var(--bg);
    color: var(--text);
  }

  .wg-page *,
  .wg-page *::before,
  .wg-page *::after {
    box-sizing: border-box;
  }

  .wg-page__shell {
    margin-inline: auto;
  }

  .wg-page__shell--narrow {
    width: min(30rem, calc(100% - 2rem));
    padding-top: clamp(2rem, 10vh, 6rem);
  }

  .wg-eyebrow {
    margin: 0;
    color: var(--accent);
    font-size: 0.76rem;
    font-weight: 750;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }

  .wg-title {
    margin: 0;
    line-height: 0.98;
    letter-spacing: -0.04em;
  }

  .wg-title--compact {
    font-size: clamp(2rem, 8vw, 3rem);
  }

  .wg-stack {
    display: grid;
    gap: 1rem;
  }

  .wg-card {
    border: 1px solid var(--panel-border);
    border-radius: var(--radius);
    background: color-mix(in srgb, var(--panel-solid) 94%, transparent);
    background-image: var(--context-weave);
    box-shadow: var(--shadow);
  }

  .wg-card--padded {
    padding: clamp(1.25rem, 4vw, 2rem);
  }

  .wg-field {
    display: grid;
    gap: 0.45rem;
  }

  .wg-label {
    color: var(--muted);
    font-size: 0.9rem;
    font-weight: 700;
  }

  .wg-control {
    width: 100%;
    min-height: 46px;
    padding: 0.75rem 0.85rem;
    border: 1px solid var(--panel-border-strong);
    border-radius: 12px;
    background: var(--panel-solid);
    color: var(--text);
    font: inherit;
  }

  .wg-button:focus-visible,
  .wg-control:focus-visible {
    outline: 3px solid var(--accent);
    outline-offset: 2px;
  }

  .wg-control::placeholder {
    color: color-mix(in srgb, var(--muted) 70%, transparent);
  }

  .wg-control:disabled {
    opacity: 0.6;
  }

  .wg-actions {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
  }

  .wg-actions {
    justify-content: flex-end;
  }

  .wg-button {
    width: fit-content;
    min-height: 44px;
    padding: 0.65rem 1rem;
    border: 1px solid transparent;
    border-radius: 999px;
    font: inherit;
    font-weight: 720;
    cursor: pointer;
  }

  .wg-button--primary {
    background: var(--accent);
    color: var(--bg);
  }

  .wg-button:disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }

  .wg-state {
    margin: 0;
    padding: 0.9rem 1rem;
    border: 1px solid var(--panel-border);
    border-radius: 12px;
    background: color-mix(in srgb, var(--panel-solid) 92%, transparent);
  }

  .wg-state p {
    margin: 0;
  }

  .wg-state p + p {
    margin-top: 0.4rem;
  }

  .wg-state--success {
    border-color: color-mix(in srgb, var(--accent) 55%, transparent);
    background: color-mix(in srgb, var(--accent) 12%, var(--panel-solid));
  }

  .wg-state--error {
    border-color: color-mix(in srgb, var(--danger) 55%, transparent);
    background: color-mix(in srgb, var(--danger) 10%, var(--panel-solid));
    color: var(--danger);
  }
  @media (max-width: 560px) {
    .wg-button--primary,
    .wg-actions .wg-button {
      width: 100%;
    }
  }
</style>
