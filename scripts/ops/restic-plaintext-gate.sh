#!/usr/bin/env bash
# Fail-closed gate for the Restic snapshot job on heim-pc.
#
# Run as ExecStartPre of the snapshot service with exactly the snapshot's
# source paths. It refuses the snapshot while any PostgreSQL plaintext dump
# (*.sql.gz, also as a symlink) lies below a source, including through a
# symlinked source root, and whenever a source is missing or cannot be scanned
# completely. It only reads; it never deletes or moves a recovery point.
set -euo pipefail
umask 077
export LC_ALL=C

fail() {
  printf 'restic-plaintext-gate: %s\n' "$*" >&2
  exit 1
}

(($# > 0)) || fail "usage: restic-plaintext-gate.sh SOURCE..."
for source in "$@"; do
  [[ "$source" == /* ]] || fail "source must be an absolute path: $source"
  resolved="$(realpath -e -- "$source")" || fail "source does not exist: $source"
  # find exits non-zero on any unreadable directory, so a partial scan fails.
  hit="$(find -P "$resolved" \( -type f -o -type l \) -name '*.sql.gz' -print -quit)" ||
    fail "could not scan source completely: $source"
  [[ -z "$hit" ]] || fail "plaintext PostgreSQL dump below snapshot source $source: $hit"
done
printf 'restic-plaintext-gate: no plaintext PostgreSQL dumps below %d source(s)\n' "$#"
