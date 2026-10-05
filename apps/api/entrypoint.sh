#!/usr/bin/env bash
set -e

# This script runs as PID 1 until the final exec. PID 1 ignores SIGTERM
# without a handler, so seeding steps run as background children that a stop
# signal can reach: the trap forwards it, waits for the child and exits.
# Both seeders upsert idempotently, so an interrupted run is redone on the
# next start.
child_pid=""
forward_stop() {
  if [ -n "$child_pid" ]; then
    kill -TERM "$child_pid" 2> /dev/null || true
    wait "$child_pid" 2> /dev/null || true
  fi
  exit 143
}
trap forward_stop TERM INT

run_stoppable() {
  "$@" &
  child_pid=$!
  local status=0
  wait "$child_pid" || status=$?
  child_pid=""
  return "$status"
}

# GEWEBE_IN_DIR should be set by docker-compose, default to .gewebe/in
DATA_DIR="${GEWEBE_IN_DIR:-.gewebe/in}"

# Optional: seed the REAL starting dataset (default: false).
# Idempotent. Recommended for real deployments together with GEWEBE_SEED_DEMO=false.
# Real and demo seeding are mutually exclusive. If both are enabled, startup fails.
# For real deployments use GEWEBE_SEED_REAL=true and GEWEBE_SEED_DEMO=false.
ENABLE_REAL_SEEDING="${GEWEBE_SEED_REAL:-false}"

# Check if demo seeding is enabled (default: false). We support "true", "1", "yes".
ENABLE_SEEDING="${GEWEBE_SEED_DEMO:-false}"

# Guard: real and demo seeding are mutually exclusive. Mixing a real first
# account with demo Garnrollen makes "which data is real?" unanswerable.
if [[ "$ENABLE_REAL_SEEDING" =~ ^(true|1|yes)$ ]] && [[ "$ENABLE_SEEDING" =~ ^(true|1|yes)$ ]]; then
  echo "Error: GEWEBE_SEED_REAL and GEWEBE_SEED_DEMO must not both be enabled." >&2
  echo "       For a real deployment set GEWEBE_SEED_REAL=true and GEWEBE_SEED_DEMO=false." >&2
  exit 1
fi

if [[ "$ENABLE_REAL_SEEDING" =~ ^(true|1|yes)$ ]]; then
  echo "Ensuring REAL seed data in $DATA_DIR (GEWEBE_SEED_REAL=$ENABLE_REAL_SEEDING)..."
  mkdir -p "$DATA_DIR"
  if command -v bootstrap-first-account > /dev/null 2>&1; then
    run_stoppable bootstrap-first-account "$DATA_DIR"
  else
    echo "Error: bootstrap-first-account not found, cannot perform GEWEBE_SEED_REAL." >&2
    exit 1
  fi
fi

if [[ "$ENABLE_SEEDING" =~ ^(true|1|yes)$ ]]; then
  # Sentinel check: If core files exist and are not empty, we assume data is present.
  if [ -s "$DATA_DIR/demo.nodes.jsonl" ] &&
    [ -s "$DATA_DIR/demo.accounts.jsonl" ] &&
    [ -s "$DATA_DIR/demo.edges.jsonl" ]; then
    echo "Data files found in $DATA_DIR. Skipping generation."
  else
    echo "Ensuring data in $DATA_DIR (Seeding Enabled)..."

    # Ensure directory exists
    mkdir -p "$DATA_DIR"

    # Run generation script to seed data if missing
    if command -v generate-demo-data > /dev/null 2>&1; then
      run_stoppable generate-demo-data "$DATA_DIR"
    else
      echo "Warning: generate-demo-data not found, skipping data seeding."
    fi
  fi
else
  echo "Skipping data seeding (GEWEBE_SEED_DEMO=$ENABLE_SEEDING)"
fi

# Exec the passed command (e.g. the API server). It installs its own
# handlers; drop the trap so nothing of this shell's handling carries over.
trap - TERM INT
exec "$@"
