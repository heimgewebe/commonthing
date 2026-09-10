#!/usr/bin/env bash
set -euo pipefail

# Reproducible nationwide Germany PMTiles build.
#
# The default input remains a pinned historical Geofabrik snapshot. A newer
# snapshot may be supplied only as a complete, non-empty provenance tuple:
# filename, URL, SHA256 and snapshot date. The integrity check remains
# mandatory. Versioned outputs are immutable: an existing version is never
# replaced. This script never changes a stable alias or production config.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" > /dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." > /dev/null 2>&1 && pwd)"
BASEMAP_DIR="${BASEMAP_DIR:-$REPO_ROOT/build/basemap}"

DEFAULT_OSM_FILE="germany-260101.osm.pbf"
DEFAULT_OSM_URL="https://download.geofabrik.de/europe/germany-260101.osm.pbf"
DEFAULT_OSM_SHA256="4a2e3181c2cef4795b62ef9b447d4fa5f7f9bb2352d563292a7b98baa75279f8"
DEFAULT_OSM_SNAPSHOT_DATE="2026-01-01"

OSM_FILE_WAS_SET="${OSM_FILE+x}"
OSM_URL_WAS_SET="${OSM_URL+x}"
OSM_SHA256_WAS_SET="${OSM_SHA256+x}"
OSM_SNAPSHOT_DATE_WAS_SET="${OSM_SNAPSHOT_DATE+x}"

BASEMAP_VERSION="${BASEMAP_VERSION:-0.1.0}"
BASEMAP_TAG="v${BASEMAP_VERSION}"
OUTPUT_PMTILES="basemap-germany-${BASEMAP_TAG}.pmtiles"
OUTPUT_META="basemap-germany-${BASEMAP_TAG}.meta.json"
OUTPUT_BUILD_RECEIPT="basemap-germany-${BASEMAP_TAG}.build.json"
OUTPUT_PMTILES_STEM="${OUTPUT_PMTILES%.pmtiles}"
PARTIAL_PMTILES=".${OUTPUT_PMTILES_STEM}.partial.$$.pmtiles"
PARTIAL_META=".${OUTPUT_META}.partial.$$"
PARTIAL_BUILD_RECEIPT=".${OUTPUT_BUILD_RECEIPT}.partial.$$"
RAW_MEASUREMENT=".${OUTPUT_PMTILES_STEM}.measurement.raw.$$"
MIN_FREE_BYTES="${BASEMAP_MIN_FREE_BYTES:-68719476736}"

PLANETILER_IMAGE="ghcr.io/onthegomap/planetiler@sha256:10e4d6850664bd2ad7a223623383c48281e7d87fb427360838b13342cac012bb"
PLANETILER_AUXILIARY_CACHE_VERSION="0.8.2"
LAKE_CENTERLINES_FILE="lake_centerline.shp.zip"
LAKE_CENTERLINES_URL="https://dev.maptiler.download/geodata/omt/lake_centerline.shp.zip"
LAKE_CENTERLINES_SHA256="56dd891eb6d23315c8176ef4f23f9ae6e152386403d7c7ab0aa54537c8ab16b1"
WATER_POLYGONS_FILE="water-polygons-split-3857.zip"
WATER_POLYGONS_URL="https://osmdata.openstreetmap.de/download/water-polygons-split-3857.zip"
WATER_POLYGONS_SHA256="e825568ab0b1cf846744a5a3e98a84e90a557da645d856f12d538ee7bffa8eff"
NATURAL_EARTH_FILE="natural_earth_vector.sqlite.zip"
NATURAL_EARTH_URL="https://dev.maptiler.download/geodata/omt/natural_earth_vector.sqlite.zip"
NATURAL_EARTH_SHA256="bd9420e7303f6abd4df5245be57107eec3ed4fa0246d31fa63bbc6ec95aaeb56"

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

SNAPSHOT_OVERRIDE_COUNT=0
for marker in \
  "$OSM_FILE_WAS_SET" \
  "$OSM_URL_WAS_SET" \
  "$OSM_SHA256_WAS_SET" \
  "$OSM_SNAPSHOT_DATE_WAS_SET"; do
  [[ -n "$marker" ]] && SNAPSHOT_OVERRIDE_COUNT=$((SNAPSHOT_OVERRIDE_COUNT + 1))
