#!/usr/bin/env bash

set -euo pipefail

echo "Checking implementation coverage..."

CRITICAL_PATHS=(
  "apps/api"
  "apps/web"
  "infra/compose"
  ".github/workflows"
  "contracts/domain"
)

FAIL=0

REGISTERED_PATHS=$(grep -E '^[[:space:]]*path:' audit/impl-registry.yaml | awk -F ': ' '{print $2}' | tr -d '"' | tr -d "'" | sed 's/\/$//')

for path in "${CRITICAL_PATHS[@]}"; do
  if [ -d "$path" ] || [ -f "$path" ]; then
    found=0
    for reg in $REGISTERED_PATHS; do
      if [[ "$reg" == "$path" ]]; then
        found=1
        break
      fi
    done

    if [ "$found" -eq 0 ]; then
      echo "ERROR: Critical implementation missing from registry: $path"
      FAIL=1
    fi
  fi
done

# Same repo-canonical tools/py environment as make validate / UV_RUN.
if ! command -v uv > /dev/null 2>&1; then
  echo "ERROR: uv is required for coverage-guard (tools/py/uv.lock)."
  exit 1
fi

# Verify that documented_by links exist.
# Fail closed: ein Parse-Fehler in der Registry darf hier nicht zu einer leeren
# Liste und damit zu einem stillen Pass werden.
if ! DOC_REFS=$(uv run --project tools/py --locked python -c "
import sys

import yaml

with open('audit/impl-registry.yaml', 'r', encoding='utf-8') as handle:
    registry = yaml.safe_load(handle)

implementations = registry.get('implementations') if isinstance(registry, dict) else None
if not isinstance(implementations, list):
    print('ERROR: audit/impl-registry.yaml has no implementations list', file=sys.stderr)
    sys.exit(1)

for entry in implementations:
    if not isinstance(entry, dict):
        print(f'ERROR: implementation entry is not a mapping: {entry!r}', file=sys.stderr)
        sys.exit(1)
    for doc in entry.get('documented_by') or []:
        print(doc)
"); then
  echo "ERROR: reading documented_by entries from audit/impl-registry.yaml failed."
  FAIL=1
  DOC_REFS=""
fi

for doc in $DOC_REFS; do
  if [ ! -f "$doc" ]; then
    echo "ERROR: Registered implementation points to dead doc link: $doc"
    FAIL=1
  fi
done

if [ "$FAIL" -eq 1 ]; then
  exit 1
fi

echo "coverage-guard pass."
