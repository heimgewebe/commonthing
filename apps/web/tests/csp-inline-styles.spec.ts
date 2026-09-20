import { test, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

/**
 * Die Produktions-CSP setzt `style-src 'self'` ohne 'unsafe-inline'.
 * Geprüft wird hier am laufenden Build, dass die Anwendung darunter startet
 * und dass der einzige verbleibende Verstoß der bekannte, framework-eigene
 * ist: SvelteKit verdrahtet den Inline-Style seiner Live-Region
 * `#svelte-announcer` fest im Client. Das Attribut wird verworfen, die Region
 * bleibt über eine Regel in src/app.css verborgen.
 */

/** Die kanonische Frontend-CSP aus dem Caddy-Vertrag, nicht aus einer Kopie. */
function frontendPolicy(): string {
  const caddyfile = path.resolve(process.cwd(), "../../infra/caddy/Caddyfile");
  const source = fs.readFileSync(caddyfile, "utf8");
  const matches = [
    ...source.matchAll(
      /^\s*header\s+@frontendResponse\s+Content-Security-Policy\s+"([^"]*)"\s*$/gm,
    ),
  ];
  if (matches.length !== 1) {
    throw new Error(
      `erwartet genau eine @frontendResponse-CSP, gefunden: ${matches.length}`,
    );
  }
  return matches[0][1];
}

/**
 * Genau ein Verstoß ist bekannt und akzeptiert: das style-Attribut von
 * SvelteKits `#svelte-announcer`. Schlägt diese Erwartung nach einem
 * SvelteKit-Upgrade um, ist das ein Signal, keine Panne: verschwindet der
 * Verstoß, kann die Sonderregel in src/app.css entfallen; kommt einer hinzu,
 * hat jemand einen Inline-Style eingeführt.
 */
const EXPECTED_VIOLATIONS = 1;

declare global {
  interface Window {
    __CSP_VIOLATIONS__: string[];
  }
}

test.describe("CSP ohne unsafe-inline", () => {
  test("das ausgelieferte Dokument enthält keinen Inline-Style", async ({
    request,
  }) => {
    const response = await request.get("/impressum");
    expect(response.ok()).toBeTruthy();

    const html = await response.text();
    expect(html).not.toMatch(/<[a-zA-Z][^>]*\sstyle\s*=/);
    expect(html).not.toMatch(/<style[\s>]/i);
  });

  test("die Anwendung startet unter der gehärteten Edge-CSP", async ({
    page,
  }) => {
    const policy = frontendPolicy();
    expect(policy).not.toContain("'unsafe-inline'");

    await page.addInitScript(() => {
      window.__CSP_VIOLATIONS__ = [];
      document.addEventListener("securitypolicyviolation", (event) => {
        window.__CSP_VIOLATIONS__.push(event.violatedDirective);
      });
    });

    // Caddy setzt diesen Header vor jeder Frontend-Antwort; der Preview-Server
    // kennt ihn nicht, also wird er hier auf demselben Weg nachgestellt.
    await page.route("**/*", async (route) => {
      const response = await route.fetch();
      const headers = { ...response.headers() };
      if ((headers["content-type"] ?? "").includes("text/html")) {
        headers["content-security-policy"] = policy;
      }
      await route.fulfill({ response, headers });
    });

    await page.goto("/impressum");
    await expect(page.locator("#svelte-announcer")).toHaveCount(1);

    const violations = await page.evaluate(() => window.__CSP_VIOLATIONS__);
    expect(violations).toHaveLength(EXPECTED_VIOLATIONS);
    expect(violations).toEqual(["style-src-attr"]);
  });

  test("die SvelteKit-Live-Region bleibt ohne Inline-Style verborgen", async ({
    page,
  }) => {
    await page.goto("/impressum");

    const announcer = page.locator("#svelte-announcer");
    await expect(announcer).toHaveCount(1);

    // Unter der Produktions-CSP verwirft der Browser das style-Attribut.
    // Genau dieser Zustand wird hier hergestellt: sichtbar werden darf die
    // Region trotzdem nicht.
    await announcer.evaluate((element) => element.removeAttribute("style"));

    const computed = await announcer.evaluate((element) => {
      const style = getComputedStyle(element);
      return {
        position: style.position,
        width: style.width,
        height: style.height,
        overflow: style.overflow,
        clipPath: style.clipPath,
      };
    });

    expect(computed.position).toBe("absolute");
    expect(computed.width).toBe("1px");
    expect(computed.height).toBe("1px");
    expect(computed.overflow).toBe("hidden");
    expect(computed.clipPath).toBe("inset(50%)");
  });
});
