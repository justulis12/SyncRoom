from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from syncroom.utils.logging import append_runtime_log


MPV_RELEASE_API = "https://api.github.com/repos/shinchiro/mpv-winbuild-cmake/releases/latest"
SEVEN_ZIP_RELEASE_API = "https://api.github.com/repos/ip7z/7zip/releases/latest"
YT_DLP_DOWNLOAD_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
APP_DIR_NAME = "SyncRoom"
RUNTIME_DIR_NAME = "mpv-runtime"
ProgressCallback = Callable[[str, int], None]


@dataclass(frozen=True)
class WindowsMediaRuntimeResult:
    mpv_path: Path
    yt_dlp_path: Path | None = None
    yt_dlp_error: str = ""


def windows_runtime_root() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return base / APP_DIR_NAME / RUNTIME_DIR_NAME


def windows_runtime_mpv_path() -> Path:
    return windows_runtime_root() / "mpv.exe"


def windows_runtime_yt_dlp_path() -> Path:
    return windows_runtime_root() / "yt-dlp.exe"


def windows_mpv_available() -> bool:
    return windows_runtime_mpv_path().exists() or shutil.which("mpv") is not None


def windows_yt_dlp_available() -> bool:
    return windows_runtime_yt_dlp_path().exists() or shutil.which("yt-dlp") is not None


def ensure_windows_media_runtime(progress: ProgressCallback | None = None) -> WindowsMediaRuntimeResult:
    _log("Checking Windows media runtime")
    _notify(progress, "Checking mpv...", 5)
    if windows_mpv_available():
        mpv_path = windows_runtime_mpv_path()
        if not mpv_path.exists():
            mpv_path = Path(shutil.which("mpv") or "mpv")
        _notify(progress, "mpv already installed.", 20)
        _log(f"mpv already available at {mpv_path}")
    else:
        mpv_path = ensure_windows_mpv_runtime(progress)

    yt_dlp_path: Path | None = None
    yt_dlp_error = ""
    if windows_yt_dlp_available():
        cached_yt_dlp = windows_runtime_yt_dlp_path()
        yt_dlp_path = cached_yt_dlp if cached_yt_dlp.exists() else Path(shutil.which("yt-dlp") or "yt-dlp")
        _notify(progress, "yt-dlp already installed.", 95)
        _log(f"yt-dlp already available at {yt_dlp_path}")
    else:
        try:
            yt_dlp_path = ensure_windows_yt_dlp_runtime(progress)
        except Exception as exc:
            yt_dlp_error = str(exc)
            _log(f"yt-dlp runtime setup failed: {yt_dlp_error}")
            _notify(progress, "mpv installed. yt-dlp unavailable.", 100)
            return WindowsMediaRuntimeResult(
                mpv_path=mpv_path,
                yt_dlp_path=None,
                yt_dlp_error=yt_dlp_error,
            )

    _notify(progress, "Media tools installed.", 100)
    _log("Windows media runtime setup finished")
    return WindowsMediaRuntimeResult(
        mpv_path=mpv_path,
        yt_dlp_path=yt_dlp_path,
        yt_dlp_error=yt_dlp_error,
    )


