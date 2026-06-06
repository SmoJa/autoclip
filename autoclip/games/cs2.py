# SPDX-License-Identifier: GPL-3.0-or-later
import json
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional, Callable, Dict, Any

from autoclip.games.base import GamePlugin
logger = logging.getLogger(__name__)

MODE_NAMES = {
    "competitive":        "Competitive",
    "premier":            "Premier",
    "gungameprogressive": "Arms Race",
    "gungametrbomb":      "Demolition",
    "retakes":            "Retakes",
    "deathmatch":         "Deathmatch",
    "casual":             "Casual",
    "scrimcomp2v2":       "Wingman",
}

# Weapon categories for kill type detection
KNIFE_WEAPONS = {
    "knife", "knife_t", "knife_ct", "knife_ghost", "knife_widowmaker",
    "knife_karambit", "knife_m9_bayonet", "knife_bayonet", "knife_flip",
    "knife_gut", "knife_tactical", "knife_falchion", "knife_survival_bowie",
    "knife_butterfly", "knife_push", "knife_cord", "knife_canis",
    "knife_ursus", "knife_gypsy_jackknife", "knife_stiletto", "knife_talon",
    "knife_skeleton", "knife_css",
}

HE_WEAPONS      = {"hegrenade", "frag_grenade"}
FIRE_WEAPONS    = {"molotov", "incgrenade"}
UTILITY_WEAPONS = {"flashbang", "smokegrenade", "decoy", "tagrenade"}
GRENADE_WEAPONS = HE_WEAPONS | FIRE_WEAPONS | UTILITY_WEAPONS  # kept for compat


from dataclasses import dataclass
from typing import List


@dataclass
class CS2Config:
    """CS2-specific settings — stored under config.game_configs['CS2']."""
    enabled: bool = True

    # Kill triggers
    kill_trigger:         bool = True
    headshot_trigger:     bool = True
    knife_kill_trigger:   bool = True
    grenade_kill_trigger: bool = True    # legacy — kept for migration
    he_kill_trigger:      bool = True
    fire_kill_trigger:    bool = True
    utility_kill_trigger: bool = True
    first_kill_trigger:   bool = False

    # Multi-kill / ace
    multikill_threshold: int  = 3
    ace_trigger:         bool = True

    # Round triggers
    round_win_trigger:   bool = False
    round_loss_trigger:  bool = False
    clutch_trigger:      bool = True

    # Situational
    low_health_trigger:   bool = True
    low_health_threshold: int  = 20
    bomb_plant_trigger:   bool = False
    bomb_defuse_trigger:  bool = False
    death_trigger:        bool = False   # clip when local player dies
    spectating_clips:     bool = False   # allow clip triggers while spectating

    # Audio
    audio_source_device: str = ""

    # Modes
    enabled_modes: list = None

    def __post_init__(self):
        if self.enabled_modes is None:
            self.enabled_modes = [
                "competitive", "premier",
                "gungameprogressive",
                "gungametrbomb",
                "retakes", "deathmatch", "casual", "scrimcomp2v2",
            ]


