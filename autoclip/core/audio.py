# SPDX-License-Identifier: GPL-3.0-or-later
"""
Audio source detection for AutoClip.
Uses pw-dump for reliable PID-based process→audio node mapping,
plus gpu-screen-recorder's own listing for the -a argument format.
"""
import subprocess
import json
import logging
from dataclasses import dataclass
from typing import List, Optional, Dict

logger = logging.getLogger(__name__)

CHAT_APP_BINARIES = {
    "discord", "goofcord", "armcord", "vesktop",
    "teamspeak", "ts3client", "mumble", "ventrilo",
    "zoom", "teams", "skype", "slack",
}

CHAT_APP_NAMES = {
    "discord", "goofcord", "teamspeak", "mumble",
    "zoom", "teams", "skype", "slack",
}

# Maps process binary name → human-readable display name shown in the UI.
# Keys are lowercase. Extend this table when new apps need friendly names.
FRIENDLY_APP_NAMES: Dict[str, str] = {
    # Chat / comms
    "discord":    "Discord",
    "goofcord":   "Discord (GoofCord)",
    "armcord":    "Discord (ArmCord)",
    "vesktop":    "Discord (Vesktop)",
    "teamspeak":  "TeamSpeak",
    "ts3client":  "TeamSpeak 3",
    "mumble":     "Mumble",
    "ventrilo":   "Ventrilo",
    "zoom":       "Zoom",
    "teams":      "Microsoft Teams",
    "skype":      "Skype",
    "slack":      "Slack",
    # Browsers
    "firefox":    "Firefox",
    "chrome":     "Chrome",
    "chromium":   "Chromium",
    "brave":      "Brave",
    "opera":      "Opera",
    # Media
    "spotify":    "Spotify",
    "vlc":        "VLC",
    "mpv":        "mpv",
    # Launchers / Wine (game names live in each game plugin's AUDIO_APP_NAMES)
    "steam":      "Steam",
    "wine":       "Wine",
    "wineserver": "Wine",
}


_game_audio_names: Optional[dict] = None

def _get_game_audio_names() -> dict:
    global _game_audio_names
    if _game_audio_names is None:
        try:
            from autoclip.games.registry import build_audio_app_names
            _game_audio_names = build_audio_app_names()
        except Exception:
            _game_audio_names = {}
    return _game_audio_names


def _app_display_name(binary: str, node_name: str) -> str:
    """Return a human-readable name for an app audio source."""
    game_names = _get_game_audio_names()
    for key in (binary.lower() if binary else "", node_name.lower()):
        if not key:
            continue
        if key in FRIENDLY_APP_NAMES:
            return FRIENDLY_APP_NAMES[key]
        if key in game_names:
            return game_names[key]
    if binary:
        return binary.capitalize()
    return node_name


@dataclass
class AudioSource:
    name:        str
    device:      str    # string passed to gpu-screen-recorder -a
    source_type: str    # "app" | "monitor" | "input" | "device"
    app_name:    str = ""
    binary:      str = ""
    pid:         int = 0


# ── pw-dump based detection ─────────────────────────────────────────────────

def _pw_dump() -> List[Dict]:
    """Return parsed pw-dump JSON, or empty list on failure."""
    try:
        result = subprocess.run(
            ["pw-dump"], capture_output=True, text=True, timeout=5
        )
        return json.loads(result.stdout)
    except Exception as e:
        logger.debug(f"pw-dump failed: {e}")
        return []


def get_pw_app_nodes() -> List[Dict]:
    """
    Parse pw-dump and return all audio nodes that belong to user applications.
    Each entry: {pid, binary, app_name, node_name, media_class}
    """
    nodes = []
    seen_node_names = set()

    for obj in _pw_dump():
        props = obj.get("info", {}).get("props", {})
        pid = props.get("application.process.id")
        if not pid:
            continue

        binary    = props.get("application.process.binary", "")
        app_name  = props.get("application.name", "")
        node_name = props.get("node.name", "")
        media_cls = props.get("media.class", "")

        # Skip system processes
        if binary in ("pipewire", "wireplumber", "pw-dump", ""):
            continue

        # Only interested in audio output streams (not input/capture)
        # media.class is Audio/Stream for app output nodes
        if node_name and node_name not in seen_node_names:
            seen_node_names.add(node_name)
            nodes.append({
                "pid":        int(pid),
                "binary":     binary,
                "app_name":   app_name,
                "node_name":  node_name,
                "media_class": media_cls,
            })

    return nodes


