# SPDX-License-Identifier: GPL-3.0-or-later
"""
User drop-in plugin loader (cross-platform).

The built-in game/audio-trigger plugins are baked into the app at build time
(in the frozen Windows build they live inside the exe bundle, not on disk), so
they can't be extended by dropping a file next to them. This module loads extra
plugins from a writable folder outside the bundle instead:

    <CONFIG_DIR>/plugins/games/      -> game plugins
    <CONFIG_DIR>/plugins/audio/      -> audio-trigger plugins

CONFIG_DIR is platform-aware (see config.py) and is where config.json lives, so
this works identically on Windows (%APPDATA%\\autoclip) and Linux
(~/.config/autoclip). Files are imported by path, so it works in both the frozen
and source builds.
"""
import importlib.util
import logging
import sys
from pathlib import Path

from autoclip.core.config import CONFIG_DIR

logger = logging.getLogger(__name__)

_README = """\
AutoClip user plugins
=====================
Drop a Python file into the matching subfolder to add a plugin WITHOUT
rebuilding AutoClip. Plugins here are loaded at startup, after the built-ins.

  games/   game plugins         (subclass autoclip.games.base.GamePlugin)
  audio/   audio-trigger plugins (subclass autoclip.audio_triggers.base.AudioTriggerPlugin)

Example - games/my_game.py:

    from autoclip.games.base import GamePlugin

    class MyGame(GamePlugin):
        NAME = "My Game"
        PROCESS_NAMES = ["mygame"]
        # ... see the built-in cs2 plugin for the full interface

Notes:
  - Files whose names start with "_" are ignored.
  - A bad plugin is logged and skipped, never fatal.
  - Restart AutoClip after adding or editing a plugin.
"""


def user_plugins_root() -> Path:
    return CONFIG_DIR / "plugins"


def ensure_user_plugin_dirs():
    """Create the plugins/{games,audio} folders + a README on first run."""
    root = user_plugins_root()
    try:
        (root / "games").mkdir(parents=True, exist_ok=True)
        (root / "audio").mkdir(parents=True, exist_ok=True)
        readme = root / "README.txt"
        if not readme.exists():
            readme.write_text(_README, encoding="utf-8")
    except OSError as e:
        logger.warning(f"Could not create user plugins folder {root}: {e}")


def iter_user_plugin_modules(subdir: str):
    """Import every top-level .py in <CONFIG_DIR>/plugins/<subdir> and yield the modules.

    Each module is imported by file path (independent of the frozen importer), so
    the registry can scan it for plugin subclasses exactly like a built-in module.
    """
    ensure_user_plugin_dirs()
    folder = user_plugins_root() / subdir
    if not folder.is_dir():
        return
    for path in sorted(folder.glob("*.py")):
        if path.name.startswith("_"):
            continue
        mod_name = f"autoclip_user_plugins.{subdir}.{path.stem}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, path)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod          # so dataclasses/pickling resolve it
            spec.loader.exec_module(mod)
            logger.info(f"Loaded user plugin: {subdir}/{path.name}")
            yield mod
        except Exception as e:
            logger.warning(f"Failed to load user plugin {subdir}/{path.name}: {e}")
