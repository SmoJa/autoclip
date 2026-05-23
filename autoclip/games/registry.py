# SPDX-License-Identifier: GPL-3.0-or-later
"""
Game plugin registry — auto-discovers all GamePlugin subclasses in games/.
"""

import importlib
import pkgutil
import logging
from pathlib import Path
from typing import Dict, List, Optional, Type

from .base import GamePlugin

logger = logging.getLogger(__name__)

# Maps process name → plugin class
_PROCESS_MAP: Dict[str, Type[GamePlugin]] = {}
# Maps game NAME → plugin class
_NAME_MAP: Dict[str, Type[GamePlugin]] = {}
# All loaded plugins
_ALL_PLUGINS: List[Type[GamePlugin]] = []


def _load_plugins():
    """Scan games/ directory and import all modules containing GamePlugin subclasses."""
    if _ALL_PLUGINS:
        return  # already loaded

    games_dir = Path(__file__).parent
    package   = __package__  # "autoclip.games"

    for finder, module_name, _ in pkgutil.iter_modules([str(games_dir)]):
        if module_name in ("base", "registry"):
            continue
        try:
            mod = importlib.import_module(f".{module_name}", package=package)
        except Exception as e:
            logger.warning(f"Failed to load game plugin {module_name}: {e}")
            continue

        for attr_name in dir(mod):
            obj = getattr(mod, attr_name)
            if (isinstance(obj, type) and
                    issubclass(obj, GamePlugin) and
                    obj is not GamePlugin and
                    obj.NAME):
                _register(obj)


def _register(cls: Type[GamePlugin]):
    if cls in _ALL_PLUGINS:
        return
    _ALL_PLUGINS.append(cls)
    _NAME_MAP[cls.NAME] = cls
    for proc in cls.PROCESS_NAMES:
        _PROCESS_MAP[proc.lower()] = cls
    logger.info(f"Registered game plugin: {cls.NAME} "
                f"(processes: {cls.PROCESS_NAMES})")


def get_all_plugins() -> List[Type[GamePlugin]]:
    _load_plugins()
    return list(_ALL_PLUGINS)


def get_plugin_for_process(process_name: str) -> Optional[Type[GamePlugin]]:
    _load_plugins()
    return _PROCESS_MAP.get(process_name.lower())


def get_plugin_by_name(name: str) -> Optional[Type[GamePlugin]]:
    _load_plugins()
    return _NAME_MAP.get(name)


def detect_running_game(process_list: List[str]) -> Optional[Type[GamePlugin]]:
    """Given a list of running process names, return the first matching plugin."""
    _load_plugins()
    lower = [p.lower() for p in process_list]
    for proc, cls in _PROCESS_MAP.items():
        if any(proc in p for p in lower):
            return cls
    return None


def build_global_metadata_tables() -> dict:
    """
    Merge all plugins' vocabulary into global lookup tables.
    Called once at startup — metadata.py uses these for encoding/decoding.
    """
    _load_plugins()
    tables = {
        "mode_abbrevs":      {},
        "trigger_abbrevs":   {},
        "mode_display":      {},
        "trigger_display":   {},
        "map_prefixes":      set(),
        "trigger_log_style": {},
    }
    for cls in _ALL_PLUGINS:
        tables["mode_abbrevs"].update(cls.MODE_ABBREVS)
        tables["trigger_abbrevs"].update(cls.TRIGGER_ABBREVS)
        tables["mode_display"].update(cls.MODE_DISPLAY)
        tables["trigger_display"].update(cls.TRIGGER_DISPLAY)
        tables["map_prefixes"].update(cls.MAP_PREFIXES)
        tables["trigger_log_style"].update(cls.TRIGGER_LOG_STYLE)
    tables["map_prefixes"] = sorted(tables["map_prefixes"])
    return tables
