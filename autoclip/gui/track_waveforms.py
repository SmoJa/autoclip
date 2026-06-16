# SPDX-License-Identifier: GPL-3.0-or-later
"""
Audio track data provider for the clip player.
Probes stream count, extracts per-track waveforms, and emits results upstream.
Volume/mute controls are rendered as VolumeDial children of TimelineWidget.
"""
import subprocess
import sys
import threading
import logging
import numpy as np
from typing import List, Dict

from autoclip.core.clips import _run_capture

from PyQt6.QtCore import QObject, pyqtSignal
from autoclip.core.clips import _FFMPEG, _FFPROBE

logger = logging.getLogger(__name__)


class MultiTrackWaveform(QObject):
    """
    Non-visual QObject that:
      - probes how many audio streams a clip has
      - extracts per-track waveform samples in a background thread
      - emits tracks_probed  → timeline sets up VolumeDial controls
      - emits waveform_ready → timeline renders waveform strips
    """
    # n_tracks, volumes, mutes, colors, labels
    tracks_probed  = pyqtSignal(int, list, list, list, list)
    # track_idx, samples (np.ndarray), color (str)
    waveform_ready = pyqtSignal(int, object, str)

    _waveform_sig  = pyqtSignal(int, object, str)   # thread-safe relay

    def __init__(self, parent=None):
        super().__init__(parent)
        self._n_streams = 0
        self._waveform_sig.connect(self._on_waveform)

    def load_tracks(self, config_tracks: List[Dict], clip_path=None):
        self._n_streams = 0
        if clip_path is None:
            return

        try:
            stdout, _, _ = _run_capture(
                [_FFPROBE, "-v", "error", "-select_streams", "a",
                 "-show_entries", "stream=index",
                 "-of", "csv=p=0", str(clip_path)],
                timeout=10,
            )
            n = len([l for l in stdout.splitlines() if l.strip()])
        except Exception:
            n = 1
        if n == 0:
            return

        self._n_streams   = n
        enabled_tracks    = [t for t in config_tracks if t.get("enabled") and t.get("device")]

        from . import theme as _theme
        track_colors_map  = _theme.current.track_colors()
        default_colors    = list(track_colors_map.values())
        track_color_list: List[str] = []

        track_labels: List[str] = []
        for i in range(n):
            cfg   = enabled_tracks[i] if i < len(enabled_tracks) else {}
            ttype = cfg.get("track_type", "custom")
            color = track_colors_map.get(ttype, default_colors[i % len(default_colors)])
            track_color_list.append(color)
            track_labels.append(cfg.get("label", f"Track {i + 1}"))

        volumes = [
            (enabled_tracks[i].get("volume", 1.0) if i < len(enabled_tracks) else 1.0)
            for i in range(n)
        ]
        mutes = [
            (enabled_tracks[i].get("muted", False) if i < len(enabled_tracks) else False)
            for i in range(n)
        ]

        self.tracks_probed.emit(n, volumes, mutes, track_color_list, track_labels)

        threading.Thread(
            target=self._extract_all,
            args=(clip_path, n, list(track_color_list)),
            daemon=True
        ).start()

    def _extract_all(self, clip_path, n_streams: int, colors: List[str]):
        for idx in range(n_streams):
            samples = _extract_track_waveform(clip_path, idx)
            color   = colors[idx] if idx < len(colors) else "#888888"
            self._waveform_sig.emit(idx, samples, color)

    def _on_waveform(self, idx: int, samples, color: str):
        self.waveform_ready.emit(idx, samples, color)


def _extract_track_waveform(path, track_index: int, num_samples: int = 600) -> np.ndarray:
    try:
        cmd = [
            _FFMPEG, "-v", "error",
            "-i", str(path),
            "-vn",
            "-map", f"0:a:{track_index}",
            "-ac", "1",
            "-ar", "8000",
            "-f", "f32le",
            "-",
        ]
        import tempfile
        with tempfile.TemporaryFile() as out_f:
            flags = {}
            if sys.platform == "win32":
                flags = {"stdin": subprocess.DEVNULL,
                         "creationflags": subprocess.CREATE_NO_WINDOW}
            proc = subprocess.Popen(cmd, stdout=out_f, stderr=subprocess.DEVNULL, **flags)
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            out_f.seek(0)
            raw = out_f.read()
        if not raw or len(raw) < 4:
            return np.zeros(num_samples)
        data = np.frombuffer(raw, dtype=np.float32)
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
            # Log scaling — maps [-60 dB, 0 dB] → [0, 1] so quiet
            # sections remain visible instead of rounding to zero.
            MIN_DB = -41.0
            floor  = 10.0 ** (MIN_DB / 20.0)
            db     = 20.0 * np.log10(np.maximum(samples, floor))
            samples = np.clip((db - MIN_DB) / (-MIN_DB), 0.0, 1.0)
        return samples
    except Exception as e:
        logger.debug(f"Track {track_index} waveform extraction failed: {e}")
        return np.zeros(num_samples)
