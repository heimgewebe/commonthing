#!/usr/bin/env bash
set -euo pipefail

# Guard: metrics workflow ref consistency
#
# Ensures that the uses: ref and the metarepo_ref input in the metrics
# workflow always point to the same immutable ref.  This prevents silent
# drift when one value is bumped but the other is forgotten.

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" > /dev/null 2>&1 && pwd)"
REPO_ROOT="${REPO_ROOT:-$(cd -- "${SCRIPT_DIR}/../.." > /dev/null 2>&1 && pwd)}"

WORKFLOW="${REPO_ROOT}/.github/workflows/metrics.yml"

if [[ ! -f "$WORKFLOW" ]]; then
  echo "ERROR: metrics workflow not found: $WORKFLOW" >&2
  exit 2
fi

# Extract the ref after the @ in the uses: line
# Pattern: uses: heimgewebe/metarepo/...@<REF> [# <provenance comment>]
# The trailing comment carries the pin's provenance declaration and is not
# part of the ref, so strip it before comparing.
USES_REF="$(grep -E '^\s+uses:\s+heimgewebe/metarepo/' "$WORKFLOW" |
  head -n1 | sed 's/.*@//; s/[[:space:]]*#.*$//' | tr -d '[:space:]')"

if [[ -z "$USES_REF" ]]; then
  echo "ERROR: could not extract uses: ref from $WORKFLOW" >&2
  exit 2
fi

# Extract the metarepo_ref input value
# Pattern: metarepo_ref: "<VALUE>" or metarepo_ref: <VALUE>
METAREPO_REF="$(grep -E '^\s+metarepo_ref:' "$WORKFLOW" |
  head -n1 | sed 's/.*metarepo_ref:[[:space:]]*//' | tr -d "\"' ")"

if [[ -z "$METAREPO_REF" ]]; then
  echo "ERROR: could not extract metarepo_ref from $WORKFLOW" >&2
  exit 2
fi

if [[ "$USES_REF" != "$METAREPO_REF" ]]; then
  echo "ERROR: metrics workflow ref drift detected!" >&2
  echo "  uses: ref      = $USES_REF" >&2
  echo "  metarepo_ref   = $METAREPO_REF" >&2
  echo "" >&2
  echo "These must be identical.  Update .github/workflows/metrics.yml" >&2
  echo "so that both the uses: ref and the metarepo_ref input point to" >&2
  echo "the same immutable commit SHA or tag." >&2
  exit 1
fi

echo "OK: metrics workflow refs are consistent ($USES_REF)"
