from __future__ import annotations

import argparse
import csv
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Callable


WAIT_TIMEOUT_SECONDS = 180
INSTALLER_TIMEOUT_SECONDS = 600
INSTALLER_TIMEOUT_EXIT_CODE = 124
SUCCESS_INSTALLER_EXIT_CODES = {0, 3010}
DOWNLOAD_ATTEMPTS = 3
DOWNLOAD_BACKOFF_SECONDS = (1, 3, 6)
RETRY_HTTP_STATUS_CODES = {429, 502, 503, 504}
EXPECTED_ASSET_NAME = "syncroom-setup.exe"
ProgressCallback = Callable[[str, str, int], None]


@dataclass(frozen=True)
class ApplyUpdateRequest:
    version: str
    asset_url: str
    asset_name: str
    app_path: Path
    pid: int


def logs_dir() -> Path:
    candidates = []
    if os.name == "nt":
        candidates.append(Path(os.environ.get("APPDATA", Path.home())) / "syncroom" / "logs")
    else:
        candidates.append(Path.home() / ".config" / "syncroom" / "logs")
    candidates.append(Path(tempfile.gettempdir()) / "syncroom-logs")

    for path in candidates:
        try:
            path.mkdir(parents=True, exist_ok=True)
            return path
        except Exception:
            continue
    raise RuntimeError("Could not create a writable logs directory.")


def log_path() -> Path:
    return logs_dir() / "update-helper.log"


def installer_log_path() -> Path:
    return logs_dir() / "installer.log"


def append_log(message: str) -> None:
    try:
        path = log_path()
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
            handle.flush()
    except Exception:
        pass


def progress_noop(_step: str, _detail: str, _percent: int) -> None:
    return


def emit_progress(progress: ProgressCallback | None, step: str, detail: str, percent: int) -> None:
    append_log(f"progress step={step!r} detail={detail!r} percent={percent}")
    if progress is not None:
        progress(step, detail, max(0, min(100, percent)))


def spawn_detached(arguments: list[str], cwd: Path) -> subprocess.Popen:
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    append_log(f"spawn detached command={subprocess.list2cmdline(arguments)} cwd={cwd}")
    return subprocess.Popen(arguments, cwd=str(cwd), close_fds=True, creationflags=creationflags)


def stage_updater_runtime(current_exe: Path) -> tuple[Path, Path]:
    temp_dir = Path(tempfile.mkdtemp(prefix="syncroom-update-run-"))
    staged_exe = temp_dir / current_exe.name
    append_log(f"staging temp directory temp_dir={temp_dir} source_exe={current_exe}")
    shutil.copy2(current_exe, staged_exe)

    internal_dir = current_exe.parent / "_internal"
    append_log(f"staging _internal check path={internal_dir} exists={internal_dir.is_dir()}")
    if internal_dir.is_dir():
        shutil.copytree(internal_dir, temp_dir / "_internal", dirs_exist_ok=True)

    append_log(f"staging complete temp_dir={temp_dir} exe={staged_exe}")
    return temp_dir, staged_exe


