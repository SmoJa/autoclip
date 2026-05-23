#!/usr/bin/env bash
# Run AutoClip in XWayland mode with terminal for debug output
cd "$(dirname "$0")/.."
QT_QPA_PLATFORM=xcb python3 -m autoclip.main "$@" 2>&1 | tee /tmp/autoclip.log
