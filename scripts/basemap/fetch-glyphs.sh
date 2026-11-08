#!/usr/bin/env bash
set -euo pipefail

# Scripts for operational bootstrap of the sovereign visual assets (Glyphs).
# Phase 2: Local generation/hosting of PBF fonts.
#
# Determinism status:
# - Pinned release version of OpenMapTiles fonts (v2.0)
# - SHA256 integrity verification of the downloaded archive
# - Extracted assets placed in reproducible local paths

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" > /dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." > /dev/null 2>&1 && pwd)"
GLYPHS_DIR="$REPO_ROOT/map-style/glyphs"
TARGET_FONT_DIR="$GLYPHS_DIR/Noto Sans Regular"

# Pin OpenMapTiles fonts v2.0 (Noto Sans)
ASSET_URL="https://github.com/openmaptiles/fonts/releases/download/v2.0/noto-sans.zip"
ASSET_API_URL="https://api.github.com/repos/openmaptiles/fonts/releases/assets/78489803"
ASSET_SHA256="d117316544b43a5dde7ee761b36e17701e9f85574e181d76a74814240fdbaf34"
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/noto-sans.XXXXXX")"
TMP_ARCHIVE="$TMP_DIR/noto-sans.zip"
trap 'rm -rf "$TMP_DIR"' EXIT

echo "=== Weltgewebe Basemap Builder ==="
echo "Target:  Glyphs (Fonts)"
echo "Input:   OpenMapTiles Fonts v2.0 (Noto Sans)"
echo "Format:  PBF (Protocol Buffers)"
echo "=================================="

# Tool checks
DOWNLOADER=""
if command -v curl > /dev/null 2>&1; then
  DOWNLOADER="curl"
elif command -v wget > /dev/null 2>&1; then
  DOWNLOADER="wget"
else
  echo "Error: Neither 'wget' nor 'curl' is available for downloading the assets." >&2
  exit 1
fi

if ! command -v unzip > /dev/null 2>&1; then
  echo "Error: 'unzip' is required to extract the font archive." >&2
  exit 1
fi

if command -v sha256sum > /dev/null 2>&1; then
  SHA256_CMD=(sha256sum)
elif command -v shasum > /dev/null 2>&1; then
  SHA256_CMD=(shasum -a 256)
else
  echo "Error: 'sha256sum' or 'shasum' is required for artifact verification but not installed." >&2
  exit 1
fi

echo "=> Preparing target directory ($TARGET_FONT_DIR)..."
mkdir -p "$TARGET_FONT_DIR"

echo "=> Checking if glyphs are already present and verified..."
if [ -f "$TARGET_FONT_DIR/.complete" ]; then
  # Parse key-value sentinel
  STORED_HASH="$(grep '^HASH=' "$TARGET_FONT_DIR/.complete" | cut -d= -f2 | tr -d ' \n\r' || true)"
  STORED_COUNT="$(grep '^COUNT=' "$TARGET_FONT_DIR/.complete" | cut -d= -f2 | tr -d ' \n\r' || true)"

  # Validate that STORED_COUNT is strictly numeric to safely use -eq
  case "$STORED_COUNT" in
    '' | *[!0-9]*) STORED_COUNT=-1 ;;
  esac

  if [ "$STORED_HASH" = "$ASSET_SHA256" ] && [ "$STORED_COUNT" -ge 0 ]; then
    # Verify the actual count in the target directory matches the expected count
    ACTUAL_COUNT="$(find "$TARGET_FONT_DIR" -maxdepth 1 -name '*.pbf' | wc -l | tr -d '[:space:]')"
    if [ "$ACTUAL_COUNT" -eq "$STORED_COUNT" ]; then
      echo "   [✓] Verified glyphs ($ACTUAL_COUNT files) already found in target directory. Skipping download."
      exit 0
    else
      echo "   [!] Existing glyphs failed completeness check ($ACTUAL_COUNT of $STORED_COUNT found). Re-fetching..."
    fi
  else
    echo "   [!] Existing glyphs failed hash/format verification (or version changed). Re-fetching..."
  fi
