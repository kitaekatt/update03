#!/usr/bin/env bash
# create-venv.sh — Step 2 of session bootstrap (no-op stub)
#
# No Python venv required yet. Always succeeds.
#
# Output: JSON to stdout
# Exit:   0 = success

create_venv() {
    cat <<EOF
{"status": "ok", "step": "venv", "venv_path": "", "python_executable": ""}
EOF
    return 0
}

# --- Main ---
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    create_venv
fi
