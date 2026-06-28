from __future__ import annotations

import faulthandler
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFontMetrics
from PySide6.QtNetwork import QAbstractSocket, QTcpSocket
from PySide6.QtWidgets import (
    QApplication,
    QBoxLayout,
    QComboBox,
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSlider,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from syncroom.mpv_controller import MpvController
from syncroom.protocol import decode_message, encode_message
from syncroom.settings import app_config_dir, default_display_name, load_settings, logs_dir, save_settings
from syncroom.updates import (
    UpdateInfo,
    check_for_updates,
    cleanup_update_download,
    download_update_asset,
)
from syncroom.windows_runtime import ensure_windows_mpv_runtime


def update_log_path() -> Path:
    return safe_logs_dir() / "update.log"


def append_update_log(message: str) -> None:
    _append_log(update_log_path(), message)


_FAULT_LOG_HANDLE = None
MAX_LOG_BYTES = 256 * 1024
THREAD_SHUTDOWN_TIMEOUT_MS = 5000
THREAD_FORCE_TERMINATE_TIMEOUT_MS = 2000


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


def append_runtime_log(message: str) -> None:
    _append_log(runtime_log_path(), message)


def append_crash_log(message: str) -> None:
    _append_log(safe_logs_dir() / "crash.log", message)


def qapplication_is_closing() -> bool:
    app = QApplication.instance()
    return app is None or app.closingDown()


def dispose_qobject_safely(obj: QObject | None) -> None:
    if obj is None:
        return
    if not qapplication_is_closing():
        obj.deleteLater()


def stop_thread_with_timeout(
    thread: QThread,
    log: Callable[[str], None],
    context: str,
    timeout_ms: int = THREAD_SHUTDOWN_TIMEOUT_MS,
) -> bool:
    if not thread.isRunning():
        return True
    thread.quit()
    if thread.wait(timeout_ms):
        return True

    log(f"{context} thread did not stop within {timeout_ms}ms; forcing termination.")
    thread.terminate()
    if thread.wait(THREAD_FORCE_TERMINATE_TIMEOUT_MS):
        log(f"{context} thread was force-terminated.")
        return False

    log(f"{context} thread could not be terminated cleanly.")
    return False


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
        append_runtime_log(
            f"Uncaught exception logged: {exc_value}"
        )
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


class SyncClient(QObject):
    room_state = Signal(dict)
    info = Signal(str)
    error = Signal(str)
    connected = Signal()
    disconnected = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.socket = QTcpSocket(self)
        self.socket.readyRead.connect(self._on_ready_read)
        self.socket.connected.connect(self.connected)
        self.socket.disconnected.connect(self.disconnected)
        self.socket.errorOccurred.connect(self._on_error)
        self._buffer = bytearray()
        self.client_id = ""
        self.room = ""

    def connect_to_server(
        self,
        host: str,
        port: int,
        room: str,
        name: str,
        password: str = "",
    ) -> None:
        self.room = room
        self.name = name
        append_runtime_log(
            f"Connecting to server host={host} port={port} room={room} name={name} password={'yes' if password else 'no'}"
        )
        self.socket.connectToHost(host, port)
        if self.socket.waitForConnected(3000):
            self.send(
                {
                    "type": "join",
                    "room": room,
                    "name": name,
                    "password": password,
                }
            )

    def disconnect_from_server(self) -> None:
        if self.socket.state() != QAbstractSocket.UnconnectedState:
            self.socket.disconnectFromHost()

    def send_state(
        self,
        media_url: str,
        position_ms: int,
        playing: bool,
        force_seek: bool = False,
        reason: str = "",
    ) -> None:
        if self.socket.state() != QAbstractSocket.ConnectedState:
            return
        self.send(
            {
                "type": "state",
                "media_url": media_url,
                "position_ms": position_ms,
                "playing": playing,
                "force_seek": force_seek,
                "reason": reason,
            }
        )

    def send(self, payload: dict) -> None:
        if self.socket.state() != QAbstractSocket.ConnectedState:
            return
        self.socket.write(encode_message(payload))

    def _on_ready_read(self) -> None:
        self._buffer.extend(self.socket.readAll().data())
        while b"\n" in self._buffer:
            raw_line, _, remainder = self._buffer.partition(b"\n")
            self._buffer = bytearray(remainder)
            if not raw_line.strip():
                continue
            try:
                payload = decode_message(raw_line)
            except Exception as exc:
                self.error.emit(f"Invalid message from server: {exc}")
                continue
            self._handle_message(payload)

    def _handle_message(self, payload: dict) -> None:
        msg_type = payload.get("type")
        if msg_type == "welcome":
            self.client_id = str(payload.get("client_id") or "")
            append_runtime_log(
                f"Connected to room welcome room={payload.get('room')} client_id={self.client_id}"
            )
            self.info.emit(f"Connected to room {payload.get('room')}")
        elif msg_type == "room_state":
            self.room_state.emit(payload)
        elif msg_type == "error":
            append_runtime_log(f"Server error: {payload.get('message')}")
            self.error.emit(str(payload.get("message") or "Unknown server error"))

    def _on_error(self, _socket_error: QAbstractSocket.SocketError) -> None:
        self.error.emit(self.socket.errorString())


class WindowsRuntimeInstallerWorker(QObject):
    progress = Signal(str, int)
    finished = Signal()
    failed = Signal(str)

    def run(self) -> None:
        try:
            ensure_windows_mpv_runtime(self._report)
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.finished.emit()

    def _report(self, message: str, percent: int) -> None:
        self.progress.emit(message, percent)


class UpdateCheckWorker(QObject):
    finished = Signal(object)

    def run(self) -> None:
        self.finished.emit(check_for_updates())


class UpdateDownloadWorker(QObject):
    progress = Signal(str, int)
    finished = Signal(str)
    failed = Signal(str)

    def __init__(self, info: UpdateInfo) -> None:
        super().__init__()
        self.info = info

    def run(self) -> None:
        try:
            path = download_update_asset(self.info, self._report)
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.finished.emit(str(path))

    def _report(self, message: str, percent: int) -> None:
        self.progress.emit(message, percent)


class ProgressScreenDialog(QDialog):
    def __init__(self, eyebrow_text: str, title_text: str, subtitle_text: str) -> None:
        super().__init__()
        self.setWindowTitle("SyncRoom Setup")
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self.setWindowFlag(Qt.WindowCloseButtonHint, False)
        self.setModal(True)
        self.setFixedSize(520, 240)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 28, 28, 28)
        layout.setSpacing(14)

        eyebrow = QLabel(eyebrow_text)
        eyebrow.setObjectName("eyebrow")
        title = QLabel(title_text)
        title.setObjectName("title")
        title.setWordWrap(True)
        subtitle = QLabel(subtitle_text)
        subtitle.setObjectName("subtitle")
        subtitle.setWordWrap(True)

        self.status_label = QLabel("Preparing setup...")
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
                background: #050505;
                color: #f2f2f4;
                font-family: "Noto Sans", "Cantarell", sans-serif;
            }
            QLabel#eyebrow {
                color: #b8b8bf;
                font-size: 11px;
                font-weight: 800;
                letter-spacing: 0.12em;
            }
            QLabel#title {
                color: #ffffff;
                font-size: 28px;
                font-weight: 800;
            }
            QLabel#subtitle, QLabel#setupStatus {
                color: #b9b9c0;
                font-size: 14px;
            }
            QProgressBar {
                min-height: 18px;
                border-radius: 9px;
                border: 1px solid #525257;
                background: #101011;
                color: #f3f3f5;
                text-align: center;
            }
            QProgressBar::chunk {
                border-radius: 8px;
                background: #f0f0f2;
            }
            """
        )

    def set_progress(self, message: str, percent: int) -> None:
        self.status_label.setText(message)
        self.progress_bar.setValue(percent)


class StartupSetupDialog(ProgressScreenDialog):
    def __init__(self) -> None:
        super().__init__(
            "FIRST-TIME SETUP",
            "Installing the video player",
            (
                "SyncRoom is downloading and preparing mpv for Windows. "
                "The app will open automatically when setup finishes."
            ),
        )


class UpdateInstallDialog(ProgressScreenDialog):
    def __init__(self) -> None:
        super().__init__(
            "UPDATING SYNCROOM",
            "Installing the new version",
            "SyncRoom is applying the update and will reopen automatically when it finishes.",
        )


class UpdateProgressDialog(QDialog):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("SyncRoom Update")
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self.setWindowFlag(Qt.WindowCloseButtonHint, False)
        self.setModal(True)
        self.setFixedSize(520, 220)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 28, 28, 28)
        layout.setSpacing(14)

        eyebrow = QLabel("UPDATE")
        eyebrow.setObjectName("eyebrow")
        title = QLabel("Installing the latest SyncRoom")
        title.setObjectName("title")
        subtitle = QLabel(
            "SyncRoom is downloading the new installer. The app will close and relaunch when the update finishes."
        )
        subtitle.setObjectName("subtitle")
        subtitle.setWordWrap(True)

        self.status_label = QLabel("Preparing update...")
        self.status_label.setObjectName("setupStatus")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)

        layout.addWidget(eyebrow)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addStretch(1)
        layout.addWidget(self.status_label)
        layout.addWidget(self.progress_bar)

        self.setStyleSheet(
            """
            QDialog {
                background: #050505;
                color: #f2f2f4;
                font-family: "Noto Sans", "Cantarell", sans-serif;
            }
            QLabel#eyebrow {
                color: #b8b8bf;
                font-size: 11px;
                font-weight: 800;
                letter-spacing: 0.12em;
            }
            QLabel#title {
                color: #ffffff;
                font-size: 28px;
                font-weight: 800;
            }
            QLabel#subtitle, QLabel#setupStatus {
                color: #b9b9c0;
                font-size: 14px;
            }
            QProgressBar {
                min-height: 18px;
                border-radius: 9px;
                border: 1px solid #525257;
                background: #101011;
                color: #f3f3f5;
                text-align: center;
            }
            QProgressBar::chunk {
                border-radius: 8px;
                background: #f0f0f2;
            }
            """
        )

    def set_progress(self, message: str, percent: int) -> None:
        self.status_label.setText(message)
        self.progress_bar.setValue(percent)


def prepare_windows_runtime_if_needed() -> bool:
    if os.name != "nt":
        return True

    probe = MpvController()
    if not probe.needs_windows_runtime_install():
        return True

    dialog = StartupSetupDialog()
    worker = WindowsRuntimeInstallerWorker()
    thread = QThread()
    worker.moveToThread(thread)
    worker.progress.connect(dialog.set_progress)
    thread.started.connect(worker.run)

    state = {
        "failure_message": "",
        "succeeded": False,
    }

    def remember_success() -> None:
        state["succeeded"] = True
        dialog.set_progress("Player installed.", 100)
        append_runtime_log("Windows runtime installer worker finished successfully")
        thread.quit()
        dialog.accept()

    def remember_failure(message: str) -> None:
        state["failure_message"] = message
        dialog.set_progress(message, 0)
        append_runtime_log(f"Windows runtime installer worker failed: {message}")
        thread.quit()
        dialog.reject()

    worker.finished.connect(remember_success)
    worker.failed.connect(remember_failure)
    thread.start()
    dialog.set_progress("Preparing setup...", 0)
    result = dialog.exec()
    stopped_cleanly = stop_thread_with_timeout(
        thread,
        append_runtime_log,
        "Windows runtime installer",
    )
    if result == QDialog.Accepted and not stopped_cleanly:
        state["failure_message"] = (
            "SyncRoom finished setup, but the installer thread did not stop cleanly. "
            f"See {runtime_log_path()} for details."
        )
        result = QDialog.Rejected
    dispose_qobject_safely(worker)
    dispose_qobject_safely(thread)

    if result == QDialog.Accepted:
        return True

    QMessageBox.critical(
        None,
        "SyncRoom Setup",
        state["failure_message"]
        or f"SyncRoom could not install mpv automatically.\n\nSee {runtime_log_path()} for details.",
    )
    return False


class MainWindow(QMainWindow):
    PAUSED_SYNC_THRESHOLD_MS = 250
    PLAYING_REWIND_THRESHOLD_MS = 1400
    PLAYING_FASTFORWARD_THRESHOLD_MS = 1800
    PLAYING_FASTFORWARD_GRACE_SECONDS = 0.8
    LOCAL_PLAYING_SEEK_THRESHOLD_MS = 1200
    LOCAL_PAUSED_SEEK_THRESHOLD_MS = 350
    SYNC_RETRY_INTERVAL_MS = 450
    HOSTED_SERVER_LABEL = "Hosted server"
    CUSTOM_SERVER_LABEL = "Custom host"
    HOSTED_SERVER_URL = "syncroom1.justys.xyz"

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("SyncRoom")
        self.resize(1100, 720)
        self.setMinimumSize(720, 520)
        self.settings = load_settings()

        self.sync_client = SyncClient()
        self.sync_client.room_state.connect(self.on_room_state)
        self.sync_client.info.connect(self.show_status)
        self.sync_client.error.connect(self.show_error)
        self.sync_client.connected.connect(lambda: self.on_connection_change(True))
        self.sync_client.disconnected.connect(lambda: self.on_connection_change(False))

        self.player = MpvController()

        self.suppress_sync = False
        self.dragging_slider = False
        self.current_media_url = ""
        self.last_known_playing = False
        self.audio_tracks: list[dict] = []
        self.current_member_count = 0
        self.pending_audio_attempts = 0
        self.closing_for_mpv_exit = False
        self.pending_room_sync: dict | None = None
        self.pending_room_sync_attempts = 0
        self.last_room_payload: dict | None = None
        self.room_sync_state = "idle"
        self.room_sync_note = ""
        self._pending_sync_retry_scheduled = False
        self.local_playback_override_until = 0.0
        self.local_playback_target: bool | None = None
        self.local_seek_override_until = 0.0
        self.local_seek_target_ms = 0
        self.last_polled_position_ms: int | None = None
        self.last_polled_playing: bool | None = None
        self.last_poll_monotonic: float | None = None
        self.pending_update_info: UpdateInfo | None = None
        self.update_check_started = False
        self.update_prompted_version = ""
        self.update_installer_path: Path | None = None
        self.last_applied_seek_token = 0
        self.last_applied_event_id = 0
        self.behind_sync_detected_at: float | None = None
        self.player_seen_running_in_room = False
        self._active_threads: list[QThread] = []
        self._active_workers: list[QObject] = []

        self._last_ui_scale = -1.0
        self.build_ui()
        self.refresh_scaled_ui(force=True)

        self.heartbeat = QTimer(self)
        self.heartbeat.setInterval(350)
        self.heartbeat.timeout.connect(self.poll_player_state)
        self.heartbeat.start()
        if os.name == "nt":
            QTimer.singleShot(2000, self.start_update_check)

    def build_ui(self) -> None:
        central = QWidget(self)
        central.setObjectName("rootCanvas")
        root = QVBoxLayout(central)
        self.root_layout = root

        dashboard_shell, dashboard_layout = self.build_panel(
            "dashboardShell",
            margins=(18, 18, 18, 18),
            spacing=16,
            glow_color="#090913",
            glow_alpha=150,
            blur=52,
        )
        dashboard_layout.addWidget(self.build_top_bar())

        content_shell, content_layout = self.build_panel(
            "contentShell",
            margins=(12, 12, 12, 12),
            spacing=0,
            glow_color="#0d1020",
            glow_alpha=120,
            blur=42,
        )
        self.page_stack = QStackedWidget()
        self.page_stack.setObjectName("pageStack")
        self.page_stack.addWidget(self.make_scroll_page(self.build_join_page()))
        self.page_stack.addWidget(self.make_scroll_page(self.build_room_page()))
        self.page_stack.currentChanged.connect(self.update_page_chrome)

        content_layout.addWidget(self.page_stack)
        dashboard_layout.addWidget(content_shell, 1)
        root.addWidget(dashboard_shell)
        self.setCentralWidget(central)
        self.statusBar().showMessage("Ready")
        self.refresh_audio_tracks()
        self.update_page_chrome(self.page_stack.currentIndex())
        QTimer.singleShot(0, self.update_responsive_layouts)

        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(QApplication.instance().quit)
        self.addAction(quit_action)

    def build_panel(
        self,
        object_name: str,
        margins: tuple[int, int, int, int] = (24, 24, 24, 24),
        spacing: int = 16,
        glow_color: str = "#090b18",
        glow_alpha: int = 125,
        blur: int = 36,
        offset_y: int = 14,
    ) -> tuple[QFrame, QVBoxLayout]:
        panel = QFrame()
        panel.setObjectName(object_name)
        panel.setFrameShape(QFrame.NoFrame)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(*margins)
        layout.setSpacing(spacing)
        self.apply_depth_effect(panel, glow_color, glow_alpha, blur, offset_y)
        return panel, layout

    def apply_depth_effect(
        self,
        widget: QWidget,
        color: str,
        alpha: int = 125,
        blur: int = 36,
        offset_y: int = 14,
    ) -> None:
        shadow = QGraphicsDropShadowEffect(widget)
        glow = QColor(color)
        glow.setAlpha(max(0, min(255, alpha)))
        shadow.setColor(glow)
        shadow.setBlurRadius(blur)
        shadow.setOffset(0, offset_y)
        widget.setGraphicsEffect(shadow)

    def build_nav_chip(self, text: str) -> QLabel:
        chip = QLabel(text)
        chip.setObjectName("navChip")
        chip.setProperty("active", False)
        chip.setAlignment(Qt.AlignCenter)
        return chip

    def set_nav_chip_active(self, chip: QLabel, active: bool) -> None:
        chip.setProperty("active", active)
        chip.style().unpolish(chip)
        chip.style().polish(chip)
        chip.update()

    def build_top_bar(self) -> QFrame:
        top_bar, top_layout = self.build_panel(
            "topBar",
            margins=(10, 10, 10, 10),
            spacing=0,
            glow_color="#080911",
            glow_alpha=70,
            blur=20,
            offset_y=6,
        )

        nav_capsule = QFrame()
        nav_capsule.setObjectName("navCapsule")
        nav_layout = QHBoxLayout(nav_capsule)
        nav_layout.setContentsMargins(8, 8, 8, 8)
        nav_layout.setSpacing(8)
        self.lobby_nav_chip = self.build_nav_chip("Lobby")
        self.room_nav_chip = self.build_nav_chip("Room")
        for chip in (self.lobby_nav_chip, self.room_nav_chip):
            nav_layout.addWidget(chip)

        self.top_bar_row = QHBoxLayout()
        self.top_bar_row.setContentsMargins(0, 0, 0, 0)
        self.top_bar_row.addWidget(nav_capsule)
        self.top_bar_row.addStretch(1)
        top_layout.addLayout(self.top_bar_row)
        return top_bar

    def make_scroll_page(self, widget: QWidget) -> QScrollArea:
        widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        scroll = QScrollArea()
        scroll.setObjectName("pageScrollArea")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setWidget(widget)
        return scroll

    def configure_resizable_label(
        self,
        label: QLabel,
        *,
        wrap: bool = True,
        vertical_policy: QSizePolicy.Policy = QSizePolicy.Preferred,
    ) -> None:
        label.setMinimumWidth(0)
        label.setWordWrap(wrap)
        label.setSizePolicy(QSizePolicy.Expanding, vertical_policy)

    def set_label_text_safe(
        self,
        label: QLabel,
        full_text: str,
        *,
        elide_mode: Qt.TextElideMode | None = None,
    ) -> None:
        text = str(full_text or "")
        label.setProperty("full_text", text)
        label.setToolTip(text)
        mode_value = None
        if elide_mode is not None:
            mode_value = elide_mode.value if hasattr(elide_mode, "value") else int(elide_mode)
        label.setProperty("elide_mode", mode_value)
        if elide_mode is None:
            label.setText(text)
            return
        label.setWordWrap(False)
        self.update_single_elided_label(label)

    def update_single_elided_label(self, label: QLabel) -> None:
        full_text = str(label.property("full_text") or "")
        elide_value = label.property("elide_mode")
        if elide_value is None:
            if label.text() != full_text:
                label.setText(full_text)
            return
        available_width = max(24, label.contentsRect().width())
        metrics = QFontMetrics(label.font())
        try:
            mode = Qt.TextElideMode(int(elide_value))
        except Exception:
            mode = Qt.ElideRight
        label.setText(metrics.elidedText(full_text, mode, available_width))
        label.setToolTip(full_text if metrics.horizontalAdvance(full_text) > available_width else "")

    def update_elided_labels(self) -> None:
        for label in self.findChildren(QLabel):
            if label.property("elide_mode") is not None:
                self.update_single_elided_label(label)

    def set_stat_card_text(
        self,
        card: QFrame,
        *,
        value: str | None = None,
        caption: str | None = None,
        value_elide_mode: Qt.TextElideMode = Qt.ElideRight,
        caption_elide_mode: Qt.TextElideMode | None = None,
    ) -> None:
        if value is not None:
            self.set_label_text_safe(card.value_label, value, elide_mode=value_elide_mode)  # type: ignore[attr-defined]
        if caption is not None:
            caption_text = str(caption)
            card.caption_label.setVisible(bool(caption_text.strip()))  # type: ignore[attr-defined]
            self.set_label_text_safe(  # type: ignore[attr-defined]
                card.caption_label,
                caption_text,
                elide_mode=caption_elide_mode,
            )

    def build_join_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("dashboardPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(0)
        self.join_page_layout = layout
        self.join_shell_layout = QVBoxLayout()
        self.join_shell_layout.setContentsMargins(0, 0, 0, 0)
        self.join_shell_layout.setSpacing(16)

        card, card_layout = self.build_panel(
            "controlPanel",
            margins=(28, 28, 28, 28),
            spacing=16,
            glow_color="#040404",
            glow_alpha=90,
            blur=24,
            offset_y=10,
        )
        card.setMaximumWidth(980)
        self.join_card = card

        eyebrow = QLabel("ACCESS")
        eyebrow.setObjectName("eyebrow")
        title = QLabel("Join a room")
        title.setObjectName("title")
        title.setWordWrap(True)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        self.join_grid = grid

        saved_host = self.settings.get("host", self.HOSTED_SERVER_URL)
        saved_host_mode = self.settings.get("host_mode", self.HOSTED_SERVER_LABEL)
        if saved_host_mode not in {self.HOSTED_SERVER_LABEL, self.CUSTOM_SERVER_LABEL}:
            saved_host_mode = self.HOSTED_SERVER_LABEL

        self.host_select = QComboBox()
        self.host_select.addItem(f"{self.HOSTED_SERVER_LABEL} ({self.HOSTED_SERVER_URL})")
        self.host_select.addItem(self.CUSTOM_SERVER_LABEL)
        self.host_input = QLineEdit(saved_host if saved_host_mode == self.CUSTOM_SERVER_LABEL else self.HOSTED_SERVER_URL)
        self.port_input = QLineEdit(str(self.settings.get("port", "24873")))
        self.room_input = QLineEdit(self.settings.get("room", "movie-night"))
        self.password_input = QLineEdit(self.settings.get("room_password", ""))
        self.name_input = QLineEdit(self.settings.get("name", default_display_name()))
        self.host_select.setCurrentIndex(1 if saved_host_mode == self.CUSTOM_SERVER_LABEL else 0)
        self.host_input.setPlaceholderText("Host or domain")
        self.port_input.setPlaceholderText("Port")
        self.room_input.setPlaceholderText("movie-night")
        self.password_input.setPlaceholderText("Room password")
        self.password_input.setEchoMode(QLineEdit.Password)
        self.name_input.setPlaceholderText("Name")
        self.name_input.returnPressed.connect(self.connect_to_room)
        self.host_select.currentIndexChanged.connect(self.on_host_mode_changed)

        self.join_host_profile_label = self.build_field_label("Server profile")
        self.join_host_label = self.build_field_label("Host or domain")
        self.join_port_label = self.build_field_label("Port")
        self.join_room_label = self.build_field_label("Room name")
        self.join_password_label = self.build_field_label("Room password")
        self.join_name_label = self.build_field_label("Display name")
        self.rebuild_join_grid(compact=False)
        self.on_host_mode_changed()

        self.join_button_row = QBoxLayout(QBoxLayout.LeftToRight)
        self.join_button_row.setContentsMargins(0, 2, 0, 0)
        self.join_button_row.setSpacing(10)
        self.connect_button = QPushButton("Join Room")
        self.connect_button.setObjectName("primaryButton")
        self.connect_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.connect_button.clicked.connect(self.connect_to_room)
        self.join_button_row.addWidget(self.connect_button, 0, Qt.AlignHCenter)

        card_layout.addWidget(eyebrow)
        card_layout.addWidget(title)
        card_layout.addLayout(grid)
        card_layout.addLayout(self.join_button_row)
        card_layout.addStretch(1)

        self.join_shell_layout.addWidget(card, 0, Qt.AlignTop | Qt.AlignHCenter)
        self.join_shell_layout.addStretch(1)
        layout.addLayout(self.join_shell_layout)
        return page

    def build_room_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("dashboardPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(0)
        self.room_page_layout = layout

        self.room_shell_layout = QBoxLayout(QBoxLayout.LeftToRight)
        self.room_shell_layout.setContentsMargins(0, 0, 0, 0)
        self.room_shell_layout.setSpacing(18)

        left_column = QVBoxLayout()
        left_column.setContentsMargins(0, 0, 0, 0)
        left_column.setSpacing(18)

        hero_card, hero_layout = self.build_panel(
            "heroPanel",
            margins=(26, 26, 26, 26),
            spacing=14,
            glow_color="#151127",
            glow_alpha=150,
            blur=44,
            offset_y=18,
        )

        self.room_header_row = QBoxLayout(QBoxLayout.LeftToRight)
        self.room_header_row.setContentsMargins(0, 0, 0, 0)
        title_box = QVBoxLayout()
        title_box.setSpacing(4)
        room_eyebrow = QLabel("ROOM CONTROL")
        room_eyebrow.setObjectName("eyebrow")
        room_title = QLabel("Room")
        room_title.setObjectName("roomTitle")
        title_box.addWidget(room_eyebrow)
        title_box.addWidget(room_title)

        self.badge_row = QBoxLayout(QBoxLayout.LeftToRight)
        self.badge_row.setContentsMargins(0, 0, 0, 0)
        self.badge_row.setSpacing(10)
        self.connection_badge = QLabel("OFFLINE")
        self.connection_badge.setObjectName("offlineBadge")
        self.connection_badge.setMinimumHeight(30)
        self.configure_resizable_label(
            self.connection_badge,
            wrap=False,
            vertical_policy=QSizePolicy.Fixed,
        )
        self.room_badge = QLabel("Room: not connected")
        self.room_badge.setObjectName("softBadge")
        self.room_badge.setMinimumHeight(30)
        self.configure_resizable_label(
            self.room_badge,
            wrap=False,
            vertical_policy=QSizePolicy.Fixed,
        )
        self.members_badge = QLabel("0 viewers")
        self.members_badge.setObjectName("softBadge")
        self.members_badge.setMinimumHeight(30)
        self.configure_resizable_label(
            self.members_badge,
            wrap=False,
            vertical_policy=QSizePolicy.Fixed,
        )
        self.set_label_text_safe(self.room_badge, "Room: not connected", elide_mode=Qt.ElideRight)
        self.set_label_text_safe(self.members_badge, "0 viewers", elide_mode=Qt.ElideRight)
        self.badge_row.addWidget(self.connection_badge)
        self.badge_row.addWidget(self.room_badge)
        self.badge_row.addWidget(self.members_badge)
        self.badge_row.addStretch(1)

        self.leave_room_button = QPushButton("Leave Room")
        self.leave_room_button.setObjectName("ghostButton")
        self.leave_room_button.clicked.connect(self.leave_room)
        self.leave_room_button.setMinimumHeight(42)
        self.leave_room_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self.room_header_row.addLayout(title_box, 1)
        self.room_header_row.addWidget(self.leave_room_button)

        self.room_stat_row = QBoxLayout(QBoxLayout.LeftToRight)
        self.room_stat_row.setContentsMargins(0, 0, 0, 0)
        self.room_stat_row.setSpacing(12)
        self.room_status_card = self.build_stat_card(
            "STATUS", "Offline", "", "accentStatCard"
        )
        self.room_name_stat = self.build_stat_card(
            "ROOM", "not connected", "", "statCard"
        )
        self.viewer_stat = self.build_stat_card(
            "VIEWERS", "0", "", "statCard"
        )
        self.room_stat_row.addWidget(self.room_status_card)
        self.room_stat_row.addWidget(self.room_name_stat)
        self.room_stat_row.addWidget(self.viewer_stat)
        self.set_stat_card_text(
            self.room_status_card,
            value="Offline",
            caption="room sync disconnected",
        )
        self.set_stat_card_text(
            self.room_name_stat,
            value="not connected",
            caption="join a room to begin",
            value_elide_mode=Qt.ElideMiddle,
        )
        self.set_stat_card_text(
            self.viewer_stat,
            value="0",
            caption="connected room members",
            caption_elide_mode=Qt.ElideRight,
        )

        hero_layout.addLayout(self.room_header_row)
        hero_layout.addLayout(self.badge_row)
        hero_layout.addLayout(self.room_stat_row)

        stage_card, stage_layout = self.build_panel(
            "stageCard",
            margins=(24, 24, 24, 24),
            spacing=16,
            glow_color="#11131f",
            glow_alpha=135,
            blur=40,
            offset_y=16,
        )

        stage_header = QHBoxLayout()
        stage_header.setContentsMargins(0, 0, 0, 0)
        stage_header.setSpacing(10)
        stage_header_text = QVBoxLayout()
        stage_header_text.setContentsMargins(0, 0, 0, 0)
        stage_header_text.setSpacing(2)
        stage_overline = QLabel("PLAYBACK")
        stage_overline.setObjectName("sectionOverline")
        stage_title = QLabel("Playback")
        stage_title.setObjectName("sectionTitle")
        stage_title.setWordWrap(True)
        stage_header_text.addWidget(stage_overline)
        stage_header_text.addWidget(stage_title)
        stage_header.addLayout(stage_header_text, 1)

        self.player_hint = QLabel("mpv window")
        self.player_hint.setObjectName("playerHint")
        self.player_hint.setAlignment(Qt.AlignLeft)
        self.current_media_label = QLabel("No media loaded yet")
        self.current_media_label.setObjectName("mediaLabel")
        self.configure_resizable_label(self.current_media_label, wrap=False)
        self.current_media_label.setMinimumHeight(18)
        self.set_label_text_safe(
            self.current_media_label,
            "No media loaded yet",
            elide_mode=Qt.ElideMiddle,
        )

        self.member_label = QLabel("Members: none")
        self.member_label.setObjectName("memberList")
        self.configure_resizable_label(self.member_label, wrap=False)
        self.member_label.setMinimumHeight(18)
        self.set_label_text_safe(self.member_label, "Members: none", elide_mode=Qt.ElideRight)

        media_surface, media_surface_layout = self.build_panel(
            "mediaSurface",
            margins=(22, 22, 22, 22),
            spacing=12,
            glow_color="#0e1020",
            glow_alpha=80,
            blur=24,
            offset_y=10,
        )
        media_surface_layout.setContentsMargins(20, 20, 20, 20)
        media_surface_layout.setSpacing(10)
        media_surface_layout.addWidget(self.player_hint)
        media_surface_layout.addWidget(self.current_media_label)
        media_surface_layout.addStretch(1)
        media_surface_layout.addWidget(self.member_label)

        stage_layout.addLayout(stage_header)
        stage_layout.addWidget(media_surface, 1)

        transport_card, transport_layout = self.build_panel(
            "transportCard",
            margins=(18, 18, 18, 18),
            spacing=10,
            glow_color="#101220",
            glow_alpha=105,
            blur=28,
            offset_y=10,
        )

        self.transport_top = QBoxLayout(QBoxLayout.LeftToRight)
        self.transport_top.setContentsMargins(0, 0, 0, 0)
        self.transport_top.setSpacing(12)
        self.play_button = QPushButton("Play")
        self.play_button.setObjectName("primaryButton")
        self.play_button.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self.play_button.clicked.connect(self.toggle_playback)
        self.position_label = QLabel("00:00")
        self.position_label.setObjectName("timeLabel")
        self.duration_label = QLabel("00:00")
        self.duration_label.setObjectName("timeLabel")

        self.position_slider = QSlider(Qt.Horizontal)
        self.position_slider.sliderPressed.connect(self.on_slider_pressed)
        self.position_slider.sliderReleased.connect(self.on_slider_released)

        self.transport_top.addWidget(self.play_button)
        self.transport_top.addWidget(self.position_label)
        self.transport_top.addWidget(self.position_slider, 1)
        self.transport_top.addWidget(self.duration_label)

        transport_layout.addLayout(self.transport_top)

        left_column.addWidget(hero_card)
        left_column.addWidget(stage_card, 1)
        left_column.addWidget(transport_card)

        side_content = QWidget()
        side_content.setObjectName("sidebarCanvas")
        side_layout = QVBoxLayout(side_content)
        side_layout.setContentsMargins(0, 0, 4, 0)
        side_layout.setSpacing(14)
        self.room_side_content = side_content
        self.room_side_layout = side_layout
        side_content.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        top_control_card, top_control_layout = self.build_panel(
            "sidebarPanel",
            margins=(20, 20, 20, 20),
            spacing=12,
            glow_color="#101220",
            glow_alpha=110,
            blur=28,
            offset_y=10,
        )

        side_title = QLabel("Stream")
        side_title.setObjectName("sectionTitle")
        side_overline = QLabel("SOURCE")
        side_overline.setObjectName("sectionOverline")

        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("Video URL")
        self.url_input.returnPressed.connect(self.load_media_from_input)

        top_control_layout.addWidget(side_overline)
        top_control_layout.addWidget(side_title)
        top_control_layout.addWidget(self.url_input)

        self.load_row = QBoxLayout(QBoxLayout.LeftToRight)
        self.load_row.setContentsMargins(0, 0, 0, 0)
        self.load_row.setSpacing(10)
        self.load_button = QPushButton("Load Link")
        self.load_button.setObjectName("primaryButton")
        self.load_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.load_button.clicked.connect(self.load_media_from_input)
        self.sync_hint_button = QPushButton("Copy Room Name")
        self.sync_hint_button.setObjectName("ghostButton")
        self.sync_hint_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.sync_hint_button.clicked.connect(self.copy_room_name_to_status)
        self.load_row.addWidget(self.load_button)
        self.load_row.addWidget(self.sync_hint_button)
        top_control_layout.addLayout(self.load_row)

        stream_note = QLabel("Direct stream links only. Playback stays local on each machine.")
        stream_note.setObjectName("inlineNote")
        stream_note.setWordWrap(True)
        top_control_layout.addWidget(stream_note)

        audio_card, audio_layout = self.build_panel(
            "sidebarPanel",
            margins=(20, 20, 20, 20),
            spacing=12,
            glow_color="#101220",
            glow_alpha=110,
            blur=28,
            offset_y=10,
        )

        audio_title = QLabel("Audio")
        audio_title.setObjectName("sectionTitle")
        audio_overline = QLabel("TRACKS")
        audio_overline.setObjectName("sectionOverline")

        self.audio_pref_input = QLineEdit(self.settings.get("audio_preferences", "eng,en,english"))
        self.audio_pref_input.setPlaceholderText("Preferred audio")

        audio_layout.addWidget(audio_overline)
        audio_layout.addWidget(audio_title)
        audio_layout.addWidget(self.audio_pref_input)

        self.audio_pref_row = QBoxLayout(QBoxLayout.LeftToRight)
        self.audio_pref_row.setContentsMargins(0, 0, 0, 0)
        self.audio_pref_row.setSpacing(10)
        self.prefer_english_button = QPushButton("English")
        self.prefer_english_button.setObjectName("pillButton")
        self.prefer_english_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.prefer_english_button.clicked.connect(
            lambda: self.set_audio_preferences("eng,en,english")
        )
        self.prefer_japanese_button = QPushButton("Japanese")
        self.prefer_japanese_button.setObjectName("pillButton")
        self.prefer_japanese_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.prefer_japanese_button.clicked.connect(
            lambda: self.set_audio_preferences("jp,jpn,japanese,jap")
        )
        self.audio_pref_row.addWidget(self.prefer_english_button)
        self.audio_pref_row.addWidget(self.prefer_japanese_button)
        audio_layout.addLayout(self.audio_pref_row)

        self.audio_track_combo = QComboBox()
        self.audio_track_combo.setMinimumWidth(0)
        self.audio_track_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.audio_track_combo.setObjectName("elevatedCombo")
        audio_layout.addWidget(self.audio_track_combo)

        self.audio_action_row = QBoxLayout(QBoxLayout.LeftToRight)
        self.audio_action_row.setContentsMargins(0, 0, 0, 0)
        self.audio_action_row.setSpacing(10)
        self.refresh_audio_button = QPushButton("Refresh")
        self.refresh_audio_button.setObjectName("ghostButton")
        self.refresh_audio_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.refresh_audio_button.clicked.connect(self.refresh_audio_tracks)
        self.use_audio_button = QPushButton("Use Selected")
        self.use_audio_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.use_audio_button.clicked.connect(self.use_selected_audio_track)
        self.audio_action_row.addWidget(self.refresh_audio_button)
        self.audio_action_row.addWidget(self.use_audio_button)
        audio_layout.addLayout(self.audio_action_row)

        side_layout.addWidget(top_control_card)
        side_layout.addWidget(audio_card)
        side_layout.addStretch(1)

        self.room_shell_layout.addLayout(left_column, 7)
        self.room_shell_layout.addWidget(side_content, 4)
        layout.addLayout(self.room_shell_layout)
        return page

    def build_field_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("fieldLabel")
        label.setMinimumHeight(16)
        label.setMinimumWidth(0)
        label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        return label

    def rebuild_join_grid(self, compact: bool) -> None:
        while self.join_grid.count():
            item = self.join_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)

        if compact:
            self.join_grid.addWidget(self.join_host_profile_label, 0, 0)
            self.join_grid.addWidget(self.host_select, 1, 0)
            self.join_grid.addWidget(self.join_host_label, 2, 0)
            self.join_grid.addWidget(self.host_input, 3, 0)
            self.join_grid.addWidget(self.join_port_label, 4, 0)
            self.join_grid.addWidget(self.port_input, 5, 0)
            self.join_grid.addWidget(self.join_room_label, 6, 0)
            self.join_grid.addWidget(self.room_input, 7, 0)
            self.join_grid.addWidget(self.join_password_label, 8, 0)
            self.join_grid.addWidget(self.password_input, 9, 0)
            self.join_grid.addWidget(self.join_name_label, 10, 0)
            self.join_grid.addWidget(self.name_input, 11, 0)
            return

        self.join_grid.addWidget(self.join_host_profile_label, 0, 0, 1, 2)
        self.join_grid.addWidget(self.host_select, 1, 0, 1, 2)
        self.join_grid.addWidget(self.join_host_label, 2, 0, 1, 2)
        self.join_grid.addWidget(self.host_input, 3, 0, 1, 2)
        self.join_grid.addWidget(self.join_port_label, 4, 0)
        self.join_grid.addWidget(self.join_room_label, 4, 1)
        self.join_grid.addWidget(self.port_input, 5, 0)
        self.join_grid.addWidget(self.room_input, 5, 1)
        self.join_grid.addWidget(self.join_password_label, 6, 0, 1, 2)
        self.join_grid.addWidget(self.password_input, 7, 0, 1, 2)
        self.join_grid.addWidget(self.join_name_label, 8, 0, 1, 2)
        self.join_grid.addWidget(self.name_input, 9, 0, 1, 2)

    def build_stat_card(
        self,
        eyebrow: str,
        value: str,
        caption: str,
        object_name: str = "statCard",
    ) -> QFrame:
        card = QFrame()
        card.setObjectName(object_name)
        card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        card.setMinimumHeight(86 if object_name in {"statCard", "accentStatCard"} else 72)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(6)

        eyebrow_label = QLabel(eyebrow)
        eyebrow_label.setObjectName("statEyebrow")
        value_label = QLabel(value)
        value_label.setObjectName("statValue")
        self.configure_resizable_label(value_label, wrap=False)
        caption_label = QLabel(caption)
        caption_label.setObjectName("statCaption")
        self.configure_resizable_label(caption_label, wrap=True)
        caption_label.setVisible(bool(caption.strip()))

        layout.addWidget(eyebrow_label)
        layout.addWidget(value_label)
        layout.addWidget(caption_label)
        card.value_label = value_label  # type: ignore[attr-defined]
        card.caption_label = caption_label  # type: ignore[attr-defined]
        if object_name == "accentStatCard":
            self.apply_depth_effect(card, "#f1f1f4", 38, 24, 8)
        elif object_name == "miniStatCard":
            self.apply_depth_effect(card, "#0d1020", 70, 18, 6)
        else:
            self.apply_depth_effect(card, "#0c0f1b", 82, 22, 7)
        return card

    def update_page_chrome(self, index: int) -> None:
        lobby_active = index == 0
        room_active = index == 1
        self.set_nav_chip_active(self.lobby_nav_chip, lobby_active)
        self.set_nav_chip_active(self.room_nav_chip, room_active)

    def compute_ui_scale(self) -> float:
        width = max(self.width(), 1)
        height = max(self.height(), 1)
        if width < 820 or height < 580:
            return 0.86
        if width < 980 or height < 680:
            return 0.93
        return 1.0

    def refresh_scaled_ui(self, force: bool = False) -> None:
        scale = self.compute_ui_scale()
        if force or self._last_ui_scale < 0 or abs(scale - self._last_ui_scale) > 0.001:
            self._last_ui_scale = scale
            self.apply_theme(scale)
        self.update_responsive_layouts()

    def update_responsive_layouts(self) -> None:
        width = self.width()
        compact_join = width < 940
        room_stacked = width < 1100
        room_compact = width < 850

        outer_margin = 10 if room_compact else 12 if room_stacked else 14
        outer_spacing = 10 if room_compact else 12
        if hasattr(self, "root_layout"):
            self.root_layout.setContentsMargins(outer_margin, outer_margin, outer_margin, 14)
            self.root_layout.setSpacing(outer_spacing)
        if hasattr(self, "join_page_layout"):
            self.join_page_layout.setContentsMargins(6 if room_compact else 8, 6 if room_compact else 8, 6 if room_compact else 8, 8)
            self.join_page_layout.setSpacing(10 if room_compact else 12)
        if hasattr(self, "room_page_layout"):
            room_margin = 6 if room_compact else 8
            self.room_page_layout.setContentsMargins(room_margin, room_margin, room_margin, room_margin)
        if hasattr(self, "join_card"):
            self.join_card.setMaximumWidth(980 if width >= 1180 else 860 if width >= 920 else 16777215)
        if hasattr(self, "join_grid"):
            last_state = self.join_grid.property("compact")
            if last_state is None or bool(last_state) != compact_join:
                self.join_grid.setProperty("compact", compact_join)
                self.rebuild_join_grid(compact_join)

        if hasattr(self, "top_bar_row"):
            self.top_bar_row.setSpacing(10 if room_compact else 12)
        if hasattr(self, "join_shell_layout"):
            self.join_shell_layout.setSpacing(12 if room_compact else 16)
        if hasattr(self, "room_shell_layout"):
            self.room_shell_layout.setDirection(QBoxLayout.TopToBottom if room_stacked else QBoxLayout.LeftToRight)
            self.room_shell_layout.setSpacing(12 if room_compact else 14 if room_stacked else 18)
        if hasattr(self, "room_header_row"):
            self.room_header_row.setDirection(QBoxLayout.TopToBottom if width < 920 else QBoxLayout.LeftToRight)
            self.room_header_row.setSpacing(10)
        if hasattr(self, "badge_row"):
            self.badge_row.setDirection(QBoxLayout.TopToBottom if width < 980 else QBoxLayout.LeftToRight)
            self.badge_row.setSpacing(8 if room_compact else 10)
        if hasattr(self, "room_stat_row"):
            self.room_stat_row.setDirection(QBoxLayout.TopToBottom if width < 980 else QBoxLayout.LeftToRight)
            self.room_stat_row.setSpacing(10)
        if hasattr(self, "transport_top"):
            self.transport_top.setDirection(QBoxLayout.TopToBottom if width < 900 else QBoxLayout.LeftToRight)
            self.transport_top.setSpacing(10)
        if hasattr(self, "load_row"):
            self.load_row.setDirection(QBoxLayout.TopToBottom if width < 960 else QBoxLayout.LeftToRight)
            self.load_row.setSpacing(10)
        if hasattr(self, "audio_pref_row"):
            self.audio_pref_row.setDirection(QBoxLayout.TopToBottom if width < 900 else QBoxLayout.LeftToRight)
            self.audio_pref_row.setSpacing(10)
        if hasattr(self, "audio_action_row"):
            self.audio_action_row.setDirection(QBoxLayout.TopToBottom if width < 960 else QBoxLayout.LeftToRight)
            self.audio_action_row.setSpacing(10)
        if hasattr(self, "join_button_row"):
            self.join_button_row.setDirection(QBoxLayout.TopToBottom if width < 760 else QBoxLayout.LeftToRight)
            self.join_button_row.setSpacing(10)
        if hasattr(self, "room_side_content"):
            self.room_side_content.setMinimumWidth(0)
            self.room_side_content.setMaximumWidth(16777215 if room_stacked else 420)
        self.update_elided_labels()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self.refresh_scaled_ui()

    def apply_theme(self, scale: float = 1.0) -> None:
        title_px = max(24, round(28 * scale))
        hero_px = max(25, round(30 * scale))
        room_px = max(20, round(22 * scale))
        section_px = max(15, round(17 * scale))
        body_px = max(12, round(13 * scale))
        note_px = max(11, round(12 * scale))
        stat_px = max(17, round(19 * scale))
        field_px = max(10, round(11 * scale))
        control_h = max(38, round(42 * scale))
        pill_h = max(36, round(42 * scale))
        radius = max(14, round(18 * scale))
        nav_h = max(32, round(36 * scale))
        shell_radius = max(22, round(28 * scale))
        content_radius = max(18, round(24 * scale))
        panel_radius = max(18, round(24 * scale))
        media_radius = max(16, round(20 * scale))
        nav_radius = max(16, round(20 * scale))
        card_radius = max(14, round(18 * scale))
        mini_radius = max(12, round(16 * scale))
        nav_pad = max(12, round(16 * scale))
        nav_chip_radius = max(14, round(18 * scale))
        badge_radius = max(12, round(15 * scale))
        badge_vpad = max(5, round(7 * scale))
        badge_hpad = max(10, round(12 * scale))
        input_pad = max(10, round(14 * scale))
        button_pad = max(14, round(18 * scale))
        pill_radius = max(14, round(18 * scale))
        slider_h = max(7, round(9 * scale))
        slider_radius = max(4, round(5 * scale))
        handle_w = max(16, round(18 * scale))
        handle_radius = max(8, round(9 * scale))
        combo_pad = max(28, round(32 * scale))
        combo_dropdown_w = max(26, round(30 * scale))
        menu_radius = max(12, round(14 * scale))
        stylesheet = """
            QWidget {
                color: #f4f4f6;
                font-family: "Noto Sans", "Cantarell", sans-serif;
                font-size: __BODY_PX__px;
                background: transparent;
            }
            QMainWindow, QWidget#rootCanvas {
                background: qradialgradient(
                    cx: 0.14, cy: 0.08, radius: 1.2,
                    fx: 0.14, fy: 0.08,
                    stop: 0 rgba(28, 28, 32, 0.22),
                    stop: 0.26 rgba(10, 10, 11, 0.92),
                    stop: 1 rgba(0, 0, 0, 1.0)
                );
            }
            QWidget#dashboardPage, QStackedWidget#pageStack, QWidget#sidebarCanvas {
                background: transparent;
            }
            QScrollArea, QScrollArea > QWidget {
                background: transparent;
                border: 0;
            }
            QFrame#dashboardShell {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 rgba(11, 11, 12, 0.99),
                    stop:0.55 rgba(15, 15, 16, 0.97),
                    stop:1 rgba(9, 9, 9, 0.99));
                border: 1px solid rgba(84, 84, 88, 0.34);
                border-radius: __SHELL_RADIUS__px;
            }
            QFrame#contentShell {
                background: rgba(10, 10, 10, 0.38);
                border: 1px solid rgba(58, 58, 62, 0.20);
                border-radius: __CONTENT_RADIUS__px;
            }
            QFrame#topBar, QFrame#heroPanel, QFrame#controlPanel, QFrame#stageCard, QFrame#transportCard, QFrame#sidebarPanel, QFrame#innerPanel {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 rgba(20, 20, 21, 0.94),
                    stop:1 rgba(12, 12, 13, 0.86));
                border: 1px solid rgba(72, 72, 76, 0.26);
                border-radius: __PANEL_RADIUS__px;
            }
            QFrame#mediaSurface {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 rgba(7, 7, 7, 0.98),
                    stop:0.60 rgba(11, 11, 12, 0.94),
                    stop:1 rgba(18, 18, 19, 0.92));
                border: 1px solid rgba(58, 58, 62, 0.24);
                border-radius: __MEDIA_RADIUS__px;
            }
            QFrame#navCapsule {
                background: rgba(12, 12, 13, 0.96);
                border: 1px solid rgba(70, 70, 74, 0.28);
                border-radius: __NAV_RADIUS__px;
            }
            QFrame#statCard, QFrame#accentStatCard, QFrame#miniStatCard {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 rgba(18, 18, 19, 0.94),
                    stop:1 rgba(11, 11, 12, 0.82));
                border: 1px solid rgba(66, 66, 70, 0.24);
                border-radius: __CARD_RADIUS__px;
            }
            QFrame#accentStatCard {
                border: 1px solid rgba(98, 98, 104, 0.34);
            }
            QFrame#miniStatCard {
                border-radius: __MINI_RADIUS__px;
            }
            QLabel#navChip {
                min-height: __NAV_H__px;
                padding: 0 __NAV_PAD__px;
                border-radius: __NAV_CHIP_RADIUS__px;
                color: rgba(214, 214, 216, 0.76);
                background: transparent;
                border: 1px solid transparent;
                font-weight: 700;
            }
            QLabel#navChip[active="true"] {
                color: #ffffff;
                background: rgba(36, 36, 38, 0.96);
                border: 1px solid rgba(118, 118, 122, 0.36);
            }
            QLabel#offlineBadge {
                background: rgba(28, 28, 29, 0.94);
                border: 1px solid rgba(90, 90, 94, 0.28);
                color: #f0f0f2;
                border-radius: __BADGE_RADIUS__px;
                padding: __BADGE_VPAD__px __BADGE_HPAD__px;
                font-weight: 700;
            }
            QLabel#onlineBadge {
                background: rgba(30, 30, 32, 0.94);
                border: 1px solid rgba(124, 124, 130, 0.30);
                color: #f5f5f7;
                border-radius: __BADGE_RADIUS__px;
                padding: __BADGE_VPAD__px __BADGE_HPAD__px;
                font-weight: 700;
            }
            QLabel#softBadge {
                background: rgba(20, 20, 21, 0.94);
                border: 1px solid rgba(78, 78, 82, 0.26);
                color: #ededf0;
                border-radius: __BADGE_RADIUS__px;
                padding: __BADGE_VPAD__px __BADGE_HPAD__px;
                font-weight: 700;
            }
            QLabel#eyebrow, QLabel#sectionOverline, QLabel#fieldLabel, QLabel#statEyebrow {
                color: #b8b8bf;
                font-size: __FIELD_PX__px;
                font-weight: 800;
                letter-spacing: 0.12em;
                text-transform: uppercase;
            }
            QLabel#title {
                color: #ffffff;
                font-size: __TITLE_PX__px;
                font-weight: 800;
            }
            QLabel#heroTitle {
                color: #ffffff;
                font-size: __HERO_PX__px;
                font-weight: 800;
            }
            QLabel#roomTitle {
                color: #ffffff;
                font-size: __ROOM_PX__px;
                font-weight: 800;
            }
            QLabel#sectionTitle {
                color: #fbfcff;
                font-size: __SECTION_PX__px;
                font-weight: 800;
            }
            QLabel#bodyText, QLabel#supportingText {
                color: rgba(198, 198, 202, 0.84);
                font-size: __BODY_PX__px;
            }
            QLabel#inlineNote, QLabel#statCaption, QLabel#mediaLabel, QLabel#memberList {
                color: rgba(168, 168, 174, 0.82);
                font-size: __NOTE_PX__px;
            }
            QLabel#playerHint {
                color: #ffffff;
                font-size: __SECTION_PX__px;
                font-weight: 800;
            }
            QLabel#statValue {
                color: #ffffff;
                font-size: __STAT_PX__px;
                font-weight: 800;
            }
            QLabel#statCaption {
                min-height: 0px;
            }
            QLabel#timeLabel {
                color: #f7f7f8;
                font-size: __NOTE_PX__px;
                font-weight: 800;
                min-width: 44px;
            }
            QLineEdit, QComboBox {
                min-width: 0px;
                min-height: __CONTROL_H__px;
                border-radius: __RADIUS__px;
                border: 1px solid rgba(84, 84, 88, 0.28);
                background: rgba(8, 8, 9, 0.96);
                color: #ffffff;
                padding: 0 __INPUT_PAD__px;
                selection-background-color: rgba(160, 160, 168, 0.40);
            }
            QLineEdit:hover, QComboBox:hover {
                border: 1px solid rgba(110, 110, 116, 0.40);
                background: rgba(12, 12, 13, 0.98);
            }
            QLineEdit:focus, QComboBox:focus {
                border: 1px solid rgba(174, 174, 180, 0.54);
                background: rgba(14, 14, 15, 0.98);
                selection-background-color: rgba(160, 160, 168, 0.46);
            }
            QPushButton {
                min-width: 0px;
                min-height: __CONTROL_H__px;
                border-radius: __RADIUS__px;
                border: 1px solid rgba(86, 86, 90, 0.28);
                padding: 0 __BUTTON_PAD__px;
                font-weight: 800;
                color: #fbfbff;
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 rgba(17, 17, 18, 0.98),
                    stop:1 rgba(10, 10, 10, 0.92));
            }
            QPushButton:hover {
                border: 1px solid rgba(122, 122, 128, 0.40);
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 rgba(21, 21, 22, 0.98),
                    stop:1 rgba(14, 14, 14, 0.94));
            }
            QPushButton:pressed {
                padding-top: 1px;
                background: rgba(9, 9, 10, 0.98);
            }
            QPushButton:focus {
                border: 1px solid rgba(184, 184, 190, 0.62);
            }
            QPushButton#primaryButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 rgba(244, 244, 246, 0.94),
                    stop:1 rgba(188, 188, 192, 0.92));
                border: 1px solid rgba(224, 224, 228, 0.58);
                color: #060606;
            }
            QPushButton#primaryButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 rgba(255, 255, 255, 0.98),
                    stop:1 rgba(205, 205, 208, 0.96));
            }
            QPushButton#primaryButton:pressed {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 rgba(210, 210, 213, 0.96),
                    stop:1 rgba(174, 174, 178, 0.94));
            }
            QPushButton#ghostButton, QPushButton#pillButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 rgba(16, 16, 17, 0.92),
                    stop:1 rgba(11, 11, 11, 0.88));
                border: 1px solid rgba(76, 76, 80, 0.26);
                color: #f1f3ff;
            }
            QPushButton#ghostButton:hover, QPushButton#pillButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 rgba(20, 20, 21, 0.98),
                    stop:1 rgba(14, 14, 14, 0.92));
                border: 1px solid rgba(108, 108, 114, 0.34);
            }
            QPushButton#pillButton {
                min-height: __PILL_H__px;
                border-radius: __PILL_RADIUS__px;
            }
            QSlider::groove:horizontal {
                height: __SLIDER_H__px;
                border-radius: __SLIDER_RADIUS__px;
                background: rgba(20, 20, 21, 0.98);
            }
            QSlider::sub-page:horizontal {
                border-radius: __SLIDER_RADIUS__px;
                background: rgba(230, 230, 234, 0.90);
            }
            QSlider::add-page:horizontal {
                border-radius: __SLIDER_RADIUS__px;
                background: rgba(32, 32, 34, 0.96);
            }
            QSlider::handle:horizontal {
                width: __HANDLE_W__px;
                margin: -6px 0;
                border-radius: __HANDLE_RADIUS__px;
                background: #f7f7f8;
                border: 2px solid rgba(92, 92, 96, 0.90);
            }
            QComboBox {
                padding-right: __COMBO_PAD__px;
            }
            QComboBox::drop-down {
                subcontrol-origin: padding;
                subcontrol-position: top right;
                width: __COMBO_DROPDOWN_W__px;
                border: 0;
                background: transparent;
            }
            QComboBox::down-arrow {
                width: 0px;
                height: 0px;
                border-left: 5px solid transparent;
                border-right: 5px solid transparent;
                border-top: 7px solid rgba(216, 216, 218, 0.92);
                margin-right: 12px;
            }
            QComboBox QAbstractItemView {
                background: rgba(10, 10, 10, 0.99);
                border: 1px solid rgba(92, 92, 96, 0.34);
                border-radius: __MENU_RADIUS__px;
                selection-background-color: rgba(78, 78, 82, 0.86);
                selection-color: #ffffff;
                color: #f5f7ff;
                outline: 0;
                padding: 6px;
            }
            QStatusBar {
                background: rgba(4, 4, 4, 0.99);
                color: rgba(210, 210, 214, 0.88);
                border-top: 1px solid rgba(54, 54, 58, 0.20);
            }
            QStatusBar::item {
                border: 0;
            }
            """
        replacements = {
            "__BODY_PX__": str(body_px),
            "__SHELL_RADIUS__": str(shell_radius),
            "__CONTENT_RADIUS__": str(content_radius),
            "__PANEL_RADIUS__": str(panel_radius),
            "__MEDIA_RADIUS__": str(media_radius),
            "__NAV_RADIUS__": str(nav_radius),
            "__CARD_RADIUS__": str(card_radius),
            "__MINI_RADIUS__": str(mini_radius),
            "__NAV_H__": str(nav_h),
            "__NAV_PAD__": str(nav_pad),
            "__NAV_CHIP_RADIUS__": str(nav_chip_radius),
            "__BADGE_RADIUS__": str(badge_radius),
            "__BADGE_VPAD__": str(badge_vpad),
            "__BADGE_HPAD__": str(badge_hpad),
            "__FIELD_PX__": str(field_px),
            "__TITLE_PX__": str(title_px),
            "__HERO_PX__": str(hero_px),
            "__ROOM_PX__": str(room_px),
            "__SECTION_PX__": str(section_px),
            "__NOTE_PX__": str(note_px),
            "__STAT_PX__": str(stat_px),
            "__CONTROL_H__": str(control_h),
            "__RADIUS__": str(radius),
            "__INPUT_PAD__": str(input_pad),
            "__BUTTON_PAD__": str(button_pad),
            "__PILL_H__": str(pill_h),
            "__PILL_RADIUS__": str(pill_radius),
            "__SLIDER_H__": str(slider_h),
            "__SLIDER_RADIUS__": str(slider_radius),
            "__HANDLE_W__": str(handle_w),
            "__HANDLE_RADIUS__": str(handle_radius),
            "__COMBO_PAD__": str(combo_pad),
            "__COMBO_DROPDOWN_W__": str(combo_dropdown_w),
            "__MENU_RADIUS__": str(menu_radius),
        }
        for token, value in replacements.items():
            stylesheet = stylesheet.replace(token, value)
        self.setStyleSheet(stylesheet)

    def connect_to_room(self) -> None:
        host = self.selected_host()
        room = self.room_input.text().strip() or "movie-night"
        name = self.name_input.text().strip() or "guest"
        password = self.password_input.text()
        try:
            port = int(self.port_input.text().strip() or "24873")
        except ValueError:
            self.show_error("Port must be a number.")
            return
        self.persist_settings()
        self.player_seen_running_in_room = False
        self.sync_client.connect_to_server(host, port, room, name, password)
        self.show_status(f"Connecting to {host}:{port}...")

    def selected_host(self) -> str:
        if self.host_select.currentIndex() == 0:
            return self.HOSTED_SERVER_URL
        return self.host_input.text().strip() or "127.0.0.1"

    def on_host_mode_changed(self) -> None:
        hosted = self.host_select.currentIndex() == 0
        self.host_input.setEnabled(not hosted)
        if hosted:
            self.host_input.setText(self.HOSTED_SERVER_URL)
        else:
            if self.host_input.text().strip() == self.HOSTED_SERVER_URL:
                custom_saved = str(self.settings.get("host", "127.0.0.1"))
                self.host_input.setText(custom_saved if custom_saved != self.HOSTED_SERVER_URL else "127.0.0.1")

    def leave_room(self) -> None:
        self.sync_client.disconnect_from_server()
        self.player_seen_running_in_room = False
        self.clear_pending_room_sync()
        self.page_stack.setCurrentIndex(0)
        self.show_status("Left room")

    def load_media_from_input(self) -> None:
        media_url = self.url_input.text().strip()
        if not media_url:
            self.show_error("Paste a direct HTTP or HTTPS video URL first.")
            return
        self.set_media_url(media_url, broadcast=True)

    def copy_room_name_to_status(self) -> None:
        room_name = self.room_input.text().strip() or "movie-night"
        QApplication.clipboard().setText(room_name)
        self.show_status(f"Copied room name: {room_name}")

    def set_media_url(self, media_url: str, broadcast: bool) -> None:
        self.current_media_url = media_url
        self.set_label_text_safe(
            self.current_media_label,
            media_url or "No media loaded yet",
            elide_mode=Qt.ElideMiddle,
        )
        append_runtime_log(f"Loading media broadcast={broadcast}")
        try:
            self.suppress_sync = True
            if os.name == "nt" and self.player.needs_windows_runtime_install():
                self.show_status("First-time setup: downloading mpv for Windows...")
            self.player.load(media_url)
            self.position_slider.setRange(0, 0)
            self.position_slider.setValue(0)
            self.position_label.setText("00:00")
            self.duration_label.setText("00:00")
            self.last_known_playing = False
            self.behind_sync_detected_at = None
            self.player_seen_running_in_room = False
            self.last_polled_position_ms = None
            self.last_polled_playing = None
            self.last_poll_monotonic = None
            if not broadcast:
                self.update_room_sync_state("loading", "Waiting for stream...")
            self.pending_audio_attempts = 10
            self.show_status("Loaded video link in mpv")
            QTimer.singleShot(250, self.refresh_audio_tracks)
            QTimer.singleShot(450, self.apply_audio_preferences_with_retry)
            self.persist_settings()
        except Exception as exc:
            append_runtime_log(f"set_media_url failed: {exc}")
            self.show_error(
                f"Could not start mpv. SyncRoom could not launch or install the player runtime. Details: {exc}"
            )
        finally:
            self.suppress_sync = False
        if broadcast:
            self.sync_client.send_state(self.current_media_url, 0, False, reason="load")

    def remember_polled_state(
        self,
        position_ms: int,
        playing: bool,
        observed_at: float | None = None,
    ) -> None:
        self.last_polled_position_ms = int(position_ms)
        self.last_polled_playing = bool(playing)
        self.last_poll_monotonic = observed_at if observed_at is not None else time.monotonic()

    def detect_local_seek(self, position_ms: int, playing: bool, observed_at: float) -> bool:
        if self.last_polled_position_ms is None or self.last_poll_monotonic is None:
            return False

        expected = int(self.last_polled_position_ms)
        if self.last_polled_playing:
            elapsed_ms = max(0, int((observed_at - self.last_poll_monotonic) * 1000))
            expected += elapsed_ms

        threshold = (
            self.LOCAL_PLAYING_SEEK_THRESHOLD_MS
            if playing or bool(self.last_polled_playing)
            else self.LOCAL_PAUSED_SEEK_THRESHOLD_MS
        )
        return abs(int(position_ms) - expected) > threshold

    def apply_server_state(self, payload: dict, settle_mode: bool = False) -> bool:
        if not self.current_media_url:
            return False

        position_ms = int(payload.get("position_ms") or 0)
        playing = bool(payload.get("playing"))
        seek_token = int(payload.get("seek_token") or 0)
        event_id = int(payload.get("event_id") or 0)
        last_action = str(payload.get("last_action") or "").strip().lower()

        local = self.player.get_status()
        if not local.get("running", True):
            raise RuntimeError("mpv not running yet")
        if str(local.get("media_url") or "") != self.current_media_url:
            raise RuntimeError("media not loaded yet")
        if int(local.get("duration_ms") or 0) <= 0:
            raise RuntimeError("media metadata not ready yet")

        self.suppress_sync = True
        drift = int(local["position_ms"]) - position_ms
        is_new_event = event_id > self.last_applied_event_id
        explicit_seek = is_new_event and (seek_token > self.last_applied_seek_token or last_action in {"seek", "load"})
        explicit_pause = is_new_event and last_action == "pause"
        explicit_play = is_new_event and last_action == "play"
        changed = False

        if explicit_seek:
            self.player.seek_absolute(position_ms)
            self.last_applied_seek_token = max(self.last_applied_seek_token, seek_token)
            changed = True
            local = self.player.get_status()
            drift = int(local["position_ms"]) - position_ms

        if playing:
            if not bool(local["playing"]):
                if explicit_play or abs(drift) > 450 or settle_mode:
                    if abs(drift) > 450:
                        self.player.seek_absolute(position_ms)
                    self.player.play()
                    changed = True
            else:
                if drift > self.PLAYING_REWIND_THRESHOLD_MS:
                    self.player.seek_absolute(position_ms)
                    self.behind_sync_detected_at = None
                    changed = True
                elif drift < -self.PLAYING_FASTFORWARD_THRESHOLD_MS:
                    now = time.monotonic()
                    if self.behind_sync_detected_at is None:
                        self.behind_sync_detected_at = now
                    elif now - self.behind_sync_detected_at >= self.PLAYING_FASTFORWARD_GRACE_SECONDS:
                        self.player.seek_absolute(position_ms + 250)
                        self.behind_sync_detected_at = now + 1.5
                        changed = True
                else:
                    self.behind_sync_detected_at = None
        else:
            self.behind_sync_detected_at = None
            if bool(local["playing"]):
                if explicit_pause or abs(drift) > self.PAUSED_SYNC_THRESHOLD_MS or settle_mode:
                    if abs(drift) > self.PAUSED_SYNC_THRESHOLD_MS:
                        self.player.seek_absolute(position_ms)
                    self.player.pause()
                    changed = True
            elif abs(drift) > self.PAUSED_SYNC_THRESHOLD_MS:
                self.player.seek_absolute(position_ms)
                changed = True

        if is_new_event:
            self.last_applied_event_id = event_id

        verify = self.player.get_status()
        remaining_drift = abs(int(verify["position_ms"]) - position_ms)
        if playing != bool(verify["playing"]):
            raise RuntimeError("playback state not settled yet")
        if remaining_drift > (2200 if playing else 450):
            raise RuntimeError("position not settled yet")

        self.last_known_playing = playing
        self.remember_polled_state(int(verify["position_ms"]), bool(verify["playing"]))
        return changed

    @staticmethod
    def is_recoverable_sync_error(exc: Exception) -> bool:
        message = str(exc).strip().lower()
        recoverable_fragments = (
            "not ready yet",
            "not settled yet",
            "property unavailable",
            "mpv is not running",
            "mpv not running yet",
            "media not loaded yet",
            "no such file or directory",
            "no response from mpv",
        )
        return any(fragment in message for fragment in recoverable_fragments)

    def update_room_sync_state(self, state: str, note: str = "") -> None:
        self.room_sync_state = state
        self.room_sync_note = note
        if not hasattr(self, "room_status_card"):
            return

        value = "Offline"
        caption = "room sync disconnected"
        if state == "live":
            value = "Live"
            caption = note or "room sync active"
        elif state == "loading":
            value = "Loading"
            caption = note or "waiting for stream"
        elif state == "recovering":
            value = "Syncing"
            caption = note or "catching up to the room"
        elif state == "connected":
            value = "Connected"
            caption = note or "connected to the room"

        self.set_stat_card_text(self.room_status_card, value=value, caption=caption)

    def schedule_pending_room_sync(self, delay_ms: int | None = None) -> None:
        if self.pending_room_sync is None or self._pending_sync_retry_scheduled:
            return
        self._pending_sync_retry_scheduled = True
        retry_delay = delay_ms if delay_ms is not None else self.SYNC_RETRY_INTERVAL_MS

        def run_retry() -> None:
            self._pending_sync_retry_scheduled = False
            self.apply_pending_room_sync()

        QTimer.singleShot(retry_delay, run_retry)

    def start_pending_room_sync(self, payload: dict, state: str, note: str) -> None:
        self.pending_room_sync = dict(payload)
        self.pending_room_sync_attempts += 1
        self.update_room_sync_state(state, note)
        self.schedule_pending_room_sync(250 if self.pending_room_sync_attempts == 1 else None)

    def clear_pending_room_sync(self) -> None:
        self.pending_room_sync = None
        self.pending_room_sync_attempts = 0
        self._pending_sync_retry_scheduled = False
        if self.sync_client.socket.state() == QAbstractSocket.ConnectedState:
            self.update_room_sync_state("live", "room sync active")
        else:
            self.update_room_sync_state("idle")

    def apply_pending_room_sync(self) -> None:
        if not self.pending_room_sync or not self.current_media_url:
            return

        try:
            self.apply_server_state(self.pending_room_sync, settle_mode=True)
            self.clear_pending_room_sync()
            append_runtime_log("apply_pending_room_sync settled successfully")
        except Exception as exc:
            append_runtime_log(f"apply_pending_room_sync retry due to: {exc}")
            note = "Waiting for stream..." if self.pending_room_sync_attempts <= 4 else "Catching up to the room..."
            self.update_room_sync_state("recovering", note)
            self.schedule_pending_room_sync()
        finally:
            self.suppress_sync = False

    def toggle_playback(self) -> None:
        if self.pending_room_sync is not None:
            self.show_status("Wait for SyncRoom to finish catching up first")
            return
        try:
            status = self.player.get_status()
            target_playing = not bool(status["playing"])
            if status["playing"]:
                self.player.pause()
            else:
                self.player.play()
            self.local_playback_target = target_playing
            self.local_playback_override_until = time.monotonic() + 1.2
            if self.current_media_url:
                self.sync_client.send_state(
                    self.current_media_url,
                    int(status["position_ms"]),
                    target_playing,
                    reason="play" if target_playing else "pause",
                )
            self.last_known_playing = target_playing
        except Exception as exc:
            self.show_error(f"Could not control mpv: {exc}")

    def on_slider_pressed(self) -> None:
        self.dragging_slider = True

    def on_slider_released(self) -> None:
        self.dragging_slider = False
        if self.pending_room_sync is not None:
            self.show_status("Wait for SyncRoom to finish catching up first")
            return
        if self.current_media_url:
            try:
                status = self.player.get_status()
                target_position = int(self.position_slider.value())
                self.player.seek_absolute(target_position)
                self.local_seek_target_ms = target_position
                self.local_seek_override_until = time.monotonic() + 1.5
                self.position_label.setText(self.format_ms(target_position))
                self.sync_client.send_state(
                    self.current_media_url,
                    target_position,
                    bool(status["playing"]),
                    force_seek=True,
                    reason="seek",
                )
            except Exception as exc:
                self.show_error(f"Could not seek in mpv: {exc}")

    def poll_player_state(self) -> None:
        if not self.current_media_url:
            return
        try:
            status = self.player.get_status()
        except Exception as exc:
            append_runtime_log(f"poll_player_state status read failed: {exc}")
            return

        if not status.get("running", True):
            if (
                self.page_stack.currentIndex() == 1
                and self.player_seen_running_in_room
                and not self.pending_room_sync
                and not self.closing_for_mpv_exit
            ):
                append_runtime_log("mpv stopped while in room, closing SyncRoom")
                self.closing_for_mpv_exit = True
                self.show_status("mpv was closed, so SyncRoom is closing too")
                QTimer.singleShot(100, QApplication.instance().quit)
            return
        self.player_seen_running_in_room = True

        position = int(status["position_ms"])
        duration = int(status["duration_ms"])
        playing = bool(status["playing"])
        observed_at = time.monotonic()

        if not self.dragging_slider:
            self.position_slider.setValue(position)
        self.position_slider.setRange(0, max(0, duration))
        self.position_label.setText(self.format_ms(position))
        self.duration_label.setText(self.format_ms(duration))
        self.play_button.setIcon(
            self.style().standardIcon(QStyle.SP_MediaPause if playing else QStyle.SP_MediaPlay)
        )
        self.play_button.setText("Pause" if playing else "Play")

        if self.pending_room_sync is not None:
            self.update_room_sync_state(
                "recovering",
                "Waiting for playback to catch up..." if duration <= 0 else "Catching up to the room...",
            )
            self.last_known_playing = playing
            self.remember_polled_state(position, playing, observed_at)
            if duration > 0:
                self.schedule_pending_room_sync(120)
            return

        if self.suppress_sync:
            self.last_known_playing = playing
            self.remember_polled_state(position, playing, observed_at)
            return

        if time.monotonic() < self.local_seek_override_until:
            if not self.dragging_slider:
                self.position_slider.setValue(self.local_seek_target_ms)
            self.position_label.setText(self.format_ms(self.local_seek_target_ms))
            self.last_known_playing = playing
            self.remember_polled_state(position, playing, observed_at)
            return

        if time.monotonic() < self.local_playback_override_until:
            self.last_known_playing = playing
            self.remember_polled_state(position, playing, observed_at)
            return

        local_seeked = self.detect_local_seek(position, playing, observed_at)
        if local_seeked:
            self.local_seek_target_ms = position
            self.local_seek_override_until = observed_at + 1.2
            self.sync_client.send_state(
                self.current_media_url,
                position,
                playing,
                force_seek=True,
                reason="seek",
            )
            self.last_known_playing = playing
            self.remember_polled_state(position, playing, observed_at)
            return

        state_changed = playing != self.last_known_playing
        if state_changed:
            self.sync_client.send_state(
                self.current_media_url,
                position,
                playing,
                reason="play" if playing else "pause",
            )
        self.last_known_playing = playing
        self.remember_polled_state(position, playing, observed_at)

    def on_room_state(self, payload: dict) -> None:
        members = payload.get("members") or []
        names = ", ".join(member.get("name", "guest") for member in members) or "none"
        self.set_label_text_safe(
            self.member_label,
            f"Members: {names}",
            elide_mode=Qt.ElideRight,
        )
        self.current_member_count = len(members)
        self.set_label_text_safe(
            self.members_badge,
            f"{self.current_member_count} viewer{'s' if self.current_member_count != 1 else ''}",
            elide_mode=Qt.ElideRight,
        )
        self.set_stat_card_text(
            self.viewer_stat,
            value=str(self.current_member_count),
            caption=names,
            caption_elide_mode=Qt.ElideRight,
        )

        media_url = str(payload.get("media_url") or "")
        position_ms = int(payload.get("position_ms") or 0)
        updated_by = str(payload.get("updated_by") or "")
        self.last_room_payload = dict(payload)

        if not media_url:
            return

        if updated_by == self.sync_client.client_id:
            return

        if media_url != self.current_media_url:
            append_runtime_log(
                f"Late join or media switch detected current={self.current_media_url or '<none>'}"
            )
            self.url_input.setText(media_url)
            self.set_media_url(media_url, broadcast=False)
            self.start_pending_room_sync(payload, "loading", "Waiting for stream...")
            return

        if self.pending_room_sync is not None:
            self.pending_room_sync = dict(payload)
            self.update_room_sync_state("recovering", "Catching up to the room...")
            self.schedule_pending_room_sync(120)
            return

        if (
            time.monotonic() < self.local_seek_override_until
            and abs(position_ms - self.local_seek_target_ms) > 350
        ):
            return

        try:
            self.apply_server_state(payload, settle_mode=False)
            self.update_room_sync_state("live", "room sync active")
        except Exception as exc:
            append_runtime_log(f"on_room_state sync exception: {exc}")
            if self.is_recoverable_sync_error(exc):
                self.show_status("Waiting for playback to finish loading...")
                self.start_pending_room_sync(payload, "recovering", "Catching up to the room...")
            else:
                self.show_transient_error(f"Could not sync mpv state: {exc}")
        finally:
            self.suppress_sync = False

    def show_status(self, message: str) -> None:
        self.statusBar().showMessage(message, 4000)

    def show_error(self, message: str) -> None:
        self.statusBar().showMessage(message, 5000)
        QMessageBox.warning(self, "SyncRoom", message)

    def show_transient_error(self, message: str) -> None:
        self.statusBar().showMessage(message, 5000)

    def on_connection_change(self, connected: bool) -> None:
        self.update_connection_state(connected)
        if connected:
            self.closing_for_mpv_exit = False
            self.update_room_sync_state("connected", "waiting for room state")
            self.page_stack.setCurrentIndex(1)
            self.show_status("Connected to server")
        else:
            self.current_member_count = 0
            self.player_seen_running_in_room = False
            self.clear_pending_room_sync()
            self.last_room_payload = None
            self.set_label_text_safe(self.members_badge, "0 viewers", elide_mode=Qt.ElideRight)
            self.set_label_text_safe(self.member_label, "Members: none", elide_mode=Qt.ElideRight)
            self.set_stat_card_text(
                self.viewer_stat,
                value="0",
                caption="connected room members",
                caption_elide_mode=Qt.ElideRight,
            )
            self.page_stack.setCurrentIndex(0)
            self.show_status("Disconnected")

    def set_audio_preferences(self, value: str) -> None:
        self.audio_pref_input.setText(value)
        self.pending_audio_attempts = 10
        self.apply_audio_preferences_with_retry()
        self.refresh_audio_tracks()
        self.persist_settings()

    def apply_audio_preferences_with_retry(self) -> None:
        self.refresh_audio_tracks(silent=True)
        applied = self.apply_audio_preferences()
        if applied:
            self.pending_audio_attempts = 0
            QTimer.singleShot(300, self.refresh_audio_tracks)
            return
        if self.pending_audio_attempts > 0 and self.current_media_url:
            self.pending_audio_attempts -= 1
            QTimer.singleShot(450, self.apply_audio_preferences_with_retry)

    def apply_audio_preferences(self) -> bool:
        preferences = [
            item.strip() for item in self.audio_pref_input.text().split(",") if item.strip()
        ]
        if not preferences or not self.current_media_url:
            return False
        try:
            already_selected = self.player.selected_audio_matches_preferences(preferences)
            if already_selected:
                self.persist_settings()
                return True
            track = self.player.select_best_audio(preferences)
        except Exception as exc:
            self.show_transient_error(f"Could not apply audio preference: {exc}")
            return False
        if track:
            label = self.describe_audio_track(track)
            self.show_status(f"Using audio track: {label}")
            self.persist_settings()
            return True
        return False

    def refresh_audio_tracks(self, silent: bool = False) -> None:
        self.audio_track_combo.clear()
        if not self.current_media_url:
            self.audio_track_combo.addItem("No media loaded", None)
            return
        try:
            self.audio_tracks = self.player.list_audio_tracks()
        except Exception as exc:
            self.audio_track_combo.addItem("Could not read audio tracks", None)
            if not silent:
                self.show_transient_error(f"Could not read audio tracks: {exc}")
            return
        if not self.audio_tracks:
            self.audio_track_combo.addItem("No audio tracks found", None)
            return
        selected_index = 0
        for track in self.audio_tracks:
            suffix = " [selected]" if track.get("selected") else ""
            self.audio_track_combo.addItem(
                f"{self.describe_audio_track(track)}{suffix}",
                int(track["id"]),
            )
            if track.get("selected"):
                selected_index = self.audio_track_combo.count() - 1
        self.audio_track_combo.setCurrentIndex(selected_index)

    def use_selected_audio_track(self) -> None:
        track_id = self.audio_track_combo.currentData()
        if track_id is None:
            return
        try:
            self.player.set_audio_track(int(track_id))
            QTimer.singleShot(300, self.refresh_audio_tracks)
            self.show_status("Switched audio track")
        except Exception as exc:
            self.show_error(f"Could not switch audio track: {exc}")

    @staticmethod
    def describe_audio_track(track: dict) -> str:
        lang = str(track.get("lang") or "unknown")
        title = str(track.get("title") or "").strip()
        if title:
            return f"{lang} - {title}"
        return lang

    @staticmethod
    def format_ms(value: int) -> str:
        total_seconds = max(0, value // 1000)
        minutes, seconds = divmod(total_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.persist_settings()
        self.player.stop()
        self.shutdown_background_jobs()
        super().closeEvent(event)

    def start_update_check(self) -> None:
        if self.update_check_started:
            return
        self.update_check_started = True
        append_runtime_log("Starting automatic update check")
        worker = UpdateCheckWorker()
        thread = QThread(self)
        self._track_background_job(thread, worker)
        worker.moveToThread(thread)
        worker.finished.connect(self.on_update_check_complete)
        worker.finished.connect(thread.quit)
        thread.finished.connect(lambda: self._release_background_job(thread, worker))
        thread.started.connect(worker.run)
        thread.start()

    def on_update_check_complete(self, payload: object) -> None:
        self.update_check_started = False
        append_runtime_log(f"Automatic update check finished payload_type={type(payload).__name__}")
        if not isinstance(payload, UpdateInfo):
            append_update_log(f"Unexpected update check payload type: {type(payload).__name__}")
            return
        self.pending_update_info = payload
        append_update_log(
            "Update check completed "
            f"available={payload.available} latest={payload.latest_version or '<none>'} "
            f"asset={payload.asset_name or '<none>'} message={payload.message or '<none>'}"
        )
        if not payload.available:
            append_runtime_log("No update available")
            return
        if payload.latest_version == self.update_prompted_version:
            return
        append_runtime_log(f"Update available latest_version={payload.latest_version}")
        self.update_prompted_version = payload.latest_version
        self.prompt_for_update(payload)

    def prompt_for_update(self, info: UpdateInfo) -> None:
        packaged_windows_build = os.name == "nt" and getattr(sys, "frozen", False)
        if packaged_windows_build and info.asset_url:
            result = QMessageBox.question(
                self,
                "SyncRoom Update",
                (
                    f"SyncRoom {info.latest_version} is available.\n\n"
                    "Install it now? SyncRoom will close, update, and reopen automatically."
                ),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if result == QMessageBox.Yes:
                self.download_and_install_update(info)
            return

        if os.name == "nt" and getattr(sys, "frozen", False) and not info.asset_url:
            append_update_log(
                f"Update available for {info.latest_version}, but no installer asset was found in the release."
            )

        result = QMessageBox.information(
            self,
            "SyncRoom Update",
            (
                f"SyncRoom {info.latest_version} is available.\n\n"
                "Automatic in-app updating is currently only enabled for the packaged Windows installer build. "
                "The release page will open now."
            ),
            QMessageBox.Ok | QMessageBox.Cancel,
            QMessageBox.Ok,
        )
        if result == QMessageBox.Ok:
            QDesktopServices.openUrl(QUrl(info.download_url))

    def show_update_error(self, message: str) -> None:
        log_hint = f"See {update_log_path()} for details."
        self.statusBar().showMessage(message, 5000)
        QMessageBox.warning(self, "SyncRoom Update", f"{message}\n\n{log_hint}")

    def download_and_install_update(self, info: UpdateInfo) -> None:
        append_update_log(
            f"Starting update download latest_version={info.latest_version} asset={info.asset_name or '<none>'}"
        )
        dialog = UpdateProgressDialog()
        worker = UpdateDownloadWorker(info)
        thread = QThread(self)
        self._track_background_job(thread, worker)
        worker.moveToThread(thread)
        worker.progress.connect(dialog.set_progress)
        thread.started.connect(worker.run)

        state = {
            "failure_message": "",
            "downloaded_path": "",
            "accepted": False,
        }

        def remember_failure(message: str) -> None:
            state["failure_message"] = message
            dialog.set_progress(message, 0)
            append_update_log(f"Update download failed: {message}")
            thread.quit()
            dialog.reject()

        def remember_path(path: str) -> None:
            state["downloaded_path"] = path
            state["accepted"] = True
            dialog.set_progress("Preparing installer...", 100)
            append_update_log(f"Update downloaded successfully to {path}")
            thread.quit()
            dialog.accept()

        worker.failed.connect(remember_failure)
        worker.finished.connect(remember_path)
        thread.start()
        dialog.set_progress("Preparing update...", 0)
        result = dialog.exec()
        stopped_cleanly = stop_thread_with_timeout(
            thread,
            append_update_log,
            "Update download",
        )
        self._release_background_job(thread, worker)
        if result == QDialog.Accepted and not stopped_cleanly:
            state["failure_message"] = (
                "The update downloaded, but the download worker did not stop cleanly."
            )
            result = QDialog.Rejected

        if result != QDialog.Accepted:
            if state["downloaded_path"]:
                cleanup_update_download(Path(state["downloaded_path"]))
            self.show_update_error(
                state["failure_message"] or "SyncRoom could not download the update."
            )
            return

        self.update_installer_path = Path(state["downloaded_path"])
        self.launch_update_installer_and_exit(self.update_installer_path)

    def launch_update_installer_and_exit(self, installer_path: Path) -> None:
        if os.name != "nt" or not getattr(sys, "frozen", False):
            append_update_log("Automatic in-app updating requested outside packaged Windows build")
            self.show_update_error(
                "Automatic in-app updating is only available in the packaged Windows build."
            )
            return

        if not installer_path.exists() or installer_path.stat().st_size <= 0:
            append_update_log(f"Installer path invalid or empty: {installer_path}")
            self.show_update_error("The downloaded installer is missing or empty.")
            cleanup_update_download(installer_path)
            return

        app_path = Path(
            sys.executable if getattr(sys, "frozen", False) else Path(sys.argv[0]).resolve()
        )
        updater_path = app_path.with_name("SyncRoomUpdate.exe")
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        if not updater_path.exists():
            append_update_log(f"Bundled updater missing at {updater_path}")
            self.show_update_error("SyncRoomUpdate.exe was not found next to the app.")
            return
        append_update_log(
            f"Launching bundled updater updater={updater_path} installer={installer_path}"
        )
        try:
            subprocess.Popen(
                [
                    str(updater_path),
                    str(installer_path),
                    str(app_path),
                    str(os.getpid()),
                ],
                creationflags=creationflags,
                close_fds=True,
                cwd=str(app_path.parent),
            )
        except Exception as exc:
            append_update_log(f"Bundled updater launch failed: {exc}")
            self.show_update_error(f"Could not launch the updater: {exc}")
            cleanup_update_download(installer_path)
            return
        append_update_log("Bundled updater launch requested successfully")
        self.show_status("Closing SyncRoom so the updater can finish the install...")
        QTimer.singleShot(400, QApplication.instance().quit)

    def _track_background_job(self, thread: QThread, worker: QObject) -> None:
        self._active_threads.append(thread)
        self._active_workers.append(worker)

    def _release_background_job(self, thread: QThread, worker: QObject) -> None:
        if worker in self._active_workers:
            self._active_workers.remove(worker)
        if thread in self._active_threads:
            self._active_threads.remove(thread)
        dispose_qobject_safely(worker)
        dispose_qobject_safely(thread)

    def shutdown_background_jobs(self) -> None:
        for thread in list(self._active_threads):
            stop_thread_with_timeout(thread, append_runtime_log, "Background worker", 2000)
        self._active_threads.clear()
        self._active_workers.clear()

    def persist_settings(self) -> None:
        save_settings(
            {
                "host": self.host_input.text().strip(),
                "host_mode": self.CUSTOM_SERVER_LABEL if self.host_select.currentIndex() == 1 else self.HOSTED_SERVER_LABEL,
                "port": self.port_input.text().strip(),
                "room": self.room_input.text().strip(),
                "room_password": self.password_input.text(),
                "name": self.name_input.text().strip(),
                "audio_preferences": self.audio_pref_input.text().strip(),
            }
        )

    def update_connection_state(self, connected: bool) -> None:
        self.connection_badge.setText("CONNECTED" if connected else "OFFLINE")
        self.connection_badge.setObjectName("onlineBadge" if connected else "offlineBadge")
        self.connection_badge.style().unpolish(self.connection_badge)
        self.connection_badge.style().polish(self.connection_badge)
        room_name = self.room_input.text().strip() or "not connected"
        self.set_label_text_safe(
            self.room_badge,
            f"Room: {room_name if connected else 'not connected'}",
            elide_mode=Qt.ElideRight,
        )
        self.update_room_sync_state("connected" if connected else "idle")
        self.set_stat_card_text(
            self.room_name_stat,
            value=room_name if connected else "not connected",
            caption="share this room name with everyone" if connected else "join a room to begin",
            value_elide_mode=Qt.ElideMiddle,
        )


def report_startup_crash(exc: BaseException) -> None:
    details = traceback.format_exc()
    path = safe_logs_dir() / "startup-crash.log"
    try:
        path.write_text(details, encoding="utf-8")
    except Exception:
        pass
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                None,
                f"SyncRoom crashed while starting.\n\nA crash log was written to:\n{path}\n\n{exc}",
                "SyncRoom",
                0x10,
            )
        except Exception:
            pass


def main() -> None:
    try:
        try:
            configure_crash_logging()
            append_runtime_log(f"SyncRoom starting config_dir={app_config_dir()}")
        except Exception:
            pass
        app = QApplication(sys.argv)
        if not prepare_windows_runtime_if_needed():
            sys.exit(1)
        window = MainWindow()
        window.show()
        sys.exit(app.exec())
    except Exception as exc:
        report_startup_crash(exc)
        raise


if __name__ == "__main__":
    main()
