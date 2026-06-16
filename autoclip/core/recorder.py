# SPDX-License-Identifier: GPL-3.0-or-later
import sys

if sys.platform == "win32":
    from autoclip.core.recorder_windows import *
else:
    from autoclip.core.recorder_linux import *
