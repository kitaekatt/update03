"""JSON entry merging for bootstrap manifests.

Ensures a target JSON file contains expected entries from a reference file,
merging specified fields while preserving others.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional


class JsonCheckResult(NamedTuple):
    passed: bool
    target: str
    message: str


def check_json_entries(
    reference_path: str,
    target_path: str,
    merge_fields: List[str],
    preserve_fields: Optional[List[str]] = None,
) -> JsonCheckResult:
    """Check if target JSON has all entries from reference with matching merge fields.

    Args:
        reference_path: Path to reference JSON file (source of truth)
        target_path: Path to target JSON file to check
        merge_fields: Fields to compare for equality
        preserve_fields: Fields to keep from target (not overwritten)

    Returns:
        JsonCheckResult with pass/fail
    """
    ref_data = _load_json(reference_path)
    if ref_data is None:
        return JsonCheckResult(
            passed=False, target=target_path,
            message=f"reference file not found: {reference_path}",
        )

    target_data = _load_json(target_path)
    if target_data is None:
        return JsonCheckResult(
            passed=False, target=target_path,
            message="target file does not exist",
        )

    # Compare merge fields per entry (top-level keys are entry names,
    # merge_fields refer to sub-fields within each entry)
    for key, ref_entry in ref_data.items():
        if not isinstance(ref_entry, dict):
            continue
        target_entry = target_data.get(key)
        if not isinstance(target_entry, dict):
            return JsonCheckResult(
                passed=False, target=target_path,
                message=f"entry '{key}' missing from target",
            )
        for field in merge_fields:
            if field in ref_entry and ref_entry[field] != target_entry.get(field):
                return JsonCheckResult(
                    passed=False, target=target_path,
                    message=f"entry '{key}' field '{field}' differs",
                )

    return JsonCheckResult(
        passed=True, target=target_path,
        message="all merge fields match",
    )


def merge_json_entries(
    reference_path: str,
    target_path: str,
    merge_fields: List[str],
    preserve_fields: Optional[List[str]] = None,
) -> JsonCheckResult:
    """Merge entries from reference into target JSON.

    Copies merge_fields from reference to target. If target exists,
    preserves preserve_fields from the existing target.

    Args:
        reference_path: Path to reference JSON file
        target_path: Path to target JSON file
        merge_fields: Fields to copy from reference
        preserve_fields: Fields to keep from existing target

    Returns:
        JsonCheckResult with pass/fail
    """
    ref_data = _load_json(reference_path)
    if ref_data is None:
        return JsonCheckResult(
            passed=False, target=target_path,
            message=f"reference file not found: {reference_path}",
        )

    target_data = _load_json(target_path) or {}
    preserve_fields = preserve_fields or []

    # Merge per entry (top-level keys are entry names,
    # merge_fields/preserve_fields refer to sub-fields within each entry)
    for key, ref_entry in ref_data.items():
        if not isinstance(ref_entry, dict):
            continue
        target_entry = target_data.setdefault(key, {})

        # Copy merge_fields from reference entry
        for field in merge_fields:
            if field in ref_entry:
                target_entry[field] = ref_entry[field]

        # Preserve existing sub-fields that shouldn't be overwritten
        # (already in target_entry — just don't touch them)

    # Write target
    target = Path(target_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w") as f:
        json.dump(target_data, f, indent=2)
        f.write("\n")

    return JsonCheckResult(
        passed=True, target=target_path,
        message="merged successfully",
    )


def _load_json(path: str) -> Optional[Dict[str, Any]]:
    """Load a JSON file. Returns None if not found or invalid."""
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
