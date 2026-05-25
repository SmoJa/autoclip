# SPDX-License-Identifier: GPL-3.0-or-later
"""
Clip library scanner and post-processing utilities.
Folder structure: output_dir / Game / YYYY-MM-DD / clip.mp4
"""
import subprocess
import threading
import logging
import functools
import json
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional, Callable
from datetime import datetime

logger = logging.getLogger(__name__)


@functools.lru_cache(maxsize=512)
def probe_hdr_peak(clip_path_str: str) -> float:
    """Return peak luminance ratio (MaxCLL nits / 100) for tonemap peak parameter.
    Falls back to 10.0 (1000 nit HDR10) if no metadata found."""
    try:
        r = subprocess.run([
            "ffprobe", "-v", "quiet",
            "-select_streams", "v:0",
            "-read_intervals", "%+#1",
            "-show_frames",
            "-show_entries",
            "stream=color_transfer:"
            "frame_side_data=max_content,max_average,max_luminance,min_luminance",
            "-of", "json",
            clip_path_str,
        ], capture_output=True, text=True, timeout=10)
        data = json.loads(r.stdout)
        for frame in data.get("frames", []):
            for sd in frame.get("side_data_list", []):
                max_cll = sd.get("max_content", 0)
                if max_cll and int(max_cll) > 0:
                    return int(max_cll) / 100.0
        for frame in data.get("frames", []):
            for sd in frame.get("side_data_list", []):
                max_lum = sd.get("max_luminance", 0)
                if max_lum:
                    try:
                        return float(max_lum) / 100.0
                    except (ValueError, TypeError):
                        pass
    except Exception:
        pass
    return 10.0


# Imported from metadata module — populated by game plugins at runtime
from autoclip.core.metadata import TRIGGER_DISPLAY as TRIGGER_NAMES
from autoclip.core.metadata import MODE_DISPLAY as MODE_NAMES
from autoclip.core.metadata import MAP_PREFIXES


@dataclass
class Clip:
    path:          Path
    game:          str
    date:          str       # YYYY-MM-DD
    filename:      str
    size_bytes:    int   = 0
    duration_secs: float = 0.0
    is_hdr:        bool  = False

    # Parsed from filename
    mode:            str  = ""
    map_name:        str  = ""
    round_num:       str  = ""
    team:            str  = ""
    score:           str  = ""
    clip_type:       str  = ""   # "s"=single, "o1"/"o2"=overflow
    triggers:        list = None
    post_event_secs: int  = -1
    _events:         list = None

    def __post_init__(self):
        if self.triggers is None:
            self.triggers = []
        if self._events is None:
            self._events = []
        self._parse_metadata()

    def _parse_metadata(self):
        from autoclip.core.metadata import ClipMeta
        cm = ClipMeta.from_filename(self.path.stem)
        self.mode      = cm.mode
        self.map_name  = cm.map_name
        self.round_num = cm.round_num
        self.team      = cm.team
        self.score     = cm.score
        self.clip_type = cm.clip_type
        self._events   = cm.events
        self.triggers  = [e.trigger for e in cm.events]
        self.post_event_secs = cm.events[0].secs_from_end if cm.events else -1


    @property
    def display_name(self) -> str:
        """Human readable name: trigger + map + mode + date."""
        parts = []
        if self.trigger_display:
            parts.append(self.trigger_display)
        if self.map_name:
            parts.append(self.map_name)
        if self.mode_display:
            parts.append(self.mode_display)
        if self.date:
            parts.append(self.date)
        return "  ·  ".join(parts) if parts else self.path.stem

    @property
    def suggested_export_name(self) -> str:
        """
        Short export name: Map-trigger-DDMmmYY  e.g. de_dust2-4k-13May26
        """
        events = self._events or []
        trigger_label = _suggest_trigger_label(events)
        map_part = self.map_name or "clip"

        # Format date as DDMmmYY e.g. 13May26
        date_part = ""
        if self.date:
            try:
                from datetime import datetime
                dt = datetime.strptime(self.date, "%Y-%m-%d")
                date_part = dt.strftime("%-d%b%y")  # 13May26
            except Exception:
                date_part = self.date

        parts = [map_part]
        if trigger_label:
            parts.append(trigger_label)
        if date_part:
            parts.append(date_part)
        return "-".join(parts)

    @property
    def trigger_display(self) -> str:
        from autoclip.core.metadata import ClipMeta
        return ClipMeta(events=self._events or []).trigger_display

    @property
    def mode_display(self) -> str:
        from autoclip.core.metadata import MODE_DISPLAY
        return MODE_DISPLAY.get(self.mode, self.mode.replace("_","  ").title()) if self.mode else ""

    @property
    def map_display(self) -> str:
        return self.map_name

    @property
    def round_display(self) -> str:
        return f"Round {self.round_num}" if self.round_num else ""

    @property
    def events(self):
        return self._events or []

    @property
    def size_mb(self) -> float:
        return self.size_bytes / (1024 * 1024)

    @property
    def duration_str(self) -> str:
        m, s = divmod(int(self.duration_secs), 60)
        return f"{m}:{s:02d}"


