from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGridLayout,
    QLabel,
    QLineEdit,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from syncroom.ui.widgets import NoWheelComboBox


AUDIO_PRESETS: dict[str, str] = {
    "english": "eng,en,english",
    "japanese": "jp,jpn,ja,jap,japanese",
    "spanish": "spa,es,spanish",
}

SUBTITLE_PRESETS: dict[str, str] = {
    "english": "eng,en,english",
    "japanese": "jp,jpn,ja,jap,japanese",
    "spanish": "spa,es,spanish",
    "english_signs": "eng,en,english",
}

YTDL_FORMATS: dict[str, str] = {
    "360p": "bv*[height<=360]+ba/b[height<=360]/b",
    "480p": "bv*[height<=480]+ba/b[height<=480]/b",
    "720p": "bv*[height<=720]+ba/b[height<=720]/b",
    "1080p": "bv*[height<=1080]+ba/b[height<=1080]/b",
    "4k": "bv*[height<=2160]+ba/b[height<=2160]/b",
}


class SettingsPanel(QWidget):
    audioPreferenceChanged = Signal()
    subtitlePreferenceChanged = Signal()
    streamingQualityChanged = Signal(str)
    playbackNotificationsChanged = Signal(bool)

    def __init__(self, settings: dict) -> None:
        super().__init__()
        self.setObjectName("settingsPage")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(16)

        header = QLabel("Settings")
        header.setObjectName("roomTitle")
        intro = QLabel("Local preferences for tracks, online streaming, and playback notifications.")
        intro.setObjectName("inlineNote")
        intro.setWordWrap(True)
        layout.addWidget(header)
        layout.addWidget(intro)
        layout.addWidget(self._build_audio_card(settings))
        layout.addWidget(self._build_subtitle_card(settings))
        layout.addWidget(self._build_streaming_card(settings))
        layout.addWidget(self._build_notifications_card(settings))
        layout.addStretch(1)

    def _build_card(self, overline: str, title: str, description: str) -> tuple[QFrame, QGridLayout]:
        card = QFrame()
        card.setObjectName("settingsCard")
        card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        layout = QGridLayout(card)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setHorizontalSpacing(14)
        layout.setVerticalSpacing(8)

        overline_label = QLabel(overline)
        overline_label.setObjectName("sectionOverline")
        title_label = QLabel(title)
        title_label.setObjectName("sectionTitle")
        description_label = QLabel(description)
        description_label.setObjectName("inlineNote")
        description_label.setWordWrap(True)

        layout.addWidget(overline_label, 0, 0, 1, 2)
        layout.addWidget(title_label, 1, 0, 1, 2)
        layout.addWidget(description_label, 2, 0, 1, 2)
        layout.setColumnStretch(0, 1)
        layout.setColumnStretch(1, 1)
        return card, layout

    def _build_audio_card(self, settings: dict) -> QFrame:
        card, layout = self._build_card(
            "AUDIO",
            "Preferred audio language",
            "Choose the audio track SyncRoom should pick when media exposes multiple tracks.",
        )
        self.audio_mode_combo = NoWheelComboBox()
        self.audio_mode_combo.setObjectName("elevatedCombo")
        self.audio_mode_combo.addItem("English", "english")
        self.audio_mode_combo.addItem("Japanese", "japanese")
        self.audio_mode_combo.addItem("Spanish", "spanish")
        self.audio_mode_combo.addItem("Custom", "custom")
        saved_mode = str(settings.get("audio_mode", "") or "")
        if not saved_mode:
            saved_mode = self._audio_mode_from_legacy(str(settings.get("audio_preferences", "") or ""))
        audio_index = self.audio_mode_combo.findData(saved_mode or "english")
        self.audio_mode_combo.setCurrentIndex(max(0, audio_index))

        self.audio_custom_input = QLineEdit(
            str(settings.get("audio_custom_preferences", "") or settings.get("audio_preferences", "") or AUDIO_PRESETS["english"])
        )
        self.audio_custom_input.setObjectName("settingsInput")
        self.audio_custom_input.setPlaceholderText("english,eng,en")

        layout.addWidget(self.audio_mode_combo, 3, 0, 1, 1)
        layout.addWidget(self.audio_custom_input, 3, 1, 1, 1)
        self.audio_mode_combo.currentIndexChanged.connect(self._on_audio_changed)
        self.audio_custom_input.editingFinished.connect(self.audioPreferenceChanged)
        self._update_audio_custom_visibility()
        return card

    def _build_subtitle_card(self, settings: dict) -> QFrame:
        card, layout = self._build_card(
            "SUBTITLES",
            "Preferred subtitles",
            "Subtitles are local only and never broadcast to the room.",
        )
        self.subtitle_mode_combo = NoWheelComboBox()
        self.subtitle_mode_combo.setObjectName("elevatedCombo")
        self.subtitle_mode_combo.addItem("Off", "off")
        self.subtitle_mode_combo.addItem("English", "english")
        self.subtitle_mode_combo.addItem("Japanese", "japanese")
        self.subtitle_mode_combo.addItem("Spanish", "spanish")
        self.subtitle_mode_combo.addItem("English Signs & Songs", "english_signs")
        self.subtitle_mode_combo.addItem("Custom", "custom")
        saved_mode = str(settings.get("subtitle_mode", "off") or "off")
        subtitle_index = self.subtitle_mode_combo.findData(saved_mode)
        self.subtitle_mode_combo.setCurrentIndex(max(0, subtitle_index))

        self.subtitle_custom_input = QLineEdit(
            str(settings.get("subtitle_custom_preferences", "eng,english") or "eng,english")
        )
        self.subtitle_custom_input.setObjectName("settingsInput")
        self.subtitle_custom_input.setPlaceholderText("english,eng,en")

        layout.addWidget(self.subtitle_mode_combo, 3, 0, 1, 1)
        layout.addWidget(self.subtitle_custom_input, 3, 1, 1, 1)
        self.subtitle_mode_combo.currentIndexChanged.connect(self._on_subtitle_changed)
        self.subtitle_custom_input.editingFinished.connect(self.subtitlePreferenceChanged)
        self._update_subtitle_custom_visibility()
        return card

    def _build_streaming_card(self, settings: dict) -> QFrame:
        card, layout = self._build_card(
            "STREAMING",
            "Streaming quality",
            "Used by yt-dlp for newly loaded online media. Direct video links are unaffected.",
        )
        self.streaming_quality_combo = NoWheelComboBox()
        self.streaming_quality_combo.setObjectName("elevatedCombo")
        self.streaming_quality_combo.addItem("360p", "360p")
        self.streaming_quality_combo.addItem("480p", "480p")
        self.streaming_quality_combo.addItem("720p", "720p")
        self.streaming_quality_combo.addItem("1080p", "1080p")
        self.streaming_quality_combo.addItem("4K when available", "4k")
        saved_quality = str(settings.get("streaming_quality", "1080p") or "1080p").lower()
        quality_index = self.streaming_quality_combo.findData(saved_quality)
        self.streaming_quality_combo.setCurrentIndex(max(0, quality_index))

        self.streaming_format_label = QLabel(self.ytdl_format())
        self.streaming_format_label.setObjectName("inlineNote")
        self.streaming_format_label.setWordWrap(True)
        layout.addWidget(self.streaming_quality_combo, 3, 0, 1, 1)
        layout.addWidget(self.streaming_format_label, 3, 1, 1, 1)
        self.streaming_quality_combo.currentIndexChanged.connect(self._on_streaming_quality_changed)
        return card

    def _build_notifications_card(self, settings: dict) -> QFrame:
        card, layout = self._build_card(
            "NOTIFICATIONS",
            "Playback notifications",
            "Show mpv OSD messages for local and remote play, pause, seek, and media load actions.",
        )
        self.playback_notifications_check = QCheckBox("Show playback OSD notifications")
        self.playback_notifications_check.setObjectName("settingsCheck")
        self.playback_notifications_check.setChecked(self._settings_bool(settings.get("playback_osd", True)))
        self.playback_notifications_check.toggled.connect(self.playbackNotificationsChanged)
        layout.addWidget(self.playback_notifications_check, 3, 0, 1, 2)
        return card

    def _on_audio_changed(self) -> None:
        self._update_audio_custom_visibility()
        self.audioPreferenceChanged.emit()

    def _on_subtitle_changed(self) -> None:
        self._update_subtitle_custom_visibility()
        self.subtitlePreferenceChanged.emit()

    def _on_streaming_quality_changed(self) -> None:
        self.streaming_format_label.setText(self.ytdl_format())
        self.streamingQualityChanged.emit(self.streaming_quality())

    def _update_audio_custom_visibility(self) -> None:
        custom = self.audio_mode() == "custom"
        self.audio_custom_input.setVisible(custom)
        self.audio_custom_input.setEnabled(custom)

    def _update_subtitle_custom_visibility(self) -> None:
        custom = self.subtitle_mode() == "custom"
        self.subtitle_custom_input.setVisible(custom)
        self.subtitle_custom_input.setEnabled(custom)

    def audio_mode(self) -> str:
        return str(self.audio_mode_combo.currentData() or "english")

    def audio_custom_preferences(self) -> str:
        return self.audio_custom_input.text().strip()

    def audio_preferences(self) -> list[str]:
        mode = self.audio_mode()
        raw = self.audio_custom_preferences() if mode == "custom" else AUDIO_PRESETS.get(mode, AUDIO_PRESETS["english"])
        return [item.strip() for item in raw.split(",") if item.strip()]

    def audio_preferences_text(self) -> str:
        return ",".join(self.audio_preferences())

    def subtitle_mode(self) -> str:
        return str(self.subtitle_mode_combo.currentData() or "off")

    def subtitle_custom_preferences(self) -> str:
        return self.subtitle_custom_input.text().strip()

    def subtitle_preferences(self) -> list[str]:
        mode = self.subtitle_mode()
        raw = self.subtitle_custom_preferences() if mode == "custom" else SUBTITLE_PRESETS.get(mode, "")
        return [item.strip() for item in raw.split(",") if item.strip()]

    def streaming_quality(self) -> str:
        return str(self.streaming_quality_combo.currentData() or "1080p")

    def ytdl_format(self) -> str:
        return YTDL_FORMATS.get(self.streaming_quality(), YTDL_FORMATS["1080p"])

    def playback_notifications_enabled(self) -> bool:
        return self.playback_notifications_check.isChecked()

    @staticmethod
    def _audio_mode_from_legacy(value: str) -> str:
        normalized = {item.strip().lower() for item in value.split(",") if item.strip()}
        if normalized and normalized <= set(AUDIO_PRESETS["japanese"].split(",")):
            return "japanese"
        if normalized and normalized <= set(AUDIO_PRESETS["spanish"].split(",")):
            return "spanish"
        if normalized and normalized <= set(AUDIO_PRESETS["english"].split(",")):
            return "english"
        return "custom" if normalized else "english"

    @staticmethod
    def _settings_bool(value: object) -> bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() not in {"0", "false", "no", "off", ""}
