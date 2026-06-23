# SPDX-License-Identifier: GPL-3.0-or-later
"""
Embedded mpv player.

Linux:   QOpenGLWidget + MpvRenderContext (OpenGL render API).
Windows: QWidget + --wid (native HWND / D3D11 embedding).

On Windows, the OpenGL path crashes because MpvRenderContext init calls the
get_proc_address ctypes callback ~100 times from mpv's render thread. Each call
triggers PyGILState_Ensure() → PyThreadState_New() → heap allocation on the heap
already corrupted by Qt's DLL initialization (Python 3.11.9 + Qt6 bug). Using
--wid avoids MpvRenderContext entirely, so no Python callback is ever invoked from
mpv's C threads at startup.
"""
import ctypes
import logging
import sys as _sys
from pathlib import Path
from typing import Optional

from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel, QSizePolicy
from PyQt6.QtCore import Qt, QTimer, pyqtSignal

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


# ============================================================
#  Windows implementation — native HWND embedding via --wid
# ============================================================

class _MpvWidgetWin32(QWidget):
    """
    mpv player using native Win32 HWND embedding (--wid).
    mpv renders via D3D11 directly into our native window handle.
    No MpvRenderContext, no ctypes callback from mpv threads.
    """
    duration_ready   = pyqtSignal(float)
    position_changed = pyqtSignal(float)
    playback_ended   = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        # Native HWND is required so we can pass it to mpv as --wid
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        # Tell Qt this widget paints its own background; reduces flicker
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setMinimumHeight(200)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        # Keep background black while no video is loaded
        self.setStyleSheet("background: black;")

        self._mpv = None
        self._ready = False
        self._duration = 0.0
        self._pending_path: Optional[Path] = None
        self._pending_is_hdr: bool = False
        self._is_hdr: bool = False
        self._n_audio_tracks = 1
        self._track_volumes  = [1.0]
        self._track_mutes    = [False]
        self._pending_audio  = False
        self._last_pos_emit  = 0.0

    def showEvent(self, event):
        super().showEvent(event)
        if self._mpv is None:
            self._init_mpv()

    def _init_mpv(self):
        import os
        mpvnet_dir = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "mpv.net")
        if os.path.isdir(mpvnet_dir) and mpvnet_dir not in os.environ.get("PATH", ""):
            os.environ["PATH"] = mpvnet_dir + os.pathsep + os.environ.get("PATH", "")
        try:
            import mpv as _mpv
        except (ImportError, OSError) as e:
            logger.error(f"python-mpv / libmpv not available: {e}")
            return

        try:
            wid = int(self.winId())
            self._mpv = _mpv.MPV(
                wid=str(wid),
                keep_open="yes",
                pause=True,
                really_quiet=False,
                msg_level="all=warn",
                hr_seek="yes",
                force_seekable=True,
                tone_mapping="hable",
                hwdec="auto-safe",
                # Disable mpv's own input handling — Qt owns events
                input_default_bindings=False,
                input_vo_keyboard=False,
                demuxer_lavf_o="fflags=+genpts+igndts,analyzeduration=20000000,probesize=50000000",
            )

            @self._mpv.property_observer("time-pos")
            def _on_pos(name, value):
                # time-pos fires per video frame on mpv's event thread. Emitting a
                # cross-thread Qt signal that often (30-60/s) floods the GUI event
                # loop — laggy/double-clicks and a stuttering playhead. Throttle to
                # ~15/s, which is plenty smooth for a playhead.
                if value is None:
                    return
                import time as _t
                now = _t.monotonic()
                if now - self._last_pos_emit < 0.066:
                    return
                self._last_pos_emit = now
                self.position_changed.emit(float(value))

            @self._mpv.property_observer("duration")
            def _on_dur(name, value):
                logger.info(f"mpv duration: {value} pending_audio={self._pending_audio} n_tracks={self._n_audio_tracks}")
                if value is not None:
                    self._duration = float(value)
                    self.duration_ready.emit(float(value))
                    if self._n_audio_tracks > 1:
                        logger.info("Applying audio filter on duration ready")
                        self._apply_audio_filter()
                    self._pending_audio = False

            @self._mpv.event_callback("end-file")
            def _on_end(event):
                self.playback_ended.emit()

            self._ready = True
            logger.info(f"mpv WID render context ready (wid={wid:#x})")

            if self._pending_path:
                path = self._pending_path
                self._pending_path = None
                QTimer.singleShot(0, lambda p=path: self._play_pending(p))
                logger.info("Pending path deferred")

        except Exception as e:
            logger.error(f"mpv WID init failed: {e}")
            self._ready = False

    def set_audio_tracks(self, n_tracks: int, volumes=None, mutes=None):
        self._n_audio_tracks = n_tracks
        self._track_volumes  = volumes if volumes else [1.0] * n_tracks
        self._track_mutes    = mutes   if mutes   else [False] * n_tracks
        logger.info(f"set_audio_tracks: n={n_tracks} ready={self._ready}")
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
        if not self._ready or not self._mpv:
            return
        n = self._n_audio_tracks
        if n <= 1:
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
        if not (self._ready and self._mpv):
            return
        try:
            self._mpv.command("frame-step")
            self._mpv.command("frame-back-step")
        except Exception:
            pass

    def cleanup(self):
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


