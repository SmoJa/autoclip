# SPDX-License-Identifier: GPL-3.0-or-later
"""
Voice phrase trigger using Vosk offline speech recognition.

Monitors microphone and/or chat audio for configurable spoken phrases and
fires a clip save when one is detected.  Uses Vosk's grammar mode so only
the configured phrases are recognised — all other speech is ignored, keeping
CPU usage low.

Requirements:
  pip install vosk sounddevice
  Model (~40 MB) is downloaded automatically on first use via Settings.
"""

import json
import threading
import logging
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .base import AudioTriggerPlugin

logger = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────

_MODELS_DIR = Path.home() / ".cache" / "autoclip" / "models"
_MODEL_DIR  = _MODELS_DIR / "vosk-model-small-en-us"
_MODEL_URL  = "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"

# ── Audio constants ───────────────────────────────────────────────────────────

SAMPLE_RATE = 16000   # Vosk small-en-us runs at 16 kHz
BLOCK_SIZE  = 4000    # 0.25 s chunks — low latency without hammering the CPU

# ── Config ────────────────────────────────────────────────────────────────────

@dataclass
class VoiceConfig:
    enabled:      bool  = False
    phrases:      str   = "clip that, save that, clip it"  # comma-separated
    cooldown:     float = 10.0
    mic_enabled:  bool  = True
    mic_device:   str   = ""
    chat_enabled: bool  = True
    chat_device:  str   = ""


# ── Plugin ────────────────────────────────────────────────────────────────────

class VoicePlugin(AudioTriggerPlugin):
    NAME              = "Voice"
    TRIGGER_NAME      = "voice"
    TRIGGER_DISPLAY   = "Voice"
    TRIGGER_LOG_STYLE = ("Voice", "#44ddff", "#001a22", True)

    def __init__(self, config: Any, on_trigger: Callable[[], None]):
        super().__init__(config, on_trigger)
        self._running      = False
        self._threads:     list[threading.Thread] = []
        self._last_fired   = 0.0
        self._fire_lock    = threading.Lock()

    def _cfg(self) -> VoiceConfig:
        return getattr(self.config, "voice", VoiceConfig())

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def start(self):
        cfg = self._cfg()
        if not cfg.enabled or self._running:
            return

        for pkg in ("vosk", "sounddevice"):
            try:
                __import__(pkg)
            except ImportError:
                logger.error(f"'Voice' trigger requires '{pkg}' — pip install {pkg}")
                return

        if not _MODEL_DIR.exists():
            logger.warning("Vosk model not found — 'Voice' detection disabled. "
                           "Download it in Settings → Audio Triggers → Clip That.")
            return

        try:
            import vosk
            model = vosk.Model(str(_MODEL_DIR))
        except Exception as e:
            logger.error(f"Failed to load Vosk model: {e}")
            return

        sources = self._resolve_sources(cfg)
        if not sources:
            logger.warning("'Voice' detection: no audio sources enabled")
            return

        self._running = True
        self._threads = []
        for device, label in sources:
            t = threading.Thread(
                target=self._run_source,
                args=(device, label, cfg, model),
                daemon=True,
            )
            self._threads.append(t)
            t.start()

        phrase_list = [p.strip() for p in cfg.phrases.split(",") if p.strip()]
        logger.info(f"'Voice' detection started — sources: {[l for _, l in sources]} "
                    f"phrases: {phrase_list}")

    def stop(self):
        self._running = False

    # ── Source resolution ──────────────────────────────────────────────────────

    def _resolve_sources(self, cfg: VoiceConfig) -> list[tuple]:
        import sounddevice as sd
        sources = []
        if cfg.mic_enabled:
            device = self._find_device(sd, cfg.mic_device, monitor=False)
            sources.append((device, "mic"))
            logger.info(f"'Voice' mic source: {device!r} ({cfg.mic_device or 'default'})")
        if cfg.chat_enabled:
            device = self._find_device(sd, cfg.chat_device, monitor=True)
            sources.append((device, "chat"))
            logger.info(f"'Voice' chat source: {device!r} ({cfg.chat_device or 'auto-monitor'})")
        return sources

    @staticmethod
    def _find_device(sd, name: str, monitor: bool) -> Optional[int]:
        if name:
            for i, d in enumerate(sd.query_devices()):
                if name in d["name"] and d["max_input_channels"] > 0:
                    return i
            logger.warning(f"Audio device '{name}' not found — using default")
            return None
        if monitor:
            for i, d in enumerate(sd.query_devices()):
                if "monitor" in d["name"].lower() and d["max_input_channels"] > 0:
                    return i
            logger.warning("No monitor device found for chat audio")
        return None

    # ── Detection loop ─────────────────────────────────────────────────────────

    def _run_source(self, device: Optional[int], label: str,
                    cfg: VoiceConfig, model):
        import sounddevice as sd
        import vosk

        phrases = [p.strip().lower() for p in cfg.phrases.split(",") if p.strip()]
        grammar = json.dumps(phrases + ["[unk]"])
        rec     = vosk.KaldiRecognizer(model, SAMPLE_RATE)
        rec.SetGrammar(grammar)

        try:
            with sd.InputStream(device=device, channels=1, samplerate=SAMPLE_RATE,
                                blocksize=BLOCK_SIZE, dtype="int16") as stream:
                while self._running:
                    block, _ = stream.read(BLOCK_SIZE)
                    if rec.AcceptWaveform(block.tobytes()):
                        result = json.loads(rec.Result())
                        text   = result.get("text", "").lower().strip()
                        if text and text != "[unk]":
                            if any(phrase in text for phrase in phrases):
                                self._maybe_fire(label, text, cfg)
        except Exception as e:
            logger.error(f"'Voice' capture error ({label}): {e}")

    def _maybe_fire(self, label: str, text: str, cfg: VoiceConfig):
        fired = False
        with self._fire_lock:
            now = time.time()
            if now - self._last_fired > cfg.cooldown:
                self._last_fired = now
                fired = True
        if fired:
            logger.info(f"'Voice' [{label}] heard: '{text}'")
            self.on_trigger()

    def get_config_class(self):
        return VoiceConfig

    def get_config_widget(self, config: Any, parent=None):
        return VoiceWidget(config, parent)


