import { defineConfig } from "@playwright/test";
import baseConfig from "./playwright.config";
import { buildExactRevisionAliases } from "./scripts/web-runtime-evidence.mjs";

const apiBase = process.env.MAP_CARDINALITY_FULLSTACK_API_BASE ?? "";
let apiUrl: URL;
try {
  apiUrl = new URL(apiBase);
} catch {
  throw new Error(
    "MAP_CARDINALITY_FULLSTACK_API_BASE must be an explicit loopback HTTP API origin",
  );
}
if (apiUrl.protocol !== "http:" || apiUrl.hostname !== "127.0.0.1") {
  throw new Error(
    "MAP_CARDINALITY_FULLSTACK_API_BASE must be an explicit loopback HTTP API origin",
  );
}

const inheritedEnvironment: Record<string, string> = Object.fromEntries(
  Object.entries(process.env).filter(
    (entry): entry is [string, string] => typeof entry[1] === "string",
  ),
);
const exactRevisionEnvironment = buildExactRevisionAliases();
const proofServerEnvironment = {
  ...inheritedEnvironment,
  ...exactRevisionEnvironment,
  AUTH_PASSKEY_PROOF_PROXY: "1",
  AUTH_PASSKEY_PROOF_PROXY_TARGET: apiUrl.origin,
};

const webServer = Array.isArray(baseConfig.webServer)
  ? baseConfig.webServer.map((server) => ({
      ...server,
      timeout: Math.max(server.timeout ?? 0, 120_000),
      reuseExistingServer: false,
      env: {
        ...server.env,
        ...proofServerEnvironment,
      },
    }))
  : baseConfig.webServer
    ? {
        ...baseConfig.webServer,
        timeout: Math.max(baseConfig.webServer.timeout ?? 0, 120_000),
        reuseExistingServer: false,
        env: {
          ...baseConfig.webServer.env,
          ...proofServerEnvironment,
        },
      }
    : undefined;

export default defineConfig({
  ...baseConfig,
  webServer,
  testDir: "tests/proofs",
  testMatch: ["**/map-cardinality-fullstack.performance.proof.ts"],
  timeout: 360_000,
  retries: 0,
  workers: 1,
  fullyParallel: false,
  use: {
    ...baseConfig.use,
    browserName: "chromium",
    trace: "retain-on-failure",
  },
});
