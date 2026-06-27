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

from PySide6.QtCore import QObject, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtNetwork import QAbstractSocket, QTcpSocket
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFrame,
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
    return logs_dir() / "update.log"


def append_update_log(message: str) -> None:
    _append_log(update_log_path(), message)


_FAULT_LOG_HANDLE = None
MAX_LOG_BYTES = 256 * 1024


def runtime_log_path() -> Path:
    return logs_dir() / "runtime.log"


def _append_log(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
        previous = path.with_suffix(path.suffix + ".1")
        try:
            if previous.exists():
                previous.unlink()
            path.replace(previous)
        except OSError:
            path.write_text("", encoding="utf-8")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
        handle.flush()


def append_runtime_log(message: str) -> None:
    _append_log(runtime_log_path(), message)


def append_crash_log(message: str) -> None:
    _append_log(logs_dir() / "crash.log", message)


def configure_crash_logging() -> None:
    global _FAULT_LOG_HANDLE
    log_root = logs_dir()
    crash_path = log_root / "crash.log"
    if crash_path.exists() and crash_path.stat().st_size > MAX_LOG_BYTES:
        crash_path.write_text("", encoding="utf-8")
    _FAULT_LOG_HANDLE = crash_path.open("a", encoding="utf-8", buffering=1)
    faulthandler.enable(_FAULT_LOG_HANDLE, all_threads=True)
    append_runtime_log(f"Logging initialized in {log_root}")

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
    dialog.set_progress("Preparing setup...", 0)
    result = dialog.exec()
    thread.wait()
    worker.deleteLater()
    thread.deleteLater()

    if result == QDialog.Accepted:
        return True

    QMessageBox.critical(
        None,
        "SyncRoom Setup",
        failure_message["text"] or "SyncRoom could not install mpv automatically.",
    )
    return False


class MainWindow(QMainWindow):
    PAUSED_SYNC_THRESHOLD_MS = 250
    PLAYING_REWIND_THRESHOLD_MS = 1400
    PLAYING_FASTFORWARD_THRESHOLD_MS = 1800
    PLAYING_FASTFORWARD_GRACE_SECONDS = 0.8
    LOCAL_PLAYING_SEEK_THRESHOLD_MS = 1200
    LOCAL_PAUSED_SEEK_THRESHOLD_MS = 350
    HOSTED_SERVER_LABEL = "Hosted server"
    CUSTOM_SERVER_LABEL = "Custom host"
    HOSTED_SERVER_URL = "syncroom1.justys.xyz"

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("SyncRoom")
        self.resize(1100, 720)
        self.setMinimumSize(860, 580)
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

        self.build_ui()
        self.apply_theme()

        self.heartbeat = QTimer(self)
        self.heartbeat.setInterval(350)
        self.heartbeat.timeout.connect(self.poll_player_state)
        self.heartbeat.start()
        if os.name == "nt":
            QTimer.singleShot(2000, self.start_update_check)

    def build_ui(self) -> None:
        central = QWidget(self)
        root = QVBoxLayout(central)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(12)

        self.page_stack = QStackedWidget()
        self.page_stack.addWidget(self.build_join_page())
        self.page_stack.addWidget(self.build_room_page())

        root.addWidget(self.page_stack)
        self.setCentralWidget(central)
        self.statusBar().showMessage("Ready")
        self.refresh_audio_tracks()

        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(QApplication.instance().quit)
        self.addAction(quit_action)

    def build_join_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        shell = QHBoxLayout()
        shell.setSpacing(16)

        intro_card = QFrame()
        intro_card.setObjectName("heroCard")
        intro_card.setMinimumWidth(360)
        intro_layout = QVBoxLayout(intro_card)
        intro_layout.setContentsMargins(28, 28, 28, 28)
        intro_layout.setSpacing(14)

        intro_eyebrow = QLabel("SYNCROOM")
        intro_eyebrow.setObjectName("eyebrow")
        intro_title = QLabel("Sync video with friends")
        intro_title.setObjectName("title")
        intro_title.setWordWrap(True)

        intro_chip_row = QHBoxLayout()
        intro_chip_row.setSpacing(10)
        self.join_port_badge = QLabel("Default port 24873")
        self.join_port_badge.setObjectName("softBadge")
        self.join_player_badge = QLabel("mpv powered")
        self.join_player_badge.setObjectName("softBadge")
        intro_chip_row.addWidget(self.join_port_badge)
        intro_chip_row.addWidget(self.join_player_badge)
        intro_chip_row.addStretch(1)

        stat_row = QHBoxLayout()
        stat_row.setSpacing(10)
        stat_row.addWidget(self.build_stat_card("PORT", "24873", "", "statCard"))
        stat_row.addWidget(self.build_stat_card("PLAYER", "mpv", "", "statCard"))
        stat_row.addWidget(self.build_stat_card("ROOM", "sync", "", "statCard"))

        intro_layout.addWidget(intro_eyebrow)
        intro_layout.addWidget(intro_title)
        intro_layout.addLayout(intro_chip_row)
        intro_layout.addLayout(stat_row)
        intro_layout.addStretch(1)

        card = QFrame()
        card.setObjectName("joinShell")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(28, 28, 28, 28)
        card_layout.setSpacing(16)

        eyebrow = QLabel("JOIN")
        eyebrow.setObjectName("eyebrow")
        title = QLabel("Connect")
        title.setObjectName("title")

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)

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

        grid.addWidget(self.host_select, 0, 0, 1, 2)
        grid.addWidget(self.host_input, 1, 0, 1, 2)
        grid.addWidget(self.port_input, 2, 0)
        grid.addWidget(self.room_input, 2, 1)
        grid.addWidget(self.password_input, 3, 0, 1, 2)
        grid.addWidget(self.name_input, 4, 0, 1, 2)
        self.on_host_mode_changed()

        button_row = QHBoxLayout()
        button_row.setSpacing(10)
        self.connect_button = QPushButton("Join Room")
        self.connect_button.setObjectName("primaryButton")
        self.connect_button.clicked.connect(self.connect_to_room)
        button_row.addWidget(self.connect_button)

        card_layout.addWidget(eyebrow)
        card_layout.addWidget(title)
        card_layout.addLayout(grid)
        card_layout.addLayout(button_row)
        card_layout.addStretch(1)

        shell.addWidget(intro_card, 5)
        shell.addWidget(card, 4)
        layout.addLayout(shell)
        return page

    def build_room_page(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(20)

        left_column = QVBoxLayout()
        left_column.setSpacing(18)

        hero_card = QFrame()
        hero_card.setObjectName("heroCard")
        hero_layout = QVBoxLayout(hero_card)
        hero_layout.setContentsMargins(24, 24, 24, 24)
        hero_layout.setSpacing(12)

        top_row = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(4)
        room_title = QLabel("Room")
        room_title.setObjectName("roomTitle")
        title_box.addWidget(room_title)

        badge_row = QHBoxLayout()
        badge_row.setSpacing(10)
        self.connection_badge = QLabel("OFFLINE")
        self.connection_badge.setObjectName("offlineBadge")
        self.room_badge = QLabel("Room: not connected")
        self.room_badge.setObjectName("softBadge")
        self.members_badge = QLabel("0 viewers")
        self.members_badge.setObjectName("softBadge")
        badge_row.addWidget(self.connection_badge)
        badge_row.addWidget(self.room_badge)
        badge_row.addWidget(self.members_badge)
        badge_row.addStretch(1)

        self.leave_room_button = QPushButton("Leave Room")
        self.leave_room_button.setObjectName("ghostButton")
        self.leave_room_button.clicked.connect(self.leave_room)

        top_row.addLayout(title_box, 1)
        top_row.addWidget(self.leave_room_button)

        stat_row = QHBoxLayout()
        stat_row.setSpacing(10)
        self.room_status_card = self.build_stat_card(
            "STATUS", "Offline", "", "accentStatCard"
        )
        self.room_name_stat = self.build_stat_card(
            "ROOM", "not connected", "", "statCard"
        )
        self.viewer_stat = self.build_stat_card(
            "VIEWERS", "0", "", "statCard"
        )
        stat_row.addWidget(self.room_status_card)
        stat_row.addWidget(self.room_name_stat)
        stat_row.addWidget(self.viewer_stat)

        hero_layout.addLayout(top_row)
        hero_layout.addLayout(badge_row)
        hero_layout.addLayout(stat_row)

        stage_card = QFrame()
        stage_card.setObjectName("stageCard")
        stage_layout = QVBoxLayout(stage_card)
        stage_layout.setContentsMargins(24, 24, 24, 24)
        stage_layout.setSpacing(12)

        self.player_hint = QLabel("mpv window")
        self.player_hint.setObjectName("playerHint")
        self.player_hint.setAlignment(Qt.AlignLeft)
        self.current_media_label = QLabel("No media loaded yet")
        self.current_media_label.setObjectName("mediaLabel")
        self.current_media_label.setWordWrap(True)

        self.member_label = QLabel("Members: none")
        self.member_label.setObjectName("memberList")
        self.member_label.setWordWrap(True)

        media_surface = QFrame()
        media_surface.setObjectName("mediaSurface")
        media_surface_layout = QVBoxLayout(media_surface)
        media_surface_layout.setContentsMargins(20, 20, 20, 20)
        media_surface_layout.setSpacing(10)
        media_surface_layout.addWidget(self.player_hint)
        media_surface_layout.addWidget(self.current_media_label)
        media_surface_layout.addStretch(1)
        media_surface_layout.addWidget(self.member_label)

        stage_layout.addWidget(media_surface, 1)

        transport_card = QFrame()
        transport_card.setObjectName("surfaceCard")
        transport_layout = QVBoxLayout(transport_card)
        transport_layout.setContentsMargins(18, 18, 18, 18)
        transport_layout.setSpacing(10)

        transport_top = QHBoxLayout()
        transport_top.setSpacing(12)
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

        transport_top.addWidget(self.play_button)
        transport_top.addWidget(self.position_label)
        transport_top.addWidget(self.position_slider, 1)
        transport_top.addWidget(self.duration_label)

        transport_layout.addLayout(transport_top)

        left_column.addWidget(hero_card)
        left_column.addWidget(stage_card, 1)
        left_column.addWidget(transport_card)

        side_scroll = QScrollArea()
        side_scroll.setObjectName("sideScroll")
        side_scroll.setWidgetResizable(True)
        side_scroll.setFrameShape(QFrame.NoFrame)
        side_scroll.setMinimumWidth(360)
        side_scroll.setMaximumWidth(400)

        side_content = QWidget()
        side_layout = QVBoxLayout(side_content)
        side_layout.setContentsMargins(0, 0, 4, 0)
        side_layout.setSpacing(12)

        top_control_card = QFrame()
        top_control_card.setObjectName("surfaceCard")
        top_control_layout = QVBoxLayout(top_control_card)
        top_control_layout.setContentsMargins(18, 18, 18, 18)
        top_control_layout.setSpacing(10)

        side_title = QLabel("Stream")
        side_title.setObjectName("sectionTitle")

        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("Video URL")
        self.url_input.returnPressed.connect(self.load_media_from_input)

        top_control_layout.addWidget(side_title)
        top_control_layout.addWidget(self.url_input)

        load_row = QHBoxLayout()
        load_row.setSpacing(10)
        self.load_button = QPushButton("Load Link")
        self.load_button.setObjectName("primaryButton")
        self.load_button.clicked.connect(self.load_media_from_input)
        self.sync_hint_button = QPushButton("Copy Room Name")
        self.sync_hint_button.setObjectName("ghostButton")
        self.sync_hint_button.clicked.connect(self.copy_room_name_to_status)
        load_row.addWidget(self.load_button)
        load_row.addWidget(self.sync_hint_button)
        top_control_layout.addLayout(load_row)

        audio_card = QFrame()
        audio_card.setObjectName("surfaceCard")
        audio_layout = QVBoxLayout(audio_card)
        audio_layout.setContentsMargins(18, 18, 18, 18)
        audio_layout.setSpacing(10)

        audio_title = QLabel("Audio")
        audio_title.setObjectName("sectionTitle")

        self.audio_pref_input = QLineEdit(self.settings.get("audio_preferences", "eng,en,english"))
        self.audio_pref_input.setPlaceholderText("Preferred audio")

        audio_layout.addWidget(audio_title)
        audio_layout.addWidget(self.audio_pref_input)

        audio_pref_row = QHBoxLayout()
        audio_pref_row.setSpacing(10)
        self.prefer_english_button = QPushButton("English")
        self.prefer_english_button.setObjectName("pillButton")
        self.prefer_english_button.clicked.connect(
            lambda: self.set_audio_preferences("eng,en,english")
        )
        self.prefer_japanese_button = QPushButton("Japanese")
        self.prefer_japanese_button.setObjectName("pillButton")
        self.prefer_japanese_button.clicked.connect(
            lambda: self.set_audio_preferences("jp,jpn,japanese,jap")
        )
        audio_pref_row.addWidget(self.prefer_english_button)
        audio_pref_row.addWidget(self.prefer_japanese_button)
        audio_layout.addLayout(audio_pref_row)

        self.audio_track_combo = QComboBox()
        self.audio_track_combo.setMinimumWidth(280)
        self.audio_track_combo.setObjectName("elevatedCombo")
        audio_layout.addWidget(self.audio_track_combo)

        audio_action_row = QHBoxLayout()
        audio_action_row.setSpacing(10)
        self.refresh_audio_button = QPushButton("Refresh")
        self.refresh_audio_button.setObjectName("ghostButton")
        self.refresh_audio_button.clicked.connect(self.refresh_audio_tracks)
        self.use_audio_button = QPushButton("Use Selected")
        self.use_audio_button.clicked.connect(self.use_selected_audio_track)
        audio_action_row.addWidget(self.refresh_audio_button)
        audio_action_row.addWidget(self.use_audio_button)
        audio_layout.addLayout(audio_action_row)

        side_layout.addWidget(top_control_card)
        side_layout.addWidget(audio_card)
        side_layout.addStretch(1)

        side_scroll.setWidget(side_content)

        layout.addLayout(left_column, 1)
        layout.addWidget(side_scroll)
        return page

    def build_field_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("fieldLabel")
        return label

    def build_stat_card(
        self,
        eyebrow: str,
        value: str,
        caption: str,
        object_name: str = "statCard",
    ) -> QFrame:
        card = QFrame()
        card.setObjectName(object_name)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(6)

        eyebrow_label = QLabel(eyebrow)
        eyebrow_label.setObjectName("statEyebrow")
        value_label = QLabel(value)
        value_label.setObjectName("statValue")
        caption_label = QLabel(caption)
        caption_label.setObjectName("statCaption")
        caption_label.setWordWrap(True)

        layout.addWidget(eyebrow_label)
        layout.addWidget(value_label)
        layout.addWidget(caption_label)
        card.value_label = value_label  # type: ignore[attr-defined]
        card.caption_label = caption_label  # type: ignore[attr-defined]
        return card

    def apply_theme(self) -> None:
        self.setStyleSheet(
            """
            QWidget {
                background: #050714;
                color: #eef1ff;
                font-family: "Noto Sans", "Cantarell", sans-serif;
                font-size: 14px;
            }
            QMainWindow {
                background: #050714;
            }
            QStackedWidget {
                background: transparent;
            }
            QScrollArea, QScrollArea > QWidget > QWidget {
                background: transparent;
            }
            QFrame#heroCard {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #12172a, stop:1 #17122a);
                border: 1px solid #2c355a;
                border-radius: 26px;
            }
            QFrame#joinShell {
                background: #111728;
                border: 1px solid #2b3554;
                border-radius: 26px;
            }
            QFrame#surfaceCard {
                background: #12192c;
                border: 1px solid #2a3452;
                border-radius: 22px;
            }
            QFrame#stageCard {
                background: #0f1424;
                border: 1px solid #242f4f;
                border-radius: 26px;
            }
            QFrame#mediaSurface {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #0a0f1d, stop:1 #11162a);
                border: 1px solid #263151;
                border-radius: 24px;
            }
            QFrame#statCard, QFrame#accentStatCard {
                background: #141b2f;
                border: 1px solid #2d3758;
                border-radius: 18px;
            }
            QFrame#accentStatCard {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #171f3c, stop:1 #1d1b3d);
                border: 1px solid #5466c9;
            }
            QLabel#eyebrow {
                color: #8f98ff;
                font-size: 11px;
                font-weight: 700;
                letter-spacing: 0.16em;
            }
            QLabel#title {
                font-size: 30px;
                font-weight: 800;
                color: #eef3ff;
            }
            QLabel#roomTitle {
                font-size: 24px;
                font-weight: 800;
                color: #eef3ff;
            }
            QLabel#sectionTitle {
                font-size: 20px;
                font-weight: 800;
                color: #97a6ff;
            }
            QLabel#subtitle, QLabel#miniText {
                color: #b7c0e4;
            }
            QLabel#featureText {
                color: #d7dcf3;
                font-size: 14px;
            }
            QLabel#fieldLabel {
                color: #8e9bd1;
                font-size: 12px;
                font-weight: 700;
                letter-spacing: 0.05em;
                background: transparent;
            }
            QLabel#statEyebrow {
                color: #8f9bc4;
                font-size: 11px;
                font-weight: 700;
                letter-spacing: 0.12em;
            }
            QLabel#statValue {
                color: #eef3ff;
                font-size: 21px;
                font-weight: 800;
            }
            QLabel#statCaption {
                color: #97a3cc;
                font-size: 12px;
            }
            QLabel {
                background: transparent;
            }
            QLabel#offlineBadge, QLabel#onlineBadge, QLabel#softBadge, QLabel#windowBadge, QLabel#mutedBadge {
                border-radius: 14px;
                padding: 8px 14px;
                font-weight: 700;
            }
            QLabel#offlineBadge {
                background: #321727;
                border: 1px solid #723252;
                color: #ffc1d8;
            }
            QLabel#onlineBadge {
                background: #12261f;
                border: 1px solid #1d9e65;
                color: #b8ffd8;
            }
            QLabel#softBadge {
                background: #121a2e;
                border: 1px solid #323d61;
                color: #d8deff;
            }
            QLabel#windowBadge {
                background: #141b2f;
                border: 1px solid #4f63c9;
                color: #9aa7ff;
            }
            QLabel#mutedBadge {
                background: #0d1222;
                border: 1px solid #283250;
                color: #aeb8de;
            }
            QLabel#playerHint {
                color: #eef2ff;
                font-size: 18px;
                font-weight: 700;
            }
            QLabel#mediaLabel {
                color: #aeb8de;
                font-size: 13px;
                padding: 0;
            }
            QLabel#memberList {
                color: #9ea9d1;
                font-size: 13px;
                padding: 0;
            }
            QLineEdit, QPushButton, QComboBox {
                min-height: 46px;
                border-radius: 14px;
                border: 1px solid #33405f;
                background: #0d1222;
                padding: 0 14px;
                selection-background-color: #6a74ff;
            }
            QLineEdit:focus, QComboBox:focus {
                border: 1px solid #8493ff;
                background: #10172b;
            }
            QPushButton {
                background: #7582f3;
                color: #10142a;
                font-weight: 700;
                border: 0;
            }
            QPushButton:hover {
                background: #8892ff;
            }
            QPushButton:pressed {
                background: #626ed4;
            }
            QPushButton#primaryButton {
                min-height: 48px;
                padding: 0 20px;
            }
            QPushButton#ghostButton {
                background: #12192c;
                border: 1px solid #33405f;
                color: #e6ecff;
            }
            QPushButton#ghostButton:hover {
                background: #18213a;
            }
            QPushButton#pillButton {
                background: #1a2240;
                color: #cad2ff;
                border: 1px solid #33405f;
            }
            QPushButton#pillButton:hover {
                background: #202a50;
            }
            QTextEdit#helperPanel {
                background: #0d1222;
                border: 1px solid #24304f;
                border-radius: 16px;
                padding: 10px;
                color: #d8def8;
            }
            QSlider::groove:horizontal {
                height: 10px;
                border-radius: 5px;
                background: #141c31;
            }
            QSlider::sub-page:horizontal {
                background: #7b88ff;
                border-radius: 5px;
            }
            QSlider::handle:horizontal {
                width: 18px;
                margin: -6px 0;
                border-radius: 9px;
                background: #eef2ff;
                border: 2px solid #7a86ff;
            }
            QLabel#timeLabel {
                color: #cfd6f7;
                font-weight: 700;
                min-width: 44px;
            }
            QComboBox {
                padding-right: 30px;
            }
            QComboBox::drop-down {
                subcontrol-origin: padding;
                subcontrol-position: top right;
                width: 28px;
                border: 0;
                background: transparent;
            }
            QComboBox::down-arrow {
                width: 0px;
                height: 0px;
                border-left: 5px solid transparent;
                border-right: 5px solid transparent;
                border-top: 7px solid #9aa8ff;
                margin-right: 10px;
            }
            QComboBox QAbstractItemView {
                background: #10162a;
                border: 1px solid #354264;
                selection-background-color: #7682f5;
                selection-color: #111528;
                color: #ebefff;
                outline: 0;
                padding: 4px;
            }
            QStatusBar::item {
                border: 0;
            }
            QStatusBar {
                background: #070b16;
                color: #cfd7fb;
            }
            """
        )

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
        self.current_media_label.setText(media_url)
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

    def apply_pending_room_sync(self) -> None:
        if not self.pending_room_sync or not self.current_media_url:
            return

        try:
            self.apply_server_state(self.pending_room_sync, settle_mode=True)
            self.pending_room_sync = None
            self.pending_room_sync_attempts = 0
            append_runtime_log("apply_pending_room_sync settled successfully")
        except Exception as exc:
            append_runtime_log(f"apply_pending_room_sync retry due to: {exc}")
            if self.pending_room_sync_attempts > 0:
                self.pending_room_sync_attempts -= 1
                QTimer.singleShot(450, self.apply_pending_room_sync)
        finally:
            self.suppress_sync = False

    def toggle_playback(self) -> None:
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
            self.last_known_playing = playing
            self.remember_polled_state(position, playing, observed_at)
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
        self.member_label.setText(f"Members: {names}")
        self.current_member_count = len(members)
        self.members_badge.setText(
            f"{self.current_member_count} viewer{'s' if self.current_member_count != 1 else ''}"
        )
        self.viewer_stat.value_label.setText(str(self.current_member_count))  # type: ignore[attr-defined]
        self.viewer_stat.caption_label.setText(names)  # type: ignore[attr-defined]

        media_url = str(payload.get("media_url") or "")
        position_ms = int(payload.get("position_ms") or 0)
        updated_by = str(payload.get("updated_by") or "")

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
            self.pending_room_sync = payload
            self.pending_room_sync_attempts = 20
            QTimer.singleShot(350, self.apply_pending_room_sync)
            return

        if self.pending_room_sync is not None:
            self.pending_room_sync = payload
            return

        if (
            time.monotonic() < self.local_seek_override_until
            and abs(position_ms - self.local_seek_target_ms) > 350
        ):
            return

        try:
            self.apply_server_state(payload, settle_mode=False)
        except Exception as exc:
            append_runtime_log(f"on_room_state sync exception: {exc}")
            if "property unavailable" in str(exc).lower() or "mpv is not running" in str(exc).lower():
                self.show_status("Waiting for mpv to finish loading...")
                self.pending_room_sync = payload
                if self.pending_room_sync_attempts <= 0:
                    self.pending_room_sync_attempts = 12
                QTimer.singleShot(500, self.apply_pending_room_sync)
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
            self.page_stack.setCurrentIndex(1)
            self.show_status("Connected to server")
        else:
            self.current_member_count = 0
            self.player_seen_running_in_room = False
            self.members_badge.setText("0 viewers")
            self.member_label.setText("Members: none")
            self.viewer_stat.value_label.setText("0")  # type: ignore[attr-defined]
            self.viewer_stat.caption_label.setText("connected room members")  # type: ignore[attr-defined]
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
        worker = UpdateCheckWorker()
        thread = QThread(self)
        self._track_background_job(thread, worker)
        worker.moveToThread(thread)
        worker.finished.connect(self.on_update_check_complete)
        worker.finished.connect(thread.quit)
        worker.finished.connect(lambda *_: self._release_background_job(thread, worker))
        thread.started.connect(worker.run)
        thread.start()

    def on_update_check_complete(self, payload: object) -> None:
        self.update_check_started = False
        if not isinstance(payload, UpdateInfo):
            return
        self.pending_update_info = payload
        if not payload.available:
            return
        if payload.latest_version == self.update_prompted_version:
            return
        self.update_prompted_version = payload.latest_version
        self.prompt_for_update(payload)

    def prompt_for_update(self, info: UpdateInfo) -> None:
        if os.name == "nt" and info.asset_url:
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

        result = QMessageBox.information(
            self,
            "SyncRoom Update",
            (
                f"SyncRoom {info.latest_version} is available.\n\n"
                "Automatic in-app updating is currently only enabled for the Windows installer build. "
                "The release page will open now."
            ),
            QMessageBox.Ok | QMessageBox.Cancel,
            QMessageBox.Ok,
        )
        if result == QMessageBox.Ok:
            QDesktopServices.openUrl(QUrl(info.download_url))

    def download_and_install_update(self, info: UpdateInfo) -> None:
        dialog = UpdateProgressDialog()
        worker = UpdateDownloadWorker(info)
        thread = QThread(self)
        self._track_background_job(thread, worker)
        worker.moveToThread(thread)
        worker.progress.connect(dialog.set_progress)
        worker.finished.connect(dialog.accept)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.started.connect(worker.run)

        failure_message = {"text": ""}
        downloaded_path = {"path": ""}

        def remember_failure(message: str) -> None:
            failure_message["text"] = message
            dialog.set_progress(message, 0)
            dialog.reject()

        def remember_path(path: str) -> None:
            downloaded_path["path"] = path
            dialog.set_progress("Preparing installer...", 100)

        worker.failed.connect(remember_failure)
        worker.finished.connect(remember_path)
        thread.start()
        dialog.set_progress("Preparing update...", 0)
        result = dialog.exec()
        thread.wait()
        self._release_background_job(thread, worker)

        if result != QDialog.Accepted:
            if downloaded_path["path"]:
                cleanup_update_download(Path(downloaded_path["path"]))
            self.show_error(failure_message["text"] or "SyncRoom could not download the update.")
            return

        self.update_installer_path = Path(downloaded_path["path"])
        self.launch_update_installer_and_exit(self.update_installer_path)

    def launch_update_installer_and_exit(self, installer_path: Path) -> None:
        if os.name != "nt" or not getattr(sys, "frozen", False):
            self.show_error("Automatic in-app updating is only available in the packaged Windows build.")
            return

        app_path = Path(
            sys.executable if getattr(sys, "frozen", False) else Path(sys.argv[0]).resolve()
        )
        script_dir = Path(tempfile.mkdtemp(prefix="syncroom-update-script-"))
        script_path = script_dir / "apply-update.cmd"
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        script_path.write_text(
            "\n".join(
                [
                    "@echo off",
                    "setlocal enableextensions",
                    f'set "SYNCROOM_LOG={update_log_path()}"',
                    f'set "SYNCROOM_INSTALLER={installer_path}"',
                    f'set "SYNCROOM_APP={app_path}"',
                    f'set "SYNCROOM_PID={os.getpid()}"',
                    'echo [%date% %time%] Script updater started >> "%SYNCROOM_LOG%"',
                    ":wait_for_syncroom",
                    'tasklist /FI "PID eq %SYNCROOM_PID%" 2>NUL | find /I "%SYNCROOM_PID%" >NUL',
                    "if not errorlevel 1 (",
                    '  timeout /t 1 /nobreak >NUL',
                    "  goto wait_for_syncroom",
                    ")",
                    'echo [%date% %time%] Launching installer >> "%SYNCROOM_LOG%"',
                    'start "" /wait "%SYNCROOM_INSTALLER%" /SP- /VERYSILENT /SUPPRESSMSGBOXES /NOCANCEL /CLOSEAPPLICATIONS',
                    'set "SYNCROOM_EXIT=%ERRORLEVEL%"',
                    'echo [%date% %time%] Installer exit code %SYNCROOM_EXIT% >> "%SYNCROOM_LOG%"',
                    'if exist "%SYNCROOM_APP%" start "" "%SYNCROOM_APP%"',
                    'echo [%date% %time%] Relaunch requested >> "%SYNCROOM_LOG%"',
                ]
            ),
            encoding="utf-8",
        )
        append_update_log(
            f"Launching updater script script={script_path} installer={installer_path} app={app_path}"
        )
        process = subprocess.Popen(
            ["cmd", "/c", str(script_path)],
            creationflags=creationflags,
            close_fds=True,
            cwd=str(script_dir),
        )
        time.sleep(0.8)
        exit_code = process.poll()
        if exit_code is not None:
            append_update_log(f"Updater script process exited early with code {exit_code}")
            self.show_error(
                "SyncRoom could not start the updater. Install the new version manually this time."
            )
            return
        append_update_log("Updater script started successfully")
        self.show_status("Closing SyncRoom so the updater can install the new version...")
        QTimer.singleShot(150, QApplication.instance().quit)

    def _track_background_job(self, thread: QThread, worker: QObject) -> None:
        self._active_threads.append(thread)
        self._active_workers.append(worker)

    def _release_background_job(self, thread: QThread, worker: QObject) -> None:
        if worker in self._active_workers:
            self._active_workers.remove(worker)
            worker.deleteLater()
        if thread in self._active_threads:
            self._active_threads.remove(thread)
            thread.deleteLater()

    def shutdown_background_jobs(self) -> None:
        for thread in list(self._active_threads):
            thread.quit()
            thread.wait(2000)
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
        self.room_badge.setText(f"Room: {room_name if connected else 'not connected'}")
        self.room_status_card.value_label.setText("Live" if connected else "Offline")  # type: ignore[attr-defined]
        self.room_status_card.caption_label.setText(  # type: ignore[attr-defined]
            "room sync active" if connected else "room sync disconnected"
        )
        self.room_name_stat.value_label.setText(room_name if connected else "not connected")  # type: ignore[attr-defined]
        self.room_name_stat.caption_label.setText(  # type: ignore[attr-defined]
            "share this room name with everyone" if connected else "join a room to begin"
        )


def report_startup_crash(exc: BaseException) -> None:
    details = traceback.format_exc()
    path = logs_dir() / "startup-crash.log"
    try:
        path.write_text(details, encoding="utf-8")
    except Exception:
        pass
    append_runtime_log(f"Startup crash logged to {path}: {exc}")
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
        configure_crash_logging()
        append_runtime_log(f"SyncRoom starting config_dir={app_config_dir()}")
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
