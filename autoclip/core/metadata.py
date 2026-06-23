# SPDX-License-Identifier: GPL-3.0-or-later
"""
AutoClip filename metadata encoding/decoding.

Format:
  GAME_MODE_MAP_rROUND_TEAM_SCORE_ev[TRIGGER:SECS,...]_TIMESTAMP.mkv

Example:
  CS2_comp_de_dust2_r8_ct_14-12_ev[hs:10,mk:7]_2026-05-12_21-32-05.mkv

All fields except GAME and TIMESTAMP are optional and omitted if unknown.
"""

import re
import sys
from dataclasses import dataclass, field
from typing import List, Optional, Dict

# Colons are illegal in NTFS filenames. Use a private-use char on Windows;
# keep ':' on Linux/macOS where it's a valid filename character.
# _parse_events normalises both back to ':' when decoding.
_EV_SEP = "" if sys.platform == "win32" else ":"

# ── Abbreviation tables ──────────────────────────────────────────────────────

MODE_TO_SHORT: Dict[str, str] = {
    "competitive":        "comp",
    "premier":            "prem",
    "gungameprogressive": "ar",
    "gungametrbomb":      "demo",
    "deathmatch":         "dm",
    "casual":             "cas",
    "scrimcomp2v2":       "wm",
    "retakes":            "ret",
}
SHORT_TO_MODE = {v: k for k, v in MODE_TO_SHORT.items()}  # extended by plugins

TRIGGER_TO_SHORT: Dict[str, str] = {
    "headshot":      "hs",
    "kill":          "k",
    "knife_kill":    "knife",
    "grenade_kill":  "nade",
    "first_kill":    "fk",
    "multikill":     "mk",
    "ace":           "ace",
    "round_win":     "rw",
    "round_loss":    "rl",
    "clutch":        "clutch",
    "low_health":    "lh",
    "bomb_plant":    "bp",
    "bomb_defuse":   "bd",
    "manual_hotkey": "manual",
}
SHORT_TO_TRIGGER = {v: k for k, v in TRIGGER_TO_SHORT.items()}  # extended by plugins

# Human-readable display names (from short or full key)
TRIGGER_DISPLAY: Dict[str, str] = {
    "hs":     "Headshot",
    "k":      "Kill",
    "knife":  "Knife Kill",
    "nade":   "Grenade Kill",
    "fk":     "First Kill",
    "mk":     "Multi-Kill",
    "ace":    "ACE",
    "rw":     "Round Win",
    "rl":     "Round Loss",
    "clutch": "Clutch",
    "lh":     "Low Health",
    "bp":     "Bomb Plant",
    "bd":     "Bomb Defuse",
    "manual": "Manual Save",
    # also support full names as keys
    "headshot":      "Headshot",
    "kill":          "Kill",
    "knife_kill":    "Knife Kill",
    "grenade_kill":  "Grenade Kill",
    "first_kill":    "First Kill",
    "multikill":     "Multi-Kill",
    "round_win":     "Round Win",
    "round_loss":    "Round Loss",
    "low_health":    "Low Health",
    "bomb_plant":    "Bomb Plant",
    "bomb_defuse":   "Bomb Defuse",
    "manual_hotkey": "Manual Save",
}

MODE_DISPLAY: Dict[str, str] = {
    "comp":  "Competitive",
    "prem":  "Premier",
    "ar":    "Arms Race",
    "demo":  "Demolition",
    "dm":    "Deathmatch",
    "cas":   "Casual",
    "wm":    "Wingman",
    "ret":   "Retakes",
    # full names too
    "competitive":        "Competitive",
    "premier":            "Premier",
    "gungameprogressive": "Arms Race",
    "gungametrbomb":      "Demolition",
    "deathmatch":         "Deathmatch",
    "casual":             "Casual",
    "scrimcomp2v2":       "Wingman",
    "retakes":            "Retakes",
}

MAP_PREFIXES = ("de_", "ar_", "cs_", "gg_", "dz_", "gd_")  # extended by plugins at runtime


