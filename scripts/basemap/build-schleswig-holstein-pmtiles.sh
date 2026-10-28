#!/usr/bin/env bash
set -euo pipefail

# Operational bootstrap for the sovereign Schleswig-Holstein PMTiles basemap.
# The OSM input and Planetiler image are pinned and hash-verified. Planetiler
# retries write only to a process-local partial path; immutable version files are
# published without replacement after a complete successful build.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" > /dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." > /dev/null 2>&1 && pwd)"
BASEMAP_DIR="$REPO_ROOT/build/basemap"

OSM_FILE="schleswig-holstein-260101.osm.pbf"
OSM_URL="https://download.geofabrik.de/europe/germany/schleswig-holstein-260101.osm.pbf"
OSM_SHA256="3e5efb30488daa87a098d61bf1f60155f89cdcf900aeadee5a1cd27910bfd450"

BASEMAP_VERSION="0.1.0"
BASEMAP_TAG="v${BASEMAP_VERSION}"
OUTPUT_PMTILES="basemap-schleswig-holstein-${BASEMAP_TAG}.pmtiles"
OUTPUT_META="basemap-schleswig-holstein-${BASEMAP_TAG}.meta.json"
OUTPUT_PMTILES_STEM="${OUTPUT_PMTILES%.pmtiles}"
PARTIAL_PMTILES=".${OUTPUT_PMTILES_STEM}.partial.$$.pmtiles"
PARTIAL_META=".${OUTPUT_META}.partial.$$"

PLANETILER_IMAGE="ghcr.io/onthegomap/planetiler@sha256:10e4d6850664bd2ad7a223623383c48281e7d87fb427360838b13342cac012bb"
PLANETILER_HTTP_TIMEOUT="${BASEMAP_PLANETILER_HTTP_TIMEOUT:-120s}"
PLANETILER_HTTP_RETRIES="${BASEMAP_PLANETILER_HTTP_RETRIES:-3}"
PLANETILER_HTTP_RETRY_WAIT="${BASEMAP_PLANETILER_HTTP_RETRY_WAIT:-10s}"

echo "=== Weltgewebe Basemap Builder ==="
echo "Target:  Schleswig-Holstein"
echo "Version: ${BASEMAP_VERSION} (Tag: ${BASEMAP_TAG})"
echo "Tool:    Planetiler (Pinned: 0.8.2 @ sha256:10e4...)"
echo "Input:   $OSM_FILE (Pinned & Hash-Verified)"
echo "Format:  PMTiles"
echo "=================================="

if ! command -v docker > /dev/null 2>&1; then
  echo "Error: 'docker' is required but not installed or not in PATH." >&2
  exit 1
fi

DOWNLOADER=""
if command -v wget > /dev/null 2>&1; then
  DOWNLOADER="wget"
elif command -v curl > /dev/null 2>&1; then
  DOWNLOADER="curl"
else
  echo "Error: Neither 'wget' nor 'curl' is available for downloading the OSM data." >&2
  exit 1
fi

mkdir -p "$BASEMAP_DIR"
cd "$BASEMAP_DIR"

FINAL_PMTILES_PATH="$BASEMAP_DIR/$OUTPUT_PMTILES"
FINAL_META_PATH="$BASEMAP_DIR/$OUTPUT_META"
PARTIAL_PMTILES_PATH="$BASEMAP_DIR/$PARTIAL_PMTILES"
PARTIAL_META_PATH="$BASEMAP_DIR/$PARTIAL_META"
PARTIAL_LAYERSTATS_PATH="${PARTIAL_PMTILES_PATH}.layerstats.tsv.gz"
FINAL_ARTIFACT_CREATED=0
FINAL_META_CREATED=0
PUBLISH_COMPLETE=0

