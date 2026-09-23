#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" > /dev/null 2>&1 && pwd)"
REPO_ROOT="$(dirname "$(dirname "$SCRIPT_DIR")")"
COMPOSE_IMAGE_GUARD="$REPO_ROOT/scripts/guard/compose-image-guard.sh"

bash "$REPO_ROOT/scripts/guard/lockfile-guard.sh" > /dev/null
bash "$REPO_ROOT/scripts/guard/compose-image-guard.sh" > /dev/null
bash "$REPO_ROOT/scripts/guard/caddy-build-header-guard.sh" > /dev/null

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT
mkdir -p "$TMP_ROOT/infra/compose" "$TMP_ROOT/infra/schauwerk-editor"
cat > "$TMP_ROOT/infra/compose/compose.prod.yml" << 'EOF'
services:
  api:
    image: weltgewebe-api:${API_VERSION:?API_VERSION must be set}
    networks:
      default:
        aliases:
          - weltgewebe-api
EOF
cat > "$TMP_ROOT/infra/compose/compose.vps.override.yml" << 'EOF'
services:
  schaubild:
    image: ${SCHAUWERK_SCHAUBILD_IMAGE:?SCHAUWERK_SCHAUBILD_IMAGE must be set}
EOF
cat > "$TMP_ROOT/infra/schauwerk-editor/release-lock.json" << 'EOF'
{
  "schema_version": "weltgewebe-schauwerk-runtime-lock.v1",
  "source_repository": "heimgewebe/schauwerk",
  "source_commit": "cccccccccccccccccccccccccccccccccccccccc",
  "image_repository": "ghcr.io/heimgewebe/schauwerk-schaubild",
  "image_digest": "sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
  "public_base_path": "/schaubild"
}
EOF
REPO_ROOT="$TMP_ROOT" bash "$COMPOSE_IMAGE_GUARD" > /dev/null

# The image name also contains weltgewebe-api; a renamed alias must still fail.
cp "$TMP_ROOT/infra/compose/compose.prod.yml" "$TMP_ROOT/compose.prod.yml.valid"
sed -i 's/^          - weltgewebe-api$/          - weltgewebe-api-legacy/' "$TMP_ROOT/infra/compose/compose.prod.yml"
if REPO_ROOT="$TMP_ROOT" bash "$COMPOSE_IMAGE_GUARD" > /dev/null 2>&1; then
  echo "ERROR: compose-image-guard accepted a production API without the weltgewebe-api alias" >&2
  exit 1
fi
mv "$TMP_ROOT/compose.prod.yml.valid" "$TMP_ROOT/infra/compose/compose.prod.yml"

# Compose merges plain alias lists from an override, so adding a network stays
# valid; !reset/!override on the alias path would drop the alias at deploy.
cp "$TMP_ROOT/infra/compose/compose.vps.override.yml" "$TMP_ROOT/compose.vps.override.yml.valid"
cat >> "$TMP_ROOT/infra/compose/compose.vps.override.yml" << 'EOF'
  api:
    networks:
      edge: {}
EOF
REPO_ROOT="$TMP_ROOT" bash "$COMPOSE_IMAGE_GUARD" > /dev/null
cp "$TMP_ROOT/compose.vps.override.yml.valid" "$TMP_ROOT/infra/compose/compose.vps.override.yml"
cat >> "$TMP_ROOT/infra/compose/compose.vps.override.yml" << 'EOF'
  api:
    networks:
      default:
        aliases: !override
          - other-name
EOF
if REPO_ROOT="$TMP_ROOT" bash "$COMPOSE_IMAGE_GUARD" > /dev/null 2>&1; then
  echo "ERROR: compose-image-guard accepted an override that replaces the weltgewebe-api alias" >&2
  exit 1
fi
cp "$TMP_ROOT/compose.vps.override.yml.valid" "$TMP_ROOT/infra/compose/compose.vps.override.yml"
cat >> "$TMP_ROOT/infra/compose/compose.vps.override.yml" << 'EOF'
  api:
    networks: !reset {}
EOF
if REPO_ROOT="$TMP_ROOT" bash "$COMPOSE_IMAGE_GUARD" > /dev/null 2>&1; then
  echo "ERROR: compose-image-guard accepted an override that resets the API networks" >&2
  exit 1
fi
mv "$TMP_ROOT/compose.vps.override.yml.valid" "$TMP_ROOT/infra/compose/compose.vps.override.yml"

uv run --project "$REPO_ROOT/tools/py" --locked python - "$TMP_ROOT/infra/schauwerk-editor/release-lock.json" << 'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
payload["image_digest"] = "sha256:short"
path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
PY
if REPO_ROOT="$TMP_ROOT" bash "$COMPOSE_IMAGE_GUARD" > /dev/null 2>&1; then
  echo "ERROR: compose-image-guard accepted an invalid Schaubild digest lock" >&2
  exit 1
fi

cat > "$TMP_ROOT/infra/schauwerk-editor/release-lock.json" << 'EOF'
{
  "schema_version": "weltgewebe-schauwerk-runtime-lock.v1",
  "source_repository": "heimgewebe/schauwerk",
  "source_commit": "cccccccccccccccccccccccccccccccccccccccc",
  "image_repository": "ghcr.io/heimgewebe/schauwerk-schaubild",
  "image_digest": "sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
  "public_base_path": "/schaubild"
}
EOF
sed -i 's/SCHAUWERK_SCHAUBILD_IMAGE/UNSAFE_IMAGE/g' "$TMP_ROOT/infra/compose/compose.vps.override.yml"
if REPO_ROOT="$TMP_ROOT" bash "$COMPOSE_IMAGE_GUARD" > /dev/null 2>&1; then
  echo "ERROR: compose-image-guard accepted an arbitrary dynamic image variable" >&2
  exit 1
fi

echo "PASS: repository contract guards"
