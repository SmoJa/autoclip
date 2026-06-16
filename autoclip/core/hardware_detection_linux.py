# SPDX-License-Identifier: GPL-3.0-or-later
"""
System capability detection for AutoClip.
Detects GPU, HDR status, and selects appropriate recording codec.
"""
import subprocess
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def detect_hdr() -> bool:
    """
    Detect if any connected display has HDR enabled.
    Checks multiple sources: DRM sysfs, KWin config, xrandr.
    Returns True if HDR is active on any display.
    """
    # Method 1: DRM HDR output metadata (most reliable)
    drm_path = Path("/sys/class/drm")
    if drm_path.exists():
        for connector in drm_path.glob("*/hdr_output_metadata"):
            try:
                data = connector.read_bytes()
                if any(b != 0 for b in data):
                    logger.info(f"HDR detected via DRM: {connector.parent.name}")
                    return True
            except Exception:
                pass

    # Method 2: KWin HDR config (KDE Plasma 6)
    try:
        r = subprocess.run(
            ["kreadconfig6", "--file", "kwinrc",
             "--group", "HDR", "--key", "EnableHDR"],
            capture_output=True, text=True, timeout=3
        )
        if r.stdout.strip().lower() == "true":
            logger.info("HDR detected via KWin config")
            return True
    except Exception:
        pass

    # Method 3: xrandr verbose output
    try:
        r = subprocess.run(
            ["xrandr", "--verbose"],
            capture_output=True, text=True, timeout=5
        )
        lines = r.stdout.splitlines()
        for i, line in enumerate(lines):
            if "HDR" in line:
                # Check nearby lines for "on" state
                context = " ".join(lines[max(0,i-1):i+3])
                if " on" in context.lower():
                    logger.info("HDR detected via xrandr")
                    return True
    except Exception:
        pass

    logger.info("HDR not detected — using SDR codec")
    return False


def detect_gpu_vendor() -> str:
    """Returns 'nvidia', 'amd', 'intel', or 'unknown'."""
    try:
        r = subprocess.run(
            ["lspci"], capture_output=True, text=True, timeout=5
        )
        out = r.stdout.lower()
        if "nvidia" in out:
            return "nvidia"
        if "amd" in out or "radeon" in out or "advanced micro" in out:
            return "amd"
        if "intel" in out and ("vga" in out or "display" in out or "3d" in out):
            return "intel"
    except Exception:
        pass
    return "unknown"


def detect_best_codec() -> str:
    """
    Choose the best recording codec based on GPU and HDR status.
    NVIDIA + HDR → hevc_hdr
    NVIDIA no HDR → hevc
    AMD/Intel + HDR → hevc_hdr (VAAPI, may not work on all systems)
    AMD/Intel no HDR → hevc
    Fallback → h264
    """
    hdr    = detect_hdr()
    vendor = detect_gpu_vendor()
    logger.info(f"System detection: GPU={vendor} HDR={hdr}")

    if hdr:
        return "hevc_hdr"
    else:
        return "hevc"
