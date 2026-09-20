#!/usr/bin/env bash
set -Eeuo pipefail
umask 022

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
run_ops_python() {
  WELTGEWEBE_OPS_SCRIPT_DIR="$SCRIPT_DIR" python3 -I - "$@"
}

SOURCE_CHECKOUT="${WELTGEWEBE_SOURCE_CHECKOUT:-/opt/weltgewebe}"
RELEASE_ROOT="${WELTGEWEBE_RELEASE_ROOT:-/opt/weltgewebe-releases}"
RUNTIME_ENV="${WELTGEWEBE_RUNTIME_ENV:-/etc/weltgewebe/weltgewebe.env}"
STATE_ROOT="${WELTGEWEBE_DEPLOY_STATE_ROOT:-/var/lib/weltgewebe-main-reconciler}"
DOCKER_CONFIG="$STATE_ROOT/docker-config"
export DOCKER_CONFIG
BUILD_USER="${WELTGEWEBE_BUILD_USER:-alex}"
FRONTEND_URL="${WELTGEWEBE_FRONTEND_VERSION_URL:-https://commonthing.net/_app/version.json}"
BASEMAP_IDENTITY_URL="${WELTGEWEBE_FRONTEND_BASEMAP_IDENTITY_URL:-https://commonthing.net/_app/basemap-build.json}"
BASEMAP_LIGHT_STYLE_URL="${WELTGEWEBE_FRONTEND_BASEMAP_LIGHT_STYLE_URL:-https://commonthing.net/local-basemap/style-germany.json}"
BASEMAP_DARK_STYLE_URL="${WELTGEWEBE_FRONTEND_BASEMAP_DARK_STYLE_URL:-https://commonthing.net/local-basemap/style-germany-dark.json}"
BASEMAP_PMTILES_URL="${WELTGEWEBE_FRONTEND_BASEMAP_PMTILES_URL:-https://commonthing.net/local-basemap/basemap-germany.pmtiles}"
SCHAUWERK_MANIFEST_URL="${WELTGEWEBE_SCHAUWERK_MANIFEST_URL:-https://commonthing.net/schaubild/manifest.json}"
API_URL="${WELTGEWEBE_API_VERSION_URL:-https://commonthing.net/api/version}"
NODE_BUILD_IMAGE="${WELTGEWEBE_NODE_BUILD_IMAGE:-docker.io/library/node@sha256:8898f8ed3c0126667837b678979b4ed83306c856a1227c8bf5f5f77740c25cd6}"
DEPLOY_HELPER="${WELTGEWEBE_DEPLOY_HELPER:-/usr/local/libexec/weltgewebe-deploy-exact-commit}"
LIVE_VERIFIER="${WELTGEWEBE_LIVE_VERIFIER:-/usr/local/libexec/weltgewebe-verify-public-release}"
ARCHIVE_VALIDATOR="${WELTGEWEBE_ARCHIVE_VALIDATOR:-/usr/local/libexec/weltgewebe-validate-web-deploy-archive}"
readonly PRODUCTION_LOCK_DOMAIN="weltgewebe-production-deployment-v1"
readonly PRODUCTION_LOCK_FILE="$STATE_ROOT/production-deployment.lock"
readonly PRODUCTION_LOCK_FD=9
readonly EX_TEMPFAIL=75
readonly EXIT_SUPERSEDED_AFTER_MIGRATION=79
readonly EXIT_SUPERSEDED_AFTER_DEPLOY=80
ARTIFACT_ROOT="$STATE_ROOT/artifacts"
RECEIPT_ROOT="$STATE_ROOT/reconcile-receipts"
DEPLOY_RECEIPT_ROOT="$STATE_ROOT/receipts"
MIN_FREE_KIB="${WELTGEWEBE_MIN_FREE_KIB:-4194304}"
BUILD_MIN_FREE_BYTES="${WELTGEWEBE_PRODUCTION_BUILD_MIN_FREE_BYTES:-51539607552}"
temporary_artifact=""
temporary_source=""
source_archive=""
target_commit=""
artifact_sha=""
state_result=""
deploy_invocation_id=""

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

require_command() {
  command -v "$1" > /dev/null 2>&1 || fail "required command not found: $1"
}

write_lock_contention_receipt() {
  local receipt="$RECEIPT_ROOT/last-contention.json"
  run_ops_python "$receipt" "$PRODUCTION_LOCK_DOMAIN" "$PRODUCTION_LOCK_FILE" << 'PY'
import json
import os
import secrets
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, os.environ["WELTGEWEBE_OPS_SCRIPT_DIR"])
from weltgewebe_secure_receipt_io import (SecureMetadataError, SecurePayloadError, read_secure_json, write_secure_json)

path = Path(sys.argv[1])
payload = {
    "schema_version": 1,
    "kind": "weltgewebe_production_lock_contention",
    "environment": "production",
    "lock_domain": sys.argv[2],
    "lock_file": sys.argv[3],
    "entrypoint": "reconciler",
    "requested_commit": None,
    "result": "already_running",
    "recorded_at": datetime.now(timezone.utc).isoformat(),
}
write_secure_json(path, payload)
PY
  printf '%s\n' "$receipt"
}

