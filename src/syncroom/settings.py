from __future__ import annotations

import getpass
import json
import os
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSettings


APP_DIR_NAME = "syncroom"
SETTINGS_FILE_NAME = "settings.json"
SETTINGS_ORG = "justys"
SETTINGS_APP = "SyncRoom"


def app_config_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / APP_DIR_NAME


def settings_path() -> Path:
    return app_config_dir() / SETTINGS_FILE_NAME


def logs_dir() -> Path:
    path = app_config_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_settings() -> dict[str, Any]:
    qt_settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
    keys = qt_settings.allKeys()
    if keys:
        return {key: qt_settings.value(key) for key in keys}

    path = settings_path()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}


def default_display_name() -> str:
    try:
        return getpass.getuser().strip() or "guest"
    except Exception:
        return os.environ.get("USER", "guest").strip() or "guest"


def save_settings(payload: dict[str, Any]) -> None:
    qt_settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
    qt_settings.clear()
    for key, value in payload.items():
        qt_settings.setValue(key, value)
    qt_settings.sync()

    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
