# SPDX-License-Identifier: GPL-3.0-or-later
"""
Shared small widgets.
"""
from PyQt6.QtWidgets import QSpinBox, QComboBox, QDoubleSpinBox, QWidget
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QPainter, QPen, QColor, QFont
from . import theme as _theme


class NoScrollSpinBox(QSpinBox):
    def wheelEvent(self, event):
        event.ignore()


class NoScrollDoubleSpinBox(QDoubleSpinBox):
    def wheelEvent(self, event):
        event.ignore()


class NoScrollComboBox(QComboBox):
    def wheelEvent(self, event):
        event.ignore()


class VolumeDial(QWidget):
    """
    Compact radial dial. Arc shows 0–100 %, percentage drawn in centre.
    Adjust with scroll wheel (1 % / notch) or vertical drag (up = louder).
    """
    value_changed = pyqtSignal(float)   # 0.0 – 1.0

    def __init__(self, color: str, initial: float = 1.0, parent=None):
        super().__init__(parent)
        self._value           = max(0.0, min(1.0, initial))
        self._color           = color
        self._drag_start_y    = 0.0
        self._drag_start_val  = 1.0
        self.setCursor(Qt.CursorShape.SizeVerCursor)
        self.setToolTip("Scroll or drag to adjust volume")

    # ── public ────────────────────────────────────────────────────────────

    def set_value(self, v: float):
        self._value = max(0.0, min(1.0, v))
        self.update()

    # ── events ────────────────────────────────────────────────────────────

    def wheelEvent(self, event):
        self._nudge(event.angleDelta().y() / 12000.0)   # 120 units/notch → 1 %
        event.accept()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start_y   = event.position().y()
            self._drag_start_val = self._value

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton:
            dy = self._drag_start_y - event.position().y()
            self._set(self._drag_start_val + dy / 150.0)

    def _nudge(self, delta: float):
        self._set(self._value + delta)

    def _set(self, v: float):
        clamped = max(0.0, min(1.0, v))
        if clamped != self._value:
            self._value = clamped
            self.update()
            self.value_changed.emit(self._value)

    # ── paint ─────────────────────────────────────────────────────────────

    def paintEvent(self, event):
        t   = _theme.current
        p   = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        pad  = max(2, min(w, h) // 8)
        r    = min(w, h) // 2 - pad
        cx, cy = w // 2, h // 2
        from PyQt6.QtCore import QRect
        rect = QRect(cx - r, cy - r, r * 2, r * 2)

        # 270° arc: start at SW (225° CCW from E), sweep CW
        START = 225 * 16
        FULL  = -270 * 16
        lw    = max(2, r // 4)

        # Track arc (background)
        pen = QPen(QColor(t.border), lw)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(rect, START, FULL)

        # Value arc
        span = int(FULL * self._value)
        if span:
            pen = QPen(QColor(self._color), lw)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.drawArc(rect, START, span)

        # Percentage label
        font = QFont("", max(6, r // 2), QFont.Weight.Bold)
        p.setFont(font)
        p.setPen(QPen(QColor(t.text_dim)))
        p.drawText(0, 0, w, h, Qt.AlignmentFlag.AlignCenter,
                   f"{int(round(self._value * 100))}%")
        p.end()
