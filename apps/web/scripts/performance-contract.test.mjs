import assert from "node:assert/strict";
import {
  mkdirSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { mkdtempSync } from "node:fs";
import test from "node:test";
import {
  assertLegacyContractsAbsent,
  loadPerformanceContract,
  parsePerformanceContract,
  repositoryRoot,
  validatePerformanceContract,
} from "./performance-contract.mjs";

function validRouteBudget() {
  return {
    schema_version: 3,
    measurement: "test route assets",
    output_directories: ["build"],
    initial_js_headroom_warning: {
      minimum_remaining_bytes: 1024,
      minimum_remaining_ratio: 0.02,
    },
    routes: {
      "/map": {
        max_initial_js_gzip_bytes: 100,
        max_initial_css_gzip_bytes: 100,
        forbid_css_markers: [],
        require_css_markers: [],
        min_initial_js_assets: 1,
        min_initial_css_assets: 1,
      },
    },
    emitted_assets: {},
  };
}

function validContract(overrides = {}) {
  return {
    schema_version: 1,
    contract_id: "weltgewebe-performance-v1",
    authority: {
      status: "canonical",
      replaces: ["ci/budget.json"],
      does_not_establish: ["production capacity"],
    },
    measurements: {
      web_build: {
        status: "blocking",
        runner: "apps/web/scripts/assert-route-performance-budget.mjs",
        artifact_kind: "generated-route-assets",
        budget: validRouteBudget(),
      },
      web_runtime: {
        status: "calibration_required",
        runner: "apps/web/playwright.performance.config.ts",
        artifact_kind: "browser-runtime-samples",
        profiles: {
          mobile: {
            viewport: { width: 390, height: 844 },
            network_profile: "project-fast-3g",
            network_conditions: {
              throttled: true,
              latency_ms: 150,
              download_kbps: 1600,
              upload_kbps: 750,
              connection_type: "cellular3g",
            },
            runs: 5,
          },
        },
        scenarios: {
          public_map: {
            path: "/map",
            readiness: {
              required_selectors: ["canvas.maplibregl-canvas", ".map-marker"],
              timeout_ms: 10000,
            },
            interaction: {
              test_ids: ["tool-fan-trigger", "tool-fan-find"],
              expected_test_id: "search-overlay",
              settle_frames: 2,
            },
            metrics: {
              largest_contentful_paint_ms: { percentile: 75, max: 2500 },
              interaction_to_next_paint_ms: { percentile: 75, max: 200 },
              usable_map_ms: { percentile: 95, max: 3000 },
            },
          },
        },
        limitations: ["not production field data"],
      },
      api_runtime: {
        status: "calibration_required",
        runner: "k6",
        artifact_kind: "api-load-samples",
        scenario: {
          virtual_users: 10,
          duration_seconds: 30,
          dataset_profile: "ci",
          concurrency_profile: "mixed-read",
          search_query: "scale",
        },
        metrics: {
          http_request_duration_ms: { percentile: 95, max: 300 },
          http_request_failed_rate: { max: 0.01 },
        },
        limitations: ["not production capacity"],
      },
      database_scale: {
        status: "blocking_plan_shape",
        runner: "scripts/performance/domain_scale.py",
        artifact_kind: "postgresql-plan-evidence",
        config: "configs/performance/domain-scale.v1.json",
        profiles: ["ci"],
        limitations: ["not end-to-end latency"],
      },
      api_replica_resources: {
        status: "calibration_required",
        artifact_kind: "container-resource-samples",
        metrics: ["cpu_seconds", "max_rss_bytes"],
        limitations: ["not calibrated"],
      },
    },
    legacy_contracts: {
      must_not_exist: ["ci/budget.json"],
      slo_reference: "policies/slo.yaml",
    },
    ...overrides,
  };
}

function temporaryDirectory(t) {
  const root = mkdtempSync(join(tmpdir(), "performance-contract-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  return root;
}

test("loads the repository canonical contract", () => {
  const contract = loadPerformanceContract();
  assert.equal(contract.contract_id, "weltgewebe-performance-v1");
  assert.equal(contract.authority.status, "canonical");
  assert.equal(contract.measurements.web_build.status, "blocking");
  assert.equal(
    contract.measurements.web_runtime.status,
    "calibration_required",
  );
  assert.equal(
    contract.measurements.web_runtime.runner,
    "apps/web/playwright.performance.config.ts",
  );
  assert.equal(
    contract.measurements.web_runtime.profiles.mobile.network_conditions
      .throttled,
    true,
  );
});

test("isolates web runtime evidence from unrelated preview servers", () => {
  const config = readFileSync(
    resolve(repositoryRoot, "apps/web/playwright.performance.config.ts"),
    "utf8",
  );
  assert.match(config, /reuseExistingServer:\s*false/);
  assert.doesNotMatch(config, /reuseExistingServer:\s*!process\.env\.CI/);
});

test("packages the canonical contract before the Docker web build", () => {
  const dockerfile = readFileSync(
    resolve(repositoryRoot, "apps/web/Dockerfile"),
    "utf8",
  );
  const contractCopy =
    "COPY policies/performance.v1.json ./policies/performance.v1.json";
  const copyIndex = dockerfile.indexOf(contractCopy);
  const buildIndex = dockerfile.indexOf("pnpm -C apps/web run build:container");
  assert.ok(copyIndex >= 0, "Docker builder must copy the canonical contract");
  assert.ok(
    buildIndex >= 0 && copyIndex < buildIndex,
    "Docker builder must copy the canonical contract before the container build",
  );
});

test("rejects magic fields, missing measurements and invalid percentiles", () => {
  assert.throws(
    () => validatePerformanceContract(validContract({ typo: true })),
    /unsupported field "typo"/,
  );
  const missing = validContract();
  delete missing.measurements.api_runtime;
  assert.throws(
    () => validatePerformanceContract(missing),
    /must define api_runtime/,
  );
  const percentile = validContract();
  percentile.measurements.web_runtime.scenarios.public_map.metrics.largest_contentful_paint_ms.percentile = 101;
  assert.throws(
    () => validatePerformanceContract(percentile),
    /percentile must be <= 100/,
  );
  assert.throws(() => parsePerformanceContract("{broken"), /not valid JSON/);
});

test("requires executable network and scenario definitions", () => {
  const missingNetwork = validContract();
  delete missingNetwork.measurements.web_runtime.profiles.mobile
    .network_conditions;
  assert.throws(
    () => validatePerformanceContract(missingNetwork),
    /must define network_conditions/,
  );

  const zeroThroughput = validContract();
  zeroThroughput.measurements.web_runtime.profiles.mobile.network_conditions.download_kbps = 0;
  assert.throws(
    () => validatePerformanceContract(zeroThroughput),
    /require non-zero throughput/,
  );

  const missingReadiness = validContract();
  delete missingReadiness.measurements.web_runtime.scenarios.public_map
    .readiness;
  assert.throws(
    () => validatePerformanceContract(missingReadiness),
    /must define readiness/,
  );

  const emptySearchQuery = validContract();
  emptySearchQuery.measurements.api_runtime.scenario.search_query = "";
  assert.throws(
    () => validatePerformanceContract(emptySearchQuery),
    /api_runtime\.scenario\.search_query must be a non-empty string/,
  );

  const wrongMetric = validContract();
  delete wrongMetric.measurements.web_runtime.scenarios.public_map.metrics
    .usable_map_ms;
  wrongMetric.measurements.web_runtime.scenarios.public_map.metrics.magic_ms = {
    percentile: 95,
    max: 1,
  };
  assert.throws(
    () => validatePerformanceContract(wrongMetric),
    /unsupported field "magic_ms"/,
  );
});

test("rejects blocking claims for uncalibrated measurement classes", () => {
  const webRuntime = validContract();
  webRuntime.measurements.web_runtime.status = "blocking";
  assert.throws(
    () => validatePerformanceContract(webRuntime),
    /web_runtime\.status must remain calibration_required/,
  );

  const apiRuntime = validContract();
  apiRuntime.measurements.api_runtime.status = "blocking";
  assert.throws(
    () => validatePerformanceContract(apiRuntime),
    /api_runtime\.status must remain calibration_required/,
  );

  const resources = validContract();
  resources.measurements.api_replica_resources.status = "blocking";
  assert.throws(
    () => validatePerformanceContract(resources),
    /api_replica_resources\.status must remain calibration_required/,
  );
});

test("requires explicit limitations for uncalibrated measurements", () => {
  const contract = validContract();
  contract.measurements.web_runtime.limitations = [];
  assert.throws(
    () => validatePerformanceContract(contract),
    /limitations must contain non-empty strings/,
  );
});

test("fails when a replaced legacy contract still exists", (t) => {
  const root = temporaryDirectory(t);
  mkdirSync(join(root, "ci"));
  writeFileSync(join(root, "ci/budget.json"), "{}\n");
  assert.throws(
    () => assertLegacyContractsAbsent(validContract(), root),
    /Legacy performance contract still exists: ci\/budget.json/,
  );
  rmSync(join(root, "ci/budget.json"));
  assert.doesNotThrow(() => assertLegacyContractsAbsent(validContract(), root));

  symlinkSync("missing.json", join(root, "ci/budget.json"));
  assert.throws(
    () => assertLegacyContractsAbsent(validContract(), root),
    /Legacy contract path contains symbolic link: ci\/budget.json \(ci\/budget.json\)/,
  );
});

test("rejects symlinked ancestors of absent legacy contracts", (t) => {
  const externalDirectory = temporaryDirectory(t);
  const linkedRoot = temporaryDirectory(t);
  symlinkSync(externalDirectory, join(linkedRoot, "ci"), "dir");
  assert.throws(
    () => assertLegacyContractsAbsent(validContract(), linkedRoot),
    /Legacy contract path contains symbolic link: ci\/budget.json \(ci\)/,
  );

  const danglingRoot = temporaryDirectory(t);
  symlinkSync("missing-directory", join(danglingRoot, "ci"), "dir");
  assert.throws(
    () => assertLegacyContractsAbsent(validContract(), danglingRoot),
    /Legacy contract path contains symbolic link: ci\/budget.json \(ci\)/,
  );
});

test("ties replaced files to explicit absence enforcement", () => {
  const missingAbsence = validContract();
  missingAbsence.authority.replaces.push("policies/perf.json");
  assert.throws(
    () => validatePerformanceContract(missingAbsence),
    /must be enforced by legacy_contracts\.must_not_exist: policies\/perf\.json/,
  );

  const undeclaredAbsence = validContract();
  undeclaredAbsence.legacy_contracts.must_not_exist.push("policies/perf.json");
  assert.throws(
    () => validatePerformanceContract(undeclaredAbsence),
    /must be declared by authority\.replaces: policies\/perf\.json/,
  );

  const fragmentReference = validContract();
  fragmentReference.authority.replaces.push(
    "policies/slo.yaml#/services/web/latency",
  );
  assert.doesNotThrow(() => validatePerformanceContract(fragmentReference));
});

test("fails closed on unsafe legacy paths", () => {
  const contract = validContract();
  contract.legacy_contracts.must_not_exist = ["../outside.json"];
  assert.throws(
    () => validatePerformanceContract(contract),
    /safe repository-relative path/,
  );
});

test("rejects a canonical contract reached through a symlinked parent", (t) => {
  const root = temporaryDirectory(t);
  const outside = temporaryDirectory(t);
  writeFileSync(
    join(outside, "performance.v1.json"),
    JSON.stringify(validContract()),
  );
  symlinkSync(outside, join(root, "redirect"), "dir");

  assert.throws(
    () =>
      loadPerformanceContract({
        contractPath: join(root, "redirect/performance.v1.json"),
        root,
      }),
    /path contains symbolic link: redirect/,
  );
});

test("rejects same-root symlink parents for the canonical contract", (t) => {
  const root = temporaryDirectory(t);
  const canonical = join(root, "canonical");
  mkdirSync(canonical);
  writeFileSync(
    join(canonical, "performance.v1.json"),
    JSON.stringify(validContract()),
  );
  symlinkSync(canonical, join(root, "redirect"), "dir");

  assert.throws(
    () =>
      loadPerformanceContract({
        contractPath: join(root, "redirect/performance.v1.json"),
        root,
      }),
    /path contains symbolic link: redirect/,
  );
});

test("rejects a symlinked canonical contract", (t) => {
  const root = temporaryDirectory(t);
  const policies = join(root, "policies");
  mkdirSync(policies);
  const outside = join(root, "outside.json");
  writeFileSync(outside, JSON.stringify(validContract()));
  const contractPath = join(policies, "performance.v1.json");
  symlinkSync(outside, contractPath);

  assert.throws(
    () => loadPerformanceContract({ contractPath, root }),
    /path contains symbolic link: policies\/performance\.v1\.json/,
  );
});

test("binds E2E instrumentation to the actual Vite build without running the production budget hook", () => {
  const packageJson = JSON.parse(
    readFileSync(resolve(repositoryRoot, "apps/web/package.json"), "utf8"),
  );
  const e2eBuild = packageJson.scripts?.["build:e2e"];
  assert.equal(typeof e2eBuild, "string");
  assert.match(
    e2eBuild,
    /cross-env VITE_PUBLIC_ENABLE_TEST_MAP=true vite build --mode test/,
  );
  assert.doesNotMatch(e2eBuild, /\bpnpm build\b/);
  assert.doesNotMatch(e2eBuild, /assert-route-performance-budget/);
});
