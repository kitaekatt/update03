#!/usr/bin/env bash
set -euo pipefail

# session-bootstrap.sh — SessionStart hook for bootstrap plugin
#
# Runs bootstrap at most once every 24 hours. Checks a timestamp file
# in plugin data dir; skips if last run was < 24h ago.
#
#   0. Check last-run timestamp (skip if recent)
#   1. Verify system tools (currently no-op)
#   2. Create/update Python venv (currently no-op)
#   3. Ensure marketplace registrations with autoUpdate
#   4. Force plugin cache refresh
#   5. Write timestamp
#
# Output: Single JSON object to stdout (lands in additionalContext)
# Exit:   0 = bootstrap complete (or skipped), 1 = error

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
PLUGIN_DATA="${HOME}/.claude/plugins/data/bootstrap"
TIMESTAMP_FILE="${PLUGIN_DATA}/last_bootstrap"
THROTTLE_SECONDS=57600  # 16 hours

# --- Source shared helpers and step functions ---

source "$SCRIPT_DIR/lib/bootstrap-helpers.sh"
source "$SCRIPT_DIR/check-system-tools.sh"
source "$SCRIPT_DIR/create-venv.sh"
source "$SCRIPT_DIR/ensure-known-marketplaces.sh"
source "$SCRIPT_DIR/update-plugins.sh"

# --- Hook Response Wrapper ---

emit_hook_response() {
    local context_message="$1"
    local user_message="${2:-$1}"
    local escaped_context escaped_user
    escaped_context="$(json_escape "$context_message")"
    escaped_user="$(json_escape "$user_message")"
    cat <<EOF
{"continue": true, "suppressOutput": false, "systemMessage": "$escaped_user", "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "$escaped_context"}}
EOF
}

emit_hook_silent() {
    cat <<EOF
{"continue": true, "suppressOutput": true}
EOF
}

# --- Throttle Helpers ---

is_throttled() {
    [ -f "$TIMESTAMP_FILE" ] || return 1
    local last_run now elapsed
    last_run=$(cat "$TIMESTAMP_FILE" 2>/dev/null) || return 1
    now=$(date +%s)
    elapsed=$((now - last_run))
    [ "$elapsed" -lt "$THROTTLE_SECONDS" ]
}

write_timestamp() {
    mkdir -p "$PLUGIN_DATA"
    date +%s > "$TIMESTAMP_FILE"
}

# --- JSON Field Extractors ---

_extract_json_field() {
    local json="$1" field="$2"
    printf '%s' "$json" | sed -n 's/.*"'"$field"'":[[:space:]]*"\([^"]*\)".*/\1/p'
}

# --- Output Helpers ---

format_bootstrap_error_context() {
    local step_json="$1"
    local context_msg
    context_msg="$(_extract_json_field "$step_json" "context_message")"
    if [ -n "$context_msg" ]; then
        local decoded
        decoded="$(printf '%b' "$context_msg")"
        printf '%s' "bootstrap -> Bootstrap failed:
${decoded}"
    else
        local msg
        msg="$(_extract_json_field "$step_json" "message")"
        printf '%s' "bootstrap -> ERROR: $msg"
    fi
}

format_bootstrap_error_user() {
    local step_json="$1"
    local user_msg
    user_msg="$(_extract_json_field "$step_json" "user_message")"
    if [ -n "$user_msg" ]; then
        local decoded
        decoded="$(printf '%b' "$user_msg")"
        printf '%s' "bootstrap -> Setup issues found:
${decoded}"
    else
        local msg
        msg="$(_extract_json_field "$step_json" "message")"
        printf '%s' "bootstrap -> ERROR: $msg"
    fi
}

# --- Main Bootstrap Flow ---

main() {
    # Step 0: Check throttle — skip if last run was < 16h ago
    if is_throttled; then
        exit 0
    fi

    # Step 1: Check system tools (no-op — always succeeds)
    local step1_json
    if ! step1_json=$(check_system_tools); then
        emit_hook_response "$(format_bootstrap_error_context "$step1_json")" "$(format_bootstrap_error_user "$step1_json")"
        exit 0
    fi

    # Step 2: Create/update venv (no-op — always succeeds)
    local step2_json
    if ! step2_json=$(create_venv); then
        emit_hook_response "$(format_bootstrap_error_context "$step2_json")"
        exit 1
    fi

    # Step 3: Ensure known_marketplaces.json has marketplace entries
    local step3_json
    if ! step3_json=$(ensure_known_marketplaces "$PLUGIN_ROOT"); then
        emit_hook_response "$(format_bootstrap_error_context "$step3_json")"
        exit 1
    fi

    # Step 4: Force plugin cache refresh
    local step4_json
    if ! step4_json=$(update_plugins); then
        emit_hook_response "$(format_bootstrap_error_context "$step4_json")"
        exit 1
    fi

    # Step 5: Write timestamp
    write_timestamp

    # All steps passed — build summary from step results
    local details=()
    local km_action
    km_action="$(_extract_json_field "$step3_json" "action")"
    [ "$km_action" = "updated" ] && details+=("synced marketplaces")

    local up_details
    up_details="$(_extract_json_field "$step4_json" "details")"
    [ -n "$up_details" ] && details+=("plugins: ${up_details}")

    local summary
    if [ ${#details[@]} -gt 0 ]; then
        summary="$(IFS='; '; printf '%s' "${details[*]}")"
    else
        summary="checked marketplaces and plugins"
    fi

    emit_hook_response "bootstrap -> ok (${summary})" "bootstrap -> ${summary}"
}

main
