# SPDX-License-Identifier: GPL-3.0-or-later
import subprocess
import threading
import logging
import shutil
import signal as _signal
import time
from pathlib import Path
from datetime import datetime
from typing import Optional, Callable, List

logger = logging.getLogger(__name__)

_WATCHDOG_INTERVAL = 5.0   # seconds between liveness checks
_RESTART_MAX       = 3     # max crash-restarts before giving up
_RESTART_WINDOW    = 60.0  # sliding window (seconds) for crash counting

HDR_CODECS = {"hevc_hdr", "av1_hdr", "hevc_10bit", "av1_10bit",
              "hevc_hdr_vulkan", "av1_hdr_vulkan", "hevc_10bit_vulkan", "av1_10bit_vulkan"}

CBR_CODECS = {"cbr", "vbr"}  # modes that use integer kbps for -q


def get_monitors() -> List[str]:
    try:
        result = subprocess.run(["xrandr", "--listmonitors"], capture_output=True, text=True)
        monitors = []
        for line in result.stdout.splitlines():
            parts = line.strip().split()
            if parts and parts[0].rstrip(":").isdigit():
                monitors.append(parts[-1])
        return monitors if monitors else []
    except Exception:
        pass
    # Wayland fallback via kscreen-doctor
    try:
        result = subprocess.run(["kscreen-doctor", "-o"], capture_output=True, text=True)
        monitors = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("Output:"):
                parts = line.split()
                if len(parts) >= 3:
                    monitors.append(parts[2])
        return monitors if monitors else ["screen"]
    except Exception:
        return ["screen"]


