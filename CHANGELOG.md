# Changelog

All notable changes to AutoClip are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [0.1.2] - 2026-05-24

### Added

- **Reactions audio trigger** — detects laughter, screaming, and shouting using PANNs CNN6 (AudioSet). Each reaction type has an independent enable toggle, sensitivity slider, and cooldown. A single model session and capture loop serves all enabled types, so CPU cost is unchanged regardless of how many are on.
- **Voice phrase trigger** — new plugin using Vosk offline speech recognition. Saves a clip when any configured spoken phrase is detected on mic or chat audio (defaults: "clip that, save that, clip it"). Phrase list is fully configurable. The Vosk small-English model (~40 MB) is downloaded automatically via Settings when first enabled.
- **Frame step buttons** — `◀` / `▶` buttons next to the play button step one frame at a time through the current clip (uses mpv `frame-back-step` / `frame-step`).
- **Set In / Set Out buttons** — buttons next to the in/out time labels snap the trim handle to the current playhead position.
- **Multi-track waveforms in the timeline** — all recorded audio tracks are rendered as stacked waveform strips inside the timeline widget. Each strip is colour-coded by track type.
- **Per-track volume dials** — a compact radial dial sits to the left of each waveform strip. Supports scroll-wheel (1 % per notch) and vertical drag adjustment. Waveform bar heights update in real time as you adjust the dial.
- **Log-scaled waveform display** — waveform samples are converted to dB (floor −42 dB) before display so quiet audio sections remain visible without being artificially boosted to full height.
- **Beautified event marker pills on hover** — marker pills in the timeline show a short label (trigger + weapon code) by default. Hovering expands to a full human-readable label (e.g. "Headshot AK-47") drawn on top without shifting other pills.
- **`VolumeDial` widget** — new reusable `VolumeDial(QWidget)` in `gui/widgets.py`: 270° arc dial with percentage label, scroll-wheel and drag input, emits `value_changed(float)`.
- **HDR preview warning badge** — an orange `⚠ HDR Preview` badge appears in the player top bar when an HDR clip is loaded. Hovering shows a tooltip explaining that super bright scenes and flashbangs may appear grey in the preview, but exports and raw files are unaffected.
- **`TRIGGER_LOG_STYLES` on `AudioTriggerPlugin`** — dict mapping trigger name → log style tuple, for plugins that fire multiple distinct trigger names from a single instance (used by Reactions).

### Changed

- **Reactions replaces Laughter** — `laughter.py` has been replaced by `reactions.py`. The old single-category laughter detector is superseded; all existing laughter trigger clips retain their filenames and metadata.
- **Timeline layout** — the timeline is now divided into a pill zone (top, 74 px) and a waveform zone (bottom, 138 px), separated by a horizontal rule. The playhead triangle and handle arrows are confined to the waveform zone.
- **Timeline horizontal coordinate system** — all normalised positions are mapped to an *effective width* `ew = widget_width − DIAL_W` so seeks, handles, playhead, and marker stems all align with the waveform area.
- **Waveform extraction** — `MultiTrackWaveform` is now a pure `QObject` (non-visual). It probes stream count, extracts per-track RMS waveforms on a daemon thread, and emits `tracks_probed` / `waveform_ready` signals. Thread safety is handled via an internal relay signal.
- **Track colour assignment** — track colours are resolved from the theme's `track_colors()` map keyed by `track_type`; unknown types fall back through the colour list.
- **Collapsible sections** — expanded sections now stack naturally with the next section immediately below, instead of floating to fill the screen. All Audio Trigger sections start collapsed. Each section header shows an enable/disable checkbox that works even when the section is collapsed.
- **Removed footer status bar** — the "recording / idle" strip at the bottom of the main window has been removed. Recording state is shown in the system tray icon and the event log.
- **`AudioTriggerPlugin.on_trigger` now accepts an optional `name` argument** — allows a single plugin to fire distinct named triggers (e.g. "laughter", "screaming", "shouting") without registering multiple plugins.
- **HDR thumbnail tone-mapping** — switched from hard-clip (`tonemap=clip`) to Hable (`tonemap=hable`) so highlights in HDR clips are compressed gracefully rather than clipping to white.
- **HDR SDR conversion and export** — same Hable tone-mapping applied to `convert_to_sdr` and all export paths.

### Fixed

- **Export clips stuck on "Loading"** — when switching from a multi-track clip to a single-track export file, the previous `lavfi-complex` audio filter (referencing `[aid2]`, `[aid3]`, etc.) remained active on mpv, causing it to silently fail to open the new file. The filter is now cleared before loading any single-track file.
- **Thumbnail cache bypassed on every visit** — the pre-check cache key was missing the `hdr`/`sdr` suffix, so it never matched the stored thumbnail key and every thumbnail was re-generated from scratch. All thumbnails now load instantly from cache on repeat visits.
- **HDR thumbnails overexposed** — `npl=80` set during the flashbang fix mapped 80 nits to white, causing normal gameplay highlights to clip. Reverted to `npl=100` with Hable tone-mapping, which compresses the full HDR range cleanly.

---

## [0.1.1] - 2026-05-13

### Added

- **Clip delete button** — red × button (hover-reveal, top-left of card) deletes a clip or folder directly from the grid. Uses `send2trash` where available, falls back to permanent delete with a confirmation dialog.
- **Open Folder button** — opens the directory currently shown in the grid (not the library root).
- **Export name uniqueness** — if an export filename already exists, a numeric suffix is appended automatically (e.g. `clip (1).mp4`).
- **Clip watchdog** — a `QFileSystemWatcher` monitors the output directory and triggers a library refresh whenever a new clip is saved, so the browser updates without a manual refresh.
- **Laughter detection sensitivity slider** — adjustable in Settings → Audio Triggers.

### Changed

- **Thumbnail seek** — thumbnails are generated at the trigger event frame (seconds from clip start) rather than 1 second in, so the card previews the moment the clip was saved for.
- **Thumbnail sharpness** — ffmpeg now scales to fill then crops to exact card size, so Qt never upscales a smaller image.
- **Grid refresh** — `update_clips` no longer rebuilds the entire widget tree; existing cards are updated in place and only new cards are appended, eliminating scroll-position reset on library changes.
- **CS2 GSI kill detection** — kill events are now captured at `phase_changed → live` in addition to the raw `kills` delta, fixing missed kills at round transitions.
- **`gpu-screen-recorder` failure detection** — gsr stderr is monitored in a background thread; a persistent silent crash now triggers a restart rather than leaving the recorder in a broken state.

### Fixed

- **Laughter model downloader crash** — `QThread` was garbage-collected before the download finished, causing a segfault. The thread is now retained for its lifetime.
- **Desktop entry path** — the `.desktop` file path is now resolved from the script location rather than `$PWD`, fixing the application menu entry when AutoClip is not launched from its own directory.

---

## [0.1.0] - 2026-05-04

Initial public release.
