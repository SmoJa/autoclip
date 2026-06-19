# SPDX-License-Identifier: GPL-3.0-or-later
import sys
import logging
from pathlib import Path
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QTabWidget, QCheckBox, QSpinBox,
    QLineEdit, QComboBox, QFileDialog, QFrame, QScrollArea, QSlider,
    QSystemTrayIcon, QMenu, QMessageBox, QGroupBox, QGridLayout,
    QSizePolicy,
)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer
from PyQt6.QtGui import QAction
from .widgets import NoScrollSpinBox, NoScrollComboBox, NoScrollDoubleSpinBox
from .audio_tracks import AudioTrackManager
from . import theme as _theme

from ..core.config import Config
from ..core.controller import AppController
from ..core.recorder import get_monitors, get_audio_sources, is_hdr_codec
from .clips_tab import ClipsTab

logger = logging.getLogger(__name__)


def app_icon():
    """The AutoClip app icon (orange circle + 'AC'). Loads the bundled .ico; falls
    back to drawing the accent circle if the file is missing."""
    from PyQt6.QtGui import QIcon
    ico = Path(__file__).parent / "autoclip.ico"
    if ico.exists():
        return QIcon(str(ico))
    from PyQt6.QtGui import QPixmap, QColor, QPainter
    pm = QPixmap(32, 32)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    c = QColor(_theme.current.accent)
    p.setBrush(c)
    p.setPen(c)
    p.drawEllipse(4, 4, 24, 24)
    p.end()
    return QIcon(pm)



class StatusBar(QWidget):
    def __init__(self):
        super().__init__()
        t = _theme.current
        self.setStyleSheet(
            f"background: {t.bg_base}; border-top: 1px solid {t.border};")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 10, 20, 10)
        self.dot = QLabel("●")
        self.dot.setStyleSheet(f"color: {t.text_faint}; font-size: 16px;")
        self.label = QLabel("STOPPED")
        self.label.setStyleSheet(
            f"color: {t.text_faint}; letter-spacing: 2px; font-size: 11px;")
        self.game_label = QLabel("")
        self.game_label.setStyleSheet(f"color: {t.accent}; font-size: 11px;")
        layout.addWidget(self.dot)
        layout.addWidget(self.label)
        layout.addStretch()
        layout.addWidget(self.game_label)

    def set_status(self, status: str, game: str = ""):
        t = _theme.current
        c = {
            "running":   t.success,
            "recording": t.success,
            "paused":    t.warning,
            "stopped":   t.text_faint,
            "error":     t.error,
        }
        color = next((v for k, v in c.items() if k in status.lower()), t.text_faint)
        self.dot.setStyleSheet(f"color: {color}; font-size: 16px;")
        self.label.setText(status.upper())
        self.label.setStyleSheet(
            f"color: {color}; letter-spacing: 2px; font-size: 11px;")
        self.game_label.setText(f"▶  {game}" if game else "")


class EventLog(QWidget):
    def __init__(self):
        super().__init__()
        t = _theme.current
        self._trigger_style: dict = {}   # populated by set_trigger_styles()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setStyleSheet(
            f"QScrollArea{{border:none;background:{t.bg_deep};}}")
        self._inner = QWidget()
        self._inner.setStyleSheet(f"background:{t.bg_deep};")
        self._il = QVBoxLayout(self._inner)
        self._il.setContentsMargins(0, 0, 0, 0)
        self._il.setSpacing(0)
        self._il.addStretch()
        self._scroll.setWidget(self._inner)
        layout.addWidget(self._scroll)
        self._entries = []

    def set_trigger_styles(self, styles: dict):
        """Load trigger styles from plugin registry. styles: trigger → (display, color, bg, bold)"""
        self._trigger_style = styles

    def _add(self, text: str, color: str, bg: str, bold: bool):
        from PyQt6.QtCore import QDateTime
        t = _theme.current
        ts = QDateTime.currentDateTime().toString("HH:mm:ss")
        weight = "bold" if bold else "normal"
        lbl = QLabel(f"  {ts}  {text}")
        lbl.setStyleSheet(
            f"color:{color}; font-size:12px; font-weight:{weight};"
            f"padding:5px 8px; border-bottom:1px solid {t.bg_raised};"
            f"background:{bg};"
        )
        self._il.insertWidget(self._il.count() - 1, lbl)
        self._entries.append(lbl)
        if len(self._entries) > 200:
            self._entries.pop(0).deleteLater()
        QTimer.singleShot(50, lambda: self._scroll.verticalScrollBar().setValue(
            self._scroll.verticalScrollBar().maximum()))

    def add_system(self, text: str):
        """System events: game start/stop, recorder status."""
        self._add(text, _theme.current.text_faint, "transparent", False)

    def add_trigger(self, trigger: str, game: str = ""):
        """Game trigger event — look up style from plugin data."""
        if trigger in self._trigger_style:
            display, color, bg, bold = self._trigger_style[trigger]
        else:
            display = trigger.replace("_", " ").title()
            color, bg, bold = _theme.current.text_dim, "transparent", False
        prefix = f"[{game.upper()}]  " if game else ""
        self._add(f"{prefix}{display}", color, bg, bold)

    def add_clip_saved(self, reason: str):
        """Clip saved — distinct from triggers, always prominent."""
        t = _theme.current
        self._add(f"◆  CLIP SAVED  ·  {reason}", t.accent, t.event_clip_bg, True)



def _make_scrollable(widget: QWidget) -> QScrollArea:
    """Wrap a widget in a QScrollArea so it scrolls when the window is too small."""
    scroll = QScrollArea()
    scroll.setWidget(widget)
    scroll.setWidgetResizable(True)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
    return scroll