fi

echo "=> Downloading font archive from OpenMapTiles..."
# Prefer exact upstream release-asset API when github.com browser release URLs 504.
# The same pinned SHA-256 is verified below, irrespective of the download path.
if [ "$DOWNLOADER" = "wget" ]; then
  if ! wget --tries=3 --timeout=30 --waitretry=2 -nv --header="Accept: application/octet-stream" -O "$TMP_ARCHIVE" "$ASSET_API_URL"; then
    echo "GitHub API asset unavailable; trying canonical URL." >&2
    wget --tries=3 --timeout=30 --waitretry=2 -nv -O "$TMP_ARCHIVE" "$ASSET_URL" || {
      echo "Error: Pinned Noto Sans archive unavailable on both routes." >&2
      exit 1
    }
  fi
else
  if ! curl -fL -sS --retry 3 --retry-delay 2 --retry-max-time 120 --connect-timeout 10 --max-time 60 -H "Accept: application/octet-stream" -o "$TMP_ARCHIVE" "$ASSET_API_URL"; then
    echo "GitHub API asset unavailable; trying canonical URL." >&2
    curl -fL -sS --retry 3 --retry-delay 2 --retry-max-time 120 --connect-timeout 10 --max-time 60 -o "$TMP_ARCHIVE" "$ASSET_URL" || {
      echo "Error: Pinned Noto Sans archive unavailable on both routes." >&2
      exit 1
    }
  fi
fi

echo "=> Verifying integrity of downloaded archive..."
ACTUAL_SHA256="$("${SHA256_CMD[@]}" "$TMP_ARCHIVE" | awk '{print $1}')"
if [ "$ACTUAL_SHA256" != "$ASSET_SHA256" ]; then
  echo "Error: Checksum mismatch for downloaded archive!" >&2
  echo "Expected: $ASSET_SHA256" >&2
  echo "Actual:   $ACTUAL_SHA256" >&2
  exit 1
fi
echo "   [✓] Integrity verified (SHA256 match)."

echo "=> Extracting 'Noto Sans Regular' glyphs safely..."
EXTRACT_DIR="$TMP_DIR/extracted"
mkdir -p "$EXTRACT_DIR"

unzip -o -q -j "$TMP_ARCHIVE" "Noto Sans Regular/*" -d "$EXTRACT_DIR" || {
  echo "Error: Failed to extract 'Noto Sans Regular' from archive." >&2
  exit 1
}

# Count extracted files
EXTRACTED_COUNT="$(find "$EXTRACT_DIR" -maxdepth 1 -name '*.pbf' | wc -l | tr -d '[:space:]')"
if [ "$EXTRACTED_COUNT" -eq 0 ]; then
  echo "Error: No .pbf files found in the extracted archive." >&2
  exit 1
fi
echo "   [✓] Extracted $EXTRACTED_COUNT .pbf files."

# Safely copy extracted files and write sentinel
mkdir -p "$TARGET_FONT_DIR"
# Remove stale .pbf and sentinel to ensure a clean target state before copying new set
rm -f "$TARGET_FONT_DIR"/*.pbf "$TARGET_FONT_DIR/.complete"

# Copy contents forcefully, then write the sentinel. We use cp instead of mv to avoid cross-device issues.
cp -f "$EXTRACT_DIR"/*.pbf "$TARGET_FONT_DIR/" || {
  echo "Error: Failed to move extracted glyphs to target directory." >&2
  exit 1
}

# Write multi-line sentinel file
cat << EOF_SENTINEL > "$TARGET_FONT_DIR/.complete"
HASH=$ASSET_SHA256
COUNT=$EXTRACTED_COUNT
EOF_SENTINEL

echo "=> Glyph fetching complete!"
echo "Artifacts are now available in: $TARGET_FONT_DIR"
