# SPDX-License-Identifier: GPL-3.0-or-later
"""
Embedded mpv player using python-mpv's MpvRenderContext (OpenGL render API).
Based on the official mpv Qt OpenGL example (mpv-player/mpv-examples).
Works on Wayland by rendering into a QOpenGLWidget FBO rather than using --wid.
"""
import ctypes
import logging
from pathlib import Path
from typing import Callable, Optional

from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel, QSizePolicy
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtOpenGLWidgets import QOpenGLWidget
from PyQt6.QtGui import QSurfaceFormat

logger = logging.getLogger(__name__)

# Video tone-mapping segment for lavfi-complex — applied for HDR clips.
# [vid1] = mpv's first video stream input, [vo] = video output.
# Clips at 100 nits: anything brighter (flashbangs, sky) maps to white.
# setparams tags the output BT.709 so mpv won't double-tone-map.
_HDR_VF = (
    "[vid1]zscale=t=linear:npl=80,"
    "format=gbrpf32le,"
    "zscale=p=bt709,"
    "tonemap=tonemap=hable:desat=0,"
    "zscale=t=bt709:m=bt709:r=tv,"
    "format=yuv420p,"
    "setparams=color_trc=bt709:colorspace=bt709:color_primaries=bt709[vo]"
)


def _set_default_format():
    """Set a permissive OpenGL format that works with NVIDIA on Wayland."""
    fmt = QSurfaceFormat()
    # Don't request core profile — compatibility profile works better with EGL/NVIDIA
    fmt.setVersion(2, 1)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CompatibilityProfile)
    fmt.setSwapInterval(1)
    QSurfaceFormat.setDefaultFormat(fmt)


