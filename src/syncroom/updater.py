from __future__ import annotations

import csv
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from io import StringIO
from pathlib import Path


WAIT_TIMEOUT_SECONDS = 180
INSTALLER_TIMEOUT_SECONDS = 600
INSTALLER_TIMEOUT_EXIT_CODE = 124
SUCCESS_INSTALLER_EXIT_CODES = {0, 3010}


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


def append_log(message: str) -> None:
    try:
        path = log_path()
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
            handle.flush()
    except Exception:
        pass


def spawn_detached(arguments: list[str], cwd: Path) -> None:
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    append_log(f"spawn detached command={subprocess.list2cmdline(arguments)} cwd={cwd}")
    subprocess.Popen(arguments, cwd=str(cwd), close_fds=True, creationflags=creationflags)


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


def pid_exists_fallback(pid: int) -> bool:
    result = subprocess.run(
        ["cmd", "/c", f'tasklist /FI "PID eq {pid}" 2>NUL'],
        capture_output=True,
        text=True,
        check=False,
    )
    return str(pid) in result.stdout


def wait_for_exit(pid: int, timeout_seconds: int = WAIT_TIMEOUT_SECONDS) -> bool:
    append_log(f"waiting for target PID pid={pid} timeout_seconds={timeout_seconds}")
    if os.name != "nt":
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


def wait_for_exit_with_tasklist_fallback(pid: int, timeout_seconds: int) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not pid_exists_fallback(pid):
            append_log(f"target PID exited according to fallback pid={pid}")
            return True
        time.sleep(0.5)
    append_log(f"target PID fallback wait timed out pid={pid}")
    return False


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


def normalized_path(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


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


def run_installer(installer_path: Path, installer_log: Path, app_path: Path) -> int:
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
    if path.name.startswith("syncroom-update-") or path.name.startswith("syncroom-update-run-"):
        safe_cleanup_path(path, "temporary update directory")


def log_startup(stage: str) -> None:
    current_exe = Path(sys.executable).resolve()
    append_log("SyncRoomUpdate process start")
    append_log(f"argv={sys.argv!r}")
    append_log(f"current_executable={current_exe}")
    append_log(f"current_working_directory={Path.cwd()}")
    append_log(f"stage={stage}")
    append_log(f"frozen={getattr(sys, 'frozen', False)}")
    append_log(f"windows_user_admin={is_running_as_admin()}")


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

    append_log("self-test completed successfully")
    print(f"SyncRoomUpdate self-test OK; log: {log_path()}")
    return 0


def main() -> int:
    try:
        stage = "self-test" if "--self-test" in sys.argv else (
            "stage2" if len(sys.argv) >= 2 and sys.argv[1] == "--stage2" else "stage1"
        )
        log_startup(stage)
        if "--self-test" in sys.argv:
            return self_test()

        if os.name != "nt":
            append_log("Updater only supports Windows; exiting.")
            return 1
        if len(sys.argv) < 4:
            append_log(f"Invalid updater arguments: {sys.argv!r}")
            return 1

        stage2 = len(sys.argv) >= 5 and sys.argv[1] == "--stage2"
        arg_offset = 2 if stage2 else 1
        installer_path = Path(sys.argv[arg_offset]).resolve()
        app_path = Path(sys.argv[arg_offset + 1]).resolve()
        pid = int(sys.argv[arg_offset + 2])
        installer_log = logs_dir() / "installer.log"
        current_exe = Path(sys.executable).resolve()

        append_log(f"received target PID pid={pid}")
        append_log(
            f"installer_path={installer_path} exists={installer_path.exists()} "
            f"size={installer_path.stat().st_size if installer_path.exists() else 0}"
        )
        append_log(f"app_path={app_path} exists={app_path.exists()}")
        append_log(f"app_path_under_program_files={path_is_under_program_files(app_path)}")
        append_log(f"installer_log={installer_log}")

        if not stage2:
            temp_dir, staged_exe = stage_updater_runtime(current_exe)
            append_log(
                f"stage1 launching stage2 staged_exe={staged_exe} installer={installer_path} "
                f"app={app_path} pid={pid}"
            )
            spawn_detached(
                [str(staged_exe), "--stage2", str(installer_path), str(app_path), str(pid)],
                temp_dir,
            )
            append_log("stage1 complete")
            return 0

        append_log("stage2 validation starting")
        if not installer_path.exists():
            append_log("Installer path does not exist; aborting update.")
            return 1
        if installer_path.stat().st_size <= 0:
            append_log("Installer path exists but is empty; aborting update.")
            return 1
        if not app_path.exists():
            append_log("App path does not exist before install; aborting update.")
            return 1

        wait_result = wait_for_exit(pid)
        append_log(f"target PID wait result pid={pid} exited={wait_result}")
        if not wait_result:
            append_log("Timed out while waiting for SyncRoom to exit; aborting installer launch.")
            return 1

        remaining = remaining_syncroom_processes(app_path)
        append_log(f"remaining SyncRoom.exe check result={remaining!r}")
        if remaining:
            append_log("SyncRoom.exe is still running from the install directory; aborting installer launch.")
            return 1

        append_log("installer launch starting after app exit verification")
        exit_code = run_installer(installer_path, installer_log, app_path)
        append_log(f"installer exit code {exit_code}")

        if exit_code not in SUCCESS_INSTALLER_EXIT_CODES:
            append_log(f"installer failed exit_code={exit_code}")
            return 1

        if app_path.exists():
            append_log(f"app relaunch starting path={app_path}")
            spawn_detached([str(app_path)], app_path.parent)
            append_log("app relaunch requested")
        else:
            append_log(f"app relaunch skipped because app path is missing: {app_path}")
            return 1

        append_log("cleanup starting")
        maybe_cleanup_update_temp(installer_path.parent)
        maybe_cleanup_update_temp(current_exe.parent)
        append_log("stage2 complete")
        return 0
    except Exception:
        append_log("Unhandled updater exception")
        append_log(traceback.format_exc().strip())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