class CS2Plugin(GamePlugin):
    """CS2 game plugin — auto-discovered by the registry."""
    NAME          = "CS2"
    PROCESS_NAMES       = ["cs2", "cs2_linux64"]
    AUDIO_PROCESS_NAME  = "cs2"   # maps to 'SDL Application' in PipeWire
    AUDIO_APP_NAMES     = {"cs2": "CS2", "hl2": "Half-Life 2", "dota2": "Dota 2"}
    MAP_PREFIXES  = ["de_", "ar_", "cs_", "gg_", "dz_", "gd_"]

    MODE_ABBREVS = {
        "competitive":        "comp",
        "premier":            "prem",
        "gungameprogressive": "ar",
        "gungametrbomb":      "demo",
        "deathmatch":         "dm",
        "casual":             "cas",
        "scrimcomp2v2":       "wm",
        "retakes":            "ret",
    }
    TRIGGER_ABBREVS = {
        "headshot":      "hs",
        "kill":          "k",
        "knife_kill":    "knife",
        "grenade_kill":  "nade",
        "he_kill":       "he",
        "fire_kill":     "fire",
        "utility_kill":  "util",
        "first_kill":    "fk",
        "multikill":     "mk",
        "ace":           "ace",
        "round_win":     "rw",
        "round_loss":    "rl",
        "clutch":        "clutch",
        "low_health":    "lh",
        "bomb_plant":    "bp",
        "bomb_defuse":   "bd",
        "death":         "death",
        "manual_hotkey": "manual",
    }
    MODE_DISPLAY = {
        "comp":  "Competitive",
        "prem":  "Premier",
        "ar":    "Arms Race",
        "demo":  "Demolition",
        "dm":    "Deathmatch",
        "cas":   "Casual",
        "wm":    "Wingman",
        "ret":   "Retakes",
    }
    TRIGGER_DISPLAY = {
        "hs":     "Headshot",
        "k":      "Kill",
        "knife":  "Knife Kill",
        "nade":   "Grenade Kill",
        "he":     "HE Grenade Kill",
        "fire":   "Fire Kill",
        "util":   "Utility Kill",
        "fk":     "First Kill",
        "mk":     "Multi-Kill",
        "ace":    "ACE",
        "rw":     "Round Win",
        "rl":     "Round Loss",
        "clutch": "Clutch",
        "lh":     "Low Health",
        "bp":     "Bomb Plant",
        "bd":     "Bomb Defuse",
        "death":  "Death",
        "manual": "Manual Save",
    }

    # (display_name, text_color, bg_color, bold)
    # Keys are raw trigger names (before abbreviation)
    TRIGGER_LOG_STYLE = {
        # Common — grey
        "kill":          ("Kill",         "#777777", "transparent", False),
        "round_win":     ("Round Win",    "#777777", "transparent", False),
        "round_loss":    ("Round Loss",   "#444444", "transparent", False),
        "bomb_plant":    ("Bomb Planted", "#777777", "transparent", False),
        "bomb_defuse":   ("Bomb Defused", "#777777", "transparent", False),
        # Uncommon — blue
        "headshot":      ("Headshot",     "#44aaff", "transparent", False),
        "first_kill":    ("First Kill",   "#44aaff", "transparent", False),
        "low_health":    ("Low Health",   "#cc4444", "transparent", False),
        # Rare — green / cyan
        "grenade_kill":  ("Grenade Kill",  "#44dd88", "transparent", False),
        "he_kill":       ("HE Kill",        "#44dd88", "transparent", False),
        "fire_kill":     ("Fire Kill",       "#ff8844", "#0f0500",     True),
        "utility_kill":  ("UTILITY KILL!",   "#ffffff", "#1a0020",     True),
        "knife_kill":    ("Knife Kill",   "#00eeff", "#001a1f",     True),
        # Epic — orange / purple
        "multikill":     ("Multi-Kill",   "#ff8800", "#0f0800",     True),
        "clutch":        ("Clutch Win",   "#cc44ff", "#120020",     True),
        # Legendary — red
        "ace":           ("ACE!",         "#ff4c00", "#1a0800",     True),
        # Death / manual
        "death":         ("Death",        "#cc4444", "transparent", False),
        "manual_hotkey": ("Manual Save",  "#888888", "transparent", False),
        "laughter":      ("Laughter",     "#888888", "transparent", False),
    }

    def __init__(self, config, on_event):
        super().__init__(config, on_event)
        self._gsi = GSIServer(config, on_event)

    def start(self):
        self._gsi.start()

    def stop(self):
        self._gsi.stop()

    def install_integration(self) -> bool:
        return self._gsi.install_cs2_config()

    def get_config_class(self):
        return CS2Config

    def get_config_widget(self, config, parent=None):
        return CS2Tab(config, parent)

    def get_current_context(self) -> str:
        d = self._gsi.detector
        spec = d.current_spectated_player if d._is_spectating else ""
        return (f"|{d.current_mode}|{d.current_map}|{d.current_round}|"
                f"{d.current_team}|{d.current_ct_score}|{d.current_t_score}"
                f"||{spec}")


