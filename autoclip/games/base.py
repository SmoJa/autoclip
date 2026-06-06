# SPDX-License-Identifier: GPL-3.0-or-later
"""
Base class for AutoClip game plugins.

To add a new game, create a file in autoclip/games/ that defines a class
inheriting from GamePlugin. The controller auto-discovers all plugins at startup.

Minimal example — games/mygame.py:

    from autoclip.games.base import GamePlugin

    class MyGamePlugin(GamePlugin):
        NAME          = "My Game"
        PROCESS_NAMES = ["mygame", "mygame64"]
        MODE_ABBREVS  = {"ranked": "rank", "casual": "cas"}
        TRIGGER_ABBREVS = {"kill": "k", "death": "d"}
        MODE_DISPLAY  = {"rank": "Ranked", "cas": "Casual"}
        TRIGGER_DISPLAY = {"k": "Kill", "d": "Death"}

        def start(self): ...
        def stop(self):  ...
        def install_integration(self) -> bool: return True
        def get_config_class(self): return None
        def get_config_widget(self, config, parent=None): return None
"""

from typing import Callable, Optional, Any


class GamePlugin:
    """
    Interface every game plugin must implement.
    All class attributes have safe defaults — only override what you need.
    """

    # ── Identity ──────────────────────────────────────────────────────────────
    NAME: str = ""                        # Display name e.g. "CS2", "Valorant"
    PROCESS_NAMES: list = []              # Process names to watch e.g. ["cs2", "cs2_linux64"]
    PROCESS_CMDLINE_CONTAINS: list = []   # Fallback: substrings to match against full cmdlines
                                          # (used for Wine/Proton games whose comm is generic)
    AUDIO_PROCESS_NAME: str = ""          # PipeWire process name if different from PROCESS_NAMES[0]
    AUDIO_APP_NAMES: dict = {}            # binary/node_name (lowercase) → display name for audio picker

    # ── Metadata vocabulary ───────────────────────────────────────────────────
    # These get merged into the global metadata tables at load time.
    # Keys are raw values from your event strings; values are short codes / display names.
    MODE_ABBREVS:    dict = {}       # raw → short  e.g. {"competitive": "comp"}
    TRIGGER_ABBREVS: dict = {}       # raw → short  e.g. {"headshot": "hs"}
    MODE_DISPLAY:    dict = {}       # short → display  e.g. {"comp": "Competitive"}
    TRIGGER_DISPLAY: dict = {}       # short → display  e.g. {"hs": "Headshot"}
    MAP_PREFIXES:    list = []       # map name prefixes e.g. ["de_", "ar_", "cs_"]

    # Event log styling — maps trigger name → (display_name, color, bg, bold)
    # Colors indicate rarity/importance. Plugins define their own rarity tiers.
    # System events (game_started, game_closed, clip_saved) are handled by the UI.
    TRIGGER_LOG_STYLE: dict = {}     # trigger → (display, color, bg, bold)

    def __init__(self, config: Any, on_event: Callable[[str], None]):
        """
        config   — the global AutoClip Config object. Access game-specific
                   settings via config.game_configs.get(self.NAME, {}).
        on_event — call this with an event string when something clip-worthy
                   happens. Format: "trigger|mode|map|round|team|ct_score|t_score|weapon"
                   Any field can be empty string if not applicable.
        """
        self.config   = config
        self.on_event = on_event

    def start(self):
        """Start event detection (e.g. open GSI server, tail log file)."""
        pass

    def stop(self):
        """Stop event detection and clean up resources."""
        pass

    def install_integration(self) -> bool:
        """
        Install any game-side integration needed (e.g. GSI config file).
        Return True on success, False on failure.
        Called once when the game is first detected or from settings UI.
        """
        return True

    def get_config_class(self):
        """
        Return a dataclass for this game's settings, or None.
        The controller stores an instance at config.game_configs[NAME].
        """
        return None

    def get_config_widget(self, config, parent=None):
        """
        Return a QWidget for this game's settings tab, or None.
        The settings UI automatically adds a tab for each plugin that
        returns a widget here.
        """
        return None

    def resolve_audio_process(self) -> Optional[str]:
        """
        Return the process name to use for PipeWire audio resolution,
        or None to use the default (same as PROCESS_NAMES[0]).
        Override if the game's audio process has a different name than
        its main process (e.g. CS2 runs as "cs2" but audio is "SDL Application").
        """
        return None

    def get_current_context(self) -> str:
        """
        Return the current game state as a pipe-suffix for event strings.
        Format: "|mode|map|round|team|ct_score|t_score||spec_player"
        (field 7 is empty weapon slot; field 8 is spectated player name)
        Used by audio triggers to attach game context when they fire.
        Returns "" if no game is active.
        """
        return ""
