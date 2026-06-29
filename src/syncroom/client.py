from __future__ import annotations

import os
import platform
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFontMetrics
from PySide6.QtNetwork import QAbstractSocket
from PySide6.QtWidgets import (
    QApplication,
    QBoxLayout,
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from syncroom import __version__
from syncroom.mpv_controller import MpvController
from syncroom.network.sync_client import SyncClient
from syncroom.settings import app_config_dir, default_display_name, load_settings, save_settings
from syncroom.ui.dialogs import StartupSetupDialog, UpdateAvailableDialog
from syncroom.ui.settings_panel import SettingsPanel
from syncroom.ui.widgets import NoWheelComboBox
from syncroom.updates import (
    UpdateInfo,
    check_for_updates,
)
from syncroom.utils.logging import (
    append_runtime_log,
    append_update_log,
    configure_crash_logging,
    flush_fault_log,
    runtime_log_path,
    safe_logs_dir,
    update_log_path,
)
from syncroom.windows_runtime import (
    ensure_windows_media_runtime,
    windows_mpv_available,
    windows_runtime_mpv_path,
    windows_runtime_yt_dlp_path,
    windows_yt_dlp_available,
)


THREAD_SHUTDOWN_TIMEOUT_MS = 5000
THREAD_FORCE_TERMINATE_TIMEOUT_MS = 2000


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


class WindowsRuntimeInstallerWorker(QObject):
    progress = Signal(str, int)
    finished = Signal(object)
    failed = Signal(str)

    def run(self) -> None:
        try:
            result = ensure_windows_media_runtime(self._report)
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.finished.emit(result)

    def _report(self, message: str, percent: int) -> None:
        self.progress.emit(message, percent)


class UpdateCheckWorker(QObject):
    finished = Signal(object)

    def run(self) -> None:
        self.finished.emit(check_for_updates())


def prepare_windows_runtime_if_needed() -> bool:
    if os.name != "nt":
        return True

    probe = MpvController()
    if not probe.needs_windows_media_runtime_install():
        return True

    dialog = StartupSetupDialog()
    worker = WindowsRuntimeInstallerWorker()
    thread = QThread()
    worker.moveToThread(thread)
    worker.progress.connect(dialog.set_progress)
    thread.started.connect(worker.run)

    state = {
        "failure_message": "",
        "yt_dlp_error": "",
        "mpv_succeeded": False,
        "timed_out": False,
    }

    def remember_success(result: object) -> None:
        state["mpv_succeeded"] = True
        state["yt_dlp_error"] = str(getattr(result, "yt_dlp_error", "") or "")
        dialog.set_progress("Media tools installed.", 100)
        append_runtime_log("Windows runtime installer worker finished successfully")
        thread.quit()

    def remember_failure(message: str) -> None:
        state["failure_message"] = message
        dialog.set_progress(message, 0)
        append_runtime_log(f"Windows runtime installer worker failed: {message}")
        thread.quit()

    def close_dialog_after_thread_finished() -> None:
        accepted = bool(state["mpv_succeeded"])
        QTimer.singleShot(0, dialog.accept if accepted else dialog.reject)

    def abort_setup_after_timeout() -> None:
        if not thread.isRunning():
            return
        state["timed_out"] = True
        state["failure_message"] = (
            "SyncRoom setup timed out while preparing media tools. "
            f"See {runtime_log_path()} for details."
        )
        append_runtime_log("Windows runtime installer timed out; terminating worker thread")
        thread.terminate()
        thread.wait(THREAD_FORCE_TERMINATE_TIMEOUT_MS)
        QTimer.singleShot(0, dialog.reject)

    worker.finished.connect(remember_success)
    worker.failed.connect(remember_failure)
    thread.finished.connect(close_dialog_after_thread_finished)
    thread.finished.connect(worker.deleteLater)
    thread.start()
    dialog.set_progress("Preparing setup...", 0)
    QTimer.singleShot(10 * 60 * 1000, abort_setup_after_timeout)
    result = dialog.exec()
    if thread.isRunning():
        stop_thread_with_timeout(thread, append_runtime_log, "Windows runtime installer")
    dispose_qobject_safely(thread)

    if result == QDialog.Accepted:
        if state["yt_dlp_error"]:
            QMessageBox.warning(
                None,
                "SyncRoom Setup",
                (
                    "SyncRoom installed mpv, but could not install yt-dlp. "
                    "Direct video links still work, but online media links may not.\n\n"
                    f"Details: {state['yt_dlp_error']}"
                ),
            )
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
        self.sync_client.error.connect(self.on_sync_client_error)
        self.sync_client.connected.connect(lambda: self.on_connection_change(True))
        self.sync_client.disconnected.connect(lambda: self.on_connection_change(False))
        self.sync_client.pong.connect(self.on_diagnostics_pong)

        self.player = MpvController()

        self.suppress_sync = False
        self.dragging_slider = False
        self.current_media_url = ""
        self.last_known_playing = False
        self.audio_tracks: list[dict] = []
        self.subtitle_tracks: list[dict] = []
        self.current_member_count = 0
        self.pending_audio_attempts = 0
        self.pending_subtitle_attempts = 0
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
        self.update_in_progress = False
        self.last_applied_seek_token = 0
        self.last_applied_event_id = 0
        self.last_osd_event_id = 0
        self.last_osd_signature = ""
        self.last_local_osd_signature = ""
        self.last_local_osd_at = 0.0
        self.show_playback_osd = self.settings_bool(self.settings.get("playback_osd", True))
        self.behind_sync_detected_at: float | None = None
        self.player_seen_running_in_room = False
        self.reconnect_enabled = False
        self.reconnect_profile: dict[str, object] | None = None
        self.reconnect_attempt = 0
        self.user_requested_disconnect = False
        self.last_connection_error = ""
        self.last_connected_at = 0.0
        self.reconnect_status = "idle"
        self.last_ping_ms: int | None = None
        self.pending_ping_sent_at: float | None = None
        self.custom_host_value = str(self.settings.get("host", "127.0.0.1") or "127.0.0.1")
        if self.custom_host_value == self.HOSTED_SERVER_URL:
            self.custom_host_value = "127.0.0.1"
        self.custom_port_value = str(self.settings.get("port", "24873") or "24873")
        self._active_threads: list[QThread] = []
        self._active_workers: list[QObject] = []

        self._last_ui_scale = -1.0
        self._elide_refresh_pending = False
        self.build_ui()
        self.refresh_scaled_ui(force=True)

        self.reconnect_timer = QTimer(self)
        self.reconnect_timer.setSingleShot(True)
        self.reconnect_timer.timeout.connect(self.perform_reconnect)
        self.diagnostics_timer = QTimer(self)
        self.diagnostics_timer.setInterval(5000)
        self.diagnostics_timer.timeout.connect(self.send_diagnostics_ping)

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
            glow_color="#050505",
            glow_alpha=150,
            blur=52,
        )
        dashboard_layout.addWidget(self.build_top_bar())

        content_shell, content_layout = self.build_panel(
            "contentShell",
            margins=(12, 12, 12, 12),
            spacing=0,
            glow_color="#050505",
            glow_alpha=120,
            blur=42,
        )
        self.page_stack = QStackedWidget()
        self.page_stack.setObjectName("pageStack")
        self.page_stack.addWidget(self.make_scroll_page(self.build_join_page()))
        self.page_stack.addWidget(self.make_scroll_page(self.build_room_page()))
        self.settings_panel = SettingsPanel(self.settings)
        self.settings_panel.audioPreferenceChanged.connect(self.on_audio_preference_changed)
        self.settings_panel.subtitlePreferenceChanged.connect(self.on_subtitle_preference_changed)
        self.settings_panel.streamingQualityChanged.connect(self.on_streaming_quality_changed)
        self.settings_panel.playbackNotificationsChanged.connect(self.on_playback_notifications_changed)
        self.page_stack.addWidget(self.make_scroll_page(self.settings_panel))
        self.page_stack.currentChanged.connect(self.update_page_chrome)

        content_layout.addWidget(self.page_stack)
        dashboard_layout.addWidget(content_shell, 1)
        root.addWidget(dashboard_shell)
        self.setCentralWidget(central)
        self.statusBar().showMessage("Ready")
        self.player.set_ytdl_format(self.settings_panel.ytdl_format())
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
        glow_color: str = "#050505",
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

    def build_nav_chip(self, text: str) -> QPushButton:
        chip = QPushButton(text)
        chip.setObjectName("navChip")
        chip.setProperty("active", False)
        chip.setCursor(Qt.PointingHandCursor)
        return chip

    def set_nav_chip_active(self, chip: QPushButton, active: bool) -> None:
        chip.setProperty("active", active)
        chip.style().unpolish(chip)
        chip.style().polish(chip)
        chip.update()

    def build_top_bar(self) -> QFrame:
        top_bar, top_layout = self.build_panel(
            "topBar",
            margins=(10, 10, 10, 10),
            spacing=0,
            glow_color="#050505",
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
        self.settings_nav_chip = self.build_nav_chip("Settings")
        self.lobby_nav_chip.clicked.connect(lambda: self.page_stack.setCurrentIndex(0))
        self.room_nav_chip.clicked.connect(lambda: self.page_stack.setCurrentIndex(1))
        self.settings_nav_chip.clicked.connect(lambda: self.page_stack.setCurrentIndex(2))
        for chip in (self.lobby_nav_chip, self.room_nav_chip, self.settings_nav_chip):
            nav_layout.addWidget(chip)

        self.top_bar_row = QHBoxLayout()
        self.top_bar_row.setContentsMargins(0, 0, 0, 0)
        self.top_bar_row.addWidget(nav_capsule)
        self.top_bar_row.addStretch(1)
        top_layout.addLayout(self.top_bar_row)
        return top_bar

    def make_scroll_page(self, widget: QWidget) -> QScrollArea:
        widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
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
        if not self.update_single_elided_label(label):
            self.schedule_elide_refresh()

    def update_single_elided_label(self, label: QLabel) -> bool:
        full_text = str(label.property("full_text") or "")
        elide_value = label.property("elide_mode")
        if elide_value is None:
            if label.text() != full_text:
                label.setText(full_text)
            return True
        if not label.isVisibleTo(self):
            if label.text() != full_text:
                label.setText(full_text)
            label.setToolTip("")
            return True
        available_width = label.contentsRect().width()
        if available_width <= 1:
            if label.text() != full_text:
                label.setText(full_text)
            label.setToolTip("")
            return False
        metrics = QFontMetrics(label.font())
        try:
            mode = Qt.TextElideMode(int(elide_value))
        except Exception:
            mode = Qt.ElideRight
        label.setText(metrics.elidedText(full_text, mode, available_width))
        label.setToolTip(full_text if metrics.horizontalAdvance(full_text) > available_width else "")
        return True

    def update_elided_labels(self) -> None:
        needs_retry = False
        for label in self.findChildren(QLabel):
            if label.property("elide_mode") is not None:
                if not self.update_single_elided_label(label):
                    needs_retry = True
        if needs_retry:
            self.schedule_elide_refresh()

    def schedule_elide_refresh(self) -> None:
        if self._elide_refresh_pending:
            return
        self._elide_refresh_pending = True

        def refresh() -> None:
            self._elide_refresh_pending = False
            self.update_elided_labels()

        QTimer.singleShot(0, refresh)
        QTimer.singleShot(50, self.update_elided_labels)

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
        self.join_shell_layout = QBoxLayout(QBoxLayout.LeftToRight)
        self.join_shell_layout.setContentsMargins(0, 0, 0, 0)
        self.join_shell_layout.setSpacing(18)

        card, card_layout = self.build_panel(
            "controlPanel",
            margins=(28, 28, 28, 28),
            spacing=16,
            glow_color="#040404",
            glow_alpha=90,
            blur=24,
            offset_y=10,
        )
        card.setMinimumWidth(0)
        card.setMaximumWidth(16777215)
        card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
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
        if str(saved_host or "").strip() and saved_host != self.HOSTED_SERVER_URL:
            self.custom_host_value = str(saved_host)

        self.host_select = NoWheelComboBox()
        self.host_select.addItem(f"{self.HOSTED_SERVER_LABEL} ({self.HOSTED_SERVER_URL})")
        self.host_select.addItem(self.CUSTOM_SERVER_LABEL)
        self.host_input = QLineEdit(self.custom_host_value if saved_host_mode == self.CUSTOM_SERVER_LABEL else self.HOSTED_SERVER_URL)
        self.port_input = QLineEdit(self.custom_port_value)
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
        self.host_input.textChanged.connect(self.on_host_input_changed)
        self.port_input.textChanged.connect(self.on_port_input_changed)
        self.room_input.textChanged.connect(self.update_lobby_summary)
        self.name_input.textChanged.connect(self.update_lobby_summary)

        self.join_host_profile_label = self.build_field_label("Server profile")
        self.join_host_label = self.build_field_label("Host or domain")
        self.join_port_label = self.build_field_label("Port")
        self.join_room_label = self.build_field_label("Room name")
        self.join_password_label = self.build_field_label("Room password")
        self.join_name_label = self.build_field_label("Display name")
        self.rebuild_join_grid(compact=False)

        self.join_button_row = QBoxLayout(QBoxLayout.LeftToRight)
        self.join_button_row.setContentsMargins(0, 2, 0, 0)
        self.join_button_row.setSpacing(10)
        self.connect_button = QPushButton("Join Room")
        self.connect_button.setObjectName("primaryButton")
        self.connect_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.connect_button.clicked.connect(self.connect_to_room)
        self.join_button_row.addWidget(self.connect_button)

        card_layout.addWidget(eyebrow)
        card_layout.addWidget(title)
        card_layout.addLayout(grid)
        card_layout.addLayout(self.join_button_row)
        card_layout.addStretch(1)

        self.join_shell_layout.addWidget(card, 1)
        layout.addLayout(self.join_shell_layout, 1)
        self.on_host_mode_changed()
        self.update_lobby_summary()
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
            glow_color="#080808",
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

        self.resync_button = QPushButton("Resync")
        self.resync_button.setObjectName("ghostButton")
        self.resync_button.setToolTip("Follow the current room playback state.")
        self.resync_button.clicked.connect(self.manual_resync_to_room)
        self.resync_button.setEnabled(False)
        self.resync_button.setMinimumHeight(42)
        self.resync_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self.room_header_row.addLayout(title_box, 1)
        self.room_header_row.addWidget(self.resync_button)
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

        self.member_label = QLabel("Members: none")
        self.member_label.setObjectName("memberList")
        self.configure_resizable_label(self.member_label, wrap=False)
        self.member_label.setMinimumHeight(18)
        self.member_label.hide()
        self.set_label_text_safe(self.member_label, "Members: none", elide_mode=Qt.ElideRight)

        self.play_button = QPushButton("Play")
        self.play_button.setObjectName("primaryButton")
        self.play_button.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self.play_button.clicked.connect(self.toggle_playback)
        self.play_button.hide()
        self.position_label = QLabel("00:00")
        self.position_label.setObjectName("timeLabel")
        self.position_label.hide()
        self.duration_label = QLabel("00:00")
        self.duration_label.setObjectName("timeLabel")
        self.duration_label.hide()

        self.position_slider = QSlider(Qt.Horizontal)
        self.position_slider.sliderPressed.connect(self.on_slider_pressed)
        self.position_slider.sliderReleased.connect(self.on_slider_released)
        self.position_slider.hide()

        left_column.addWidget(hero_card)

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
            glow_color="#070707",
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

        self.current_media_label = QLabel("No media loaded yet")
        self.current_media_label.setObjectName("mediaLabel")
        self.configure_resizable_label(self.current_media_label, wrap=False)
        self.current_media_label.setMinimumHeight(18)
        self.set_label_text_safe(
            self.current_media_label,
            "No media loaded yet",
            elide_mode=Qt.ElideMiddle,
        )
        top_control_layout.addWidget(self.current_media_label)

        ytdlp_note = "yt-dlp available" if self.player.yt_dlp_available() else "yt-dlp not found"
        stream_note = QLabel(f"Direct links work. Web links use yt-dlp when available ({ytdlp_note}).")
        stream_note.setObjectName("inlineNote")
        stream_note.setWordWrap(True)
        top_control_layout.addWidget(stream_note)

        audio_card, audio_layout = self.build_panel(
            "sidebarPanel",
            margins=(20, 20, 20, 20),
            spacing=12,
            glow_color="#070707",
            glow_alpha=110,
            blur=28,
            offset_y=10,
        )

        audio_title = QLabel("Audio track")
        audio_title.setObjectName("sectionTitle")
        audio_overline = QLabel("TRACKS")
        audio_overline.setObjectName("sectionOverline")

        audio_layout.addWidget(audio_overline)
        audio_layout.addWidget(audio_title)

        self.audio_track_combo = NoWheelComboBox()
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

        subtitle_card, subtitle_layout = self.build_panel(
            "sidebarPanel",
            margins=(20, 20, 20, 20),
            spacing=12,
            glow_color="#070707",
            glow_alpha=105,
            blur=26,
            offset_y=9,
        )

        subtitle_title = QLabel("Subtitle track")
        subtitle_title.setObjectName("sectionTitle")
        subtitle_overline = QLabel("CAPTIONS")
        subtitle_overline.setObjectName("sectionOverline")
        subtitle_layout.addWidget(subtitle_overline)
        subtitle_layout.addWidget(subtitle_title)

        self.subtitle_track_combo = NoWheelComboBox()
        self.subtitle_track_combo.setMinimumWidth(0)
        self.subtitle_track_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.subtitle_track_combo.setObjectName("elevatedCombo")
        subtitle_layout.addWidget(self.subtitle_track_combo)

        self.subtitle_action_row = QBoxLayout(QBoxLayout.LeftToRight)
        self.subtitle_action_row.setContentsMargins(0, 0, 0, 0)
        self.subtitle_action_row.setSpacing(10)
        self.refresh_subtitle_button = QPushButton("Refresh")
        self.refresh_subtitle_button.setObjectName("ghostButton")
        self.refresh_subtitle_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.refresh_subtitle_button.clicked.connect(self.refresh_subtitle_tracks)
        self.use_subtitle_button = QPushButton("Use Selected")
        self.use_subtitle_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.use_subtitle_button.clicked.connect(self.use_selected_subtitle_track)
        self.subtitle_action_row.addWidget(self.refresh_subtitle_button)
        self.subtitle_action_row.addWidget(self.use_subtitle_button)
        subtitle_layout.addLayout(self.subtitle_action_row)

        diagnostics_card, diagnostics_layout = self.build_panel(
            "sidebarPanel",
            margins=(18, 18, 18, 18),
            spacing=10,
            glow_color="#070707",
            glow_alpha=95,
            blur=24,
            offset_y=8,
        )

        diagnostics_overline = QLabel("HEALTH")
        diagnostics_overline.setObjectName("sectionOverline")
        diagnostics_title = QLabel("Diagnostics")
        diagnostics_title.setObjectName("sectionTitle")
        diagnostics_layout.addWidget(diagnostics_overline)
        diagnostics_layout.addWidget(diagnostics_title)

        self.diagnostics_connection_value = QLabel("Offline")
        self.diagnostics_ping_value = QLabel("Unknown")
        self.diagnostics_sync_value = QLabel("Unknown")
        self.diagnostics_reconnect_value = QLabel("idle")
        for label in (
            self.diagnostics_connection_value,
            self.diagnostics_ping_value,
            self.diagnostics_sync_value,
            self.diagnostics_reconnect_value,
        ):
            label.setObjectName("diagnosticValue")
            self.configure_resizable_label(label, wrap=False)
        diagnostics_layout.addLayout(
            self.build_diagnostics_row("Connection", self.diagnostics_connection_value)
        )
        diagnostics_layout.addLayout(self.build_diagnostics_row("Ping", self.diagnostics_ping_value))
        diagnostics_layout.addLayout(self.build_diagnostics_row("Sync", self.diagnostics_sync_value))
        diagnostics_layout.addLayout(
            self.build_diagnostics_row("Reconnect", self.diagnostics_reconnect_value)
        )

        diagnostics_actions = QBoxLayout(QBoxLayout.LeftToRight)
        diagnostics_actions.setContentsMargins(0, 4, 0, 0)
        diagnostics_actions.setSpacing(10)
        self.copy_report_button = QPushButton("Copy Report")
        self.copy_report_button.setObjectName("ghostButton")
        self.copy_report_button.setToolTip("Copy connection and player diagnostics to clipboard.")
        self.copy_report_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.copy_report_button.clicked.connect(self.copy_debug_report)
        self.open_logs_button = QPushButton("Logs")
        self.open_logs_button.setObjectName("pillButton")
        self.open_logs_button.setToolTip("Open the SyncRoom logs folder.")
        self.open_logs_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.open_logs_button.clicked.connect(self.open_logs_folder)
        diagnostics_actions.addWidget(self.copy_report_button)
        diagnostics_actions.addWidget(self.open_logs_button)
        diagnostics_layout.addLayout(diagnostics_actions)

        left_column.addWidget(diagnostics_card)
        left_column.addStretch(1)
        side_layout.addWidget(top_control_card)
        side_layout.addWidget(audio_card)
        side_layout.addWidget(subtitle_card)
        side_layout.addStretch(1)

        self.room_shell_layout.addLayout(left_column, 5)
        self.room_shell_layout.addWidget(side_content, 5)
        layout.addLayout(self.room_shell_layout)
        return page

    def build_diagnostics_row(self, title: str, value_label: QLabel) -> QBoxLayout:
        row = QBoxLayout(QBoxLayout.LeftToRight)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        title_label = QLabel(title)
        title_label.setObjectName("diagnosticLabel")
        title_label.setMinimumWidth(78)
        title_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        row.addWidget(title_label)
        row.addWidget(value_label, 1)
        return row

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

        hosted = hasattr(self, "host_select") and self.host_select.currentIndex() == 0
        if compact:
            row = 0
            self.join_grid.addWidget(self.join_host_profile_label, row, 0)
            row += 1
            self.join_grid.addWidget(self.host_select, row, 0)
            row += 1
            self.join_grid.addWidget(self.join_host_label, row, 0)
            row += 1
            self.join_grid.addWidget(self.host_input, row, 0)
            row += 1
            if not hosted:
                self.join_grid.addWidget(self.join_port_label, row, 0)
                row += 1
                self.join_grid.addWidget(self.port_input, row, 0)
                row += 1
            self.join_grid.addWidget(self.join_room_label, row, 0)
            row += 1
            self.join_grid.addWidget(self.room_input, row, 0)
            row += 1
            self.join_grid.addWidget(self.join_password_label, row, 0)
            row += 1
            self.join_grid.addWidget(self.password_input, row, 0)
            row += 1
            self.join_grid.addWidget(self.join_name_label, row, 0)
            row += 1
            self.join_grid.addWidget(self.name_input, row, 0)
            return

        self.join_grid.addWidget(self.join_host_profile_label, 0, 0, 1, 2)
        self.join_grid.addWidget(self.host_select, 1, 0, 1, 2)
        self.join_grid.addWidget(self.join_host_label, 2, 0, 1, 2)
        self.join_grid.addWidget(self.host_input, 3, 0, 1, 2)
        if hosted:
            self.join_grid.addWidget(self.join_room_label, 4, 0, 1, 2)
            self.join_grid.addWidget(self.room_input, 5, 0, 1, 2)
        else:
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
            self.apply_depth_effect(card, "#070707", 70, 18, 6)
        else:
            self.apply_depth_effect(card, "#070707", 82, 22, 7)
        return card

    def update_page_chrome(self, index: int) -> None:
        lobby_active = index == 0
        room_active = index == 1
        settings_active = index == 2
        in_room_context = bool(self.sync_client.client_id) or (
            self.reconnect_enabled and not self.user_requested_disconnect and index in {1, 2}
        )
        self.lobby_nav_chip.setVisible(not in_room_context)
        self.room_nav_chip.setVisible(in_room_context)
        self.settings_nav_chip.setVisible(in_room_context)
        self.set_nav_chip_active(self.lobby_nav_chip, lobby_active)
        self.set_nav_chip_active(self.room_nav_chip, room_active)
        self.set_nav_chip_active(self.settings_nav_chip, settings_active)
        self.schedule_elide_refresh()

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
        compact_join = width < 860
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
            self.join_card.setMaximumWidth(16777215)
        if hasattr(self, "join_grid"):
            last_state = self.join_grid.property("compact")
            if last_state is None or bool(last_state) != compact_join:
                self.join_grid.setProperty("compact", compact_join)
                self.rebuild_join_grid(compact_join)

        if hasattr(self, "top_bar_row"):
            self.top_bar_row.setSpacing(10 if room_compact else 12)
        if hasattr(self, "join_shell_layout"):
            self.join_shell_layout.setDirection(QBoxLayout.TopToBottom)
            self.join_shell_layout.setSpacing(12 if room_compact else 14)
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
        if hasattr(self, "load_row"):
            self.load_row.setDirection(QBoxLayout.TopToBottom if width < 960 else QBoxLayout.LeftToRight)
            self.load_row.setSpacing(10)
        if hasattr(self, "audio_action_row"):
            self.audio_action_row.setDirection(QBoxLayout.TopToBottom if width < 960 else QBoxLayout.LeftToRight)
            self.audio_action_row.setSpacing(10)
        if hasattr(self, "subtitle_action_row"):
            self.subtitle_action_row.setDirection(QBoxLayout.TopToBottom if width < 960 else QBoxLayout.LeftToRight)
            self.subtitle_action_row.setSpacing(10)
        if hasattr(self, "join_button_row"):
            self.join_button_row.setDirection(QBoxLayout.TopToBottom if width < 760 else QBoxLayout.LeftToRight)
            self.join_button_row.setSpacing(10)
        if hasattr(self, "room_side_content"):
            self.room_side_content.setMinimumWidth(0)
            self.room_side_content.setMaximumWidth(16777215 if room_stacked else 420)
        self.update_elided_labels()
        self.schedule_elide_refresh()

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
            QFrame#settingsCard {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 rgba(19, 19, 20, 0.96),
                    stop:1 rgba(10, 10, 11, 0.90));
                border: 1px solid rgba(78, 78, 82, 0.30);
                border-radius: __CARD_RADIUS__px;
            }
            QPushButton#navChip {
                min-height: __NAV_H__px;
                padding: 0 __NAV_PAD__px;
                border-radius: __NAV_CHIP_RADIUS__px;
                color: rgba(214, 214, 216, 0.76);
                background: transparent;
                border: 1px solid transparent;
                font-weight: 700;
            }
            QPushButton#navChip:hover {
                color: #ffffff;
                background: rgba(28, 28, 30, 0.92);
                border: 1px solid rgba(255, 255, 255, 0.48);
            }
            QPushButton#navChip[active="true"] {
                color: #ffffff;
                background: rgba(36, 36, 38, 0.96);
                border: 1px solid rgba(255, 255, 255, 0.55);
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
            QLabel#reconnectingBadge {
                background: rgba(34, 31, 24, 0.94);
                border: 1px solid rgba(146, 132, 96, 0.32);
                color: #f4eee0;
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
                color: #fbfbfb;
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
            QLabel#diagnosticLabel {
                color: rgba(168, 168, 174, 0.82);
                font-size: __NOTE_PX__px;
                font-weight: 800;
            }
            QLabel#diagnosticValue {
                color: #f7f7f8;
                font-size: __NOTE_PX__px;
                font-weight: 800;
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
                color: #fbfbfb;
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 rgba(17, 17, 18, 0.98),
                    stop:1 rgba(10, 10, 10, 0.92));
            }
            QPushButton:hover {
                border: 1px solid rgba(255, 255, 255, 0.55);
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 rgba(26, 26, 27, 0.99),
                    stop:1 rgba(16, 16, 17, 0.96));
            }
            QPushButton:pressed {
                padding-top: 1px;
                background: rgba(9, 9, 10, 0.98);
            }
            QPushButton:focus {
                border: 1px solid rgba(184, 184, 190, 0.62);
            }
            QPushButton:disabled {
                color: rgba(148, 148, 152, 0.56);
                border: 1px solid rgba(64, 64, 68, 0.20);
                background: rgba(10, 10, 11, 0.58);
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
                color: #f1f1f3;
            }
            QPushButton#ghostButton:hover, QPushButton#pillButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 rgba(20, 20, 21, 0.98),
                    stop:1 rgba(14, 14, 14, 0.92));
                border: 1px solid rgba(255, 255, 255, 0.52);
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
                color: #f5f5f7;
                outline: 0;
                padding: 6px;
            }
            QCheckBox#settingsCheck {
                min-height: __CONTROL_H__px;
                color: #f2f2f4;
                font-weight: 800;
                spacing: 10px;
            }
            QCheckBox#settingsCheck::indicator {
                width: 18px;
                height: 18px;
                border-radius: 5px;
                border: 1px solid rgba(118, 118, 124, 0.44);
                background: rgba(8, 8, 9, 0.98);
            }
            QCheckBox#settingsCheck::indicator:hover {
                border: 1px solid rgba(255, 255, 255, 0.55);
                background: rgba(22, 22, 23, 0.98);
            }
            QCheckBox#settingsCheck::indicator:checked {
                border: 1px solid rgba(255, 255, 255, 0.64);
                background: rgba(236, 236, 238, 0.94);
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
            port = self.selected_port()
        except ValueError:
            self.show_error("Port must be a number.")
            return
        self.persist_settings()
        self.player_seen_running_in_room = False
        self.reconnect_profile = {
            "host": host,
            "port": port,
            "room": room,
            "name": name,
            "password": password,
        }
        self.reconnect_enabled = True
        self.user_requested_disconnect = False
        self.reconnect_attempt = 0
        self.reconnect_status = "connecting"
        self.last_connection_error = ""
        self.last_ping_ms = None
        self.pending_ping_sent_at = None
        self.reconnect_timer.stop()
        self.sync_client.connect_to_server(host, port, room, name, password)
        self.update_diagnostics()
        self.show_status(f"Connecting to {host}:{port}...")

    def selected_host(self) -> str:
        if self.host_select.currentIndex() == 0:
            return self.HOSTED_SERVER_URL
        return self.host_input.text().strip() or "127.0.0.1"

    def selected_port(self) -> int:
        if self.host_select.currentIndex() == 0:
            return 24873
        return int(self.port_input.text().strip() or "24873")

    def update_lobby_summary(self) -> None:
        if not hasattr(self, "lobby_server_stat"):
            return
        profile = "Hosted" if self.host_select.currentIndex() == 0 else "Custom"
        host = self.selected_host()
        port = "24873" if self.host_select.currentIndex() == 0 else self.port_input.text().strip() or "24873"
        room = self.room_input.text().strip() or "movie-night"
        name = self.name_input.text().strip() or default_display_name()
        self.set_stat_card_text(
            self.lobby_server_stat,
            value=profile,
            caption=host if self.host_select.currentIndex() == 0 else f"{host}:{port}",
            caption_elide_mode=Qt.ElideMiddle,
        )
        self.set_stat_card_text(
            self.lobby_room_stat,
            value=room,
            caption="ready to join",
            value_elide_mode=Qt.ElideMiddle,
        )
        self.set_stat_card_text(
            self.lobby_name_stat,
            value=name,
            caption="display name",
            value_elide_mode=Qt.ElideMiddle,
        )

    def on_host_mode_changed(self) -> None:
        hosted = self.host_select.currentIndex() == 0
        if not hosted:
            self.host_input.setText(self.custom_host_value)
            self.port_input.setText(self.custom_port_value)
        else:
            current_host = self.host_input.text().strip()
            if current_host and current_host != self.HOSTED_SERVER_URL:
                self.custom_host_value = current_host
            self.custom_port_value = self.port_input.text().strip() or self.custom_port_value
            self.host_input.setText(self.HOSTED_SERVER_URL)
        self.host_input.setEnabled(not hosted)
        self.join_port_label.setVisible(not hosted)
        self.port_input.setVisible(not hosted)
        if hasattr(self, "join_grid"):
            compact = bool(self.join_grid.property("compact"))
            self.rebuild_join_grid(compact)
        self.update_lobby_summary()

    def on_host_input_changed(self) -> None:
        if self.host_select.currentIndex() == 1:
            self.custom_host_value = self.host_input.text().strip()
        self.update_lobby_summary()

    def on_port_input_changed(self) -> None:
        if self.host_select.currentIndex() == 1:
            self.custom_port_value = self.port_input.text().strip()
        self.update_lobby_summary()

    def leave_room(self) -> None:
        self.user_requested_disconnect = True
        self.reconnect_enabled = False
        self.reconnect_status = "idle"
        self.reconnect_timer.stop()
        self.diagnostics_timer.stop()
        self.pending_ping_sent_at = None
        self.sync_client.disconnect_from_server()
        self.player_seen_running_in_room = False
        self.clear_pending_room_sync()
        self.page_stack.setCurrentIndex(0)
        self.update_diagnostics()
        self.show_status("Left room")

    def manual_resync_to_room(self) -> None:
        if not self.is_room_connected():
            self.show_status("Not connected to a room.")
            return
        if not self.last_room_payload:
            self.show_status("No room state received yet.")
            return

        payload = dict(self.last_room_payload)
        media_url = str(payload.get("media_url") or "")
        if media_url and media_url != self.current_media_url:
            self.url_input.setText(media_url)
            self.set_media_url(media_url, broadcast=False)
            self.start_pending_room_sync(payload, "recovering", "Manual resync...")
            self.show_status("Manual resync started.")
            return

        if not self.current_media_url:
            self.show_status("No room media to resync yet.")
            return

        try:
            self.apply_server_state(payload, settle_mode=True)
            self.clear_pending_room_sync()
            self.update_room_sync_state("live", "Manual resync complete")
            self.show_status("Resynced to room.")
        except Exception as exc:
            append_runtime_log(f"manual_resync_to_room sync exception: {exc}")
            if self.is_recoverable_sync_error(exc):
                self.start_pending_room_sync(payload, "recovering", "Manual resync...")
                self.show_status("Manual resync started.")
            else:
                self.show_transient_error(f"Could not resync mpv state: {exc}")
        finally:
            self.suppress_sync = False

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
            if os.name == "nt" and self.player.needs_windows_mpv_runtime_install():
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
            self.pending_subtitle_attempts = 10
            self.show_status("Loaded video link in mpv")
            if broadcast:
                self.show_local_playback_osd("load", 0)
            QTimer.singleShot(250, self.refresh_audio_tracks)
            QTimer.singleShot(450, self.apply_audio_preferences_with_retry)
            QTimer.singleShot(300, self.refresh_subtitle_tracks)
            QTimer.singleShot(500, self.apply_subtitle_preferences_with_retry)
            self.persist_settings()
        except Exception as exc:
            append_runtime_log(f"set_media_url failed: {exc}")
            hint = ""
            if not self.player.yt_dlp_available():
                hint = " This link may require yt-dlp. Direct video links still work."
            self.show_error(
                f"Could not start mpv. SyncRoom could not launch or install the player runtime. Details: {exc}{hint}"
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

    @staticmethod
    def settings_bool(value: object) -> bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() not in {"0", "false", "no", "off", ""}

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
        elif state == "reconnecting":
            value = "Reconnecting"
            caption = note or "waiting for server"
        elif state == "connected":
            value = "Connected"
            caption = note or "connected to the room"

        self.set_stat_card_text(self.room_status_card, value=value, caption=caption)
        self.schedule_elide_refresh()
        self.update_diagnostics()

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
                self.show_local_playback_osd(
                    "play" if target_playing else "pause",
                    int(status["position_ms"]),
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
                self.show_local_playback_osd("seek", target_position)
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
                self.page_stack.currentIndex() in {1, 2}
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
            self.show_local_playback_osd("seek", position)
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
            self.show_local_playback_osd("play" if playing else "pause", position)
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
        event_id = int(payload.get("event_id") or 0)
        first_room_payload = self.last_room_payload is None
        self.last_room_payload = dict(payload)
        self.update_diagnostics()

        if first_room_payload and event_id > 0:
            self.last_osd_event_id = max(self.last_osd_event_id, event_id)

        if not media_url:
            return

        if updated_by == self.sync_client.client_id:
            self.last_osd_event_id = max(self.last_osd_event_id, event_id)
            return

        if media_url != self.current_media_url:
            append_runtime_log(
                f"Late join or media switch detected current={self.current_media_url or '<none>'}"
            )
            self.maybe_show_remote_playback_osd(payload, media_switch=True)
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
            self.maybe_show_remote_playback_osd(payload)
        except Exception as exc:
            append_runtime_log(f"on_room_state sync exception: {exc}")
            if self.is_recoverable_sync_error(exc):
                self.show_status("Waiting for playback to finish loading...")
                self.start_pending_room_sync(payload, "recovering", "Catching up to the room...")
                self.maybe_show_remote_playback_osd(payload)
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

    def is_room_connected(self) -> bool:
        return (
            bool(self.sync_client.client_id)
            and self.sync_client.socket.state() == QAbstractSocket.ConnectedState
        )

    def on_sync_client_error(self, message: str) -> None:
        message = message.strip() or "Connection error"
        lowered = message.lower()
        if self.user_requested_disconnect and any(
            fragment in lowered for fragment in ("operation canceled", "operation cancelled", "socket operation")
        ):
            return
        self.last_connection_error = message
        append_runtime_log(f"Sync client error handled: {message}")
        if SyncClient.is_join_rejection(message):
            self.reconnect_enabled = False
            self.user_requested_disconnect = True
            self.reconnect_status = "stopped"
            self.reconnect_timer.stop()
            self.diagnostics_timer.stop()
            self.update_connection_state(False)
            self.update_diagnostics()
            self.show_error(message)
            return

        if self.reconnect_enabled and not self.user_requested_disconnect and not self.update_in_progress:
            self.show_transient_error(f"Connection issue: {message}")
            self.schedule_reconnect()
            return

        self.show_error(message)

    def schedule_reconnect(self) -> None:
        if (
            not self.reconnect_enabled
            or self.user_requested_disconnect
            or self.update_in_progress
            or not self.reconnect_profile
        ):
            return
        if self.reconnect_timer.isActive():
            return
        delays_ms = (1000, 2000, 5000, 10000)
        self.reconnect_attempt += 1
        delay_ms = delays_ms[self.reconnect_attempt - 1] if self.reconnect_attempt <= len(delays_ms) else 20000
        delay_seconds = max(1, delay_ms // 1000)
        self.reconnect_status = f"attempt {self.reconnect_attempt} - next try in {delay_seconds}s"
        self.update_connection_state(False, reconnecting=True)
        self.update_room_sync_state("reconnecting", "Connection lost - reconnecting...")
        self.update_diagnostics()
        self.show_status("Connection lost - reconnecting...")
        self.reconnect_timer.start(delay_ms)

    def perform_reconnect(self) -> None:
        if (
            not self.reconnect_enabled
            or self.user_requested_disconnect
            or self.update_in_progress
            or not self.reconnect_profile
        ):
            return
        profile = self.reconnect_profile
        self.reconnect_status = f"attempt {self.reconnect_attempt} - connecting"
        self.update_connection_state(False, reconnecting=True)
        self.update_diagnostics()
        self.show_status(f"Reconnecting... attempt {self.reconnect_attempt}")
        self.sync_client.connect_to_server(
            str(profile["host"]),
            int(profile["port"]),
            str(profile["room"]),
            str(profile["name"]),
            str(profile.get("password") or ""),
        )

    def send_diagnostics_ping(self) -> None:
        if not self.is_room_connected():
            self.pending_ping_sent_at = None
            self.update_diagnostics()
            return
        if self.pending_ping_sent_at is None:
            self.pending_ping_sent_at = time.monotonic()
            self.sync_client.send({"type": "ping"})
        self.update_diagnostics()

    def on_diagnostics_pong(self) -> None:
        if self.pending_ping_sent_at is None:
            return
        self.last_ping_ms = max(0, int((time.monotonic() - self.pending_ping_sent_at) * 1000))
        self.pending_ping_sent_at = None
        self.update_diagnostics()

    def update_diagnostics(self) -> None:
        if not hasattr(self, "diagnostics_connection_value"):
            return
        connected = self.is_room_connected()
        reconnecting = self.reconnect_enabled and (
            self.reconnect_timer.isActive()
            or self.sync_client.socket.state() == QAbstractSocket.ConnectingState
            or self.reconnect_status.startswith("attempt")
        ) and not connected
        if connected:
            connection_text = "Connected"
        elif reconnecting:
            connection_text = "Reconnecting"
        else:
            connection_text = "Offline"
        self.set_label_text_safe(
            self.diagnostics_connection_value,
            connection_text,
            elide_mode=Qt.ElideRight,
        )
        self.set_label_text_safe(
            self.diagnostics_ping_value,
            f"{self.last_ping_ms} ms" if self.last_ping_ms is not None else "Unknown",
            elide_mode=Qt.ElideRight,
        )
        self.set_label_text_safe(
            self.diagnostics_sync_value,
            self.diagnostics_sync_text(),
            elide_mode=Qt.ElideRight,
        )
        self.set_label_text_safe(
            self.diagnostics_reconnect_value,
            self.reconnect_status,
            elide_mode=Qt.ElideRight,
        )
        self.resync_button.setEnabled(connected and self.last_room_payload is not None)

    def current_drift_text(self) -> str:
        if not self.last_room_payload or self.last_polled_position_ms is None:
            return "Unknown"
        media_url = str(self.last_room_payload.get("media_url") or "")
        if not media_url or media_url != self.current_media_url:
            return "Unknown"
        drift_ms = int(self.last_polled_position_ms) - int(self.last_room_payload.get("position_ms") or 0)
        sign = "+" if drift_ms >= 0 else "-"
        return f"{sign}{abs(drift_ms)} ms"

    def diagnostics_sync_text(self) -> str:
        if self.room_sync_state == "live":
            return "Live"
        if self.room_sync_state == "recovering":
            return "Syncing"
        if self.room_sync_state == "reconnecting":
            return "Reconnecting"
        if self.room_sync_state == "connected":
            return "Connected"
        return "Unknown"

    def copy_debug_report(self) -> None:
        log_root = safe_logs_dir()
        profile = self.reconnect_profile or {}
        player_status: dict[str, object] = {}
        try:
            player_status = self.player.get_status()
        except Exception as exc:
            player_status = {"error": str(exc)}
        payload = self.last_room_payload or {}
        app_path = Path(sys.executable if getattr(sys, "frozen", False) else sys.argv[0]).resolve()
        updater_path = app_path.with_name("SyncRoomUpdate.exe")
        lines = [
            f"SyncRoom version: {__version__}",
            f"OS/platform: {platform.platform()}",
            f"Python version: {platform.python_version()}",
            f"Frozen: {bool(getattr(sys, 'frozen', False))}",
            f"App path: {app_path}",
            f"Executable: {sys.executable}",
            f"Logs folder: {log_root}",
            f"runtime.log: {runtime_log_path()}",
            f"update.log: {update_log_path()}",
            f"crash.log: {log_root / 'crash.log'}",
            f"Server: {profile.get('host', self.selected_host())}:{profile.get('port', self.port_input.text().strip() or '24873')}",
            f"Room: {profile.get('room', self.room_input.text().strip() or 'movie-night')}",
            f"Display name: {profile.get('name', self.name_input.text().strip() or 'guest')}",
            f"Connection state: {self.diagnostics_connection_value.text()}",
            f"Reconnect enabled: {self.reconnect_enabled}",
            f"Reconnect attempt/status: {self.reconnect_attempt} / {self.reconnect_status}",
            f"Last connection error: {self.last_connection_error or '<none>'}",
            f"Room sync: {self.room_sync_state} / {self.room_sync_note or '<none>'}",
            f"Approx drift: {self.current_drift_text()}",
            f"Current media URL: {self.current_media_url or '<none>'}",
            f"mpv available: {self.player.mpv_available()}",
            f"mpv running: {self.player.is_running()}",
            f"mpv path: {self.player.mpv_path}",
            f"yt-dlp available: {self.player.yt_dlp_available()}",
            f"yt-dlp path: {self.player.yt_dlp_path() or '<none>'}",
            f"Selected streaming quality: {self.settings_panel.streaming_quality()}",
            f"Effective ytdl-format: {self.player.effective_ytdl_format()}",
            f"Windows runtime mpv cache path: {windows_runtime_mpv_path() if os.name == 'nt' else '<not Windows>'}",
            f"Windows runtime mpv cache available: {windows_mpv_available() if os.name == 'nt' else '<not Windows>'}",
            f"Windows runtime yt-dlp cache path: {windows_runtime_yt_dlp_path() if os.name == 'nt' else '<not Windows>'}",
            f"Windows runtime yt-dlp cache available: {windows_yt_dlp_available() if os.name == 'nt' else '<not Windows>'}",
            f"Player status: {player_status}",
            f"Last payload event_id: {payload.get('event_id', '<none>')}",
            f"Last payload seek_token: {payload.get('seek_token', '<none>')}",
            f"Last payload playing: {payload.get('playing', '<none>')}",
            f"Last payload media present: {bool(payload.get('media_url'))}",
            f"Current member count: {self.current_member_count}",
            f"Windows updater exists: {updater_path.exists() if os.name == 'nt' else 'not Windows'}",
        ]
        QApplication.clipboard().setText("\n".join(lines))
        self.show_status("Debug report copied.")

    def open_logs_folder(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(safe_logs_dir())))
        self.show_status("Opened logs folder.")

    def on_connection_change(self, connected: bool) -> None:
        if connected:
            was_reconnecting = self.reconnect_attempt > 0 or self.reconnect_status.startswith("attempt")
            self.last_connected_at = time.monotonic()
            self.reconnect_attempt = 0
            self.reconnect_status = "idle"
            self.last_connection_error = ""
            self.reconnect_timer.stop()
            if not self.diagnostics_timer.isActive():
                self.diagnostics_timer.start()
            self.update_connection_state(True)
            self.closing_for_mpv_exit = False
            self.update_room_sync_state("connected", "waiting for room state")
            self.page_stack.setCurrentIndex(1)
            self.schedule_elide_refresh()
            self.update_diagnostics()
            self.send_diagnostics_ping()
            self.show_status("Reconnected" if was_reconnecting else "Connected to server")
        else:
            self.diagnostics_timer.stop()
            self.pending_ping_sent_at = None
            if (
                self.reconnect_enabled
                and not self.user_requested_disconnect
                and not self.update_in_progress
                and self.reconnect_profile
            ):
                self.current_member_count = 0
                self.player_seen_running_in_room = False
                self.set_label_text_safe(self.members_badge, "0 viewers", elide_mode=Qt.ElideRight)
                self.set_stat_card_text(
                    self.viewer_stat,
                    value="0",
                    caption="reconnecting to room",
                    caption_elide_mode=Qt.ElideRight,
                )
                self.update_connection_state(False, reconnecting=True)
                self.schedule_reconnect()
                self.schedule_elide_refresh()
                return
            self.update_connection_state(False)
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
            self.schedule_elide_refresh()
            self.update_diagnostics()
            self.show_status("Disconnected")

    def on_audio_preference_changed(self) -> None:
        self.pending_audio_attempts = 10
        self.apply_audio_preferences_with_retry()
        self.refresh_audio_tracks(silent=True)
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
        elif self.current_media_url:
            self.refresh_audio_tracks(silent=True)

    def apply_audio_preferences(self) -> bool:
        preferences = self.settings_panel.audio_preferences()
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

    def on_subtitle_preference_changed(self) -> None:
        self.pending_subtitle_attempts = 10
        self.apply_subtitle_preferences_with_retry()
        self.refresh_subtitle_tracks(silent=True)
        self.persist_settings()

    def subtitle_preferences(self) -> list[str]:
        return self.settings_panel.subtitle_preferences()

    def on_streaming_quality_changed(self, _quality: str = "") -> None:
        self.player.set_ytdl_format(self.settings_panel.ytdl_format())
        self.persist_settings()
        self.show_status(f"Streaming quality set to {self.settings_panel.streaming_quality()}")

    def on_playback_notifications_changed(self, enabled: bool) -> None:
        self.show_playback_osd = bool(enabled)
        self.persist_settings()
        self.show_status("Playback notifications enabled" if enabled else "Playback notifications disabled")

    def apply_subtitle_preferences_with_retry(self) -> None:
        self.refresh_subtitle_tracks(silent=True)
        applied = self.apply_subtitle_preferences()
        if applied:
            self.pending_subtitle_attempts = 0
            QTimer.singleShot(300, self.refresh_subtitle_tracks)
            return
        if self.pending_subtitle_attempts > 0 and self.current_media_url:
            self.pending_subtitle_attempts -= 1
            QTimer.singleShot(450, self.apply_subtitle_preferences_with_retry)
        elif self.current_media_url:
            self.refresh_subtitle_tracks(silent=True)

    def apply_subtitle_preferences(self) -> bool:
        if not self.current_media_url:
            return False
        mode = self.settings_panel.subtitle_mode()
        try:
            if mode == "off":
                self.player.disable_subtitles()
                return True
            track = self.player.select_best_subtitle(mode, self.subtitle_preferences())
        except Exception as exc:
            append_runtime_log(f"apply_subtitle_preferences failed: {exc}")
            return False
        if track:
            self.show_status(f"Using subtitle track: {self.player.describe_subtitle_track(track)}")
            self.persist_settings()
            return True
        if mode == "english_signs" and self.pending_subtitle_attempts <= 0:
            self.show_status("No English Signs & Songs subtitle track found.")
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

    def refresh_subtitle_tracks(self, silent: bool = False) -> None:
        self.subtitle_track_combo.clear()
        if not self.current_media_url:
            self.subtitle_track_combo.addItem("No media loaded", None)
            return
        try:
            self.subtitle_tracks = self.player.list_subtitle_tracks()
        except Exception as exc:
            self.subtitle_track_combo.addItem("Could not read subtitles", None)
            if not silent:
                self.show_transient_error(f"Could not read subtitle tracks: {exc}")
            return
        if not self.subtitle_tracks:
            self.subtitle_track_combo.addItem("No subtitle tracks found", None)
            return
        selected_index = 0
        for track in self.subtitle_tracks:
            suffix = " [selected]" if track.get("selected") else ""
            self.subtitle_track_combo.addItem(
                f"{self.player.describe_subtitle_track(track)}{suffix}",
                int(track["id"]),
            )
            if track.get("selected"):
                selected_index = self.subtitle_track_combo.count() - 1
        self.subtitle_track_combo.setCurrentIndex(selected_index)

    def use_selected_subtitle_track(self) -> None:
        track_id = self.subtitle_track_combo.currentData()
        if track_id is None:
            return
        try:
            self.player.set_subtitle_track(int(track_id))
            QTimer.singleShot(300, self.refresh_subtitle_tracks)
            self.show_status("Switched subtitle track")
        except Exception as exc:
            self.show_error(f"Could not switch subtitle track: {exc}")

    @staticmethod
    def describe_audio_track(track: dict) -> str:
        lang = str(track.get("lang") or "unknown")
        title = str(track.get("title") or "").strip()
        if title:
            return f"{lang} - {title}"
        return lang

    @staticmethod
    def format_osd_time(position_ms: int) -> str:
        total_seconds = max(0, int(position_ms) // 1000)
        minutes, seconds = divmod(total_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    @staticmethod
    def format_osd_actor_name(name: str, fallback: str) -> str:
        clean_name = " ".join(str(name or "").strip().split()) or fallback
        if len(clean_name) > 24:
            clean_name = clean_name[:21].rstrip() + "..."
        return clean_name

    def remote_actor_name(self, payload: dict) -> str:
        name = str(payload.get("updated_by_name") or "").strip()
        updated_by = str(payload.get("updated_by") or "")
        if not name and updated_by:
            for member in payload.get("members") or []:
                if str(member.get("id") or "") == updated_by:
                    name = str(member.get("name") or "").strip()
                    break
        return self.format_osd_actor_name(name, "Someone")

    def local_actor_name(self) -> str:
        name = self.name_input.text().strip() if hasattr(self, "name_input") else ""
        if not name:
            name = str(getattr(self.sync_client, "name", "") or "").strip()
        return self.format_osd_actor_name(name, "guest")

    def build_action_osd_message(self, action: str, actor_name: str, position_ms: int = 0) -> str | None:
        action = str(action or "").strip().lower()
        if action == "pause":
            return f"{actor_name} paused at {self.format_osd_time(position_ms)}"
        if action == "play":
            return f"{actor_name} resumed"
        if action == "seek":
            return f"{actor_name} skipped to {self.format_osd_time(position_ms)}"
        if action == "load":
            return f"{actor_name} loaded new media"
        return None

    def build_remote_action_osd_message(self, payload: dict, actor_name: str) -> str | None:
        return self.build_action_osd_message(
            str(payload.get("last_action") or ""),
            actor_name,
            int(payload.get("position_ms") or 0),
        )

    def show_local_playback_osd(self, action: str, position_ms: int = 0) -> None:
        if not self.show_playback_osd:
            return
        action = str(action or "").strip().lower()
        if action not in {"play", "pause", "seek", "load"}:
            return
        message = self.build_action_osd_message(action, self.local_actor_name(), position_ms)
        if not message:
            return
        signature = "|".join(
            [
                action,
                str(max(0, int(position_ms)) // 1000),
                self.current_media_url if action == "load" else "",
            ]
        )
        now = time.monotonic()
        if signature == self.last_local_osd_signature and now - self.last_local_osd_at < 1.5:
            return
        self.last_local_osd_signature = signature
        self.last_local_osd_at = now
        self.player.show_osd_message(message)

    def maybe_show_remote_playback_osd(self, payload: dict, *, media_switch: bool = False) -> None:
        event_id = int(payload.get("event_id") or 0)
        action = str(payload.get("last_action") or "").strip().lower()
        updated_by = str(payload.get("updated_by") or "")
        if event_id <= 0:
            return
        if event_id <= self.last_osd_event_id:
            return
        if not self.show_playback_osd or updated_by == self.sync_client.client_id:
            self.last_osd_event_id = max(self.last_osd_event_id, event_id)
            return
        if action not in {"play", "pause", "seek", "load"}:
            self.last_osd_event_id = max(self.last_osd_event_id, event_id)
            return
        if media_switch and action != "load":
            self.last_osd_event_id = max(self.last_osd_event_id, event_id)
            return

        signature = "|".join(
            [
                updated_by,
                action,
                str(int(payload.get("position_ms") or 0)),
                str(int(payload.get("seek_token") or 0)),
                str(payload.get("media_url") or ""),
            ]
        )
        if signature and signature == self.last_osd_signature:
            self.last_osd_event_id = max(self.last_osd_event_id, event_id)
            return

        message = self.build_remote_action_osd_message(payload, self.remote_actor_name(payload))
        self.last_osd_event_id = max(self.last_osd_event_id, event_id)
        self.last_osd_signature = signature
        if message:
            self.player.show_osd_message(message)

    @staticmethod
    def format_ms(value: int) -> str:
        total_seconds = max(0, value // 1000)
        minutes, seconds = divmod(total_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self.update_in_progress:
            append_update_log("closeEvent accepted immediately during update handoff")
            self.best_effort_update_shutdown()
            event.accept()
            return
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
            dialog = UpdateAvailableDialog(__version__, info.latest_version)
            if dialog.exec() == QDialog.Accepted:
                self.launch_bundled_updater_and_exit(info)
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

    def launch_bundled_updater_and_exit(self, info: UpdateInfo) -> None:
        append_update_log("update accepted by user")
        if os.name != "nt" or not getattr(sys, "frozen", False):
            append_update_log("Automatic in-app updating requested outside packaged Windows build")
            self.show_update_error(
                "Automatic in-app updating is only available in the packaged Windows build."
            )
            return

        if not info.asset_url:
            append_update_log("Update handoff rejected because asset_url is missing")
            self.show_update_error("The latest release does not include a downloadable installer.")
            return
        if info.asset_name.lower() != "syncroom-setup.exe":
            append_update_log(
                f"Update handoff rejected because asset_name={info.asset_name or '<none>'}"
            )
            self.show_update_error("The latest release did not include SyncRoom-Setup.exe.")
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
            "updater launch requested "
            f"updater={updater_path} app_path={app_path} pid={os.getpid()} "
            f"latest_version={info.latest_version} asset_name={info.asset_name} asset_url={info.asset_url}"
        )
        command = [
            str(updater_path),
            "--apply-update",
            "--version",
            info.latest_version,
            "--asset-url",
            info.asset_url,
            "--asset-name",
            info.asset_name,
            "--app-path",
            str(app_path),
            "--pid",
            str(os.getpid()),
        ]
        try:
            process = subprocess.Popen(
                command,
                creationflags=creationflags,
                close_fds=True,
                cwd=str(app_path.parent),
            )
        except Exception as exc:
            append_update_log(f"Bundled updater launch failed: {exc}")
            self.show_update_error(f"Could not launch the updater: {exc}")
            return
        append_update_log(f"updater process PID pid={process.pid}")
        self.update_in_progress = True
        append_update_log("update handoff flag set")
        self.hide()
        append_update_log("main window hidden")
        app = QApplication.instance()
        if app is not None:
            app.processEvents()
        self.best_effort_update_shutdown()
        append_update_log("hard exit starting")
        flush_fault_log()
        os._exit(0)

    def best_effort_update_shutdown(self) -> None:
        try:
            self.persist_settings()
        except Exception as exc:
            append_update_log(f"best-effort settings save failed during update handoff: {exc}")
        try:
            if self.heartbeat.isActive():
                self.heartbeat.stop()
                append_update_log("heartbeat timer stopped during update handoff")
        except Exception as exc:
            append_update_log(f"heartbeat timer stop failed during update handoff: {exc}")
        try:
            self.sync_client.socket.abort()
            append_update_log("network socket aborted during update handoff")
        except Exception as exc:
            append_update_log(f"network socket abort failed during update handoff: {exc}")
        for thread in list(self._active_threads):
            try:
                if thread.isRunning():
                    thread.quit()
                    append_update_log("background worker quit requested during update handoff")
            except Exception as exc:
                append_update_log(f"background worker shutdown failed during update handoff: {exc}")

    def force_update_handoff_exit(self) -> None:
        if not self.update_in_progress:
            return
        append_update_log("fallback os._exit triggered during update handoff")
        os._exit(0)

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
                "host": self.custom_host_value.strip() or "127.0.0.1",
                "host_mode": self.CUSTOM_SERVER_LABEL if self.host_select.currentIndex() == 1 else self.HOSTED_SERVER_LABEL,
                "port": self.custom_port_value.strip() or "24873",
                "room": self.room_input.text().strip(),
                "room_password": self.password_input.text(),
                "name": self.name_input.text().strip(),
                "audio_mode": self.settings_panel.audio_mode(),
                "audio_preferences": self.settings_panel.audio_preferences_text(),
                "audio_custom_preferences": self.settings_panel.audio_custom_preferences(),
                "subtitle_mode": self.settings_panel.subtitle_mode(),
                "subtitle_custom_preferences": self.settings_panel.subtitle_custom_preferences(),
                "streaming_quality": self.settings_panel.streaming_quality(),
                "playback_osd": self.settings_panel.playback_notifications_enabled(),
            }
        )

    def update_connection_state(self, connected: bool, reconnecting: bool = False) -> None:
        if connected:
            badge_text = "CONNECTED"
            badge_name = "onlineBadge"
        elif reconnecting:
            badge_text = "RECONNECTING"
            badge_name = "reconnectingBadge"
        else:
            badge_text = "OFFLINE"
            badge_name = "offlineBadge"
        self.connection_badge.setText(badge_text)
        self.connection_badge.setObjectName(badge_name)
        self.connection_badge.style().unpolish(self.connection_badge)
        self.connection_badge.style().polish(self.connection_badge)
        room_name = self.room_input.text().strip() or "not connected"
        self.set_label_text_safe(
            self.room_badge,
            f"Room: {room_name if connected or reconnecting else 'not connected'}",
            elide_mode=Qt.ElideRight,
        )
        self.update_room_sync_state("connected" if connected else "reconnecting" if reconnecting else "idle")
        self.set_stat_card_text(
            self.room_name_stat,
            value=room_name if connected or reconnecting else "not connected",
            caption=(
                "share this room name with everyone"
                if connected
                else "trying to restore the room"
                if reconnecting
                else "join a room to begin"
            ),
            value_elide_mode=Qt.ElideMiddle,
        )
        self.schedule_elide_refresh()
        self.update_diagnostics()


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