@dataclass
class EventMark:
    """A single trigger event with its timing."""
    trigger:       str    # short code e.g. "hs"
    secs_from_end: float  # seconds from end of clip to this event (float for precision)
    weapon:        str = ""  # weapon used e.g. "awp", "ak47"

    @property
    def display(self) -> str:
        # Check global table first (patched by controller at startup)
        if self.trigger in TRIGGER_DISPLAY:
            base = TRIGGER_DISPLAY[self.trigger]
        else:
            # Try plugin registry directly (for clips opened before controller runs)
            try:
                from autoclip.games.registry import build_global_metadata_tables
                tables = build_global_metadata_tables()
                base = tables["trigger_display"].get(
                    self.trigger,
                    self.trigger.replace("_", " ").title()
                )
            except Exception:
                base = self.trigger.replace("_", " ").title()
        if self.weapon:
            return f"{base} ({self.weapon})"
        return base

    @property
    def display_short(self) -> str:
        """Short label for timeline marker."""
        parts = [self.trigger]
        if self.weapon:
            parts.append(self.weapon)
        return " ".join(parts)


@dataclass
class ClipMeta:
    """All metadata associated with a clip, encodable in a filename."""
    game:             str = "CS2"
    mode:             str = ""     # short code e.g. "comp"
    map_name:         str = ""     # e.g. "de_dust2"
    round_num:        str = ""
    team:             str = ""     # "ct" or "t"
    score:            str = ""     # e.g. "14-12" (ct_score-t_score)
    events:           List[EventMark] = field(default_factory=list)
    clip_type:        str = ""   # "s"=single, "o1"/"o2"/... = overflow sequence
    spectated_player: str = ""   # name of player being spectated, if any

    # ── Display helpers ──────────────────────────────────────────────────────

    @property
    def mode_display(self) -> str:
        return MODE_DISPLAY.get(self.mode, self.mode)

    @property
    def trigger_display(self) -> str:
        if not self.events:
            return "Manual Save"
        seen = []
        for e in self.events:
            d = e.display
            if d not in seen:
                seen.append(d)
        return " + ".join(seen)

    @property
    def map_display(self) -> str:
        return self.map_name

    @property
    def round_display(self) -> str:
        return f"Round {self.round_num}" if self.round_num else ""

    @property
    def team_display(self) -> str:
        return self.team.upper() if self.team else ""

    @property
    def spectated_display(self) -> str:
        return f"Spectating {self.spectated_player}" if self.spectated_player else ""

    # ── Encoding ─────────────────────────────────────────────────────────────

    def to_filename_stem(self, timestamp: str = "") -> str:
        """
        Build filename stem from metadata.
        CS2_comp_de_dust2_r8_ct_14-12_ev[hs:10,mk:7]_2026-05-12_21-32-05
        """
        from datetime import datetime as _dt
        ts = timestamp or _dt.now().strftime("%Y-%m-%d_%H-%M-%S")

        def safe(s: str) -> str:
            return "".join(c if c.isalnum() or c in "-_" else "_" for c in s)

        parts = [safe(self.game)]
        if self.mode:
            parts.append(safe(self.mode))
        if self.map_name:
            parts.append(safe(self.map_name))
        if self.round_num:
            parts.append(f"r{safe(self.round_num)}")
        if self.team:
            parts.append(safe(self.team))
        if self.score:
            parts.append(safe(self.score))
        if self.clip_type:
            parts.append(safe(self.clip_type))
        if self.spectated_player:
            parts.append(f"spec[{safe(self.spectated_player)}]")
        if self.events:
            sep = _EV_SEP
            items = []
            for e in self.events:
                s = f"{e.trigger}{sep}{round(e.secs_from_end, 1)}"
                if e.weapon:
                    s += f"{sep}{e.weapon}"
                items.append(s)
            parts.append(f"ev[{','.join(items)}]")
        parts.append(ts)
        return "_".join(parts)

    # ── Decoding ─────────────────────────────────────────────────────────────

    @classmethod
    def from_filename(cls, stem: str) -> "ClipMeta":
        """Parse a filename stem into ClipMeta."""
        meta = cls()

        # Extract spec[...] block (spectated player name)
        spec_match = re.search(r"spec\[([^\]]*)\]", stem)
        if spec_match:
            meta.spectated_player = spec_match.group(1)
            stem = stem[:spec_match.start()].rstrip("_") + stem[spec_match.end():]

        # Extract ev[...] block first (may contain colons/commas)
        ev_match = re.search(r"ev\[([^\]]*)\]", stem)
        if ev_match:
            meta.events = cls._parse_events(ev_match.group(1))
            stem = stem[:ev_match.start()].rstrip("_") + stem[ev_match.end():]

        # Extract timestamp (YYYY-MM-DD_HH-MM-SS at end)
        stem = re.sub(r"_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$", "", stem)

        # Clean up any double underscores left by removals
        stem = re.sub(r"_+", "_", stem).strip("_")

        # Split remaining parts
        parts = stem.split("_")
        i = 0
        while i < len(parts):
            part = parts[i]

            # Skip empty or game name (first part)
            if not part or (i == 0 and part.upper() == part):
                i += 1
                continue

            # Map name — check BEFORE mode so "ar" in "ar_shoots" becomes map not mode
            # But only when this part is NOT a mode AND next part together form a map prefix
            # e.g. CS2_ar_ar_shoots: first "ar" is mode, second "ar"+"shoots" is map
            is_mode_part = part in MODE_TO_SHORT or part in SHORT_TO_MODE
            next_forms_map = (i + 1 < len(parts) and
                    any(f"{part}_{parts[i+1]}".startswith(px) for px in MAP_PREFIXES))
            # If this part is a mode AND could start a map, look ahead:
            # if there's another occurrence that also forms a map, this is the mode
            if is_mode_part and next_forms_map:
                # Check if next part alone starts a map with the part after it
                if (i + 2 < len(parts) and
                        any(f"{parts[i+1]}_{parts[i+2]}".startswith(px) for px in MAP_PREFIXES)):
                    # This part is the mode, next is the map start
                    meta.mode = MODE_TO_SHORT.get(part, part)
                    i += 1
                    continue
            if next_forms_map:
                # Start building map name from this pair
                map_parts = [part, parts[i+1]]
                j = i + 2
                while j < len(parts):
                    nxt = parts[j]
                    # Stop at any known metadata token
                    if (nxt in MODE_TO_SHORT or nxt in SHORT_TO_MODE or
                            re.match(r"^r\d+$", nxt) or
                            nxt in ("ct", "t") or
                            re.match(r"^\d+-\d+$", nxt) or
                            nxt in TRIGGER_TO_SHORT or nxt in SHORT_TO_TRIGGER or
                            nxt == "s" or re.match(r"^o\d+$", nxt)):
                        break
                    map_parts.append(nxt)
                    j += 1
                meta.map_name = "_".join(map_parts)
                i = j
                continue

            # Mode (short or full)
            if part in MODE_TO_SHORT or part in SHORT_TO_MODE:
                meta.mode = MODE_TO_SHORT.get(part, part)
                i += 1
                continue

            # Round e.g. r8
            if re.match(r"^r\d+$", part):
                meta.round_num = part[1:]
                i += 1
                continue

            # Team
            if part in ("ct", "t"):
                meta.team = part
                i += 1
                continue

            # Score e.g. 14-12
            if re.match(r"^\d+-\d+$", part):
                meta.score = part
                i += 1
                continue

            # Clip type e.g. "s", "o1", "o2"
            if part == "s" or re.match(r"^o\d+$", part):
                meta.clip_type = part
                i += 1
                continue

            i += 1

        return meta

    @staticmethod
    def _parse_events(ev_str: str) -> List[EventMark]:
        """Parse 'hs:10,mk:7' into EventMark list.
        Linux clips use U+F022 as the colon separator inside events."""
        ev_str = ev_str.replace("", ":")
        events = []
        for part in ev_str.split(","):
            part = part.strip()
            if not part:
                continue
            if ":" in part:
                fields = part.split(":")
                try:
                    trigger = fields[0].strip()
                    secs    = float(fields[1].strip())
                    weapon  = fields[2].strip() if len(fields) > 2 else ""
                    events.append(EventMark(trigger=trigger,
                                            secs_from_end=secs,
                                            weapon=weapon))
                except (ValueError, IndexError):
                    pass
            else:
                events.append(EventMark(trigger=part, secs_from_end=0))
        return events
