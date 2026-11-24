import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const source = readFileSync(
  new URL("./TopBar.svelte", import.meta.url),
  "utf8",
);

describe("TopBar accessibility contract", () => {
  it("exposes navigation semantics instead of an incomplete ARIA toolbar", () => {
    expect(source).toContain("<nav");
    expect(source).toContain('aria-label="Navigation"');
    expect(source).toContain("</nav>");
    expect(source).not.toContain('role="toolbar"');
  });
});

describe("TopBar destination contract", () => {
  const authSource = readFileSync(
    new URL("./TopBarAuth.svelte", import.meta.url),
    "utf8",
  );
  const settingsSource = readFileSync(
    new URL("../../routes/settings/+page.svelte", import.meta.url),
    "utf8",
  );

  it("offers the public catalog independently of account state", () => {
    const publicLink = authSource.indexOf('href="/commons"');
    const accountBlock = authSource.indexOf("{#if authView.showAccountLink}");
    expect(publicLink).toBeGreaterThanOrEqual(0);
    expect(publicLink).toBeLessThan(accountBlock);
  });

  it("links signed-in users to the existing community controls", () => {
    const accountBlock = authSource.indexOf("{#if authView.showAccountLink}");
    const end = authSource.indexOf("{/if}", accountBlock);
    expect(authSource.slice(accountBlock, end)).toContain(
      'href="/settings#meine-gemeinschaften"',
    );
    expect(settingsSource).toContain('href="#meine-gemeinschaften"');
    expect(settingsSource).toMatch(
      /id="meine-gemeinschaften">\s*{#if AudienceCircleControls}/,
    );
  });
});