prepare_production_lock_file() {
  if [[ -e "$PRODUCTION_LOCK_FILE" || -L "$PRODUCTION_LOCK_FILE" ]]; then
    [[ -f "$PRODUCTION_LOCK_FILE" && ! -L "$PRODUCTION_LOCK_FILE" ]] ||
      fail "production deployment lock is not a regular file"
  else
    (umask 077 && : > "$PRODUCTION_LOCK_FILE")
  fi
  [[ "$(stat --format=%u "$PRODUCTION_LOCK_FILE")" == "0" ]] ||
    fail "production deployment lock is not root-owned"
  local lock_mode
  lock_mode="$(stat --format=%a "$PRODUCTION_LOCK_FILE")"
  (((8#$lock_mode & 022) == 0)) ||
    fail "production deployment lock is group- or world-writable"
}

acquire_production_lock() {
  local contention_receipt
  prepare_production_lock_file
  exec 9<> "$PRODUCTION_LOCK_FILE"
  if ! flock -n "$PRODUCTION_LOCK_FD"; then
    contention_receipt="$(write_lock_contention_receipt)"
    echo "production_reconcile=already_running lock_domain=$PRODUCTION_LOCK_DOMAIN receipt=$contention_receipt"
    exit 0
  fi
}

new_deploy_invocation_id() {
  python3 -I -c 'import secrets; print(secrets.token_hex(32))'
}

measure_available_build_bytes() {
  df -B1 --output=avail "$ARTIFACT_ROOT" | awk 'NR==2 {print $1}'
}

measure_docker_build_cache_metrics() {
  local raw
  raw="$(docker system df --format '{{json .}}')" || return 1
  DOCKER_SYSTEM_DF_JSON_LINES="$raw" run_ops_python << 'PY_DOCKER_BUILD_CACHE_METRICS'
from decimal import Decimal, InvalidOperation
import json
import os
import re

SIZE = re.compile(r"^(?P<number>[0-9]+(?:\.[0-9]+)?)(?P<unit>B|kB|MB|GB|TB|PB)$")
FACTORS = {
    "B": 1,
    "kB": 1000,
    "MB": 1000**2,
    "GB": 1000**3,
    "TB": 1000**4,
    "PB": 1000**5,
}

def parse_size(value: object, field: str) -> int:
    if not isinstance(value, str):
        raise SystemExit(f"Docker {field} is not text")
    token = value.strip().split(" ", 1)[0]
    match = SIZE.fullmatch(token)
    if match is None:
        raise SystemExit(f"Docker {field} has unsupported size syntax: {value!r}")
    try:
        number = Decimal(match.group("number"))
    except InvalidOperation as exc:
        raise SystemExit(f"Docker {field} is not numeric: {exc}")
    return int(number * FACTORS[match.group("unit")])

rows = []
for line in os.environ.get("DOCKER_SYSTEM_DF_JSON_LINES", "").splitlines():
    if not line.strip():
        continue
    try:
        row = json.loads(line)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Docker system df emitted invalid JSON: {exc}")
    if not isinstance(row, dict):
        raise SystemExit("Docker system df row is not an object")
    if row.get("Type") == "Build Cache":
        rows.append(row)

if not rows:
    print("0\t0\t0")
    raise SystemExit(0)
if len(rows) != 1:
    raise SystemExit("Docker system df emitted multiple Build Cache summaries")
row = rows[0]
try:
    active = int(row.get("Active", ""))
except (TypeError, ValueError) as exc:
    raise SystemExit(f"Docker Build Cache Active is invalid: {exc}")
if active < 0:
    raise SystemExit("Docker Build Cache Active must not be negative")
size = parse_size(row.get("Size"), "Build Cache Size")
reclaimable = parse_size(row.get("Reclaimable"), "Build Cache Reclaimable")
if reclaimable > size:
    raise SystemExit("Docker reclaimable build cache exceeds total build cache")
print(f"{active}\t{size}\t{reclaimable}")
PY_DOCKER_BUILD_CACHE_METRICS
}

ensure_production_build_disk_headroom() {
  local commit="$1"
  local available_before
  local available_after
  local metrics_before
  local metrics_after
  local active_before
  local total_before
  local reclaimable_before
  local active_after
  local total_after
  local reclaimable_after
  local min_free_space_output
  local min_free_space_error_excerpt
  local cleanup_rc
  local cleanup_failure

  available_before="$(measure_available_build_bytes)" ||
    fail "production build disk preflight could not measure free space"
  [[ "$available_before" =~ ^[0-9]+$ ]] ||
    fail "production build disk preflight returned invalid free-space data"
  metrics_before="$(measure_docker_build_cache_metrics)" ||
    fail "production build disk preflight could not inspect Docker build cache"
  IFS=$'\t' read -r active_before total_before reclaimable_before <<< "$metrics_before"
  [[ "$active_before" =~ ^[0-9]+$ && "$total_before" =~ ^[0-9]+$ && "$reclaimable_before" =~ ^[0-9]+$ ]] ||
    fail "production build disk preflight returned invalid Docker build-cache metrics"

  echo "production_disk_preflight=observed commit=$commit available_bytes=$available_before required_bytes=$BUILD_MIN_FREE_BYTES build_cache_total_bytes=$total_before build_cache_reclaimable_bytes=$reclaimable_before build_cache_active=$active_before"
  if ((available_before >= BUILD_MIN_FREE_BYTES)); then
    echo "production_disk_preflight=decision commit=$commit action=none result=sufficient_headroom"
    return 0
  fi

  if ((active_before > 0)); then
    echo "production_disk_preflight=decision commit=$commit action=blocked reason=active_build_cache"
    fail "production build disk pressure cannot be recovered while Docker build cache is active"
  fi
  if ((reclaimable_before <= 0)); then
    echo "production_disk_preflight=decision commit=$commit action=blocked reason=no_reclaimable_build_cache"
    fail "insufficient production build disk headroom and no reclaimable Docker build cache"
  fi

  echo "production_disk_preflight=decision commit=$commit action=prune scope=unused_build_cache_only policy=min_free_space target_free_bytes=$BUILD_MIN_FREE_BYTES"
  cleanup_rc=0
  cleanup_failure=""
  if min_free_space_output="$(docker builder prune --force --min-free-space "$BUILD_MIN_FREE_BYTES" 2>&1)"; then
    echo "production_disk_preflight=capability commit=$commit operation=min_free_space capability=supported"
  else
    cleanup_rc=$?
    if [[ "$min_free_space_output" == *"buildkit v0.17.0+ is required for max-used-space and min-free-space filters"* ]]; then
      echo "production_disk_preflight=capability commit=$commit operation=min_free_space capability=unsupported"
      echo "production_disk_preflight=decision commit=$commit action=prune scope=unused_build_cache_only policy=unused_for_168h"
      if docker builder prune --force --filter until=168h > /dev/null; then
        cleanup_rc=0
      else
        cleanup_rc=$?
        cleanup_failure="unused_for_168h"
      fi
    else
      echo "production_disk_preflight=capability commit=$commit operation=min_free_space capability=unknown"
      printf -v min_free_space_error_excerpt '%q' "${min_free_space_output:0:512}"
      printf 'production_disk_preflight_error=min_free_space_unknown commit=%s detail=%s\n' \
        "$commit" "$min_free_space_error_excerpt" >&2
      cleanup_failure="min_free_space_unknown"
    fi
  fi

  available_after="$(measure_available_build_bytes)" ||
    fail "post-cleanup production build disk preflight could not measure free space"
  [[ "$available_after" =~ ^[0-9]+$ ]] ||
    fail "post-cleanup production build disk preflight returned invalid free-space data"
  metrics_after="$(measure_docker_build_cache_metrics)" ||
    fail "post-cleanup production build disk preflight could not inspect Docker build cache"
  IFS=$'\t' read -r active_after total_after reclaimable_after <<< "$metrics_after"
  [[ "$active_after" =~ ^[0-9]+$ && "$total_after" =~ ^[0-9]+$ && "$reclaimable_after" =~ ^[0-9]+$ ]] ||
    fail "post-cleanup production build disk preflight returned invalid Docker build-cache metrics"
  echo "production_disk_preflight=post_cleanup commit=$commit available_bytes=$available_after required_bytes=$BUILD_MIN_FREE_BYTES build_cache_total_bytes=$total_after build_cache_reclaimable_bytes=$reclaimable_after build_cache_active=$active_after"

  if ((cleanup_rc != 0)); then
    echo "production_disk_preflight=cleanup commit=$commit result=failed policy=$cleanup_failure exit_code=$cleanup_rc"
    fail "bounded Docker build-cache cleanup failed"
  fi
  if ((available_after < BUILD_MIN_FREE_BYTES)); then
    echo "production_disk_preflight=decision commit=$commit action=blocked reason=insufficient_headroom_after_cleanup"
    fail "insufficient production build disk headroom after bounded Docker build-cache cleanup"
  fi
  echo "production_disk_preflight=decision commit=$commit action=continue result=cleanup_sufficient"
}

fetch_main() {
  # A network failure must never fall back to a stale remote-tracking ref.
  # Propagate both commands explicitly because errexit is not reliable in every
  # command-substitution context.
  git -C "$SOURCE_CHECKOUT" fetch --no-tags origin \
    "+refs/heads/main:refs/remotes/origin/main" || return 1
  git -C "$SOURCE_CHECKOUT" rev-parse refs/remotes/origin/main || return 1
}

verify_public_schauwerk_runtime() {
  local expected_image_ref="$1"
  local container_ids
  local container_id
  local live_image
  local live_health
  local health_attempt
  local manifest_json

  [[ "$expected_image_ref" =~ ^ghcr\.io/heimgewebe/schauwerk-schaubild@sha256:[0-9a-f]{64}$ ]] || return 1

  container_ids="$(
    docker ps \
      --filter 'label=com.docker.compose.project=weltgewebe' \
      --filter 'label=com.docker.compose.service=schaubild' \
      --format '{{.ID}}'
  )" || return 1
  [[ "$(grep -c . <<< "$container_ids")" == "1" ]] || {
    echo "public Schaubild runtime container identity is ambiguous or absent" >&2
    return 1
  }
  container_id="$container_ids"
  live_image="$(docker inspect --format '{{.Config.Image}}' "$container_id")" || return 1
  [[ "$live_image" == "$expected_image_ref" ]] || {
    echo "public Schaubild runtime image differs from reviewed digest" >&2
    return 1
  }

  # A freshly recreated container can legitimately remain in Docker's
  # "starting" health state through the configured 10s start period and first
  # probes. Poll only that transient state; fail closed on any other state.
  for ((health_attempt = 1; health_attempt <= 120; health_attempt++)); do
    live_health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$container_id")" || return 1
    if [[ "$live_health" == "healthy" ]]; then
      break
    fi
    if [[ "$live_health" != "starting" ]]; then
      echo "public Schaubild runtime container health is '${live_health:-missing}'" >&2
      return 1
    fi
    if (( health_attempt < 120 )); then
      sleep 1
    fi
  done
  [[ "$live_health" == "healthy" ]] || {
    echo "public Schaubild runtime container did not become healthy within 120 seconds" >&2
    return 1
  }

  manifest_json="$(
    curl --fail --silent --show-error \
      --proto '=https' \
      --connect-timeout 5 \
      --max-time 15 \
      --max-filesize 1048576 \
      --header 'Accept-Encoding: identity' \
      "$SCHAUWERK_MANIFEST_URL"
  )" || return 1
  [[ -n "$manifest_json" ]] || return 1
  SCHAUWERK_MANIFEST_JSON="$manifest_json" run_ops_python << 'PY_SCHAUWERK_PUBLIC' || return 1
import json
import os

try:
    manifest = json.loads(os.environ["SCHAUWERK_MANIFEST_JSON"])
except (KeyError, json.JSONDecodeError) as exc:
    raise SystemExit(f"invalid public Schaubild manifest: {exc}")
if manifest.get("schema_version") != "schauwerk-standalone-editor-manifest.v2":
    raise SystemExit("public Schaubild manifest schema is not native v2")
if manifest.get("editor_engine") != "schauwerk-native-diagram-v1":
    raise SystemExit("public Schaubild editor engine is not native")
if manifest.get("cutover_status") != "native-primary-with-legacy-compatibility":
    raise SystemExit("public Schaubild cutover status is not native-primary")
if manifest.get("public_base_path") != "/schaubild":
    raise SystemExit("public Schaubild base path is not canonical")
native = manifest.get("native_renderer")
if not isinstance(native, dict):
    raise SystemExit("public Schaubild native renderer metadata is missing")
if native.get("renderer") != "schauwerk-native-diagram-v1":
    raise SystemExit("public Schaubild native renderer identity mismatch")
if native.get("api_path") != "/schaubild/api/native-viewer":
    raise SystemExit("public Schaubild native API path mismatch")
PY_SCHAUWERK_PUBLIC
}

verify_public_germany_basemap_delivery() {
  local commit="$1"
  local expected_style_sha="$2"
  local expected_dark_style_sha="$3"
  local expected_artifact_size="$4"
  local expected_range_sha="$5"
  local identity_json
  local public_style_sha
  local public_dark_style_sha

  identity_json="$(
    curl --fail --silent --show-error \
      --proto '=https' \
      --connect-timeout 5 \
      --max-time 15 \
      --max-filesize 65536 \
      "$BASEMAP_IDENTITY_URL"
  )" || return 1
  [[ -n "$identity_json" ]] || return 1

  BASEMAP_IDENTITY_JSON="$identity_json" run_ops_python "$commit" "$expected_style_sha" << 'PY_PUBLIC_BASEMAP_IDENTITY' || return 1
import json
import os
import re
import sys

commit, style_sha = sys.argv[1:3]
try:
    payload = json.loads(os.environ["BASEMAP_IDENTITY_JSON"])
except (KeyError, json.JSONDecodeError) as exc:
    raise SystemExit(f"invalid public basemap identity JSON: {exc}")
expected = {
    "schema_version": 1,
    "mode": "local-sovereign",
    "variant": "germany",
    "style_path": "/local-basemap/style-germany.json",
    "source_commit": commit,
    "style_sha256": style_sha,
}
if not isinstance(payload, dict) or payload != expected:
    raise SystemExit(
        "public basemap identity differs from expected nationwide Germany contract: "
        + json.dumps(payload, sort_keys=True)
    )
if not re.fullmatch(r"[0-9a-f]{40}", commit):
    raise SystemExit("expected public source commit is malformed")
if not re.fullmatch(r"[0-9a-f]{64}", style_sha):
    raise SystemExit("expected public style hash is malformed")
PY_PUBLIC_BASEMAP_IDENTITY

  public_style_sha="$(
    curl --fail --silent --show-error \
      --proto '=https' \
      --connect-timeout 5 \
      --max-time 15 \
      --max-filesize 1048576 \
      --header 'Accept-Encoding: identity' \
      "$BASEMAP_LIGHT_STYLE_URL" |
      sha256sum | awk '{print $1}'
  )" || return 1
  if [[ "$public_style_sha" != "$expected_style_sha" ]]; then
    echo "public nationwide Germany light style hash mismatch" >&2
    return 1
  fi

  public_dark_style_sha="$(
    curl --fail --silent --show-error \
      --proto '=https' \
      --connect-timeout 5 \
      --max-time 15 \
      --max-filesize 1048576 \
      --header 'Accept-Encoding: identity' \
      "$BASEMAP_DARK_STYLE_URL" |
      sha256sum | awk '{print $1}'
  )" || return 1
  if [[ "$public_dark_style_sha" != "$expected_dark_style_sha" ]]; then
    echo "public nationwide Germany dark style hash mismatch" >&2
    return 1
  fi

  (
    range_headers="$(mktemp)" || exit 1
    range_body="$(mktemp)" || {
      rm -f -- "$range_headers"
      exit 1
    }
    trap 'rm -f -- "$range_headers" "$range_body"' EXIT
    curl --fail --silent --show-error \
      --proto '=https' \
      --connect-timeout 5 \
      --max-time 15 \
      --max-filesize 127 \
      --header 'Accept-Encoding: identity' \
      --range 0-126 \
      -D "$range_headers" \
      "$BASEMAP_PMTILES_URL" > "$range_body" || exit 1
    run_ops_python "$range_headers" "$range_body" "$expected_artifact_size" "$expected_range_sha" << 'PY_PUBLIC_BASEMAP_RANGE' || exit 1
import hashlib
import re
import sys
from pathlib import Path

headers_path = Path(sys.argv[1])
body_path = Path(sys.argv[2])
expected_size_raw = sys.argv[3]
expected_range_sha = sys.argv[4]
if re.fullmatch(r"[1-9][0-9]*", expected_size_raw) is None:
    raise SystemExit("selected Germany PMTiles size is invalid")
expected_size = int(expected_size_raw)
if expected_size < 127:
    raise SystemExit("selected Germany PMTiles artifact is too small for the range proof")
if re.fullmatch(r"[0-9a-f]{64}", expected_range_sha) is None:
    raise SystemExit("selected Germany PMTiles range hash is invalid")
raw_headers = headers_path.read_text(encoding="iso-8859-1")
blocks = [
    block
    for block in raw_headers.replace("\r\n", "\n").split("\n\n")
    if block.strip()
]
if not blocks:
    raise SystemExit("public Germany PMTiles range response has no headers")
lines = blocks[-1].splitlines()
if not lines or re.fullmatch(r"HTTP/\S+ 206(?: .*)?", lines[0]) is None:
    raise SystemExit("public Germany PMTiles range response is not HTTP 206")
headers = {}
for line in lines[1:]:
    if ":" not in line:
        continue
    name, value = line.split(":", 1)
    headers[name.strip().lower()] = value.strip()
content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
if content_type != "application/octet-stream":
    raise SystemExit("public Germany PMTiles range response has wrong content type")
content_range = headers.get("content-range", "")
if content_range != f"bytes 0-126/{expected_size}":
    raise SystemExit("public Germany PMTiles range response has invalid Content-Range")
if headers.get("content-length") != "127":
    raise SystemExit("public Germany PMTiles range response has invalid Content-Length")
if "bytes" not in headers.get("accept-ranges", "").lower():
    raise SystemExit("public Germany PMTiles range response lacks Accept-Ranges: bytes")
payload = body_path.read_bytes()
if len(payload) != 127:
    raise SystemExit("public Germany PMTiles range response has wrong payload length")
if not payload.startswith(b"PMTiles"):
    raise SystemExit("public Germany PMTiles range response lacks PMTiles signature")
if hashlib.sha256(payload).hexdigest() != expected_range_sha:
    raise SystemExit(
        "public Germany PMTiles range response does not match selected Germany artifact"
    )
PY_PUBLIC_BASEMAP_RANGE
  )
}

