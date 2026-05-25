# SPDX-License-Identifier: GPL-3.0-or-later
"""
Reactions audio trigger — detects laughter, screaming, and shouting using
PANNs CNN6 (AudioSet neural network classifier).

Each reaction type has independent enable, sensitivity, and cooldown settings.
A single model session and audio capture loop serves all enabled types, so
CPU cost is the same regardless of how many are turned on.

Requirements:
  pip install onnxruntime sounddevice
  Model downloadable from Settings.
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
_MODEL_URL  = "https://github.com/SmoJa/autoclip/releases/download/v0.1.0/panns_cnn6.onnx"

# ── Reaction definitions ───────────────────────────────────────────────────────
# AudioSet class indices from PANNs 527-class label map (Kong et al.)

_REACTIONS = [
    {
        "key":      "laughter",
        "display":  "Laughter",
        "desc":     "Giggling, chuckling, belly laughs — any kind of laughing.",
        "trigger":  "laughter",
        "color":    ("#ff9900", "#2a1a00"),
        "indices":  [16, 17, 18, 19, 20, 21],  # Laughter, Baby laughter, Giggle, Snicker, Belly laugh, Chuckle
        "sens":     0.6,
        "cooldown": 10.0,
    },
    {
        "key":      "screaming",
        "display":  "Screaming",
        "desc":     "Sharp, sudden vocal outbursts — jump scares, panic, shock.",
        "trigger":  "screaming",
        "color":    ("#ff4444", "#220000"),
        "indices":  [13],  # Screaming
        "sens":     0.5,
        "cooldown": 15.0,
    },
    {
        "key":      "shouting",
        "display":  "Shouting",
        "desc":     "Sustained raised voice — hype calls, yelling, excited reactions.",
        "trigger":  "shouting",
        "color":    ("#ff8844", "#1a0800"),
        "indices":  [8, 9, 10, 11],  # Shout, Bellow, Whoop, Yell
        "sens":     0.5,
        "cooldown": 15.0,
    },
]

# ── Audio constants ───────────────────────────────────────────────────────────

SAMPLE_RATE    = 32000
WINDOW_SAMPLES = 30720   # 0.96 s at 32 kHz — one CNN6 analysis window
STRIDE_SAMPLES = 16000   # 0.5 s — how often inference runs
BLOCK_SIZE     = 3200    # 0.1 s — sounddevice capture granularity

# ── Config ────────────────────────────────────────────────────────────────────

@dataclass
class ReactionsConfig:
    enabled:      bool  = False
    mic_enabled:  bool  = True
    mic_device:   str   = ""
    chat_enabled: bool  = True
    chat_device:  str   = ""
    # Per-reaction settings (flat — avoids nested-dataclass serialisation issues)
    laughter_enabled:     bool  = True
    laughter_sensitivity: float = 0.6
    laughter_cooldown:    float = 10.0
    screaming_enabled:    bool  = True
    screaming_sensitivity: float = 0.5
    screaming_cooldown:   float = 15.0
    shouting_enabled:     bool  = True
    shouting_sensitivity: float = 0.5
    shouting_cooldown:    float = 15.0


# ── Plugin ────────────────────────────────────────────────────────────────────

class ReactionsPlugin(AudioTriggerPlugin):
    NAME         = "Reactions"
    TRIGGER_NAME = "reactions"
    TRIGGER_LOG_STYLES = {
        r["trigger"]: (r["display"], r["color"][0], r["color"][1], True)
        for r in _REACTIONS
    }

    def __init__(self, config: Any, on_trigger: Callable):
        super().__init__(config, on_trigger)
        self._running  = False
        self._threads: list[threading.Thread] = []
        self._session  = None
        self._last     = {r["trigger"]: 0.0 for r in _REACTIONS}
        self._lock     = threading.Lock()

    def _cfg(self) -> ReactionsConfig:
        return getattr(self.config, "reactions", ReactionsConfig())

    def start(self):
        cfg = self._cfg()
        if not cfg.enabled or self._running:
            return

        for pkg in ("sounddevice", "onnxruntime"):
            try:
                __import__(pkg)
            except ImportError:
                logger.error(f"Reactions detection requires '{pkg}' — pip install {pkg}")
                return

        if not _MODEL_PATH.exists():
            logger.warning("PANNs CNN6 model not found — reactions detection disabled. "
                           "Download it in Settings → Audio Triggers → Reactions.")
            return

        try:
            from autoclip.core.model_cache import ModelCache
            self._session = ModelCache.get_session(_MODEL_PATH)
        except Exception as e:
            logger.error(f"Failed to load audio classifier model: {e}")
            return

        sources = self._resolve_sources(cfg)
        if not sources:
            logger.warning("Reactions detection: no sources enabled")
            return

        self._running = True
        self._threads = []
        for device, label in sources:
            t = threading.Thread(target=self._run_source, args=(device, label), daemon=True)
            self._threads.append(t)
            t.start()
        logger.info(f"Reactions detector started ({len(sources)} source(s): "
                    f"{[l for _, l in sources]})")

    def stop(self):
        self._running = False

    def _resolve_sources(self, cfg: ReactionsConfig) -> list[tuple]:
        import sounddevice as sd
        sources = []
        if cfg.mic_enabled:
            device = self._find_device(sd, cfg.mic_device, monitor=False)
            sources.append((device, "mic"))
            logger.info(f"Reactions mic source: {device!r} ({cfg.mic_device or 'default'})")
        if cfg.chat_enabled:
            device = self._find_device(sd, cfg.chat_device, monitor=True)
            sources.append((device, "chat"))
            logger.info(f"Reactions chat source: {device!r} ({cfg.chat_device or 'auto-monitor'})")
        return sources

    @staticmethod
    def _find_device(sd, name: str, monitor: bool) -> Optional[int]:
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
        return None

    def _run_source(self, device: Optional[int], source_label: str):
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
                        self._infer(waveform, source_label)
        except Exception as e:
            logger.error(f"Reactions capture error ({source_label}): {e}")

    def _infer(self, waveform: np.ndarray, source_label: str):
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
            avg = scores.mean(axis=0)
        except Exception as e:
            logger.debug(f"Reactions inference error: {e}")
            return

        now = time.time()
        for r in _REACTIONS:
            key     = r["key"]
            trigger = r["trigger"]
            if not getattr(cfg, f"{key}_enabled", False):
                continue
            sens      = getattr(cfg, f"{key}_sensitivity", r["sens"])
            cooldown  = getattr(cfg, f"{key}_cooldown",    r["cooldown"])
            score     = float(avg[r["indices"]].max())
            threshold = 0.40 - sens * 0.30
            fired = False
            with self._lock:
                if score >= threshold and now - self._last[trigger] > cooldown:
                    self._last[trigger] = now
                    fired = True
            if fired:
                logger.info(
                    f"Reactions [{source_label}] {key}  "
                    f"score={score:.3f}  thr={threshold:.3f}"
                )
                self.on_trigger(trigger)

    def get_config_class(self):
        return ReactionsConfig

    def get_config_widget(self, config: Any, parent=None):
        return ReactionsWidget(config, parent)


# ── Settings widget ───────────────────────────────────────────────────────────

def ReactionsWidget(config: Any, parent=None):
    from PyQt6.QtWidgets import (
        QWidget, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox,
        QSlider, QDoubleSpinBox, QGroupBox, QGridLayout, QPushButton,
    )
    from PyQt6.QtCore import Qt, QThread, pyqtSignal
    from autoclip.gui import theme as _theme
    from autoclip.gui.widgets import NoScrollComboBox

    t = _theme.current

    def _gsr_friendly_names():
        try:
            from autoclip.core.audio import get_gsr_audio_devices
            gsr_path = getattr(config, "gpu_recorder_path", "gpu-screen-recorder")
            return {src.device: src.name for src in get_gsr_audio_devices(gsr_path)}
        except Exception:
            return {}

    def _input_devices():
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
        friendly = _gsr_friendly_names()
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
        label0   = "Auto-detect monitor" + (f"  ({chat_hint} detected)" if chat_hint else "")
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
        for track in (getattr(config, "audio_tracks", None) or []):
            if track.get("track_type") == "mic" and track.get("device"):
                return track["device"]
        return ""

    def _recorder_chat_label():
        for track in (getattr(config, "audio_tracks", None) or []):
            if track.get("track_type") == "chat":
                return track.get("label", "")
        return ""

    def _build_combo(items, current_name: str) -> NoScrollComboBox:
        combo = NoScrollComboBox()
        for label, value in items:
            combo.addItem(label, userData=value)
        for i in range(combo.count()):
            if combo.itemData(i) == current_name:
                combo.setCurrentIndex(i)
                break
        return combo

    # ── Downloader ─────────────────────────────────────────────────────────
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
        settings_changed = pyqtSignal()

        def __init__(self, cfg_obj, par):
            super().__init__(par)
            self._config     = cfg_obj
            self._downloader = None
            self._reaction_cbs:    dict = {}
            self._reaction_sliders: dict = {}
            self._reaction_lbls:   dict = {}
            self._reaction_spins:  dict = {}
            self._build_ui()

        def _build_ui(self):
            root = QVBoxLayout(self)
            root.setContentsMargins(0, 0, 0, 0)
            root.setSpacing(12)

            desc = QLabel(
                "Monitors microphone and chat audio using PANNs CNN6, an AudioSet-trained "
                "neural network. Detects laughter, screaming, and shouting — each with "
                "independent sensitivity and cooldown. All processing is local."
            )
            desc.setWordWrap(True)
            desc.setStyleSheet(f"color: {t.text_dim}; font-size: 11px;")
            root.addWidget(desc)

            # ── Setup ──────────────────────────────────────────────────────
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
            mr.setContentsMargins(0, 0, 0, 0)
            mr.setSpacing(8)
            self._model_lbl    = QLabel()
            self._dl_btn       = QPushButton("Download model")
            self._progress_lbl = QLabel("")
            self._progress_lbl.setStyleSheet(f"color: {t.text_dim}; font-size: 10px;")
            mr.addWidget(self._model_lbl)
            mr.addWidget(self._dl_btn)
            mr.addWidget(self._progress_lbl)
            mr.addStretch()
            sl.addWidget(model_row)

            if not _MODEL_URL:
                note = QLabel(f"Place panns_cnn6.onnx at:  {_MODEL_PATH}")
                note.setStyleSheet(f"color: {t.text_faint}; font-size: 10px; font-family: monospace;")
                sl.addWidget(note)

            root.addWidget(setup_box)
            self._refresh_model_status()
            self._dl_btn.clicked.connect(self._start_download)

            # ── Detection ──────────────────────────────────────────────────
            detect_box = QGroupBox("Detection")
            dl = QVBoxLayout(detect_box)
            dl.setSpacing(10)

            cfg = getattr(self._config, "reactions", None)

            self._enabled_cb = QCheckBox("Enable reactions detection")
            self._enabled_cb.setChecked(cfg.enabled if cfg else False)
            dl.addWidget(self._enabled_cb)

            # Per-reaction grid
            grid_w = QWidget()
            grid   = QGridLayout(grid_w)
            grid.setSpacing(8)
            grid.setColumnStretch(2, 1)

            # Column headers
            for col, hdr in enumerate(("", "Enable", "Sensitivity", "Cooldown (s)")):
                lbl = QLabel(hdr)
                lbl.setStyleSheet(f"color: {t.text_faint}; font-size: 10px;")
                grid.addWidget(lbl, 0, col)

            for row, r in enumerate(_REACTIONS, start=1):
                key = r["key"]

                name_w  = QWidget()
                name_vb = QVBoxLayout(name_w)
                name_vb.setContentsMargins(0, 0, 0, 0)
                name_vb.setSpacing(1)
                name_lbl = QLabel(r["display"])
                name_lbl.setStyleSheet("font-weight: bold;")
                desc_lbl = QLabel(r["desc"])
                desc_lbl.setStyleSheet(f"color: {t.text_faint}; font-size: 10px;")
                desc_lbl.setWordWrap(True)
                name_vb.addWidget(name_lbl)
                name_vb.addWidget(desc_lbl)
                grid.addWidget(name_w, row, 0)

                en_cb = QCheckBox()
                en_cb.setChecked(getattr(cfg, f"{key}_enabled", r["sens"] > 0) if cfg else True)
                grid.addWidget(en_cb, row, 1)
                self._reaction_cbs[key] = en_cb

                sens_val = getattr(cfg, f"{key}_sensitivity", r["sens"]) if cfg else r["sens"]
                sens_w = QWidget()
                sw = QHBoxLayout(sens_w)
                sw.setContentsMargins(0, 0, 0, 0)
                sw.setSpacing(4)
                slider = QSlider(Qt.Orientation.Horizontal)
                slider.setRange(0, 100)
                slider.setValue(int(sens_val * 100))
                sens_lbl = QLabel(f"{slider.value()}%")
                sens_lbl.setFixedWidth(36)
                slider.valueChanged.connect(lambda v, lbl=sens_lbl: lbl.setText(f"{v}%"))
                sw.addWidget(slider)
                sw.addWidget(sens_lbl)
                grid.addWidget(sens_w, row, 2)
                self._reaction_sliders[key] = slider
                self._reaction_lbls[key]    = sens_lbl

                cool_val = getattr(cfg, f"{key}_cooldown", r["cooldown"]) if cfg else r["cooldown"]
                spin = QDoubleSpinBox()
                spin.setRange(3.0, 120.0)
                spin.setSingleStep(5.0)
                spin.setDecimals(0)
                spin.setValue(cool_val)
                spin.setFixedWidth(70)
                grid.addWidget(spin, row, 3)
                self._reaction_spins[key] = spin

            dl.addWidget(grid_w)

            sens_hint = QLabel(
                "Low (0–30%): loud or sustained reactions only — fewest false positives.\n"
                "Medium (40–70%): catches most reactions, occasional false triggers.\n"
                "High (80–100%): picks up quieter and shorter reactions — most sensitive to background noise."
            )
            sens_hint.setStyleSheet(f"color: {t.text_faint}; font-size: 10px;")
            dl.addWidget(sens_hint)

            root.addWidget(detect_box)

            # ── Sources ────────────────────────────────────────────────────
            src_box = QGroupBox("Audio Sources")
            src_l   = QGridLayout(src_box)
            src_l.setSpacing(10)
            src_l.setColumnStretch(1, 1)

            init_mic_en  = cfg.mic_enabled  if cfg else True
            init_chat_en = cfg.chat_enabled if cfg else True
            init_mic_dev = (cfg.mic_device  if cfg and cfg.mic_device
                            else _recorder_mic_device())
            init_chat_dev = cfg.chat_device if cfg else ""

            self._mic_cb = QCheckBox("Microphone")
            self._mic_cb.setChecked(init_mic_en)
            self._mic_combo = _build_combo(_input_devices(), init_mic_dev)
            self._mic_combo.setEnabled(init_mic_en)
            self._mic_cb.toggled.connect(self._mic_combo.setEnabled)
            src_l.addWidget(self._mic_cb,    0, 0)
            src_l.addWidget(self._mic_combo, 0, 1)

            self._chat_cb = QCheckBox("Chat audio")
            self._chat_cb.setChecked(init_chat_en)
            self._chat_combo = _build_combo(_monitor_devices(), init_chat_dev)
            self._chat_combo.setEnabled(init_chat_en)
            self._chat_cb.toggled.connect(self._chat_combo.setEnabled)
            src_l.addWidget(self._chat_cb,    1, 0)
            src_l.addWidget(self._chat_combo, 1, 1)

            chat_rec  = _recorder_chat_label()
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

            # Auto-save on any change
            self._enabled_cb.toggled.connect(lambda _: self.settings_changed.emit())
            self._mic_cb.toggled.connect(lambda _: self.settings_changed.emit())
            self._mic_combo.currentIndexChanged.connect(lambda _: self.settings_changed.emit())
            self._chat_cb.toggled.connect(lambda _: self.settings_changed.emit())
            self._chat_combo.currentIndexChanged.connect(lambda _: self.settings_changed.emit())
            for key in self._reaction_cbs:
                self._reaction_cbs[key].toggled.connect(lambda _: self.settings_changed.emit())
                self._reaction_sliders[key].sliderReleased.connect(self.settings_changed.emit)
                self._reaction_spins[key].editingFinished.connect(self.settings_changed.emit)

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
            cfg = getattr(self._config, "reactions", None)
            if cfg is not None:
                cfg.enabled      = self._enabled_cb.isChecked()
                cfg.mic_enabled  = self._mic_cb.isChecked()
                cfg.mic_device   = self._mic_combo.currentData() or ""
                cfg.chat_enabled = self._chat_cb.isChecked()
                cfg.chat_device  = self._chat_combo.currentData() or ""
                for key in self._reaction_cbs:
                    setattr(cfg, f"{key}_enabled",      self._reaction_cbs[key].isChecked())
                    setattr(cfg, f"{key}_sensitivity",   self._reaction_sliders[key].value() / 100.0)
                    setattr(cfg, f"{key}_cooldown",      float(self._reaction_spins[key].value()))
            self._config.save()

    return _Widget(config, parent)
