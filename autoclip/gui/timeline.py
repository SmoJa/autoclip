# SPDX-License-Identifier: GPL-3.0-or-later
"""
Custom timeline widget with stacked per-track waveforms, playhead, and in/out handles.
Pill zone (top) shows event markers. Waveform zone (bottom) has track waveforms and
per-track VolumeDial controls pinned to the left edge.
"""
import numpy as np
import logging
from PyQt6.QtWidgets import QWidget, QSizePolicy
from PyQt6.QtCore import Qt, pyqtSignal, QRect, QPoint
from PyQt6.QtGui import QPainter, QColor, QPen, QFont
from . import theme as _theme

logger = logging.getLogger(__name__)

HANDLE_W    = 10
PILL_ZONE_H = 74    # height of the pill / marker zone at the top
DIAL_W      = 78    # width reserved at the left of the waveform zone for volume dials


class TimelineWidget(QWidget):
    """
    Layout:
      ┌─────────────────────────────────────────┐  ← y=0
      │           pill / marker zone            │  PILL_ZONE_H px
      ├────┬────────────────────────────────────┤  ← y=PILL_ZONE_H
      │dial│  waveform strip 0                  │
      │dial│  waveform strip 1                  │  (h - PILL_ZONE_H) px
      │    │  …                                 │
      └────┴────────────────────────────────────┘  ← y=h

    Horizontal positions use an *effective* width  ew = w - DIAL_W  so that
    seeks, handles, playhead, and marker lines all align with the waveform.
    """
    in_point_changed     = pyqtSignal(float)
    out_point_changed    = pyqtSignal(float)
    seek_requested       = pyqtSignal(float)
    track_volume_changed = pyqtSignal(int, float)
    track_mute_changed   = pyqtSignal(int, bool)

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(PILL_ZONE_H + 138)   # 74 pill + 138 waveform = 212
        self.setMaximumHeight(PILL_ZONE_H + 138)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.CrossCursor)

        self._waveforms:       list = []
        self._waveform_colors: list = []
        self._track_volumes:   list = []
        self._in  = 0.0
        self._out = 1.0
        self._pos = 0.0
        self._markers:    list = []
        self._track_dials: list = []

        self._dragging    = None
        self._hovered_idx = -1
        self._pill_rects: list = []
        self.setMouseTracking(True)

    # ── public: waveform data ─────────────────────────────────────────────

    def set_track_waveform(self, idx: int, samples: np.ndarray, color: str = None):
        t = _theme.current
        while len(self._waveforms) <= idx:
            self._waveforms.append(np.zeros(800))
            self._waveform_colors.append(t.waveform)
        self._waveforms[idx] = samples
        if color:
            self._waveform_colors[idx] = color
        self.update()

    def clear_waveforms(self):
        self._waveforms = []
        self._waveform_colors = []
        self.update()

    # ── public: track controls ────────────────────────────────────────────

    def setup_track_controls(self, n: int, volumes: list, mutes: list, colors: list):
        from .widgets import VolumeDial
        self.clear_track_controls()
        self._track_volumes = [volumes[i] if i < len(volumes) else 1.0 for i in range(n)]
        if n == 0:
            return
        wh       = self.height() - PILL_ZONE_H
        strip_h  = max(1, wh // n)
        dial_dim = max(16, min(strip_h - 4, DIAL_W - 4))

        for i in range(n):
            color = colors[i] if i < len(colors) else "#888888"
            vol   = self._track_volumes[i]
            dial  = VolumeDial(color, vol, self)
            x = (DIAL_W - dial_dim) // 2
            y = PILL_ZONE_H + i * strip_h + (strip_h - dial_dim) // 2
            dial.setFixedSize(dial_dim, dial_dim)
            dial.move(x, y)
            dial.show()
            dial.value_changed.connect(lambda v, idx=i: self._on_dial_volume(idx, v))
            self._track_dials.append(dial)

    def _on_dial_volume(self, idx: int, v: float):
        while len(self._track_volumes) <= idx:
            self._track_volumes.append(1.0)
        self._track_volumes[idx] = v
        self.update()
        self.track_volume_changed.emit(idx, v)

    def clear_track_controls(self):
        for d in self._track_dials:
            d.deleteLater()
        self._track_dials.clear()

    # ── public: playhead / handles / markers ──────────────────────────────

    def set_position(self, pos: float):
        self._pos = max(self._in, min(self._out, pos))
        self.update()

    def set_in(self, v: float):
        self._in = max(0.0, min(self._out - 0.01, v))
        self.update()

    def set_out(self, v: float):
        self._out = max(self._in + 0.01, min(1.0, v))

    def set_markers(self, markers: list):
        self._markers = markers
        self._hovered_idx = -1
        self.update()
        self.update()

    @property
    def in_point(self) -> float:  return self._in
    @property
    def out_point(self) -> float: return self._out

    # ── painting ──────────────────────────────────────────────────────────

    def paintEvent(self, event):
        LABEL_H  = 20
        LABEL_PAD = 7
        LEVELS   = 3
        LEVEL_H  = LABEL_H + 4

        TRIGGER_COLORS = {
            "k":      "#777777", "rw": "#777777", "rl": "#444444",
            "bp":     "#777777", "bd": "#777777", "manual": "#777777",
            "hs":     "#44aaff", "fk": "#44aaff", "lh": "#cc4444",
            "nade":   "#44dd88", "he": "#44dd88", "knife": "#00eeff",
            "fire":   "#ff8844", "mk": "#ffdd00", "clutch": "#cc44ff",
            "ace":    "#ff44aa", "util": "#ffffff",
        }
        DEFAULT_MARKER_COLOR = "#777777"

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        wz = PILL_ZONE_H          # waveform zone top
        wh = h - wz               # waveform zone height
        ew = w - DIAL_W           # effective width for normalised positions

        t  = _theme.current
        handle_in      = QColor(t.handle_in)
        handle_out     = QColor(t.handle_out)
        playhead_color = QColor(t.playhead)

        # ── Background ────────────────────────────────────────────────────
        p.fillRect(0, 0, w, h, QColor(t.bg_base))

        pz_bg = QColor(t.bg_deep); pz_bg.setAlpha(120)
        p.fillRect(0, 0, w, wz, pz_bg)

        # Pill zone / waveform zone separator
        p.setPen(QPen(QColor(t.border), 1))
        p.drawLine(0, wz, w, wz)

        # Dial zone / waveform area separator (vertical)
        p.drawLine(DIAL_W, wz, DIAL_W, h)

        # ── Waveform strips ───────────────────────────────────────────────
        if self._waveforms:
            n_tracks = len(self._waveforms)
            strip_h  = max(1, wh // n_tracks)
            p.setPen(Qt.PenStyle.NoPen)
            for ti, (samples, clr_str) in enumerate(
                    zip(self._waveforms, self._waveform_colors)):
                strip_top = wz + ti * strip_h
                strip_mid = strip_top + strip_h // 2
                wcolor = QColor(clr_str)
                wdim   = QColor(clr_str); wdim.setAlpha(50)
                if len(samples) > 0:
                    vol   = self._track_volumes[ti] if ti < len(self._track_volumes) else 1.0
                    bar_w = max(1, ew / len(samples))
                    for i, amp in enumerate(samples):
                        x     = DIAL_W + int(i * ew / len(samples))
                        bar_h = max(0, int(amp * vol * (strip_h // 2 - 2)))
                        if bar_h == 0:
                            continue
                        norm  = i / len(samples)
                        p.setBrush(wcolor if self._in <= norm <= self._out else wdim)
                        p.drawRect(x, strip_mid - bar_h,
                                   max(1, int(bar_w) - 1), bar_h * 2)
                if ti < n_tracks - 1:
                    div_y = strip_top + strip_h - 1
                    p.setPen(QPen(QColor(t.border), 1))
                    p.drawLine(DIAL_W, div_y, w, div_y)
                    p.setPen(Qt.PenStyle.NoPen)
        else:
            p.setPen(QPen(QColor(t.border), 2))
            p.drawLine(DIAL_W, wz + wh // 2, w, wz + wh // 2)

        # ── Selection overlay + handles (waveform zone, right of dials) ───
        in_x  = DIAL_W + int(self._in  * ew)
        out_x = DIAL_W + int(self._out * ew)
        sel_c = QColor(t.accent); sel_c.setAlpha(40)
        p.fillRect(in_x, wz, out_x - in_x, wh, sel_c)

        mid_y = wz + wh // 2
        p.fillRect(in_x - HANDLE_W // 2, wz, HANDLE_W, wh, handle_in)
        p.setPen(QPen(QColor(t.playhead), 1))
        p.drawLine(in_x, wz + 4, in_x + 6, mid_y)
        p.drawLine(in_x, h - 4,  in_x + 6, mid_y)

        p.fillRect(out_x - HANDLE_W // 2, wz, HANDLE_W, wh, handle_out)
        p.drawLine(out_x, wz + 4, out_x - 6, mid_y)
        p.drawLine(out_x, h - 4,  out_x - 6, mid_y)

        # ── Event marker pills ────────────────────────────────────────────
        p.setFont(QFont("", 9, QFont.Weight.Bold))
        fm = p.fontMetrics()

        placed: list[list[tuple]] = [[] for _ in range(LEVELS)]

        def _assign_level(lx: int, lw: int) -> int:
            for lvl in range(LEVELS):
                if not any(
                    not (lx + lw + 2 < px or lx > px + pw + 2)
                    for px, pw in placed[lvl]
                ):
                    placed[lvl].append((lx, lw))
                    return lvl
            placed[0].append((lx, lw))
            return 0

        def _draw_pill(mx, lx, ly, lw, lh, label, color, alpha=200):
            pill_rgb  = color.red() * 0.299 + color.green() * 0.587 + color.blue() * 0.114
            text_c    = QColor("#000000") if pill_rgb > 160 else QColor("#ffffff")
            outline_c = QColor("#ffffff") if pill_rgb > 160 else QColor("#000000")
            outline_c.setAlpha(180)
            bg = QColor(color); bg.setAlpha(alpha)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(lx, ly, lw, lh, 4, 4)
            p.setPen(QPen(color, 1))
            p.drawLine(mx, ly + lh, mx, wz)   # stem to separator line
            p.setPen(QPen(outline_c, 1))
            for dx, dy in [(-1,-1),(0,-1),(1,-1),(-1,0),(1,0),(-1,1),(0,1),(1,1)]:
                p.drawText(lx+dx, ly+dy, lw, lh, Qt.AlignmentFlag.AlignCenter, label)
            p.setPen(QPen(text_c, 1))
            p.drawText(lx, ly, lw, lh, Qt.AlignmentFlag.AlignCenter, label)

        new_pill_rects = []
        _pill_geom     = []

        for i, marker in enumerate(self._markers):
            mx    = DIAL_W + int(marker["norm"] * ew)
            label = marker.get("label", "")
            color = QColor(TRIGGER_COLORS.get(marker.get("trigger", ""), DEFAULT_MARKER_COLOR))

            pen = QPen(color, 1, Qt.PenStyle.DashLine)
            pen.setDashPattern([3, 4])
            p.setPen(pen)
            p.drawLine(mx, wz, mx, h - 4)

            p.setPen(QPen(color, 2))
            p.drawLine(mx, h - 4, mx, h)

            if label:
                lw_  = fm.horizontalAdvance(label) + LABEL_PAD * 2
                lh   = LABEL_H
                lx   = max(1, min(mx - lw_ // 2, w - lw_ - 1))
                level = _assign_level(lx, lw_)
                ly   = 2 + level * LEVEL_H
                _pill_geom.append((i, mx, lx, ly, lw_, lh, color))
                if i != self._hovered_idx:
                    new_pill_rects.append((i, QRect(lx, ly, lw_, lh)))
                    _draw_pill(mx, lx, ly, lw_, lh, label, color)

        # Hovered pill — draw on top with full_label
        if 0 <= self._hovered_idx < len(self._markers):
            marker     = self._markers[self._hovered_idx]
            full_label = marker.get("full_label", marker.get("label", ""))
            geom = next((g for g in _pill_geom if g[0] == self._hovered_idx), None)
            if geom and full_label:
                _, mx, _, ly, _, lh, color = geom
                lw_ = fm.horizontalAdvance(full_label) + LABEL_PAD * 2
                lx  = max(1, min(mx - lw_ // 2, w - lw_ - 1))
                new_pill_rects.append((self._hovered_idx, QRect(lx, ly, lw_, lh)))
                _draw_pill(mx, lx, ly, lw_, lh, full_label, color, alpha=230)

        self._pill_rects = new_pill_rects

        # ── Playhead (waveform zone only) ─────────────────────────────────
        px = DIAL_W + int(self._pos * ew)
        p.setPen(QPen(playhead_color, 2))
        p.drawLine(px, wz, px, h)
        p.setBrush(playhead_color)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawPolygon(*[QPoint(px - 6, wz), QPoint(px + 6, wz), QPoint(px, wz + 10)])

        p.end()

    # ── mouse interaction ─────────────────────────────────────────────────

    def _norm(self, x) -> float:
        return max(0.0, min(1.0, (x - DIAL_W) / max(1, self.width() - DIAL_W)))

    def _hit_handle(self, x: int) -> str | None:
        ew    = self.width() - DIAL_W
        in_x  = DIAL_W + int(self._in  * ew)
        out_x = DIAL_W + int(self._out * ew)
        if abs(x - in_x)  <= HANDLE_W + 4: return "in"
        if abs(x - out_x) <= HANDLE_W + 4: return "out"
        return None

    def mousePressEvent(self, event):
        x = event.position().x()
        y = event.position().y()
        if y < PILL_ZONE_H:
            return
        hit = self._hit_handle(int(x))
        if hit:
            self._dragging = hit
        else:
            self._dragging = "seek"
            pos = max(self._in, min(self._out, self._norm(x)))
            self.seek_requested.emit(pos)

    def mouseMoveEvent(self, event):
        x    = event.position().x()
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
            self.seek_requested.emit(max(self._in, min(self._out, norm)))

        if not self._dragging:
            mx, my  = int(event.position().x()), int(event.position().y())
            new_idx = -1
            for idx, rect in self._pill_rects:
                if rect.contains(QPoint(mx, my)):
                    new_idx = idx
                    break
            if new_idx != self._hovered_idx:
                self._hovered_idx = new_idx
                self.update()

    def mouseReleaseEvent(self, event):
        self._dragging = None

    def leaveEvent(self, event):
        if self._hovered_idx != -1:
            self._hovered_idx = -1
            self.update()
