import { defineConfig } from "@playwright/test";
import config from "../playwright.config";

// Run from apps/web: pnpm exec playwright test --config=src/nongeographic-flow.config.ts
export default defineConfig({
  ...config,
  testDir: ".",
  testMatch: "nongeographic-flow.spec.ts",
});
