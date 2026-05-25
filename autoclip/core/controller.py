# SPDX-License-Identifier: GPL-3.0-or-later
import logging
import threading
import time
import subprocess
from typing import Callable, Optional

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
        codec = getattr(self.config, "gpu_recorder_codec", "")
        if not codec:
            from autoclip.core.hardware_detection import detect_best_codec
            codec = detect_best_codec()
            self.config.gpu_recorder_codec = codec
            self.config.save()
            logger.info(f"Auto-detected codec: {codec}")

    def start(self):
        self._running = True
        self._ensure_codec_detected()  # before recorder starts
        self.hotkey_manager.start()
        for plugin in self._audio_trigger_plugins:
            plugin.start()
        threading.Thread(target=self._game_watcher, daemon=True).start()
        self._emit_status("running")
        logger.info("AutoClip started")

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

    def _game_watcher(self):
        while self._running:
            detected_cls = self._detect_game()
            detected_name = detected_cls.NAME if detected_cls else None
            if detected_name != self._current_game:
                self._on_game_change(detected_cls)
            time.sleep(5)

    def _detect_game(self):
        """Return plugin class for the first detected running game, or None."""
        from autoclip.games.registry import detect_running_game
        try:
            result = subprocess.run(
                ["ps", "-eo", "comm="], capture_output=True, text=True)
            procs = result.stdout.splitlines()
            return detect_running_game(procs)
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
                self.recorder.start(new_name)
        else:
            self._emit_event("game_closed")
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
            tracks = self.config.audio_tracks or []
            updated = False
            for track in tracks:
                if isinstance(track, dict) and track.get("track_type") == "game":
                    if track.get("device") != device:
                        track["device"] = device
                        updated = True
                    break
            if updated:
                self.config.save()
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