class CS2EventDetector:
    def __init__(self, config, on_event: Callable[[str], None]):
        self.config = config
        self.on_event = on_event
        self._prev_state: Dict[str, Any] = {}

        # Current game context
        self.current_mode:     str = ""
        self.current_map:      str = ""
        self.current_round:    int = 0
        self.current_team:     str = ""
        self.current_ct_score: int = 0
        self.current_t_score:  int = 0

        # Clutch tracking state
        self._in_clutch:        bool = False  # player is last alive
        self._clutch_triggered: bool = False  # already fired clutch event

        # Low health / death tracking — avoid repeat triggers
        self._low_health_triggered: bool = False
        self._death_triggered:      bool = False  # reset on respawn/new round

        # Bomb tracking
        self._bomb_planted_triggered:  bool = False
        self._bomb_defused_triggered:  bool = False
        self._local_has_c4:            bool = False   # local player is bomb carrier
        self._local_defusing:          bool = False   # local player started defusing

        # Spectating state — persisted because GSI only sends steamid on change
        self._provider_steamid:      str  = ""   # cached local player steamid
        self._is_spectating:         bool = False
        self.current_spectated_player: str = ""  # name of player being spectated

    def _mode_allowed(self) -> bool:
        modes = getattr(self.config.cs2, "enabled_modes", [])
        if not modes:
            return True
        return self.current_mode in modes

    def process(self, data: Dict[str, Any]):
        if not getattr(self.config.cs2, "enabled", True):
            return
        map_data   = data.get("map", {})
        player     = data.get("player", {})
        state      = player.get("state", {})
        round_data = data.get("round", {})

        prev_player = self._prev_state.get("player", {})
        prev_state  = prev_player.get("state", {})
        prev_round  = self._prev_state.get("round", {})
        prev_map    = self._prev_state.get("map", {})

        self.current_mode     = map_data.get("mode", "")
        self.current_map      = map_data.get("name", "")
        self.current_round    = map_data.get("round", 0)
        self.current_team     = player.get("team", "").lower()
        self.current_ct_score = map_data.get("team_ct", {}).get("score", 0)
        self.current_t_score  = map_data.get("team_t", {}).get("score", 0)

        phase = map_data.get("phase", "")
        in_match = phase in ("live", "freezetime", "over")
        # Also process kills when the previous tick was in a match — catches final kills
        # that arrive in the same GSI payload as a phase change to intermission/gameover
        # (e.g. final kill at halftime, Arms Race game-winning knife kill).
        prev_in_match = prev_map.get("phase", "") in ("live", "freezetime", "over")

        # Spectating detection — stateful, because GSI uses delta encoding and only
        # sends player.steamid when it changes (not every tick).
        # provider.steamid = always local player; player.steamid = observed player.
        raw_provider = str(data.get("provider", {}).get("steamid", ""))
        raw_player   = str(player.get("steamid", ""))
        if raw_provider:
            self._provider_steamid = raw_provider
        if self._provider_steamid and raw_player:
            new_spectating = (self._provider_steamid != raw_player)
            if self._is_spectating and not new_spectating:
                self.current_spectated_player = ""
            self._is_spectating = new_spectating
            logger.debug(
                f"Spectating update: {self._is_spectating} "
                f"(provider={self._provider_steamid} player={raw_player})"
            )
        # Track spectated player name when it appears in the payload
        if self._is_spectating:
            player_name = player.get("name", "")
            if player_name:
                self.current_spectated_player = player_name

        # Reset per-round state on new round (freezetime)
        if phase == "freezetime" and prev_map.get("phase") != "freezetime":
            self._in_clutch              = False
            self._clutch_triggered       = False
            self._low_health_triggered   = False
            self._death_triggered        = False
            self._bomb_planted_triggered    = False
            self._bomb_defused_triggered    = False
            self._local_has_c4              = False
            self._local_defusing            = False
            self._is_spectating             = False   # alive again at round start
            self.current_spectated_player   = ""

        # Reset low health / death triggers when health recovers (respawn / Arms Race)
        prev_health = prev_state.get("health", 100)
        curr_health = state.get("health", 100)
        if curr_health > prev_health:
            if self._low_health_triggered:
                self._low_health_triggered = False
            if self._death_triggered:
                self._death_triggered = False

        if not in_match and not prev_in_match:
            self._prev_state = data
            return

        if not self._mode_allowed():
            self._prev_state = data
            return

        cs2         = self.config.cs2
        player_team = player.get("team", "")
        health      = state.get("health", 100)

        is_spectating = self._is_spectating

        # Active weapon — check PREVIOUS state too since Arms Race switches weapon on kill
        def _active_weapon(d):
            for wep in d.get("player", {}).get("weapons", {}).values():
                if wep.get("state") == "active":
                    return wep.get("name", "").replace("weapon_", "")
            return ""

        active_wep      = _active_weapon(data)
        prev_active_wep = _active_weapon(self._prev_state)
        kill_weapon = prev_active_wep if (prev_active_wep and active_wep != prev_active_wep) else active_wep
        logger.debug(f"Kill weapon detection: active={active_wep!r} prev={prev_active_wep!r} kill_weapon={kill_weapon!r}")

        round_kills  = state.get("round_kills", 0)
        prev_kills   = prev_state.get("round_kills", 0)
        round_hs     = state.get("round_killhs", 0)
        prev_hs      = prev_state.get("round_killhs", 0)
        match_kills  = player.get("match_stats", {}).get("kills", 0)

        # ── Kill detection ──────────────────────────────────────────────
        # Skip kill events while spectating unless the user has opted in.
        # (GSI reports the spectated player's stats, not the local player's.)
        if round_kills > prev_kills and (not is_spectating or cs2.spectating_clips):
            new_kills = round_kills - prev_kills
            new_hs    = round_hs - prev_hs

            # Determine kill type by weapon at time of kill
            if kill_weapon in KNIFE_WEAPONS and cs2.knife_kill_trigger:
                self._fire("knife_kill", kill_weapon)
            elif kill_weapon in GRENADE_WEAPONS:
                if kill_weapon in HE_WEAPONS and getattr(cs2, "he_kill_trigger", cs2.grenade_kill_trigger):
                    self._fire("he_kill", kill_weapon)
                elif kill_weapon in FIRE_WEAPONS and getattr(cs2, "fire_kill_trigger", cs2.grenade_kill_trigger):
                    self._fire("fire_kill", kill_weapon)
                elif kill_weapon in UTILITY_WEAPONS and getattr(cs2, "utility_kill_trigger", cs2.grenade_kill_trigger):
                    self._fire("utility_kill", kill_weapon)
            elif new_hs > 0 and cs2.headshot_trigger:
                self._fire("headshot", kill_weapon)
            elif cs2.kill_trigger:
                self._fire("kill", kill_weapon)

            # First kill of round
            if cs2.first_kill_trigger and prev_kills == 0 and round_kills >= 1:
                self._fire("first_kill", kill_weapon)

            # Multi-kill / Ace
            threshold = cs2.multikill_threshold
            if threshold > 0 and round_kills >= threshold:
                if prev_kills < threshold:
                    if round_kills >= 5 and cs2.ace_trigger:
                        self._fire("ace", kill_weapon)
                    else:
                        self._fire("multikill", kill_weapon)

        # Non-kill events only apply during clean match phases, not transition phases
        if not in_match:
            self._prev_state = data
            return

        # ── Death detection ─────────────────────────────────────────────
        # Only fires for the local player (not while spectating someone else).
        if (cs2.death_trigger and not is_spectating
                and not self._death_triggered
                and curr_health == 0 and prev_health > 0):
            self._death_triggered = True
            self._fire("death")

        # ── Round win / loss / clutch ───────────────────────────────────
        prev_phase = prev_round.get("phase", "")
        curr_phase = round_data.get("phase", "")

        if curr_phase == "over" and prev_phase != "over":
            win_team = round_data.get("win_team", "")

            if win_team and player_team:
                if win_team == player_team:
                    # Only clip a round win if the local player survived to see it
                    if cs2.round_win_trigger and health > 0:
                        self._fire("round_win")
                    # Clutch win — were we the last alive when round ended?
                    if self._in_clutch and not self._clutch_triggered and cs2.clutch_trigger:
                        self._clutch_triggered = True
                        self._fire("clutch")
                else:
                    # Only clip a round loss if the local player was still alive
                    if cs2.round_loss_trigger and health > 0:
                        self._fire("round_loss")

        # ── Clutch state tracking ───────────────────────────────────────
        # Player is in a clutch if they're alive and their team has no other
        # living players. Use map team alive counts.
        if player_team and health > 0 and phase == "live":
            ct_alive = map_data.get("team_ct", {}).get("players_alive", 99)
            t_alive  = map_data.get("team_t", {}).get("players_alive", 99)
            my_team_alive = ct_alive if player_team == "CT" else t_alive
            if my_team_alive == 1 and not self._in_clutch:
                self._in_clutch = True
                logger.debug("Clutch situation detected")

        # ── Low health ─────────────────────────────────────────────────
        prev_health = prev_state.get("health", 100)
        threshold_hp = getattr(cs2, "low_health_threshold", 20)
        if (cs2.low_health_trigger and not self._low_health_triggered
                and health > 0 and health <= threshold_hp
                and prev_health > threshold_hp):
            self._low_health_triggered = True
            self._fire("low_health")

        # ── Bomb events ─────────────────────────────────────────────────
        bomb      = round_data.get("bomb", "")
        prev_bomb = prev_round.get("bomb", "")

        # Track whether local player is carrying the C4 (weapons only sent on change,
        # so we maintain this as persistent state and clear it when the bomb is planted).
        for wep in player.get("weapons", {}).values():
            if "c4" in wep.get("name", "").lower():
                self._local_has_c4 = True
                break

        # Bomb plant — only fire if local player was the bomb carrier
        if (cs2.bomb_plant_trigger and not self._bomb_planted_triggered
                and bomb == "planted" and prev_bomb != "planted"
                and self._local_has_c4):
            self._bomb_planted_triggered = True
            self._local_has_c4 = False
            self._fire("bomb_plant")

        # Defuse — mark that local player started defusing when the "defusing"
        # state appears while they are CT and alive (only one player can defuse at a time).
        if (bomb == "defusing" and prev_bomb != "defusing"
                and player_team == "CT" and health > 0):
            self._local_defusing = True

        # Fire on successful defuse only if this player was the one defusing
        if (cs2.bomb_defuse_trigger and not self._bomb_defused_triggered
                and bomb == "defused" and prev_bomb != "defused"
                and self._local_defusing):
            self._bomb_defused_triggered = True
            self._fire("bomb_defuse")

        self._prev_state = data

    def _fire(self, trigger: str, weapon: str = ""):
        spec = self.current_spectated_player if self._is_spectating else ""
        event = (f"{trigger}|{self.current_mode}|{self.current_map}|"
                 f"{self.current_round}|{self.current_team}|"
                 f"{self.current_ct_score}|{self.current_t_score}|"
                 f"{weapon}|{spec}")
        logger.info(f"CS2 event: {event}")
        self.on_event(event)


