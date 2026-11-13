<script lang="ts">
  import { onMount } from "svelte";
  import PushDeviceManager from "$lib/components/PushDeviceManager.svelte";
  import { authStore } from "$lib/auth/store";
  import {
    applicationServerKey,
    deletePushSubscription,
    getNotificationPreferences,
    getPushConfig,
    registerPushSubscription,
    updateNotificationPreferences,
    type NotificationPreferences,
    type PushConfig,
  } from "$lib/api/notifications";
  import {
    PUSH_PERMISSION_BLOCKED,
    describeNotificationError,
  } from "$lib/notifications/feedback";

  let supported = $state(false);
  let loading = $state(true);
  let savingPreference = $state(false);
  let changingDevice = $state(false);
  let config: PushConfig | null = $state(null);
  let preferences: NotificationPreferences = $state({
    direct_messages_push: false,
  });
  let browserSubscription: PushSubscription | null = $state(null);
  let currentServerRegistration: boolean | null = $state(null);
  let deviceManagerRefreshVersion = $state(0);
  let currentDeviceEnabled = $derived(
    browserSubscription !== null && currentServerRegistration !== false,
  );
  let localCleanupAvailable = $derived(
    browserSubscription !== null && currentServerRegistration === false,
  );
  let permission: NotificationPermission | "unsupported" =
    $state("unsupported");
  let error = $state("");
  let warning = $state("");
  let notice = $state("");
  let sectionElement: HTMLElement | null = null;

  function clearFeedback(): void {
    error = "";
    warning = "";
    notice = "";
  }

  function refreshPermission(): void {
    if (!supported || document.visibilityState === "hidden") return;
    permission = Notification.permission;
    if (permission !== "denied" && warning === PUSH_PERMISSION_BLOCKED) {
      warning = "";
    }
  }

  function focusDeepLink(): void {
    if (window.location.hash !== "#benachrichtigungen") return;
    sectionElement?.focus({ preventScroll: true });
  }

  function handleCurrentRegistrationChange(registered: boolean | null): void {
    currentServerRegistration = registered;
  }

  async function loadBrowserSubscription(): Promise<void> {
    const registration = await navigator.serviceWorker.getRegistration("/");
    browserSubscription =
      (await registration?.pushManager.getSubscription()) ?? null;
  }

  async function load(): Promise<void> {
    loading = true;
    clearFeedback();
    const controller = new AbortController();
    try {
      [config, preferences] = await Promise.all([
        getPushConfig(controller.signal),
        getNotificationPreferences(controller.signal),
      ]);
      await loadBrowserSubscription();
      refreshPermission();
    } catch (cause) {
      config = null;
      error = describeNotificationError(cause, "load");
    } finally {
      loading = false;
    }
  }

  async function retryAuth(): Promise<void> {
    clearFeedback();
    loading = true;
    const status = await authStore.checkAuth({ force: true });
    if (status.state === "authenticated" && status.authenticated) {
      if (supported) {
        await load();
        return;
      }
      loading = false;
      return;
    }
    loading = false;
  }

  async function changePreference(event: Event): Promise<void> {
    const checked = (event.currentTarget as HTMLInputElement).checked;
    const previous = preferences.direct_messages_push;
    preferences = { ...preferences, direct_messages_push: checked };
    savingPreference = true;
    clearFeedback();
    try {
      preferences = await updateNotificationPreferences(checked);
      notice = checked
        ? browserSubscription
          ? "Push-Hinweise für private Nachrichten sind für dein Konto eingeschaltet."
          : "Push-Hinweise sind für dein Konto eingeschaltet. Aktiviere dieses Gerät, um sie hier zu empfangen."
        : "Push-Hinweise für private Nachrichten sind für dein Konto ausgeschaltet.";
    } catch (cause) {
      preferences = { ...preferences, direct_messages_push: previous };
      error = describeNotificationError(cause, "preference");
    } finally {
      savingPreference = false;
    }
  }

  async function enableCurrentDevice(): Promise<void> {
    if (changingDevice || !config?.enabled || !config.application_server_key)
      return;
    if (permission === "denied") {
      warning = PUSH_PERMISSION_BLOCKED;
      return;
    }

    changingDevice = true;
    clearFeedback();
    let createdSubscription: PushSubscription | null = null;
    let cleanupEndpoint: string | null = null;
    try {
      permission = await Notification.requestPermission();
      if (permission !== "granted") {
        warning =
          permission === "denied"
            ? PUSH_PERMISSION_BLOCKED
            : "Benachrichtigungen wurden nicht freigegeben. Ohne Freigabe bleibt Push auf diesem Gerät aus.";
        return;
      }

      const registration = await navigator.serviceWorker.register("/sw.js", {
        scope: "/",
      });
      await navigator.serviceWorker.ready;
      const existing = await registration.pushManager.getSubscription();
      createdSubscription =
        existing ??
        (await registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: applicationServerKey(
            config.application_server_key,
          ),
        }));
      cleanupEndpoint = createdSubscription.endpoint;
      await registerPushSubscription(createdSubscription);
      preferences = await updateNotificationPreferences(true);
      currentServerRegistration = true;
      browserSubscription = createdSubscription;
      deviceManagerRefreshVersion += 1;
      notice =
        "Push ist auf diesem Gerät aktiviert. Push-Hinweise für private Nachrichten sind für dein Konto eingeschaltet.";
    } catch (cause) {
      if (createdSubscription && cleanupEndpoint) {
        try {
          await deletePushSubscription(cleanupEndpoint);
          currentServerRegistration = false;
          deviceManagerRefreshVersion += 1;
          await createdSubscription.unsubscribe().catch(() => false);
          await loadBrowserSubscription();
          if (browserSubscription) {
            warning =
              "Die fehlgeschlagene Einrichtung wurde auf dem Server zurückgenommen, aber das lokale Browser-Abo konnte nicht beendet werden. Push bleibt auf diesem Gerät aus; versuche die lokale Deaktivierung erneut.";
          }
        } catch (cleanupCause) {
          currentServerRegistration = null;
          browserSubscription = createdSubscription;
          warning = `Die Einrichtung ist fehlgeschlagen und konnte nicht vollständig zurückgenommen werden. Das Geräte-Abo bleibt sichtbar, damit du die Deaktivierung erneut versuchen kannst. ${describeNotificationError(
            cleanupCause,
            "device-disable",
          )}`;
        }
      }
      error = describeNotificationError(cause, "device-enable");
    } finally {
      changingDevice = false;
    }
  }

  async function disableCurrentDevice(): Promise<void> {
    if (changingDevice || !browserSubscription) return;
    changingDevice = true;
    clearFeedback();
    const subscription = browserSubscription;
    try {
      // Server first: if this request fails, keep the browser subscription so
      // the endpoint remains recoverable after a reload and the user can retry.
      await deletePushSubscription(subscription.endpoint);
      currentServerRegistration = false;
      deviceManagerRefreshVersion += 1;

      await subscription.unsubscribe().catch(() => false);
      await loadBrowserSubscription();
      if (browserSubscription) {
        warning =
          "Der Geräte-Eintrag ist auf dem Server entfernt, aber das lokale Browser-Abo konnte nicht beendet werden. Versuche „Lokales Browser-Abo entfernen“ erneut.";
        return;
      }
      notice = "Push ist auf diesem Gerät deaktiviert.";
    } catch (cause) {
      error = describeNotificationError(cause, "device-disable");
    } finally {
      changingDevice = false;
    }
  }

  async function cleanupLocalSubscription(): Promise<void> {
    if (
      changingDevice ||
      !browserSubscription ||
      currentServerRegistration !== false
    )
      return;
    changingDevice = true;
    clearFeedback();
    try {
      await browserSubscription.unsubscribe().catch(() => false);
      await loadBrowserSubscription();
      if (browserSubscription) {
        warning =
          "Das lokale Browser-Abo konnte nicht beendet werden. Versuche „Lokales Browser-Abo entfernen“ erneut.";
        return;
      }
      notice = "Das lokale Browser-Abo wurde entfernt.";
    } catch (cause) {
      error = describeNotificationError(cause, "device-disable");
    } finally {
      changingDevice = false;
    }
  }

  onMount(() => {
    supported =
      typeof navigator.serviceWorker !== "undefined" &&
      typeof window.PushManager !== "undefined" &&
      typeof window.Notification !== "undefined";
    permission = supported ? Notification.permission : "unsupported";
    void authStore.checkAuth().then((status) => {
      if (
        status.state === "authenticated" &&
        status.authenticated &&
        supported
      ) {
        void load();
      } else {
        loading = false;
      }
    });

    const handleVisibilityChange = () => refreshPermission();
    document.addEventListener("visibilitychange", handleVisibilityChange);
    window.addEventListener("focus", refreshPermission);
    window.addEventListener("hashchange", focusDeepLink);
    focusDeepLink();

    return () => {
      document.removeEventListener("visibilitychange", handleVisibilityChange);
      window.removeEventListener("focus", refreshPermission);
      window.removeEventListener("hashchange", focusDeepLink);
    };
  });