def get_audio_sources() -> List[str]:
    try:
        result = subprocess.run(["pactl", "list", "short", "sources"], capture_output=True, text=True)
        return [line.split()[1] for line in result.stdout.splitlines() if len(line.split()) >= 2]
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
        self._no_audio_mode = False
        self._audio_poll_timer: Optional[threading.Timer] = None
        self._last_game: str = ""

        # Watchdog state
        self._should_be_running = False
        self._watchdog_thread: Optional[threading.Thread] = None
        self._watchdog_stop = threading.Event()
        self._restart_attempts = 0
        self._restart_window_start = 0.0

        # Silent-failure detection: count consecutive saves that produced no file
        self._consecutive_save_failures = 0

    def is_installed(self) -> bool:
        parts = self.config.gpu_recorder_path.strip().split()
        return shutil.which(parts[0]) is not None

    def is_running(self) -> bool:
        with self._lock:
            if self._process is None:
                return False
            if self._process.poll() is not None:
                try:
                    stderr = self._process.stderr.read().decode(errors="replace") if self._process.stderr else ""
                    if stderr:
                        logger.warning(f"Recorder stderr: {stderr[:500]}")
                except Exception:
                    pass
                self._process = None
                return False
            return True

    def start(self, game: str = "Unknown") -> bool:
        self._last_game = game
        # A call while _should_be_running is False is a fresh external start —
        # reset the crash counter so the watchdog gets a clean slate.
        fresh_start = not self._should_be_running
        if fresh_start:
            self._restart_attempts = 0
            self._restart_window_start = time.time()

        if not self.is_installed():
            logger.error("gpu-screen-recorder not found")
            self._emit_status("error: not installed")
            return False
        if self.is_running():
            self._should_be_running = True
            self._start_watchdog()
            return True

        output_dir = self._make_output_dir(game)
        clip_len    = self.config.clip_length_seconds
        buffer_secs = clip_len

        cmd = self.config.gpu_recorder_path.strip().split()

        monitor = getattr(self.config, "monitor", "screen") or "screen"
        cmd += ["-w", monitor]

        codec = getattr(self.config, "gpu_recorder_codec", "hevc_hdr")
        quality_mode = getattr(self.config, "gpu_recorder_quality_mode", "cbr")
        bitrate = getattr(self.config, "gpu_recorder_bitrate_kbps", 30000)
        quality_preset = getattr(self.config, "gpu_recorder_quality_preset", "very_high")
        color_range = getattr(self.config, "gpu_recorder_color_range", "full")
        tune = getattr(self.config, "gpu_recorder_tune", "quality")

        # -q is kbps integer for cbr/vbr, named preset for qp/auto
        if quality_mode in CBR_CODECS:
            q_value = str(bitrate)
        else:
            q_value = quality_preset

        cmd += [
            "-f", str(self.config.gpu_recorder_fps),
            "-bm", quality_mode,
            "-q", q_value,
            "-k", codec,
            "-cr", color_range,
            "-tune", tune,
            "-r", str(buffer_secs),
            "-o", str(output_dir),
            "-c", "mkv",
        ]

        # Build audio track arguments — validate each device, fall back gracefully
        audio_tracks = getattr(self.config, "audio_tracks", None)
        if audio_tracks:
            valid_app_devices, valid_hw_devices = self._get_valid_audio_devices()
            all_valid = valid_app_devices | valid_hw_devices
            tracks_added = 0
            for track in audio_tracks:
                if isinstance(track, dict):
                    enabled    = track.get("enabled", True)
                    device     = track.get("device", "")
                    track_type = track.get("track_type", "custom")
                else:
                    enabled    = getattr(track, "enabled", True)
                    device     = getattr(track, "device", "")
                    track_type = getattr(track, "track_type", "custom")
                if not enabled or not device or device in ('', 'none', 'default', 'default_input', 'default_output'):
                    continue
                # gsr uses "app:Name" prefix for application audio
                # For validation, compare bare name against --list-application-audio output
                gsr_device   = device  # pass as-is to gsr (already has app: prefix)
                check_device = device[4:] if device.startswith("app:") else device
                if all_valid and check_device not in all_valid:
                    # App audio unavailable — fall back to monitor for game/chat
                    if track_type in ("game", "chat") and valid_hw_devices:
                        monitor = next(
                            (d for d in valid_hw_devices if "monitor" in d.lower()),
                            None
                        )
                        if monitor:
                            logger.info(
                                f"App audio {check_device!r} unavailable, "
                                f"falling back to monitor: {monitor!r}"
                            )
                            cmd += ["-a", monitor]
                        else:
                            logger.warning(f"Skipping unavailable audio device: {check_device!r}")
                    else:
                        logger.warning(f"Skipping unavailable audio device: {check_device!r}")
                    continue
                cmd += ["-a", gsr_device]
                tracks_added += 1
        else:
            audio_source = getattr(self.config, "audio_source", "default")
            if audio_source and audio_source not in ('', 'none', 'default'):
                cmd += ["-a", audio_source]

        logger.info(f"Starting recorder: {' '.join(cmd)}")

        try:
            with self._lock:
                self._process = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
            logger.info(f"Launched pid={self._process.pid}")

            time.sleep(2)
            if self._process.poll() is not None:
                out = self._process.stdout.read().decode(errors="replace")
                err = self._process.stderr.read().decode(errors="replace")
                # If crash is due to audio device, retry without audio
                if "is not a valid audio device" in err:
                    logger.warning("Audio device rejected by recorder — retrying without audio")
                    self._process = None
                    # Build cmd without any -a flags
                    clean_cmd = []
                    skip_next = False
                    for token in cmd:
                        if skip_next:
                            skip_next = False
                            continue
                        if token == "-a":
                            skip_next = True
                            continue
                        clean_cmd.append(token)
                    logger.info(f"Retrying: {' '.join(clean_cmd)}")
                    self._process = subprocess.Popen(
                        clean_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
                    )
                    time.sleep(2)
                    if self._process.poll() is not None:
                        out2 = self._process.stdout.read().decode(errors="replace")
                        err2 = self._process.stderr.read().decode(errors="replace")
                        logger.error(f"Recorder exited on retry!\nstderr: {err2}")
                        self._process = None
                        self._emit_status("error: recorder crashed")
                        return False
                    logger.info("Recorder running without audio (will restart when audio stream is ready)")
                    self._emit_status("recording (no audio)")
                    self._no_audio_mode = True
                    self._should_be_running = True
                    self._start_watchdog()
                    # Only start polling if not already polling
                    if self._audio_poll_timer is None:
                        self._schedule_audio_poll()
                    return True
                logger.error(f"Recorder exited immediately!\nstdout: {out}\nstderr: {err}")
                self._process = None
                self._emit_status("error: recorder crashed")
                return False

            self._should_be_running = True
            self._start_watchdog()
            self._emit_status("recording")
            return True
        except Exception as e:
            logger.error(f"Failed to launch recorder: {e}")
            self._emit_status(f"error: {e}")
            return False

    def stop(self):
        self._should_be_running = False
        self._stop_watchdog()
        # Only cancel poll and reset no-audio if stop is called externally
        # (not from within the poll itself — checked via _no_audio_mode)
        if self._audio_poll_timer:
            self._audio_poll_timer.cancel()
            self._audio_poll_timer = None
        self._no_audio_mode = False
        with self._lock:
            if self._process and self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                self._process = None
        self._emit_status("stopped")

    def _start_watchdog(self):
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            return
        self._watchdog_stop.clear()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog, name="gsr-watchdog", daemon=True)
        self._watchdog_thread.start()
        logger.debug("Watchdog started")

    def _stop_watchdog(self):
        self._watchdog_stop.set()

    def report_save_success(self):
        """Reset silent-failure counter after a clip was written successfully."""
        self._consecutive_save_failures = 0

    def report_save_failure(self):
        """Called when a save signal produced no output file.
        After 2 consecutive failures, force-restart gsr — it is likely stuck."""
        self._consecutive_save_failures += 1
        logger.warning(
            f"Save produced no output ({self._consecutive_save_failures} consecutive)"
        )
        if self._consecutive_save_failures >= 2:
            logger.warning("gsr appears stuck — forcing restart")
            self._consecutive_save_failures = 0
            game = self._last_game
            threading.Thread(
                target=self._force_restart, args=(game,), daemon=True
            ).start()

    def _force_restart(self, game: str):
        self.stop()
        time.sleep(1.0)
        self.start(game)

    def _watchdog(self):
        while not self._watchdog_stop.wait(_WATCHDOG_INTERVAL):
            if not self._should_be_running:
                return

            with self._lock:
                proc = self._process
            if proc is None or proc.poll() is None:
                continue

            # Process exited without us asking it to
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

            # Sliding-window crash counter
            now = time.time()
            if now - self._restart_window_start > _RESTART_WINDOW:
                self._restart_attempts = 0
                self._restart_window_start = now
            self._restart_attempts += 1

            if self._restart_attempts > _RESTART_MAX:
                logger.error(
                    f"Recorder crashed {_RESTART_MAX} times within "
                    f"{_RESTART_WINDOW:.0f}s — giving up. "
                    "Check gpu-screen-recorder setup."
                )
                self._emit_status("error: recorder keeps crashing")
                self._should_be_running = False
                return

            delay = min(2.0 * self._restart_attempts, 10.0)
            logger.info(
                f"Restarting recorder in {delay:.0f}s "
                f"(attempt {self._restart_attempts}/{_RESTART_MAX})..."
            )
            # Interruptible delay — wakes immediately if stop() is called
            if self._watchdog_stop.wait(delay):
                return
            if not self._should_be_running:
                return

            self._emit_status("restarting recorder...")
            self.start(self._last_game)

    def _schedule_audio_poll(self, interval: float = 3.0):
        """Poll every few seconds; restart with audio once stream is available."""
        def _poll():
            # Stop polling if we're no longer in no-audio mode or recorder stopped
            if not self._no_audio_mode:
                return
            if not self.is_running():
                # Recorder stopped externally — stop polling
                self._no_audio_mode = False
                return

            app_devices, _ = self._get_valid_audio_devices()
            audio_tracks = getattr(self.config, "audio_tracks", None) or []
            wanted = set()
            for track in audio_tracks:
                d = track.get("device", "") if isinstance(track, dict) else getattr(track, "device", "")
                e = track.get("enabled", True) if isinstance(track, dict) else getattr(track, "enabled", True)
                if e and d and d not in ("", "none", "default", "default_input", "default_output"):
                    wanted.add(d)

            # app_devices from gsr are bare names; wanted may have "app:" prefix
            wanted_bare = {d[4:] if d.startswith("app:") else d for d in wanted}
            available = wanted_bare & app_devices
            if available:
                logger.info(f"Audio stream available: {available} — restarting with audio")
                # Mark as NOT no-audio BEFORE stopping, so stop() doesn't
                # trigger another poll cycle
                self._no_audio_mode = False
                game = self._last_game or "Unknown"
                # Stop current no-audio recorder
                with self._lock:
                    if self._process and self._process.poll() is None:
                        self._process.terminate()
                        try: self._process.wait(timeout=3)
                        except: self._process.kill()
                    self._process = None
                time.sleep(0.3)
                # Start fresh with audio — don't call self.start() to avoid
                # re-entering the retry/poll loop; build command directly
                self.start(game)
            else:
                # Schedule next poll
                self._audio_poll_timer = threading.Timer(interval, _poll)
                self._audio_poll_timer.daemon = True
                self._audio_poll_timer.start()

        self._audio_poll_timer = threading.Timer(interval, _poll)
        self._audio_poll_timer.daemon = True
        self._audio_poll_timer.start()

    def _get_valid_audio_devices(self) -> tuple:
        """
        Return (app_devices, hw_devices) — two sets of currently valid sources.
        app_devices: per-application streams (dynamic, may disappear when muted)
        hw_devices:  hardware devices and monitors (always available)
        """
        try:
            gsr = self.config.gpu_recorder_path.strip().split()[0]
            app_devices = set()
            hw_devices  = set()
            # App audio streams
            r = subprocess.run([gsr, "--list-application-audio"],
                               capture_output=True, text=True, timeout=5)
            for line in r.stdout.splitlines():
                name = line.strip()
                if name:
                    app_devices.add(name)
            # Hardware devices
            r2 = subprocess.run([gsr, "--list-audio-devices"],
                                capture_output=True, text=True, timeout=5)
            for line in r2.stdout.splitlines():
                if "|" in line:
                    device_id = line.split("|", 1)[0].strip()
                else:
                    device_id = line.strip()
                if device_id:
                    hw_devices.add(device_id)
            logger.debug(f"App audio: {app_devices} | HW audio: {hw_devices}")
            return app_devices, hw_devices
        except Exception as e:
            logger.debug(f"Could not get valid audio devices: {e}")
            return set(), set()  # empty = skip validation

    def save_clip(self, event_meta: str = "") -> bool:
        """Save replay buffer. event_meta format: trigger|mode|map|round"""
        if not self.is_running():
            logger.warning("Recorder not running")
            return False
        try:
            self._process.send_signal(_signal.SIGUSR1)
            logger.info(f"Save signal sent (meta: {event_meta or 'manual'})")
            return True
        except Exception as e:
            logger.error(f"Save signal failed: {e}")
            return False

    @staticmethod
    def parse_event_meta(event: str) -> dict:
        """Parse a rich event string into its components."""
        parts = event.split("|")
        if len(parts) == 4:
            return {
                "trigger": parts[0],
                "mode":    parts[1],
                "map":     parts[2],
                "round":   parts[3],
            }
        return {"trigger": event, "mode": "", "map": "", "round": ""}

    @staticmethod
    def build_clip_filename(game: str, meta: dict, clip_length: int = 30,
                             post_event: int = 7) -> str:
        """
        Build a descriptive filename encoding clip metadata.
        Format: GAME_MODE_MAP_rROUND_TRIGGER_e{offset}_TIMESTAMP
        Example: CS2_competitive_de_dust2_r12_headshot_e23_2026-05-10_21-32-05

        e{offset} = seconds from clip start to trigger event
        = clip_length - post_event_seconds
        This lets thumbnail extraction always seek to the right frame.
        """
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

        def safe(s):
            return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(s))

        parts = [safe(game)]
        if meta.get("mode"):
            parts.append(safe(meta["mode"]))
        if meta.get("map"):
            parts.append(safe(meta["map"]))
        if meta.get("round"):
            parts.append(f"r{safe(meta['round'])}")
        if meta.get("trigger"):
            parts.append(safe(meta["trigger"]))
        # p{N} = seconds after trigger event (post-event delay)
        # marker position = clip_duration - p{N}
        parts.append(f"p{post_event}")
        parts.append(ts)
        return "_".join(p for p in parts if p)

    def _make_output_dir(self, game: str) -> Path:
        date_str = datetime.now().strftime("%d-%b-%Y")
        safe = "".join(c if c.isalnum() or c in "-_ " else "_" for c in game)
        path = Path(self.config.output_dir) / safe / date_str
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _emit_status(self, status: str):
        if self.on_status_change:
            self.on_status_change(status)
