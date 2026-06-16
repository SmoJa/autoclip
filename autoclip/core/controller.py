# SPDX-License-Identifier: GPL-3.0-or-later
import logging
import threading
import time
import subprocess
import sys
from typing import Callable, Optional


def _list_processes_win32():
    """Return list of process image names using ctypes TH32 snapshot.
    Avoids subprocess entirely to sidestep Python 3.11.9 Windows AV bug."""
    import ctypes, ctypes.wintypes
    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize",              ctypes.wintypes.DWORD),
            ("cntUsage",            ctypes.wintypes.DWORD),
            ("th32ProcessID",       ctypes.wintypes.DWORD),
            ("th32DefaultHeapID",   ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID",        ctypes.wintypes.DWORD),
            ("cntThreads",          ctypes.wintypes.DWORD),
            ("th32ParentProcessID", ctypes.wintypes.DWORD),
            ("pcPriClassBase",      ctypes.c_long),
            ("dwFlags",             ctypes.wintypes.DWORD),
            ("szExeFile",           ctypes.c_char * 260),
        ]

    k32 = ctypes.windll.kernel32
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE:
        return []
    try:
        pe = PROCESSENTRY32()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
        names = []
        if k32.Process32First(snap, ctypes.byref(pe)):
            while True:
                names.append(pe.szExeFile.decode("utf-8", errors="replace"))
                if not k32.Process32Next(snap, ctypes.byref(pe)):
                    break
        return names
    finally:
        k32.CloseHandle(snap)

from .config import Config
from .recorder import RecorderManager
from .audio_mix import mix_audio_tracks, DeferredMixQueue
from .clip_trigger import HotkeyManager, ClipTrigger

logger = logging.getLogger(__name__)


