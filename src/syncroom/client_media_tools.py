from __future__ import annotations

import os
import platform
import shutil
import time
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication

from syncroom.ui.dialogs import ProgressScreenDialog
from syncroom.utils.logging import append_runtime_log
from syncroom.windows_runtime import (
    ensure_windows_mpv_runtime,
    mpv_version,
    needs_yt_dlp_update,
    update_windows_yt_dlp_runtime,
    windows_runtime_root,
    yt_dlp_version,
)


class MediaToolsWorker(QObject):
    progress = Signal(str, int)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, action: str) -> None:
        super().__init__()
        self.action = action

    def run(self) -> None:
        try:
            if self.action == "repair_mpv":
                if os.name != "nt":
                    raise RuntimeError("mpv is managed by your Linux package manager.")
                path = ensure_windows_mpv_runtime(self._report)
                self.finished.emit({"action": self.action, "path": str(path), "message": "mpv repaired."})
                return
            if self.action == "update_yt_dlp":
                path = update_windows_yt_dlp_runtime(self._report)
                self.finished.emit({"action": self.action, "path": str(path), "message": "yt-dlp updated."})
                return
            if self.action == "auto_yt_dlp":
                if os.name == "nt" and needs_yt_dlp_update():
                    path = update_windows_yt_dlp_runtime(self._report)
                    self.finished.emit({"action": self.action, "path": str(path), "message": "yt-dlp updated."})
                    return
                self.finished.emit({"action": self.action, "path": "", "message": "yt-dlp already up to date."})
                return
            raise RuntimeError(f"Unknown media tools action: {self.action}")
        except Exception as exc:
            self.failed.emit(str(exc))

    def _report(self, message: str, percent: int) -> None:
        self.progress.emit(message, percent)


