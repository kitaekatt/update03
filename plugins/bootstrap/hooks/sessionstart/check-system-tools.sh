#!/usr/bin/env bash
# check-system-tools.sh — Step 1 of session bootstrap (no-op stub)
#
# No system tools required yet. Always succeeds.
#
# Output: JSON to stdout
# Exit:   0 = success

check_system_tools() {
    cat <<EOF
{"status": "ok", "step": "system_tools", "os": "$(detect_os 2>/dev/null || echo unknown)", "tools_checked": []}
EOF
    return 0
}

# --- Main ---
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    # Source helpers if running standalone
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    source "$SCRIPT_DIR/lib/bootstrap-helpers.sh"
    check_system_tools
fi
