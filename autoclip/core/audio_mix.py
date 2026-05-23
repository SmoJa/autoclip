# SPDX-License-Identifier: GPL-3.0-or-later
"""
Audio mix-down processor for AutoClip.

When audio_track_mode == "mixed_immediate": runs ffmpeg immediately after each clip save.
When audio_track_mode == "mixed_deferred": queues clips and processes when game closes.
When audio_track_mode == "separate": no processing, clips keep individual tracks.

ffmpeg stream-copies video (no re-encode) and amix-es all audio tracks to one.
"""

import subprocess
import threading
import logging
import time
from pathlib import Path
from typing import List, Optional, Callable

logger = logging.getLogger(__name__)


def mix_audio_tracks(clip_path: Path) -> bool:
    """
    Mix all audio tracks in clip_path to a single stream, in-place.
    Video is stream-copied. Returns True on success.
    """
    try:
        # Probe how many audio streams the clip has
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index",
             "-of", "csv=p=0", str(clip_path)],
            capture_output=True, text=True, timeout=10
        )
        n_audio = len([l for l in r.stdout.splitlines() if l.strip()])

        if n_audio <= 1:
            logger.debug(f"Only {n_audio} audio track(s) — no mix needed: {clip_path.name}")
            return True

        tmp_path = clip_path.with_suffix(".mix_tmp" + clip_path.suffix)

        # Build ffmpeg command:
        # -c:v copy          — stream-copy video, no re-encode
        # -filter_complex    — amix all audio streams to one
        # -c:a aac           — encode the mixed audio
        # -b:a 320k          — reasonable quality
        filter_inputs = "".join(f"[0:a:{i}]" for i in range(n_audio))
        filter_complex = f"{filter_inputs}amix=inputs={n_audio}:normalize=0:duration=longest[aout]"

        cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-i", str(clip_path),
            "-filter_complex", filter_complex,
            "-map", "0:v",
            "-map", "[aout]",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "320k",
            str(tmp_path)
        ]

        logger.info(f"Mixing {n_audio} audio tracks: {clip_path.name}")
        result = subprocess.run(cmd, timeout=120, capture_output=True)

        if result.returncode != 0:
            err = result.stderr.decode(errors="replace")
            logger.error(f"Audio mix failed: {err[:200]}")
            if tmp_path.exists():
                tmp_path.unlink()
            return False

        if not tmp_path.exists() or tmp_path.stat().st_size < 1024:
            logger.error("Audio mix produced empty file")
            return False

        # Replace original
        clip_path.unlink()
        tmp_path.rename(clip_path)
        logger.info(f"Audio mix complete: {clip_path.name}")
        return True

    except Exception as e:
        logger.error(f"Audio mix error: {e}")
        return False


class DeferredMixQueue:
    """
    Queues clips for audio mix-down and processes them when notified
    that the game has ended / player is out of match.
    """

    def __init__(self, on_status: Optional[Callable[[str], None]] = None):
        self._queue: List[Path] = []
        self._lock  = threading.Lock()
        self._on_status = on_status

    def enqueue(self, path: Path):
        with self._lock:
            if path not in self._queue:
                self._queue.append(path)
                logger.info(f"Queued for deferred mix: {path.name} ({len(self._queue)} pending)")
        if self._on_status:
            self._on_status(f"{len(self._queue)} clip(s) pending audio mix")

    def flush(self):
        """Process all queued clips. Call when game closes or match ends."""
        with self._lock:
            queue = list(self._queue)
            self._queue.clear()

        if not queue:
            return

        logger.info(f"Processing deferred mix queue: {len(queue)} clip(s)")
        if self._on_status:
            self._on_status(f"Mixing audio for {len(queue)} clip(s)...")

        def _process():
            for i, path in enumerate(queue):
                if self._on_status:
                    self._on_status(f"Mixing audio {i+1}/{len(queue)}: {path.name}")
                if path.exists():
                    mix_audio_tracks(path)
            if self._on_status:
                self._on_status("")
            logger.info("Deferred mix queue complete")

        threading.Thread(target=_process, daemon=True).start()

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._queue)
