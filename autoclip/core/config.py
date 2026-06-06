# SPDX-License-Identifier: GPL-3.0-or-later
import json
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import List

CONFIG_PATH = Path.home() / ".config" / "autoclip" / "config.json"


@dataclass
class Config:
    recording_enabled: bool = True
    record_without_game: bool = False   # keep gsr running even when no game detected
    output_dir: str = str(Path.home() / "Videos" / "Autoclip")
    clip_length_seconds: int = 30
    post_event_seconds: int = 7
    manual_hotkey: str = "<ctrl>+<shift>+s"

    # Recorder
    gpu_recorder_path: str = "/usr/local/bin/gpu-screen-recorder"
    exports_dir: str = ""
    monitor: str = "screen"
    capture_mode: str = "monitor"
    audio_source: str = "default"        # legacy
    audio_tracks: list = None
    audio_track_mode: str = "separate"   # "separate"|"mixed_immediate"|"mixed_deferred"
    separate_audio_tracks: bool = True   # legacy

    # Encoding
    gpu_recorder_codec: str = ""
    gpu_recorder_quality_mode: str = "cbr"
    gpu_recorder_bitrate_kbps: int = 30000
    gpu_recorder_quality_preset: str = "very_high"
    gpu_recorder_fps: int = 60
    gpu_recorder_color_range: str = "full"
    gpu_recorder_tune: str = "quality"

    # Appearance
    theme: str = "dark_orange"

    # GSI
    gsi_port: int = 3000

    # Per-game configs — stored as dicts, hydrated by each plugin
    # Access via config.cs2 (injected by controller after plugin load)
    _game_configs: dict = field(default_factory=dict)

    # Per-audio-trigger configs — hydrated by each plugin at startup
    # Access via config.laughter, config.sensevoice, etc.
    _audio_trigger_configs: dict = field(default_factory=dict)

    def _default_audio_tracks(self):
        return [
            {"label": "Game",  "device": "", "enabled": True,
             "track_type": "game",  "volume": 1.0, "muted": False},
            {"label": "Mic",   "device": "", "enabled": False,
             "track_type": "mic",   "volume": 1.0, "muted": False},
            {"label": "Chat",  "device": "", "enabled": False,
             "track_type": "chat",  "volume": 1.0, "muted": False},
        ]

    def save(self):
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if self.audio_tracks is None:
            self.audio_tracks = self._default_audio_tracks()
        data = {
            k: v for k, v in asdict(self).items()
            if not k.startswith("_")
        }
        # Save per-game configs under their plugin NAME key
        for name, cfg_obj in self._game_configs.items():
            data[name.lower()] = asdict(cfg_obj)
        # Save per-audio-trigger configs under "trigger_<name>" key
        for name, cfg_obj in self._audio_trigger_configs.items():
            data[f"trigger_{name.lower()}"] = asdict(cfg_obj)
        with open(CONFIG_PATH, "w") as f:
            json.dump(data, f, indent=2)

    @classmethod
    def load(cls) -> "Config":
        if not CONFIG_PATH.exists():
            cfg = cls()
            cfg.save()
            return cfg
        try:
            with open(CONFIG_PATH) as f:
                data = json.load(f)

            # Migrate old laughter_* flat keys → trigger_laughter namespace
            if "laughter_enabled" in data and "trigger_laughter" not in data:
                data["trigger_laughter"] = {
                    "enabled":      data.pop("laughter_enabled", False),
                    "audio_source": data.pop("laughter_audio_source", "mic"),
                    "sensitivity":  data.pop("laughter_sensitivity", 0.6),
                }
            else:
                for k in ("laughter_enabled", "laughter_audio_source", "laughter_sensitivity"):
                    data.pop(k, None)

            # Migrate trigger_voice → trigger_phrases (plugin renamed)
            if "trigger_voice" in data and "trigger_phrases" not in data:
                data["trigger_phrases"] = data.pop("trigger_voice")
            else:
                data.pop("trigger_voice", None)

            # Migrate old pre_event_seconds
            if "pre_event_seconds" in data and "clip_length_seconds" not in data:
                data["clip_length_seconds"] = (
                    data.pop("pre_event_seconds") + data.get("post_event_seconds", 7))
            else:
                data.pop("pre_event_seconds", None)

            # Split into known Config fields and plugin data
            known_fields = set(cls.__dataclass_fields__)
            core  = {k: v for k, v in data.items() if k in known_fields}
            extra = {k: v for k, v in data.items() if k not in known_fields}

            cfg = cls(**{k: v for k, v in core.items() if k in known_fields})
            if cfg.audio_tracks is None:
                cfg.audio_tracks = cfg._default_audio_tracks()

            # Stash raw plugin dicts so controllers can hydrate them
            cfg._raw_game_data = {
                k: v for k, v in extra.items() if not k.startswith("trigger_")
            }
            cfg._raw_audio_trigger_data = {
                k[8:]: v for k, v in extra.items() if k.startswith("trigger_")
            }
            return cfg
        except Exception:
            return cls()

    def inject_game_config(self, name: str, config_cls, raw_data: dict):
        """Called by controller after plugin load to hydrate per-game config."""
        kwargs = {k: v for k, v in raw_data.items()
                  if k in config_cls.__dataclass_fields__}
        obj = config_cls(**kwargs)
        if hasattr(obj, "__post_init__"):
            pass
        self._game_configs[name] = obj
        # Expose as attribute for convenience: config.cs2, config.valorant etc.
        setattr(self, name.lower(), obj)

    def inject_audio_trigger_config(self, name: str, config_cls, raw_data: dict):
        """Called by controller after plugin load to hydrate per-trigger config."""
        kwargs = {k: v for k, v in raw_data.items()
                  if k in config_cls.__dataclass_fields__}
        obj = config_cls(**kwargs)
        self._audio_trigger_configs[name] = obj
        # Expose as attribute: config.laughter, config.sensevoice, etc.
        setattr(self, name.lower(), obj)
