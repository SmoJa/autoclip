# SPDX-License-Identifier: GPL-3.0-or-later
"""Audio-only libobs process-loopback capture (Windows).

Captures ONE application's audio via libobs `wasapi_process_output_capture` and
streams raw float32 mono PCM at the requested sample rate to stdout, for the audio
triggers (Reactions/Phrases) to analyse. This reuses the exact per-app capture the
recorder uses — OBS already solved Windows process loopback — instead of a bespoke
WASAPI implementation. Run by the bundled obs-runtime python.

    python obs_audio_capture.py <exe> [--rate 16000]

Protocol: raw little-endian float32 mono samples on stdout (nothing else — libobs
logging is redirected to stderr). Exits on stdin EOF/"stop" or a broken stdout pipe.
"""
import sys
import os
import threading
import queue
import ctypes as C

# Reuse the recorder's libobs bindings, path constants, and window finder.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import obs_recorder as R  # noqa: E402

MAX_CH = 8


class _audio_data(C.Structure):
    _fields_ = [("data", C.POINTER(C.c_uint8) * MAX_CH),
                ("frames", C.c_uint32),
                ("timestamp", C.c_uint64)]


_AUDIO_CB = C.CFUNCTYPE(None, C.c_void_p, C.c_void_p, C.POINTER(_audio_data), C.c_bool)


def _parse(argv):
    a = {"exe": None, "rate": 16000}
    i = 0
    while i < len(argv):
        if argv[i] == "--rate" and i + 1 < len(argv):
            a["rate"] = int(argv[i + 1]); i += 2
        elif a["exe"] is None:
            a["exe"] = argv[i]; i += 1
        else:
            i += 1
    return a


def main():
    args = _parse(sys.argv[1:])
    if not args["exe"]:
        sys.stderr.write("error: <exe> required\n"); return 1
    exe = args["exe"].lower()
    rate = args["rate"]

    # Reserve the real stdout for PCM, then point C-level stdout (fd 1) at stderr so
    # libobs logging can't corrupt the PCM stream.
    out_fd = os.dup(1)
    os.dup2(2, 1)

    obs = R.Obs()
    if not obs.startup(b"en-US", None, None):
        sys.stderr.write("error: obs_startup failed\n"); return 1
    obs.add_data_path((R.OBS_DATA_LIBOBS + "\\").encode())
    import glob
    for dll in glob.glob(os.path.join(R.OBS_BIN, "*.dll")):
        try: C.CDLL(dll)
        except OSError: pass

    # Audio-only at the trigger's rate, mono (SPEAKERS_MONO=1) — libobs resamples/downmixes.
    oai = R.obs_audio_info(samples_per_sec=rate, speakers=1)
    obs.reset_audio(C.byref(oai))
    mod = R.P()
    binp = os.path.join(R.OBS_PLUGIN_BIN, "win-wasapi.dll").encode()
    datap = os.path.join(R.OBS_PLUGIN_DATA, "win-wasapi").encode()
    if obs.open_module(C.byref(mod), binp, datap) == 0:
        obs.init_module(mod)
    obs.post_load()

    add_cb = obs.lib.obs_source_add_audio_capture_callback
    add_cb.argtypes = [C.c_void_p, _AUDIO_CB, C.c_void_p]
    add_cb.restype = None

    # Bounded queue: if the consumer falls behind, drop the OLDEST audio (real-time
    # detection only cares about recent audio) rather than block libobs's audio thread.
    q = queue.Queue(maxsize=256)

    def _on_audio(param, source, adp, muted):
        ad = adp.contents
        if not ad.data[0] or ad.frames == 0:
            return
        pcm = C.string_at(ad.data[0], ad.frames * 4)   # float32 mono bytes
        try:
            q.put_nowait(pcm)
        except queue.Full:
            try: q.get_nowait()
            except queue.Empty: pass
            try: q.put_nowait(pcm)
            except queue.Full: pass

    cb = _AUDIO_CB(_on_audio)

    # Find the app window and create the per-app capture source.
    w = R.find_game_window(os.path.splitext(exe)[0])
    if not w:
        sys.stderr.write(f"error: window for {exe!r} not found\n"); return 1
    ad = obs.data_create()
    obs.data_set_string(ad, b"window", R.obs_window_string(*w).encode())
    obs.data_set_int(ad, b"priority", 2)   # match by executable
    src = obs.source_create(b"wasapi_process_output_capture", b"trig_cap", ad, None)
    obs.data_release(ad)
    if not src:
        sys.stderr.write("error: source create failed\n"); return 1
    add_cb(src, cb, None)
    obs.set_output_source(1, src)   # activate -> capture thread starts
    sys.stderr.write(f"ready: capturing {exe} at {rate} Hz mono\n"); sys.stderr.flush()

    # Writer thread drains PCM to the real stdout; exits if the pipe breaks (parent gone).
    def _writer():
        while True:
            pcm = q.get()
            if pcm is None:
                break
            try:
                os.write(out_fd, pcm)
            except OSError:
                os._exit(0)
    threading.Thread(target=_writer, daemon=True).start()

    # Block on stdin: parent closes it (EOF) or sends "stop" to stop us.
    for line in sys.stdin:
        if line.strip() == "stop":
            break
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
