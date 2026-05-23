#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Export PANNs CNN6 (Pretrained Audio Neural Networks) to ONNX for AutoClip.

CNN6 is a compact AudioSet classifier (527 classes, ~14 MB) trained by
Qiuqiang Kong et al. — MIT licence. Weights are downloaded from Zenodo.

Run once:
    pip install torch panns_inference
    python3 scripts/export_audio_classifier.py

Output: ~/.cache/autoclip/models/panns_cnn6.onnx
After this completes, laughter detection is ready to use.
"""

import sys
import urllib.request
from pathlib import Path

import torch
import torch.nn as nn
import torch.nn.functional as F

OUTPUT_PATH  = Path.home() / ".cache" / "autoclip" / "models" / "panns_cnn6.onnx"
WEIGHTS_URL  = "https://zenodo.org/records/3987831/files/Cnn6_mAP%3D0.343.pth"
WEIGHTS_PATH = Path.home() / "panns_data" / "Cnn6_mAP=0.343.pth"

# Audio config — must match SAMPLE_RATE / WINDOW_SAMPLES in audio_triggers/laughter.py
SAMPLE_RATE    = 32000
WINDOW_SAMPLES = 30720   # 0.96 s at 32 kHz
CLASSES        = 527


# ── CNN6 model definition ─────────────────────────────────────────────────────
# Mirrors the architecture from Kong et al. "PANNs" (MIT licence) exactly so
# that Zenodo checkpoint state-dict keys load without remapping.

from panns_inference.models import (
    Spectrogram, LogmelFilterBank, SpecAugmentation,
    init_layer, init_bn,
)


class ConvBlock5(nn.Module):
    """Single-conv block with 5×5 kernel — CNN6's actual architecture."""
    def __init__(self, in_channels, out_channels):
        super().__init__()
        self.conv1 = nn.Conv2d(in_channels, out_channels, kernel_size=(5, 5),
                               stride=(1, 1), padding=(2, 2), bias=False)
        self.bn1 = nn.BatchNorm2d(out_channels)
        init_layer(self.conv1)
        init_bn(self.bn1)

    def forward(self, x, pool_size=(2, 2), pool_type='avg'):
        x = F.relu_(self.bn1(self.conv1(x)))
        if pool_type == 'avg':
            x = F.avg_pool2d(x, kernel_size=pool_size)
        elif pool_type == 'max':
            x = F.max_pool2d(x, kernel_size=pool_size)
        elif pool_type == 'avg+max':
            x = F.avg_pool2d(x, kernel_size=pool_size) + F.max_pool2d(x, kernel_size=pool_size)
        return x


class Cnn6(nn.Module):
    def __init__(self):
        super().__init__()
        sr, win, hop = SAMPLE_RATE, 1024, 320
        self.spectrogram_extractor = Spectrogram(
            n_fft=win, hop_length=hop, win_length=win,
            window='hann', center=True, pad_mode='reflect',
            freeze_parameters=True,
        )
        self.logmel_extractor = LogmelFilterBank(
            sr=sr, n_fft=win, n_mels=64,
            fmin=50, fmax=14000, ref=1.0, amin=1e-10, top_db=None,
            freeze_parameters=True,
        )
        self.spec_augmenter = SpecAugmentation(
            time_drop_width=64, time_stripes_num=2,
            freq_drop_width=8,  freq_stripes_num=2,
        )
        self.bn0          = nn.BatchNorm2d(64)
        self.conv_block1  = ConvBlock5(in_channels=1,   out_channels=64)
        self.conv_block2  = ConvBlock5(in_channels=64,  out_channels=128)
        self.conv_block3  = ConvBlock5(in_channels=128, out_channels=256)
        self.conv_block4  = ConvBlock5(in_channels=256, out_channels=512)
        self.fc1          = nn.Linear(512, 512, bias=True)
        self.fc_audioset  = nn.Linear(512, CLASSES, bias=True)
        init_bn(self.bn0)
        init_layer(self.fc1)
        init_layer(self.fc_audioset)

    def forward(self, x):
        x = self.spectrogram_extractor(x)
        x = self.logmel_extractor(x)
        x = x.transpose(1, 3)
        x = self.bn0(x)
        x = x.transpose(1, 3)
        x = self.conv_block1(x, pool_size=(2, 2), pool_type='avg')
        x = F.dropout(x, p=0.2, training=self.training)
        x = self.conv_block2(x, pool_size=(2, 2), pool_type='avg')
        x = F.dropout(x, p=0.2, training=self.training)
        x = self.conv_block3(x, pool_size=(2, 2), pool_type='avg')
        x = F.dropout(x, p=0.2, training=self.training)
        x = self.conv_block4(x, pool_size=(2, 2), pool_type='avg')
        x = F.dropout(x, p=0.2, training=self.training)
        x = torch.mean(x, dim=3)
        x1, _ = torch.max(x, dim=2)
        x2     = torch.mean(x, dim=2)
        x = x1 + x2
        x = F.dropout(x, p=0.5, training=self.training)
        x = F.relu_(self.fc1(x))
        return torch.sigmoid(self.fc_audioset(x))   # (batch, 527)


