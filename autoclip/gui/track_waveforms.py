# SPDX-License-Identifier: GPL-3.0-or-later
"""
Multi-track waveform display with per-track volume and mute controls.
Shows one waveform strip per audio stream found in the clip file.
"""
import subprocess
import threading
import logging
import numpy as np
from typing import List, Dict, Optional

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QSlider, QSizePolicy, QFrame,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QPainter, QColor
from . import theme as _theme

logger = logging.getLogger(__name__)


class WaveformBar(QWidget):
    def __init__(self, label: str, color: str = None):
        super().__init__()
        self._samples = np.zeros(600)
        self._color   = QColor(color or _theme.current.waveform)
        self._muted   = False
        self._label   = label
        self.setFixedHeight(48)
        self.setMinimumWidth(200)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setStyleSheet(f"background: {_theme.current.bg_base};")

    def set_waveform(self, samples: np.ndarray):
        self._samples = samples
        self.update()

    def set_muted(self, muted: bool):
        self._muted = muted
        self.update()

    def paintEvent(self, event):
        t = _theme.current
        p = QPainter(self)
        w, h = self.width(), self.height()
        mid = h // 2
        p.fillRect(0, 0, w, h, QColor(t.bg_base))
        color = QColor(t.text_faint) if self._muted else self._color
        p.setBrush(color)
        p.setPen(Qt.PenStyle.NoPen)
        if len(self._samples) > 0:
            for i, amp in enumerate(self._samples):
                bar_h = max(0, int(amp * (mid - 4)))
                if bar_h == 0:
                    continue
                x = int(i * w / len(self._samples))
                bw = max(1, int(w / len(self._samples)) - 1)
                p.drawRect(x, mid - bar_h, bw, bar_h * 2)
        p.setPen(QColor(t.text_dim))
        p.drawText(4, 0, 60, h, Qt.AlignmentFlag.AlignVCenter, self._label)
        p.end()


class TrackVolumeControl(QWidget):
    mute_changed   = pyqtSignal(bool)
    volume_changed = pyqtSignal(float)

    def __init__(self, color: str):
        super().__init__()
        self.setFixedWidth(160)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 0, 6, 0)
        layout.setSpacing(6)

        self._mute_btn = QPushButton("🔊")
        self._mute_btn.setFixedSize(26, 26)
        self._mute_btn.setCheckable(True)
        self._mute_btn.setStyleSheet(
            "QPushButton { background: transparent; border: none; font-size: 14px; }"
            "QPushButton:checked { color: #555; }"
        )
        self._mute_btn.clicked.connect(self._on_mute)
        layout.addWidget(self._mute_btn)

        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(0, 100)
        self._slider.setValue(100)
        self._slider.setStyleSheet(
            f"QSlider::groove:horizontal {{ background: {_theme.current.border}; height: 3px; }}"
            f"QSlider::handle:horizontal {{ background: {color}; width: 12px; "
            f"height: 12px; margin: -4px 0; border-radius: 6px; }}"
            f"QSlider::sub-page:horizontal {{ background: {color}; }}"
        )
        self._slider.valueChanged.connect(lambda v: self.volume_changed.emit(v / 100.0))
        layout.addWidget(self._slider)

    def _on_mute(self, checked: bool):
        self._mute_btn.setText("🔇" if checked else "🔊")
        self.mute_changed.emit(checked)

    def set_volume(self, v: float):
        self._slider.blockSignals(True)
        self._slider.setValue(int(v * 100))
        self._slider.blockSignals(False)

    def set_muted(self, muted: bool):
        self._mute_btn.setChecked(muted)
        self._mute_btn.setText("🔇" if muted else "🔊")


