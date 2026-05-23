# SPDX-License-Identifier: GPL-3.0-or-later
"""
Custom timeline widget with waveform, playhead, and in/out handles.
"""
import numpy as np
import subprocess
import threading
import logging
from PyQt6.QtWidgets import QWidget, QSizePolicy
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QRect, QPoint
from PyQt6.QtGui import QPainter, QColor, QPen, QBrush, QLinearGradient, QPolygon, QFont
from . import theme as _theme

logger = logging.getLogger(__name__)

HANDLE_W = 10


def extract_waveform(path, num_samples: int = 800) -> np.ndarray:
    """Extract normalised waveform samples from audio using ffmpeg."""
    try:
        cmd = [
            "ffmpeg", "-i", str(path),
            "-ac", "1",
            "-filter:a", f"aresample=8000,asetnsamples={num_samples}",
            "-map", "0:a:0",
            "-f", "f32le",
            "-",
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=30)
        if result.returncode != 0 or not result.stdout:
            return np.zeros(num_samples)
        data = np.frombuffer(result.stdout, dtype=np.float32)
        if len(data) == 0:
            return np.zeros(num_samples)
        # Resample to exactly num_samples
        indices = np.linspace(0, len(data) - 1, num_samples).astype(int)
        samples = np.abs(data[indices])
        mx = samples.max()
        if mx > 0:
            samples /= mx
        return samples
    except Exception as e:
        logger.debug(f"Waveform extraction failed: {e}")
        return np.zeros(num_samples)


