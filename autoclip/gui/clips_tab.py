# SPDX-License-Identifier: GPL-3.0-or-later
"""
Clip management tab.
- Grid view: games → dates → clips (folder icons)
- Clip view: full-tab embedded mpv player with timeline, waveform, trim, SDR export
"""
import subprocess
import logging
import threading
import queue
from pathlib import Path
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QFrame, QLineEdit, QProgressBar, QMessageBox,
    QGridLayout, QSizePolicy, QStackedWidget, QCheckBox, QSpacerItem,
    QDialog, QRadioButton, QFileDialog, QComboBox, QGroupBox,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QSize, QFileSystemWatcher
from PyQt6.QtGui import QPixmap, QColor, QPainter, QFont, QPolygon
from PyQt6.QtCore import QPoint

from ..core.clips import (
    Clip, scan_library, probe_clips_async,
    rename_clip, convert_to_sdr, trim_clip, extract_waveform, export_clip,
)
from .timeline import TimelineWidget
from .player import MpvWidget, MpvPlayer
from .track_waveforms import MultiTrackWaveform
from . import theme as _theme

logger = logging.getLogger(__name__)

# Thumbnail cache
_THUMB_DIR = Path.home() / ".cache" / "autoclip" / "thumbs"
_THUMB_DIR.mkdir(parents=True, exist_ok=True)

# Single worker thread for thumbnail generation — prevents ffmpeg overload
_thumb_queue: queue.Queue = queue.Queue()

def _thumb_worker():
    while True:
        fn = _thumb_queue.get()
        try:
            fn()
        except Exception:
            pass

threading.Thread(target=_thumb_worker, daemon=True, name="thumb-worker").start()

def _unique_export_name(export_dir: Path, name: str, suffix: str) -> str:
    """Return name, or name (1), name (2)... if file already exists."""
    if not (export_dir / (name + suffix)).exists():
        return name
    i = 1
    while (export_dir / (f"{name} ({i})" + suffix)).exists():
        i += 1
    return f"{name} ({i})"


def _thumb_hashes(clip_path: Path, duration: float = 0.0,
                  post: float = -1) -> set:
    """Return all cache-key hashes that could exist for this clip."""
    import hashlib
    seeks = {1}
    if post >= 0 and duration > 0:
        seeks.add(int(max(1, duration - post)))
    result = set()
    for seek in seeks:
        for hdr in (True, False):
            key = f"{clip_path}@{seek}@{CARD_W}@{'hdr' if hdr else 'sdr'}"
            result.add(hashlib.md5(key.encode()).hexdigest())
    return result


def _delete_clip_thumbnails(clip) -> None:
    for h in _thumb_hashes(clip.path, clip.duration_secs, clip.post_event_secs):
        p = _THUMB_DIR / f"{h}.jpg"
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass


def _purge_unused_thumbnails(clips: list) -> None:
    """Delete thumbnails that don't match any known clip. Run in background."""
    import threading
    def _run():
        known = set()
        for c in clips:
            known |= _thumb_hashes(c.path, c.duration_secs, c.post_event_secs)
        for p in _THUMB_DIR.glob("*.jpg"):
            if p.stem not in known:
                try:
                    p.unlink(missing_ok=True)
                except Exception:
                    pass
    threading.Thread(target=_run, daemon=True).start()


def _get_thumbnail(clip_path: Path, size: int = 120,
                   event_offset: int = -1,
                   is_hdr: bool = False) -> Optional["QPixmap"]:
    """
    Return cached thumbnail QPixmap at the trigger event frame.
    event_offset: seconds from clip start to trigger (-1 = use 1s fallback).
    is_hdr: apply tone-mapping only if clip is actually HDR.
    """
    from PyQt6.QtGui import QPixmap as _QPixmap
    import hashlib, subprocess
    seek = max(0, event_offset) if event_offset >= 0 else 1
    cache_key = f"{clip_path}@{seek}@{size}@{'hdr' if is_hdr else 'sdr'}"
    key = hashlib.md5(cache_key.encode()).hexdigest()
    thumb_path = _THUMB_DIR / f"{key}.jpg"
    if not thumb_path.exists():
        try:
            if is_hdr:
                vf = (
                    f"zscale=t=linear:npl=100,"
                    f"format=gbrpf32le,"
                    f"zscale=p=bt709,"
                    f"tonemap=tonemap=hable:desat=0,"
                    f"zscale=t=bt709:m=bt709:r=tv,"
                    f"format=yuv420p,"
                    f"scale={size}:-1"
                )
            else:
                vf = f"scale={size}:-1"

            result = subprocess.run([
                "ffmpeg", "-v", "error",
                "-ss", str(seek),
                "-i", str(clip_path),
                "-vframes", "1",
                "-vf", vf,
                "-q:v", "3",
                str(thumb_path)
            ], timeout=10, capture_output=True)

            if result.returncode != 0 or not thumb_path.exists():
                err = result.stderr.decode(errors="replace").strip()
                logger.warning(f"Thumbnail failed for {clip_path.name} seek={seek}: {err or 'no output'}")

            # Fallback to plain scale if HDR filter failed
            if is_hdr and (result.returncode != 0 or not thumb_path.exists()):
                subprocess.run([
                    "ffmpeg", "-v", "error",
                    "-ss", str(seek),
                    "-i", str(clip_path),
                    "-vframes", "1",
                    "-vf", f"scale={size}:-1",
                    "-q:v", "3",
                    str(thumb_path)
                ], timeout=10, capture_output=True)
        except Exception as e:
            logger.warning(f"Thumbnail exception for {clip_path.name}: {e}")
            return None
    if thumb_path.exists():
        pm = _QPixmap(str(thumb_path))
        if not pm.isNull():
            return pm
    return None

FOLDER_W = 240
FOLDER_H = 160
CARD_W   = 400
CARD_H   = 225   # 16:9 thumbnail — text overlaid on bottom


# ── Icon painters ──────────────────────────────────────────────────────────

def _folder_pixmap(label: str, color: str = None) -> QPixmap:
    color = color or _theme.current.accent
    pm = QPixmap(FOLDER_W, FOLDER_H)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    body = QColor(color); body.setAlpha(180)
    p.setBrush(body); p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(0, 18, FOLDER_W, FOLDER_H - 18, 8, 8)
    tab = QColor(color)
    p.setBrush(tab)
    p.drawRoundedRect(0, 8, FOLDER_W // 2, 20, 4, 4)
    p.setPen(QColor("#ffffff"))
    f = QFont("JetBrains Mono", 9, QFont.Weight.Bold)
    p.setFont(f)
    p.drawText(0, 18, FOLDER_W, FOLDER_H - 18, Qt.AlignmentFlag.AlignCenter,
               label[:14])
    p.end()
    return pm


def _clip_pixmap(is_hdr: bool = False) -> QPixmap:
    t = _theme.current
    pm = QPixmap(CARD_W, CARD_H)
    pm.fill(QColor(t.bg_base))
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(t.bg_deep)); p.setPen(Qt.PenStyle.NoPen)
    for y in [6, 22, 38, 54, 70, 86]:
        p.drawRoundedRect(5, y, 10, 12, 2, 2)
        p.drawRoundedRect(CARD_W - 15, y, 10, 12, 2, 2)
    p.setBrush(QColor(t.bg_raised))
    p.drawRect(20, 6, CARD_W - 40, CARD_H - 12)
    tri_color = QColor(t.hdr_badge) if is_hdr else QColor(t.accent)
    p.setBrush(tri_color); p.setPen(Qt.PenStyle.NoPen)
    cx, cy = CARD_W // 2, CARD_H // 2
    poly = QPolygon([QPoint(cx-12, cy-16), QPoint(cx-12, cy+16), QPoint(cx+18, cy)])
    p.drawPolygon(poly)
    if is_hdr:
        p.setPen(QColor(t.hdr_badge))
        p.setFont(QFont("JetBrains Mono", 7, QFont.Weight.Bold))
        p.drawText(CARD_W - 34, 10, "HDR")
    p.end()
    return pm


# ── Icon card ──────────────────────────────────────────────────────────────