done

case "$SNAPSHOT_OVERRIDE_COUNT" in
  0)
    OSM_FILE="$DEFAULT_OSM_FILE"
    OSM_URL="$DEFAULT_OSM_URL"
    OSM_SHA256="$DEFAULT_OSM_SHA256"
    OSM_SNAPSHOT_DATE="$DEFAULT_OSM_SNAPSHOT_DATE"
    ;;
  4)
    [[ -n "$OSM_FILE" ]] || fail "OSM_FILE override must not be empty"
    [[ -n "$OSM_URL" ]] || fail "OSM_URL override must not be empty"
    [[ -n "$OSM_SHA256" ]] || fail "OSM_SHA256 override must not be empty"
    [[ -n "$OSM_SNAPSHOT_DATE" ]] ||
      fail "OSM_SNAPSHOT_DATE override must not be empty"
    ;;
  *)
    fail "override OSM_FILE, OSM_URL, OSM_SHA256 and OSM_SNAPSHOT_DATE together"
    ;;
esac

[[ "$OSM_FILE" == "$(basename -- "$OSM_FILE")" ]] ||
  fail "OSM_FILE must be a plain filename inside BASEMAP_DIR"
[[ "$OSM_FILE" == *.osm.pbf ]] || fail "OSM_FILE must end in .osm.pbf"
[[ "$OSM_URL" =~ ^https:// ]] || fail "OSM_URL must use HTTPS"
[[ "$OSM_SHA256" =~ ^[0-9a-f]{64}$ ]] ||
  fail "OSM_SHA256 must be exactly 64 lowercase hexadecimal characters"
[[ "$OSM_SNAPSHOT_DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] ||
  fail "OSM_SNAPSHOT_DATE must use YYYY-MM-DD"
[[ "$BASEMAP_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] ||
  fail "BASEMAP_VERSION must use numeric semantic versioning"
case "$MIN_FREE_BYTES" in
  '' | *[!0-9]*) fail "BASEMAP_MIN_FREE_BYTES must be a positive integer" ;;
esac
((MIN_FREE_BYTES > 0)) || fail "BASEMAP_MIN_FREE_BYTES must be greater than zero"

command -v docker > /dev/null 2>&1 || fail "docker is required"
command -v python3 > /dev/null 2>&1 || fail "python3 is required"

python3 - "$OSM_SNAPSHOT_DATE" << 'PY'
import datetime as dt
import sys

try:
    snapshot = dt.date.fromisoformat(sys.argv[1])
except ValueError as exc:
    raise SystemExit(f"invalid OSM_SNAPSHOT_DATE: {sys.argv[1]}") from exc
if snapshot > dt.datetime.now(dt.timezone.utc).date():
    raise SystemExit(f"OSM_SNAPSHOT_DATE lies in the future: {snapshot}")
PY

if command -v sha256sum > /dev/null 2>&1; then
  SHA256_CMD=(sha256sum)
elif command -v shasum > /dev/null 2>&1; then
  SHA256_CMD=(shasum -a 256)
else
  fail "sha256sum or shasum is required"
fi

if command -v wget > /dev/null 2>&1; then
  DOWNLOADER="wget"
elif command -v curl > /dev/null 2>&1; then
  DOWNLOADER="curl"
else
  fail "wget or curl is required"
fi

mkdir -p "$BASEMAP_DIR"
BASEMAP_DIR="$(cd "$BASEMAP_DIR" > /dev/null 2>&1 && pwd)"
AUXILIARY_REL_DIR="auxiliary-v${PLANETILER_AUXILIARY_CACHE_VERSION}"
AUXILIARY_DIR="$BASEMAP_DIR/$AUXILIARY_REL_DIR"
mkdir -p "$AUXILIARY_DIR"
AUXILIARY_DIR="$(cd "$AUXILIARY_DIR" > /dev/null 2>&1 && pwd)"
AVAILABLE_BYTES="$(df -Pk "$BASEMAP_DIR" | awk 'NR==2 {printf "%.0f\n", $4 * 1024}')"
case "$AVAILABLE_BYTES" in
  '' | *[!0-9]*) fail "could not determine free disk space for $BASEMAP_DIR" ;;
esac
if ((AVAILABLE_BYTES < MIN_FREE_BYTES)); then
  fail "insufficient free disk: ${AVAILABLE_BYTES} bytes available, ${MIN_FREE_BYTES} required"
fi

if [[ -r /proc/meminfo ]]; then
  AVAILABLE_MEMORY_BYTES="$(awk '/^MemAvailable:/ {printf "%.0f\n", $2 * 1024}' /proc/meminfo)"
  echo "Available memory: ${AVAILABLE_MEMORY_BYTES:-unknown} bytes"
fi

echo "=== Weltgewebe Germany Basemap Builder ==="
echo "Version:       ${BASEMAP_VERSION}"
echo "Snapshot date: ${OSM_SNAPSHOT_DATE}"
echo "Input:         ${OSM_FILE}"
echo "Output:        ${OUTPUT_PMTILES}"
echo "Build dir:     ${BASEMAP_DIR}"
echo "Free bytes:    ${AVAILABLE_BYTES}"
echo "Toolchain:     ${PLANETILER_IMAGE}"
echo "========================================="

cd "$BASEMAP_DIR"

FINAL_PMTILES_PATH="$BASEMAP_DIR/$OUTPUT_PMTILES"
FINAL_META_PATH="$BASEMAP_DIR/$OUTPUT_META"
FINAL_BUILD_RECEIPT_PATH="$BASEMAP_DIR/$OUTPUT_BUILD_RECEIPT"
PARTIAL_PMTILES_PATH="$BASEMAP_DIR/$PARTIAL_PMTILES"
PARTIAL_META_PATH="$BASEMAP_DIR/$PARTIAL_META"
PARTIAL_BUILD_RECEIPT_PATH="$BASEMAP_DIR/$PARTIAL_BUILD_RECEIPT"
RAW_MEASUREMENT_PATH="$BASEMAP_DIR/$RAW_MEASUREMENT"
PARTIAL_INPUT_PATH=""
PARTIAL_AUXILIARY_PATHS=()
PARTIAL_LAYERSTATS_PATH="${PARTIAL_PMTILES_PATH}.layerstats.tsv.gz"
FINAL_ARTIFACT_CREATED=0
FINAL_BUILD_RECEIPT_CREATED=0
FINAL_META_CREATED=0
PUBLISH_COMPLETE=0

cleanup_build() {
  rm -f -- \
    "$PARTIAL_PMTILES_PATH" \
    "$PARTIAL_META_PATH" \
    "$PARTIAL_BUILD_RECEIPT_PATH" \
    "$RAW_MEASUREMENT_PATH" \
    "$PARTIAL_LAYERSTATS_PATH"
  if [[ -n "$PARTIAL_INPUT_PATH" ]]; then
    rm -f -- "$PARTIAL_INPUT_PATH"
  fi
  if ((${#PARTIAL_AUXILIARY_PATHS[@]} > 0)); then
    rm -f -- "${PARTIAL_AUXILIARY_PATHS[@]}"
  fi
  if [[ "$PUBLISH_COMPLETE" != "1" ]]; then
    [[ "$FINAL_META_CREATED" == "1" ]] && rm -f -- "$FINAL_META_PATH"
    [[ "$FINAL_BUILD_RECEIPT_CREATED" == "1" ]] && rm -f -- "$FINAL_BUILD_RECEIPT_PATH"
    [[ "$FINAL_ARTIFACT_CREATED" == "1" ]] && rm -f -- "$FINAL_PMTILES_PATH"
  fi
  return 0
}

on_interrupt() {
  exit 130
}

on_terminate() {
  exit 143
}

trap cleanup_build EXIT
trap on_interrupt INT
trap on_terminate TERM

for immutable_output in "$FINAL_PMTILES_PATH" "$FINAL_BUILD_RECEIPT_PATH" "$FINAL_META_PATH"; do
  if [[ -e "$immutable_output" || -L "$immutable_output" ]]; then
    fail "versioned output already exists: $immutable_output; choose a new BASEMAP_VERSION"
  fi
done

if [[ ! -f "$OSM_FILE" ]]; then
  PARTIAL_INPUT_PATH="$BASEMAP_DIR/.${OSM_FILE}.partial.$$"
  echo ">> Downloading pinned Germany OSM snapshot..."
  if [[ "$DOWNLOADER" == "wget" ]]; then
    wget -qO "$PARTIAL_INPUT_PATH" "$OSM_URL" ||
      fail "download failed: $OSM_URL"
  else
    curl -fL --retry 3 --retry-delay 5 -o "$PARTIAL_INPUT_PATH" "$OSM_URL" ||
      fail "download failed: $OSM_URL"
  fi
  if [[ -e "$OSM_FILE" || -L "$OSM_FILE" ]]; then
    fail "OSM input appeared concurrently: $BASEMAP_DIR/$OSM_FILE"
  fi
  mv "$PARTIAL_INPUT_PATH" "$OSM_FILE"
  PARTIAL_INPUT_PATH=""
else
  echo ">> Reusing existing input: $OSM_FILE"
fi

ACTUAL_INPUT_SHA256="$("${SHA256_CMD[@]}" "$OSM_FILE" | awk '{print $1}')"
[[ "$ACTUAL_INPUT_SHA256" == "$OSM_SHA256" ]] || {
  echo "Expected: $OSM_SHA256" >&2
  echo "Actual:   $ACTUAL_INPUT_SHA256" >&2
  fail "input checksum mismatch"
}
echo "   [✓] Input integrity verified"

download_verified_auxiliary() {
  local label="$1"
  local url="$2"
  local expected_sha256="$3"
  local target="$4"
  local partial actual_sha256

  [[ "$url" =~ ^https:// ]] || fail "$label URL must use HTTPS"
  [[ "$expected_sha256" =~ ^[0-9a-f]{64}$ ]] ||
    fail "$label SHA256 must be exactly 64 lowercase hexadecimal characters"
  [[ ! -L "$target" ]] || fail "$label cache path must not be a symlink: $target"

  if [[ ! -e "$target" ]]; then
    partial="${target}.partial.$$"
    PARTIAL_AUXILIARY_PATHS+=("$partial")
    rm -f -- "$partial"
    echo ">> Downloading pinned $label source..."
    if [[ "$DOWNLOADER" == "wget" ]]; then
      if wget --tries=5 --waitretry=3 --retry-connrefused --timeout=30 -nv \
        -O "$partial" "$url"; then
        :
      else
        download_rc=$?
        case "$download_rc" in
          4) fail "$label download network/timeout failure after bounded retries: $url" ;;
          8) fail "$label download HTTP/server failure after bounded retries: $url" ;;
          *) fail "$label download failed after bounded retries (wget exit $download_rc): $url" ;;
        esac
      fi
    else
      if curl -fL --retry 4 --retry-delay 3 --retry-all-errors \
        --connect-timeout 30 --speed-limit 1024 --speed-time 30 \
        -o "$partial" "$url"; then
        :
      else
        download_rc=$?
        case "$download_rc" in
          22) fail "$label download HTTP failure after bounded retries: $url" ;;
          28) fail "$label download timeout/stall failure after bounded retries: $url" ;;
          *) fail "$label download failed after bounded retries (curl exit $download_rc): $url" ;;
        esac
      fi
    fi
    [[ -s "$partial" ]] || fail "$label download produced an empty file"
    if [[ -e "$target" || -L "$target" ]]; then
      fail "$label cache appeared concurrently: $target"
    fi
    mv "$partial" "$target"
  fi

  [[ -f "$target" && -s "$target" ]] ||
    fail "$label cache must be a non-empty regular file: $target"
  actual_sha256="$("${SHA256_CMD[@]}" "$target" | awk '{print $1}')"
  [[ "$actual_sha256" == "$expected_sha256" ]] || {
    echo "Expected $label SHA256: $expected_sha256" >&2
    echo "Actual $label SHA256:   $actual_sha256" >&2
    fail "$label checksum mismatch"
  }
  echo "   [✓] $label integrity verified"
}

LAKE_CENTERLINES_PATH="$AUXILIARY_DIR/$LAKE_CENTERLINES_FILE"
WATER_POLYGONS_PATH="$AUXILIARY_DIR/$WATER_POLYGONS_FILE"
NATURAL_EARTH_PATH="$AUXILIARY_DIR/$NATURAL_EARTH_FILE"
download_verified_auxiliary "lake centerlines" "$LAKE_CENTERLINES_URL" "$LAKE_CENTERLINES_SHA256" "$LAKE_CENTERLINES_PATH"
download_verified_auxiliary "water polygons" "$WATER_POLYGONS_URL" "$WATER_POLYGONS_SHA256" "$WATER_POLYGONS_PATH"
download_verified_auxiliary "Natural Earth" "$NATURAL_EARTH_URL" "$NATURAL_EARTH_SHA256" "$NATURAL_EARTH_PATH"

BUILD_RUN_ID="${GERMANY_BASEMAP_BUILD_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
[[ "$BUILD_RUN_ID" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$ ]] ||
  fail "GERMANY_BASEMAP_BUILD_RUN_ID has an invalid container identifier"
BUILD_CONTAINER_NAME="weltgewebe-germany-basemap-${BUILD_RUN_ID}"
DOCKER_RUN_ARGS=(
  --platform linux/amd64
  --user "$(id -u):$(id -g)"
  --mount "type=bind,src=$BASEMAP_DIR,dst=/data"
)
if [[ -n "${BASEMAP_DOCKER_MEMORY:-}" ]]; then
  DOCKER_RUN_ARGS+=(--memory "$BASEMAP_DOCKER_MEMORY")
fi

rm -f -- "$PARTIAL_PMTILES_PATH" "$PARTIAL_LAYERSTATS_PATH" "$RAW_MEASUREMENT_PATH"
if ! python3 "$SCRIPT_DIR/run-measured-container.py" \
  --container-name "$BUILD_CONTAINER_NAME" \
  --workspace "$BASEMAP_DIR" \
  --receipt "$RAW_MEASUREMENT_PATH" \
  --sample-interval-seconds "${GERMANY_BASEMAP_MEASUREMENT_INTERVAL_SECONDS:-1}" \
  -- docker run "${DOCKER_RUN_ARGS[@]}" "$PLANETILER_IMAGE" \
  --osm-path="/data/$OSM_FILE" \
  --lake-centerlines-path="/data/$AUXILIARY_REL_DIR/$LAKE_CENTERLINES_FILE" \
  --water-polygons-path="/data/$AUXILIARY_REL_DIR/$WATER_POLYGONS_FILE" \
  --natural-earth-path="/data/$AUXILIARY_REL_DIR/$NATURAL_EARTH_FILE" \
  --use-wikidata=false \
  --output="/data/$PARTIAL_PMTILES"; then
  fail "measured Planetiler execution failed"
fi

[[ -s "$PARTIAL_PMTILES_PATH" ]] || fail "Planetiler produced no non-empty artifact"

PMTILES_SIZE="$(wc -c < "$PARTIAL_PMTILES_PATH" | tr -d '[:space:]')"
PMTILES_SHA256="$("${SHA256_CMD[@]}" "$PARTIAL_PMTILES_PATH" | awk '{print $1}')"
[[ -n "$PMTILES_SHA256" && "$PMTILES_SIZE" -gt 0 ]] || fail "invalid artifact hash or size"

RAW_MEASUREMENT_PATH="$RAW_MEASUREMENT_PATH" \
  BUILD_RECEIPT_PATH="$PARTIAL_BUILD_RECEIPT_PATH" \
  RECEIPT_VERSION="$BASEMAP_VERSION" \
  RECEIPT_TOOLCHAIN="$PLANETILER_IMAGE" \
  RECEIPT_INPUT_URL="$OSM_URL" \
  RECEIPT_INPUT_FILE="$OSM_FILE" \
  RECEIPT_SNAPSHOT_DATE="$OSM_SNAPSHOT_DATE" \
  RECEIPT_INPUT_SHA256="$OSM_SHA256" \
  RECEIPT_ARTIFACT_NAME="$OUTPUT_PMTILES" \
  RECEIPT_ARTIFACT_SHA256="$PMTILES_SHA256" \
  RECEIPT_ARTIFACT_SIZE="$PMTILES_SIZE" \
  python3 << 'PY'
import json, os
from pathlib import Path
raw=json.loads(Path(os.environ["RAW_MEASUREMENT_PATH"]).read_text())
if raw.get("schema_version")!=1 or raw.get("contract")!="bounded-docker-build-measurement-v1": raise SystemExit("raw build measurement contract mismatch")
if raw.get("status")!="completed" or raw.get("exit_code")!=0: raise SystemExit("raw build measurement did not complete successfully")
if raw.get("oom_killed") is not False: raise SystemExit("measured build was OOM-killed")
if type(raw.get("sample_count")) is not int or raw["sample_count"]<=0: raise SystemExit("measured build contains no resource samples")
if not isinstance(raw.get("duration_seconds"),(int,float)) or raw["duration_seconds"]<=0: raise SystemExit("measured build duration is invalid")
peaks=raw.get("peaks")
if not isinstance(peaks,dict): raise SystemExit("measured build peaks are missing")
for field in ("cpu_percent","memory_bytes","workspace_growth_bytes","filesystem_consumed_bytes"):
    value=peaks.get(field)
    if not isinstance(value,(int,float)) or value<=0: raise SystemExit(f"measured build peak {field} must be positive")
payload={
 "schema_version":1,"verdict":"PROVEN","contract":"germany-pmtiles-measured-build-v1",
 "version":os.environ["RECEIPT_VERSION"],"region":"germany",
 "toolchain":{"generator":"planetiler","image":os.environ["RECEIPT_TOOLCHAIN"]},
 "input":{"url":os.environ["RECEIPT_INPUT_URL"],"file":os.environ["RECEIPT_INPUT_FILE"],"snapshot_date":os.environ["RECEIPT_SNAPSHOT_DATE"],"sha256":os.environ["RECEIPT_INPUT_SHA256"]},
 "artifact":{"name":os.environ["RECEIPT_ARTIFACT_NAME"],"sha256":os.environ["RECEIPT_ARTIFACT_SHA256"],"size_bytes":int(os.environ["RECEIPT_ARTIFACT_SIZE"])},
 "execution":{"started_at":raw["started_at"],"finished_at":raw["finished_at"],"duration_seconds":raw["duration_seconds"],"sample_interval_seconds":raw["sample_interval_seconds"],"sample_count":raw["sample_count"],"command_sha256":raw["command_sha256"],"container_name":raw["container_name"],"oom_killed":raw["oom_killed"]},
 "measurement_scope":raw["measurement_scope"],"baseline":raw["baseline"],"peaks":peaks}
Path(os.environ["BUILD_RECEIPT_PATH"]).write_text(json.dumps(payload,indent=2,sort_keys=True)+"\n")
PY
[[ -s "$PARTIAL_BUILD_RECEIPT_PATH" ]] || fail "measured build receipt was not written"

BUILD_TIMESTAMP_VALUE=""
if [[ "${NON_REPRODUCIBLE_BUILD_TIMESTAMP:-}" == "1" ]]; then
  BUILD_TIMESTAMP_VALUE="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
elif [[ -n "${SOURCE_DATE_EPOCH:-}" ]]; then
  BUILD_TIMESTAMP_VALUE="$(date -u -d "@${SOURCE_DATE_EPOCH}" +"%Y-%m-%dT%H:%M:%SZ")" || BUILD_TIMESTAMP_VALUE=""
fi

META_VERSION="$BASEMAP_VERSION" \
  META_TOOLCHAIN="$PLANETILER_IMAGE" \
  META_INPUT_URL="$OSM_URL" \
  META_INPUT_FILE="$OSM_FILE" \
  META_SNAPSHOT_DATE="$OSM_SNAPSHOT_DATE" \
  META_INPUT_SHA256="$OSM_SHA256" \
  META_LAKE_CENTERLINES_URL="$LAKE_CENTERLINES_URL" \
  META_LAKE_CENTERLINES_SHA256="$LAKE_CENTERLINES_SHA256" \
  META_WATER_POLYGONS_URL="$WATER_POLYGONS_URL" \
  META_WATER_POLYGONS_SHA256="$WATER_POLYGONS_SHA256" \
  META_NATURAL_EARTH_URL="$NATURAL_EARTH_URL" \
  META_NATURAL_EARTH_SHA256="$NATURAL_EARTH_SHA256" \
  META_ARTIFACT_NAME="$OUTPUT_PMTILES" \
  META_ARTIFACT_SHA256="$PMTILES_SHA256" \
  META_ARTIFACT_SIZE="$PMTILES_SIZE" \
  META_BUILD_RECEIPT_NAME="$OUTPUT_BUILD_RECEIPT" \
  META_BUILD_TIMESTAMP="$BUILD_TIMESTAMP_VALUE" \
  python3 - "$PARTIAL_META_PATH" << 'PY'
import json
import os
import pathlib
import sys

payload = {
    "schema_version": 1,
    "version": os.environ["META_VERSION"],
    "region": "germany",
    "country_code": "DE",
    "activation": "opt-in",
    "toolchain": {
        "generator": "planetiler",
        "image": os.environ["META_TOOLCHAIN"],
    },
    "input": {
        "url": os.environ["META_INPUT_URL"],
        "file": os.environ["META_INPUT_FILE"],
        "snapshot_date": os.environ["META_SNAPSHOT_DATE"],
        "sha256": os.environ["META_INPUT_SHA256"],
    },
    "auxiliary_sources": {
        "lake_centerlines": {
            "url": os.environ["META_LAKE_CENTERLINES_URL"],
            "sha256": os.environ["META_LAKE_CENTERLINES_SHA256"],
        },
        "water_polygons": {
            "url": os.environ["META_WATER_POLYGONS_URL"],
            "sha256": os.environ["META_WATER_POLYGONS_SHA256"],
        },
        "natural_earth": {
            "url": os.environ["META_NATURAL_EARTH_URL"],
            "sha256": os.environ["META_NATURAL_EARTH_SHA256"],
        },
        "wikidata": {"enabled": False},
    },
    "artifact_name": os.environ["META_ARTIFACT_NAME"],
    "sha256": os.environ["META_ARTIFACT_SHA256"],
    "size_bytes": int(os.environ["META_ARTIFACT_SIZE"]),
    "build_receipt": os.environ["META_BUILD_RECEIPT_NAME"],
    "status": "ready",
}
build_timestamp = os.environ.get("META_BUILD_TIMESTAMP", "")
if build_timestamp:
    payload["build_timestamp"] = build_timestamp
pathlib.Path(sys.argv[1]).write_text(
    json.dumps(payload, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY

[[ -s "$PARTIAL_META_PATH" ]] || fail "metadata sentinel was not written"
[[ -s "$PARTIAL_BUILD_RECEIPT_PATH" ]] || fail "build receipt was not written"

publish_immutable_triplet() {
  local failed=0

  trap '' INT TERM

  if ! ln "$PARTIAL_PMTILES_PATH" "$FINAL_PMTILES_PATH"; then
    echo "ERROR: could not publish immutable Germany artifact: $FINAL_PMTILES_PATH" >&2
    failed=1
  else
    FINAL_ARTIFACT_CREATED=1
  fi

  if [[ "$failed" == "0" ]]; then
    if ! ln "$PARTIAL_BUILD_RECEIPT_PATH" "$FINAL_BUILD_RECEIPT_PATH"; then
      echo "ERROR: could not publish immutable Germany build receipt: $FINAL_BUILD_RECEIPT_PATH" >&2
      failed=1
    else
      FINAL_BUILD_RECEIPT_CREATED=1
    fi
  fi

  if [[ "$failed" == "0" ]]; then
    if ! ln "$PARTIAL_META_PATH" "$FINAL_META_PATH"; then
      echo "ERROR: could not publish immutable Germany metadata: $FINAL_META_PATH" >&2
      failed=1
    else
      FINAL_META_CREATED=1
      PUBLISH_COMPLETE=1
    fi
  fi

  trap on_interrupt INT
  trap on_terminate TERM
  return "$failed"
}

publish_immutable_triplet || fail "could not publish the immutable Germany version triplet"
cleanup_build
trap - EXIT INT TERM

echo "   [✓] Germany artifact ready"
echo "Artifact: $FINAL_PMTILES_PATH"
echo "Metadata: $FINAL_META_PATH"
echo "Build receipt: $FINAL_BUILD_RECEIPT_PATH"
echo "SHA256:   $PMTILES_SHA256"
echo "Size:     $PMTILES_SIZE bytes"
echo "No stable alias or production setting was changed."
