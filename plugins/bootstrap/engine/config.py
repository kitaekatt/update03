"""Bootstrap config loading, migration, and persistence."""

import json
import os
import shutil

CURRENT_SCHEMA_VERSION = 3


def load_config(data_dir: str, defaults_dir: str) -> dict:
    """Load config from data dir, copying defaults if missing.

    Args:
        data_dir: User data directory (e.g. ~/.claude/plugins/data/bootstrap/)
        defaults_dir: Plugin defaults directory containing config.json

    Returns:
        Parsed config dict
    """
    config_path = os.path.join(data_dir, "config.json")
    defaults_path = os.path.join(defaults_dir, "config.json")

    if not os.path.exists(config_path):
        os.makedirs(data_dir, exist_ok=True)
        shutil.copy2(defaults_path, config_path)

    with open(config_path, "r") as f:
        config = json.load(f)

    migrated = migrate_config(config)
    if migrated is not config:
        save_config(data_dir, migrated)
        return migrated

    return config


def migrate_config(config: dict) -> dict:
    """Migrate config to current schema version.

    Returns the same dict if no migration needed, or a new dict if migrated.
    """
    version = config.get("schema_version", 0)

    if version >= CURRENT_SCHEMA_VERSION:
        return config

    # Copy to avoid mutating the original
    migrated = dict(config)

    # Migration from v0 to v1: add missing fields
    if version < 1:
        migrated.setdefault("enabled_plugins", [])
        migrated["schema_version"] = 1

    # Migration from v1 to v2: add log_success settings
    if version < 2:
        migrated.setdefault("log_success_shell", True)
        migrated.setdefault("log_success_checks", True)
        migrated["schema_version"] = 2

    # Migration from v2 to v3: disable success logging by default
    if version < 3:
        migrated["log_success_shell"] = False
        migrated["log_success_checks"] = False
        migrated["schema_version"] = 3

    return migrated


def save_config(data_dir: str, config: dict) -> None:
    """Write config back to data dir.

    Args:
        data_dir: User data directory
        config: Config dict to save
    """
    config_path = os.path.join(data_dir, "config.json")
    os.makedirs(data_dir, exist_ok=True)
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")
