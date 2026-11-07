import { defineConfig, type ReporterDescription } from "@playwright/test";
import { resolve } from "node:path";

const PORT = Number(process.env.PORT ?? (process.env.CI ? 5173 : 4173));
const shouldStartWebServer = process.env.PLAYWRIGHT_SKIP_WEBSERVER !== "1";
const systemChromium = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH?.trim();
const htmlReportDir = resolve(
  process.cwd(),
  process.env.PLAYWRIGHT_HTML_REPORT ?? "playwright-report",
);
const htmlOpenSetting = (process.env.PLAYWRIGHT_HTML_REPORT_OPEN ?? "never") as
  | "never"
  | "on-failure"
  | "always";
const junitOutputName =
  process.env.PLAYWRIGHT_JUNIT_OUTPUT_NAME ?? "results.xml";
// Ensure CI uploads always find an HTML report directory.
const htmlReporter: ReporterDescription = [
  "html",
  { open: htmlOpenSetting, outputFolder: htmlReportDir },
];
const isCI = /^(1|true)$/i.test(process.env.CI ?? "");
const consoleReporter: ReporterDescription = isCI ? ["dot"] : ["line"];
const junitReporter: ReporterDescription = [
  "junit",
  { outputFile: resolve(htmlReportDir, junitOutputName) },
];
/**
 * Reporter aus ENV parsen:
 *   PW_TEST_REPORTER="dot,html,junit"
 *   PLAYWRIGHT_HTML_REPORT_OPEN="never|on-failure|always"
 *   PLAYWRIGHT_JUNIT_OUTPUT_NAME="results.xml"
 */
function resolveEnvReporters(): ReporterDescription[] | undefined {
  const spec = process.env.PW_TEST_REPORTER?.split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  if (!spec || spec.length === 0) return undefined;

  const mapped: ReporterDescription[] = [];
  for (const key of spec) {
    if (key === "dot" || key === "line" || key === "list") {
      mapped.push([key]);
    } else if (key === "html") {
      mapped.push([
        "html",
        { open: htmlOpenSetting, outputFolder: htmlReportDir },
      ]);
    } else if (key === "junit") {
      mapped.push([
        "junit",
        { outputFile: resolve(htmlReportDir, junitOutputName) },
      ]);
    } else {
      // Fallback: Unbekannte Bezeichner ignorieren
    }
  }
  return mapped.length ? mapped : undefined;
}

const envReporters = resolveEnvReporters();
const reporter: ReporterDescription[] = envReporters ?? [
  consoleReporter,
  htmlReporter,
  junitReporter,
];

export default defineConfig({
  testDir: "tests",
  timeout: 60_000,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? undefined : 2,
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: "on-first-retry",
    // NixOS/local developer machines may use a wrapped Chromium that already
    // supplies the shared libraries absent from upstream FHS Playwright builds.
    // CI keeps its normal pinned Playwright browser unless explicitly overridden.
    ...(systemChromium
      ? { launchOptions: { executablePath: systemChromium } }
      : {}),
  },
  reporter,
  ...(shouldStartWebServer
    ? {
        webServer: {
          command: `pnpm run build:e2e && pnpm preview --host 0.0.0.0 --port ${PORT}`,
          url: `http://127.0.0.1:${PORT}`,
          timeout: 90_000,
          reuseExistingServer: !process.env.CI,
        },
      }
    : {}),
});
