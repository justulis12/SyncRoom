from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Callable


MPV_RELEASE_API = "https://api.github.com/repos/shinchiro/mpv-winbuild-cmake/releases/latest"
SEVEN_ZIP_RELEASE_API = "https://api.github.com/repos/ip7z/7zip/releases/latest"
APP_DIR_NAME = "SyncRoom"
RUNTIME_DIR_NAME = "mpv-runtime"
ProgressCallback = Callable[[str, int], None]


def windows_runtime_root() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return base / APP_DIR_NAME / RUNTIME_DIR_NAME


def windows_runtime_mpv_path() -> Path:
    return windows_runtime_root() / "mpv.exe"


def windows_mpv_available() -> bool:
    return windows_runtime_mpv_path().exists() or shutil.which("mpv") is not None


def ensure_windows_mpv_runtime(progress: ProgressCallback | None = None) -> Path:
    mpv_path = windows_runtime_mpv_path()
    if mpv_path.exists():
        _notify(progress, "Player already installed.", 100)
        return mpv_path

    runtime_dir = mpv_path.parent
    staging_dir = runtime_dir.with_name(f"{runtime_dir.name}-staging")

    try:
        with tempfile.TemporaryDirectory(
            prefix="syncroom-mpv-",
            ignore_cleanup_errors=True,
        ) as temp_dir:
            temp_root = Path(temp_dir)
            _notify(progress, "Checking latest mpv release...", 5)
            asset = _pick_asset(_load_latest_release())
            archive_path = temp_root / asset["name"]
            extract_dir = temp_root / "extract"
            extract_dir.mkdir(parents=True, exist_ok=True)

            _notify(progress, "Downloading mpv...", 10)
            _download_file(asset["browser_download_url"], archive_path, progress)
            extractor_path = temp_root / "7zr.exe"
            _notify(progress, "Downloading extractor...", 72)
            _download_file(_load_7zr_download_url(), extractor_path)

            _notify(progress, "Extracting player files...", 78)
            _extract_archive(extractor_path, archive_path, extract_dir)

            _notify(progress, "Preparing runtime...", 88)
            source_dir = _find_runtime_source(extract_dir)
            _commit_runtime(source_dir, runtime_dir, staging_dir)
    except OSError as exc:
        if mpv_path.exists():
            _notify(progress, "Player installed.", 100)
            return mpv_path
        raise RuntimeError(f"Could not finish installing mpv: {exc}") from exc

    if not mpv_path.exists():
        raise RuntimeError("mpv download completed, but mpv.exe was not found.")
    _notify(progress, "Player installed.", 100)
    return mpv_path


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
                percent = 10 + int(ratio * 60)
                _notify(progress, f"Downloading mpv... {int(ratio * 100)}%", percent)
        if total <= 0:
            _notify(progress, "Downloading mpv...", 70)


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


def _notify(progress: ProgressCallback | None, message: str, percent: int) -> None:
    if progress is not None:
        progress(message, max(0, min(100, percent)))
