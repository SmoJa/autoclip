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
                 parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header button
        self._toggle = QPushButton()
        self._toggle.setCheckable(True)
        self._toggle.setChecked(expanded)
        t = _theme.current
        self._toggle.setStyleSheet(
            f"QPushButton {{"
            f"  background: {t.bg_base}; border: none;"
            f"  border-bottom: 1px solid {t.border};"
            f"  color: {t.text}; font-size: 13px; font-weight: bold;"
            f"  padding: 10px 16px; text-align: left;"
            f"}}"
            f"QPushButton:hover {{ background: {t.bg_raised}; }}"
            f"QPushButton:checked {{ border-bottom: 2px solid {t.accent}; }}"
        )
        self._update_label(title, expanded)
        self._toggle.clicked.connect(lambda checked: self._on_toggle(title, checked))
        layout.addWidget(self._toggle)

        # Content — stretch=1 so it fills available space when expanded
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
        from PyQt6.QtWidgets import QSizePolicy as _QSP
        sp = self.sizePolicy()
        sp.setVerticalPolicy(
            _QSP.Policy.Expanding if checked else _QSP.Policy.Maximum
        )
        self.setSizePolicy(sp)
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

        self._layout = QVBoxLayout(self)
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
                    section = CollapsibleSection(
                        plugin_cls.NAME, widget,
                        expanded=(i == 0)
                    )
                    section.toggled.connect(self._on_section_toggled)
                    self._layout.addWidget(section, 0)
                    self._sections.append(section)

            # Trailing spacer absorbs unused space when all sections are collapsed
            self._layout.addStretch(0)
            self._update_stretches()

    def _on_section_toggled(self, _expanded: bool):
        self._update_stretches()

    def _update_stretches(self):
        any_expanded = any(s._toggle.isChecked() for s in self._sections)
        for s in self._sections:
            self._layout.setStretch(self._layout.indexOf(s), 1 if s._toggle.isChecked() else 0)
        # Trailing stretch fills space only when everything is collapsed
        self._layout.setStretch(self._layout.count() - 1, 0 if any_expanded else 1)


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
        self.autostart_chk.setToolTip(
            "Creates an autostart entry so AutoClip launches when you log in.\n"
            "Uses XDG autostart (~/.config/autostart/autoclip.desktop)."
        )
        self.autostart_chk.toggled.connect(self._on_autostart_toggled)
        app_l.addWidget(self.autostart_chk)
        layout.addWidget(app_g)

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

        rec_l.addWidget(QLabel("gpu-screen-recorder path"), 0, 0)
        self.rec_path = QLineEdit(config.gpu_recorder_path)
        rec_l.addWidget(self.rec_path, 0, 1)

        rec_l.addWidget(QLabel("FPS"), 1, 0)
        self.fps = NoScrollSpinBox()
        self.fps.setRange(24, 240)
        self.fps.setValue(config.gpu_recorder_fps)
        rec_l.addWidget(self.fps, 1, 1)

        layout.addWidget(rec_g)

        # Encoding
        enc_g = QGroupBox("Encoding")
        enc_l = QGridLayout(enc_g)
        enc_l.setSpacing(14)

        enc_l.addWidget(QLabel("Codec"), 0, 0)
        self.codec = NoScrollComboBox()
        self.codec.addItems(["hevc_hdr", "av1_hdr", "hevc_10bit", "av1_10bit", "hevc", "av1", "h264"])
        self.codec.setCurrentText(config.gpu_recorder_codec)
        self.codec.setToolTip(
            "hevc_hdr / av1_hdr: best for HDR displays\n"
            "hevc / av1: SDR high quality\n"
            "h264: most compatible"
        )
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

        # Wire all reversible fields to dirty checking (auto-clears if reverted)
        for _sig in [
            self._theme_combo.currentIndexChanged,
            self.dir_edit.textChanged,
            self.exp_edit.textChanged,
            self.rec_path.textChanged,
            self.fps.valueChanged,
            self.codec.currentTextChanged,
            self.color_range.currentTextChanged,
            self.quality_mode.currentTextChanged,
            self.bitrate.valueChanged,
            self.quality_preset.currentTextChanged,
            self.tune.currentTextChanged,
            self.monitor_combo.currentTextChanged,
            self.pre.valueChanged,
            self.post.valueChanged,
            self.hotkey.textChanged,
        ]:
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
            or self.codec.currentText()           != c.gpu_recorder_codec
            or self.color_range.currentText()     != c.gpu_recorder_color_range
            or self.quality_mode.currentText()    != c.gpu_recorder_quality_mode
            or self.bitrate.value()               != c.gpu_recorder_bitrate_kbps
            or self.quality_preset.currentText()  != c.gpu_recorder_quality_preset
            or self.tune.currentText()            != c.gpu_recorder_tune
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

    def _on_mode_changed(self, mode: str):
        cbr_vbr = mode in ("cbr", "vbr")
        self.bitrate.setEnabled(cbr_vbr)
        self.quality_preset.setEnabled(not cbr_vbr)

    def _is_autostart_enabled(self) -> bool:
        from pathlib import Path
        return (Path.home() / ".config/autostart/autoclip.desktop").exists()

    def _on_autostart_toggled(self, enabled: bool):
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
                "Comment=Automatic game clip recorder\n"
                f"Exec=bash -c \"cd {install_dir} && QT_QPA_PLATFORM=xcb "
                f"python3 -m autoclip.main 2>&1 | tee /tmp/autoclip.log\"\n"
                "Hidden=false\n"
                "NoDisplay=false\n"
                "X-GNOME-Autostart-enabled=true\n"
                "X-KDE-autostart-after=panel\n"
            )
            import logging
            logging.getLogger(__name__).info(f"Autostart enabled: {desktop_path}")
        else:
            if desktop_path.exists():
                desktop_path.unlink()
            import logging
            logging.getLogger(__name__).info("Autostart disabled")

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
        self.config.gpu_recorder_codec = self.codec.currentText()
        self.config.gpu_recorder_color_range = self.color_range.currentText()
        self.config.gpu_recorder_quality_mode = self.quality_mode.currentText()
        self.config.gpu_recorder_bitrate_kbps = self.bitrate.value()
        self.config.gpu_recorder_quality_preset = self.quality_preset.currentText()
        self.config.gpu_recorder_tune = self.tune.currentText()
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
        self.config.save()
        self._mark_clean()


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

        self._layout = QVBoxLayout(self)
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
            for i, cls in enumerate(plugins):
                widget = cls.get_config_widget(cls, config)
                if widget is None:
                    continue

                # Wrap plugin widget + save button in a single content widget
                content = QWidget()
                cl = QVBoxLayout(content)
                cl.setContentsMargins(16, 16, 16, 16)
                cl.setSpacing(12)
                cl.addWidget(widget)

                save_btn = QPushButton("SAVE & APPLY")
                save_btn.setObjectName("primary")
                plugin_name = cls.NAME
                save_btn.clicked.connect(
                    lambda checked=False, n=plugin_name: self._save_plugin(n)
                )
                cl.addWidget(save_btn)
                cl.addStretch()

                section = CollapsibleSection(cls.NAME, content, expanded=(i == 0))
                section.toggled.connect(self._on_section_toggled)
                self._layout.addWidget(section, 0)
                self._sections.append(section)
                self._plugin_widgets[cls.NAME] = widget

            self._layout.addStretch(0)
            self._update_stretches()

    def _on_section_toggled(self, _expanded: bool):
        self._update_stretches()

    def _update_stretches(self):
        any_expanded = any(s._toggle.isChecked() for s in self._sections)
        for s in self._sections:
            self._layout.setStretch(self._layout.indexOf(s), 1 if s._toggle.isChecked() else 0)
        self._layout.setStretch(self._layout.count() - 1, 0 if any_expanded else 1)

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

    def __init__(self, controller: AppController):
        super().__init__()
        self.controller = controller
        self.config = controller.config
        self._current_game = None
        self._clip_count = 0
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
        self.rec_toggle = QPushButton("REC  OFF")
        self.rec_toggle.setCheckable(True)
        self.rec_toggle.setChecked(True)
        self.rec_toggle.setFixedSize(110, 38)
        self._update_rec_toggle(True)
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

        self.status_bar = StatusBar()
        root.addWidget(self.status_bar)

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
        # Generate a simple coloured icon programmatically — no icon file needed
        from PyQt6.QtGui import QPixmap, QColor, QPainter
        pixmap = QPixmap(32, 32)
        pixmap.fill(QColor(0, 0, 0, 0))
        painter = QPainter(pixmap)
        painter.setBrush(QColor(_theme.current.accent))
        painter.setPen(QColor(_theme.current.accent))
        painter.drawEllipse(4, 4, 24, 24)
        painter.end()
        from PyQt6.QtGui import QIcon
        self.tray.setIcon(QIcon(pixmap))
        self.setWindowIcon(QIcon(pixmap))
        m = QMenu()
        show = QAction("Show AutoClip", self)
        show.triggered.connect(self.show)
        quit_a = QAction("Quit", self)
        quit_a.triggered.connect(self._quit)
        m.addAction(show)
        m.addSeparator()
        m.addAction(quit_a)
        self.tray.setContextMenu(m)
        self.tray.activated.connect(lambda _: self.show())
        self.tray.show()

    def _wire(self):
        self._status_sig.connect(self._on_status)
        self._event_sig.connect(self._on_event)
        self._clip_sig.connect(self._on_clip)
        self.controller.on_status_change = lambda s: self._status_sig.emit(s)
        self.controller.on_event = lambda e: self._event_sig.emit(e)
        self.controller.on_clip_saved = lambda r: self._clip_sig.emit(r)
        self.rec_tab._controller = self.controller
        self.audio_triggers_tab._controller = self.controller
        # Pass plugin trigger styles to event log
        styles = getattr(self.controller, 'trigger_log_style', {})
        self.event_log.set_trigger_styles(styles)
        # Unsaved-changes guard for Settings tab
        self._prev_tab_index = 0
        self._tab_change_guard = False
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

    def _on_status(self, s: str):
        self.status_bar.set_status(s, self._current_game or "")
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
            self.status_bar.set_status("recording", self._current_game)
            self.event_log.add_system(f"Game started: {self._current_game}")
        elif e == "game_closed":
            self._current_game = None
            self._game_dot.setStyleSheet(f"color:{t.text_faint}; font-size:10px;")
            self._game_name_lbl.setStyleSheet(
                f"color:{t.text_faint}; font-size:11px; margin-right:16px;")
            self._game_name_lbl.setText("No game")
            self.event_log.add_system("Game closed")
        else:
            # Format: "game:trigger"
            game    = e.split(":", 1)[0] if ":" in e else ""
            trigger = e.split(":", 1)[1] if ":" in e else e
            self.event_log.add_trigger(trigger, game)

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
        self.tray.showMessage("AutoClip", "Running in system tray",
                              QSystemTrayIcon.MessageIcon.Information, 1500)

    def _quit(self):
        self._save_geometry()
        self.controller.stop()
        QApplication.quit()

    def _save_geometry(self):
        from pathlib import Path as _P
        import json
        geo = self.geometry()
        try:
            p = _P.home() / ".config" / "autoclip" / "window.json"
            p.write_text(json.dumps(
                {"x": geo.x(), "y": geo.y(), "w": geo.width(), "h": geo.height()}))
        except Exception:
            pass

    def _restore_geometry(self) -> bool:
        from pathlib import Path as _P
        import json
        try:
            p = _P.home() / ".config" / "autoclip" / "window.json"
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
    config = Config.load()
    controller = AppController(config)
    app = QApplication(sys.argv)
    app.setApplicationName("AutoClip")
    _theme.load(config.theme)
    app.setStyleSheet(_theme.build_stylesheet(_theme.current))
    app.setQuitOnLastWindowClosed(False)
    window = MainWindow(controller)
    window.show()
    try:
        controller.start()
    except RuntimeError as e:
        from PyQt6.QtWidgets import QMessageBox
        QMessageBox.critical(None, "AutoClip — Startup Error", str(e))
        sys.exit(1)
    sys.exit(app.exec())
