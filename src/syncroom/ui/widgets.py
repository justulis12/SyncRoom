from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QBrush,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPaintEvent,
    QPen,
)
from PySide6.QtWidgets import QCheckBox, QComboBox, QSizePolicy, QWidget


class NoWheelComboBox(QComboBox):
    def wheelEvent(self, event) -> None:  # type: ignore[override]
        if self.view().isVisible():
            super().wheelEvent(event)
            return
        event.ignore()


class ToggleSwitch(QCheckBox):
    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("toggleSwitch")
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def sizeHint(self) -> QSize:  # type: ignore[override]
        return QSize(54, 30)

    def minimumSizeHint(self) -> QSize:  # type: ignore[override]
        return self.sizeHint()

    def hitButton(self, pos) -> bool:  # type: ignore[override]
        return self.rect().contains(pos)

    def paintEvent(self, _event: QPaintEvent) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        rect = QRectF(self.rect()).adjusted(2, 3, -2, -3)
        radius = rect.height() / 2
        checked = self.isChecked()
        enabled = self.isEnabled()

        if checked:
            track_gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
            track_gradient.setColorAt(0.0, QColor(246, 246, 248, 230 if enabled else 120))
            track_gradient.setColorAt(1.0, QColor(172, 172, 178, 220 if enabled else 110))
            border = QColor(255, 255, 255, 150 if enabled else 70)
        else:
            track_gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
            track_gradient.setColorAt(0.0, QColor(18, 18, 19, 245 if enabled else 130))
            track_gradient.setColorAt(1.0, QColor(7, 7, 8, 245 if enabled else 120))
            border = QColor(100, 100, 106, 105 if enabled else 55)

        painter.setBrush(QBrush(track_gradient))
        painter.setPen(QPen(border, 1.1))
        painter.drawRoundedRect(rect, radius, radius)

        knob_diameter = rect.height() - 8
        knob_x = rect.right() - knob_diameter - 4 if checked else rect.left() + 4
        knob_rect = QRectF(knob_x, rect.top() + 4, knob_diameter, knob_diameter)
        knob_gradient = QLinearGradient(knob_rect.topLeft(), knob_rect.bottomRight())
        if checked:
            knob_gradient.setColorAt(0.0, QColor("#ffffff"))
            knob_gradient.setColorAt(1.0, QColor("#d4d7dc"))
            knob_border = QColor(255, 255, 255, 150 if enabled else 70)
        else:
            knob_gradient.setColorAt(0.0, QColor("#2a2a2d"))
            knob_gradient.setColorAt(1.0, QColor("#111112"))
            knob_border = QColor(125, 125, 132, 90 if enabled else 50)

        painter.setBrush(QBrush(knob_gradient))
        painter.setPen(QPen(knob_border, 1))
        painter.drawEllipse(knob_rect)

        if self.hasFocus():
            focus_rect = rect.adjusted(-2, -2, 2, 2)
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(255, 255, 255, 90), 1))
            painter.drawRoundedRect(focus_rect, focus_rect.height() / 2, focus_rect.height() / 2)


class GradientWordmarkLabel(QWidget):
    def __init__(self, text: str = "SyncRoom", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._sync_text = text[:4] if text == "SyncRoom" else text
        self._room_text = text[4:] if text == "SyncRoom" else ""
        self._pixel_size = 24
        self.setObjectName("brandWordmark")
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def set_pixel_size(self, value: int) -> None:
        self._pixel_size = max(14, int(value))
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:  # type: ignore[override]
        font = self._wordmark_font()
        metrics = QFontMetrics(font)
        return QSize(self._wordmark_width(metrics) + 24, metrics.height() + 10)

    def minimumSizeHint(self) -> QSize:  # type: ignore[override]
        font = self._wordmark_font()
        metrics = QFontMetrics(font)
        return QSize(self._wordmark_width(metrics) + 12, metrics.height() + 6)

    def paintEvent(self, _event: QPaintEvent) -> None:  # type: ignore[override]
        rect = QRectF(self.rect()).adjusted(8, 2, -8, -2)
        if rect.isEmpty():
            return

        painter = QPainter(self)
        painter.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        font = self._wordmark_font()
        painter.setFont(font)

        metrics = QFontMetrics(font)
        sync_width = metrics.horizontalAdvance(self._sync_text)
        total_width = self._wordmark_width(metrics)
        baseline_y = rect.center().y() + (metrics.ascent() - metrics.descent()) / 2
        sync_pos = QPointF(rect.right() - total_width, baseline_y)
        room_pos = QPointF(sync_pos.x() + sync_width - 0.5, baseline_y)

        self._draw_wordmark_text(
            painter,
            sync_pos + QPointF(0, 1.4),
            room_pos + QPointF(0, 1.4),
            QColor(0, 0, 0, 150),
        )
        self._draw_wordmark_text(
            painter,
            sync_pos + QPointF(0, 0.8),
            room_pos + QPointF(0, 0.8),
            QColor(255, 255, 255, 24),
        )

        sync_gradient = QLinearGradient(sync_pos, sync_pos + QPointF(0, -metrics.height()))
        sync_gradient.setColorAt(0.0, QColor("#ffffff"))
        sync_gradient.setColorAt(1.0, QColor("#f5f7fa"))
        painter.setPen(QPen(QBrush(sync_gradient), 1))
        painter.drawText(sync_pos, self._sync_text)

        room_gradient = QLinearGradient(room_pos, room_pos + QPointF(metrics.horizontalAdvance(self._room_text), 0))
        room_gradient.setColorAt(0.0, QColor("#f4f6f8"))
        room_gradient.setColorAt(0.34, QColor("#b9c0ca"))
        room_gradient.setColorAt(0.66, QColor("#7e8794"))
        room_gradient.setColorAt(1.0, QColor("#d7dce3"))
        painter.setPen(QPen(QBrush(room_gradient), 1))
        painter.drawText(room_pos, self._room_text)

    def _wordmark_font(self) -> QFont:
        font = QFont(self.font())
        font.setPixelSize(self._pixel_size)
        font.setWeight(QFont.Weight.Black)
        spacing_type = getattr(QFont, "PercentageSpacing", None)
        if spacing_type is None:
            spacing_type = QFont.LetterSpacingType.PercentageSpacing
        font.setLetterSpacing(spacing_type, 103)
        return font

    def _wordmark_width(self, metrics: QFontMetrics) -> int:
        return metrics.horizontalAdvance(self._sync_text) + metrics.horizontalAdvance(self._room_text)

    def _draw_wordmark_text(
        self,
        painter: QPainter,
        sync_pos: QPointF,
        room_pos: QPointF,
        color: QColor,
    ) -> None:
        painter.setPen(color)
        painter.drawText(sync_pos, self._sync_text)
        painter.drawText(room_pos, self._room_text)
