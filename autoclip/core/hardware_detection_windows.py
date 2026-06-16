# SPDX-License-Identifier: GPL-3.0-or-later
"""
System capability detection for AutoClip — Windows.
GPU vendor detected via wmic. HDR detection via PowerShell/WMI.
"""
import subprocess
import logging

logger = logging.getLogger(__name__)


def detect_hdr() -> bool:
    """
    Detect if any connected display has HDR enabled on Windows.
    Queries WMI via PowerShell for displays with HDR active.
    """
    try:
        from autoclip.core.clips import _run_capture
        stdout, _, rc = _run_capture(
            [
                "powershell", "-NoProfile", "-NonInteractive", "-Command",
                "Get-WmiObject -Namespace root/wmi -Class WmiMonitorColorimetry "
                "| Select-Object -ExpandProperty Active 2>$null",
            ],
            timeout=5,
        )
        if rc == 0 and "true" in stdout.lower():
            logger.info("HDR detected via WMI")
            return True
    except Exception:
        pass

    logger.info("HDR not detected — using SDR codec")
    return False


def detect_gpu_vendor() -> str:
    """Returns 'nvidia', 'amd', 'intel', or 'unknown'.
    Uses Get-CimInstance (wmic was removed in Windows 11 22H2+)."""
    try:
        from autoclip.core.clips import _run_capture
        stdout, _, rc = _run_capture(
            [
                "powershell", "-NoProfile", "-NonInteractive", "-Command",
                "Get-CimInstance -ClassName Win32_VideoController "
                "| Select-Object -ExpandProperty Name",
            ],
            timeout=5,
        )
        out = stdout.lower()
        if "nvidia" in out:
            return "nvidia"
        if "amd" in out or "radeon" in out or "advanced micro" in out:
            return "amd"
        if "intel" in out:
            return "intel"
    except Exception:
        pass
    return "unknown"


def detect_best_codec() -> str:
    """
    Choose the best recording codec based on GPU and HDR status.
    NVIDIA + HDR → hevc_hdr
    NVIDIA no HDR → hevc
    AMD/Intel → hevc
    Fallback → h264
    """
    hdr    = detect_hdr()
    vendor = detect_gpu_vendor()
    logger.info(f"System detection: GPU={vendor} HDR={hdr}")

    if hdr:
        return "hevc_hdr"
    return "hevc"
