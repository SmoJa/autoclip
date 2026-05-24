#!/usr/bin/env bash
# Launch AutoClip. Logs are written to /tmp/autoclip.log.
# For live debug output run: tail -f /tmp/autoclip.log
cd "$(dirname "$0")/.."
QT_QPA_PLATFORM=xcb python3 -m autoclip.main "$@" >> /tmp/autoclip.log 2>&1
