"""File-based logging for bootstrap operations."""

import os
from datetime import datetime, timezone
from typing import List


LOG_FILENAME = "bootstrap.log"
MAX_LOG_LINES = 500


def write_log_block(data_dir: str, header_label: str, entries: List[str]) -> None:
    """Write a header + timestamped log entries as an atomic block.

    Only call this when entries is non-empty. Writes a section header
    followed by the timestamped entries, then trims the log if needed.

    Args:
        data_dir: Directory containing the log file
        header_label: Label for the section header (e.g. "Shell", "Engine")
        entries: List of log messages to append (must be non-empty)
    """
    if not entries:
        return

    os.makedirs(data_dir, exist_ok=True)
    log_file = os.path.join(data_dir, LOG_FILENAME)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    lines = [f"--- {header_label} {timestamp} ---\n"]
    for entry in entries:
        lines.append(f"{entry}\n")

    with open(log_file, "a") as f:
        f.writelines(lines)

    _trim_log(log_file)


def _trim_log(log_file: str) -> None:
    """Keep only the last MAX_LOG_LINES lines."""
    try:
        with open(log_file, "r") as f:
            all_lines = f.readlines()
        if len(all_lines) > MAX_LOG_LINES:
            with open(log_file, "w") as f:
                f.writelines(all_lines[-MAX_LOG_LINES:])
    except (FileNotFoundError, PermissionError):
        pass