def _suggest_trigger_label(events: list) -> str:
    """Derive a short human-readable trigger label from event marks."""
    if not events:
        return "clip"

    triggers = [e.trigger for e in events]

    # Ace
    if "ace" in triggers:
        return "ace"

    # Clutch
    if "clutch" in triggers:
        return "clutch"

    # Knife kill
    if "knife" in triggers:
        return "knife"

    # Utility kill (legendary)
    if "util" in triggers:
        return "utility-kill"

    # Fire kill
    if "fire" in triggers:
        return "molotov"

    # Count individual kill events.
    INDIVIDUAL_KILLS = {"hs", "k", "nade", "he", "fire", "knife", "util"}
    kill_count = sum(1 for t in triggers if t in INDIVIDUAL_KILLS)
    # mk means at least 3 kills happened — floor at 3 only if count is lower
    if "mk" in triggers and kill_count < 3:
        kill_count = 3

    if kill_count >= 5:
        return "ace"
    if kill_count >= 2:
        return f"{kill_count}k"

    # Single kill types
    if "hs" in triggers:
        return "hs"
    if "lh" in triggers:
        return "low-health"
    if "bp" in triggers:
        return "bomb-plant"
    if "bd" in triggers:
        return "bomb-defuse"
    if "rw" in triggers:
        return "round-win"

    return "clip"


def _suggest_trigger_label(events: list) -> str:
    """Derive a short human-readable trigger label from event marks."""
    if not events:
        return "clip"

    triggers = [e.trigger for e in events]

    if "ace"    in triggers: return "ace"
    if "clutch" in triggers: return "clutch"
    if "knife"  in triggers: return "knife"
    if "util"   in triggers: return "utility-kill"
    if "fire"   in triggers: return "molotov"

    INDIVIDUAL_KILLS = {"hs", "k", "nade", "he", "fire", "knife", "util"}
    kill_count = sum(1 for t in triggers if t in INDIVIDUAL_KILLS)
    if "mk" in triggers and kill_count < 3:
        kill_count = 3

    if kill_count >= 5: return "ace"
    if kill_count >= 2: return f"{kill_count}k"
    if "hs" in triggers: return "hs"
    if "lh" in triggers: return "low-health"
    if "bp" in triggers: return "bomb-plant"
    if "bd" in triggers: return "bomb-defuse"
    if "rw" in triggers: return "round-win"
    return "clip"


def probe_clip(path: Path) -> tuple[float, bool]:
    """Return (duration_seconds, is_hdr) using ffprobe."""
    try:
        # Query both stream and format — HDR MKVs often have N/A stream duration
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=duration,color_transfer:format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=10
        )
        import re
        parts    = re.split(r"[,\n]", result.stdout.strip())
        duration = 0.0
        is_hdr   = False
        for part in parts:
            part = part.strip()
            if not part or part == "N/A":
                continue
            if part in ("smpte2084", "arib-std-b67", "smpte428"):
                is_hdr = True
            else:
                try:
                    v = float(part)
                    if v > 0 and duration == 0.0:
                        duration = v
                except ValueError:
                    pass
        return duration, is_hdr
    except Exception as e:
        logger.warning(f"ffprobe failed for {path.name}: {e}")
        return 0.0, False


def scan_library(output_dir: str) -> List[Clip]:
    """Scan the output directory and return sorted list of clips."""
    root = Path(output_dir)
    clips = []
    if not root.exists():
        return clips

    # Structure: root / Game / YYYY-MM-DD / file.mp4
    for game_dir in sorted(root.iterdir()):
        if not game_dir.is_dir():
            continue
        game = game_dir.name
        for date_dir in sorted(game_dir.iterdir(), reverse=True):
            if not date_dir.is_dir():
                continue
            date = date_dir.name
            for clip_path in sorted(date_dir.iterdir(), reverse=True):
                if clip_path.suffix.lower() not in (".mp4", ".mkv", ".mov"):
                    continue
                clip = Clip(
                    path=clip_path,
                    game=game,
                    date=date,
                    filename=clip_path.name,
                    size_bytes=clip_path.stat().st_size,
                )
                clips.append(clip)

    return clips


