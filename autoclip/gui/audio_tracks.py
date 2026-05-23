# SPDX-License-Identifier: GPL-3.0-or-later
"""
Audio track manager widget for the Recording tab.
"""
import logging
import threading
from typing import List, Dict, Any, Tuple

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QCheckBox, QLineEdit, QFrame, QSizePolicy,
    QDialog, QDialogButtonBox, QMessageBox,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from .widgets import NoScrollComboBox
from . import theme as _theme

logger = logging.getLogger(__name__)

# (section_key, track_types, header_text, default_add_type)
_SECTIONS: List[Tuple[str, Tuple[str, ...], str, str]] = [
    ("game",     ("game",),        "GAME",       "game"),
    ("chat_mic", ("mic", "chat"),  "CHAT & MIC", "mic"),
    ("other",    ("custom",),      "OTHER APPS", "custom"),
]


def _section_for_type(track_type: str) -> str:
    for key, types, _, _ in _SECTIONS:
        if track_type in types:
            return key
    return "other"


class TrackRow(QWidget):
    changed          = pyqtSignal()
    remove_requested = pyqtSignal(object)

    def __init__(self, track: Dict[str, Any], sources: List[Dict], parent=None):
        super().__init__(parent)
        self._track = track
        t = _theme.current
        self.setFixedHeight(44)
        self.setStyleSheet(f"background: {t.bg_base}; border-bottom: 1px solid {t.border};")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 4, 10, 4)
        layout.setSpacing(10)

        # Label edit
        self._label_edit = QLineEdit(track.get("label", "Track"))
        self._label_edit.setFixedWidth(110)
        self._label_edit.setStyleSheet(
            f"background: {t.bg_deep}; border: 1px solid {t.border}; "
            f"color: {t.text}; padding: 3px 6px; font-size: 11px;"
        )
        self._label_edit.textChanged.connect(self._on_label_changed)
        layout.addWidget(self._label_edit)

        # Source dropdown
        self._source_combo = NoScrollComboBox()
        self._source_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._source_combo.setMinimumWidth(200)
        self._source_combo.setSizeAdjustPolicy(
            NoScrollComboBox.SizeAdjustPolicy.AdjustToContents)
        self._source_combo.setStyleSheet(
            f"background: {t.bg_deep}; border: 1px solid {t.border}; "
            f"color: {t.text}; padding: 3px 6px; font-size: 11px;"
        )
        self._populate_sources(sources, track.get("device", ""))
        self._source_combo.currentIndexChanged.connect(self._on_source_changed)
        layout.addWidget(self._source_combo, 1)

        # Enable checkbox
        self._enable_cb = QCheckBox()
        self._enable_cb.setChecked(track.get("enabled", True))
        self._enable_cb.setToolTip("Enable this audio track")
        self._enable_cb.toggled.connect(self._on_enabled_changed)
        layout.addWidget(self._enable_cb)

        # Remove button
        rm_btn = QPushButton("✕")
        rm_btn.setFixedSize(26, 26)
        rm_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {t.text_faint}; border: none; font-size: 14px; }}"
            f"QPushButton:hover {{ color: {t.error}; }}"
        )
        rm_btn.clicked.connect(self._confirm_remove)
        layout.addWidget(rm_btn)

    def _confirm_remove(self):
        label = self._track.get("label", "this track")
        reply = QMessageBox.question(
            self,
            "Remove Track",
            f"Remove audio track \"{label}\"?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.remove_requested.emit(self)

    def _populate_sources(self, sources: List[Dict], current_device: str):
        self._source_combo.blockSignals(True)
        self._source_combo.clear()
        self._source_combo.addItem("— none —", "")

        for src in sources:
            device  = src.get("device", "")
            display = src.get("name", device)
            self._source_combo.addItem(display, device)

        self._source_combo.addItem("+ Enter manually…", "__manual__")

        matched = False
        for i in range(self._source_combo.count()):
            if self._source_combo.itemData(i) == current_device:
                self._source_combo.setCurrentIndex(i)
                matched = True
                break

        if not matched and current_device:
            insert_pos = self._source_combo.count() - 1
            self._source_combo.insertItem(insert_pos, current_device, current_device)
            self._source_combo.setCurrentIndex(insert_pos)

        self._source_combo.blockSignals(False)

    def update_sources(self, sources: List[Dict]):
        current = self._track.get("device", "")
        self._populate_sources(sources, current)

    def _on_label_changed(self, text: str):
        self._track["label"] = text
        self.changed.emit()

    def _on_source_changed(self, index: int):
        device = self._source_combo.itemData(index)
        if device == "__manual__":
            dialog = QDialog(self)
            dialog.setWindowTitle("Enter audio source")
            tt = _theme.current
            dialog.setStyleSheet(f"background: {tt.bg_deep}; color: {tt.text};")
            dl = QVBoxLayout(dialog)
            lbl = QLabel("Paste the device name or application path:")
            lbl.setStyleSheet(f"color: {tt.text_dim}; font-size: 11px;")
            dl.addWidget(lbl)
            edit = QLineEdit()
            edit.setStyleSheet(
                f"background: {tt.bg_base}; border: 1px solid {tt.border}; "
                f"color: {tt.text}; padding: 6px;"
            )
            dl.addWidget(edit)
            btns = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Ok |
                QDialogButtonBox.StandardButton.Cancel
            )
            btns.accepted.connect(dialog.accept)
            btns.rejected.connect(dialog.reject)
            dl.addWidget(btns)
            if dialog.exec() == QDialog.DialogCode.Accepted and edit.text().strip():
                manual = edit.text().strip()
                self._source_combo.blockSignals(True)
                insert_pos = self._source_combo.count() - 1
                self._source_combo.insertItem(insert_pos, manual, manual)
                self._source_combo.setCurrentIndex(insert_pos)
                self._source_combo.blockSignals(False)
                self._track["device"] = manual
                self.changed.emit()
            else:
                prev = self._track.get("device", "")
                self._source_combo.blockSignals(True)
                for i in range(self._source_combo.count()):
                    if self._source_combo.itemData(i) == prev:
                        self._source_combo.setCurrentIndex(i)
                        break
                self._source_combo.blockSignals(False)
            return

        self._track["device"] = device or ""
        self.changed.emit()

    def _on_enabled_changed(self, checked: bool):
        self._track["enabled"] = checked
        self.changed.emit()

    def track_data(self) -> Dict[str, Any]:
        return self._track