write_state() {
  local result="$1"
  local observed_main="${2:-}"
  local detail="${3:-}"
  [[ -n "$target_commit" ]] || return 0
  local receipt="$RECEIPT_ROOT/$target_commit.json"
  run_ops_python "$receipt" "$target_commit" "$result" "$artifact_sha" "$observed_main" "$detail" << 'PY'
import json
import os
import secrets
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, os.environ["WELTGEWEBE_OPS_SCRIPT_DIR"])
from weltgewebe_secure_receipt_io import (SecureMetadataError, SecurePayloadError, read_secure_json, write_secure_json)

path = Path(sys.argv[1])
payload = {
    "schema_version": 3,
    "target_commit": sys.argv[2],
    "result": sys.argv[3],
    "web_artifact_sha256": sys.argv[4] or None,
    "observed_main": sys.argv[5] or None,
    "detail": sys.argv[6] or None,
    "lock_domain": "weltgewebe-production-deployment-v1",
    "lock_owner_entrypoint": "reconciler",
    "recorded_at": datetime.now(timezone.utc).isoformat(),
}
write_secure_json(path, payload)
PY
  state_result="$result"
}

read_deploy_terminal_result() {
  local commit="$1"
  local invocation_id="$2"
  local deployment_receipt="$DEPLOY_RECEIPT_ROOT/$commit.json"

  run_ops_python "$deployment_receipt" "$commit" "$invocation_id" << 'PY'
import json
import os
import stat
import sys
from pathlib import Path
sys.path.insert(0, os.environ["WELTGEWEBE_OPS_SCRIPT_DIR"])
from weltgewebe_secure_receipt_io import (SecureMetadataError, SecurePayloadError, read_secure_json, write_secure_json)

MAX_RECEIPT_BYTES = 1048576
path = Path(sys.argv[1])
commit = sys.argv[2]
invocation_id = sys.argv[3]


try:
    payload = read_secure_json(path)
except (OSError, ValueError) as exc:
    raise SystemExit(f"terminal deployment receipt is unsafe: {exc}") from exc
if payload.get("schema_version") != 5:
    raise SystemExit("terminal deployment receipt schema is not current")
if payload.get("commit") != commit:
    raise SystemExit("terminal deployment receipt targets another commit")
if payload.get("deploy_invocation_id") != invocation_id:
    raise SystemExit("terminal deployment receipt does not match current invocation")
if payload.get("lock_domain") != "weltgewebe-production-deployment-v1":
    raise SystemExit("terminal deployment receipt has another lock domain")
if payload.get("lock_owner_entrypoint") != "reconciler":
    raise SystemExit("terminal deployment receipt has another lock owner")
if payload.get("lock_handoff") != "inherited":
    raise SystemExit("terminal deployment receipt is not bound to inherited handoff")
result = payload.get("result")
if result not in {"superseded_after_migration", "superseded_after_deploy"}:
    raise SystemExit(f"unexpected terminal deployment result: {result!r}")
print(result)
PY
}

read_deploy_tempfail_diagnostic() {
  local commit="$1"
  local invocation_id="$2"
  local deployment_receipt="$DEPLOY_RECEIPT_ROOT/$commit.json"
  local contention_receipt="$DEPLOY_RECEIPT_ROOT/contention/$invocation_id.json"
  local legacy_contention_receipt="$DEPLOY_RECEIPT_ROOT/last-contention.json"
  run_ops_python "$deployment_receipt" "$contention_receipt" \
    "$legacy_contention_receipt" "$commit" "$invocation_id" << 'PY'
import json
import os
import stat
import sys
from pathlib import Path
sys.path.insert(0, os.environ["WELTGEWEBE_OPS_SCRIPT_DIR"])
from weltgewebe_secure_receipt_io import (SecureMetadataError, SecurePayloadError, read_secure_json, write_secure_json)

MAX_RECEIPT_BYTES = 1048576
deployment_path = Path(sys.argv[1])
contention_path = Path(sys.argv[2])
legacy_contention_path = Path(sys.argv[3])
commit = sys.argv[4]
invocation_id = sys.argv[5]


def read_safe(path: Path):
    try:
        return read_secure_json(path)
    except FileNotFoundError:
        return None
    except (OSError, SecureMetadataError, SecurePayloadError):
        return "unsafe"


def is_current_contention(payload: object) -> bool:
    return (
        isinstance(payload, dict)
        and payload.get("schema_version") == 2
        and payload.get("kind") == "weltgewebe_production_lock_contention"
        and payload.get("requested_commit") == commit
        and payload.get("deploy_invocation_id") == invocation_id
        and payload.get("lock_domain") == "weltgewebe-production-deployment-v1"
        and payload.get("entrypoint") == "reconciler"
        and payload.get("result") == "already_running"
    )


deployment = read_safe(deployment_path)
if deployment == "unsafe":
    print("untrusted_receipt")
elif (
    isinstance(deployment, dict)
    and deployment.get("schema_version") == 5
    and deployment.get("commit") == commit
    and deployment.get("deploy_invocation_id") == invocation_id
    and deployment.get("lock_domain") == "weltgewebe-production-deployment-v1"
    and deployment.get("lock_owner_entrypoint") == "reconciler"
    and deployment.get("lock_handoff") == "inherited"
    and deployment.get("result") == "failed"
):
    print("failed_deployment")
else:
    contention = read_safe(contention_path)
    if contention == "unsafe":
        print("untrusted_receipt")
    elif is_current_contention(contention):
        print("lock_contention")
    elif contention is None:
        legacy_contention = read_safe(legacy_contention_path)
        if legacy_contention == "unsafe":
            print("untrusted_receipt")
        elif is_current_contention(legacy_contention):
            print("lock_contention")
        else:
            print("unexplained")
    else:
        # The invocation-specific slot is authoritative when present. A
        # conflicting object must not be overruled by the shared legacy slot.
        print("unexplained")
PY
}

repair_observed_deployment_state() {
  local verification_receipt="$1"
  local deployment_receipt="$DEPLOY_RECEIPT_ROOT/$target_commit.json"

  run_ops_python "$verification_receipt" "$deployment_receipt" "$target_commit" << 'PY'
import json
import os
import secrets
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, os.environ["WELTGEWEBE_OPS_SCRIPT_DIR"])
from weltgewebe_secure_receipt_io import (SecureMetadataError, SecurePayloadError, read_secure_json, write_secure_json)

MAX_RECEIPT_BYTES = 1048576
verification_path = Path(sys.argv[1])
deployment_path = Path(sys.argv[2])
commit = sys.argv[3]


try:
    verification = read_secure_json(verification_path)
