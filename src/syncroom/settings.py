from __future__ import annotations

import getpass
import json
import os
from pathlib import Path
from typing import Any


APP_DIR_NAME = "syncroom"
SETTINGS_FILE_NAME = "settings.json"


def settings_path() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / APP_DIR_NAME / SETTINGS_FILE_NAME


def load_settings() -> dict[str, Any]:
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
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
