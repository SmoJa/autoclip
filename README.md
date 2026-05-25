# AutoClip

Automatic game clip recorder for Linux. Monitors game events in real time and triggers [gpu-screen-recorder](https://git.dec05eba.com/gpu-screen-recorder/about/) to save a replay-buffer clip the moment something clip-worthy happens — kills, clutches, vocal reactions, spoken phrases, or a manual hotkey.

![License: GPL-3.0](https://img.shields.io/badge/license-GPL--3.0-blue)
![Platform: Linux](https://img.shields.io/badge/platform-Linux-lightgrey)

> **Current game support: Counter-Strike 2 only.**
> AutoClip uses a plugin architecture — each game is a self-contained module. CS2 is the only built-in game plugin right now, but adding support for a new game is a single new file. Contributions welcome.

---

## Features

- **Automatic game triggers** — CS2 kills, headshots, multi-kills, clutches, and bomb events via Game State Integration (GSI). Zero anti-cheat risk — GSI is an official Valve feature.
- **Audio triggers** — two built-in triggers monitor your mic and chat audio simultaneously:
  - **Reactions** — detects laughter, screaming, and shouting via PANNs CNN6 (local ML inference). Each reaction type has independent sensitivity and cooldown settings.
  - **Voice phrases** — saves a clip when you say a configured phrase (e.g. "clip that"). Uses Vosk offline speech recognition — no cloud, no API key.
  - New triggers can be added as single-file plugins.
- **Manual hotkey** — save a clip at any time with `Ctrl+Shift+S` (configurable).
- **Clip browser** — browse, preview, trim, and export clips with a built-in player. The timeline shows stacked per-track waveforms with per-track volume dials, frame-step controls, and set-in/set-out buttons.
- **Multi-track audio** — record game audio, mic, and chat as separate tracks; adjust per-track volume in the timeline; mix down on export.
- **Clip metadata** — events, map, round, team, and score are encoded in the filename so clips are self-describing.
- **Extensible** — adding a new game or audio trigger is a single new file. See [Adding a new game](#adding-a-new-game) below.

---

## Screenshots

![Clip browser](Screenshot_2.png)
*Clip browser — browse by game and date, preview thumbnails*

![Clip timeline](Screenshot_3.png)
*Player with timeline scrubber, event markers, and per-track waveforms*

![Settings](Screenshot_1.png)
*Settings — recorder, audio tracks, encoding, and clip timing*

---

## Requirements

### System packages

| Package | Purpose |
|---------|---------|
| [gpu-screen-recorder](https://git.dec05eba.com/gpu-screen-recorder/about/) | Replay buffer recording |
| `mpv` / `libmpv` | In-app clip playback |
| `ffmpeg` | Thumbnails, export, clip probing |
| Python 3.10+ | Runtime |

Install `mpv` and `ffmpeg` via your distro's package manager.

Install gpu-screen-recorder via your distro's package manager, or as a Flatpak:
```bash
flatpak install flathub com.dec05eba.gpu_screen_recorder
```
See the [gpu-screen-recorder README](https://git.dec05eba.com/gpu-screen-recorder/about/) for other options. The default expected path is `/usr/local/bin/gpu-screen-recorder` (configurable in Settings → Recorder).

### Python dependencies

```bash
pip install -r autoclip/requirements.txt
```

| Package | Purpose |
|---------|---------|
| PyQt6 | GUI |
| python-mpv | mpv Python bindings |
| pynput | Global hotkey |
| sounddevice | Microphone/monitor capture for audio triggers |
| numpy | Audio processing |
| onnxruntime | ML inference for Reactions trigger |
| requests | GSI HTTP server |
| send2trash | Trash clips from the browser (optional, falls back to permanent delete) |
| vosk | Offline speech recognition for Voice trigger (optional) |

> **Hotkey note:** `pynput` requires your user to be in the `input` group:
> ```bash
> sudo usermod -aG input $USER
> # log out and back in
> ```

### Audio trigger models (optional)

Both audio trigger models are downloaded automatically the first time you enable them in Settings — no extra steps needed.

- **Reactions** — uses PANNs CNN6 (~25 MB ONNX). Downloaded via **Settings → Audio Triggers → Reactions**. If you prefer to generate the model file yourself, see `scripts/export_audio_classifier.py`.
- **Voice phrases** — uses the Vosk small English model (~40 MB). Downloaded via **Settings → Audio Triggers → Voice**.

---

## Installation

```bash
curl -s https://raw.githubusercontent.com/SmoJa/autoclip/main/install.sh | bash
```

This installs AutoClip to `~/.local/share/autoclip/`, installs Python dependencies, and adds an entry to your application menu. After that, launch AutoClip from your application menu or run:

```bash
~/.local/share/autoclip/autoclip/run.sh
```

To enable autostart on login, use the toggle in **Settings → Application**.

---

## Running

```bash
~/.local/share/autoclip/autoclip/run.sh
```


---

## First-time setup

1. **Settings → Recorder** — verify the `gpu-screen-recorder` path and output directory.
2. **Settings → Audio Tracks** — select your game audio source and optionally mic/chat tracks.
3. **Game Triggers → CS2 → Install GSI Config** — writes the GSI config into your CS2 installation so CS2 broadcasts events to AutoClip. Restart CS2 afterwards.
4. Launch a game. AutoClip detects the process, starts the recorder, and begins saving clips on configured events.

---

## How it works

```
CS2 (GSI HTTP) ──► cs2.py ──► controller.py ──► clip_trigger.py ──► gpu-screen-recorder
                                    ▲
         reactions.py / voice.py ─┘  (audio triggers, same pipeline)
```

- gpu-screen-recorder runs a **replay buffer** — the last N seconds of video/audio are held in RAM at near-zero CPU cost.
- When a trigger fires, AutoClip waits the configured post-event delay (default 7 s), then signals gsr to flush the buffer to disk.
- The saved clip is renamed with full metadata encoded in the filename: map, mode, round, score, events with timestamps.

---

## Clip filename format

```
CS2_comp_de_dust2_r8_ct_14-12_ev[hs:7.0:awp,mk:5.0:awp]_2026-05-23_21-32-05.mkv
 │    │    │         │  │       └─ events: trigger:secs_before_end:weapon
 │    │    │         │  └─ score ct-t
 │    │    │         └─ round
 │    │    └─ map
 │    └─ mode
 └─ game
```

---

## Adding a new game

Create `autoclip/games/mygame.py` with a class inheriting `GamePlugin`. Set `NAME`, `PROCESS_NAMES`, trigger/mode vocabulary tables, and implement `start()` / `stop()`. The registry auto-discovers it on next launch — no other files need changing. See `games/base.py` for the full interface and `games/cs2.py` as a reference.

## Adding a new audio trigger

Create `autoclip/audio_triggers/myplugin.py` inheriting `AudioTriggerPlugin`. Set `NAME`, `TRIGGER_NAME`, and implement `start()` / `stop()`. See `audio_triggers/base.py` for the full interface and `audio_triggers/reactions.py` or `audio_triggers/voice.py` as references.

---

## Known limitations and untested areas

AutoClip is in early release. The following areas have not been thoroughly tested and may have rough edges:

- **Reactions and Voice triggers** — implemented and functional but not extensively tested in real gameplay sessions. Sensitivity defaults and phrase detection accuracy may need tuning per setup.
- **AMD and Intel GPUs** — codec and hardware detection is implemented for AMD and Intel but has only been tested on NVIDIA. Encoding settings may need manual adjustment.
- **HDR recording** — the HDR codec path exists but is untested. HDR clip previews in the player may show highlights as slightly grey rather than pure white; exports are unaffected.
- **Plugin architecture** — the game and audio trigger plugin systems are designed for extensibility but have only been tested with the built-in plugins. There may be rough edges when adding new ones.
- **Non-KDE desktop environments** — developed and tested on KDE Plasma. GNOME, Hyprland, and other compositors should work but autostart and desktop launcher integration is untested.
- **Non-Nobara/Fedora systems** — only tested on Nobara Linux. Other distros should work but may require additional setup.
- **Native Wayland** — AutoClip runs under XWayland. Native Wayland rendering is not currently supported.
Contributions and bug reports are welcome.

---

## Project layout

```
autoclip/
├── main.py                        # Entry point
├── requirements.txt
├── core/
│   ├── controller.py              # Orchestrates all components
│   ├── config.py                  # Settings (persisted to ~/.config/autoclip/config.json)
│   ├── clip_trigger.py            # Accumulates events, fires save signal, renames clips
│   ├── recorder.py                # gpu-screen-recorder lifecycle
│   ├── clips.py                   # Clip scanning, probing, trim, export
│   ├── metadata.py                # Filename encoding/decoding
│   ├── audio.py                   # PipeWire device enumeration
│   ├── audio_mix.py               # Post-processing audio mix-down
│   ├── hardware_detection.py      # GPU/HDR detection, codec selection
│   └── model_cache.py             # Shared ONNX session cache
├── games/
│   ├── base.py                    # GamePlugin interface
│   ├── registry.py                # Auto-discovery
│   └── cs2.py                     # CS2 GSI integration
├── audio_triggers/
│   ├── base.py                    # AudioTriggerPlugin interface
│   ├── registry.py                # Auto-discovery
│   ├── reactions.py               # PANNs CNN6 laughter/screaming/shouting detector
│   └── voice.py                   # Vosk offline voice phrase trigger
└── gui/
    ├── main_window.py             # Main window, Dashboard, Settings tabs
    ├── clips_tab.py               # Clip browser and player
    ├── player.py                  # Embedded mpv (OpenGL)
    ├── timeline.py                # Seek bar, event marker pills, stacked per-track waveforms
    ├── track_waveforms.py         # Per-track waveform extraction (QObject, background thread)
    ├── audio_tracks.py            # Audio track configuration widget
    ├── theme.py                   # Theme system
    └── widgets.py                 # Shared small widgets (VolumeDial, NoScroll* variants)
scripts/
└── export_audio_classifier.py    # One-time PANNs CNN6 → ONNX export
```

---

## File locations

| Path | Purpose |
|------|---------|
| `~/.config/autoclip/config.json` | Settings |
| `~/.config/autoclip/themes/` | Custom themes |
| `~/Videos/Autoclip/` | Clips (default, configurable) |
| `~/.cache/autoclip/thumbs/` | Thumbnail cache |
| `~/.cache/autoclip/models/` | ML models |
| `/tmp/autoclip.log` | Log output |

---

## License

AutoClip is licensed under the [GNU General Public License v3.0](LICENSE).

The PANNs CNN6 model weights ([Kong et al.](https://arxiv.org/abs/1912.10211)) are MIT licensed.
