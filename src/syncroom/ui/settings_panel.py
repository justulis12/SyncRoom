from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from syncroom.ui.widgets import NoWheelComboBox, ToggleSwitch


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
    ytDlpAutoUpdateChanged = Signal(bool)
    updateYtDlpRequested = Signal()
    repairMpvRequested = Signal()
    openMediaToolsFolderRequested = Signal()
    copyMediaToolsReportRequested = Signal()

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
        layout.addWidget(self._build_media_tools_card(settings))
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
        row = self._build_switch_row("Playback notifications", "Show local mpv OSD messages for room actions.")
        self.playback_notifications_switch = row.findChild(ToggleSwitch)
        self.playback_notifications_switch.setChecked(self._settings_bool(settings.get("playback_osd", True)))
        self.playback_notifications_switch.toggled.connect(self.playbackNotificationsChanged)
        layout.addWidget(row, 3, 0, 1, 2)
        return card

    def _build_media_tools_card(self, settings: dict) -> QFrame:
        card, layout = self._build_card(
            "MEDIA TOOLS",
            "mpv and yt-dlp",
            "Inspect and repair local media helpers without changing the installer bundle.",
        )
        self.mpv_status_value = self._build_tool_value("Checking...")
        self.mpv_version_value = self._build_tool_value("Unknown")
        self.mpv_path_value = self._build_tool_value("Unknown")
        self.yt_dlp_status_value = self._build_tool_value("Checking...")
        self.yt_dlp_version_value = self._build_tool_value("Unknown")
        self.yt_dlp_path_value = self._build_tool_value("Unknown")
        self.media_tools_note = QLabel("Media tool actions run locally and keep SyncRoom responsive.")
        self.media_tools_note.setObjectName("inlineNote")
        self.media_tools_note.setWordWrap(True)

        layout.addLayout(self._build_tool_row("mpv status", self.mpv_status_value), 3, 0, 1, 2)
        layout.addLayout(self._build_tool_row("mpv version", self.mpv_version_value), 4, 0, 1, 2)
        layout.addLayout(self._build_tool_row("mpv path", self.mpv_path_value), 5, 0, 1, 2)

        self.repair_mpv_button = QPushButton("Repair mpv")
        self.repair_mpv_button.setObjectName("ghostButton")
        self.repair_mpv_button.clicked.connect(self.repairMpvRequested)
        layout.addWidget(self.repair_mpv_button, 6, 0, 1, 2)

        layout.addLayout(self._build_tool_row("yt-dlp status", self.yt_dlp_status_value), 7, 0, 1, 2)
        layout.addLayout(self._build_tool_row("yt-dlp version", self.yt_dlp_version_value), 8, 0, 1, 2)
        layout.addLayout(self._build_tool_row("yt-dlp path", self.yt_dlp_path_value), 9, 0, 1, 2)

        self.update_yt_dlp_button = QPushButton("Update yt-dlp now")
        self.update_yt_dlp_button.setObjectName("primaryButton")
        self.update_yt_dlp_button.clicked.connect(self.updateYtDlpRequested)
        layout.addWidget(self.update_yt_dlp_button, 10, 0, 1, 2)

        row = self._build_switch_row("Auto-update yt-dlp", "Check at startup at most once every 24 hours.")
        self.yt_dlp_auto_update_switch = row.findChild(ToggleSwitch)
        self.yt_dlp_auto_update_switch.setChecked(self._settings_bool(settings.get("yt_dlp_auto_update", True)))
        self.yt_dlp_auto_update_switch.toggled.connect(self.ytDlpAutoUpdateChanged)
        layout.addWidget(row, 11, 0, 1, 2)

        action_row = QHBoxLayout()
        action_row.setContentsMargins(0, 4, 0, 0)
        action_row.setSpacing(10)
        self.open_media_tools_button = QPushButton("Open media tools folder")
        self.open_media_tools_button.setObjectName("ghostButton")
        self.open_media_tools_button.clicked.connect(self.openMediaToolsFolderRequested)
        self.copy_media_tools_button = QPushButton("Copy media tools report")
        self.copy_media_tools_button.setObjectName("ghostButton")
        self.copy_media_tools_button.clicked.connect(self.copyMediaToolsReportRequested)
        action_row.addWidget(self.open_media_tools_button)
        action_row.addWidget(self.copy_media_tools_button)
        layout.addLayout(action_row, 12, 0, 1, 2)
        layout.addWidget(self.media_tools_note, 13, 0, 1, 2)
        return card

    def _build_switch_row(self, title: str, detail: str) -> QWidget:
        row = QWidget()
        row.setObjectName("switchRow")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(12)
        text_column = QVBoxLayout()
        text_column.setContentsMargins(0, 0, 0, 0)
        text_column.setSpacing(2)
        title_label = QLabel(title)
        title_label.setObjectName("switchTitle")
        detail_label = QLabel(detail)
        detail_label.setObjectName("inlineNote")
        detail_label.setWordWrap(True)
        text_column.addWidget(title_label)
        text_column.addWidget(detail_label)
        switch = ToggleSwitch()
        layout.addLayout(text_column, 1)
        layout.addWidget(switch, 0)
        return row

    def _build_tool_value(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("toolValue")
        label.setWordWrap(True)
        label.setTextInteractionFlags(label.textInteractionFlags() | Qt.TextSelectableByMouse)
        return label

    def _build_tool_row(self, title: str, value_label: QLabel) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        title_label = QLabel(title)
        title_label.setObjectName("toolLabel")
        title_label.setMinimumWidth(92)
        row.addWidget(title_label, 0)
        row.addWidget(value_label, 1)
        return row

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
        return self.playback_notifications_switch.isChecked()

    def yt_dlp_auto_update_enabled(self) -> bool:
        return self.yt_dlp_auto_update_switch.isChecked()

    def set_streaming_quality(self, quality: str) -> None:
        index = self.streaming_quality_combo.findData(quality)
        if index >= 0:
            self.streaming_quality_combo.setCurrentIndex(index)

    def set_media_tools_status(self, payload: dict[str, str]) -> None:
        self.mpv_status_value.setText(payload.get("mpv_status", "Unknown"))
        self.mpv_version_value.setText(payload.get("mpv_version", "Unknown"))
        self.mpv_path_value.setText(payload.get("mpv_path", "Unknown"))
        self.yt_dlp_status_value.setText(payload.get("yt_dlp_status", "Unknown"))
        self.yt_dlp_version_value.setText(payload.get("yt_dlp_version", "Unknown"))
        self.yt_dlp_path_value.setText(payload.get("yt_dlp_path", "Unknown"))
        self.media_tools_note.setText(payload.get("note", "Media tool status refreshed."))

    def set_media_tools_actions_enabled(self, enabled: bool) -> None:
        self.repair_mpv_button.setEnabled(enabled)
        self.update_yt_dlp_button.setEnabled(enabled)
        self.open_media_tools_button.setEnabled(enabled)
        self.copy_media_tools_button.setEnabled(enabled)

    def media_tools_report(self) -> str:
        return "\n".join(
            [
                f"mpv status: {self.mpv_status_value.text()}",
                f"mpv version: {self.mpv_version_value.text()}",
                f"mpv path: {self.mpv_path_value.text()}",
                f"yt-dlp status: {self.yt_dlp_status_value.text()}",
                f"yt-dlp version: {self.yt_dlp_version_value.text()}",
                f"yt-dlp path: {self.yt_dlp_path_value.text()}",
                f"yt-dlp auto-update: {self.yt_dlp_auto_update_enabled()}",
                f"streaming quality: {self.streaming_quality()}",
                f"ytdl-format: {self.ytdl_format()}",
            ]
        )

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