class ClientMediaToolsMixin:
    def on_yt_dlp_auto_update_changed(self, enabled: bool) -> None:
        self.persist_settings()
        self.show_status("yt-dlp auto-update enabled" if enabled else "yt-dlp auto-update disabled")
        if enabled:
            self.maybe_start_yt_dlp_auto_update()

    def refresh_media_tools_status(self, note: str = "Media tool status refreshed.") -> None:
        mpv_path = self.player.mpv_path
        yt_dlp_path = self.player.yt_dlp_path()
        mpv_available = self.player.mpv_available()
        yt_dlp_available = self.player.yt_dlp_available()
        mpv_text = mpv_version(mpv_path) if mpv_available else ""
        yt_dlp_text = yt_dlp_version(yt_dlp_path) if yt_dlp_path else ""
        if os.name != "nt":
            note = self.linux_media_tools_hint()
        payload = {
            "mpv_status": "Installed" if mpv_available else "Missing",
            "mpv_version": mpv_text or "Unknown",
            "mpv_path": mpv_path or "Unknown",
            "yt_dlp_status": "Installed" if yt_dlp_available else "Missing",
            "yt_dlp_version": yt_dlp_text or "Unknown",
            "yt_dlp_path": yt_dlp_path or "Unknown",
            "note": note,
        }
        self.settings_panel.set_media_tools_status(payload)

    def linux_media_tools_hint(self) -> str:
        if os.name == "nt":
            return ""
        distro_hint = platform.platform().lower()
        arch_like = Path("/etc/arch-release").exists() or "cachy" in distro_hint or "arch" in distro_hint
        if arch_like:
            return "Managed by your package manager. On Arch/CachyOS, update with: sudo pacman -Syu yt-dlp"
        return "Managed by your package manager. Update yt-dlp with your distro's normal package update command."

    def update_yt_dlp_now(self) -> None:
        if os.name != "nt":
            self.refresh_media_tools_status(self.linux_media_tools_hint())
            self.show_status("yt-dlp is managed by your system package manager on Linux.")
            return
        self.run_media_tools_action("update_yt_dlp", "Updating yt-dlp...", show_dialog=True)

    def repair_mpv_runtime(self) -> None:
        if os.name != "nt":
            self.show_status("mpv is managed by your system package manager on Linux.")
            self.refresh_media_tools_status("mpv is managed by your system package manager on Linux.")
            return
        self.run_media_tools_action("repair_mpv", "Repairing mpv...", show_dialog=True)

    def open_media_tools_folder(self) -> None:
        if os.name == "nt":
            folder = windows_runtime_root()
            folder.mkdir(parents=True, exist_ok=True)
        else:
            path = self.player.yt_dlp_path() or shutil.which("mpv") or str(Path.home())
            folder = Path(path).parent if path else Path.home()
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
        self.show_status("Opened media tools folder.")

    def copy_media_tools_report(self) -> None:
        QApplication.clipboard().setText(self.settings_panel.media_tools_report())
        self.show_status("Media tools report copied.")

    def maybe_start_yt_dlp_auto_update(self) -> None:
        if not self.settings_panel.yt_dlp_auto_update_enabled():
            return
        if os.name != "nt":
            self.refresh_media_tools_status("System yt-dlp is managed outside SyncRoom on Linux.")
            return
        now = time.time()
        if now - self.yt_dlp_last_check < 24 * 60 * 60:
            self.refresh_media_tools_status("yt-dlp auto-update was checked within the last 24 hours.")
            return
        self.yt_dlp_last_check = now
        self.yt_dlp_last_status = "Checking yt-dlp..."
        self.persist_settings()
        self.run_media_tools_action("auto_yt_dlp", "Checking yt-dlp...", show_dialog=False)

    def run_media_tools_action(self, action: str, title: str, *, show_dialog: bool) -> None:
        if self.media_tools_action_running:
            self.show_status("Media Tools is already working. Please wait.")
            return
        self.media_tools_action_running = True
        self.settings_panel.set_media_tools_actions_enabled(False)
        worker = MediaToolsWorker(action)
        thread = QThread()
        worker.moveToThread(thread)
        dialog: ProgressScreenDialog | None = None
        state = {
            "success": False,
            "finished": False,
            "message": "Media tools updated.",
        }
        if show_dialog:
            dialog = ProgressScreenDialog("MEDIA TOOLS", title, "SyncRoom is preparing local media helpers.")
            dialog.set_progress("Starting...", 0)
            worker.progress.connect(dialog.set_progress)
        else:
            worker.progress.connect(lambda message, _percent: self.show_status(message))

        def finish(result: object) -> None:
            data = result if isinstance(result, dict) else {}
            message = str(data.get("message") or "Media tools updated.")
            state["success"] = True
            state["finished"] = True
            state["message"] = message
            self.yt_dlp_last_status = message
            if dialog is not None:
                dialog.set_progress(f"{message} Closing...", 100)
            thread.quit()

        def fail(message: str) -> None:
            state["success"] = False
            state["finished"] = True
            state["message"] = message
            self.yt_dlp_last_status = message
            append_runtime_log(f"Media tools action failed action={action}: {message}")
            if dialog is not None:
                dialog.show_failure(f"Media tools action failed:\n{message}")
            if not show_dialog:
                self.show_status("yt-dlp auto-update check failed; see logs.")
            thread.quit()

        def complete_action() -> None:
            if not self.media_tools_action_running:
                return
            self.media_tools_action_running = False
            self.settings_panel.set_media_tools_actions_enabled(True)
            if action in {"update_yt_dlp", "auto_yt_dlp"}:
                self.player.set_ytdl_format(self.settings_panel.ytdl_format())
            note = str(state["message"])
            if not bool(state["success"]):
                note = f"Media tools action failed: {note}"
            self.refresh_media_tools_status(note)
            self.show_status(str(state["message"]) if bool(state["success"]) else "Media tools action failed.")

        def on_thread_finished() -> None:
            if dialog is not None and bool(state["success"]):
                QTimer.singleShot(1000, dialog.accept)
            elif dialog is None:
                complete_action()
            self._release_background_job(thread, worker)

        worker.finished.connect(finish)
        worker.failed.connect(fail)
        thread.started.connect(worker.run)
        thread.finished.connect(on_thread_finished)
        self._track_background_job(thread, worker)
        thread.start()
        if dialog is not None:
            dialog.exec()
            complete_action()
