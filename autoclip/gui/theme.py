# SPDX-License-Identifier: GPL-3.0-or-later
"""
Theme system for AutoClip.

Built-in themes live in gui/themes/*.json.
User themes live in ~/.config/autoclip/themes/*.json.
User themes with the same stem as a built-in override it.

Usage:
    from autoclip.gui import theme
    theme.load("dark_orange")          # call once at startup
    color = theme.current.accent       # reference anywhere
"""
import json
import logging
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)

_BUILTIN_DIR  = Path(__file__).parent / "themes"
from autoclip.core.config import CONFIG_DIR as _CONFIG_DIR
_USER_DIR     = _CONFIG_DIR / "themes"
DEFAULT_ID    = "dark_orange"


@dataclass
class Theme:
    # Metadata
    name:        str = "Dark Orange"
    author:      str = "AutoClip"
    description: str = ""

    # Backgrounds
    bg_deep:    str = "#0e0f11"
    bg_base:    str = "#16181b"
    bg_raised:  str = "#1e2024"
    bg_overlay: str = "#252830"

    # Borders / dividers
    border: str = "#2a2b2e"

    # Text
    text:       str = "#d4d4d4"
    text_dim:   str = "#888888"
    text_faint: str = "#555555"

    # Accent
    accent:       str = "#ff4c00"
    accent_hover: str = "#ff6a24"
    accent_text:  str = "#ffffff"

    # Semantic
    success: str = "#00cc66"
    warning: str = "#ff8c00"
    error:   str = "#cc3300"

    # Miscellaneous UI details
    hdr_badge:     str = "#cc88ff"
    event_clip_bg: str = "#1a0500"

    # QPainter / canvas
    waveform:   str = "#ff4c00"
    playhead:   str = "#ffffff"
    handle_in:  str = "#00cc66"
    handle_out: str = "#ff4c00"

    # Track type colours (waveform strips, audio track badges)
    track_game:   str = "#ff4c00"
    track_mic:    str = "#00cc66"
    track_chat:   str = "#4488ff"
    track_custom: str = "#aa88ff"

    def track_colors(self) -> Dict[str, str]:
        return {
            "game":   self.track_game,
            "mic":    self.track_mic,
            "chat":   self.track_chat,
            "custom": self.track_custom,
        }


# Module-level current theme — set once at startup before any widgets are created.
current: Theme = Theme()


# ── Loading ───────────────────────────────────────────────────────────────────

def _field_names() -> set:
    return {f.name for f in fields(Theme)}


def load_from_path(path: Path) -> Theme:
    """Parse a theme JSON file and return a Theme instance."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    known = _field_names()
    color_overrides = {k: v for k, v in data.get("colors", {}).items() if k in known}

    return Theme(
        name        = data.get("name",        path.stem.replace("_", " ").title()),
        author      = data.get("author",      ""),
        description = data.get("description", ""),
        **color_overrides,
    )


def discover() -> Dict[str, Path]:
    """
    Return {theme_id: path} for all available themes.
    User themes (~/.config/autoclip/themes/) override built-ins with the same stem.
    """
    themes: Dict[str, Path] = {}
    for p in sorted(_BUILTIN_DIR.glob("*.json")):
        themes[p.stem] = p
    if _USER_DIR.exists():
        for p in sorted(_USER_DIR.glob("*.json")):
            themes[p.stem] = p   # user theme wins
    return themes


def load(theme_id: str) -> Theme:
    """
    Load a theme by ID (JSON file stem) and set it as current.
    Falls back to the default dark_orange if the ID is not found.
    Returns the loaded Theme.
    """
    global current
    all_themes = discover()
    path = all_themes.get(theme_id) or all_themes.get(DEFAULT_ID)
    if path is None:
        logger.warning(f"Theme {theme_id!r} not found, using built-in defaults")
        current = Theme()
        return current
    try:
        current = load_from_path(path)
        logger.info(f"Loaded theme {theme_id!r} from {path}")
    except Exception as e:
        logger.warning(f"Failed to load theme {path}: {e} — using defaults")
        current = Theme()
    return current


# ── Stylesheet builder ────────────────────────────────────────────────────────

def build_stylesheet(t: Theme) -> str:
    """Generate the application-wide Qt stylesheet from a theme."""
    return f"""
QMainWindow, QWidget {{
    background-color: {t.bg_deep};
    color: {t.text};
    font-family: "JetBrains Mono", "Fira Code", "Courier New", monospace;
    font-size: 13px;
}}
QTabWidget::pane {{
    border: 1px solid {t.border};
    border-top: none;
    background: {t.bg_deep};
}}
QTabBar::tab {{
    background: {t.bg_base};
    color: {t.text_dim};
    padding: 10px 24px;
    border: none;
    border-bottom: 2px solid transparent;
    font-size: 11px;
    letter-spacing: 1.5px;
}}
QTabBar::tab:selected {{
    color: {t.text};
    border-bottom: 2px solid {t.accent};
    background: {t.bg_deep};
}}
QTabBar::tab:hover:!selected {{ color: {t.text_dim}; background: {t.bg_overlay}; }}
QPushButton {{
    background-color: {t.bg_raised};
    color: {t.text};
    border: 1px solid {t.border};
    padding: 8px 20px;
    font-family: "JetBrains Mono", monospace;
    font-size: 12px;
    letter-spacing: 1px;
}}
QPushButton:hover {{ background-color: {t.bg_overlay}; border-color: {t.accent}; color: {t.accent_text}; }}
QPushButton:pressed {{ background-color: {t.accent}; color: {t.accent_text}; }}
QPushButton#primary {{ background-color: {t.accent}; color: {t.accent_text}; border: none; font-weight: bold; }}
QPushButton#primary:hover {{ background-color: {t.accent_hover}; }}
QPushButton#off {{ background-color: {t.bg_raised}; color: {t.text_dim}; border: 1px solid {t.border}; }}
QCheckBox {{ spacing: 10px; color: {t.text}; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border: 1px solid {t.border}; background: {t.bg_base}; }}
QCheckBox::indicator:checked {{ background: {t.accent}; border-color: {t.accent}; }}
QRadioButton {{ color: {t.text}; spacing: 8px; }}
QRadioButton::indicator {{ width: 14px; height: 14px; border: 1px solid {t.border}; border-radius: 7px; background: {t.bg_base}; }}
QRadioButton::indicator:checked {{ background: {t.accent}; border-color: {t.accent}; }}
QSpinBox, QDoubleSpinBox, QLineEdit, QComboBox {{
    background: {t.bg_base}; border: 1px solid {t.border};
    color: {t.text}; padding: 6px 10px;
    font-family: "JetBrains Mono", monospace;
}}
QSpinBox:focus, QLineEdit:focus, QComboBox:focus {{ border-color: {t.accent}; }}
QComboBox::drop-down {{ border: none; padding-right: 8px; }}
QSlider::groove:horizontal {{ background: {t.border}; height: 4px; }}
QSlider::handle:horizontal {{ background: {t.accent}; width: 14px; height: 14px; margin: -5px 0; }}
QSlider::sub-page:horizontal {{ background: {t.accent}; }}
QGroupBox {{
    border: 1px solid {t.border}; margin-top: 16px; padding-top: 12px;
    color: {t.text_dim}; font-size: 11px; letter-spacing: 2px;
}}
QGroupBox::title {{
    subcontrol-origin: margin; subcontrol-position: top left;
    left: 12px; top: -1px; background: {t.bg_deep}; padding: 0 6px;
}}
QScrollArea {{ border: none; }}
QFrame#sep {{ background: {t.border}; max-height: 1px; }}
"""
