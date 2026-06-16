# SPDX-License-Identifier: GPL-3.0-or-later
"""Windows recording backend for AutoClip, built on libobs (OBS 32.x) via ctypes.

Runs as a SEPARATE process (spawned by recorder_windows.py with the bundled OBS
python), keeping libobs's D3D11 device and worker threads out of the PyQt GUI
process. Captures the game window via Windows Graphics Capture (WGC) — which works
for exclusive/flip fullscreen AND borderless, with no hook injection, no anti-cheat
concern, and no game-side config. Encodes once with NVENC into a replay-buffer ring;
saving just muxes the buffered packets.

Protocol (identical to the old Rust recorder, so it's a drop-in):
    stdout  "ready"            replay buffer is rolling
    stdin   "save <path>"      save the last N seconds to <path>
    stdout  "saved"            clip written
    stdout  "error: <msg>"     save failed
    stdin   "stop"             stop and exit

Args: --process <name> --fps <n> --bitrate <bps> --buffer-secs <n>
      --codec <h264|hevc> --width <w> --height <h>
      [--audio-system <device>] [--audio-mic <device>] [--no-audio]
"""
import ctypes as C
import os
import sys
import time
import glob
import json

# ── OBS runtime location ──────────────────────────────────────────────────────
# When launched with the bundled python (which lives in <bundle>/bin/64bit next to
# obs.dll + the helper exes), obs.dll sits beside sys.executable. Fall back to a
# system OBS install for dev runs.
_host = os.path.dirname(sys.executable)
if os.path.exists(os.path.join(_host, "obs.dll")):
    OBS_BIN = _host
else:
    OBS_BIN = r"C:\Program Files\obs-studio\bin\64bit"
OBS_ROOT = os.path.dirname(os.path.dirname(OBS_BIN)) if OBS_BIN.lower().endswith("64bit") else OBS_BIN
OBS_DATA_LIBOBS  = os.path.join(OBS_ROOT, "data", "libobs")
OBS_PLUGIN_BIN   = os.path.join(OBS_ROOT, "obs-plugins", "64bit")
OBS_PLUGIN_DATA  = os.path.join(OBS_ROOT, "data", "obs-plugins")

# Only the plugins we actually use (loading all of them drags in frontend-tools etc.
# that expect a Qt frontend and crash a headless host).
NEEDED_MODULES = ["win-capture", "win-wasapi", "obs-ffmpeg", "obs-nvenc", "obs-x264"]

# enum values pinned to OBS 32.x
VIDEO_FORMAT_NV12, VIDEO_CS_709, VIDEO_RANGE_PARTIAL = 2, 2, 1
OBS_SCALE_BICUBIC, SPEAKERS_STEREO = 2, 2

# Where OBS writes replay files before we relocate them to the requested path.
SPOOL_DIR = os.path.join(os.environ.get("TEMP", "."), "autoclip-obs-spool")


def log(msg):
    # Diagnostics go to stderr (recorder_windows.py drains it to a log file);
    # stdout is reserved for the line protocol.
    sys.stderr.write(f"[obs_recorder] {msg}\n")
    sys.stderr.flush()


# Protocol replies go here. main() redirects the C-level stdout (fd 1) to stderr so
# libobs's own logging can't corrupt the line protocol, and saves the real stdout fd
# for replies.
_PROTO_FD = 1


def reply(line):
    os.write(_PROTO_FD, (line + "\n").encode())


# ── ctypes structures ─────────────────────────────────────────────────────────
class obs_video_info(C.Structure):
    _fields_ = [
        ("graphics_module", C.c_char_p),
        ("fps_num", C.c_uint32), ("fps_den", C.c_uint32),
        ("base_width", C.c_uint32), ("base_height", C.c_uint32),
        ("output_width", C.c_uint32), ("output_height", C.c_uint32),
        ("output_format", C.c_int), ("adapter", C.c_uint32),
        ("gpu_conversion", C.c_bool),
        ("colorspace", C.c_int), ("range", C.c_int), ("scale_type", C.c_int),
    ]


class obs_audio_info(C.Structure):
    _fields_ = [("samples_per_sec", C.c_uint32), ("speakers", C.c_int)]


