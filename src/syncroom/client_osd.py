from __future__ import annotations

import time


class ClientOsdMixin:
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
