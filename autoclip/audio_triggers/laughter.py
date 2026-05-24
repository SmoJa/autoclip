# SPDX-License-Identifier: GPL-3.0-or-later
"""
Laughter detection audio trigger using PANNs CNN6 (AudioSet neural network classifier).
Replaces the FFT energy-burst detector with local ML-based audio event classification.

Monitors two audio sources simultaneously (microphone + chat application audio).
Both sources share a single model session and a single trigger cooldown.

Requirements:
  pip install onnxruntime sounddevice
  python scripts/export_audio_classifier.py   (one-time model export, needs torch + panns_inference)

PANNs CNN6 processes audio at 32 kHz and outputs class probabilities for 527 AudioSet
categories per 0.96-second analysis window. We watch the laughter-family indices (16–21).
"""

import threading
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np

from .base import AudioTriggerPlugin

logger = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────

_MODELS_DIR = Path.home() / ".cache" / "autoclip" / "models"
_MODEL_PATH = _MODELS_DIR / "panns_cnn6.onnx"

_MODEL_URL = "https://github.com/SmoJa/autoclip/releases/download/v0.1.0/panns_cnn6.onnx"

# ── AudioSet class indices for laughter variants ──────────────────────────────
# Source: AudioSet ontology, PANNs 527-class label map (Kong et al.)
_LAUGHTER_INDICES = [16, 17, 18, 19, 20, 21]
# 16=Laughter  17=Baby laughter  18=Giggle  19=Snicker  20=Belly laugh  21=Chuckle

# ── Audio constants ───────────────────────────────────────────────────────────
SAMPLE_RATE    = 32000
WINDOW_SAMPLES = 30720   # 0.96 s at 32 kHz — one CNN6 analysis window
STRIDE_SAMPLES = 16000   # 0.5 s — how often we run inference
BLOCK_SIZE     = 3200    # 0.1 s — sounddevice capture granularity


# ── Config ────────────────────────────────────────────────────────────────────

@dataclass
class LaughterConfig:
    enabled:      bool  = False
    sensitivity:  float = 0.6    # 0.0–1.0; mapped to score threshold 0.40–0.10
    cooldown:     float = 10.0   # minimum seconds between triggers
    mic_enabled:  bool  = True   # monitor user microphone
    mic_device:   str   = ""     # device name; empty = system default mic
    chat_enabled: bool  = True   # monitor chat application audio
    chat_device:  str   = ""     # device name; empty = auto-detect monitor device


# ── Plugin ────────────────────────────────────────────────────────────────────