class calldata(C.Structure):
    _fields_ = [("stack", C.c_void_p), ("size", C.c_size_t),
                ("capacity", C.c_size_t), ("fixed", C.c_bool)]


P, CP = C.c_void_p, C.c_char_p


class Obs:
    """Thin ctypes binding to the subset of libobs we use."""

    def __init__(self):
        # os.add_dll_directory returns a handle that removes the dir on GC — keep it.
        self._dll_dirs = [os.add_dll_directory(OBS_BIN)]
        os.chdir(OBS_BIN)
        self.lib = C.CDLL(os.path.join(OBS_BIN, "obs.dll"))
        self._bind()

    def _sig(self, name, restype, *argtypes):
        fn = getattr(self.lib, name)
        fn.restype = restype
        fn.argtypes = list(argtypes)
        return fn

    def _bind(self):
        s = self._sig
        self.startup        = s("obs_startup", C.c_bool, CP, CP, P)
        self.shutdown       = s("obs_shutdown", None)
        self.add_data_path  = s("obs_add_data_path", None, CP)
        self.open_module    = s("obs_open_module", C.c_int, C.POINTER(P), CP, CP)
        self.init_module    = s("obs_init_module", C.c_bool, P)
        self.post_load      = s("obs_post_load_modules", None)
        self.reset_video    = s("obs_reset_video", C.c_int, C.POINTER(obs_video_info))
        self.reset_audio    = s("obs_reset_audio", C.c_bool, C.POINTER(obs_audio_info))
        self.enum_encoders  = s("obs_enum_encoder_types", C.c_bool, C.c_size_t, C.POINTER(CP))
        self.data_create    = s("obs_data_create", P)
        self.data_set_string = s("obs_data_set_string", None, P, CP, CP)
        self.data_set_int   = s("obs_data_set_int", None, P, CP, C.c_longlong)
        self.data_set_bool  = s("obs_data_set_bool", None, P, CP, C.c_bool)
        self.data_release   = s("obs_data_release", None, P)
        self.source_create  = s("obs_source_create", P, CP, CP, P, P)
        self.source_get_width  = s("obs_source_get_width", C.c_uint32, P)
        self.source_get_height = s("obs_source_get_height", C.c_uint32, P)
        self.source_set_audio_mixers = s("obs_source_set_audio_mixers", None, P, C.c_uint32)
        self.source_set_volume = s("obs_source_set_volume", None, P, C.c_float)
        self.source_set_muted  = s("obs_source_set_muted", None, P, C.c_bool)
        self.set_output_source = s("obs_set_output_source", None, C.c_uint32, P)
        self.get_video      = s("obs_get_video", P)
        self.get_audio      = s("obs_get_audio", P)
        self.venc_create    = s("obs_video_encoder_create", P, CP, CP, P, P)
        self.aenc_create    = s("obs_audio_encoder_create", P, CP, CP, P, C.c_size_t, P)
        self.enc_set_video  = s("obs_encoder_set_video", None, P, P)
        self.enc_set_audio  = s("obs_encoder_set_audio", None, P, P)
        self.output_create  = s("obs_output_create", P, CP, CP, P, P)
        self.output_set_venc = s("obs_output_set_video_encoder", None, P, P)
        self.output_set_aenc = s("obs_output_set_audio_encoder", None, P, P, C.c_size_t)
        self.output_start   = s("obs_output_start", C.c_bool, P)
        self.output_stop    = s("obs_output_stop", None, P)
        self.output_last_err = s("obs_output_get_last_error", CP, P)
        self.output_proc    = s("obs_output_get_proc_handler", P, P)
        self.proc_call      = s("proc_handler_call", C.c_bool, P, CP, C.POINTER(calldata))

    def enum_encoder_ids(self):
        ids, i, sp = [], 0, CP()
        while self.enum_encoders(i, C.byref(sp)):
            if sp.value:
                ids.append(sp.value.decode())
            i += 1
        return ids


# ── Win32: find the game's top-level window by process name ───────────────────
_user32 = C.WinDLL("user32", use_last_error=True)
_kernel32 = C.WinDLL("kernel32", use_last_error=True)
from ctypes import wintypes  # noqa: E402

