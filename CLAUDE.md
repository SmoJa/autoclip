# AutoClip — Claude Development Notes

## Project overview

AutoClip is an open-source Linux game clip recorder (https://github.com/SmoJa/autoclip).
This branch adds a Windows port. The goal is zero regression on Linux — all platform
differences are isolated in split files, never mixed into shared code.

**State as of 2026-06-12:** Windows port is functional end-to-end. App runs stably,
detects CS2 via GSI, and the recording backend has been rewritten into a fast
GSR-style encode-once pipeline (saves in ~1.2s, ~120MB RAM). Validated against a
synthetic test target; **the real CS2 + GPU end-to-end test is the next step** and is
why we're moving to a bare-metal Windows machine (the dev VM had GPU passthrough and
contention problems — see "History" below).

## Git layout — IMPORTANT

This is a git repo with two branches:
- **`master`** — the older recorder pipeline (re-encodes the whole buffer on save,
  ~90s saves, ~1.4GB RAM, writes a spool file). Slow but proven. **Safety fallback.**
- **`gsr-style-recorder`** — the new encode-once pipeline. **This is the active branch
  and what you want.** All the wins (1.2s saves, 120MB RAM, no spool, clean H.264).

If the new pipeline ever misbehaves: `git checkout master && (cd autoclip-recorder && cargo build --release)`.
To keep it after a good real-world test: merge `gsr-style-recorder` into `master`.

## Repository layout

```
autoclip/                   Python package (main app — PyQt6 GUI)
  core/
    audio.py                Dispatch shim → audio_{linux,windows}.py
    hardware_detection.py   Dispatch shim → hardware_detection_{linux,windows}.py
    recorder.py             Dispatch shim → recorder_{linux,windows}.py
    recorder_windows.py     Windows RecorderManager: spawns autoclip-recorder.exe,
                            stdin/stdout protocol, watchdog, save queue. Forces H.264.
    config.py               Platform-aware paths (%APPDATA%\autoclip on Windows).
                            __post_init__ forces gpu_recorder_codec=h264 on Windows.
    clips.py                Clip scanner — _FFMPEG/_FFPROBE resolve via winget path
    controller.py           Game watcher: ctypes snapshot + QTimer (Win) / thread (Linux)
  gui/
    main_window.py          Win: QT_OPENGL=desktop, game-detection QTimer, codec=h264/hevc
    clips_tab.py            Thumbnail cache %LOCALAPPDATA%\autoclip\thumbs (Win)
    player.py               Linux = QOpenGLWidget+MpvRenderContext; Win = QWidget + --wid
  requirements-windows.txt  Windows deps

autoclip-recorder/          Rust CLI — the recording backend
  Cargo.toml                Depends on the VENDORED windows-record (path = ../vendor/...)
  src/main.rs               stdin/stdout protocol: ready / save <path> / saved / stop
                            Args: --process --fps --bitrate --buffer-secs --codec
                                  --width --height [--no-audio] [--audio-mic]

vendor/windows-record/      Vendored + heavily patched copy of the windows-record crate.
                            We own this now — upstream 0.1.0 had multiple bugs.
  src/encode/mod.rs         NEW: async hardware H.264 encoder MFT wrapper, EncodedVideoRing
                            (compressed-packet ring, keyframe-aligned eviction), PcmRing.
  src/encode/mux.rs         NEW: save-time mux of compressed packets → MP4 (no re-encode);
                            SPS/PPS sequence-header extraction fallback.
  src/processing/mod.rs     process_samples_encoded() — capture→NV12→encoder→ring; audio ring
  src/recorder/inner.rs     init() wires capture+encoder+rings; save_replay() = mux snapshot
  src/capture/              DXGI duplication (video), WASAPI (audio/mic), window finding

dev/                        Test scripts (gitignored). fakegame.ps1 = synthetic test target;
                            test-*.ps1 = pipeline/soak/edge tests; test_python_integration.py
```

## Platform split pattern

```python
# audio.py (shim)
import sys
if sys.platform == "win32":
    from autoclip.core.audio_windows import *
else:
    from autoclip.core.audio_linux import *
```

Never put `if sys.platform` guards inside shared logic files.

## Windows recording backend — GSR-style encode-once pipeline (NEW, 2026-06-12)

