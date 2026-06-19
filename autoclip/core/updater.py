# SPDX-License-Identifier: GPL-3.0-or-later
"""Self-update for AutoClip — cross-platform.

The CHECK is system-agnostic: query the GitHub Releases of SmoJa/autoclip and compare
the latest tag to the running version. Only the APPLY step differs per platform:
  - Windows: download the installer .exe asset and run it (per-user, no UAC).
  - Linux:   `git pull` the source install (+ pip deps) — mirrors install.sh.

All network/process work is best-effort and never raises into the caller.
"""
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional, Tuple, Callable

from .. import __version__

logger = logging.getLogger(__name__)

GITHUB_REPO = "SmoJa/autoclip"
_RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
_HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": "AutoClip-Updater"}


def is_frozen() -> bool:
    """True when running as the packaged Windows AutoClip.exe (PyInstaller)."""
    return bool(getattr(sys, "frozen", False))


def _git_install_dir() -> Optional[Path]:
    """The git working tree the running code lives in (Linux source install), or None."""
    root = Path(__file__).resolve().parents[2]   # <repo>/autoclip/core/updater.py -> <repo>
    return root if (root / ".git").exists() else None


def can_self_update() -> bool:
    """Whether this install can apply an update in place."""
    if sys.platform == "win32":
        return is_frozen()
    return _git_install_dir() is not None


def _installed_runtime() -> int:
    """The frozen runtime version, stamped into the environment by the bootstrap.
    0 when not frozen (dev / Linux source), which the Windows path never relies on."""
    try:
        return int(os.environ.get("AUTOCLIP_RUNTIME_VERSION", "0"))
    except ValueError:
        return 0