class GSIRequestHandler(BaseHTTPRequestHandler):
    detector: Optional[CS2EventDetector] = None

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body   = self.rfile.read(length)
            data   = json.loads(body)
            # Log key GSI fields for debugging
            map_data = data.get("map", {})
            player   = data.get("player", {})
            state    = player.get("state", {})
            logger.debug(
                f"GSI: mode={map_data.get('mode')} phase={map_data.get('phase')} "
                f"round_kills={state.get('round_kills')} health={state.get('health')}"
            )
            if self.detector:
                self.detector.process(data)
        except Exception as e:
            logger.debug(f"GSI error: {e}")
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


class GSIServer:
    def __init__(self, config, on_event: Callable[[str], None]):
        self.config   = config
        self.detector = CS2EventDetector(config, on_event)
        self._server: Optional[HTTPServer] = None

    @property
    def is_running(self) -> bool:
        return self._server is not None

    def start(self):
        if self._server:
            return
        GSIRequestHandler.detector = self.detector
        try:
            self._server = HTTPServer(("127.0.0.1", self.config.gsi_port),
                                      GSIRequestHandler)
        except OSError as e:
            if e.errno == 98:
                msg = (
                    f"Port {self.config.gsi_port} is already in use.\n\n"
                    "Another instance of AutoClip is already running — "
                    "check your system tray and quit it first."
                )
                logger.error(msg)
                raise RuntimeError(msg) from e
            raise
        t = threading.Thread(target=self._server.serve_forever, daemon=True)
        t.start()
        logger.info(f"GSI server on port {self.config.gsi_port}")

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server = None

    def install_cs2_config(self) -> bool:
        import os
        paths = [
            os.path.expanduser(
                "~/.steam/steam/steamapps/common/"
                "Counter-Strike Global Offensive/game/core/cfg"),
            os.path.expanduser(
                "~/.local/share/Steam/steamapps/common/"
                "Counter-Strike Global Offensive/game/core/cfg"),
        ]
        cfg = f'''// AutoClip CS2 GSI Config
"autoclip"
{{
    "uri" "http://127.0.0.1:{self.config.gsi_port}/"
    "timeout" "5.0"
    "buffer"  "0.1"
    "throttle" "0.1"
    "heartbeat" "10.0"
    "data"
    {{
        "provider"           "1"
        "map"                "1"
        "round"              "1"
        "player_id"          "1"
        "player_state"       "1"
        "player_weapons"     "1"
        "player_match_stats" "1"
    }}
}}
'''
        for p in paths:
            if os.path.isdir(p):
                dest = os.path.join(p, "gamestate_integration_autoclip.cfg")
                try:
                    with open(dest, "w") as f:
                        f.write(cfg)
                    logger.info(f"GSI config -> {dest}")
                    return True
                except Exception as e:
                    logger.error(f"Write failed: {e}")
        return False

    def find_cs2_cfg_path(self) -> Optional[str]:
        import os
        for p in [
            os.path.expanduser(
                "~/.steam/steam/steamapps/common/"
                "Counter-Strike Global Offensive/game/core/cfg"),
            os.path.expanduser(
                "~/.local/share/Steam/steamapps/common/"
                "Counter-Strike Global Offensive/game/core/cfg"),
        ]:
            if os.path.isdir(p):
                return p
        return None