class CollapsibleSection(QWidget):
    """A titled collapsible container for game plugin settings."""
    toggled = pyqtSignal(bool)  # emitted after expand/collapse

    def __init__(self, title: str, widget: QWidget, expanded: bool = True,
                 header_widget: QWidget = None, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        t = _theme.current

        # Header row: toggle button (left) + optional widget (right)
        header_row = QWidget()
        header_row.setStyleSheet(
            f"QWidget {{ background: {t.bg_base}; border-bottom: 1px solid {t.border}; }}"
        )
        hl = QHBoxLayout(header_row)
        hl.setContentsMargins(0, 0, 12, 0)
        hl.setSpacing(0)

        self._toggle = QPushButton()
        self._toggle.setCheckable(True)
        self._toggle.setChecked(expanded)
        self._toggle.setStyleSheet(
            f"QPushButton {{"
            f"  background: transparent; border: none;"
            f"  color: {t.text}; font-size: 13px; font-weight: bold;"
            f"  padding: 10px 16px; text-align: left;"
            f"}}"
            f"QPushButton:hover {{ background: {t.bg_raised}; }}"
        )
        hl.addWidget(self._toggle, 1)

        if header_widget is not None:
            hl.addWidget(header_widget, 0)

        self._update_label(title, expanded)
        self._toggle.clicked.connect(lambda checked: self._on_toggle(title, checked))
        layout.addWidget(header_row)

        # Content
        self._content = widget
        self._content.setVisible(expanded)
        layout.addWidget(self._content, 1)
        self._title = title

    def _update_label(self, title: str, expanded: bool):
        arrow = "▾" if expanded else "▸"
        self._toggle.setText(f"  {arrow}  {title}")

    def _on_toggle(self, title: str, checked: bool):
        self._content.setVisible(checked)
        self._update_label(title, checked)
        self.updateGeometry()
        self.toggled.emit(checked)


class AutoRecordTab(QWidget):
    """
    Single tab hosting collapsible settings sections for each game plugin.
    New games appear automatically when their plugin file is added to games/.
    """

    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self.game_widgets: dict = {}
        self._sections: list = []

        from PyQt6.QtWidgets import QScrollArea as _QSA
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = _QSA()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(_QSA.Shape.NoFrame)
        outer.addWidget(scroll)

        content = QWidget()
        scroll.setWidget(content)
        self._layout = QVBoxLayout(content)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)

        # Intro label
        intro = QLabel(
            "Configure which in-game events automatically trigger clip saves. "
            "Each game can be expanded or collapsed."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {_theme.current.text_faint}; font-size: 11px; padding: 8px 24px;")
        self._layout.addWidget(intro)

        # One collapsible section per loaded plugin
        try:
            from autoclip.games.registry import get_all_plugins
            plugins = get_all_plugins()
        except Exception:
            plugins = []

        if not plugins:
            empty = QLabel("No game plugins found in games/ directory.")
            empty.setStyleSheet(f"color: {_theme.current.text_faint}; font-size: 12px; padding: 24px;")
            self._layout.addWidget(empty)
            self._layout.addStretch(1)
        else:
            for i, plugin_cls in enumerate(plugins):
                try:
                    tmp = plugin_cls.__new__(plugin_cls)
                    tmp.config   = config
                    tmp.on_event = lambda e: None
                    widget = tmp.get_config_widget(config)
                except Exception as e:
                    import logging as _log
                    _log.getLogger(__name__).warning(
                        f"Could not build config widget for {plugin_cls.NAME}: {e}")
                    widget = None

                if widget:
                    self.game_widgets[plugin_cls.NAME] = widget

                    enable_cb = None
                    if hasattr(widget, "_enabled_cb"):
                        from PyQt6.QtWidgets import QCheckBox as _QCB
                        enable_cb = _QCB("Enabled")
                        enable_cb.setChecked(widget._enabled_cb.isChecked())
                        enable_cb.toggled.connect(widget._enabled_cb.setChecked)
                        widget._enabled_cb.toggled.connect(enable_cb.setChecked)

                    section = CollapsibleSection(
                        plugin_cls.NAME, widget,
                        expanded=False,
                        header_widget=enable_cb
                    )
                    self._layout.addWidget(section)
                    self._sections.append(section)

            self._layout.addStretch(1)

    def _on_section_toggled(self, _expanded: bool):
        pass

    def _update_stretches(self):
        pass


class RecordingTab(QWidget):
    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self._controller = None  # set by MainWindow after construction
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        _inner = QWidget()
        layout = QVBoxLayout(_inner)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        t = _theme.current

        # Appearance
        appear_g = QGroupBox("Appearance")
        appear_l = QHBoxLayout(appear_g)
        appear_l.addWidget(QLabel("Theme"))
        self._theme_combo = NoScrollComboBox()
        all_themes = _theme.discover()
        for tid in sorted(all_themes):
            try:
                th = _theme.load_from_path(all_themes[tid])
                display = th.name
            except Exception:
                display = tid.replace("_", " ").title()
            self._theme_combo.addItem(display, tid)
            if tid == config.theme:
                self._theme_combo.setCurrentIndex(self._theme_combo.count() - 1)
        appear_l.addWidget(self._theme_combo)
        restart_note = QLabel("Takes effect after restart.")
        restart_note.setStyleSheet(f"color:{t.text_faint}; font-size:11px; margin-left:8px;")
        appear_l.addWidget(restart_note)
        appear_l.addStretch()
        layout.addWidget(appear_g)

        # Application settings
        app_g = QGroupBox("Application")
        app_l = QVBoxLayout(app_g)
        self.autostart_chk = QCheckBox("Start AutoClip automatically on login")
        self.autostart_chk.setChecked(self._is_autostart_enabled())
        import sys as _sys
        if _sys.platform == "win32":
            self.autostart_chk.setToolTip(
                "Creates a Windows startup registry entry so AutoClip launches when you log in."
            )
        else:
            self.autostart_chk.setToolTip(
                "Creates an autostart entry so AutoClip launches when you log in.\n"
                "Uses XDG autostart (~/.config/autostart/autoclip.desktop)."
            )
        self.autostart_chk.toggled.connect(self._on_autostart_toggled)
        app_l.addWidget(self.autostart_chk)

        self.start_minimized_chk = QCheckBox("Start minimized to system tray")
        self.start_minimized_chk.setChecked(config.start_minimized)
        self.start_minimized_chk.setToolTip(
            "Launch hidden in the system tray instead of showing the window.\n"
            "Useful with 'Start automatically on login' so AutoClip starts\n"
            "quietly on boot. Click the tray icon any time to open it."
        )
        self.start_minimized_chk.toggled.connect(self._mark_dirty)
        app_l.addWidget(self.start_minimized_chk)

        self.record_without_game_chk = QCheckBox("Record without a game (for audio triggers)")
        self.record_without_game_chk.setChecked(config.record_without_game)
        self.record_without_game_chk.setToolTip(
            "Keep the replay buffer running even when no game is detected.\n"
            "Clips are saved to a 'General' folder and triggered by audio\n"
            "plugins (Reactions, Voice) regardless of game state."
        )
        self.record_without_game_chk.toggled.connect(self._mark_dirty)
        app_l.addWidget(self.record_without_game_chk)
        layout.addWidget(app_g)

        # Updates — check + one-click install (no automatic/silent updates).
        # The widgets live here (Settings tab); the logic lives on MainWindow, reached
        # via the _main_window back-reference it sets after building this tab.
        from .. import __version__ as _ver
        upd_g = QGroupBox("Updates")
        upd_l = QVBoxLayout(upd_g)
        upd_row = QHBoxLayout()
        self._update_status_lbl = QLabel(f"AutoClip v{_ver}")
        self._update_status_lbl.setStyleSheet(f"color:{_theme.current.text_dim}; font-size:12px;")
        self._update_btn = QPushButton("Check for Updates")
        self._update_btn.setToolTip(
            "Check GitHub for a newer release. AutoClip never updates on its own —\n"
            "if one is found, this becomes an Install button you click when ready."
        )
        self._update_btn.clicked.connect(lambda: self._main_window._on_update_btn_clicked())
        upd_row.addWidget(self._update_status_lbl)
        upd_row.addStretch()
        upd_row.addWidget(self._update_btn)
        upd_l.addLayout(upd_row)
        layout.addWidget(upd_g)

        # Output directory
        dir_g = QGroupBox("Output Directory")
        dir_l = QHBoxLayout(dir_g)
        self.dir_edit = QLineEdit(config.output_dir)
        self.dir_btn = QPushButton("BROWSE")
        self.dir_btn.clicked.connect(self._browse)
        dir_l.addWidget(self.dir_edit)
        dir_l.addWidget(self.dir_btn)
        layout.addWidget(dir_g)

        exp_g = QGroupBox("Exports Directory")
        exp_l = QVBoxLayout(exp_g)
        exp_note = QLabel("Where exported clips (trimmed, SDR converted) are saved. Leave blank to use a subfolder inside the output directory.")
        exp_note.setStyleSheet(f"color:{t.text_dim};font-size:11px;")
        exp_note.setWordWrap(True)
        exp_l.addWidget(exp_note)
        exp_row = QHBoxLayout()
        self.exp_edit = QLineEdit(config.exports_dir)
        self.exp_edit.setPlaceholderText("Leave blank for auto (OutputDir/Exports/)")
        self.exp_btn = QPushButton("BROWSE")
        self.exp_btn.clicked.connect(self._browse_exports)
        exp_row.addWidget(self.exp_edit)
        exp_row.addWidget(self.exp_btn)
        exp_l.addLayout(exp_row)
        layout.addWidget(exp_g)

        # Recorder settings
        rec_g = QGroupBox("Recorder Settings")
        rec_l = QGridLayout(rec_g)
        rec_l.setSpacing(14)

        self._rec_path_label = QLabel("gpu-screen-recorder path")
        self.rec_path = QLineEdit(config.gpu_recorder_path)
        import sys as _sys
        _show_recorder_path = _sys.platform != "win32"
        self._rec_path_label.setVisible(_show_recorder_path)
        self.rec_path.setVisible(_show_recorder_path)
        rec_l.addWidget(self._rec_path_label, 0, 0)
        rec_l.addWidget(self.rec_path, 0, 1)

        _fps_label = QLabel("FPS")
        _fps_tip = ("Frames per second recorded. Match your game's framerate for\n"
                    "smooth clips. 60 is standard; higher uses more GPU and disk.")
        _fps_label.setToolTip(_fps_tip)
        rec_l.addWidget(_fps_label, 1, 0)
        self.fps = NoScrollSpinBox()
        self.fps.setRange(24, 240)
        self.fps.setValue(config.gpu_recorder_fps)
        self.fps.setToolTip(_fps_tip)
        rec_l.addWidget(self.fps, 1, 1)

        layout.addWidget(rec_g)

        # Encoding
        import sys as _sys
        self._is_win = _sys.platform == "win32"
        if self._is_win:
            self._build_encoding_windows(layout, config)
        else:
            self._build_encoding_linux(layout, config)

        # Monitor
        mon_g = QGroupBox("Display")
        mon_l = QHBoxLayout(mon_g)
        self.monitor_combo = NoScrollComboBox()
        self.monitor_combo.setMinimumWidth(260)
        self.refresh_monitors_btn = QPushButton("↻")
        self.refresh_monitors_btn.setFixedWidth(36)
        self.refresh_monitors_btn.setToolTip("Refresh monitor list")
        self.refresh_monitors_btn.clicked.connect(self._refresh_monitors)
        mon_l.addWidget(self.monitor_combo)
        mon_l.addWidget(self.refresh_monitors_btn)
        mon_l.addStretch()
        layout.addWidget(mon_g)
        self._refresh_monitors()

        # Audio tracks
        audio_g = QGroupBox("Audio Tracks")
        audio_gl = QVBoxLayout(audio_g)
        audio_gl.setContentsMargins(8, 12, 8, 8)

        # Audio track mode — three radio buttons
        from PyQt6.QtWidgets import QRadioButton, QButtonGroup
        mode = getattr(config, "audio_track_mode", "separate")

        self._audio_mode_group = QButtonGroup(self)

        self.radio_separate = QRadioButton("Separate tracks")
        self.radio_separate.setChecked(mode == "separate")
        self.radio_separate.setToolTip(
            "<b>Separate tracks</b><br>"
            "Each audio source (game, mic, chat) is saved as an independent stream "
            "in the clip file.<br><br>"
            "Best for: editing in DaVinci Resolve, Premiere etc. where you want "
            "full per-track volume control. Also required for AutoClip's own "
            "per-track waveform display and volume sliders.<br><br>"
            "Clips require export or editing before sharing — players like VLC "
            "and YouTube will only play one track at a time."
        )

        self.radio_mixed_now = QRadioButton("Mixed — process immediately")
        self.radio_mixed_now.setChecked(mode == "mixed_immediate")
        self.radio_mixed_now.setToolTip(
            "<b>Mixed (immediate)</b><br>"
            "All audio sources are mixed down to a single stream right after "
            "each clip is saved.<br><br>"
            "Best for: quickly sharing clips without editing — just grab the "
            "file and upload to YouTube, Discord, etc.<br><br>"
            "Processing is fast (video is not re-encoded, only audio is mixed) "
            "and runs in the background. Minimal impact on game performance on "
            "modern CPUs and NVMe drives."
        )

        self.radio_mixed_later = QRadioButton("Mixed — process when game closes")
        self.radio_mixed_later.setChecked(mode == "mixed_deferred")
        self.radio_mixed_later.setToolTip(
            "<b>Mixed (deferred)</b><br>"
            "All audio sources are mixed down to a single stream, but processing "
            "is queued until CS2 closes or a match ends.<br><br>"
            "Best for: users who want share-ready clips but prefer zero background "
            "activity while in-game. All queued clips are processed together "
            "when you exit the game."
        )

        for rb in (self.radio_separate, self.radio_mixed_now, self.radio_mixed_later):
            rb.setStyleSheet("QRadioButton { font-size: 11px; }")
            rb.toggled.connect(self._check_dirty)
            self._audio_mode_group.addButton(rb)
            audio_gl.addWidget(rb)

        self.audio_manager = AudioTrackManager(config)
        self.audio_manager.changed.connect(self._mark_dirty)
        audio_gl.addWidget(self.audio_manager)

        auto_btn = QPushButton("AUTO-DETECT TRACKS")
        auto_btn.setStyleSheet(
            f"background: {t.bg_raised}; color: {t.text}; border: 1px solid {t.border}; "
            f"padding: 6px 14px; font-size: 11px;"
        )
        auto_btn.setToolTip(
            "Automatically detect game, mic, and chat app audio sources.\n"
            "Launch CS2 and Discord first for best results."
        )
        auto_btn.clicked.connect(self._on_auto_detect)
        audio_gl.addWidget(auto_btn)
        layout.addWidget(audio_g)

        # Clip timing
        timing_g = QGroupBox("Clip Timing")
        tl = QGridLayout(timing_g)
        tl.setSpacing(14)

        pre_lbl = QLabel("Clip length  (seconds)")
        pre_lbl.setToolTip(
            "Total length of each saved clip in seconds.\n"
            "The clip starts this many seconds before the triggering event\n"
            "and ends after the trigger delay. For example, a 30s clip with\n"
            "a 7s trigger delay captures 23s before and 7s after the event."
        )
        tl.addWidget(pre_lbl, 0, 0)
        self.pre = NoScrollSpinBox()
        self.pre.setRange(5, 300)
        self.pre.setValue(config.clip_length_seconds)
        self.pre.setToolTip(pre_lbl.toolTip())
        self.pre.setSuffix("  s")
        tl.addWidget(self.pre, 0, 1)

        post_lbl = QLabel("Trigger delay  (seconds)")
        post_lbl.setToolTip(
            "How long to wait after a trigger fires before saving the clip.\n"
            "This captures the aftermath — the kill reaction, round end, etc.\n"
            "Must be less than the clip length. Also acts as the quiet window\n"
            "for event accumulation: if another trigger fires within this\n"
            "window, the timer resets and both events are saved in one clip."
        )
        tl.addWidget(post_lbl, 1, 0)
        self.post = NoScrollSpinBox()
        self.post.setRange(1, 60)
        self.post.setValue(config.post_event_seconds)
        self.post.setToolTip(post_lbl.toolTip())
        self.post.setSuffix("  s")
        tl.addWidget(self.post, 1, 1)

        # Preview label showing effective pre-event footage
        self._timing_info = QLabel("")
        self._timing_info.setStyleSheet(f"color: {t.text_dim}; font-size: 11px;")
        tl.addWidget(self._timing_info, 2, 0, 1, 2)

        # Wire up live validation
        self.pre.valueChanged.connect(self._validate_timing)
        self.post.valueChanged.connect(self._validate_timing)
        self._validate_timing()

        layout.addWidget(timing_g)

        # Hotkey
        hk_g = QGroupBox("Manual Save Hotkey")
        hk_l = QHBoxLayout(hk_g)
        self.hotkey = QLineEdit(config.manual_hotkey)
        self.hotkey.setPlaceholderText("e.g. <ctrl>+<shift>+s")
        hk_l.addWidget(self.hotkey)
        layout.addWidget(hk_g)

        self._dirty = False
        layout.addStretch()
        outer.addWidget(_make_scrollable(_inner))

        # Fixed footer — always visible outside the scroll area
        footer = QWidget()
        footer.setStyleSheet(
            f"background:{t.bg_base}; border-top: 1px solid {t.border};")
        fl = QHBoxLayout(footer)
        fl.setContentsMargins(24, 12, 24, 12)
        fl.setSpacing(16)

        self._dirty_lbl = QLabel("● Unsaved changes")
        self._dirty_lbl.setStyleSheet(f"color:{t.warning}; font-size:11px;")
        self._dirty_lbl.hide()
        fl.addWidget(self._dirty_lbl)

        self._restart_lbl = QLabel("· restart required for theme change")
        self._restart_lbl.setStyleSheet(f"color:{t.text_faint}; font-size:11px;")
        self._restart_lbl.hide()
        fl.addWidget(self._restart_lbl)

        fl.addStretch()

        self.save_btn = QPushButton("SAVE SETTINGS")
        self.save_btn.setObjectName("primary")
        self.save_btn.clicked.connect(self._save)
        fl.addWidget(self.save_btn)
        outer.addWidget(footer)

        # Wire all reversible fields to dirty checking (auto-clears if reverted).
        # Encoding fields differ per platform — each _build_encoding_* registers its
        # own signals in self._enc_dirty_signals.
        for _sig in [
            self._theme_combo.currentIndexChanged,
            self.dir_edit.textChanged,
            self.exp_edit.textChanged,
            self.rec_path.textChanged,
            self.fps.valueChanged,
            self.monitor_combo.currentTextChanged,
            self.pre.valueChanged,
            self.post.valueChanged,
            self.hotkey.textChanged,
        ] + getattr(self, "_enc_dirty_signals", []):
            _sig.connect(self._check_dirty)

    def _check_dirty(self, *_):
        c = self.config
        if self.radio_separate.isChecked():
            cur_mode = "separate"
        elif self.radio_mixed_now.isChecked():
            cur_mode = "mixed_immediate"
        else:
            cur_mode = "mixed_deferred"

        changed = (
            self._theme_combo.currentData()       != c.theme
            or self.dir_edit.text()               != c.output_dir
            or self.exp_edit.text().strip()       != c.exports_dir
            or self.rec_path.text()               != c.gpu_recorder_path
            or self.fps.value()                   != c.gpu_recorder_fps
            or self._encoding_changed(c)
            or self.monitor_combo.currentText()   != c.monitor
            or cur_mode                           != c.audio_track_mode
            or self.pre.value()                   != c.clip_length_seconds
            or self.post.value()                  != c.post_event_seconds
            or self.hotkey.text()                 != c.manual_hotkey
        )
        if changed:
            self._mark_dirty()
        else:
            self._mark_clean()

    def _on_auto_detect(self):
        game = ""
        display = ""
        if self._controller and self._controller._current_game:
            display = self._controller._current_game
            game = display
        self.audio_manager.auto_detect(game=game, game_display_name=display)

    def _mark_dirty(self, *_):
        self._dirty = True
        self._dirty_lbl.show()
        self._update_restart_note()

    def _mark_clean(self):
        self._dirty = False
        self._dirty_lbl.hide()
        self._restart_lbl.hide()

    def _update_restart_note(self, *_):
        if self._theme_combo.currentData() != self.config.theme:
            self._restart_lbl.show()
        else:
            self._restart_lbl.hide()

    def _refresh_monitors(self):
        monitors = get_monitors()
        current = self.config.monitor
        self.monitor_combo.clear()
        self.monitor_combo.addItems(monitors if monitors else ["screen"])
        if current in monitors:
            self.monitor_combo.setCurrentText(current)

    # ---- Encoding panels (platform-specific) -------------------------------

    # Tier-A named presets -> (rate_control, cq, bitrate_kbps, nvenc_preset, multipass).
    # Shared with the first-run hardware default in config.py.
    from ..core.config import ENCODING_PRESETS as _ENC_PRESETS

    @staticmethod
    def _set_combo_by_data(combo, data):
        i = combo.findData(data)
        combo.setCurrentIndex(i if i >= 0 else 0)

    def _build_encoding_linux(self, layout, config):
        enc_g = QGroupBox("Encoding")
        enc_l = QGridLayout(enc_g)
        enc_l.setSpacing(14)

        enc_l.addWidget(QLabel("Codec"), 0, 0)
        self.codec = NoScrollComboBox()
        self.codec.addItems(["hevc_hdr", "av1_hdr", "hevc_10bit", "av1_10bit", "hevc", "av1", "h264"])
        self.codec.setToolTip(
            "hevc_hdr / av1_hdr: best for HDR displays\n"
            "hevc / av1: SDR high quality\n"
            "h264: most compatible"
        )
        self.codec.setCurrentText(config.gpu_recorder_codec)
        enc_l.addWidget(self.codec, 0, 1)

        enc_l.addWidget(QLabel("Color range"), 1, 0)
        self.color_range = NoScrollComboBox()
        self.color_range.addItems(["full", "limited"])
        self.color_range.setCurrentText(config.gpu_recorder_color_range)
        enc_l.addWidget(self.color_range, 1, 1)

        enc_l.addWidget(QLabel("Bitrate mode"), 2, 0)
        self.quality_mode = NoScrollComboBox()
        self.quality_mode.addItems(["cbr", "vbr", "qp", "auto"])
        self.quality_mode.setCurrentText(config.gpu_recorder_quality_mode)
        self.quality_mode.setToolTip("cbr: recommended for replay buffer")
        self.quality_mode.currentTextChanged.connect(self._on_mode_changed)
        enc_l.addWidget(self.quality_mode, 2, 1)

        enc_l.addWidget(QLabel("Bitrate (kbps)"), 3, 0)
        self.bitrate = NoScrollSpinBox()
        self.bitrate.setRange(1000, 200000)
        self.bitrate.setSingleStep(1000)
        self.bitrate.setValue(config.gpu_recorder_bitrate_kbps)
        self.bitrate.setToolTip("Used for cbr/vbr. 30000 = 30 Mbps")
        enc_l.addWidget(self.bitrate, 3, 1)

        enc_l.addWidget(QLabel("Quality preset"), 4, 0)
        self.quality_preset = NoScrollComboBox()
        self.quality_preset.addItems(["medium", "high", "very_high", "ultra"])
        self.quality_preset.setCurrentText(config.gpu_recorder_quality_preset)
        self.quality_preset.setToolTip("Used for qp/auto mode")
        enc_l.addWidget(self.quality_preset, 4, 1)

        enc_l.addWidget(QLabel("Tune"), 5, 0)
        self.tune = NoScrollComboBox()
        self.tune.addItems(["quality", "performance"])
        self.tune.setCurrentText(config.gpu_recorder_tune)
        enc_l.addWidget(self.tune, 5, 1)

        layout.addWidget(enc_g)
        self._on_mode_changed(config.gpu_recorder_quality_mode)
        self._enc_dirty_signals = [
            self.codec.currentTextChanged, self.color_range.currentTextChanged,
            self.quality_mode.currentTextChanged, self.bitrate.valueChanged,
            self.quality_preset.currentTextChanged, self.tune.currentTextChanged,
        ]

    def _build_encoding_windows(self, layout, config):
        enc_g = QGroupBox("Encoding (NVENC)")
        g = QGridLayout(enc_g)
        g.setSpacing(14)
        self._enc_row = 0

        def add_row(text, widget, tip):
            """Add a label+widget row; apply the same tooltip to both and return the label."""
            lbl = QLabel(text)
            lbl.setToolTip(tip)
            widget.setToolTip(tip)
            g.addWidget(lbl, self._enc_row, 0)
            g.addWidget(widget, self._enc_row, 1)
            self._enc_row += 1
            return lbl

        self.enc_preset_combo = NoScrollComboBox()
        for lbl, data in [("Quality", "quality"), ("Balanced", "balanced"),
                          ("Performance", "performance"), ("Storage saver", "storage"),
                          ("Custom", "custom")]:
            self.enc_preset_combo.addItem(lbl, data)
        add_row("Preset", self.enc_preset_combo,
                "One-click starting points that fill in the settings below:\n"
                "• Quality — near-lossless, larger files (CQ 18, slow preset)\n"
                "• Balanced — great quality, reasonable size (recommended)\n"
                "• Performance — lowest GPU load (CBR, fast preset)\n"
                "• Storage saver — smaller files (higher CQ)\n"
                "Changing any setting below switches this to Custom.")

        self.codec = NoScrollComboBox()
        self.codec.addItems(["hevc", "h264", "av1"])
        self.codec.setCurrentText(config.gpu_recorder_codec or "hevc")
        add_row("Codec", self.codec,
                "Video compression format:\n"
                "• hevc (H.265) — best quality for the size (recommended)\n"
                "• h264 — largest files but plays everywhere\n"
                "• av1 — smallest files; needs RTX 40-series / RX 7000+")

        self.nvenc_rc = NoScrollComboBox()
        for lbl, data in [("CQP — constant quality", "cqp"),
                          ("CBR — constant bitrate", "cbr"),
                          ("VBR — variable bitrate", "vbr")]:
            self.nvenc_rc.addItem(lbl, data)
        add_row("Rate control", self.nvenc_rc,
                "How the encoder spends data:\n"
                "• CQP — holds a constant quality; file size varies. Best for clips.\n"
                "• CBR — holds a constant bitrate; predictable file size.\n"
                "• VBR — varies the bitrate up to a ceiling.")

        self.bitrate = NoScrollSpinBox()
        self.bitrate.setRange(1000, 200000); self.bitrate.setSingleStep(1000)
        self.bitrate.setValue(config.gpu_recorder_bitrate_kbps)
        self._lbl_bitrate = add_row("Bitrate (kbps)", self.bitrate,
                "Target data rate for CBR/VBR. Higher = better quality and bigger\n"
                "files. 30000 = 30 Mbps, a good 1440p/60 starting point.")

        self.nvenc_maxbitrate = NoScrollSpinBox()
        self.nvenc_maxbitrate.setRange(1000, 400000); self.nvenc_maxbitrate.setSingleStep(1000)
        self.nvenc_maxbitrate.setValue(config.nvenc_max_bitrate_kbps)
        self._lbl_maxbitrate = add_row("Max bitrate (kbps)", self.nvenc_maxbitrate,
                "VBR only: the ceiling the bitrate may spike to during busy,\n"
                "fast-moving scenes. Keep it above the target bitrate.")

        self.nvenc_cq = NoScrollSpinBox()
        self.nvenc_cq.setRange(0, 51)
        self.nvenc_cq.setValue(config.nvenc_cq_level)
        self._lbl_cq = add_row("Quality (CQ)", self.nvenc_cq,
                "CQP only: the quality target. Lower = better quality and bigger\n"
                "files. 18–24 is a good range; 22 is a solid default.")

        self.nvenc_preset_combo = NoScrollComboBox()
        for data, lbl in [("p1", "P1 — fastest"), ("p2", "P2"), ("p3", "P3"), ("p4", "P4"),
                          ("p5", "P5 — balanced"), ("p6", "P6"), ("p7", "P7 — best quality")]:
            self.nvenc_preset_combo.addItem(lbl, data)
        add_row("Encoder preset", self.nvenc_preset_combo,
                "How hard the GPU works per frame. Higher presets give better\n"
                "quality at the same size but use more GPU. P5 is a good balance.")

        self.nvenc_multipass_combo = NoScrollComboBox()
        for data, lbl in [("disabled", "Disabled"), ("qres", "Quarter resolution"),
                          ("fullres", "Full resolution")]:
            self.nvenc_multipass_combo.addItem(lbl, data)
        add_row("Multipass", self.nvenc_multipass_combo,
                "Analyses each frame twice to spend bitrate more wisely, reducing\n"
                "blocky artifacts in motion. Quarter-res is nearly free; full-res\n"
                "costs more GPU. Mainly helps CBR/VBR.")

        self.nvenc_profile_combo = NoScrollComboBox()
        for data, lbl in [("auto", "Auto"), ("main", "Main"), ("high", "High"),
                          ("main10", "Main 10 (HDR/10-bit)")]:
            self.nvenc_profile_combo.addItem(lbl, data)
        add_row("Profile", self.nvenc_profile_combo,
                "Encoder feature set. Auto is right for almost everyone.\n"
                "Main 10 enables 10-bit/HDR (HEVC/AV1) if your capture is HDR.")

        self.nvenc_bframes = NoScrollSpinBox()
        self.nvenc_bframes.setRange(0, 4)
        self.nvenc_bframes.setValue(config.nvenc_bframes)
        add_row("B-frames", self.nvenc_bframes,
                "Frames stored as the difference between neighbours — they shrink\n"
                "files at no real quality cost. 2 is a safe default; set 0 only if\n"
                "a player has trouble with the clips.")

        layout.addWidget(enc_g)

        # Load saved values (before wiring, so no spurious dirty/custom marking).
        self._set_combo_by_data(self.nvenc_rc, config.nvenc_rate_control)
        self._set_combo_by_data(self.nvenc_preset_combo, config.nvenc_preset)
        self._set_combo_by_data(self.nvenc_multipass_combo, config.nvenc_multipass)
        self._set_combo_by_data(self.nvenc_profile_combo, config.nvenc_profile)
        self._set_combo_by_data(self.enc_preset_combo, config.encoding_preset)
        self._update_nvenc_rc_visibility()

        self.enc_preset_combo.currentIndexChanged.connect(self._on_enc_preset_selected)
        self.nvenc_rc.currentIndexChanged.connect(self._on_nvenc_rc_changed)
        for w in (self.codec, self.nvenc_preset_combo, self.nvenc_multipass_combo,
                  self.nvenc_profile_combo):
            w.currentIndexChanged.connect(self._enc_knob_changed)
        for w in (self.bitrate, self.nvenc_maxbitrate, self.nvenc_cq, self.nvenc_bframes):
            w.valueChanged.connect(self._enc_knob_changed)

        self._enc_dirty_signals = [
            self.codec.currentIndexChanged, self.nvenc_rc.currentIndexChanged,
            self.bitrate.valueChanged, self.nvenc_maxbitrate.valueChanged,
            self.nvenc_cq.valueChanged, self.nvenc_preset_combo.currentIndexChanged,
            self.nvenc_multipass_combo.currentIndexChanged,
            self.nvenc_profile_combo.currentIndexChanged, self.nvenc_bframes.valueChanged,
            self.enc_preset_combo.currentIndexChanged,
        ]

    def _update_nvenc_rc_visibility(self):
        rc = self.nvenc_rc.currentData()
        cbr_vbr = rc in ("cbr", "vbr")
        self._lbl_bitrate.setVisible(cbr_vbr); self.bitrate.setVisible(cbr_vbr)
        self._lbl_maxbitrate.setVisible(rc == "vbr"); self.nvenc_maxbitrate.setVisible(rc == "vbr")
        self._lbl_cq.setVisible(rc == "cqp"); self.nvenc_cq.setVisible(rc == "cqp")

    def _on_nvenc_rc_changed(self, *_):
        self._update_nvenc_rc_visibility()
        self._enc_knob_changed()

    def _enc_knob_changed(self, *_):
        # A manual knob change means the named preset no longer matches -> Custom.
        if getattr(self, "_applying_preset", False):
            return
        combo = self.enc_preset_combo
        if combo.currentData() != "custom":
            combo.blockSignals(True)
            self._set_combo_by_data(combo, "custom")
            combo.blockSignals(False)

    def _on_enc_preset_selected(self, *_):
        name = self.enc_preset_combo.currentData()
        if name and name != "custom":
            self._apply_enc_preset(name)

    def _apply_enc_preset(self, name):
        spec = self._ENC_PRESETS.get(name)
        if not spec:
            return
        rc, cq, br, preset, multipass = spec
        self._applying_preset = True
        try:
            self._set_combo_by_data(self.nvenc_rc, rc)
            self.nvenc_cq.setValue(cq)
            self.bitrate.setValue(br)
            self._set_combo_by_data(self.nvenc_preset_combo, preset)
            self._set_combo_by_data(self.nvenc_multipass_combo, multipass)
            self._update_nvenc_rc_visibility()
        finally:
            self._applying_preset = False

    def _encoding_changed(self, c) -> bool:
        if self._is_win:
            return (self.codec.currentText()                 != c.gpu_recorder_codec
                    or self.nvenc_rc.currentData()           != c.nvenc_rate_control
                    or self.bitrate.value()                  != c.gpu_recorder_bitrate_kbps
                    or self.nvenc_maxbitrate.value()         != c.nvenc_max_bitrate_kbps
                    or self.nvenc_cq.value()                 != c.nvenc_cq_level
                    or self.nvenc_preset_combo.currentData() != c.nvenc_preset
                    or self.nvenc_multipass_combo.currentData() != c.nvenc_multipass
                    or self.nvenc_profile_combo.currentData() != c.nvenc_profile
                    or self.nvenc_bframes.value()            != c.nvenc_bframes
                    or self.enc_preset_combo.currentData()   != c.encoding_preset)
        return (self.codec.currentText()           != c.gpu_recorder_codec
                or self.color_range.currentText()  != c.gpu_recorder_color_range
                or self.quality_mode.currentText() != c.gpu_recorder_quality_mode
                or self.bitrate.value()            != c.gpu_recorder_bitrate_kbps
                or self.quality_preset.currentText() != c.gpu_recorder_quality_preset
                or self.tune.currentText()         != c.gpu_recorder_tune)

    def _encoding_save(self, c):
        c.gpu_recorder_codec = self.codec.currentText()
        c.gpu_recorder_bitrate_kbps = self.bitrate.value()
        if self._is_win:
            c.nvenc_rate_control = self.nvenc_rc.currentData()
            c.nvenc_max_bitrate_kbps = self.nvenc_maxbitrate.value()
            c.nvenc_cq_level = self.nvenc_cq.value()
            c.nvenc_preset = self.nvenc_preset_combo.currentData()
            c.nvenc_multipass = self.nvenc_multipass_combo.currentData()
            c.nvenc_profile = self.nvenc_profile_combo.currentData()
            c.nvenc_bframes = self.nvenc_bframes.value()
            c.encoding_preset = self.enc_preset_combo.currentData()
        else:
            c.gpu_recorder_color_range = self.color_range.currentText()
            c.gpu_recorder_quality_mode = self.quality_mode.currentText()
            c.gpu_recorder_quality_preset = self.quality_preset.currentText()
            c.gpu_recorder_tune = self.tune.currentText()

    def _on_mode_changed(self, mode: str):
        cbr_vbr = mode in ("cbr", "vbr")
        self.bitrate.setEnabled(cbr_vbr)
        self.quality_preset.setEnabled(not cbr_vbr)

    def _is_autostart_enabled(self) -> bool:
        import sys
        if sys.platform == "win32":
            try:
                import winreg
                key = winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Run",
                    0, winreg.KEY_READ,
                )
                winreg.QueryValueEx(key, "AutoClip")
                winreg.CloseKey(key)
                return True
            except OSError:
                return False
        from pathlib import Path
        return (Path.home() / ".config/autostart/autoclip.desktop").exists()

    def _on_autostart_toggled(self, enabled: bool):
        import sys
        import logging
        log = logging.getLogger(__name__)
        if sys.platform == "win32":
            try:
                import winreg, sys as _sys
                key = winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Run",
                    0, winreg.KEY_SET_VALUE,
                )
                if enabled:
                    exe = _sys.executable
                    winreg.SetValueEx(key, "AutoClip", 0, winreg.REG_SZ,
                                      f'"{exe}" -m autoclip.main')
                    log.info("Autostart enabled via registry")
                else:
                    try:
                        winreg.DeleteValue(key, "AutoClip")
                    except OSError:
                        pass
                    log.info("Autostart disabled via registry")
                winreg.CloseKey(key)
            except OSError as e:
                log.warning(f"Could not update autostart registry key: {e}")
            return

        from pathlib import Path
        autostart_dir  = Path.home() / ".config/autostart"
        desktop_path   = autostart_dir / "autoclip.desktop"
        install_dir    = Path.home() / ".local/share/autoclip"

        if enabled:
            autostart_dir.mkdir(parents=True, exist_ok=True)
            desktop_path.write_text(
                "[Desktop Entry]\n"
                "Type=Application\n"
                "Name=AutoClip\n"
                "Icon=autoclip\n"
                "Comment=Automatic game clip recorder\n"
                f"Exec=bash -c \"cd {install_dir} && QT_QPA_PLATFORM=xcb "
                f"python3 -m autoclip.main >> /tmp/autoclip.log 2>&1\"\n"
                "Hidden=false\n"
                "NoDisplay=false\n"
                "X-GNOME-Autostart-enabled=true\n"
                "X-KDE-autostart-after=panel\n"
            )
            log.info(f"Autostart enabled: {desktop_path}")
        else:
            if desktop_path.exists():
                desktop_path.unlink()
            log.info("Autostart disabled")

    def _validate_timing(self):
        clip_len = self.pre.value()
        delay    = self.post.value()

        # Clamp trigger delay to at most clip_length - 1
        if delay >= clip_len:
            self.post.blockSignals(True)
            self.post.setValue(clip_len - 1)
            self.post.blockSignals(False)
            delay = clip_len - 1

        # Update max for post spinner dynamically
        self.post.setMaximum(clip_len - 1)

        pre_footage = clip_len - delay
        self._timing_info.setText(
            f"Clip = {pre_footage}s before event  +  {delay}s after  =  {clip_len}s total"
        )

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Select Output Directory", self.config.output_dir)
        if d:
            self.dir_edit.setText(d)

    def _browse_exports(self):
        start = self.exp_edit.text() or self.config.output_dir
        d = QFileDialog.getExistingDirectory(self, "Select Exports Directory", start)
        if d:
            self.exp_edit.setText(d)

    def _save(self):
        self.config.theme = self._theme_combo.currentData()
        self.config.output_dir = self.dir_edit.text()
        self.config.exports_dir = self.exp_edit.text().strip()
        self.config.gpu_recorder_path = self.rec_path.text()
        self.config.gpu_recorder_fps = self.fps.value()
        self._encoding_save(self.config)
        self.config.monitor = self.monitor_combo.currentText()
        if self.radio_separate.isChecked():
            self.config.audio_track_mode = "separate"
        elif self.radio_mixed_now.isChecked():
            self.config.audio_track_mode = "mixed_immediate"
        else:
            self.config.audio_track_mode = "mixed_deferred"
        # audio_tracks saved directly by AudioTrackManager
        self.config.clip_length_seconds = self.pre.value()
        self.config.post_event_seconds = min(self.post.value(), self.pre.value() - 1)
        self.config.manual_hotkey = self.hotkey.text()
        self.config.record_without_game = self.record_without_game_chk.isChecked()
        self.config.start_minimized = self.start_minimized_chk.isChecked()
        self.config.save()
        self._mark_clean()
        # Apply the new settings to a live recording session (restarts the recorder,
        # which only reads config when spawned) so audio/encoding changes take effect.
        if self._controller:
            self._controller.reapply_recorder_settings()


