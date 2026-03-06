#!/usr/bin/env python3
"""Bootstrap engine — processes bootstrap manifests and emits hook responses.

Usage:
    python3 bootstrap_engine.py --plugin-root /path/to/bootstrap --data-dir /path/to/data

Exit behavior:
    Emits hook JSON to stdout with systemMessage showing new log entries.
    On failure, additionalContext includes remediation instructions for the agent.
    Silent exit (no stdout) when there are no new log entries to display.
"""

import argparse
import json
import os
import sys


def main():
    parser = argparse.ArgumentParser(description="Bootstrap engine")
    parser.add_argument("--plugin-root", required=True, help="Path to bootstrap plugin root")
    parser.add_argument("--data-dir", required=True, help="Path to bootstrap data directory")
    parser.add_argument("--hook-start-epoch", type=int, default=0, help="(unused, kept for backward compat)")
    args = parser.parse_args()

    plugin_root = args.plugin_root
    data_dir = args.data_dir

    # Add lib/ to path for imports
    sys.path.insert(0, os.path.join(plugin_root, "lib"))
    sys.path.insert(0, os.path.join(plugin_root, "engine"))

    from config import load_config
    from cache import check_cache, write_cache, compute_current_hash
    from log import write_log_block
    from tool_check import check_tool
    from path_check import check_path_entry
    from platform_detect import detect_os
    from plugin_resolve import list_enabled_plugins
    from venv_check import check_venv
    from git_dep_check import check_git_dep

    # Step 1: Load/migrate config
    defaults_dir = os.path.join(plugin_root, "defaults")
    config = load_config(data_dir, defaults_dir)

    # Step 2: Compute current hash + check cache (self-bootstrap only)
    manifest_path = os.path.join(plugin_root, "bootstrap.json")
    compute_current_hash(data_dir, [manifest_path])
    self_cached = check_cache(data_dir, [manifest_path])

    current_os = detect_os()
    log_success = config.get("log_success_checks", True)
    all_failures = []
    all_log_entries = []

    # Step 3: Self-bootstrap (own manifest)
    if self_cached:
        if log_success:
            all_log_entries.append("bootstrap: cached")
    else:
        with open(manifest_path, "r") as f:
            manifest = json.load(f)

        log_entries = []
        failures = _process_manifest(manifest, current_os, data_dir, plugin_root, log_entries, log_success=log_success)
        all_log_entries.extend(log_entries)

        if failures:
            all_failures.extend(failures)
        else:
            write_cache(data_dir, [manifest_path])

    # Step 3b: Activate bootstrap venv site-packages so PyYAML is available
    _activate_bootstrap_venv(data_dir)

    # Step 3c: Process personal config (user-bootstrap.json)
    user_manifest_path = os.path.join(data_dir, "user-bootstrap.json")
    if os.path.isfile(user_manifest_path):
        compute_current_hash(data_dir, [user_manifest_path])
        if check_cache(data_dir, [user_manifest_path]):
            if log_success:
                all_log_entries.append("user: cached")
        else:
            with open(user_manifest_path, "r") as f:
                user_manifest = json.load(f)
            log_entries = []
            failures = _process_manifest(
                user_manifest, current_os, data_dir, plugin_root, log_entries,
                plugin_name="user", log_success=log_success,
            )
            all_log_entries.extend(f"user: {e}" for e in log_entries)
            if failures:
                all_failures.extend(failures)
            else:
                write_cache(data_dir, [user_manifest_path])

    # Step 4: Process enabled plugins
    # Compute marketplace root: bootstrap is at <marketplace>/plugins/bootstrap
    plugins_dir = os.path.dirname(plugin_root)
    registry_path = os.path.join(plugins_dir, "installed_plugins.json")

    enabled = list_enabled_plugins(config, registry_path, plugins_dir)
    for plugin_info in enabled:
        plugin_manifest_path = os.path.join(plugin_info.install_path, "bootstrap.json")
        if not os.path.isfile(plugin_manifest_path):
            continue

        # Per-plugin data dir and cache
        plugin_data_dir = os.path.join(
            os.path.dirname(data_dir), plugin_info.name
        )
        os.makedirs(plugin_data_dir, exist_ok=True)

        with open(plugin_manifest_path, "r") as f:
            plugin_manifest = json.load(f)

        # Config phase runs outside the cache gate (config can change between sessions)
        config_section = plugin_manifest.get("config")
        if config_section:
            config_failures = _process_config(
                config_section, plugin_data_dir, plugin_info.install_path,
                all_log_entries, plugin_name=plugin_info.name,
            )
            if config_failures:
                all_failures.extend(config_failures)

        # Cache gate for tools/venv/git_deps
        compute_current_hash(plugin_data_dir, [plugin_manifest_path])
        if check_cache(plugin_data_dir, [plugin_manifest_path]):
            if log_success:
                all_log_entries.append(f"{plugin_info.name}: cached")
            continue

        log_entries = []
        failures = _process_manifest(
            plugin_manifest, current_os, plugin_data_dir, plugin_info.install_path, log_entries,
            plugin_name=plugin_info.name, log_success=log_success,
        )
        all_log_entries.extend(f"{plugin_info.name}: {e}" for e in log_entries)

        if failures:
            all_failures.extend(failures)
        else:
            write_cache(plugin_data_dir, [plugin_manifest_path])

    # Step 5: Write log block (header + entries) only if we have entries
    if all_log_entries:
        write_log_block(data_dir, "Engine", all_log_entries)

    # Step 7: Emit results — show new log entries to user (since last display)
    log_content = _read_new_log_entries(data_dir)
    if all_failures:
        emit_failure_response(all_failures, current_os, log_content)
    elif log_content:
        emit_success_response(log_content)
    # else: nothing to show — silent exit


