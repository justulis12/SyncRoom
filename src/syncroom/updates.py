from __future__ import annotations

import json
import shutil
import socket
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from syncroom import __repo__, __version__
from syncroom.settings import logs_dir


LATEST_RELEASE_API = f"https://api.github.com/repos/{__repo__}/releases/latest"
LATEST_RELEASE_PAGE = f"https://github.com/{__repo__}/releases/latest"
ProgressCallback = Callable[[str, int], None]
MAX_LOG_BYTES = 256 * 1024
DOWNLOAD_ATTEMPTS = 3
DOWNLOAD_BACKOFF_SECONDS = (1, 3, 6)
RETRY_HTTP_STATUS_CODES = {429, 502, 503, 504}


@dataclass
class UpdateInfo:
    available: bool
    latest_version: str = ""
    download_url: str = LATEST_RELEASE_PAGE
    asset_name: str = ""
    asset_url: str = ""
    message: str = ""


def update_log_path() -> Path:
    return logs_dir() / "update.log"


def append_update_log(message: str) -> None:
    path = update_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
            path.write_text("", encoding="utf-8")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except Exception:
        return


def check_for_updates(timeout: float = 3.0) -> UpdateInfo:
    append_update_log(f"Checking for updates via {LATEST_RELEASE_API}")
    try:
        payload = load_latest_release(timeout=timeout)
    except Exception as exc:
        append_update_log(f"Update check failed: {exc}")
        return UpdateInfo(False, message=f"Could not check for updates: {exc}")

    tag_name = str(payload.get("tag_name") or "").strip()
    if not tag_name:
        return UpdateInfo(False, message="No published releases found yet.")

    latest = _normalize_version(tag_name)
    current = _normalize_version(__version__)
    html_url = str(payload.get("html_url") or LATEST_RELEASE_PAGE)
    asset_name, asset_url = _select_windows_installer_asset(payload)
    if _version_key(latest) > _version_key(current):
        append_update_log(
            f"Newer release detected latest={latest} current={current} asset={asset_name or '<none>'}"
        )
        return UpdateInfo(
            True,
            latest_version=latest,
            download_url=html_url,
            asset_name=asset_name,
            asset_url=asset_url,
            message=(
                ""
                if asset_name and asset_url
                else "A newer release exists, but SyncRoom-Setup.exe was not attached to it."
            ),
        )
    append_update_log(f"No update needed latest={latest} current={current}")
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
        append_update_log(f"Latest release request succeeded status={getattr(response, 'status', 'unknown')}")
        return json.load(response)


