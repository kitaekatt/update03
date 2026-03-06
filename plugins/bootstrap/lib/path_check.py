"""PATH entry verification and persistent remediation."""

import os
import sys
from typing import NamedTuple, Tuple


class CheckResult(NamedTuple):
    path: str
    passed: bool
    message: str


def check_path_entry(path_entry: str) -> CheckResult:
    """Check if a directory is present in PATH.

    Args:
        path_entry: Directory path to check (supports ~ expansion)

    Returns:
        CheckResult with pass/fail
    """
    expanded = os.path.expanduser(path_entry)
    path_dirs = os.environ.get("PATH", "").split(os.pathsep)

    # Normalize for comparison
    expanded_norm = os.path.normpath(expanded)
    for d in path_dirs:
        if os.path.normpath(d) == expanded_norm:
            return CheckResult(
                path=path_entry,
                passed=True,
                message=f"{path_entry} is in PATH",
            )

    return CheckResult(
        path=path_entry,
        passed=False,
        message=f"{path_entry} ({expanded}) is not in PATH",
    )


def add_path_to_shell_config(path_entry: str) -> Tuple[bool, str]:
    """Persistently add a path entry to shell RC files.

    Appends `export PATH="<path>:$PATH"` to the appropriate RC file(s).
    Idempotent: skips files where the path is already declared.

    Returns:
        (success, message) tuple
    """
    expanded = os.path.expanduser(path_entry)

    # Build portable export line using $HOME where possible
    home = os.path.expanduser("~")
    if expanded.startswith(home):
        path_expr = '"$HOME' + expanded[len(home):] + ':$PATH"'
    else:
        path_expr = f'"{expanded}:$PATH"'
    export_line = f'export PATH={path_expr}'

    # Determine RC files by platform
    if sys.platform == "darwin":
        rc_files = [os.path.expanduser("~/.zshrc"), os.path.expanduser("~/.bashrc")]
    else:
        # Linux and Windows (Git Bash)
        rc_files = [os.path.expanduser("~/.bashrc")]

    written = []
    for rc_file in rc_files:
        try:
            if os.path.exists(rc_file):
                content = open(rc_file).read()
                # Skip if already declared (check both expanded and unexpanded forms)
                if expanded in content or path_entry in content:
                    continue
            with open(rc_file, "a") as f:
                f.write(f'\n# Added by bootstrap\n{export_line}\n')
            written.append(os.path.basename(rc_file))
        except OSError:
            pass

    if written:
        return True, f"added to {', '.join(written)} (reload shell to take effect)"
    return True, "already declared in shell config"