def _activate_bootstrap_venv(data_dir):
    """Add bootstrap venv site-packages to sys.path so PyYAML is importable."""
    import glob as globmod
    venv_path = os.path.join(data_dir, ".venv")
    # Look for site-packages in both Unix and Windows layouts
    patterns = [
        os.path.join(venv_path, "lib", "python*", "site-packages"),
        os.path.join(venv_path, "Lib", "site-packages"),
    ]
    for pattern in patterns:
        matches = globmod.glob(pattern)
        for sp in matches:
            if sp not in sys.path:
                sys.path.insert(0, sp)


def _process_config(config_section, plugin_data_dir, plugin_root, log_entries, plugin_name=""):
    """Process the config section of a plugin manifest.

    Runs outside the cache gate — config can change between sessions.
    Returns list of failures (missing config fields).
    """
    from config_check import config_init, config_validate, run_autodetect, load_yaml_config, save_yaml_config

    config_file = config_section["file"]
    defaults_source = config_section.get("defaults_source")

    # 1. Config init: copy defaults if config doesn't exist
    if defaults_source:
        config_path = config_init(plugin_data_dir, plugin_root, defaults_source, config_file)
    else:
        config_path = os.path.join(plugin_data_dir, config_file)

    if not os.path.isfile(config_path):
        return []

    # 2. Load config
    config = load_yaml_config(config_path)

    required_fields = config_section.get("required_fields", {})

    # 3. Autodetect (optional): run only when at least one required field is empty
    autodetect_spec = config_section.get("autodetect")
    if autodetect_spec and required_fields:
        has_empty = any(not config.get(f) for f in required_fields)
        if has_empty:
            try:
                changed = run_autodetect(plugin_root, autodetect_spec, config, config_path)
                if changed:
                    save_yaml_config(config_path, config)
                    log_entries.append(f"{plugin_name}: config autodetect updated values")
            except Exception:
                pass  # Autodetect errors are non-fatal

    # 4. Validate required fields (apply defaults, collect missing)
    config, missing = config_validate(config, required_fields, config_path)

    # Write back if defaults were applied
    if any(f.get("default") is not None for f in required_fields.values()):
        # Re-check if any defaults were actually applied (config may have changed)
        current_on_disk = load_yaml_config(config_path)
        if config != current_on_disk:
            save_yaml_config(config_path, config)

    if not missing:
        log_entries.append(f"{plugin_name}: config ok")
        return []

    # 5. Fix-all: aggregate missing fields into failure directives
    failures = []
    for m in missing:
        failures.append({
            "type": "config",
            "field": m["field"],
            "user_msg": m["user_msg"],
            "agent_msg": m["agent_msg"],
            "plugin": plugin_name,
        })

    return failures