class AppController:
    def __init__(self, config: Config):
        self.config = config
        self._running      = False
        self._current_game: Optional[str] = None
        self._active_plugin = None   # GamePlugin instance for current game

        self.recorder      = RecorderManager(config)
        self.clip_trigger  = ClipTrigger(config, self.recorder)
        self.hotkey_manager = HotkeyManager(
            config, lambda: self.clip_trigger.trigger("manual_hotkey"))
        self._audio_trigger_plugins = []

        self.on_status_change: Optional[Callable[[str], None]] = None
        self.on_event:         Optional[Callable[[str], None]] = None
        self.on_clip_saved:    Optional[Callable[[str], None]] = None
        self.on_status:        Optional[Callable[[str], None]] = None

        self._mix_queue = DeferredMixQueue(on_status=self._emit_status)

        self.recorder.on_status_change    = self._emit_status
        self.clip_trigger.on_clip_saved   = self._on_clip_saved

        # Load all game plugins and merge their metadata tables
        self._load_plugins()
        self._load_audio_trigger_plugins()

    def _load_plugins(self):
        """Discover game plugins and merge their metadata vocabularies."""
        from autoclip.games.registry import (
            get_all_plugins, build_global_metadata_tables
        )
        self._plugin_classes = get_all_plugins()
        tables = build_global_metadata_tables()
        # Patch metadata module with merged tables from all plugins
        try:
            import autoclip.core.metadata as _meta
            _meta.MODE_TO_SHORT.update(tables["mode_abbrevs"])
            _meta.SHORT_TO_MODE.update({v: k for k, v in tables["mode_abbrevs"].items()})
            _meta.TRIGGER_TO_SHORT.update(tables["trigger_abbrevs"])
            _meta.SHORT_TO_TRIGGER.update({v: k for k, v in tables["trigger_abbrevs"].items()})
            _meta.TRIGGER_DISPLAY.update(tables["trigger_display"])
            _meta.MODE_DISPLAY.update(tables["mode_display"])
            if tables["map_prefixes"]:
                _meta.MAP_PREFIXES = tuple(
                    sorted(set(list(_meta.MAP_PREFIXES) + list(tables["map_prefixes"])))
                )
        except Exception as e:
            logger.warning(f"Metadata table merge failed: {e}")
        # Expose merged log styles for the UI
        self.trigger_log_style = tables.get("trigger_log_style", {})

        # Hydrate per-game configs from plugin definitions + saved data
        raw = getattr(self.config, "_raw_game_data", {})
        for cls in self._plugin_classes:
            cfg_cls = cls.get_config_class(cls)
            if cfg_cls is not None:
                game_raw = raw.get(cls.NAME.lower(), {})
                self.config.inject_game_config(cls.NAME, cfg_cls, game_raw)
                # Trigger __post_init__ if needed (e.g. enabled_modes)
                game_cfg = self.config._game_configs[cls.NAME]
                if hasattr(game_cfg, "__post_init__"):
                    if getattr(game_cfg, "enabled_modes", True) is None:
                        game_cfg.__post_init__()

        logger.info(f"Loaded {len(self._plugin_classes)} game plugin(s): "
                    f"{[p.NAME for p in self._plugin_classes]}")

    def _load_audio_trigger_plugins(self):
        """Discover audio trigger plugins and hydrate their configs."""
        from autoclip.audio_triggers.registry import get_all_plugins
        raw = getattr(self.config, "_raw_audio_trigger_data", {})
        for cls in get_all_plugins():
            cfg_cls = cls.get_config_class(cls)
            if cfg_cls is not None:
                raw_data = raw.get(cls.NAME.lower(), {})
                self.config.inject_audio_trigger_config(cls.NAME, cfg_cls, raw_data)
            trigger_name = cls.TRIGGER_NAME
            def _make_audio_callback(default_n):
                def _cb(name=None):
                    n   = name if name else default_n
                    ctx = (self._active_plugin.get_current_context()
                           if self._active_plugin else "")
                    self.clip_trigger.trigger(n + ctx)
                return _cb
            plugin = cls(self.config, _make_audio_callback(trigger_name))
            self._audio_trigger_plugins.append(plugin)
            if cls.TRIGGER_NAME and cls.TRIGGER_LOG_STYLE:
                self.trigger_log_style[cls.TRIGGER_NAME] = cls.TRIGGER_LOG_STYLE
            for tname, tstyle in cls.TRIGGER_LOG_STYLES.items():
                self.trigger_log_style[tname] = tstyle
        logger.info(f"Loaded {len(self._audio_trigger_plugins)} audio trigger plugin(s): "
                    f"{[p.NAME for p in self._audio_trigger_plugins]}")

    def _ensure_codec_detected(self):
        import sys
        codec = getattr(self.config, "gpu_recorder_codec", "")
        if not codec:
            from autoclip.core.hardware_detection import detect_best_codec
            codec = detect_best_codec()
            # The Windows/libobs recorder takes a bare codec (hevc|h264|av1) — HDR is
            # handled via the encoder profile, not a codec suffix. Normalize.
            if sys.platform == "win32":
                cl = codec.lower()
                codec = "av1" if cl.startswith("av1") else "h264" if cl == "h264" else "hevc"
            self.config.gpu_recorder_codec = codec
            self.config.save()
            logger.info(f"Auto-detected codec: {codec}")

        # First-run hardware-aware encoding preset (Windows/NVENC only).
        if sys.platform == "win32" and not getattr(self.config, "encoding_initialized", False):
            from autoclip.core.hardware_detection import detect_gpu_vendor
            from autoclip.core.config import apply_encoding_preset
            vendor = detect_gpu_vendor()
            # NVENC (NVIDIA) handles CQP cheaply -> Balanced. Other GPUs fall back to
            # CPU x264, where a fast CBR preset spares the processor -> Performance.
            name = "balanced" if vendor == "nvidia" else "performance"
            apply_encoding_preset(self.config, name)
            self.config.encoding_initialized = True
            self.config.save()
            logger.info(f"Hardware-aware encoding preset: {name} (GPU={vendor})")

    def start(self):
        self._running = True
        self._ensure_codec_detected()  # before recorder starts
        self.hotkey_manager.start()
        for plugin in self._audio_trigger_plugins:
            plugin.start()
        if self.config.record_without_game and self.config.recording_enabled:
            self.recorder.start("General")
        if sys.platform != "win32":
            # On Windows, background threads AV after Qt/mpv load (heap corruption).
            # Game detection is driven by a QTimer in the main thread instead.
            threading.Thread(target=self._game_watcher, daemon=True).start()
        self._emit_status("running")
        logger.info(f"AutoClip started (recording_enabled={self.config.recording_enabled})")

    def stop(self):
        self._running = False
        if self._active_plugin:
            self._active_plugin.stop()
            self._active_plugin = None
        self.hotkey_manager.stop()
        for plugin in self._audio_trigger_plugins:
            plugin.stop()
        self.recorder.stop()
        self._emit_status("stopped")

    def set_recording_enabled(self, enabled: bool):
        logger.info(f"Recording {'enabled' if enabled else 'disabled'}")
        self.config.recording_enabled = enabled
        if not enabled:
            self.recorder.stop()
            self._emit_status("recording paused")
        else:
            if self._current_game:
                self.recorder.start(self._current_game)
            self._emit_status("running")
        self.config.save()

    def restart_audio_trigger(self, name: str):
        """Stop and restart the named audio trigger plugin (picks up new config)."""
        for plugin in self._audio_trigger_plugins:
            if plugin.NAME == name:
                plugin.stop()
                time.sleep(0.3)
                plugin.start()
                break

    def install_game_integration(self, game_name: str) -> bool:
        """Install integration for named game (e.g. CS2 GSI config)."""
        from autoclip.games.registry import get_plugin_by_name
        cls = get_plugin_by_name(game_name)
        if cls:
            plugin = cls(self.config, self._on_game_event)
            return plugin.install_integration()
        return False

    def get_plugin_config_widget(self, game_name: str, parent=None):
        """Return settings widget for a game plugin, or None."""
        from autoclip.games.registry import get_plugin_by_name
        cls = get_plugin_by_name(game_name)
        if cls:
            plugin = cls(self.config, self._on_game_event)
            return plugin.get_config_widget(self.config, parent)
        return None

    # ── Game detection ────────────────────────────────────────────────────────

    def tick_game_detection(self):
        """Single game-detection poll — call from Qt main thread via QTimer on Windows."""
        if not self._running:
            return
        detected_cls = self._detect_game()
        detected_name = detected_cls.NAME if detected_cls else None
        if detected_name != self._current_game:
            self._on_game_change(detected_cls)

    def reapply_recorder_settings(self):
        """Restart the recorder so newly-saved settings (audio sources/tracks, codec,
        bitrate, fps...) take effect mid-session — the recorder reads config only when
        spawned. Runs on a background thread so saving settings doesn't block the GUI.
        No-op when not actively recording a game."""
        if not (self._running and self._current_game and self.config.recording_enabled):
            return
        game = self._current_game

        def _restart():
            try:
                self.recorder.stop()
                time.sleep(0.5)
                self.recorder.start(game)
                logger.info("Recorder restarted to apply new settings")
            except Exception as e:
                logger.error(f"Recorder restart after settings change failed: {e}")
        threading.Thread(target=_restart, daemon=True, name="recorder-reapply").start()

    def _game_watcher(self):
        while self._running:
            detected_cls = self._detect_game()
            detected_name = detected_cls.NAME if detected_cls else None
            if detected_name != self._current_game:
                self._on_game_change(detected_cls)
            time.sleep(5)

    def _detect_game(self):
        """Return plugin class for the first detected running game, or None."""
        import sys
        from autoclip.games.registry import detect_running_game, detect_running_game_by_cmdline
        try:
            if sys.platform == "win32":
                procs = _list_processes_win32()
                return detect_running_game(procs)
            else:
                result = subprocess.run(
                    ["ps", "-eo", "comm="], capture_output=True, text=True)
                procs = result.stdout.splitlines()
                game = detect_running_game(procs)
                if game:
                    return game
                # Fallback: full cmdlines for Wine/Proton games whose comm is generic
                result2 = subprocess.run(
                    ["ps", "-eo", "args="], capture_output=True, text=True)
                return detect_running_game_by_cmdline(result2.stdout.splitlines())
        except Exception:
            return None

    def _on_game_change(self, new_plugin_cls):
        # Flush deferred mix queue when leaving a game
        if new_plugin_cls is None and self._mix_queue.pending_count > 0:
            logger.info("Game closed — flushing deferred audio mix queue")
            self._mix_queue.flush()

        # Stop previous plugin
        if self._active_plugin:
            self._active_plugin.stop()
            self._active_plugin = None

        new_name = new_plugin_cls.NAME if new_plugin_cls else None
        self._current_game = new_name

        if new_plugin_cls:
            logger.info(f"Game detected: {new_name}")
            self._emit_event(f"game_started:{new_name}")

            # Instantiate and start plugin
            self._active_plugin = new_plugin_cls(self.config, self._on_game_event)
            self._active_plugin.start()

            # Resolve game audio
            threading.Thread(
                target=self._resolve_game_audio,
                args=(new_plugin_cls,),
                daemon=True
            ).start()

            if self.config.recording_enabled:
                # Stop any existing recording (e.g. general mode) before starting game
                self.recorder.stop()
                self.recorder.start(new_name)
        else:
            self._emit_event("game_closed")
            if self.config.record_without_game and self.config.recording_enabled:
                self.recorder.stop()
                self.recorder.start("General")
            else:
                self.recorder.stop()

    def _on_game_event(self, event: str):
        """Callback from active game plugin."""
        if not self.config.recording_enabled:
            return
        trigger = event.split("|")[0] if "|" in event else event
        self._emit_event(f"{self._current_game}:{trigger}")
        self.clip_trigger.trigger(event)

    def _resolve_game_audio(self, plugin_cls):
        try:
            from autoclip.core.audio import resolve_game_audio_node
            # Access resolve_audio_process without instantiating the plugin
            audio_proc = getattr(plugin_cls, "AUDIO_PROCESS_NAME", None)
            process = audio_proc or plugin_cls.PROCESS_NAMES[0]
            device = resolve_game_audio_node(process, self.config.gpu_recorder_path)
            if not device:
                return
            logger.info(f"Resolved game audio: {plugin_cls.NAME} → {device}")
            tracks = self.config.audio_tracks
            if tracks is None:
                tracks = []
                self.config.audio_tracks = tracks

            game_name = plugin_cls.NAME
            # Find existing track for this specific game (by game_name tag or label)
            target = None
            for t in tracks:
                if not isinstance(t, dict) or t.get("track_type") != "game":
                    continue
                if t.get("game_name") == game_name or t.get("label") == game_name:
                    target = t
                    break

            if target is None:
                # Add a new per-game track
                target = {
                    "label":      game_name,
                    "device":     device,
                    "enabled":    True,
                    "track_type": "game",
                    "game_name":  game_name,
                    "volume":     1.0,
                    "muted":      False,
                }
                tracks.append(target)
            else:
                target["device"]    = device
                target["label"]     = game_name
                target["game_name"] = game_name

            self.config.save()
            self._emit_event(f"game_audio_resolved:{game_name}")
        except Exception as e:
            logger.warning(f"Audio node resolution failed: {e}")

    # ── Audio mix ─────────────────────────────────────────────────────────────

    def _on_clip_saved(self, reason: str):
        mode = getattr(self.config, "audio_track_mode", "separate")
        if mode in ("mixed_immediate", "mixed_deferred"):
            self._handle_audio_mix(mode)
        if self.on_clip_saved:
            self.on_clip_saved(reason)

    def _handle_audio_mix(self, mode: str):
        from pathlib import Path as _P
        output_dir = _P(self.config.output_dir)
        all_clips = [
            p for ext in ("*.mkv", "*.mp4", "*.mov")
            for p in output_dir.rglob(ext)
        ]
        if not all_clips:
            return
        newest = max(all_clips, key=lambda p: p.stat().st_mtime)
        if time.time() - newest.stat().st_mtime > 60:
            return
        if mode == "mixed_immediate":
            threading.Thread(
                target=mix_audio_tracks, args=(newest,), daemon=True).start()
        elif mode == "mixed_deferred":
            self._mix_queue.enqueue(newest)

    def flush_mix_queue(self):
        self._mix_queue.flush()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _emit_status(self, s: str):
        if self.on_status_change:
            self.on_status_change(s)
        if self.on_status:
            self.on_status(s)

    def _emit_event(self, e: str):
        if self.on_event:
            self.on_event(e)
