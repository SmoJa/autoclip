# SPDX-License-Identifier: GPL-3.0-or-later
"""
RecorderManager for AutoClip — Windows backend.
Spawns the bundled libobs recorder (obs_recorder.py run by the obs-runtime
python) and communicates via a simple stdin/stdout line protocol.
"""
import os
import subprocess
import tempfile
import threading
import logging
import time
from pathlib import Path
from datetime import datetime
from typing import Optional, Callable, List

logger = logging.getLogger(__name__)

_WATCHDOG_INTERVAL = 5.0
_RESTART_MAX       = 3
_RESTART_WINDOW    = 60.0
_SAVE_TIMEOUT      = 30.0   # libobs saves by muxing buffered packets (~1-2s); keep a
                            # generous margin for disk/relocate under load
_READY_TIMEOUT     = 130.0  # recorder polls up to 120s for the game window before
                            # printing "ready"

HDR_CODECS = {"hevc_hdr", "av1_hdr", "hevc_10bit", "av1_10bit"}


def _find_obs_python() -> Optional[str]:
    """Locate the bundled OBS-runtime python.exe that runs the libobs recorder.

    It must live alongside obs.dll in the runtime's bin/64bit so OBS resolves its
    helper exes (obs-ffmpeg-mux.exe) and data via the host-executable directory.
    """
    import sys
    override = os.environ.get("AUTOCLIP_OBS_PYTHON")
    if override and Path(override).exists():
        return override
    roots = []
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).parent)          # installed: beside AutoClip.exe
    roots.append(Path(__file__).parent.parent.parent)      # dev: repo root
    for root in roots:
        p = root / "obs-runtime" / "bin" / "64bit" / "python.exe"
        if p.exists():
            return str(p)
    return None


def _find_recorder_script(obs_python: str) -> str:
    """Path to obs_recorder.py the OBS python runs. Prefer a copy bundled in the
    obs-runtime (so a frozen build can run real source, not a .pyc inside the exe);
    fall back to the source package when running from a dev checkout."""
    bundled = Path(obs_python).parents[2] / "obs_recorder.py"   # obs-runtime\obs_recorder.py
    if bundled.exists():
        return str(bundled)
    return str(Path(__file__).parent / "obs_recorder.py")


def _resolve_process_name(game: str) -> str:
    """
    Look up the first process name for a game plugin by NAME.
    Falls back to the lowercased game string if not found.
    """
    try:
        from autoclip.games import registry as _reg
        for plugin_cls in getattr(_reg, "_plugins", []):
            if getattr(plugin_cls, "NAME", "") == game:
                names = getattr(plugin_cls, "PROCESS_NAMES", [])
                if names:
                    return names[0]
    except Exception:
        pass
    return game.lower().replace(" ", "")


def get_monitors() -> List[str]:
    """Enumerate connected monitors as readable labels, e.g.
    "Display 1 — 3440×1440 (Primary)" or "DELL U2720Q — 3840×2160".

    The Windows recorder captures the game window (WGC), so this value is
    cosmetic — it just needs to be human-readable in the Display dropdown.
    """
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32

        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        class MONITORINFOEXW(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT),
                        ("rcWork", RECT), ("dwFlags", wintypes.DWORD),
                        ("szDevice", wintypes.WCHAR * 32)]

        class DISPLAY_DEVICEW(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("DeviceName", wintypes.WCHAR * 32),
                        ("DeviceString", wintypes.WCHAR * 128),
                        ("StateFlags", wintypes.DWORD),
                        ("DeviceID", wintypes.WCHAR * 128),
                        ("DeviceKey", wintypes.WCHAR * 128)]

        MONITORINFOF_PRIMARY = 0x1

        def _model_name(device: str) -> str:
            # The monitor (model) name lives on iDevNum 0 of the adapter device.
            dd = DISPLAY_DEVICEW()
            dd.cb = ctypes.sizeof(DISPLAY_DEVICEW)
            if user32.EnumDisplayDevicesW(device, 0, ctypes.byref(dd), 0):
                name = (dd.DeviceString or "").strip()
                if name and name.lower() != "generic pnp monitor":
                    return name
            return ""

        labels = []

        def _cb(hMon, hdcMon, lprc, data):
            mi = MONITORINFOEXW()
            mi.cbSize = ctypes.sizeof(MONITORINFOEXW)
            idx = len(labels) + 1
            if user32.GetMonitorInfoW(hMon, ctypes.byref(mi)):
                w = mi.rcMonitor.right - mi.rcMonitor.left
                h = mi.rcMonitor.bottom - mi.rcMonitor.top
                head = _model_name(mi.szDevice) or f"Display {idx}"
                label = f"{head} — {w}×{h}"
                if mi.dwFlags & MONITORINFOF_PRIMARY:
                    label += " (Primary)"
                labels.append(label)
            else:
                labels.append(f"Display {idx}")
            return True

        MONITORENUMPROC = ctypes.WINFUNCTYPE(
            ctypes.c_bool, ctypes.c_ulong, ctypes.c_ulong,
            ctypes.POINTER(RECT), ctypes.c_double,
        )
        user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(_cb), 0)
        return labels if labels else ["Primary"]
    except Exception:
        return ["Primary"]


