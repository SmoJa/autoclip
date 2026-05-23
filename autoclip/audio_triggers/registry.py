# SPDX-License-Identifier: GPL-3.0-or-later
"""
Audio trigger plugin registry — auto-discovers all AudioTriggerPlugin subclasses.
"""

import importlib
import pkgutil
import logging
from pathlib import Path
from typing import List, Optional, Type

from .base import AudioTriggerPlugin

logger = logging.getLogger(__name__)

_ALL_PLUGINS: List[Type[AudioTriggerPlugin]] = []
_NAME_MAP: dict = {}


def _load_plugins():
    if _ALL_PLUGINS:
        return

    pkg_dir = Path(__file__).parent
    package = __package__  # "autoclip.audio_triggers"

    for _, module_name, _ in pkgutil.iter_modules([str(pkg_dir)]):
        if module_name in ("base", "registry"):
            continue
        try:
            mod = importlib.import_module(f".{module_name}", package=package)
        except Exception as e:
            logger.warning(f"Failed to load audio trigger plugin {module_name}: {e}")
            continue

        for attr_name in dir(mod):
            obj = getattr(mod, attr_name)
            if (isinstance(obj, type) and
                    issubclass(obj, AudioTriggerPlugin) and
                    obj is not AudioTriggerPlugin and
                    obj.NAME):
                _register(obj)


def _register(cls: Type[AudioTriggerPlugin]):
    if cls in _ALL_PLUGINS:
        return
    _ALL_PLUGINS.append(cls)
    _NAME_MAP[cls.NAME] = cls
    logger.info(f"Registered audio trigger plugin: {cls.NAME}")


def get_all_plugins() -> List[Type[AudioTriggerPlugin]]:
    _load_plugins()
    return list(_ALL_PLUGINS)


def get_plugin_by_name(name: str) -> Optional[Type[AudioTriggerPlugin]]:
    _load_plugins()
    return _NAME_MAP.get(name)
