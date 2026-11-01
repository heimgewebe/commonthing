import { expect, test } from "@playwright/test";

for (const theme of ["dark", "light"] as const) {
  test(
    "native wool punctuation shares the typographic baseline in " + theme,
    async ({ page }) => {
      await page.emulateMedia({ colorScheme: theme });
      await page.goto("/settings?wollschrift=1");
      await expect(page.getByTestId("wool-experiment-toggle")).toBeChecked();

      const family =
        theme === "dark" ? "Garnschrift C Woll" : "Garnschrift C Petrol";
      const inkBounds = await page.evaluate(async (font) => {
        await document.fonts.load('110px "' + font + '"');
        await document.fonts.ready;
        if (!document.fonts.check('110px "' + font + '"')) {
          throw new Error("native wool bitmap font missing");
        }
        const chars = [
          "n",
          "E",
          ".",
          "&",
          ":",
          ";",
          "-",
          "–",
          ",",
          "?",
          "!",
          "/",
        ];
        const bounds: Record<string, { top: number; bottom: number }> = {};
        const canvas = document.createElement("canvas");
        canvas.width = 200;
        canvas.height = 190;
        const context = canvas.getContext("2d", { willReadFrequently: true });
        if (!context) throw new Error("canvas 2D context missing");
        context.font = '110px "' + font + '"';
        for (const character of chars) {
          context.clearRect(0, 0, canvas.width, canvas.height);
          context.fillText(character, 45, 112);
          const pixels = context.getImageData(0, 0, 200, 190).data;
          let first = 999;
          let last = -999;
          for (let y = 0; y < 190; y++) {
            for (let x = 0; x < 200; x++) {
              if (pixels[(y * 200 + x) * 4 + 3] >= 100) {
                first = Math.min(first, y - 112);
                last = Math.max(last, y - 112);
              }
            }
          }
          bounds[character] = { top: first, bottom: last };
        }
        return bounds;
      }, family);

      const target: Record<string, number> = {
        n: 1,
        E: 1,
        ".": 2,
        "&": 2,
        ":": 2,
        ";": 10,
        "-": -20,
        "–": -20,
        ",": 10,
        "?": 2,
        "!": 2,
        "/": 4,
      };
      for (const [character, bottom] of Object.entries(target)) {
        expect(
          Math.abs(inkBounds[character].bottom - bottom),
          "incorrect optical baseline for " + character,
        ).toBeLessThanOrEqual(2);
      }
    },
  );
}
