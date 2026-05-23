# SPDX-License-Identifier: GPL-3.0-or-later
import threading
import logging
import time
import subprocess
from pathlib import Path
from typing import Callable, Optional, List

logger = logging.getLogger(__name__)

class HotkeyManager:
    def __init__(self, config, on_hotkey: Callable[[], None]):
        self.config   = config
        self.on_hotkey = on_hotkey
        self._listener = None

    def start(self):
        try:
            from pynput import keyboard
            def on_activate():
                logger.info("Manual hotkey pressed")
                self.on_hotkey()
            self._listener = keyboard.GlobalHotKeys({
                self.config.manual_hotkey: on_activate
            })
            self._listener.daemon = True
            self._listener.start()
            logger.info(f"Hotkey registered: {self.config.manual_hotkey}")
        except Exception as e:
            logger.error(f"Hotkey registration failed: {e}")

    def stop(self):
        if self._listener:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None

class ClipTrigger:
    """
    Accumulates trigger events within the post-event delay window, then saves
    one clip covering all of them when the window expires.

    Timing is anchored on save_signal_time — the moment we tell gsr to save.
    Since gsr's replay buffer ends at the save signal, every event's position
    in the video is simply: duration - (save_signal_time - event_time).
    """

    def __init__(self, config, recorder):
        self.config   = config
        self.recorder = recorder

        self._lock               = threading.Lock()
        self._pending            = False
        self._events: List[tuple] = []   # [(event_str, monotonic_time), ...]
        self._timer: Optional[threading.Timer] = None
        self._first_trigger_time: Optional[float] = None

        # Overflow tracking
        self._overflow_seq   = 0    # increments each overflow save in a streak
        self._in_overflow    = False
        # Full event history — used to include prior events in overflow clips
        self._event_history: list = []   # (ev_str, ev_time) for all recent events

        self.on_clip_saved: Optional[Callable[[str], None]] = None

    # ── Public ────────────────────────────────────────────────────────────────

    def trigger(self, reason: str = "manual"):
        """
        Add a trigger event. Resets the quiet window timer.
        If accumulating would push the first event out of the replay buffer,
        saves the current batch immediately and starts fresh.
        """
        with self._lock:
            now        = time.monotonic()
            delay      = self.config.post_event_seconds
            clip_len   = getattr(self.config, "clip_length_seconds", 30)
            safe_window = clip_len - delay - 2   # 2s margin

            # Buffer overflow check
            if self._events and self._first_trigger_time is not None:
                elapsed = now - self._first_trigger_time
                if elapsed >= safe_window:
                    logger.info(
                        f"Buffer overflow ({elapsed:.1f}s >= {safe_window:.1f}s) "
                        f"— saving batch {self._overflow_seq + 1}"
                    )
                    if self._timer:
                        self._timer.cancel()
                        self._timer = None
                    batch = list(self._events)
                    self._overflow_seq  += 1
                    self._in_overflow    = True
                    seq = self._overflow_seq
                    self._events.clear()
                    self._first_trigger_time = None
                    # Trim history to last clip_length seconds
                    cutoff = now - clip_len
                    self._event_history = [
                        e for e in self._event_history if e[1] >= cutoff]
                    # Pass full history so events from prior clips
                    # that fall within this clip's window get markers
                    history_snapshot = list(self._event_history)
                    threading.Thread(
                        target=self._save_batch,
                        args=(batch, f"o{seq}", history_snapshot),
                        daemon=True
                    ).start()

            # Reset timer
            if self._timer:
                self._timer.cancel()
                self._timer = None

            # Accumulate
            if not self._events:
                self._first_trigger_time = now
            self._events.append((reason, now))
            self._event_history.append((reason, now))
            self._pending = True

            logger.info(
                f"Trigger: {reason} — "
                f"{len(self._events)} event(s), saving in {delay}s"
            )

            self._timer = threading.Timer(delay, self._on_timer)
            self._timer.daemon = True
            self._timer.start()

    def _probe_duration(self, path) -> float:
        """Get real clip duration via ffprobe."""
        try:
            r = subprocess.run(
                ["ffprobe", "-v", "error",
                 "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1",
                 str(path)],
                capture_output=True, text=True, timeout=10
            )
            val = r.stdout.strip()
            if val and val != "N/A":
                return float(val)
        except Exception as e:
            logger.debug(f"ffprobe failed: {e}")
        return 0.0

    def cancel(self):
        """Cancel any pending save."""
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None
            self._events.clear()
            self._pending             = False
            self._first_trigger_time  = None

    # ── Internal ──────────────────────────────────────────────────────────────

    def _on_timer(self):
        """Quiet window expired — save and reset overflow state."""
        with self._lock:
            events = list(self._events)
            self._events.clear()
            self._pending             = False
            self._timer               = None
            self._first_trigger_time  = None
            # Determine clip type before resetting
            if self._in_overflow:
                # This is the final clip of an overflow streak — include history
                self._overflow_seq += 1
                clip_type = f"o{self._overflow_seq}"
                history_snapshot = list(self._event_history)
            else:
                clip_type = "s"
                history_snapshot = None
            self._in_overflow  = False
            self._overflow_seq = 0
            self._event_history.clear()  # streak over, fresh start

        threading.Thread(
            target=self._save_batch,
            args=(events, clip_type, history_snapshot),
            daemon=True
        ).start()

    def _save_batch(self, events: list, clip_type: str, history: list = None):
        """
        Signal gsr to save, then rename the clip with accurate metadata.

        Key insight: save_signal_time is recorded right before save_clip().
        gsr's replay buffer ends at this moment, so:
            event_position = clip_duration - (save_signal_time - event_time)
        """
        if not events:
            return

        combined = self._build_reason(events)
        logger.info(f"Saving clip: {combined.split('|')[0]} ({clip_type})")

        # ← anchor: everything is relative to this moment
        save_signal_time = time.monotonic()
        signal_wall_time = time.time()
        success = self.recorder.save_clip(event_meta=combined)
        if not success:
            return

        # Poll until a new clip file appears (written after save signal)
        # This is more reliable than a fixed sleep for rapid overflow saves
        output_dir = Path(self.config.output_dir)
        newest = None
        deadline = time.time() + 10.0  # max 10s wait
        while time.time() < deadline:
            time.sleep(0.25)
            all_clips = [
                p for ext in ("*.mkv", "*.mp4", "*.mov")
                for p in output_dir.rglob(ext)
            ]
            if all_clips:
                candidate = max(all_clips, key=lambda p: p.stat().st_mtime)
                # File must have been created after we sent the save signal
                if candidate.stat().st_mtime >= signal_wall_time - 0.5:
                    # Wait a bit more to ensure gsr has finished writing
                    time.sleep(0.5)
                    newest = candidate
                    break

        if newest is None:
            logger.warning("No new clip file found after save signal — skipping rename")
            return

        try:
            self._rename_clip(events, combined, save_signal_time, clip_type,
                              newest=newest, history=history)
        except Exception as e:
            logger.warning(f"Rename failed: {e}")

        if self.on_clip_saved:
            self.on_clip_saved(combined)

    def _rename_clip(self, events: list, reason: str,
                     save_signal_time: float, clip_type: str,
                     newest: Path = None, history: list = None):
        """
        Probe clip duration, compute event positions, and rename with full ClipMeta.
        secs_from_end = save_signal_time - event_time (raw_offset only)
        """
        from autoclip.core.metadata import (
            ClipMeta, EventMark, TRIGGER_TO_SHORT, MODE_TO_SHORT
        )

        if newest is None:
            # Fallback: scan for newest clip (legacy path)
            output_dir = Path(self.config.output_dir)
            all_clips = [
                p for ext in ("*.mkv", "*.mp4", "*.mov")
                for p in output_dir.rglob(ext)
            ]
            if not all_clips:
                return
            newest = max(all_clips, key=lambda p: p.stat().st_mtime)
            if time.time() - newest.stat().st_mtime > 30:
                logger.warning("Newest clip is too old — skipping rename")
                return

        # Parse context from reason string
        parts       = reason.split("|")
        mode_raw    = parts[1] if len(parts) > 1 else ""
        map_name    = parts[2] if len(parts) > 2 else ""
        round_num   = parts[3] if len(parts) > 3 else ""
        team        = parts[4] if len(parts) > 4 else ""
        ct_score    = parts[5] if len(parts) > 5 else ""
        t_score     = parts[6] if len(parts) > 6 else ""
        spec_player = parts[7] if len(parts) > 7 else ""
        score       = f"{ct_score}-{t_score}" if ct_score and t_score else ""

        # secs_from_end = save_signal_time - ev_time (raw_offset only)
        # The keyframe slip and GSI lag cancel out:
        # - slip is footage added AFTER save_signal, doesn't shift event positions
        # - GSI lag is baked into ev_time, but gsr lags identically so they cancel
        # Verified empirically: raw_offset alone gives <0.05s accuracy.
        real_duration = self._probe_duration(newest)
        clip_length   = float(getattr(self.config, "clip_length_seconds", 30))
        logger.info(f"Clip timing: type={clip_type} real={real_duration:.3f}s events={len(events)}")

        # For overflow clips, merge historical events within this clip's window
        all_events = list(events)
        if history and clip_type.startswith("o"):
            window_start = save_signal_time - clip_length
            current_ev_times = {e[1] for e in events}
            for ev_str, ev_time in history:
                if ev_time >= window_start and ev_time not in current_ev_times:
                    all_events.append((ev_str, ev_time))
            all_events.sort(key=lambda e: e[1])
            logger.info(f"  history merge: {len(events)} current + "
                        f"{len(all_events)-len(events)} from history = {len(all_events)} total")

        # Build event marks
        event_marks = []
        for i, item in enumerate(all_events):
            if isinstance(item, tuple) and len(item) == 2:
                ev_str, ev_time = item
            else:
                logger.warning(f"  [{i}] unexpected event format: {item!r}")
                continue
            ev_parts      = ev_str.split("|")
            trigger       = ev_parts[0]
            weapon        = ev_parts[7] if len(ev_parts) > 7 else ""
            short         = TRIGGER_TO_SHORT.get(trigger, trigger)
            raw_offset    = save_signal_time - ev_time
            secs_from_end = max(0.0, raw_offset)
            event_marks.append(EventMark(
                trigger=short,
                secs_from_end=secs_from_end,
                weapon=weapon,
            ))
            logger.debug(f"  [{i:2d}] {short} {weapon} raw={raw_offset:.3f}s sfe={secs_from_end:.3f}s")

        try:
            game = newest.parent.parent.name
        except Exception:
            game = "CS2"

        meta = ClipMeta(
            game=game,
            mode=MODE_TO_SHORT.get(mode_raw, mode_raw),
            map_name=map_name,
            round_num=round_num,
            team=team,
            score=score,
            clip_type=clip_type,
            events=event_marks,
            spectated_player=spec_player,
        )

        new_name = meta.to_filename_stem() + newest.suffix
        # Linux filename limit is 255 bytes — truncate events if needed
        while len(new_name.encode()) > 255 and len(meta.events) > 1:
            meta = meta.__class__(
                game=meta.game, mode=meta.mode, map_name=meta.map_name,
                round_num=meta.round_num, team=meta.team, score=meta.score,
                clip_type=meta.clip_type, events=meta.events[1:],  # drop oldest
                spectated_player=meta.spectated_player,
            )
            new_name = meta.to_filename_stem() + newest.suffix
        new_path = newest.parent / new_name
        if new_path != newest:
            newest.rename(new_path)
            logger.info(f"Renamed: {new_name}")

    def _build_reason(self, events: list) -> str:
        """Build combined reason string from event list."""
        triggers = []
        mode_raw = map_name = round_num = team = ct_score = t_score = spec_player = ""

        for ev_str, _ in events:
            parts   = ev_str.split("|")
            trigger = parts[0]
            if trigger not in triggers:
                triggers.append(trigger)
            if len(parts) >= 4 and not mode_raw:
                mode_raw  = parts[1]
                map_name  = parts[2]
                round_num = parts[3]
            if len(parts) >= 7 and not team:
                team     = parts[4]
                ct_score = parts[5]
                t_score  = parts[6]
            if len(parts) > 8 and not spec_player:
                spec_player = parts[8]

        return (f"{'_'.join(triggers)}|{mode_raw}|{map_name}|{round_num}|"
                f"{team}|{ct_score}|{t_score}|{spec_player}")
