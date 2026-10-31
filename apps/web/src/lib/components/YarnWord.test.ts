import { readFileSync, readdirSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { render } from "svelte/server";
import YarnWord from "./YarnWord.svelte";

const read = (path: string) => readFileSync(resolve(path), "utf8");

describe("YarnWord scope and SSR", () => {
  for (const word of ["Login", "Einstellungen"] as const) {
    it(`keeps real ${word} text and decorative local assets in SSR`, () => {
      const { body } = render(YarnWord, { props: { word } });
      expect(body).toContain(`>${word}</span>`);
      expect(body.match(/alt=""/g)).toHaveLength(2);
      expect(body.match(/aria-hidden="true"/g)).toHaveLength(2);
      expect(body).toContain(`/garnschrift-c/${word.toLowerCase()}-light.svg`);
      expect(body).toContain(`/garnschrift-c/${word.toLowerCase()}-dark.svg`);
      expect(body).not.toContain("style=");
      expect(body).not.toMatch(/\son(?:load|error)=/);
      expect(body).not.toContain("loaded");
    });
  }

  it("is imported only by the two headings, never the map or focus panel", () => {
    for (const [page, word] of [
      ["login", "Login"],
      ["settings", "Einstellungen"],
    ]) {
      expect(read(`src/routes/${page}/+page.svelte`)).toMatch(
        new RegExp(`<h1[^>]*>\\s*<YarnWord word="${word}"`),
      );
    }
    for (const entry of readdirSync(resolve("src/routes/map"))) {
      if (entry.endsWith(".svelte") || entry.endsWith(".ts")) {
        expect(read(`src/routes/map/${entry}`)).not.toContain("YarnWord");
      }
    }
    const component = read("src/lib/components/YarnWord.svelte");
    expect(component).not.toMatch(/AccountPanel|ContextPanel|from.*map/);
  });
});