class LaughterPlugin(AudioTriggerPlugin):
    NAME              = "Laughter"
    TRIGGER_NAME      = "laughter"
    TRIGGER_DISPLAY   = "Laughter"
    TRIGGER_LOG_STYLE = ("Laughter", "#ff9900", "#2a1a00", True)

    def __init__(self, config: Any, on_trigger: Callable[[], None]):
        super().__init__(config, on_trigger)
        self._running        = False
        self._threads:       list[threading.Thread] = []
        self._session        = None
        self._last_triggered = 0.0
        self._trigger_lock   = threading.Lock()

    def _cfg(self) -> LaughterConfig:
        return getattr(self.config, "laughter", LaughterConfig())

    def start(self):
        cfg = self._cfg()
        if not cfg.enabled or self._running:
            return

        for pkg in ("sounddevice", "onnxruntime"):
            try:
                __import__(pkg)
            except ImportError:
                logger.error(f"Laughter detection requires '{pkg}' — pip install {pkg}")
                return

        if not _MODEL_PATH.exists():
            logger.warning("PANNs CNN6 model not found — laughter detection disabled. "
                           "Run scripts/export_audio_classifier.py or download in Settings.")
            return

        try:
            from autoclip.core.model_cache import ModelCache
            self._session = ModelCache.get_session(_MODEL_PATH)
        except Exception as e:
            logger.error(f"Failed to load audio classifier model: {e}")
            return

        sources = self._resolve_sources(cfg)
        if not sources:
            logger.warning("Laughter detection: no sources enabled")
            return

        self._running = True
        self._threads = []
        for device, name in sources:
            t = threading.Thread(target=self._run_source, args=(device, name), daemon=True)
            self._threads.append(t)
            t.start()
        logger.info(f"ML laughter detector started ({len(sources)} source(s): "
                    f"{[n for _, n in sources]})")

    def stop(self):
        self._running = False

    def _resolve_sources(self, cfg: LaughterConfig) -> list[tuple]:
        """Return list of (sounddevice_device, label) for enabled sources."""
        import sounddevice as sd
        sources = []
        if cfg.mic_enabled:
            device = self._find_device(sd, cfg.mic_device, monitor=False)
            sources.append((device, "mic"))
            logger.info(f"Laughter mic source: {device!r} ({cfg.mic_device or 'default'})")
        if cfg.chat_enabled:
            device = self._find_device(sd, cfg.chat_device, monitor=True)
            sources.append((device, "chat"))
            logger.info(f"Laughter chat source: {device!r} ({cfg.chat_device or 'auto-monitor'})")
        return sources

    @staticmethod
    def _find_device(sd, name: str, monitor: bool) -> Optional[int]:
        """Resolve a device name to an index, or auto-detect a monitor if name is empty."""
        if name:
            for i, d in enumerate(sd.query_devices()):
                if name in d["name"] and d["max_input_channels"] > 0:
                    return i
            logger.warning(f"Audio device '{name}' not found, using default")
            return None
        if monitor:
            for i, d in enumerate(sd.query_devices()):
                if "monitor" in d["name"].lower() and d["max_input_channels"] > 0:
                    return i
            logger.warning("No monitor device found for chat audio capture")
        return None  # system default

    def _run_source(self, device: Optional[int], source_name: str):
        import sounddevice as sd
        ring  = np.zeros(WINDOW_SAMPLES, dtype=np.float32)
        head  = 0
        since = 0
        try:
            with sd.InputStream(device=device, channels=1, samplerate=SAMPLE_RATE,
                                blocksize=BLOCK_SIZE, dtype="float32") as stream:
                while self._running:
                    block, _ = stream.read(BLOCK_SIZE)
                    samples   = block.flatten()
                    n         = len(samples)
                    end       = head + n
                    if end <= WINDOW_SAMPLES:
                        ring[head:end] = samples
                    else:
                        split          = WINDOW_SAMPLES - head
                        ring[head:]    = samples[:split]
                        ring[:n-split] = samples[split:]
                    head   = (head + n) % WINDOW_SAMPLES
                    since += n
                    if since >= STRIDE_SAMPLES:
                        since    = 0
                        waveform = np.concatenate([ring[head:], ring[:head]])
                        self._infer(waveform, source_name)
        except Exception as e:
            logger.error(f"Laughter capture error ({source_name}): {e}")

    def _infer(self, waveform: np.ndarray, source_name: str):
        cfg = self._cfg()
        try:
            inp  = self._session.get_inputs()[0]
            data = waveform.astype(np.float32)
            if len(inp.shape) == 2:
                data = data[np.newaxis, :]
            raw    = self._session.run(None, {inp.name: data})[0]
            scores = np.asarray(raw)
            if scores.ndim == 3:
                scores = scores[0]
            laughter_score = float(scores.mean(axis=0)[_LAUGHTER_INDICES].max())
        except Exception as e:
            logger.debug(f"CNN6 inference error: {e}")
            return

        threshold = 0.40 - cfg.sensitivity * 0.30
        fired = False
        with self._trigger_lock:
            now = time.time()
            if laughter_score >= threshold and now - self._last_triggered > cfg.cooldown:
                self._last_triggered = now
                fired = True

        if fired:
            logger.info(f"Laughter [{source_name}]  score={laughter_score:.3f}  thr={threshold:.3f}")
            self.on_trigger()

    def get_config_class(self):
        return LaughterConfig

    def get_config_widget(self, config: Any, parent=None):
        return LaughterWidget(config, parent)


# ── Settings widget ───────────────────────────────────────────────────────────

