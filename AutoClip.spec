# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the AutoClip GUI. Onedir build -> dist/AutoClip/AutoClip.exe.
#
# Option A packaging: this bundle is the STABLE RUNTIME only (Python + PyQt6 + mpv +
# onnxruntime + …). The `autoclip` package itself is deliberately EXCLUDED from the
# bundle and shipped as loose .py files beside the exe (see AutoClip.iss), so it can be
# updated by replacing files instead of rebuilding the installer. autoclip_app.py puts
# the install dir on sys.path so the loose copy is imported. Because autoclip is excluded
# from analysis, every third-party dependency it uses must be listed in hiddenimports.
# The obs-runtime recorder bundle is shipped separately by the installer; libmpv-2.dll is
# bundled below.
import os
from PyInstaller.utils.hooks import collect_data_files, collect_all

ICON = os.path.join('autoclip', 'gui', 'autoclip.ico')

# autoclip's own data (themes, .ico) now ships loose with the package, so only
# third-party data (onnxruntime) needs bundling here.
datas = collect_data_files('onnxruntime')

# vosk (Phrases speech trigger): bundle the package + its native libs (libvosk).
# collect_all pulls datas, binaries, and submodules.
_vosk_datas, _vosk_binaries, _vosk_hiddenimports = collect_all('vosk')
datas += _vosk_datas

# libmpv for in-app playback (bundled so users don't need mpv.net installed)
binaries = list(_vosk_binaries)
_mpv = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs', 'mpv.net', 'libmpv-2.dll')
if os.path.exists(_mpv):
    binaries.append((_mpv, '.'))

# Every third-party package the loose autoclip code imports (anywhere). Enumerated
# explicitly because `autoclip` is excluded from analysis below.
hiddenimports = [
    'sounddevice', 'mpv', 'onnxruntime', 'numpy', 'send2trash', 'pynput', 'vosk',
    'PyQt6.QtCore', 'PyQt6.QtGui', 'PyQt6.QtWidgets', 'PyQt6.QtNetwork',
] + _vosk_hiddenimports

a = Analysis(
    ['autoclip_app.py'],
    pathex=['.'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    # Exclude autoclip so the LOOSE copy (on sys.path) is imported, not a frozen one.
    excludes=['autoclip', 'tkinter', 'PyQt5', 'PySide6', 'matplotlib'],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='AutoClip',
    console=False,
    icon=ICON,
)
coll = COLLECT(exe, a.binaries, a.datas, name='AutoClip')
