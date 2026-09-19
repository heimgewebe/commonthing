// Minimaler inert-Polyfill:
// - blockiert Focus & Clicks in [inert]
// - setzt aria-hidden, solange inert aktiv ist
// Safari < 16.4 & ältere iPadOS-Versionen profitieren davon.

// Speichert den ursprünglichen aria-hidden-Wert für Knoten, deren Subtree wir ausblenden.
// Hinweis: Diese Variante isoliert ausschließlich innerhalb des inert-Subtrees und modelliert
// keine verschachtelten inert-Container. Verschachtelte inert-Bereiche sollten daher vermieden
// oder bewusst sequentiell aktiviert werden.
const previousSubtreeAriaHidden = new WeakMap<Element, string | null>();

function toggleAriaHidden(element: Element, on: boolean) {
  if (on) {
    if (!previousSubtreeAriaHidden.has(element)) {
      previousSubtreeAriaHidden.set(
        element,
        element.getAttribute("aria-hidden"),
      );
    }
    if (element.getAttribute("aria-hidden") !== "true") {
      element.setAttribute("aria-hidden", "true");
    }
  } else {
    if (!previousSubtreeAriaHidden.has(element)) return;

    const previous = previousSubtreeAriaHidden.get(element);
    previousSubtreeAriaHidden.delete(element);

    if (previous === null) {
      element.removeAttribute("aria-hidden");
    } else {
      element.setAttribute("aria-hidden", previous as string);
    }
  }
}

function setSubtreeAriaHidden(root: Element, on: boolean) {
  toggleAriaHidden(root, on);

  const descendants = root.querySelectorAll<HTMLElement>("*");
  for (let i = 0; i < descendants.length; i++) {
    toggleAriaHidden(descendants[i], on);
  }
}

/**
 * Setze `window.__FORCE_INERT_POLYFILL__ = true`, um den Polyfill auch bei
 * moderner Browser-Unterstützung explizit zu aktivieren (z. B. für Tests).
 */
export function ensureInertPolyfill() {
  // SSR-Schutz und moderne Browser mit nativer inert-Unterstützung überspringen.
  if (typeof document === "undefined" || typeof HTMLElement === "undefined")
    return;

  const win = window as typeof window & {
    __FORCE_INERT_POLYFILL__?: boolean;
    __INERT_POLYFILL_ACTIVE__?: boolean;
  };

  const forcePolyfill = win.__FORCE_INERT_POLYFILL__ === true;
  if (!forcePolyfill && "inert" in HTMLElement.prototype) return;

  // Idempotenz: nur einmal aktivieren
  if (win.__INERT_POLYFILL_ACTIVE__) return;
  win.__INERT_POLYFILL_ACTIVE__ = true;

  // Der Style-Schutz (Pointer & Selection aus) steht als [inert]-Regel in
  // src/app.css. Eine Laufzeit-Injektion waere ein Style-Element und damit
  // auf style-src 'unsafe-inline' angewiesen; siehe policies/security.yml.

  // aria-hidden initial anwenden
  const syncAll = () => {
    document
      .querySelectorAll<HTMLElement>("[inert]")
      .forEach((el) => setSubtreeAriaHidden(el, true));
  };
  syncAll();

  // Fokus-, Click- & Tastatur-Blocker.
  // Diese Listener werden bewusst nie entfernt: Das Polyfill wird einmal pro
  // Dokument installiert und muss für dessen gesamte Lebensdauer greifen.
  document.addEventListener(
    "focusin",
    (e) => {
      const t = e.target as HTMLElement | null;
      if (t && t.closest("[inert]")) {
        t.blur?.();
        (document.activeElement as HTMLElement | null)?.blur?.();
      }
    },
    { capture: true },
  );

  document.addEventListener(
    "click",
    (e) => {
      const t = e.target as HTMLElement | null;
      if (t && t.closest("[inert]")) {
        e.preventDefault();
        e.stopPropagation();
      }
    },
    { capture: true },
  );

  document.addEventListener(
    "keydown",
    (e) => {
      const active = document.activeElement as HTMLElement | null;
      const target = e.target as HTMLElement | null;
      const withinInert = (node: HTMLElement | null) =>
        !!node && !!node.closest?.("[inert]");

      if (withinInert(active) || withinInert(target)) {
        // Tab-/Enter-/Space-Interaktionen blocken
        if (e.key === "Tab") {
          e.preventDefault();
          e.stopPropagation();
          active?.blur?.();
          return;
        }
        if (e.key === " " || e.key === "Spacebar" || e.key === "Enter") {
          e.preventDefault();
          e.stopPropagation();
        }
      }
    },
    { capture: true },
  );

  // Beobachte nachträgliche Änderungen an 'inert' und neu eingefügte Knoten
  const mo = new MutationObserver((muts) => {
    let hasAddedNodes = false;

    for (const m of muts) {
      if (
        m.type === "attributes" &&
        m.attributeName === "inert" &&
        m.target instanceof HTMLElement
      ) {
        const el = m.target as HTMLElement;
        const on = el.hasAttribute("inert");
        setSubtreeAriaHidden(el, on);
        continue;
      }

      if (m.type === "childList" && m.addedNodes.length > 0) {
        for (let i = 0; i < m.addedNodes.length; i++) {
          if (m.addedNodes[i].nodeType === Node.ELEMENT_NODE) {
            hasAddedNodes = true;
            break;
          }
        }
      }
    }

    if (hasAddedNodes) {
      syncAll();
    }
  });

  mo.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["inert"],
    childList: true,
    subtree: true,
  });
}
