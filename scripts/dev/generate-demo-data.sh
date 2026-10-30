#!/usr/bin/env bash
set -euo pipefail

# Erzeugt Demo-Daten falls nicht vorhanden.
# Argument 1: Ziel-Verzeichnis (default: .gewebe/in)
DIR="${1:-.gewebe/in}"

mkdir -p "$DIR"

# --- Helpers ---

# Temp file handling
TMP_FILE=""
cleanup() {
  if [ -n "$TMP_FILE" ] && [ -f "$TMP_FILE" ]; then
    rm -f "$TMP_FILE"
  fi
}
trap cleanup EXIT

# remove_line_by_id <file> <id>
# Removes all lines containing the exact ID (anchored at start) from the JSONL file.
remove_line_by_id() {
  local file="$1"
  local id="$2"
  # Use regex anchor to match start of line for robustness: ^{"id":"UUID"
  local match_pattern="^{\"id\"[[:space:]]*:[[:space:]]*\"$id\""

  if [ -s "$file" ] && grep -q "$match_pattern" "$file"; then
    echo "→ removing legacy/stale ID $id from $(basename "$file")"
    TMP_FILE=$(mktemp)
    # grep -v returns 1 if all lines removed or input empty, hence || true
    grep -v "$match_pattern" "$file" > "$TMP_FILE" || true
    mv "$TMP_FILE" "$file"
    chmod 644 "$file"
    TMP_FILE="" # Reset for trap
  fi
}

# ensure_jsonl_line <file> <id> <canonical_json> [check_field] [expected_value]
# Ensures that exactly one valid entry for the given ID exists.
# If multiple exist, or if the existing one lacks 'check_field', it replaces them.
ensure_jsonl_line() {
  local file="$1"
  local id="$2"
  local canonical_json="$3"
  local check_field="${4:-}"
  local expected_value="${5:-}"

  # Use regex anchor to match start of line for robustness
  local match_pattern="^{\"id\"[[:space:]]*:[[:space:]]*\"$id\""
  local needs_migration=0

  if [ -s "$file" ]; then
    # Count occurrences of the ID (anchored).
    # NOTE: Do NOT use -F because match_pattern contains regex anchor `^`.
    local count
    count=$(grep -c "$match_pattern" "$file" || true)

    if [ "$count" -eq 0 ]; then
      # Case: ID missing completely
      echo "→ updating: adding $id to $(basename "$file")"
      echo "$canonical_json" >> "$file"
      return 0
    elif [ "$count" -gt 1 ]; then
      # Case: Duplicates found
      echo "→ migrating: deduplicating $id in $(basename "$file")"
      needs_migration=1
    else
      # Case: Exactly one match. Check semantics/staleness.
      if [ -n "$check_field" ]; then
        local existing_line
        # Do NOT use -F here either.
        existing_line=$(grep "$match_pattern" "$file")

        if [ -n "$expected_value" ]; then
          # On upgrades, replace only known demo IDs whose kind is still legacy.
          if command -v jq > /dev/null 2>&1; then
            if ! echo "$existing_line" | jq -e --arg field "$check_field" --arg expected "$expected_value" '.[$field] == $expected' > /dev/null 2>&1; then
              needs_migration=1
            fi
          elif ! echo "$existing_line" | grep -Eq "\"$check_field\"[[:space:]]*:[[:space:]]*\"$expected_value\""; then
            needs_migration=1
          fi
          if [ "$needs_migration" -eq 1 ]; then
            echo "→ migrating: fixing stale $check_field for $id in $(basename "$file")"
          fi
        elif command -v jq > /dev/null 2>&1; then
          if echo "$existing_line" | jq -e "has(\"$check_field\") | not" > /dev/null 2>&1; then
            echo "→ migrating: fixing stale entry (missing $check_field) $id in $(basename "$file")"
            needs_migration=1
          fi
        elif ! echo "$existing_line" | grep -q "\"$check_field\"[[:space:]]*:"; then
          echo "→ migrating: fixing stale entry (missing $check_field) $id in $(basename "$file")"
          needs_migration=1
        fi
      fi
    fi
  else
    # Case: File does not exist or is empty
    echo "→ seeds: $(basename "$file") (new)"
    echo "$canonical_json" > "$file"
    return 0
  fi

  # Apply migration if needed
  if [ "$needs_migration" -eq 1 ]; then
    TMP_FILE=$(mktemp)
    # Filter out the specific ID (remove all occurrences). No -F.
    grep -v "$match_pattern" "$file" > "$TMP_FILE" || true
    # Append the correct canonical line
    echo "$canonical_json" >> "$TMP_FILE"
    mv "$TMP_FILE" "$file"
    chmod 644 "$file"
    TMP_FILE=""
  fi
}

# --- ACCOUNTS ---
ACCOUNT_ID="7d97a42e-3704-4a33-a61f-0e0a6b4d65d8"
ACCOUNT_JSON='{"id":"7d97a42e-3704-4a33-a61f-0e0a6b4d65d8","type":"garnrolle","title":"gewebespinnerAYE","summary":"Persönlicher Account (Garnrolle), am Wohnsitz verortet. Ursprung von Fäden ins Gewebe.","location":{"lat":53.5604148,"lon":10.0629844},"map_state":"exact","radius_m":0,"tags":["account","garnrolle","wohnort"]}'

# Just ensure the file exists so ensure_jsonl_line doesn't complain about missing file if Logic A branches
touch "$DIR/demo.accounts.jsonl"
ensure_jsonl_line "$DIR/demo.accounts.jsonl" "$ACCOUNT_ID" "$ACCOUNT_JSON" "map_state"

