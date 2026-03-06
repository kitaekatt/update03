#!/usr/bin/env bash
set -uo pipefail

# session-bootstrap.sh — Thin bash wrapper for the Python bootstrap engine.
#
# Resolves paths, guards for python3, then delegates to the engine.
# Engine's stdout becomes the hook response (JSON with systemMessage).
#
# NOTE: We intentionally do NOT use set -e. With -e, any unexpected command
# failure causes silent exit with no JSON output, and Claude Code shows nothing.
# Instead, we handle errors explicitly and ensure JSON is always emitted.

# Safety net: if the script exits without producing output, emit minimal JSON
HOOK_OUTPUT_EMITTED=""

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Derive marketplace name from plugin root path.
# Works for both dev layout (~/Dev/<marketplace>/plugins/bootstrap/)
# and cache layout (~/.claude/plugins/cache/<marketplace>/bootstrap/<version>/).
MARKETPLACE_NAME="$(basename "$(cd "$PLUGIN_ROOT/../.." && pwd)")"
BOOTSTRAP_LABEL="${MARKETPLACE_NAME}:bootstrap"
PLUGIN_DATA="${HOME}/.claude/plugins/data/${MARKETPLACE_NAME}/bootstrap"

# Set trap after BOOTSTRAP_LABEL is defined so variable expands correctly
trap '[ -z "$HOOK_OUTPUT_EMITTED" ] && echo "{\"continue\": true, \"suppressOutput\": false, \"systemMessage\": \"'"${BOOTSTRAP_LABEL}"': shell error\", \"hookSpecificOutput\": {\"hookEventName\": \"SessionStart\"}}"' EXIT

# --- Capture hook input from stdin and record start time ---
HOOK_INPUT=$(cat)
HOOK_START_EPOCH=$(date +%s 2>/dev/null || echo "0")

# --- Logging ---
# Collect entries in memory; write as a block at the end (with header) only if non-empty.
SHELL_LOG_ENTRIES=()

log_entry() {
    local msg="$1"
    local ts
    ts="$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || echo "unknown-time")"
    SHELL_LOG_ENTRIES+=("[$ts] $msg")
}

flush_log() {
    # Write collected entries as a block with a "Shell" header, only if non-empty.
    if [ ${#SHELL_LOG_ENTRIES[@]} -eq 0 ]; then
        return
    fi
    local ts
    ts="$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || echo "unknown-time")"
    mkdir -p "$PLUGIN_DATA"
    {
        echo "--- Shell $ts ---"
        for entry in "${SHELL_LOG_ENTRIES[@]}"; do
            echo "$entry"
        done
    } >> "$PLUGIN_DATA/bootstrap.log"
}

# --- Read log_success_shell from config (pre-Python, so use grep) ---
LOG_SUCCESS_SHELL="false"
CONFIG_FILE="$PLUGIN_DATA/config.json"
if [ -f "$CONFIG_FILE" ]; then
    # Extract value: grep for the key, strip to true/false
    val=$(grep -o '"log_success_shell"[[:space:]]*:[[:space:]]*[a-z]*' "$CONFIG_FILE" 2>/dev/null | grep -o '[a-z]*$' || echo "false")
    if [ "$val" = "true" ]; then
        LOG_SUCCESS_SHELL="true"
    fi
fi

# --- Ensure ~/.local/bin is at front of PATH ---
# Tools installed by bootstrap (uv, python3) land here. Prepend so they're found first.
LOCAL_BIN="${HOME}/.local/bin"
case ":${PATH}:" in
    *":${LOCAL_BIN}:"*) ;;  # already in PATH
    *) export PATH="${LOCAL_BIN}:${PATH}" ;;
esac

# --- Find Python 3 ---
# Validate each candidate by execution, not just PATH presence.
# This handles Windows Store stubs (python3 in PATH but exits 126).
# Include the standalone install path directly — hard links in ~/.local/bin
# can't find stdlib, so we check the original install location.

PYTHON=""
STANDALONE_DIR="${HOME}/.local/share/python-standalone"
CANDIDATES=(python3 python)
# Add standalone install paths (platform-dependent)
if [ -x "${STANDALONE_DIR}/python/python.exe" ]; then
    CANDIDATES+=("${STANDALONE_DIR}/python/python.exe")
elif [ -x "${STANDALONE_DIR}/python/install/bin/python3" ]; then
    CANDIDATES+=("${STANDALONE_DIR}/python/install/bin/python3")
fi

for candidate in "${CANDIDATES[@]}"; do
    if [ -x "$candidate" ] || command -v "$candidate" &>/dev/null; then
        if "$candidate" -c "import sys; sys.exit(0 if sys.version_info[0] >= 3 else 1)" 2>/dev/null; then
            PYTHON="$candidate"
            PYTHON_PATH="$(command -v "$candidate" 2>/dev/null || echo "$candidate")"
            break
        fi
    fi
done

# Log python3 success if found and logging enabled
if [ -n "$PYTHON" ] && [ "$LOG_SUCCESS_SHELL" = "true" ]; then
    log_entry "python3: ok - found at $PYTHON_PATH"
fi

# --- Self-bootstrap Python via python-build-standalone ---
# If no valid Python 3 is found, download a standalone build and install it
# to the plugin data directory with a symlink in ~/.local/bin.