class AudioTriggersTab(QWidget):
    """
    AUDIO TRIGGERS tab — one section per registered AudioTriggerPlugin.
    New plugins appear automatically; no code changes needed here.
    """
    def __init__(self, config: Config):
        super().__init__()
        self._controller = None
        self.config = config
        self._plugin_widgets = {}   # plugin_name → widget
        self._sections: list = []

        from PyQt6.QtWidgets import QScrollArea as _QSA
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = _QSA()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(_QSA.Shape.NoFrame)
        outer.addWidget(scroll)

        content_w = QWidget()
        scroll.setWidget(content_w)
        self._layout = QVBoxLayout(content_w)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        t = _theme.current

        intro = QLabel(
            "Configure audio-based clip triggers. "
            "Each trigger can be expanded or collapsed."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {t.text_faint}; font-size: 11px; padding: 8px 24px;")
        self._layout.addWidget(intro)

        from autoclip.audio_triggers.registry import get_all_plugins
        plugins = get_all_plugins()

        if not plugins:
            lbl = QLabel("No audio trigger plugins found.")
            lbl.setStyleSheet(f"color: {t.text_faint}; font-size: 12px; padding: 24px;")
            self._layout.addWidget(lbl)
            self._layout.addStretch(1)
        else:
            for cls in plugins:
                widget = cls.get_config_widget(cls, config)
                if widget is None:
                    continue

                content = QWidget()
                cl = QVBoxLayout(content)
                cl.setContentsMargins(16, 16, 16, 16)
                cl.setSpacing(12)
                cl.addWidget(widget)

                # Enable checkbox shown in the header even when collapsed
                from PyQt6.QtWidgets import QCheckBox as _QCB
                enable_cb = _QCB("Enabled")
                init_enabled = widget._enabled_cb.isChecked() if hasattr(widget, "_enabled_cb") else False
                enable_cb.setChecked(init_enabled)
                if hasattr(widget, "_enabled_cb"):
                    enable_cb.toggled.connect(widget._enabled_cb.setChecked)
                    widget._enabled_cb.toggled.connect(enable_cb.setChecked)

                section = CollapsibleSection(cls.NAME, content, expanded=False,
                                             header_widget=enable_cb)
                self._layout.addWidget(section)
                self._sections.append(section)
                self._plugin_widgets[cls.NAME] = widget

                plugin_name = cls.NAME
                if hasattr(widget, "settings_changed"):
                    widget.settings_changed.connect(
                        lambda n=plugin_name: self._save_plugin(n)
                    )

            self._layout.addStretch(1)

    def _save_plugin(self, name: str):
        widget = self._plugin_widgets.get(name)
        if widget and hasattr(widget, "save"):
            widget.save()
        if self._controller:
            self._controller.restart_audio_trigger(name)
            self._controller.on_event and self._controller.on_event(  # type: ignore
                f"Audio trigger '{name}' settings applied"
            )


class MainWindow(QMainWindow):
    _status_sig = pyqtSignal(str)
    _event_sig = pyqtSignal(str)
    _clip_sig = pyqtSignal(str)
    _update_sig = pyqtSignal(object, bool)   # (result, manual)
    _apply_sig = pyqtSignal(str, str)        # (apply status, release page url)

    def __init__(self, controller: AppController):
        super().__init__()
        self.controller = controller
        self.config = controller.config
        self._current_game = None
        self._clip_count = 0
        self._pending_update = None   # (tag, win_url, page) once a check finds one
        self.setWindowTitle("AutoClip")
        self.setMinimumSize(740, 580)
        if not self._restore_geometry():
            self.resize(1400, 860)
            screen = QApplication.primaryScreen().geometry()
            self.move(
                (screen.width()  - 1400) // 2,
                (screen.height() -  860) // 2,
            )
        self._build_ui()
        self._build_tray()
        self._wire()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        t = _theme.current
        hdr = QWidget()
        hdr.setStyleSheet(f"background: {t.bg_base}; border-bottom: 1px solid {t.border};")
        hl = QHBoxLayout(hdr)
        hl.setContentsMargins(24, 16, 24, 16)
        title = QLabel("AUTOCLIP")
        title.setStyleSheet(f"font-size: 22px; font-weight: bold; color: {t.text}; letter-spacing: 3px;")
        sub = QLabel("automatic clip recorder")
        sub.setStyleSheet(f"font-size: 11px; color: {t.text_faint}; letter-spacing: 2px;")
        sub.setAlignment(Qt.AlignmentFlag.AlignBottom)
        tc = QVBoxLayout()
        tc.setSpacing(2)
        tc.addWidget(title)
        tc.addWidget(sub)

        hl.addLayout(tc)
        hl.addStretch()

        # Status indicators
        self._game_dot = QLabel("⬤")
        self._game_dot.setStyleSheet(f"color:{t.text_faint}; font-size:10px;")
        self._game_dot.setToolTip("Game detected")
        self._game_name_lbl = QLabel("No game")
        self._game_name_lbl.setStyleSheet(f"color:{t.text_faint}; font-size:11px; margin-right:16px;")

        self._rec_dot = QLabel("⬤")
        self._rec_dot.setStyleSheet(f"color:{t.text_faint}; font-size:10px;")
        self._rec_dot.setToolTip("Recorder status")
        self._rec_status_lbl = QLabel("Stopped")
        self._rec_status_lbl.setStyleSheet(f"color:{t.text_faint}; font-size:11px; margin-right:20px;")

        for w in (self._game_dot, self._game_name_lbl,
                  self._rec_dot, self._rec_status_lbl):
            hl.addWidget(w)

        # Recording toggle — large pill style
        _rec_on = self.controller.config.recording_enabled
        self.rec_toggle = QPushButton("REC  OFF")
        self.rec_toggle.setCheckable(True)
        self.rec_toggle.setChecked(_rec_on)
        self.rec_toggle.setFixedSize(110, 38)
        self._update_rec_toggle(_rec_on)
        self.rec_toggle.clicked.connect(self._toggle_recording)
        hl.addWidget(self.rec_toggle)
        root.addWidget(hdr)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.dash_tab = self._build_dash()
        self.rec_tab = RecordingTab(self.config)
        self.audio_triggers_tab = AudioTriggersTab(self.config)
        self.clips_tab = ClipsTab(self.config)
        self._auto_record_tab = AutoRecordTab(self.config)
        self._game_tabs = self._auto_record_tab.game_widgets
        self.tabs.addTab(self.dash_tab, "DASHBOARD")
        self.tabs.addTab(self.clips_tab, "CLIPS")
        self.tabs.addTab(self._auto_record_tab, "GAME TRIGGERS")
        self.tabs.addTab(self.audio_triggers_tab, "AUDIO TRIGGERS")
        self.tabs.addTab(self.rec_tab, "SETTINGS")
        root.addWidget(self.tabs, 1)

        cs2_tab = self._game_tabs.get("CS2")
        if cs2_tab and hasattr(cs2_tab, "install_btn"):
            cs2_tab.install_btn.clicked.connect(self._install_gsi)

    def _build_dash(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Thin stat strip at top
        t = _theme.current
        strip = QWidget()
        strip.setFixedHeight(44)
        strip.setStyleSheet(f"background:{t.bg_base}; border-bottom:1px solid {t.border};")
        sl = QHBoxLayout(strip)
        sl.setContentsMargins(24, 0, 24, 0)
        sl.setSpacing(32)

        self._clips_lbl = QLabel("CLIPS  0")
        self._clips_lbl.setStyleSheet(
            f"color:{t.text_faint}; font-size:11px; letter-spacing:1px;")
        sl.addWidget(self._clips_lbl)
        sl.addStretch()

        log_lbl = QLabel("EVENT LOG")
        log_lbl.setStyleSheet(f"color:{t.text_faint}; font-size:10px; letter-spacing:2px;")
        sl.addWidget(log_lbl)
        layout.addWidget(strip)

        self.event_log = EventLog()
        layout.addWidget(self.event_log, 1)
        return w

    def _build_tray(self):
        self.tray = QSystemTrayIcon(self)
        icon = app_icon()
        self.tray.setIcon(icon)
        self.setWindowIcon(icon)
        m = QMenu()
        show = QAction("Show AutoClip", self)
        show.triggered.connect(self.surface)
        upd = QAction("Check for Updates…", self)
        upd.triggered.connect(lambda: self._check_for_updates(manual=True))
        quit_a = QAction("Quit", self)
        quit_a.triggered.connect(self._quit)
        m.addAction(show)
        m.addAction(upd)
        m.addSeparator()
        m.addAction(quit_a)
        self.tray.setContextMenu(m)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _on_tray_activated(self, reason):
        # Left/middle click or double-click surfaces the window; right-click opens
        # the context menu (handled by Qt) so don't surface on Context.
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick,
                      QSystemTrayIcon.ActivationReason.MiddleClick):
            self.surface()

    def surface(self):
        """Bring the window to the foreground from the tray / hidden / minimized."""
        if self.isMinimized():
            self.showNormal()      # restore without clobbering a maximized state
        else:
            self.show()            # unhide; keeps prior normal/maximized state
        self.raise_()
        self.activateWindow()

    def _on_update_btn_clicked(self):
        # One button, two roles: install a found update, else check for one.
        if self._pending_update:
            self._install_update()
        else:
            self._check_for_updates(manual=True)

    def _check_for_updates(self, manual: bool = False):
        """Check GitHub releases in the background; result handled on the GUI thread."""
        import threading
        from ..core import updater
        tab = getattr(self, "rec_tab", None)
        if tab is not None and hasattr(tab, "_update_btn"):
            tab._update_btn.setEnabled(False)
            tab._update_status_lbl.setText("Checking for updates…")
            tab._update_status_lbl.setStyleSheet(
                f"color:{_theme.current.text_dim}; font-size:12px;")

        def _work():
            self._update_sig.emit(updater.check_for_update(), manual)
        threading.Thread(target=_work, daemon=True).start()

    def _on_update_result(self, result, manual: bool):
        """Update the Settings UI (and a non-modal tray alert) — never auto-installs."""
        from .. import __version__ as _ver
        tab = getattr(self, "rec_tab", None)
        has_ui = tab is not None and hasattr(tab, "_update_btn")
        if has_ui:
            tab._update_btn.setEnabled(True)

        if not result:
            self._pending_update = None
            if has_ui:
                tab._update_btn.setText("Check for Updates")
                tab._update_status_lbl.setText(f"Up to date (v{_ver})")
                tab._update_status_lbl.setStyleSheet(
                    f"color:{_theme.current.text_dim}; font-size:12px;")
            if manual:
                self.tray.showMessage("AutoClip", f"You're up to date (v{_ver}).",
                                      QSystemTrayIcon.MessageIcon.Information, 2500)
            return

        tag, _win_url, _page = result
        self._pending_update = result
        if has_ui:
            tab._update_status_lbl.setText(f"Update available: {tag}")
            tab._update_status_lbl.setStyleSheet(
                f"color:{_theme.current.accent}; font-size:12px; font-weight:bold;")
            tab._update_btn.setText(f"Install {tag}")
        # Non-modal alert so a background check still lets the user know.
        self.tray.showMessage("AutoClip — update available",
                              f"{tag} is ready. Open Settings → Updates to install.",
                              QSystemTrayIcon.MessageIcon.Information, 4000)

    def _install_update(self):
        """One-click install of the pending update (download/apply runs off the GUI thread)."""
        import threading, webbrowser
        from ..core import updater
        if not self._pending_update:
            return
        tab = self.rec_tab
        tag, win_url, page = self._pending_update
        if not updater.can_self_update():
            # Dev/source-without-git or no installer asset — hand off to the browser.
            webbrowser.open(page)
            tab._update_status_lbl.setText("Opened the download page in your browser.")
            return
        tab._update_btn.setEnabled(False)
        tab._update_status_lbl.setText(f"Downloading {tag}…")

        def _work():
            self._apply_sig.emit(updater.apply_update(win_url), page)
        threading.Thread(target=_work, daemon=True).start()

    def _on_apply_result(self, status: str, page: str):
        import webbrowser
        tab = self.rec_tab
        if status == "installing":
            self._quit()
        elif status == "updated-restart":
            tab._update_status_lbl.setText("Updated — restart AutoClip to apply.")
            QMessageBox.information(self, "Update installed",
                                   "AutoClip was updated. Restart it to apply the changes.")
            self._quit()
        else:
            tab._update_status_lbl.setText("Update failed — opened the download page.")
            tab._update_btn.setEnabled(True)
            webbrowser.open(page)

    def _wire(self):
        self._status_sig.connect(self._on_status)
        self._event_sig.connect(self._on_event)
        self._clip_sig.connect(self._on_clip)
        self._update_sig.connect(self._on_update_result)
        self._apply_sig.connect(self._on_apply_result)
        # Background-check shortly after launch to ALERT (non-modal) when an update
        # exists — it never installs on its own; the user clicks Install in Settings.
        from ..core import updater
        if updater.can_self_update():
            QTimer.singleShot(4000, lambda: self._check_for_updates(manual=False))
        self.controller.on_status_change = lambda s: self._status_sig.emit(s)
        self.controller.on_event = lambda e: self._event_sig.emit(e)
        self.controller.on_clip_saved = lambda r: self._clip_sig.emit(r)
        self.rec_tab._controller = self.controller
        self.rec_tab._main_window = self   # so the Settings "Updates" button reaches update logic
        self.audio_triggers_tab._controller = self.controller
        # Pass plugin trigger styles to event log
        styles = getattr(self.controller, 'trigger_log_style', {})
        self.event_log.set_trigger_styles(styles)
        # Unsaved-changes guard for Settings tab
        self._prev_tab_index = 0
        # Initialise clip counter from disk so it shows total, not just this session
        self._refresh_clip_count()
        self._tab_change_guard = False
        self._clips_loaded = False
        self.tabs.currentChanged.connect(self._on_tab_changed)

    def _on_tab_changed(self, new_index: int):
        if self._tab_change_guard:
            self._prev_tab_index = new_index
            return
        settings_index = self.tabs.indexOf(self.rec_tab)
        if (self._prev_tab_index == settings_index
                and self.rec_tab._dirty):
            theme_changed = (
                self.rec_tab._theme_combo.currentData() != self.rec_tab.config.theme)
            msg = QMessageBox(self)
            msg.setWindowTitle("Unsaved Settings")
            msg.setText("You have unsaved changes in Settings.")
            info = "Save before leaving?"
            if theme_changed:
                info += "\n\nNote: theme changes take effect after restarting AutoClip."
            msg.setInformativeText(info)
            save_btn    = msg.addButton("Save",    QMessageBox.ButtonRole.AcceptRole)
            discard_btn = msg.addButton("Discard", QMessageBox.ButtonRole.DestructiveRole)  # noqa: F841
            cancel_btn  = msg.addButton("Cancel",  QMessageBox.ButtonRole.RejectRole)
            msg.setDefaultButton(save_btn)
            msg.exec()
            if msg.clickedButton() is cancel_btn:
                self._tab_change_guard = True
                self.tabs.setCurrentIndex(settings_index)
                self._tab_change_guard = False
                return
            if msg.clickedButton() is save_btn:
                self.rec_tab._save()
        self._prev_tab_index = new_index
        clips_index = self.tabs.indexOf(self.clips_tab)
        if new_index == clips_index and not self._clips_loaded:
            self._clips_loaded = True
            self.clips_tab.refresh()

    def _on_status(self, s: str):
        recording = s.lower() in ("running", "recording")
        t = _theme.current
        self._rec_dot.setStyleSheet(
            f"color:{t.success if recording else t.text_faint}; font-size:10px;")
        self._rec_status_lbl.setStyleSheet(
            f"color:{t.success if recording else t.text_faint}; font-size:11px; margin-right:20px;")
        self._rec_status_lbl.setText(s.capitalize() if s else "Stopped")

    def _on_event(self, e: str):
        t = _theme.current
        if e.startswith("game_started:"):
            self._current_game = e.split(":", 1)[1]
            self._game_dot.setStyleSheet(f"color:{t.success}; font-size:10px;")
            self._game_name_lbl.setStyleSheet(
                f"color:{t.success}; font-size:11px; margin-right:16px;")
            self._game_name_lbl.setText(self._current_game)
            self.event_log.add_system(f"Game started: {self._current_game}")
        elif e.startswith("game_audio_resolved:"):
            game_name = e.split(":", 1)[1]
            self.rec_tab.audio_manager.reload_tracks(active_game=game_name)
        elif e == "game_closed":
            self._current_game = None
            self._game_dot.setStyleSheet(f"color:{t.text_faint}; font-size:10px;")
            self._game_name_lbl.setStyleSheet(
                f"color:{t.text_faint}; font-size:11px; margin-right:16px;")
            self._game_name_lbl.setText("No game")
            self.event_log.add_system("Game closed")
            self.rec_tab.audio_manager.reload_tracks(active_game="")
        else:
            # Format: "game:trigger"
            game    = e.split(":", 1)[0] if ":" in e else ""
            trigger = e.split(":", 1)[1] if ":" in e else e
            self.event_log.add_trigger(trigger, game)

    def _refresh_clip_count(self):
        from autoclip.core.clips import scan_library
        clips = scan_library(self.config.output_dir)
        self._clip_count = len(clips)
        self._clips_lbl.setText(f"CLIPS  {self._clip_count}")

    def _on_clip(self, reason: str):
        self._clip_count += 1
        self._clips_lbl.setText(f"CLIPS  {self._clip_count}")
        triggers_raw = reason.split("|")[0] if "|" in reason else reason
        # Build display using plugin trigger log styles
        styles = getattr(self.controller, "trigger_log_style", {})
        parts = []
        for t in triggers_raw.split("_"):
            if t in styles:
                name = styles[t][0]
            else:
                try:
                    from autoclip.games.registry import build_global_metadata_tables
                    tables = build_global_metadata_tables()
                    name = tables["trigger_display"].get(t, t.replace("_", " ").title())
                except Exception:
                    name = t.replace("_", " ").title()
            if name not in parts:
                parts.append(name)
        label = " + ".join(parts) if parts else triggers_raw
        self.event_log.add_clip_saved(label)
        self.tray.showMessage("AutoClip", f"Clip saved — {label}",
                              QSystemTrayIcon.MessageIcon.Information, 2000)

    def _update_rec_toggle(self, on: bool):
        t = _theme.current
        if on:
            self.rec_toggle.setText("◉  REC")
            self.rec_toggle.setStyleSheet(
                f"QPushButton {{"
                f"  background:{t.accent}; color:{t.accent_text};"
                f"  border:2px solid {t.accent}; border-radius:19px;"
                f"  font-size:13px; font-weight:bold; letter-spacing:2px;"
                f"}}"
            )
        else:
            self.rec_toggle.setText("○  REC")
            self.rec_toggle.setStyleSheet(
                f"QPushButton {{"
                f"  background:{t.bg_raised}; color:{t.text_faint};"
                f"  border:2px solid {t.border}; border-radius:19px;"
                f"  font-size:13px; font-weight:bold; letter-spacing:2px;"
                f"}}"
                f"QPushButton:hover {{ border-color:{t.text_dim}; color:{t.text_dim}; }}"
            )

    def _toggle_recording(self, checked: bool):
        self.controller.set_recording_enabled(checked)
        self._update_rec_toggle(checked)

    def _install_gsi(self):
        cs2_tab = self._game_tabs.get("CS2")
        if self.controller.install_game_integration("CS2"):
            # Get path from the CS2 plugin directly
            try:
                from autoclip.games.cs2 import GSIServer
                path = GSIServer(self.controller.config, lambda e: None).find_cs2_cfg_path()
            except Exception:
                path = "unknown path"
            if cs2_tab and hasattr(cs2_tab, "gsi_status"):
                cs2_tab.gsi_status.setText(f"✓ Installed to {path}")
                cs2_tab.gsi_status.setStyleSheet(f"color: {_theme.current.success}; font-size: 12px;")
            if cs2_tab and hasattr(cs2_tab, "install_btn"):
                cs2_tab.install_btn.setVisible(False)   # installed now — hide the button
            QMessageBox.information(self, "GSI Installed",
                "Config installed.\nRestart CS2 for it to take effect.")
        else:
            if cs2_tab and hasattr(cs2_tab, "gsi_status"):
                cs2_tab.gsi_status.setText("✗ CS2 not found — install manually")
                cs2_tab.gsi_status.setStyleSheet(f"color: {_theme.current.error}; font-size: 12px;")
            QMessageBox.warning(self, "Not Found",
                "Could not find CS2 installation.\n"
                "You may need to copy the GSI config manually.")

    def closeEvent(self, event):
        event.ignore()
        self.hide()
        self.tray.showMessage(
            "AutoClip", "Still running in the system tray — recording continues. "
            "Right-click the tray icon to quit.",
            QSystemTrayIcon.MessageIcon.Information, 2500)

    # --- Free UI resources while hidden to the system tray ------------------
    # When the window goes to the tray we tear down the whole clip player so
    # mpv's memory (core + decode buffers) is released; it's rebuilt on demand
    # when the user reopens a clip. Recording, GSI, and the audio/game triggers
    # run in their own threads/process and are deliberately untouched.
    def hideEvent(self, event):
        self._enter_tray()
        super().hideEvent(event)

    def showEvent(self, event):
        self._exit_tray()
        super().showEvent(event)

    def _enter_tray(self):
        if getattr(self, "_in_tray", False) or not hasattr(self, "clips_tab"):
            return
        self._in_tray = True
        try:
            self.clips_tab.release_player()
            self.tabs.setCurrentIndex(0)   # reopen on the dashboard
        except Exception:
            logger.debug("tray teardown failed", exc_info=True)

    def _exit_tray(self):
        if not getattr(self, "_in_tray", False):
            return
        self._in_tray = False
        try:
            self.clips_tab.restore_after_tray()
        except Exception:
            logger.debug("tray restore failed", exc_info=True)

    def _quit(self):
        self._save_geometry()
        self.controller.stop()
        QApplication.quit()

    def _save_geometry(self):
        from pathlib import Path as _P
        import json
        from autoclip.core.config import CONFIG_DIR
        geo = self.geometry()
        try:
            p = CONFIG_DIR / "window.json"
            p.write_text(json.dumps(
                {"x": geo.x(), "y": geo.y(), "w": geo.width(), "h": geo.height()}))
        except Exception:
            pass

    def _restore_geometry(self) -> bool:
        from pathlib import Path as _P
        import json
        from autoclip.core.config import CONFIG_DIR
        try:
            p = CONFIG_DIR / "window.json"
            if p.exists():
                d = json.loads(p.read_text())
                screen = QApplication.primaryScreen().geometry()
                x = max(0, min(d["x"], screen.width()  - 200))
                y = max(0, min(d["y"], screen.height() - 200))
                w = max(800, min(d["w"], screen.width()))
                h = max(600, min(d["h"], screen.height()))
                self.setGeometry(x, y, w, h)
                return True
        except Exception:
            pass
        return False


def run_app():
    logging.basicConfig(level=logging.DEBUG,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    import sys as _sys, atexit, traceback, faulthandler
    # A windowed (no-console) frozen build has sys.stderr == None; faulthandler
    # needs a real file, so point it at the log file (or skip if neither exists).
    if _sys.stderr is not None:
        faulthandler.enable()
    else:
        for h in logging.getLogger().handlers:
            stream = getattr(h, "stream", None)
            if stream is not None and hasattr(stream, "fileno"):
                try:
                    faulthandler.enable(stream)
                    break
                except (ValueError, OSError):
                    pass
    def _on_exit():
        import logging as _l
        _l.getLogger(__name__).warning("run_app: process exiting (atexit)\n%s",
                                       "".join(traceback.format_stack()))
    atexit.register(_on_exit)
    # Force native desktop OpenGL on Windows — Qt defaults to ANGLE (DirectX)
    # which is incompatible with mpv's OpenGL render context.
    if _sys.platform == "win32":
        import os as _os
        _os.environ.setdefault("QT_OPENGL", "desktop")
        # Windows toast notifications inherit the app name + icon from the process's
        # AppUserModelID. Without one they show "python" + a generic icon. Register an
        # explicit ID (display name + icon) and adopt it so toasts read "AutoClip" with
        # our icon. (The installer also tags its Start-menu shortcut with this same ID.)
        try:
            import ctypes as _ct, winreg as _wr
            _aumid = "SmoJa.AutoClip"
            _ico = str(Path(__file__).parent / "autoclip.ico")
            with _wr.CreateKey(_wr.HKEY_CURRENT_USER,
                               rf"Software\Classes\AppUserModelId\{_aumid}") as _k:
                _wr.SetValueEx(_k, "DisplayName", 0, _wr.REG_SZ, "AutoClip")
                if _os.path.exists(_ico):
                    _wr.SetValueEx(_k, "IconUri", 0, _wr.REG_SZ, _ico)
            _ct.windll.shell32.SetCurrentProcessExplicitAppUserModelID(_ct.c_wchar_p(_aumid))
        except Exception:
            pass
    app = QApplication(sys.argv)
    app.setApplicationName("AutoClip")
    # Tie the process to its desktop entry (autoclip.desktop) so Linux/Wayland match
    # the app icon for the window, taskbar, and notifications. No-op off Linux.
    app.setDesktopFileName("autoclip")

    # Single-instance guard (cross-platform via Qt local sockets). A second launch
    # connects to the running instance, asks it to surface, then exits — before any
    # heavy init or binding the GSI port. If the connect fails we are the primary.
    from PyQt6.QtNetwork import QLocalServer, QLocalSocket
    import getpass
    _sock_name = f"autoclip-singleton-{getpass.getuser()}"
    _probe = QLocalSocket()
    _probe.connectToServer(_sock_name)
    if _probe.waitForConnected(250):
        _probe.write(b"surface")
        _probe.flush()
        _probe.waitForBytesWritten(500)
        _probe.disconnectFromServer()
        logging.getLogger(__name__).info(
            "Another AutoClip instance is already running — surfaced it and exiting.")
        return
    QLocalServer.removeServer(_sock_name)   # clear any stale socket from a crash
    _singleton_server = QLocalServer()
    _singleton_server.listen(_sock_name)

    config = Config.load()
    controller = AppController(config)
    _theme.load(config.theme)
    app.setStyleSheet(_theme.build_stylesheet(_theme.current))
    app.setQuitOnLastWindowClosed(False)
    window = MainWindow(controller)

    # When a second launch pings the singleton server, surface this window.
    def _on_second_instance():
        conn = _singleton_server.nextPendingConnection()
        if conn is not None:
            conn.readAll()
            conn.disconnectFromServer()
        window.surface()
    _singleton_server.newConnection.connect(_on_second_instance)

    if config.start_minimized:
        # Stay hidden in the tray; let the user know it launched so it's not "missing".
        window.tray.showMessage(
            "AutoClip", "Started in the system tray — click the icon to open.",
            QSystemTrayIcon.MessageIcon.Information, 2500)
    else:
        window.show()
    try:
        controller.start()
    except RuntimeError as e:
        from PyQt6.QtWidgets import QMessageBox
        QMessageBox.critical(None, "AutoClip — Startup Error", str(e))
        sys.exit(1)

    # On Windows, game detection runs on the main thread via QTimer instead of a
    # background thread — background threads AV after Qt/mpv corrupt Python's heap.
    if _sys.platform == "win32":
        _game_timer = QTimer()
        _game_timer.timeout.connect(controller.tick_game_detection)
        _game_timer.start(5000)

    sys.exit(app.exec())
