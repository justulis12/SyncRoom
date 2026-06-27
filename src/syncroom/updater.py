from __future__ import annotations

import os
import subprocess
import sys
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

        installer_path = Path(sys.argv[1]).resolve()
        app_path = Path(sys.argv[2]).resolve()
        pid = int(sys.argv[3])
        installer_log = logs_dir() / "installer.log"

        append_log(
            f"Updater started installer={installer_path} app={app_path} pid={pid}"
        )
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
