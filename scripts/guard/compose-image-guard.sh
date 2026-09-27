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


class ComposeLoader(yaml.SafeLoader):
    """SafeLoader that keeps Compose's merge tags visible instead of failing."""


class MergeTag:
    def __init__(self, tag: str) -> None:
        self.tag = tag


def _construct_merge_tag(loader: yaml.SafeLoader, node: yaml.Node) -> MergeTag:
    return MergeTag(node.tag)


for merge_tag in ("!reset", "!override"):
    ComposeLoader.add_constructor(merge_tag, _construct_merge_tag)

LEGACY_API_ALIAS = "weltgewebe-api"  # commonthing-naming: legacy
LEGACY_API_ALIAS_PATH = ("services", "api", "networks", "default", "aliases")


def load_compose(path: Path) -> object:
    try:
        return yaml.load(path.read_text(encoding="utf-8"), Loader=ComposeLoader)
    except (OSError, yaml.YAMLError) as exc:
        failures.append(f"{path.relative_to(root)} cannot be parsed: {exc}")
        return None


# Transitional compatibility contract (docs/deploy/commonthing.naming.md):
# commonThing is canonical, but current Edge Caddy and Prometheus consumers still
# use LEGACY_API_ALIAS. Guard it only until those consumers migrate. Check the
# parsed alias list, not text, because the legacy API image contains the same word.
if prod.is_file():
    node: object = load_compose(prod)
    for key in LEGACY_API_ALIAS_PATH:
        node = node.get(key) if isinstance(node, dict) else None
    if not isinstance(node, list) or LEGACY_API_ALIAS not in node:
        failures.append(
            "infra/compose/compose.prod.yml: services.api.networks.default.aliases "
            f"must retain legacy compatibility alias {LEGACY_API_ALIAS!r}"
        )

# Legacy deploy entrypoint `scripts/weltgewebe-up`.  # commonthing-naming: legacy
# It deploys compose.prod.yml plus an override. Compose merges plain alias lists,
# but !reset or !override anywhere on the alias path, or a network_mode, can
# remove the compatibility alias from the deployed model.
for overlay in sorted(compose_dir.glob("compose.*.override.y*ml")):
    rel = overlay.relative_to(root)
    node = load_compose(overlay)
    for depth, key in enumerate(LEGACY_API_ALIAS_PATH):
        node = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, MergeTag):
            failures.append(
                f"{rel}: {node.tag} on {'.'.join(LEGACY_API_ALIAS_PATH[: depth + 1])} "
                f"can drop legacy compatibility alias {LEGACY_API_ALIAS!r}"
            )
            break
        if depth == 1 and isinstance(node, dict) and "network_mode" in node:
            failures.append(
                f"{rel}: services.api.network_mode drops legacy compatibility alias "
                f"{LEGACY_API_ALIAS!r}"
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
    " and the API keeps its required legacy compatibility alias"
)
PY