# ── Steps ─────────────────────────────────────────────────────────────────────

def download_weights():
    if WEIGHTS_PATH.exists():
        print(f"  Weights already cached: {WEIGHTS_PATH}")
        return
    WEIGHTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    print(f"  Downloading CNN6 weights (~14 MB) from Zenodo...")
    def _hook(count, block_size, total):
        if total > 0:
            pct = min(100, int(count * block_size * 100 / total))
            print(f"\r  {pct}%", end="", flush=True)
    urllib.request.urlretrieve(WEIGHTS_URL, str(WEIGHTS_PATH), reporthook=_hook)
    print()


def build_model():
    model = Cnn6()
    ckpt  = torch.load(WEIGHTS_PATH, map_location="cpu", weights_only=True)
    state = ckpt.get("model", ckpt)
    missing, unexpected = model.load_state_dict(state, strict=False)
    if missing:
        print(f"  Warning — missing keys: {missing[:5]}")
    model.eval()
    return model


def export(model):
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    dummy = torch.zeros(1, WINDOW_SAMPLES)
    # dynamo=False forces the legacy exporter which embeds weights into the file
    torch.onnx.export(
        model,
        dummy,
        str(OUTPUT_PATH),
        input_names=["waveform"],
        output_names=["predictions"],
        dynamic_axes={"waveform": {0: "batch"}, "predictions": {0: "batch"}},
        opset_version=17,
        dynamo=False,
    )


def verify():
    import numpy as np
    import onnxruntime as ort
    sess = ort.InferenceSession(str(OUTPUT_PATH), providers=["CPUExecutionProvider"])
    inp  = sess.get_inputs()[0]
    print(f"  Input:  {inp.name}  shape={inp.shape}")
    dummy   = np.zeros((1, WINDOW_SAMPLES), dtype=np.float32)
    outputs = sess.run(None, {inp.name: dummy})
    scores  = outputs[0]
    print(f"  Output: shape={scores.shape}")
    if scores.shape[-1] != CLASSES:
        print(f"  ERROR: expected {CLASSES} classes, got {scores.shape[-1]}")
        sys.exit(1)
    size_mb = OUTPUT_PATH.stat().st_size / 1_048_576
    print(f"  Saved:  {OUTPUT_PATH}  ({size_mb:.1f} MB)")


if __name__ == "__main__":
    print("=== AutoClip audio classifier export (PANNs CNN6) ===\n")

    print("Step 1/3  Download weights")
    download_weights()
    print("  OK\n")

    print("Step 2/3  Build and load model")
    model = build_model()
    print("  OK\n")

    print("Step 3/3  Export to ONNX + verify")
    export(model)
    verify()
    print("\nDone. Enable laughter detection in Settings → Audio Triggers → Laughter.")
