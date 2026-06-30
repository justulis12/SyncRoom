from __future__ import annotations

import sys
from pathlib import Path


def project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def resource_path(*parts: str) -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(str(frozen_root)).joinpath(*parts)
    return project_root().joinpath(*parts)


def app_icon_path() -> Path:
    return resource_path("assets", "syncroom.png")
