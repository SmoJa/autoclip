# Contributing to AutoClip

Thanks for your interest in contributing. AutoClip is in early release and there's plenty of room for improvement.

## Reporting bugs

Open a [GitHub Issue](https://github.com/SmoJa/autoclip/issues) and include:

- Your distro and desktop environment
- GPU make and model
- AutoClip version
- What you expected to happen and what actually happened
- Relevant log output from `/tmp/autoclip.log`

## Adding a new game

Each game is a self-contained plugin in `autoclip/games/`. The registry auto-discovers any file placed there — no other files need changing.

1. Create `autoclip/games/mygame.py` with a class inheriting `GamePlugin`
2. Set the class attributes:
   - `NAME` — display name, e.g. `"Rocket League"`
   - `PROCESS_NAMES` — list of process names to watch for, e.g. `["RocketLeague"]`
   - `TRIGGER_ABBREVS` / `TRIGGER_DISPLAY` — short codes and display names for each event type your plugin fires, e.g. `{"goal": "gl"}` and `{"gl": "Goal"}`
   - `MODE_ABBREVS` / `MODE_DISPLAY` — same for game modes
   - `TRIGGER_LOG_STYLE` — how each trigger appears in the event log: `{"gl": ("Goal", "#color", "#bg", bold)}`
3. Implement `start()` — begin watching for game events (start a GSI server, tail a log file, or whatever method the game supports)
4. Implement `stop()` — clean up threads and connections
5. Call `self.on_event(event_string)` whenever something clip-worthy happens

The event string format is: `"trigger|mode|map|round|team|ct_score|t_score|weapon"`. Any fields your game doesn't have can be left empty.

See `autoclip/games/base.py` for the full interface and `autoclip/games/cs2.py` as a working reference. CS2 uses Valve's Game State Integration (HTTP) — other games might use log file parsing, a game's own API, or a simple polling loop.

## Adding a new audio trigger

Audio triggers follow the same pattern, in `autoclip/audio_triggers/`.

1. Create `autoclip/audio_triggers/myplugin.py` with a class inheriting `AudioTriggerPlugin`
2. Set the class attributes:
   - `NAME` — display name shown in the Audio Triggers tab, e.g. `"Gunshot"`
   - `TRIGGER_NAME` — short code used in clip filenames, e.g. `"gunshot"`
   - `TRIGGER_LOG_STYLE` — how it appears in the event log: `("Gunshot", "#color", "#bg", bold)`
3. Implement `start()` — begin listening on the audio source (mic, monitor device, or both)
4. Implement `stop()` — stop all threads and close audio streams
5. Call `self.on_trigger()` when the target sound is detected — the controller handles the rest

The trigger runs in a background thread and shares the same clip-save pipeline as game triggers. If your trigger needs a settings UI, implement `get_config_widget()` to return a QWidget.

See `autoclip/audio_triggers/base.py` for the interface and `autoclip/audio_triggers/laughter.py` as a reference. The laughter plugin is a good template for ML-based detection — it loads an ONNX model via the shared `ModelCache`, captures audio in a ring buffer with `sounddevice`, and runs inference on a sliding window.

## General guidelines

- Keep game and audio trigger logic inside their respective plugin files. The rest of the app is game-agnostic and should stay that way.
- The plugin registry is auto-discovery based — if your plugin isn't being picked up, check that your class sets a non-empty `NAME` and inherits the correct base class.
- Log with `logger.info` for meaningful events, `logger.debug` for noisy detail. Avoid logging inside tight loops.
- Qt widgets must only be updated from the main thread. Use `QTimer.singleShot(0, fn)` or signals to bridge from background threads.