class MpvWidget(QOpenGLWidget):
    """
    QOpenGLWidget that renders mpv video via libmpv's OpenGL render API.
    Uses the official mpv Qt OpenGL pattern: render into defaultFramebufferObject().
    """
    duration_ready   = pyqtSignal(float)
    position_changed = pyqtSignal(float)
    playback_ended   = pyqtSignal()
    _update_requested  = pyqtSignal()
    _refresh_frame_sig = pyqtSignal()

    def __init__(self, parent=None):
        _set_default_format()
        super().__init__(parent)
        self._mpv = None
        self._ctx = None
        self._duration = 0.0
        self._ready = False
        self._pending_path: Optional[Path] = None
        self._pending_is_hdr: bool = False
        self._is_hdr: bool = False
        # Audio state — initialized here so set_audio_tracks() before GL ready works
        self._n_audio_tracks = 1
        self._track_volumes  = [1.0]
        self._track_mutes    = [False]
        self._pending_audio  = False

        self.setMinimumHeight(200)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._update_requested.connect(self._qt_update, Qt.ConnectionType.QueuedConnection)
        self._refresh_frame_sig.connect(self._refresh_frame, Qt.ConnectionType.QueuedConnection)

    # ------------------------------------------------------------------ #
    #  OpenGL lifecycle                                                    #
    # ------------------------------------------------------------------ #

    def initializeGL(self):
        try:
            import mpv as _mpv
        except ImportError:
            logger.error("python-mpv not installed")
            return

        try:
            self._mpv = _mpv.MPV(
                vo="libmpv",
                keep_open="yes",
                pause=True,
                really_quiet=False,
                msg_level="all=warn",
                hr_seek="yes",
                force_seekable=True,
                tone_mapping="clip",
                demuxer_lavf_o="fflags=+genpts+igndts,analyzeduration=20000000,probesize=50000000",
            )
            # Note: _n_audio_tracks, _track_volumes, _track_mutes, _pending_audio
            # are initialized in __init__ and NOT reset here so pre-GL set_audio_tracks() works

            # Observe properties
            @self._mpv.property_observer("time-pos")
            def _on_pos(name, value):
                if value is not None:
                    self.position_changed.emit(float(value))

            @self._mpv.property_observer("duration")
            def _on_dur(name, value):
                logger.info(f"mpv duration: {value} pending_audio={self._pending_audio} n_tracks={self._n_audio_tracks}")
                if value is not None:
                    self._duration = float(value)
                    self.duration_ready.emit(float(value))
                    # Always apply audio filter when file loads — covers both
                    # the pending case and the race where duration fires fast
                    if self._n_audio_tracks > 1:
                        logger.info("Applying audio filter on duration ready")
                        self._apply_audio_filter()
                    self._pending_audio = False
                    # Refresh first frame so HDR tone-mapping pipeline is applied
                    self._refresh_frame_sig.emit()

            @self._mpv.event_callback("end-file")
            def _on_end(event):
                self.playback_ended.emit()

            # Build render context — proc address callback must be a ctypes function
            from PyQt6.QtGui import QOpenGLContext as _QGLCtx
            _MpvGlGetProcAddressFn = ctypes.CFUNCTYPE(
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p
            )

            def _get_proc_addr_py(_, name):
                ctx = _QGLCtx.currentContext()
                if ctx is None:
                    return None
                fn = ctx.getProcAddress(name)
                return ctypes.cast(int(fn), ctypes.c_void_p).value if fn else None

            self._proc_addr_fn = _MpvGlGetProcAddressFn(_get_proc_addr_py)

            self._ctx = _mpv.MpvRenderContext(
                self._mpv,
                "opengl",
                opengl_init_params={"get_proc_address": self._proc_addr_fn},
            )
            self._ctx.update_cb = self._on_mpv_update

            self._ready = True
            logger.info("mpv OpenGL render context ready")

            # Fallback render timer — ensures frames are painted even if
            # the update callback is missed (XWayland quirk)
            self._render_timer = QTimer(self)
            self._render_timer.setInterval(16)  # ~60fps
            self._render_timer.timeout.connect(self._force_repaint)
            self._render_timer.start()

            # Load pending file — deferred so initializeGL fully returns first.
            # Playing inside initializeGL (before Qt finishes GL setup) causes
            # the first render to show garbled frames; deferring to the next
            # event loop tick avoids that race.
            logger.info(f"GL ready — pending={self._pending_path}")
            if self._pending_path:
                path = self._pending_path
                self._pending_path = None
                QTimer.singleShot(0, lambda p=path: self._play_pending(p))
                logger.info("Pending path deferred")

        except Exception as e:
            logger.error(f"mpv GL init failed: {e}")
            self._ready = False

    def paintGL(self):
        if not self._ready or self._ctx is None:
            return
        try:
            fbo = self.defaultFramebufferObject()
            self._ctx.render(
                flip_y=True,
                opengl_fbo={"fbo": fbo, "w": self.width(), "h": self.height()},
            )
        except Exception as e:
            logger.debug(f"paintGL: {e}")

    def _on_mpv_update(self):
        # Called from mpv thread — emit signal to safely dispatch to Qt main thread
        self._update_requested.emit()

    def _qt_update(self):
        if not self._ready or not self._ctx:
            return
        if self._ctx.update():
            if self.window().isMinimized():
                self.makeCurrent()
                self.paintGL()
                self.context().swapBuffers(self.context().surface())
                self.doneCurrent()
            else:
                # repaint() is synchronous and works under XWayland
                # unlike update() which may be deferred indefinitely
                self.repaint()

    # ------------------------------------------------------------------ #
    #  Playback control                                                    #
    # ------------------------------------------------------------------ #

    def set_audio_tracks(self, n_tracks: int,
                         volumes: list = None, mutes: list = None):
        """Store track config and apply lavfi filter once mpv has loaded the file."""
        self._n_audio_tracks = n_tracks
        self._track_volumes  = volumes if volumes else [1.0] * n_tracks
        self._track_mutes    = mutes   if mutes   else [False] * n_tracks
        logger.info(f"set_audio_tracks: n={n_tracks} ready={self._ready}")
        # If file already loaded (duration known) apply immediately
        # Otherwise set flag — duration observer will apply it
        if self._ready and self._mpv:
            try:
                if self._mpv.duration:
                    logger.info("set_audio_tracks: duration known, applying now")
                    self._apply_audio_filter()
                    return
            except Exception:
                pass
        logger.info("set_audio_tracks: setting _pending_audio=True")
        self._pending_audio = True

    def set_track_volume(self, idx: int, volume: float):
        if idx < len(self._track_volumes):
            self._track_volumes[idx] = volume
            self._apply_audio_filter()

    def set_track_mute(self, idx: int, muted: bool):
        if idx < len(self._track_mutes):
            self._track_mutes[idx] = muted
            self._apply_audio_filter()

    def _apply_audio_filter(self):
        """Build and apply lavfi amix filter for all audio tracks."""
        if not self._ready or not self._mpv:
            return
        n = self._n_audio_tracks
        if n <= 1:
            # Clear any stale multi-track lavfi-complex from the previous clip —
            # if left active it references aid2/aid3 that don't exist in single-track
            # files, causing mpv to silently fail to load the file entirely.
            try:
                self._mpv["lavfi-complex"] = ""
            except Exception:
                pass
            vol = 0.0 if self._track_mutes[0] else self._track_volumes[0]
            try:
                self._mpv.volume = vol * 100
            except Exception:
                pass
            return
        # Build lavfi filter chain:
        # [aid1]volume=v1[a0];[aid2]volume=v2[a1];[a0][a1]amix=inputs=2:normalize=0[ao]
        parts  = []
        inputs = []
        for i in range(n):
            vol = 0.0 if self._track_mutes[i] else self._track_volumes[i]
            parts.append(f"[aid{i+1}]volume={vol:.3f}[a{i}]")
            inputs.append(f"[a{i}]")
        mix = f"{''.join(inputs)}amix=inputs={n}:normalize=0[ao]"
        graph = ";".join(parts) + ";" + mix
        logger.info(f"Applying lavfi: n_tracks={n} graph={graph}")
        try:
            self._mpv["lavfi-complex"] = graph
            logger.info("lavfi-complex set OK")
        except Exception as e:
            logger.warning(f"lavfi filter failed: {e}")

    def _play_pending(self, path):
        if not (self._ready and self._mpv):
            return
        try:
            self._is_hdr = self._pending_is_hdr
            self._apply_audio_filter()
            self._mpv.pause = True
            self._mpv.play(str(path))
            self._mpv.pause = True
            logger.info(f"Pending path played: {path}")
        except Exception as e:
            logger.error(f"Pending play failed: {e}")

    def load(self, path: Path, is_hdr: bool = False):
        self._duration = 0.0
        self._is_hdr = is_hdr
        self._pending_is_hdr = is_hdr
        logger.info(f"Player.load called: ready={self._ready} path={path} is_hdr={is_hdr}")
        if not self._ready:
            self._pending_path = path
            logger.info("mpv not ready, queuing path")
            return
        logger.info(f"Calling mpv.play: {path}")
        try:
            self._apply_audio_filter()
            self._mpv.pause = True
            self._mpv.play(str(path))
            self._mpv.pause = True
            logger.info("mpv.play() called OK")
        except Exception as e:
            logger.error(f"mpv play: {e}")

    def play_pause(self):
        if self._ready and self._mpv:
            self._mpv.pause = not self._mpv.pause

    def pause(self):
        if self._ready and self._mpv:
            self._mpv.pause = True

    def seek(self, secs: float):
        if self._ready and self._mpv:
            try:
                self._mpv.seek(secs, "absolute")
                # Force frame update while paused — mpv won't redraw otherwise
                if self._mpv.pause:
                    try:
                        self._mpv.command("frame-step")
                        self._mpv.command("frame-back-step")
                    except Exception:
                        pass
            except Exception:
                pass

    def seek_norm(self, norm: float):
        if self._duration > 0:
            self.seek(norm * self._duration)

    def frame_step(self, forward: bool = True):
        if self._ready and self._mpv:
            try:
                self._mpv.command("frame-step" if forward else "frame-back-step")
            except Exception:
                pass

    def stop(self):
        if self._ready and self._mpv:
            try:
                self._mpv.stop()
            except Exception:
                pass

    def _refresh_frame(self):
        """Step forward+back to force mpv to re-render the current frame.
        Called via signal after file load so HDR tone-mapping is fully initialized."""
        if not (self._ready and self._mpv):
            return
        try:
            self._mpv.command("frame-step")
            self._mpv.command("frame-back-step")
        except Exception:
            pass

    def _force_repaint(self):
        if self._ready and self._ctx and self._ctx.update():
            self.repaint()

    def cleanup(self):
        if hasattr(self, '_render_timer') and self._render_timer:
            self._render_timer.stop()
        if self._ctx:
            try:
                self._ctx.update_cb = None
                del self._ctx
            except Exception:
                pass
            self._ctx = None
        if self._mpv:
            try:
                self._mpv.terminate()
                del self._mpv
            except Exception:
                pass
            self._mpv = None
        self._ready = False

    def closeEvent(self, e):
        self.cleanup()
        super().closeEvent(e)

    @property
    def duration(self) -> float:
        return self._duration


class MpvPlayer:
    """Wraps MpvWidget to provide callback-based interface for PlayerView."""

    def __init__(self, widget,
                 on_position=None, on_duration=None, on_end=None):
        self._widget = widget
        if on_position: widget.position_changed.connect(on_position)
        if on_duration:  widget.duration_ready.connect(on_duration)
        if on_end:       widget.playback_ended.connect(on_end)

    def load(self, path, is_hdr=False): self._widget.load(path, is_hdr)
    def set_audio_tracks(self, n, volumes=None, mutes=None):
        self._widget.set_audio_tracks(n, volumes, mutes)
    def set_track_volume(self, idx, volume): self._widget.set_track_volume(idx, volume)
    def set_track_mute(self, idx, muted):    self._widget.set_track_mute(idx, muted)
    def play_pause(self):            self._widget.play_pause()
    def pause(self):                 self._widget.pause()
    def seek(self, secs):            self._widget.seek(secs)
    def seek_norm(self, norm):       self._widget.seek_norm(norm)
    def frame_step(self, forward=True): self._widget.frame_step(forward)
    def stop(self):                  self._widget.stop()
