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

# --- Parse flags ---
FLAG_VERBOSE=""
FLAG_CONSOLE=""
ENGINE_FLAGS=()
for arg in "$@"; do
    case "$arg" in
        --verbose) FLAG_VERBOSE=1; ENGINE_FLAGS+=(--verbose) ;;
        --console) FLAG_CONSOLE=1; ENGINE_FLAGS+=(--console) ;;
    esac
done

# Derive marketplace name from plugin root path.
# Works for both dev layout (~/Dev/<marketplace>/plugins/bootstrap/)
# and cache layout (~/.claude/plugins/cache/<marketplace>/bootstrap/<version>/).
MARKETPLACE_NAME="$(basename "$(cd "$PLUGIN_ROOT/../.." && pwd)")"
BOOTSTRAP_LABEL="${MARKETPLACE_NAME}:bootstrap"
PLUGIN_DATA="${HOME}/.claude/plugins/data/${MARKETPLACE_NAME}/bootstrap"

# Set trap after BOOTSTRAP_LABEL is defined so variable expands correctly
# In console mode, no JSON safety net needed — plain text output
if [ -z "$FLAG_CONSOLE" ]; then
    trap '[ -z "$HOOK_OUTPUT_EMITTED" ] && echo "{\"continue\": true, \"suppressOutput\": false, \"systemMessage\": \"'"${BOOTSTRAP_LABEL}"': shell error\", \"hookSpecificOutput\": {\"hookEventName\": \"SessionStart\"}}"' EXIT
fi

# --- Capture hook input from stdin and record start time ---
# In console mode, skip stdin read (no hook JSON piped in)
if [ -n "$FLAG_CONSOLE" ]; then
    HOOK_INPUT=""
else
    HOOK_INPUT=$(cat)
fi
HOOK_START_EPOCH=$(date +%s 2>/dev/null || echo "0")

# --- Logging ---
# Collect entries in memory; write as a block at the end (with header) only if non-empty.
SHELL_LOG_ENTRIES=()

log_entry() {
    local msg="$1"
    local ts
    ts="$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || echo "unknown-time")"
    SHELL_LOG_ENTRIES+=("[$ts] $msg")
    # In console mode, also print to stdout immediately
    if [ -n "$FLAG_CONSOLE" ]; then
        echo "[$ts] $msg"
    fi
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
# --verbose and --console override config to show all shell entries
if [ -n "$FLAG_VERBOSE" ] || [ -n "$FLAG_CONSOLE" ]; then
    LOG_SUCCESS_SHELL="true"
fi

# --- Ensure ~/.local/bin is at front of PATH ---
# Tools installed by bootstrap (uv, python3) land here. Prepend so they're found first.
LOCAL_BIN="${HOME}/.local/bin"
case ":${PATH}:" in
    *":${LOCAL_BIN}:"*) ;;  # already in PATH
    *) export PATH="${LOCAL_BIN}:${PATH}" ;;
esac

# --- Ensure Python is installed in ~/.local/bin ---
# We always use our standalone Python in ~/.local/bin. System Python is not used.
# Check if it's in place and works; install standalone if not.

PYTHON=""
OS="$(uname -s)"
STANDALONE_DIR="${HOME}/.local/share/python-standalone"

if [[ "$OS" == MINGW* ]] || [[ "$OS" == MSYS* ]]; then
    WANT_PYTHON="${LOCAL_BIN}/python3.exe"
    STANDALONE_PYTHON="${STANDALONE_DIR}/python/python.exe"
else
    WANT_PYTHON="${LOCAL_BIN}/python3"
    STANDALONE_PYTHON="${STANDALONE_DIR}/python/install/bin/python3"
fi

# Check 1: ~/.local/bin/python3 exists and works
if [ -x "$WANT_PYTHON" ] && "$WANT_PYTHON" -c "import sys; sys.exit(0 if sys.version_info[0] >= 3 else 1)" 2>/dev/null; then
    PYTHON="$WANT_PYTHON"
    if [ "$LOG_SUCCESS_SHELL" = "true" ]; then
        log_entry "python3: ok - found at $WANT_PYTHON"
    fi
