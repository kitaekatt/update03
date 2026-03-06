"""Python venv validation and remediation."""

import os
import subprocess
from typing import List, NamedTuple, Optional


class VenvCheckResult(NamedTuple):
    passed: bool
    message: str
    venv_path: str
    remediation_cmd: Optional[str] = None


def check_venv(plugin_data_dir: str, plugin_root: str, check_imports: List[str]) -> VenvCheckResult:
    """Check if a Python venv exists and required imports are available.

    Args:
        plugin_data_dir: Plugin data directory (venv lives at <data_dir>/.venv)
        plugin_root: Plugin root directory (for uv sync --project)
        check_imports: List of module names to try importing

    Returns:
        VenvCheckResult with pass/fail and optional remediation command
    """
    venv_path = os.path.join(plugin_data_dir, ".venv")
    remediation = f"uv sync --project {plugin_root}"

    # Check venv directory exists
    if not os.path.isdir(venv_path):
        return VenvCheckResult(
            passed=False,
            message=f"venv not found at {venv_path}",
            venv_path=venv_path,
            remediation_cmd=remediation,
        )

    # Find python binary
    python_bin = _find_python(venv_path)
    if not python_bin:
        return VenvCheckResult(
            passed=False,
            message=f"no python binary in {venv_path}",
            venv_path=venv_path,
            remediation_cmd=remediation,
        )

    # Check python works
    try:
        subprocess.run(
            [python_bin, "-c", "import sys; sys.exit(0)"],
            capture_output=True, timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return VenvCheckResult(
            passed=False,
            message=f"python binary not functional at {python_bin}",
            venv_path=venv_path,
            remediation_cmd=remediation,
        )

    # Check each import
    for module in check_imports:
        try:
            result = subprocess.run(
                [python_bin, "-c", f"import {module}"],
                capture_output=True, timeout=10,
            )
            if result.returncode != 0:
                return VenvCheckResult(
                    passed=False,
                    message=f"import {module} failed in venv",
                    venv_path=venv_path,
                    remediation_cmd=remediation,
                )
        except (subprocess.SubprocessError, OSError):
            return VenvCheckResult(
                passed=False,
                message=f"failed to check import {module}",
                venv_path=venv_path,
                remediation_cmd=remediation,
            )

    return VenvCheckResult(
        passed=True,
        message=f"venv ok ({len(check_imports)} imports verified)",
        venv_path=venv_path,
    )


def _find_python(venv_path: str) -> Optional[str]:
    """Find the python binary in a venv."""
    candidates = [
        os.path.join(venv_path, "bin", "python"),
        os.path.join(venv_path, "Scripts", "python.exe"),
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return None
