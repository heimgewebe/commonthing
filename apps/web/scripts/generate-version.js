import fs from "node:fs";
import path from "node:path";
import { execSync } from "node:child_process";
import { computeBuildArtifactTree } from "./build-artifact-evidence.mjs";
import { resolveConfiguredBuildDirectory } from "./route-performance-budget-core.mjs";

const args = process.argv.slice(2);
const allowedArgs = ["--client", "--server", "--artifact-tree"];
const unknownArgs = args.filter((a) => !allowedArgs.includes(a));

if (unknownArgs.length > 0) {
  console.error(`ERROR: Unknown arguments: ${unknownArgs.join(", ")}`);
  console.error(
    `Usage: node generate-version.js [--client] [--server] [--artifact-tree]`,
  );
  process.exit(1);
}

const writeClient = args.length === 0 || args.includes("--client");
const writeServer = args.length === 0 || args.includes("--server");
const bindArtifactTree = args.includes("--artifact-tree");

if (bindArtifactTree && !args.includes("--server")) {
  console.error("ERROR: --artifact-tree requires --server.");
  process.exit(1);
}

const defaultBuildDir = path.resolve(process.cwd(), "build");
const clientDir = path.resolve(process.cwd(), "src/lib/generated");
const clientFile = path.join(clientDir, "buildVersion.json");
const clientModuleFile = path.join(clientDir, "buildVersion.ts");
const compileRevisionFile = path.resolve(
  process.cwd(),
  "static/_app/compile-revision.json",
);

let commit = null;
const revisionVariables = [
  { name: "GIT_COMMIT_SHA" },
  { name: "GITHUB_SHA" },
  { name: "VERCEL_GIT_COMMIT_SHA", providerFlag: "VERCEL" },
  { name: "VITE_VERCEL_GIT_COMMIT_SHA", providerFlag: "VERCEL" },
  { name: "PUBLIC_VERCEL_GIT_COMMIT_SHA", providerFlag: "VERCEL" },
  { name: "CF_PAGES_COMMIT_SHA", providerFlag: "CF_PAGES" },
];
const suppliedRevisions = [];
for (const { name, providerFlag } of revisionVariables) {
  const raw = process.env[name];
  if (typeof raw !== "string" || raw.trim() === "") continue;
  if (providerFlag && process.env[providerFlag] !== "1") {
    console.error(
      `ERROR: ${name} requires ${providerFlag}=1 to establish a platform build context.`,
    );
    process.exit(1);
  }
  const value = raw.trim().toLowerCase();
  if (!/^[0-9a-f]{40}$/.test(value)) {
    console.error(
      `ERROR: ${name} must be a full 40-character lowercase hexadecimal commit.`,
    );
    process.exit(1);
  }
  suppliedRevisions.push({ name, value });
}
const distinctSuppliedRevisions = [
  ...new Set(suppliedRevisions.map(({ value }) => value)),
];
if (distinctSuppliedRevisions.length > 1) {
  console.error(
    `ERROR: Conflicting build revisions: ${suppliedRevisions
      .map(({ name, value }) => `${name}=${value}`)
      .join(", ")}.`,
  );
  process.exit(1);
}
if (distinctSuppliedRevisions.length === 1) {
  commit = distinctSuppliedRevisions[0];
} else {
  try {
    const resolvedCommit = execSync("git rev-parse HEAD", {
      encoding: "utf8",
    }).trim();
    if (!/^[0-9a-f]{40}$/.test(resolvedCommit)) {
      throw new Error("git returned a non-canonical commit identity");
    }
    commit = resolvedCommit;
  } catch {
    console.warn(
      "WARNING: Could not determine git commit. Using non-deterministic fallback.",
    );
  }
}
const shortSha = commit?.slice(0, 8) ?? null;

