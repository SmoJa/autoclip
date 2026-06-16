# SPDX-License-Identifier: GPL-3.0-or-later
"""
Audio source detection for AutoClip — Windows (WASAPI).
Enumerates devices via sounddevice. No auto-detection of per-process
audio; users select sources manually in the UI.
"""
import os
import logging
from dataclasses import dataclass
from typing import List, Optional, Dict

logger = logging.getLogger(__name__)

# System/shell processes that have windows but aren't real audio apps — hidden from
# the per-application audio list so it only shows games, chat apps, browsers, etc.
_SKIP_APP_PROCS = {
    "explorer", "dwm", "textinputhost", "applicationframehost", "searchhost",
    "searchapp", "startmenuexperiencehost", "shellexperiencehost", "systemsettings",
    "lockapp", "widgets", "widgetservice", "sihost", "ctfmon", "taskmgr",
    "python", "pythonw",        # AutoClip itself
    "conhost", "cmd", "powershell", "pwsh", "windowsterminal",  # consoles/terminals
}

CHAT_APP_BINARIES = {
    "discord", "goofcord", "armcord", "vesktop",
    "teamspeak", "ts3client", "mumble", "ventrilo",
    "zoom", "teams", "skype", "slack",
}

CHAT_APP_NAMES = {
    "discord", "goofcord", "teamspeak", "mumble",
    "zoom", "teams", "skype", "slack",
}

FRIENDLY_APP_NAMES: Dict[str, str] = {
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
    "firefox":    "Firefox",
    "chrome":     "Chrome",
    "chromium":   "Chromium",
    "brave":      "Brave",
    "opera":      "Opera",
    "spotify":    "Spotify",
    "vlc":        "VLC",
    "mpv":        "mpv",
    "steam":      "Steam",
}


@dataclass
class AudioSource:
    name:        str
    device:      str    # device name passed to the recorder
    source_type: str    # "monitor" | "input" | "device"
    app_name:    str = ""
    binary:      str = ""
    pid:         int = 0


def _enumerate_wasapi() -> List[AudioSource]:
    """Return all WASAPI audio devices via sounddevice."""
    try:
        import sounddevice as sd
    except ImportError:
        logger.warning("sounddevice not installed — no audio devices available")
        return []

    sources = []
    try:
        hostapis = sd.query_hostapis()
        wasapi_idx = next(
            (i for i, a in enumerate(hostapis) if "wasapi" in a["name"].lower()),
            None,
        )

        for dev in sd.query_devices():
            if wasapi_idx is not None and dev["hostapi"] != wasapi_idx:
                continue
            if dev["max_output_channels"] > 0:
                sources.append(AudioSource(
                    name=dev["name"],
                    device=dev["name"],
                    source_type="monitor",
                ))
            elif dev["max_input_channels"] > 0:
                sources.append(AudioSource(
                    name=dev["name"],
                    device=dev["name"],
                    source_type="input",
                ))
    except Exception as e:
        logger.warning(f"Audio device enumeration failed: {e}")

    return sources


def _enumerate_app_sources() -> List[AudioSource]:
    """Return running applications that can be captured per-process via OBS
    wasapi_process_output_capture. Enumerates visible top-level windows (which is
    how OBS itself targets process audio), one entry per distinct executable.

    Mirrors the Linux app-node listing: device uses the ``app:`` prefix.
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    seen: Dict[str, tuple] = {}   # exe stem -> (exe_basename, window_title)

    def _cb(hwnd, _):
        try:
            if not user32.IsWindowVisible(hwnd) or user32.GetWindowTextLengthW(hwnd) == 0:
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
            if not h:
                return True
            try:
                buf = ctypes.create_unicode_buffer(260)
                size = wintypes.DWORD(260)
                if not kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    return True
                exe = os.path.basename(buf.value)
            finally:
                kernel32.CloseHandle(h)
            stem = os.path.splitext(exe)[0].lower()
            if stem in _SKIP_APP_PROCS or stem in seen:
                return True
            title = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, title, 256)
            seen[stem] = (exe, title.value)
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(WNDENUMPROC(_cb), 0)
    except Exception as e:
        logger.warning(f"App audio enumeration failed: {e}")
        return []

    sources = []
    for stem, (exe, title) in sorted(seen.items()):
        # A window title that's a file path (e.g. a console window) is not a friendly
        # app name — fall back to the executable stem instead.
        title_ok = title and "\\" not in title and "/" not in title
        friendly = FRIENDLY_APP_NAMES.get(stem) or (title if title_ok else None) or stem.title()
        sources.append(AudioSource(
            name=f"{friendly}  (app)",
            device=f"app:{exe.lower()}",
            source_type="app",
            app_name=friendly,
            binary=stem,
        ))
    return sources


# ── Public API (mirrors audio_linux.py) ────────────────────────────────────

def get_gsr_audio_devices(gsr_path: str = "") -> List[AudioSource]:
    """Windows: enumerate WASAPI devices. gsr_path is ignored."""
    return _enumerate_wasapi()


def get_pw_app_nodes() -> List[Dict]:
    """Windows: no PipeWire equivalent."""
    return []


def detect_all_sources(gsr_path: str = "") -> List[AudioSource]:
    """Per-application sources first (games, chat apps), then raw WASAPI devices."""
    return _enumerate_app_sources() + _enumerate_wasapi()


def resolve_game_audio_node(game_process: str, gsr_path: str = "") -> Optional[str]:
    """Windows: no automatic per-process resolution. User selects manually."""
    return None


def auto_detect_tracks(
    gsr_path: str = "",
    running_game: str = "",
    game_display_name: str = "",
) -> list:
    """
    Return default tracks using the system default output and input devices.
    Since Windows has no PipeWire-style per-process routing, we can't
    auto-detect which output device a game is using — the user picks manually.
    """
    tracks = []
    try:
        import sounddevice as sd
        default_out = sd.query_devices(kind="output")
        default_in  = sd.query_devices(kind="input")

        game_label = game_display_name if game_display_name else "Game"
        tracks.append({
            "label":      game_label,
            "device":     default_out["name"],
            "enabled":    True,
            "track_type": "game",
            "volume":     1.0,
            "muted":      False,
        })
        tracks.append({
            "label":      "Mic",
            "device":     default_in["name"],
            "enabled":    True,
            "track_type": "mic",
            "volume":     1.0,
            "muted":      False,
        })
    except Exception as e:
        logger.warning(f"Default audio device detection failed: {e}")

    return tracks


def friendly_source_name(device: str) -> str:
    if len(device) > 55:
        return device[:52] + "…"
    return device