def probe_clips_async(clips: List[Clip], on_done: Callable):
    """Probe clip metadata in a background thread."""
    def _probe():
        for clip in clips:
            duration, is_hdr = probe_clip(clip.path)
            clip.duration_secs = duration
            clip.is_hdr = is_hdr
        on_done()
    threading.Thread(target=_probe, daemon=True).start()


def rename_clip(clip: Clip, new_name: str) -> Optional[Path]:
    """Rename a clip file, keeping its extension."""
    new_name = new_name.strip()
    if not new_name:
        return None
    # Sanitise
    safe = "".join(c if c.isalnum() or c in "-_ ." else "_" for c in new_name)
    if not safe.endswith(clip.path.suffix):
        safe += clip.path.suffix
    new_path = clip.path.parent / safe
    try:
        clip.path.rename(new_path)
        clip.path = new_path
        clip.filename = new_path.name
        return new_path
    except Exception as e:
        logger.error(f"Rename failed: {e}")
        return None


def convert_to_sdr(clip: Clip, on_progress: Callable[[str], None],
                   on_done: Callable[[bool, Path], None],
                   output_dir: Optional[Path] = None,
                   output_name: Optional[str] = None):
    """
    Convert an HDR clip to SDR using ffmpeg + h264_nvenc tone mapping.
    Output goes to output_dir if specified, otherwise alongside the original.
    Runs in a background thread.
    """
    dest_dir = output_dir if output_dir else clip.path.parent
    dest_dir.mkdir(parents=True, exist_ok=True)
    stem = output_name if output_name else (clip.path.stem + "_SDR")
    out_path = dest_dir / (stem + clip.path.suffix)

    def _convert():
        import tempfile, os as _os

        # If input and output are the same path, write to a temp file first
        if clip.path.resolve() == out_path.resolve():
            tmp_fd, tmp_path_str = tempfile.mkstemp(suffix=out_path.suffix,
                                                    dir=out_path.parent)
            _os.close(tmp_fd)
            actual_out = Path(tmp_path_str)
        else:
            actual_out = out_path

        vf = (
            "zscale=t=linear:npl=100,"
            "format=gbrpf32le,"
            "zscale=p=bt709,"
            "tonemap=tonemap=hable:desat=0,"
            "zscale=t=bt709:m=bt709:r=tv,"
            "format=yuv420p"
        )

        cmd_nvenc = [
            "ffmpeg", "-y",
            "-i", str(clip.path),
            "-vf", vf,
            "-c:v", "h264_nvenc",
            "-b:v", "20M",
            "-c:a", "copy",
            str(actual_out)
        ]
        cmd_cpu = [
            "ffmpeg", "-y",
            "-i", str(clip.path),
            "-vf", vf,
            "-c:v", "libx264",
            "-crf", "18",
            "-preset", "fast",
            "-c:a", "copy",
            str(actual_out)
        ]

        on_progress("Starting conversion...")

        strip_meta = [
            "-map_metadata", "0",
            "-metadata:s:v", "color_transfer=bt709",
            "-metadata:s:v", "color_primaries=bt709",
            "-metadata:s:v", "color_space=bt709",
        ]

        on_progress("Starting conversion...")

        for cmd, label in [(cmd_nvenc, "NVENC"), (cmd_cpu, "CPU")]:
            full_cmd = cmd[:-1] + strip_meta + [cmd[-1]]
            on_progress(f"Encoding with {label}...")
            logger.info(f"SDR convert cmd: {' '.join(full_cmd)}")
            try:
                result = subprocess.run(full_cmd, capture_output=True, text=True, timeout=600)
                if result.returncode == 0 and actual_out.exists():
                    # If we used a temp file, replace the original
                    if actual_out != out_path:
                        import os as _os2
                        if out_path.exists():
                            out_path.unlink()
                        actual_out.rename(out_path)
                    logger.info(f"SDR conversion done: {out_path}")
                    on_done(True, out_path)
                    return
                else:
                    logger.warning(f"{label} encode failed (rc={result.returncode})")
                    logger.warning(f"stderr: {result.stderr[-500:]}")
                    if actual_out.exists() and actual_out != out_path:
                        actual_out.unlink()
            except subprocess.TimeoutExpired:
                logger.error("Conversion timed out")
                on_done(False, out_path)
                return
            except Exception as e:
                logger.warning(f"{label} failed: {e}")

        on_done(False, out_path)

    threading.Thread(target=_convert, daemon=True).start()