</script>

<section
  bind:this={sectionElement}
  id="benachrichtigungen"
  class="notification-settings"
  aria-labelledby="notification-settings-heading"
  tabindex="-1"
>
  <div class="section-heading">
    <div>
      <p class="eyebrow">Push für private Nachrichten</p>
      <h2 id="notification-settings-heading">Benachrichtigungen</h2>
    </div>
    <a class="inbox-link touch-target" href="/nachrichten">Zum Postfach</a>
  </div>

  <p class="explanation">
    Push ist nur ein Hinweis. Die Nachricht selbst bleibt in commonThing und
    wird erst nach dem Öffnen geladen. Auf dem Sperrbildschirm erscheint daher
    kein Nachrichtentext und kein Absendername.
  </p>

  <p class="platform-note">
    Auf iPhone und iPad funktioniert Push in der zum Home-Bildschirm
    hinzugefügten commonThing-Web-App.
  </p>

  {#if $authStore.state === "checking" || loading}
    <p class="status">Benachrichtigungseinstellungen werden geladen …</p>
  {:else if $authStore.state === "degraded"}
    <div class="status error status-with-action" role="alert">
      <p>
        Dein Anmeldestatus konnte nicht geprüft werden. Prüfe die Verbindung und
        versuche es erneut.
      </p>
      <button
        class="btn secondary touch-target"
        type="button"
        onclick={retryAuth}>Anmeldung erneut prüfen</button
      >
    </div>
  {:else if !$authStore.authenticated}
    <div class="status status-with-action" role="status">
      <p>Melde dich an, um Push-Hinweise und Gerätefreigaben zu verwalten.</p>
      <a class="btn secondary touch-target" href="/login">Anmelden</a>
    </div>
  {:else if !supported}
    <p class="status warning">
      Web Push ist in diesem Browser oder in dieser Browser-Ansicht nicht
      verfügbar. Registrierte Push-Geräte des Kontos kannst du unten trotzdem
      verwalten.
    </p>
    <PushDeviceManager currentEndpoint={null} />
  {:else if error && !config}
    <div class="status error status-with-action" role="alert">
      <p>{error}</p>
      <button class="btn secondary touch-target" type="button" onclick={load}
        >Erneut versuchen</button
      >
    </div>
  {:else}
    <div class="preference-row">
      <div>
        <strong>Kontoeinstellung</strong>
        <p>
          Push-Hinweise für private Nachrichten. Diese Einstellung legt fest, ob
          commonThing solche Hinweise für dein Konto senden soll.
        </p>
      </div>
      <label class="switch-label">
        <input
          type="checkbox"
          aria-label="Push-Hinweise für private Nachrichten"
          checked={preferences.direct_messages_push}
          disabled={savingPreference}
          onchange={changePreference}
        />
        <span>{preferences.direct_messages_push ? "An" : "Aus"}</span>
      </label>
    </div>

    <div class="device-card">
      <div>
        <strong>Geräteeinstellung</strong>
        <p>
          {#if currentDeviceEnabled}
            Push auf diesem Gerät: aktiviert.
          {:else if localCleanupAvailable}
            Push auf diesem Gerät: serverseitig deaktiviert. Das lokale
            Browser-Abo muss noch entfernt werden.
          {:else if permission === "denied"}
            Push auf diesem Gerät: blockiert. Die Freigabe muss im Browser oder
            Betriebssystem geändert werden.
          {:else}
            Push auf diesem Gerät: nicht aktiviert.
          {/if}
        </p>
      </div>

      {#if currentDeviceEnabled}
        <button
          class="btn secondary touch-target"
          type="button"
          disabled={changingDevice}
          onclick={disableCurrentDevice}
        >
          {changingDevice
            ? "Wird deaktiviert …"
            : "Auf diesem Gerät deaktivieren"}
        </button>
      {:else if localCleanupAvailable}
        <div class="device-actions">
          <button
            class="btn secondary touch-target"
            type="button"
            disabled={changingDevice}
            onclick={cleanupLocalSubscription}
          >
            {changingDevice
              ? "Wird entfernt …"
              : "Lokales Browser-Abo entfernen"}
          </button>
          {#if permission === "denied"}
            <span class="blocked-action">
              In Browser- oder Systemeinstellungen freigeben
            </span>
          {:else}
            <button
              class="btn primary touch-target"
              type="button"
              disabled={changingDevice || !config?.enabled}
              onclick={enableCurrentDevice}
            >
              {changingDevice
                ? "Wird aktiviert …"
                : "Auf diesem Gerät aktivieren"}
            </button>
          {/if}
        </div>
      {:else if permission === "denied"}
        <span class="blocked-action">
          In Browser- oder Systemeinstellungen freigeben
        </span>
      {:else}
        <button
          class="btn primary touch-target"
          type="button"
          disabled={changingDevice || !config?.enabled}
          onclick={enableCurrentDevice}
        >
          {changingDevice ? "Wird aktiviert …" : "Auf diesem Gerät aktivieren"}
        </button>
      {/if}
    </div>

    {#key deviceManagerRefreshVersion}
      <PushDeviceManager
        currentEndpoint={browserSubscription?.endpoint ?? null}
        onCurrentRegistrationChange={handleCurrentRegistrationChange}
      />
    {/key}

    {#if config && !config.enabled}
      <p class="status warning">
        Push ist auf diesem commonThing-Server derzeit nicht verfügbar. Deine
        Nachrichten bleiben im Postfach.
      </p>
    {/if}
    {#if notice}<p class="status success" aria-live="polite">{notice}</p>{/if}
    {#if warning}
      <div class="status warning status-with-action" role="status">
        <p>{warning}</p>
      </div>
    {/if}
    {#if error}<p class="status error" role="alert">{error}</p>{/if}
  {/if}
</section>

<style>
  .notification-settings {
    display: grid;
    gap: 1rem;
    padding: clamp(1rem, 3vw, 1.5rem);
    scroll-margin-top: 1rem;
  }

  .notification-settings:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 3px;
  }

  .section-heading,
  .preference-row,
  .device-card,
  .device-actions,
  .status-with-action {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 1rem;
  }

  h2,
  p {
    margin: 0;
  }

  .eyebrow {
    color: var(--muted);
    font-size: 0.78rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .explanation,
  .platform-note,
  .preference-row p,
  .device-card p,
  .blocked-action {
    color: var(--muted);
  }

  .preference-row,
  .device-card {
    padding: 1rem;
    border: 1px solid var(--panel-border);
    border-radius: var(--radius, 0.8rem);
    background: var(--panel-solid);
  }

  .switch-label {
    display: inline-flex;
    align-items: center;
    gap: 0.45rem;
    min-height: 44px;
    white-space: nowrap;
  }

  .switch-label input {
    width: 1.2rem;
    height: 1.2rem;
  }

  .touch-target {
    min-height: 44px;
  }

  .inbox-link {
    display: inline-flex;
    align-items: center;
    color: var(--accent);
  }

  .status {
    padding: 0.75rem 0.9rem;
    border-radius: 0.65rem;
    background: var(--panel-solid);
  }

  .warning {
    border-inline-start: 3px solid var(--accent);
  }

  .error {
    border-inline-start: 3px solid var(--danger, currentColor);
  }

  .success {
    border-inline-start: 3px solid var(--accent);
  }

  @media (max-width: 620px) {
    .section-heading,
    .preference-row,
    .device-card,
    .device-actions,
    .status-with-action {
      align-items: stretch;
      flex-direction: column;
    }

    .section-heading .inbox-link,
    .device-card button,
    .device-actions,
    .status-with-action button,
    .status-with-action a {
      width: 100%;
    }
  }
</style>
