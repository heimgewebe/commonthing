#!/usr/bin/env bash
set -e

# This script stays PID 1 for the container's whole life. PID 1 ignores
# SIGTERM without a handler, and an exec'd server would be PID 1 without one
# until it installs its own, so a stop in that window would be lost. Every
# step therefore runs as a background child that the trap can reach: it
# forwards the signal, waits for the child and exits. Both seeders upsert
# idempotently, so an interrupted run is redone on the next start.
child_pid=""
serving=0
# shellcheck disable=SC2317,SC2329 # invoked by the trap below
forward_stop() {
  local status=143
  if [ -n "$child_pid" ]; then
    kill -TERM "$child_pid" 2> /dev/null || true
    status=0
    wait "$child_pid" 2> /dev/null || status=$?
  fi
  # A stopped seeding step is not a success; the server reports its own exit.
  if [ "$serving" -ne 1 ]; then
    status=143
  fi
  exit "$status"
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

# Run the passed command (e.g. the API server) under the same forwarding and
# exit with its status.
serving=1
status=0
run_stoppable "$@" || status=$?
exit "$status"
