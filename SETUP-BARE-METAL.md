# Bare-metal Windows bring-up

Quick guide to get AutoClip + the GSR-style recorder running on a fresh Windows PC.
On bare metal you can ignore everything the dev VM needed (virtual display, Sunshine,
Moonlight, Proxmox GPU fixes) — you have a real GPU and monitor.

## 1. Unzip

Unzip `autoclip-windows-dev.zip` anywhere, e.g. `C:\dev\autoclip-windows-dev`.
The git repo comes with it (history + both branches). You'll be on the
`gsr-style-recorder` branch (the fast pipeline). `git checkout master` is the fallback.

## 2. Install toolchain (one-time)

Run in an **admin** PowerShell. Most are winget one-liners.

```powershell
# Python 3.11+ (if not already installed)
winget install Python.Python.3.11

# Rust (MSVC toolchain) — installs per-user, no admin needed
Invoke-WebRequest https://win.rustup.rs/x86_64 -OutFile $env:TEMP\rustup-init.exe
& $env:TEMP\rustup-init.exe -y --default-toolchain stable-x86_64-pc-windows-msvc --profile minimal

# Visual Studio Build Tools — C++ workload (needed to LINK the Rust recorder). ~2GB.
winget install Microsoft.VisualStudio.2022.BuildTools --override "--add Microsoft.VisualStudio.Workload.VCTools --includeRecommended --passive --norestart --wait"

# ffmpeg/ffprobe and mpv (video playback)
winget install Gyan.FFmpeg
winget install mpv.net
```

Add `%LOCALAPPDATA%\Programs\mpv.net` to your user PATH (needed for `libmpv-2.dll`).

## 3. Python deps

```powershell
cd <repo>\autoclip
py -m pip install -r requirements-windows.txt
py -m pip install pynput        # optional: manual hotkey support
```

## 4. Build the recorder

```powershell
cd <repo>\autoclip-recorder
cargo build --release
# -> target\release\autoclip-recorder.exe (the app finds it here automatically)
```

A prebuilt `autoclip-recorder.exe` from the VM is included at the repo root for a quick
smoke test, but **rebuild it** — a fresh build on your machine is the clean baseline.
(If the prebuilt one fails to start, you're missing the VC++ 2015-2022 redistributable;
the build tools above include it.)

## 5. Run

```powershell
cd <repo>
py -m autoclip.main
```

## 6. CS2 end-to-end test (the whole point)

1. Install/launch CS2 via Steam.
2. In AutoClip, install the GSI config (button in the CS2 settings), or copy
   `gamestate_integration_autoclip.cfg` to
   `...\Steam\steamapps\common\Counter-Strike Global Offensive\game\core\cfg\`.
   It points GSI at `http://127.0.0.1:3000/`.
3. Launch CS2, get a few kills. AutoClip should detect the game, start the recorder,
   and save clips in ~1-2s each to `%USERPROFILE%\Videos\Autoclip\CS2\`.
4. **Watch `autoclip-recorder.exe` private bytes in Task Manager** during a longer
   session — see the "open risk" note in CLAUDE.md. Expected: flat at ~120MB. If it
   climbs without bound, tell Claude — there's a documented fix direction.

## 7. If the recorder misbehaves

```powershell
cd <repo>
git checkout master
cd autoclip-recorder; cargo build --release
```

That reverts to the slow-but-proven pipeline (90s saves, but reliable).

## Where the context lives

The `claude-context/` folder in this zip has the full development memory: the project
overview, the overnight build journal, and the morning report with every bug we fixed
and why. Point your new Claude session at these at the start so it has the full history.
The recorder diagnostic logging is intentionally left ON (set `RUST_LOG=info` when
launching the recorder standalone to see ring stats and pipeline counters).