def LaughterWidget(config: Any, parent=None):
    """Factory: returns a configured QWidget for the laughter plugin settings."""
    from PyQt6.QtWidgets import (
        QWidget, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox,
        QSlider, QDoubleSpinBox, QGroupBox, QGridLayout, QPushButton,
    )
    from PyQt6.QtCore import Qt, QThread, pyqtSignal
    from autoclip.gui import theme as _theme
    from autoclip.gui.widgets import NoScrollComboBox

    t = _theme.current

    def _gsr_friendly_names():
        """Return {device_id: friendly_name} from gsr --list-audio-devices."""
        try:
            from autoclip.core.audio import get_gsr_audio_devices
            gsr_path = getattr(config, "gpu_recorder_path", "gpu-screen-recorder")
            return {src.device: src.name for src in get_gsr_audio_devices(gsr_path)}
        except Exception:
            return {}

    def _input_devices():
        """Input devices with gsr-sourced friendly names where available."""
        friendly = _gsr_friendly_names()
        devs = [("System default", "")]
        try:
            import sounddevice as sd
            for d in sd.query_devices():
                if d["max_input_channels"] > 0 and "monitor" not in d["name"].lower():
                    name = d["name"]
                    devs.append((friendly.get(name, name), name))
        except Exception:
            pass
        return devs

    def _monitor_devices():
        """Monitor/loopback devices with gsr-sourced friendly names, listed first."""
        friendly = _gsr_friendly_names()
        # Also note any running chat app so the user knows which monitor to pick
        chat_hint = ""
        try:
            from autoclip.core.audio import get_pw_app_nodes, CHAT_APP_BINARIES, FRIENDLY_APP_NAMES
            for node in get_pw_app_nodes():
                binary = node.get("binary", "").lower()
                if any(c in binary for c in CHAT_APP_BINARIES):
                    chat_hint = FRIENDLY_APP_NAMES.get(binary, binary.capitalize())
                    break
        except Exception:
            pass

        label0 = f"Auto-detect monitor" + (f"  ({chat_hint} detected)" if chat_hint else "")
        monitors = [(label0, "")]
        others   = []
        try:
            import sounddevice as sd
            for d in sd.query_devices():
                if d["max_input_channels"] > 0:
                    name  = d["name"]
                    label = friendly.get(name, name)
                    if "monitor" in name.lower():
                        monitors.append((label, name))
                    else:
                        others.append((label, name))
        except Exception:
            pass
        return monitors + others

    def _recorder_mic_device():
        """Return the device name used by the recorder's mic track, or ''."""
        for track in (getattr(config, "audio_tracks", None) or []):
            if track.get("track_type") == "mic" and track.get("device"):
                return track["device"]
        return ""

    def _recorder_chat_label():
        """Return the label of the recorder's chat track, or ''."""
        for track in (getattr(config, "audio_tracks", None) or []):
            if track.get("track_type") == "chat":
                return track.get("label", "")
        return ""

    def _build_device_combo(items, current_name: str) -> NoScrollComboBox:
        combo = NoScrollComboBox()
        for label, value in items:
            combo.addItem(label, userData=value)
        # Select the item whose data matches current_name
        for i in range(combo.count()):
            if combo.itemData(i) == current_name:
                combo.setCurrentIndex(i)
                break
        return combo

    # ── Downloader thread ──────────────────────────────────────────────────
    class _Downloader(QThread):
        progress = pyqtSignal(int)
        done     = pyqtSignal(bool, str)

        def __init__(self, url: str, dest: Path):
            super().__init__()
            self._url  = url
            self._dest = dest

        def run(self):
            import urllib.request
            tmp = self._dest.with_suffix(".tmp")
            try:
                self._dest.parent.mkdir(parents=True, exist_ok=True)
                def _hook(count, block_size, total):
                    if total > 0:
                        self.progress.emit(min(100, int(count * block_size * 100 / total)))
                urllib.request.urlretrieve(self._url, str(tmp), reporthook=_hook)
                tmp.rename(self._dest)
                self.done.emit(True, "")
            except Exception as e:
                tmp.unlink(missing_ok=True)
                self.done.emit(False, str(e))

    # ── Main widget ────────────────────────────────────────────────────────
    class _Widget(QWidget):
        def __init__(self, cfg_obj, par):
            super().__init__(par)
            self._config     = cfg_obj
            self._downloader = None
            self._build_ui()

        def _build_ui(self):
            root = QVBoxLayout(self)
            root.setContentsMargins(0, 0, 0, 0)
            root.setSpacing(12)

            desc = QLabel(
                "Monitors microphone and chat audio simultaneously using PANNs CNN6, "
                "an AudioSet-trained neural network. Triggers a clip save when laughter "
                "is detected on either source. All processing is local."
            )
            desc.setWordWrap(True)
            desc.setStyleSheet(f"color: {t.text_dim}; font-size: 11px;")
            root.addWidget(desc)

            # ── Model status ───────────────────────────────────────────────
            setup_box = QGroupBox("Setup")
            sl = QVBoxLayout(setup_box)
            sl.setSpacing(8)

            try:
                import onnxruntime  # noqa
            except ImportError:
                warn = QLabel("onnxruntime not installed.  Run:  pip install onnxruntime")
                warn.setStyleSheet("color: #ff6666; font-family: monospace; font-size: 11px;")
                sl.addWidget(warn)

            model_row = QWidget()
            mr = QHBoxLayout(model_row)
            mr.setContentsMargins(0, 0, 0, 0); mr.setSpacing(8)
            self._model_lbl    = QLabel()
            self._dl_btn       = QPushButton("Download model")
            self._dl_btn.setFixedWidth(140)
            self._progress_lbl = QLabel("")
            self._progress_lbl.setStyleSheet(f"color: {t.text_dim}; font-size: 10px;")
            mr.addWidget(self._model_lbl)
            mr.addWidget(self._dl_btn)
            mr.addWidget(self._progress_lbl)
            mr.addStretch()
            sl.addWidget(model_row)

            if not _MODEL_URL:
                note = QLabel(
                    f"Place panns_cnn6.onnx at:  {_MODEL_PATH}\n"
                    "Run:  python3 scripts/export_audio_classifier.py"
                )
                note.setWordWrap(True)
                note.setStyleSheet(f"color: {t.text_faint}; font-size: 10px; font-family: monospace;")
                sl.addWidget(note)

            root.addWidget(setup_box)
            self._refresh_model_status()
            self._dl_btn.clicked.connect(self._start_download)

            # ── Detection settings ─────────────────────────────────────────
            detect_box = QGroupBox("Detection")
            dl = QGridLayout(detect_box)
            dl.setSpacing(12)

            cfg            = getattr(self._config, "laughter", None)
            init_enabled   = cfg.enabled      if cfg else False
            init_sens      = cfg.sensitivity  if cfg else 0.6
            init_cool      = cfg.cooldown     if cfg else 10.0
            init_mic_en    = cfg.mic_enabled  if cfg else True
            init_chat_en   = cfg.chat_enabled if cfg else True

            # If no device has been saved yet, inherit from the recorder's tracks
            # so the user doesn't have to configure both independently.
            init_mic_dev   = (cfg.mic_device  if cfg and cfg.mic_device
                              else _recorder_mic_device())
            init_chat_dev  = cfg.chat_device  if cfg else ""

            self._enabled_cb = QCheckBox("Enable laughter detection")
            self._enabled_cb.setChecked(init_enabled)
            dl.addWidget(self._enabled_cb, 0, 0, 1, 2)

            dl.addWidget(QLabel("Sensitivity"), 1, 0)
            sens_row = QWidget()
            sr = QHBoxLayout(sens_row)
            sr.setContentsMargins(0, 0, 0, 0)
            self._sens_slider = QSlider(Qt.Orientation.Horizontal)
            self._sens_slider.setRange(0, 100)
            self._sens_slider.setValue(int(init_sens * 100))
            self._sens_lbl = QLabel(f"{self._sens_slider.value()}%")
            self._sens_lbl.setFixedWidth(40)
            self._sens_slider.valueChanged.connect(lambda v: self._sens_lbl.setText(f"{v}%"))
            sr.addWidget(self._sens_slider); sr.addWidget(self._sens_lbl)
            dl.addWidget(sens_row, 1, 1)

            sens_hint = QLabel(
                "Higher values detect quieter or briefer laughter but increase false positives.\n"
                "Low (0–30%): loud, unmistakable laughter only.\n"
                "Medium (40–70%): most laughter caught, occasional false triggers.\n"
                "High (80–100%): catches quiet chuckles, but applause or excited speech may trigger it."
            )
            sens_hint.setStyleSheet(f"color: {t.text_faint}; font-size: 10px;")
            dl.addWidget(sens_hint, 2, 0, 1, 2)

            dl.addWidget(QLabel("Cooldown (s)"), 3, 0)
            self._cooldown_spin = QDoubleSpinBox()
            self._cooldown_spin.setRange(5.0, 120.0)
            self._cooldown_spin.setSingleStep(5.0)
            self._cooldown_spin.setDecimals(0)
            self._cooldown_spin.setValue(init_cool)
            self._cooldown_spin.setFixedWidth(80)
            dl.addWidget(self._cooldown_spin, 3, 1)

            cool_hint = QLabel(
                "Minimum seconds between saves. Prevents multiple clips from the same laugh."
            )
            cool_hint.setStyleSheet(f"color: {t.text_faint}; font-size: 10px;")
            dl.addWidget(cool_hint, 4, 0, 1, 2)

            root.addWidget(detect_box)

            # ── Sources ────────────────────────────────────────────────────
            src_box = QGroupBox("Audio Sources")
            src_l   = QGridLayout(src_box)
            src_l.setSpacing(10)
            src_l.setColumnStretch(1, 1)

            # Microphone row
            self._mic_cb = QCheckBox("Microphone")
            self._mic_cb.setChecked(init_mic_en)
            self._mic_combo = _build_device_combo(_input_devices(), init_mic_dev)
            self._mic_combo.setEnabled(init_mic_en)
            self._mic_cb.toggled.connect(self._mic_combo.setEnabled)
            src_l.addWidget(self._mic_cb,    0, 0)
            src_l.addWidget(self._mic_combo, 0, 1)

            # Chat audio row
            self._chat_cb = QCheckBox("Chat audio")
            self._chat_cb.setChecked(init_chat_en)
            self._chat_combo = _build_device_combo(_monitor_devices(), init_chat_dev)
            self._chat_combo.setEnabled(init_chat_en)
            self._chat_cb.toggled.connect(self._chat_combo.setEnabled)
            src_l.addWidget(self._chat_cb,    1, 0)
            src_l.addWidget(self._chat_combo, 1, 1)

            chat_rec = _recorder_chat_label()
            hint_text = (
                f"Monitor devices capture all system audio output. "
                f"Matches your recorder's \"{chat_rec}\" track."
                if chat_rec else
                "Monitor devices capture all system audio output — "
                "use these for chat apps (Discord, TeamSpeak, etc.)."
            )
            hint = QLabel(hint_text)
            hint.setWordWrap(True)
            hint.setStyleSheet(f"color: {t.text_faint}; font-size: 10px;")
            src_l.addWidget(hint, 2, 0, 1, 2)

            root.addWidget(src_box)

        def _refresh_model_status(self):
            if _MODEL_PATH.exists():
                size_mb = _MODEL_PATH.stat().st_size / 1_048_576
                self._model_lbl.setText(f"Model ready  ({size_mb:.1f} MB)")
                self._model_lbl.setStyleSheet("color: #66cc88;")
                self._dl_btn.setVisible(False)
                self._progress_lbl.setVisible(False)
            else:
                self._model_lbl.setText("Model not downloaded")
                self._model_lbl.setStyleSheet(f"color: {t.text_dim};")
                self._dl_btn.setVisible(bool(_MODEL_URL))
                self._progress_lbl.setVisible(False)

        def _start_download(self):
            if self._downloader and self._downloader.isRunning():
                return
            self._dl_btn.setEnabled(False)
            self._progress_lbl.setText("Starting…")
            self._progress_lbl.setVisible(True)
            self._downloader = _Downloader(_MODEL_URL, _MODEL_PATH)
            self._downloader.progress.connect(lambda p: self._progress_lbl.setText(f"{p}%"))
            self._downloader.done.connect(self._on_dl_done)
            self._downloader.start()

        def _on_dl_done(self, ok: bool, err: str):
            self._dl_btn.setEnabled(True)
            if ok:
                self._progress_lbl.setText("")
                self._refresh_model_status()
            else:
                self._progress_lbl.setText(f"Failed: {err}")
                self._progress_lbl.setStyleSheet("color: #ff6666;")

        def save(self):
            cfg = getattr(self._config, "laughter", None)
            if cfg is not None:
                cfg.enabled      = self._enabled_cb.isChecked()
                cfg.sensitivity  = self._sens_slider.value() / 100.0
                cfg.cooldown     = float(self._cooldown_spin.value())
                cfg.mic_enabled  = self._mic_cb.isChecked()
                cfg.mic_device   = self._mic_combo.currentData() or ""
                cfg.chat_enabled = self._chat_cb.isChecked()
                cfg.chat_device  = self._chat_combo.currentData() or ""
            self._config.save()

    return _Widget(config, parent)
