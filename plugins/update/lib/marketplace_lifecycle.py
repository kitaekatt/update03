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


class VersionCheckResult(NamedTuple):
    up_to_date: bool
    ref: str
    installed_version: str
    latest_version: str  # empty string if unknown
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
    """Check if a marketplace is registered and cloned in known_marketplaces.json.

    A marketplace entry without installLocation means the JSON entry exists
    (e.g. from json_entries merge) but the repo hasn't been cloned yet.
    """
    km_path = os.path.expanduser("~/.claude/plugins/known_marketplaces.json")
    try:
        with open(km_path, "r") as f:
            data = json.load(f)
        if name in data and data[name].get("installLocation"):
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


def _to_cli_ref(plugin_ref: str) -> str:
    """Convert marketplace:plugin to plugin@marketplace format for CLI."""
    if ":" in plugin_ref:
        marketplace, plugin_name = plugin_ref.split(":", 1)
        return f"{plugin_name}@{marketplace}"
    return plugin_ref


def check_plugin_version(plugin_ref: str) -> VersionCheckResult:
    """Check if the installed plugin version matches the latest marketplace version.

    Returns up_to_date=True if current or version cannot be determined.
    Returns up_to_date=False only when a definitive newer version is available.
    """
    cli_ref = _to_cli_ref(plugin_ref)
    marketplace = plugin_ref.split(":", 1)[0] if ":" in plugin_ref else None
    plugin_name = plugin_ref.split(":", 1)[1] if ":" in plugin_ref else plugin_ref

    # Get installed version
    ip_path = os.path.expanduser("~/.claude/plugins/installed_plugins.json")
    installed_version = ""
    try:
        with open(ip_path, "r") as f:
            data = json.load(f)
        installs = data.get("plugins", {}).get(cli_ref, [])
        if installs:
            installed_version = installs[0].get("version", "")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    if not installed_version:
        return VersionCheckResult(
            up_to_date=True, ref=plugin_ref,
            installed_version="", latest_version="",
            message="not installed (skipping version check)",
        )

    if not marketplace:
        return VersionCheckResult(
            up_to_date=True, ref=plugin_ref,
            installed_version=installed_version, latest_version="",
            message=f"version {installed_version} (no marketplace)",
        )

    # Get latest version from marketplace index
    km_path = os.path.expanduser("~/.claude/plugins/known_marketplaces.json")
    latest_version = ""
    try:
        with open(km_path, "r") as f:
            km_data = json.load(f)
        install_location = km_data.get(marketplace, {}).get("installLocation", "")
        if install_location:
            mkt_path = os.path.join(install_location, ".claude-plugin", "marketplace.json")
            with open(mkt_path, "r") as f:
                mkt_data = json.load(f)
            for entry in mkt_data.get("plugins", []):
                if entry.get("name") == plugin_name:
                    latest_version = entry.get("version", "")
                    break
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    if not latest_version:
        return VersionCheckResult(
            up_to_date=True, ref=plugin_ref,
            installed_version=installed_version, latest_version="",
            message=f"version {installed_version} (marketplace version unknown)",
        )

    if installed_version == latest_version:
        return VersionCheckResult(
            up_to_date=True, ref=plugin_ref,
            installed_version=installed_version, latest_version=latest_version,
            message=f"version {installed_version} (current)",
        )

    return VersionCheckResult(
        up_to_date=False, ref=plugin_ref,
        installed_version=installed_version, latest_version=latest_version,
        message=f"installed {installed_version}, latest {latest_version}",
    )


def check_plugin_enabled(plugin_ref: str) -> LifecycleResult:
    """Check if a plugin is currently enabled in settings.json enabledPlugins."""
    cli_ref = _to_cli_ref(plugin_ref)
    settings_path = os.path.expanduser("~/.claude/settings.json")
    try:
        with open(settings_path, "r") as f:
            data = json.load(f)
        if data.get("enabledPlugins", {}).get(cli_ref) is True:
            return LifecycleResult(passed=True, ref=plugin_ref, message="enabled")
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return LifecycleResult(passed=False, ref=plugin_ref, message="not enabled")


def enable_plugin_in_claude(plugin_ref: str) -> LifecycleResult:
    """Enable a plugin in Claude Code via `claude plugin enable`."""
    cli_ref = _to_cli_ref(plugin_ref)
    ok, stdout, stderr = _run_claude(["plugin", "enable", cli_ref])
    if ok:
        return LifecycleResult(passed=True, ref=plugin_ref, message="enabled in Claude Code")
    return LifecycleResult(passed=False, ref=plugin_ref, message=f"enable failed: {stderr.strip()}")


def disable_plugin_in_claude(plugin_ref: str) -> LifecycleResult:
    """Disable a plugin in Claude Code via `claude plugin disable`."""
    cli_ref = _to_cli_ref(plugin_ref)
    ok, stdout, stderr = _run_claude(["plugin", "disable", cli_ref])
    if ok:
        return LifecycleResult(passed=True, ref=plugin_ref, message="disabled in Claude Code")
    return LifecycleResult(passed=False, ref=plugin_ref, message=f"disable failed: {stderr.strip()}")