def _process_manifest(manifest, current_os, data_dir, plugin_root, log_entries, plugin_name="bootstrap", log_success=True):
    """Process a single plugin's bootstrap manifest. Returns list of failures."""
    from tool_check import check_tool
    from path_check import check_path_entry
    from venv_check import check_venv
    from git_dep_check import check_git_dep

    failures = []
    prefix = f"{plugin_name}: " if plugin_name != "bootstrap" else ""

    # Check tools
    for tool_def in manifest.get("tools", []):
        name = tool_def["name"]
        install_cmds = tool_def.get("install", {})
        result = check_tool(name, install_cmds, current_os)

        if result.passed:
            if log_success:
                log_entries.append(f"{prefix}{result.name}: ok - {result.message}")
            continue

        # Tool not found — attempt remediation if install command available
        if result.install_cmd:
            log_entries.append(f"{prefix}{result.name}: not found, attempting install")
            from tool_check import run_install
            ok, _output = run_install(result.install_cmd)
            if ok:
                recheck = check_tool(name, install_cmds, current_os)
                if recheck.passed:
                    log_entries.append(f"{prefix}{result.name}: installed - ran `{result.install_cmd}`, now {recheck.message}")
                    continue  # no failure to record
            # Install failed or tool still missing after install
            log_entries.append(f"{prefix}{result.name}: FAILED - install attempted but still not found")
        else:
            log_entries.append(f"{prefix}{result.name}: FAILED - {result.message}")

        failures.append({
            "type": "tool",
            "name": result.name,
            "message": result.message,
            "install_cmd": result.install_cmd,
            "plugin": plugin_name,
        })

    # Check path entries
    for path_entry in manifest.get("path_entries", []):
        expanded = os.path.expanduser(path_entry)
        result = check_path_entry(path_entry)
        if result.passed and not log_success:
            pass  # still need to fall through to ensure PATH update
        else:
            log_entries.append(f"{prefix}PATH {result.path}: {'ok' if result.passed else 'FAILED'} - {result.message}")
        if not result.passed:
            failures.append({
                "type": "path",
                "path": result.path,
                "message": result.message,
                "plugin": plugin_name,
            })
        # Add to current process PATH so subsequent phases can find tools there
        current_path = os.environ.get("PATH", "")
        if os.path.normpath(expanded) not in [os.path.normpath(d) for d in current_path.split(os.pathsep)]:
            os.environ["PATH"] = expanded + os.pathsep + current_path

    # Check venv
    venv_def = manifest.get("venv")
    if venv_def:
        check_imports = venv_def.get("check_imports", [])
        result = check_venv(data_dir, plugin_root, check_imports)

        if not result.passed:
            # Attempt auto-remediation — run uv sync with venv in data dir
            log_entries.append(f"{prefix}venv: not ready, attempting setup")
            import shutil
            import subprocess as _sp
            venv_path = os.path.join(data_dir, ".venv")

            # Find uv — may have just been installed to ~/.local/bin
            local_bin = os.path.expanduser("~/.local/bin")
            uv_bin = shutil.which("uv")
            if not uv_bin:
                # Check ~/.local/bin directly (not yet in PATH)
                for name in ("uv", "uv.exe", "uv.EXE"):
                    candidate = os.path.join(local_bin, name)
                    if os.path.isfile(candidate):
                        uv_bin = candidate
                        break

            if uv_bin:
                env = dict(os.environ, UV_PROJECT_ENVIRONMENT=venv_path)
                # Ensure ~/.local/bin in PATH for uv's own child processes
                if local_bin not in env.get("PATH", ""):
                    env["PATH"] = local_bin + os.pathsep + env.get("PATH", "")
                try:
                    _sp.run(
                        [uv_bin, "sync", "--project", plugin_root],
                        env=env, capture_output=True, timeout=120,
                    )
                    # Re-check after remediation
                    result = check_venv(data_dir, plugin_root, check_imports)
                    if result.passed:
                        log_entries.append(f"{prefix}venv: created")
                except (_sp.SubprocessError, OSError):
                    pass  # Fall through to failure handling

        if not result.passed or log_success:
            log_entries.append(f"{prefix}venv: {'ok' if result.passed else 'FAILED'} - {result.message}")
        if not result.passed:
            failures.append({
                "type": "venv",
                "message": result.message,
                "remediation_cmd": result.remediation_cmd,
                "plugin": plugin_name,
            })

    # Check git deps
    for dep_def in manifest.get("git_deps", []):
        result = check_git_dep(
            data_dir,
            dep_def["url"],
            dep_def["branch"],
            dep_def.get("sparse_paths"),
        )
        if not result.passed or log_success:
            log_entries.append(f"{prefix}git {result.repo_name}: {'ok' if result.passed else 'FAILED'} - {result.message}")
        if not result.passed:
            failures.append({
                "type": "git_dep",
                "name": result.repo_name,
                "message": result.message,
                "remediation_cmd": result.remediation_cmd,
                "plugin": plugin_name,
            })

    # Variable resolution for subsequent phases
    from var_resolve import build_variables, resolve_vars
    config = _load_plugin_config(data_dir)
    variables = build_variables(plugin_root, data_dir, config)

    # Check INI settings
    for ini_def in manifest.get("ini_settings", []):
        ini_file = resolve_vars(ini_def["file"], variables)
        if ini_file is None:
            if log_success:
                log_entries.append(f"{prefix}ini {ini_def['file']}: skipped (unresolved vars)")
            continue

        section = ini_def["section"]
        # Ensure section has brackets for check/write
        section_header = section if section.startswith("[") else f"[{section}]"

        from ini_check import check_ini_setting, write_ini_setting
        for key, expected in ini_def.get("settings", {}).items():
            result = check_ini_setting(ini_file, section_header, key, expected)
            if result.passed:
                if log_success:
                    log_entries.append(f"{prefix}ini {key}: ok")
            else:
                try:
                    write_ini_setting(ini_file, section_header, key, expected)
                    log_entries.append(f"{prefix}ini {key}: set to {expected}")
                except OSError as e:
                    log_entries.append(f"{prefix}ini {key}: FAILED - {e}")
                    failures.append({
                        "type": "ini",
                        "file": ini_file,
                        "key": key,
                        "message": str(e),
                        "plugin": plugin_name,
                    })

    # Check JSON entries
    for json_def in manifest.get("json_entries", []):
        ref_path = resolve_vars(json_def.get("reference", ""), variables)
        target_path = resolve_vars(json_def.get("target", ""), variables)
        if ref_path is None or target_path is None:
            if log_success:
                log_entries.append(f"{prefix}json: skipped (unresolved vars)")
            continue

        # Resolve reference relative to plugin root if not absolute
        if not os.path.isabs(ref_path):
            ref_path = os.path.join(plugin_root, ref_path)
        # Expand ~ in target path
        target_path = os.path.expanduser(target_path)

        merge_fields = json_def.get("merge_fields", [])
        preserve_fields = json_def.get("preserve_fields", [])

        from json_check import check_json_entries, merge_json_entries
        result = check_json_entries(ref_path, target_path, merge_fields, preserve_fields)
        if result.passed:
            if log_success:
                log_entries.append(f"{prefix}json {os.path.basename(target_path)}: ok")
        else:
            result = merge_json_entries(ref_path, target_path, merge_fields, preserve_fields)
            if result.passed:
                log_entries.append(f"{prefix}json {os.path.basename(target_path)}: merged")
            else:
                log_entries.append(f"{prefix}json {os.path.basename(target_path)}: FAILED - {result.message}")
                failures.append({
                    "type": "json",
                    "target": target_path,
                    "message": result.message,
                    "plugin": plugin_name,
                })

    # Check PyPI packages
    for pypi_def in manifest.get("pypi_packages", []):
        extract_to = resolve_vars(pypi_def["extract_to"], variables)
        if extract_to is None:
            if log_success:
                log_entries.append(f"{prefix}pypi {pypi_def['package']}: skipped (unresolved vars)")
            continue

        from pypi_check import check_pypi_package, download_and_extract
        result = check_pypi_package(pypi_def["package"], extract_to)
        if result.passed:
            if log_success:
                log_entries.append(f"{prefix}pypi {result.package}: ok")
        else:
            extract_pattern = pypi_def.get("extract_pattern")
            result = download_and_extract(pypi_def["package"], extract_to, extract_pattern)
            if result.passed:
                log_entries.append(f"{prefix}pypi {result.package}: {result.message}")
            else:
                log_entries.append(f"{prefix}pypi {result.package}: FAILED - {result.message}")
                failures.append({
                    "type": "pypi",
                    "package": pypi_def["package"],
                    "message": result.message,
                    "plugin": plugin_name,
                })

    # Check marketplace entries
    for mkt_def in manifest.get("marketplaces", []):
        mkt_name = mkt_def.get("name", "")
        source_url = mkt_def.get("source", "")
        if not mkt_name or not source_url:
            continue

        from marketplace_lifecycle import check_marketplace_exists, add_marketplace, update_marketplace

        mkt_result = check_marketplace_exists(mkt_name)
        if mkt_result.passed:
            if log_success:
                log_entries.append(f"{prefix}marketplace {mkt_name}: ok")
        else:
            # Auto-add marketplace via CLI
            log_entries.append(f"{prefix}marketplace {mkt_name}: not found, adding")
            add_result = add_marketplace(source_url, mkt_name)
            if add_result.passed:
                log_entries.append(f"{prefix}marketplace {mkt_name}: added")
            else:
                log_entries.append(f"{prefix}marketplace {mkt_name}: FAILED - {add_result.message}")
                failures.append({
                    "type": "marketplace",
                    "name": mkt_name,
                    "message": add_result.message,
                    "plugin": plugin_name,
                })

    # Check plugin entries
    for plugin_def in manifest.get("plugins", []):
        plugin_ref = plugin_def.get("ref", "")
        enabled = plugin_def.get("enabled", True)
        if not plugin_ref:
            continue

        from marketplace_lifecycle import check_plugin_installed, install_plugin
        from plugin_lifecycle import check_plugin_enabled
        from plugin_resolve import parse_plugin_ref

        config_path = os.path.join(os.path.dirname(data_dir), "bootstrap", "config.json")

        # Check if plugin is installed (global registry, handles both ref formats)
        install_result = check_plugin_installed(plugin_ref)
        if not install_result.passed:
            # Auto-install via CLI
            log_entries.append(f"{prefix}plugin {plugin_ref}: not installed, installing")
            inst = install_plugin(plugin_ref)
            if inst.passed:
                log_entries.append(f"{prefix}plugin {plugin_ref}: installed")
            else:
                log_entries.append(f"{prefix}plugin {plugin_ref}: FAILED - {inst.message}")
                failures.append({
                    "type": "plugin",
                    "ref": plugin_ref,
                    "message": inst.message,
                    "plugin": plugin_name,
                })
                continue

        if enabled:
            en_result = check_plugin_enabled(config_path, plugin_ref)
            if en_result.passed:
                if log_success:
                    log_entries.append(f"{prefix}plugin {plugin_ref}: ok")
            else:
                from plugin_lifecycle import enable_plugin
                enable_plugin(config_path, plugin_ref)
                log_entries.append(f"{prefix}plugin {plugin_ref}: enabled")
        else:
            en_result = check_plugin_enabled(config_path, plugin_ref)
            if not en_result.passed:
                if log_success:
                    log_entries.append(f"{prefix}plugin {plugin_ref}: ok (disabled)")
            else:
                from plugin_lifecycle import disable_plugin
                disable_plugin(config_path, plugin_ref)
                log_entries.append(f"{prefix}plugin {plugin_ref}: disabled")

    # Script phase
    script_def = manifest.get("script")
    if script_def:
        script_failures = _run_script_phase(
            script_def, plugin_root, data_dir, config, log_entries,
            prefix=prefix, plugin_name=plugin_name,
        )
        failures.extend(script_failures)

    return failures