def ensure_windows_mpv_runtime(progress: ProgressCallback | None = None) -> Path:
    mpv_path = windows_runtime_mpv_path()
    if mpv_path.exists():
        _notify(progress, "mpv already installed.", 20)
        _log(f"mpv runtime cache already exists at {mpv_path}")
        return mpv_path

    runtime_dir = mpv_path.parent
    staging_dir = runtime_dir.with_name(f"{runtime_dir.name}-staging")
    _log(f"Installing mpv runtime to {runtime_dir}")

    try:
        with tempfile.TemporaryDirectory(
            prefix="syncroom-mpv-",
            ignore_cleanup_errors=True,
        ) as temp_dir:
            temp_root = Path(temp_dir)
            _notify(progress, "Checking mpv...", 5)
            asset = _pick_asset(_load_latest_release())
            archive_path = temp_root / asset["name"]
            extract_dir = temp_root / "extract"
            extract_dir.mkdir(parents=True, exist_ok=True)

            _notify(progress, "Downloading mpv...", 10)
            _download_file(
                asset["browser_download_url"],
                archive_path,
                progress,
                label="Downloading mpv...",
                start_percent=10,
                end_percent=70,
            )
            extractor_path = temp_root / "7zr.exe"
            _notify(progress, "Downloading extractor...", 72)
            _download_file(
                _load_7zr_download_url(),
                extractor_path,
                label="Downloading extractor...",
                start_percent=72,
                end_percent=76,
            )

            _notify(progress, "Extracting mpv...", 78)
            _extract_archive(extractor_path, archive_path, extract_dir)

            _notify(progress, "Preparing runtime...", 88)
            source_dir = _find_runtime_source(extract_dir)
            _commit_runtime(source_dir, runtime_dir, staging_dir)
    except OSError as exc:
        if mpv_path.exists():
            _notify(progress, "mpv installed.", 90)
            _log(f"mpv runtime recovered at {mpv_path} after setup error: {exc}")
            return mpv_path
        _log(f"mpv runtime setup failed: {exc}")
        raise RuntimeError(f"Could not finish installing mpv: {exc}") from exc

    if not mpv_path.exists():
        _log("mpv runtime setup finished without mpv.exe")
        raise RuntimeError("mpv download completed, but mpv.exe was not found.")
    _notify(progress, "mpv installed.", 90)
    _log(f"mpv runtime installed at {mpv_path}")
    return mpv_path


def ensure_windows_yt_dlp_runtime(progress: ProgressCallback | None = None) -> Path:
    yt_dlp_path = windows_runtime_yt_dlp_path()
    if yt_dlp_path.exists():
        _notify(progress, "yt-dlp already installed.", 95)
        _log(f"yt-dlp runtime cache already exists at {yt_dlp_path}")
        return yt_dlp_path

    discovered = shutil.which("yt-dlp")
    if discovered:
        _notify(progress, "yt-dlp already installed.", 95)
        _log(f"yt-dlp available on PATH at {discovered}")
        return Path(discovered)

    runtime_dir = yt_dlp_path.parent
    part_path = yt_dlp_path.with_name(f"{yt_dlp_path.name}.part")
    runtime_dir.mkdir(parents=True, exist_ok=True)
    _safe_unlink(part_path)
    _log(f"Installing yt-dlp runtime to {yt_dlp_path}")
    _notify(progress, "Downloading yt-dlp...", 90)

    try:
        _download_file(
            YT_DLP_DOWNLOAD_URL,
            part_path,
            progress,
            label="Downloading yt-dlp...",
            start_percent=90,
            end_percent=98,
        )
        part_path.replace(yt_dlp_path)
    except Exception as exc:
        _safe_unlink(part_path)
        _log(f"yt-dlp download failed: {exc}")
        raise RuntimeError(f"Could not install yt-dlp: {exc}") from exc

    if not yt_dlp_path.exists():
        raise RuntimeError("yt-dlp download completed, but yt-dlp.exe was not found.")
    _notify(progress, "yt-dlp installed.", 99)
    _log(f"yt-dlp runtime installed at {yt_dlp_path}")
    return yt_dlp_path


