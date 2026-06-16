# SPDX-License-Identifier: GPL-3.0-or-later
import sys

if sys.platform == "win32":
    from autoclip.core.audio_windows import *
else:
    from autoclip.core.audio_linux import *