class IconCard(QWidget):
    clicked        = pyqtSignal(object)
    double_clicked = pyqtSignal(object)
    delete_requested = pyqtSignal(object)
    _thumb_ready   = pyqtSignal(object)

    def __init__(self, label: str, sublabel: str = "", sublabel2: str = "",
                 data=None, pixmap: QPixmap = None, is_folder: bool = False,
                 duration: str = "", size_mb: float = 0, clip_type: str = ""):
        super().__init__()
        self.data      = data
        self.is_folder = is_folder
        self._selected = False

        if is_folder:
            w = FOLDER_W + 24
            h = FOLDER_H + 52
        else:
            # Clip cards: thumbnail fills full card, text overlaid on bottom
            w = CARD_W + 16
            h = CARD_H + 16

        self.setFixedSize(w, h)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self._thumb_ready.connect(self._apply_thumb)

        if is_folder:
            # Folder: keep original vertical layout
            layout = QVBoxLayout(self)
            layout.setContentsMargins(8, 8, 8, 8)
            layout.setSpacing(3)
            layout.setAlignment(Qt.AlignmentFlag.AlignHCenter)

            self._icon = QLabel()
            self._icon.setFixedSize(FOLDER_W, FOLDER_H)
            self._icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
            t = _theme.current
            self._icon.setStyleSheet(f"background:{t.bg_raised}; border-radius:3px;")
            if pixmap:
                self._icon.setPixmap(pixmap.scaled(
                    FOLDER_W, FOLDER_H,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation))
            layout.addWidget(self._icon)

            self._sub = QLabel(sublabel)
            self._sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._sub.setStyleSheet(f"color: {t.text}; font-size: 12px; font-weight: bold;")
            layout.addWidget(self._sub)

            self._lbl = QLabel(label)
            self._lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._lbl.setStyleSheet(f"color: {t.text_dim}; font-size: 10px;")
            layout.addWidget(self._lbl)

            # Delete button — top-right, absolute, visible on hover only
            self._del_btn = QPushButton("X", self)
            self._del_btn.setGeometry(12, 12, 24, 24)
            self._del_btn.setStyleSheet(
                "QPushButton {"
                "  color: #ffffff; font-size: 13px; font-weight: bold;"
                "  background: rgba(180,40,40,200);"
                "  border: none; border-radius: 4px; padding: 0;"
                "}"
                "QPushButton:hover { background: rgba(220,60,60,230); }")
            self._del_btn.setVisible(False)
            self._del_btn.clicked.connect(
                lambda: self.delete_requested.emit(self.data))

        else:
            # Clip card: use absolute positioning so thumbnail fills entire card
            # Text overlay sits on a gradient at the bottom
            t = _theme.current
            self._icon = QLabel(self)
            self._icon.setGeometry(8, 8, CARD_W, CARD_H)
            self._icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._icon.setStyleSheet(
                f"background:{t.bg_deep}; border-radius:6px;")
            if pixmap:
                self._icon.setPixmap(pixmap.scaled(
                    CARD_W, CARD_H,
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation))

            # Async thumbnail
            if data is not None and hasattr(data, "path"):
                import hashlib
                post   = getattr(data, "post_event_secs", -1)
                dur    = getattr(data, "duration_secs", 0.0)
                is_hdr = getattr(data, "is_hdr", False)
                # Fall back to 30s if clip not yet probed — seek lands near the event
                # and the cache key matches once the real duration is known (~30s rounds same)
                effective_dur = dur if dur > 0 else 30.0
                seek_sec = max(1, effective_dur - post) if post >= 0 else -1
                actual_seek = int(seek_sec) if seek_sec >= 0 else 1
                cache_key = f"{data.path}@{actual_seek}@{CARD_W}@{'hdr' if is_hdr else 'sdr'}"
                cached = _THUMB_DIR / f"{hashlib.md5(cache_key.encode()).hexdigest()}.jpg"
                if cached.exists():
                    # Already cached — set immediately, no spinner needed
                    from PyQt6.QtGui import QPixmap as _QP
                    pm = _QP(str(cached))
                    if not pm.isNull():
                        self._icon.setPixmap(pm.scaled(
                            CARD_W, CARD_H,
                            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                            Qt.TransformationMode.SmoothTransformation))
                else:
                    self._spinner = _Spinner(32, self)
                    self._spinner.move(8 + (CARD_W - 32) // 2, 8 + (CARD_H - 32) // 2)
                    self._spinner.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
                    def _load(p=data.path, s=seek_sec, hdr=is_hdr):
                        pm = _get_thumbnail(p, size=CARD_W, event_offset=int(s), is_hdr=hdr)
                        self._thumb_ready.emit(pm)  # always emit so spinner is cleared
                    _thumb_queue.put(_load)

            # Gradient overlay at bottom
            self._overlay = QLabel(self)
            self._overlay.setGeometry(8, 8, CARD_W, CARD_H)
            self._overlay.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            self._overlay.setStyleSheet(
                "background: qlineargradient("
                "x1:0, y1:0, x2:0, y2:1,"
                "stop:0 rgba(0,0,0,0),"
                "stop:0.45 rgba(0,0,0,0),"
                "stop:1 rgba(0,0,0,210));"
                "border-radius: 6px;")

            # Text block — bottom left of thumbnail
            TEXT_PAD = 10
            TEXT_H   = 72
            text_w   = CARD_W - TEXT_PAD * 2
            text_y   = CARD_H + 8 - TEXT_H

            # Trigger pill — sized to text, pill background, outline text
            self._lbl = QLabel(label, self)
            self._lbl.setFont(QFont("", 11, QFont.Weight.Bold))
            fm = self._lbl.fontMetrics()
            pill_w = min(fm.horizontalAdvance(label) + 20, text_w)
            pill_h = 24
            self._lbl.setGeometry(8 + TEXT_PAD, text_y, pill_w, pill_h)
            self._lbl.setWordWrap(False)
            self._lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._lbl.setStyleSheet(
                "color: #ffffff; font-size: 11px; font-weight: bold;"
                "background: rgba(0,0,0,160);"
                "border: 1px solid rgba(255,255,255,60);"
                "border-radius: 4px;"
                "padding: 0px 6px;")

            # Map + mode row
            meta_parts = []
            if sublabel:
                meta_parts.append(sublabel)
            if sublabel2:
                meta_parts.append(sublabel2)
            meta_text = "  ·  ".join(meta_parts)
            self._map_lbl = QLabel(meta_text, self)
            self._map_lbl.setGeometry(8 + TEXT_PAD, text_y + 26, text_w, 18)
            self._map_lbl.setStyleSheet(
                f"color: {t.text}; font-size: 12px; background: transparent;")

            # Footer row — duration, size, clip type
            footer_parts = []
            if duration:
                footer_parts.append(duration)
            if size_mb > 0:
                footer_parts.append(f"{size_mb:.0f} MB")
            if clip_type:
                footer_parts.append(clip_type.upper())
            footer_text = "  ·  ".join(footer_parts)
            self._footer_lbl = QLabel(footer_text, self)
            self._footer_lbl.setGeometry(8 + TEXT_PAD, text_y + 46, text_w, 16)
            self._footer_lbl.setStyleSheet(
                f"color: {t.text_dim}; font-size: 11px; background: transparent;")

            # HDR badge — top right
            if getattr(data, "is_hdr", False):
                hdr_lbl = QLabel("HDR", self)
                hdr_lbl.setGeometry(8 + CARD_W - 44, 16, 36, 16)
                hdr_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                hdr_lbl.setStyleSheet(
                    f"color: {t.hdr_badge}; font-size: 9px; font-weight: bold;"
                    f"background: rgba(60,0,80,180); border-radius: 3px;")

            # Delete button — top-left corner, visible on hover only
            self._del_btn = QPushButton("X", self)
            self._del_btn.setGeometry(12, 12, 24, 24)
            self._del_btn.setStyleSheet(
                "QPushButton {"
                "  color: #ffffff; font-size: 13px; font-weight: bold;"
                "  background: rgba(180,40,40,200);"
                "  border: none; border-radius: 4px;"
                "  padding: 0;"
                "}"
                "QPushButton:hover { background: rgba(220,60,60,230); }")
            self._del_btn.setVisible(False)
            self._del_btn.clicked.connect(self._on_del_clicked)

            # Raise spinner above all other widgets now that they're all created
            if hasattr(self, "_spinner"):
                self._spinner.raise_()

        self._update_style()

    def set_sublabel(self, t: str): self._sub.setText(t)
    def set_selected(self, v: bool):
        self._selected = v; self._update_style()

    def _update_style(self):
        self.update()  # trigger repaint

    def enterEvent(self, e):
        self._hovered = True
        self.update()
        if hasattr(self, "_del_btn"):
            self._del_btn.setVisible(True)

    def leaveEvent(self, e):
        self._hovered = False
        self.update()
        if hasattr(self, "_del_btn"):
            self._del_btn.setVisible(False)

    def _on_del_clicked(self):
        from PyQt6.QtWidgets import QMessageBox
        clip = self.data
        name = clip.path.name if hasattr(clip, "path") else str(clip)
        reply = QMessageBox.question(
            self, "Delete Clip",
            f"Delete {name}?\n\nThis will move the file to trash.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            import send2trash
            send2trash.send2trash(str(clip.path))
        except ImportError:
            clip.path.unlink()
        except Exception as e:
            QMessageBox.warning(self, "Delete Failed", str(e))
            return
        _delete_clip_thumbnails(clip)
        self.delete_requested.emit(clip)

    def paintEvent(self, e):
        super().paintEvent(e)
        from PyQt6.QtGui import QPainter, QPen, QColor
        from PyQt6.QtCore import QRectF
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        t = _theme.current
        if self._selected:
            color, width = QColor(t.accent), 3
        elif getattr(self, "_hovered", False):
            color, width = QColor(t.text_dim), 3
        elif self.is_folder:
            color, width = QColor(t.border), 2
        else:
            painter.end()
            return  # no border for unselected clip cards
        pen = QPen(color, width)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        r = QRectF(self.rect()).adjusted(width/2, width/2, -width/2, -width/2)
        painter.drawRoundedRect(r, 8, 8)
        painter.end()

    def _apply_thumb(self, pm: QPixmap):
        if pm is not None:
            if self.is_folder:
                self._icon.setPixmap(pm.scaled(
                    FOLDER_W, FOLDER_H,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation))
            else:
                self._icon.setPixmap(pm.scaled(
                    CARD_W, CARD_H,
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation))
        if hasattr(self, "_spinner"):
            self._spinner.stop()
            self._spinner.hide()

    def mousePressEvent(self, e):      self.clicked.emit(self.data)
    def mouseDoubleClickEvent(self, e): self.double_clicked.emit(self.data)


# ── Grid view ──────────────────────────────────────────────────────────────

class GridView(QWidget):
    """Scrollable icon grid with breadcrumb navigation."""
    clip_selected = pyqtSignal(object)   # Clip

    def __init__(self, config):
        super().__init__()
        self.config = config
        self._clips: list[Clip] = []
        self._nav: list = []
        self._selected_card: IconCard | None = None
        self._sort_key     = "date"
        self._sort_reverse = True   # newest first

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Toolbar
        t = _theme.current
        toolbar = QWidget()
        toolbar.setStyleSheet(f"background:{t.bg_base};border-bottom:1px solid {t.border};")
        tl = QHBoxLayout(toolbar)
        tl.setContentsMargins(16, 10, 16, 10)
        tl.setSpacing(10)

        self._back_btn = QPushButton("← BACK")
        self._back_btn.setEnabled(False)
        self._back_btn.clicked.connect(self._go_back)
        self._back_btn.setStyleSheet(
            f"background:{t.bg_raised};color:{t.text_dim};border:1px solid {t.border};padding:6px 14px;")

        self._breadcrumb = QLabel("All Games")
        self._breadcrumb.setStyleSheet(f"color:{t.text_dim};font-size:11px;letter-spacing:1px;")

        self._count_lbl = QLabel("")
        self._count_lbl.setStyleSheet(f"color:{t.text_faint};font-size:11px;")

        self._refresh_btn = QPushButton("↻  REFRESH")
        self._refresh_btn.setStyleSheet(
            f"background:{t.bg_raised};color:{t.text};border:1px solid {t.border};padding:6px 14px;")

        self._open_btn = QPushButton("OPEN FOLDER")
        self._open_btn.clicked.connect(self._open_current_folder)
        self._open_btn.setStyleSheet(
            f"background:{t.bg_raised};color:{t.text};border:1px solid {t.border};padding:6px 14px;")

        tl.addWidget(self._back_btn)
        tl.addWidget(self._breadcrumb)
        tl.addStretch()
        tl.addWidget(self._count_lbl)
        tl.addWidget(self._refresh_btn)
        tl.addWidget(self._open_btn)
        layout.addWidget(toolbar)

        # Sort bar — shown only at clip level
        # Sort bar — rebuilt dynamically when entering a clip folder
        self._sort_bar = QWidget()
        self._sort_bar.setStyleSheet(f"background:{t.bg_deep}; border-bottom:1px solid {t.border};")
        self._sort_bar.setVisible(False)
        self._sort_bar_layout = QHBoxLayout(self._sort_bar)
        self._sort_bar_layout.setContentsMargins(16, 6, 16, 6)
        self._sort_bar_layout.setSpacing(8)
        self._sort_btns = {}
        self._sort_dir_btn = None
        layout.addWidget(self._sort_bar)

        # Scroll area + grid
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setStyleSheet(f"QScrollArea{{border:none;background:{t.bg_deep};}}")

        self._grid_widget = QWidget()
        self._grid_widget.setStyleSheet(f"background:{t.bg_deep};")
        self._grid = QGridLayout(self._grid_widget)
        self._grid.setSpacing(16)
        self._grid.setContentsMargins(24, 24, 24, 24)
        self._grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._scroll.setWidget(self._grid_widget)
        layout.addWidget(self._scroll, 1)

    def _set_sort(self, key: str):
        self._sort_key = key
        for k, btn in self._sort_btns.items():
            btn.setChecked(k == key)
        self._update_sort_dir_label()
        if len(self._nav) == 2:
            self._render_clips()

    def _update_sort_dir_label(self):
        if not self._sort_dir_btn:
            return
        if self._sort_key == "date":
            label = "Newest first" if self._sort_reverse else "Oldest first"
        else:
            label = "Z → A" if self._sort_reverse else "A → Z"
        self._sort_dir_btn.setText(label)

    def _toggle_sort_dir(self):
        self._sort_reverse = not self._sort_reverse
        self._update_sort_dir_label()
        if len(self._nav) == 2:
            self._render_clips()

    def _build_sort_bar(self, clips: list):
        """Rebuild sort bar based on which metadata fields exist in these clips."""
        # Clear existing buttons
        for btn in self._sort_btns.values():
            btn.deleteLater()
        self._sort_btns.clear()
        if self._sort_dir_btn:
            self._sort_dir_btn.deleteLater()
            self._sort_dir_btn = None
        # Clear layout items (keep none)
        while self._sort_bar_layout.count():
            item = self._sort_bar_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        tt = _theme.current
        sort_lbl = QLabel("Sort:")
        sort_lbl.setStyleSheet(f"color:{tt.text_faint};font-size:11px;")
        self._sort_bar_layout.addWidget(sort_lbl)

        # Date is always available
        available = [("date", "Date")]

        # Only add fields that have actual data in these clips
        if any(c.map_name for c in clips):
            available.append(("map", "Map"))
        if any(c.mode for c in clips):
            available.append(("mode", "Mode"))
        if any(c.triggers for c in clips):
            available.append(("trigger", "Trigger"))
        if any(c.team for c in clips):
            available.append(("team", "Team"))
        if any(c.score for c in clips):
            available.append(("score", "Score"))

        BTN_STYLE = (
            f"QPushButton{{background:{tt.bg_raised};color:{tt.text_dim};border:1px solid {tt.border};"
            f"padding:3px 10px;font-size:11px;border-radius:3px;}}"
            f"QPushButton:checked{{color:{tt.accent};border-color:{tt.accent};}}"
        )
        DIR_STYLE = (
            f"QPushButton{{background:{tt.bg_overlay};color:{tt.warning};border:1px solid {tt.warning};"
            f"padding:3px 10px;font-size:11px;border-radius:3px;}}"
            f"QPushButton:hover{{color:{tt.accent_hover};}}"
        )

        for key, label in available:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setChecked(key == self._sort_key)
            btn.setStyleSheet(BTN_STYLE)
            btn.clicked.connect(lambda _, k=key: self._set_sort(k))
            self._sort_btns[key] = btn
            self._sort_bar_layout.addWidget(btn)

        # Ensure sort key is valid for these clips
        if self._sort_key not in self._sort_btns:
            self._sort_key = "date"
            if "date" in self._sort_btns:
                self._sort_btns["date"].setChecked(True)

        # Direction button
        self._sort_dir_btn = QPushButton("")
        self._sort_dir_btn.setStyleSheet(DIR_STYLE)
        self._sort_dir_btn.clicked.connect(self._toggle_sort_dir)
        self._sort_bar_layout.addWidget(self._sort_dir_btn)
        self._update_sort_dir_label()

        self._sort_bar_layout.addStretch()

    def connect_refresh(self, fn):
        self._refresh_btn.clicked.connect(fn)

    def load(self, clips: list[Clip]):
        self._clips = clips
        self._count_lbl.setText(f"{len(clips)} clips")
        self._nav = []
        self._show_games()

    def update_clips(self, clips: list[Clip]):
        self._clips = clips
        self._count_lbl.setText(f"{len(clips)} clips")
        self._refresh_view()

    def _refresh_view(self):
        if not self._nav:            self._show_games()
        elif len(self._nav) == 1:    self._show_dates(self._nav[0])
        elif len(self._nav) == 2:    self._show_clips(*self._nav)

    def _show_games(self):
        self._nav = []
        self._breadcrumb.setText("All Games")
        self._back_btn.setEnabled(False)
        self._sort_bar.setVisible(False)
        games = {}
        for c in self._clips:
            games.setdefault(c.game, 0); games[c.game] += 1
        cards = []
        for g, cnt in sorted(games.items()):
            card = IconCard(g, f"{cnt} clips", data=g,
                            pixmap=_folder_pixmap(g, _theme.current.accent), is_folder=True)
            card.clicked.connect(lambda g: self._show_dates(g))
            card.delete_requested.connect(self._on_delete_folder)
            cards.append(card)
        self._populate(cards)

    def _show_dates(self, game: str):
        self._nav = [game]
        self._breadcrumb.setText(f"All Games  ›  {game}")
        self._back_btn.setEnabled(True)
        self._sort_bar.setVisible(False)
        dates = {}
        for c in self._clips:
            if c.game == game:
                dates.setdefault(c.date, 0); dates[c.date] += 1
        cards = []
        for d, cnt in sorted(dates.items(), reverse=True):
            card = IconCard(d, f"{cnt} clips", data=(game, d),
                            pixmap=_folder_pixmap(d, _theme.current.track_chat), is_folder=True)
            card.clicked.connect(lambda tup: self._show_clips(*tup))
            card.delete_requested.connect(self._on_delete_folder)
            cards.append(card)
        self._populate(cards)

    def _show_clips(self, game: str, date: str):
        self._nav = [game, date]
        self._breadcrumb.setText(f"All Games  ›  {game}  ›  {date}")
        self._back_btn.setEnabled(True)
        self._sort_bar.setVisible(True)
        clips_here = [c for c in self._clips if c.game == game and c.date == date]
        self._build_sort_bar(clips_here)
        self._render_clips(game, date)

    def _render_clips(self, game: str = None, date: str = None):
        """Render clip cards with current sort. Uses self._nav if args omitted."""
        if game is None and len(self._nav) == 2:
            game, date = self._nav
        if game is None:
            return
        clips = [c for c in self._clips if c.game == game and c.date == date]

        # Apply sort
        sort_key = self._sort_key
        reverse  = self._sort_reverse
        def _mtime(c):
            try:
                return c.path.stat().st_mtime
            except Exception:
                return 0.0

        SORT_KEYS = {
            "date":    lambda c: (_mtime(c),),
            "map":     lambda c: (c.map_name    or "zzz", _mtime(c)),
            "mode":    lambda c: (c.mode_display or "zzz", _mtime(c)),
            "trigger": lambda c: (c.trigger_display,       _mtime(c)),
            "team":    lambda c: (c.team         or "zzz", _mtime(c)),
            "score":   lambda c: (c.score        or "zzz", _mtime(c)),
        }
        key_fn = SORT_KEYS.get(sort_key, SORT_KEYS["date"])
        clips.sort(key=key_fn, reverse=reverse)

        cards = []
        for clip in clips:
            # Build footer: duration, size, clip type badge
            type_label = {
                "s":  "single",
            }.get(clip.clip_type,
                  f"overflow {clip.clip_type[1:]}" if clip.clip_type.startswith("o")
                  else "")
            # Export clips have no GSI metadata in their filenames — show stem directly
            is_export = clip.game.lower() == "exports"
            card = IconCard(
                clip.path.stem if is_export else clip.suggested_export_name,
                "" if is_export else clip.map_display,
                "" if is_export else clip.mode_display,
                data=clip,
                pixmap=None,
                is_folder=False,
                duration=clip.duration_str,
                size_mb=clip.size_mb,
                clip_type=type_label,
            )
            card.clicked.connect(self._on_clip_dblclick)
            card.delete_requested.connect(self._on_delete_clip)
            cards.append(card)
        self._populate(cards)
        self._selected_card = None

    def _open_current_folder(self):
        from pathlib import Path as _P
        parts = [self.config.output_dir] + self._nav
        path = str(_P(*parts))
        subprocess.Popen(["xdg-open", path])

    def _on_delete_folder(self, data):
        from pathlib import Path as _P
        from PyQt6.QtWidgets import QMessageBox
        if isinstance(data, tuple):
            game, date = data
            folder = _P(self.config.output_dir) / game / date
            label = f"{game} / {date}"
            after = lambda: self._show_dates(game)
            clip_filter = lambda c: not (c.game == game and c.date == date)
        else:
            game = data
            folder = _P(self.config.output_dir) / game
            label = game
            after = self._show_games
            clip_filter = lambda c: c.game != game

        n = sum(1 for c in self._clips if not clip_filter(c))
        reply = QMessageBox.question(
            self, "Delete Folder",
            f"Delete '{label}' and all {n} clip(s) inside?\n\nThis will move the folder to trash.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        clips_removed = [c for c in self._clips if not clip_filter(c)]
        try:
            import send2trash
            send2trash.send2trash(str(folder))
        except ImportError:
            import shutil
            shutil.rmtree(folder)
        except Exception as e:
            QMessageBox.warning(self, "Delete Failed", str(e))
            return
        for c in clips_removed:
            _delete_clip_thumbnails(c)
        self._clips = [c for c in self._clips if clip_filter(c)]
        after()

    def _on_delete_clip(self, clip):
        self._clips = [c for c in self._clips if c.path != clip.path]
        self._show_clips(*self._nav)

    def _go_back(self):
        if len(self._nav) == 2:   self._show_dates(self._nav[0])
        elif len(self._nav) == 1: self._show_games()

    def _populate(self, cards: list):
        while self._grid.count():
            item = self._grid.takeAt(0)
            if item.widget(): item.widget().deleteLater()
        if not cards:
            lbl = QLabel("No clips found")
            lbl.setStyleSheet(f"color:{_theme.current.text_faint};font-size:14px;")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._grid.addWidget(lbl, 0, 0)
            return
        cw = FOLDER_W + 24 if cards[0].is_folder else CARD_W + 24
        cols = max(1, (self._scroll.width() - 48) // (cw + 16))
        for i, card in enumerate(cards):
            self._grid.addWidget(card, i // cols, i % cols)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        QTimer.singleShot(60, self._refresh_view)

    def _on_clip_click(self, clip: Clip):
        if self._selected_card:
            self._selected_card.set_selected(False)
        for i in range(self._grid.count()):
            w = self._grid.itemAt(i).widget()
            if w and hasattr(w, 'data') and w.data is clip:
                self._selected_card = w
                w.set_selected(True)
                break
        self.clip_selected.emit(clip)

    def _on_clip_dblclick(self, clip: Clip):
        self.clip_selected.emit(clip)


# ── Player view ────────────────────────────────────────────────────────────

class _Spinner(QWidget):
    """Simple animated spinner drawn with QPainter."""
    def __init__(self, size=40, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(30)

    def _tick(self):
        self._angle = (self._angle + 12) % 360
        self.update()

    def paintEvent(self, e):
        from PyQt6.QtGui import QPainter, QPen, QColor
        from PyQt6.QtCore import QRectF
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(4, 4, self.width()-8, self.height()-8)
        # Track
        p.setPen(QPen(QColor(_theme.current.border), 4))
        p.drawEllipse(r)
        # Arc
        pen = QPen(QColor(_theme.current.accent), 4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawArc(r, -self._angle * 16, 100 * 16)
        p.end()

    def stop(self):
        self._timer.stop()


class ExportDialog(QDialog):
    """
    Export dialog with three states:
    1. Settings — basic/advanced options
    2. Encoding — spinner + live status
    3. Done     — success/fail + open folder button
    """
    # Thread-safe signal for status updates from encode thread
    _progress_sig = pyqtSignal(str, float)   # msg, pct (-1 = phase label)
    _done_sig      = pyqtSignal(bool, object)  # success, Path

    PLATFORM_LIMITS = [
        ("No limit",                  None),
        ("10 MB  ·  Discord (Free)",  10),
        ("25 MB  ·  WhatsApp",        25),
        ("50 MB  ·  Telegram",        50),
        ("100 MB  ·  iMessage",       100),
        ("500 MB  ·  Discord Nitro",  500),
        ("512 MB  ·  Twitter / X",    512),
        ("Custom...",                 -1),
    ]

    RESOLUTIONS = [
        ("Source", None),
        ("1080p",  1080),
        ("720p",   720),
        ("480p",   480),
    ]

    FRAMERATES = [
        ("Source", None),
        ("60 fps", 60),
        ("30 fps", 30),
        ("24 fps", 24),
    ]

    def __init__(self, clip, suggested_name, in_s, out_s, trimming, config,
                 on_export, parent=None):
        """
        on_export: callable(settings, on_progress, on_done)
                   called when user hits Export. Runs the encode in background.
        """
        super().__init__(parent)
        self._clip      = clip
        self._in_s      = in_s
        self._out_s     = out_s
        self._config    = config
        self._on_export = on_export
        self._advanced  = False
        self._out_path  = None

        self.setWindowTitle("Export Clip")
        self.setFixedWidth(540)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, True)
        _t = _theme.current
        self.setStyleSheet(f"""
            QDialog  {{ background: {_t.bg_base}; }}
            QLabel   {{ color: {_t.text}; }}
            QLineEdit, QComboBox {{
                background: {_t.bg_raised}; border: 1px solid {_t.border};
                color: {_t.text}; padding: 5px 8px; border-radius: 3px;
            }}
            QGroupBox {{
                color: {_t.text_dim}; font-size: 11px; letter-spacing: 1px;
                border: 1px solid {_t.border}; border-radius: 4px;
                margin-top: 8px; padding-top: 12px;
            }}
            QGroupBox::title {{ subcontrol-origin: margin; left: 8px; }}
            QCheckBox, QRadioButton {{ color: {_t.text}; padding: 4px 0; }}
            QPushButton {{
                background: {_t.bg_raised}; color: {_t.text};
                border: 1px solid {_t.border}; border-radius: 4px;
                padding: 6px 16px; font-size: 12px;
            }}
            QPushButton:hover  {{ background: {_t.bg_overlay}; }}
            QPushButton#primary {{
                background: {_t.accent}; color: {_t.accent_text};
                border-color: {_t.accent}; font-weight: bold;
            }}
            QPushButton#primary:hover {{ background: {_t.accent_hover}; }}
        """)

        self._stack = QStackedWidget()
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self._stack)

        self._stack.addWidget(self._build_settings_page(suggested_name, in_s, out_s, trimming, clip, config))
        self._stack.addWidget(self._build_encoding_page())
        self._stack.addWidget(self._build_done_page())

        self._progress_sig.connect(self._on_progress)
        self._done_sig.connect(self._on_done)
        self._stack.setCurrentIndex(0)
        self.adjustSize()

    # ── Page builders ──────────────────────────────────────────────────────

    def _build_settings_page(self, suggested_name, in_s, out_s, trimming, clip, config):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setSpacing(12)
        lay.setContentsMargins(20, 20, 20, 20)

        # Filename
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Clip Name:"))
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText(suggested_name)
        name_row.addWidget(self._name_edit)
        lay.addLayout(name_row)

        # ── Basic section ─────────────────────────────────────────────────
        self._basic_widget = QWidget()
        bl = QVBoxLayout(self._basic_widget)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(10)

        # Preset
        preset_g = QGroupBox("PRESET")
        pl = QVBoxLayout(preset_g)
        pl.setSpacing(4)
        self._preset_btns = []
        for key, label, desc in [
            ("keep",   "Keep original quality",
             "Lossless copy — no re-encoding, HDR preserved. Best for archiving or editing."),
            ("share",  "Share online",
             "H.264, SDR, MP4. Works everywhere — Discord, YouTube, Twitter, Reddit."),
            ("editor", "Edit in video editor",
             "Original quality in MP4 container for editor compatibility."),
        ]:
            rb = QRadioButton(label)
            rb.setProperty("preset_key", key)
            rb.setChecked(key == "share")
            rb.setStyleSheet("font-weight:bold;")
            dl = QLabel(desc)
            dl.setStyleSheet(f"color:{_theme.current.text_faint};font-size:10px;padding-left:22px;")
            dl.setWordWrap(True)
            vl = QVBoxLayout(); vl.setSpacing(1)
            vl.addWidget(rb); vl.addWidget(dl)
            pl.addLayout(vl)
            self._preset_btns.append(rb)
        bl.addWidget(preset_g)

        # Resolution
        res_g = QGroupBox("RESOLUTION")
        rl = QHBoxLayout(res_g)
        self._res_combo = QComboBox()
        for label, _ in self.RESOLUTIONS:
            self._res_combo.addItem(label)
        rl.addWidget(self._res_combo)
        rl.addStretch()
        bl.addWidget(res_g)

        # Framerate
        fps_g = QGroupBox("FRAMERATE")
        fl = QHBoxLayout(fps_g)
        self._fps_combo = QComboBox()
        for label, _ in self.FRAMERATES:
            self._fps_combo.addItem(label)
        fl.addWidget(self._fps_combo)
        fl.addStretch()
        bl.addWidget(fps_g)

        # File size limit
        size_g = QGroupBox("FILE SIZE LIMIT")
        size_vl = QVBoxLayout(size_g)
        size_info = QLabel(
            "Set a file size target to make clips sharable on platforms with upload limits. "
            "The clip will be re-encoded to fit — lower limits mean lower quality.")
        size_info.setWordWrap(True)
        size_info.setStyleSheet(f"color:{_theme.current.text_faint};font-size:10px;padding-bottom:4px;")
        size_vl.addWidget(size_info)
        size_row = QHBoxLayout()
        self._size_combo = QComboBox()
        for label, _ in self.PLATFORM_LIMITS:
            self._size_combo.addItem(label)
        self._size_combo.currentIndexChanged.connect(self._on_size_changed)
        self._custom_mb = QLineEdit()
        self._custom_mb.setPlaceholderText("MB")
        self._custom_mb.setFixedWidth(70)
        self._custom_mb.hide()
        size_row.addWidget(self._size_combo)
        size_row.addWidget(self._custom_mb)
        size_row.addStretch()
        size_vl.addLayout(size_row)
        bl.addWidget(size_g)

        # Trim
        trim_g = QGroupBox("TRIM")
        tl = QHBoxLayout(trim_g)
        self._trim_check = QCheckBox(f"Use in/out points  ({in_s:.2f}s → {out_s:.2f}s)")
        self._trim_check.setChecked(trimming)
        tl.addWidget(self._trim_check)
        bl.addWidget(trim_g)

        lay.addWidget(self._basic_widget)

        # Advanced toggle
        adv_row = QHBoxLayout()
        self._adv_btn = QPushButton("Advanced ▾")
        self._adv_btn.setFlat(True)
        self._adv_btn.setStyleSheet(f"color:{_theme.current.text_faint};font-size:11px;background:transparent;border:none;")
        self._adv_btn.clicked.connect(self._toggle_advanced)
        adv_row.addStretch()
        adv_row.addWidget(self._adv_btn)
        lay.addLayout(adv_row)

        # Advanced section
        self._adv_widget = QWidget()
        self._adv_widget.hide()
        al = QVBoxLayout(self._adv_widget)
        al.setContentsMargins(0, 0, 0, 0)
        al.setSpacing(10)

        codec_g = QGroupBox("CODEC")
        cl2 = QHBoxLayout(codec_g)
        self._codec_combo = QComboBox()
        for c in ["Copy (lossless)", "H.264", "HEVC (H.265)"]:
            self._codec_combo.addItem(c)
        cl2.addWidget(self._codec_combo)
        cl2.addStretch()
        al.addWidget(codec_g)

        if clip.is_hdr:
            sdr_g = QGroupBox("COLOR")
            sdr_l = QHBoxLayout(sdr_g)
            self._sdr_check = QCheckBox("Convert HDR to SDR")
            self._sdr_check.setChecked(False)
            sdr_l.addWidget(self._sdr_check)
            al.addWidget(sdr_g)
        else:
            self._sdr_check = None

        dest_g = QGroupBox("DESTINATION")
        dest_l = QHBoxLayout(dest_g)
        cfg_exports = getattr(config, "exports_dir", "")
        default_dest = str(Path(cfg_exports) / clip.game) if cfg_exports else \
                       str(clip.path.parent.parent.parent / "Exports" / clip.game)
        self._dest_edit = QLineEdit(default_dest)
        browse_btn = QPushButton("Browse")
        browse_btn.clicked.connect(self._browse_dest)
        dest_l.addWidget(self._dest_edit)
        dest_l.addWidget(browse_btn)
        al.addWidget(dest_g)

        lay.addWidget(self._adv_widget)

        # Buttons
        btn_row = QHBoxLayout()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        self._export_btn = QPushButton("Export")
        self._export_btn.setObjectName("primary")
        self._export_btn.setFixedWidth(100)
        self._export_btn.clicked.connect(self._start_export)
        btn_row.addStretch()
        btn_row.addWidget(cancel_btn)
        btn_row.addSpacing(8)
        btn_row.addWidget(self._export_btn)
        lay.addLayout(btn_row)

        return page

    def _build_encoding_page(self):
        from PyQt6.QtWidgets import QProgressBar
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(20, 32, 20, 32)
        lay.setSpacing(12)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._spinner = _Spinner(48)
        spinner_row = QHBoxLayout()
        spinner_row.addStretch()
        spinner_row.addWidget(self._spinner)
        spinner_row.addStretch()
        lay.addLayout(spinner_row)

        self._enc_status = QLabel("Starting...")
        self._enc_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._enc_status.setStyleSheet(f"color:{_theme.current.text_dim};font-size:12px;")
        self._enc_status.setWordWrap(True)
        lay.addWidget(self._enc_status)

        self._enc_progress = QProgressBar()
        self._enc_progress.setRange(0, 100)
        self._enc_progress.setValue(0)
        self._enc_progress.setTextVisible(False)
        self._enc_progress.setFixedHeight(6)
        _ep = _theme.current
        self._enc_progress.setStyleSheet(f"""
            QProgressBar {{
                background: {_ep.bg_raised}; border: 1px solid {_ep.border}; border-radius: 3px;
            }}
            QProgressBar::chunk {{ background: {_ep.accent}; border-radius: 3px; }}
        """)
        self._enc_progress.hide()
        lay.addWidget(self._enc_progress)

        self._enc_detail = QLabel("")
        self._enc_detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._enc_detail.setStyleSheet(f"color:{_theme.current.text_faint};font-size:11px;")
        lay.addWidget(self._enc_detail)

        return page

    def _build_done_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(20, 32, 20, 24)
        lay.setSpacing(12)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)

        _d = _theme.current
        self._done_icon = QLabel("✓")
        self._done_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._done_icon.setStyleSheet(f"color:{_d.success};font-size:40px;")
        lay.addWidget(self._done_icon)

        self._done_title = QLabel("Export complete")
        self._done_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._done_title.setStyleSheet(f"color:{_d.text};font-size:15px;font-weight:bold;")
        lay.addWidget(self._done_title)

        self._done_detail = QLabel("")
        self._done_detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._done_detail.setStyleSheet(f"color:{_d.text_dim};font-size:11px;")
        self._done_detail.setWordWrap(True)
        lay.addWidget(self._done_detail)

        lay.addSpacing(8)

        btn_row = QHBoxLayout()
        self._open_folder_btn = QPushButton("Open Folder")
        self._open_folder_btn.setObjectName("primary")
        self._open_folder_btn.clicked.connect(self._open_folder)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_row.addStretch()
        btn_row.addWidget(self._open_folder_btn)
        btn_row.addSpacing(8)
        btn_row.addWidget(close_btn)
        btn_row.addStretch()
        lay.addLayout(btn_row)

        return page

    # ── State transitions ──────────────────────────────────────────────────

    def _start_export(self):
        s = self._collect_settings()
        self._stack.setCurrentIndex(1)
        self._enc_status.setText("Starting...")
        self.setFixedHeight(self.sizeHint().height())

        def _progress(msg, pct=-1.0):
            self._progress_sig.emit(msg, pct)

        def _done(ok, path):
            self._done_sig.emit(ok, path)

        self._on_export(s, _progress, _done)

    def _on_progress(self, msg: str, pct: float):
        if pct >= 0:
            self._enc_progress.setValue(int(pct))
            self._enc_progress.show()
            self._enc_detail.setText(msg)
        else:
            self._enc_status.setText(msg)
            self._enc_detail.clear()

    def _on_done(self, success: bool, path):
        from pathlib import Path as _P
        self._spinner.stop()
        self._out_path = _P(str(path)) if path else None
        self._stack.setCurrentIndex(2)

        _dt = _theme.current
        if success and self._out_path and self._out_path.exists():
            size_mb = self._out_path.stat().st_size / (1024 * 1024)
            self._done_icon.setText("✓")
            self._done_icon.setStyleSheet(f"color:{_dt.success};font-size:40px;")
            self._done_title.setText("Export complete")
            self._done_detail.setText(
                f"{self._out_path.name}\n{size_mb:.1f} MB")
            self._open_folder_btn.setText("Open Folder")
        else:
            self._done_icon.setText("✗")
            self._done_icon.setStyleSheet(f"color:{_dt.error};font-size:40px;")
            self._done_title.setText("Export failed")
            self._done_title.setStyleSheet(f"color:{_dt.error};font-size:15px;font-weight:bold;")
            self._done_detail.setText("Check that ffmpeg is installed and the output folder is writable.")
            self._open_folder_btn.setText("Open Log")

    def _open_folder(self):
        from PyQt6.QtGui import QDesktopServices
        from PyQt6.QtCore import QUrl
        if self._out_path and self._out_path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._out_path.parent)))
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile("/tmp/autoclip.log"))

    # ── Helpers ────────────────────────────────────────────────────────────

    def _on_size_changed(self, idx):
        self._custom_mb.setVisible(self.PLATFORM_LIMITS[idx][1] == -1)

    def _toggle_advanced(self):
        self._advanced = not self._advanced
        if self._advanced and self._sdr_check:
            preset = next((rb.property("preset_key") for rb in self._preset_btns
                           if rb.isChecked()), "share")
            self._sdr_check.setChecked(preset == "share" and self._clip.is_hdr)
        self._adv_widget.setVisible(self._advanced)
        self._adv_btn.setText("Advanced ▴" if self._advanced else "Advanced ▾")
        self.adjustSize()

    def _browse_dest(self):
        folder = QFileDialog.getExistingDirectory(self, "Export Destination",
                                                   self._dest_edit.text())
        if folder:
            self._dest_edit.setText(folder)

    def _collect_settings(self) -> dict:
        name = self._name_edit.text().strip() or \
               self._name_edit.placeholderText() or \
               self._clip.suggested_export_name

        trimming = self._trim_check.isChecked()
        in_s  = self._in_s  if trimming else 0.0
        out_s = self._out_s if trimming else (self._clip.duration_secs or 9999)

        preset = next((rb.property("preset_key") for rb in self._preset_btns
                       if rb.isChecked()), "share")
        do_sdr = (self._sdr_check.isChecked() if self._advanced and self._sdr_check
                  else preset == "share" and self._clip.is_hdr)

        size_mb = None
        idx = self._size_combo.currentIndex()
        limit = self.PLATFORM_LIMITS[idx][1]
        if limit == -1:
            try: size_mb = float(self._custom_mb.text())
            except ValueError: pass
        elif limit:
            size_mb = float(limit)

        export_dir = Path(self._dest_edit.text() if self._advanced
                         else self._default_export_dir())

        fps_idx = self._fps_combo.currentIndex()
        fps = self.FRAMERATES[fps_idx][1]

        return dict(name=name, do_sdr=do_sdr, in_s=in_s, out_s=out_s,
                    trimming=trimming, size_mb=size_mb, export_dir=export_dir,
                    preset=preset, fps=fps)

    # Keep settings() as alias for _collect_settings for compatibility
    def settings(self) -> dict:
        return self._collect_settings()

    def _default_export_dir(self) -> str:
        cfg_exports = getattr(self._config, "exports_dir", "")
        if cfg_exports:
            return str(Path(cfg_exports) / self._clip.game)
        return str(self._clip.path.parent.parent.parent / "Exports" / self._clip.game)


