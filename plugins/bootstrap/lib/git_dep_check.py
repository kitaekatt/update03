"""Git dependency clone validation and remediation."""

import os
import subprocess
from typing import List, NamedTuple, Optional


class GitDepCheckResult(NamedTuple):
    passed: bool
    message: str
    repo_name: str
    target_path: str
    remediation_cmd: Optional[str] = None


def check_git_dep(
    data_dir: str,
    url: str,
    branch: str,
    sparse_paths: Optional[List[str]] = None,
) -> GitDepCheckResult:
    """Check if a git dependency is cloned correctly.

    Args:
        data_dir: Plugin data directory (clones go to <data_dir>/github/<repo_name>/)
        url: Git repository URL
        branch: Expected branch name
        sparse_paths: Optional list of paths for sparse checkout

    Returns:
        GitDepCheckResult with pass/fail and optional remediation command
    """
    repo_name = _extract_repo_name(url)
    target_path = os.path.join(data_dir, "github", repo_name)

    # Build remediation command
    remediation = _build_clone_cmd(url, branch, target_path, sparse_paths)

    # Check directory exists
    if not os.path.isdir(target_path):
        return GitDepCheckResult(
            passed=False,
            message=f"{repo_name} not cloned",
            repo_name=repo_name,
            target_path=target_path,
            remediation_cmd=remediation,
        )

    # Check it's a git repo
    git_dir = os.path.join(target_path, ".git")
    if not os.path.exists(git_dir):
        return GitDepCheckResult(
            passed=False,
            message=f"{repo_name} exists but is not a git repo",
            repo_name=repo_name,
            target_path=target_path,
            remediation_cmd=remediation,
        )

    # Check branch
    try:
        result = subprocess.run(
            ["git", "-C", target_path, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        current_branch = result.stdout.strip()
        if current_branch != branch:
            return GitDepCheckResult(
                passed=False,
                message=f"{repo_name} on branch {current_branch}, expected {branch}",
                repo_name=repo_name,
                target_path=target_path,
                remediation_cmd=f"git -C {target_path} checkout {branch}",
            )
    except (subprocess.SubprocessError, OSError):
        return GitDepCheckResult(
            passed=False,
            message=f"could not check branch for {repo_name}",
            repo_name=repo_name,
            target_path=target_path,
            remediation_cmd=remediation,
        )

    return GitDepCheckResult(
        passed=True,
        message=f"{repo_name} cloned on {branch}",
        repo_name=repo_name,
        target_path=target_path,
    )


def clone_git_dep(url: str, branch: str, target_path: str, sparse_paths=None) -> tuple:
    """Clone a git dependency. Returns (success, message)."""
    cmd = _build_clone_cmd(url, branch, target_path, sparse_paths)
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            return True, f"cloned to {target_path}"
        return False, result.stderr.strip() or "clone failed"
    except (subprocess.SubprocessError, OSError) as e:
        return False, str(e)


def pull_git_dep(target_path: str) -> tuple:
    """Pull latest changes in an existing git dep. Returns (success, message)."""
    try:
        result = subprocess.run(
            ["git", "-C", target_path, "pull"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            return True, "pulled latest"
        return False, result.stderr.strip() or "pull failed"
    except (subprocess.SubprocessError, OSError) as e:
        return False, str(e)


def _extract_repo_name(url: str) -> str:
    """Extract repository name from URL."""
    # Handle URLs like https://github.com/octocat/Hello-World or .git suffix
    name = url.rstrip("/").rsplit("/", 1)[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return name


def _build_clone_cmd(
    url: str,
    branch: str,
    target_path: str,
    sparse_paths: Optional[List[str]] = None,
) -> str:
    """Build the git clone command string."""
    if sparse_paths:
        # Sparse checkout: clone with no-checkout, set sparse paths, checkout
        paths_str = " ".join(sparse_paths)
        return (
            f"git clone --no-checkout --branch {branch} {url} {target_path} && "
            f"cd {target_path} && "
            f"git sparse-checkout set {paths_str} && "
            f"git checkout {branch}"
        )
    return f"git clone --branch {branch} {url} {target_path}"