_WNDENUMPROC = C.WINFUNCTYPE(C.c_bool, wintypes.HWND, wintypes.LPARAM)
_user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.GetWindowTextW.argtypes = [wintypes.HWND, C.c_wchar_p, C.c_int]
_user32.GetClassNameW.argtypes = [wintypes.HWND, C.c_wchar_p, C.c_int]
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, C.POINTER(wintypes.DWORD)]
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _proc_exe_stem(pid):
    h = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        buf = C.create_unicode_buffer(260)
        size = wintypes.DWORD(260)
        if _kernel32.QueryFullProcessImageNameW(h, 0, buf, C.byref(size)):
            return os.path.splitext(os.path.basename(buf.value))[0].lower()
    finally:
        _kernel32.CloseHandle(h)
    return None


def find_game_window(proc_stem):
    """Return (title, classname, exe) for a visible top-level window owned by a
    process whose exe stem matches proc_stem, or None."""
    proc_stem = proc_stem.lower()
    found = {}

    def cb(hwnd, _):
        if not _user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, C.byref(pid))
        stem = _proc_exe_stem(pid.value)
        if stem != proc_stem:
            return True
        title = C.create_unicode_buffer(512)
        cls = C.create_unicode_buffer(256)
        _user32.GetWindowTextW(hwnd, title, 512)
        _user32.GetClassNameW(hwnd, cls, 256)
        # skip tiny helper/tool windows
        if not title.value:
            return True
        found["v"] = (title.value, cls.value, stem + ".exe")
        return False  # stop enumerating

    _user32.EnumWindows(_WNDENUMPROC(cb), 0)
    return found.get("v")


def obs_window_string(title, cls, exe):
    # OBS "window" match string: "title:class:exe" with ':' and '#' escaped per field.
    def enc(v):
        return v.replace("#", "#22").replace(":", "#3A")
    return f"{enc(title)}:{enc(cls)}:{enc(exe)}"


# ── argument parsing ──────────────────────────────────────────────────────────
def parse_args(argv):
    a = {"fps": 60, "bitrate": 30_000_000, "buffer_secs": 30, "codec": "h264",
         "process": None, "width": 0, "height": 0,
         "audio_system": None, "audio_mic": None, "no_audio": False,
         "audio_config": None,
         # encoder controls (defaults mirror config.py)
         "rate_control": "cbr", "cq": 20, "max_bitrate": 60_000_000,
         "preset": "p5", "multipass": "qres", "profile": "auto", "bframes": 2}
    i = 0
    while i < len(argv):
        k = argv[i]
        if k == "--no-audio":
            a["no_audio"] = True; i += 1; continue
        v = argv[i + 1] if i + 1 < len(argv) else ""
        if   k == "--fps":          a["fps"] = int(v or 60)
        elif k == "--bitrate":      a["bitrate"] = int(v or 30_000_000)
        elif k == "--buffer-secs":  a["buffer_secs"] = int(v or 30)
        elif k == "--codec":        a["codec"] = v or "h264"
        elif k == "--process":      a["process"] = v
        elif k == "--width":        a["width"] = int(v or 0)
        elif k == "--height":       a["height"] = int(v or 0)
        elif k == "--audio-system": a["audio_system"] = v
        elif k == "--audio-mic":    a["audio_mic"] = v
        elif k == "--audio-config": a["audio_config"] = v
        elif k == "--rate-control": a["rate_control"] = (v or "cbr").lower()
        elif k == "--cq":           a["cq"] = int(v or 20)
        elif k == "--max-bitrate":  a["max_bitrate"] = int(v or 60_000_000)
        elif k == "--preset":       a["preset"] = (v or "p5").lower()
        elif k == "--multipass":    a["multipass"] = (v or "qres").lower()
        elif k == "--profile":      a["profile"] = (v or "auto").lower()
        elif k == "--bframes":      a["bframes"] = int(v or 2)
        i += 2
    return a


def make_video_info(w, h, fps):
    return obs_video_info(
        graphics_module=b"libobs-d3d11.dll", fps_num=fps, fps_den=1,
        base_width=w, base_height=h, output_width=w, output_height=h,
        output_format=VIDEO_FORMAT_NV12, adapter=0, gpu_conversion=True,
        colorspace=VIDEO_CS_709, range=VIDEO_RANGE_PARTIAL, scale_type=OBS_SCALE_BICUBIC,
    )