class PlayerView(QWidget):
    """Full-tab player with embedded mpv, timeline, waveform, trim, export."""
    back_requested  = pyqtSignal()
    export_done     = pyqtSignal()
    # Internal signals for thread-safe UI updates
    _waveform_ready = pyqtSignal(object)   # np.ndarray
    _export_status  = pyqtSignal(str)      # status message
    _export_done_s  = pyqtSignal(bool, object)  # success, Path

    def __init__(self, config):
        super().__init__()
        self._config = config
        self._clip: Clip | None = None
        self._current_markers = []
        self._current_markers = []
        self._player: MpvPlayer | None = None
        self._duration = 0.0
        self._waveform_thread = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Top bar
        _pv = _theme.current
        topbar = QWidget()
        topbar.setStyleSheet(f"background:{_pv.bg_base};border-bottom:1px solid {_pv.border};")
        tl = QHBoxLayout(topbar)
        tl.setContentsMargins(16, 10, 16, 10)

        self._back_btn = QPushButton("← BACK TO CLIPS")
        self._back_btn.setStyleSheet(
            f"background:{_pv.bg_raised};color:{_pv.text};border:1px solid {_pv.border};padding:6px 14px;")
        self._back_btn.clicked.connect(self._on_back)

        self._title_lbl = QLabel("")
        self._title_lbl.setStyleSheet(f"color:{_pv.text};font-size:13px;font-weight:bold;")

        self._info_lbl = QLabel("")
        self._info_lbl.setStyleSheet(f"color:{_pv.text_dim};font-size:11px;")

        tl.addWidget(self._back_btn)
        tl.addSpacing(16)
        tl.addWidget(self._title_lbl)
        tl.addSpacing(12)
        tl.addWidget(self._info_lbl)
        tl.addStretch()
        layout.addWidget(topbar)

        # libmpv OpenGL video widget
        self._video_widget = MpvWidget()
        self._video_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._video_widget.setMinimumHeight(200)
        layout.addWidget(self._video_widget, 1)

        # Loading overlay — transparent label parented to video widget, resizes with it
        self._loading_lbl = QLabel("Loading...", self._video_widget)
        self._loading_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._loading_lbl.setStyleSheet(
            f"background: rgba(0,0,0,0); color: {_pv.accent}; "
            f"font-size: 16px; font-weight: bold;")
        self._loading_lbl.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._loading_lbl.hide()

        # Controls bar
        controls = QWidget()
        controls.setStyleSheet(f"background:{_pv.bg_deep};border-top:1px solid {_pv.border};")
        controls.setMinimumHeight(200)
        cl = QVBoxLayout(controls)
        cl.setContentsMargins(16, 12, 16, 12)
        cl.setSpacing(10)

        # Single action row: play | time | markers | stretch | in | out | export | delete
        pb_row = QHBoxLayout()

        self._play_btn = QPushButton("▶  PLAY")
        self._play_btn.setCheckable(True)
        self._play_btn.setFixedWidth(140)
        self._play_btn.setStyleSheet(
            f"QPushButton {{"
            f"  background:{_pv.bg_raised}; color:{_pv.text};"
            f"  border:1px solid {_pv.border}; border-radius:4px;"
            f"  padding:6px 14px; font-size:12px; font-weight:bold;"
            f"}}"
            f"QPushButton:checked {{"
            f"  background:{_pv.accent}; color:{_pv.accent_text}; border-color:{_pv.accent};"
            f"}}"
            f"QPushButton:disabled {{ color:{_pv.text_faint}; border-color:{_pv.border}; }}"
        )
        self._play_btn.clicked.connect(self._toggle_play)
        self._play_btn.setEnabled(False)

        self._pos_lbl = QLabel("0:00 / 0:00")
        self._pos_lbl.setStyleSheet(f"color:{_pv.text_dim};font-size:11px;min-width:140px;")

        self._markers_toggle = QPushButton("⬥ MARKERS")
        self._markers_toggle.setCheckable(True)
        self._markers_toggle.setChecked(True)
        self._markers_toggle.setFixedWidth(100)
        self._markers_toggle.setStyleSheet(
            f"QPushButton{{background:{_pv.bg_raised};color:{_pv.text_faint};"
            f"border:1px solid {_pv.border};border-radius:3px;"
            f"padding:4px 8px;font-size:10px;font-weight:bold;}}"
            f"QPushButton:checked{{color:{_pv.warning};border-color:{_pv.warning};}}"
        )
        self._markers_toggle.toggled.connect(self._on_markers_toggled)

        self._in_lbl  = QLabel("In: 0.0s")
        self._in_lbl.setStyleSheet(f"color:{_pv.handle_in};font-size:11px;")
        self._out_lbl = QLabel("Out: 0.0s")
        self._out_lbl.setStyleSheet(f"color:{_pv.handle_out};font-size:11px;")

        self._export_btn = QPushButton("EXPORT CLIP")
        self._export_btn.setObjectName("primary")
        self._export_btn.setFixedWidth(130)
        self._export_btn.setEnabled(False)
        self._export_btn.clicked.connect(self._do_export)

        self._delete_btn = QPushButton("🗑  DELETE")
        self._delete_btn.setFixedWidth(100)
        self._delete_btn.setEnabled(False)
        self._delete_btn.setStyleSheet(
            f"QPushButton{{background:{_pv.bg_overlay};color:{_pv.error};border:1px solid {_pv.error};"
            f"padding:6px 12px;font-size:11px;font-weight:bold;}}"
            f"QPushButton:hover{{color:{_pv.text};border-color:{_pv.error};}}"
            f"QPushButton:disabled{{background:{_pv.bg_raised};color:{_pv.text_faint};border-color:{_pv.border};}}"
        )
        self._delete_btn.clicked.connect(self._do_delete)

        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet(f"color:{_pv.text_dim};font-size:11px;")

        pb_row.addWidget(self._play_btn)
        pb_row.addSpacing(12)
        pb_row.addWidget(self._pos_lbl)
        pb_row.addSpacing(8)
        pb_row.addWidget(self._markers_toggle)
        pb_row.addStretch()
        pb_row.addWidget(self._status_lbl)
        pb_row.addSpacing(8)
        pb_row.addWidget(self._in_lbl)
        pb_row.addSpacing(8)
        pb_row.addWidget(self._out_lbl)
        pb_row.addSpacing(16)
        pb_row.addWidget(self._export_btn)
        pb_row.addSpacing(8)
        pb_row.addWidget(self._delete_btn)
        cl.addLayout(pb_row)

        # Timeline (main seek/trim bar)
        self._timeline = TimelineWidget()
        self._timeline.seek_requested.connect(self._on_seek)
        self._timeline.in_point_changed.connect(self._on_in_changed)
        self._timeline.out_point_changed.connect(self._on_out_changed)
        cl.addWidget(self._timeline)

        # Multi-track waveform display — now gets full remaining height
        self._track_waveforms = MultiTrackWaveform()
        self._track_waveforms.track_volume_changed.connect(self._on_track_volume)
        self._track_waveforms.track_mute_changed.connect(self._on_track_mute)
        self._track_waveforms.tracks_probed.connect(self._on_tracks_probed)
        cl.addWidget(self._track_waveforms)

        layout.addWidget(controls)

        # Position label update on timeline drag
        self._pos_timer = QTimer()
        self._pos_timer.setInterval(100)
        self._pos_timer.timeout.connect(self._check_outpoint)
        self._pos_timer.start()

        # Connect internal thread-safe signals
        self._waveform_ready.connect(self._timeline.set_waveform)
        self._export_status.connect(self._status_lbl.setText)
        self._export_done_s.connect(self._on_export_done)

    # ------------------------------------------------------------------ #

    def load(self, clip: Clip):
        self._clip = clip
        self._duration = clip.duration_secs or 1.0

        self._title_lbl.setText(clip.display_name)
        self._info_lbl.setText(
            f"{clip.game}  ·  {clip.date}  ·  {clip.duration_str}  ·  "
            f"{clip.size_mb:.1f} MB  ·  {'HDR' if clip.is_hdr else 'SDR'}"
        )
        self._suggested_name = clip.suggested_export_name
        # Probe clip for HDR/duration if not yet done
        import threading as _t
        def _probe_and_update():
            from ..core.clips import probe_clip
            dur, is_hdr = probe_clip(clip.path)
            if dur > 0: clip.duration_secs = dur
            clip.is_hdr = is_hdr
        _t.Thread(target=_probe_and_update, daemon=True).start()

        # Reset timeline
        self._timeline.set_in(0.0)
        self._timeline.set_out(1.0)
        self._timeline.set_position(0.0)
        self._timeline.set_waveform(__import__("numpy").zeros(800))
        self._update_in_out_labels()

        # Set event marker — will be refined when real duration arrives
        self._timeline.set_markers([])
        self._loading_lbl.show()

        # Reuse existing MpvPlayer — only create once
        if self._player is None:
            self._player = MpvPlayer(
                self._video_widget,
                on_position=self._on_position,
                on_duration=self._on_duration_received,
                on_end=self._on_playback_ended,
            )
        else:
            self._video_widget.stop()
            # Pause immediately so new clip doesn't auto-play

        # Load track waveforms BEFORE player.load() so audio config is ready
        # when mpv opens the file and fires duration event
        tracks = getattr(self._config, "audio_tracks", None) or []
        self._track_waveforms.load_tracks(tracks, clip.path)

        self._player.load(clip.path)
        self._pos_timer.start()

        # Extract waveform in background
        threading.Thread(target=self._extract_waveform, daemon=True).start()

    def _extract_waveform(self):
        if self._clip:
            samples = extract_waveform(self._clip.path)
            self._waveform_ready.emit(samples)

    def _on_markers_toggled(self, checked: bool):
        if checked:
            self._timeline.set_markers(self._current_markers)
        else:
            self._timeline.set_markers([])

    def _do_delete(self):
        if not self._clip:
            return
        name = self._clip.path.name
        msg = f"Permanently delete this clip?\n\n{name}\n\nThis cannot be undone."
        reply = QMessageBox.question(
            self,
            "Delete Clip",
            msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            self._clip.path.unlink()
            logger.info(f"Deleted clip: {name}")
            # Stop player and go back to grid
            if self._player:
                self._player.stop()
            self.back_requested.emit()
            self.export_done.emit()  # triggers grid refresh
        except Exception as e:
            QMessageBox.warning(self, "Delete Failed", f"Could not delete clip:\n{e}")

    def _on_export_done(self, success: bool, path):
        from pathlib import Path as _P
        p = _P(str(path))
        self._export_btn.setEnabled(True)
        if success:
            self._status_lbl.setText(f"✓ {p.name}")
            self.export_done.emit()
        else:
            self._status_lbl.setText("✗ Export failed — check ffmpeg/zscale")

    def _check_outpoint(self):
        """Pause playback when playhead reaches out point, update pos label."""
        self._refresh_pos_label()
        if self._duration > 0 and self._player:
            pos_norm = self._timeline._pos
            if pos_norm >= self._timeline.out_point - 0.005:
                # Explicit pause — never toggle, player may already be paused
                self._player.pause()
                # Only snap back to in point if the user has set a trim range.
                # Without trim, leave the playhead at the end so the clip doesn't
                # appear to restart (pressing play will seek to in point instead).
                has_trim = not (self._timeline.in_point == 0.0 and
                                self._timeline.out_point == 1.0)
                if has_trim:
                    in_norm = self._timeline.in_point
                    self._timeline.set_position(in_norm)
                    if self._player:
                        self._player.seek_norm(in_norm)
                    self._seek_suppress_until = 0.0
                self._update_play_btn()

    def _on_duration_received(self, dur: float):
        self._duration = dur
        clip = self._clip  # capture now — may change by the time _f runs
        def _f():
            self._play_btn.setEnabled(True)
            self._export_btn.setEnabled(True)
            self._delete_btn.setEnabled(True)
            self._status_lbl.setText("")
            self._update_in_out_labels()
            self._loading_lbl.hide()
            self._update_play_btn()
            # Set marker using real duration — position = duration - post_event
            if clip and clip.events and dur > 0:
                markers = []
                for ev in clip.events:
                    trigger_sec = max(0.0, dur - ev.secs_from_end)
                    norm = min(1.0, trigger_sec / dur)
                    label = ev.trigger
                    if ev.weapon:
                        label = f"{ev.trigger} {ev.weapon}"
                    markers.append({
                        "norm":    norm,
                        "label":   label,
                        "trigger": ev.trigger,
                    })
                self._current_markers = markers
                if self._markers_toggle.isChecked():
                    self._timeline.set_markers(markers)
        QTimer.singleShot(0, _f)

    def _on_playback_ended(self):
        """Clip reached the end — pause. _check_outpoint handles in-point rewind for trim."""
        if self._player:
            try:
                self._player.pause()
            except Exception:
                pass
        self._update_play_btn()

    def _on_position(self, pos: float):
        if self._duration > 0:
            # Suppress position updates briefly after a manual seek
            # to prevent the playhead jumping back before mpv catches up
            if hasattr(self, '_seek_suppress_until'):
                import time
                if time.monotonic() < self._seek_suppress_until:
                    return
            norm = pos / self._duration
            QTimer.singleShot(0, lambda n=norm: self._timeline.set_position(n))

    def _refresh_pos_label(self):
        if self._duration > 0:
            pos = self._timeline._pos * self._duration
            self._pos_lbl.setText(
                f"{self._fmt(pos)} / {self._fmt(self._duration)}"
            )
            return pos
        return 0.0

    def _fmt(self, secs: float) -> str:
        m = int(secs) // 60
        s = secs % 60
        return f"{m}:{s:05.2f}"

    def _toggle_play(self):
        if self._player:
            # If paused at or past the out point, restart from in point before unpausing
            try:
                is_paused = self._player._widget._mpv.pause
            except Exception:
                is_paused = False
            if is_paused and self._timeline._pos >= self._timeline.out_point - 0.005:
                import time
                in_norm = self._timeline.in_point
                self._seek_suppress_until = time.monotonic() + 1.5
                self._timeline.set_position(in_norm)
                self._player.seek_norm(in_norm)
            self._player.play_pause()
            self._update_play_btn()

    def _update_play_btn(self):
        """Sync play button label and checked state with mpv pause state."""
        try:
            paused = self._player._widget._mpv.pause if self._player else True
        except Exception:
            paused = True
        self._play_btn.setChecked(not paused)
        self._play_btn.setText("❚❚  PAUSE" if not paused else "▶  PLAY")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_loading_lbl") and self._video_widget:
            self._loading_lbl.setGeometry(self._video_widget.rect())

    def keyPressEvent(self, event):
        """Space = play/pause, Escape = back."""
        from PyQt6.QtCore import Qt as _Qt
        if event.key() == _Qt.Key.Key_Space:
            self._toggle_play()
            event.accept()
        elif event.key() == _Qt.Key.Key_Escape:
            self.back_requested.emit()
            event.accept()
        else:
            super().keyPressEvent(event)

    def _on_seek(self, norm: float):
        import time
        # Suppress incoming position updates for 1.5s after seek
        # so playhead doesn't flicker back while mpv is seeking
        self._seek_suppress_until = time.monotonic() + 1.5
        # Update playhead immediately for responsive feel
        self._timeline.set_position(norm)
        self._pos_lbl.setText(
            f"{self._fmt(norm * self._duration)} / {self._fmt(self._duration)}"
        )
        if self._player:
            self._player.seek_norm(norm)

    def _on_in_changed(self, norm: float):
        self._update_in_out_labels()
        if self._timeline._pos < norm:
            import time
            self._seek_suppress_until = time.monotonic() + 1.5
            self._timeline.set_position(norm)
            self._pos_lbl.setText(
                f"{self._fmt(norm * self._duration)} / {self._fmt(self._duration)}"
            )
            if self._player:
                self._player.seek_norm(norm)

    def _on_out_changed(self, norm: float):
        self._update_in_out_labels()
        # Move playhead immediately to out point when handle is dragged past it
        if self._timeline._pos > norm:
            self._timeline.set_position(norm)
            self._pos_lbl.setText(
                f"{self._fmt(norm * self._duration)} / {self._fmt(self._duration)}"
            )
            if self._player:
                self._player.seek_norm(norm)

    def _update_in_out_labels(self):
        in_s  = self._timeline.in_point  * self._duration
        out_s = self._timeline.out_point * self._duration
        self._in_lbl.setText(f"In: {in_s:.2f}s")
        self._out_lbl.setText(f"Out: {out_s:.2f}s")

    def _do_export(self):
        if not self._clip:
            return
        in_s  = self._timeline.in_point  * self._duration
        out_s = self._timeline.out_point * self._duration
        trimming = not (self._timeline.in_point == 0.0 and
                        self._timeline.out_point == 1.0)

        def _run_export(settings, on_progress, on_done):
            output_name = settings["name"]
            do_sdr      = settings["do_sdr"]
            in_s        = settings["in_s"]
            out_s       = settings["out_s"]
            trimming    = settings["trimming"]
            export_dir  = settings["export_dir"]
            preset      = settings.get("preset", "keep")
            out_suffix  = ".mp4" if preset in ("share", "editor") else self._clip.path.suffix
            export_dir.mkdir(parents=True, exist_ok=True)
            output_name = _unique_export_name(export_dir, output_name, out_suffix)
            out_path    = export_dir / (output_name + out_suffix)

            if trimming:
                on_progress("Trimming...", -1.0)
                clip_ref = self._clip
                def _after_trim(success, trimmed_path):
                    if not success:
                        on_done(False, trimmed_path); return
                    tmp_clip = Clip(path=trimmed_path, game=clip_ref.game,
                                    date=clip_ref.date, filename=trimmed_path.name,
                                    is_hdr=clip_ref.is_hdr)
                    def _cleanup(ok, p):
                        try:
                            if trimmed_path.exists():
                                trimmed_path.unlink()
                        except Exception:
                            pass
                        on_done(ok, p)
                    export_clip(tmp_clip, out_path,
                                do_sdr=do_sdr, size_mb=settings.get("size_mb"),
                                fps=settings.get("fps"),
                                on_progress=on_progress, on_done=_cleanup)
                trim_clip(self._clip, in_s, out_s, _after_trim,
                          output_dir=export_dir, output_name=output_name + "_trim")
            else:
                export_clip(self._clip, out_path,
                            do_sdr=do_sdr, size_mb=settings.get("size_mb"),
                            fps=settings.get("fps"),
                            on_progress=on_progress, on_done=on_done)

        dlg = ExportDialog(
            clip=self._clip,
            suggested_name=getattr(self, "_suggested_name", self._clip.suggested_export_name),
            in_s=in_s, out_s=out_s, trimming=trimming,
            config=self._config,
            on_export=_run_export,
            parent=self,
        )
        dlg.exec()

    def _on_tracks_probed(self, n_tracks: int, volumes: list, mutes: list):
        if self._player:
            self._player.set_audio_tracks(n_tracks, volumes, mutes)

    def _on_track_volume(self, track_idx: int, volume: float):
        if self._player:
            self._player.set_track_volume(track_idx, volume)

    def _on_track_mute(self, track_idx: int, muted: bool):
        if self._player:
            self._player.set_track_mute(track_idx, muted)

    def _on_back(self):
        self._pos_timer.stop()
        if self._player:
            self._player.stop()
        self._player = None
        self.back_requested.emit()

    def closeEvent(self, e):
        self._pos_timer.stop()
        if self._player:
            self._player.stop()
        self._video_widget.cleanup()
        super().closeEvent(e)


# ── Main clips tab ─────────────────────────────────────────────────────────

class ClipsTab(QWidget):
    def __init__(self, config):
        super().__init__()
        self.config = config
        self._clips: list[Clip] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._stack = QStackedWidget()

        self._grid_view = GridView(config)
        self._grid_view.clip_selected.connect(self._open_player)
        self._grid_view.connect_refresh(self.refresh)

        self._player_view = PlayerView(config)
        self._player_view.back_requested.connect(self._close_player)
        self._player_view.export_done.connect(self.refresh)

        self._stack.addWidget(self._grid_view)   # index 0
        self._stack.addWidget(self._player_view) # index 1
        layout.addWidget(self._stack)

        # File system watcher — auto-refresh when new clips are saved
        self._watcher = QFileSystemWatcher()
        self._watcher.directoryChanged.connect(self._on_dir_changed)
        self._refresh_debounce = QTimer()
        self._refresh_debounce.setSingleShot(True)
        self._refresh_debounce.setInterval(1500)
        self._refresh_debounce.timeout.connect(self._refresh_keep_pos)

        QTimer.singleShot(500, self.refresh)

    def _watch_output_dir(self):
        """Watch output dir and all subdirs."""
        from pathlib import Path as _P
        p = _P(self.config.output_dir)
        if not p.exists():
            return
        existing = set(self._watcher.directories())
        for d in [p] + list(p.rglob("*")):
            if d.is_dir() and str(d) not in existing:
                self._watcher.addPath(str(d))

    def _on_dir_changed(self, path: str):
        """New file/dir detected — debounce then refresh keeping position."""
        self._watcher.addPath(path)
        self._refresh_debounce.start()

    def _refresh_keep_pos(self):
        """Refresh clip list without resetting navigation position."""
        self._clips = scan_library(self.config.output_dir)
        self._grid_view.update_clips(self._clips)
        probe_clips_async(self._clips,
                          lambda: QTimer.singleShot(0, self._on_probe_done))
        self._watch_output_dir()

    def refresh(self):
        """Refresh keeping current nav position."""
        self._clips = scan_library(self.config.output_dir)
        self._grid_view.update_clips(self._clips)
        probe_clips_async(self._clips,
                          lambda: QTimer.singleShot(0, self._on_probe_done))
        self._watch_output_dir()

    def _on_probe_done(self):
        self._grid_view.update_clips(self._clips)
        self._grid_view._refresh_view()  # re-render cards with durations
        _purge_unused_thumbnails(self._clips)

    def _open_player(self, clip: Clip):
        self._player_view.load(clip)
        self._stack.setCurrentIndex(1)

    def _close_player(self):
        self._stack.setCurrentIndex(0)
