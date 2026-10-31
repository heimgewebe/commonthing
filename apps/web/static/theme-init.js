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

  try {
    current = normalize(window.localStorage.getItem(key));
  } catch {
    // System bleibt der sichere Standard ohne Speicherzugriff.
  }

  const syncSelect = (control) => {
    control.value = current;
  };

  const syncControlTree = (scope = document) => {
    if (
      scope instanceof HTMLSelectElement &&
      scope.matches("[data-wg-theme-control]")
    ) {
      syncSelect(scope);
    }
    scope.querySelectorAll?.("[data-wg-theme-control]").forEach(syncSelect);
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
  });
})();