def _load_plugin_config(data_dir):
    """Load plugin config from data_dir if it exists. Returns dict or empty."""
    try:
        from config_check import load_yaml_config
        import os
        config_path = os.path.join(data_dir, "config.yaml")
        if os.path.isfile(config_path):
            return load_yaml_config(config_path)
    except Exception:
        pass
    return {}


def _detect_marketplace_name(plugins_dir):
    """Detect the marketplace name from the plugins directory path.

    Works for both dev layout (~/Dev/<marketplace>/plugins/) and
    cache layout (~/.claude/plugins/cache/<marketplace>/<plugin>/).
    Falls back to reading installed_plugins.json keys for the marketplace name.
    """
    reg_path = os.path.join(plugins_dir, "installed_plugins.json")
    try:
        with open(reg_path, "r") as f:
            registry = json.load(f)
        for ref in registry.get("plugins", {}):
            if ":" in ref:
                return ref.split(":", 1)[0]
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    # Fallback: parent directory name
    return os.path.basename(os.path.dirname(plugins_dir))


def _run_script_phase(script_def, plugin_root, data_dir, config, log_entries, prefix="", plugin_name=""):
    """Run a custom bootstrap script. Returns list of failures."""
    import importlib.util

    script_path = os.path.join(plugin_root, script_def["path"])
    entry_point = script_def.get("entry_point", "bootstrap")

    if not os.path.isfile(script_path):
        log_entries.append(f"{prefix}script: skipped ({script_def['path']} not found)")
        return []

    # Build context object for the script
    ctx = _ScriptContext(config, data_dir, plugin_root, log_entries, prefix, plugin_name)

    try:
        spec = importlib.util.spec_from_file_location("_bootstrap_script", script_path)
        if spec is None or spec.loader is None:
            log_entries.append(f"{prefix}script: FAILED - could not load {script_def['path']}")
            return []
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        func = getattr(module, entry_point, None)
        if func is None:
            log_entries.append(f"{prefix}script: FAILED - {entry_point}() not found in {script_def['path']}")
            return []

        func(ctx)
        return ctx.failures
    except Exception as e:
        log_entries.append(f"{prefix}script: FAILED - {e}")
        return []