class AudioTrackManager(QWidget):
    """Audio track manager with thread-safe signals for source detection."""
    changed = pyqtSignal()

    _sources_ready_sig = pyqtSignal(list)
    _auto_tracks_sig   = pyqtSignal(list)
    _status_sig        = pyqtSignal(str)

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self._sources: List[Dict] = []
        self._track_rows: List[TrackRow] = []
        self._section_layouts: Dict[str, QVBoxLayout] = {}

        self._sources_ready_sig.connect(self._on_sources_ready)
        self._auto_tracks_sig.connect(self._apply_auto_tracks)
        self._status_sig.connect(self._set_status)

        t = _theme.current
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # Toolbar
        toolbar = QHBoxLayout()
        self._status_lbl = QLabel("Click Refresh to detect sources")
        self._status_lbl.setStyleSheet(f"color: {t.text_dim}; font-size: 11px;")

        refresh_btn = QPushButton("↻  REFRESH SOURCES")
        refresh_btn.setStyleSheet(
            f"background: {t.bg_raised}; color: {t.text}; border: 1px solid {t.border}; "
            f"padding: 5px 12px; font-size: 11px;"
        )
        refresh_btn.clicked.connect(self.refresh_sources)

        toolbar.addWidget(self._status_lbl)
        toolbar.addStretch()
        toolbar.addWidget(refresh_btn)
        layout.addLayout(toolbar)

        # Three sections
        for key, _types, header_text, add_type in _SECTIONS:
            section_widget = self._make_section(key, header_text, add_type)
            layout.addWidget(section_widget)

        note = QLabel(
            "Game audio uses per-app PipeWire capture to record only the game. "
            "Launch the game and Discord before clicking Auto-Detect for best results."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {t.text_faint}; font-size: 10px;")
        layout.addWidget(note)

        self._load_tracks()
        QTimer.singleShot(500, self.refresh_sources)

    # ------------------------------------------------------------------ #
    #  Section construction                                                #
    # ------------------------------------------------------------------ #

    def _make_section(self, key: str, header_text: str, add_type: str) -> QWidget:
        t = _theme.current
        track_color = t.track_colors().get(add_type, t.text_dim)

        outer = QWidget()
        outer.setStyleSheet(
            f"QWidget#section_{key} {{ background: {t.bg_base}; border: 1px solid {t.border}; }}"
        )
        outer.setObjectName(f"section_{key}")
        vl = QVBoxLayout(outer)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(0)

        # Section header
        hdr = QWidget()
        hdr.setFixedHeight(30)
        hdr.setStyleSheet(f"background: {t.bg_deep}; border-bottom: 1px solid {t.border};")
        hl = QHBoxLayout(hdr)
        hl.setContentsMargins(10, 0, 8, 0)
        hl.setSpacing(8)

        title_lbl = QLabel(header_text)
        title_lbl.setStyleSheet(
            f"color: {track_color}; font-size: 9px; font-weight: bold; letter-spacing: 1px;"
            f" border: none; background: transparent;"
        )
        hl.addWidget(title_lbl)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"background: {t.border}; max-height: 1px; border: none;")
        sep.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        hl.addWidget(sep, 1)

        add_btn = QPushButton("+ ADD")
        add_btn.setFixedHeight(20)
        add_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {t.text_dim}; "
            f"border: 1px solid {t.border}; font-size: 10px; padding: 0 8px; }}"
            f"QPushButton:hover {{ color: {t.text}; border-color: {track_color}; }}"
        )
        add_btn.clicked.connect(lambda: self._add_track(add_type))
        hl.addWidget(add_btn)

        vl.addWidget(hdr)

        # Rows container
        rows_w = QWidget()
        rows_w.setStyleSheet(f"background: {t.bg_base}; border: none;")
        rows_l = QVBoxLayout(rows_w)
        rows_l.setContentsMargins(0, 0, 0, 0)
        rows_l.setSpacing(0)
        rows_l.addStretch()
        vl.addWidget(rows_w)

        self._section_layouts[key] = rows_l
        return outer

    # ------------------------------------------------------------------ #
    #  Track row management                                                #
    # ------------------------------------------------------------------ #

    def _load_tracks(self):
        # Clear all sections (keep the trailing stretch in each)
        for rows_l in self._section_layouts.values():
            while rows_l.count() > 1:
                item = rows_l.takeAt(0)
                if item and item.widget():
                    item.widget().deleteLater()
        self._track_rows.clear()

        tracks = self.config.audio_tracks
        if not tracks:
            tracks = self.config._default_audio_tracks()
            self.config.audio_tracks = tracks

        for track in tracks:
            self._add_row(track)

    def _add_row(self, track: Dict):
        ttype = track.get("track_type", "custom")
        rows_l = self._section_layouts[_section_for_type(ttype)]

        row = TrackRow(track, self._sources)
        row.changed.connect(self._on_track_changed)
        row.remove_requested.connect(self._remove_row)
        # Insert before the trailing stretch
        rows_l.insertWidget(rows_l.count() - 1, row)
        self._track_rows.append(row)

    def _add_track(self, track_type: str):
        default_labels = {"game": "Game", "mic": "Mic", "chat": "Chat", "custom": "Custom"}
        new_track = {
            "label":      default_labels.get(track_type, "Custom"),
            "device":     "",
            "enabled":    True,
            "track_type": track_type,
            "volume":     1.0,
            "muted":      False,
        }
        if self.config.audio_tracks is None:
            self.config.audio_tracks = []
        self.config.audio_tracks.append(new_track)
        self._add_row(new_track)
        self.changed.emit()

    def _remove_row(self, row: TrackRow):
        ttype = row.track_data().get("track_type", "custom")
        rows_l = self._section_layouts[_section_for_type(ttype)]

        track = row.track_data()
        if self.config.audio_tracks and track in self.config.audio_tracks:
            self.config.audio_tracks.remove(track)
        rows_l.removeWidget(row)
        row.deleteLater()
        if row in self._track_rows:
            self._track_rows.remove(row)
        self.changed.emit()

    def _on_track_changed(self):
        self.changed.emit()

    # ------------------------------------------------------------------ #
    #  Source detection — all thread-safe via signals                     #
    # ------------------------------------------------------------------ #

    def _set_status(self, msg: str):
        self._status_lbl.setText(msg)

    def refresh_sources(self):
        self._status_sig.emit("Detecting sources…")

        def _detect():
            try:
                from ..core.audio import detect_all_sources
                gsr_path = getattr(self.config, "gpu_recorder_path", "gpu-screen-recorder")
                raw = detect_all_sources(gsr_path)
                sources = [
                    {
                        "name":        s.name,
                        "device":      s.device,
                        "source_type": s.source_type,
                        "app_name":    s.app_name,
                    }
                    for s in raw
                ]
                self._sources_ready_sig.emit(sources)
            except Exception as e:
                logger.error(f"Source detection failed: {e}")
                self._status_sig.emit(f"Detection failed: {e}")

        threading.Thread(target=_detect, daemon=True).start()

    def _on_sources_ready(self, sources: List[Dict]):
        self._sources = sources
        app_count = sum(1 for s in sources if s.get("source_type") == "app")
        dev_count  = sum(1 for s in sources if s.get("source_type") != "app")
        self._status_sig.emit(
            f"{app_count} app streams, {dev_count} device sources detected"
        )
        for row in self._track_rows:
            row.update_sources(sources)

    def auto_detect(self, game: str = "", game_display_name: str = ""):
        """Auto-populate tracks — thread-safe."""
        self._status_sig.emit("Auto-detecting audio sources…")

        def _run():
            try:
                from ..core.audio import auto_detect_tracks
                gsr_path = getattr(self.config, "gpu_recorder_path",
                                   "gpu-screen-recorder")
                track_dicts = auto_detect_tracks(gsr_path, game, game_display_name)
                if not track_dicts:
                    self._status_sig.emit(
                        "No sources auto-detected — try launching the game and Discord first"
                    )
                else:
                    self._auto_tracks_sig.emit(track_dicts)
            except Exception as e:
                logger.error(f"Auto-detect failed: {e}")
                self._status_sig.emit(f"Auto-detect failed: {e}")

        threading.Thread(target=_run, daemon=True).start()

    def _apply_auto_tracks(self, tracks: List[Dict]):
        self.config.audio_tracks = tracks
        self._load_tracks()
        self.refresh_sources()
        n = len(tracks)
        self._status_sig.emit(f"Auto-detected {n} track{'s' if n != 1 else ''}")
        self.changed.emit()
