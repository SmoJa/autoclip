#!/usr/bin/env bash
# Run AutoClip in a Konsole window showing live debug output
cd "$(dirname "$0")/.."
konsole --noclose -e bash -c "QT_QPA_PLATFORM=xcb python3 -m autoclip.main 2>&1 | tee /tmp/autoclip.log; echo '--- AutoClip exited ---'; read" &