def trim_clip(clip: Clip, start_secs: float, end_secs: float,
              on_done: Callable[[bool, Path], None],
              output_dir: Optional[Path] = None,
              output_name: Optional[str] = None):
    """Trim a clip between start and end seconds."""
    dest_dir = output_dir if output_dir else clip.path.parent
    dest_dir.mkdir(parents=True, exist_ok=True)
    stem = output_name if output_name else (clip.path.stem + "_trimmed")
    out_path = dest_dir / (stem + clip.path.suffix)

    def _trim():
        duration = end_secs - start_secs
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(start_secs),
            "-i", str(clip.path),
            "-t", str(duration),
            "-c", "copy",
            str(out_path)
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            success = result.returncode == 0 and out_path.exists()
            on_done(success, out_path)
        except Exception as e:
            logger.error(f"Trim failed: {e}")
            on_done(False, out_path)

    threading.Thread(target=_trim, daemon=True).start()


def export_clip(clip: Clip, out_path: Path,
                do_sdr: bool = False,
                size_mb: float = None,
                fps: float = None,
                on_progress: Callable = None,
                on_done: Callable[[bool, Path], None] = None):
    """
    Export a clip with optional SDR conversion, fps change, and file size targeting.
    Uses two-pass encoding when size_mb is set.
    on_progress(msg: str, pct: float) — pct=-1 means phase label, pct 0-100 means progress update.
    Runs in a background thread.
    """
    def _status(msg, pct=-1.0):
        if on_progress:
            on_progress(msg, pct)

    def _done(success):
        if on_done:
            on_done(success, out_path)

    def _run_ffmpeg(cmd, duration, pct_start=0.0, pct_end=100.0):
        """Stream ffmpeg stderr for live progress. Returns (returncode, stderr_text).

        Reads the default -stats output directly via os.read so updates arrive
        as soon as ffmpeg writes them (stderr is unbuffered in C). Handles the
        \\r-delimited stat lines ffmpeg uses for progress.
        """
        import re as _re, os as _os
        time_re  = _re.compile(rb'time=(\d+):(\d+):([\d.]+)')
        speed_re = _re.compile(rb'speed=\s*([\d.]+)x')

        proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)
        fd = proc.stderr.fileno()
        buf = b""
        stderr_all = b""

        while True:
            try:
                chunk = _os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            stderr_all += chunk
            buf += chunk
            while True:
                cr = buf.find(b'\r')
                lf = buf.find(b'\n')
                if cr == -1 and lf == -1:
                    break
                idx = min(x for x in (cr, lf) if x != -1)
                line, buf = buf[:idx], buf[idx + 1:]
                if not line.strip() or not duration:
                    continue
                tm = time_re.search(line)
                if not tm:
                    continue
                elapsed = (int(tm.group(1)) * 3600
                           + int(tm.group(2)) * 60
                           + float(tm.group(3)))
                pct = pct_start + min(1.0, elapsed / duration) * (pct_end - pct_start)
                parts = []
                sm = speed_re.search(line)
                if sm:
                    sp_str = sm.group(1).decode()
                    parts.append(sp_str + "x")
                    try:
                        sp = float(sp_str)
                        if sp > 0.01:
                            rem = (duration - elapsed) / sp
                            if 0 < rem < 3600:
                                parts.append(f"ETA {int(rem)}s")
                    except ValueError:
                        pass
                _status(" · ".join(parts), pct)

        proc.wait()
        return proc.returncode, stderr_all.decode("utf-8", errors="replace")

    def _run():
        import os as _os
        out_path.parent.mkdir(parents=True, exist_ok=True)
        duration = clip.duration_secs or 0.0

        # Build combined video filter chain
        vf_parts = []
        if do_sdr:
            vf_parts.append(
                "zscale=t=linear:npl=100,"
                "format=gbrpf32le,"
                "zscale=p=bt709,"
                "tonemap=tonemap=hable:desat=0,"
                "zscale=t=bt709:m=bt709:r=tv,"
                "format=yuv420p"
            )
        if fps:
            vf_parts.append(f"fps={fps}")
        vf_chain = ",".join(vf_parts) if vf_parts else None

        if size_mb:
            dur = duration or 30.0
            audio_bitrate_kbps = 128
            target_bits = size_mb * 8 * 1024 * 1024
            video_bitrate_kbps = max(200, int(
                (target_bits - audio_bitrate_kbps * 1000 * dur) / dur / 1000
            ))
            logger.info(f"Size target {size_mb}MB → video bitrate {video_bitrate_kbps}kbps")
            if video_bitrate_kbps < 500:
                _status(f"Warning: {size_mb}MB target requires very low bitrate "
                        f"({video_bitrate_kbps}kbps) — quality will be poor")

            tmp_dir = out_path.parent
            passlog = str(tmp_dir / "ffmpeg2pass")
            base_cmd = ["ffmpeg", "-y", "-i", str(clip.path)]
            if vf_chain:
                base_cmd += ["-vf", vf_chain]
            vb = f"{video_bitrate_kbps}k"
            ab = f"{audio_bitrate_kbps}k"

            _status("Analysing (pass 1/2)...")
            pass1 = base_cmd + [
                "-c:v", "libx264", "-b:v", vb,
                "-pass", "1", "-passlogfile", passlog,
                "-an", "-f", "null", "/dev/null"
            ]
            rc, err = _run_ffmpeg(pass1, dur, 0.0, 50.0)
            if rc != 0:
                logger.warning(f"Pass 1 failed: {err[-300:]}")
                _done(False); return

            _status("Encoding (pass 2/2)...")
            pass2 = base_cmd + [
                "-c:v", "libx264", "-b:v", vb,
                "-pass", "2", "-passlogfile", passlog,
                "-c:a", "aac", "-b:a", ab,
                str(out_path)
            ]
            rc, err = _run_ffmpeg(pass2, dur, 50.0, 100.0)

            for ext in ("-0.log", "-0.log.mbtree"):
                try: _os.unlink(passlog + ext)
                except Exception: pass

            if rc == 0 and out_path.exists():
                actual_mb = out_path.stat().st_size / (1024 * 1024)
                logger.info(f"Export done: {actual_mb:.1f}MB (target {size_mb}MB)")
                _done(True)
            else:
                logger.warning(f"Pass 2 failed: {err[-300:]}")
                _done(False)

        elif vf_chain or out_path.suffix.lower() == ".mp4":
            _status("Encoding...")
            base_cmd = ["ffmpeg", "-y", "-i", str(clip.path)]
            if vf_chain:
                base_cmd += ["-vf", vf_chain]
            for codec, label in [("h264_nvenc", "NVENC"), ("libx264 -crf 18 -preset fast".split(), "CPU")]:
                c_args = codec if isinstance(codec, list) else [codec]
                cmd = base_cmd + ["-c:v"] + c_args + ["-b:v", "20M", "-c:a", "copy", str(out_path)]
                rc, err = _run_ffmpeg(cmd, duration, 0.0, 100.0)
                if rc == 0 and out_path.exists():
                    _done(True); return
                logger.warning(f"{label} failed: {err[-200:]}")
            _done(False)

        else:
            _status("Copying...")
            cmd = ["ffmpeg", "-y", "-i", str(clip.path), "-c", "copy", str(out_path)]
            rc, err = _run_ffmpeg(cmd, duration, 0.0, 100.0)
            if rc == 0 and out_path.exists():
                _done(True)
            else:
                import shutil
                try:
                    shutil.copy2(clip.path, out_path)
                    _done(True)
                except Exception as e:
                    logger.error(f"Copy failed: {e}")
                    _done(False)

    threading.Thread(target=_run, daemon=True).start()