class TimelineWidget(QWidget):
    """
    Custom timeline bar showing:
      - waveform background
      - selection region between in/out handles
      - draggable in (green) and out (orange) handles
      - animated playhead
    Emits signals when handles move or user clicks to seek.
    """
    in_point_changed  = pyqtSignal(float)   # 0.0–1.0
    out_point_changed = pyqtSignal(float)
    seek_requested    = pyqtSignal(float)    # position 0.0–1.0

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(72)
        self.setMaximumHeight(72)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.CrossCursor)

        self._waveform: np.ndarray = np.zeros(800)
        self._waveform_ready = False
        self._in  = 0.0    # normalised 0–1
        self._out = 1.0
        self._pos = 0.0    # playhead position 0–1
        self._markers: list = []  # [{"norm": float, "label": str}]

        self._dragging = None   # "in" | "out" | "seek"

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    def set_waveform(self, samples: np.ndarray):
        self._waveform = samples
        self._waveform_ready = True
        self.update()

    def set_position(self, pos: float):
        # Clamp playhead between in and out markers
        self._pos = max(self._in, min(self._out, pos))
        self.update()

    def set_in(self, v: float):
        self._in = max(0.0, min(self._out - 0.01, v))
        self.update()

    def set_out(self, v: float):
        self._out = max(self._in + 0.01, min(1.0, v))

    def set_markers(self, markers: list):
        """Set event markers. Each marker: {norm: float 0-1, label: str}"""
        self._markers = markers
        self.update()
        self.update()

    @property
    def in_point(self) -> float:
        return self._in

    @property
    def out_point(self) -> float:
        return self._out

    # ------------------------------------------------------------------ #
    #  Painting                                                            #
    # ------------------------------------------------------------------ #

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        t = _theme.current
        waveform_bg    = QColor(t.bg_base)
        waveform_color = QColor(t.waveform)
        track_color    = QColor(t.border)
        handle_in      = QColor(t.handle_in)
        handle_out     = QColor(t.handle_out)
        playhead_color = QColor(t.playhead)
        sel_c = QColor(t.accent)
        sel_c.setAlpha(40)

        # Background
        p.fillRect(0, 0, w, h, waveform_bg)

        # Waveform
        if self._waveform_ready and len(self._waveform) > 0:
            bar_w = max(1, w / len(self._waveform))
            mid = h // 2
            dim = QColor(waveform_color)
            dim.setAlpha(50)
            p.setPen(Qt.PenStyle.NoPen)
            for i, amp in enumerate(self._waveform):
                x = int(i * w / len(self._waveform))
                bar_h = max(0, int(amp * (h // 2 - 4)))
                if bar_h == 0:
                    continue
                norm = i / len(self._waveform)
                color = waveform_color if self._in <= norm <= self._out else dim
                p.setBrush(color)
                p.drawRect(x, mid - bar_h, max(1, int(bar_w) - 1), bar_h * 2)
        else:
            # Draw placeholder track line
            p.setPen(QPen(track_color, 2))
            p.drawLine(0, h // 2, w, h // 2)

        # Selection overlay
        in_x  = int(self._in  * w)
        out_x = int(self._out * w)
        p.fillRect(in_x, 0, out_x - in_x, h, sel_c)

        # In handle
        p.fillRect(in_x - HANDLE_W // 2, 0, HANDLE_W, h, handle_in)
        # Arrow marker
        p.setPen(QPen(QColor(t.playhead), 1))
        p.drawLine(in_x, 4, in_x + 6, h // 2)
        p.drawLine(in_x, h - 4, in_x + 6, h // 2)

        # Out handle
        p.fillRect(out_x - HANDLE_W // 2, 0, HANDLE_W, h, handle_out)
        p.drawLine(out_x, 4, out_x - 6, h // 2)
        p.drawLine(out_x, h - 4, out_x - 6, h // 2)

        # Event markers — colored pills with labels
        # Colors match event log rarity tiers. No orange — waveform is orange.
        TRIGGER_COLORS = {
            # Common — grey
            "k":      "#777777",
            "rw":     "#777777",
            "rl":     "#444444",
            "bp":     "#777777",
            "bd":     "#777777",
            "manual": "#777777",
            # Uncommon — blue / red
            "hs":     "#44aaff",
            "fk":     "#44aaff",
            "lh":     "#cc4444",
            # Rare — green / cyan / amber
            "nade":   "#44dd88",
            "he":     "#44dd88",
            "knife":  "#00eeff",
            "fire":   "#ff8844",
            # Epic — yellow / purple
            "mk":     "#ffdd00",
            "clutch": "#cc44ff",
            # Legendary — magenta / white
            "ace":    "#ff44aa",
            "util":   "#ffffff",
        }
        DEFAULT_MARKER_COLOR = "#777777"

        p.setFont(QFont("", 9, QFont.Weight.Bold))
        fm = p.fontMetrics()
        LABEL_H   = 22   # taller pills
        LABEL_PAD = 7
        LEVELS    = 3    # number of stagger levels
        LEVEL_H   = LABEL_H + 4  # vertical step between levels

        # Assign stagger levels to avoid overlap
        # Track placed pills per level as (x_start, x_end) ranges
        placed: list[list[tuple]] = [[] for _ in range(LEVELS)]

        def _assign_level(lx: int, lw: int) -> int:
            for lvl in range(LEVELS):
                overlap = any(
                    not (lx + lw + 2 < px or lx > px + pw + 2)
                    for px, pw in placed[lvl]
                )
                if not overlap:
                    placed[lvl].append((lx, lw))
                    return lvl
            # All levels overlap — use level based on index mod LEVELS
            placed[0].append((lx, lw))
            return 0

        for marker in self._markers:
            mx    = int(marker["norm"] * w)
            label = marker.get("label", "")
            color = QColor(TRIGGER_COLORS.get(
                marker.get("trigger", ""), DEFAULT_MARKER_COLOR))

            # Vertical line full height (subtle)
            pen = QPen(color, 1, Qt.PenStyle.DashLine)
            pen.setDashPattern([3, 4])
            p.setPen(pen)
            p.drawLine(mx, 0, mx, h)

            # Tick mark at bottom
            p.setPen(QPen(color, 2))
            p.drawLine(mx, h - 4, mx, h)

            if label:
                lw = fm.horizontalAdvance(label) + LABEL_PAD * 2
                lh = LABEL_H
                lx = mx - lw // 2
                lx = max(1, min(lx, w - lw - 1))

                # Assign stagger level
                level = _assign_level(lx, lw)
                ly = 2 + level * LEVEL_H

                # Pill background
                p.setPen(Qt.PenStyle.NoPen)
                bg = QColor(color)
                bg.setAlpha(200)
                p.setBrush(bg)
                p.drawRoundedRect(lx, ly, lw, lh, 4, 4)

                # Stem from pill bottom to tick at bottom of timeline
                p.setPen(QPen(color, 1))
                p.drawLine(mx, ly + lh, mx, h - 4)

                # Outline text: draw in black at 8 offsets then white on top
                # Gives clean readable outline on any pill color
                # Choose text color: white on dark pills, black on light pills
                pill_rgb = color.red() * 0.299 + color.green() * 0.587 + color.blue() * 0.114
                text_color = QColor("#000000") if pill_rgb > 160 else QColor("#ffffff")
                outline_color = QColor("#ffffff") if pill_rgb > 160 else QColor("#000000")
                outline_color.setAlpha(180)
                p.setPen(QPen(outline_color, 1))
                for dx, dy in [(-1,-1),(0,-1),(1,-1),(-1,0),(1,0),(-1,1),(0,1),(1,1)]:
                    p.drawText(lx+dx, ly+dy, lw, lh,
                               Qt.AlignmentFlag.AlignCenter, label)
                p.setPen(QPen(text_color, 1))
                p.drawText(lx, ly, lw, lh,
                           Qt.AlignmentFlag.AlignCenter, label)

        # Playhead
        px = int(self._pos * w)
        p.setPen(QPen(playhead_color, 2))
        p.drawLine(px, 0, px, h)
        # Playhead triangle top
        p.setBrush(playhead_color)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawPolygon(*[QPoint(px - 6, 0), QPoint(px + 6, 0), QPoint(px, 10)])

        p.end()

    # ------------------------------------------------------------------ #
    #  Mouse interaction                                                   #
    # ------------------------------------------------------------------ #

    def _norm(self, x: int) -> float:
        return max(0.0, min(1.0, x / self.width()))

    def _hit_handle(self, x: int) -> str | None:
        in_x  = int(self._in  * self.width())
        out_x = int(self._out * self.width())
        if abs(x - in_x)  <= HANDLE_W + 4: return "in"
        if abs(x - out_x) <= HANDLE_W + 4: return "out"
        return None

    def mousePressEvent(self, event):
        x = event.position().x()
        hit = self._hit_handle(int(x))
        if hit:
            self._dragging = hit
        else:
            self._dragging = "seek"
            pos = max(self._in, min(self._out, self._norm(x)))
            self.seek_requested.emit(pos)

    def mouseMoveEvent(self, event):
        x = event.position().x()
        norm = self._norm(x)
        if self._dragging == "in":
            self._in = max(0.0, min(self._out - 0.01, norm))
            self.in_point_changed.emit(self._in)
            self.update()
        elif self._dragging == "out":
            self._out = max(self._in + 0.01, min(1.0, norm))
            self.out_point_changed.emit(self._out)
            self.update()
        elif self._dragging == "seek":
            # Clamp seek to in/out region
            clamped = max(self._in, min(self._out, norm))
            self.seek_requested.emit(clamped)

    def mouseReleaseEvent(self, event):
        self._dragging = None

    def _norm(self, x) -> float:
        return max(0.0, min(1.0, x / max(1, self.width())))
