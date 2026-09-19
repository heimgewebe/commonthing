<script lang="ts">
  import { page } from "$app/stores";

  let token: string | null = $derived($page.url.searchParams.get("token"));
  let challengeId: string | null = $derived(
    $page.url.searchParams.get("challenge_id"),
  );

  // Keep the rendered state correct during SSR, then react only when navigation
  // actually changes the token/challenge pair on the client.
  let lastEvaluatedToken: string | null = $state(
    $page.url.searchParams.get("token"),
  );
  let lastEvaluatedChallengeId: string | null = $state(
    $page.url.searchParams.get("challenge_id"),
  );
  let status: "idle" | "loading" | "success" | "error" | "invalid" = $state(
    $page.url.searchParams.get("token") &&
      $page.url.searchParams.get("challenge_id")
      ? "idle"
      : "invalid",
  );

  $effect.pre(() => {
    if (
      token === lastEvaluatedToken &&
      challengeId === lastEvaluatedChallengeId
    ) {
      return;
    }

    lastEvaluatedToken = token;
    lastEvaluatedChallengeId = challengeId;
    status = token && challengeId ? "idle" : "invalid";
  });

  async function confirm() {
    if (!token || !challengeId) return;
    status = "loading";
    try {
      const res = await fetch("/api/auth/step-up/magic-link/consume", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token, challenge_id: challengeId }),
      });
      if (res.ok) {
        status = "success";
      } else {
        status = "error";
      }
    } catch (e) {
      status = "error";
    }
  }
</script>

<div class="col step-up">
  <div class="panel col step-up__panel">
    <h1>Aktion bestätigen</h1>
    {#if status === "invalid"}
      <div class="step-up__danger">
        Dieser Bestätigungslink ist unvollständig oder ungültig. Bitte fordere
        einen neuen Link an.
      </div>
    {:else if status === "success"}
      <div class="step-up__success">
        Die Aktion wurde erfolgreich bestätigt. Du kannst dieses Fenster nun
        schließen.
      </div>
    {:else if status === "error"}
      <div class="step-up__danger">
        Ein Fehler ist aufgetreten oder der Link ist abgelaufen.
      </div>
    {:else}
      <p>
        Bitte klicke auf den Button, um die angeforderte Aktion freizugeben.
      </p>
      <button class="btn" disabled={status === "loading"} onclick={confirm}>
        {status === "loading" ? "Wird bestätigt..." : "Jetzt bestätigen"}
      </button>
    {/if}
  </div>
</div>

<style>
  /* Keine Inline-Styles: style-src laeuft ohne 'unsafe-inline'. */
  .step-up {
    gap: 1.5rem;
    padding: 1.5rem;
    max-width: 400px;
    margin: 0 auto;
    margin-top: 10vh;
  }

  .step-up__panel {
    gap: 1rem;
  }

  .step-up__danger {
    color: var(--color-danger, #ff6b6b);
  }

  .step-up__success {
    color: var(--color-theme-2, #2ecc71);
  }
</style>
