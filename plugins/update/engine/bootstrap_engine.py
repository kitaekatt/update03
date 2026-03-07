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
    parser.add_argument("--verbose", action="store_true", help="Show all entries including ok/cached")
    parser.add_argument("--console", action="store_true", help="Plain text output, no JSON/log writes")
    args = parser.parse_args()

    # --console implies --verbose
    if args.console:
        args.verbose = True

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
    manifest_path = os.path.join(plugin_root, "update.json")
    compute_current_hash(data_dir, [manifest_path])
    self_cached = check_cache(data_dir, [manifest_path])

    current_os = detect_os()
    log_success = config.get("log_success_checks", False) or args.verbose
    all_failures = []
    # Bootstrap's own entries — written to bootstrap's log
    bootstrap_action_entries = []
    bootstrap_ok_entries = []
    # Display sections: list of (header, action_entries, ok_entries)
    display_sections = []

    # Detect marketplace name and plugin identity for log prefixes
    plugins_dir = os.path.dirname(plugin_root)
    # Marketplace name: go 2 levels up from plugin_root and take basename.
    # Dev: update03/plugins/update → up 2 → update03
    # Cache: cache/update03/update/0.9.0 → up 2 → update03
    marketplace_name = os.path.basename(os.path.normpath(os.path.join(plugin_root, "..", "..")))
    plugin_json_path = os.path.join(plugin_root, ".claude-plugin", "plugin.json")
    boot_plugin_name = "update"
    version = ""
    try:
        with open(plugin_json_path, "r") as f:
            pj = json.load(f)
            boot_plugin_name = pj.get("name", "update")
            version = pj.get("version", "")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    version_suffix = f"@{version}" if version else ""
    bootstrap_label = f"{marketplace_name}:{boot_plugin_name}{version_suffix}" if marketplace_name else f"{boot_plugin_name}{version_suffix}"

    # Step 3: Self-bootstrap (own manifest)
    # "cached" entries are log-file-only (not displayed) — they mean "nothing to check"
    all_cached_entries = []
    if self_cached:
        all_cached_entries.append(f"{bootstrap_label}: cached")
    else:
        with open(manifest_path, "r") as f:
            manifest = json.load(f)

        action_entries = []
        ok_entries = []
        failures = _process_manifest(manifest, current_os, data_dir, plugin_root, action_entries, ok_entries)
        bootstrap_action_entries.extend(action_entries)
        bootstrap_ok_entries.extend(ok_entries)

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
            all_cached_entries.append("user: cached")
        else:
            with open(user_manifest_path, "r") as f:
                user_manifest = json.load(f)
            action_entries = []
            ok_entries = []
            failures = _process_manifest(
                user_manifest, current_os, data_dir, plugin_root, action_entries, ok_entries,
                plugin_name="user",
            )
            bootstrap_action_entries.extend(f"user: {e}" for e in action_entries)
            bootstrap_ok_entries.extend(f"user: {e}" for e in ok_entries)
            if failures:
                all_failures.extend(failures)
            else:
                write_cache(data_dir, [user_manifest_path])

    # Add bootstrap's own section to display
    display_sections.append((bootstrap_label, list(bootstrap_action_entries), list(bootstrap_ok_entries)))

    # Step 4: Process enabled plugins
    registry_path = os.path.join(plugins_dir, "installed_plugins.json")

    enabled_plugins = list_enabled_plugins(config, registry_path, plugins_dir)
    for plugin_info in enabled_plugins:
        plugin_manifest_path = os.path.join(plugin_info.install_path, "update.json")
        if not os.path.isfile(plugin_manifest_path):
            continue

        # Per-plugin data dir and cache
        plugin_data_dir = os.path.join(
            os.path.dirname(data_dir), plugin_info.name
        )
        os.makedirs(plugin_data_dir, exist_ok=True)

        with open(plugin_manifest_path, "r") as f:
            plugin_manifest = json.load(f)

        # Per-plugin entry lists
        plugin_action_entries = []
        plugin_ok_entries = []

        # Config phase runs outside the cache gate (config can change between sessions)
        config_section = plugin_manifest.get("config")
        if config_section:
            config_failures = _process_config(
                config_section, plugin_data_dir, plugin_info.install_path,
                plugin_action_entries, ok_entries=plugin_ok_entries, plugin_name=plugin_info.name,
            )
            if config_failures:
                all_failures.extend(config_failures)

        # Cache gate for tools/venv/git_deps
        compute_current_hash(plugin_data_dir, [plugin_manifest_path])
        if check_cache(plugin_data_dir, [plugin_manifest_path]):
            all_cached_entries.append(f"{plugin_info.name}: cached")
        else:
            action_entries = []
            ok_entries = []
            failures = _process_manifest(
                plugin_manifest, current_os, plugin_data_dir, plugin_info.install_path,
                action_entries, ok_entries, plugin_name=plugin_info.name,
            )
            plugin_action_entries.extend(action_entries)
            plugin_ok_entries.extend(ok_entries)

            if failures:
                all_failures.extend(failures)
            else:
                write_cache(plugin_data_dir, [plugin_manifest_path])

        # Add plugin section to display
        plugin_label = f"{plugin_info.name}@{plugin_info.version}" if plugin_info.version else plugin_info.name
        plugin_display_header = f"{plugin_info.marketplace}:{plugin_label}" if plugin_info.marketplace else plugin_label
        display_sections.append((plugin_display_header, list(plugin_action_entries), list(plugin_ok_entries)))

    # Step 5: Read shell log entries BEFORE writing engine entries to the log
    if not args.console:
        shell_content = _read_new_log_entries(data_dir)
    else:
        shell_content = ""  # Console mode: shell already printed its entries

    # Step 6: Write bootstrap's own entries to log file (for debugging)
    # Skip in console mode — no file writes
    bootstrap_log_entries = bootstrap_action_entries + bootstrap_ok_entries + all_cached_entries
    if bootstrap_log_entries and not args.console:
        write_log_block(data_dir, bootstrap_label, bootstrap_log_entries)

    # Step 7: Build display from sections — actions always, ok only if log_success
    # Each section with entries gets a header line
    display_lines = []
    for header, actions, oks in display_sections:
        section_entries = list(actions)
        if log_success:
            section_entries.extend(oks)
        if section_entries:
            display_lines.append(f"--- {header} ---")
            display_lines.extend(section_entries)
    # --verbose also includes cached entries (appended without section header)
    if args.verbose:
        display_lines.extend(all_cached_entries)

    if args.console:
        # Console mode: plain text to stdout, no JSON
        for line in display_lines:
            print(line)
        if all_failures:
            print(f"\n{bootstrap_label} -> {len(all_failures)} failure(s):")
            for f in all_failures:
                print(f"  - [{f['type']}] {f.get('name', f.get('message', ''))}")
        return

    # Build final display: shell entries + section entries
    parts = []
    if shell_content:
        parts.append(shell_content)
    parts.extend(display_lines)
    display_content = "\n".join(parts)

    # Update the log display marker
    _update_display_marker(data_dir)

    # Step 8: Emit results
    if all_failures:
        emit_failure_response(all_failures, current_os, display_content, label=bootstrap_label)
    elif display_content:
        emit_success_response(display_content, label=bootstrap_label)
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


