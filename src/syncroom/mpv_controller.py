from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from syncroom.windows_runtime import (
    ensure_windows_mpv_runtime,
    windows_mpv_available,
    windows_runtime_mpv_path,
)


class MpvController:
    AUDIO_ALIAS_GROUPS = [
        {"eng", "en", "english"},
        {"jp", "jpn", "ja", "jap", "japanese"},
    ]
    ENGLISH_ALIASES = {"eng", "en", "english"}
    SIGNS_SONGS_TOKENS = {"sign", "signs", "song", "songs", "forced", "ss", "signssongs"}
    NON_FULL_SUBTITLE_TOKENS = SIGNS_SONGS_TOKENS | {"sdh", "commentary", "comments"}

    def __init__(self) -> None:
        self.process: subprocess.Popen[bytes] | None = None
        self.ipc_path = self._build_ipc_path()
        self._request_id = 0
        self.mpv_path = self._resolve_mpv_path()
        self._attempted_runtime_install = False

    def _resolve_mpv_path(self) -> str:
        candidates: list[Path] = []
        if os.name == "nt":
            candidates.append(windows_runtime_mpv_path())
        else:
            candidates.extend(
                [
                    Path("/usr/bin/mpv"),
                    Path("/usr/local/bin/mpv"),
                ]
            )

        for candidate in candidates:
            if candidate.exists():
                return str(candidate)

        discovered = shutil.which("mpv")
        return discovered or "mpv"

    def yt_dlp_path(self) -> str:
        candidates: list[Path] = []
        exe_dir = Path(sys.executable).resolve().parent
        mpv_path = Path(self.mpv_path)
        discovered = shutil.which("yt-dlp")
        if discovered:
            return discovered
        candidates.append(exe_dir / "yt-dlp.exe")
        candidates.append(exe_dir / "yt-dlp")
        if os.name == "nt":
            candidates.append(windows_runtime_mpv_path().parent / "yt-dlp.exe")
        if mpv_path.parent:
            candidates.append(mpv_path.parent / "yt-dlp.exe")
            candidates.append(mpv_path.parent / "yt-dlp")
            candidates.append(mpv_path.parent / "runtime" / "yt-dlp.exe")

        for candidate in candidates:
            if candidate.exists():
                return str(candidate)

        return ""

    def yt_dlp_available(self) -> bool:
        return bool(self.yt_dlp_path())

    def _build_ipc_path(self) -> str:
        token = f"syncroom-{uuid.uuid4().hex}"
        if os.name == "nt":
            return rf"\\.\pipe\{token}"
        return str(Path(tempfile.gettempdir()) / f"{token}.sock")

    def ensure_running(self) -> None:
        if self.process is not None and self.process.poll() is None:
            return

        if os.name != "nt":
            with contextlib_suppress(FileNotFoundError):
                os.unlink(self.ipc_path)

        if os.name == "nt" and not self._mpv_command_exists():
            self._install_windows_runtime()

        command = [
            self.mpv_path,
            "--idle=yes",
            "--force-window=yes",
            "--keep-open=yes",
            f"--input-ipc-server={self.ipc_path}",
            "--title=SyncRoom Player",
            "--profile=sw-fast",
            "--osd-align-x=left",
            "--osd-align-y=top",
            "--osd-margin-x=24",
            "--osd-margin-y=24",
            "--osd-font-size=28",
        ]
        ytdlp_path = self.yt_dlp_path()
        if ytdlp_path:
            command.extend(
                [
                    "--ytdl=yes",
                    f"--script-opts-append=ytdl_hook-ytdl_path={ytdlp_path}",
                ]
            )
        try:
            self.process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            if os.name != "nt" or self._attempted_runtime_install:
                raise
            self._install_windows_runtime()
            command[0] = self.mpv_path
            self.process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        self._wait_for_ipc()

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def load(self, media_url: str) -> None:
        self.ensure_running()
        self.command(["loadfile", media_url, "replace"])
        self.command(["set_property", "pause", True])

    def play(self) -> None:
        self.command(["set_property", "pause", False])

    def pause(self) -> None:
        self.command(["set_property", "pause", True])

    def seek_absolute(self, position_ms: int) -> None:
        self.command(["set_property", "time-pos", max(0.0, position_ms / 1000.0)])

    def show_osd_message(self, text: str, duration_ms: int = 2800) -> None:
        if not self.is_running():
            return
        cleaned = "".join(
            ch if ch.isprintable() and ch not in "\r\n\t" else " "
            for ch in str(text or "")
        )
        cleaned = " ".join(cleaned.split())
        if not cleaned:
            return
        if len(cleaned) > 160:
            cleaned = cleaned[:157].rstrip() + "..."
        try:
            self.command(["show-text", cleaned, max(500, int(duration_ms))])
        except Exception:
            return

    def list_audio_tracks(self) -> list[dict[str, Any]]:
        track_list = self.get_property("track-list", [])
        audio_tracks: list[dict[str, Any]] = []
        for track in track_list or []:
            if track.get("type") != "audio":
                continue
            audio_tracks.append(
                {
                    "id": int(track.get("id") or 0),
                    "title": str(track.get("title") or ""),
                    "lang": str(track.get("lang") or ""),
                    "codec": str(track.get("codec") or ""),
                    "selected": bool(track.get("selected")),
                }
            )
        return audio_tracks

    def set_audio_track(self, track_id: int) -> None:
        self.command(["set_property", "aid", int(track_id)])

    def list_subtitle_tracks(self) -> list[dict[str, Any]]:
        track_list = self.get_property("track-list", [])
        subtitle_tracks: list[dict[str, Any]] = []
        for track in track_list or []:
            if track.get("type") != "sub":
                continue
            subtitle_tracks.append(
                {
                    "id": int(track.get("id") or 0),
                    "title": str(track.get("title") or ""),
                    "lang": str(track.get("lang") or ""),
                    "codec": str(track.get("codec") or ""),
                    "selected": bool(track.get("selected")),
                }
            )
        return subtitle_tracks

    def set_subtitle_track(self, track_id: int) -> None:
        self.command(["set_property", "sid", int(track_id)])

    def disable_subtitles(self) -> None:
        self.command(["set_property", "sid", "no"])

    def select_best_audio(self, preferences: list[str]) -> dict[str, Any] | None:
        tracks = self.list_audio_tracks()
        if not tracks:
            return None

        normalized_preferences = self._expand_preferences(preferences)
        if not normalized_preferences:
            return None

        for preference in normalized_preferences:
            for track in tracks:
                if self.track_matches_preference(track, preference):
                    self.set_audio_track(int(track["id"]))
                    return track
        return None

    def selected_audio_matches_preferences(self, preferences: list[str]) -> dict[str, Any] | None:
        normalized_preferences = self._expand_preferences(preferences)
        if not normalized_preferences:
            return None

        for track in self.list_audio_tracks():
            if not track.get("selected"):
                continue
            for preference in normalized_preferences:
                if self.track_matches_preference(track, preference):
                    return track
        return None

    def select_best_subtitle(
        self,
        mode: str,
        preferences: list[str] | None = None,
    ) -> dict[str, Any] | None:
        mode = mode.strip().lower()
        if mode == "off":
            self.disable_subtitles()
            return None

        tracks = self.list_subtitle_tracks()
        if not tracks:
            return None

        chosen: dict[str, Any] | None = None
        if mode == "english":
            english_tracks = [track for track in tracks if self.subtitle_is_english(track)]
            clean_tracks = [
                track
                for track in english_tracks
                if not self.subtitle_has_any_token(track, self.NON_FULL_SUBTITLE_TOKENS)
            ]
            chosen = clean_tracks[0] if clean_tracks else english_tracks[0] if len(english_tracks) == 1 else None
        elif mode == "english_signs":
            chosen = next(
                (
                    track
                    for track in tracks
                    if self.subtitle_is_english(track)
                    and self.subtitle_has_any_token(track, self.SIGNS_SONGS_TOKENS)
                ),
                None,
            )
        elif mode == "custom":
            normalized_preferences = self._expand_preferences(preferences or [])
            for preference in normalized_preferences:
                chosen = next(
                    (track for track in tracks if self.track_matches_preference(track, preference)),
                    None,
                )
                if chosen is not None:
                    break

        if chosen is None:
            return None
        self.set_subtitle_track(int(chosen["id"]))
        return chosen

    def stop(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        try:
            self.command(["quit"])
        except Exception:
            self.process.terminate()

    def get_status(self) -> dict[str, Any]:
        if not self.is_running():
            return {
                "running": False,
                "playing": False,
                "position_ms": 0,
                "duration_ms": 0,
                "media_url": "",
            }
        pause = self.get_property("pause", True)
        time_pos = self.get_property("time-pos", 0)
        duration = self.get_property("duration", 0)
        path = self.get_property("path", "")
        return {
            "running": True,
            "playing": not bool(pause),
            "position_ms": int(float(time_pos or 0) * 1000),
            "duration_ms": int(float(duration or 0) * 1000),
            "media_url": str(path or ""),
        }

    def get_property(self, name: str, default: Any = None) -> Any:
        try:
            return self.command(["get_property", name])
        except RuntimeError as exc:
            if "unavailable" in str(exc):
                return default
            raise

    def command(self, command: list[Any]) -> Any:
        if not self.is_running():
            raise RuntimeError("mpv is not running")
        self._request_id += 1
        payload = {
            "command": command,
            "request_id": self._request_id,
        }
        response = self._send_and_receive(payload)
        if response.get("error") not in {"success", None}:
            raise RuntimeError(str(response.get("error")))
        return response.get("data")

    def _wait_for_ipc(self) -> None:
        deadline = time.time() + 5
        while time.time() < deadline:
            if os.name == "nt":
                try:
                    handle = open(self.ipc_path, "r+b", buffering=0)
                    handle.close()
                    return
                except OSError:
                    time.sleep(0.1)
            else:
                if os.path.exists(self.ipc_path):
                    return
                time.sleep(0.1)
        raise RuntimeError("mpv started but IPC socket was not created")

    def _send_and_receive(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw = (json.dumps(payload) + "\n").encode("utf-8")
        if os.name == "nt":
            with open(self.ipc_path, "r+b", buffering=0) as handle:
                handle.write(raw)
                line = handle.readline()
        else:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(1.0)
                client.connect(self.ipc_path)
                client.sendall(raw)
                line = self._readline_from_socket(client)
        if not line:
            raise RuntimeError("No response from mpv")
        return json.loads(line.decode("utf-8"))

    def _mpv_command_exists(self) -> bool:
        mpv_candidate = Path(self.mpv_path)
        if mpv_candidate.is_absolute() or any(sep in self.mpv_path for sep in ("/", "\\")):
            return mpv_candidate.exists()
        return shutil.which(self.mpv_path) is not None

    def _install_windows_runtime(self) -> None:
        if self._attempted_runtime_install:
            return
        self._attempted_runtime_install = True
        self.mpv_path = str(ensure_windows_mpv_runtime())

    def needs_windows_runtime_install(self) -> bool:
        return os.name == "nt" and not self._mpv_command_exists() and not windows_mpv_available()

    @staticmethod
    def _readline_from_socket(client: socket.socket) -> bytes:
        buffer = bytearray()
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            buffer.extend(chunk)
            if b"\n" in chunk:
                break
        return bytes(buffer).splitlines()[0] if buffer else b""

    @staticmethod
    def _normalize_token(value: str) -> str:
        return "".join(ch.lower() for ch in value if ch.isalnum())

    @classmethod
    def _expand_preferences(cls, preferences: list[str]) -> list[str]:
        expanded: list[str] = []
        for item in preferences:
            normalized = cls._normalize_token(item)
            if not normalized:
                continue
            expanded.append(normalized)
            for group in cls.AUDIO_ALIAS_GROUPS:
                if normalized in group:
                    expanded.extend(sorted(group))
        seen: set[str] = set()
        result: list[str] = []
        for item in expanded:
            if item not in seen:
                seen.add(item)
                result.append(item)
        return result

    @classmethod
    def track_matches_preference(cls, track: dict[str, Any], preference: str) -> bool:
        haystack = " ".join(
            [
                cls._normalize_token(str(track.get("lang") or "")),
                cls._normalize_token(str(track.get("title") or "")),
                cls._normalize_token(str(track.get("codec") or "")),
            ]
        ).strip()
        return bool(haystack and preference in haystack)

    @classmethod
    def subtitle_is_english(cls, track: dict[str, Any]) -> bool:
        lang = cls._normalize_token(str(track.get("lang") or ""))
        title = cls._normalize_token(str(track.get("title") or ""))
        return any(
            alias in {lang, title}
            or lang.startswith(alias)
            or alias in title
            for alias in cls.ENGLISH_ALIASES
        )

    @classmethod
    def subtitle_has_any_token(cls, track: dict[str, Any], tokens: set[str]) -> bool:
        haystack = " ".join(
            [
                cls._normalize_token(str(track.get("title") or "")),
                cls._normalize_token(str(track.get("lang") or "")),
                cls._normalize_token(str(track.get("codec") or "")),
            ]
        )
        return any(token in haystack for token in tokens)

    @staticmethod
    def describe_subtitle_track(track: dict[str, Any]) -> str:
        lang = str(track.get("lang") or "unknown")
        title = str(track.get("title") or "").strip()
        if title:
            return f"{lang} - {title}"
        return lang


class contextlib_suppress:
    def __init__(self, *exceptions: type[BaseException]) -> None:
        self.exceptions = exceptions

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, _tb) -> bool:
        return exc_type is not None and issubclass(exc_type, self.exceptions)
