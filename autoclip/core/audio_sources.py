# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared audio source backend for audio-trigger plugins.

One place to (a) list selectable audio sources for a plugin's dropdowns and
(b) open a capture stream for a chosen source — so every audio trigger sees the
same devices + app sources and no plugin re-implements the plumbing.

A "source" the user picks is one of:
  - ""                    default device (mic) / auto-detected monitor (chat)
  - a device name         a sounddevice input/monitor device
  - "app:<exe>"           one application's audio (Windows process loopback)

Capture is uniform: `open_stream(...)` returns an object with the slice of the
`sounddevice.InputStream` API the triggers use — `with s: block,_ = s.read(n)`.
Cross-platform: device capture via sounddevice everywhere; app capture via libobs
on Windows (Linux app capture is a future addition).
"""
import sys
import logging

logger = logging.getLogger(__name__)


# ── source enumeration (for dropdowns) ──────────────────────────────────────
def app_sources():
    """[(label, "app:<exe>")] for running apps (Windows). [] on other platforms."""
    if sys.platform != "win32":
        return []
    try:
        from autoclip.core.audio_windows import _enumerate_app_sources
        return [(s.name, s.device) for s in _enumerate_app_sources()]
    except Exception:
        return []


def _friendly_names(config):
    """device name -> friendlier label (uses the recorder's audio-device names)."""
    try:
        from autoclip.core.audio import get_gsr_audio_devices
        gsr_path = getattr(config, "gpu_recorder_path", "gpu-screen-recorder")
        return {src.device: src.name for src in get_gsr_audio_devices(gsr_path)}
    except Exception:
        return {}


def _chat_hint():
    """Friendly name of a detected chat app, for the monitor 'auto-detect' label."""
    try:
        from autoclip.core.audio import get_pw_app_nodes, CHAT_APP_BINARIES, FRIENDLY_APP_NAMES
        for node in get_pw_app_nodes():
            binary = node.get("binary", "").lower()
            if any(c in binary for c in CHAT_APP_BINARIES):
                return FRIENDLY_APP_NAMES.get(binary, binary.capitalize())
    except Exception:
        pass
    return ""


def input_sources(config):
    """Sources for a mic picker: default + input devices + apps."""
    friendly = _friendly_names(config)
    devs = [("System default", "")]
    try:
        import sounddevice as sd
        for d in sd.query_devices():
            if d["max_input_channels"] > 0 and "monitor" not in d["name"].lower():
                name = d["name"]
                devs.append((friendly.get(name, name), name))
    except Exception:
        pass
    return devs + app_sources()


def monitor_sources(config):
    """Sources for a chat picker: auto-detect monitor + monitors + apps + other inputs."""
    friendly = _friendly_names(config)
    hint = _chat_hint()
    label0 = "Auto-detect monitor" + (f"  ({hint} detected)" if hint else "")
    monitors = [(label0, "")]
    others = []
    try:
        import sounddevice as sd
        for d in sd.query_devices():
            if d["max_input_channels"] > 0:
                name = d["name"]
                label = friendly.get(name, name)
                (monitors if "monitor" in name.lower() else others).append((label, name))
    except Exception:
        pass
    return monitors + app_sources() + others


# ── resolution + capture ────────────────────────────────────────────────────
def resolve_source(value, monitor=False):
    """Turn a saved source value into a capture spec: "app:<exe>" passes through;
    a device name / "" resolves to a sounddevice device index (or None=default)."""
    if isinstance(value, str) and value.startswith("app:"):
        return value
    try:
        import sounddevice as sd
        if value:
            for i, d in enumerate(sd.query_devices()):
                if value in d["name"] and d["max_input_channels"] > 0:
                    return i
            logger.warning(f"Audio device '{value}' not found — using default")
            return None
        if monitor:
            for i, d in enumerate(sd.query_devices()):
                if "monitor" in d["name"].lower() and d["max_input_channels"] > 0:
                    return i
            logger.warning("No monitor device found for chat audio")
    except Exception as e:
        logger.warning(f"Audio device resolution failed: {e}")
    return None


def open_stream(spec, rate, blocksize, dtype="float32"):
    """A capture stream (.read(n) -> (ndarray, overflow)) for an app or a device."""
    if isinstance(spec, str) and spec.startswith("app:"):
        from autoclip.core.app_audio_capture import AppAudioStream
        return AppAudioStream(spec[4:], rate, blocksize=blocksize, dtype=dtype)
    import sounddevice as sd
    return sd.InputStream(device=spec, channels=1, samplerate=rate,
                          blocksize=blocksize, dtype=dtype)