let now = new Date();
if (process.env.SOURCE_DATE_EPOCH) {
  if (!/^[0-9]+$/.test(process.env.SOURCE_DATE_EPOCH)) {
    console.error("ERROR: SOURCE_DATE_EPOCH must be a non-negative integer.");
    process.exit(1);
  }
  const epoch = Number.parseInt(process.env.SOURCE_DATE_EPOCH, 10);
  now = new Date(epoch * 1000);
  if (Number.isNaN(now.getTime())) {
    console.error(
      "ERROR: SOURCE_DATE_EPOCH is outside the supported date range.",
    );
    process.exit(1);
  }
}
const epochMs = now.getTime();
const builtAt = now.toISOString();
const version = shortSha || commit || "unknown";
const buildId = shortSha ? `${shortSha}-${epochMs}` : `unknown-${epochMs}`;
const payload = { version, build_id: buildId, built_at: builtAt };
if (commit) payload.commit = commit;

const filesWritten = [];
if (writeServer) {
  if (!commit) {
    console.error(
      "ERROR: Server build identity requires a canonical Git commit.",
    );
    process.exit(1);
  }
  let serverPayload = payload;
  let serverBuildDir = defaultBuildDir;
  if (bindArtifactTree) {
    let artifactTree;
    try {
      serverBuildDir = resolveConfiguredBuildDirectory({
        root: process.cwd(),
      });
      artifactTree = computeBuildArtifactTree(serverBuildDir);
    } catch (error) {
      console.error(
        `ERROR: Could not bind the completed build artifact: ${error.message}`,
      );
      process.exit(1);
    }
    if (artifactTree.fileCount < 1) {
      console.error(
        "ERROR: --artifact-tree requires a completed build with at least one regular file.",
      );
      process.exit(1);
    }
    if (artifactTree.compileRevision !== commit) {
      console.error(
        `ERROR: Compiled client revision ${artifactTree.compileRevision ?? "missing"} does not match server revision ${commit}.`,
      );
      process.exit(1);
    }
    serverPayload = {
      ...payload,
      artifact_tree: {
        schema_version: artifactTree.schemaVersion,
        sha256: artifactTree.sha256,
        file_count: artifactTree.fileCount,
        compile_revision: artifactTree.compileRevision,
        provenance: "unattested",
      },
    };
  }
  const targetFile = path.join(serverBuildDir, "_app/version.json");
  fs.mkdirSync(path.dirname(targetFile), { recursive: true });
  fs.writeFileSync(
    targetFile,
    JSON.stringify(serverPayload, null, 2) + "\n",
    "utf8",
  );

  const prodVerityFile = path.join(
    serverBuildDir,
    ".well-known/prodverity.json",
  );
  const prodVerityManifest = {
    schema_version: 1,
    revision: commit,
    checks: [
      { id: "homepage", path: "/", expected_status: 200 },
      {
        id: "frontend-version",
        path: "/_app/version.json",
        expected_status: 200,
      },
      { id: "api-version", path: "/api/version", expected_status: 200 },
    ],
  };
  fs.mkdirSync(path.dirname(prodVerityFile), { recursive: true });
  fs.writeFileSync(
    prodVerityFile,
    JSON.stringify(prodVerityManifest, null, 2) + "\n",
    "utf8",
  );
  filesWritten.push(targetFile, prodVerityFile);
}
if (writeClient) {
  if (!commit) {
    console.error(
      "ERROR: Client build identity requires a canonical Git commit.",
    );
    process.exit(1);
  }
  fs.mkdirSync(path.dirname(compileRevisionFile), { recursive: true });
  fs.writeFileSync(
    compileRevisionFile,
    JSON.stringify({ schema_version: 1, compile_revision: commit }, null, 2) +
      "\n",
    "utf8",
  );
  fs.mkdirSync(clientDir, { recursive: true });
  fs.writeFileSync(clientFile, JSON.stringify(payload, null, 2) + "\n", "utf8");
  const moduleSource =
    "// AUTO-GENERATED by scripts/generate-version.js — do not edit.\n" +
    "export const BUILD_VERSION = " +
    JSON.stringify(version) +
    " as const;\n" +
    "export default " +
    JSON.stringify(payload, null, 2) +
    " as const;\n";
  fs.writeFileSync(clientModuleFile, moduleSource, "utf8");
  filesWritten.push(compileRevisionFile, clientFile, clientModuleFile);
}
console.log(
  filesWritten.length > 0
    ? `Generated build identity: ${version} at ${filesWritten.join(" and ")}`
    : "No version files written.",
);
