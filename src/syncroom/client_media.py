from __future__ import annotations

import os
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QMessageBox

from syncroom.utils.logging import append_runtime_log


class ClientMediaMixin:
    def set_media_url(self, media_url: str, broadcast: bool) -> None:
        previous_media_url = self.current_media_url
        if media_url != previous_media_url:
            self.fallback_prompt_seen.clear()
            if self.pending_room_sync is not None:
                append_runtime_log(
                    "Clearing pending sync because media changed "
                    f"old_media={previous_media_url or '<none>'} new_media={media_url or '<none>'}"
                )
                self.clear_pending_room_sync()
        previous_status: dict[str, object] = {}
        try:
            previous_status = self.player.get_status()
        except Exception:
            previous_status = {}
        self.start_media_switch(media_url, previous_status)
        self.current_media_url = media_url
        self.set_label_text_safe(
            self.current_media_label,
            media_url or "No media loaded yet",
            elide_mode=Qt.ElideMiddle,
        )
        append_runtime_log(f"Loading media broadcast={broadcast}")
        loaded = False
        try:
            self.suppress_sync = True
            if os.name == "nt" and self.player.needs_windows_mpv_runtime_install():
                self.show_status("First-time setup: downloading mpv for Windows...")
            self.player.mark_media_load_start(media_url)
            self.player.load(media_url)
            loaded = True
            self.position_slider.setRange(0, 0)
            self.position_slider.setValue(0)
            self.position_label.setText("00:00")
            self.duration_label.setText("00:00")
            self.last_known_playing = False
            self.player_seen_running_in_room = False
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
            self.start_media_load_watchdog(media_url)
            self.persist_settings()
        except Exception as exc:
            append_runtime_log(f"set_media_url failed: {exc}")
            hint = ""
            if not self.player.yt_dlp_available():
                hint = " This link may require yt-dlp. Direct video links still work."
            self.show_error(
                f"Could not start mpv. SyncRoom could not launch or install the player runtime. Details: {exc}{hint}"
            )
            self.finish_media_switch(False)
        finally:
            self.suppress_sync = False
        if broadcast and loaded:
            self.sync_client.send_state(self.current_media_url, 0, False, reason="load")

    def start_media_switch(self, media_url: str, previous_status: dict[str, object] | None = None) -> None:
        self.media_load_generation += 1
        self.media_switch_in_progress = True
        self.loading_media_url = media_url
        self.media_loaded_confirmed = False
        self.media_switch_started_at = time.monotonic()
        self.previous_mpv_media_url = str((previous_status or {}).get("media_url") or "")
        self.last_polled_position_ms = None
        self.last_polled_playing = None
        self.last_poll_monotonic = None
        self.behind_sync_detected_at = None
        self.local_playback_override_until = 0.0
        self.local_playback_target = None
        self.local_seek_override_until = 0.0
        self.local_seek_target_ms = 0
        append_runtime_log(
            "media switch started "
            f"generation={self.media_load_generation} media_url={media_url} "
            f"previous_mpv_media_url={self.previous_mpv_media_url or '<none>'}"
        )

    def finish_media_switch(self, confirmed: bool) -> None:
        self.media_switch_in_progress = False
        self.media_loaded_confirmed = confirmed
        if not confirmed:
            self.loading_media_url = ""
        append_runtime_log(
            "media switch finished "
            f"generation={self.media_load_generation} confirmed={confirmed} "
            f"media_url={self.current_media_url or self.loading_media_url or '<none>'}"
        )

    def is_online_media_url(self, media_url: str) -> bool:
        return media_url.strip().lower().startswith(("http://", "https://"))

    def normalize_media_path(self, value: str) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            return ""
        if self.is_online_media_url(cleaned):
            return cleaned
        try:
            return str(Path(cleaned).expanduser().resolve())
        except Exception:
            return os.path.normcase(os.path.abspath(os.path.expanduser(cleaned)))

    def local_media_paths_match(self, current_media_url: str, mpv_media_url: str) -> bool:
        return self.normalize_media_path(current_media_url) == self.normalize_media_path(mpv_media_url)

    def mpv_status_matches_current_media(self, status: dict) -> bool:
        if not self.current_media_url:
            return False
        mpv_media_url = str(status.get("media_url") or "").strip()
        if not mpv_media_url:
            return False
        duration = int(status.get("duration_ms") or 0)
        position = int(status.get("position_ms") or 0)
        if self.is_online_media_url(self.current_media_url):
            if self.media_switch_in_progress and mpv_media_url == self.previous_mpv_media_url:
                elapsed = max(0.0, time.monotonic() - self.media_switch_started_at)
                if elapsed < 1.5 or position > 3000:
                    return False
            if duration > 0:
                return True
            if int(status.get("audio_track_count") or 0) > 0 or int(status.get("video_track_count") or 0) > 0:
                return True
            elapsed = max(0.0, time.monotonic() - self.media_switch_started_at)
            return (
                elapsed >= self.ONLINE_MEDIA_CONFIRM_DELAY_SECONDS
                and bool(status.get("time_pos_available"))
                and not bool(status.get("idle_active"))
                and not bool(status.get("core_idle"))
            )
        return self.local_media_paths_match(self.current_media_url, mpv_media_url)

    def maybe_confirm_media_loaded(self, status: dict) -> bool:
        if not self.media_switch_in_progress:
            return self.mpv_status_matches_current_media(status)
        if self.loading_media_url != self.current_media_url:
            return False
        if not self.mpv_status_matches_current_media(status):
            return False
        position = int(status.get("position_ms") or 0)
        playing = bool(status.get("playing"))
        self.finish_media_switch(True)
        self.remember_polled_state(position, playing)
        self.last_known_playing = playing
        append_runtime_log(
            "media load confirmed "
            f"generation={self.media_load_generation} media_url={self.current_media_url} "
            f"mpv_media_url={status.get('media_url') or '<none>'} "
            f"position_ms={position} duration_ms={int(status.get('duration_ms') or 0)}"
        )
        return True

    def lower_streaming_quality(self, quality: str) -> str:
        order = ["360p", "480p", "720p", "1080p", "4k"]
        try:
            index = order.index(quality)
        except ValueError:
            return ""
        if index <= 0:
            return ""
        return order[index - 1]

    def start_media_load_watchdog(self, media_url: str) -> None:
        if not self.is_online_media_url(media_url):
            return
        quality = self.settings_panel.streaming_quality()
        if not self.lower_streaming_quality(quality):
            return
        self.media_load_watchdog_token += 1
        token = self.media_load_watchdog_token
        QTimer.singleShot(15000, lambda: self.check_media_load_watchdog(token, media_url, quality))

    def check_media_load_watchdog(self, token: int, media_url: str, quality: str) -> None:
        if token != self.media_load_watchdog_token or media_url != self.current_media_url:
            return
        if (media_url, quality) in self.fallback_prompt_seen:
            return
        if self.media_is_ready(media_url):
            return
        mpv_error = self.player.recent_mpv_error_summary()
        if mpv_error:
            self.fallback_prompt_seen.add((media_url, quality))
            self.show_media_load_error_prompt(mpv_error)
            return
        lower_quality = self.lower_streaming_quality(quality)
        if not lower_quality:
            return
        self.fallback_prompt_seen.add((media_url, quality))
        dialog = QMessageBox(self)
        dialog.setWindowTitle("SyncRoom")
        dialog.setIcon(QMessageBox.Question)
        dialog.setText("This video is taking a while to load. Try lower streaming quality?")
        dialog.setInformativeText(f"SyncRoom can retry this media locally at {lower_quality}. Other room members are not affected.")
        try_button = dialog.addButton("Try lower quality", QMessageBox.AcceptRole)
        dialog.addButton("Keep waiting", QMessageBox.RejectRole)
        dialog.exec()
        if dialog.clickedButton() is try_button:
            self.try_lower_streaming_quality(media_url, lower_quality)

    def show_media_load_error_prompt(self, mpv_error: str) -> None:
        guidance = self.media_load_error_guidance(mpv_error)
        dialog = QMessageBox(self)
        dialog.setWindowTitle("SyncRoom")
        dialog.setIcon(QMessageBox.Warning)
        dialog.setText(guidance)
        dialog.setInformativeText(mpv_error[:900])
        dialog.addButton("OK", QMessageBox.AcceptRole)
        dialog.exec()

    def media_load_error_guidance(self, mpv_error: str) -> str:
        lowered = mpv_error.lower()
        if any(fragment in lowered for fragment in ("private video", "sign in to confirm", "age-restricted", "video unavailable", "this video is unavailable")):
            return "This online video is unavailable or needs sign-in. Try another link."
        if any(fragment in lowered for fragment in ("requested format is not available", "no video formats found")):
            return "This quality is not available for the video. Try a lower streaming quality."
        if any(fragment in lowered for fragment in ("unable to extract", "yt-dlp failed", "youtube-dl failed")):
            return "yt-dlp could not read this video. Try Settings -> Media Tools -> Update yt-dlp."
        if any(fragment in lowered for fragment in ("http error", "403", "404")):
            return "The video host rejected the request. Try another link or update yt-dlp."
        return "This video could not be loaded. Check the link or update yt-dlp from Media Tools."

    def media_is_ready(self, media_url: str) -> bool:
        try:
            status = self.player.get_status()
        except Exception as exc:
            append_runtime_log(f"media_is_ready status failed: {exc}")
            return False
        if not status.get("running", False):
            return False
        loaded_path = str(status.get("media_url") or "")
        duration = int(status.get("duration_ms") or 0)
        track_count = int(status.get("track_count") or 0)
        has_loaded_evidence = (
            duration > 0
            or track_count > 0
            or bool(status.get("time_pos_available"))
            or not bool(status.get("idle_active"))
            or not bool(status.get("core_idle"))
        )
        return (
            bool(loaded_path)
            and media_url == self.current_media_url
            and self.mpv_status_matches_current_media(status)
            and has_loaded_evidence
        )

    def try_lower_streaming_quality(self, media_url: str, lower_quality: str) -> None:
        self.settings_panel.set_streaming_quality(lower_quality)
        self.player.set_ytdl_format(self.settings_panel.ytdl_format())
        self.persist_settings()
        self.show_status(f"Trying {lower_quality}...")
        self.set_media_url(media_url, broadcast=False)