# ============================================================
#  Linux implementation — OpenGL render context
# ============================================================

def _set_default_format():
    """Set a permissive OpenGL format that works with NVIDIA on Wayland."""
    from PyQt6.QtOpenGLWidgets import QOpenGLWidget as _qog
    from PyQt6.QtGui import QSurfaceFormat
    fmt = QSurfaceFormat()
    fmt.setVersion(2, 1)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CompatibilityProfile)
    fmt.setSwapInterval(1)
    QSurfaceFormat.setDefaultFormat(fmt)


if _sys.platform != "win32":
    from PyQt6.QtOpenGLWidgets import QOpenGLWidget as _QOpenGLWidget
    # Must be called before QApplication creates any window surfaces.
    # Runs at import time — player.py is imported before run_app() is entered.
    _set_default_format()

    class _MpvWidgetLinux(_QOpenGLWidget):
        """
        QOpenGLWidget that renders mpv video via libmpv's OpenGL render API.
        """
        duration_ready   = pyqtSignal(float)
        position_changed = pyqtSignal(float)
        playback_ended   = pyqtSignal()
        _update_requested  = pyqtSignal()
        _refresh_frame_sig = pyqtSignal()

        def __init__(self, parent=None):
            super().__init__(parent)
            self._mpv = None
            self._ctx = None
            self._duration = 0.0
            self._ready = False
            self._pending_path: Optional[Path] = None
            self._pending_is_hdr: bool = False
            self._is_hdr: bool = False
            self._n_audio_tracks = 1
            self._track_volumes  = [1.0]
            self._track_mutes    = [False]
            self._pending_audio  = False

            self.setMinimumHeight(200)
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            self._update_requested.connect(self._qt_update, Qt.ConnectionType.QueuedConnection)
            self._refresh_frame_sig.connect(self._refresh_frame, Qt.ConnectionType.QueuedConnection)

        def initializeGL(self):
            self._init_mpv()

        def _init_mpv(self):
            """Initialize (or reinitialize) mpv and its render context.
            Must be called with an active OpenGL context (guaranteed by Qt when
            called from initializeGL; caller must makeCurrent() otherwise)."""
            try:
                import mpv as _mpv
            except (ImportError, OSError) as e:
                logger.error(f"python-mpv / libmpv not available: {e}")
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
                    tone_mapping="hable",
                    hwdec="auto-safe",
                    demuxer_lavf_o="fflags=+genpts+igndts,analyzeduration=20000000,probesize=50000000",
                )

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
                        if self._n_audio_tracks > 1:
                            logger.info("Applying audio filter on duration ready")
                            self._apply_audio_filter()
                        self._pending_audio = False
                        self._refresh_frame_sig.emit()

                @self._mpv.event_callback("end-file")
                def _on_end(event):
                    self.playback_ended.emit()

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

                if not hasattr(self, '_render_timer'):
                    self._render_timer = QTimer(self)
                    self._render_timer.setInterval(16)
                    self._render_timer.timeout.connect(self._force_repaint)
                self._render_timer.start()

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
                    self.repaint()

        def set_audio_tracks(self, n_tracks: int, volumes=None, mutes=None):
            self._n_audio_tracks = n_tracks
            self._track_volumes  = volumes if volumes else [1.0] * n_tracks
            self._track_mutes    = mutes   if mutes   else [False] * n_tracks
            logger.info(f"set_audio_tracks: n={n_tracks} ready={self._ready}")
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
            if not self._ready or not self._mpv:
                return
            n = self._n_audio_tracks
            if n <= 1:
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
            if self._mpv is None:
                # mpv was torn down (tray); reinitialize with active GL context
                self._pending_path = path
                try:
                    self.makeCurrent()
                    self._init_mpv()
                finally:
                    self.doneCurrent()
                return
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
            if not (self._ready and self._mpv):
                return
            try:
                self._mpv.command("frame-step")
                self._mpv.command("frame-back-step")
            except Exception:
                pass

        def _force_repaint(self):
            if self._ready and self._ctx:
                self._ctx.update()
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


# Select implementation at module load time
MpvWidget = _MpvWidgetWin32 if _sys.platform == "win32" else _MpvWidgetLinux


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