class _ScriptContext:
    """Context object passed to custom bootstrap scripts."""

    def __init__(self, config, data_dir, plugin_root, log_entries, prefix, plugin_name):
        self.config = dict(config) if config else {}
        self.config_path = os.path.join(data_dir, "config.yaml")
        self.data_dir = data_dir
        self.plugin_root = plugin_root
        self.failures = []
        self._log_entries = log_entries
        self._prefix = prefix
        self._plugin_name = plugin_name

    def save_config(self) -> None:
        """Write config back to disk."""
        from config_check import save_yaml_config
        save_yaml_config(self.config_path, self.config)

    def add_failure(self, failure_type: str, **kwargs) -> None:
        """Register a failure for fix-all aggregation."""
        failure = {"type": failure_type, "plugin": self._plugin_name}
        failure.update(kwargs)
        self.failures.append(failure)

    def log(self, message: str) -> None:
        """Add a log entry."""
        self._log_entries.append(f"{self._prefix}{message}")


def _read_new_log_entries(data_dir):
    """Read log entries since the last time we displayed them.

    Uses a 'last_displayed_at' file to track the timestamp of the last display.
    After reading, updates the timestamp so the next call only shows new entries.
    """
    from log import LOG_FILENAME
    log_file = os.path.join(data_dir, LOG_FILENAME)
    marker_file = os.path.join(data_dir, "last_displayed_at")

    try:
        with open(log_file, "r") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return ""

    # Read last-displayed timestamp
    last_displayed = ""
    try:
        with open(marker_file, "r") as f:
            last_displayed = f.read().strip()
    except FileNotFoundError:
        pass

    # Filter to entries after the last-displayed timestamp
    new_lines = []
    for line in lines:
        # Extract timestamp from lines like "[2026-03-05T18:47:24Z] ..."
        # or include header lines like "--- Shell 2026-03-05T18:47:24Z ---"
        ts = _extract_timestamp(line)
        if ts and last_displayed and ts <= last_displayed:
            continue
        new_lines.append(line)

    if not new_lines:
        return ""

    # Update the marker to the latest timestamp in the log
    latest_ts = ""
    for line in reversed(lines):
        ts = _extract_timestamp(line)
        if ts:
            latest_ts = ts
            break
    if latest_ts:
        os.makedirs(data_dir, exist_ok=True)
        with open(marker_file, "w") as f:
            f.write(latest_ts)

    return "".join(new_lines).rstrip("\n")