def download_update_asset(
    info: UpdateInfo,
    progress: ProgressCallback | None = None,
) -> Path:
    if not info.asset_url or not info.asset_name:
        raise RuntimeError("No downloadable installer was attached to the latest release.")
    if info.asset_name.lower() != "syncroom-setup.exe":
        raise RuntimeError(
            f"Unexpected update asset selected: {info.asset_name}. Expected SyncRoom-Setup.exe."
        )

    temp_dir = Path(tempfile.mkdtemp(prefix="syncroom-update-"))
    destination = temp_dir / info.asset_name
    partial_path = destination.with_suffix(destination.suffix + ".part")
    append_update_log(
        f"Starting installer download asset={info.asset_name} url={info.asset_url} temp_dir={temp_dir}"
    )
    _notify(progress, "Preparing update download...", 0)

    last_error = ""
    try:
        for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
            if partial_path.exists():
                partial_path.unlink(missing_ok=True)
            append_update_log(
                "Installer download attempt "
                f"attempt={attempt}/{DOWNLOAD_ATTEMPTS} url={info.asset_url} temp_path={partial_path}"
            )
            try:
                downloaded = _download_update_once(
                    info.asset_url,
                    partial_path,
                    progress,
                    attempt,
                )
                partial_size = partial_path.stat().st_size if partial_path.exists() else 0
                append_update_log(
                    "Installer download attempt completed "
                    f"attempt={attempt}/{DOWNLOAD_ATTEMPTS} downloaded_bytes={downloaded} "
                    f"temp_path={partial_path} size={partial_size}"
                )
                if partial_size <= 0:
                    raise RuntimeError("The downloaded installer was empty.")

                partial_path.replace(destination)
                if not destination.exists() or destination.stat().st_size <= 0:
                    raise RuntimeError("The downloaded installer was not saved correctly.")
                append_update_log(
                    f"Installer download completed path={destination} size={destination.stat().st_size}"
                )
                return destination
            except Exception as exc:
                last_error = _format_download_error(exc)
                downloaded_bytes = partial_path.stat().st_size if partial_path.exists() else 0
                append_update_log(
                    "Installer download attempt failed "
                    f"attempt={attempt}/{DOWNLOAD_ATTEMPTS} url={info.asset_url} "
                    f"error={last_error} downloaded_bytes={downloaded_bytes} temp_path={partial_path}"
                )
                partial_path.unlink(missing_ok=True)
                if attempt >= DOWNLOAD_ATTEMPTS or not _is_retryable_download_error(exc):
                    break
                delay = DOWNLOAD_BACKOFF_SECONDS[min(attempt - 1, len(DOWNLOAD_BACKOFF_SECONDS) - 1)]
                _notify(progress, f"Download failed; retrying in {delay}s...", 0)
                append_update_log(
                    f"Retrying installer download after {delay}s attempt={attempt + 1}/{DOWNLOAD_ATTEMPTS}"
                )
                time.sleep(delay)

        message = (
            "Could not download the update installer after several attempts. "
            "Please try again in a few minutes."
        )
        if last_error:
            message = f"{message} Last error: {last_error}"
        append_update_log(f"Installer download failed after retries: {last_error or '<unknown>'}")
        raise RuntimeError(message)
    finally:
        if partial_path.exists():
            partial_path.unlink(missing_ok=True)
        if not destination.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)


def _download_update_once(
    url: str,
    partial_path: Path,
    progress: ProgressCallback | None,
    attempt: int,
) -> int:
    request = urllib.request.Request(url, headers={"User-Agent": "SyncRoom"})
    with urllib.request.urlopen(request, timeout=60) as response, partial_path.open("wb") as handle:
        status_code = int(getattr(response, "status", 200) or 200)
        append_update_log(f"Installer download response attempt={attempt} status={status_code}")
        if status_code >= 400:
            raise RuntimeError(f"Update download returned HTTP {status_code}.")

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

        append_update_log(
            "Installer download stream ended "
            f"attempt={attempt} status={status_code} downloaded_bytes={downloaded} "
            f"expected_bytes={total} temp_path={partial_path}"
        )
        if downloaded <= 0:
            raise RuntimeError("The downloaded installer was empty.")
        if total > 0 and downloaded != total:
            raise RuntimeError(
                f"Downloaded size mismatch: expected {total} bytes, got {downloaded} bytes."
            )
        if total <= 0:
            _notify(progress, "Downloading update...", 100)
        return downloaded


def _is_retryable_download_error(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return int(exc.code) in RETRY_HTTP_STATUS_CODES
    if isinstance(exc, urllib.error.URLError):
        return True
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    message = str(exc).lower()
    return "timed out" in message or "temporarily unavailable" in message


def _format_download_error(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code} {exc.reason}"
    if isinstance(exc, urllib.error.URLError):
        return str(exc.reason)
    return str(exc)


def cleanup_update_download(path: Path | None) -> None:
    if path is None:
        return
    append_update_log(f"Cleaning up downloaded installer at {path}")
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
            append_update_log(f"Selected Windows installer asset {name}")
            return name, str(asset.get("browser_download_url") or "")
    append_update_log("No SyncRoom-Setup.exe asset was found in the latest release")
    return "", ""


def _notify(progress: ProgressCallback | None, message: str, percent: int) -> None:
    if progress is not None:
        progress(message, max(0, min(100, percent)))
