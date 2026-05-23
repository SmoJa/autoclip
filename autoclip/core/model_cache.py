# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared ONNX InferenceSession cache — load once, reuse across plugins."""

import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_sessions: dict = {}
_lock = threading.Lock()


class ModelCache:
    @classmethod
    def get_session(cls, model_path: Path):
        """Return (or create) an InferenceSession for model_path. Thread-safe."""
        key = str(model_path)
        with _lock:
            if key not in _sessions:
                import onnxruntime as ort
                opts = ort.SessionOptions()
                opts.inter_op_num_threads = 1
                opts.intra_op_num_threads = 2
                opts.log_severity_level   = 3  # suppress ort verbose output
                sess = ort.InferenceSession(
                    str(model_path), opts,
                    providers=["CPUExecutionProvider"],
                )
                _sessions[key] = sess
                logger.info(f"Loaded ONNX model: {model_path.name}")
            return _sessions[key]