def _extract_timestamp(line):
    """Extract ISO timestamp from a log line.

    Handles both formats:
        [2026-03-05T18:47:24Z] message...
        --- Shell 2026-03-05T18:47:24Z ---
    Returns the timestamp string or empty string.
    """
    line = line.strip()
    if line.startswith("[") and "]" in line:
        return line[1:line.index("]")]
    if line.startswith("---") and line.endswith("---"):
        parts = line.split()
        if len(parts) >= 3:
            return parts[-2]
    return ""


def emit_success_response(log_content):
    """Emit hook JSON showing bootstrap log to user."""
    response = {
        "continue": True,
        "suppressOutput": False,
        "systemMessage": f"bootstrap:\n{log_content}",
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
        },
    }
    print(json.dumps(response))


def emit_failure_response(failures, current_os, log_content):
    """Emit hook JSON with fix-all directives to stdout."""
    agent_lines = ["bootstrap -> Setup issues found. Fix in order:\n"]

    for i, f in enumerate(failures, 1):
        plugin_tag = f" [{f['plugin']}]" if f.get("plugin", "bootstrap") != "bootstrap" else ""
        if f["type"] == "tool":
            agent_lines.append(f"{i}. Install {f['name']}{plugin_tag}: `{f['install_cmd'] or 'see documentation'}`")
        elif f["type"] == "path":
            agent_lines.append(f"{i}. Add {f['path']} to PATH{plugin_tag}")
        elif f["type"] == "venv":
            agent_lines.append(f"{i}. Setup venv{plugin_tag}: `{f['remediation_cmd']}`")
        elif f["type"] == "git_dep":
            agent_lines.append(f"{i}. Clone {f['name']}{plugin_tag}: `{f['remediation_cmd']}`")
        elif f["type"] == "config":
            agent_lines.append(f"{i}. {f['agent_msg']}{plugin_tag}")
        elif f["type"] == "ini":
            agent_lines.append(f"{i}. Fix INI setting {f['key']} in {f['file']}{plugin_tag}: {f['message']}")
        elif f["type"] == "pypi":
            agent_lines.append(f"{i}. Download {f['package']} from PyPI{plugin_tag}: {f['message']}")
        elif f["type"] == "script":
            agent_lines.append(f"{i}. Script issue{plugin_tag}: {f.get('message', 'see log')}")
        elif f["type"] == "json":
            agent_lines.append(f"{i}. Merge JSON entries into {f['target']}{plugin_tag}: {f['message']}")
        elif f["type"] == "marketplace":
            agent_lines.append(f"{i}. Add marketplace {f['name']}{plugin_tag}: {f['message']}")
        elif f["type"] == "plugin":
            agent_lines.append(f"{i}. Install plugin {f['ref']}{plugin_tag}: {f['message']}")

    agent_lines.append("\nAfter fixing, type 'fix-all' or 'fixed' to re-run bootstrap, or restart Claude Code.")
    agent_msg = "\n".join(agent_lines)

    response = {
        "continue": True,
        "suppressOutput": False,
        "systemMessage": f"bootstrap:\n{log_content}",
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": agent_msg,
        },
    }

    print(json.dumps(response))


if __name__ == "__main__":
    main()