# ── Settings widget ───────────────────────────────────────────────────────────

def VoiceWidget(config: Any, parent=None):
    from PyQt6.QtWidgets import (
        QWidget, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox,
        QDoubleSpinBox, QGroupBox, QGridLayout, QPushButton, QLineEdit,
    )
    from PyQt6.QtCore import QThread, pyqtSignal
    from autoclip.gui import theme as _theme
    from autoclip.gui.widgets import NoScrollComboBox

    t = _theme.current

    # ── Helpers (shared with laughter widget) ──────────────────────────────
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
        monitors = [("Auto-detect monitor", "")]
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

    def _build_combo(items, current) -> NoScrollComboBox:
        combo = NoScrollComboBox()
        for label, value in items:
            combo.addItem(label, userData=value)
        for i in range(combo.count()):
            if combo.itemData(i) == current:
                combo.setCurrentIndex(i)
                break
        return combo

    # ── pip installer ─────────────────────────────────────────────────────
    class _PipInstaller(QThread):
        done = pyqtSignal(bool, str)

        def run(self):
            import subprocess, sys
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "vosk"],
                    capture_output=True, text=True
                )
                if result.returncode == 0:
                    self.done.emit(True, "")
                else:
                    self.done.emit(False, result.stderr.strip().splitlines()[-1] if result.stderr else "pip failed")
            except Exception as e:
                self.done.emit(False, str(e))

    # ── Model downloader ───────────────────────────────────────────────────
    class _Downloader(QThread):
        progress = pyqtSignal(str)
        done     = pyqtSignal(bool, str)

        def run(self):
            import urllib.request
            tmp_zip = _MODELS_DIR / "vosk-small-en.zip"
            try:
                _MODELS_DIR.mkdir(parents=True, exist_ok=True)

                def _hook(count, block_size, total):
                    if total > 0:
                        pct = min(100, int(count * block_size * 100 / total))
                        self.progress.emit(f"Downloading… {pct}%")

                self.progress.emit("Downloading…")
                urllib.request.urlretrieve(_MODEL_URL, str(tmp_zip), reporthook=_hook)

                self.progress.emit("Extracting…")
                with zipfile.ZipFile(tmp_zip) as zf:
                    zf.extractall(_MODELS_DIR)

                # Rename extracted folder (versioned) to the expected name
                for child in _MODELS_DIR.iterdir():
                    if child.is_dir() and child.name.startswith("vosk-model-small-en-us"):
                        if child != _MODEL_DIR:
                            child.rename(_MODEL_DIR)
                        break

                tmp_zip.unlink(missing_ok=True)
                self.done.emit(True, "")
            except Exception as e:
                tmp_zip.unlink(missing_ok=True)
                self.done.emit(False, str(e))

    # ── Main widget ────────────────────────────────────────────────────────
    class _Widget(QWidget):
        settings_changed = pyqtSignal()

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
                "Listens for spoken phrases on your microphone and/or chat audio. "
                "When a configured phrase is detected, a clip is saved. "
                "All recognition runs locally using Vosk — no internet connection required during use."
            )
            desc.setWordWrap(True)
            desc.setStyleSheet(f"color: {t.text_dim}; font-size: 11px;")
            root.addWidget(desc)

            # ── Model status ───────────────────────────────────────────────
            setup_box = QGroupBox("Setup")
            sl = QVBoxLayout(setup_box)
            sl.setSpacing(8)

            self._pip_installer = None
            vosk_row = QWidget()
            vr = QHBoxLayout(vosk_row)
            vr.setContentsMargins(0, 0, 0, 0)
            vr.setSpacing(8)
            self._vosk_lbl     = QLabel()
            self._pip_btn      = QPushButton("Install vosk")
            self._pip_status   = QLabel("")
            self._pip_status.setStyleSheet(f"color: {t.text_dim}; font-size: 10px;")
            vr.addWidget(self._vosk_lbl)
            vr.addWidget(self._pip_btn)
            vr.addWidget(self._pip_status)
            vr.addStretch()
            sl.addWidget(vosk_row)

            model_row = QWidget()
            mr = QHBoxLayout(model_row)
            mr.setContentsMargins(0, 0, 0, 0)
            mr.setSpacing(8)
            self._model_lbl    = QLabel()
            self._dl_btn       = QPushButton("Download model (~40 MB)")
            self._status_lbl   = QLabel("")
            self._status_lbl.setStyleSheet(f"color: {t.text_dim}; font-size: 10px;")
            mr.addWidget(self._model_lbl)
            mr.addWidget(self._dl_btn)
            mr.addWidget(self._status_lbl)
            mr.addStretch()
            sl.addWidget(model_row)
            root.addWidget(setup_box)
            self._refresh_model_status()
            self._pip_btn.clicked.connect(self._install_vosk)
            self._dl_btn.clicked.connect(self._start_download)

            # ── Detection settings ─────────────────────────────────────────
            detect_box = QGroupBox("Detection")
            dl = QGridLayout(detect_box)
            dl.setSpacing(12)

            cfg           = getattr(self._config, "voice", None)
            init_enabled  = cfg.enabled   if cfg else False
            init_phrases  = cfg.phrases   if cfg else "clip that, save that, clip it"
            init_cool     = cfg.cooldown  if cfg else 10.0
            init_mic_en   = cfg.mic_enabled  if cfg else True
            init_chat_en  = cfg.chat_enabled if cfg else True
            init_mic_dev  = cfg.mic_device   if cfg else ""
            init_chat_dev = cfg.chat_device  if cfg else ""

            self._enabled_cb = QCheckBox("Enable voice trigger detection")
            self._enabled_cb.setChecked(init_enabled)
            dl.addWidget(self._enabled_cb, 0, 0, 1, 2)

            dl.addWidget(QLabel("Trigger phrases"), 1, 0)
            self._phrases_edit = QLineEdit(init_phrases)
            self._phrases_edit.setPlaceholderText("comma-separated phrases")
            dl.addWidget(self._phrases_edit, 1, 1)

            phrase_hint = QLabel("Comma-separated. Vosk matches these exactly — keep them short and distinct.")
            phrase_hint.setStyleSheet(f"color: {t.text_faint}; font-size: 10px;")
            dl.addWidget(phrase_hint, 2, 0, 1, 2)

            dl.addWidget(QLabel("Cooldown (s)"), 3, 0)
            self._cooldown_spin = QDoubleSpinBox()
            self._cooldown_spin.setRange(3.0, 120.0)
            self._cooldown_spin.setSingleStep(1.0)
            self._cooldown_spin.setDecimals(0)
            self._cooldown_spin.setValue(init_cool)
            self._cooldown_spin.setFixedWidth(80)
            dl.addWidget(self._cooldown_spin, 3, 1)

            root.addWidget(detect_box)

            # ── Sources ────────────────────────────────────────────────────
            src_box = QGroupBox("Audio Sources")
            src_l   = QGridLayout(src_box)
            src_l.setSpacing(10)
            src_l.setColumnStretch(1, 1)

            self._mic_cb    = QCheckBox("Microphone")
            self._mic_cb.setChecked(init_mic_en)
            self._mic_combo = _build_combo(_input_devices(), init_mic_dev)
            self._mic_combo.setEnabled(init_mic_en)
            self._mic_cb.toggled.connect(self._mic_combo.setEnabled)
            src_l.addWidget(self._mic_cb,    0, 0)
            src_l.addWidget(self._mic_combo, 0, 1)

            self._chat_cb    = QCheckBox("Chat audio")
            self._chat_cb.setChecked(init_chat_en)
            self._chat_combo = _build_combo(_monitor_devices(), init_chat_dev)
            self._chat_combo.setEnabled(init_chat_en)
            self._chat_cb.toggled.connect(self._chat_combo.setEnabled)
            src_l.addWidget(self._chat_cb,    1, 0)
            src_l.addWidget(self._chat_combo, 1, 1)

            hint = QLabel(
                "Microphone captures your own voice. "
                "Chat audio uses a monitor device to hear what teammates say."
            )
            hint.setWordWrap(True)
            hint.setStyleSheet(f"color: {t.text_faint}; font-size: 10px;")
            src_l.addWidget(hint, 2, 0, 1, 2)

            root.addWidget(src_box)

            # Auto-save on any setting change
            self._enabled_cb.toggled.connect(lambda _: self.settings_changed.emit())
            self._phrases_edit.editingFinished.connect(self.settings_changed.emit)
            self._cooldown_spin.editingFinished.connect(self.settings_changed.emit)
            self._mic_cb.toggled.connect(lambda _: self.settings_changed.emit())
            self._mic_combo.currentIndexChanged.connect(lambda _: self.settings_changed.emit())
            self._chat_cb.toggled.connect(lambda _: self.settings_changed.emit())
            self._chat_combo.currentIndexChanged.connect(lambda _: self.settings_changed.emit())

        def _refresh_model_status(self):
            try:
                import vosk  # noqa
                vosk_ok = True
            except ImportError:
                vosk_ok = False

            if vosk_ok:
                self._vosk_lbl.setText("vosk installed")
                self._vosk_lbl.setStyleSheet("color: #66cc88;")
                self._pip_btn.setVisible(False)
            else:
                self._vosk_lbl.setText("vosk not installed")
                self._vosk_lbl.setStyleSheet("color: #ff8866;")
                self._pip_btn.setVisible(True)

            if _MODEL_DIR.exists():
                self._model_lbl.setText("Model ready")
                self._model_lbl.setStyleSheet("color: #66cc88;")
                self._dl_btn.setVisible(False)
                self._status_lbl.setVisible(False)
            else:
                self._model_lbl.setText("Model not downloaded")
                self._model_lbl.setStyleSheet(f"color: {t.text_dim};")
                self._dl_btn.setVisible(True)

        def _install_vosk(self):
            if self._pip_installer and self._pip_installer.isRunning():
                return
            self._pip_btn.setEnabled(False)
            self._pip_status.setText("Installing…")
            self._pip_installer = _PipInstaller()
            self._pip_installer.done.connect(self._on_pip_done)
            self._pip_installer.start()

        def _on_pip_done(self, ok: bool, err: str):
            self._pip_btn.setEnabled(True)
            if ok:
                self._pip_status.setText("")
                self._refresh_model_status()
            else:
                self._pip_status.setText(f"Failed: {err}")
                self._pip_status.setStyleSheet("color: #ff6666;")

        def _start_download(self):
            if self._downloader and self._downloader.isRunning():
                return
            self._dl_btn.setEnabled(False)
            self._status_lbl.setText("Starting…")
            self._status_lbl.setVisible(True)
            self._downloader = _Downloader()
            self._downloader.progress.connect(self._status_lbl.setText)
            self._downloader.done.connect(self._on_dl_done)
            self._downloader.start()

        def _on_dl_done(self, ok: bool, err: str):
            self._dl_btn.setEnabled(True)
            if ok:
                self._status_lbl.setText("")
                self._refresh_model_status()
            else:
                self._status_lbl.setText(f"Failed: {err}")
                self._status_lbl.setStyleSheet("color: #ff6666;")

        def save(self):
            cfg = getattr(self._config, "voice", None)
            if cfg is not None:
                cfg.enabled      = self._enabled_cb.isChecked()
                cfg.phrases      = self._phrases_edit.text()
                cfg.cooldown     = float(self._cooldown_spin.value())
                cfg.mic_enabled  = self._mic_cb.isChecked()
                cfg.mic_device   = self._mic_combo.currentData() or ""
                cfg.chat_enabled = self._chat_cb.isChecked()
                cfg.chat_device  = self._chat_combo.currentData() or ""
            self._config.save()

    return _Widget(config, parent)