The old pipeline stored RAW frames in the replay buffer and **re-encoded the entire
buffer on every save** (~90s, ~1.4GB RAM, plus a continuous spool file to disk). The
new pipeline mirrors gpu-screen-recorder's design:

1. A hardware H.264 encoder MFT (found via `MFTEnumEx` + `MFT_ENUM_FLAG_HARDWARE`)
   encodes each captured frame **once**, as it arrives, on its own event-loop thread
   (`VideoEncoder::run` — services `METransformNeedInput`/`METransformHaveOutput`).
2. The compressed packets go into `EncodedVideoRing` — a `VecDeque<Arc<EncodedPacket>>`
   evicted **whole GOPs at a time** so the buffer always starts on a keyframe.
   30s of 12Mbps H.264 ≈ 45MB (vs ~1.4GB of raw frames).
3. Audio stays as raw PCM in `PcmRing` (cheap, ~5MB) and is AAC-encoded only at save.
4. **Save = mux** (`encode/mux.rs`): the sink writer's video input type == output type
   (both H.264) so NO re-encode happens; timestamps rebased to the first keyframe.
   ~1.2s for a 30s clip. **No spool file.**

Key implementation notes / gotchas (windows-rs 0.48):
- D3D11 device needs `D3D11_CREATE_DEVICE_VIDEO_SUPPORT` + multithread protection, and
  the encoder gets it via an `IMFDXGIDeviceManager` (zero-copy NV12 texture input).
- `ICodecAPI` lives in `Media::MediaFoundation` (not `DirectShow`) in this binding;
  `VARIANT` needs the `Win32_System_Ole` feature; `IMFActivate::GetAllocatedString`
  (not `GetString`); `IMFMediaType::GetItem(guid, None)`.
- Keyframe cadence pinned via `MF_MT_MAX_KEYFRAME_SPACING` (= fps, i.e. 1s GOPs) plus a
  best-effort `ICodecAPI` GOP set. Verified: clips start on IDR, exact 1s GOP spacing.
- The crate matches the capture target window by TITLE substring; we added a fallback
  that also matches the owning process's exe name (so "cs2" matches "Counter-Strike 2"),
  and `inner.rs` polls up to 120s for the window to appear (games launch slowly).
- WASAPI timestamp fix: `qpc_position` from `GetBuffer` is already in 100ns units; the
  original crate treated it as raw QPC ticks → garbage timestamps on any machine whose
  QPC frequency ≠ 10MHz (e.g. the dev VM at 100MHz). Fixed in capture/audio.rs + microphone.rs.

The Python side (`recorder_windows.py`) is unchanged in protocol — it still just sends
`save <path>` and waits for `saved`. It forces H.264 (the GTX 960's HEVC MFT produced
broken reference frames), passes the real display resolution via `--width/--height`
(GetSystemMetrics), and uses millisecond-resolution clip filenames (sub-second saves
collide at second granularity).

### One open risk to validate on bare metal
A 30-min soak run *concurrently with other heavy GPU work* showed recorder private
bytes climbing to ~18GB. Could NOT reproduce in isolation — every clean run is flat
(15-min clean soak held at ~120MB). Bare metal with a single game + no VM contention
should be the clean case, but **watch `autoclip-recorder.exe` private bytes during the
first long real session.** If it climbs unbounded, the likely cause is the async encoder
MFT holding NV12 input textures when NVENC is starved; the fix would be pooling the NV12
conversion textures in `processing/video.rs::convert_bgra_to_nv12`.

## Windows stability — Python 3.11.9 + Qt6 heap corruption (FIXED 2026-06-10)

The app used to crash with an access violation ~15s after startup. Root cause: Qt6's
DLL init corrupts Python's process heap during `QApplication.__init__`. After that, any
newly allocated Python lock/semaphore (`threading.Condition.wait()`, `queue.Queue.get()`,
`Popen.wait()`, `PyGILState_Ensure()` from a new C thread) can AV. Rules that keep it
stable (DO NOT REGRESS):

1. **No Python callbacks from foreign C threads.** `player.py` uses `--wid` HWND
   embedding on Windows, not `MpvRenderContext`.
2. **No new background Python threads that block on locks/queues.** Use QTimer on the
   main thread (`controller.tick_game_detection`) or poll with `get_nowait()` + `time.sleep()`.