def _process_config(config_section, plugin_data_dir, plugin_root, action_entries, ok_entries=None, plugin_name=""):
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
                    action_entries.append("config autodetect updated values")
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
        if ok_entries is not None:
            ok_entries.append("config ok")
        else:
            action_entries.append("config ok")
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


def _process_manifest(manifest, current_os, data_dir, plugin_root, action_entries, ok_entries, plugin_name="bootstrap"):
    """Process a single plugin's bootstrap manifest. Returns list of failures.

    Entries are split into two lists:
    - action_entries: actions performed, failures, conditions not met (always displayed)
    - ok_entries: checks that passed (only displayed if log_success is true)
    """
    from tool_check import check_tool
    from path_check import check_path_entry
    from venv_check import check_venv
    from git_dep_check import check_git_dep

    failures = []
    prefix = ""

    # Check tools
    for tool_def in manifest.get("tools", []):
        name = tool_def["name"]
        install_cmds = tool_def.get("install", {})
        result = check_tool(name, install_cmds, current_os)

        if result.passed:
            ok_entries.append(f"{prefix}{result.name}: ok - {result.message}")
            continue

        # Tool not found — attempt remediation if install command available
        if result.install_cmd:
            action_entries.append(f"{prefix}{result.name}: not found, attempting install")
            from tool_check import run_install
            ok, _output = run_install(result.install_cmd)
            if ok:
                recheck = check_tool(name, install_cmds, current_os)
                if recheck.passed:
                    action_entries.append(f"{prefix}{result.name}: installed - ran `{result.install_cmd}`, now {recheck.message}")
                    continue  # no failure to record
            # Install failed or tool still missing after install
            action_entries.append(f"{prefix}{result.name}: FAILED - install attempted but still not found")
        else:
            action_entries.append(f"{prefix}{result.name}: FAILED - {result.message}")

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
        if result.passed:
            ok_entries.append(f"{prefix}PATH {result.path}: ok - {result.message}")
        else:
            # Attempt persistent remediation: add to shell RC files
            from path_check import add_path_to_shell_config
            ok, msg = add_path_to_shell_config(path_entry)
            action_entries.append(f"{prefix}PATH {result.path}: not in PATH, added to shell config - {msg}")
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
            uv_cmd = f"uv sync --project {plugin_root}"
            action_entries.append(f"{prefix}venv: not ready, running `{uv_cmd}`")
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
                        action_entries.append(f"{prefix}venv: created")
                except (_sp.SubprocessError, OSError):
                    pass  # Fall through to failure handling

        if result.passed:
            ok_entries.append(f"{prefix}venv: ok - {result.message}")
        else:
            action_entries.append(f"{prefix}venv: FAILED - {result.message}")
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
        if result.passed:
            ok_entries.append(f"{prefix}git {result.repo_name}: ok - {result.message}")
        else:
            from git_dep_check import clone_git_dep, pull_git_dep
            import os as _os
            target_path = result.target_path
            if not _os.path.isdir(target_path):
                # Not cloned — clone it
                action_entries.append(f"{prefix}git {result.repo_name}: not cloned, cloning from {dep_def['url']}")
                ok, msg = clone_git_dep(dep_def["url"], dep_def["branch"], target_path, dep_def.get("sparse_paths"))
            else:
                # Exists but wrong branch or broken — pull
                action_entries.append(f"{prefix}git {result.repo_name}: {result.message}, pulling")
                ok, msg = pull_git_dep(target_path)

            if ok:
                action_entries.append(f"{prefix}git {result.repo_name}: {msg}")
            else:
                action_entries.append(f"{prefix}git {result.repo_name}: FAILED - {msg}")
                failures.append({
                    "type": "git_dep",
                    "name": result.repo_name,
                    "message": msg,
                    "remediation_cmd": result.remediation_cmd,
                    "plugin": plugin_name,
                })

    # Check marketplace entries (before json_entries — marketplaces must be cloned
    # before we merge fields like autoUpdate into known_marketplaces.json)
    for mkt_def in manifest.get("marketplaces", []):
        mkt_name = mkt_def.get("name", "")
        source_url = mkt_def.get("source", "")
        if not mkt_name or not source_url:
            continue

        from marketplace_lifecycle import check_marketplace_exists, add_marketplace, update_marketplace

        mkt_result = check_marketplace_exists(mkt_name)
        if mkt_result.passed:
            ok_entries.append(f"{prefix}marketplace {mkt_name}: ok")
        else:
            # Auto-add marketplace via CLI
            action_entries.append(f"{prefix}marketplace {mkt_name}: not found, adding")
            add_result = add_marketplace(source_url, mkt_name)
            if add_result.passed:
                action_entries.append(f"{prefix}marketplace {mkt_name}: added via `claude plugin marketplace add {source_url}` (modifies known_marketplaces.json)")
            else:
                action_entries.append(f"{prefix}marketplace {mkt_name}: FAILED - {add_result.message}")
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

        # Compute CLI ref for logging (marketplace:plugin → plugin@marketplace)
        cli_ref = f"{plugin_ref.split(':', 1)[1]}@{plugin_ref.split(':', 1)[0]}" if ":" in plugin_ref else plugin_ref

        # Check if plugin is installed (global registry, handles both ref formats)
        install_result = check_plugin_installed(plugin_ref)
        if not install_result.passed:
            # Auto-install via CLI
            action_entries.append(f"{prefix}plugin {plugin_ref}: not installed, running `claude plugin install {cli_ref}`")
            inst = install_plugin(plugin_ref)
            if inst.passed:
                action_entries.append(f"{prefix}plugin {plugin_ref}: installed (added '{cli_ref}' to settings.json enabledPlugins)")
            else:
                action_entries.append(f"{prefix}plugin {plugin_ref}: FAILED - {inst.message}")
                failures.append({
                    "type": "plugin",
                    "ref": plugin_ref,
                    "message": inst.message,
                    "plugin": plugin_name,
                })
                continue

        from marketplace_lifecycle import enable_plugin_in_claude, disable_plugin_in_claude, check_plugin_enabled, check_plugin_version, update_plugin

        if enabled:
            # Check if version is up to date (only for already-installed plugins)
            if install_result.passed:
                ver_result = check_plugin_version(plugin_ref)
                if not ver_result.up_to_date:
                    action_entries.append(f"{prefix}plugin {plugin_ref}: outdated ({ver_result.message}), running `claude plugin update {cli_ref}`")
                    upd_result = update_plugin(plugin_ref)
                    if upd_result.passed:
                        action_entries.append(f"{prefix}plugin {plugin_ref}: updated to {ver_result.latest_version}")
                    else:
                        action_entries.append(f"{prefix}plugin {plugin_ref}: update failed - {upd_result.message}")

            # Check enabled state in settings.json
            enabled_result = check_plugin_enabled(plugin_ref)
            if enabled_result.passed:
                ok_entries.append(f"{prefix}plugin {plugin_ref}: ok")
            else:
                action_entries.append(f"{prefix}plugin {plugin_ref}: installed but not enabled, running `claude plugin enable {cli_ref}`")
                en_result = enable_plugin_in_claude(plugin_ref)
                if en_result.passed:
                    action_entries.append(f"{prefix}plugin {plugin_ref}: enabled (added '{cli_ref}' to settings.json enabledPlugins)")
                else:
                    action_entries.append(f"{prefix}plugin {plugin_ref}: enable failed - {en_result.message}")
                    failures.append({
                        "type": "plugin",
                        "ref": plugin_ref,
                        "message": en_result.message,
                        "plugin": plugin_name,
                    })
        else:
            # Only disable if currently enabled (check before acting)
            enabled_result = check_plugin_enabled(plugin_ref)
            if not enabled_result.passed:
                ok_entries.append(f"{prefix}plugin {plugin_ref}: already disabled")
            else:
                dis_result = disable_plugin_in_claude(plugin_ref)
                if dis_result.passed:
                    action_entries.append(f"{prefix}plugin {plugin_ref}: disabled via `claude plugin disable {cli_ref}` (removed '{cli_ref}' from settings.json enabledPlugins)")
                else:
                    action_entries.append(f"{prefix}plugin {plugin_ref}: disable failed - {dis_result.message}")
                    failures.append({
                        "type": "plugin",
                        "ref": plugin_ref,
                        "message": dis_result.message,
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
            ok_entries.append(f"{prefix}ini {ini_def['file']}: skipped (unresolved vars)")
            continue

        section = ini_def["section"]
        # Ensure section has brackets for check/write
        section_header = section if section.startswith("[") else f"[{section}]"

        from ini_check import check_ini_setting, write_ini_setting
        for key, expected in ini_def.get("settings", {}).items():
            result = check_ini_setting(ini_file, section_header, key, expected)
            if result.passed:
                ok_entries.append(f"{prefix}ini {key}: ok")
            else:
                try:
                    write_ini_setting(ini_file, section_header, key, expected)
                    action_entries.append(f"{prefix}ini {key}: set to {expected}")
                except OSError as e:
                    action_entries.append(f"{prefix}ini {key}: FAILED - {e}")
                    failures.append({
                        "type": "ini",
                        "file": ini_file,
                        "key": key,
                        "message": str(e),
                        "plugin": plugin_name,
                    })

    # Check JSON entries (after marketplaces — so known_marketplaces.json has valid entries)
    for json_def in manifest.get("json_entries", []):
        ref_path = resolve_vars(json_def.get("reference", ""), variables)
        target_path = resolve_vars(json_def.get("target", ""), variables)
        if ref_path is None or target_path is None:
            ok_entries.append(f"{prefix}json: skipped (unresolved vars)")
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
            ok_entries.append(f"{prefix}json {os.path.basename(target_path)}: ok")
        else:
            result = merge_json_entries(ref_path, target_path, merge_fields, preserve_fields)
            if result.passed:
                action_entries.append(f"{prefix}json {os.path.basename(target_path)}: merged")
            else:
                action_entries.append(f"{prefix}json {os.path.basename(target_path)}: FAILED - {result.message}")
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
            ok_entries.append(f"{prefix}pypi {pypi_def['package']}: skipped (unresolved vars)")
            continue

        from pypi_check import check_pypi_package, download_and_extract
        result = check_pypi_package(pypi_def["package"], extract_to)
        if result.passed:
            ok_entries.append(f"{prefix}pypi {result.package}: ok")
        else:
            extract_pattern = pypi_def.get("extract_pattern")
            result = download_and_extract(pypi_def["package"], extract_to, extract_pattern)
            if result.passed:
                action_entries.append(f"{prefix}pypi {result.package}: {result.message}")
            else:
                action_entries.append(f"{prefix}pypi {result.package}: FAILED - {result.message}")
                failures.append({
                    "type": "pypi",
                    "package": pypi_def["package"],
                    "message": result.message,
                    "plugin": plugin_name,
                })

    # Script phase
    script_def = manifest.get("script")
    if script_def:
        script_failures = _run_script_phase(
            script_def, plugin_root, data_dir, config, action_entries,
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
    Does NOT update the marker — call _update_display_marker() after all entries are written.
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

    # Filter to blocks after the last-displayed timestamp.
    # Timestamps are only on header lines (--- label timestamp ---).
    # When a header is old, skip it and all lines until the next header.
    new_lines = []
    include_block = not last_displayed  # If no marker, include everything
    for line in lines:
        ts = _extract_timestamp(line)
        if ts:
            # This is a header line — decide whether to include this block
            include_block = ts > last_displayed if last_displayed else True
        if include_block:
            new_lines.append(line)

    if not new_lines:
        return ""

    return "".join(new_lines).rstrip("\n")


def _update_display_marker(data_dir):
    """Update the display marker to the latest timestamp in the log file."""
    from log import LOG_FILENAME
    log_file = os.path.join(data_dir, LOG_FILENAME)
    marker_file = os.path.join(data_dir, "last_displayed_at")

    try:
        with open(log_file, "r") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return

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


def _extract_timestamp(line):
    """Extract ISO timestamp from a log header line.

    Format: --- label timestamp ---
    Returns the timestamp string or empty string.
    """
    line = line.strip()
    if line.startswith("---") and line.endswith("---"):
        parts = line.split()
        if len(parts) >= 3:
            return parts[-2]
    return ""


def emit_success_response(log_content, label="bootstrap"):
    """Emit hook JSON showing bootstrap log to user and agent."""
    response = {
        "continue": True,
        "suppressOutput": False,
        "systemMessage": f"{label}:\n{log_content}",
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": f"{label} -> bootstrap complete:\n{log_content}",
        },
    }
    print(json.dumps(response))


def emit_failure_response(failures, current_os, log_content, label="bootstrap"):
    """Emit hook JSON with fix-all directives to stdout."""
    agent_lines = [f"{label} -> Setup issues found. Fix in order:\n"]

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
        "systemMessage": f"{label}:\n{log_content}",
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": agent_msg,
        },
    }

    print(json.dumps(response))


if __name__ == "__main__":
    main()
