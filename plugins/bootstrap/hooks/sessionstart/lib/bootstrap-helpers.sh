#!/usr/bin/env bash
# bootstrap-helpers.sh — Shared functions for session bootstrap steps
#
# Source this file before sourcing step scripts to avoid helper duplication.
# Each step script guards its own copies with `declare -f` checks.

# --- JSON Output Helpers ---

json_escape() {
    local s="$1"
    s="${s//\\/\\\\}"
    s="${s//\"/\\\"}"
    s="${s//$'\n'/\\n}"
    s="${s//$'\t'/\\t}"
    printf '%s' "$s"
}

# --- OS Detection ---

detect_os() {
    local ostype="${OSTYPE:-}"
    if [ -n "$ostype" ]; then
        case "$ostype" in
            darwin*)    printf 'macos'   ; return ;;
            linux-gnu*) printf 'ubuntu'  ; return ;;
            msys*|cygwin*) printf 'windows'; return ;;
        esac
    fi

    local uname_s
    uname_s="$(uname -s 2>/dev/null || true)"
    case "$uname_s" in
        Darwin)  printf 'macos'   ; return ;;
        Linux)   printf 'ubuntu'  ; return ;;
        MINGW*|MSYS*|CYGWIN*) printf 'windows'; return ;;
    esac

    return 1
}
