from __future__ import annotations

import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, QObject
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QMessageBox, QProgressBar, QVBoxLayout


def updater_log_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    return base / "SyncRoom" / "updater.log"


def append_updater_log(message: str) -> None:
    path = updater_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")


class UpdateInstallerWorker(QObject):
    progress = Signal(str, int)
    finished = Signal()
    failed = Signal(str)

    def __init__(self, installer_path: str, app_path: str, pid: int) -> None:
        super().__init__()
        self.installer_path = installer_path
        self.app_path = app_path
        self.pid = pid

    def run(self) -> None:
        try:
            append_updater_log(
                f"Updater started installer={self.installer_path} app={self.app_path} pid={self.pid}"
            )
            self.progress.emit("Waiting for SyncRoom to close...", 10)
            self._wait_for_pid_exit()
            self.progress.emit("Installing update...", 45)
            result = subprocess.run(
                [
                    self.installer_path,
                    "/SP-",
                    "/VERYSILENT",
                    "/SUPPRESSMSGBOXES",
                    "/NOCANCEL",
                    "/CLOSEAPPLICATIONS",
                ],
                check=False,
            )
            append_updater_log(f"Installer exited with code {result.returncode}")
            if result.returncode not in {0}:
                raise RuntimeError(f"Installer exited with code {result.returncode}")
            self.progress.emit("Restarting SyncRoom...", 90)
            subprocess.Popen([self.app_path], close_fds=True)
            append_updater_log("Restarted SyncRoom successfully")
        except Exception as exc:
            append_updater_log(f"Updater failed: {exc}")
            self.failed.emit(str(exc))
            return
        self.finished.emit()

    def _wait_for_pid_exit(self) -> None:
        deadline = time.time() + 180
        while time.time() < deadline:
            if not self._pid_exists(self.pid):
                return
            time.sleep(0.4)
        raise RuntimeError("Timed out while waiting for SyncRoom to close for the update.")

    @staticmethod
    def _pid_exists(pid: int) -> bool:
        check = subprocess.run(
            ["cmd", "/c", f'tasklist /FI "PID eq {pid}" 2>NUL'],
            capture_output=True,
            text=True,
            check=False,
        )
        return str(pid) in check.stdout


class UpdateInstallDialog(QDialog):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("SyncRoom Update")
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self.setWindowFlag(Qt.WindowCloseButtonHint, False)
        self.setModal(True)
        self.setFixedSize(520, 240)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 28, 28, 28)
        layout.setSpacing(14)

        eyebrow = QLabel("UPDATING SYNCROOM")
        eyebrow.setObjectName("eyebrow")
        title = QLabel("Installing the new version")
        title.setObjectName("title")
        title.setWordWrap(True)
        subtitle = QLabel(
            "SyncRoom is applying the update and will reopen automatically when it finishes."
        )
        subtitle.setObjectName("subtitle")
        subtitle.setWordWrap(True)

        self.status_label = QLabel("Preparing update...")
        self.status_label.setObjectName("setupStatus")
        self.status_label.setWordWrap(True)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)

        layout.addWidget(eyebrow)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addStretch(1)
        layout.addWidget(self.status_label)
        layout.addWidget(self.progress_bar)

        self.setStyleSheet(
            """
            QDialog {
                background: #050508;
                color: #f8f4ff;
                font-family: "Noto Sans", "Cantarell", sans-serif;
            }
            QLabel#eyebrow {
                color: #bfa3dd;
                font-size: 11px;
                font-weight: 700;
                letter-spacing: 0.16em;
            }
            QLabel#title {
                color: #fbf7ff;
                font-size: 28px;
                font-weight: 800;
            }
            QLabel#subtitle, QLabel#setupStatus {
                color: #d3c5e3;
                font-size: 14px;
            }
            QProgressBar {
                min-height: 18px;
                border-radius: 9px;
                border: 1px solid #3d2e4c;
                background: #120f18;
                color: #f5effc;
                text-align: center;
            }
            QProgressBar::chunk {
                border-radius: 8px;
                background: #8c58d6;
            }
            """
        )

    def set_progress(self, message: str, percent: int) -> None:
        self.status_label.setText(message)
        self.progress_bar.setValue(percent)


def run_windows_update_helper(installer_path: str, app_path: str, pid: int) -> int:
    app = QApplication(sys.argv)
    dialog = UpdateInstallDialog()
    worker = UpdateInstallerWorker(installer_path, app_path, pid)
    thread = QThread()
    worker.moveToThread(thread)
    worker.progress.connect(dialog.set_progress)
    worker.finished.connect(dialog.accept)
    worker.finished.connect(thread.quit)
    worker.failed.connect(thread.quit)
    thread.started.connect(worker.run)

    failure_message = {"text": ""}

    def remember_failure(message: str) -> None:
        failure_message["text"] = message
        dialog.set_progress(message, 0)
        dialog.reject()

    worker.failed.connect(remember_failure)
    thread.start()
    dialog.set_progress("Preparing update...", 0)
    result = dialog.exec()
    thread.wait()
    worker.deleteLater()
    thread.deleteLater()

    if result == QDialog.Accepted:
        return 0

    QMessageBox.critical(
        None,
        "SyncRoom Update",
        failure_message["text"] or "SyncRoom could not install the update automatically.",
    )
    return 1


def main() -> None:
    try:
        if os.name != "nt" or len(sys.argv) < 4:
            raise SystemExit(1)
        installer_path = sys.argv[1]
        app_path = sys.argv[2]
        pid = int(sys.argv[3])
        raise SystemExit(run_windows_update_helper(installer_path, app_path, pid))
    except Exception:
        append_updater_log(traceback.format_exc().strip())
        raise


if __name__ == "__main__":
    main()
