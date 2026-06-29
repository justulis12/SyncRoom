from __future__ import annotations

import faulthandler
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

from syncroom.settings import logs_dir


MAX_LOG_BYTES = 256 * 1024
_FAULT_LOG_HANDLE = None


def update_log_path() -> Path:
    return safe_logs_dir() / "update.log"


def runtime_log_path() -> Path:
    return safe_logs_dir() / "runtime.log"


def safe_logs_dir() -> Path:
    try:
        return logs_dir()
    except Exception:
        fallback = Path(tempfile.gettempdir()) / "syncroom-logs"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def prepare_log_file(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8"):
            pass
    except Exception:
        pass


def _append_log(path: Path, message: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
            path.write_text("", encoding="utf-8")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
            handle.flush()
    except Exception:
        return


def append_update_log(message: str) -> None:
    _append_log(update_log_path(), message)


def append_runtime_log(message: str) -> None:
    _append_log(runtime_log_path(), message)


def append_crash_log(message: str) -> None:
    _append_log(safe_logs_dir() / "crash.log", message)


def flush_fault_log() -> None:
    if _FAULT_LOG_HANDLE is None:
        return
    try:
        _FAULT_LOG_HANDLE.flush()
    except Exception:
        pass


def configure_crash_logging() -> None:
    global _FAULT_LOG_HANDLE
    try:
        log_root = safe_logs_dir()
        prepare_log_file(log_root / "runtime.log")
        crash_path = log_root / "crash.log"
        prepare_log_file(crash_path)
        _FAULT_LOG_HANDLE = crash_path.open("a", encoding="utf-8", buffering=1)
        faulthandler.enable(_FAULT_LOG_HANDLE, all_threads=True)
        append_runtime_log(f"Logging initialized in {log_root}")
    except Exception:
        _FAULT_LOG_HANDLE = None
        return

    def handle_exception(exc_type, exc_value, exc_traceback) -> None:
        details = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        append_crash_log("Uncaught exception:\n" + details)
        append_runtime_log(f"Uncaught exception logged: {exc_value}")
        if _FAULT_LOG_HANDLE is not None:
            _FAULT_LOG_HANDLE.flush()
        sys.__excepthook__(exc_type, exc_value, exc_traceback)

    def handle_thread_exception(args) -> None:
        details = "".join(
            traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)
        )
        append_crash_log("Thread exception:\n" + details)
        append_runtime_log(f"Thread exception logged: {args.exc_value}")
        if _FAULT_LOG_HANDLE is not None:
            _FAULT_LOG_HANDLE.flush()

    sys.excepthook = handle_exception
    threading.excepthook = handle_thread_exception
