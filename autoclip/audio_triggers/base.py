# SPDX-License-Identifier: GPL-3.0-or-later
"""
Base class for AutoClip audio trigger plugins.

To add a new audio trigger, create a file in autoclip/audio_triggers/ with a class
inheriting from AudioTriggerPlugin. The controller auto-discovers it at startup.

Minimal example — audio_triggers/mydetector.py:

    from autoclip.audio_triggers.base import AudioTriggerPlugin
    from dataclasses import dataclass

    @dataclass
    class MyConfig:
        enabled: bool = False
        threshold: float = 0.5

    class MyDetectorPlugin(AudioTriggerPlugin):
        NAME            = "My Detector"
        TRIGGER_NAME    = "mydetector"
        TRIGGER_DISPLAY = "My Detector"
        TRIGGER_LOG_STYLE = ("My Detector", "#aabbcc", "#001122", False)

        def start(self):
            cfg = getattr(self.config, "mydetector", MyConfig())
            if not cfg.enabled:
                return
            # start detection thread ...

        def stop(self): ...

        def get_config_class(self):
            return MyConfig

        def get_config_widget(self, config, parent=None):
            return MyDetectorWidget(config, parent)
"""

from typing import Callable, Any, Optional


class AudioTriggerPlugin:
    """
    Interface every audio trigger plugin must implement.
    All class attributes have safe defaults — only override what you need.
    """

    NAME: str = ""              # Display name shown in UI, e.g. "Laughter"
    TRIGGER_NAME: str = ""      # Short code used in clip metadata, e.g. "laughter"
    TRIGGER_DISPLAY: str = ""   # Human-readable name for event log, e.g. "Laughter"

    # (display_name, color, bg_color, bold) — event log row styling.
    # Leave empty to use default event log styling.
    TRIGGER_LOG_STYLE: tuple = ()

    def __init__(self, config: Any, on_trigger: Callable[[], None]):
        """
        config     — the global AutoClip Config object. Access plugin-specific
                     settings via getattr(config, self.NAME.lower(), None).
        on_trigger — call this with no args when the trigger fires.
        """
        self.config = config
        self.on_trigger = on_trigger

    def start(self):
        """Start detection. Usually spawns a background thread."""
        pass

    def stop(self):
        """Stop detection and release resources."""
        pass

    def get_config_class(self) -> Optional[type]:
        """
        Return a dataclass for this plugin's settings, or None.
        The controller stores an instance at config.<name.lower()>.
        """
        return None

    def get_config_widget(self, config: Any, parent=None):
        """
        Return a QWidget for this plugin's settings panel, or None.
        The AudioTriggersTab automatically adds a section for each plugin
        that returns a widget here.
        """
        return None