def _fetch_latest_release(timeout: int = 8) -> Optional[dict]:
    try:
        req = urllib.request.Request(_RELEASES_API, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except Exception as e:
        logger.info(f"Release fetch failed: {e}")
        return None


def _find_source_asset(data: dict) -> Tuple[Optional[str], int]:
    """Find the loose-source asset `autoclip-src-<ver>[-rt<N>].zip` in a release.
    Returns (download_url, min_runtime). min_runtime defaults to 0 when the asset
    name carries no `-rt<N>` tag (i.e. compatible with any runtime)."""
    for a in data.get("assets", []):
        name = a.get("name", "").lower()
        if name.startswith("autoclip-src") and name.endswith(".zip"):
            m = re.search(r"-rt(\d+)\.zip$", name)
            return a.get("browser_download_url"), (int(m.group(1)) if m else 0)
    return None, 0


# ── shared check ────────────────────────────────────────────────────────────
def _parse_version(v: str) -> tuple:
    v = (v or "").lstrip("vV").split("-")[0].split("+")[0]
    out = []
    for part in v.split("."):
        try:
            out.append(int(part))
        except ValueError:
            out.append(0)
    return tuple(out) or (0,)


def check_for_update(timeout: int = 8) -> Optional[Tuple[str, Optional[str], str]]:
    """Return (latest_tag, win_installer_url, release_page_url) when a newer release
    exists, else None. win_installer_url is the Windows installer asset (None if absent
    or on Linux — Linux updates from git, not an asset)."""
    data = _fetch_latest_release(timeout)
    if data is None:
        return None

    tag = data.get("tag_name", "")
    if not tag or _parse_version(tag) <= _parse_version(__version__):
        return None

    page = data.get("html_url", f"https://github.com/{GITHUB_REPO}/releases/latest")
    win_url = None
    if sys.platform == "win32":
        assets = data.get("assets", [])
        for a in assets:
            name = a.get("name", "").lower()
            if name.endswith(".exe") and ("setup" in name or "install" in name):
                win_url = a.get("browser_download_url")
                break
        if not win_url:
            for a in assets:
                if a.get("name", "").lower().endswith(".exe"):
                    win_url = a.get("browser_download_url")
                    break
    logger.info(f"Update available: {__version__} -> {tag}")
    return (tag, win_url, page)


# ── platform apply ──────────────────────────────────────────────────────────
def apply_update(win_installer_url: Optional[str],
                 on_progress: Optional[Callable[[int], None]] = None) -> str:
    """Apply the update for the current platform. Returns a status string:
      'installing'      Windows installer launched — caller should quit now
      'updated-restart' Linux git pull done — caller should restart to apply
      'failed'          could not apply (caller should fall back to opening the page)
    """
    if sys.platform == "win32":
        return _apply_windows(win_installer_url, on_progress)
    return _apply_linux()


def _apply_windows(url: Optional[str], on_progress) -> str:
    # Prefer a loose-code update (replace autoclip/*.py in place — no installer) when the
    # release carries a source asset AND the installed runtime is new enough for it.
    data = _fetch_latest_release()
    if data is not None:
        src_url, min_runtime = _find_source_asset(data)
        if src_url and _installed_runtime() >= min_runtime:
            if _apply_windows_code_update(src_url, on_progress):
                return "updated-restart"
            logger.info("Code update failed; falling back to the full installer.")
        elif src_url:
            logger.info(f"Source update needs runtime >= {min_runtime} "
                        f"(installed {_installed_runtime()}); using full installer.")

    if not url:
        return "failed"
    dest = os.path.join(tempfile.gettempdir(), "AutoClip-Setup-update.exe")
    try:
        def _hook(block, size, total):
            if on_progress and total > 0:
                on_progress(min(100, int(block * size * 100 / total)))
        urllib.request.urlretrieve(url, dest, _hook if on_progress else None)
        subprocess.Popen([dest, "/SILENT", "/CLOSEAPPLICATIONS",
                          "/RESTARTAPPLICATIONS", "/NOICONS"])
        return "installing"
    except Exception as e:
        logger.error(f"Windows update failed: {e}")
        return "failed"


def _apply_windows_code_update(url: str, on_progress) -> bool:
    """Apply a light update by OVERLAYING the payload tree onto the install folder.

    The payload zip mirrors the install layout (autoclip/ + the loose obs-runtime
    helper scripts), so this is fully generic: a future update that adds a new loose
    file just includes it in the tree — no change needed here. Loose files aren't
    locked while the app runs, so the overlay is safe and takes effect on restart.
    Locked files (the frozen exe, obs-runtime binaries) are never in the payload —
    those only change via the full installer (gated by RUNTIME_VERSION)."""
    import os
    install_dir = Path(__file__).resolve().parents[2]   # <app>/autoclip/core/updater.py -> <app>
    if not (install_dir / "autoclip").is_dir():
        logger.error("Loose autoclip/ not found beside the app — can't code-update.")
        return False
    tmp = Path(tempfile.mkdtemp(prefix="autoclip-upd-"))
    try:
        zpath = tmp / "update.zip"
        def _hook(block, size, total):
            if on_progress and total > 0:
                on_progress(min(100, int(block * size * 100 / total)))
        urllib.request.urlretrieve(url, str(zpath), _hook if on_progress else None)
        payload = tmp / "payload"
        with zipfile.ZipFile(zpath) as zf:
            zf.extractall(payload)
        # A valid payload always carries the app package at its mirrored path.
        if not (payload / "autoclip").is_dir():
            logger.error("Update payload has no autoclip/ — aborting.")
            return False
        # Overlay every payload file onto the install dir at its mirrored location.
        count = 0
        for root, _dirs, files in os.walk(payload):
            rel = Path(root).relative_to(payload)
            for f in files:
                dest = install_dir / rel / f
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(Path(root) / f, dest)
                count += 1
        # Drop stale bytecode so the new .py recompile cleanly on restart.
        for pyc in (install_dir / "autoclip").rglob("__pycache__"):
            shutil.rmtree(pyc, ignore_errors=True)
        logger.info(f"Light update applied — overlaid {count} file(s) into {install_dir}")
        return True
    except Exception as e:
        logger.error(f"Code update failed: {e}")
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _apply_linux() -> str:
    install_dir = _git_install_dir()
    if not install_dir:
        return "failed"
    try:
        subprocess.run(["git", "-C", str(install_dir), "pull", "--ff-only"],
                       check=True, capture_output=True, text=True, timeout=120)
        req = install_dir / "autoclip" / "requirements.txt"
        if req.exists():
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", str(req)],
                           check=False, timeout=300)
        return "updated-restart"
    except Exception as e:
        logger.error(f"Linux git update failed: {e}")
        return "failed"
