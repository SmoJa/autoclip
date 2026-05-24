#!/usr/bin/env bash
# AutoClip installer
set -e

INSTALL_DIR="$HOME/.local/share/autoclip"
DESKTOP_DIR="$HOME/.local/share/applications"

echo "Installing AutoClip to $INSTALL_DIR..."

# Clone or update
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "Existing installation found — updating..."
    git -C "$INSTALL_DIR" pull
else
    git clone https://github.com/SmoJa/autoclip.git "$INSTALL_DIR"
fi

# Python dependencies
echo "Installing Python dependencies..."
pip install -r "$INSTALL_DIR/autoclip/requirements.txt"

# Make scripts executable
chmod +x "$INSTALL_DIR/autoclip/run.sh"
chmod +x "$INSTALL_DIR/autoclip/run_debug.sh"

# Desktop launcher
mkdir -p "$DESKTOP_DIR"
cp "$INSTALL_DIR/autoclip/autoclip.desktop" "$DESKTOP_DIR/autoclip.desktop"

echo ""
echo "Done. Launch AutoClip from your application menu, or run:"
echo "  $INSTALL_DIR/autoclip/run.sh"
