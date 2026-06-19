# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-application audio capture for the audio triggers (Windows).

`AppAudioStream` mirrors the slice of the `sounddevice.InputStream` API the triggers
use — `with AppAudioStream(exe, rate) as s: block, _ = s.read(n)` — but the samples
come from ONE application via the bundled obs-runtime helper (libobs process
loopback) instead of an audio device. So a trigger can capture e.g. just Discord.

Windows only; on Linux the triggers offer only device sources, so this isn't used.
"""
import os
import subprocess
import sys
from pathlib import Path


def _helper_cmd(exe: str, rate: int):
    from autoclip.core.recorder_windows import _find_obs_python
    obs_python = _find_obs_python()
    if not obs_python:
        raise RuntimeError("obs-runtime python not found — can't capture app audio")
    helper = Path(obs_python).resolve().parents[2] / "obs_audio_capture.py"
    if not helper.exists():
        raise RuntimeError(f"audio capture helper missing: {helper}")
    return [obs_python, str(helper), exe, "--rate", str(rate)]


class AppAudioStream:
    """Drop-in for sounddevice.InputStream that reads one app's audio (mono float32)."""

    def __init__(self, exe: str, samplerate: int, blocksize: int = 0,
                 dtype: str = "float32", **_ignored):
        self._rate = samplerate
        self._dtype = dtype          # "float32" (Reactions) or "int16" (Phrases/vosk)
        self._buf = bytearray()
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self._proc = subprocess.Popen(
            _helper_cmd(exe, samplerate),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=flags,
        )

    def read(self, frames: int):
        """Return (ndarray shape (frames, 1) float32, overflowed=False). Blocks."""
        import numpy as np
        need = frames * 4
        while len(self._buf) < need:
            if self._proc.poll() is not None:
                raise RuntimeError("app audio capture process ended")
            chunk = self._proc.stdout.read(need - len(self._buf))
            if not chunk:
                raise RuntimeError("app audio capture stream closed")
            self._buf.extend(chunk)
        raw = bytes(self._buf[:need])
        del self._buf[:need]
        arr = np.frombuffer(raw, dtype=np.float32)   # helper always streams float32
        if self._dtype == "int16":
            arr = (np.clip(arr, -1.0, 1.0) * 32767.0).astype(np.int16)
        return arr.reshape(-1, 1), False

    def close(self):
        if self._proc.poll() is None:
            try:
                self._proc.stdin.write(b"stop\n"); self._proc.stdin.flush()
            except Exception:
                pass
            try:
                self._proc.wait(timeout=1.5)
            except Exception:
                try: self._proc.kill()
                except Exception: pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
