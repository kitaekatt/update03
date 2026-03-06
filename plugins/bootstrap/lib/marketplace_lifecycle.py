"""Marketplace and plugin lifecycle operations using Claude Code CLI.

Wraps `claude plugin marketplace` and `claude plugin` commands for
marketplace and plugin management (add, remove, update, install, etc.).
"""

import json
import os
import shutil
import subprocess
from typing import NamedTuple, Optional


class LifecycleResult(NamedTuple):
    passed: bool
    ref: str
    message: str


def _find_claude_cli() -> Optional[str]:
    """Find the claude CLI binary."""
    return shutil.which("claude")


def _run_claude(args: list, timeout: int = 120) -> tuple:
    """Run a claude CLI command. Returns (success, stdout, stderr)."""
    claude = _find_claude_cli()
    if not claude:
        return False, "", "claude CLI not found"
    try:
        result = subprocess.run(
            [claude] + args,
            capture_output=True, text=True, timeout=timeout,
        )
        return result.returncode == 0, result.stdout, result.stderr
    except (subprocess.SubprocessError, OSError) as e:
        return False, "", str(e)


# --- Marketplace operations ---

def check_marketplace_exists(name: str) -> LifecycleResult:
    """Check if a marketplace is registered in known_marketplaces.json."""
    km_path = os.path.expanduser("~/.claude/plugins/known_marketplaces.json")
    try:
        with open(km_path, "r") as f:
            data = json.load(f)
        if name in data:
            return LifecycleResult(passed=True, ref=name, message="marketplace exists")
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return LifecycleResult(passed=False, ref=name, message="marketplace not found")


def add_marketplace(source_url: str, name: str = "") -> LifecycleResult:
    """Add a marketplace via `claude plugin marketplace add`."""
    ok, stdout, stderr = _run_claude(["plugin", "marketplace", "add", source_url])
    ref = name or source_url
    if ok:
        return LifecycleResult(passed=True, ref=ref, message="marketplace added")
    return LifecycleResult(passed=False, ref=ref, message=f"add failed: {stderr.strip()}")


def remove_marketplace(name: str) -> LifecycleResult:
    """Remove a marketplace via `claude plugin marketplace remove`."""
    ok, stdout, stderr = _run_claude(["plugin", "marketplace", "remove", name])
    if ok:
        return LifecycleResult(passed=True, ref=name, message="marketplace removed")
    return LifecycleResult(passed=False, ref=name, message=f"remove failed: {stderr.strip()}")


def update_marketplace(name: str = "") -> LifecycleResult:
    """Update a marketplace via `claude plugin marketplace update`."""
    args = ["plugin", "marketplace", "update"]
    if name:
        args.append(name)
    ok, stdout, stderr = _run_claude(args)
    ref = name or "all"
    if ok:
        return LifecycleResult(passed=True, ref=ref, message="marketplace updated")
    return LifecycleResult(passed=False, ref=ref, message=f"update failed: {stderr.strip()}")


# --- Plugin operations ---

def check_plugin_installed(plugin_ref: str) -> LifecycleResult:
    """Check if a plugin is installed in the global installed_plugins.json.

    Args:
        plugin_ref: Plugin reference in marketplace:plugin format
    """
    ip_path = os.path.expanduser("~/.claude/plugins/installed_plugins.json")
    try:
        with open(ip_path, "r") as f:
            data = json.load(f)
        plugins = data.get("plugins", {})
        # Check both marketplace:plugin and plugin@marketplace formats
        # since Claude Code CLI uses plugin@marketplace internally
        if plugin_ref in plugins:
            return LifecycleResult(passed=True, ref=plugin_ref, message="installed")
        # Try the CLI format (plugin@marketplace)
        if ":" in plugin_ref:
            marketplace, plugin_name = plugin_ref.split(":", 1)
            cli_ref = f"{plugin_name}@{marketplace}"
            if cli_ref in plugins:
                return LifecycleResult(passed=True, ref=plugin_ref, message="installed")
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return LifecycleResult(passed=False, ref=plugin_ref, message="not installed")


def install_plugin(plugin_ref: str, scope: str = "user") -> LifecycleResult:
    """Install a plugin via `claude plugin install`.

    Args:
        plugin_ref: Plugin reference in marketplace:plugin format
        scope: Installation scope (user, project, local)
    """
    # Claude CLI uses plugin@marketplace format
    if ":" in plugin_ref:
        marketplace, plugin_name = plugin_ref.split(":", 1)
        cli_ref = f"{plugin_name}@{marketplace}"
    else:
        cli_ref = plugin_ref

    ok, stdout, stderr = _run_claude(["plugin", "install", cli_ref, "--scope", scope])
    if ok:
        return LifecycleResult(passed=True, ref=plugin_ref, message="installed")
    return LifecycleResult(passed=False, ref=plugin_ref, message=f"install failed: {stderr.strip()}")


def uninstall_plugin(plugin_ref: str) -> LifecycleResult:
    """Uninstall a plugin via `claude plugin uninstall`."""
    if ":" in plugin_ref:
        marketplace, plugin_name = plugin_ref.split(":", 1)
        cli_ref = f"{plugin_name}@{marketplace}"
    else:
        cli_ref = plugin_ref

    ok, stdout, stderr = _run_claude(["plugin", "uninstall", cli_ref])
    if ok:
        return LifecycleResult(passed=True, ref=plugin_ref, message="uninstalled")
    return LifecycleResult(passed=False, ref=plugin_ref, message=f"uninstall failed: {stderr.strip()}")


def update_plugin(plugin_ref: str, scope: str = "user") -> LifecycleResult:
    """Update a plugin via `claude plugin update`."""
    if ":" in plugin_ref:
        marketplace, plugin_name = plugin_ref.split(":", 1)
        cli_ref = f"{plugin_name}@{marketplace}"
    else:
        cli_ref = plugin_ref

    ok, stdout, stderr = _run_claude(["plugin", "update", cli_ref, "--scope", scope])
    if ok:
        return LifecycleResult(passed=True, ref=plugin_ref, message="updated")
    return LifecycleResult(passed=False, ref=plugin_ref, message=f"update failed: {stderr.strip()}")