cleanup_build() {
  rm -f -- "$PARTIAL_PMTILES_PATH" "$PARTIAL_META_PATH" "$PARTIAL_LAYERSTATS_PATH"
  if [[ "$PUBLISH_COMPLETE" != "1" ]]; then
    [[ "$FINAL_META_CREATED" == "1" ]] && rm -f -- "$FINAL_META_PATH"
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

for published_path in "$FINAL_PMTILES_PATH" "$FINAL_META_PATH"; do
  if [[ -e "$published_path" || -L "$published_path" ]]; then
    echo "Error: Published version already exists and will not be replaced: $published_path" >&2
    exit 1
  fi
done

if [ ! -f "$OSM_FILE" ]; then
  echo "=> Downloading OSM data for Schleswig-Holstein ($OSM_FILE)..."
  if [ "$DOWNLOADER" = "wget" ]; then
    wget --tries=5 --waitretry=3 --retry-connrefused --timeout=30 -O "$OSM_FILE" "$OSM_URL" || {
      rm -f "$OSM_FILE"
      exit 1
    }
  else
    curl -fL --retry 5 --retry-delay 3 -o "$OSM_FILE" "$OSM_URL" || {
      rm -f "$OSM_FILE"
      exit 1
    }
  fi
else
  echo "=> OSM data '$OSM_FILE' already exists locally, skipping download."
fi

if command -v sha256sum > /dev/null 2>&1; then
  SHA256_CMD=(sha256sum)
elif command -v shasum > /dev/null 2>&1; then
  SHA256_CMD=(shasum -a 256)
else
  echo "Error: 'sha256sum' or 'shasum' is required for artifact verification." >&2
  exit 1
fi

ACTUAL_SHA256="$("${SHA256_CMD[@]}" "$OSM_FILE" | awk '{print $1}')"
if [ "$ACTUAL_SHA256" != "$OSM_SHA256" ]; then
  echo "Error: Checksum mismatch for $OSM_FILE!" >&2
  echo "Expected: $OSM_SHA256" >&2
  echo "Actual:   $ACTUAL_SHA256" >&2
  exit 1
fi
echo "   [✓] Input integrity verified."

# Always bind Planetiler to the same dated, fully hashed coastline archive.
python3 "$SCRIPT_DIR/prepare-water-polygons.py" "$BASEMAP_DIR"

PLANETILER_MAX_ATTEMPTS="${PLANETILER_MAX_ATTEMPTS:-3}"
PLANETILER_RETRY_DELAY_SECONDS="${PLANETILER_RETRY_DELAY_SECONDS:-5}"
if ! [[ "$PLANETILER_MAX_ATTEMPTS" =~ ^[1-5]$ ]]; then
  echo "Error: PLANETILER_MAX_ATTEMPTS must be an integer from 1 to 5." >&2
  exit 1
fi
if ! [[ "$PLANETILER_RETRY_DELAY_SECONDS" =~ ^[0-9]+$ ]]; then
  echo "Error: PLANETILER_RETRY_DELAY_SECONDS must be a non-negative integer." >&2
  exit 1
fi
if ! [[ "$PLANETILER_HTTP_RETRIES" =~ ^[1-9][0-9]*$ ]]; then
  echo "Error: BASEMAP_PLANETILER_HTTP_RETRIES must be a positive integer." >&2
  exit 1
fi

run_planetiler() {
  docker run --rm \
    --platform linux/amd64 \
    --user "$(id -u):$(id -g)" \
    -v "$BASEMAP_DIR":/data \
    "$PLANETILER_IMAGE" \
    --osm-path="/data/$OSM_FILE" \
    --output="/data/$PARTIAL_PMTILES" \
    --download=true \
    --water-polygons-path="/data/data/sources/water-polygons-split-3857.zip" \
    --http-timeout="$PLANETILER_HTTP_TIMEOUT" \
    --http-retries="$PLANETILER_HTTP_RETRIES" \
    --http-retry-wait="$PLANETILER_HTTP_RETRY_WAIT"
}

echo "=> Running Planetiler via Docker to generate $OUTPUT_PMTILES..."
PLANETILER_ATTEMPT=1
while true; do
  # Keep fully downloaded auxiliary sources across attempts, but never reuse a
  # partial archive or temporary feature store from a failed build.
  rm -f -- "$PARTIAL_PMTILES_PATH" "$PARTIAL_LAYERSTATS_PATH"
  rm -rf -- "$BASEMAP_DIR/tmp"

  if run_planetiler; then
    break
  fi

  if [ "$PLANETILER_ATTEMPT" -ge "$PLANETILER_MAX_ATTEMPTS" ]; then
    echo "Error: Planetiler failed after $PLANETILER_ATTEMPT attempt(s)." >&2
    exit 1
  fi

  echo "Warning: Planetiler attempt $PLANETILER_ATTEMPT failed; retrying after ${PLANETILER_RETRY_DELAY_SECONDS}s." >&2
  sleep "$PLANETILER_RETRY_DELAY_SECONDS"
  PLANETILER_ATTEMPT=$((PLANETILER_ATTEMPT + 1))
done

if [[ ! -s "$PARTIAL_PMTILES_PATH" ]]; then
  echo "Error: Complete partial artifact $PARTIAL_PMTILES was not produced." >&2
  exit 1
fi

PMTILES_SIZE="$(wc -c < "$PARTIAL_PMTILES_PATH" | tr -d '[:space:]')"
PMTILES_SHA256="$("${SHA256_CMD[@]}" "$PARTIAL_PMTILES_PATH" | awk '{print $1}')"
if [ -z "$PMTILES_SHA256" ] || [ "$PMTILES_SIZE" -eq 0 ]; then
  echo "Error: Failed to determine valid size or hash for $PARTIAL_PMTILES." >&2
  exit 1
fi

BUILD_TIMESTAMP_VALUE=""
if [ "${NON_REPRODUCIBLE_BUILD_TIMESTAMP:-}" = "1" ]; then
  BUILD_TIMESTAMP_VALUE="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
elif [ -n "${SOURCE_DATE_EPOCH:-}" ]; then
  BUILD_TIMESTAMP_VALUE="$(date -u -d "@${SOURCE_DATE_EPOCH}" +"%Y-%m-%dT%H:%M:%SZ")" || BUILD_TIMESTAMP_VALUE=""
fi

if [ -n "$BUILD_TIMESTAMP_VALUE" ]; then
  BUILD_TIMESTAMP_JSON="  \"build_timestamp\": \"${BUILD_TIMESTAMP_VALUE}\","
else
  BUILD_TIMESTAMP_JSON=""
fi

cat << MANIFEST > "$PARTIAL_META_PATH"
{
  "version": "${BASEMAP_VERSION}",
  "region": "schleswig-holstein",
${BUILD_TIMESTAMP_JSON}
  "toolchain": {
    "generator": "planetiler",
    "image": "${PLANETILER_IMAGE}"
  },
  "input": {
    "url": "${OSM_URL}",
    "sha256": "${OSM_SHA256}",
    "note": "Pinned historical snapshot with verified SHA256 integrity"
  },
  "artifact_name": "${OUTPUT_PMTILES}",
  "sha256": "${PMTILES_SHA256}",
  "size_bytes": ${PMTILES_SIZE},
  "status": "ready"
}
MANIFEST
[[ -s "$PARTIAL_META_PATH" ]] || {
  echo "Error: Failed to generate metadata for $OUTPUT_PMTILES." >&2
  exit 1
}

publish_immutable_pair() {
  local failed=0

  # INT/TERM are ignored only for the tiny non-atomic two-link publication
  # window. This prevents an interrupt from leaving an artifact without its
  # matching metadata. PUBLISH_COMPLETE is set before signal handling resumes.
  trap '' INT TERM

  if ! ln "$PARTIAL_PMTILES_PATH" "$FINAL_PMTILES_PATH"; then
    echo "Error: Could not publish immutable artifact: $FINAL_PMTILES_PATH" >&2
    failed=1
  else
    FINAL_ARTIFACT_CREATED=1
  fi

  if [[ "$failed" == "0" ]]; then
    if ! ln "$PARTIAL_META_PATH" "$FINAL_META_PATH"; then
      echo "Error: Could not publish immutable metadata: $FINAL_META_PATH" >&2
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

publish_immutable_pair || exit 1
cleanup_build
trap - EXIT INT TERM

echo "=> Basemap generation complete!"
echo "Artifact: $FINAL_PMTILES_PATH"
echo "Metadata: $FINAL_META_PATH"
