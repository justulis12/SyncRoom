from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)


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


class UpdateAvailableDialog(QDialog):
    def __init__(self, current_version: str, latest_version: str) -> None:
        super().__init__()
        self.setWindowTitle("SyncRoom Update")
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self.setModal(True)
        self.setFixedSize(460, 300)

        shell = QVBoxLayout(self)
        shell.setContentsMargins(18, 18, 18, 18)
        shell.setSpacing(0)

        card = QFrame()
        card.setObjectName("updateCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(24, 24, 24, 22)
        card_layout.setSpacing(14)

        eyebrow = QLabel("UPDATE AVAILABLE")
        eyebrow.setObjectName("updateEyebrow")
        title = QLabel("A new SyncRoom is ready")
        title.setObjectName("updateTitle")
        title.setWordWrap(True)
        body = QLabel("SyncRoom will close, update, and reopen automatically.")
        body.setObjectName("updateBody")
        body.setWordWrap(True)

        version_text = QLabel(f"Current {current_version}  |  Latest {latest_version}")
        version_text.setObjectName("updateVersion")

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        buttons.addStretch(1)
        later_button = QPushButton("Later")
        later_button.setObjectName("secondaryButton")
        update_button = QPushButton("Update now")
        update_button.setObjectName("primaryButton")
        update_button.setDefault(True)
        buttons.addWidget(later_button)
        buttons.addWidget(update_button)

        card_layout.addWidget(eyebrow)
        card_layout.addWidget(title)
        card_layout.addWidget(body)
        card_layout.addWidget(version_text)
        card_layout.addStretch(1)
        card_layout.addLayout(buttons)
        shell.addWidget(card)

        later_button.clicked.connect(self.reject)
        update_button.clicked.connect(self.accept)

        self.setStyleSheet(
            """
            QDialog {
                background: #000000;
                color: #f6f6f7;
                font-family: "Noto Sans", "Cantarell", sans-serif;
            }
            QFrame#updateCard {
                background: #080808;
                border: 1px solid #303034;
                border-radius: 14px;
            }
            QLabel#updateEyebrow {
                color: #a6a6ad;
                font-size: 11px;
                font-weight: 800;
                letter-spacing: 0.12em;
            }
            QLabel#updateTitle {
                color: #ffffff;
                font-size: 24px;
                font-weight: 800;
            }
            QLabel#updateBody {
                color: #c8c8ce;
                font-size: 14px;
                line-height: 1.3;
            }
            QLabel#updateVersion {
                color: #8f8f98;
                font-size: 12px;
            }
            QPushButton {
                min-width: 96px;
                min-height: 34px;
                border-radius: 8px;
                padding: 0 16px;
                font-size: 13px;
                font-weight: 700;
            }
            QPushButton#primaryButton {
                color: #050505;
                background: #f2f2f4;
                border: 1px solid #ffffff;
            }
            QPushButton#primaryButton:hover {
                background: #ffffff;
            }
            QPushButton#secondaryButton {
                color: #eeeeef;
                background: #151516;
                border: 1px solid #36363a;
            }
            QPushButton#secondaryButton:hover {
                background: #202023;
            }
            """
        )
