#!/usr/bin/env bash
set -euo pipefail

# Tests for the metrics reusable-workflow contract and local snapshot producer.
# Verifies that the metrics ref guard correctly detects mismatches
# between uses: ref and metarepo_ref in the metrics workflow.
# It also locks the snapshot script's default file-output exit semantics.
#
# Tests call the REAL guard and snapshot scripts — no shadow
# reimplementation of production logic.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" > /dev/null 2>&1 && pwd)"
REPO_ROOT="$(dirname "$(dirname "$SCRIPT_DIR")")"
GUARD_SCRIPT="$REPO_ROOT/scripts/guard/metrics-ref-guard.sh"
SNAPSHOT_SCRIPT="$REPO_ROOT/scripts/wgx-metrics-snapshot.sh"

TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TEMP_DIR"' EXIT

PASS=0
FAIL=0

report() {
  if [ "$1" -eq 0 ]; then
    PASS=$((PASS + 1))
    echo "PASS: $2"
  else
    FAIL=$((FAIL + 1))
    echo "FAIL: $2"
  fi
}

# Case 1: Matching refs — should pass
mkdir -p "$TEMP_DIR/case1/.github/workflows"
cat > "$TEMP_DIR/case1/.github/workflows/metrics.yml" << 'YAML'
name: Metrics
on:
  workflow_dispatch:
jobs:
  metrics:
    uses: heimgewebe/metarepo/.github/workflows/wgx-metrics.yml@abc123def456
    with:
      metarepo_ref: abc123def456
      post_url: https://example.com
YAML

if REPO_ROOT="$TEMP_DIR/case1" bash "$GUARD_SCRIPT" > /dev/null 2>&1; then
  report 0 "Matching refs pass"
else
  report 1 "Matching refs should pass"
fi

# Case 2: Mismatched refs — should fail with exit 1
mkdir -p "$TEMP_DIR/case2/.github/workflows"
cat > "$TEMP_DIR/case2/.github/workflows/metrics.yml" << 'YAML'
name: Metrics
on:
  workflow_dispatch:
jobs:
  metrics:
    uses: heimgewebe/metarepo/.github/workflows/wgx-metrics.yml@abc123def456
    with:
      metarepo_ref: xyz789different
      post_url: https://example.com
YAML

if REPO_ROOT="$TEMP_DIR/case2" bash "$GUARD_SCRIPT" > /dev/null 2>&1; then
  report 1 "Mismatched refs should fail"
else
  report 0 "Mismatched refs correctly detected"
fi

# Case 3: Missing workflow file — should fail with exit 2
if REPO_ROOT="$TEMP_DIR/nonexistent" bash "$GUARD_SCRIPT" > /dev/null 2>&1; then
  report 1 "Missing workflow should fail"
else
  report 0 "Missing workflow correctly detected"
fi

# Case 4: Quoted metarepo_ref — should still match
mkdir -p "$TEMP_DIR/case4/.github/workflows"
cat > "$TEMP_DIR/case4/.github/workflows/metrics.yml" << 'YAML'
name: Metrics
on:
  workflow_dispatch:
jobs:
  metrics:
    uses: heimgewebe/metarepo/.github/workflows/wgx-metrics.yml@sha256abc123
    with:
      metarepo_ref: "sha256abc123"
      post_url: https://example.com
YAML

if REPO_ROOT="$TEMP_DIR/case4" bash "$GUARD_SCRIPT" > /dev/null 2>&1; then
  report 0 "Quoted metarepo_ref correctly matches"
else
  report 1 "Quoted metarepo_ref should be stripped and match"
fi

# Case 5: Provenance comment after the uses: ref — must be stripped, not compared
mkdir -p "$TEMP_DIR/case5/.github/workflows"
cat > "$TEMP_DIR/case5/.github/workflows/metrics.yml" << 'YAML'
name: Metrics
on:
  workflow_dispatch:
jobs:
  metrics:
    uses: heimgewebe/metarepo/.github/workflows/wgx-metrics.yml@abc123def456 # provenance: untagged (upstream publishes no release tags)
    with:
      metarepo_ref: abc123def456
      post_url: https://example.com
YAML

if REPO_ROOT="$TEMP_DIR/case5" bash "$GUARD_SCRIPT" > /dev/null 2>&1; then
  report 0 "Provenance comment after the ref is ignored"
else
  report 1 "Provenance comment after the ref should be ignored"
fi

# Case 6: Default snapshot mode writes the file and exits successfully without stdout
snapshot_output="$TEMP_DIR/snapshot.json"
snapshot_stdout="$TEMP_DIR/snapshot.stdout"
if WGX_METRICS_OUTPUT="$snapshot_output" bash "$SNAPSHOT_SCRIPT" > "$snapshot_stdout"; then
  if [ -s "$snapshot_output" ] && [ ! -s "$snapshot_stdout" ] &&
    jq -e '.ts and .host and .updates and .backup and .drift' \
      "$snapshot_output" > /dev/null; then
    report 0 "Default snapshot mode writes valid JSON and exits zero"
  else
    report 1 "Default snapshot mode output contract should hold"
  fi
else
  report 1 "Default snapshot mode should exit zero"
fi

echo ""
echo "test_metrics_ref_guard: $PASS passed, $FAIL failed"
if [ "$FAIL" -ne 0 ]; then
  exit 1
fi