def find_audio_node_for_pid(pid: int) -> Optional[str]:
    """
    Given a process PID, find its audio output node name in PipeWire.
    Returns the node_name string (e.g. "SDL Application") or None.
    """
    nodes = get_pw_app_nodes()
    # Prefer nodes with a real node_name (output stream) over empty ones
    matches = [n for n in nodes if n["pid"] == pid and n["node_name"]]
    if not matches:
        return None
    # If multiple, prefer the one that looks like an audio output
    for n in matches:
        if "Stream" in n.get("media_class", ""):
            return n["node_name"]
    return matches[0]["node_name"]


def find_audio_node_for_binary(binary: str) -> Optional[str]:
    """
    Given a process binary name or app name, find its audio output node name.
    Checks both binary and app_name — Wine/Proton processes report binary as
    "wine64-preloader" but app_name as the actual exe (e.g. "PathOfExileSteam.exe").
    Returns the node_name string or None.
    """
    nodes = get_pw_app_nodes()
    binary_lower = binary.lower()
    matches = [
        n for n in nodes
        if n["node_name"] and (
            binary_lower in n["binary"].lower() or
            binary_lower in n["app_name"].lower()
        )
    ]
    if not matches:
        return None
    for n in matches:
        if "Stream" in n.get("media_class", ""):
            return n["node_name"]
    return matches[0]["node_name"]


def get_running_pid(process_name: str) -> Optional[int]:
    """Get PID of a running process by name."""
    try:
        result = subprocess.run(
            ["pgrep", "-x", process_name],
            capture_output=True, text=True
        )
        pids = [int(p) for p in result.stdout.split() if p.strip()]
        return pids[0] if pids else None
    except Exception:
        return None


# ── gpu-screen-recorder based listing ───────────────────────────────────────

def _run_gsr(args: list, gsr_path: str = "gpu-screen-recorder") -> str:
    try:
        r = subprocess.run([gsr_path] + args, capture_output=True, text=True, timeout=5)
        return r.stdout
    except Exception as e:
        logger.debug(f"gsr {args} failed: {e}")
        return ""


def get_gsr_app_audio(gsr_path: str = "gpu-screen-recorder") -> List[AudioSource]:
    """
    Get per-application audio streams from gsr --list-application-audio.
    Format: one app name per line.
    Enriched with pw-dump data to get the real binary name.
    """
    pw_nodes = get_pw_app_nodes()
    # Build a map: node_name -> binary
    node_to_binary = {n["node_name"]: n["binary"] for n in pw_nodes if n["node_name"]}

    sources = []
    out = _run_gsr(["--list-application-audio"], gsr_path)
    for line in out.splitlines():
        name = line.strip()
        if not name or name.startswith("#"):
            continue
        binary = node_to_binary.get(name, "")
        sources.append(AudioSource(
            name=_app_display_name(binary, name),
            device=f"app:{name}",  # gsr -a requires "app:" prefix for application audio
            source_type="app",
            app_name=name,
            binary=binary,
        ))
    return sources


def get_gsr_audio_devices(gsr_path: str = "gpu-screen-recorder") -> List[AudioSource]:
    """
    Get audio devices from gsr --list-audio-devices.
    Format: device_id|Display Name
    """
    sources = []
    out = _run_gsr(["--list-audio-devices"], gsr_path)
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "|" in line:
            device_id, display_name = line.split("|", 1)
        else:
            device_id = display_name = line
        device_id    = device_id.strip()
        display_name = display_name.strip()
        lower = device_id.lower()
        if "monitor" in lower:
            stype = "monitor"
        elif any(x in lower for x in ("input", "mono", "fallback", "capture")):
            stype = "input"
        else:
            stype = "device"
        sources.append(AudioSource(
            name=display_name,
            device=device_id,
            source_type=stype,
        ))
    return sources


def detect_all_sources(gsr_path: str = "gpu-screen-recorder") -> List[AudioSource]:
    """All available audio sources, deduplicated."""
    seen = set()
    all_sources = []
    for src in get_gsr_app_audio(gsr_path) + get_gsr_audio_devices(gsr_path):
        if src.device not in seen:
            seen.add(src.device)
            all_sources.append(src)
    return all_sources


# ── Reliable game/chat audio node resolution ─────────────────────────────────

