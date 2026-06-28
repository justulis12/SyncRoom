from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path


WAIT_TIMEOUT_SECONDS = 180


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
    subprocess.Popen(arguments, cwd=str(cwd), close_fds=True, creationflags=creationflags)


def stage_updater_runtime(current_exe: Path) -> tuple[Path, Path]:
    temp_dir = Path(tempfile.mkdtemp(prefix="syncroom-update-run-"))
    staged_exe = temp_dir / current_exe.name
    shutil.copy2(current_exe, staged_exe)

    internal_dir = current_exe.parent / "_internal"
    if internal_dir.is_dir():
        shutil.copytree(internal_dir, temp_dir / "_internal", dirs_exist_ok=True)

    append_log(f"staging temp directory temp_dir={temp_dir} exe={staged_exe}")
    return temp_dir, staged_exe


def pid_exists(pid: int) -> bool:
    result = subprocess.run(
        ["cmd", "/c", f'tasklist /FI "PID eq {pid}" 2>NUL'],
        capture_output=True,
        text=True,
        check=False,
    )
    return str(pid) in result.stdout


def wait_for_exit(pid: int, timeout_seconds: int = WAIT_TIMEOUT_SECONDS) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not pid_exists(pid):
            return True
        time.sleep(0.5)
    return False


def run_installer(installer_path: Path, installer_log: Path) -> int:
    command = [
        str(installer_path),
        "/SP-",
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NOCANCEL",
        "/CLOSEAPPLICATIONS",
        "/NORESTARTAPPLICATIONS",
        f"/LOG={installer_log}",
    ]
    append_log(f"installer command {subprocess.list2cmdline(command)}")
    result = subprocess.run(
        command,
        cwd=str(installer_path.parent),
        check=False,
    )
    return int(result.returncode)


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


def main() -> int:
    try:
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
        if not stage2:
            append_log(
                f"stage1 start current_exe={current_exe} installer={installer_path} app={app_path} pid={pid}"
            )
            temp_dir, staged_exe = stage_updater_runtime(current_exe)
            append_log(
                f"Staging updater from {current_exe} to {staged_exe} installer={installer_path} app={app_path} pid={pid}"
            )
            spawn_detached(
                [str(staged_exe), "--stage2", str(installer_path), str(app_path), str(pid)],
                temp_dir,
            )
            return 0

        append_log(f"stage2 start installer={installer_path} app={app_path} pid={pid}")
        append_log(
            f"installer path existence exists={installer_path.exists()} size={installer_path.stat().st_size if installer_path.exists() else 0}"
        )
        if not installer_path.exists():
            append_log("Installer path does not exist; aborting update.")
            return 1
        if installer_path.stat().st_size <= 0:
            append_log("Installer path exists but is empty; aborting update.")
            return 1

        wait_result = wait_for_exit(pid)
        append_log(f"target PID wait result pid={pid} exited={wait_result}")
        if not wait_result:
            append_log("Timed out while waiting for SyncRoom to exit; aborting installer launch.")
            return 1

        append_log("Target process exited, launching installer")
        exit_code = run_installer(installer_path, installer_log)
        append_log(f"installer exit code {exit_code}")

        if exit_code not in {0, 3010}:
            raise RuntimeError(f"Installer exited with code {exit_code}")

        if app_path.exists():
            append_log(f"app relaunch attempt path={app_path}")
            spawn_detached([str(app_path)], app_path.parent)
        else:
            append_log(f"app relaunch attempt skipped because app path is missing: {app_path}")
            return 1

        maybe_cleanup_update_temp(installer_path.parent)
        maybe_cleanup_update_temp(current_exe.parent)
        return 0
    except Exception:
        append_log(traceback.format_exc().strip())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
