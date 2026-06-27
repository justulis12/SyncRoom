from __future__ import annotations

import json
import shutil
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from syncroom import __repo__, __version__


LATEST_RELEASE_API = f"https://api.github.com/repos/{__repo__}/releases/latest"
LATEST_RELEASE_PAGE = f"https://github.com/{__repo__}/releases/latest"
ProgressCallback = Callable[[str, int], None]


@dataclass
class UpdateInfo:
    available: bool
    latest_version: str = ""
    download_url: str = LATEST_RELEASE_PAGE
    asset_name: str = ""
    asset_url: str = ""
    message: str = ""


def check_for_updates(timeout: float = 3.0) -> UpdateInfo:
    try:
        payload = load_latest_release(timeout=timeout)
    except Exception as exc:
        return UpdateInfo(False, message=f"Could not check for updates: {exc}")

    tag_name = str(payload.get("tag_name") or "").strip()
    if not tag_name:
        return UpdateInfo(False, message="No published releases found yet.")

    latest = _normalize_version(tag_name)
    current = _normalize_version(__version__)
    html_url = str(payload.get("html_url") or LATEST_RELEASE_PAGE)
    asset_name, asset_url = _select_windows_installer_asset(payload)
    if _version_key(latest) > _version_key(current):
        return UpdateInfo(
            True,
            latest_version=latest,
            download_url=html_url,
            asset_name=asset_name,
            asset_url=asset_url,
        )
    return UpdateInfo(
        False,
        latest_version=latest,
        download_url=html_url,
        asset_name=asset_name,
        asset_url=asset_url,
        message="You are up to date.",
    )


def load_latest_release(timeout: float = 3.0) -> dict:
    request = urllib.request.Request(
        LATEST_RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "SyncRoom",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def download_update_asset(
    info: UpdateInfo,
    progress: ProgressCallback | None = None,
) -> Path:
    if not info.asset_url or not info.asset_name:
        raise RuntimeError("No downloadable installer was attached to the latest release.")

    temp_dir = Path(tempfile.mkdtemp(prefix="syncroom-update-"))
    destination = temp_dir / info.asset_name
    request = urllib.request.Request(info.asset_url, headers={"User-Agent": "SyncRoom"})
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as handle:
        total = int(response.headers.get("Content-Length") or "0")
        downloaded = 0
        while True:
            chunk = response.read(1024 * 128)
            if not chunk:
                break
            handle.write(chunk)
            downloaded += len(chunk)
            if total > 0:
                ratio = min(downloaded / total, 1.0)
                _notify(progress, f"Downloading update... {int(ratio * 100)}%", int(ratio * 100))
        if total <= 0:
            _notify(progress, "Downloading update...", 100)
    return destination


def cleanup_update_download(path: Path | None) -> None:
    if path is None:
        return
    shutil.rmtree(path.parent, ignore_errors=True)


def _normalize_version(value: str) -> str:
    return value.lower().removeprefix("v").strip()


def _version_key(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in value.split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits or "0"))
    return tuple(parts)


def _select_windows_installer_asset(payload: dict) -> tuple[str, str]:
    for asset in payload.get("assets", []):
        name = str(asset.get("name") or "")
        if name.lower() == "syncroom-setup.exe":
            return name, str(asset.get("browser_download_url") or "")
    return "", ""


def _notify(progress: ProgressCallback | None, message: str, percent: int) -> None:
    if progress is not None:
        progress(message, max(0, min(100, percent)))