def normalized_path(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def pid_exists_fallback(pid: int) -> bool:
    result = subprocess.run(
        ["cmd", "/c", f'tasklist /FI "PID eq {pid}" 2>NUL'],
        capture_output=True,
        text=True,
        check=False,
    )
    return str(pid) in result.stdout


def wait_for_exit_with_tasklist_fallback(pid: int, timeout_seconds: int) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not pid_exists_fallback(pid):
            append_log(f"target PID exited according to fallback pid={pid}")
            return True
        time.sleep(0.5)
    append_log(f"target PID fallback wait timed out pid={pid}")
    return False


def wait_for_exit(pid: int, timeout_seconds: int = WAIT_TIMEOUT_SECONDS) -> bool:
    append_log(f"target PID wait start pid={pid} timeout_seconds={timeout_seconds}")
    if os.name != "nt":
        append_log("target PID wait skipped on non-Windows platform")
        return True

    try:
        import ctypes
        from ctypes import wintypes

        SYNCHRONIZE = 0x00100000
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        WAIT_OBJECT_0 = 0
        WAIT_TIMEOUT = 0x00000102
        ERROR_INVALID_PARAMETER = 87

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.OpenProcess(
            SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            int(pid),
        )
        if not handle:
            error = ctypes.get_last_error()
            append_log(f"OpenProcess failed for target PID pid={pid} winerror={error}")
            if error == ERROR_INVALID_PARAMETER:
                append_log(f"target PID already exited pid={pid}")
                return True
            return wait_for_exit_with_tasklist_fallback(pid, timeout_seconds)

        try:
            result = kernel32.WaitForSingleObject(handle, int(timeout_seconds * 1000))
            if result == WAIT_OBJECT_0:
                append_log(f"target PID exited pid={pid}")
                return True
            if result == WAIT_TIMEOUT:
                append_log(f"target PID wait timed out pid={pid}")
                return False
            append_log(f"target PID wait failed pid={pid} wait_result={result}")
            return False
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        append_log("Windows process wait failed; falling back to tasklist polling")
        append_log(traceback.format_exc().strip())
        return wait_for_exit_with_tasklist_fallback(pid, timeout_seconds)


def remaining_syncroom_processes(app_path: Path) -> list[tuple[int, str]]:
    if os.name != "nt":
        return []
    command = [
        "powershell",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        (
            "Get-CimInstance Win32_Process -Filter \"Name = 'SyncRoom.exe'\" | "
            "Select-Object ProcessId,ExecutablePath | ConvertTo-Csv -NoTypeInformation"
        ),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except Exception:
        append_log("remaining SyncRoom.exe check failed")
        append_log(traceback.format_exc().strip())
        return []

    if result.returncode != 0:
        append_log(
            "remaining SyncRoom.exe check returned nonzero "
            f"exit_code={result.returncode} stderr={result.stderr.strip()}"
        )
        return []

    install_dir = normalized_path(app_path.parent)
    remaining: list[tuple[int, str]] = []
    for row in csv.DictReader(StringIO(result.stdout)):
        executable = str(row.get("ExecutablePath") or "").strip()
        process_id = str(row.get("ProcessId") or "").strip()
        if not executable or not process_id:
            continue
        try:
            if normalized_path(Path(executable).parent) == install_dir:
                remaining.append((int(process_id), executable))
        except Exception:
            continue
    append_log(f"remaining SyncRoom.exe check install_dir={app_path.parent} matches={remaining!r}")
    return remaining


def is_running_as_admin() -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        append_log("admin status check failed")
        append_log(traceback.format_exc().strip())
        return False


def program_files_roots() -> list[Path]:
    roots: list[Path] = []
    for key in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        value = os.environ.get(key)
        if value:
            roots.append(Path(value))
    return roots


def path_is_under_program_files(path: Path) -> bool:
    candidate = normalized_path(path)
    for root in program_files_roots():
        root_text = normalized_path(root)
        if candidate == root_text or candidate.startswith(root_text + os.sep):
            return True
    return False


def installer_arguments(installer_log: Path) -> list[str]:
    return [
        "/SP-",
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NOCANCEL",
        "/CLOSEAPPLICATIONS",
        "/NORESTARTAPPLICATIONS",
        f"/LOG={installer_log}",
    ]


def run_installer(
    installer_path: Path,
    installer_log: Path,
    app_path: Path,
    progress: ProgressCallback | None = None,
) -> int:
    args = installer_arguments(installer_log)
    command = [str(installer_path), *args]
    needs_admin = path_is_under_program_files(app_path)
    is_admin = is_running_as_admin()
    append_log(
        "installer launch decision "
        f"needs_admin={needs_admin} is_admin={is_admin} app_path={app_path} "
        f"under_program_files={path_is_under_program_files(app_path)}"
    )
    append_log(f"installer command {subprocess.list2cmdline(command)}")
    if needs_admin and not is_admin:
        emit_progress(progress, "Installing update", "Waiting for administrator permission...", 72)
        append_log("installer elevation requested via ShellExecuteEx runas")
        return run_installer_elevated(installer_path, args)
    append_log("installer launch starting without elevation")
    return run_installer_normal(command, installer_path.parent)


def run_installer_normal(command: list[str], cwd: Path) -> int:
    process = subprocess.Popen(command, cwd=str(cwd))
    append_log(f"installer process started pid={process.pid}")
    try:
        exit_code = process.wait(timeout=INSTALLER_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        append_log(f"installer execution timed out after {INSTALLER_TIMEOUT_SECONDS}s")
        return INSTALLER_TIMEOUT_EXIT_CODE
    append_log(f"installer process exited pid={process.pid} exit_code={exit_code}")
    return int(exit_code)


def run_installer_elevated(installer_path: Path, args: list[str]) -> int:
    import ctypes
    from ctypes import wintypes

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SW_SHOWNORMAL = 1
    WAIT_OBJECT_0 = 0
    WAIT_TIMEOUT = 0x00000102
    ERROR_CANCELLED = 1223
    STILL_ACTIVE = 259

    class ShellExecuteInfo(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", wintypes.ULONG),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", ctypes.c_void_p),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIcon", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(ShellExecuteInfo)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    info = ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(ShellExecuteInfo)
    info.fMask = SEE_MASK_NOCLOSEPROCESS
    info.hwnd = None
    info.lpVerb = "runas"
    info.lpFile = str(installer_path)
    info.lpParameters = subprocess.list2cmdline(args)
    info.lpDirectory = str(installer_path.parent)
    info.nShow = SW_SHOWNORMAL

    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        error = ctypes.get_last_error()
        if error == ERROR_CANCELLED:
            append_log("installer elevation canceled by user")
            return ERROR_CANCELLED
        append_log(f"installer elevation failed winerror={error}")
        raise OSError(error, f"ShellExecuteExW failed with WinError {error}")

    append_log(f"elevated installer process started handle={int(info.hProcess or 0)}")
    try:
        result = kernel32.WaitForSingleObject(info.hProcess, int(INSTALLER_TIMEOUT_SECONDS * 1000))
        if result == WAIT_TIMEOUT:
            append_log(f"elevated installer execution timed out after {INSTALLER_TIMEOUT_SECONDS}s")
            return INSTALLER_TIMEOUT_EXIT_CODE
        if result != WAIT_OBJECT_0:
            append_log(f"elevated installer wait failed wait_result={result}")
            return 1

        exit_code = wintypes.DWORD(STILL_ACTIVE)
        if not kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(exit_code)):
            error = ctypes.get_last_error()
            append_log(f"GetExitCodeProcess failed winerror={error}")
            return 1
        append_log(f"elevated installer process exited exit_code={int(exit_code.value)}")
        return int(exit_code.value)
    finally:
        if info.hProcess:
            kernel32.CloseHandle(info.hProcess)


def safe_cleanup_path(path: Path, description: str) -> None:
    append_log(f"cleanup attempt description={description} path={path}")
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink(missing_ok=True)
    except Exception:
        append_log(traceback.format_exc().strip())


def maybe_cleanup_update_temp(path: Path) -> None:
    if path.name.startswith(("syncroom-update-", "syncroom-update-run-", "syncroom-update-download-")):
        safe_cleanup_path(path, "temporary update directory")


def _format_download_error(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code} {exc.reason}"
    if isinstance(exc, urllib.error.URLError):
        return str(exc.reason)
    return str(exc)


def _is_retryable_download_error(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return int(exc.code) in RETRY_HTTP_STATUS_CODES
    if isinstance(exc, urllib.error.URLError):
        return True
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    message = str(exc).lower()
    return "timed out" in message or "temporarily unavailable" in message


def download_installer_asset(
    asset_url: str,
    asset_name: str,
    progress: ProgressCallback | None = None,
) -> Path:
    if asset_name.lower() != EXPECTED_ASSET_NAME:
        raise RuntimeError(f"Unexpected update asset: {asset_name}. Expected SyncRoom-Setup.exe.")
    if not asset_url:
        raise RuntimeError("No update asset URL was provided.")

    temp_dir = Path(tempfile.mkdtemp(prefix="syncroom-update-download-"))
    destination = temp_dir / "SyncRoom-Setup.exe"
    partial_path = destination.with_suffix(destination.suffix + ".part")
    append_log(f"download staging temp_dir={temp_dir} temp_path={partial_path} final_path={destination}")
    emit_progress(progress, "Downloading update", "Preparing download...", 30)

    last_error = ""
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        expected_bytes = 0
        downloaded_bytes = 0
        partial_path.unlink(missing_ok=True)
        append_log(
            "download attempt start "
            f"attempt={attempt}/{DOWNLOAD_ATTEMPTS} url={asset_url} temp_path={partial_path} "
            f"final_path={destination}"
        )
        try:
            request = urllib.request.Request(asset_url, headers={"User-Agent": "SyncRoomUpdate"})
            with urllib.request.urlopen(request, timeout=60) as response, partial_path.open("wb") as handle:
                status_code = int(getattr(response, "status", 200) or 200)
                expected_bytes = int(response.headers.get("Content-Length") or "0")
                append_log(
                    "download response "
                    f"attempt={attempt}/{DOWNLOAD_ATTEMPTS} status={status_code} expected_bytes={expected_bytes}"
                )
                if status_code >= 400:
                    raise RuntimeError(f"Update download returned HTTP {status_code}.")

                while True:
                    chunk = response.read(1024 * 128)
                    if not chunk:
                        break
                    handle.write(chunk)
                    downloaded_bytes += len(chunk)
                    if expected_bytes > 0:
                        ratio = min(downloaded_bytes / expected_bytes, 1.0)
                        percent = 30 + int(ratio * 35)
                        emit_progress(
                            progress,
                            "Downloading update",
                            f"Downloading SyncRoom-Setup.exe ({int(ratio * 100)}%)...",
                            percent,
                        )

            append_log(
                "download stream ended "
                f"attempt={attempt}/{DOWNLOAD_ATTEMPTS} downloaded_bytes={downloaded_bytes} "
                f"expected_bytes={expected_bytes} temp_path={partial_path} final_path={destination}"
            )
            if downloaded_bytes <= 0:
                raise RuntimeError("The downloaded installer was empty.")
            if expected_bytes > 0 and downloaded_bytes != expected_bytes:
                raise RuntimeError(
                    f"Downloaded size mismatch: expected {expected_bytes} bytes, got {downloaded_bytes} bytes."
                )

            partial_path.replace(destination)
            final_size = destination.stat().st_size if destination.exists() else 0
            if final_size <= 0:
                raise RuntimeError("The downloaded installer was not saved correctly.")
            append_log(
                "download attempt succeeded "
                f"attempt={attempt}/{DOWNLOAD_ATTEMPTS} downloaded_bytes={downloaded_bytes} "
                f"expected_bytes={expected_bytes} final_path={destination} final_size={final_size}"
            )
            emit_progress(progress, "Downloading update", "Download complete.", 65)
            return destination
        except Exception as exc:
            last_error = _format_download_error(exc)
            downloaded_bytes = partial_path.stat().st_size if partial_path.exists() else downloaded_bytes
            append_log(
                "download attempt failed "
                f"attempt={attempt}/{DOWNLOAD_ATTEMPTS} url={asset_url} status_or_error={last_error} "
                f"downloaded_bytes={downloaded_bytes} expected_bytes={expected_bytes} "
                f"temp_path={partial_path} final_path={destination}"
            )
            partial_path.unlink(missing_ok=True)
            if attempt >= DOWNLOAD_ATTEMPTS or not _is_retryable_download_error(exc):
                break
            delay = DOWNLOAD_BACKOFF_SECONDS[min(attempt - 1, len(DOWNLOAD_BACKOFF_SECONDS) - 1)]
            emit_progress(progress, "Downloading update", f"Download failed; retrying in {delay}s...", 32)
            append_log(f"download retry scheduled delay_seconds={delay} next_attempt={attempt + 1}")
            time.sleep(delay)

    safe_cleanup_path(temp_dir, "failed download directory")
    raise RuntimeError(
        "Could not download the update installer after several attempts."
        + (f" Last error: {last_error}" if last_error else "")
    )


def validate_apply_request(request: ApplyUpdateRequest) -> None:
    append_log(
        "apply request "
        f"version={request.version} asset_name={request.asset_name} asset_url={request.asset_url} "
        f"app_path={request.app_path} target_pid={request.pid}"
    )
    if request.asset_name.lower() != EXPECTED_ASSET_NAME:
        raise RuntimeError(f"Unexpected asset name: {request.asset_name}. Expected SyncRoom-Setup.exe.")
    if not request.asset_url:
        raise RuntimeError("No asset URL was provided.")
    if request.pid <= 0:
        raise RuntimeError(f"Invalid target PID: {request.pid}")
    if not request.app_path.exists():
        raise RuntimeError(f"SyncRoom.exe was not found at {request.app_path}")


def apply_update_stage2(request: ApplyUpdateRequest, progress: ProgressCallback | None = None) -> int:
    current_exe = Path(sys.executable).resolve()
    installer_path: Path | None = None
    emit_progress(progress, "Preparing update", f"Installing {request.version}", 8)
    validate_apply_request(request)
    append_log(f"log path={log_path()}")
    append_log(f"installer_log={installer_log_path()}")
    append_log(f"current_executable={current_exe}")
    append_log(f"current_working_directory={Path.cwd()}")

    emit_progress(progress, "Closing SyncRoom", "Waiting for SyncRoom to close...", 18)
    wait_result = wait_for_exit(request.pid)
    append_log(f"target PID wait result pid={request.pid} exited={wait_result}")
    if not wait_result:
        raise RuntimeError("Timed out while waiting for SyncRoom to close.")

    emit_progress(progress, "Closing SyncRoom", "Checking for remaining SyncRoom processes...", 24)
    remaining = remaining_syncroom_processes(request.app_path)
    append_log(f"remaining SyncRoom.exe check result={remaining!r}")
    if remaining:
        raise RuntimeError("SyncRoom.exe is still running from the install directory.")

    installer_path = download_installer_asset(request.asset_url, request.asset_name, progress)
    append_log(
        f"installer ready path={installer_path} size={installer_path.stat().st_size if installer_path.exists() else 0}"
    )

    emit_progress(progress, "Installing update", f"Installing {request.version}...", 70)
    exit_code = run_installer(installer_path, installer_log_path(), request.app_path, progress)
    append_log(f"installer exit code {exit_code}")
    if exit_code == 1223:
        raise RuntimeError("Update canceled because administrator permission was not granted.")
    if exit_code not in SUCCESS_INSTALLER_EXIT_CODES:
        raise RuntimeError(f"Installer failed with exit code {exit_code}.")

    emit_progress(progress, "Relaunching SyncRoom", "Opening SyncRoom...", 92)
    if not request.app_path.exists():
        raise RuntimeError(f"SyncRoom.exe was missing after install: {request.app_path}")
    process = spawn_detached([str(request.app_path)], request.app_path.parent)
    append_log(f"app relaunch requested pid={process.pid} path={request.app_path}")

    emit_progress(progress, "Done", "SyncRoom has been updated.", 100)
    append_log("cleanup starting")
    if installer_path is not None:
        maybe_cleanup_update_temp(installer_path.parent)
    maybe_cleanup_update_temp(current_exe.parent)
    append_log("stage2 apply update complete")
    return 0


def apply_legacy_stage2(installer_path: Path, app_path: Path, pid: int) -> int:
    append_log(
        f"legacy stage2 installer_path={installer_path} app_path={app_path} pid={pid} "
        f"installer_exists={installer_path.exists()}"
    )
    if not installer_path.exists() or installer_path.stat().st_size <= 0:
        append_log("legacy installer path is missing or empty")
        return 1
    if not wait_for_exit(pid):
        append_log("legacy target PID wait failed")
        return 1
    remaining = remaining_syncroom_processes(app_path)
    if remaining:
        append_log(f"legacy remaining SyncRoom.exe processes found: {remaining!r}")
        return 1
    exit_code = run_installer(installer_path, installer_log_path(), app_path)
    if exit_code not in SUCCESS_INSTALLER_EXIT_CODES:
        append_log(f"legacy installer failed exit_code={exit_code}")
        return 1
    if app_path.exists():
        spawn_detached([str(app_path)], app_path.parent)
    maybe_cleanup_update_temp(installer_path.parent)
    maybe_cleanup_update_temp(Path(sys.executable).resolve().parent)
    append_log("legacy stage2 complete")
    return 0


def log_startup(stage: str) -> None:
    current_exe = Path(sys.executable).resolve()
    append_log("SyncRoomUpdate process start")
    append_log(f"argv={sys.argv!r}")
    append_log(f"current_executable={current_exe}")
    append_log(f"current_working_directory={Path.cwd()}")
    append_log(f"stage={stage}")
    append_log(f"frozen={getattr(sys, 'frozen', False)}")
    append_log(f"windows_user_admin={is_running_as_admin()}")


def verify_ui_import() -> bool:
    try:
        from PySide6.QtCore import Qt  # noqa: F401
        from PySide6.QtGui import QGuiApplication  # noqa: F401
        from PySide6.QtWidgets import QApplication  # noqa: F401

        append_log("self-test UI import succeeded")
        return True
    except Exception:
        append_log("self-test UI import failed")
        append_log(traceback.format_exc().strip())
        return False


def self_test() -> int:
    log_startup("self-test")
    current_exe = Path(sys.executable).resolve()
    internal_dir = current_exe.parent / "_internal"
    log_root = logs_dir()
    append_log(f"self-test logs_dir={log_root} writable={log_root.is_dir()}")
    append_log(f"self-test current_exe exists={current_exe.exists()} path={current_exe}")
    append_log(f"self-test _internal exists={internal_dir.is_dir()} path={internal_dir}")
    if not current_exe.exists():
        append_log("self-test failed because current executable is missing")
        return 1
    if getattr(sys, "frozen", False) and not internal_dir.is_dir():
        append_log("self-test failed because bundled _internal directory is missing")
        return 1

    temp_dir: Path | None = None
    try:
        temp_dir, staged_exe = stage_updater_runtime(current_exe)
        append_log(f"self-test staged_exe exists={staged_exe.exists()} path={staged_exe}")
        if not staged_exe.exists():
            append_log("self-test failed because staged executable is missing")
            return 1
        if getattr(sys, "frozen", False) and not (temp_dir / "_internal").is_dir():
            append_log("self-test failed because staged _internal directory is missing")
            return 1
    except Exception:
        append_log("self-test staging failed")
        append_log(traceback.format_exc().strip())
        return 1
    finally:
        if temp_dir is not None:
            safe_cleanup_path(temp_dir, "self-test staged updater")

    if not verify_ui_import():
        return 1

    append_log("self-test completed successfully")
    print(f"SyncRoomUpdate self-test OK; log: {log_path()}")
    return 0


def run_ui_self_test() -> int:
    try:
        return run_with_ui(
            lambda progress: (
                emit_progress(progress, "Preparing update", "UI self-test starting...", 15),
                time.sleep(0.4),
                emit_progress(progress, "Downloading update", "Checking progress display...", 45),
                time.sleep(0.4),
                emit_progress(progress, "Done", "UI self-test complete.", 100),
                0,
            )[-1],
            version="UI self-test",
        )
    except Exception:
        append_log("ui-self-test failed")
        append_log(traceback.format_exc().strip())
        return 1


def run_with_ui(worker_callback: Callable[[ProgressCallback], int], version: str) -> int:
    try:
        from PySide6.QtCore import QObject, QThread, QTimer, Signal
        from PySide6.QtWidgets import (
            QApplication,
            QFrame,
            QHBoxLayout,
            QLabel,
            QProgressBar,
            QPushButton,
            QVBoxLayout,
            QWidget,
        )
    except Exception:
        append_log("UI initialization import failed; continuing headless")
        append_log(traceback.format_exc().strip())
        return worker_callback(progress_noop)

    class Worker(QObject):
        progress = Signal(str, str, int)
        finished = Signal(int)
        failed = Signal(str)

        def run(self) -> None:
            try:
                exit_code = worker_callback(lambda step, detail, percent: self.progress.emit(step, detail, percent))
            except Exception as exc:
                append_log("worker failed")
                append_log(traceback.format_exc().strip())
                self.failed.emit(str(exc))
                return
            self.finished.emit(exit_code)

    class UpdaterWindow(QWidget):
        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle("Updating SyncRoom")
            self.setObjectName("updateRoot")
            self.setFixedSize(500, 320)
            self.close_button = QPushButton("Close")
            self.close_button.setObjectName("secondaryButton")
            self.close_button.hide()
            self.close_button.clicked.connect(self.close)

            shell = QVBoxLayout(self)
            shell.setContentsMargins(18, 18, 18, 18)
            shell.setSpacing(0)
            card = QFrame()
            card.setObjectName("updateCard")
            layout = QVBoxLayout(card)
            layout.setContentsMargins(24, 24, 24, 22)
            layout.setSpacing(12)

            eyebrow = QLabel("SYNCROOM UPDATE")
            eyebrow.setObjectName("updateEyebrow")
            title = QLabel("Updating SyncRoom")
            title.setObjectName("updateTitle")
            self.step_label = QLabel("Preparing update")
            self.step_label.setObjectName("updateStep")
            self.detail_label = QLabel("Starting updater...")
            self.detail_label.setObjectName("updateDetail")
            self.detail_label.setWordWrap(True)
            self.progress_bar = QProgressBar()
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(0)
            self.progress_bar.setTextVisible(False)
            self.version_label = QLabel(f"Installing {version}")
            self.version_label.setObjectName("updateVersion")

            button_row = QHBoxLayout()
            button_row.addStretch(1)
            button_row.addWidget(self.close_button)

            layout.addWidget(eyebrow)
            layout.addWidget(title)
            layout.addSpacing(4)
            layout.addWidget(self.step_label)
            layout.addWidget(self.detail_label)
            layout.addWidget(self.progress_bar)
            layout.addWidget(self.version_label)
            layout.addStretch(1)
            layout.addLayout(button_row)
            shell.addWidget(card)

            self.setStyleSheet(
                """
                QWidget#updateRoot {
                    background: #000000;
                    color: #f7f7f8;
                    font-family: "Noto Sans", "Segoe UI", sans-serif;
                }
                QLabel {
                    background: transparent;
                }
                QFrame#updateCard {
                    background: #080808;
                    border: 1px solid #303034;
                    border-radius: 14px;
                }
                QLabel#updateEyebrow {
                    color: #a3a3aa;
                    font-size: 10px;
                    font-weight: 800;
                    letter-spacing: 0.12em;
                }
                QLabel#updateTitle {
                    color: #ffffff;
                    font-size: 24px;
                    font-weight: 800;
                }
                QLabel#updateStep {
                    color: #f3f3f5;
                    font-size: 15px;
                    font-weight: 700;
                }
                QLabel#updateDetail, QLabel#updateVersion {
                    color: #a8a8af;
                    font-size: 12px;
                }
                QProgressBar {
                    min-height: 8px;
                    max-height: 8px;
                    border-radius: 4px;
                    border: 1px solid #333338;
                    background: #111112;
                }
                QProgressBar::chunk {
                    border-radius: 4px;
                    background: #eeeeef;
                }
                QPushButton#secondaryButton {
                    min-width: 86px;
                    min-height: 32px;
                    border-radius: 8px;
                    color: #eeeeef;
                    background: #151516;
                    border: 1px solid #36363a;
                    font-weight: 700;
                }
                QPushButton#secondaryButton:hover {
                    background: #202023;
                }
                """
            )

        def set_progress(self, step: str, detail: str, percent: int) -> None:
            self.step_label.setText(step)
            self.detail_label.setText(detail)
            self.progress_bar.setValue(max(0, min(100, percent)))

        def show_failure(self, message: str) -> None:
            self.step_label.setText("Update failed")
            self.detail_label.setText(f"{message}\n\nLog folder: {logs_dir()}")
            self.progress_bar.setValue(0)
            self.close_button.show()

    try:
        app = QApplication.instance() or QApplication(sys.argv[:1])
        window = UpdaterWindow()
        window.show()
        append_log("UI start succeeded")
    except Exception:
        append_log("UI window creation failed; continuing headless")
        append_log(traceback.format_exc().strip())
        return worker_callback(progress_noop)

    worker = Worker()
    thread = QThread()
    worker.moveToThread(thread)
    worker.progress.connect(window.set_progress)
    thread.started.connect(worker.run)
    result = {"code": 1}
    success_close_scheduled = {"value": False}

    def finish(exit_code: int) -> None:
        result["code"] = exit_code
        thread.quit()
        if exit_code == 0:
            window.set_progress("Done", "SyncRoom has been updated.", 100)
        else:
            window.show_failure(f"Updater exited with code {exit_code}.")

    def fail(message: str) -> None:
        result["code"] = 1
        thread.quit()
        window.show_failure(message)

    def schedule_success_close() -> None:
        if result["code"] != 0 or success_close_scheduled["value"]:
            return
        success_close_scheduled["value"] = True
        append_log("success auto-close scheduled")

        def close_success() -> None:
            append_log("success auto-close fired")
            window.close()
            append_log("QApplication exit requested")
            app.exit(0)

        QTimer.singleShot(1800, close_success)

    worker.finished.connect(finish)
    worker.failed.connect(fail)
    thread.finished.connect(schedule_success_close)
    thread.finished.connect(worker.deleteLater)
    thread.start()
    app.exec()
    if thread.isRunning():
        thread.quit()
        thread.wait(2000)
    return int(result["code"])


def stage1_apply_update(request: ApplyUpdateRequest) -> int:
    current_exe = Path(sys.executable).resolve()
    temp_dir, staged_exe = stage_updater_runtime(current_exe)
    command = [
        str(staged_exe),
        "--stage2",
        "--apply-update",
        "--version",
        request.version,
        "--asset-url",
        request.asset_url,
        "--asset-name",
        request.asset_name,
        "--app-path",
        str(request.app_path),
        "--pid",
        str(request.pid),
    ]
    process = spawn_detached(command, temp_dir)
    append_log(f"stage1 launched stage2 pid={process.pid} staged_exe={staged_exe}")
    append_log("stage1 complete")
    return 0


def parse_apply_request(namespace: argparse.Namespace) -> ApplyUpdateRequest:
    return ApplyUpdateRequest(
        version=str(namespace.version or "").strip(),
        asset_url=str(namespace.asset_url or "").strip(),
        asset_name=str(namespace.asset_name or "").strip(),
        app_path=Path(str(namespace.app_path)).resolve(),
        pid=int(namespace.pid),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SyncRoom updater helper")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--ui-self-test", action="store_true")
    parser.add_argument("--stage2", action="store_true")
    parser.add_argument("--apply-update", action="store_true")
    parser.add_argument("--version", default="")
    parser.add_argument("--asset-url", default="")
    parser.add_argument("--asset-name", default="")
    parser.add_argument("--app-path", default="")
    parser.add_argument("--pid", type=int, default=0)
    parser.add_argument("legacy", nargs="*")
    return parser


def main() -> int:
    namespace = build_parser().parse_args()
    stage = "self-test"
    if namespace.ui_self_test:
        stage = "ui-self-test"
    elif namespace.apply_update:
        stage = "stage2" if namespace.stage2 else "stage1"
    elif namespace.stage2:
        stage = "legacy-stage2"
    elif namespace.legacy:
        stage = "legacy-stage1"
    try:
        log_startup(stage)
        if namespace.self_test:
            return self_test()
        if namespace.ui_self_test:
            return run_ui_self_test()

        if namespace.apply_update:
            request = parse_apply_request(namespace)
            if namespace.stage2:
                return run_with_ui(lambda progress: apply_update_stage2(request, progress), request.version)
            return stage1_apply_update(request)

        if os.name != "nt":
            append_log("Updater only supports Windows for install mode; exiting.")
            return 1
        if len(namespace.legacy) < 3:
            append_log(f"Invalid updater arguments: {sys.argv!r}")
            return 1

        if not namespace.stage2:
            installer_path = Path(namespace.legacy[0]).resolve()
            app_path = Path(namespace.legacy[1]).resolve()
            pid = int(namespace.legacy[2])
            temp_dir, staged_exe = stage_updater_runtime(Path(sys.executable).resolve())
            process = spawn_detached(
                [str(staged_exe), "--stage2", str(installer_path), str(app_path), str(pid)],
                temp_dir,
            )
            append_log(f"legacy stage1 launched stage2 pid={process.pid}")
            return 0

        installer_path = Path(namespace.legacy[0]).resolve()
        app_path = Path(namespace.legacy[1]).resolve()
        pid = int(namespace.legacy[2])
        return apply_legacy_stage2(installer_path, app_path, pid)
    except Exception:
        append_log("Unhandled updater exception")
        append_log(traceback.format_exc().strip())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