3. **No `subprocess.run(capture_output=True)` on Windows.** Use `clips._run_capture()`
   (temp files + ctypes `WaitForSingleObject`; HANDLE must be `ctypes.c_void_p` on x64).
4. **No subprocess for process listing.** `controller._list_processes_win32()` uses
   ctypes `CreateToolhelp32Snapshot`.
5. **`probe_clips_async` runs synchronously on Windows.**
6. Pre-import `concurrent.futures`/`multiprocessing` at module load (before Qt).

## Windows dependencies

| Dependency | How |
|---|---|
| Python 3.11+ | python.org installer (InstallAllUsers=1) |
| pip packages | `py -m pip install -r autoclip/requirements-windows.txt` |
| Rust + MSVC toolchain | rustup, `stable-x86_64-pc-windows-msvc` |
| **VS Build Tools (C++ workload)** | `winget install Microsoft.VisualStudio.2022.BuildTools --override "--add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"` — required to link the Rust recorder |
| ffmpeg + ffprobe | `winget install Gyan.FFmpeg` (auto-detected via winget path fallback) |
| libmpv-2.dll | `winget install mpv.net` (need `%LOCALAPPDATA%\Programs\mpv.net` on PATH) |
| pynput (optional) | `py -m pip install pynput` — manual hotkey |

## How to run / build

```powershell
# Run the app
cd <repo>
py -m autoclip.main          # or: py %TEMP%\run_autoclip4.py (faulthandler wrapper)

# Build the recorder
cd autoclip-recorder
cargo build --release        # output: target\release\autoclip-recorder.exe
```

`recorder_windows.py` searches for the exe at: package root → `autoclip-recorder/target/debug/`
→ `autoclip-recorder/target/release/` → PATH.

## Pending work (priority order)

1. **Real CS2 + GPU end-to-end test** (the reason for the bare-metal move). Launch CS2
   via Steam (GSI port 3000, process `cs2.exe`), get kills, confirm clips save fast and
   play back correctly. **Watch recorder memory** (see "open risk" above). The GSI config
   `gamestate_integration_autoclip.cfg` goes in `...\Counter-Strike Global Offensive\game\core\cfg\`.
2. If the test is clean: **merge `gsr-style-recorder` → `master`**, then quiet the
   diagnostic logging (ring stats every 256 packets, pipeline counters every 300 frames —
   currently at info level, intentionally left on for the CS2 test).
3. **Single-instance lock** — named mutex on Windows (`kernel32.CreateMutexW`, check
   `GetLastError() == 183`), lockfile on Linux. Add to top of `run_app()`.
4. **Windows installer** — Inno Setup. Bundle Python deps, ffmpeg, libmpv-2.dll,
   autoclip-recorder.exe, Start-menu shortcut to `pythonw -m autoclip.main`.
5. **Linux smoke test** — Linux paths are unchanged by design but untested this cycle.
6. **README + CHANGELOG** for Windows support, then push to GitHub.

## Known issues / gotchas

- CS2 process is `cs2.exe`; the plugin's `PROCESS_NAMES` substring-matches fine.
- Linux clip filenames use Unicode private-use chars as separators; garbled in Windows
  terminals but ffprobe/ffmpeg handle them. The recorder/app use U+F022 for `:` in
  filenames (NTFS forbids `:`) — `metadata.py` maps it back on parse.
- Thumbnails cached in `%LOCALAPPDATA%\autoclip\thumbs\`; HDR clips tone-mapped (zscale +
  tonemap=hable) before JPEG.
- The clip scanner handles both `output_dir/Game/YYYY-MM-DD/file.mp4` and flat
  `output_dir/Game/file.mp4` layouts.

## History (context — mostly moot on bare metal)

The development VM (Proxmox VM 601, GTX 960 passthrough) needed: a Code 43 fix
(`qm set 601 --cpu host,hidden=1` + vendor-id spoof), a virtual display driver +
Sunshine/Moonlight to get a GPU-driven display (the GPU had no monitor), and VirtIO NIC
UDP-offload disabled for streaming. **None of this applies on a bare-metal machine with
a real GPU and monitor.** The contention-induced memory growth we couldn't fully chase
down was likely a VM/multi-tenant-GPU artifact too — bare metal should be the clean case.
Full blow-by-blow is in the Claude memory files bundled under `claude-context/`.