def newest_stable_mp4(dirpath, since):
    files = [f for f in glob.glob(os.path.join(dirpath, "*.mp4")) if os.path.getmtime(f) >= since]
    if not files:
        return None
    f = max(files, key=os.path.getmtime)
    s1 = os.path.getsize(f); time.sleep(0.2); s2 = os.path.getsize(f)
    return f if (s1 == s2 and s2 > 0) else None


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    # Reserve the real stdout for the line protocol, then point C-level stdout (fd 1)
    # at stderr so libobs's logging (which writes to fd 1) goes to the stderr log
    # instead of corrupting the protocol channel recorder_windows.py reads.
    global _PROTO_FD
    _PROTO_FD = os.dup(1)
    os.dup2(2, 1)

    args = parse_args(sys.argv[1:])
    if not args["process"]:
        reply("error: --process is required")
        return 1

    os.makedirs(SPOOL_DIR, exist_ok=True)
    for f in glob.glob(os.path.join(SPOOL_DIR, "*.mp4")):
        try: os.remove(f)
        except OSError: pass

    fps = args["fps"]
    mon_w = args["width"] or 1920
    mon_h = args["height"] or 1080

    obs = Obs()
    if not obs.startup(b"en-US", None, None):
        reply("error: obs_startup failed")
        return 1
    obs.add_data_path((OBS_DATA_LIBOBS + "\\").encode())

    # Preload bin DLLs so plugin deps (FFmpeg) resolve (obs loads plugins with an
    # altered search path that ignores add_dll_directory).
    for dll in glob.glob(os.path.join(OBS_BIN, "*.dll")):
        try: C.CDLL(dll)
        except OSError: pass

    # CRITICAL: reset video BEFORE loading modules — win-capture only enables WGC
    # (wgc_supported) if D3D11 graphics are already up when it loads.
    ovi = make_video_info(mon_w, mon_h, fps)
    rc = obs.reset_video(C.byref(ovi))
    if rc != 0:
        reply(f"error: obs_reset_video failed rc={rc}")
        return 1
    oai = obs_audio_info(samples_per_sec=48000, speakers=SPEAKERS_STEREO)
    obs.reset_audio(C.byref(oai))

    for name in NEEDED_MODULES:
        mod = P()
        binp = os.path.join(OBS_PLUGIN_BIN, name + ".dll").encode()
        datap = os.path.join(OBS_PLUGIN_DATA, name).encode()
        if obs.open_module(C.byref(mod), binp, datap) == 0:
            obs.init_module(mod)
    obs.post_load()
    enc_ids = obs.enum_encoder_ids()

    # Wait for the game window to appear (games create it well after process start).
    proc_stem = args["process"].lower()
    win = None
    deadline = time.time() + 120
    while time.time() < deadline:
        win = find_game_window(proc_stem)
        if win:
            break
        time.sleep(0.5)
    if not win:
        reply("error: game window not found")
        return 1
    log(f"capturing window: {win}")

    # window_capture via WGC (method=2), matched by executable (priority=2).
    sd = obs.data_create()
    obs.data_set_string(sd, b"window", obs_window_string(*win).encode())
    obs.data_set_int(sd, b"method", 2)
    obs.data_set_int(sd, b"priority", 2)
    obs.data_set_bool(sd, b"capture_cursor", False)
    src = obs.source_create(b"window_capture", b"clip_video", sd, None)
    obs.data_release(sd)
    if not src:
        reply("error: capture source create failed")
        return 1
    obs.set_output_source(0, src)

    # ── audio sources from --audio-config ──────────────────────────────────
    # Each track becomes an OBS source: app:<exe> -> wasapi_process_output_capture
    # (a game's or chat program's audio), an output device, or the mic. `separate`
    # puts each source on its own mixer/output track; otherwise all mix to track 0.
    a_tracks, separate = [], False
    if args["audio_config"]:
        try:
            cfg = json.loads(args["audio_config"])
            a_tracks = cfg.get("tracks", [])
            separate = bool(cfg.get("separate")) and len(a_tracks) > 1
        except Exception as e:
            log(f"bad audio-config: {e}")
    if not a_tracks and not args["no_audio"]:
        a_tracks = [{"role": "game", "kind": "out", "id": "default", "vol": 1.0, "mute": False}]

    audio_sources = []   # (source, track_idx) — keep refs alive
    _achan = 1
    for tr in a_tracks:
        kind, ident = tr.get("kind", "out"), tr.get("id", "")
        ad = obs.data_create()
        a_src = None
        if kind == "app":
            w = find_game_window(os.path.splitext(ident)[0].lower())
            if w:
                obs.data_set_string(ad, b"window", obs_window_string(*w).encode())
                obs.data_set_int(ad, b"priority", 2)   # match by executable
                a_src = obs.source_create(b"wasapi_process_output_capture",
                                          f"clip_a{_achan}".encode(), ad, None)
            else:
                log(f"audio app not running, skipping: {ident}")
        else:
            if ident and ident.lower() != "default":
                obs.data_set_string(ad, b"device_id", ident.encode())
            sid = b"wasapi_input_capture" if kind == "in" else b"wasapi_output_capture"
            a_src = obs.source_create(sid, f"clip_a{_achan}".encode(), ad, None)
        obs.data_release(ad)
        if not a_src:
            continue
        track_idx = len(audio_sources) if separate else 0
        obs.source_set_audio_mixers(a_src, 1 << track_idx)
        obs.source_set_volume(a_src, C.c_float(float(tr.get("vol", 1.0))))
        if tr.get("mute"):
            obs.source_set_muted(a_src, True)
        obs.set_output_source(_achan, a_src)
        audio_sources.append((a_src, track_idx))
        _achan += 1
    log(f"audio: {len(audio_sources)} source(s), separate={separate}")

    # Size the canvas to the actual captured content (games often render at a
    # sub-native / stretched resolution; a monitor-sized canvas would letterbox).
    tw = th = 0
    for _ in range(80):
        tw, th = obs.source_get_width(src), obs.source_get_height(src)
        if tw and th:
            break
        time.sleep(0.1)
    if tw and th and (tw, th) != (mon_w, mon_h):
        log(f"canvas -> capture target {tw}x{th}")
        obs.reset_video(C.byref(make_video_info(tw, th, fps)))

    # encoders — pick the codec's NVENC encoder, falling back toward x264/h264.
    cl = args["codec"].lower()
    want = "hevc" if cl.startswith("hevc") else "av1" if cl.startswith("av1") else "h264"
    cands = [f"obs_nvenc_{want}_tex", f"obs_nvenc_{want}_cuda"]
    if want == "h264":
        cands += ["jim_nvenc", "obs_x264"]
    enc_id = next((e for e in cands if e in enc_ids), None)
    if enc_id is None:
        # Requested codec's NVENC isn't available — fall back to h264 (NVENC then x264).
        enc_id = next((e for e in ["obs_nvenc_h264_tex", "obs_nvenc_h264_cuda", "obs_x264"]
                       if e in enc_ids), "obs_x264")
        log(f"codec {want!r} encoder unavailable; falling back to {enc_id}")
        want = "h264"
    is_nvenc = enc_id.startswith("obs_nvenc") or enc_id == "jim_nvenc"

    rc = args["rate_control"].upper()          # CBR | VBR | CQP
    br_kbps = max(1000, args["bitrate"] // 1000)
    ed = obs.data_create()
    if is_nvenc:
        if rc == "CQP":
            obs.data_set_string(ed, b"rate_control", b"CQP")
            obs.data_set_int(ed, b"cqp", max(0, min(51, args["cq"])))
        elif rc == "VBR":
            obs.data_set_string(ed, b"rate_control", b"VBR")
            obs.data_set_int(ed, b"bitrate", br_kbps)
            obs.data_set_int(ed, b"max_bitrate", max(br_kbps, args["max_bitrate"] // 1000))
        else:  # CBR
            obs.data_set_string(ed, b"rate_control", b"CBR")
            obs.data_set_int(ed, b"bitrate", br_kbps)
        obs.data_set_string(ed, b"preset2", args["preset"].encode())
        obs.data_set_string(ed, b"tune", b"hq")
        obs.data_set_string(ed, b"multipass", args["multipass"].encode())
        obs.data_set_int(ed, b"bf", max(0, min(4, args["bframes"])))
        if args["profile"] not in ("auto", ""):
            obs.data_set_string(ed, b"profile", args["profile"].encode())
    else:  # obs_x264 (CPU fallback): map CQP→CRF, otherwise CBR/VBR bitrate
        if rc == "CQP":
            obs.data_set_string(ed, b"rate_control", b"CRF")
            obs.data_set_int(ed, b"crf", max(0, min(51, args["cq"])))
        else:
            obs.data_set_string(ed, b"rate_control", rc.encode())
            obs.data_set_int(ed, b"bitrate", br_kbps)
        # x264 'preset' is a speed name, not p1–p7 — keep it fast to spare the CPU.
        obs.data_set_string(ed, b"preset", b"veryfast")
    obs.data_set_int(ed, b"keyint_sec", 2)
    venc = obs.venc_create(enc_id.encode(), b"venc", ed, None)
    obs.data_release(ed)
    obs.enc_set_video(venc, obs.get_video())
    log(f"video encoder: {enc_id} rc={rc} "
        f"{'cq='+str(args['cq']) if rc=='CQP' else 'br='+str(br_kbps)+'k'} "
        f"preset={args['preset']} multipass={args['multipass']}")

    # one AAC encoder per used mixer track (1 for mixed, N for separate tracks)
    audio_encoders = []   # (track_idx, encoder)
    for track_idx in sorted({ti for (_, ti) in audio_sources}):
        adn = obs.data_create()
        obs.data_set_int(adn, b"bitrate", 160)
        ae = obs.aenc_create(b"ffmpeg_aac", f"aenc{track_idx}".encode(), adn, track_idx, None)
        obs.data_release(adn)
        obs.enc_set_audio(ae, obs.get_audio())
        audio_encoders.append((track_idx, ae))

    # replay buffer output
    od = obs.data_create()
    obs.data_set_string(od, b"directory", SPOOL_DIR.encode())
    obs.data_set_string(od, b"format", b"clip_%CCYY-%MM-%DD_%hh-%mm-%ss")
    obs.data_set_string(od, b"extension", b"mp4")
    obs.data_set_bool(od, b"allow_spaces", False)
    obs.data_set_int(od, b"max_time_sec", args["buffer_secs"])
    obs.data_set_int(od, b"max_size_mb", 1024)
    out = obs.output_create(b"replay_buffer", b"clip_rb", od, None)
    obs.data_release(od)
    if not out:
        reply("error: replay_buffer output create failed")
        return 1
    obs.output_set_venc(out, venc)
    for track_idx, ae in audio_encoders:
        obs.output_set_aenc(out, ae, track_idx)

    if not obs.output_start(out):
        err = obs.output_last_err(out)
        reply("error: output start failed: " + (err.decode() if err else "unknown"))
        return 1

    ph = obs.output_proc(out)
    reply("ready")

    def do_save(dest):
        buf = (C.c_uint8 * 256)()
        cd = calldata(stack=C.cast(buf, C.c_void_p), size=0, capacity=256, fixed=True)
        t0 = time.time()
        if not obs.proc_call(ph, b"save", C.byref(cd)):
            return False
        produced = None
        for _ in range(200):
            produced = newest_stable_mp4(SPOOL_DIR, t0 - 1)
            if produced:
                break
            time.sleep(0.1)
        if not produced:
            return False
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            try:
                os.replace(produced, dest)          # fast path (same drive)
            except OSError:
                import shutil as _sh
                _sh.move(produced, dest)             # cross-drive fallback
            return True
        except OSError as e:
            log(f"relocate failed: {e}")
            return False

    # protocol loop
    for line in sys.stdin:
        line = line.strip().lstrip("﻿")
        if line.startswith("save "):
            dest = line[5:].strip()
            reply("saved" if do_save(dest) else "error: save failed")
        elif line == "stop":
            break
        elif line:
            reply(f"error: unknown command: {line}")

    obs.output_stop(out)
    # libobs teardown from a foreign host can access-violate; clip is finalized.
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
