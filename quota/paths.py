"""XDG locations. Accounts (no secrets) live in the config dir, isolated CLI homes in the data dir."""

import os
from pathlib import Path


def _xdg(variable: str, fallback: str) -> Path:
    value = os.environ.get(variable)
    return Path(value) if value else Path.home() / fallback


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / "quota"


def data_dir() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share") / "quota"


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state") / "quota"


def accounts_file() -> Path:
    return config_dir() / "accounts.json"


def accounts_root() -> Path:
    return data_dir() / "accounts"


def account_home(provider: str, account_id: str) -> Path:
    return accounts_root() / provider / account_id
