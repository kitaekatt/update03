#!/usr/bin/env bash
# ensure-known-marketplaces.sh — Ensure plugins-kit entry in known_marketplaces.json
#
# Merges the reference known_marketplaces.json into ~/.claude/plugins/known_marketplaces.json.
# - Adds plugins-kit entry if missing
# - Updates source and autoUpdate fields to match reference
# - Preserves all other marketplace entries
# - Never adds, removes, or modifies lastUpdated or installLocation fields
#
# Output: JSON to stdout
# Exit:   0 = success, 1 = error

# --- JSON Output Helpers ---
if ! declare -f json_escape >/dev/null 2>&1; then
json_escape() {
    local s="$1"
    s="${s//\\/\\\\}"
    s="${s//\"/\\\"}"
    s="${s//$'\n'/\\n}"
    s="${s//$'\t'/\\t}"
    printf '%s' "$s"
}
fi

_emit_km_success() {
    local action="$1"
    cat <<EOF
{"status": "ok", "step": "known_marketplaces", "action": "$(json_escape "$action")"}
EOF
}

_emit_km_error() {
    local message="$1"
    cat <<EOF
{"status": "error", "step": "known_marketplaces", "message": "$(json_escape "$message")"}
EOF
}

ensure_known_marketplaces() {
    local plugin_root="$1"
    local reference_file="${plugin_root}/known_marketplaces.json"
    local target_file="${HOME}/.claude/plugins/known_marketplaces.json"

    if [ ! -f "$reference_file" ]; then
        _emit_km_error "Reference file not found: $reference_file"
        return 1
    fi

    # Ensure target directory exists
    mkdir -p "$(dirname "$target_file")"

    # Use Python for reliable JSON merge
    local result
    if ! result=$(python3 -c "
import json, sys

reference_path = sys.argv[1]
target_path = sys.argv[2]

# Load reference (fields we want to enforce)
with open(reference_path) as f:
    reference = json.load(f)

# Load existing target or start empty
try:
    with open(target_path) as f:
        target = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    target = {}

changed = False

for marketplace_name, ref_entry in reference.items():
    if marketplace_name not in target:
        # New entry — add it (without lastUpdated/installLocation)
        target[marketplace_name] = dict(ref_entry)
        changed = True
    else:
        # Existing entry — update only reference fields, preserve others
        existing = target[marketplace_name]
        for key, value in ref_entry.items():
            if key in ('lastUpdated', 'installLocation'):
                continue  # never touch these
            if existing.get(key) != value:
                existing[key] = value
                changed = True

if changed:
    with open(target_path, 'w') as f:
        json.dump(target, f, indent=2)
        f.write('\n')
    print('updated')
else:
    print('unchanged')
" "$reference_file" "$target_file" 2>&1); then
        _emit_km_error "Python merge failed: $result"
        return 1
    fi

    _emit_km_success "$result"
    return 0
}

# --- Main ---
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    if [ $# -lt 1 ]; then
        _emit_km_error "Usage: ensure-known-marketplaces.sh <plugin-root>"
        exit 1
    fi
    ensure_known_marketplaces "$1"
fi