def resolve_game_audio_node(game_process: str, gsr_path: str = "gpu-screen-recorder") -> Optional[str]:
    """
    Reliably find the gpu-screen-recorder audio source for a game process.

    Strategy:
    1. Find the game's PID
    2. Look up its PipeWire audio node via pw-dump (PID match — 100% reliable)
    3. Verify that node is in gsr's --list-application-audio
    4. Return "application/<node_name>" or None

    Falls back to binary name matching if PID lookup fails.
    """
    # Step 1: find PID
    pid = get_running_pid(game_process)

    # Step 2: find PipeWire node for that PID
    node_name = None
    if pid:
        node_name = find_audio_node_for_pid(pid)
        logger.info(f"PID {pid} ({game_process}) → PipeWire node: {node_name!r}")

    # Step 3: if no PID match, try binary name
    if not node_name:
        node_name = find_audio_node_for_binary(game_process)
        logger.info(f"Binary match ({game_process}) → PipeWire node: {node_name!r}")

    if not node_name:
        return None

    # Step 4: verify gsr can see this node
    gsr_apps = [s.app_name for s in get_gsr_app_audio(gsr_path)]
    if node_name in gsr_apps:
        return f"app:{node_name}"

    logger.warning(f"Node {node_name!r} found in PipeWire but not in gsr app list")
    return f"app:{node_name}"  # try anyway


def resolve_chat_audio_node(gsr_path: str = "gpu-screen-recorder") -> Optional[str]:
    """
    Find a chat app audio node using pw-dump binary matching.
    Only returns sources that are currently producing audio (in gsr's list).
    """
    gsr_apps = {s.app_name: s.device for s in get_gsr_app_audio(gsr_path)}
    pw_nodes = get_pw_app_nodes()

    # First: look for known chat binaries in pw-dump
    for node in pw_nodes:
        binary = node.get("binary", "").lower()
        node_name = node.get("node_name", "")
        if any(chat in binary for chat in CHAT_APP_BINARIES):
            # Is it currently producing audio (in gsr list)?
            if node_name in gsr_apps:
                return f"app:{node_name}"
            # Not producing audio right now but we know the binary
            logger.info(f"Chat app {binary!r} found but not currently producing audio")

    return None


def auto_detect_tracks(
    gsr_path: str = "gpu-screen-recorder",
    running_game: str = "",
    game_display_name: str = "",
) -> list:
    """
    Auto-detect audio tracks with reliable PID-based node resolution.
    Returns list of track dicts.
    game_display_name is used as the game track label when provided
    (e.g. "CS2"); falls back to "Game" when no game is known.
    """
    tracks = []

    # ── Game track ───────────────────────────────────────────────────────
    game_device = None

    if running_game:
        # Try reliable PID-based resolution first
        game_process = running_game.lower().replace(" ", "")
        game_device = resolve_game_audio_node(game_process, gsr_path)

    # Fallback: SDL Application (common for Steam/CS2)
    if not game_device:
        gsr_apps = get_gsr_app_audio(gsr_path)
        for src in gsr_apps:
            if src.app_name.lower() == "sdl application":
                game_device = src.device
                break

    # Final fallback: monitor device
    if not game_device:
        for src in get_gsr_audio_devices(gsr_path):
            if src.source_type == "monitor":
                game_device = src.device
                break

    if game_device:
        game_label = game_display_name if game_display_name else "Game"
        tracks.append({
            "label":      game_label,
            "device":     game_device,
            "enabled":    True,
            "track_type": "game",
            "volume":     1.0,
            "muted":      False,
        })

    # ── Chat track ───────────────────────────────────────────────────────
    chat_device = resolve_chat_audio_node(gsr_path)
    if chat_device:
        chat_label = "Chat"
        pw_nodes = get_pw_app_nodes()
        for node in pw_nodes:
            binary = node.get("binary", "")
            if any(c in binary.lower() for c in CHAT_APP_BINARIES):
                chat_label = _app_display_name(binary, node.get("node_name", ""))
                break
        tracks.append({
            "label":      chat_label,
            "device":     chat_device,
            "enabled":    True,
            "track_type": "chat",
            "volume":     1.0,
            "muted":      False,
        })

    # ── Mic track ────────────────────────────────────────────────────────
    for src in get_gsr_audio_devices(gsr_path):
        if src.source_type == "input":
            tracks.append({
                "label":      "Mic",
                "device":     src.device,
                "enabled":    True,
                "track_type": "mic",
                "volume":     1.0,
                "muted":      False,
            })
            break

    return tracks


def friendly_source_name(device: str) -> str:
    if len(device) > 55:
        return device[:52] + "…"
    return device