class CS2Tab:
    """
    Settings widget for CS2. Returned by CS2Plugin.get_config_widget().
    Entirely self-contained — no CS2 code needed in main_window.py.
    """

    def __new__(cls, config, parent=None):
        """Return a QWidget without importing Qt at module level."""
        from PyQt6.QtWidgets import (
            QWidget, QVBoxLayout, QGridLayout, QGroupBox,
            QCheckBox, QLabel, QPushButton, QHBoxLayout,
            QSpinBox, QAbstractSpinBox, QSizePolicy,
        )
        from PyQt6.QtCore import Qt, QTimer, QObject, pyqtSignal
        import threading

        class _Sig(QObject):
            result = pyqtSignal(object)

        w = QWidget(parent)
        w.config = config
        layout = QVBoxLayout(w)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        cs2 = config.cs2

        w._enabled_cb = QCheckBox("Enable CS2 monitoring", w)
        w._enabled_cb.setChecked(getattr(cs2, "enabled", True))

        # ── GSI Setup ────────────────────────────────────────────────
        gsi = QGroupBox("GSI Setup")
        gl = QVBoxLayout(gsi)
        gl.setSpacing(12)
        info = QLabel(
            "CS2 uses Game State Integration to broadcast kill and round events.\n"
            "Click below to automatically install the config file into your CS2 installation."
        )
        info.setWordWrap(True)
        info.setStyleSheet("color: #777; font-size: 12px;")
        gl.addWidget(info)
        w.gsi_status = QLabel("")
        w.gsi_status.setStyleSheet("color: #888; font-size: 12px;")
        gl.addWidget(w.gsi_status)
        import os as _os
        _gsi_file = None
        for _p in [
            _os.path.expanduser(
                "~/.steam/steam/steamapps/common/"
                "Counter-Strike Global Offensive/game/core/cfg/"
                "gamestate_integration_autoclip.cfg"),
            _os.path.expanduser(
                "~/.local/share/Steam/steamapps/common/"
                "Counter-Strike Global Offensive/game/core/cfg/"
                "gamestate_integration_autoclip.cfg"),
        ]:
            if _os.path.isfile(_p):
                _gsi_file = _p
                break
        if _gsi_file:
            w.gsi_status.setText(f"✓ Installed to {_gsi_file}")
            w.gsi_status.setStyleSheet("color: #00cc66; font-size: 12px;")
        else:
            w.gsi_status.setText("✗ Not installed")
            w.gsi_status.setStyleSheet("color: #cc3300; font-size: 12px;")
        w.install_btn = QPushButton("INSTALL GSI CONFIG")
        w.install_btn.setObjectName("primary")
        gl.addWidget(w.install_btn)

        audio_row = QHBoxLayout()
        _saved_device = next(
            (t.get("device", "") for t in (config.audio_tracks or [])
             if isinstance(t, dict) and t.get("track_type") == "game"),
            ""
        )
        w.audio_status = QLabel(f"✓ {_saved_device}" if _saved_device else "Game audio: not resolved")
        w.audio_status.setStyleSheet(
            "color: #00cc66; font-size: 11px;" if _saved_device else "color: #888; font-size: 11px;"
        )
        resolve_btn = QPushButton("RESOLVE AUDIO")
        resolve_btn.setStyleSheet(
            "background:#1e2024;color:#d4d4d4;border:1px solid #2e3035;padding:5px 12px;")
        resolve_btn.setToolTip(
            "Detect CS2's audio output node via PipeWire.\n"
            "CS2 must be running. Result is saved to your audio tracks.")

        def _resolve_audio():
            w.audio_status.setText("Resolving…")
            resolve_btn.setEnabled(False)
            sig = _Sig(w)  # parent=w → main-thread affinity → emit cross-thread is queued

            def _on_result(device):
                resolve_btn.setEnabled(True)
                if device:
                    tracks = config.audio_tracks or []
                    for track in tracks:
                        if isinstance(track, dict) and track.get("track_type") == "game":
                            track["device"] = device
                            break
                    config.save()
                    w.audio_status.setText(f"✓ {device}")
                    w.audio_status.setStyleSheet("color: #00cc66; font-size: 11px;")
                else:
                    w.audio_status.setText("✗ Not found — is CS2 running?")
                    w.audio_status.setStyleSheet("color: #cc3300; font-size: 11px;")

            sig.result.connect(_on_result)

            def _run():
                import logging as _log
                device = None
                try:
                    from autoclip.core.audio import resolve_game_audio_node
                    device = resolve_game_audio_node("cs2", config.gpu_recorder_path)
                except Exception:
                    _log.getLogger(__name__).exception("resolve_game_audio_node failed")
                sig.result.emit(device)

            threading.Thread(target=_run, daemon=True).start()

        resolve_btn.clicked.connect(_resolve_audio)
        audio_row.addWidget(w.audio_status)
        audio_row.addStretch()
        audio_row.addWidget(resolve_btn)
        gl.addLayout(audio_row)
        layout.addWidget(gsi)

        # ── Kill Triggers ─────────────────────────────────────────────
        kill_g = QGroupBox("Kill Triggers")
        kl = QGridLayout(kill_g)
        kl.setSpacing(10)

        kill_cb    = QCheckBox("Any kill");            kill_cb.setChecked(cs2.kill_trigger)
        hs_cb      = QCheckBox("Headshot kill");       hs_cb.setChecked(cs2.headshot_trigger)
        knife_cb   = QCheckBox("Knife kill");          knife_cb.setChecked(cs2.knife_kill_trigger)
        he_cb      = QCheckBox("HE grenade kill");     he_cb.setChecked(getattr(cs2, "he_kill_trigger", True))
        fire_cb    = QCheckBox("Fire kill  (molotov / incendiary)"); fire_cb.setChecked(getattr(cs2, "fire_kill_trigger", True))
        util_cb    = QCheckBox("Utility kill  (flash / smoke / decoy)"); util_cb.setChecked(getattr(cs2, "utility_kill_trigger", True))
        util_cb.setToolTip(
            "Extremely rare — requires a direct hit from a flashbang, smoke,\n"
            "or decoy on an enemy with 1 HP. Worth capturing when it happens!")
        firstk_cb  = QCheckBox("First kill of round"); firstk_cb.setChecked(cs2.first_kill_trigger)

        mk_row_w = QWidget()
        mk_l = QHBoxLayout(mk_row_w); mk_l.setContentsMargins(0,0,0,0)
        multikill_cb   = QCheckBox("Multi-kill  (kills ≥)"); multikill_cb.setChecked(cs2.multikill_threshold > 0)
        multikill_spin = QSpinBox(); multikill_spin.setRange(2,4)
        multikill_spin.setValue(max(min(cs2.multikill_threshold,4),2))
        multikill_spin.setFixedWidth(52)
        multikill_spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        mk_l.addWidget(multikill_cb); mk_l.addWidget(multikill_spin); mk_l.addStretch()

        ace_cb = QCheckBox("Ace  (5 kills in round)"); ace_cb.setChecked(cs2.ace_trigger)

        kl.addWidget(kill_cb,    0, 0); kl.addWidget(hs_cb,      0, 1)
        kl.addWidget(knife_cb,   1, 0); kl.addWidget(he_cb,      1, 1)
        kl.addWidget(fire_cb,    2, 0); kl.addWidget(util_cb,    2, 1)
        kl.addWidget(firstk_cb,  3, 0); kl.addWidget(mk_row_w,  3, 1)
        kl.addWidget(ace_cb,     4, 0)
        layout.addWidget(kill_g)

        # ── Round Triggers ────────────────────────────────────────────
        round_g = QGroupBox("Round & Match Triggers")
        rl = QGridLayout(round_g); rl.setSpacing(10)
        roundwin_cb  = QCheckBox("Round win");            roundwin_cb.setChecked(cs2.round_win_trigger)
        roundloss_cb = QCheckBox("Round loss");           roundloss_cb.setChecked(cs2.round_loss_trigger)
        clutch_cb    = QCheckBox("Clutch win  (last alive)"); clutch_cb.setChecked(cs2.clutch_trigger)
        rl.addWidget(roundwin_cb, 0, 0); rl.addWidget(roundloss_cb, 0, 1)
        rl.addWidget(clutch_cb,   1, 0)
        layout.addWidget(round_g)

        # ── Situational Triggers ──────────────────────────────────────
        sit_g = QGroupBox("Situational Triggers")
        sl = QGridLayout(sit_g); sl.setSpacing(10)
        lh_row_w = QWidget()
        lh_l = QHBoxLayout(lh_row_w); lh_l.setContentsMargins(0,0,0,0)
        lowhealth_cb   = QCheckBox("Low health  (≤"); lowhealth_cb.setChecked(cs2.low_health_trigger)
        lowhealth_spin = QSpinBox(); lowhealth_spin.setRange(1,99)
        lowhealth_spin.setValue(cs2.low_health_threshold); lowhealth_spin.setFixedWidth(52)
        lowhealth_spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        lh_hp = QLabel("HP)"); lh_hp.setStyleSheet("color:#888;")
        lh_l.addWidget(lowhealth_cb); lh_l.addWidget(lowhealth_spin); lh_l.addWidget(lh_hp); lh_l.addStretch()
        bombplant_cb  = QCheckBox("Bomb plant");  bombplant_cb.setChecked(cs2.bomb_plant_trigger)
        bombdefuse_cb = QCheckBox("Bomb defuse"); bombdefuse_cb.setChecked(cs2.bomb_defuse_trigger)
        death_cb      = QCheckBox("My death  (clip when I die)")
        death_cb.setChecked(getattr(cs2, "death_trigger", False))
        spectating_cb = QCheckBox("Save clips while spectating another player")
        spectating_cb.setChecked(getattr(cs2, "spectating_clips", False))
        sl.addWidget(lh_row_w,       0, 0, 1, 2)
        sl.addWidget(bombplant_cb,   1, 0); sl.addWidget(bombdefuse_cb, 1, 1)
        sl.addWidget(death_cb,       2, 0, 1, 2)
        sl.addWidget(spectating_cb,  3, 0, 1, 2)
        layout.addWidget(sit_g)

        # ── Mode Filter ───────────────────────────────────────────────
        modes_g = QGroupBox("Record In These Modes")
        ml = QGridLayout(modes_g); ml.setSpacing(10)
        mode_checks = {}
        mode_defs = [
            ("competitive","Competitive"), ("premier","Premier"),
            ("gungameprogressive","Arms Race"), ("gungametrbomb","Demolition"),
            ("retakes","Retakes"), ("deathmatch","Deathmatch"),
            ("casual","Casual"), ("scrimcomp2v2","Wingman"),
        ]
        enabled_modes = cs2.enabled_modes or []

        def _save():
            cs2.enabled              = w._enabled_cb.isChecked()
            cs2.kill_trigger         = kill_cb.isChecked()
            cs2.headshot_trigger     = hs_cb.isChecked()
            cs2.knife_kill_trigger   = knife_cb.isChecked()
            cs2.he_kill_trigger      = he_cb.isChecked()
            cs2.fire_kill_trigger    = fire_cb.isChecked()
            cs2.utility_kill_trigger = util_cb.isChecked()
            cs2.first_kill_trigger   = firstk_cb.isChecked()
            cs2.multikill_threshold  = multikill_spin.value() if multikill_cb.isChecked() else 0
            cs2.ace_trigger          = ace_cb.isChecked()
            cs2.round_win_trigger    = roundwin_cb.isChecked()
            cs2.round_loss_trigger   = roundloss_cb.isChecked()
            cs2.clutch_trigger       = clutch_cb.isChecked()
            cs2.low_health_trigger   = lowhealth_cb.isChecked()
            cs2.low_health_threshold = lowhealth_spin.value()
            cs2.bomb_plant_trigger   = bombplant_cb.isChecked()
            cs2.bomb_defuse_trigger  = bombdefuse_cb.isChecked()
            cs2.death_trigger        = death_cb.isChecked()
            cs2.spectating_clips     = spectating_cb.isChecked()
            cs2.enabled_modes = [k for k, cb in mode_checks.items() if cb.isChecked()]
            config.save()

        for i, (key, label) in enumerate(mode_defs):
            cb = QCheckBox(label)
            cb.setChecked(key in enabled_modes)
            cb.toggled.connect(_save)
            mode_checks[key] = cb
            ml.addWidget(cb, i // 2, i % 2)
        layout.addWidget(modes_g)
        layout.addStretch()

        for cb in [w._enabled_cb, kill_cb, hs_cb, knife_cb, he_cb, fire_cb, util_cb,
                   firstk_cb, multikill_cb, ace_cb, roundwin_cb,
                   roundloss_cb, clutch_cb, lowhealth_cb,
                   bombplant_cb, bombdefuse_cb, death_cb, spectating_cb]:
            cb.toggled.connect(_save)
        multikill_spin.valueChanged.connect(_save)
        lowhealth_spin.valueChanged.connect(_save)

        return w