def extract_waveform(path, num_samples: int = 800):
    """Extract normalised waveform amplitude samples from a clip's audio track."""
    import numpy as np
    try:
        # Decode audio to raw PCM float32 at low sample rate for waveform display
        cmd = [
            "ffmpeg", "-v", "error",
            "-i", str(path),
            "-vn",                          # no video
            "-ac", "1",                     # mono
            "-ar", "8000",                  # 8kHz is plenty for waveform display
            "-f", "f32le",                  # raw float32 little-endian
            "-",
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=60)
        if not result.stdout or len(result.stdout) < 4:
            import logging
            logging.getLogger(__name__).debug(
                f"Waveform: no audio data. stderr: {result.stderr[-200:]}")
            return np.zeros(num_samples)
        data = np.frombuffer(result.stdout, dtype=np.float32)
        if len(data) < num_samples:
            return np.zeros(num_samples)
        # Downsample to num_samples by taking RMS of chunks
        chunk_size = max(1, len(data) // num_samples)
        samples = np.array([
            np.sqrt(np.mean(data[i*chunk_size:(i+1)*chunk_size]**2))
            for i in range(num_samples)
        ], dtype=np.float32)
        mx = samples.max()
        if mx > 0:
            samples /= mx
        return samples
    except Exception as e:
        import logging
        logging.getLogger(__name__).debug(f"Waveform extraction failed: {e}")
        import numpy as np
        return np.zeros(num_samples)