# Check 2: standalone installed but link in ~/.local/bin missing — restore it
elif [ -x "$STANDALONE_PYTHON" ] && "$STANDALONE_PYTHON" -c "import sys; sys.exit(0 if sys.version_info[0] >= 3 else 1)" 2>/dev/null; then
    mkdir -p "$LOCAL_BIN"
    if [[ "$OS" == MINGW* ]] || [[ "$OS" == MSYS* ]]; then
        WIN_SRC="$(cygpath -w "$STANDALONE_PYTHON")"
        WIN_DEST="$(cygpath -w "$WANT_PYTHON")"
        powershell.exe -Command "New-Item -ItemType HardLink -Path '$WIN_DEST' -Target '$WIN_SRC' -Force" > /dev/null
        log_entry "python3: restored hard link $WANT_PYTHON -> $STANDALONE_PYTHON"
        PYTHON="$STANDALONE_PYTHON"  # Use direct path; hard link has known DLL issue (see Task #1)
    else
        ln -sf "$STANDALONE_PYTHON" "$WANT_PYTHON"
        log_entry "python3: restored symlink $WANT_PYTHON -> $STANDALONE_PYTHON"
        PYTHON="$WANT_PYTHON"
    fi
fi

# Check 3: nothing works — download and install standalone
if [ -z "$PYTHON" ]; then
    log_entry "python3: not in ~/.local/bin, installing standalone"

    PY_VERSION="3.12.9"
    RELEASE_TAG="20250317"
    ARCH="$(uname -m)"

    if [[ "$OS" == "Darwin" ]]; then
        [[ "$ARCH" == "arm64" ]] && TRIPLE="aarch64-apple-darwin" || TRIPLE="x86_64-apple-darwin"
    elif [[ "$OS" == "Linux" ]]; then
        [[ "$ARCH" == "aarch64" ]] && TRIPLE="aarch64-unknown-linux-gnu" || TRIPLE="x86_64-unknown-linux-gnu"
    elif [[ "$OS" == MINGW* ]] || [[ "$OS" == MSYS* ]]; then
        TRIPLE="x86_64-pc-windows-msvc"
    else
        log_entry "python3: FAILED - unsupported platform for auto-install ($OS)"
        flush_log
        HOOK_OUTPUT_EMITTED=1
        printf '{"continue": true, "suppressOutput": false, "systemMessage": "%s -> python3 not found and platform not supported for auto-install. Install Python 3 manually.", "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "%s -> CRITICAL: python3 not found. Unsupported platform. Install Python 3.x manually."}}\n' "${BOOTSTRAP_LABEL}" "${BOOTSTRAP_LABEL}"
        exit 0
    fi

    ARCHIVE="cpython-${PY_VERSION}+${RELEASE_TAG}-${TRIPLE}-install_only_stripped.tar.gz"
    URL="https://github.com/indygreg/python-build-standalone/releases/download/${RELEASE_TAG}/${ARCHIVE}"

    log_entry "python3: downloading $ARCHIVE"
    mkdir -p "$STANDALONE_DIR"
    if ! curl -LsSf "$URL" | tar xz -C "$STANDALONE_DIR" 2>/dev/null; then
        log_entry "python3: FAILED - download error"
        flush_log
        HOOK_OUTPUT_EMITTED=1
        printf '{"continue": true, "suppressOutput": false, "systemMessage": "%s -> python3 auto-install failed (download error). Install Python 3 manually.", "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "%s -> CRITICAL: python3 not found. Auto-install download failed. Install Python 3.x manually."}}\n' "${BOOTSTRAP_LABEL}" "${BOOTSTRAP_LABEL}"
        exit 0
    fi

    mkdir -p "$LOCAL_BIN"
    if [[ "$OS" == MINGW* ]] || [[ "$OS" == MSYS* ]]; then
        WIN_SRC="$(cygpath -w "$STANDALONE_PYTHON")"
        WIN_DEST="$(cygpath -w "$WANT_PYTHON")"
        powershell.exe -Command "New-Item -ItemType HardLink -Path '$WIN_DEST' -Target '$WIN_SRC' -Force" > /dev/null
        log_entry "python3: installed standalone, linked to $WANT_PYTHON"
        PYTHON="$STANDALONE_PYTHON"  # Use direct path; hard link has known DLL issue (see Task #1)
    else
        ln -sf "$STANDALONE_PYTHON" "$WANT_PYTHON"
        log_entry "python3: installed standalone, linked to $WANT_PYTHON"
        PYTHON="$WANT_PYTHON"
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
# In console mode, skip file writes (entries were already printed to stdout)
if [ -z "$FLAG_CONSOLE" ]; then
    flush_log
fi

# --- Invoke Engine ---

HOOK_OUTPUT_EMITTED=1
exec "$PYTHON" "${PLUGIN_ROOT}/engine/bootstrap_engine.py" \
    --plugin-root "$PLUGIN_ROOT" \
    --data-dir "$PLUGIN_DATA" \
    --hook-start-epoch "$HOOK_START_EPOCH" \
    "${ENGINE_FLAGS[@]}"
