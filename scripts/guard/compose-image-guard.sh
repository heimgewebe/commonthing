#!/usr/bin/env bash
set -euo pipefail
TOOLING_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." > /dev/null 2>&1 && pwd)"
REPO_ROOT="${REPO_ROOT:-$TOOLING_ROOT}"
# Same repo-canonical tools/py environment as make validate / UV_RUN.
# Invoked from repo root by make validate / test_repo_contract_guards.
if ! command -v uv > /dev/null 2>&1; then
  echo "ERROR: uv is required for compose-image-guard (tools/py/uv.lock)." >&2
  exit 1
fi
uv run --project "$TOOLING_ROOT/tools/py" --locked python - "$REPO_ROOT" << 'PY'
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

root = Path(sys.argv[1])
compose_dir = root / "infra" / "compose"
prod = compose_dir / "compose.prod.yml"
failures: list[str] = []

if not prod.is_file():
    failures.append(f"production compose file missing: {prod}")
else:
    text = prod.read_text(encoding="utf-8")
    expected = "image: weltgewebe-api:${API_VERSION:?API_VERSION must be set}"
    if expected not in text:
        failures.append("production API image must require a concrete API_VERSION")


def api_default_aliases(path: Path) -> list | None:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    node: object = document
    for key in ("services", "api", "networks", "default", "aliases"):
        node = node.get(key) if isinstance(node, dict) else None
    return node if isinstance(node, list) else None


# Stable internal DNS identity (docs/deploy/heimserver.integration.md, section 11):
# Edge Caddy and Prometheus address the API as weltgewebe-api, never as a
# Compose service suffix. Checked on the parsed alias list, not by text search,
# because the API image name contains the same word.
if prod.is_file() and "weltgewebe-api" not in (api_default_aliases(prod) or []):
    failures.append(
        "infra/compose/compose.prod.yml: services.api.networks.default.aliases must list 'weltgewebe-api'"
    )

image_re = re.compile(r"^\s*image:\s*([^\s#]+)")
digest_re = re.compile(r"@sha256:[0-9a-f]{64}$")
schauwerk_dynamic_image_line = (
    "image: ${SCHAUWERK_SCHAUBILD_IMAGE:?SCHAUWERK_SCHAUBILD_IMAGE must be set}"
)


def validate_schauwerk_runtime_lock() -> str | None:
    lock_path = root / "infra" / "schauwerk-editor" / "release-lock.json"
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return f"cannot read Schaubild runtime lock: {exc}"
    required = {
        "schema_version",
        "source_repository",
        "source_commit",
        "image_repository",
        "image_digest",
        "public_base_path",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        return "Schaubild runtime lock field matrix is invalid"
    if payload.get("schema_version") != "weltgewebe-schauwerk-runtime-lock.v1":
        return "Schaubild runtime lock schema is invalid"
    if payload.get("source_repository") != "heimgewebe/schauwerk":
        return "Schaubild runtime lock source repository is invalid"
    if not re.fullmatch(r"[0-9a-f]{40}", str(payload.get("source_commit", ""))):
        return "Schaubild runtime lock source commit is invalid"
    if payload.get("image_repository") != "ghcr.io/heimgewebe/schauwerk-schaubild":
        return "Schaubild runtime lock image repository is invalid"
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(payload.get("image_digest", ""))):
        return "Schaubild runtime lock image digest is invalid"
    if payload.get("public_base_path") != "/schaubild":
        return "Schaubild runtime lock public base path is invalid"
    return None


for path in sorted(compose_dir.glob("*.y*ml")):
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        match = image_re.match(line)
        if not match:
            continue
        image = match.group(1)
        rel = path.relative_to(root)
        if image == "weltgewebe-api:${API_VERSION:?API_VERSION":
            # The unquoted Compose interpolation contains spaces; verify the full line.
            if line.strip() != "image: weltgewebe-api:${API_VERSION:?API_VERSION must be set}":
                failures.append(f"{rel}:{line_no} malformed API image contract")
            continue
        if "${" in image:
            if (
                rel == Path("infra/compose/compose.vps.override.yml")
                and line.strip() == schauwerk_dynamic_image_line
            ):
                lock_error = validate_schauwerk_runtime_lock()
                if lock_error is not None:
                    failures.append(f"{rel}:{line_no} {lock_error}")
                continue
            failures.append(f"{rel}:{line_no} variable image reference is not statically reviewable: {image}")
            continue
        if ":latest" in image or ":-latest" in image:
            failures.append(f"{rel}:{line_no} latest tag/fallback is forbidden: {image}")
        if not digest_re.search(image):
            failures.append(f"{rel}:{line_no} external image is not pinned by sha256 digest: {image}")

if failures:
    for finding in failures:
        print(f"ERROR: {finding}", file=sys.stderr)
    raise SystemExit(1)
print(
    "PASS: all external Compose images are digest-pinned, the API tag is fail-closed"
    " and the API keeps its weltgewebe-api alias"
)
PY