except (OSError, SecureMetadataError, SecurePayloadError) as exc:
    raise SystemExit(f"public verification receipt evidence is unsafe: {exc}") from exc
try:
    existing = read_secure_json(deployment_path, missing_ok=True)
except SecurePayloadError:
    existing = None
except (OSError, SecureMetadataError) as exc:
    raise SystemExit(f"deployment receipt evidence is unsafe: {exc}") from exc


def is_lower_hex(value: object, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


SCHEMA5_VERIFIED_KEYS = frozenset(
    {
        "schema_version",
        "environment",
        "commit",
        "web_artifact_sha256",
        "started_at",
        "completed_at",
        "api_commit",
        "frontend_commit",
        "observed_main_after_deploy",
        "migration_completed_at",
        "lock_domain",
        "lock_owner_entrypoint",
        "lock_handoff",
        "result",
        "deploy_invocation_id",
    }
)
SCHEMA5_VERIFIED_OBSERVED_KEYS = SCHEMA5_VERIFIED_KEYS | {"evidence_boundary"}


def parse_aware_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


PUBLIC_VERIFICATION_KEYS = frozenset(
    {
        "schema_version",
        "expected_commit",
        "verified_at",
        "pass",
        "status",
        "pass_scope",
        "identity_verified",
        "artifact_tree_declaration_verified",
        "attestation_verified",
        "provenance_status",
        "reasons",
        "limitations",
        "frontend",
        "api",
    }
)
PUBLIC_ENDPOINT_KEYS = frozenset(
    {"url", "status", "commit", "version", "headers", "error", "artifact_tree"}
)
PUBLIC_ARTIFACT_TREE_KEYS = frozenset(
    {"schema_version", "sha256", "file_count", "compile_revision", "provenance", "error"}
)
PUBLIC_LIMITATIONS = [
    "the public verifier does not reconstruct the deployed artifact tree",
    "artifact provenance is explicitly unattested",
]
LIMITED_EVIDENCE_BOUNDARY = (
    "Exact public identity and declared artifact-tree consistency only; "
    "deployed bytes were not reconstructed and build provenance is unattested."
)
SCHEMA6_LIMITED_KEYS = SCHEMA5_VERIFIED_KEYS | {
    "evidence_boundary",
    "public_verification_schema_version",
    "public_verification_status",
    "public_pass_scope",
    "identity_verified",
    "artifact_tree_declaration_verified",
    "attestation_verified",
    "artifact_tree_schema_version",
    "artifact_tree_sha256",
    "artifact_tree_file_count",
    "artifact_tree_compile_revision",
    "artifact_tree_provenance",
}


def validate_limited_public_verification(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != PUBLIC_VERIFICATION_KEYS:
        raise SystemExit("public verification receipt field matrix is invalid")
    if (
        payload.get("schema_version") != 3
        or payload.get("expected_commit") != commit
        or payload.get("pass") is not True
        or payload.get("status") != "consistency_pass_unattested"
        or payload.get("pass_scope")
        != "identity_and_declared_artifact_consistency"
        or payload.get("identity_verified") is not True
        or payload.get("artifact_tree_declaration_verified") is not True
        or payload.get("attestation_verified") is not False
        or payload.get("provenance_status") != "unattested"
        or payload.get("reasons") != []
        or payload.get("limitations") != PUBLIC_LIMITATIONS
        or parse_aware_timestamp(payload.get("verified_at")) is None
    ):
        raise SystemExit("public verification receipt is not an exact limited pass")

    checked_endpoints: dict[str, dict[str, object]] = {}
    for name in ("frontend", "api"):
        endpoint = payload.get(name)
        if not isinstance(endpoint, dict) or set(endpoint) != PUBLIC_ENDPOINT_KEYS:
            raise SystemExit(f"public {name} receipt field matrix is invalid")
        if (
            endpoint.get("status") != 200
            or endpoint.get("commit") != commit
            or endpoint.get("error") is not None
            or not isinstance(endpoint.get("headers"), dict)
        ):
            raise SystemExit(f"public {name} receipt is not commit-bound")
        checked_endpoints[name] = endpoint

    frontend_endpoint = checked_endpoints["frontend"]
    api_endpoint = checked_endpoints["api"]
    if frontend_endpoint.get("version") != commit[:8]:
        raise SystemExit("public frontend receipt version is not commit-bound")
    frontend_headers = frontend_endpoint["headers"]
    assert isinstance(frontend_headers, dict)
    if "no-store" not in str(frontend_headers.get("cache-control", "")).lower():
        raise SystemExit("public frontend receipt lacks no-store evidence")
    api_headers = api_endpoint["headers"]
    assert isinstance(api_headers, dict)
    if (
        api_headers.get("x-weltgewebe-api-build") != commit
        or api_headers.get("x-weltgewebe-build") != commit[:8]
        or api_endpoint.get("artifact_tree") is not None
    ):
        raise SystemExit("public API receipt headers are not commit-bound")

    artifact_tree = frontend_endpoint.get("artifact_tree")
    if not isinstance(artifact_tree, dict) or set(artifact_tree) != PUBLIC_ARTIFACT_TREE_KEYS:
        raise SystemExit("public artifact-tree receipt field matrix is invalid")
    file_count = artifact_tree.get("file_count")
    if (
        artifact_tree.get("schema_version") != 1
        or not is_lower_hex(artifact_tree.get("sha256"), 64)
        or not isinstance(file_count, int)
        or isinstance(file_count, bool)
        or not 1 <= file_count <= (1 << 53) - 1
        or artifact_tree.get("compile_revision") != commit
        or artifact_tree.get("provenance") != "unattested"
        or artifact_tree.get("error") is not None
    ):
        raise SystemExit("public artifact-tree receipt is not a valid unattested declaration")
    return artifact_tree


def is_current_limited_receipt(
    payload: object,
    artifact_tree: dict[str, object],
) -> bool:
    if not isinstance(payload, dict) or set(payload) != SCHEMA6_LIMITED_KEYS:
        return False
    completed_at = parse_aware_timestamp(payload.get("completed_at"))
    return (
        payload.get("schema_version") == 6
        and payload.get("environment") == "production"
        and payload.get("commit") == commit
        and payload.get("web_artifact_sha256") is None
        and payload.get("started_at") is None
        and completed_at is not None
        and payload.get("api_commit") == commit
        and payload.get("frontend_commit") == commit
        and payload.get("observed_main_after_deploy") == commit
        and payload.get("migration_completed_at") is None
        and payload.get("lock_domain") == "weltgewebe-production-deployment-v1"
        and payload.get("lock_owner_entrypoint") == "reconciler"
        and payload.get("lock_handoff") == "public-observation"
        and payload.get("result") == "consistent_observed_unattested"
        and payload.get("deploy_invocation_id") is None
        and payload.get("evidence_boundary") == LIMITED_EVIDENCE_BOUNDARY
        and payload.get("public_verification_schema_version") == 3
        and payload.get("public_verification_status")
        == "consistency_pass_unattested"
        and payload.get("public_pass_scope")
        == "identity_and_declared_artifact_consistency"
        and payload.get("identity_verified") is True
        and payload.get("artifact_tree_declaration_verified") is True
        and payload.get("attestation_verified") is False
        and payload.get("artifact_tree_schema_version") == 1
        and payload.get("artifact_tree_sha256") == artifact_tree.get("sha256")
        and payload.get("artifact_tree_file_count") == artifact_tree.get("file_count")
        and payload.get("artifact_tree_compile_revision") == commit
        and payload.get("artifact_tree_provenance") == "unattested"
    )


def is_current_verified_receipt(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False

    result = payload.get("result")
    if result == "verified":
        expected_keys = SCHEMA5_VERIFIED_KEYS
    elif result == "verified_observed":
        expected_keys = SCHEMA5_VERIFIED_OBSERVED_KEYS
    else:
        return False
    if set(payload) != expected_keys:
        return False

    completed_at = parse_aware_timestamp(payload.get("completed_at"))
    if (
        payload.get("schema_version") != 5
        or payload.get("environment") != "production"
        or payload.get("commit") != commit
        or payload.get("lock_domain") != "weltgewebe-production-deployment-v1"
        or payload.get("api_commit") != commit
        or payload.get("frontend_commit") != commit
        or payload.get("observed_main_after_deploy") != commit
        or completed_at is None
    ):
        return False

    owner = payload.get("lock_owner_entrypoint")
    handoff = payload.get("lock_handoff")
    invocation_id = payload.get("deploy_invocation_id")
    if result == "verified":
        started_at = parse_aware_timestamp(payload.get("started_at"))
        migration_completed_at = parse_aware_timestamp(
            payload.get("migration_completed_at")
        )
        if (
            not is_lower_hex(payload.get("web_artifact_sha256"), 64)
            or started_at is None
            or migration_completed_at is None
            or not started_at <= migration_completed_at <= completed_at
        ):
            return False
        return (
            owner == "deploy-helper"
            and handoff == "direct"
            and invocation_id is None
        ) or (
            owner == "reconciler"
            and handoff == "inherited"
            and is_lower_hex(invocation_id, 64)
        )

    evidence_boundary = payload.get("evidence_boundary")
    return (
        owner == "reconciler"
        and handoff == "public-observation"
        and invocation_id is None
        and payload.get("web_artifact_sha256") is None
        and payload.get("started_at") is None
        and payload.get("migration_completed_at") is None
        and isinstance(evidence_boundary, str)
        and bool(evidence_boundary.strip())
    )


def migrate_schema4_verified_receipt(payload: object) -> dict[str, object] | None:
    if not isinstance(payload, dict):
        return None
    started_at = parse_aware_timestamp(payload.get("started_at"))
    completed_at = parse_aware_timestamp(payload.get("completed_at"))
    migration_completed_at = parse_aware_timestamp(
        payload.get("migration_completed_at")
    )
    expected_keys = {
        "schema_version",
        "environment",
        "commit",
        "web_artifact_sha256",
        "started_at",
        "completed_at",
        "api_commit",
        "frontend_commit",
        "observed_main_after_deploy",
        "migration_completed_at",
        "lock_domain",
        "lock_owner_entrypoint",
        "lock_handoff",
        "result",
    }
    if set(payload) != expected_keys:
        return None
    if (
        payload.get("schema_version") != 4
        or payload.get("environment") != "production"
        or payload.get("commit") != commit
        or not is_lower_hex(payload.get("web_artifact_sha256"), 64)
        or started_at is None
        or completed_at is None
        or payload.get("api_commit") != commit
        or payload.get("frontend_commit") != commit
        or payload.get("observed_main_after_deploy") != commit
        or migration_completed_at is None
        or payload.get("lock_domain") != "weltgewebe-production-deployment-v1"
        or payload.get("lock_owner_entrypoint") != "deploy-helper"
        or payload.get("lock_handoff") != "direct"
        or payload.get("result") != "verified"
    ):
        return None
    if not started_at <= migration_completed_at <= completed_at:
        return None
    migrated = dict(payload)
    migrated["schema_version"] = 5
    migrated["deploy_invocation_id"] = None
    return migrated


artifact_tree = validate_limited_public_verification(verification)
assert isinstance(verification, dict)
frontend = verification["frontend"]
api = verification["api"]
assert isinstance(frontend, dict)
assert isinstance(api, dict)

if is_current_verified_receipt(existing) and existing.get("result") == "verified":
    raise SystemExit(0)
if is_current_limited_receipt(existing, artifact_tree):
    raise SystemExit(0)

migrated_schema4 = migrate_schema4_verified_receipt(existing)
if migrated_schema4 is not None:
    write_secure_json(deployment_path, migrated_schema4)
    raise SystemExit(0)

payload = {
    "schema_version": 6,
    "environment": "production",
    "commit": commit,
    "web_artifact_sha256": None,
    "started_at": None,
    "completed_at": verification.get("verified_at"),
    "api_commit": api.get("commit"),
    "frontend_commit": frontend.get("commit"),
    "observed_main_after_deploy": commit,
    "migration_completed_at": None,
    "lock_domain": "weltgewebe-production-deployment-v1",
    "lock_owner_entrypoint": "reconciler",
    "lock_handoff": "public-observation",
    "result": "consistent_observed_unattested",
    "deploy_invocation_id": None,
    "evidence_boundary": LIMITED_EVIDENCE_BOUNDARY,
    "public_verification_schema_version": verification.get("schema_version"),
    "public_verification_status": verification.get("status"),
    "public_pass_scope": verification.get("pass_scope"),
    "identity_verified": verification.get("identity_verified"),
    "artifact_tree_declaration_verified": verification.get(
        "artifact_tree_declaration_verified"
    ),
    "attestation_verified": verification.get("attestation_verified"),
    "artifact_tree_schema_version": artifact_tree.get("schema_version"),
    "artifact_tree_sha256": artifact_tree.get("sha256"),
    "artifact_tree_file_count": artifact_tree.get("file_count"),
    "artifact_tree_compile_revision": artifact_tree.get("compile_revision"),
    "artifact_tree_provenance": artifact_tree.get("provenance"),
}
write_secure_json(deployment_path, payload)
PY

  ln -sfn "receipts/$target_commit.json" "$STATE_ROOT/current.json"

  local candidate_release="$RELEASE_ROOT/$target_commit"
  if [[ -e "$candidate_release" ]]; then
    [[ -d "$candidate_release" && ! -L "$candidate_release" ]] ||
      fail "observed release path is unsafe: $candidate_release"
    local release_root_real
    local candidate_real
    local candidate_head
    release_root_real="$(realpath "$RELEASE_ROOT")"
    candidate_real="$(realpath "$candidate_release")"
    case "$candidate_real" in
      "$release_root_real"/*) ;;
      *) fail "observed release escaped the release root" ;;
    esac
    [[ "$(stat --format=%u "$candidate_real")" == "0" ]] ||
      fail "observed release is not root-owned"
    local candidate_mode
    candidate_mode="$(stat --format=%a "$candidate_real")"
    (((8#$candidate_mode & 022) == 0)) ||
      fail "observed release is group- or world-writable"
    candidate_head="$(git -C "$candidate_real" rev-parse HEAD)"
    [[ "$candidate_head" == "$target_commit" ]] ||
      fail "observed release does not match the public commit"
    if [[ -L "$STATE_ROOT/current-release" ]]; then
      local previous_release
      previous_release="$(readlink -f "$STATE_ROOT/current-release")" ||
        fail "current release marker is broken"
      case "$previous_release" in
        "$release_root_real"/*)
          if [[ "$previous_release" != "$candidate_real" ]]; then
            ln -sfn "$previous_release" "$STATE_ROOT/previous-release"
          fi
          ;;
        *) fail "current release marker escapes release root" ;;
      esac
    fi
    ln -sfn "$candidate_real" "$STATE_ROOT/current-release"
  fi
}

is_terminal_success_state() {
  case "$1" in
    consistent_observed_unattested | deferred | verified | verified_observed | \
      superseded_after_observe | superseded_after_migration | \
      superseded_after_deploy | superseded_after_verify)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

cleanup() {
  local rc=$?
  trap - EXIT
  if [[ -n "$temporary_artifact" && -f "$temporary_artifact" && ! -L "$temporary_artifact" ]]; then
    rm -f -- "$temporary_artifact"
  fi
  if [[ -n "$temporary_source" && -f "$temporary_source" && ! -L "$temporary_source" ]]; then
    rm -f -- "$temporary_source"
  fi
  if [[ -n "$source_archive" && -f "$source_archive" && ! -L "$source_archive" ]]; then
    rm -f -- "$source_archive"
  fi
  if ((rc != 0)) && [[ -n "$target_commit" ]] &&
    ! is_terminal_success_state "$state_result"; then
    write_state "failed" "" "reconciler exit code $rc" || true
  fi
  exit "$rc"
}
trap cleanup EXIT

prune_artifacts() {
  local old_artifact
  local -a web_artifacts=()
  find "$ARTIFACT_ROOT" -maxdepth 1 -type f -name 'source-*.tar' -delete
  while IFS= read -r old_artifact; do
    [[ "$old_artifact" == "$ARTIFACT_ROOT/web-$target_commit.tar.gz" ]] && continue
    rm -f -- "$old_artifact"
  done < <(find "$ARTIFACT_ROOT" -maxdepth 1 -type f -name 'web-*.tar.gz' -mtime +7 -print)

  mapfile -t web_artifacts < <(find "$ARTIFACT_ROOT" -maxdepth 1 -type f -name 'web-*.tar.gz' -printf '%T@ %p\n' | sort -nr | cut -d' ' -f2-)
  if ((${#web_artifacts[@]} > 20)); then
    for old_artifact in "${web_artifacts[@]:20}"; do
      [[ "$old_artifact" == "$ARTIFACT_ROOT/web-$target_commit.tar.gz" ]] && continue
      rm -f -- "$old_artifact"
    done
  fi
}

prune_deploy_contention_receipts() {
  local contention_root="$DEPLOY_RECEIPT_ROOT/contention"
  local old_receipt
  local -a receipts=()

  find "$contention_root" -maxdepth 1 -type f -name '*.json' -mtime +7 -delete
  mapfile -t receipts < <(
    find "$contention_root" -maxdepth 1 -type f -name '*.json' \
      -printf '%T@ %p\n' | sort -nr | cut -d' ' -f2-
  )
  if ((${#receipts[@]} > 20)); then
    for old_receipt in "${receipts[@]:20}"; do
      rm -f -- "$old_receipt"
    done
  fi
}

path_contains_mount() {
  local target_path="$1"
  local boundary_path="$2"
  run_ops_python "$target_path" "$boundary_path" /proc/self/mountinfo << 'PY'
import os
import sys


def decode_mount_path(value: str) -> str:
    for escaped, plain in (
        (r"\040", " "),
        (r"\011", "\t"),
        (r"\012", "\n"),
        (r"\134", "\\"),
    ):
        value = value.replace(escaped, plain)
    return value


target = os.path.realpath(sys.argv[1])
boundary = os.path.realpath(sys.argv[2])
mountinfo_path = sys.argv[3]
boundary_prefix = boundary + os.sep
if target != boundary and not target.startswith(boundary_prefix):
    raise SystemExit(2)
target_prefix = target + os.sep
with open(mountinfo_path, encoding="utf-8") as mountinfo:
    for raw_line in mountinfo:
        fields = raw_line.split()
        if len(fields) < 5:
            raise SystemExit(2)
        mount_path = decode_mount_path(fields[4])
        mount_within_boundary = (
            mount_path == boundary or mount_path.startswith(boundary_prefix)
        )
        target_within_mount = target == mount_path or target.startswith(
            mount_path + os.sep
        )
        mount_within_target = mount_path == target or mount_path.startswith(
            target_prefix
        )
        if mount_within_boundary and (target_within_mount or mount_within_target):
            raise SystemExit(0)
raise SystemExit(1)
PY
}

cleanup_release_runtime_paths() {
  local release_dir="$1"
  local release_real
  local web_parent_path="$release_dir/apps/web"
  local web_build_path="$web_parent_path/build"
  local web_build_real
  local basemap_path="$release_dir/build/basemap"
  local basemap_real
  local mount_rc
  local protected_path
  local unsafe_path

  release_real="$(realpath -e -- "$release_dir")" || return 1
  [[ "$release_real" == "$release_dir" ]] || {
    echo "release cleanup path changed identity: $release_dir" >&2
    return 1
  }

  for protected_path in "$release_real" "$release_dir/apps" "$web_parent_path" "$release_dir/build"; do
    [[ -e "$protected_path" || -L "$protected_path" ]] || continue
    [[ -d "$protected_path" && ! -L "$protected_path" ]] || {
      echo "release cleanup parent is unsafe: $protected_path" >&2
      return 1
    }
    if ! unsafe_path="$(
      find "$protected_path" -maxdepth 0 -xdev \( ! -user "$EUID" -o -perm /022 \) -print -quit
    )"; then
      echo "could not inspect release cleanup parent: $protected_path" >&2
      return 1
    fi
    [[ -z "$unsafe_path" ]] || {
      echo "release cleanup parent is not exclusively owned and protected: $unsafe_path" >&2
      return 1
    }
  done

  for protected_path in "$web_parent_path" "$release_dir/build"; do
    [[ -d "$protected_path" && ! -L "$protected_path" ]] || continue
    if path_contains_mount "$protected_path" "$release_real"; then
      echo "release cleanup parent intersects a mount: $protected_path" >&2
      return 1
    else
      mount_rc=$?
      ((mount_rc == 1)) || {
        echo "could not verify release cleanup parent mount state: $protected_path" >&2
        return 1
      }
    fi
  done

  if [[ -L "$web_build_path" ]]; then
    if ! rm -f -- "$web_build_path"; then
      echo "could not unlink release web build symlink: $web_build_path" >&2
      return 1
    fi
  elif [[ -d "$web_build_path" ]]; then
    web_build_real="$(realpath -e -- "$web_build_path")" || return 1
    case "$web_build_real" in
      "$release_real"/*) ;;
      *)
        echo "release web build escaped release root: $web_build_path" >&2
        return 1
        ;;
    esac
    if path_contains_mount "$web_build_real" "$release_real"; then
      echo "release web build contains a mount: $web_build_path" >&2
      return 1
    else
      mount_rc=$?
      ((mount_rc == 1)) || {
        echo "could not verify release web build mount state: $web_build_path" >&2
        return 1
      }
    fi
    if ! rm -rf --one-file-system -- "$web_build_real"; then
      echo "could not remove release web build: $web_build_path" >&2
      return 1
    fi
  elif [[ -e "$web_build_path" ]]; then
    echo "release web build has an unexpected file type: $web_build_path" >&2
    return 1
  fi
  if [[ -L "$basemap_path" ]]; then
    if ! rm -f -- "$basemap_path"; then
      echo "could not unlink release basemap symlink: $basemap_path" >&2
      return 1
    fi
  elif [[ -d "$basemap_path" ]]; then
    basemap_real="$(realpath -e -- "$basemap_path")" || return 1
    case "$basemap_real" in
      "$release_real"/*) ;;
      *)
        echo "legacy release basemap escaped release root: $basemap_path" >&2
        return 1
        ;;
    esac

    if path_contains_mount "$basemap_real" "$release_real"; then
      echo "legacy release basemap contains a mount: $basemap_path" >&2
      return 1
    else
      mount_rc=$?
      ((mount_rc == 1)) || {
        echo "could not verify legacy release basemap mount state: $basemap_path" >&2
        return 1
      }
    fi

    if ! unsafe_path="$(
      find "$basemap_real" -xdev \
        \( -type f -o -type d \) \
        \( ! -user "$EUID" -o -perm /022 \) -print -quit
    )"; then
      echo "could not inspect legacy release basemap: $basemap_path" >&2
      return 1
    fi
    [[ -z "$unsafe_path" ]] || {
      echo "legacy release basemap is not exclusively owned and protected: $unsafe_path" >&2
      return 1
    }
    if ! rm -rf --one-file-system -- "$basemap_real"; then
      echo "could not remove legacy release basemap: $basemap_path" >&2
      return 1
    fi
  elif [[ -e "$basemap_path" ]]; then
    echo "legacy release basemap has an unexpected file type: $basemap_path" >&2
    return 1
  fi
  rmdir "$release_dir/build" 2> /dev/null || true
}

prune_releases() {
  local release_root_real
  local current_release=""
  local previous_release=""
  local current_name
  local previous_name
  local release_candidates
  local release_dir
  local release_name
  local release_head
  local release_status
  local release_mode

  release_root_real="$(realpath -e -- "$RELEASE_ROOT")" || {
    echo "skipping release pruning: release root is unavailable" >&2
    return 0
  }
  if [[ -e "$STATE_ROOT/current-release" || -L "$STATE_ROOT/current-release" ]]; then
    [[ -L "$STATE_ROOT/current-release" ]] || {
      echo "skipping release pruning: current release marker is unsafe" >&2
      return 0
    }
    current_release="$(readlink -f "$STATE_ROOT/current-release" 2> /dev/null || true)"
  fi
  if [[ -e "$STATE_ROOT/previous-release" || -L "$STATE_ROOT/previous-release" ]]; then
    [[ -L "$STATE_ROOT/previous-release" ]] || {
      echo "skipping release pruning: previous release marker is unsafe" >&2
      return 0
    }
    previous_release="$(readlink -f "$STATE_ROOT/previous-release" 2> /dev/null || true)"
    if [[ -z "$previous_release" ]]; then
      echo "skipping release pruning: previous release marker is broken" >&2
      return 0
    fi
  fi
  if [[ -z "$current_release" ]]; then
    echo "skipping release pruning: current release marker is unavailable" >&2
    return 0
  fi

  current_name="${current_release##*/}"
  if [[ "${current_release%/*}" != "$release_root_real" ||
    ! "$current_name" =~ ^[0-9a-f]{40}$ ||
    ! -d "$current_release" || -L "$current_release" ]]; then
    echo "skipping release pruning: current release marker is not a canonical release" >&2
    return 0
  fi
  [[ "$(stat --format=%u "$current_release")" == "0" ]] || {
    echo "skipping release pruning: current release is not root-owned" >&2
    return 0
  }
  release_mode="$(stat --format=%a "$current_release")"
  (((8#$release_mode & 022) == 0)) || {
    echo "skipping release pruning: current release is group- or world-writable" >&2
    return 0
  }

  if [[ -n "$previous_release" ]]; then
    previous_name="${previous_release##*/}"
    if [[ "${previous_release%/*}" != "$release_root_real" ||
      ! "$previous_name" =~ ^[0-9a-f]{40}$ ||
      ! -d "$previous_release" || -L "$previous_release" ]]; then
      echo "skipping release pruning: previous release marker is not a canonical release" >&2
      return 0
    fi
    [[ "$(stat --format=%u "$previous_release")" == "0" ]] || {
      echo "skipping release pruning: previous release is not root-owned" >&2
      return 0
    }
    release_mode="$(stat --format=%a "$previous_release")"
    (((8#$release_mode & 022) == 0)) || {
      echo "skipping release pruning: previous release is group- or world-writable" >&2
      return 0
    }
  fi

  if ! release_candidates="$(
    find "$release_root_real" -mindepth 1 -maxdepth 1 -type d -mtime +14 -printf '%f\n'
  )"; then
    echo "could not enumerate release pruning candidates" >&2
    return 1
  fi

  while IFS= read -r release_name; do
    [[ -n "$release_name" ]] || continue
    [[ "$release_name" =~ ^[0-9a-f]{40}$ ]] || continue
    release_dir="$release_root_real/$release_name"
    [[ "$release_dir" == "$current_release" || "$release_dir" == "$previous_release" ]] && continue
    [[ -d "$release_dir" && ! -L "$release_dir" ]] || continue
    [[ "$(stat --format=%u "$release_dir")" == "0" ]] || continue
    release_head="$(git -C "$release_dir" rev-parse HEAD 2> /dev/null || true)"
    [[ "$release_head" == "$release_name" ]] || continue
    if ! cleanup_release_runtime_paths "$release_dir"; then
      echo "retaining release after guarded cleanup refusal: $release_dir" >&2
      continue
    fi
    if ! release_status="$(git -C "$release_dir" status --porcelain --untracked-files=normal)"; then
      echo "retaining release after Git status failure: $release_dir" >&2
      continue
    fi
    if [[ -z "$release_status" ]]; then
      git -C "$SOURCE_CHECKOUT" worktree remove "$release_dir"
    fi
  done <<< "$release_candidates"
  git -C "$SOURCE_CHECKOUT" worktree prune --expire=now
}

[[ "$EUID" -eq 0 ]] || fail "production reconciler must run as root"
[[ -d "$SOURCE_CHECKOUT" && ! -L "$SOURCE_CHECKOUT" ]] || fail "source checkout is unsafe: $SOURCE_CHECKOUT"
[[ -f "$RUNTIME_ENV" && ! -L "$RUNTIME_ENV" ]] || fail "runtime environment is missing or unsafe: $RUNTIME_ENV"
[[ -x "$DEPLOY_HELPER" ]] || fail "deploy helper is not installed: $DEPLOY_HELPER"
[[ -x "$LIVE_VERIFIER" ]] || fail "live verifier is not installed: $LIVE_VERIFIER"
[[ -x "$ARCHIVE_VALIDATOR" ]] || fail "archive validator is not installed: $ARCHIVE_VALIDATOR"
getent passwd "$BUILD_USER" > /dev/null || fail "build user does not exist: $BUILD_USER"
[[ "$MIN_FREE_KIB" =~ ^[0-9]+$ ]] || fail "minimum free space is invalid"
[[ "$BUILD_MIN_FREE_BYTES" =~ ^[0-9]+$ ]] || fail "production build minimum free disk is invalid"
((BUILD_MIN_FREE_BYTES >= 8589934592)) ||
  fail "production build minimum free disk must preserve at least the semantic-search reserve"

for command_name in git docker sha256sum flock install id rm mv awk getent chmod ln readlink find sort cut df stat rmdir python3 realpath tar curl mktemp; do
  require_command "$command_name"
done

install -d -o root -g root -m 0711 "$STATE_ROOT"
install -d -o root -g root -m 0700 \
  "$ARTIFACT_ROOT" "$RECEIPT_ROOT" "$DEPLOY_RECEIPT_ROOT" "$DOCKER_CONFIG"
install -d -o root -g root -m 0700 "$DEPLOY_RECEIPT_ROOT/contention"
install -d -o root -g root -m 0755 "$RELEASE_ROOT"
acquire_production_lock
prune_deploy_contention_receipts

available_kib="$(df -Pk "$ARTIFACT_ROOT" | awk 'NR==2 {print $4}')"
[[ "$available_kib" =~ ^[0-9]+$ ]] || fail "could not determine free disk space"
((available_kib >= MIN_FREE_KIB)) || fail "insufficient free disk space: ${available_kib}KiB available"

[[ "$(stat --format=%u "$SOURCE_CHECKOUT")" == "0" ]] || fail "source checkout is not root-owned"
git_common_dir="$(git -C "$SOURCE_CHECKOUT" rev-parse --git-common-dir)"
[[ "$git_common_dir" == /* ]] || git_common_dir="$SOURCE_CHECKOUT/$git_common_dir"
git_common_dir="$(readlink -f "$git_common_dir")"
unsafe_git_path="$(find "$git_common_dir" -xdev \( ! -user root -o -perm /022 \) -print -quit)"
[[ -z "$unsafe_git_path" ]] || fail "Git object store is not entirely root-owned and non-writable by group/world: $unsafe_git_path"

target_commit="$(fetch_main)"
[[ "$target_commit" =~ ^[0-9a-f]{40}$ ]] || fail "origin/main did not resolve to a full commit"
write_state "observed" "$target_commit" "reconcile started"

# Schaubild is a separately versioned native runtime served by this edge.
# A same-commit no-op therefore needs the exact reviewed OCI digest from this
# repository commit plus live runtime/public-manifest readback.
schauwerk_runtime_lock_json="$(
  git -C "$SOURCE_CHECKOUT" show "$target_commit:infra/schauwerk-editor/release-lock.json"
)" || fail "target commit is missing the Schaubild runtime lock"
IFS=$'\t' read -r expected_schauwerk_image_ref expected_schauwerk_source_commit <<< "$(
  SCHAUWERK_RUNTIME_LOCK_JSON="$schauwerk_runtime_lock_json" run_ops_python << 'PY_SCHAUWERK_RUNTIME_LOCK'
import json
import os
import re

try:
    payload = json.loads(os.environ["SCHAUWERK_RUNTIME_LOCK_JSON"])
except (KeyError, json.JSONDecodeError) as exc:
    raise SystemExit(f"invalid Schaubild runtime lock JSON: {exc}")
expected_keys = {
    "schema_version",
    "source_repository",
    "source_commit",
    "image_repository",
    "image_digest",
    "public_base_path",
}
if not isinstance(payload, dict) or set(payload) != expected_keys:
    raise SystemExit("Schaubild runtime lock field matrix is invalid")
if payload.get("schema_version") != "weltgewebe-schauwerk-runtime-lock.v1":
    raise SystemExit("Schaubild runtime lock schema is invalid")
if payload.get("source_repository") != "heimgewebe/schauwerk":
    raise SystemExit("Schaubild runtime lock repository is invalid")
if payload.get("image_repository") != "ghcr.io/heimgewebe/schauwerk-schaubild":
    raise SystemExit("Schaubild runtime image repository is invalid")
if payload.get("public_base_path") != "/schaubild":
    raise SystemExit("Schaubild runtime public base path is invalid")
source_commit = payload.get("source_commit")
image_digest = payload.get("image_digest")
if not isinstance(source_commit, str) or re.fullmatch(r"[0-9a-f]{40}", source_commit) is None:
    raise SystemExit("Schaubild source commit is invalid")
if not isinstance(image_digest, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest) is None:
    raise SystemExit("Schaubild image digest is invalid")
print(
    f"ghcr.io/heimgewebe/schauwerk-schaubild@{image_digest}\t{source_commit}"
)
PY_SCHAUWERK_RUNTIME_LOCK
)" || fail "target Schaubild runtime lock is invalid"
[[ "$expected_schauwerk_image_ref" =~ ^ghcr\.io/heimgewebe/schauwerk-schaubild@sha256:[0-9a-f]{64}$ ]] ||
  fail "target Schaubild image reference is invalid"
[[ "$expected_schauwerk_source_commit" =~ ^[0-9a-f]{40}$ ]] ||
  fail "target Schaubild source commit is invalid"

# Nationwide Germany is the production sovereign contract. Bind the no-op
# decision to the exact Germany style in this commit before public readback.
for germany_style in map-style/style-germany.json map-style/style-germany-dark.json; do
  git -C "$SOURCE_CHECKOUT" cat-file -e "$target_commit:$germany_style" ||
    fail "target commit is missing required nationwide Germany style: $germany_style"
done
expected_germany_style_sha="$(
  git -C "$SOURCE_CHECKOUT" show "$target_commit:map-style/style-germany.json" |
    sha256sum | awk '{print $1}'
)"
[[ "$expected_germany_style_sha" =~ ^[0-9a-f]{64}$ ]] ||
  fail "target nationwide Germany style hash is invalid"
expected_germany_dark_style_sha="$(
  git -C "$SOURCE_CHECKOUT" show "$target_commit:map-style/style-germany-dark.json" |
    sha256sum | awk '{print $1}'
)"
[[ "$expected_germany_dark_style_sha" =~ ^[0-9a-f]{64}$ ]] ||
  fail "target nationwide Germany dark style hash is invalid"

# The public build label alone is insufficient: the local data aliases that
# serve nationwide Germany must also be intact before any same-commit no-op.
germany_basemap_root="$SOURCE_CHECKOUT/build/basemap"
[[ -d "$germany_basemap_root" && ! -L "$germany_basemap_root" ]] ||
  fail "nationwide Germany basemap artifact root is missing or unsafe"
germany_basemap_real="$(realpath -e "$germany_basemap_root")" ||
  fail "nationwide Germany basemap artifact root cannot be resolved"
source_real="$(realpath -e "$SOURCE_CHECKOUT")" || fail "source checkout cannot be resolved"
[[ "$germany_basemap_real" == "$source_real/build/basemap" ]] ||
  fail "nationwide Germany basemap artifact root escaped the production checkout"
[[ "$(stat --format=%u "$germany_basemap_real")" == "0" ]] ||
  fail "nationwide Germany basemap artifact root is not root-owned"
germany_basemap_mode="$(stat --format=%a "$germany_basemap_real")"
(((8#$germany_basemap_mode & 022) == 0)) ||
  fail "nationwide Germany basemap artifact root is group- or world-writable"
germany_artifact_target=""
for germany_alias in basemap-germany.pmtiles basemap-germany.meta.json; do
  germany_path="$germany_basemap_real/$germany_alias"
  [[ -e "$germany_path" || -L "$germany_path" ]] ||
    fail "required nationwide Germany basemap alias is missing: $germany_alias"
  germany_target="$(realpath -e "$germany_path")" ||
    fail "required nationwide Germany basemap alias is broken: $germany_alias"
  case "$germany_target" in
    "$germany_basemap_real"/*) ;;
    *) fail "nationwide Germany basemap alias escapes canonical data root: $germany_alias" ;;
  esac
  [[ -f "$germany_target" && -s "$germany_target" ]] ||
    fail "nationwide Germany basemap alias is not a non-empty regular file: $germany_alias"
  [[ "$(stat --format=%u "$germany_target")" == "0" ]] ||
    fail "nationwide Germany basemap target is not root-owned: $germany_alias"
  germany_target_mode="$(stat --format=%a "$germany_target")"
  (((8#$germany_target_mode & 022) == 0)) ||
    fail "nationwide Germany basemap target is group- or world-writable: $germany_alias"
  if [[ "$germany_alias" == "basemap-germany.pmtiles" ]]; then
    germany_artifact_target="$germany_target"
  fi
done
[[ -n "$germany_artifact_target" ]] ||
  fail "nationwide Germany PMTiles target was not selected"
expected_germany_artifact_size="$(stat --format=%s "$germany_artifact_target")"
[[ "$expected_germany_artifact_size" =~ ^[0-9]+$ ]] ||
  fail "selected nationwide Germany PMTiles size is invalid"
((expected_germany_artifact_size >= 127)) ||
  fail "selected nationwide Germany PMTiles artifact is too small for the range proof"
expected_germany_range_sha="$(
  run_ops_python "$germany_artifact_target" << 'PY_SELECTED_GERMANY_RANGE'
import hashlib
import sys
from pathlib import Path

artifact_path = Path(sys.argv[1])
with artifact_path.open("rb") as artifact:
    payload = artifact.read(127)
if len(payload) != 127:
    raise SystemExit("selected Germany PMTiles artifact has fewer than 127 bytes")
print(hashlib.sha256(payload).hexdigest())
PY_SELECTED_GERMANY_RANGE
)" || fail "could not hash the selected nationwide Germany PMTiles range"
[[ "$expected_germany_range_sha" =~ ^[0-9a-f]{64}$ ]] ||
  fail "selected nationwide Germany PMTiles range hash is invalid"

initial_receipt="$RECEIPT_ROOT/observed-$target_commit.json"
if "$LIVE_VERIFIER" \
  --expected-commit "$target_commit" \
  --frontend-url "$FRONTEND_URL" \
  --api-url "$API_URL" \
  --output "$initial_receipt"; then
  basemap_identity_matches=0
  if verify_public_germany_basemap_delivery \
    "$target_commit" "$expected_germany_style_sha" "$expected_germany_dark_style_sha" \
    "$expected_germany_artifact_size" "$expected_germany_range_sha"; then
    basemap_identity_matches=1
  fi
  schauwerk_identity_matches=0
  if verify_public_schauwerk_runtime "$expected_schauwerk_image_ref"; then
    schauwerk_identity_matches=1
  fi
  observed_main="$(fetch_main)"
  if [[ "$observed_main" != "$target_commit" ]]; then
    write_state "superseded_after_observe" "$observed_main" "main advanced after public readback"
    echo "production_reconcile=superseded observed=$target_commit current=$observed_main"
    exit 0
  fi
  if [[ "$basemap_identity_matches" == "1" && "$schauwerk_identity_matches" == "1" ]]; then
    repair_observed_deployment_state "$initial_receipt"
    write_state "consistent_observed_unattested" "$target_commit" \
      "public identity, artifact declaration, Germany basemap delivery, and reviewed Schauwerk release matched; provenance remains unattested"
    ln -sfn "reconcile-receipts/$target_commit.json" "$STATE_ROOT/reconcile-current.json"
    prune_artifacts
    prune_releases
    echo "production_reconcile=noop commit=$target_commit state=consistent_observed_unattested basemap_variant=germany schauwerk_release=verified"
    exit 0
  fi
  if [[ "$schauwerk_identity_matches" != "1" ]]; then
    write_state "schauwerk_release_identity_drift" "$target_commit" \
      "public commit matched but the public Schaubild runtime did not match the reviewed OCI digest; redeploy required"
    echo "production_reconcile=repair_required commit=$target_commit reason=schaubild_runtime_image_identity_drift"
  else
    write_state "basemap_identity_drift" "$target_commit" \
      "public commit matched but nationwide Germany basemap identity or delivery routes did not; rebuild required"
    echo "production_reconcile=repair_required commit=$target_commit reason=basemap_identity_drift"
  fi
fi

ensure_production_build_disk_headroom "$target_commit"

build_uid="$(id -u "$BUILD_USER")"
build_gid="$(id -g "$BUILD_USER")"
[[ "$build_uid" =~ ^[0-9]+$ && "$build_gid" =~ ^[0-9]+$ ]] || fail "build user IDs are invalid"
commit_epoch="$(git -C "$SOURCE_CHECKOUT" show -s --format=%ct "$target_commit")"
[[ "$commit_epoch" =~ ^[0-9]+$ ]] || fail "target commit timestamp is invalid"

# The nationwide Germany styles and persistent data aliases were validated
# before the public no-op decision; reuse that proof for the build path.
write_state "building" "$target_commit" "nationwide Germany basemap pre-build guard passed"

source_archive="$ARTIFACT_ROOT/source-$target_commit.tar"
temporary_source="$source_archive.tmp.$$"
git -C "$SOURCE_CHECKOUT" archive --format=tar --output="$temporary_source" "$target_commit"
mv "$temporary_source" "$source_archive"
temporary_source=""
chmod 0444 "$source_archive"
write_state "building" "$target_commit" "source archive exported"

temporary_artifact="$ARTIFACT_ROOT/.web-$target_commit.$$.tmp"
(
  ulimit -f 262144
  docker run --rm \
    --platform linux/amd64 \
    --user "$build_uid:$build_gid" \
    --cap-drop ALL \
    --security-opt no-new-privileges \
    --pids-limit 512 \
    --memory 2g \
    --cpus 2 \
    --read-only \
    --tmpfs /tmp:rw,exec,nosuid,nodev,size=1g,mode=1777 \
    --tmpfs /workspace:rw,exec,nosuid,nodev,size=3g,mode=1777 \
    --network bridge \
    --mount "type=bind,src=$source_archive,dst=/source.tar,readonly" \
    --workdir /workspace \
    --env HOME=/tmp/home \
    --env COREPACK_HOME=/tmp/corepack \
    --env npm_config_cache=/tmp/npm-cache \
    --env CI=true \
    --env SOURCE_DATE_EPOCH="$commit_epoch" \
    --env GIT_COMMIT_SHA="$target_commit" \
    --env PUBLIC_BASEMAP_MODE=local-sovereign \
    --env PUBLIC_BASEMAP_VARIANT=germany \
    "$NODE_BUILD_IMAGE" \
    sh -lc '{ /usr/bin/tar -xf /source.tar -C /workspace && cd /workspace/apps/web && mkdir -p "$HOME" "$COREPACK_HOME" "$npm_config_cache" /tmp/bin && corepack enable --install-directory /tmp/bin pnpm && export PATH="/tmp/bin:$PATH" && pnpm install --frozen-lockfile && pnpm build; } >&2 && exec /usr/bin/tar --sort=name --mtime="@$SOURCE_DATE_EPOCH" --owner=0 --group=0 --numeric-owner -czf - build'
) > "$temporary_artifact"
rm -f -- "$source_archive"
source_archive=""

[[ -s "$temporary_artifact" && ! -L "$temporary_artifact" ]] || fail "frontend build stream is missing or unsafe"
"$ARCHIVE_VALIDATOR" "$temporary_artifact"

# Prove that the immutable web bundle was actually built for nationwide
# Germany. A correct environment variable is not enough; the bundle's own
# machine-readable identity must match the exact commit and Germany style.
basemap_identity_json="$(
  tar -xOzf "$temporary_artifact" build/_app/basemap-build.json 2> /dev/null
)" || fail "frontend artifact is missing readable basemap build identity"
[[ -n "$basemap_identity_json" ]] || fail "frontend basemap build identity is empty"
if ! BASEMAP_IDENTITY_JSON="$basemap_identity_json" run_ops_python "$target_commit" "$expected_germany_style_sha" << 'PY_BASEMAP_IDENTITY'; then
import json
import os
import re
import sys

commit, style_sha = sys.argv[1:3]
try:
    payload = json.loads(os.environ["BASEMAP_IDENTITY_JSON"])
except (KeyError, json.JSONDecodeError) as exc:
    raise SystemExit(f"invalid basemap identity JSON: {exc}")
expected = {
    "schema_version": 1,
    "mode": "local-sovereign",
    "variant": "germany",
    "style_path": "/local-basemap/style-germany.json",
    "source_commit": commit,
    "style_sha256": style_sha,
}
if not isinstance(payload, dict) or payload != expected:
    raise SystemExit(
        "basemap identity differs from expected nationwide Germany contract: "
        + json.dumps(payload, sort_keys=True)
    )
if not re.fullmatch(r"[0-9a-f]{40}", commit):
    raise SystemExit("expected source commit is malformed")
if not re.fullmatch(r"[0-9a-f]{64}", style_sha):
    raise SystemExit("expected style hash is malformed")
PY_BASEMAP_IDENTITY

  fail "frontend basemap build identity mismatch"
fi

artifact="$ARTIFACT_ROOT/web-$target_commit.tar.gz"
mv "$temporary_artifact" "$artifact"
temporary_artifact=""
chmod 0600 "$artifact"
artifact_sha="$(sha256sum "$artifact" | awk '{print $1}')"
[[ "$artifact_sha" =~ ^[0-9a-f]{64}$ ]] || fail "frontend artifact hash is invalid"
write_state "artifact_validated" "$target_commit" "frontend artifact ready"

current_main="$(fetch_main)"
if [[ "$current_main" != "$target_commit" ]]; then
  write_state "deferred" "$current_main" "main advanced after build"
  prune_artifacts
  echo "production_reconcile=deferred built=$target_commit current=$current_main"
  exit 0
fi

deploy_invocation_id="$(new_deploy_invocation_id)"
[[ "$deploy_invocation_id" =~ ^[0-9a-f]{64}$ ]] ||
  fail "deploy invocation identity generation failed"
set +e
WELTGEWEBE_PRODUCTION_LOCK_FD="$PRODUCTION_LOCK_FD" \
  WELTGEWEBE_PRODUCTION_LOCK_DOMAIN="$PRODUCTION_LOCK_DOMAIN" \
  WELTGEWEBE_PRODUCTION_LOCK_OWNER_ENTRYPOINT="reconciler" \
  WELTGEWEBE_DEPLOY_INVOCATION_ID="$deploy_invocation_id" \
  "$DEPLOY_HELPER" \
  --commit "$target_commit" \
  --web-artifact "$artifact" \
  --web-sha256 "$artifact_sha"
deploy_rc=$?
set -e
deploy_result=""
case "$deploy_rc" in
  0) ;;

  "$EXIT_SUPERSEDED_AFTER_MIGRATION")
    deploy_result="$(read_deploy_terminal_result "$target_commit" "$deploy_invocation_id")"
    [[ "$deploy_result" == "superseded_after_migration" ]] ||
      fail "deploy helper exit/result mismatch: exit=$deploy_rc result=$deploy_result"
    ;;
  "$EXIT_SUPERSEDED_AFTER_DEPLOY")
    deploy_result="$(read_deploy_terminal_result "$target_commit" "$deploy_invocation_id")"
    [[ "$deploy_result" == "superseded_after_deploy" ]] ||
      fail "deploy helper exit/result mismatch: exit=$deploy_rc result=$deploy_result"
    ;;
  "$EX_TEMPFAIL")
    tempfail_diagnostic="$(read_deploy_tempfail_diagnostic "$target_commit" "$deploy_invocation_id")"
    case "$tempfail_diagnostic" in
      failed_deployment)
        fail "deploy helper returned child temporary failure after writing a failed receipt for the current invocation"
        ;;
      lock_contention)
        fail "deploy helper reported production lock contention during inherited handoff"
        ;;
      untrusted_receipt)
        fail "deploy helper returned temporary failure with unsafe receipt evidence"
        ;;
      *)
        fail "deploy helper returned unexplained temporary failure under inherited production lock"
        ;;
    esac
    ;;
  *)
    fail "deploy helper failed with exit code $deploy_rc"
    ;;
esac

if [[ -n "$deploy_result" ]]; then
  current_main="$(fetch_main)"
  case "$deploy_result" in
    superseded_after_migration)
      write_state \
        "superseded_after_migration" \
        "$current_main" \
        "migration completed; full deploy skipped after main advanced"
      echo "production_reconcile=superseded_after_migration migrated=$target_commit current=$current_main"
      ;;
    superseded_after_deploy)
      write_state \
        "superseded_after_deploy" \
        "$current_main" \
        "full deploy completed after main advanced"
      echo "production_reconcile=superseded_after_deploy deployed=$target_commit current=$current_main"
      ;;
    *)
      fail "unexpected superseded reason: $deploy_result"
      ;;
  esac
  prune_artifacts
  exit 0
fi

final_receipt="$RECEIPT_ROOT/public-$target_commit.json"
"$LIVE_VERIFIER" \
  --expected-commit "$target_commit" \
  --frontend-url "$FRONTEND_URL" \
  --api-url "$API_URL" \
  --wait-seconds 120 \
  --poll-seconds 5 \
  --output "$final_receipt"
verify_public_germany_basemap_delivery \
  "$target_commit" "$expected_germany_style_sha" "$expected_germany_dark_style_sha" \
  "$expected_germany_artifact_size" "$expected_germany_range_sha" ||
  fail "public nationwide Germany basemap delivery mismatch after deploy"
verify_public_schauwerk_runtime "$expected_schauwerk_image_ref" ||
  fail "public Schaubild runtime does not match the reviewed OCI digest after deploy"

current_main="$(fetch_main)"
if [[ "$current_main" != "$target_commit" ]]; then
  write_state "superseded_after_verify" "$current_main" "public readback passed after main advanced"
  prune_artifacts
  echo "production_reconcile=superseded verified=$target_commit current=$current_main"
  exit 0
fi

write_state "verified" "$current_main" "public readback and main identity agree"
ln -sfn "reconcile-receipts/$target_commit.json" "$STATE_ROOT/reconcile-current.json"
prune_artifacts
prune_releases
echo "production_reconcile=verified commit=$target_commit artifact_sha256=$artifact_sha receipt=$RECEIPT_ROOT/$target_commit.json"
