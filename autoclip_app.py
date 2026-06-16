# SPDX-License-Identifier: GPL-3.0-or-later
"""Frozen-app bootstrap for AutoClip (PyInstaller target).

This exe bundles the stable runtime (Python + PyQt6 + mpv + onnxruntime + …) but
NOT the `autoclip` package itself — that ships as loose .py files beside the exe so
it can be updated by replacing files (Option A), the same way the Linux source
install updates via git. The bundle excludes `autoclip`, and this bootstrap puts the
install dir on sys.path so the loose copy is the one that gets imported.

RUNTIME_VERSION identifies the frozen runtime. Bump it ONLY when the bundled runtime
or its dependencies change — the updater refuses a loose-code update that needs a
newer runtime than the one installed, falling back to a full installer instead.
"""
import os
import sys
import logging
import tempfile

RUNTIME_VERSION = 1


def _install_dir() -> str:
    # Frozen: the loose autoclip/ package sits next to AutoClip.exe.
    # Dev (not frozen): this file's own dir is the repo root.
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _log_path() -> str:
    base = os.path.join(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()), "autoclip")
    try:
        os.makedirs(base, exist_ok=True)
    except OSError:
        base = tempfile.gettempdir()
    return os.path.join(base, "autoclip.log")


# Make the loose autoclip/ package importable (it is NOT in the frozen bundle).
_root = _install_dir()
if _root not in sys.path:
    sys.path.insert(0, _root)

# Expose the runtime version to the (loose) updater so it can gate code-only updates.
os.environ["AUTOCLIP_RUNTIME_VERSION"] = str(RUNTIME_VERSION)

logging.basicConfig(
    level=logging.INFO,
    filename=_log_path(),
    filemode="a",
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

if __name__ == "__main__":
    try:
        from autoclip.gui.main_window import run_app
        run_app()
    except BaseException:
        import traceback
        logging.getLogger("autoclip").critical(
            "Fatal startup error:\n%s", traceback.format_exc()
        )
        raise
