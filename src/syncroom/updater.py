from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path


def logs_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path.home() / ".config"
    path = base / "syncroom" / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_path() -> Path:
    return logs_dir() / "update-helper.log"


def append_log(message: str) -> None:
    path = log_path()
    try:
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


def pid_exists(pid: int) -> bool:
    result = subprocess.run(
        ["cmd", "/c", f'tasklist /FI "PID eq {pid}" 2>NUL'],
        capture_output=True,
        text=True,
        check=False,
    )
    return str(pid) in result.stdout


def wait_for_exit(pid: int, timeout_seconds: int = 180) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not pid_exists(pid):
            return
        time.sleep(0.5)
    raise RuntimeError("Timed out while waiting for SyncRoom to close.")


def main() -> None:
    try:
        if os.name != "nt":
            raise SystemExit(1)
        if len(sys.argv) < 4:
            raise SystemExit(1)

        stage2 = len(sys.argv) >= 5 and sys.argv[1] == "--stage2"
        arg_offset = 2 if stage2 else 1
        installer_path = Path(sys.argv[arg_offset]).resolve()
        app_path = Path(sys.argv[arg_offset + 1]).resolve()
        pid = int(sys.argv[arg_offset + 2])
        installer_log = logs_dir() / "installer.log"

        current_exe = Path(sys.executable).resolve()
        if not stage2:
            temp_dir = Path(tempfile.mkdtemp(prefix="syncroom-update-run-"))
            staged_exe = temp_dir / current_exe.name
            shutil.copy2(current_exe, staged_exe)
            append_log(
                f"Staging updater from {current_exe} to {staged_exe} installer={installer_path} app={app_path} pid={pid}"
            )
            spawn_detached(
                [str(staged_exe), "--stage2", str(installer_path), str(app_path), str(pid)],
                temp_dir,
            )
            return

        append_log(f"Updater stage2 started installer={installer_path} app={app_path} pid={pid}")
        wait_for_exit(pid)
        append_log("Target process exited, launching installer")

        result = subprocess.run(
            [
                str(installer_path),
                "/SP-",
                "/VERYSILENT",
                "/SUPPRESSMSGBOXES",
                "/NOCANCEL",
                "/CLOSEAPPLICATIONS",
                "/RESTARTAPPLICATIONS",
                f"/LOG={installer_log}",
            ],
            cwd=str(installer_path.parent),
            check=False,
        )
        append_log(f"Installer exited with code {result.returncode}")

        if result.returncode != 0:
            raise RuntimeError(f"Installer exited with code {result.returncode}")

        if app_path.exists():
            subprocess.Popen([str(app_path)], cwd=str(app_path.parent), close_fds=True)
            append_log("Requested app relaunch")
    except Exception:
        append_log(traceback.format_exc().strip())
        raise


if __name__ == "__main__":
    main()
