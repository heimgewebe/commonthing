(() => {
  const key = "weltgewebe.theme";
  const root = document.documentElement;
  const media = window.matchMedia?.("(prefers-color-scheme: dark)");
  const dark = {
    "--bg": "#0e1517",
    "--panel": "rgba(20, 30, 32, 0.94)",
    "--panel-solid": "#151f21",
    "--context-material": "#202b33",
    "--context-weave": 'url("/textures/porcelain-dark.png")',
    "--context-light": "rgba(172, 193, 210, 0.055)",
    "--context-edge": "rgba(195, 213, 227, 0.13)",
    "--context-shade": "rgba(0, 0, 0, 0.32)",
    "--panel-border": "rgba(220, 239, 232, 0.11)",
    "--panel-border-strong": "rgba(220, 239, 232, 0.22)",
    "--text": "#edf5f1",
    "--muted": "#a4b3ae",
    "--accent": "#72c8b6",
    "--accent-soft": "rgba(114, 200, 182, 0.16)",
    "--danger": "#ff8d92",
    "--shadow": "0 14px 36px rgba(0, 0, 0, 0.38)",
  };
  const normalize = (value) =>
    value === "light" || value === "dark" ? value : "system";
  let current = "system";
  const woolKey = "commonthing.wool-everywhere";
  let wool = "off";

  try {
    current = normalize(window.localStorage.getItem(key));
    wool = window.localStorage.getItem(woolKey) === "on" ? "on" : "off";
  } catch {
    // Die Anwendung bleibt ohne Speicherzugriff vollständig benutzbar.
  }

  // An- und Abschalten per URL (?wollschrift=1 und ?wollschrift=0).
  // Normalbetrieb bleibt unverändert und lädt keine zusätzliche Fontdatei.
  const woolQuery = new URLSearchParams(window.location.search).get(
    "wollschrift",
  );
  if (woolQuery === "1" || woolQuery === "0") {
    wool = woolQuery === "1" ? "on" : "off";
    try {
      window.localStorage.setItem(woolKey, wool);
    } catch {
      // Der aktuelle Seitenzustand funktioniert auch ohne lokalen Speicher.
    }
  }
  // Load one extra local stylesheet only for the opted-in browser.
  // This keeps every regular /login, /settings and /map CSS budget unchanged.
  const syncWoolStyles = () => {
    const existing = document.getElementById("wg-wool-experiment-styles");
    if (wool === "on" && !existing) {
      const link = document.createElement("link");
      link.id = "wg-wool-experiment-styles";
      link.rel = "stylesheet";
      link.href = "/wool-experiment.css";
      document.head.appendChild(link);
    } else if (wool === "off" && existing) {
      existing.remove();
    }
    root.dataset.woolExperiment = wool;
  };
  syncWoolStyles();

  const syncSelect = (control) => {
    if (control.matches("[data-wg-theme-control]")) control.value = current;
    if (control.matches("[data-wg-wool-control]"))
      control.checked = wool === "on";
  };

  const syncControlTree = (scope = document) => {
    if (
      (scope instanceof HTMLSelectElement &&
        scope.matches("[data-wg-theme-control]")) ||
      (scope instanceof HTMLInputElement &&
        scope.matches("[data-wg-wool-control]"))
    ) {
      syncSelect(scope);
    }
    scope
      .querySelectorAll?.("[data-wg-theme-control], [data-wg-wool-control]")
      .forEach(syncSelect);
  };

  const watchControls = () => {
    syncControlTree();
    requestAnimationFrame(() => requestAnimationFrame(syncControlTree));
    new MutationObserver((records) => {
      for (const record of records) {
        record.addedNodes.forEach(syncControlTree);
      }
    }).observe(document.body, { childList: true, subtree: true });
  };

  const apply = (preference, persist = false) => {
    current = normalize(preference);
    const resolved =
      current === "system" ? (media?.matches ? "dark" : "light") : current;

    for (const [name, value] of Object.entries(dark)) {
      if (resolved === "dark") root.style.setProperty(name, value);
      else root.style.removeProperty(name);
    }

    root.dataset.theme = current;
    root.dataset.colorScheme = resolved;
    root.style.colorScheme = resolved;
    document
      .querySelector('meta[name="theme-color"]')
      ?.setAttribute("content", resolved === "dark" ? "#0e1517" : "#f3efe5");
    syncControlTree();

    if (persist) {
      try {
        window.localStorage.setItem(key, current);
      } catch {
        // Die Darstellung funktioniert auch ohne lokale Speicherung.
      }
    }
  };

  const getSelect = (target) =>
    target instanceof HTMLSelectElement &&
    target.matches("[data-wg-theme-control]")
      ? target
      : null;

  apply(current);
  media?.addEventListener("change", () => {
    if (current === "system") apply(current);
  });
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", watchControls, {
      once: true,
    });
  } else {
    watchControls();
  }
  document.addEventListener(
    "pointerdown",
    (event) => {
      const control = getSelect(event.target);
      if (control) syncSelect(control);
    },
    true,
  );
  document.addEventListener("focusin", (event) => {
    const control = getSelect(event.target);
    if (control) syncSelect(control);
  });
  document.addEventListener("change", (event) => {
    const control = getSelect(event.target);
    if (control) apply(control.value, true);
    const woolControl = event.target;
    if (
      woolControl instanceof HTMLInputElement &&
      woolControl.matches("[data-wg-wool-control]")
    ) {
      wool = woolControl.checked ? "on" : "off";
      syncWoolStyles();
      try {
        window.localStorage.setItem(woolKey, wool);
      } catch {
        // Abschalten funktioniert auch ohne lokalen Speicher.
      }
      syncControlTree();
    }
  });
})();