if [ -z "$PYTHON" ]; then
    log_entry "python3: not found in PATH, installing standalone"

    PY_VERSION="3.12.9"
    RELEASE_TAG="20250317"
    INSTALL_DIR="${HOME}/.local/share/python-standalone"

    # Detect platform
    OS="$(uname -s)"
    ARCH="$(uname -m)"

    # Map to python-build-standalone target triple
    if [[ "$OS" == "Darwin" ]]; then
        if [[ "$ARCH" == "arm64" ]]; then
            TRIPLE="aarch64-apple-darwin"
        else
            TRIPLE="x86_64-apple-darwin"
        fi
    elif [[ "$OS" == "Linux" ]]; then
        if [[ "$ARCH" == "aarch64" ]]; then
            TRIPLE="aarch64-unknown-linux-gnu"
        else
            TRIPLE="x86_64-unknown-linux-gnu"
        fi
    elif [[ "$OS" == MINGW* ]] || [[ "$OS" == MSYS* ]]; then
        TRIPLE="x86_64-pc-windows-msvc"
    else
        log_entry "python3: FAILED - unsupported platform for auto-install ($OS)"
        flush_log
        HOOK_OUTPUT_EMITTED=1
        cat <<'EOF'
{"continue": true, "suppressOutput": false, "systemMessage": "${BOOTSTRAP_LABEL} -> python3 not found and platform not supported for auto-install. Install Python 3 manually.", "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "${BOOTSTRAP_LABEL} -> CRITICAL: python3 not found. Unsupported platform for auto-install. Install Python 3.x manually."}}
EOF
        exit 0
    fi

    ARCHIVE="cpython-${PY_VERSION}+${RELEASE_TAG}-${TRIPLE}-install_only_stripped.tar.gz"
    URL="https://github.com/indygreg/python-build-standalone/releases/download/${RELEASE_TAG}/${ARCHIVE}"

    log_entry "python3: downloading $ARCHIVE"

    # Download and extract
    mkdir -p "$INSTALL_DIR"
    if ! curl -LsSf "$URL" | tar xz -C "$INSTALL_DIR" 2>/dev/null; then
        log_entry "python3: FAILED - download error"
        flush_log
        HOOK_OUTPUT_EMITTED=1
        cat <<'EOF'
{"continue": true, "suppressOutput": false, "systemMessage": "${BOOTSTRAP_LABEL} -> python3 not found and auto-install failed (download error). Install Python 3 manually.", "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "${BOOTSTRAP_LABEL} -> CRITICAL: python3 not found. Auto-install download failed. Install Python 3.x manually."}}
EOF
        exit 0
    fi

    # Make standalone Python available for future sessions via ~/.local/bin
    mkdir -p "${HOME}/.local/bin"
    if [[ "$OS" == MINGW* ]] || [[ "$OS" == MSYS* ]]; then
        PYTHON="${INSTALL_DIR}/python/python.exe"
        # Windows: hard link via PowerShell (no elevation needed; same drive assumed)
        WIN_SRC="$(cygpath -w "$PYTHON")"
        WIN_DEST="$(cygpath -w "${HOME}/.local/bin/python3.exe")"
        powershell.exe -Command "New-Item -ItemType HardLink -Path '$WIN_DEST' -Target '$WIN_SRC' -Force" > /dev/null
        log_entry "python3: installed $PYTHON, linked to ~/.local/bin/python3.exe"
    else
        PYTHON="${INSTALL_DIR}/python/install/bin/python3"
        ln -sf "$PYTHON" "${HOME}/.local/bin/python3"
        log_entry "python3: installed $PYTHON, linked to ~/.local/bin/python3"
    fi
fi

# --- Extract hook input fields for logging ---
HOOK_SOURCE=""
HOOK_SESSION_ID=""
HOOK_MODEL=""
if command -v jq &>/dev/null && [ -n "$HOOK_INPUT" ]; then
    HOOK_SOURCE=$(echo "$HOOK_INPUT" | jq -r '.source // empty' 2>/dev/null || true)
    HOOK_SESSION_ID=$(echo "$HOOK_INPUT" | jq -r '.session_id // empty' 2>/dev/null || true)
    HOOK_MODEL=$(echo "$HOOK_INPUT" | jq -r '.model // empty' 2>/dev/null || true)
elif [ -n "$HOOK_INPUT" ]; then
    # Fallback: grep for fields (no jq available)
    HOOK_SOURCE=$(echo "$HOOK_INPUT" | grep -o '"source"[[:space:]]*:[[:space:]]*"[^"]*"' | grep -o '"[^"]*"$' | tr -d '"' || true)
    HOOK_SESSION_ID=$(echo "$HOOK_INPUT" | grep -o '"session_id"[[:space:]]*:[[:space:]]*"[^"]*"' | grep -o '"[^"]*"$' | tr -d '"' || true)
    HOOK_MODEL=$(echo "$HOOK_INPUT" | grep -o '"model"[[:space:]]*:[[:space:]]*"[^"]*"' | grep -o '"[^"]*"$' | tr -d '"' || true)
fi
if [ "$LOG_SUCCESS_SHELL" = "true" ]; then
    log_entry "hook: source=$HOOK_SOURCE session=$HOOK_SESSION_ID model=$HOOK_MODEL"
fi

# --- Flush shell log entries (if any) before handing off to engine ---
flush_log

# --- Invoke Engine ---

HOOK_OUTPUT_EMITTED=1
exec "$PYTHON" "${PLUGIN_ROOT}/engine/bootstrap_engine.py" \
    --plugin-root "$PLUGIN_ROOT" \
    --data-dir "$PLUGIN_DATA" \
    --hook-start-epoch "$HOOK_START_EPOCH"