# --- NODES ---
NODE_ID="b52be17c-4ab7-4434-98ce-520f86290cf0"
NODE_LINE='{"id":"b52be17c-4ab7-4434-98ce-520f86290cf0","kind":"resource","title":"fairschenkbox","summary":"Öffentliche Fair-Schenk-Box","info":"Dies ist eine **Demo-Info** für den Test.","created_at":"2025-12-22T00:00:00Z","updated_at":"2025-12-22T00:00:00Z","location":{"lat":53.558894813662505,"lon":10.060228407382967}}'

# Seed known demo records with canonical identities. Upgrade only these reserved
# demo IDs on existing installations; never rewrite unrelated user nodes.
ensure_jsonl_line "$DIR/demo.nodes.jsonl" "00000000-0000-0000-0000-000000000001" '{"id":"00000000-0000-0000-0000-000000000001","kind":"place","title":"Marktplatz Hamburg","created_at":"2025-01-01T12:00:00Z","updated_at":"2025-11-01T09:00:00Z","location":{"lon":9.9937,"lat":53.5511}}' "kind" "place"
ensure_jsonl_line "$DIR/demo.nodes.jsonl" "00000000-0000-0000-0000-000000000002" '{"id":"00000000-0000-0000-0000-000000000002","kind":"place","title":"Nachbarschaftshaus","created_at":"2025-01-01T12:00:00Z","updated_at":"2025-11-02T12:15:00Z","location":{"lon":10.0002,"lat":53.5523}}' "kind" "place"
ensure_jsonl_line "$DIR/demo.nodes.jsonl" "00000000-0000-0000-0000-000000000003" '{"id":"00000000-0000-0000-0000-000000000003","kind":"resource","title":"Tauschbox Altona","created_at":"2025-01-01T12:00:00Z","updated_at":"2025-10-30T18:45:00Z","location":{"lon":9.9813,"lat":53.5456}}' "kind" "resource"
ensure_jsonl_line "$DIR/demo.nodes.jsonl" "00000000-0000-0000-0000-000000000004" '{"id":"00000000-0000-0000-0000-000000000004","kind":"place","title":"Gemeinschaftsgarten","created_at":"2025-01-01T12:00:00Z","updated_at":"2025-11-05T10:00:00Z","location":{"lon":10.0184,"lat":53.5631}}' "kind" "place"
ensure_jsonl_line "$DIR/demo.nodes.jsonl" "00000000-0000-0000-0000-000000000005" '{"id":"00000000-0000-0000-0000-000000000005","kind":"project","title":"Reparaturcafé","created_at":"2025-01-01T12:00:00Z","updated_at":"2025-11-03T16:20:00Z","location":{"lon":9.9708,"lat":53.5615}}' "kind" "project"

# Clean up one superseded legacy demo ID.
remove_line_by_id "$DIR/demo.nodes.jsonl" "00000000-0000-0000-0000-000000000006"

# Migrate the known fairschenkbox ID from legacy "Knoten" to resource.
ensure_jsonl_line "$DIR/demo.nodes.jsonl" "$NODE_ID" "$NODE_LINE" "kind" "resource"

# --- EDGES ---
EDGE_ID="00000000-0000-0000-0000-00000000E001"
EDGE_LINE='{"id":"00000000-0000-0000-0000-00000000E001","source_type":"account","source_id":"7d97a42e-3704-4a33-a61f-0e0a6b4d65d8","target_type":"node","target_id":"b52be17c-4ab7-4434-98ce-520f86290cf0","edge_kind":"reference","note":"faden","created_at":"2025-12-22T00:00:00Z"}'

# Pre-seed bulk data if missing
if [ ! -s "$DIR/demo.edges.jsonl" ]; then
  echo "→ seeds: edges"
  cat > "$DIR/demo.edges.jsonl" << EOF
{"id":"00000000-0000-0000-0000-000000000101","source_type":"node","source_id":"00000000-0000-0000-0000-000000000001","target_type":"node","target_id":"00000000-0000-0000-0000-000000000002","edge_kind":"reference","note":"Kooperation Marktplatz ↔ Nachbarschaftshaus","created_at":"2025-01-01T12:00:00Z"}
{"id":"00000000-0000-0000-0000-000000000102","source_type":"node","source_id":"00000000-0000-0000-0000-000000000002","target_type":"node","target_id":"00000000-0000-0000-0000-000000000004","edge_kind":"reference","note":"Gemeinschaftsaktion Gartenpflege","created_at":"2025-01-01T12:00:00Z"}
{"id":"00000000-0000-0000-0000-000000000103","source_type":"node","source_id":"00000000-0000-0000-0000-000000000001","target_type":"node","target_id":"00000000-0000-0000-0000-000000000003","edge_kind":"reference","note":"Tauschbox liefert Material","created_at":"2025-01-01T12:00:00Z"}
{"id":"00000000-0000-0000-0000-000000000104","source_type":"node","source_id":"00000000-0000-0000-0000-000000000005","target_type":"node","target_id":"00000000-0000-0000-0000-000000000001","edge_kind":"reference","note":"Reparaturcafé hilft Marktplatz","created_at":"2025-01-01T12:00:00Z"}
EOF
fi

# Ensure canonical edge (Note: we use 'target_id' as semantic check field to ensure it points to the right place)
ensure_jsonl_line "$DIR/demo.edges.jsonl" "$EDGE_ID" "$EDGE_LINE" "target_id"