class MultiTrackWaveform(QWidget):
    """
    Stacked waveform display — one strip per audio stream in the clip.
    Labels come from config tracks where available, fallback to "Track N".
    Volume/mute controls adjust mpv audio track volume via lavfi.
    """
    track_volume_changed = pyqtSignal(int, float)
    track_mute_changed   = pyqtSignal(int, bool)

    # Internal signal for thread-safe waveform updates
    _waveform_ready  = pyqtSignal(int, object)   # stream_index, np.ndarray
    tracks_probed    = pyqtSignal(int, list, list)  # n_tracks, volumes, mutes

    def __init__(self):
        super().__init__()
        self._bars:    List[WaveformBar]       = []
        self._vols:    List[TrackVolumeControl] = []
        self._n_streams = 0

        from PyQt6.QtWidgets import QScrollArea as _QSA
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._scroll = _QSA()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        t = _theme.current
        self._scroll.setStyleSheet(
            f"QScrollArea{{border:none;background:transparent;}}"
            f"QScrollBar:vertical{{width:6px;background:{t.bg_deep};border-radius:3px;}}"
            f"QScrollBar::handle:vertical{{background:{t.border};border-radius:3px;min-height:20px;}}"
        )

        self._inner = QWidget()
        self._layout = QVBoxLayout(self._inner)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(1)

        self._scroll.setWidget(self._inner)
        outer.addWidget(self._scroll)

        self._waveform_ready.connect(self._on_waveform)
        self.setMaximumHeight(3 * 51)  # show max 3 tracks, scroll for more

    def load_tracks(self, config_tracks: List[Dict], clip_path=None):
        """
        Build UI rows based on actual audio streams in the clip file.
        config_tracks provides labels/colours; clip_path drives stream count.
        """
        # Clear existing rows
        for i in reversed(range(self._layout.count())):
            item = self._layout.itemAt(i)
            if item and item.widget():
                item.widget().deleteLater()
        self._bars.clear()
        self._vols.clear()
        self._n_streams = 0

        if clip_path is None:
            return

        # Probe how many audio streams the clip actually has
        try:
            r = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "a",
                 "-show_entries", "stream=index",
                 "-of", "csv=p=0", str(clip_path)],
                capture_output=True, text=True, timeout=10
            )
            stream_lines = [l for l in r.stdout.splitlines() if l.strip()]
            n = len(stream_lines)
        except Exception:
            n = 1

        if n == 0:
            lbl = QLabel("No audio streams in clip")
            lbl.setStyleSheet(f"color: {_theme.current.text_faint}; font-size: 11px; padding: 8px;")
            self._layout.addWidget(lbl)
            return

        self._n_streams = n

        # Build label/colour map from config tracks (by position)
        enabled_tracks = [t for t in config_tracks if t.get("enabled") and t.get("device")]

        track_colors = _theme.current.track_colors()
        default_colors = list(track_colors.values())
        for i in range(n):
            # Match config track by index
            cfg = enabled_tracks[i] if i < len(enabled_tracks) else {}
            ttype = cfg.get("track_type", "custom")
            color = track_colors.get(ttype, default_colors[i % len(default_colors)])
            label = cfg.get("label", f"Track {i+1}")

            t = _theme.current
            row = QWidget()
            row.setFixedHeight(50)
            row.setStyleSheet(f"background: {t.bg_base}; border-bottom: 1px solid {t.border};")
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(0)

            bar = WaveformBar(label, color)
            self._bars.append(bar)
            rl.addWidget(bar, 1)

            sep = QFrame()
            sep.setFrameShape(QFrame.Shape.VLine)
            sep.setStyleSheet(f"background: {t.border}; max-width: 1px;")
            rl.addWidget(sep)

            vol = TrackVolumeControl(color)
            vol.set_volume(cfg.get("volume", 1.0))
            vol.set_muted(cfg.get("muted", False))
            idx = i
            vol.volume_changed.connect(lambda v, i=idx: self._on_volume(i, v))
            vol.mute_changed.connect(lambda m, i=idx: self._on_mute(i, m))
            self._vols.append(vol)
            rl.addWidget(vol)

            self._layout.addWidget(row)

        # Notify player of track count and initial volumes/mutes
        volumes = [cfg.get("volume", 1.0) for cfg in
                   (enabled_tracks[:n] + [{}] * max(0, n - len(enabled_tracks)))]
        mutes   = [cfg.get("muted", False) for cfg in
                   (enabled_tracks[:n] + [{}] * max(0, n - len(enabled_tracks)))]
        # Resize to fit up to 3 tracks, scroll for more
        visible = min(n, 3)
        self.setMaximumHeight(visible * 51)
        self.setMinimumHeight(visible * 51)
        self.tracks_probed.emit(n, volumes, mutes)

        # Extract waveforms in background
        if clip_path:
            threading.Thread(
                target=self._extract_all,
                args=(clip_path, n),
                daemon=True
            ).start()

    def _extract_all(self, clip_path, n_streams: int):
        for idx in range(n_streams):
            samples = _extract_track_waveform(clip_path, idx)
            self._waveform_ready.emit(idx, samples)

    def _on_waveform(self, idx: int, samples):
        if idx < len(self._bars):
            self._bars[idx].set_waveform(samples)

    def _on_volume(self, idx: int, volume: float):
        self.track_volume_changed.emit(idx, volume)

    def _on_mute(self, idx: int, muted: bool):
        if idx < len(self._bars):
            self._bars[idx].set_muted(muted)
        self.track_mute_changed.emit(idx, muted)

    def clear(self):
        for bar in self._bars:
            bar.set_waveform(np.zeros(600))


def _extract_track_waveform(path, track_index: int, num_samples: int = 600) -> np.ndarray:
    try:
        cmd = [
            "ffmpeg", "-v", "error",
            "-i", str(path),
            "-vn",
            "-map", f"0:a:{track_index}",
            "-ac", "1",
            "-ar", "8000",
            "-f", "f32le",
            "-",
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=60)
        if not result.stdout or len(result.stdout) < 4:
            return np.zeros(num_samples)
        data = np.frombuffer(result.stdout, dtype=np.float32)
        if len(data) < num_samples:
            return np.zeros(num_samples)
        chunk_size = max(1, len(data) // num_samples)
        samples = np.array([
            np.sqrt(np.mean(data[i*chunk_size:(i+1)*chunk_size]**2))
            for i in range(num_samples)
        ], dtype=np.float32)
        mx = samples.max()
        if mx > 0:
            samples /= mx
        return samples
    except Exception as e:
        logger.debug(f"Track {track_index} waveform extraction failed: {e}")
        return np.zeros(num_samples)