def _load_latest_release() -> dict:
    request = urllib.request.Request(
        MPV_RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "SyncRoom",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def _pick_asset(payload: dict) -> dict[str, str]:
    fallback: dict[str, str] | None = None
    for asset in payload.get("assets", []):
        name = str(asset.get("name") or "")
        if not name.endswith(".7z"):
            continue
        if name.startswith("mpv-x86_64-") and "-v3-" not in name:
            return {
                "name": name,
                "browser_download_url": str(asset.get("browser_download_url") or ""),
            }
        if fallback is None and name.startswith("mpv-x86_64-v3-"):
            fallback = {
                "name": name,
                "browser_download_url": str(asset.get("browser_download_url") or ""),
            }
    if fallback is not None:
        return fallback
    raise RuntimeError("Could not find a compatible Windows mpv build.")


def _load_7zr_download_url() -> str:
    request = urllib.request.Request(
        SEVEN_ZIP_RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "SyncRoom",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.load(response)
    for asset in payload.get("assets", []):
        if str(asset.get("name") or "").lower() == "7zr.exe":
            return str(asset.get("browser_download_url") or "")
    raise RuntimeError("Could not find 7zr.exe for Windows extraction.")


def _download_file(
    url: str,
    destination: Path,
    progress: ProgressCallback | None = None,
    label: str = "Downloading mpv...",
    start_percent: int = 10,
    end_percent: int = 70,
) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "SyncRoom"})
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
                percent = start_percent + int(ratio * max(0, end_percent - start_percent))
                _notify(progress, f"{label} {int(ratio * 100)}%", percent)
        if total <= 0:
            _notify(progress, label, end_percent)


def _find_runtime_source(root: Path) -> Path:
    for path in root.rglob("mpv.exe"):
        return path.parent
    raise RuntimeError("Could not find mpv.exe in the downloaded archive.")


def _extract_archive(extractor_path: Path, archive_path: Path, extract_dir: Path) -> None:
    result = subprocess.run(
        [
            str(extractor_path),
            "x",
            str(archive_path),
            f"-o{extract_dir}",
            "-y",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        details = (result.stderr or "").strip()
        raise RuntimeError(f"Could not extract mpv archive with 7zr.exe. {details}".strip())


def _commit_runtime(source_dir: Path, runtime_dir: Path, staging_dir: Path) -> None:
    _safe_rmtree(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)
    _copy_tree(source_dir, staging_dir)
    _preserve_cached_runtime_tools(runtime_dir, staging_dir)

    runtime_dir.parent.mkdir(parents=True, exist_ok=True)
    if runtime_dir.exists():
        try:
            _safe_rmtree(runtime_dir)
        except OSError:
            _merge_tree(staging_dir, runtime_dir)
            if windows_runtime_mpv_path().exists():
                _safe_rmtree(staging_dir, strict=False)
                return
            raise

    try:
        staging_dir.replace(runtime_dir)
    except OSError:
        _merge_tree(staging_dir, runtime_dir)
        if windows_runtime_mpv_path().exists():
            _safe_rmtree(staging_dir, strict=False)
            return
        raise


def _copy_tree(source_dir: Path, target_dir: Path) -> None:
    for item in source_dir.iterdir():
        target = target_dir / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            _retry_file_copy(item, target)


def _merge_tree(source_dir: Path, target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    for item in source_dir.iterdir():
        target = target_dir / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            _retry_file_copy(item, target)


def _preserve_cached_runtime_tools(runtime_dir: Path, staging_dir: Path) -> None:
    for filename in ("yt-dlp.exe",):
        cached_file = runtime_dir / filename
        if cached_file.exists():
            _retry_file_copy(cached_file, staging_dir / filename)


def _retry_file_copy(source: Path, target: Path, attempts: int = 8) -> None:
    last_error: OSError | None = None
    for _ in range(attempts):
        try:
            shutil.copy2(source, target)
            return
        except OSError as exc:
            last_error = exc
            time.sleep(0.25)
    if last_error is not None:
        raise last_error


def _safe_rmtree(path: Path, strict: bool = True, attempts: int = 8) -> None:
    if not path.exists():
        return
    last_error: OSError | None = None
    for _ in range(attempts):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            last_error = exc
            time.sleep(0.25)
    if strict and last_error is not None:
        raise last_error


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        _log(f"Could not remove temporary file {path}: {exc}")


def _notify(progress: ProgressCallback | None, message: str, percent: int) -> None:
    if progress is not None:
        progress(message, max(0, min(100, percent)))


def _log(message: str) -> None:
    append_runtime_log(message)