def get_audio_sources() -> List[str]:
    """List WASAPI output/input device names via sounddevice."""
    try:
        import sounddevice as sd
        return [d["name"] for d in sd.query_devices()]
    except Exception:
        return []


def is_hdr_codec(codec: str) -> bool:
    return codec in HDR_CODECS


class RecorderManager:
    def __init__(self, config):
        self.config = config
        self._process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self.on_status_change: Optional[Callable[[str], None]] = None
        self._last_game: str = ""
        self._last_process_name: str = ""

        self._should_be_running = False
        self._watchdog_thread: Optional[threading.Thread] = None
        self._watchdog_stop = threading.Event()
        self._restart_attempts = 0
        self._restart_window_start = 0.0
        self._consecutive_save_failures = 0
        self._stderr_file = None
        self._saves_pending = 0
        # Serializes save_clip calls: concurrent saves would race to read the
        # same stdout pipe and mis-attribute "saved" responses
        self._save_lock = threading.Lock()

    def is_installed(self) -> bool:
        return _find_obs_python() is not None

    def is_running(self) -> bool:
        with self._lock:
            if self._process is None:
                return False
            if self._process.poll() is not None:
                try:
                    err = self._process.stderr.read().decode(errors="replace") if self._process.stderr else ""
                    if err:
                        logger.warning(f"Recorder stderr: {err[:500]}")
                except Exception:
                    pass
                self._process = None
                return False
            return True

    def start(self, game: str = "Unknown") -> bool:
        self._last_game = game

        fresh_start = not self._should_be_running
        if fresh_start:
            self._restart_attempts = 0
            self._restart_window_start = time.time()

        obs_python = _find_obs_python()
        if not obs_python:
            logger.error("OBS runtime python not found (bundle missing)")
            self._emit_status("error: not installed")
            return False

        if self.is_running():
            self._should_be_running = True
            self._start_watchdog()
            return True

        process_name = _resolve_process_name(game)
        self._last_process_name = process_name

        codec        = getattr(self.config, "gpu_recorder_codec", "hevc")
        fps          = getattr(self.config, "gpu_recorder_fps", 60)
        bitrate_kbps = getattr(self.config, "gpu_recorder_bitrate_kbps", 30000)
        buffer_secs  = getattr(self.config, "clip_length_seconds", 30)
        rate_control = getattr(self.config, "nvenc_rate_control", "cbr")
        cq_level     = getattr(self.config, "nvenc_cq_level", 20)
        max_bitrate  = getattr(self.config, "nvenc_max_bitrate_kbps", 60000)
        preset       = getattr(self.config, "nvenc_preset", "p5")
        multipass    = getattr(self.config, "nvenc_multipass", "qres")
        profile      = getattr(self.config, "nvenc_profile", "auto")
        bframes      = getattr(self.config, "nvenc_bframes", 2)

        # Run the libobs recorder script directly with the bundled OBS python. It's
        # self-contained (stdlib + ctypes only), so no package import / cwd /
        # PYTHONPATH is needed — which is also what lets it run on embeddable python
        # (whose restricted sys.path ignores PYTHONPATH).
        recorder_script = _find_recorder_script(obs_python)
        cmd = [
            obs_python, recorder_script,
            "--process",      process_name,
            "--fps",          str(fps),
            "--bitrate",      str(bitrate_kbps * 1000),
            "--buffer-secs",  str(buffer_secs),
            "--codec",        codec,
            "--rate-control", rate_control,
            "--cq",           str(cq_level),
            "--max-bitrate",  str(max_bitrate * 1000),
            "--preset",       preset,
            "--multipass",    multipass,
            "--profile",      profile,
            "--bframes",      str(bframes),
        ]

        # Pass the primary display size as the initial canvas; the recorder then
        # resizes the canvas to the captured game window's actual resolution.
        try:
            import ctypes
            sw = ctypes.windll.user32.GetSystemMetrics(0)
            sh = ctypes.windll.user32.GetSystemMetrics(1)
            if sw > 0 and sh > 0:
                cmd += ["--width", str(sw), "--height", str(sh)]
        except Exception:
            pass

        # Audio: build a structured config the recorder turns into OBS sources.
        # Each enabled track becomes a process capture (app:<exe> ->
        # wasapi_process_output_capture), an output device (wasapi_output_capture),
        # or the mic (wasapi_input_capture). `separate` -> one output track per source.
        import json
        audio_tracks = getattr(self.config, "audio_tracks", None) or []
        a_tracks = []
        for track in audio_tracks:
            t = track if isinstance(track, dict) else vars(track)
            if not t.get("enabled", True) or not t.get("device", ""):
                continue
            role = t.get("track_type", "game")
            dev = t["device"]
            if role == "mic":
                kind, ident = "in", dev
            elif dev.startswith("app:"):
                kind, ident = "app", dev[4:]
            else:
                kind, ident = "out", dev
            a_tracks.append({
                "role": role, "kind": kind, "id": ident,
                "vol": float(t.get("volume", 1.0)),
                "mute": bool(t.get("muted", False)),
            })
        if a_tracks:
            # The GUI saves the mode in audio_track_mode ("separate" | "mixed_immediate"
            # | "mixed_deferred"); separate_audio_tracks is a stale legacy field.
            separate = getattr(self.config, "audio_track_mode", "separate") == "separate"
            cmd += ["--audio-config", json.dumps({
                "separate": separate,
                "tracks": a_tracks,
            })]

        logger.info(f"Starting recorder: {' '.join(cmd)}")

        # libobs diagnostics go to a log file (the recorder routes obs's logging to
        # its stderr); an unread stderr PIPE would fill up and block the recorder.
        stderr_log = Path(tempfile.gettempdir()) / "autoclip-recorder-stderr.log"
        env = os.environ.copy()

        try:
            with self._lock:
                self._stderr_file = open(stderr_log, "a", encoding="utf-8", errors="replace")
                self._process = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=self._stderr_file,
                    text=True,
                    bufsize=1,
                    env=env,
                    # The recorder is a console python.exe; from a windowed (no-console)
                    # GUI, Windows would spawn a visible console for it. Suppress it —
                    # the stdin/stdout protocol pipes still work.
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )

            # Wait for "ready" — covers the recorder's window-wait poll
            ready = self._wait_for_line("ready", timeout=_READY_TIMEOUT)
            if not ready:
                err = ""
                try:
                    err = stderr_log.read_text(encoding="utf-8", errors="replace")[-2000:]
                except Exception:
                    pass
                logger.error(f"Recorder failed to start. stderr: {err}")
                self._process = None
                self._emit_status("error: recorder failed to start")
                return False

            self._should_be_running = True
            self._start_watchdog()
            self._emit_status("recording")
            logger.info(f"Recorder ready (pid={self._process.pid}, process={process_name!r})")
            return True

        except Exception as e:
            logger.error(f"Failed to launch recorder: {e}")
            self._emit_status(f"error: {e}")
            return False

    def stop(self):
        self._should_be_running = False
        self._stop_watchdog()
        with self._lock:
            proc = self._process
            self._process = None
            stderr_file = self._stderr_file
            self._stderr_file = None

        if proc and proc.poll() is None:
            # Everything below runs in the background — stop() is called on the
            # Qt main thread on game exit, and blocking it for the encode queue
            # froze the GUI. Taking _save_lock first means queued saves get to
            # send their commands and collect responses before "stop" is sent;
            # writing "stop" earlier made the recorder exit before reading them.
            def _graceful_shutdown():
                with self._save_lock:
                    try:
                        proc.stdin.write("stop\n")
                        proc.stdin.flush()
                    except Exception:
                        pass
                try:
                    proc.wait(timeout=_SAVE_TIMEOUT)
                except Exception:
                    proc.kill()
                if stderr_file:
                    try:
                        stderr_file.close()
                    except Exception:
                        pass

            threading.Thread(target=_graceful_shutdown, daemon=True).start()
        elif stderr_file:
            try:
                stderr_file.close()
            except Exception:
                pass
        self._emit_status("stopped")

    def save_clip(self, event_meta: str = "") -> bool:
        """Save the replay buffer to a timestamped file in the output directory."""
        if not self.is_running():
            logger.warning("Recorder not running — cannot save clip")
            return False

        output_dir = self._make_output_dir(self._last_game)
        # Millisecond suffix: saves complete in well under a second now, so
        # second-granularity names collide when triggers fire back-to-back
        ts          = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")[:-3]
        out_path    = output_dir / f"replay_{ts}.mp4"

        try:
            with self._lock:
                proc = self._process
            self._saves_pending += 1
            try:
                with self._save_lock:
                    proc.stdin.write(f"save {out_path}\n")
                    proc.stdin.flush()
                    # pass proc: stop() clears self._process on game exit, but
                    # the recorder still finishes and answers in-flight saves
                    response = self._wait_for_line(None, timeout=_SAVE_TIMEOUT,
                                                   proc=proc)
            finally:
                self._saves_pending -= 1
            if response and response.startswith("saved"):
                logger.info(f"Clip saved: {out_path} (meta: {event_meta or 'manual'})")
                return True
            else:
                logger.error(f"Save failed — response: {response!r}")
                return False
        except Exception as e:
            logger.error(f"Save command failed: {e}")
            return False

    def report_save_success(self):
        self._consecutive_save_failures = 0

    def report_save_failure(self):
        self._consecutive_save_failures += 1
        logger.warning(f"Save produced no output ({self._consecutive_save_failures} consecutive)")
        if self._consecutive_save_failures >= 2:
            logger.warning("Recorder appears stuck — forcing restart")
            self._consecutive_save_failures = 0
            threading.Thread(
                target=self._force_restart, args=(self._last_game,), daemon=True
            ).start()

    # ── Internal helpers ────────────────────────────────────────────────────

    def _wait_for_line(self, expected: Optional[str], timeout: float,
                       proc=None) -> Optional[str]:
        """
        Read lines from the recorder's stdout until we get one starting with
        `expected`, or until `timeout` seconds elapse. If expected is None,
        return the first line received. Pass `proc` explicitly when the wait
        must survive stop() clearing self._process (in-flight saves).
        """
        deadline = time.monotonic() + timeout
        if proc is None:
            with self._lock:
                proc = self._process
        if proc is None:
            return None
        try:
            proc.stdout._sock = None  # not a socket; ensure non-blocking isn't assumed
        except Exception:
            pass
        while time.monotonic() < deadline:
            try:
                line = proc.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if line:
                    logger.debug(f"recorder: {line}")
                    if expected is None or line.startswith(expected):
                        return line
                    if line.startswith("error:"):
                        logger.error(f"Recorder error: {line}")
                        return line
            except Exception:
                break
        return None

    def _make_output_dir(self, game: str) -> Path:
        date_str = datetime.now().strftime("%d-%b-%Y")
        safe = "".join(c if c.isalnum() or c in "-_ " else "_" for c in game)
        path = Path(self.config.output_dir) / safe / date_str
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _emit_status(self, status: str):
        if self.on_status_change:
            self.on_status_change(status)

    def _force_restart(self, game: str):
        self.stop()
        time.sleep(1.0)
        self.start(game)

    def _start_watchdog(self):
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            return
        self._watchdog_stop.clear()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog, name="recorder-watchdog", daemon=True
        )
        self._watchdog_thread.start()

    def _stop_watchdog(self):
        self._watchdog_stop.set()

    def _watchdog(self):
        while not self._watchdog_stop.wait(_WATCHDOG_INTERVAL):
            if not self._should_be_running:
                return
            with self._lock:
                proc = self._process
            if proc is None or proc.poll() is None:
                continue

            try:
                err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
            except Exception:
                err = ""
            logger.warning(
                f"Recorder exited unexpectedly (rc={proc.returncode})"
                + (f" — {err.strip()[-200:]}" if err.strip() else "")
            )
            with self._lock:
                self._process = None
            self._emit_status("error: recorder died")

            now = time.time()
            if now - self._restart_window_start > _RESTART_WINDOW:
                self._restart_attempts = 0
                self._restart_window_start = now
            self._restart_attempts += 1

            if self._restart_attempts > _RESTART_MAX:
                logger.error(
                    f"Recorder crashed {_RESTART_MAX} times in "
                    f"{_RESTART_WINDOW:.0f}s — giving up."
                )
                self._emit_status("error: recorder keeps crashing")
                self._should_be_running = False
                return

            delay = min(2.0 * self._restart_attempts, 10.0)
            logger.info(f"Restarting recorder in {delay:.0f}s (attempt {self._restart_attempts}/{_RESTART_MAX})...")
            if self._watchdog_stop.wait(delay):
                return
            if not self._should_be_running:
                return

            self._emit_status("restarting recorder...")
            self.start(self._last_game)

    # ── Compatibility stubs ─────────────────────────────────────────────────

    @staticmethod
    def parse_event_meta(event: str) -> dict:
        parts = event.split("|")
        if len(parts) == 4:
            return {"trigger": parts[0], "mode": parts[1], "map": parts[2], "round": parts[3]}
        return {"trigger": event, "mode": "", "map": "", "round": ""}

    @staticmethod
    def build_clip_filename(game: str, meta: dict, clip_length: int = 30, post_event: int = 7) -> str:
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        def safe(s):
            return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(s))
        parts = [safe(game)]
        if meta.get("mode"):    parts.append(safe(meta["mode"]))
        if meta.get("map"):     parts.append(safe(meta["map"]))
        if meta.get("round"):   parts.append(f"r{safe(meta['round'])}")
        if meta.get("trigger"): parts.append(safe(meta["trigger"]))
        parts.append(f"p{post_event}")
        parts.append(ts)
        return "_".join(p for p in parts if p)
